# Piloto de microtareas verificables — PREREGISTRO

> Este documento se commitea **antes** de escribir el código del piloto y antes de obtener cualquier resultado.
> Cambiarlo después de ver resultados invalida el piloto: cualquier modificación posterior se agrega como
> «Enmienda N», con fecha y motivo, sin borrar el texto original.

Fecha: 2026-10-21. Rama: `claude/constructor-ia-0.7-piloto-microtareas`. Base: 0.6.1-exp1 (`cd180d4`).

## Propósito

**Averiguar si la idea es buena, no demostrar que lo es.** La hipótesis a poner a prueba:

> Un modelo local pequeño puede reparar instrumentos si el sistema divide el problema en unidades suficientemente
> pequeñas, le da evidencia adecuada, verifica cada modificación y conserva el progreso.

El piloto (2 defectos) no puede confirmarla estadísticamente. Sirve para:

1. comprobar que el circuito es íntegro: sin fugas del oráculo, con autoría registrada y presupuestos respetados;
2. obtener una primera **dirección** de cada comparación;
3. registrar las señales que **contradigan** la hipótesis;
4. calibrar el experimento completo.

## Los tres desafíos, medidos por separado

| Desafío | Qué se mide | Depende del brazo |
|---|---|---|
| 1. Reconocer que algo está mal | Qué verificaciones (del sistema o del modelo) fallan con el defecto sembrado | No: etapa compartida |
| 2. Localizar | Si la instrucción sembrada está entre los 3 primeros candidatos del localizador automático | No: determinista |
| 3. Corregir sin romper | Si la versión final coincide con la referencia en todos los parámetros del juez | Sí |

## Banco

- **Referencia:** un instrumento escrito por Claude, solo dentro del laboratorio, que **nunca** se muestra a ningún
  brazo ni entra en ningún catálogo. Mide las `top` ventanas de `ancho_min` minutos con mayor suma de valores
  válidos, con cobertura: leídas, válidas e inválidas.
- **D1 «etiqueta desplazada»** (observado en el laboratorio de Diego): el inicio de la ventana se calcula una ventana
  antes. Es silencioso: no hay excepción y las sumas son correctas, pero quedan asignadas a otra etiqueta.
- **D2 «contador reiniciado»** (observado): un bloque posterior al bucle reinicia la cobertura con una condición
  que siempre se cumple.
- **Estudio de mutantes** (sin modelo): todas las mutaciones mecánicas de un solo punto sobre la referencia
  (constante ±1, comparación invertida o ampliada, operador aritmético cambiado, instrucción eliminada). Mide los
  desafíos 1 y 2 del coordinador solo. Los mutantes equivalentes (el juez no ve diferencia) se informan aparte y no
  cuentan como fallas de detección.

## El juez (aislamiento del oráculo)

El juez es la referencia ejecutada sobre la misma base, con dos juegos de parámetros:

- **visible:** `ancho_min=15, top=3`, los parámetros del instrumento;
- **reserva:** `ancho_min=30, top=5`, que ningún brazo usa nunca.

Una reparación está **corregida** si coincide con la referencia en los dos juegos: inicios exactos, sumas con error
≤ 0,001 y los tres contadores exactos.

Reglas de aislamiento:

1. El objeto juez no se pasa a ningún brazo: los brazos lo reciben como `None`.
2. **Auditoría de fugas**, sobre todos los prompts guardados al final:
   - en ningún brazo pueden aparecer las líneas de la referencia que no existen en el instrumento defectuoso
     (la corrección);
   - en los brazos B y D no puede aparecer la marca del oráculo.
3. Un episodio con una fuga queda **inválido** y se informa como tal.
4. Ninguna intervención humana durante una corrida.

## Brazos

| Brazo | Alcance del cambio | Ubicación del defecto | Evidencia |
|---|---|---|---|
| A | 0.6.1 sin cambios (`Sesion._ciclo`) | diagnóstico 0.6.1 | autoevaluación 0.6.1 |
| B | microtarea (solo las instrucciones candidatas con ±2 líneas) | localizador automático (top 3) | suite compartida |
| B\* | microtarea | oráculo: la instrucción sembrada | suite compartida |
| C | instrumento completo | oráculo: la misma de B\* | suite compartida |
| D | instrumento completo | localizador automático (top 3) | suite compartida |

- **Sin especificación correcta** en ningún brazo (decisión de esta ronda).
- **Suite compartida:** invariantes del contrato (autor `sistema`) más los verificadores que escribe el modelo
  (autor `modelo`), una sola vez por defecto y repetición, sin ver el código, como en una prueba de caja negra. Se
  usa en B, B\*, C y D. Su costo se informa aparte, como «costo de detección».
- **Umbral de aceptación** (B, B\*, C y D, idéntico en los cuatro): una versión nueva se acepta solo si el conjunto
  de verificaciones que fallan se achica y ninguna que pasaba empieza a fallar. A conserva el comportamiento de
  0.6.1: no tiene umbral.
- **Verificadores inválidos:** un verificador que lanza una excepción o no cumple el contrato se excluye
  mecánicamente, sin usar el juez. Los que «fallan con la referencia» se informan como **falsas alarmas**, una
  medición hecha por el juez después de terminar; no se filtran durante la corrida.

## Presupuestos comparables (fijados de antemano)

- **Por episodio** (un brazo × un defecto × una repetición): máximo **6 llamadas** al modelo y **1200 s** de modelo.
  Lo que se agote primero termina el episodio.
- **Detección compartida:** máximo 2 llamadas por defecto y repetición.
- **Corte por mismo error:** 3 fallas seguidas con la misma firma.
- **Iguales para todos:** modelo, `num_ctx` (6144), temperatura (0,2), `num_predict` de código (2048) y de
  decisiones (768), base sintética (`pequena`, semilla 7) y sandbox.
- **Repeticiones del piloto:** 2 por defecto, es decir 2 defectos × 2 repeticiones × 5 brazos = 20 episodios.

## Cómo se leen los resultados

Para cada comparación y cada defecto, el piloto informa **una de cuatro categorías**, calculadas mecánicamente:

- **a favor:** el brazo de la hipótesis corrige en más repeticiones que el otro;
- **en contra:** corrige en menos;
- **empate:** corrigen lo mismo; si ambos corrigen, se informa el costo;
- **no informativo:** el defecto no fue detectado por la suite, así que ningún brazo con suite pudo empezar.

| Comparación | Pregunta | La hipótesis predice |
|---|---|---|
| B\* contra C | ¿Importa el tamaño, con la misma información? | B\* ≥ C con menor costo |
| B contra B\* | ¿Cuánto cuesta localizar solo? | B ≈ B\* |
| D contra C | ¿Cuánto cuesta localizar solo, con cambios completos? | D ≈ C |
| B contra D | ¿Importa el tamaño, con localización automática? | B ≥ D |
| B contra A | ¿La propuesta mejora lo actual? | B > A |

## Resultados que contradicen la hipótesis (registro obligatorio)

El informe empieza con la sección **«Contra la hipótesis»**, antes que cualquier resultado favorable. Ahí se
enumera cada comparación «en contra», cada defecto no detectado por la suite y cada defecto que el localizador no
encontró (top 3), con la misma prominencia que los resultados favorables. Si la sección queda vacía, el informe lo
dice explícitamente y aclara que con 2 defectos eso **no** confirma la hipótesis.

Señales de refutación declaradas de antemano:

- **R1:** B\* ≤ C en ambos defectos → «en el piloto, el tamaño del cambio no ayudó».
- **R2:** B < B\* en ambos defectos → «la localización automática es el cuello de botella; sin oráculo no hay
  autonomía».
- **R3:** un defecto silencioso (D1) no detectado por ninguna verificación del sistema ni del modelo → «el
  desafío 1 no está resuelto para fallas silenciosas».
- **R4:** el estudio de mutantes da una detección del sistema menor que 50 % de los mutantes no equivalentes →
  «las invariantes genéricas no alcanzan; la detección depende de verificaciones del modelo».
- **R5:** top 3 del localizador < 50 % de los mutantes detectados → «el localizador estático no alcanza».

## Criterio para pasar al experimento completo

No depende de que la hipótesis salga favorecida. Se pasa al experimento completo (8 defectos × 3 repeticiones ×
5 brazos) si:

- 0 fugas;
- 0 episodios con errores no atribuidos (cada falla queda marcada como del modelo, del coordinador o del entorno);
- presupuestos respetados en todos los episodios;
- el informe se genera completo, incluso con resultados en contra.

## Qué NO cambia

- El sandbox, el intermediario MariaDB de solo lectura y los servicios de la VivoBook.
- Todo el código nuevo vive en `constructor_ia/laboratorio/piloto/`.
- El piloto usa el MariaDB privado y desechable del laboratorio. Las transformaciones de datos, si las hubiera,
  se hacen solo sobre la base sintética de ese servidor privado.

---

## Enmienda 1 (2026-10-21): resultado del estudio de mutantes y correcciones de implementación

Escrita **después** de correr el estudio de mutantes (sin modelo, en el entorno de desarrollo de Claude: sandbox real
y MariaDB privado, base `pequena` semilla 7). El texto preregistrado de arriba no cambia.

### Resultado

- **Mutantes:** 51 en total; 12 equivalentes (el juez no ve diferencia) y 39 no equivalentes.
- **Desafío 1, detección del sistema:** S1 a S3 detectan **29 de 39 (74 %)**. **R4 no se activa.**
  - Los cambios de operador aritmético, la clase del defecto D1, se detectan **0 de 3** veces.
  - El «not» eliminado se detecta 0 de 1 veces.
  - Las invariantes genéricas ven bien las fallas de cobertura y de ejecución, y no ven las fallas silenciosas de
    cálculo.
- **Desafío 2, localización con la detección del sistema:** top 1 en 14 %, **top 3 en 28 %**. **R5 se activa:**
  «el localizador estático no alcanza».
- **Localización con detección oráculo** (el juez dice qué campos difieren; es una cota del localizador aislado):
  top 3 en 36 %.
- **Por qué falla la localización,** en 21 casos:
  - **15 son omisiones** (instrucción eliminada). Un localizador que señala instrucciones existentes no puede
    señalar una ausencia.
  - **6 quedaron en el puesto 4**: las constantes de inicialización, castigadas por la regla «la última escritura
    primero».
  - **0 quedaron fuera de la rebanada.** La rebanada es completa; fallan el orden y el tratamiento de las omisiones.
  - Sin contar las omisiones, el top 3 acierta en 8 de 14 casos (57 %).

