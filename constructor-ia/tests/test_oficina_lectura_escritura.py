"""Experimento 7.3, corrección B del localizador (OFICINA.md, «Experimento 7.3 — corrección B»).

Corrida real 20261024-031338: las únicas citas verificadas de Gemma fueron las líneas 31 y 32 (zona 27–37), y la
rama inaccesible decía que «aplicados» solo guarda movimientos que no son anulaciones (línea 44). Esa relación causal
no llegaba al localizador. Corrección B: si una variable se LEE en una zona anclada y el revisor la nombra en su
diagnóstico causal, se agregan las zonas de sus ESCRITURAS en la misma función, con límites y registro. Sin «línea N»
(C), sin transitividad y sin casos ocultos. SIN inferencias reales.
"""
import ast
import json
import unittest
from unittest import mock

from constructor_ia.oficina import parches as PA
from constructor_ia.oficina import revision_existente as RE
from constructor_ia.oficina.misiones_programacion import REFERENCIA_CONCILIAR

from .helpers import bwrap_works
from .test_oficina_hipotesis import CASO_DE_ANULACION, A, diagnostico
from .test_oficina_parches import COMO_QWEN, F, LITERALES, parche
from .test_oficina_revision_existente import Base

# Diagnóstico equivalente al de la corrida real: citas verificadas en 31 y 32; «aplicados» y la línea 44 solo en la
# rama inaccesible.
CITAS_REALES = ['if ref["tipo"] == "anulacion":', 'ignorar(m, "anulacion_de_anulacion")']
RAMA_REAL = ("aplicados solo guarda movimientos que no son anulaciones (línea 44): la anulación 4 no encuentra a la 3 "
             "y nunca se llega a anulacion_de_anulacion")


def diagnostico_real(rama=RAMA_REAL, **otros):
    d = diagnostico(CASO_DE_ANULACION)
    hp = d["hipotesis_principal"]
    hp.update(citas_del_codigo=CITAS_REALES, rama_inaccesible=rama,
              secuencia_causal=["la anulación 4 busca a la 3 entre los movimientos aplicados",
                                "no la encuentra y se ignora con un motivo equivocado"],
              propiedad_a_cambiar="el registro de los movimientos aplicados tiene que incluir las anulaciones")
    hp.update(otros)
    return d


def texto(rama=RAMA_REAL, **otros):
    d = diagnostico_real(rama, **otros)
    hp = d["hipotesis_principal"]
    return RE.texto_causal_del_revisor({"principal": {k: hp.get(k) for k in (
        "regla_del_contrato", "secuencia_causal", "rama_inaccesible", "propiedad_a_cambiar")}, "hechos": []})


def localizar(texto_causal, codigo=A, citas=CITAS_REALES, literales=LITERALES):
    return PA.localizar(codigo, F, citas, literales, (), texto_causal)


class FundamentalTests(unittest.TestCase):
    def test_the_real_run_diagnosis_now_yields_zones_27_37_and_41_46(self):
        self.assertEqual(localizar("")["zonas"], [(27, 37)], "sin texto causal: lo que pasó en la corrida real")
        loc = localizar(texto())
        self.assertEqual(loc["zonas"], [(27, 37), (41, 46)])
        self.assertEqual(loc["relaciones"], [{"variable": "aplicados", "lectura": 27, "escritura": 45,
                                              "tipo_escritura": "asignación de un elemento", "zona": (41, 46),
                                              "resultado": "agregada"}])
        self.assertIn("relaciones lectura → escritura", loc["metodo"])

    def test_the_causal_text_comes_only_from_the_principal_prose_and_facts(self):
        d = {"principal": {"regla_del_contrato": "R", "secuencia_causal": ["S1", "S2"], "rama_inaccesible": "RI",
                           "propiedad_a_cambiar": "P", "citas": [{"texto": "aplicados.get(x)", "verificada": True}]},
             "hechos": ["H"], "secundarias": [{"descripcion": "aplicados", "propiedad_a_cambiar": "aplicados"}]}
        self.assertEqual(RE.texto_causal_del_revisor(d), "R\nS1\nS2\nRI\nP\nH")


