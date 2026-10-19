"""Regresiones de M20261019-4c8c37 (0.4.7, Qwen3 4B real) — recuperación autónoma y eficiencia, v0.4.8.

Observado: 14 minutos, 0 consultas exploratorias, 0 ciclos y ningún conocimiento nuevo.
* El modelo intentó repetir C59 (perfil) y C61 (frecuentes), consultas de una expedición anterior.
* Después alternó entre `relacion` sin sus argumentos obligatorios y `frecuentes` mientras declaraba buscar una
  relación entre tablas. Repitió el error y la misión terminó por «respuestas_repetidas».
* Luego dedicó varios minutos a un informe y tres propuestas de aplicaciones sin haber ejecutado ninguna consulta.

0.4.8: el intento que no se puede reparar queda registrado como intento fallido y el agente sigue con otra pregunta;
la reparación muestra la contradicción y las formas de resolverla (incluida «abandonar»); el mismo atasco se
reconoce aunque cambie la herramienta; las reparaciones tienen límites de tiempo y de generación; y sin consultas con
resultados el cierre es un diagnóstico breve del sistema, sin otra llamada larga a Ollama. Nada se elige por el
modelo: ni tablas, ni preguntas, ni objetivos.

Bases ficticias (nombres aleatorios): `etiqueta` de la tabla padre cumple el papel de la columna de C59/C61.
"""
import json
import time
import unittest

from constructor_ia.explorer.mapstore import MapStore

from . import mariadb_fixture
from .helpers import wait_for
from .test_explorer_regresion import Base, Script, good_report


def D(herramienta, pregunta, busca, **kw):
    return {"pregunta": pregunta, "por_que": "investigar", "evidencia": [], "herramienta": herramienta, "busca": busca,
            **kw}


END = {"pregunta": "fin", "por_que": "listo", "evidencia": [], "herramienta": "terminar", "busca": "otro"}
REL_Q = "¿Cómo se relaciona la tabla hija con la tabla padre?"


def real_sequence(p, c):
    """Las respuestas de la etapa A de M20261019-4c8c37, en el mismo orden (con nombres sintéticos)."""
    return [
        # paso 1: repetir C59 y C61; luego relacion sin sus argumentos obligatorios
        lambda pr, i: D("perfil", "¿Qué valores distintos tiene etiqueta?", "valores_distintos", tabla=p,
                        columna="etiqueta", columna_objetivo="etiqueta"),
        lambda pr, i: D("frecuentes", "¿Qué valores de etiqueta son más frecuentes?", "frecuencias", tabla=p,
                        columna="etiqueta", columna_objetivo="etiqueta"),
        lambda pr, i: D("relacion", REL_Q, "relacion", tabla=c),
        # paso 2: frecuentes para una relación, relacion sin argumentos, y otra vez el mismo error
        lambda pr, i: D("frecuentes", REL_Q, "relacion", tabla=c, columna=f"{p}_ref"),
        lambda pr, i: D("relacion", REL_Q, "relacion", tabla=c),
        lambda pr, i: D("relacion", REL_Q, "relacion", tabla=c),
    ]


def report_with_apps(pr, i):
    """El informe largo con tres propuestas que 0.4.7 pedía aunque no hubiera consultas."""
    t = i["tables"][0]
    return {"resumen": "Informe.", "descubrimientos": [], "incertidumbres": [], "aplicaciones": [
        {"nombre": f"App {k}", "tipo": "tablero", "descripcion": "d", "consultas": [f"SELECT COUNT(*) FROM `{t}`"],
         "evidencia": []} for k in range(3)]}


