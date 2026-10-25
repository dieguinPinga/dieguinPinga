# oh-supervisor: intentos independientes sobre oh-smoke

Ejecuta `oh-smoke/correr.sh` hasta 3 veces, **de a una**, con semillas distintas, y se detiene en el primer
intento que cumple los 6 criterios de éxito (con los originales intactos) o cuando se agotan las semillas.
No agrega otro LLM, no cambia OpenHands, `smoke.py`, `correr.sh`, el sandbox ni sus límites de memoria y
tiempo, y no instala servicios. Es un único archivo en Python, solo biblioteca estándar.

## Cómo se garantiza que cada intento sea independiente

| Garantía | Quién la da | Cómo la verifica el supervisor |
|---|---|---|
| Código de partida limpio | `correr.sh` crea `resultados/<fecha>-s<semilla>/` nueva y copia `tarea/` ahí | La carpeta informada tiene que ser **nueva** (no estaba antes del intento), estar en `resultados/` y terminar en `-s<semilla>`. Su `huellas_inicio.txt` tiene que coincidir con las huellas de `tarea/` que el supervisor tomó al empezar. Si no, se detiene todo. |
| Nada se arrastra entre intentos | Cada intento es un proceso `correr.sh` nuevo: sandbox nuevo, puente nuevo y conversación de OpenHands nueva (`persistence_dir` dentro de la carpeta del intento) | El supervisor no le pasa nada al agente salvo la semilla como argumento: no hay feedback, ni historial, ni resultados de intentos anteriores. Hereda el entorno tal cual. |
| Originales intactos | `correr.sh` no monta `tarea/`, `ocultas/` ni `control/` en el sandbox del agente | Antes y después de cada intento compara el sha256 de `tarea/`, `ocultas/`, `control/`, `smoke.py`, `relay.py`, `correr.sh` y los `requisitos*.lock`. Ante cualquier cambio, o si `correr.sh` informa `ALTERADOS`, se detiene con `integridad` y ese intento no cuenta como éxito. |
| Pruebas ocultas reservadas | Solo las usa la evaluación de `correr.sh`, en otro sandbox | El supervisor no lee `ocultas/` (solo su huella) y sus registros quedan en `oh-supervisor/registros/`, que no se monta en ningún sandbox. |
| Sin ejecuciones simultáneas | — | Candado (`flock`) en `registros/.candado`: un supervisor a la vez. Antes de cada intento busca en `/proc` cualquier `correr.sh` en ejecución, también uno lanzado a mano, y si lo encuentra se detiene. Los intentos son secuenciales. |
| Sin restos de procesos | — | Cada `correr.sh` corre en su propio grupo de procesos. Ante Ctrl-C o el plazo de seguridad del supervisor (3600 s, por encima del peor caso de `correr.sh`, unos 1600 s) se mata el grupo entero. |

Las semillas tienen que ser distintas, y si se repiten no se corre nada. Las semillas cambian el muestreo del
modelo, no el código de partida.

## Cuándo se detiene

- `exito`: un intento cumple los criterios 1 a 6 de `resultado_final.json` y `originales_intactos == "ok"`.
  Cinco de seis no alcanza.
- `intentos_agotados`: se usaron todas las semillas sin éxito. Un control previo fallido de `correr.sh`
  (sin GPU) cuenta como intento fallido, con motivo `control_previo`.
- `integridad`: excepción de seguridad. Si cambió un original, si la carpeta del intento no es nueva o no
  partió de los originales, o si hay otro `correr.sh` corriendo, se detiene sin seguir intentando.
- `interrumpido`: Ctrl-C. Igual se escribe el resumen.

## Motivos de fallo registrados

`stuck`, `texto_sin_herramienta` (el modelo respondió texto sin `<function=…>`), `termino_sin_finish`, `tiempo`,
`corte_forzado`, `corte_externo`, `error_sdk`, `agente_sin_resultado`, `control_previo`, `puente`,
`timeout_supervisor` y `criterios (<estado>)`. En todos los casos se registra además la lista de
`criterios_fallidos`.

## Instalación

No hace falta instalar nada: usa el Python 3.12 del sistema y la instalación existente de `~/oh-smoke`.

```bash
git clone --depth 1 --branch claude/zealous-cray-jqmao5 https://github.com/dieguinPinga/dieguinPinga.git /tmp/oh-src
cp -r /tmp/oh-src/oh-supervisor ~/oh-supervisor && rm -rf /tmp/oh-src
```

`~/oh-smoke` no se toca: el supervisor lo usa tal como quedó.

## Pruebas (sin Ollama, sin bwrap)

```bash
cd ~/oh-supervisor && python3 -m unittest discover -s tests -v
```

Usan `tests/falso_correr.py`, un doble de `correr.sh` con el mismo contrato (carpeta nueva, copia de `tarea/`,
`huellas_inicio.txt`, `resultado_final.json` y línea `carpeta:`), y escenarios por semilla: éxito, stuck,
texto sin herramienta, ocultas reprobadas, corte, control previo fallido, originales alterados, carpeta
reusada, copia no limpia y proceso colgado. Todo corre en un directorio temporal.

## Ejecución real

```bash
cd ~/oh-supervisor
python3 supervisor.py --oh-smoke ~/oh-smoke --semillas 11,12,13
```

- `--semillas`: de 1 a 3 semillas distintas. Su cantidad es el máximo de intentos.
- Sale con código 0 si hubo éxito y con 1 si no.

Registro en `registros/<fecha>/`:

- `resumen.json`: configuración, huellas de los originales, cada intento (semilla, inicio, fin, segundos,
  salida de `correr.sh`, carpeta, motivo, criterios, llamadas, tokens, acciones) y motivo de detención.
- `intento-N.log`: salida completa de `correr.sh`.
- `intento-N.resultado_final.json`: copia del resultado de ese intento.

La carpeta completa de cada intento (eventos del agente, `puente.jsonl`, `cambios.diff`, evaluación) queda en
`~/oh-smoke/resultados/`, como en las corridas manuales. El supervisor no borra nada.
