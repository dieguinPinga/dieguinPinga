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

### F5: verificar — dos capas separadas (ajustado tras la revisión del diseño)

**Un SQL correcto no implica una respuesta correcta.** Por eso hay dos verificaciones distintas, y la respuesta lleva
el nivel que corresponde a lo que efectivamente se pudo comprobar.

**1. Verificación técnica de la consulta** (¿es válida y su resultado es aritméticamente coherente?):
- **antes de ejecutar:** nombres reales, solo lectura, solo las tablas elegidas y costo estimado (`EXPLAIN`);
- **después:** ejecución sin error, sin recorte y con filas;
- **contrapruebas derivadas mecánicamente del plan MEDIR.** Todas son **matemáticamente válidas para cualquier base**:
  no suponen nada sobre los valores (por ejemplo, una SUMA puede superar la cantidad de filas: la versión anterior de
  este diseño decía «un conteo o una suma no supera el total de la tabla», que es falso para sumas):
  - **multiplicación por JOIN:** filas unidas = filas distintas de la tabla raíz. Si cada fila raíz se une con a lo
    sumo una fila de cada tabla, son iguales. Se exige solo para agregados sensibles a duplicados (conteo, suma,
    promedio);
  - **partición:** la suma de los conteos o sumas de TODOS los grupos = el total con el mismo filtro (los grupos de
    un GROUP BY particionan las filas filtradas);
  - **cotas:** si las k filas de un grupo valen entre mín y máx, su suma está entre k·mín y k·máx, y su promedio
    entre mín y máx;
  - **ranking:** el primero informado es el extremo entre todos los grupos;
  - **contraprueba del ganador:** el mismo agregado recalculado solo para ese grupo (WHERE, sin GROUP BY, ORDER ni
    LIMIT).
- **SQL libre:** no permite derivar estas contrapruebas, así que su verificación técnica queda incompleta y la
  respuesta no puede pasar de «incierta».

**2. Verificación semántica de la respuesta** (¿responde la PREGUNTA?), sin conocer el dominio:

| Chequeo | Qué comprueba |
|---|---|
| S1 período | El período de la pregunta es exactamente el intervalo efectivo del filtro. Detecta el error clásico de «entre A y B» sobre DATETIME, que deja afuera casi todo el último día |
| S2 intención | Mayor o menor, conteo y promedio coinciden con el orden y el agregado del plan |
| S3 entidad | La entidad preguntada («qué producto») corresponde a la tabla por la que se agrupa |
| S4 términos | Cada término de contenido tiene respaldo en el plan: nombres del esquema, la ficha de una tabla del plan (interpretación del modelo: **respaldo débil, se informa como tal**) o un término del glosario vinculado en una respuesta verificada. Un vínculo solo **declarado** por el modelo no es respaldo |
| S5 alternativas | Ninguna otra tabla describe lo mismo con igual o mayor respaldo. Si alguna lo hace, la elección depende de una interpretación que el coordinador no puede comprobar |
| S6 fidelidad | La respuesta redactada cita el grupo y el valor del resultado, y ningún número ajeno |
| S7 identidad | La columna de agrupación distingue filas: si no, puede juntar entidades distintas |
| S8 empate | No hay empate en el primer puesto |

**Nivel de la respuesta:**
- **rechazada** (por la capa técnica o la semántica): hay una falla. Se reintenta M3 con el motivo; si vuelve a
  fallar, la misión **se abstiene**.
- **incierta:** no hay fallas, pero algo no se pudo verificar. La respuesta se informa con la lista exacta de lo no
  verificado.
- **verificada:** todo dio ok. Lo respaldado por fichas sigue siendo interpretación del modelo; en el laboratorio,
  el juez mide si era correcta, y una «verificada» incorrecta es una **confirmación falsa**.
- **Memoria:** una consulta aprendida se vuelve a ejecutar y verificar siempre; su resultado anterior no se usa como
  respuesta.

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
| 6. Consultas aprendidas | `mem_consultas` (nueva) | Pregunta, tablas, MEDIR o SQL, huellas del esquema, hash del resultado y nivel de verificación. **El veredicto del juez NUNCA se escribe en la memoria**: el agente no puede ver si acertó (corrige la versión anterior de este diseño) | Plantilla: **siempre se vuelve a ejecutar y verificar**, nunca se responde «de memoria» |
| 7. Fallos | `mem_fallos` (nueva) | Qué se intentó, en qué etapa y por qué falló | Para no repetirlo; se muestra como «intentos fallidos» |
| 8. Bitácora | `mem_misiones`, `mem_decisiones` (nuevas) | Misiones, estados, decisiones y costo | Permanente |

**Reglas:**
- **Solo lo verificado es `comprobado`.** Lo que interpreta el modelo es `inferencia`, con evidencia. Una ficha
  confirmada por respuestas verificadas sigue siendo una interpretación respaldada, no un hecho del sistema.
- **No se borra nada:** cada cambio queda en el historial (como desde 0.4.5).
- **Dos vigencias distintas** (ajustado tras la revisión del diseño: un esquema sin cambios no significa que las
  mediciones sigan actualizadas):
  - **vigencia del esquema:** huella de la estructura de cada tabla (columnas, tipos, claves, índices). Si cambia,
    la ficha de la tabla pasa a `revisar` y se vuelve a describir (M1), y la tabla se vuelve a medir;
  - **vigencia de los datos:** huella de los datos de cada tabla (filas y dos resúmenes de todas sus celdas). Si
    cambia, con el esquema igual, se repiten las **mediciones** (perfil, contención de las relaciones que la tocan),
    pero la ficha se conserva. En tablas demasiado grandes la huella no se calcula y la vigencia de los datos se
    trata como desconocida, es decir, desactualizada;
  - en ningún caso se responde con un resultado guardado: las consultas aprendidas se vuelven a ejecutar.

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

