# PRRR Market Data — Etapa 1 / 1b / 1c

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
