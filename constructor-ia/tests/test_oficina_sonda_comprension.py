"""Experimento 7.4 (OFICINA.md, «Experimento 7.4»): sonda de comprensión causal del programador.

SIN inferencias reales: la corrida de origen se genera con el recorrido 7.3 simulado (revisor y programador falsos,
sandbox real) y la sonda consulta a un Ollama falso. Se comprueba qué recibe el modelo, qué no recibe, qué se
registra, la evaluación separada y que la corrida original no se modifica.
"""
import hashlib
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
from constructor_ia.oficina import revision_existente as RE
from constructor_ia.oficina import sonda_comprension as SC
from constructor_ia.oficina.misiones_programacion import REFERENCIA_CONCILIAR
from constructor_ia.ollama import OllamaClient

from .fake_ollama import FakeOllama
from .helpers import bwrap_works
from .test_oficina_hipotesis import CASO_DE_ANULACION
from .test_oficina_lectura_escritura import diagnostico_real
from .test_oficina_parches import parche
from .test_oficina_revision_existente import Base, revision

INCORRECTO = parche({"original": "    total = 0", "reemplazo": "    total = 1"})
BUENA = ("Cuando la anulación 4 apunta a la 3, se busca ref = aplicados.get(3). Pero la anulación 3 nunca se guardó en "
         "aplicados, porque el registro está dentro de la condición if tipo != \"anulacion\": las anulaciones "
         "aplicadas no se registran en aplicados. Entonces ref es None y se ignora con anulacion_inexistente. Por eso "
         "nunca se llega a evaluar ref[\"tipo\"] == \"anulacion\".")
MALA = ("La anulación 4 revierte el efecto del movimiento 3 y resta el saldo. La rama ref[\"tipo\"] == \"anulacion\" "
        "no se alcanza porque el saldo queda negativo.")
SOLO_OBSERVA = "La anulación 3 no está en aplicados, así que ref es None."


