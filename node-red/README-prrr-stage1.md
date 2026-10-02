# PRRR Market Data — Etapas 1 → 6b

**Versión actual: `prrr-market-data-stage6b.json`** (generador `tools/build-prrr-stage6b.js`). Sobre una etapa 6 en marcha, usar el **addon `prrr-market-data-stage6b-addon.json`**.

## Etapa 6b — serie GMX en el gráfico ZEC de MERCADO (sólo visual)
```
tick 500 ms → GMX → MERCADO (lee md_gmx del poller ZEC_GMX existente) → [ui_template invisible, grupo MERCADO]
                                                                          → navegador: serie "GMX" en el gráfico ZEC
```
* **100 % aditivo: 0 nodos modificados, 4 agregados.**

  | id | tipo | qué hace |
  |---|---|---|
  | `prrr_grp_gmx_mkt` | group (editor) | Marco visual "ETAPA 6b" en el editor. |
  | `prrr_gmx_mkt_tick` | inject 500 ms | Despierta al nodo siguiente. |
  | `prrr_gmx_mkt` | function | Toma de `global md_gmx` (escrito por el poller de la etapa 6, sólo lectura) las publicaciones nuevas de ZEC_GMX. Guarda 4 h `[recepción, mid]` y las envía al navegador. Atiende el pedido de historial `since`. |
  | `prrr_gmx_mkt_ui` | ui_template 1×1 invisible en el grupo MERCADO | En el navegador agrega la serie **"GMX"** al almacén de series del gráfico ZEC (`window.__prrrPlot`), con la misma estructura que cada exchange. |

* **Qué no se toca:**
  * El template MERCADO no se modifica. Dibuja la serie GMX con sus reglas actuales: ventana visible, autoescala (excluye series stale), escalón entre datos reales, corte de línea tras 60 s sin datos, "(stale)" en la leyenda tras 60 s y el último punto sin extender hacia adelante.
  * Tampoco cambian el PRRR, las SMA25/50, las señales, `market_1s`, `gmx_price` ni el poller ZEC_GMX.
* **Identificación:** la leyenda dice **GMX**, en rojo `#d62728` (color ya asignado a la clave "GMX" en la paleta del dashboard). La serie sólo aparece en ZEC; BTC, GMX y XMR quedan iguales.
* **Datos:**
  * Valor = `mid` = (min+max)/2 de cada publicación real de ZEC_GMX, sin interpolar ni rellenar.
  * Eje X = hora de **recepción local**, el mismo reloj que las series PRRR.
  * La edad estricta de 10 s del dato GMX sigue en DIAGNÓSTICO → ZEC_GMX.
* **Navegador:** al abrir la página recibe el historial de hasta 4 h. Al volver a la pestaña pide sólo lo que falta (`since`).

### Instalación sin cortar la adquisición
1. En el editor, abrir la pestaña **PRRR Market Data**.
2. Menú → **Import** → pegar o abrir `prrr-market-data-stage6b-addon.json` → **Import**.
   * Debe decir **"Imported: 3 nodes, 1 group"**, sin diálogo de conflicto.
   * **Si aparece "Some of the nodes you are importing already exist…", elegir Cancel** (significa que ya estaba importado). No usar "Import copy".
3. Hacer clic en el lienzo para soltar los nodos y luego **Deploy → Modified Nodes**.
4. Recargar MERCADO no es necesario: la serie aparece sola en unos segundos.

**Por qué no se modifica el template MERCADO:** el diálogo *Import* de Node-RED no ofrece "replace" para nodos de un flow (sólo para tabs, subflows y config nodes). Un addon con el template modificado sólo podría importarse como **copia**, lo que duplicaría MERCADO, o reemplazando el tab entero.

**Validado con el diálogo Import real (Node-RED 5.0.7):**
* Con un navegador ya abierto en MERCADO, la serie GMX apareció ~4 s después del deploy, sólo en ZEC.
* Los WebSockets siguieron contando, con 0 reconexiones.
* market_1s: 294/294 s por activo, contiguos a través del deploy.
* gmx_price: filas = claves únicas.
* Página nueva: carga el historial.
* GMX caído: "GMX (stale)" y ningún punto nuevo inventado. Al volver, retoma solo.


## Etapa 6 — ZEC_GMX: precio de ejecución de GMX para ZEC/USD (Arbitrum)
```
tick 250 ms → ZEC_GMX poller ─→ [http request] GET oracle keeper /prices/tickers ─→ ZEC_GMX poller
                  │  (sólo publicaciones nuevas)                                          │
                  ├─→ GMX DB writer (cola · lotes) ─→ [mysql] gmx_price                    │
                  └─→ (1 Hz) indicador ZEC_GMX en DIAGNÓSTICO  ← último trade ZEC del PRRR (md_ring, sólo lectura)
```
SEÑAL (PRRR multi-exchange → SMA25/50) y EJECUCIÓN (oráculo GMX → ZEC_GMX) quedan separadas. **No se modificó ningún nodo existente:** hay 13 nodos nuevos y ninguno de los 173 anteriores cambia.

