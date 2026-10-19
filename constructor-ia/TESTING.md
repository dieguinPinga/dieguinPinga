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
| `tests/test_sandbox.py` (30) | Ejecutor con **Bubblewrap y seccomp reales** frente a entradas adversariales: solución correcta/incorrecta, errores de sintaxis e importación, excepciones esperadas, bucle infinito, bloqueo a nivel de módulo, intento de leer casos ocultos / memoria del arnés / `/home`, red (1.1.1.1, 11434, 11435, 1880), escritura fuera de `/tmp`, falsificación de resultados y muerte del arnés, proceso nieto con `setsid` + `os._exit`, ptrace/userns, inundación de salida, modo programa, pruebas del operador, cancelación, timeout global, HMAC adulterado, limpieza de temporales, bloqueo si los límites no se verifican. |
| `tests/test_engine.py` (10) | Motor y cola con Ollama simulado: aprobación al primer intento sin re-ejecución, reparación con errores reales, fallo tras 3 intentos, cancelación durante la generación (Ollama recibe la desconexión), cancelación en cola y orden FIFO con una sola inferencia simultánea, Ollama caído, ejecutor bloqueado nunca aprueba, recuperación tras caída (`interrumpida`, no se re-ejecuta, reencolar crea misión nueva), apagado marca `interrumpida`, chat y borrador (sin ejecutar nada). |
| `tests/test_web.py` (12) | Servidor HTTP real: estáticos y cabeceras de seguridad, traversal en estáticos, 401 sin sesión en todas las rutas (misiones, archivos, descargas, SSE…), login fallido y Origin ajeno, Host no permitido (DNS rebinding), CSRF ausente/incorrecto, Origin ajeno, Content-Type, flujo completo misión → archivos → descarga → aprobación humana, archivo alterado en disco rechazado, entradas inválidas, conversación, SSE, `/api/status` sin secretos, logout, registros y auditoría sin secretos. |

| `tests/test_taller_web.py` (26) | Taller Web: extracción de varios archivos, rechazo de rutas peligrosas (`..`, `/etc/passwd`, `./../`, tipos no permitidos), límites, fusión con el intento previo, ZIP; validación de misiones y configuración web; CSP de la vista previa; verificación en sandbox real (proyecto correcto con y sin node, `node --check` con módulos, errores heurísticos de JS, recursos externos, scripts y eventos en línea, referencias rotas, rutas absolutas, ids duplicados o inexistentes, requisitos del operador, el JS nunca se ejecuta); motor (reparación que reutiliza archivos, versiones por intento, rutas maliciosas nunca escritas); servidor de vista previa (token, cabeceras, sin cookies, 404 ante traversal y tokens vencidos, 405 en POST, Host); el panel enmarca solo la vista previa y rechaza `Sec-Fetch-Site: same-site`; ZIP autenticado; fallback a ZIP. |

| `tests/test_explorer.py` (27) | Explorador con MariaDB 10.11 real y privado: filtro SQL, privilegios, intermediario (ataques con el filtro desactivado, límites, `KILL`, cancelación, aislamiento, registros), agente sobre bases de nombres aleatorios, continuación con cambios de esquema, detención, API y tableros con instantáneas. Detalle en EXPLORADOR.md. |

| `tests/test_revisions.py` (8) | Revisiones: reutilización de código y archivos, pruebas heredadas y nuevas, versión vigente y fallo que la preserva, historial, inmutabilidad (SHA-256), diferencias, API protegida, reencolado con linaje. Detalle en REVISIONES.md. |

| `tests/test_explorer_regresion.py` (13) | Regresiones de la primera expedición real (0.4.1): base equivocada en `information_schema` reparada sin ejecutarse y conteos afirmados desde 0 filas marcados sin verificar; herramientas sin argumentos o anidados reparadas sin consumir pasos; contraste de afirmaciones con estado, filas y cifras; comentarios de tablas y columnas en el contexto como pistas no confiables; presupuesto de tiempo total que incluye inválidas e informe; proceso ≠ calidad ≠ objetivo, y reintento del informe. Detalle en EXPLORADOR.md §0.4.1. |
| `tests/test_explorer_contexto.py` (15) | Regresiones de M20261018-bdcbec (0.4.2): estimación de tokens contra conteos medidos con el vocabulario de Qwen; reserva de respuesta; reducción por niveles y vista por columnas sin perder evidencia; tabla ancha numérica (29 columnas) que entra en num_ctx=4096 y completa el ciclo; desborde `exceed_context_size_error` aprendido y nunca reenviado; contexto imposible que termina sin reintentos idénticos; etapa A sin afirmaciones anticipadas; etapa B contrastada (cifras, valores literales, evidencia ficticia, vigencia de registros históricos); resumen no respaldado que no vuelve al modelo. Detalle en EXPLORADOR.md §0.4.2. |
| `tests/test_explorer_argumentos.py` (11) | Regresiones de la prueba real de 0.4.2 en caverna_01 (0.4.3): el texto «null» es un valor ausente y no una tabla; los argumentos que la herramienta no usa no invalidan el paso; nombres reales nunca tratados como relleno; esquema de la etapa A con nombres reales (enum) y esquema estricto ante un error repetido; la tabla la elige el modelo, nunca el sistema; errores idénticos que se cortan en vez de repetirse; respuestas crudas guardadas. Detalle en EXPLORADOR.md §0.4.3. |
| `tests/test_explorer_ollama.py` (22) | Regresiones de M20261018-3571c4 (0.4.4): servidor que acepta y no responde → timeout (fase espera/generación), nunca «no disponible»; puerto cerrado → no disponible; misión que NO falla ante un bloqueo del servidor (con 0.4.3 terminaba fallida) y completa el ciclo; dos bloqueos seguidos → `ollama_sin_respuesta`; etapa B acotada (la respuesta máxima entra en la reserva), recuperación de truncas sin repetir y una sola interpretación mínima; reserva ajustada a la velocidad medida; prefijo estable; tiempos de Ollama registrados; reglas de `ollama-diag` (presión de memoria, swap sin lecturas, cgroup, CPU, servidor) y muestreo real de un proceso. Detalle en EXPLORADOR.md §0.4.4. |
| `tests/test_explorer_conocimiento.py` (17) | Conocimiento acumulativo (0.4.5): procedencia (reafirmar con menos respaldo nunca degrada, mejor respaldo sube la certeza sin tocar el texto, confirmaciones con evidencia sumada, el modelo no revive un hecho descartado, la reobservación del sistema conserva la expedición creadora); migración 0006 sin cambios sobre una base 0.4.4; reglas de `knowledge` (capacidades y coherencia, SQL nunca bloqueado, firmas, alcance, clasificación conservadora, claves); y de punta a punta con varias expediciones sobre la misma memoria: desajuste pregunta-herramienta reparado, repetición entre expediciones con justificación y detección de cambios, repetir lo sabido no es conocimiento nuevo, generalización desde una muestra acotada como hipótesis, diferencias en el tiempo que no son contradicciones. Detalle en EXPLORADOR.md §0.4.5. |

