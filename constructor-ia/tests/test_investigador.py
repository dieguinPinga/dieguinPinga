"""Investigador experimental (0.5): pruebas SINTÉTICAS de la arquitectura.

Comprueban que el ciclo funciona y que sigue siendo seguro: el motor no inventa, el SQL pasa por el intermediario de
solo lectura, la siguiente pregunta se formula DESPUÉS de recibir la evidencia real, la dependencia declarada se
distingue de la comprobable, la memoria se recupera, y una investigación interrumpida se reanuda sin perder el hilo.

NO demuestran inteligencia autónoma: aquí el «modelo» es un guion que lee el mensaje que recibe. La capacidad de
investigar solo se valida con Qwen3 real en la VivoBook.
"""
import json
import re
import time
import unittest

from constructor_ia.explorer import sqlguard
from constructor_ia.explorer.mapstore import MapStore
from constructor_ia.investigador import contract as C
from constructor_ia.investigador import dependency as DEP
from constructor_ia.investigador.engine import Clarification, Engine, sql_literal

from . import mariadb_fixture
from .helpers import wait_for
from .test_explorer import Cave, ExplorerAgentTests

SCHEMA = {"tablas": {
    "p": {"tipo": "tabla", "filas_estimadas": 40, "columnas": [
        {"nombre": "id", "tipo": "int(11)"}, {"nombre": "etiqueta", "tipo": "varchar(40)"},
        {"nombre": "valor", "tipo": "decimal(10,2)"}]},
    "c": {"tipo": "tabla", "filas_estimadas": 300, "columnas": [
        {"nombre": "id", "tipo": "int(11)"}, {"nombre": "p_ref", "tipo": "int(11)"},
        {"nombre": "momento", "tipo": "datetime"}, {"nombre": "cantidad", "tipo": "int(11)"}]}}}


def engine():
    return Engine(None, SCHEMA, None, None, "x", "base", 100)


def med(que, *objetos, filtro=None, sql=None):
    return {"que": que, "objetos": list(objetos), "filtro": filtro, "sql": sql, "remedir": False}


class EngineNeverInventsTests(unittest.TestCase):
    def test_exact_names_only(self):
        e = engine()
        self.assertEqual(e.resolve("`P`.`Etiqueta`"), ("p", "etiqueta"))
        self.assertEqual(e.resolve("base.p.valor"), ("p", "valor"))
        self.assertEqual(e.resolve("cantidad"), ("c", "cantidad"), "una columna de UNA sola tabla no es ambigua")
        with self.assertRaises(Clarification) as cm:
            e.resolve("etiquta")
        self.assertIn("p.etiqueta", cm.exception.options, "se ofrecen nombres reales; no se elige por parecido")
        with self.assertRaises(Clarification) as cm:
            e.resolve("id")
        self.assertEqual(set(cm.exception.options), {"p.id", "c.id"})

    def test_incompatible_requests_return_compact_clarifications(self):
        e = engine()
        cases = [(med("distribucion", "p"), "necesita una columna de p", "p.etiqueta"),
                 (med("tendencia", "c.cantidad"), "no es una columna de fecha", "c.momento"),
                 (med("relacion", "c.p_ref"), "DOS objetos", None),
                 (med("rango", "p.valor", "c.cantidad"), "UNA tabla", "p"),
                 (med("ejemplos", "p", filtro={"objeto": "c.cantidad", "valor": "3"}), "columna de p", "p.valor"),
                 (med("conteo"), "al menos un objeto", None),
                 (med("sql"), "no escribiste la consulta", None),
                 (med("sql", sql="SELECT * FROM inexistente"), "no se puede ejecutar", None)]
        for m, text, option in cases:
            with self.assertRaises(Clarification, msg=str(m)) as cm:
                e.plan(m)
            self.assertIn(text, cm.exception.text, m)
            if option:
                self.assertIn(option, cm.exception.options, m)
        self.assertNotIn("p", [o for o in e.plan(med("tendencia", "c.momento"))["columnas"]])

    def test_plans_are_verifiable_sql(self):
        e = engine()
        p = e.plan(med("distribucion", "p.etiqueta", filtro={"objeto": "p.valor", "valor": "12.50"}))
        self.assertEqual(p["sql"], "SELECT v AS valor, COUNT(*) AS veces FROM (SELECT `etiqueta` AS v FROM `p` WHERE "
                                   "`valor` = 12.50 LIMIT 100) s GROUP BY v ORDER BY veces DESC LIMIT 15")
        r = e.plan(med("relacion", "c.p_ref", "p.id"))
        self.assertIn("FROM `p` d WHERE d.`id` = s.v", r["sql"])
        for m in (med("conteo", "p"), med("rango", "p.valor"), med("ejemplos", "p", "p.etiqueta"),
                  med("tendencia", "c.momento"), med("sql", sql="SELECT COUNT(*) FROM p")):
            sql = e.plan(m)["sql"]
            self.assertEqual(sqlguard.check(sql), sql, m)  # aceptada tal cual por el filtro de solo lectura

    def test_filter_values_are_literals(self):
        evil = "x' OR 1=1; DROP TABLE p; -- "
        self.assertEqual(sql_literal(evil), "'x'' OR 1=1; DROP TABLE p; -- '")
        self.assertEqual(sql_literal("a\\' OR 1=1"), "'a\\\\'' OR 1=1'")
        self.assertEqual(sql_literal("-12.5"), "-12.5")
        sql = engine().plan(med("ejemplos", "p", filtro={"objeto": "p.etiqueta", "valor": evil}))["sql"]
        self.assertEqual(sqlguard.check(sql), sql, "un único SELECT de solo lectura: el «;» queda dentro del literal")


