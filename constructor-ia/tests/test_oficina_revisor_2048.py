"""7.3b: el revisor de `oficina revisar-existente` genera hasta 2048 tokens (con 1024 la revisión con caso sintético
se cortaba en la corrida 20261024-040804). Nada más cambia: el programador, el contexto, la temperatura y el revisor
de `oficina programar` (1024) quedan como estaban. SIN inferencias reales.
"""
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from constructor_ia import cli
from constructor_ia.oficina import registro as RG
from constructor_ia.oficina import remoto as RM
from constructor_ia.oficina import revision_existente as RE
from constructor_ia.oficina import revisor as RV
from constructor_ia.oficina.misiones_programacion import REFERENCIA_CONCILIAR
from constructor_ia.sandbox.executor import Executor
from constructor_ia.sandbox.limiter import NullLimiter

from .fake_ollama import FakeOllama
from .helpers import bwrap_works
from .test_oficina_hipotesis import A, A2, CASO_DE_ANULACION, bloque, diagnostico
from .test_oficina_parches import COMO_QWEN, parche
from .test_oficina_revision_existente import revision

ANTES = {"num_ctx": 8192, "num_predict": 2048, "temperature": 0.2}     # programador, igual que antes


def opciones(req):
    return {k: req["options"][k] for k in ("num_ctx", "num_predict", "temperature")}


@unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
class RevisorTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="cia-revisor-2048-"))

    def cli(self, carpeta, respuestas, *argv):
        fake = FakeOllama(respuestas, models=("gemma4:12b",))
        self.addCleanup(fake.close)
        reg = RG.Registro(carpeta / "oficina.db")
        reg.registrar("prueba", fake.url, ["gemma4:12b"], permitir_local=True)
        reg.cerrar()
        out, err = StringIO(), StringIO()
        ex = lambda cfg: Executor(cfg, limiter=NullLimiter(), allow_unverified_limits=True)  # noqa: E731
        with redirect_stdout(out), redirect_stderr(err), mock.patch("constructor_ia.sandbox.executor.Executor", ex), \
                mock.patch.object(RM, "memoria_disponible_mb", lambda: 8000):
            for args in argv:
                rc = cli.main(["--config", str(self.dir / "no-existe.toml"), "oficina", *args,
                               "--carpeta", str(carpeta)])
        return rc, out.getvalue() + err.getvalue(), fake

    def test_revisar_existente_reviewer_uses_2048_and_the_programmer_keeps_its_configuration(self):
        lab = self.dir / "oficina-revision-7-3c"
        rc, out, fake = self.cli(lab, [revision(CASO_DE_ANULACION), parche(COMO_QWEN)],
                                 ["revisar-existente", "--trabajador", "prueba", "--defecto",
                                  "anulaciones_no_registradas", "--variante", "7.3", "--autorizo-inferencia-remota"])
        self.assertEqual(rc, 0, out)
        revisor, programador = fake.requests
        self.assertTrue(revisor["messages"][0]["content"].startswith(RV.SISTEMA_REVISOR_AUTONOMO))
        self.assertEqual(opciones(revisor), dict(ANTES, num_predict=2048))
        self.assertEqual(RE.NUM_PREDICT_REVISOR, 2048)
        self.assertEqual(opciones(programador), ANTES)
        inf = json.loads(next((lab / "experimento-7").glob("corrida-*/informe.json")).read_text())
        self.assertEqual((inf["limites"]["num_predict_revisor"], inf["limites"]["num_predict_programador"]),
                         (2048, 2048))
        self.assertEqual((inf["limites"]["max_revisiones"], inf["limites"]["max_reparaciones"],
                          inf["limites"]["max_segundos"]), (2, 3, 2400.0), "intentos y presupuesto sin cambios")

    def test_oficina_programar_keeps_its_1024_reviewer(self):
        carpeta = self.dir / "oficina"
        rc, out, fake = self.cli(carpeta, [bloque(A), bloque(A2), json.dumps(diagnostico(None)),
                                           bloque(REFERENCIA_CONCILIAR)],
                                 ["encolar-programacion", "--mision", "conciliar_movimientos"],
                                 ["programar", "--trabajador", "prueba", "--mision", "conciliar_movimientos",
                                  "--autorizo-inferencia-remota", "--revisor"])
        self.assertEqual(rc, 0, out)
        revisores = [r for r in fake.requests if r["messages"][0]["content"].startswith(RV.SISTEMA_REVISOR)]
        self.assertEqual(len(revisores), 1)
        self.assertEqual(opciones(revisores[0]), dict(ANTES, num_predict=1024))
        self.assertTrue(all(opciones(r) == ANTES for r in fake.requests if r not in revisores))


if __name__ == "__main__":
    unittest.main()
