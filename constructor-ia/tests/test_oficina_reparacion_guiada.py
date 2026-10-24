"""Experimento 7.5 (OFICINA.md, «Experimento 7.5»): reparación guiada con la causa explícita, parche mínimo.

SIN inferencias reales: la corrida de origen se genera con el 7.3 simulado y el programador es un Ollama falso; el
sandbox es real. Se comprueban el pedido, la validación y aplicación de parches, los tres intentos como máximo, la
detención con motivo, la batería independiente como criterio y que la corrida original no cambia.
"""
import hashlib
import json
import shutil
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
from constructor_ia.oficina import revision_existente as RE
from constructor_ia.oficina.misiones_programacion import REFERENCIA_CONCILIAR
from constructor_ia.ollama import OllamaClient

from .fake_ollama import FakeOllama
from .helpers import bwrap_works
from .test_oficina_lectura_escritura import diagnostico_real
from .test_oficina_parches import COMO_QWEN, IRRELEVANTE, QUITAR_CONDICION, parche

INEXISTENTE = parche({"original": "    total = 0", "reemplazo": "    total = 1"})
# Arregla el caso del revisor pero rompe «anulacion_repetida», que el caso no ejercita.
ROMPE_OTRA_REGLA = parche(QUITAR_CONDICION, {"original": '            if ref["id"] in anulados:',
                                             "reemplazo": "            if False:"})


