# Instalación y operación (por SSH)

Todo se instala como el usuario `diego`, **sin sudo**, en `~/constructor-ia/`.
No se tocan `~/agente-constructor/` (laboratorio), Node-RED, Vigilanti, MariaDB,
Tailscale, OpenClaw ni Ollama 11434. El instalador no instala paquetes del
sistema ni dependencias globales.

## 1. Requisitos (verificar, no instalar)

```bash
python3 --version            # >= 3.11 (Mint 22.3: 3.12.3)
bwrap --version              # 0.9.0
systemd-run --version | head -1
curl -s http://127.0.0.1:11435/api/tags | grep -o '"name":"[^"]*"'   # qwen3:4b-instruct, qwen3:1.7b
ss -ltn 'sport = :8766'      # debe estar vacío (puerto libre)
loginctl show-user "$USER" -p Linger   # ver §5
```

## 2. Obtener el código e instalar

```bash
mkdir -p ~/src
git clone -b claude/modest-clarke-1dzltx https://github.com/dieguinPinga/dieguinPinga.git ~/src/dieguinPinga
bash ~/src/dieguinPinga/constructor-ia/scripts/install.sh
```

Resultado:

```
~/constructor-ia/
  app/         código (copia limpia del repo)      app.prev/  versión anterior (rollback)
  venv/        entorno virtual propio (sin pip; no hay dependencias)
  bin/         constructor-ia, diagnostico.sh, update.sh, rollback.sh, uninstall.sh
  config/      config.toml (0600)
  data/        constructor.db, missions/, runs/, logs/, backups/  (0700)
~/.config/systemd/user/constructor-ia.service   (instalado, NO habilitado)
```

El instalador es idempotente: volver a ejecutarlo conserva `config/` y `data/`
y guarda la versión anterior en `app.prev/`. Si el puerto configurado está
ocupado por otro servicio, se detiene con un error.

## 3. Primer usuario y autodiagnóstico

```bash
~/constructor-ia/bin/constructor-ia adduser diego        # pide la contraseña (mín. 10 caracteres)
~/constructor-ia/bin/constructor-ia selftest             # debe terminar en "AISLAMIENTO OPERATIVO"
~/constructor-ia/bin/constructor-ia selftest --stress mem,fork,orphan   # límites reales (ver TESTING.md)
```

Si `selftest` falla, el servidor arranca igual pero **la ejecución de código
queda bloqueada** y las misiones fallan con el motivo. Causas típicas:
`Failed to connect to bus` (no hay gestor systemd de usuario en esa sesión:
ver §5), controladores `memory`/`pids`/`cpu` no delegados (ver `diagnostico.sh`).

Cambiar contraseña: `~/constructor-ia/bin/constructor-ia passwd diego` (cierra todas las sesiones).

## 4. Iniciar, detener, diagnosticar

```bash
# Prueba en primer plano (Ctrl+C detiene limpiamente)
~/constructor-ia/bin/constructor-ia serve

# Como servicio de usuario
systemctl --user start constructor-ia
systemctl --user status constructor-ia --no-pager
systemctl --user stop constructor-ia
journalctl --user -u constructor-ia -n 100 --no-pager
tail -f ~/constructor-ia/data/logs/constructor-ia.log

# Diagnóstico completo de solo lectura
~/constructor-ia/bin/diagnostico.sh
```

Dirección local: **http://127.0.0.1:8766**

Desde la PC sin exponer nada, con un túnel SSH:
`ssh -L 8766:127.0.0.1:8766 diego@<vivobook>` y abrir http://127.0.0.1:8766.

## 5. Arranque automático (solo cuando lo apruebes)

El servicio **no** se habilita en la instalación. Tras completar las pruebas de
TESTING.md:

```bash
systemctl --user enable constructor-ia
```

Para que un servicio de usuario corra sin sesión abierta hace falta *linger*.
Verificá con `loginctl show-user diego -p Linger`. Si ya es `yes` (probable si
`ollama-cpu.service` es un servicio de usuario), no hay nada que hacer. Si es
`no`, habilitarlo (`sudo loginctl enable-linger diego`) es una decisión tuya:
afecta solo al gestor de servicios de `diego`, pero es un cambio del sistema que
este proyecto no hace automáticamente.

## 6. Acceso seguro desde el celular por Tailscale

El servidor escucha solo en `127.0.0.1` por defecto. **El instalador no modifica
Tailscale.** Opciones, de menor a mayor intervención:

### Opción A (recomendada, sin tocar Tailscale): escuchar también en la IP Tailscale

```bash
tailscale ip -4          # solo lectura, p. ej. 100.101.102.103
nano ~/constructor-ia/config/config.toml
```

