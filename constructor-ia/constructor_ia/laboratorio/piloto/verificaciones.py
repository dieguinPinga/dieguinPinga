"""Suite de verificaciones de un instrumento, con AUTORÍA: sistema, modelo o derivada de una falla.

Sistema (invariantes del contrato, sin dominio):
* S1_medicion   — el instrumento midió algo (estado de la medición válida o parcial; 0.6.1).
* S2_cobertura  — si declara leidas/validas/invalidas: leidas = validas + invalidas y filas_analizadas = leidas
                  (la definición de cobertura es del contrato, no de la ciencia).
* S3_determinismo — dos ejecuciones iguales dan la misma salida.

Modelo: verificadores escritos por el modelo SIN ver el código del instrumento (caja negra). Son instrumentos que
reciben p["salida"], vuelven a medir con SQL propio y devuelven {"ok", "campos", "contraejemplos"}. Un verificador que
falla técnicamente se excluye de forma mecánica (no se usa el juez para filtrarlos).
"""
from __future__ import annotations

from ...instrumentos.medicion import ejecutar
from .banco import comparar

SISTEMA = ("S1_medicion", "S2_cobertura", "S3_determinismo")


def _run(ent, almacen, inst_id, params):
    return ejecutar(executor=ent.executor, broker=almacen.broker, almacen=almacen, cfg=ent.cfg, instrument_id=inst_id,
                    params=params, schema=ent.schema)


def _lugar(run) -> dict | None:
    v = run.get("verdict") or {}
    if v.get("falla"):
        return v["falla"].get("lugar")
    exc = v.get("excepciones_capturadas") or []
    return exc[0] if exc else None


def evaluar(ent, almacen, inst_id: int, params: dict, verificadores: list[dict]) -> dict:
    """Ejecuta el instrumento y su suite. Devuelve {"run", "salida", "checks": {id: {...}}, "invalidos": [...]}."""
    run = _run(ent, almacen, inst_id, params)
    if run["status"] in ("bloqueado", "cancelado"):
        raise RuntimeError(f"entorno: ejecución {run['status']}: {run.get('error')}")
    v = run.get("verdict") or {}
    salida = run.get("output") if run["status"] == "ok" else None
    checks = {}
    ok1 = run["status"] == "ok" and v.get("medicion") in ("valida", "parcial")
    campos1 = [] if ok1 else (["filas_analizadas"] if v.get("cobertura") == "nula" else [])
    checks["S1_medicion"] = {"autor": "sistema", "ok": ok1, "campos": campos1, "lugar": None if ok1 else _lugar(run),
                             "detalle": "midió" if ok1 else f"{run['status']}/{v.get('medicion')}: "
                                                            f"{v.get('motivo_medicion') or run.get('error') or ''}"[:300]}
    cob = (salida or {}).get("cobertura") if isinstance(salida, dict) else None
    if isinstance(cob, dict) and all(isinstance(cob.get(k), (int, float)) for k in ("leidas", "validas", "invalidas")):
        malos = []
        if cob["leidas"] != cob["validas"] + cob["invalidas"]:
            malos += ["leidas", "validas", "invalidas"]
        if cob.get("filas_analizadas") != cob["leidas"]:
            malos += ["filas_analizadas"]
        checks["S2_cobertura"] = {"autor": "sistema", "ok": not malos, "campos": sorted(set(malos)), "lugar": None,
                                  "detalle": "aritmética de la cobertura" + ("" if not malos else f": {cob}")}
    if salida is not None:
        run2 = _run(ent, almacen, inst_id, params)
        dif = comparar(run2.get("output"), salida, tol=0)
        checks["S3_determinismo"] = {"autor": "sistema", "ok": dif["igual"], "campos": dif["campos"], "lugar": None,
                                     "detalle": "misma salida en dos ejecuciones" if dif["igual"] else
                                     f"dos ejecuciones difieren en {dif['campos']}"}
    invalidos = []
    for ver in verificadores:
        cid = ver["id"]
        if salida is None:
            checks[cid] = {"autor": "modelo", "ok": False, "campos": [], "lugar": None,
                           "detalle": "no hay salida que verificar"}
            continue
        vr = _run(ent, almacen, ver["instrumento_id"], {"parametros": params, "salida": salida,
                                                         **(ver.get("extra") or {})})
        res = (vr.get("output") or {}).get("resultado") if vr["status"] == "ok" else None
        if not isinstance(res, dict) or not isinstance(res.get("ok"), bool):
            invalidos.append({"id": cid, "estado": vr["status"], "error": (vr.get("error") or "")[:300]})
            continue
        campos = [str(c)[:60] for c in (res.get("campos") or []) if isinstance(c, (str, int))][:10]
        checks[cid] = {"autor": "modelo", "ok": res["ok"], "campos": campos, "lugar": None,
                       "detalle": str(res.get("contraejemplos") or "")[:600]}
    return {"run": run, "salida": salida, "checks": checks, "invalidos": invalidos}


def fallas(ev: dict) -> list[dict]:
    return [{"id": k, **c} for k, c in ev["checks"].items() if not c["ok"]]


def firma(ev: dict) -> str:
    return ";".join(f"{k}:{','.join(c['campos'])}" for k, c in sorted(ev["checks"].items()) if not c["ok"])


def umbral(antes: dict, despues: dict) -> tuple[bool, str]:
    """Se acepta si el conjunto de fallas se achica y nada que pasaba empieza a fallar."""
    fa = {k for k, c in antes["checks"].items() if not c["ok"]}
    pa = {k for k, c in antes["checks"].items() if c["ok"]}
    fd = {k for k, c in despues["checks"].items() if not c["ok"]}
    rotas = sorted(k for k in pa if k in fd or k not in despues["checks"])
    if rotas:
        return False, f"regresión: dejaron de pasar {rotas}"
    if not (fd < fa):
        return False, "no mejora: las mismas verificaciones siguen fallando" if fd == fa else \
            f"no mejora: fallan {sorted(fd)}"
    return True, f"mejora: ahora pasan {sorted(fa - fd)}"


def texto_fallas(ev: dict) -> str:
    out = []
    for f in fallas(ev):
        out.append(f"- [{f['autor']}] {f['id']}: {f['detalle']}" + (f" (campos: {', '.join(f['campos'])})"
                                                                    if f["campos"] else ""))
    return "\n".join(out) or "(ninguna)"
