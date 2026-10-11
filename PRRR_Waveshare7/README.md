# PRRR BITCOIN — Waveshare ESP32-S3-Touch-LCD-7 (800×480)

Port del monitor PRRR (ESP32 + ST7789 172×320) a la **Waveshare ESP32-S3-Touch-LCD-7**
(ST7262 RGB, táctil GT911, 8 MB PSRAM) usando **LVGL 8.3.11** y **ESP32_Display_Panel**.

Se conservaron los cálculos, los WebSockets y la estética del original: precio ponderado por
flujo, buckets lógicos, SMA 25/55, LONG/SHORT, BTC/s, río de flujo con dithering, waterfall
en perspectiva, escala de ticks y actualización parcial. El sketch original quedó como
referencia en `original/PRRR_ST7789_original/` (sin las claves WiFi).

![Simulación del port, ventana 5m](docs/simulador_5m.png)

## Qué está verificado y qué no

| Verificado | Cómo |
|---|---|
| Compila sin errores con ESP32 core **3.3.12**, placa *Waveshare ESP32-S3-Touch-LCD-7*, PSRAM Enabled, 8 MB, Huge APP | `arduino-cli` con ESP32_Display_Panel 1.0.5, ESP32_IO_Expander 1.1.1, esp-lib-utils 0.3.0, lvgl 8.3.11, WebSockets 2.7.3, ArduinoJson 7.4.2. Usa 52 % de flash y 17 % de RAM interna |
| Lógica de cálculo, dibujo y LVGL | Mismos archivos `.cpp` compilados en una PC con LVGL real y trades sintéticos. Las capturas de `docs/` salen de esa simulación, incluido un toque simulado en los botones de ventana |
| **No verificado en hardware** | FPS real, uso del ancho de banda de la PSRAM y conexión con exchanges reales desde este sketch. Ver "Primer arranque" |

## Arquitectura

```
 Tarea LVGL (lvgl_v8_port, prioridad 2)      Tarea loop() (Arduino, prioridad 1)
 ───────────────────────────────────────     ─────────────────────────────────────────
 lv_timer_handler(): refresco + táctil        feedsLoop(): WiFi + wsBinance/wsCoinbase.loop()
        │                                         └─ callbacks → market.cpp (solo datos)
        │ botón tocado → atomic<int>              updateFlows(), updateVirtualLed()
        │ requestedWindow ───────────────────▶   marketApplyWindow() si hay pedido
        │                                         render.cpp → Canvas565 (buffer TRASERO, PSRAM)
        │                                         publishFrame():
        │◀──── lvgl_port_lock(20 ms) ─────────      copia rects sucios → buffer del lv_canvas
        │                                            uiApply(textos ya formateados)
        │                                          lvgl_port_unlock()
```

- **Ningún callback WebSocket llama a LVGL.** Los callbacks solo actualizan el modelo, igual
  que en el original.
- El dibujo por píxeles (lento) se hace **fuera** del lock, en un buffer privado. Bajo el lock
  solo se copian las zonas modificadas y se asignan los textos ya armados. Así LVGL nunca
  muestra un cuadro a medio dibujar y la red no queda bloqueada esperando a la pantalla.
- Si el lock está ocupado (`lvgl_port_lock(20)` falla), las zonas sucias se conservan y se
  publican en la vuelta siguiente.
- Serial: solo eventos de conexión y un resumen cada 10 s. Nunca se imprime cada trade.

### Archivos

