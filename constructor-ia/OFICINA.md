# Oficina de agentes distribuidos

Roles, computadoras y misiones como entidades independientes. El diseño aprobado está en `AGENTE_DATOS.md` §15 y en
la propuesta técnica del 2026-10-23. Se implementa por etapas; cada una tiene sus pruebas y no altera el laboratorio.

**Decisiones del MVP (Diego y ChatGPT):**
- Tailscale con ACL restrictiva y comprobación de identidad, sin token adicional.
- Un turno de inferencia simultáneo por trabajador.
- Primera microtarea real: revisión de Python, con verificación independiente.

## Etapa 1: registro de trabajadores y disponibilidad (implementada, sin inferencias)

### Qué se reutiliza (verificado al releer el código)

| Pieza | Uso |
|---|---|
| `ollama.OllamaClient` | Sin cambios. `version()`, `tags()` y `loaded()` (`/api/ps`) para comprobar disponibilidad. Habla HTTP, así que Tailscale Serve tiene que exponer HTTP o TCP |
| `db.Database` | Conexión SQLite (WAL, una conexión por hilo). La oficina usa su propio esquema en su propia base; **no agrega migraciones** a la base de la aplicación |
| `ollama.InferenceGate` | Hoy es global por proceso. Se separa por destino en la etapa 3, conservando el comportamiento actual por omisión |
| `missions.py`, `workers.py` | Atados a la aplicación web (una misión pesada a la vez). Sirven como patrón de estados y de cola para la etapa 2, no para reutilizarlos directamente |
| `agente_datos/qwen.py` | Su interfaz `pedir()` es la que implementará el adaptador remoto en la etapa 3 |

### Qué funciona

- **`constructor_ia/oficina/registro.py`**, con una base propia en `<datos>/oficina/oficina.db`.
- **Registro de un trabajador:**
  - URL `http://` del tailnet (IP 100.64.0.0/10 o nombre `*.ts.net`) con puerto explícito, sin ruta ni credenciales.
    Loopback solo para pruebas (`--permitir-local`), y nunca el puerto 11434 del Ollama principal;
  - identidad comprobada con `tailscale whois --json` (solo lectura) contra el nodo esperado (`--nodo`);
  - modelos declarados y recursos (CPU, RAM, GPU, VRAM);
  - capacidad fija en 1 y permisos fijos en `inferencia`.
- **Disponibilidad (solo lectura):**
  - vuelve a comprobar la identidad y después lee `/api/version`, `/api/tags` y `/api/ps`, sin inferencias;
  - un trabajador está «disponible» si responde, sigue siendo el mismo nodo y tiene todos sus modelos declarados;
  - el latido vence a los 120 s de la última comprobación exitosa;
  - cada comprobación queda registrada, con latencia, versión, modelos, modelos cargados, faltantes, identidad y
    motivo.
- **Retiro:** el trabajador sale del registro y su historial se conserva.
- **CLI:**

```bash
constructor-ia oficina registrar --id pulenta --url http://<IP o nombre .ts.net>:11435 --modelo gemma4:12b \
    --nodo <nombre del nodo en Tailscale> --cpu "Core Ultra 9 185H" --ram-gb 32 --gpu "RTX 4070" --vram-gb 8
constructor-ia oficina estado            # todos (código 3 si alguno no está disponible); --id X; --json
constructor-ia oficina retirar --id pulenta
```

Pruebas: `tests/test_oficina.py` (14), con un Ollama falso y un `whois` simulado. Cubren:
- **URL:** solo tailnet y puerto explícito, loopback solo en pruebas y nunca 11434;
- **Identidad:** registro con identidad comprobada; identidad distinta o nodo faltante = rechazo; identidad que
  cambia = no disponible, sin hablarle;
- **Registro:** duplicados e id inválidos rechazados; retiro con historial conservado;
- **Disponibilidad:** solo lee (ningún POST), modelo faltante = no disponible, desconexión = no disponible, latido
  vencido = no disponible ahora;
- **CLI:** registrar, estado, retirar, errores sin traza;
- **Aislamiento:** la oficina solo crea su propia base.

Suite completa con esta etapa: 521 pruebas, OK (670 s).

### Qué hacen ustedes a mano (la oficina no configura Tailscale)

1. **En la notebook:** Ollama escucha solo en `127.0.0.1`, y Tailscale Serve lo publica en el puerto 11435 dentro del
   tailnet, en HTTP o TCP. **Sin Funnel.**
2. **ACL:** solo la VivoBook (por ejemplo, `tag:coordinador`) llega a `tag:trabajador:11435`. Ollama no tiene
   autenticación ni permisos por ruta: la ACL es la única barrera para `/api/pull` o `/api/delete`.
3. **En la VivoBook:**
   - `constructor-ia oficina registrar …`;
   - `constructor-ia oficina estado` (no hace inferencias; la notebook solo ve tres lecturas).

Comprobada en hardware real por Diego y ChatGPT (commit `db453ca`, worktree independiente de la VivoBook):
- trabajador `pulenta` registrado: identidad de Tailscale `rampern`, `gemma4:12b`, RTX 4070 de 8 GB, capacidad 1 y
  permiso «inferencia»;
- estado disponible, latencia de comprobación 282 ms, historial persistente en SQLite;
- sin inferencias.

**Pendiente antes de enviar trabajos reales:** verificar la ACL restrictiva de Tailscale.

## Etapa 2: cola persistente de tareas (implementada, sin inferencias)

`constructor_ia/oficina/cola.py`, en la misma base experimental (`<datos>/oficina/oficina.db`, tablas propias:
`misiones`, `tareas`, `dependencias`, `eventos`).

- **Misiones con dependencias:**
  - una misión es un grupo de tareas;
  - cada tarea declara rol, modelo, permiso (`inferencia`), origen de los datos (`sintetico`), una carga JSON de
    hasta 64 KB, `max_intentos` (1–10), `tiempo_max_s` y `depende_de`;
  - se rechazan los ciclos, las dependencias desconocidas, los permisos que no sean `inferencia` y los datos que no
    sean sintéticos;
  - todo o nada: una misión inválida no deja nada a medias.
- **Estados:** `pendiente → asignada → en_curso → propuesta → verificada`. Una tarea rechazada, vencida o con una falla
  informada vuelve a `pendiente` mientras queden intentos; si no, pasa a `fallida`, y lo que dependía de ella queda
  `cancelada`. La misión queda `pendiente`, `en_curso`, `completa`, `fallida` o `cancelada`.
- **Asignación atómica:**
  - `tomar` corre en una transacción `BEGIN IMMEDIATE`, y tiene pruebas con 8 hilos y con 4 procesos;
  - un turno por trabajador;
  - solo se asignan tareas con todas sus dependencias **verificadas**, el modelo y el permiso declarados y datos
    sintéticos;
  - `tomar_para` exige, además, un trabajador del registro disponible **ahora** (latido vigente).
- **Arriendos:**
  - cada asignación lleva un token y vence a los `tiempo_max_s` segundos; `latido` la renueva;
  - una entrega con un token viejo, o con el arriendo vencido, se rechaza: el resultado tardío de un trabajador
    desconectado nunca pisa una reasignación.
- **Recuperación:** `recuperar` devuelve a la cola los arriendos vencidos y cuenta el intento. Se llama al arrancar y
  periódicamente, así que un coordinador que se cae y vuelve recupera lo que quedó «en curso».
- **Verificación central:**
  - el trabajador solo entrega una **propuesta**;
  - solo el coordinador la verifica o la rechaza;
  - una propuesta no habilita a las tareas que dependen de ella; una rechazada no queda como resultado.
- **Trazabilidad:** cada transición queda en `eventos`, con su origen, su destino, el trabajador y el motivo.
- **Ensayo** (`ensayo.py`): trabajadores simulados en hilos, en una base desechable propia, sin red ni modelos.
  - Dos misiones: d y e sueltas, y la cadena a → b → c.
  - El «caido» toma d y se desconecta; el «tramposo» entrega una propuesta incorrecta; el «firme» trabaja bien.
  - El coordinador recupera y verifica con una regla determinista.
  - Se espera:
    - las dos misiones completas;
    - una recuperación;
    - una propuesta rechazada;
    - la entrega tardía rechazada;
    - las dos misiones avanzando a la vez.

**CLI** (base de la oficina; solo el ensayo usa una base desechable):

```bash
constructor-ia oficina mision --archivo plan.json   # {"titulo": …, "tareas": [{"nombre", "rol", "modelo", "carga", "depende_de", …}]}
constructor-ia oficina cola [--json]                # misiones y tareas con estado, intento, trabajador y dependencias
constructor-ia oficina recuperar                    # arriendos vencidos → cola
constructor-ia oficina cancelar --mision <id>
constructor-ia oficina ensayo-cola                  # trabajadores simulados, base en <datos>/oficina/ensayos/cola-<fecha>
```

En esta etapa nadie toma tareas de la base real: no hay ejecutor todavía (etapa 3). Las misiones creadas ahí quedan
pendientes, que es lo esperado.

Suite completa con esta etapa: 536 pruebas, OK (657 s).

### Cómo verificar la cola desde la VivoBook (sin inferencias)

Ejecutar en el worktree independiente; no se detiene ni se toca ningún servicio:

```bash
cd <worktree> && git pull --ff-only
python3 -m unittest tests.test_oficina_cola tests.test_oficina     # 31 pruebas, sin red ni modelos
python3 -m constructor_ia oficina ensayo-cola                     # «Resultado: OK» (unos 3 s)
cat > /tmp/plan-revision.json <<'JSON'
{"titulo": "revisión de prueba", "tareas": [
  {"nombre": "revisar", "rol": "revisor", "modelo": "gemma4:12b", "carga": {"texto": "def f(): return 1"}},
  {"nombre": "resumir", "rol": "redactor", "modelo": "gemma4:12b", "carga": {}, "depende_de": ["revisar"]}]}
JSON
python3 -m constructor_ia oficina mision --archivo /tmp/plan-revision.json
python3 -m constructor_ia oficina cola          # «resumir» depende de «revisar»; las dos quedan pendientes
python3 -m constructor_ia oficina cancelar --mision <id que imprimió «mision»>
```

El ensayo y las pruebas no usan la red, la notebook, Ollama ni el laboratorio.

## Escenario estándar permanente: el almacén (aprobado por Diego y ChatGPT)

Un único contexto ficticio para probar la oficina, hoy con trabajadores simulados y en las etapas siguientes con
inferencia distribuida. Está en `constructor_ia/oficina/almacen.py` (datos, verdad, misiones y verificación) y en
`ensayo_almacen.py` (los nueve trabajadores y el coordinador). El ensayo genérico de la cola (`ensayo-cola`) se
conserva.

### Registros: `ventas(id, fecha, producto, cantidad, precio_unitario)`

20 ventas, del 1 al 20 de marzo de 2026, una por día y rotando los productos: Arroz $2, Fideos $3, Aceite $10 y
Yerba $5. Todo con enteros.

| id | fecha | producto | cant. | precio | importe | | id | fecha | producto | cant. | precio | importe |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 03-01 | Arroz | 2 | 2 | 4 | | 11 | 03-11 | Aceite | 1 | 10 | 10 |
| 2 | 03-02 | Fideos | 3 | 3 | 9 | | 12 | 03-12 | Yerba | 4 | 5 | 20 |
| 3 | 03-03 | Aceite | 1 | 10 | 10 | | 13 | 03-13 | Arroz | 4 | 2 | 8 |
| 4 | 03-04 | Yerba | 2 | 5 | 10 | | 14 | 03-14 | Fideos | 1 | 3 | 3 |
| 5 | 03-05 | Arroz | 5 | 2 | 10 | | 15 | 03-15 | Aceite | 3 | 10 | 30 |
| 6 | 03-06 | Fideos | 2 | 3 | 6 | | 16 | 03-16 | Yerba | 2 | 5 | 10 |
| 7 | 03-07 | Aceite | 2 | 10 | 20 | | 17 | 03-17 | Arroz | 6 | 2 | 12 |
| 8 | 03-08 | Yerba | 1 | 5 | 5 | | 18 | 03-18 | Fideos | 5 | 3 | 15 |
| 9 | 03-09 | Arroz | 3 | 2 | 6 | | 19 | 03-19 | Aceite | 2 | 10 | 20 |
| 10 | 03-10 | Fideos | 4 | 3 | 12 | | 20 | 03-20 | Yerba | 3 | 5 | 15 |

### Misiones y resultados esperados

| Misión | Tarea(s) | Resultado |
|---|---|---|
| A | importe total | **235** |
| B | unidades totales | **56** |
| C | por producto | Arroz 20 u / $40 · Fideos 15 / 45 · Yerba 12 / 60 · Aceite 9 / 90 |
| D | más unidades, más importe (dependen de C verificada) | **Arroz 20** · **Aceite 90** (rankings inversos) |
| E | período del 1 al 10 de marzo, inclusive | **$92 y 25 unidades** |
| F | auditoría de casos sintéticos propios (no depende de A) | $220 → «incorrecto», valor 235 · $235 → «correcto», 235 |

- **El auditor** recibe los registros y la propuesta a auditar, nunca la respuesta del coordinador. El coordinador
  verifica su dictamen por su cuenta.
- **Más adelante**, la auditoría de propuestas reales se integrará como un paso previo a la verificación definitiva.

### Verdad independiente

- `verdad()` la calcula en Python desde los registros, y las pruebas exigen que coincida con la tabla escrita a mano
  (`ESPERADO`).
- La verdad nunca entra en una carga y lleva un canario que se busca en la base del ensayo.
- Un rechazo dice qué campo no coincide, sin revelar el valor correcto.
- Se comparan enteros exactos: `"235"` (texto) o `true` no valen como 235.

### Nueve trabajadores simulados

| Perfil | Rol | Conducta | Dónde se ve |
|---|---|---|---|
| periodo_corrido | calculista, un turno | incluye el día 11 | E: 102 / 26 → rechazada |
| distraido | calculista, un turno | omite la última fila | A: 220 → rechazada |
| confundido | calculista, un turno | ignora la cantidad | B: 20 → rechazada |
| mezclador | calculista, un turno | intercambia unidades e importe | C → rechazada |
| complaciente | auditor, un turno | dice «correcto» a todo | F $220 → rechazada |
| formato_roto | calculista, un turno | devuelve algo ilegible | la primera tarea que vuelve a la cola → rechazada |
| desconectado | calculista | abandona su primera tarea; vuelve después del vencimiento | arriendo recuperado; su entrega tardía, rechazada; después trabaja bien |
| riguroso | auditor | recalcula | F → aceptadas |
| correcto | calculista | calcula bien | termina los reintentos → aceptadas |

- **Arranque escalonado:** cada trabajador arranca cuando el anterior ya tomó su primera tarea. Por eso cada perfil
  recibe una tarea donde su error se ve, y la corrida es reproducible.
- **Lo que comprueba el ensayo:**
  - las cinco misiones completas, con exactamente los valores de la tabla;
  - **cero verificaciones falsas**: cada tarea verificada se vuelve a comparar con la tabla escrita a mano;
  - los nueve perfiles participaron;
  - cada perfil con error fue rechazado una vez;
  - un arriendo recuperado y una entrega tardía rechazada;
  - aceptaciones en un reintento;
  - varias misiones avanzando a la vez;
  - ninguna fuga de la verdad.
- **Historial:** `informe.json` guarda, por cada transición y cada verificación, quién, cuándo y por qué.

```bash
python3 -m unittest tests.test_oficina_almacen          # 12 pruebas
python3 -m constructor_ia oficina ensayo-almacen        # «Resultado: OK» en unos 2 s; informe en <datos>/oficina/ensayos/almacen-<fecha>/
```

Suite completa con el almacén: 548 pruebas, OK (653 s).

Salida esperada: las cinco misiones `completa`. Por perfil (tomadas / aceptadas / rechazadas): `1/0/1` en los seis
de un turno, `desconectado` con 1 abandonada y 1 entrega tardía rechazada, y `riguroso` y `correcto` sin rechazos.
En total, 6 rechazos y 1 recuperación.

## Etapa 3: programación real con Gemma remoto (preparada, SIN inferencias ni código de la IA ejecutado)

Objetivo: que una IA real reciba una misión, escriba Python, lo ejecute aislado, reciba una evaluación y se corrija
en nuevos intentos, sin intervención humana entre ellos.

### Lo que ya existía y se reutiliza sin cambios

| Componente | Qué aporta |
|---|---|
| `engine.Engine` (motor original de Constructor IA) | El ciclo completo: prompt inicial → código → validación estática → pruebas en el sandbox → informe de fallos → reparación, hasta `max_attempts`. Registra cada intento: respuesta, código, resultado, tokens, tiempo y motivo de corte |
| `sandbox.executor.Executor` | Bubblewrap + seccomp, sin red, límites de memoria, procesos y CPU (scope de systemd), plazo por caso y total, autodiagnóstico antes de habilitarse y resultado firmado con un nonce |
| `prompts` | Ejemplos visibles en el prompt y casos ocultos solo contados. `failure_report` no revela el valor esperado de un caso oculto |
| `codeextract` | Extracción y validación estática del código |
| `ollama.OllamaClient` | Inferencia por `/api/chat`, con plazo, cancelación y respaldo si el modelo no admite «think» |
| Oficina, etapas 1 y 2 | Registro con identidad, disponibilidad y la cola con arriendos, recuperación y verificación central |

El `InferenceGate` global de la aplicación y `ollama.py` no se modificaron.

### Lo que se agregó (lo mínimo)

- **`oficina/remoto.py`:**
  - `ClienteRemoto`: el mismo `OllamaClient`, con una lista blanca de rutas (`GET /api/version|tags|ps` y
    `POST /api/chat`). `pull`, `delete`, `create` y `generate` se rechazan sin conectarse;
  - `compuerta(destino)`: un candado por URL; Gemma remoto y un Qwen local no se bloquean entre sí;
  - `elegir()`: el planificador. Un trabajador sirve si está disponible ahora y tiene el modelo y el permiso. Un
    destino local necesita además 3.900 MiB libres; si no hay ninguno, la tarea queda pendiente con el motivo.
- **`oficina/misiones_programacion.py`:** la primera misión, `resumen_ventas(ventas, desde=None, hasta=None)`, sobre los
  mismos 20 registros del almacén.
  - 2 casos visibles (un ejemplo chico y la lista vacía).
  - 5 casos ocultos: los 20 registros, el período del 1 al 10 inclusive, un «desde» en el último día, un período sin
    ventas y un empate resuelto por orden alfabético.
  - Incluye nuestra **solución de referencia**: la comprobación previa la corre en el sandbox para demostrar que las
    pruebas son correctas antes de gastar una inferencia.
- **`oficina/programador.py`:**
  - `Taller`: el `Engine` original, con su base y su carpeta **propias** (`<oficina>/taller.db` y
    `<oficina>/taller/`) y el cliente remoto;
  - `Programador.atender(trabajador)`, en este orden:
    1. toma una tarea de rol «programador» de la cola;
    2. registra la ejecución (tarea, intento de la cola, misión del taller);
    3. corre el ciclo completo, renovando el arriendo con un latido; si el arriendo se pierde, la misión se cancela;
    4. entrega una **propuesta** con la huella del código, los tokens y el tiempo.

    Si Ollama no responde o el sandbox está bloqueado, es una falla de entorno: la tarea vuelve a la cola, nunca
    queda como respuesta.
  - `verificar()`: el coordinador **vuelve a ejecutar** el `solucion.py` final contra todos los casos, incluidos los
    ocultos. Además comprueba la huella y que la misión haya aprobado. Recién ahí la tarea queda verificada.
  - **Interrupciones:** una ejecución que quedó a medias por un proceso caído se marca «interrumpida», nunca
    «aprobada». La cola reasigna la tarea con otro token, y una entrega tardía del proceso anterior se rechaza.
