# Explorador autónomo de bases de datos (v0.3.0)

> «Tomá esta linterna, entrá en esta caverna y empezá a construir un mapa».

El Explorador le da a la IA una **linterna**: herramientas de solo lectura sobre una base MariaDB que
no conoce. Un **cartógrafo** (ciclo autónomo) observa, pregunta, consulta, analiza y registra un mapa
persistente. El **constructor** usa ese mapa para proponer aplicaciones y construir tableros en el
Taller Web con instantáneas de datos autorizadas.

Nada del código conoce tablas, monedas ni dominios: todo se descubre con `information_schema` y con
las consultas que el agente decide ejecutar.

## Arquitectura

```
 operador (panel) ──► misión «exploracion» ──► cola (1 a la vez) ──► Explorer (agent.py)
                                                                      │  prompt: estructura observada,
                                                                      │  mapa (F#), resultados (C#) como DATOS
                                                                      ▼
                                                     Ollama CPU 11435 (salida JSON con esquema)
                                                                      │  {pregunta, por_que, evidencia,
                                                                      │   herramienta, argumentos, hallazgos}
                                                                      ▼
   herramientas del sistema (SQL con identificadores validados) / sql libre
                                                                      ▼
   Broker (broker.py) ── sqlguard ── conexión NUEVA por consulta ── SET SESSION tx_read_only=1,
     una consulta a la vez            (mysqlproto.py, sin               max_statement_time, sql_select_limit,
     topes filas/bytes/tiempo          LOCAL INFILE ni multi-sentencia) max_join_size
     KILL QUERY al cancelar                                             ▼
                                                     MariaDB: cuenta exclusiva SELECT/SHOW VIEW en UNA base
   registro: explore_queries (texto, estado, filas, bytes, ms, resumen)
                                                                      ▼
   MapStore (mapstore.py) en la SQLite de Constructor IA: instantáneas de esquema, hechos con certeza,
   pasos con justificación y evidencia. La base explorada NUNCA se escribe.
```

| Archivo | Rol |
|---|---|
| `explorer/mysqlproto.py` | Cliente del protocolo MariaDB en biblioteca estándar: `mysql_native_password`, sin `CLIENT_LOCAL_FILES` ni `CLIENT_MULTI_STATEMENTS`, topes de filas/bytes/tiempo durante la lectura. |
| `explorer/sqlguard.py` | Filtro previo por tokens (capa secundaria): una sentencia SELECT/WITH/SHOW/DESCRIBE/EXPLAIN; rechaza escrituras, `INTO`, bloqueos, `SLEEP`/`BENCHMARK`/`LOAD_FILE`, variables de usuario y comentarios ejecutables `/*! */`. |
| `explorer/broker.py` | Intermediario: verificación de la cuenta, sesión limitada, ejecución, cancelación y registro. |
| `explorer/mapstore.py` | Mapa: estructura, huella del esquema, diferencias, hechos, hipótesis de relaciones por nombres. |
| `explorer/agent.py` | Ciclo autónomo, herramientas, defensa ante instrucciones en los datos e informe final. |
| `migrations/0002_explorador.sql` | Tablas `explore_snapshots`, `explore_queries`, `explore_facts`, `explore_steps`. |

### El ciclo (por paso)

1. **Observar**: al empezar, 4 consultas fijas a `information_schema` (tablas, columnas, índices, claves
   foráneas) limitadas a `DATABASE()`. Se guarda una instantánea con huella; si cambió desde la anterior,
   se registran los **cambios de esquema**.
2. **Preguntar y elegir**: el modelo recibe la estructura, el mapa (hechos `F#` con su certeza), las
   incertidumbres y los últimos resultados (`C#`), y responde un JSON forzado por esquema con
   `pregunta`, `por_que`, `evidencia` y una herramienta.
3. **Ejecutar** por el intermediario: `muestra`, `perfil`, `frecuentes`, `relacion` (SQL generado por el
   sistema con identificadores que existen en la estructura observada, sobre muestras) o `sql` libre.
4. **Analizar y registrar**: el sistema calcula hechos **comprobados** (perfiles, conteos, contención de
   valores de una relación). Lo que el modelo concluye se guarda como **inferencia**, citando evidencia.
5. **Incertidumbres** abiertas y **decisión** del próximo paso. Los errores de SQL vuelven al modelo para
   reformular.
6. Al final (o al agotar pasos, consultas o tiempo): informe con descubrimientos y **tres aplicaciones**,
   cada una con consultas SELECT validadas.

### Certezas del mapa

| Certeza | Significado |
|---|---|
| **comprobado** | Calculado por el sistema a partir de una consulta registrada (`information_schema`, conteos, contención de valores). |
| **inferencia** | Interpretación del modelo. Cita evidencia, pero no está verificada. |
| **hipótesis** | Candidato sin probar (p. ej. `x_id` → tabla `x` por convención de nombres). |
| **descartado** | Hipótesis refutada por una consulta. |

La evidencia que cita el modelo se verifica: si nombra un `F#` o `C#` inexistente, el paso lo muestra como
«sin respaldo». Una relación «comprobada» por contención de valores significa que todos los valores
distintos muestreados existen en el destino. **No** implica una restricción declarada; las claves foráneas
declaradas se registran aparte.

## Seguridad

| Requisito | Control efectivo | Verificado en |
|---|---|---|
| Nunca `root` | Rechazado en la configuración y en la verificación (`CURRENT_USER()`). | `test_explorer_config`, broker |
| Solo lectura mínima | `SHOW GRANTS` solo puede tener `USAGE` global y `SELECT`/`SHOW VIEW` sobre la base configurada, con comodines escapados (`prrr\_market`), sin `GRANT OPTION`, roles ni `PROXY`. Si no, la conexión queda **bloqueada** (fallo cerrado). | `test_over_privileged_accounts_are_blocked` |
| No depender de palabras clave | Además de los privilegios: sonda inocua `CREATE TEMPORARY TABLE` que debe fallar por privilegios; sesión `tx_read_only=1` (aunque los privilegios estuvieran mal, el DML falla con 1792); protocolo sin multi-sentencias ni `LOCAL INFILE`. Con el filtro **desactivado** en las pruebas, ningún ataque modifica nada. | `test_server_enforces_even_if_filter_is_bypassed`, `test_read_only_session_blocks_dml_even_with_bad_grants` |
| Límites verificables | `max_statement_time`, `sql_select_limit`, `max_join_size`, leídos de vuelta tras aplicarlos; topes de filas (200), bytes (256 KiB) y tiempo (10 s) en el cliente; `KILL QUERY` del propio hilo al exceder o cancelar; una consulta a la vez en todo el proceso; límites por cuenta (`MAX_USER_CONNECTIONS 2`, `MAX_QUERIES_PER_HOUR 3000`). | `test_limits_rows_bytes_join_size`, `test_timeout_kills_server_query`, `test_one_query_at_a_time` |
| Credenciales | Archivo 0600, propio, sin enlaces, leído solo al conectar. Nunca en `config.toml`, prompts, eventos, registros, API ni código generado. `SHOW GRANTS` se guarda con el hash oculto. | `test_queries_are_logged_without_secrets`, prueba del agente (prompts sin contraseña) |
| Sandbox | Sin cambios: el código generado sigue sin red ni sockets (incluido el de MariaDB). El Explorador **no ejecuta código generado**; las consultas las hace el servidor por el intermediario. | autodiagnóstico existente |
| Separar bases | Una conexión = una base; privilegios verificados sobre esa base; mapas y registros por conexión. Otra base → error 1142. | `test_isolation_between_databases` |
| Instrucciones maliciosas en los datos | Resultados delimitados como «DATOS NO CONFIABLES» (el delimitador no puede falsificarse desde los datos), celdas recortadas, respuesta restringida a un esquema JSON, y las únicas acciones posibles son lecturas limitadas. Una orden `DROP` escrita en los datos y propuesta por el agente es rechazada y registrada. | `test_explores_unknown_database_and_builds_map`, `test_format_result_fences_untrusted_data` |
| Detener inmediatamente | Cancelación → el modelo se corta (cierre del streaming) y la consulta en curso recibe `KILL QUERY`; se escribe un informe parcial. | `test_stop_immediately`, `test_cancel_is_immediate` |
| Sin nuevas interfaces públicas | Solo endpoints autenticados del panel (mismas protecciones: sesión, CSRF, Host, Sec-Fetch-Site). MariaDB solo por socket local o 127.0.0.1. | `ExplorerWebTests` |
| No tocar MariaDB global | El proyecto no ejecuta nada como administrador. El operador crea **una** cuenta propia con el SQL generado; no se cambian variables globales ni otras cuentas o bases. | procedimiento abajo |

## Procedimiento: cuenta SQL restringida en la VivoBook

Lo ejecuta el operador. Constructor IA no tiene ni usa acceso de administrador.

1. Agregar la conexión en `~/constructor-ia/config/config.toml`:
   ```toml
   [[explorer.connections]]
   name = "prrr_market"
   database = "prrr_market"
   user = "cia_explorer"
   password_file = "/home/diego/constructor-ia/config/secretos/prrr_market.pass"
   socket = "/run/mysqld/mysqld.sock"
   ```
2. Generar la contraseña (archivo 0600) y el SQL, sin que la contraseña aparezca en pantalla ni en el SQL:
   ```bash
   ~/constructor-ia/bin/constructor-ia explorer-setup prrr_market > /tmp/cia_explorer.sql
   cat /tmp/cia_explorer.sql          # revisar
   ```
   Contenido esperado (el hash varía):
   ```sql
   CREATE USER 'cia_explorer'@'localhost' IDENTIFIED BY PASSWORD '*…'
     WITH MAX_USER_CONNECTIONS 2 MAX_QUERIES_PER_HOUR 3000 MAX_STATEMENT_TIME 15;
   GRANT SELECT, SHOW VIEW ON `prrr\_market`.* TO 'cia_explorer'@'localhost';
   SHOW GRANTS FOR 'cia_explorer'@'localhost';
   ```
3. Ejecutarlo como administrador y borrar el archivo temporal:
   ```bash
   sudo mariadb < /tmp/cia_explorer.sql && rm /tmp/cia_explorer.sql
   ```
4. Verificar desde Constructor IA (no requiere reiniciar):
   ```bash
   ~/constructor-ia/bin/constructor-ia explorer-verify prrr_market
   # RESULTADO: CONEXIÓN VERIFICADA (solo lectura)
   ```
   Si aparece «comodines sin escapar», la concesión se hizo como `prrr_market` en vez de `prrr\_market`
   (en MariaDB `_` es comodín y podría abarcar otras bases).
5. Reiniciar el servicio para que el panel cargue la conexión: `systemctl --user restart constructor-ia`.

Tablas futuras de `prrr_market` quedan cubiertas automáticamente por `prrr\_market.*`. Para retirar el
acceso: `sudo mariadb -e "DROP USER 'cia_explorer'@'localhost';"` y borrar la entrada y el archivo de contraseña.

## Uso

**Explorador** (menú superior):
- Conexiones con su estado (verificada o bloqueada, motivo, privilegios verificados, límites de sesión,
  sonda de escritura) y botón *Verificar ahora*.
- *Nueva exploración*: conexión, misión (precargada con el texto de la demostración), pasos, consultas y
  minutos máximos, y autorización explícita.
- En la misión: progreso, pregunta actual, justificación, consulta, pasos con su resultado resumido y
  evidencia, hallazgos, relaciones, estadísticas, advertencias, incertidumbres, aplicaciones propuestas,
  y los botones **Detener exploración** y **Continuar exploración** (crea una misión nueva sobre el mismo mapa).
- *Ver mapa*: estructura, relaciones (comprobadas, hipótesis, descartadas), estadísticas, advertencias,
  cambios de esquema, descubrimientos, aplicaciones, incertidumbres, elementos obsoletos e historial de
  consultas con su justificación.
- Archivos de la misión: `exploracion/informe.md` (legible, con la certeza de cada hecho) y `exploracion/mapa.json`.

