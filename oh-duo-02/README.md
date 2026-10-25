# OH-DUO-02: detector de estancamiento (solo diagnóstico)

Lee los eventos que ya registra OpenHands en cada ejecución y produce un informe cronológico, con evidencias
y número de iteración, que distingue cuatro casos:

- **estancamiento**;
- **agotamiento de iteraciones**;
- **error real de infraestructura**;
- **finalización correcta**.

**Qué no hace:**

- No detiene ni corrige agentes.
- No modifica registros: escribe solo en `informes/` y se niega a escribir dentro de lo que analiza.
- No cambia prompts, límites ni modelos.
- No usa pruebas ocultas ni soluciones de referencia.
- No mira el nombre del modelo. Una prueba lo verifica: los mismos eventos con otro modelo dan el mismo
  veredicto.

Solo biblioteca estándar.

## Estructura real de los eventos (SDK 1.58.1), inspeccionada antes de escribir las reglas

- **Persistencia:** `estado/conversacion/<id>/events/event-NNNNN-<uuid>.json`, un evento por archivo, más
  `base_state.json` con `execution_status`. En la misma carpeta hay `.eventlog.lock` y
  `.eventlog-len-N.marker`, que **no** son eventos. El detector usa el mismo patrón de nombres que el SDK
  (`persistence_const.EVENT_NAME_RE`). En OH-DUO-01 estos eventos quedan en
  `ejecuciones/<P>__<modelo>/instancia/resultados/<corrida>/estado/conversacion/`.
- **`ActionEvent`:** `tool_name`, `tool_call_id`, `llm_response_id` (uno por paso del agente, que es lo que
  el SDK cuenta como iteración) y `action`:
  - `TerminalAction{command}`;
  - `FileEditorAction{command, path, old_str, new_str, file_text, view_range}`;
  - `FinishAction{message}`.
- **`ObservationEvent`:** se vincula a su acción por `tool_call_id`.
  - `TerminalObservation{exit_code, content}`.
  - `FileEditorObservation{is_error, old_content, new_content}`. Los dos últimos traen **el archivo completo
    antes y después** de cada edición aplicada.
  - Una edición rechazada llega con `is_error: true` y el motivo; por ejemplo «No replacement was performed.
    `new_str` and `old_str` must be different.».
- **`AgentErrorEvent`:** error de una herramienta (el agente puede seguir). No es infraestructura.
- **`ConversationErrorEvent{code, detail, classification}`:** el límite de iteraciones aparece como
  `code="MaxIterationsReached"`, `detail="Agent reached maximum iterations limit (30)."`,
  `classification.kind="agent_action"`, y deja `execution_status="error"`.
  - **Por qué OH-DUO-01 se equivocó:** su `estado_ejecucion` ve `estado_final == "error"` y lo trata como
    error de infraestructura, sin mirar el código del evento.
- **El SDK no protege los archivos de prueba:** en la reproducción, la edición de `test_intervalos.py` se
  aplicó. La regla de «no modificarlos» está solo en el pedido.
- **El detector propio del SDK** (`stuck_detector.py`) solo reconoce patrones **consecutivos**:
  - 4 pares acción-observación iguales seguidos;
  - 3 errores seguidos de la misma acción;
  - alternancias de 6 eventos.

  Un ciclo de acciones repetidas con otras acciones en el medio no lo activa y llega al límite de
  iteraciones. Este detector mira la ejecución completa.

## Reglas y por qué

El estado del código se sigue paso a paso, sin leer archivos del disco:

- **Punto de partida:** la `tarea/` original de la instancia, que es lo que recibió el agente.
- **Ediciones del editor:** cada edición aplicada actualiza la huella con `new_content`.
- **Escrituras por terminal:** un comando de terminal que puede escribir (`sed -i`, `>`, `tee`, `mv`, `cp`,
  `rm`, …) cuenta, por precaución, como cambio. Así no se marca «sin cambio» algo que quizás sí cambió.

| Regla | Detecta | Justificación |
|---|---|---|
| **A1** repetición equivalente sin cambio | La misma acción canónica sobre el **mismo estado del código**, ≥ `repeticion_equivalente_sin_cambio` veces, en cualquier punto de la ejecución | Repetir una acción sin que nada cambie no puede dar información nueva. Correr las pruebas después de cada edición efectiva **no** cuenta, porque el estado es otro. Con `equivalencia_pruebas`, `python -m unittest`, `python3 -m unittest -v` y `pytest -q` son la misma acción |
| **A2** fallos iguales sin cambio | Las **mismas** pruebas fallando ≥ `fallos_iguales_sin_cambio` veces con el mismo código productivo | Es el ciclo «probar, no arreglar, probar». La firma son los nombres de `FAIL:`/`ERROR:` de la salida de unittest |
| **A3** edición de archivo protegido | Escrituras del editor sobre `patrones_protegidos` (por omisión `test_*.py`, más `test_<módulo>.py` de la misión) o comandos de terminal que podrían escribirlos | El pedido prohíbe modificarlos y el SDK no lo impide. Se informa si la edición fue efectiva, rechazada o solo posible |
| **A4** ediciones rechazadas o sin efecto | `is_error`, `AgentErrorEvent` o `old_content == new_content`, ≥ `ediciones_rechazadas_o_sin_efecto` | Ediciones que no cambian nada: el agente cree avanzar y no avanza |
| **A5** pruebas que aprueban solo tras editar lo protegido | Pruebas que pasan **después** de una edición efectiva de un archivo protegido, con el código productivo igual al original | El agente adaptó la prueba al error, en lugar de lo contrario |
| **A6** cola sin progreso | ≥ `iteraciones_finales_sin_progreso` iteraciones finales sin cambio efectivo del código productivo (solo si no terminó con `finish`) | Cuánto del presupuesto se gastó sin avanzar |

