# Taller Web (v0.2.0)

Nuevo tipo de misión **«Página web / Tablero»**: Qwen3 genera un proyecto de
varios archivos (HTML, CSS, JavaScript y recursos de texto como JSON o SVG), que
se verifica en el sandbox, se corrige entre intentos reutilizando lo anterior, se
conserva por versiones y se puede ver en una vista previa aislada o descargar en ZIP.

## Cómo se usa

1. *Nueva misión* → Tipo: **Página web / Tablero**.
2. Describir la página. Botón **Cargar ejemplo: tablero de mensajes** para la demostración.
3. Definir los **requisitos verificables** (los decide el operador, el modelo no los cambia):

   | Tipo | Ejemplo | Qué verifica |
   |---|---|---|
   | Debe existir el elemento (selector) | `textarea` · `button#agregar` · `ul, ol` · `input[type=text]` | Algún elemento de algún HTML coincide (etiqueta, `#id`, `.clase`, `[atributo]`, `[atributo=valor]`, alternativas con coma; sin combinadores) |
   | Debe mostrar el texto | `Total de palabras` | Texto visible de `index.html` |
   | El JavaScript debe usar | `localStorage` | Aparece en algún `.js` |
   | El CSS debe incluir | `background` | Aparece en algún `.css` |

   Cada requisito puede ser oculto: el modelo no lo ve y, si falla, solo sabe que falló.
4. Confirmar revisión y autorización → *Crear misión*.

En la misión: sección **Proyecto web** con selector de versión (cada intento o la
versión final aprobada), **Vista previa** y **Descargar ZIP**.

## Verificaciones automáticas (siempre, dentro del sandbox)

| Verificación | Detalle |
|---|---|
| `estructura` | `index.html`, `<!DOCTYPE html>`, `<html>`, `<head>`, `<body>`, `<title>`, `<meta charset>`, `<meta name="viewport">` |
| `html:<archivo>` | Etiquetas mal anidadas o sin cerrar, cierres sobrantes, scripts en línea, atributos `on*`, URLs `javascript:`, `<base>` |
| `css:<archivo>` | Llaves y cadenas balanceadas, comentarios cerrados |
| `js:<archivo>` | `node --check` del sistema (`/usr/bin/node`) **sin ejecutar** el código; módulos ES con `--input-type=module`. Si no hay node en `/usr`, tokenizador heurístico (cadenas, plantillas, regex, comentarios, paréntesis) |
| `referencias` | `src`/`href`/`url()`/`import` locales existen; ids buscados con `getElementById`/`querySelector('#…')` existen; ids duplicados; rutas absolutas (rompen la vista previa) |
| `politica_recursos_locales` | Sin CDN, fuentes, imágenes, `import`, `fetch`, `WebSocket` ni `EventSource` hacia otros orígenes |

**Límite honesto:** no hay navegador en el sandbox, así que el *comportamiento*
(clics, conteo de palabras, persistencia) no se ejecuta automáticamente. Se
verifica la estructura, la sintaxis, las referencias y los requisitos declarados;
el comportamiento se comprueba en la vista previa.

## Generación y corrección

- Formato pedido al modelo: cada archivo precedido por `### archivo: ruta` y un bloque de código.
- Rutas en lista blanca: minúsculas, hasta 2 carpetas, extensiones `html css js mjs json svg txt md`.
  Se rechazan `..`, rutas absolutas, mayúsculas, otros tipos. Límites: 12 archivos, 100 KiB por archivo, 400 KiB total (configurable).
- En la reparación el prompt incluye los archivos actuales (primero los que fallaron) y los
  errores reales; el modelo devuelve **solo los archivos que cambia** (`### eliminar: ruta` para borrar).
  Los demás se reutilizan del intento anterior.
- Cada intento guarda el proyecto completo en `intento-N/web/…`; la versión aprobada en `final/web/…`.
  Más `intento-N/respuesta_modelo.md` y `intento-N/resultado.json`.
- Máximo de intentos: el del formulario, acotado por `engine.max_attempts_limit`.
- Modelo: `[ollama.web]` (por defecto `qwen3:4b-instruct`, `num_ctx = 6144`, `num_predict = 3072`, `timeout_s = 600`).

## Modelo de seguridad de la vista previa

La vista previa es una **superficie no confiable**.

| Control | Implementación |
|---|---|
| Origen separado | Servidor propio en `web.preview_port` (8767), mismas IP de escucha que el panel (loopback / Tailscale; nunca 0.0.0.0). Otro puerto = otro origen: el JS generado no puede leer el DOM ni las respuestas del panel. |
| Sin sesión | El servidor de vista previa no lee ni emite cookies. Acceso por token aleatorio de 256 bits en la ruta (`/p/<token>/…`), con vencimiento (120 min), creado solo por un usuario autenticado con CSRF. En memoria: un reinicio los invalida. |
| Sin acceso a la API | La API del panel rechaza toda petición con `Sec-Fetch-Site: same-site` o `cross-site`. Otro puerto del mismo host es *same-site* y el navegador enviaría la cookie, pero el servidor no la acepta. Esto también protege frente a páginas de Node-RED/Vigilanti en otros puertos. |
| CSP de la vista previa | `default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; form-action 'none'; base-uri 'none'; object-src 'none'; frame-src 'none'; frame-ancestors <orígenes del panel>; sandbox allow-scripts allow-same-origin allow-forms allow-modals` |
| Aislamiento del navegador | `iframe sandbox="allow-scripts allow-same-origin allow-forms allow-modals"` (sin popups, sin navegación del documento superior, sin descargas). La directiva CSP `sandbox` mantiene esas restricciones aunque se abra en otra pestaña. `allow-same-origin` solo da acceso a *su propio* origen (para `localStorage`), nunca al del panel. |
| Panel | Su CSP solo permite enmarcar el origen de la vista previa (`frame-src http://<host>:8767`) y mantiene `frame-ancestors 'none'`. |
| Otras cabeceras | `nosniff`, `Referrer-Policy: no-referrer`, COOP/CORP `same-origin`, `Permissions-Policy` sin cámara/micrófono/ubicación, `Cache-Control: no-store`, `X-Robots-Tag: noindex`. |
| Solo estáticos | Únicamente archivos registrados en la base para ese intento, con SHA-256 verificado. Nada de Python, comandos ni procesos en el servidor. `respuesta_modelo.md` y `resultado.json` no son accesibles. |

