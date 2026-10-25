"""Modelo simulado con el formato de Ollama (/api/show, /api/chat) para las pruebas de OH-DUO-05. Sin inferencia.

Responde llamadas <function=...> fijas (el formato por prompt que usa el SDK con native_tool_calling=False).
  Servidor(respuesta, demora)   en un hilo; `respuesta(n, mensajes)` da el texto del pedido n (desde 1).
  bucle_p1                      imita el bucle de Qwen en P1 (corre pruebas, mira, ediciones sin efecto) SIN tocar
                                las pruebas; cuando el pedido trae el mensaje del coordinador, corrige, prueba y
                                termina. Es el modelo de la prueba de integración: el cambio de conducta depende
                                solo de que el mensaje haya llegado a la conversación.
"""
from __future__ import annotations

import http.server
import json
import threading
import time

CMD = lambda n: f"<function=terminal>\n<parameter=command>echo paso {n}</parameter>\n</function>"  # noqa: E731
FIN = "<function=finish>\n<parameter=message>listo</parameter>\n</function>"
SENAL = "Review your diagnosis"          # frase de la plantilla `pedido` de OH-DUO-04


def T(c: str) -> str:
    return f"<function=terminal>\n<parameter=command>{c}</parameter>\n</function>"


def E(**p) -> str:
    return "<function=file_editor>\n" + "".join(f"<parameter={k}>{v}</parameter>\n" for k, v in p.items()) + "</function>"


BUCLE_P1 = [T("python3 -m unittest -v"),
            E(command="view", path="/work/intervalos.py"),
            E(command="str_replace", path="/work/intervalos.py", old_str="resultado.append((inicio, fin))",
              new_str="resultado.append((inicio, fin))"),
            T("python3 -m unittest -v"),
            T("cat intervalos.py"),
            T("python3 -m unittest -v ")]
ARREGLO_P1 = [E(command="str_replace", path="/work/intervalos.py", old_str="(resultado[-1][0], fin)",
                new_str="(resultado[-1][0], max(resultado[-1][1], fin))"),
              T("python3 -m unittest -v"),
              FIN]


def bucle_p1():
    estado = {"desde": None}

    def respuesta(n: int, mensajes: list) -> str:
        if n == 1:                                     # conversación nueva: vuelve al bucle
            estado["desde"] = None
        if estado["desde"] is None and SENAL in json.dumps(mensajes[2:]):
            estado["desde"] = n
        if estado["desde"] is None:
            return BUCLE_P1[(n - 1) % len(BUCLE_P1)]
        return ARREGLO_P1[min(n - estado["desde"], len(ARREGLO_P1) - 1)]
    respuesta.estado = estado
    return respuesta


class Servidor:
    """estado_ollama=False: /api/ps siempre muestra el modelo cargado (pruebas de integración).
    estado_ollama=True: imita a Ollama: el modelo se carga con el primer pedido, cada pedido renueva su
    expires_at, y POST /api/generate {"keep_alive": 0} sin prompt lo descarga (sin generar nada).
    `otros` agrega modelos cargados por «otro proceso» (para probar que A1 no los toca)."""

    def __init__(self, respuesta, demora: float = 0.2, estado_ollama: bool = False):
        self.respuesta, self.demora, self.pedidos = respuesta, demora, []
        self.estado_ollama, self.cargado, self.vence, self.otros, self.descargas = estado_ollama, False, None, [], []
        yo = self

        class H(http.server.BaseHTTPRequestHandler):
            def _json(self, d):
                b = json.dumps(d).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_POST(self):
                d = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/api/show":
                    return self._json({"capabilities": ["completion"], "model_info": {"x.context_length": 32768}})
                if self.path == "/api/generate":
                    yo.descargas.append(d)
                    if d.get("keep_alive") == 0 and not d.get("prompt"):
                        yo.cargado = False
                    return self._json({"model": d.get("model"), "done": True, "done_reason": "unload"})
                yo.cargado, yo.vence = True, f"2099-01-01T00:00:{len(yo.pedidos) % 60:02d}Z"
                if len(d["messages"]) <= 2:            # conversación nueva
                    yo.pedidos.clear()
                yo.pedidos.append(d["messages"])
                time.sleep(yo.demora)
                self._json({"message": {"role": "assistant", "content": yo.respuesta(len(yo.pedidos), d["messages"])},
                            "done": True, "done_reason": "stop", "prompt_eval_count": 100, "eval_count": 10})

            def do_GET(self):
                if not yo.estado_ollama:
                    return self._json({"version": "0.0-simulado", "models": [{"name": "qwen2.5-coder:7b"}]})   # ya cargado
                modelos = ([{"name": "qwen2.5-coder:7b", "expires_at": yo.vence, "size": 1, "size_vram": 1}] if yo.cargado else []) \
                    + [{"name": n, "expires_at": "2099-01-01T00:00:00Z"} for n in yo.otros]
                self._json({"version": "0.0-simulado", "models": modelos})

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.puerto = self.srv.server_port
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def cerrar(self):
        self.srv.shutdown()
        self.srv.server_close()


if __name__ == "__main__":      # python3 simulado.py ARCHIVO_PUERTO [demora] [estado]: sirve bucle_p1 hasta que lo maten
    import sys
    from pathlib import Path
    srv = Servidor(bucle_p1(), demora=float(sys.argv[2]) if len(sys.argv) > 2 else 0.2,
                   estado_ollama=len(sys.argv) > 3 and sys.argv[3] == "estado")
    Path(sys.argv[1]).write_text(str(srv.puerto))
    threading.Event().wait()