class NegativasTests(unittest.TestCase):
    def test_a_variable_not_named_by_the_reviewer_does_not_widen(self):
        t = texto(rama="la anulación de una anulación nunca se detecta",
                  secuencia_causal=["la anulación 4 apunta a la 3", "sale otro motivo"],
                  propiedad_a_cambiar="detectar la anulación de una anulación")
        self.assertEqual(localizar(t)["zonas"], [(27, 37)])

    def test_line_references_are_not_anchors(self):
        loc = localizar("el problema está en la línea 44 y en la línea 45")
        self.assertEqual((loc["zonas"], loc["relaciones"]), ([(27, 37)], []))

    def test_named_but_not_read_in_an_anchored_zone_does_not_widen(self):
        loc = localizar("saldos y conciliacion quedan bien; inconsistencias también")
        self.assertEqual((loc["zonas"], loc["relaciones"]), ([(27, 37)], []))

    def test_names_match_whole_identifiers_only(self):
        for t in ("movimientos_aplicados", "inaplicados", "aplicadosX", "APLICADOS"):
            with self.subTest(texto=t):
                self.assertEqual(localizar(t)["zonas"], [(27, 37)])

    def test_limit_a_plain_word_equal_to_the_variable_counts_as_a_mention(self):
        # Límite conocido y aceptado: «aplicados» también es una palabra común del contrato; si el revisor la usa en
        # prosa («los movimientos aplicados»), cuenta como mención de la variable.
        self.assertEqual(localizar("la anulación busca entre los movimientos aplicados")["zonas"], [(27, 37), (41, 46)])

    def test_top_level_initialization_is_not_a_related_write(self):
        loc = localizar(texto())
        self.assertNotIn(3, [r["escritura"] for r in loc["relaciones"]], "«vistos, aplicados, anulados = …» (línea 3)")

    def test_no_transitivity_from_added_zones(self):
        # «tipo» se lee en la zona agregada 41–46, no en la anclada 27–37: su escritura (línea 9) no entra.
        loc = localizar(texto() + " porque tipo es anulacion")
        self.assertEqual(loc["zonas"], [(27, 37), (41, 46)])
        self.assertNotIn("tipo", [r["variable"] for r in loc["relaciones"]])

    def test_writes_already_inside_an_anchored_zone_are_recorded_as_covered(self):
        loc = localizar("ref queda en None")
        self.assertEqual(loc["zonas"], [(27, 37)])
        self.assertEqual([(r["variable"], r["escritura"], r["resultado"]) for r in loc["relaciones"]],
                         [("ref", 27, "ya cubierta")])


# Código sintético para los límites: una lectura anclada y escrituras en bloques separados, en otra función y en una
# función anidada.
SINTETICO = '''def f(datos):
    estado = {}
    st = 0
    for d in datos:
        a = 1
        if d in estado and st:
            continue
        b = 2
        c = 3
    for d in datos:
        x1 = 1
        estado[d] = 1
        x2 = 2
    for d in datos:
        x3 = 1
        estado.update({d: 2})
        x4 = 2
    for d in datos:
        x5 = 1
        estado.pop(d, None)
        x6 = 2
    st += 1

    def ayudar():
        estado["z"] = 0
    return estado


def g(estado):
    estado["x"] = 1
    return estado
'''
CITA_SINTETICA = ["if d in estado and st:"]


def lineas_de(fragmento):
    return [i for i, ln in enumerate(SINTETICO.splitlines(), 1) if fragmento in ln]


