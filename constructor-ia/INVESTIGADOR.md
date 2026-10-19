# Investigador autónomo — prototipo experimental (0.5.0-exp1)

Módulo nuevo, `constructor_ia/investigador/`, separado del Explorador 0.4.8 (`constructor_ia/explorer/`). El
Explorador queda intacto y se puede seguir usando para comparar (`explorer-trial`).

Objetivo: una IA que entra a una base desconocida, investiga por iniciativa propia, aprende de la evidencia y usa ese
conocimiento para decidir qué investigar después. La unidad de progreso es el **conocimiento respaldado que contribuye
a una investigación posterior**, no la cantidad de consultas.

> Las pruebas automáticas usan un «modelo» de guion: comprueban que la arquitectura funciona y sigue siendo segura.
> **No demuestran inteligencia autónoma.** Eso solo se valida con Qwen3 real en la VivoBook.

## Ciclo

```
MEMORIA ──(contexto seleccionado, ≤ 1400 tokens)──► QWEN3: PEDIDO  (solo al empezar o si no hubo «siguiente»)
   ▲                                                    │  {pregunta, porque, depende_de, medir{que, objetos, filtro|sql}}
   │                                                    ▼
   │                                         MOTOR: nombres EXACTOS → plan verificable → intermediario de solo lectura
   │                                                    │  (o ACLARACIÓN compacta, sin ejecutar nada)
   │                                                    ▼
   │                                   EVIDENCIA REAL + alcance + hechos calculados (comprobado)
   │                                                    │
   │                                                    ▼
   │                          QWEN3: LECTURA — en UNA respuesta: interpretación, ≤2 afirmaciones, dudas, dudas que
   │                          cierra y SIGUIENTE pedido (formulado con la evidencia delante)
   │                                                    │
   └──── VERIFICADOR: cifras/valores/identificadores, alcance, texto cortado · dependencia declarada vs comprobable
```

- **Una llamada por ciclo:** la lectura trae la siguiente pregunta, que se ejecuta sin otra llamada. La pregunta
  inicial (`PEDIDO`) se pide solo al empezar una línea o si una lectura no trajo un siguiente válido.
- **La siguiente pregunta siempre se genera DESPUÉS de recibir la evidencia real.**
- **Sin cadenas de reparación:** una respuesta que no es JSON tiene UN reintento corto. Si vuelve a fallar, el ciclo
  queda `fallo_modelo`. Con 2 seguidos, la investigación se detiene.
- **Sin informes extensos ni aplicaciones:** solo ciclos, evidencia, memoria, decisiones y métricas.

## Contrato

**Pedido (Qwen3 → motor).** El modelo no escribe herramientas: dice qué medir y sobre qué.

| `medir.que` | Qué mide | Objetos |
|---|---|---|
| `conteo` | filas (de una tabla, o con un filtro) | tabla |
| `distribucion` | valores más frecuentes | tabla.columna |
| `rango` | nulos, distintos, mínimo, máximo, promedio | tabla.columna |
| `ejemplos` | filas reales (todas o las columnas indicadas) | tabla[.columna…] |
| `relacion` | ¿los valores de una columna existen en otra? | dos tabla.columna |
| `tendencia` | filas por día de una columna de fecha | tabla.columna (fecha) |
| `sql` | un SELECT propio de solo lectura en `medir.sql` | — |

`filtro: {objeto, valor}` restringe a una columna igual a un valor copiado de los datos (siempre como literal SQL).
`remedir: true` vuelve a medir algo ya medido. Con hasta 160 nombres, `objetos` está restringido por la gramática a
nombres reales del esquema.

**Motor de evidencia (`engine.py`), determinista.**
- Los nombres se resuelven solo por coincidencia EXACTA (sin distinguir mayúsculas ni acentos). Nunca se elige un
  nombre por parecido.
- Si un nombre no existe, es ambiguo (una columna de varias tablas) o es incompatible con lo pedido (distribución
  sin columna, tendencia sin fecha, relación con un solo objeto, objetos de varias tablas, filtro de otra tabla), el
  motor devuelve una **aclaración** con las opciones reales y no ejecuta nada.
- El SQL libre pasa por `precheck_sql` y por el intermediario (`sqlguard`, una sola sentencia, solo lectura, límites
  de sesión, usuario con permisos mínimos).
- Si la misma medición ya existe (de cualquier expedición), se entrega esa evidencia marcada como «YA MEDIDO», en
  lugar de repetirla.
- Para conteo, distribución, rango y relación calcula hechos `comprobado` sin interpretación, en el espacio de nombres
  `inv:` (nunca modifica un hecho histórico).
- El alcance (muestra o tabla completa) se calcula con el SQL realmente ejecutado (reglas de 0.4.7).

