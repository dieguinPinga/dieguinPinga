"""OH-DUO-03: supervisor en tiempo real (solo observa, registra y alerta; nunca interviene).

Componentes, separados:
  Programador  OpenHands (Qwen, Gemma u otro) dentro de su sandbox. No se toca.
  Observador   FuenteEventos: lee, desde fuera y en solo lectura, los eventos que el SDK persiste uno por uno
               (estado/conversacion/<id>/events/event-NNNNN-<id>.json; escritura atómica: temporal + os.replace).
  Detector     las reglas de OH-DUO-02 (oh-duo-02/estancamiento.py), importadas sin modificarlas: después de
               cada evento nuevo se aplican al prefijo recibido.
  Registrador  Registro: alertas.jsonl (solo se agrega), supervision.json y supervision.md.

Modos:
  reproducir --ejecucion DIR [--ritmo N]   reproduce una ejecución terminada evento por evento (fase A)
  vigilar --corrida DIR                    observa en vivo una corrida de OH-DUO-01 mientras duo.py ejecuta (fase B)

No lee pruebas ocultas, referencias ni verificacion/; no escribe en lo observado; no envía nada al agente.
Solo biblioteca estándar.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

VERSION = "0.1.0"
AQUI = Path(__file__).resolve().parent
GRUPALES = ("A1_repeticion_equivalente_sin_cambio", "A2_fallos_iguales_sin_cambio_del_codigo")


def ahora() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def cargar_detector(oh_duo_02: Path):
    spec = importlib.util.spec_from_file_location("oh_duo_02_estancamiento", oh_duo_02 / "estancamiento.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------------------------------- observador

class FuenteEventos:
    """Entrega los eventos de una carpeta en orden de índice, sin saltear ninguno: si llega el 7 antes que el 6,
    espera al 6. Ignora temporales y marcadores del SDK (no cumplen su patrón de nombres)."""

    def __init__(self, directorio: Path, detector):
        self.dir, self.E = directorio, detector
        self.siguiente, self.pendientes, self.ilegibles = 0, {}, {}

    def nuevos(self) -> list[dict]:
        if self.dir.is_dir():
            for p in self.dir.iterdir():
                m = self.E.EVENTO_RE.match(p.name)
                if not m or int(m.group(1)) < self.siguiente or int(m.group(1)) in self.pendientes:
                    continue
                try:
                    e = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    self.ilegibles[p.name] = self.ilegibles.get(p.name, 0) + 1   # se reintenta en la próxima vuelta
                    continue
                e["_idx"] = int(m.group(1))
                self.pendientes[e["_idx"]] = e
        salida = []
        while self.siguiente in self.pendientes:
            salida.append(self.pendientes.pop(self.siguiente))
            self.siguiente += 1
        return salida

    def huecos(self) -> list[int]:
        """Índices que faltan antes de eventos ya recibidos (para informar al cerrar)."""
        return [i for i in range(self.siguiente, max(self.pendientes) + 1) if i not in self.pendientes] if self.pendientes else []


# -------------------------------------------------------------------------------------------- detector

def clave(alerta: dict) -> str:
    """Identidad de un patrón: una alerta por grupo (A1/A2: acción o pruebas + primer evento del grupo, que no
    cambia al crecer el grupo) y una por regla en las demás."""
    if alerta["regla"] in GRUPALES:
        partes = [alerta["regla"], alerta.get("accion") or "|".join(alerta.get("pruebas") or []),
                  str(alerta["evidencia"][0]["evento"])]
    else:
        partes = [alerta["regla"]]
    return " :: ".join(partes)


def iteracion_umbral(alerta: dict, u: dict) -> int | None:
    """Primera iteración en la que la regla alcanzó su umbral, según la evidencia de OH-DUO-02."""
    it, r = alerta["iteraciones"], alerta["regla"]
    n = {"A1_repeticion_equivalente_sin_cambio": u["repeticion_equivalente_sin_cambio"],
         "A2_fallos_iguales_sin_cambio_del_codigo": u["fallos_iguales_sin_cambio"],
         "A4_ediciones_rechazadas_o_sin_efecto": u["ediciones_rechazadas_o_sin_efecto"]}.get(r)
    if n is not None:
        return it[n - 1] if len(it) >= n else None
    if r == "A6_iteraciones_finales_sin_progreso":
        return it[0] - 1 + u["iteraciones_finales_sin_progreso"]
    return it[0] if it else None


def metadatos(ejecucion: Path, E) -> tuple[set[str], dict]:
    """Archivos protegidos y huellas del código original, igual que OH-DUO-02 (analizar_ejecucion): del contrato
    de la misión (mision.json) y de la tarea que recibió el agente. Nunca de ocultas ni de la referencia."""
    mision = E.leer_json(ejecucion / "instancia" / "mision.json") or E.leer_json(ejecucion / "mision.json") or {}
    mod = mision.get("modulo")
    protegidos = {f"test_{mod}.py"} if mod else set()
    tarea = next((d for d in (ejecucion / "instancia" / "tarea", ejecucion / "tarea") if d.is_dir()), None)
    originales = {f.name: E.sha(f.read_text(encoding="utf-8"))[:16]
                  for f in sorted(tarea.iterdir()) if f.is_file() and f.suffix == ".py"} if tarea else {}
    return protegidos, originales


class Supervisor:
    """Aplica el detector al prefijo de eventos y convierte los cambios en entradas del registro."""

    def __init__(self, E, u: dict, protegidos: set[str], originales: dict, registro: "Registro"):
        self.E, self.u, self.protegidos, self.originales, self.reg = E, u, protegidos, originales, registro
        self.eventos: list[dict] = []
        self.activas: dict[str, dict] = {}       # clave → estado de la alerta
        self.historial: list[dict] = []          # todas las alertas que existieron (también retiradas)
        self.estancamiento_provisional = None

    def procesar(self, nuevos: list[dict]) -> None:
        for e in nuevos:
            self.eventos.append(e)
            r = self.E.analizar(list(self.eventos), [], self.u, self.protegidos, self.originales, None)
            self._comparar(r, e)

    def _comparar(self, r: dict, e: dict) -> None:
        iteracion = r["iteraciones"]
        vistas = {}
        for a in r["alertas"]:
            k = clave(a)
            vistas[k] = a
            if k not in self.activas:
                entrada = {"clave": k, "regla": a["regla"], "estado": "provisional", "aparecio": ahora(),
                           "evento_de_deteccion": e["_idx"], "iteracion_de_deteccion": iteracion,
                           "iteracion_umbral": iteracion_umbral(a, self.u), "veces": a["veces"],
                           "accion": a.get("accion"), "evidencia": a.get("evidencia") or a.get("detalle")}
                self.activas[k] = entrada
                self.historial.append(entrada)
                self.reg.anotar("alerta_nueva", entrada)
            elif a["veces"] != self.activas[k]["veces"]:
                self.activas[k]["veces"] = a["veces"]
                self.reg.anotar("alerta_actualizada", {"clave": k, "veces": a["veces"], "evento": e["_idx"],
                                                       "iteracion": iteracion})
        for k in [k for k in self.activas if k not in vistas]:
            entrada = self.activas.pop(k)
            entrada.update(estado="retirada", retirada_en_evento=e["_idx"], retirada_en_iteracion=iteracion)
            self.reg.anotar("alerta_retirada", {"clave": k, "evento": e["_idx"], "iteracion": iteracion})
        v = r["veredicto"]["estancamiento"]
        if v is True and self.estancamiento_provisional is None:
            self.estancamiento_provisional = {"evento": e["_idx"], "iteracion": iteracion, "momento": ahora(),
                                              "patrones": r["veredicto"].get("patrones")}
            self.reg.anotar("estancamiento_provisional", self.estancamiento_provisional)

    def cerrar(self, base_state: dict | None, avisos: list[str]) -> dict:
        """Resultado definitivo: el detector sobre todos los eventos, con el estado final del SDK."""
        final = self.E.analizar(list(self.eventos), avisos, self.u, self.protegidos, self.originales, base_state)
        claves_finales = {clave(a) for a in final["alertas"]}
        for entrada in self.historial:
            if entrada["estado"] == "provisional":
                entrada["estado"] = "definitiva" if entrada["clave"] in claves_finales else "descartada"
        self.reg.anotar("cierre", {"terminacion": final["terminacion"], "veredicto": final["veredicto"]})
        return final


# ------------------------------------------------------------------------------------------ registrador

class Registro:
    """alertas.jsonl se escribe línea por línea (con flush) durante la observación: si el proceso se corta,
    lo registrado hasta ese momento queda."""

    def __init__(self, destino: Path):
        destino.mkdir(parents=True, exist_ok=True)
        self.destino = destino
        self._f = open(destino / "alertas.jsonl", "a", encoding="utf-8")

    def anotar(self, tipo: str, datos: dict) -> None:
        self._f.write(json.dumps({"momento": ahora(), "tipo": tipo, **datos}, ensure_ascii=False) + "\n")
        self._f.flush()

    def cerrar(self, informe: dict) -> None:
        self._f.close()
        (self.destino / "supervision.json").write_text(json.dumps(informe, ensure_ascii=False, indent=1), encoding="utf-8")
        (self.destino / "supervision.md").write_text(markdown(informe), encoding="utf-8")


def markdown(i: dict) -> str:
    f, v = i["final"], i["final"]["veredicto"]
    ls = [f"# Supervisión: {i['ejecucion']}", "", f"Modo `{i['modo']}`, observador {i['observador']}. "
          f"Eventos: {i['eventos']}. Iteraciones: {f['iteraciones']}.", "",
          f"- **Terminación (definitiva):** `{f['terminacion']['tipo']}`",
          f"- **Estancamiento (definitivo):** {v['estancamiento']} ({v.get('confianza')})",
          f"- **Estancamiento provisional detectado en:** "
          + (f"iteración {i['estancamiento_provisional']['iteracion']} (evento {i['estancamiento_provisional']['evento']}), "
             f"{i['anticipacion_iteraciones']} iteraciones antes del final" if i["estancamiento_provisional"] else "nunca"),
          f"- **Coincide con OH-DUO-02 offline:** {i['coincide_con_oh_duo_02']}", ""]
    ls += [f"- Nota: {n}" for n in i["notas"]]
    ls += ["", "| Alerta | Umbral alcanzado (iter.) | Detectada (iter. / evento) | Veces | Estado |", "|---|---|---|---|---|"]
    for a in i["alertas"]:
        ls.append(f"| {a['clave'][:80]} | {a['iteracion_umbral']} | {a['iteracion_de_deteccion']} / {a['evento_de_deteccion']} | "
                  f"{a['veces']} | {a['estado']} |")
    if not i["alertas"]:
        ls.append("| (ninguna) | | | | |")
    return "\n".join(ls) + "\n"


# --------------------------------------------------------------------------------------- integraciones

def carpeta_eventos(ejecucion: Path) -> Path | None:
    raices = sorted((ejecucion / "instancia" / "resultados").glob("*")) if (ejecucion / "instancia").is_dir() else [ejecucion]
    dirs = [d for r in raices for d in sorted(r.glob("estado/conversacion/*/events"))]
    return dirs[-1] if dirs else None


def informe(ejecucion: Path, modo: str, sup: Supervisor, final: dict, fuente: FuenteEventos, E, u: dict) -> dict:
    """Junta el resultado y lo compara con OH-DUO-02 aplicado offline sobre la misma ejecución."""
    offline = E.analizar_ejecucion(ejecucion, u)
    comparar = ("veredicto", "terminacion", "alertas", "estado_ejecucion_sugerido")
    notas = list(final["notas"])
    if fuente.huecos():
        notas.append(f"eventos que nunca llegaron: {fuente.huecos()[:20]}")
    if fuente.ilegibles:
        notas.append(f"archivos que no se pudieron leer al menos una vez (se reintentaron): {sorted(fuente.ilegibles)[:10]}")
    ep = sup.estancamiento_provisional
    return {"observador": VERSION, "ejecucion": ejecucion.name, "modo": modo, "eventos": len(sup.eventos),
            "estancamiento_provisional": ep,
            "anticipacion_iteraciones": final["iteraciones"] - ep["iteracion"] if ep else None,
            "alertas": sup.historial, "final": {k: final[k] for k in ("iteraciones", "terminacion", "veredicto",
                                                                       "estado_ejecucion_sugerido", "alertas")},
            "coincide_con_oh_duo_02": all(final[k] == offline[k] for k in comparar), "notas": notas}


def reproducir(ejecucion: Path, salida: Path, E, u: dict, ritmo: float = 0.0) -> dict:
    """Fase A: entrega los eventos ya registrados de a uno, en orden, como si fueran llegando."""
    d = carpeta_eventos(ejecucion)
    if d is None:
        raise SystemExit(f"{ejecucion}: no hay eventos del SDK para reproducir")
    fuente = FuenteEventos(d, E)
    protegidos, originales = metadatos(ejecucion, E)
    reg = Registro(salida)
    sup = Supervisor(E, u, protegidos, originales, reg)
    for e in fuente.nuevos():
        sup.procesar([e])
        if ritmo:
            time.sleep(ritmo)
    final = sup.cerrar(E.leer_json(d.parent / "base_state.json"), [])
    inf = informe(ejecucion, "reproduccion", sup, final, fuente, E, u)
    reg.cerrar(inf)
    return inf


def vigilar_ejecucion(ejecucion: Path, salida: Path, E, u: dict, intervalo: float, espera_cierre: float,
                      termina=lambda: False) -> dict:
    """Fase B: observa una ejecución mientras ocurre. Termina cuando aparece ejecucion.json (OH-DUO-01 la
    congeló), o cuando hubo un evento terminal y después `espera_cierre` segundos sin eventos nuevos."""
    protegidos, originales = metadatos(ejecucion, E)
    reg = Registro(salida)
    sup = Supervisor(E, u, protegidos, originales, reg)
    fuente, ultimo, terminal = None, time.monotonic(), False
    while True:
        if fuente is None and (d := carpeta_eventos(ejecucion)) is not None:
            fuente = FuenteEventos(d, E)
            reg.anotar("eventos_encontrados", {"ruta": str(d)})
        congelada = (ejecucion / "ejecucion.json").exists()   # se mira ANTES de leer: después ya no llega nada
        nuevos = fuente.nuevos() if fuente is not None else []
        if nuevos:
            sup.procesar(nuevos)
            ultimo = time.monotonic()
            terminal = terminal or any(e.get("kind") == "ConversationErrorEvent" or
                                       (e.get("kind") == "ActionEvent" and e.get("tool_name") == "finish") for e in nuevos)
        if congelada and not nuevos and (fuente is not None or carpeta_eventos(ejecucion) is None):
            break
        if terminal and time.monotonic() - ultimo > espera_cierre:
            break
        if termina():
            reg.anotar("interrumpido", {})
            break
        time.sleep(intervalo)
    base = E.leer_json(fuente.dir.parent / "base_state.json") if fuente else None
    final = sup.cerrar(base, [] if fuente else ["no aparecieron eventos durante la observación"])
    inf = informe(ejecucion, "vivo", sup, final, fuente or FuenteEventos(Path("/nonexistent"), E), E, u)
    reg.cerrar(inf)
    return inf


def vigilar_corrida(corrida: Path, salida: Path, E, u: dict, intervalo: float = 0.5, espera_cierre: float = 10.0,
                    maximo: int | None = None, termina=lambda: False) -> list[dict]:
    """Observa, de a una, las ejecuciones que OH-DUO-01 va creando en corrida/ejecuciones/. Las ya congeladas
    antes de empezar se omiten (para esas está el modo reproducir)."""
    previas = {d.name for d in (corrida / "ejecuciones").glob("*") if (d / "ejecucion.json").exists()}
    hechas, informes = set(previas), []
    while not termina() and (maximo is None or len(informes) < maximo):
        nuevas = sorted(d for d in (corrida / "ejecuciones").glob("*") if d.is_dir() and d.name not in hechas
                        and (d / "instancia").is_dir())
        if not nuevas:
            time.sleep(intervalo)
            continue
        e = nuevas[0]
        print(f"== observando {e.name}", flush=True)
        inf = vigilar_ejecucion(e, salida / e.name, E, u, intervalo, espera_cierre, termina)
        hechas.add(e.name)
        informes.append(inf)
        imprimir(inf)
    return informes


def imprimir(inf: dict) -> None:
    ep = inf["estancamiento_provisional"]
    print(f"   {inf['ejecucion']}: {len(inf['alertas'])} alertas; estancamiento provisional "
          + (f"en la iteración {ep['iteracion']}" if ep else "nunca")
          + f"; definitivo={inf['final']['veredicto']['estancamiento']} ({inf['final']['terminacion']['tipo']}); "
            f"coincide con OH-DUO-02: {inf['coincide_con_oh_duo_02']}", flush=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--oh-duo-02", type=Path, default=next((x for x in (AQUI.parent / "oh-duo-02", Path.home() / "oh-duo-02")
                                                           if (x / "estancamiento.py").is_file()), AQUI.parent / "oh-duo-02"))
    p.add_argument("--umbrales", type=Path)
    p.add_argument("--salida", type=Path)
    sub = p.add_subparsers(dest="modo", required=True)
    r = sub.add_parser("reproducir")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--ejecucion", type=Path)
    g.add_argument("--corrida", type=Path)
    r.add_argument("--ritmo", type=float, default=0.0, help="segundos entre eventos (0: lo más rápido posible)")
    v = sub.add_parser("vigilar")
    v.add_argument("--corrida", type=Path, required=True)
    v.add_argument("--intervalo", type=float, default=0.5)
    v.add_argument("--maximo", type=int, help="cantidad de ejecuciones a observar antes de salir")
    a = p.parse_args(argv)
    E = cargar_detector(a.oh_duo_02)
    u = E.cargar_umbrales(a.umbrales)
    origen = (a.corrida or a.ejecucion).resolve()
    salida = (a.salida or AQUI / "informes" / origen.name).resolve()
    if salida == origen or origen in salida.parents:
        raise SystemExit("la salida no puede estar dentro de lo observado")
    if a.modo == "reproducir":
        ejecs = sorted(d for d in (origen / "ejecuciones").iterdir() if d.is_dir()) if a.corrida else [origen]
        for e in ejecs:
            imprimir(reproducir(e, salida / e.name if a.corrida else salida, E, u, a.ritmo))
    else:
        try:
            vigilar_corrida(origen, salida, E, u, a.intervalo, maximo=a.maximo)
        except KeyboardInterrupt:
            print("== observación interrumpida por el operador")
    print(f"Informes: {salida}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
