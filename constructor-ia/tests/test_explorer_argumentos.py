"""Regresiones de la primera prueba real de 0.4.2 sobre caverna_01 — v0.4.3.

Observado: la exploración repetía «la tabla "null" no existe», agotaba las dos reparaciones y volvía a producir el
mismo error sin ejecutar consultas.

Causa: el esquema de la etapa A declaraba los argumentos como texto; la salida estructurada no puede emitir un JSON
null, así que el modelo escribía la PALABRA "null" en los campos que no aplicaban. normalize_step descartaba None y
"" pero no "null", y validaba también argumentos que la herramienta no usa. La reparación pedía «el MISMO objeto
JSON corregido», y el modelo lo copiaba.

Las pruebas usan bases de nombres aleatorios: el sistema nunca elige la tabla por el modelo.
"""
import json
import unittest

from constructor_ia.explorer import validation as V
from constructor_ia.explorer.agent import Explorer

from . import mariadb_fixture
from .test_explorer_regresion import Base, Script, good_report

TABLES = {"ops": {"columnas": [{"nombre": "id"}, {"nombre": "precio"}]},
          "eventos": {"columnas": [{"nombre": "id"}, {"nombre": "tipo"}]}}


def nulls(**over):
    """Respuesta de etapa A como la observada: todos los campos de texto presentes, los que no aplican = "null"."""
    d = {"pregunta": "¿Qué contiene la tabla?", "por_que": "leer registros", "evidencia": [], "herramienta": "muestra",
         "tabla": "null", "columna": "null", "tabla_destino": "null", "columna_destino": "null", "sql": "null"}
    d.update(over)
    return d


class NormalizeTests(unittest.TestCase):
    def test_text_null_is_absent_not_a_table_name(self):
        with self.assertRaises(V.StepProblem) as cm:
            V.normalize_step(nulls(), TABLES, "db")
        e = cm.exception
        self.assertEqual((e.kind, e.field, e.received), ("falta_argumento", "tabla", "null"))
        self.assertNotIn("no existe", str(e), "«null» no es una tabla inexistente: es un valor ausente")
        self.assertIn("«tabla» llegó como el texto «null»", str(e))

    def test_unused_arguments_never_invalidate_the_step(self):
        step, notes = V.normalize_step(nulls(tabla="ops"), TABLES, "db")
        self.assertEqual({k: step.get(k) for k in V.ARG_KEYS if k in step}, {"tabla": "ops"})
        step, _ = V.normalize_step(nulls(herramienta="perfil", tabla="ops", columna="None"), TABLES, "db")
        self.assertNotIn("columna", step, "perfil sin columna es válido: cuenta filas")
        step, _ = V.normalize_step(nulls(herramienta="sql", sql="SELECT 1", tabla="N/A"), TABLES, "db")
        self.assertEqual(step["sql"], "SELECT 1")
        self.assertNotIn("tabla", step)
        step, notes = V.normalize_step({"pregunta": "p", "herramienta": "muestra", "tabla": "ops",
                                        "columna": "precio"}, TABLES, "db")
        self.assertIn("se ignoraron argumentos que «muestra» no usa: columna", notes)

    def test_placeholders_in_required_fields_are_reported_precisely(self):
        for tool, over, field in (("frecuentes", {"tabla": "ops"}, "columna"),
                                  ("relacion", {"tabla": "ops", "columna": "id", "columna_destino": "id"}, "tabla_destino"),
                                  ("sql", {}, "sql")):
            with self.assertRaises(V.StepProblem) as cm:
                V.normalize_step(nulls(herramienta=tool, **over), TABLES, "db")
            self.assertEqual(cm.exception.field, field, tool)
            self.assertEqual(cm.exception.kind, "falta_argumento")

    def test_real_names_are_never_placeholders(self):
        tables = {"null": {"columnas": [{"nombre": "na"}]}, "tabla": {"columnas": [{"nombre": "none"}]}}
        step, _ = V.normalize_step({"pregunta": "p", "herramienta": "frecuentes", "tabla": "null", "columna": "na"},
                                   tables, "db")
        self.assertEqual((step["tabla"], step["columna"]), ("null", "na"))
        step, _ = V.normalize_step({"pregunta": "p", "herramienta": "perfil", "tabla": "tabla", "columna": "none"},
                                   tables, "db")
        self.assertEqual((step["tabla"], step["columna"]), ("tabla", "none"))

    def test_signature_identifies_repeated_errors(self):
        sigs = set()
        for _ in range(2):
            try:
                V.normalize_step(nulls(), TABLES, "db")
            except V.StepProblem as e:
                sigs.add(e.signature)
        self.assertEqual(len(sigs), 1)