def huellas(carpeta: Path) -> dict:
    return {str(p.relative_to(carpeta)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(carpeta.rglob("*")) if p.is_file()}


class EvaluacionTests(unittest.TestCase):
    def test_a_causal_explanation_is_identified(self):
        ev = SC.evaluar(BUENA)
        self.assertTrue(ev["identifica_ausencia_en_aplicados"])
        self.assertTrue(ev["observa_que_la_referida_no_esta"])
        self.assertTrue(ev["explica_ref_none"])
        self.assertTrue(ev["cita_la_condicion"])
        self.assertFalse(ev["contiene_codigo"])
        self.assertFalse(ev["propone_cambios"])

    def test_a_wrong_explanation_is_not(self):
        ev = SC.evaluar(MALA)
        self.assertEqual([ev[k] for k in ("identifica_ausencia_en_aplicados", "observa_que_la_referida_no_esta",
                                          "explica_ref_none", "cita_la_condicion")], [False] * 4)

    def test_observing_the_absence_is_separated_from_explaining_its_cause(self):
        ev = SC.evaluar(SOLO_OBSERVA)
        self.assertEqual((ev["observa_que_la_referida_no_esta"], ev["identifica_ausencia_en_aplicados"]),
                         (True, False))

    def test_code_changes_and_paraphrase_are_flagged(self):
        ev = SC.evaluar(BUENA + "\nHabría que agregar el registro.\n```python\naplicados[mid] = m\n```")
        self.assertTrue(ev["contiene_codigo"])
        self.assertTrue(ev["propone_cambios"])
        copia = "Las anulaciones aplicadas no se registran en aplicados."
        ev = SC.evaluar(copia, "Rama que no se alcanza: las anulaciones aplicadas no se registran en aplicados.")
        self.assertTrue(ev["identifica_ausencia_en_aplicados"])
        self.assertTrue(ev["solo_parafrasis_del_revisor"])


@unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
class SondaTests(Base):
    def corrida(self, **conf):
        _, _, d = self.correr([json.dumps(diagnostico_real(), ensure_ascii=False), INCORRECTO, INCORRECTO, INCORRECTO],
                              variante="7.3", **conf)
        return d

    def test_sources_are_the_real_zones_and_the_demonstrated_diagnosis(self):
        d = self.corrida()
        f = SC.fuentes(d)
        self.assertEqual((f["mision"], f["zonas"], f["revision"]), ("conciliar_movimientos", [(27, 37), (41, 46)], 1))
        self.assertIn("aplicados solo guarda", f["diagnostico"]["principal"]["rama_inaccesible"])
        self.assertEqual(sorted(f["archivos"]), ["inicial/preparacion.json", "inicial/solucion.py",
                                                 "reparacion-1/reparacion.json", "revision-1/revision.json"])

    def test_the_request_has_zones_diagnosis_and_questions_but_no_code_request_nor_forbidden_data(self):
        f = SC.fuentes(self.corrida())
        sistema, usuario = (m["content"] for m in SC.mensajes(f))
        for texto in ("ZONA AUTORIZADA 1 (líneas 27–37)", "ZONA AUTORIZADA 2 (líneas 41–46)",
                      ' 44|         if tipo != "anulacion":', "Rama que no se alcanza: aplicados solo guarda",
                      "referencia (con 'anula') a otro movimiento que también es una anulación",
                      'no se alcanza la rama `ref["tipo"] == "anulacion"`', "No escribas código ni propongas cambios"):
            self.assertIn(texto, usuario)
        self.assertIn("NO escribís código, NO proponés cambios ni parches", sistema)
        for prohibido in ("for m in sorted(", "EVIDENCIA VERIFICADA", "QUÉ HACER", "ver la evidencia", "Caso oculto",
                          "9/9", "anulaciones_no_registradas", REFERENCIA_CONCILIAR, "```python",
                          RE.DEFECTOS["conciliar_movimientos"]["anulaciones_no_registradas"]["explicacion"],
                          "identifica_ausencia", "anulaciones exitosas"):
            self.assertNotIn(prohibido, usuario)

    def test_one_inference_recorded_literally_with_its_metadata_and_a_separate_evaluation(self):
        d = self.corrida()
        antes = huellas(d)
        fake = FakeOllama([BUENA], models=("qwen2.5-coder:7b",))
        self.addCleanup(fake.close)
        lab = Path(tempfile.mkdtemp(prefix="cia-sonda-"))
        destino = SC.carpeta_de_sonda(lab, d)
        r = SC.correr(destino, SC.fuentes(d), OllamaClient(fake.url), RM.compuerta(fake.url),
                      PG.modelo_remoto("qwen2.5-coder:7b"))
        self.assertTrue(r["ok"])
        self.assertEqual(len(fake.requests), 1)
        req = fake.requests[0]
        self.assertEqual(req["model"], "qwen2.5-coder:7b")
        self.assertEqual({k: req["options"][k] for k in ("num_ctx", "num_predict", "temperature")},
                         {"num_ctx": 8192, "num_predict": 2048, "temperature": 0.2})
        self.assertEqual((destino / "respuesta.txt").read_text(), BUENA, "respuesta literal")
        meta = json.loads((destino / "metadatos.json").read_text())
        self.assertEqual((meta["modelo"], meta["cortada"]), ("qwen2.5-coder:7b", False))
        self.assertIn("tiempos", meta)
        ev = json.loads((destino / "evaluacion.json").read_text())
        self.assertTrue(ev["identifica_ausencia_en_aplicados"])
        origen = json.loads((destino / "origen.json").read_text())
        self.assertTrue(origen["auditoria_de_fugas"]["sin_fugas"])
        self.assertEqual(json.loads((destino / "pedido.json").read_text())[1]["content"],
                         req["messages"][1]["content"], "lo registrado es lo enviado")
        self.assertEqual(huellas(d), antes, "la corrida original no se modifica")

    def test_a_truncated_answer_is_recorded_and_still_evaluated(self):
        d = self.corrida()
        fake = FakeOllama([{"text": MALA, "done_reason": "length"}], models=("qwen2.5-coder:7b",))
        self.addCleanup(fake.close)
        destino = SC.carpeta_de_sonda(Path(tempfile.mkdtemp(prefix="cia-sonda-")), d)
        r = SC.correr(destino, SC.fuentes(d), OllamaClient(fake.url), RM.compuerta(fake.url),
                      PG.modelo_remoto("qwen2.5-coder:7b"))
        self.assertEqual((r["metadatos"]["fin"], r["evaluacion"]["respuesta_cortada"]), ("length", True))
        self.assertFalse(r["evaluacion"]["identifica_ausencia_en_aplicados"])

    def test_refusals(self):
        d = self.corrida()
        with self.assertRaisesRegex(SC.ErrorSonda, "dentro de la corrida"):
            SC.carpeta_de_sonda(d / "sub", d)
        lab = Path(tempfile.mkdtemp(prefix="cia-sonda-"))
        (lab / "taller.db").write_text("")
        with self.assertRaisesRegex(SC.ErrorSonda, "experimento anterior"):
            SC.carpeta_de_sonda(lab, d)
        _, _, d71 = self.correr([revision(CASO_DE_ANULACION), f"```python\n{REFERENCIA_CONCILIAR}```\n"])
        with self.assertRaisesRegex(SC.ErrorSonda, "zonas autorizadas"):
            SC.fuentes(d71)
        _, _, sin = self.correr([revision({"args": [[]]}), "no es JSON"], variante="7.3")
        with self.assertRaisesRegex(SC.ErrorSonda, "discrepancia demostrada"):
            SC.fuentes(sin)


@unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
class CliTests(Base):
    def test_preflight_does_not_infer_and_the_authorized_run_does_once(self):
        _, _, d = self.correr([json.dumps(diagnostico_real(), ensure_ascii=False), INCORRECTO], variante="7.3",
                              max_reparaciones=1)
        antes = huellas(d)
        lab = Path(tempfile.mkdtemp(prefix="cia-sonda-cli-")) / "oficina-sonda-7-4"
        fake = FakeOllama([BUENA], models=("qwen2.5-coder:7b",))
        self.addCleanup(fake.close)
        reg = RG.Registro(lab / "oficina.db")
        reg.registrar("prueba", fake.url, ["qwen2.5-coder:7b"], permitir_local=True)
        reg.cerrar()

        def cli_(*extra):
            out, err = StringIO(), StringIO()
            with redirect_stdout(out), redirect_stderr(err), \
                    mock.patch.object(RM, "memoria_disponible_mb", lambda: 8000):
                rc = cli.main(["--config", str(lab.parent / "no-existe.toml"), "oficina", "sondear-comprension",
                               "--corrida", str(d), "--carpeta", str(lab), "--trabajador", "prueba", *extra])
            return rc, out.getvalue() + err.getvalue()
        rc, out = cli_("--mostrar-pedido")
        self.assertEqual(rc, 0, out)
        self.assertIn("zonas 27–37, 41–46", out)
        self.assertIn("PREGUNTAS:", out)
        self.assertEqual(fake.requests, [], "la preparación no infiere")
        self.assertFalse((lab / "experimento-7-4").exists(), "ni escribe")
        rc, out = cli_("--autorizo-inferencia-remota")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(fake.requests), 1)
        self.assertIn("identifica_ausencia_en_aplicados: sí", out)
        self.assertEqual(len(list((lab / "experimento-7-4").glob("sonda-*/respuesta.txt"))), 1)
        self.assertEqual(huellas(d), antes)


if __name__ == "__main__":
    unittest.main()
