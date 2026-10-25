#!/usr/bin/env bash
# Una ejecución del experimento E1 (e1/E1.md). Es oh-duo-05/ejecutor/correr.sh más UN montaje en solo lectura:
# guion.json → /opt/oh/guion.json (las preguntas de E1 y en qué acción se insertan). Lo demás es idéntico.
# --- texto original de oh-duo-05:
# Una ejecución de OH-DUO-05 (un problema, un modelo), con buzón de mensajes. Uso: ./correr.sh [semilla]
# Es oh-duo-01/ejecutor/correr.sh más: un buzón <corrida>/buzon/mensajes.jsonl montado EN SOLO LECTURA en
# /buzon, y puente.py (en solo lectura) para que agente.py entregue esos mensajes durante el mismo run().
# Lo demás (sandbox, puente HTTP, límites, control previo, evaluación) es idéntico.
# --- texto original de oh-duo-01:
# Una ejecución de OH-DUO-01 (un problema, un modelo). Uso: ./correr.sh [semilla]   (modelo: variable MODELO)
# Es oh-lab/ejecutor/correr.sh SIN pruebas ocultas ni referencia: la instancia no las contiene y acá solo se
# evalúan las visibles. Sandbox, puente, límites y control previo (sin ocultas) son los mismos. Agrega el texto
# del rol (enfoque.txt), montado en solo lectura. Instancia que arma oh-duo-01/duo.py:
#   <instancia>/{correr.sh, smoke.py, relay.py, mision.json, enfoque.txt, tarea/, venv -> oh-smoke/venv}
# Escribe solo en <instancia>/resultados/<corrida>/ y en un directorio temporal /tmp/oh-smoke.* (socket).
set -euo pipefail

BASE="$(cd "$(dirname "$0")" && pwd)"
SEMILLA="${1:-1}"
OLLAMA_URL="${OLLAMA_URL:-http://100.92.153.59:11435}"
MODELO="${MODELO:-qwen2.5-coder:7b}"
NUM_CTX="${NUM_CTX:-16384}"
MAX_ITER="${MAX_ITER:-30}"
MAX_SEG="${MAX_SEG:-900}"
# Tras MAX_SEG, el agente tiene GRACIA s para cerrar el paso en curso; después smoke.py registra y sale.
# Un pedido bloqueado puede tardar 2 × 300 s + espera, por eso el corte externo deja 180 s más.
GRACIA="${GRACIA:-60}"
CORTE_EXTERNO=$((MAX_SEG + GRACIA + 180))
# SIN_SCOPE=1 solo para ensayar donde no hay sesión systemd de usuario (no usar en la VivoBook).
SIN_SCOPE="${SIN_SCOPE:-0}"
MODULO="$(python3 -I -c 'import json,re,sys; m=json.load(open(sys.argv[1]))["modulo"]; assert re.fullmatch(r"[a-z_][a-z0-9_]*", m), m; print(m)' "$BASE/mision.json")"
PRUEBA="test_$MODULO.py"