- **Dónde se ejecuta el código:** **siempre en la VivoBook**, dentro del sandbox. La pulenta solo hace inferencia:
  nunca recibe ni ejecuta código.
- **CLI:**

```bash
constructor-ia oficina comprobar-programacion --trabajador pulenta    # SIN inferencias ni código de la IA
constructor-ia oficina encolar-programacion                           # misión resumen_ventas, gemma4:12b
constructor-ia oficina programar --trabajador pulenta --autorizo-inferencia-remota
```

`comprobar-programacion` comprueba, sin inferencias:
- el trabajador: identidad, modelo instalado y latido;
- el planificador;
- el autodiagnóstico del sandbox;
- la misión con la solución de referencia (7/7).

`programar` repite esas comprobaciones y se niega sin `--autorizo-inferencia-remota`. Con la autorización, atiende
**una** tarea, la verifica centralmente e imprime cada intento (estado, casos, tokens y segundos).

Pruebas: `tests/test_oficina_programacion.py` (13), con un Ollama falso que devuelve código escrito a mano y el
sandbox real (bwrap):
- **Rutas:** la lista blanca rechaza todo lo demás antes de conectarse.
- **Candado:** uno por destino.
- **Planificador:**
  - lo remoto no depende de la RAM de la VivoBook;
  - un destino local con 3.650 MiB queda pendiente;
  - sin modelo, sin permiso o no disponible: pendiente.
- **Misión:**
  - la referencia aprueba 7/7;
  - dos soluciones con errores típicos (sin filtro de fecha; «hasta» exclusivo) fallan en casos ocultos;
  - el prompt no contiene ningún valor ni argumento de un caso oculto.
- **Ciclo completo sin intervención:**
  - el intento 1 falla, el 2 falla y el 3 aprueba;
  - el informe de corrección llega al modelo sin la verdad;
  - la verificación central aprueba 7/7;
  - código y respuesta guardados por intento.
- **Casos negativos:**
  - una misión que nunca aprueba se rechaza y vuelve a la cola;
  - una propuesta con la huella adulterada se rechaza;
  - un trabajador caído es una falla de entorno;
  - una ejecución interrumpida queda «interrumpida», sin duplicados y con la entrega tardía rechazada;
  - un arriendo perdido cancela la misión;
  - un trabajador no disponible no toma nada.
- **CLI:** comprobar no infiere; programar sin la autorización se niega sin enviar nada; con la autorización,
  ACEPTADA.

Suite completa con esta etapa: 561 pruebas, OK (665 s).

### Comprobaciones necesarias antes de la primera ejecución real

1. **ACL de Tailscale (pendiente, a cargo de ustedes; la oficina no las modifica):**
   - en la consola de administración, solo la VivoBook llega al puerto 11435 de la pulenta;
   - desde otro equipo del tailnet, `curl http://<pulenta>:11435/api/tags` tiene que fallar;
   - en la pulenta, `netstat -an | findstr 11434` debe mostrar solo `127.0.0.1:11434`;
   - Tailscale Serve, sin Funnel: `tailscale serve status` y `tailscale funnel status`.
2. **Conectividad e identidad, desde la VivoBook:**
   - `tailscale ping <pulenta>`;
   - `tailscale whois <IP>`: tiene que mostrar el nodo `rampern`.
3. **Modelo realmente instalado:** `constructor-ia oficina estado --id pulenta` (lee `/api/tags` y `/api/ps`): tiene que
   decir disponible, con `gemma4:12b`.
4. **Sandbox habilitado en la VivoBook:** lo cubre `comprobar-programacion` (autodiagnóstico de bwrap, seccomp y
   límites).
5. **Pruebas válidas:** la referencia aprueba 7/7, también en `comprobar-programacion`.

### Procedimiento protegido para la primera ejecución (cuando lo autoricen)

En el worktree independiente de la VivoBook. No se detiene ni se reinicia ningún servicio: la inferencia es remota y
el sandbox tiene sus propios límites.

```bash
cd <worktree> && git pull --ff-only
python3 -m unittest tests.test_oficina_programacion tests.test_oficina_almacen tests.test_oficina_cola tests.test_oficina
C=~/constructor-ia-piloto/data/oficina-programacion-1          # carpeta NUEVA para esta corrida
python3 -m constructor_ia oficina registrar --carpeta $C --id pulenta --url http://<IP o .ts.net>:11435 \
    --modelo gemma4:12b --nodo rampern --gpu "RTX 4070" --vram-gb 8 --ram-gb 32
python3 -m constructor_ia oficina comprobar-programacion --carpeta $C --trabajador pulenta   # todo [OK]
python3 -m constructor_ia oficina encolar-programacion --carpeta $C
python3 -m constructor_ia oficina programar --carpeta $C --trabajador pulenta --autorizo-inferencia-remota
python3 -m constructor_ia oficina cola --carpeta $C
```

**Qué se espera:**
- hasta 4 intentos con Gemma, cada uno ejecutado en el sandbox de la VivoBook;
- la verificación central imprime ACEPTADA o RECHAZADA. Si es RECHAZADA, la tarea vuelve a la cola: queda 1 intento;
- todo queda en `$C`:
  - `taller.db` y `taller/`: respuestas, código por intento, resultados, tokens y tiempos;
  - `oficina.db`: cola, eventos, ejecuciones y comprobaciones.

**Lo que esta etapa no demuestra:** que Gemma resuelva la misión. Eso lo dice la primera corrida autorizada.

### Primera ejecución real (Diego y ChatGPT)

Gemma 12B en `rampern` resolvió `resumen_ventas` en el **primer intento**: 7/7 pruebas, 670 tokens y 64,2 s. La
verificación central independiente también aprobó.

## Segunda misión: `conciliar_movimientos` (preparada, SIN inferencias)

Se agrega al catálogo (`oficina/misiones_programacion.py`) sin tocar el motor. Usa el mismo circuito: `Engine`,
Bubblewrap + seccomp, el informe de fallos existente (`prompts.failure_report`), hasta **4 intentos**, la verificación
central por reejecución y el registro de cada intento. No tiene fallos artificiales: los reintentos, si los hay, salen
de la dificultad real de la misión.

### Contrato

`conciliar_movimientos(movimientos, saldos_iniciales=None, conteo_fisico=None)`.

**Entrada:**
- `movimientos`: diccionarios de dos formas:
  - ingreso o egreso: `{"id", "fecha" 'AAAA-MM-DD', "producto", "tipo": "ingreso"|"egreso", "cantidad"}`;
  - anulación: `{"id", "fecha", "producto", "tipo": "anulacion", "anula": <id>}`. Su producto se ignora: vale el del
    movimiento anulado.
- `saldos_iniciales` y `conteo_fisico`: `{producto: entero}`, o `None`.
- No se modifica la entrada.

**Proceso:**
- En orden de `(fecha, id)`, sin importar el orden de la lista.
- Cada movimiento se aplica o se ignora. Si se ignora, genera una inconsistencia con el **primer** motivo que
  corresponda, revisados en este orden:
  1. `id_duplicado`;
  2. `tipo_desconocido`;
  3. `cantidad_invalida` (cantidad que no es un entero mayor que 0), o `saldo_insuficiente` si un egreso dejaría el
     saldo negativo;
  4. para anulaciones:
     - `anulacion_inexistente` (no hay un movimiento aplicado anterior con ese id);
     - `anulacion_de_anulacion`;
     - `anulacion_repetida`;
     - `saldo_insuficiente`, si revertir dejaría el saldo negativo.
- Una anulación revierte por completo el efecto del movimiento anulado.

**Salida:**
- `saldos`: los productos de `saldos_iniciales` o de algún movimiento aplicado;
- `movimientos_aplicados`;
- `inconsistencias`: `[{"id", "motivo"}]`, en el orden de proceso;
- `conciliacion`: `None` sin conteo; si hay conteo, `{producto: conteo − saldo}`, solo con las diferencias distintas
  de 0 (un producto que falta en uno de los dos lados vale 0).

### Casos (resultados escritos a mano)

| Caso | Qué ejercita | Resultado |
|---|---|---|
| V1 (visible) | ingreso, egreso, anulación del egreso, saldo inicial y conteo | Arroz 12 y Yerba 5; 4 aplicados; conciliación `{"Yerba": -1}` |
| V2 (visible) | egreso sin saldo | Aceite 2; 1 aplicado; `saldo_insuficiente` (id 2) |
| H1 | lista vacía | todo vacío; conciliación `None` |
| H2 | solo saldos y conteo: un saldo en 0 y un producto solo en el conteo | Arroz 3 y Fideos 0; conciliación `{"Sal": 2}` |
| H3 | lista desordenada; la anulación es anterior, por fecha, al egreso que anula | Arroz 3; 3 aplicados; `anulacion_inexistente` |
| H4 | revertir un ingreso ya vendido, una anulación repetida, una anulación de anulación y una anulación posterior válida | Yerba 0; 4 aplicados; 3 inconsistencias; conciliación `{}` |
| H5 | id duplicado, cantidades 0, −2 y `"4"`, tipo desconocido, anulación de un movimiento ignorado | Sal 5; 1 aplicado; 6 inconsistencias |
| H6 | varios productos desordenados, egreso hasta 0 exacto, conteo con un producto sin saldo | Arroz 9, Aceite 2 y Fideos 0; 9 aplicados; conciliación `{"Aceite": -1, "Yerba": 2}` |
| H7 | el mismo día manda el id, no el tipo (el egreso id 1 va antes que el ingreso id 2) | Sal 5; `saldo_insuficiente` (id 1) |

La **solución de referencia** (`REFERENCIA_CONCILIAR`) es código nuestro, independiente de los valores escritos a
mano. `comprobar-programacion` la ejecuta en el sandbox y tiene que aprobar 9/9 antes de cualquier inferencia. El
modelo ve el contrato, los 2 casos visibles y el aviso «Además hay 7 casos ocultos». Si un caso oculto falla, el
informe dice cuál falló, pero no su valor esperado ni sus argumentos.

Pruebas: `tests/test_oficina_conciliar.py` (7).
- **Referencia:**
  - coincide con los 9 resultados escritos a mano y no modifica la entrada;
  - aprueba 9/9 en el sandbox real.
- **Cobertura:** cubren los 7 motivos del contrato.
- **Errores típicos,** escritos a mano solo para validar las pruebas (nunca se le dan al modelo): procesar en el orden
  de la lista, no controlar el saldo, incluir las diferencias en 0 y anular movimientos ignorados. Cada uno falla en
  al menos un caso oculto, sin que el informe revele el esperado.
- **Prompt:** no contiene productos, fechas ni motivos propios de los casos ocultos.
- **Ciclo existente:** recorre la misión y la verifica centralmente, 9/9.
- **CLI:** comprobar y encolar sin inferencias.

Suite completa con esta misión: 568 pruebas, OK (654 s).

### Ejecutar la segunda misión en la VivoBook (cuando lo autoricen)

```bash
cd <worktree> && git pull --ff-only
python3 -m unittest tests.test_oficina_conciliar tests.test_oficina_programacion     # 20 pruebas, sin inferencias
C=~/constructor-ia-piloto/data/oficina-programacion-2              # carpeta NUEVA (la de la primera misión queda intacta)
python3 -m constructor_ia oficina registrar --carpeta $C --id pulenta --url http://<IP o .ts.net>:11435 \
    --modelo gemma4:12b --nodo rampern --gpu "RTX 4070" --vram-gb 8 --ram-gb 32
python3 -m constructor_ia oficina comprobar-programacion --carpeta $C --trabajador pulenta --mision conciliar_movimientos
python3 -m constructor_ia oficina encolar-programacion --carpeta $C --mision conciliar_movimientos
python3 -m constructor_ia oficina programar --carpeta $C --trabajador pulenta --mision conciliar_movimientos \
    --autorizo-inferencia-remota
python3 -m constructor_ia oficina cola --carpeta $C
```

`comprobar-programacion` tiene que dar todo `[OK]`, con la referencia en 9/9. `programar` imprime cada intento
(estado, casos, tokens y segundos) y la verificación central. El registro completo queda en `$C/taller.db` y
`$C/taller/`.

## Autocorrección: auditoría y mejoras, después de la segunda misión real

**Resultado real de `conciliar_movimientos`:**
- 4 intentos de Gemma 12B: 8/9, 0/9, 8/9 y 8/9; verificación central rechazada;
- en tres intentos falló solo el caso 6: `anulacion_de_anulacion` clasificada como `anulacion_inexistente`;
- Gemma detectó la contradicción en sus comentarios, pero no corrigió la implementación.

### Qué recibía el modelo (auditoría de `engine.py` y `prompts.py`)

| Intento | Recibía | Se ocultaba |
|---|---|---|
| 1 | contrato, firma, 2 ejemplos visibles y «Además hay 7 casos ocultos» | los casos ocultos |
| 2 a 4 | contrato (no se recortaba: 2.254 caracteres, contra un límite de 4.755), ejemplos, el **último** código y `failure_report` | argumentos, valores esperados y salida de cada caso oculto |

Para un caso oculto que falla, `failure_report` dice solamente `Caso oculto caso 6: fallo`.

**Por qué no se corrigió:**
1. El informe no decía **qué** parte del resultado estaba mal: el modelo tenía que adivinar entre todo el contrato.
2. El motor siempre corrige a partir del **último** código. Después del 0/9 del intento 2, el intento 3 partió de esa
   versión rota y no de la de 8/9.
3. Nada señalaba que el mismo caso fallaba intento tras intento.

(La causa exacta del 0/9 está en `intento-2/resultado.json` de la corrida, en la VivoBook.)

### Mejoras (generales; nada específico de una misión)

Están en `oficina/autocorreccion.py`, `MotorConMemoria`: una subclase del `Engine` original que solo usa la oficina.
No cambian `engine.py` ni `prompts.py`, el sandbox, la validación, la aprobación (todas las pruebas, con resultado
firmado) ni la verificación central.

1. **Pistas seguras para los casos ocultos fallidos:**
   - las **claves de primer nivel** del resultado con un valor incorrecto, solo si ese nombre ya figura en el
     contrato y solo si el caso espera un diccionario. Por ejemplo: «claves con valor incorrecto: 'inconsistencias'
     (el resto está bien)»;
   - si devolvió claves que el contrato no pide, o algo que no es un diccionario;
   - **qué ejercita** el caso: una categoría que escribe el autor de la misión (campo `categoria`) y que describe la
     regla del contrato en juego, sin valores. Se muestra solo cuando ese caso falla.

   Nunca se muestran valores esperados, argumentos ni la salida del caso. Si la salida vino recortada, no hay pista
   de claves.
2. **Mejor versión conservada:**
   - el puntaje de un intento es la cantidad de casos aprobados (0 si no se pudo ejecutar);
   - la mejor versión es la de mayor puntaje; en un empate, la primera;
   - cada intento conserva su código y su resultado, así que **nada se reemplaza ni se borra**;
   - al terminar, la mejor queda en `mejor/solucion.py` y `mejor/resumen.json`, con el historial de puntajes y de la
     base de cada intento.
3. **Control de regresiones:** si el último intento aprobó **menos** casos que el mejor, el siguiente parte de la
   mejor versión, con su informe y un aviso explícito: «ATENCIÓN, REGRESIÓN: tu intento 2 aprobó 0 de 9 casos, MENOS
   que el intento 1 (8 de 9)… abajo va la del intento 1 como punto de partida». Con el mismo puntaje se sigue desde
   el último.
4. **Estancamiento:** se avisa si un caso oculto falló en varios intentos y sigue fallando en la base («las
   correcciones anteriores no cambiaron la regla responsable…»), o si el código es idéntico a uno anterior.
5. **Verificación final:**
   - una misión solo se acepta si algún intento pasa **todas** las pruebas, y el coordinador la vuelve a ejecutar;
   - si no aprueba, la verificación central igual reejecuta la mejor versión conservada e informa su puntaje
     independiente (por ejemplo, «mejor versión conservada: intento 1 (8/9); reejecución independiente: 8/9»), y la
     rechaza.
6. **Trazabilidad:** lo que se le envió al modelo en cada reintento queda en `intento-N/retroalimentacion.md`, con su
   punto de partida. `programar` imprime la mejor versión conservada.

Las dos misiones tienen ahora la `categoria` de cada caso oculto. Las corridas históricas no se tocan: el código que
produjo Gemma sigue en su carpeta, sin cambios.

Pruebas: `tests/test_oficina_autocorreccion.py` (6), con un Ollama falso y el sandbox real.
- **Pistas:**
  - solo nombres del contrato y la categoría, sin valores, productos ni fechas;
  - nunca nombres que no estén en el contrato;
  - claves de más, resultado que no es un diccionario, salida recortada y casos visibles.
- **Simulación pedida:** el intento 1 da 8/9 (el mismo tipo de error de Gemma), el 2 empeora a 0/9 y el 3 recibe el
  aviso de regresión y el código del intento 1, no el del 2. El 3 aprueba 9/9, la verificación central acepta y el
  código de cada intento se conserva tal cual.
- **Sin éxito:** la mejor versión (intento 1, 8/9) queda conservada, después de cada regresión se vuelve a partir de
  ella, y la verificación central la reejecuta (8/9) y rechaza.
- **Estancamiento:** código idéntico y caso persistente señalados; un empate no es una regresión.

Suite completa: 574 pruebas, OK (670 s).

### Próxima prueba real (VivoBook contra Gemma 12B en rampern)

```bash
cd <worktree> && git pull --ff-only
python3 -m unittest tests.test_oficina_autocorreccion tests.test_oficina_conciliar tests.test_oficina_programacion
C=~/constructor-ia-piloto/data/oficina-programacion-3              # carpeta NUEVA; las corridas anteriores quedan intactas
python3 -m constructor_ia oficina registrar --carpeta $C --id pulenta --url http://<IP o .ts.net>:11435 \
    --modelo gemma4:12b --nodo rampern --gpu "RTX 4070" --vram-gb 8 --ram-gb 32
python3 -m constructor_ia oficina comprobar-programacion --carpeta $C --trabajador pulenta --mision conciliar_movimientos
python3 -m constructor_ia oficina encolar-programacion --carpeta $C --mision conciliar_movimientos
python3 -m constructor_ia oficina programar --carpeta $C --trabajador pulenta --mision conciliar_movimientos \
    --autorizo-inferencia-remota
```

Lo que el modelo recibió en cada reintento queda en `$C/taller/missions/<misión>/intento-N/retroalimentacion.md`.

## Rol REVISOR DE CÓDIGO: PROGRAMADOR → REVISOR → PROGRAMADOR → EVALUADOR

**Lo que motivó el rol:** la tercera experiencia real, con el coordinador `090a9ef`, dio 8/9 en los cuatro
intentos. Fueron cuatro versiones distintas con el **mismo** error (una anulación de una anulación salía como
`anulacion_inexistente`), pese a las pistas seguras. Hipótesis a investigar: un segundo rol que lea el código contra
el contrato, aunque sea el mismo modelo en otra sesión, mejora la autocorrección.