### Fuente investigada (código oficial `gmx-io/gmx-synthetics` y `gmx-io/gmx-interface`)
* **Ejecución on-chain:**
  * Las órdenes de GMX v2 (apertura, cierre, TP/SL, liquidaciones) se ejecutan con precios que el *keeper* entrega firmados en la transacción.
  * `Oracle.sol` (`setPrices` → `_validatePrices`) verifica cada precio con el proveedor configurado para el token (`oracleProviderForToken`). Además exige que sea reciente (`MAX_ORACLE_PRICE_AGE`) y lo compara contra el feed de referencia de Chainlink (`MAX_ORACLE_REF_PRICE_DEVIATION_FACTOR`).
  * Cada precio validado tiene **min** y **max**. En `ChainlinkDataStreamProvider.sol`, `min = bid` y `max = ask` del reporte.
  * La ejecución usa max o min según el lado: abrir long / cerrar short con max, y cerrar long / abrir short con min. Los triggers (TP/SL) siguen esa misma regla (`isLong ? maxPrice : minPrice`).
* **Proveedor de ZEC:**
  * ZEC es un token **sintético** (`0x6eAbbaA3278556Dc5b19c034dc26c0eaB60d65B5`, 8 decimales), mercado `ZEC/USD [WBTC-USDC]` `0x587759c237acCa739bCE3911647BacF56C876E60`, listado el 2025-12-22.
  * El proveedor por defecto de los scripts de configuración es `chainlinkDataStream`, y todos los sintéticos equivalentes (XMR, PI, …) usan `dataStreamFeedId`. Por eso, con muy alta probabilidad, ZEC usa **Chainlink Data Streams**.
  * No figura todavía en el `config/tokens.ts` público. La confirmación definitiva es on-chain: `DataStore.getAddress(oracleProviderForTokenKey(oracle, ZEC))`.
* **Lectura programática:**
  * El **oracle keeper oficial** `https://arbitrum-api.gmxinfra.io/prices/tickers`, con los fallbacks oficiales `arbitrum-api-fallback.gmxinfra.io` y `arbitrum-api-fallback.gmxinfra2.io`.
  * Es la misma API que consulta la interfaz de GMX cada 1000 ms (`useTokenRecentPricesData`), y publica el min/max de los reportes del oráculo.
  * **No existe WebSocket**, por eso se usa REST.
  * No es scraping del frontend.
* **Formato real** (fixture oficial grabado de la API):
  * Ejemplo: `{"tokenAddress":"0x6eAb…65B5","tokenSymbol":"ZEC","minPrice":"4438634890306012950000000","maxPrice":"4438952227650124450000000","updatedAt":1783345314888,"timestamp":1783345314}`.
  * Conversión: USD = raw / 10^(30−8) = **443,8635 / 443,8952**. Se hace exacta con BigInt, sin pasar por float.
* **Límites honestos:**
  * La API publica el mismo min/max que usa el keeper, pero el precio exacto de una ejecución es el del reporte incluido en *esa* transacción. Puede diferir en milisegundos y se puede auditar on-chain con el evento `OraclePriceUpdate`.
  * La API no informa id de proveedor ni latencia propia. Por eso sólo se guardan `updatedAt`, min y max, sin inventar nada más.

### Implementación
* **Consulta:** cada `pollMs` = 1000 ms (igual que la interfaz oficial), con una sola consulta en vuelo y timeout de 4 s.
* **Errores:** tras 3 errores seguidos pasa al siguiente host oficial, con backoff de 1 → 2 → 4 s por host y tope de 30 s. Estando en un fallback, cada 10 min vuelve a probar el primario.
* **Registro:** sólo cuando la fuente publica algo nuevo (`updatedAt`/min/max distintos). Las repetidas se cuentan y no se guardan, y un `updatedAt` más viejo (host atrasado) se descarta. No hay ticks artificiales, interpolación ni relleno.
* **Frecuencia:** lo que se observa es min(frecuencia de la fuente, 1/pollMs). El indicador muestra el intervalo mediano entre publicaciones y cuántas consultas vinieron repetidas.
  * Si "repetidas" queda en ~0 y el intervalo ≈ pollMs, la fuente cambia más rápido que la consulta. En ese caso se puede bajar `pollMs` (en el *On Start* del poller) a 500 ms; no conviene bajar más contra la API pública.
* **Memoria:** `global.get('md_gmx','memory')` → `last.ZEC` y `ring.ZEC` (últimos 3600 registros), para el futuro simulador.

### Tabla `gmx_price` (nueva, independiente; `market_1s` no se toca)
Crear una vez como administrador con **`sql/stage6-gmx_price.sql`**:
* PK `(symbol, source_ts)`: una fila por publicación de la fuente, sin duplicados. Índice `(symbol, ts)`.
* Columnas:
  * `ts`: recepción local, DATETIME(3) UTC.
  * `source_ts`: `updatedAt`.
  * `price`: (min+max)/2, derivado.
  * `min_price` / `max_price`: DECIMAL(30,18).
  * `age_ms` = ts − source_ts.
  * `rtt_ms`: ida y vuelta HTTP.
  * `source`: host + ruta.