class LimitesTests(unittest.TestCase):
    def loc(self, texto_causal):
        return PA.localizar(SINTETICO, "f", CITA_SINTETICA, [], (), texto_causal)

    def test_at_most_two_related_zones_and_the_rest_recorded(self):
        loc = self.loc("estado no se actualiza")
        rel = {r["escritura"]: r["resultado"] for r in loc["relaciones"]}
        self.assertEqual(rel, {lineas_de("estado[d] = 1")[0]: "agregada",
                               lineas_de("estado.update")[0]: "agregada",
                               lineas_de("estado.pop")[0]: "límite de 2 zonas relacionadas"})
        self.assertEqual(len(loc["zonas"]), 3)

    def test_writes_in_nested_or_other_functions_are_excluded(self):
        escritas = [r["escritura"] for r in self.loc("estado no se actualiza")["relaciones"]]
        for fuera in ('estado["z"] = 0', 'estado["x"] = 1'):
            self.assertNotIn(lineas_de(fuera)[0], escritas)
        nodo = next(n for n in ast.parse(SINTETICO).body if n.name == "f")
        self.assertEqual([e["linea"] for e in PA.escrituras(nodo, "estado")],
                         [lineas_de(x)[0] for x in ("estado[d] = 1", "estado.update", "estado.pop")])

    def test_short_names_are_ignored(self):
        loc = self.loc("st sigue en 0")
        self.assertEqual(loc["relaciones"], [], "«st» se lee en la zona y se escribe abajo, pero es demasiado corto")

    def test_the_line_limit_is_respected(self):
        with mock.patch.object(PA, "MAX_LINEAS_ZONAS", 7):
            loc = self.loc("estado no se actualiza")
        self.assertTrue(all(r["resultado"] != "agregada" for r in loc["relaciones"]))
        self.assertIn("límite de 7 líneas", {r["resultado"] for r in loc["relaciones"]})


@unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
class SimulacionTests(Base):
    def test_repair_simulation_with_the_widened_zones(self):
        inf, fake, d = self.correr([json.dumps(diagnostico_real(), ensure_ascii=False), parche(COMO_QWEN)],
                                   variante="7.3")
        rep = inf["reparaciones"][0]
        self.assertEqual(rep["localizacion"]["zonas"], [(27, 37), (41, 46)])
        self.assertEqual([(c["lineas"], c["verificada"]) for c in rep["localizacion"]["citas"]],
                         [([31], True), ([32], True)])
        self.assertEqual([(r["variable"], r["lectura"], r["escritura"], r["resultado"])
                          for r in rep["localizacion"]["relaciones"]], [("aplicados", 27, 45, "agregada")])
        self.assertEqual(json.loads((d / "reparacion-1/parche.json").read_text())["aplicacion"],
                         [{"cambio": 1, "lineas": [43, 43], "lineas_nuevas": 2}])
        self.assertEqual(rep["discrepancia_sintetica"], "eliminada")
        self.assertTrue(inf["exito"], inf.get("detenido"))
        self.assertEqual((inf["verificacion"]["aprobados"], inf["verificacion"]["total"]), (9, 9))
        usuario = fake.requests[1]["messages"][1]["content"]
        self.assertIn("ZONA AUTORIZADA 2 (líneas 41–46)", usuario)
        self.assertTrue(inf["auditoria_de_fugas"]["sin_fugas"])
        self.assertNotIn(REFERENCIA_CONCILIAR, usuario)

    def test_without_the_causal_mention_the_same_patch_is_still_out_of_zone(self):
        d = diagnostico_real(rama="la anulación de una anulación nunca se detecta",
                             secuencia_causal=["la anulación 4 apunta a la 3", "sale otro motivo"],
                             propiedad_a_cambiar="detectar la anulación de una anulación")
        inf, _, _ = self.correr([json.dumps(d, ensure_ascii=False), parche(COMO_QWEN)], variante="7.3",
                                max_reparaciones=1)
        rep = inf["reparaciones"][0]
        self.assertEqual(rep["localizacion"]["zonas"], [(27, 37)])
        self.assertIn("fuera de las zonas autorizadas", rep["parche"]["motivo"])
        self.assertFalse(inf["exito"])


if __name__ == "__main__":
    unittest.main()
