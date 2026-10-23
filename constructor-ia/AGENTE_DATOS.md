# Agente de datos: explorador autónomo con memoria persistente — DISEÑO (propuesta, sin código)

> Estado: propuesta para revisión de Diego y ChatGPT (2026-10-23). No hay código ni inferencias.
> Nada de esto toca las bases productivas, Constructor IA productivo, Vigilancia, Node-RED, OpenClaw, Tailscale ni
> el Ollama principal.

## Misión experimental

1. Recibir una base MariaDB desconocida, al principio sintética.
2. Descubrir sus tablas y columnas sin nombres preprogramados.
3. Investigar qué información representan.
4. Identificar qué tablas sirven para responder una pregunta en lenguaje natural.
5. Formular consultas SQL exclusivamente de lectura.
6. Verificar los resultados y registrar lo aprendido en una memoria persistente.
7. Reiniciar el agente y comprobar que recupera ese conocimiento en una segunda misión.

**Requisito permanente:** la computadora queda encendida, y el agente recibe misiones nuevas sin perder lo aprendido.

## 1. Punto de partida

### Lo que ya existe y se reutiliza (sin modificarlo)

| Pieza | Qué hace hoy | Uso en el agente |
|---|---|---|
| `explorer/mysqlproto.py`, `sqlguard.py`, `broker.py` | Lectura segura y verificada: cuenta con solo `SELECT`, sesión `tx_read_only`, topes de filas, bytes y tiempo, `KILL QUERY`, una consulta a la vez, registro de cada consulta y fallo cerrado si la cuenta tiene privilegios de más | **Intermediario**, igual que hoy, también contra el MariaDB privado de pruebas |
| `explorer/mapstore.py` | Instantáneas del esquema con huella y diferencias; hechos con certeza, procedencia e historial (0.4.5–0.4.7) | Niveles 1 a 3 de la memoria |
| `explorer/knowledge.py` | Firma de consultas y alcance real según el SQL ejecutado: LIMIT interiores, muestra o tabla completa (0.4.7) | Verificación del alcance; detectar repeticiones |
| `explorer/validation.py` | `precheck_sql` (nombres que existen, base correcta) y contraste léxico de afirmaciones: cifras, valores, identificadores y alias | Verificación previa del SQL y de la respuesta redactada |
| `explorer/context.py` | Estimación conservadora de tokens, calibrada con Qwen | Presupuesto de cada prompt (`num_ctx` 4096) |
| `investigador/engine.py`, `contract.py` | Contrato «qué medir sobre qué objetos», compilado por un motor determinista; nombres exactos o una **aclaración**, sin ejecutar | Herramienta `medir` (consultas sin SQL escrito por el modelo) |
| `laboratorio/privado.py`, `sintetica.py` | MariaDB privado, desechable y sin red; base sintética con verdad conocida | Entorno de la primera misión; modelo para el generador nuevo |
| `laboratorio/piloto/` (ciclo, sugerencias) | Coordinador con regla de decisión fija y registrada; rechazos que explican el motivo; ambigüedad rechazada; juez separado con datos propios; auditoría de fugas; llamadas registradas por partes; reanudación; script protegido | El **patrón** del coordinador, del juez y de la operación en la VivoBook |
| `missions.py`, `workers.py`, `ollama.py` | Cola persistente; una misión a la vez; `InferenceGate`: una inferencia a la vez en todo el proceso | Operación permanente, en la fase de integración |

El Explorador 0.4.8 y el Investigador 0.5 **quedan intactos**, como referencia. El agente es un paquete nuevo
(`constructor_ia/agente_datos/`) que los importa.

### Lo que enseñaron los intentos anteriores

1. **El agente libre con herramientas no funcionó con Qwen3 4B en CPU (Explorador 0.3–0.4.8).**
   - En cada paso, el modelo tenía que planificar, elegir la herramienta y escribir sus argumentos.
   - Cada versión agregó reparaciones alrededor del modelo. Aun así, la última prueba real (0.4.7,
     `M20261019-4c8c37`) terminó con **0 consultas exploratorias en 14 minutos**.
