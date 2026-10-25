#!/usr/bin/env bash
# Experimento E1 (e1/E1.md): de la solución a la edición. Localiza en qué etapa se rompe la cadena
# comprensión → identificación → acción → aplicación → verificación, reutilizando la trayectoria determinista de A1.
#
#   bash e1.sh --simulado --a1 DIR_A1 [--variante arregla|no_entiende|formula_mal|rechazo_sin_recuperacion|rechazo_con_recuperacion]
#   bash e1.sh --real --confirmo-inferencia-real [--a1 DIR_A1]       valida, comprueba, pide escribir E1 y ejecuta
#
# Condiciones (inferencias reales: 1 pedido directo + 2 corridas cortas que repiten A1 hasta el punto elegido):
#   E1-A0  pedido directo, sin OpenHands: código y pruebas visibles auténticos de P1 → ¿identifica la corrección?
#   E1-AB  A1 idéntico hasta la acción k (código y fallo a la vista) + pregunta A (diagnóstico) + pregunta B (aplicar)
#   E1-C   A1 idéntico hasta la acción j (su edición rechazada real) + las mismas preguntas A y B
# Antes de CADA inferencia: las mismas comprobaciones de Ollama que A1 (nadie lo usa; se descarga solo Qwen; arranque
# en frío) y después, si apareció otro modelo. El código del modelo se ejecuta solo en bwrap. Nada anterior se borra.
set -euo pipefail
AQUI="$(cd "$(dirname "$0")/.." && pwd)"
MODO="" CONFIRMA=0 VALIDAR=1 VENTANA=20 ETIQUETA="$(date +%Y%m%d-%H%M%S)" A1="" VARIANTE=arregla
ARCHIVO_USUARIO="${ARCHIVO_USUARIO:-$HOME/pruebas-qwen/P1_REAL_DIRECTO/intervalos_extraido.py}"
while [ $# -gt 0 ]; do
    case "$1" in
        --simulado) MODO=simulado ;;
        --real) MODO=real ;;
        --confirmo-inferencia-real) CONFIRMA=1 ;;
        --sin-validar) VALIDAR=0 ;;
        --ventana) VENTANA="$2"; shift ;;
        --etiqueta) ETIQUETA="$2"; shift ;;
        --a1) A1="$2"; shift ;;
        --variante) VARIANTE="$2"; shift ;;
        *) echo "opción desconocida: $1"; exit 2 ;;
    esac
    shift
done
if [ "$MODO" = "" ] || { [ "$MODO" = real ] && [ "$CONFIRMA" != 1 ]; }; then
    sed -n '2,6p' "$0"; echo; echo "Para inferencia real hacen falta --real y --confirmo-inferencia-real."; exit 2
fi

# Condiciones fijas, las de A1 (no se toman del entorno).
export MAX_ITER=30 MAX_SEG=900 GRACIA=60 NUM_CTX=16384 REGISTRO_LLM=1
unset MODELO
SEMILLA=11
export OH_SMOKE="${OH_SMOKE:-$HOME/oh-smoke}" OH_SUPERVISOR="${OH_SUPERVISOR:-$HOME/oh-supervisor}"
VENV="${VENV:-$OH_SMOKE/venv}"
NOMBRE="e1-p1-s$SEMILLA-$ETIQUETA"

if [ "$MODO" = simulado ]; then
    BASE="$(mktemp -d /tmp/oh5-e1.XXXXXX)"
    CORRIDAS="$BASE/corridas" INFORMES="${OH5_INFORME:-$BASE/informes}"
    python3 -I "$AQUI/tests/simulado.py" "$BASE/puerto" 0.1 estado "$VARIANTE" &
    SIM=$!
    trap 'kill $SIM 2>/dev/null || true' EXIT
    for _ in $(seq 50); do [ -s "$BASE/puerto" ] && break; sleep 0.1; done
    export OLLAMA_URL="http://127.0.0.1:$(cat "$BASE/puerto")"
    VENTANA=1
    SIMULADO=(--simulado)
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
B1REF="$(ls -dt "$(dirname "$A1")"/b1-p1-s11-* 2>/dev/null | head -1 || true)"
DEST="$INFORMES/$NOMBRE"
fallo() { echo "ABORTADO: $*"; echo "Registros (se conservan): $DEST"; exit 1; }
[ -e "$DEST" ] && fallo "ya existe $DEST"
mkdir -p "$DEST/estado_ollama"
exec > >(tee "$DEST/e1.log") 2>&1
echo "== Experimento E1 · modo $MODO${SIMULADO:+ (variante $VARIANTE)} · $NOMBRE · $(date -Is) · $(hostname)"
echo "   referencia A1: $A1"

if [ "$MODO" = real ] && [ "$VALIDAR" = 1 ]; then
    echo; echo "== 0. validar.sh"
    bash "$AQUI/validar.sh" > "$DEST/validar.log" 2>&1 || { tail -n 15 "$DEST/validar.log"; fallo "validar.sh no pasó"; }
    tail -n 1 "$DEST/validar.log"
fi