---

## 11. Fase 1: implementación y resultados (2026-10-23, sin Qwen)

**Decisiones de Diego y ChatGPT aplicadas:**
1. MEDIR primero y SQL libre solo de lectura, por el intermediario existente;
2. memoria propia en `agente.db`;
3. opción A para Ollama CPU (para la fase 2);
4. Explorador 0.4.8 e Investigador 0.5 reutilizados **sin modificarlos**.

### Módulos (`constructor_ia/agente_datos/`)

| Archivo | Rol |
|---|---|
| `generador.py` | Base B1 con verdad oculta: 6 tablas, señuelo, relación solo por convención, columna casi nula, instrucción maliciosa en los datos, ventas del 31 de marzo que deciden la respuesta y un canario en la verdad. La verdad se calcula en Python, sin SQL. Incluye el cambio de esquema (M3) y datos nuevos con el mismo esquema |
| `cartografo.py` | Esquema (consultas e instantánea del Explorador), huella de estructura y huella de datos por tabla, perfiles de tabla completa, frecuentes, muestra, y relaciones candidatas con contención y unicidad medidas |
| `medir.py` | Contrato MEDIR (agregado, medida, agrupar, filtros, orden y límite) y su compilador. Los JOIN siguen solo relaciones comprobadas, primero muchos→uno; calcula los intervalos efectivos de cada filtro y genera las consultas de contraprueba |
| `microtareas.py` | M1 a M4: prompts por partes y lectura estricta, con una sola propuesta. Los datos y los perfiles van delimitados como no confiables, y las fichas en orden barajado con semilla registrada |
| `verificador.py` | Capa técnica (previa, posterior e invariantes) y capa semántica (S1 a S8), separadas, y el nivel resultante |
| `memoria.py` | `agente.db`: migraciones existentes más las tablas `mem_*` (misiones, decisiones, mediciones, relaciones, fichas, glosario, consultas aprendidas y fallos) |
| `coordinador.py` | Misión F0–F8, regla de decisión, presupuesto de llamadas, registro (`decisiones.jsonl`, `llamadas.jsonl`) e informe (`informe.json`, `informe.md`) |
| `juez.py` | Evalúa cada informe contra la verdad oculta y audita fugas (canario y descripciones de la verdad) en todo lo que produjo el agente |
| `guion.py` | Modelo de guion para las pruebas: responde leyendo solo su prompt y tiene perfiles negativos |
| `__main__.py` | `python -m constructor_ia.agente_datos mision …`: una misión por proceso |

### Diferencias con el diseño (decididas al implementar)

- **El contrato MEDIR es propio del agente.** El del Investigador no expresa agregados agrupados sobre varias tablas;
  se tomó su idea («qué medir sobre qué») y su escape de literales.
- **La fase 1 no usa `explore_facts`.** Las relaciones y mediciones van en `mem_*`, con su propia vigencia. Del mapa
  se usan las instantáneas de esquema, y del intermediario, el registro de consultas.
- **Primer contacto:** el agente describe **todas** las tablas sin ficha, no solo las relevantes a la pregunta. Por
  eso, en M2, las 6 fichas ya existen.
- **Consulta aprendida:** se reconoce solo cuando la pregunta normalizada es idéntica. Las paráfrasis no se reconocen.
- **Presupuesto agotado:** la misión se abstiene. Todavía no se reanuda una misión cortada.
- **Pendiente para las fases 2 y 3:** el agente residente y el adaptador de Qwen.

### Resultados (modelo de guion, MariaDB privado, cada misión en un proceso distinto)

| Misión | Nivel | Correcta (juez) | Llamadas | Consultas | Tablas medidas | Fichas reutilizadas | Consulta aprendida |
|---|---|---|---|---|---|---|---|
| M1 (proceso A, memoria vacía, Q1) | verificada | sí | 5 (M1×2, M2, M3, M4) | 41 | 6 | 0 | no |
| M2 (proceso B, con la memoria de M1, Q2) | incierta | sí | 3 (M2, M3, M4) | 16 | 0 | 6 | no |
| M2′ (proceso C, sin memoria, Q2) | incierta | sí | 5 (M1×2, M2, M3, M4) | 41 | 6 | 0 | no |
| M3 (proceso D, columna nueva en productos, Q1) | verificada | sí | 2 (M1, M4) | 23 | 1 | 5 | sí |
| Datos nuevos (proceso E, mismo esquema, Q1) | verificada | sí | 1 (M4) | 25 | 2 | 6 | sí |

- **M2 y M2′ quedan «inciertas» a propósito.** Para «gastó», el único vínculo con cantidad × precio_unitario es el
  que declara el modelo, y eso no es respaldo. La respuesta es correcta, pero se informa como no verificada.
- **Memoria:**
  - M2 tuvo la misma respuesta que M2′, con **2 llamadas y 25 consultas menos**;
  - M3 volvió a medir y describir **solo** productos;
  - con datos nuevos, se volvieron a medir **solo** pedidos y líneas de pedido, se conservaron las fichas y se volvió
    a ejecutar la consulta aprendida.