### Correcciones de implementación hechas antes de cualquier corrida con modelo

1. **Localizador.** La regla «las instrucciones que solo arman la salida van al final» marcaba cualquier diccionario
   con la clave buscada. Eso incluía `cobertura = {"leidas": 0, …}`, que es justamente el defecto D2. La intención
   documentada era castigar solo la construcción de la salida devuelta (`return`), y así quedó.
   - La encontró la prueba del circuito (`tests/test_piloto.py`).
   - **No cambió ningún número del estudio de mutantes**: se volvió a correr y dio exactamente lo mismo.
2. **Ensamblador.** Cuando se reemplaza el encabezado de una instrucción compuesta sin «:», el error que recibe el
   modelo ahora explica que para quitar el bloque la acción es «eliminar». Antes decía «unexpected indent». La
   atribución al modelo no cambia.

### Qué NO se cambia por este resultado

El localizador de los brazos B y D queda **como está**. Cambiarlo después de ver los mutantes sería ajustar al banco.

Las dos mejoras que el resultado sugiere se evaluarán como una **variante separada**, sobre una **referencia nueva**
que todavía no existe, para no sobreajustar a estos 51 mutantes:

- microtareas de omisión: «¿falta algo en este bloque?»;
- otro orden de candidatas.

---

## Anexo operativo: cómo se corre (no modifica el preregistro)

> **Reemplazado por el Anexo 2.** Los comandos de abajo usan la instalación (`~/constructor-ia/bin`), y eso exigiría
> instalar la rama. El Anexo 2 corre el piloto desde un checkout aparte, sin instalar nada.

Código: `constructor_ia/laboratorio/piloto/`.

| Archivo | Contenido |
|---|---|
| `banco.py` | referencia, D1, D2, mutantes y juez |
| `parches.py` | unidades editables y ensamblado verificado |
| `localizador.py` | localizador estático |
| `verificaciones.py` | suite con autoría y umbral de aceptación |
| `brazos.py` | los cinco brazos y el presupuesto |
| `piloto.py` | orquestador, auditoría de fugas, lectura preregistrada y reanudación |
| `mutantes.py` | estudio de mutantes |
| `informe.py` | informe; empieza con «Contra la hipótesis» |

En la VivoBook, cuando ustedes lo decidan:

```bash
~/constructor-ia/bin/constructor-ia selftest                 # debe decir AISLAMIENTO OPERATIVO
systemctl --user stop constructor-ia
# 1) sin modelo, unos minutos: desafíos 1 y 2 del coordinador solo
~/constructor-ia/bin/constructor-ia laboratorio-mutantes --yes
# 2) el piloto con Qwen3 4B: 20 episodios
~/constructor-ia/bin/constructor-ia laboratorio-piloto --yes --modelo qwen3:4b-instruct
#    si se interrumpe, se retoma sin repetir lo hecho:
~/constructor-ia/bin/constructor-ia laboratorio-piloto --yes --reanudar ~/constructor-ia/data/laboratorio/piloto-<fecha>
systemctl --user start constructor-ia
```

**Duración estimada,** con el modelo de tiempos que surge de sus mediciones (≈ 15 s fijos + 0,24 s por token
generado), como **cota superior:**

- hasta 128 llamadas;
- en el peor caso, unas 4 a 5 horas: los brazos A, C y D agotan sus 6 reescrituras completas de ≈ 190 s cada una;
- con microtareas que resuelven en 1 o 2 llamadas, bastante menos.

No se midió en la VivoBook.

**Archivos que deja** en `data/laboratorio/piloto-<fecha>/`:

| Archivo | Contenido |
|---|---|
| `episodios.jsonl` | un episodio por línea, con el juez, los pasos, la atribución y el costo |
| `prompts.jsonl` | cada prompt con sus partes etiquetadas por origen y la respuesta cruda |
| `deteccion.json` | los verificadores escritos por el modelo, con su validez y lo que detectan |
| `piloto.json` y `resumen.md` | la lectura preregistrada |
| `*.sqlite` | un almacén por episodio, más la detección y el juez, por separado |

Se usa el MariaDB privado y desechable del laboratorio: no toca el servicio MariaDB, sus bases ni su configuración.

---

## Enmienda 2 (2026-10-21, antes de cualquier corrida con modelo): tiempo máximo por llamada y ensayo técnico

1. **Tiempo máximo por llamada: 900 s para todos los brazos** (`piloto.TIMEOUT_S`). Antes valía 300 s, el valor por
   defecto del explorador.
   - Con las mediciones de Diego (≈ 15 s + 0,24 s por token), una reescritura completa de más de unos 1.200 tokens
     superaría los 300 s.
   - Eso habría castigado a los brazos de reescritura completa (A, C y D) y no a las microtareas: un sesgo **a favor**
     de la hipótesis.
   - Motivo: equidad entre brazos, no ningún resultado.
2. **Ensayo técnico** (`--ensayo`), pedido por Diego y ChatGPT:
   - alcance: D2, 1 repetición, los 5 brazos, 2 llamadas por episodio y 1 de detección, más el estudio de mutantes;
   - comprueba que Qwen real, el sandbox y el MariaDB privado funcionan juntos;
   - **queda excluido del análisis preregistrado:**
     - su carpeta lleva la marca `ENSAYO_TECNICO`;
     - el informe empieza con «ENSAYO TÉCNICO — EXCLUIDO DEL ANÁLISIS PREREGISTRADO»;
     - el código impide reanudar una carpeta de ensayo como corrida real, y al revés.

## Anexo 2: correr el piloto SIN instalar la rama

> Los pasos del ensayo técnico quedaron detallados y automatizados en el **Anexo 3**, con la restauración
> garantizada del servicio. Este anexo explica el porqué.

`update.sh` **reemplaza la instalación**: la versión actual pasa a `app.prev` y la nueva queda en servicio. Por eso
el piloto no se instala. Corre desde un checkout aparte con `scripts/piloto.sh`, que:

- usa su propia carpeta, `~/constructor-ia-piloto`, con su configuración (`config.toml`, permisos 0600) y sus datos
  (permisos 0700);
- usa el intérprete del entorno virtual existente solo para ejecutar, en modo aislado (`-I`), sin instalarle nada;
- comprueba que el código cargado sea el del checkout y no el instalado;
- se niega a correr desde la instalación o a poner su carpeta dentro de ella.

**No toca:** `~/constructor-ia/app`, la base `~/constructor-ia/data/constructor.db`, `~/constructor-ia/config`, el
servicio systemd, el MariaDB instalado, Ollama 11434, Node-RED, Vigilancia, Tailscale ni OpenClaw.

### Pasos en la VivoBook (los hacen ustedes)

```bash
# 0) Python del entorno virtual (hace falta >= 3.11)
~/constructor-ia/venv/bin/python3 --version

# 1) Checkout aparte con git worktree: el checkout de siempre no cambia de rama
cd ~/src/dieguinPinga
git fetch origin claude/constructor-ia-0.7-piloto-microtareas
git worktree add ~/src/cia-piloto origin/claude/constructor-ia-0.7-piloto-microtareas
git -C ~/src/cia-piloto log --oneline -5          # debe mostrar el preregistro (9862c7b) antes del código

# 2) Huellas de la instalación ANTES (para demostrar después que no cambió)
find ~/constructor-ia/app -type f | sort | xargs sha256sum | sha256sum > ~/piloto-huella-app-antes.txt
sha256sum ~/constructor-ia/config/config.toml > ~/piloto-huella-config-antes.txt

# 3) Aislamiento, con la carpeta propia del piloto
bash ~/src/cia-piloto/constructor-ia/scripts/piloto.sh selftest      # debe decir AISLAMIENTO OPERATIVO

# 4) Detener SOLO el servicio constructor-ia mientras dura el ensayo (el piloto se niega a competir por Ollama CPU)
systemctl --user stop constructor-ia
sha256sum ~/constructor-ia/data/constructor.db > ~/piloto-huella-db-antes.txt

# 5) ENSAYO TÉCNICO (excluido del análisis): ≤ 11 llamadas al modelo, más los mutantes (sin modelo, ~1 min)
bash ~/src/cia-piloto/constructor-ia/scripts/piloto.sh laboratorio-piloto --yes --ensayo

# 6) Comprobar que la instalación no cambió y volver a arrancar el servicio
sha256sum -c ~/piloto-huella-db-antes.txt
find ~/constructor-ia/app -type f | sort | xargs sha256sum | sha256sum | diff - ~/piloto-huella-app-antes.txt \
  && echo "app intacta"
sha256sum -c ~/piloto-huella-config-antes.txt
systemctl --user start constructor-ia
```

**Qué mirar en el ensayo** (`~/constructor-ia-piloto/data/laboratorio/ensayo-<fecha>/resumen.md`):

- **Integridad:** 0 fugas y 0 presupuesto excedido.
- **Que Qwen real produzca respuestas utilizables.** En `prompts.jsonl` están los textos crudos. Una respuesta con
  formato inválido no es una falla técnica: es un dato del modelo.
- **Que el MariaDB privado se haya borrado:** no debe quedar la carpeta `mariadb/`.
- **Tiempos reales por llamada:** sirven para estimar la duración del piloto completo.

**Después, la corrida preregistrada** (en otro momento, con el servicio detenido y las mismas huellas antes y
después):

```bash
bash ~/src/cia-piloto/constructor-ia/scripts/piloto.sh laboratorio-piloto --yes
# si se interrumpe:
bash ~/src/cia-piloto/constructor-ia/scripts/piloto.sh laboratorio-piloto --yes --reanudar \
  ~/constructor-ia-piloto/data/laboratorio/piloto-<fecha>
```

### Rollback (deshacer todo)

Como no se instaló nada, deshacer es borrar:

```bash
git -C ~/src/dieguinPinga worktree remove ~/src/cia-piloto
rm -rf ~/constructor-ia-piloto           # antes, guardar los resultados que quieran conservar
```

Si el servicio quedó detenido: `systemctl --user start constructor-ia`.

### Recursos durante la corrida