* El usuario `prrr` ya tiene `SELECT, INSERT, UPDATE` sobre `prrr_market.*` (etapa 3), así que no hace falta un GRANT nuevo.
* **Writer:** lotes cada ≤ 5 s, cola en RAM acotada (7200), reintento con backoff y `ON DUPLICATE KEY UPDATE` que conserva la hora de recepción original.
* **Si la tabla no existe**, el indicador lo dice y el resto sigue funcionando.

### DIAGNÓSTICO → grupo "ZEC_GMX · precio de ejecución GMX (oráculo)"
* **Estado:** ok / error / sin actualizar / conectando; host en uso.
* **Precios:** min (bid), max (ask), mid, spread.
* **Tiempos:** updatedAt de la fuente, hora de recepción, edad del dato (en rojo si supera 10 s) y edad al recibir.
* **Frecuencia:** actualizaciones (total y últimos 60 s) e intervalo mediano.
* **Comparación:** último trade ZEC del PRRR (fuente y antigüedad) y **GMX mid − PRRR** en USD y en %. Si el dato GMX está viejo, se marca.
* **Consultas:** ok/err, RTT, consultas demoradas, cambios de host y último error.
* **DB `gmx_price`:** estado, filas, cola, errores y último guardado.

### Consultas útiles
```sql
SELECT COUNT(*), MIN(source_ts), MAX(source_ts), AVG(age_ms), AVG(rtt_ms) FROM gmx_price WHERE symbol='ZEC';
SELECT * FROM gmx_price WHERE symbol='ZEC' ORDER BY source_ts DESC LIMIT 20;
-- intervalo real entre publicaciones
SELECT AVG(d), MIN(d), MAX(d) FROM (SELECT TIMESTAMPDIFF(MICROSECOND, LAG(source_ts) OVER (ORDER BY source_ts), source_ts)/1000 d FROM gmx_price WHERE symbol='ZEC') x;
-- GMX vs PRRR por segundo (close del bucket 1 s)
SELECT m.ts, m.close_price prrr, AVG(g.price) gmx, AVG(g.price) - m.close_price diff_usd, (AVG(g.price)/m.close_price - 1)*100 diff_pct
FROM market_1s m JOIN gmx_price g ON g.symbol = m.symbol AND g.ts >= m.ts AND g.ts < m.ts + INTERVAL 1 SECOND
WHERE m.symbol='ZEC' AND m.ts > UTC_TIMESTAMP() - INTERVAL 10 MINUTE GROUP BY m.ts ORDER BY m.ts DESC LIMIT 20;
```

### Despliegue
**Actualizar la instancia en marcha, sin cortar la adquisición (recomendado):**
1. Crear la tabla con `sql/stage6-gmx_price.sql`, como root.
2. En el editor, abrir la pestaña **PRRR Market Data** e importar **`prrr-market-data-stage6-addon.json`** con *Import* normal, sin Replace. Son sólo los 13 nodos nuevos, que reutilizan el config node MariaDB existente.
3. Hacer **Deploy → Modified Nodes**. Sólo arrancan los nodos nuevos.

**Instalación nueva o reemplazo completo:** usar `prrr-market-data-stage6.json` (Replace + **Deploy Full**). En este caso se reinicia todo el flow y market_1s queda unos segundos sin datos, igual que en etapas anteriores.

Node-RED necesita salida HTTPS hacia `*.gmxinfra.io` / `*.gmxinfra2.io`.

### Validación (sandbox: Node-RED 5.0.7 + MariaDB 10.11)
Los hosts de GMX están bloqueados por el proxy de este entorno. Por eso la API se simuló con un **mock** que devuelve la respuesta real grabada en los fixtures oficiales de `gmx-interface` (120 tokens, mismos campos), con ZEC en random walk y publicaciones cada 0,6–1,6 s. Precio y diferencia contra el PRRR son del mock: **la diferencia real GMX−PRRR hay que leerla en tu instalación.**

**Addon sobre un stage5 en marcha, con el editor real:**
* Quedaron "modificados" sólo los 11 nodos nuevos.
* Deploy *Modified Nodes*: los WebSockets siguieron contando (binance 560 → 1386), con 0 reconexiones.
* gmx_price empezó a llenarse.

**10 min continuos:**
* Lag del event loop: mediana 0 ms, p99 1 ms, máx 13 ms.
* PRRR: lag máx/s mediana 1 ms, 185 ev/s, 0 reconexiones.
* market_1s: 601/601 segundos por activo, 0 huecos desde antes del deploy.
* gmx_price: 501 publicaciones, intervalo prom. 1,2 s (0,6–2,4 s), edad prom. 0,53 s, RTT prom. 53 ms.

