#!/usr/bin/env bash
# Corre los comandos del piloto DESDE ESTE CHECKOUT, sin instalar nada.
#
# No toca: ~/constructor-ia/app (la versión instalada), su base SQLite, su configuración, el servicio systemd ni
# el MariaDB instalado. Usa una carpeta propia (por defecto ~/constructor-ia-piloto) con su propia configuración y
# sus propios datos, y el intérprete del entorno virtual existente solo para EJECUTAR (no le instala nada).
#
# Uso:  bash <checkout>/constructor-ia/scripts/piloto.sh selftest
#       bash <checkout>/constructor-ia/scripts/piloto.sh laboratorio-piloto --yes --ensayo
# Para deshacer todo: borrar el checkout y la carpeta del piloto (ver PILOTO.md, «Instalación sin instalar»).
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE="${CIA_PILOTO_DIR:-$HOME/constructor-ia-piloto}"
PY="${CIA_PYTHON:-$HOME/constructor-ia/venv/bin/python3}"
INSTALADA="${CIA_PREFIX:-$HOME/constructor-ia}"

case "$SRC/" in
  "$INSTALADA"/*) echo "piloto.sh debe correr desde un checkout aparte, no desde la instalación ($INSTALADA)"; exit 1 ;;
esac
case "$BASE/" in
  "$INSTALADA"/*) echo "La carpeta del piloto no puede estar dentro de la instalación ($INSTALADA)"; exit 1 ;;
esac
[ -x "$PY" ] || PY="$(command -v python3)"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || {
  echo "Hace falta Python >= 3.11 (el del entorno virtual de Constructor IA: $HOME/constructor-ia/venv/bin/python3)"
  exit 1; }

mkdir -p "$BASE/data"
chmod 700 "$BASE" "$BASE/data"
if [ ! -f "$BASE/config.toml" ]; then
  cat > "$BASE/config.toml" <<EOF
# Configuración SOLO del piloto (independiente de $INSTALADA/config/config.toml).
[server]
data_dir = "$BASE/data"

[ollama]
# Ollama CPU (la instancia principal 11434 está prohibida por la configuración).
url = "http://127.0.0.1:11435"
EOF
  chmod 600 "$BASE/config.toml"
  echo "Configuración del piloto creada en $BASE/config.toml"
fi

export CIA_BASE_DIR="$BASE" CIA_CONFIG="$BASE/config.toml"
# -I: modo aislado (sin PYTHONPATH ni sitio de usuario). El checkout va PRIMERO en sys.path y se comprueba que el
# código cargado sea el de este checkout y no el instalado.
exec "$PY" -I -c '
import sys
src = sys.argv.pop(1)
sys.path.insert(0, src)
import constructor_ia
if not constructor_ia.__file__.startswith(src + "/"):
    sys.exit(f"se cargó otro código ({constructor_ia.__file__}); se esperaba {src}")
import os
datos = os.environ.get("CIA_BASE_DIR", "?") + "/data"
print(f"[piloto] código: {src} · versión {constructor_ia.VERSION} · datos: {datos}", file=sys.stderr)
from constructor_ia.cli import main
sys.exit(main())
' "$SRC" "$@"
