# PRRR Market Data — Etapas 1 → 2

**Versión actual: `prrr-market-data-stage2e.json`** (generador `tools/build-prrr-stage2e.js`). Al importar, elegir **Replace**.

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