### Integración (auditoría: el punto más sencillo)

`MotorConMemoria._attempt` ya decide qué recibe cada reintento. Se agregó solo:
- un punto de extensión, `_complementar`, que por omisión no hace nada;
- la subclase `MotorConRevisor`, que la oficina usa siempre: **sin revisor se comporta exactamente como antes**.

No cambian `engine.py`, `prompts.py`, el sandbox, la aprobación por pruebas, la mejor versión conservada ni la
verificación central. El revisor es el evaluador de nadie: evalúan las pruebas y el verificador.

### El rol (`oficina/revisor.py`)

**Prompt propio** (`SISTEMA_REVISOR`). Le pide buscar contradicciones entre el código y el contrato, ramas
inalcanzables, estados que no se registran, chequeos en otro orden y casos confundidos, y prohíbe dos cosas: escribir
el código corregido y aprobar. Responde en JSON (`format: json` de Ollama) con este esquema:

```json
{"resumen": "...", "observaciones": [{"regla_del_contrato": "...", "problema": "...", "donde": "...",
                                     "por_que": "...", "sugerencia": "..."}]}
```

**Qué recibe** (solo información autorizada, la misma que ya ve el programador):
- el contrato completo, la firma y los ejemplos visibles;
- el código candidato (el punto de partida del próximo intento);
- el feedback autorizado de ese candidato: informe, pistas seguras y avisos.

Nunca recibe argumentos, valores esperados ni salidas de los casos ocultos.

**Qué no puede hacer:**
- escribir la solución: los bloques de código de su respuesta se reemplazan por «[código omitido…]»;
- aprobar: decir «APROBADO» no aprueba nada;
- reemplazar el código: el punto de partida sigue siendo el del programador.

Sus observaciones llegan al programador como texto («puede equivocarse: vos decidís»), hasta 2.500 caracteres.

**Cuándo interviene** (criterio general, `debe_revisar`; vale para cualquier misión):
- antes del intento N (N > 1), si hay al menos 2 intentos ejecutados, el último **no superó** al mejor de los
  anteriores, **y** un mismo caso (visible u oculto) falló en **cada uno** de los últimos 2 intentos;
- si el programador mejora, o si fallan casos distintos, no interviene y no se gasta ninguna inferencia.

Un intento que no llega a ejecutarse (por ejemplo, por un error de sintaxis) no tiene casos fallidos, así que no
cuenta para el criterio.

**Límites** (`ConfigRevisor`): una revisión omitida queda registrada con su motivo.
- `max_revisiones` (2 por misión): las fallidas también cuentan;
- `max_segundos` (1.200 s de revisión por misión);
- `num_predict` 1.024 y plazo de 600 s por llamada;
- el pedido tiene que entrar en el contexto del modelo.

**Fallas del revisor:** si no responde o devuelve algo inválido, queda «fallida» con su motivo. El programador sigue
con el feedback normal y la mejor versión no se toca.

**Registro para auditoría:**
- `intento-N/revision.json`: criterio, pedido completo, respuesta, diagnóstico saneado, tokens, segundos y estado
  (hecha, fallida u omitida);
- `intento-N/retroalimentacion.md`: lo que recibió el programador, incluidas las observaciones;
- `mejor/resumen.json`: lista de revisiones;
- el evento «revision» en el taller;
- `programar` imprime cada revisión.

**Uso:** `programar … --revisor [--revisor-modelo M] [--max-revisiones 2] [--max-segundos-revision 1200]`. Sin
`--revisor`, todo funciona igual que antes.

Pruebas: `tests/test_oficina_revisor.py` (10), con un Ollama falso y el sandbox real.
- **Criterio:**
  - 1 intento, mejora o casos distintos: no interviene;
  - mismo caso sin mejora, o empeora y repite el caso: interviene.
- **Respuesta:** se valida y se sanea (sin código); las inválidas se rechazan.
- **Pedido:** contiene el contrato, el código y el feedback autorizado, pero nada de los casos ocultos.
- **Simulación pedida:**
  - el programador se estanca (8/9 y 8/9, con dos códigos distintos y el mismo error);
  - el revisor interviene una vez, antes del intento 3, y señala que las anulaciones aplicadas no se registran;
  - el programador recibe las observaciones y aprueba 9/9; la verificación central acepta;
  - todos los registros y el código de cada intento se conservan.
- **Sin estancamiento:** si el programador mejora, el revisor no se llama.
- **Sin autoridad:** un revisor que dice «APROBADO» y escribe código no aprueba ni reemplaza nada. Su código se
  omite, la misión falla y la mejor versión (8/9) se conserva y se reejecuta.
- **Fallas:** respuesta inválida o revisor caído: se registra y el ciclo continúa con los 4 intentos.
- **Límites:** con `max_revisiones=1` hay una sola llamada y la segunda queda omitida; `max_segundos=0`: ninguna
  llamada.
- **Compatibilidad:** sin revisor, las mismas 4 llamadas de siempre.

Suite completa: 584 pruebas, OK (674 s).

### Cuarta experiencia real (VivoBook contra Gemma 12B en rampern)

```bash
cd <worktree> && git pull --ff-only
python3 -m unittest tests.test_oficina_revisor tests.test_oficina_autocorreccion tests.test_oficina_conciliar \
    tests.test_oficina_programacion                                   # 36 pruebas, sin inferencias
C=~/constructor-ia-piloto/data/oficina-programacion-4               # carpeta NUEVA; las anteriores quedan intactas
python3 -m constructor_ia oficina registrar --carpeta $C --id pulenta --url http://<IP o .ts.net>:11435 \
    --modelo gemma4:12b --nodo rampern --gpu "RTX 4070" --vram-gb 8 --ram-gb 32
python3 -m constructor_ia oficina comprobar-programacion --carpeta $C --trabajador pulenta --mision conciliar_movimientos
python3 -m constructor_ia oficina encolar-programacion --carpeta $C --mision conciliar_movimientos
python3 -m constructor_ia oficina programar --carpeta $C --trabajador pulenta --mision conciliar_movimientos \
    --autorizo-inferencia-remota --revisor
```

**Qué se espera:**
- hasta 4 intentos del programador y, como máximo, 2 revisiones, cada una solo si hay estancamiento;
- las llamadas son secuenciales en la notebook;
- `programar` imprime cada intento, cada revisión (estado, tokens y segundos), la mejor versión y la verificación
  central.

**Dónde mirar:** `$C/taller/missions/<misión>/intento-N/revision.json` (qué vio y qué dijo el revisor) y
`intento-N/retroalimentacion.md` (qué recibió el programador).

**Para comparar con la experiencia 3:** el programador es el mismo y el coordinador también (`090a9ef` más el
revisor). La única diferencia es la intervención del revisor ante el estancamiento.

## Revisión causal (quinta experiencia)

**Cuarta experiencia real** (`d118dae`, con revisor):
- intentos: 0/9, 8/9, 8/9 y 8/9;
- el revisor intervino antes del intento 4 y la verificación central rechazó;
- el revisor señaló el problema solo en parte, con 4 observaciones de distinto peso;
- el programador conservó el defecto: el código busca el id anulado en una estructura que registra ingresos y
  egresos pero **no** las anulaciones aplicadas, así que devuelve `anulacion_inexistente` antes de llegar a
  `anulacion_de_anulacion`.

### Auditoría: ¿el programador usa las observaciones o reescribe desde cero?

Hay un problema de arquitectura más profundo, que **no** se arregló ahora porque implica cambiar el núcleo (se
propone para discutir):

1. **El programador reescribe el archivo completo en cada intento.** El prompt de sistema del motor
   (`prompts.BUILDER_SYSTEM`: «Escribes el archivo completo solucion.py») y el cierre del pedido de reparación
   («Corrige el programa y devuelve el archivo completo») no tienen un modo de edición acotada. El modelo vuelve a
   construir el programa desde su propia concepción, y un defecto de diseño (qué guarda una estructura) se reintroduce
   aunque el diagnóstico sea correcto.
2. **El diagnóstico quedaba enterrado.** Se agregaba dentro de la sección «Resultados reales de las pruebas (errores a
   corregir)» de `prompts.repair`, mezclado con el informe de pruebas, y sin pedir nada concreto.
3. **Con 4 intentos, el revisor llega tarde.** El criterio de estancamiento recién puede actuar antes del intento 3.
   En la corrida real (0/9 → 8/9 era una mejora) actuó antes del 4: el programador tuvo **un** solo intento con la
   revisión.

**Propuestas de cambio mayor, para decidir:**
- un pedido de reparación propio de la oficina, que pida editar lo señalado y conservar el resto, o directamente un
  parche;
- 5 o 6 intentos en las misiones con revisor.

### Mejoras implementadas (generales; ninguna regla de una misión)

1. **Revisión causal** (`SISTEMA_REVISOR` nuevo). El revisor tiene que:
   - elegir **una hipótesis principal**, fundada en una regla del contrato;
   - **citar literalmente** las condiciones o líneas del código (se le pasa numerado);
   - describir la **secuencia causal**: condición evaluada → estado que falta o es incorrecto → rama que no se
     alcanza;
   - indicar la **propiedad del código que tiene que cambiar**, sin escribir código;
   - separar hechos de hipótesis, con a lo sumo 2 hipótesis secundarias.
2. **Hechos comprobados por el coordinador:** cada cita se busca literalmente en el código candidato (ignorando
   espacios repetidos). Si está, es un **HECHO**; si no, se muestra como hipótesis. Una «cita» de más de 2 líneas o con
   un bloque de código se descarta, igual que todo código del revisor.
3. **Bloque separado del informe de pruebas:** el diagnóstico llega después del informe, entre `════ DIAGNÓSTICO DEL
   REVISOR — rol distinto; NO es un resultado de pruebas ════` y `════ FIN DEL DIAGNÓSTICO DEL REVISOR ════`. Contiene
   la hipótesis principal, los hechos citados, la secuencia causal, la rama que no se alcanza, la **propiedad a
   cambiar** y la instrucción: «partí de tu versión anterior, ubicá las líneas citadas y cambiá esa propiedad
   conservando lo que ya funciona».
4. **Medición de uso:**
   - **similitud con su base:** cada intento registra cuánto se parece a su punto de partida (`difflib`, 0 a 1), con
     tres etiquetas: «edición acotada» (≥ 0,85), «cambios amplios» (≥ 0,5) o «reescritura». Queda en el historial de
     `mejor/resumen.json`;
   - **efecto de cada revisión:** queda en `revision.json` con el puntaje siguiente, la similitud, las citas que siguen
     iguales, los casos que persisten y la atención: «atendida» si cambió al menos una línea citada, «no atendida» si
     no cambió ninguna;
   - `programar` imprime el efecto.
5. **Seguimiento:** si el intento siguiente no tocó las líneas citadas y sigue fallando, el próximo pedido lleva un
   aviso del **coordinador** (no del revisor): «tu intento N no cambió las líneas que señaló el revisor (…) y los
   casos … siguen fallando. La propiedad que el revisor pidió cambiar era: …».

**Sin cambios:**
- el revisor no recibe valores esperados ni entradas ocultas;
- los límites: 2 revisiones, 1.200 s, 1.024 tokens, contexto;
- el sandbox, la mejor versión, la verificación central y `engine.py` / `prompts.py`.

Pruebas: `tests/test_oficina_revisor.py`, ahora 13, con un Ollama falso y el sandbox real. Las nuevas o reescritas:
- **Citas:** las literales se comprueban como hechos, aunque traigan número de línea; las inventadas quedan como
  hipótesis; las que son código se descartan.
- **Formato del bloque:** delimitado, hechos y secuencia causal, rama, propiedad y secundarias.
- **Respuesta inválida:** sin secuencia de 2 pasos o sin propiedad, se rechaza.
- **Ciclo completo:** el diagnóstico va después del informe, sin mezclarse. La revisión queda «atendida» (la línea
  citada cambió) y el programa aprueba 9/9.
- **Diagnóstico ignorado:** el programador devuelve casi el mismo código (similitud > 0,95), la revisión queda «no
  atendida», con las dos citas iguales y el caso 6 persistente, y el siguiente pedido trae el SEGUIMIENTO DEL
  COORDINADOR.
- **Generalidad:** el mismo mecanismo funciona sobre `resumen_ventas`, con un error de «hasta» exclusivo citado y
  corregido.

Suite completa: 587 pruebas, OK (675 s).

### Quinta experiencia real (VivoBook contra Gemma 12B en rampern)

```bash
cd <worktree> && git pull --ff-only
python3 -m unittest tests.test_oficina_revisor tests.test_oficina_autocorreccion tests.test_oficina_conciliar \
    tests.test_oficina_programacion                                   # 39 pruebas, sin inferencias
C=~/constructor-ia-piloto/data/oficina-programacion-5               # carpeta NUEVA; las anteriores quedan intactas
python3 -m constructor_ia oficina registrar --carpeta $C --id pulenta --url http://<IP o .ts.net>:11435 \
    --modelo gemma4:12b --nodo rampern --gpu "RTX 4070" --vram-gb 8 --ram-gb 32
python3 -m constructor_ia oficina comprobar-programacion --carpeta $C --trabajador pulenta --mision conciliar_movimientos
python3 -m constructor_ia oficina encolar-programacion --carpeta $C --mision conciliar_movimientos
python3 -m constructor_ia oficina programar --carpeta $C --trabajador pulenta --mision conciliar_movimientos \
    --autorizo-inferencia-remota --revisor
```

**Qué mirar:**
- `revision.json`: citas verificadas, secuencia causal, propiedad y `efecto` (atendida o no, similitud);
- el historial de `mejor/resumen.json`: `tipo_de_cambio` de cada intento, para ver si edita o reescribe;
- `retroalimentacion.md`: el bloque delimitado y, si corresponde, el seguimiento.

## Experimento 6: hipótesis verificables (implementado, SIN inferencias; opcional)

**Contexto:** en la quinta experiencia, `conciliar_movimientos` quedó en 8/9 durante cuatro intentos. El revisor
intervino dos veces, pero sus diagnósticos no fueron atendidos ni identificaron bien la causa (confundir un
movimiento inexistente con una anulación aplicada).

**Idea:** el revisor no solo señala, también **propone un caso sintético mínimo**, construido solo desde el contrato
público, donde su hipótesis predice una discrepancia. El coordinador lo **contrasta** en el sandbox. Se activa con
`programar … --revisor --hipotesis-verificables`; sin la opción, todo es igual que antes.

### Circuito (`oficina/contraste.py`)

1. **Pedido al revisor:** el prompt agrega (`SISTEMA_CASO`) el campo `caso_sintetico` con `args`, `kwargs` y
   `que_deberia_pasar`. Recibe lo mismo que antes: contrato, código numerado y feedback autorizado. Nada de los casos
   ocultos.
2. **Validez del caso:**
   - forma JSON y tamaño máximo;
   - estructura según el contrato, con un validador por misión en `misiones_programacion.VALIDADORES`, derivado del
     contrato sin modificarlo: claves exactas de cada movimiento, fechas `AAAA-MM-DD`, argumentos permitidos y a lo
     sumo 12 elementos.
3. **Salvaguarda:** si la entrada coincide **exactamente** con la de un caso oculto, no se ejecuta ni se revela nada,
   y queda inconclusa.
4. **Contraste en el sandbox** (Bubblewrap, seccomp y los mismos límites):
   - primero con la **implementación de referencia autorizada**: si no procesa el caso, el caso queda fuera del
     contrato (inconclusa), y su salida es lo que el contrato pide;
   - después con el **código del programador** (el punto de partida del próximo intento).
5. **Clasificación** (campos separados desde la estabilización, ver más abajo):
   - **discrepancia demostrada:** el programa devuelve algo distinto (o lanza una excepción). Demuestra que el
     programa se comporta mal **en ese caso**; la explicación causal del revisor queda «compatible, no demostrada»;
   - **discrepancia no observada:** devuelve lo mismo; la hipótesis no tiene respaldo en el caso y no se presenta
     como un defecto;
   - **inconclusa:** caso inválido, privado, salida demasiado grande o fallo del sandbox.
6. **Lo que recibe el programador:**
   - el diagnóstico del revisor, con una línea de estado que distingue el comportamiento demostrado de la causa
     (que sigue siendo una hipótesis), o «SIN CONTRASTAR»;
   - si hay discrepancia demostrada, un bloque aparte, **«EVIDENCIA VERIFICADA POR EL COORDINADOR (caso sintético del
     revisor; NO es una prueba oculta)»**, con el caso, lo que pide el contrato (salida de la referencia), lo que
     devolvió su código, las claves distintas, «DEMOSTRADO: en esta entrada tu código no cumple el contrato» y
     «NO DEMOSTRADO: la causa», con la propiedad que propone el revisor.

   El caso sintético es público (lo escribió el revisor) y nunca reemplaza a la evaluación.
7. **Evaluación:** el evaluador central sigue ejecutando la **batería original de nueve casos**, sin cambios.

### Métricas de cada revisión (`intento-N/revision.json`)

| Métrica | Dónde |
|---|---|
| hipótesis propuesta | `contraste.hipotesis` y `diagnostico.principal` |
| caso sintético | `contraste.caso` |
| validez según el contrato | `contraste.validez` (válido o no, y motivo) |
| ejecución y salida de la referencia y del programa | `contraste.referencia` y `contraste.candidato` (`ejecucion`, `salida`, `detalle`) |
| discrepancia entre referencia y programa | `contraste.discrepancia` (demostrada, no_observada, inconclusa) y `contraste.comportamiento_candidato` |
| validez de la hipótesis causal | `contraste.hipotesis_causal` (compatible_no_demostrada, sin_respaldo_en_el_caso, sin_evaluar) y `contraste.motivo` |
| ¿se modificó la propiedad señalada? (atención textual) | `efecto.atencion_textual` (cambió líneas citadas, no cambió las líneas citadas, sin citas verificadas) y `efecto.tipo_de_cambio` |
| ¿se resolvió el problema? (resolución funcional) | `efecto.resolucion_funcional`, que resume las dos filas siguientes |
| ¿la corrección eliminó la discrepancia sintética? | `efecto.discrepancia_sintetica`: eliminada, persiste o inconclusa (se vuelve a contrastar el intento siguiente) |
| ¿mejoró, se mantuvo o empeoró en los casos originales? | `efecto.originales`, con `aprobados_base` y `aprobados` |

`programar` imprime el resultado del contraste y el efecto de cada revisión.

### Límites y riesgos

- Un caso sintético por revisión y las mismas 2 revisiones por misión: a lo sumo dos consultas a la referencia
  por misión, cada una sobre un caso mínimo escrito por el revisor.
- La referencia de una misión le dice al programador la respuesta correcta para **ese** caso público. Es un oráculo
  acotado, no un acceso a las pruebas ocultas. Por eso existen la salvaguarda de coincidencia exacta y el tamaño
  máximo. Una coincidencia **parcial** con un caso oculto no se detecta, y está permitida: el revisor nunca vio esos
  datos.
- Validez: el validador comprueba la forma, y la referencia comprueba que el caso se pueda procesar. Si una misión
  no tiene validador, el contraste no se activa.
- La salida comparable está limitada a lo que reporta el arnés (400 caracteres). Una salida más grande deja el
  contraste inconcluso.

