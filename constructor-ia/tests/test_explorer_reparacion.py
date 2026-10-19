"""Regresiones de M20261019-448547 (0.4.5, caverna_01) — v0.4.6.

Observado: 10,2 minutos, 0 consultas, 3 respuestas inválidas y 6 reparaciones. Qwen3 preguntaba por los valores
distintos de una columna que nombraba en su propia pregunta, pero alternaba entre perfil(tabla) sin columna y
frecuentes(tabla) sin columna. El control de coherencia rechazaba bien, pero la reparación no ayudaba:
* decía «perfil usa: tabla, columna (obligatorios: tabla)», cierto para contar filas, falso para ESA pregunta;
* las dos formas del mismo error tenían firmas distintas, así que nunca se activaba el esquema estricto;
* el esquema estricto de perfil no exigía columna ni ofrecía las columnas válidas;
* la columna que el modelo ya había elegido en su pregunta no se aprovechaba.
Además, el informe de esa expedición (sin consultas) registró F85–F87 como inferencias respaldadas citando hechos
de expediciones anteriores, y las consultas de las aplicaciones propuestas no se contrastaban con las columnas reales.

Bases ficticias (nombres aleatorios); `etiqueta` cumple el papel de la columna nombrada.
"""
import json
import unittest

from constructor_ia.explorer.mapstore import MapStore

from . import mariadb_fixture
from .helpers import wait_for
from .test_explorer_regresion import Base, Script

ASK = "¿Cuáles son los valores distintos de etiqueta?"


def D(herramienta, pregunta=ASK, busca="valores_distintos", **kw):
    return {"pregunta": pregunta, "por_que": "conocer los valores", "evidencia": [], "herramienta": herramienta,
            "busca": busca, **kw}


END = {"pregunta": "fin", "por_que": "listo", "evidencia": [], "herramienta": "terminar", "busca": "otro"}


