# OH-DUO-03: supervisor en tiempo real (solo observa)

Observa a los agentes programadores **mientras trabajan**, aplica las reglas de OH-DUO-02 a cada evento nuevo y
registra cuándo aparece cada alerta y con qué evidencia.

- **Qué no hace:** no detiene, no reinicia y no le envía nada al agente; no cambia código, prompts, modelos ni
  límites.
- **Qué no lee:** pruebas ocultas, referencias ni `verificacion/`.
- **Qué no escribe:** nada dentro de lo que observa.
- **Qué no modifica:** OH-DUO-01 ni OH-DUO-02.

Solo biblioteca estándar.

## Interfaces del SDK 1.58.1 y estrategia elegida

| Opción | Cómo | Decisión |
|---|---|---|
| `Conversation(callbacks=[…])` | Una función que el SDK llama con cada evento, en el mismo proceso | **Descartada.** Obliga a modificar `agente.py` (OH-DUO-01) y pone al observador dentro del sandbox del agente, mezclando componentes |
| Agent-server / `RemoteConversation` (websocket) | Eventos por la red | **Descartada.** Cambia la arquitectura del programador |
| `agente.log` | Texto del registro | **Descartada.** No tiene estructura ni todos los datos |
| **Persistencia de eventos** | `EventLog.append` escribe **cada** evento en cuanto ocurre, en `estado/conversacion/<id>/events/event-NNNNN-<uuid>.json`, de forma **atómica** (`atomic_write_text`: temporal oculto, `fsync`, `os.replace`) | **Elegida.** Se lee desde fuera, en solo lectura, sin tocar al programador y con el mismo formato que analiza OH-DUO-02 |

Por qué esta estrategia es segura y suficiente:

- **Independiente:** el observador corre en el anfitrión, fuera del sandbox, y no depende del modelo, del
  prompt ni de dónde se hace la inferencia. Solo necesita ver la carpeta `estado/` de la ejecución, que
  OH-DUO-01 ya monta en `instancia/resultados/<corrida>/estado/`.
- **Sin archivos a medio escribir:** la escritura atómica lo garantiza. Igual, un archivo ilegible se
  reintenta y se informa.
- **En orden:** si el evento 7 aparece antes que el 6, se espera al 6. Los huecos que nunca se llenan se
  informan al cerrar.
- **Latencia:** el intervalo de sondeo (por omisión 0,5 s). Es despreciable frente a una iteración de un LLM
  local, que dura segundos.

## Arquitectura

```
Programador (OpenHands en bwrap) ──escribe──► estado/conversacion/<id>/events/event-*.json
                                                      │ solo lectura, sondeo
Observador  FuenteEventos ◄──────────────────────────┘   (orden por índice, sin huecos, reintentos)
    │ eventos nuevos
Detector    oh-duo-02/estancamiento.py → analizar(prefijo)      (reglas A1–A6, sin copiarlas)
    │ alertas del prefijo
Supervisor  clave por patrón → nueva / actualizada / retirada · estancamiento provisional · cierre definitivo
    │
Registrador alertas.jsonl (solo se agrega, flush por línea) · supervision.json · supervision.md
```

**Reutilización sin duplicar.** Después de cada evento, el supervisor llama a `E.analizar` de OH-DUO-02
sobre el prefijo recibido. No define reglas propias (una prueba lo verifica). Al cerrar, compara su resultado
con `E.analizar_ejecucion` (OH-DUO-02 offline) y lo informa en `coincide_con_oh_duo_02`.

**Lo único repetido** son las 6 líneas de `metadatos()`, que calculan qué archivos están protegidos
(`test_<módulo>.py`) y las huellas de la `tarea/` original, igual que `analizar_ejecucion`. Para eliminarlas
habría que exponer esa función en OH-DUO-02. Es un cambio pequeño, pero queda **pendiente de autorización**;
mientras tanto, la comparación con OH-DUO-02 detectaría cualquier diferencia.

## Alertas: identidad, primera iteración, provisional y definitiva

- **Una alerta por patrón (sin duplicados):**
  - A1 y A2: regla, acción o pruebas, y el primer evento del grupo, que no cambia al crecer el grupo.
  - Demás reglas: una por regla.

  Si el patrón crece, se anota `alerta_actualizada` (más veces), no una alerta nueva.
- **`iteracion_umbral`:** la primera iteración en la que la regla alcanzó su umbral, según la evidencia (por
  ejemplo, la tercera repetición en A1). **`iteracion_de_deteccion` y `evento_de_deteccion`:** cuándo la vio
  el observador. En las pruebas coinciden.
- **Provisional:** toda alerta durante la ejecución. **Definitiva:** la que sigue en el análisis final.
  **Retirada:** la que dejó de cumplirse durante la ejecución; por ejemplo, A6 desaparece si el agente vuelve
  a cambiar el código productivo. **Descartada:** provisional que no sobrevivió al cierre.
- **Estancamiento provisional:** el primer evento en el que el veredicto de OH-DUO-02 sobre el prefijo da
  estancamiento. El definitivo sale del cierre.

**Ojo:** según la definición de OH-DUO-02, una sola edición **efectiva** de un archivo protegido (A3) ya cuenta
como patrón. Por eso, en la reproducción, el estancamiento provisional aparece en la iteración 3. Cada regla
tiene, además, su propia primera iteración en el informe.

## Validación

