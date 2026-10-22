"""Experimento de sugerencias (PILOTO.md, «Experimento de sugerencias»): «localizador» contra «evidencia» en la
primera llamada, con plan fijo, la misma semilla por pareja y los prompts comparados ANTES de llamar.

Los modelos simulados validan el CIRCUITO del experimento (plan, semillas, orden, diferencias de prompts, límite,
reanudación, clasificación, juez), NO cómo responde Qwen.
"""
import json
import re
import tempfile
import types
import unittest
from pathlib import Path

from constructor_ia.laboratorio.piloto import ciclo as C
from constructor_ia.laboratorio.piloto import defectos as DF
from constructor_ia.laboratorio.piloto import sugerencias as SG
from constructor_ia.laboratorio.piloto.entorno import Entorno
from constructor_ia.sandbox.executor import Executor
from constructor_ia.sandbox.limiter import NullLimiter

from . import mariadb_fixture
from .helpers import bwrap_works, make_cfg

DATOS = Path(__file__).resolve().parent / "datos"


def banco(*ids):
    ok, problemas = DF.cargar(SG.BANCO)
    assert not problemas, problemas
    return {d["id"]: d for d in ok if not ids or d["id"] in ids}


def obs_sin_regla(candidatas=(17, 15, 12), juegos=()):
    """Una observación en la que ninguna regla congelada responde (como X05, X07, X08, X11 y X12)."""
    return {"candidatas": list(candidatas), "hipotesis": list(candidatas), "juegos": list(juegos),
            "reglas_v1": {"respuesta": None}}


EVIDENCIA = [{"params": p, "salida": {"cobertura": {"leidas": 1}}, "fallas": [{"id": "S2_cobertura"}],
              "texto": "- S2_cobertura: aritmética de la cobertura (campos: leidas)"} for p in C.PARAMS_COORDINADOR]


