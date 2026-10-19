"""Mapa persistente de una conexión («el cartógrafo»), en la base de Constructor IA.

Certezas:
* comprobado  — calculado por el sistema a partir de una consulta registrada
                (information_schema, conteos, contención de valores). Lleva evidencia.
* inferencia  — interpretación del modelo; cita consultas/hechos, pero no está verificada.
* hipotesis   — candidato aún no probado (p. ej. relación sugerida por nombres de columnas).
* descartado  — hipótesis refutada por una consulta.
* sin_verificar — afirmación del modelo que NO se pudo contrastar con su evidencia (sin evidencia, evidencia
                vacía o con error, o cifras que no aparecen en el resultado). No es un descubrimiento.
Nada de esto escribe en la base explorada.

Procedencia (0.4.5): mission_id es la expedición que CREÓ el hecho y no se sobrescribe; last_mission_id es la última
que lo observó. Cada cambio queda en explore_fact_history con el estado anterior. Una afirmación del modelo repetida
nunca cambia su texto original ni le baja la certeza: se registra como confirmación o reafirmación.
"""
from __future__ import annotations

import hashlib
import json
import re
import time

from ..db import Database

CERTAINTIES = ("comprobado", "inferencia", "hipotesis", "descartado", "sin_verificar")
# Orden de respaldo para afirmaciones del modelo («descartado» lo decide solo el sistema con una medición).
RANK = {"sin_verificar": 0, "hipotesis": 1, "inferencia": 2, "comprobado": 3}
KINDS = ("estructura", "relacion", "estadistica", "advertencia", "hallazgo", "incertidumbre", "oportunidad", "cambio")
NUMERIC_RE = re.compile(r"^(tinyint|smallint|mediumint|int|integer|bigint|decimal|numeric|float|double|real|bit)", re.I)
TEMPORAL_RE = re.compile(r"^(date|datetime|timestamp|time|year)", re.I)


def is_numeric(col_type: str) -> bool:
    return bool(NUMERIC_RE.match(col_type or ""))


def is_temporal(col_type: str) -> bool:
    return bool(TEMPORAL_RE.match(col_type or ""))


# Consultas de descubrimiento de estructura: genéricas, sin nombres de tablas.
SCHEMA_QUERIES = {
    "tablas": "SELECT TABLE_NAME, TABLE_TYPE, ENGINE, TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH, CREATE_TIME, UPDATE_TIME,"
              " TABLE_COMMENT FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME",
    "columnas": "SELECT TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY, EXTRA,"
                " COLUMN_COMMENT FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE()"
                " ORDER BY TABLE_NAME, ORDINAL_POSITION",
    "indices": "SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME FROM information_schema.STATISTICS"
               " WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX",
    "claves_foraneas": "SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME, CONSTRAINT_NAME"
                       " FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA = DATABASE()"
                       " AND REFERENCED_TABLE_NAME IS NOT NULL ORDER BY TABLE_NAME, CONSTRAINT_NAME",
}


def build_schema(results: dict) -> dict:
    """results: nombre → QueryOutcome. Devuelve {tablas: {...}} con estructura y estimaciones."""
    def rows(key):
        o = results[key]
        return [dict(zip(o.columns, r)) for r in o.rows]

    tables: dict = {}
    for r in rows("tablas"):
        tables[r["TABLE_NAME"]] = {
            "tipo": "vista" if "VIEW" in str(r["TABLE_TYPE"]) else "tabla", "motor": r["ENGINE"],
            "filas_estimadas": r["TABLE_ROWS"], "bytes_datos": r["DATA_LENGTH"], "bytes_indices": r["INDEX_LENGTH"],
            "creada": r["CREATE_TIME"], "actualizada": r["UPDATE_TIME"], "comentario": (r["TABLE_COMMENT"] or "")[:200],
            "columnas": [], "indices": {}, "claves_foraneas": []}
    for r in rows("columnas"):
        t = tables.get(r["TABLE_NAME"])
        if t is not None:
            t["columnas"].append({"nombre": r["COLUMN_NAME"], "tipo": r["COLUMN_TYPE"], "nulo": r["IS_NULLABLE"] == "YES",
                                  "clave": r["COLUMN_KEY"] or "", "extra": r["EXTRA"] or "",
                                  "comentario": (r["COLUMN_COMMENT"] or "")[:200]})
    for r in rows("indices"):
        t = tables.get(r["TABLE_NAME"])
        if t is not None:
            idx = t["indices"].setdefault(r["INDEX_NAME"], {"unico": not int(r["NON_UNIQUE"]), "columnas": []})
            idx["columnas"].append(r["COLUMN_NAME"])
    for r in rows("claves_foraneas"):
        t = tables.get(r["TABLE_NAME"])
        if t is not None:
            t["claves_foraneas"].append({"columna": r["COLUMN_NAME"], "tabla_ref": r["REFERENCED_TABLE_NAME"],
                                         "columna_ref": r["REFERENCED_COLUMN_NAME"], "nombre": r["CONSTRAINT_NAME"]})
    return {"tablas": tables}