- **Pruebas negativas:**

  | Caso | Lo que pasó |
  |---|---|
  | Señuelo | Usó los borradores y dio el producto equivocado, pero como **incierta** (S5: lineas_pedido describe lo mismo). Para el juez es un error declarado, **no** una confirmación falsa |
  | Filtro «entre el 1 y el 31» en DATETIME | Rechazado por S1 con la explicación; el guion lo corrigió en el segundo intento |
  | Mismo error, sin corregir | Abstención; el fallo quedó en la memoria |
  | JOIN que multiplica filas | Rechazado dos veces por la capa técnica; abstención |
  | SQL libre | Incierta: las contrapruebas no se pueden derivar |
  | Respuesta ambigua | Rechazada sin elegir |
  | Consulta aprendida ilegible | Siguió por el camino normal |
  | Relación que desaparece | Se retiró solo esa relación |
  | Cuenta con INSERT | Misión bloqueada antes de cualquier llamada |

- **Seguridad:**
  - el canario y las descripciones de la verdad no aparecen en ningún archivo del agente;
  - la instrucción maliciosa solo aparece dentro de bloques de datos no confiables;
  - la base explorada no cambia;
  - 0 confirmaciones falsas en todas las misiones.

**Lo que esto NO demuestra:**
- Ninguna capacidad de Qwen: el guion está escrito para esta base.
- Que la capa semántica sea suficiente. El respaldo por fichas es interpretación del modelo, y la fase 2 va a medir
  cuántas confirmaciones falsas deja pasar.
- Que las raíces léxicas reconozcan sinónimos o paráfrasis.
- Nada sobre bases grandes o reales.

### Próximo paso (fase 2, cuando lo autoricen)

Falta, antes de usar Qwen:
- un adaptador del modelo (`laboratorio/protocolo.Modelo` con la guardia de memoria de V2, como en el piloto);
- un verbo protegido en `scripts/piloto_etapas.sh` que detenga el servicio y lo restaure siempre (opción A).

Con eso, en la VivoBook: M1, reinicio, M2 y M2′ con Qwen y un presupuesto de llamadas por misión, sobre una base
sintética en el MariaDB privado.

---

## 12. Fase 2: Qwen3 4B en la VivoBook — preparación (2026-10-23, sin inferencias)

### Qué se agregó

| Pieza | Qué hace |
|---|---|
| `agente_datos/qwen.py` | **Adaptador** para M1–M4. Reutiliza sin cambios `protocolo.Modelo` con los parámetros del piloto (temperatura 0.2, `num_ctx` 4096, `keep_alive` 0s, respuesta corta de hasta 768 tokens, plazo de 900 s) y la envoltura V2 (guardia de memoria de 3900 MiB antes de cada llamada, control de contexto con la reserva de respuesta). Una falla de Ollama se reintenta una vez y, si se repite, la misión queda **interrumpida**. La guardia de memoria también la interrumpe. Un pedido que no entra en el contexto es un **error** del coordinador, y no se envía |
| `coordinador.py` | **Estado** de la misión separado del **nivel** de la respuesta: `completa`, `interrumpida`, `error` o `bloqueada`. Una misión interrumpida no tiene nivel (`sin_resultado`) y nunca cuenta como abstención. Si M1 no entra en el contexto, el lote baja de 3 tablas a 2 o 1. Registra tokens (entrada, generados y tiempo del modelo), respuestas truncadas, rechazos e interrupciones del entorno |
| `__main__.py` | `--modelo qwen` con sus parámetros. Ctrl-C, SIGTERM o la muerte del orquestador cierran la misión como interrumpida y dejan su informe. Códigos de salida: 0 completa, 3 error o bloqueada, 4 interrumpida |
| `experimento.py` | El orquestador: `preparar`, `comprobar`, `mision` y `resumen` (ver abajo) |
| `cli.py` | `laboratorio-agente --preparar \| --comprobar \| --mision M1\|M2\|M2p \| --estado` |
| `scripts/piloto_etapas.sh` | `agente0` (sin modelo; no detiene nada; no acepta `--mision`) y `agente [minutos] --mision …`, protegido como `ciclo`: detiene SOLO constructor-ia, arma el temporizador de respaldo y lo restaura siempre |

### Cómo garantiza las condiciones

- **Juez aparte.** La verdad oculta (`juez/verdad.json`, 0600) la genera `--preparar` y solo la lee el
  orquestador DESPUÉS de la misión. El agente corre en **otro proceso**, que no recibe la verdad ni su ruta. Al
  final se auditan el canario y las descripciones de la verdad en su memoria, sus registros y su conexión.
- **Mismos datos sintéticos.** En cada misión, B1 se vuelve a crear desde la semilla en un MariaDB privado NUEVO
  (sin red, desechable). Su huella (CHECKSUM y filas de cada tabla) tiene que ser idéntica a la del plan; si no, no
  se ejecuta y no se marca ninguna ranura.
- **Memoria.** M1 empieza con una memoria vacía (`memoria_m1_m2`). M2 usa esa misma memoria y exige que M1 haya
  terminado **completa**. M2′ empieza con otra memoria vacía (`memoria_m2p`).
- **Sin duplicaciones silenciosas.** Cada ranura se ejecuta una sola vez por carpeta:
  - una ranura completa, interrumpida, con error o que quedó «en_curso» (porque el proceso murió) no se repite ni se
    continúa;
  - un proceso del agente que falla sin dejar informe y sin que nadie lo haya interrumpido (por ejemplo, porque no
    arrancó) queda como **error**, no como interrumpida;
  - el mensaje dice por qué;
  - para otra corrida: otra carpeta y `--preparar`.
- **Sin agentes huérfanos.** Si el orquestador muere, el proceso del agente recibe SIGTERM, cierra la misión como
  interrumpida y no sigue llamando a Ollama después de que el script protegido restauró el servicio.
