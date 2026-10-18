# Seguridad: amenazas, controles y riesgos restantes

## Principios

1. **No es una terminal remota.** No existe ningún endpoint que ejecute
   comandos. El navegador nunca elige qué se ejecuta: el comando del sandbox es
   fijo (Python del sistema + arnés propio) y se construye como lista de
   argumentos, sin shell. Lo único variable es el *contenido* de `solucion.py`
   y los casos de prueba, que viajan como datos.
2. **Fallo cerrado.** Si no se puede verificar el aislamiento, la ejecución se
   bloquea y las misiones fallan con el motivo. Nunca se ejecuta “sin límites”.
3. **La aprobación depende de pruebas independientes**, definidas o revisadas
   por el operador, invisibles e inmodificables para el código generado.
4. **Ningún cambio en el anfitrión** desde la interfaz. Las aprobaciones humanas
   solo generan un paquete con SHA-256 para aplicar manualmente.

## Activos protegidos

Vigilanti (servidor, datos, grabaciones), Node-RED, MariaDB, Tailscale,
OpenClaw, Ollama 11434 y sus modelos, credenciales de `diego` (`~/.ssh`,
tokens), el laboratorio `~/agente-constructor/` y los datos de Constructor IA.

## Amenazas y controles

### A. Código generado malicioso o defectuoso (principal)

| Control | Detalle | Verificado |
|---|---|---|
| Namespaces | `bwrap --unshare-all --unshare-user --disable-userns` (usuario, PID, red, IPC, UTS, cgroup) | prueba `pid_namespace`, `userns_bloqueado` |
| Sin red | Espacio de red propio: solo `lo` vacío; sockets abstractos del anfitrión inaccesibles | `red_bloqueada` (1.1.1.1, 11434, 11435, 18789, 1880, 3306, 100.100.100.100) |
| Sistema de archivos mínimo | Raíz tmpfs remontada solo lectura; `/usr` ro; `/etc/ld.so.cache`; `/proc` propio; `/dev` mínimo; `/tmp` tmpfs de 32 MiB; código y arnés ro. **No** se montan `/home`, `/root`, `/run`, `/var`, `/sys`, sockets ni bases de datos | `rutas_sensibles_invisibles` (incluye `/home/diego/vigilancia`, `.ollama`, `.openclaw`, `/run/mysqld`, `docker.sock`), `escritura_solo_tmp` |
| Privilegios | `--cap-drop ALL`, `no_new_privs`, entorno vacío (`--clearenv`), `--new-session` (sin TIOCSTI) | `sin_capacidades`, `no_new_privs`, `entorno_limpio` |
| seccomp | Filtro BPF propio (x86_64): EPERM para ptrace, process_vm_*, bpf, io_uring, keyctl, perf_event_open, userfaultfd, mount/umount/pivot_root, unshare/setns, módulos, kexec, reboot, swap, open_by_handle_at, pidfd_getfd…; syscalls x32 y otra arquitectura bloqueadas | `seccomp_activo` |
| Recursos del grupo completo | Scope systemd con `MemoryMax=256M`, `MemorySwapMax=0`, `TasksMax=32`, `CPUQuota=50%`; **verificado leyendo el cgroup** antes de entregar trabajo | `limites_cgroup` (solo en la VivoBook) |
| Tiempo | Por caso (5 s, `RLIMIT_CPU`) y global (20 s); cancelación del operador | pruebas `test_infinite_loop…`, `test_global_timeout`, `test_cancel…` |
| Sin huérfanos | `--die-with-parent`; init del PID namespace; `systemctl --user kill/stop` de la unidad; verificación de `cgroup.procs` vacío; limpieza de `cia-job-*` al arrancar y en `ExecStopPost` | `limpieza_procesos`, `test_process_escape…` |
| Otros rlimits del hijo | core 0, archivos ≤ 8 MiB, 64 descriptores | — |
| Salidas acotadas | stdout ≤ 4 MiB, stderr ≤ 64 KiB en el anfitrión; 64/16 KiB en el arnés | `test_output_flood_is_bounded` |

### B. Manipulación de la evaluación por el código generado

- La solución corre en un **proceso separado** del arnés; se comunica solo por
  JSON en tuberías.
- Los casos (incluidos los ocultos) se entregan al arnés por stdin y solo viven
  en su memoria; el arnés se marca `PR_SET_DUMPABLE=0` (su `/proc/<pid>/mem`,
  `environ` y `fd/` quedan inaccesibles para el hijo; ptrace además bloqueado
  por seccomp).
- El resultado se firma con HMAC-SHA256 usando un nonce aleatorio por ejecución;
  imprimir líneas falsas o matar al arnés produce “resultado inválido”, nunca
  aprobación (`test_forged_result_is_rejected`).
- Los valores esperados de casos ocultos no se envían al modelo en las
  reparaciones.

### C. Acceso web no autorizado (tailnet, navegador, red local)

- Autenticación obligatoria en toda la API salvo `/api/login` y `/api/health`
  (que no revela nada). Contraseñas con scrypt (N=2^14, r=8), mínimo 10 caracteres.
- Sesiones: token de 256 bits en cookie `HttpOnly; SameSite=Strict; Secure`
  (configurable), guardado como SHA-256; expiración por inactividad (12 h) y
  absoluta (7 días); cambiar la contraseña cierra todas las sesiones.
- CSRF: token por sesión en cabecera `X-CSRF-Token` para todo POST, más
  verificación de `Origin`/`Sec-Fetch-Site` y exigencia de `application/json`.
- Anti DNS-rebinding: `Host` debe estar en `allowed_hosts`.
- Fuerza bruta: retraso exponencial por IP + límite global (30 fallos / 5 min),
  porque detrás de un proxy todo llega desde 127.0.0.1. Fallos auditados.