### Resultado de la última ejecución (entorno de desarrollo de Claude)

```
Ubuntu 24.04.5 (contenedor), Python 3.13.16, bubblewrap 0.9.0, x86_64, sin systemd de usuario
Ran 218 tests in 156.593s   (v0.4.5; incluye conocimiento acumulativo y procedencia)
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

Taller Web (v0.2.0), además, en Chromium real con modelo simulado: misión web creada desde la
interfaz con el ejemplo precargado, fallida en el intento 1 (falta `js/app.js`), corregida en el 2
reutilizando `index.html` y `css/estilo.css`; en la vista previa se agregaron mensajes, se contaron
7 palabras y los datos persistieron al reabrir. Desde dentro de la vista previa: cookie vacía,
`top.location` → SecurityError, `fetch` al panel y a Internet bloqueados por CSP, `window.open` bloqueado.

### Regresión: OOM en `selftest --stress mem` (v0.1.1)

Síntoma en la VivoBook: `El arnés no produjo un resultado válido (código -15)`
aunque el kernel registró `Memory cgroup out of memory: Killed process … (python3.12)`.
Causa: con `OOMPolicy=stop` (valor por defecto de systemd) el OOM del proceso de
prueba hace que systemd detenga toda la unidad `cia-job-*.scope` con SIGTERM,
incluido el arnés, antes de que firme el resultado; además el anfitrión no leía
`memory.events`, así que no podía distinguirlo de un fallo del supervisor.

Corrección: la unidad usa `OOMPolicy=continue` (el kernel mata solo al proceso
que excede `memory.max`; los límites no cambian); el arnés espera a que el
anfitrión cierre stdin para que este lea `memory.events` con el cgroup vivo; el
resultado sin firma con `oom_kill > 0` se clasifica `oom` (fallo verificado por
memoria, nunca éxito) y sin evidencia sigue siendo `error`; `selftest --stress mem`
solo pasa con SIGKILL **y** `oom_kill ≥ 1`.

Pruebas en `OomRegressionTests` y `MemStressVerdictTests` (cgroup simulado, bwrap
real). El OOM real del kernel y `OOMPolicy=continue` solo se pueden comprobar en
la VivoBook (V2).

### Qué es simulado

- **Ollama**: `tests/fake_ollama.py` imita `/api/chat` en streaming NDJSON, `/api/tags`,
  `/api/ps`, `/api/version`. No se probó con `qwen3:4b-instruct` real.
- **Límites cgroup y `memory.events`**: en las pruebas se usa `NullLimiter` o un cgroup simulado en un directorio temporal (solo construible desde código de
  prueba, no seleccionable por configuración). La lectura/validación de `memory.max`,
  `pids.max` y `cpu.max` se probó con archivos de cgroup simulados.

## Verificaciones obligatorias en la VivoBook

Claude no tuvo acceso a la VivoBook: estas comprobaciones **no** se realizaron y deben
hacerse antes de habilitar el arranque automático.

| # | Comprobación | Comando / procedimiento | Esperado |
|---|---|---|---|
| V1 | Autodiagnóstico | `~/constructor-ia/bin/constructor-ia selftest` | Todas `[OK]`, incluido `limites_cgroup` con `memory.max=268435456`, `pids.max=32`, `cpu.max=50000 100000`; “AISLAMIENTO OPERATIVO” |
| V2 | Memoria | `constructor-ia selftest --stress mem` | `[OK] limite_memoria: código=-9 memory.events oom_kill=1` (o más), `stress_mem: código=-9`, sin `sobrevivio`, “AISLAMIENTO OPERATIVO”; `journalctl -k` muestra `Memory cgroup out of memory: Killed process … (python3.12)`; Vigilanti sigue funcionando |
| V3 | Fork bomb | `constructor-ia selftest --stress fork` | `[OK] limite_tareas: limite N …` con N < 32 |
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
