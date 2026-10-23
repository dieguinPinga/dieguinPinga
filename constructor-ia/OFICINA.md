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

### Lo que falta (siguientes etapas)

3. Adaptador remoto con la interfaz `pedir()` y solo rutas de inferencia, candado por destino, y planificador por
   modelo, turno libre, permisos y RAM de la VivoBook (umbral de 3.900 MiB: si no alcanza, remoto o pendiente).
4. Coordinador concurrente con un escritor único y propuestas verificadas centralmente.
5. Prueba de punta a punta con dos misiones simultáneas (Ollama falsos con latencia), más la suite completa.
6. Procedimiento manual con la notebook: revisión de Python con verificación independiente (la corren ustedes).

**Fuera del MVP:** memoria por rol aplicada al agente de datos, misiones del agente repartidas entre trabajadores y
ejecución remota de código, que requiere aislamiento propio.
