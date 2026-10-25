"""Laboratorio con el ejecutor simulado (tests/falso_ejecutor): varias misiones sin intervención, sin Ollama.

Usa el supervisor real de oh-supervisor (sin modificarlo). Ruta: $OH_SUPERVISOR, ../oh-supervisor
(repositorio) o ~/oh-supervisor.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent))
import lab  # noqa: E402

FALSO = AQUI / "falso_ejecutor"
BANCO = AQUI.parent / "banco"
OH_SUPERVISOR = next(p for p in (Path(os.environ.get("OH_SUPERVISOR", "/nonexistent")),
                                 AQUI.parent.parent / "oh-supervisor", Path.home() / "oh-supervisor")
                     if (p / "supervisor.py").is_file())


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="oh-lab-"))
        self.oh_smoke = self.tmp / "oh-smoke"
        (self.oh_smoke / "venv").mkdir(parents=True)
        for f in ("relay.py", "requisitos.lock", "requisitos-tools.lock"):
            (self.oh_smoke / f).write_text(f"# {f}\n")
        self.estado = self.tmp / "estado"
        self.estado.mkdir()
        self.banco = self.tmp / "banco"
        shutil.copytree(BANCO, self.banco)
        os.chmod(FALSO / "correr.sh", 0o755)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def lab(self, plan, ids=None, semillas=(11, 12, 13)):
        env = {"LAB_PLAN": json.dumps(plan), "LAB_ESTADO": str(self.estado)}
        with mock.patch.dict(os.environ, env):
            return lab.correr_laboratorio(self.oh_smoke, OH_SUPERVISOR, list(semillas), ids,
                                          self.tmp / "registros", self.tmp / "instancias",
                                          banco=self.banco, ejecutor=FALSO)

    def llamadas(self):
        p = self.estado / "llamadas.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


PLAN_MIXTO = {
    "C01": {"11": "exito"},
    "C02": {"11": "stuck", "12": "exito"},
    "C03": {"11": "stuck", "12": "stuck", "13": "exito"},
    "C04": {},                                   # tres stuck
    "S01": {"11": "exito"}, "S02": {"11": "ocultas", "12": "exito"}, "S03": {"11": "exito"},
    "S04": {"11": "ocultas", "12": "ocultas", "13": "ocultas"},
    "K01": {"11": "exito"}, "K02": {},
    "O01": {"11": "exito"}, "O02": {"11": "stuck", "12": "exito"},
}


class DoceMisiones(Base):
    @classmethod
    def setUpClass(cls):
        cls._t = Base("run")
        cls._t.setUp()
        cls.inf = cls._t.lab(PLAN_MIXTO)
        cls.ll = cls._t.llamadas()

    @classmethod
    def tearDownClass(cls):
        cls._t.tearDown()

    def test_recorre_las_doce_sin_intervencion(self):
        self.assertEqual(self.inf["motivo_detencion"], "completo")
        self.assertEqual(self.inf["totales"]["ejecutadas"], 12)
        self.assertEqual(self.inf["no_ejecutadas"], [])
        self.assertEqual([m["id"] for m in self.inf["misiones"]],
                         ["C01", "C02", "C03", "C04", "K01", "K02", "O01", "O02", "S01", "S02", "S03", "S04"])

    def test_resueltas_y_no_resueltas(self):
        self.assertEqual(self.inf["no_resueltas"], ["C04", "K02", "S04"])
        self.assertEqual(len(self.inf["resueltas"]), 9)
        self.assertEqual(self.inf["totales"]["porcentaje_exito"], 75.0)

    def test_se_detiene_al_resolver_y_usa_hasta_tres_intentos(self):
        esperados = {"C01": 1, "C02": 2, "C03": 3, "C04": 3, "K01": 1, "K02": 3, "O01": 1, "O02": 2,
                     "S01": 1, "S02": 2, "S03": 1, "S04": 3}
        self.assertEqual(self.inf["intentos_por_mision"], esperados)
        self.assertEqual(self.inf["totales"]["intentos"], sum(esperados.values()))
        self.assertEqual(len(self.ll), sum(esperados.values()))

    def test_porcentaje_por_categoria(self):
        pc = self.inf["por_categoria"]
        self.assertEqual(pc["cálculo"], {"misiones": 4, "resueltas": 3, "porcentaje_exito": 75.0})
        self.assertEqual(pc["sobrescritura"], {"misiones": 4, "resueltas": 3, "porcentaje_exito": 75.0})
        self.assertEqual(pc["condición"], {"misiones": 2, "resueltas": 1, "porcentaje_exito": 50.0})
        self.assertEqual(pc["omisión"], {"misiones": 2, "resueltas": 2, "porcentaje_exito": 100.0})

    def test_motivos_de_fallo(self):
        # stuck: C02 1, C03 2, C04 3, K02 3, O02 1 = 10; ocultas: S02 1, S04 3 = 4
        self.assertEqual(self.inf["motivos_de_fallo"], {"stuck": 10, "criterios (finished)": 4})

    def test_llamadas_y_tokens_sumados(self):
        t = self.inf["totales"]
        self.assertEqual(t["llamadas_llm"], 5 * t["intentos"])
        self.assertEqual((t["tokens_prompt"], t["tokens_generados"]), (8000 * t["intentos"], 300 * t["intentos"]))
        c03 = next(m for m in self.inf["misiones"] if m["id"] == "C03")
        self.assertEqual((c03["llamadas_llm"], c03["intento_exitoso"]), (15, 3))
        self.assertEqual([i["semilla"] for i in c03["intentos"]], [11, 12, 13])

    def test_cada_intento_desde_el_original_y_sin_restos(self):
        self.assertTrue(all(l["copia_limpia"] for l in self.ll))
        for l in self.ll:
            mod = l["mision_json"]["modulo"]
            self.assertEqual(l["vio"], sorted([f"{mod}.py", f"test_{mod}.py"]))
        for m in self.inf["misiones"]:
            self.assertEqual(len({i["carpeta"] for i in m["intentos"]}), m["intentos_usados"])

    def test_el_agente_no_ve_ocultas_ni_referencia_ni_categoria(self):
        for l in self.ll:
            self.assertEqual(set(l["mision_json"]), {"id", "modulo"})
            self.assertNotIn("ocultas", l["vio"])
            self.assertNotIn("control", l["vio"])

    def test_secuencial(self):
        orden = sorted(self.ll, key=lambda l: l["inicio"])
        for a, b in zip(orden, orden[1:]):
            self.assertLessEqual(a["fin"], b["inicio"])

    def test_informe_en_disco_y_registros_por_mision(self):
        reg = Path(self.inf["misiones"][0]["registro"]).parent
        en_disco = json.loads((reg / "informe.json").read_text())
        self.assertEqual(en_disco["totales"], self.inf["totales"])
        for m in self.inf["misiones"]:
            r = json.loads((Path(m["registro"]) / "resumen.json").read_text())
            self.assertEqual(len(r["intentos"]), m["intentos_usados"])

    def test_banco_intacto(self):
        self.assertEqual(lab.huella_arbol(self._t.banco), self.inf["huellas"]["banco"])
        self.assertEqual(lab.huella_arbol(self._t.banco), lab.huella_arbol(BANCO))


class Casos(Base):
    def test_una_mision_concreta(self):
        inf = self.lab({"K02": {"11": "stuck", "12": "exito"}}, ids=["K02"])
        self.assertEqual((inf["totales"]["ejecutadas"], inf["resueltas"]), (1, ["K02"]))
        self.assertEqual({l["mision"] for l in self.llamadas()}, {"K02"})

    def test_mision_desconocida(self):
        with self.assertRaises(SystemExit):
            self.lab({}, ids=["Z99"])
        self.assertEqual(self.llamadas(), [])

    def test_integridad_detiene_el_laboratorio(self):
        inf = self.lab({"C01": {"11": "exito"}, "C02": {"11": "toca_instancia"}}, ids=["C01", "C02", "C03"])
        self.assertEqual(inf["motivo_detencion"], "integridad")
        self.assertEqual(inf["resueltas"], ["C01"])
        self.assertEqual(inf["no_resueltas"], ["C02"])
        self.assertEqual(inf["no_ejecutadas"], ["C03"])
        self.assertNotIn("C03", {l["mision"] for l in self.llamadas()})

    def test_banco_invalido_no_ejecuta_nada(self):
        shutil.rmtree(self.banco / "S03" / "control")
        with self.assertRaises(lab.BancoInvalido):
            self.lab({})
        self.assertEqual(self.llamadas(), [])

    def test_un_solo_laboratorio_a_la_vez(self):
        reg = self.tmp / "registros"
        reg.mkdir()
        bloqueo = open(reg / ".candado", "w")
        import fcntl
        fcntl.flock(bloqueo, fcntl.LOCK_EX)
        try:
            with self.assertRaises(SystemExit) as e:
                self.lab({}, ids=["C01"])
            self.assertIn("otro laboratorio", str(e.exception))
        finally:
            bloqueo.close()
        self.assertEqual(self.llamadas(), [])

    def test_instancia_sin_datos_del_defecto(self):
        self.lab({"C01": {"11": "exito"}}, ids=["C01"])
        inst = next((self.tmp / "instancias").iterdir()) / "C01"
        self.assertEqual(json.loads((inst / "mision.json").read_text()), {"id": "C01", "modulo": "promedio"})
        self.assertTrue((inst / "venv").is_symlink())
        self.assertEqual(sorted(p.name for p in (inst / "tarea").iterdir()), ["promedio.py", "test_promedio.py"])

    def test_cli_valida_semillas(self):
        for s in ("1,1", "1,2,3,4", ""):
            with self.subTest(semillas=s), self.assertRaises(SystemExit):
                lab.main(["--semillas", s, "--oh-smoke", str(self.oh_smoke)])


if __name__ == "__main__":
    unittest.main()
