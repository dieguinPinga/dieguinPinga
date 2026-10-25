# Protocolo · Qwen real en P1: sin coordinador frente a con coordinador

> **Estado: preparado, no ejecutado.** Para revisar antes de autorizar inferencias reales. El ensayo con
> modelo simulado (`bash comparar_qwen_p1.sh --simulado`) forma parte de `validar.sh`.

## Pregunta

Cuando Qwen 2.5 Coder 7B entra en el bucle de P1 (repite pruebas sin cambiar el código), ¿un único mensaje
del coordinador de OH-DUO-04, entregado durante el mismo `run()`, cambia lo que hace después y su resultado?

## Diseño

Son dos corridas de una ejecución cada una. Todo es idéntico salvo `--enviar`:

| Brazo | Corrida | Cadena (observador → coordinador → despachador) | Buzón |
|---|---|---|---|
| Control | `qwen-p1-s<semilla>-<etiqueta>-control` | corre completa y decide, pero **no envía** | vacío |
| Intervenida | `qwen-p1-s<semilla>-<etiqueta>-intervenida` | corre completa y **envía** | lo escribe el despachador |

Los dos brazos usan el mismo ejecutor (`oh-duo-05/ejecutor`, con el puente activo). Así, la única diferencia
posible es el contenido del buzón. El orden es primero el control y después la intervenida, una tras otra y
nunca en paralelo: `ejecutar.py` lo impide.

### Condiciones fijas

`comparar_qwen_p1.sh` las fija; no se toman del entorno.

| | Valor |
|---|---|
| Problema | P1 (`unir_intervalos`), banco de OH-DUO-01 sin cambios |
| Modelo | `qwen2.5-coder:7b`, rol `qwen_minimo.txt` |
| Semilla / temperatura | 11 (`--semilla`) / 0,2 |
| Límites | 30 iteraciones (`MAX_ITER`), 900 s + 60 s de gracia, `num_ctx` 16384 |
| Sandbox | bwrap igual que en OH-DUO-01, más el buzón en solo lectura; `systemd-run --scope` (2 GB, 256 tareas, 200 % CPU) |
| Coordinador | `coordinador.json` sin cambios: confirma con A2 o ≥ 2 reglas de bucle; máximo 2 intervenciones; enfriamiento 5; inglés |
| Despachador | solo `nueva_estrategia`, solo texto de plantilla, margen ≥ 5 iteraciones |
| Ollama | `http://100.92.153.59:11435`, sin cambios de configuración |

`protocolo.json` registra las huellas sha256 de todo lo que interviene: ejecutor, puente, cadena, duo.py,
rol, P1, detector, umbrales, observador y coordinador. También guarda la memoria de la GPU antes y después.

## Procedimiento

```bash
cd ~/oh-duo-05
bash comparar_qwen_p1.sh --real --confirmo-inferencia-real --semilla 11      # ~15–35 min
```

1. **Comprobaciones previas.** Cualquiera de estas aborta antes de usar la GPU:
   - un `correr.sh` en curso;
   - corridas que ya existen;
   - falta el venv o falta el supervisor;
   - Ollama no responde;
   - `qwen2.5-coder:7b` no figura en `/api/tags`.

   Las consultas a Ollama son solo de lectura (`/api/version`, `/api/tags`).
2. **Control.** Arranca `cadena.py` sin `--enviar` y después `ejecutar.py ejecutar`. El control previo de
   `correr.sh` verifica el sandbox y que el buzón sea de solo lectura.
3. **Intervenida.** Es igual que el control, con `--enviar`.
4. **Verificación.** Las pruebas ocultas de las dos ejecuciones corren fuera del sandbox, después de congelarlas.
5. **Comparación.** Lo hace `intervenciones.py comparar`, que escribe `comparacion.md` y `comparacion.json`.

Todo queda en `informes/qwen-p1-s11-<etiqueta>/` y en `corridas/`. El script no borra nada.

