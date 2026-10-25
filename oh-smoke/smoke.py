"""Smoke test: el motor de OpenHands, sin agregados, con Qwen en modo de herramientas por prompt.

Corre DENTRO del sandbox (lo lanza correr.sh). Montajes:
  /work    copia de tarea/ (lo único escribible por el agente)
  /estado  eventos, métricas y resultado (escribible; el agente no lo conoce)
  /sock    socket Unix del puente hacia Ollama
No hay red: Ollama se alcanza solo por relay.interno → /sock/ollama.sock → relay.py host.
"""
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, "/opt/oh")
import relay  # noqa: E402

relay.interno("/sock/ollama.sock")

import litellm  # noqa: E402
from pydantic import SecretStr  # noqa: E402

from openhands.sdk import LLM, Agent, Conversation, Tool  # noqa: E402
from openhands.sdk.event import ActionEvent, AgentErrorEvent, MessageEvent, ObservationEvent  # noqa: E402
from openhands.tools.file_editor import FileEditorTool  # noqa: E402
from openhands.tools.terminal import TerminalTool  # noqa: E402

MODELO = os.environ["OH_MODELO"]
SEMILLA = int(os.environ["OH_SEMILLA"])
NUM_CTX = int(os.environ["OH_NUM_CTX"])
MAX_ITER = int(os.environ["OH_MAX_ITER"])
MAX_SEG = int(os.environ["OH_MAX_SEG"])
WORK, ESTADO = Path("/work"), Path("/estado")

TAREA = (
    "The repository in the current directory contains duracion.py and its tests in test_duracion.py.\n"
    "1. Run the tests with: python3 -m unittest -v\n"
    "2. Find the cause of any failure and fix it in duracion.py.\n"
    "3. Do NOT modify test_duracion.py.\n"
    "4. Run the tests again to confirm they pass, then call finish."
)


# Mensajes que el SDK reinyecta como «user» cuando la llamada por prompt está mal formada.
FORMATO_INVALIDO = ("for function '", "not found in available tools", "is expected to be")