**Los registros reales de `ensayo-p1` siguen sin estar en el repositorio.** Las validaciones de acá usan la
reproducción de OH-DUO-02 (`oh-duo-02/tests/fixtures/repro-p1`): eventos reales del SDK 1.58.1, con el sandbox y
el ejecutor de OH-DUO-01, generados con un Ollama simulado. En la VivoBook hay que repetir la fase A con los
registros reales (ver «Despliegue»).

### Fase A: reproducción cronológica (`informes/repro-p1/`)

| Ejecución | Primera alerta | Estancamiento provisional | Final | Coincide con OH-DUO-02 |
|---|---|---|---|---|
| P1__qwen | iteración **3** de 30 | iteración 3 (27 antes del final) | estancamiento, agotamiento de iteraciones | sí |
| P1__gemma | **ninguna** | nunca | `finish`, sin estancamiento | sí |

Primera iteración en la que cada regla alcanzó su umbral, en Qwen:

| Regla | Iteración |
|---|---|
| A3 edición de archivo protegido | 3 |
| A5 pruebas que aprueban solo tras editar lo protegido | 4 |
| A6 cola sin progreso | 8 |
| A4 ediciones rechazadas o sin efecto | 8 |
| A1 repetición equivalente (`ejecutar_pruebas`) | 9 |

### Fase B: en vivo, con un agente simulado (`informes/vivo-p1-simulado/`)

La cadena de OH-DUO-01 corrió de verdad: `duo.py ejecutar`, SDK 1.58.1 en `bwrap` sin privilegios, puente y un
Ollama simulado que tarda 0,3 s por respuesta. Al mismo tiempo, `observador.py vigilar --corrida` corría en
otro proceso.

- **Qwen:** la primera alerta (A3) quedó registrada en la iteración 3, **10,6 s antes** del
  `ConversationErrorEvent MaxIterationsReached`. A5, A6, A4 y A1 llegaron después, todas antes del final salvo
  la última A1, en la iteración 30.
- **Gemma:** el registro tiene solo `eventos_encontrados` y `cierre`. **0 alertas.**
- **Las dos ejecuciones** coinciden con OH-DUO-02 offline.

### Pruebas automáticas (13)

- **Fase A (7):** alertas antes de la iteración 30; primera iteración por regla; Gemma sin alertas;
  coincidencia con OH-DUO-02; sin duplicados y todas definitivas; evidencia; registros intactos e informes.
- **Fuente (1):** orden, huecos, temporales y marcadores del SDK, archivos ilegibles reintentados.
- **Fase B (3):** un escritor simulado, con escritura atómica como el SDK, prueba que las alertas lleguen antes
  del último evento; la observación de varias ejecuciones omitiendo las congeladas; y que no se lean
  `ocultas/`, `referencia/` ni `verificacion/`.
- **Provisionales (2):** A6 que se retira cuando el agente progresa y termina bien; y que todas las alertas
  salgan de `E.analizar`, sin reglas propias.

## Despliegue en la VivoBook

Requiere `~/oh-duo-02`, al lado. No instala nada, no corre como servicio y no necesita permisos especiales:
el mismo usuario que corre `duo.py`.

```bash
git clone --depth 1 --branch claude/zealous-cray-jqmao5 https://github.com/dieguinPinga/dieguinPinga.git /tmp/oh-src
cp -r /tmp/oh-src/oh-duo-03 ~/oh-duo-03 && rm -rf /tmp/oh-src
cd ~/oh-duo-03 && python3 -m unittest discover -s tests -v
```

**Fase A con los registros reales** (sin inferencias):

```bash
python3 observador.py reproducir --corrida ~/oh-duo-01/corridas/ensayo-p1        # → informes/ensayo-p1/
python3 observador.py reproducir --corrida ~/oh-duo-01/corridas/ensayo-p1 --ritmo 0.2   # más lento, para mirarlo
```

**Fase B en vivo** (cuando autoricen la prueba controlada). En dos terminales, primero el observador:

```bash
# terminal 1
python3 ~/oh-duo-03/observador.py vigilar --corrida ~/oh-duo-01/corridas/<nombre> --maximo 2
# terminal 2
cd ~/oh-duo-01 && python3 duo.py ejecutar --corrida <nombre> --problemas P1
```

- **Qué ejecuciones observa:** las nuevas. Las ya congeladas antes de empezar se omiten; para esas está
  `reproducir`.
- **Cuándo termina cada una:** cuando OH-DUO-01 escribe `ejecucion.json`, o 10 s después de un evento
  terminal.
- **Dónde se ve:** `informes/<nombre>/<ejecución>/alertas.jsonl` crece en vivo; se puede seguir con
  `tail -f`.

Opciones: `--intervalo` (segundos de sondeo), `--umbrales` (los de OH-DUO-02), `--oh-duo-02` (ruta del
detector) y `--salida`.

## Límites conocidos

- **Costo:** cada evento vuelve a analizar el prefijo completo (cuadrático), unos milisegundos con 60 a 100
  eventos. Para ejecuciones mucho más largas convendría un detector incremental.
- **Retirada de A6:** A6 puede aparecer como provisional y retirarse si el agente después progresa. Queda
  registrada como `retirada` y no cuenta como definitiva.
- **Usuario compartido:** el observador corre con el mismo usuario que el agente. El agente no puede verlo,
  porque está en su sandbox sin `/home`, y el observador no le escribe nada.
