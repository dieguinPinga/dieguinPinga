# WAVE_TEST_V1: ¿tiene WAVE poder predictivo?

Esto es un test de medición, no una estrategia. No envía órdenes y trabaja todo en RAM: no usa MySQL, disco, context store ni dashboard.

## Archivos

| Archivo | Qué es |
|---|---|
| `WAVE_TEST_V1.json` | Tab nuevo y aislado. No contiene ningún nodo de `WAVE_RAW_V1` y ningún ID coincide con los de ese flow. |
| `WAVE_TAP_link_out.json` | Un único `link out` llamado **TAP → WAVE_TEST**, sin tab asignado y ya enlazado con `WAVE TAP IN`. |

## Instalación

1. **Importar el tab.** *Menú → Import → `WAVE_TEST_V1.json`*. Crea el tab `WAVE_TEST_V1`.
2. **Importar el tap.** Abrí el tab **WAVE_RAW_V1** y elegí *Import → `WAVE_TAP_link_out.json` → Import to: **current flow***. Colocá el nodo cerca de los NORMALIZER.
   - Alternativa manual: creá un `link out`, llamalo `TAP → WAVE_TEST`, elegí el modo *Send to all connected link nodes* y marcá `WAVE TAP IN` (tab `WAVE_TEST_V1`).
   - Si al abrir el tap no aparece marcado `WAVE TAP IN`, marcalo a mano.
3. **Cablear a mano 4 cables.** Cada cable va desde la **salida 1** (la de arriba, "eventos -> engine") de un NORMALIZER hacia `TAP → WAVE_TEST`:
   - `NORMALIZER · BINANCE`
   - `NORMALIZER · COINBASE`
   - `NORMALIZER · KRAKEN`
   - `NORMALIZER · OKX`

   **No** uses la salida 2 (resync -> adapter). Los cables que ya van hacia `WAVE RAW ENGINE` quedan como están.
4. **Deploy.**

## Medición

- **Muestreo:** un punto fijo cada 250 ms, con el reloj local.
- **Dirección:** el signo de `ALL.delta_usd_250ms`, el flujo agresor BUY−SELL consolidado de los 4 exchanges.
- **Fuerza:** `|delta_usd_250ms|`, en USD.
- **Precio principal:** el mid de Binance.
- **Control:** la mediana de los retornos de mid de los exchanges disponibles.
- **Horizontes:** +100, +250, +500, +1000, +2000 y +5000 ms.
- **Sin look-ahead:** cada valor "as-of" usa solo eventos con `t_recv ≤ t0` para la muestra y `t_recv ≤ t0+h` para el precio futuro. Lo verifica `test/unit_lookahead.js`.
- **Ventanas:** el código se copia literalmente del `WAVE RAW ENGINE` al hacer el build, y `validate.js` lo comprueba.

Los terciles de fuerza son **provisionales**, porque sus límites cambian durante la corrida. Sirven solo como diagnóstico. La fuerza bruta se guarda siempre.

`baseline_random_direction` es solo un control de cordura (sanity check). No sirve para medir significancia.

## Pruebas

```
node build.js && node validate.js
node test/unit_lookahead.js
node test/unit_reorder.js                       # stream desordenado == mismo stream ordenado; late_beyond_buffer
node test/run_e2e.js <dir-node-red> 75          # mercado simulado sin señal: debe dar ≈ 0 bps
node test/run_e2e.js <dir-node-red> 75 plant    # señal plantada (el mid de Binance sigue al flujo con 100 ms de retraso)
node test/run_e2e.js <dir-node-red> 40 notap    # referencia de carga del engine sin el tap
```

## Export de reportes a disco

`WAVE TEST EXPORT` escribe un JSON completo en `/home/plapopepo/wave_reports`. Si el directorio no existe, lo crea. Para usar otro directorio, definí la variable de entorno `WAVE_REPORT_DIR`.

Archivos que genera:

- `WAVE_TEST_LATEST.json`: se sobreescribe en cada export, tanto en el automático (inject **AUTO EXPORT**, cada 5 min) como en el manual.
- `WAVE_TEST_YYYY-MM-DD_HH-mm-ss.json`: solo con el inject **EXPORT REPORT**. Se conservan los últimos 100.