- El MariaDB privado usa unos 64 MB de buffer y su carpeta de datos es temporal.
- Qwen3 4B usa su memoria habitual en Ollama CPU.
- La VivoBook tiene 5,7 GiB y otros servicios activos. Elegir el momento es decisión de ustedes.

---

## Anexo 3: ensayo técnico en la VivoBook, por etapas

Se usa `scripts/piloto_etapas.sh`, que vive en el checkout aparte. Cada etapa se corre a mano por SSH y se revisa
antes de seguir. **Solo la etapa 4 detiene algo**, y lo único que detiene es `constructor-ia`, durante el ensayo.

La etapa 4 restaura el servicio con tres capas:

1. **Trampa del script:** al terminar, fallar, recibir Ctrl-C o perder la sesión SSH (SIGHUP), interrumpe el ensayo
   con SIGINT y vuelve a arrancar `constructor-ia`. El SIGINT hace que el ensayo limpie su MariaDB privado.
2. **Temporizador de respaldo** de systemd: `cia-piloto-restaurar.timer`, transitorio, a límite + 5 minutos. Si el
   script muriera sin poder reaccionar (por ejemplo con `kill -9`), arranca el servicio igual. Si el temporizador no
   se puede armar, **no se detiene nada**.
3. **Límite de tiempo** del ensayo: 90 minutos por defecto, máximo 180.

Las tres capas tienen pruebas automáticas (`tests/test_piloto.py`, `StagesScriptTests`). Cubren éxito, falla,
Ctrl-C, caída de SSH, límite de tiempo, servicio que ya estaba detenido y temporizador imposible de armar.

### Etapa 1 — estado actual (solo lectura; todavía no existe el checkout)

```bash
systemctl --user is-active constructor-ia; systemctl --user is-enabled constructor-ia
grep -o 'VERSION = "[^"]*"' ~/constructor-ia/app/constructor_ia/__init__.py
~/constructor-ia/venv/bin/python3 --version                        # hace falta >= 3.11
systemctl --user list-units --type=service --state=running --no-pager
systemctl list-units --type=service --state=running --no-pager      # solo se listan: no se tocan
curl -s http://127.0.0.1:11435/api/tags | python3 -m json.tool | grep '"name"'
free -h; df -h ~; nproc
for b in mariadbd mariadb-install-db mariadb bwrap systemd-run timeout flock git tmux; do
  printf '%-20s %s\n' "$b" "$(command -v $b || echo FALTA)"; done
ls -d ~/constructor-ia-piloto ~/src/cia-piloto 2>/dev/null || echo "sin restos de ensayos anteriores"
git -C ~/src/dieguinPinga status --short | head
```

Para seguir, todo esto tiene que cumplirse:

- `constructor-ia` en el estado que esperan;
- Python 3.11 o más;
- `qwen3:4b-instruct` en la lista de Ollama CPU;
- ningún binario en FALTA, salvo `tmux`, que es opcional.

### Etapa 2 — checkout aparte de la rama y su commit

```bash
cd ~/src/dieguinPinga
git fetch origin claude/constructor-ia-0.7-piloto-microtareas
git worktree add --detach ~/src/cia-piloto origin/claude/constructor-ia-0.7-piloto-microtareas
git -C ~/src/cia-piloto rev-parse HEAD         # debe coincidir con el commit que les indicó Claude
git -C ~/src/cia-piloto log --oneline -6       # el preregistro 9862c7b aparece ANTES del código del piloto
git -C ~/src/dieguinPinga status --short       # el checkout de siempre no cambió
```

### Etapa 3 — el entorno del piloto es propio

```bash
E=~/src/cia-piloto/constructor-ia/scripts/piloto_etapas.sh
bash $E huellas antes        # solo lectura de la instalación; guarda las huellas en ~/constructor-ia-piloto/huellas
bash $E entorno
```

Qué esperar de `entorno`:

- `[piloto] código: ~/src/cia-piloto/constructor-ia`, y no `~/constructor-ia/app`;
- `"data_dir": ~/constructor-ia-piloto/data` y `"ollama_url": http://127.0.0.1:11435`;
- los binarios encontrados y `qwen3:4b-instruct: instalado`;
- `RESULTADO: AISLAMIENTO OPERATIVO`. Si dice `EJECUCIÓN BLOQUEADA`, no se sigue.

El MariaDB privado recién se crea en la etapa 4, dentro de la carpeta del ensayo, con `--no-defaults`, socket propio y
`--skip-networking`.

### Etapa 4 — el ensayo técnico con Qwen3 4B real

Si existe `tmux`, conviene abrir una sesión antes (`tmux new -s piloto`): así una caída de SSH no lo interrumpe. Sin
`tmux`, una caída lo interrumpe, el servicio vuelve solo y el ensayo se puede reanudar.

```bash
bash $E ensayo 90                      # protocolo v2 (Enmienda 3); «bash $E ensayo 90 --protocolo v1» reproduce el v1
```

- **Lo que hace:**
  1. arma el temporizador de respaldo;
  2. detiene solo `constructor-ia`;
  3. toma la huella de la base productiva;
  4. corre `laboratorio-piloto --ensayo`: primero el estudio de mutantes (~1 min, sin modelo) y después D2 con los 5
     brazos (≤ 11 llamadas);
  5. compara la huella de la base;
  6. vuelve a arrancar el servicio y desarma el temporizador.
- **Duración esperada:** de 20 a 45 minutos. No se midió; el tope es de 90.
- **Al final debe decir:** `base productiva: sin cambios` y `constructor-ia.service activo de nuevo`.

### Etapa 5 — resultados, prompts y autoría (solo lectura)

```bash
bash $E revisar                    # LARGO=1500 bash $E revisar  → respuestas crudas más largas
```

Qué mirar:

- tipo `ENSAYO TÉCNICO`;
- MariaDB privado y clave desechable borrados;
- 0 fugas y ningún presupuesto excedido;
- en cada llamada: tiempo real, tokens y `completa` o `truncada`;
- en cada prompt: las partes por origen. Un `oraculo` solo puede aparecer en B\* y C;
- la respuesta cruda de Qwen;
- en cada paso: las candidatas del localizador y la autoría de cada falla (modelo o coordinador);
- «Procesos del MariaDB privado que hayan quedado: ninguno».

### Etapa 6 — la instalación quedó intacta

```bash
bash $E huellas despues
bash $E comparar                   # app, config, servicios y puertos: «igual»
systemctl --user is-active constructor-ia
systemctl --user list-timers --all --no-pager | grep cia-piloto || echo "sin temporizador de respaldo pendiente"
```

Si `comparar` muestra diferencias en servicios ajenos, puede ser un cambio propio de ese servicio durante el ensayo:
se revisa, no se corrige a ciegas.

**Deshacer todo** (ver el Anexo 2): `git -C ~/src/dieguinPinga worktree remove ~/src/cia-piloto` y borrar
`~/constructor-ia-piloto` después de guardar los resultados que quieran conservar.

---

## Enmienda 3 (2026-10-22): protocolo v2, después del ensayo técnico v1 y antes de la corrida preregistrada

El ensayo técnico v1 (Qwen3 4B real en la VivoBook, excluido del análisis) cumplió su propósito: mostró fallas del
coordinador, del contrato y del entorno antes de la corrida que cuenta. **Sus resultados se conservan tal como
corrieron** y quedan identificados como protocolo v1. El código de v1 (`piloto/brazos.py`) no cambió y se puede
reproducir con `--protocolo v1`. La corrida preregistrada todavía no se hizo y usará el **protocolo v2**
(`piloto/v2.py`).

### Lo que mostró el ensayo, clasificado

| Evidencia del ensayo v1 | Clase | Verificación hecha |
|---|---|---|
| B\*: Qwen respondió «CANDIDATO: 21» dos veces y v1 lo rechazó por formato | **contrato** (índice frente a línea) | La respuesta era interpretable: era la línea de la única candidata. |
| El mismo parche de B\*, interpretado | **modelo** | Contrafáctico sin inferencia, con el juez real: en las dos lecturas posibles (la compuesta completa o solo el encabezado) la cobertura sigue en 0. Qwen agregó `or …` a una condición **siempre verdadera** en vez de quitarla. |
| B repitió el parche rechazado | **retroalimentación** (coordinador) | Reproducido: los tres prompts después de los rechazos eran idénticos byte a byte. El motivo del rechazo nunca llegaba al modelo. El brazo A (0.6.1) sí recibe su historial: era una desigualdad entre brazos. |
| D: corregido según el juez y, aun así, `V1` seguía fallando | **verificación** (falsa alarma probable) | La medición de falsas alarmas del preregistro no estaba implementada: ahora sí. |
| D: conexión cortada por el OOM de Ollama, registrada como falla del modelo | **entorno** (mal atribuido) | Confirmado: el cliente convertía el corte en «sin respuesta» y el ensamblado se lo atribuía al modelo. |
| B a D y la detección recibían `{resultado_kb}` sin completar en el prompt de sistema | coordinador (menor) | Corregido en v2 para todos. |

### Qué cambia en v2 (igual para B, B\*, C y D; A conserva el ciclo de 0.6.1)

1. **Contrato:**
   - las candidatas se nombran con letras (A, B, C);
   - un número que coincide con la línea de **una** candidata se acepta y queda registrado como interpretación;
   - si la respuesta es ilegible, se pide una reparación de formato con autoría «contrato»; solo la segunda falla es
     del modelo;
   - las instrucciones compuestas se reemplazan **completas**, encabezado y cuerpo, que es lo que Qwen hizo de forma
     natural.
2. **Retroalimentación:**
   - cada prompt incluye los intentos anteriores: el último con su diff y las verificaciones posteriores, y los
     anteriores en una línea, con su resultado y su motivo;
   - un parche idéntico a uno ya rechazado no se ejecuta («repetido») y se le avisa al modelo;
   - todo sale de la suite y de las respuestas del propio modelo, **nunca del juez**: la auditoría de fugas sigue
     igual.
3. **Disputa de verificadores:**
   - si solo fallan verificadores del modelo y las verificaciones del sistema pasan, el modelo decide **una vez por
     verificador** si el error está en el instrumento o en el verificador;
   - un verificador impugnado deja de bloquear el episodio;
   - el juez evalúa esa decisión **después**: la falsa alarma se mide corriendo el verificador sobre la referencia.
