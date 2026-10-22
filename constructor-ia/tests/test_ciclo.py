"""Ciclo de investigación v1 (PILOTO.md, «Ciclo v1»): hipótesis → reparación → verificación → juez separado.

Los modelos simulados validan el CIRCUITO (aislamiento del juez, verificación en dos juegos de parámetros, forma de la
salida, confirmaciones falsas medidas, límite y reanudación, interrupciones), NO la capacidad de un modelo.
"""
import json
import re
import tempfile
import types
import unittest
from pathlib import Path

from constructor_ia.laboratorio.piloto import banco as B
from constructor_ia.laboratorio.piloto import ciclo as C
from constructor_ia.laboratorio.piloto import defectos as DF
from constructor_ia.laboratorio.piloto import e0 as E0
from constructor_ia.laboratorio.piloto.entorno import Entorno
from constructor_ia.sandbox.executor import Executor
from constructor_ia.sandbox.limiter import NullLimiter

from . import mariadb_fixture
from .helpers import bwrap_works, make_cfg

BANCO = Path(__file__).resolve().parent.parent / "constructor_ia" / "laboratorio" / "piloto" / "banco_desarrollo"
REF = B.REFERENCIA.splitlines()


def banco(*ids):
    ok, problemas = DF.cargar(BANCO)
    assert not problemas, problemas
    return [d for d in ok if not ids or d["id"] in ids]


def hipotesis_del_prompt(prompt: str) -> int:
    return int(re.search(r"instrucción marcada \(línea (\d+)", prompt).group(1))


class Modelo:
    """Responde con una función (id del defecto deducido del código, línea de la hipótesis) → texto."""

    def __init__(self, responder, fallar_en=()):
        self.responder, self.fallar_en = responder, set(fallar_en)
        self.nombre, self.system, self.base = "simulado", "", types.SimpleNamespace(num_ctx=8192)
        self.llamadas = []

    def pedir(self, tipo, prompt, *, json_out, cancel=None, corto=False, seed=None):
        self.llamadas.append({"seed": seed, "prompt": prompt})
        if len(self.llamadas) in self.fallar_en:
            return None, {"tipo": tipo, "resultado": "error", "error": "Conexión con Ollama interrumpida"}
        return self.responder(prompt, hipotesis_del_prompt(prompt)), {"tipo": tipo, "resultado": "completa",
                                                                      "reloj_ms": 1000}


def bloque(codigo):
    return f"```python\n{codigo}\n```\nMOTIVO: simulado"