2. **Las microtareas conducidas por un coordinador sí funcionaron (piloto, ciclo v1.3.x).**
   - El coordinador decide el flujo, el modelo responde una pregunta acotada y cada propuesta se verifica.
   - Un juez con datos propios midió **0 confirmaciones falsas en las corridas reales del ciclo** (X01 y X07), con
     confirmaciones verdaderas en X01 y X07.
3. **Una preferencia sin evidencia ancla al modelo.** En X07, con la misma semilla, la sugerencia 17 lo llevó a la 17;
   sin sugerencia, encontró la 12.
4. **Otros errores del modelo:** copia constantes de la evidencia, da dos propuestas en una respuesta y declara
   efectos que no son valores. Todo esto lo detecta la verificación; no se le pide al modelo que se autoevalúe.
5. **Lo que el sistema puede calcular no se le pide al modelo.** El esquema, los perfiles, las relaciones por
   contención de valores y el alcance son deterministas.

**Principio del diseño:** el coordinador conduce, el modelo interpreta, el sistema verifica, y la memoria guarda solo
lo verificado, siempre con su procedencia.

## 2. Roles

| Rol | Quién | Qué hace | Qué NO hace nunca |
|---|---|---|---|
| **Operador** | Diego / ChatGPT | Da misiones (conexión + pregunta), autoriza bases y fases | — |
| **Coordinador** | código | Máquina de estados de la misión, presupuestos, regla de decisión fija; registra cada decisión con su porqué | Interpretar datos o inventar respuestas |
| **Cartógrafo** | código, sin modelo | Esquema (`information_schema`), perfiles por columna, relaciones por contención de valores, huellas por tabla | Llamar al modelo |
| **Intérprete** | Qwen3 4B, en microtareas | Describe tablas, elige tablas para una pregunta, formula la consulta y redacta la respuesta | Elegir herramientas, ejecutar algo, decidir el flujo o aceptar su propio resultado |
| **Intermediario** | código (`broker`) | Ejecuta solo lecturas, con todos sus controles | Escribir, aceptar varias sentencias o usar otra base |
| **Verificador** | código | Revisa el SQL antes de ejecutarlo, el resultado (alcance, invariantes, contraprueba) y la respuesta (contraste léxico) | Aceptar algo porque el modelo lo afirma |
| **Bibliotecario** | código | Guarda la memoria, controla su vigencia (huellas) y arma lo que se recuerda para cada misión | Reescribir o borrar un hecho: solo agrega historial |
| **Juez** | código, **solo en el laboratorio** | Compara con la verdad oculta del generador sintético; mide respuestas correctas y confirmaciones falsas | Ser visto por el agente (auditoría de fugas, como en el piloto) |

## 3. Herramientas

Las usa el **coordinador**. El modelo no las invoca: responde microtareas.

| Herramienta | Origen | Para qué |
|---|---|---|
| `esquema()` | 4 consultas fijas a `information_schema`, limitadas a `DATABASE()` | Tablas, columnas, tipos, índices, claves declaradas y comentarios; huella global y **por tabla** |
| `perfil(tabla)` | consultas generadas por el sistema | Filas exactas (o estimadas si es grande), y por columna: nulos, distintos, mínimo y máximo, y valores frecuentes, con el alcance registrado |
| `muestra(tabla, n)` | SQL del sistema | Pocas filas reales, entregadas al modelo como **DATOS NO CONFIABLES** |
| `relacion(a.x, b.y)` | SQL del sistema | Contención de valores: ¿los valores de `a.x` existen en `b.y`? |
| `medir(que, objetos, filtro)` | motor del Investigador | Conteos, distribuciones, rangos y tendencias sin SQL escrito por el modelo |
| `sql(select)` | SQL del modelo | Pasa por `precheck_sql`, `sqlguard`, `EXPLAIN` (estimación de filas bajo un tope) y el intermediario |
| `recordar(conexión, pregunta)` | bibliotecario | Lo que ya se sabe y es relevante, con procedencia y vigencia |
| `registrar(...)` | bibliotecario | Fichas, glosario, consultas aprendidas y fallos, con su evidencia |

