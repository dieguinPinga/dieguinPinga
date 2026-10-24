"""Experimento 7.12 (OFICINA.md, «Experimento 7.12»): torneo de seis soluciones independientes.

SIN inferencias reales: los seis candidatos se simulan (un Ollama falso con gemma y qwen) y el sandbox es real. Orden
fijo de los pedidos: A parche (qwen) · B parche (qwen) · C diagnóstico (gemma), parche (qwen) · D localización (gemma),
parche (qwen) · E parche (gemma) · F plan (qwen), parche (qwen). Demuestran la arquitectura, no la capacidad.
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
from constructor_ia.oficina import reparacion_coordinada as RC
from constructor_ia.oficina import reparacion_coordinada_10 as RC10
from constructor_ia.oficina import reparacion_coordinada_11 as RC11
from constructor_ia.oficina import reparacion_guiada as RGD
from constructor_ia.oficina import reparacion_torneo_12 as RT12
from constructor_ia.oficina import revision_existente as RE
from constructor_ia.oficina.misiones_programacion import MISIONES, REFERENCIA_CONCILIAR
from constructor_ia.ollama import OllamaClient

from .fake_ollama import FakeOllama
from .helpers import bwrap_works
from .test_oficina_lectura_escritura import diagnostico_real
from .test_oficina_normalizacion_sangria import QWEN
from .test_oficina_parches import IRRELEVANTE, parche
from .test_oficina_reparacion_coordinada_8 import INVERTIR, gemma8
from .test_oficina_reparacion_coordinada_9 import APRUEBA, NO_CAUSA
from .test_oficina_reparacion_coordinada_10 import PLAN_OK as PLAN_OK_10
from .test_oficina_reparacion_coordinada_11 import FUERA_DE_ZONA, PLAN_OK as PLAN_11, diag11, plan11
from .test_oficina_reparacion_guiada import INEXISTENTE, ROMPE_OTRA_REGLA

F = "conciliar_movimientos"
# El mismo programa que QWEN (con la sangría ya correcta): otro texto, otra clave de parche.
QWEN_EXACTO = {"original": '        if tipo != "anulacion":', "reemplazo": "        if True:"}


def p(cambio, marca=""):
    return cambio if isinstance(cambio, str) else parche(cambio, justificacion=f"[{marca}] justificación")


def torneo(a, b, c, d, e, f_, diag_c=None, loc_d=None, plan_f=None):
    """Las nueve respuestas en el orden de los pedidos."""
    return [p(a, "MARCA-A"), p(b, "MARCA-B"), diag_c or gemma8("MARCA-C"), p(c, "MARCA-C2"),
            loc_d or diag11(causa="[MARCA-D] las anulaciones aplicadas no quedan registradas"), p(d, "MARCA-D2"),
            p(e, "MARCA-E"), plan_f or plan11('if tipo != "anulacion":', "[MARCA-F] quitar la condición del registro"),
            p(f_, "MARCA-F2")]


@unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
class TorneoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from .test_oficina_revision_existente import Base
        Base.setUpClass()
        cls.ex = Base.ex
        b = Base()
        _, _, cls.origen = b.correr([json.dumps(diagnostico_real(), ensure_ascii=False), INEXISTENTE, INEXISTENTE,
                                     INEXISTENTE], variante="7.3")
        b.doCleanups()

    def correr(self, respuestas, mod=RT12, **k):
        fake = FakeOllama(respuestas, models=("gemma4:12b", "qwen2.5-coder:7b"))
        self.addCleanup(fake.close)
        f = RC.preparar(self.origen)
        destino = mod.carpeta(Path(tempfile.mkdtemp(prefix="cia-exp712-")), self.origen)
        inf = mod.correr(destino, f, self.ex, OllamaClient(fake.url), RM.compuerta(fake.url),
                         PG.modelo_remoto("gemma4:12b", num_predict=RE.NUM_PREDICT_REVISOR, timeout_s=600),
                         PG.modelo_remoto("qwen2.5-coder:7b"), **k)
        return inf, fake, destino

    def cand(self, inf, letra):
        return next(c for c in inf["candidatos"] if c["letra"] == letra)

    def test_candidates_are_independent(self):
        inf, fake, _ = self.correr(torneo(QWEN, INVERTIR, NO_CAUSA, IRRELEVANTE, FUERA_DE_ZONA, QWEN))
        self.assertEqual([r["model"] for r in fake.requests],
                         ["qwen2.5-coder:7b", "qwen2.5-coder:7b", "gemma4:12b", "qwen2.5-coder:7b", "gemma4:12b",
                          "qwen2.5-coder:7b", "gemma4:12b", "qwen2.5-coder:7b", "qwen2.5-coder:7b"])
        propias = {3: "MARCA-C", 5: "MARCA-D", 8: "MARCA-F"}       # pedido → la marca de SU propia cadena
        marcas = ["MARCA-A", "MARCA-B", "MARCA-C", "MARCA-C2", "MARCA-D", "MARCA-D2", "MARCA-E", "MARCA-F", "MARCA-F2"]
        for i, r in enumerate(fake.requests):
            texto = json.dumps(r["messages"], ensure_ascii=False)
            for marca in marcas:
                if propias.get(i) == marca:
                    self.assertIn(marca, texto)
                else:
                    self.assertNotIn(f"[{marca}]" if marca.endswith(("2", "A", "B", "E")) else marca, texto,
                                     f"el pedido {i} contiene {marca}")
            for otro in ("superó", "rechazado en la etapa", "bateria", "ganador"):
                self.assertNotIn(otro, texto)
        self.assertEqual(self.cand(inf, "C")["pasos"],
                         [{"etapa": "diagnostico", "modelo": "gemma4:12b"}, {"etapa": "parche", "modelo": "qwen2.5-coder:7b"}])

    def test_no_contamination_between_patches(self):
        antes = hashlib.sha256((Path(self.origen) / "inicial/solucion.py").read_bytes()).hexdigest()
        inf, _, d = self.correr(torneo(QWEN, INVERTIR, NO_CAUSA, IRRELEVANTE, FUERA_DE_ZONA, QWEN))
        original = inf["huella_del_codigo_original"]
        for c in inf["candidatos"]:
            self.assertEqual((c["huella_base"], c["huella_original_despues"]), (original, original))
        a = (d / "candidato-A/solucion.py").read_text()
        b = (d / "candidato-B/solucion.py").read_text()
        self.assertIn("if True:", a)
        self.assertNotIn("if True:", b, "el parche de A no llega a B")
        self.assertIn('if tipo == "anulacion":', b)
        self.assertNotIn('if tipo != "anulacion":', b)
        self.assertEqual(hashlib.sha256((Path(self.origen) / "inicial/solucion.py").read_bytes()).hexdigest(), antes)

    def test_only_one_of_six_passes(self):
        inf, _, _ = self.correr(torneo(INVERTIR, NO_CAUSA, ROMPE_OTRA_REGLA, IRRELEVANTE, FUERA_DE_ZONA, QWEN))
        self.assertTrue(inf["exito"])
        self.assertEqual((inf["ganadores"], inf["ganador_por_desempate"]), (["F"], "F"))
        filas = {x["letra"]: tuple(x[k] for k in RT12.ETAPAS) for x in inf["tabla"]}
        self.assertEqual(filas["A"][:3], (True, True, False))
        self.assertEqual(filas["C"], (True, True, True, True, False), "pasa lo observable, falla la batería")
        self.assertEqual(filas["E"][0], False)
        self.assertIn("fuera de las zonas autorizadas", self.cand(inf, "E")["motivo"])
        self.assertEqual(filas["F"], (True,) * 5)
        self.assertEqual((self.cand(inf, "F")["bateria"]["aprobados"], self.cand(inf, "F")["bateria"]["total"]), (9, 9))
        self.assertEqual(inf["clasificacion"], RT12.CLASIFICACION_12 + " (ganadores: F)")

    def test_none_passes(self):
        inf, _, _ = self.correr(torneo(INVERTIR, NO_CAUSA, ROMPE_OTRA_REGLA, IRRELEVANTE, FUERA_DE_ZONA, INVERTIR))
        self.assertFalse(inf["exito"])
        self.assertEqual((inf["ganadores"], inf["ganador_por_desempate"], inf["clasificacion"]), ([], None, None))
        self.assertIn("ningún candidato", inf["motivo"])

    def test_identical_programs_and_duplicated_answers(self):
        inf, _, _ = self.correr(torneo(QWEN, QWEN, NO_CAUSA, IRRELEVANTE, QWEN_EXACTO, INVERTIR))
        b, e = self.cand(inf, "B"), self.cand(inf, "E")
        self.assertEqual((b["duplicado_de"], b["tipo_de_duplicado"]), ("A", "la misma respuesta de parche"))
        self.assertEqual((e["duplicado_de"], e["tipo_de_duplicado"]), ("A", "el mismo programa resultante (otro texto)"))
        self.assertEqual(e["bateria_reutilizada_de"], "A", "la batería corre una vez por programa")
        self.assertEqual(inf["ganadores"][0], "E", "desempate: sin normalización antes que con normalización")
        self.assertEqual(sorted(inf["ganadores"]), ["A", "B", "E"])
        self.assertIn("no una afirmación", inf["criterio_de_desempate"])
        self.assertEqual(inf["programas_distintos"], 4)

    def test_out_of_zone_changes_are_rejected(self):
        inf, _, d = self.correr(torneo(FUERA_DE_ZONA, INVERTIR, NO_CAUSA, IRRELEVANTE, FUERA_DE_ZONA, QWEN))
        a = self.cand(inf, "A")
        self.assertEqual(a["etapas"]["1_parche_valido_y_en_zona"], False)
        self.assertFalse((d / "candidato-A/solucion.py").exists())

    def test_no_battery_leaks(self):
        inf, fake, _ = self.correr(torneo(QWEN, INVERTIR, NO_CAUSA, IRRELEVANTE, FUERA_DE_ZONA, QWEN))
        todo = json.dumps(fake.requests, ensure_ascii=False)
        for prohibido in ("Caso oculto", "anulaciones_no_registradas", REFERENCIA_CONCILIAR, "9/9",
                          RE.DEFECTOS[F]["anulaciones_no_registradas"]["explicacion"],
                          RGD.CAUSAS[(F, "anulaciones_no_registradas")]["texto"]):
            self.assertNotIn(prohibido, todo)
        for c in MISIONES[F][0]()["spec"]["cases"]:
            if not c.get("visible", True) and c.get("categoria"):
                self.assertNotIn(c["categoria"], todo)
        self.assertTrue(inf["auditoria_de_fugas"]["sin_fugas"])

    def test_invalid_chain_budget_and_records(self):
        inf, fake, d = self.correr(torneo(QWEN, INVERTIR, None, NO_CAUSA, FUERA_DE_ZONA, QWEN,
                                          diag_c="no es JSON")[:3] + torneo(0, 0, 0, NO_CAUSA, FUERA_DE_ZONA, QWEN)[4:])
        c = self.cand(inf, "C")
        self.assertIn("diagnostico inválido", c["motivo"])
        self.assertEqual(len(fake.requests), 8, "C no llega a pedir el parche")
        self.assertTrue(self.cand(inf, "D")["llamadas"])
        for rel in ("candidato-A/parche/pedido.json", "candidato-A/parche/respuesta.txt", "candidato-A/candidato.json",
                    "candidato-A/solucion.py", "candidato-F/plan/respuesta.txt", "informe.json"):
            self.assertTrue((d / rel).exists(), rel)
        inf, fake, _ = self.correr([], max_segundos=0)
        self.assertEqual(fake.requests, [])
        self.assertTrue(all(c["motivo"].startswith("no ejecutado") for c in inf["candidatos"]))
        self.assertEqual(RT12.MAX_LLAMADAS, 9)

    def test_previous_experiments_still_work(self):
        inf, _, _ = self.correr([diag11(), PLAN_11, APRUEBA, parche(QWEN)], mod=RC11)
        self.assertTrue(inf["exito"])
        inf, _, _ = self.correr([gemma8(), PLAN_OK_10, APRUEBA, parche(QWEN)], mod=RC10)
        self.assertTrue(inf["exito"])

    def test_cli(self):
        lab = Path(tempfile.mkdtemp(prefix="cia-exp712-cli-")) / "oficina-torneo-7-12"
        fake = FakeOllama(torneo(INVERTIR, NO_CAUSA, ROMPE_OTRA_REGLA, IRRELEVANTE, FUERA_DE_ZONA, QWEN),
                          models=("gemma4:12b", "qwen2.5-coder:7b"))
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
                rc = cli.main(["--config", str(lab.parent / "no-existe.toml"), "oficina", "reparar-torneo-12",
                               "--corrida", str(self.origen), "--carpeta", str(lab), "--trabajador", "prueba", *extra])
            return rc, out.getvalue() + err.getvalue()
        rc, out = cli_("--mostrar-pedido")
        self.assertEqual(rc, 0, out)
        self.assertIn("--- estrategias ---", out)
        self.assertIn("ESTRATEGIA:", out)
        self.assertEqual(fake.requests, [])
        self.assertFalse((lab / "experimento-7-12").exists())
        rc, out = cli_("--autorizo-inferencia-remota")
        self.assertEqual(rc, 0, out)
        self.assertIn("  F [✓ ✓ ✓ ✓ ✓]", out)
        self.assertIn("ganadores: F", out)
        self.assertIn("Experimento 7.12: ÉXITO", out)


if __name__ == "__main__":
    unittest.main()
