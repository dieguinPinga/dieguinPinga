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