| Archivo | Contenido |
|---|---|
| `PRRR_Waveshare7.ino` | `setup()` (inicialización de la placa como el ejemplo oficial), `loop()`, formato de textos y `publishFrame()` |
| `prrr_config.h` | Colores, tiempos, geometría 800×480, ventanas y activación por etapas |
| `market.h/.cpp` | Modelo original: exchanges, precio ponderado, buckets, SMA, flujo, LED virtual |
| `feeds.h/.cpp` | WiFi multi-red y WebSockets Binance/Coinbase (mismos endpoints) |
| `render.h/.cpp` | Funciones de dibujo originales (precio, SMA, ticks, río y waterfall) |
| `canvas565.h/.cpp` | Reemplazo de Arduino_GFX: primitivas RGB565 con doble buffer y rectángulos sucios |
| `color565.h` | `lerp565`, `scale565` |
| `ui.h/.cpp` | Objetos LVGL: precio, BTC/s, LONG/SHORT, exchanges, barras, escala y botones |
| `wifi_secrets.example.h` | Plantilla de credenciales (copiar a `wifi_secrets.h`, que no se sube) |

## Qué se reutilizó y qué se adaptó

| Parte del original | En el port |
|---|---|
| `recomputeWeightedPrice`, `updateFlows`, `isFresh`, stale 15 s | Igual. Precios en `double` |
| Buckets lógicos (`ingestChartSample`, `writeBucketSample`, `rebuildVisibleHistory`) | Igual. El id de bucket avanza solo cuando llega un trade |
| `calcSMAAt`, `updateCrossSignal`, `trendBullRatioAt` | Igual. Sumas en `double` |
| `computeChartRange` (+8 % de margen), `chartRangeChanged` | Igual |
| Dibujo de precio en escalones, SMA de doble trazo, ticks $10/$100/$1000 | Igual, sobre `Canvas565` |
| `drawFlowBand`, `drawFlowWaterfall`, `drawLiveFlowFront` | Igual. En el original existían pero **no se llamaban**; ahora tienen paneles propios |
| Texto con `gfx->print` | Etiquetas LVGL (Montserrat) |
| NeoPixel (`updateLed`) | "LED virtual": círculo junto a BTC/USD, misma fórmula y suavizado |
| Badge L/S dentro del gráfico | Recuadro LONG/SHORT grande en la cabecera |
| `Arduino_GFX`, ST7789, pines SPI, `BL_PIN`, `LED_PIN`, `setCpuFrequencyMhz(80)` | Eliminados. ESP32_Display_Panel gestiona RGB, táctil y backlight (por CH422G). La CPU queda a 240 MHz porque el bus RGB lo necesita |

### Cambios de cálculo y por qué

1. **`double` para precios y SMA.** Un `float` tiene 24 bits de mantisa: a ~112.000 USD el
   paso mínimo es ~0,008 USD y la suma de 55 precios (~6·10⁶) redondea a 0,5 USD. Las
   fórmulas no cambian. El S3 calcula `double` por software, pero son pocos miles de
   operaciones por cuadro.
2. **`updateFlows()` ya no hace `chartDirty = false`.** En el original esa línea podía
   cancelar un redibujado completo pendiente (avance de bucket o cambio de escala) si coincidía
   en la misma vuelta de `loop()`. El gráfico quedaba sin desplazarse hasta el siguiente
   cambio de rango.
3. **`drawTailRegion()` corregida.** Con columnas de 4 px, `xStart = prevX + 8` quedaba a la
   derecha de `xEnd` y la función salía sin dibujar. La última columna solo se actualizaba en
   redibujados completos. Ahora se limpia y redibuja exactamente esa columna cada 120 ms.
4. **El río de flujo no se recalcula en cada trade.** `flowHistory` depende del reloj y de
   `flowSecondBuckets`, que solo cambian en `updateFlows()`. Antes eran hasta 14.400
   iteraciones por trade; ahora se hace cada 120 ms con el mismo resultado.
5. **Río alineado durante el arranque.** El original hacía `oldestSecond = 0` mientras el
   uptime era menor que la ventana, así que el río crecía desde la izquierda y no coincidía
   con el precio, que crece desde la derecha. Ahora los segundos negativos quedan vacíos.
6. **`waterfallRoadColorForTrend`** comparaba `ri < histCount` y promediaba columnas vacías
   (precio 0) con el historial incompleto. Ahora usa solo columnas válidas.
7. **H:/L: muestran el máximo y el mínimo reales** de la ventana. El original mostraba los
   bordes del gráfico, que incluyen el 8 % de margen.