R="$BASE/resultados/$(date +%Y%m%d-%H%M%S)-s$SEMILLA"
mkdir -p "$R/work" "$R/estado" "$R/control" "$R/buzon"
: > "$R/buzon/mensajes.jsonl"     # lo escribe solo el despachador, desde el anfitrión
chmod 700 "$R"
SOCK="$(mktemp -d /tmp/oh-smoke.XXXXXX)"   # ruta corta: AF_UNIX admite ~107 caracteres
trap 'kill ${PUENTE:-} 2>/dev/null || true; rm -rf "$SOCK"' EXIT
cp -a "$BASE/tarea/." "$R/work/"
( cd "$BASE" && sha256sum tarea/* smoke.py relay.py enfoque.txt puente.py guion.json ) > "$R/huellas_inicio.txt"

# Sistema de archivos mínimo, sin /home, sin red, sin capacidades, entorno vacío.
COMUN=(--unshare-all --die-with-parent --new-session --clearenv --cap-drop ALL
       --ro-bind /usr /usr --symlink usr/bin /bin --symlink usr/sbin /sbin
       --symlink usr/lib /lib --symlink usr/lib64 /lib64
       --ro-bind /etc/ld.so.cache /etc/ld.so.cache --ro-bind-try /etc/localtime /etc/localtime
       --ro-bind-try /etc/alternatives /etc/alternatives
       --proc /proc --dev /dev --tmpfs /tmp
       --setenv PATH /usr/bin:/bin --setenv LANG C.UTF-8 --setenv HOME /tmp/home)

# Evalúa $1/$MODULO.py con la prueba VISIBLE original, en un sandbox nuevo: solo esos dos archivos, en solo
# lectura, en /work. Escribe $2.visibles.{txt,exit}. Las ocultas no existen en esta instancia.
evaluar() {
    local dir="$1" salida="$2"
    set +e
    timeout -k 5 60 bwrap "${COMUN[@]}" --ro-bind "$dir" /work --chdir /tmp \
        --remount-ro / /usr/bin/python3.12 -I -B -m unittest discover -s /work -p "$PRUEBA" -v > "$salida.visibles.txt" 2>&1
    echo $? > "$salida.visibles.exit"
    set -e
}

# Copia a $2 el $MODULO.py de $1 solo si es un archivo regular (sin enlaces), más la prueba visible original.
copiar_para_evaluar() {
    python3 -I - "$1/$MODULO.py" "$2" "$BASE/tarea/$PRUEBA" <<'EOF'
import os, shutil, stat, sys
origen, destino, prueba = sys.argv[1:]
modulo, nombre_prueba = os.path.basename(origen), os.path.basename(prueba)
os.makedirs(destino, exist_ok=True)
try:
    fd = os.open(origen, os.O_RDONLY | os.O_NOFOLLOW)
except OSError as e:
    sys.exit(f"{modulo} no es un archivo regular legible: {e}")
with os.fdopen(fd, "rb") as f:
    if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
        sys.exit(f"{modulo} no es un archivo regular")
    datos = f.read(1 << 20)
with open(os.path.join(destino, modulo), "wb") as g:
    g.write(datos)
shutil.copyfile(prueba, os.path.join(destino, nombre_prueba))
EOF
}

# ¿Ran 6 tests sin errores de importación? (para distinguir «falla la solución» de «falla la evaluación»)
importo() { grep -q "^Ran [1-9]" "$1" && ! grep -qE "ImportError|ModuleNotFoundError|SyntaxError" "$1"; }

echo "== Control previo (sin GPU): sandbox y prueba visible sobre el original"
copiar_para_evaluar "$BASE/tarea" "$R/control/original"
evaluar "$R/control/original" "$R/control/original"
# Los mismos montajes que usa el agente (más abajo), comprobados antes de gastar GPU.
MONTAJES=(--ro-bind "$BASE/venv" /opt/venv
          --ro-bind "$BASE/smoke.py" /opt/oh/smoke.py --ro-bind "$BASE/relay.py" /opt/oh/relay.py
          --ro-bind "$BASE/enfoque.txt" /opt/oh/enfoque.txt
          --ro-bind "$BASE/puente.py" /opt/oh/puente.py --ro-bind "$R/buzon" /buzon
          --ro-bind "$BASE/guion.json" /opt/oh/guion.json
          --bind "$R/work" /work --bind "$R/estado" /estado --bind "$SOCK" /sock --chdir /work
          --setenv OPENHANDS_SUPPRESS_BANNER 1 --setenv LITELLM_LOCAL_MODEL_COST_MAP True)
set +e
bwrap "${COMUN[@]}" "${MONTAJES[@]}" --remount-ro / /opt/venv/bin/python -I - "$BASE" > "$R/control/sandbox.txt" 2>&1 <<'EOF'
import os, socket, sys
base = sys.argv[1]
fallas = []
for ruta in ("/home", "/root", "/run", "/var", "/srv", "/mnt", "/media", "/etc/passwd", "/ocultas", base,
             os.path.join(base, "ocultas"), os.path.join(base, "tarea")):
    if os.path.exists(ruta):
        fallas.append(f"visible: {ruta}")
for ruta in ("/usr/bin/x", "/opt/venv/x", "/opt/oh/x", "/opt/oh/smoke.py", "/opt/oh/enfoque.txt", "/opt/oh/puente.py",
             "/buzon/x", "/buzon/mensajes.jsonl", "/opt/oh/guion.json", "/etc/x", "/x"):
    try:
        open(ruta, "a").close(); fallas.append(f"escribible: {ruta}")
    except OSError:
        pass
for ruta in ("/work/.prueba", "/estado/.prueba", "/tmp/.prueba"):
    open(ruta, "w").close(); os.unlink(ruta)
for destino in (("1.1.1.1", 53), ("100.92.153.59", 11435), ("127.0.0.1", 11434), ("127.0.0.1", 11435)):
    try:
        socket.create_connection(destino, timeout=2).close(); fallas.append(f"red: {destino}")
    except OSError:
        pass
import openhands.sdk, openhands.tools.terminal, openhands.tools.file_editor  # noqa: E401
print("FALLAS:", fallas) if fallas else print("SANDBOX OK")
EOF
SANDBOX=$?
set -e
FALLA=""
[ "$SANDBOX" = 0 ] && grep -q "SANDBOX OK" "$R/control/sandbox.txt" || FALLA="sandbox (ver control/sandbox.txt)"
importo "$R/control/original.visibles.txt" || FALLA="$FALLA la prueba visible no importa $MODULO.py"
[ "$(cat "$R/control/original.visibles.exit")" != 0 ] || FALLA="$FALLA el original aprueba las visibles"
if [ -n "$FALLA" ]; then echo "  control previo FALLÓ: $FALLA — no se usa la GPU. Carpeta: $R"; exit 1; fi
echo "  OK: sandbox; el original reprueba la prueba visible, sin errores de importación"

python3 -I "$BASE/relay.py" host --socket "$SOCK/ollama.sock" --destino "$OLLAMA_URL" \
    --modelo "$MODELO" --log "$R/puente.jsonl" &
PUENTE=$!
for _ in $(seq 50); do [ -S "$SOCK/ollama.sock" ] && break; sleep 0.1; done
[ -S "$SOCK/ollama.sock" ] || { echo "el puente no arrancó"; exit 1; }

AGENTE=(bwrap "${COMUN[@]}" "${MONTAJES[@]}"
        --setenv OH_MODELO "$MODELO" --setenv OH_SEMILLA "$SEMILLA" --setenv OH_NUM_CTX "$NUM_CTX"
        --setenv OH_MAX_ITER "$MAX_ITER" --setenv OH_MAX_SEG "$MAX_SEG" --setenv OH_GRACIA "$GRACIA"
        --setenv OH_MODULO "$MODULO" --setenv OH_REGISTRO_LLM "${REGISTRO_LLM:-0}"
        --remount-ro / /opt/venv/bin/python -I /opt/oh/smoke.py)

if [ "$SIN_SCOPE" = 1 ]; then
    SCOPE=()
else
    SCOPE=(systemd-run --user --scope --quiet --unit="oh-smoke-$$"
           -p MemoryMax=2G -p MemorySwapMax=0 -p TasksMax=256 -p CPUQuota=200%)
fi

echo "== Agente (semilla $SEMILLA, $MODELO, num_ctx $NUM_CTX, máx $MAX_ITER iteraciones / $MAX_SEG s + $GRACIA s; corte externo $CORTE_EXTERNO s)"
set +e
timeout -k 10 "$CORTE_EXTERNO" "${SCOPE[@]}" "${AGENTE[@]}" > "$R/agente.log" 2>&1
echo $? > "$R/agente.exit"
set -e
kill "$PUENTE" 2>/dev/null || true   # cierra también cualquier pedido en curso hacia Ollama

echo "== Evaluación visible (fuera del sandbox del agente: su $MODULO.py con la prueba visible original)"
if copiar_para_evaluar "$R/work" "$R/eval" 2> "$R/eval_copia.txt"; then
    evaluar "$R/eval" "$R/eval"
    diff -u "$BASE/tarea/$MODULO.py" "$R/eval/$MODULO.py" > "$R/cambios.diff" || true
else
    echo 1 > "$R/eval.visibles.exit"
fi
curl -sS -m 5 "$OLLAMA_URL/api/ps" > "$R/api_ps.json" 2>/dev/null || true   # solo lectura
( cd "$BASE" && sha256sum tarea/* smoke.py relay.py enfoque.txt puente.py guion.json ) | diff - "$R/huellas_inicio.txt" > /dev/null \
    && echo ok > "$R/originales_intactos" || echo ALTERADOS > "$R/originales_intactos"

python3 -I - "$R" "$BASE" "$MODULO" <<'EOF'
import hashlib, json, os, sys, pathlib
r, base, modulo = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
codigo, prueba = f"{modulo}.py", f"test_{modulo}.py"
def huella(p):
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()
try:
    res = json.loads((r / "estado/resultado.json").read_text())
except (OSError, ValueError):
    res = {"criterios": {}, "detalle": {}, "nota": f"sin resultado.json del agente; ver {r}/agente.log"}
crit = res["criterios"]
crit["3_edito_codigo_y_no_las_pruebas"] = (
    huella(r / "work" / codigo) not in (None, huella(base / "tarea" / codigo))
    and huella(r / "work" / prueba) == huella(base / "tarea" / prueba))
crit["4_visibles_pasan_y_termino_con_finish"] = (
    (r / "eval.visibles.exit").read_text().strip() == "0" and bool(res.get("termino_con_finish")))
pedidos = [json.loads(l) for l in (r / "puente.jsonl").read_text().splitlines() if l.strip()]
chats = [p for p in pedidos if p.get("ruta") == "/api/chat" and p.get("permitido")]
crit["6_num_ctx_en_cada_pedido"] = bool(chats) and all((p.get("num_ctx") or 0) >= 16384 for p in chats)
res["pedidos_rechazados_por_el_puente"] = [p for p in pedidos if p.get("permitido") is False]
res["originales_intactos"] = (r / "originales_intactos").read_text().strip()
res["agente_exit"] = (r / "agente.exit").read_text().strip()
(r / "resultado_final.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
for k in sorted(crit):
    print(f"  {'OK ' if crit[k] else 'NO '} {k}")
print(f"  estado={res.get('estado_final')} llamadas={res.get('llamadas_llm')} tokens={res.get('tokens_prompt')}+"
      f"{res.get('tokens_generados')} {res.get('segundos')}s vencio={res.get('vencio_tiempo')} "
      f"corte_forzado={res.get('corte_forzado')} salida={res['agente_exit']}")
print(f"  acciones={res.get('acciones')}")
print(f"  mensajes de texto sin herramienta: {res['detalle'].get('mensajes_de_texto_del_agente')}; "
      f"correcciones de formato del SDK: {res['detalle'].get('correcciones_de_formato_del_sdk')}")
print(f"  rechazados por el puente: {len(res['pedidos_rechazados_por_el_puente'])}; originales: {res['originales_intactos']}")
if res.get("nota"):
    print(f"  {res['nota']}")
print(f"  carpeta: {r}")
EOF