4. **Entorno no es modelo:**
   - un corte de conexión, un tiempo agotado o un contexto rechazado por Ollama interrumpen el episodio sin contarlo
     como llamada ni como falla del modelo;
   - el episodio se reintenta una vez;
   - dos interrupciones seguidas, o memoria insuficiente, **detienen la corrida**, que después se reanuda;
   - todo queda en `interrupciones.jsonl`.
5. **Memoria** (sin tocar la configuración de Ollama, que es un servicio instalado):
   - `keep_alive: "0s"` **por pedido**: el modelo se descarga después de cada llamada y no convive con el sandbox.
     El cliente no lo enviaba, así que regía el valor del servidor, 5 minutos por defecto;
   - **guardia**: antes de cada llamada exige `MemAvailable` ≥ 3.900 MiB si el modelo no está cargado; espera hasta
     3 minutos y, si no alcanza, aborta limpio;
   - antes de cada ejecución en el sandbox espera a que el modelo se haya descargado.
6. **Contexto: 4.096 tokens** (antes 6.144):
   - antes de cada llamada se estima el prompt (factor 0,8 sobre el estimador del proyecto; el ensayo midió 0,73)
     con una reserva de salida de 1.700 tokens para código (máximo observado 1.377, más un 23 %) y 768 para
     respuestas cortas;
   - si no entra, se recorta el historial; si aun así no entra, **no se llama** y el episodio es inválido por culpa
     del coordinador.

### Margen de contexto, medido con prompts v2 reales (peor caso: rechazos sucesivos)

- El caso más grande es la reescritura completa de D1 en su tercer intento: ≈ 2.385 tokens estimados de entrada
  más 1.700 de reserva, unos 4.085 de 4.096. Con el factor real del ensayo (0,73) quedan unos 220 tokens libres.
- Con dos intentos detallados, los brazos de reescritura completa perdían historial antes que las microtareas.
  Eso rompía la condición «misma información» de B\* frente a C. Por eso la política quedó en **un intento
  detallado para todos**.
- Aun así, en ese peor caso, el tercer intento de D en D1 se queda solo con los resúmenes. El informe marca cada
  recorte («Historial recortado por contexto»), para que no pase desapercibido en las comparaciones.

### Memoria: lo que sabemos y lo que no

**Medido en desarrollo:**
- el MariaDB privado ocupa ≈ 100 MiB (bajar su buffer de 64 a 16 MiB casi no cambia nada);
- el proceso del piloto, ≈ 23 MiB;
- cada ejecución en el sandbox, hasta 256 MiB (límite del cgroup).

**Informado por Diego:**
- `llama-server` tenía ≈ 4,1 GiB residentes con 6.144 tokens de contexto y 864 MiB de caché KV (≈ 144 KiB por
  token);
- con 4.096 tokens la caché pasa a ≈ 576 MiB (288 MiB menos), así que el modelo quedaría en ≈ 3,8 GiB;
- había ≈ 4 GiB disponibles sin el modelo.

**El margen es chico: unos 200 MiB.** La guardia evita **lanzar** una llamada sin memoria y `keep_alive` evita la
convivencia con el sandbox, pero **ninguna de las dos puede impedir un OOM si otro proceso crece durante una
generación**.

Para afinar el umbral de 3.900 MiB serviría la línea completa del OOM del kernel (`anon-rss` y `file-rss`). Si los
pesos están mapeados desde el archivo, una parte de esos 4,1 GiB es caché recuperable.

Otras palancas, que son decisión de ustedes y no cambié: otra cuantización, un modelo más chico o swap.

### Pruebas (casos de regresión del ensayo)

`tests/test_piloto_v2.py`, con sandbox y MariaDB reales y modelos simulados:

- la respuesta exacta de Qwen en B\*: v2 la interpreta, la ensambla, el umbral la rechaza (no corrige) y su
  repetición no se ejecuta; con `--protocolo v1` se reproduce el rechazo original por formato;
- el prompt cambia después de un rechazo e incluye el intento, el motivo y las verificaciones posteriores;
- la falsa alarma de D: después de una corrección aceptada solo falla V1; el modelo lo impugna y el episodio
  termina. El juez confirma la falsa alarma y la decisión correcta; si el modelo defiende al verificador
  equivocado, queda «en contra»;
- los cortes de Ollama: se reintentan sin culpar al modelo, dos seguidos detienen la corrida y se reanuda;
- la guardia de memoria: detiene la corrida sin llamar al modelo;
- el contexto insuficiente: episodio inválido por culpa del coordinador, sin llamadas;
- `keep_alive` solo se envía cuando se pide;
- carpetas de v1 y v2 que no se mezclan al reanudar.

## Experimento de elección: Etapas 0 y 1 (preregistro, 2026-10-22, antes de cualquier llamada al modelo)

Este experimento es aparte de la corrida preregistrada de los brazos y no la modifica. **No** se implementan
todavía los cambios de devolución ni de alcance: primero se aísla el efecto de mostrar la evidencia.

**Pregunta.** Con las mismas candidatas, ¿mostrarle a Qwen la evidencia de ejecución de cada una lo ayuda a elegir
la instrucción que causa las fallas?

### La evidencia de ejecución (genérica, no específica de D1 ni de D2)

- **Qué candidatas se observan.** El anfitrión deriva del árbol sintáctico qué nombres define y usa cada candidata.
  Solo cuentan los nombres propios del programa: parámetros y asignaciones, sin funciones ni módulos.
- **Cómo se observa.** El sandbox ejecuta el instrumento **una vez** con los parámetros visibles y observa esas
  líneas con `sys.settrace`:
  - solo en el archivo del instrumento y solo si se pidió la traza;
  - no observa comprensiones ni lambdas, y una línea que se repite seguida cuenta una vez;
  - los valores salen con una representación acotada, que nunca recorre una colección entera.
- **Qué hechos muestra.** Los mismos campos para todas las candidatas, con la misma plantilla:
  - cuántas veces se ejecutó;
  - en un `if`/`while`, en cuántas evaluaciones la condición fue verdadera;
  - los valores antes y después de su última ejecución;
  - si, al terminar `medir`, cada nombre que define conserva el valor que dejó esa línea o si otra instrucción lo
    cambió después;
  - si asigna una constante que reemplazó un valor distinto;
  - hasta dos muestras de «usa → deja».
- **Qué no hace.** Nunca dice cuál es la causa. La traza se calcula **solo** con el instrumento defectuoso, los
  datos y las candidatas. No usa la referencia, el juez ni la instrucción sembrada.
- **Cuánto cuesta.** Se mide en desarrollo: ≈ 0,3 s más por ejecución, y un bucle de 200.000 vueltas sigue acotado.

### Etapa 0 (sin modelo; no detiene nada)

**Construcción de los casos.** Se usan D1, D2 y los mutantes de un punto (Anexo 1):

1. El almacén del juez se usa solo para descartar mutantes equivalentes.
2. Las fallas salen de una **verificación fija**, `VERIFICADOR_FIJO`, igual en las dos condiciones. Vuelve a medir
   el total y las sumas por ventana.
3. Las tres candidatas salen del localizador estático.
4. La traza se calcula con el instrumento defectuoso.

**Uso de la identidad de la instrucción sembrada.** Solo para dos cosas:
- (a) decidir qué casos entran: la sembrada tiene que estar entre las tres candidatas;
- (b) puntuar.

**Métricas.**
- Casos detectados y casos con la sembrada entre las candidatas.
- En cuántos la traza **distingue** a la sembrada: algún rasgo estructural la separa de las otras dos.
- En cuántos hay una señal anómala exclusiva de la sembrada: se pierde, o una constante reemplazó un valor.
- Todo por clase: **cálculo** (como D1) y **sobrescritura** (como D2).

**Selección (fijada antes de mirar respuestas).** Como máximo 8 casos:
1. D1 y D2, si su sembrada está entre las candidatas.
2. Después, mutantes por id, prefiriendo líneas sembradas **distintas**, para no repetir gemelos.
3. Después, se completa con el resto.

**Resultado real en el entorno de desarrollo:**
- 51 mutantes más D1 y D2;
- 41 no equivalentes y 38 detectados;
- 15 con la sembrada entre las 3 candidatas: 14 de cálculo y 1 de sobrescritura;
- la traza distingue a la sembrada en 2 de 15 (D2 y M027). La señal exclusiva aparece solo en D2;
- selección: D1, D2, M022, M027, M029, M030, M039, M050. La sembrada queda en la posición original 1, 2 o 3 en 3,
  3 y 2 casos.

El banco y la base sintética son deterministas (semilla 7), así que en la VivoBook la Etapa 0 debería dar lo mismo.
Si da otra cosa, **no se pasa a la Etapa 1** sin revisarlo antes.

**Predicción, escrita antes de correr el modelo:**
- efecto claro en la sobrescritura;
- débil en los errores de cálculo, donde la traza solo aporta muestras de valores;
- con un solo caso de sobrescritura, el veredicto global **«inconcluso» es plausible y aceptable**.

### Etapa 1: diseño

- **Una única diferencia.** El control y el tratamiento reciben exactamente las mismas partes:
  - pregunta y parámetros;
  - salida actual y verificaciones que fallan;
  - candidatas con código limpio (sin marcas) y contexto numerado.
  
  El tratamiento agrega **solo** la parte «traza». El código lo comprueba en cada ensayo: las partes del
  tratamiento sin la traza deben ser idénticas a las del control, o la corrida se aborta.
- **Llamada.** Una llamada corta, sin parches ni historial: `CANDIDATO: <letra>` / `MOTIVO: <una frase>`.
- **Contrabalanceo.**
  - 3 rotaciones del orden de las candidatas, de modo que cada candidata pasa por cada posición;
  - 2 repeticiones;
  - en total, 8 casos × 3 × 2 = 48 pares, es decir 96 llamadas.
- **Aleatoriedad.**
  - temperatura 0,2;
  - **semilla de Ollama por par**: `sha256(caso|rotación|repetición)`, la misma en control y tratamiento;
  - el orden de ejecución de los 96 ensayos se baraja con `SEMILLA_ORDEN = 20261022`, para que el estado de la
    máquina no se confunda con la condición.