Contenido del reporte: `generated_at`, `uptime_s`, `config`, `counts`, `queue`, `strength`, `primary_binance_mid`, `secondary_median_mid`, `baseline_random_direction`, `samples_meta` y `samples`, con las últimas 500 muestras completas (`pre_move` y `future`).

Peso medido: ~2.8 KB por muestra, así que con 500 muestras el reporte ocupa ~1.4 MB. El histórico ocupa como máximo ~140 MB.

Cómo escribe:

- La escritura es atómica: primero `.tmp`, después `fsync` y por último `rename`.
- Es asíncrona y va por bloques de 50 muestras, así que no bloquea la llegada de eventos.

El código de `WAVE TEST EVAL` no cambia. El export usa sus salidas existentes:

1. espera el próximo detalle, que sale cada ≤5 s;
2. pide un DUMP de muestras;
3. escribe el archivo.

## WAVE LAB (dashboard)

La página se abre en `http://<host>:1880/wave-lab`. Es oscura, horizontal y se sirve con nodos core (`http in`, `template`, `http response`), así que no requiere Dashboard 2.0 ni paquetes.

- **Datos:** consume solo el detalle de `WAVE TEST EVAL` (cada 5 s). Mientras la página está abierta, pide además un DUMP cada 10 s para la sección "¿llegamos tarde?". Con la página cerrada no agrega ninguna carga.
- **Endpoints:**

  | Ruta | Qué hace |
  |---|---|
  | `GET /wave-lab` | Sirve la página. |
  | `GET /wave-lab/data` | Devuelve el estado en JSON, desde memoria. |
  | `POST /wave-lab/reset` | Envía el mismo `{topic:'reset'}` que el inject RESET TEST. |
  | `GET /wave-lab/export` | Descarga un JSON con las últimas 500 muestras. No escribe a disco. |

- **Rutas:** cuelgan de `httpNodeRoot`, que por defecto es `/`. Si configuraste `httpNodeAuth`, también se aplica aquí.
- **Lo que no cambió:** `WAVE TEST EVAL` no se modificó. En `WAVE TEST EXPORT` solo cambió el ruteo de los DUMP: las muestras van al LAB y el Debug muestra solo los DUMP pedidos a mano.

## Reorder buffer (orden estricto por `t_recv`)

`WAVE TEST EVAL` no aplica los mensajes en el orden en que llegan al tap. Primero los acumula en un buffer en RAM ordenado por `(t_recv, orden de llegada)` y recién los entrega a `advance()` y `apply()` cuando `t_recv <= watermark`, con `watermark = now − 250 ms`.

- Un timer de 50 ms mueve el watermark aunque el feed se detenga.
- Un mensaje que llega con `t_recv <= watermark`, es decir, después de que ese instante ya se procesó, no se inserta en ventanas pasadas. Se cuenta en `late_beyond_buffer` y se descarta solo del TEST.

La telemetría aparece en `queue` (detalle y export):

| Campo | Qué mide |
|---|---|
| `reorder_buffer_current` / `reorder_buffer_max` | Mensajes en el buffer ahora y el máximo visto. |
| `late_beyond_buffer` | Mensajes descartados por llegar después del watermark. |
| `out_of_order_events` | Mensajes que llegaron fuera de orden. |
| `lateness_ms` (p50/p95/p99/max) | Cuánto llega cada mensaje detrás del `t_recv` más nuevo ya visto. |
| `arrival_delay_ms` (p50/p95/p99/max) | `now − t_recv` al llegar al tap. |
| `watermark_lag_ms` | Distancia entre el reloj y el watermark. |

`sample()`, `resolve()`, `advance()`, `apply()`, las ventanas y las constantes de muestreo y horizontes son textualmente idénticas a la versión anterior; `validate.js` lo verifica. `test/unit_reorder.js` comprueba que un stream de ~83.000 mensajes, con ~74.500 llegados fuera de orden (hasta 200 ms), produce muestras, estadísticas y baseline idénticos al mismo stream ordenado.
