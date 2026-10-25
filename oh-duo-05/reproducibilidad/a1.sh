#!/usr/bin/env bash
# Experimento A1 (DIAGNOSTICO.md §6): reproducibilidad de Qwen real. Tres controles independientes de P1, semilla
# 11, temperatura 0,2, máximo 30 iteraciones, SIN intervención (la cadena corre sin --enviar: decide, no envía).
#
#   bash a1.sh --simulado                              ensayo completo sin inferencia (lo usa validar.sh)
#   bash a1.sh --real --confirmo-inferencia-real       valida, comprueba, pide escribir A1 y ejecuta
#
# Antes de CADA corrida (reproducibilidad/ollama_estado.py preparar):
#   1. observa Ollama --ventana segundos: si hay otro modelo cargado, si el vencimiento de un modelo se renueva
#      (alguien lo usa), si hay conexiones o uso de GPU (cuando Ollama es local) o procesos de OH-DUO → ABORTA;
#   2. descarga SOLO qwen2.5-coder:7b (keep_alive 0, sin prompt: no genera) y solo si está cargado;
#   3. verifica que no quede ningún modelo cargado: las tres corridas empiezan en frío y con la caché vacía.
# Después de cada corrida registra si apareció otro modelo (corrida contaminada → A1 no concluyente).
# El SDK registra cada pedido y respuesta cruda (REGISTRO_LLM=1 → estado/llm/). Nada anterior se borra ni se
# sobrescribe: corridas/a1-p1-s11-<etiqueta>-rN e informes/a1-p1-s11-<etiqueta>/ son nuevas.
set -euo pipefail
AQUI="$(cd "$(dirname "$0")/.." && pwd)"
MODO="" CONFIRMA=0 VALIDAR=1 VENTANA=20 N=3 ETIQUETA="$(date +%Y%m%d-%H%M%S)"
while [ $# -gt 0 ]; do
    case "$1" in
        --simulado) MODO=simulado ;;
        --real) MODO=real ;;
        --confirmo-inferencia-real) CONFIRMA=1 ;;
        --sin-validar) VALIDAR=0 ;;
        --ventana) VENTANA="$2"; shift ;;
        --etiqueta) ETIQUETA="$2"; shift ;;
        *) echo "opción desconocida: $1"; exit 2 ;;
    esac
    shift
done
if [ "$MODO" = "" ] || { [ "$MODO" = real ] && [ "$CONFIRMA" != 1 ]; }; then
    sed -n '2,6p' "$0"; echo; echo "Para inferencia real hacen falta --real y --confirmo-inferencia-real."; exit 2
fi

# Condiciones fijas, iguales en las tres corridas (no se toman del entorno).
export MAX_ITER=30 MAX_SEG=900 GRACIA=60 NUM_CTX=16384 REGISTRO_LLM=1
unset MODELO
SEMILLA=11
export OH_SMOKE="${OH_SMOKE:-$HOME/oh-smoke}" OH_SUPERVISOR="${OH_SUPERVISOR:-$HOME/oh-supervisor}"
VENV="${VENV:-$OH_SMOKE/venv}"
NOMBRE="a1-p1-s$SEMILLA-$ETIQUETA"

if [ "$MODO" = simulado ]; then
    BASE="$(mktemp -d /tmp/oh5-a1.XXXXXX)"
    CORRIDAS="$BASE/corridas" INFORMES="${OH5_INFORME:-$BASE/informes}"
    python3 -I "$AQUI/tests/simulado.py" "$BASE/puerto" 0.1 estado &
    SIM=$!
    trap 'kill $SIM 2>/dev/null || true' EXIT
    for _ in $(seq 50); do [ -s "$BASE/puerto" ] && break; sleep 0.1; done
    export OLLAMA_URL="http://127.0.0.1:$(cat "$BASE/puerto")"
    VENTANA=1
    SIMULADO=(--simulado)     # el simulado no es el Ollama de esta máquina: sin señales de su GPU ni sus conexiones
else
    CORRIDAS="$AQUI/corridas" INFORMES="$AQUI/informes"
    export OLLAMA_URL="${OLLAMA_URL:-http://100.92.153.59:11435}"
    SIMULADO=()
fi
DEST="$INFORMES/$NOMBRE"
fallo() { echo "ABORTADO: $*"; echo "Registros (se conservan): $DEST"; exit 1; }
for k in $(seq $N); do [ -e "$CORRIDAS/$NOMBRE-r$k" ] && fallo "ya existe $CORRIDAS/$NOMBRE-r$k"; done
[ -e "$DEST" ] && fallo "ya existe $DEST"
mkdir -p "$DEST/estado_ollama"
exec > >(tee "$DEST/a1.log") 2>&1
echo "== Experimento A1 · modo $MODO · $NOMBRE · $(date -Is) · $(hostname)"

# ---- 0. validación completa (sin inferencia) antes de tocar la GPU
if [ "$MODO" = real ] && [ "$VALIDAR" = 1 ]; then
    echo; echo "== 0. validar.sh"
    bash "$AQUI/validar.sh" > "$DEST/validar.log" 2>&1 || { tail -n 15 "$DEST/validar.log"; fallo "validar.sh no pasó"; }
    tail -n 1 "$DEST/validar.log"
fi

# ---- 1. comprobaciones previas
echo; echo "== 1. comprobaciones previas"
[ -x "$VENV/bin/python" ] || fallo "falta $VENV"
[ -f "$OH_SUPERVISOR/supervisor.py" ] || fallo "falta $OH_SUPERVISOR/supervisor.py"
pgrep -f "/correr\.sh" > /dev/null && fallo "hay un correr.sh en ejecución"
curl -sSf -m 5 "$OLLAMA_URL/api/version" > "$DEST/ollama_version.json" || fallo "Ollama no responde en $OLLAMA_URL"
if [ "$MODO" = real ]; then
    curl -sSf -m 5 "$OLLAMA_URL/api/tags" | grep -q '"qwen2.5-coder:7b"' || fallo "qwen2.5-coder:7b no está en Ollama"