- **Registro.** Por misión, en la memoria del agente: `llamadas.jsonl` (prompts por partes, respuesta cruda,
  semilla, tokens, tiempo y memoria), `decisiones.jsonl` (reglas, rechazos y su motivo), `informe.json`/`.md`
  y `explore_queries` en `agente.db` (cada consulta). En la carpeta del experimento:
  - `resultados/<R>.json`, con el **nivel del coordinador** (verificada o incierta) y el **veredicto del juez**
    (correcta, confirmación falsa, error declarado) **separados**;
  - `registros/<R>.err`, lo que escribió el agente en vivo;
  - `resumen.md`.

### Limitación conocida: no hay reanudación

Una misión interrumpida (corte de Ollama, guardia de memoria, límite de tiempo, Ctrl-C) **no se continúa**:
- queda registrada como interrumpida, sin veredicto;
- su ranura no se puede volver a correr en esa carpeta;
- si fue M1, M2 queda bloqueada (su memoria sería parcial).

Para repetir: `agente0 --preparar --carpeta <otra>` y usar `--carpeta <otra>` en cada comando. La comparación M2 contra
M2′ solo es válida dentro de una misma carpeta (mismos datos y misma memoria de M1).

### Pruebas (sin inferencias)

`tests/test_agente_fase2.py` (12). Usa un Ollama SIMULADO por HTTP que responde con el guion, así se prueban el
cliente real, el modelo del laboratorio, la guardia V2 y el control de contexto. Comprueba:
- **adaptador:** los parámetros del piloto y la guardia; la misión completa por HTTP (opciones enviadas, semilla por
  pedido, tokens registrados, todos los pedidos de B1 dentro de `num_ctx` 4096 con la reserva);
- **interrupciones:** dos fallas de Ollama → interrumpida sin respuesta ni consulta aprendida; un contexto demasiado
  chico → error sin enviar nada; Ctrl-C durante una llamada → código 4 e informe interrumpido; un orquestador muerto
  con kill -9 → el agente huérfano cierra su misión;
- **orquestador:** M1, M2 y M2′ con los mismos datos y los veredictos separados (M2 y M2′: correctas para el juez,
  inciertas para el coordinador); M1 no se repite; M2 bloqueada si M1 no está completa o quedó interrumpida; una
  ranura «en_curso» de un proceso muerto cuenta como interrumpida; datos distintos frenan antes de marcar nada; la
  comprobación sin modelo (incluida la de Ollama solo con `/api/tags`, sin inferencias).

Además se probó el camino real de la VivoBook en este entorno (`scripts/piloto.sh`, MariaDB privado verdadero, modelo
de guion): `--preparar`, `--comprobar`, M1 (verificada y correcta), M1 repetida (rechazada), M2 y M2′ (inciertas y
correctas). Eso destapó un límite real: si la carpeta es muy profunda, el socket del MariaDB privado supera los 107
bytes. Ahora se usa una carpeta temporal corta cuando hace falta; en la VivoBook la ruta por omisión entra.

### Procedimiento en la VivoBook: SOLO M1

Desde el checkout aparte (`E=~/src/dieguinPinga/constructor-ia/scripts/piloto_etapas.sh`):

```bash
cd ~/src/dieguinPinga && git fetch origin claude/constructor-ia-0.7-piloto-microtareas \
  && git checkout claude/constructor-ia-0.7-piloto-microtareas && git pull --ff-only
bash $E huellas antes
bash $E agente0 --comprobar      # SIN modelo, no detiene nada: MariaDB privado, solo lectura, escritura rechazada,
                                 # misión de prueba con el guion en otro proceso, juez, fugas y /api/tags de Ollama
bash $E agente0 --preparar       # base B1 + verdad oculta + huella (una sola vez por carpeta)
bash $E agente0 --comprobar      # otra vez: ahora también confirma que los datos son idénticos a los del plan
bash $E agente 60 --mision M1    # PROTEGIDO: detiene solo constructor-ia, temporizador de respaldo, lo restaura siempre
bash $E agente0 --estado
bash $E huellas despues && bash $E comparar
```

Todo queda en `~/constructor-ia-piloto/data/laboratorio/agente-fase2/`. **No borrar la carpeta**: M2 usará esa
misma memoria. Para revisar M1, mandar:
- `resumen.md`, `resultados/M1.json` y `registros/M1.err`;
- de `memoria_m1_m2/misiones/<misión>/`: `llamadas.jsonl`, `decisiones.jsonl` e `informe.json`.

M2 y M2′ (`bash $E agente 60 --mision M2` y `--mision M2p`) quedan para después de revisar M1.

---

## 13. Fase 2b: lo que enseñó la primera M1 real y su corrección (2026-10-23, sin inferencias)

### La primera M1 con Qwen3 4B (carpeta original `agente-fase2`, conservada)

- **Resultado:** completa, en **abstención**. 5 llamadas (M1×2, M2, M3×2), sin fugas ni interrupciones.
- **Reproducción:** con las respuestas reales, los 5 prompts salen idénticos byte a byte a los de la VivoBook.
- **Fichas:** las 6 de M1 fueron correctas.

Problemas:
1. **M3 escribió la medida en la línea fija** («MEDIR: suma …» en vez de «MEDIDA: suma …»). La retroalimentación
   («falta MEDIDA») no citaba la respuesta ni mostraba la forma correcta.
2. **M2 omitió `pedidos`**, la única tabla con la fecha de las ventas. El coordinador solo comprobaba existencia y
   conexión, así que M3 nunca vio una columna de fecha y omitió el período.
3. **Hallazgo adicional:** con las fichas que escribe Qwen, «vendió» y «unidades» no tienen respaldo en el esquema. El
   guion de la fase 1 sobrestimaba ese respaldo.

### Corrección (aprobada por Diego y ChatGPT)

