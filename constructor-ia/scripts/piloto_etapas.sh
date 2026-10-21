#!/usr/bin/env bash
# Etapas del ensayo técnico del piloto en la VivoBook (PILOTO.md, Anexo 3). Se corre desde el checkout aparte.
#
#   huellas antes|despues  — SOLO LECTURA de la instalación: huellas de app y config, servicios en marcha, puertos.
#                            Escribe únicamente en la carpeta del piloto.
#   entorno                — comprueba que el piloto usa su propia configuración, datos y MariaDB privado.
#   ensayo [minutos]       — el ÚNICO paso que detiene algo: detiene constructor-ia, corre el ensayo técnico con
#                            un límite de tiempo y SIEMPRE lo vuelve a arrancar (trampa + temporizador de respaldo).
#   revisar [carpeta]      — SOLO LECTURA: resultados, prompts, respuestas crudas y autoría del último ensayo.
#   comparar               — SOLO LECTURA: huellas antes contra después.
#
# No toca Vigilancia, Node-RED, OpenClaw, Tailscale, Ollama, el MariaDB instalado ni las bases productivas.
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE="${CIA_PILOTO_DIR:-$HOME/constructor-ia-piloto}"
INSTALADA="${CIA_PREFIX:-$HOME/constructor-ia}"
PILOTO_SH="${PILOTO_SH:-$SRC/scripts/piloto.sh}"
SERVICIO="constructor-ia.service"
UNIDAD="${PILOTO_UNIDAD:-m}"   # solo para las pruebas automáticas («s»); en la VivoBook, minutos
RESPALDO="cia-piloto-restaurar"
HUELLAS="$BASE/huellas"

die() { echo "[detenido] $*" >&2; exit 1; }

huella_app() {
  [ -d "$INSTALADA/app" ] || { echo "sin-app"; return; }
  (cd "$INSTALADA/app" && find . -type f -not -path '*/__pycache__/*' -print0 | sort -z | xargs -0 sha256sum) \
    | sha256sum | cut -d' ' -f1
}

huella_db() {
  local f out=""
  for f in "$INSTALADA/data/constructor.db" "$INSTALADA/data/constructor.db-wal"; do
    [ -f "$f" ] && out="$out $(sha256sum "$f" | cut -d' ' -f1)"
  done
  echo "${out:- sin-base}"
}

cmd_huellas() {
  local fase="${1:-}"
  case "$fase" in antes|despues) ;; *) die "uso: huellas antes|despues" ;; esac
  mkdir -p "$HUELLAS/$fase"
  chmod 700 "$BASE" "$HUELLAS" 2>/dev/null || true
  huella_app > "$HUELLAS/$fase/app.txt"
  if [ -f "$INSTALADA/config/config.toml" ]; then
    sha256sum "$INSTALADA/config/config.toml" | cut -d' ' -f1 > "$HUELLAS/$fase/config.txt"
  else
    echo sin-config > "$HUELLAS/$fase/config.txt"
  fi
  systemctl --user list-units --type=service --state=running --no-legend --plain 2>/dev/null \
    | awk '{print $1}' | sort > "$HUELLAS/$fase/servicios-usuario.txt" || true
  systemctl list-units --type=service --state=running --no-legend --plain 2>/dev/null \
    | awk '{print $1}' | sort > "$HUELLAS/$fase/servicios-sistema.txt" || true
  ss -ltnH 2>/dev/null | awk '{print $4}' | sort -u > "$HUELLAS/$fase/puertos.txt" || true
  systemctl --user is-active "$SERVICIO" > "$HUELLAS/$fase/constructor-ia.txt" 2>/dev/null || true
  echo "Huellas «$fase» guardadas en $HUELLAS/$fase:"
  echo "  app: $(cat "$HUELLAS/$fase/app.txt")"
  echo "  config: $(cat "$HUELLAS/$fase/config.txt")"
  echo "  constructor-ia: $(cat "$HUELLAS/$fase/constructor-ia.txt")"
  echo "  servicios en marcha: $(wc -l < "$HUELLAS/$fase/servicios-usuario.txt") de usuario," \
       "$(wc -l < "$HUELLAS/$fase/servicios-sistema.txt") del sistema; puertos: $(wc -l < "$HUELLAS/$fase/puertos.txt")"
}