**Fallos simulados:**
* HTTP 500: a los 3 errores pasa al fallback.
* Cuelgue: timeout 4 s y cambio de host.
* Connection refused: el estado pasa a error y recorre los hosts.
* Respuesta sin ZEC: se cuenta, sin registrar nada.
* Recuperación: ~4 s después de que vuelve el host. A los 10 min regresa solo al primario.
* MariaDB caída 40 s: la cola se mantuvo y se vació al volver, sin huecos.
* Log: 1 aviso por minuto como máximo; 0 `[error]`.

**Integridad:**
* 811 filas = 811 claves únicas tras varios reinicios.
* `ts ≥ source_ts` siempre y `max ≥ min` siempre.
* DECIMAL exacto.

## Etapa 5 — LABORATORIO histórico (sólo lectura de `market_1s`)
```
[LABORATORIO ui_template] → LAB controlador (arma el SQL, 1 consulta a la vez) → [mysql: sólo SELECT] → resultado chico → LABORATORIO
```
* **Sin cambios en el resto del flow:** no se modificó ningún nodo existente.
* **Requisitos:** el usuario `prrr` sólo necesita `SELECT`, porque no se crean tablas, ni siquiera temporales. MariaDB ≥ 10.2 (funciones de ventana).
* **Variables en T:** se calculan con ventanas `ROWS BETWEEN 24/49 PRECEDING AND CURRENT ROW`, sólo pasado y presente.
  * Una observación es utilizable sólo si T−49…T son 50 segundos contiguos (`LAG(ts,49) = T−49 s`).
* **Labels futuros:** precio exacto en T+60/300/900/3600 s, por `LEFT JOIN` a la clave primaria. Si la fila no existe todavía o hay un hueco, el valor es NULL y la observación no cuenta para ese horizonte.
* **Resumen:** 7 condiciones descriptivas × 4 horizontes, con N, N indep. ≈ N·k/horizonte, media, mediana exacta (`ROW_NUMBER`) y % > 0.
  * Se marca "muestra insuficiente" si N indep. < 30.
  * Para períodos de más de 120 000 s, el resumen usa 1 de cada k segundos (muestreo sistemático) y lo indica en pantalla.
* **Rendimiento:** todo se calcula dentro de MariaDB y a Node-RED vuelven ~130 filas.
  * Cada sentencia tiene `max_statement_time = 120 s` y hay una sola consulta en vuelo. Sólo se ejecuta al abrir la pestaña, al cambiar parámetros o con ACTUALIZAR.
* **Modular:** `buildLab({featureSymbol, labelSymbol, …})`. BTC → ZEC = featureSymbol 'BTC', labelSymbol 'ZEC' (no expuesto todavía).
* **Validado con MariaDB 10.11 real:**
  * 10 343 observaciones con huecos y `no_trade`: las 15 variables y los labels coinciden exactamente con un cálculo independiente.
  * Resumen exacto, también con muestreo.
  * Sin look-ahead: alterar todo lo posterior a T* no cambia ninguna variable de entrada ≤ T*.
  * Con 2,4 M filas (7 d × 4 activos): 1h 0,2 s, 24h 9 s, 7d/todo ~26 s.
  * Mientras tanto, el PRRR procesó todos los eventos (lag mediana 1 ms, picos ≤ 63 ms) y el writer siguió guardando sin atraso extra.


**Etapa 4: `prrr-market-data-stage4.json`** (generador `tools/build-prrr-stage4.js`).

## Etapa 4 — Analítica 25/50 (sólo calcular y observar)
```
BUCKETS 1s ─┬→ MariaDB (sin cambios)
            └→ ANALÍTICA 25/50 (nuevo) → MERCADO mux → dashboard
```
* **Fuente:** los buckets de 1 s del normalizador (no RAW). Buffer propio de 50 buckets por activo.
* **Precio:** `price_sma_25/50` es la media de `close` de los últimos 25/50 segundos con precio; `no_trade` participa con su precio arrastrado. Vale `null` hasta completar la ventana. `price_sma_spread_pct = (SMA25/SMA50 − 1) × 100`.
* **Flujo:** `delta_25/50 = Σ delta_usd` y `volume_25/50 = Σ vol_usd`; `no_trade` aporta 0. `pressure = delta / volumen`, o 0 si el volumen es 0.
* **Vista:** en cada chart de precio se agregan 2 líneas finas neutras, SMA25 continua y SMA50 punteada, con un borde claro para que se lean sobre el precio.
  * Debajo hay una tira compacta: 25s / 50s (Δ, VOL, presión) y SMA25−SMA50 %. Verde/rojo indica sólo el signo.
