"""Conocimiento acumulativo — v0.4.5.

Problemas observados en dos expediciones reales con Qwen3 4B (0.4.4):
1. La pregunta pedía valores distintos o frecuentes, pero se ejecutaba una herramienta que solo cuenta filas.
2. Se repetía exactamente la misma consulta, incluso entre expediciones.
3. Se consideraban completos ciclos que no aportaban conocimiento nuevo (además, upsert reasignaba la expedición
   de un hecho viejo repetido y podía bajarle la certeza).
4. Se generalizaban conclusiones a toda una tabla a partir de diez registros.

El modelo simulado elige tablas, preguntas y herramientas; el sistema solo informa, repara y clasifica. Las bases
son ficticias (nombres aleatorios).
"""
import json
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from constructor_ia import db as dbmod
from constructor_ia.db import Database
from constructor_ia.explorer import knowledge as K
from constructor_ia.explorer.mapstore import MapStore

from . import mariadb_fixture
from .helpers import wait_for
from .test_explorer_contexto import WideBase
from .test_explorer_regresion import Script, good_report

T = {"ops": {"columnas": [{"nombre": n, "tipo": t} for n, t in (("id", "int"), ("duracion", "int"), ("motivo", "varchar"),
                                                              ("creado", "datetime"))]},
     "ev": {"columnas": [{"nombre": "id", "tipo": "int"}]}}


def tmp_db(test) -> Database:
    d = tempfile.mkdtemp()
    test.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
    db = Database(Path(d) / "c.db")
    db.migrate()
    return db


class ProvenanceTests(unittest.TestCase):
    """La memoria existente nunca pierde certeza, texto original ni procedencia."""

    def setUp(self):
        self.db = tmp_db(self)
        self.m = MapStore(self.db)

    def fact(self, fid):
        return dict(self.db.one("SELECT * FROM explore_facts WHERE id=?", (fid,)))

    def test_restating_with_less_support_never_downgrades(self):
        fid, out = self.m.record_note("c", "hallazgo", "La duración es 1080", evidence=["C5"], mission_id="M1",
                                      certainty="inferencia", data={"verificacion": "respaldada"})
        self.assertEqual(out, "creado")
        _, out = self.m.record_note("c", "hallazgo", "la duración  es 1080", evidence=[], mission_id="M2",
                                    certainty="sin_verificar", data={"verificacion": "sin_evidencia"})
        self.assertEqual(out, "reafirmado_sin_respaldo")
        f = self.fact(fid)
        self.assertEqual((f["certainty"], f["mission_id"], f["text"]), ("inferencia", "M1", "La duración es 1080"))
        self.assertEqual(f["last_mission_id"], "M2")
        self.assertEqual(len(json.loads(f["data_json"])["reafirmaciones"]), 1)
        events = [(h["event"], h["mission_id"], h["certainty_before"], h["certainty_after"]) for h in self.m.history("c", fid)]
        self.assertEqual(events, [("creado", "M1", None, "inferencia"),
                                  ("reafirmado_sin_respaldo", "M2", "inferencia", "inferencia")])

    def test_better_support_raises_certainty_and_keeps_the_original_text(self):
        fid, _ = self.m.record_note("c", "hallazgo", "Hay valores «X»", mission_id="M1", certainty="sin_verificar",
                                    data={"verificacion": "sin_evidencia"})
        _, out = self.m.record_note("c", "hallazgo", "HAY valores «X»", evidence=["C9"], mission_id="M2",
                                    certainty="inferencia", data={"verificacion": "respaldada"})
        self.assertEqual(out, "certeza_subio")
        f = self.fact(fid)
        self.assertEqual((f["certainty"], f["text"], f["mission_id"]), ("inferencia", "Hay valores «X»", "M1"))
        self.assertEqual(json.loads(f["evidence_json"]), ["C9"])
        self.assertEqual(self.m.history("c", fid)[-1]["certainty_before"], "sin_verificar")

    def test_same_support_is_a_confirmation_with_merged_evidence(self):
        fid, _ = self.m.record_note("c", "hallazgo", "Motivo «A»", evidence=["C1"], mission_id="M1", certainty="inferencia")
        _, out = self.m.record_note("c", "hallazgo", "Motivo «A»", evidence=["C7"], mission_id="M3", certainty="inferencia")
        self.assertEqual(out, "confirmado")
        f = self.fact(fid)
        self.assertEqual(json.loads(f["evidence_json"]), ["C1", "C7"])
        self.assertEqual(f["mission_id"], "M1")

    def test_model_cannot_revive_a_discarded_fact(self):
        fid, _ = self.m.record_note("c", "hallazgo", "Relación refutada", mission_id="M1", certainty="hipotesis")
        subject = self.fact(fid)["subject"]
        self.m.upsert("c", "hallazgo", "descartado", subject, "Relación refutada", mission_id="M1")  # medición del sistema
        _, out = self.m.record_note("c", "hallazgo", "Relación refutada", evidence=["C2"], mission_id="M2",
                                    certainty="inferencia")
        self.assertEqual(self.fact(fid)["certainty"], "descartado")
        self.assertEqual(out, "reafirmado_sin_respaldo")

    def test_system_reobservation_keeps_creator_and_records_previous_state(self):
        fid = self.m.upsert("c", "estadistica", "comprobado", "filas:ops", "ops: 60 filas contadas", mission_id="M1",
                            evidence=["C3"])
        self.m.upsert("c", "estadistica", "comprobado", "filas:ops", "ops: 60 filas contadas", mission_id="M2",
                      evidence=["C3"])
        self.assertEqual(len(self.m.history("c", fid)), 1, "sin cambios no se agrega historial")
        self.m.upsert("c", "estadistica", "comprobado", "filas:ops", "ops: 75 filas contadas", mission_id="M3",
                      evidence=["C11"])
        f = self.fact(fid)
        self.assertEqual((f["mission_id"], f["last_mission_id"], f["text"]), ("M1", "M3", "ops: 75 filas contadas"))
        last = self.m.history("c", fid)[-1]
        self.assertEqual((last["event"], last["text"], last["evidence"]), ("reobservado", "ops: 60 filas contadas", ["C3"]))