@unittest.skipUnless(mariadb_fixture.available(), "MariaDB no instalado")
class RepairTests(Base):
    def setUp(self):
        super().setUp()
        self.app, self.fake, self.spec = self.make(self.cave, [])

    def mission(self, script, limits=None):
        self.fake.default = script
        spec = {**self.spec, **(limits or {})}
        mid = self.start_mission(self.app, spec)
        wait_for(lambda: self.app.missions.get(mid)["state"] in ("completada", "fallida", "cancelada"), timeout=120)
        self.mid = mid
        run = dict(self.app.db.one("SELECT * FROM explore_runs WHERE mission_id=?", (mid,)))
        return self.app.missions.get(mid), run, json.loads(run["metrics_json"])

    def agent_queries(self, mid=None):
        return [dict(r) for r in self.app.db.all("SELECT tool, sql_text, status FROM explore_queries WHERE mission_id=?"
                                                 " AND origin='agente'", (mid or self.mid,))]

    def stage_a(self):
        return [r for r in self.fake.requests if "ETAPA A" in r["messages"][-1]["content"]
                or "NO se ejecutó (no se gastó un paso)" in r["messages"][-1]["content"]]

    def test_exact_sequence_uses_the_column_the_model_named(self):
        p = self.cave.parent
        perfil, frec = (lambda pr, i: D("perfil", tabla=p)), (lambda pr, i: D("frecuentes", tabla=p))
        # Secuencia de la misión real: tres pasos de tres intentos alternando; cada paso termina con una forma distinta
        # del error, así 0.4.5 nunca veía dos pasos iguales seguidos: 0 consultas, 3 inválidas, 6 reparaciones.
        sequence = [perfil, frec, perfil, frec, perfil, frec, perfil, frec, perfil] + [lambda pr, i: END] * 3
        m, run, mt = self.mission(Script(sequence, report=None))
        queries = self.agent_queries()
        self.assertGreaterEqual(len(queries), 1, "con 0.4.5 esta secuencia ejecutaba 0 consultas")
        self.assertIn("`etiqueta`", queries[0]["sql_text"], "se usó la columna que el modelo nombró")
        self.assertEqual(queries[0]["status"], "ok")
        first = self.app.db.one("SELECT evidence_json, repairs FROM explore_steps WHERE mission_id=? AND status!='invalida'"
                                " ORDER BY id", (self.mid,))
        notes = json.loads(first["evidence_json"])["correcciones"]
        self.assertTrue(any("se usó la columna «etiqueta» que elegiste en tu pregunta" in n for n in notes),
                        "la decisión aprovechada queda a la vista")
        self.assertEqual(first["repairs"], 0)
        self.assertEqual([(q["tool"], "`etiqueta`" in q["sql_text"]) for q in queries[:2]],
                         [("perfil", True), ("frecuentes", True)])

    def test_without_a_named_column_the_repair_requires_a_real_one(self):
        p = self.cave.parent
        m, run, mt = self.mission(Script([
            lambda pr, i: D("perfil", pregunta="¿Qué valores distintos hay en esta tabla?", tabla=p),
            lambda pr, i: D("frecuentes", pregunta="¿Qué valores distintos hay en esta tabla?", tabla=p, columna="etiqueta"),
            lambda pr, i: END]))
        repair = self.stage_a()[1]
        msg = repair["messages"][-1]["content"]
        self.assertIn(f"Columnas válidas de {p}: id, etiqueta, valor", msg)
        self.assertIn("es obligatoria para esa pregunta", msg)
        self.assertNotIn("(obligatorios: tabla)", msg, "ya no se dice que la columna es opcional")
        sc = repair["format"]
        self.assertIn("columna", sc["required"])
        self.assertEqual(sc["properties"]["columna"]["enum"], ["id", "etiqueta", "valor"])
        self.assertEqual(sc["properties"]["tabla"]["enum"], [p], "la tabla sigue siendo la que eligió el modelo")
        self.assertEqual(sc["properties"]["herramienta"]["enum"], ["perfil", "frecuentes", "sql", "abandonar"],
                         "0.4.8: en una reparación el modelo también puede abandonar la pregunta")
        self.assertEqual([q["tool"] for q in self.agent_queries()], ["frecuentes"])
        self.assertEqual(mt["desajustes_pregunta_herramienta"], 1)

    def test_alternating_without_a_column_is_one_repeated_error(self):
        p = self.cave.parent
        q = "¿Qué valores distintos hay en esta tabla?"
        alt = [lambda pr, i: D("perfil", pregunta=q, tabla=p), lambda pr, i: D("frecuentes", pregunta=q, tabla=p)] * 10
        m, run, mt = self.mission(Script(alt), limits={"max_steps": 6})
        # 0.4.8: cada paso fallido queda registrado y el agente sigue; tres seguidos con el mismo atasco cortan el bucle
        self.assertEqual(run["ended_reason"], "respuestas_repetidas")
        inv = self.app.db.all("SELECT repairs, context_json FROM explore_steps WHERE mission_id=? AND status='invalida'",
                              (self.mid,))
        self.assertEqual(len(inv), 3)
        for s in inv:
            tries = json.loads(s["context_json"])["respuestas_modelo"]
            self.assertEqual([t["firma"] for t in tries], ["falta_columna", "falta_columna"],
                             "perfil sin columna y frecuentes sin columna son el MISMO error")
            self.assertEqual(tries[1]["esquema"], "reparacion_columna", "la primera reparación ya exige la columna")
        self.assertLessEqual(len(self.stage_a()), 6)
        self.assertEqual(self.agent_queries(), [])

    def test_a_question_naming_several_columns_is_not_decided_for_the_model(self):
        p = self.cave.parent
        q = "¿Qué valores distintos tienen etiqueta y valor?"
        m, run, mt = self.mission(Script([lambda pr, i: D("perfil", pregunta=q, tabla=p),
                                          lambda pr, i: D("perfil", pregunta=q, tabla=p, columna="valor"),
                                          lambda pr, i: END]))
        msg = self.stage_a()[1]["messages"][-1]["content"]
        self.assertIn("nombraste varias columnas (etiqueta, valor): elegí una", msg)
        self.assertEqual([q_["sql_text"].count("`valor`") > 0 for q_ in self.agent_queries()], [True],
                         "se ejecuta la columna que el modelo eligió al reparar")

    def test_history_only_report_claims_and_proposals_stay_apart(self):
        # 0.4.8: una expedición SIN resultados ya no pide informe (cierre diagnóstico, ver test_explorer_recuperacion).
        # Lo que esta prueba protege sigue igual con una consulta propia que no aporta hallazgos: el informe que solo
        # cita hechos de otra expedición no los registra como nuevos, y las propuestas se contrastan con EXPLAIN.
        p = self.cave.parent
        old_claim = "Hay etiquetas del tipo «nombre 1»"
        self.mission(Script([lambda pr, i: D("muestra", pregunta="¿Qué contiene?", busca="ejemplos", tabla=p),
                             lambda pr, i: END],
                            interpret=lambda pr, i: {"resumen": "Etiquetas.", "hallazgos": [
                                {"texto": old_claim, "tipo": "hallazgo", "alcance": "muestra", "evidencia": [f"C{i['q']}"]}]},
                            report=lambda pr, i: {"resumen": "x", "descubrimientos": [], "aplicaciones": [],
                                                  "incertidumbres": []}))
        first = self.mid
        old = next(f for f in MapStore(self.app.db).facts("cueva", limit=500) if f["text"] == old_claim)

        def report(pr, i):
            self.report_prompt = pr
            return {"resumen": "Resumen.", "descubrimientos": [
                {"texto": "Las etiquetas siguen el patrón «nombre 1»", "evidencia": [f"F{old['id']}"]},
                {"texto": "Hay etiquetas del tipo «nombre 1»", "evidencia": [f"F{old['id']}"]}],
                "aplicaciones": [
                    {"nombre": "Tablero A", "tipo": "tablero", "descripcion": "d", "evidencia": [],
                     "consultas": [f"SELECT etiqueta, COUNT(*) FROM `{p}` GROUP BY etiqueta"]},
                    {"nombre": "Tablero B", "tipo": "tablero", "descripcion": "d", "evidencia": [],
                     "consultas": [f"SELECT symbol_inexistente FROM `{p}`"]},
                    {"nombre": "Tablero C", "tipo": "informe", "descripcion": "d", "evidencia": [],
                     "consultas": [f"SELECT COUNT(*) FROM `{p}`", f"SELECT columna_falsa FROM `{p}`"]}],
                "incertidumbres": []}

        c = self.cave.child
        m, run, mt = self.mission(Script([lambda pr, i: D("muestra", pregunta="¿Qué hay en la otra tabla?",
                                                          busca="ejemplos", tabla=c), lambda pr, i: END],
                                         report=report), limits={"max_steps": 6})
        second = self.mid
        self.assertEqual(mt["consultas_propias_con_resultado"], 1)
        self.assertIn(f"F{old['id']} [histórico", self.report_prompt)
        own = [f for f in MapStore(self.app.db).facts("cueva", limit=500)
               if f["mission_id"] == second and f["kind"] == "hallazgo"]
        self.assertEqual(own, [], "sin consultas no hay hallazgos propios: lo histórico no se registra como nuevo")
        self.assertEqual(mt["afirmaciones_historicas_del_informe"], 2)
        again = next(f for f in MapStore(self.app.db).facts("cueva", limit=500) if f["id"] == old["id"])
        self.assertEqual((again["certainty"], again["mission_id"], again["text"]), ("inferencia", first, old_claim))
        informe = self.app.files.read_rel(second, "exploracion/informe.md")
        sec_new = informe.split("## 1. Conocimiento nuevo de esta expedición")[1].split("## 2.")[0]
        self.assertNotIn(old_claim, sec_new)
        sec_hist = informe.split("## 2. Conocimiento histórico")[1].split("## 3.")[0]
        self.assertIn("Síntesis del agente sobre conocimiento histórico", sec_hist)
        self.assertIn(f"creado en {first}", sec_hist)
        sec_prop = informe.split("## 3. Propuestas de aplicaciones (no son hallazgos)")[1]
        self.assertIn("✓ válida para el esquema real", sec_prop)
        self.assertIn("Unknown column", sec_prop, "MariaDB contrasta las columnas propuestas con el esquema real")
        apps = {f["data"]["nombre"]: f for f in MapStore(self.app.db).facts("cueva", kinds=("oportunidad",))
                if f["mission_id"] == second}
        self.assertEqual(set(apps), {"Tablero A", "Tablero C"}, "una propuesta sin ninguna consulta válida se descarta")
        self.assertEqual(apps["Tablero A"]["certainty"], "hipotesis", "una propuesta no es un hecho")
        self.assertEqual(mt["consultas_propuestas"], {"validas": 2, "invalidas": 2, "sin_verificar": 0})
        explains = self.app.db.all("SELECT sql_text FROM explore_queries WHERE mission_id=? AND tool='validacion'", (second,))
        self.assertTrue(explains and all(r[0].startswith("EXPLAIN ") for r in explains), "validación sin ejecutar")
        self.assertEqual(run["objective_met"], 0)


if __name__ == "__main__":
    unittest.main()
