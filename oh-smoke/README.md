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
   │ solo el modelo indicado, cuerpo ≤ 4 MiB           /work = copia de tarea/ (único lugar escribible)
   ▼
http://100.92.153.59:11435 (Pulenta)
```

- `systemd-run --user --scope`: MemoryMax 2G, sin swap, TasksMax 256, CPUQuota 200 %.
- Las pruebas ocultas (`ocultas/`) nunca entran al sandbox del agente; corren después en otro sandbox sin red,
  con el resultado montado en solo lectura.
- `num_ctx` viaja en cada pedido (16384) y el puente lo registra en `puente.jsonl`.

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
`visibles.txt`, `ocultas.txt`, `api_ps.json` y `agente.log`.

## Criterios (fijados antes de correr)

1. Al menos una llamada válida a una herramienta distinta de `finish`.
2. Corrió las pruebas y vio el fallo antes de editar.
3. Cambió `duracion.py` y no `test_duracion.py`.
4. Las pruebas visibles pasan al final y terminó con `finish`.
5. Las pruebas ocultas pasan (detectan, por ejemplo, si escribió a mano el valor de `"1h30m"`).
6. `num_ctx` ≥ 16384 en cada pedido a `/api/chat`.
