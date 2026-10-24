"""Experimento 7 (OFICINA.md, «Experimento 7»): revisión autónoma de una solución existente.

SIN inferencias reales: revisor y programador simulados (Ollama falso) y el sandbox real (bwrap). Recorrido completo:
solución inicial con un defecto controlado → revisor autónomo → contraste → programador → verificación independiente.
"""
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from constructor_ia import cli, prompts
from constructor_ia.oficina import contraste as CT
from constructor_ia.oficina import programador as PG
from constructor_ia.oficina import registro as RG
from constructor_ia.oficina import remoto as RM
from constructor_ia.oficina import revision_existente as RE
from constructor_ia.oficina import revisor as RV
from constructor_ia.oficina.misiones_programacion import (MISIONES, REFERENCIA, REFERENCIA_CONCILIAR, VALIDADORES,
                                                          casos_conciliar, mision_conciliar_movimientos)
from constructor_ia.ollama import OllamaClient
from constructor_ia.sandbox.executor import Executor
from constructor_ia.sandbox.limiter import NullLimiter

from .fake_ollama import FakeOllama
from .helpers import bwrap_works, make_cfg
from .test_oficina_hipotesis import CASO_DE_ANULACION, CASO_SIN_DISCREPANCIA, A, bloque, diagnostico, mov

DEFECTO = RE.DEFECTOS["conciliar_movimientos"]["anulaciones_no_registradas"]
# Corrige la anulación pero introduce OTRO defecto (orden dentro del día), que el caso sintético no ejercita.
CORRIGE_A_MEDIAS = REFERENCIA_CONCILIAR.replace('sorted(movimientos, key=lambda x: (x["fecha"], x["id"]))',
                                                'sorted(movimientos, key=lambda x: x["fecha"])')
# Un caso sintético por defecto del catálogo, escrito desde el contrato: demuestra que cada defecto es verificable.
CASOS_POR_DEFECTO = {
    ("conciliar_movimientos", "anulaciones_no_registradas"): CASO_DE_ANULACION,
    ("conciliar_movimientos", "orden_solo_por_fecha"): {"args": [[
        {"id": 2, "fecha": "2026-01-01", "producto": "X", "tipo": "ingreso", "cantidad": 5},
        {"id": 1, "fecha": "2026-01-01", "producto": "X", "tipo": "egreso", "cantidad": 2}]]},
    ("conciliar_movimientos", "saldo_cero_rechazado"): {"args": [[mov(1, "ingreso", cantidad=2),
                                                                  mov(2, "egreso", cantidad=2)]]},
    ("resumen_ventas", "hasta_excluido"): {"args": [[[1, "2026-03-02", "Arroz", 1, 2]]],
                                           "kwargs": {"hasta": "2026-03-02"}},
    ("resumen_ventas", "desempate_invertido"): {"args": [[[1, "2026-03-01", "Arroz", 1, 1],
                                                          [2, "2026-03-01", "Yerba", 1, 1]]]},
}


def revision(caso):
    return json.dumps(diagnostico(caso))


@unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = make_cfg(sandbox_timeout_s=20, sandbox_case_timeout_s=3)
        cls.ex = Executor(cls.cfg, limiter=NullLimiter(), allow_unverified_limits=True)
        cls.ex.self_test()
        assert cls.ex.enabled, cls.ex.disabled_reason

    def correr(self, respuestas, *, mision="conciliar_movimientos", codigo=None, defecto="anulaciones_no_registradas",
               **conf):
        fake = FakeOllama(respuestas, models=("gemma4:12b",))
        self.addCleanup(fake.close)
        prep = RE.preparar(self.ex, mision, codigo, defecto)
        destino = RE.carpeta_de_corrida(Path(tempfile.mkdtemp(prefix="cia-exp7-")))
        c = RE.ConfigExperimento(PG.modelo_remoto("gemma4:12b"),
                                 PG.modelo_remoto("gemma4:12b", num_predict=1024, timeout_s=60), mision=mision, **conf)
        inf = RE.Experimento(destino, self.ex, OllamaClient(fake.url), RM.compuerta(fake.url), c,
                             log=lambda *_: None).correr(prep)
        return inf, fake, destino


