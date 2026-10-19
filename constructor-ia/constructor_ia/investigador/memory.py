"""Memoria persistente de investigaciones y selección del contexto relevante para cada llamada.

La memoria guarda investigaciones, no frases: cada ciclo registra qué pregunta intentaba responder, por qué surgió,
qué evidencia obtuvo, qué conclusiones quedaron respaldadas (y cuáles no), su alcance, qué dudas abrió o cerró, qué
falló y en qué línea de investigación estaba. Los hechos siguen en explore_facts (con su certeza efectiva), sin
modificar los históricos: los ciclos los referencian.

El contexto que recibe el modelo lo arma el software con un tope de tokens: la línea activa, las dudas abiertas,
lo que ya se sabe de las tablas en juego, lo que ya se midió (para no repetirlo sin saberlo), los intentos fallidos
y el esquema. No le dice qué investigar.
"""
from __future__ import annotations

import json
import time

from ..explorer import knowledge as K
from ..explorer.context import estimate_tokens
from ..explorer.mapstore import EFFECTIVE_SQL

# Cantidades por nivel de contexto (0 = completo; los siguientes, si no entra en el presupuesto de tokens).
FACTS = (8, 5, 3)
DOUBTS = (4, 3, 2)
MEASURED = (8, 5, 3)
LINE_CYCLES = (2, 2, 1)
FAILURES = (3, 2, 1)
OTHER_LINES = (3, 2, 0)
FULL_SCHEMA_COLS = (80, 40, 0)  # si el esquema entero tiene hasta estas columnas, se muestra completo


