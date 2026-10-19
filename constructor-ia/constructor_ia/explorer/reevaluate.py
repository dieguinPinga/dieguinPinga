"""Reevaluación de la memoria del Explorador con las reglas de 0.4.7 (explorer-reevaluar).

Nunca cambia ni borra hechos, consultas ni historial. Por defecto SOLO SIMULA: lee la base (en modo de solo lectura
desde la línea de comandos) y devuelve un plan con lo que cambiaría y por qué, identificado por un código. Aplicar
exige ese código (autorización explícita del plan visto): cada punto se registra en explore_reviews, y para los
hechos además como evento «reevaluado» de su historial, que fija su certeza efectiva. El texto y la certeza
original quedan intactos en explore_facts.

Reglas, en este orden (cada una ve el resultado de las anteriores):
1. alcance_consulta    — el alcance registrado de una consulta se recalcula con su SQL ejecutado (LIMIT interiores).
2. verificacion        — las afirmaciones se vuelven a contrastar distinguiendo identificadores SQL, alias y valores.
3. alcance_afirmacion  — una afirmación sobre la tabla completa cuya evidencia no la cubre pasa a hipótesis.
4. texto_incompleto    — una afirmación cortada por un límite de longitud anterior no se certifica.
5. aporte              — lo registrado como conocimiento nuevo que solo reformula o redondea algo ya sabido.
"""
from __future__ import annotations

import hashlib
import json
import re
import time

from . import knowledge as K
from . import validation as V
from .mapstore import EFFECTIVE_SQL

VERSION = "0.4.7"
MODEL_KINDS = ("hallazgo", "advertencia")
# Límites de longitud de versiones anteriores: 180 (hallazgos), 140 (hipótesis e incertidumbres), 600 (guardado).
OLD_LIMITS = (180, 140, 600)


def _loads(s, default=None):
    try:
        return json.loads(s) if s else default
    except ValueError:
        return default