**Terminación**, según el último evento terminal:

| Evento | Terminación |
|---|---|
| `ConversationErrorEvent` `MaxIterationsReached` | `agotamiento_de_iteraciones` |
| `ConversationErrorEvent` `MaxBudgetReached` | `limite_de_presupuesto` |
| `ConversationErrorEvent` con otro código | `error_de_infraestructura` |
| `ConversationErrorEvent` **sin código** | `indeterminado`. Si las iteraciones igualan `limite_de_iteraciones`: `agotamiento_de_iteraciones_probable` |
| `finish` | `finalizacion_con_finish` |
| `PauseEvent` | `corte_por_tiempo` |
| `execution_status=stuck` | `detenido_por_el_detector_del_sdk` |
| Mensaje sin herramienta | `termino_con_texto_sin_herramienta` |
| Ninguno | `indeterminado` |

**Veredicto:**

- `finish` → no hay estancamiento. Las alertas quedan como «observaciones».
- Error real → no hay estancamiento, salvo que además haya patrones; en ese caso es «indeterminado».
- Cualquier otro final con A1, A2, A4, A5, A6, o A3 efectiva → **estancamiento**. La confianza es alta con
  dos o más patrones y agotamiento.
- Sin eventos → indeterminado, con el motivo.

Además se sugiere el `estado_ejecucion` correcto para OH-DUO-01 (por ejemplo, agotamiento → `incompleta`),
sin modificarlo.

**Fidelidad y datos faltantes:** se informan expresamente:

- huecos en la numeración de eventos;
- archivos ilegibles;
- acciones sin observación;
- falta del código original;
- más de una conversación;
- uso del resumen de `agente.py`, cuyas observaciones están recortadas a 4000 caracteres y sin el código del
  `ConversationErrorEvent`.

**No se analiza** el texto de los pensamientos: depende del modelo y no es verificable. «Interpretó mal el
fallo» no se puede detectar con estas reglas; se ve en la cronología.

## Uso

```bash
cd ~/oh-duo-02
python3 -m unittest discover -s tests -v
python3 estancamiento.py --corrida ~/oh-duo-01/corridas/ensayo-p1                 # → informes/ensayo-p1/
python3 estancamiento.py --corrida ~/oh-duo-01/corridas/ensayo-p1 --umbrales mis_umbrales.json
python3 estancamiento.py --ejecucion ~/oh-duo-01/corridas/ensayo-p1/ejecuciones/P1__qwen
```

`umbrales.json` tiene los valores por omisión. Una clave desconocida se rechaza.

**Salidas:**

- `informes/<corrida>/estancamiento.json`: por ejecución, la fuente, las iteraciones, la terminación con su
  evidencia, el veredicto, el estado sugerido, las alertas con iteraciones y eventos, las notas y la
  cronología completa (iteración, número de evento, acción canónica, resultado, huella del código productivo
  y marcas).
- `estancamiento.md`: lo mismo, legible.

## Validación

**Los registros reales de `ensayo-p1` no están en el repositorio** (quedaron en la VivoBook). Mientras tanto,
el caso de referencia es `tests/fixtures/repro-p1`: eventos **reales** del SDK 1.58.1, generados con el sandbox
y el ejecutor de OH-DUO-01, con un Ollama simulado que reproduce lo descrito.

- **Qwen:** pruebas repetidas, edición de la prueba protegida (efectiva, y después rechazada), ediciones
  sin efecto y 30 iteraciones.
- **Gemma:** éxito en 6 llamadas.

En esa reproducción, OH-DUO-01 clasificó a Qwen como `error_infraestructura (estado_error_del_sdk)`, igual que
en la corrida real. Resultado del detector (`informes/repro-p1/`):

| Ejecución | Terminación | Estancamiento | Patrones | Estado registrado → sugerido |
|---|---|---|---|---|
| P1__qwen | `agotamiento_de_iteraciones` (MaxIterationsReached) | **sí**, alta | A1, A3, A4, A5, A6 | `error_infraestructura` → `incompleta` |
| P1__gemma | `finalizacion_con_finish` | no | ninguna alerta | `finalizada` → `finalizada` |

**Para validar con los registros reales:**

```bash
python3 estancamiento.py --corrida ~/oh-duo-01/corridas/ensayo-p1
```

Pasame `informes/ensayo-p1/`. Si la carpeta `instancia/` de las ejecuciones no existe, el detector usa el
resumen de `logs/` y lo indica.

**Pruebas (21):** 8 sobre la referencia (veredictos, evidencia con iteración, umbrales, solo lectura, fuente
alternativa, independencia del modelo) y 13 sintéticas:

- repeticiones legítimas;
- éxito con repeticiones (sin estancamiento);
- fallos iguales;
- comandos equivalentes, con y sin equivalencia;
- error de herramienta;
- error real de infraestructura;
- eventos faltantes;
- ejecución sin eventos;
- edición protegida por terminal;
- edición sin efecto;
- detector del SDK y pausa;
- texto sin herramienta;
- umbrales desconocidos.
