#!/usr/bin/env bash
# Desinstala Constructor IA. Por defecto conserva config/ y data/.
# Uso: uninstall.sh [--purge]   (--purge borra también datos, tras confirmación)
set -euo pipefail
PREFIX="${CIA_PREFIX:-$HOME/constructor-ia}"
case "$PREFIX" in "$HOME"/constructor-ia*) ;; *) echo "Prefijo inesperado: $PREFIX"; exit 1;; esac
echo "==> Deteniendo y deshabilitando el servicio (solo constructor-ia)"
systemctl --user stop constructor-ia.service 2>/dev/null || true
systemctl --user disable constructor-ia.service 2>/dev/null || true
systemctl --user stop 'cia-job-*.scope' 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/constructor-ia.service"
systemctl --user daemon-reload 2>/dev/null || true
echo "==> Eliminando código, entorno virtual y lanzadores"
rm -rf "$PREFIX/app" "$PREFIX/app.prev" "$PREFIX/app.new" "$PREFIX/app.failed" "$PREFIX/venv" "$PREFIX/bin"
if [ "${1:-}" = "--purge" ]; then
  read -r -p "Esto borra TODAS las misiones, conversaciones y la configuración en $PREFIX. Escriba BORRAR: " ans
  if [ "$ans" = "BORRAR" ]; then
    rm -rf "$PREFIX"
    echo "Datos eliminados."
  else
    echo "Cancelado: datos conservados en $PREFIX/config y $PREFIX/data"
  fi
else
  echo "Datos conservados en $PREFIX/config y $PREFIX/data (use --purge para borrarlos)."
fi
