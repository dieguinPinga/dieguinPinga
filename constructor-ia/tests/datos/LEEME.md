# Datos de pruebas

- `llamadas-X07-v12.jsonl` (pendiente): el `llamadas.jsonl` de la corrida de X07 con el ciclo v1.2 en la VivoBook
  (commit `b954b2f`), con las tres respuestas reales de Qwen y los prompts que recibió. Tiene solo datos sintéticos.
  Si se copia acá, `tests/test_ciclo.py::RealX07V12Tests` comprueba con el texto exacto que el formato v1.2 (sin
  DIAGNOSTICO) no se aplica en v1.3. Mientras falte, esa prueba queda omitida, y las regresiones usan la línea 12
  textual y una reconstrucción de las líneas 17 y 15.