- **Modelo y recursos.**
  - `qwen3:4b-instruct`, contexto de 4096 y `keep_alive: 0s`;
  - guardia de memoria de 3900 MiB;
  - tiempo máximo de 900 s por llamada.
  
  Durante la Etapa 1 no corre código: ni sandbox ni MariaDB.
- **Entorno no es modelo.** Las interrupciones se manejan como en la Enmienda 3:
  - un corte, un tiempo agotado o falta de memoria o de contexto quedan en `interrupciones.jsonl` y no se puntúan;
  - dos cortes seguidos, o falta de memoria o de contexto, detienen la corrida, que después se reanuda.

### Cómo se puntúa (fijado antes de correr)

- **Correcto:** la instrucción elegida, por letra o por el número de línea de una única candidata, se superpone con
  las líneas sembradas.
- Una respuesta **ilegible cuenta como incorrecta**. También se informa aparte, por condición.
- **Unidad de análisis:** el par (caso, rotación, repetición). Solo cuentan los pares completos.
- **Contraste:** acierto del tratamiento menos acierto del control, con la **prueba exacta de McNemar** bilateral
  sobre los pares discordantes. b = solo acierta el tratamiento; c = solo acierta el control.
- **Veredicto preregistrado:**
  - **«ayuda»** si la diferencia es ≥ 20 puntos y p < 0,05;
  - **«perjudica»** en el caso simétrico;
  - **«inconcluso»** en cualquier otro caso.
  
  Con 48 pares, por ejemplo, b = 10 y c = 0 da 21 puntos y p ≈ 0,002, y alcanza. b = 12 y c = 4 da 17 puntos y
  no alcanza.
- **Se informa siempre, aunque no decide el veredicto:**
  - acierto por clase (cálculo y sobrescritura);
  - posición elegida por condición, para el sesgo de posición;
  - acierto por rotación;
  - largo medio de la evidencia de la sembrada frente a la de las otras: si la sembrada recibe sistemáticamente
    más texto, el largo podría explicar el efecto;
  - auditoría de fugas: ninguna línea de la referencia puede aparecer en una parte del coordinador ni en la
    traza;
  - segundos por llamada.
- Las muestras de entrada y salida (prompt completo por partes y respuesta cruda) quedan en `ensayos.jsonl`.

### Cómo se corre en la VivoBook (por etapas; nada automático)

Desde el checkout aparte (Anexo 2), con `E=scripts/piloto_etapas.sh`:

```bash
# Etapa 0: sin modelo, no detiene el servicio. Escribe casos.json (la carpeta se imprime al final).
bash $E eleccion0
# Revisar casos.json y la selección ANTES de seguir.

# Prueba de rendimiento: 4 ensayos (4 llamadas), protegida como el ensayo técnico
# (detiene constructor-ia y SIEMPRE lo restaura; límite de 30 minutos).
bash $E eleccion 30 --casos <carpeta-etapa0>/casos.json --limite 4
# Mirar resumen.md: segundos por llamada, memoria e interrupciones. Con eso se decide si sigue y con qué límite.

# Recién después, y si ustedes lo deciden: completar lo que falta en la MISMA carpeta (no repite lo hecho).
bash $E eleccion <minutos> --reanudar <carpeta-etapa1> --completo
```

- `laboratorio-eleccion` exige `--limite N` o `--completo`: **nunca lanza las 96 llamadas por defecto**.
- No suponemos que la corrida completa dure una hora: el límite se fija con lo que mida la prueba.
- Si el tiempo se agota, el ensayo en curso se interrumpe limpio y la corrida se retoma con `--reanudar`.
- Los ensayos de la prueba de rendimiento siguen el plan barajado y cuentan en el análisis, porque el protocolo
  es el mismo. Si después de la prueba cambia algo (modelo, contexto, plantilla), se empieza en una carpeta nueva.

**Pruebas:**
- `tests/test_eleccion.py`, con sandbox y MariaDB reales y un modelo simulado;
- `tests/test_piloto.py::StagesScriptTests`, que prueba que `eleccion` restaura el servicio y que `eleccion0` no
  detiene nada.

## Análisis forense de la Etapa 1 (posterior a los resultados; no modifica el preregistro ni el veredicto)

**Resultado informado por Diego y ChatGPT:**
- acierto del control 75 %, del tratamiento 66,7 %;
- diferencia −8,3 puntos y p = 0,2891 (2 pares en que solo acierta el tratamiento, 6 en que solo acierta el
  control);
- **veredicto preregistrado: inconcluso. Queda así.**

Todo lo que sigue es **posterior a los resultados** y se apoya en 8 pares discordantes. Además, los 6 pares de un
mismo caso no son independientes. Por eso son **hipótesis para un experimento nuevo**, no conclusiones.

### Los prompts, reconstruidos

Los prompts se reconstruyeron exactamente con el commit `9fce3f1`: la Etapa 0 es determinista y la selección dio la
misma. La huella de los 96 textos es `516ccd69c023b98d`, y `laboratorio-eleccion-forense` la calcula sobre los
prompts guardados para comprobarlo.

**1. Separación entre la salida y el verificador: el campo `medido`.**
- El contraejemplo del verificador fijo se muestra como `{'inicio': …, 'suma': …, 'medido': …}`. Tiene la misma forma
  que una ventana de la salida, más una clave que **nadie explica**: es la suma que el verificador recalculó para la
  ventana [inicio, inicio + 15 min).
- Marca a la vez los campos `inicio` y `suma`.
- En el tratamiento, la traza de la candidata que arma `mejores` agrega «deja
  mejores=[[…2 elementos], […2 elementos], …]». Leída junto al contraejemplo, sugiere «a cada ventana le falta un
  tercer campo».
- Lo que sí distinguiría el defecto de D1 no se muestra en ningún lado: la suma 403,121 es exactamente la de la
  ventana siguiente (04:00).
- En D2, `{'salida': 0, 'medido': 15000}` no dice de qué campo es cada número.
- Además, el verificador fijo aparece como `[modelo] VF`, pero lo escribió el coordinador. No confunde las
  condiciones, porque es igual en las dos, pero es ruido.

**2. Claridad temporal y redacción: el patrón de D2 según la rotación.**
- A la instrucción causante (22) le toca el bloque **más corto** (388 caracteres). Dice «cobertura **conserva** el
  valor que dejó esta línea», que suena sano.
- A las víctimas (17 y 15) les tocan bloques más largos (686 y 704), con tres muestras de contadores. Dicen «**ya no
  tiene** el valor que dejó esta línea (cambió después)», que suena a defecto.
- La traza nunca dice **quién** cambió el valor después: lo cambió la línea 22.
- En las líneas que están dentro de un bucle, «antes/después» muestra el valor de la vuelta anterior. Como los dicts
  se recortan, a veces se ven idénticos: en D1, la línea 20 muestra «antes sumas={…}; después sumas={…}» con el
  mismo texto, y la línea 19 dice «antes 23:30; después 23:30», que parece «no cambió nada».

**3. Orden y letras.**
- Rotación 0: la 22 es A, el primer bloque.
- Rotación 2: la 22 es B, y la 17 es C, el bloque más largo y el último antes de la pregunta.
- Rotación 1: la 22 es C. Si el tratamiento no cambió nada en la rotación 1, la recencia sola no lo explica; la
  combinación de largo, etiqueta y posición sí podría.

**4. Motivo frente a candidato (pendiente de las respuestas).** Hay dos explicaciones posibles:
- **(a) Se elige a la víctima:** el motivo nombra la 22 y la 17, y la elección cae en la que la traza marca con «ya
  no tiene el valor».
- **(b) Error de letra:** el motivo nombra solo la 22, pero la letra corresponde a la 17.

La herramienta separa los dos casos: (a) aparece como «nombra varias, incluida la elegida» y (b) como «incoherente».

### Herramienta: `laboratorio-eleccion-forense` (solo lectura, sin modelo)

```bash
bash $E forense <carpeta-etapa1> --salida ~/forense-eleccion.md --json ~/forense-eleccion.json
```

- No escribe dentro de la carpeta del experimento y se niega si se le pide hacerlo. No llama a Ollama ni ejecuta
  código.
- Es genérica. Por cada respuesta informa:
  - qué candidatas nombra el motivo y si es coherente con la letra;
  - los términos que el motivo tomó **solo** de las verificaciones, **solo** de la traza o **solo** de la salida;
  - la etiqueta que la traza le dio a la elegida y a la sembrada;
  - si la elegida tenía el bloque más largo o el último;
  - una matriz de elecciones por caso, rotación y condición;
  - los 8 pares discordantes, con los dos motivos y la traza mostrada.

### Ideas para el próximo experimento (a discutir; nada implementado)

- **Factores separados con los mismos casos**, de a uno, con el mismo largo de texto por candidata:
  - **(i)** un contraste del verificador explicado («el verificador recalculó la suma de la ventana [03:45, 04:00):
    63,327; el instrumento informa 403,121»), frente al actual;
  - **(ii)** una redacción **de flujo**, atribuida al que escribe: «esta línea reemplazó el valor que venían
    acumulando las líneas 15 y 17; ese valor es el que llega a la salida», frente al actual «conserva / ya no
    tiene»;
  - **(iii)** solo los hechos conectados con los campos que fallan, frente a todo;
  - **(iv)** las 6 permutaciones en vez de 3 rotaciones, para separar primacía de recencia.
- **Un coordinador que selecciona, no que agrega.** Desde los campos que fallan, seguir hacia atrás en la traza
  quién escribió por última vez el valor que llega a esa salida. Es un corte dinámico y genérico. Se muestra solo esa
  cadena, con un presupuesto de caracteres igual en todas las condiciones.
- **Una lectura sin modelo antes del modelo:**
  - una regla fija que elige usando solo la evidencia presentada mide si esa evidencia **alcanza en principio**;
  - si la regla no puede, no tiene sentido pedírselo a Qwen.
- **Más casos de sobrescritura:** hoy hay 1 solo. Además, un análisis que trate al caso como unidad (por ejemplo,
  una prueba de permutación por caso), porque los pares de un mismo caso están correlacionados.

### Auditoría de la VivoBook y dos hallazgos de validez (2026-10-22)

**Los prompts coinciden.**
- La VivoBook comparó los 96 prompts guardados con los reconstruidos con `eleccion.partes()`: huella
  `950ecc054f484387` en los dos casos y 0 diferencias. Las 96 respuestas conservaron su huella.
