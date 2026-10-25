# OH-DUO-04: coordinador de estrategias (simulación)

Cuando OH-DUO-03 detecta un bucle improductivo, el coordinador decide qué mensaje le **habría** enviado al
**mismo** programador para que cambie de estrategia.

**En esta fase no envía nada:** cada decisión queda registrada con `enviado: false` y
`intervenciones_enviadas` es siempre 0. No hay ningún canal hacia el agente.

**Qué no hace:**

- No cambia de modelo ni da la solución.
- No lee pruebas ocultas, referencias ni el banco.
- No modifica OH-DUO-01, OH-DUO-02 ni OH-DUO-03, ni sus registros.

Solo biblioteca estándar.

## Cómo decide

**Entrada:** el `alertas.jsonl` de OH-DUO-03 (`alerta_nueva`, `alerta_actualizada`, `alerta_retirada`,
`cierre`), en el orden en que se produjo. Reutiliza las alertas tal cual, sin volver a analizar eventos.

| Familia | Reglas de OH-DUO-02 | Decisión |
|---|---|---|
| **Acción prohibida** | A3 editar un archivo protegido; A5 pruebas que pasan solo por haberlo editado | `advertencia` (una por episodio). **No** pide cambiar de estrategia |
| **Bucle** | A1 repetición equivalente; A2 fallos iguales; A4 ediciones rechazadas o sin efecto; A6 sin progreso | `nueva_estrategia`, **solo cuando el bucle se confirma** |

- **Confirmación** (`coordinador.json`): A2 sola (las mismas pruebas fallando tres veces sin cambios en el
  código productivo), o al menos `min_reglas_de_bucle` (2) reglas distintas de bucle activas a la vez. Una
  primera señal suelta, como A1 al ver varias veces el mismo archivo o A6 sola, se registra como
  `sin_intervencion` («señal de bucle todavía sin confirmar»).
- **Un episodio, una intervención:** después de una `nueva_estrategia`, todo lo que aparece o se actualiza
  queda como `sin_intervencion` («episodio ya intervenido»).
  - **Cuándo se cierra el episodio:** cuando hay progreso, es decir, cuando OH-DUO-03 retira A6 (el código
    productivo volvió a cambiar) o retira todas las alertas de bucle.
  - **Cuándo puede abrirse otro:** recién después de `enfriamiento_iteraciones` (5) y con un máximo de
    `max_intervenciones` (2) por ejecución.
- **Registro de cada decisión:**
  - cuándo (momento de la decisión y de la alerta, iteración, evento);
  - por qué (`disparada_por`, `motivo`, `alertas_activas`);
  - qué (el mensaje exacto, su sha256 y su traducción al español).

  Los casos sin intervención también quedan registrados, con su motivo.

## Mensajes: solo plantillas

Las frases están en `coordinador.json` y se arman según las reglas activas en ese momento. Lo único variable
son los **nombres de las pruebas que el propio agente vio fallar** en su terminal (de la clave de A2).

Nunca entran código, valores esperados, contenido de las ocultas ni la referencia. Una prueba verifica que
ninguna línea de la referencia de P1 aparece en los mensajes; la referencia se lee solo dentro de esa prueba.

Ejemplo real de la reproducción (Qwen, iteración 8):

> You have not changed the production code for several iterations. Several of your edits were rejected or had
> no effect. Changing the tests is not a valid fix. Review your diagnosis: state which result you expected and
> which one you got, then propose a different strategy. Do not modify the tests.

**Idioma:** por omisión el mensaje es en **inglés**, porque todo el pedido de OpenHands está en inglés y un
modelo de 7B se confunde menos sin mezclar idiomas. La versión en español, la de tu propuesta, queda
registrada al lado (`traduccion_es`). Para usarla: `{"idioma": "es"}` en un `--config`.

## Validación

**No hay registros reales de Qwen ni de Gemma en el repositorio** (ni de `ensayo-p1` ni de la validación en
vivo de OH-DUO-03): quedaron en la VivoBook. Acá se validó con los registros que sí existen: los informes de
OH-DUO-03 sobre la reproducción de `ensayo-p1`, que son eventos reales del SDK generados con un Ollama
simulado.

| Registro | Qwen | Gemma |
|---|---|---|
| `informes/repro-p1` (reproducción) | advertencia en la iteración **3**; **nueva estrategia en la 8** (confirmada por A6 + A4), 22 iteraciones antes del final; 8 señales posteriores sin intervención (mismo episodio) | ninguna decisión |
| `informes/vivo-p1-simulado` | lo mismo | ninguna decisión |
| `informes/vivo-04-cadena-simulada`: **cadena completa en vivo** (duo.py + SDK en bwrap, OH-DUO-03 `vigilar` y OH-DUO-04 `vigilar` en paralelo, Ollama simulado de 0,3 s por respuesta) | advertencia **10,1 s** y nueva estrategia **8,1 s** antes del `MaxIterationsReached`; `enviado: false` | ninguna decisión |