class CatalogoTests(Base):
    def test_every_controlled_defect_is_hidden_from_the_examples_and_verifiable_with_a_contract_case(self):
        for mision, defectos in RE.DEFECTOS.items():
            fabrica, referencia = MISIONES[mision]
            ct = CT.desde_mision(fabrica, referencia, VALIDADORES[mision])
            for nombre in defectos:
                with self.subTest(defecto=nombre):
                    prep = RE.preparar(self.ex, mision, None, nombre)
                    self.assertTrue(prep["defecto_verificable"], prep["motivo"])
                    b = prep["bateria_inicial"]
                    visibles = [i + 1 for i, c in enumerate(fabrica()["spec"]["cases"]) if c.get("visible", True)]
                    self.assertFalse(set(b["fallidos"]) & set(visibles), "los ejemplos visibles no lo delatan")
                    caso, motivo = CT.leer_caso(CASOS_POR_DEFECTO[(mision, nombre)])
                    r = CT.contrastar(self.ex, ct, prep["codigo_inicial"], caso, motivo)
                    self.assertEqual(r["discrepancia"], "demostrada", r["motivo"])

    def test_preparation_rejects_a_solution_without_a_functional_defect_and_bad_defects(self):
        prep = RE.preparar(self.ex, "conciliar_movimientos")
        self.assertFalse(prep["defecto_verificable"])
        self.assertIn("aprueba toda la batería", prep["motivo"])
        inf, fake, _ = self.correr([], defecto=None)
        self.assertEqual(fake.requests, [], "sin un defecto verificable no se gasta ninguna inferencia")
        self.assertFalse(inf["exito"])
        self.assertIn("no tiene un defecto funcional verificable", inf["detenido"])
        with self.assertRaisesRegex(RE.ErrorExperimento, "defecto desconocido"):
            RE.preparar(self.ex, "conciliar_movimientos", None, "no_existe")
        with self.assertRaisesRegex(RE.ErrorExperimento, "exactamente una vez"):
            RE.preparar(self.ex, "resumen_ventas", REFERENCIA_CONCILIAR, "hasta_excluido")
        roto = RE.preparar(self.ex, "conciliar_movimientos", "def conciliar_movimientos(m, s=None, c=None):\n    "
                           "return 1/0\n")
        self.assertTrue(roto["defecto_verificable"], "falla en la batería: es un defecto funcional")
        sin_cargar = RE.preparar(self.ex, "conciliar_movimientos", "import os\nos.nope()\n"
                                 "def conciliar_movimientos(m):\n    return {}\n")
        self.assertFalse(sin_cargar["defecto_verificable"])

    def test_an_existing_file_is_the_starting_point_without_its_comments(self):
        codigo = "# Escrito por otro equipo.\n" + A.replace("    for m in sorted(", "    # recorrer en orden\n"
                                                                                  "    for m in sorted(", 1)
        prep = RE.preparar(self.ex, "conciliar_movimientos", codigo)
        self.assertEqual((prep["origen"], prep["codigo_inicial"]), ("archivo", A))
        self.assertTrue(prep["defecto_verificable"])
        self.assertIsNone(prep["defecto"])

    def test_the_lab_never_reuses_a_previous_experiment_folder(self):
        d = Path(tempfile.mkdtemp(prefix="cia-exp7-lab-"))
        (d / "taller.db").write_text("")
        with self.assertRaisesRegex(RE.ErrorExperimento, "experimento anterior"):
            RE.carpeta_de_corrida(d)
        d2 = Path(tempfile.mkdtemp(prefix="cia-exp7-lab-"))
        a, b = RE.carpeta_de_corrida(d2), RE.carpeta_de_corrida(d2)
        self.assertNotEqual(a, b, "cada corrida en su subcarpeta; nada se sobrescribe")


