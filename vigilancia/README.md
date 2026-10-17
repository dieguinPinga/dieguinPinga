# Vigilancia: estación audiovisual doméstica (vigd)

Convierte la ASUS VivoBook (Linux Mint 22) en una estación de vigilancia con una **biblioteca de
eventos estilo EZVIZ**: el evento más reciente arriba, miniatura, categoría, hora, duración, audio y
video reproducibles, transcripción, filtros, búsqueda, estadísticas y estado de sensores. Está pensada
para usarse desde el celular, vía Tailscale.

- **Sin dependencias nuevas**: Python 3.12 (biblioteca estándar) + `numpy` y `webrtcvad` (ya están en
  `~/vad-venv`), `ffmpeg` y `parec`. `faster-whisper` es opcional y se usa sólo para transcribir.
- **La captura no depende de Whisper ni de Ollama.** La transcripción corre en otro proceso, con
  prioridad baja, y sólo si hay memoria libre.
- **No modifica** Node-RED, Ollama, Tailscale, los dispositivos de audio predeterminados ni otros
  entornos. Todo lo que escribe queda en `~/vigilancia-data` (configurable).

## Arquitectura

```
                ┌───────────────────────── proceso "capture" ─────────────────────────┐
 micrófono ──► parec ──► buffer circular 20 s ──► VAD + energía adaptativa ──┐         │
                                                                             ├─► EventManager ──► Finalizer ──► SQLite
 /dev/video0 ─► ffmpeg ─┬─► segmentos H.264 de 4 s (buffer rodante 60 s) ────┤   (correlación,     (m4a, mp4 por
                        └─► gris 160x120 @5 fps ──► detector de movimiento ──┘    pre/post-roll)   copia, miniatura)
                └──────────────────────────────────────────────────────────────────────┘
 proceso "web"    : dashboard + API JSON + SSE en vivo + archivos con Range (sólo lee la base)
 proceso "worker" : Whisper (nice 15), toma eventos con voz pendientes de transcribir
 proceso "all"    : supervisor que lanza los tres y los reinicia si caen
```

- **Un único ffmpeg** abre la cámara: graba segmentos H.264 de 4 s en disco y, a la vez, entrega cuadros
  grises de baja resolución para detectar movimiento. Los clips de cada evento se arman **copiando
  segmentos sin recodificar**, por eso cuestan casi nada de CPU, y se les agrega el audio del evento.
- **Audio**: tramas de 30 ms. El piso de ruido es el percentil 20 de los últimos 10 s, así que un
  ventilador constante pasa a ser "ambiente". Una ventana de 0,5 s decide entre *voz* (WebRTC VAD +
  nivel) y *sonido* (energía), y una subida brusca se marca como *impulsivo*.
- **Movimiento**: fondo adaptativo sobre una imagen de 80×60 suavizada; se exigen 3 cuadros
  seguidos con al menos 0,8 % de la imagen cambiada. Los **cambios de luz** se detectan
  normalizando el brillo: si al compensar la ganancia casi no queda diferencia, es luz y no
  movimiento. También hay **zonas ignoradas** configurables (ventana, TV).
- **Eventos**: sonido y movimiento simultáneos van al **mismo evento**. Los disparos separados por
  menos de `merge_gap_s` también se fusionan. Se guardan 5 s antes y 6 s después. Si un evento pasa
  de 5 minutos, se divide y las partes quedan enlazadas (`continuation_of`).

### Clasificación: qué se informa y qué no

| Etiqueta | Cómo se obtiene | Confianza mostrada |
|---|---|---|
| Voz | WebRTC VAD | proporción de ventanas con voz (**no** es una probabilidad) |
| Sonido | energía sobre el ambiente | nivel pico y medio en dBFS |
| Impulsivo | subida brusca ≥ 20 dB | ninguna: "posible golpe/puerta/caída, no confirmado" |
| Movimiento | diferencia con el fondo | fracción máxima de la imagen que cambió |
| Cambio de luz | brillo global | se registra y **no** cuenta como movimiento |

