"""Experimento 7.1 (OFICINA.md, «Experimento 7.1»): retroalimentación específica entre intentos del programador.

SIN inferencias reales: revisor y programador simulados y el sandbox real. Una reparación ejecutable que no elimina
la discrepancia recibe el resultado CONCRETO de su intento en el caso sintético (salida real y la que pide el
contrato, clave por clave), su cambio de código y si repitió un programa o una salida, nunca datos ocultos.
"""
import json
import unittest

from constructor_ia.oficina import revision_existente as RE
from constructor_ia.oficina.misiones_programacion import REFERENCIA_CONCILIAR

from .helpers import bwrap_works
from .test_oficina_hipotesis import CASO_DE_ANULACION, A, bloque
from .test_oficina_revision_existente import Base, revision

# Reparación «casi idéntica» (como las de Qwen): cambia código que no afecta la salida del caso.
CASI_IGUAL = A.replace("    n = 0\n", "    n = 0\n    procesados = 0\n", 1).replace(
    "        mid, tipo = m[\"id\"], m[\"tipo\"]\n", "        mid, tipo = m[\"id\"], m[\"tipo\"]\n        procesados += 1\n", 1)
# El mismo programa que CASI_IGUAL con otro formato y comentarios.
CASI_IGUAL_REFORMATEADO = "# segunda vuelta\n" + CASI_IGUAL.replace("    procesados = 0\n",
                                                                     "    procesados = 0  # contador\n\n", 1)
# Elimina la discrepancia del caso, pero rompe un ejemplo visible (egresos sin control de saldo).
SIN_CONTROL_DE_SALDO = REFERENCIA_CONCILIAR.replace("if saldos.get(prod, 0) + efecto < 0:", "if False:")


class UnidadesTests(unittest.TestCase):
    def test_outputs_are_compared_key_by_key_with_missing_and_extra_elements(self):
        esperado = {"saldos": {"X": 10}, "movimientos_aplicados": 3,
                    "inconsistencias": [{"id": 4, "motivo": "anulacion_de_anulacion"}], "conciliacion": None}
        obtenido = dict(esperado, inconsistencias=[{"id": 4, "motivo": "anulacion_inexistente"}], saldos={"X": 7},
                        extra=1)
        c = RE.comparar_salidas(esperado, obtenido)
        self.assertEqual(c["distintas"], ["saldos", "inconsistencias", "extra"])
        self.assertEqual(c["coinciden"], ["movimientos_aplicados", "conciliacion"])
        self.assertEqual(c["detalle"]["inconsistencias"]["faltan"], [{"id": 4, "motivo": "anulacion_de_anulacion"}])
        self.assertEqual(c["detalle"]["inconsistencias"]["sobran"], [{"id": 4, "motivo": "anulacion_inexistente"}])
        self.assertEqual(c["detalle"]["saldos"]["subclaves_distintas"], ["X"])
        self.assertFalse(RE.comparar_salidas(esperado, "excepción ValueError")["comparable"])

    def test_same_program_ignores_format_and_comments_but_not_logic(self):
        self.assertEqual(RE.programa(CASI_IGUAL), RE.programa(CASI_IGUAL_REFORMATEADO))
        self.assertNotEqual(RE.programa(CASI_IGUAL), RE.programa(A))
        c = RE.cambio_de_codigo(A, CASI_IGUAL)
        self.assertEqual((c["lineas_agregadas"], c["lineas_quitadas"], c["sin_cambios"]), (2, 0, False))
        self.assertIn("+    procesados = 0", c["diff"])
        self.assertTrue(RE.cambio_de_codigo(CASI_IGUAL, CASI_IGUAL_REFORMATEADO)["sin_cambios"])

    def test_an_exception_is_reported_as_a_non_comparable_output(self):
        caso = {"args": [[{"id": 1}]], "kwargs": {}}
        t = RE.texto_retroalimentacion(2, 1, "f", caso, {"saldos": {}}, {"ejecucion": "excepcion",
                                                                       "salida": "excepción KeyError: 'fecha'"},
                                       RE.comparar_salidas({"saldos": {}}, None), RE.cambio_de_codigo("a = 1\n", "a = 2\n"),
                                       [], [], "persiste")
        self.assertIn("Tu intento 2 no devolvió una salida comparable: excepcion — excepción KeyError: 'fecha'", t)
        self.assertIn("QUÉ CAMBIASTE respecto de tu intento 1 (+1 / -1 líneas", t)
        self.assertIn("NO es una prueba oculta", t)


@unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
class CicloTests(Base):
    def pedidos(self, fake):
        return [r["messages"][1]["content"] for r in fake.requests]

    def test_a_failed_repair_gets_its_concrete_result_its_diff_and_repetitions(self):
        inf, fake, d = self.correr([revision(CASO_DE_ANULACION), bloque(CASI_IGUAL), bloque(CASI_IGUAL_REFORMATEADO),
                                    bloque(REFERENCIA_CONCILIAR)], max_reparaciones=3)
        self.assertTrue(inf["exito"], inf.get("detenido"))
        _, primero, segundo, tercero = self.pedidos(fake)
        # Intento 1: resultado concreto, clave por clave, y el cambio respecto de la versión inicial.
        for texto in ("RESULTADO DE TU INTENTO 1 EN EL CASO DEL REVISOR (caso sintético; NO es una prueba oculta)",
                      'Salida que pide el contrato: {"saldos": {"X": 10}',
                      'Salida de tu intento 1: {"saldos": {"X": 10}',
                      "RESULTADO: SIGUE DISTINTA en 'inconsistencias'. Ya coinciden: 'saldos', "
                      "'movimientos_aplicados', 'conciliacion'.",
                      """- 'inconsistencias': falta [{"id": 4, "motivo": "anulacion_de_anulacion"}] · sobra """
                      """[{"id": 4, "motivo": "anulacion_inexistente"}]""",
                      "SIN EFECTO EN ESTE CASO: tu intento 1 devuelve exactamente la misma salida que la versión inicial",
                      "QUÉ CAMBIASTE respecto de la versión inicial (+2 / -0 líneas, sin comentarios):",
                      "+    procesados = 0", "Versión actual de solucion.py (tu intento 1)",
                      "TUS REPARACIONES ANTERIORES", "- intento 1 (desde la versión inicial): +2/-0 líneas; el caso "
                      "sigue distinto en 'inconsistencias'; misma salida que la versión inicial"):
            self.assertIn(texto, segundo)
        self.assertNotIn("EVIDENCIA VERIFICADA", segundo, "reemplaza la evidencia de la versión inicial")
        # Intento 2: el mismo programa con otro formato.
        self.assertIn("REPETICIÓN: tu intento 2 es el MISMO programa que tu intento 1", tercero)
        self.assertIn("QUÉ CAMBIASTE respecto de tu intento 1: nada (sin contar comentarios).", tercero)
        self.assertIn("- intento 2 (desde tu intento 1): sin cambios de código; el caso sigue distinto en "
                      "'inconsistencias'; mismo programa que tu intento 1", tercero)
        self.assertEqual(len({primero, segundo, tercero}), 3, "ningún pedido se repite sin información nueva")
        # Registro.
        r1, r2, _ = inf["reparaciones"]
        self.assertEqual((r1["claves_distintas"], r1["misma_salida_que"], r1["mismo_programa_que"]),
                         (["inconsistencias"], [0], []))
        self.assertEqual((r2["mismo_programa_que"], r2["cambio"]["sin_cambios"]), ([1], True))
        self.assertIn("SIGUE DISTINTA", (d / "reparacion-1/retroalimentacion.md").read_text())
        # Separación: nada de la batería oculta.
        self.assertTrue(inf["auditoria_de_fugas"]["sin_fugas"])
        for p in (segundo, tercero):
            for secreto in ("Caso oculto", "/9", "anulaciones_no_registradas"):
                self.assertNotIn(secreto, p)

    def test_repeating_after_the_warning_stops_without_spending_more_inference(self):
        inf, fake, _ = self.correr([revision(CASO_DE_ANULACION), bloque(CASI_IGUAL), bloque(CASI_IGUAL),
                                    bloque(CASI_IGUAL_REFORMATEADO), bloque(REFERENCIA_CONCILIAR)], max_reparaciones=4)
        self.assertEqual(len(fake.requests), 4, "revisor + 3 reparaciones; la cuarta no se pide")
        self.assertIn("repitió un programa ya probado después del aviso", inf["detenido"])
        self.assertFalse(inf["exito"])
        self.assertEqual(inf["verificacion_informativa"]["intento"], 2)

    def test_a_repair_that_fixes_the_case_but_breaks_a_visible_example_gets_only_public_evidence(self):
        inf, fake, _ = self.correr([revision(CASO_DE_ANULACION), bloque(SIN_CONTROL_DE_SALDO),
                                    bloque(REFERENCIA_CONCILIAR)])
        self.assertTrue(inf["exito"])
        segundo = self.pedidos(fake)[2]
        self.assertIn("RESULTADO: en este caso tu intento ya devuelve lo que pide el contrato.", segundo)
        self.assertIn("Tu intento 1 no cumple los ejemplos visibles del contrato:", segundo)
        self.assertIn("Caso 2:", segundo)
        self.assertNotIn("Caso oculto", segundo)
        self.assertEqual(inf["reparaciones"][0]["visibles"], {"ok": False, "aprobados": 1, "total": 2})

    def test_the_record_keeps_every_request_response_and_feedback(self):
        inf, fake, d = self.correr([revision(CASO_DE_ANULACION), bloque(CASI_IGUAL), bloque(REFERENCIA_CONCILIAR)])
        for n in (1, 2):
            for f in ("pedido.json", "respuesta.md", "solucion.py", "reparacion.json"):
                self.assertTrue((d / f"reparacion-{n}" / f).exists(), (n, f))
        enviado = json.loads((d / "reparacion-2/pedido.json").read_text())[1]["content"]
        self.assertIn((d / "reparacion-1/retroalimentacion.md").read_text(), enviado,
                      "lo registrado es exactamente lo que se envió")


if __name__ == "__main__":
    unittest.main()