* **Valores:** quedan en `global.get('md_an_2550','memory')`. No se escriben en MariaDB porque se derivan de `market_1s`.
* **Validado:**
  * Las 9 magnitudes coinciden con un recálculo independiente, en pruebas sintéticas (con `no_trade` y `no_price`) y en vivo contra los buckets de la memoria para los 4 activos.
  * CPU de Node-RED 11 % contra 10 % en la etapa 3 (ruido), con lag y cola idénticos.
  * MariaDB sigue guardando sin huecos.
  * MERCADO a 1920×1080 con Edge 67 %: 4 columnas de 707 px, charts de 689×714 y sin scroll.


**Etapa 3: `prrr-market-data-stage3.json`** (generador `tools/build-prrr-stage3.js`).

## Etapa 3 — Persistencia en MariaDB (`prrr_market.market_1s`)
```
BUCKETS 1s → (existente) ─→ DB writer (cola RAM acotada · lotes) ─→ [mysql] ─→ resultado ─┐
                                 ↑ tick 1 s            catch (errores) · status (conexión) ──┘
```
* **Nodo requerido:** `node-red-node-mysql`. Usa `mysql2` y es compatible con MariaDB. Se instala desde *Manage palette → Install* o con `cd ~/.node-red && npm i node-red-node-mysql`.
* **Credenciales:** en el config node **MariaDB prrr_market (localhost)** se cargan usuario `prrr` y su contraseña, que quedan en los credentials cifrados de Node-RED. No hay contraseñas en el código.
* **Origen de los datos:** los buckets exactos del normalizador existente, que no se recalculan.
* **Inserción:** `INSERT … VALUES ? ON DUPLICATE KEY UPDATE`, en lotes de hasta 400 filas cada ≤ 5 s, con un solo lote en vuelo.
* **Desacople:** el writer sólo encola, así que el PRRR nunca espera a la base.
* **Si MariaDB se cae:** el lote vuelve a la cola y se reintenta con backoff de 5 → 60 s; un lote sin respuesta en 30 s también se reintenta.
  * La cola está acotada a 14 400 filas (1 h). Si la caída dura más, se descartan las filas más viejas y se cuentan.
  * El error se registra en el log de Node-RED, sin repetir el mismo mensaje más de una vez por minuto.
* **Columna `ts`:** su tipo se detecta en `information_schema`. DATETIME/TIMESTAMP se guarda en **UTC**, BIGINT en ms epoch, INT en segundos epoch.
* **`no_trade`:** vale 1 en segundos sin trades (precio arrastrado). Los buckets `no_price` (todavía no hubo ningún precio) no se guardan.
* **Indicador en DIAGNÓSTICO:** DB conectada, último bucket guardado, filas guardadas y errores DB. Los contadores son de la sesión actual de Node-RED.
* **Validado con MariaDB 10.11 real:**
  * Escritura de los 4 activos con un segundo por fila.
  * Caída de MariaDB de 46 s sin huecos ni impacto en el PRRR (lag de event loop 1 ms).
  * Reinicio de Node-RED: el histórico permanece y no hay duplicados.
  * Al apagar Node-RED se pierden como máximo los ~5 s que estaban en cola (no pueden escribirse durante el cierre).

Consultas de verificación:
```sql
SELECT symbol, COUNT(*) filas, MIN(ts), MAX(ts), SUM(no_trade) seg_sin_trades FROM market_1s GROUP BY symbol;
SELECT * FROM market_1s WHERE symbol='BTC' ORDER BY ts DESC LIMIT 10;
SELECT symbol, ts, COUNT(*) c FROM market_1s GROUP BY symbol, ts HAVING c > 1;   -- debe estar vacío
```


**2e: `prrr-market-data-stage2e.json`** (generador `tools/build-prrr-stage2e.js`).

## 2e — Ventana visible, intervalo de ploteo, pausa (sólo presentación)
* **Selector anterior ("Render UI", 100–2000 ms, en DIAGNÓSTICO):**
  * Controlaba la **frecuencia de refresco**. Cada N ms el core enviaba, por fuente, **sólo el último trade** del intervalo.
  * No descartaba eventos (RAW, métricas y buckets siempre recibieron todo), pero **ocultaba picos intermedios** en el gráfico.
* **Ahora:**
  * **Ploteo (100/250/500/1000 ms, inicial 250):** sigue siendo la cadencia con la que se envían puntos al navegador. Por intervalo y fuente se envían **mín, máx y último con su hora real**, así que no se pierden picos.
  * **Dónde se ajusta:** se cambia desde MERCADO o desde el desplegable de DIAGNÓSTICO, que es el mismo control del servidor.
  * **Cabeceras:** también usan esta cadencia, igual que antes.
* **Ventana visible (5 min / 15 min / 1 h / 4 h, inicial 15 min):** control del navegador, compartida por los 4 gráficos, que quedan sincronizados en el mismo reloj. Se recuerda en el navegador.
* **PLOT feeder** (nuevo, en paralelo, lee una copia del MD STREAM): arma el agregado de cada intervalo y el historial de gráficos en 2 resoluciones.
  * A: 250 ms × 15 min. B: 5 s × 4 h.
  * Min/máx/último por slot, en arrays de tamaño fijo: ~203 KB por serie, ~6,3 MB para 31 series.
  * Un navegador nuevo pide el historial al abrir la página, y al cambiar de pestaña no se pierde.
