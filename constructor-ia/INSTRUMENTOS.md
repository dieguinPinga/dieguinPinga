# Instrumentos de investigación (0.6.1-exp1): Fase 0 y Fase 1

> **0.6.1:** después de la primera prueba real (Qwen3 4B, T1) se separó «terminó» de «midió», se agregó un
> diagnóstico compacto para reparar instrumentos y se distinguen las fallas de formato, de instrumento y de método.
> Detalle en [§0.6.1](#061-diagnóstico-de-la-primera-prueba-real-y-reparación).

Un **instrumento** es un programa Python pequeño y reutilizable, escrito por el modelo. Tiene una función
`medir(db, p)`, corre aislado y consulta MariaDB solamente a través de un canal de lectura. Devuelve una medición
compacta y verificable. Existe porque el catálogo fijo de herramientas del Explorador no podía expresar mediciones
nuevas, como un histograma (prueba M20261019-023c0d). Las primitivas son fijas y las mediciones las inventa el
modelo.

- **Fase 0, implementada:** el canal seguro entre el Python aislado y el intermediario MariaDB, más el almacén de
  instrumentos y la verificación de sus resultados.
- **Fase 1, implementada:** un laboratorio para comparar modelos (por ejemplo Qwen3 4B y Qwen3 1.7B) mientras
  escriben, evalúan, corrigen y reutilizan instrumentos sobre una base sintética con verdad conocida.
- **Fase 2, NO implementada:** la investigación autónoma completa con instrumentos. Tampoco hay integración con la
  interfaz web ni con `investigar`.

## Arquitectura

```
modelo ──(manifiesto + código)──▶ revisión estática (AST) ──▶ almacén (versión inmutable)
                                                                   │
                                   ┌───────────────────────────────┘
                                   ▼
   sandbox (bwrap --unshare-all, seccomp, cgroup, sin red, sin credenciales, sin archivos del anfitrión)
   ┌──────────────────────────────────────────────────────────────┐
   │ arnés confiable (no volcable)   ◀─ JSON ─▶  instrumento (no confiable) │
   │   firma HMAC cada pedido                       medir(db, p) usa investiga.py │
   └───────────────┬──────────────────────────────────────────────┘
          stdout: CIA-RPC <json> <hmac>        stdin: respuesta JSON
                   ▼
   anfitrión: Executor (verifica HMAC; una línea falsa = muerte del sandbox)
          → Canal (presupuestos operativos, necesidades) → Broker (cuenta de solo lectura, sqlguard,
            límites de sesión, explore_queries con origen «instrumento»)
                   ▼
   verificación (contrato, cobertura declarada frente a la real, recortes) → huella de los datos → registro
```

### El canal (Fase 0)

- El proceso del instrumento solo habla con el arnés, a través de descriptores heredados. El arnés firma cada
  pedido con el nonce de la ejecución. El instrumento no conoce ese nonce y no puede imprimir líneas válidas para el
  anfitrión. Si el anfitrión recibe una línea con firma inválida, mata la ejecución.
- `investiga.py` ofrece cuatro primitivas: `consultar(sql, max_filas)`, `estimar(sql)` (con EXPLAIN y sin ejecutar
  la consulta), `esquema()` y `presupuesto()`. `Resultado.valores(col)` convierte a número los DECIMAL, que llegan
  como texto. Es comunicación, no un catálogo de mediciones.
- Cada consulta pasa por el **mismo intermediario** que usa el Explorador. Ese intermediario aplica la cuenta de
  solo lectura, sqlguard, `max_statement_time`, el bloqueo de escrituras y DDL y el registro en `explore_queries`.
  Las consultas de instrumentos quedan con `origin='instrumento'` y `tool='inst:<módulo>'`, y tienen sus propios
  topes de filas y de bytes en el intermediario.
- El tiempo de cálculo del instrumento **no incluye la espera de MariaDB**. Una consulta lenta no consume el
  presupuesto de cálculo; tiene su propio tope (`query_timeout_s` del explorador).

### Presupuestos (operativos, no metodológicos)

| Recurso | Valor por defecto | `[instrumentos]` |
|---|---|---|
| Consultas por ejecución | 20 | `consultas` |
| Filas transferidas | 50 000 | `filas` |
| Bytes transferidos | 8 MB | `bytes_mb` |
| Cálculo (sin contar la espera de MariaDB) | 60 s | `segundos` |
| Resultado estructurado | 4 KB | `resultado_kb` |
| Código del instrumento | 24 KB | `codigo_kb` |

Los millones de registros se estudian **agregando en SQL**: con COUNT, SUM, AVG, GROUP BY o `FLOOR(valor/ancho)`
se transfieren pocas filas. Cuando un presupuesto no alcanza, el canal **no recorta en silencio**:

- registra una *necesidad*: qué recurso, cuánto se pidió, cuánto quedaba y en qué consulta;
- le avisa al instrumento, con `truncado` y `motivo` o con la excepción `PresupuestoAgotado`;
- la verificación marca el resultado como parcial y distingue si el instrumento lo declaró o no
  (`parcial_sin_declarar`). Una muestra recortada nunca se presenta como si representara al universo.

Las necesidades quedan en la ejecución (`accounting.necesidades`). Así se puede evaluar si conviene subir un tope.

### Contrato y verificación

`medir` devuelve `{"resultado": {...}, "cobertura": {"filas_analizadas": n, ...}, "limitaciones": [...]}`.
El contenido de `resultado` es libre: lo inventa el instrumento. La verificación determinista revisa lo siguiente:

- **contrato:** que sea un objeto JSON, que tenga las tres claves y que no supere el tamaño máximo;
- **cobertura:** contrasta `filas_analizadas` con lo que el canal entregó:
  - `demostrada`: la cifra coincide con un número devuelto por una consulta agregada (un COUNT, por ejemplo) o con
    las filas crudas recibidas;
  - `compatible`: es menor que las filas recibidas;
  - `no_demostrada`: el instrumento afirma más de lo que pudo ver;
  - `nula`: declara 0 filas (0.6.1: antes podía salir «demostrada» si algún resultado agregado valía 0);
  - `no_declarada`;
- **recortes:** si alguna consulta fue truncada o se registraron necesidades, y si el instrumento lo declaró;
- **medición** (0.6.1), separada del estado técnico:

  | Estado | Significa |
  |---|---|
  | `contrato_invalido` | la salida no tiene la forma acordada |
  | `error_de_calculo` | el instrumento capturó una excepción y devolvió un resultado sin datos, o la mencionó en su salida |
  | `sin_evidencia` | 0 filas analizadas, ninguna fila recibida o resultado vacío |
  | `parcial` | algún tope recortó los datos |
  | `valida` | resultado estructurado con datos detrás; puede llevar advertencias (excepciones capturadas que no lo vaciaron) |

  Las excepciones capturadas por el propio instrumento se registran dentro del sandbox (tipo, mensaje, línea y
  código de esa línea) aunque el programa termine «bien».

La verificación no decide si la medición responde la pregunta. Eso lo evalúa el modelo y, en el laboratorio, la
verdad conocida.

### Revisión estática (defensa adicional)

La barrera real es el sandbox. Además, antes de ejecutar se rechazan:

- importaciones fuera de la lista permitida: math, statistics, collections, datetime, json, itertools, bisect,
  decimal, re, functools, heapq, fractions, random, operator, string e investiga;
- `eval`, `exec`, `compile`, `open`, `__import__`, `getattr`, `setattr` y llamadas parecidas;
- atributos y nombres con doble guion bajo;
- `global` y `nonlocal`;
- código sin `medir(db, p)`.

**Extensible sin reconstruir:** `[instrumentos] modulos_extra = ["numpy"]` agrega una capacidad ya verificada (debe
estar instalada en `/usr`, que el sandbox monta como solo lectura). Hay una lista de módulos que **nunca** se
habilitan, ni siquiera por configuración: os, sys, subprocess, socket, ctypes, importlib, pickle, io, pathlib,
threading, urllib, http y otros. Si `modulos_extra` incluye alguno, la configuración se rechaza.

### Almacén, reutilización y memoria

- **Instrumentos:** son versiones **inmutables** (tabla `inst_instruments`). El mismo nombre con el mismo código es la
  misma versión. Otro código crea una versión nueva, enlazada con la anterior (`parent_id`). Se conservan siempre:
  son la receta para volver a medir. Cada instrumento guarda manifiesto (propósito, parámetros, plan, muestreo),
  autor, misión y notas.
- **Reutilización:**
  - con otros parámetros o con datos nuevos, el mismo instrumento se ejecuta sin reescribirlo;
  - un instrumento puede importar instrumentos anteriores como bibliotecas
    (`import tardias_v1`, montado en `bib/tardias_v1.py`), que también pasan la revisión estática.
- **Ejecuciones** (tabla `inst_runs`): cada una guarda la receta exacta (versión y parámetros), las consultas
  realizadas (con su referencia en `explore_queries`), el veredicto, la huella de los datos, el costo y la salida.
- **Caducidad por tiempo y por cuota**, ambas configurables (`retencion_dias` = 30 y `cuota_mb` = 50). Al caducar, se
  descarta la salida y la ejecución queda `resumida`. Se conservan los metadatos: receta, parámetros, consultas,
  huella y hash de la salida.
- **Evidencia indispensable:** `Almacen.pin(run_id, motivo)` ancla una ejecución, que entonces no caduca. MariaDB
  cambia, así que el programa y sus parámetros no siempre bastan para reproducir lo observado. La salida anclada,
  junto con las consultas que la produjeron, es la evidencia mínima de lo que se vio originalmente.
- **Huella de los datos:** para cada tabla consultada se guardan las estimaciones del motor (`TABLE_ROWS`,
  `UPDATE_TIME`, `DATA_LENGTH`). `Almacen.staleness(run_id, broker)` informa si la evidencia está «posiblemente
  desactualizada». Usa estimaciones: detecta cambios, pero no los demuestra. Por ejemplo, una corrección del mismo
  tamaño puede pasar inadvertida.

Los conceptos, las hipótesis y las relaciones entre investigaciones e instrumentos pertenecen a la Fase 2. En esta
fase solo existe su soporte: la misión y el autor de cada instrumento y de cada ejecución, el anclaje y las notas.

## Laboratorio (Fase 1)

### Base sintética con verdad conocida

`constructor_ia/laboratorio/sintetica.py` usa un dominio neutro: estaciones que registran lecturas. Es determinista
por semilla. Fenómenos sembrados:

| Fenómeno | Detalle |
|---|---|
| Distribución bimodal | La mayoría de las lecturas sigue ~N(20, 2.5); la zona «alta» sigue ~N(41, 4). |
| Relación oculta | La zona, que está en `estaciones`, explica el segundo modo, que aparece en `lecturas`. |
| Anomalías | 40 lecturas con valor 999.9 (falla de sensor). |
| Estación trabada | Una estación registra exactamente 0.0 durante 7 días. |
| Huecos | Un hueco global de 7 h y una estación sin datos durante 3 días. |
| Llegadas tardías | El 2 % llega entre 6 y 48 h tarde, y una estación llega tarde el 15 % de las veces. |
| Correcciones | La tabla `correcciones` guarda valores corregidos días después. |
| Dinámica | `avanzar()` agrega un día, registros tardíos en días ya cerrados y más correcciones. |

Escalas disponibles:

- `pequena`: ~15 mil lecturas;
- `normal`: ~120 mil, más que el presupuesto de filas, de modo que obliga a agregar;
- `grande`: ~900 mil.

### Tareas y puntaje

La verdad se mide con SQL como root sobre el MariaDB privado, fuera del sandbox y sin el modelo. El puntaje es 1, 0.5
o 0, siempre con el detalle que lo justifica. Las preguntas no revelan qué se sembró.

| Tarea | Pregunta | Se puntúa contra |
|---|---|---|
| T1 | ¿Dónde se concentran los valores? | Los intervalos de ancho 2 con ≥85 % del máximo. El intervalo informado debe contener uno y medir ≤6 de ancho (≤12 da 0.5). |
| T2 | ¿Cuál es el período más largo sin lecturas? | El hueco real, calculado con LAG. Se exige superposición ≥50 % y que el hueco informado no mida más del doble. |
| T3 | ¿Qué grupo se comporta distinto? | La zona, o las estaciones con un promedio mayor que el global + 10 (Jaccard ≥0.75). |
| T4 | ¿Qué proporción llega más de 1 h tarde? | La proporción exacta, con error ≤10 % (≤30 % da 0.5). El instrumento debe aceptar `desde` y `hasta`. |
| T5 | ¿Hay valores imposibles o repetidos? | 999.9 con su conteo exacto y 0.0 (la estación trabada) con un margen del 5 %. Cada uno vale 0.5. |
| C | Igual que T3, a partir de un instrumento sembrado que solo calcula mínimo, máximo y promedio globales. | Se mide si el modelo detecta que no alcanza y si lo corrige con una versión 2. |
| R | Con datos nuevos (`avanzar`): la proporción de llegadas tardías en los últimos 3 días. | El modelo debe elegir el instrumento de T4 y proponer **solo parámetros**. Se puntúa contra la verdad recalculada. |

### Protocolo

Para cada tarea:

1. **Diseño:** el modelo responde un manifiesto JSON y el código.
2. **Ejecución:** revisión estática, sandbox con canal y verificación (estado técnico y estado de la medición).
3. **Si no midió** (falla técnica, `error_de_calculo`, `sin_evidencia` o `contrato_invalido`): **reparación**. No se
   pide autoevaluación de algo que no midió. El modelo recibe el código actual y un diagnóstico compacto (ver §0.6.1)
   y devuelve una versión nueva con el campo `causa` (qué causaba la falla y qué cambió).
4. **Si midió:** **autoevaluación** conceptual (`responde` si/parcial/no, `motivo`, `respuesta` en la forma pedida,
   `mejora`). Si dice «no» o «parcial»: **revisión del método**.
5. **Si la autoevaluación no se puede leer:** se diagnostica la respuesta y se pide **reparar el formato**
   (`--reparaciones-formato`, 1 por defecto) sin tocar el instrumento; si sigue ilegible se rescata lo completo de un
   JSON cortado; si nada sirve, la tarea termina como «evaluación ilegible», sin reescribir el instrumento.
6. **Presupuesto:** hasta `--correcciones` versiones nuevas por tarea (3 por defecto, reparaciones y revisiones
   juntas). La reparación se corta antes si la misma falla aparece `--mismo-error` veces seguidas (3 por defecto).
7. **Puntaje:** la respuesta final se compara con la verdad. La autoevaluación se contrasta con el puntaje
   (calibración).

**Mismas condiciones para todos los modelos:** misma base (semilla), prompts, presupuestos, `num_ctx`, temperatura,
tokens máximos y máximo de correcciones. Cada modelo y cada repetición tienen su propia base, idéntica a las demás, y
su propio almacén, así que un modelo no reutiliza instrumentos de otro. Si el sandbox queda bloqueado o el operador
cancela, el laboratorio se **aborta**: una falla de infraestructura no se le cobra al modelo.

### Métricas (por modelo)

- **Resultados:** puntaje medio y tareas resueltas.
- **Formato:** código con el formato pedido; autoevaluación con el formato pedido en la primera respuesta,
  autoevaluaciones con formato reparado, rescatadas e ilegibles.
- **Ejecución y verificación:** rechazos estáticos, ejecuciones técnicamente correctas, mediciones válidas, errores
  de cálculo, mediciones sin evidencia, cobertura demostrada (sobre mediciones válidas) y parciales sin declarar.
- **Autoevaluación:** calibrada, sobreconfianza (dijo «si» y falló) y subconfianza.
- **Corrección y reutilización:** versiones nuevas escritas; reparaciones, reparaciones que eliminaron la falla,
  reparaciones con el mismo error o sin cambios, tareas cortadas por error persistente; revisiones del método; tareas
  corregidas que terminaron resueltas, si detectó el instrumento insuficiente (C) y si reutilizó sin reescribir (R).
- **Costo:** llamadas, llamadas fallidas, tokens de entrada y generados, segundos de modelo y segundos totales.

### Ejecución en la VivoBook (la hacen ustedes, cuando lo decidan)

```bash
cd ~/src/dieguinPinga
git fetch origin claude/constructor-ia-0.6.1-reparacion
git checkout claude/constructor-ia-0.6.1-reparacion
git pull --ff-only
~/constructor-ia/bin/constructor-ia backup                # copia de la base SQLite antes de actualizar
bash ~/src/dieguinPinga/constructor-ia/scripts/update.sh
~/constructor-ia/bin/constructor-ia selftest              # debe decir AISLAMIENTO OPERATIVO
systemctl --user stop constructor-ia                      # para no competir por Ollama CPU
~/constructor-ia/bin/constructor-ia laboratorio-instrumentos --yes \
    --modelos qwen3:4b-instruct,qwen3:1.7b --escala normal --repeticiones 1
systemctl --user start constructor-ia
# comparar dos corridas (por ejemplo, un modelo por noche):
~/constructor-ia/bin/constructor-ia laboratorio-comparar <a>/resultados.json <b>/resultados.json
# revisar una corrida (solo lectura; también las de 0.6.0): llamadas, textos crudos, código y consultas
~/constructor-ia/bin/constructor-ia laboratorio-revisar ~/constructor-ia/data/laboratorio/<fecha>
```

- Para una primera prueba corta: `--tareas T1,T4,R --escala pequena`.
- Los resultados quedan en `~/constructor-ia/data/laboratorio/<fecha>/`:
  - `resultados.json` guarda todo lo medido, incluidas las llamadas, las versiones, las autoevaluaciones y la verdad;
  - `resumen.md`;
  - una base SQLite por modelo y repetición, con los instrumentos y las consultas.
- `laboratorio-comparar` avisa cuando las condiciones de las dos corridas no son iguales.
- Ctrl-C cancela. La corrida queda `abortada` y el informe se escribe igual.

**El MariaDB del laboratorio es PRIVADO.** Usa los binarios instalados con `--no-defaults` (no lee my.cnf), un
datadir y un socket propios dentro de `data/laboratorio/<fecha>/mariadb`, `--skip-networking` (sin puerto TCP) y un
buffer de 64 MB. Corre como el usuario de constructor-ia y no como root. Al terminar se detiene y se borra; con
`--conservar-mariadb` el datadir queda disponible para inspección. **No toca el servicio MariaDB, su configuración ni
sus bases.** La cuenta de solo lectura del laboratorio usa una clave aleatoria y desechable, guardada en un archivo
0600 que se borra al terminar.

Esto no se comprobó en la VivoBook: no tengo acceso a ella. Si AppArmor u otra política impidiera arrancar un
`mariadbd` con un datadir propio, el comando falla cerrado y muestra el `err.log`. El laboratorio se compara contra
Ollama CPU (`ollama_url`, puerto 11435) y no descarga modelos. Si falta alguno, lo informa y termina.

## Rollback

```bash
~/constructor-ia/bin/rollback.sh          # vuelve a la versión anterior instalada (app.prev)
```

- La migración 0009 solo agrega las tablas `inst_instruments` e `inst_runs`; la versión 0.5 las ignora.
- No cambia hechos, reevaluaciones ni tablas existentes.
- El laboratorio no escribe en la base SQLite del servicio: usa sus propias bases dentro de `data/laboratorio/`, que
  se pueden borrar sin efecto alguno.
- Para volver a la base exacta anterior, usar la copia hecha con `constructor-ia backup`.
- Las ramas anteriores (0.4.x y `claude/constructor-ia-0.5-investigador`) siguen publicadas sin cambios.

## Pruebas

- **`tests/test_instrumentos.py`:** Fase 0 con bubblewrap y seccomp reales y un MariaDB privado. Cubre:
  - agregación en SQL con cobertura demostrada;
  - escrituras rechazadas con el checksum intacto;
  - sin red, credenciales ni archivos del anfitrión;
  - líneas falsificadas;
  - presupuestos y necesidades;
  - cobertura mentida y salida excesiva;
  - error, timeout y rechazo;
  - reutilización con parámetros, bibliotecas y huella;
  - caducidad por tiempo y por cuota sin tocar lo anclado;
  - la sección `[instrumentos]` de la configuración.
- **`tests/test_laboratorio.py`:** Fase 1. Cubre:
  - el analizador de respuestas, el puntaje contra verdades conocidas y la calibración;
  - los fenómenos sembrados y el cambio de datos;
  - el MariaDB privado (sin red y borrado al terminar);
  - el circuito completo con dos guiones de Ollama simulado, uno competente y uno incompetente;
  - `laboratorio-revisar`.
- **`tests/test_reparacion.py`** (0.6.1): regresiones de la primera prueba real, con sandbox y MariaDB reales:
  1. un programa que captura una excepción y devuelve un resultado vacío → `error_de_calculo`, con la excepción, la
     línea y su código;
  2. cobertura declarada 0 después de un error → `nula`, aunque un resultado agregado valga 0;
  3. un valor numérico de SQL que llega como texto → el diagnóstico muestra `DECIMAL→str` y la línea del error;
     `Resultado.tipos` dentro del sandbox;
  4. una autoevaluación ilegible (cortada por el límite, JSON inválido) → se diagnostica, se repara el formato, se
     rescata o queda ilegible, **sin reescribir el instrumento**; el texto crudo completo queda guardado;
  5. una reparación que no corrige el error → «MISMO ERROR» con el último cambio a la vista y corte acotado; y la
     misma falla reparada en el segundo intento (una primera falla no es incapacidad);
  6. una medición válida que no responde la pregunta → revisión del **método**, no reparación;
  - y la secuencia completa de la corrida de T1 con el circuito nuevo; `laboratorio-revisar` sobre resultados de 0.6.0.

**Las pruebas con guiones validan el circuito, no la capacidad de un modelo.** Esa capacidad se mide con
`laboratorio-instrumentos` y los modelos reales.

## Limitaciones conocidas

- **Cobertura «demostrada»:** basta con que la cifra declarada aparezca entre los números de alguna consulta
  agregada (o entre las sumas de sus columnas enteras, desde 0.6.1). Es una comprobación de plausibilidad, no una
  prueba de que el cálculo use esas filas.
- **Excepciones capturadas:** se observan con `sys.settrace` en los marcos del instrumento (sin eventos por línea).
  Es un diagnóstico, no una barrera: el instrumento no puede importar `sys` para apagarlo. Agrega un costo pequeño a
  cada llamada a función de Python.
- **Huella:** se basa en estimaciones del motor (ver arriba).
- **Puntajes:** las tolerancias son explícitas, pero son una decisión de diseño. Con una sola repetición, una
  diferencia de una tarea entre modelos puede deberse al azar del muestreo.
- **Prompts:** están en español y son iguales para ambos modelos. No se ajustaron a ninguno de los dos.
- **Ejecución en la VivoBook:** no se midió el tiempo que tomará. Cada tarea implica entre 2 y 6 llamadas al modelo.

## 0.6.1: diagnóstico de la primera prueba real y reparación

Corrida: `laboratorio-instrumentos --modelos qwen3:4b-instruct --tareas T1 --escala pequena --correcciones 1`
(0.6.0-exp1, commit `8787f7b`); 822,7 s, 4 llamadas, 2.772 tokens generados, puntaje 0.

### Qué falló en el sistema (verificado en el código)

1. **Falso positivo de cobertura.** El canal guardaba todos los números devueltos por consultas agregadas para
   contrastar la cobertura. `filas_analizadas: 0` coincidía con cualquier 0 de esos resultados, por ejemplo el índice
   del primer intervalo de `FLOOR((valor - mínimo) / ancho)`. Por eso 0 filas salían «demostradas». Corregido:
   cobertura `nula`.
2. **«Terminó» se confundía con «midió».** El instrumento atrapó el `TypeError` y devolvió `{"desde": 0, "hasta": 0}`.
   El sandbox informaba `ok` y nada miraba si hubo un error atrapado. Corregido: estado de la medición
   (`error_de_calculo`) y registro de excepciones capturadas.
3. **Una autoevaluación ilegible provocaba reescribir el instrumento.** En 0.6.0, si la autoevaluación no se podía
   leer, el siguiente paso era una «corrección» del código. Corregido: falla de formato → reparar la respuesta.
4. **La corrección no tenía información para encontrar la causa.** El prompt de corrección de 0.6.0 decía sólo
   «Salida: {…} (la evaluación no tuvo el formato pedido)». No incluía la línea, el código de esa línea ni los tipos de
   los datos recibidos. El único rastro del error era el texto que el propio instrumento puso en `limitaciones`.
   Con eso, conservar el error en la versión 2 es esperable. Corregido: diagnóstico compacto (abajo).
5. **Se guardaban sólo 300 caracteres** de la respuesta rechazada. Corregido: hasta 8000, con el diagnóstico.

### Por qué se rechazaron las dos autoevaluaciones (lo que se puede y no se puede afirmar)

En 0.6.0 una autoevaluación se rechazaba si:

- el texto no era JSON válido;
- no era un objeto;
- `responde` no era exactamente `si`, `parcial` o `no` (un `sí` con tilde también se rechazaba).

Según el informe, ambas empezaban con `{"responde": "no", …`, así que el valor era válido. Las causas posibles que
quedan son dos:

- **cortada por el límite de tokens** (`num_predict_json` = 768). Con el formato JSON de Ollama la salida respeta la
  gramática, así que una respuesta completa debería ser válida. Que no lo fuera apunta a un corte;
- **JSON inválido** por otra causa.

Los números son compatibles con el corte: si cada evaluación usó 768 tokens, quedan 1.236 para las dos escrituras de
código. Pero eso **no lo demuestra**. `resultados.json` de esa corrida ya guarda, por llamada, `resultado`
(`truncada` o `completa`) y `tokens_generados`. Lo resuelve, sin modificar nada:

```bash
~/constructor-ia/bin/constructor-ia laboratorio-revisar ~/constructor-ia/data/laboratorio/<fecha>
```

Si las líneas `llamada evaluacion-T1` dicen `truncada, 768 tokens generados`, fue el límite. Si dicen `completa`, el
texto crudo mostrado (los 300 caracteres que se guardaron) indica el error. El mismo comando muestra:

- el código de `rango_lecturas_v1` y `rango_lecturas_v2`;
- las consultas que hicieron.

Con eso se puede estudiar la segunda sospecha, la reconstrucción de los intervalos relativos al mínimo. **No pude
revisar ese código: no está en el repositorio ni tengo acceso a la VivoBook.**

Desde 0.6.1 el pedido de evaluación es explícitamente breve (motivo y mejora de una o dos frases), `sí` se acepta y
cada falla de formato queda diagnosticada (causa, detalle con la posición del error, fin, tokens y texto crudo).

### El diagnóstico de reparación

Ejemplo real de las pruebas (un DECIMAL dividido por un entero, en el segundo intento fallido):

```
Estado técnico: error.
Error sin capturar: TypeError: unsupported operand type(s) for /: 'str' and 'int' en instrumento.py línea 5 (medir): `prop = tarde / n`
Datos recibidos:
  C7 [ok] 1 filas: SELECT SUM(TIMESTAMPDIFF(SECOND, momento, recibido) > 3600) AS tarde, COUNT(*) AS n FROM lecturas
      columnas: tarde DECIMAL→str '135', n BIGINT→int 4767
Intentos anteriores:
- v1: TypeError: unsupported operand type(s) for /: 'str' and 'int'
- v2: TypeError: … → MISMO ERROR que la versión anterior: el cambio no lo corrigió
Último cambio (v1 → v2):
@@ -7 +7 @@
-            "limitaciones": []}
+            "limitaciones": ["proporción global"]}
```

Además incluye el código actual completo, porque hace falta para modificarlo. No incluye las versiones anteriores
completas (sólo el último cambio) ni registros largos. El prompt entero ronda los 3 KB. El diagnóstico es general:
`constructor_ia/instrumentos/diagnostico.py` no sabe nada del laboratorio ni de T1. La «firma» de una falla (tipo y
mensaje, sin números) permite reconocer que una reparación no cambió el error aunque cambien las líneas.

### Una decisión que queda para ustedes: ¿DECIMAL como número?

La causa del error de T1 es una trampa de nuestro transporte: MariaDB devuelve DECIMAL (también `AVG()`, `SUM()` de
expresiones y `MIN()`/`FLOOR()` de columnas DECIMAL) y el canal lo entrega como texto para no perder precisión.
Convertirlo a número en el canal eliminaría la trampa para cualquier modelo. **No lo cambié**, por dos razones:

- cambiaría las condiciones respecto de la corrida 0.6.0;
- haría desaparecer justamente el error con el que quieren medir la capacidad de reparación.

Ahora el tipo es visible: está en el diagnóstico, en `Resultado.tipos` y en la documentación del canal. Si después
de la próxima corrida prefieren quitar la trampa, es un cambio pequeño y localizado en `canal.py`.

### Rollback de 0.6.1

`~/constructor-ia/bin/rollback.sh` vuelve a 0.6.0. No hay migraciones nuevas: los campos nuevos viajan dentro de
`verdict_json` y de `resultados.json`. La rama `claude/constructor-ia-0.6-instrumentos` sigue publicada sin cambios,
y los resultados de la corrida real no se tocaron.
