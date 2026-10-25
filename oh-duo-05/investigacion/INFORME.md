# OH-DUO-05: investigación técnica para intervenir durante la ejecución

**Estado:** solo investigación. No se implementó la intervención real, no se ejecutaron inferencias y no se
modificaron OH-DUO-01, 02, 03 ni 04.

**Cómo se verificó:**

- Código fuente de OpenHands en la etiqueta `v1.58.1` (commit `60192ed5`).
- El paquete `openhands-sdk==1.58.1` instalado con el mismo `requisitos.lock` que usa la VivoBook.
- Dos experimentos reproducibles con un LLM **simulado**: `experimento_mecanismos.py` y `prototipo_puente.py`.

## 1. ¿Funcionan los dos mecanismos en el SDK 1.58.1?

Los dos ejemplos existen en `v1.58.1`: `examples/01_standalone_sdk/18_send_message_while_processing.py` y
`09_pause_example.py`. Los dos funcionan, pero **solo el primero es compatible con el experimento**.

Resultado de `experimento_mecanismos.py` (límite de 8 iteraciones para que sea rápido):

| Caso | Resultado medido |
|---|---|
| **A) `send_message()` con `run()` activo** (ejemplo 18) | El mensaje entra como `MessageEvent(source="user", sender="oh-duo-04")`, y lo incluye el siguiente pedido al LLM. `send_message` esperó hasta el fin del paso en curso (0,1 a 0,9 s con el LLM simulado). **8 acciones con límite 8: el límite se respeta.** |
| **B) `pause()` → `send_message()` → `run()`** (ejemplo 09) | Misma conversación y historial conservado, pero **13 acciones con límite 8**: cada `run()` arranca su contador en 0. |
| **C) `send_message()` después de `finish`** | El estado pasa de `finished` a **`idle`** y **nadie lo procesa** (0 pedidos al LLM). |
| **D) El mismo mensaje dos veces** | 2 `MessageEvent`: **el SDK no deduplica**. |

**Por qué, en el código del SDK** (`conversation/impl/local_conversation.py`):

- **Candado:** `send_message` hace `with self._state:`. El `run()` síncrono retiene ese candado durante todo
  `agent.step()`, que incluye la llamada al LLM y la ejecución de la herramienta. La liberación del candado
  durante la espera de red (`_released_state_lock_during_io`) existe **solo en `arun()`**.
- **Consecuencia:** un `send_message` desde otro hilo **espera a que termine el paso en curso**; no lo
  interrumpe ni se pierde.
- **Sin inanición:** el candado es un `FIFOLock` (`conversation/fifo_lock.py`), que atiende en orden de
  llegada. El mensaje entra **entre dos pasos**.
- **Contador por `run()`:** `_run()` declara `iteration = 0` como variable local y corta con
  `MaxIterationsReached` al llegar a `max_iteration_per_run` **de esa llamada**.
- **Estados finales:** `send_message` vuelve `FINISHED` o `STUCK` a `IDLE` («new message resets terminal
  states»). Si `run()` ya salió, el mensaje queda sin procesar.
- **El modelo no ve el remitente:** `MessageEvent.to_llm_message()` no incluye `sender`. Qwen lo recibe como un
  mensaje de usuario más. El origen queda en el evento, para auditoría.
- **Detector del SDK:** `stuck_detector._events_since_last_user_message` reinicia su ventana con cada mensaje
  de usuario. El detector propio del SDK vuelve a empezar después de la intervención; OH-DUO-02/03 no, porque
  miran la ejecución completa.
- **Sin condensador:** `Agent.condenser` es `None` por omisión y `agente.py` no lo cambia, así que el mensaje
  no se resume ni se descarta del contexto.

## 2. `agente.py` de OH-DUO-01: ¿puede el coordinador llegar a la misma `Conversation`?

**No.** `conv` vive dentro del proceso `agente.py`, que corre en `bwrap --unshare-all`, con espacios propios de
PID, montaje, red e IPC. El coordinador (OH-DUO-04) corre en el anfitrión, en otro proceso. No hay memoria
compartida ni API, y abrir una (un socket o el agent-server) cambiaría la arquitectura y el sandbox.

Lo único que comparten son archivos de las carpetas montadas. Por eso, quien llame a `conv.send_message()`
tiene que ser **un hilo dentro del mismo proceso que `conv`**.

Estructura actual relevante (`oh-duo-01/ejecutor/agente.py`, líneas 90–116):

- `conv = Conversation(...)` y `conv.send_message(TAREA)`;
- `conv.run()` en un hilo `correr`;
- el hilo principal hace `join(MAX_SEG)`; si vence, pide `conv.pause()` en otro hilo, porque `pause()` también
  espera el candado.

## 3. Alternativa recomendada: un hilo «puente» con buzón de solo lectura

Se descarta **pause → send → run (B)**: le da al agente intervenido más iteraciones que al de control (30 más
por cada intervención) y rompe la comparación. También se descartan los *hooks* del SDK: el único que agrega
contexto (`UserPromptSubmit`) enriquece mensajes de usuario existentes, no inyecta nuevos.

