"""Orquestador de OH-DUO-01 con el ejecutor simulado (tests/falso_ejecutor). Sin Ollama ni bwrap.

Usa el supervisor real de oh-supervisor (sin modificarlo): $OH_SUPERVISOR, ../oh-supervisor o ~/oh-supervisor.
"""
import builtins
import csv
import http.server
import io
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
RAIZ = AQUI.parent
sys.path.insert(0, str(RAIZ))
import duo  # noqa: E402

FALSO = AQUI / "falso_ejecutor"
OH_SUPERVISOR = next(p for p in (Path(os.environ.get("OH_SUPERVISOR", "/nonexistent")),
                                 RAIZ.parent / "oh-supervisor", Path.home() / "oh-supervisor")
                     if (p / "supervisor.py").is_file())
Q, G = duo.MODELOS["qwen"]["modelo"], duo.MODELOS["gemma"]["modelo"]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="oh-duo-"))
        self.oh_smoke = self.tmp / "oh-smoke"
        (self.oh_smoke / "venv").mkdir(parents=True)
        (self.oh_smoke / "relay.py").write_text("# relay\n")
        self.banco = self.tmp / "banco"
        shutil.copytree(RAIZ / "banco", self.banco)
        self.estado = self.tmp / "estado"
        self.estado.mkdir()
        self.corridas = self.tmp / "corridas"
        os.chmod(FALSO / "correr.sh", 0o755)

    def tearDown(self):
        for p in self.tmp.rglob("*"):   # los congelados quedan en solo lectura
            if not p.is_symlink():
                os.chmod(p, 0o755 if p.is_dir() else 0o644)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self, plan, **extra):
        return {"DUO_PLAN": json.dumps(plan), "DUO_ESTADO": str(self.estado), "DUO_BANCO_PRUEBA": str(self.banco),
                "OLLAMA_URL": "http://127.0.0.1:9", **extra}

    def ejecutar(self, plan, problemas=None, modelos=("qwen", "gemma"), semilla=11, orden="alterno", **kw):
        with mock.patch.dict(os.environ, self.env(plan)):
            return duo.ejecutar(self.corridas, "c1", self.oh_smoke, OH_SUPERVISOR, problemas, list(modelos), semilla,
                                orden, banco=self.banco, ejecutor=FALSO, roles=RAIZ / "roles", **kw)

    def verificar(self):
        return duo.verificar(self.corridas, "c1", banco=self.banco, bwrap=None)

    def llamadas(self):
        p = self.estado / "llamadas.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


PLAN = {
    "P1": {Q: "correcta", G: "correcta"},          # ambos
    "P2": {Q: "correcta", G: "incorrecta"},        # solo Qwen
    "P3": {Q: "stuck_mal", G: "correcta"},         # solo Gemma
    "P4": {Q: "sdk_error_bien", G: "texto"},       # Qwen potencialmente correcta; Gemma incompleta
    "P5": {Q: "sdk_error", G: "stuck_bien"},       # error de infraestructura; Gemma potencialmente correcta
    "P6": {Q: "control", G: "corte"},              # error de infraestructura; incompleta
}