class PlanTests(unittest.TestCase):
    def test_the_plan_is_fixed_alternated_and_has_one_call_per_defect_and_condition(self):
        self.assertEqual(SG.LLAMADAS_POR_CONDICION, 1)
        self.assertEqual(SG.TANDAS["1"], [("X05", "localizador"), ("X05", "evidencia"),
                                          ("X08", "evidencia"), ("X08", "localizador")])
        self.assertEqual(SG.TANDAS["2"], [("X11", "localizador"), ("X11", "evidencia"),
                                          ("X12", "evidencia"), ("X12", "localizador")])
        self.assertEqual(SG.TANDAS["control"], [("X07", "localizador")])
        primero = [p[1] for t in ("1", "2") for p in SG.TANDAS[t][::2]]
        self.assertEqual(primero.count("localizador"), primero.count("evidencia"), "orden alternado y balanceado")

    def test_both_conditions_of_a_pair_share_the_seed(self):
        """La semilla depende del defecto y de la llamada, no de la condición (la etiqueta solo prefija la clave)."""
        self.assertEqual(C.semilla("X07", 0), 40056709, "la misma de las corridas reales de X07 v1.3.3 y v1.3.4")
        self.assertNotEqual(C.semilla("X05", 0), C.semilla("X08", 0))

    def test_first_step_by_policy(self):
        o = obs_sin_regla()
        self.assertEqual((C.primer_paso(o, "evidencia")["foco"], C.primer_paso(o, "localizador")["foco"]), (None, 17))
        self.assertIsNone(C.primer_paso(o, "localizador")["origen"], "como v1.3.3: sin decir de dónde viene")
        con_regla = {**o, "hipotesis": [19, 21, 20], "reglas_v1": {"respuesta": {"eleccion": 19, "lectura": "R4"}}}
        self.assertEqual(C.primer_paso(con_regla, "evidencia")["foco"], 19)
        self.assertEqual(C.primer_paso(con_regla, "localizador")["foco"], 19, "con una regla, las dos sugieren igual")
        with self.assertRaises(ValueError):
            C.primer_paso(o, "otra")

    def test_prompts_of_a_pair_differ_only_in_the_suggestion_and_the_differences_are_recorded(self):
        x05 = banco("X05")["X05"]
        c = SG.comparar_prompts({"id": "X05", "codigo": x05["codigo"]}, obs_sin_regla(juegos=EVIDENCIA))
        self.assertTrue(c["solo_difieren_en_la_sugerencia"])
        self.assertTrue(c["misma_evidencia"])
        self.assertEqual(c["sugerida"], {"evidencia": None, "localizador": 17})
        self.assertEqual(c["diferencias"], [
            "-Código actual. Los números de línea y la marca «>>» están SOLO para ubicarte; no son parte del código:",
            "+Código actual. Los números de línea están SOLO para ubicarte; no son parte del código:",
            '->>  17 |         cobertura["validas"] = 1',
            '+    17 |         cobertura["validas"] = 1',
            "-Instrucciones candidatas (solo se puede cambiar UNA de estas, completa):",
            "+Instrucciones candidatas, sin orden de preferencia (solo se puede cambiar UNA de estas, completa):",
            "-Línea 17  ← sugerida por el coordinador:",
            "+Línea 17:",
            "-El coordinador sugiere investigar la línea 17, pero es solo una sugerencia, TODAVÍA NO COMPROBADA: "
            "primero decidí cuál de las candidatas es la causa (DIAGNOSTICO) y qué variable o clave hay que cambiar "
            "(CAMBIA); después escribí el cambio MÍNIMO para ESA instrucción, que haga lo que dijiste.",
            "+El coordinador NO sugiere ninguna línea: no tiene evidencia suficiente para preferir una candidata (el "
            "orden de la lista no indica preferencia). primero decidí cuál de las candidatas es la causa "
            "(DIAGNOSTICO) y qué variable o clave hay que cambiar (CAMBIA); después escribí el cambio MÍNIMO para "
            "ESA instrucción, que haga lo que dijiste."])
        for cond in C.POLITICAS:
            self.assertIn(C.FORMATO, SG._texto(c["partes"][cond]), "el formato nuevo de v1.3.4 en las dos")
            self.assertEqual(C.auditar(c["partes"][cond], x05), [])

    def test_any_other_difference_is_detected(self):
        a = "Código actual.\n>>  17 | x\nEl coordinador sugiere investigar la línea 17, pero es solo una sugerencia, " \
            "TODAVÍA NO COMPROBADA: primero"
        b = "Código actual.\n    17 | x\nEl coordinador NO sugiere ninguna línea: no tiene evidencia suficiente " \
            "para preferir una candidata (el orden de la lista no indica preferencia). primero"
        self.assertEqual(SG.normalizar(a), SG.normalizar(b))
        self.assertNotEqual(SG.normalizar(a + " otra cosa"), SG.normalizar(b))
        self.assertNotEqual(SG.normalizar(a.replace("17 | x", "17 | y")), SG.normalizar(b))

    def test_an_uninformative_pair_stops_the_plan_before_any_call(self):
        x05 = banco("X05")["X05"]
        con_regla = {**obs_sin_regla(), "estado": "investigable",
                     "reglas_v1": {"respuesta": {"eleccion": 17, "lectura": "R2"}}}
        cmp_ = {"X05": SG.comparar_prompts({"id": "X05", "codigo": x05["codigo"]}, con_regla)}
        with self.assertRaisesRegex(SG.PlanInvalido, "sugieren lo mismo"):
            SG.validar_plan([("X05", "localizador"), ("X05", "evidencia")], {"X05": con_regla}, cmp_)
        sin = {**obs_sin_regla(), "estado": "investigable"}
        cmp_ = {"X05": SG.comparar_prompts({"id": "X05", "codigo": x05["codigo"]}, sin)}
        cmp_["X05"]["solo_difieren_en_la_sugerencia"] = False
        with self.assertRaisesRegex(SG.PlanInvalido, "algo más que la sugerencia"):
            SG.validar_plan([("X05", "localizador")], {"X05": sin}, cmp_)

    @unittest.skipUnless((DATOS / "llamadas-X07-v133.jsonl").exists(), "falta tests/datos/llamadas-X07-v133.jsonl")
    def test_x07_control_prompt_differs_from_the_real_v133_one_only_in_the_new_format_line(self):
        """El control separa los dos factores que cambiaron juntos en v1.3.4: con «localizador», el prompt de X07 es
        el de la corrida REAL de v1.3.3 (sugerencia 17, «>>», «← sugerida») salvo la línea nueva del formato."""
        real = json.loads((DATOS / "llamadas-X07-v133.jsonl").read_text().splitlines()[0])
        x07 = banco("X07")["X07"]
        ps = C.partes_iniciales({"id": "X07", "codigo": x07["codigo"]}, obs_sin_regla(), "localizador")
        nuevo = [t for o, t in ps if o == "coordinador"]
        viejo = [t for o, t in real["partes"] if o == "coordinador"]
        linea = ("El código tiene que CALCULAR los valores: no copies números de la salida ni de las verificaciones. "
                 "No uses atributos internos (nombres con doble guion bajo, como __len__): el sandbox los rechaza.\n")
        self.assertEqual(len(nuevo), len(viejo))
        self.assertEqual(nuevo[:-1], viejo[:-1], "la pregunta, el glosario, el código marcado y las candidatas")
        self.assertEqual(nuevo[-1].replace(linea, ""), viejo[-1])
        self.assertIn(linea, nuevo[-1])

    def test_classification(self):
        def j(intento, objetivo=(17,), causa=True, **extra):
            return {"id": "X", "objetivo": list(objetivo), "causa_entre_candidatas": causa,
                    "intentos": [intento] if intento else [], **extra}

        def it(diag, sug=17, rechazo=None, interp="reemplazar"):
            return {"diagnostico": diag, "sugerida": sug, "rechazo": rechazo, "interpretacion": interp, "fugas": []}
        casos = [(j(it(17)), "correcto", True, True), (j(it(15)), "incorrecto", False, False),
                 (j(it(None, rechazo="ambigua")), "ambiguo", False, False),
                 (j(it(None, interp="ninguna")), "abstencion", False, False),
                 (j(it(None, interp="ninguna"), objetivo=(14,), causa=False), "abstencion", True, False),
                 (j(it(15, sug=None)), "incorrecto", False, False),
                 (j(it(None, rechazo="formato")), "sin_diagnostico", False, False), (j(None), "sin_llamada", False, False)]
        for juicio, cat, correcta, anclada in casos:
            f = SG.clasificar(juicio, "localizador")
            self.assertEqual((f["categoria"], f["respuesta_correcta"], f["anclado_a_la_sugerencia"]),
                             (cat, correcta, anclada), juicio)
        falsa = SG.clasificar(j(it(17), resultado="CONFIRMACIÓN FALSA", aceptada_por_el_coordinador=True), "evidencia")
        self.assertTrue(falsa["confirmacion_falsa"] and falsa["aceptada_por_el_coordinador"])
        r = SG.resumir([falsa, SG.clasificar(j(it(15)), "localizador")])
        self.assertEqual((r["evidencia"]["confirmaciones_falsas"], r["localizador"]["categorias"]),
                         (1, {"incorrecto": 1}))


