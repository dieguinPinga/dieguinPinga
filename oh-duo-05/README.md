# OH-DUO-05 · Integración aislada del ejecutor con el puente de mensajes

Es la continuación de `investigacion/`. Lleva las decisiones del coordinador de OH-DUO-04 a la conversación
del programador **durante el mismo `run()`**, con `conversation.send_message()`. No pausa ni reinicia, y el
presupuesto sigue siendo de 30 iteraciones.

OH-DUO-01, 02, 03 y 04 no se modifican: se importan desde sus carpetas (`../oh-duo-0X`, `~/oh-duo-0X` o la
variable `OH_DUO_0X`). Esta fase **no hace inferencias reales** y **no activa el coordinador sobre corridas de
producción**. El despachador solo escribe en corridas creadas por `ejecutar.py`, que llevan la marca
`.oh-duo-05`, y por omisión no escribe nada.

## Validación (VivoBook, un solo comando)

```bash
rm -rf /tmp/oh-src && git clone --depth 1 --branch claude/zealous-cray-jqmao5 https://github.com/dieguinPinga/dieguinPinga.git /tmp/oh-src && mkdir -p ~/oh-duo-05 && cp -r /tmp/oh-src/oh-duo-05/. ~/oh-duo-05/ && rm -rf /tmp/oh-src && bash ~/oh-duo-05/validar.sh
```

`validar.sh` no contacta el Ollama real. El modelo es un servidor simulado en `127.0.0.1` y las corridas se
crean en un directorio temporal. Los pasos son:

| # | Paso | Qué comprueba |
|---|---|---|
| 1 | prerrequisitos | venv, bwrap, oh-smoke, oh-supervisor y oh-duo-01..04 presentes; ningún `correr.sh` activo |
| 2 | diagnóstico del SDK | las 10 comprobaciones de la investigación |
| 3 | OH-DUO-01..04 sin cambios | 136 archivos idénticos a los de la rama (`huellas_oh_duo_01_04.sha256`) |
| 4 | despachador, 12 pruebas | solo plantillas, solo corridas marcadas, sin enlaces, idempotente, simulación sin escritura |
| 5 | puente, 5 pruebas | SDK real en proceso; ver detalle abajo |
| 6 | integración, 7 pruebas | bwrap real, puente HTTP real, SDK real y la cadena completa, control contra intervenida |

Las pruebas del puente verifican:
- entrega durante `run()` con el límite intacto;
- duplicados ignorados;
- que un mensaje tras `finish` se descarta sin volver a `idle`;
- formato inválido y líneas a medio escribir;
- que funciona sin buzón.

El registro y la comparación quedan en `~/oh-duo-05/informes/validacion-<fecha>/`. El paso 6 tarda entre 1 y 2
minutos y usa `systemd-run --user --scope`, igual que las corridas reales. Donde no hay sesión de usuario, se
corre con `SIN_SCOPE=1`.

## Arquitectura

```
 anfitrión                                                          │ sandbox bwrap (sin red, / en solo lectura)
                                                                    │
 ejecutar.py ── duo.py de OH-DUO-01 (sin cambios) ── correr.sh ─────┼─▶ agente.py (smoke.py)
                                                                    │     conv.run()  ← mismo run(), 30 iteraciones
 cadena.py                                                          │     └─ hilo Puente (puente.py, solo lectura)
   observador OH-DUO-03 ◀─ events/*.json (solo lectura) ◀───────────┼──── /estado
   └─▶ alertas.jsonl ─▶ coordinador OH-DUO-04 ─▶ decisiones.jsonl   │        │ lee líneas completas
         └─▶ despachador.py ── valida y escribe una línea ─▶ buzon/mensajes.jsonl ──ro──▶ /buzon/mensajes.jsonl
                                                                    │        │ bajo el candado: RUNNING → send_message
                                                                    │        └─▶ /estado/entregas.jsonl
```