class Completo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = Base("run")
        cls.b.setUp()
        cls.hechas = cls.b.ejecutar(PLAN)
        cls.d = cls.b.verificar()
        cls.c = cls.b.corridas / "c1"
        cls.ll = cls.b.llamadas()

    @classmethod
    def tearDownClass(cls):
        cls.b.tearDown()

    def test_doce_ejecuciones_secuenciales_en_orden_alterno(self):
        orden = [(l["problema"], l["modelo"]) for l in self.ll]
        self.assertEqual(orden[:4], [("P1", Q), ("P1", G), ("P2", G), ("P2", Q)])
        self.assertEqual(len(orden), 12)
        self.assertEqual(len(set(orden)), 12)

    def test_categorias(self):
        m = self.d["matriz"]
        self.assertEqual(m["P1"], {"qwen": "correcta", "gemma": "correcta"})
        self.assertEqual(m["P2"], {"qwen": "correcta", "gemma": "incorrecta"})
        self.assertEqual(m["P3"], {"qwen": "incompleta", "gemma": "correcta"})
        self.assertEqual(m["P4"], {"qwen": "potencialmente_correcta_sin_finalizar", "gemma": "incompleta"})
        self.assertEqual(m["P5"], {"qwen": "error_infraestructura", "gemma": "potencialmente_correcta_sin_finalizar"})
        self.assertEqual(m["P6"], {"qwen": "error_infraestructura", "gemma": "incompleta"})

    def test_metricas(self):
        cmp_ = self.d["comparacion"]
        self.assertEqual(cmp_["resueltos_por_ambos"], ["P1"])
        self.assertEqual(cmp_["solo_qwen"], ["P2"])
        self.assertEqual(cmp_["solo_gemma"], ["P3"])
        self.assertEqual(cmp_["no_resueltos"], ["P4", "P5", "P6"])
        cc = cmp_["cobertura_conjunta_teorica"]
        self.assertEqual((cc["problemas"], cc["fraccion"]), (["P1", "P2", "P3"], 0.5))
        self.assertEqual(cc["incluyendo_potencialmente_correctas"], round(5 / 6, 3))
        self.assertIn("Oráculo retrospectivo", cc["nota"])
        pm = self.d["por_modelo"]
        self.assertEqual((pm["qwen"]["correcta"], pm["qwen"]["tasa_resolucion"]), (2, round(2 / 6, 3)))
        self.assertEqual(pm["gemma"]["correcta"], 2)
        self.assertEqual(pm["qwen"]["llamadas_llm"], 4 * 5)        # el control previo no llega a inferir
        self.assertEqual(pm["gemma"]["tokens_prompt"], 9000 * 6)

    def test_motivos_distinguidos(self):
        mot = {(f["problema"], f["programador"]): f["motivo"] for f in self.d["ejecuciones"]}
        self.assertEqual(mot[("P5", "qwen")][:9], "error_sdk")
        self.assertEqual(mot[("P6", "qwen")], "control_previo")
        self.assertEqual(mot[("P6", "gemma")], "corte_externo")
        self.assertEqual(mot[("P3", "qwen")], "stuck")
        self.assertEqual(mot[("P4", "gemma")], "texto_sin_herramienta")

    def test_codigo_conservado_aun_con_error_del_sdk(self):
        e = self.c / "ejecuciones" / "P4__qwen"
        final = (e / "final" / "rle.py").read_text()
        self.assertEqual(final, (self.b.banco / "P4" / "referencia" / "rle.py").read_text())
        man = json.loads((e / "manifiesto.json").read_text())
        self.assertIn("rle.py", man["archivos"])
        self.assertTrue((e / "logs" / "estado__eventos.json").exists())

    def test_misma_entrada_para_los_dos_y_rol_distinto(self):
        por = {}
        for l in self.ll:
            por.setdefault(l["problema"], {})[l["modelo"]] = l
        for p, d in por.items():
            with self.subTest(problema=p):
                self.assertEqual(d[Q]["tarea"], d[G]["tarea"])
                self.assertEqual(d[Q]["mision"], d[G]["mision"])
                self.assertEqual(d[Q]["enfoque"], (RAIZ / "roles" / "qwen_minimo.txt").read_text())
                self.assertEqual(d[G]["enfoque"], (RAIZ / "roles" / "gemma_invariantes.txt").read_text())

    def test_la_instancia_no_tiene_ocultas_ni_referencia(self):
        for l in self.ll:
            self.assertNotIn("ocultas", l["instancia"])
            self.assertNotIn("referencia", l["instancia"])
            self.assertEqual(set(l["mision"]), {"id", "modulo"})
            self.assertEqual(set(l["tarea"]), {f"{l['mision']['modulo']}.py", f"test_{l['mision']['modulo']}.py"})

    def test_salidas(self):
        filas = list(csv.DictReader(io.StringIO((self.c / "matriz.csv").read_text())))
        self.assertEqual(len(filas), 12)
        self.assertEqual({f["categoria"] for f in filas} - set(duo.CATEGORIAS), set())
        md = (self.c / "informe.md").read_text()
        for texto in ("## Matriz", "Cobertura conjunta teórica", "Oráculo retrospectivo", "no estiman la capacidad"):
            self.assertIn(texto, md)
        self.assertEqual(json.loads((self.c / "resultados.json").read_text())["comparacion"], self.d["comparacion"])
        self.assertEqual(self.d["auditoria_fugas"], [])

    def test_ocultas_solo_en_la_verificacion(self):
        for f in (self.c / "ejecuciones").rglob("*"):
            if f.is_file():
                self.assertNotIn("Ocultas(unittest.TestCase)", f.read_bytes().decode(errors="replace"))
        self.assertTrue(any((self.c / "verificacion").glob("*.ocultas.txt")))

    def test_despues_de_verificar_no_se_ejecuta_mas(self):
        with self.assertRaises(SystemExit):
            self.b.ejecutar(PLAN, problemas=["P1"])