- La reconstrucción de Claude había dado `516ccd69c023b98d`. **Causa:** la versión de Python del sandbox.
  - El caso M050 se cae con `IndexError`, y su traceback entra en «Verificaciones que fallan».
  - Python 3.13 subraya la expresión con `~~~~~~^^^^` y 3.12 con `^^^^^^`.
  - Repitiendo la Etapa 0 con el sandbox en Python 3.12 se obtiene `950ecc054f484387`. El único campo distinto es
    `texto_fallas` de M050.
- **Lección:** la Etapa 0 tiene que guardar la versión de Python del sandbox y una huella por caso y por parte.

**La traza se pierde cuando el instrumento se cae.**
- `instrument_runner` no incluye la traza en el mensaje final de error.
- Por eso el tratamiento de M050 afirmó «se ejecutó 0 veces» para las tres candidatas. Es **falso**: la línea 19 se
  ejecutó y es donde se cae.
- M050 no tiene pares discordantes, así que **no cambia el resultado**. Aun así, son 6 prompts de tratamiento con
  evidencia incorrecta.
- No se corrige en este experimento: queda como condición previa del próximo. Hay que enviar la traza parcial
  también cuando hay error y decir «sin traza» en vez de «0 veces».

**Las repeticiones no aportaron información.** Con temperatura 0,2, las dos repeticiones de cada combinación
eligieron lo mismo. En la práctica hay 24 celdas, no 48 pares independientes, y las discordantes son 4 celdas: 1 a
favor y 3 en contra.

## Próxima etapa, paso 1: instrumentación corregida y E0 sin modelo (2026-10-22)

El experimento de elección terminado **no cambia**:
- sus 96 resultados quedan preservados;
- `eleccion.partes()` sigue reproduciendo los prompts guardados a partir de su `casos.json`.

Una salvedad: si hoy se repitiera su Etapa 0, M050 tendría traza, porque se corrigió el runner, y su prompt de
tratamiento cambiaría. Por eso la reproducción se hace siempre desde el `casos.json` guardado.

### Correcciones de instrumentación

- **La traza parcial llega aunque el instrumento se caiga.** La traza lleva `completa: false`.
- Cada línea informa `interrumpida`: cuántas veces una excepción la cortó antes de terminar. En esas ejecuciones,
  «después» no es lo que la línea habría dejado.
- E0 guarda la **versión de Python del sandbox** y una **huella por caso y por parte**: código, salida, fallas y
  traza.
- Pendiente para el próximo experimento con modelo, sin tocar la plantilla v1:
  - el verificador del coordinador se rotulará «coordinador», no «modelo»;
  - una traza ausente dirá «sin traza», no «0 veces».

### E0: ¿la evidencia del coordinador alcanza para que una regla fija elija la causa?

Hay seis lecturas genéricas. Cada una tiene un criterio causal explícito y se abstiene si el criterio no da una única
candidata. Se combinan en un orden fijo: primero las que usan evidencia ya mostrada a Qwen, después las nuevas.

| lectura | criterio | evidencia |
|---|---|---|
| R1 lugar del error | la excepción ocurre en una candidata | ya mostrada |
| R2 último escritor | escribe un valor que llega a la salida reemplazando otro, y el valor que otra candidata dejó en ese nombre se pierde | traza ya mostrada |
| R3 acumulación sin efecto | una acumulación que se ejecutó y nunca cambió el valor | traza ya mostrada |
| R4 contraste clave/valor | en una agregación `d[k] = …`, el verificador de contraste indica si lo erróneo es la **clave** (no alineada, o la suma informada pertenece a otra ventana) o el **valor** (por ejemplo, signo opuesto); se elige la candidata del camino estático correspondiente | nueva: verificador de contraste |
| R5 omisión | si la instrucción no se ejecutara, pasarían todas las verificaciones | nueva: intervención |
| R6 escala del efecto | un factor k ∈ {−1, ½, 2} sobre el efecto de una acumulación hace pasar todo | nueva: intervención |

**Respuesta a la precaución sobre los errores de cálculo.** La regla del último escritor (R2) solo sirve para
sobrescrituras, y en el banco solo responde en D2. Los errores de cálculo necesitan otra clase de evidencia:
- **contraste clave/valor:** el verificador no solo dice «no coincide», sino *qué parte* del par no coincide;
- **intervención sobre el efecto:** ¿cambiar cuánto suma esta instrucción explica la diferencia?
- **acumulación inerte.**

**Primer hallazgo de E0:**
- Omitir la acumulación de sumas deja **0 ventanas**, y las verificaciones pasan porque no hay nada que verificar.
- Por eso una intervención solo «repara» si la salida conserva su forma (mismas claves y mismos largos de listas).
- En el banco hubo 6 intervenciones así; sin esta condición, R5 habría elegido mal en D1, M029 y M049.
- La misma debilidad afecta a cualquier parche que «arregle» vaciando la salida.

**Resultado en el entorno de desarrollo** (Python 3.13; 15 casos con la sembrada entre las candidatas; 124 versiones
intervenidas; unos 200 s):

| grupo | casos | R1 | R2 | R3 | R4 | R5 | R6 | combinada |
|---|---|---|---|---|---|---|---|---|
| total | 15 | 2/2 | 1/1 | 3/3 | 6/6 | 1/1 | 4/4 | 15/15 |
| cálculo / mutante | 13 | 2/2 | 0/0 | 3/3 | 5/5 | 0/0 | 4/4 | 13/13 |
| cálculo / observado (D1) | 1 | – | – | – | 1/1 | – | – | 1/1 |
| sobrescritura / observado (D2) | 1 | – | 1/1 | – | – | 1/1 | – | 1/1 |

Cada celda es «responde/acierta». Ninguna lectura eligió mal cuando respondió.

**Cómo leerlo (importante):**
- **Las reglas se escribieron mirando estos mismos 15 casos.** El 15/15 dice que la evidencia del coordinador
  alcanza *en principio*. No dice que las reglas generalicen.
- **Circularidad:**
  - los mutantes son cambios de un punto: constante ±1 y operador;
  - R6 con k = ½ deshace un `+= 2`, y con k = −1 deshace un `- v`; R3 detecta un `+= 0`;
  - las únicas pruebas sin esa ventaja son D1 y D2, que vinieron de errores reales de Qwen.
- **Evidencia ya mostrada a Qwen:** R1, R2 y R3 resuelven 6 de 15 casos. D1 y M029 no estaban entre ellos: la
  evidencia que los resuelve (R4) **no se le mostró**.
- **Propuesta:** que Diego y ChatGPT escriban entre 10 y 20 defectos nuevos sin ver las reglas, de cálculo y de
  sobrescritura, y que las reglas se congelen antes de mirarlos. Así E0 deja de medir solo lo posible en principio.

**En la VivoBook** (sin modelo; no detiene nada):

```bash
bash $E e0
```

Debería dar la misma tabla. La versión de Python solo cambia el subrayado de los tracebacks, que R1 no usa.

### Propuesta de prueba corta y controlada (a aprobar antes de implementarla)

- **Pregunta.** Con las mismas candidatas, ¿un contraste del verificador explicado ayuda a Qwen a elegir la causa
  de un error de cálculo con clave?
- **Hipótesis.** Viene del análisis forense, donde el campo `medido` desorientó a Qwen, y de E0, donde R4 resuelve
  esos casos.
- **Casos.** Los 5 de cálculo con agregación con clave en los que E0 indica que el contraste alcanza:
  - D1, M029 y M049: camino de la clave;
  - M030 y M045: camino del valor.
  
  Son 5 defectos distintos. M046 queda afuera porque es el gemelo de M045 (valor inicial −1 en vez de +1).
- **Una sola diferencia.**
  - **Control:** las fallas como en v1, pero con el rótulo «coordinador».
  - **Tratamiento:** el mismo texto más una frase de contraste por ventana, por ejemplo: «el verificador recalculó
    la suma de [03:45, 04:00): 63,327; el instrumento informa 403,121, que es la suma de la ventana que empieza a
    las 04:00».
  - Sin traza en ninguna de las dos condiciones.
- **Contrabalanceo.**
  - las 6 permutaciones de las candidatas;
  - 1 repetición, porque las repeticiones dieron siempre lo mismo;
  - temperatura 0,2;
  - semilla por par;
  - orden barajado.
- **Costo.**
  - 5 casos × 6 permutaciones × 2 condiciones = **60 llamadas**;
  - con 117,8 s de media serían unas 2 horas;
  - con el peor caso observado (313 s), hasta 5,2 horas.
  
  Se corre en tandas con `--limite` (por ejemplo 12 llamadas, unos 25 minutos) y se reanuda.
- **Lectura preregistrada.**
  - Por caso: acierto del tratamiento y del control sobre sus 6 permutaciones.
  - **Pasa a E1 completo** si el tratamiento supera al control en al menos 4 de los 5 casos y en ninguno queda
    por debajo.
  - Con 5 casos no hay potencia para un valor p fino: es una prueba de **efecto grande**, y un resultado pequeño se
    lee como «inconcluso».
  - Además se informan:
    - la posición elegida en el control;
    - la coherencia entre motivo y elección;
    - los términos que el motivo toma solo del verificador.

## Las respuestas reales: qué hipótesis quedan respaldadas (2026-10-22)

Fuente: `forense-eleccion.md` de la VivoBook (huella de los prompts `950ecc054f484387`). Las repeticiones fueron
idénticas, así que la unidad es la **celda** (caso × rotación × condición): 24 por condición.

