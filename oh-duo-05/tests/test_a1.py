"""Experimento A1 sin inferencia: estado de Ollama (simulado) y comparación de trayectorias (SDK real en proceso).
   <venv>/bin/python -I -m unittest discover -s tests -p test_a1.py -v
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI))
sys.path.insert(0, str(AQUI.parent / "reproducibilidad"))
os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
import ollama_estado as O  # noqa: E402
import simulado as S  # noqa: E402

try:
    import litellm
    from pydantic import SecretStr
    from openhands.sdk import LLM, Agent, Conversation, Tool
    from openhands.tools.terminal import TerminalTool
    SDK = True
except ImportError:
    SDK = False

QWEN = "qwen2.5-coder:7b"
NADA = lambda s: None  # noqa: E731


def chat(url):
    cuerpo = json.dumps({"model": QWEN, "messages": [{"role": "user", "content": "x"}] * 3}).encode()
    urllib.request.urlopen(urllib.request.Request(url + "/api/chat", data=cuerpo), timeout=5).read()


class EstadoOllama(unittest.TestCase):
    """Contra el Ollama simulado: no es el Ollama de esta máquina ni usa su GPU, así que las pruebas declaran
    senales_locales=False (si no, medirían el Ollama real de la máquina, como pasó en la VivoBook)."""

    def setUp(self):
        self.srv = S.Servidor(lambda n, m: S.FIN, demora=0.0, estado_ollama=True)
        self.addCleanup(self.srv.cerrar)
        self.url = f"http://127.0.0.1:{self.srv.puerto}"

    def test_inactivo_sin_modelos(self):
        ok, motivos, fotos = O.inactivo(self.url, ventana=0, dormir=NADA, senales_locales=False)
        self.assertTrue(ok, motivos)
        self.assertEqual(fotos[0]["cargados"], [])
        self.assertEqual(fotos[0]["version"], "0.0-simulado")

    def test_otro_modelo_cargado_aborta_y_no_toca_nada(self):
        chat(self.url)                                   # qwen cargado y ocioso
        self.srv.otros = ["gemma4:12b"]
        r = O.preparar(self.url, ventana=0, dormir=NADA, senales_locales=False)
        self.assertEqual(r["resultado"], "abortado")
        self.assertIn("gemma4:12b", r["actividad"][0])
        self.assertEqual(self.srv.descargas, [])         # no se pidió ninguna descarga
        self.assertTrue(self.srv.cargado)

    def test_uso_durante_la_observacion_aborta(self):
        chat(self.url)
        fin = threading.Event()

        def otro_proceso():
            while not fin.is_set():
                chat(self.url)
                time.sleep(0.05)
        h = threading.Thread(target=otro_proceso, daemon=True)
        h.start()
        try:
            r = O.preparar(self.url, ventana=0.6, senales_locales=False)
        finally:
            fin.set()
            h.join()
        self.assertEqual(r["resultado"], "abortado")
        self.assertTrue(any("alguien lo está usando" in m or "conexiones" in m for m in r["actividad"]), r["actividad"])
        self.assertEqual(self.srv.descargas, [])

    def test_descarga_solo_qwen_sin_prompt_y_verifica_en_frio(self):
        chat(self.url)
        r = O.preparar(self.url, ventana=0, dormir=NADA, senales_locales=False)
        self.assertEqual(r["resultado"], "en_frio", r["actividad"])
        self.assertEqual(self.srv.descargas, [{"model": QWEN, "keep_alive": 0}])     # sin prompt: no genera
        self.assertEqual(r["inicial"]["cargados"], [])
        self.assertEqual(r["descarga"], {"enviado": True, "motivo": "descargado"})

    def test_sin_cargar_no_envia_nada(self):
        r = O.preparar(self.url, ventana=0, dormir=NADA, senales_locales=False)
        self.assertEqual(r["resultado"], "en_frio")
        self.assertEqual(r["descarga"]["enviado"], False)
        self.assertEqual(self.srv.descargas, [])

    def test_despues_detecta_contaminacion(self):
        chat(self.url)
        self.assertFalse(O.despues(self.url)["contaminada"])
        self.srv.otros = ["llama3:8b"]
        r = O.despues(self.url)
        self.assertTrue(r["contaminada"])
        self.assertEqual(r["otros_modelos_aparecieron"], ["llama3:8b"])


class SenalesLocales(unittest.TestCase):
    """Las protecciones que dependen de la máquina (ss, nvidia-smi, pgrep), con salidas simuladas: así se prueban
    también donde esas herramientas no existen."""

    def setUp(self):
        self.srv = S.Servidor(lambda n, m: S.FIN, demora=0.0, estado_ollama=True)
        self.addCleanup(self.srv.cerrar)
        self.url = f"http://127.0.0.1:{self.srv.puerto}"

    def salida(self, texto, codigo=0):
        return mock.Mock(stdout=texto, returncode=codigo)

    def test_por_omision_una_url_local_mira_conexiones_y_gpu(self):
        with mock.patch.object(O, "conexiones", return_value=0) as c, mock.patch.object(O, "gpu", return_value=[]) as g:
            ok, motivos, fotos = O.inactivo(self.url, ventana=0, dormir=NADA)       # sin senales_locales: lo de siempre
        self.assertTrue(fotos[0]["senales_locales"])
        self.assertTrue(c.called and g.called)
        self.assertTrue(ok, motivos)

    def test_conexion_abierta_aborta(self):
        with mock.patch.object(O, "conexiones", return_value=1), mock.patch.object(O, "gpu", return_value=[]):
            r = O.preparar(self.url, ventana=0, dormir=NADA)
        self.assertEqual(r["resultado"], "abortado")
        self.assertIn("conexiones abiertas con Ollama", r["actividad"][0])
        self.assertEqual(self.srv.descargas, [])

    def test_gpu_en_uso_aborta(self):
        uso = [{"uso_pct": "32", "memoria_usada_mib": "900"}]
        with mock.patch.object(O, "conexiones", return_value=0), mock.patch.object(O, "gpu", return_value=uso):
            r = O.preparar(self.url, ventana=0, dormir=NADA)
        self.assertEqual(r["resultado"], "abortado")
        self.assertIn("la GPU está en uso", r["actividad"][0])
        self.assertEqual(self.srv.descargas, [])

    def test_procesos_de_oh_duo_abortan_tambien_con_el_simulado(self):
        with mock.patch.object(O, "procesos_oh", return_value=["123 bash /x/correr.sh 11"]):
            r = O.preparar(self.url, ventana=0, dormir=NADA, senales_locales=False)
        self.assertEqual(r["resultado"], "abortado")
        self.assertIn("procesos de OH-DUO", r["actividad"][0])

    def test_solo_el_simulado_puede_omitir_las_senales_locales(self):
        real = S.Servidor(lambda n, m: S.FIN, demora=0.0, estado_ollama=True)
        self.addCleanup(real.cerrar)
        with mock.patch.object(O, "pedir", side_effect=lambda u, r, *a, **k: {"version": "0.12.6"} if r == "/api/version"
                               else {"models": []}):
            with self.assertRaises(ValueError):
                O.inactivo(f"http://127.0.0.1:{real.puerto}", ventana=0, dormir=NADA, senales_locales=False)
            with self.assertRaises(SystemExit):
                with mock.patch.object(sys, "exit", side_effect=SystemExit):
                    sys.exit(O.main(["preparar", "--url", f"http://127.0.0.1:{real.puerto}", "--simulado"]) or 0)
        self.assertEqual(real.descargas, [])

    def test_conexiones_cuenta_el_lado_servidor_de_los_puertos_de_ollama(self):
        ss = ("0 0 127.0.0.1:11434 127.0.0.1:51234\n"       # alguien conectado al Ollama de la máquina: cuenta
              "0 0 127.0.0.1:51234 127.0.0.1:11434\n"       # el mismo, del lado cliente: no se cuenta dos veces
              "0 0 100.92.153.59:11435 100.1.2.3:40000\n"   # cliente remoto en el puerto de la URL: cuenta
              "0 0 127.0.0.1:22 127.0.0.1:50000\n")
        with mock.patch.object(O.subprocess, "run", return_value=self.salida(ss)):
            self.assertEqual(O.conexiones("http://100.92.153.59:11435"), 2)
        with mock.patch.object(O.subprocess, "run", side_effect=OSError("sin ss")):
            self.assertIsNone(O.conexiones("http://100.92.153.59:11435"))

    def test_gpu_lee_nvidia_smi(self):
        linea = "NVIDIA GeForce RTX 4070 Laptop GPU, 550.54, 900, 8188, 32, 50, 1500, 20.1\n"
        with mock.patch.object(O.subprocess, "run", return_value=self.salida(linea)):
            (g,) = O.gpu()
        self.assertEqual((g["uso_pct"], g["memoria_usada_mib"], g["memoria_total_mib"]), ("32", "900", "8188"))
        with mock.patch.object(O.subprocess, "run", return_value=self.salida("", 9)):
            self.assertIsNone(O.gpu())


@unittest.skipUnless(SDK, "el SDK de OpenHands no está en este Python")
class Comparacion(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="oh5-a1-"))
        self.exp = self.tmp / "informes" / "a1-p1-s11-x"
        (self.exp / "estado_ollama").mkdir(parents=True)

    def corrida(self, nombre, respuesta, antes=True):
        srv = S.Servidor(respuesta, demora=0.02)
        self.addCleanup(srv.cerrar)
        e = self.tmp / "corridas" / nombre / "ejecuciones" / "P1__qwen"
        r = e / "instancia" / "resultados" / "r1"
        r.mkdir(parents=True)
        # el mismo directorio de trabajo en todas, como /work en el sandbox (el SDK lo incluye en cada observación)
        work = self.tmp / "work"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir()
        (e / "ejecucion.json").write_text(json.dumps({"estado_ejecucion": "x", "cambio_de_modelo": True}))
        litellm.OllamaChatConfig(num_ctx=16384)
        llm = LLM(usage_id="agente", model=f"ollama_chat/{QWEN}", base_url=f"http://127.0.0.1:{srv.puerto}",
                  api_key=SecretStr("x"), native_tool_calling=False, temperature=0.2, seed=11, reasoning_effort="none",
                  capability_overrides={"supports_sampling_params": True}, max_input_tokens=16384, num_retries=0,
                  timeout=30, log_completions=True, log_completions_folder=str(r / "estado" / "llm"))
        agent = Agent(llm=llm, tools=[Tool(name=TerminalTool.name, params={"terminal_type": "subprocess"})],
                      include_default_tools=["FinishTool"])
        conv = Conversation(agent=agent, workspace=str(work), persistence_dir=str(r / "estado" / "conversacion"),
                            max_iteration_per_run=10, stuck_detection=True, visualizer=None)
        conv.send_message("tarea")
        conv.run()
        if antes:
            (self.exp / "estado_ollama" / f"{nombre}-antes.json").write_text(json.dumps(
                {"resultado": "en_frio", "inicial": {"cargados": [], "version": "0.0", "entorno_ollama": None, "gpu": None}}))
            (self.exp / "estado_ollama" / f"{nombre}-despues.json").write_text(json.dumps({"contaminada": False}))
        return nombre

    def experimento(self, nombres):
        (self.exp / "experimento.json").write_text(json.dumps({"corridas": nombres}))
        import comparar_a1 as C
        return C.analizar(self.exp, self.tmp / "corridas"), C

    def test_tres_identicas_son_reproducibles(self):
        fijo = lambda n, m: S.FIN if n >= 4 else S.T(f"echo {n}")  # noqa: E731
        r, C = self.experimento([self.corrida(f"r{k}", fijo) for k in (1, 2, 3)])
        self.assertEqual(r["veredicto"], "REPRODUCIBLE", r["pares"])
        self.assertEqual(len(r["pares"]), 3)
        self.assertTrue(all(c["llamadas"] == 4 for c in r["corridas"]))       # 4 respuestas crudas por corrida
        self.assertIn("REPRODUCIBLE", C.markdown(r))

    def test_divergencia_con_el_mismo_pedido(self):
        fijo = lambda n, m: S.FIN if n >= 4 else S.T(f"echo {n}")  # noqa: E731
        ruido = lambda n, m: S.FIN if n >= 4 else S.T("echo 2b" if n == 2 else f"echo {n}")  # noqa: E731
        r, _ = self.experimento([self.corrida("r1", fijo), self.corrida("r2", fijo), self.corrida("r3", ruido)])
        self.assertEqual(r["veredicto"], "NO REPRODUCIBLE (el modelo respondió distinto a pedidos idénticos)")
        par = {tuple(p["par"]): p for p in r["pares"]}
        self.assertTrue(par[("r1", "r2")]["identicas"])
        p = par[("r1", "r3")]
        self.assertEqual(p["primera_accion_distinta"], 1)
        self.assertEqual(p["primera_llamada_distinta"], 1)
        self.assertTrue(p["detalle"]["pedido_identico"])          # misma entrada, otra respuesta: es del modelo
        self.assertIsNotNone(p["detalle"]["caracter"])
        self.assertEqual(p["no_determinismo_del_modelo_en_llamadas"], [1])

    def test_ruido_del_entorno_no_se_confunde_con_el_modelo(self):
        import comparar_a1 as C
        a = '[{"content": "Ran 3 tests in 0.001s\\nOK"}]'
        b = '[{"content": "Ran 3 tests in 0.002s\\nOK"}]'
        self.assertNotEqual(a, b)
        self.assertEqual(C.sin_ruido(a), C.sin_ruido(b))
        self.assertNotEqual(C.sin_ruido(a), C.sin_ruido(a.replace("OK", "FAILED")))
        # y con conversaciones reales: un comando cuya salida trae la duración
        dur = lambda n, m: S.FIN if n >= 3 else S.T("python3 -c 'print(\"Ran 3 tests in %.3fs\" % __import__(\"time\").time())'")  # noqa: E731
        r, _ = self.experimento([self.corrida("r1", dur), self.corrida("r2", dur)])
        (p,) = r["pares"]
        self.assertTrue(p["identicas"], p)                       # mismas respuestas y acciones
        self.assertGreaterEqual(p["pedidos"]["solo_ruido_del_entorno"], 1)
        self.assertEqual(p["pedidos"]["distinto"], 0)
        self.assertEqual(r["veredicto"], "REPRODUCIBLE")

    def test_sin_estado_inicial_no_es_concluyente(self):
        fijo = lambda n, m: S.FIN if n >= 3 else S.T(f"echo {n}")  # noqa: E731
        r, _ = self.experimento([self.corrida("r1", fijo), self.corrida("r2", fijo, antes=False)])
        self.assertEqual(r["veredicto"], "NO CONCLUYENTE")
        self.assertTrue(any("falta el estado inicial" in x for x in r["equivalencia"]["problemas"]))


if __name__ == "__main__":
    unittest.main()
