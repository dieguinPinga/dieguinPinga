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