**Diseño (A, con resguardos):**

```
anfitrión                                           sandbox (sin cambios salvo un montaje de solo lectura)
OH-DUO-04 decide → despachador ──agrega línea──►  <corrida>/buzon/mensajes.jsonl ──ro──► /buzon/mensajes.jsonl
                                                                                           │ hilo puente (agente.py)
                                                                                           ▼
                                                 with conv.state:  si RUNNING → conv.send_message(texto, sender=…)
```

Verificado con `prototipo_puente.py`:

| Caso | Resultado |
|---|---|
| Durante `run()` | `entregado`, mensaje en el evento 6 y primera acción posterior en el 7; **8/8 acciones** |
| Línea duplicada en el buzón | `entregado` + `duplicado_ignorado`: un solo `MessageEvent` |
| Llega después de `finish` | `descartado` (`finished`); **el estado queda `finished`**, no `idle` |

### Modificaciones exactas (en COPIAS nuevas, `oh-duo-05/ejecutor/`; OH-DUO-01 queda igual)

**`agente.py`** = `oh-duo-01/ejecutor/agente.py` más este bloque, entre `conv.send_message(TAREA)` (línea 92) y
`error = []` (línea 96):

```python
# --- Puente hacia el coordinador: lee /buzon/mensajes.jsonl (solo lectura) y entrega cada mensaje una vez ---
from openhands.sdk.conversation.state import ConversationExecutionStatus as _Estado  # noqa: E402

BUZON, ENTREGAS = Path("/buzon/mensajes.jsonl"), ESTADO / "entregas.jsonl"
fin_puente = threading.Event()


def puente():
    leido, pendiente, vistos = 0, "", set()
    while not fin_puente.is_set():
        try:
            if BUZON.is_file():
                with open(BUZON, encoding="utf-8") as f:    # mismo patrón que el seguimiento en vivo de OH-DUO-04
                    f.seek(leido)
                    pendiente += f.read()
                    leido = f.tell()
                *completas, pendiente = pendiente.split("\n")  # una línea a medio escribir espera
                for linea in filter(None, completas):
                    m = json.loads(linea)
                    registro = {"id": m["id"], "sha256": m["sha256"], "momento": time.time()}
                    if m["id"] in vistos:
                        registro["resultado"] = "duplicado_ignorado"
                    else:
                        vistos.add(m["id"])
                        with conv.state:                            # mismo FIFOLock que run(): sin carreras
                            estado = conv.state.execution_status
                            if estado != _Estado.RUNNING:
                                registro.update(resultado="descartado", estado=estado.value)
                            else:
                                conv.send_message(m["texto"], sender=f"oh-duo-04:{m['id']}")
                                registro.update(resultado="entregado", evento=len(conv.state.events) - 1)
                    with open(ENTREGAS, "a", encoding="utf-8") as g:
                        g.write(json.dumps(registro) + "\n")
        except Exception as e:  # el puente nunca debe tumbar al agente
            with open(ENTREGAS, "a", encoding="utf-8") as g:
                g.write(json.dumps({"resultado": "error_del_puente", "detalle": repr(e)}) + "\n")
        fin_puente.wait(0.5)


threading.Thread(target=puente, daemon=True).start()
```

Y `fin_puente.set()` inmediatamente después de `corte_forzado = hilo.is_alive()` (línea 116).

**`correr.sh`** = `oh-duo-01/ejecutor/correr.sh` con tres cambios:

```bash
# (1) junto a «mkdir -p "$R/work" "$R/estado" "$R/control"»:
mkdir -p "$R/buzon" && : > "$R/buzon/mensajes.jsonl"
# (2) en MONTAJES, después de la línea de enfoque.txt:
          --ro-bind "$R/buzon" /buzon
# (3) en el control previo del sandbox, agregar "/buzon/x" a la lista de rutas que deben ser NO escribibles.
```

Es el único cambio al sandbox: un montaje **de solo lectura**. No se usa `/estado` como buzón porque es
escribible por el agente, y un comando suyo podría fabricarse mensajes «del coordinador».

**Despachador** (nuevo, en el anfitrión): lee `decisiones.jsonl` de OH-DUO-04. Solo con `--enviar`, y solo
para `nueva_estrategia`, agrega a `<corrida>/buzon/mensajes.jsonl` la línea
`{"id": "<episodio>-<n>", "texto": …, "sha256": …}`, con escritura atómica de línea completa. Lo registra en
su propio `despachos.jsonl`. OH-DUO-04 no cambia: sigue escribiendo `enviado: false`, y el envío real es
responsabilidad del despachador, que queda registrado aparte.

## 4. Cómo confirmar con eventos que Qwen recibió la instrucción

Tres niveles, de más fuerte a más débil:

1. **Entregado al SDK:** existe `MessageEvent` con `source="user"` y `sender="oh-duo-04:<id>"` en los eventos
   persistidos, en el índice *k*. Cruce: la línea de `entregas.jsonl` con el mismo `id` y `evento = k`.