@unittest.skipUnless(mariadb_fixture.available(), "MariaDB no instalado")
class RecoveryTests(Base):
    def setUp(self):
        super().setUp()
        self.app, self.fake, self.spec = self.make(self.cave, [])
        p = self.cave.parent
        # Expedición anterior: deja C59 (perfil) y C61 (frecuentes) en la memoria, como en la base real.
        self.mission(Script([lambda pr, i: D("perfil", "¿Valores de etiqueta?", "valores_distintos", tabla=p,
                                             columna="etiqueta", columna_objetivo="etiqueta"),
                             lambda pr, i: D("frecuentes", "¿Frecuencias de etiqueta?", "frecuencias", tabla=p,
                                             columna="etiqueta", columna_objetivo="etiqueta"),
                             lambda pr, i: END], report=good_report))
        self.previous = self.mid
        self.fake.requests.clear()

    def mission(self, script, limits=None):
        self.fake.default = script
        mid = self.start_mission(self.app, {**self.spec, **(limits or {})})
        t0 = time.monotonic()
        wait_for(lambda: self.app.missions.get(mid)["state"] in ("completada", "fallida", "cancelada"), timeout=120)
        self.elapsed = time.monotonic() - t0
        self.mid = mid
        run = dict(self.app.db.one("SELECT * FROM explore_runs WHERE mission_id=?", (mid,)))
        return self.app.missions.get(mid), run, json.loads(run["metrics_json"])

    def steps(self):
        return [dict(r) for r in self.app.db.all("SELECT * FROM explore_steps WHERE mission_id=? ORDER BY id",
                                                 (self.mid,))]

    def queries(self):
        return [dict(r) for r in self.app.db.all("SELECT tool, sql_text, status, row_count FROM explore_queries WHERE"
                                                 " mission_id=? AND origin='agente' ORDER BY id", (self.mid,))]

    def contents(self, marker):
        return [r["messages"][-1]["content"] for r in self.fake.requests if marker in r["messages"][-1]["content"]]

    def test_exact_sequence_is_recorded_and_the_agent_keeps_investigating(self):
        p, c = self.cave.parent, self.cave.child
        recovered = [lambda pr, i: D("muestra", "¿Qué registros tiene la tabla hija?", "ejemplos", tabla=c),
                     lambda pr, i: D("frecuentes", "¿Qué cantidades son más frecuentes?", "frecuencias", tabla=c,
                                     columna="cantidad", columna_objetivo="cantidad"),
                     lambda pr, i: END]

        def interpret(pr, i):
            return {"resumen": "Registros reales.", "hallazgos": [
                {"texto": "En la muestra hay registros con cantidad.", "tipo": "hallazgo", "evidencia": [f"C{i['q']}"],
                 "alcance": "muestra"}]}

        script = Script(real_sequence(p, c) + recovered, report=report_with_apps, interpret=interpret)
        m, run, mt = self.mission(script)
        # La secuencia real se consumió entera y en el mismo orden; después el agente siguió investigando.
        self.assertEqual(script.i, len(real_sequence(p, c)) + len(recovered))
        self.assertEqual([q["tool"] for q in self.queries()], ["muestra", "frecuentes"],
                         "0.4.7 terminaba aquí con 0 consultas")
        self.assertEqual(run["ended_reason"], "terminado_por_el_agente")
        failed = [s for s in self.steps() if s["status"] == "invalida"]
        self.assertEqual(len(failed), 2)
        keys = [[t["atasco"] for t in json.loads(s["context_json"])["respuestas_modelo"]] for s in failed]
        self.assertEqual(keys[0], ["repetida", "repetida", "intencion:relacion"],
                         "repetir C59 con perfil y C61 con frecuentes es UN atasco, aunque cambie la herramienta")
        self.assertEqual(keys[1], ["intencion:relacion"] * 3,
                         "frecuentes para una relación y relacion sin argumentos son el MISMO atasco")
        # Ayuda concreta en las reparaciones: qué ya se sabe, la contradicción y cómo resolverla.
        repairs = self.contents("NO se ejecutó (no se gastó un paso)")
        self.assertIn("esa consulta ya se ejecutó", repairs[0])
        self.assertIn(f"en la expedición {self.previous}", repairs[0])
        self.assertIn('«herramienta»: "abandonar"', repairs[0])
        contradiction = next(r for r in repairs if "CONTRADICCIÓN" in r)
        self.assertIn("dice buscar una relación entre tablas", contradiction)
        self.assertIn("los cuatro argumentos son obligatorios", contradiction)
        self.assertIn("2) Mantener «frecuentes» y corregir «busca»", contradiction)
        self.assertTrue(any("es el MISMO atasco" in r for r in repairs))
        # El siguiente paso ve los intentos fallidos registrados y que el atasco se repitió.
        step3 = next(x for x in self.contents("ETAPA A: decidí") if "INTENTOS FALLIDOS" in x and
                     "El mismo atasco se repitió" in x)
        self.assertIn(REL_Q, step3)
        self.assertIn("no se pudo reparar", step3)
        self.assertEqual(mt["preguntas_abandonadas"], {"por_el_modelo": 0, "sin_reparacion": 2})
        self.assertEqual(mt["atascos_repetidos"], 1)
        self.assertGreaterEqual(mt["ciclos_completos"], 1)
        self.assertGreaterEqual(mt["hechos_nuevos"], 1)
        informe = self.app.files.read_rel(self.mid, "exploracion/informe.md")
        self.assertIn("## Intentos fallidos (preguntas abandonadas)", informe)
        mapa = json.loads(self.app.files.read_rel(self.mid, "exploracion/mapa.json"))
        self.assertEqual([a["paso"] for a in mapa["intentos_fallidos"]], [1, 1])
        self.assertEqual(len(self.contents("MAPA CONSTRUIDO")), 1, "con consultas con resultado sí hay informe")

    def test_the_model_can_abandon_its_question_and_ask_another(self):
        c = self.cave.child
        script = Script([
            lambda pr, i: D("frecuentes", REL_Q, "relacion", tabla=c, columna=f"{self.cave.parent}_ref"),
            lambda pr, i: {"pregunta": REL_Q, "por_que": "todavía no sé con qué columna de destino compararla",
                           "evidencia": [], "herramienta": "abandonar", "busca": "relacion"},
            lambda pr, i: D("muestra", "¿Qué registros tiene la tabla hija?", "ejemplos", tabla=c),
            lambda pr, i: END], report=good_report)
        m, run, mt = self.mission(script)
        st = self.steps()
        self.assertEqual([s["status"] for s in st], ["abandonada", "ok", "terminar"])
        self.assertEqual(st[0]["question"], REL_Q)
        self.assertIn("abandonada por el modelo: todavía no sé con qué columna", st[0]["analysis"])
        self.assertEqual(st[0]["repairs"], 1)
        self.assertEqual([q["tool"] for q in self.queries()], ["muestra"], "nada se ejecutó por la pregunta abandonada")
        self.assertIn(c, self.queries()[0]["sql_text"], "la tabla la eligió el modelo")
        self.assertEqual(mt["preguntas_abandonadas"], {"por_el_modelo": 1, "sin_reparacion": 0})
        nxt = next(x for x in self.contents("ETAPA A: decidí") if "INTENTOS FALLIDOS" in x)
        self.assertIn("la abandonaste", nxt)
        repair = self.fake.requests[1]
        self.assertIn("abandonar", repair["format"]["properties"]["herramienta"]["enum"])

    def test_without_recovery_the_close_is_a_brief_diagnosis(self):
        p, c = self.cave.parent, self.cave.child
        script = Script(real_sequence(p, c) * 3, report=report_with_apps)
        m, run, mt = self.mission(script)
        self.assertEqual(self.queries(), [])
        self.assertEqual(run["ended_reason"], "respuestas_repetidas",
                         "tres preguntas seguidas con el mismo atasco: se corta, acotado")
        self.assertEqual(len([s for s in self.steps() if s["status"] == "invalida"]), 3)
        self.assertEqual(self.contents("MAPA CONSTRUIDO"), [], "sin resultados no se pide el informe largo a Ollama")
        self.assertEqual(run["report_status"], "diagnostico")
        self.assertEqual(mt["aplicaciones_validas"], 0)
        self.assertEqual(MapStore(self.app.db).facts("cueva", kinds=("oportunidad",)),
                         [f for f in MapStore(self.app.db).facts("cueva", kinds=("oportunidad",))
                          if f["mission_id"] != self.mid], "no se proponen aplicaciones sin consultas")
        informe = self.app.files.read_rel(self.mid, "exploracion/informe.md")
        self.assertIn("## Cierre diagnóstico", informe)
        self.assertIn("sin pedirle otro informe al modelo", informe)
        self.assertIn("buscar una relación entre tablas con una herramienta o argumentos que no lo permiten", informe)
        self.assertTrue(any("cierre diagnóstico" in r for r in json.loads(run["reasons_json"])))
        self.assertEqual(len(self.fake.requests), 9, "3 pasos × (respuesta + 2 reparaciones), y nada más")

    def test_unproductive_repairs_are_bounded_in_time_and_generation(self):
        p, c = self.cave.parent, self.cave.child
        self.app.cfg.explorer_repair_budget_s = 1
        self.app.cfg.explorer_repair_num_predict = 100
        self.app.cfg.explorer_repair_timeout_s = 30
        self.fake.delay = 0.6
        bad = lambda pr, i: D("relacion", REL_Q, "relacion", tabla=c)  # noqa: E731
        script = Script([bad] * 9 + [lambda pr, i: D("muestra", "¿Qué registros hay?", "ejemplos", tabla=p),
                                     lambda pr, i: END], report=good_report)
        m, run, mt = self.mission(script)
        self.assertTrue(mt["presupuesto_de_reparaciones_agotado"])
        failed = [s for s in self.steps() if s["status"] == "invalida"]
        self.assertEqual(len(failed), 3)
        self.assertGreaterEqual(failed[0]["repairs"], 1)
        self.assertEqual([s["repairs"] for s in failed[1:]], [0, 0], "sin presupuesto, la pregunta se abandona enseguida")
        self.assertEqual(mt["reparaciones_llamadas"], failed[0]["repairs"], "agotado, no se piden más reparaciones")
        self.assertGreaterEqual(mt["reparaciones_s"], 1)
        # Una reparación se pide solo si queda presupuesto: pasado el tope no hay más pedidos de reparación.
        n_repairs = len(self.contents("NO se ejecutó (no se gastó un paso)"))
        self.assertEqual(n_repairs, mt["reparaciones_llamadas"])
        self.assertEqual(len(self.fake.requests), 3 + n_repairs, "3 intentos y sus reparaciones; sin resultados no hay "
                         "informe largo: el cierre es un diagnóstico del sistema")
        self.assertEqual(run["ended_reason"], "respuestas_repetidas")
        repair_np = [r["options"]["num_predict"] for r in self.fake.requests
                     if "NO se ejecutó (no se gastó un paso)" in r["messages"][-1]["content"]]
        decide_np = [r["options"]["num_predict"] for r in self.fake.requests
                     if "ETAPA A: decidí" in r["messages"][-1]["content"]]
        self.assertTrue(repair_np and all(n <= 100 for n in repair_np), repair_np)
        self.assertTrue(decide_np and all(n == self.app.cfg.explorer_decide_num_predict for n in decide_np), decide_np)
        calls = json.loads(self.app.files.read_rel(self.mid, "exploracion/llamadas_ollama.json"))
        rep = [x for x in (calls.get("llamadas") if isinstance(calls, dict) else calls) if x.get("etapa") == "A-rep"]
        self.assertTrue(rep and all(x["plazo_s"] <= 30 for x in rep), "plazo propio de las reparaciones")


if __name__ == "__main__":
    unittest.main()
