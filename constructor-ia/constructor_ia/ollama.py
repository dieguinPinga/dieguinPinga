"""Cliente mínimo para la API local de Ollama (instancia CPU, puerto 11435).

Usa streaming para poder cancelar una generación cerrando la conexión (Ollama
aborta la inferencia cuando el cliente se desconecta) y para detectar
respuestas incompletas (`done_reason == "length"` o flujo cortado).
"""
from __future__ import annotations

import http.client
import json
import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from .config import ModelConfig

THINK_RE = re.compile(r"<think>.*?(</think>|$)", re.S)
CONNECT_TIMEOUT_S = 5.0   # conectar (no generar): si no alcanza, el servidor no está escuchando
SOCKET_MARGIN_S = 30.0    # el socket nunca se adelanta al vigilante del plazo total
MAX_RESPONSE_CHARS = 200_000


class OllamaError(Exception):
    pass


class OllamaUnavailable(OllamaError):
    """No se pudo establecer la conexión: el servidor no está escuchando."""


class OllamaTimeout(OllamaError):
    """El servidor aceptó el pedido pero no respondió a tiempo.

    phase: «espera» (no llegó ni el encabezado: el servidor estaba ocupado o trabado antes de generar) o
    «generacion» (empezó a responder y no terminó). En ambos casos el servidor sigue vivo y probablemente sigue
    procesando el pedido abandonado: no es lo mismo que «no disponible».
    """

    def __init__(self, msg: str, phase: str, waited_s: float):
        super().__init__(msg)
        self.phase, self.waited_s = phase, waited_s


class ContextOverflow(OllamaError):
    """El mensaje no entra en el contexto configurado (p. ej. exceed_context_size_error).

    Repetir el mismo mensaje fallaría igual: quien llama debe reducirlo.
    """

    def __init__(self, msg: str, prompt_tokens: int | None = None, n_ctx: int | None = None):
        super().__init__(msg)
        self.prompt_tokens, self.n_ctx = prompt_tokens, n_ctx


class Cancelled(Exception):
    pass


CONTEXT_ERROR_RE = re.compile(r"exceed[s_ ]*(?:the )?(?:available )?context|context[_ ](?:size|length|window)|"
                              r"too (?:long|many tokens)|n_ctx", re.I)


def _first_int(d: dict, keys) -> int | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    return None


def parse_context_error(error) -> ContextOverflow | None:
    """Reconoce un error de contexto excedido (texto u objeto) y extrae las cifras si las trae."""
    detail = error if isinstance(error, dict) else {}
    text = json.dumps(error, ensure_ascii=False) if isinstance(error, dict) else str(error)
    if not CONTEXT_ERROR_RE.search(text):
        return None
    prompt = _first_int(detail, ("n_prompt_tokens", "prompt_tokens", "n_tokens", "tokens"))
    ctx = _first_int(detail, ("n_ctx", "num_ctx", "context_size", "context_length", "n_ctx_slot"))
    if prompt is None:
        m = (re.search(r"n_prompt_tokens\D{0,4}(\d+)", text) or re.search(r"(\d+)\s*tokens", text)
             or re.search(r"(?:requires|requiere|prompt)\D{0,20}(\d{3,})", text))
        prompt = int(m.group(1)) if m else None
    if ctx is None:
        found = [int(x) for x in re.findall(r"(?:n_ctx|num_ctx|context(?:[_ ](?:size|length|window))?)\D{0,25}?(\d{3,})",
                                             text, re.I)]
        ctx = next((x for x in found if x != prompt), None)
    msg = (detail.get("message") if isinstance(detail.get("message"), str) else None) or text
    return ContextOverflow(f"Contexto excedido: {msg[:200]}", prompt, ctx)