class DependencyTests(unittest.TestCase):
    prev = {"refs": ["C7", "F20", "D3"], "rows": [["nombre 7", 1], ["nombre 9", 1], ["123.07", 3]]}

    def req(self, valor=None, deps=(), sql=None):
        return {"pregunta": "q", "depende_de": list(deps),
                "medir": {"que": "ejemplos", "objetos": ["p"], "sql": sql,
                          "filtro": {"objeto": "p.etiqueta", "valor": valor} if valor else None}}

    def test_declared_is_not_verifiable(self):
        d = DEP.assess(self.prev, self.req(deps=["C7"]), "contexto previo")
        self.assertEqual((d["tipo"], d["declarada"], d["comprobable"]), ("declarada", True, False))
        d = DEP.assess(self.prev, self.req(deps=["F1"]), "")
        self.assertEqual(d["tipo"], "ninguna", "citar un hecho viejo no es depender del ciclo anterior")

    def test_new_value_from_the_data_is_verifiable(self):
        d = DEP.assess(self.prev, self.req("nombre 7"), "esquema: p(etiqueta) y C17")
        self.assertEqual((d["tipo"], d["anclas"]), ("comprobable", ["nombre 7"]))
        d = DEP.assess(self.prev, self.req(sql="SELECT * FROM p WHERE valor = 123.07"), "")
        self.assertEqual(d["anclas"], ["123.07"])
        self.assertTrue(DEP.assess(self.prev, self.req("nombre 7", deps=["C7"]), "")["declarada"])

    def test_values_known_before_the_evidence_do_not_count(self):
        d = DEP.assess(self.prev, self.req("nombre 7"), "LO QUE YA SE SABE: F3 nombre 7 aparece")
        self.assertFalse(d["comprobable"], "ya estaba en su contexto: no prueba que la evidencia cambió la decisión")
        self.assertFalse(DEP.assess(self.prev, self.req("nombre 8"), "")["comprobable"], "no está en los datos")
        self.assertEqual(DEP.assess(None, self.req("nombre 7"), "")["tipo"], "inicial")

    def test_reuse_needs_the_value_not_only_the_citation(self):
        facts = {"F20": {"respaldada": True, "valores": {"nombre 7", "123.07"}},
                 "F21": {"respaldada": False, "valores": {"nombre 7"}}}
        self.assertEqual(DEP.reuse(self.req("nombre 7", deps=["F20", "F21"]), facts),
                         [{"hecho": "F20", "declarada": True, "comprobable": True}])
        self.assertEqual(DEP.reuse(self.req("otro", deps=["F20"]), facts),
                         [{"hecho": "F20", "declarada": True, "comprobable": False}])