echo; echo "== 1. comprobaciones previas"
[ -x "$VENV/bin/python" ] || fallo "falta $VENV"
[ -f "$OH_SUPERVISOR/supervisor.py" ] || fallo "falta $OH_SUPERVISOR/supervisor.py"
command -v bwrap > /dev/null || fallo "falta bwrap"
pgrep -f "/correr\.sh" > /dev/null && fallo "hay un correr.sh en ejecución"
curl -sSf -m 5 "$OLLAMA_URL/api/version" > "$DEST/ollama_version.json" || fallo "Ollama no responde en $OLLAMA_URL"
if [ "$MODO" = real ]; then
    curl -sSf -m 5 "$OLLAMA_URL/api/tags" | grep -q '"qwen2.5-coder:7b"' || fallo "qwen2.5-coder:7b no está en Ollama"
fi
python3 -I "$AQUI/reproducibilidad/ollama_estado.py" inactivo --url "$OLLAMA_URL" --ventana "$VENTANA" "${SIMULADO[@]}" \
    --salida "$DEST/estado_ollama/inicio.json" || fallo "Ollama está en uso (ver arriba); no se descarga nada"
echo "-- plan desde A1"
"$VENV/bin/python" -I "$AQUI/e1/plan.py" --a1 "$A1" --salida "$DEST" || fallo "A1 no permite ninguna condición de E1"
python3 -I "$AQUI/e1/preparar_e1.py" --a1 "$A1" --plan "$DEST/plan.json" --salida "$DEST/experimento.json" --modo "$MODO" \
    --url "$OLLAMA_URL" --nombre "$NOMBRE" --corridas "$CORRIDAS" || fallo "las condiciones no son las de A1"

if [ "$MODO" = real ]; then
    echo; echo "Se van a hacer inferencias REALES con qwen2.5-coder:7b (semilla $SEMILLA, temperatura 0,2):"
    echo "   E1-A0: 1 pedido directo · E1-AB y E1-C: corridas que repiten A1 hasta el punto elegido y hacen 2 preguntas"
    echo "   (topes: $(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["max_acciones"])' "$DEST/plan.json") acciones)."
    echo "   Antes de cada una se descarga SOLO qwen2.5-coder:7b, si nadie está usando Ollama."
    [ -t 0 ] || fallo "hace falta una terminal para confirmar"
    read -r -p "Escribí E1 para empezar (cualquier otra cosa cancela): " RESPUESTA
    [ "$RESPUESTA" = "E1" ] || fallo "cancelado por el operador"
fi

preparar() {
    python3 -I "$AQUI/reproducibilidad/ollama_estado.py" preparar --url "$OLLAMA_URL" --ventana "$VENTANA" "${SIMULADO[@]}" \
        --salida "$DEST/estado_ollama/$1-antes.json" || fallo "no se pudo arrancar $1 en frío (Ollama en uso o el modelo no se descargó)"
}
despues() {
    python3 -I "$AQUI/reproducibilidad/ollama_estado.py" despues --url "$OLLAMA_URL" --salida "$DEST/estado_ollama/$1-despues.json" \
        || echo "  AVISO: otro modelo apareció durante $1: queda marcada como contaminada"
}

echo; echo "== 2. E1-A0 (pedido directo, sin agente)"
preparar a0
EVAL=(); [ -f "$ARCHIVO_USUARIO" ] && EVAL=(--evaluar "$ARCHIVO_USUARIO")
python3 -I "$AQUI/e1/directo.py" --url "$OLLAMA_URL" --salida "$DEST/a0" "${EVAL[@]}" || fallo "E1-A0 falló"
despues a0

i=3
for C in $(python3 -I -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1]))["corridas"]))' "$DEST/experimento.json"); do
    CORR="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["corridas"][sys.argv[2]])' "$DEST/experimento.json" "$C")"
    G="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["condiciones"][sys.argv[2]]["guion"])' "$DEST/plan.json" "$C")"
    echo; echo "== $i. $C ($CORR, guion $G)"
    preparar "$C"
    python3 -I "$AQUI/e1/ejecutar_e1.py" --corridas "$CORRIDAS" ejecutar --corrida "$CORR" --guion "$DEST/$G" \
        --semilla "$SEMILLA" --oh-smoke "$OH_SMOKE" --oh-supervisor "$OH_SUPERVISOR" | tee "$DEST/$C.ejecutar.log"
    despues "$C"
    python3 -I "$AQUI/e1/ejecutar_e1.py" --corridas "$CORRIDAS" verificar --corrida "$CORR" > "$DEST/$C.verificar.log"
    i=$((i + 1))
done

echo; echo "== $i. análisis por etapas"
"$VENV/bin/python" -I "$AQUI/e1/analizar_e1.py" --experimento "$DEST" ${B1REF:+--b1 "$B1REF"} \
    2> >(grep -v "INFO\|UserWarning\|warnings.warn\|^ \{10,\}" >&2)
echo; echo "== listo. Informe: $DEST/informe_e1.md (y .json, e1.log, plan.json, experimento.json, a0/, estado_ollama/)"
