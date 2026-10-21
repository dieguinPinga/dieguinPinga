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
from . import verificaciones as V


TIMEOUT_S = 900   # Enmienda 2: igual para todos los brazos; 300 s cortaría las reescrituras completas
MARCA_ENSAYO = "ENSAYO_TECNICO"
PRESUPUESTO_ENSAYO = {"llamadas": 2, "segundos": 1800.0, "deteccion": 1}


def preparar_salida(salida: Path, ensayo: bool) -> Path:
    """Una carpeta es de ensayo técnico o de la corrida preregistrada, nunca de las dos: la marca lo impide al
    reanudar."""
    salida = Path(salida)
    if salida.exists() and any(p.name != "mariadb" for p in salida.iterdir()):
        es_ensayo = (salida / MARCA_ENSAYO).exists()
        if es_ensayo != ensayo:
            raise ValueError(f"{salida} es de {'un ensayo técnico' if es_ensayo else 'la corrida preregistrada'}: "
                             "no se mezclan")
    salida.mkdir(parents=True, exist_ok=True, mode=0o700)
    if ensayo:
        (salida / MARCA_ENSAYO).write_text("Ensayo técnico: EXCLUIDO del análisis preregistrado (PILOTO.md).\n")
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


def _deteccion(ent, crear_modelo, defecto, pres: Presupuesto, log) -> dict:
    """Etapa compartida: el modelo escribe verificadores SIN ver el código; se validan mecánicamente."""
    alm = ent.almacen(f"deteccion_{defecto.id}_{int(time.time() * 1000)}")
    modelo = BR.ModeloConPresupuesto(crear_modelo(), pres.deteccion, 10 ** 9)
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


def correr(cfg, *, ent, crear_modelo, defectos=("D1", "D2"), repeticiones: int = 2, brazos=BR.BRAZOS,
           salida: Path, presupuesto: Presupuesto | None = None, ensayo: bool = False, log=print) -> dict:
    salida = preparar_salida(salida, ensayo)
    pres = presupuesto or Presupuesto()
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
    for d_id in defectos:
        defecto = B.DEFECTOS[d_id]
        lineas = B.lineas_de(defecto.codigo, defecto.sembrada)
        for rep in range(1, repeticiones + 1):
            clave_det = f"{d_id}_r{rep}"
            pendientes = [b for b in brazos if (d_id, rep, b) not in hechos]
            if not pendientes:
                continue
            if clave_det not in detecciones and any(b != "A" for b in pendientes):
                log(f"== {d_id} — repetición {rep}: detección compartida")
                detecciones[clave_det] = _deteccion(ent, crear_modelo, defecto, pres, log)
                deteccion_arch.write_text(json.dumps(detecciones, ensure_ascii=False, indent=1))
            det = detecciones.get(clave_det, {"verificadores": []})
            for brazo in pendientes:
                log(f"== {d_id} — repetición {rep} — brazo {brazo}")
                alm = ent.almacen(f"ep_{d_id}_r{rep}_{brazo.replace('*', 'x')}")
                modelo = BR.ModeloConPresupuesto(crear_modelo(), pres.llamadas, pres.segundos)
                ctx = Ctx(ent=ent, almacen=alm, modelo=modelo)
                t0 = time.monotonic()
                if brazo == "A":
                    r = BR.correr_a(ctx, defecto)
                else:
                    vers = [{"id": v["id"], "instrumento_id": alm.save(f"verif_{v['id']}", v["codigo"],
                                                                       {"proposito": "verificador"},
                                                                       author="modelo")["id"]}
                            for v in det["verificadores"] if v["valido"]]
                    r = BR.correr_suite(ctx, brazo, defecto, vers, lineas if brazo in BR.ORACULO else None)
                ent.liberar(alm)
                juez = _juez(ent, ref, r["vigente"]["codigo"], defecto.codigo)
                fugas = auditar(modelo.llamadas, brazo, defecto)
                ep = {"defecto": d_id, "repeticion": rep, "brazo": brazo, "juez": juez, "fugas": fugas,
                      "valido": not fugas, "motivo_fin": r["motivo_fin"], "pasos": r["pasos"],
                      "llamadas": _llamadas(modelo), "segundos_modelo": round(modelo.segundos, 1),
                      "segundos_reloj": round(time.monotonic() - t0, 1),
                      "presupuesto_respetado": len(modelo.llamadas) <= pres.llamadas,
                      "codigo_final": r["vigente"]["codigo"]}
                with (salida / "prompts.jsonl").open("a") as f:
                    for i, c in enumerate(modelo.llamadas, 1):
                        f.write(json.dumps({"defecto": d_id, "repeticion": rep, "brazo": brazo, "n": i,
                                            "tipo": c.get("tipo"), "partes": c["partes"],
                                            "respuesta": c.get("respuesta")}, ensure_ascii=False) + "\n")
                with archivo.open("a") as f:
                    f.write(json.dumps(ep, ensure_ascii=False) + "\n")
                hechos[(d_id, rep, brazo)] = ep
                log(f"   {brazo}: {'CORREGIDO' if juez['corregido'] else 'no corregido'} "
                    f"({r['motivo_fin']}, {len(modelo.llamadas)} llamadas)" + (f" FUGAS: {fugas}" if fugas else ""))
    episodios = [hechos[k] for k in sorted(hechos)]
    res = {"version": VERSION, "preregistro": "PILOTO.md", "ensayo": ensayo, "presupuesto": pres.as_dict(), "defectos": list(defectos),
           "repeticiones": repeticiones, "brazos": list(brazos), "detecciones": detecciones, "episodios": episodios}
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
    integridad["apto_para_experimento_completo"] = integridad["fugas"] == 0 and \
        integridad["presupuesto_excedido"] == 0 and integridad["episodios_invalidos"] == 0
    return {"contra_la_hipotesis": contra, "comparaciones": comparaciones, "senales": senales,
            "localizacion_primer_paso": {"aciertos": sum(loc), "total": len(loc)}, "integridad": integridad}