class ContractTests(unittest.TestCase):
    def test_normalization(self):
        self.assertIsNone(C.normalize_pedido({"pregunta": "x", "medir": {"que": "inventado"}}))
        self.assertIsNone(C.normalize_pedido({"medir": {"que": "conteo"}}))
        p = C.normalize_pedido({"pregunta": " ¿Cuántas? ", "medir": {"que": "conteo", "objetos": ["p", ""]},
                                "depende_de": ["c7", "x", "D2", "C7"]})
        self.assertEqual((p["pregunta"], p["medir"]["objetos"], p["depende_de"]), ("¿Cuántas?", ["p"], ["C7", "D2"]))
        names = ["p", "p.etiqueta"]
        self.assertEqual(C.lectura_schema(names)["properties"]["siguiente"]["properties"]["medir"]["properties"]
                         ["objetos"]["items"]["enum"], names, "la gramática solo admite nombres reales")


# --- Con MariaDB y el agente completo ------------------------------------------------------------------

def data_block(prompt: str) -> str:
    return prompt.split("<<<DATOS NO CONFIABLES", 1)[1].split("<<<FIN DE DATOS>>>", 1)[0] if "<<<DATOS" in prompt else ""


def cycle_no(prompt: str) -> int | None:
    m = re.search(r"TU PREGUNTA \(ciclo (\d+)\)", prompt)
    return int(m.group(1)) if m else None


def pedido(pregunta, que, *objetos, filtro=None, deps=(), sql=None, remedir=False):
    return {"pregunta": pregunta, "porque": "quiero saberlo", "depende_de": list(deps),
            "medir": {"que": que, "objetos": list(objetos), **({"filtro": filtro} if filtro else {}),
                      **({"sql": sql} if sql else {}), **({"remedir": True} if remedir else {})}}


