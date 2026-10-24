"""Segunda misión de programación de la oficina: conciliar_movimientos (OFICINA.md).

SIN inferencias reales. Comprueba que las pruebas de la misión son correctas (la solución de referencia coincide con
los resultados escritos a mano y aprueba en el sandbox real), que detectan errores típicos, que los casos ocultos no
llegan al modelo y que el circuito existente (Engine, sandbox, verificación central) la recorre sin cambios.
"""
import copy
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from constructor_ia import cli, prompts
from constructor_ia.oficina import cola as CO
from constructor_ia.oficina import programador as PG
from constructor_ia.oficina import registro as RG
from constructor_ia.oficina import remoto as RM
from constructor_ia.oficina.misiones_programacion import (MISIONES, REFERENCIA_CONCILIAR, casos_conciliar,
                                                          mision_conciliar_movimientos)
from constructor_ia.ollama import OllamaClient
from constructor_ia.sandbox.executor import Executor
from constructor_ia.sandbox.limiter import NullLimiter

from .fake_ollama import FakeOllama
from .helpers import bwrap_works, make_cfg

# Errores típicos escritos a mano, SOLO para comprobar que las pruebas los detectan (no se le dan al modelo).
ORDEN_DE_LISTA = REFERENCIA_CONCILIAR.replace('sorted(movimientos, key=lambda x: (x["fecha"], x["id"]))',
                                              "movimientos")
SIN_CONTROL_DE_SALDO = REFERENCIA_CONCILIAR.replace("if saldos.get(prod, 0) + efecto < 0:", "if False:")
CONCILIACION_CON_CEROS = REFERENCIA_CONCILIAR.replace("            if d != 0:\n                conciliacion[prod] = d",
                                                      "            conciliacion[prod] = d")
ANULA_IGNORADOS = REFERENCIA_CONCILIAR.replace('ref = aplicados.get(m.get("anula"))',
                                               'ref = aplicados.get(m.get("anula")) or {"id": m.get("anula"), '
                                               '"tipo": "ingreso", "producto": m["producto"], "efecto": 0}')


def bloque(codigo):
    return f"```python\n{codigo}```\n"


class ContratoTests(unittest.TestCase):
    def test_reference_matches_the_hand_written_results_without_mutating_inputs(self):
        ns = {}
        exec(REFERENCIA_CONCILIAR, ns)
        for i, (args, kwargs, esperado, _) in enumerate(casos_conciliar()):
            with self.subTest(caso=i):
                a, k = copy.deepcopy(args), copy.deepcopy(kwargs)
                self.assertEqual(ns["conciliar_movimientos"](*a, **k), esperado)
                self.assertEqual((a, k), (args, kwargs), "no modifica la entrada")

    def test_mission_shape(self):
        m = mision_conciliar_movimientos()
        casos = m["spec"]["cases"]
        self.assertEqual(m["max_attempts"], 4)
        self.assertEqual((sum(c["visible"] for c in casos), sum(not c["visible"] for c in casos)), (2, 7))
        self.assertIn("conciliar_movimientos", MISIONES)
        motivos = {i["motivo"] for c in casos for i in c["expect"]["inconsistencias"]}
        self.assertEqual(motivos, {"id_duplicado", "tipo_desconocido", "cantidad_invalida", "saldo_insuficiente",
                                   "anulacion_inexistente", "anulacion_de_anulacion", "anulacion_repetida"},
                         "las pruebas cubren todos los motivos del contrato")
        for motivo in motivos:
            self.assertIn(f"'{motivo}'", m["description"], "cada motivo está definido en el contrato")

    def test_hidden_cases_never_reach_the_prompt(self):
        m = mision_conciliar_movimientos()
        texto = json.dumps(prompts.initial(m["description"], m["spec"], 40000), ensure_ascii=False)
        self.assertIn("Además hay 7 casos ocultos", texto)
        for oculto in ('\\"Sal\\"', '\\"Fideos\\"', "devolucion", "2026-06-", "2026-07-", "id_duplicado\\\", \\\"id"):
            self.assertNotIn(oculto, texto)


@unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
class SandboxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = make_cfg(sandbox_timeout_s=20, sandbox_case_timeout_s=3)
        cls.ex = Executor(cls.cfg, limiter=NullLimiter(), allow_unverified_limits=True)
        cls.ex.self_test()
        assert cls.ex.enabled, cls.ex.disabled_reason

    def test_reference_passes_every_case_in_the_real_sandbox(self):
        c = PG.comprobar_mision(self.ex, "conciliar_movimientos")
        self.assertEqual((c["ok"], c["casos"], c["visibles"], c["ocultos"]), (True, "9/9", 2, 7))

    def test_typical_mistakes_are_caught_by_hidden_cases(self):
        m = mision_conciliar_movimientos()
        for nombre, codigo in (("orden de la lista", ORDEN_DE_LISTA), ("sin control de saldo", SIN_CONTROL_DE_SALDO),
                               ("conciliación con ceros", CONCILIACION_CON_CEROS),
                               ("anula movimientos ignorados", ANULA_IGNORADOS)):
            with self.subTest(error=nombre):
                r = self.ex.run(codigo, m["spec"], "").result
                ocultos = [c for c in r["cases"] if not c["ok"] and not c.get("visible")]
                self.assertTrue(ocultos, f"{nombre}: algún caso oculto tiene que fallar")
                informe = prompts.failure_report(r, [], 2500)
                self.assertIn("Caso oculto", informe)
                for c in m["spec"]["cases"]:
                    if not c["visible"] and c["expect"]["inconsistencias"]:
                        self.assertNotIn(json.dumps(c["expect"]["inconsistencias"], ensure_ascii=False), informe)

    def test_the_existing_cycle_runs_the_new_mission_and_verifies_it_centrally(self):
        d = Path(tempfile.mkdtemp(prefix="cia-conciliar-"))
        cola = CO.Cola(d / "oficina.db")
        self.addCleanup(cola.cerrar)
        fake = FakeOllama([bloque(REFERENCIA_CONCILIAR)], models=("gemma4:12b",))
        self.addCleanup(fake.close)
        mid = cola.crear_mision("programación: conciliar_movimientos", [{
            "nombre": "conciliar_movimientos", "rol": "programador", "modelo": "gemma4:12b",
            "carga": {"mision": "conciliar_movimientos"}, "max_intentos": 2, "tiempo_max_s": 300}])
        prog = PG.Programador(d, self.cfg, self.ex, cola, log=lambda *_: None,
                              cliente_para=lambda t: OllamaClient(t["url"]))
        r = prog.atender({"id": "pulenta", "url": fake.url, "modelos": ["gemma4:12b"], "permisos": ["inferencia"],
                          "capacidad": 1, "disponible_ahora": True})
        self.assertEqual((r["resultado"], r["informe"]["estado"], r["informe"]["intento_final"]),
                         ("propuesta", "aprobada", 1))
        ok, detalle = PG.verificar(cola, d, self.cfg, self.ex, r["tarea"])
        self.assertTrue(ok, detalle)
        self.assertIn("(9/9 casos, incluidos los ocultos)", detalle)
        self.assertEqual(cola.mision(mid)["estado"], "completa")
        self.assertEqual(fake.requests[0]["model"], "gemma4:12b")
        prompt = json.dumps(fake.requests[0]["messages"], ensure_ascii=False)
        self.assertNotIn("2026-06-", prompt)


@unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
class CliTests(unittest.TestCase):
    def test_check_and_enqueue_the_new_mission_without_inference(self):
        d = Path(tempfile.mkdtemp(prefix="cia-conciliar-cli-"))
        fake = FakeOllama([], models=("gemma4:12b",))
        self.addCleanup(fake.close)
        reg = RG.Registro(d / "oficina" / "oficina.db")
        reg.registrar("prueba", fake.url, ["gemma4:12b"], permitir_local=True)
        reg.cerrar()

        def correr(*argv):
            out, err = StringIO(), StringIO()
            ex = lambda cfg: Executor(cfg, limiter=NullLimiter(), allow_unverified_limits=True)  # noqa: E731
            with redirect_stdout(out), redirect_stderr(err), \
                    mock.patch("constructor_ia.sandbox.executor.Executor", ex), \
                    mock.patch.object(RM, "memoria_disponible_mb", lambda: 8000):
                rc = cli.main(["--config", str(d / "no-existe.toml"), "oficina", *argv, "--carpeta", str(d / "oficina")])
            return rc, out.getvalue()
        rc, out = correr("comprobar-programacion", "--trabajador", "prueba", "--mision", "conciliar_movimientos")
        self.assertEqual(rc, 0, out)
        self.assertIn("[OK] misión «conciliar_movimientos» con la solución de REFERENCIA: 9/9 casos (2 visibles, "
                      "7 ocultos)", out)
        rc, out = correr("encolar-programacion", "--mision", "conciliar_movimientos")
        self.assertEqual(rc, 0, out)
        self.assertEqual(fake.requests, [], "ni la comprobación ni el encolado infieren")


if __name__ == "__main__":
    unittest.main()
