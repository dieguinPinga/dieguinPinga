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

### Lo que falta (siguientes etapas)

4. Coordinador concurrente con un escritor único y propuestas verificadas centralmente.
5. Prueba de punta a punta con dos misiones simultáneas (Ollama falsos con latencia), más la suite completa.
6. Procedimiento manual con la notebook: revisión de Python con verificación independiente (la corren ustedes).

**Fuera del MVP:** memoria por rol aplicada al agente de datos, misiones del agente repartidas entre trabajadores y
ejecución remota de código, que requiere aislamiento propio.
