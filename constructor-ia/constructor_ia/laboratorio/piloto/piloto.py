"""Orquestador del piloto (PILOTO.md): detección compartida → brazos → juez → auditoría → lectura preregistrada.

Reanudable: cada episodio terminado se agrega a episodios.jsonl; al reanudar se saltan los ya hechos (una corrida
nocturna interrumpida no se repite desde cero).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from ... import VERSION, codeextract
from . import banco as B
from . import brazos as BR
from . import v2 as V2
from . import verificaciones as V


TIMEOUT_S = 900   # Enmienda 2: igual para todos los brazos; 300 s cortaría las reescrituras completas
MARCA_ENSAYO = "ENSAYO_TECNICO"
MARCA_PROTOCOLO = "PROTOCOLO"
PROTOCOLOS = ("v1", "v2")
MAX_INTERRUPCIONES_SEGUIDAS = 2
PRESUPUESTO_ENSAYO = {"llamadas": 2, "segundos": 1800.0, "deteccion": 1}


def preparar_salida(salida: Path, ensayo: bool, protocolo: str = "v1") -> Path:
    """Una carpeta es de ensayo técnico o de la corrida preregistrada, y de UN protocolo: las marcas impiden
    mezclarlos al reanudar (una carpeta sin marca de protocolo es v1, como el ensayo original)."""
    if protocolo not in PROTOCOLOS:
        raise ValueError(f"protocolo desconocido: {protocolo}")
    salida = Path(salida)
    if salida.exists() and any(p.name != "mariadb" for p in salida.iterdir()):
        es_ensayo = (salida / MARCA_ENSAYO).exists()
        if es_ensayo != ensayo:
            raise ValueError(f"{salida} es de {'un ensayo técnico' if es_ensayo else 'la corrida preregistrada'}: "
                             "no se mezclan")
        marca = salida / MARCA_PROTOCOLO
        previo = marca.read_text().strip() if marca.exists() else "v1"
        if previo != protocolo:
            raise ValueError(f"{salida} es del protocolo {previo}: no se reanuda con {protocolo}")
    salida.mkdir(parents=True, exist_ok=True, mode=0o700)
    if ensayo:
        (salida / MARCA_ENSAYO).write_text("Ensayo técnico: EXCLUIDO del análisis preregistrado (PILOTO.md).\n")
    (salida / MARCA_PROTOCOLO).write_text(protocolo + "\n")
    return salida


@dataclass
class Presupuesto:
    llamadas: int = 6
    segundos: float = 1200.0
    deteccion: int = 2

    def as_dict(self):
        return {"llamadas_por_episodio": self.llamadas, "segundos_por_episodio": self.segundos,
                "llamadas_de_deteccion": self.deteccion}


@dataclass
class Ctx:
    ent: object
    almacen: object
    modelo: object
    extra: dict = field(default_factory=dict)


VERIF_PROMPT = """Sos un VERIFICADOR independiente. Un instrumento de medición (no vas a ver su código) responde:
PREGUNTA: {pregunta}
Parámetros de la ejecución: {params}
Su salida real fue:
```json
{salida}
```
Estructura de la base (filas estimadas): {esquema}

