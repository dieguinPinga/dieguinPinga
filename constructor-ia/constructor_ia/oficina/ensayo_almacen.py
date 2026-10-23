"""Ensayo estándar del almacén (OFICINA.md): nueve trabajadores simulados sobre los mismos 20 registros.

Sin red, sin modelos y sin otras bases: una base desechable propia. Los trabajadores solo reciben la CARGA de su
tarea (registros, consigna y formato); la verdad vive en el coordinador, que verifica cada propuesta.

Perfiles (rol calculista, salvo los dos auditores). Cada perfil con error trabaja UN turno y comete su error de
forma reproducible; «correcto» y «riguroso» siguen hasta el final:
- correcto: calcula bien;
- distraido: omite la última fila (en A da 220);
- confundido: ignora la cantidad: suma precios y cuenta filas (en B da 20);
- mezclador: devuelve unidades donde se pide importe y al revés;
- periodo_corrido: toma también el día 11 en el período (102 $ y 26 unidades);
- formato_roto: devuelve algo ilegible;
- desconectado: abandona su tarea sin avisar; vuelve después de que su arriendo venció, intenta entregar tarde (se
  rechaza) y sigue trabajando bien;
- complaciente (auditor): dice «correcto» a todo (aprueba los $220);
- riguroso (auditor): recalcula y dictamina.

Arranque escalonado y determinista: cada trabajador arranca cuando el anterior ya tomó su primera tarea, en este
orden: periodo_corrido (E), distraido (A), confundido (B), mezclador (C), complaciente (auditoría de $220),
formato_roto y desconectado (la primera tarea que vuelva a la cola), riguroso y correcto. Así cada perfil recibe una
tarea donde su error se ve, y varias misiones avanzan a la vez.
"""
from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from pathlib import Path

from . import almacen as AL
from .cola import ArriendoInvalido, Cola

ORDEN = ("periodo_corrido", "distraido", "confundido", "mezclador", "complaciente", "formato_roto", "desconectado",
         "riguroso", "correcto")
AUDITORES = ("complaciente", "riguroso")
UN_TURNO = ("periodo_corrido", "distraido", "confundido", "mezclador", "complaciente", "formato_roto")


def resolver(carga: dict, perfil: str) -> dict:
    """Lo que entrega cada perfil, calculado SOLO con la carga (nunca con la verdad)."""
    tipo, regs = carga["tipo"], [tuple(r) for r in carga["registros"]]
    if perfil == "formato_roto":
        return {"texto": "no pude leer la tabla"}
    if perfil == "distraido":
        regs = regs[:-1]
    if tipo == "periodo":
        hasta = "2026-03-11" if perfil == "periodo_corrido" else carga["hasta"]
        regs = [r for r in regs if carga["desde"] <= r[1] <= hasta]
    if perfil == "confundido":
        regs = [(i, f, p, 1, precio) for i, f, p, _, precio in regs]     # ignora la cantidad
    por = defaultdict(lambda: {"unidades": 0, "importe": 0})
    for _, _, prod, cant, precio in regs:
        por[prod]["unidades"] += cant
        por[prod]["importe"] += cant * precio
    if perfil == "mezclador":
        por = {p: {"unidades": v["importe"], "importe": v["unidades"]} for p, v in por.items()}
    imp, uni = sum(v["importe"] for v in por.values()), sum(v["unidades"] for v in por.values())
    if tipo == "importe_total":
        return {"importe": imp}
    if tipo == "unidades_total":
        return {"unidades": uni}
    if tipo == "por_producto":
        return {"por_producto": {p: dict(v) for p, v in por.items()}}
    if tipo in ("mas_unidades", "mas_importe"):
        k = "unidades" if tipo == "mas_unidades" else "importe"
        p, v = max(por.items(), key=lambda kv: kv[1][k])
        return {"producto": p, "valor": v[k]}
    if tipo == "periodo":
        return {"importe": imp, "unidades": uni}
    auditado = carga["propuesta_a_auditar"]["importe"]
    if perfil == "complaciente":
        return {"dictamen": "correcto", "valor": auditado}
    return {"dictamen": "correcto" if auditado == imp else "incorrecto", "valor": imp}


