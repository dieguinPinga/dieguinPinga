"""Cartógrafo: descubre la estructura y mide los datos SIN modelo, siempre por el intermediario de solo lectura.

- esquema(): las 4 consultas genéricas a information_schema del Explorador (limitadas a DATABASE()); guarda una
  instantánea con huella en el mapa existente (MapStore).
- huella_estructura(tabla): columnas, tipos, nulos, claves, índices y claves foráneas de UNA tabla.
- huella_datos(tabla): filas y dos resúmenes de todas las celdas (XOR y suma de CRC32 por fila). Detecta cambios de
  datos aunque el esquema sea el mismo. En tablas más grandes que LIMITE_HUELLA no se calcula (vigencia desconocida:
  se trata como desactualizada).
- perfilar(tabla): filas exactas y, por columna, no nulos, distintos, mínimo y máximo; valores frecuentes de las
  columnas de pocos valores; una muestra de 5 filas (las primeras físicas: NO es aleatoria). Alcance: tabla completa.
- relaciones(): candidatas por claves declaradas y por convención de nombres (las hipótesis genéricas del mapa), cada
  una MEDIDA: contención (¿todo valor del hijo existe en el padre?) y unicidad del destino.
"""
from __future__ import annotations

import hashlib
import json

from ..explorer.mapstore import SCHEMA_QUERIES, MapStore, build_schema, candidate_relations, is_numeric, is_temporal
from .medir import _q

LIMITE_HUELLA = 200_000      # filas estimadas: por encima, la huella de datos no se calcula
LIMITE_FRECUENTES = 12       # columnas con hasta tantos valores distintos: se guardan sus frecuencias
TIPOS_SIN_PERFIL = ("blob", "binary", "varbinary", "geometry")


class ErrorCartografo(RuntimeError):
    pass


def huella_estructura(t: dict) -> str:
    canon = {"columnas": [(c["nombre"], c["tipo"], c["nulo"], c["clave"]) for c in t["columnas"]],
             "indices": sorted((k, v["unico"], tuple(v["columnas"])) for k, v in t["indices"].items()),
             "fk": sorted((f["columna"], f["tabla_ref"], f["columna_ref"]) for f in t["claves_foraneas"])}
    return hashlib.sha256(json.dumps(canon, sort_keys=True, default=str).encode()).hexdigest()[:16]