* **Dibujo por columna de píxel:** rango min–máx + escalón. Nunca se dibujan más puntos que la resolución disponible.
  * **Redibujo** = máx(ploteo, resolución), con tope de 1 s. Por ejemplo, en 4 h 1 px ≈ 21 s.
* **Pausa / Volver a vivo:** congela la vista de los 4 gráficos y los Δ. Los datos siguen entrando.
* **Historial insuficiente:** se muestra "historial disponible: X de Y" y no se rellena hacia atrás.
* **Δ BUY−SELL:** siempre los últimos 60 s en buckets de 1 s, independiente de la ventana.
* **Medido** en Chromium a 1920×1080 con Edge 67 %, con 31 series de 4 h completas, ventana de 4 h y ploteo de 100 ms:
  * ~1,7 ms de dibujo por chart;
  * ~18 % del hilo principal (85 % antes del tope de redibujo);
  * heap JS estable en 7,5 MB.
* **Sin cambios:** WebSockets, normalizadores, core (métricas), normalizador 1 s, memoria y BUY/SELL.


**2d: `prrr-market-data-stage2d.json`** (generador `tools/build-prrr-stage2d.js`).

## 2d — MERCADO a pantalla completa (sólo layout)
* **Capa fija:** MERCADO es una capa `position: fixed` de `left:0` a `right:0` (100vw), debajo de la barra superior. Tiene `display:grid; grid-template-columns: repeat(4, minmax(0,1fr))`.
  * No depende de clases ni del ancho de los grupos del Dashboard. Ningún contenedor padre puede limitar su ancho, y tapa cualquier otro grupo que hubiera en la pestaña.
* **Columna:** cada una contiene nombre + precio, datos compactos, chart de precio (72 %) y Δ BUY−SELL USD (28 %).
* **Medido en 1920×1080 con Edge 67 %** (viewport 2866×1350): la grilla ocupa 0 → 2866 px y cada columna mide 707 px. Los 4 charts de precio miden 689×778 y los 4 Δ 689×287.
* **Sin cambios:** backend y DIAGNÓSTICO.


**2c: `prrr-market-data-stage2c.json`** (generador `tools/build-prrr-stage2c.js`).

## 2c — Pestañas MERCADO y DIAGNÓSTICO (sólo presentación)
* **MERCADO**: diseñada para 1920×1080 con Edge al 67 %, que da un viewport CSS de ≈ 2866×1350.
  * Un único widget con grilla CSS propia de 4 columnas iguales (`repeat(4, minmax(0,1fr))`), que nunca hace wrap. Ocupa el 100 % del ancho y del alto visibles.
  * **Fila 1:** BTC | ZEC | GMX | XMR. Cada columna tiene nombre, precio, último trade (fuente, BUY/SELL, edad, ev/s, trades/min), exchanges compactos y el chart PRRR de precio con autoescala Y.
  * **Fila 2:** Δ BUY−SELL USD · 60 s por activo.
  * Medido en ese escenario: los 4 charts de precio miden 687×706 px CSS y los 4 deltas 687×348, sin scroll horizontal.
  * Un multiplexor junta los mensajes de cada ciclo de render en 1 solo envío al navegador.
* **DIAGNÓSTICO**: es la pestaña anterior, con throughput/frescura, mercados, controles, métricas técnicas y la tabla de la memoria.
* **Nodos convertidos:** los viejos widgets de precio, tarjeta y delta pasaron a ser nodos `change` que etiquetan mensajes para MERCADO, con los mismos IDs.
* **Grupos que quedan huérfanos al importar con Replace:** `prrr_ui_g_zec`, `prrr_ui_g_gmx` y `prrr_ui_g_xmr` quedan como config nodes sin uso (no se muestran). Se pueden borrar desde el panel de configuración de Node-RED.
* **Sin cambios:** backend, RAW, core, buckets, memoria y cálculo del delta.


**2b: `prrr-market-data-stage2b.json`** (generador `tools/build-prrr-stage2.js`).

## 2b — Autoescala dinámica del eje Y (sólo presentación)
* **Escala:** min/max de los puntos **visibles** en la ventana de 5 min, tomando sólo las series con datos actuales, más 10% de padding.
* **Series stale** (último trade hace más de 60 s): se dibujan atenuadas, con "(stale)" en la leyenda, y no definen la escala.
* **Cambios de escala:** expansión inmediata si aparece un precio fuera de rango. Cuando un extremo viejo sale de la ventana, la escala se contrae suavemente (exponencial, τ = 1,5 s).
* **Rango mínimo:** 0,002% del precio (BTC ≈ 1,2 USD), para que un precio quieto no genere una escala degenerada.
* **Alcance:** cambian sólo los 6 `ui_template` de gráficos. RAW, core, buckets y memoria quedan idénticos.


