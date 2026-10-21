"""Los cinco brazos del piloto. Ninguno recibe el juez: la referencia y la verdad viven en otro almacén.

A  — 0.6.1 sin cambios (`Sesion._ciclo`, congelado).
B  — microtarea, localizador automático (top 3).
B* — microtarea, oráculo de ubicación (la instrucción sembrada).
C  — instrumento completo, oráculo de ubicación.
D  — instrumento completo, localizador automático.

B, B*, C y D comparten: la suite (sistema + verificadores del modelo), el umbral de aceptación, el presupuesto y el
corte por falta de progreso. Solo difieren en el ALCANCE del cambio y en el ORIGEN de la ubicación.

Cada prompt se arma con PARTES etiquetadas por origen (coordinador, oráculo, código, evidencia) para auditar fugas.
"""
from __future__ import annotations

import re
import time

from ... import codeextract
from .. import tareas as T
from ..protocolo import Sesion
from . import banco as B
from . import localizador as L
from . import parches as P
from . import verificaciones as V

MARCA_ORACULO = "[ubicación verificada]"
BRAZOS = ("A", "B", "B*", "C", "D")
MICRO = {"B", "B*"}
ORACULO = {"B*", "C"}
CORTE_SIN_PROGRESO = 3


class Agotado(Exception):
    pass


class ModeloConPresupuesto:
    """Envuelve un protocolo.Modelo: corta por llamadas o por segundos de modelo y guarda cada prompt completo con sus
    partes etiquetadas y la respuesta cruda (para auditar fugas y reutilizar respuestas sin inferir de nuevo)."""

    def __init__(self, modelo, max_llamadas: int, max_segundos: float):
        self.inner, self.max_llamadas, self.max_segundos = modelo, max_llamadas, max_segundos
        self.nombre = modelo.nombre
        self.llamadas: list[dict] = []
        self.segundos = 0.0
        self._partes = None

    @property
    def system(self):
        return self.inner.system

    @system.setter
    def system(self, v):
        self.inner.system = v

    def con_partes(self, partes):
        self._partes = partes
        return self

    def pedir(self, tipo, prompt, *, json_out, cancel=None):
        if len(self.llamadas) >= self.max_llamadas or self.segundos >= self.max_segundos:
            raise Agotado(f"presupuesto agotado ({len(self.llamadas)} llamadas, {self.segundos:.0f} s)")
        t0 = time.monotonic()
        text, call = self.inner.pedir(tipo, prompt, json_out=json_out, cancel=cancel)
        self.segundos += (call.get("reloj_ms") or (time.monotonic() - t0) * 1000) / 1000
        partes, self._partes = self._partes or [("sin_etiquetar", prompt)], None
        self.llamadas.append({**call, "prompt": prompt, "partes": partes, "respuesta": text})
        return text, call


def _contexto(salida) -> list[tuple[str, str]]:
    return [("coordinador", f"Pregunta que responde el instrumento: {B.PREGUNTA}\nParámetros: {B.PARAMS_VISIBLES}\n"),
            ("evidencia", "Salida actual: " + str(salida)[:1500] + "\n")]


def _evidencia(ev) -> list[tuple[str, str]]:
    from ...instrumentos import diagnostico as D
    return [("evidencia", "Verificaciones que fallan:\n" + V.texto_fallas(ev) + "\n"),
            ("evidencia", "Diagnóstico de la ejecución:\n" + D.texto(D.diagnosticar(ev["run"])) + "\n")]


def _texto(partes) -> str:
    return "\n".join(t for _, t in partes)


RE_CAND = re.compile(r"CANDIDATO:\s*(\d+)", re.I)
RE_ACC = re.compile(r"ACCI[OÓ]N:\s*(reemplazar|eliminar)", re.I)


def _micro(ctx, codigo, cands, ev, oraculo: bool):
    lista = []
    for i, c in enumerate(cands, 1):
        u = c["unidad"]
        tipo = " (compuesta: reemplazás solo su encabezado)" if u.compuesta else ""
        motivo = f"{MARCA_ORACULO} el defecto está en esta instrucción" if oraculo else \
            f"sospechosa: {c['motivo']} — puede no ser la causa"
        lista.append(f"{i}) línea {u.lineno}{tipo} — {motivo}\n{P.mostrar(codigo, u)}")
    partes = _contexto(ev["salida"]) + _evidencia(ev) + [
        ("oraculo" if oraculo else "coordinador", "Instrucciones candidatas (se muestran con 2 líneas de contexto; "
                                                  "«>>» marca la editable):\n" + "\n\n".join(lista) + "\n"),
        ("coordinador", "Corregí UNA instrucción. Respondé EXACTAMENTE:\nCANDIDATO: <número>\n"
                        "ACCION: reemplazar | eliminar\n```python\n<solo la instrucción nueva>\n```\n"
                        "(si ACCION es eliminar, no incluyas código)")]
    text, _ = ctx.modelo.con_partes(partes).pedir("micro", _texto(partes), json_out=False)
    m, a = RE_CAND.search(text or ""), RE_ACC.search(text or "")
    if not m or not a or not (1 <= int(m.group(1)) <= len(cands)):
        return None, "modelo", "respuesta sin CANDIDATO/ACCION válidos"
    u = cands[int(m.group(1)) - 1]["unidad"]
    frag = ""
    bloque = re.search(r"```[a-zA-Z0-9]*\s*\n(.*?)```", text, re.S)
    if a.group(1).lower() == "reemplazar":
        frag = bloque.group(1) if bloque else text[a.end():]
    ens = P.aplicar(codigo, u, a.group(1).lower(), frag)
    return ens.codigo, ens.atribucion, ens.detalle