**No** se muestran "ladrido", "alarma" ni "vidrio roto": hoy no hay un modelo instalado que lo
estime. El siguiente paso propuesto es YAMNet (TFLite, unos 4 MB, 521 clases de AudioSet) dentro del
worker, sin tocar la captura. Tampoco hay reconocimiento facial ni biométrico.

## Instalación (en la notebook)

```bash
# 1) Traer el código (sólo esta carpeta del repo)
git clone -b claude/great-goldberg-whhvyg https://github.com/dieguinPinga/dieguinPinga.git ~/vigilancia-src
cp -r ~/vigilancia-src/vigilancia ~/vigilancia
cd ~/vigilancia

# 2) Relevamiento de SÓLO LECTURA (opcional, recomendado)
bash tools/relevamiento.sh 2>&1 | tee ~/relevamiento-vigilancia.txt

# 3) Configuración
mkdir -p ~/vigilancia-data/config && chmod 700 ~/vigilancia-data
cp config.example.toml ~/vigilancia-data/config/config.toml
chmod 600 ~/vigilancia-data/config/config.toml
~/vad-venv/bin/python -m vigd hash-password      # pegar la línea en [web] password_hash
tailscale ip -4                                  # agregar esa IP en [web] bind
nano ~/vigilancia-data/config/config.toml

# 4) Diagnóstico (no graba nada salvo que pidas --mic / --cam)
~/vad-venv/bin/python -m vigd -c ~/vigilancia-data/config/config.toml diag
~/vad-venv/bin/python -m vigd -c ~/vigilancia-data/config/config.toml diag --mic 5 --cam
```

> Usar `~/vad-venv/bin/python` sólo **lee** ese entorno; no se instala nada en él. Si preferís un
> entorno propio:
> `python3 -m venv ~/vigilancia/.venv && ~/vigilancia/.venv/bin/pip install numpy webrtcvad-wheels`,
> y dejá `transcription.python` apuntando a `~/vad-venv/bin/python` (que ya tiene faster-whisper).

## Ejecución manual

```bash
cd ~/vigilancia
~/vad-venv/bin/python -m vigd -c ~/vigilancia-data/config/config.toml all
```

Abrí `http://IP-TAILSCALE:8765/` desde el celular (con Tailscale activo) o `http://127.0.0.1:8765/`
en la notebook. `Ctrl+C` lo detiene: el evento en curso se cierra y se guarda.

También se pueden lanzar por separado: `capture`, `web` y `worker`.

### Dashboard

- **Eventos**: lista descendente que se actualiza sola (SSE) y muestra el evento en curso con la
  marca "EN CURSO". Tiene filtros por tipo (voz, sonido, movimiento, impulsivo, favoritos), por
  fechas y por texto de la transcripción, paginación con "Cargar más" o scroll infinito y una
  "Vista reciente" con el último cuadro grabado (de hace 8 s o menos).
- **Detalle**: video con su audio, audio solo, forma de onda si no hay video, etiquetas con su
  fuente, intensidad, transcripción, navegación entre eventos ‹ ›, favorito, re-transcribir,
  descargas y eliminar.
- **Estadísticas**: eventos por día, actividad por hora en las últimas 24 h y espacio usado.
- **Estado**: procesos, micrófono (nivel en vivo contra el ruido ambiente), cámara (fps, brillo,
  aviso de oscuridad) y cola de transcripción.
- Los indicadores **MIC** y **CAM** del encabezado laten en rojo mientras se está capturando.

## Seguridad

- Login con usuario y contraseña. El hash es PBKDF2-SHA256 de 300k iteraciones y vive en la
  configuración (chmod 600), nunca en el código. La cookie de sesión está firmada con HMAC y es
  `HttpOnly` y `SameSite=Lax`. Después de 8 intentos fallidos, la IP queda bloqueada 15 minutos.