class Casos(Base):
    def test_verificar_exige_todo_congelado(self):
        self.ejecutar({"P1": {Q: "correcta"}}, problemas=["P1"], modelos=["qwen"])
        (self.corridas / "c1" / "ejecuciones" / "P1__qwen" / "ejecucion.json").unlink()
        with self.assertRaises(SystemExit):
            self.verificar()
        self.assertFalse((self.corridas / "c1" / "congelado.json").exists())

    def test_congelado_alterado_se_detecta(self):
        self.ejecutar({"P1": {Q: "correcta"}}, problemas=["P1"], modelos=["qwen"])
        f = self.corridas / "c1" / "ejecuciones" / "P1__qwen" / "final" / "intervalos.py"
        os.chmod(f, 0o644)
        f.write_text("# cambiado después de congelar\n")
        with self.assertRaises(SystemExit) as e:
            self.verificar()
        self.assertIn("manifiesto", str(e.exception))

    def test_ejecutar_no_lee_ocultas_ni_referencia(self):
        leidos = []
        abrir, abrir_os = builtins.open, os.open

        def registrar(ruta, *a, **kw):
            leidos.append(str(ruta))
            return abrir(ruta, *a, **kw)

        def registrar_os(ruta, *a, **kw):
            leidos.append(str(ruta))
            return abrir_os(ruta, *a, **kw)
        with mock.patch("builtins.open", registrar), mock.patch("io.open", registrar), mock.patch("os.open", registrar_os):
            self.ejecutar({"P2": {Q: "incorrecta", G: "incorrecta"}}, problemas=["P2"])
        propios = [r for r in leidos if str(self.banco) in r]
        self.assertTrue(propios)
        self.assertFalse([r for r in propios if "/ocultas/" in r or "/referencia/" in r])

    def test_enlaces_no_se_siguen(self):
        self.ejecutar({"P1": {Q: "enlace"}}, problemas=["P1"], modelos=["qwen"])
        man = json.loads((self.corridas / "c1" / "ejecuciones" / "P1__qwen" / "manifiesto.json").read_text())
        self.assertEqual(man["omitidos"], ["secreto.txt"])
        self.assertFalse((self.corridas / "c1" / "ejecuciones" / "P1__qwen" / "final" / "secreto.txt").exists())

    def test_auditoria_detecta_fugas(self):
        self.ejecutar({"P3": {Q: "fuga"}}, problemas=["P3"], modelos=["qwen"])
        d = self.verificar()
        self.assertEqual(len(d["auditoria_fugas"]), 1)
        self.assertEqual(d["auditoria_fugas"][0]["archivo"], "final/notas.txt")

    def test_timeout_mata_el_grupo(self):
        self.ejecutar({"P1": {Q: "colgar"}}, problemas=["P1"], modelos=["qwen"], timeout=2)
        reg = json.loads((self.corridas / "c1" / "ejecuciones" / "P1__qwen" / "ejecucion.json").read_text())
        self.assertEqual((reg["estado_ejecucion"], reg["motivo"]), ("error_infraestructura", "timeout_del_orquestador"))
        hijo = int((self.estado / "hijo-P1.pid").read_text())
        time.sleep(0.2)
        stat = Path(f"/proc/{hijo}/stat")
        self.assertTrue(not stat.exists() or stat.read_text().split()[2] == "Z")

    def test_reanudable_sin_repetir(self):
        self.ejecutar({}, problemas=["P1"], modelos=["qwen"])
        self.ejecutar({}, problemas=["P1", "P2"], modelos=["qwen"])
        self.assertEqual([l["problema"] for l in self.llamadas()], ["P1", "P2"])

    def test_misma_configuracion_en_toda_la_corrida(self):
        self.ejecutar({}, problemas=["P1"], modelos=["qwen"])
        with self.assertRaises(SystemExit):
            self.ejecutar({}, problemas=["P2"], modelos=["qwen"], semilla=99)

    def test_un_proceso_a_la_vez(self):
        self.corridas.mkdir()
        import fcntl
        f = open(self.corridas / ".candado", "w")
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            with self.assertRaises(SystemExit):
                self.ejecutar({}, problemas=["P1"], modelos=["qwen"])
        finally:
            f.close()
        self.assertEqual(self.llamadas(), [])

    def test_cambio_de_modelo(self):
        class PS(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                b = json.dumps({"models": [{"name": Q}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), PS)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with mock.patch.dict(os.environ, self.env({}, OLLAMA_URL=f"http://127.0.0.1:{srv.server_port}")):
                duo.ejecutar(self.corridas, "c1", self.oh_smoke, OH_SUPERVISOR, ["P1"], ["qwen", "gemma"], 11,
                             "alterno", banco=self.banco, ejecutor=FALSO, roles=RAIZ / "roles")
        finally:
            srv.shutdown()
            srv.server_close()
        regs = {r: json.loads((self.corridas / "c1" / "ejecuciones" / f"P1__{r}" / "ejecucion.json").read_text())
                for r in ("qwen", "gemma")}
        self.assertEqual((regs["qwen"]["cambio_de_modelo"], regs["gemma"]["cambio_de_modelo"]), (False, True))

    def test_problema_o_modelo_desconocido(self):
        for kw in ({"problemas": ["P9"]}, {"modelos": ["llama"]}):
            with self.subTest(**kw), self.assertRaises(SystemExit):
                self.ejecutar({}, **kw)
        self.assertEqual(self.llamadas(), [])


if __name__ == "__main__":
    unittest.main()