class Modelo:
    def __init__(self, responder, fallar_en=()):
        self.responder, self.fallar_en = responder, set(fallar_en)
        self.nombre, self.system, self.base = "simulado", "", types.SimpleNamespace(num_ctx=8192)
        self.llamadas = []

    def pedir(self, tipo, prompt, *, json_out, cancel=None, corto=False, seed=None):
        self.llamadas.append({"seed": seed, "prompt": prompt})
        if len(self.llamadas) in self.fallar_en:
            return None, {"tipo": tipo, "resultado": "error", "error": "Conexión con Ollama interrumpida"}
        m = re.search(r"El coordinador sugiere investigar la línea (\d+)", prompt)
        return self.responder(prompt, int(m.group(1)) if m else None), {"tipo": tipo, "resultado": "completa",
                                                                         "reloj_ms": 1000}


def anclado_o_correcto(prompt, sugerida):
    """«localizador»: diagnostica la sugerida y reemplaza por lo mismo + 0 (no repara). «evidencia»: en X05 la
    reparación correcta de la 17; si no, NINGUNA."""
    if sugerida is not None:
        instr = re.search(rf"Línea {sugerida}  ← sugerida por el coordinador:\n```python\n(.*?)\n```", prompt, re.S)
        return f"DIAGNOSTICO: {sugerida}\nCAMBIA: x\n```python\n{instr.group(1)}  # igual\n```\nMOTIVO: simulado"
    if 'cobertura["validas"] = 1' in prompt:
        return ('DIAGNOSTICO: 17\nCAMBIA: cobertura["validas"]\n```python\ncobertura["validas"] += 1\n```\n'
                "MOTIVO: acumula")
    return "DIAGNOSTICO: NINGUNA\nMOTIVO: simulado"


