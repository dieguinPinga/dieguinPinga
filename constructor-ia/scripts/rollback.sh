#!/usr/bin/env bash
# Vuelve a la versión anterior del código (app.prev). Los datos no se tocan;
# si hace falta restaurar la base, ver INSTALL.md §Recuperación.
set -euo pipefail
PREFIX="${CIA_PREFIX:-$HOME/constructor-ia}"
[ -d "$PREFIX/app.prev" ] || { echo "No hay versión anterior en $PREFIX/app.prev"; exit 1; }
WAS_ACTIVE=0
systemctl --user is-active --quiet constructor-ia.service 2>/dev/null && WAS_ACTIVE=1
[ "$WAS_ACTIVE" = 1 ] && systemctl --user stop constructor-ia.service
rm -rf "$PREFIX/app.failed"
mv "$PREFIX/app" "$PREFIX/app.failed"
mv "$PREFIX/app.prev" "$PREFIX/app"
install -m 644 "$PREFIX/app/systemd/constructor-ia.service" "$HOME/.config/systemd/user/constructor-ia.service"
systemctl --user daemon-reload || true
[ "$WAS_ACTIVE" = 1 ] && systemctl --user start constructor-ia.service
echo "Rollback hecho. Versión fallida conservada en $PREFIX/app.failed"