class SchemaTests(unittest.TestCase):
    def explorer(self, tables):
        ex = Explorer.__new__(Explorer)
        ex.schema = {"tablas": tables}
        return ex

    def test_stage_a_schema_offers_only_real_names(self):
        schema = self.explorer(TABLES)._decide_schema()
        self.assertEqual(schema["properties"]["tabla"]["enum"], ["ops", "eventos"])
        self.assertEqual(schema["properties"]["tabla_destino"]["enum"], ["ops", "eventos"])
        self.assertNotIn("enum", schema["properties"]["columna"], "0.4.4: columnas como texto (el enum pesaba ~500 tokens)")
        self.assertNotIn("tabla", schema["required"], "elegir tabla no es obligatorio: depende de la herramienta")

    def test_strict_schema_requires_the_tools_arguments(self):
        schema = self.explorer(TABLES)._strict_schema("frecuentes")
        self.assertEqual(set(schema["required"]), {"pregunta", "por_que", "herramienta", "tabla", "columna"})
        self.assertNotIn("sql", schema["properties"])
        self.assertEqual(schema["properties"]["herramienta"]["enum"], ["frecuentes", "terminar"])
        self.assertEqual(schema["properties"]["tabla"]["enum"], ["ops", "eventos"], "el modelo sigue eligiendo")

    def test_huge_databases_fall_back_to_free_text(self):
        many = {f"t{i}": {"columnas": [{"nombre": "id"}]} for i in range(200)}
        schema = self.explorer(many)._decide_schema()
        self.assertNotIn("enum", schema["properties"]["tabla"])


@unittest.skipUnless(mariadb_fixture.available(), "MariaDB no instalado")
class Caverna01NullTests(Base):
    def decisions(self):
        return [r for r in self.fake.requests if "ETAPA A" in r["messages"][-1]["content"]
                or "NO se ejecutó" in r["messages"][-1]["content"]]

    def agent_queries(self):
        return self.app.db.all("SELECT sql_text, status FROM explore_queries WHERE mission_id=? AND origin='agente'",
                               (self.mid,))

    def test_null_in_unused_fields_executes_without_repairs(self):
        # Lo más probable en la prueba real: tabla elegida y los demás campos con «null».
        script = Script([lambda p, i: nulls(tabla=i["tables"][0]),
                         lambda p, i: nulls(herramienta="terminar")], report=good_report)
        m, run = self.run_script(script)
        q = self.agent_queries()
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["status"], "ok")
        steps = [s for s in self.steps() if s["status"] != "invalida"]
        self.assertEqual(steps[0]["repairs"], 0)
        self.assertEqual(json.loads(steps[0]["args_json"]), {"tabla": script.info["tables"][0]})
        self.assertEqual(json.loads(run["metrics_json"])["respuestas_invalidas"], 0)

    def test_null_table_is_repaired_by_the_model_not_by_the_system(self):
        chosen = {}

        def strict_choice(p, i):
            chosen["t"] = i["tables"][-1]  # el modelo (simulado) elige; el sistema no
            return nulls(tabla=chosen["t"])

        script = Script([lambda p, i: nulls(), lambda p, i: nulls(), strict_choice,
                         lambda p, i: nulls(herramienta="terminar")], report=good_report)
        m, run = self.run_script(script)
        dec = self.decisions()
        first_schema = dec[0]["format"]
        self.assertEqual(set(first_schema["properties"]["tabla"]["enum"]), set(script.info["tables"]),
                         "la gramática solo admite tablas reales")
        repair1 = dec[1]["messages"][-1]["content"]
        self.assertIn("«tabla» llegó como el texto «null»", repair1)
        self.assertIn("es un texto de relleno", repair1)
        self.assertIn("la que vos elijas", repair1)
        self.assertNotIn("MISMO objeto", repair1, "no se pide copiar la respuesta que tenía el error")
        self.assertNotIn("no existe", repair1)
        repair2 = dec[2]
        self.assertIn("repetiste el mismo error", repair2["messages"][-1]["content"])
        self.assertIn("tabla", repair2["format"]["required"], "la reparación repetida usa el esquema estricto")
        self.assertEqual(repair2["format"]["properties"]["herramienta"]["enum"], ["muestra", "terminar"])
        q = self.agent_queries()
        self.assertEqual(len(q), 1)
        self.assertIn(chosen["t"], q[0]["sql_text"], "se lee la tabla que eligió el modelo")
        step = [s for s in self.steps() if s["status"] != "invalida"][0]
        self.assertEqual(step["repairs"], 2)
        tries = json.loads(step["context_json"])["respuestas_modelo"]
        self.assertEqual([t["esquema"] for t in tries], ["normal", "normal", "estricto"])
        self.assertTrue(all('"null"' in t["raw"] for t in tries), "las respuestas crudas quedan para inspección")
        self.assertEqual(tries[0]["firma"], tries[1]["firma"])
        self.assertEqual(tries[2]["firma"], "")

    def test_identical_errors_stop_instead_of_looping(self):
        script = Script([lambda p, i: nulls()] * 20, report=good_report)
        m, run = self.run_script(script, limits={"max_steps": 10})
        self.assertEqual(run["ended_reason"], "respuestas_repetidas")
        inv = [s for s in self.steps() if s["status"] == "invalida"]
        self.assertEqual(len(inv), 2, "dos pasos seguidos con el mismo error bastan")
        self.assertLessEqual(len(self.decisions()), 6, "como máximo 3 pedidos por paso, sin bucle")
        self.assertEqual(self.agent_queries(), [], "el sistema no ejecuta nada en nombre del modelo")
        tries = json.loads(inv[0]["context_json"])["respuestas_modelo"]
        self.assertEqual(len(tries), 3)
        self.assertTrue(any("mismo error" in r for r in json.loads(run["reasons_json"])))
        self.assertNotEqual(run["quality"], "cumplido")


if __name__ == "__main__":
    unittest.main()
