"""Experimento 7.8 (OFICINA.md, «Experimento 7.8»): diagnóstico causal con preservación de comportamiento.

SIN inferencias reales (diagnosticadora y programador simulados; sandbox real). Lo central: «comportamiento_a_preservar»
sale de la respuesta de la diagnosticadora y llega completo al programador; un parche que invierte la condición (lo
que hizo Qwen en el 7.7) recibe solo la diferencia observable del caso; no hay fugas; el 7.7 sigue igual.
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
from .test_oficina_reparacion_coordinada import gemma
from .test_oficina_reparacion_guiada import INEXISTENTE

F = "conciliar_movimientos"
ZONAS = [(27, 37), (41, 46)]
# Lo que propuso Qwen tres veces en el 7.7.
INVERTIR = {"original": 'if tipo != "anulacion":', "reemplazo": 'if tipo == "anulacion":'}


def gemma8(marca="PRESERVAR-x9", **cambios):
    d = json.loads(gemma("MARCA-8"))
    d.update(comportamiento_defectuoso="las anulaciones aplicadas no quedan en aplicados",
             comportamiento_a_preservar=[f"[{marca}] los ingresos y egresos aplicados tienen que seguir en aplicados",
                                         "una anulación de un egreso aplicado tiene que seguir funcionando"])
    d.update(cambios)
    return json.dumps({k: v for k, v in d.items() if v is not None}, ensure_ascii=False)


class DiagnosticoTests(unittest.TestCase):
    def test_the_fields_come_from_the_diagnosticians_answer(self):
        d = RC8.interpretar(gemma8("MARCA-ÚNICA-1"), A, ZONAS, F)
        self.assertEqual(d["comportamiento_a_preservar"][0],
                         "[MARCA-ÚNICA-1] los ingresos y egresos aplicados tienen que seguir en aplicados")
        texto = RC8.texto_para_el_programador(d)
        self.assertIn("Comportamiento a preservar (la corrección no debe romperlo):", texto)
        self.assertIn("    - [MARCA-ÚNICA-1] los ingresos y egresos aplicados tienen que seguir en aplicados", texto)
        self.assertIn("    - una anulación de un egreso aplicado tiene que seguir funcionando", texto)
        self.assertIn("Comportamiento defectuoso: las anulaciones aplicadas no quedan en aplicados", texto)

    def test_insufficient_diagnoses_are_rejected(self):
        for cambios, motivo in (({"comportamiento_a_preservar": None}, "comportamiento_a_preservar"),
                                ({"comportamiento_a_preservar": []}, "comportamiento_a_preservar"),
                                ({"comportamiento_a_preservar": ["  "]}, "comportamiento_a_preservar"),
                                ({"comportamiento_defectuoso": ""}, "comportamiento_defectuoso"),
                                ({"causa": None}, "falta «causa»")):
            with self.subTest(motivo=motivo, cambios=cambios):
                with self.assertRaisesRegex(ValueError, motivo):
                    RC8.interpretar(gemma8(**cambios), A, ZONAS, F)
        with self.assertRaisesRegex(ValueError, "comportamiento_"):
            RC8.interpretar(gemma(), A, ZONAS, F)          # un diagnóstico del 7.7 no alcanza para el 7.8

    def test_no_prefabricated_solution_in_the_prompts(self):
        causa = RGD.CAUSAS[(F, "anulaciones_no_registradas")]["texto"]
        for texto in (RC8.SISTEMA_DIAGNOSTICO_8, RC8.PEDIDO_8):
            for prohibido in ("if True", "!=", "==", "aplicados", "anulacion", causa, "condición que", "reemplaz"):
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

    def correr(self, respuestas, ocho=True, **k):
        fake = FakeOllama(respuestas, models=("gemma4:12b", "qwen2.5-coder:7b"))
        self.addCleanup(fake.close)
        f = RC.preparar(self.origen)
        mod = RC8 if ocho else RC
        destino = mod.carpeta(Path(tempfile.mkdtemp(prefix="cia-exp78-")), self.origen)
        inf = mod.correr(destino, f, self.ex, OllamaClient(fake.url), RM.compuerta(fake.url),
                         PG.modelo_remoto("gemma4:12b", num_predict=RE.NUM_PREDICT_REVISOR, timeout_s=600),
                         PG.modelo_remoto("qwen2.5-coder:7b"), **k)
        return inf, fake, destino

    def usuario(self, fake, i):
        return fake.requests[i]["messages"][1]["content"]

    def test_the_programmer_receives_the_preserved_behavior_and_is_asked_to_justify(self):
        with mock.patch.dict(RGD.CAUSAS, {}, clear=True):
            inf, fake, d = self.correr([gemma8("PRESERVAR-q7"), parche(QWEN)])
        self.assertEqual(fake.requests[0]["messages"][0]["content"], RC8.SISTEMA_DIAGNOSTICO_8)
        programador = self.usuario(fake, 1)
        self.assertIn("[PRESERVAR-q7] los ingresos y egresos aplicados tienen que seguir en aplicados", programador)
        self.assertIn("Comportamiento defectuoso:", programador)
        self.assertTrue(programador.endswith(RC8.PEDIDO_8))
        self.assertNotIn(RGD.PEDIDO, programador)
        self.assertIn("[PRESERVAR-q7]", inf["diagnostico_usado"]["texto"])
        self.assertTrue(inf["diagnostico_en_cada_pedido_del_programador"])

    def test_successful_repair_nine_of_nine(self):
        inf, fake, d = self.correr([gemma8(), parche(QWEN, justificacion="registra todo y no rompe ingresos")])
        self.assertTrue(inf["exito"], inf["motivo"])
        self.assertEqual(inf["clasificacion"], RC8.CLASIFICACION_8 + " (con normalización de la sangría)")
        self.assertEqual((inf["reparacion"]["verificacion"]["aprobados"], inf["reparacion"]["verificacion"]["total"]),
                         (9, 9))
        self.assertIn("preservación de comportamiento", inf["experimento"])
        self.assertIn("experimento-7-8", str(d))
        p = json.loads((d / "reparacion/intento-1/parche.json").read_text())
        self.assertEqual(p["justificacion"], "registra todo y no rompe ingresos", "la justificación queda registrada")
        for rel in ("diagnostico-1/respuesta.txt", "diagnostico-1/diagnostico.json", "reparacion/informe.json",
                    "informe.json"):
            self.assertTrue((d / rel).exists(), rel)

    def test_an_incorrect_patch_gets_only_the_observable_difference_next_time(self):
        inf, fake, d = self.correr([gemma8(), parche(INVERTIR), parche(QWEN)])
        self.assertTrue(inf["exito"])
        r1 = inf["reparacion"]["intentos"][0]
        self.assertEqual((r1["estado"], r1["discrepancia_sintetica"], r1["sangria_normalizada"]),
                         ("aplicado", "persiste", True))
        segundo = self.usuario(fake, 2)
        for texto in ("Tu parche del intento 1 se aplicó (el coordinador ajustó solo su sangría), pero la "
                      "DISCREPANCIA PERSISTE en el caso sintético.", "Lo que pide el contrato:",
                      "Lo que devuelve el código con tu parche:", "Claves distintas:",
                      "Además no cumple los ejemplos visibles del contrato:"):
            self.assertIn(texto, segundo)
        for otro in ("RESULTADO DE TU INTENTO", "QUÉ CAMBIASTE", "SIN EFECTO EN ESTE CASO", "Caso oculto", "9/9"):
            self.assertNotIn(otro, segundo)
        registrada = (d / "reparacion/intento-1/retroalimentacion.md").read_text()
        self.assertIn(registrada, segundo, "lo registrado es lo enviado")

    def test_three_incorrect_patches_stop(self):
        inf, fake, _ = self.correr([gemma8()] + [parche(INVERTIR)] * 3 + [parche(QWEN)])
        self.assertEqual(len(fake.requests), 4, "1 diagnóstico + 3 parches")
        self.assertFalse(inf["exito"])
        self.assertIn("ningún parche corrigió el defecto en 3 intentos", inf["motivo"])

    def test_insufficient_diagnoses_stop_without_the_programmer(self):
        inf, fake, d = self.correr([gemma8(comportamiento_a_preservar=None), gemma(), parche(QWEN)])
        self.assertEqual([r["model"] for r in fake.requests], ["gemma4:12b", "gemma4:12b"])
        self.assertIn("comportamiento_a_preservar", inf["diagnosticos"][0]["motivo"])
        self.assertIn("Tu diagnóstico anterior no se pudo usar (falta «comportamiento_a_preservar»",
                      self.usuario(fake, 1))
        self.assertIsNone(inf["reparacion"])
        self.assertFalse(inf["exito"])

    def test_no_hidden_cases_leak(self):
        inf, fake, _ = self.correr([gemma8(), parche(INVERTIR), parche(QWEN)])
        self.assertTrue(inf["reparacion"]["auditoria_de_fugas"]["sin_fugas"])
        todo = json.dumps(fake.requests, ensure_ascii=False)
        for prohibido in ("Caso oculto", "anulaciones_no_registradas", REFERENCIA_CONCILIAR,
                          RE.DEFECTOS[F]["anulaciones_no_registradas"]["explicacion"]):
            self.assertNotIn(prohibido, todo)
        for c in MISIONES[F][0]()["spec"]["cases"]:
            if not c.get("visible", True) and c.get("categoria"):
                self.assertNotIn(c["categoria"], todo)

    def test_7_7_remains_intact(self):
        inf, fake, d = self.correr([gemma("MARCA-77"), parche(QWEN)], ocho=False)
        self.assertTrue(inf["exito"])
        self.assertEqual(inf["clasificacion"], "diagnóstico y reparación automáticos coordinados (con normalización de "
                                               "la sangría)")
        self.assertEqual(fake.requests[0]["messages"][0]["content"], RC.SISTEMA_DIAGNOSTICO)
        programador = self.usuario(fake, 1)
        self.assertTrue(programador.endswith(RGD.PEDIDO))
        self.assertNotIn("Comportamiento a preservar", programador)
        self.assertIn("experimento-7-7", str(d))
        # El 7.7 sigue usando la retroalimentación del 7.1.
        inf, fake, _ = self.correr([gemma(), parche(INVERTIR), parche(QWEN)], ocho=False)
        self.assertIn("RESULTADO DE TU INTENTO 1", self.usuario(fake, 2))

    def test_cli(self):
        lab = Path(tempfile.mkdtemp(prefix="cia-exp78-cli-")) / "oficina-coordinada-7-8"
        fake = FakeOllama([gemma8(), parche(QWEN)], models=("gemma4:12b", "qwen2.5-coder:7b"))
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
                rc = cli.main(["--config", str(lab.parent / "no-existe.toml"), "oficina", "reparar-coordinado-8",
                               "--corrida", str(self.origen), "--carpeta", str(lab), "--trabajador", "prueba", *extra])
            return rc, out.getvalue() + err.getvalue()
        rc, out = cli_("--mostrar-pedido")
        self.assertEqual(rc, 0, out)
        self.assertIn("COMPORTAMIENTO A PRESERVAR", out)
        self.assertEqual(fake.requests, [])
        rc, out = cli_("--autorizo-inferencia-remota")
        self.assertEqual(rc, 0, out)
        self.assertIn("Experimento 7.8: ÉXITO (diagnóstico y reparación automáticos coordinados, con preservación de "
                      "comportamiento", out)
        self.assertEqual(len(list((lab / "experimento-7-8").glob("reparacion-*/informe.json"))), 1)


if __name__ == "__main__":
    unittest.main()
