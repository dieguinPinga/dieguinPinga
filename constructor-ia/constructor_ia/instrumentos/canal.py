"""Canal del anfitrión: atiende los pedidos de consulta de un instrumento aislado.

Cada pedido llega FIRMADO por el arnés confiable del sandbox (el instrumento no puede hablar con nadie más). Acá se
aplican los presupuestos OPERATIVOS de la ejecución y cada consulta pasa por el intermediario MariaDB de solo lectura
(cuenta con permisos mínimos, sqlguard, límites de sesión, registro en explore_queries).

Un presupuesto es un límite de recursos, no una decisión metodológica: cuando no alcanza, el canal NO recorta en
silencio. Lo registra como necesidad (qué recurso, cuánto se pidió, cuánto quedaba y para qué consulta), avisa al
instrumento (truncado y motivo, o PresupuestoAgotado) y la verificación marca el resultado como parcial.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

AGG_RE = re.compile(r"\b(count|sum|avg|min|max|stddev|std|variance|var_pop|var_samp|group\s+by|distinct)\b", re.I)
MAX_NUMBERS = 4000  # cifras de resultados agregados que se conservan para contrastar la cobertura declarada


@dataclass
class Presupuesto:
    consultas: int
    filas: int
    bytes: int
    segundos: int
    resultado_bytes: int

    @classmethod
    def from_cfg(cls, cfg, **over) -> "Presupuesto":
        p = cls(consultas=cfg.instrumentos_consultas, filas=cfg.instrumentos_filas,
                bytes=cfg.instrumentos_bytes_mb * 1024 * 1024, segundos=cfg.instrumentos_segundos,
                resultado_bytes=cfg.instrumentos_resultado_kb * 1024)
        for k, v in over.items():
            if v is not None:
                setattr(p, k, int(v))
        return p

    def as_dict(self) -> dict:
        return asdict(self)


def _jsonable(v):
    if v is None or isinstance(v, (int, float, str, bool)):
        return v
    return str(v)


class Canal:
    def __init__(self, broker, budget: Presupuesto, *, mission_id: str | None, ref: str, cancel=None):
        self.broker, self.budget, self.mission_id, self.ref, self.cancel = broker, budget, mission_id, ref, cancel
        self.queries: list[dict] = []
        self.needs: list[dict] = []
        self.used = {"consultas": 0, "estimaciones": 0, "filas": 0, "bytes": 0}
        self.agg_numbers: set = set()

    def remaining(self) -> dict:
        b = self.budget
        return {"consultas": b.consultas - self.used["consultas"], "filas": b.filas - self.used["filas"],
                "bytes": b.bytes - self.used["bytes"]}

    def _refuse(self, rid, recurso, pedido, disponible, sql) -> dict:
        self.needs.append({"recurso": recurso, "pedido": pedido, "disponible": disponible, "sql": sql[:300]})
        return {"id": rid, "ok": False, "error": "presupuesto", "budget": self.remaining(),
                "detalle": f"presupuesto operativo de {recurso} agotado (se pidió {pedido}, quedaban {disponible}); "
                           "un tope de recursos no es una muestra representativa: si hace falta más, declaralo"}

    def __call__(self, req: dict) -> dict:
        rid, op, sql = req.get("id"), req.get("op"), str(req.get("sql") or "")
        if op == "estimar":
            if self.used["estimaciones"] >= self.budget.consultas:
                return self._refuse(rid, "estimaciones", self.used["estimaciones"] + 1, 0, sql)
            self.used["estimaciones"] += 1
            q = sql if sql.lstrip().lower().startswith("explain") else "EXPLAIN " + sql
            o = self.broker.run(q, mission_id=self.mission_id, origin="instrumento", tool=f"inst:{self.ref}:estimar",
                                question="estimación de costo", cancel=self.cancel, max_rows=200)
            self.queries.append({"op": "estimar", "consulta": f"C{o.query_id}" if o.query_id else None,
                                 "sql": q[:500], "estado": o.status, "filas": 0, "bytes": 0, "ms": o.ms})
            if o.status != "ok":
                return {"id": rid, "ok": False, "error": "consulta", "detalle": f"{o.status}: {o.error}"[:400]}
            est = None
            if "rows" in o.columns:
                i = o.columns.index("rows")
                vals = [int(r[i]) for r in o.rows if r[i] is not None and str(r[i]).isdigit()]
                est = max(vals) if vals else None
            plan = [dict(zip(o.columns, [_jsonable(x) for x in r])) for r in o.rows[:10]]
            return {"id": rid, "ok": True, "filas_estimadas": est, "plan": plan, "budget": self.remaining()}
        if op != "consultar":
            return {"id": rid, "ok": False, "error": "pedido", "detalle": "operación desconocida"}
        left = self.remaining()
        if left["consultas"] <= 0:
            return self._refuse(rid, "consultas", self.budget.consultas + 1, 0, sql)
        if left["filas"] <= 0 or left["bytes"] <= 0:
            res = "filas" if left["filas"] <= 0 else "bytes"
            return self._refuse(rid, res, int(req.get("max_filas") or 0) or 1, left[res], sql)
        want = max(1, int(req.get("max_filas") or 10000))
        cap = min(want, left["filas"])
        o = self.broker.run(sql, mission_id=self.mission_id, origin="instrumento", tool=f"inst:{self.ref}",
                            question="consulta de un instrumento", cancel=self.cancel, max_rows=cap,
                            max_bytes=left["bytes"])
        self.used["consultas"] += 1
        rows = [[_jsonable(x) for x in r] for r in o.rows]
        agg = bool(AGG_RE.search(sql))
        entry = {"op": "consultar", "consulta": f"C{o.query_id}" if o.query_id else None, "sql": sql[:500],
                 "estado": o.status, "filas": len(rows), "bytes": o.bytes, "ms": o.ms, "agregada": agg,
                 "truncado": bool(o.truncated)}
        self.queries.append(entry)
        if o.status != "ok":
            return {"id": rid, "ok": False, "error": "consulta", "detalle": f"{o.status}: {o.error}"[:400],
                    "budget": self.remaining()}
        self.used["filas"] += len(rows)
        self.used["bytes"] += o.bytes
        motivo = ""
        if o.truncated:
            if cap < want and len(rows) >= cap:
                motivo = (f"tope operativo de filas de esta ejecución: se pidieron hasta {want} y quedaban {cap}; "
                          "el resultado es PARCIAL")
                self.needs.append({"recurso": "filas", "pedido": want, "disponible": cap, "sql": sql[:300]})
            elif "byte" in (o.truncated_reason or "").lower():
                motivo = "tope operativo de bytes de esta ejecución: el resultado es PARCIAL"
                self.needs.append({"recurso": "bytes", "pedido": None, "disponible": left["bytes"], "sql": sql[:300]})
            else:
                motivo = f"el resultado tiene más de {want} filas (max_filas que pidió el instrumento): es PARCIAL"
            entry["motivo"] = motivo
        if agg and len(self.agg_numbers) < MAX_NUMBERS:
            for r in rows:
                for x in r:
                    if isinstance(x, (int, float)) or (isinstance(x, str) and re.fullmatch(r"-?\d+(\.\d+)?", x)):
                        self.agg_numbers.add(str(x).split(".")[0] if str(x).endswith(".0") else str(x))
        return {"id": rid, "ok": True, "columnas": list(o.columns), "filas": rows, "truncado": bool(o.truncated),
                "motivo": motivo, "consulta": entry["consulta"], "budget": self.remaining()}

    def accounting(self) -> dict:
        return {"usado": dict(self.used), "presupuesto": self.budget.as_dict(), "consultas": self.queries,
                "necesidades": self.needs}