**Etapa 2: `prrr-market-data-stage2.json`**. Contiene la etapa 1c intacta más el módulo de la etapa 2.

## Etapa 2 — Normalizador temporal 1 s + memoria 30 min
```
MD STREAM (RAW, intacto) ─→ NORMALIZADOR 1 s ─→ MEMORIA 1 s (RAM, 1800 × 4) ─→ BUCKETS 1s → (persistencia / indicadores futuros)
                                                                           └→ vista MEMORIA / NORMALIZADOR
```
* **Reloj común:** `local_receive_timestamp` (el reloj de la notebook). Todos los activos se alinean en los mismos segundos exactos `[t, t+1000)`. Un segundo se cierra 500 ms después de terminar, como margen para la cola interna. Un trade que llega después del cierre se cuenta como **tardío**; nunca se mueve a otro segundo.
* **Bucket** (una fila plana por activo y segundo): `t, open, high, low, close, trades, events, vol_usd, buy_usd, sell_usd, delta_usd, unknown_side_usd, exchanges, sources, spot_trades, perp_trades, synthetic, status`.
* **Lado sin determinar:** suma a `vol_usd` y `unknown_side_usd`, nunca a BUY ni SELL.
* **Segundo sin trades:** OHLC = último precio conocido, contadores en 0, `synthetic: true` y `status: "no_trade"`. Si todavía no hubo ningún precio, `status: "no_price"` y OHLC en `null`.
* **Memoria:** `global.get('md_mem_1s','memory').series[SYM] = { size:1800, buf, idx, count }`, un buffer circular que nunca crece.
* **Único cambio a la etapa 1c:** el link out `MD STREAM →` suma un destino (el normalizador). Adquisición, core y visualización quedan igual.


**Versión actual: `prrr-market-data-stage1c.json`** (generador `tools/build-prrr-stage1c.js`). Conserva todos los IDs de nodo de 1b: al importar, elegir **Replace**.

## Etapa 1c
* **Dashboard**: grilla 2×2 simétrica (BTC | ZEC / GMX | XMR). Cada instrumento tiene el mismo diseño: cabecera (precio, fuente, BUY/SELL, edad, ev/s, trades/min), precios por fuente y gráfico. Debajo, a ancho completo: throughput/frescura, mercados escuchados, controles y métricas técnicas.
* **Gráficos**: canvas liviano en `ui_template`. `ui_chart` redibujaba todo el gráfico por cada punto de cada serie y saturaba el navegador con 10+ fuentes. Ahora hay 1 mensaje por ciclo de render, lo que equivale a 1 redibujado. Los buffers están acotados (5 min por serie, máx. 1500 puntos) y cada fuente tiene el mismo color en todos los gráficos. Sólo se dibujan trades reales: no se extiende ni se interpola el precio.
* **trades/min reales** por fuente y mercado, en una ventana deslizante de 60 s. Para Binance perp se cuentan los fills reales dentro de cada `@aggTrade`.
* **Nuevas fuentes legítimas para GMX / XMR** (perpetuos del mismo activo, siempre como fuentes separadas `*-perp`):

| Fuente | GMX | XMR | Canal |
|---|---|---|---|
| binance-perp | GMXUSDT | XMRUSDT | USDⓈ-M `@aggTrade` (endpoint `/market`, migración de URLs de 2026) |
| bybit-perp | GMXUSDT | XMRUSDT | linear `publicTrade` |
| bitget (spot) | GMXUSDT | — | v2 `trade` |
| bitget-perp | GMXUSDT | XMRUSDT | v2 `trade` USDT-FUTURES |
| kraken-perp | PF_GMXUSD | PF_XMRUSD | Futures v1 `trade` |
| hyperliquid | — | XMR | `trades` (si el mercado es HIP-3, el coin lleva prefijo `dex:XMR`) |

* **Descartadas por ahora**: OKX swap y Gate futures (la cantidad viene en contratos: haría falta el multiplicador por REST), BitMEX (contratos), KuCoin (requiere token HTTP previo), HTX (frames gzip) y MEXC spot (protobuf).


**Etapa 1b (actual): `prrr-market-data-stage1b.json`** = etapa 1 + GMX y XMR. Usa los mismos IDs de nodo que la etapa 1: al importarlo, Node-RED avisa que los nodos ya existen y hay que elegir **Replace** para actualizar el flow en el lugar. El generador es `tools/build-prrr-stage1b.js`.

Flow de Node-RED: `prrr-market-data-stage1.json` (se importa desde **Import → Clipboard** o **Import → select a file**).
Lo genera `tools/build-prrr-stage1.js`: si editás el generador, corré `node tools/build-prrr-stage1.js`.

## Arquitectura

```
[autostart] → [switch UI] → [PRRR WS Connector (subflow)] → [normalizar <exchange>] → link out ─┐
                                        └──────── ws_status (1/s) ───────────────────→ link out ─┤
                                                                                                 ▼
                              [tick 50 ms] ──→ MD BUS (link in) → MD CORE ──┬─ salida 1: MD STREAM → (indicadores futuros)
                                                                            └─ salidas 2..8: dashboard (a la frecuencia de render)
```

