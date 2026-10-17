#!/usr/bin/env bash
# Fase 1 — Relevamiento de SOLO LECTURA para la estación de vigilancia.
# No instala, no modifica configuración, no graba audio ni video, no usa sudo.
# Uso:  bash relevamiento.sh 2>&1 | tee ~/relevamiento-vigilancia.txt
# Revisá la salida antes de compartirla (contiene IPs de Tailscale y nombres de procesos).

set -u
export LC_ALL=C
T="timeout 8"
sec() { printf '\n===== %s =====\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }
run() { echo "\$ $*"; $T "$@" 2>&1 || echo "(código de salida $?)"; }

VENV_PY="${VENV_PY:-/home/diego/vad-venv/bin/python}"

sec "Fecha / sistema"
run date -Is
run uname -a
cat /etc/os-release 2>/dev/null | grep -E '^(NAME|VERSION|UBUNTU_CODENAME)='
run timedatectl show -p Timezone -p NTPSynchronized
run id
run uptime

sec "CPU / memoria / temperatura / energía"
lscpu 2>/dev/null | grep -E 'Model name|^CPU\(s\)|Thread|MHz'
run free -h
cat /proc/pressure/cpu /proc/pressure/memory 2>/dev/null
have sensors && run sensors
for z in /sys/class/thermal/thermal_zone*; do
  [ -r "$z/temp" ] && echo "$(cat "$z/type" 2>/dev/null): $(( $(cat "$z/temp") / 1000 ))°C"
done
for p in /sys/class/power_supply/*; do
  [ -r "$p/type" ] && echo "$(basename "$p") type=$(cat "$p/type") online=$(cat "$p/online" 2>/dev/null) status=$(cat "$p/status" 2>/dev/null) cap=$(cat "$p/capacity" 2>/dev/null)"
done
grep -hE '^\s*#?\s*HandleLidSwitch' /etc/systemd/logind.conf /etc/systemd/logind.conf.d/*.conf 2>/dev/null

sec "Procesos que más consumen"
ps -eo pid,user,pcpu,pmem,rss,etime,comm --sort=-pcpu | head -20
echo
ps -eo pid,user,pcpu,pmem,rss,comm --sort=-rss | head -12

sec "Almacenamiento"
run df -h / /home
lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINT,MODEL 2>/dev/null
ls -ld /home/diego/vigilancia* 2>/dev/null || echo "No existe aún /home/diego/vigilancia*"

sec "Puertos en escucha"
run ss -tlnp
run ss -ulnp

sec "Servicios relevantes (solo consulta)"
systemctl list-units --type=service --state=running --no-pager 2>/dev/null | grep -Ei 'node-?red|ollama|tailscale|pipewire|pulse|mosquitto|nginx|caddy|apache|motion|docker' || true
systemctl --user list-units --type=service --state=running --no-pager 2>/dev/null | head -40
run loginctl show-user "$(id -un)" -p Linger

sec "Tailscale (solo lectura)"
if have tailscale; then
  run tailscale version
  run tailscale ip -4
  run tailscale serve status
else echo "tailscale no está en PATH"; fi

sec "Ollama (solo lectura)"
if have curl; then run curl -s --max-time 3 http://127.0.0.1:11434/api/tags; fi
have ollama && run ollama list

sec "Node-RED (solo detección, no se toca)"
pgrep -a -f node-red || echo "Node-RED no detectado como proceso"

sec "Cámara"
ls -l /dev/video* 2>/dev/null
lsusb 2>/dev/null | grep -i -E '13d3|cam|video'
if have v4l2-ctl; then
  run v4l2-ctl --list-devices
  for d in /dev/video*; do
    echo "--- $d"
    $T v4l2-ctl -d "$d" --info 2>&1 | grep -E 'Driver name|Card type|Bus info|Device Caps' -A1 | head -8
    $T v4l2-ctl -d "$d" --list-formats-ext 2>&1 | head -40
  done
  run v4l2-ctl -d /dev/video0 --list-ctrls
fi
have fuser && { echo "Procesos usando la cámara:"; fuser -v /dev/video* 2>&1 || echo "(ninguno visible)"; }
for d in /sys/bus/usb/devices/*; do
  if [ "$(cat "$d/idVendor" 2>/dev/null)" = "13d3" ]; then
    echo "USB $(basename "$d") $(cat "$d/idVendor"):$(cat "$d/idProduct") autosuspend=$(cat "$d/power/control" 2>/dev/null)"
  fi
done

sec "Audio (PipeWire/PulseAudio, solo consulta)"
have pactl && { run pactl info; run pactl list short sources; }
have wpctl && run wpctl status

sec "FFmpeg y aceleración por hardware"
if have ffmpeg; then
  ffmpeg -hide_banner -version | head -1
  ffmpeg -hide_banner -encoders 2>/dev/null | grep -E 'libx264|h264_vaapi|h264_qsv|aac|libopus|mjpeg'
  ffmpeg -hide_banner -hwaccels 2>/dev/null
fi
ls -l /dev/dri 2>/dev/null
have vainfo && $T vainfo 2>&1 | grep -E 'Driver version|VAProfileH264' | head -8
lspci 2>/dev/null | grep -Ei 'vga|3d|display'

sec "Python"
for py in /usr/bin/python3 "$VENV_PY"; do
  [ -x "$py" ] || { echo "$py no existe"; continue; }
  echo "--- $py"
  # -B: no escribir .pyc; -I: aislado del directorio actual
  $T "$py" -B -I - <<'PY' 2>&1
import sys, importlib, sqlite3
print("python", sys.version.split()[0], "prefix", sys.prefix)
print("sqlite", sqlite3.sqlite_version, end=" ")
try:
    sqlite3.connect(":memory:").execute("create virtual table t using fts5(x)"); print("FTS5=si")
except Exception as e: print("FTS5=no", e)
for m in ["numpy","cv2","webrtcvad","faster_whisper","ctranslate2","av","fastapi","uvicorn","starlette","jinja2","argon2","bcrypt","onnxruntime","ai_edge_litert","tflite_runtime","sounddevice","paho.mqtt"]:
    try:
        mod = importlib.import_module(m); print(f"  {m:16} {getattr(mod,'__version__','ok')}")
    except Exception as e: print(f"  {m:16} -- ({type(e).__name__})")
PY
done
[ -x "$VENV_PY" ] && $T "$VENV_PY" -B -m pip list --disable-pip-version-check 2>/dev/null | head -80
dpkg -l python3-venv python3-dev sqlite3 v4l-utils ffmpeg psmisc vainfo intel-media-va-driver i965-va-driver 2>/dev/null | grep '^ii' | awk '{print $2, $3}'

sec "Modelos Whisper en caché"
ls -d ~/.cache/huggingface/hub/*whisper* 2>/dev/null && du -sh ~/.cache/huggingface/hub/*whisper* 2>/dev/null || echo "sin caché HF de whisper"

sec "Experimentos previos (solo listado)"
find ~ -maxdepth 4 -name '*.py' -not -path '*/.*' -not -path '*/site-packages/*' -not -path '*venv*' -print0 2>/dev/null \
  | xargs -0 -r grep -l -E 'webrtcvad|faster_whisper|parec|cv2\.VideoCapture|/dev/video' 2>/dev/null | head -30

echo; echo "Relevamiento terminado. No se modificó nada."
