#!/usr/bin/env bash
# Diagnóstico del primer ensayo real de OH-DUO-05, SOLO LECTURA. Uso:
#   bash ~/oh-duo-05/diagnostico/extraer_vivobook.sh [informes/qwen-p1-s11-<etiqueta>]   (por omisión, el último)
# No hace inferencias, no carga ni descarga modelos, no cambia la configuración de Ollama y no escribe en las
# corridas. Las únicas consultas a Ollama son GET /api/version y GET /api/ps. Escribe en
# <protocolo>/diagnostico/: diagnostico.{md,json} (analizar_par.py), sistema.txt, ollama_journal.txt y un
# paquete <protocolo>-diagnostico.tar.gz con los registros de los dos brazos (sin work/ ni el venv).
set -u
AQUI="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${VENV:-$HOME/oh-smoke/venv}"
CORRIDAS="${CORRIDAS:-$AQUI/corridas}"
OLLAMA_URL="${OLLAMA_URL:-http://100.92.153.59:11435}"
PROT="${1:-$(ls -dt "$AQUI"/informes/qwen-p1-s*-[0-9]* 2>/dev/null | grep -v validacion | head -1)}"
[ -f "$PROT/protocolo.json" ] || { echo "no encuentro protocolo.json en '$PROT'"; exit 1; }
PROT="$(cd "$PROT" && pwd)"
D="$PROT/diagnostico"
mkdir -p "$D"
read -r CTRL INT < <(python3 -I -c 'import json,sys; b=json.load(open(sys.argv[1]))["brazos"]; print(b["control"]["corrida"], b["intervenida"]["corrida"])' "$PROT/protocolo.json")
echo "== protocolo: $PROT"
echo "   control: $CTRL · intervenida: $INT"

# ---- 1. análisis de los registros (eventos, relay, métricas del SDK, detector stuck reproducido)
"$VENV/bin/python" -I "$AQUI/diagnostico/analizar_par.py" --protocolo "$PROT" --corridas "$CORRIDAS" --salida "$D" \
    2> >(grep -v "INFO\|UserWarning\|warnings.warn\|^ \{10,\}" >&2)

# ---- 2. configuración y estado de Ollama y de la GPU (solo lectura)
{
    echo "### fecha: $(date -Is) · host: $(hostname)"
    echo; echo "### Ollama en $OLLAMA_URL"
    curl -sS -m 5 "$OLLAMA_URL/api/version"; echo
    echo "--- /api/ps ahora (qué está cargado, VRAM, contexto, vencimiento)"
    curl -sS -m 5 "$OLLAMA_URL/api/ps"; echo
    echo; echo "### ¿Ollama corre en esta máquina?"
    H="${OLLAMA_URL#http://}"; H="${H%%:*}"; P="${OLLAMA_URL##*:}"
    ip -o addr 2>/dev/null | grep -qw "$H" && echo "la IP $H es local" || echo "la IP $H NO es de esta máquina"
    ss -ltnp 2>/dev/null | grep -E ":($P|11434)\b" || echo "(sin escuchas en $P/11434 visibles para este usuario)"
    command -v ollama >/dev/null && { echo "ollama --version:"; ollama --version 2>&1; }
    echo; echo "### servicio ollama (variables que afectan la reproducibilidad)"
    systemctl show ollama --property=Environment,ExecStart,ActiveEnterTimestamp --no-pager 2>/dev/null || echo "(sin servicio de sistema ollama)"
    systemctl cat ollama --no-pager 2>/dev/null | grep -E "Environment|ExecStart" || true
    systemctl --user show ollama --property=Environment --no-pager 2>/dev/null | grep -v "^Environment=$" || true
    for v in OLLAMA_NUM_PARALLEL OLLAMA_MAX_LOADED_MODELS OLLAMA_FLASH_ATTENTION OLLAMA_KV_CACHE_TYPE OLLAMA_KEEP_ALIVE \
             OLLAMA_CONTEXT_LENGTH OLLAMA_NEW_ENGINE OLLAMA_SCHED_SPREAD OLLAMA_GPU_OVERHEAD OLLAMA_HOST OLLAMA_DEBUG; do
        p="$(pgrep -x ollama | head -1)"
        [ -n "$p" ] && [ -r "/proc/$p/environ" ] && tr '\0' '\n' < "/proc/$p/environ" | grep "^$v=" || true
    done
    echo; echo "### GPU"
    command -v nvidia-smi >/dev/null && nvidia-smi --query-gpu=name,driver_version,memory.used,memory.total,temperature.gpu,clocks.sm --format=csv || echo "(sin nvidia-smi)"
    for f in gpu_antes.csv gpu_despues.csv ollama_version.json; do [ -f "$PROT/$f" ] && { echo "--- $f (del protocolo)"; cat "$PROT/$f"; }; done
    echo; echo "### huellas registradas por el protocolo frente a las actuales"
    python3 -I - "$PROT/protocolo.json" <<'EOF'
import hashlib, json, sys
from pathlib import Path
h = json.load(open(sys.argv[1]))["huellas"]
cambios = [p for p, s in h.items() if not Path(p).is_file() or hashlib.sha256(Path(p).read_bytes()).hexdigest() != s]
print("sin cambios desde el ensayo" if not cambios else f"CAMBIARON: {cambios}")
EOF
} > "$D/sistema.txt" 2>&1
echo; echo "== sistema: $D/sistema.txt"; cat "$D/sistema.txt"

# ---- 3. registro de Ollama durante el ensayo (si es local y el usuario puede leerlo)
DESDE="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["inicio"][:19].replace("T"," "))' "$CORRIDAS/$CTRL/ejecuciones/P1__qwen/ejecucion.json" 2>/dev/null)"
HASTA="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["fin"][:19].replace("T"," "))' "$CORRIDAS/$INT/ejecuciones/P1__qwen/ejecucion.json" 2>/dev/null)"
if [ -n "$DESDE" ] && [ -n "$HASTA" ]; then
    { journalctl -u ollama --since "$DESDE" --until "$HASTA" --no-pager 2>&1 || true
      journalctl --user -u ollama --since "$DESDE" --until "$HASTA" --no-pager 2>/dev/null || true; } > "$D/ollama_journal.txt"
    echo; echo "== registro de Ollama entre $DESDE y $HASTA ($(wc -l < "$D/ollama_journal.txt") líneas); lo relevante:"
    grep -iE "load|runner|offload|layers|ctx|parallel|cache|kv|flash|error|evict|unload|expired" "$D/ollama_journal.txt" | head -40
fi

# ---- 4. paquete con los registros (sin work/ ni el venv; nada se modifica)
INFORMES="$(dirname "$PROT")"
PAQ="$(mktemp -d)"
mkdir -p "$PAQ/corridas" "$PAQ/informes"
for c in "$CTRL" "$INT"; do
    ln -s "$CORRIDAS/$c" "$PAQ/corridas/$c"
    [ -d "$INFORMES/$c" ] && ln -s "$INFORMES/$c" "$PAQ/informes/$c"
done
ln -s "$PROT" "$PAQ/informes/$(basename "$PROT")"
tar -chzf "$PROT-diagnostico.tar.gz" -C "$PAQ" --exclude=work --exclude=venv --exclude=eval --exclude=original corridas informes
rm -rf "$PAQ"
echo; echo "== listo. Para compartir: $D/diagnostico.md y $D/sistema.txt (texto), o el paquete $PROT-diagnostico.tar.gz"
