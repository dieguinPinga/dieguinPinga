#!/usr/bin/env bash
# Diagnóstico de SOLO LECTURA de Constructor IA y su entorno.
# No modifica, detiene ni reinicia ningún servicio.
PREFIX="${CIA_PREFIX:-$HOME/constructor-ia}"
UIDN="$(id -u)"
sec() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
ok()  { printf '  [OK]    %s\n' "$*"; }
bad() { printf '  [FALLA] %s\n' "$*"; }
inf() { printf '  %s\n' "$*"; }

sec "Instalación"
[ -d "$PREFIX/app" ] && ok "código en $PREFIX/app" || bad "falta $PREFIX/app"
[ -x "$PREFIX/venv/bin/python3" ] && ok "venv: $("$PREFIX/venv/bin/python3" --version)" || bad "falta el venv"
[ -f "$PREFIX/config/config.toml" ] && ok "config: $(stat -c '%a' "$PREFIX/config/config.toml") $PREFIX/config/config.toml" || bad "falta config.toml"
[ -d "$PREFIX/data" ] && inf "datos: $(du -sh "$PREFIX/data" 2>/dev/null | cut -f1) (permisos $(stat -c '%a' "$PREFIX/data"))"
"$PREFIX/bin/constructor-ia" check-config >/dev/null 2>&1 && ok "configuración válida" || bad "configuración inválida: $PREFIX/bin/constructor-ia check-config"
PORT="$("$PREFIX/bin/constructor-ia" check-config 2>/dev/null | "$PREFIX/venv/bin/python3" -c 'import json,sys; print(json.load(sys.stdin)["port"])' 2>/dev/null || echo 8766)"

sec "Servicio"
inf "estado:     $(systemctl --user is-active constructor-ia.service 2>/dev/null)"
inf "habilitado: $(systemctl --user is-enabled constructor-ia.service 2>/dev/null)"
inf "linger:     $(loginctl show-user "$USER" -p Linger --value 2>/dev/null) (yes = los servicios de usuario siguen sin sesión abierta)"
if command -v ss >/dev/null; then
  L="$(ss -Hltn "sport = :$PORT" 2>/dev/null)"
  [ -n "$L" ] && inf "escuchando: $(echo "$L" | awk '{print $4}' | tr '\n' ' ')" || inf "nadie escucha en :$PORT"
fi
if command -v curl >/dev/null; then
  curl -fsS -m 3 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && ok "responde en http://127.0.0.1:$PORT" || inf "no responde en 127.0.0.1:$PORT"
fi
inf "unidades de sandbox activas: $(systemctl --user list-units --plain --no-legend 'cia-job-*.scope' 2>/dev/null | wc -l)"

sec "Taller Web"
PPORT="$("$PREFIX/venv/bin/python3" -c 'import sys,tomllib; print(tomllib.load(open(sys.argv[1],"rb")).get("web",{}).get("preview_port",8767))' "$PREFIX/config/config.toml" 2>/dev/null || echo 8767)"
if command -v ss >/dev/null; then
  L="$(ss -Hltn "sport = :$PPORT" 2>/dev/null)"
  [ -n "$L" ] && inf "vista previa escuchando: $(echo "$L" | awk '{print $4}' | tr '\n' ' ')" || inf "vista previa: nadie escucha en :$PPORT"
fi
[ -x /usr/bin/node ] && ok "node para verificación de sintaxis: $(/usr/bin/node --version)" || inf "sin /usr/bin/node: la sintaxis JS se verifica con heurística"

sec "Ollama CPU (11435)"
if command -v curl >/dev/null; then
  V="$(curl -fsS -m 3 http://127.0.0.1:11435/api/version 2>/dev/null)" && ok "disponible $V" || bad "no responde en 127.0.0.1:11435"
  curl -fsS -m 3 http://127.0.0.1:11435/api/tags 2>/dev/null | grep -o '"name":"[^"]*"' | sed 's/^/  modelo: /'
fi

sec "Aislamiento"
command -v bwrap >/dev/null && ok "bwrap $(bwrap --version 2>/dev/null)" || bad "bwrap no instalado"
command -v systemd-run >/dev/null && ok "$(systemd-run --version | head -1)" || bad "systemd-run no disponible"
inf "userns sin privilegios: $(cat /proc/sys/kernel/unprivileged_userns_clone 2>/dev/null || echo n/d) ; restricción AppArmor: $(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns 2>/dev/null || echo n/d)"
CG="/sys/fs/cgroup/user.slice/user-$UIDN.slice/user@$UIDN.service"
inf "controladores delegados al usuario: $(cat "$CG/cgroup.subtree_control" 2>/dev/null || echo 'n/d')"
"$PREFIX/bin/constructor-ia" selftest 2>&1 | sed 's/^/  /'

sec "Servicios existentes (consulta de solo lectura)"
for u in nodered node-red vigilanti ollama ollama-cpu tailscaled mariadb openclaw; do
  s="$(systemctl is-active "$u" 2>/dev/null)"; us="$(systemctl --user is-active "$u" 2>/dev/null)"
  [ "$s" = "active" ] || [ "$us" = "active" ] && inf "$u: sistema=$s usuario=$us"
done

sec "Recursos"
free -h | sed 's/^/  /'
inf "carga: $(cut -d' ' -f1-3 /proc/loadavg)"

sec "Últimos registros"
tail -n 15 "$PREFIX/data/logs/constructor-ia.log" 2>/dev/null | sed 's/^/  /' || inf "sin registros"
