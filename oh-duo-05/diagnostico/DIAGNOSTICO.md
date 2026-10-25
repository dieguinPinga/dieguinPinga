# Diagnóstico del primer ensayo real (P1, Qwen, semilla 11)

**Ensayo:** control 30 iteraciones (`MaxIterationsReached`); intervenida 17 (`stuck`). El mensaje se
entregó, P1 no se resolvió y el par salió **NO VÁLIDO** porque divergió desde la acción 1.

**Alcance:** sin inferencias nuevas. Este documento separa lo **verificado acá** (con el SDK 1.58.1, el relay
y el ejecutor de la rama, usando un modelo simulado) de las **hipótesis**. Las hipótesis se deciden con los
registros de la VivoBook, que extrae y analiza un solo comando, solo de lectura:

```bash
rm -rf /tmp/oh-src && git clone --depth 1 --branch claude/zealous-cray-jqmao5 https://github.com/dieguinPinga/dieguinPinga.git /tmp/oh-src && cp -r /tmp/oh-src/oh-duo-05/. ~/oh-duo-05/ && rm -rf /tmp/oh-src && bash ~/oh-duo-05/diagnostico/extraer_vivobook.sh
```

El comando deja todo en `~/oh-duo-05/informes/qwen-p1-s11-<etiqueta>/diagnostico/`:
- `diagnostico.md`: el par, analizado;
- `sistema.txt`: configuración de Ollama y de la GPU, y si los archivos cambiaron desde el ensayo;
- `ollama_journal.txt`: el registro de Ollama durante el ensayo, si se puede leer;
- un paquete `.tar.gz` con los registros de los dos brazos.

Para seguir alcanza con pegar `diagnostico.md` y `sistema.txt`. Las únicas consultas a Ollama son
`GET /api/version` y `GET /api/ps`.

## 1. Qué cambia desde la primera acción

**Verificado.** Capturé el primer pedido que el SDK le manda a Ollama con la configuración exacta de `agente.py`,
en dos conversaciones distintas:
- los dos pedidos son iguales salvo una línea del system prompt, *"Your current working directory is: …"*;
- en el sandbox esa línea es `/work` en los dos brazos, así que el primer pedido es **idéntico byte a byte**;
- no lleva fecha, hora ni identificadores;
- las opciones que viajan son `{"temperature": 0.2, "seed": 11, "num_ctx": 16384, "num_predict": 2048, "stop": ["</function"]}` y `think: false`. El relay registra esas mismas opciones y el tamaño de cada pedido.

**Consecuencia.** Si la acción 1 difiere y el pedido 1 fue idéntico, entonces **Ollama devolvió otra respuesta
al mismo pedido**. `analizar_par.py` lo comprueba con los datos reales. Compara las huellas de:
- el system prompt y las herramientas (`SystemPromptEvent`);
- la tarea;
- el tamaño del primer pedido según el relay (`puente.jsonl`);
- las opciones de **todos** los pedidos.

También muestra lado a lado las primeras acciones: herramienta, argumentos, pensamiento, `prompt_tokens`,
`completion_tokens` y latencia de cada llamada.

## 2. ¿Semilla 11 + temperatura 0,2 garantizan reproducibilidad?

**No en nuestra configuración.**
- **Qué fija la semilla:** la secuencia del generador aleatorio del muestreo, no los logits.
- **De qué dependen los logits en llama.cpp con GPU:** de cómo se procesa el prompt: el tamaño de lote, si se
  reutiliza la caché KV de un prefijo ya evaluado y cuántos pedidos atiende el modelo en paralelo.
- **Por qué alcanza con eso para divergir:** con temperatura 0,2 la distribución es puntiaguda pero no
  determinista. Una diferencia numérica mínima cambia un token cuando la probabilidad cae cerca del umbral del
  muestreo, y desde ese token la conversación entera es otra.

**Entre los dos brazos hubo una diferencia de estado de Ollama**, y el protocolo no la controlaba:

| | control | intervenida |
|---|---|---|
| modelo al empezar | posiblemente sin cargar (la advertencia) | cargado: lo dejó el control |
| caché KV al empezar | vacía | con la conversación del control, que comparte con el primer pedido el prefijo largo (system prompt de ≈ 25 000 caracteres más la tarea) |