**Constructor** (integración con el Taller Web): en cada aplicación propuesta, *Construir en el Taller Web*
abre una misión web precargada. Al crearla, el servidor ejecuta **en ese momento** las consultas válidas de
la aplicación por el intermediario (mismos límites) y las guarda como `datos/<nombre>.json`
(`{nombre, consulta, obtenido, columnas, filas}`, como máximo 100 KiB por archivo y 200 KiB en total).
Esos archivos son **inmutables**: si el modelo intenta reescribirlos, se ignora. El JavaScript generado
los lee con `fetch('datos/…')` desde la vista previa aislada; no recibe credenciales ni puede consultar MariaDB.
No hay actualización en vivo en esta versión. Si se agrega, deberá ser una API autenticada de solo lectura
que sirva consultas pre-aprobadas, nunca SQL enviado por el navegador.

## Primera demostración (`prrr_market`)

Tras crear la cuenta y verificar la conexión: *Explorador* → *Nueva exploración* → conexión
`prrr_market` → misión precargada:

> «Explorá la base de datos que tenés disponible. No te voy a explicar qué contiene. Descubrí su
> estructura, investigá su información y construí un mapa de lo que encontrás. Identificá relaciones,
> patrones y oportunidades para desarrollar herramientas útiles. Cuando termines, presentame tus
> descubrimientos y tres aplicaciones que podrías construir».

El modelo no recibe información previa: solo lo que devuelven las herramientas. Con Qwen3 4B en CPU,
cada paso tarda entre decenas de segundos y unos minutos; 25 pasos pueden llevar más de media hora.

**Esta demostración no se ejecutó con `prrr_market` ni con Qwen3 real** (no hay acceso a la VivoBook).
La «prueba inicial de exploración y construcción del mapa» se hizo así:
- MariaDB 10.11.14 real y privado, con bases cuyos nombres de tablas y columnas son **aleatorios** en cada
  ejecución, con una clave foránea declarada, una relación solo por convención de nombre, una tabla sin
  clave primaria y un texto malicioso dentro de los datos.
- Un agente simulado que **solo decide a partir de su prompt**.
- En la interfaz real (Chromium): verificación, estructura, muestra, perfil, valores frecuentes (el texto
  malicioso llegó como dato), hipótesis de relación comprobada, `DROP` rechazado, error de SQL y
  reformulación, informe con tres aplicaciones (una con `DELETE`, marcada como no construible), mapa,
  construcción de un tablero con instantánea y vista previa funcionando.

## Actualización por SSH (0.2.0 → 0.3.0)

```bash
cd ~/src/dieguinPinga
git fetch origin claude/modest-clarke-1dzltx
git checkout claude/modest-clarke-1dzltx
git pull --ff-only
bash ~/src/dieguinPinga/constructor-ia/scripts/update.sh      # copia de la base, reinstala, autodiagnóstico
~/constructor-ia/bin/constructor-ia selftest                   # el sandbox no cambió: debe seguir OPERATIVO
```
Luego seguir el **procedimiento de la cuenta SQL** (arriba) y reiniciar el servicio.
- Migración: `0002_explorador.sql` agrega 4 tablas nuevas a la base propia de Constructor IA (no toca las existentes).
- `config.toml` no se modifica. Sin `[[explorer.connections]]`, el Explorador muestra «no hay conexiones».
- Para desactivarlo: `[explorer] enabled = false`.

## Rollback (0.3.0 → 0.2.0)

```bash
# 1) Cancelar exploraciones pendientes o en curso (la 0.2.0 no sabe ejecutarlas):
~/constructor-ia/venv/bin/python3 -c "
import sqlite3, time; c = sqlite3.connect('$HOME/constructor-ia/data/constructor.db')
c.execute(\"UPDATE missions SET state='cancelada', finished_at=?, error='rollback a 0.2.0' WHERE state IN ('pendiente','explorando') AND json_extract(spec_json, '\$.mode')='exploracion'\", (time.time(),)); c.commit()"
# 2) Volver al código anterior (app.prev):
~/constructor-ia/bin/rollback.sh
```
- La base queda en `user_version = 2`; la 0.2.0 ignora las tablas del Explorador (sus migraciones solo aplican números mayores).
  Las misiones `completada` se ven como un estado desconocido pero no se ejecutan.
- Para volver exactamente al estado anterior de datos: restaurar la copia que hizo `update.sh` en
  `~/constructor-ia/data/backups/` (INSTALL.md §10).
- La cuenta MariaDB puede quedar (solo lectura) o eliminarse con `DROP USER` (arriba).

## Pruebas

`tests/test_explorer.py` (27 pruebas; requiere `mariadbd` instalado, si no se omiten):
- **Unidades**: filtro SQL (más de 30 variantes de escritura y abuso rechazadas), análisis de privilegios
  (incluidos privilegios por columna), configuración (sin root, sin bases del sistema, solo local),
  archivo de contraseña, hipótesis genéricas, diferencias de esquema, delimitación de datos no confiables.
- **Intermediario con MariaDB real**: cuenta mínima verificada; cuentas con privilegios de más, comodines,
  globales, tabla temporal o `GRANT OPTION` bloqueadas; contraseña errónea; con el filtro desactivado,
  12 ataques (DML, DDL, multi-sentencia, `LOCAL INFILE`, `OUTFILE`, otra base, `SET GLOBAL`, `GRANT`)
  sin efecto y con la suma de verificación de las tablas intacta; DML bloqueado por la sesión de solo lectura
  aun con privilegios de escritura; topes de filas, bytes y `MAX_JOIN_SIZE`; tiempo con `KILL` en el
  servidor; cancelación en menos de 3 s; una consulta a la vez; aislamiento entre bases; registro sin secretos.
- **Agente sobre bases desconocidas**: mapa completo, relaciones declarada y comprobada, perfiles,
  advertencias, inferencias con evidencia, evidencia inventada marcada, `DROP` rechazado, error y
  reformulación, informe con tres aplicaciones, base sin cambios, contraseña ausente de todos los prompts,
  inyección confinada al bloque de datos; continuación con detección de cambios de esquema; detención
  inmediata con informe parcial; conexión bloqueada sin consultar al modelo.
- **API y constructor**: conexiones y verificación, validación de misiones, tablero construido con
  instantánea inmutable, consultas inválidas rechazadas.

Pendiente en la VivoBook:

| # | Comprobación |
|---|---|
| E1 | `explorer-verify prrr_market` → VERIFICADA, privilegios exactos. |
| E2 | Exploración real con Qwen3 4B: estabilidad del JSON, calidad del mapa, duración por paso, memoria (`num_ctx 6144`). |
| E3 | Durante la exploración: `SHOW PROCESSLIST` como administrador muestra a lo sumo una consulta de `cia_explorer`; carga de MariaDB normal para Vigilanti y Node-RED. |
| E4 | *Detener* durante una consulta larga: estado cancelada en segundos, sin consultas vivas. |
| E5 | Construir un tablero desde una aplicación propuesta y verlo en la vista previa. |

## Limitaciones y riesgos pendientes

1. **Calidad del modelo**: Qwen3 4B puede repetir preguntas, sacar conclusiones débiles o terminar pronto.
   Sus conclusiones son siempre *inferencias*; lo verificado lo calcula el sistema.
2. **Muestras**: perfiles, frecuencias y relaciones trabajan sobre muestras (`sample_rows`, 2000 valores
   distintos para relaciones) y se indica cuando aplica. `TABLE_ROWS` es una estimación del motor.
3. **El filtro SQL es una lista de denegación** y capa secundaria. La seguridad efectiva descansa en los
   privilegios verificados, la sesión de solo lectura y el protocolo.
4. **Recursos del servidor**: `max_join_size` se basa en estimaciones del optimizador y `max_statement_time`
   corta por tiempo. Una consulta legítima pero pesada puede consumir CPU/IO de MariaDB hasta 10 s (una a
   la vez). Ajustar a la baja si Vigilanti lo nota.
5. **Datos que llegan al modelo**: las muestras (y los comentarios de tablas y columnas) entran en el prompt
   local. Si la base tuviera datos personales, quedarían en los registros de Constructor IA
   (`explore_queries`, recortados).
6. **Instrucciones en los datos**: no pueden ampliar permisos, pero sí sesgar las conclusiones del modelo.
7. **Instantáneas en tableros**: contienen datos reales dentro del proyecto, el ZIP y la vista previa (cuyo
   `localStorage` es compartido entre vistas previas). No hay actualización en vivo.
8. **Autenticación**: solo `mysql_native_password`; sin TLS (conexión local por socket). `ed25519` y
   `caching_sha2` no están soportados.
9. **Metadatos visibles**: cualquier cuenta ve los *nombres* de tablas de `performance_schema` en
   `information_schema` (no su contenido). Las consultas de estructura se limitan a `DATABASE()`.
10. **Mapa**: los hechos estructurales se marcan obsoletos al desaparecer; las estadísticas no se recalculan
    solas (se actualizan al volver a perfilarlas y muestran su fecha).

---

# Cambios 0.4.1: lecciones de la primera expedición real

Expedición `M20261018-0e8d90` (Qwen3 4B en CPU, `prrr_market`). El intermediario funcionó, pero el agente
no leyó registros reales. Los seis defectos se corrigieron **en la infraestructura que rodea al modelo**:
Qwen3 sigue decidiendo qué preguntar, qué tablas investigar y qué aplicaciones proponer. No hay rutas ni
conocimiento fijo sobre ninguna base, tabla ni activo.

| # | Defecto observado | Corrección | Prueba de regresión |
|---|---|---|---|
| 1 | SQL sobre `information_schema` con `table_schema = 'conceptito'` (base equivocada); 0 filas; F13 afirmó conteos exactos | **Verificación previa** de SQL: base equivocada, tablas inexistentes u otras bases se devuelven al modelo como **reparación** (no se ejecutan ni gastan un paso), con la base conectada y las tablas disponibles. El prompt indica `BASE CONECTADA` y `TABLE_SCHEMA = DATABASE()`. Cada resultado se clasifica: ejecutada ≠ útil (`vacía`, `solo nulos`, `metadatos`, `datos`), y el modelo recibe qué **no** puede concluir. | `D1WrongSchemaAndEmptyResultTests` |
| 2 | `muestra {}` y `perfil {}` rechazadas; el agente concluyó que las tablas no estaban disponibles | **Validación por herramienta** con argumentos obligatorios; se aceptan argumentos anidados (`argumentos`, `args`…) y nombres con distinta capitalización, con prefijo de base o muy parecidos (corrección registrada). Si falta algo: hasta **2 reparaciones** sin consumir pasos, con un mensaje explícito («las tablas SÍ existen: …»). Las inválidas guardan el texto crudo y el motivo. | `D2ToolArgumentsTests` |
| 3 | F13 registrado como inferencia sin evidencia | Cada afirmación se **contrasta** con su evidencia: debe existir, ser de una consulta con datos (o un hecho respaldado) y sus cifras deben aparecer en ese resultado. Si no, queda **`sin_verificar`**: fuera del mapa que ve el modelo, fuera de los descubrimientos y listada aparte. Citar evidencia inexistente nunca se acepta. | `D3EvidenceTests` |
| 4 | Comentarios de tablas y columnas (paper trading, ZEC, exchanges, precios) no llegaban al modelo | La estructura del prompt incluye los **comentarios** (`«…»`, recortados) marcados como metadatos no confiables: pistas que deben verificarse, nunca instrucciones. También se indica qué tablas ya se leyeron y cuáles no. | `D4MetadataContextTests` |
| 5 | Límite de 20 min, terminó a los 27:21 | **Presupuesto único** de tiempo: cada generación (pasos, inválidas, reparaciones, informe) recibe como máximo el tiempo restante, y se reserva tiempo para el informe (20 % del total, entre 30 s y 10 min). Si no alcanza, se escribe el informe automático. La duración real y el presupuesto se registran. | `D5TimeBudgetTests` |
| 6 | `completada` con un informe sin resumen ni aplicaciones | **Proceso ≠ calidad ≠ objetivo**: `explore_runs` guarda el motivo de finalización, la calidad (`cumplido`/`parcial`/`insuficiente`), si se cumplió el objetivo (leyó registros reales, hay hallazgos respaldados, informe con resumen y 3 aplicaciones válidas), métricas y motivos. El informe se pide con más tokens (`report_num_predict = 1800`) y, si llega truncado o sin 3 aplicaciones válidas, se reintenta en versión breve. Una aplicación cuya única consulta es inválida no cuenta. | `D6OutcomeTests` |

