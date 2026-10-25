#!/usr/bin/env bash
# Auditoría de SOLO LECTURA de A1 y B1 reales (diagnostico/auditar_b1.py). Uso:
#   bash ~/oh-duo-05/diagnostico/auditar_vivobook.sh [informes/b1-p1-s11-<etiqueta>]     (por omisión, el último B1)
# No hace inferencias, no toca Ollama ni las corridas. Las versiones intermedias de intervalos.py se evalúan en un
# bwrap nuevo, sobre copias en /tmp. Escribe <b1>/auditoria/auditoria.{md,json}.
set -u
AQUI="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${VENV:-$HOME/oh-smoke/venv}"
B1="${1:-$(ls -dt "$AQUI"/informes/b1-p1-s11-* 2>/dev/null | grep -v auditoria | head -1)}"
[ -f "$B1/experimento.json" ] || { echo "no encuentro el experimento B1 ('$B1')"; exit 1; }
command -v bwrap > /dev/null || { echo "falta bwrap"; exit 1; }
"$VENV/bin/python" -I "$AQUI/diagnostico/auditar_b1.py" --b1 "$B1" 2> >(grep -v "INFO\|UserWarning\|warnings.warn\|^ \{10,\}" >&2)
echo; echo "== Para compartir: $B1/auditoria/auditoria.md"