- El servidor escucha **sólo en las direcciones de `bind`**: localhost y la IP de Tailscale. No se
  abre ningún puerto a Internet ni se modifica Tailscale.
  Si querés HTTPS, `tailscale serve` es una opción, pero cambia la configuración de Tailscale, así
  que se haría sólo con tu aprobación explícita.
- Los archivos se sirven **por identificador de la base**, nunca por ruta, y cada ruta se valida
  dentro de `data_dir`. Las cabeceras incluyen CSP estricta, `X-Frame-Options: DENY` y `nosniff`.
  Las escrituras exigen la cabecera `X-Vig` y el mismo origen (protección CSRF).
- `umask 077`: todo lo que se crea queda accesible sólo para `diego`.
- `api_token` (opcional) habilita **sólo lectura** (`GET /api/*`) para integraciones.
- **Privacidad**: no apuntes la cámara a baños ni dormitorios y avisá a quienes puedan ser grabados.
  El pie del dashboard lo recuerda.

## Almacenamiento y retención

```
~/vigilancia-data/
  config/   config.toml, session.secret
  db/       vigilancia.db (SQLite WAL)
  audio/AAAA-MM-DD/<uid>.m4a      AAC 48 kbps mono
  video/AAAA-MM-DD/<uid>.mp4      H.264 640x480 @15 fps + audio
  thumbs/AAAA-MM-DD/<uid>.jpg|png miniatura o forma de onda
  segments/                       buffer rodante (≈60 s, se recrea al iniciar)
  logs/     capture.log, web.log, worker.log, supervisor.log (rotan a 5 MB × 5)
  tmp/
```

Cada 10 minutos se borran los eventos con más de `retention_days` días (7 por defecto, salvo los
favoritos). Si el disco libre baja de `min_free_gb` o los clips superan `max_storage_gb`, se borran
los eventos más antiguos. Nunca se borra nada fuera de `data_dir`.

### Esquema de la base

| Tabla | Contenido |
|---|---|
| `events` | id, uid, started_at, ended_at, status (recording/processing/ready/error), category, labels (JSON), has_voice/has_sound/has_motion/impulsive/light_change, peak_db, mean_db, noise_floor_db, voice_ratio, motion_max, motion_ratio, transcript, transcript_status, starred, continuation_of, error, updated_at |
| `media` | id, event_id → events (CASCADE), kind (audio/video/thumb), relpath (único, relativo a data_dir), mime, bytes, duration |
| `status` | latido de cada proceso (captura, audio, video, worker) con su detalle JSON |
| `meta` | schema_version |

Definición completa en `vigd/db.py`.

## Consumo estimado (i7-8550U)

| Componente | CPU | RAM |
|---|---|---|
| ffmpeg cámara (x264 veryfast 640×480@15) | 8–15 % de un núcleo | ~60 MB |
| parec + VAD + energía | ~1 % | ~10 MB |
| Detector de movimiento (80×60 @5 fps) | ~1 % | — |
| Proceso capture (Python + numpy) | — | ~60 MB |
| Web en reposo | ~0 % | ~30 MB |
| Whisper small int8 (sólo al transcribir) | 2 hilos, nice 15 | 500–900 MB; se libera tras 10 min |

Disco: alrededor de 0,3–0,6 MB por minuto de clip. Con 2 h de eventos por día, son unos 0,5 GB
diarios, o sea 3–4 GB con 7 días de retención.

## Funcionamiento continuo (systemd), requiere tu aprobación

`systemd/vigilancia.service` es un **servicio de usuario**: no necesita root y no toca servicios del
sistema. No se instala solo. Para activarlo:

```bash
mkdir -p ~/.config/systemd/user
cp ~/vigilancia/systemd/vigilancia.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now vigilancia.service
journalctl --user -u vigilancia -f
```