class RecorridoTests(Base):
    def test_full_path_reviewer_finds_the_defect_and_the_programmer_fixes_it(self):
        inf, fake, d = self.correr([revision(CASO_DE_ANULACION), bloque(REFERENCIA_CONCILIAR)])
        self.assertTrue(inf["exito"], inf)
        self.assertEqual(inf["criterios"], {"1_defecto_funcional_verificable": True,
                                            "2_caso_reproduce_discrepancia_real": True,
                                            "3_programador_modifico_la_solucion": True,
                                            "4_discrepancia_eliminada_y_verificacion_independiente": True})
        ct = inf["revisiones"][0]["contraste"]
        self.assertEqual((ct["discrepancia"], ct["hipotesis_causal"]), ("demostrada", "compatible_no_demostrada"))
        self.assertEqual(inf["reparaciones"][0]["discrepancia_sintetica"], "eliminada")
        self.assertEqual((inf["verificacion"]["ok"], inf["verificacion"]["aprobados"], inf["verificacion"]["total"]),
                         (True, 9, 9))
        # Pedido al revisor: contrato y código, ningún resultado de pruebas.
        rv = fake.requests[0]["messages"]
        self.assertEqual(rv[0]["content"], RV.SISTEMA_REVISOR_AUTONOMO)
        self.assertIn('if tipo != "anulacion":', rv[1]["content"])
        self.assertNotIn("FEEDBACK", rv[1]["content"])
        self.assertNotIn("Caso oculto", rv[1]["content"])
        # Pedido al programador: diagnóstico y evidencia, sin la batería; con las reglas de concisión.
        pg = fake.requests[1]["messages"]
        self.assertTrue(pg[0]["content"].startswith(prompts.BUILDER_SYSTEM))
        self.assertIn("El código va SIN comentarios", pg[0]["content"])
        for texto in (RV.INICIO, "DEMOSTRADO: en esta entrada tu código no cumple el contrato.",
                      "NO DEMOSTRADO: la causa.", "No hay otros resultados de pruebas."):
            self.assertIn(texto, pg[1]["content"])
        todo = json.dumps(fake.requests, ensure_ascii=False)
        for secreto in ("anulaciones_no_registradas", DEFECTO["explicacion"], "aprueba", "9/9", "8/9"):
            self.assertNotIn(secreto, todo)
        self.assertTrue(inf["auditoria_de_fugas"]["sin_fugas"])
        # Registro y consumo.
        for rel in ("inicial/solucion.py", "inicial/preparacion.json", "revision-1/pedido.json",
                    "revision-1/respuesta.txt", "revision-1/revision.json", "reparacion-1/pedido.json",
                    "reparacion-1/respuesta.md", "reparacion-1/solucion.py", "reparacion-1/reparacion.json",
                    "final/solucion.py", "informe.json"):
            self.assertTrue((d / rel).exists(), rel)
        self.assertEqual((d / "inicial/solucion.py").read_text(), A)
        self.assertEqual(json.loads((d / "informe.json").read_text())["exito"], True)
        c = inf["consumo"]
        self.assertEqual((c["total_llamadas"], c["por_rol"]["revisor"]["llamadas"],
                          c["por_rol"]["programador"]["llamadas"]), (2, 1, 1))
        self.assertEqual(inf["inicial"]["defecto"]["id"], "anulaciones_no_registradas", "solo en el registro")

    def test_a_review_without_discrepancy_gets_a_second_chance_with_only_its_own_result(self):
        inf, fake, _ = self.correr([revision(CASO_SIN_DISCREPANCIA), revision(CASO_DE_ANULACION),
                                    bloque(REFERENCIA_CONCILIAR)])
        self.assertTrue(inf["exito"])
        self.assertEqual([r["contraste"]["discrepancia"] for r in inf["revisiones"]], ["no_observada", "demostrada"])
        segunda = fake.requests[1]["messages"][1]["content"]
        self.assertIn("NOTA DEL COORDINADOR", segunda)
        self.assertIn("no mostró ninguna discrepancia", segunda)

    def test_without_a_demonstrated_discrepancy_the_programmer_is_never_called(self):
        inf, fake, _ = self.correr([revision(CASO_SIN_DISCREPANCIA), "esto no es JSON"])
        self.assertEqual(len(fake.requests), 2, "solo las dos revisiones permitidas")
        self.assertEqual([r["estado"] for r in inf["revisiones"]], ["hecha", "fallida"])
        self.assertEqual(inf["reparaciones"], [])
        self.assertEqual([k for k, v in inf["criterios"].items() if v], ["1_defecto_funcional_verificable"])
        self.assertFalse(inf["exito"])
        self.assertIn("no demostró ninguna discrepancia", inf["detenido"])

    def test_a_case_equal_to_a_hidden_input_is_not_run_nor_revealed(self):
        args, kwargs, _, _ = casos_conciliar()[5]
        inf, fake, _ = self.correr([revision({"args": args, "kwargs": kwargs}), revision(CASO_DE_ANULACION),
                                    bloque(REFERENCIA_CONCILIAR)])
        primera = inf["revisiones"][0]["contraste"]
        self.assertEqual((primera["discrepancia"], primera["referencia"]), ("inconclusa", None))
        nota = fake.requests[1]["messages"][1]["content"]
        self.assertIn("Tu caso anterior no se pudo usar: elegí otra entrada", nota)
        for palabra in ("privado", "oculto"):
            self.assertNotIn(palabra, nota.split("NOTA DEL COORDINADOR")[1])
        self.assertTrue(inf["exito"])

    def test_truncated_and_unchanged_repairs_get_only_pertinent_evidence(self):
        inf, fake, _ = self.correr([revision(CASO_DE_ANULACION),
                                    {"text": "```python\n" + REFERENCIA_CONCILIAR[:300], "done_reason": "length"},
                                    bloque(A), bloque(REFERENCIA_CONCILIAR)])
        self.assertTrue(inf["exito"])
        reps = inf["reparaciones"]
        self.assertEqual([r["estado"] for r in reps], ["no_ejecutable", "ejecutada", "ejecutada"])
        self.assertEqual([r.get("modifico") for r in reps], [True, False, True])
        self.assertEqual(reps[1]["discrepancia_sintetica"], "persiste")
        segundo, tercero = (fake.requests[i]["messages"][1]["content"] for i in (2, 3))
        self.assertIn("se cortó al llegar al límite de 2048 tokens", segundo)
        self.assertIn("RESULTADO DE TU INTENTO 2 EN EL CASO DEL REVISOR", tercero)
        self.assertIn("REPETICIÓN: tu intento 2 es el MISMO programa que la versión inicial", tercero)
        self.assertEqual(inf["consumo"]["por_rol"]["programador"]["cortadas"], 1)
        self.assertEqual(inf["verificacion"]["intento"], 3)

    def test_eliminating_the_discrepancy_is_not_enough_the_independent_battery_decides(self):
        inf, fake, _ = self.correr([revision(CASO_DE_ANULACION), bloque(CORRIGE_A_MEDIAS)])
        self.assertEqual(len(fake.requests), 2, "una sola verificación; su resultado nunca vuelve a un modelo")
        self.assertEqual(inf["reparaciones"][0]["discrepancia_sintetica"], "eliminada")
        self.assertEqual((inf["verificacion"]["ok"], inf["verificacion"]["aprobados"]), (False, 7))
        self.assertFalse(inf["criterios"]["4_discrepancia_eliminada_y_verificacion_independiente"])
        self.assertFalse(inf["exito"])

    def test_limits_of_repairs_and_inference_time(self):
        inf, fake, _ = self.correr([revision(CASO_DE_ANULACION), bloque(A), bloque(A)], max_reparaciones=2)
        self.assertEqual(len(fake.requests), 3)
        self.assertIsNone(inf["verificacion"])
        self.assertEqual(inf["verificacion_informativa"]["aprobados"], 8)
        self.assertFalse(inf["criterios"]["3_programador_modifico_la_solucion"])
        inf, fake, _ = self.correr([revision(CASO_DE_ANULACION)], max_segundos=0)
        self.assertEqual(fake.requests, [])
        self.assertIn("límite de tiempo de inferencia", inf["detenido"])

    def test_resumen_ventas_with_a_controlled_defect(self):
        caso = CASOS_POR_DEFECTO[("resumen_ventas", "hasta_excluido")]
        inf, _, _ = self.correr([revision(caso), bloque(REFERENCIA)], mision="resumen_ventas", defecto="hasta_excluido")
        self.assertTrue(inf["exito"], inf.get("detenido"))
        self.assertEqual(inf["verificacion"]["aprobados"], 7)


