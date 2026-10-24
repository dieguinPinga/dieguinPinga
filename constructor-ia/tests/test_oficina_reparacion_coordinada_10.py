"""Experimento 7.10 (OFICINA.md, «Experimento 7.10»): planificación causal antes de programar.

SIN inferencias reales. Estas pruebas demuestran que la ARQUITECTURA funciona (con respuestas simuladas), no que los
modelos puedan reparar el defecto: eso solo lo dirá la corrida real. Orden de los pedidos en cada ciclo: plan (qwen),
revisión del plan (gemma), [otro plan, otra revisión], parche (qwen); antes de todo, el diagnóstico (gemma).
"""
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from constructor_ia import cli
from constructor_ia.oficina import programador as PG
from constructor_ia.oficina import registro as RG
from constructor_ia.oficina import remoto as RM
from constructor_ia.oficina import reparacion_coordinada as RC
from constructor_ia.oficina import reparacion_coordinada_8 as RC8
from constructor_ia.oficina import reparacion_coordinada_9 as RC9
from constructor_ia.oficina import reparacion_coordinada_10 as RC10
from constructor_ia.oficina import reparacion_guiada as RGD
from constructor_ia.oficina import revision_existente as RE
from constructor_ia.oficina.misiones_programacion import MISIONES, REFERENCIA_CONCILIAR
from constructor_ia.ollama import OllamaClient

from .fake_ollama import FakeOllama
from .helpers import bwrap_works
from .test_oficina_hipotesis import A
from .test_oficina_lectura_escritura import diagnostico_real
from .test_oficina_normalizacion_sangria import QWEN
from .test_oficina_parches import parche
from .test_oficina_reparacion_coordinada_8 import INVERTIR, gemma8
from .test_oficina_reparacion_coordinada_9 import APRUEBA, NO_CAUSA, revision
from .test_oficina_reparacion_guiada import INEXISTENTE, ROMPE_OTRA_REGLA

F = "conciliar_movimientos"
ZONAS = [(27, 37), (41, 46)]


def plan(estado, operacion, cambiar, lineas, preservar=("los ingresos y egresos siguen registrados",), cambio=None,
         marca=""):
    d = {"estado_incorrecto": estado + marca, "operacion_que_lo_produce": operacion, "comportamiento_a_cambiar": cambiar,
         "comportamiento_a_preservar": list(preservar),
         "lineas_a_modificar": [{"linea": linea, "por_que": "es la operación responsable"} for linea in lineas]}
    if cambio:
        d["cambio_respecto_del_anterior"] = cambio
    return json.dumps(d, ensure_ascii=False)


PLAN_OK = plan("las anulaciones aplicadas no se registran en aplicados",
               "la escritura en aplicados está condicionada por el tipo del movimiento",
               "registrar también las anulaciones aplicadas", ['if tipo != "anulacion":'])
PLAN_OK_2 = plan("las anulaciones aplicadas no se registran en aplicados",
                 "la escritura en aplicados está condicionada por el tipo del movimiento",
                 "registrar también las anulaciones aplicadas", ['if tipo != "anulacion":'],
                 cambio="el parche anterior invirtió la condición; ahora hay que registrar todos los tipos")
PLAN_SIN_CAUSA = plan("la anulación referida da None", "la búsqueda de la anulación referida",
                      "tratar el caso None como anulación de anulación", ["if ref is None:"])
PLAN_ROMPE = plan("aplicados contiene ingresos y egresos", "la condición del tipo deja pasar ingresos y egresos",
                  "invertir la condición para que solo pasen anulaciones", ['if tipo != "anulacion":'],
                  preservar=("nada",))
CRITICA_CAUSA = "[CRÍTICA-PLAN-c1] el plan trata el síntoma (ref None) y no la escritura que deja afuera a las anulaciones"
CRITICA_PRESERVAR = "[CRÍTICA-PLAN-p2] invertir la condición deja de registrar ingresos y egresos, que hay que preservar"
RECHAZA_CAUSA = revision("rechazar", CRITICA_CAUSA, elimina=False, preserva=True)
RECHAZA_PRESERVAR = revision("rechazar", CRITICA_PRESERVAR, elimina=True, preserva=False)


