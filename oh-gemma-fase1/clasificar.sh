#!/usr/bin/env bash
# Clasifica una carpeta de resultados de medir.py dentro de un sandbox sin red. Uso: ./clasificar.sh resultados/<fecha>
set -euo pipefail
KIT="$(cd "$(dirname "$0")" && pwd)"
SALIDA="$(cd "$1" && pwd)"
VENV="${OH_VENV:-$HOME/oh-smoke/venv}"
[ -f "$SALIDA/resultados.json" ] || { echo "falta $SALIDA/resultados.json"; exit 1; }
bwrap --unshare-all --die-with-parent --new-session --clearenv --cap-drop ALL \
    --ro-bind /usr /usr --symlink usr/bin /bin --symlink usr/sbin /sbin \
    --symlink usr/lib /lib --symlink usr/lib64 /lib64 \
    --ro-bind /etc/ld.so.cache /etc/ld.so.cache --ro-bind-try /etc/alternatives /etc/alternatives \
    --proc /proc --dev /dev --tmpfs /tmp \
    --ro-bind "$VENV" /opt/venv --ro-bind "$KIT" /kit --bind "$SALIDA" /salida \
    --setenv PATH /usr/bin:/bin --setenv LANG C.UTF-8 --setenv HOME /tmp \
    --setenv OPENHANDS_SUPPRESS_BANNER 1 --setenv LITELLM_LOCAL_MODEL_COST_MAP True \
    --remount-ro / \
    /opt/venv/bin/python -I /kit/clasificar.py /salida/resultados.json /kit/herramientas.json
