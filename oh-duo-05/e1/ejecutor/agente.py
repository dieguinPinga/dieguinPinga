"""Agente de E1 (e1/E1.md): el de OH-DUO-05 SIN CAMBIOS más el guion de /opt/oh/guion.json, que inserta preguntas en
puntos exactos de la conversación: la primera, justo después de la observación de la acción `tras_accion` (callback
del SDK, sin depender de tiempos); cada una con un tope de acciones antes de pausar. Si el agente responde con texto
el SDK termina el run y se envía la siguiente. Registro: /estado/guion.jsonl. Con un guion sin preguntas el agente
se comporta exactamente como el de OH-DUO-05 (lo verifica la identidad del prefijo con A1).

--- texto original de oh-duo-05:
Agente de OH-DUO-05: el de OH-DUO-01 más el puente de mensajes (/opt/oh/puente.py), que entrega durante el
mismo run() los mensajes del buzón /buzon/mensajes.jsonl (solo lectura). Sin pausas, sin reinicios: el presupuesto
de iteraciones es el mismo. Registro del puente: /estado/entregas.jsonl.

--- texto original de oh-duo-01:
Agente de OH-DUO-01: oh-lab/ejecutor/agente.py más el texto del rol (/opt/oh/enfoque.txt) al final del pedido.

Mismo LLM, herramientas, conversación, plazos y lectura de eventos para los dos modelos; el modelo llega en
OH_MODELO y el rol en enfoque.txt. El rol cambia el método, no la información: los dos reciben el mismo código,
las mismas pruebas visibles y el mismo pedido base.

Corre DENTRO del sandbox (lo lanza correr.sh). Montajes:
  /work    copia de tarea/ en resultados/<corrida>/work
  /estado  eventos y métricas de esta corrida
  /sock    socket Unix del puente hacia Ollama
No hay red: Ollama se alcanza solo por relay.interno → /sock/ollama.sock → relay.py host.

Este proceso y los comandos del agente comparten sandbox y usuario, así que acá solo se registra lo que dicen
los eventos del SDK. Los criterios que dependen del código (cambios, pruebas visibles y ocultas) los evalúa
correr.sh afuera, sobre una copia, en otro sandbox.
"""
import json
import os
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
MODULO = os.environ["OH_MODULO"]
CODIGO, PRUEBA = f"{MODULO}.py", f"test_{MODULO}.py"
GRACIA = int(os.environ["OH_GRACIA"])
# Registro de cada pedido y respuesta cruda del modelo en /estado/llm/ (lo usa el experimento A1). No cambia
# lo que se le envía al modelo; por omisión está apagado.
REGISTRO_LLM = os.environ.get("OH_REGISTRO_LLM", "0") == "1"
WORK, ESTADO = Path("/work"), Path("/estado")
# Un pedido bloqueado dura como mucho LLM_TIMEOUT × (1 + LLM_REINTENTOS) más la espera entre intentos (8–64 s).
LLM_TIMEOUT, LLM_REINTENTOS = 300, 1

TAREA = (
    f"The repository in the current directory contains {CODIGO} and its tests in {PRUEBA}.\n"
    "1. Run the tests with: python3 -m unittest -v\n"
    f"2. Find the cause of any failure and fix it in {CODIGO}.\n"
    f"3. Do NOT modify {PRUEBA}.\n"
    "4. Run the tests again to confirm they pass, then call finish."
)
ENFOQUE = Path("/opt/oh/enfoque.txt").read_text(encoding="utf-8").strip().replace("{CODIGO}", CODIGO)
TAREA = f"{TAREA}\n\n{ENFOQUE}"


# Mensajes que el SDK reinyecta como «user» cuando la llamada por prompt está mal formada.
FORMATO_INVALIDO = ("for function '", "not found in available tools", "is expected to be")


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
    timeout=LLM_TIMEOUT,
    num_retries=LLM_REINTENTOS,
    log_completions=REGISTRO_LLM,
    log_completions_folder=str(ESTADO / "llm"),
)
agent = Agent(
    llm=llm,
    tools=[Tool(name=TerminalTool.name, params={"terminal_type": "subprocess"}),
           Tool(name=FileEditorTool.name)],
    include_default_tools=["FinishTool"],
)

# ---- guion de E1 (lo único nuevo respecto de oh-duo-05/ejecutor/agente.py)
GUION = json.loads(Path("/opt/oh/guion.json").read_text(encoding="utf-8"))
SONDAS = GUION.get("sondas") or []
guion = {"actual": None, "acciones": 0, "acciones_desde": 0, "registro": []}
SIN_TIEMPO = threading.Event()          # vencido MAX_SEG: no se envían más preguntas


