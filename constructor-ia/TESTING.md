# Pruebas

## Pruebas automáticas

Sin dependencias (unittest de la biblioteca estándar):

```bash
cd ~/constructor-ia/app            # o el checkout: constructor-ia/
python3 -m unittest discover -s tests -t . -v
```

| Archivo | Cubre |
|---|---|
| `tests/test_units.py` (26) | Configuración y límites (rechazo de 0.0.0.0, LAN, puertos 1880/3306/11434/11435/18789, Ollama 11434), scrypt, sesiones, expiración, invalidación por cambio de contraseña, limitador de intentos, validación de misiones, extracción y validación de código, presupuesto de prompt, no filtración de casos ocultos, estructura del filtro seccomp, comando y verificación del limitador cgroup (con archivos de cgroup simulados), redacción de secretos, parser de borradores. |
| `tests/test_sandbox.py` (23) | Ejecutor con **Bubblewrap y seccomp reales** frente a entradas adversariales: solución correcta/incorrecta, errores de sintaxis e importación, excepciones esperadas, bucle infinito, bloqueo a nivel de módulo, intento de leer casos ocultos / memoria del arnés / `/home`, red (1.1.1.1, 11434, 11435, 1880), escritura fuera de `/tmp`, falsificación de resultados y muerte del arnés, proceso nieto con `setsid` + `os._exit`, ptrace/userns, inundación de salida, modo programa, pruebas del operador, cancelación, timeout global, HMAC adulterado, limpieza de temporales, bloqueo si los límites no se verifican. |
| `tests/test_engine.py` (10) | Motor y cola con Ollama simulado: aprobación al primer intento sin re-ejecución, reparación con errores reales, fallo tras 3 intentos, cancelación durante la generación (Ollama recibe la desconexión), cancelación en cola y orden FIFO con una sola inferencia simultánea, Ollama caído, ejecutor bloqueado nunca aprueba, recuperación tras caída (`interrumpida`, no se re-ejecuta, reencolar crea misión nueva), apagado marca `interrumpida`, chat y borrador (sin ejecutar nada). |
| `tests/test_web.py` (12) | Servidor HTTP real: estáticos y cabeceras de seguridad, traversal en estáticos, 401 sin sesión en todas las rutas (misiones, archivos, descargas, SSE…), login fallido y Origin ajeno, Host no permitido (DNS rebinding), CSRF ausente/incorrecto, Origin ajeno, Content-Type, flujo completo misión → archivos → descarga → aprobación humana, archivo alterado en disco rechazado, entradas inválidas, conversación, SSE, `/api/status` sin secretos, logout, registros y auditoría sin secretos. |

### Resultado de la última ejecución (entorno de desarrollo de Claude)

```
Ubuntu 24.04.5 (contenedor), Python 3.13.16, bubblewrap 0.9.0, x86_64, sin systemd de usuario
Ran 71 tests in 41.250s
OK
```

Además se verificó manualmente en ese entorno:
- `install.sh` como usuario no root (dos veces: idempotente, conserva config, crea `app.prev`).
- `constructor-ia selftest` como usuario no root → **EJECUCIÓN BLOQUEADA** con el motivo
  `Failed to connect to bus` (no hay gestor systemd de usuario): el sistema falla cerrado.
- Servidor instalado: `/api/health`, 401 sin sesión, login, estado con Ollama simulado,
  misión rechazada sin llamar al modelo por ejecución bloqueada, apagado limpio con SIGTERM.
- Interfaz en Chromium (390×844 y 1280×860): login, nueva misión, misión fallida y
  reparada hasta aprobarse, consola en vivo, diferencias, chat, estado; sin errores de
  consola ni violaciones de CSP.

### Qué es simulado

- **Ollama**: `tests/fake_ollama.py` imita `/api/chat` en streaming NDJSON, `/api/tags`,
  `/api/ps`, `/api/version`. No se probó con `qwen3:4b-instruct` real.
- **Límites cgroup**: en las pruebas se usa `NullLimiter` (solo construible desde código de
  prueba, no seleccionable por configuración). La lectura/validación de `memory.max`,
  `pids.max` y `cpu.max` se probó con archivos de cgroup simulados.

## Verificaciones obligatorias en la VivoBook

Claude no tuvo acceso a la VivoBook: estas comprobaciones **no** se realizaron y deben
hacerse antes de habilitar el arranque automático.