Pruebas: `tests/test_oficina_hipotesis.py` (11), con revisores y programadores simulados y el sandbox real.
- **Contraste:**
  - discrepancia real → confirmada (anulación de una anulación: contrato `anulacion_de_anulacion`, programa
    `anulacion_inexistente`);
  - sin discrepancia → refutada, también con la referencia;
  - 7 casos fuera del contrato → inconclusos y nunca ejecutados con el programa;
  - una entrada igual a un caso oculto no se ejecuta ni se revela;
  - una referencia que no procesa el caso, o una salida demasiado grande → inconclusa;
  - un programa que lanza una excepción → confirmada.
- **Ciclo:**
  - una hipótesis confirmada llega como evidencia separada y el pedido al revisor no tiene datos ocultos. El
    programador corrige: atendida, discrepancia eliminada, originales que mejoran a 9/9, y la verificación central
    la acepta con la batería original;
  - una hipótesis refutada se presenta como «NO CONFIRMADA» y sin evidencia;
  - un caso inválido queda «SIN CONTRASTAR»;
  - una discrepancia ignorada se mide (no atendida, persiste, se mantuvo) y se conserva la mejor versión;
  - sin la opción, nada cambia.

Suite completa: 598 pruebas, OK (656 s).

### Sexta experiencia (cuando la autoricen)

```bash
cd <worktree> && git pull --ff-only
python3 -m unittest tests.test_oficina_hipotesis tests.test_oficina_revisor tests.test_oficina_autocorreccion \
    tests.test_oficina_conciliar tests.test_oficina_programacion       # 50 pruebas, sin inferencias
C=~/constructor-ia-piloto/data/oficina-programacion-6
python3 -m constructor_ia oficina registrar --carpeta $C --id pulenta --url http://<IP o .ts.net>:11435 \
    --modelo gemma4:12b --nodo rampern --gpu "RTX 4070" --vram-gb 8 --ram-gb 32
python3 -m constructor_ia oficina comprobar-programacion --carpeta $C --trabajador pulenta --mision conciliar_movimientos
python3 -m constructor_ia oficina encolar-programacion --carpeta $C --mision conciliar_movimientos
python3 -m constructor_ia oficina programar --carpeta $C --trabajador pulenta --mision conciliar_movimientos \
    --autorizo-inferencia-remota --revisor --hipotesis-verificables
```

## Estabilización del experimento 6 (implementado, SIN inferencias)

La sexta experiencia real (`b53656b`) mostró tres problemas. Ninguno cambia contratos, pruebas ocultas, sandbox,
roles, modelos ni límites de tokens; sin `--revisor` todo funciona como antes.

### 1. Código largo y respuestas cortadas

**Síntoma:** Gemma gastaba tokens en comentarios especulativos, y el tercer intento se cortó a 2048 tokens con un
`elif` incompleto.

**Corrección** (`oficina/autocorreccion.py`):
- **Reglas de concisión:** solo en los pedidos al programador (el revisor conserva su prompt), el sistema agrega
  «El código va SIN comentarios…» y el límite real de tokens de la respuesta (`num_predict`).
- **Punto de partida sin comentarios:** el código que se le devuelve al programador pierde los comentarios de línea
  completa. Se usa `tokenize`, así que no se tocan los strings ni los comentarios al final de una línea, y un código
  que no tokeniza se deja igual. El `solucion.py` histórico de cada intento queda intacto.
- **Respuestas cortadas:** se registra `done_reason` en el intento (`truncada` si es `length`). Un intento que no se
  pudo ejecutar (cortado, sin código o sin cargar) **nunca** es la mejor versión ni el punto de partida:
  - el siguiente pedido avisa «Tu intento N SE CORTÓ al llegar al límite de … tokens» y parte de la mejor versión
    ejecutada («el punto de partida es el intento K (p de t)»);
  - si no hay ninguna versión ejecutada, vuelve al pedido inicial del contrato.

### 2. Contraste: ejecución, salida, discrepancia y causa, por separado

**Síntoma:** la referencia producía la salida correcta, pero quedaba registrada como `estado: fallo` y `ok: false`.
El arnés siempre compara contra una expectativa y en el contraste no hay ninguna.

**Corrección** (`oficina/contraste.py`, `ejecutar`):
- Cada ejecución registra solo `ejecucion` (completa, excepcion, timeout, error_de_carga…), `salida` y `detalle`. El
  veredicto del arnés se ignora, porque se le pasa una expectativa centinela.
- El resultado separa:
  - `validez` del caso;
  - `referencia` y `candidato`, cada uno con su ejecución y su salida;
  - `discrepancia`;
  - `comportamiento_candidato`;
  - `hipotesis_causal`.
- Una discrepancia demostrada prueba el comportamiento incorrecto del programa en ese caso, **no** la explicación del
  revisor, que queda `compatible_no_demostrada`.
- El texto al programador también lo separa: «DEMOSTRADO…» y «NO DEMOSTRADO: la causa…».

### 3. Atención textual ≠ resolución funcional

**Síntoma:** la segunda revisión quedó «no atendida» aunque el programa pasó de un error de sintaxis a 6/9 y eliminó
la discrepancia sintética.

**Corrección:**
- `efecto.atencion_textual` mide solo si cambiaron las líneas citadas.
- `efecto.resolucion_funcional` mide lo que importa: la discrepancia sintética (eliminada, persiste o inconclusa) y
  los casos originales (mejoró, se mantuvo o empeoró).
- La similitud se calcula contra el punto de partida que realmente se envió.
- El seguimiento del coordinador solo insiste si las líneas siguen iguales **y** la discrepancia no se eliminó **y**
  los originales no mejoraron.
- `programar` imprime ambas métricas.

### Pruebas

`tests/test_oficina_estabilizacion.py` (8), sin inferencias, con Ollama falso y sandbox real:
- quitar comentarios solo de línea completa;
- las reglas de concisión y el límite de tokens solo en el pedido al programador;
- el punto de partida sin comentarios, con el histórico intacto;
- un intento cortado (`done_reason: length`, `elif` incompleto) no es ni base ni mejor versión;
- todo cortado → vuelve al contrato;
- una reparación por otro camino: no cambió las líneas citadas, pero discrepancia eliminada y originales mejoró;
- seguimiento solo mientras el problema funcional persiste;
- evidencia que separa el comportamiento de la causa.

`test_oficina_hipotesis.py` y `test_oficina_revisor.py` se adaptaron a los campos nuevos. Las ejecuciones de la
referencia ya no tienen `ok`, `estado` ni `got`.

Suite completa: 606 pruebas, OK (667 s).

### Repetir la sexta experiencia (cuando la autoricen)

Mismos comandos que arriba, en una carpeta nueva (por ejemplo `oficina-programacion-6b`). Antes, las pruebas sin
inferencias:

```bash
python3 -m unittest tests.test_oficina_estabilizacion tests.test_oficina_hipotesis tests.test_oficina_revisor \
    tests.test_oficina_autocorreccion tests.test_oficina_conciliar tests.test_oficina_programacion   # 58 pruebas
```

## Experimento 7: revisión autónoma de una solución existente (implementado, SIN inferencias)

**Objetivo:** demostrar que el revisor puede descubrir **por su cuenta** un defecto funcional con una hipótesis
verificable, y que el programador puede corregirlo con ese diagnóstico.

**Diferencia con los experimentos anteriores:** el revisor no espera un estancamiento ni recibe resultados de
pruebas. Revisa un código existente solo con el contrato público.

Es una modalidad aparte (`oficina revisar-existente`, módulo `oficina/revision_existente.py`). `oficina programar`,
el motor, los roles, los modelos, el sandbox y las pruebas ocultas no cambian.

### Recorrido

0. **Preparación, sin inferencias ni escritura:**
   - **Solución inicial:** un archivo Python existente (`--codigo`) o la referencia de la misión.
   - **Defecto controlado opcional** (`--defecto`, catálogo `DEFECTOS`): un reemplazo textual exacto, que tiene que
     aparecer una sola vez.
   - **Limpieza:** se quitan los comentarios de línea completa.
   - **Comprobación:** el coordinador ejecuta la referencia y la solución inicial contra la batería completa.
     - Si la solución inicial aprueba todo, o no carga, no hay un defecto funcional verificable y no se gasta
       ninguna inferencia.
     - El nombre y la explicación del defecto, y el resultado de esa batería, quedan solo en el registro.
1. **Revisor:** el mismo rol, con un pedido autónomo (`revisor.SISTEMA_REVISOR_AUTONOMO` y `mensajes_autonomos`).
   - **Recibe:** el contrato, la firma, los ejemplos visibles y el código numerado. Ningún resultado de pruebas.
   - **Devuelve:** la hipótesis causal de siempre, con citas que el coordinador comprueba, y un caso sintético
     **obligatorio**.
2. **Contraste:** el mismo de la estabilización.
   - **Validación:** el caso se valida contra el contrato. Si coincide con la entrada de un caso oculto, no se
     ejecuta.
   - **Ejecución:** se corre en el sandbox con la referencia y con el candidato.
   - **Resultado:** queda registrado por separado:
     - la ejecución y la salida de cada uno;
     - la discrepancia (demostrada, no observada o inconclusa);
     - la hipótesis causal, que con una discrepancia queda «compatible, no demostrada».
   - **Si no hay discrepancia:** el revisor puede volver a intentar (`--max-revisiones`, 2 por omisión). Recibe una
     nota que dice solo qué pasó con **su** caso. Un caso igual a uno oculto se informa como «elegí otra entrada»,
     sin decir por qué.
3. **Programador:** solo si la discrepancia quedó demostrada.
   - **Recibe:** el contrato, los ejemplos visibles, el código, el diagnóstico y la evidencia («DEMOSTRADO… / NO
     DEMOSTRADO: la causa…»), con las reglas de concisión. Ningún dato de la batería.
   - **Intentos:** hasta `--max-reparaciones` (3).
   - **Entre intentos, solo evidencia pertinente:**
     - su respuesta cortada o inválida;
     - el caso sintético de nuevo, con lo que devolvió;
     - el informe de los ejemplos visibles, que son públicos.
   - **Punto de partida:** el último intento que se pudo ejecutar.
4. **Verificación independiente:** cuando un intento elimina la discrepancia y cumple los ejemplos visibles, el
   coordinador lo ejecuta **una sola vez** contra la batería completa. Ese resultado nunca vuelve a un modelo.

### Defectos controlados (`--defecto`)

| Misión | Defecto | Qué rompe (no llega a los modelos) |
|---|---|---|
| conciliar_movimientos | `anulaciones_no_registradas` | anular una anulación da `anulacion_inexistente` |
| conciliar_movimientos | `orden_solo_por_fecha` | dentro del mismo día manda el orden de la lista, no el id |
| conciliar_movimientos | `saldo_cero_rechazado` | un saldo que llega exactamente a 0 se rechaza |
| resumen_ventas | `hasta_excluido` | el día «hasta» queda afuera |
| resumen_ventas | `desempate_invertido` | en un empate gana el último alfabéticamente |

Todos pasan los ejemplos visibles, fallan la batería y tienen un caso mínimo del contrato que los demuestra (lo
comprueban las pruebas).

### Criterios de éxito (`informe.json`, `criterios`)

1. `1_defecto_funcional_verificable`: la solución inicial carga y falla la batería independiente.
2. `2_caso_reproduce_discrepancia_real`: un caso del revisor demostró una discrepancia con la referencia.
3. `3_programador_modifico_la_solucion`: un intento devolvió código distinto de su punto de partida (sin contar los
   comentarios).
4. `4_discrepancia_eliminada_y_verificacion_independiente`: el intento verificado elimina la discrepancia y aprueba
   la batería completa.

`exito` exige los cuatro criterios **y** una auditoría de fugas limpia. La auditoría busca, en cada pedido enviado a
un modelo:
- el nombre y la explicación del defecto;
- las entradas, las salidas esperadas y las categorías de los casos ocultos.

Un dato oculto que también es público (por ejemplo, igual a un ejemplo visible) no cuenta.

### Registro (`<carpeta>/experimento-7/corrida-<fecha>/`)

- **`inicial/`:** `solucion.py` y `preparacion.json`, con el origen, las huellas, el defecto y la batería inicial.
- **`revision-N/`:** `pedido.json`, `respuesta.txt` y `revision.json`, con el diagnóstico y el contraste.
- **`reparacion-N/`:** `pedido.json`, `respuesta.md`, `solucion.py` y `reparacion.json`, con el contraste nuevo, los
  ejemplos visibles y si modificó el código.
- **`final/solucion.py`.**
- **`informe.json`:** los límites, cada revisión y reparación, la verificación y los criterios. También el consumo:
  cada llamada con su rol, segundos, tokens de entrada y generados, motivo de fin y si se cortó, con totales por rol.

### Límites

- **Revisiones y reparaciones:** 2 y 3 por omisión.
- **Tiempo de inferencia:** `--max-segundos`, 2400 por omisión, revisado antes de cada llamada.
- **Tokens:** 2048 para el programador y 1024 para el revisor, con los mismos plazos por llamada que `programar`.
- **Sandbox:** el mismo de siempre.
- **Laboratorio aislado:** la carpeta no puede tener `taller.db` ni `taller/`, que son de los experimentos 1 a 6b.
  Cada corrida va a una subcarpeta nueva y nada se sobrescribe.

**Riesgo conocido:** la referencia responde el caso sintético del revisor, igual que en el experimento 6: es un
oráculo acotado a casos públicos y mínimos. La batería completa solo se usa al preparar y al verificar.

### Pruebas

`tests/test_oficina_revision_existente.py` (15), con revisor y programador simulados y el sandbox real:
- **Catálogo:** cada defecto pasa los ejemplos visibles, falla la batería y tiene un caso del contrato que lo
  demuestra.
- **Preparación:**
  - rechaza una solución sin defecto sin inferir, y también defectos desconocidos o inaplicables;
  - un programa que no carga no cuenta como defecto funcional;
  - un archivo existente entra sin sus comentarios.
- **Laboratorio:** nunca se reutiliza una carpeta anterior.
- **Recorrido completo:** éxito en los cuatro criterios.
  - El revisor recibe solo el contrato y el código.
  - El programador recibe el diagnóstico y la evidencia separada, sin la batería, sin el defecto y sin datos
    ocultos.
  - Quedan el registro y el consumo.
- **Revisor:**
  - una segunda oportunidad con la nota de su propio caso;
  - sin discrepancia demostrada, nunca se llama al programador;
  - un caso igual a uno oculto no se ejecuta ni se revela.
- **Reparación:**
  - una respuesta cortada y otra sin cambios reciben solo la evidencia pertinente;
  - eliminar la discrepancia no alcanza: la batería decide, una sola vez;
  - se respetan los límites de reparaciones y de tiempo.
- **Otras:** `resumen_ventas` con un defecto controlado; la auditoría de fugas detecta el defecto y los datos
  ocultos; el CLI prepara sin inferir ni escribir, corre con autorización y rechaza la carpeta de un experimento
  anterior sin tocarla.

Suite completa: 621 pruebas, OK (668 s).

### Ejecutar el experimento 7 (cuando lo autoricen)

```bash
cd <worktree> && git pull --ff-only
python3 -m unittest tests.test_oficina_revision_existente tests.test_oficina_estabilizacion \
    tests.test_oficina_hipotesis tests.test_oficina_revisor            # 47 pruebas, sin inferencias
L=~/constructor-ia-piloto/data/oficina-revision-7                     # laboratorio NUEVO (no el 6 ni el 6b)
python3 -m constructor_ia oficina registrar --carpeta $L --id pulenta --url http://<IP o .ts.net>:11435 \
    --modelo gemma4:12b --nodo rampern --gpu "RTX 4070" --vram-gb 8 --ram-gb 32
# 1) Preparación: comprueba trabajador, sandbox y defecto; NO infiere ni escribe la corrida.
python3 -m constructor_ia oficina revisar-existente --carpeta $L --trabajador pulenta \
    --mision conciliar_movimientos --defecto anulaciones_no_registradas
# 2) Experimento: a lo sumo 2 revisiones + 3 reparaciones (5 inferencias) y 2400 s de inferencia.
python3 -m constructor_ia oficina revisar-existente --carpeta $L --trabajador pulenta \
    --mision conciliar_movimientos --defecto anulaciones_no_registradas --autorizo-inferencia-remota
```

El resultado sale por pantalla:
- la verificación independiente;
- el consumo de cada llamada;
- los cuatro criterios;
- la auditoría de fugas;
- `Experimento 7: ÉXITO` o `SIN ÉXITO`, que también es el código de salida (0 o 1).

Todo el detalle queda en `$L/experimento-7/corrida-*/informe.json`. Para partir de un archivo propio, agregá
`--codigo archivo.py`; con `--defecto`, el defecto se aplica sobre ese archivo.

## Experimento 7.1: retroalimentación específica al programador (implementado, SIN inferencias)

**Corrida analizada:** `corrida-20261024-013359`, con Gemma 12B de revisor y Qwen 2.5 Coder 7B de programador.
- El revisor encontró el defecto (anulaciones no registradas) y el coordinador demostró la discrepancia.
- Qwen hizo tres reparaciones ejecutables y casi idénticas; ninguna eliminó la discrepancia.
- Entre intentos solo recibía «todavía no cumple», sin el resultado concreto de cada reparación.

**Cambio:** solo la retroalimentación entre intentos del programador en `oficina revisar-existente`. No cambian los
contratos, `oficina programar`, el revisor, el contraste ni la verificación.

### Qué recibe ahora tras una reparación ejecutable que no elimina la discrepancia

1. **El mismo caso sintético** se ejecuta otra vez sobre la nueva versión, en el sandbox.
2. **Resultado concreto** (bloque «RESULTADO DE TU INTENTO N EN EL CASO DEL REVISOR (caso sintético; NO es una
   prueba oculta)»):
   - la salida que pide el contrato y la salida real de su intento;
   - si lanzó una excepción o no terminó, lo dice.
3. **Claves distintas:** «SIGUE DISTINTA en 'inconsistencias'. Ya coinciden: …», con el detalle de cada clave:
   - en las listas, qué elementos **faltan** y cuáles **sobran**;
   - en los diccionarios, qué subclaves difieren;
   - en los demás valores, el del contrato y el suyo.
4. **Qué cambió:** el diff unificado de su código respecto de su punto de partida, sin comentarios ni líneas vacías
   («QUÉ CAMBIASTE respecto de la versión inicial (+2 / -0 líneas)…»), o «nada». Es solo descriptivo: no se le
   muestra la referencia ni se le propone una solución. La única guía es de método: seguir la entrada por su código
   y ver si las líneas cambiadas se ejecutan.
5. **Repeticiones:**
   - **«REPETICIÓN»:** el intento es el **mismo programa**, con el mismo árbol sintáctico, que una versión ya probada
     (solo cambian espacios o comentarios).
   - **«SIN EFECTO EN ESTE CASO»:** devuelve **la misma salida** que una versión ya probada, es decir, el cambio no
     afecta a esa entrada.
   - **Historial** («TUS REPARACIONES ANTERIORES»): una línea por intento, con el tamaño del cambio, las claves
     distintas y las repeticiones. Así cada pedido trae información nueva.
   - **Corte:** si después de un aviso de repetición vuelve a entregar un programa ya probado, el coordinador se
     detiene. Otro pedido no traería información nueva, así que no se gastan más inferencias.