def fingerprint(schema: dict) -> str:
    """Huella de la ESTRUCTURA (ignora estimaciones de filas, tamaños y fechas de actualización)."""
    canon = {name: {"tipo": t["tipo"], "columnas": [(c["nombre"], c["tipo"], c["nulo"], c["clave"]) for c in t["columnas"]],
                    "indices": sorted((k, v["unico"], tuple(v["columnas"])) for k, v in t["indices"].items()),
                    "fk": sorted((f["columna"], f["tabla_ref"], f["columna_ref"]) for f in t["claves_foraneas"])}
             for name, t in schema["tablas"].items()}
    return hashlib.sha256(json.dumps(canon, sort_keys=True, default=str).encode()).hexdigest()


def diff_schema(old: dict | None, new: dict) -> list[str]:
    if old is None:
        return []
    changes = []
    ot, nt = old["tablas"], new["tablas"]
    for name in sorted(set(nt) - set(ot)):
        changes.append(f"tabla nueva: {name}")
    for name in sorted(set(ot) - set(nt)):
        changes.append(f"tabla eliminada: {name}")
    for name in sorted(set(ot) & set(nt)):
        oc = {c["nombre"]: c["tipo"] for c in ot[name]["columnas"]}
        nc = {c["nombre"]: c["tipo"] for c in nt[name]["columnas"]}
        for c in sorted(set(nc) - set(oc)):
            changes.append(f"columna nueva: {name}.{c} ({nc[c]})")
        for c in sorted(set(oc) - set(nc)):
            changes.append(f"columna eliminada: {name}.{c}")
        for c in sorted(set(oc) & set(nc)):
            if oc[c] != nc[c]:
                changes.append(f"tipo cambiado: {name}.{c} {oc[c]} → {nc[c]}")
        if set(ot[name]["indices"]) != set(nt[name]["indices"]):
            changes.append(f"índices cambiados en {name}")
    return changes


def candidate_relations(schema: dict) -> list[tuple[str, str, str, str, str]]:
    """Hipótesis genéricas por nombres y tipos (no conoce ningún dominio).

    Reglas: columna «X_id»/«Xid»/«id_X» que coincide con el nombre (o singular/plural simple) de otra
    tabla con clave primaria de tipo compatible; o columna con el mismo nombre que la clave primaria
    de una sola columna de otra tabla.
    """
    tables = schema["tablas"]
    pks = {}
    for name, t in tables.items():
        pk = [c for c in t["columnas"] if c["clave"] == "PRI"]
        if len(pk) == 1:
            pks[name] = pk[0]
    declared = {(name, f["columna"]) for name, t in tables.items() for f in t["claves_foraneas"]}
    out = []

    def base_type(tp):
        return re.sub(r"\(.*|\s+unsigned.*", "", tp or "", flags=re.I).lower()

    def name_variants(n):
        n = n.lower()
        return {n, n + "s", n + "es", n[:-1] if n.endswith("s") else n, n[:-2] if n.endswith("es") else n}

    for name, t in tables.items():
        for c in t["columnas"]:
            if (name, c["nombre"]) in declared or c["clave"] == "PRI":
                continue
            cname = c["nombre"].lower()
            stem = None
            for pat in (r"^(.+?)_?id$", r"^id_?(.+)$", r"^(.+?)_?(?:code|codigo|key|clave)$"):
                m = re.match(pat, cname)
                if m and m.group(1):
                    stem = m.group(1).rstrip("_")
                    break
            for other, pk in pks.items():
                if other == name or base_type(pk["tipo"]) != base_type(c["tipo"]):
                    continue
                same_pk_name = cname == pk["nombre"].lower() and cname != "id"
                by_stem = stem is not None and other.lower() in name_variants(stem)
                if same_pk_name or by_stem:
                    why = "mismo nombre que la clave primaria" if same_pk_name else "nombre de columna sugiere la tabla"
                    out.append((name, c["nombre"], other, pk["nombre"], why))
    return out


