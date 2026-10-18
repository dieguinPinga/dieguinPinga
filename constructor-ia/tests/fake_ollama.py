"""Servidor Ollama simulado para pruebas: responde /api/chat en streaming NDJSON."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeOllama:
    def __init__(self, responses=None, delay=0.0, models=("qwen3:4b-instruct", "qwen3:1.7b")):
        self.responses = list(responses or [])
        self.default = "```python\ndef f():\n    return 1\n```"
        self.delay = delay
        self.models = list(models)
        self.requests: list[dict] = []
        self.disconnects = 0
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _json(self, obj):
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/tags":
                    self._json({"models": [{"name": m} for m in outer.models]})
                elif self.path == "/api/version":
                    self._json({"version": "0.34.0"})
                elif self.path == "/api/ps":
                    self._json({"models": []})
                else:
                    self.send_error(404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with outer._lock:
                    outer.requests.append(body)
                    text = outer.responses.pop(0) if outer.responses else outer.default
                    if callable(text):  # respuesta dinámica: recibe la petición completa
                        text = text(body)
                    outer.active += 1
                    outer.max_active = max(outer.max_active, outer.active)
                try:
                    if isinstance(text, Exception):
                        self.send_response(500)
                        msg = json.dumps({"error": str(text)}).encode()
                        self.send_header("Content-Length", str(len(msg)))
                        self.end_headers()
                        self.wfile.write(msg)
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.end_headers()
                    pieces = [text[i:i + 20] for i in range(0, len(text), 20)] or [""]
                    for p in pieces:
                        if outer.delay:
                            time.sleep(outer.delay)
                        self.wfile.write((json.dumps({"message": {"role": "assistant", "content": p}, "done": False}) + "\n").encode())
                        self.wfile.flush()
                    self.wfile.write((json.dumps({"message": {"role": "assistant", "content": ""}, "done": True,
                                                  "done_reason": "stop", "eval_count": len(text) // 4,
                                                  "prompt_eval_count": 100}) + "\n").encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    with outer._lock:
                        outer.disconnects += 1
                finally:
                    with outer._lock:
                        outer.active -= 1
                    self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