6. **Separación:**
   - Todo sale del caso sintético (público), de los ejemplos visibles y del propio código del programador. La
     batería oculta solo se usa en la preparación y en la verificación final.
   - La auditoría de fugas sigue revisando cada pedido.
   - El bloque nuevo reemplaza a la evidencia de la versión inicial, y el pedido indica de qué versión parte
     («Versión actual de solucion.py (tu intento N)»).

### Registro

- **`reparacion-N/retroalimentacion.md`:** exactamente el bloque que recibió el pedido siguiente.
- **`reparacion-N/reparacion.json`:** suma estos campos:
  - `claves_distintas`;
  - `cambio` (líneas agregadas y quitadas, o sin cambios);
  - `mismo_programa_que`;
  - `misma_salida_que` (0 es la versión inicial).
- Los pedidos y las respuestas siguen en `pedido.json` y `respuesta.md`.

### Pruebas

`tests/test_oficina_retroalimentacion.py` (7), con revisor y programador simulados y el sandbox real:
- **Comparación de salidas:** clave por clave, con los elementos que faltan y sobran.
- **Programa y diff:** «mismo programa» ignora el formato y los comentarios, pero no la lógica; el diff lo refleja.
- **Excepciones:** una excepción se informa como salida no comparable.
- **Ciclo completo:** una reparación casi idéntica, como las de Qwen, recibe:
  - su salida real y la del contrato, clave por clave;
  - su diff;
  - «sin efecto en este caso»;
  - el historial.

  La siguiente, el mismo programa reformateado, recibe «REPETICIÓN». Ningún pedido se repite, y no hay datos
  ocultos ni el defecto.
- **Corte:** repetir después del aviso detiene la corrida sin otra inferencia.
- **Ejemplos visibles:** una reparación que arregla el caso pero rompe un ejemplo visible recibe solo evidencia
  pública.
- **Registro:** lo guardado es exactamente lo que se envió.

`test_oficina_revision_existente.py` se adaptó al texto nuevo.

Suite completa: 628 pruebas, OK (701 s).

### Repetir el experimento 7 con la retroalimentación nueva (laboratorio NUEVO)

```bash
cd <worktree> && git pull --ff-only
python3 -m unittest tests.test_oficina_retroalimentacion tests.test_oficina_revision_existente \
    tests.test_oficina_estabilizacion tests.test_oficina_hipotesis tests.test_oficina_revisor   # 54, sin inferencias
L=~/constructor-ia-piloto/data/oficina-revision-7-1                   # carpeta nueva: la corrida anterior no se toca
python3 -m constructor_ia oficina registrar --carpeta $L --id pulenta --url http://<IP o .ts.net>:11435 \
    --modelo gemma4:12b --modelo qwen2.5-coder:7b --nodo rampern --gpu "RTX 4070" --vram-gb 8 --ram-gb 32
python3 -m constructor_ia oficina revisar-existente --carpeta $L --trabajador pulenta \
    --mision conciliar_movimientos --defecto anulaciones_no_registradas \
    --modelo qwen2.5-coder:7b --revisor-modelo gemma4:12b                            # preparación, sin inferencias
python3 -m constructor_ia oficina revisar-existente --carpeta $L --trabajador pulenta \
    --mision conciliar_movimientos --defecto anulaciones_no_registradas \
    --modelo qwen2.5-coder:7b --revisor-modelo gemma4:12b --autorizo-inferencia-remota
```

Para comparar con la corrida anterior, mirá `reparacion-N/retroalimentacion.md` y `reparacion.json`
(`claves_distintas`, `cambio`, `mismo_programa_que`, `misma_salida_que`) en
`$L/experimento-7/corrida-*/`. Usá en `--modelo` el nombre exacto con el que Qwen figura en `ollama list` del
trabajador.

## Experimento 7.2: diagnóstico previo a la reparación (implementado, SIN inferencias)

**Contexto:** en el 7.1, Gemma encontró el defecto, pero Qwen 2.5 Coder 7B devolvió dos veces el programa original,
aun con evidencia concreta. **Hipótesis:** separar el razonamiento causal de la generación de código mejora la
reparación.

**Variante seleccionable:** `oficina revisar-existente … --variante 7.2`. Por omisión es `7.1`, que funciona
exactamente igual que antes (lo comprueban las pruebas). `oficina programar` no cambia.

### Qué se mantiene constante (para comparar arquitecturas)

- Los modelos, `num_ctx`, `num_predict`, plazos y temperatura.
- El defecto, el revisor, el contraste, la verificación final y los criterios de éxito.
- El texto del pedido de reparación: en 7.2 es el mismo pedido del 7.1 más un bloque con el diagnóstico propio. Ningún
  prompt existente se modificó.
- `--max-reparaciones`: cada iteración A+B cuenta como **un** intento, no se agregan intentos para compensar la etapa
  nueva.
- `--max-segundos`: las dos inferencias de cada iteración entran en el mismo presupuesto, revisado antes de A y otra
  vez antes de B.

### Etapas de cada intento

- **A. Diagnóstico** (`SISTEMA_DIAGNOSTICO_PROGRAMADOR`, salida JSON):
  - **Recibe** el **mismo material** que el pedido de reparación del 7.1 (contrato, ejemplos visibles, código actual,
    diagnóstico del revisor, evidencia del caso o resultado de su último intento, historial y avisos). Solo cambian
    el sistema y la última línea: «Hacé tu diagnóstico y respondé solo con el JSON pedido, sin código».
  - **Responde:** `causa`, `elementos` (variables, estructuras o condiciones copiadas del código),
    `modificacion`, `justificacion` y `efectos_secundarios`.
  - **Validación** (`interpretar_diagnostico`):
    - los cinco campos son obligatorios, con textos no vacíos y al menos un elemento;
    - si incluye el programa (`def <función>(`), es inválido;
    - se quitan los bloques de código;
    - cada elemento se comprueba contra el código (`en_el_codigo`), igual que las citas del revisor.
  - **Si es inválido**, no se reintenta ni se suma un intento: la etapa B sigue con «Tu diagnóstico no se pudo usar
    (motivo)».
- **B. Reparación:** el pedido del 7.1 (las reglas de concisión de siempre) más el bloque «TU DIAGNÓSTICO (etapa
  previa; lo escribiste vos y el coordinador no lo verificó)», con causa, elementos (marcando los que no están en el
  código), modificación, justificación y efectos secundarios. El coordinador ejecuta, contrasta y verifica como antes.
- **C. Retroalimentación:** la del 7.1 sin cambios:
  - salida esperada y obtenida, clave por clave;
  - diff entre versiones;
  - mismo programa o misma salida;
  - historial;
  - corte cuando no aparece información nueva.

  El resultado alimenta el diagnóstico (A) del intento siguiente.

### Información y registro

- **Sin filtraciones:** ni casos ocultos, ni referencia, ni defecto. La auditoría revisa también los pedidos de
  diagnóstico.
- **Archivos por intento** (`reparacion-N/`): `diagnostico_pedido.json`, `diagnostico_respuesta.txt` y
  `diagnostico.json` (validez, motivo y campos), además de `pedido.json`, `respuesta.md`, `solucion.py`,
  `reparacion.json` (con `diagnostico_propio`) y `retroalimentacion.md`.
- **`informe.json`:**
  - `variante` (id y descripción);
  - cada llamada con `etapa` (`diagnostico` o `reparacion`), tokens, segundos y motivo de fin;
  - `consumo.programador_por_etapa`, con los totales separados.

### Pruebas

`tests/test_oficina_diagnostico_previo.py` (11), sin inferencias, con revisor y programador simulados y el sandbox
real:
- **Diagnósticos:** uno válido se sanea y sus elementos se comprueban contra el código. Los inválidos o
  incompletos se rechazan con su motivo: sin JSON, faltan campos, causa vacía, sin elementos, efectos que no son una
  lista, o con el programa adentro.
- **Mismo material:** la etapa A recibe el mismo material que la B, y sin la variante el pedido es exactamente el del
  7.1.
- **Diagnóstico válido y reparación válida:**
  - la etapa B es el pedido del 7.1 más el diagnóstico;
  - es un solo intento;
  - el registro y el consumo quedan separados por etapa;
  - no hay filtraciones.
- **Diagnóstico inválido:** no suma intentos y la reparación sigue con el motivo.
- **Reparación que no elimina la discrepancia:** su resultado alimenta el diagnóstico siguiente.
- **Repetición del programa:** corta como en el 7.1.
- **Presupuesto:** el diagnóstico cuenta dentro del presupuesto, que se agota entre A y B.
- **Compatibilidad:** la variante por omisión se comporta como el 7.1.
- **CLI:** `--variante 7.2`, identificada en pantalla y en el informe.

Suite completa: 638 pruebas, OK (718 s).

### Ejecutar el experimento 7.2 (carpeta NUEVA; mismos modelos, defecto y límites que el 7.1)

```bash
cd <worktree> && git pull --ff-only
python3 -m unittest tests.test_oficina_diagnostico_previo tests.test_oficina_retroalimentacion \
    tests.test_oficina_revision_existente                                # 33 pruebas, sin inferencias
L=~/constructor-ia-piloto/data/oficina-revision-7-2
python3 -m constructor_ia oficina registrar --carpeta $L --id pulenta --url http://<IP o .ts.net>:11435 \
    --modelo gemma4:12b --modelo qwen2.5-coder:7b --nodo rampern --gpu "RTX 4070" --vram-gb 8 --ram-gb 32
python3 -m constructor_ia oficina revisar-existente --carpeta $L --trabajador pulenta \
    --mision conciliar_movimientos --defecto anulaciones_no_registradas \
    --modelo qwen2.5-coder:7b --revisor-modelo gemma4:12b --variante 7.2      # preparación, sin inferencias
python3 -m constructor_ia oficina revisar-existente --carpeta $L --trabajador pulenta \
    --mision conciliar_movimientos --defecto anulaciones_no_registradas \
    --modelo qwen2.5-coder:7b --revisor-modelo gemma4:12b --variante 7.2 --autorizo-inferencia-remota
```

Con los límites por omisión (2 revisiones, 3 reparaciones, 2400 s), la variante 7.2 hace a lo sumo 2 + 3 × 2 = 8
inferencias, contra 5 en el 7.1, dentro del mismo presupuesto de tiempo.

## Experimento 7.3: reparación mediante parches mínimos (implementado, SIN inferencias)

**Contexto:** en el 7.1 y el 7.2, Qwen 2.5 Coder 7B no reparó `conciliar_movimientos`, aunque Gemma encontró el
defecto. En una prueba directa, con el defecto localizado y un pedido de parche mínimo, Qwen propuso registrar las
anulaciones válidas en `aplicados`.

**Pregunta:** ¿puede un modelo pequeño reparar con modificaciones localizadas en lugar de regenerar el archivo?

**Variante:** `--variante 7.3`. Las variantes 7.1 (por omisión) y 7.2 no cambian, y `oficina programar` tampoco.

**Lo único que cambia es la forma de reparar:**
- **Iguales al 7.1:** los modelos, `num_ctx`, `num_predict`, temperatura, plazos, misión, defecto, revisor,
  contraste, retroalimentación (diff, mismo programa o salida, historial, corte), verificación independiente,
  criterios, auditoría, `--max-reparaciones` y `--max-segundos`.
- **Un parche por intento:** cada parche es una inferencia y un intento de reparación.

### Recorrido

1. **Revisor y contraste:** como en el 7.1.
2. **Localización** (`oficina/parches.py`, `localizar`). La hace el coordinador, sin pruebas ocultas, con:
   - **anclas permitidas:**
     - las citas del revisor que el coordinador comprobó en el código;
     - las líneas con literales de texto que aparecen en la diferencia del caso sintético, sea en la salida que pide
       el contrato o en la del candidato (por ejemplo `"anulacion_inexistente"`);
   - **análisis estático** del árbol sintáctico: cada ancla se lleva a su sentencia. Si su bloque tiene menos de 3
     sentencias, se sube al que la contiene. La zona es esa sentencia con hasta 2 vecinas antes y después, como
     máximo 30 líneas por zona y 80 en total. Las zonas que se tocan se unen.
   - **Sin anclas**, la zona es la función completa, y queda registrado.
   - Se recalcula sobre el punto de partida de cada intento.
3. **Programador** (`SISTEMA_PARCHE`, salida JSON):
   - **Recibe** el mismo material que en el 7.1, pero en lugar del archivo ve:
     - las **zonas autorizadas** numeradas;
     - un contexto de **solo lectura**: la firma y las asignaciones de primer nivel de la función.
   - **Responde** `{"cambios": [{"original", "reemplazo"}], "justificacion"}`, con 3 cambios como máximo.
4. **Validación y aplicación determinista** (`interpretar_parche`, `aplicar`). Ninguna instrucción del modelo se
   ejecuta: el parche es un dato y la única operación es reemplazar líneas en una **copia** del código. Se rechaza,
   con el motivo, un parche que:
   - no es JSON;
   - no tiene cambios o tiene más de 3;
   - tiene un original vacío;
   - tiene un original que no aparece (se comparan líneas completas, ignorando solo los espacios finales) o aparece
     varias veces;
   - cambia líneas fuera de las zonas autorizadas;
   - tiene cambios superpuestos;
   - tiene un reemplazo igual al original, de más de 25 líneas o que reescribe la función;
   - deja un resultado que no es Python válido o que ya no define la función.

   Un parche rechazado consume su intento y no produce código.
5. **Verificación:**
   - **Primero, lo autorizado:** el caso sintético y los ejemplos visibles.
   - **Después, la batería independiente:** si la discrepancia desaparece y los ejemplos visibles pasan, se ejecuta
     la batería completa una sola vez.
6. **Retroalimentación:** la del 7.1. Además, un parche **repetido** (misma huella, ignorando espacios finales) se
   marca con «REPETICIÓN». Si se repite después del aviso, el coordinador se detiene. Un parche rechazado deja «Tu
   parche fue rechazado: motivo».

### Registro (`reparacion-N/`)

- `pedido.json` y `respuesta.txt`;
- `parche.json`: el parche, las zonas, si se aplicó, las líneas tocadas y el motivo de un rechazo;
- `solucion.py`: el código parcheado, solo si se aplicó;
- `reparacion.json`: `localizacion`, con zonas, anclas y método; `parche`; `parche_repetido_de`; y los campos del
  7.1;
- `retroalimentacion.md`.

En `informe.json`, cada llamada del programador tiene `etapa: "parche"`.

### Criterio de éxito

Es el mismo de siempre: no basta con un parche distinto. El código parcheado tiene que eliminar la discrepancia **y**
aprobar la batería independiente completa.

### Pruebas

`tests/test_oficina_parches.py` (13), sin inferencias, con revisor y programador simulados y el sandbox real:
- **Localización:** con las citas comprobadas y los literales de la diferencia sintética; sin anclas, la función
  completa.
- **Parches válidos:**
  - quitar la condición, o registrar las anulaciones como propuso Qwen;
  - se aplican sobre una copia;
  - se reconocen como repetidos aunque cambien los espacios finales.
- **Parches inválidos**, con su motivo:
  - sin JSON o sin la lista «cambios»;
  - vacío, o con un original vacío;
  - original inexistente, o que aparece varias veces;
  - sintaxis inválida en el resultado;
  - fuera de alcance;
  - reemplazo igual al original;
  - cambios superpuestos;
  - más de 3 cambios;
  - reescritura de la función.
- **Ciclo:**
  - un parche válido aprueba 9/9 en la verificación independiente;
  - un parche rechazado recibe su motivo y consume un intento;
  - un parche sin efecto recibe la retroalimentación del 7.1 y zonas nuevas;
  - un parche repetido se detiene después del aviso, también si se repite sobre su propio resultado;
  - el presupuesto también limita los parches;
  - no hay filtraciones;
  - las variantes 7.1 y 7.2 no usan parches.
- **CLI:** `--variante 7.3`.

Suite completa: 651 pruebas, OK (747 s).

### Preflight y ejecución (carpeta NUEVA `oficina-revision-7-3`)

```bash
cd <worktree> && git pull --ff-only
python3 -m unittest tests.test_oficina_parches tests.test_oficina_diagnostico_previo \
    tests.test_oficina_retroalimentacion tests.test_oficina_revision_existente     # 46 pruebas, sin inferencias
L=~/constructor-ia-piloto/data/oficina-revision-7-3
python3 -m constructor_ia oficina registrar --carpeta $L --id pulenta --url http://<IP o .ts.net>:11435 \
    --modelo gemma4:12b --modelo qwen2.5-coder:7b --nodo rampern --gpu "RTX 4070" --vram-gb 8 --ram-gb 32
# preflight: trabajador, planificador, sandbox, referencia y defecto (sin inferencias, no escribe la corrida)
python3 -m constructor_ia oficina revisar-existente --carpeta $L --trabajador pulenta \
    --mision conciliar_movimientos --defecto anulaciones_no_registradas \
    --modelo qwen2.5-coder:7b --revisor-modelo gemma4:12b --variante 7.3
# ejecución: como máximo 2 revisiones + 3 parches (5 inferencias), 2400 s
python3 -m constructor_ia oficina revisar-existente --carpeta $L --trabajador pulenta \
    --mision conciliar_movimientos --defecto anulaciones_no_registradas \
    --modelo qwen2.5-coder:7b --revisor-modelo gemma4:12b --variante 7.3 --autorizo-inferencia-remota
```

## Experimento 7.3 — corrección A del localizador (citas multilínea)

**Origen:** en la corrida real `corrida-20261024-031338`, el coordinador autorizó solo las líneas 27–37, no la zona
41–46 (el registro en `aplicados`), aunque Gemma señalaba la línea 44.

**Defecto encontrado en `f66aab9`:** la verificación y la ubicación de las citas usaban criterios distintos.
- `revisor.interpretar` acepta citas de hasta 2 líneas y las verifica con `cita_en_codigo`, que normaliza el código
  **entero**.
- `parches.anclas` buscaba cada cita dentro de **una sola línea**.

Por eso una cita verificada de las líneas 44–45 se descartaba en silencio, y solo quedaban los literales de la
diferencia sintética (líneas 29 y 32 → zona 27–37).

**Corrección A** (lo único que cambia):
- **`parches.lineas_de_cita`:** ubica la cita con el mismo criterio de la verificación, en la ventana **mínima** de
  líneas consecutivas que la contiene (hasta 6). Una cita de varias líneas ancla todas sus líneas; una de una línea
  se comporta igual que antes.
- **`localizar`:** devuelve `citas`, un registro auditable de cada cita del revisor:
  - las verificadas, con sus líneas o con el motivo por el que no anclaron;
  - las no verificadas, como descartadas.
- **`revision_existente`:** pasa también las citas no verificadas, y guarda `localizacion.citas` en
  `reparacion-N/reparacion.json`.

**Lo que no cambia:** no hay relación entre lectura y escritura (B) ni referencias «línea N» del texto causal (C).
Los literales, `_zona`, los límites, los modelos y las demás variables siguen iguales.

**Pruebas:** `tests/test_oficina_localizador.py` (8). Contra el código de `f66aab9`, las dos que reproducen el
defecto fallan con `[(27, 37)] != [(27, 37), (41, 46)]`.
- **Cita verificada de dos líneas:** ahora ancla las líneas 44–45 y la zona 41–46.
- **Texto de una línea que abarca dos del código:** también se ubica.
- **Citas de una línea:** se comportan como antes.
- **Citas no verificadas, o que ya no están:** nunca anclan y quedan registradas con el motivo.
- **Mismo criterio:** la ubicación coincide con la verificación en todas las líneas y pares de líneas del código.
- **Sin B:** con solo los literales, la zona sigue siendo 27–37.
- **Recorrido simulado:** cita de dos líneas y parche estilo Qwen sobre la línea 43. Antes se rechazaba por caer
  fuera de zona; ahora se aplica y aprueba 9/9.