@dataclass
class Generation:
    text: str
    done: bool
    done_reason: str | None
    prompt_tokens: int | None
    eval_tokens: int | None
    duration_ms: int
    truncated: bool
    # Tiempos informados por Ollama (ms) y medidos por el cliente: su diferencia revela esperas del servidor
    # que no son carga del modelo, evaluación del prompt ni generación (p. ej. cola o mantenimiento de caché).
    load_ms: int | None = None
    prompt_eval_ms: int | None = None
    eval_ms: int | None = None
    total_ms: int | None = None
    first_byte_ms: int | None = None

    def timings(self) -> dict:
        def rate(tokens, ms):
            return round(tokens / (ms / 1000), 2) if tokens and ms else None
        known = sum(x for x in (self.load_ms, self.prompt_eval_ms, self.eval_ms) if x)
        return {
            "reloj_ms": self.duration_ms, "primer_byte_ms": self.first_byte_ms, "total_ollama_ms": self.total_ms,
            "carga_ms": self.load_ms, "prompt_ms": self.prompt_eval_ms, "generacion_ms": self.eval_ms,
            "tokens_prompt": self.prompt_tokens, "tokens_generados": self.eval_tokens,
            "prompt_tokens_s": rate(self.prompt_tokens, self.prompt_eval_ms),
            "generacion_tokens_s": rate(self.eval_tokens, self.eval_ms),
            # tiempo de reloj que Ollama no atribuye a carga, prompt ni generación
            "sin_explicar_ms": max(0, self.duration_ms - known) if self.total_ms is not None else None,
            "fin": self.done_reason,
        }


def _ms(ns) -> int | None:
    return int(ns / 1_000_000) if isinstance(ns, (int, float)) and ns >= 0 else None


class InferenceGate:
    """Garantiza una sola inferencia pesada a la vez en todo el proceso."""

    def __init__(self):
        self._lock = threading.Lock()
        self.holder: str | None = None

    def acquire(self, who: str, cancel: threading.Event | None = None) -> None:
        while not self._lock.acquire(timeout=0.5):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
        self.holder = who

    def release(self) -> None:
        self.holder = None
        self._lock.release()