Por defecto un servicio de usuario corre mientras `diego` tenga la sesión iniciada. Para que arranque
sin login, hace falta `sudo loginctl enable-linger diego`. Es un cambio del sistema, así que se decide
aparte. Tené en cuenta además que, **con la tapa cerrada**, la notebook puede suspenderse según
`HandleLidSwitch` en `/etc/systemd/logind.conf` (el relevamiento lo muestra); no lo cambié.

## Integración con Node-RED (opcional)

`nodered/vigilancia-flow.json` crea **una pestaña nueva** ("Vigilancia (vigd)") y no toca los flujos
existentes. Se importa desde Menú → Importar → pegar el JSON → "nuevo flujo". Trae:

- `POST /vigilancia/evento`: recibe cada evento terminado. Para activarlo, en `config.toml` poné
  `webhook_url = "http://127.0.0.1:1880/vigilancia/evento"`.
- Una consulta a `/api/status` cada 60 s que avisa si un componente falla. Para eso, completá
  `VIGD_TOKEN` en las variables de la pestaña con el mismo valor que `web.api_token`.

## Pruebas

```bash
cd ~/vigilancia
~/vad-venv/bin/python -m unittest discover -s tests -t .              # unitarias (<1 s)
VIGD_E2E=1 ~/vad-venv/bin/python -m unittest tests.test_e2e           # punta a punta, ~45 s
```

La prueba de punta a punta usa cámara y micrófono **sintéticos** de ffmpeg, así que no toca los
dispositivos reales. Verifica que se generen eventos con audio, video con dos pistas y miniatura.

## Diagnóstico y recuperación

- `python -m vigd -c CONFIG diag` revisa dependencias, cámara (y quién la usa), disco, integridad
  de la base, archivos faltantes y el latido de cada proceso.
- Si `parec` o `ffmpeg` mueren, por ejemplo al desenchufar la cámara, se reintenta con espera
  creciente. Mientras tanto el otro sensor sigue funcionando.
- Si la captura se corta en medio de un evento, al reiniciar ese evento queda marcado como `error`
  ("Interrumpido por reinicio"), sin borrar nada.
- Si un proceso cae, el supervisor (`all`) lo relanza. Además, systemd relanza el supervisor.

## Desinstalación (sin afectar otras aplicaciones)

```bash
systemctl --user disable --now vigilancia.service 2>/dev/null
rm -f ~/.config/systemd/user/vigilancia.service && systemctl --user daemon-reload
rm -rf ~/vigilancia ~/vigilancia-src          # código
rm -rf ~/vigilancia-data                      # ¡borra también todas las grabaciones!
# En Node-RED, si lo importaste: borrar la pestaña "Vigilancia (vigd)".
```

No quedan paquetes, servicios del sistema ni cambios en otros entornos.

## Limitaciones conocidas

- WebRTC VAD puede marcar como "voz" tonos puros o música. Por eso la etiqueta muestra la
  proporción y la fuente, no una certeza.
- La webcam VGA no tiene infrarrojo. Con oscuridad total no hay detección de movimiento útil, y la
  pestaña Estado lo indica ("oscuro").
- La alineación entre audio y video es por reloj de pared, con un error de ±0,5 s
  aproximadamente. Los cortes del clip caen en cuadros clave (cada 1 s).
- La cámara admite un solo proceso a la vez. Mientras la captura corre, otro programa no puede
  usarla, y `diag --cam` lo informa.

## Próximos pasos (Fase 5)

1. YAMNet en el worker para ladridos, alarmas y vidrio, mostrando sólo las clases con puntaje
   mayor al umbral y la fuente.
2. Preguntas en lenguaje natural ("¿qué pasó entre las 14 y las 16?") con Ollama `qwen3:1.7b`. La
   consulta SQL arma un contexto con eventos reales y el modelo sólo resume ese contexto, citando
   ids e indicando cuando no hay datos.
3. MQTT opcional para domótica.
