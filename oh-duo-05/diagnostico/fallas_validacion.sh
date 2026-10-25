#!/usr/bin/env bash
# Diagnóstico de SOLO LECTURA de una validación fallida (paso 11, pruebas del Ollama simulado). Uso:
#   bash ~/oh-duo-05/diagnostico/fallas_validacion.sh [informes/validacion-<fecha>]     (por omisión, la última)
# No hace inferencias, no contacta al Ollama real (salvo GET /api/ps y /api/version si se indica OLLAMA_URL),
# no descarga modelos y no reinicia nada. Escribe <validacion>/diagnostico_fallas.txt y lo muestra.
#   1. las fallas completas tal como quedaron en el registro de la validación;
#   2. qué señal de actividad dispara ollama_estado.py contra el Ollama SIMULADO, foto por foto;
#   3. las condiciones del entorno que alimentan esas señales: ss, nvidia-smi, pgrep, ip, puertos de Ollama;
#   4. lo mismo que vería el modo real contra el Ollama de verdad (solo GET), para saber si A1 real abortaría.
set -u
AQUI="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${VENV:-$HOME/oh-smoke/venv}"
OLLAMA_URL="${OLLAMA_URL:-http://100.92.153.59:11435}"
V="${1:-$(ls -dt "$AQUI"/informes/validacion-* 2>/dev/null | head -1)}"
[ -d "$V" ] || { echo "no encuentro la carpeta de la validación ('$V')"; exit 1; }
SAL="$V/diagnostico_fallas.txt"
{
echo "### Diagnóstico de fallas · $(date -Is) · $(hostname) · usuario $(id -un)"
echo "validación: $V"

echo; echo "### 1. Fallas registradas en la validación (paso 11)"
if [ -f "$V/validacion.log" ]; then
    sed -n '/=== 11 /,/--- \(OK\|FALLÓ\): 11/p' "$V/validacion.log" | grep -vE "^\s+(INFO|warnings.warn)|UserWarning" | head -200
else
    echo "(no está validacion.log)"
fi

echo; echo "### 2. Señales que ve ollama_estado.py contra el Ollama SIMULADO (sin tocar el real)"
"$VENV/bin/python" -I - "$AQUI" <<'EOF'
import json, sys
from pathlib import Path
aqui = Path(sys.argv[1])
sys.path[:0] = [str(aqui / "tests"), str(aqui / "reproducibilidad")]
import simulado as S, ollama_estado as O
srv = S.Servidor(lambda n, m: S.FIN, 0.0, estado_ollama=True)
url = f"http://127.0.0.1:{srv.puerto}"
print(f"servidor simulado en {url}")
print(f"es_local({url}) = {O.es_local(url)}")
for nombre, kw in (("como lo llamaba la versión que falló (señales locales automáticas)", {}),
                   ("con la corrección (el simulado no es el Ollama de esta máquina)", {"senales_locales": False})):
    try:
        ok, motivos, fotos = O.inactivo(url, ventana=0, dormir=lambda s: None, **kw)
    except TypeError:
        print(f"- {nombre}: esta versión de ollama_estado.py no tiene la corrección todavía")
        continue
    print(f"- {nombre}: inactivo={ok}; motivos={motivos}")
    f = fotos[0]
    print(f"    foto: conexiones={f['conexiones']} gpu={json.dumps(f['gpu'], ensure_ascii=False)} procesos_oh={f['procesos_oh']}")
srv.cerrar()
EOF

echo; echo "### 3. Entorno que alimenta las señales"
for h in ss nvidia-smi pgrep ip curl python3; do printf '%-11s %s\n' "$h" "$(command -v $h || echo 'NO ESTÁ')"; done
echo "--- conexiones TCP establecidas con 11434/11435 (sin procesos; las cuenta ollama_estado.py)"
ss -Htn state established 2>/dev/null | awk '$3 ~ /:(11434|11435)$/ || $4 ~ /:(11434|11435)$/' || echo "(ss no disponible)"
echo "total en el lado servidor: $(ss -Htn state established 2>/dev/null | awk '$3 ~ /:(11434|11435)$/' | wc -l)"
echo "--- quién escucha en 11434/11435"
ss -Hltn 2>/dev/null | grep -E ":(11434|11435)\b" || echo "(nadie visible)"
echo "--- uso de GPU (5 muestras, 1 s)"
for _ in 1 2 3 4 5; do nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader 2>/dev/null || echo "(sin nvidia-smi)"; sleep 1; done
echo "--- procesos de la GPU"
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader 2>/dev/null || true
echo "--- procesos que ollama_estado.py considera de OH-DUO"
pgrep -af '/correr\.sh|/opt/oh/smoke\.py' || echo "(ninguno)"
echo "--- direcciones locales"
ip -o -4 addr 2>/dev/null | awk '{print $2, $4}'

echo; echo "### 4. Lo que vería A1 real contra $OLLAMA_URL (solo GET /api/version y /api/ps; nada se descarga)"
"$VENV/bin/python" -I - "$AQUI" "$OLLAMA_URL" <<'EOF'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "reproducibilidad"))
import ollama_estado as O
url = sys.argv[2]
try:
    ok, motivos, fotos = O.inactivo(url, ventana=10)
    f = fotos[-1]
    print(f"es_local={f['ollama_local']} versión={f['version']} cargados={[m['name'] for m in f['cargados']]}")
    print(f"inactivo={ok}; motivos={motivos}")
    print(f"conexiones={[x['conexiones'] for x in fotos]} gpu_uso={[[g.get('uso_pct') for g in (x['gpu'] or [])] for x in fotos]}")
except Exception as e:
    print(f"no se pudo consultar {url}: {e!r}")
EOF
} > "$SAL" 2>&1
cat "$SAL"
echo; echo "== guardado en $SAL"
