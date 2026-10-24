"""Experimento 7.3, corrección A del localizador (OFICINA.md, «Experimento 7.3 — corrección A»).

Defecto (commit f66aab9): revisor.interpretar acepta y VERIFICA citas de hasta 2 líneas (cita_en_codigo normaliza el
código entero), pero parches.anclas buscaba cada cita dentro de UNA sola línea: una cita verificada de las líneas
44–45 se descartaba en silencio y la zona 41–46 (registro en «aplicados») quedaba afuera. Corrección: ubicar la cita
con el mismo criterio de la verificación, en la ventana mínima de líneas consecutivas, y registrar cada cita con sus
líneas o el motivo por el que no ancló. NO incluye relaciones lectura-escritura (B) ni referencias «línea N» (C).
SIN inferencias reales.
"""
import json
import unittest

from constructor_ia.oficina import parches as PA
from constructor_ia.oficina import revisor as RV
from constructor_ia.oficina.misiones_programacion import REFERENCIA_CONCILIAR

from .helpers import bwrap_works
from .test_oficina_hipotesis import CASO_DE_ANULACION, A, diagnostico
from .test_oficina_parches import COMO_QWEN, F, LITERALES, parche
from .test_oficina_revision_existente import Base

REGISTRO = 'aplicados[mid] = {"id": mid, "tipo": tipo, "producto": prod, "efecto": efecto}'
# Cita de dos líneas (44–45), como la acepta y verifica revisor.interpretar.
CITA_DOS_LINEAS = 'if tipo != "anulacion":\n            ' + REGISTRO
# La misma relación escrita en una sola línea de texto: abarca dos líneas del código.
CITA_EN_UNA_LINEA = 'if tipo != "anulacion": ' + REGISTRO


def revision_con_citas(citas, **otros):
    d = diagnostico(CASO_DE_ANULACION)
    d["hipotesis_principal"]["citas_del_codigo"] = citas
    d["hipotesis_principal"].update(otros)
    return json.dumps(d, ensure_ascii=False)


class CitasTests(unittest.TestCase):
    def test_a_verified_two_line_citation_now_anchors_both_lines_and_the_zone_41_46(self):
        cita = RV.interpretar(revision_con_citas([CITA_DOS_LINEAS]), A)["principal"]["citas"][0]
        self.assertTrue(cita["verificada"], "el revisor la verifica (criterio sobre el código entero)")
        loc = PA.localizar(A, F, [cita["texto"]], LITERALES)
        self.assertEqual(loc["zonas"], [(27, 37), (41, 46)], "en f66aab9 daba solo [(27, 37)]")
        self.assertEqual(loc["citas"], [{"texto": cita["texto"], "verificada": True, "lineas": [44, 45], "motivo": None}])
        self.assertEqual(sorted(a["linea"] for a in loc["anclas"] if a["origen"] == "cita del revisor"), [44, 45])

    def test_a_one_line_text_spanning_two_code_lines_is_located(self):
        self.assertTrue(RV.cita_en_codigo(CITA_EN_UNA_LINEA, A))
        self.assertEqual(PA.lineas_de_cita(A, CITA_EN_UNA_LINEA), [44, 45])
        self.assertIn((41, 46), PA.localizar(A, F, [CITA_EN_UNA_LINEA], LITERALES)["zonas"])

    def test_single_line_citations_behave_as_before(self):
        loc = PA.localizar(A, F, ['if tipo != "anulacion":'], LITERALES)
        self.assertEqual(loc["zonas"], [(27, 37), (41, 46)])
        self.assertEqual(sorted(a["linea"] for a in loc["anclas"]), [29, 32, 44])
        antes = [i for i, ln in enumerate(A.splitlines(), 1) if "continue" in ln]      # criterio de f66aab9
        self.assertEqual(PA.lineas_de_cita(A, "continue"), antes,
                         "una cita corta sigue anclando todas sus apariciones")

    def test_unverified_or_unlocatable_citations_never_anchor_and_are_recorded(self):
        loc = PA.localizar(A, F, [], LITERALES, ["línea 44: if tipo != 'anulacion'"])
        self.assertEqual(loc["zonas"], [(27, 37)])
        self.assertEqual(loc["citas"], [{"texto": "línea 44: if tipo != 'anulacion'", "verificada": False,
                                         "lineas": [], "motivo": "no verificada: no aparece en el código"}])
        corregido = A.replace('        if tipo != "anulacion":\n            ' + REGISTRO, "        " + REGISTRO)
        loc = PA.localizar(corregido, F, [CITA_DOS_LINEAS], LITERALES)
        self.assertEqual(loc["citas"][0]["lineas"], [])
        self.assertIn("no se ubicó", loc["citas"][0]["motivo"], "verificada al principio, ya no está en esta versión")

    def test_location_uses_the_same_criterion_as_verification(self):
        lineas = A.splitlines()
        for i in range(len(lineas) - 1):
            for cita in (lineas[i], lineas[i] + "\n" + lineas[i + 1], lineas[i].strip() + " " + lineas[i + 1].strip()):
                if cita.strip():
                    with self.subTest(linea=i + 1, cita=cita[:40]):
                        self.assertEqual(bool(PA.lineas_de_cita(A, cita)), RV.cita_en_codigo(cita, A))

    def test_correction_a_does_not_add_read_write_zones(self):
        # Sin citas, los literales solo llevan a 27–37: la relación aplicados.get ↔ aplicados[mid] = (B) no existe.
        self.assertEqual(PA.localizar(A, F, [], LITERALES)["zonas"], [(27, 37)])


@unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
class CicloTests(Base):
    def test_the_real_run_pattern_two_line_citation_then_a_patch_on_line_43(self):
        # Diagnóstico con la cita de dos líneas y el parche estilo Qwen sobre la línea 43: en f66aab9 el parche se
        # rechazaba por caer fuera de la zona autorizada (solo 27–37).
        inf, fake, d = self.correr([revision_con_citas([CITA_DOS_LINEAS]), parche(COMO_QWEN)], variante="7.3")
        rep = inf["reparaciones"][0]
        self.assertEqual(rep["localizacion"]["zonas"], [(27, 37), (41, 46)])
        self.assertEqual(rep["localizacion"]["citas"][0]["lineas"], [44, 45])
        self.assertTrue(rep["parche"]["aplicado"])
        self.assertTrue(inf["exito"], inf.get("detenido"))
        self.assertEqual(inf["verificacion"]["aprobados"], 9)
        self.assertIn("ZONA AUTORIZADA 2 (líneas 41–46)", fake.requests[1]["messages"][1]["content"])
        registrado = json.loads((d / "reparacion-1/reparacion.json").read_text())["localizacion"]["citas"]
        self.assertEqual(registrado[0]["lineas"], [44, 45], "queda auditable en el registro")
        self.assertTrue(inf["auditoria_de_fugas"]["sin_fugas"])
        self.assertNotIn(REFERENCIA_CONCILIAR, fake.requests[1]["messages"][1]["content"])

    def test_unverified_citations_are_recorded_in_the_run(self):
        # Prosa causal sin nombrar variables: mide solo la corrección A (la B no interviene).
        inf, _, _ = self.correr([revision_con_citas(["línea 44: aplicados solo guarda no anulaciones"],
                                                    secuencia_causal=["la anulación 4 apunta a la 3",
                                                                      "sale otro motivo"],
                                                    propiedad_a_cambiar="detectar la anulación de una anulación"),
                                 parche(COMO_QWEN)], variante="7.3", max_reparaciones=1)
        rep = inf["reparaciones"][0]
        self.assertEqual(rep["localizacion"]["zonas"], [(27, 37)])
        self.assertEqual([(c["verificada"], c["motivo"]) for c in rep["localizacion"]["citas"]],
                         [(False, "no verificada: no aparece en el código")])
        self.assertFalse(rep["parche"]["aplicado"])
        self.assertIn("fuera de las zonas autorizadas", rep["parche"]["motivo"])


if __name__ == "__main__":
    unittest.main()