| Archivo | Función |
|---|---|
| `ejecutor/correr.sh` | Es el de OH-DUO-01 más el buzón `<resultado>/buzon/` montado `--ro-bind` en `/buzon` y `puente.py` en `/opt/oh/` (solo lectura). El control previo verifica que `/buzon/mensajes.jsonl`, `/buzon/x` y `/opt/oh/puente.py` no se puedan escribir. |
| `ejecutor/agente.py` | Es el de OH-DUO-01 más tres líneas: crea el `Puente`, lo detiene antes de leer los eventos y agrega `intervenciones` a `resultado.json`. |
| `ejecutor/puente.py` | Corre como hilo dentro del agente (detalle abajo). |
| `ejecutar.py` | Corre problemas con `duo.py` de OH-DUO-01 (importado, sin modificarlo) y este ejecutor, en `corridas/`, y crea la marca `.oh-duo-05`. |
| `despachador.py` | Lleva cada decisión de OH-DUO-04 al buzón de su ejecución (detalle abajo). |
| `cadena.py` | En vivo, une observador → coordinador → despachador en un solo proceso. Sin `--enviar` es la corrida de control. |
| `intervenciones.py` | `analizar` una corrida y `comparar` el control con la intervenida (ver abajo). |

El hilo de `puente.py`:
- lee solo líneas completas;
- valida `{id, texto, sha256}`;
- deduplica por `id`;
- toma el candado del estado (el mismo FIFOLock de `run()`), y solo si está `running` llama a `send_message(texto, sender="oh-duo-04:<id>")`;
- registra la primera acción posterior.

El despachador aplica estas reglas:
- el texto tiene que estar formado **solo por frases de `coordinador.json`**; el único dato variable son nombres `test_*`;
- solo despacha `nueva_estrategia`;
- con un margen de al menos 5 iteraciones;
- con `id` único;
- `O_APPEND|O_NOFOLLOW` y sin enlaces en la ruta.

## Condiciones del pedido

| Condición | Dónde se cumple |
|---|---|
| 1. Solo en `oh-duo-05/` | 01–04 se importan sin cambios; `validar.sh` paso 3 lo comprueba por huellas |
| 2. Buzón en solo lectura en bwrap | `correr.sh`: `--ro-bind "$R/buzon" /buzon`; control previo; `test_sandbox_y_buzon_de_solo_lectura` |
| 3. `send_message()` en el mismo `run()` | `puente.py` corre como hilo, sin `pause()` ni un segundo `run()`; `test_entrega_durante_run_sin_cambiar_el_limite` |
| 4. Estado comprobado bajo el candado | `with self.conv.state:` y la comprobación `running`; `test_despues_de_finish_se_descarta_y_el_estado_no_cambia` |
| 5. Deduplicación y registro | id en el despachador (persistente) y en el puente; `entregas.jsonl` registra `entregado`, `descartado`, `duplicado_ignorado`, `rechazado_formato` y `accion_posterior`; `despachos.jsonl` registra cada decisión |
| 6. Presupuesto de 30 | `MAX_ITER` no cambia; `test_presupuesto_original_de_30` (control: 30; intervenida: ≤ 30) |
| 7. Sin archivos protegidos ni solución | ver detalle abajo |
| 8. Integración con sandbox real y modelo simulado | `tests/test_integracion.py` (con `tests/simulado.py`) |
| 9. Comparar con y sin intervención | sección siguiente e `intervenciones.py comparar` |

Detalle de la condición 7:
- el coordinador no tiene acceso de escritura a la instancia; lo único que se escribe es una línea en el buzón, que el agente solo lee;
- el texto se valida contra las plantillas: sin código, sin texto libre, sin ocultas;
- lo cubren `test_rechaza_texto_libre_codigo_o_solucion`, `test_intervenida_mensaje_solo_de_plantillas` y `test_sin_fugas_de_ocultas`.

## Resultado con el modelo simulado (esta rama)

El modelo simulado repite en P1 el bucle de Qwen: corre las pruebas, mira el código, hace una edición sin
efecto y vuelve a probar, sin tocar las pruebas. Solo cambia de conducta si el mensaje del coordinador aparece
en la conversación.

| ejecución | intervenciones (confirmadas) | cambio de conducta | acciones | terminación | estancamiento | visibles | categoría (ocultas) |
|---|---|---|---|---|---|---|---|
| P1__qwen | 1 (1) | sí | 30 → 10–11 (según la latencia) | agotamiento_de_iteraciones → finalizacion_con_finish | sí → no | no → sí | error_infraestructura → correcta |

Secuencia en la intervenida:

| Iteración | Qué pasó |
|---|---|
| 6 | OH-DUO-04 decide `nueva_estrategia` (A2 confirmado) |
| 7 | el despachador escribe en el buzón y el puente lo entrega; el mensaje queda en el evento 16 |
| 8 | primera acción posterior: `file_editor` sobre `intervalos.py` (evento 17) |
| 10 | `finish` |

El texto entregado fue: *"You are repeating the same tests with the same failure. Failing tests:
test_intervalo_contenido. Review your diagnosis: … Do not modify the tests."*