class OllamaClient:
    def __init__(self, base_url: str):
        parts = urlsplit(base_url)
        self.host = parts.hostname or "127.0.0.1"
        self.port = parts.port or 11435

    def _conn(self, timeout: float) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def _get_json(self, path: str, timeout: float = 3.0) -> dict:
        conn = self._conn(timeout)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read(2_000_000)
            if resp.status != 200:
                raise OllamaError(f"HTTP {resp.status} en {path}")
            return json.loads(body)
        except (OSError, http.client.HTTPException, ValueError) as e:
            raise OllamaError(f"Ollama no disponible: {e}") from None
        finally:
            conn.close()

    def tags(self) -> list[str]:
        return [m.get("name", "") for m in self._get_json("/api/tags").get("models", [])]

    def loaded(self) -> list[str]:
        return [m.get("name", "") for m in self._get_json("/api/ps").get("models", [])]

    def version(self) -> str:
        return str(self._get_json("/api/version").get("version", "?"))

    def chat(self, model: ModelConfig, messages: list[dict], cancel: threading.Event | None = None,
             on_progress=None, fmt=None) -> Generation:
        """fmt: None, "json" o un esquema JSON (salida estructurada de Ollama)."""
        try:
            return self._chat(model, messages, cancel, on_progress, use_think=True, fmt=fmt)
        except OllamaError as e:
            # Modelos sin soporte de "think" rechazan el campo: reintentar sin él.
            if "think" in str(e).lower():
                return self._chat(model, messages, cancel, on_progress, use_think=False, fmt=fmt)
            raise

    def _chat(self, model: ModelConfig, messages, cancel, on_progress, use_think: bool, fmt=None) -> Generation:
        payload = {
            "model": model.name,
            "messages": messages,
            "stream": True,
            "options": {
                "num_ctx": model.num_ctx,
                "num_predict": model.num_predict,
                "temperature": model.temperature,
            },
        }
        if use_think:
            payload["think"] = model.think
        if model.keep_alive is not None:
            payload["keep_alive"] = model.keep_alive
        if fmt is not None:
            payload["format"] = fmt
        start = time.monotonic()
        # Conexión con plazo corto: si el servidor no escucha, es «no disponible». Una vez conectado, el plazo total
        # lo controla el vigilante; el socket tiene un margen mayor para no adelantarse y confundir un servidor
        # ocupado con uno caído.
        conn = self._conn(timeout=min(CONNECT_TIMEOUT_S, model.timeout_s))
        try:
            conn.connect()
        except OSError as e:
            conn.close()
            raise OllamaUnavailable(f"No se pudo contactar a Ollama: {e}") from None
        conn.sock.settimeout(model.timeout_s + SOCKET_MARGIN_S)
        stop = threading.Event()
        why: list[str] = []
        got_headers: list[float] = []

        def watchdog():
            # Cierra el socket si se cancela o se agota el plazo total: Ollama
            # detecta la desconexión y aborta la inferencia.
            while not stop.wait(0.2):
                if cancel is not None and cancel.is_set():
                    why.append("cancel")
                elif time.monotonic() - start > model.timeout_s:
                    why.append("timeout")
                else:
                    continue
                try:
                    if conn.sock is not None:
                        conn.sock.shutdown(2)
                except OSError:
                    pass
                return

        wd = threading.Thread(target=watchdog, daemon=True)
        wd.start()
        parts: list[str] = []
        size = 0
        final: dict = {}

        def check_abort(timed_out: bool = False):
            if why and why[0] == "cancel":
                raise Cancelled()
            if why or timed_out:
                waited = time.monotonic() - start
                if got_headers:
                    raise OllamaTimeout(f"Tiempo de generación agotado ({model.timeout_s:.0f} s): Ollama empezó a "
                                        "responder pero no terminó", "generacion", waited)
                raise OllamaTimeout(f"Ollama no respondió en {waited:.0f} s: aceptó el pedido pero no envió nada "
                                    "(servidor ocupado o trabado; puede seguir procesando el pedido abandonado)",
                                    "espera", waited)

        try:
            try:
                conn.request("POST", "/api/chat", body=json.dumps(payload),
                             headers={"Content-Type": "application/json"})
                resp = conn.getresponse()
                got_headers.append(time.monotonic())
            except TimeoutError:
                check_abort(timed_out=True)
                raise
            except (OSError, http.client.HTTPException) as e:
                check_abort()
                raise OllamaError(f"Ollama cortó la conexión sin responder: {e}") from None
            if resp.status != 200:
                body = resp.read(4000).decode("utf-8", "replace")
                try:
                    msg = json.loads(body).get("error", body)
                except (ValueError, AttributeError):
                    msg = body
                overflow = parse_context_error(msg)
                if overflow is not None:
                    raise overflow
                raise OllamaError(f"Ollama respondió HTTP {resp.status}: {str(msg)[:300]}")
            last_progress = 0.0
            while True:
                try:
                    line = resp.readline(1_000_000)
                except TimeoutError:
                    check_abort(timed_out=True)
                    raise
                except (OSError, http.client.HTTPException) as e:
                    check_abort()
                    raise OllamaError(f"Conexión con Ollama interrumpida: {e}") from None
                check_abort()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except ValueError:
                    raise OllamaError("Respuesta de Ollama mal formada") from None
                if "error" in chunk:
                    overflow = parse_context_error(chunk["error"])
                    if overflow is not None:
                        raise overflow
                    raise OllamaError(f"Ollama: {str(chunk['error'])[:300]}")
                piece = (chunk.get("message") or {}).get("content") or ""
                if piece:
                    parts.append(piece)
                    size += len(piece)
                    if size > MAX_RESPONSE_CHARS:
                        raise OllamaError("Respuesta del modelo demasiado larga")
                    if on_progress and time.monotonic() - last_progress > 5:
                        last_progress = time.monotonic()
                        on_progress(size)
                if chunk.get("done"):
                    final = chunk
                    break
        finally:
            stop.set()
            conn.close()
        text = THINK_RE.sub("", "".join(parts)).strip()
        done = bool(final.get("done"))
        reason = final.get("done_reason")
        return Generation(
            text=text, done=done, done_reason=reason,
            prompt_tokens=final.get("prompt_eval_count"), eval_tokens=final.get("eval_count"),
            duration_ms=int((time.monotonic() - start) * 1000),
            truncated=(not done) or reason == "length",
            load_ms=_ms(final.get("load_duration")), prompt_eval_ms=_ms(final.get("prompt_eval_duration")),
            eval_ms=_ms(final.get("eval_duration")), total_ms=_ms(final.get("total_duration")),
            first_byte_ms=int((got_headers[0] - start) * 1000) if got_headers else None,
        )
