"""diagnostico/analizar_par.py con conversaciones reales del SDK y el modelo simulado, en proceso. Sin inferencia.
   <venv>/bin/python -I -m unittest discover -s tests -p test_diagnostico.py -v
Comprueba que el detector stuck reproducido sobre los eventos guardados coincide con lo que hizo el SDK.
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI))
sys.path.insert(0, str(AQUI.parent / "diagnostico"))
os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
import simulado as S  # noqa: E402

try:
    import litellm
    from pydantic import SecretStr
    from openhands.sdk import LLM, Agent, Conversation, Tool
    from openhands.tools.terminal import TerminalTool
    SDK = True
except ImportError:
    SDK = False

import analizar_par as A  # noqa: E402


@unittest.skipUnless(SDK, "el SDK de OpenHands no está en este Python")
class Diagnostico(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="oh5-diag-"))

    def corrida(self, nombre, respuesta, max_iter=12, mensaje_en=None):
        """Una corrida con la misma estructura de carpetas que deja ejecutar.py."""
        srv = S.Servidor(respuesta, demora=0.05)
        self.addCleanup(srv.cerrar)
        e = self.tmp / nombre / "ejecuciones" / "P1__qwen"
        r = e / "instancia" / "resultados" / "r1"
        (r / "work").mkdir(parents=True)
        (e / "ejecucion.json").write_text(json.dumps({"estado_ejecucion": "x", "cambio_de_modelo": False,
                                                       "modelos_cargados_antes": ["qwen2.5-coder:7b"]}))
        litellm.OllamaChatConfig(num_ctx=16384)
        llm = LLM(usage_id="agente", model="ollama_chat/qwen2.5-coder:7b", base_url=f"http://127.0.0.1:{srv.puerto}",
                  api_key=SecretStr("x"), native_tool_calling=False, temperature=0.2, seed=11, reasoning_effort="none",
                  capability_overrides={"supports_sampling_params": True}, max_input_tokens=16384, num_retries=0, timeout=30)
        agent = Agent(llm=llm, tools=[Tool(name=TerminalTool.name, params={"terminal_type": "subprocess"})],
                      include_default_tools=["FinishTool"])
        conv = Conversation(agent=agent, workspace=str(r / "work"), persistence_dir=str(r / "estado" / "conversacion"),
                            max_iteration_per_run=max_iter, stuck_detection=True, visualizer=None)
        conv.send_message("tarea")
        if mensaje_en is not None:
            def enviar():
                while sum(type(x).__name__ == "ActionEvent" for x in list(conv.state.events)) < mensaje_en:
                    time.sleep(0.01)
                with conv.state:
                    conv.send_message("Review your diagnosis.", sender="oh-duo-04:e1")
            threading.Thread(target=enviar, daemon=True).start()
        conv.run()
        return self.tmp / nombre, conv

    def test_stuck_reproducido_coincide_con_el_sdk(self):
        c, conv = self.corrida("c", lambda n, m: S.T("ls"))
        self.assertEqual(conv.state.execution_status.value, "stuck")
        a = A.analizar_corrida(c)
        self.assertEqual(a["estado_final_sdk"], "stuck")
        self.assertEqual(a["stuck"]["stuck_tras_evento"], len(conv.state.events) - 1)
        self.assertTrue(a["stuck"]["patron"].startswith("accion_y_observacion_repetidas"))
        self.assertEqual(len(a["acciones"]), 4)

    def test_un_mensaje_de_usuario_reinicia_la_ventana(self):
        c, conv = self.corrida("m", lambda n, m: S.T("ls"), mensaje_en=2)
        a = A.analizar_corrida(c)
        self.assertEqual(conv.state.execution_status.value, "stuck")
        (msg,) = a["mensajes_de_usuario_posteriores"]
        self.assertEqual(msg["tipo"], "coordinador")
        self.assertEqual(msg["primera_accion_despues"]["n"], msg["acciones_antes"])
        self.assertGreater(a["stuck"]["ventana_desde_evento"], msg["evento"])     # la ventana empieza después
        cx = msg["contexto_de_la_llamada_siguiente"]                             # el modelo lo tuvo en su contexto
        self.assertTrue(cx["posiciones_del_texto"], cx)
        self.assertEqual(cx["rol"], "user")
        self.assertIsNone(a["condensador"])
        self.assertEqual(len(a["acciones"]), msg["acciones_antes"] + 4)           # 4 iguales DESPUÉS del mensaje
        self.assertEqual(a["stuck"]["stuck_tras_evento"], len(conv.state.events) - 1)

    def test_sin_stuck_informa_la_racha(self):
        c, conv = self.corrida("v", lambda n, m: S.T(f"echo {n % 3}"), max_iter=8)
        a = A.analizar_corrida(c)
        self.assertIsNone(a["stuck"]["stuck_tras_evento"])
        self.assertEqual(a["stuck"]["racha_maxima_acciones_identicas"], 1)

    def test_par_identico_y_par_que_diverge_en_la_primera_accion(self):
        fijo = lambda n, m: S.FIN if n >= 3 else S.T(f"echo {n}")  # noqa: E731
        c, _ = self.corrida("c1", fijo)
        i, _ = self.corrida("i1", fijo)
        p = A.comparar(A.analizar_corrida(c), A.analizar_corrida(i))
        self.assertTrue(all(p["entradas_iguales"].values()), p["entradas_iguales"])
        self.assertIsNone(p["primera_divergencia"])
        otro = lambda n, m: S.FIN if n >= 3 else S.T(f"echo otro {n}")  # noqa: E731
        i2, _ = self.corrida("i2", otro)
        p2 = A.comparar(A.analizar_corrida(c), A.analizar_corrida(i2))
        self.assertEqual(p2["primera_divergencia"], 0)
        self.assertEqual(p2["hipotesis"][0]["veredicto"], "descartada")       # mismas entradas: no es H1
        md = A.markdown(A.analizar_corrida(c), A.analizar_corrida(i2), p2)
        self.assertIn("Acciones idénticas antes de divergir: **0**", md)


if __name__ == "__main__":
    unittest.main()
