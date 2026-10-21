"""Ejecutar un instrumento: revisión estática → sandbox con canal de consultas → verificación → huella → registro.

El instrumento corre aislado (Bubblewrap, seccomp, cgroup, sin red ni credenciales); sus consultas pasan por el canal
del anfitrión y el intermediario de solo lectura. El resultado queda registrado con su receta exacta (versión y
parámetros), las consultas que hizo, la cobertura declarada frente a la real, la huella de los datos y su costo.
"""
from __future__ import annotations

import time

from . import estatico
from .almacen import Almacen
from .canal import Canal, Presupuesto
from .verificacion import verify


def observar_esquema(broker, mission_id: str | None = None) -> dict:
    """Estructura real (information_schema) leída con el intermediario; no modifica hechos ni memoria."""
    from ..explorer.mapstore import SCHEMA_QUERIES, build_schema

    results = {}
    for key, sql in SCHEMA_QUERIES.items():
        o = broker.run(sql, mission_id=mission_id, step=0, origin="sistema", tool="esquema",
                       question=f"¿Qué {key.replace('_', ' ')} existen?", rationale="estructura para instrumentos")
        if o.status != "ok":
            raise RuntimeError(f"No se pudo leer la estructura ({key}): {o.error}")
        results[key] = o
    return build_schema(results)


def compact_schema(schema: dict) -> dict:
    return {t: {"filas_estimadas": v.get("filas_estimadas"),
                "columnas": {c["nombre"]: str(c.get("tipo", "")) for c in v.get("columnas", [])}}
            for t, v in (schema.get("tablas") or {}).items()}


def _lugar(raw) -> dict | None:
    """El lugar del error lo informa el proceso NO confiable: se acota y se tipa antes de guardarlo."""
    if not isinstance(raw, dict):
        return None
    try:
        linea = int(raw.get("linea"))
    except (TypeError, ValueError):
        linea = None
    return {"archivo": str(raw.get("archivo") or "")[:80], "linea": linea, "funcion": str(raw.get("funcion") or "")[:80],
            "codigo": str(raw.get("codigo") or "")[:200]}


def _excepciones(raw) -> list[dict]:
    out = []
    for e in raw if isinstance(raw, list) else []:
        if isinstance(e, dict):
            try:
                veces = max(1, int(e.get("veces") or 1))
            except (TypeError, ValueError):
                veces = 1
            out.append({"tipo": str(e.get("tipo") or "")[:80], "mensaje": str(e.get("mensaje") or "")[:200],
                        "veces": veces, **(_lugar(e) or {})})
        if len(out) >= 8:
            break
    return out


def ejecutar(*, executor, broker, almacen: Almacen, cfg, instrument_id: int, params: dict, schema: dict,
             mission_id: str | None = None, budget: Presupuesto | None = None, cancel=None) -> dict:
    inst = almacen.get(instrument_id)
    if inst is None:
        raise ValueError(f"instrumento inexistente: {instrument_id}")
    budget = budget or Presupuesto.from_cfg(cfg)
    libs = almacen.library_files(inst["libraries"])
    lib_modules = [k[4:-3] for k in libs]
    t0 = time.monotonic()

    def record(status, **kw):
        cost = {"reloj_s": round(time.monotonic() - t0, 3), **kw.pop("cost", {})}
        rid = almacen.record_run(instrument_id, broker.name, mission_id, params, status, cost=cost, **kw)
        return almacen.run(rid)

    problems = estatico.check(inst["code"], extra_modules=cfg.instrumentos_modulos_extra, libraries=lib_modules,
                              max_bytes=cfg.instrumentos_codigo_kb * 1024)
    for code in libs.values():
        problems += [f"biblioteca: {p}" for p in estatico.check(code, extra_modules=cfg.instrumentos_modulos_extra,
                                                                  libraries=lib_modules,
                                                                  max_bytes=cfg.instrumentos_codigo_kb * 1024)
                     if "medir" not in p]
    if problems:
        return record("rechazado", error="; ".join(problems))
    canal = Canal(broker, budget, mission_id=mission_id, ref=inst["modulo"], cancel=cancel)
    wall = budget.segundos + budget.consultas * (cfg.explorer_query_timeout_s + 3) + 15
    res = executor.run_instrument({"instrumento.py": inst["code"], **libs},
                                  {"parametros": params or {}, "esquema": compact_schema(schema),
                                   "presupuesto": budget.as_dict()}, canal, wall, cancel)
    accounting = canal.accounting()
    tables = Almacen.tables_in([q["sql"] for q in accounting["consultas"]], set((schema.get("tablas") or {})))
    fingerprint = Almacen.fingerprint(broker, tables, mission_id) if tables else {}
    if res.status != "ok":
        return record({"cancelled": "cancelado", "timeout": "timeout", "blocked": "bloqueado"}.get(res.status, "error"),
                      error=res.reason, accounting=accounting, fingerprint=fingerprint)
    h = (res.result or {}).get("instrumento") or {}
    cost = {"calculo_s": h.get("calculo_s"), "espera_mariadb_s": h.get("espera_s"), "pedidos": h.get("rpc")}
    final = h.get("instrumento") or {}
    if h.get("timeout"):
        return record("timeout", error=f"el cálculo del instrumento superó {budget.segundos} s (sin contar la espera de "
                                       "MariaDB)", accounting=accounting, fingerprint=fingerprint, cost=cost)
    if h.get("error") or not final:
        return record("error", error=h.get("error") or "sin resultado", accounting=accounting,
                      fingerprint=fingerprint, cost=cost)
    excepciones = _excepciones(final.get("excepciones"))
    if not final.get("ok"):
        falla = {"tipo": str(final.get("exc") or "")[:80], "mensaje": str(final.get("msg") or "")[:500],
                 "lugar": _lugar(final.get("lugar"))}
        return record("error", error=f"{final.get('exc')}: {final.get('msg')}\n{final.get('tb') or ''}".strip(),
                      accounting=accounting, fingerprint=fingerprint, cost=cost,
                      verdict={"falla": falla, "excepciones_capturadas": excepciones})
    output = final.get("valor")
    verdict = verify(output, accounting, canal.agg_numbers, budget.resultado_bytes, excepciones)
    kept = output if verdict["bytes"] <= budget.resultado_bytes else {"_descartada": "salida demasiado grande"}
    return record("ok" if verdict["contrato"] else "contrato_incumplido", output=kept, accounting=accounting,
                  verdict=verdict, fingerprint=fingerprint, cost=cost,
                  error=None if verdict["contrato"] else "; ".join(verdict["problemas"]))