cmd_entorno() {
  echo "== Código y datos del piloto"
  bash "$PILOTO_SH" check-config 2>&1 | grep -E '\[piloto\]|"data_dir"|"ollama_url"' || true
  echo "== Binarios para el MariaDB privado (se usan con --no-defaults, datadir y socket propios, sin red)"
  for b in mariadbd mariadb-install-db mariadb bwrap systemd-run; do
    printf '  %-20s %s\n' "$b" "$(command -v "$b" || echo 'NO ENCONTRADO')"
  done
  echo "== Modelo en Ollama CPU (solo lectura)"
  "${CIA_PYTHON:-$INSTALADA/venv/bin/python3}" -c '
import json, urllib.request
try:
    tags = json.load(urllib.request.urlopen("http://127.0.0.1:11435/api/tags", timeout=5))["models"]
    names = [m["name"] for m in tags]
    print("  qwen3:4b-instruct:", "instalado" if "qwen3:4b-instruct" in names else "NO instalado", "·", names)
except Exception as e:
    print("  Ollama CPU no respondió:", e)'
  echo "== Aislamiento con la carpeta del piloto"
  bash "$PILOTO_SH" selftest
}

# Estado del ensayo: GLOBAL a propósito. La trampa de salida corre después de que cmd_ensayo terminó; con variables
# locales ya no existirían y el servicio quedaría detenido (lo detectó tests/test_piloto.py).
ACTIVO=0
DB_ANTES=""
PID_ENSAYO=""

cmd_ensayo() {
  local limite="${1:-90}" rc=0
  [[ "$limite" =~ ^[0-9]+$ ]] && [ "$limite" -ge 10 ] && [ "$limite" -le 180 ] || die "límite en minutos: 10 a 180"
  mkdir -p "$BASE"
  exec 9>"$BASE/.ensayo.lock"
  flock -n 9 || die "ya hay un ensayo en curso"
  systemctl --user is-active --quiet "$SERVICIO" && ACTIVO=1

  restaurar() {
    trap - EXIT INT TERM HUP
    if [ -n "$PID_ENSAYO" ] && kill -0 "$PID_ENSAYO" 2>/dev/null; then
      echo "[piloto] interrumpiendo el ensayo (SIGINT: limpia y detiene su MariaDB privado)…"
      kill -INT "$PID_ENSAYO" 2>/dev/null || true
      for _ in $(seq 1 90); do kill -0 "$PID_ENSAYO" 2>/dev/null || break; sleep 1; done
      kill -KILL "$PID_ENSAYO" 2>/dev/null || true
    fi
    if [ "$ACTIVO" = 1 ]; then
      if [ -n "$DB_ANTES" ]; then
        if [ "$(huella_db)" = "$DB_ANTES" ]; then echo "[piloto] base productiva: sin cambios (huella idéntica)"
        else echo "[ATENCIÓN] la huella de la base productiva cambió mientras el servicio estaba detenido"; fi
      fi
      echo "[piloto] volviendo a arrancar $SERVICIO…"
      systemctl --user start "$SERVICIO" || true
      sleep 3
      if systemctl --user is-active --quiet "$SERVICIO"; then
        echo "[piloto] $SERVICIO activo de nuevo"
        systemctl --user stop "$RESPALDO.timer" 2>/dev/null || true
      else
        echo "[ERROR] $SERVICIO no arrancó; revisar: systemctl --user status $SERVICIO" \
             "(el temporizador de respaldo $RESPALDO.timer queda armado)"
      fi
    fi
  }
  trap restaurar EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap 'exit 129' HUP

  if [ "$ACTIVO" = 1 ]; then
    systemctl --user stop "$RESPALDO.timer" 2>/dev/null || true
    systemctl --user reset-failed "$RESPALDO.service" 2>/dev/null || true
    # Respaldo: si este script muriera sin poder reaccionar (kill -9), systemd arranca el servicio igual.
    systemd-run --user --unit="$RESPALDO" --on-active="$((limite + 5))$UNIDAD" \
      --description="Restaurar constructor-ia tras el ensayo del piloto" \
      "$(command -v systemctl)" --user start "$SERVICIO" >/dev/null \
      || { ACTIVO=0; die "no se pudo armar el temporizador de respaldo: no se detiene nada"; }
    echo "[piloto] temporizador de respaldo armado: arranca $SERVICIO en $((limite + 5)) min pase lo que pase"
    echo "[piloto] deteniendo SOLO $SERVICIO mientras dura el ensayo (límite: $limite min)"
    systemctl --user stop "$SERVICIO"
    DB_ANTES="$(huella_db)"
  else
    echo "[piloto] $SERVICIO no estaba activo: no se detiene ni se arranca nada"
  fi
  timeout --signal=INT --kill-after=120 "${limite}$UNIDAD" bash "$PILOTO_SH" laboratorio-piloto --yes --ensayo &
  PID_ENSAYO=$!
  wait "$PID_ENSAYO" || rc=$?
  PID_ENSAYO=""
  case "$rc" in
    0) echo "[piloto] ensayo terminado" ;;
    124) echo "[piloto] el ensayo alcanzó el límite de $limite min y se interrumpió (es reanudable)" ;;
    *) echo "[piloto] el ensayo terminó con código $rc" ;;
  esac
  return "$rc"
}