@unittest.skipUnless(mariadb_fixture.available(), "MariaDB no instalado")
class InvestigatorTests(ExplorerAgentTests):
    test_explores_unknown_database_and_builds_map = None
    test_continue_detects_schema_changes = None
    test_stop_immediately = None
    test_blocked_connection_fails_closed = None

    def setUp(self):
        self.cave = Cave(self.srv)
        self.app, self.fake, self.spec = self.make(self.cave, [])

    def investigate(self, model, **spec):
        self.fake.default = lambda body: json.dumps(r) if not isinstance(r := model(body["messages"][-1]["content"]),
                                                                          str) else r
        mid = self.app.missions.create({"title": "Investigación", "description": "Investigá esta base por iniciativa "
                                        "propia.", "spec": {"mode": "investigacion", "conn": "cueva", "max_ciclos": 4,
                                                            "max_minutos": 5, **spec},
                                        "tests_py": "", "max_attempts": 1}, "qwen3:4b-instruct", "diego")
        self.app.mission_worker.notify()
        wait_for(lambda: self.app.missions.get(mid)["state"] in ("completada", "fallida", "cancelada"), timeout=90)
        self.mid = mid
        return mid

    def session(self, mid=None):
        r = self.app.db.one("SELECT * FROM inv_sessions WHERE mission_id=?", (mid or self.mid,))
        return dict(r) | {"state": json.loads(r["state_json"]), "metrics": json.loads(r["metrics_json"] or "null")}

    def cycles(self, mid=None):
        from constructor_ia.investigador.memory import InvMemory
        return InvMemory(self.app.db, MapStore(self.app.db), "cueva").cycles(mid or self.mid)

    def prompts(self):
        return [r["messages"][-1]["content"] for r in self.fake.requests]

    def chain_model(self, p):
        """Un «investigador» de guion que DECIDE leyendo la evidencia que recibe (no conoce los datos de antemano)."""
        notes = {}

        def model(prompt):
            if "ELEGÍ TU PRÓXIMA PREGUNTA" in prompt:
                return pedido("¿Qué etiquetas hay en la tabla padre?", "distribucion", f"{p}.etiqueta")
            asked = re.search(r"TU PREGUNTA \(ciclo \d+\): «([^»]*)»", prompt).group(1)
            n = {"¿Qué etiquetas hay en la tabla padre?": 1}.get(asked) or (
                2 if asked.startswith("¿Qué valor tiene la fila") else 3 if asked.startswith("¿Cuántas filas tienen")
                else 4)
            q = re.search(r"EVIDENCIA (C\d+)", prompt).group(1)
            rows = data_block(prompt)
            if n == 1:
                v = re.search(r"nombre \d+", rows).group(0)
                notes["v"] = v
                return {"lectura": "Cada etiqueta aparece una vez en lo observado.",
                        "afirmaciones": [{"texto": f"En la muestra, «{v}» es una de las etiquetas.", "evidencia": [q],
                                          "alcance": "muestra"}],
                        "dudas": [f"¿Qué valor tiene la fila «{v}»?"],
                        "siguiente": pedido(f"¿Qué valor tiene la fila con etiqueta {v}?", "ejemplos", p,
                                            filtro={"objeto": f"{p}.etiqueta", "valor": v}, deps=[q])}
            if n == 2:
                if "v" not in notes:  # al reanudar, el guion lo toma de la pregunta en curso
                    notes["v"] = re.search(r"con etiqueta (.+)\?$", asked).group(1)
                row = next(ln for ln in rows.splitlines() if notes["v"] in ln)
                valor = row.split("|")[-1].strip()
                notes["valor"] = valor
                d = re.search(r"D(\d+): ¿Qué valor tiene", prompt)
                return {"lectura": f"La fila tiene valor {valor}.",
                        "afirmaciones": [{"texto": f"La fila con etiqueta «{notes['v']}» tiene valor {valor}.",
                                          "evidencia": [q], "alcance": "subconjunto"}],
                        "cierra": [f"D{d.group(1)}"] if d else [],
                        "siguiente": pedido(f"¿Cuántas filas tienen valor {valor}?", "conteo", p,
                                            filtro={"objeto": f"{p}.valor", "valor": valor}, deps=[q])}
            if n == 3:
                if "valor" not in notes:
                    notes["valor"] = re.search(r"tienen valor ([\d.]+)", asked).group(1)
                f = re.search(r"F(\d+) \[inferencia[^\]]*\] La fila con etiqueta", prompt)
                notes["f"] = f"F{f.group(1)}" if f else None
                return {"lectura": "Hay una sola fila con ese valor.", "afirmaciones": [],
                        "siguiente": pedido("¿Qué otras columnas tiene esa fila?", "ejemplos", p,
                                            filtro={"objeto": f"{p}.valor", "valor": notes["valor"]},
                                            deps=[notes["f"]] if notes["f"] else [])}
            return {"lectura": "Sigo.", "siguiente": pedido("¿Y la tabla hija?", "conteo", self.cave.child)}
        return model, notes

    def test_cycle_chain_with_verifiable_dependencies_and_reuse(self):
        p = self.cave.parent
        maps = MapStore(self.app.db)
        hist = maps.record_note("cueva", "hallazgo", "Hecho histórico de una expedición anterior", evidence=[],
                                mission_id="M-vieja", certainty="inferencia")[0]
        # El modelo va a repetir LITERALMENTE una afirmación histórica (cualquiera sea la etiqueta que lea): no debe
        # confirmarla, subirle la certeza ni tocarla.
        same = [maps.record_note("cueva", "hallazgo", f"En la muestra, «nombre {i}» es una de las etiquetas.",
                                 evidence=[], mission_id="M-vieja", certainty="hipotesis")[0] for i in range(1, 41)]
        before = [tuple(r) for r in self.app.db.all("SELECT * FROM explore_facts ORDER BY id")]
        model, notes = self.chain_model(p)
        self.investigate(model)
        prompts = self.prompts()
        self.assertEqual(sum("ELEGÍ TU PRÓXIMA PREGUNTA" in x for x in prompts), 1, "una sola pregunta inicial")
        self.assertEqual(sum("TU PREGUNTA (ciclo" in x for x in prompts), 4, "una lectura por ciclo, nada más")
        self.assertEqual(len(prompts), 5, "sin reparaciones, informes ni aplicaciones")
        for x in prompts[1:]:
            self.assertIn("EVIDENCIA C", x, "la siguiente pregunta se formula con la evidencia real delante")
        cy = self.cycles()
        self.assertEqual([c["origin"] for c in cy], ["pedido", "lectura", "lectura", "lectura"])
        self.assertEqual([c["dependency"]["tipo"] for c in cy], ["inicial", "comprobable", "comprobable", "ninguna"])
        self.assertEqual(cy[1]["dependency"]["anclas"], [notes["v"]])
        self.assertEqual(cy[2]["dependency"]["anclas"], [notes["valor"]])
        self.assertTrue(cy[1]["dependency"]["declarada"])
        self.assertEqual(cy[3]["reuse"], [{"hecho": notes["f"], "declarada": True, "comprobable": True}])
        # Hechos calculados separados de las interpretaciones
        f1 = MapStore(self.app.db).facts("cueva", limit=500)
        computed = next(f for f in f1 if f"F{f['id']}" == cy[0]["computed"][0]["id"])
        self.assertEqual((computed["kind"], computed["certainty"]), ("estadistica", "comprobado"))
        self.assertTrue(computed["subject"].startswith("inv:frec:"))
        claim = next(x for x in cy[1]["claims"])
        self.assertEqual((claim["certeza"], claim["verificacion"]), ("inferencia", "respaldada"))
        self.assertEqual(cy[1]["doubts"]["cerradas"], cy[0]["doubts"]["abiertas"], "la duda del ciclo 1 se cerró")
        m = self.session()["metrics"]
        self.assertTrue(m["criterios"]["cumplidos"], m)
        self.assertEqual((m["max_transiciones_comprobables_seguidas"], m["conclusiones_reutilizadas_comprobables"]),
                         (2, 1))
        self.assertGreater(m["tokens_prompt"], 0)
        self.assertEqual(m["llamadas"], 5)
        self.assertTrue(self.session()["state"]["pending"], "la siguiente pregunta queda guardada para reanudar")
        # Nada histórico cambió; ninguna reevaluación se aplicó; ni informe ni aplicaciones.
        after = {r[0]: tuple(r) for r in self.app.db.all("SELECT * FROM explore_facts ORDER BY id")}
        self.assertEqual([after[r[0]] for r in before], before)
        self.assertTrue(any(r[0] == hist for r in before))
        self.assertNotIn(int(cy[0]["claims"][0]["id"][1:]), same, "la afirmación nueva vive aparte de la histórica")
        self.assertEqual(cy[0]["claims"][0]["certeza"], "inferencia")
        self.assertEqual(self.app.db.one("SELECT COUNT(*) FROM explore_reviews")[0], 0)
        self.assertFalse([f for f in f1 if f["kind"] == "oportunidad"])
        files = {r[0] for r in self.app.db.all("SELECT rel_path FROM files WHERE mission_id=?", (self.mid,))}
        self.assertEqual(files, {"investigacion/ciclos.json", "investigacion/metricas.json", "investigacion/resumen.md"})

    def test_ambiguous_request_gets_a_clarification_and_nothing_is_executed(self):
        p = self.cave.parent

        def model(prompt):
            if "ELEGÍ TU PRÓXIMA PREGUNTA" in prompt:
                return pedido("¿Qué etiquetas hay?", "distribucion", "etiquta")  # nombre mal escrito
            if "no se pudo medir sin inventar" in prompt:
                self.assertIn(f"{p}.etiqueta", prompt, "la aclaración trae los nombres reales")
                return {"lectura": "Me equivoqué de nombre.", "siguiente": pedido("¿Qué etiquetas hay?",
                                                                                   "distribucion", f"{p}.etiqueta")}
            return {"lectura": "Listo.", "siguiente": pedido("¿Cuántas filas?", "conteo", p)}
        self.investigate(model, max_ciclos=2)
        cy = self.cycles()
        self.assertEqual([c["status"] for c in cy], ["aclaracion", "completo"])
        self.assertEqual(cy[0]["query_ids"], [], "no se ejecutó nada por el pedido ambiguo")
        self.assertIn("no existe en el esquema", cy[0]["evidence"]["aclaracion"])
        self.assertEqual(self.app.db.one("SELECT COUNT(*) FROM explore_queries WHERE mission_id=? AND origin="
                                         "'investigador'", (self.mid,))[0], 1)

    def test_security_controls_still_apply(self):
        p = self.cave.parent
        before = self.cave.checksum()
        steps = iter([pedido("¿Puedo borrar?", "sql", sql=f"DELETE FROM `{p}`"),
                      pedido("¿Inyección?", "ejemplos", p, filtro={"objeto": f"{p}.etiqueta",
                                                                  "valor": f"x' OR 1=1; DROP TABLE `{p}`; -- "}),
                      pedido("¿Otra base?", "sql", sql="SELECT * FROM mysql.user"),
                      pedido("¿Cuántas filas?", "conteo", p)])

        def model(prompt):
            return {"lectura": "Veo el resultado.", "siguiente": next(steps)} if "TU PREGUNTA" in prompt else next(steps)
        self.investigate(model, max_ciclos=3)
        cy = self.cycles()
        self.assertEqual(cy[0]["evidence"]["estado"], "rechazada", "el intermediario rechaza escrituras")
        self.assertEqual((cy[1]["evidence"]["estado"], cy[1]["evidence"]["filas_total"]), ("ok", 0))
        self.assertEqual(cy[2]["status"], "aclaracion")
        self.assertEqual(self.cave.checksum(), before, "la base explorada no cambió")

    def test_memory_is_recovered_and_known_evidence_reused(self):
        p = self.cave.parent

        def first(prompt):
            if "ELEGÍ" in prompt:
                return pedido("¿Qué etiquetas hay?", "distribucion", f"{p}.etiqueta")
            return {"lectura": "Etiquetas únicas.", "dudas": ["¿Las etiquetas siguen un patrón?"],
                    "siguiente": pedido("¿Cuántas filas?", "conteo", p)}
        a = self.investigate(first, max_ciclos=1)
        qa = self.app.db.one("SELECT id FROM explore_queries WHERE mission_id=? AND origin='investigador'", (a,))[0]
        self.fake.requests.clear()

        def second(prompt):
            if "ELEGÍ" in prompt:
                second.brief = prompt
                return pedido("¿Qué etiquetas hay?", "distribucion", f"{p}.etiqueta")
            if cycle_no(prompt) == 1:
                return {"lectura": "Ya lo sabía.", "siguiente": pedido("¿Cambió?", "distribucion", f"{p}.etiqueta",
                                                                       remedir=True)}
            return {"lectura": "Igual.", "siguiente": pedido("¿Cuántas?", "conteo", p)}
        self.investigate(second, max_ciclos=2)
        self.assertIn(f"C{qa}", second.brief.split("YA MEDIDO")[1].split("\n\n")[0], "lo ya medido se informa")
        self.assertRegex(second.brief.split("DUDAS ABIERTAS")[1], r"D\d+: ¿Las etiquetas siguen un patrón\?")
        cy = self.cycles()
        self.assertEqual(cy[0]["evidence"]["reutilizada"]["de"], f"C{qa}", "se entrega lo ya medido en vez de repetir")
        self.assertIn("YA MEDIDO antes", self.prompts()[1])
        self.assertIsNone(cy[1]["evidence"]["reutilizada"], "«remedir» vuelve a medir")
        n_new = self.app.db.one("SELECT COUNT(*) FROM explore_queries WHERE mission_id=? AND origin='investigador'",
                                (self.mid,))[0]
        self.assertEqual(n_new, 1, "solo la medición pedida otra vez a propósito")
        self.assertEqual(self.session()["metrics"]["evidencia_reutilizada"], 1)

    def test_invalid_answers_get_one_short_retry_and_no_loop(self):
        self.investigate(lambda prompt: "esto no es JSON")
        m = self.session()["metrics"]
        self.assertEqual(m["finalizacion"], "respuestas_invalidas")
        self.assertEqual(len(self.fake.requests), 4, "2 pedidos × (respuesta + 1 reintento), sin cadenas de reparación")
        self.assertEqual(m["fallos_modelo"], 2)
        self.assertIn("no devolvió una respuesta válida", m["donde_fallo"])
        self.assertIn("Consumo total: 4 llamadas", m["donde_fallo"])

    def test_interrupt_mid_cycle_and_resume_keeps_the_thread(self):
        p = self.cave.parent
        model, notes = self.chain_model(p)

        def interrupting(prompt):
            if "¿Qué valor tiene la fila" in prompt.split("TU PREGUNTA", 1)[-1][:120] and not interrupting.done:
                interrupting.done = True
                self.app.mission_worker.cancel_current(self.app.mission_worker.current)
                return "x" * 2000  # se corta mientras llega: la evidencia del ciclo 2 queda sin leer
            return model(prompt)
        interrupting.done = False
        self.fake.delay = 0.01
        a = self.investigate(interrupting)
        self.assertEqual(self.app.missions.get(a)["state"], "cancelada")
        sa = self.session(a)
        self.assertEqual(sa["status"], "interrumpida")
        ca = self.cycles(a)
        self.assertEqual([c["status"] for c in ca], ["completo", "evidencia"], "la evidencia del ciclo 2 quedó guardada")
        self.assertEqual(sa["state"]["read_pending"], ca[1]["id"])
        self.fake.delay = 0.0
        self.fake.requests.clear()
        b = self.investigate(model, reanudar=a, max_ciclos=2)
        prompts = self.prompts()
        self.assertNotIn("ELEGÍ TU PRÓXIMA PREGUNTA", "".join(prompts), "se retoma sin una pregunta inicial nueva")
        self.assertIn(f"«{notes['v']}»", prompts[0], "primero se lee la evidencia que había quedado sin leer")
        ca2 = self.cycles(a)
        self.assertEqual(ca2[1]["status"], "completo")
        cb = self.cycles(b)
        self.assertEqual(cb[0]["origin"], "lectura")
        self.assertEqual(cb[0]["parent_id"], ca2[1]["id"], "el hilo continúa desde el último ciclo interrumpido")
        self.assertEqual(cb[0]["line_id"], ca2[0]["line_id"])
        self.assertEqual(cb[0]["dependency"]["tipo"], "comprobable", "la dependencia cruza la interrupción")
        self.assertEqual(self.session(a)["status"], "reanudada")
        self.assertEqual(self.session(a)["resumed_by"], b)
        self.assertEqual(self.session(b)["resumed_from"], a)


if __name__ == "__main__":
    unittest.main()
