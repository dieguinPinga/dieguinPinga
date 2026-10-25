#!/usr/bin/env bash
# Protocolo OH-DUO-05 (PROTOCOLO.md): Qwen en P1 SIN coordinador (control) frente a CON coordinador (intervenida).
#
#   bash comparar_qwen_p1.sh --simulado                               ensayo completo SIN inferencia (validar.sh)
#   bash comparar_qwen_p1.sh --real --confirmo-inferencia-real [--semilla 11]     ← corridas reales con Qwen
#
# Los dos brazos usan el mismo problema (P1), modelo (qwen2.5-coder:7b), semilla, ejecutor (con puente; en el
# control el buzón queda vacío), límites (30 iteraciones, 900 s + 60 s, num_ctx 16384) y la misma cadena
# observador → coordinador → despachador; lo único que cambia es --enviar. Orden: control, después intervenida.
# Las pruebas ocultas se corren al final, fuera del sandbox, con las dos ejecuciones ya congeladas.
set -euo pipefail
AQUI="$(cd "$(dirname "$0")" && pwd)"
MODO="" CONFIRMA=0 SEMILLA=11 ETIQUETA="$(date +%Y%m%d-%H%M%S)"
while [ $# -gt 0 ]; do
    case "$1" in
        --simulado) MODO=simulado ;;
        --real) MODO=real ;;
        --confirmo-inferencia-real) CONFIRMA=1 ;;
        --semilla) SEMILLA="$2"; shift ;;
        --etiqueta) ETIQUETA="$2"; shift ;;
        *) echo "opción desconocida: $1"; exit 2 ;;
    esac
    shift
done
if [ "$MODO" = "" ] || { [ "$MODO" = real ] && [ "$CONFIRMA" != 1 ]; }; then
    sed -n '2,10p' "$0"; echo; echo "Para inferencia real hacen falta --real y --confirmo-inferencia-real."; exit 2
fi
[[ "$SEMILLA" =~ ^[0-9]+$ ]] || { echo "semilla inválida"; exit 2; }

# Condiciones fijas, iguales en los dos brazos (no se toman del entorno).
export MAX_ITER=30 MAX_SEG=900 GRACIA=60 NUM_CTX=16384
unset MODELO
export OH_SMOKE="${OH_SMOKE:-$HOME/oh-smoke}" OH_SUPERVISOR="${OH_SUPERVISOR:-$HOME/oh-supervisor}"
NOMBRE="qwen-p1-s$SEMILLA-$ETIQUETA"
CTRL="$NOMBRE-control" INT="$NOMBRE-intervenida"

if [ "$MODO" = simulado ]; then
    BASE="$(mktemp -d /tmp/oh5-protocolo.XXXXXX)"
    CORRIDAS="$BASE/corridas" INFORMES="${OH5_INFORME:-$BASE/informes}"
    python3 -I "$AQUI/tests/simulado.py" "$BASE/puerto" 0.2 &
    SIM=$!
    trap 'kill $SIM 2>/dev/null || true' EXIT
    for _ in $(seq 50); do [ -s "$BASE/puerto" ] && break; sleep 0.1; done
    export OLLAMA_URL="http://127.0.0.1:$(cat "$BASE/puerto")"
else
    CORRIDAS="$AQUI/corridas" INFORMES="$AQUI/informes"
    export OLLAMA_URL="${OLLAMA_URL:-http://100.92.153.59:11435}"
fi
DEST="$INFORMES/$NOMBRE"
mkdir -p "$DEST"
exec > >(tee "$DEST/protocolo.log") 2>&1
echo "== Protocolo OH-DUO-05 · modo $MODO · $NOMBRE · $(date -Is) · $(hostname)"

# ---- 0. comprobaciones previas: cualquiera aborta antes de usar la GPU
fallo() { echo "ABORTADO: $*"; exit 1; }
pgrep -f "/correr.sh" > /dev/null && fallo "hay un correr.sh en ejecución"
for c in "$CTRL" "$INT"; do [ -e "$CORRIDAS/$c" ] && fallo "ya existe $CORRIDAS/$c"; done
[ -x "$OH_SMOKE/venv/bin/python" ] || fallo "falta $OH_SMOKE/venv"
[ -f "$OH_SUPERVISOR/supervisor.py" ] || fallo "falta $OH_SUPERVISOR/supervisor.py"
curl -sSf -m 5 "$OLLAMA_URL/api/version" > "$DEST/ollama_version.json" || fallo "Ollama no responde en $OLLAMA_URL"
if [ "$MODO" = real ]; then
    curl -sSf -m 5 "$OLLAMA_URL/api/tags" | grep -q '"qwen2.5-coder:7b"' || fallo "qwen2.5-coder:7b no está en Ollama"
    command -v nvidia-smi > /dev/null && nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv > "$DEST/gpu_antes.csv" || true
