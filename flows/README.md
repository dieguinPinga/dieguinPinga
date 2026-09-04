# CRYPTO LITE V3 — tablero Node-RED (BTC · XMR · GMX · LTC)

Tablero liviano para Raspberry Pi lentas. Muestra las 4 monedas en un panel
resumen + una tarjeta y un gráfico por moneda, y **guarda los precios en disco
para poder ver varios días de historial** (aguanta reinicios de Node-RED).

Archivo importable: [`crypto-lite-v3.json`](./crypto-lite-v3.json)

---

## Qué trae

- **Panel RESUMEN** arriba: las 4 monedas en una grilla 2×2 con precio,
  variación % de la última hora (verde/rojo) y "frescura" del dato en segundos.
- **EN VIVO · 1s**: 4 gráficos chiquitos (grilla 2×2) con 1 punto por segundo y
  ventana de 2 min. El eje Y se auto-ajusta → "zoom" de los micro-movimientos del
  precio actual. Son efímeros: **no** se guardan a disco.
- **Por moneda**: una tarjeta de detalle + un gráfico de precio.
  - BTC: flujo BTC/s, promedio y RSSI (viene por MQTT).
  - XMR / GMX / LTC: bid, ask y edad del feed (vienen por WebSocket de Kraken).
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
- Para XMR/GMX/LTC: salida a internet hacia `wss://ws.kraken.com`.

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

- ¿Querés ver más días en el gráfico? Subí `cryptoViewDays` y también
  el `removeOlder` de cada nodo `ui_chart` (viene en 3 días). Ojo: más puntos =
  más trabajo para el navegador. Si vas a muchos días, conviene además espaciar
  el guardado (nodo **Historial · cada 3min**: subí `repeat` a 300 = 5 min).
- ¿Querés guardar más historia? Subí `cryptoKeepDays`. El disco usado es chico
  (~30 bytes por punto → ~4 KB por moneda por día).

## Notas

- Si un feed no llega, la moneda aparece atenuada con un punto rojo en el panel.
- Kraken pide `GMX/USD`; si tu Kraken no lista ese par, esa tarjeta quedará
  vacía (las otras funcionan igual).
- Para que el historial sobreviva reinicios **de la Pi**, alcanza con este
  esquema (se guarda en archivos); no depende de la persistencia de contexto de
  Node-RED.