class Simulado(threading.Thread):
    def __init__(self, ruta: Path, perfil: str, demora: float, fin: threading.Event, bitacora: list,
                 lock: threading.Lock, espera_vuelta: float):
        super().__init__(name=f"almacen-{perfil}", daemon=True)
        self.ruta, self.perfil, self.demora, self.fin = ruta, perfil, demora, fin
        self.bitacora, self.lock, self.espera_vuelta = bitacora, lock, espera_vuelta
        self.tomo = threading.Event()

    def run(self):
        cola = Cola(self.ruta)
        roles = ["auditor"] if self.perfil in AUDITORES else ["calculista"]
        abandonada = None
        try:
            while not self.fin.is_set():
                t = cola.tomar(self.perfil, ["simulado"], roles=roles)
                if t is None:
                    time.sleep(0.02)
                    continue
                self.tomo.set()
                inicio = time.monotonic()
                cola.iniciar(t["id"], t["token"])
                if self.perfil == "desconectado" and abandonada is None:
                    abandonada = (t["id"], t["token"])
                    self._anotar(t, inicio, "abandonada (desconexión)")
                    time.sleep(self.espera_vuelta)          # vuelve cuando su arriendo ya venció
                    try:
                        cola.entregar(*abandonada, resolver(t["carga"], self.perfil))
                        self._anotar(t, inicio, "entrega tardía ACEPTADA (error)")
                    except ArriendoInvalido as e:
                        self._anotar(t, inicio, f"volvió; entrega tardía rechazada: {e}")
                    continue
                time.sleep(self.demora)
                propuesta = resolver(t["carga"], self.perfil)
                try:
                    cola.entregar(t["id"], t["token"], propuesta)
                    self._anotar(t, inicio, "entregada", propuesta)
                except ArriendoInvalido as e:
                    self._anotar(t, inicio, f"rechazada por la cola: {e}")
                if self.perfil in UN_TURNO:
                    return
        finally:
            self.tomo.set()
            cola.cerrar()

    def _anotar(self, t, inicio, como, propuesta=None):
        with self.lock:
            self.bitacora.append({"perfil": self.perfil, "tarea": t["nombre"], "tarea_id": t["id"],
                                  "mision": t["mision_id"], "intento": t["intentos"], "inicio": inicio,
                                  "fin": time.monotonic(), "como": como, "propuesta": propuesta})


def correr(carpeta: Path, *, demora: float = 0.3, tiempo_max_s: int = 1, limite_s: float = 60.0, log=print) -> dict:
    carpeta = Path(carpeta)
    carpeta.mkdir(parents=True, exist_ok=True, mode=0o700)
    ruta = carpeta / "almacen.db"
    if ruta.exists():
        raise FileExistsError(f"{ruta} ya existe: un ensayo por carpeta")
    verdad = AL.verdad()                       # solo la conoce el coordinador
    cola = Cola(ruta)
    ids = [cola.crear_mision(titulo, tareas) for titulo, tareas in AL.misiones(tiempo_max_s=tiempo_max_s)]
    fin, lock, bitacora = threading.Event(), threading.Lock(), []
    hilos = {p: Simulado(ruta, p, demora, fin, bitacora, lock, espera_vuelta=tiempo_max_s + 0.6) for p in ORDEN}
    verificaciones = []
    t0 = time.monotonic()

    def coordinar():
        for tid in cola.recuperar():
            verificaciones.append({"tarea_id": tid, "quien": "coordinador", "decision": "recuperada",
                                   "motivo": "arriendo vencido", "t": round(time.monotonic() - t0, 2)})
        for mid in ids:
            for t in cola.mision(mid)["tareas"]:
                if t["estado"] == "propuesta":
                    ok, motivo = AL.verificar(t["nombre"], t["propuesta"], verdad)
                    cola.verificar(t["id"], ok, motivo)
                    verificaciones.append({"tarea_id": t["id"], "tarea": t["nombre"], "quien": t["trabajador_id"],
                                           "intento": t["intentos"], "decision": "aceptada" if ok else "rechazada",
                                           "motivo": motivo, "propuesta": t["propuesta"],
                                           "t": round(time.monotonic() - t0, 2)})

    for p in ORDEN:                            # arranque escalonado: el siguiente, cuando el anterior tomó
        hilos[p].start()
        while not hilos[p].tomo.wait(0.02):
            coordinar()
            if time.monotonic() - t0 > limite_s:
                break
    while time.monotonic() - t0 < limite_s:
        coordinar()
        if all(cola.mision(m)["estado"] in ("completa", "fallida", "cancelada") for m in ids):
            break
        time.sleep(0.03)
    fin.set()
    for h in hilos.values():
        h.join(5)
    out = resumen(cola, ids, verdad, bitacora, verificaciones, t0, ruta)
    cola.cerrar()
    out["fugas"] = [str(ruta)] if verdad["_canario"].encode() in Path(ruta).read_bytes() else []
    out["ok"] = out["ok"] and not out["fugas"]
    log(texto(out))
    return out


