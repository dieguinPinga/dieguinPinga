# CONCEPTITO LAB

Laboratorio externo y experimental. **No toca Node-RED ni el Function live**: solo lee MariaDB (`prrr_market`).

Pregunta que busca responder: *¿qué diferencia estadísticamente a un buen cruce SMA35/70 de uno malo?*

## Instalación y ejecución

```bash
mkdir -p ~/conceptito && cp conceptito_lab.py requirements.txt ~/conceptito/
cd ~/conceptito
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python3 conceptito_lab.py
```

Opciones útiles:

| Opción | Qué hace |
|---|---|
| `--split-ts "2026-10-05 00:00:00"` | Fija el corte temprano/tardío (por defecto: mediana de las señales). Sirve para "lo nuevo = fuera de muestra". |
| `--avail recv` | Modo más estricto: una quote solo se usa si además ya fue recibida (`ts <= T`). |
| `--mkt-lag 0` | Usa el bucket `market_1s` con `ts = T` en el instante T (ver nota 5). |
| `--gmx-source X` | Filtra `gmx_price.source` si hay más de una. |
| `--since` / `--until` | Limita las señales analizadas (fechas UTC). |
| `--gmx-csv` / `--mkt-csv` | Lee desde CSV en vez de la base. |

Cada corrida vuelve a leer todo el histórico, así que al acumular días nuevos alcanza con volver a ejecutar.

## Salidas

| Archivo | Contenido |
|---|---|
| `conceptito_signals_dataset.csv` | Una fila por cruce que pasa CD32: features causales y resultados futuros |
| `conceptito_bands.csv` | Quintiles de cada feature: n, % TP25 antes de SL75, MFE/MAE, bruto y neto, separados temprano/tardío |
| `conceptito_feature_ranking.csv` | Spearman contra el neto, IC por bootstrap de bloques de 1 h, estabilidad entre mitades y veredicto |
| `conceptito_report.txt` | Copia de todo lo que se imprime en pantalla |

### Convenciones

- Las columnas `*_dir*` están orientadas según la señal: **positivo = a favor de la señal**. Las features crudas se mantienen en el CSV.
- `tpA_slB` vale `1` si el TP llegó primero, `-1` si llegó primero el SL y `0` si no llegó ninguno en 100 m.
- `t_tpX` / `t_slX` son los segundos desde la entrada hasta tocar la barrera. Quedan vacíos (NaN) si no la toca.
- `sim_tp25_sl75_to100_*` es la simulación independiente principal. `sim_tp7_sl75_to26_*` replica la configuración del live como referencia.
- `*_trigger_pnl` es el PnL real en el segundo en que se disparó el TP o el SL.
- `mfe_*` / `mae_*` son crudos: pueden ser negativos o positivos, a diferencia del live, que arranca en 0.
- Las señales cuyo horizonte no llegó a completarse antes del final de los datos se marcan con `complete_Hm = 0` y no entran en el análisis de ese horizonte.

## Semántica (igual que el live)

- **Reloj:** `source_ts`. Para cada boundary T se usa la última quote con `source_ts <= T`. Si tiene más de 10 s, el segundo es stale.
- **Stale:** un segundo stale reinicia el signo y hace que las SMA vuelvan a calentar. El reloj del cooldown no se reinicia.
- **Cooldown:** todo cruce reinicia el reloj de 32 s.
- **Entrada:** la señal ocurre en N y la entrada exactamente en N+1, que tiene que ser válido.
- **Precios:** LONG entra a `max` y sale a `min`; SHORT al revés.
- **Timeout:** en el primer segundo válido con tiempo transcurrido ≥ H.
- **Barreras:** TP y SL realizan exactamente el nivel, con prioridad TP > SL > TIMEOUT.
- **Conversión de tiempos:** `to_ms()` convierte cualquier `datetime64` (ns/us/ms) restando la época y dividiendo por un `Timedelta`. Aborta si algún año cae fuera de 2020–2035, para evitar el bug de 2026 → 1970.
- **Causalidad verificada:** se recortan los datos en un instante X y las features de todas las señales anteriores a X quedan idénticas.

## Problemas metodológicos (leer antes de interpretar)

1. **Las señales no son independientes.** Con un cruce por minuto y horizontes de 100 m, cientos de operaciones se superponen y comparten el mismo movimiento de precio. 1.500 señales en 36 h equivalen a muchos menos "experimentos" reales. Por eso los IC se calculan con bootstrap de **bloques de 1 hora**. Aun así, ~36 bloques es poco.
2. **El SL "exacto" es optimista.** Si el precio salta de −70 a −90 en un segundo, la operación queda registrada en −75. El reporte muestra el PnL real en el disparo (`trigger_pnl`). El TP es conservador: se recorta en +25.
3. **`source_ts` como momento de disponibilidad.** Usar `source_ts` supone conocer la quote antes de recibirla; el retraso real es `ts − source_ts`, que el reporte imprime. El live hace lo mismo, así que el modo por defecto lo replica. `--avail recv` muestra si ese pequeño adelanto cambia los resultados. Requiere que `ts` esté en UTC.
4. **El lab es una versión "ideal" del live.** Usa todas las quotes. El live lee solo la última quote por tick e ignora las que llegan desordenadas.
5. **Semántica de `market_1s.ts`.** Si `ts` marca el *inicio* del segundo, el bucket `ts = T` todavía no terminó en el instante T. Por eso, por defecto, en T solo se usan buckets con `ts <= T − 1s` (`--mkt-lag 1`). Además, el reloj de market (exchanges) no es el de GMX. El reporte incluye un **chequeo de relojes**: desfasaje en horas por nivel de precio (detecta zonas horarias mal guardadas) y lag en segundos por correlación de retornos de 1 s.
6. **Costos no modelados.** Solo se descuentan los $3 de fees. GMX además cobra borrow, funding y price impact, y con 100 m de tenencia no son despreciables.
7. **Comparaciones múltiples.** Se prueban unas 44 features, así que 2 o 3 pueden aparecer como "significativas" por azar. Una **CANDIDATA** es una hipótesis para confirmar con días nuevos (`--split-ts`), no un hallazgo.
8. **Bandas descriptivas.** Los cortes de quintil se calculan sobre toda la muestra; sirven para describir, no son una regla operativa. El veredicto se apoya en la estabilidad entre mitades y en el IC por bloques.
9. **Régimen y lado.** Con pocas horas de datos, una tendencia sostenida hace que LONG o SHORT dominen el resultado. Antes de creerle a una feature, conviene revisar la sección "Por lado".
10. **`vol_usd_*` absoluto no es estacionario** entre días. Para comparar en el tiempo es mejor usar `relvol_*`.

## Veredictos del ranking

| Veredicto | Criterio |
|---|---|
| `INSUFICIENTE` | Algún quintil tiene menos de 30 señales, o alguna mitad tiene menos de 15 |
| `SIN EFECTO` | \|rho\| < 0,03 |
| `INCONSISTENTE` | El signo de rho cambia entre el bloque temprano y el tardío |
| `CANDIDATA` | Mismo signo en ambas mitades, \|rho\| ≥ 0,04 en cada una, IC por bloques que excluye 0 y bandas monótonas (\|monot\| ≥ 0,7) |
| `DÉBIL` | Todo lo demás |