class Cartografo:
    def __init__(self, broker, memoria, conexion: str, mision_id: str):
        self.broker, self.mem, self.conexion, self.mid = broker, memoria, conexion, mision_id
        self.consultas = 0

    def _run(self, sql: str, herramienta: str, pregunta: str, max_rows: int | None = None):
        self.consultas += 1
        o = self.broker.run(sql, mission_id=self.mid, origin="sistema", tool=herramienta, question=pregunta,
                            rationale="cartógrafo (sin modelo)", max_rows=max_rows)
        if o.status != "ok":
            raise ErrorCartografo(f"{herramienta}: {o.status} {o.error}")
        return o

    def esquema(self) -> dict:
        res = {k: self._run(sql, "esquema", f"¿Qué {k} existen?") for k, sql in SCHEMA_QUERIES.items()}
        schema = build_schema(res)
        MapStore(self.mem.db).save_snapshot(self.conexion, self.mid, schema)
        return schema

    def huella_datos(self, tabla: str, t: dict) -> dict:
        estimadas = t.get("filas_estimadas") or 0
        if estimadas > LIMITE_HUELLA:
            return {"huella": None, "motivo": f"tabla grande (~{estimadas} filas): huella de datos no calculada"}
        celdas = ", ".join(f"IFNULL(CAST({_q(c['nombre'])} AS CHAR), '\\\\N')" for c in t["columnas"]
                           if not c["tipo"].lower().startswith(TIPOS_SIN_PERFIL))
        o = self._run(f"SELECT COUNT(*), BIT_XOR(CRC32(CONCAT_WS('#', {celdas}))), "
                      f"SUM(CRC32(CONCAT_WS('#', {celdas}))) FROM {_q(tabla)}", "huella_datos",
                      f"¿Cambiaron los datos de {tabla}?")
        filas, x, s = o.rows[0]
        h = hashlib.sha256(f"{filas}|{x}|{s}".encode()).hexdigest()[:16]
        return {"huella": h, "filas": int(filas), "consulta_id": o.query_id}

    def perfilar(self, tabla: str, t: dict) -> tuple[dict, dict, list]:
        cols = [c for c in t["columnas"] if not c["tipo"].lower().startswith(TIPOS_SIN_PERFIL)]
        exprs = ["COUNT(*)"]
        for c in cols:
            n = _q(c["nombre"])
            corto = n if (is_numeric(c["tipo"]) or is_temporal(c["tipo"])) else f"LEFT({n}, 60)"
            exprs += [f"COUNT({n})", f"COUNT(DISTINCT {n})", f"MIN({corto})", f"MAX({corto})"]
        o = self._run(f"SELECT {', '.join(exprs)} FROM {_q(tabla)}", "perfil", f"¿Cómo son las columnas de {tabla}?")
        fila = o.rows[0]
        perfil = {"filas": int(fila[0]), "alcance": "tabla_completa", "columnas": {}, "consulta_id": o.query_id}
        ids = [o.query_id]
        for i, c in enumerate(cols):
            nn, dist, mn, mx = fila[1 + 4 * i: 5 + 4 * i]
            perfil["columnas"][c["nombre"]] = {"tipo": c["tipo"], "clave": c["clave"], "no_nulos": int(nn),
                                               "distintos": int(dist), "min": mn, "max": mx}
        for c in cols:
            p = perfil["columnas"][c["nombre"]]
            if 0 < p["distintos"] <= LIMITE_FRECUENTES and c["clave"] != "PRI":
                n = _q(c["nombre"])
                f = self._run(f"SELECT {n}, COUNT(*) FROM {_q(tabla)} GROUP BY {n} ORDER BY 2 DESC, 1 LIMIT 5",
                              "frecuentes", f"¿Qué valores toma {tabla}.{c['nombre']}?")
                p["frecuentes"] = [[r[0], int(r[1])] for r in f.rows]
                ids.append(f.query_id)
        m = self._run(f"SELECT * FROM {_q(tabla)} LIMIT 5", "muestra", f"¿Cómo son algunas filas de {tabla}?")
        ids.append(m.query_id)
        muestra = {"columnas": m.columns, "filas": m.rows, "alcance": "primeras filas físicas (no aleatoria)",
                   "consulta_id": m.query_id}
        return perfil, muestra, ids

    def candidatas(self, schema: dict) -> list[dict]:
        out = []
        for name, t in schema["tablas"].items():
            for f in t["claves_foraneas"]:
                out.append({"tabla": name, "columna": f["columna"], "tabla_ref": f["tabla_ref"],
                            "columna_ref": f["columna_ref"], "origen": "declarada"})
        vistos = {(r["tabla"], r["columna"]) for r in out}
        for a, ca, b, cb, _ in candidate_relations(schema):
            if (a, ca) not in vistos:
                out.append({"tabla": a, "columna": ca, "tabla_ref": b, "columna_ref": cb, "origen": "convencion"})
        return out

    def medir_relacion(self, rel: dict, schema: dict, perfiles: dict) -> dict:
        a, ca, b, cb = rel["tabla"], rel["columna"], rel["tabla_ref"], rel["columna_ref"]
        o = self._run(f"SELECT COUNT(*) FROM {_q(a)} x WHERE x.{_q(ca)} IS NOT NULL AND NOT EXISTS "
                      f"(SELECT 1 FROM {_q(b)} y WHERE y.{_q(cb)} = x.{_q(ca)})", "relacion",
                      f"¿Todo {a}.{ca} existe en {b}.{cb}?")
        huerfanos = int(o.rows[0][0])
        tb = schema["tablas"][b]
        pk = [c["nombre"] for c in tb["columnas"] if c["clave"] == "PRI"]
        unico_idx = any(v["unico"] and v["columnas"] == [cb] for v in tb["indices"].values())
        pb = perfiles.get(b, {}).get("columnas", {}).get(cb, {})
        unico = pk == [cb] or unico_idx or (pb and pb.get("distintos") == pb.get("no_nulos") ==
                                             perfiles[b].get("filas"))
        return {**rel, "contencion": huerfanos == 0, "huerfanos": huerfanos, "unico": bool(unico),
                "consulta_id": o.query_id}
