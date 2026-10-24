"""Experimento 7.6 (OFICINA.md, «Experimento 7.6»): reparación guiada con normalización SEGURA de la sangría.

SIN inferencias reales. En el 7.5, Qwen propuso tres veces `if tipo != "anulacion":` → `if True:` sin sangría y los
tres parches se rechazaron. Con la sangría original el parche aprueba 9/9 (comprobado en la VivoBook). La
normalización es estrictamente de formato: nunca cambia el contenido de una línea y rechaza toda ambigüedad.
"""
import hashlib
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from constructor_ia import cli
from constructor_ia.oficina import parches as PA
from constructor_ia.oficina import programador as PG
from constructor_ia.oficina import registro as RG
from constructor_ia.oficina import remoto as RM
from constructor_ia.oficina import reparacion_guiada as RGD
from constructor_ia.oficina.autocorreccion import sin_comentarios
from constructor_ia.oficina.misiones_programacion import REFERENCIA_CONCILIAR, mision_conciliar_movimientos
from constructor_ia.ollama import OllamaClient

from .fake_ollama import FakeOllama
from .helpers import bwrap_works
from .test_oficina_hipotesis import A
from .test_oficina_parches import parche
from .test_oficina_reparacion_guiada import INEXISTENTE
from .test_oficina_lectura_escritura import diagnostico_real

ZONAS = [(27, 37), (41, 46)]
SPEC = mision_conciliar_movimientos()["spec"]
REGISTRO = 'aplicados[mid] = {"id": mid, "tipo": tipo, "producto": prod, "efecto": efecto}'
# La propuesta literal de Qwen en el 7.5 (sin sangría).
QWEN = {"original": 'if tipo != "anulacion":', "reemplazo": "if True:"}
HUELLA_7_3C = "5b017882482e69ee8ea18b17e3519d36458e002bba8b8686348991ce91f5a542"


def normalizar(*cambios):
    return RGD.normalizar_sangria(A, {"cambios": list(cambios), "justificacion": ""}, ZONAS)


class PositivasTests(unittest.TestCase):
    def test_the_real_defective_code_is_the_one_verified_in_the_vivobook(self):
        self.assertEqual(hashlib.sha256(A.encode()).hexdigest(), HUELLA_7_3C)

    def test_qwens_patch_only_gets_its_indentation(self):
        p, reg = normalizar(QWEN)
        self.assertEqual(p["cambios"], [{"original": '        if tipo != "anulacion":', "reemplazo": "        if True:"}])
        self.assertEqual((reg[0]["normalizado"], reg[0]["lineas"], reg[0]["desplazamiento"]), (True, [44, 44], 8))
        nuevo, _ = PA.aplicar(A, p, ZONAS, SPEC)
        self.assertEqual([ln for ln in nuevo.splitlines() if ln not in A.splitlines()], ["        if True:"],
                         "una sola línea distinta")

    def test_multiline_fragments_keep_their_relative_structure(self):
        p, reg = normalizar({"original": 'if tipo != "anulacion":\n    ' + REGISTRO, "reemplazo": REGISTRO})
        self.assertEqual(reg[0]["desplazamiento"], 8)
        nuevo, _ = PA.aplicar(A, p, ZONAS, SPEC)
        self.assertEqual(nuevo, sin_comentarios(REFERENCIA_CONCILIAR))

    def test_over_indented_fragments_shift_back(self):
        p, reg = normalizar({"original": '            if tipo != "anulacion":',
                             "reemplazo": "            if True:"})
        self.assertEqual((reg[0]["desplazamiento"], p["cambios"][0]["reemplazo"]), (-4, "        if True:"))

    def test_literal_matches_are_not_touched(self):
        exacto = {"original": '        if tipo != "anulacion":', "reemplazo": "        if True:"}
        p, reg = normalizar(exacto)
        self.assertEqual((p["cambios"], reg[0]["normalizado"]), ([exacto], False))

    def test_content_is_never_changed_only_leading_spaces(self):
        reemplazo = "if  True :  # x == 'y'\n    pass"
        p, _ = normalizar({"original": 'if tipo != "anulacion":', "reemplazo": reemplazo})
        self.assertEqual([ln.lstrip(" ") for ln in p["cambios"][0]["reemplazo"].splitlines()],
                         [ln.lstrip(" ") for ln in reemplazo.splitlines()])


