#!/usr/bin/env bash
# Una corrida del smoke test. Uso: ./correr.sh [semilla]
# No toca constructor-ia, sus servicios, Ollama ni la red: todo queda en resultados/<fecha>-s<semilla>/.
set -euo pipefail

BASE="$(cd "$(dirname "$0")" && pwd)"
SEMILLA="${1:-1}"
OLLAMA_URL="${OLLAMA_URL:-http://100.92.153.59:11435}"
MODELO="${MODELO:-qwen2.5-coder:7b}"
NUM_CTX="${NUM_CTX:-16384}"
MAX_ITER="${MAX_ITER:-30}"
MAX_SEG="${MAX_SEG:-900}"
# SIN_SCOPE=1 solo para ensayar donde no hay sesión systemd de usuario (no usar en la VivoBook).
SIN_SCOPE="${SIN_SCOPE:-0}"

R="$BASE/resultados/$(date +%Y%m%d-%H%M%S)-s$SEMILLA"
mkdir -p "$R/work" "$R/estado"
chmod 700 "$R"
SOCK="$(mktemp -d /tmp/oh-smoke.XXXXXX)"   # ruta corta: AF_UNIX admite ~107 caracteres
cp -a "$BASE/tarea/." "$R/work/"
sha256sum "$BASE/tarea/"* "$BASE/ocultas/"* > "$R/huellas_inicio.txt"

python3 -I "$BASE/relay.py" host --socket "$SOCK/ollama.sock" --destino "$OLLAMA_URL" \
    --modelo "$MODELO" --log "$R/puente.jsonl" &
PUENTE=$!
trap 'kill $PUENTE 2>/dev/null || true; rm -rf "$SOCK"' EXIT
for _ in $(seq 50); do [ -S "$SOCK/ollama.sock" ] && break; sleep 0.1; done
[ -S "$SOCK/ollama.sock" ] || { echo "el puente no arrancó"; exit 1; }

# Sistema de archivos mínimo, sin /home, sin red, sin capacidades, entorno vacío.
COMUN=(--unshare-all --die-with-parent --new-session --clearenv --cap-drop ALL
       --ro-bind /usr /usr --symlink usr/bin /bin --symlink usr/sbin /sbin
       --symlink usr/lib /lib --symlink usr/lib64 /lib64
       --ro-bind /etc/ld.so.cache /etc/ld.so.cache --ro-bind-try /etc/localtime /etc/localtime
       --proc /proc --dev /dev --tmpfs /tmp
       --setenv PATH /usr/bin:/bin --setenv LANG C.UTF-8 --setenv HOME /tmp/home)

AGENTE=(bwrap "${COMUN[@]}"
        --ro-bind "$BASE/venv" /opt/venv
        --ro-bind "$BASE/smoke.py" /opt/oh/smoke.py --ro-bind "$BASE/relay.py" /opt/oh/relay.py
        --bind "$R/work" /work --bind "$R/estado" /estado --bind "$SOCK" /sock
        --chdir /work
        --setenv OPENHANDS_SUPPRESS_BANNER 1 --setenv LITELLM_LOCAL_MODEL_COST_MAP True
        --setenv OH_MODELO "$MODELO" --setenv OH_SEMILLA "$SEMILLA" --setenv OH_NUM_CTX "$NUM_CTX"
        --setenv OH_MAX_ITER "$MAX_ITER" --setenv OH_MAX_SEG "$MAX_SEG"
        /opt/venv/bin/python -I /opt/oh/smoke.py)

if [ "$SIN_SCOPE" = 1 ]; then
    SCOPE=()
else
    SCOPE=(systemd-run --user --scope --quiet --unit="oh-smoke-$$"
           -p MemoryMax=2G -p MemorySwapMax=0 -p TasksMax=256 -p CPUQuota=200%)
fi

echo "== Agente (semilla $SEMILLA, $MODELO, num_ctx $NUM_CTX, máx $MAX_ITER iteraciones / $MAX_SEG s)"
set +e
timeout -k 10 $((MAX_SEG + 120)) "${SCOPE[@]}" "${AGENTE[@]}" > "$R/agente.log" 2>&1
echo $? > "$R/agente.exit"
set -e
kill $PUENTE 2>/dev/null || true

echo "== Pruebas ocultas (otro sandbox, sin red, resultado en solo lectura)"
set +e
bwrap "${COMUN[@]}" --ro-bind "$R/work" /work --ro-bind "$BASE/ocultas" /ocultas --chdir /tmp \
    /usr/bin/python3 -I -B -m unittest discover -s /ocultas -v > "$R/ocultas.txt" 2>&1
echo $? > "$R/ocultas.exit"
set -e

curl -sS -m 5 "$OLLAMA_URL/api/ps" > "$R/api_ps.json" 2>/dev/null || true   # solo lectura
diff -u "$BASE/tarea/duracion.py" "$R/work/duracion.py" > "$R/cambios.diff" || true
sha256sum "$BASE/tarea/"* "$BASE/ocultas/"* | diff - "$R/huellas_inicio.txt" > /dev/null \
    && echo ok > "$R/originales_intactos" || echo ALTERADOS > "$R/originales_intactos"

python3 -I - "$R" <<'EOF'
import json, sys, pathlib
r = pathlib.Path(sys.argv[1])
try:
    res = json.loads((r / "estado/resultado.json").read_text())
except (OSError, ValueError):
    sys.exit(f"sin resultado.json; ver {r}/agente.log")
crit = res["criterios"]
crit["5_ocultas_pasan"] = (r / "ocultas.exit").read_text().strip() == "0"
pedidos = [json.loads(l) for l in (r / "puente.jsonl").read_text().splitlines() if l.strip()]
chats = [p for p in pedidos if p.get("ruta") == "/api/chat" and p.get("permitido")]
crit["6_num_ctx_en_cada_pedido"] = bool(chats) and all((p.get("num_ctx") or 0) >= 16384 for p in chats)
res["pedidos_rechazados_por_el_puente"] = [p for p in pedidos if p.get("permitido") is False]
res["originales_intactos"] = (r / "originales_intactos").read_text().strip()
(r / "resultado_final.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
for k, v in crit.items():
    print(f"  {'OK ' if v else 'NO '} {k}")
print(f"  estado={res['estado_final']} llamadas={res['llamadas_llm']} tokens={res['tokens_prompt']}+"
      f"{res['tokens_generados']} {res['segundos']}s acciones={res['acciones']}")
print(f"  mensajes de texto sin herramienta: {res['detalle']['mensajes_de_texto_del_agente']}; "
      f"correcciones de formato del SDK: {res['detalle']['correcciones_de_formato_del_sdk']}")
print(f"  rechazados por el puente: {len(res['pedidos_rechazados_por_el_puente'])}; originales: {res['originales_intactos']}")
print(f"  carpeta: {r}")
EOF