| hipótesis | estado | evidencia |
|---|---|---|
| F1 verificador sin explicar (`medido`) | **respaldada**, y peor de lo previsto | En los 4 perjuicios de D1 y M029, Qwen escribe «el modelo espera un campo "medido" que no está presente». Leyó el rótulo `[modelo] VF` como «el modelo esperado». «medido» aparece en 6 motivos con traza y en 1 sin ella. |
| F1b relación de S2 no enunciada | plausible | Errores comunes a ambas condiciones en M027 y M039: Qwen elige el contador de `leidas` (línea 10). «Aritmética de la cobertura» nunca dice `leidas = validas + invalidas`. |
| F2 `[…2 elementos]` | **no respaldada** | Ningún motivo lo menciona. Lo que sí aparece es «la línea que construye el resultado». |
| F3 hechos sin atribución | **respaldada en D2** | Con traza, Qwen ve la contradicción («leidas llega a 15000… filas_analizadas es 0») e incluso escribe «la línea 22 reinicia el contador», pero busca la línea que «cuenta» y elige la 17. Nunca une la contradicción con la 22, porque la traza no dice quién cambió el valor. |
| F4 largo del bloque | débil | La elegida tenía el bloque más largo en 20 de 48 (el azar da 16). |
| F5 recencia | **no respaldada** | La elegida era el último bloque en 16 de 48: exactamente el azar. |
| F5b sesgo hacia la posición B | **respaldada** (el control) | El control elige B en **13 de 24 celdas** (el azar da 8). **5 de sus 6 celdas erradas son una B.** En D2 elige B en las tres rotaciones. |
| F6 antes/después ambiguo | sin evidencia | Casi ningún motivo usa «antes» ni «después». |

**Por qué la traza perjudica en D1 y M029.**
- Solo pasa en la rotación 0, donde la línea que arma `mejores` es la A, el primer bloque de la traza. En las otras
  rotaciones el tratamiento elige lo mismo que el control.
- El control razonaba bien («desplaza las ventanas, no quedan alineadas al reloj»).
- Con la traza delante, Qwen pasa a explicar la falla como un problema de **estructura de la salida**: falta
  `medido`. Atribuye ese problema a la línea que construye el resultado.
- **Causa principal:** el verificador mal presentado (F1). La traza no aporta información falsa: le da a Qwen un
  objeto concreto («deja mejores=…», ubicado primero) donde anclar esa lectura equivocada.
- La parte de la posición es una hipótesis plausible, no probada.

**Por qué D2 cambia tanto según el orden.**
- **El control nunca entendió D2.** Elige B en las tres rotaciones y acierta solo cuando la B es la 22 (rotación 2).
- El «perjuicio» de la rotación 2 es la pérdida de un acierto que venía de la posición, no del contenido.
- Con traza, el tratamiento abandona la B: acierta cuando la 22 está primera y, cuando no, elige una víctima
  («se pierde», o la línea que «cuenta»).
- **Balance:** 1 de 3 rotaciones en ambas condiciones. La traza cambió el mecanismo del error (de la posición a la
  víctima), no la comprensión.

**Lo que este experimento dice, en limpio.**
- El resultado está dominado por:
  - la presentación de las verificaciones (F1, F1b);
  - las heurísticas de posición cuando la evidencia no resuelve (F5b);
  - una traza que muestra hechos sin atribuir causas (F3).
- Los casos en que el contraste apunta directo siempre se aciertan en ambas condiciones: M050 (excepción), M030
  (sumas negativas) y M022 (`leidas` 30000 contra 15000).
- **Qwen acierta cuando la evidencia ya contiene la atribución, y adivina cuando tiene que construirla.** Esto
  coincide con E0: la evidencia que resuelve D1 y M029 (R4) no se le mostró.

## Evaluación a ciegas de E0 (preregistro, antes de ver ningún defecto nuevo)

**Reglas congeladas:** `constructor_ia/laboratorio/piloto/e0.py` con huella **`ee46552429b3047e`** (sha256 del
archivo). Cada corrida imprime la huella, y un resultado con otra huella no cuenta para esta evaluación.

**Cómo se hace:**

```bash
bash $E e0 --defectos <carpeta>
```

La carpeta lleva `manifiesto.json` y un archivo por defecto. El formato está en
`constructor_ia/laboratorio/piloto/defectos.py`.

Solo se evalúan esos defectos. **Claude no los ve** antes del resultado, y ninguna regla se ajusta después: un
cambio de regla es otra versión con otra huella, evaluada sobre defectos nuevos.

**Defectos:**
- Entre 10 y 20, escritos por Diego y ChatGPT.
- Al menos 4 de cálculo, 4 de sobrescritura y algunos de condición y de omisión.
- Mejor si se parecen a errores que comete un modelo (como D1 y D2) y no solo a cambios de un carácter. Pueden ocupar
  varias líneas.
- **Blindaje parcial:** ustedes ya leyeron la descripción de las reglas. Para reducir ese sesgo, que los escriba
  ChatGPT en una conversación nueva, sin este historial, con la consigna «errores realistas que un programador o un
  modelo cometería en este instrumento».

**Lectura preregistrada (por defecto):**
- Resultados posibles: equivalente / no detectado / la causa fuera de las 3 candidatas / acierta / se equivoca / se
  abstiene.
- **Lo primero que importa es la precisión cuando responde.** Un coordinador que responde poco pero no se equivoca
  sabe cuándo consultar. Uno que se equivoca con confianza no sirve.
- **Un error de R5 o R6**, que son intervenciones que «confirman», es el hallazgo más grave: significaría que la
  confirmación por intervención no es confiable.
- **Las abstenciones** marcan dónde haría falta Qwen u otra investigación. Se informan por clase.
- **Los defectos con la causa fuera de las candidatas** miden al localizador, no a las reglas.
- **R4 tiene un límite conocido:** su verificador de contraste es específico de este instrumento de ventanas.
  Generalizarlo a instrumentos desconocidos es un problema de la arquitectura, no de esta evaluación.

## Auditoría de la primera evaluación a ciegas de E0 (2026-10-22; reglas `ee46552429b3047e` sin cambios)

**Fuente.** 12 defectos escritos por otro ChatGPT, sin conocer las reglas.
- Banco original: `banco_depuracion_python.zip`. Versión con el manifiesto en formato E0:
  `banco_e0_12_validado.zip`. Los programas de los dos ZIP son idénticos.
- Las huellas de código de los 8 casos evaluados coinciden con las del informe de la VivoBook, así que se auditó
  exactamente lo que se midió.
- Cada defecto se comparó línea a línea contra la referencia del banco.

### Etiquetas

| id | cambio real (diff contra la referencia) | etiqueta | auditoría |
|---|---|---|---|
| X01 | l.19 `% (ancho + 1)` | 19 | correcta |
| X02 | l.19 `m.minute // ancho` | 19 | correcta. **Es el mismo defecto que el mutante M049 del banco**, que se miró al escribir las reglas. |
| X03 | l.20 `+ (v - 1)` | 20 | correcta |
| X04 | l.22 `round(s, 1)` | 22 | correcta |
| X05 | l.17 `validas = 1` | 17 | correcta |
| X06 | l.20 `sumas[inicio] = v` | 20 | correcta |
| X07 | l.12 `leidas = 1` | 12 | correcta |
| X08 | l.15 `invalidas = 1` | 15 | correcta |
| X09 | l.14 `0.5 <= v` | 14 | correcta. Equivalente con estos datos: se infiere que ninguna lectura vale exactamente 0,5. |
| X10 | l.14 `v <= 900` | 14 | correcta. Equivalente con estos datos: se infiere lo mismo para 900. |
| X11 | se borró `continue` (l.16) | 16 | **incorrecta.** En X11.py la línea 16 es `cobertura["validas"] += 1`, que es inocente. Falta el `continue` dentro del `if` de la l.14. |
| X12 | se borró `validas += 1` (l.17) | 17 | **incorrecta.** En X12.py la línea 17 es `m = strptime(…)`, que es inocente. La instrucción falta entre la l.16 y la l.17. |

**Por qué fallan las omisiones.** Al borrar una línea, todas las siguientes se corren una posición, y la etiqueta
quedó apuntando a la línea que ocupa ahora ese lugar. En una omisión no hay ninguna línea existente que sea «la
causa»: hay un **punto de inserción**. La convención para puntuarlas tiene que fijarse antes de la próxima
evaluación.

### Resultado auditado

**Con el filtro de E0** (solo los casos con la causa entre las candidatas):
- X11 pasa a «causa fuera de las candidatas», porque ni el `if` de la l.14 ni la l.15 están entre [20, 18, 16].
- Quedan **7 evaluables: 4 aciertos, 3 abstenciones, 0 errores.**
- Los 4 aciertos son reales: R4 eligió exactamente la línea del diff.

**Sin el filtro, como respondería el coordinador en uso real** (no sabe si la causa quedó entre las candidatas).
Se corrieron las mismas lecturas congeladas sobre los 10 defectos detectados:
- **4 aciertos, 1 error y 5 abstenciones.**
- **El error es X04.** El contraste dice «la clave está bien y el valor no» (403,1 contra 403,121), y R4 elige la l.20,
  donde se acumula la suma. La causa es la l.22: el redondeo, **después** de la agregación.
- La precisión cuando responde es **4 de 5**, no 4 de 4.

**Un casi-error oculto en X11.** El contraste también dice «valor». Si la localización hubiera incluido la l.19, donde
se acumulan las sumas, R4 la habría elegido, y la causa es un `continue` que falta. Se comprobó llamando a R4 con
esas candidatas. R4 se abstuvo solo porque la localización dejó afuera la l.19.

### Por qué no respondieron R1, R2, R3, R5 y R6

- **R1.** Ningún defecto lanza una excepción.
- **R2 y R3, en X05, X07 y X08** (`x = 1` en lugar de `x += 1`). La evidencia estaba en la traza: en X07, la l.12 se
  ejecuta 15000 veces y `leidas` queda clavado en 1. Pero:
  - R2 mira solo la **última** ejecución, donde «antes» y «después» valen 1;
  - R3 exige la sintaxis `+=`;
  - la traza observa el diccionario `cobertura` entero, no la clave `leidas`.
  
  Las reglas están atadas a la **sintaxis** y no al **comportamiento**: «un contador que no progresa».
- **R5.** Omitir `x = 1` no repara nada: falta acumular, no sobra una instrucción.
- **R6.** Solo escala acumulaciones `+=`. Pasar de `=` a `+=` no está en su vocabulario de reparaciones.
- **Las intervenciones confirman solo lo que su vocabulario de cambios puede reparar.** Con defectos que no son
  mutaciones de un punto, ese vocabulario no alcanza.

### Localización

- **X04:** el localizador propuso [21, 19, 20] y no la l.22, que arma `ventanas` con una comprensión.
- **X12:** propuso [15, 12, 9], las escrituras de `cobertura`. Es la zona correcta, pero la causa es una instrucción
  ausente.