def huellas(carpeta: Path) -> dict:
    return {str(p.relative_to(carpeta)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(carpeta.rglob("*")) if p.is_file()}


@unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
class ReparacionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from .test_oficina_revision_existente import Base
        Base.setUpClass()
        cls.ex = Base.ex
        b = Base()
        _, _, cls.origen = b.correr([json.dumps(diagnostico_real(), ensure_ascii=False), INEXISTENTE, INEXISTENTE,
                                     INEXISTENTE], variante="7.3")
        b.doCleanups()                                          # cierra el Ollama falso de la corrida de origen

    def correr(self, respuestas, **k):
        fake = FakeOllama(respuestas, models=("qwen2.5-coder:7b",))
        self.addCleanup(fake.close)
        f = RGD.preparar(self.origen)
        destino = RGD.carpeta_de_reparacion(Path(tempfile.mkdtemp(prefix="cia-exp75-")), self.origen)
        inf = RGD.correr(destino, f, self.ex, OllamaClient(fake.url), RM.compuerta(fake.url),
                         PG.modelo_remoto("qwen2.5-coder:7b"), **k)
        return inf, fake, destino

    def usuario(self, fake, i):
        return fake.requests[i]["messages"][1]["content"]

    # Pedido --------------------------------------------------------------------------------------------------------
    def test_the_request_has_the_real_zones_and_the_explicit_cause_and_asks_only_for_a_patch(self):
        f = RGD.preparar(self.origen)
        self.assertEqual(f["zonas"], [(27, 37), (41, 46)])
        sistema, usuario = (m["content"] for m in RGD.mensajes(f))
        self.assertEqual(sistema, PA.SISTEMA_PARCHE)
        for texto in ("ZONA AUTORIZADA 1 (líneas 27–37)", "ZONA AUTORIZADA 2 (líneas 41–46)",
                      "CAUSA DEL DEFECTO (confirmada por el equipo", "Las anulaciones válidas no quedan registradas en "
                      "`aplicados`", 'condicionada por `if tipo != "anulacion":`', "No escribas el programa completo"):
            self.assertIn(texto, usuario)
        for prohibido in ("for m in sorted(", "Caso oculto", "9/9", "anulaciones_no_registradas", REFERENCIA_CONCILIAR,
                          RE.DEFECTOS["conciliar_movimientos"]["anulaciones_no_registradas"]["explicacion"],
                          "EVIDENCIA VERIFICADA"):
            self.assertNotIn(prohibido, usuario)

    def test_preparation_requires_the_cause_lines_inside_the_zones(self):
        copia = Path(tempfile.mkdtemp(prefix="cia-exp75-origen-")) / "corrida"
        shutil.copytree(self.origen, copia)
        rep = copia / "reparacion-1/reparacion.json"
        d = json.loads(rep.read_text())
        d["localizacion"]["zonas"] = [[27, 37]]
        rep.write_text(json.dumps(d))
        with self.assertRaisesRegex(RGD.ErrorReparacion, "no está dentro de las zonas autorizadas"):
            RGD.preparar(copia)
        with mock.patch.dict(RGD.CAUSAS, {}, clear=True):
            with self.assertRaisesRegex(RGD.ErrorReparacion, "no hay una causa explícita"):
                RGD.preparar(self.origen)

    # Resultados ----------------------------------------------------------------------------------------------------
    def test_a_correct_minimal_patch_passes_the_nine_independent_tests(self):
        antes = huellas(self.origen)
        inf, fake, d = self.correr([parche(COMO_QWEN)])
        self.assertTrue(inf["exito"], inf["motivo"])
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(fake.requests[0].get("format"), "json")
        self.assertEqual((inf["verificacion"]["aprobados"], inf["verificacion"]["total"]), (9, 9))
        self.assertEqual(inf["criterios"], {"parche_valido_y_aplicado": True, "modifica_el_codigo_defectuoso": True,
                                            "bateria_independiente_completa": True})
        for rel in ("intento-1/pedido.json", "intento-1/respuesta.txt", "intento-1/parche.json",
                    "intento-1/solucion.py", "informe.json"):
            self.assertTrue((d / rel).exists(), rel)
        self.assertEqual((d / "intento-1/respuesta.txt").read_text(), parche(COMO_QWEN), "respuesta literal")
        self.assertTrue(inf["auditoria_de_fugas"]["sin_fugas"])
        self.assertEqual(huellas(self.origen), antes, "la corrida original no cambia")

    def test_a_rejected_patch_is_recorded_and_its_reason_is_returned(self):
        inf, fake, d = self.correr([INEXISTENTE, INEXISTENTE, parche(QUITAR_CONDICION)])
        self.assertTrue(inf["exito"])
        self.assertEqual([i["estado"] for i in inf["intentos"]], ["rechazado", "rechazado", "aplicado"])
        self.assertIn("Tu parche del intento 1 fue rechazado: cambio 1: el fragmento original no aparece",
                      self.usuario(fake, 1))
        self.assertIn("Es el mismo parche que el del intento 1.", self.usuario(fake, 2))
        self.assertIn("no aparece en el código", json.loads((d / "intento-1/parche.json").read_text())["motivo"])
        self.assertFalse((d / "intento-1/solucion.py").exists())

    def test_three_failed_attempts_stop_with_the_reason(self):
        inf, fake, d = self.correr([parche(IRRELEVANTE)] * 3 + [parche(COMO_QWEN)])
        self.assertEqual(len(fake.requests), 3, "máximo tres intentos; el cuarto no se pide")
        self.assertFalse(inf["exito"])
        self.assertEqual(inf["motivo"], "ningún parche corrigió el defecto en 3 intentos")
        self.assertEqual([i["discrepancia_sintetica"] for i in inf["intentos"]], ["persiste"] * 3)
        segundo = self.usuario(fake, 1)
        for texto in ("se aplicó, pero no corrige el defecto", "se aplica de nuevo sobre el código de arriba",
                      "SIGUE DISTINTA en 'inconsistencias'", "SIN EFECTO EN ESTE CASO"):
            self.assertIn(texto, segundo)
        self.assertIsNone(inf["verificacion"], "la batería no se ejecuta sin eliminar la discrepancia")

    def test_the_independent_battery_decides_and_is_never_sent_back(self):
        inf, fake, _ = self.correr([ROMPE_OTRA_REGLA, parche(COMO_QWEN)])
        self.assertEqual(len(fake.requests), 1, "una sola verificación; su resultado no vuelve al modelo")
        self.assertEqual(inf["intentos"][0]["discrepancia_sintetica"], "eliminada")
        self.assertFalse(inf["verificacion"]["ok"])
        self.assertIn("la batería independiente no aprobó", inf["motivo"])
        self.assertFalse(inf["exito"])

    def test_truncation_budget_and_records(self):
        inf, fake, d = self.correr([{"text": parche(COMO_QWEN)[:60], "done_reason": "length"}, parche(COMO_QWEN)])
        self.assertIn("se cortó por el límite de tokens", inf["intentos"][0]["motivo"])
        self.assertTrue(inf["exito"])
        self.assertEqual([ll["cortada"] for ll in inf["llamadas"]], [True, False])
        inf, fake, _ = self.correr([parche(COMO_QWEN)], max_segundos=0)
        self.assertEqual(fake.requests, [])
        self.assertIn("límite de tiempo", inf["motivo"])

    def test_refuses_to_write_inside_the_original_run(self):
        with self.assertRaisesRegex(RGD.ErrorReparacion, "dentro de la corrida"):
            RGD.carpeta_de_reparacion(self.origen / "x", self.origen)

    # CLI -----------------------------------------------------------------------------------------------------------
    def test_cli_preflight_does_not_infer_and_the_authorized_run_does(self):
        lab = Path(tempfile.mkdtemp(prefix="cia-exp75-cli-")) / "oficina-reparacion-7-5"
        fake = FakeOllama([parche(COMO_QWEN)], models=("qwen2.5-coder:7b",))
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
                               "--corrida", str(self.origen), "--carpeta", str(lab), "--trabajador", "prueba", *extra])
            return rc, out.getvalue() + err.getvalue()
        rc, out = cli_("--mostrar-pedido")
        self.assertEqual(rc, 0, out)
        self.assertIn("[OK] batería independiente: la referencia aprueba y el código defectuoso no", out)
        self.assertIn("CAUSA DEL DEFECTO", out)
        self.assertEqual(fake.requests, [])
        self.assertFalse((lab / "experimento-7-5").exists())
        rc, out = cli_("--autorizo-inferencia-remota")
        self.assertEqual(rc, 0, out)
        self.assertIn("Experimento 7.5: ÉXITO", out)
        self.assertIn("criterio bateria_independiente_completa: sí", out)
        self.assertEqual(len(fake.requests), 1)


if __name__ == "__main__":
    unittest.main()
