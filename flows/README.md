# CRYPTO LITE V3 — tablero Node-RED (BTC · XMR · GMX · LTC)

Tablero liviano para Raspberry Pi lentas. Muestra las 4 monedas en un panel
resumen + una tarjeta y un gráfico por moneda, y **guarda los precios en disco
para poder ver varios días de historial** (aguanta reinicios de Node-RED).

Archivo importable (última versión): [`crypto-lite-v29.json`](./crypto-lite-v29.json)
— **break-even de LTC en 55.57** (línea del gráfico + fila de la tarjeta) y **alarma sonora al
cruzar la barrera**: cuando el precio de LTC cruza el break-even (para arriba o para abajo) suena
una **sirena molesta** (onda cuadrada, fuerte) e imposible de confundir con los ticks normales,
más un cartel ⚠ que dice hacia dónde cruzó. Tiene banda muerta (histéresis) y cooldown para no
repetir en el borde. El valor se edita en el beeper (`var BE = { LTC: 55.57 }`) y en el config
(`cryptoBreakeven`). La sirena ignora el mute por moneda (siempre te avisa) pero respeta el ON/OFF
y el volumen general.

v28: **el sonido ahora suena parejo en las 4 monedas**: antes el beeper se alimentaba de BTC (MQTT,
~2/s) y de los ticks esporádicos de Kraken, así que casi solo se escuchaba BTC. Ahora lo alimenta
el **muestreador de 1s** (tiene el precio de las 4, refrescado por REST cada 2s), con cadencia
pareja y sin depender del WebSocket. Cada moneda suena cuando **realmente** se mueve.

v27: **sonido activado por default**: viene en ON y se **auto-desbloquea con el primer clic/tecla**
en cualquier parte de la página (los navegadores no dejan sonar sin una interacción; ya no hace
falta buscar el botón). El botón pasó a ser ON/OFF (🔊/🔇) y la elección se recuerda.

v26: **un instrumento distinto por moneda** (sintetizado con armónicos + envolvente ADSR, no las
ondas crudas): **BTC 🔔 campana**, **XMR 🪵 marimba**, **GMX 🎸 cuerda pulsada**, **LTC 🎹 órgano**,
separados por octavas. La dirección es un **gesto de 2 notas** (sube = ascendente / baja =
descendente) en vez de glissando, mucho más claro. Volumen proporcional a la magnitud. Botón
**▶** por moneda para escuchar cada instrumento.

v25: una voz por moneda con ondas crudas (sine/triangle/square/sawtooth) + glissando — quedaban
parecidas entre sí; v26 las reemplaza por instrumentos sintetizados bien distintos.

v24: **ticks sonoros integrados**: el beeper (Web Audio) ya viene cableado al feed **real**
(BTC por MQTT + XMR/GMX/LTC por Kraken), así **cada variación de precio suena**. Vive arriba de
los gráficos EN VIVO: apretá **🔓 Activar sonido** una vez (el navegador lo exige), regulá el
volumen y hacé **clic en cada moneda para silenciarla** (se recuerda). El beep es proporcional
(volumen = magnitud vs volatilidad 2 min; tono agudo+verde = sube, grave+rojo = baja).

v23: **aprovecha toda la telemetría MQTT de BTC**: además del precio compuesto ahora usa
`binance` y `coinbase` **por separado**, sus edades (`binance_age_ms`/`coinbase_age_ms`),
`flow_btc_m` y la **señal `LONG/SHORT`** (`signal`/`signal_source`/`signal_samples`) que ya
mandaba el sensor y se estaban descartando. En el **gráfico BTC en vivo** se ven 3 líneas
(compuesto en dorado, Binance en cian, Coinbase en azul, con leyenda); en la **tarjeta BTC**
hay filas de Binance/Coinbase con su edad, el **spread CB−BIN** ($ y %) y un **badge verde/rojo**
con la señal.

v22: **filtro anti-outlier en BTC**: rechaza saltos absurdos vs el último precio bueno
(`cryptoBtcMaxJump`, 15%) → un glitch del sensor (ej. 100k) ya no ensucia el histórico.
Para purgar datos malos existentes, usar [`reset-btc-snippet.json`](./reset-btc-snippet.json).