## 4. Flujo de una misión

```
misión (conexión, pregunta, presupuesto)
  │
  F0 ── recordar: memoria de la conexión + huellas actuales del esquema
  │       └─ tablas sin cambios → se usa lo que ya se sabe · cambiadas → «revisar» · nuevas → descubrir
  F1 ── descubrir (cartógrafo, sin modelo): esquema, perfiles y relaciones por contención, solo de lo nuevo o cambiado
  F2 ── comprender (Intérprete M1, lotes de ≤3 tablas sin ficha vigente) ──► verificación de roles ──► fichas
  F3 ── elegir tablas (Intérprete M2: pregunta + fichas, SIN orden de preferencia) ──► verificación ──► tablas
  F4 ── consultar (Intérprete M3: MEDIR o SQL) ──► precheck · sqlguard · EXPLAIN ──► intermediario
  F5 ── verificar el resultado (alcance, invariantes genéricos, contraprueba independiente, memoria)
  F6 ── redactar (Intérprete M4) ──► contraste léxico (o respuesta determinista si falla)
  F7 ── registrar en la memoria (solo lo verificado como «comprobado»; las interpretaciones como «inferencia»)
  F8 ── informe: respuesta, certeza, evidencia (C#), qué vino de la memoria y qué es nuevo
```

### Microtareas del Intérprete

Todas tienen formato estricto, **una sola propuesta por respuesta** (si hay varias incompatibles, se rechaza sin
elegir) y la retroalimentación dice qué falló. Ninguna lleva una preferencia del coordinador sin evidencia.