- CSP estricta sin scripts ni estilos en línea, `frame-ancestors 'none'`,
  `nosniff`, `no-referrer`, COOP/CORP. El frontend inserta datos solo con
  `textContent` (sin `innerHTML`).
- Escucha solo en loopback o IP Tailscale; `0.0.0.0`, `::` y LAN se rechazan
  en la configuración.
- Límites de conexiones, clientes SSE, tamaño de cuerpo, cola (20 pendientes) y
  respuestas de chat en espera (4).

### D. Rutas y archivos

- El navegador solo referencia archivos por **id numérico**. Las rutas
  relativas las genera el servidor con patrones fijos y se validan con regex.
- Lectura con `O_NOFOLLOW`, verificación de que la ruta real sigue dentro de la
  misión y comprobación del SHA-256 registrado (un archivo alterado en disco se
  rechaza).
- Identificadores de misión validados (`M\d{8}-[0-9a-f]{6}`).
- Directorios de datos `0700`, archivos `0600`, `UMask=0077`.
- Directorios temporales de ejecución con nombre aleatorio, creados con
  `O_EXCL`, eliminados al terminar y al arrancar.

### E. Secretos y registros

- Ningún secreto en el repositorio ni en `config.toml` (los usuarios se crean
  por CLI). `.gitignore` excluye config real, base y registros.
- El registro no incluye cuerpos, cookies ni query strings; además un filtro
  redacta patrones `password=`, `token`, `csrf`, `cookie`, `cia_session=`.
- `/api/status` no expone secretos (probado).

### F. Servicios existentes

- Puertos reservados rechazados en la configuración: 1880, 3306, 11434, 11435, 18789.
- `ollama.url` solo puede ser loopback y nunca 11434.
- Ningún script detiene, reinicia ni reconfigura otros servicios;
  `diagnostico.sh` solo consulta (`is-active`).
- El servicio propio tiene prioridad baja (`CPUWeight=50`, `IOWeight=50`, `Nice=5`) y límites de memoria.

## Riesgos restantes (honestos)

1. **El aislamiento no es una garantía frente a un atacante con un 0-day del
   kernel.** Bubblewrap + seccomp + cgroups reducen mucho la superficie, pero
   comparten kernel con el anfitrión. Para código abiertamente hostil, una VM
   (p. ej. Firecracker/QEMU) sería la siguiente capa.
2. **Superficie de lectura dentro del sandbox**: `/usr` completo es visible
   (incluye `/usr/local` y `/usr/share`) y `/proc` propio expone información
   del kernel (versión, `cpuinfo`, `meminfo`). No contiene credenciales.
3. **El filtro seccomp es una lista de denegación**, no de permisos. Bloquea
   las llamadas más peligrosas conocidas; llamadas nuevas del kernel no listadas
   se permiten.
4. **Límites de cgroup no comprobados en este entorno de desarrollo**: aquí no
   hay gestor systemd de usuario. El código verifica los valores reales y
   bloquea si faltan; la eficacia (OOM, fork bomb) debe confirmarse en la
   VivoBook con `selftest --stress mem,fork,orphan`.
5. **Sin `NoNewPrivileges` en la unidad del servidor**, a propósito: en Ubuntu
   24.04 impediría la transición al perfil AppArmor que habilita a `bwrap`.
   `bwrap` aplica `no_new_privs` dentro del sandbox (verificado).
6. **HTTP sin TLS en la opción A de Tailscale**: el cifrado lo da WireGuard;
   cualquier nodo de tu tailnet puede intentar autenticarse. Mitigar con ACL de Tailscale.
7. **Un único rol de usuario**: todo usuario autenticado ve todas las misiones
   (pensado para un solo operador).
8. **Pruebas avanzadas en Python** las escribe el operador y corren dentro del
   arnés (en el sandbox). Son código confiable del operador; no pegar código de
   terceros sin revisarlo.
9. **Calidad del modelo**: un modelo de 4B puede no resolver tareas complejas;
   el sistema no lo oculta: falla tras los intentos configurados.
10. **Prompt injection en el chat**: el chat no tiene herramientas ni puede
    ejecutar nada; el peor caso es una respuesta engañosa.

## Auditoría del prototipo (`~/agente-constructor/`)

No se copió código del prototipo (no está en el repositorio; se rediseñó desde
la descripción). Puntos del mandato y cómo se resuelven aquí:

| Punto | Resolución |
|---|---|
| Cancelación de todo el grupo | Unidad `cia-job-*.scope` matada con `systemctl kill`, `killpg`, `--die-with-parent`; verificación de cgroup vacío. |
| Límites verificables | Lectura de `memory.max`, `pids.max`, `cpu.max` del cgroup real antes de ejecutar. |
| Aislamiento de archivos y dispositivos | Raíz vacía ro, sin `/home`, `/dev` mínimo, pruebas de visibilidad en el autodiagnóstico. |
| Separación código/pruebas | Pruebas solo en memoria del arnés; solución en proceso aparte. |
| Modificación de pruebas por el agente | Imposible: el modelo solo produce texto; las pruebas no están en disco ni son escribibles. |
| Enlaces simbólicos | `O_NOFOLLOW`, sin symlinks en el instalador, validación de rutas. |
| Temporales | Directorios aleatorios 0700, `O_EXCL`, borrado garantizado. |
| Concurrencia | Una ejecución a la vez (lock) y una inferencia a la vez (gate); reclamo atómico en SQLite. |
| Inyección de comandos | Sin shell; argumentos fijos; nombre de unidad validado con regex. |
| Privilegios mínimos | Usuario normal, sin sudo, sin capacidades, userns anidados bloqueados. |