**Probar los parlantes:** [`audio-test-snippet.json`](./audio-test-snippet.json) — flujo
autocontenido (crea su propia pestaña **🔊 TEST AUDIO**) con dos botones: uno hace sonar el
**SERVER** (la notebook que corre Node-RED, vía un nodo `exec` que prueba `paplay`→`aplay`→
`speaker-test`) y otro suena en el **NAVEGADOR** donde ves el dashboard (nodo `ui_audio`, voz
del navegador). El botón del server muestra el resultado (`✅ rc 0` o el error) en la tarjeta
"Estado server". Si el server no suena aunque los parlantes andan, casi siempre es que Node-RED
corre como servicio sin sesión de audio: instalá `pulseaudio-utils`/`alsa-utils`, o arrancá
Node-RED desde tu sesión de escritorio.

**Alertas sonoras de precio (tick-flash sonoro):**
[`audio-tick-snippet.json`](./audio-tick-snippet.json) — flujo autocontenido (pestaña
**🔊 TICKS SONOROS**) que convierte cada cambio de precio en un **beep proporcional**, la versión
audible del tick-flash: **volumen = magnitud** del cambio (normalizada por la volatilidad de los
últimos 2 min, como la opacidad del parpadeo) y **tono/color = dirección** (sube = beep agudo
+ verde, baja = beep grave + rojo). Usa **Web Audio** dentro de un `ui_template` (no `ui_audio`,
que solo hace TTS). Trae un **simulador** (inject "▶ demo") para escucharlo al instante.
- Primero apretá **🔓 Activar audio** (el navegador exige un click para habilitar sonido).
- Para engancharlo al **precio real**: en el editor, cableá las salidas de tick en vivo del flujo
  principal hacia el nodo `ui_template` **beeper** — la de *BTC guardar* (`22a9a563f73205fe`) y las
  4 de *Guardar Kraken* (`942b2551f3443d26`), que ya emiten `{payload: precio, topic: 'BTC'|…}`.
  Ahí desactivá/borrá el inject "▶ demo".

v21: **BTC = solo MQTT como verdad del precio**, gana el mensaje con **timestamp más nuevo**;
Kraken/REST ya no tocan el precio de BTC (solo vol24/bid/ask).

v20: BTC por MQTT primario (Kraken/REST eran respaldo del precio si MQTT callaba >15s).

v19: **WS reactivo** (tick directo a los gráficos en vivo), historial a 30s y trades a 10s.

v18: "power pack" — historial 1 punto/min, **doble EMA** (lenta 20 + rápida 9,
`cryptoEmaFast`), trades a 20s.

v17: **BTC con 2 decimales** (tarjeta y resumen; antes iba redondeado).

v16: **ticker REST a 2s** (más fluido) y **margen fijo por moneda** en los gráficos en vivo
(evita el overshoot del eje): BTC +$15, XMR +$0.50, GMX +$0.02, LTC +$0.40 (editable en
*Precio actual (1s)*).

v15 **corta la línea en los huecos**
de los 4 gráficos grandes (apagón/feed caído) con un punto `null`, para no "pegar" datos
de timestamps separados. Umbral configurable `cryptoGapMin` (10 min). También reinicia la
EMA tras el hueco.

v14 unificó el fondo (`#0d1117`) y agregó identidad de color por moneda (borde + símbolo).

v12 agregó **margen de eje** en los gráficos EN VIVO (2 series invisibles piso/techo,
sin `ui_control`, no borra datos). Los gráficos grandes quedan sin margen todavía.

v11 sincronizó el flash con los gráficos en vivo (tarjetas a 1s).

v10 hizo el **tick-flash proporcional**:
la opacidad del parpadeo depende del tamaño del cambio, normalizado por la volatilidad
de los últimos 2 min (cambio chico → flash tenue; salto grande → flash fuerte). En
tarjetas y resumen, vía `ng-style` (no toca los datos del gráfico).
v9 subió el poll de trades a 30s.

⚠️ `crypto-lite-v8.json` quedó DESCARTADO: intentaba dar padding al eje Y con
`ui_control`, pero en node-red-dashboard eso **borra los puntos del gráfico** cada vez
que se reenvía (no borra el disco, solo el dibujo). v9 revierte ese cambio.
v7 llevó el tick-flash al panel RESUMEN.
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
