"""Fase 1: compatibilidad de Gemma con OpenHands, medida directamente contra Ollama.

Se ejecuta en la VivoBook (anfitrión), sin OpenHands ni sandbox: solo hace pedidos HTTP a Ollama. Usa las
mismas rutas que permite oh-smoke/relay.py (GET /api/version, /api/tags, /api/ps; POST /api/show, /api/chat)
y solo los dos modelos indicados. No llama a /api/pull, /api/create, /api/delete ni cambia la configuración.

Lo que sí hace: CARGA Gemma en la GPU de Pulenta (Ollama desaloja el modelo que haya) y al final, salvo
--sin-restaurar, vuelve a cargar Qwen con un pedido de 1 token, para medir ese cambio y dejar Pulenta como
estaba. Los pedidos son los reales de OpenHands (pedidos/paso1.json y paso2.json), con sus mismas opciones.

Uso:  python3 medir.py [--ollama http://100.92.153.59:11435] [--salida resultados/<fecha>]
Solo biblioteca estándar.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

AQUI = Path(__file__).resolve().parent
RUTAS = {("GET", "/api/version"), ("GET", "/api/tags"), ("GET", "/api/ps"),
         ("POST", "/api/show"), ("POST", "/api/chat")}
TIMEOUT_CHAT = 300   # el mismo que usa el agente (LLM_TIMEOUT de smoke.py)
SEMILLAS = (11, 12, 13)


class Ollama:
    def __init__(self, url: str, modelos: set[str]):
        self.url, self.modelos = url.rstrip("/"), modelos

    def pedir(self, metodo: str, ruta: str, cuerpo: dict | None = None, timeout: float = 30):
        if (metodo, ruta) not in RUTAS:
            raise ValueError(f"ruta no permitida: {metodo} {ruta}")
        if cuerpo is not None and cuerpo.get("model") not in self.modelos:
            raise ValueError(f"modelo no permitido: {cuerpo.get('model')}")
        datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
        req = urllib.request.Request(self.url + ruta, data=datos, method=metodo,
                                     headers={"Content-Type": "application/json"})
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read()), round(time.monotonic() - t0, 2)
        except urllib.error.HTTPError as e:
            return e.code, {"error": e.read().decode(errors="replace")[:2000]}, round(time.monotonic() - t0, 2)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return None, {"error": repr(e)}, round(time.monotonic() - t0, 2)


def seg(ns) -> float | None:
    return round(ns / 1e9, 2) if isinstance(ns, (int, float)) else None


def resumen_show(d: dict) -> dict:
    info = d.get("model_info") or {}
    return {
        "capabilities": d.get("capabilities"),
        "details": d.get("details"),
        "parameters": d.get("parameters"),
        "context_length": next((v for k, v in info.items() if k.endswith(".context_length")), None),
        "model_info_arquitectura": {k: v for k, v in info.items()
                                    if k.endswith((".block_count", ".attention.head_count", ".attention.head_count_kv",
                                                   ".attention.key_length", ".attention.value_length",
                                                   ".embedding_length", ".attention.sliding_window"))
                                    or k in ("general.architecture", "general.parameter_count")},
        "plantilla_menciona_tools": ".Tools" in (d.get("template") or ""),
        "largo_plantilla": len(d.get("template") or ""),
        "modified_at": d.get("modified_at"),
    }


def resumen_ps(d: dict) -> list[dict]:
    return [{k: m.get(k) for k in ("name", "digest", "size", "size_vram", "context_length", "expires_at")}
            | {"fraccion_en_vram": round(m["size_vram"] / m["size"], 3) if m.get("size") else None}
            for m in (d or {}).get("models", [])]


def llamada(ol: Ollama, modelo: str, pedido: dict, semilla: int, con_think: bool) -> dict:
    cuerpo = {"model": modelo, "messages": pedido["messages"], "stream": False,
              "options": {**pedido["options"], "seed": semilla}}
    if con_think:
        cuerpo["think"] = False    # lo que manda el SDK con reasoning_effort="none"
    estado, d, reloj = ol.pedir("POST", "/api/chat", cuerpo, timeout=TIMEOUT_CHAT)
    msg = d.get("message") or {}
    eval_s, prompt_s = seg(d.get("eval_duration")), seg(d.get("prompt_eval_duration"))
    return {
        "paso": pedido["paso"], "semilla": semilla, "con_think_false": con_think, "http": estado,
        "error": d.get("error"), "segundos_reloj": reloj, "done_reason": d.get("done_reason"),
        "load_s": seg(d.get("load_duration")), "prompt_eval_s": prompt_s, "eval_s": eval_s,
        "total_s": seg(d.get("total_duration")),
        "prompt_eval_count": d.get("prompt_eval_count"), "eval_count": d.get("eval_count"),
        "tokens_por_s_generacion": round(d["eval_count"] / eval_s, 1) if eval_s and d.get("eval_count") else None,
        "tokens_por_s_prompt": (round(d["prompt_eval_count"] / prompt_s, 1)
                                if prompt_s and d.get("prompt_eval_count") else None),
        "num_ctx": pedido["options"].get("num_ctx"),
        "content": msg.get("content"), "thinking": msg.get("thinking"), "tool_calls": msg.get("tool_calls"),
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--ollama", default="http://100.92.153.59:11435")
    p.add_argument("--modelo", default="gemma4:12b")
    p.add_argument("--qwen", default="qwen2.5-coder:7b")
    p.add_argument("--salida", type=Path, default=AQUI / "resultados" / datetime.now().strftime("%Y%m%d-%H%M%S"))
    p.add_argument("--sin-restaurar", action="store_true", help="no recargar Qwen al final")
    a = p.parse_args()
    a.salida.mkdir(parents=True, exist_ok=False)
    ol = Ollama(a.ollama, {a.modelo, a.qwen})
    pedidos = [json.loads((AQUI / "pedidos" / f"paso{k}.json").read_text(encoding="utf-8")) for k in (1, 2)]
    r: dict = {"inicio": datetime.now().astimezone().isoformat(timespec="seconds"), "ollama": a.ollama,
               "modelo": a.modelo, "qwen": a.qwen, "semillas": SEMILLAS, "timeout_chat_s": TIMEOUT_CHAT}

    def guardar():
        (a.salida / "resultados.json").write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")

    print("== Inventario (solo lectura)", flush=True)
    r["version"] = ol.pedir("GET", "/api/version")[1]
    tags = ol.pedir("GET", "/api/tags")[1]
    r["tags"] = [{k: m.get(k) for k in ("name", "digest", "size", "details")}
                 for m in tags.get("models", []) if m.get("name") in (a.modelo, a.qwen)]
    for nombre, clave in ((a.modelo, "show_gemma"), (a.qwen, "show_qwen")):
        estado, d, _ = ol.pedir("POST", "/api/show", {"model": nombre})
        r[clave] = resumen_show(d) if estado == 200 else {"http": estado, "error": d.get("error")}
    r["ps_antes"] = resumen_ps(ol.pedir("GET", "/api/ps")[1])
    guardar()
    if not isinstance(r["show_gemma"].get("capabilities"), list):
        print(f"   {a.modelo} no está disponible: {r['show_gemma']}")
        return 1

    print(f"== Pedidos reales de OpenHands a {a.modelo} (2 pasos × {len(SEMILLAS)} semillas)", flush=True)
    r["llamadas"], con_think = [], True
    for pedido in pedidos:
        for s in SEMILLAS:
            x = llamada(ol, a.modelo, pedido, s, con_think)
            if x["http"] == 400 and con_think and "think" in (x["error"] or "").lower():
                # El SDK manda think:false; si Ollama lo rechaza, se registra y se sigue sin el campo.
                r["think_false_rechazado"] = x["error"]
                r["llamadas"].append(x)
                con_think = False
                x = llamada(ol, a.modelo, pedido, s, con_think)
            r["llamadas"].append(x)
            print(f"   paso {x['paso']} semilla {s}: http={x['http']} {x['segundos_reloj']} s "
                  f"(carga {x['load_s']} s) prompt={x['prompt_eval_count']} gen={x['eval_count']} "
                  f"fin={x['done_reason']}", flush=True)
            if "ps_tras_carga" not in r:   # con el modelo recién cargado
                r["ps_tras_carga"] = resumen_ps(ol.pedir("GET", "/api/ps")[1])
            guardar()

    if not a.sin_restaurar:
        print(f"== Vuelta a {a.qwen} (1 token, num_ctx 16384) para medir el cambio de modelo", flush=True)
        estado, d, reloj = ol.pedir("POST", "/api/chat", {
            "model": a.qwen, "messages": [{"role": "user", "content": "ok"}], "stream": False,
            "options": {"num_predict": 1, "num_ctx": 16384}}, timeout=TIMEOUT_CHAT)
        r["vuelta_a_qwen"] = {"http": estado, "error": d.get("error"), "segundos_reloj": reloj,
                              "load_s": seg(d.get("load_duration"))}
    r["ps_final"] = resumen_ps(ol.pedir("GET", "/api/ps")[1])
    r["fin"] = datetime.now().astimezone().isoformat(timespec="seconds")
    guardar()
    print(f"== Listo: {a.salida / 'resultados.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