def huella(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# num_ctx viaja en cada pedido (options.num_ctx); el puente lo registra para verificarlo.
litellm.OllamaChatConfig(num_ctx=NUM_CTX)
llm = LLM(
    usage_id="agente",
    model=f"ollama_chat/{MODELO}",
    base_url="http://127.0.0.1:11435",
    api_key=SecretStr("ollama"),
    native_tool_calling=False,
    temperature=0.2,
    seed=SEMILLA,
    max_input_tokens=NUM_CTX,
    max_output_tokens=2048,
    reasoning_effort="none",
    # El SDK descarta temperature en modelos que «admiten reasoning_effort»; Qwen 2.5 Coder no razona.
    capability_overrides={"supports_sampling_params": True},
    caching_prompt=False,
    timeout=600,
    num_retries=2,
)
agent = Agent(
    llm=llm,
    tools=[Tool(name=TerminalTool.name, params={"terminal_type": "subprocess"}),
           Tool(name=FileEditorTool.name)],
    include_default_tools=["FinishTool"],
)

huella_pruebas = huella(WORK / "test_duracion.py")
huella_codigo = huella(WORK / "duracion.py")
conv = Conversation(agent=agent, workspace=str(WORK), persistence_dir=str(ESTADO / "conversacion"),
                    max_iteration_per_run=MAX_ITER, stuck_detection=True, visualizer=None)
conv.send_message(TAREA)

vencido = threading.Event()


def vencer():
    vencido.set()
    conv.pause()


reloj = threading.Timer(MAX_SEG, vencer)
reloj.start()
inicio = time.time()
error = None
try:
    conv.run()
except Exception as e:  # se registra; la evaluación sigue
    error = repr(e)
finally:
    reloj.cancel()
segundos = round(time.time() - inicio, 1)

# --- Lectura de eventos (solo lo que el SDK registró) ---
eventos, acciones = [], []
corrio_pruebas = vio_fallo = edito_codigo = termino_con_finish = False
mensajes_sin_herramienta = errores_formato = 0
for ev in conv.state.events:
    fila = {"tipo": type(ev).__name__, "fuente": getattr(ev, "source", None)}
    if isinstance(ev, ActionEvent):
        accion = ev.action.model_dump(mode="json") if ev.action else None
        fila.update(herramienta=ev.tool_name, accion=accion, pensamiento=[t.text for t in ev.thought])
        acciones.append(ev.tool_name)
        if ev.tool_name == TerminalTool.name and accion and "unittest" in (accion.get("command") or ""):
            corrio_pruebas = True
        if ev.tool_name == FileEditorTool.name and accion and str(accion.get("path", "")).endswith("duracion.py") \
                and accion.get("command") != "view":
            edito_codigo = True
        if ev.tool_name == "finish":
            termino_con_finish = True
    elif isinstance(ev, ObservationEvent):
        texto = json.dumps(ev.observation.model_dump(mode="json"), ensure_ascii=False)
        fila.update(herramienta=ev.tool_name, observacion=texto[:4000])
        if corrio_pruebas and not edito_codigo and ("FAILED" in texto or "AssertionError" in texto):
            vio_fallo = True
    elif isinstance(ev, MessageEvent):
        texto = " ".join(getattr(c, "text", "") for c in ev.llm_message.content)
        fila.update(texto=texto[:4000])
        if ev.source == "agent":
            mensajes_sin_herramienta += 1
        elif ev.source == "user" and any(k in texto for k in FORMATO_INVALIDO):
            errores_formato += 1  # corrección automática del SDK ante una llamada mal formada
    elif isinstance(ev, AgentErrorEvent):
        fila.update(error=ev.error[:4000])
    eventos.append(fila)
(ESTADO / "eventos.json").write_text(json.dumps(eventos, ensure_ascii=False, indent=1), encoding="utf-8")

# --- Pruebas visibles al final (las corre el arnés, no el agente) ---
visibles = subprocess.run(["/usr/bin/python3", "-B", "-m", "unittest", "-v"], cwd=WORK,
                          capture_output=True, text=True, timeout=60)
(ESTADO / "visibles.txt").write_text(visibles.stdout + visibles.stderr, encoding="utf-8")

m = conv.conversation_stats.get_combined_metrics()
uso = m.accumulated_token_usage
resultado = {
    "modelo": MODELO, "semilla": SEMILLA, "num_ctx": NUM_CTX, "max_iter": MAX_ITER, "max_seg": MAX_SEG,
    "estado_final": str(conv.state.execution_status.value), "vencio_tiempo": vencido.is_set(), "error": error,
    "segundos": segundos, "llamadas_llm": len(m.token_usages),
    "tokens_prompt": uso.prompt_tokens if uso else None, "tokens_generados": uso.completion_tokens if uso else None,
    "acciones": acciones,
    "criterios": {
        "1_llamada_a_herramienta_valida": any(a != "finish" for a in acciones),
        "2_corrio_pruebas_y_vio_fallo": corrio_pruebas and vio_fallo,
        "3_edito_codigo_y_no_las_pruebas": huella(WORK / "duracion.py") != huella_codigo
                                           and huella(WORK / "test_duracion.py") == huella_pruebas,
        "4_visibles_pasan_y_termino_con_finish": visibles.returncode == 0 and termino_con_finish,
    },
    # Un mensaje de texto del agente (p. ej. JSON en vez de <function=...>) hace que el SDK dé la tarea por terminada.
    "detalle": {"edito_por_file_editor": edito_codigo, "mensajes_de_texto_del_agente": mensajes_sin_herramienta,
                "correcciones_de_formato_del_sdk": errores_formato},
}
(ESTADO / "resultado.json").write_text(json.dumps(resultado, ensure_ascii=False, indent=1), encoding="utf-8")
print(json.dumps(resultado, ensure_ascii=False, indent=1))
