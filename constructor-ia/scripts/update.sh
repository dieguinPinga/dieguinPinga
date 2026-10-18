#!/usr/bin/env bash
# Actualiza Constructor IA desde un checkout nuevo del repositorio.
# Uso: bash <checkout>/constructor-ia/scripts/update.sh
set -euo pipefail
PREFIX="${CIA_PREFIX:-$HOME/constructor-ia}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ "$SRC" != "$PREFIX/app" ] || { echo "Ejecute update.sh desde el checkout nuevo, no desde la instalación"; exit 1; }
WAS_ACTIVE=0
systemctl --user is-active --quiet constructor-ia.service 2>/dev/null && WAS_ACTIVE=1
if [ -f "$PREFIX/data/constructor.db" ]; then
  echo "==> Copia de seguridad de la base"
  "$PREFIX/bin/constructor-ia" backup
fi
if [ "$WAS_ACTIVE" = 1 ]; then
  echo "==> Deteniendo el servicio"
  systemctl --user stop constructor-ia.service
fi
bash "$SRC/scripts/install.sh"
echo "==> Autodiagnóstico"
"$PREFIX/bin/constructor-ia" selftest || echo "[aviso] la ejecución aislada quedará bloqueada hasta resolverlo"
if [ "$WAS_ACTIVE" = 1 ]; then
  systemctl --user start constructor-ia.service
  sleep 2
  systemctl --user is-active --quiet constructor-ia.service || {
    echo "[error] el servicio no arrancó; para volver atrás: $PREFIX/bin/rollback.sh"; exit 1; }
fi
echo "Actualización terminada. Para volver a la versión anterior: $PREFIX/bin/rollback.sh"
