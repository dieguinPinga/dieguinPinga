"""Estabilización del experimento 6 (OFICINA.md, «Estabilización»), SIN inferencias reales.

Tres problemas de la sexta experiencia:
1. código con demasiados comentarios especulativos y respuestas cortadas por el límite de tokens;
2. el contraste mezclaba ejecución, salida, discrepancia y validez causal;
3. «atención» (textual) se confundía con la resolución funcional.
"""
import json
import tempfile
import unittest
from pathlib import Path

from constructor_ia import prompts
from constructor_ia.oficina import autocorreccion as AC
from constructor_ia.oficina import cola as CO
from constructor_ia.oficina import contraste as CT
from constructor_ia.oficina import programador as PG
from constructor_ia.oficina import revisor as RV
from constructor_ia.oficina.misiones_programacion import REFERENCIA_CONCILIAR
from constructor_ia.ollama import OllamaClient
from constructor_ia.sandbox.executor import Executor
from constructor_ia.sandbox.limiter import NullLimiter

from .fake_ollama import FakeOllama
from .helpers import bwrap_works, make_cfg
from .test_oficina_hipotesis import CASO_DE_ANULACION, diagnostico

A = REFERENCIA_CONCILIAR.replace(
    '        aplicados[mid] = {"id": mid, "tipo": tipo, "producto": prod, "efecto": efecto}',
    '        if tipo != "anulacion":\n'
    '            aplicados[mid] = {"id": mid, "tipo": tipo, "producto": prod, "efecto": efecto}')
A2 = A + "\n_VARIANTE = 2\n"
# A con comentarios especulativos de línea completa (como los de la sexta experiencia).
A_COMENTADO = A.replace("    for m in sorted(", "    # Hmm, ¿y si la anulación apunta a otra anulación? Tal vez...\n"
                                               "    # Podría ser anulacion_inexistente, o no. Revisar.\n"
                                               "    for m in sorted(", 1)
# Un elif cortado a mitad, como el tercer intento real.
CORTADO = A[:A.index('            if ref["tipo"] == "anulacion":')] + '            elif ref["tipo"] =='
# Corrige la discrepancia POR OTRO CAMINO: no toca la línea citada por el revisor.
OTRO_CAMINO = A.replace("        n += 1\n", "        if tipo == \"anulacion\":\n"
                                           "            aplicados[mid] = {\"id\": mid, \"tipo\": tipo, \"producto\": prod, "
                                           "\"efecto\": efecto}\n        n += 1\n", 1)


def bloque(c):
    return f"```python\n{c}```\n"


class ComentariosTests(unittest.TestCase):
    def test_only_full_line_comments_are_removed(self):
        codigo = ('def f(x):\n    """Doc # no es comentario."""\n    # especulación\n    s = "a # b"  # en línea\n'
                  '        # sangría rara\n    return s\n')
        self.assertEqual(AC.sin_comentarios(codigo),
                         'def f(x):\n    """Doc # no es comentario."""\n    s = "a # b"  # en línea\n    return s\n')
        self.assertEqual(AC.sin_comentarios(CORTADO), CORTADO, "un código cortado no se toca")
        self.assertIsNone(AC.sin_comentarios(None))


@unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
class CicloTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = make_cfg(sandbox_timeout_s=20, sandbox_case_timeout_s=3)
        cls.ex = Executor(cls.cfg, limiter=NullLimiter(), allow_unverified_limits=True)
        cls.ex.self_test()

    def correr(self, respuestas, revisor=None):
        d = Path(tempfile.mkdtemp(prefix="cia-estabilizacion-"))
        cola = CO.Cola(d / "oficina.db")
        self.addCleanup(cola.cerrar)
        fake = FakeOllama(respuestas, models=("gemma4:12b",))
        self.addCleanup(fake.close)
        cola.crear_mision("programación: conciliar_movimientos", [{
            "nombre": "conciliar_movimientos", "rol": "programador", "modelo": "gemma4:12b",
            "carga": {"mision": "conciliar_movimientos"}, "max_intentos": 2, "tiempo_max_s": 300}])
        r = PG.Programador(d, self.cfg, self.ex, cola, log=lambda *_: None, revisor=revisor,
                           cliente_para=lambda t: OllamaClient(t["url"])).atender(
            {"id": "pulenta", "url": fake.url, "modelos": ["gemma4:12b"], "permisos": ["inferencia"], "capacidad": 1,
             "disponible_ahora": True})
        taller = PG.Taller(d, self.cfg, self.ex, fake.url, PG.modelo_remoto("gemma4:12b"), cliente=object())
        self.addCleanup(taller.cerrar)
        resumen = json.loads(taller.files.read_rel(r["informe"]["mision_taller"], "mejor/resumen.json") or "{}")
        return r, fake, taller, resumen

    # 1. Código conciso y respuestas cortadas -------------------------------------------------------------------
    def test_programmer_requests_carry_the_concision_rules_and_the_token_limit(self):
        rv = RV.ConfigRevisor(PG.modelo_remoto("gemma4:12b", num_predict=1024, timeout_s=60))
        r, fake, *_ = self.correr([bloque(A), bloque(A2), json.dumps(diagnostico(None)), bloque(REFERENCIA_CONCILIAR)],
                                  revisor=rv)
        sistemas = [q["messages"][0]["content"] for q in fake.requests]
        programador = [s for s in sistemas if s.startswith(prompts.BUILDER_SYSTEM)]
        self.assertEqual(len(programador), 3)
        for s in programador:
            self.assertIn("El código va SIN comentarios", s)
            self.assertIn("límite de 2048 tokens", s)
        self.assertNotIn("Reglas de la oficina", sistemas[2], "el revisor tiene su propio prompt, sin cambios")
        self.assertTrue(sistemas[2].startswith(RV.SISTEMA_REVISOR))

    def test_the_starting_code_is_sent_without_speculative_comments(self):
        r, fake, taller, _ = self.correr([bloque(A_COMENTADO), bloque(REFERENCIA_CONCILIAR)])
        segundo = fake.requests[1]["messages"][1]["content"]
        self.assertNotIn("Hmm, ¿y si la anulación", segundo)
        self.assertIn('if tipo != "anulacion":', segundo, "el código en sí se conserva")
        mid = r["informe"]["mision_taller"]
        self.assertIn("Hmm, ¿y si la anulación", taller.files.read_rel(mid, "intento-1/solucion.py"),
                      "el código histórico del intento queda intacto")

    def test_a_truncated_attempt_is_never_the_starting_point_nor_the_best(self):
        r, fake, taller, resumen = self.correr([bloque(A), {"text": "```python\n" + CORTADO, "done_reason": "length"},
                                                bloque(REFERENCIA_CONCILIAR)])
        inf = r["informe"]
        self.assertEqual([i["test_status"] for i in inf["intentos"]], ["fallidas", "no_ejecutadas", "aprobadas"])
        self.assertEqual(inf["intentos"][1]["done_reason"], "length")
        tercero = fake.requests[2]["messages"][1]["content"]
        self.assertIn("Tu intento 2 SE CORTÓ al llegar al límite de 2048 tokens", tercero)
        self.assertIn("Como tu intento 2 no se pudo ejecutar, el punto de partida es el intento 1 (8 de 9)", tercero)
        self.assertNotIn('elif ref["tipo"] ==', tercero, "el código cortado no es el punto de partida")
        self.assertEqual([h["base"] for h in resumen["historial"]], [None, 1, 1])
        self.assertEqual(inf["estado"], "aprobada")

    def test_without_any_runnable_version_it_starts_again_from_the_contract(self):
        r, fake, taller, resumen = self.correr([{"text": "```python\n" + CORTADO, "done_reason": "length"},
                                                bloque(REFERENCIA_CONCILIAR)])
        segundo = fake.requests[1]["messages"][1]["content"]
        self.assertNotIn("Versión anterior de solucion.py", segundo, "pedido inicial, no de reparación")
        self.assertIn("Tu intento 1 SE CORTÓ", segundo)
        self.assertEqual(resumen["historial"][1]["base"], None)
        self.assertEqual(r["informe"]["estado"], "aprobada")

    # 3. Atención textual ≠ resolución funcional ----------------------------------------------------------------
    def test_a_repair_by_another_path_is_functionally_resolved_even_if_the_cited_lines_stay(self):
        rv = RV.ConfigRevisor(PG.modelo_remoto("gemma4:12b", num_predict=1024, timeout_s=60),
                              hipotesis_verificables=True)
        r, fake, taller, _ = self.correr(
            [bloque(A), bloque(A2), json.dumps(diagnostico(CASO_DE_ANULACION)), bloque(OTRO_CAMINO)], revisor=rv)
        self.assertEqual((r["informe"]["estado"], [i["passed"] for i in r["informe"]["intentos"]]),
                         ("aprobada", [8, 8, 9]))
        ef = json.loads(taller.files.read_rel(r["informe"]["mision_taller"], "intento-3/revision.json"))["efecto"]
        self.assertEqual(ef["atencion_textual"], "no cambió las líneas citadas", "la línea citada sigue igual…")
        self.assertEqual((ef["discrepancia_sintetica"], ef["originales"]), ("eliminada", "mejoró"),
                         "…pero el problema funcional se resolvió")
        self.assertEqual(ef["resolucion_funcional"], "discrepancia eliminada, originales mejoró")


class SeguimientoTests(unittest.TestCase):
    def st(self, **efecto):
        ef = {"intento": 3, "citas_que_siguen_igual": ['if tipo != "anulacion":'], "persisten_casos": [6]} | efecto
        return {"historial": [{"n": 3, "puntaje": 8, "total": 9}],
                "revisiones": [{"efecto": ef, "diagnostico": {"principal": {"propiedad_a_cambiar": "registrar"}}}]}

    def test_the_follow_up_only_insists_while_the_functional_problem_remains(self):
        self.assertIn("SEGUIMIENTO DEL COORDINADOR", AC.MotorConRevisor._seguimiento(None, self.st()))
        self.assertEqual(AC.MotorConRevisor._seguimiento(None, self.st(discrepancia_sintetica="eliminada")), "",
                         "sin discrepancia, no importa que no haya tocado las líneas citadas")
        self.assertEqual(AC.MotorConRevisor._seguimiento(None, self.st(originales="mejoró")), "")


class ContrasteSeparadoTests(unittest.TestCase):
    """2. La evidencia separa lo demostrado (comportamiento) de lo no demostrado (la causa)."""

    def test_evidence_text_separates_behavior_from_cause(self):
        c = {"caso": {"args": [[{"id": 1}]], "kwargs": {}},
             "referencia": {"ejecucion": "completa", "salida": {"inconsistencias": [], "saldos": {}}},
             "candidato": {"ejecucion": "completa", "salida": {"inconsistencias": [{"id": 1}], "saldos": {}}}}
        t = CT.texto_para_programador(c, "f", "el registro tiene que incluir X")
        self.assertIn("DEMOSTRADO: en esta entrada tu código no cumple el contrato.", t)
        self.assertIn("NO DEMOSTRADO: la causa. El revisor cree que la propiedad responsable es: el registro tiene que "
                      "incluir X", t)
        self.assertIn("claves distintas: 'inconsistencias'", t)
        self.assertNotIn("CONFIRMADA", t)


if __name__ == "__main__":
    unittest.main()
