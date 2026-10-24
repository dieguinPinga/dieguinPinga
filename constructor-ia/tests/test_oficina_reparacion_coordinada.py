"""Experimento 7.7 (OFICINA.md, «Experimento 7.7»): diagnóstico (gemma) y reparación (qwen) coordinados.

SIN inferencias reales: diagnosticadora y programador simulados (un Ollama falso con los dos modelos) y el sandbox
real. Lo central: el diagnóstico que recibe el programador SALE de la respuesta de la diagnosticadora (marcas únicas
por prueba, y funciona aun sin la causa del 7.5/7.6), la diagnosticadora no recibe la causa ni datos ocultos, y un
diagnóstico insuficiente detiene el experimento sin sustituirlo.
"""
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from constructor_ia import cli
from constructor_ia.oficina import programador as PG
from constructor_ia.oficina import registro as RG
from constructor_ia.oficina import remoto as RM
from constructor_ia.oficina import reparacion_coordinada as RC
from constructor_ia.oficina import reparacion_guiada as RGD
from constructor_ia.oficina import revision_existente as RE
from constructor_ia.oficina.misiones_programacion import REFERENCIA_CONCILIAR
from constructor_ia.ollama import OllamaClient

from .fake_ollama import FakeOllama
from .helpers import bwrap_works
from .test_oficina_hipotesis import A
from .test_oficina_lectura_escritura import diagnostico_real
from .test_oficina_normalizacion_sangria import QWEN
from .test_oficina_parches import IRRELEVANTE, parche
from .test_oficina_reparacion_guiada import INEXISTENTE

F = "conciliar_movimientos"
ZONAS = [(27, 37), (41, 46)]


def gemma(marca="MARCA-7f3", **cambios):
    d = {"causa": f"[{marca}] la escritura en aplicados está dentro de la condición del tipo, así que las "
                  "anulaciones aplicadas no se registran en aplicados",
         "lineas_citadas": ['if tipo != "anulacion":', '            ref = aplicados.get(m.get("anula"))'],
         "mecanismo": ["la anulación 3 se aplica pero no queda en aplicados",
                       "la anulación 4 busca a la 3 con aplicados.get y obtiene None"],
         "relacion_con_la_discrepancia": "por eso sale anulacion_inexistente en lugar de anulacion_de_anulacion"}
    d.update(cambios)
    return json.dumps({k: v for k, v in d.items() if v is not None}, ensure_ascii=False)


class InterpretarTests(unittest.TestCase):
    def test_a_valid_diagnosis_with_citations_checked_against_the_zones(self):
        d = RC.interpretar_diagnostico(gemma(), A, ZONAS, F)
        self.assertEqual([(x["lineas"], x["en_zona"]) for x in d["lineas_citadas"]], [([44], True), ([27], True)])
        self.assertIn("[MARCA-7f3]", RC.texto_para_el_programador(d))

    def test_insufficient_diagnoses_are_rejected_with_the_reason(self):
        casos = {"no es JSON": "no contiene un objeto JSON",
                 gemma(causa=None): "falta «causa»",
                 gemma(mecanismo=["uno"]): "al menos 2 pasos",
                 gemma(relacion_con_la_discrepancia=""): "relacion_con_la_discrepancia",
                 gemma(lineas_citadas=["if algo_inexistente:"]): "ninguna línea citada",
                 gemma(lineas_citadas=["saldos = dict(saldos_iniciales or {})"]): "ninguna línea citada",
                 "```python\ndef conciliar_movimientos(m):\n    return {}\n```": "incluyó el programa"}
        for texto, motivo in casos.items():
            with self.subTest(motivo=motivo):
                with self.assertRaisesRegex(ValueError, motivo):
                    RC.interpretar_diagnostico(texto, A, ZONAS, F)

    def test_the_identification_is_evaluated_afterwards_and_separately(self):
        bien = RC.interpretar_diagnostico(gemma(), A, ZONAS, F)
        mal = RC.interpretar_diagnostico(gemma(causa="el saldo queda negativo",
                                               mecanismo=["se resta el saldo", "queda negativo"],
                                               relacion_con_la_discrepancia="por el saldo",
                                               lineas_citadas=['if ref["tipo"] == "anulacion":']), A, ZONAS, F)
        self.assertEqual((RC.evaluar_diagnostico(bien)["identifica_la_causa"],
                          RC.evaluar_diagnostico(mal)["identifica_la_causa"]), (True, False))


@unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
class CicloTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from .test_oficina_revision_existente import Base
        Base.setUpClass()
        cls.ex = Base.ex
        b = Base()
        _, _, cls.origen = b.correr([json.dumps(diagnostico_real(), ensure_ascii=False), INEXISTENTE, INEXISTENTE,
                                     INEXISTENTE], variante="7.3")
        b.doCleanups()

    def correr(self, respuestas, **k):
        fake = FakeOllama(respuestas, models=("gemma4:12b", "qwen2.5-coder:7b"))
        self.addCleanup(fake.close)
        f = RC.preparar(self.origen)
        destino = RC.carpeta(Path(tempfile.mkdtemp(prefix="cia-exp77-")), self.origen)
        inf = RC.correr(destino, f, self.ex, OllamaClient(fake.url), RM.compuerta(fake.url),
                        PG.modelo_remoto("gemma4:12b", num_predict=RE.NUM_PREDICT_REVISOR, timeout_s=600),
                        PG.modelo_remoto("qwen2.5-coder:7b"), **k)
        return inf, fake, destino

    def contenido(self, fake, i):
        return fake.requests[i]["messages"][1]["content"]

    def test_the_diagnostician_gets_the_observable_discrepancy_but_not_the_cause(self):
        inf, fake, _ = self.correr([gemma(), parche(QWEN)])
        pedido = fake.requests[0]
        self.assertEqual((pedido["model"], pedido["options"]["num_predict"], pedido.get("format")),
                         ("gemma4:12b", 2048, "json"))
        self.assertEqual(pedido["messages"][0]["content"], RC.SISTEMA_DIAGNOSTICO)
        texto = self.contenido(fake, 0)
        for esperado in ("ZONA AUTORIZADA 1 (líneas 27–37)", "ZONA AUTORIZADA 2 (líneas 41–46)",
                         "DISCREPANCIA OBSERVADA", "Lo que pide el contrato:", "Lo que devuelve el código:",
                         "'inconsistencias': falta", "anulacion_de_anulacion", "anulacion_inexistente"):
            self.assertIn(esperado, texto)
        causa_del_equipo = RGD.CAUSAS[(F, "anulaciones_no_registradas")]["texto"]
        for prohibido in (causa_del_equipo, "no quedan registradas", "aplicados solo guarda", "Rama que no se alcanza",
                          "anulaciones_no_registradas", RE.DEFECTOS[F]["anulaciones_no_registradas"]["explicacion"],
                          REFERENCIA_CONCILIAR, "Caso oculto", "9/9", "DIAGNÓSTICO DEL REVISOR"):
            self.assertNotIn(prohibido, texto)

    def test_positive_integration_the_programmer_uses_the_diagnosticians_answer(self):
        with mock.patch.dict(RGD.CAUSAS, {}, clear=True):          # sin ninguna causa escrita por el equipo
            inf, fake, d = self.correr([gemma("MARCA-a91"), parche(QWEN)])
        self.assertTrue(inf["exito"], inf["motivo"])
        self.assertEqual(inf["clasificacion"],
                         "diagnóstico y reparación automáticos coordinados (con normalización de la sangría)")
        self.assertEqual([r["model"] for r in fake.requests], ["gemma4:12b", "qwen2.5-coder:7b"])
        programador = self.contenido(fake, 1)
        self.assertIn(RC.TITULO_DIAGNOSTICO, programador)
        self.assertIn("[MARCA-a91]", programador, "el diagnóstico sale de la respuesta de la diagnosticadora")
        self.assertNotIn(RGD.TITULO_CAUSA, programador)
        self.assertTrue(inf["diagnostico_en_cada_pedido_del_programador"])
        self.assertEqual(inf["criterios"], {"1_diagnosticadora_identifica_la_causa": True,
                                            "2_programador_recibe_ese_diagnostico_y_genera_un_parche": True,
                                            "3_coordinador_aplica_el_parche": True,
                                            "4_bateria_independiente_completa": True})
        self.assertEqual((inf["reparacion"]["verificacion"]["aprobados"], inf["reparacion"]["verificacion"]["total"]),
                         (9, 9))
        for rel in ("diagnostico-1/pedido.json", "diagnostico-1/respuesta.txt", "diagnostico-1/diagnostico.json",
                    "reparacion/intento-1/pedido.json", "reparacion/intento-1/parche.json", "reparacion/informe.json",
                    "informe.json"):
            self.assertTrue((d / rel).exists(), rel)
        self.assertEqual((d / "diagnostico-1/respuesta.txt").read_text(), gemma("MARCA-a91"))

    def test_different_diagnoses_reach_the_programmer_unchanged(self):
        for marca in ("MARCA-uno", "MARCA-dos"):
            with self.subTest(marca=marca):
                _, fake, _ = self.correr([gemma(marca), parche(QWEN)])
                self.assertIn(f"[{marca}]", self.contenido(fake, 1))

    def test_an_insufficient_diagnosis_stops_without_substitution(self):
        inf, fake, d = self.correr([gemma(causa=None), "no es JSON", parche(QWEN)])
        self.assertEqual([r["model"] for r in fake.requests], ["gemma4:12b", "gemma4:12b"], "el programador no se llama")
        self.assertIn("no produjo un diagnóstico válido en 2 intento(s)", inf["detenido"])
        self.assertIsNone(inf["reparacion"])
        self.assertFalse((d / "reparacion").exists())
        self.assertFalse(any(inf["criterios"].values()))
        self.assertFalse(inf["exito"])
        self.assertIn("Tu diagnóstico anterior no se pudo usar (falta «causa»)", self.contenido(fake, 1))
        causa_del_equipo = RGD.CAUSAS[(F, "anulaciones_no_registradas")]["texto"]
        self.assertFalse(any(causa_del_equipo in json.dumps(r, ensure_ascii=False) for r in fake.requests))

    def test_a_second_diagnosis_can_succeed(self):
        inf, fake, _ = self.correr([gemma(lineas_citadas=["if algo:"]), gemma("MARCA-2"), parche(QWEN)])
        self.assertTrue(inf["exito"])
        self.assertEqual([d["valido"] for d in inf["diagnosticos"]], [False, True])
        self.assertIn("[MARCA-2]", self.contenido(fake, 2))

    def test_a_wrong_cause_is_not_counted_as_success_even_if_the_patch_works(self):
        mal = gemma(causa="el saldo queda negativo", mecanismo=["se resta el saldo", "queda negativo"],
                    relacion_con_la_discrepancia="por el saldo", lineas_citadas=['if ref["tipo"] == "anulacion":'])
        inf, _, _ = self.correr([mal, parche(QWEN)])
        self.assertTrue(inf["reparacion"]["exito"], "el parche funciona")
        self.assertFalse(inf["criterios"]["1_diagnosticadora_identifica_la_causa"])
        self.assertFalse(inf["exito"])
        self.assertIsNone(inf["clasificacion"])

    def test_failed_repairs_and_budget(self):
        inf, fake, _ = self.correr([gemma(), parche(IRRELEVANTE), parche(IRRELEVANTE), parche(IRRELEVANTE),
                                    parche(QWEN)])
        self.assertEqual(len(fake.requests), 4, "1 diagnóstico + 3 parches como máximo")
        self.assertFalse(inf["exito"])
        self.assertIn("ningún parche corrigió el defecto en 3 intentos", inf["motivo"])
        inf, fake, _ = self.correr([gemma(), parche(QWEN)], max_segundos=0)
        self.assertEqual(fake.requests, [])
        self.assertIn("límite de tiempo", inf["detenido"])

    def test_cli_preflight_and_run(self):
        lab = Path(tempfile.mkdtemp(prefix="cia-exp77-cli-")) / "oficina-coordinada-7-7"
        fake = FakeOllama([gemma(), parche(QWEN)], models=("gemma4:12b", "qwen2.5-coder:7b"))
        self.addCleanup(fake.close)
        reg = RG.Registro(lab / "oficina.db")
        reg.registrar("prueba", fake.url, ["gemma4:12b", "qwen2.5-coder:7b"], permitir_local=True)
        reg.cerrar()
        from constructor_ia.sandbox.executor import Executor
        from constructor_ia.sandbox.limiter import NullLimiter

        def cli_(*extra):
            out, err = StringIO(), StringIO()
            ex = lambda cfg: Executor(cfg, limiter=NullLimiter(), allow_unverified_limits=True)  # noqa: E731
            with redirect_stdout(out), redirect_stderr(err), mock.patch("constructor_ia.sandbox.executor.Executor", ex), \
                    mock.patch.object(RM, "memoria_disponible_mb", lambda: 8000):
                rc = cli.main(["--config", str(lab.parent / "no-existe.toml"), "oficina", "reparar-coordinado",
                               "--corrida", str(self.origen), "--carpeta", str(lab), "--trabajador", "prueba", *extra])
            return rc, out.getvalue() + err.getvalue()
        rc, out = cli_("--mostrar-pedido")
        self.assertEqual(rc, 0, out)
        self.assertIn("DISCREPANCIA OBSERVADA", out)
        self.assertIn("[OK] trabajador y planificador (gemma4:12b)", out)
        self.assertEqual(fake.requests, [])
        self.assertFalse((lab / "experimento-7-7").exists())
        rc, out = cli_("--autorizo-inferencia-remota")
        self.assertEqual(rc, 0, out)
        self.assertIn("Experimento 7.7: ÉXITO (diagnóstico y reparación automáticos coordinados", out)
        self.assertEqual(len(fake.requests), 2)


if __name__ == "__main__":
    unittest.main()