| # | Comprobación | Comando / procedimiento | Esperado |
|---|---|---|---|
| V1 | Autodiagnóstico | `~/constructor-ia/bin/constructor-ia selftest` | Todas `[OK]`, incluido `limites_cgroup` con `memory.max=268435456`, `pids.max=32`, `cpu.max=50000 100000`; “AISLAMIENTO OPERATIVO” |
| V2 | Memoria | `constructor-ia selftest --stress mem` | `stress_mem: código=-9` (matado por OOM del scope), sin `sobrevivio`; Vigilanti sigue funcionando |
| V3 | Fork bomb | `constructor-ia selftest --stress fork` | `stdout='limite N …'` con N < 32 |
| V4 | Huérfanos | `constructor-ia selftest --stress orphan`, luego `pgrep -u diego -f 'time.sleep(600)'` | limpieza verificada = True; `pgrep` sin resultados |
| V5 | Bucle sin fin del arnés | `constructor-ia selftest --stress spin` | estado `timeout` a los 20 s; `systemctl --user list-units 'cia-job-*'` vacío |
| V6 | Ollama CPU real | Misión de ejemplo (abajo) | Petición visible en `journalctl` de `ollama-cpu`; código generado guardado |
| V7 | Arranque/parada | `systemctl --user start/stop constructor-ia` | inicia < 5 s, “Detenido limpiamente” en el registro |
| V8 | Navegador cerrado | Crear misión desde el celular, cerrar el navegador, volver a los minutos | La misión siguió y muestra intentos, pruebas y archivos |
| V9 | Reinicio durante una misión | `systemctl --user restart constructor-ia` mientras genera | Misión en `interrumpida`; se puede reencolar; nada se re-ejecuta solo |
| V10 | Cancelación | Cancelar una misión en `generando` y otra en `probando` | Estado `cancelada`; `ollama ps` (11435) deja de generar; sin `cia-job-*` |
| V11 | Servicios existentes intactos | Antes y después: `systemctl is-active` de Node-RED, Vigilanti, MariaDB, Tailscale, Ollama 11434/11435, OpenClaw; `ls -l` de sus configuraciones | Mismo estado y fechas de modificación |
| V12 | Sin escrituras sobre Vigilanti | `find /home/diego/vigilancia -newer ~/constructor-ia/app -user diego` tras usar el sistema | Solo archivos que Vigilanti escribe por sí mismo |
| V13 | Tailscale opción A | Desde el celular: `http://<IP 100.x>:8766` | Login, misión completa |
| V14 | Tailscale opción B (si se elige) | `https://<nombre>.ts.net:8443` | Login y creación de misión (si POST devuelve 403 “Origen no permitido”, Serve está reescribiendo `Host`: informar) |
| V15 | Rollback | `update.sh` con la misma versión, luego `rollback.sh` | Servicio vuelve a arrancar |
| V16 | Carga | Observar `top` durante una misión | Ollama CPU domina; Constructor IA < 5 % CPU y < 100 MB |

### Misión de ejemplo para V6/V8

Para la primera prueba usar algo trivial y verificable a mano:

- Título: `Sumar dos números` · Tipo: función · Función: `sumar`
- Casos: `1, 2` → `3` · `-5, 5` → `0` · `0.1, 0.2` → `0.3` (marcar como oculto)
- Marcar “Revisé los casos” y “Autorizo ejecutar…”

Después, una tarea algo más exigente (p. ej. validar palíndromos ignorando
mayúsculas y signos: `"Anita lava la tina"` → `true`, `"hola"` → `false`).

## Matriz de aceptación del mandato (§14)

| # | Criterio | Automatizado aquí | En la VivoBook |
|---|---|---|---|
| 1 | Servidor arranca sin errores | ✔ test_web, arranque manual | V7 |
| 2 | Autenticación | ✔ test_units, test_web | V13 |
| 3 | Sin sesión no hay misiones ni descargas | ✔ `test_unauthenticated_denied` | — |
| 4 | Misiones en SQLite | ✔ test_engine | — |
| 5 | Encolar misión | ✔ test_engine, test_web | — |
| 6 | Ollama CPU recibe la consulta | ✔ simulado (modelo, `num_ctx`, `think`) | **V6** |
| 7 | Código generado conservado | ✔ archivos por intento + final | V6 |
| 8 | Aislamiento y límites | ✔ bwrap/seccomp reales; cgroup simulado | **V1–V5** |
| 9 | Pruebas independientes con resultados reales | ✔ test_sandbox | V6 |
| 10 | Errores activan reintento | ✔ `test_repair_with_real_errors` | V6 |
| 11 | Completada no se re-ejecuta | ✔ `test_approved_first_try_and_not_rerun` | — |
| 12 | Cancelada deja de consumir | ✔ `test_cancel_*` | **V10** |
| 13 | Sobrevive a la desconexión del navegador | ✔ por diseño (hilos, no HTTP) | **V8** |
| 14 | Estado consistente tras reinicio | ✔ `test_recovery_after_restart` | **V9** |
| 15 | Sin secretos en logs | ✔ `test_zz_logs_have_no_secrets` | — |
| 16 | Sin escritura sobre Vigilanti | ✔ sandbox sin `/home`; servidor solo escribe en `~/constructor-ia` | **V12** |
| 17 | No modifica Node-RED | ✔ por diseño (ningún script lo toca) | **V11** |
| 18 | No modifica OpenClaw | ✔ por diseño (no se usa) | **V11** |
| 19 | Parada limpia | ✔ SIGTERM manual, `test_shutdown_*` | V7 |
| 20 | Rollback | ✔ script; instalación con `app.prev` probada | **V15** |
