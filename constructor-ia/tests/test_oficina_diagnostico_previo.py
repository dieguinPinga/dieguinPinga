"""Experimento 7.2 (OFICINA.md, «Experimento 7.2»): diagnóstico del programador antes de la reparación.

SIN inferencias reales: revisor y programador simulados y el sandbox real. Etapa A (diagnóstico JSON, sin código),
etapa B (reparación con su diagnóstico) y etapa C (la retroalimentación del 7.1). La variante 7.1 no cambia.
"""
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from constructor_ia import cli, prompts
from constructor_ia.oficina import registro as RG
from constructor_ia.oficina import remoto as RM
from constructor_ia.oficina import revision_existente as RE
from constructor_ia.oficina.misiones_programacion import REFERENCIA_CONCILIAR, mision_conciliar_movimientos
from constructor_ia.sandbox.executor import Executor
from constructor_ia.sandbox.limiter import NullLimiter

from .fake_ollama import FakeOllama
from .helpers import bwrap_works
from .test_oficina_hipotesis import CASO_DE_ANULACION, A, bloque
from .test_oficina_retroalimentacion import CASI_IGUAL
from .test_oficina_revision_existente import Base, revision

DIAGNOSTICO = {"causa": "las anulaciones aplicadas no quedan en el registro de aplicados",
               "elementos": ['if tipo != "anulacion":', "aplicados", "variable_inventada"],
               "modificacion": "registrar también las anulaciones aplicadas",
               "justificacion": "así la anulación 4 encuentra a la 3 y sale anulacion_de_anulacion",
               "efectos_secundarios": ["anulacion_repetida usa el mismo registro"]}


def diag(**cambios):
    d = dict(DIAGNOSTICO, **cambios)
    return json.dumps({k: v for k, v in d.items() if v is not None}, ensure_ascii=False)


class InterpretarTests(unittest.TestCase):
    def test_a_valid_diagnosis_is_sanitized_and_its_elements_checked_against_the_code(self):
        d = RE.interpretar_diagnostico("Mi diagnóstico:\n" + diag(), A, "conciliar_movimientos")
        self.assertEqual([e["en_el_codigo"] for e in d["elementos"]], [True, True, False])
        self.assertEqual(d["elementos_en_el_codigo"], 2)
        self.assertEqual(d["efectos_secundarios"], ["anulacion_repetida usa el mismo registro"])
        t = RE.texto_diagnostico_propio(d)
        self.assertIn("Causa: las anulaciones aplicadas no quedan", t)
        self.assertIn("«variable_inventada» (no aparece literalmente en el código)", t)

    def test_invalid_or_incomplete_diagnoses_are_rejected_with_a_reason(self):
        casos = {"no es JSON": "no contiene un objeto JSON",
                 diag(efectos_secundarios=None): "faltan campos: efectos_secundarios",
                 diag(causa="  "): "«causa» vacío",
                 diag(elementos=[]): "al menos una variable",
                 diag(efectos_secundarios=3): "tiene que ser una lista",
                 "```python\ndef conciliar_movimientos(m):\n    return {}\n```": "incluyó el programa"}
        for texto, motivo in casos.items():
            with self.subTest(motivo=motivo):
                with self.assertRaisesRegex(ValueError, motivo):
                    RE.interpretar_diagnostico(texto, A, "conciliar_movimientos")
        self.assertIn("no se pudo usar (faltan campos)", RE.texto_diagnostico_propio(None, "faltan campos"))

    def test_without_the_variant_the_repair_request_is_exactly_the_7_1_one(self):
        spec = mision_conciliar_movimientos()["spec"]
        a = ("desc", spec, A, "DIAG", "EVID", "", 20000, ["h"], 1)
        self.assertEqual(RE.mensajes_reparacion(*a), RE.mensajes_reparacion(*a, propio=""))
        diag_msgs = RE.mensajes_diagnostico(*a)
        self.assertEqual(diag_msgs[1]["content"][:-len(RE.PEDIDO_DE_DIAGNOSTICO)],
                         RE.mensajes_reparacion(*a)[1]["content"][:-len(RE.PEDIDO_DE_CODIGO)],
                         "la etapa A recibe el mismo material que la reparación")


@unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
class CicloTests(Base):
    def pedidos(self, fake):
        return [r["messages"] for r in fake.requests]

    def test_valid_diagnosis_then_valid_repair(self):
        inf, fake, d = self.correr([revision(CASO_DE_ANULACION), diag(), bloque(REFERENCIA_CONCILIAR)],
                                   variante="7.2")
        self.assertTrue(inf["exito"], inf.get("detenido"))
        self.assertEqual(inf["variante"]["id"], "7.2")
        _, a, b = self.pedidos(fake)
        self.assertEqual(a[0]["content"], RE.SISTEMA_DIAGNOSTICO_PROGRAMADOR)
        self.assertEqual(fake.requests[1].get("format"), "json")
        self.assertTrue(a[1]["content"].endswith(RE.PEDIDO_DE_DIAGNOSTICO))
        self.assertTrue(b[0]["content"].startswith(prompts.BUILDER_SYSTEM))
        propio = RE.texto_diagnostico_propio(inf["reparaciones"][0]["diagnostico_propio"])
        self.assertEqual(b[1]["content"], a[1]["content"][:-len(RE.PEDIDO_DE_DIAGNOSTICO)] + propio + "\n\n"
                         + RE.PEDIDO_DE_CODIGO, "la etapa B es el pedido del 7.1 más su diagnóstico")
        self.assertIn("Causa: las anulaciones aplicadas no quedan en el registro de aplicados", b[1]["content"])
        self.assertEqual(len(inf["reparaciones"]), 1, "A + B cuentan como UN intento de reparación")
        # Registro y consumo separados por etapa.
        for f in ("diagnostico_pedido.json", "diagnostico_respuesta.txt", "diagnostico.json", "pedido.json",
                  "respuesta.md", "solucion.py"):
            self.assertTrue((d / "reparacion-1" / f).exists(), f)
        self.assertEqual([(ll["rol"], ll.get("etapa")) for ll in inf["consumo"]["llamadas"]],
                         [("revisor", None), ("programador", "diagnostico"), ("programador", "reparacion")])
        etapas = inf["consumo"]["programador_por_etapa"]
        self.assertEqual((etapas["diagnostico"]["llamadas"], etapas["reparacion"]["llamadas"]), (1, 1))
        self.assertEqual(inf["consumo"]["segundos"], round(sum(ll["segundos"] for ll in inf["consumo"]["llamadas"]), 1))
        # Sin filtraciones.
        self.assertTrue(inf["auditoria_de_fugas"]["sin_fugas"])
        for m in (a, b):
            for secreto in ("Caso oculto", "anulaciones_no_registradas", RE.DEFECTOS["conciliar_movimientos"]
                            ["anulaciones_no_registradas"]["explicacion"], REFERENCIA_CONCILIAR):
                self.assertNotIn(secreto, m[1]["content"])

    def test_an_invalid_diagnosis_does_not_add_attempts(self):
        inf, fake, d = self.correr([revision(CASO_DE_ANULACION), diag(efectos_secundarios=None),
                                    bloque(REFERENCIA_CONCILIAR)], variante="7.2", max_reparaciones=1)
        self.assertTrue(inf["exito"])
        self.assertEqual(len(fake.requests), 3)
        reg = inf["reparaciones"][0]["diagnostico_propio"]
        self.assertEqual((reg["valido"], reg["motivo"]), (False, "faltan campos: efectos_secundarios"))
        self.assertIn("Tu diagnóstico no se pudo usar (faltan campos: efectos_secundarios).",
                      fake.requests[2]["messages"][1]["content"])

    def test_a_repair_that_does_not_fix_the_case_feeds_the_next_diagnosis(self):
        inf, fake, _ = self.correr([revision(CASO_DE_ANULACION), diag(), bloque(CASI_IGUAL), diag(),
                                    bloque(REFERENCIA_CONCILIAR)], variante="7.2", max_reparaciones=2)
        self.assertTrue(inf["exito"])
        self.assertEqual(len(inf["reparaciones"]), 2)
        self.assertEqual(inf["reparaciones"][0]["discrepancia_sintetica"], "persiste")
        segundo_diagnostico = fake.requests[3]["messages"]
        self.assertEqual(segundo_diagnostico[0]["content"], RE.SISTEMA_DIAGNOSTICO_PROGRAMADOR)
        for texto in ("RESULTADO DE TU INTENTO 1 EN EL CASO DEL REVISOR", "SIGUE DISTINTA en 'inconsistencias'",
                      "SIN EFECTO EN ESTE CASO", "TUS REPARACIONES ANTERIORES"):
            self.assertIn(texto, segundo_diagnostico[1]["content"])

    def test_repeating_the_same_program_stops_as_in_7_1(self):
        inf, fake, _ = self.correr([revision(CASO_DE_ANULACION), diag(), bloque(A), diag(), bloque(A), diag(),
                                    bloque(REFERENCIA_CONCILIAR)], variante="7.2", max_reparaciones=3)
        self.assertEqual(len(fake.requests), 5, "revisor + 2 × (diagnóstico + reparación); no hay tercera")
        self.assertIn("repitió un programa ya probado después del aviso", inf["detenido"])
        self.assertEqual([r["mismo_programa_que"] for r in inf["reparaciones"]], [[0], [0]])
        self.assertIn("REPETICIÓN: tu intento 1 es el MISMO programa que la versión inicial",
                      fake.requests[3]["messages"][1]["content"])
        self.assertFalse(inf["exito"])

    def test_the_time_budget_counts_the_diagnosis(self):
        inf, fake, _ = self.correr([revision(CASO_DE_ANULACION), diag()], variante="7.2", max_segundos=0)
        self.assertEqual(fake.requests, [])
        llamadas = iter([None, None, "límite de tiempo de inferencia (simulado)"])
        with mock.patch.object(RE.Experimento, "_sin_tiempo", lambda self: next(llamadas)):
            inf, fake, _ = self.correr([revision(CASO_DE_ANULACION), diag(), bloque(REFERENCIA_CONCILIAR)],
                                       variante="7.2")
        self.assertEqual(len(fake.requests), 2, "revisor + diagnóstico; la reparación ya no entra en el presupuesto")
        self.assertEqual(inf["reparaciones"][0]["estado"], "sin_tiempo")
        self.assertIn("límite de tiempo", inf["detenido"])
        self.assertFalse(inf["exito"])

    def test_the_default_variant_keeps_the_7_1_behavior(self):
        inf, fake, d = self.correr([revision(CASO_DE_ANULACION), bloque(REFERENCIA_CONCILIAR)])
        self.assertTrue(inf["exito"])
        self.assertEqual(inf["variante"]["id"], "7.1")
        self.assertEqual(len(fake.requests), 2)
        self.assertNotIn("TU DIAGNÓSTICO", fake.requests[1]["messages"][1]["content"])
        self.assertFalse((d / "reparacion-1/diagnostico.json").exists())
        self.assertNotIn("diagnostico_propio", inf["reparaciones"][0])


@unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
class CliTests(unittest.TestCase):
    def test_the_variant_is_selectable_and_identified(self):
        d = Path(tempfile.mkdtemp(prefix="cia-exp72-cli-"))
        lab = d / "oficina-revision-7-2"
        fake = FakeOllama([revision(CASO_DE_ANULACION), diag(), bloque(REFERENCIA_CONCILIAR)], models=("gemma4:12b",))
        self.addCleanup(fake.close)
        reg = RG.Registro(lab / "oficina.db")
        reg.registrar("prueba", fake.url, ["gemma4:12b"], permitir_local=True)
        reg.cerrar()
        out, err = StringIO(), StringIO()
        ex = lambda cfg: Executor(cfg, limiter=NullLimiter(), allow_unverified_limits=True)  # noqa: E731
        with redirect_stdout(out), redirect_stderr(err), mock.patch("constructor_ia.sandbox.executor.Executor", ex), \
                mock.patch.object(RM, "memoria_disponible_mb", lambda: 8000):
            rc = cli.main(["--config", str(d / "no-existe.toml"), "oficina", "revisar-existente", "--carpeta",
                           str(lab), "--trabajador", "prueba", "--defecto", "anulaciones_no_registradas",
                           "--variante", "7.2", "--autorizo-inferencia-remota"])
        texto = out.getvalue()
        self.assertEqual(rc, 0, texto + err.getvalue())
        self.assertIn("variante 7.2 (diagnóstico previo del programador", texto)
        self.assertIn("programador (diagnostico 1)", texto)
        self.assertIn("programador (reparacion 1)", texto)
        self.assertIn("Experimento 7: ÉXITO", texto)
        inf = json.loads(next((lab / "experimento-7").glob("corrida-*/informe.json")).read_text())
        self.assertEqual(inf["variante"]["id"], "7.2")


if __name__ == "__main__":
    unittest.main()
