"""Rol REVISOR DE CÓDIGO (OFICINA.md, «Revisor»): PROGRAMADOR → REVISOR → PROGRAMADOR → EVALUADOR.

SIN inferencias reales: un Ollama falso responde en orden a las llamadas del programador y del revisor con textos
escritos en la prueba; el código se ejecuta en el sandbox real (bwrap). Las variantes de código son SOLO para
ejercitar el mecanismo (no se le dan a ningún modelo real) y no hay reglas especiales para ninguna misión.
"""
import json
import tempfile
import unittest
from pathlib import Path

from constructor_ia.oficina import cola as CO
from constructor_ia.oficina import programador as PG
from constructor_ia.oficina import revisor as RV
from constructor_ia.oficina.misiones_programacion import DESCRIPCION_CONCILIAR, REFERENCIA_CONCILIAR
from constructor_ia.ollama import OllamaClient, OllamaUnavailable
from constructor_ia.sandbox.executor import Executor
from constructor_ia.sandbox.limiter import NullLimiter

from .fake_ollama import FakeOllama
from .helpers import bwrap_works, make_cfg

# A: no registra las anulaciones aplicadas → «anular una anulación» sale «anulacion_inexistente» (falla el caso 6).
A = REFERENCIA_CONCILIAR.replace(
    '        aplicados[mid] = {"id": mid, "tipo": tipo, "producto": prod, "efecto": efecto}',
    '        if tipo != "anulacion":\n'
    '            aplicados[mid] = {"id": mid, "tipo": tipo, "producto": prod, "efecto": efecto}')
A2, A3, A4 = (A + f"\n# variante {i}\n" for i in (2, 3, 4))     # distinto código, el mismo error
D = A.replace('sorted(movimientos, key=lambda x: (x["fecha"], x["id"]))', "movimientos")   # peor que A

DIAGNOSTICO = {"resumen": "Las anulaciones aplicadas nunca se registran, así que no se las puede encontrar después.",
               "observaciones": [{
                   "regla_del_contrato": "anulacion_de_anulacion",
                   "problema": "solo se guardan en «aplicados» los ingresos y egresos, nunca las anulaciones aplicadas",
                   "donde": "el bloque que agrega a «aplicados»",
                   "por_que": "una anulación que anula a otra anulación no la encuentra y termina como "
                              "«anulacion_inexistente», cuando el contrato pide «anulacion_de_anulacion»",
                   "sugerencia": "registrar también las anulaciones aplicadas para que la regla de anulación de "
                                 "anulación pueda aplicarse"}]}
CON_CODIGO = {"resumen": "APROBADO: la solución ya es correcta.",
              "observaciones": [{"regla_del_contrato": "todas", "problema": "nada", "por_que": "está bien",
                                 "sugerencia": "usá esto:\n```python\ndef conciliar_movimientos(*a):\n    return {}\n```"}]}


def bloque(codigo):
    return f"```python\n{codigo}```\n"


class CriterioTests(unittest.TestCase):
    def h(self, n, puntaje, fallidos):
        return {"n": n, "puntaje": puntaje, "fallidos_todos": fallidos}

    def test_general_stagnation_criterion(self):
        self.assertFalse(RV.debe_revisar([self.h(1, 8, [6])])[0], "con un solo intento no hay estancamiento")
        si, motivo = RV.debe_revisar([self.h(1, 8, [6]), self.h(2, 8, [6])])
        self.assertTrue(si)
        self.assertIn("los casos 6 fallaron en los intentos 1, 2", motivo)
        self.assertFalse(RV.debe_revisar([self.h(1, 5, [3, 6]), self.h(2, 8, [6])])[0], "mejoró: no se revisa")
        self.assertFalse(RV.debe_revisar([self.h(1, 8, [6]), self.h(2, 8, [7])])[0], "fallan casos distintos")
        self.assertTrue(RV.debe_revisar([self.h(1, 8, [6]), self.h(2, 0, [1, 2, 6])])[0], "empeoró y repite el 6")

    def test_answers_are_validated_and_sanitized(self):
        d = RV.interpretar(json.dumps(CON_CODIGO))
        self.assertNotIn("def conciliar_movimientos", json.dumps(d, ensure_ascii=False))
        self.assertIn("[código omitido", d["observaciones"][0]["sugerencia"])
        for malo in ("no es json", "{}", '{"observaciones": []}', '{"observaciones": [{"problema": ""}]}'):
            with self.assertRaises(ValueError):
                RV.interpretar(malo)

    def test_reviewer_prompt_has_only_authorized_information(self):
        from constructor_ia.oficina.misiones_programacion import mision_conciliar_movimientos
        m = mision_conciliar_movimientos()
        texto = json.dumps(RV.mensajes(m["description"], m["spec"], A, "Caso oculto caso 6: fallo"), ensure_ascii=False)
        self.assertIn(DESCRIPCION_CONCILIAR[:200].replace("'", "'"), json.loads(texto)[1]["content"])
        self.assertIn("Además hay 7 casos ocultos", texto)
        for privado in ("2026-05-06", "2026-06-", '\\"Sal\\"', "devolucion"):
            self.assertNotIn(privado, texto)


@unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
class RevisorEnElCicloTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = make_cfg(sandbox_timeout_s=20, sandbox_case_timeout_s=3)
        cls.ex = Executor(cls.cfg, limiter=NullLimiter(), allow_unverified_limits=True)
        cls.ex.self_test()
        assert cls.ex.enabled, cls.ex.disabled_reason

    def correr(self, respuestas, revisor="config", cliente=OllamaClient, **cfg_revisor):
        d = Path(tempfile.mkdtemp(prefix="cia-revisor-"))
        cola = CO.Cola(d / "oficina.db")
        self.addCleanup(cola.cerrar)
        fake = FakeOllama(respuestas, models=("gemma4:12b",))
        self.addCleanup(fake.close)
        cola.crear_mision("programación: conciliar_movimientos", [{
            "nombre": "conciliar_movimientos", "rol": "programador", "modelo": "gemma4:12b",
            "carga": {"mision": "conciliar_movimientos"}, "max_intentos": 2, "tiempo_max_s": 300}])
        if revisor == "config":
            revisor = RV.ConfigRevisor(PG.modelo_remoto("gemma4:12b", num_predict=1024, timeout_s=60), **cfg_revisor)
        r = PG.Programador(d, self.cfg, self.ex, cola, log=lambda *_: None, revisor=revisor,
                           cliente_para=lambda t: cliente(t["url"])).atender(
            {"id": "pulenta", "url": fake.url, "modelos": ["gemma4:12b"], "permisos": ["inferencia"], "capacidad": 1,
             "disponible_ahora": True})
        taller = PG.Taller(d, self.cfg, self.ex, fake.url, PG.modelo_remoto("gemma4:12b"), cliente=object())
        self.addCleanup(taller.cerrar)
        resumen = json.loads(taller.files.read_rel(r["informe"]["mision_taller"], "mejor/resumen.json"))
        return r, fake, taller, cola, d, resumen

    @staticmethod
    def del_revisor(fake):
        return [i for i, q in enumerate(fake.requests) if q["messages"][0]["content"] == RV.SISTEMA_REVISOR]

    def test_stuck_programmer_reviewer_finds_the_contradiction_and_the_programmer_fixes_it(self):
        r, fake, taller, cola, d, resumen = self.correr(
            [bloque(A), bloque(A2), json.dumps(DIAGNOSTICO), bloque(REFERENCIA_CONCILIAR)])
        inf = r["informe"]
        self.assertEqual([i["passed"] for i in inf["intentos"]], [8, 8, 9])
        self.assertEqual((inf["estado"], inf["intento_final"]), ("aprobada", 3))
        self.assertEqual(self.del_revisor(fake), [2], "el revisor intervino una vez, antes del intento 3")
        rev = fake.requests[2]
        self.assertEqual((rev["model"], rev["format"], rev["options"]["num_predict"]), ("gemma4:12b", "json", 1024))
        pedido = rev["messages"][1]["content"]
        self.assertIn("CONTRATO DE LA MISIÓN", pedido)
        self.assertIn("# variante 2", pedido, "revisa el candidato vigente (el intento 2, igual puntaje)")
        self.assertIn("Caso oculto caso 6: fallo", pedido)
        for privado in ("2026-05-06", "anulacion_de_anulacion\\\", \\\"id"):
            self.assertNotIn(privado, json.dumps(rev["messages"], ensure_ascii=False))
        tercero = fake.requests[3]["messages"][1]["content"]
        self.assertIn("OBSERVACIONES DEL REVISOR DE CÓDIGO", tercero)
        self.assertIn("solo se guardan en «aplicados» los ingresos y egresos", tercero)
        rj = json.loads(taller.files.read_rel(inf["mision_taller"], "intento-3/revision.json"))
        self.assertEqual(rj["estado"], "hecha")
        self.assertIn("estancamiento", rj["criterio"])
        self.assertEqual(rj["diagnostico"]["observaciones"][0]["regla_del_contrato"], "anulacion_de_anulacion")
        self.assertIn("mensajes", rj, "el pedido completo al revisor queda para auditoría")
        self.assertEqual([x["estado"] for x in resumen["revisiones"]], ["hecha"])
        self.assertIn("OBSERVACIONES DEL REVISOR",
                      taller.files.read_rel(inf["mision_taller"], "intento-3/retroalimentacion.md"))
        for n, codigo in ((1, A), (2, A2), (3, REFERENCIA_CONCILIAR)):
            self.assertEqual(taller.files.read_rel(inf["mision_taller"], f"intento-{n}/solucion.py").strip(),
                             codigo.strip())
        ok, detalle = PG.verificar(cola, d, self.cfg, self.ex, r["tarea"])
        self.assertTrue(ok, detalle)

    def test_no_review_while_the_programmer_improves(self):
        r, fake, *_ = self.correr([bloque(D), bloque(A), bloque(REFERENCIA_CONCILIAR)])
        self.assertEqual([i["passed"] for i in r["informe"]["intentos"]][1:], [8, 9])
        self.assertEqual(self.del_revisor(fake), [], "progresó: ninguna inferencia de revisión")

    def test_the_reviewer_cannot_approve_nor_replace_the_solution(self):
        r, fake, taller, cola, d, resumen = self.correr(
            [bloque(A), bloque(A2), json.dumps(CON_CODIGO), bloque(A3), bloque(A4)], max_revisiones=1)
        inf = r["informe"]
        self.assertEqual(inf["estado"], "fallida", "decir «APROBADO» no aprueba nada")
        self.assertEqual(inf["mejor"]["aprobados"], 8)
        tercero = fake.requests[3]["messages"][1]["content"]
        self.assertIn("[código omitido", tercero)
        self.assertNotIn("return {}", tercero, "el código del revisor no llega como solución")
        self.assertIn("# variante 2", tercero, "el punto de partida sigue siendo el código del programador")
        mejor = taller.files.read_rel(inf["mision_taller"], "mejor/solucion.py")
        self.assertNotIn("return {}", mejor)
        ok, detalle = PG.verificar(cola, d, self.cfg, self.ex, r["tarea"])
        self.assertFalse(ok)
        self.assertIn("reejecución independiente: 8/9", detalle)

    def test_an_invalid_answer_is_recorded_and_the_cycle_continues(self):
        r, fake, taller, *_rest, resumen = self.correr(
            [bloque(A), bloque(A2), "esto no es JSON", bloque(REFERENCIA_CONCILIAR)])
        self.assertEqual(r["informe"]["estado"], "aprobada")
        rv = resumen["revisiones"][0]
        self.assertEqual(rv["estado"], "fallida")
        self.assertIn("respuesta inválida del revisor", rv["motivo"])
        self.assertNotIn("OBSERVACIONES DEL REVISOR", fake.requests[3]["messages"][1]["content"])

    def test_an_unavailable_reviewer_is_recorded_and_the_best_code_is_kept(self):
        class RevisorCaido(OllamaClient):
            def chat(self, model, messages, cancel=None, on_progress=None, fmt=None):
                if fmt == "json":
                    raise OllamaUnavailable("No se pudo contactar a Ollama: caído")
                return super().chat(model, messages, cancel, on_progress, fmt)
        r, fake, taller, *_rest, resumen = self.correr([bloque(A), bloque(A2), bloque(A3), bloque(A4)],
                                                       cliente=RevisorCaido)
        self.assertEqual(r["informe"]["estado"], "fallida")
        self.assertEqual(r["informe"]["mejor"]["aprobados"], 8)
        self.assertEqual([x["estado"] for x in resumen["revisiones"]], ["fallida", "fallida"])
        self.assertIn("el revisor no respondió", resumen["revisiones"][0]["motivo"])
        self.assertEqual(len(r["informe"]["intentos"]), 4, "el programador siguió sus 4 intentos")

    def test_call_and_time_limits_are_respected(self):
        r, fake, taller, *_rest, resumen = self.correr(
            [bloque(A), bloque(A2), json.dumps(DIAGNOSTICO), bloque(A3), bloque(A4)], max_revisiones=1)
        self.assertEqual(self.del_revisor(fake), [2], "una sola llamada, aunque siguió estancado")
        self.assertEqual([x["estado"] for x in resumen["revisiones"]], ["hecha", "omitida"])
        self.assertIn("límite de 1 revisiones", resumen["revisiones"][1]["motivo"])
        r, fake, taller, *_rest, resumen = self.correr([bloque(A), bloque(A2), bloque(A3), bloque(A4)],
                                                       max_segundos=0)
        self.assertEqual(self.del_revisor(fake), [])
        self.assertIn("límite de tiempo de revisión", resumen["revisiones"][0]["motivo"])

    def test_without_a_reviewer_the_previous_behavior_is_unchanged(self):
        r, fake, *_rest, resumen = self.correr([bloque(A), bloque(A2), bloque(A3), bloque(A4)], revisor=None)
        self.assertEqual(len(fake.requests), 4)
        self.assertEqual(self.del_revisor(fake), [])
        self.assertEqual(resumen["revisiones"], [])


if __name__ == "__main__":
    unittest.main()
