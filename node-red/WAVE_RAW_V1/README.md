# WAVE_RAW_V1 — sensor de alta frecuencia BTC (Node-RED)

**Archivo a importar:** `WAVE_RAW_V1.json`. En Node-RED, abrí *Menú → Import*, pegá o elegí el archivo y elegí *Import to: new flow*.
Crea un tab nuevo e independiente, `WAVE_RAW_V1`, y no toca ningún flow existente.

Requisitos: `functionExternalModules: true`. Node-RED instala solo el módulo `ws` la primera vez que despliega el flow.

## Arquitectura

```
[WS ADAPTER x4] --frames crudos--> [NORMALIZER x4] --eventos--> [WAVE RAW ENGINE] --1 Hz--> [Debug PULSE]
       ^                                  |                                       \-> [Debug TELEMETRY] (inactivo)
       +------------- resync -------------+
```

| Nodo | Qué hace |
|---|---|
| WS ADAPTER · X | Abre 2 WebSockets persistentes, uno para `trade` y otro para `book`. En `ws.on('message')` la primera línea es `const t = Date.now();`. Después emite `{exchange, conn, gen, t_recv, payload}` sin parsear y sin clonar. Reconecta con backoff exponencial de 1 a 30 s con jitter. Tiene watchdog de feed stale (20 s sin mensajes), timeout de handshake, ping de protocolo cada 10 s y ping de aplicación en OKX y Kraken. |
| NORMALIZER · X | Parsea los mensajes y aplica la semántica de cada API. Mantiene el libro en memoria, pero sólo emite BBO + top5/top10. Pide resync al adaptador cuando el libro queda corrupto. Una vez por segundo envía sus contadores. |
| WAVE RAW ENGINE | Event-driven: cada mensaje actualiza en el acto las ventanas de 100/250/500/1000 ms (sumas incrementales y deques monótonos). Un timer de 1 s sólo lee el estado y emite el PULSE y la TELEMETRY. |

No incluye dashboard, MySQL, escritura a disco, context store, órdenes, señales LONG/SHORT, SMA ni velas.

## Canales y semántica por exchange

| Exchange | Trades | Lado agresor | Book |
|---|---|---|---|
| Binance `BTCUSDT` | `@trade`, 1 trade por mensaje | `m=true` (el comprador es maker) ⇒ **SELL**; `m=false` ⇒ **BUY** | `@bookTicker` (BBO en tiempo real) + `@depth10@100ms` (top 10 completo, sin snapshot REST) |
| Coinbase `BTC-USD` | `market_trades` (el snapshot histórico inicial se ignora) | `side` es el lado del **maker** ⇒ agresor = opuesto (`CB_SIDE_FIELD_IS_MAKER`) | `level2`: snapshot completo + updates con cantidad absoluta; libro completo en memoria; un gap de `sequence_num` dispara resync |
| Kraken `BTC/USD` | v2 `trade` | `side` = lado del taker | v2 `book` depth 10: snapshot + updates, truncado a 10 niveles y checksum CRC32 verificado |
| OKX `BTC-USDT` | `trades-all` (endpoint `/business`, sin agregación) | `side` = lado del taker | `bbo-tbt` (tick a tick) + `books` (400 niveles: snapshot + updates con continuidad `prevSeqId`/`seqId`; checksum si viene distinto de 0) |

`side_check_agree_pct` en el PULSE compara cada trade contra el BBO previo: precio ≥ ask ⇒ BUY, precio ≤ bid ⇒ SELL. Con datos reales debería quedar claramente por encima de 70–80 %. Si quedara muy bajo, la semántica de ese exchange estaría invertida. Este control es la verificación en vivo de la convención de Coinbase.

## Qué mirar en el PULSE (1 Hz)

- `trade_eps` / `book_eps`: eventos normalizados por segundo. `*_fps`: frames WebSocket crudos por segundo, incluidos heartbeats.
- `*_interarrival_ms`, `*_ia_p50_ms`, `*_ia_p95_ms`: tiempo entre frames con datos, es decir, la **resolución temporal real**.
- `*_latency_p50_ms`: `local_receive_timestamp - exchange_timestamp`. Depende de NTP y es `null` en el libro de Binance spot, que no trae timestamp.
- `*_age_ms`: edad del último evento.
- `buy_usd_250ms`, `sell_usd_250ms`, `delta_usd_250ms`, `price_move_250ms_bps`, `max_excursion_250ms_bps`, más el delta y el movimiento de 1000 ms.
- `best_bid`/`best_ask`, `spread_bps`, `book_imbalance` (L1), `book_imbalance_5`, `book_imbalance_10` e `imb_vel_250ms_per_s`.
- `ALL`: flujos consolidados. El precio usa la **mediana** de los movimientos por exchange, porque USD y USDT no se mezclan.
- `ranking`: qué exchange entrega mayor frecuencia y menor latencia.
- `node.pipeline_lag_*`: tiempo entre el socket y el ENGINE. Si crece de forma sostenida, Node-RED está acumulando cola. `node.loop_lag_*` mide el lag del event loop.

TELEMETRY, en el Debug inactivo, trae todas las ventanas, percentiles (n/mean/p50/p95/min/max), contadores del normalizer (inválidos, descartados, gaps, resyncs, checksums) y el estado de cada conexión.

## Notas operativas

- Binance devuelve HTTP 451 desde EE. UU. En ese caso, cambiá la URL en *On Start* del adaptador por `stream.binance.us`.
- Kraken: el checksum usa precisión de precio 1 y de cantidad 8 para BTC/USD. Si nunca valida, se desactiva solo tras 3 fallos y avisa.
- OKX: desde 2026-06 el checksum de `books` llega en 0 (deprecado). La integridad se controla por `seqId`/`prevSeqId`.

## Build y pruebas

```
node build.js        # regenera WAVE_RAW_V1.json desde src/
node validate.js     # IDs únicos, wires, tipos prohibidos, timestamp antes del parseo, sintaxis
node test/run_e2e.js <dir-con-node-red-instalado> 50   # Node-RED real + exchanges simulados con fallos
```

La prueba e2e levanta servidores que imitan el formato de cada exchange e inyecta cuatro fallos:

- un socket cortado (Binance);
- un gap de secuencia (Coinbase);
- un gap de `seqId` (OKX);
- un feed mudo con el socket abierto (Kraken).

Verifica la reconexión, el resync y el watchdog. Los checksums de Kraken y OKX se validan contra libros generados de forma independiente.