8. **Ventanas táctiles.** En el original eran constantes. Al cambiar de ventana, los buckets
   se reconstruyen desde un historial de precio por segundo (5,6 h en PSRAM), así no se pierde
   lo recibido. Las columnas reconstruidas usan tiempo de reloj: un hueco sin trades queda
   vacío en vez de comprimirse. Desde ahí sigue la lógica original.

**Nota sobre la ventana original:** `CHART_WINDOW_MS = CHART_WINDOW_SECONDS * 120UL` daba
1.728.000 ms (**28:48**) para el precio, mientras el río usaba `CHART_WINDOW_SECONDS` =
**4 h**. No sé si era intencional, así que el botón **ORIG** reproduce exactamente eso y es
la ventana inicial. Los demás botones (1m, 5m, 15m, 1h, 4h) usan la misma ventana para precio
y flujo.

### Agregado nuevo (no estaba en el original)

- **Barra AGRESOR / COMPRA %:** volumen de Binance separado por lado agresor según el campo
  `m` del stream `@trade` (`m = true`: el comprador es maker, así que el agresor vendió).
  Usa el mismo suavizado EMA (α = 0,35) que el flujo. Coinbase no participa porque no verifiqué
  el significado exacto de `side` en su canal `ticker`.
- **Etiquetas de escala** con valores redondos a la derecha de los ticks originales.
- **Barra PESO:** participación de Binance (naranja) y Coinbase (azul) en la ponderación.

## Instalación (Arduino IDE)

### 1. Bibliotecas

Las que ya tenés: **ESP32_Display_Panel** (con sus dependencias **ESP32_IO_Expander** y
**esp-lib-utils**), **lvgl 8.3.11**, **WebSockets** (Markus Sattler, `WebSocketsClient`) y
**ArduinoJson 7.x**. No hacen falta Arduino_GFX ni Adafruit_NeoPixel.

### 2. Archivos de puerto (desde tu demo que funciona)

Copiá a la carpeta `PRRR_Waveshare7/` estos archivos de tu ejemplo **`09_lvgl_v8_demo`**:

- `lvgl_v8_port.h`
- `lvgl_v8_port.cpp`
- `esp_panel_board_supported_conf.h` y, si existen, `esp_panel_board_custom_conf.h`,
  `esp_panel_drivers_conf.h` y `esp_utils_conf.h`

No están en el repositorio a propósito. Son los que ya probaste con tu placa, así que traen
la configuración de pines, el anti-tearing y el bounce buffer que te funcionaron.

**Comprobación:** abrí el `.ino` de esa demo y buscá estas dos líneas:

```cpp
Board *board = new Board();
lvgl_port_init(board->getLCD(), board->getTouch());
```

Si las tiene, la API coincide con este proyecto (ESP32_Display_Panel 1.x). Si en cambio usa
`ESP_Panel *panel = new ESP_Panel();`, es la API 0.x: actualizá ESP32_Display_Panel o avisame
para adaptar `setup()`.

### 3. `lv_conf.h`

Usá el mismo `lv_conf.h` de la demo, en el mismo lugar donde lo tengas (normalmente
`Documentos/Arduino/libraries/lv_conf.h`). Revisá:

| Opción | Valor |
|---|---|
| `LV_COLOR_DEPTH` | `16` (si no, el sketch da error de compilación a propósito) |
| `LV_COLOR_16_SWAP` | `0` para RGB |
| `LV_USE_CANVAS` | `1` |
| `LV_FONT_MONTSERRAT_12/14/16/20/24/28/48` | `1` (si falta alguna se usa una más chica) |
| `LV_USE_PERF_MONITOR` | Recomendado `0`: si está en 1, el contador de FPS tapa la línea de estado. Útil activarlo un rato para medir |

### 4. WiFi

Copiá `wifi_secrets.example.h` como **`wifi_secrets.h`** y completá tus redes. Se prueban en
orden, igual que en el original. `wifi_secrets.h` está en `.gitignore`.