class MigrationTests(unittest.TestCase):
    def test_migration_0006_preserves_existing_memory(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        old = Path(d) / "mig"
        old.mkdir()
        for f in sorted(dbmod.MIGRATIONS_DIR.glob("000[1-5]_*.sql")):
            shutil.copy(f, old / f.name)
        path = Path(d) / "c.db"
        with mock.patch.object(dbmod, "MIGRATIONS_DIR", old):
            self.assertEqual(Database(path).migrate(), 5)
        c = sqlite3.connect(path)
        now = time.time()
        rows = [("c", "M-vieja", "hallazgo", "inferencia", "hallazgo:a", "Hecho respaldado viejo", '["C1"]'),
                ("c", "M-vieja", "estadistica", "comprobado", "filas:ops", "ops: 60 filas contadas", '["C2"]'),
                ("c", "M-otra", "hallazgo", "sin_verificar", "hallazgo:b", "Afirmación sin verificar", "[]")]
        for r in rows:
            c.execute("INSERT INTO explore_facts(conn, mission_id, kind, certainty, subject, text, evidence_json,"
                      " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (*r, now, now))
        c.commit()
        c.close()
        db = Database(path)
        self.assertEqual(db.migrate(), 9)  # hasta la última migración (0007, 0008 y 0009 solo agregan tablas)
        got = [tuple(r) for r in db.all("SELECT conn, mission_id, kind, certainty, subject, text, evidence_json FROM"
                                        " explore_facts ORDER BY id")]
        self.assertEqual(got, rows, "la migración no cambia ningún hecho")
        self.assertEqual([r[0] for r in db.all("SELECT last_mission_id FROM explore_facts ORDER BY id")],
                         ["M-vieja", "M-vieja", "M-otra"])
        hist = db.all("SELECT event, certainty_before FROM explore_fact_history ORDER BY fact_id")
        self.assertEqual([tuple(h) for h in hist], [("estado_al_migrar_0.4.5", "inferencia"),
                                                    ("estado_al_migrar_0.4.5", "comprobado"),
                                                    ("estado_al_migrar_0.4.5", "sin_verificar")])
        # el código 0.4.4 inserta con columnas nombradas: sigue funcionando sobre el esquema nuevo (rollback)
        db.execute("INSERT INTO explore_facts(conn, mission_id, kind, certainty, subject, text, data_json, evidence_json,"
                   " created_at, updated_at) VALUES('c','M-044','hallazgo','inferencia','hallazgo:z','x',NULL,'[]',?,?)",
                   (now, now))


class KnowledgeRulesTests(unittest.TestCase):
    def test_tool_capabilities_and_coherence(self):
        count_rows = K.tool_shape("perfil", {"tabla": "ops"})
        self.assertIn("solo cuenta filas", K.coherence("valores_distintos", None, "perfil", {"tabla": "ops"}, count_rows))
        self.assertEqual(K.coherence("filas", None, "perfil", {"tabla": "ops"}, count_rows), "")
        freq = K.tool_shape("frecuentes", {"tabla": "ops", "columna": "motivo"})
        self.assertEqual(K.coherence("frecuencias", "motivo", "frecuentes", {"tabla": "ops", "columna": "motivo"}, freq), "")
        self.assertIn("columna «duracion»", K.coherence("frecuencias", "duracion", "frecuentes",
                                                         {"tabla": "ops", "columna": "motivo"}, freq))
        self.assertEqual(K.coherence("otro", None, "perfil", {"tabla": "ops"}, count_rows), "", "«otro» no se controla")

    def test_sql_is_never_blocked_by_the_heuristic(self):
        for sql in ("WITH x AS (SELECT 1) SELECT * FROM x", "SELECT COUNT(*) FROM ops", "SELECT 1"):
            self.assertEqual(K.coherence("frecuencias", None, "sql", {"sql": sql}, K.sql_shape(sql, T)), "")
        self.assertEqual(K.answer_status("frecuencias", K.sql_shape("SELECT COUNT(*) FROM ops", T), "datos"), "parcial")
        self.assertEqual(K.answer_status("frecuencias", K.sql_shape("WITH x AS (SELECT 1) SELECT * FROM x", T), "datos"),
                         "indeterminada")
        self.assertEqual(K.answer_status("frecuencias", K.sql_shape(
            "SELECT motivo, COUNT(*) FROM ops GROUP BY motivo", T), "datos"), "respondida")

    def test_signatures(self):
        self.assertEqual(K.firma("SELECT  *\nFROM `ops` LIMIT 10;"), K.firma("select * from ops limit 10"))
        self.assertNotEqual(K.firma("SELECT * FROM ops WHERE motivo='A'"), K.firma("SELECT * FROM ops WHERE motivo='a'"))
        a = K.firma_forma(K.sql_shape("SELECT * FROM ops LIMIT 10", T))
        self.assertEqual(a, K.firma_forma(K.sql_shape("SELECT * FROM ops LIMIT 5", T)), "casi iguales")

    def test_scope_and_generalization(self):
        sample = K.scope(K.tool_shape("muestra", {"tabla": "ops"}), 10, False, {"ops": (60, True)})
        self.assertEqual(sample["tipo"], "muestra_arbitraria")
        self.assertIn("NO es una muestra aleatoria", sample["descripcion"])
        self.assertFalse(K.claim_scope_check("Todas las operaciones duran 1080", None, sample)[0])
        self.assertFalse(K.claim_scope_check("Las operaciones duran 1080", "tabla", sample)[0])
        self.assertTrue(K.claim_scope_check("Las diez filas leídas duran 1080", "muestra", sample)[0])
        full = K.scope(K.tool_shape("perfil", {"tabla": "ops", "columna": "duracion"}), 1, False, {"ops": (60, True)},
                       analyzed=60, sample_cap=100000)
        self.assertTrue(K.claim_scope_check("Todas las operaciones duran 1080", None, full)[0])
        tiny = K.scope(K.tool_shape("muestra", {"tabla": "ev"}), 4, False, {"ev": (4, False)})
        self.assertEqual(tiny["tipo"], "tabla_completa", "devolvió menos que el LIMIT: es la tabla entera")
        sub = K.scope(K.sql_shape("SELECT * FROM ops WHERE motivo='A' LIMIT 5", T), 5, False, {"ops": (60, True)})
        self.assertEqual(sub["tipo"], "subconjunto")

    def test_classification_is_conservative(self):
        base = {"key": "ops|duracion", "values": {"1080"}, "consulta": 9, "tipo": "agregado_total", "observadas": 1,
                "mision": "M2", "ts": 10_000.0}
        old = {"id": 4, "values": {"1080", "60"}, "consulta": 3, "tipo": "agregado_total", "observadas": 1,
               "mision": "M1", "ts": 1_000.0}
        self.assertEqual(K.classify({**base, "key": None}, [old])[0], "indeterminado")
        self.assertEqual(K.classify(base, [])[0], "nuevo")
        self.assertEqual(K.classify({**base, "values": set()}, [old])[0], "indeterminado")
        self.assertEqual(K.classify(base, [old])[0], "confirma")
        changed = {**base, "values": {"900"}}
        self.assertEqual(K.classify(changed, [old])[0], "difiere_tiempo", "otro momento: no es contradicción")
        self.assertEqual(K.classify(changed, [{**old, "tipo": "muestra_arbitraria", "observadas": 10}])[0],
                         "difiere_alcance", "otro alcance: no es contradicción")
        same = {**old, "mision": "M2", "ts": 9_900.0}
        self.assertEqual(K.classify(changed, [same])[0], "contradice")
        self.assertEqual(K.classify({**changed, "consulta": 3}, [same])[0], "amplia", "misma evidencia: otra faceta")
        self.assertTrue(K.is_trivial("La consulta devolvió 10 filas", 10, 10))
        self.assertFalse(K.is_trivial("Hay 10 filas con motivo «X»", 10, 10))

    def test_claim_key_uses_mentions_and_where_values_appear(self):
        shape = K.tool_shape("muestra", {"tabla": "ops"})
        self.assertEqual(K.claim_key("La duración es 1080", shape, T), "ops|duracion")
        self.assertEqual(K.claim_key("Aparece «VENCIDO»", shape, T, ["id", "motivo"], [[1, "VENCIDO"]]), "ops|motivo")
        self.assertIsNone(K.claim_key("Los datos parecen simulados", shape, T), "sin columna identificable")


def D(pregunta, herramienta, busca="otro", **kw):
    return {"pregunta": pregunta, "por_que": "investigar", "evidencia": [], "herramienta": herramienta, "busca": busca, **kw}


END = D("fin", "terminar")


@unittest.skipUnless(mariadb_fixture.available(), "MariaDB no instalado")
class CumulativeKnowledgeTests(WideBase):
    """Varias expediciones sobre la MISMA memoria (misma aplicación)."""

    def setUp(self):
        super().setUp()
        self.app, self.fake, self.spec = self.make(self.cave, [])

    def mission(self, script):
        self.fake.default = script
        mid = self.start_mission(self.app, dict(self.spec))
        wait_for(lambda: self.app.missions.get(mid)["state"] in ("completada", "fallida", "cancelada"), timeout=120)
        self.mid = mid
        run = dict(self.app.db.one("SELECT * FROM explore_runs WHERE mission_id=?", (mid,)))
        return self.app.missions.get(mid), run, json.loads(run["metrics_json"])

    def decisions(self):
        return [r["messages"][-1]["content"] for r in self.fake.requests if "ETAPA B:" not in r["messages"][-1]["content"]
                and "MAPA CONSTRUIDO" not in r["messages"][-1]["content"]]

    def fact_by_text(self, text):
        return next(f for f in MapStore(self.app.db).facts("cueva", limit=500) if f["text"] == text)

    def test_question_and_tool_mismatch_is_repaired_but_sql_is_free(self):
        cte = f"WITH x AS (SELECT COUNT(*) AS c FROM {self.ops}) SELECT c FROM x"
        m, run, mt = self.mission(Script([
            lambda p, i: D("¿Qué motivos distintos hay?", "perfil", "valores_distintos", tabla=self.ops),
            lambda p, i: D("¿Qué motivos distintos hay?", "frecuentes", "frecuencias", tabla=self.ops, columna="motivo"),
            lambda p, i: D("¿Cuántos motivos distintos hay?", "sql", "valores_distintos", sql=cte),
            lambda p, i: END], report=good_report))
        repair = next(p for p in self.decisions() if "NO se ejecutó" in p)
        self.assertIn("perfil(tabla) sin columna solo cuenta filas", repair)
        self.assertIn("frecuentes(tabla, columna)", repair, "se informan las herramientas posibles; elige el modelo")
        self.assertEqual(mt["desajustes_pregunta_herramienta"], 1)
        rows = self.app.db.all("SELECT tool, busca, respuesta FROM explore_queries WHERE mission_id=? AND origin='agente'"
                               " ORDER BY id", (self.mid,))
        self.assertEqual([tuple(r) for r in rows], [("frecuentes", "frecuencias", "respondida"),
                                                    ("sql", "valores_distintos", "parcial")],
                         "el SQL que el analizador no entiende del todo se ejecuta igual")

    def test_repeated_query_across_expeditions_needs_a_reason_and_detects_changes(self):
        sample = lambda p, i: D("¿Qué registros hay?", "muestra", "ejemplos", tabla=self.ops)  # noqa: E731
        self.mission(Script([sample, lambda p, i: END], report=good_report))
        first = self.mid
        why = lambda p, i: D("¿Qué registros hay?", "muestra", "ejemplos", tabla=self.ops,  # noqa: E731
                             repetir_porque="ver si los datos cambiaron")
        m, run, mt = self.mission(Script([sample, why, lambda p, i: END], report=good_report))
        repair = next(p for p in self.decisions() if "ya se ejecutó" in p)
        self.assertIn(f"en la expedición {first}", repair)
        self.assertIn("repetir_porque", repair)
        self.assertEqual((mt["consultas_repetidas_sin_justificar"], mt["consultas_repetidas_justificadas"],
                          mt["cambios_de_datos_detectados"]), (1, 1, 0))
        ap = json.loads(self.app.db.one("SELECT aporte_json FROM explore_steps WHERE mission_id=? AND tool='muestra'",
                                        (self.mid,))[0])
        self.assertIn("sin cambios", ap["repeticion"])
        self.srv.sql(f"UPDATE `{self.ops}` SET motivo='OTRO' WHERE id=1000", database=self.cave.db)
        m, run, mt = self.mission(Script([why, lambda p, i: D("¿Cuánto dura?", "perfil", "rango_nulos", tabla=self.ops,
                                                              columna="duracion"), lambda p, i: END], report=good_report))
        self.assertEqual(mt["cambios_de_datos_detectados"], 1)
        change = [f for f in MapStore(self.app.db).facts("cueva", kinds=("cambio",)) if "consulta repetida" in f["text"]]
        self.assertEqual(len(change), 1)
        self.assertEqual(mt["ciclos_con_conocimiento_nuevo"], 1, "un cambio de datos detectado es conocimiento nuevo")

    def test_restating_known_knowledge_is_not_new_and_preserves_provenance(self):
        claim = "Las filas leídas tienen duración 1080"
        interp = lambda p, i: {"resumen": "Duración 1080.", "hallazgos": [  # noqa: E731
            {"texto": claim, "tipo": "hallazgo", "alcance": "muestra", "evidencia": [f"C{i['q']}"]}]}
        m, run, mt = self.mission(Script([
            lambda p, i: D("¿Qué registros hay?", "muestra", "ejemplos", tabla=self.ops),
            lambda p, i: D("¿Cuántas filas hay?", "perfil", "filas", tabla=self.ops), lambda p, i: END],
            report=good_report, interpret=lambda p, i: interp(p, i) if "SQL EJECUTADO: SELECT * FROM" in p else
            {"resumen": "Conteo.", "hallazgos": []}))
        first = self.mid
        self.assertEqual((mt["ciclos_formales"], mt["ciclos_con_conocimiento_nuevo"]), (1, 1))
        original = self.fact_by_text(claim)
        self.assertEqual(original["data"]["aporte"], "nuevo")

        def restate(p, i):
            return {"resumen": "Duración 1080.", "hallazgos": [
                {"texto": claim, "tipo": "hallazgo", "alcance": "muestra", "evidencia": [f"C{i['q']}"]},
                {"texto": "En estas filas la duración observada es 1080", "tipo": "hallazgo", "alcance": "muestra",
                 "evidencia": [f"C{i['q']}"]},
                {"texto": claim, "tipo": "hallazgo", "evidencia": ["C999999"]}]}

        m, run, mt = self.mission(Script([
            lambda p, i: D("¿Y las primeras cinco?", "sql", "ejemplos", sql=f"SELECT * FROM {self.ops} LIMIT 5"),
            lambda p, i: D("¿Qué motivos hay?", "frecuentes", "frecuencias", tabla=self.ops, columna="motivo"),
            lambda p, i: END], report=good_report,
            interpret=lambda p, i: restate(p, i) if "LIMIT 5" in p else {"resumen": "Motivos.", "hallazgos": []}))
        self.assertEqual(mt["ciclos_formales"], 1)
        self.assertEqual(mt["ciclos_con_conocimiento_nuevo"], 0, "repetir lo sabido no es conocimiento nuevo")
        self.assertEqual(mt["hechos_nuevos"], 0)
        self.assertGreaterEqual(mt["confirmaciones"], 2)
        self.assertEqual(run["objective_met"], 0)
        self.assertTrue(any("ninguno aportó conocimiento nuevo" in r for r in json.loads(run["reasons_json"])))
        after = self.fact_by_text(claim)
        self.assertEqual((after["id"], after["certainty"], after["mission_id"], after["text"]),
                         (original["id"], "inferencia", first, claim), "certeza, procedencia y texto intactos")
        events = [h["event"] for h in MapStore(self.app.db).history("cueva", after["id"])]
        self.assertEqual(events, ["creado", "confirmado", "reafirmado_sin_respaldo"])

    def test_conclusions_about_the_whole_table_need_whole_table_evidence(self):
        wide = "Todas las operaciones tienen duración 1080"
        m, run, mt = self.mission(Script([
            lambda p, i: D("¿Qué registros hay?", "muestra", "ejemplos", tabla=self.ops),
            lambda p, i: D("¿La duración es siempre 1080?", "perfil", "rango_nulos", tabla=self.ops, columna="duracion"),
            lambda p, i: D("¿Qué categorías hay?", "muestra", "ejemplos", tabla=self.cave.ref),
            lambda p, i: END], report=good_report,
            interpret=lambda p, i: {"resumen": "Duración 1080.", "hallazgos": [
                {"texto": wide if self.ops in p and "perfil" not in p.lower() and "LIMIT 10" in p else
                 (wide + " (un valor distinto)" if "COUNT(DISTINCT" in p else "Todas las filas tienen descripción «cat»"),
                 "tipo": "hallazgo", "evidencia": [f"C{i['q']}"]}]}))
        sample_claim = self.fact_by_text(wide)
        self.assertEqual(sample_claim["certainty"], "hipotesis")
        self.assertEqual(sample_claim["text"], wide, "el texto original no se modifica")
        self.assertEqual(sample_claim["data"]["verificacion"], "generaliza_mas_alla_de_la_evidencia")
        self.assertIn("NO es una muestra aleatoria", sample_claim["data"]["alcance"])
        self.assertIn("tabla completa", sample_claim["data"]["verificar_con"])
        pending = [p for p in self.decisions() if "PENDIENTE DE VERIFICAR" in p]
        self.assertTrue(pending and f"F{sample_claim['id']}" in pending[0], "queda como hipótesis abierta en el mapa")
        verified = self.fact_by_text(wide + " (un valor distinto)")
        self.assertEqual(verified["certainty"], "inferencia", "el perfil cubre la tabla completa")
        small = self.fact_by_text("Todas las filas tienen descripción «cat»")
        self.assertEqual(small["certainty"], "inferencia", "una tabla leída entera admite conclusiones generales")
        self.assertEqual(mt["generalizaciones_acotadas"], 1)

    def test_values_that_change_over_time_are_not_contradictions(self):
        prof = lambda p, i: D("¿Cuánto dura?", "perfil", "rango_nulos", tabla=self.ops, columna="duracion")  # noqa: E731
        m, run, mt = self.mission(Script([prof, lambda p, i: END], report=good_report,
                                         interpret=lambda p, i: {"resumen": "Constante.", "hallazgos": [
                                             {"texto": "La duración es 1080 en todas las filas", "tipo": "hallazgo",
                                              "evidencia": [f"C{i['q']}"]}]}))
        self.srv.sql(f"UPDATE `{self.ops}` SET duracion=900 WHERE id<1010", database=self.cave.db)
        rng = f"SELECT MIN(duracion) AS minimo, MAX(duracion) AS maximo FROM {self.ops}"
        # Expedición siguiente, minutos después: los datos cambiaron entre ambas.
        m, run, mt = self.mission(Script([lambda p, i: D("¿Rango?", "sql", "rango_nulos", sql=rng), lambda p, i: END],
                                         report=good_report,
                                         interpret=lambda p, i: {"resumen": "Rango.", "hallazgos": [
                                             {"texto": "La duración va de 900 a 1080", "tipo": "hallazgo",
                                              "evidencia": [f"C{i['q']}"]}]}))
        later = self.fact_by_text("La duración va de 900 a 1080")
        self.assertEqual(later["data"]["aporte"], "difiere_tiempo")
        self.assertEqual(mt["contradicciones"], 0)
        self.assertEqual(self.fact_by_text("La duración es 1080 en todas las filas")["certainty"], "inferencia",
                         "la observación anterior se conserva con su momento")


if __name__ == "__main__":
    unittest.main()
