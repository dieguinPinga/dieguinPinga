# CRYPTO LITE V3 — tablero Node-RED (BTC · XMR · GMX · LTC)

Tablero liviano para Raspberry Pi lentas. Muestra las 4 monedas en un panel
resumen + una tarjeta y un gráfico por moneda, y **guarda los precios en disco
para poder ver varios días de historial** (aguanta reinicios de Node-RED).

Archivo importable (última versión): [`crypto-lite-v7.json`](./crypto-lite-v7.json)
— sobre [`crypto-lite-v6.json`](./crypto-lite-v6.json) extiende el **tick-flash**
también al panel RESUMEN (cada tile parpadea verde/rojo al cambiar su precio).
v6 agregó el tick-flash en las tarjetas de detalle; sobre
[`crypto-lite-v5.json`](./crypto-lite-v5.json).
v5 trae **break-even** por moneda (fila % en tarjeta + línea amarilla en el gráfico,
config `cryptoBreakeven`, ej. LTC 54.85), sobre la base hand-tuned
[`crypto-lite-v4.json`](./crypto-lite-v4.json). El [`crypto-lite-v3.json`](./crypto-lite-v3.json) queda de referencia.

---

## Qué trae

- **Panel RESUMEN** arriba: las 4 monedas en una grilla 2×2 con precio,
  variación % de la última hora (verde/rojo) y "frescura" del dato en segundos.
- **EN VIVO · 1s**: 4 gráficos chiquitos (grilla 2×2) con 1 punto por segundo y
  ventana de 2 min. El eje Y se auto-ajusta → "zoom" de los micro-movimientos del
  precio actual. Son efímeros: **no** se guardan a disco.
- **Por moneda**: una tarjeta de detalle enriquecida + un gráfico de precio con
  **media móvil (EMA)** superpuesta.
  - Tarjeta: **Mín/Máx** (con barra de rango que marca dónde está el precio hoy),
    **Δ1h y Δ24h**, y para las de Kraken **Vol 24h + nº de operaciones**.
  - BTC: precio/vol/presión por WebSocket de Kraken (par XBT/USD), **más** el
    flujo BTC/s, promedio y RSSI de tu sensor por MQTT (se muestran como "Sensor").
  - XMR / GMX / LTC: además bid, ask, edad del feed, y **presión compra/venta**
    (últimos 5 min, barra verde/roja) vía el canal de *trades* de Kraken.
- **Historial en disco**: 1 punto cada 3 min por moneda, guardado en
  `~/.node-red/cryptohist/<MONEDA>.log` (un JSON por línea). Al arrancar,
  Node-RED recarga ese historial en los gráficos (por defecto muestra 3 días).
- **Recorte automático** cada 6 h para que los archivos no crezcan sin fin
  (por defecto conserva 7 días).

## Por qué es liviano

- Las **tarjetas** se refrescan rápido (cada 5 s) → se siente ágil.
- Los **gráficos e historial** van despacio (1 punto cada 3 min) → con 3 días
  son ~1.440 puntos por gráfico: poca memoria, poca escritura a disco y el
  navegador no se ahoga.
- Al cargar el historial se hace **downsample** a máx. 600 puntos por gráfico.
- Los gráficos **EN VIVO (1s)** son livianos: ventana de 2 min = ~120 puntos c/u,
  y no tocan disco. Si tu feed no actualiza tan seguido, la línea queda escalonada
  (muestra el último precio conocido); si el feed se cae >30 s, deja de dibujar.
- Usa solo **nodos nativos** de Node-RED (`file` / `file in`): no hay que
  instalar SQLite ni módulos extra.

## Requisitos

- Node-RED con **node-red-dashboard** instalado.
- Para BTC: un broker MQTT (por defecto `127.0.0.1:1883`, ej. Mosquitto local)
  publicando en `btc/#` un JSON tipo:
  `{"price":65000,"flow_btc_s":0.12,"rssi":-70,"device":"esp32"}`.
