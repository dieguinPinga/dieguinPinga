"""OH-DUO-04: coordinador de estrategias, en SIMULACIÓN (decide y registra; no envía nada al agente).

Consume las alertas de OH-DUO-03 (alertas.jsonl) en el orden en que se produjeron y decide qué mensaje le
habría mandado al MISMO programador, sin cambiar de modelo ni darle la solución:

  advertencia        una acción prohibida (A3 editar un archivo protegido, A5 pruebas que pasan solo por eso).
                     Una por episodio; no pide cambiar de estrategia.
  nueva_estrategia   un bucle improductivo CONFIRMADO (no ante la primera señal): A2 sola, o al menos
                     `min_reglas_de_bucle` reglas distintas entre A1, A2, A4 y A6 activas a la vez.
                     Una por episodio; el episodio se cierra cuando hay progreso (OH-DUO-03 retira A6 o todas
                     las alertas de bucle) y después hay que esperar `enfriamiento_iteraciones`.

Los mensajes salen de plantillas fijas (coordinador.json). Lo único variable son conteos y los nombres de
pruebas que el propio agente vio fallar en su terminal. No lee pruebas ocultas, referencias ni el código del
banco; no modifica las alertas ni los registros de OH-DUO-01/02/03.

Uso:
  python3 coordinador.py simular --alertas ~/oh-duo-03/informes/<corrida>/<ejecución>     (una ejecución)
  python3 coordinador.py simular --informes ~/oh-duo-03/informes/<corrida>                 (todas)
  python3 coordinador.py vigilar --informes ~/oh-duo-03/informes/<corrida>     (en vivo, junto a OH-DUO-03)
Solo biblioteca estándar.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

VERSION = "0.1.0"
AQUI = Path(__file__).resolve().parent
PROHIBIDAS = {"A3_edicion_de_archivo_protegido", "A5_pruebas_aprueban_solo_tras_editar_lo_protegido"}
BUCLE = {"A1_repeticion_equivalente_sin_cambio", "A2_fallos_iguales_sin_cambio_del_codigo",
         "A4_ediciones_rechazadas_o_sin_efecto", "A6_iteraciones_finales_sin_progreso"}
PROGRESO = "A6_iteraciones_finales_sin_progreso"     # si OH-DUO-03 la retira, el código productivo cambió


def ahora() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def cargar_config(p: Path | None) -> dict:
    base = json.loads((AQUI / "coordinador.json").read_text(encoding="utf-8"))
    if p:
        extra = json.loads(p.read_text(encoding="utf-8"))
        if desconocidas := set(extra) - set(base):
            raise SystemExit(f"opciones desconocidas: {sorted(desconocidas)}")
        base.update(extra)
    return base


def pruebas_de(alerta: dict) -> list[str]:
    """Nombres de las pruebas que fallaban, tal como los mostró la terminal del agente (clave de A2)."""
    if alerta.get("regla") != "A2_fallos_iguales_sin_cambio_del_codigo":
        return []
    partes = alerta["clave"].split(" :: ")
    return [p for p in partes[1].split("|") if p] if len(partes) >= 3 else []


class Coordinador:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.activas: dict[str, dict] = {}            # clave → alerta de OH-DUO-03
        self.decisiones: list[dict] = []
        self.episodio_bucle: dict | None = None       # abierto desde la intervención hasta que haya progreso
        self.episodio_prohibido: dict | None = None
        self.ultima_intervencion_iter: int | None = None
        self.iteracion = 0
        self.cerrado = None

    # ---- entrada
    def procesar(self, linea: dict) -> list[dict]:
        tipo, nuevas = linea.get("tipo"), []
        self.iteracion = max(self.iteracion, linea.get("iteracion_de_deteccion") or linea.get("iteracion") or 0)
        if tipo == "alerta_nueva":
            self.activas[linea["clave"]] = linea
            nuevas += self._al_aparecer(linea)
        elif tipo == "alerta_actualizada" and linea["clave"] in self.activas:
            self.activas[linea["clave"]]["veces"] = linea.get("veces")
        elif tipo == "alerta_retirada":
            retirada = self.activas.pop(linea["clave"], None)
            nuevas += self._al_retirarse(retirada, linea)
        elif tipo == "cierre":
            self.cerrado = linea
        self.decisiones += nuevas
        return nuevas

    # ---- reglas de decisión
    def _reglas_activas(self, familia: set[str]) -> set[str]:
        return {a["regla"] for a in self.activas.values() if a["regla"] in familia}

    def _bucle_confirmado(self) -> tuple[bool, str]:
        activas = self._reglas_activas(BUCLE)
        for combinacion in self.cfg["confirmar_con"]:
            if set(combinacion) <= activas:
                return True, f"combinación confirmatoria {sorted(combinacion)}"
        if len(activas) >= self.cfg["min_reglas_de_bucle"]:
            return True, f"{len(activas)} reglas de bucle distintas activas a la vez"
        return False, ""

    def _al_aparecer(self, alerta: dict) -> list[dict]:
        out = []
        if alerta["regla"] in PROHIBIDAS:
            if self.episodio_prohibido is None and self.cfg["advertir_acciones_prohibidas"]:
                self.episodio_prohibido = {"desde_iteracion": self.iteracion, "alerta": alerta["clave"]}
                out.append(self._decision("advertencia", alerta, "acción prohibida", self._mensaje_advertencia(alerta)))
            else:
                out.append(self._omision(alerta, "advertencia del mismo episodio ya registrada"))
            return out
        confirmado, motivo = self._bucle_confirmado()
        if not confirmado:
            out.append(self._omision(alerta, "señal de bucle todavía sin confirmar"))
        elif self.episodio_bucle is not None:
            out.append(self._omision(alerta, f"episodio ya intervenido en la iteración {self.episodio_bucle['iteracion']}"))
        elif len([d for d in self.decisiones if d["tipo"] == "nueva_estrategia"]) >= self.cfg["max_intervenciones"]:
            out.append(self._omision(alerta, f"límite de {self.cfg['max_intervenciones']} intervenciones alcanzado"))
        elif (self.ultima_intervencion_iter is not None
              and self.iteracion - self.ultima_intervencion_iter < self.cfg["enfriamiento_iteraciones"]):
            out.append(self._omision(alerta, f"enfriamiento: última intervención en la iteración {self.ultima_intervencion_iter}"))
        else:
            d = self._decision("nueva_estrategia", alerta, motivo, self._mensaje_estrategia())
            self.episodio_bucle = {"iteracion": self.iteracion, "claves": sorted(self.activas)}
            self.ultima_intervencion_iter = self.iteracion
            out.append(d)
        return out

    def _al_retirarse(self, alerta: dict | None, linea: dict) -> list[dict]:
        if self.episodio_bucle is None or alerta is None:
            return []
        if alerta["regla"] == PROGRESO or not self._reglas_activas(BUCLE):
            cierre = {"tipo": "episodio_cerrado", "momento": ahora(), "momento_alerta": linea.get("momento"),
                      "iteracion": linea.get("iteracion"), "evento": linea.get("evento"),
                      "motivo": "progreso: el código productivo volvió a cambiar" if alerta["regla"] == PROGRESO
                      else "ya no hay alertas de bucle activas", "intervencion_en_iteracion": self.episodio_bucle["iteracion"]}
            self.episodio_bucle = None
            return [cierre]
        return []

    # ---- mensajes (solo plantillas + datos que el agente ya vio)
    def _mensaje_estrategia(self) -> dict:
        reglas = self._reglas_activas(BUCLE | PROHIBIDAS)
        idioma = self.cfg["idioma"]
        partes = []
        if "A2_fallos_iguales_sin_cambio_del_codigo" in reglas or "A1_repeticion_equivalente_sin_cambio" in reglas:
            partes.append("repeticion")
        if "A6_iteraciones_finales_sin_progreso" in reglas:
            partes.append("sin_progreso")
        if "A4_ediciones_rechazadas_o_sin_efecto" in reglas:
            partes.append("ediciones_sin_efecto")
        if reglas & PROHIBIDAS:
            partes.append("pruebas_modificadas")
        pruebas = sorted({p for a in self.activas.values() for p in pruebas_de(a)})

        def armar(lang):
            t = self.cfg["plantillas"][lang]
            frases = [t[p] for p in partes] + ([t["pruebas_fallando"].format(pruebas=", ".join(pruebas))] if pruebas else [])
            return " ".join(frases + [t["pedido"]])
        return {"texto": armar(idioma), "traduccion_es": armar("es") if idioma != "es" else None, "partes": partes,
                "datos_variables": {"pruebas": pruebas}}

    def _mensaje_advertencia(self, alerta: dict) -> dict:
        t = self.cfg["plantillas"][self.cfg["idioma"]]
        return {"texto": t["advertencia"], "traduccion_es": self.cfg["plantillas"]["es"]["advertencia"]
                if self.cfg["idioma"] != "es" else None, "partes": ["advertencia"], "datos_variables": {}}

    # ---- registro
    def _decision(self, tipo: str, alerta: dict, motivo: str, mensaje: dict) -> dict:
        return {"tipo": tipo, "enviado": False, "modo": "simulacion", "momento": ahora(),
                "momento_alerta": alerta.get("momento"), "iteracion": self.iteracion,
                "evento": alerta.get("evento_de_deteccion"), "disparada_por": alerta["clave"], "motivo": motivo,
                "alertas_activas": sorted(self.activas), "mensaje": mensaje["texto"],
                "mensaje_sha256": hashlib.sha256(mensaje["texto"].encode()).hexdigest(),
                "traduccion_es": mensaje["traduccion_es"], "partes_del_mensaje": mensaje["partes"],
                "datos_variables": mensaje["datos_variables"]}

    def _omision(self, alerta: dict, motivo: str) -> dict:
        return {"tipo": "sin_intervencion", "momento": ahora(), "momento_alerta": alerta.get("momento"),
                "iteracion": self.iteracion, "evento": alerta.get("evento_de_deteccion"),
                "disparada_por": alerta["clave"], "motivo": motivo}


# --------------------------------------------------------------------------------------------- E/S

def leer_lineas(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def resumen(nombre: str, c: Coordinador, supervision: dict | None) -> dict:
    final = (supervision or {}).get("final") or {}
    estr = [d for d in c.decisiones if d["tipo"] == "nueva_estrategia"]
    adv = [d for d in c.decisiones if d["tipo"] == "advertencia"]
    iters = final.get("iteraciones")
    return {"coordinador": VERSION, "ejecucion": nombre, "modo": "simulacion", "intervenciones_enviadas": 0,
            "advertencias": len(adv), "nuevas_estrategias": len(estr),
            "primera_advertencia_iteracion": adv[0]["iteracion"] if adv else None,
            "primera_estrategia_iteracion": estr[0]["iteracion"] if estr else None,
            "iteraciones_finales": iters,
            "anticipacion_iteraciones": iters - estr[0]["iteracion"] if estr and iters else None,
            "terminacion": (final.get("terminacion") or {}).get("tipo"),
            "estancamiento_definitivo": (final.get("veredicto") or {}).get("estancamiento"),
            "cierre_recibido": c.cerrado is not None, "decisiones": c.decisiones}


def markdown(r: dict) -> str:
    ls = [f"# Coordinación (simulación): {r['ejecucion']}", "",
          f"- Intervenciones enviadas: **{r['intervenciones_enviadas']}** (simulación: nunca se envía nada)",
          f"- Advertencias por acciones prohibidas: {r['advertencias']} (primera en la iteración {r['primera_advertencia_iteracion']})",
          f"- Recomendaciones de nueva estrategia: {r['nuevas_estrategias']} (primera en la iteración "
          f"{r['primera_estrategia_iteracion']}" + (f", {r['anticipacion_iteraciones']} antes del final)" if r["anticipacion_iteraciones"] else ")"),
          f"- Terminación según OH-DUO-03: `{r['terminacion']}`; estancamiento definitivo: {r['estancamiento_definitivo']}", "",
          "| Iter | Evento | Decisión | Disparada por | Motivo |", "|---|---|---|---|---|"]
    for d in r["decisiones"]:
        ls.append(f"| {d.get('iteracion')} | {d.get('evento')} | {d['tipo']} | {str(d.get('disparada_por', ''))[:60]} | {d.get('motivo')} |")
    for d in r["decisiones"]:
        if d["tipo"] in ("advertencia", "nueva_estrategia"):
            ls += ["", f"**{d['tipo']}**, iteración {d['iteracion']} (sha256 `{d['mensaje_sha256'][:12]}`):", "",
                   f"> {d['mensaje']}"]
            if d.get("traduccion_es"):
                ls += ["", f"> _(es)_ {d['traduccion_es']}"]
    return "\n".join(ls) + "\n"


def escribir(destino: Path, r: dict) -> None:
    destino.mkdir(parents=True, exist_ok=True)
    with open(destino / "decisiones.jsonl", "w", encoding="utf-8") as f:
        for d in r["decisiones"]:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    (destino / "coordinacion.json").write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    (destino / "coordinacion.md").write_text(markdown(r), encoding="utf-8")


def simular(dir_alertas: Path, destino: Path, cfg: dict) -> dict:
    """Reproduce el flujo de alertas de una ejecución, en su orden, y registra las decisiones."""
    c = Coordinador(cfg)
    for linea in leer_lineas(dir_alertas / "alertas.jsonl"):
        c.procesar(linea)
    sup = json.loads((dir_alertas / "supervision.json").read_text()) if (dir_alertas / "supervision.json").is_file() else None
    r = resumen(dir_alertas.name, c, sup)
    escribir(destino, r)
    return r


def vigilar(dir_alertas: Path, destino: Path, cfg: dict, intervalo: float = 0.5, termina=lambda: False) -> dict:
    """Sigue en vivo el alertas.jsonl que OH-DUO-03 va escribiendo, hasta su línea de cierre."""
    c, leidas, pendiente = Coordinador(cfg), 0, ""
    destino.mkdir(parents=True, exist_ok=True)
    with open(destino / "decisiones.jsonl", "w", encoding="utf-8") as salida:
        while c.cerrado is None and not termina():
            p = dir_alertas / "alertas.jsonl"
            if p.is_file():
                with open(p, encoding="utf-8") as f:
                    f.seek(leidas)
                    trozo = f.read()
                    leidas = f.tell()
                pendiente += trozo
                *completas, pendiente = pendiente.split("\n")       # una línea a medio escribir espera
                for l in completas:
                    if l.strip():
                        for d in c.procesar(json.loads(l)):
                            salida.write(json.dumps(d, ensure_ascii=False) + "\n")
                            salida.flush()
                            if d["tipo"] in ("advertencia", "nueva_estrategia"):
                                print(f"   [{d['tipo']}] iteración {d['iteracion']}: {d['mensaje']}", flush=True)
            if c.cerrado is None:
                time.sleep(intervalo)
    for _ in range(int(10 / max(intervalo, 0.01))):   # OH-DUO-03 escribe supervision.json justo después del cierre
        if (dir_alertas / "supervision.json").is_file() or termina():
            break
        time.sleep(intervalo)
    sup = json.loads((dir_alertas / "supervision.json").read_text()) if (dir_alertas / "supervision.json").is_file() else None
    r = resumen(dir_alertas.name, c, sup)
    escribir(destino, r)
    return r


def imprimir(r: dict) -> None:
    print(f"{r['ejecucion']}: advertencias={r['advertencias']} (iter {r['primera_advertencia_iteracion']}), "
          f"nuevas_estrategias={r['nuevas_estrategias']} (iter {r['primera_estrategia_iteracion']}), "
          f"enviadas={r['intervenciones_enviadas']}, terminación={r['terminacion']}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--config", type=Path, help="opciones que reemplazan las de coordinador.json")
    p.add_argument("--salida", type=Path)
    sub = p.add_subparsers(dest="modo", required=True)
    s = sub.add_parser("simular")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--alertas", type=Path, help="carpeta de una ejecución en los informes de OH-DUO-03")
    g.add_argument("--informes", type=Path, help="carpeta de una corrida en los informes de OH-DUO-03")
    v = sub.add_parser("vigilar")
    v.add_argument("--informes", type=Path, required=True)
    v.add_argument("--intervalo", type=float, default=0.5)
    v.add_argument("--maximo", type=int)
    a = p.parse_args(argv)
    cfg = cargar_config(a.config)
    origen = (getattr(a, "alertas", None) or a.informes).resolve()
    salida = (a.salida or AQUI / "informes" / origen.name).resolve()
    if salida == origen or origen in salida.parents:
        raise SystemExit("la salida no puede estar dentro de los informes de OH-DUO-03")
    if a.modo == "simular":
        dirs = [origen] if getattr(a, "alertas", None) else sorted(d for d in origen.iterdir() if (d / "alertas.jsonl").is_file())
        for d in dirs:
            imprimir(simular(d, salida if getattr(a, "alertas", None) else salida / d.name, cfg))
    else:
        hechas, n = set(), 0
        try:
            while a.maximo is None or n < a.maximo:
                nuevas = sorted(d for d in origen.glob("*") if (d / "alertas.jsonl").is_file() and d.name not in hechas
                                and not (d / "supervision.json").is_file())
                if not nuevas:
                    time.sleep(a.intervalo)
                    continue
                print(f"== coordinando (simulación) {nuevas[0].name}", flush=True)
                imprimir(vigilar(nuevas[0], salida / nuevas[0].name, cfg, a.intervalo))
                hechas.add(nuevas[0].name)
                n += 1
        except KeyboardInterrupt:
            print("== interrumpido por el operador")
    print(f"Informes: {salida}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