## Qué se mide

**Primaria:** la categoría con pruebas ocultas de cada brazo (`correcta`, `incorrecta`, `incompleta`, …).

**Secundarias:**
- terminación según OH-DUO-02: `finalizacion_con_finish` o `agotamiento_de_iteraciones`;
- iteraciones y acciones;
- si las pruebas visibles pasan;
- el veredicto de estancamiento de OH-DUO-02.

**Del mensaje** (solo en la intervenida), en tres niveles:
1. está en la conversación, como `MessageEvent` con `sender oh-duo-04:<id>`, y en `entregas.jsonl`;
2. hay una acción posterior del agente;
3. después del mensaje, el agente edita el código productivo o las pruebas aprueban.

También se registra en qué iteración llegó la decisión, cuándo se entregó y cuántas acciones quedaban.

## Criterios de validez

Los evalúa `intervenciones.py` y salen en `comparacion.md`. Si falla alguno, el par **no sirve** para
atribuirle nada al mensaje:

| Criterio | Por qué |
|---|---|
| misma semilla y mismo modelo en los dos brazos | condiciones iguales |
| el control no recibió ningún mensaje | el control es control |
| ≤ 30 iteraciones en cada brazo | presupuesto intacto |
| originales intactos en los dos | el agente no tocó lo protegido |
| verificados y sin fugas de pruebas ocultas | evaluación limpia |
| **acciones idénticas hasta el mensaje** (herramienta y argumentos generados) | atribuye la diferencia al mensaje y no al azar |
| cada mensaje entregado tiene acción posterior; el puente no descartó ni rechazó nada | el mensaje llegó a tiempo |

El aviso de que el modelo no estaba cargado en Ollama no invalida el par: cambia los tiempos, no las
respuestas.

## Cómo leer el resultado

| Resultado | Lectura |
|---|---|
| par no válido por divergencia previa | Qwen no fue determinista entre corridas. Repetir, y si vuelve a pasar, comparar por distribución con varias semillas en lugar de por pares |
| válido, sin intervención | Qwen no entró en bucle confirmado esta vez (o salió solo). No mide el mensaje. Probar otra semilla |
| válido; intervino; sin acción posterior | el mensaje llegó tarde. Revisar la latencia y el margen |
| válido; intervino; acción posterior pero sin cambio de conducta | Qwen recibió el mensaje y siguió igual. Es un resultado negativo **real** para esta plantilla |
| válido; cambió la conducta, pero la categoría no mejora | el mensaje rompe el bucle pero no orienta. Dato para rediseñar la plantilla, nunca para darle la solución |
| válido; la intervenida es `correcta` y el control no | evidencia a favor, **en un caso** |

**Un par es un caso, no una medición.** Si el primer par es válido e informativo, el paso siguiente que
propongo es repetir con las semillas 11, 12 y 13 y alternar el orden (intervenida primero en la semilla 12),
para descartar el efecto de la temperatura de la GPU o de la carga previa. Recién después conviene ampliar a
P2–P6. Cada ampliación necesita tu autorización aparte.

## Fuera de este protocolo

- Gemma; otros problemas; cambios en prompts, roles, plantillas, umbrales o límites.
- Activar el coordinador en corridas de producción de OH-DUO-01: las corridas son siempre de `oh-duo-05/corridas/`.
- Mensajes de advertencia (A3/A5): el despachador solo envía `nueva_estrategia`.

## Revisión después de la corrida

1. Validez: `comparacion.md`, en la línea «par VÁLIDO / NO VÁLIDO».
2. Texto entregado: `corridas/<intervenida>/ejecuciones/P1__qwen/instancia/resultados/*/buzon/mensajes.jsonl`.
3. Qué hizo Qwen en cada brazo: `informes/<corrida>/observador/P1__qwen/supervision.md` de los dos brazos.
4. Huellas: `protocolo.json` contra el `validar.sh` de ese día.