Esto prueba el **mecanismo**: el mensaje llega, se registra, se confirma y respeta el límite. No mide su
eficacia con Qwen, que es lo que falta medir con inferencias reales.

## Cómo comparar una corrida con intervención y otra sin intervención

> Requiere inferencias reales con Qwen: **no se hace en esta fase**. Queda documentado para cuando se autorice.
> Siempre en `oh-duo-05/corridas/`, nunca sobre corridas de OH-DUO-01.

Se usan el mismo problema, el mismo modelo, la misma semilla y los mismos límites. Lo único que cambia es
`--enviar`. Cada corrida necesita dos terminales, desde `~/oh-duo-05`:

```bash
# Control (la cadena decide, pero no envía nada)
python3 cadena.py --corrida corridas/ctrl-p1-s11 --maximo 1                   # terminal A (espera la marca)
python3 ejecutar.py ejecutar --corrida ctrl-p1-s11 --problemas P1 --modelos qwen --semilla 11   # terminal B

# Intervenida (idéntica, con --enviar)
python3 cadena.py --corrida corridas/int-p1-s11 --maximo 1 --enviar
python3 ejecutar.py ejecutar --corrida int-p1-s11 --problemas P1 --modelos qwen --semilla 11

# Pruebas ocultas (solo fuera del sandbox, ya terminadas las dos) y comparación
python3 ejecutar.py verificar --corrida ctrl-p1-s11
python3 ejecutar.py verificar --corrida int-p1-s11
python3 intervenciones.py comparar --control corridas/ctrl-p1-s11 --intervenida corridas/int-p1-s11
# → informes/comparacion-ctrl-p1-s11-vs-int-p1-s11/comparacion.{md,json}
```

Qué mirar, en este orden:
1. **Entrega.** El despacho dice `escrito_en_buzon`, el puente registra `entregado` y el mensaje aparece en la
   conversación con `sender oh-duo-04:<id>`. Si figura `descartado`, la conversación ya no estaba `running`
   (el mensaje llegó tarde).
2. **Recepción.** Hay una `accion_posterior`, que prueba que el modelo respondió a un pedido que ya contenía
   el mensaje.
3. **Conducta.** Se mira si después del mensaje editó el código productivo y si las pruebas aprobaron. También
   cambian las iteraciones, la terminación y el estancamiento de OH-DUO-02.
4. **Resultado.** La categoría con ocultas que da `verificar`.
5. **Límites.** Las iteraciones son ≤ 30 en ambas y los originales quedan intactos.

Cuidados al interpretar:
- **Antes del mensaje**, las dos corridas deberían ser iguales (misma semilla y temperatura 0,2). Si
  divergen antes, la comparación de ese par no aísla el efecto del mensaje.
- **Un par es un caso.** Para concluir hay que repetir con varias semillas (por ejemplo 11, 12 y 13) y con
  P1–P6.
- **La categoría «error_infraestructura» del control** viene de OH-DUO-01, que clasifica
  `MaxIterationsReached` como error del SDK. La terminación correcta (`agotamiento_de_iteraciones`) la da
  OH-DUO-02, y aparece en la misma tabla.

## Limitaciones conocidas

- La latencia es de alrededor de una iteración: el puente solo entra entre pasos, porque `run()` retiene el
  candado durante cada `agent.step`. Por eso el despachador exige un margen mínimo de 5 iteraciones.
- `sender` no llega al LLM. Para el modelo, el mensaje es un mensaje de usuario más; la trazabilidad está en
  los eventos y en `entregas.jsonl`.
- Hay una carrera **preexistente** entre OH-DUO-03 y OH-DUO-04:
  - OH-DUO-03 escribe `supervision.json` sin hacerlo atómico, y OH-DUO-04 puede leerlo a medio escribir;
  - `test_vigilar_en_vivo` de OH-DUO-04 falla aproximadamente 1 de cada 6 veces;
  - no se corrigió para no modificar 01–04;
  - `cadena.py` tolera el caso: las decisiones ya están en `decisiones.jsonl` y solo falta el resumen, que se
    regenera con `coordinador.py simular`;
  - la corrección propuesta es una escritura atómica (temporal + `os.replace`) en `observador.py`, pendiente
    de autorización.
- Por omisión solo se despacha `nueva_estrategia`. Las advertencias de OH-DUO-04 (A3/A5) se envían con
  `--incluir-advertencias`, que todavía no se probó con un modelo real.
