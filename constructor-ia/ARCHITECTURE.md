# Arquitectura

Un único proceso Python (biblioteca estándar) con hilos, más procesos efímeros
y aislados por cada ejecución de pruebas.

```
 navegador (PC / celular por Tailscale)
      │  HTTP + SSE (cookie de sesión, CSRF)
      ▼
 ┌──────────────────────── constructor-ia serve (systemd --user) ────────────────────────┐
 │ web/server.py   ThreadingHTTPServer acotado: API JSON, SSE, estáticos locales          │
 │ auth.py         usuarios scrypt, sesiones (hash SHA-256 en BD), límite de intentos     │
 │ missions.py     cola persistente en SQLite, transiciones de estado atómicas            │
 │ workers.py      MissionWorker (1 misión a la vez) · ChatWorker                         │
 │ engine.py       objetivo → código → validación → pruebas → reparación                  │
 │ ollama.py       cliente streaming + InferenceGate (1 inferencia a la vez en total)     │
 │ files.py        archivos por misión, acceso solo por id, O_NOFOLLOW + SHA-256          │
 │ events.py       eventos persistentes + notificación a SSE                              │
 │ sandbox/        executor.py · limiter.py · seccomp.py · harness.py · child_runner.py   │
 └──────────────┬───────────────────────────────────────────────┬─────────────────────────┘
                │ http://127.0.0.1:11435 (Ollama CPU)            │ systemd-run --user --scope
                ▼                                                 ▼
        ollama-cpu.service                    cia-job-XXXX.scope (MemoryMax, TasksMax, CPUQuota)
        (no se modifica)                        └─ bwrap (namespaces, sin red, seccomp, ro)
                                                    └─ harness.py (confiable, no volcable)
                                                         └─ child_runner.py + solucion.py (no confiable)
```

## Componentes

**Servidor HTTP** (`web/server.py`). `ThreadingHTTPServer` con semáforo de
conexiones (32), límite de clientes SSE (6), cuerpo máx. 256 KiB, timeout de
socket 30 s. Despacho por tabla de rutas con indicador de autenticación.
Estáticos solo desde `static/` con lista de nombres válidos.

**Cola y estados** (`missions.py`). La misión pendiente más antigua se toma con
`BEGIN IMMEDIATE` + `UPDATE … WHERE state='pendiente'`, lo que impide ejecutar
dos veces la misma misión. Al arrancar, las misiones en estado activo pasan a
`interrumpida` y **no** se re-ejecutan automáticamente; *reencolar* crea una
misión nueva con la misma especificación, sin alterar el historial original.
Las misiones `aprobada` no pueden reencolarse.

**Trabajadores** (`workers.py`). Hilos en segundo plano independientes de las
peticiones HTTP: cerrar el navegador o perder Tailscale no afecta el trabajo.
`InferenceGate` garantiza una sola inferencia en todo el proceso (chat y
misiones comparten la instancia CPU con `OLLAMA_NUM_PARALLEL=1`).

**Motor** (`engine.py`, `prompts.py`, `codeextract.py`). Presupuesto de prompt
derivado de `num_ctx` (4096) y `num_predict`. En la reparación se envían la
versión anterior y los errores **reales** del arnés; de los casos ocultos solo
se informa que fallaron (sin valor esperado). Se registran duración y tokens
(`prompt_eval_count`, `eval_count`). Respuestas cortadas (`done_reason=length`
o flujo interrumpido) se marcan y se pide una solución más corta.

**Ejecutor** (`sandbox/`). Ver SECURITY.md. Flujo por ejecución:
1. Directorio `data/runs/run-<aleatorio>/` con `code/solucion.py` y copia del arnés (solo lectura).
2. `systemd-run --user --scope --unit=cia-job-<hex> -p MemoryMax… -- bwrap … python3 -I -S harness.py`.
3. El anfitrión **verifica** en `/sys/fs/cgroup/…/cia-job-<hex>.scope` que `memory.max`, `pids.max` y `cpu.max` son los configurados. Si no, mata y bloquea.
4. Recién entonces envía por stdin la especificación de pruebas (los casos no existen en disco dentro del sandbox) y un nonce.
5. Espera con tiempo máximo y cancelación; al terminar mata la unidad completa y verifica que `cgroup.procs` quede vacío.
6. Acepta solo la línea `CIA-RESULT <json> <hmac>` con HMAC válido.

**Persistencia** (SQLite WAL, `migrations/NNNN_*.sql`, `PRAGMA user_version`):
`users`, `sessions`, `missions`, `attempts`, `files`, `events`, `conversations`,
`messages`, `approvals`, `audit`, `kv`. Archivos de misión en
`data/missions/<id>/{intento-N,final,pruebas,paquete}/`.

**Tiempo real**: SSE (`/api/stream`) con `Last-Event-ID`; el servidor espera
con `threading.Condition` (sin sondeo activo) y envía un ping cada 15 s. El
cliente solo recarga datos cuando llega un evento relevante (con *debounce*).

**Retención**: limpieza horaria de eventos (60 días), máx. 2000 eventos por
misión, registro rotativo (2 MB × 3), auditoría 1 año, sesiones vencidas.

## Taller Web (v0.2.0)

```
webproject.py   extracción de "### archivo: ruta" (lista blanca), fusión con el intento previo, ZIP, prompts
engine.py       _run_web / _attempt_web: generar → fusionar → guardar intento-N/web → verificar en sandbox
sandbox/harness.py  run_web_checks: HTML (html.parser), CSS, JS (node --check o heurística), referencias, requisitos
web/preview.py  servidor de vista previa (puerto 8767): tokens en memoria, solo estáticos, CSP sandbox
web/server.py   POST /api/missions/<id>/preview · GET /api/missions/<id>/zip · bloqueo Sec-Fetch-Site
```

El ejecutor recibe un diccionario de archivos en lugar de `solucion.py`; el comando del sandbox y
sus límites son los mismos. Las misiones web usan `[ollama.web]` (contexto mayor). Detalle en TALLER_WEB.md.

## Ollama directo vs. OpenClaw

| Función | Implementación |
|---|---|
| Generación y reparación de código | **Ollama directo** (`/api/chat`, 11435, `qwen3:4b-instruct`) |
| Conversación, borradores, explicaciones | **Ollama directo** (11435, `qwen3:1.7b` configurable) |
| Estado del modelo | **Ollama directo** (`/api/tags`, `/api/ps`, `/api/version`) |
| OpenClaw | **No se usa en la v0.1.** No se lee ni se modifica su configuración ni sus agentes. |

Motivo: la API de Ollama permite streaming, cancelación real (cierre de
conexión), límites de tokens/contexto/timeout y conteo de tokens, y no depende
de permisos de herramientas. La integración futura con el agente `constructor`
de OpenClaw (por su gateway `127.0.0.1:18789`) debería ser de solo consulta,
sin cambiar sus restricciones (`read`, `ls`).

## Consumo de recursos

Proceso servidor en reposo: ~60 MB RSS medidos en desarrollo (incluye el Ollama simulado); sin sondeo activo, los hilos solo esperan.
Límites del servicio: `MemoryHigh=300M`, `MemoryMax=600M`, `CPUWeight=50`,
`Nice=5`. Las pruebas corren en su propio scope (256 MiB, 32 tareas, 50 % CPU).
El modelo lo carga Ollama CPU (máx. 1 modelo, keep-alive 1 min).