En la interfaz, la exploración muestra **Evaluación del resultado** (proceso, objetivo, informe, lectura real,
evidencia, respuestas inválidas y reparaciones), los pasos inválidos con su motivo y texto crudo, y las
**afirmaciones sin verificar** aparte. El mapa tiene su propia sección para estas últimas.

Archivos nuevos por exploración: `exploracion/evaluacion.json`. El `informe.md` indica las tablas leídas,
el estado del informe y las afirmaciones sin verificar.

## Ensayos con Qwen3 real

El entorno de desarrollo no puede descargar Ollama ni los modelos (la red lo bloquea), así que **las
pruebas automáticas usan un modelo simulado que reproduce los errores observados**. Los ensayos con Qwen3
se hacen en la VivoBook:

```bash
# Con la interfaz: Explorador → Nueva exploración (misma misión que la primera vez, 20 min).
# O sin interfaz (detener antes el servicio para no competir por Ollama CPU):
systemctl --user stop constructor-ia
~/constructor-ia/bin/constructor-ia explorer-trial prrr_market --minutes 20 --steps 15 --queries 30 --yes
systemctl --user start constructor-ia
# Evaluación de cualquier exploración (solo lectura), incluida la primera:
~/constructor-ia/bin/constructor-ia explorer-report M20261018-xxxxxx
```

## Cómo verificar que la segunda expedición lee e interpreta muestras reales

Criterios objetivos de `explorer-report` (o del panel *Evaluación del resultado*), comparables con
`M20261018-0e8d90`:

| Criterio | Primera expedición | Esperado en la segunda |
|---|---|---|
| Tiempo total ≤ presupuesto | 27:21 > 20:00 | `tiempo X de 20.0 min` con X ≤ 20 |
| Tablas cuyos registros se leyeron | 0 de 6 | al menos 3 de 6 (`tablas_leidas`) |
| Consultas con datos | 0 | ≥ 5 (`consultas_con_datos`), con pocas `vacias`/`con_error` |
| Hallazgos respaldados | 0 (F13 sin evidencia) | ≥ 3, cada uno citando una consulta C# con datos |
| Afirmaciones sin verificar | contadas como hallazgos | listadas aparte, con su motivo |
| Informe | sin resumen ni aplicaciones | `informe completo`: resumen y 3 aplicaciones con consultas válidas |
| Pasos perdidos por argumentos | 2 rechazos | `reparaciones` > 0 posible, pero `respuestas_invalidas` bajas y ningún paso perdido |
| Resultado | «completada» engañosa | `Resultado: CUMPLIDO`, o `PARCIAL`/`INSUFICIENTE` con motivos explícitos |

