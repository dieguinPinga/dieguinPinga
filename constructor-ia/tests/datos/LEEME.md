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
