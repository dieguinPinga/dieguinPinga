"""Pruebas del supervisor con un correr.sh falso (tests/falso_correr.py). No llaman a Ollama ni usan bwrap.

    python3 -m unittest discover -s oh-supervisor/tests -v
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent))
import supervisor as S  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="oh-sup-"))
        self.oh = self.tmp / "oh-smoke"
        for d in ("tarea", "ocultas", "control"):
            (self.oh / d).mkdir(parents=True)
        (self.oh / "tarea/duracion.py").write_text('_FACTOR = {"h": 3600, "m": 100, "s": 1}\n')
        (self.oh / "tarea/test_duracion.py").write_text("# visibles\n")
        (self.oh / "ocultas/test_ocultas.py").write_text("SECRETO_OCULTO = 1\n")
        (self.oh / "control/duracion.py").write_text("# referencia\n")
        for f in ("smoke.py", "relay.py", "requisitos.lock", "requisitos-tools.lock"):
            (self.oh / f).write_text(f"# {f}\n")
        shutil.copy(AQUI / "falso_correr.py", self.oh / "correr.sh")
        os.chmod(self.oh / "correr.sh", 0o755)
        (self.oh / "resultados").mkdir()
        self.estado = self.tmp / "estado"
        self.estado.mkdir()
        self.registros = self.tmp / "registros" / "corrida"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def correr(self, plan, semillas=(11, 12, 13), **kw):
        env = {"FALSO_PLAN": json.dumps({str(k): v for k, v in plan.items()}), "FALSO_ESTADO": str(self.estado)}
        with mock.patch.dict(os.environ, env):
            return S.supervisar(self.oh, list(semillas), self.registros, **kw)

    def llamadas(self):
        p = self.estado / "llamadas.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def arbol(self):
        """Huella de todo oh-smoke salvo resultados/ (lo único que correr.sh puede escribir)."""
        h = hashlib.sha256()
        for p in sorted(self.oh.rglob("*")):
            if "resultados" in p.relative_to(self.oh).parts or not p.is_file():
                continue
            h.update(str(p.relative_to(self.oh)).encode() + p.read_bytes())
        return h.hexdigest()


class Detencion(Base):
    def test_exito_en_el_primer_intento_no_sigue(self):
        r = self.correr({11: "exito"})
        self.assertTrue(r["exito"])
        self.assertEqual((r["intento_exitoso"], r["motivo_detencion"], r["intentos_realizados"]), (1, "exito", 1))
        self.assertEqual(len(self.llamadas()), 1)

    def test_dos_stuck_y_exito_en_el_tercero(self):
        r = self.correr({11: "stuck", 12: "stuck", 13: "exito"})
        self.assertEqual([i["motivo"] for i in r["intentos"]], ["stuck", "stuck", "exito"])
        self.assertEqual(r["intento_exitoso"], 3)
        self.assertEqual(r["intentos"][0]["criterios_fallidos"],
                         ["3_edito_codigo_y_no_las_pruebas", "4_visibles_pasan_y_termino_con_finish",
                          "5_ocultas_pasan"])

    def test_intentos_agotados(self):
        r = self.correr({11: "texto", 12: "ocultas", 13: "corte"})
        self.assertFalse(r["exito"])
        self.assertEqual(r["motivo_detencion"], "intentos_agotados")
        self.assertEqual([i["motivo"] for i in r["intentos"]],
                         ["texto_sin_herramienta", "criterios (finished)", "corte_externo"])
        self.assertEqual(r["intentos"][1]["criterios_fallidos"], ["5_ocultas_pasan"])

    def test_cinco_de_seis_criterios_no_es_exito(self):
        r = self.correr({11: "ocultas", 12: "exito"})
        self.assertFalse(r["intentos"][0]["exito"])
        self.assertEqual(r["intento_exitoso"], 2)

    def test_control_previo_fallido_cuenta_como_intento_y_sigue(self):
        r = self.correr({11: "control", 12: "exito"})
        self.assertEqual(r["intentos"][0]["motivo"], "control_previo")
        self.assertEqual(r["intentos"][0]["salida_correr"], 1)
        self.assertTrue(r["intentos"][0]["carpeta"])
        self.assertEqual(r["intento_exitoso"], 2)

    def test_menos_semillas_menos_intentos(self):
        r = self.correr({11: "stuck"}, semillas=(11,))
        self.assertEqual((r["intentos_realizados"], r["motivo_detencion"]), (1, "intentos_agotados"))


class Independencia(Base):
    def test_semillas_distintas_y_solo_la_semilla_como_argumento(self):
        self.correr({11: "stuck", 12: "stuck", 13: "stuck"})
        ll = self.llamadas()
        self.assertEqual([l["semilla"] for l in ll], ["11", "12", "13"])
        for l in ll:
            self.assertEqual(l["argv"], [str(self.oh / "correr.sh"), l["semilla"]])

    def test_semillas_repetidas_rechazadas(self):
        with self.assertRaises(SystemExit):
            self.correr({}, semillas=(11, 11, 12))
        self.assertEqual(self.llamadas(), [])

    def test_cada_intento_parte_de_una_copia_limpia_y_carpeta_nueva(self):
        r = self.correr({11: "stuck", 12: "stuck", 13: "stuck"})
        self.assertTrue(all(l["copia_limpia"] for l in self.llamadas()))
        carpetas = [i["carpeta"] for i in r["intentos"]]
        self.assertEqual(len(set(carpetas)), 3)
        self.assertTrue(all(i["partio_del_original"] for i in r["intentos"]))
        for c in carpetas:   # los restos de cada intento quedan en su carpeta, no en tarea/
            self.assertTrue((Path(c) / "work/basura.txt").exists())
        self.assertFalse((self.oh / "tarea/basura.txt").exists())

    def test_no_corre_en_paralelo(self):
        self.correr({11: "stuck", 12: "stuck", 13: "exito"})
        ll = self.llamadas()
        for a, b in zip(ll, ll[1:]):
            self.assertLessEqual(a["fin"], b["inicio"])

    def test_supervisor_no_escribe_en_oh_smoke(self):
        antes = self.arbol()
        self.correr({11: "stuck", 12: "exito"})
        self.assertEqual(self.arbol(), antes)
        self.assertFalse(any(p.name in ("resumen.json", ".candado") for p in self.oh.rglob("*")))

    def test_las_ocultas_no_viajan_al_agente(self):
        """El supervisor solo pasa la semilla; el contenido de ocultas/ no aparece en argv ni en el entorno."""
        with mock.patch.object(S.subprocess, "Popen", wraps=S.subprocess.Popen) as popen:
            self.correr({11: "exito"})
        args, kw = popen.call_args
        self.assertNotIn("SECRETO_OCULTO", json.dumps([args, kw.get("env")], default=str))
        self.assertIsNone(kw.get("env"))   # hereda el entorno tal cual: no agrega nada


class Integridad(Base):
    def test_originales_alterados_por_correr_detiene_todo(self):
        r = self.correr({11: "toca_tarea", 12: "exito"})
        self.assertEqual(r["motivo_detencion"], "integridad")
        self.assertFalse(r["exito"])
        self.assertEqual((r["intentos"][0]["exito"], r["intentos"][0]["motivo"]), (False, "integridad"))
        self.assertEqual(len(self.llamadas()), 1)

    def test_correr_informa_alterados_detiene_todo(self):
        r = self.correr({11: "alterados", 12: "exito"})
        self.assertEqual((r["intentos"][0]["motivo"], r["motivo_detencion"]), ("integridad", "integridad"))
        self.assertIn("originales alterados", r["detalle_detencion"])
        self.assertEqual(len(self.llamadas()), 1)

    def test_carpeta_reusada_detiene_todo(self):
        r = self.correr({11: "stuck", 12: "reusar", 13: "exito"})
        self.assertEqual(r["motivo_detencion"], "integridad")
        self.assertIn("no es nueva", r["detalle_detencion"])

    def test_intento_que_no_partio_del_original(self):
        r = self.correr({11: "sin_copia", 12: "exito"})
        self.assertEqual(r["motivo_detencion"], "integridad")
        self.assertFalse(r["intentos"][0]["partio_del_original"])
        self.assertFalse(r["intentos"][0]["exito"])

    def test_originales_cambiados_entre_intentos(self):
        original = S.ejecutar

        def y_ademas_alterar(*a, **kw):
            salida = original(*a, **kw)
            (self.oh / "ocultas/test_ocultas.py").write_text("cambiado\n")
            return salida
        with mock.patch.object(S, "ejecutar", y_ademas_alterar):
            r = self.correr({11: "stuck", 12: "exito"})
        self.assertEqual(r["motivo_detencion"], "integridad")
        self.assertEqual(len(self.llamadas()), 1)


class Concurrencia(Base):
    def test_rechaza_si_hay_un_correr_lanzado_a_mano(self):
        env = {**os.environ, "FALSO_PLAN": json.dumps({"99": "colgar"}), "FALSO_ESTADO": str(self.estado)}
        manual = subprocess.Popen([str(self.oh / "correr.sh"), "99"], env=env, start_new_session=True)
        try:
            time.sleep(0.5)
            r = self.correr({11: "exito"})
        finally:
            os.killpg(manual.pid, 9)
            manual.wait()
        self.assertEqual(r["motivo_detencion"], "integridad")
        self.assertIn("otro correr.sh", r["detalle_detencion"])
        self.assertEqual(r["intentos"], [])

    def test_un_solo_supervisor_a_la_vez(self):
        errores = []

        def segundo():
            time.sleep(0.5)
            try:
                S.supervisar(self.oh, [21], self.registros.parent / "otra")
            except SystemExit as e:
                errores.append(str(e))
        t = threading.Thread(target=segundo)
        t.start()
        self.correr({11: "colgar"}, semillas=(11,), timeout=2)
        t.join()
        self.assertEqual(errores, ["ya hay otro supervisor en ejecución"])

    def test_timeout_mata_el_grupo_entero(self):
        r = self.correr({11: "colgar", 12: "exito"}, timeout=2)
        self.assertEqual(r["intentos"][0]["motivo"], "timeout_supervisor")
        self.assertIsNone(r["intentos"][0]["salida_correr"])
        hijo = int((self.estado / "hijo-11.pid").read_text())
        time.sleep(0.2)
        estado = Path(f"/proc/{hijo}/stat")
        self.assertTrue(not estado.exists() or estado.read_text().split()[2] == "Z", "quedó vivo un hijo")
        self.assertEqual(r["intento_exitoso"], 2)


class Registro(Base):
    def test_resumen_json_y_registros_por_intento(self):
        r = self.correr({11: "stuck", 12: "exito"})
        en_disco = json.loads((self.registros / "resumen.json").read_text())
        self.assertEqual(en_disco, r)
        for clave in ("inicio", "fin", "segundos_total", "semillas", "huellas_originales", "motivo_detencion"):
            self.assertIn(clave, en_disco)
        for n, i in enumerate(en_disco["intentos"], 1):
            for clave in ("semilla", "inicio", "fin", "segundos", "motivo", "criterios", "carpeta", "llamadas_llm"):
                self.assertIn(clave, i)
            self.assertTrue((self.registros / f"intento-{n}.log").read_text().strip())
            self.assertTrue((self.registros / f"intento-{n}.resultado_final.json").exists())
            self.assertTrue(Path(i["carpeta"]).is_dir())   # la carpeta de correr.sh se conserva

    def test_cli_limita_a_tres_intentos(self):
        with self.assertRaises(SystemExit):
            S.main(["--oh-smoke", str(self.oh), "--semillas", "1,2,3,4", "--registros", str(self.tmp / "r")])


if __name__ == "__main__":
    unittest.main()
