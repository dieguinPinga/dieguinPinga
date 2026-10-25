# oh-lab: laboratorio de reparación con varias misiones

Pasa de un solo defecto (`oh-smoke`) a un **banco de 12 programas distintos**. Cada misión se resuelve con el
supervisor de `oh-supervisor` (hasta 3 intentos independientes) y el laboratorio sigue con la siguiente, sin
intervención. Al final, y después de cada misión, deja un informe JSON.

No modifica `oh-smoke/`, `oh-supervisor/`, `constructor-ia`, Ollama ni ningún servicio. No agrega modelos ni
paralelismo. Solo biblioteca estándar.

```
oh-lab/
  banco/<ID>/                 una misión: el agente solo recibe tarea/
    mision.json               id, categoría, módulo, función, descripción del defecto (no sale del banco)
    tarea/<mod>.py            código con el defecto
    tarea/test_<mod>.py       pruebas visibles (una falla por el defecto)
    ocultas/test_ocultas.py   evaluación reservada, con casos distintos de los visibles
    control/<mod>.py          solución de referencia (solo para el control previo)
  ejecutor/correr.sh          oh-smoke/correr.sh parametrizado por mision.json
  ejecutor/agente.py          oh-smoke/smoke.py parametrizado por OH_MODULO
  lab.py                      orquestador de misiones
  tests/                      pruebas del banco y del laboratorio (ejecutor simulado, sin Ollama)
  ARQUITECTURA.md             VivoBook ↔ Pulenta y requisitos para sumar computadoras
```

## 1. Banco de problemas

| ID | Categoría | Programa | Defecto |
|---|---|---|---|
| C01 | cálculo | `promedio_ponderado` | divide por la cantidad de pesos en lugar de por su suma |
| C02 | cálculo | `percentil` (rango más cercano) | usa el rango como índice desde cero |
| C03 | cálculo | `a_hms` (segundos a HH:MM:SS) | minutos sobre el total en lugar del resto de la hora |
| C04 | cálculo | `area` (fórmula del agrimensor) | falta dividir por dos |
| S01 | sobrescritura | `contar_palabras` | cada aparición reemplaza el conteo por 1 |
| S02 | sobrescritura | `agrupar` | cada registro reemplaza la lista de su grupo |
| S03 | sobrescritura | `aplanar` | cada sublista reemplaza el acumulado |
| S04 | sobrescritura | `fusionar` (configuración) | una sección anidada reemplaza a la de base en lugar de combinarse |
| K01 | condición | `es_bisiesto` | «y» en lugar de «o» en la excepción de los divisibles por 400 |
| K02 | condición | `clasificar` (IMC) | límite superior de «normal» inclusivo (`<= 25`) |
| O01 | omisión | `unicos` | falta registrar cada elemento como visto |
| O02 | omisión | `leer_numeros` | falta descartar las líneas de comentario |

Las pruebas (`tests/test_banco.py`) comprueban lo siguiente:

- Hay 12 misiones con la distribución 4/4/2/2 y 12 programas distintos.
- Cada **original reprueba** las visibles y las ocultas sin errores de importación, y cada **referencia
  aprueba** las dos.
- Cada defecto es una sola zona de 1 o 2 líneas, y en las omisiones el original tiene líneas de menos.
- Las pruebas visibles y las ocultas no comparten casos.
- Lo que ve el agente no menciona la categoría ni el defecto.

### ¿Por qué no reutilizar el banco anterior?

`constructor-ia/.../laboratorio/piloto/banco_desarrollo/` (X01–X12) tiene la misma distribución, pero:

- Son 12 mutaciones de **un único programa** (`medir(db, p)`), así que no mide si la oficina repara
  *distintos* programas.
- Depende de la interfaz `db.consultar` del Explorador, que no existe en el sandbox de OpenHands.
- No trae pruebas visibles ni ocultas por archivo: el juez compara contra una referencia con parámetros de
  reserva.