@unittest.skipUnless(bwrap_works() and mariadb_fixture.available(), "bubblewrap o MariaDB no disponibles")
class ExperimentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = make_cfg(sandbox_timeout_s=60)
        cls.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        cls.ex = Executor(cls.cfg, limiter=NullLimiter(), allow_unverified_limits=True)
        assert cls.ex.self_test()["ok"]
        cls.dir = Path(tempfile.mkdtemp(prefix="cia-sugerencias-"))
        cls.ent = Entorno(cls.cfg, cls.ex, mariadb_fixture.server(), cls.dir / "ent", base="sug_t",
                          escala={"dias": 3, "por_dia": 300})

    @classmethod
    def tearDownClass(cls):
        cls.ent.cerrar()

    def correr(self, nombre, tanda, modelo=None):
        return SG.correr_tanda(self.cfg, self.ent, tanda=tanda, salida=self.dir / nombre,
                               crear_modelo=(lambda: modelo) if modelo else None, log=lambda *_: None,
                               datos_juez={"semilla": 20261023, "dias": 4, "por_dia": 257},
                               datos_variados={"semilla": 4242, "dias": 3, "por_dia": 211})

    def test_batch_1_end_to_end(self):
        m = Modelo(anclado_o_correcto)
        res = self.correr("exp/tanda-1", "1", m)
        self.assertEqual(res["corrida"], "completa")
        self.assertEqual(len(m.llamadas), 4, "una llamada por defecto y condición")
        llamadas = (self.dir / "exp/tanda-1" / "llamadas.jsonl").read_text().splitlines()
        claves = [json.loads(x)["clave"] for x in llamadas]
        self.assertEqual(claves, ["localizador|X05|c0|S17", "evidencia|X05|c0|S-", "evidencia|X08|c0|S-",
                                  "localizador|X08|c0|S17"], "el orden alternado del plan")
        self.assertEqual([x["seed"] for x in m.llamadas],
                         [C.semilla("X05", 0)] * 2 + [C.semilla("X08", 0)] * 2, "misma semilla por pareja")
        parejas = json.loads((self.dir / "exp/tanda-1" / "parejas.json").read_text())
        for did in ("X05", "X08"):
            self.assertTrue(parejas[did]["solo_difieren_en_la_sugerencia"])
            self.assertEqual(parejas[did]["prompt_enviado_igual_al_comparado"],
                             {"localizador": True, "evidencia": True})
            self.assertEqual(len(parejas[did]["diferencias"]), 10)
        filas = {(f["id"], f["condicion"]): f for f in res["filas"]}
        self.assertEqual({k: f["categoria"] for k, f in filas.items()},
                         {("X05", "localizador"): "correcto", ("X05", "evidencia"): "correcto",
                          ("X08", "evidencia"): "abstencion", ("X08", "localizador"): "incorrecto"})
        self.assertTrue(filas[("X08", "localizador")]["anclado_a_la_sugerencia"])
        self.assertFalse(filas[("X05", "localizador")]["aceptada_por_el_coordinador"], "«+ 0» no repara nada")
        self.assertEqual(filas[("X05", "evidencia")]["resultado_juez"], "confirmación verdadera")
        self.assertEqual(res["resumen"]["evidencia"]["confirmaciones_verdaderas"], 1)
        self.assertEqual(sum(r["confirmaciones_falsas"] for r in res["resumen"].values()), 0)
        self.assertEqual(sum(r["fugas"] for r in res["resumen"].values()), 0)
        texto = (self.dir / "exp/tanda-1" / "resumen.md").read_text()
        self.assertIn("| 1 | X05 | evidencia |", texto)
        self.assertIn("->>  17 |", texto)
        # Volver a correr la tanda no repite llamadas, y una carpeta de la tanda 1 no sirve para la 2.
        m2 = Modelo(anclado_o_correcto)
        self.assertEqual(self.correr("exp/tanda-1", "1", m2)["llamadas_nuevas"], 0)
        self.assertEqual(m2.llamadas, [])
        with self.assertRaisesRegex(SG.PlanInvalido, "otro plan"):
            self.correr("exp/tanda-1", "2", m2)
        self.assertIn("| 1 | X08 | localizador |", SG.resumen_general(self.dir / "exp"))

    def test_interruptions_stop_and_resume_without_repeating(self):
        m = Modelo(anclado_o_correcto, fallar_en=(2, 3))
        res = self.correr("cortes", "1", m)
        self.assertTrue(res["corrida"].startswith("detenido_por_entorno"))
        self.assertEqual(res["llamadas_nuevas"], 1)
        m2 = Modelo(anclado_o_correcto)
        res = self.correr("cortes", "1", m2)
        self.assertEqual((res["corrida"], len(m2.llamadas), res["llamadas_totales"]), ("completa", 3, 4))

    def test_batch_2_and_control_and_prompts_only(self):
        res = self.correr("solo", "2")
        self.assertEqual((res["corrida"], res["filas"]), ("solo_prompts", []))
        self.assertFalse((self.dir / "solo" / "llamadas.jsonl").exists(), "sin modelo no se llama a nadie")
        p = json.loads((self.dir / "solo" / "parejas.json").read_text())
        self.assertEqual(p["X11"]["sugerida"]["localizador"], 20)
        self.assertEqual(p["X12"]["sugerida"]["localizador"], 15)
        self.assertEqual((p["X11"]["orden"], p["X12"]["orden"]), (["localizador", "evidencia"],
                                                                  ["evidencia", "localizador"]))
        m = Modelo(anclado_o_correcto)
        res = self.correr("t2", "2", m)
        self.assertEqual(len(m.llamadas), 4)
        filas = {(f["id"], f["condicion"]): f for f in res["filas"]}
        self.assertTrue(filas[("X11", "evidencia")]["respuesta_correcta"], "la causa está fuera: NINGUNA es correcta")
        self.assertFalse(filas[("X12", "localizador")]["respuesta_correcta"])
        m = Modelo(anclado_o_correcto)
        res = self.correr("control", "control", m)
        self.assertEqual(len(m.llamadas), 1)
        self.assertEqual(m.llamadas[0]["seed"], 40056709)
        self.assertEqual([f["sugerida"] for f in res["filas"]], [17])


if __name__ == "__main__":
    unittest.main()