Escribí un verificador: un programa con def medir(db, p), donde p["parametros"] son los parámetros de arriba y
p["salida"] es esa salida. Tiene que volver a medir con SQL PROPIO, de forma independiente, alguna afirmación concreta
de la salida (no la repitas: comprobala), y devolver:
{{"resultado": {{"ok": true o false, "campos": [nombres de las claves de la salida que están mal],
  "contraejemplos": [hasta 3: qué dice la salida y qué mediste vos]}},
 "cobertura": {{"filas_analizadas": n}}, "limitaciones": ["..."]}}
{extra}Respondé solo con un bloque ```python```."""


def _deteccion(ent, crear_modelo, defecto, pres: Presupuesto, log, envolver=None) -> dict:
    """Etapa compartida: el modelo escribe verificadores SIN ver el código; se validan mecánicamente."""
    alm = ent.almacen(f"deteccion_{defecto.id}_{int(time.time() * 1000)}")
    modelo = envolver(crear_modelo(), pres.deteccion, 10 ** 9) if envolver else \
        BR.ModeloConPresupuesto(crear_modelo(), pres.deteccion, 10 ** 9)
    if hasattr(modelo, "guardia"):
        modelo.guardia.antes_de_sandbox()
    inst = alm.save("ventanas", defecto.codigo, B.MANIFIESTO, author="banco")
    ev = V.evaluar(ent, alm, inst["id"], B.PARAMS_VISIBLES, [])
    salida = json.dumps(ev["salida"], ensure_ascii=False)[:2500] if ev["salida"] is not None else "null"
    from ...instrumentos.medicion import compact_schema
    esquema = json.dumps(compact_schema(ent.schema), ensure_ascii=False)[:1500]
    verificadores, extra = [], ""
    for n in range(1, pres.deteccion + 1):
        prompt = VERIF_PROMPT.format(pregunta=B.PREGUNTA, params=B.PARAMS_VISIBLES, salida=salida, esquema=esquema,
                                     extra=extra)
        try:
            text, _ = modelo.con_partes([("coordinador", prompt)]).pedir("verificador", prompt, json_out=False)
        except BR.Agotado:
            break
        code, _ = codeextract.extract_code(text or "")
        if code is None:
            extra = "Tu respuesta anterior no tenía un bloque ```python```.\n"
            continue
        vid = alm.save(f"verif_{n}", code, {"proposito": "verificador"}, author="modelo")["id"]
        if hasattr(modelo, "guardia"):
            modelo.guardia.antes_de_sandbox()
        prueba = V.evaluar(ent, alm, inst["id"], B.PARAMS_VISIBLES, [{"id": f"V{n}", "instrumento_id": vid}])
        valido = f"V{n}" in prueba["checks"]
        verificadores.append({"id": f"V{n}", "codigo": code, "valido": valido,
                              "detecta": valido and not prueba["checks"][f"V{n}"]["ok"],
                              "campos": prueba["checks"].get(f"V{n}", {}).get("campos", []),
                              "error": None if valido else prueba["invalidos"][0]})
        extra = ("Ya existe un verificador que comprueba otra cosa; escribí uno que compruebe una afirmación "
                 "DISTINTA de la salida.\n") if valido else \
            (f"Tu verificador anterior falló al ejecutarse: {prueba['invalidos'][0]['error'][:400]}\n"
             "Corregilo.\n")
    ent.liberar(alm)
    sistema = {k: c for k, c in ev["checks"].items()}
    log(f"  detección {defecto.id}: sistema falla {[k for k, c in sistema.items() if not c['ok']]}; verificadores "
        + ", ".join(f"{v['id']}={'inválido' if not v['valido'] else ('detecta' if v['detecta'] else 'no detecta')}"
                    for v in verificadores))
    return {"verificadores": verificadores, "sistema": {k: {"ok": c["ok"], "campos": c["campos"]}
                                                       for k, c in sistema.items()},
            "llamadas": _llamadas(modelo)}


def _llamadas(modelo) -> list[dict]:
    return [{k: v for k, v in c.items() if k not in ("prompt", "respuesta", "partes")} for c in modelo.llamadas]


def _juez(ent, ref: dict, codigo: str, codigo_defectuoso: str) -> dict:
    """Fuera de los brazos: ejecuta la versión final y la defectuosa con los parámetros visibles y de reserva."""
    alm = ent.almacen(f"juez_{int(time.time() * 1000)}")
    out = {}
    for nombre, cod in (("final", codigo), ("defectuoso", codigo_defectuoso)):
        iid = alm.save(nombre, cod, B.MANIFIESTO, author="juez")["id"]
        out[nombre] = {}
        for k, p in (("visible", B.PARAMS_VISIBLES), ("reserva", B.PARAMS_RESERVA)):
            run = V._run(ent, alm, iid, p)
            out[nombre][k] = B.comparar(run.get("output"), ref[k]) if run["status"] == "ok" else \
                {"igual": False, "campos": ["(error)"]}
    ent.liberar(alm)
    corregido = out["final"]["visible"]["igual"] and out["final"]["reserva"]["igual"]
    antes = set(out["defectuoso"]["visible"]["campos"]) | set(out["defectuoso"]["reserva"]["campos"])
    despues = set(out["final"]["visible"]["campos"]) | set(out["final"]["reserva"]["campos"])
    return {"corregido": corregido, "solo_visible": out["final"]["visible"]["igual"] and not corregido,
            "campos_mal_antes": sorted(antes), "campos_mal_despues": sorted(despues),
            "regresiones": sorted(despues - antes)}


def auditar(llamadas: list[dict], brazo: str, defecto: B.Defecto) -> list[str]:
    """Fugas: líneas de la referencia que no están en el defectuoso (la corrección) en partes del coordinador, la
    evidencia o el oráculo; y la marca del oráculo en los brazos sin oráculo."""
    ref_lines = {ln.strip() for ln in B.REFERENCIA.splitlines() if ln.strip()}
    def_lines = {ln.strip() for ln in defecto.codigo.splitlines() if ln.strip()}
    correccion = sorted(ref_lines - def_lines)
    fugas = []
    for i, c in enumerate(llamadas):
        for origen, texto in c.get("partes") or []:
            if origen in ("coordinador", "oraculo") and any(x in texto for x in correccion):
                fugas.append(f"llamada {i + 1}: la corrección aparece en una parte «{origen}»")
            if brazo not in BR.ORACULO and BR.MARCA_ORACULO in texto:
                fugas.append(f"llamada {i + 1}: marca del oráculo en el brazo {brazo}")
    return fugas


def _falsas_alarmas(ent, detecciones: dict, ref: dict) -> dict:
    """DESPUÉS de los episodios (nunca alimenta a un brazo): cada verificador válido se corre sobre la salida de la
    REFERENCIA. Si la declara mal, es una falsa alarma. Mide la calidad de la detección del modelo."""
    alm = ent.almacen(f"juez_falsas_alarmas_{int(time.time() * 1000)}")
    out = {}
    for clave, d in detecciones.items():
        for v in d.get("verificadores", []):
            if not v.get("valido"):
                continue
            vid = alm.save(f"fa_{clave}_{v['id']}", v["codigo"], {"proposito": "verificador"}, author="modelo")["id"]
            run = V._run(ent, alm, vid, {"parametros": B.PARAMS_VISIBLES, "salida": ref["visible"]})
            res = (run.get("output") or {}).get("resultado") if run["status"] == "ok" else None
            ok = res.get("ok") if isinstance(res, dict) else None
            out.setdefault(clave, {})[v["id"]] = {
                "falsa_alarma": ok is False, "no_evaluable": ok is None,
                "campos": (res or {}).get("campos") if isinstance(res, dict) else None}
    ent.liberar(alm)
    return out


def correr(cfg, *, ent, crear_modelo, defectos=("D1", "D2"), repeticiones: int = 2, brazos=BR.BRAZOS,
           salida: Path, presupuesto: Presupuesto | None = None, ensayo: bool = False, protocolo: str = "v1",
           guardia=None, log=print) -> dict:
    salida = preparar_salida(salida, ensayo, protocolo)
    pres = presupuesto or Presupuesto()
    v2 = protocolo == "v2"
    guardia = guardia or V2.Guardia(minimo_mb=0)

    def crear():
        m = crear_modelo()
        if v2:   # v1 dejaba «{resultado_kb}» sin completar en el prompt de sistema de B–D y la detección
            from ..protocolo import SYSTEM
            m.system = SYSTEM.replace("{resultado_kb}", str(cfg.instrumentos_resultado_kb))
        return m

    def envolver(m, llamadas, segundos):
        return V2.ModeloV2(m, llamadas, segundos, guardia) if v2 else BR.ModeloConPresupuesto(m, llamadas, segundos)

    archivo = salida / "episodios.jsonl"
    hechos = {}
    if archivo.exists():
        for ln in archivo.read_text().splitlines():
            e = json.loads(ln)
            hechos[(e["defecto"], e["repeticion"], e["brazo"])] = e
    deteccion_arch = salida / "deteccion.json"
    detecciones = json.loads(deteccion_arch.read_text()) if deteccion_arch.exists() else {}
    juez_alm = ent.almacen("juez_referencia")
    ref_id = juez_alm.save("referencia", B.REFERENCIA, B.MANIFIESTO, author="banco")["id"]
    ref = {k: V._run(ent, juez_alm, ref_id, p).get("output") for k, p in
           (("visible", B.PARAMS_VISIBLES), ("reserva", B.PARAMS_RESERVA))}
    interrupciones, seguidas, estado = [], 0, "completo"

    def interrumpido(tipo, d_id, rep, brazo, e, modelo=None):
        reg = {"ts": time.time(), "tipo": tipo, "defecto": d_id, "repeticion": rep, "brazo": brazo,
               "detalle": str(e)[:500], "llamadas_hechas": len(modelo.llamadas) if modelo else None,
               "memoria": guardia.registro[-3:]}
        interrupciones.append(reg)
        with (salida / "interrupciones.jsonl").open("a") as f:
            f.write(json.dumps(reg, ensure_ascii=False) + "\n")
        log(f"   INTERRUPCIÓN ({tipo}): {str(e)[:200]}")

    for d_id in defectos:
        if estado != "completo":
            break
        defecto = B.DEFECTOS[d_id]
        lineas = B.lineas_de(defecto.codigo, defecto.sembrada)
        for rep in range(1, repeticiones + 1):
            if estado != "completo":
                break
            clave_det = f"{d_id}_r{rep}"
            pendientes = [b for b in brazos if (d_id, rep, b) not in hechos]
            if not pendientes:
                continue
            if clave_det not in detecciones and any(b != "A" for b in pendientes):
                log(f"== {d_id} — repetición {rep}: detección compartida")
                try:
                    detecciones[clave_det] = _deteccion(ent, crear, defecto, pres, log, envolver)
                except (V2.EntornoInterrumpido, V2.MemoriaInsuficiente, V2.ContextoInsuficiente) as e:
                    interrumpido(type(e).__name__, d_id, rep, "deteccion", e)
                    estado = "detenido_por_entorno"
                    break
                deteccion_arch.write_text(json.dumps(detecciones, ensure_ascii=False, indent=1))
            det = detecciones.get(clave_det, {"verificadores": []})
            for brazo in pendientes:
                if estado != "completo":
                    break
                intento_ep = 0
                while True:
                    intento_ep += 1
                    log(f"== {d_id} — repetición {rep} — brazo {brazo}"
                        + (" (reintento tras una interrupción del entorno)" if intento_ep > 1 else ""))
                    alm = ent.almacen(f"ep_{d_id}_r{rep}_{brazo.replace('*', 'x')}_{intento_ep}")
                    modelo = envolver(crear(), pres.llamadas, pres.segundos)
                    ctx = Ctx(ent=ent, almacen=alm, modelo=modelo)
                    t0 = time.monotonic()
                    r, motivo_invalido = None, None
                    try:
                        if brazo == "A":
                            r = BR.correr_a(ctx, defecto)
                        else:
                            validos = [v for v in det["verificadores"] if v["valido"]]
                            vers = [{"id": v["id"], "instrumento_id": alm.save(
                                f"verif_{v['id']}", v["codigo"], {"proposito": "verificador"}, author="modelo")["id"]}
                                for v in validos]
                            if v2:
                                r = V2.correr_suite(ctx, brazo, defecto, vers,
                                                    lineas if brazo in BR.ORACULO else None,
                                                    {v["id"]: v["codigo"] for v in validos})
                            else:
                                r = BR.correr_suite(ctx, brazo, defecto, vers, lineas if brazo in BR.ORACULO else None)
                    except V2.ContextoInsuficiente as e:
                        motivo_invalido = f"coordinador: {e}"
                    except (V2.EntornoInterrumpido, V2.MemoriaInsuficiente) as e:
                        ent.liberar(alm)
                        interrumpido(type(e).__name__, d_id, rep, brazo, e, modelo)
                        seguidas += 1
                        if isinstance(e, V2.MemoriaInsuficiente) or seguidas >= MAX_INTERRUPCIONES_SEGUIDAS \
                                or intento_ep >= 2:
                            estado = "detenido_por_entorno"
                            break
                        continue
                    seguidas = 0
                    ent.liberar(alm)
                    codigo_final = r["vigente"]["codigo"] if r else defecto.codigo
                    guardia.antes_de_sandbox()
                    juez = _juez(ent, ref, codigo_final, defecto.codigo)
                    fugas = auditar(modelo.llamadas, brazo, defecto)
                    ep = {"defecto": d_id, "repeticion": rep, "brazo": brazo, "protocolo": protocolo, "juez": juez,
                          "fugas": fugas, "valido": not fugas and motivo_invalido is None,
                          "motivo_invalido": motivo_invalido,
                          "motivo_fin": r["motivo_fin"] if r else "invalido", "pasos": r["pasos"] if r else [],
                          "disputas": (r or {}).get("disputas", []), "impugnados": (r or {}).get("impugnados", []),
                          "fallas_finales": (r or {}).get("fallas_finales"),
                          "llamadas": _llamadas(modelo), "segundos_modelo": round(modelo.segundos, 1),
                          "segundos_reloj": round(time.monotonic() - t0, 1),
                          "presupuesto_respetado": len(modelo.llamadas) <= pres.llamadas,
                          "reintento_por_entorno": intento_ep > 1, "codigo_final": codigo_final}
                    with (salida / "prompts.jsonl").open("a") as f:
                        for i, c in enumerate(modelo.llamadas, 1):
                            f.write(json.dumps({"defecto": d_id, "repeticion": rep, "brazo": brazo, "n": i,
                                                "tipo": c.get("tipo"), "partes": c["partes"],
                                                "respuesta": c.get("respuesta")}, ensure_ascii=False) + "\n")
                    with archivo.open("a") as f:
                        f.write(json.dumps(ep, ensure_ascii=False) + "\n")
                    hechos[(d_id, rep, brazo)] = ep
                    log(f"   {brazo}: {'CORREGIDO' if juez['corregido'] else 'no corregido'} "
                        f"({ep['motivo_fin']}, {len(modelo.llamadas)} llamadas)"
                        + (f" FUGAS: {fugas}" if fugas else "") + (f" INVÁLIDO: {motivo_invalido}"
                                                                   if motivo_invalido else ""))
                    break
    episodios = [hechos[k] for k in sorted(hechos)]
    res = {"version": VERSION, "preregistro": "PILOTO.md", "protocolo": protocolo, "ensayo": ensayo,
           "estado": estado, "presupuesto": pres.as_dict(), "defectos": list(defectos), "repeticiones": repeticiones,
           "brazos": list(brazos), "detecciones": detecciones, "episodios": episodios,
           "interrupciones": interrupciones, "guardia_memoria": guardia.registro[-200:]}
    guardia.antes_de_sandbox()
    res["falsas_alarmas"] = _falsas_alarmas(ent, detecciones, ref)
    res["lectura"] = leer(res)
    return res


# Lectura preregistrada -----------------------------------------------------------------------------------

COMPARACIONES = [("B*", "C", "¿Importa el tamaño, con la misma información?"),
                 ("B", "B*", "¿Cuánto cuesta localizar solo?"),
                 ("D", "C", "¿Cuánto cuesta localizar solo, con cambios completos?"),
                 ("B", "D", "¿Importa el tamaño, con localización automática?"),
                 ("B", "A", "¿La propuesta mejora lo actual?")]


def leer(res: dict) -> dict:
    eps = [e for e in res["episodios"] if e["valido"]]

    def corregidos(brazo, d):
        return sum(1 for e in eps if e["brazo"] == brazo and e["defecto"] == d and e["juez"]["corregido"])

    def costo(brazo, d):
        return sum(len(e["llamadas"]) for e in eps if e["brazo"] == brazo and e["defecto"] == d)

    def detectado(d):
        dets = [v for k, v in res["detecciones"].items() if k.startswith(d + "_")]
        return any(any(not c["ok"] for c in x["sistema"].values()) or any(v.get("detecta") for v in x["verificadores"])
                   for x in dets)

    comparaciones, contra = [], []
    for a, b, pregunta in COMPARACIONES:
        for d in res["defectos"]:
            if a not in res["brazos"] or b not in res["brazos"]:
                continue
            ca, cb = corregidos(a, d), corregidos(b, d)
            if not detectado(d) and "A" not in (a, b):
                cat = "no informativo"
            elif ca > cb:
                cat = "a favor"
            elif ca < cb:
                cat = "en contra"
            else:
                cat = "empate"
            fila = {"comparacion": f"{a} vs {b}", "pregunta": pregunta, "defecto": d, "corregidos": [ca, cb],
                    "llamadas": [costo(a, d), costo(b, d)], "categoria": cat}
            comparaciones.append(fila)
            if cat == "en contra":
                contra.append(f"{a} vs {b} en {d}: {a} corrigió {ca} y {b} {cb} ({pregunta})")
    for d in res["defectos"]:
        if not detectado(d):
            contra.append(f"{d}: ninguna verificación (sistema ni modelo) detectó el defecto → desafío 1 sin resolver")
    loc = []
    for e in eps:
        if e["brazo"] in ("B", "D"):
            lineas = B.lineas_de(B.DEFECTOS[e["defecto"]].codigo, B.DEFECTOS[e["defecto"]].sembrada)
            for p in e["pasos"][:1]:
                hit = any(lineas[0] <= c <= lineas[1] for c in p.get("candidatas", []))
                loc.append(hit)
                if not hit:
                    contra.append(f"{e['brazo']} {e['defecto']} r{e['repeticion']}: el localizador no puso la "
                                  "instrucción sembrada entre sus 3 candidatas (primer paso)")
    ds = list(res["defectos"])
    senales = {"R1": bool(ds) and all(corregidos("B*", d) <= corregidos("C", d) for d in ds),
               "R2": bool(ds) and all(corregidos("B", d) < corregidos("B*", d) for d in ds)}
    senales["R3"] = "D1" in res["defectos"] and not detectado("D1")
    integridad = {"fugas": sum(len(e["fugas"]) for e in res["episodios"]),
                  "episodios_invalidos": sum(1 for e in res["episodios"] if not e["valido"]),
                  "presupuesto_excedido": sum(1 for e in res["episodios"] if not e["presupuesto_respetado"]),
                  "atribucion_coordinador": sum(1 for e in eps for p in e["pasos"]
                                                if isinstance(p, dict) and p.get("atribucion") == "coordinador")}
    integridad["interrupciones_entorno"] = len(res.get("interrupciones", []))
    integridad["historial_recortado"] = [
        f"{e['defecto']} r{e['repeticion']} {e['brazo']} paso {i}" for e in res["episodios"]
        for i, p in enumerate(e["pasos"], 1) if isinstance(p, dict) and p.get("historial_incluido") is not None
        and p.get("historial_incluido") < V2.HISTORIAL_COMPLETO and i > 1]
    integridad["estado"] = res.get("estado", "completo")
    integridad["apto_para_experimento_completo"] = integridad["fugas"] == 0 and \
        integridad["presupuesto_excedido"] == 0 and integridad["episodios_invalidos"] == 0 and \
        integridad["estado"] == "completo"
    autoria: dict = {}
    for e in res["episodios"]:
        for p in e["pasos"]:
            if not isinstance(p, dict) or "candidatas" not in p:
                continue
            if p.get("contrato"):
                autoria["contrato (formato reparado)"] = autoria.get("contrato (formato reparado)", 0) + 1
            clave = p.get("atribucion") or ("sin falla" if p.get("resultado") == "aceptado" else "rechazo del umbral")
            autoria[clave] = autoria.get(clave, 0) + 1
        if e.get("motivo_invalido"):
            autoria["coordinador (episodio inválido)"] = autoria.get("coordinador (episodio inválido)", 0) + 1
    autoria["entorno (interrupciones)"] = len(res.get("interrupciones", []))
    fa = res.get("falsas_alarmas") or {}
    disputas = []
    for e in eps:
        for d in e.get("disputas") or []:
            info = (fa.get(f"{e['defecto']}_r{e['repeticion']}") or {}).get(d["verificador"]) or {}
            falsa = info.get("falsa_alarma")
            disputas.append({"episodio": f"{e['defecto']} r{e['repeticion']} {e['brazo']}", **d,
                             "falsa_alarma_segun_juez": falsa,
                             "decision_correcta": None if falsa is None else (d["decision"] == "verificador") == falsa})
            if falsa is not None and (d["decision"] == "verificador") != falsa:
                contra.append(f"{e['brazo']} {e['defecto']} r{e['repeticion']}: el modelo decidió que estaba mal el "
                              f"{d['decision']} en la disputa de {d['verificador']}, y el juez dice lo contrario")
    for clave, vs in fa.items():
        for vid, info in vs.items():
            if info.get("falsa_alarma"):
                contra.append(f"{clave} {vid}: el verificador del modelo declara mal la salida de la REFERENCIA "
                              "(falsa alarma)")
    return {"contra_la_hipotesis": contra, "comparaciones": comparaciones, "senales": senales,
            "localizacion_primer_paso": {"aciertos": sum(loc), "total": len(loc)}, "integridad": integridad,
            "autoria": autoria, "disputas": disputas, "falsas_alarmas": fa}