- Su propio `LEEME.md` lo declara banco de **desarrollo** («se miraron, ya no miden generalización»).

Adaptarlo exigía escribir un `db` falso y pruebas para cada mutante, y aun así seguiría siendo un solo
programa. Por eso se creó uno nuevo. **Advertencia:** este banco también lo escribió Claude, con referencia y
pruebas incluidas, así que mide lo que mide un banco hecho por el mismo equipo. Para medir generalización
hace falta un banco escrito por otros.

## 2. Cómo se adapta cada misión al contrato actual de OpenHands

El contrato es el de `oh-smoke`, sin cambios en OpenHands. Mismo LLM y opciones (`native_tool_calling=False`,
`num_ctx` 16384, temperatura 0.2, semilla), mismas herramientas (terminal, editor de archivos, `finish`),
mismos `max_iteration_per_run=30`, `MAX_SEG` 900, `GRACIA` 60, MemoryMax 2G y la misma lectura de eventos. El
**único** cambio es el nombre del módulo en el pedido:

```
The repository in the current directory contains {mod}.py and its tests in test_{mod}.py.
1. Run the tests with: python3 -m unittest -v
2. Find the cause of any failure and fix it in {mod}.py.
3. Do NOT modify test_{mod}.py.
4. Run the tests again to confirm they pass, then call finish.
```

El pedido no dice la categoría ni el defecto. Toda la información viene del código, del docstring y de la
prueba que falla.

`ejecutor/correr.sh` y `ejecutor/agente.py` son copias de `oh-smoke/correr.sh` y `oh-smoke/smoke.py` donde
solo se reemplazó `duracion` por el módulo de la misión (`diff` contra los originales: solo esos nombres, más
un comentario de cabecera). Sandbox bwrap, puente con lista blanca, control previo, evaluación fuera del
sandbox del agente y criterios 1 a 6 son idénticos. Una corrección menor: `smoke.py` usa
`endswith("duracion.py")`, que también coincide con `test_duracion.py` (solo afecta a un dato de detalle, no
a los criterios). En la copia se compara el nombre exacto. `oh-smoke` no se tocó.

### La instancia: el contrato de oh-smoke por misión

Para cada misión, `lab.py` arma `instancias/<corrida>/<ID>/` con **el mismo diseño de carpetas que
`oh-smoke`**:

```
correr.sh  smoke.py (= ejecutor/agente.py)  relay.py  requisitos*.lock   (copias)
mision.json  {"id", "modulo"}                                             (sin categoría ni descripción)
tarea/  ocultas/  control/                                                (copias de banco/<ID>/)
venv -> ~/oh-smoke/venv                                                   (enlace, solo lectura)
resultados/
```

Así `oh-supervisor/supervisor.py` se usa **tal cual** (importado, sin modificar), y aplica sus garantías a
cada misión:

- carpeta nueva por intento, que parte de los originales;
- huellas de `tarea/`, `ocultas/`, `control/` y de los scripts sin cambios;
- un solo `correr.sh` a la vez;
- detención al primer éxito o a los 3 intentos.

## 3. Independencia y aislamiento

- **Entre intentos:** lo garantiza el supervisor (ver `oh-supervisor/README.md`). Cada intento copia `tarea/`
  desde la instancia, que no cambia.
- **Entre misiones:** cada misión tiene su propia instancia, y el sandbox del agente monta solo la copia de
  trabajo de esa misión. Las semillas son las mismas en todas las misiones (11, 12, 13), pero el código, el
  pedido y la conversación son distintos.
- **Pruebas ocultas y referencia:** nunca se montan en el sandbox del agente. Solo las usan el control previo
  y la evaluación de `correr.sh`, en otro sandbox sin red. `mision.json` de la instancia no lleva la categoría
  ni la descripción del defecto.