class UnitTests(unittest.TestCase):
    def test_rules_are_the_frozen_ones(self):
        self.assertEqual(E0.huella_reglas(), C.HUELLA_REGLAS)

    def test_coordinator_params_are_not_the_judges(self):
        self.assertNotIn(B.PARAMS_RESERVA, C.PARAMS_COORDINADOR)
        self.assertEqual(C.PARAMS_COORDINADOR[0], B.PARAMS_VISIBLES)

    def test_omissions_are_scored_by_insertion_point(self):
        por = {d["id"]: d for d in banco()}
        self.assertEqual(DF.objetivo(por["X11"]), {14, 15}, "falta «continue» al final del cuerpo del if")
        self.assertEqual(DF.objetivo(por["X12"]), {14, 17}, "falta una instrucción entre el if y la l.17")
        self.assertEqual(DF.objetivo(por["X04"]), {22})
        sin = dict(por["X11"], insercion={"despues_de": 14, "sangria": 12})
        self.assertEqual(DF.objetivo(sin), {14, 15}, "hueco al principio del cuerpo: la siguiente y el dueño")

    def test_bad_insertion_is_rejected(self):
        d = Path(tempfile.mkdtemp())
        (d / "a.py").write_text(B.REFERENCIA)
        (d / "manifiesto.json").write_text(json.dumps([{"id": "Y1", "archivo": "a.py", "lineas": [3, 3],
                                                        "clase": "omisión", "insercion": {"despues_de": 999}}]))
        ok, problemas = DF.cargar(d)
        self.assertEqual(ok, [])
        self.assertIn("insercion", problemas[0])

    def test_response_interpretation(self):
        self.assertEqual(C.interpretar(bloque("x = 1"))["fragmento"].strip(), "x = 1")
        self.assertEqual(C.interpretar("NO_ES_ESTA\nMOTIVO: la causa está antes")["tipo"], "no_es_esta")
        self.assertEqual(C.interpretar("ELIMINAR\nMOTIVO: sobra")["tipo"], "eliminar")
        self.assertEqual(C.interpretar("no sé")["tipo"], "ilegible")
        self.assertEqual(C.interpretar(None)["tipo"], "ilegible")

    def test_leak_audit(self):
        x04 = banco("X04")[0]
        self.assertEqual(C.auditar([("coordinador", x04["codigo"])], x04), [])
        self.assertTrue(C.auditar([("coordinador", REF[21])], x04), "la línea de la referencia que el defecto cambió")
        self.assertTrue(C.auditar([("evidencia", x04["descripcion"])], x04))
        self.assertTrue(C.auditar([("evidencia", f"Con los parámetros {B.PARAMS_RESERVA}")], x04))

    def test_prompt_shows_hypothesis_not_judge_information(self):
        x04 = banco("X04")[0]
        obs = {"juegos": [{"params": p, "salida": {"resultado": {}}, "fallas": [{"id": "VF"}],
                           "texto": "- VF: algo (campos: suma)"} for p in C.PARAMS_COORDINADOR]}
        ps = C.partes({"id": "X04", "codigo": x04["codigo"]}, obs, 20)
        texto = "\n".join(t for _, t in ps)
        self.assertIn("TODAVÍA NO COMPROBADA", texto)
        self.assertIn(">>  20 |", texto)
        self.assertIn(str(C.PARAMS_COORDINADOR[1]), texto)
        self.assertNotIn("[modelo]", texto)
        self.assertEqual(C.auditar(ps, x04), [])