class Reevaluator:
    def __init__(self, db, conn: str):
        self.db, self.conn = db, conn
        snap = db.one("SELECT schema_json FROM explore_snapshots WHERE conn=? ORDER BY id DESC LIMIT 1", (conn,))
        self.tables = (_loads(snap[0], {}) or {}).get("tablas", {}) if snap else {}
        self.names = {K.fold(t) for t in self.tables} | {
            K.fold(c["nombre"]) for t in self.tables.values() for c in t.get("columnas", [])}
        self.has_reviews = bool(db.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='explore_reviews'"))

    # Estado actual ------------------------------------------------------------------------------------
    def _load(self):
        self.queries = {r["id"]: dict(r) for r in self.db.all(
            "SELECT id, tool, sql_text, status, row_count, truncated, result_json, alcance_json, created_at"
            " FROM explore_queries WHERE conn=? ORDER BY id", (self.conn,))}
        self.scopes = {qid: _loads(q["alcance_json"]) for qid, q in self.queries.items()}
        self.facts = [dict(r) for r in self.db.all(
            f"SELECT f.*, {EFFECTIVE_SQL} AS certeza_efectiva FROM explore_facts f WHERE f.conn=? AND f.status='vigente'"
            " ORDER BY f.id", (self.conn,))]
        self.cert = {f["id"]: f["certeza_efectiva"] or f["certainty"] for f in self.facts}
        self.aporte = {f["id"]: (_loads(f["data_json"], {}) or {}).get("aporte") for f in self.facts}
        self.done: set = set()  # (objeto, id, regla) ya aplicados: las marcas no se repiten
        if self.has_reviews:
            for r in self.db.all("SELECT objeto, objeto_id, regla, despues_json FROM explore_reviews WHERE conn=?"
                                 " ORDER BY id", (self.conn,)):
                self.done.add((r["objeto"], r["objeto_id"], r["regla"]))
                d = _loads(r["despues_json"], {}) or {}
                if r["objeto"] == "consulta" and d.get("alcance"):
                    self.scopes[r["objeto_id"]] = d["alcance"]
                if r["objeto"] == "hecho" and d.get("aporte"):
                    self.aporte[r["objeto_id"]] = d["aporte"]

    @staticmethod
    def _origin_query(data: dict, evidence: list) -> int | None:
        return (data.get("observacion") or {}).get("consulta") or next(
            (int(e[1:]) for e in evidence if str(e).startswith("C") and str(e)[1:].isdigit()), None)

    # Regla 1 ------------------------------------------------------------------------------------------
    def _rescope(self, qid: int) -> dict | None:
        q = self.queries.get(qid)
        if q is None or q["status"] != "ok":
            return None
        old = self.scopes.get(qid) or {}
        forma = dict(old.get("forma") or K.sql_shape(q["sql_text"], self.tables))
        lims = K.sql_limits(q["sql_text"])
        forma.update(limit=lims["exterior"], limites_interiores=lims["interiores"])
        res = _loads(q["result_json"], {}) or {}
        cols, rows = res.get("columns") or [], res.get("rows") or []
        tabs = forma.get("tablas") or []
        table_rows = {}
        if len(tabs) == 1:
            if old.get("filas_tabla") is not None:
                table_rows[tabs[0]] = (old["filas_tabla"], bool(old.get("filas_tabla_exactas")))
            elif tabs[0] in self.tables:
                table_rows[tabs[0]] = (self.tables[tabs[0]].get("filas_estimadas"), False)
        analyzed = K.analyzed_rows(q["tool"], "filas" in cols, cols, rows, q["sql_text"])
        sc = K.scope(forma, q["row_count"] or 0, bool(q["truncated"]), table_rows, analyzed, sql=q["sql_text"])
        return {"forma": forma, **sc}

    def _rule_scopes(self, items: list) -> None:
        cited = set()
        for f in self.facts:
            if f["kind"] in MODEL_KINDS and self.cert[f["id"]] == "inferencia":
                q = self._origin_query(_loads(f["data_json"], {}) or {}, _loads(f["evidence_json"], []) or [])
                if q:
                    cited.add(q)
        for qid in self.queries:
            old = self.scopes.get(qid)
            if old is None and qid not in cited:
                continue
            new = self._rescope(qid)
            if new is None:
                continue
            if old and (old.get("tipo"), bool(old.get("completo"))) == (new["tipo"], new["completo"]):
                continue
            before = {k: (old or {}).get(k) for k in ("tipo", "completo", "descripcion")}
            before["limit"] = ((old or {}).get("forma") or {}).get("limit")
            items.append({"objeto": "consulta", "id": qid, "regla": "alcance_consulta",
                          "certeza_anterior": None, "certeza_nueva": None, "veredicto": "alcance_recalculado",
                          "detalle": (f"{before['tipo'] or 'sin alcance registrado'} (completo={before['completo']}) → "
                                      f"{new['tipo']} (completo={new['completo']}): {new['descripcion']}"),
                          "antes": before if old else None, "despues": {"alcance": new}, "evidencia": [f"C{qid}"]})
            self.scopes[qid] = new

    # Reglas 2-5 por hecho -----------------------------------------------------------------------------
    def _evidence(self, ids: list) -> list[dict]:
        out = []
        for e in ids:
            e = str(e)
            if not re.fullmatch(r"[CF]\d+", e):
                continue
            if e[0] == "C":
                q = self.queries.get(int(e[1:]))
                if q is None:
                    continue
                res = _loads(q["result_json"], {}) or {}
                cells, cols = V.evidence_cells(q["result_json"])
                out.append({"id": e, "tipo": "consulta", "status": q["status"], "row_count": q["row_count"],
                            "utilidad": V.usefulness(q["status"], q["row_count"] or 0, res.get("rows") or [], q["sql_text"]),
                            "texto": V.evidence_text(q["result_json"]), "celdas": cells, "columnas": cols,
                            "sql": q["sql_text"]})
            else:
                f = next((x for x in self.facts if x["id"] == int(e[1:])), None)
                if f is not None:
                    out.append({"id": e, "tipo": "hecho", "certainty": self.cert[f["id"]],
                                "texto": f"{f['text']} {f['data_json'] or ''}"})
        return out

    def _cited_scope(self, data: dict, evidence: list) -> dict | None:
        q = self._origin_query(data, evidence)
        if q:
            return self.scopes.get(q)
        for e in evidence:
            if str(e).startswith("F"):
                f = next((x for x in self.facts if x["id"] == int(str(e)[1:])), None)
                if f is None:
                    return None
                q = self._origin_query(_loads(f["data_json"], {}) or {}, _loads(f["evidence_json"], []) or [])
                sc = self.scopes.get(q) if q else None
                if not sc or not sc.get("completo"):
                    return sc
        return None

    def _key_facts(self, key: str, before_id: int, related: bool) -> list[dict]:
        tabs, _, cols = key.partition("|")
        cols = set(filter(None, cols.split(",")))
        out = []
        for f in self.facts:
            if f["id"] >= before_id or not f["clave"] or self.cert[f["id"]] not in ("inferencia", "comprobado"):
                continue
            ft, _, fc = f["clave"].partition("|")
            if related:
                if ft != tabs or f["clave"] == key or not cols & set(filter(None, fc.split(","))):
                    continue
            elif f["clave"] != key:
                continue
            d = _loads(f["data_json"], {}) or {}
            obs = d.get("observacion") or {}
            out.append({"id": f["id"], "values": set(d.get("valores") or K.claim_values(f["text"])),
                        "consulta": self._origin_query(d, _loads(f["evidence_json"], []) or []),
                        "tipo": obs.get("tipo"), "observadas": obs.get("filas"),
                        "mision": obs.get("mision") or f["mission_id"], "ts": obs.get("ts") or f["created_at"]})
        return out

    def _classify(self, f: dict, data: dict, ev: list) -> tuple:
        obs = data.get("observacion") or {}
        new = {"key": data["clave"], "values": set(data.get("valores") or K.claim_values(f["text"])),
               "consulta": self._origin_query(data, ev), "tipo": obs.get("tipo"), "observadas": obs.get("filas"),
               "mision": obs.get("mision") or f["mission_id"], "ts": obs.get("ts") or f["created_at"]}
        return K.classify(new, self._key_facts(data["clave"], f["id"], False),
                          self._key_facts(data["clave"], f["id"], True))

    def _item(self, items, f, regla, new_cert, veredicto, detalle, evidencia, despues=None):
        old = self.cert[f["id"]]
        items.append({"objeto": "hecho", "id": f["id"], "regla": regla, "certeza_anterior": old,
                      "certeza_nueva": new_cert, "veredicto": veredicto, "detalle": detalle,
                      "antes": {"certeza": old, "aporte": self.aporte.get(f["id"])},
                      "despues": despues or {"certeza": new_cert}, "evidencia": evidencia})
        self.cert[f["id"]] = new_cert

    def _rule_fact(self, items: list, f: dict) -> None:
        data = _loads(f["data_json"], {}) or {}
        ev = _loads(f["evidence_json"], []) or []
        text = f["text"]
        if f["kind"] in MODEL_KINDS:
            # 2. Verificación con identificadores, alias y valores separados (con la fecha original de la afirmación).
            if data.get("verificacion") in ("respaldada", "valores_no_encontrados", "cifras_no_encontradas") and \
                    self.cert[f["id"]] in ("inferencia", "sin_verificar"):
                lits: dict = {}
                cert, verdict, detail = V.verify_claim(text, self._evidence(ev), now=f["created_at"], names=self.names,
                                                       out=lits)
                if cert != self.cert[f["id"]]:
                    after = {"certeza": cert, "literales": lits}
                    if cert == "inferencia" and data.get("clave"):
                        # Ahora respaldada: su aporte se evalúa frente a lo registrado antes que ella.
                        after["aporte"] = self._classify(f, data, ev)[0]
                        self.aporte[f["id"]] = after["aporte"]
                    self._item(items, f, "verificacion", cert, verdict,
                               f"{detail} (antes: {data.get('verificacion')})" + (
                                   f"; literales: {json.dumps(lits, ensure_ascii=False)}" if lits else ""),
                               [e for e in ev if str(e)[0] in "CF"], after)
            # 3. Alcance de la afirmación con el alcance (re)calculado de su evidencia.
            if self.cert[f["id"]] == "inferencia":
                sc = self._cited_scope(data, ev)
                ok, note = K.claim_scope_check(text, data.get("alcance_declarado"), sc)
                if not ok:
                    self._item(items, f, "alcance_afirmacion", "hipotesis", "generaliza_mas_alla_de_la_evidencia",
                               note, [e for e in ev if str(e).startswith("C")] or ev,
                               {"certeza": "hipotesis", "alcance": (sc or {}).get("descripcion"),
                                "verificar_con": K.how_to_verify(sc)})
        # 4. Texto cortado por un límite anterior.
        # Ya marcado y sin nada que cambiar: no se repite (si un evento posterior lo volvió a inferencia, sí).
        flagged = ("hecho", f["id"], "texto_incompleto") in self.done and self.cert[f["id"]] != "inferencia"
        if f["kind"] in MODEL_KINDS + ("incertidumbre",) and not data.get("texto_incompleto") and not flagged:
            limit = next((n for n in OLD_LIMITS if V.is_truncated(text, n) and len(text) <= n), None)
            if limit:
                new = "hipotesis" if self.cert[f["id"]] == "inferencia" else self.cert[f["id"]]
                self._item(items, f, "texto_incompleto", new, "texto_incompleto",
                           f"el texto ocupa el límite de {limit} caracteres sin terminar la frase: puede faltarle un "
                           "calificador; no se certifica (el texto original se conserva)", ev,
                           {"certeza": new, "texto_incompleto": True, "limite": limit})
        # 5. Aporte: lo contado como conocimiento nuevo que solo reformula o redondea algo ya registrado.
        if f["kind"] in MODEL_KINDS and self.cert[f["id"]] == "inferencia" and self.aporte.get(f["id"]) in K.KNOWLEDGE \
                and data.get("clave"):
            aporte, rel, why = self._classify(f, data, ev)
            if aporte not in K.KNOWLEDGE:
                c = self.cert[f["id"]]
                self._item(items, f, "aporte", c, aporte, f"{why} (antes contado como «{self.aporte[f['id']]}»)",
                           [f"F{rel}"] if rel else [], {"certeza": c, "aporte": aporte, "relacion_con": rel})
                self.aporte[f["id"]] = aporte

    # API ----------------------------------------------------------------------------------------------
    def plan(self, only: set[str] | None = None) -> list[dict]:
        """Lo que cambiaría (sin escribir nada). only: {"F106", "C61"} limita el plan a esos objetos."""
        self._load()
        items: list[dict] = []
        self._rule_scopes(items)
        for f in self.facts:
            self._rule_fact(items, f)
        if only:
            items = [it for it in items if f"{'C' if it['objeto'] == 'consulta' else 'F'}{it['id']}" in only]
        return items

    @staticmethod
    def code(items: list[dict]) -> str:
        canon = [[it["objeto"], it["id"], it["regla"], it["certeza_anterior"], it["certeza_nueva"], it["veredicto"]]
                 for it in items]
        return hashlib.sha256(json.dumps(canon).encode()).hexdigest()[:12]

    def apply(self, items: list[dict], plan_code: str, authorized_by: str) -> int:
        """Registra el plan autorizado. Exige el código del plan visto y la tabla explore_reviews (migración 0007)."""
        if not items:
            return 0
        if self.code(items) != plan_code:
            raise ValueError("el plan cambió desde la simulación (o el código no corresponde): volvé a simular")
        if not self.has_reviews:
            raise RuntimeError("falta la migración 0007 (explore_reviews)")
        now = time.time()
        with self.db.tx() as c:
            for it in items:
                rid = c.execute(
                    "INSERT INTO explore_reviews(conn, objeto, objeto_id, regla, version, plan, certeza_anterior,"
                    " certeza_nueva, veredicto, detalle, antes_json, despues_json, evidencia_json, autorizado_por,"
                    " created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (self.conn, it["objeto"], it["id"], it["regla"], VERSION, plan_code, it["certeza_anterior"],
                     it["certeza_nueva"], it["veredicto"], it["detalle"], json.dumps(it["antes"], default=str),
                     json.dumps(it["despues"], default=str), json.dumps(it["evidencia"]), authorized_by, now)).lastrowid
                if it["objeto"] == "hecho":
                    c.execute("INSERT INTO explore_fact_history(fact_id, conn, mission_id, event, certainty_before,"
                              " certainty_after, text, evidence_json, data_json, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                              (it["id"], self.conn, None, "reevaluado", it["certeza_anterior"], it["certeza_nueva"], None,
                               json.dumps(it["evidencia"]), json.dumps({"revision": rid, "regla": it["regla"],
                                                                        "veredicto": it["veredicto"],
                                                                        "detalle": it["detalle"], "version": VERSION,
                                                                        "plan": plan_code}, default=str), now))
        return len(items)