def resumen(cola, ids, verdad, bitacora, verificaciones, t0, ruta) -> dict:
    misiones = {m: cola.mision(m) for m in ids}
    # Cero verificaciones falsas: toda tarea VERIFICADA se vuelve a comparar con los valores escritos a mano.
    falsas = [t["id"] for m in misiones.values() for t in m["tareas"]
              if t["estado"] == "verificada" and t["propuesta"] != AL.ESPERADO[t["nombre"]]]
    por_perfil = {p: {"tomadas": 0, "aceptadas": 0, "rechazadas": 0, "abandonadas": 0, "tardias_rechazadas": 0}
                  for p in ORDEN}
    for b in bitacora:
        d = por_perfil[b["perfil"]]
        d["tomadas"] += 1
        d["abandonadas"] += b["como"].startswith("abandonada")
        d["tardias_rechazadas"] += "entrega tardía rechazada" in b["como"]
    for v in verificaciones:
        if v.get("quien") in por_perfil:
            por_perfil[v["quien"]]["aceptadas" if v["decision"] == "aceptada" else "rechazadas"] += 1
    for d in por_perfil.values():
        d["tomadas"] -= d["tardias_rechazadas"]          # la vuelta no es una tarea nueva
    trabajo = [b for b in bitacora if b["como"] == "entregada"]
    solape = any(a["mision"] != b["mision"] and a["inicio"] < b["fin"] and b["inicio"] < a["fin"]
                 for a in trabajo for b in trabajo)
    historial = [e for m in ids for e in cola.eventos(mid=m)]
    out = {"base": str(ruta), "segundos": round(time.monotonic() - t0, 2),
           "misiones": {m["titulo"]: m["estado"] for m in misiones.values()},
           "resultados": {t["nombre"]: t["propuesta"] for m in misiones.values() for t in m["tareas"]},
           "verificaciones_falsas": falsas, "por_perfil": por_perfil,
           "rechazos": sum(v["decision"] == "rechazada" for v in verificaciones),
           "recuperadas": sum(v["decision"] == "recuperada" for v in verificaciones),
           "solape_entre_misiones": solape, "verificaciones": verificaciones, "bitacora": [
               {k: (round(v - t0, 2) if k in ("inicio", "fin") else v) for k, v in b.items()} for b in bitacora],
           "historial": historial,
           "verdad_coincide_con_esperado": {k: v for k, v in verdad.items() if k != "_canario"} == AL.ESPERADO}
    sin_participar = [p for p, d in por_perfil.items() if d["tomadas"] == 0]
    out["sin_participar"] = sin_participar
    out["ok"] = (all(e == "completa" for e in out["misiones"].values()) and not falsas and not sin_participar
                 and out["recuperadas"] >= 1 and por_perfil["desconectado"]["tardias_rechazadas"] == 1
                 and all(por_perfil[p]["rechazadas"] >= 1 for p in UN_TURNO)
                 and por_perfil["riguroso"]["aceptadas"] >= 1 and solape and out["verdad_coincide_con_esperado"])
    return out


def texto(r: dict) -> str:
    lineas = [f"Ensayo del almacén ({r['segundos']} s, base {r['base']}):"]
    lineas += [f"- {m}: {e}" for m, e in r["misiones"].items()]
    lineas.append("Trabajadores (tomadas / aceptadas / rechazadas / abandonadas / entregas tardías rechazadas):")
    lineas += [f"- {p}: {d['tomadas']} / {d['aceptadas']} / {d['rechazadas']} / {d['abandonadas']} / "
               f"{d['tardias_rechazadas']}" for p, d in r["por_perfil"].items()]
    lineas += [f"Rechazos del coordinador: {r['rechazos']} · arriendos recuperados: {r['recuperadas']} · "
               f"misiones a la vez: {'sí' if r['solape_entre_misiones'] else 'no'}",
               f"Verificaciones falsas: {len(r['verificaciones_falsas'])} · perfiles sin participar: "
               f"{', '.join(r['sin_participar']) or 'ninguno'} · fugas de la verdad: {len(r.get('fugas', []))}",
               "Resultados verificados: " + json.dumps(r["resultados"], ensure_ascii=False),
               f"Resultado: {'OK' if r['ok'] else 'FALLÓ'}"]
    return "\n".join(lineas)