- **Requisitos por regla, antes de avanzar** (`requisitos.py`). De la pregunta se deducen el período (intervalo
  exacto), el orden, el límite (singular → 1; un número explícito → ese número) y la entidad. **Nunca se inventan.**
  Si la regla no puede determinarlos sin ambigüedad, la misión se abstiene pidiendo una **aclaración**, sin llamar a M2.
  Ejemplos de esos casos:
  - un mes sin año;
  - varios períodos;
  - «la semana pasada»;
  - mayor y menor a la vez;
  - «más de 100»;
  - varios resultados sin decir cuántos.
- **Cobertura después de M2.** Si la pregunta pide un período, una entidad o una medida y las tablas elegidas no lo
  cubren, M2 se rechaza ANTES de pasar a M3. La retroalimentación lista las columnas que sí lo cubren en tablas
  conectadas, sustentadas por el esquema y las relaciones comprobadas: todas, en orden alfabético y sin preferencia
  (el señuelo también aparece).
- **M3:**
  - **sin la línea fija** `MEDIR: agregado`;
  - **formato armado con las columnas reales** de las tablas elegidas: todas las de cada rol, en orden alfabético
    (`<una de: a | b | c>`), sin elegir ninguna;
  - **requisitos explícitos**: el FILTRO del período con sus fechas exactas, el ORDEN y el LIMITE.
- **Normalizaciones de forma, registradas y nunca de contenido:**
  - `MEDIR: suma t.c` sin una línea MEDIDA se lee como MEDIDA. `MEDIR: suma` sin columna sigue siendo inválido, y una
    MEDIR distinta de la MEDIDA es una ambigüedad;
  - una columna sin tabla se completa solo si es única entre las tablas elegidas;
  - ORDEN y LIMITE ausentes se completan con lo que la regla dedujo. Si no coinciden con la pregunta, S2 falla.
- **Requisitos del plan antes de ejecutar.** Si falta el FILTRO del período o AGRUPAR, el plan se rechaza sin ejecutar
  SQL.
- **Retroalimentación que enseña:** cita las líneas de la respuesta anterior, dice qué falló y cómo corregirlo.
- **«Verificada» no se apoya solo en interpretaciones del modelo.** En S3 y S4, un término respaldado solo por una ficha
  escrita por Qwen queda «no verificable» y la respuesta queda **incierta**. Respaldan el esquema o el glosario
  vinculado en respuestas verificadas; un vínculo solo declarado no respalda. «Verificada» sigue siendo alcanzable
  cuando el esquema respalda los términos y no hay una tabla alternativa con igual respaldo.
- **Habilitación de M2 en el laboratorio.** M2 exige que M1:
  - haya terminado completa;
  - haya dado una respuesta (verificada o incierta, no abstención);
  - sea **correcta según el juez**, sin confirmación falsa.

  El criterio queda en `plan.json`. Lo aplica el orquestador; **el veredicto del juez y su verdad no llegan a los
  prompts ni a la memoria del agente** (hay pruebas que lo verifican).
- **Sin cambios:** S7 (por pedido, queda para otra revisión), el Explorador 0.4.8, el Investigador 0.5, el laboratorio
  y los servicios.

### Pruebas (sin inferencias)

`tests/test_agente_requisitos.py` (10), con las respuestas REALES de M1 (`tests/datos/llamadas-M1-fase2.jsonl`):
- **Prompts de M1:** reproducidos idénticos a los de la VivoBook, con las mismas fichas.
- **M2 real** (`lineas_pedido, productos`): rechazada por cobertura, con `pedidos.fecha` y `pedidos_borrador.creado`
  como alternativas neutrales. Repetida, se rechaza de nuevo y **no se llega a M3**.
- **M3 reales:** `MEDIR: suma` sigue rechazada por contenido, con la cita. `MEDIR: suma lineas_pedido.cantidad` se
  normaliza (queda registrado), y la falta del FILTRO se detecta antes de ejecutar, con la corrección exacta.
- **Prompt nuevo de M3:** sin la línea fija; requisitos y opciones completas en orden alfabético.
- **Guion `qwen_m1`** (imita los errores reales y sigue la retroalimentación): correcta e incierta, con 6 llamadas; la
  consulta aprendida se guarda en forma canónica.
- **Guion `qwen_m1_terco`:** abstención sin llegar a M3 y sin confirmación falsa.
- **Pregunta sin año:** se pide una aclaración y no se eligen tablas.
- **Criterio de M2:** abstención, incorrecta, confirmación falsa e interrumpida bloquean; correcta (verificada o
  incierta) habilita. Una M1 en abstención bloquea M2 en el orquestador, y el veredicto no aparece en la memoria.

Se ajustaron las expectativas de las pruebas de las fases 1 y 2: lo que antes era «verificada» solo por la ficha ahora
es «incierta». Se agregaron pruebas de que «verificada» se alcanza con respaldo del esquema o del glosario verificado,
y de que el límite se controla contra la pregunta.

**Lo que esto no demuestra:** que Qwen elija bien las tablas o escriba bien M3 con los prompts nuevos. Eso lo dice la
próxima corrida real.

### Próxima corrida en la VivoBook (carpeta NUEVA; la original queda intacta)

```bash
cd ~/src/dieguinPinga && git pull --ff-only
C=~/constructor-ia-piloto/data/laboratorio/agente-fase2-b
bash $E huellas antes
bash $E agente0 --preparar --carpeta $C
bash $E agente0 --comprobar --carpeta $C
bash $E agente 60 --mision M1 --carpeta $C
bash $E agente0 --estado --carpeta $C
bash $E huellas despues && bash $E comparar
```

Si M1 da una respuesta correcta según el juez, el orquestador habilita M2 (`bash $E agente 60 --mision M2 --carpeta $C`)
y después M2′ (`--mision M2p`). Si no, M2 queda bloqueada con el motivo.