**Pruebas (17):**

- **6 sobre los registros existentes:**
  - Qwen y Gemma en las dos reproducciones;
  - salidas;
  - la cadena eventos → OH-DUO-03 → OH-DUO-04, que da lo mismo que los informes guardados;
  - que no lea nada fuera de las alertas;
  - que los mensajes no contengan la solución.
- **11 sintéticas:**
  - la primera advertencia no activa la estrategia;
  - una señal suelta no alcanza;
  - A2 sola confirma y nombra las pruebas;
  - sin repeticiones en el episodio;
  - nuevo episodio tras el progreso, con enfriamiento;
  - límite de intervenciones;
  - retirada antes de confirmar;
  - advertencias desactivables e idioma;
  - mensaje solo de plantillas;
  - configuración desconocida;
  - seguimiento en vivo con líneas cortadas a la mitad.

## Mañana en la VivoBook

```bash
git clone --depth 1 --branch claude/zealous-cray-jqmao5 https://github.com/dieguinPinga/dieguinPinga.git /tmp/oh-src
cp -r /tmp/oh-src/oh-duo-04 ~/oh-duo-04 && rm -rf /tmp/oh-src
cd ~/oh-duo-04 && python3 -m unittest discover -s tests -v      # 17 pruebas; usa ~/oh-duo-01..03 si están
```

**1. Sobre los registros reales que ya existen (sin inferencias).** Si OH-DUO-03 ya dejó informes de la
corrida real:

```bash
python3 coordinador.py simular --informes ~/oh-duo-03/informes/<corrida-real>     # → informes/<corrida-real>/
less informes/<corrida-real>/P1__qwen/coordinacion.md
```

Si solo están los eventos de `ensayo-p1`, primero se generan las alertas:

```bash
python3 ~/oh-duo-03/observador.py reproducir --corrida ~/oh-duo-01/corridas/ensayo-p1
python3 coordinador.py simular --informes ~/oh-duo-03/informes/ensayo-p1
```

Hay que revisar en qué iteración aparece la `nueva_estrategia` (OH-DUO-03 detectó el estancamiento en la 10),
si el mensaje es razonable para lo que estaba haciendo Qwen, y que Gemma no tenga ninguna decisión.

**2. En vivo, en simulación** (con una corrida nueva de OH-DUO-01, solo si autorizan la inferencia). Las
terminales se abren en este orden:

```bash
# terminal 1: OH-DUO-03 observa los eventos
python3 ~/oh-duo-03/observador.py vigilar --corrida ~/oh-duo-01/corridas/<nueva> --maximo 2
# terminal 2: OH-DUO-04 sigue las alertas y muestra lo que habría enviado
python3 ~/oh-duo-04/coordinador.py vigilar --informes ~/oh-duo-03/informes/<nueva> --maximo 2
# terminal 3: el programador, igual que siempre
cd ~/oh-duo-01 && python3 duo.py ejecutar --corrida <nueva> --problemas P1
```

En la terminal 2 aparecen líneas `[advertencia]` y `[nueva_estrategia]` con el mensaje. **Nada llega al
agente.** El resultado queda en `~/oh-duo-04/informes/<nueva>/`: `decisiones.jsonl`, `coordinacion.json` y
`coordinacion.md`.

## Lo que falta para intervenir de verdad (no implementado)

Para enviar el mensaje haría falta un canal hacia la conversación. Por ejemplo:

- `agente.py` lee, entre pasos, un archivo de buzón montado en solo lectura;
- y llama a `conversation.send_message`.

Eso modifica el ejecutor de OH-DUO-01 y el diseño experimental (el programador deja de ser autónomo), así que
requiere su autorización. Además, convendría un experimento que compare las corridas con y sin
intervención en las mismas condiciones.

## Decisiones abiertas

- **Idioma del mensaje:** inglés, por omisión, o español.
- **Confirmación:** ¿A4 (ediciones rechazadas) cuenta como señal de bucle? Hoy sí. En la reproducción, la
  confirmación de la iteración 8 se apoya en A6 + A4; sin A4 llegaría con A1 + A6 en la 9.
- **Advertencias por acciones prohibidas:** activadas. Con `advertir_acciones_prohibidas: false` solo quedan
  las recomendaciones de estrategia.
