# PRRR Market Data — Etapa 1

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