Contraste humano, independiente del agente:
1. Elegir 2 o 3 hallazgos respaldados, abrir la consulta citada (C#) en el historial del mapa y comprobar que
   las cifras y valores del hallazgo aparecen en ese resultado.
2. Ejecutar por su cuenta 1 o 2 de esas consultas (son SELECT) y confirmar el resultado.
3. Revisar en los pasos si el modelo usó los **comentarios** como pistas (p. ej. en «por qué») y si después
   los **verificó** con consultas de datos. Un comentario solo no basta para un hallazgo respaldado.
4. Revisar que las 3 aplicaciones usen tablas que realmente se leyeron.
5. Si quedan respuestas inválidas, su texto crudo está en el paso: sirve para ajustar `num_predict` o el
   presupuesto, no la autonomía del agente.

Para comprobar además la **generalidad** (base desconocida), las pruebas automáticas ya exploran en cada
ejecución bases con nombres aleatorios. En la VivoBook puede repetirse sobre otra base autorizada, siempre
con su propia cuenta de solo lectura.

## Actualización por SSH (0.4.0 → 0.4.1)

```bash
cd ~/src/dieguinPinga
git fetch origin claude/constructor-ia-0.4.1-explorador
git checkout claude/constructor-ia-0.4.1-explorador
git pull --ff-only
bash ~/src/dieguinPinga/constructor-ia/scripts/update.sh
~/constructor-ia/bin/constructor-ia selftest
~/constructor-ia/bin/constructor-ia explorer-verify prrr_market
```
Migración automática `0004` (tabla `explore_runs` y dos columnas en `explore_steps`). El mapa existente
de `prrr_market` se conserva. `config.toml` no cambia; opcional: `[explorer] report_num_predict = 1800`
(es el valor por defecto).

Si la instalación actual es 0.3.0 (sin 0.4.0), el mismo procedimiento aplica también las migraciones de
0.4.0 (revisiones).

## Rollback (0.4.1 → 0.4.0)

```bash
# 1) Cancelar exploraciones pendientes o en curso y retirar del mapa las afirmaciones «sin_verificar»
#    (la 0.4.0 no conoce esa certeza):
~/constructor-ia/venv/bin/python3 -c "
import sqlite3, time; c = sqlite3.connect('$HOME/constructor-ia/data/constructor.db')
c.execute(\"UPDATE missions SET state='cancelada', finished_at=?, error='rollback a 0.4.0' WHERE state IN ('pendiente','explorando') AND json_extract(spec_json, '\$.mode')='exploracion'\", (time.time(),))
c.execute(\"UPDATE explore_facts SET status='obsoleto' WHERE certainty='sin_verificar'\"); c.commit()"
# 2) Volver al código anterior:
~/constructor-ia/bin/rollback.sh
```
La 0.4.0 ignora `explore_runs` y las columnas nuevas. Para recuperar exactamente los datos previos, restaurar
la copia que hizo `update.sh` (INSTALL.md §10).

## Limitaciones de esta corrección

- Contrastar cifras es una comprobación **léxica**: confirma que los números afirmados aparecen en la
  evidencia, no que la interpretación sea correcta. Una afirmación sin cifras se acepta si cita un resultado
  con datos.
- Las correcciones automáticas de nombres solo se aplican a coincidencias únicas y muy parecidas; si no, se
  pide reparación al modelo.
- La verificación previa de SQL detecta tablas en `FROM`/`JOIN` y filtros de esquema en `information_schema`,
  no errores de columnas (esos los informa MariaDB y el modelo reformula).
- El presupuesto acota la duración de cada generación, pero si Ollama tarda en cargar el modelo, ese tiempo
  también se descuenta: con presupuestos muy cortos puede no quedar tiempo para ningún paso.
- La calidad final depende de Qwen3 4B. La infraestructura evita que sus errores se registren como
  descubrimientos y le da información para corregirse, pero no lo hace más inteligente.

---

# Cambios 0.4.2: contexto administrado y ciclo en dos etapas

Expedición M20261018-bdcbec (`caverna_01`): por primera vez Qwen3 formuló una pregunta propia y ejecutó una
consulta válida (`SELECT * FROM … LIMIT 10`, C26, diez registros reales). Después fallaron dos cosas:

1. **Contexto excedido.** El mensaje siguiente necesitó 5377 tokens con `num_ctx = 4096`; Ollama respondió
   HTTP 400 (`exceed_context_size_error`) y los reintentos crecieron hasta 5439.
   **Causa medida:** 0.4.1 calculaba el tamaño suponiendo 3,2 caracteres por token. Con el vocabulario real de
   Qwen, el texto en español rinde ~3,6, pero las filas de datos rinden **~1,2**: el tokenizador separa los
   números dígito a dígito, así que precios, fechas, identificadores y hashes pesan casi el triple de lo
   supuesto. Además, cada reintento agregaba la respuesta anterior y el motivo del error, así que crecía.
2. **Interpretación anticipada.** Antes de ejecutar C26, respuestas truncadas ya «describían» resultados que
   no existían (precios como 1.23456789, duraciones de 60/120 s, salidas por TP, referencias a C1/C2,
   cantidades absurdas): el formato de un solo paso pedía hallazgos y la siguiente consulta en la misma
   respuesta.

## Qué cambió

| Requisito | Implementación | Prueba |
|---|---|---|
| Medir el mensaje completo | `explorer/context.py`: estimación por exceso que cuenta cada dígito como token, más siglas, códigos y letras sin forma de palabra. Suma instrucciones, datos, historial, la plantilla de chat (por mensaje) y el **esquema JSON** de salida. Se contrastó con el vocabulario BPE de Qwen: ningún tipo de dato queda por debajo del conteo real (peor caso 1,00; texto en español 1,3–1,8 por exceso). | `ContextUnitTests.test_estimate_is_conservative_against_measured_qwen_counts` (muestras medidas) |
| Reservar la respuesta | Presupuesto de entrada = `(num_ctx − reserva) × 0,95 − 96`. Reservas: etapa A `decide_num_predict` (384), etapa B `num_predict` del modelo, informe `report_num_predict`. En los niveles mínimos se pide una respuesta breve y la reserva baja (B: 600/400; informe: 1200/900). | `test_budget_reserves_answer_and_margin`, `test_wide_sample_fits_the_context…` (todo pedido: mensaje + reserva ≤ num_ctx) |
| Reducir sin perder evidencia | Cinco niveles (0 = completo … 4 = mínimo): menos filas y columnas, celdas más cortas, menos hechos del mapa, incertidumbres e historial, comentarios más cortos. Los resultados anchos pasan a una **vista por columnas**: valores distintos, «igual en las N filas», o mínimo y máximo. Así ninguna columna queda oculta y los números y fechas nunca se recortan. El historial viejo se resume en una línea por paso. La evidencia completa sigue en `explore_queries`, y contra ella se contrastan las afirmaciones. | `test_rows_view_reduces_but_says_what_is_hidden`, `C26WideSampleTests` (29 columnas, todos los decimales en la evidencia) |
| No repetir ante `exceed_context_size_error` | `ollama.py` reconoce el error (objeto o texto) y extrae `n_prompt_tokens`/`n_ctx`. El agente calibra el factor de tokens con la cifra real: solo sube, y queda guardado por modelo en `kv.explorer_token_factor`. Pasa a un nivel más chico, nunca reenvía un mensaje idéntico (por huella SHA-256) y deja de enviar lo que, ya calibrado, sabe que no entra. Si ni el mínimo entra, termina con `contexto_insuficiente` y escribe el informe automático. | `test_reported_overflow_is_learned_and_never_resent` (tokenizador 1,8× más pesado), `test_context_that_never_fits_ends_without_identical_retries`, `test_parse_context_errors` |
| Dos etapas estrictas | **A — investigación:** JSON pequeño (pregunta, por qué, evidencia, herramienta, argumentos). Si trae hallazgos, incertidumbres o «resultados», se **descartan sin registrarse**; la respuesta cruda queda en el paso para diagnóstico. **B — interpretación:** tras ejecutar, recibe los registros reales, el identificador auténtico `C#`, la estructura de las tablas consultadas y una nota de fechas. Recién ahí formula hallazgos, hipótesis e incertidumbres. Solo se interpretan resultados con datos o metadatos. | `TwoStageTests.test_stage_a_cannot_anticipate_and_stage_b_is_contrasted` |
| Contrastar afirmaciones | Las C# citables son **solo las consultas de esta exploración**; citar una inexistente o ajena invalida la afirmación (`evidencia_inexistente`). Las cifras deben figurar en la evidencia (corregido: las fechas `2025-10-01` ahora se reconocen). Nuevo: los **valores literales** (códigos en MAYÚSCULAS y textos entre comillas) también deben figurar (`valores_no_encontrados`, p. ej. «TP» cuando los datos dicen TIMEOUT). El **resumen** de la interpretación también se contrasta: si no está respaldado, no vuelve al modelo (ni siquiera la cifra inventada). | mismas pruebas + `test_summary_with_invented_figures_does_not_return_to_the_model`, `test_claims_numbers_literals_and_currentness` |
| Histórico ≠ actual | La etapa B recibe el rango de fechas del resultado, la fecha y hora actual, y la advertencia de que son registros históricos. Una afirmación que diga «actual», «vigente», «ahora», «hoy», etc. queda `sin_verificar` (`vigencia_no_demostrada`) salvo que la evidencia tenga registros de la última hora. Es genérico: se basa en los valores de fecha, no en nombres de tablas. | `test_claims_numbers_literals_and_currentness`, `test_temporal_note_marks_history` |
| Objetivo mínimo medible | Nueva métrica **`ciclos_completos`**: pregunta → consulta válida → registros reales → interpretación con al menos un hallazgo respaldado de ese C# → nueva pregunta (otra herramienta, no `terminar`). «Cumplido» ahora exige al menos un ciclo. | `D6OutcomeTests.test_report_retry_and_successful_objective`, `test_invalid_interpretation_keeps_evidence_and_is_reported` |

No hay conocimiento de ninguna base concreta: las pruebas usan nombres aleatorios y una tabla ancha genérica.

**Corrección adicional del intermediario (detener al instante).** Las corridas bajo carga revelaron una carrera
presente desde 0.3: al detener una exploración, el vigilante cerraba el socket de la consulta en curso, pero en
Linux cerrar un socket desde otro hilo no despierta un `recv` bloqueado. Si además la conexión auxiliar del
`KILL QUERY` no llegaba a tiempo, el error se ignoraba y la consulta seguía hasta su tope (se midieron 32 s en vez
de menos de 3). Ahora el vigilante hace `shutdown` del socket, lo que libera al cliente de inmediato, y reintenta
`KILL QUERY` hasta tres veces para limpiar el servidor (`max_statement_time` sigue cubriendo el resto). Prueba
determinista: `BrokerSecurityTests.test_cancel_is_immediate_even_if_kill_query_fails`; sin la corrección tarda 6 s.
Los controles de solo lectura (cuenta mínima verificada, sesión de solo lectura, filtro, KILL QUERY), la
memoria persistente y el sandbox no cambiaron.

**Visible para el operador:**
- En la UI, cada paso muestra su interpretación (etapa B) con la certeza de cada afirmación, lo anticipado que se descartó y el diagnóstico de contexto (nivel, tokens estimados, desbordes). El panel *Evaluación del resultado* suma ciclos y contexto.
- `explorer-report` imprime la **traza del ciclo** paso a paso.
- Se corrigió además un desplazamiento horizontal en el móvil (líneas con JSON largo, presente desde 0.4.1).

## Cómo verificar en la próxima prueba real (VivoBook)

Objetivo mínimo: **pregunta autónoma → consulta válida → registros reales → interpretación respaldada →
nueva pregunta**.

```bash
systemctl --user stop constructor-ia     # no competir por Ollama CPU
~/constructor-ia/bin/constructor-ia explorer-trial caverna_01 --minutes 20 --steps 15 --queries 30 --yes
systemctl --user start constructor-ia
~/constructor-ia/bin/constructor-ia explorer-report <misión>
```

En la salida de `explorer-report`:

1. `ciclos_completos ≥ 1`. En la traza, una línea `Paso n: … → C# ok, N filas`, seguida de
   `etapa B: … [respaldada] · k hallazgos respaldados` con k ≥ 1, y luego un `Paso n+1` con otra herramienta.
2. `desbordes_de_contexto`: idealmente 0. Si hubo alguno, debe haber **uno solo por tipo de mensaje** y luego
   `contexto: … nivel X` sin desborde. Nunca dos pedidos iguales: cada desborde va seguido de un mensaje más
   chico.
3. `respuestas_con_afirmaciones_anticipadas`: puede ser > 0 (es lo que hacía el modelo), pero esas afirmaciones
   **no aparecen** entre los hallazgos.
4. Contraste humano: abrir en la UI el paso con la interpretación respaldada, comparar sus cifras y valores
   con la tabla del resultado (C#), y repetir esa consulta a mano.
5. Si el modelo dice «actual» o «vigente» sobre registros con fecha vieja, esa afirmación debe figurar como
   *sin verificar (vigencia_no_demostrada)*.

Si `num_ctx` sigue en 4096, conviene no subirlo sin medir antes la memoria de Ollama CPU. La 0.4.2 está hecha
para funcionar con 4096; un contexto mayor solo permite niveles de reducción menores.

## Actualización por SSH (0.4.1 → 0.4.2)

```bash
cd ~/src/dieguinPinga
git fetch origin claude/constructor-ia-0.4.2-contexto
git checkout claude/constructor-ia-0.4.2-contexto
git pull --ff-only
bash ~/src/dieguinPinga/constructor-ia/scripts/update.sh
~/constructor-ia/bin/constructor-ia selftest
~/constructor-ia/bin/constructor-ia explorer-verify caverna_01
```

- Migración automática `0005`: agrega dos columnas a `explore_steps` (`interpretation`, `context_json`).
- `config.toml` no necesita cambios. Opcional: `[explorer] decide_num_predict = 384` (valor por defecto).
- La calibración de tokens empieza en 1,0 y solo sube con lo que informe Ollama.
- Desde 0.4.0 el mismo procedimiento aplica también la migración 0004.

## Rollback (0.4.2 → 0.4.1)

```bash
# 1) Cancelar exploraciones pendientes o en curso:
~/constructor-ia/venv/bin/python3 -c "
import sqlite3, time; c = sqlite3.connect('$HOME/constructor-ia/data/constructor.db')
c.execute(\"UPDATE missions SET state='cancelada', finished_at=?, error='rollback a 0.4.1' WHERE state IN ('pendiente','explorando') AND json_extract(spec_json, '\$.mode')='exploracion'\", (time.time(),)); c.commit()"
# 2) Volver al código anterior:
~/constructor-ia/bin/rollback.sh
```

- La 0.4.1 ignora las columnas nuevas y la clave `explorer_token_factor`.
- Las hipótesis de la etapa B quedan como hechos `hallazgo` con certeza `hipotesis`, que la 0.4.1 ya conoce.
- Para recuperar exactamente los datos previos, restaurar la copia que hizo `update.sh` (INSTALL.md §10).

## Limitaciones

- El tamaño se **estima**: Ollama no expone un contador de tokens. La estimación se validó con el vocabulario de
  Qwen (familia Qwen2/3), no con el modelo cargado en la VivoBook. Por eso existe la calibración con las cifras
  que devuelve Ollama.
- La vista por columnas y los niveles reducidos le muestran al modelo menos filas. Puede interpretar menos,
  pero lo que afirme se contrasta contra el resultado completo registrado.
- La detección de vigencia reconoce fechas ISO (`AAAA-MM-DD [hh:mm[:ss]]`) en los valores. No reconoce
  marcas de tiempo numéricas (epoch) ni zonas horarias: con esas, una afirmación de «actual» queda sin
  verificar.
- Los valores literales se contrastan por presencia textual: «VENCIDO» en la evidencia respalda «motivo
  VENCIDO» aunque estuviera en otra columna.
- Cada paso con datos hace dos llamadas al modelo (A y B). En CPU eso reduce los pasos posibles dentro de los
  20 minutos, a cambio de que cada hallazgo se base en un resultado real.

---

# Cambios 0.4.3: argumentos «null» y bucles de reparación

Primera prueba real de 0.4.2 sobre `caverna_01`: la exploración repetía «la tabla "null" no existe», agotaba
las dos reparaciones y volvía al mismo error, sin ejecutar consultas.

## Causa

1. El esquema JSON de la etapa A declaraba todos los argumentos como **texto**. La salida estructurada de
   Ollama no puede emitir un JSON `null` en un campo de texto, así que cuando un argumento «no aplica», el
   modelo escribe la **palabra** `"null"`.
2. `normalize_step()` descartaba `None` y `""`, pero no el texto `"null"`. Lo trataba como un nombre de tabla
   que no existe.
3. Además validaba **todos** los argumentos aunque la herramienta no los usara. Un `muestra` con una tabla
   real se rechazaba igual si traía `"columna": "null"`. La prueba de regresión lo confirma contra el código
   0.4.2. Es probable que la prueba real cayera en este caso.
4. La reparación pedía «Respondé de nuevo el **MISMO** objeto JSON corregido» y reenviaba la respuesta
   anterior completa: un modelo chico la copia, «null» incluido.

## Corrección (sin elegir tablas por el modelo)

| Cambio | Detalle |
|---|---|
| Valores de relleno = ausentes | `null`, `None`, `n/a`, `-`, `ninguna`, etc. cuentan como «sin valor». Un nombre que **existe** en la base (aunque se llame `null`) nunca se trata como relleno. |
| Solo se validan los argumentos de la herramienta elegida | `muestra` usa `tabla`; `perfil` usa `tabla` y opcionalmente `columna`; `frecuentes` usa `tabla` y `columna`; `relacion` usa los cuatro; `sql` usa `sql`. Los demás se ignoran y quedan anotados en las correcciones. |
| Nombres reales en el esquema | En la etapa A, `tabla`/`tabla_destino` y `columna`/`columna_destino` son un `enum` con los nombres observados (hasta 80 tablas y 300 columnas; con más, texto libre). La gramática ya no admite «null». **El modelo sigue eligiendo cuál.** |
| Reparación precisa | Dice qué campo llegó como «null», que es un texto de relleno, qué usa la herramienta, que escriba el nombre de la tabla **que él elija** entre las existentes, y que puede cambiar de herramienta. Ya no pide «el mismo objeto»: solo se reenvía un fragmento de la respuesta anterior. |
| Sin bucles idénticos | Cada problema tiene una firma (tipo, campo, valor). Si la reparación repite la misma firma, la siguiente usa un **esquema estricto**: los argumentos de esa herramienta son obligatorios y solo se admiten nombres reales. Si aun así se repite, no se insiste en ese paso. Si dos pasos seguidos terminan con el mismo error, la exploración se detiene (`respuestas_repetidas`) en vez de gastar el presupuesto. |
| Respuestas crudas guardadas | Cada intento de la etapa A (respuesta y reparaciones) queda en `explore_steps.context_json.respuestas_modelo`, con su problema, su firma y el esquema usado. Se ven en la UI («Respuestas crudas del modelo en la etapa A») y con `explorer-report <misión> --respuestas`. |

Pruebas: `tests/test_explorer_argumentos.py` (11). Contra el código 0.4.2, las dos regresiones de
comportamiento fallan: `muestra` con tabla real y `"columna": "null"` se rechazaba, y el error idéntico se
repetía en bucle.

## Inspeccionar las respuestas reales de caverna_01 (antes de actualizar)

0.4.2 guardó la última respuesta cruda de cada paso inválido y los avisos con cada problema. Este comando abre
la base **en solo lectura** (`mode=ro`), no modifica nada y no requiere detener el servicio. Muestra la
exploración más reciente:

```bash
~/constructor-ia/venv/bin/python3 - <<'FIN'
import os, sqlite3
c = sqlite3.connect("file:" + os.path.expanduser("~/constructor-ia/data/constructor.db") + "?mode=ro", uri=True)
mid = c.execute("SELECT id FROM missions WHERE json_extract(spec_json, '$.mode')='exploracion'"
                " ORDER BY created_at DESC LIMIT 1").fetchone()[0]
print("Misión", mid)
for n, st, rep, an, raw in c.execute("SELECT n, status, repairs, analysis, raw FROM explore_steps"
                                     " WHERE mission_id=? ORDER BY id", (mid,)):
    print(f"Paso {n} [{st}] reparaciones={rep}: {an or ''}\n  respuesta cruda: {raw or '(no guardada)'}")
for (msg,) in c.execute("SELECT message FROM events WHERE mission_id=? AND kind='aviso' ORDER BY id LIMIT 40", (mid,)):
    print("aviso:", msg)
FIN
```

Qué buscar: si la respuesta cruda trae `"tabla": "null"` (el modelo no eligió tabla) o una tabla real con
`"columna": "null"` u otros campos en «null» (el caso que 0.4.2 rechazaba sin motivo).

Después de actualizar: `~/constructor-ia/bin/constructor-ia explorer-report <misión> --respuestas`.

## Actualización por SSH (0.4.2 → 0.4.3)

```bash
cd ~/src/dieguinPinga
git fetch origin claude/constructor-ia-0.4.3-argumentos
git checkout claude/constructor-ia-0.4.3-argumentos
git pull --ff-only
bash ~/src/dieguinPinga/constructor-ia/scripts/update.sh
~/constructor-ia/bin/constructor-ia selftest
~/constructor-ia/bin/constructor-ia explorer-verify caverna_01
```

- Sin migraciones nuevas: el esquema de la base sigue en 0005.
- `config.toml` no cambia.
- Solo se actualiza Constructor IA; ningún otro servicio se toca.

## Rollback (0.4.3 → 0.4.2)

```bash
~/constructor-ia/venv/bin/python3 -c "
import sqlite3, time; c = sqlite3.connect('$HOME/constructor-ia/data/constructor.db')
c.execute(\"UPDATE missions SET state='cancelada', finished_at=?, error='rollback a 0.4.2' WHERE state IN ('pendiente','explorando') AND json_extract(spec_json, '\$.mode')='exploracion'\", (time.time(),)); c.commit()"
~/constructor-ia/bin/rollback.sh
```

Sin cambios de esquema que deshacer. Detalle cosmético: en pasos con reparaciones hechos por 0.4.3, la UI de
0.4.2 muestra una línea de contexto de más («nivel undefined»), porque no conoce `respuestas_modelo`.

## Limitaciones

- Con más de 80 tablas o 300 columnas, el esquema vuelve a texto libre para no inflar el mensaje. Ahí rigen la
  normalización y la reparación, pero no la restricción gramatical.
- El `enum` de columnas reúne las de todas las tablas: el modelo todavía puede elegir una columna de otra
  tabla. Eso se detecta y se repara con la lista de columnas de la tabla elegida.
- Que la gramática respete el `enum` depende de Ollama. Si una versión lo ignorara, siguen valiendo la
  normalización, la reparación precisa y el corte de bucles.

---

# Cambios 0.4.4: bloqueos de Ollama CPU e interpretaciones acotadas

Exploración real M20261018-3571c4 (0.4.3): C35 y C36 devolvieron diez registros reales cada una, pero la etapa B
se truncó una y otra vez. Después Ollama CPU (puerto 11435) pasó 317 580 ms «actualizando su caché de prompts»,
Constructor informó un timeout «al contactar a Ollama CPU» y la misión terminó **fallida**. Ollama completó el
pedido abandonado con HTTP 500 a los 5m18s y luego siguió respondiendo con HTTP 200.

## Qué explica cada parte (lo comprobado y lo que falta medir)

**Comprobado en Constructor (reproducido en pruebas):**

1. **La misión falló por una clasificación errónea, no porque Ollama estuviera caído.** El socket del cliente
   tenía el mismo plazo que el vigilante del tiempo total. Cuando el servidor acepta el pedido pero no envía nada,
   a veces vencía primero el socket: el error decía «No se pudo contactar a Ollama» y el agente, al ver
   «contactar», lo trataba como servidor caído y abortaba la misión. El plazo real era fraccionario
   (`min(300 s, tiempo restante)`) y el vigilante revisa cada 0,5 s, así que el socket ganaba casi siempre. Con
   el mismo escenario simulado, 0.4.3 termina *fallida* y 0.4.4 *completada* con el ciclo cerrado.
2. **El pedido abandonado seguía ocupando el servidor.** Ollama atiende un pedido a la vez: el siguiente pedido
   espera en cola detrás del abandonado (lo que viste: HTTP 500 a los 5m18s, después HTTP 200).
3. **Las truncas de la etapa B consumían el tiempo.** La interpretación no tenía límites de cantidad ni de largo
   y el reintento usaba la misma reserva de 900 tokens. A ~4 tokens/s eso son ~225 s por intento, más la
   evaluación del prompt.

**Cuentas con los datos informados (no son mediciones):**

- KV cache de Qwen3-4B (36 capas, 8 cabezas KV × 128, f16): **~144 KiB por token**. 1437 MiB en 4 contextos son
  ~359 MiB cada uno, es decir **~2500 tokens por contexto**: el tamaño de nuestros mensajes. Los contextos
  guardados en la caché de prompts son, casi seguro, los pedidos recientes de Constructor.
- Memoria aproximada de Ollama CPU durante la exploración: pesos de Qwen3-4B Q4 (~2,5 GiB) + KV activo con
  `num_ctx=4096` (~576 MiB) + caché de prompts (1,4 GiB) + búferes ≈ **4,5–5 GiB**, en una VivoBook de 5,7 GiB
  que además ejecuta MariaDB, Node-RED, Vigilanti y el Ollama principal. La presión de memoria es **plausible**,
  pero no está demostrada. Que haya 1 GiB en swap no prueba nada: importa si se **lee** swap mientras Ollama no
  responde.

**Lo que falta medir en la VivoBook** (para eso es `ollama-diag`): si durante los 317 s hubo lecturas de swap y
fallos de página mayores en el proceso de Ollama (presión de memoria); si Ollama usó CPU sin responder (cómputo
propio del servidor al copiar estados de la caché); si estuvo casi sin CPU (espera o bloqueo interno); si su
cgroup llegó a `memory.high`/`memory.max`; o si otros procesos ocuparon la CPU.

## Cambios en Constructor

| Cambio | Detalle |
|---|---|
| Ocupado ≠ caído | La conexión tiene un plazo propio de 5 s: si falla, Ollama **no está disponible**; se comprueba 3 veces antes de declarar la misión fallida. Si Ollama acepta el pedido y no responde, el error es `OllamaTimeout`, con la fase (`espera` o `generacion`): falla esa llamada, no la misión. El socket tiene 30 s de margen y nunca se adelanta al vigilante, que revisa cada 0,2 s. |
| Pedido siguiente con más plazo | Tras un timeout, el próximo pedido recibe el doble de plazo (dentro del presupuesto), porque espera en cola detrás del abandonado. Con **2 timeouts seguidos** se deja de insistir: finalización `ollama_sin_respuesta`, informe automático sin pedirle otro informe al modelo y motivo explícito. El proceso queda *completada* y la evaluación dice por qué el resultado es insuficiente. Un timeout en la etapa A no cuenta como respuesta inválida del modelo. |
| Etapa B acotada | Esquema con límites: resumen ≤ 200 caracteres, ≤ 3 hallazgos de ≤ 180, ≤ 1 hipótesis y ≤ 1 incertidumbre. La respuesta máxima posible estima 451 tokens (con la estimación pesimista), dentro de la reserva `interpret_num_predict = 512`. Antes no había límite y la reserva era de 900. |
| Sin truncas repetidas | Si la respuesta se trunca, se **recuperan sus partes completas**: nunca se termina un texto cortado y lo recuperado se contrasta igual con la evidencia. Si no hay nada recuperable, se pide **una sola** interpretación mínima (una frase y un hallazgo, reserva de 256), en vez de repetir la misma de 512/900. |
| Etapa A acotada | Largos máximos en pregunta, por qué, argumentos y SQL. Se retiró el `enum` de columnas de 0.4.3: repetía todas las columnas dos veces (~500 tokens estimados por mensaje). El de tablas se mantiene. |
| Reserva según la velocidad medida | Con la velocidad de generación más baja de las últimas tres llamadas (y suponiendo que el prompt se evalúa entero), la reserva baja si no terminaría dentro del plazo. Hay un piso por etapa y queda anotado el motivo. |
| Prefijo estable | Un único mensaje de sistema para las etapas A, B y el informe, y el mismo comienzo del mensaje de usuario (misión y base). Las marcas «leída/sin leer» salieron de la estructura (cambiaban en cada paso). Así Ollama puede reutilizar la evaluación del prefijo en vez de recalcularla, lo que en CPU cuesta decenas de segundos por pedido. Solo el nivel mínimo de contexto usa un sistema compacto por etapa. |
| Tiempos medidos | Cada llamada registra el tiempo de reloj, el primer byte, los `load/prompt_eval/eval/total_duration` que informa Ollama, los tokens/s y el **tiempo sin explicar** (reloj menos carga, prompt y generación). Están en `exploracion/llamadas_ollama.json`, en la evaluación (`metrics.ollama`), en la traza de `explorer-report` y en la UI. |

## Diagnóstico en la VivoBook (solo lectura; no cambia ningún servicio)

`constructor-ia ollama-diag` lee `/proc`, `/sys/fs/cgroup` y `GET /api/ps` de la instancia configurada en
Constructor (`ollama.url`, 11435). Se niega a apuntar al puerto 11434.

```bash
# Terminal 1: muestreo durante la prueba (cada 1 s, 25 min)
~/constructor-ia/bin/constructor-ia ollama-diag --segundos 1500
# Terminal 2: la prueba (con el servicio detenido para no competir por Ollama CPU)
systemctl --user stop constructor-ia
~/constructor-ia/bin/constructor-ia explorer-trial caverna_01 --minutes 20 --steps 15 --queries 30 --yes
systemctl --user start constructor-ia
# Después: cruzar las muestras con cada llamada de esa exploración
~/constructor-ia/bin/constructor-ia ollama-diag --analizar ~/constructor-ia/logs/ollama-diag-<fecha>.jsonl --mision <misión>
~/constructor-ia/bin/constructor-ia explorer-report <misión>
```

- Si Ollama CPU corre con otro usuario, el proceso no se detecta solo: el comando lista los candidatos y se
  indica con `--pid`. Para eso leer `/proc` como root (`sudo`) también es solo lectura.
- El comando sugiere el `journalctl` de la unidad de Ollama CPU para la misma ventana, filtrando líneas de
  caché, slot y prompt.

**Cómo leer los indicios** (cada uno cita sus cifras; no son conclusiones automáticas):

| Indicio | Qué significa | Qué hacer (con aprobación, solo en 11435) |
|---|---|---|
| PRESIÓN DE MEMORIA: MiB leídos de swap, fallos mayores, tiempo con tareas trabadas por memoria (PSI full) | El bloqueo coincide con lecturas de swap: el swap **sí** explica la lentitud | Reducir la memoria de Ollama CPU (ver abajo) |
| «Hay N MiB en swap pero casi sin lecturas» | El swap ocupado **no** explica la lentitud | Buscar la causa en el servidor |
| LÍMITE DEL CGROUP (`memory.events high/max`) | systemd estrangula a Ollama por `MemoryHigh`/`MemoryMax` | Revisar los límites de esa unidad |
| CONTENCIÓN DE CPU | Otros procesos ocupaban la CPU (ver `top_cpu` en las muestras) | Programar las pruebas fuera de esos momentos |
| Sin presión y Ollama con CPU alta | El tiempo se fue en cómputo propio del servidor (p. ej. copiar estados de la caché) | Problema o configuración del servidor |
| Sin presión y Ollama casi sin CPU | Espera o bloqueo interno del servidor | Problema del servidor: registros con journalctl |

**Opciones para Ollama CPU, solo después de medir y con aprobación del operador.** Ninguna afecta al puerto
11434 ni se aplica en esta versión:

- Si `/api/ps` muestra **dos modelos cargados** (Qwen3 4B y el 1.7B del chat), son ~1,3 GiB más: conviene no
  usar el chat durante las pruebas o descargar el modelo del chat.
- Cuantizar el KV cache (`OLLAMA_KV_CACHE_TYPE=q8_0` con `OLLAMA_FLASH_ATTENTION=1`) reduce a la mitad el KV
  activo y el tamaño de cada contexto guardado.
- Revisar si Ollama 0.34 permite limitar el tamaño de su caché de prompts (`ollama serve --help` y la
  documentación de esa versión). No lo supongo: depende de la versión instalada.

## Actualización por SSH (0.4.3 → 0.4.4)

```bash
cd ~/src/dieguinPinga
git fetch origin claude/constructor-ia-0.4.4-ollama
git checkout claude/constructor-ia-0.4.4-ollama
git pull --ff-only
bash ~/src/dieguinPinga/constructor-ia/scripts/update.sh
~/constructor-ia/bin/constructor-ia selftest
~/constructor-ia/bin/constructor-ia explorer-verify caverna_01
```

- Sin migraciones nuevas.
- `config.toml` no necesita cambios. Opcional: `[explorer] interpret_num_predict = 512` (valor por defecto).
- Ollama, MariaDB, Node-RED y los demás servicios no se tocan.

## Rollback (0.4.4 → 0.4.3)

```bash
~/constructor-ia/venv/bin/python3 -c "
import sqlite3, time; c = sqlite3.connect('$HOME/constructor-ia/data/constructor.db')
c.execute(\"UPDATE missions SET state='cancelada', finished_at=?, error='rollback a 0.4.3' WHERE state IN ('pendiente','explorando') AND json_extract(spec_json, '\$.mode')='exploracion'\", (time.time(),)); c.commit()"
~/constructor-ia/bin/rollback.sh
```

Sin cambios de esquema que deshacer. Los pasos con estado `sin_respuesta` y los archivos
`llamadas_ollama.json` quedan como registro; la UI de 0.4.3 los muestra como pasos comunes.

## Objetivo de la próxima prueba

Un ciclo respaldado con Qwen3 4B local: pregunta → consulta → registros → interpretación respaldada → nueva
pregunta.

- En `explorer-report`: `ciclos_completos ≥ 1` y, en la traza, `etapa B (completa|recuperada|minima): …` con al
  menos un hallazgo respaldado.
- En `metrics.ollama`: `timeouts` en 0, o pocos y sin terminar la misión; `truncadas` bajas;
  `generacion_tokens_s` cerca de 4.
- `ollama-diag --mision` muestra qué pasó en cada llamada lenta.

## Limitaciones

- Que la gramática respete `maxLength`/`maxItems` depende de la versión de Ollama. Si no los respetara, la
  reserva acotada, la recuperación de truncas y la interpretación mínima siguen funcionando.
- El «tiempo sin explicar» depende de los campos `*_duration` que informe Ollama. Si el bloqueo ocurre antes de
  empezar el pedido, puede aparecer como espera de primer byte o como timeout en fase `espera`.
- Los indicios de `ollama-diag` usan umbrales simples y conservadores: orientan la investigación, no
  reemplazan mirar las cifras y los registros del servidor.

---

# Cambios 0.4.5: conocimiento acumulativo

Dos expediciones reales con 0.4.4 mostraron cuatro problemas de calidad en la curiosidad del agente. La mejora no
programa qué tablas investigar ni qué preguntar: el modelo sigue eligiendo libremente tablas, preguntas y
herramientas. El sistema lo ayuda a reconocer cuándo una consulta responde a su pregunta, cuándo repite
información y cuándo una conclusión excede la evidencia.

## Problemas y causas

| # | Problema observado | Causa en el código 0.4.4 |
|---|---|---|
| 1 | Pregunta por valores distintos o frecuentes y ejecuta una herramienta que solo cuenta filas | La pregunta (texto libre) y la herramienta no se comparaban. Además, desde 0.4.3, `perfil` con `"columna": "null"` pasaba en silencio a «contar filas». |
| 2 | Repite exactamente la misma consulta, incluso entre expediciones | Las consultas se guardaban pero nadie las consultaba antes de ejecutar; el modelo solo veía los últimos pasos de su expedición. |
| 3 | Ciclos «completos» sin conocimiento nuevo | Bastaba cualquier hallazgo respaldado. Repetir un hecho conocido devolvía su id y contaba como respaldado. **`upsert` además reasignaba ese hecho a la expedición nueva y podía bajarle la certeza.** |
| 4 | Generaliza a toda una tabla desde diez registros | El contraste de afirmaciones verificaba cifras y valores, no el alcance de la evidencia. |

**Defecto de procedencia (corregido, prioritario).** En una prueba con memoria creada por 0.4.4 y una segunda
expedición también 0.4.4, 16 de 17 hechos quedaron reasignados a la segunda expedición, y un hallazgo respaldado
bajó de `inferencia` a `sin_verificar` por una sola reafirmación sin evidencia. En el mismo escenario con 0.4.5
(misma memoria, migrada), los 17 hechos conservan certeza, texto y expedición creadora.

## Cambios

**Procedencia, certeza e historial (`mapstore`).**
- `mission_id` es la expedición que **creó** el hecho y ya no se sobrescribe. `last_mission_id` es la última que
  lo observó.
- Cada cambio queda en `explore_fact_history`, con el estado anterior y el nuevo.
- Hechos medidos por el sistema (`upsert`): la medición nueva reemplaza a la anterior (una relación puede
  descartarse con datos nuevos), pero el estado anterior queda en el historial y la expedición creadora no se
  pierde.
- Afirmaciones del modelo (`record_note`): **nunca** se cambia el texto original ni se baja la certeza. El
  resultado de repetir una afirmación es uno de:
  - `confirmado`: igual respaldo; se suma la evidencia.
  - `certeza_subio`: mejor respaldo.
  - `reafirmado_sin_respaldo`: menos respaldo; queda anotado y el hecho no cambia.
  Un hecho `descartado` por el sistema no lo revive el modelo.

**1. ¿La consulta responde la pregunta?**
- La etapa A declara en `busca` qué quiere averiguar: filas, ejemplos, valores distintos, frecuencias, rango o
  nulos, relación, agregado por grupo, evolución temporal u «otro». Opcionalmente, también `columna_objetivo`.
- Las herramientas del sistema tienen capacidades exactas. Si no pueden responder, se pide una reparación sin
  gastar el paso, informando qué herramientas sí podrían (p. ej. «perfil(tabla) sin columna solo cuenta filas;
  pueden responderlo perfil(tabla, columna), frecuentes(tabla, columna) o sql con COUNT(DISTINCT …)»). La elección
  sigue siendo del modelo.
- **El SQL propio nunca se bloquea por la heurística.** Se analiza su forma (agregados, GROUP BY, WHERE,
  ORDER BY, LIMIT, JOIN) solo para calificar después si respondió: `respondida`, `parcial`, `no_respondida` o
  `indeterminada` (si no se entiende).
- La etapa B recibe: «TU PREGUNTA BUSCABA: … → la consulta la responde solo en parte».

**2. ¿Repite lo que ya se sabe?**
- Cada consulta tiene una **firma** (SQL normalizado) y una **firma de forma** (operación, tablas, columnas).
- Antes de ejecutar, se busca la firma en todas las expediciones de la conexión. Las consultas anteriores a 0.4.5
  se completan al empezar cada exploración, sin cambiar nada más.
- Si ya se ejecutó, se pide una reparación que cita la consulta (C#, expedición, antigüedad, filas) y los hechos
  que salieron de ella.
- El modelo **puede insistir** explicando en `repetir_porque` por qué la repite. Entonces se ejecuta y se compara
  la huella del resultado:
  - igual: «sin cambios desde C#»;
  - distinta: se registra el hecho `cambio` «el resultado cambió» (conocimiento nuevo).
- Las consultas casi iguales (misma forma) solo generan un aviso en la etapa B.

**3. ¿Aporta conocimiento nuevo?**
- **Clave de cada afirmación: tablas | columnas.** Las columnas salen de los nombres que menciona (sin importar
  acentos) y de las columnas del resultado donde aparecen sus valores. Los alias del resultado no cuentan, y un
  valor que aparece en más de dos columnas se considera ambiguo.
- **Clasificación conservadora** frente a los hechos con la misma clave:

  | Aporte | Cuándo | ¿Conocimiento nuevo? |
  |---|---|---|
  | `nuevo` | no había hechos con esa clave | sí |
  | `amplia` | agrega valores sobre algo ya estudiado (p. ej. otra faceta de la misma evidencia) | sí |
  | `difiere_alcance` | valores distintos a un hecho observado con **otro alcance** | sí (no es contradicción) |
  | `difiere_tiempo` | valores distintos a un hecho de **otra expedición o momento** (los datos pueden cambiar) | sí (no es contradicción) |
  | `confirma` | sus valores ya estaban registrados | no |
  | `contradice` | mismo alcance, **misma expedición**, poco tiempo, evidencia distinta y cifras distintas | no (queda marcado para investigar) |
  | `indeterminado` | no se puede saber de qué tabla y columna habla, o no trae valores para comparar | no |
  | `trivial` | solo repite el tamaño del resultado | no |
  | `hipotesis` / `sin_respaldo` | no está respaldada | no |

- **Ciclo con conocimiento nuevo:** pregunta → consulta que no quedó `no_respondida` → al menos un hallazgo
  respaldado con aporte nuevo (o un cambio de datos detectado) → nueva pregunta.
- El ciclo de 0.4.4 se conserva como `ciclos_formales` (y `ciclos_completos`, por compatibilidad).
- **El objetivo de una expedición exige `ciclos_con_conocimiento_nuevo ≥ 1`.**

**4. ¿La conclusión excede la evidencia?**
- **Alcance de cada resultado**, calculado por el sistema:
  - `muestra_arbitraria`: LIMIT sin ORDER ni WHERE; son las primeras filas físicas, **no** una muestra aleatoria.
  - `extremos`: con ORDER BY.
  - `subconjunto`: con WHERE.
  - `tabla_completa`: también cuando devolvió menos filas que su LIMIT.
  - `agregado_total`.
  - `agregado_muestra`: se alcanzó el tope de muestra.
  - `metadatos` o `desconocido`.
  La etapa B lo recibe: «ALCANCE DE ESTA EVIDENCIA: 10 filas de 60 (17 %): las primeras en el orden físico; NO es
  una muestra aleatoria».
- **Cómo se detecta una generalización:** cada hallazgo puede declarar su `alcance` (muestra, subconjunto o
  tabla), y un detector de cuantificadores («todas», «siempre», «ninguna», «cada», «solo», «la tabla tiene…») sirve
  de respaldo.
- **Si generaliza:** cuando habla de la tabla entera con evidencia parcial, **no se reescribe**. El texto original
  se conserva, la certeza pasa a `hipotesis` (veredicto `generaliza_mas_alla_de_la_evidencia`) y se registran
  aparte el alcance real y cómo verificarla.
- Esas hipótesis aparecen en la etapa A como «PENDIENTE DE VERIFICAR O ACLARAR», junto con las diferencias entre
  observaciones. Es información, no una orden.

**Mapa de conocimiento en la etapa A.** «LO QUE YA SE SABE», sección que abarca todas las expediciones, muestra
por tabla:
- filas (contadas o estimadas);
- si ya se vieron ejemplos;
- columnas estudiadas y sin estudiar;
- hipótesis abiertas y diferencias.

Se reduce por niveles como el resto del mensaje.

**Otras correcciones.**
- En la vista por columnas de un resultado ancho, las columnas informativas (constantes y de texto) van primero.
- Se corrigió un aviso inalcanzable del informe en 0.4.4 («no trae tres aplicaciones válidas»).
- Se corrigió «null tokens/s» cuando Ollama no informa la velocidad.

## Visible para el operador

- **UI:**
  - cada hecho muestra su aporte, alcance y procedencia (creado en, última observación, confirmaciones y
    reafirmaciones), y un botón **Historial**;
  - cada paso muestra qué buscaba, si se respondió, su alcance y si repitió o se pareció a otra consulta;
  - la evaluación suma una línea «Conocimiento».
- **`explorer-report <misión>`:** la traza incluye `busca → respuesta`, alcance y el aporte de cada afirmación.
- **`explorer-hecho <conexión> <id>`:** texto original, certeza, alcance, aporte e historial completo de un hecho.
- **Métricas nuevas:**
  - ciclos: `ciclos_formales`, `ciclos_con_conocimiento_nuevo`;
  - aportes: `hechos_nuevos`, `confirmaciones`, `contradicciones`, `diferencias_tiempo_o_alcance`,
    `aportes_indeterminados`, `afirmaciones_triviales`, `generalizaciones_acotadas`;
  - repeticiones y desajustes: `consultas_repetidas_sin_justificar` / `_justificadas`,
    `cambios_de_datos_detectados`, `desajustes_pregunta_herramienta`, `preguntas_no_respondidas`;
  - control: `pasos_con_busca_otro` (para detectar si el modelo esquiva el control) y `cobertura` (columnas
    estudiadas al inicio y al final).

## Migración 0006 (solo agrega)

- **`explore_facts`:** `last_mission_id` (inicializado con `mission_id`) y `clave`.
- **Tabla nueva `explore_fact_history`:** se siembra con el estado de cada hecho al migrar
  (`estado_al_migrar_0.4.5`).
- **`explore_queries`:** `firma`, `firma_forma`, `busca`, `respuesta`, `alcance_json` y `resultado_hash`.
- **`explore_steps`:** `aporte_json`.

No modifica ni borra datos. Limitación honesta: si antes de 0.4.5 un `upsert` ya había sobrescrito la expedición
o la certeza de un hecho, ese estado anterior no es recuperable. El historial empieza en el estado al migrar, y
no se inventa.

## Pruebas

- **Nuevas:** `tests/test_explorer_conocimiento.py` (17).
  - procedencia: reafirmar con menos respaldo nunca degrada; mejor respaldo sube la certeza sin tocar el texto;
    confirmación con evidencia sumada; el modelo no revive un hecho descartado; la reobservación del sistema
    conserva la expedición creadora;
  - migración 0006 sobre una base 0.4.4 con hechos, sin cambios;
  - reglas del módulo `knowledge`;
  - los cuatro problemas de punta a punta sobre bases ficticias, con varias expediciones sobre la misma memoria.
- **Entre versiones:** expedición con el código 0.4.4, migración y expedición con 0.4.5 sobre la misma memoria.
  Sin pérdidas de certeza ni procedencia, y la repetición de una consulta de 0.4.4 se detecta. El mismo escenario
  íntegro en 0.4.4 reasigna 16 de 17 hechos y degrada un hallazgo respaldado.

## Actualización por SSH (0.4.4 → 0.4.5)

```bash
cd ~/src/dieguinPinga
git fetch origin claude/constructor-ia-0.4.5-conocimiento
git checkout claude/constructor-ia-0.4.5-conocimiento
git pull --ff-only
bash ~/src/dieguinPinga/constructor-ia/scripts/update.sh
~/constructor-ia/bin/constructor-ia selftest
~/constructor-ia/bin/constructor-ia explorer-verify caverna_01
```

- `update.sh` hace la copia de la base antes de migrar (INSTALL.md §10). La migración 0006 se aplica sola al
  arrancar.
- `config.toml` no cambia.
- No se tocan Ollama, MariaDB, Node-RED ni otros servicios.

## Rollback (0.4.5 → 0.4.4)

```bash
~/constructor-ia/venv/bin/python3 -c "
import sqlite3, time; c = sqlite3.connect('$HOME/constructor-ia/data/constructor.db')
c.execute(\"UPDATE missions SET state='cancelada', finished_at=?, error='rollback a 0.4.4' WHERE state IN ('pendiente','explorando') AND json_extract(spec_json, '\$.mode')='exploracion'\", (time.time(),)); c.commit()"
~/constructor-ia/bin/rollback.sh
```

- La 0.4.4 ignora las columnas y la tabla nuevas.
- **Con 0.4.4 vuelve el defecto de `upsert`:** las expediciones que se hagan después del rollback pueden
  reasignar o degradar hechos. El historial queda como registro, pero deja de actualizarse.
- Para recuperar exactamente el estado previo a la actualización, restaurar la copia de `update.sh`.

## Qué mirar en la próxima prueba real (dos expediciones sobre la misma base)

1. En la segunda expedición:
   - `consultas_repetidas_sin_justificar` debería bajar a 0 después de las reparaciones;
   - si hay repeticiones justificadas, se verá «sin cambios» o «cambió»;
   - `ciclos_con_conocimiento_nuevo ≥ 1`;
   - más `hechos_nuevos` que `confirmaciones`;
   - la cobertura debería crecer (`cobertura.al_inicio` → `columnas_estudiadas`).
2. `pasos_con_busca_otro`: si es alto, el modelo está esquivando el control de pregunta/herramienta.
3. Afirmaciones del tipo «todas…» sobre `muestra`: deberían aparecer como hipótesis con su alcance, y quizá ser
   verificadas en un paso posterior (sin que nadie se lo ordene).
4. `explorer-hecho` sobre un hallazgo de la primera expedición: debe conservar expedición creadora, texto y
   certeza, con las confirmaciones o reafirmaciones de la segunda en su historial.

## Limitaciones

- La forma del SQL y la clasificación de aportes son **léxicas**: claves de tablas y columnas, cifras y valores
  literales. Una paráfrasis sin valores sobre algo ya estudiado queda `indeterminado`, nunca «nuevo».
- La contradicción solo se marca dentro de una misma expedición. Entre expediciones, una diferencia es
  `difiere_tiempo`, aunque se deba a un error de interpretación. Se prefirió no presumir contradicciones.
- El detector de cuantificadores puede marcar como generalización frases que no lo son (p. ej. «solo» con otro
  sentido). En ese caso la afirmación queda como hipótesis con su alcance, no se descarta.
- `busca` lo declara el propio modelo; si elige «otro», no hay control de coherencia. La métrica
  `pasos_con_busca_otro` permite detectarlo.

---

# Cambios 0.4.6: reparación de «falta la columna» e informe sin mezclar historia

Primera exploración real de 0.4.5 (M20261019-448547, `caverna_01`):
- 10,2 min, 0 consultas, 3 respuestas inválidas y 6 reparaciones.
- Ollama respondió bien: 10 respuestas completas.

Qwen3 preguntaba por los valores distintos de una columna que **nombraba en su propia pregunta**, pero alternaba
entre `perfil(tabla)` sin columna y `frecuentes(tabla)` sin columna. Además, el informe registró F85–F87 como
inferencias respaldadas sin haber ejecutado ninguna consulta.

## Causas (reproducidas)

La secuencia de prueba reproduce exactamente la misión: tres pasos de tres intentos, cada paso terminando con
una forma distinta del error. Con 0.4.5 da **0 consultas, 3 respuestas inválidas y 6 reparaciones**.

1. **El mensaje de reparación engañaba.** Decía «perfil usa: tabla, columna (obligatorios: tabla)». Es cierto para
   contar filas, pero falso para una pregunta por valores distintos.
2. **Dos firmas para el mismo error.** `perfil` sin columna daba `coherencia|…` y `frecuentes` sin columna daba
   `falta_argumento|columna|…`. Alternar las dos formas nunca contaba como repetición: no se activaba el esquema
   estricto y las reparaciones se agotaban.
3. **El esquema estricto de `perfil` no exigía columna** ni ofrecía las columnas válidas.
4. **La columna que el modelo ya había elegido** en su pregunta no se aprovechaba.
5. **El informe mezclaba historia con descubrimientos.** Una afirmación que solo citaba hechos de expediciones
   anteriores se verificaba contra ellos y se registraba como inferencia de la expedición actual.
6. **Las consultas de las aplicaciones propuestas** solo se comprobaban a nivel de tablas, no de columnas.

## Corrección

**Etapa A (el control de coherencia se mantiene igual de estricto):**
- **Se aprovecha la columna que el modelo eligió, de forma transparente.** Si `perfil` o `frecuentes` llegan sin
  columna para una pregunta que la necesita, se usa la columna que el propio modelo eligió:
  - primero su `columna_objetivo`, si es una columna real de la tabla que eligió;
  - si no, su `pregunta`, solo si nombra **exactamente una** columna de esa tabla (coincidencia exacta de palabra,
    sin adivinar por parecido).
  Queda la nota visible «se usó la columna «X» que elegiste en tu pregunta». Si nombra varias o ninguna, no se
  elige nada por él.
- **«Falta la columna» es un único error** (`falta_columna`), venga de `perfil` o de `frecuentes`. La alternancia
  cuenta como repetición, dentro de un paso y entre pasos.
- **Reparación concreta desde el primer intento.** El mensaje dice que la columna es obligatoria para esa pregunta,
  lista las columnas válidas de la tabla que el modelo eligió y, si nombró varias, le pide que elija una. El
  esquema de reparación:
  - exige `tabla` y `columna`;
  - `tabla` = la que eligió el modelo;
  - `columna` = solo columnas reales de esa tabla;
  - herramientas = las que pueden responder lo que busca, más `sql`.
  Si aun así repite el error, no se insiste en ese paso, y dos pasos seguidos con el mismo error detienen la
  exploración.
- **El esquema estricto** de `perfil` exige la columna cuando la pregunta la necesita.

**Informe: tres partes separadas.**
1. **Conocimiento nuevo de esta expedición:** hechos creados en ella. Si no ejecutó consultas, lo dice
   explícitamente.
2. **Conocimiento histórico:** creado antes; se conserva con su procedencia. Incluye lo reobservado o confirmado en
   esta expedición y la **síntesis del agente** sobre lo histórico, es decir, las afirmaciones del informe que solo
   citan hechos de expediciones anteriores. Esas afirmaciones **no** se registran como hechos nuevos.
3. **Propuestas de aplicaciones (no son hallazgos):** se guardan con certeza `hipotesis` y marca `propuesta`. Sus
   consultas se contrastan con el **esquema real mediante `EXPLAIN` en MariaDB**, que verifica tablas y columnas
   sin ejecutar la consulta: «✓ válida para el esquema real» o el error real (p. ej. `Unknown column`). Una
   propuesta sin ninguna consulta válida se descarta.

El mensaje del informe al modelo marca cada hecho como «ESTA EXPEDICIÓN» o «histórico». Si no hubo consultas, le
pide dejar «descubrimientos» vacío. La UI muestra las mismas tres secciones.

**Métricas nuevas:**
- `consultas_propias_con_resultado`;
- `afirmaciones_historicas_del_informe`;
- `consultas_propuestas` (válidas, inválidas, sin verificar);
- motivo explícito en la evaluación cuando la expedición no ejecutó consultas.

**Sin cambios:** el filtro de solo lectura, la verificación de evidencia y la procedencia de la memoria. La prueba
del informe comprueba que el hecho histórico citado conserva certeza, texto y expedición creadora.

## Pruebas

`tests/test_explorer_reparacion.py` (5):
- la secuencia exacta de la misión;
- reparación con columna obligatoria y nombres reales cuando la pregunta no nombra columna;
- alternancia sin resolución tratada como un único error repetido;
- pregunta que nombra varias columnas, sin decidir por el modelo;
- expedición sin consultas: historia y propuestas separadas, consultas propuestas contrastadas con `EXPLAIN`, y
  memoria histórica intacta.

Las cinco fallan con el código 0.4.5.

## Actualización por SSH (0.4.5 → 0.4.6)

```bash
cd ~/src/dieguinPinga
git fetch origin claude/constructor-ia-0.4.6-reparacion
git checkout claude/constructor-ia-0.4.6-reparacion
git pull --ff-only
bash ~/src/dieguinPinga/constructor-ia/scripts/update.sh
~/constructor-ia/bin/constructor-ia selftest
~/constructor-ia/bin/constructor-ia explorer-verify caverna_01
```

Sin migraciones nuevas (el esquema sigue en 0006). `config.toml` no cambia. No se tocan Ollama, MariaDB,
Node-RED ni otros servicios.

## Rollback (0.4.6 → 0.4.5)

```bash
~/constructor-ia/venv/bin/python3 -c "
import sqlite3, time; c = sqlite3.connect('$HOME/constructor-ia/data/constructor.db')
c.execute(\"UPDATE missions SET state='cancelada', finished_at=?, error='rollback a 0.4.5' WHERE state IN ('pendiente','explorando') AND json_extract(spec_json, '\$.mode')='exploracion'\", (time.time(),)); c.commit()"
~/constructor-ia/bin/rollback.sh
```

Sin cambios de esquema que deshacer. Las propuestas creadas por 0.4.6 quedan con certeza `hipotesis`, y las
consultas `EXPLAIN` de validación quedan como consultas del sistema (`tool = validacion`); la 0.4.5 las muestra
como cualquier otra.

## Qué mirar en la próxima prueba

- En la traza de `explorer-report`: «se usó la columna «…» que elegiste en tu pregunta», o reparaciones con «es
  obligatoria para esa pregunta» seguidas de una consulta ejecutada.
- `consultas_propias_con_resultado > 0` y menos `reparaciones` por paso.
- En el informe:
  - la sección 1 solo con lo de esta expedición;
  - la sección 2 con lo histórico y su expedición de origen;
  - la sección 3 con las propuestas, cada consulta marcada como válida o con su error real.

## Limitaciones

- Una pregunta que nombra la columna con otra palabra («el símbolo» por `symbol`) no se aprovecha: no se adivina
  por parecido. En ese caso actúa la reparación con la lista de columnas válidas.
- `EXPLAIN` verifica que la consulta sea válida para el esquema, no que responda bien a lo que la propuesta
  promete.
- Si la expedición termina sin conexión (cancelación), las consultas propuestas quedan «sin verificar» (solo se
  comprobaron las tablas).

---

# Cambios 0.4.7: memoria confiable (alcance real, identificadores, textos cortados, reformulaciones)

Auditoría de la memoria real de 0.4.6: 19 misiones, 64 consultas, 116 hechos, 126 eventos de historial, SQLite
íntegra. Se encontraron cuatro problemas.

## Causas

1. **C61 / F106: cobertura completa certificada sin estar demostrada.**
   - C59 era `perfil` de una columna, C60 `perfil` de otra columna y C61 `frecuentes` de la misma columna que C59, las
     tres de la misma tabla (unas 900 mil filas según la estimación del motor), con `LIMIT 100000` en una subconsulta.
     C59 y C60 quedaron bien registradas como muestra, porque `perfil` informa las filas analizadas; C61 no.
   - Las herramientas del sistema usaban la forma `tool_shape` (sin límites), y las filas analizadas solo se
     conocían para `perfil`. Con `analyzed=None`, `scope` daba `capped=false`.
   - Resultado: se guardó `agregado_total`, `completo=true` y `forma.limit=null`, y F106 («C61 analizó toda la
     tabla») quedó como inferencia respaldada.
   - Además, `claim_scope_check` dejaba pasar tres casos: el alcance declarado («muestra») mandaba sobre un texto que
     decía «toda la tabla»; frases como «la totalidad» no se reconocían; y un alcance desconocido se aceptaba.
2. **F104: nombres buscados como datos.** Todo literal entre comillas se buscaba en el resultado. En un resultado
   agregado solo hay alias, así que el nombre de una tabla o columna nunca aparecía. Al revés, un alias se
   «encontraba» en los encabezados como si fuera un valor.
3. **F97: frases cortadas.** La gramática de Ollama corta la cadena en el `maxLength` del esquema (180 para
   hallazgos, 140 para hipótesis e incertidumbres) aunque la frase no haya terminado.
4. **Reformulaciones contadas como conocimiento nuevo.**
   - Al clasificar, se excluían los hechos nacidos de la misma consulta.
   - Dos hallazgos que decían lo mismo, o la reformulación de una estadística del sistema, salían como «nuevo».
   - Un redondeo (3.2 frente a 3.21) se tomaba como una diferencia.

## Corrección

**Alcance según el SQL ejecutado** (`knowledge.sql_limits`, `scope(sql=…)`):
- Se distinguen el LIMIT exterior (recorta el resultado) y los interiores (subconsultas y CTE: recortan las filas
  analizadas). La forma registrada lleva ambos.
- La cobertura completa se certifica solo si está **demostrada**: sin LIMIT interior, o con menos filas analizadas
  que ese límite, o con un conteo **exacto** de la tabla menor que el límite.
- `frecuentes` informa las filas analizadas solo cuando el resultado muestra todos los grupos (la suma de «veces»).
- Si no se puede demostrar, el alcance es `agregado_muestra` («hasta N filas por un LIMIT interior, de ~M filas»),
  con `cobertura: no_demostrada`.
- `claim_scope_check`: lo que el texto dice explícitamente («toda la tabla», «la totalidad», «el total de
  registros», «analizó toda…») manda sobre el alcance declarado. Si el alcance de la evidencia no se puede
  determinar, una afirmación sobre la tabla completa queda como hipótesis.
- Si la evidencia son solo hechos citados, se usa el alcance de la consulta que originó cada uno.

**Identificadores frente a valores** (`validation.classify_literals`). Cada literal se clasifica:
- **Valor de dato:** debe estar en las celdas del resultado (o en un hecho citado), nunca en los encabezados.
- **Alias:** una columna del resultado o un `AS x` del SQL. Es un nombre, no un dato; presentado como valor («el
  valor «x»») no se acepta.
- **Identificador:** un nombre del esquema que figura en el SQL citado. Si la evidencia no lo usa, queda
  `identificadores_ajenos`.
- **Parámetro:** una cifra del SQL citado (un `LIMIT 100000`, un alias `p95`) se acepta solo si el texto la presenta
  como parámetro: «límite configurado de 100000», «como máximo 100000 filas», «tope», «percentil 95». Un límite
  configurado no demuestra filas leídas. «C61 analizó 100000 registros» habla de filas efectivamente observadas, así
  que esa cifra tiene que estar en el resultado: un perfil la informa; un `frecuentes` que muestra solo los primeros
  grupos, no.

La clasificación queda en `data.literales` del hecho.

**Textos completos:**
- La etapa B pide hasta 2 hallazgos de una frase completa, de hasta 280 caracteres (antes 3 de 180). Las hipótesis
  e incertidumbres van hasta 200 (antes 140). La respuesta más larga posible (~468 tokens) sigue entrando en la
  reserva de 512.
- Un texto que ocupa el límite sin terminar la frase se guarda marcado (`texto_incompleto`), con certeza hipótesis.
- El largo guardado sube de 600 a 1000 caracteres. Las propuestas se recortan de forma visible («[…]»), y el texto
  completo queda en sus datos.

**Conocimiento nuevo:**
- `classify` compara también con los hechos de la misma consulta. Decir lo mismo que uno de ellos es `reformula`;
  coincidir con otra consulta es `confirma`; un valor que es el redondeo de uno conocido es `redondeo_compatible`
  (no demuestra igualdad ni diferencia). Ninguno de los tres es conocimiento nuevo.
- Compartir columnas **no** hace equivalentes a dos hechos de claves distintas. Solo se reconoce una reformulación
  si nació de la misma consulta y no trae ningún valor nuevo.
- `hechos_nuevos` cuenta **unidades** distintas. Si el modelo reformula una estadística que el sistema acaba de
  crear con esa consulta, la unidad es esa estadística, contada una sola vez.
- Métricas nuevas: `reformulaciones`, `redondeos_compatibles` y `afirmaciones_truncadas`. El informe y la UI separan
  «Reformulaciones y confirmaciones (no suman conocimiento nuevo)».

## Reevaluación de la memoria existente (sin cambiar los 116 hechos)

- **Migración 0007** (solo agrega): crea la tabla `explore_reviews` y un índice.
- La **certeza efectiva** de un hecho es la del último evento de su historial. La certeza original sigue en
  `explore_facts.certainty`, y el texto no se toca.
- La interfaz, los mensajes al modelo, la clasificación y `explorer-hecho` usan la certeza efectiva y muestran la
  original y la reevaluación.
- Una evidencia nueva posterior puede volver a subir la certeza efectiva (queda en el historial). La columna original
  nunca baja.

`constructor-ia explorer-reevaluar <conexión>` **solo simula**: abre la base en modo de solo lectura (ni siquiera
migra) y lista qué cambiaría y por qué, con un código de plan. Las reglas se aplican en orden:
1. `alcance_consulta`: el alcance de cada consulta se recalcula con su SQL ejecutado. El alcance original de la
   consulta se conserva y el nuevo queda en la reevaluación.
2. `verificacion`: se vuelve a contrastar, con la fecha original de la afirmación, separando identificadores, alias,
   parámetros y valores.
3. `alcance_afirmacion`: una afirmación sobre la tabla completa cuya evidencia no la cubre pasa a hipótesis.
4. `texto_incompleto`: un texto cortado en un límite anterior (180, 140 o 600) no se certifica.
5. `aporte`: lo contado como nuevo que solo reformula o redondea algo ya registrado. Cambia el aporte, no la certeza.

Aplicar exige `--aplicar --plan <código>` (el de la simulación revisada; con `--solo F106,C61`, el de esa
simulación). Antes de escribir:
- se hace una copia `data/backups/constructor-…-pre-reevaluacion.db`;
- se vuelve a calcular el plan y, si cambió, no se aplica nada.

Cada punto queda en `explore_reviews` (regla, versión, plan, antes y después, evidencia y quién lo autorizó) y, para
los hechos, como evento `reevaluado` del historial. Una reevaluación aplicada no se repite.

## Pruebas

`tests/test_explorer_memoria.py` (22), con datos sintéticos (ningún nombre ni valor de la base auditada):
- C59, C60 y C61 con sus formas exactas: perfil(tabla, columna A), perfil(tabla, columna B) y frecuentes(tabla,
  columna A), con el SQL que genera el propio agente (`LIMIT 100000` interior). La cobertura se demuestra solo cuando
  corresponde, también con los LIMIT interiores del SQL propio del modelo.
- F106 y las frases explícitas sobre la tabla completa; un alcance desconocido no se certifica.
- F104: identificadores en un resultado agregado, nombres ajenos a la evidencia y alias que no son datos.
- Límite configurado frente a filas analizadas: «límite configurado de 100000» se acepta como parámetro; «analizó
  100000 registros» se rechaza contra un `frecuentes` (el resultado no lo informa) y se acepta contra un perfil que
  informa 100000 filas. Si el perfil informa 4321, decir 100000 es falso aunque el LIMIT sea 100000.
- F97: un texto cortado en 179 caracteres frente a una frase completa de ~250.
- Reformulación, confirmación, columnas compartidas que no bastan y redondeo compatible.
- Memoria sintética en estado 0.4.6 con 116 hechos:
  - la simulación no escribe nada (huella del archivo idéntica);
  - aplicar sin el código del plan no escribe;
  - aplicada, los 116 hechos quedan idénticos (texto, certeza original, procedencia y fechas), la certeza efectiva
    cambia solo donde corresponde y la reevaluación no se repite;
  - el comando de consola con `--solo`, la copia de seguridad previa y `explorer-hecho`.
- De punta a punta con el agente real y el MariaDB privado de pruebas, con el tope de muestra en 100 sobre una tabla
  de 300 filas. Con 0.4.6 esta prueba reproduce el defecto: C61 sale `agregado_total, completo=True`.

## Actualización por SSH (0.4.6 → 0.4.7)

```bash
cd ~/src/dieguinPinga
git fetch origin claude/constructor-ia-0.4.7-memoria
git checkout claude/constructor-ia-0.4.7-memoria
git pull --ff-only
bash ~/src/dieguinPinga/constructor-ia/scripts/update.sh
~/constructor-ia/bin/constructor-ia selftest
~/constructor-ia/bin/constructor-ia explorer-verify caverna_01
```

Al arrancar, el servicio aplica la migración 0007 (solo agrega). `config.toml` no cambia. No se tocan Ollama,
MariaDB, Node-RED ni otros servicios.

## Reevaluar la memoria real (después de instalar, solo si lo autorizás)

```bash
~/constructor-ia/bin/constructor-ia explorer-reevaluar caverna_01            # simulación: no escribe nada
~/constructor-ia/bin/constructor-ia explorer-reevaluar caverna_01 --json > ~/reevaluacion-plan.json
# revisar el plan; aplicar SOLO con autorización explícita, todo o una parte:
~/constructor-ia/bin/constructor-ia explorer-reevaluar caverna_01 --solo C61,F106 --aplicar --plan <código>
~/constructor-ia/bin/constructor-ia explorer-hecho caverna_01 106
```

## Rollback (0.4.7 → 0.4.6)

```bash
~/constructor-ia/venv/bin/python3 -c "
import sqlite3, time; c = sqlite3.connect('$HOME/constructor-ia/data/constructor.db')
c.execute(\"UPDATE missions SET state='cancelada', finished_at=?, error='rollback a 0.4.6' WHERE state IN ('pendiente','explorando') AND json_extract(spec_json, '\$.mode')='exploracion'\", (time.time(),)); c.commit()"
~/constructor-ia/bin/rollback.sh
```

- 0.4.6 funciona sobre la base migrada: ignora `explore_reviews` y usa la certeza original de cada hecho, que nunca
  se modificó.
- Si 0.4.6 vuelve a observar un hecho reevaluado, su evento queda último en el historial. Al volver a 0.4.7, simular
  de nuevo con `explorer-reevaluar`: la regla que corresponda vuelve a proponerse.
- Si se aplicó una reevaluación y se quiere volver exactamente al estado anterior, restaurar la copia
  `data/backups/constructor-…-pre-reevaluacion.db` con el servicio detenido (INSTALL.md §10, Recuperación ante fallos).

## Limitaciones

- Un texto cortado se detecta por ocupar el límite sin terminar la frase. Una frase completa que termina sin
  puntuación justo en el límite también se marca: es un error conservador.
- El alcance recalculado de consultas viejas usa las filas de la tabla registradas entonces (o la estimación del
  último esquema). No se vuelve a consultar MariaDB.
- `redondeo_compatible` se reconoce cuando el valor conocido tiene más decimales. Otras equivalencias (porcentajes,
  unidades) quedan como diferencias o como `indeterminado`.
