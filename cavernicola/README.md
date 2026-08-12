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

## Modelo de datos (entendido desde el export real 2026-07)

### Flujo de punta a punta
1. **Entradas (4 JSON en disco)** que leen los botones "Recargar":
   - `planComprasV2.json` -> **criterio/config** (la fuente de verdad editable).
   - `comprasRealesIA.json` -> compras del mes.
   - `stockIntermediosIA.json` -> foto actual de piletas (stock).
   - `agendaComprasIA.json` -> agenda.
2. Nodo **35280** combina todo en `buildDashboard` -> nodo **a778** arma el `arbol`
   (rama -> subrama -> material) -> los 2 `ui_template` lo dibujan.
3. Cada edicion en la UI manda una `accion` al 35280, que actualiza el criterio y
   lo **persiste** en `planComprasV2.json` (salida 2 -> nodo file "Guardar").

### De donde salen los minimos/maximos (y los botoncitos +/-)
- Semilla en el codigo: `BANDAS_MATERIAL_BASE` (dentro del 35280), solo si no hay config.
- **Fuente viva:** `planComprasV2.json`:
  - `criterio.bandasMaterial` -> min/max por material.
  - `criterio.bandasGrupo`    -> bandas "sumar grupo".
- La UI edita con las acciones:
  - `setBandaMaterial` (min/max de un material)  -> se guarda en el JSON.
  - `setBandaGrupo`   (checkbox "sumar grupo")    -> se guarda en el JSON.

### Banda de grupo ("sumar grupo")
Cuando esta activa, el min/max se evalua sobre la **suma** de los materiales del grupo;
los materiales individuales quedan min=0/max=0 y su estado pasa a "BANDA GRUPO".
Ej 2026-07: STRETCH CARAMELO (min 30 / max 50, incluye STRECH CARAMELO + STRECH TUTTY,
stock 13.2, falta 16.8).

### Quirk a decidir: max = 0
Un material con **max 0** siempre muestra "SOBRA" (exceso = todo el stock) aunque este
por debajo del minimo y sume faltante. Ej: ADI AMARILLO (stock 9.5, min 20 -> falta 10.5
y a la vez SOBRA 9.5). El faltante de la subrama SI lo cuenta
(PEAD INYECCION: min 190 - stock 108.2 = 81.8 tn). Se puede cambiar el criterio para que
max=0 signifique "sin tope" (no marcar sobra) si asi se prefiere.

### Importante para "tenerlo como base"
El export `compras_v4_excel_*.json` (guardado en `samples/`) es el **modelo de lectura**
(arbol, planos, plan, auditorias) y NO incluye `criterio`. Para reconstruir la config viva
1:1 (asignaciones material->rama/subrama, bandasGrupo exactas, metasPorMes) hace falta el
archivo **`planComprasV2.json`**. Ese es el verdadero "base".