## 14. Fase 2c: medida insuficiente, clave ≠ etiqueta y sin alias internos (2026-10-23, sin inferencias)

### La segunda corrida real de M2 (carpeta original `agente-fase2-b`, conservada)

- **Resultado:** completa, **incierta**. 4 llamadas, 16 consultas. Juez: **incorrecta**, sin confirmación falsa.
- **Plan de Qwen:** `MEDIDA: suma lineas_pedido.precio_unitario`, `AGRUPAR: clientes.id`, con el filtro de marzo, el
  orden y el límite correctos, e `INTERPRETA: cliente = clientes.id`. El cliente era el correcto (59, Lucía Castro);
  el valor no: 60996.72 en lugar de 400521.93 (el gasto es la suma de cantidad × precio unitario).
- **Respuesta:** Qwen escribió dos veces «El cliente g1 gastó 60996.72». S6 las rechazó y el coordinador usó la
  respuesta determinista `g1 = 59; valor = 60996.72`, que muestra un alias interno y la clave en lugar del nombre.

Tres problemas: una medida matemáticamente correcta que mide otra cosa; la clave de agrupación confundida con lo que
se muestra; alias internos que llegan al usuario.

### Corrección (aprobada por Diego y ChatGPT)

**A1 — interpretación declarada de los conceptos de la medida** (`requisitos.py`):
- Los **conceptos de la medida** son las palabras de contenido que quedan al quitar el período, el orden, el límite
  y la entidad, que ya tienen sus propios controles: «gastó» en Q2, «vendió» y «unidades» en Q1. Tampoco cuentan
  las palabras que el plan usa como valor de un filtro por igualdad («Rosario» en `FILTRO: clientes.ciudad = Rosario`).
- M3 tiene que escribir una línea `INTERPRETA` para cada concepto. En un plan MEDIR, esas líneas tienen que decir lo
  mismo que la MEDIDA:
  - no pueden citar columnas que no estén en la MEDIDA;
  - entre todas tienen que cubrir todas las columnas de la MEDIDA;
  - si nombran una función, tiene que ser la del plan.
- En SQL libre solo se exige que estén declaradas.
- Si falta una o no es coherente, el plan se rechaza **antes de ejecutar**, con una retroalimentación que cita la
  respuesta y muestra la forma.
- La declaración es una **hipótesis** de Qwen, nunca una verificación: se guarda en el glosario como «declarada».

**A2 — sensibilidad a la medida** (`sensibilidad.py`):
- Se aplica cuando el plan es un ranking agrupado con suma, promedio, máximo o mínimo sobre una columna, y solo si los
  demás chequeos no fallaron. El coordinador vuelve a calcular el mismo ranking con otras medidas plausibles.
- **Las alternativas son acotadas y justificadas por tipos, roles y relaciones:**
  - columnas con rol «medida» en la ficha (sin ficha: numéricas que no son clave ni referencia);
  - de la tabla raíz del plan y de las tablas que el plan ya une en sentido muchos→uno;
  - cada columna sola, y cada producto de dos con al menos una de la raíz;
  - **solo de la misma magnitud** que la medida del plan;
  - siempre con la misma función, los mismos filtros, los mismos grupos y el mismo orden.
- **Alternativa plausible ≠ operación posible** (corrección pedida por Diego y ChatGPT antes del commit).
  - Sumar unidades y sumar dinero son operaciones posibles sobre los mismos datos, pero responden preguntas
    distintas: si cambian el ganador, no demuestran ambigüedad, cambian la magnitud.
  - La magnitud de una medida es su **firma de tipos**, una regla genérica y no propia de B1: la clase SQL de cada
    factor.
    - **entera:** INT y similares;
    - **continua:** DECIMAL, FLOAT, DOUBLE;
    - **indicador:** una columna entera cuyos valores medidos están todos entre 0 y 1. Su suma cuenta filas, no suma
      cantidades.
  - Una suma de unidades (entera) solo se compara con otras columnas enteras. Un producto entera × continua solo se
    compara con otros productos entera × continua.
  - Las operaciones de otra magnitud se listan como **«otras magnitudes»**: no se ejecutan, no cuestan consultas y
    nunca provocan una abstención.
- **Ninguna alternativa agrega un JOIN** ni cambia las filas.
- **Límites:** el orden es determinista y hay a lo sumo 6 consultas además de la del plan. Si hay más candidatas o
  una consulta falla, la exploración queda **incompleta**, se registra y nunca se concluye que el ganador es estable.
- **Clases:**
  - ganador dependiente: alguna alternativa cambia el ganador;
  - valor dependiente: el mismo ganador con otro valor;
  - estable: el mismo ganador y el mismo valor;
  - sin alternativas;
  - incompleta;
  - no aplica.

**Política (S9)**, aplicada solo cuando la **medida no está confirmada**:

| Caso | Nivel | Qué se muestra |
|---|---|---|
| El ganador depende de la medida | **abstención**, sin reintento (otra medida no confirmaría nada) | las alternativas como diagnóstico, sin declarar cuál es correcta |
| Mismo ganador, el valor depende, o exploración incompleta | **incierta** | una respuesta determinista con el valor de cada interpretación; ninguno se presenta como confirmado (no se pide M4) |
| Mismo ganador y mismo valor | **incierta** (vía S4) | «la estabilidad no constituye respaldo semántico» |
| Sin alternativas de la misma magnitud | **incierta** (vía S4) | «no implica que la medida sea correcta», más la lista de otras magnitudes |

- **Medida confirmada:** la pregunta dice qué medir y S4 respalda **todas** sus palabras con el esquema o con el
  glosario verificado. Una ficha de Qwen o un vínculo declarado no confirman nada.