def anotar_guion(**d):
    guion["registro"].append({"momento": time.time(), **d})
    with open(ESTADO / "guion.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(guion["registro"][-1], ensure_ascii=False) + "\n")


def enviar_sonda(k):
    s = SONDAS[k]
    guion["actual"], guion["acciones_desde"] = k, 0
    conv.send_message(s["texto"], sender=f"e1:{s['id']}")
    anotar_guion(sonda=s["id"], evento=len(conv.state.events) - 1, tras_accion=guion["acciones"] - 1)


def al_evento(e):
    if isinstance(e, ActionEvent):
        guion["acciones"] += 1
        if guion["actual"] is not None:
            guion["acciones_desde"] += 1
    elif isinstance(e, (ObservationEvent, AgentErrorEvent)):
        if guion["actual"] is None and SONDAS and guion["acciones"] == GUION["tras_accion"] + 1 \
                and not guion["registro"]:
            enviar_sonda(0)                                   # justo después de la observación de esa acción
        elif guion["actual"] is not None and guion["acciones_desde"] >= SONDAS[guion["actual"]]["max_acciones"]:
            anotar_guion(sonda=SONDAS[guion["actual"]]["id"], pausa_por_tope=guion["acciones_desde"])
            conv.pause()


conv = Conversation(agent=agent, workspace=str(WORK), persistence_dir=str(ESTADO / "conversacion"),
                    max_iteration_per_run=MAX_ITER, stuck_detection=True, visualizer=None, callbacks=[al_evento])
conv.send_message(TAREA)

import puente  # noqa: E402   (/opt/oh/puente.py, en solo lectura)

canal = puente.Puente(conv, Path("/buzon/mensajes.jsonl"), ESTADO / "entregas.jsonl")
canal.iniciar()

# Plazo: a los MAX_SEG se pide pausa (el SDK la aplica al terminar el paso en curso). Si el paso sigue
# bloqueado (pedido al LLM o comando largo) GRACIA segundos después, se registra lo que haya y se sale igual.
error = []


def correr():
    try:
        conv.run()
        # las preguntas siguientes de E1: se envían cuando el run anterior terminó (texto) o se pausó (tope)
        while guion["actual"] is not None and guion["actual"] + 1 < len(SONDAS) and not SIN_TIEMPO.is_set():
            anotar_guion(sonda=SONDAS[guion["actual"]]["id"], fin=str(conv.state.execution_status.value))
            enviar_sonda(guion["actual"] + 1)
            conv.run()
        if guion["actual"] is not None:
            anotar_guion(sonda=SONDAS[guion["actual"]]["id"], fin=str(conv.state.execution_status.value))
        elif SONDAS:
            anotar_guion(no_alcanzado=GUION["tras_accion"], fin=str(conv.state.execution_status.value))
    except Exception as e:  # se registra; la evaluación sigue
        error.append(repr(e))


inicio = time.time()
hilo = threading.Thread(target=correr, daemon=True)
hilo.start()
hilo.join(MAX_SEG)
vencio_tiempo = hilo.is_alive()
if vencio_tiempo:
    # pause() toma el candado del estado, que el paso en curso retiene mientras espera al LLM: se pide en
    # otro hilo para no quedar bloqueados también acá.
    SIN_TIEMPO.set()
    threading.Thread(target=conv.pause, daemon=True).start()
    hilo.join(GRACIA)
corte_forzado = hilo.is_alive()
canal.detener()   # antes de leer los eventos; registra las últimas acciones posteriores
segundos = round(time.time() - inicio, 1)

# --- Lectura de eventos (solo lo que el SDK registró) ---
eventos, acciones = [], []
corrio_pruebas = vio_fallo = edito_codigo = termino_con_finish = False
mensajes_sin_herramienta = errores_formato = 0
for ev in list(conv.state.events):
    fila = {"tipo": type(ev).__name__, "fuente": getattr(ev, "source", None)}
    if isinstance(ev, ActionEvent):
        accion = ev.action.model_dump(mode="json") if ev.action else None
        fila.update(herramienta=ev.tool_name, accion=accion, pensamiento=[t.text for t in ev.thought])
        acciones.append(ev.tool_name)
        if ev.tool_name == TerminalTool.name and accion and "unittest" in (accion.get("command") or ""):
            corrio_pruebas = True
        if ev.tool_name == FileEditorTool.name and accion and Path(str(accion.get("path", ""))).name == CODIGO \
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

m = conv.conversation_stats.get_combined_metrics()
uso = m.accumulated_token_usage
resultado = {
    "modelo": MODELO, "semilla": SEMILLA, "num_ctx": NUM_CTX, "max_iter": MAX_ITER, "max_seg": MAX_SEG,
    "estado_final": str(conv.state.execution_status.value), "vencio_tiempo": vencio_tiempo,
    "corte_forzado": corte_forzado, "error": error[0] if error else None,
    "segundos": segundos, "llamadas_llm": len(m.token_usages),
    "tokens_prompt": uso.prompt_tokens if uso else None, "tokens_generados": uso.completion_tokens if uso else None,
    "acciones": acciones,
    "criterios": {
        "1_llamada_a_herramienta_valida": any(a != "finish" for a in acciones),
        "2_corrio_pruebas_y_vio_fallo": corrio_pruebas and vio_fallo,
    },
    "termino_con_finish": termino_con_finish,
    "intervenciones": canal.resumen(),
    "guion": {"condicion": GUION.get("condicion"), "tras_accion": GUION.get("tras_accion"), "registro": guion["registro"]},
    # Un mensaje de texto del agente (p. ej. JSON en vez de <function=...>) hace que el SDK dé la tarea por terminada.
    "detalle": {"edito_por_file_editor": edito_codigo, "mensajes_de_texto_del_agente": mensajes_sin_herramienta,
                "correcciones_de_formato_del_sdk": errores_formato},
}
(ESTADO / "resultado.json").write_text(json.dumps(resultado, ensure_ascii=False, indent=1), encoding="utf-8")
print(json.dumps(resultado, ensure_ascii=False, indent=1), flush=True)
os._exit(0)  # no esperar a un hilo bloqueado
