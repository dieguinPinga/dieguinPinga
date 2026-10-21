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
bash $E ensayo 90
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
