"""Ensayo de la cola con trabajadores SIMULADOS (etapa 2): sin red, sin modelos y sin inferencias.

Sirve para verificar la cola en la VivoBook con una base desechable propia (nunca la oficina.db real):
- dos misiones independientes: «sueltas» (d y e, sin dependencias) y una cadena a → b → c;
- tres trabajadores simulados en hilos, que toman tareas de la cola compartida en SQLite, cada uno con un turno:
  «firme» entrega bien; «caido» toma una tarea y se desconecta sin entregar ni renovar (su arriendo vence);
  «tramposo» entrega una propuesta incorrecta la primera vez;
- un coordinador que recupera arriendos vencidos y VERIFICA cada propuesta con una regla determinista (la propuesta
  tiene que ser el texto de la carga invertido). Los trabajadores nunca verifican.

Resultado esperado: las dos misiones completas; la tarea del «caido» reasignada después del vencimiento; la
propuesta del «tramposo» rechazada y reintentada; una entrega tardía con un token viejo rechazada; y avance
simultáneo de las dos misiones (intervalos de trabajo solapados).
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from .cola import ArriendoInvalido, Cola


def _plan(sufijo: str, modelo: str, tiempo_max_s: int) -> tuple[list[dict], list[dict]]:
    def t(nombre, texto, deps=()):
        return {"nombre": nombre, "rol": "revisor", "modelo": modelo, "carga": {"texto": texto + sufijo},
                "depende_de": list(deps), "max_intentos": 3, "tiempo_max_s": tiempo_max_s}
    return [t("a", "uno"), t("b", "dos", ["a"]), t("c", "tres", ["b"])], [t("d", "cuatro"), t("e", "cinco")]


class Simulado(threading.Thread):
    def __init__(self, ruta: Path, tid: str, modelo: str, comportamiento: str, demora: float, fin: threading.Event,
                 registro: list, lock: threading.Lock):
        super().__init__(name=f"simulado-{tid}", daemon=True)
        self.ruta, self.tid, self.modelo, self.comp, self.demora = ruta, tid, modelo, comportamiento, demora
        self.fin, self.registro, self.lock = fin, registro, lock
        self.viejo = None          # (tarea, token) de la tarea abandonada por el «caido»

    def run(self):
        cola = Cola(self.ruta)          # una conexión propia por hilo, como un proceso aparte
        trampa_hecha = False
        try:
            while not self.fin.is_set():
                t = cola.tomar(self.tid, [self.modelo])
                if t is None:
                    time.sleep(0.05)
                    continue
                inicio = time.monotonic()
                cola.iniciar(t["id"], t["token"])
                if self.comp == "caido" and self.viejo is None:
                    self.viejo = (t["id"], t["token"])
                    self._anotar(t, inicio, "abandonada")
                    return                  # se desconecta: ni entrega ni latido
                time.sleep(self.demora)
                texto = t["carga"]["texto"]
                propuesta = {"eco": texto[::-1]}
                if self.comp == "tramposo" and not trampa_hecha:
                    propuesta, trampa_hecha = {"eco": texto}, True
                try:
                    cola.entregar(t["id"], t["token"], propuesta)
                    self._anotar(t, inicio, "entregada")
                except ArriendoInvalido as e:
                    self._anotar(t, inicio, f"rechazada: {e}")
        finally:
            cola.cerrar()

    def _anotar(self, t, inicio, como):
        with self.lock:
            self.registro.append({"trabajador": self.tid, "tarea": t["id"], "mision": t["mision_id"], "inicio": inicio,
                                  "fin": time.monotonic(), "como": como})


def correr(carpeta: Path, *, demora: float = 0.4, tiempo_max_s: int = 1, limite_s: float = 30.0, log=print) -> dict:
    carpeta = Path(carpeta)
    carpeta.mkdir(parents=True, exist_ok=True, mode=0o700)
    ruta = carpeta / "ensayo-cola.db"
    if ruta.exists():
        raise FileExistsError(f"{ruta} ya existe: un ensayo por carpeta")
    modelo = "simulado"
    cola = Cola(ruta)
    cadena, suelta = _plan("", modelo, tiempo_max_s)
    # Orden de creación = orden de asignación: el «caido» toma d; el «tramposo», e; el «firme», a (otra misión).
    m2 = cola.crear_mision("ensayo: tareas sueltas d y e", suelta)
    m1 = cola.crear_mision("ensayo: cadena a → b → c", cadena)
    fin, lock, registro = threading.Event(), threading.Lock(), []
    hilos = [Simulado(ruta, "caido", modelo, "caido", demora, fin, registro, lock),
             Simulado(ruta, "tramposo", modelo, "tramposo", demora, fin, registro, lock),
             Simulado(ruta, "firme", modelo, "firme", demora, fin, registro, lock)]
    t0 = time.monotonic()
    for h in hilos:                      # arranque escalonado: cada uno toma la siguiente tarea libre
        h.start()
        time.sleep(0.1)
    rechazos = recuperadas = 0
    while time.monotonic() - t0 < limite_s:
        recuperadas += len(cola.recuperar())
        for m in (m1, m2):
            for t in cola.mision(m)["tareas"]:
                if t["estado"] == "propuesta":
                    ok = (t["propuesta"] or {}).get("eco") == t["carga"]["texto"][::-1]
                    cola.verificar(t["id"], ok, "eco invertido correcto" if ok else "el eco no es el texto invertido")
                    rechazos += not ok
        if all(cola.mision(m)["estado"] in ("completa", "fallida", "cancelada") for m in (m1, m2)):
            break
        time.sleep(0.05)
    fin.set()
    for h in hilos:
        h.join(5)
    tardia = None
    if hilos[0].viejo:
        try:
            cola.entregar(*hilos[0].viejo, {"eco": "tarde"})
            tardia = "ACEPTADA (error)"
        except ArriendoInvalido as e:
            tardia = f"rechazada: {e}"
    trabajo = [r for r in registro if r["como"] == "entregada"]
    solape = any(a["mision"] != b["mision"] and a["inicio"] < b["fin"] and b["inicio"] < a["fin"]
                 for a in trabajo for b in trabajo)
    out = {"base": str(ruta), "misiones": {m: cola.mision(m)["estado"] for m in (m2, m1)},
           "recuperadas": recuperadas, "propuestas_rechazadas": rechazos, "entrega_tardia": tardia,
           "solape_entre_misiones": solape, "segundos": round(time.monotonic() - t0, 2),
           "intervalos": [{k: (round(v - t0, 2) if k in ("inicio", "fin") else v) for k, v in r.items()}
                          for r in registro]}
    out["ok"] = (all(e == "completa" for e in out["misiones"].values()) and recuperadas >= 1 and rechazos >= 1
                 and (tardia or "").startswith("rechazada") and solape)
    cola.cerrar()
    log(texto(out))
    return out


def texto(r: dict) -> str:
    lineas = [f"Ensayo de la cola ({r['segundos']} s, base {r['base']}):"]
    lineas += [f"- misión {m}: {e}" for m, e in r["misiones"].items()]
    lineas += [f"- arriendos vencidos recuperados: {r['recuperadas']}",
               f"- propuestas rechazadas por el coordinador: {r['propuestas_rechazadas']}",
               f"- entrega tardía con un token viejo: {r['entrega_tardia']}",
               f"- dos misiones avanzando a la vez: {'sí' if r['solape_entre_misiones'] else 'no'}",
               f"Resultado: {'OK' if r['ok'] else 'FALLÓ'}"]
    return "\n".join(lineas)