class UnidadesTests(unittest.TestCase):
    def test_plan_validation(self):
        p = RC10.interpretar_plan(PLAN_OK, A, ZONAS, F, 1)
        self.assertEqual([(x["lineas"], x["en_zona"]) for x in p["lineas_a_modificar"]], [([44], True)])
        for texto, motivo, ciclo in (("no es JSON", "objeto JSON", 1),
                                     (plan("", "op", "c", ['if tipo != "anulacion":']), "estado_incorrecto", 1),
                                     (plan("e", "op", "c", ["saldos = dict(saldos_iniciales or {})"]), "dentro de las zonas", 1),
                                     (PLAN_OK, "cambio_respecto_del_anterior", 2),
                                     ("def conciliar_movimientos(m):\n  return {}", "incluyó el programa", 1)):
            with self.subTest(motivo=motivo):
                with self.assertRaisesRegex(ValueError, motivo):
                    RC10.interpretar_plan(texto, A, ZONAS, F, ciclo)

    def test_repeated_plans_are_detected_and_coherence_is_measured(self):
        a = RC10.interpretar_plan(PLAN_OK, A, ZONAS, F, 1)
        b = RC10.interpretar_plan(PLAN_OK_2, A, ZONAS, F, 2)
        self.assertEqual(RC10.clave_plan(a), RC10.clave_plan(b), "el mismo plan, aunque cambie la explicación")
        self.assertNotEqual(RC10.clave_plan(a), RC10.clave_plan(RC10.interpretar_plan(PLAN_ROMPE, A, ZONAS, F, 1)))
        self.assertTrue(RC10.coherencia(a, [{"lineas": [44, 44]}])["coherente"])
        self.assertFalse(RC10.coherencia(a, [{"lineas": [28, 28]}])["coherente"])

    def test_no_known_solution_in_the_prompts(self):
        causa = RGD.CAUSAS[(F, "anulaciones_no_registradas")]["texto"]
        for texto in (RC10.SISTEMA_PLANIFICADOR, RC10.PEDIDO_PLAN, RC10.SISTEMA_REVISORA_PLAN, RC10.PEDIDO_PARCHE):
            for prohibido in ("if True", "!=", "aplicados", "anulacion", causa):
                self.assertNotIn(prohibido, texto)


@unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
class CicloTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from .test_oficina_revision_existente import Base
        Base.setUpClass()
        cls.ex = Base.ex
        b = Base()
        _, _, cls.origen = b.correr([json.dumps(diagnostico_real(), ensure_ascii=False), INEXISTENTE, INEXISTENTE,
                                     INEXISTENTE], variante="7.3")
        b.doCleanups()

    def correr(self, respuestas, mod=RC10, **k):
        fake = FakeOllama(respuestas, models=("gemma4:12b", "qwen2.5-coder:7b"))
        self.addCleanup(fake.close)
        f = RC.preparar(self.origen)
        destino = mod.carpeta(Path(tempfile.mkdtemp(prefix="cia-exp710-")), self.origen)
        inf = mod.correr(destino, f, self.ex, OllamaClient(fake.url), RM.compuerta(fake.url),
                         PG.modelo_remoto("gemma4:12b", num_predict=RE.NUM_PREDICT_REVISOR, timeout_s=600),
                         PG.modelo_remoto("qwen2.5-coder:7b"), **k)
        return inf, fake, destino

    def usuario(self, fake, i):
        return fake.requests[i]["messages"][1]["content"]

    def test_1_a_plan_that_does_not_remove_the_cause_is_rejected(self):
        inf, fake, _ = self.correr([gemma8(), PLAN_SIN_CAUSA, RECHAZA_CAUSA, PLAN_OK, APRUEBA, parche(QWEN)])
        p1, p2 = inf["ciclos"][0]["planes"]
        self.assertEqual((p1["revision"]["decision"], p2["revision"]["decision"]), ("rechazar", "aprobar"))
        self.assertTrue(p1["evaluacion"]["revision_correcta"])
        self.assertEqual(fake.requests[2]["messages"][0]["content"], RC10.SISTEMA_REVISORA_PLAN)
        self.assertTrue(inf["exito"])

    def test_2_a_plan_that_breaks_correct_behavior_is_rejected(self):
        inf, _, _ = self.correr([gemma8(), PLAN_ROMPE, RECHAZA_PRESERVAR, PLAN_OK, APRUEBA, parche(QWEN)])
        r = inf["ciclos"][0]["planes"][0]["revision"]
        self.assertEqual((r["decision"], r["preserva_comportamiento"]), ("rechazar", False))
        self.assertTrue(inf["exito"])

    def test_3_and_7_a_correct_plan_is_approved_and_the_repair_passes_nine_of_nine(self):
        with mock.patch.dict(RGD.CAUSAS, {}, clear=True):
            inf, fake, d = self.correr([gemma8(), PLAN_OK, APRUEBA, parche(QWEN)])
        self.assertTrue(inf["exito"], inf["motivo"])
        self.assertEqual([r["model"] for r in fake.requests],
                         ["gemma4:12b", "qwen2.5-coder:7b", "gemma4:12b", "qwen2.5-coder:7b"])
        self.assertEqual((inf["verificacion"]["aprobados"], inf["verificacion"]["total"]), (9, 9))
        m = inf["medicion"]
        for k in ("diagnostico_correcto", "plan_causal_correcto", "revision_del_plan_correcta",
                  "parche_coherente_con_el_plan", "parche_aplicado", "caso_sintetico_corregido",
                  "bateria_independiente_9_9"):
            self.assertTrue(m[k], k)
        self.assertEqual(inf["clasificacion"], RC10.CLASIFICACION_10 + " (con normalización de la sangría)")
        for rel in ("diagnostico-1/respuesta.txt", "ciclo-1/plan-1/plan.json", "ciclo-1/revision-plan-1/revision.json",
                    "ciclo-1/parche/parche.json", "ciclo-1/parche/solucion.py", "informe.json"):
            self.assertTrue((d / rel).exists(), rel)
        self.assertEqual(inf["consumo"]["por_rol"], {"diagnosticadora": 1, "planificador": 1, "revisora del plan": 1,
                                                    "programador": 1})

    def test_4_the_critique_of_a_rejected_plan_reaches_qwen_in_full(self):
        inf, fake, d = self.correr([gemma8(), PLAN_SIN_CAUSA, RECHAZA_CAUSA, PLAN_OK, APRUEBA, parche(QWEN)])
        esperado = RC10.texto_critica_plan(1, RC9.interpretar_revision(RECHAZA_CAUSA, F))
        self.assertIn(esperado, self.usuario(fake, 3))
        self.assertIn(CRITICA_CAUSA, self.usuario(fake, 3))
        self.assertEqual(inf["ciclos"][0]["planes"][0]["critica_enviada"], esperado)

    def test_5_the_patch_corresponds_to_the_approved_plan(self):
        inf, fake, _ = self.correr([gemma8(), PLAN_OK, APRUEBA, parche(QWEN)])
        aprobado = RC10.interpretar_plan(PLAN_OK, A, ZONAS, F, 1)
        pedido = self.usuario(fake, 3)
        self.assertIn("PLAN APROBADO (lo que tenés que implementar):\n" + RC10.texto_plan(aprobado), pedido)
        self.assertTrue(pedido.endswith(RC10.PEDIDO_PARCHE))
        self.assertTrue(inf["ciclos"][0]["parche"]["coherencia_con_el_plan"]["coherente"])
        inf, _, _ = self.correr([gemma8(), PLAN_OK, APRUEBA, parche(NO_CAUSA), PLAN_OK_2, APRUEBA, parche(QWEN)])
        self.assertFalse(inf["ciclos"][0]["parche"]["coherencia_con_el_plan"]["coherente"],
                         "un parche sobre otra línea no corresponde al plan")

    def test_6_an_incorrect_patch_fails_even_with_an_approved_plan(self):
        inf, fake, _ = self.correr([gemma8(), PLAN_OK, APRUEBA, parche(INVERTIR), PLAN_OK_2, APRUEBA, parche(QWEN)])
        pa1 = inf["ciclos"][0]["parche"]
        self.assertEqual((pa1["aplicado"], pa1["discrepancia_sintetica"]), (True, "persiste"))
        self.assertTrue(pa1["fallo_de_implementacion"])
        self.assertEqual(inf["medicion"]["fallos_de_implementacion"], 1)
        plan_ciclo_2 = self.usuario(fake, 4)
        for texto in ("RESULTADO DEL CICLO 1", "DISCREPANCIA PERSISTE", "Lo que devuelve el código con tu parche",
                      "En tu nuevo plan explicá qué cambia respecto de este intento."):
            self.assertIn(texto, plan_ciclo_2)
        self.assertTrue(inf["exito"])
        inf, fake, _ = self.correr([gemma8(), PLAN_OK, APRUEBA, ROMPE_OTRA_REGLA])
        self.assertFalse(inf["verificacion"]["ok"], "la batería decide aunque el plan esté aprobado")
        self.assertFalse(inf["exito"])
        self.assertEqual(len(fake.requests), 4)

    def test_8_no_hidden_tests_leak(self):
        inf, fake, _ = self.correr([gemma8(), PLAN_SIN_CAUSA, RECHAZA_CAUSA, PLAN_OK, APRUEBA, parche(INVERTIR),
                                    PLAN_OK_2, APRUEBA, parche(QWEN)])
        todo = json.dumps(fake.requests, ensure_ascii=False)
        for prohibido in ("Caso oculto", "anulaciones_no_registradas", REFERENCIA_CONCILIAR, "9/9",
                          RE.DEFECTOS[F]["anulaciones_no_registradas"]["explicacion"]):
            self.assertNotIn(prohibido, todo)
        for c in MISIONES[F][0]()["spec"]["cases"]:
            if not c.get("visible", True) and c.get("categoria"):
                self.assertNotIn(c["categoria"], todo)
        for i in (2, 4, 7):                               # revisiones del plan: sin resultados de pruebas
            self.assertNotIn("DISCREPANCIA PERSISTE", self.usuario(fake, i))
            self.assertNotIn("Lo que devuelve el código con tu parche", self.usuario(fake, i))
        self.assertTrue(inf["auditoria_de_fugas"]["sin_fugas"])

    def test_9_repeated_plans_and_patches_are_detected(self):
        inf, fake, _ = self.correr([gemma8(), PLAN_OK, APRUEBA, parche(INVERTIR), PLAN_OK_2, APRUEBA, parche(INVERTIR),
                                    PLAN_OK_2, APRUEBA, parche(INVERTIR)])
        self.assertEqual(inf["ciclos"][1]["planes"][0]["repite_el_plan"], "ciclo 1, plan 1")
        self.assertEqual(inf["ciclos"][1]["parche"]["repite_el_parche_del_ciclo"], 1)
        self.assertIn("REPETICIÓN", self.usuario(fake, 9))
        self.assertIn("repite uno anterior después del aviso", inf["detenido"])
        self.assertEqual(inf["medicion"]["parches_repetidos"], 2)
        self.assertGreaterEqual(inf["medicion"]["planes_repetidos"], 2)
        self.assertFalse(inf["exito"])
        inf, fake, _ = self.correr([gemma8(), PLAN_SIN_CAUSA, RECHAZA_CAUSA, PLAN_SIN_CAUSA, RECHAZA_CAUSA,
                                    PLAN_OK_2, APRUEBA, parche(QWEN)])
        self.assertIn("Tu plan es igual al anterior (ciclo 1, plan 1)", self.usuario(fake, 5))
        self.assertTrue(inf["ciclos"][0]["sin_plan_aprobado"])
        self.assertTrue(inf["exito"])

    def test_limits_invalid_review_and_budget(self):
        sin_causa_2 = plan("la anulación referida da None", "la búsqueda", "tratar None", ["if ref is None:"],
                           cambio="otro intento")
        inf, fake, _ = self.correr([gemma8()] + [PLAN_SIN_CAUSA, RECHAZA_CAUSA] * 2 + [sin_causa_2, RECHAZA_CAUSA] * 4
                                   + [PLAN_OK, APRUEBA, parche(QWEN)])
        self.assertEqual(len(fake.requests), 1 + 3 * 4, "3 ciclos × 2 planes × (plan + revisión)")
        self.assertFalse(inf["exito"])
        inf, _, _ = self.correr([gemma8(), PLAN_OK, "no es JSON", parche(QWEN)])
        self.assertEqual(inf["ciclos"][0]["plan_aprobado"]["por"], "revisión inválida (sin decisión)")
        self.assertTrue(inf["medicion"]["bateria_independiente_9_9"], "deciden las comprobaciones deterministas")
        self.assertFalse(inf["exito"], "sin aprobación de la revisora no es el éxito pedido")
        inf, fake, _ = self.correr([gemma8()], max_segundos=0)
        self.assertEqual(fake.requests, [])
        self.assertIn("límite de tiempo", inf["detenido"])

    def test_10_previous_experiments_still_work(self):
        for mod, respuestas in ((RC9, [gemma8(), parche(QWEN), APRUEBA]), (RC8, [gemma8(), parche(QWEN)])):
            with self.subTest(mod=mod.__name__):
                inf, _, _ = self.correr(respuestas, mod=mod)
                self.assertTrue(inf["exito"])
                self.assertNotIn("ciclos", inf)

    def test_cli(self):
        lab = Path(tempfile.mkdtemp(prefix="cia-exp710-cli-")) / "oficina-coordinada-7-10"
        fake = FakeOllama([gemma8(), PLAN_OK, APRUEBA, parche(QWEN)], models=("gemma4:12b", "qwen2.5-coder:7b"))
        self.addCleanup(fake.close)
        reg = RG.Registro(lab / "oficina.db")
        reg.registrar("prueba", fake.url, ["gemma4:12b", "qwen2.5-coder:7b"], permitir_local=True)
        reg.cerrar()
        from constructor_ia.sandbox.executor import Executor
        from constructor_ia.sandbox.limiter import NullLimiter

        def cli_(*extra):
            out, err = StringIO(), StringIO()
            ex = lambda cfg: Executor(cfg, limiter=NullLimiter(), allow_unverified_limits=True)  # noqa: E731
            with redirect_stdout(out), redirect_stderr(err), mock.patch("constructor_ia.sandbox.executor.Executor", ex), \
                    mock.patch.object(RM, "memoria_disponible_mb", lambda: 8000):
                rc = cli.main(["--config", str(lab.parent / "no-existe.toml"), "oficina", "reparar-coordinado-10",
                               "--corrida", str(self.origen), "--carpeta", str(lab), "--trabajador", "prueba", *extra])
            return rc, out.getvalue() + err.getvalue()
        rc, out = cli_("--mostrar-pedido")
        self.assertEqual(rc, 0, out)
        self.assertIn("experimento 7.10", out)
        self.assertEqual(fake.requests, [])
        rc, out = cli_("--autorizo-inferencia-remota")
        self.assertEqual(rc, 0, out)
        self.assertIn("ciclo 1 · plan 1: válido · revisora: aprobar", out)
        self.assertIn("ciclo 1 · parche: aplicado · sangría normalizada · coherente con el plan: sí", out)
        self.assertIn("bateria_independiente_9_9: sí", out)
        self.assertIn("Experimento 7.10: ÉXITO", out)


if __name__ == "__main__":
    unittest.main()
