#!/usr/bin/env bash
# Diagnóstico de SOLO LECTURA para OH-DUO-05: comprueba, en el SDK instalado en la VivoBook, los hechos de los
# que depende la recomendación. No escribe nada, no hace inferencias ni toca servicios.
# Uso: bash diagnostico_vivobook.sh [ruta del venv, por omisión ~/oh-smoke/venv]
set -u
VENV="${1:-$HOME/oh-smoke/venv}"
echo "== venv: $VENV"
"$VENV/bin/python" --version
command -v bwrap >/dev/null && bwrap --version
for f in ~/oh-duo-01/ejecutor/agente.py ~/oh-duo-01/ejecutor/correr.sh; do
    [ -f "$f" ] && sha256sum "$f"
done
OPENHANDS_SUPPRESS_BANNER=1 LITELLM_LOCAL_MODEL_COST_MAP=True "$VENV/bin/python" -I - <<'EOF'
import importlib.metadata as md, inspect
from openhands.sdk.conversation.impl.local_conversation import LocalConversation
from openhands.sdk.conversation import state as st
from openhands.sdk.event import MessageEvent
from openhands.sdk import Agent
ok = lambda c: "OK " if c else "NO "
v = md.version("openhands-sdk")
print(f"openhands-sdk {v} / openhands-tools {md.version('openhands-tools')}")
envio = inspect.getsource(LocalConversation.send_message)
corrida = inspect.getsource(LocalConversation._run)
checks = {
    "versión 1.58.1 (la verificada)": v == "1.58.1",
    "send_message(message, sender=...) existe": "sender" in inspect.signature(LocalConversation.send_message).parameters,
    "send_message toma el candado del estado (with self._state)": "with self._state" in envio,
    "send_message vuelve FINISHED/STUCK a IDLE (riesgo tras finish)": "ConversationExecutionStatus.IDLE" in envio,
    "el candado del estado es FIFOLock (sin inanición)": "FIFOLock" in inspect.getsource(st),
    "run() síncrono retiene el candado durante agent.step": "_step_holds_state_lock = True" in corrida,
    "el contador de iteraciones es local a cada run() (pause+run lo reinicia)": "iteration = 0" in corrida,
    "MaxIterationsReached se emite al llegar a max_iteration_per_run": "MaxIterationsReached" in corrida,
    "sender NO se envía al LLM (to_llm_message lo ignora)": "sender" not in inspect.getsource(MessageEvent.to_llm_message),
    "Agent sin condensador por omisión (el mensaje no se resume)": Agent.model_fields["condenser"].default is None,
}
for k, c in checks.items():
    print(f"  {ok(c)} {k}")
print("\nSi alguna línea dice NO, el diagnóstico de OH-DUO-05 debe revisarse para esta versión antes de implementar.")
EOF