def _clip(text, n: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def _j(v):
    return json.dumps(v, ensure_ascii=False, default=str)


class InvMemory:
    def __init__(self, db, maps, conn: str):
        self.db, self.maps, self.conn = db, maps, conn

    # Líneas, ciclos, dudas ------------------------------------------------------------------------
    def new_line(self, mission_id: str, title: str) -> int:
        now = time.time()
        return self.db.execute("INSERT INTO inv_lines(conn, mission_id, title, created_at, updated_at) VALUES(?,?,?,?,?)",
                               (self.conn, mission_id, title[:300], now, now)).lastrowid

    def line(self, line_id: int | None):
        return self.db.one("SELECT * FROM inv_lines WHERE id=?", (line_id,)) if line_id else None

    def start_cycle(self, mission_id: str, line_id: int, n: int, parent_id: int | None, request: dict, origin: str) -> int:
        return self.db.execute(
            "INSERT INTO inv_cycles(conn, mission_id, line_id, n, parent_id, question, why, origin, request_json,"
            " depends_json, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.conn, mission_id, line_id, n, parent_id, request["pregunta"], request.get("porque", ""), origin,
             _j(request), _j(request.get("depende_de") or []), "en_curso", time.time())).lastrowid

    def update_cycle(self, cycle_id: int, **fields) -> None:
        cols = {k: (_j(v) if k.endswith("_json") else v) for k, v in fields.items()}
        sets = ", ".join(f"{k}=?" for k in cols)
        self.db.execute(f"UPDATE inv_cycles SET {sets} WHERE id=?", (*cols.values(), cycle_id))
        if "line_id" not in fields:
            self.db.execute("UPDATE inv_lines SET updated_at=? WHERE id=(SELECT line_id FROM inv_cycles WHERE id=?)",
                            (time.time(), cycle_id))

    def cycle(self, cycle_id: int | None) -> dict | None:
        r = self.db.one("SELECT * FROM inv_cycles WHERE id=?", (cycle_id,)) if cycle_id else None
        if r is None:
            return None
        d = dict(r)
        for k in list(d):
            if k.endswith("_json"):
                d[k[:-5]] = json.loads(d.pop(k)) if d[k] else None
        return d

    def cycles(self, mission_id: str) -> list[dict]:
        return [self.cycle(r[0]) for r in self.db.all("SELECT id FROM inv_cycles WHERE mission_id=? ORDER BY n, id",
                                                      (mission_id,))]

    def open_doubt(self, mission_id: str, cycle_id: int, line_id: int, text: str) -> int:
        now = time.time()
        return self.db.execute("INSERT INTO inv_doubts(conn, mission_id, cycle_id, line_id, text, created_at, updated_at)"
                               " VALUES(?,?,?,?,?,?,?)", (self.conn, mission_id, cycle_id, line_id, text[:300], now,
                                                          now)).lastrowid

    def close_doubt(self, doubt_id: int, cycle_id: int) -> bool:
        cur = self.db.execute("UPDATE inv_doubts SET status='cerrada_por_el_modelo', closed_by=?, updated_at=? WHERE"
                              " id=? AND conn=? AND status='abierta'", (cycle_id, time.time(), doubt_id, self.conn))
        return cur.rowcount > 0

    # Sesión (interrumpir y reanudar) --------------------------------------------------------------
    def save_session(self, mission_id: str, status: str, state: dict, metrics: dict | None = None,
                     resumed_from: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO inv_sessions(mission_id, conn, status, state_json, metrics_json, resumed_from, updated_at)"
            " VALUES(?,?,?,?,?,?,?) ON CONFLICT(mission_id) DO UPDATE SET status=excluded.status,"
            " state_json=excluded.state_json, metrics_json=COALESCE(excluded.metrics_json, inv_sessions.metrics_json),"
            " resumed_from=COALESCE(excluded.resumed_from, inv_sessions.resumed_from), updated_at=excluded.updated_at",
            (mission_id, self.conn, status, _j(state), _j(metrics) if metrics is not None else None, resumed_from,
             time.time()))

    def session(self, mission_id: str) -> dict | None:
        r = self.db.one("SELECT * FROM inv_sessions WHERE mission_id=?", (mission_id,))
        if r is None:
            return None
        d = dict(r)
        d["state"] = json.loads(d.pop("state_json"))
        d["metrics"] = json.loads(d.pop("metrics_json")) if d.get("metrics_json") else None
        return d

    def mark_resumed(self, old_mid: str, new_mid: str) -> None:
        self.db.execute("UPDATE inv_sessions SET status='reanudada', resumed_by=?, updated_at=? WHERE mission_id=?",
                        (new_mid, time.time(), old_mid))

    # Contexto para el modelo ------------------------------------------------------------------------
    def _line_tables(self, line_id: int | None) -> set:
        tabs = set()
        for r in self.db.all("SELECT plan_json FROM inv_cycles WHERE line_id=? ORDER BY id DESC LIMIT 4", (line_id,)):
            p = json.loads(r[0]) if r[0] else {}
            tabs.update(x for x in [p.get("tabla"), *((p.get("destino") or [])[:1])] if x)
        return tabs

    def _line_text(self, line_id, k: int) -> str:
        line = self.line(line_id)
        if line is None:
            return "ninguna todavía: empezás una nueva."
        rows = self.db.all("SELECT id FROM inv_cycles WHERE line_id=? AND status NOT IN ('en_curso') ORDER BY id DESC"
                           " LIMIT ?", (line_id, k))
        out = [f"L{line['id']} «{_clip(line['title'], 120)}»"]
        for r in reversed(rows):
            c = self.cycle(r[0])
            ev = c.get("evidence") or {}
            what = (f"aclaración: {_clip(ev.get('aclaracion'), 120)}" if ev.get("aclaracion") else
                    f"{ev.get('consulta')} ({_clip(ev.get('plan'), 80)}; {ev.get('filas_total', 0)} filas)")
            out.append(f"- Pregunta «{_clip(c['question'], 110)}» → {what}"
                       + (f" → tu lectura: «{_clip(c['reading'], 140)}»" if c.get("reading") else ""))
        return "\n".join(out)

    def _doubts_text(self, line_id, k: int) -> str:
        rows = self.db.all("SELECT id, text, line_id FROM inv_doubts WHERE conn=? AND status='abierta' ORDER BY"
                           " (line_id IS ?) DESC, id DESC LIMIT ?", (self.conn, line_id, k))
        out = [f"D{r['id']}: {_clip(r['text'], 140)}" for r in rows]
        if len(out) < k:  # incertidumbres históricas del Explorador (sin modificarlas)
            for f in self.maps.facts(self.conn, kinds=("incertidumbre",), limit=k - len(out)):
                out.append(f"F{f['id']} (incertidumbre registrada antes): {_clip(f['text'], 140)}")
        return "\n".join(out)

    def _facts_text(self, focus: set, k: int) -> str:
        facts = [f for f in self.maps.facts(self.conn, kinds=("estadistica", "relacion", "hallazgo", "advertencia",
                                                               "cambio"), limit=300)
                 if f["certainty"] in ("comprobado", "inferencia") and not str(f["subject"]).startswith("filas_estimadas:")]

        def rank(f):
            tabs = set(filter(None, str(f.get("clave") or "").partition("|")[0].split(",")))
            hit = bool(focus & tabs) or any(t in f["text"] for t in focus)
            return (not hit, -f["id"])
        out = []
        for f in sorted(facts, key=rank)[:k]:
            d = f.get("data") or {}
            scope = d.get("alcance") or (d.get("observacion") or {}).get("tipo") or ""
            out.append(f"F{f['id']} [{f['certainty']}{' · ' + _clip(scope, 40) if scope else ''}] {_clip(f['text'], 150)}")
        return "\n".join(out)

    def _measured_text(self, focus: set, k: int) -> str:
        rows = self.db.all("SELECT id, mission_id, created_at, firma_forma, alcance_json FROM explore_queries WHERE"
                           " conn=? AND origin IN ('agente', 'investigador') AND status='ok' AND alcance_json IS NOT NULL"
                           " ORDER BY id DESC LIMIT 300", (self.conn,))
        seen, items = set(), []
        for r in rows:
            if r["firma_forma"] in seen:
                continue
            seen.add(r["firma_forma"])
            sc = json.loads(r["alcance_json"]) or {}
            forma = sc.get("forma") or {}
            tabs, cols = forma.get("tablas") or [], forma.get("columnas") or []
            obj = ", ".join(f"{tabs[0]}.{c}" for c in cols) if tabs and cols else ", ".join(tabs) or "?"
            label = (f"{obj}: {forma.get('operacion', '?')} (C{r['id']}, "
                     f"{'tabla completa' if sc.get('completo') else 'muestra o parcial'}, "
                     f"{time.strftime('%Y-%m-%d', time.localtime(r['created_at']))})")
            items.append((not (focus & set(tabs)), -r["id"], label))
        return "\n".join(x[2] for x in sorted(items)[:k])

    def _failures_text(self, k: int) -> str:
        rows = self.db.all("SELECT question, status, evidence_json FROM inv_cycles WHERE conn=? AND status IN"
                           " ('aclaracion', 'fallo_modelo') ORDER BY id DESC LIMIT ?", (self.conn, k))
        out = []
        for r in rows:
            ev = json.loads(r["evidence_json"]) if r["evidence_json"] else {}
            out.append(f"«{_clip(r['question'], 100)}»: "
                       + (f"aclaración — {_clip(ev.get('aclaracion'), 120)}" if r["status"] == "aclaracion"
                          else "respuesta del modelo inválida"))
        return "\n".join(out)

    def _other_lines_text(self, line_id, k: int) -> str:
        if not k:
            return ""
        rows = self.db.all("SELECT id, title FROM inv_lines WHERE conn=? AND status='abierta' AND id IS NOT ? ORDER BY"
                           " updated_at DESC LIMIT ?", (self.conn, line_id, k))
        return "\n".join(f"L{r['id']}: {_clip(r['title'], 110)}" for r in rows)

    def _schema_text(self, schema: dict, focus: set, level: int) -> str:
        tables = schema["tablas"]
        total = sum(len(v["columnas"]) for v in tables.values())
        full_all = total <= FULL_SCHEMA_COLS[level]
        out = []
        for t, v in tables.items():
            n = v.get("filas_estimadas")
            head = f"{t} (~{n} filas)" if n is not None else t
            if full_all or t in focus:
                out.append(f"{head}: " + ", ".join(f"{c['nombre']} {str(c.get('tipo', '')).split('(')[0]}"
                                                   for c in v["columnas"][:40]))
            else:
                out.append(f"{head}, {len(v['columnas'])} columnas")
        return "\n".join(out)

    def briefing(self, schema: dict, objective: str, line_id: int | None, budget_tokens: int) -> tuple[str, int]:
        """(texto, nivel): el contexto relevante que entra en el presupuesto de tokens."""
        focus = self._line_tables(line_id)
        text = ""
        for level in range(3):
            parts = [f"OBJETIVO: {_clip(objective, 300)}",
                     f"LÍNEA DE INVESTIGACIÓN ACTUAL: {self._line_text(line_id, LINE_CYCLES[level])}"]
            for title, body in (("DUDAS ABIERTAS (podés retomarlas citando su id)", self._doubts_text(line_id, DOUBTS[level])),
                                ("LO QUE YA SE SABE (respaldado; citá F# si te apoyás en algo)",
                                 self._facts_text(focus, FACTS[level])),
                                ("YA MEDIDO (pedirlo de nuevo devuelve esa evidencia; «remedir» para ver si cambió)",
                                 self._measured_text(focus, MEASURED[level])),
                                ("INTENTOS FALLIDOS RECIENTES", self._failures_text(FAILURES[level])),
                                ("OTRAS LÍNEAS ABIERTAS", self._other_lines_text(line_id, OTHER_LINES[level])),
                                ("ESQUEMA (nombres reales)", self._schema_text(schema, focus, level))):
                if body:
                    parts.append(f"{title}:\n{body}")
            text = "\n\n".join(parts)
            if estimate_tokens(text) <= budget_tokens:
                return text, level
        return text, 2


def fact_values(maps, conn: str, fid: int) -> set:
    r = maps.db.one(f"SELECT f.text, f.data_json, {EFFECTIVE_SQL} AS ce FROM explore_facts f WHERE f.conn=? AND f.id=?",
                    (conn, fid))
    if r is None:
        return set()
    d = json.loads(r["data_json"] or "{}") or {}
    return set(d.get("valores") or K.claim_values(r["text"]))