- **Con la medida confirmada,** S9 se informa y no cambia el nivel.
- **Por qué la condición:** si la abstención fuera incondicional, una pregunta como «¿qué producto tiene la mayor
  cantidad?» (el esquema dice `cantidad`) se abstendría solo porque existen otras columnas numéricas.

**Clave, etiqueta y valor, siempre separados** (`identidad.py`, `medir.py`). El coordinador decide por regla qué
identifica a cada grupo:
1. **Clave primaria** → es la clave. La etiqueta se elige aparte.
2. **Referencia comprobada** a una clave única → es la clave. La etiqueta sale de la tabla referida, si está elegida.
3. **La entidad nombra la columna** («qué ciudad» ↔ `clientes.ciudad`) → el valor es la entidad.
4. **La entidad nombra la tabla** («qué cliente» ↔ `clientes`), la columna no es la clave y hay una clave primaria →
   se agrupa por la clave y la columna queda como etiqueta. Se registra como normalización.
5. **Un valor distinto por fila en los datos actuales** → sirve como clave, pero solo por los datos.
6. **Nada de lo anterior** → la identidad queda sin verificar.

- El SQL agrupa por la clave y la etiqueta.
- La **respuesta estructurada** trae `clave`, `etiqueta`, `valor`, `medida`, `valor_confirmado` y, si corresponde, la
  sensibilidad.
- **S7** da «ok» con clave primaria, referencia comprobada, reagrupación o atributo. La etiqueta visible se informa
  aparte.
- **El juez** compara también la clave cuando la verdad la tiene: dos clientes con el mismo nombre no son la misma
  respuesta.

**Sin alias internos:**
- M4, la respuesta determinista y el informe usan nombres reales, por ejemplo `clientes.nombre`,
  `clientes.id (clave)` y `suma(lineas_pedido.cantidad × lineas_pedido.precio_unitario)`.
- **S6** rechaza `g1`, `e1` y similares, salvo que sean valores del resultado. Exige la etiqueta y, si la etiqueta se
  repite en la tabla, también la clave.

**Memoria:**
- Solo una respuesta «verificada» registra términos como verificados, y eso exige el respaldo del esquema o del
  glosario verificado.
- Una declaración repetida en varias misiones (o coincidente entre agentes) sigue siendo «declarada».
- Una abstención no deja consulta aprendida.

### Resultados (modelo de guion y respuestas reales reproducidas; sin inferencias)

| Caso | Antes de la fase 2c | Ahora |
|---|---|---|
| **Plan real de M2** (suma de precios unitarios para «gastó») | incierta, «El cliente g1 gastó 60996.72» / `g1 = 59; valor = 60996.72` | Sin INTERPRETA para «gastó», se **rechaza antes de ejecutar**. Declarada como `suma lineas_pedido.precio_unitario`: no hay otra medida continua en esas tablas (cantidad × precio es otra magnitud, listada) → **incierta** por S4. El M4 real con «g1» se rechaza dos veces; respuesta determinista `clientes.nombre = Lucía Castro; clientes.id (clave) = 59; suma(lineas_pedido.precio_unitario) = 60996.72`. Juez: incorrecta, **error declarado, sin confirmación falsa** |
| **Q1 («más unidades»), plan correcto** | incierta y correcta | **incierta y correcta; M1 se completa.** Las sumas de dinero (que darían Pincel B2, clave 22, en lugar de Balde B2, clave 34) son otras magnitudes: se listan, no se ejecutan y no provocan abstención |
| **Q2, plan correcto** (cantidad × precio unitario) — control positivo | incierta y correcta | incierta y correcta, nunca verificada |
| **«¿Qué producto facturó más?»** (cantidad × precio cobrado) | — | se compara con cantidad × precio de lista, que es la misma magnitud: mismo ganador, otro valor → incierta con el valor de cada interpretación, sin M4 |
| **Nombres repetidos** (variante del generador: el 2.º y el 3.º cliente de marzo se llaman igual y juntos superan al ganador) | agrupar por nombre habría dado «Carla Domínguez», 675355.93 | el guion agrupa por `clientes.nombre`, el coordinador reagrupa por `clientes.id` → clave 59, «Lucía Castro», 400521.93; un M4 sin la clave se rechaza; juez correcta |
| **Base opaca** (`t_x`, `t_y`, `t_w`), dos precios (`p`, `p2`) que cambian el ganador | — | misma magnitud → **abstención**, con `suma(t_w.q)` listada como otra magnitud |
| **Base opaca**, medida correcta (q·p) | — | **también abstención**: q·p2 es la misma magnitud y da otro ganador; nada confirma cuál precio pide la pregunta |
| **Base opaca**, «compró más unidades» con suma de `q` | — | el dinero da otros ganadores, pero es otra magnitud → **incierta**, sin abstención |
| **Base opaca**, ganador estable | — | incierta, clave 4 y etiqueta «Tres», con la clave citada porque `t_x.n` se repite |
| **Base opaca**, agrupada por un nombre repetido | — | la regla no puede reagrupar (nada dice que «cliente» sea `t_x`); S7 lo informa e incierta, nunca verificada |
| **Medida confirmada por el esquema** («la mayor cantidad») | — | S9 solo informa; un M4 que insiste en «g1» se rechaza dos veces; respuesta determinista con nombres reales |
| **Glosario declarado** en 3 misiones coincidentes y por otro agente | — | sigue «declarado»; S4 sigue sin respaldo |

**Consecuencia para el laboratorio:**
- La M1 preregistrada (Q1) se completa, incierta y correcta según el guion, y habilita M2 como antes.
- Las preguntas preregistradas no cambiaron.
- La ambigüedad que la primera versión de esta corrección le atribuía a Q1 era falsa: comparaba unidades con dinero.

