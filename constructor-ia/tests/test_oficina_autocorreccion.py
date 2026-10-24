"""Autocorrección con memoria de la mejor versión (OFICINA.md, «Autocorrección»), SIN inferencias reales.

Un Ollama falso devuelve código escrito en la prueba; el código se ejecuta en el sandbox real (bwrap). Se comprueba
qué recibe el modelo en cada reintento: pistas seguras para los casos ocultos (sin valores ni datos), aviso de
regresión con la mejor versión como punto de partida, avisos de estancamiento, y que la mejor versión se conserva y
se verifica de forma independiente al final.
"""
import json
import tempfile
import unittest
from pathlib import Path

from constructor_ia.oficina import autocorreccion as AC
from constructor_ia.oficina import cola as CO
from constructor_ia.oficina import programador as PG
from constructor_ia.oficina.misiones_programacion import (CATEGORIAS_CONCILIAR, DESCRIPCION_CONCILIAR,
                                                          REFERENCIA_CONCILIAR, mision_conciliar_movimientos)
from constructor_ia.ollama import OllamaClient
from constructor_ia.sandbox.executor import Executor
from constructor_ia.sandbox.limiter import NullLimiter

from .fake_ollama import FakeOllama
from .helpers import bwrap_works, make_cfg

# Variantes escritas a mano SOLO para ejercitar el mecanismo (no se le dan a ningún modelo real):
# A: no registra las anulaciones aplicadas, así que «anular una anulación» sale como «anulacion_inexistente»
#    (el mismo tipo de error que tuvo Gemma en la segunda misión real): falla un solo caso oculto (el 6).
A = REFERENCIA_CONCILIAR.replace(
    '        aplicados[mid] = {"id": mid, "tipo": tipo, "producto": prod, "efecto": efecto}',
    '        if tipo != "anulacion":\n'
    '            aplicados[mid] = {"id": mid, "tipo": tipo, "producto": prod, "efecto": efecto}')
# B: una reescritura que rompe todo (0/9).
B = "def conciliar_movimientos(movimientos, saldos_iniciales=None, conteo_fisico=None):\n    raise ValueError('x')\n"
# C: otra variante peor que A (no ordena la lista).
C = REFERENCIA_CONCILIAR.replace('sorted(movimientos, key=lambda x: (x["fecha"], x["id"]))', "movimientos")


def bloque(codigo):
    return f"```python\n{codigo}```\n"


class PistasTests(unittest.TestCase):
    spec = mision_conciliar_movimientos()["spec"]

    def resultado(self, n, got, status="fallo", visible=False):
        return {"cases": [{"n": n, "kind": "caso", "visible": visible, "ok": False, "status": status, "got": got,
                           "expected": "NO SE USA"}]}

    def test_only_contract_key_names_and_the_category_never_values(self):
        esperado = self.spec["cases"][5]["expect"]
        got = json.dumps(dict(esperado, inconsistencias=[{"id": 6, "motivo": "anulacion_inexistente"}]))
        p = AC.pistas(self.spec, self.resultado(6, got), DESCRIPCION_CONCILIAR)
        self.assertEqual(p[6][0], "claves del resultado con un valor incorrecto: 'inconsistencias' (el resto de las "
                                  "claves está bien)")
        self.assertEqual(p[6][1], f"qué ejercita este caso: {CATEGORIAS_CONCILIAR[5]}")
        texto = json.dumps(p, ensure_ascii=False)
        for privado in ("anulacion_de_anulacion", "anulacion_repetida", "Yerba", "2026-05", "NO SE USA"):
            self.assertNotIn(privado, texto)

    def test_key_names_outside_the_contract_are_never_shown(self):
        spec = {"cases": [{"args": [], "expect": {"saldo_secreto": 1, "ok": 2}, "visible": False}]}
        p = AC.pistas(spec, self.resultado(1, json.dumps({"saldo_secreto": 0, "ok": 2})), "contrato con 'ok'")
        self.assertEqual(p, {}, "«saldo_secreto» no figura en el contrato: no se nombra")

    def test_other_shapes(self):
        p = AC.pistas(self.spec, self.resultado(3, json.dumps({"saldos": {}, "movimientos_aplicados": 0,
                                                               "inconsistencias": [], "conciliacion": None,
                                                               "extra": 1})), DESCRIPCION_CONCILIAR)
        self.assertIn("devolvió claves que el contrato no pide", p[3])
        p = AC.pistas(self.spec, self.resultado(3, json.dumps([1, 2])), DESCRIPCION_CONCILIAR)
        self.assertIn("el resultado no es un diccionario como pide el contrato", p[3])
        p = AC.pistas(self.spec, self.resultado(3, '{"saldos": {"Arroz…'), DESCRIPCION_CONCILIAR)
        self.assertEqual(p[3], [f"qué ejercita este caso: {CATEGORIAS_CONCILIAR[2]}"], "salida recortada: solo la "
                                                                                       "categoría")
        self.assertEqual(AC.pistas(self.spec, self.resultado(1, "{}", visible=True), DESCRIPCION_CONCILIAR), {},
                         "los casos visibles ya tienen su informe completo")


@unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
class MemoriaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = make_cfg(sandbox_timeout_s=20, sandbox_case_timeout_s=3)
        cls.ex = Executor(cls.cfg, limiter=NullLimiter(), allow_unverified_limits=True)
        cls.ex.self_test()
        assert cls.ex.enabled, cls.ex.disabled_reason

    def correr(self, respuestas):
        d = Path(tempfile.mkdtemp(prefix="cia-autocorreccion-"))
        cola = CO.Cola(d / "oficina.db")
        self.addCleanup(cola.cerrar)
        fake = FakeOllama([bloque(c) for c in respuestas], models=("gemma4:12b",))
        self.addCleanup(fake.close)
        cola.crear_mision("programación: conciliar_movimientos", [{
            "nombre": "conciliar_movimientos", "rol": "programador", "modelo": "gemma4:12b",
            "carga": {"mision": "conciliar_movimientos"}, "max_intentos": 2, "tiempo_max_s": 300}])
        r = PG.Programador(d, self.cfg, self.ex, cola, log=lambda *_: None,
                           cliente_para=lambda t: OllamaClient(t["url"])).atender(
            {"id": "pulenta", "url": fake.url, "modelos": ["gemma4:12b"], "permisos": ["inferencia"], "capacidad": 1,
             "disponible_ahora": True})
        taller = PG.Taller(d, self.cfg, self.ex, fake.url, PG.modelo_remoto("gemma4:12b"), cliente=object())
        self.addCleanup(taller.cerrar)
        return r, fake, taller, cola, d

    def prompt(self, fake, i):
        return fake.requests[i]["messages"][1]["content"]

    def test_a_worse_second_attempt_does_not_replace_the_best_and_the_third_starts_from_it(self):
        r, fake, taller, cola, d = self.correr([A, B, REFERENCIA_CONCILIAR])
        inf = r["informe"]
        self.assertEqual([(i["passed"], i["total"]) for i in inf["intentos"]], [(8, 9), (0, 9), (9, 9)])
        self.assertEqual((inf["estado"], inf["intento_final"]), ("aprobada", 3))
        segundo, tercero = self.prompt(fake, 1), self.prompt(fake, 2)
        # 2.º intento: parte del 1.º (el mejor hasta ahora), con la pista segura del caso 6.
        self.assertIn('if tipo != "anulacion":', segundo, "parte del código del intento 1")
        self.assertIn("claves del resultado con un valor incorrecto: 'inconsistencias'", segundo)
        self.assertIn(CATEGORIAS_CONCILIAR[5], segundo)
        # 3.º intento: aviso de regresión y la versión del intento 1 como punto de partida, no la del 2.
        self.assertIn("ATENCIÓN, REGRESIÓN: tu intento 2 aprobó 0 de 9 casos, MENOS que el intento 1 (8 de 9)", tercero)
        self.assertIn('if tipo != "anulacion":', tercero, "el código de partida es el de A")
        self.assertNotIn("raise ValueError('x')", tercero, "la versión que empeoró no es el punto de partida")
        for req in fake.requests:                      # nunca la verdad de un caso oculto
            texto = json.dumps(req["messages"], ensure_ascii=False)
            self.assertNotIn("2026-05-06", texto)
            self.assertNotIn("anulacion_de_anulacion\\\", \\\"id", texto)
        retro = taller.files.read_rel(inf["mision_taller"], "intento-3/retroalimentacion.md")
        self.assertTrue(retro.startswith("Punto de partida: intento 1"))
        # Nada se reescribe: cada intento conserva el código que devolvió el modelo.
        for n, codigo in ((1, A), (2, B), (3, REFERENCIA_CONCILIAR)):
            self.assertEqual(taller.files.read_rel(inf["mision_taller"], f"intento-{n}/solucion.py").strip(),
                             codigo.strip())
        self.assertEqual(inf["mejor"]["intento"], 3)
        ok, detalle = PG.verificar(cola, d, self.cfg, self.ex, r["tarea"])
        self.assertTrue(ok, detalle)

    def test_without_success_the_best_version_is_kept_and_verified_independently(self):
        r, fake, taller, cola, d = self.correr([A, B, C, B])
        inf = r["informe"]
        self.assertEqual(inf["estado"], "fallida")
        self.assertEqual([i["passed"] for i in inf["intentos"]][0], 8)
        self.assertEqual((inf["mejor"]["intento"], inf["mejor"]["aprobados"], inf["mejor"]["total"]), (1, 8, 9))
        mid = inf["mision_taller"]
        self.assertEqual(taller.files.read_rel(mid, "mejor/solucion.py").strip(), A.strip())
        resumen = json.loads(taller.files.read_rel(mid, "mejor/resumen.json"))
        self.assertEqual([h["base"] for h in resumen["historial"]], [None, 1, 1, 1],
                         "después de cada regresión se vuelve a partir de la mejor")
        ok, detalle = PG.verificar(cola, d, self.cfg, self.ex, r["tarea"])
        self.assertFalse(ok, "sin todas las pruebas no se acepta")
        self.assertIn("mejor versión conservada: intento 1 (8/9); reejecución independiente: 8/9", detalle)

    def test_identical_code_and_persistent_failures_are_pointed_out(self):
        r, fake, taller, cola, d = self.correr([A, A, REFERENCIA_CONCILIAR])
        tercero = self.prompt(fake, 2)
        self.assertIn("Tu intento 2 devolvió exactamente el mismo código que el intento 1", tercero)
        self.assertIn("El caso oculto 6 falló en los intentos 1, 2", tercero)
        self.assertNotIn("REGRESIÓN", tercero, "igual puntaje no es una regresión")
        self.assertEqual(r["informe"]["estado"], "aprobada")


if __name__ == "__main__":
    unittest.main()