class NegativasTests(unittest.TestCase):
    def rechaza(self, motivo, *cambios):
        with self.assertRaisesRegex(PA.ParcheRechazado, motivo):
            normalizar(*cambios)

    def test_logical_changes_are_not_normalized(self):
        for original in ('if tipo != "anulacion" :', "if tipo != 'anulacion':", 'if tipo == "anulacion"  :',
                         'if tipo!="anulacion":', 'if tipo != "anulación":'):
            with self.subTest(original=original):
                self.rechaza("no aparece en el código ni siquiera ajustando la sangría",
                             {"original": original, "reemplazo": "if True:"})

    def test_ambiguous_fragments_are_rejected(self):
        self.rechaza(r"aparece \d+ veces", {"original": "continue", "reemplazo": "pass"})
        self.rechaza(r"aparece \d+ veces", {"original": "                continue", "reemplazo": "pass"})

    def test_out_of_zone_fragments_are_rejected(self):
        self.rechaza("fuera de las zonas autorizadas",
                     {"original": "saldos = dict(saldos_iniciales or {})", "reemplazo": "saldos = {}"})
        self.rechaza("fuera de las zonas autorizadas",
                     {"original": 'mid, tipo = m["id"], m["tipo"]', "reemplazo": 'mid, tipo = m["id"], "x"'})

    def test_broken_relative_structure_is_rejected(self):
        self.rechaza("sangría relativa", {"original": 'if tipo != "anulacion":\n' + REGISTRO, "reemplazo": REGISTRO})

    def test_tabs_and_inconsistent_replacements_are_rejected(self):
        self.rechaza("tabulaciones", {"original": '\tif tipo != "anulacion":', "reemplazo": "\tif True:"})
        self.rechaza("misma sangría que la del original", {"original": 'if tipo != "anulacion":',
                                                           "reemplazo": "    if True:"})
        self.rechaza("sangría negativa", {"original": '            if tipo != "anulacion":',
                                          "reemplazo": "            if True:\n  pass"})

    def test_the_normalized_patch_still_goes_through_the_usual_validation(self):
        p, _ = normalizar({"original": 'if tipo != "anulacion":', "reemplazo": "if True"})
        with self.assertRaisesRegex(PA.ParcheRechazado, "no es válido"):
            PA.aplicar(A, p, ZONAS, SPEC)


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

    def correr(self, respuestas, normalizar=True):
        fake = FakeOllama(respuestas, models=("qwen2.5-coder:7b",))
        self.addCleanup(fake.close)
        f = RGD.preparar(self.origen)
        destino = RGD.carpeta_de_reparacion(Path(tempfile.mkdtemp(prefix="cia-exp76-")), self.origen,
                                            "experimento-7-6" if normalizar else "experimento-7-5")
        inf = RGD.correr(destino, f, self.ex, OllamaClient(fake.url), RM.compuerta(fake.url),
                         PG.modelo_remoto("qwen2.5-coder:7b"), normalizar=normalizar)
        return inf, fake, destino

    def test_qwens_patch_is_normalized_applied_and_passes_nine_of_nine_without_intervention(self):
        inf, fake, d = self.correr([parche(QWEN)] * 3)
        self.assertTrue(inf["exito"], inf["motivo"])
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual((inf["verificacion"]["aprobados"], inf["verificacion"]["total"]), (9, 9))
        self.assertEqual(inf["clasificacion"], "reparación automática con normalización de formato (sangría)")
        self.assertTrue(inf["intentos"][0]["sangria_normalizada"])
        reg = json.loads((d / "intento-1/parche.json").read_text())
        self.assertEqual(reg["cambios"], [QWEN], "el parche original queda registrado tal cual")
        self.assertEqual(reg["parche_normalizado"]["cambios"][0]["reemplazo"], "        if True:")
        self.assertIn("no aparece en el código", reg["rechazo_literal"])
        self.assertIn("sin cambiar su contenido", reg["normalizacion"][0]["justificacion"])
        self.assertIn("normalización segura de la sangría", inf["experimento"])
        self.assertTrue(inf["auditoria_de_fugas"]["sin_fugas"])
        self.assertNotIn("Caso oculto", fake.requests[0]["messages"][1]["content"])

    def test_without_the_option_7_5_is_unchanged(self):
        inf, fake, _ = self.correr([parche(QWEN)] * 3, normalizar=False)
        self.assertEqual(len(fake.requests), 3)
        self.assertEqual([i["estado"] for i in inf["intentos"]], ["rechazado"] * 3)
        self.assertFalse(inf["exito"])
        self.assertIsNone(inf["clasificacion"])

    def test_normalization_does_not_fix_logic(self):
        irrelevante = parche({"original": "n += 1", "reemplazo": "n = n + 1"})
        inf, fake, _ = self.correr([irrelevante] * 3)
        self.assertEqual(len(fake.requests), 3, "máximo tres inferencias")
        self.assertEqual([(i["estado"], i["sangria_normalizada"], i["discrepancia_sintetica"]) for i in inf["intentos"]],
                         [("aplicado", True, "persiste")] * 3)
        self.assertIn("(el coordinador ajustó solo su sangría)", fake.requests[1]["messages"][1]["content"])
        self.assertFalse(inf["exito"])

    def test_unsafe_patches_stay_rejected(self):
        inf, _, d = self.correr([parche({"original": "continue", "reemplazo": "pass"}),
                                 parche({"original": "saldos = dict(saldos_iniciales or {})", "reemplazo": "saldos = {}"}),
                                 parche({"original": "if tipo != 'anulacion':", "reemplazo": "if True:"})])
        self.assertEqual([i["estado"] for i in inf["intentos"]], ["rechazado"] * 3)
        motivos = [i["motivo"] for i in inf["intentos"]]
        self.assertIn("aparece", motivos[0])
        self.assertIn("fuera de las zonas autorizadas", motivos[1])
        self.assertIn("ni siquiera ajustando la sangría", motivos[2])
        self.assertFalse(inf["exito"])

    def test_cli_option_preflight_and_run(self):
        lab = Path(tempfile.mkdtemp(prefix="cia-exp76-cli-")) / "oficina-reparacion-7-6"
        fake = FakeOllama([parche(QWEN)], models=("qwen2.5-coder:7b",))
        self.addCleanup(fake.close)
        reg = RG.Registro(lab / "oficina.db")
        reg.registrar("prueba", fake.url, ["qwen2.5-coder:7b"], permitir_local=True)
        reg.cerrar()
        from constructor_ia.sandbox.executor import Executor
        from constructor_ia.sandbox.limiter import NullLimiter

        def cli_(*extra):
            out, err = StringIO(), StringIO()
            ex = lambda cfg: Executor(cfg, limiter=NullLimiter(), allow_unverified_limits=True)  # noqa: E731
            with redirect_stdout(out), redirect_stderr(err), mock.patch("constructor_ia.sandbox.executor.Executor", ex), \
                    mock.patch.object(RM, "memoria_disponible_mb", lambda: 8000):
                rc = cli.main(["--config", str(lab.parent / "no-existe.toml"), "oficina", "reparar-guiado",
                               "--corrida", str(self.origen), "--carpeta", str(lab), "--trabajador", "prueba",
                               "--normalizar-sangria", *extra])
            return rc, out.getvalue() + err.getvalue()
        rc, out = cli_()
        self.assertEqual(rc, 0, out)
        self.assertIn("experimento 7.6", out)
        self.assertEqual(fake.requests, [])
        rc, out = cli_("--autorizo-inferencia-remota")
        self.assertEqual(rc, 0, out)
        self.assertIn("sangría normalizada", out)
        self.assertIn("Experimento 7.6: ÉXITO (reparación automática con normalización de formato (sangría))", out)
        self.assertEqual(len(list((lab / "experimento-7-6").glob("reparacion-*/informe.json"))), 1)


if __name__ == "__main__":
    unittest.main()