fi
python3 -I "$AQUI/reproducibilidad/ollama_estado.py" inactivo --url "$OLLAMA_URL" --ventana "$VENTANA" "${SIMULADO[@]}" \
    --salida "$DEST/estado_ollama/inicio.json" || fallo "Ollama está en uso (ver arriba); no se descarga nada"

python3 -I - "$AQUI" "$DEST/experimento.json" "$MODO" "$SEMILLA" "$OLLAMA_URL" "$NOMBRE" "$N" <<'EOF'
import hashlib, json, os, sys
from pathlib import Path
aqui, salida, modo, semilla, url, nombre, n = sys.argv[1:]
aqui = Path(aqui)
def raiz(m):
    return next((p for p in (aqui.parent / m, Path.home() / m) if p.is_dir()), None)
archivos = [aqui / "ejecutor" / f for f in ("correr.sh", "agente.py", "puente.py")] + \
    [aqui / f for f in ("ejecutar.py", "cadena.py", "reproducibilidad/ollama_estado.py", "reproducibilidad/comparar_a1.py")] + \
    [raiz("oh-duo-01") / f for f in ("duo.py", "roles/qwen_minimo.txt", "banco/P1/tarea/intervalos.py", "banco/P1/tarea/test_intervalos.py")] + \
    [raiz("oh-duo-02") / "umbrales.json", raiz("oh-duo-03") / "observador.py", raiz("oh-duo-04") / "coordinador.json"]
json.dump({"experimento": "A1 reproducibilidad", "modo": modo, "problema": "P1", "modelo": "qwen2.5-coder:7b",
           "semilla": int(semilla), "temperatura": 0.2, "ollama": url, "coordinador": "observa y decide, no envía",
           "limites": {k: os.environ[k] for k in ("MAX_ITER", "MAX_SEG", "GRACIA", "NUM_CTX", "REGISTRO_LLM")},
           "arranque": "en frío: modelo descargado y sin otros modelos cargados antes de cada corrida",
           "corridas": [f"{nombre}-r{k}" for k in range(1, int(n) + 1)],
           "huellas": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in archivos}},
          open(salida, "w"), ensure_ascii=False, indent=1)
EOF

# ---- 2. confirmación explícita (solo real)
if [ "$MODO" = real ]; then
    echo; echo "Se van a ejecutar $N corridas REALES de Qwen (≈ hasta 16 min cada una) con:"
    echo "   P1 · qwen2.5-coder:7b · semilla $SEMILLA · temperatura 0,2 · 30 iteraciones · sin intervención"
    echo "   antes de cada una se descarga SOLO qwen2.5-coder:7b, si nadie está usando Ollama."
    [ -t 0 ] || fallo "hace falta una terminal para confirmar"
    read -r -p "Escribí A1 para empezar (cualquier otra cosa cancela): " RESPUESTA
    [ "$RESPUESTA" = "A1" ] || fallo "cancelado por el operador"
fi

# ---- 3. las corridas
for k in $(seq $N); do
    C="$NOMBRE-r$k"
    echo; echo "== 3.$k corrida $C"
    python3 -I "$AQUI/reproducibilidad/ollama_estado.py" preparar --url "$OLLAMA_URL" --ventana "$VENTANA" "${SIMULADO[@]}" \
        --salida "$DEST/estado_ollama/$C-antes.json" || fallo "no se pudo arrancar $C en frío (Ollama en uso o el modelo no se descargó)"
    python3 -I "$AQUI/cadena.py" --corrida "$CORRIDAS/$C" --salida "$INFORMES/$C" --maximo 1 > "$DEST/$C.cadena.log" 2>&1 &
    CAD=$!
    python3 -I "$AQUI/ejecutar.py" --corridas "$CORRIDAS" ejecutar --corrida "$C" --problemas P1 --modelos qwen \
        --semilla "$SEMILLA" --oh-smoke "$OH_SMOKE" --oh-supervisor "$OH_SUPERVISOR" | tee "$DEST/$C.ejecutar.log"
    wait "$CAD" || echo "  aviso: la cadena terminó con error (ver $DEST/$C.cadena.log)"
    python3 -I "$AQUI/reproducibilidad/ollama_estado.py" despues --url "$OLLAMA_URL" --salida "$DEST/estado_ollama/$C-despues.json" \
        || echo "  AVISO: otro modelo apareció durante $C: la corrida queda marcada como contaminada"
    n_llm="$(ls "$CORRIDAS/$C"/ejecuciones/P1__qwen/instancia/resultados/*/estado/llm 2>/dev/null | wc -l)"
    echo "  respuestas crudas registradas: $n_llm"
done

# ---- 4. pruebas ocultas (fuera del sandbox) y comparación de trayectorias
echo; echo "== 4. verificación y comparación"
for k in $(seq $N); do
    python3 -I "$AQUI/ejecutar.py" --corridas "$CORRIDAS" verificar --corrida "$NOMBRE-r$k" > "$DEST/$NOMBRE-r$k.verificar.log"
done
"$VENV/bin/python" -I "$AQUI/reproducibilidad/comparar_a1.py" --experimento "$DEST" --corridas "$CORRIDAS" \
    2> >(grep -v "INFO\|UserWarning\|warnings.warn\|^ \{10,\}" >&2)
echo; echo "== listo. Informe: $DEST/comparacion_a1.md (y .json, a1.log, estado_ollama/, experimento.json)"
