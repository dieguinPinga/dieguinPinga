"""Experimento 7.9 (OFICINA.md, «Experimento 7.9»): revisión crítica de parches entre agentes.

SIN inferencias reales: diagnosticadora, programador y revisora simulados (un Ollama falso con gemma y qwen) y el
sandbox real. Orden de los pedidos: diagnóstico (gemma), parche 1 (qwen), revisión 1 (gemma), parche 2, revisión 2…
Se mide la capacidad crítica de la revisora contra lo observable y la batería: no se asume que tenga razón.
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
from constructor_ia.oficina import reparacion_guiada as RGD
from constructor_ia.oficina import revision_existente as RE
from constructor_ia.oficina.misiones_programacion import MISIONES, REFERENCIA_CONCILIAR
from constructor_ia.ollama import OllamaClient

from .fake_ollama import FakeOllama
from .helpers import bwrap_works
from .test_oficina_lectura_escritura import diagnostico_real
from .test_oficina_normalizacion_sangria import QWEN
from .test_oficina_parches import parche
from .test_oficina_reparacion_coordinada import gemma
from .test_oficina_reparacion_coordinada_8 import INVERTIR, gemma8
from .test_oficina_reparacion_guiada import INEXISTENTE, ROMPE_OTRA_REGLA

F = "conciliar_movimientos"
# Lo que propuso Qwen tres veces en el 7.8: no toca la escritura en «aplicados».
NO_CAUSA = {"original": "if ref is None:", "reemplazo": "if ref is None or ref['tipo'] == 'anulacion':"}


def revision(decision, critica="", elimina=None, preserva=None, justificacion="análisis causal del parche"):
    d = {"decision": decision, "justificacion_causal": justificacion, "critica": critica}
    if elimina is not None:
        d["elimina_la_causa"] = elimina
    if preserva is not None:
        d["preserva_comportamiento"] = preserva
    return json.dumps(d, ensure_ascii=False)


APRUEBA = revision("aprobar", elimina=True, preserva=True)
CRITICA_CAUSA = ("[CRÍTICA-7c2] el parche cambia la condición de búsqueda pero la anulación referida sigue sin estar "
                 "registrada: la escritura que la registraría no cambió, así que ref sigue siendo None")
CRITICA_PRESERVAR = ("[CRÍTICA-p41] el parche registra las anulaciones pero deja de registrar los ingresos y egresos, "
                     "que tienen que seguir registrados")
RECHAZA_CAUSA = revision("rechazar", CRITICA_CAUSA, elimina=False, preserva=True)
RECHAZA_PRESERVAR = revision("rechazar", CRITICA_PRESERVAR, elimina=True, preserva=False)


class InterpretarTests(unittest.TestCase):
    def test_structured_decision(self):
        r = RC9.interpretar_revision(RECHAZA_CAUSA, F)
        self.assertEqual((r["decision"], r["elimina_la_causa"], r["preserva_comportamiento"]), ("rechazar", False, True))
        t = RC9.texto_critica(2, r)
        self.assertIn("REVISIÓN DE TU PARCHE DEL INTENTO 2 (otra IA, la revisora; NO es un resultado de pruebas): "
                      "RECHAZADO.", t)
        self.assertIn("Crítica: " + CRITICA_CAUSA, t)

    def test_invalid_reviews(self):
        for texto, motivo in (("no es JSON", "objeto JSON"), (revision("tal vez"), "«decision»"),
                              (revision("rechazar"), "necesita «critica»"),
                              (revision("aprobar", justificacion=""), "justificacion_causal"),
                              ("def conciliar_movimientos(m):\n  return {}", "incluyó el programa")):
            with self.subTest(motivo=motivo):
                with self.assertRaisesRegex(ValueError, motivo):
                    RC9.interpretar_revision(texto, F)


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

    def correr(self, respuestas, mod=RC9, **k):
        fake = FakeOllama(respuestas, models=("gemma4:12b", "qwen2.5-coder:7b"))
        self.addCleanup(fake.close)
        f = RC.preparar(self.origen)
        destino = mod.carpeta(Path(tempfile.mkdtemp(prefix="cia-exp79-")), self.origen)
        inf = mod.correr(destino, f, self.ex, OllamaClient(fake.url), RM.compuerta(fake.url),
                         PG.modelo_remoto("gemma4:12b", num_predict=RE.NUM_PREDICT_REVISOR, timeout_s=600),
                         PG.modelo_remoto("qwen2.5-coder:7b"), **k)
        return inf, fake, destino

    def usuario(self, fake, i):
        return fake.requests[i]["messages"][1]["content"]

    def decisiones(self, inf):
        return inf["capacidad_critica_de_la_revisora"]["decisiones"]

    # 1 y 2: la revisora detecta ------------------------------------------------------------------------------------
    def test_1_the_reviewer_detects_a_patch_that_does_not_change_the_cause(self):
        inf, fake, d = self.correr([gemma8(), parche(NO_CAUSA), RECHAZA_CAUSA, parche(QWEN), APRUEBA])
        self.assertEqual([r["model"] for r in fake.requests],
                         ["gemma4:12b", "qwen2.5-coder:7b", "gemma4:12b", "qwen2.5-coder:7b", "gemma4:12b"])
        self.assertEqual(fake.requests[2]["messages"][0]["content"], RC9.SISTEMA_REVISORA)
        i1 = inf["reparacion"]["intentos"][0]
        self.assertEqual((i1["estado"], i1["discrepancia_sintetica"]), ("rechazado_por_revisora", "persiste"))
        self.assertTrue(self.decisiones(inf)[0]["deteccion_correcta"])
        self.assertTrue(inf["exito"])
        self.assertTrue((d / "reparacion/intento-1/revision.json").exists())

    def test_2_the_reviewer_detects_a_patch_that_breaks_preserved_behavior(self):
        inf, _, _ = self.correr([gemma8(), parche(INVERTIR), RECHAZA_PRESERVAR, parche(QWEN), APRUEBA])
        i1 = inf["reparacion"]["intentos"][0]
        self.assertFalse(i1["visibles"]["ok"], "el parche rompe un ejemplo visible")
        r = i1["revision"]
        self.assertEqual((r["decision"], r["preserva_comportamiento"]), ("rechazar", False))
        self.assertTrue(self.decisiones(inf)[0]["deteccion_correcta"])

    # 3: la crítica llega íntegra -----------------------------------------------------------------------------------
    def test_3_the_critique_reaches_the_next_attempt_in_full(self):
        _, fake, d = self.correr([gemma8(), parche(NO_CAUSA), RECHAZA_CAUSA, parche(QWEN), APRUEBA])
        esperado = RC9.texto_critica(1, RC9.interpretar_revision(RECHAZA_CAUSA, F))
        segundo = self.usuario(fake, 3)
        self.assertIn(esperado, segundo)
        self.assertIn(CRITICA_CAUSA, segundo)
        self.assertEqual((d / "reparacion/intento-1/retroalimentacion.md").read_text(), esperado)

    # 4: aprobar no evita la validación -----------------------------------------------------------------------------
    def test_4_an_approval_does_not_skip_independent_validation(self):
        inf, fake, _ = self.correr([gemma8(), parche(INVERTIR), APRUEBA, parche(QWEN), APRUEBA])
        i1 = inf["reparacion"]["intentos"][0]
        self.assertEqual((i1["estado"], i1["discrepancia_sintetica"]), ("aplicado", "persiste"))
        self.assertTrue(self.decisiones(inf)[0]["aprobacion_incorrecta"], "se registra que aprobó un parche defectuoso")
        self.assertIn("DISCREPANCIA PERSISTE", self.usuario(fake, 3))
        self.assertTrue(inf["exito"])
        inf, fake, _ = self.correr([gemma8(), ROMPE_OTRA_REGLA, APRUEBA, parche(QWEN), APRUEBA])
        self.assertEqual(len(fake.requests), 3, "la batería decide una sola vez")
        self.assertFalse(inf["reparacion"]["verificacion"]["ok"])
        self.assertTrue(self.decisiones(inf)[0]["aprobacion_incorrecta"])
        self.assertFalse(inf["exito"])

    # 5: tres propuestas incorrectas --------------------------------------------------------------------------------
    def test_5_three_incorrect_proposals_end_without_success(self):
        inf, fake, _ = self.correr([gemma8(), parche(NO_CAUSA), RECHAZA_CAUSA, parche(INVERTIR), RECHAZA_PRESERVAR,
                                    parche(NO_CAUSA), RECHAZA_CAUSA, parche(QWEN), APRUEBA])
        self.assertEqual(len(fake.requests), 7, "1 diagnóstico + 3 propuestas + 3 revisiones")
        self.assertFalse(inf["exito"])
        self.assertIn("ningún parche corrigió el defecto en 3 intentos", inf["motivo"])
        self.assertEqual(inf["capacidad_critica_de_la_revisora"]["detecciones_correctas"], 3)

    # 6: reparación correcta ----------------------------------------------------------------------------------------
    def test_6_a_correct_repair_passes_nine_of_nine(self):
        with mock.patch.dict(RGD.CAUSAS, {}, clear=True):
            inf, fake, d = self.correr([gemma8(), parche(QWEN), APRUEBA])
        self.assertTrue(inf["exito"], inf["motivo"])
        self.assertEqual((inf["reparacion"]["verificacion"]["aprobados"], inf["reparacion"]["verificacion"]["total"]),
                         (9, 9))
        self.assertTrue(inf["criterios"]["5_revisora_aprueba_el_parche_verificado"])
        self.assertEqual(inf["clasificacion"], RC9.CLASIFICACION_9 + " (con normalización de la sangría)")
        self.assertIn("experimento-7-9", str(d))

    def test_a_false_rejection_and_an_invalid_review_are_recorded(self):
        inf, _, _ = self.correr([gemma8(), parche(QWEN), revision("rechazar", "no me convence"), parche(QWEN), APRUEBA])
        self.assertTrue(self.decisiones(inf)[0]["rechazo_de_parche_observablemente_correcto"])
        self.assertTrue(inf["exito"])
        inf, _, _ = self.correr([gemma8(), parche(QWEN), "no es JSON"])
        self.assertTrue(inf["reparacion"]["exito"], "decidieron las comprobaciones deterministas")
        self.assertEqual(inf["capacidad_critica_de_la_revisora"]["revisiones_invalidas"], 1)
        self.assertFalse(inf["criterios"]["5_revisora_aprueba_el_parche_verificado"])
        self.assertFalse(inf["exito"], "sin una revisión válida no se cumple el criterio")

    # 7: sin fugas --------------------------------------------------------------------------------------------------
    def test_7_no_hidden_tests_leak_and_the_reviewer_gets_no_test_results(self):
        inf, fake, _ = self.correr([gemma8(), parche(INVERTIR), APRUEBA, parche(QWEN), APRUEBA])
        todo = json.dumps(fake.requests, ensure_ascii=False)
        for prohibido in ("Caso oculto", "anulaciones_no_registradas", REFERENCIA_CONCILIAR,
                          RE.DEFECTOS[F]["anulaciones_no_registradas"]["explicacion"], "9/9"):
            self.assertNotIn(prohibido, todo)
        for c in MISIONES[F][0]()["spec"]["cases"]:
            if not c.get("visible", True) and c.get("categoria"):
                self.assertNotIn(c["categoria"], todo)
        for i in (2, 4):                                   # las dos revisiones
            revision_pedido = self.usuario(fake, i)
            self.assertIn("DIAGNÓSTICO DE LA CAUSA", revision_pedido)
            self.assertIn("DIFERENCIA RESULTANTE EN EL CÓDIGO", revision_pedido)
            for resultado in ("DISCREPANCIA PERSISTE", "Lo que devuelve el código con tu parche",
                              "no cumple los ejemplos visibles", "Lo que pide el contrato"):
                self.assertNotIn(resultado, revision_pedido)
        self.assertTrue(inf["reparacion"]["auditoria_de_fugas"]["sin_fugas"])

    # 8: los anteriores siguen funcionando --------------------------------------------------------------------------
    def test_8_previous_experiments_still_work(self):
        inf, fake, _ = self.correr([gemma8(), parche(QWEN)], mod=RC8)
        self.assertTrue(inf["exito"])
        self.assertEqual(len(fake.requests), 2, "el 7.8 no revisa")
        self.assertNotIn("revision", inf["reparacion"]["intentos"][0])
        inf, fake, _ = self.correr([gemma(), parche(QWEN)], mod=RC)
        self.assertTrue(inf["exito"])
        self.assertEqual(inf["clasificacion"], "diagnóstico y reparación automáticos coordinados (con normalización "
                                               "de la sangría)")

    def test_cli(self):
        lab = Path(tempfile.mkdtemp(prefix="cia-exp79-cli-")) / "oficina-coordinada-7-9"
        fake = FakeOllama([gemma8(), parche(QWEN), APRUEBA], models=("gemma4:12b", "qwen2.5-coder:7b"))
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
                rc = cli.main(["--config", str(lab.parent / "no-existe.toml"), "oficina", "reparar-coordinado-9",
                               "--corrida", str(self.origen), "--carpeta", str(lab), "--trabajador", "prueba", *extra])
            return rc, out.getvalue() + err.getvalue()
        rc, out = cli_("--mostrar-pedido")
        self.assertEqual(rc, 0, out)
        self.assertIn("revisión crítica del parche", out)
        self.assertEqual(fake.requests, [])
        rc, out = cli_("--autorizo-inferencia-remota")
        self.assertEqual(rc, 0, out)
        self.assertIn("revisora: aprobar", out)
        self.assertIn("revisora: 0 detecciones correctas · 0 aprobaciones incorrectas", out)
        self.assertIn("Experimento 7.9: ÉXITO (diagnóstico, reparación y revisión crítica automáticos coordinados", out)


if __name__ == "__main__":
    unittest.main()
