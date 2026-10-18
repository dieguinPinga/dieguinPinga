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