- Para BTC/XMR/GMX/LTC: salida a internet hacia `wss://ws.kraken.com/v2`
  (tiempo real, WebSocket v2 de Kraken) y a `https://api.kraken.com` (latido
  REST de respaldo). Kraken es de los pocos exchanges que aún lista XMR (Monero).

## Cómo importar

1. En Node-RED: menú (☰) → **Import** → pegá el contenido de
   `crypto-lite-v3.json` → **Import**.
2. **Deploy**.
3. Abrí el dashboard en `http://<ip-de-la-pi>:1880/ui` → pestaña
   **CRYPTO LITE V3**.

La carpeta `cryptohist/` se crea sola bajo el directorio de Node-RED
(`~/.node-red/`). El historial se ve completo recién después de tener la Pi
prendida un rato (se llena a 1 punto/min).

## Ajustes (nodo "Config al arrancar")

Editá el nodo function **Set config global** para cambiar el comportamiento:

| Variable            | Default        | Qué hace                                        |
|---------------------|----------------|-------------------------------------------------|
| `cryptoHistBase`    | `cryptohist/`  | Carpeta donde se guardan los `.log`.            |
| `cryptoViewDays`    | `3`            | Días que se **muestran** en los gráficos al cargar. |
| `cryptoKeepDays`    | `7`            | Días que se **guardan** en disco.               |
| `cryptoStaleSec`    | `600`          | Segs sin dato para marcar la moneda como "caída" (gris). |
| `cryptoEmaN`        | `20`           | Períodos de la media móvil (EMA). A 3 min/punto ≈ 1 h.  |

- ¿Querés ver más días en el gráfico? Subí `cryptoViewDays` y también
  el `removeOlder` de cada nodo `ui_chart` (viene en 3 días). Ojo: más puntos =
  más trabajo para el navegador. Si vas a muchos días, conviene además espaciar
  el guardado (nodo **Historial · cada 3min**: subí `repeat` a 300 = 5 min).
- ¿Querés guardar más historia? Subí `cryptoKeepDays`. El disco usado es chico
  (~30 bytes por punto → ~4 KB por moneda por día).

## Notas

- Si un feed no llega en `cryptoStaleSec` (10 min por defecto), la moneda
  aparece atenuada con un punto rojo. Kraken solo manda precio cuando hay
  operaciones, así que monedas de bajo volumen (GMX, XMR) pueden estar minutos
  "quietas" sin estar caídas: por eso el umbral es amplio.
- **Ticker REST** (nodo *REST ticker · 20s*): cada 20 s trae precio/vol de las
  4 monedas por HTTP. Es la fuente principal si el WebSocket no llega.
- **Trades REST** (nodo *REST trades · 60s*): cada 60 s trae las operaciones
  recientes (con lado compra/venta) por HTTP y alimenta las **barras de presión**
  sin depender del WebSocket. Ideal si tu red bloquea WebSockets.
- Kraken pide `GMX/USD`; si tu Kraken no lista ese par, esa tarjeta quedará
  vacía (las otras funcionan igual).
- **Presión compra/venta**: se calcula de las operaciones reales de Kraken
  (canal *trades*, últimos 5 min). En monedas de bajo volumen (GMX) puede tardar
  en tener datos hasta que ocurra alguna operación. **BTC no tiene split** real:
  el feed MQTT manda un flujo único. Por eso BTC ahora también toma precio y
  operaciones del WebSocket de Kraken (XBT/USD) y así tiene barra de presión real.
- **BTC: dos fuentes.** Kraken (XBT/USD) manda el precio de mercado, volumen y
  presión; tu sensor MQTT aporta flujo/RSSI/device (línea "Sensor") y queda de
  respaldo del precio si el WebSocket de Kraken se cae >2 min.
- **Vol 24h / operaciones**: vienen en el ticker de Kraken (ticker + REST), sin
  costo extra, ahora también para BTC.
- La **EMA** se reconstruye desde el historial en disco al arrancar, así que la
  línea de tendencia aparece completa apenas cargás.
- Para que el historial sobreviva reinicios **de la Pi**, alcanza con este
  esquema (se guarda en archivos); no depende de la persistencia de contexto de
  Node-RED.