def _completo(ctx, codigo, cands, ev, oraculo: bool):
    if oraculo:
        u = cands[0]["unidad"]
        loc = [("oraculo", f"{MARCA_ORACULO} El defecto está en la línea {u.lineno}:\n{P.mostrar(codigo, u, 0)}\n")]
    else:
        loc = [("coordinador", "Instrucciones sospechosas (localizadas automáticamente, pueden no ser la causa):\n"
                + "\n".join(f"- línea {c['unidad'].lineno}: {c['motivo']}" for c in cands) + "\n")]
    partes = _contexto(ev["salida"]) + [("codigo", f"Instrumento actual:\n```python\n{codigo}```\n")] + \
        _evidencia(ev) + loc + [("coordinador", "Devolvé el instrumento COMPLETO corregido, en un solo bloque "
                                                "```python```, sin texto adicional.")]
    text, _ = ctx.modelo.con_partes(partes).pedir("completo", _texto(partes), json_out=False)
    nuevo, nota = codeextract.extract_code(text or "")
    if nuevo is None:
        return None, "modelo", nota
    return nuevo, None, nota or "ok"


def correr_suite(ctx, brazo: str, defecto: B.Defecto, verificadores: list[dict], oraculo_lineas) -> dict:
    """Brazos B, B*, C y D. `oraculo_lineas` solo se usa en B* y C."""
    alm, codigo = ctx.almacen, defecto.codigo
    inst = alm.save(f"ventanas_{brazo.replace('*', 'x')}", codigo, B.MANIFIESTO, author="banco")
    ev = V.evaluar(ctx.ent, alm, inst["id"], B.PARAMS_VISIBLES, verificadores)
    pasos, firmas, motivo_fin = [], [], "sin_fallas"
    vigente = {"id": inst["id"], "codigo": codigo}
    while V.fallas(ev):
        if brazo in ORACULO:
            u = P.unidad_en(vigente["codigo"], oraculo_lineas[0]) if oraculo_lineas else None
            cands = [{"unidad": u, "motivo": "oráculo"}] if u else []
        else:
            cands = L.localizar(vigente["codigo"], V.fallas(ev), k=3)
        if not cands:
            motivo_fin = "sin_candidatas"
            break
        paso = {"candidatas": [c["unidad"].lineno for c in cands], "fallas_antes": sorted(
            f["id"] for f in V.fallas(ev))}
        try:
            nuevo, atrib, detalle = (_micro if brazo in MICRO else _completo)(ctx, vigente["codigo"], cands, ev,
                                                                               brazo in ORACULO)
        except Agotado:
            motivo_fin = "presupuesto"
            break
        paso.update({"atribucion": atrib, "detalle": detalle})
        if nuevo is None:
            paso["resultado"] = "ensamblado_fallido"
        else:
            nid = alm.save(f"ventanas_{brazo.replace('*', 'x')}", nuevo, B.MANIFIESTO,
                           author=f"modelo:{ctx.modelo.nombre}", parent_id=vigente["id"])["id"]
            ev2 = V.evaluar(ctx.ent, alm, nid, B.PARAMS_VISIBLES, verificadores)
            ok, razon = V.umbral(ev, ev2)
            paso.update({"version": nid, "aceptado": ok, "razon": razon, "fallas_despues": sorted(
                f["id"] for f in V.fallas(ev2))})
            paso["resultado"] = "aceptado" if ok else "rechazado"
            if ok:
                vigente, ev = {"id": nid, "codigo": nuevo}, ev2
        pasos.append(paso)
        firmas.append(V.firma(ev))
        if len(firmas) >= CORTE_SIN_PROGRESO and len(set(firmas[-CORTE_SIN_PROGRESO:])) == 1 and \
                not any(p.get("aceptado") for p in pasos[-CORTE_SIN_PROGRESO:]):
            motivo_fin = "sin_progreso"
            break
    return {"vigente": vigente, "pasos": pasos, "motivo_fin": motivo_fin,
            "fallas_finales": sorted(f["id"] for f in V.fallas(ev)), "invalidos": ev["invalidos"]}


def correr_a(ctx, defecto: B.Defecto) -> dict:
    """Brazo A: 0.6.1 tal cual. Sin suite compartida ni umbral; termina cuando el modelo dice «sí», se agota el
    presupuesto o corta el mismo error."""
    tarea = T.Tarea("P", "ventanas (piloto)", B.PREGUNTA, B.MANIFIESTO["salida"])
    ses = Sesion(cfg=ctx.ent.cfg, modelo=ctx.modelo, executor=ctx.ent.executor, broker=ctx.almacen.broker,
                 almacen=ctx.almacen, server=None, info={}, schema=ctx.ent.schema, max_correcciones=99,
                 log=lambda *_: None)
    inst = ctx.almacen.save("ventanas_A", defecto.codigo, B.MANIFIESTO, author="banco")
    reg = {"tarea": "P", "llamadas": [], "versiones": [], "evaluaciones": [], "correcciones": 0}
    motivo = "termino"
    try:
        ses._ciclo(tarea, reg, ctx.almacen.get(inst["id"]), defecto.codigo, 0)
    except Agotado:
        motivo = "presupuesto"
    ultima = reg["versiones"][-1]["instrumento_id"] if reg["versiones"] else inst["id"]
    final = ctx.almacen.get(ultima)
    return {"vigente": {"id": final["id"], "codigo": final["code"]}, "pasos": reg.get("intentos", []),
            "motivo_fin": motivo, "evaluaciones": reg["evaluaciones"]}