2. **Recibido por el modelo:** existe un `ActionEvent` (o un `MessageEvent` del agente) con índice **mayor que
   *k***.
   - **Por qué alcanza:** `send_message` solo entra cuando terminó el paso en curso, y el paso siguiente arma
     su pedido con todos los eventos, sin condensador.
   - **Comprobación en el experimento A:** el mensaje del evento 8 aparece en el texto del pedido #4.
   - **Si no hay ninguna acción posterior** (por ejemplo, se agotaron las iteraciones o el plazo): «entregado,
     no procesado».
3. **Cambio de comportamiento** (se mide, no se afirma): después de *k*, si aparece una acción nueva, si
   cambia el código productivo (la huella de OH-DUO-02), si se apaga el patrón o si el pensamiento menciona
   el diagnóstico. OH-DUO-02/03 ya calculan casi todo esto; habría que agregar el «antes y después» de la
   intervención.

## 5. Riesgos

| Riesgo | Detalle | Mitigación en el diseño |
|---|---|---|
| **Concurrencia** | `send_message` espera el fin del paso: hasta un pedido completo al LLM (≤ 300 s × 2 intentos) más la herramienta | El que espera es el hilo puente, no el agente ni el coordinador. El candado es FIFO. La comprobación de `RUNNING` y el envío ocurren bajo el **mismo** candado |
| **Carrera con `finish` o el límite** | Un `send_message` crudo tras `finish` cambia el estado a `idle`. OH-DUO-01 registraría `estado_final="idle"` en vez de `finished` y se perdería el mensaje | Solo se envía en `RUNNING`; si no, se registra `descartado` (verificado: el estado queda `finished`) |
| **Plazo `MAX_SEG`** | La pausa del plazo y el puente compiten por el candado | La pausa sigue en su propio hilo. Con `PAUSED`, el puente descarta. `fin_puente` se activa antes de leer los eventos |
| **Duplicación** | El SDK no deduplica | `id` por episodio y mensaje. El puente ignora los `id` ya vistos y el despachador no reescribe. Hay doble registro (`despachos.jsonl` en el anfitrión y `entregas.jsonl` en el sandbox) |
| **Límite de 30 iteraciones** | El mensaje no consume una iteración, pero si llega tarde no queda margen. Con B (pause/run) se multiplicaría el presupuesto | Solo A (sin `pause`/`run`). El despachador no envía si quedan menos de N iteraciones (propuesta: 5). En la reproducción, la intervención de la iteración 8 deja 22 |
| **Línea a medio escribir** | El despachador escribe mientras el puente lee | El puente solo procesa líneas terminadas en `\n` (como el seguimiento en vivo de OH-DUO-04). El despachador escribe cada línea completa de una vez |
| **Mensajes falsificados** | Un comando del agente podría escribir en carpetas escribibles | Buzón montado **en solo lectura**. Además, el control previo verifica que `/buzon` no sea escribible |
| **Contexto** | Prompt inicial de unos 8 000 a 10 000 tokens con `num_ctx` 16384; el mensaje agrega unos 80 tokens | Despreciable; se controla con `prompt_eval_count` |
| **Detector del SDK** | Su ventana se reinicia con el mensaje | Se documenta. Las comparaciones usan OH-DUO-02, que mira todo |
| **Validez experimental** | Con intervención, el programador deja de ser autónomo | Comparar con y sin intervención en las mismas condiciones, con mismas semillas y límites; cuenta solo el criterio de las ocultas, evaluadas al final |

## 6. Prueba mínima propuesta (sin modificar OH-DUO-01 a 04)

1. **Hecha acá, sin GPU:** `experimento_mecanismos.py` y `prototipo_puente.py`, con el SDK 1.58.1 en proceso y
   un LLM simulado (resultados arriba).
2. **En la VivoBook, sin GPU** (siguiente paso que propongo):
   1. `bash diagnostico_vivobook.sh`, de solo lectura, sobre el `venv` instalado.
   2. Los dos experimentos con el `venv` real. No usan la red ni el sandbox; escriben solo en `/tmp`:

      ```bash
      ~/oh-smoke/venv/bin/python -I experimento_mecanismos.py
      ~/oh-smoke/venv/bin/python -I prototipo_puente.py
      ```

3. **Después, con autorización, todavía sin GPU:** implementar las copias de §3 en `oh-duo-05/ejecutor/`. Probar
   con la cadena real (bwrap, puente y el Ollama simulado que reproduce el bucle de Qwen) que:
   - el mensaje llega por el buzón;
   - aparece el `MessageEvent` con `sender`;
   - hay una acción posterior;
   - las acciones siguen ≤ 30;
   - los casos tardío y duplicado se manejan bien.
4. **Recién después, una inferencia real controlada:** P1 con Qwen, con intervención en simulación primero y
   después real, comparada con la corrida sin intervención.

## Resultado del diagnóstico de acá

`bash diagnostico_vivobook.sh <venv con openhands-sdk 1.58.1>`: las 10 comprobaciones dan **OK**. Si en la
VivoBook alguna da **NO**, hay que revisar este informe antes de implementar.