- **X11:** con la etiqueta corregida, también quedó afuera.
- Son **3 de 10** defectos detectados con la causa fuera de las candidatas.

### Qué se puede concluir y qué no

**Se puede concluir:**
- En este instrumento, con estos datos, el **contraste clave/valor** (R4) localiza defectos nuevos de cálculo en la
  clave y en el valor de la agregación: X01, X02, X03 y X06.
- Las seis reglas **se abstienen** en los reinicios de contadores y en las omisiones, en lugar de inventar una
  respuesta.

**No se puede concluir:**
- **Que el coordinador sepa cuándo no sabe.** En uso real respondió con confianza y se equivocó en X04, y estuvo a
  una candidata de equivocarse en X11. El modelo de R4 del «camino del valor» termina en la agregación y no ve lo que
  pasa después (redondeo, orden, formato), ni el flujo de control.
- **Que generalice a otros instrumentos.** R4 depende de un verificador de contraste escrito a mano para ventanas.
- **Una estimación de precisión.** Es 4 de 5, con muy pocos casos. X02 repite a M049, y los 12 son cambios de una
  línea en el mismo instrumento.
- **Algo sobre condiciones de borde.** X09 y X10 no se pueden detectar con estos datos.

### Lecciones de método (para la próxima evaluación, sin tocar esta)

1. **Evaluar de punta a punta:** sobre todos los defectos detectados, sin filtrar por «causa entre las candidatas».
   El filtro escondió el único error.
2. **Etiquetar las omisiones por punto de inserción:** bloque y posición, con una regla de puntuación fijada antes.
3. **Las condiciones de borde necesitan datos de borde.** Introducir filas de prueba en la base privada (por
   ejemplo, valores exactos de 0,5 y 900) es en sí una intervención que el coordinador podría diseñar.

## Ciclo v1: hipótesis → reparación → verificación (preregistro, 2026-10-22, antes de cualquier llamada)

**Objetivo:** la primera versión de un ciclo de investigación del coordinador, no otro experimento de elección. El
hallazgo de X04 lo motiva: **identificar una línea sospechosa no es demostrar una causa.**

### Correcciones del método de evaluación (valen desde ahora; las evaluaciones anteriores no cambian)

1. **De punta a punta.** Se evalúan todos los defectos detectados, sin filtrar por «causa entre las candidatas».
2. **Omisiones por punto de inserción.**
   - El manifiesto declara `"insercion": {"despues_de": N, "sangria": K}`.
   - Una localización acierta si señala la instrucción inmediatamente anterior o la inmediatamente posterior al
     hueco dentro del mismo bloque.
   - Si el hueco está al principio o al final del bloque, también vale el encabezado del dueño del bloque (un `if`, un
     `for`…).
   - Implementado en `defectos.objetivo()`. En el banco de desarrollo: X11 → {14, 15} y X12 → {14, 17}.
3. **Datos de borde.** La base privada recibe lecturas exactamente en los límites de validez de la pregunta: una de
   0,5 cada 30 minutos (1440 a escala completa) y 6 de 900. Con ellas, X09 y X10 dejan de ser equivalentes y se
   detectan.

### Reglas y bancos

- **Las seis reglas de E0 siguen congeladas** (`ee46552429b3047e`). El ciclo se niega a correr con otras: cualquier
  cambio de reglas será otra versión del ciclo.
- **Los 12 defectos son ahora el banco de desarrollo** (`constructor_ia/laboratorio/piloto/banco_desarrollo/`):
  - los programas están como llegaron;
  - el manifiesto agrega el punto de inserción en X11 y X12;
  - no miden generalización. Para eso hace falta otro banco independiente.

### El ciclo (`constructor_ia/laboratorio/piloto/ciclo.py`)

1. **Observa.**
   - Ejecuta el instrumento con su suite (S1–S3 y VF) en **dos juegos de parámetros propios**: los visibles
     `{ancho_min: 15, top: 3}` y `{ancho_min: 20, top: 4}`.
   - El segundo juego **no** es el de reserva del juez (`{30, 5}`), y el código lo afirma.
   - Si nada falla, no hay nada que investigar.
2. **Formula hipótesis.**
   - Localiza 3 candidatas y consulta las reglas congeladas.
   - Si una regla da una única candidata, esa va primero; después siguen las demás, en el orden del localizador.
   - Como máximo **3 hipótesis por defecto**.
3. **Pide una reparación.** Por cada hipótesis hace una llamada corta a Qwen: «hipótesis TODAVÍA NO COMPROBADA: la
   causa está en la instrucción marcada; proponé el reemplazo mínimo». Qwen puede responder ELIMINAR o NO_ES_ESTA.
   - **Qwen ve:** la pregunta; la salida y las fallas de los juegos que fallan; un glosario fijo de las verificaciones
     (que ahora se rotulan como del coordinador); el código numerado con la hipótesis marcada.
   - **Qwen no ve:** la referencia, los parámetros de reserva, la descripción ni las líneas del defecto.
   - Cada hipótesis es una llamada independiente, sin historial. Semilla por (defecto, hipótesis), temperatura 0,2,
     `keep_alive` 0s, contexto de 4096 y guardia de memoria.
4. **Verifica: solo el coordinador, solo en el sandbox.** Acepta la reparación únicamente si:
   - cambia el código;
   - pasa la revisión estática;
   - en **los dos juegos** pasa toda la suite;
   - en los dos conserva la **forma** de la salida. Si el defecto no terminaba con un juego, se exige que ninguna
     lista quede vacía.
   
   La primera reparación aceptada termina la investigación («confirmada»). Si ninguna se acepta, el ciclo **se
   abstiene**. Qwen propone; nunca aprueba.
5. **Juez, después y por separado.**
   - **Aislamiento.** `investigar()` recibe solo `{"id", "codigo"}`; una prueba lo verifica con un espía. El juez se
     crea recién cuando la investigación terminó.
   - **Qué compara.** Cada propuesta contra la referencia, en los parámetros visibles y en los de reserva, con los
     datos de borde.
   - **Auditoría de fugas** en cada prompt: líneas de la referencia, la descripción del defecto y los parámetros de
     reserva.

### Métricas preregistradas

- **Principal: confirmaciones falsas.** Son reparaciones aceptadas que el juez rechaza. Importa más no aceptar una
  reparación incorrecta que abstenerse.
- **Secundarias:**
  - confirmaciones verdaderas;
  - abstenciones;
  - «abstención con una reparación correcta rechazada», que mide si la verificación es demasiado estricta;
  - llamadas usadas;
  - reglas v1 de punta a punta;
  - causa fuera de las candidatas;
  - fugas.
- **Lectura.** Una sola confirmación falsa es un hallazgo que se analiza antes de seguir; no se promedia con los
  aciertos.

**El riesgo ya está a la vista.** La prueba `test_true_and_false_confirmations…` usa una reparación simulada de X05
(`validas += 2; invalidas -= 1`). Esa reparación:
- pasa S1, S2, S3 y VF en los dos juegos;
- es incorrecta.

El ciclo la acepta y el juez la marca como **CONFIRMACIÓN FALSA**. Es decir, la suite del coordinador **no controla
cómo se reparten las lecturas entre válidas e inválidas**, y una reparación de Qwen podría aprovechar ese hueco.

### Ciclo sin modelo sobre el banco de desarrollo (entorno de desarrollo, Python 3.12, 0 llamadas, 188 s)

- Con datos de borde se detectan los 12 defectos; ninguno queda equivalente.
- **Reglas v1 de punta a punta: 4 aciertos, 3 errores y 5 abstenciones** (precisión de 4 de 7).
  - Los errores son X04 (redondeo) y X09 y X10 (límites inclusivos).
  - En los tres, R4 dice «camino del valor» y elige la acumulación.
- En **5 de 12** defectos la causa queda fuera de las 3 candidatas: X04, X09, X10, X11 y X12. En esos casos ninguna
  hipótesis apunta a la causa, así que **la conducta correcta del ciclo es abstenerse**.
- **El peligro concreto en esos 5 casos:** que Qwen proponga, en otra línea, una compensación que la suite no ve. En
  X09 y X10, por ejemplo, filtrar el valor de borde en la acumulación sin corregir la condición.

### Prueba pequeña en la VivoBook (la deciden ustedes; nada es automático)

```bash
# 0) Sin modelo (no detiene nada; unos 15–20 minutos en la VivoBook): reglas v1 de punta a punta y juez
bash $E ciclo0 --defectos constructor_ia/laboratorio/piloto/banco_desarrollo

# 1) Prueba pequeña: 4 defectos, como máximo 12 llamadas, protegida (detiene y SIEMPRE restaura constructor-ia)
bash $E ciclo 90 --defectos constructor_ia/laboratorio/piloto/banco_desarrollo --ids X01,X07,X09,X04 \
     --limite-llamadas 12
# si se corta: bash $E ciclo 90 --reanudar <carpeta> --limite-llamadas N
```

**Por qué estos 4 defectos:**

| defecto | regla | causa | qué pone a prueba |
|---|---|---|---|
| X01 | acierta | entre las candidatas | confirmación esperable con la primera hipótesis |
| X07 | se abstiene | tercera hipótesis | Qwen tiene que decir NO_ES_ESTA o fallar en las dos primeras |
| X09 | se equivoca | fuera de las candidatas | riesgo de compensación |
| X04 | se equivoca | fuera de las candidatas | el caso que motivó todo |

**Tiempo estimado:**
- 12 llamadas × 118 s de media son unos 24 minutos, más la observación y las verificaciones en el sandbox;
- con el peor caso visto (313 s por llamada) serían unos 63 minutos;
- límite propuesto: 90 minutos.

**Qué revisar después:**
- `resumen.md` y `juez.json`;
- `llamadas.jsonl`: prompts por partes, respuestas crudas, segundos, memoria;
- `interrupciones.jsonl`;
- que no haya fugas.

### Qué todavía NO hace el ciclo v1 (a propósito)

- No usa el historial entre hipótesis: un rechazo no se le explica a Qwen.
- No le pide a Qwen hipótesis nuevas fuera de las 3 candidatas.
- No aprende de corridas anteriores.
- No diseña verificaciones nuevas (por ejemplo, una que controle el reparto entre válidas e inválidas).

Son los pasos siguientes de la arquitectura, y cada uno será otra versión.