* **Subflow `PRRR WS Connector`**: un único cliente WebSocket genérico (módulo `ws`) que se reutiliza para todos los exchanges.
  Hace la reconexión con backoff (de 1 s a 30 s), tiene un watchdog que reconecta si el canal queda en silencio (`STALE_MS`), manda el ping de aplicación (`PING_MS`/`PING_PAYLOAD`) y se prende o apaga con `{topic:"enable"}`. Se configura con las variables de entorno de cada instancia.
* **Normalizador por exchange**: convierte cada trade al formato común, sin agregar nada. Descarta y cuenta los mensajes inválidos.
* **MD CORE**: el trabajo por trade es O(1). Acumula contadores por exchange y por símbolo, calcula latencias y guarda un buffer circular de 5000 trades por símbolo (`global.md_ring`). Reenvía cada trade sin tocarlo por `MD STREAM →`.
  El dashboard se actualiza con un **tick de 50 ms**, separado de la llegada de trades: los gráficos se redibujan cada 100/250/500/1000/2000 ms (se elige desde la UI) y las estadísticas se calculan cada 1 s (`global.md_stats`).

## Exchanges

| Exchange | BTC | ZEC | Canal | Lado agresor | Timestamp |
|---|---|---|---|---|---|
| Binance | BTCUSDT | ZECUSDT | `@trade` | `m` | **µs** (`timeUnit=MICROSECOND`) |
| Coinbase | BTC-USD | ZEC-USD | `matches` | inverso de `side` (es el lado del maker) | µs (ISO) |
| Kraken | BTC/USD | ZEC/USD | v2 `trade` | `side` | µs (ISO) |
| OKX | BTC-USDT | ZEC-USDT | `trades-all` (sin agregar) | `side` | ms |
| Bybit | BTCUSDT | ZECUSDT | `publicTrade` | `S` | ms |
| Bitfinex | tBTCUSD | tZECUSD | `trades` (`te`) | signo de `amount` | ms |

### Etapa 1b: GMX y XMR

| Exchange | GMX | XMR | Canal | Lado agresor | Timestamp |
|---|---|---|---|---|---|
| Binance | GMXUSDT | no lo lista (deslistado en 2024) | `@trade` | `m` | µs |
| OKX | GMX-USDT | no lo lista (deslistado en 2024) | `trades-all` | `side` | ms |
| Bybit | GMXUSDT | no | `publicTrade` | `S` | ms |
| Kraken | GMX/USD | XMR/USD | v2 `trade` | `side` | µs |
| Bitfinex | no | tXMRUSD | `trades` (`te`) | signo de `amount` | ms |
| **Gate** (nuevo) | GMX_USDT | XMR_USDT | `spot.trades` | `side` | ms con decimales |
| **Poloniex** (nuevo) | no | XMR_USDT | `trades` | `takerSide` | ms |

Gate y Poloniex solo se suscriben a GMX y XMR, así que los datos de BTC y ZEC quedan igual que en la etapa 1. GMX (el DEX) no se usa como fuente porque no ofrece un WebSocket público de precios.
Todo evento lleva ahora `kind: "trade"` (campo nuevo). El resto del formato no cambió.

## Métricas del dashboard

* **ev/s** por exchange, **BTC ev/s** y **ZEC ev/s**, total y pico.
* **frames/s**: los frames WebSocket crudos, incluidos los heartbeats.
* **edad**: tiempo desde el último trade de cada símbolo en cada exchange.
* **exch→local (ms)** = `local_receive_timestamp − exchange_timestamp`. Depende de qué tan sincronizado esté el reloj de la notebook: activá NTP (`timedatectl set-ntp true` o chrony). El valor **mín** es la mejor estimación de la latencia de red más el offset de reloj.
* **cola NR (ms)**: el tiempo que pasa desde que llega el frame hasta que el trade entra al core. **No depende de ningún reloj externo.** Si sube, Node-RED se está saturando: es el indicador de que empieza a perderse frescura.
* **lag event-loop**: la demora del tick de 50 ms. También sube cuando la notebook no da abasto.

## Notas

* Hace falta el módulo `ws`. Node-RED lo instala solo la primera vez si `functionExternalModules: true` está en `settings.js` (es el valor por defecto desde la v3). Si lo tenés en `false`, activalo o instalalo a mano con `cd ~/.node-red && npm i ws`.
* El estado de runtime se guarda en el context store `memory`. Si no está definido, Node-RED usa el store por defecto y puede loguear un aviso una sola vez. Si tu store por defecto es `localfilesystem`, agregá uno en memoria: `contextStorage: { default: {module:"localfilesystem"}, memory: {module:"memory"} }`.
* Algunos exchanges bloquean ciertos países (por ejemplo, Binance.com y Bybit desde EE.UU.). En ese caso, apagalos con su switch.