### Limitaciones que permanecen

1. **A1 exige coherencia, no corrección.** La propuesta real con `INTERPRETA: gastó = suma
   lineas_pedido.precio_unitario` pasa A1. En esas tablas no hay otra medida continua, así que tampoco la frena la
   sensibilidad. Queda **incierta** porque «gastó» no tiene respaldo verificable, y el juez la marca incorrecta: es un
   error declarado, no una confirmación falsa. Que sea la medida equivocada lo sigue detectando solo el juez.
2. **La firma de tipos es un indicio de magnitud, no una prueba de significado.**
   - Dos columnas enteras pueden ser magnitudes distintas (unidades y días) y se compararían.
   - Dinero guardado en centavos enteros quedaría separado de un DECIMAL.
   - Productos de la misma firma pueden no tener sentido (precio × precio).
   - Lo que la firma no separa se muestra como diagnóstico; el significado sigue sin confirmar (S4).
3. **La sensibilidad no sabe cuál medida es la correcta.** No la elige: decide si la respuesta depende de una
   interpretación de la misma magnitud. Con la medida correcta y otra de la misma magnitud que cambia el ganador,
   también se abstiene (base opaca).
4. **Lo que la exploración no mira:** otras funciones (conteo frente a suma), otras tablas (los borradores siguen a
   cargo de S5), otros filtros o columnas de tablas unidas uno→muchos. En tablas con muchas medidas de la misma
   magnitud, el tope de 6 consultas deja la exploración incompleta y la respuesta incierta.
5. **«Medida confirmada» se decide por raíces de 4 letras (S4).** Una coincidencia accidental puede contar como
   respaldo, y un sinónimo no cuenta.
6. **Límites de la identidad:**
   - La reagrupación por la clave necesita que la entidad nombre la tabla. Con nombres opacos no ocurre: S7 lo
     informa, pero la respuesta puede juntar entidades distintas (queda incierta, nunca verificada).
   - Una columna única «en los datos actuales» cuenta como clave solo por los datos.
   - La etiqueta automática se elige por regla (la de más valores distintos) y puede no ser la más legible.
7. **Respuestas deterministas:** con valor dependiente no se pide M4. La respuesta es correcta en su forma, pero
   menos natural.
8. **Costo y consulta aprendida:**
   - La sensibilidad suma hasta 6 consultas por intento; las otras magnitudes no cuestan consultas.
   - La consulta aprendida no vuelve a pedir A1: la interpretación se declaró en la misión original y sigue siendo una
     hipótesis.
9. **Sin inferencias nuevas.** Las pruebas no dicen cómo responderá Qwen al requisito de INTERPRETA. Puede escribir
   una interpretación coherente y equivocada (limitación 1).

**Pruebas:** `tests/test_agente_fase2c.py` (34) y ajustes puntuales en las pruebas de las fases 1, 2 y 2b. La suite
completa: 507 pruebas, OK (624 s).

Sin cambios: el Explorador 0.4.8, el Investigador 0.5, los servicios, Vigilancia, Node-RED, OpenClaw y las bases
productivas. Las corridas `agente-fase2` y `agente-fase2-b` quedan intactas. Las corridas nuevas van en carpetas
nuevas.

## 15. Trabajo futuro: oficina de agentes distribuidos (registrado, NO implementado)

**Estado comprobado por Diego y ChatGPT (2026-10-23), fuera de este repositorio:**
- La VivoBook (servidor central, con Qwen 4B, la coordinación y la memoria) envió una microtarea de revisión de Python
  a una segunda computadora con Windows, GPU de 8 GB y Ollama con `gemma4:12b`.
- La conexión fue por Tailscale, con Tailscale Serve en un puerto propio y sin tocar el Ollama principal.
- Gemma encontró y corrigió un error de cálculo: 21 s, 71 tokens, 13,19 tokens/s, con la GPU trabajando.
- La comunicación está probada. Todavía no hay un trabajador registrado ni una cola distribuida.

**Idea: separar tres conceptos.**
- **Roles:** qué tarea se hace (intérprete, revisor, redactor…). Cada rol tiene su memoria lógica, y el conocimiento
  queda centralizado en la VivoBook.
- **Computadoras:** cada una se postula como trabajador y declara sus modelos y capacidades (GPU, contexto,
  velocidad).
- **Misiones:** el coordinador las divide en tareas y asigna cada una a un trabajador compatible con su rol.

**Componentes a diseñar en una etapa propia:**
- registro de trabajadores, con detección de disponibilidad (latido y vencimiento);
- cola persistente de misiones y tareas, con estados equivalentes a los actuales (completa, interrumpida, error);
- ejecución remota con permisos acotados y por invitación del coordinador;
- resultados verificables: el coordinador central verifica todo, igual que hoy, y un trabajador nunca escribe la
  memoria directamente;
- recuperación ante desconexiones: una tarea sin respuesta vuelve a la cola o se declara interrumpida, nunca se da
  por completa.

**Distinción obligatoria:**
- **Inferencia remota:** enviar un prompt y recibir texto. Es lo único probado hasta ahora.
- **Ejecución remota de código** (por ejemplo, Python en el trabajador): requiere un aislamiento específico,
  equivalente a lo que exige el piloto con Bubblewrap y seccomp, y no se habilita por el solo hecho de tener inferencia
  remota.

La implementación avanza por etapas en `OFICINA.md`; la etapa 1 (registro y disponibilidad) ya está hecha.
Esta sección no cambia el laboratorio ni sus pruebas. Las direcciones y los nombres de las computadoras se dejan
fuera del repositorio a propósito.
