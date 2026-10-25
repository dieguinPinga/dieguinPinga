# Auditoría de A1 y B1 reales (P1, Qwen, semilla 11)

> **Estado.** Las trayectorias reales están en la VivoBook. No se suben a la rama: `corridas/` e `informes/`
> están en `.gitignore`. Este documento separa lo que ya se puede afirmar de lo que tiene que salir de los
> registros. Esos datos los produce, solo leyendo, `diagnostico/auditar_vivobook.sh`.

```bash
rm -rf /tmp/oh-src && git clone --depth 1 --branch claude/zealous-cray-jqmao5 https://github.com/dieguinPinga/dieguinPinga.git /tmp/oh-src && cp -r /tmp/oh-src/oh-duo-05/. ~/oh-duo-05/ && rm -rf /tmp/oh-src && bash ~/oh-duo-05/diagnostico/auditar_vivobook.sh
```

El resultado queda en `informes/b1-p1-s11-<etiqueta>/auditoria/auditoria.md`. No hace inferencias ni toca
Ollama o las corridas. Las versiones intermedias de `intervalos.py` se evalúan en un bwrap nuevo, sobre copias.

## Hechos ya registrados

Son los informados del experimento y lo que se verifica en el repositorio.

| # | Hecho | Fuente |
|---|---|---|
| H1 | A1: 3 de 3 corridas idénticas (30 pedidos, respuestas y acciones), agotaron las 30 iteraciones, sin `stuck` y sin resolver | `comparacion_a1.md` |
| H2 | B1: 3 de 3 válidas y reproducibles, cambio de conducta inmediato, 0 de 3 resolvieron P1. El mensaje entró en la llamada 9 | `comparacion_b1.md` |
| H3 | Ediciones de `intervalos.py`: 6 en B1 y 5 en A1. `comparar_b1` cuenta **todas** las acciones `file_editor` que no son de lectura, incluidas las rechazadas o sin efecto; la auditoría separa aplicadas y rechazadas | `comparar_b1.py` |
| H4 | El defecto de P1 está en la fusión: `resultado[-1] = (resultado[-1][0], fin)`. Cuando el intervalo nuevo está contenido en el anterior, achica el extremo. La corrección es tomar `max(resultado[-1][1], fin)` | `oh-duo-01/banco/P1` |
| H5 | El original pasa 2 de 3 pruebas visibles y 4 de 6 ocultas. La visible que falla es `test_intervalo_contenido`, con `AssertionError: Lists differ: [(1, 3)] != [(1, 10)]` | medido por `auditar_b1.py` sobre el original |
| H6 | El mensaje de B1 nombra la prueba que falla y pide «state which result you expected and which one you got», pero **no** incluye el resultado esperado ni el obtenido. Esos datos el agente ya los tiene en la salida de la prueba | `coordinador.json` (plantillas) |

## Lo que responde la auditoría (por pregunta)

| Pregunta | Sección de `auditoria.md` | Qué hay ahí (hechos) |
|---|---|---|
| 1. Acciones desde la intervención | §1 | A1 y B1 lado a lado desde la acción del mensaje: herramienta, comando y observación, por llamada |
| 2. Las 6 ediciones frente a las 5 | §2 y 5 | Cada edición con llamada, texto quitado y puesto, si se aplicó y si la herramienta la aceptó, más el estado reconstruido del archivo, comprobado contra el archivo final congelado |
| 3. Hipótesis de Qwen | §3 | El pensamiento textual de cada llamada. Las etiquetas por palabras clave son *interpretación automática* |
| 4. Pruebas protegidas | §4 | `file_editor` sobre `test_*.py`, escrituras por terminal, alertas A3/A5 de OH-DUO-03 y advertencias de OH-DUO-04 (en B1 no se envían) |
| 5. ¿Se acercó y revirtió? | §2 y 5 | Pruebas visibles y ocultas que pasa cada versión, parecido con la referencia y si contiene la corrección. «Versión mejor revertida» aparece solo si una versión intermedia pasó más ocultas que la final |
| 6. Señales para un diagnóstico independiente | §6 | Cada mensaje de fallo de las visibles, desde qué llamada y cuántas veces, y si los pensamientos lo mencionan |

La herramienta se validó con una conversación real del SDK que culpa a la prueba, la edita con
`file_editor` y con `sed -i`, aplica la corrección y la revierte. Las 5 pruebas pasan
(`tests/test_auditoria.py`). En el par A1/B1 simulado, la reconstrucción coincide con el archivo final en
4 de 4 corridas.

## Propuesta mínima para el próximo experimento (B2)

Se decide con la auditoría y cambia **una sola cosa** respecto de B1. Los controles siguen siendo A1, y la
comparación es válida mientras el prefijo coincida con A1 hasta el mensaje.

| Si la auditoría muestra… | Hipótesis | B2 propuesto (único cambio) | Requiere |
|---|---|---|---|
| §6: el fallo `[(1, 3)] != [(1, 10)]` aparece muchas veces y los pensamientos nunca lo citan; §5: ninguna edición toca la línea de la fusión | Qwen no conecta el resultado observado con la línea defectuosa | que el mensaje incluya **la línea de fallo visible** (`AssertionError: …`), que el agente ya tiene en su contexto: es observable y no es la solución | cambiar la plantilla `pruebas_fallando` de OH-DUO-04: autorización |
| §4: después del mensaje vuelve a tocar `test_*.py` | el mensaje no alcanza para frenar el cambio de pruebas | B1 con `--incluir-advertencias` (envía también las advertencias A3/A5 que OH-DUO-04 ya decide) | solo una opción del despachador, sin cambiar código |
| §5: una versión con la corrección pasó más pruebas y después fue revertida | Qwen encuentra la corrección pero no la reconoce | que el mensaje informe el **resultado visible** de cada versión («después de la acción k pasaban 3 de 3 visibles»), solo con pruebas visibles | nueva plantilla y dato variable: autorización |
| §3: el pensamiento sostiene que la prueba está mal (la culpa a la prueba) | la estrategia del modelo, no la información | mantener el mensaje y cambiar el momento (más temprano): probar si la hipótesis se fija con las iteraciones | cambiar el umbral de confirmación de OH-DUO-04: autorización |

**Recomendación previa, a confirmar con los datos.** H5 y H6 muestran que la señal decisiva (esperaba
`[(1, 10)]`, obtuvo `[(1, 3)]`) está en la salida de la prueba y no en el mensaje. Si la §6 confirma que Qwen
nunca la cita, la primera fila es la de menor costo y la más informativa:
- un solo dato variable más en la plantilla;
- solo información visible;
- se puede comparar con A1 y con B1 en el mismo punto de inyección.
