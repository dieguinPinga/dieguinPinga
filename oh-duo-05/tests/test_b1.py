"""Experimento B1 sin inferencia: comprobaciones contra A1 y comparación con el contrafáctico (SDK real en proceso).
   <venv>/bin/python -I -m unittest discover -s tests -p test_b1.py -v
"""
import hashlib
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
sys.path.insert(0, str(AQUI))
sys.path.insert(0, str(AQUI.parent / "reproducibilidad"))
os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
import preparar_b1 as P  # noqa: E402
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
MSG = "You are repeating the same tests with the same failure. Review your diagnosis. Do not modify the tests."


def escribir_lineas(p: Path, filas):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(f) + "\n" for f in filas))


class Comprobaciones(unittest.TestCase):
    """preparar_b1.comprobar: todo lo que tiene que valer antes de usar la GPU."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="oh5-b1p-"))
        self.codigo = self.tmp / "agente.py"
        self.codigo.write_text("codigo de A1\n")
        self.a1 = self.tmp / "informes" / "a1-p1-s11-x"
        self.a1.mkdir(parents=True)
        corridas = ["a1-p1-s11-x-r1", "a1-p1-s11-x-r2"]
        (self.a1 / "experimento.json").write_text(json.dumps({
            "corridas": corridas, "corridas_dir": str(self.tmp / "corridas"), "problema": "P1",
            "huellas": {str(self.codigo): hashlib.sha256(self.codigo.read_bytes()).hexdigest()}}))
        (self.a1 / "comparacion_a1.json").write_text(json.dumps({"veredicto": "REPRODUCIBLE"}))
        (self.a1 / "ollama_version.json").write_text(json.dumps({"version": "0.12.6"}))
        for n in corridas:
            escribir_lineas(self.tmp / "informes" / n / "coordinador" / "P1__qwen" / "decisiones.jsonl",
                            [{"tipo": "advertencia", "iteracion": 3}, {"tipo": "nueva_estrategia", "iteracion": 8, "mensaje_sha256": "abc"}])
            escribir_lineas(self.tmp / "informes" / n / "despachos.jsonl", [{"iteracion": 8, "resultado": "simulado"}])
            ps = self.tmp / "corridas" / n / "ejecuciones" / "P1__qwen" / "instancia" / "resultados" / "r" / "api_ps.json"
            ps.parent.mkdir(parents=True)
            ps.write_text(json.dumps({"models": [{"name": QWEN, "digest": "sha256:aaaa"}]}))

    def comprobar(self, modo="real", version="0.12.6", digest="sha256:aaaa"):
        with mock.patch.object(P, "version_actual", return_value=version), mock.patch.object(P, "digest_actual", return_value=digest):
            return P.comprobar(self.a1, modo, "http://x")

    def test_todo_igual_que_a1(self):
        problemas, info = self.comprobar()
        self.assertEqual(problemas, [])
        self.assertEqual(info["prediccion"]["decision_en_la_iteracion"], 8)
        self.assertEqual(info["digest"], {"a1": "sha256:aaaa", "ahora": "sha256:aaaa"})

    def test_codigo_cambiado(self):
        self.codigo.write_text("otro codigo\n")
        self.assertTrue(any("el código cambió" in p for p in self.comprobar()[0]))

    def test_version_o_modelo_cambiados(self):
        self.assertTrue(any("versión de Ollama" in p for p in self.comprobar(version="0.13.0")[0]))
        self.assertTrue(any("el modelo cambió" in p for p in self.comprobar(digest="sha256:bbbb")[0]))
        self.assertTrue(any("no se puede verificar" in p for p in self.comprobar(digest=None)[0]))
        self.assertEqual(self.comprobar(modo="simulado", digest=None)[0], [])       # el simulado no tiene digest

    def test_a1_no_reproducible(self):
        (self.a1 / "comparacion_a1.json").write_text(json.dumps({"veredicto": "NO REPRODUCIBLE"}))
        self.assertTrue(any("no dio REPRODUCIBLE" in p for p in self.comprobar()[0]))

    def test_sin_decision_o_sin_margen_en_a1(self):
        for n in ("a1-p1-s11-x-r1", "a1-p1-s11-x-r2"):
            escribir_lineas(self.tmp / "informes" / n / "despachos.jsonl", [{"iteracion": 27, "resultado": "sin_margen"}])
        self.assertTrue(any("no habría enviado" in p for p in self.comprobar()[0]))
        for n in ("a1-p1-s11-x-r1", "a1-p1-s11-x-r2"):
            escribir_lineas(self.tmp / "informes" / n / "coordinador" / "P1__qwen" / "decisiones.jsonl", [{"tipo": "advertencia"}])
        self.assertTrue(any("nunca decidió" in p for p in self.comprobar()[0]))

    def test_decisiones_distintas_entre_corridas_de_a1(self):
        escribir_lineas(self.tmp / "informes" / "a1-p1-s11-x-r2" / "coordinador" / "P1__qwen" / "decisiones.jsonl",
                        [{"tipo": "nueva_estrategia", "iteracion": 9, "mensaje_sha256": "abc"}])
        self.assertTrue(any("decidió distinto" in p for p in self.comprobar()[0]))


@unittest.skipUnless(SDK, "el SDK de OpenHands no está en este Python")
class Contrafactico(unittest.TestCase):
    """comparar_b1 con conversaciones reales del SDK: A1 determinista y variantes de B1."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="oh5-b1c-"))
        cls.corridas = cls.tmp / "corridas"
        cls.a1 = cls.experimento("a1-p1-s11-x", [cls.corrida(f"a1-p1-s11-x-r{k}", cls.bucle) for k in (1, 2)])

    @staticmethod
    def bucle(n, m):
        return S.T(f"echo paso {n % 3}")

    @classmethod
    def corrida(cls, nombre, respuesta, mensaje_tras=None, max_iter=10):
        srv = S.Servidor(respuesta, demora=0.02)
        e = cls.corridas / nombre / "ejecuciones" / "P1__qwen"
        r = e / "instancia" / "resultados" / "r1"
        r.mkdir(parents=True)
        work = cls.tmp / "work"                   # el mismo /work en todas, como en el sandbox
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir()
        (e / "ejecucion.json").write_text(json.dumps({"estado_ejecucion": "x", "cambio_de_modelo": True}))
        (e / "instancia" / "mision.json").write_text(json.dumps({"modulo": "intervalos"}))
        litellm.OllamaChatConfig(num_ctx=16384)
        llm = LLM(usage_id="agente", model=f"ollama_chat/{QWEN}", base_url=f"http://127.0.0.1:{srv.puerto}",
                  api_key=SecretStr("x"), native_tool_calling=False, temperature=0.2, seed=11, reasoning_effort="none",
                  capability_overrides={"supports_sampling_params": True}, max_input_tokens=16384, num_retries=0,
                  timeout=30, log_completions=True, log_completions_folder=str(r / "estado" / "llm"))
        agent = Agent(llm=llm, tools=[Tool(name=TerminalTool.name, params={"terminal_type": "subprocess"})],
                      include_default_tools=["FinishTool"])
        conv = Conversation(agent=agent, workspace=str(work), persistence_dir=str(r / "estado" / "conversacion"),
                            max_iteration_per_run=max_iter, stuck_detection=True, visualizer=None)
        conv.send_message("tarea")
        if mensaje_tras is not None:
            def enviar():
                while sum(type(x).__name__ == "ActionEvent" for x in list(conv.state.events)) < mensaje_tras:
                    time.sleep(0.005)
                with conv.state:
                    conv.send_message(MSG, sender="oh-duo-04:P1__qwen-nueva_estrategia-it3")
                escribir_lineas(r / "estado" / "entregas.jsonl", [{"id": "x", "resultado": "entregado", "iteracion_al_entregar": mensaje_tras}])
            threading.Thread(target=enviar, daemon=True).start()
        conv.run()
        srv.cerrar()
        return nombre

    @classmethod
    def experimento(cls, nombre, corridas):
        d = cls.tmp / "informes" / nombre
        (d / "estado_ollama").mkdir(parents=True, exist_ok=True)
        (d / "experimento.json").write_text(json.dumps({"corridas": corridas, "huellas": {}, "problema": "P1"}))
        (d / "ollama_version.json").write_text(json.dumps({"version": "0.0-simulado"}))
        for n in corridas:
            (d / "estado_ollama" / f"{n}-antes.json").write_text(json.dumps(
                {"resultado": "en_frio", "inicial": {"cargados": [], "version": "0.0", "entorno_ollama": None, "gpu": None}}))
            (d / "estado_ollama" / f"{n}-despues.json").write_text(json.dumps({"contaminada": False}))
        return d

    def b1(self, nombre, respuesta, mensaje_tras=3):
        c = self.corrida(nombre, respuesta, mensaje_tras)
        d = self.experimento(nombre.rsplit("-r", 1)[0], [c])
        import comparar_b1 as B
        r = B.analizar(self.a1, d, self.corridas, self.corridas, None)
        self.assertEqual(r["a1_veredicto"], "REPRODUCIBLE")
        self.assertEqual(r["problemas_globales"], [])
        return r["corridas"][0]

    def test_cambia_de_conducta_y_termina(self):
        f = self.b1("b1-cambia-r1", lambda n, m: S.FIN if MSG in json.dumps(m) else self.bucle(n, m))
        self.assertTrue(f["valida"], f["problemas"])
        self.assertEqual(f["prefijo"]["pedidos"]["distinto"], 0)
        self.assertEqual(f["prefijo"]["respuestas_distintas"], [])
        self.assertEqual(f["pedido_con_mensaje"]["igual_a_a1_salvo_el_mensaje"], "identico")
        self.assertEqual(f["pedido_con_mensaje"]["entradas_agregadas"], 1)
        self.assertEqual(f["conducta"]["llamadas_hasta_el_cambio"], 0)
        self.assertFalse(f["conducta"]["misma_respuesta_que_a1_en_esa_llamada"])
        self.assertEqual(f["terminacion"]["tipo"], "finish")
        self.assertEqual(f["terminacion"]["control"], "agotamiento_de_iteraciones")

    def test_mensaje_ignorado_no_es_cambio_de_conducta(self):
        f = self.b1("b1-ignora-r1", self.bucle)
        self.assertTrue(f["valida"], f["problemas"])
        self.assertFalse(f["conducta"]["cambio"])
        self.assertTrue(f["conducta"]["misma_respuesta_que_a1_en_esa_llamada"])
        self.assertEqual(f["terminacion"]["tipo"], "agotamiento_de_iteraciones")

    def test_stuck_tras_el_mensaje_se_separa(self):
        f = self.b1("b1-stuck-r1", lambda n, m: S.T("ls") if MSG in json.dumps(m) else self.bucle(n, m))
        self.assertTrue(f["valida"], f["problemas"])
        self.assertTrue(f["conducta"]["cambio"])                          # cambió...
        self.assertEqual(f["terminacion"]["tipo"], "stuck")               # ...y el SDK la cortó
        self.assertTrue(f["terminacion"]["ventana_del_stuck_empieza_tras_el_mensaje"])
        self.assertTrue(f["terminacion"]["stuck_patron"].startswith("accion_y_observacion_repetidas"))

    def test_divergencia_antes_del_mensaje_invalida_la_corrida(self):
        f = self.b1("b1-diverge-r1", lambda n, m: S.T("echo otra") if n == 2 else self.bucle(n, m))
        self.assertFalse(f["valida"])
        self.assertTrue(any("ANTES del mensaje" in p for p in f["problemas"]), f["problemas"])

    def test_sin_mensaje_no_es_valida(self):
        f = self.b1("b1-sinmsg-r1", self.bucle, mensaje_tras=None)
        self.assertFalse(f["valida"])
        self.assertTrue(any("no llegó a intervenir" in p for p in f["problemas"]))


if __name__ == "__main__":
    unittest.main()
