# Fase 1: compatibilidad de Gemma 12B con OpenHands

Comprueba, **sin correr el laboratorio**, si `gemma4:12b` puede programar con OpenHands SDK 1.58.1 en las
mismas condiciones que Qwen. No usa ni modifica `oh-smoke`, `oh-supervisor`, `oh-lab`, los servicios ni la
configuración de Ollama. Solo biblioteca estándar, salvo la clasificación, que usa el SDK del `venv` de
`oh-smoke` dentro de un sandbox sin red.

## Qué hace

1. **Inventario (solo lectura):** `/api/version`, `/api/tags`, `/api/show` de Gemma y de Qwen (capacidades,
   cuantización, tamaño, contexto máximo, arquitectura, si la plantilla soporta herramientas) y `/api/ps`.
2. **Seis pedidos reales de OpenHands a Gemma:** `pedidos/paso1.json` (pedido inicial) y `pedidos/paso2.json`
   (después de correr las pruebas, con la salida real del fallo), cada uno con las semillas 11, 12 y 13.
   - **Origen:** mensajes y opciones idénticos a los del SDK (modo por prompt, `stop: ["</function"]`,
     `num_ctx` 16384, `temperature` 0.2, `num_predict` 2048, `think: false`, sin `tools`), capturados con el
     sandbox real y la misión C01 del banco de desarrollo.
   - **Plazo:** 300 s por pedido, el mismo que el agente.
   - **Si Ollama rechaza `think: false`:** se registra y se repite sin ese campo.
3. **Medidas:**
   - carga (`load_duration`) y VRAM (`size_vram` frente a `size`, en `/api/ps`, justo después de cargar);
   - tokens del prompt (para detectar si el contexto se trunca), velocidad de prompt y de generación,
     segundos por pedido y `done_reason`.
4. **Vuelta a Qwen** (salvo `--sin-restaurar`): un pedido de 1 token para medir el cambio de modelo y dejar
   Pulenta con Qwen cargado, como estaba.
5. **Clasificación** (`clasificar.sh`): pasa cada respuesta por el **convertidor real** de OpenHands
   (`fn_call_converter`) y la clasifica igual que el SDK durante una corrida:
   - `llamada`;
   - `error_de_formato` (el SDK se lo devuelve al modelo);
   - `texto_sin_herramienta` (el SDK termina la conversación).

**Efecto sobre Pulenta:** carga Gemma (Ollama desaloja el modelo que haya) y al final vuelve a cargar Qwen.
No cambia la configuración ni descarga, crea o borra modelos. Rutas usadas: las mismas que permite
`relay.py`. Duración: entre 1 y 35 minutos, según la velocidad de Gemma (7 pedidos con un tope de 300 s).

## Comandos

**En Pulenta (PowerShell, solo lectura), antes:**

```powershell
nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv
ollama ps
```

**En la VivoBook:**

```bash
git clone --depth 1 --branch claude/zealous-cray-jqmao5 https://github.com/dieguinPinga/dieguinPinga.git /tmp/oh-src
cp -r /tmp/oh-src/oh-gemma-fase1 ~/oh-gemma-fase1 && rm -rf /tmp/oh-src
cd ~/oh-gemma-fase1 && chmod +x clasificar.sh
python3 medir.py                                    # --ollama http://100.92.153.59:11435 por omisión
./clasificar.sh resultados/<fecha>                  # escribe resultados/<fecha>/dictamen.json
```

**En Pulenta, mientras corre `medir.py`** (cuando aparezca el primer «paso 1 semilla 11»), y otra vez al final:

```powershell
nvidia-smi --query-gpu=memory.used --format=csv
ollama ps
```

Pasame `resultados/<fecha>/resultados.json`, `dictamen.json` y las salidas de `nvidia-smi` y `ollama ps`.

## Criterios del dictamen (fijados antes de medir)

| Criterio | Condición |
|---|---|
| C1 | `capabilities` incluye `completion` y el contexto máximo es ≥ 16384 |
| C2 | Los pedidos con las opciones del SDK responden HTTP 200 (C2b: también con `think: false`) |
| C3 | Ningún pedido llega a los 300 s |
| C4 | Las 6 respuestas son llamadas válidas `<function=…>` (C4b: al menos 5 de 6) |
| C4c | Al menos 5 de 6 usan una herramienta plausible: `terminal` en el paso 1; `file_editor` o `terminal` en el paso 2 |
| C5 | Sin truncado: tokens de prompt < `num_ctx` y `done_reason == "stop"` |

- **`preparada`:** C1, C2, C3, C4, C4c y C5.
- **`preparada_con_riesgos`:** C1, C3, C4b y C5.
- **`no_preparada`:** cualquier otro caso.
- **Si falla C2b:** se marca que hace falta no enviar `think` (`reasoning_effort=None`). Es un cambio de
  configuración del agente que se decide aparte.

**Límites de esta prueba:** 6 respuestas en 2 pasos de **una** misión no demuestran que Gemma complete una
misión. Solo muestran si respeta el formato y si entra en los límites. Ese es el objetivo del intento real
que se autorizaría después. La fracción en VRAM y las velocidades dependen de lo que haya en Pulenta en ese
momento.

## Verificación hecha acá (sin GPU)

El kit se probó contra un Ollama simulado con tres comportamientos, con `bwrap` real sin privilegios:

| Comportamiento simulado | Dictamen |
|---|---|
| Formato correcto | `preparada` |
| Respuestas `tool_code` al estilo Gemma | `no_preparada`, con `texto_sin_herramienta` |
| Rechazo de `think: false` | `preparada (requiere no enviar think…)` |