Suite completa: 659 pruebas, OK (744 s).

### Comprobar la corrida real (solo lectura, en la VivoBook)

```bash
cd <worktree>/constructor-ia && git pull --ff-only
C=~/constructor-ia-piloto/data/oficina-revision-7-3/experimento-7/corrida-20261024-031338
python3 - "$C" 2 <<'PY'
import json, sys
from pathlib import Path
from constructor_ia.oficina import parches as PA
c, nrev = Path(sys.argv[1]), sys.argv[2]
codigo = (c / "inicial/solucion.py").read_text()
rev = json.loads((c / f"revision-{nrev}/revision.json").read_text())
p = rev["diagnostico"]["principal"]
loc = json.loads((c / "reparacion-1/reparacion.json").read_text()).get("localizacion", {})
print("zonas registradas:", loc.get("zonas"), "· anclas:", [(a["linea"], a["origen"]) for a in loc.get("anclas", [])])
norm = lambda t: " ".join(t.split())
for x in p["citas"]:
    t = x["texto"]
    antes = [i for i, ln in enumerate(codigo.splitlines(), 1) if norm(t) and norm(t) in norm(ln)]
    nuevo = PA.lineas_de_cita(codigo, t) if x["verificada"] else "—"
    print(f"cita {t!r}\n   verificada={x['verificada']} · líneas con f66aab9={antes} · líneas con la corrección A={nuevo}")
for k, v in (("secuencia_causal", p["secuencia_causal"]), ("rama_inaccesible", p["rama_inaccesible"]),
             ("propiedad_a_cambiar", p["propiedad_a_cambiar"]), ("hechos", rev["diagnostico"]["hechos"])):
    s = json.dumps(v, ensure_ascii=False)
    if "aplicados" in s or "44" in s:
        print(f"menciona aplicados/44 en {k}: {s[:300]}")
PY
```

**Cómo leer el resultado:**
- **A explica la pérdida:** alguna cita está `verificada=True`, con `líneas con f66aab9=[]` y con 44 o 45 en `líneas
  con la corrección A`. Corresponde repetir 7.3 en una carpeta nueva.
- **A no la explica:** la línea 44 o `aplicados` aparecen solo en las líneas «menciona …», o en citas con
  `verificada=False`. Corresponde investigar B (o C) antes de repetir.

## Experimento 7.3 — corrección B del localizador (relaciones lectura → escritura)

**Verificación en la VivoBook** (corrida real `20261024-031338`, con `24d18a7`): la corrección A **no** explicaba la
pérdida de las líneas 41–46.
- Las únicas citas verificadas de Gemma eran las líneas 31 y 32, que ya anclaban en `f66aab9`.
- La relación causal estaba en `rama_inaccesible`: «`aplicados` solo guarda movimientos que no son anulaciones»
  (línea 44), y esa prosa no llegaba al localizador.

**Corrección B** (`parches.relaciones_lectura_escritura`, `escrituras`, `lecturas`; lo único que cambia):
1. **Lecturas:** en las zonas **ancladas** (citas y literales, después de los límites) se buscan las variables que
   se **leen**, es decir, nombres en contexto de carga. Por ejemplo, `aplicados` en `ref = aplicados.get(...)`,
   línea 27.
2. **Mención del revisor:** solo cuentan las variables que el revisor **nombra** en su diagnóstico causal:
   - se usan regla, secuencia causal, rama inaccesible, propiedad a cambiar y hechos
     (`revision_existente.texto_causal_del_revisor`);
   - no se usan las citas (ya son anclas) ni las hipótesis secundarias;
   - el nombre se compara como identificador completo y debe tener al menos 3 caracteres.
3. **Escrituras:** se toman las de esa variable en la **misma función**:
   - cuentan la reasignación, `x[k] = …`, `del x[k]` y las llamadas `x.add/append/extend/insert/update/setdefault/
     pop/popitem/remove/discard/clear()`;
   - no cuentan las funciones anidadas ni otras funciones;
   - tampoco la inicialización de primer nivel, que ya es contexto de solo lectura.
4. **Zonas nuevas:** cada escritura que no está cubierta suma su zona con el mismo `_zona`.
   - Límites: como máximo 2 zonas relacionadas, y siempre dentro del tope de 80 líneas.
   - No hay transitividad: las lecturas de una zona agregada no agregan otras.
5. **Registro:** `localizacion.relaciones` guarda variable, línea de lectura, línea de escritura, tipo, zona y
   resultado («agregada», «ya cubierta» o el límite alcanzado).

**Lo que no se hace:**
- interpretar referencias «línea N» (C);
- usar casos ocultos, la batería independiente o la implementación de referencia;
- cambiar los modelos, los límites, el prompt del parche, la validación o la aplicación.

**Límite conocido:** una palabra común igual a la variable, como «los movimientos aplicados», cuenta como mención.
Lo fija una prueba.

**Resultado con un diagnóstico equivalente al real:** zonas `[(27, 37), (41, 46)]`, con la relación `aplicados`:
lectura en la línea 27 → escritura en la 45 (`aplicados[mid] = …`) → zona 41–46.

**Pruebas:** `tests/test_oficina_lectura_escritura.py` (16).
- **Fundamental:** sin texto causal la zona es 27–37, como en la corrida real; con el diagnóstico real, 27–37 y
  41–46, con la relación registrada.
- **Origen del texto causal:** sale solo de la prosa principal y de los hechos.
- **Negativas:**
  - una variable no nombrada no agrega zonas;
  - «línea 44» no ancla nada;
  - una variable nombrada pero que no se lee en una zona anclada no agrega zonas;
  - solo coinciden identificadores completos;
  - la inicialización de primer nivel queda afuera;
  - no hay transitividad (`tipo`);
  - una escritura ya cubierta solo se registra;
  - los nombres cortos se ignoran;
  - las funciones anidadas y las otras funciones quedan afuera.
- **Límites:** como máximo 2 zonas relacionadas y el tope de líneas.
- **Simulación:**
  - con el diagnóstico equivalente al real, el parche estilo Qwen sobre la línea 43 se aplica, elimina la
    discrepancia y aprueba 9/9;
  - sin la mención causal, el mismo parche sigue fuera de zona.

En `test_oficina_localizador.py`, la prueba de citas no verificadas usa ahora una prosa sin nombres de variables,
para medir solo la corrección A.

Suite completa: 675 pruebas, OK (770 s).

### Repetir 7.3 (carpeta NUEVA; mismos modelos, defecto y límites)

```bash
cd <worktree> && git pull --ff-only
python3 -m unittest tests.test_oficina_lectura_escritura tests.test_oficina_localizador tests.test_oficina_parches
L=~/constructor-ia-piloto/data/oficina-revision-7-3b
python3 -m constructor_ia oficina registrar --carpeta $L --id pulenta --url http://<IP o .ts.net>:11435 \
    --modelo gemma4:12b --modelo qwen2.5-coder:7b --nodo rampern --gpu "RTX 4070" --vram-gb 8 --ram-gb 32
python3 -m constructor_ia oficina revisar-existente --carpeta $L --trabajador pulenta \
    --mision conciliar_movimientos --defecto anulaciones_no_registradas \
    --modelo qwen2.5-coder:7b --revisor-modelo gemma4:12b --variante 7.3                 # preflight
python3 -m constructor_ia oficina revisar-existente --carpeta $L --trabajador pulenta \
    --mision conciliar_movimientos --defecto anulaciones_no_registradas \
    --modelo qwen2.5-coder:7b --revisor-modelo gemma4:12b --variante 7.3 --autorizo-inferencia-remota
```

Para atribuir el resultado, miren en `reparacion-N/reparacion.json`:
- `localizacion.citas`, que corresponde a la corrección A;
- `localizacion.relaciones`, que corresponde a la corrección B.

## Experimento 7.3b — revisor con 2048 tokens de salida

**Corrida `20261024-040804`:** se detuvo antes del programador. Las dos respuestas del revisor (`gemma4:12b`)
llegaron a los 1024 tokens y quedaron con el JSON incompleto.

**Cambio (único):** el revisor de `oficina revisar-existente` usa `num_predict` = 2048
(`revision_existente.NUM_PREDICT_REVISOR`, aplicado en `cli._oficina_revision_existente`). Antes era 1024.

**Sin cambios:**
- el revisor de `oficina programar`, que sigue en 1024;
- el programador: `num_ctx` 8192, `num_predict` 2048 y temperatura 0.2;
- los modelos, los plazos, los intentos, el presupuesto de 2400 s;
- los localizadores A y B, los prompts, los parches y la misión.

Prueba: `tests/test_oficina_revisor_2048.py` (2).
- En `revisar-existente`, el pedido al revisor sale con 2048, el del programador con lo mismo de antes, y
  `informe.json` lo registra.
- En `programar --revisor`, el revisor sigue con 1024.

Suite completa: 677 pruebas, OK (764 s).

Repetir 7.3b en un laboratorio nuevo (por ejemplo `oficina-revision-7-3c`) con los mismos comandos de la sección
anterior.

## Experimento 7.4: sonda de comprensión causal (implementado, SIN inferencias; diagnóstico, no reparación)

**Contexto (7.3c):**
- Gemma diagnosticó bien el defecto.
- El coordinador habilitó las zonas 27–37 y 41–46.
- Qwen generó tres veces un parche incorrecto.

**Pregunta:** ¿`qwen2.5-coder:7b` entiende la **causa** cuando recibe las dos zonas?

**Qué es:** una prueba independiente, `oficina sondear-comprension` (`oficina/sonda_comprension.py`). No toca el
coordinador, `revisar-existente`, los parches ni la corrida original, y no lleva a ninguna reparación.

### Recorrido

1. **Fuentes, solo lectura**, de una corrida terminada de `revisar-existente` (`--corrida`):
   - `inicial/solucion.py`;
   - `inicial/preparacion.json`;
   - la primera `revision-N/revision.json` con la discrepancia **demostrada**;
   - las zonas de `reparacion-1/reparacion.json`.

   Se registran las huellas de esos archivos. La sonda escribe en un laboratorio aparte (`--carpeta`, nunca dentro
   de la corrida).
2. **Pedido** (`SISTEMA_SONDA` y `PREGUNTAS`), en prosa:
   - **Contenido:**
     - el contrato;
     - las dos zonas con el contexto de solo lectura, con el mismo formato que en el 7.3;
     - los campos del diagnóstico de Gemma: regla, citas comprobadas, secuencia causal, rama inaccesible, propiedad
       señalada y hechos.
   - **Preguntas:**
     - qué ocurre, paso a paso, cuando una anulación referencia a otra anulación ya aplicada;
     - por qué no se alcanza `ref["tipo"] == "anulacion"`.
   - **No incluye:**
     - pedido de código ni de parches;
     - las instrucciones de reparación del 7.1 («QUÉ HACER…»);
     - la evidencia del caso sintético;
     - datos de la batería;
     - el defecto ni la referencia;
     - la rúbrica de evaluación.

   La auditoría de fugas revisa el pedido y, si encuentra algo, no se envía.
3. **Inferencia:** una sola, con la infraestructura remota existente (`ClienteRemoto` con lista blanca y compuerta
   por destino) y los parámetros del programador (`num_ctx` 8192, `num_predict` 2048, temperatura 0.2).
4. **Registro** en `<carpeta>/experimento-7-4/sonda-<fecha>/`:
   - `origen.json`: fuentes, huellas, zonas y auditoría;
   - `pedido.json`;
   - `respuesta.txt`: literal;
   - `metadatos.json`: modelo, opciones, tokens, tiempos, motivo de fin y si se cortó;
   - `evaluacion.json`.
5. **Evaluación separada** (`evaluar`), automática e **indicativa**: requiere leer `respuesta.txt`.
   - `identifica_ausencia_en_aplicados`: alguna oración dice que las anulaciones aplicadas no se registran (no se
     guardan, no se agregan…) en `aplicados`, o atribuye la falta a la condición `tipo != "anulacion"`;
   - `observa_que_la_referida_no_esta`: alguna oración dice que la anulación referida no está, sin explicar por qué;
   - `explica_ref_none` y `cita_la_condicion`;
   - `contiene_codigo` y `propone_cambios`, que violan la consigna;
   - `solo_parafrasis_del_revisor`, porque el diagnóstico de Gemma ya contiene la causa y la sonda mide si Qwen la
     explica, no si la descubre.

### Pruebas

`tests/test_oficina_sonda_comprension.py` (10), sin inferencias. La corrida de origen se genera con el 7.3 simulado.
- **Evaluación:**
  - una explicación causal se identifica y una equivocada no;
  - observar la ausencia se separa de explicar su causa;
  - se marcan el código, la propuesta de cambios y la paráfrasis.
- **Fuentes:** las zonas reales 27–37 y 41–46, y el diagnóstico demostrado.
- **Pedido:** tiene las zonas, el diagnóstico y las preguntas, y nada de lo prohibido.
- **Inferencia y registro:**
  - una sola inferencia, con el modelo y las opciones de siempre;
  - la respuesta literal y los metadatos quedan guardados;
  - lo registrado es lo enviado;
  - la corrida original no cambia (huellas).
- **Respuesta cortada:** se registra y se evalúa igual.
- **Rechazos:**
  - escribir dentro de la corrida;
  - un laboratorio con `taller.db`;
  - una corrida 7.1, que no tiene zonas;
  - una corrida sin discrepancia demostrada.
- **CLI:** la preparación no infiere ni escribe; con autorización hace una inferencia.

Suite completa: 687 pruebas, OK (814 s).

### Ejecutar en la VivoBook (con autorización explícita)

```bash
cd <worktree> && git pull --ff-only && cd constructor-ia
python3 -m unittest tests.test_oficina_sonda_comprension                 # sin inferencias
R=~/constructor-ia-piloto/data/oficina-revision-7-3c/experimento-7/corrida-<la de 7.3c>
L=~/constructor-ia-piloto/data/oficina-sonda-7-4
python3 -m constructor_ia oficina registrar --carpeta $L --id pulenta --url http://<IP o .ts.net>:11435 \
    --modelo qwen2.5-coder:7b --nodo rampern --gpu "RTX 4070" --vram-gb 8 --ram-gb 32
python3 -m constructor_ia oficina sondear-comprension --corrida $R --carpeta $L --trabajador pulenta \
    --mostrar-pedido                                                     # preparación: muestra el pedido, no infiere
python3 -m constructor_ia oficina sondear-comprension --corrida $R --carpeta $L --trabajador pulenta \
    --autorizo-inferencia-remota                                         # UNA inferencia
```

## Experimento 7.5: capacidad real de reparación con la causa explícita (implementado, SIN inferencias)

**Contexto (7.4):** Qwen identificó que `ref` es `None`, pero no lo conectó con la condición `tipo != "anulacion"`.

**Pregunta:** ¿`qwen2.5-coder:7b` produce un parche mínimo y correcto si se le **da** la causa?

**Qué es:** un experimento aislado, `oficina reparar-guiado` (`oficina/reparacion_guiada.py`), que reutiliza piezas
existentes sin ampliar el coordinador:
- las fuentes de solo lectura del 7.4;
- el formato, la validación y la aplicación de parches del 7.3;
- el contraste, la retroalimentación del 7.1 y la batería independiente.

### Recorrido (a lo sumo 3 intentos)

1. **Fuentes**, solo lectura, de la corrida 7.3c:
   - el código defectuoso;
   - las zonas del primer intento (27–37 y 41–46);
   - el caso sintético demostrado por el revisor.

   Se comprueba que las líneas clave de la causa estén dentro de las zonas.
2. **Pedido** (`SISTEMA_PARCHE` del 7.3):
   - **Contenido:** el contrato, los ejemplos visibles, las zonas y la **causa explícita** escrita por el equipo
     (`CAUSAS`): las anulaciones válidas no quedan registradas en `aplicados` porque la escritura está condicionada
     por `tipo != "anulacion"`.
   - **Formato:** solo un parche JSON, nunca el programa completo.
   - **No incluye:** casos ocultos, la referencia ni resultados de la batería.
   - La auditoría de fugas revisa cada pedido antes de enviarlo.
3. **Validación:** el parche se valida y se aplica sobre una copia del **mismo** código defectuoso en cada intento,
   con coincidencia exacta, zonas y sintaxis, sin relajar ningún control. Un rechazo se registra con su motivo.
4. **Si el parche se aplica:**
   - **Primero:** el caso sintético y los ejemplos visibles.
   - **Si los dos dan bien:** la batería independiente completa, una sola vez. Su resultado nunca vuelve al modelo.
5. **Si falla:** el intento siguiente recibe solo evidencia permitida:
   - el motivo del rechazo, o si repitió un parche;
   - o el resultado de su parche en el caso sintético (retroalimentación del 7.1) y en los ejemplos visibles.

   Después de 3 intentos, o si la batería falla, se detiene con el motivo.

### Criterio de éxito (`informe.json`, `criterios`)

Hacen falta las tres cosas, además de una auditoría limpia:
- **Parche válido y aplicado.**
- **Programa distinto del defectuoso.**
- **Batería independiente completa aprobada (9/9).**

**Fracaso:** si en 3 intentos no se cumple, el equipo deja de agregar ayudas específicas para este modelo y pasa a
evaluar otro programador.

### Registro (`<carpeta>/experimento-7-5/reparacion-<fecha>/`)

- `intento-N/pedido.json` y `respuesta.txt` (literal);
- `parche.json`: el parche, si se aplicó, las líneas y el motivo del rechazo;
- `solucion.py`, solo si se aplicó;
- `retroalimentacion.md`;
- `informe.json`: intentos, llamadas (tokens, tiempos, motivo de fin, si se cortó), verificación, criterios,
  auditoría y motivo.

### Pruebas

`tests/test_oficina_reparacion_guiada.py` (9), sin inferencias, con la corrida de origen del 7.3 simulado y el
sandbox real.
- **Pedido:** tiene las zonas reales, la causa explícita y pide solo un parche, sin datos prohibidos.
- **Preparación:** exige que las líneas de la causa estén en las zonas y que haya una causa escrita.
- **Parche correcto:** pasa 9/9; la respuesta y el registro son literales y la corrida original queda intacta.
- **Rechazos:** se registran y su motivo vuelve al modelo, incluido el parche repetido.
- **Tres intentos fallidos:** se detiene con el motivo sin pedir un cuarto, y entre intentos recibe la
  retroalimentación del caso.
- **Batería independiente:** decide una sola vez y no vuelve al modelo.
- **Otros:** respuesta cortada, presupuesto de tiempo y rechazo a escribir dentro de la corrida.
- **CLI:** la preparación no infiere ni escribe, y comprueba que la referencia aprueba y el defectuoso no; con
  autorización, una inferencia.

Suite completa: 696 pruebas, OK (837 s).

### Una prueba real desde la VivoBook (con autorización explícita)

