# Smoke test: OpenHands SDK 1.58.1 + Qwen 2.5 Coder 7B (herramientas por prompt)

Pregunta: ¿el motor de OpenHands, sin agregados, logra con Qwen en Pulenta correr pruebas, ver un fallo,
corregirlo y terminar solo? Es independiente de `constructor-ia`: no importa nada del proyecto ni toca sus
servicios, modelos, laboratorios o reglas de red. Para revertir: `rm -rf ~/oh-smoke`.

## Por qué `native_tool_calling=False`

Ollama no convierte en `tool_calls` el JSON que Qwen 2.5 Coder escribe como texto (lo comprobaron en Pulenta).
Con `native_tool_calling=False`, el SDK no manda `tools` a Ollama: describe las herramientas en el prompt
(con un ejemplo), pide el formato `<function=terminal><parameter=command>…</parameter></function>`, corta
con `stop=["</function"]` y lo convierte en llamadas (`openhands/sdk/llm/mixins/fn_call_converter.py`).
Si la llamada está mal formada, el SDK se lo devuelve al modelo como mensaje para que la corrija.
**Límite:** si el modelo responde con texto sin `<function=`, por ejemplo con el JSON de antes, el SDK lo
toma como respuesta final y termina la conversación (`response_dispatch.py`). El smoke test lo registra en
`mensajes_de_texto_del_agente`; no lo corregimos.

## Aislamiento

```
VivoBook                                      sandbox (bwrap --unshare-all, sin red, sin /home, entorno vacío)
relay.py host  ◄── /tmp/oh-smoke.*/ollama.sock ──►  127.0.0.1:11435 (hilo en smoke.py)
   │ solo GET version/tags/ps, POST show/chat,         OpenHands: agente + terminal + editor
   │ solo el modelo indicado, cuerpo ≤ 4 MiB           /work, /estado, /sock y /tmp (tmpfs): escribibles
   ▼
http://100.92.153.59:11435 (Pulenta)
```

- `systemd-run --user --scope`: MemoryMax 2G, sin swap, TasksMax 256, CPUQuota 200 %.
- **Rutas escribibles para el agente:** solo `resultados/<corrida>/work` (copia de `tarea/`),
  `resultados/<corrida>/estado`, el directorio temporal del socket y un `/tmp` en memoria. `tarea/`, `ocultas/`,
  `control/`, el proyecto, `/home` y los servicios no se montan. `venv/`, `smoke.py` y `relay.py` van en solo
  lectura. `correr.sh` verifica todo esto en el control previo y compara al final las huellas de los originales.
- **Evaluación fuera del sandbox del agente:** al terminar, `correr.sh` copia solo `duracion.py` (si es un
  archivo regular, sin seguir enlaces) junto con la prueba visible original, y corre las visibles y las ocultas
  en otro sandbox sin red, en solo lectura. Las ocultas nunca entran al sandbox del agente.
- **Control previo, sin usar la GPU:** antes del agente, la misma evaluación tiene que reprobar el `duracion.py`
  original y aprobar `control/duracion.py`, sin errores de importación, y el sandbox tiene que pasar sus
  comprobaciones. Si algo falla, la corrida se detiene antes de llamar a Ollama.
- **Plazos:** a los `MAX_SEG` (900 s) se pide pausa al agente. Con `GRACIA` (60 s) más sin cerrar (pedido
  bloqueado: hasta 2 × 300 s más la espera, o un comando largo), `smoke.py` registra lo que haya y sale. El
  corte externo es `MAX_SEG + GRACIA + 180` s. Al salir se cierra el puente, y con él cualquier pedido en curso.
- `num_ctx` viaja en cada pedido (16384) y el puente lo registra en `puente.jsonl`.

### Lo que el puente NO aísla

El puente impide conectarse a cualquier otro destino, pero **no es aislamiento absoluto de información**.
Cualquier proceso del sandbox, también un comando que escriba el agente, puede mandar texto a Ollama dentro de
un pedido a `/api/chat` (por ejemplo el contenido de `/work`) y leer la respuesta. Ese texto llega a Pulenta y
puede quedar en sus registros. También puede consultar `/api/tags`, `/api/ps` y `/api/show`, y pedir
inferencias al modelo permitido con las opciones que quiera, o sea consumir GPU y tiempo de Pulenta. En este
sandbox solo hay datos sintéticos.

### Riesgos que quedan (aceptados para este smoke test)

- Los criterios 1 y 2 se leen de los eventos del SDK dentro del mismo sandbox y con el mismo usuario que los
  comandos del agente, que en teoría podría alterar `/estado`. Los criterios 3 a 6 se calculan afuera.
- En la evaluación, la solución del agente corre en el mismo proceso que las pruebas ocultas (no hay un arnés
  separado con resultado firmado como en `constructor-ia`).
- No hay filtro seccomp propio (sí namespaces, `--cap-drop ALL`, `no_new_privs` de bwrap y límites de cgroup).

## Instalación (sin sudo, nada global)

```bash
python3 -c "import ensurepip" && echo ensurepip-ok          # si falla: ver «Sin ensurepip» abajo
cd ~/oh-smoke
python3.12 -m venv venv
venv/bin/pip install --require-hashes --no-deps -r requisitos.lock -r requisitos-tools.lock
venv/bin/python -I -c "import openhands.sdk, openhands.tools.terminal, openhands.tools.file_editor; print('OK')"
```

`requisitos.lock` fija 141 paquetes con su hash SHA-256 (unos 350–500 MB en `venv/`). Todos son wheels menos
`func-timeout` 4.3.5, que pip construye desde el sdist con setuptools (revisado: su `setup.py` solo llama a
`setup()`). `openhands-tools` va sin dependencias, así que no se instalan browser-use, posthog ni los
clientes de Google.

**Sin ensurepip** (falta el paquete `python3.12-venv` y no queremos apt): `python3.12 -m venv --without-pip venv`
y después `venv/bin/python pip.pyz install …`, con `pip.pyz` descargado en `~/oh-smoke`. Se decide solo si hace falta.

## Correr

```bash
cd ~/oh-smoke && chmod +x correr.sh
./correr.sh 1        # semilla 1; repetir con 2 y 3
```

Variables opcionales: `OLLAMA_URL` (por omisión `http://100.92.153.59:11435`), `MODELO`, `NUM_CTX` (16384),
`MAX_ITER` (30), `MAX_SEG` (900). Cada corrida queda en `resultados/<fecha>-s<semilla>/`:
`resultado_final.json`, `estado/eventos.json` (todo lo que hizo el agente), `puente.jsonl`, `cambios.diff`,
`eval.visibles.txt`, `eval.ocultas.txt`, `control/` (control previo), `api_ps.json` y `agente.log`.

## Criterios (fijados antes de correr)

1. Al menos una llamada válida a una herramienta distinta de `finish`.
2. Corrió las pruebas y vio el fallo antes de editar.
3. Cambió `duracion.py` y no `test_duracion.py`.
4. Las pruebas visibles pasan al final y terminó con `finish`.
5. Las pruebas ocultas pasan (detectan, por ejemplo, si escribió a mano el valor de `"1h30m"`).
6. `num_ctx` ≥ 16384 en cada pedido a `/api/chat`.
