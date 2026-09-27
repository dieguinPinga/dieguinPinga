# Node-RED · BTC + ZEC

Requiere `@flowfuse/node-red-dashboard`.

| Archivo | Uso |
|---|---|
| `btc-zec-live.flow.json` | Flow completo: solapas **LIVE** y **FLOW**. Importar en una instancia vacía. |
| `btc-zec-flow.tab.json` | Sólo la solapa **FLOW**. Importar en una instancia que ya tiene LIVE desplegada (reutiliza sus nodos de configuración). |
| `src/flow/accumulator.js` | Código del nodo Function "FLOW · Acumulador agresivo 1 s". |
| `src/flow/template.vue` | Plantilla del dashboard FLOW (se genera una por activo). |
| `build-flow-tab.js` | Regenera los dos JSON desde `src/flow/`. No modifica los nodos de LIVE. |
| `test/accumulator.test.js` | Prueba del acumulador: clasificación, conservación de trades, trades tardíos, 500 trades/s. |

```
node node-red/build-flow-tab.js
node node-red/test/accumulator.test.js
```

Dashboard: `/btc-zec-live/live` y `/btc-zec-live/flow`.