@unittest.skipUnless(bwrap_works() and mariadb_fixture.available(), "bubblewrap o MariaDB no disponibles")
class CycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = make_cfg(sandbox_timeout_s=60)
        cls.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        cls.ex = Executor(cls.cfg, limiter=NullLimiter(), allow_unverified_limits=True)
        assert cls.ex.self_test()["ok"]
        cls.dir = Path(tempfile.mkdtemp(prefix="cia-ciclo-"))
        cls.ent = Entorno(cls.cfg, cls.ex, mariadb_fixture.server(), cls.dir / "ent", base="ciclo_t",
                          escala={"dias": 3, "por_dia": 300})

    @classmethod
    def tearDownClass(cls):
        cls.ent.cerrar()

    def correr(self, nombre, defectos, modelo=None, **kw):
        return C.correr(self.cfg, self.ent, defectos=defectos, salida=self.dir / nombre,
                        crear_modelo=(lambda: modelo) if modelo else None, log=lambda *_: None, **kw)

    def test_boundary_data_make_an_inclusive_limit_detectable(self):
        res = self.correr("bordes", banco("X10"))
        self.assertEqual(res["bordes"]["valor_900"], 6)
        (j,) = res["juicio"]
        self.assertFalse(j["equivalente"], "con lecturas de 900, «v <= 900» cambia la salida")
        self.assertNotEqual(j["estado"], "no_detectado", "la suma de la ventana con 900 no coincide con VF")

    def test_verification_needs_both_parameter_sets_and_the_same_shape(self):
        co = C.Coordinador(self.ent)
        x04 = banco("X04")[0]
        obs = C.observar(co, {"id": "X04", "codigo": x04["codigo"]})
        self.assertEqual(obs["estado"], "investigable")
        self.assertTrue(C.verificar(co, obs, x04["codigo"], B.REFERENCIA)["aceptada"])
        fijo = B.REFERENCIA.replace('top = int(p.get("top", 3))', "top = 3")
        v = C.verificar(co, obs, x04["codigo"], fijo)
        self.assertFalse(v["aceptada"])
        self.assertIn("forma", v["motivo"], "con top=4 devuelve 3 ventanas: VF no lo ve, la forma sí")
        vacio = B.REFERENCIA.replace("    mejores = sorted", "    sumas = {}\n    mejores = sorted")
        self.assertFalse(C.verificar(co, obs, x04["codigo"], vacio)["aceptada"], "salida vacía")
        self.assertEqual(C.verificar(co, obs, x04["codigo"], x04["codigo"])["motivo"], "no cambia el código")

    def test_true_and_false_confirmations_are_told_apart_by_the_judge(self):
        vistos = []
        original = C.investigar

        def espia(co, problema, obs, *a, **k):
            vistos.append(set(problema))
            return original(co, problema, obs, *a, **k)

        def responder(prompt, linea):
            if 'cobertura["leidas"] = 1' in prompt and linea == 12:        # X07: la reparación correcta
                return bloque('cobertura["leidas"] += 1')
            if 'cobertura["validas"] = 1' in prompt and linea == 17:       # X05: engaña a S2 y a VF
                return bloque('cobertura["validas"] += 2\ncobertura["invalidas"] -= 1')
            return "NO_ES_ESTA\nMOTIVO: no parece"
        C.investigar = espia
        try:
            res = self.correr("confirmaciones", banco("X07", "X05"), Modelo(responder))
        finally:
            C.investigar = original
        self.assertEqual(vistos, [{"id", "codigo"}] * 2, "la investigación recibe SOLO id y código")
        por = {j["id"]: j for j in res["juicio"]}
        self.assertEqual(por["X07"]["resultado"], "confirmación verdadera")
        self.assertEqual(por["X05"]["resultado"], "CONFIRMACIÓN FALSA",
                         "pasa toda la suite del coordinador, pero el juez ve validas e invalidas mal")
        self.assertEqual(res["resumen"]["confirmaciones_falsas"], ["X05"])
        self.assertEqual(res["resumen"]["fugas"], 0)
        llamadas = [json.loads(x) for x in (self.dir / "confirmaciones" / "llamadas.jsonl").read_text().splitlines()]
        for ll in llamadas:
            texto = "\n".join(t for _, t in ll["partes"])
            self.assertNotIn(str(B.PARAMS_RESERVA), texto)
            self.assertNotIn("Cada fila reemplaza", texto, "la descripción del defecto no llega al modelo")
        self.assertIn("CONFIRMACIÓN FALSA", (self.dir / "confirmaciones" / "resumen.md").read_text())

    def test_call_limit_and_resume_without_repeating_calls(self):
        nunca = lambda prompt, linea: "NO_ES_ESTA"                          # noqa: E731
        m1 = Modelo(nunca)
        r1 = self.correr("reanudar", banco("X07"), m1, limite_llamadas=2)
        self.assertEqual((r1["corrida"], len(m1.llamadas)), ("limite", 2))
        m2 = Modelo(nunca)
        r2 = self.correr("reanudar", banco("X07"), m2)
        self.assertEqual(len(m2.llamadas), 1, "solo la hipótesis que faltaba")
        self.assertEqual(r2["llamadas_totales"], 3)
        self.assertEqual(r2["juicio"][0]["resultado"], "abstención")
        self.assertEqual(len({ll["seed"] for ll in m1.llamadas + m2.llamadas}), 3, "una semilla por hipótesis")

    def test_two_consecutive_ollama_failures_stop_the_run(self):
        m = Modelo(lambda p, ln: "NO_ES_ESTA", fallar_en=(1, 2))
        res = self.correr("cortes", banco("X07"), m)
        self.assertTrue(res["corrida"].startswith("detenido_por_entorno"))
        self.assertEqual(len(res["interrupciones"]), 2)
        self.assertEqual(res["llamadas_totales"], 0, "un corte no es una respuesta del modelo")

    def test_without_model_it_is_the_end_to_end_rule_evaluation(self):
        res = self.correr("sin_modelo", banco("X04", "X01"))
        por = {j["id"]: j for j in res["juicio"]}
        self.assertEqual(res["llamadas_totales"], 0)
        self.assertEqual(por["X01"]["reglas_v1_punta_a_punta"], "acierta")
        self.assertEqual(por["X04"]["reglas_v1_punta_a_punta"], "se equivoca",
                         "el error que el filtro de E0 escondía queda a la vista")
        self.assertFalse(por["X04"]["causa_entre_candidatas"])


if __name__ == "__main__":
    unittest.main()