```bash
cd <worktree> && git pull --ff-only && cd constructor-ia
python3 -m unittest tests.test_oficina_reparacion_guiada                 # sin inferencias
R=~/constructor-ia-piloto/data/oficina-revision-7-3c/experimento-7/corrida-<la de 7.3c>
L=~/constructor-ia-piloto/data/oficina-reparacion-7-5
python3 -m constructor_ia oficina registrar --carpeta $L --id pulenta --url http://<IP o .ts.net>:11435 \
    --modelo qwen2.5-coder:7b --nodo rampern --gpu "RTX 4070" --vram-gb 8 --ram-gb 32
python3 -m constructor_ia oficina reparar-guiado --corrida $R --carpeta $L --trabajador pulenta \
    --mostrar-pedido                                                     # preparación: no infiere ni escribe
python3 -m constructor_ia oficina reparar-guiado --corrida $R --carpeta $L --trabajador pulenta \
    --autorizo-inferencia-remota                                         # hasta 3 inferencias
```

## Experimento 7.6: reparación automática con normalización segura de la sangría (implementado, SIN inferencias)

**Contexto (7.5, verificado en la VivoBook):**
- Qwen propuso tres veces `if tipo != "anulacion":` → `if True:` sin sangría, y los tres parches se rechazaron por
  coincidencia literal.
- Con la sangría original, el mismo parche aprueba 9/9, con la huella del código defectuoso `5b017882…f5a542`.

**Objetivo:** repetir el 7.5 sin intervención humana durante la reparación.

**Qué cambia:** solo la opción `--normalizar-sangria` de `oficina reparar-guiado` (`reparacion_guiada.py`,
`normalizar_sangria`). Sin la opción, el 7.5 queda exactamente igual. El coordinador, `revisar-existente`, los
parches del 7.3, el sandbox, los límites, la auditoría y la batería no cambian. Las inferencias siguen siendo 3 como
máximo, sin datos ocultos.

### Cuándo y cómo se normaliza (estrictamente de formato)

1. **Disparador:** solo cuando `parches.aplicar` rechaza el parche porque un fragmento original **no aparece
   literalmente**. Cualquier otro rechazo queda como está.
2. **Por cambio:**
   - si coincide literalmente una vez, no se toca;
   - si coincide literalmente más de una vez, se rechaza por ambiguo.
3. **Sin coincidencia literal:** se busca el fragmento comparando, línea por línea:
   - el **contenido idéntico** después de la sangría: mismas comillas, operadores y expresiones;
   - la **sangría relativa** idéntica entre sus líneas;
   - las líneas vacías en el mismo lugar.
4. **Se rechaza si:**
   - no hay coincidencias: la normalización no corrige el contenido;
   - hay **más de una** en todo el código: ambiguo;
   - la única coincidencia está **fuera de las zonas autorizadas**;
   - hay tabulaciones en la sangría;
   - la primera línea del reemplazo tiene una sangría distinta de la del original, porque no se sabe cuánto
     desplazarla;
   - alguna línea quedaría con sangría negativa.
5. **Desplazamiento:** el original y el reemplazo se desplazan la **misma** cantidad de espacios. El contenido de
   cada línea no cambia.
6. **Validación:** el parche normalizado pasa por **la misma** validación y aplicación de siempre: zonas,
   superposición, sintaxis y que defina la función.
7. **Registro** en `intento-N/parche.json`:
   - el parche original tal cual (`cambios`);
   - `rechazo_literal`;
   - `parche_normalizado`;
   - `normalizacion`: por cambio, líneas, desplazamiento y justificación.

   En `informe.json`, cada intento tiene `sangria_normalizada` y `clasificacion` vale «reparación automática con
   normalización de formato (sangría)» o «reparación autónoma». Las corridas van a `experimento-7-6/`.

### Pruebas

`tests/test_oficina_normalizacion_sangria.py` (17).
- **Positivas:**
  - la huella del código defectuoso coincide con la verificada en la VivoBook;
  - el parche de Qwen solo recibe su sangría (una sola línea distinta);
  - un fragmento multilínea conserva su estructura relativa y produce la referencia;
  - un fragmento con sangría de más se desplaza hacia atrás;
  - una coincidencia literal no se toca;
  - el contenido nunca cambia.
- **Negativas:**
  - cambios lógicos (espacios internos, comillas, operador, acento) no se normalizan;
  - fragmentos ambiguos;
  - fragmentos fuera de zona;
  - estructura relativa rota;
  - tabulaciones;
  - reemplazo con otra sangría;
  - sangría negativa;
  - un parche normalizado con sintaxis inválida sigue rechazado.
- **Ciclo:**
  - el parche de Qwen se normaliza, se aplica y aprueba 9/9 en la primera inferencia, sin intervención;
  - sin la opción, el 7.5 sigue con tres rechazos;
  - la normalización no corrige la lógica;
  - los parches inseguros siguen rechazados;
  - el CLI con preparación y ejecución.

Suite completa: 713 pruebas, OK (834 s).

### Verificar sin inferencias y después una corrida real independiente

```bash
cd <worktree> && git pull --ff-only && cd constructor-ia
python3 -m unittest tests.test_oficina_normalizacion_sangria tests.test_oficina_reparacion_guiada   # sin inferencias
R=~/constructor-ia-piloto/data/oficina-revision-7-3c/experimento-7/corrida-<la de 7.3c>
L=~/constructor-ia-piloto/data/oficina-reparacion-7-6                    # laboratorio nuevo
python3 -m constructor_ia oficina registrar --carpeta $L --id pulenta --url http://<IP o .ts.net>:11435 \
    --modelo qwen2.5-coder:7b --nodo rampern --gpu "RTX 4070" --vram-gb 8 --ram-gb 32
python3 -m constructor_ia oficina reparar-guiado --corrida $R --carpeta $L --trabajador pulenta \
    --normalizar-sangria --mostrar-pedido                                # preparación: no infiere ni escribe
python3 -m constructor_ia oficina reparar-guiado --corrida $R --carpeta $L --trabajador pulenta \
    --normalizar-sangria --autorizo-inferencia-remota                    # hasta 3 inferencias
```

## Experimento 7.7: diagnóstico y reparación coordinados entre dos IA (implementado, SIN inferencias)

**Contexto:** el 7.6 logró 9/9 en una inferencia real, pero con la **causa escrita por el equipo**.

**Objetivo:** reemplazar esa causa por un diagnóstico generado automáticamente por Gemma.

**Qué es:** el módulo `oficina reparar-coordinado` (`oficina/reparacion_coordinada.py`). Reutiliza el ciclo del 7.6
**tal cual**: `reparacion_guiada.correr`, con la normalización de la sangría.
- El único cambio en `reparacion_guiada.py` es que el rótulo de la causa es un parámetro (`titulo_causa`), con el de
  siempre por omisión. El 7.5 y el 7.6 no cambian.
- No cambian el coordinador general, `revisar-existente`, los parches, el sandbox, los límites, la auditoría ni la
  batería.

### Roles

1. **Diagnosticadora (`gemma4:12b`, `num_predict` 2048, salida JSON):**
   - **Recibe:**
     - el contrato y los ejemplos visibles;
     - las zonas de la corrida 7.3 de origen;
     - la **discrepancia observable** del caso sintético, recalculada en el sandbox: la entrada, lo que pide el
       contrato, lo que devuelve el código y qué falta o sobra.
   - **No recibe:**
     - la causa del equipo (`CAUSAS`);
     - el diagnóstico anterior del revisor;
     - el defecto ni la referencia;
     - casos ocultos.

     La auditoría de fugas revisa cada pedido.
   - **Responde:** `causa`, `lineas_citadas`, `mecanismo` y `relacion_con_la_discrepancia`.
   - **Validación:**
     - los campos están completos y hay al menos 2 pasos de mecanismo;
     - no incluye código;
     - al menos una línea citada aparece **literalmente dentro de las zonas**.
   - **Si el diagnóstico es insuficiente:** se registra el fallo y se le pide de nuevo, con el motivo, como máximo 2
     veces. Si sigue siendo insuficiente, **el experimento se detiene**: nunca se sustituye por la causa conocida.
2. **Programador (`qwen2.5-coder:7b`):** el ciclo del 7.6 con el diagnóstico de Gemma, rotulado «DIAGNÓSTICO DE LA
   DIAGNOSTICADORA (otra IA; el coordinador comprobó que las líneas citadas existen en el código, NO que la causa sea
   correcta)». Como máximo 3 parches JSON.
3. **Coordinador determinista:**
   - valida el diagnóstico y el parche;
   - normaliza solo la sangría y aplica sobre una copia;
   - prueba el caso sintético y los ejemplos visibles;
   - ejecuta la batería independiente una sola vez.

   Nada de eso vuelve a los modelos. El tiempo de las dos etapas comparte un mismo presupuesto (2400 s).

### Criterios de éxito (`informe.json`)

1. `1_diagnosticadora_identifica_la_causa`: evaluación **posterior y separada** del texto del diagnóstico
   (`sonda_comprension.evaluar`: ¿dice que las anulaciones aplicadas no se registran en `aplicados`?). No decide nada
   durante la corrida ni llega a ningún modelo.
2. `2_programador_recibe_ese_diagnostico_y_genera_un_parche`: se comprueba sobre los `pedido.json` efectivamente
   enviados.
3. `3_coordinador_aplica_el_parche`.
4. `4_bateria_independiente_completa`: 9/9.

**Clasificación** si se cumplen los cuatro: «diagnóstico y reparación automáticos coordinados», con la aclaración
«(con normalización de la sangría)» si correspondió. No es autonomía general.

### Registro (`<carpeta>/experimento-7-7/reparacion-<fecha>/`)

- `diagnostico-K/pedido.json`, `respuesta.txt` (literal) y `diagnostico.json` (validez, motivo, tokens, tiempos);
- `reparacion/`: el registro completo del ciclo del 7.6;
- `informe.json`: el diagnóstico usado (texto y huella), la comprobación de que llegó a cada pedido del programador,
  la evaluación del diagnóstico, los criterios, la clasificación y el motivo.

### Pruebas

`tests/test_oficina_reparacion_coordinada.py` (11), sin inferencias, con diagnosticadora y programador simulados y el
sandbox real.
- **Diagnóstico:** uno válido tiene sus citas comprobadas contra las zonas. Los insuficientes se rechazan con su
  motivo: sin JSON, sin causa, un solo paso, sin relación, citas inexistentes o fuera de zona, o con el programa.
- **Evaluación:** posterior y separada.
- **Pedido a la diagnosticadora:** tiene la discrepancia observable y no tiene la causa del equipo, el diagnóstico
  anterior, el defecto, la referencia ni datos ocultos.
- **Integración positiva:**
  - es **sin ninguna causa escrita** (`CAUSAS` vacío);
  - el pedido del programador tiene la marca única de la respuesta de Gemma, y diagnósticos distintos llegan
    distintos;
  - aprueba 9/9.
- **Diagnóstico insuficiente dos veces:** se detiene sin llamar al programador y sin sustituirlo.
- **Segundo diagnóstico válido:** se usa ese.
- **Causa equivocada:** no cuenta como éxito aunque el parche funcione.
- **Otros:** tres parches fallidos, presupuesto y CLI.

Suite completa: 724 pruebas, OK (855 s).

### Verificar en la VivoBook y después correr

```bash
cd <worktree> && git pull --ff-only && cd constructor-ia
python3 -m unittest tests.test_oficina_reparacion_coordinada tests.test_oficina_normalizacion_sangria \
    tests.test_oficina_reparacion_guiada                                    # sin inferencias
R=~/constructor-ia-piloto/data/oficina-revision-7-3c/experimento-7/corrida-<la de 7.3c>
L=~/constructor-ia-piloto/data/oficina-coordinada-7-7                       # laboratorio nuevo
python3 -m constructor_ia oficina registrar --carpeta $L --id pulenta --url http://<IP o .ts.net>:11435 \
    --modelo gemma4:12b --modelo qwen2.5-coder:7b --nodo rampern --gpu "RTX 4070" --vram-gb 8 --ram-gb 32
python3 -m constructor_ia oficina reparar-coordinado --corrida $R --carpeta $L --trabajador pulenta \
    --mostrar-pedido                                     # preparación: muestra el pedido a Gemma; no infiere ni escribe
python3 -m constructor_ia oficina reparar-coordinado --corrida $R --carpeta $L --trabajador pulenta \
    --autorizo-inferencia-remota                         # hasta 2 diagnósticos + 3 parches
```

## Experimento 7.8: diagnóstico causal con preservación de comportamiento (implementado, SIN inferencias)

**Contexto (7.7):**
- Gemma identificó la causa: las anulaciones aplicadas no se registraban en `aplicados`.
- Qwen propuso tres veces `if tipo != "anulacion":` → `if tipo == "anulacion":`. Los parches se aplicaban, pero
  dejaban de registrar los ingresos y egresos, y la discrepancia seguía.

**Objetivo:** que Gemma diagnostique también qué comportamiento correcto hay que preservar, y que Qwen lo use.

**Qué es:** un experimento independiente, `oficina reparar-coordinado-8` (`oficina/reparacion_coordinada_8.py`), que
reutiliza el recorrido del 7.7 (`reparacion_coordinada.correr`).
- **Cambio en el 7.7 y en el ciclo de reparación:** solo puntos de extensión opcionales, que por omisión no hacen
  nada:
  - `ajustes` en `reparacion_coordinada.correr`;
  - `pedido` y `retroalimentacion` en `reparacion_guiada`.

  Las pruebas del 7.5, 7.6 y 7.7 pasan sin cambios.
- **Lo que no cambia:** el coordinador general, los parches, el sandbox, los límites, la auditoría y la batería.

### Diferencias con el 7.7 (todas generadas o medidas, ninguna escrita por el equipo)

1. **Diagnosticadora** (`SISTEMA_DIAGNOSTICO_8`): además de causa, citas, mecanismo y relación, describe:
   - el `comportamiento_defectuoso`: qué hace mal y cuándo;
   - el `comportamiento_a_preservar`: lista de lo que hoy funciona según el contrato y podría romperse al corregir.

   Los dos campos se validan (no pueden faltar ni estar vacíos). Si faltan, el diagnóstico es insuficiente y se le
   pide otra vez; como máximo 2 diagnósticos, sin sustitución.
2. **Programador:** recibe esos campos **completos** dentro del diagnóstico. Se le pide (`PEDIDO_8`) que en
   `justificacion` explique cómo su parche corrige el comportamiento defectuoso sin romper cada comportamiento a
   preservar. No se le dice qué condición cambiar ni cómo, y la justificación queda en `parche.json`.
3. **Retroalimentación** si un parche se aplica y no corrige el caso: solo que **la discrepancia persiste** y la
   diferencia observable del caso sintético:
   - la entrada;
   - lo que pide el contrato;
   - lo que devuelve el código con su parche;
   - las claves distintas y lo que falta o sobra;
   - si rompió un ejemplo visible del contrato (público), ese informe.

   No recibe el diff ni el resto de la retroalimentación del 7.1, ni nada de la batería.

**Lo que se mantiene igual:** 2 diagnósticos, 3 parches y 2400 s como máximo; normalización segura de la sangría;
auditoría de fugas; batería independiente una sola vez.

**Éxito:** los cuatro criterios del 7.7. Se clasifica como «diagnóstico y reparación automáticos coordinados, con
preservación de comportamiento», más «(con normalización de la sangría)» si correspondió. Las corridas van a
`experimento-7-8/`.

### Pruebas

`tests/test_oficina_reparacion_coordinada_8.py` (11), sin inferencias, con respuestas simuladas y el sandbox real.
- **Origen de los campos:** `comportamiento_a_preservar` y `comportamiento_defectuoso` salen de la respuesta de
  Gemma (marca única) y llegan completos al texto del programador.
- **Diagnósticos insuficientes:**
  - sin el campo, vacío, en blanco o sin el comportamiento defectuoso;
  - un diagnóstico del 7.7 tampoco alcanza;
  - dos insuficientes detienen el experimento sin llamar al programador.
- **Sin solución prefabricada:** los prompts del 7.8 no tienen condiciones, operadores, nombres del código ni la
  causa del equipo.
- **Lo que recibe el programador:** la marca única de Gemma (con `CAUSAS` vacío), el pedido de justificación y no el
  pedido del 7.6.
- **Parche que invierte la condición (el del 7.7):**
  - se aplica, normalizado;
  - la discrepancia persiste;
  - el intento siguiente recibe solo la diferencia observable y el informe de los ejemplos visibles;
  - lo registrado es lo enviado.
- **Reparación exitosa:** 9/9, con la justificación registrada.
- **Tres parches incorrectos:** se detiene.
- **Sin fugas:** ni casos ocultos, ni sus categorías, ni el defecto, ni la referencia.
- **El 7.7 sigue intacto:** mismo sistema y mismo pedido, sin los campos nuevos, y con la retroalimentación del 7.1.
- **CLI.**

Suite completa: 735 pruebas, OK (879 s).

### Verificar sin inferencias y después una corrida real independiente

```bash
cd <worktree> && git pull --ff-only && cd constructor-ia
python3 -m unittest tests.test_oficina_reparacion_coordinada_8 tests.test_oficina_reparacion_coordinada \
    tests.test_oficina_normalizacion_sangria tests.test_oficina_reparacion_guiada          # sin inferencias
R=~/constructor-ia-piloto/data/oficina-revision-7-3c/experimento-7/corrida-<la de 7.3c>
L=~/constructor-ia-piloto/data/oficina-coordinada-7-8                                     # laboratorio nuevo
python3 -m constructor_ia oficina registrar --carpeta $L --id pulenta --url http://<IP o .ts.net>:11435 \
    --modelo gemma4:12b --modelo qwen2.5-coder:7b --nodo rampern --gpu "RTX 4070" --vram-gb 8 --ram-gb 32
python3 -m constructor_ia oficina reparar-coordinado-8 --corrida $R --carpeta $L --trabajador pulenta \
    --mostrar-pedido                                   # preparación: muestra el pedido a Gemma; no infiere ni escribe
python3 -m constructor_ia oficina reparar-coordinado-8 --corrida $R --carpeta $L --trabajador pulenta \
    --autorizo-inferencia-remota                       # hasta 2 diagnósticos + 3 parches
```

## Experimento 7.9: revisión crítica de parches entre agentes (implementado, SIN inferencias)

**Contexto (7.8):**
- Gemma diagnosticó bien la causa.
- Qwen propuso tres veces `if ref is None:` → `if ref is None or ref['tipo'] == 'anulacion':`. Los parches se
  aplicaban y la discrepancia seguía.

**Objetivo:** comprobar si una revisión independiente de Gemma detecta errores causales en los parches de Qwen y lo
ayuda a corregirlos antes de la verificación final.

**Qué es:** el experimento `oficina reparar-coordinado-9` (`oficina/reparacion_coordinada_9.py`), que reutiliza el
recorrido del 7.8.
- En `reparacion_guiada.correr` solo se agrega el punto de extensión opcional `revision` (por omisión, sin revisión),
  y `reparacion_coordinada` lo pasa.
- El 7.5 al 7.8 no cambian y sus pruebas pasan.

### Roles (dos modelos; Gemma en dos invocaciones separadas)

1. **Diagnosticadora (gemma):** como en el 7.8.
2. **Programador (qwen):** propone un parche. El coordinador lo valida y lo aplica sobre una copia, con la
   normalización de la sangría. Un parche inválido se rechaza ahí y no llega a revisión.