ultima_carpeta() {
  ls -1d "$BASE"/data/laboratorio/ensayo-* 2>/dev/null | tail -1
}

cmd_revisar() {
  local carpeta="${1:-$(ultima_carpeta)}"
  [ -n "$carpeta" ] && [ -d "$carpeta" ] || die "no hay carpetas de ensayo en $BASE/data/laboratorio"
  bash "$PILOTO_SH" laboratorio-piloto-revisar "$carpeta" --largo "${LARGO:-600}"
  echo "== Procesos del MariaDB privado que hayan quedado (debería no haber ninguno)"
  pgrep -af "mariadbd.*$BASE" || echo "  ninguno"
}

cmd_comparar() {
  local a="$HUELLAS/antes" d="$HUELLAS/despues" ok=1 f
  [ -d "$a" ] && [ -d "$d" ] || die "faltan huellas: correr «huellas antes» y «huellas despues»"
  for f in app config constructor-ia; do
    if cmp -s "$a/$f.txt" "$d/$f.txt"; then echo "  $f: igual"
    else echo "  $f: DISTINTO ($(cat "$a/$f.txt") → $(cat "$d/$f.txt"))"; ok=0; fi
  done
  for f in servicios-usuario servicios-sistema puertos; do
    if cmp -s "$a/$f.txt" "$d/$f.txt"; then echo "  $f: igual"
    else echo "  $f: diferencias (− antes, + después):"; diff "$a/$f.txt" "$d/$f.txt" | grep '^[<>]' \
      | sed 's/^</    −/; s/^>/    +/' || true; ok=0; fi
  done
  [ "$ok" = 1 ] && echo "RESULTADO: la instalación y los servicios quedaron como estaban" \
    || echo "RESULTADO: hay diferencias; revisarlas (un servicio ajeno puede haber cambiado por su cuenta)"
}

case "${1:-}" in
  huellas) shift; cmd_huellas "$@" ;;
  entorno) cmd_entorno ;;
  ensayo) shift; cmd_ensayo "$@" ;;
  revisar) shift; cmd_revisar "$@" ;;
  comparar) cmd_comparar ;;
  *) sed -n '2,13p' "$0"; exit 2 ;;
esac
