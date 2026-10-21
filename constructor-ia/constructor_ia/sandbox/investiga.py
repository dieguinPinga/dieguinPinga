"""Primitivas de los instrumentos de investigación (se ejecuta DENTRO del sandbox).

Son fijas y pocas a propósito: comunicación, no mediciones. Qué medir y cómo sintetizarlo lo decide el instrumento.

    def medir(db, p):
        r = db.consultar("SELECT FLOOR(valor/5)*5 AS desde, COUNT(*) AS n FROM lecturas GROUP BY desde", max_filas=500)
        ...
        return {"resultado": {...}, "cobertura": {...}, "limitaciones": [...]}

* db.consultar(sql, max_filas)  → Resultado: columnas, tipos (tipo MariaDB de cada columna), filas, truncado, motivo,
  consulta (C#); r.valores("col") convierte a número (los DECIMAL, como AVG() o FLOOR() de un DECIMAL, llegan como
  texto: hay que convertirlos con float() antes de compararlos o calcular). Cada consulta pasa por el
  intermediario de solo lectura del anfitrión (no hay credenciales ni red en el sandbox). Conviene agregar en SQL
  (GROUP BY, COUNT, SUM…): así se estudian millones de registros transfiriendo pocas filas.
* db.estimar(sql)              → {"filas_estimadas": n, "plan": [...]} con EXPLAIN, sin ejecutar la consulta.
* db.esquema()                 → tablas y columnas observadas (sin consultar la base).
* db.presupuesto()             → lo que queda: consultas, filas y bytes.
* p                            → los parámetros de esta ejecución (los mismos del manifiesto, con sus valores).

Si se agota un presupuesto, consultar() lanza PresupuestoAgotado con el detalle: el instrumento puede devolver un
resultado parcial DECLARADO como tal (un tope de recursos no es una muestra representativa).
Solo biblioteca estándar.
"""
import json

_transport = None  # lo instala el proceso de ejecución: envía un pedido y devuelve la respuesta
_schema = {}
_budget = {}
_seq = [0]


class PresupuestoAgotado(Exception):
    """Se pidió más de lo que el presupuesto operativo de esta ejecución permite."""


class ConsultaFallida(Exception):
    """El intermediario rechazó la consulta o MariaDB devolvió un error."""


class Resultado:
    def __init__(self, columnas, filas, truncado, motivo, consulta, tipos=None):
        self.columnas, self.filas, self.truncado, self.motivo, self.consulta = columnas, filas, truncado, motivo, consulta
        self.tipos = list(tipos or [])

    def __iter__(self):
        return iter(self.filas)

    def __len__(self):
        return len(self.filas)

    def dicts(self):
        return [dict(zip(self.columnas, f)) for f in self.filas]

    def valores(self, columna, convertir=float):
        """Valores de una columna convertidos (los DECIMAL de MariaDB, p. ej. AVG(), llegan como texto); los nulos y lo
        que no se puede convertir se omiten."""
        i = self.columnas.index(columna)
        out = []
        for f in self.filas:
            try:
                if f[i] is not None:
                    out.append(convertir(f[i]))
            except (TypeError, ValueError):
                pass
        return out

    def __repr__(self):
        return f"Resultado({self.consulta}, {len(self.filas)} filas{', TRUNCADO' if self.truncado else ''})"


def _pedir(op, **kw):
    if _transport is None:
        raise RuntimeError("canal no disponible")
    _seq[0] += 1
    reply = _transport(dict(op=op, id=_seq[0], **kw))
    if reply.get("budget"):
        _budget.update(reply["budget"])
    if not reply.get("ok"):
        if reply.get("error") == "presupuesto":
            raise PresupuestoAgotado(reply.get("detalle") or "presupuesto agotado")
        raise ConsultaFallida(reply.get("detalle") or reply.get("error") or "consulta fallida")
    return reply


def consultar(sql, max_filas=10000):
    r = _pedir("consultar", sql=str(sql), max_filas=int(max_filas))
    return Resultado(r.get("columnas") or [], r.get("filas") or [], bool(r.get("truncado")), r.get("motivo") or "",
                     r.get("consulta"), r.get("tipos"))


def estimar(sql):
    r = _pedir("estimar", sql=str(sql))
    return {"filas_estimadas": r.get("filas_estimadas"), "plan": r.get("plan") or []}


def esquema():
    return json.loads(json.dumps(_schema))


def presupuesto():
    return dict(_budget)