class AuditoriaTests(unittest.TestCase):
    def test_the_leak_audit_detects_the_defect_and_hidden_data(self):
        spec = mision_conciliar_movimientos()["spec"]
        oculto = next(c for c in spec["cases"] if not c.get("visible", True) and c.get("categoria"))
        info = {"id": "anulaciones_no_registradas", "explicacion": DEFECTO["explicacion"]}
        limpio = RE.auditar([[{"role": "user", "content": "contrato y código"}]], spec, info)
        self.assertTrue(limpio["sin_fugas"])
        for texto, esperado in (("anulaciones_no_registradas", "nombre del defecto"),
                                (oculto["categoria"], "categoría del caso oculto"),
                                (json.dumps(spec["cases"][4]["expect"], ensure_ascii=False), "salida esperada"),
                                (json.dumps(spec["cases"][4]["args"], ensure_ascii=False, separators=(",", ":")),
                                 "entrada del caso oculto 5")):
            a = RE.auditar([[{"role": "user", "content": f"…{texto}…"}]], spec, info)
            self.assertFalse(a["sin_fugas"])
            self.assertTrue(any(esperado in h for h in a["hallazgos"]), (esperado, a["hallazgos"]))


@unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
class CliTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="cia-exp7-cli-"))
        self.lab = self.dir / "oficina-revision-7"
        self.fake = FakeOllama([revision(CASO_DE_ANULACION), bloque(REFERENCIA_CONCILIAR)], models=("gemma4:12b",))
        self.addCleanup(self.fake.close)
        reg = RG.Registro(self.lab / "oficina.db")
        reg.registrar("prueba", self.fake.url, ["gemma4:12b"], permitir_local=True)
        reg.cerrar()

    def correr(self, *argv, carpeta=None):
        out, err = StringIO(), StringIO()
        ex = lambda cfg: Executor(cfg, limiter=NullLimiter(), allow_unverified_limits=True)  # noqa: E731
        with redirect_stdout(out), redirect_stderr(err), mock.patch("constructor_ia.sandbox.executor.Executor", ex), \
                mock.patch.object(RM, "memoria_disponible_mb", lambda: 8000):
            rc = cli.main(["--config", str(self.dir / "no-existe.toml"), "oficina", "revisar-existente",
                           "--carpeta", str(carpeta or self.lab), "--trabajador", "prueba", *argv])
        return rc, out.getvalue(), err.getvalue()

    def test_preparation_without_inference_then_the_authorized_run(self):
        rc, out, _ = self.correr("--defecto", "anulaciones_no_registradas")
        self.assertEqual(rc, 0, out)
        self.assertIn("[OK] solución inicial con un defecto funcional verificable", out)
        self.assertIn("listo para autorizar el experimento 7", out)
        self.assertEqual(self.fake.requests, [], "la preparación no infiere")
        self.assertFalse((self.lab / "experimento-7").exists(), "ni escribe una corrida")
        rc, out, _ = self.correr("--defecto", "anulaciones_no_registradas", "--autorizo-inferencia-remota")
        self.assertEqual(rc, 0, out)
        self.assertIn("Experimento 7: ÉXITO", out)
        self.assertIn("Verificación independiente del intento 1: 9/9 APROBADA", out)
        self.assertIn("auditoría de fugas: sin fugas", out)
        self.assertEqual(len(self.fake.requests), 2)
        self.assertEqual(len(list((self.lab / "experimento-7").glob("corrida-*/informe.json"))), 1)

    def test_an_existing_file_and_the_refusals(self):
        archivo = self.dir / "existente.py"
        archivo.write_text(A)
        rc, out, _ = self.correr("--codigo", str(archivo))
        self.assertEqual(rc, 0, out)
        rc, out, _ = self.correr("--autorizo-inferencia-remota")
        self.assertEqual(rc, 3, "la referencia sin defecto no es un experimento válido")
        self.assertIn("no tiene un defecto funcional verificable", out)
        anterior = self.dir / "oficina-programacion-6b"
        (anterior / "taller").mkdir(parents=True)
        reg = RG.Registro(anterior / "oficina.db")
        reg.registrar("prueba", self.fake.url, ["gemma4:12b"], permitir_local=True)
        reg.cerrar()
        rc, out, _ = self.correr("--defecto", "anulaciones_no_registradas", "--autorizo-inferencia-remota",
                                 carpeta=anterior)
        self.assertEqual(rc, 3)
        self.assertIn("[FALLA] laboratorio aislado", out)
        self.assertEqual(sorted(p.name for p in anterior.iterdir()), ["oficina.db", "taller"], "no se toca nada")
        self.assertEqual(self.fake.requests, [])


if __name__ == "__main__":
    unittest.main()