Comprobado en Chromium real (prueba de extremo a extremo): desde la vista previa
`document.cookie` está vacío, acceder a `top.location` lanza `SecurityError`,
`fetch` al panel y a Internet quedan bloqueados por CSP, `window.open` está
bloqueado, y `localStorage` funciona y persiste.

**Riesgos y límites:**
- `localStorage` es por origen: **todas las vistas previas lo comparten**. Un proyecto puede leer
  lo que guardó otro proyecto (nunca datos del panel).
- La vista previa usa `http://` en la opción A de Tailscale; con HTTPS (Tailscale Serve) hace falta
  un segundo mapeo y `web.preview_origin` (ver abajo). Sin eso, la interfaz deshabilita la vista
  previa y ofrece el ZIP.
- Nunca mapear panel y vista previa al **mismo origen** (mismo esquema + host + puerto) en un proxy:
  anularía el aislamiento.
- El JS generado corre en el navegador del operador. Con estas barreras, lo peor que puede hacer
  es consumir CPU de esa pestaña o mostrar contenido engañoso.

## Demostración pedida

Tablero oscuro para escribir mensajes, listarlos, contar palabras y guardar en el navegador:

1. *Nueva misión* → **Página web / Tablero** → **Cargar ejemplo: tablero de mensajes**.
   Requisitos precargados: `textarea`, `button`, `ul, ol`, JS usa `localStorage`, JS usa
   `addEventListener`, CSS incluye `background`. Revisarlos, marcar las dos casillas y crear.
2. Esperar: con Qwen3 4B en CPU, cada intento puede tardar varios minutos.
3. *Vista previa*: escribir mensajes, verificar el conteo, recargar con *Vista previa* y comprobar
   que los mensajes siguen. *Descargar ZIP* para obtener el proyecto.

Esta demostración **no se ejecutó con Qwen3 real** (no hay modelo en el entorno de desarrollo):
se validó todo el flujo con un modelo simulado que primero olvida `js/app.js` y luego lo corrige.

## Actualización por SSH (de 0.1.1 a 0.2.0)

```bash
cd ~/src/dieguinPinga
git fetch origin claude/modest-clarke-1dzltx
git checkout claude/modest-clarke-1dzltx
git pull --ff-only
ss -ltn 'sport = :8767'                       # debe estar vacío (puerto de la vista previa)
bash ~/src/dieguinPinga/constructor-ia/scripts/update.sh
~/constructor-ia/bin/constructor-ia selftest   # el aislamiento no cambió: debe seguir OPERATIVO
~/constructor-ia/bin/diagnostico.sh | sed -n '/Taller Web/,/Ollama/p'
```

- `update.sh` hace copia de la base, detiene el servicio si estaba activo, reinstala conservando
  `config/` y `data/`, autodiagnostica y vuelve a iniciar. Rollback: `~/constructor-ia/bin/rollback.sh`.
- No hay migración de base de datos en esta versión.
- `config.toml` existente no se modifica: los valores por defecto del Taller Web se aplican solos.
  Para ajustarlos, copiar las secciones `[ollama.web]` y `[web]` de `app/config/config.example.toml`.
- Sin `/usr/bin/node`, la sintaxis JS se verifica con la heurística. No se instala nada:
  si Node-RED usa un node de `/usr/bin`, se aprovecha; si usa uno de `~/.nvm`, no es visible
  en el sandbox (a propósito).

### Acceso a la vista previa desde el celular

- **Opción A (IP Tailscale en `server.listen`)**: la vista previa escucha en las mismas IP,
  puerto 8767. No hay que hacer nada más; `http://100.x.y.z:8766` enmarca `http://100.x.y.z:8767`.
- **Opción B (Tailscale Serve, HTTPS)**: es una decisión tuya porque modifica la configuración de Serve:
  ```bash
  tailscale serve --bg --https=8444 http://127.0.0.1:8767
  ```
  y en `config.toml`:
  ```toml
  [web]
  preview_origin = "https://<nombre>.ts.net:8444"
  panel_origins = ["https://<nombre>.ts.net:8443"]
  ```
  Reiniciar el servicio. Nunca usar `tailscale funnel`.

## Verificaciones pendientes en la VivoBook

| # | Comprobación | Esperado |
|---|---|---|
| W1 | `ss -ltnp` tras iniciar | 8766 y 8767 solo en 127.0.0.1 (y la IP Tailscale si se configuró) |
| W2 | Demostración con Qwen3 4B real | Misión aprobada o fallida con errores comprensibles; archivos por intento |
| W3 | Vista previa desde el celular | El tablero funciona; los mensajes persisten al recargar |
| W4 | `free -h` durante la generación con `num_ctx = 6144` | Sin swap intenso; Vigilanti normal. Si no, bajar a 4096 |
| W5 | `selftest` y `selftest --stress mem,fork,orphan` | Sin cambios respecto de 0.1.1 |
| W6 | Desde la vista previa (consola del navegador del celular o la PC): `fetch('http://<host>:8766/api/me')` | Bloqueado por CSP |
