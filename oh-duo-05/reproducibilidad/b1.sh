#!/usr/bin/env bash
# Experimento B1 (B1.md): efecto del coordinador. Corridas de P1 CON intervención (la cadena corre con --enviar),
# comparadas con los controles históricos de A1, que fueron REPRODUCIBLES.
#
#   bash b1.sh --simulado --a1 DIR_A1                              ensayo sin inferencia (lo usa validar.sh)
#   bash b1.sh --real --confirmo-inferencia-real [--a1 DIR_A1]     verifica, valida, pide escribir B1 y ejecuta
#
# Todo lo demás es idéntico a A1 (mismo ejecutor, límites, semilla, registro crudo, arranque en frío y las mismas
# comprobaciones de seguridad de Ollama antes y después de cada corrida). Antes de tocar la GPU,
# reproducibilidad/preparar_b1.py verifica contra A1: veredicto REPRODUCIBLE, mismo código (huella por huella),
# misma versión de Ollama, mismo modelo (digest) y que el coordinador de A1 habría intervenido.
# Por omisión --a1 es el último informes/a1-p1-s11-* que dio REPRODUCIBLE. Nada anterior se borra.
set -euo pipefail
AQUI="$(cd "$(dirname "$0")/.." && pwd)"
MODO="" CONFIRMA=0 VALIDAR=1 VENTANA=20 N=3 ETIQUETA="$(date +%Y%m%d-%H%M%S)" A1=""
while [ $# -gt 0 ]; do
    case "$1" in
        --simulado) MODO=simulado ;;
        --real) MODO=real ;;
        --confirmo-inferencia-real) CONFIRMA=1 ;;
        --sin-validar) VALIDAR=0 ;;
        --ventana) VENTANA="$2"; shift ;;
        --etiqueta) ETIQUETA="$2"; shift ;;
        --a1) A1="$2"; shift ;;
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
NOMBRE="b1-p1-s$SEMILLA-$ETIQUETA"

if [ "$MODO" = simulado ]; then
    BASE="$(mktemp -d /tmp/oh5-b1.XXXXXX)"
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
if [ -z "$A1" ]; then
    A1="$(for d in $(ls -dt "$AQUI"/informes/a1-p1-s11-* 2>/dev/null); do
            python3 -I -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1]))["veredicto"] == "REPRODUCIBLE" else 1)' \
                "$d/comparacion_a1.json" 2>/dev/null && { echo "$d"; break; }; done)"
fi
[ -n "$A1" ] && [ -f "$A1/experimento.json" ] || { echo "ABORTADO: no encuentro el experimento A1 de referencia (--a1)"; exit 1; }
A1="$(cd "$A1" && pwd)"
DEST="$INFORMES/$NOMBRE"
fallo() { echo "ABORTADO: $*"; echo "Registros (se conservan): $DEST"; exit 1; }
for k in $(seq $N); do [ -e "$CORRIDAS/$NOMBRE-r$k" ] && fallo "ya existe $CORRIDAS/$NOMBRE-r$k"; done
[ -e "$DEST" ] && fallo "ya existe $DEST"
mkdir -p "$DEST/estado_ollama"
exec > >(tee "$DEST/b1.log") 2>&1
echo "== Experimento B1 · modo $MODO · $NOMBRE · $(date -Is) · $(hostname)"
echo "   referencia A1: $A1"

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

echo; echo "== 1b. comprobaciones contra A1 (código, Ollama, modelo, decisión del coordinador)"
python3 -I "$AQUI/reproducibilidad/preparar_b1.py" --a1 "$A1" --salida "$DEST/experimento.json" --modo "$MODO" \
    --url "$OLLAMA_URL" --nombre "$NOMBRE" --n "$N" --corridas "$CORRIDAS" \
    || fallo "las condiciones de B1 no son las de A1: el control histórico no serviría"

# ---- 2. confirmación explícita (solo real)
if [ "$MODO" = real ]; then
    echo; echo "Se van a ejecutar $N corridas REALES de Qwen (≈ hasta 16 min cada una) con:"
    echo "   P1 · qwen2.5-coder:7b · semilla $SEMILLA · temperatura 0,2 · 30 iteraciones · CON intervención del coordinador"
    echo "   comparadas con los controles de A1: $(basename "$A1")"
    echo "   antes de cada una se descarga SOLO qwen2.5-coder:7b, si nadie está usando Ollama."
    [ -t 0 ] || fallo "hace falta una terminal para confirmar"
    read -r -p "Escribí B1 para empezar (cualquier otra cosa cancela): " RESPUESTA
    [ "$RESPUESTA" = "B1" ] || fallo "cancelado por el operador"
fi

# ---- 3. las corridas
for k in $(seq $N); do
    C="$NOMBRE-r$k"
    echo; echo "== 3.$k corrida $C"
    python3 -I "$AQUI/reproducibilidad/ollama_estado.py" preparar --url "$OLLAMA_URL" --ventana "$VENTANA" "${SIMULADO[@]}" \
        --salida "$DEST/estado_ollama/$C-antes.json" || fallo "no se pudo arrancar $C en frío (Ollama en uso o el modelo no se descargó)"
    python3 -I "$AQUI/cadena.py" --corrida "$CORRIDAS/$C" --salida "$INFORMES/$C" --maximo 1 --enviar > "$DEST/$C.cadena.log" 2>&1 &
    CAD=$!
    python3 -I "$AQUI/ejecutar.py" --corridas "$CORRIDAS" ejecutar --corrida "$C" --problemas P1 --modelos qwen \
        --semilla "$SEMILLA" --oh-smoke "$OH_SMOKE" --oh-supervisor "$OH_SUPERVISOR" | tee "$DEST/$C.ejecutar.log"
    wait "$CAD" || echo "  aviso: la cadena terminó con error (ver $DEST/$C.cadena.log)"
    python3 -I "$AQUI/reproducibilidad/ollama_estado.py" despues --url "$OLLAMA_URL" --salida "$DEST/estado_ollama/$C-despues.json" \
        || echo "  AVISO: otro modelo apareció durante $C: la corrida queda marcada como contaminada"
    grep -h '"resultado"' "$INFORMES/$C/despachos.jsonl" 2>/dev/null | sed 's/^/  despacho: /' | cut -c1-160 || true
    n_llm="$(ls "$CORRIDAS/$C"/ejecuciones/P1__qwen/instancia/resultados/*/estado/llm 2>/dev/null | wc -l)"
    echo "  respuestas crudas registradas: $n_llm"
done

# ---- 4. pruebas ocultas (fuera del sandbox) y comparación de trayectorias
echo; echo "== 4. verificación y comparación"
for k in $(seq $N); do
    python3 -I "$AQUI/ejecutar.py" --corridas "$CORRIDAS" verificar --corrida "$NOMBRE-r$k" > "$DEST/$NOMBRE-r$k.verificar.log"
done
"$VENV/bin/python" -I "$AQUI/reproducibilidad/comparar_b1.py" --a1 "$A1" --b1 "$DEST" \
    2> >(grep -v "INFO\|UserWarning\|warnings.warn\|^ \{10,\}" >&2)
echo; echo "== listo. Informe: $DEST/comparacion_b1.md (y .json, b1.log, estado_ollama/, experimento.json)"