| Microtarea | Recibe | Responde | Verificación (código) |
|---|---|---|---|
| **M1 describir** | Hasta 3 tablas: columnas, tipos, perfiles (P#) y 3–5 filas de muestra como datos no confiables | Por tabla: `FILA` (qué representa una fila) y el rol de cada columna (identificador, referencia a tabla.columna, tiempo, medida, categoría o texto), citando P# | Las columnas existen. Cada rol es compatible con el tipo y el perfil: tiempo → temporal o fecha legible; medida → numérica; referencia → contención comprobada (o queda «hipótesis»). Las cifras citadas tienen que estar en P# |
| **M2 elegir tablas** | La pregunta y todas las fichas vigentes, en un orden barajado con semilla registrada | `TABLAS` (≤3), `COLUMNAS` y `POR_QUE` | Existen. Están conectadas por relaciones conocidas; si no, la retroalimentación lo dice. Una tabla elegida sin columnas útiles para la pregunta queda marcada |
| **M3 consultar** | La pregunta, las fichas elegidas, los caminos de relación y las coincidencias del glosario | `MEDIR {...}` o `SQL` (un SELECT) | Antes de ejecutar: `precheck_sql`, solo las tablas elegidas (o un cambio declarado), `sqlguard` y `EXPLAIN` bajo el tope. Después: ver F5 |
| **M4 redactar** | La pregunta y el resultado verificado (datos no confiables) | `RESPUESTA` en 1–2 frases, citando las celdas | Contraste léxico (`validation`): cada cifra y valor está en el resultado. Si falla dos veces, la respuesta es la tabla del resultado, sin redacción |

### F5: verificar un resultado

Las comprobaciones son genéricas: ninguna conoce la base ni la pregunta.

- **Ejecución y utilidad:** sin error, con filas, sin recortes; si hay recorte, el alcance lo dice (reglas de 0.4.7).
- **Invariantes:**
  - un conteo o una suma no supera el total de la tabla;
  - en un ranking, el primero es realmente el máximo (se reordena el resultado);
  - una fecha pedida cae en el rango que perfiló el cartógrafo.
- **Multiplicación por JOIN** (error clásico): se cuentan las filas con y sin el JOIN (`COUNT(*)` contra
  `COUNT(DISTINCT clave)` de la tabla principal). Si los agregados se inflan, se rechaza y se explica por qué.
- **Contraprueba independiente:** el coordinador recalcula el número clave con otra formulación derivada
  mecánicamente. Por ejemplo, para «el grupo con más X», recalcula X solo para ese grupo con un `WHERE`. Si no
  coinciden, se rechaza.
- **Memoria:** si una consulta aprendida respondía lo mismo, se comparan los resultados. Una diferencia se registra
  como `difiere_tiempo` (los datos cambian); no se presume un error.

### Regla de decisión y presupuesto

- **Regla fija y registrada**, como `decidir` en el ciclo:
  - hasta 2 intentos por microtarea, con retroalimentación;
  - un rechazo en F5 vuelve a M3 con el motivo;
  - dos rechazos seguidos en la misma etapa terminan la misión como **«sin respuesta verificada»** (una abstención,
    no un fracaso oculto).
- **Presupuesto por misión:** llamadas al modelo (6 en la primera prueba real), consultas y minutos. El límite corta
  como «incompleta» y la misión se reanuda sin repetir llamadas.
- **Registro:** `llamadas.jsonl`, con los prompts por partes, para auditar fugas, y `decisiones.jsonl`, con etapa,
  entradas, regla aplicada y porqué. El panel y los informes leen de ahí.

## 5. Memoria persistente

**Dónde.** En la fase de laboratorio, un archivo SQLite propio (`agente.db`) en la carpeta de datos del laboratorio,
como en el piloto. **No** es el `constructor.db` productivo. Tiene las tablas `explore_*`, que necesitan el
intermediario y el mapa, más las nuevas `mem_*`.

| Nivel | Tabla | Contenido | Vigencia |
|---|---|---|---|
| 1. Estructura | `explore_snapshots` (existe) | Esquema y huella global; **nuevo:** huella por tabla | Se compara en F0 de cada misión |
| 2. Mediciones | `explore_queries` (existe) | SQL ejecutado, firma, alcance, hash del resultado, fecha | Permanente (historial) |
| 3. Hechos | `explore_facts` + historial (existe) | `comprobado`, `inferencia`, `hipótesis` o `descartado`, con evidencia y misión creadora | Nunca se reescriben; las reevaluaciones se agregan |
| 4. Fichas de tabla | `mem_fichas` (nueva) | Qué es una fila, roles de las columnas y relaciones, con evidencia (P#, C#), la huella de la tabla al crearla y las confirmaciones (usada en respuestas verificadas) | `vigente`; `revisar` si cambió la huella; `obsoleta` si la tabla desapareció |
| 5. Glosario | `mem_glosario` (nueva) | Término de una pregunta («vendió», «marzo», «cliente») → tabla.columna, respaldado por una respuesta verificada | Usos y contradicciones; sube o baja con la evidencia, nunca se borra |
| 6. Consultas aprendidas | `mem_consultas` (nueva) | Pregunta, tablas, MEDIR o SQL, firma, huella del esquema, hash del resultado, verificación y veredicto del juez (laboratorio) | Plantilla: **siempre se vuelve a ejecutar y verificar**, nunca se responde «de memoria» |
| 7. Fallos | `mem_fallos` (nueva) | Qué se intentó, en qué etapa y por qué falló | Para no repetirlo; se muestra como «intentos fallidos» |
| 8. Bitácora | `mem_misiones`, `mem_decisiones` (nuevas) | Misiones, estados, decisiones y costo | Permanente |

**Reglas:**
- **Solo lo verificado es `comprobado`.** Lo que interpreta el modelo es `inferencia`, con evidencia. Una ficha
  confirmada por respuestas verificadas sigue siendo una interpretación respaldada, no un hecho del sistema.
- **No se borra nada:** cada cambio queda en el historial (como desde 0.4.5).
- **Vigencia por huella de tabla:**
  - si una tabla cambia, sus fichas pasan a `revisar` y sus consultas aprendidas a «a revalidar»;
  - el cartógrafo vuelve a perfilar **solo** esa tabla;
  - los hechos de datos llevan fecha, porque los datos cambian aunque el esquema no.

**Recuperación (F0), determinista y sin dependencias externas.** Se buscan coincidencias léxicas (sin acentos, con
raíces simples) entre la pregunta y el glosario, las fichas y los nombres de columnas. El resultado se acota en tokens
con `context.py`.
- Se presenta como «LO QUE YA SE SABE», con su procedencia («F12, de la misión M…, confirmada 2 veces»).
- **No se presenta como una sugerencia.** Una preferencia aparece solo si tiene evidencia, y dice cuál: por ejemplo,
  «esta tabla respondió una pregunta verificada que usaba el término X». Es la lección del experimento de
  sugerencias.

## 6. Operación permanente

Se llega por fases, para no tocar el servicio productivo antes de tiempo:

1. **Laboratorio (primero).** Cada misión corre como un proceso aparte, desde el checkout separado, con su carpeta de
   datos, su MariaDB privado y su `agente.db`. Un proceso nuevo por misión **es** la prueba de reinicio: lo único que
   sobrevive es la memoria en disco.
2. **Agente residente (después).** Un proceso `agente-servir` toma misiones de una bandeja persistente: la cola de
   `agente.db`, con las mismas transiciones atómicas que `missions.py`.
   - Una misión a la vez.
   - Si el proceso se reinicia, la misión en curso queda `interrumpida` y se reanuda sin repetir llamadas.
   - La memoria se carga en cada misión, no se guarda en RAM.
3. **Integración (solo con su autorización).** El agente pasa a ser un tipo de misión del servicio
   `constructor-ia.service`, con `InferenceGate`, el panel y `update.sh`, como se hizo con el Explorador.

**Una decisión que es de ustedes:** Ollama CPU es uno solo. Hasta la integración, un proceso residente separado del
servicio no puede coordinar las inferencias con él.
- **Opción A (recomendada):** que el residente corra solo con el servicio detenido, como el piloto, con el script
  protegido.
- **Opción B:** aceptar que compitan, con la guardia de memoria de V2.

## 7. Seguridad

- **Sin bases productivas en esta etapa.** Solo bases sintéticas en el MariaDB privado.
- **El mismo camino que en producción.** Igual se crea una cuenta de solo lectura en el MariaDB privado y el
  intermediario la verifica: cuando llegue el momento de una base real, cambia la conexión, no el código. Esa conexión
  real se hará con el procedimiento de cuenta restringida que ya existe (EXPLORADOR.md) y con autorización explícita.
- **Datos no confiables** delimitados. Una instrucción plantada en la base sintética forma parte de la prueba.
- **No se ejecuta código generado:** el agente solo produce SQL, y el sandbox no interviene. Si más adelante vuelven
  los instrumentos en Python, pasan por el sandbox, como en 0.6.
- **El juez y la verdad del generador nunca llegan a los prompts:** auditoría de fugas sobre `llamadas.jsonl`.

## 8. Prueba mínima de punta a punta

### Base B1 (sintética, determinista por semilla)

Un generador nuevo produce 6 tablas con nombres descriptivos en español:
- clientes, productos, categorías, pedidos y líneas de pedido;
- un **señuelo**: pedidos en borrador, con nombres parecidos y filas que no son ventas confirmadas.

Siembra:
- una clave foránea declarada y otra solo por convención de nombres;
- una columna casi toda nula;
- un texto con una instrucción maliciosa;
- fechas en un rango conocido.

La **verdad** queda en un archivo que ve solo el juez: qué representa cada tabla, las relaciones y las respuestas de
cada pregunta, calculadas en Python a partir de las filas generadas, sin SQL.

Una variante **B2**, con nombres opacos (`t3.c7`), queda para la etapa siguiente: comprueba que el agente entiende por
la evidencia y no por los nombres.

### Misiones

| # | Proceso | Memoria | Pregunta | Tablas necesarias |
|---|---|---|---|---|
| M1 | A | vacía | «¿Qué producto vendió más unidades en marzo de 2026?» | pedidos, líneas de pedido, productos (el señuelo tienta) |
| — | fin del proceso A | | `PRAGMA integrity_check` sobre `agente.db` | |
| M2 | B (nuevo) | la de M1 | «¿Qué cliente gastó más en marzo de 2026?» | pedidos y líneas de pedido (ya conocidas), clientes (nueva) |
| M2′ | C (nuevo) | **vacía**, misma semilla | la misma que M2 | control sin memoria |
| M3 | D (nuevo) | la de M2 | la de M1, después de agregarle una columna a productos | detecta el cambio y vuelve a perfilar solo productos |

### Criterios, fijados antes de correr

- **Métrica principal:** 0 confirmaciones falsas, es decir, ninguna respuesta que el coordinador dé por verificada y
  el juez marque como incorrecta.
- **M1 y M2:** correctas según el juez.
- **M2 contra M2′:** M2 es igual de correcta, con **menos llamadas y menos consultas**.
  - No repite consultas de descubrimiento sobre tablas sin cambios.
  - No llama a M1 para las tablas con ficha vigente.
  - Su informe lista qué usó de la memoria y de dónde salió.
- **M3:** el cambio de huella se detecta, solo la ficha de productos pasa a `revisar`, solo esa tabla se vuelve a
  perfilar, y la respuesta sigue siendo correcta.
- **Memoria auditable:**
  - cada ficha, término y consulta aprendida tiene evidencia y misión creadora;
  - los fallos quedan registrados;
  - la auditoría de fugas da 0.

### Dos niveles de prueba

1. **Automática, sin Qwen.** Usa el sandbox, el MariaDB privado y un modelo de guion. Valida el **circuito**:
   memoria, vigencia, reinicio, verificación, juez y fugas. **No demuestra inteligencia.**
2. **Real, en la VivoBook.** Con Qwen3 4B, comandos protegidos y un presupuesto de ≤ 6 llamadas por misión (unos
   20–30 min cada una). Recién ahí se sabe si el modelo describe, elige y consulta bien.

### Lo que esta prueba no puede demostrar

- **Errores semánticos.** Las invariantes y la contraprueba detectan errores de SQL (JOIN que multiplica, recortes,
  rankings mal ordenados), no los de interpretación: por ejemplo, usar el señuelo con una consulta impecable. En el
  laboratorio eso lo ve el juez; en producción, un humano.
- **Paráfrasis.** La recuperación léxica no reconoce paráfrasis sin palabras en común.
- **Una base real.** Una base sintética de 6 tablas no es una base productiva: el tamaño, la suciedad y los nombres
  reales serán otra prueba.

## 9. Plan propuesto

| Fase | Contenido | Inferencias |
|---|---|---|
| 0 | Este diseño, revisado y aprobado | no |
| 1 | Generador B1 con verdad y juez; `mem_*` en `agente.db`; cartógrafo con huella por tabla; coordinador con microtareas y regla de decisión; prueba automática completa (M1, M2, M2′, M3) con modelo de guion | no |
| 2 | En la VivoBook: M1, reinicio, M2 y M2′ con Qwen, con comandos protegidos | sí, con presupuesto |
| 3 | Agente residente (bandeja persistente) y M3; después, B2 con nombres opacos | sí |
| 4 | Integración en el servicio y el panel | solo con autorización |
| 5 | Una base real, con cuenta de solo lectura | solo con autorización explícita |

## 10. Decisiones para ustedes

1. **Consultas: ¿MEDIR (contrato) o SQL libre?** Recomiendo los dos, con MEDIR primero porque es más seguro para un
   modelo 4B. El SQL libre queda permitido, con la verificación completa.
2. **¿Dónde vive la memoria?** Recomiendo un archivo propio (`agente.db`) durante el laboratorio, y migrarlo al
   servicio en la integración.
3. **Agente residente y Ollama CPU:** opción A (con el servicio detenido) u opción B (§6).
4. **Explorador 0.4.8 e Investigador 0.5:** recomiendo dejarlos intactos como referencia, sin extenderlos.

**Fuera de alcance por ahora:** proponer aplicaciones o tableros (lo hacía el Explorador) y el agente libre que elige
herramientas.
