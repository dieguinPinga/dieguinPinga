"""Verificación mecánica de send_message()/pause() en OpenHands SDK 1.58.1 con un LLM simulado (sin inferencia).

Corre en proceso, sin sandbox, en un directorio temporal; el «modelo» es un servidor HTTP local con el
formato de Ollama que devuelve llamadas <function=...> fijas. Uso: <venv>/bin/python -I experimento.py
"""
import http.server
import importlib.metadata as md
import json
import os
import tempfile
import threading
import time

os.environ["OPENHANDS_SUPPRESS_BANNER"] = "1"
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
import litellm  # noqa: E402
from pydantic import SecretStr  # noqa: E402

from openhands.sdk import LLM, Agent, Conversation, Tool  # noqa: E402
from openhands.sdk.event import ActionEvent, MessageEvent  # noqa: E402
from openhands.tools.terminal import TerminalTool  # noqa: E402

PEDIDOS = []
CMD = lambda n: f"<function=terminal>\n<parameter=command>echo paso {n}</parameter>\n</function>"  # noqa: E731
FIN = "<function=finish>\n<parameter=message>listo</parameter>\n</function>"


class H(http.server.BaseHTTPRequestHandler):
    demora = 0.5
    respuesta = staticmethod(lambda n: CMD(n))

    def do_POST(self):
        d = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/api/show":
            b = b'{"capabilities":["completion"],"model_info":{"x.context_length":32768}}'
        else:
            PEDIDOS.append({"t": time.monotonic(), "texto": json.dumps(d["messages"])})
            time.sleep(H.demora)
            b = json.dumps({"message": {"role": "assistant", "content": H.respuesta(len(PEDIDOS))}, "done": True,
                            "done_reason": "stop", "prompt_eval_count": 100, "eval_count": 10}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()


def conversacion(max_iter):
    litellm.OllamaChatConfig(num_ctx=16384)
    llm = LLM(usage_id="a", model="ollama_chat/simulado", base_url=f"http://127.0.0.1:{srv.server_port}",
              api_key=SecretStr("x"), native_tool_calling=False, reasoning_effort="none",
              capability_overrides={"supports_sampling_params": True}, max_input_tokens=16384, num_retries=0, timeout=30)
    agent = Agent(llm=llm, tools=[Tool(name=TerminalTool.name, params={"terminal_type": "subprocess"})],
                  include_default_tools=["FinishTool"])
    w = tempfile.mkdtemp()
    return Conversation(agent=agent, workspace=w, persistence_dir=os.path.join(w, "p"), max_iteration_per_run=max_iter,
                        stuck_detection=True, visualizer=None)


def resumen(conv):
    evs = list(conv.state.events)
    acciones = sum(isinstance(e, ActionEvent) for e in evs)
    mensajes = [(i, e.sender) for i, e in enumerate(evs) if isinstance(e, MessageEvent) and e.source == "user"]
    return acciones, mensajes, conv.state.execution_status.value, conv.state.id


def principal():
    print("openhands-sdk", md.version("openhands-sdk"))

    print("\n== A) send_message() mientras run() está activo (ejemplo 18)")
    PEDIDOS.clear()
    H.demora, H.respuesta = 1.0, staticmethod(lambda n: CMD(n))
    conv = conversacion(8)
    conv.send_message("tarea inicial")
    hilo = threading.Thread(target=conv.run)
    hilo.start()
    time.sleep(2.5)
    t0 = time.monotonic()
    conv.send_message("MENSAJE DEL COORDINADOR", sender="oh-duo-04")
    bloqueo = time.monotonic() - t0
    hilo.join()
    acciones, mensajes, estado, _ = resumen(conv)
    llego = next((k + 1 for k, p in enumerate(PEDIDOS) if "MENSAJE DEL COORDINADOR" in p["texto"]), None)
    print(f"   send_message esperó {bloqueo:.2f} s; mensajes de usuario {mensajes}; lo incluye el pedido "
          f"#{llego} de {len(PEDIDOS)}; acciones {acciones} (límite 8); estado {estado}")

    print("\n== B) pause() → send_message() → run() (ejemplo 09)")
    PEDIDOS.clear()
    conv = conversacion(8)
    conv.send_message("tarea inicial")
    hilo = threading.Thread(target=conv.run)
    hilo.start()
    time.sleep(4.5)
    t0 = time.monotonic()
    conv.pause()
    bloqueo = time.monotonic() - t0
    hilo.join()
    a1, _, e1, c1 = resumen(conv)
    conv.send_message("MENSAJE DEL COORDINADOR", sender="oh-duo-04")
    conv.run()
    a2, _, e2, c2 = resumen(conv)
    print(f"   pause() esperó {bloqueo:.2f} s; tras la pausa {a1} acciones ({e1}); tras run() {a2} acciones en "
          f"total con límite 8 por run ({e2}); misma conversación: {c1 == c2}")

    print("\n== C) el mensaje llega después de finish")
    PEDIDOS.clear()
    H.demora, H.respuesta = 0.2, staticmethod(lambda n: FIN if n >= 2 else CMD(n))
    conv = conversacion(8)
    conv.send_message("tarea inicial")
    conv.run()
    antes = len(PEDIDOS)
    conv.send_message("MENSAJE TARDÍO", sender="oh-duo-04")
    time.sleep(1)
    print(f"   estado {resumen(conv)[2]}; pedidos al LLM después del mensaje: {len(PEDIDOS) - antes}")

    print("\n== D) el mismo mensaje dos veces")
    PEDIDOS.clear()
    H.demora, H.respuesta = 0.5, staticmethod(lambda n: FIN if n >= 4 else CMD(n))
    conv = conversacion(8)
    conv.send_message("tarea inicial")
    hilo = threading.Thread(target=conv.run)
    hilo.start()
    time.sleep(0.8)
    conv.send_message("REPETIDO", sender="oh-duo-04")
    conv.send_message("REPETIDO", sender="oh-duo-04")
    hilo.join()
    n = sum(1 for e in conv.state.events if isinstance(e, MessageEvent) and "REPETIDO" in str(e.llm_message.content))
    print(f"   MessageEvent con el mismo texto: {n}")


if __name__ == "__main__":
    principal()
