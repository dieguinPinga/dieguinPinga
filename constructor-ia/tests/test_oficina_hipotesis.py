"""Experimento 6 (OFICINA.md, «Hipótesis verificables»): el revisor propone un caso sintético mínimo desde el contrato
y el coordinador lo contrasta en el sandbox contra la referencia y contra el programa.

SIN inferencias reales: revisores y programadores simulados (Ollama falso) y el sandbox real (bwrap). Las variantes
de código son solo para ejercitar el mecanismo; los nueve casos originales siguen siendo la evaluación.
"""
import json
import tempfile
import unittest
from pathlib import Path

from constructor_ia.oficina import cola as CO
from constructor_ia.oficina import contraste as CT
from constructor_ia.oficina import programador as PG
from constructor_ia.oficina import revisor as RV
from constructor_ia.oficina.misiones_programacion import (REFERENCIA_CONCILIAR, VALIDADORES, casos_conciliar,
                                                          mision_conciliar_movimientos)
from constructor_ia.ollama import OllamaClient
from constructor_ia.sandbox.executor import Executor
from constructor_ia.sandbox.limiter import NullLimiter

from .fake_ollama import FakeOllama
from .helpers import bwrap_works, make_cfg

A = REFERENCIA_CONCILIAR.replace(
    '        aplicados[mid] = {"id": mid, "tipo": tipo, "producto": prod, "efecto": efecto}',
    '        if tipo != "anulacion":\n'
    '            aplicados[mid] = {"id": mid, "tipo": tipo, "producto": prod, "efecto": efecto}')
A2, A3, A4 = (A + f"\n_VARIANTE = {i}\n" for i in (2, 3, 4))


def mov(i, tipo, **k):
    return {"id": i, "fecha": f"2026-01-0{i}", "producto": "X", "tipo": tipo, **k}


# Caso mínimo construido desde el contrato: anular una anulación aplicada.
CASO_DE_ANULACION = {"args": [[mov(1, "ingreso", cantidad=10), mov(2, "egreso", cantidad=3),
                               mov(3, "anulacion", anula=2), mov(4, "anulacion", anula=3)]],
                     "que_deberia_pasar": "el movimiento 4 anula a una anulación: motivo anulacion_de_anulacion"}
CASO_SIN_DISCREPANCIA = {"args": [[mov(1, "ingreso", cantidad=5), mov(2, "egreso", cantidad=2)]]}


def diagnostico(caso):
    return {"hipotesis_principal": {
        "regla_del_contrato": "anulacion_de_anulacion",
        "citas_del_codigo": ['if tipo != "anulacion":'],
        "secuencia_causal": ["la condición deja a las anulaciones afuera del registro de aplicados",
                             "una anulación que apunta a otra anulación no la encuentra",
                             "sale «anulacion_inexistente» antes de evaluar «anulacion_de_anulacion»"],
        "rama_inaccesible": "anulacion_de_anulacion",
        "propiedad_a_cambiar": "el registro de aplicados tiene que incluir las anulaciones aplicadas",
        "confianza": "alta"}, "caso_sintetico": caso}


def bloque(c):
    return f"```python\n{c}```\n"


@unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
class ContrasteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = make_cfg(sandbox_timeout_s=20, sandbox_case_timeout_s=3)
        cls.ex = Executor(cls.cfg, limiter=NullLimiter(), allow_unverified_limits=True)
        cls.ex.self_test()
        assert cls.ex.enabled, cls.ex.disabled_reason
        cls.ct = CT.desde_mision(mision_conciliar_movimientos, REFERENCIA_CONCILIAR, VALIDADORES["conciliar_movimientos"])

    def contrastar(self, codigo, caso_json, contraste=None):
        caso, motivo = CT.leer_caso(caso_json)
        return CT.contrastar(self.ex, contraste or self.ct, codigo, caso, motivo)

    def test_a_real_discrepancy_confirms_the_hypothesis(self):
        r = self.contrastar(A, CASO_DE_ANULACION)
        self.assertEqual((r["discrepancia"], r["comportamiento_candidato"], r["hipotesis_causal"], r["validez"]["valido"]),
                         ("demostrada", "incorrecto_en_el_caso", "compatible_no_demostrada", True),
                         "la discrepancia se demuestra; la explicación causal queda solo compatible")
        self.assertEqual((r["referencia"]["ejecucion"], r["candidato"]["ejecucion"]), ("completa", "completa"))
        self.assertEqual(r["referencia"]["salida"]["inconsistencias"], [{"id": 4, "motivo": "anulacion_de_anulacion"}])
        self.assertEqual(r["candidato"]["salida"]["inconsistencias"], [{"id": 4, "motivo": "anulacion_inexistente"}])
        self.assertEqual(CT.diferencias(r["referencia"]["salida"], r["candidato"]["salida"]), ["inconsistencias"])
        for k in ("ok", "estado", "got"):
            self.assertNotIn(k, r["referencia"], "sin el veredicto del arnés contra una expectativa ajena")

    def test_no_discrepancy_leaves_the_hypothesis_unconfirmed(self):
        r = self.contrastar(A, CASO_SIN_DISCREPANCIA)
        self.assertEqual((r["discrepancia"], r["comportamiento_candidato"], r["hipotesis_causal"]),
                         ("no_observada", "correcto_en_el_caso", "sin_respaldo_en_el_caso"))
        self.assertEqual(self.contrastar(REFERENCIA_CONCILIAR, CASO_DE_ANULACION)["discrepancia"], "no_observada",
                         "con la referencia no hay discrepancia que mostrar")

    def test_cases_outside_the_contract_are_inconclusive(self):
        malos = {"sin args": ({"kwargs": {}}, "falta el argumento «movimientos»"),
                 "fecha mal": ({"args": [[{"id": 1, "fecha": "1/1/26", "producto": "X", "tipo": "ingreso",
                                           "cantidad": 1}]]}, "fecha"),
                 "clave de más": ({"args": [[dict(mov(1, "ingreso", cantidad=1), extra=1)]]}, "exactamente"),
                 "demasiado grande": ({"args": [[mov(1, "ingreso", cantidad=1)] * 13]}, "caso mínimo"),
                 "argumento inventado": ({"args": [[mov(1, "ingreso", cantidad=1)]], "kwargs": {"modo": 1}},
                                         "a lo sumo"),
                 "no es un caso": ("texto", "«args» (lista)"),
                 "ausente": (None, "no propuso")}
        for nombre, (caso, esperado) in malos.items():
            with self.subTest(nombre):
                r = self.contrastar(A, caso)
                self.assertEqual((r["discrepancia"], r["hipotesis_causal"]), ("inconclusa", "sin_evaluar"))
                self.assertIn(esperado, r["motivo"])
                self.assertIsNone(r["candidato"], "un caso inválido nunca se ejecuta con el programa")

    def test_a_case_equal_to_a_private_input_is_never_executed_nor_revealed(self):
        args, kwargs, _, _ = casos_conciliar()[5]                  # entrada de un caso oculto
        llamadas = []

        class Espia:
            def run(self, *a, **k):
                llamadas.append(a)
                raise AssertionError("no debía ejecutarse")
        caso, motivo = CT.leer_caso({"args": args, "kwargs": kwargs})
        r = CT.contrastar(Espia(), self.ct, A, caso, motivo)
        self.assertEqual(r["discrepancia"], "inconclusa")
        self.assertIn("coincide con la entrada de un caso privado", r["motivo"])
        self.assertIsNone(r["referencia"])
        self.assertEqual(llamadas, [])

    def test_reference_that_cannot_process_the_case_makes_it_inconclusive(self):
        rota = CT.Contraste("conciliar_movimientos", "def conciliar_movimientos(*a, **k):\n    raise ValueError('x')\n",
                            VALIDADORES["conciliar_movimientos"])
        r = self.contrastar(A, CASO_DE_ANULACION, rota)
        self.assertEqual((r["discrepancia"], r["referencia"]["ejecucion"]), ("inconclusa", "excepcion"))
        self.assertIn("la referencia no procesa el caso", r["motivo"])
        enorme = CT.Contraste("conciliar_movimientos", "def conciliar_movimientos(*a, **k):\n    return 'x' * 5000\n",
                              VALIDADORES["conciliar_movimientos"])
        self.assertIn("demasiado grande", self.contrastar(A, CASO_DE_ANULACION, enorme)["motivo"])

    def test_programs_that_crash_on_the_case_confirm_the_discrepancy(self):
        r = self.contrastar("def conciliar_movimientos(m, s=None, c=None):\n    raise KeyError('anula')\n",
                            CASO_DE_ANULACION)
        self.assertEqual((r["discrepancia"], r["candidato"]["ejecucion"]), ("demostrada", "excepcion"))
        self.assertIn("KeyError", str(r["candidato"]["salida"]))


@unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
class CicloTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = make_cfg(sandbox_timeout_s=20, sandbox_case_timeout_s=3)
        cls.ex = Executor(cls.cfg, limiter=NullLimiter(), allow_unverified_limits=True)
        cls.ex.self_test()

    def correr(self, respuestas, hipotesis=True, **k):
        d = Path(tempfile.mkdtemp(prefix="cia-hipotesis-"))
        cola = CO.Cola(d / "oficina.db")
        self.addCleanup(cola.cerrar)
        fake = FakeOllama(respuestas, models=("gemma4:12b",))
        self.addCleanup(fake.close)
        cola.crear_mision("programación: conciliar_movimientos", [{
            "nombre": "conciliar_movimientos", "rol": "programador", "modelo": "gemma4:12b",
            "carga": {"mision": "conciliar_movimientos"}, "max_intentos": 2, "tiempo_max_s": 300}])
        rv = RV.ConfigRevisor(PG.modelo_remoto("gemma4:12b", num_predict=1024, timeout_s=60),
                              hipotesis_verificables=hipotesis, **k)
        r = PG.Programador(d, self.cfg, self.ex, cola, log=lambda *_: None, revisor=rv,
                           cliente_para=lambda t: OllamaClient(t["url"])).atender(
            {"id": "pulenta", "url": fake.url, "modelos": ["gemma4:12b"], "permisos": ["inferencia"], "capacidad": 1,
             "disponible_ahora": True})
        taller = PG.Taller(d, self.cfg, self.ex, fake.url, PG.modelo_remoto("gemma4:12b"), cliente=object())
        self.addCleanup(taller.cerrar)
        return r, fake, taller, cola, d

    def revision(self, taller, r, n):
        return json.loads(taller.files.read_rel(r["informe"]["mision_taller"], f"intento-{n}/revision.json"))

    def test_confirmed_hypothesis_reaches_the_programmer_and_the_original_nine_cases_decide(self):
        r, fake, taller, cola, d = self.correr(
            [bloque(A), bloque(A2), json.dumps(diagnostico(CASO_DE_ANULACION)), bloque(REFERENCIA_CONCILIAR)])
        self.assertEqual(r["informe"]["estado"], "aprobada")
        pedido_revisor = fake.requests[2]["messages"]
        self.assertIn("CASO SINTÉTICO MÍNIMO", pedido_revisor[0]["content"])
        texto_revisor = json.dumps(pedido_revisor, ensure_ascii=False)
        for privado in ("2026-05-06", "2026-06-", "anulacion_de_anulacion\\\", \\\"id"):
            self.assertNotIn(privado, texto_revisor)
        rv = self.revision(taller, r, 3)
        ct = rv["contraste"]
        self.assertEqual((ct["discrepancia"], ct["hipotesis_causal"], ct["validez"]["valido"]),
                         ("demostrada", "compatible_no_demostrada", True))
        self.assertEqual(ct["hipotesis"], "el registro de aplicados tiene que incluir las anulaciones aplicadas")
        self.assertEqual(ct["caso"]["args"], CASO_DE_ANULACION["args"])
        self.assertIn("salida", ct["referencia"])
        tercero = fake.requests[3]["messages"][1]["content"]
        self.assertIn("ESTADO DE LA HIPÓTESIS: el caso sintético DEMOSTRÓ que tu código no cumple", tercero)
        self.assertIn("NO DEMOSTRADO: la causa.", tercero)
        self.assertIn("EVIDENCIA VERIFICADA POR EL COORDINADOR (caso sintético del revisor; NO es una prueba oculta)",
                      tercero)
        self.assertIn('"motivo": "anulacion_de_anulacion"', tercero, "lo que pide el contrato para el caso sintético")
        self.assertIn("claves distintas: 'inconsistencias'", tercero)
        self.assertNotIn("2026-05-06", tercero)
        ef = rv["efecto"]
        self.assertEqual((ef["atencion_textual"], ef["discrepancia_sintetica"], ef["originales"], ef["aprobados"]),
                         ("cambió líneas citadas", "eliminada", "mejoró", 9))
        self.assertEqual(ef["resolucion_funcional"], "discrepancia eliminada, originales mejoró")
        ok, detalle = PG.verificar(cola, d, self.cfg, self.ex, r["tarea"])
        self.assertTrue(ok, detalle)
        self.assertIn("9/9", detalle, "la evaluación sigue siendo la batería original")

    def test_an_unconfirmed_hypothesis_is_not_presented_as_a_defect(self):
        r, fake, taller, *_ = self.correr(
            [bloque(A), bloque(A2), json.dumps(diagnostico(CASO_SIN_DISCREPANCIA)), bloque(A3), bloque(A4)],
            max_revisiones=1)
        self.assertEqual(self.revision(taller, r, 3)["contraste"]["discrepancia"], "no_observada")
        tercero = fake.requests[3]["messages"][1]["content"]
        self.assertIn("ESTADO DE LA HIPÓTESIS: el caso sintético NO mostró ninguna discrepancia", tercero)
        self.assertNotIn("EVIDENCIA VERIFICADA", tercero)

    def test_an_invalid_case_is_inconclusive_and_marked_as_such(self):
        r, fake, taller, *_ = self.correr(
            [bloque(A), bloque(A2), json.dumps(diagnostico({"args": "no"})), bloque(A3), bloque(A4)],
            max_revisiones=1)
        ct = self.revision(taller, r, 3)["contraste"]
        self.assertEqual((ct["discrepancia"], ct["validez"]["valido"]), ("inconclusa", False))
        self.assertIn("ESTADO DE LA HIPÓTESIS: SIN CONTRASTAR", fake.requests[3]["messages"][1]["content"])

    def test_an_ignored_confirmed_discrepancy_is_measured(self):
        r, fake, taller, *_ = self.correr(
            [bloque(A), bloque(A2), json.dumps(diagnostico(CASO_DE_ANULACION)), bloque(A3), bloque(A4)],
            max_revisiones=1)
        ef = self.revision(taller, r, 3)["efecto"]
        self.assertEqual((ef["atencion_textual"], ef["discrepancia_sintetica"], ef["originales"]),
                         ("no cambió las líneas citadas", "persiste", "se mantuvo"))
        self.assertEqual(ef["resolucion_funcional"], "discrepancia persiste, originales se mantuvo")
        self.assertEqual(r["informe"]["estado"], "fallida")
        self.assertEqual(r["informe"]["mejor"]["aprobados"], 8, "se conserva la mejor versión")

    def test_without_the_option_nothing_changes(self):
        r, fake, taller, *_ = self.correr(
            [bloque(A), bloque(A2), json.dumps(diagnostico(CASO_DE_ANULACION)), bloque(REFERENCIA_CONCILIAR)],
            hipotesis=False)
        self.assertNotIn("CASO SINTÉTICO", fake.requests[2]["messages"][0]["content"])
        rv = self.revision(taller, r, 3)
        self.assertNotIn("contraste", rv)
        self.assertNotIn("EVIDENCIA VERIFICADA", fake.requests[3]["messages"][1]["content"])


if __name__ == "__main__":
    unittest.main()
