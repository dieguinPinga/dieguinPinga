"""Experimento 7.11 (OFICINA.md, «Experimento 7.11»): localización causal y revisión no bloqueante.

SIN inferencias reales: diagnosticadora, planificador, revisora y programador simulados; sandbox real. Demuestran la
ARQUITECTURA, no la capacidad de los modelos. Lo central: el resultado depende de la ejecución, no de la opinión de
la revisora (rechaza un plan correcto → se prueba igual; aprueba uno incorrecto → la ejecución lo desmiente).
Orden de los pedidos: diagnóstico; por ciclo: plan, revisión, [plan, revisión], parche.
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
from constructor_ia.oficina import reparacion_coordinada_9 as RC9
from constructor_ia.oficina import reparacion_coordinada_10 as RC10
from constructor_ia.oficina import reparacion_coordinada_11 as RC11
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
from .test_oficina_reparacion_coordinada_10 import PLAN_OK as PLAN_OK_10
from .test_oficina_reparacion_guiada import INEXISTENTE

F = "conciliar_movimientos"
ZONAS = [(27, 37), (41, 46)]


def diag11(**cambios):
    d = {"causa": "las anulaciones aplicadas no quedan registradas en aplicados",
         "origen_del_defecto": {"linea": 'if tipo != "anulacion":',
                                "descripcion": "la escritura en aplicados excluye a las anulaciones"},
         "punto_de_manifestacion": {"linea": 'ref = aplicados.get(m.get("anula"))',
                                    "descripcion": "la anulación referida no se encuentra y ref queda en None"},
         "cadena_causal": ["la anulación 3 se aplica pero no se registra", "la anulación 4 la busca y no la encuentra",
                           "sale anulacion_inexistente"],
         "comportamiento_a_preservar": ["los ingresos y egresos aplicados siguen registrados"]}
    d.update(cambios)
    return json.dumps({k: v for k, v in d.items() if v is not None}, ensure_ascii=False)


def plan11(linea, accion, cambio=None, **otros):
    d = {"estado_a_corregir": "aplicados no contiene las anulaciones aplicadas; tendría que contenerlas",
         "intervenciones": [{"linea": linea, "accion": accion}],
         "por_que_elimina_la_causa": "corrige la escritura donde se origina el estado, no la búsqueda posterior",
         "invariantes_preservados": ["los ingresos y egresos siguen registrados en aplicados"]}
    if cambio:
        d["cambio_respecto_del_anterior"] = cambio
    d.update(otros)
    return json.dumps({k: v for k, v in d.items() if v is not None}, ensure_ascii=False)


PLAN_OK = plan11('if tipo != "anulacion":', "quitar la condición para registrar todos los movimientos aplicados")
PLAN_OK_VARIANTE = plan11('if tipo != "anulacion":', "Registrar TODOS los movimientos aplicados, quitando la condición.")
PLAN_ALT = plan11('aplicados[mid] = {"id": mid, "tipo": tipo, "producto": prod, "efecto": efecto}',
                  "hacer que esta escritura se ejecute para cada movimiento aplicado, sin depender del tipo")
PLAN_ALT_2 = plan11('aplicados[mid] = {"id": mid, "tipo": tipo, "producto": prod, "efecto": efecto}',
                    "hacer que esta escritura se ejecute para cada movimiento aplicado, sin depender del tipo",
                    cambio="otro enfoque sobre la misma escritura")
# Variante TEXTUAL del parche que invierte la condición: otro texto, el mismo programa.
INVERTIR_VARIANTE = dict(INVERTIR, reemplazo='if tipo=="anulacion":')
PLAN_SINTOMA = plan11("if ref is None:", "tratar el None como anulación de anulación cuando falta la referida")
PLAN_VISTOS = plan11('ref = aplicados.get(m.get("anula"))',
                     "buscar la anulación referida en vistos en lugar de aplicados")
PLAN_DESCRIPTIVO = plan11('if tipo != "anulacion":', "arreglarlo")
CRITICA_VISTOS = ("[CRÍTICA-v1] vistos contiene también movimientos ignorados; anular uno ignorado no está permitido por "
                  "el contrato: el plan confunde movimientos vistos con aplicados")
RECHAZA_VISTOS = revision("rechazar", CRITICA_VISTOS, elimina=False, preserva=False)
RECHAZA = revision("rechazar", "[CRÍTICA-r] no me convence", elimina=False, preserva=True)
FUERA_DE_ZONA = parche({"original": "    saldos = dict(saldos_iniciales or {})", "reemplazo": "    saldos = {}"})


class UnidadesTests(unittest.TestCase):
    def test_origin_and_manifestation_are_separated(self):
        d = RC11.interpretar_diagnostico(diag11(), A, ZONAS, F)
        self.assertEqual((d["origen_del_defecto"]["lineas"], d["punto_de_manifestacion"]["lineas"]), ([44], [27]))
        t = RC11.texto_diagnostico(d)
        self.assertIn('Origen del defecto: «if tipo != "anulacion":» (línea 44)', t)
        self.assertIn('Punto de manifestación: «ref = aplicados.get(m.get("anula"))» (línea 27)', t)
        mismo = {"linea": 'if tipo != "anulacion":', "descripcion": "x"}
        for cambios, motivo in (({"punto_de_manifestacion": mismo}, "la misma línea"),
                                ({"origen_del_defecto": {"linea": "saldos = dict(saldos_iniciales or {})",
                                                         "descripcion": "x"}}, "origen_del_defecto.linea"),
                                ({"cadena_causal": ["uno"]}, "cadena_causal"),
                                ({"comportamiento_a_preservar": []}, "comportamiento_a_preservar")):
            with self.subTest(motivo=motivo):
                with self.assertRaisesRegex(ValueError, motivo):
                    RC11.interpretar_diagnostico(diag11(**cambios), A, ZONAS, F)

    def test_a_concrete_plan_versus_a_merely_descriptive_one(self):
        p = RC11.interpretar_plan(PLAN_OK, A, ZONAS, F, False)
        self.assertEqual(p["intervenciones"][0]["lineas"], [44])
        for texto in (PLAN_DESCRIPTIVO, plan11("if algo_que_no_existe:", "una acción suficientemente larga"),
                      plan11('if tipo != "anulacion":', "quitar la condición", intervenciones=[])):
            with self.assertRaisesRegex(ValueError, "plan descriptivo"):
                RC11.interpretar_plan(texto, A, ZONAS, F, False)
        with self.assertRaisesRegex(ValueError, "cambio_respecto_del_anterior"):
            RC11.interpretar_plan(PLAN_OK, A, ZONAS, F, True)

    def test_textual_variants_of_the_same_intervention_are_the_same_plan(self):
        a = RC11.interpretar_plan(PLAN_OK, A, ZONAS, F, False)
        b = RC11.interpretar_plan(PLAN_OK_VARIANTE, A, ZONAS, F, False)
        c = RC11.interpretar_plan(PLAN_ALT, A, ZONAS, F, False)
        self.assertEqual(RC11.clave_plan(a), RC11.clave_plan(b))
        self.assertNotEqual(RC11.clave_plan(a), RC11.clave_plan(c))

    def test_no_known_solution_in_the_prompts(self):
        causa = RGD.CAUSAS[(F, "anulaciones_no_registradas")]["texto"]
        for texto in (RC11.SISTEMA_DIAGNOSTICO_11, RC11.SISTEMA_PLANIFICADOR_11):
            for prohibido in ("if True", "!=", "aplicados", "anulacion", "vistos", causa):
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

    def correr(self, respuestas, mod=RC11, **k):
        fake = FakeOllama(respuestas, models=("gemma4:12b", "qwen2.5-coder:7b"))
        self.addCleanup(fake.close)
        f = RC.preparar(self.origen)
        destino = mod.carpeta(Path(tempfile.mkdtemp(prefix="cia-exp711-")), self.origen)
        inf = mod.correr(destino, f, self.ex, OllamaClient(fake.url), RM.compuerta(fake.url),
                         PG.modelo_remoto("gemma4:12b", num_predict=RE.NUM_PREDICT_REVISOR, timeout_s=600),
                         PG.modelo_remoto("qwen2.5-coder:7b"), **k)
        return inf, fake, destino

    def usuario(self, fake, i):
        return fake.requests[i]["messages"][1]["content"]

    def test_origin_and_manifestation_reach_the_planner_and_are_measured(self):
        with mock.patch.dict(RGD.CAUSAS, {}, clear=True):
            inf, fake, d = self.correr([diag11(), PLAN_OK, APRUEBA, parche(QWEN)])
        self.assertTrue(inf["exito"], inf["motivo"])
        self.assertIn("Origen del defecto:", self.usuario(fake, 1))
        self.assertIn("Punto de manifestación:", self.usuario(fake, 1))
        m = inf["medicion"]
        self.assertEqual(m["posteriores_deterministas_aproximadas"],
                         {"1_diagnostico_identifica_el_origen": True, "2_diagnostico_identifica_la_manifestacion": True,
                          "3_plan_propone_intervencion_causal_concreta": True, "4_plan_declara_invariantes": True})
        self.assertEqual(m["demostrado_por_ejecucion"],
                         {"4_plan_preserva_invariantes": True, "6_parche_implementa_el_plan": True,
                          "parche_aplicado": True, "7_parche_supera_el_caso_sintetico": True,
                          "8_bateria_independiente_9_9": True})
        self.assertEqual(m["opinion_del_modelo"]["5_revision_de_gemma"], "aprobar")
        self.assertTrue(m["plan_causal_correcto"])
        self.assertEqual(inf["referencias_posteriores"], {"origen": [44, 45], "manifestacion": list(range(27, 37))})
        self.assertEqual((inf["verificacion"]["aprobados"], inf["verificacion"]["total"]), (9, 9))

    def test_a_descriptive_plan_is_not_reviewed(self):
        inf, fake, _ = self.correr([diag11(), PLAN_DESCRIPTIVO, PLAN_OK, APRUEBA, parche(QWEN)])
        self.assertEqual([r["model"] for r in fake.requests],
                         ["gemma4:12b", "qwen2.5-coder:7b", "qwen2.5-coder:7b", "gemma4:12b", "qwen2.5-coder:7b"])
        self.assertIn("plan descriptivo", inf["ciclos"][0]["planes"][0]["motivo"])
        self.assertIn("Tu plan 1 no se pudo usar (plan descriptivo", self.usuario(fake, 2))
        self.assertTrue(inf["exito"])

    def test_a_plan_confusing_seen_with_applied_is_rejected_and_the_critique_arrives_in_full(self):
        inf, fake, _ = self.correr([diag11(), PLAN_VISTOS, RECHAZA_VISTOS, PLAN_OK, APRUEBA, parche(QWEN)])
        p1 = inf["ciclos"][0]["planes"][0]
        self.assertEqual(p1["revision"]["decision"], "rechazar")
        esperado = RC10.texto_critica_plan(1, RC9.interpretar_revision(RECHAZA_VISTOS, F))
        self.assertIn(esperado, self.usuario(fake, 3))
        self.assertIn(CRITICA_VISTOS, self.usuario(fake, 3))
        self.assertEqual(p1["critica_enviada"], esperado)
        self.assertTrue(inf["exito"])

    def test_gemma_rejects_a_correct_plan_and_execution_decides(self):
        inf, fake, d = self.correr([diag11(), PLAN_OK, RECHAZA, PLAN_ALT, RECHAZA, parche(QWEN)])
        c1 = inf["ciclos"][0]
        self.assertEqual(c1["plan_a_implementar"], {"plan": 2, "estado": "rechazado por la revisora: se implementa bajo "
                                                    "evaluación experimental", "bajo_evaluacion_experimental": True})
        self.assertTrue(inf["exito"], "el resultado lo decide la ejecución, no el rechazo")
        self.assertEqual((inf["verificacion"]["aprobados"], inf["verificacion"]["total"]), (9, 9))
        self.assertIn("implementado bajo evaluación experimental", inf["clasificacion"])
        self.assertEqual(inf["medicion"]["desacuerdos_revisora_vs_ejecucion"],
                         [{"ciclo": 1, "plan": 2, "revisora": "rechazar", "ejecucion": "aprobó la batería",
                           "tipo": "rechazó un plan cuya implementación aprobó la batería"}])
        self.assertEqual(json.loads((d / "ciclo-1/parche/parche.json").read_text())["estado_del_plan"],
                         "rechazado por la revisora: se implementa bajo evaluación experimental")

    def test_gemma_approves_an_incorrect_plan_and_execution_contradicts_it(self):
        inf, fake, _ = self.correr([diag11(), PLAN_SINTOMA, APRUEBA, parche(NO_CAUSA),
                                    plan11('if tipo != "anulacion":', "quitar la condición para registrar todo lo aplicado",
                                           cambio="el anterior trataba el síntoma"), APRUEBA, parche(QWEN)])
        pa1 = inf["ciclos"][0]["parche"]
        self.assertEqual((pa1["aplicado"], pa1["discrepancia_sintetica"]), (True, "persiste"))
        self.assertIn({"ciclo": 1, "plan": 1, "revisora": "aprobar", "ejecucion": "no corrigió el defecto",
                       "tipo": "aprobó un plan cuya implementación falló"},
                      inf["medicion"]["desacuerdos_revisora_vs_ejecucion"])
        self.assertFalse(inf["ciclos"][0]["planes"][0]["evaluacion"]["plan_causal_correcto"])
        plan_c2 = self.usuario(fake, 4)
        self.assertIn("DISCREPANCIA PERSISTE", plan_c2)
        self.assertIn("En tu nuevo plan explicá qué cambia respecto de este intento.", plan_c2)
        self.assertTrue(inf["exito"])

    def test_a_rejected_plan_never_skips_deterministic_controls(self):
        inf, fake, d = self.correr([diag11(), PLAN_OK, RECHAZA, PLAN_ALT, RECHAZA, FUERA_DE_ZONA])
        pa = inf["ciclos"][0]["parche"]
        self.assertTrue(inf["ciclos"][0]["plan_a_implementar"]["bajo_evaluacion_experimental"])
        self.assertFalse(pa["aplicado"])
        self.assertIn("fuera de las zonas autorizadas", pa["motivo"])
        self.assertFalse((d / "ciclo-1/parche/solucion.py").exists())
        self.assertFalse(inf["exito"])

    def test_out_of_zone_changes_are_rejected_with_an_approved_plan_too(self):
        inf, _, _ = self.correr([diag11(), PLAN_OK, APRUEBA, FUERA_DE_ZONA])
        self.assertIn("fuera de las zonas autorizadas", inf["ciclos"][0]["parche"]["motivo"])

    def test_isolation_and_hidden_battery(self):
        inf, fake, _ = self.correr([diag11(), PLAN_SINTOMA, APRUEBA, parche(NO_CAUSA),
                                    plan11('if tipo != "anulacion":', "quitar la condición para registrar todo lo aplicado",
                                           cambio="otro"), RECHAZA, PLAN_ALT_2, RECHAZA, parche(QWEN)])
        todo = json.dumps(fake.requests, ensure_ascii=False)
        for prohibido in ("Caso oculto", "anulaciones_no_registradas", REFERENCIA_CONCILIAR, "9/9",
                          RE.DEFECTOS[F]["anulaciones_no_registradas"]["explicacion"], "referencias_posteriores",
                          "manifestacion\": [", "evaluación experimental"):
            self.assertNotIn(prohibido, todo)
        for c in MISIONES[F][0]()["spec"]["cases"]:
            if not c.get("visible", True) and c.get("categoria"):
                self.assertNotIn(c["categoria"], todo)
        for r in fake.requests:                         # las revisiones del plan no reciben resultados de pruebas
            if r["messages"][0]["content"] == RC11.SISTEMA_REVISORA_PLAN_11:
                self.assertNotIn("DISCREPANCIA PERSISTE", r["messages"][1]["content"])
        self.assertTrue(inf["auditoria_de_fugas"]["sin_fugas"])
        self.assertTrue(inf["exito"])

    def test_repeated_plans_and_patches_are_detected(self):
        inf, fake, _ = self.correr([diag11(), PLAN_OK, RECHAZA, PLAN_OK_VARIANTE, parche(INVERTIR)]
                                   + [plan11('if tipo != "anulacion":', "invertir la condición para que pasen las anulaciones",
                                             cambio="otro"), APRUEBA, parche(INVERTIR_VARIANTE)] * 2)
        p2 = inf["ciclos"][0]["planes"][1]
        self.assertEqual(p2["repite_el_plan"], "ciclo 1, plan 1", "una variante textual de la misma intervención")
        self.assertIsNone(p2["revision"]["decision"], "un plan repetido no gasta una revisión")
        self.assertIn("propone la MISMA intervención", p2["critica_enviada"])
        self.assertTrue(inf["ciclos"][0]["plan_a_implementar"]["bajo_evaluacion_experimental"])
        self.assertEqual(inf["ciclos"][1]["parche"]["mismo_programa_que_el_ciclo"], 1)
        self.assertIn("repite uno anterior después del aviso", inf["detenido"])
        self.assertEqual(inf["medicion"]["planes_repetidos"], 2)
        self.assertFalse(inf["exito"])

    def test_limits(self):
        inf, fake, _ = self.correr([diag11()] + [PLAN_DESCRIPTIVO] * 10)
        self.assertEqual(len(fake.requests), 1 + RC11.MAX_PLANES)
        self.assertFalse(inf["exito"])
        inf, fake, _ = self.correr([diag11()], max_segundos=0)
        self.assertEqual(fake.requests, [])

    def test_previous_experiments_still_work(self):
        inf, _, _ = self.correr([gemma8(), PLAN_OK_10, APRUEBA, parche(QWEN)], mod=RC10)
        self.assertTrue(inf["exito"])
        inf, _, _ = self.correr([gemma8(), parche(QWEN), APRUEBA], mod=RC9)
        self.assertTrue(inf["exito"])

    def test_cli(self):
        lab = Path(tempfile.mkdtemp(prefix="cia-exp711-cli-")) / "oficina-coordinada-7-11"
        fake = FakeOllama([diag11(), PLAN_OK, RECHAZA, PLAN_ALT, RECHAZA, parche(QWEN)],
                          models=("gemma4:12b", "qwen2.5-coder:7b"))
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
                rc = cli.main(["--config", str(lab.parent / "no-existe.toml"), "oficina", "reparar-coordinado-11",
                               "--corrida", str(self.origen), "--carpeta", str(lab), "--trabajador", "prueba", *extra])
            return rc, out.getvalue() + err.getvalue()
        rc, out = cli_("--mostrar-pedido")
        self.assertEqual(rc, 0, out)
        self.assertIn("PUNTO DE MANIFESTACIÓN", out)
        self.assertEqual(fake.requests, [])
        rc, out = cli_("--autorizo-inferencia-remota")
        self.assertEqual(rc, 0, out)
        self.assertIn("RECHAZADO por la revisora: se implementa bajo evaluación experimental", out)
        self.assertIn("8_bateria_independiente_9_9: sí", out)
        self.assertIn("desacuerdos_revisora_vs_ejecucion: 1", out)
        self.assertIn("Experimento 7.11: ÉXITO", out)


if __name__ == "__main__":
    unittest.main()