3. **Revisora (gemma, otra invocación, `SISTEMA_REVISORA`):**
   - **Recibe:**
     - el contrato y los ejemplos visibles;
     - el código **original** (las zonas);
     - el diagnóstico;
     - el parche aplicado, con su justificación y la diferencia resultante.
   - **No recibe ningún resultado de pruebas:** ni el caso sintético sobre el parche, ni los ejemplos, ni la batería.
   - **Responde** un JSON: `decision` («aprobar» o «rechazar»), `elimina_la_causa`, `preserva_comportamiento`,
     `justificacion_causal` y, si rechaza, `critica`. Sin código.
   - Una respuesta inválida queda registrada como revisión inválida y no se inventa una decisión.
4. **Coordinador:**
   - **Si rechaza:** la crítica llega **íntegra** al intento siguiente de Qwen, como «REVISIÓN DE TU PARCHE… (otra
     IA, la revisora; NO es un resultado de pruebas): RECHAZADO».
   - **Si aprueba, o la revisión es inválida:** siguen las comprobaciones deterministas de siempre (caso sintético,
     ejemplos visibles y la batería una sola vez). **La aprobación no garantiza nada.**

**Límites:** 2 diagnósticos, 3 propuestas de Qwen (cada una con, como máximo, una revisión) y 2400 s para todo,
contando también las revisiones. La auditoría de fugas revisa también el pedido a la revisora.

### Capacidad crítica (`informe.json`, `capacidad_critica_de_la_revisora`)

Cada decisión se compara con lo **observable**: el caso sintético y los ejemplos visibles sobre el parche, que se
calculan siempre aunque no se le muestren a nadie. Si se ejecutó, también con la **batería**. Se cuentan:
- **detecciones correctas:** rechazó un parche que no cumple lo observable;
- **aprobaciones incorrectas:** aprobó un parche que no cumple lo observable o que no pasó la batería;
- **rechazos de parches observablemente correctos;**
- **revisiones inválidas.**

### Criterio de éxito

Los cuatro criterios del 7.7 y el 7.8, más `5_revisora_aprueba_el_parche_verificado`: Gemma diagnostica, Qwen
propone, Gemma revisa y aprueba, y la batería da 9/9.

Se clasifica como «diagnóstico, reparación y revisión crítica automáticos coordinados», más «(con normalización de la
sangría)» si correspondió. Las corridas van a `experimento-7-9/`. Cada revisión queda en
`reparacion/intento-N/revision.json` (pedido, respuesta literal, decisión y campos) y cada crítica en
`retroalimentacion.md`.

### Pruebas

`tests/test_oficina_reparacion_coordinada_9.py` (12), con respuestas simuladas y el sandbox real.
1. **Parche que no modifica la causa** (el del 7.8): la revisora lo rechaza y se cuenta como detección correcta.
2. **Parche que rompe un comportamiento a preservar** (invierte la condición; rompe un ejemplo visible): rechazado y
   detectado.
3. **La crítica llega íntegra** al intento siguiente, y lo registrado es lo enviado.
4. **Una aprobación no evita la validación:**
   - aprobar el parche que invierte la condición se registra como aprobación incorrecta, y Qwen recibe la
     discrepancia;
   - aprobar un parche que pasa lo observable pero no la batería no es éxito.
5. **Tres propuestas incorrectas** terminan sin éxito: 7 pedidos, 3 detecciones.
6. **Una reparación correcta**, sin ninguna causa escrita, aprueba 9/9 con los cinco criterios.
7. **Sin fugas:** la revisora no recibe resultados de pruebas.
8. **El 7.8 y el 7.7 siguen funcionando** (sin revisión).

Además:
- **Rechazo injustificado:** se registra el rechazo de un parche correcto.
- **Revisión inválida:** deciden las comprobaciones deterministas, pero no hay éxito.
- **Interpretación de la decisión** y **CLI**.

Suite completa: 747 pruebas, OK (915 s).

### Preparación sin inferencias y corrida real independiente

```bash
cd <worktree> && git pull --ff-only && cd constructor-ia
python3 -m unittest tests.test_oficina_reparacion_coordinada_9 tests.test_oficina_reparacion_coordinada_8 \
    tests.test_oficina_reparacion_coordinada tests.test_oficina_normalizacion_sangria                  # sin inferencias
R=~/constructor-ia-piloto/data/oficina-revision-7-3c/experimento-7/corrida-<la de 7.3c>
L=~/constructor-ia-piloto/data/oficina-coordinada-7-9                                                 # laboratorio nuevo
python3 -m constructor_ia oficina registrar --carpeta $L --id pulenta --url http://<IP o .ts.net>:11435 \
    --modelo gemma4:12b --modelo qwen2.5-coder:7b --nodo rampern --gpu "RTX 4070" --vram-gb 8 --ram-gb 32
python3 -m constructor_ia oficina reparar-coordinado-9 --corrida $R --carpeta $L --trabajador pulenta \
    --mostrar-pedido                                     # preparación: no infiere ni escribe
python3 -m constructor_ia oficina reparar-coordinado-9 --corrida $R --carpeta $L --trabajador pulenta \
    --autorizo-inferencia-remota                         # hasta 2 diagnósticos + 3 propuestas + 3 revisiones
```

## Experimento 7.10: planificación causal antes de programar (implementado, SIN inferencias)

**Contexto (7.9):** Gemma identificó la causa y rechazó dos parches defectuosos, pero Qwen agotó sus tres intentos.

**Objetivo:** comprobar si separar la planificación de la implementación permite que Qwen repare a partir del
diagnóstico autónomo de Gemma.

**Qué es:** `oficina reparar-coordinado-10` (`oficina/reparacion_coordinada_10.py`), un orquestador propio. El ciclo
cambia de forma (plan → revisión del plan → parche), así que no usa el bucle del 7.6, pero reutiliza sus piezas:
- el diagnóstico, la discrepancia observable y la retroalimentación del 7.8;
- el formato de revisión del 7.9;
- la validación y aplicación de parches, la normalización de sangría, el caso sintético, los ejemplos visibles, la
  batería y la auditoría.

No modifica ningún módulo anterior, salvo el CLI.

### Roles (dos modelos; cada rol, una invocación independiente)

1. **Diagnosticadora (gemma):** como en el 7.8 y el 7.9.
2. **Planificador (qwen, `SISTEMA_PLANIFICADOR`):** un plan JSON, **sin código**, con:
   - el estado incorrecto;
   - la operación que lo produce;
   - el comportamiento a cambiar;
   - el comportamiento a preservar;
   - las líneas a modificar, copiadas del código, cada una con su porqué;
   - desde el ciclo 2, `cambio_respecto_del_anterior`.

   **Validación:** campos completos y al menos una línea que exista dentro de las zonas.
3. **Revisora del plan (gemma, `SISTEMA_REVISORA_PLAN`):** decide si el plan elimina la causa y preserva el
   comportamiento.
   - Responde con decisión, justificación causal y crítica, el mismo esquema del 7.9. No propone su propia solución.
   - **Si rechaza:** la crítica llega **íntegra** al planificador. Cada ciclo admite como máximo 2 planes.
   - **Si la revisión es inválida:** no decide; se sigue y deciden las comprobaciones deterministas, pero no cuenta
     como aprobación.
4. **Programador (qwen):** un parche JSON que implementa el **plan aprobado**, que se le muestra completo.
5. **Coordinador:**
   - valida y aplica el parche sobre una copia, con normalización de sangría;
   - mide si el parche es **coherente con el plan**, es decir, si toca las líneas planificadas;
   - prueba el caso sintético y los ejemplos visibles, y ejecuta la batería una sola vez.

   La aprobación de un plan no prueba nada ni evita ninguna comprobación.

### Ciclos

Como máximo **3 ciclos** de plan → revisión → parche, y **2400 s** para todo.
- **Si el parche falla:** el ciclo siguiente planifica con la **discrepancia observable** del 7.8 (o el motivo del
  rechazo del parche) y el plan anterior, y tiene que explicar qué cambia.
- **Repeticiones:** se detectan planes repetidos (misma huella, sin contar la explicación del cambio) y parches
  repetidos. Un plan repetido se le señala al planificador; un parche que se repite **después del aviso** detiene la
  corrida.
- **Auditoría:** se revisa cada pedido. Nunca se envían casos ocultos, la referencia, el defecto ni la causa del
  equipo.

### Medición (`informe.json`, `medicion`)

**Por pasos:**
- diagnóstico correcto;
- plan causal correcto;
- revisión del plan correcta;
- parche coherente con el plan;
- parche aplicado;
- caso sintético corregido;
- batería independiente 9/9.

**Conteos:**
- aprobaciones incorrectas de planes;
- rechazos injustificados de planes;
- fallos de implementación: un plan correcto aprobado cuyo parche no corrigió el caso;
- planes repetidos;
- parches repetidos.

«Correcto» para el diagnóstico y el plan es una evaluación **posterior e indicativa** del texto: no llega a ningún
modelo, no decide nada y conviene confirmarla leyendo.

**Éxito:** diagnóstico correcto, plan **aprobado por la revisora**, parche aplicado, caso corregido y 9/9, sin fugas.
Se clasifica como «diagnóstico, planificación, revisión e implementación automáticos coordinados».

**Registro:**
- `diagnostico-K/`;
- `ciclo-N/plan-K/`: pedido, respuesta y plan;
- `ciclo-N/revision-plan-K/`;
- `ciclo-N/parche/`: pedido, respuesta, `parche.json` con la coherencia, `solucion.py` y `retroalimentacion.md`;
- `informe.json`: llamadas con rol, tokens y segundos.

### Pruebas

`tests/test_oficina_reparacion_coordinada_10.py` (14). Demuestran la **arquitectura** con respuestas simuladas, **no**
la capacidad de los modelos.
1. Un plan que no elimina la causa se rechaza.
2. Un plan que rompe un comportamiento correcto se rechaza.
3. Un plan correcto se aprueba.
4. La crítica llega íntegra a Qwen.
5. El parche recibe el plan aprobado completo, y se mide la coherencia (un parche sobre otra línea no es coherente).
6. Un parche incorrecto no pasa aunque el plan esté aprobado (se cuenta como fallo de implementación), y la batería
   decide.
7. Una reparación correcta da 9/9 con las siete mediciones en «sí».
8. No hay fugas, y la revisora del plan no recibe resultados de pruebas.
9. Se detectan planes y parches repetidos; un parche repetido después del aviso detiene la corrida.
10. El 7.8 y el 7.9 siguen funcionando.

Además: validación del plan, prompts sin la solución, límites (3 ciclos × 2 planes), revisión inválida, presupuesto y
CLI.

Suite completa: 761 pruebas, OK (927 s).

### Preparación sin inferencias (VivoBook)

```bash
cd <worktree> && git pull --ff-only && cd constructor-ia
python3 -m unittest tests.test_oficina_reparacion_coordinada_10 tests.test_oficina_reparacion_coordinada_9 \
    tests.test_oficina_reparacion_coordinada_8 tests.test_oficina_reparacion_coordinada                 # sin inferencias
R=~/constructor-ia-piloto/data/oficina-revision-7-3c/experimento-7/corrida-<la de 7.3c>
L=~/constructor-ia-piloto/data/oficina-coordinada-7-10                                              # laboratorio nuevo
python3 -m constructor_ia oficina registrar --carpeta $L --id pulenta --url http://<IP o .ts.net>:11435 \
    --modelo gemma4:12b --modelo qwen2.5-coder:7b --nodo rampern --gpu "RTX 4070" --vram-gb 8 --ram-gb 32
python3 -m constructor_ia oficina reparar-coordinado-10 --corrida $R --carpeta $L --trabajador pulenta \
    --mostrar-pedido                                     # no infiere ni escribe
# Corrida real, solo con autorización: ... reparar-coordinado-10 ... --autorizo-inferencia-remota
```

## Experimento 7.11: localización causal y revisión no bloqueante (implementado, SIN inferencias)

**Contexto (7.10):** Gemma diagnosticó bien, Qwen produjo seis planes, Gemma rechazó los seis y no se llegó a ningún
parche.

**Qué es:** `oficina reparar-coordinado-11` (`oficina/reparacion_coordinada_11.py`), un orquestador propio. Reutiliza,
sin modificarlas, piezas del 7.6 al 7.10:
- la sesión, la auditoría y la revisión del plan del 7.10;
- la discrepancia y la retroalimentación del 7.8;
- la validación y aplicación de parches, la normalización de sangría, el contraste, los ejemplos visibles y la
  batería.

Los experimentos 7.5 a 7.10 no cambian. El CLI solo suma el comando.

### 1. Localización causal (diagnosticadora, gemma)

El JSON separa:
- `origen_del_defecto`: la línea y la operación o condición que introduce el estado incorrecto;
- `punto_de_manifestacion`: la línea donde ese estado produce el resultado observable;
- `cadena_causal`: al menos 2 pasos;
- `comportamiento_a_preservar`: invariantes del contrato.

Origen y manifestación tienen que ser líneas que existan literalmente dentro de las zonas, y **distintas**. Si no,
el diagnóstico es insuficiente; hay como máximo 2 diagnósticos y nunca se sustituye por nada nuestro.

### 2. Plan concreto (planificador, qwen)

**Contenido del plan:**
- `estado_a_corregir`;
- `intervenciones`: la línea del código y la acción, en palabras;
- `por_que_elimina_la_causa`, en lugar de ocultar el síntoma;
- `invariantes_preservados`;
- después de un parche que falló, `cambio_respecto_del_anterior`.

**Filtros antes de la revisión:**
- **Plan descriptivo:** si no tiene una intervención concreta (una línea dentro de las zonas y una acción), se
  rechaza sin gastar la revisión.
- **Plan repetido:** si es la misma intervención que uno anterior, tampoco se revisa y se le pide otra. Dos planes son
  la misma intervención si tienen las mismas líneas y las mismas palabras de acción, sin contar acentos, mayúsculas,
  orden ni palabras vacías, y comparando por raíz («quitar», «quitando»). Así se detectan variantes de redacción.

### 3. Revisión no bloqueante (revisora, gemma)

- **Formato:** la revisora aprueba o rechaza con una crítica causal (el formato del 7.9 y el 7.10). La crítica llega
  **íntegra** al planificador. Cada ciclo admite como máximo 2 planes.
- **Si no se aprueba ninguno:** el coordinador implementa el **último plan válido, rechazado, bajo evaluación
  experimental**, solo para comprobarlo en el sandbox. Queda registrado en `plan_a_implementar` y en el parche, y se
  imprime.
- **Lo que esa excepción no saltea:** la validación del parche, las zonas, la normalización, la copia, bwrap y
  seccomp, el caso sintético, los ejemplos visibles, la batería y los límites.

### 4. Verificación objetiva y criterio

**El criterio principal es el comportamiento comprobado.** **Éxito** = parche aplicado, caso sintético corregido,
batería 9/9 y auditoría limpia. La aprobación de Gemma no es éxito y su rechazo no es fracaso. Si el plan fue
rechazado y la ejecución lo valida, la clasificación lo dice: «implementado bajo evaluación experimental».

### 5. Medición (`informe.json`, `medicion`), separada por tipo de evidencia

- **Demostrado por ejecución:**
  - el plan preserva los invariantes (su implementación coherente aprobó la batería);
  - el parche implementa el plan (coherencia de líneas);
  - el parche se aplicó;
  - el parche supera el caso sintético;
  - la batería da 9/9.
- **Posteriores deterministas aproximadas:** se calculan **después** y no llegan a ningún modelo.
  - el diagnóstico identifica el origen y la manifestación;
  - el plan propone una intervención causal concreta;
  - el plan declara invariantes.

  Las referencias son `origen` (las líneas que difieren entre el código defectuoso y la referencia, en 7.3c 44–45) y
  `manifestacion` (la zona donde se emite el valor equivocado del caso sintético).
- **Opinión del modelo:** la decisión de Gemma y si el plan se implementó bajo evaluación experimental.

**Plan causal correcto** = apunta al origen **y** su implementación coherente aprobó la batería. Mencionar el defecto
no alcanza, y un plan que no se pudo ejecutar no cuenta como correcto.

**Desacuerdos entre la revisora y la ejecución:** se registran los casos «aprobó un plan cuya implementación falló» y
«rechazó un plan cuya implementación aprobó la batería».

### 6. Límites

- 3 ciclos;
- 2 planes por ciclo, como máximo 6 planes en total;
- 3 parches;
- 2400 s.

Un parche que repite a otro después del aviso detiene la corrida. Cuenta como repetido el mismo texto (sin espacios
finales) o un texto distinto que produce **el mismo programa**. La auditoría revisa cada pedido.

### Pruebas

`tests/test_oficina_reparacion_coordinada_11.py` (16). Demuestran la arquitectura, no la capacidad de los modelos.
- **Diagnóstico:** origen y manifestación separados (la misma línea se rechaza); los dos llegan al planificador y se
  miden.
- **Plan:** un plan concreto se acepta y uno descriptivo se rechaza sin revisión.
- **Revisión:**
  - un plan que confunde `vistos` con `aplicados` se rechaza, y la crítica llega íntegra;
  - Gemma **rechaza un plan correcto**: se implementa bajo evaluación experimental, da 9/9 y queda registrado el
    desacuerdo;
  - Gemma **aprueba un plan incorrecto**: la ejecución lo desmiente, queda el desacuerdo, el plan no se cuenta como
    correcto y el ciclo siguiente recibe la discrepancia.
- **Controles:** un plan rechazado no saltea controles (un parche fuera de zona se rechaza igual), y tampoco con un
  plan aprobado.
- **Aislamiento:** no se filtran casos ocultos, referencias posteriores ni la batería; las revisiones no reciben
  resultados de pruebas.
- **Repeticiones:** planes repetidos (variante textual) y parches equivalentes (otro texto, el mismo programa).
- **Otros:** límites, presupuesto, prompts sin la solución, y el 7.10 y el 7.9 siguen funcionando.
- **CLI.**

Suite completa: 777 pruebas, OK (954 s).

### Actualizar la VivoBook y solo la preparación

```bash
cd <worktree> && git pull --ff-only && cd constructor-ia
python3 -m unittest tests.test_oficina_reparacion_coordinada_11 tests.test_oficina_reparacion_coordinada_10 \
    tests.test_oficina_reparacion_coordinada_9                                                  # sin inferencias
R=~/constructor-ia-piloto/data/oficina-revision-7-3c/experimento-7/corrida-<la de 7.3c>
L=~/constructor-ia-piloto/data/oficina-coordinada-7-11                                      # laboratorio nuevo
python3 -m constructor_ia oficina registrar --carpeta $L --id pulenta --url http://<IP o .ts.net>:11435 \
    --modelo gemma4:12b --modelo qwen2.5-coder:7b --nodo rampern --gpu "RTX 4070" --vram-gb 8 --ram-gb 32
python3 -m constructor_ia oficina reparar-coordinado-11 --corrida $R --carpeta $L --trabajador pulenta \
    --mostrar-pedido                                      # no infiere ni escribe
```

### Lo que falta (siguientes etapas)

4. Coordinador concurrente con un escritor único y propuestas verificadas centralmente.
5. Prueba de punta a punta con dos misiones simultáneas (Ollama falsos con latencia), más la suite completa.
6. Procedimiento manual con la notebook: revisión de Python con verificación independiente (la corren ustedes).

**Fuera del MVP:** memoria por rol aplicada al agente de datos, misiones del agente repartidas entre trabajadores y
ejecución remota de código, que requiere aislamiento propio.