**Hipótesis principal (H2/H3).** La intervenida reutilizó ese prefijo en caché y evaluó solo el resto. Es otro
camino numérico, y explicaría una primera respuesta distinta con el mismo pedido. Se distingue con los datos:
- `prompt_tokens` de la primera llamada en cada brazo. Es el `prompt_eval_count` de Ollama. Si con el mismo
  pedido la intervenida informa bastantes menos tokens, es compatible con una caché reutilizada. Hay que
  confirmar cómo lo cuenta la versión instalada;
- la latencia de la primera llamada: en frío incluye la carga del modelo;
- `OLLAMA_NUM_PARALLEL`, `OLLAMA_FLASH_ATTENTION` y `OLLAMA_KV_CACHE_TYPE` en `sistema.txt`;
- en `ollama_journal.txt`, si el modelo quedó entero en la GPU o se descargó y recargó entre brazos.

**H4, variabilidad residual con un estado idéntico,** no se puede descartar sin repeticiones (etapa A, abajo).

## 3. Por qué el SDK detuvo la intervenida como `stuck` y el control llegó a 30

**Verificado en el código del SDK 1.58.1** (`conversation/stuck_detector.py` y `local_conversation.py`).

Antes de cada paso del agente, `run()` consulta el detector. Mira los **últimos 20 eventos posteriores al
último mensaje de origen `user`** y corta si encuentra alguno de estos patrones:

| Patrón | Condición |
|---|---|
| acción y observación repetidas | 4 acciones idénticas y 4 observaciones idénticas |
| acción con error repetida | más de 3 |
| monólogo | 3 mensajes del agente seguidos |
| alternancia | A, B, A, B, A, B |

«Idéntica» incluye el **pensamiento**, la herramienta y todos los parámetros. Solo ignora los identificadores.

**Dos consecuencias:**
- **El mensaje del coordinador entra como `user` y reinicia la ventana.** No protege: si después Qwen repite 4
  veces exactamente lo mismo, el SDK corta. Lo comprobé con el SDK real (`test_un_mensaje_de_usuario_reinicia_la_ventana`):
  mensaje tras 2 acciones, corte exactamente 4 acciones idénticas después.
- **El control no se cortó, así que en sus 30 iteraciones nunca hubo 4 repeticiones exactas seguidas.** Puede
  ser porque cambia el pensamiento o algún detalle del comando, o porque las correcciones de formato del SDK,
  que también son `user`, reiniciaron la ventana.

**En los datos reales,** `analizar_par.py` reproduce el detector del SDK evento por evento sobre los registros
guardados. La reproducción coincide con el SDK en las 4 pruebas. Informa:
- para la intervenida: en qué evento corta, con qué patrón y desde dónde empieza la ventana;
- para el control: la racha máxima de acciones idénticas y qué mensajes `user` reiniciaron la ventana.

**Implicación para el protocolo:** `stuck` es un **tercer actor**. Las «17 contra 30 iteraciones» no miden el
efecto del coordinador: miden un corte del SDK. OH-DUO-01 lo clasifica como `incompleta`, y OH-DUO-02 como
`detenido_por_el_detector_del_sdk`.

## 4. ¿El mensaje entró al contexto del modelo? ¿Cuál fue la primera acción posterior?

**Verificado (el mecanismo):**
- `send_message` agrega un `MessageEvent` de usuario.
- Sin condensador (`Agent.condenser = None`, que es nuestra configuración), el SDK arma el contexto de cada
  llamada con **todos** los eventos.
- `test_puente` comprueba que el pedido siguiente al modelo contiene el texto.

**En los datos reales,** `analizar_par.py`:
- reconstruye **con la misma función del SDK** (`events_to_messages`) el contexto exacto de la llamada que
  siguió al mensaje, y dice si el texto está, en qué posición y con qué rol;
- compara `prompt_tokens` antes y después: tiene que crecer al menos el largo del mensaje;
- muestra la primera acción posterior con su pensamiento, que indica si Qwen «respondió» al pedido o siguió
  igual;
- incluye lo que registró el puente (`entregado`, `accion_posterior`).

## 5. Advertencia «el modelo posiblemente no estaba cargado»

