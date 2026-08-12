# Cavernicola v2 — Flow de Node-RED

Tablero de compras (plan por familias, bandas de stock, mix mensual) para Node-RED
con dashboard (`node-red-dashboard`).

## Qué hay acá

```
cavernicola/
├─ flow.json        <- ARCHIVO FINAL. Este es el que se importa en Node-RED.
├─ build.js         <- Arma flow.json a partir de src/ (escapado automatico y seguro).
├─ src/             <- Cuerpos de codigo/HTML en texto plano, faciles de editar:
│   ├─ 35280-criterio-simple.js      (nodo activo: calcula el tablero)
│   ├─ a778-estructurar-arbol.js     ("function 8": arma el arbol rama/subrama/material)
│   ├─ b6a7-tablero.html             (ui_template principal "aca mostramos")
│   ├─ 1256-plan-sugerido.html       (ui_template "plan sugerido 200 tn")
│   ├─ 5905-criterio-simple.js       (nodo LEGACY deshabilitado; stub documentado)
│   ├─ ecodp_wrap_plan_minipc.js
│   ├─ ecodp_wrap_json_compras_v2.js
│   └─ ecodp_preparar_json_compras_v2.js
└─ README.md
```

## Cómo editar "cositas"

1. Se edita el archivo correspondiente en `src/` (texto plano, sin escapar).
   - Cambiar mínimos/máximos por material -> `BANDAS_MATERIAL_BASE` en `35280-criterio-simple.js`.
   - Cambiar objetivos del plan -> `defaultCriteria().planFamilias` en el mismo archivo.
   - Cambiar textos/colores del tablero -> `b6a7-tablero.html`.
2. Se regenera el flow:
   ```
   node build.js
   ```
3. Se importa `flow.json` en Node-RED (Menu -> Import -> pegar/seleccionar el archivo).

## Notas

- El nodo `5905` es una version vieja, **deshabilitada** (`d:true`, sin cablear).
  El tablero real corre en el nodo `35280`. El `5905` se dejó como stub para no
  arrastrar codigo muerto; no afecta el funcionamiento.
- Rutas de datos que usa el flow en la maquina destino:
  `/home/gangleo81/planComprasV2.json`, `comprasRealesIA.json`,
  `stockIntermediosIA.json`, `agendaComprasIA.json`.
- Los watchdog hacen POST a `http://100.73.116.111:8080/api/ia/ingest/plan_compras`.