class MapStore:
    def __init__(self, db: Database):
        self.db = db

    # Instantáneas ------------------------------------------------------
    def last_snapshot(self, conn: str):
        row = self.db.one("SELECT * FROM explore_snapshots WHERE conn=? ORDER BY id DESC LIMIT 1", (conn,))
        if row is None:
            return None
        d = dict(row)
        d["schema"] = json.loads(d.pop("schema_json"))
        d["changes"] = json.loads(d.pop("changes_json") or "[]")
        return d

    def save_snapshot(self, conn: str, mission_id: str | None, schema: dict) -> tuple[dict, list[str], bool]:
        prev = self.last_snapshot(conn)
        fp = fingerprint(schema)
        changes = diff_schema(prev["schema"] if prev else None, schema)
        self.db.execute("INSERT INTO explore_snapshots(conn, mission_id, fingerprint, schema_json, changes_json,"
                        " observed_at) VALUES(?,?,?,?,?,?)",
                        (conn, mission_id, fp, json.dumps(schema, default=str), json.dumps(changes), time.time()))
        return schema, changes, prev is None

    # Hechos ------------------------------------------------------------
    def _history(self, fact_id: int, conn: str, mission_id, event: str, before: str | None, after: str | None,
                 text: str | None, evidence, data) -> None:
        self.db.execute("INSERT INTO explore_fact_history(fact_id, conn, mission_id, event, certainty_before,"
                        " certainty_after, text, evidence_json, data_json, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (fact_id, conn, mission_id, event, before, after, text,
                         json.dumps(evidence if evidence is not None else []),
                         json.dumps(data, default=str) if data is not None else None, time.time()))

    def upsert(self, conn: str, kind: str, certainty: str, subject: str, text: str, *, data=None, evidence=None,
               mission_id: str | None = None, clave: str | None = None) -> int:
        """Hechos medidos por el SISTEMA: uno por (conexión, tipo, sujeto), que se actualiza al reobservarse.

        La medición nueva reemplaza a la anterior (puede cambiar la certeza: una relación comprobada puede
        descartarse con datos nuevos), pero la expedición creadora se conserva y el estado anterior queda en el
        historial cada vez que algo cambia.
        """
        assert kind in KINDS and certainty in CERTAINTIES
        now = time.time()
        row = self.db.one("SELECT * FROM explore_facts WHERE conn=? AND kind=? AND subject=?", (conn, kind, subject))
        data_json = json.dumps(data, default=str) if data is not None else None
        ev_json = json.dumps(evidence or [])
        if row:
            changed = (row["certainty"] != certainty or row["text"] != text[:1000] or row["data_json"] != data_json
                       or row["status"] != "vigente")
            if changed:
                self._history(row["id"], conn, mission_id, "reobservado", row["certainty"], certainty, row["text"],
                              json.loads(row["evidence_json"] or "[]"),
                              json.loads(row["data_json"]) if row["data_json"] else None)
            self.db.execute("UPDATE explore_facts SET certainty=?, text=?, data_json=?, evidence_json=?, last_mission_id=?,"
                            " clave=COALESCE(?, clave), updated_at=?, status='vigente' WHERE id=?",
                            (certainty, text[:1000], data_json, ev_json, mission_id, clave, now, row["id"]))
            return row["id"]
        cur = self.db.execute(
            "INSERT INTO explore_facts(conn, mission_id, last_mission_id, kind, certainty, subject, text, data_json,"
            " evidence_json, clave, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (conn, mission_id, mission_id, kind, certainty, subject[:200], text[:1000], data_json, ev_json, clave, now, now))
        self._history(cur.lastrowid, conn, mission_id, "creado", None, certainty, text[:1000], evidence or [], data)
        return cur.lastrowid

    def record_note(self, conn: str, kind: str, text: str, *, evidence=None, mission_id=None, data=None,
                    certainty: str = "inferencia", clave: str | None = None) -> tuple[int | None, str]:
        """Afirmaciones del MODELO (hallazgos, advertencias, incertidumbres, oportunidades), sin duplicar textos.

        Devuelve (id, resultado): «creado»; «confirmado» (misma afirmación con igual respaldo: se suma la evidencia);
        «certeza_subio» (ahora está mejor respaldada); «reafirmado_sin_respaldo» (repetida con MENOS respaldo: el
        hecho conserva su certeza y la reafirmación queda anotada). Nunca cambia el texto original, la expedición
        que lo creó ni baja la certeza.
        """
        assert kind in KINDS and certainty in CERTAINTIES
        text = " ".join(str(text).split())[:600]
        if len(text) < 4:
            return None, "descartado"
        subject = kind + ":" + hashlib.sha1(text.lower().encode()).hexdigest()[:16]
        row = self.db.one("SELECT * FROM explore_facts WHERE conn=? AND kind=? AND subject=?", (conn, kind, subject))
        if row is None:
            return self.upsert(conn, kind, certainty, subject, text, data=data, evidence=evidence,
                               mission_id=mission_id, clave=clave), "creado"
        old_data = json.loads(row["data_json"]) if row["data_json"] else {}
        old_ev = json.loads(row["evidence_json"] or "[]")
        new_ev = [e for e in (evidence or []) if e not in old_ev]
        entry = {"mision": mission_id, "evidencia": evidence or [], "certeza": certainty, "ts": round(time.time(), 1),
                 **({"verificacion": data.get("verificacion"), "detalle": data.get("detalle")} if data else {})}
        old_rank, new_rank = RANK.get(row["certainty"], -1), RANK.get(certainty, -1)
        merged = dict(old_data)
        if row["certainty"] != "descartado" and new_rank > old_rank:
            outcome, final = "certeza_subio", certainty
            # El respaldo nuevo es mejor: se adoptan su verificación y su alcance; lo anterior queda en el historial.
            merged.update({k: v for k, v in (data or {}).items() if k not in ("confirmaciones", "reafirmaciones")})
        elif new_rank == old_rank:
            outcome, final = "confirmado", row["certainty"]
            merged["confirmaciones"] = (old_data.get("confirmaciones") or []) + [entry]
        else:
            outcome, final = "reafirmado_sin_respaldo", row["certainty"]
            merged["reafirmaciones"] = (old_data.get("reafirmaciones") or []) + [entry]
        self._history(row["id"], conn, mission_id, outcome, row["certainty"], final, text, evidence or [], data)
        self.db.execute("UPDATE explore_facts SET certainty=?, data_json=?, evidence_json=?, last_mission_id=?,"
                        " clave=COALESCE(clave, ?), updated_at=? WHERE id=?",
                        (final, json.dumps(merged, default=str), json.dumps((old_ev + new_ev)[:20]), mission_id, clave,
                         time.time(), row["id"]))
        return row["id"], outcome

    def add_note(self, conn: str, kind: str, text: str, *, evidence=None, mission_id=None, data=None,
                 certainty: str = "inferencia") -> int | None:
        """Compatibilidad: como record_note, devolviendo solo el id."""
        return self.record_note(conn, kind, text, evidence=evidence, mission_id=mission_id, data=data,
                                certainty=certainty)[0]

    def history(self, conn: str, fact_id: int) -> list[dict]:
        out = []
        for r in self.db.all("SELECT * FROM explore_fact_history WHERE conn=? AND fact_id=? ORDER BY id", (conn, fact_id)):
            d = dict(r)
            d["evidence"] = json.loads(d.pop("evidence_json") or "[]")
            d["data"] = json.loads(d.pop("data_json")) if d.get("data_json") else None
            out.append(d)
        return out

    def mark_obsolete(self, conn: str, subjects_prefix: str) -> None:
        self.db.execute("UPDATE explore_facts SET status='obsoleto', updated_at=? WHERE conn=? AND subject LIKE ?",
                        (time.time(), conn, subjects_prefix + "%"))

    def fact_exists(self, conn: str, fact_id: int) -> bool:
        return bool(self.db.one("SELECT 1 FROM explore_facts WHERE conn=? AND id=?", (conn, fact_id)))

    def query_exists(self, conn: str, query_id: int) -> bool:
        return bool(self.db.one("SELECT 1 FROM explore_queries WHERE conn=? AND id=?", (conn, query_id)))

    def facts(self, conn: str, kinds=None, status: str = "vigente", limit: int = 500) -> list[dict]:
        sql = "SELECT * FROM explore_facts WHERE conn=? AND status=?"
        args: list = [conn, status]
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += list(kinds)
        sql += " ORDER BY id DESC LIMIT ?"
        out = []
        for r in self.db.all(sql, (*args, limit)):
            d = dict(r)
            d["data"] = json.loads(d.pop("data_json")) if d.get("data_json") else None
            d["evidence"] = json.loads(d.pop("evidence_json") or "[]")
            out.append(d)
        return out

    # Observación de estructura ----------------------------------------
    def register_schema_facts(self, conn: str, mission_id: str, schema: dict, evidence: dict) -> None:
        tables = schema["tablas"]
        ev_t, ev_c, ev_fk = [f"C{evidence['tablas']}"], [f"C{evidence['columnas']}"], [f"C{evidence['claves_foraneas']}"]
        current = set()
        for name, t in tables.items():
            subj = f"tabla:{name}"
            current.add(subj)
            pk = [c["nombre"] for c in t["columnas"] if c["clave"] == "PRI"]
            self.upsert(conn, "estructura", "comprobado", subj,
                        f"{t['tipo']} {name}: {len(t['columnas'])} columnas"
                        + (f", clave primaria ({', '.join(pk)})" if pk else ", SIN clave primaria")
                        + f", {len(t['indices'])} índices"
                        + (f"; comentario: «{t['comentario'][:150]}»" if t.get("comentario") else ""),
                        data={"columnas": t["columnas"], "indices": t["indices"]}, evidence=ev_t + ev_c,
                        mission_id=mission_id)
            if t["filas_estimadas"] is not None:
                self.upsert(conn, "estadistica", "comprobado", f"filas_estimadas:{name}",
                            f"{name}: ~{t['filas_estimadas']} filas (estimación del motor {t['motor']}, no exacta), "
                            f"{_kb(t['bytes_datos'])} de datos", data={"filas_estimadas": t["filas_estimadas"]},
                            evidence=ev_t, mission_id=mission_id)
            if not pk and t["tipo"] == "tabla":
                self.upsert(conn, "advertencia", "comprobado", f"sin_pk:{name}",
                            f"La tabla {name} no tiene clave primaria declarada", evidence=ev_c, mission_id=mission_id)
            for f in t["claves_foraneas"]:
                self.upsert(conn, "relacion", "comprobado", f"rel:{name}.{f['columna']}->{f['tabla_ref']}.{f['columna_ref']}",
                            f"{name}.{f['columna']} → {f['tabla_ref']}.{f['columna_ref']} (clave foránea declarada)",
                            data={"origen": "declarada"}, evidence=ev_fk, mission_id=mission_id)
        for row in self.db.all("SELECT subject FROM explore_facts WHERE conn=? AND kind='estructura' AND status='vigente'",
                               (conn,)):
            if row[0] not in current:
                self.mark_obsolete(conn, row[0])
        for a, ac, b, bc, why in candidate_relations(schema):
            subj = f"rel:{a}.{ac}->{b}.{bc}"
            existing = self.db.one("SELECT certainty FROM explore_facts WHERE conn=? AND kind='relacion' AND subject=?",
                                   (conn, subj))
            if existing is None:
                self.upsert(conn, "relacion", "hipotesis", subj, f"{a}.{ac} → {b}.{bc}? ({why}; sin verificar)",
                            data={"origen": "heuristica"}, evidence=ev_c, mission_id=mission_id)


def _kb(n) -> str:
    try:
        return f"{int(n) / 1024:.0f} KiB"
    except (TypeError, ValueError):
        return "tamaño desconocido"
