# Datos de pruebas

## `llamadas-X07-v12.jsonl`

Es el `llamadas.jsonl` **real** de la corrida de X07 con el ciclo v1.2 en la VivoBook (commit `b954b2f`): las tres
respuestas de Qwen, los prompts por partes, semillas, tokens, tiempos y memoria. Tiene solo datos sintéticos. Lo
usan las pruebas `tests/test_ciclo.py::RealX07V12Tests` y las constantes `X07_V12_*` de ese archivo.

| llamada | línea marcada | código de Qwen (textual) |
|---|---|---|
| 1 | 17 (`validas += 1`) | `cobertura["leidas"] += 1` |
| 2 | 15 (`invalidas += 1`) | `cobertura["leidas"] += 1` |
| 3 | 12 (`leidas = 1`) | `cobertura["leidas"] = r.filas[0][0] is not None and len(r.filas) > 0` |

## `llamadas-X07-v13.jsonl`

Es el `llamadas.jsonl` **real** de la corrida de X07 con el ciclo v1.3 en la VivoBook. Lo usan
`tests/test_ciclo.py::RealX07V13Tests` y `CycleTests.test_replaying_the_real_v13_answers_under_v134`.

| llamada | sugerida | respuesta (resumen) |
|---|---|---|
| 1 | 17 | `DIAGNOSTICO: 17` (`validas = 1`) y después `DIAGNOSTICO: 12` (`leidas = len(r.filas)`); cortada en 768 tokens |
| 2 | 15 | `DIAGNOSTICO: 15`, `cobertura["invalidas"] = 1643` (un número copiado de la evidencia) |
| 3 | 12 | `DIAGNOSTICO: 12`, `cobertura["leidas"] = r.filas.__len__()` |

## `llamadas-X07-v133.jsonl`

Es el `llamadas.jsonl` **real** de la corrida de X07 con el ciclo v1.3.3 en la VivoBook (3 llamadas; abstención, 0
confirmaciones falsas). Tiene solo datos sintéticos. Lo usa `CycleTests.test_replaying_the_real_v133_answers_under_v134`
como regresión de las decisiones del coordinador v1.3.4 (no predice cómo respondería Qwen al prompt nuevo).

| llamada | sugerida | respuesta (resumen) |
|---|---|---|
| 1 | 17 | `DIAGNOSTICO: 17` (`validas = 1`) y, en la «CORRECCIÓN FINAL», `DIAGNOSTICO: 12` (`leidas = r.filas.__len__()`) |
| 2 | 17 | `DIAGNOSTICO: 17`, `cobertura["validas"] = 14803` (un número copiado de la evidencia) |
| 3 | 15 | `DIAGNOSTICO: 15`, `cobertura["invalidas"] = 1643` (un número copiado de la evidencia) |

## `llamadas-M1-fase2.jsonl`

Es el `llamadas.jsonl` **real** de la primera misión M1 del agente de datos con Qwen3 4B en la VivoBook (fase 2;
carpeta `agente-fase2`): 5 llamadas (M1×2, M2, M3×2), con prompts por partes, respuestas crudas, semillas, tokens,
tiempos y memoria. Tiene solo datos sintéticos (base B1). Lo usa `tests/test_agente_requisitos.py` como regresión.

| llamada | respuesta (resumen) |
|---|---|
| M1 (2) | fichas correctas de las 6 tablas |
| M2 | `TABLAS: lineas_pedido, productos` (omite `pedidos`, la única con la fecha de las ventas) |
| M3 (1.ª) | `MEDIR: suma` / `AGRUPAR: producto_id` (sin columna ni tabla) |
| M3 (2.ª) | `MEDIR: suma lineas_pedido.cantidad` / `AGRUPAR: lineas_pedido.producto_id` (sin FILTRO, ORDEN ni LIMITE) |
