#!/usr/bin/env bash
# Instalación / actualización idempotente de Constructor IA (usuario, sin sudo).
# Uso: bash scripts/install.sh            (desde el directorio del código fuente)
# Variables: CIA_PREFIX (por defecto ~/constructor-ia)
set -euo pipefail
umask 077

PREFIX="${CIA_PREFIX:-$HOME/constructor-ia}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
PORT_DEFAULT=8766

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[aviso]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "No ejecutar como root: instale con su usuario normal."
case "$PREFIX" in "$HOME"/*) ;; *) die "CIA_PREFIX debe estar dentro de $HOME";; esac
[ "$SRC" != "$PREFIX/app" ] || die "Ejecute el instalador desde el código fuente, no desde $PREFIX/app"
[ -f "$SRC/constructor_ia/__init__.py" ] || die "No se encuentra el código fuente en $SRC"
case "$PREFIX" in */agente-constructor*|*/vigilancia*) die "Ruta de instalación no permitida";; esac

say "Comprobaciones previas (solo lectura)"
PY="$(command -v python3 || true)"
[ -n "$PY" ] || die "python3 no encontrado"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || die "Se requiere Python >= 3.11"
"$PY" -c 'import sqlite3, tomllib, hashlib; hashlib.scrypt' || die "Python sin sqlite3/tomllib/scrypt"
command -v bwrap >/dev/null || warn "bwrap no encontrado: la ejecución de pruebas quedará BLOQUEADA"
command -v systemd-run >/dev/null || warn "systemd-run no encontrado: la ejecución de pruebas quedará BLOQUEADA"
echo "  python: $("$PY" --version)  bwrap: $(bwrap --version 2>/dev/null || echo 'no')"

# Puerto: no reutilizar puertos ocupados por otros servicios.
PORT="$PORT_DEFAULT"
if [ -f "$PREFIX/config/config.toml" ]; then
  PORT="$("$PY" -c 'import sys,tomllib; print(tomllib.load(open(sys.argv[1],"rb")).get("server",{}).get("port",8766))' "$PREFIX/config/config.toml")"
fi
if command -v ss >/dev/null && ss -Hltn "sport = :$PORT" | grep -q .; then
  if systemctl --user is-active --quiet constructor-ia.service 2>/dev/null; then
    echo "  puerto $PORT en uso por Constructor IA (se reiniciará manualmente tras actualizar)"
  else
    die "El puerto $PORT ya está ocupado por otro servicio. Cambie server.port en $PREFIX/config/config.toml"
  fi
fi

say "Instalando en $PREFIX"
mkdir -p "$PREFIX" "$PREFIX/config" "$PREFIX/data" "$PREFIX/bin"
chmod 700 "$PREFIX" "$PREFIX/config" "$PREFIX/data"

STAGE="$PREFIX/app.new"
rm -rf "$STAGE"
mkdir -p "$STAGE"
for item in constructor_ia static config/config.example.toml systemd scripts tests \
            README.md ARCHITECTURE.md SECURITY.md INSTALL.md TESTING.md requirements.txt; do
  [ -e "$SRC/$item" ] || continue
  mkdir -p "$STAGE/$(dirname "$item")"
  cp -R "$SRC/$item" "$STAGE/$item"
done
find "$STAGE" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$STAGE" -type l -delete   # nunca copiar enlaces simbólicos
chmod -R u=rwX,go= "$STAGE"
if [ -d "$PREFIX/app" ]; then
  rm -rf "$PREFIX/app.prev"
  mv "$PREFIX/app" "$PREFIX/app.prev"
  echo "  versión anterior conservada en $PREFIX/app.prev (rollback)"
fi
mv "$STAGE" "$PREFIX/app"

if [ ! -x "$PREFIX/venv/bin/python3" ]; then
  say "Creando entorno virtual propio (sin pip: no hay dependencias externas)"
  "$PY" -m venv --without-pip "$PREFIX/venv"
fi
SITE="$("$PREFIX/venv/bin/python3" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
mkdir -p "$SITE"
printf '%s\n' "$PREFIX/app" > "$SITE/constructor_ia.pth"

install -m 700 "$PREFIX/app/scripts/constructor-ia.sh" "$PREFIX/bin/constructor-ia"
for s in diagnostico.sh update.sh rollback.sh uninstall.sh; do
  install -m 700 "$PREFIX/app/scripts/$s" "$PREFIX/bin/$s"
done

if [ ! -f "$PREFIX/config/config.toml" ]; then
  install -m 600 "$PREFIX/app/config/config.example.toml" "$PREFIX/config/config.toml"
  echo "  configuración creada: $PREFIX/config/config.toml"
else
  echo "  configuración existente conservada: $PREFIX/config/config.toml"
fi

say "Validando configuración"
CIA_BASE_DIR="$PREFIX" "$PREFIX/bin/constructor-ia" check-config >/dev/null
echo "  configuración válida"

say "Servicio systemd de usuario (instalado, NO habilitado ni iniciado)"
mkdir -p "$UNIT_DIR"
if [ -f "$UNIT_DIR/constructor-ia.service" ] && ! cmp -s "$PREFIX/app/systemd/constructor-ia.service" "$UNIT_DIR/constructor-ia.service"; then
  cp "$UNIT_DIR/constructor-ia.service" "$PREFIX/data/constructor-ia.service.bak"
fi
install -m 644 "$PREFIX/app/systemd/constructor-ia.service" "$UNIT_DIR/constructor-ia.service"
systemctl --user daemon-reload 2>/dev/null || warn "No se pudo recargar systemd --user (¿sesión sin gestor de usuario?)"

cat <<MSG

Instalación completada. Próximos pasos (ver INSTALL.md):
  1) Crear usuario web:        $PREFIX/bin/constructor-ia adduser diego
  2) Autodiagnóstico sandbox:  $PREFIX/bin/constructor-ia selftest
  3) Prueba en primer plano:   $PREFIX/bin/constructor-ia serve      (Ctrl+C para salir)
  4) Iniciar como servicio:    systemctl --user start constructor-ia
  5) Diagnóstico:              $PREFIX/bin/diagnostico.sh
  El arranque automático NO está habilitado. Cuando lo apruebe:
                               systemctl --user enable constructor-ia
MSG