```toml
[server]
listen = ["127.0.0.1", "100.101.102.103"]
allowed_hosts = ["127.0.0.1", "localhost", "100.101.102.103"]
cookie_secure = false    # se accede por http://; el tráfico va cifrado por WireGuard
```

```bash
systemctl --user restart constructor-ia
```

Abrir en el celular (con Tailscale activo): **http://100.101.102.103:8766**

Justificación y alcance: la configuración rechaza `0.0.0.0` y cualquier IP que
no sea loopback o del rango Tailscale (100.64.0.0/10), así que el servicio no
queda en la LAN ni en Internet. El canal está cifrado de extremo a extremo por
WireGuard. Cualquier dispositivo de tu tailnet puede *llegar* al puerto, por eso
la autenticación es obligatoria, con límite de intentos, cookies HttpOnly y
SameSite=Strict, CSRF y verificación de Host/Origin. El socket usa
`IP_FREEBIND`, de modo que arranca aunque `tailscale0` todavía no esté listo.

Recomendado (decisión tuya, en el panel de Tailscale): una regla ACL que
permita el puerto 8766 de la VivoBook solo a tus dispositivos.

### Opción B: Tailscale Serve (HTTPS con certificado)

Da HTTPS y nombre `*.ts.net`, pero **modifica la configuración de Serve de
Tailscale**, que el mandato prohíbe tocar sin tu decisión. Si la tomás:

```bash
tailscale serve status                  # revisar que no pise una configuración existente
tailscale serve --bg --https=8443 http://127.0.0.1:8766
# NUNCA usar "tailscale funnel" (expondría a Internet).
```

Con esta opción dejar `listen = ["127.0.0.1"]` y `cookie_secure = true`; el
nombre `.ts.net` ya está en `allowed_hosts`. Para revertir:
`tailscale serve --https=8443 off`.

## 7. Actualización

```bash
cd ~/src/dieguinPinga && git pull
bash ~/src/dieguinPinga/constructor-ia/scripts/update.sh
```

`update.sh`: copia de seguridad de la base → detiene el servicio si estaba
activo → reinstala (conserva config y datos, guarda `app.prev`) → autodiagnóstico
→ vuelve a iniciar si estaba activo. Las migraciones SQLite se aplican solas al
arrancar (`PRAGMA user_version`).

## 8. Rollback

```bash
~/constructor-ia/bin/rollback.sh          # vuelve al código de app.prev
```

Si la versión nueva migró la base y la anterior no la entiende, restaurar
también la copia de seguridad (ver §10).

## 9. Desinstalación

```bash
~/constructor-ia/bin/uninstall.sh          # quita servicio, código, venv; conserva config/ y data/
~/constructor-ia/bin/uninstall.sh --purge  # además borra datos (pide escribir BORRAR)
```

Solo se tocan `~/constructor-ia/`, la unidad `constructor-ia.service` y las
unidades transitorias `cia-job-*.scope` propias.

## 10. Recuperación ante fallos

| Situación | Qué hacer |
|---|---|
| El servicio no arranca | `journalctl --user -u constructor-ia -n 50`; `constructor-ia check-config`; puerto ocupado → cambiar `server.port`. |
| Misiones quedaron en curso tras un corte | Al arrancar se marcan `interrumpida` (no se re-ejecutan solas). Desde la interfaz: *Reencolar (nueva misión)*. |
| Procesos de sandbox colgados | `systemctl --user list-units 'cia-job-*'`; se detienen solos al arrancar/parar; manual: `systemctl --user stop 'cia-job-*.scope'`. |
| Base dañada | `systemctl --user stop constructor-ia`; `cp ~/constructor-ia/data/backups/<copia>.db ~/constructor-ia/data/constructor.db`; `rm -f ~/constructor-ia/data/constructor.db-wal ~/constructor-ia/data/constructor.db-shm`; iniciar. |
| Copia manual de la base | `~/constructor-ia/bin/constructor-ia backup` (copia consistente con la API de SQLite). |
| Contraseña olvidada | `~/constructor-ia/bin/constructor-ia passwd diego`. |
| Ejecución bloqueada | Estado → *Repetir autodiagnóstico*, o `constructor-ia selftest` y ver qué comprobación falla. |
| `selftest` bloqueado con `Unknown assignment: OOMPolicy=continue` | systemd demasiado antiguo para `OOMPolicy` en scopes (Mint 22.3 trae 255, que sí lo admite). Comprobar: `systemd-run --user --scope -p OOMPolicy=continue true`. No desactivar el control: la ejecución permanece bloqueada (fallo cerrado). |
| Versión nueva defectuosa | `~/constructor-ia/bin/rollback.sh`. |