- **De dónde sale:** `duo.py` consulta `GET /api/ps` antes de cada ejecución. `cambio_de_modelo` vale `true`
  si `qwen2.5-coder:7b` no figuraba, y `null` si no pudo consultarlo.
- **Su efecto directo es de tiempos:** la primera llamada incluye la carga del modelo. Eso se descuenta de los
  900 s, no de las 30 iteraciones.
- **Corrección a lo que dije en el protocolo:** escribí que este aviso «no invalida el par». Para la
  reproducibilidad **sí importa**: es justamente la marca de que los dos brazos arrancaron con Ollama en
  estados distintos, en frío y en caliente, con caché distinta (punto 2).

`sistema.txt` y `ollama_journal.txt` muestran el estado y las cargas y descargas de esos minutos.

## 6. Protocolo propuesto: separar variabilidad, intervención y detección

> Propuesta para revisar. Cada etapa necesita tu autorización. No toca detectores, reglas ni límites. La
> etapa C1 cambia una opción del ejecutor y por eso va aparte.

**Estandarizar el estado de Ollama antes de cada corrida.** Se descarga el modelo con
`ollama stop qwen2.5-coder:7b` (o `POST /api/generate` con `keep_alive: 0` y sin prompt). No genera tokens.
Así cada corrida arranca en frío y con la caché vacía. Además, antes y después de cada corrida se verifica
`/api/ps` y se registra `OLLAMA_NUM_PARALLEL`.

| Etapa | Qué aísla | Corridas | Lectura |
|---|---|---|---|
| **A1** reproducibilidad en frío | variabilidad del modelo | 3 controles (sin `--enviar`), semilla 11, cada uno con el modelo descargado antes | si las 3 son idénticas, hay determinismo con estado estandarizado |
| **A2** efecto del estado | caché y carga de Ollama | 2 controles seguidos **sin** descargar | si A1 es idéntico y A2 diverge, el estado de Ollama explica el NO VÁLIDO del ensayo 1 |
| **B** intervención | efecto del mensaje | pares control/intervenida en frío, semillas 11, 12 y 13, orden alternado | **si A1 es determinista:** el par es válido y lo que cambia después del mensaje se le puede atribuir. **Si no:** se comparan distribuciones (≥ 5 por brazo), no pares |
| **C0** detector del SDK (sin GPU) | corte por `stuck` | sobre todas las corridas anteriores | `analizar_par.py` dice cuándo y por qué cortaría el detector en cada brazo |
| **C1** sin detector del SDK | estancamiento contra intervención | pares como en B con `stuck_detection=False` en **los dos** brazos, para que el único corte sea el límite de 30 | 2 × 2: coordinador {no, sí} × detector del SDK {sí, no} |

**Métricas, por corrida.** La medida principal es la categoría con pruebas ocultas. Además:
- terminación, separando `stuck` del agotamiento del límite y de `finish`;
- índice de la primera divergencia frente a su gemela (en A y B);
- iteración del mensaje;
- prueba de que el mensaje entró al contexto;
- primera acción posterior.

**Costo aproximado.** Cada corrida dura hasta unos 16 minutos. A son 5 corridas (≈ 1,3 h); B son 6
(≈ 1,6 h); C1 son 6 más.

**Orden recomendado:**
1. Diagnóstico de datos (este paso).
2. A1 + A2, que es lo más barato y decide el resto.
3. B.
4. C0, sin costo.
5. C1, solo si B muestra que `stuck` cambia el resultado.

## Lecturas adicionales (manuales, solo lectura)

Si `extraer_vivobook.sh` no pudo leer algo por permisos, o si Ollama corre en otra máquina:

```bash
journalctl -u ollama --since "AAAA-MM-DD HH:MM" --until "AAAA-MM-DD HH:MM" --no-pager | grep -iE "load|offload|layers|ctx|parallel|cache|kv|flash|unload"   # en la máquina de Ollama (puede pedir sudo)
systemctl show ollama --property=Environment --no-pager        # variables del servicio
nvidia-smi --query-gpu=name,driver_version,memory.used,memory.total --format=csv
curl -s http://100.92.153.59:11435/api/ps                      # qué está cargado ahora (no carga nada)
```