### 5. Menú Herramientas

| Opción | Valor |
|---|---|
| Placa | **Waveshare ESP32-S3-Touch-LCD-7** (en el core 3.3.12 su "PSRAM: Enabled" es OPI) |
| CPU Frequency | 240 MHz |
| Flash Size | 8MB (64Mb) |
| Partition Scheme | Huge APP (3MB No OTA/1MB SPIFFS) |
| PSRAM | Enabled |
| USB CDC On Boot | Como en la demo que te funcionó |
| Puerto | COM14 |

Si usás *ESP32S3 Dev Module*, elegí **PSRAM: OPI PSRAM**. Ahí "Enabled" significa QSPI y
no sirve para esta placa.

### 6. Compilar y cargar

Abrí `PRRR_Waveshare7/PRRR_Waveshare7.ino` → Verificar → Subir. Monitor serie a 115200.

## Primer arranque y verificación por etapas

En `prrr_config.h`:

```cpp
#define PRRR_ENABLE_SMA        1
#define PRRR_ENABLE_FLOW_BAND  1
#define PRRR_ENABLE_WATERFALL  1
```

Para ir paso a paso, poné los tres en `0` (solo precio en vivo y gráfico) y activalos de a uno.

Salida esperada por Serial:

```
[PRRR] Inicializando placa
[PRRR] Inicializando LVGL
[WiFi] Conectado a MiRed, IP: 192.168.1.191
[BIN] Conectado a: /ws/btcusdt@trade
[CB] Conectado a: /
[PRRR] precio=112345.67 BIN=on 112345.60 (18.0 msg/s) CB=on 112347.10 (4.2 msg/s) flujo=1.234 BTC/s cols=37 senal=1 heap=... psram=...
```

- **Etapa 1:** precio grande cambiando de color y puntos BIN/CB en verde. Con la ventana ORIG
  aparece una columna nueva cada ~13 s; con **1m**, cada 0,45 s.
- **Etapa 2:** las SMA aparecen cuando hay 25 y 55 columnas (con 1m, ~11 s y ~25 s). Después
  se muestra LONG/SHORT.
- **Etapa 3–4:** el río y el waterfall se mueven cada 240 ms.
- `heap`/`psram` del resumen tienen que mantenerse estables.

Colores de los puntos de exchange: verde = conectado y con datos frescos; amarillo =
conectado pero sin precio hace más de 15 s; rojo = desconectado.

### Si algo no anda

| Síntoma | Qué revisar |
|---|---|
| `Falta lvgl_v8_port.h` | Paso 2 |
| `Falta wifi_secrets.h` | Paso 4 |
| `PRRR requiere LV_COLOR_DEPTH 16` | Paso 3 |
| `PSRAM no detectada` | Herramientas → PSRAM |
| Imagen que se corre o parpadea al dibujar el waterfall | Ancho de banda de la PSRAM. Subí `flowDrawIntervalMs` (240 → 400) o desactivá el waterfall para confirmarlo. En `lvgl_v8_port.h`, `LVGL_PORT_AVOID_TEARING_MODE` según la guía del ejemplo oficial |
| Colores invertidos (rojo↔azul o raros) | `LV_COLOR_16_SWAP` debe ser 0 |
| El táctil no cambia la ventana | Probá primero el táctil con la demo. El Serial imprime `[PRRR] Ventana -> 5m` al tocar |

## Ajustes rápidos

- `FLOW_BAR_MAX_BTCS` (2,0): escala del río y del waterfall. Subilo si se saturan.
- `COLOR_SMA_FAST`: sigue siendo el azul puro de prueba del original. Los verdes alternativos
  están en el comentario.
- `WINDOW_PRESETS`: agregá o cambiá ventanas (máximo 4 h de flujo por `FLOW_SECONDS_SLOTS`).
- `CHART_COLUMN_W` (4) y `CHART_INNER_W` (532): 133 columnas visibles.
- No hay trading ni órdenes: solo visualización.
