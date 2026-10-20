# Instrumentos de investigación (0.6.0-exp1): Fase 0 y Fase 1

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
  - `no_declarada`;
- **recortes:** si alguna consulta fue truncada o se registraron necesidades, y si el instrumento lo declaró.

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
2. **Ejecución:** revisión estática, sandbox con canal y verificación.
3. **Autoevaluación:** el modelo indica `responde` (si, parcial o no), el `motivo`, la `respuesta` en la forma pedida
   y una `mejora`.
4. **Corrección:** si la ejecución falló o la autoevaluación no fue «si», el modelo escribe una versión nueva, hasta
   `--correcciones` veces (2 por defecto).
5. **Puntaje:** la respuesta final se compara con la verdad. La autoevaluación se contrasta con el puntaje
   (calibración).

**Mismas condiciones para todos los modelos:** misma base (semilla), prompts, presupuestos, `num_ctx`, temperatura,
tokens máximos y máximo de correcciones. Cada modelo y cada repetición tienen su propia base, idéntica a las demás, y
su propio almacén, así que un modelo no reutiliza instrumentos de otro. Si el sandbox queda bloqueado o el operador
cancela, el laboratorio se **aborta**: una falla de infraestructura no se le cobra al modelo.

### Métricas (por modelo)

- **Resultados:** puntaje medio y tareas resueltas.
- **Formato:** código con el formato pedido y autoevaluación con el formato pedido.
- **Ejecución y verificación:** rechazos estáticos, ejecuciones correctas, cobertura demostrada y parciales sin
  declarar.
- **Autoevaluación:** calibrada, sobreconfianza (dijo «si» y falló) y subconfianza.
- **Corrección y reutilización:** correcciones escritas, correcciones que repitieron el mismo código, tareas
  corregidas que terminaron resueltas, si detectó el instrumento insuficiente (C) y si reutilizó sin reescribir (R).
- **Costo:** llamadas, llamadas fallidas, tokens de entrada y generados, segundos de modelo y segundos totales.

### Ejecución en la VivoBook (la hacen ustedes, cuando lo decidan)

```bash
cd ~/src/dieguinPinga
git fetch origin claude/constructor-ia-0.6-instrumentos
git checkout claude/constructor-ia-0.6-instrumentos
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
  - el circuito completo con dos guiones de Ollama simulado, uno competente y uno incompetente.

**Las pruebas con guiones validan el circuito, no la capacidad de un modelo.** Esa capacidad se mide con
`laboratorio-instrumentos` y los modelos reales.

## Limitaciones conocidas

- **Cobertura «demostrada»:** basta con que la cifra declarada aparezca entre los números de alguna consulta
  agregada. Es una comprobación de plausibilidad, no una prueba de que el cálculo use esas filas.
- **Huella:** se basa en estimaciones del motor (ver arriba).
- **Puntajes:** las tolerancias son explícitas, pero son una decisión de diseño. Con una sola repetición, una
  diferencia de una tarea entre modelos puede deberse al azar del muestreo.
- **Prompts:** están en español y son iguales para ambos modelos. No se ajustaron a ninguno de los dos.
- **Ejecución en la VivoBook:** no se midió el tiempo que tomará. Cada tarea implica entre 2 y 6 llamadas al modelo.