**Lectura (Qwen3, con la evidencia delante).** Responde un JSON con:
- `lectura`;
- hasta 2 `afirmaciones` `{texto, evidencia, alcance}`;
- hasta 2 `dudas`;
- `cierra` (los D# que la evidencia responde);
- `siguiente` (un pedido; `nueva_linea: true` para cambiar de rumbo).

**Verificación.** Cada afirmación se contrasta con su evidencia con las mismas reglas de 0.4.7:
- cifras y valores en los datos, e identificadores frente a alias y parámetros;
- alcance: una afirmación sobre toda la tabla con evidencia de muestra queda como hipótesis;
- un texto cortado no se certifica.

Las afirmaciones se guardan en su propio espacio de nombres (`inv:hallazgo:`). Si el modelo repite textualmente un
hecho histórico, ese hecho no se toca.

## Memoria de investigaciones (migración 0008, solo agrega tablas)

- `inv_lines`: líneas de investigación (el hilo, que puede cruzar misiones).
- `inv_cycles`: por ciclo, se registra:
  - la pregunta, el porqué y de qué dice depender;
  - el pedido, el plan o la aclaración;
  - la consulta y la evidencia (también la reutilizada), y los hechos calculados;
  - la lectura y las afirmaciones con su verificación;
  - las dudas abiertas y cerradas;
  - las conclusiones reutilizadas;
  - el siguiente pedido;
  - la dependencia evaluada y el costo (llamadas, tokens, segundos).
- `inv_doubts`: dudas abiertas y cerradas, y qué ciclo las cerró.
- `inv_sessions`: estado para reanudar (línea, último ciclo, pedido pendiente, evidencia sin leer) y métricas.

**Contexto de cada llamada**, seleccionado por el software y con tope de tokens: el objetivo, la línea activa (últimos
ciclos y lecturas), las dudas abiertas (incluidas las incertidumbres históricas del Explorador), lo que ya se sabe de
las tablas en juego, lo ya medido, los intentos fallidos, otras líneas abiertas y el esquema. No le dice qué
investigar.

## Dependencias: declarada frente a comprobable

- **Declarada:** el siguiente pedido cita en `depende_de` la consulta, un hecho calculado, una afirmación respaldada o
  una duda del ciclo anterior. Es lo que el modelo dice; no prueba nada.
- **Comprobable:** el pedido usa un valor (de filtro o literal de su SQL) que **apareció en los datos del resultado
  anterior** y que el modelo **no tenía antes de verlo** (no figuraba en su contexto ni en su pedido anterior).
- **Reutilización de una conclusión:** declarada si el pedido cita un hecho respaldado anterior; comprobable solo si
  además usa un valor que esa conclusión contiene. Citar un hecho anterior es una referencia, no aprendizaje.

## Criterios de la primera prueba real

- **Dos transiciones comprobables seguidas:** tres consultas encadenadas, cada pregunta anclada en un valor del
  resultado anterior.
- **Al menos una conclusión respaldada reutilizada de forma comprobable.**

Si no se cumplen, `donde_fallo` dice el **primer punto exacto**:
- un ciclo sin respuesta válida;
- una aclaración;
- una consulta con error o sin filas;
- una pregunta que solo citó el ciclo anterior o no usó ningún valor nuevo;
- un límite de tiempo.

También informa el consumo total: llamadas, tokens de entrada y generados, y segundos de modelo y de reloj.

**Referencia de tamaño** (base de prueba): el sistema ocupa ~430 tokens; el pedido inicial, ~230 de contexto; cada
lectura, ~700–1100 de contexto más la evidencia. Las respuestas tienen un tope de 320 (pedido) y 720 (lectura).

## Prueba en la VivoBook (cuando la autoricen)

```bash
cd ~/src/dieguinPinga
git fetch origin claude/constructor-ia-0.5-investigador
git checkout claude/constructor-ia-0.5-investigador
git pull --ff-only
bash ~/src/dieguinPinga/constructor-ia/scripts/update.sh
~/constructor-ia/bin/constructor-ia selftest
systemctl --user stop constructor-ia          # para no competir por Ollama CPU
~/constructor-ia/bin/constructor-ia investigar caverna_01 --ciclos 4 --minutos 15 --yes
~/constructor-ia/bin/constructor-ia investigacion-report <misión>
systemctl --user start constructor-ia
```

- `investigar` muestra los eventos en vivo y al final el resumen: ciclos, dependencias con sus anclas, afirmaciones
  y su verificación, reutilizaciones, costo por ciclo y `donde_fallo`.
- Los archivos quedan en la misión: `investigacion/ciclos.json`, `metricas.json` y `resumen.md`.
- **Interrumpir y reanudar:** Ctrl-C deja la sesión `interrumpida`, con la evidencia recibida y sin leer (si la
  había) y el pedido pendiente. Para continuar el mismo hilo:
  `investigar caverna_01 --reanudar <misión> --yes`. Primero lee lo que quedó sin leer y después ejecuta el pedido
  pendiente, sin una pregunta inicial nueva.
- **Comparar con el Explorador:** `explorer-trial caverna_01 …` sigue funcionando igual.

No se aplican reevaluaciones (`explorer-reevaluar` sigue en simulación salvo autorización), no se modifican los
hechos históricos y no se toca la configuración de Ollama ni otros servicios.

## Rollback

```bash
~/constructor-ia/bin/rollback.sh
```

La migración 0008 solo agrega tablas `inv_*`; 0.4.8 las ignora. Los hechos que creó el investigador (`inv:…`) quedan
como cualquier otro hecho nuevo, sin afectar a los históricos. Para volver a la base exacta anterior, usar una copia
de seguridad (`constructor-ia backup` antes de probar; INSTALL.md §10).

## Limitaciones conocidas

- La dependencia comprobable se basa en valores concretos. Una pregunta legítimamente motivada por lo observado, pero
  que no usa ningún valor (por ejemplo, «veo nulos en X, ¿qué pasa con Y?»), cuenta solo como declarada. Es una
  medición conservadora.
- Si una afirmación se rechaza, la siguiente pregunta formulada en la misma respuesta se ejecuta igual (puede
  apoyarse en una lectura equivocada). La siguiente evidencia lo muestra.
- `cierra` lo decide el modelo: una duda cerrada no implica que la respuesta esté verificada.
- Por ahora no hay ejecución autónoma permanente ni interfaz web propia: se usa desde la consola.