- **Banco:** su huella se toma al empezar y se compara después de cada misión. Si cambia, o si el supervisor
  detiene una misión por `integridad`, se detiene el laboratorio y las misiones que faltan quedan como
  `no_ejecutadas`.
- **Un solo laboratorio a la vez:** candado en `registros/.candado`. Las misiones van una por una.

## 4. Informe (`registros/<corrida>/informe.json`)

Se reescribe después de cada misión, así que una corrida cortada deja el informe parcial.

- `totales`: misiones, ejecutadas, resueltas, porcentaje de éxito, intentos, segundos, llamadas al LLM y
  tokens (prompt y generados).
- `resueltas`, `no_resueltas`, `no_ejecutadas`.
- `por_categoria`: misiones, resueltas y porcentaje de éxito de cálculo, sobrescritura, condición y omisión.
- `motivos_de_fallo`: cuenta por motivo (`stuck`, `texto_sin_herramienta`, `criterios (finished)`,
  `corte_forzado`…) sobre todos los intentos fallidos.
- `intentos_por_mision`.
- `misiones[]`: por misión, la categoría, si se resolvió, en qué intento, los segundos, las llamadas y tokens
  sumados, y por intento la semilla, los segundos, el motivo, los criterios fallidos, las llamadas, los tokens
  y la carpeta de resultados.
- `huellas`: banco, ejecutor, supervisor y relay, para saber exactamente con qué se midió.

Además quedan, por misión, `registros/<corrida>/<ID>/` (`resumen.json`, `intento-N.log` y
`intento-N.resultado_final.json`) y las carpetas completas de cada intento en
`instancias/<corrida>/<ID>/resultados/`.

## 5. Uso

Requiere lo que ya está en la VivoBook: `~/oh-smoke` con su `venv` y `~/oh-supervisor`. No instala nada.

```bash
git clone --depth 1 --branch claude/zealous-cray-jqmao5 https://github.com/dieguinPinga/dieguinPinga.git /tmp/oh-src
cp -r /tmp/oh-src/oh-lab ~/oh-lab && rm -rf /tmp/oh-src

cd ~/oh-lab
python3 -m unittest discover -s tests -v                      # banco + laboratorio simulado, sin GPU

# Con Qwen real (solo cuando lo aprueben):
python3 lab.py --misiones C01                                 # una misión concreta
python3 lab.py --misiones todas --semillas 11,12,13           # las 12
```

Opciones: `--oh-smoke` (`~/oh-smoke`), `--oh-supervisor` (`~/oh-supervisor`), `--registros` e `--instancias`
(dentro de `oh-lab/`). Las variables de `correr.sh` (`OLLAMA_URL`, `MAX_SEG`…) se heredan sin cambios.

**Duración con Qwen real:** el techo de cada intento es de unos 20 minutos (`MAX_SEG` 900 + `GRACIA` 60 +
control previo y evaluación). En el peor caso, 12 misiones × 3 intentos rondarían 12 h. El tiempo real
depende de los intentos: conviene empezar por `--misiones C01` para medirlo.

## 6. Verificación hecha (sin GPU)

- `tests/`: 25 pruebas (13 del banco y 12 del laboratorio con el ejecutor simulado y el supervisor real).
- **Punta a punta con la cadena real**, como usuario sin privilegios: `bubblewrap` 0.9.0 de Ubuntu 24.04,
  puente, `ejecutor/correr.sh`, `oh-supervisor/supervisor.py` y `lab.py`, con un Ollama falso que «repara»
  con la referencia y que hace fallar a propósito el primer intento de C02, O01 y S04.
  - **Resultado:** 12/12 resueltas, 15 intentos, `motivos_de_fallo = {"texto_sin_herramienta": 3}`, la
    evaluación oculta aprobada en el sandbox de evaluación de cada misión, unos 229 s en total.
  - **Qué demuestra:** que la cadena funciona. **No mide** la capacidad de Qwen.