fi

# Registro de condiciones: huellas de todo lo que interviene, para comprobar después que nada cambió.
python3 -I - "$AQUI" "$DEST/protocolo.json" "$MODO" "$SEMILLA" "$OLLAMA_URL" "$CTRL" "$INT" <<'EOF'
import hashlib, json, os, sys
from pathlib import Path
aqui, salida, modo, semilla, url, ctrl, inter = sys.argv[1:]
aqui = Path(aqui)
def raiz(n):
    for p in (aqui.parent / n, Path.home() / n):
        if p.is_dir():
            return p
oh = {n: raiz(n) for n in ("oh-duo-01", "oh-duo-02", "oh-duo-03", "oh-duo-04")}
archivos = [aqui / "ejecutor" / f for f in ("correr.sh", "agente.py", "puente.py")] + \
    [aqui / f for f in ("ejecutar.py", "cadena.py", "despachador.py", "intervenciones.py")] + \
    [oh["oh-duo-01"] / f for f in ("duo.py", "roles/qwen_minimo.txt", "banco/P1/tarea/intervalos.py",
                                   "banco/P1/tarea/test_intervalos.py")] + \
    [oh["oh-duo-02"] / "estancamiento.py", oh["oh-duo-02"] / "umbrales.json", oh["oh-duo-03"] / "observador.py",
     oh["oh-duo-04"] / "coordinador.py", oh["oh-duo-04"] / "coordinador.json"]
json.dump({
    "protocolo": "OH-DUO-05 Qwen P1 control vs intervenida", "modo": modo, "problema": "P1",
    "modelo": "qwen2.5-coder:7b", "semilla": int(semilla), "temperatura": 0.2, "ollama": url,
    "limites": {k: os.environ[k] for k in ("MAX_ITER", "MAX_SEG", "GRACIA", "NUM_CTX")},
    "brazos": {"control": {"corrida": ctrl, "enviar": False}, "intervenida": {"corrida": inter, "enviar": True}},
    "orden": ["control", "intervenida"],
    "huellas": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in archivos},
}, open(salida, "w"), ensure_ascii=False, indent=1)
EOF
echo "  condiciones registradas en $DEST/protocolo.json"

# ---- 1–2. un brazo: la cadena en vivo (con o sin --enviar) y la ejecución
brazo() {
    local corrida="$1"; shift
    echo; echo "== brazo $corrida ($([ $# -gt 0 ] && echo "CON coordinador" || echo "SIN coordinador: nada se envía"))"
    python3 -I "$AQUI/cadena.py" --corrida "$CORRIDAS/$corrida" --salida "$INFORMES/$corrida" --maximo 1 "$@" \
        > "$DEST/$corrida.cadena.log" 2>&1 &
    local cad=$!
    python3 -I "$AQUI/ejecutar.py" --corridas "$CORRIDAS" ejecutar --corrida "$corrida" --problemas P1 \
        --modelos qwen --semilla "$SEMILLA" --oh-smoke "$OH_SMOKE" --oh-supervisor "$OH_SUPERVISOR" \
        | tee "$DEST/$corrida.ejecutar.log"
    wait "$cad" || echo "  aviso: la cadena terminó con error (ver $DEST/$corrida.cadena.log)"
    tail -n 3 "$DEST/$corrida.cadena.log"
}
brazo "$CTRL"
brazo "$INT" --enviar
[ "$MODO" = real ] && command -v nvidia-smi > /dev/null && \
    nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv > "$DEST/gpu_despues.csv" || true

# ---- 3. pruebas ocultas (fuera del sandbox del agente, con las dos ya congeladas)
echo; echo "== verificación con pruebas ocultas"
for c in "$CTRL" "$INT"; do python3 -I "$AQUI/ejecutar.py" --corridas "$CORRIDAS" verificar --corrida "$c" > "$DEST/$c.verificar.log"; done

# ---- 4. comparación y validez
echo; echo "== comparación"
python3 -I "$AQUI/intervenciones.py" comparar --control "$CORRIDAS/$CTRL" --intervenida "$CORRIDAS/$INT" \
    --despachos "$INFORMES/$INT/despachos.jsonl" --salida "$DEST"
echo; echo "== listo. Informe: $DEST (comparacion.md, comparacion.json, protocolo.json, protocolo.log)"
