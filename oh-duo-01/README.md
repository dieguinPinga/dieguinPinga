# OH-DUO-01: dos programadores, los mismos problemas

¿Dos LLM con métodos distintos aportan diversidad útil? Cada uno de los 6 problemas lo resuelven, por
separado, los dos programadores:

| Programador | Modelo | Rol (`roles/`) |
|---|---|---|
| A | `qwen2.5-coder:7b` | corrección mínima: el defecto causal y la edición más chica posible |
| B | `gemma4:12b` | invariantes: reconstruir el contrato y corregir sus contradicciones |

Los dos reciben el mismo código, las mismas pruebas visibles y el mismo pedido base. El rol se agrega al
final y cambia el método, no la información (48 palabras cada uno). No se clasifican problemas ni se asignan
modelos: cada modelo enfrenta los 6.

No modifica `oh-smoke`, `oh-supervisor`, `oh-lab`, servicios ni la configuración de Ollama. Solo biblioteca
estándar.

## Qué se reutiliza

| De | Qué | Cómo |
|---|---|---|
| `oh-smoke` | `venv/`, `relay.py`, `requisitos*.lock` | enlace en solo lectura y copias en cada instancia |
| `oh-lab/ejecutor` | `correr.sh`, `agente.py` | copias en `ejecutor/`: se **quitaron** las ocultas y la referencia y se agregó el rol (ver abajo) |
| `oh-supervisor` | `ejecutar` (grupo de procesos con plazo) y `otro_correr_activo` | importado sin modificar |
| `oh-lab` | el método de validación del banco | `tests/test_banco.py` |

Cambios del ejecutor respecto de `oh-lab/ejecutor`:

- **`correr.sh`:** sin montaje, evaluación ni control previo de ocultas y referencia; la instancia no las
  tiene. El control previo solo comprueba el sandbox y que el original repruebe la visible. Monta
  `enfoque.txt` en solo lectura.
- **`agente.py`:** las únicas sentencias nuevas son `ENFOQUE = …` y `TAREA = f"{TAREA}\n\n{ENFOQUE}"`
  (lo verifica una prueba que compara el AST).
- Sandbox, puente, límites (30 iteraciones, 900 s + 60 s, 2 GB) y lectura de eventos son los mismos para los
  dos modelos. El modelo llega por la variable `MODELO` que ya usaba `correr.sh`.

## Banco (`banco/P1–P6`), inédito

| ID | Programa | Defecto |
|---|---|---|
| P1 | `unir_intervalos` | un intervalo contenido acorta al anterior (toma su fin en lugar del máximo) |
| P2 | `Inventario` | un retiro fallido de un producto desconocido lo registra con stock 0 |
| P3 | `paginar` | las páginas se redondean hacia abajo; la última incompleta queda fuera de rango |
| P4 | `codificar`/`decodificar` (RLE) | al decodificar, cada dígito reemplaza la cantidad (fallan las de dos cifras) |
| P5 | `tabla` de posiciones | los goles en contra del visitante usan sus propios goles |
| P6 | `luhn_valido` | recorre los dígitos desde la izquierda (falla con largos pares) |

Cada problema trae `tarea/` (código defectuoso y prueba visible), `ocultas/test_ocultas.py` y
`referencia/`. `tests/test_banco.py` comprueba:

- que cada original repruebe las visibles y las ocultas sin errores de importación, y que cada referencia
  apruebe las dos;
- que el defecto sea una sola zona de 1 o 2 líneas;
- que las pruebas visibles y las ocultas no compartan casos;
- que no se repitan módulos ni funciones del banco de `oh-lab`.

**Advertencia:** los problemas, las pruebas y los roles los escribió Claude. Según lo acordado (el banco de
confirmación lo genera alguien que no diseña los prompts), **OH-DUO-01 es exploratorio**: sirve para probar el
sistema dual y ver tendencias, no para confirmar diferencias entre modelos.

## Aislamiento de las ocultas

1. **`ejecutar` no las lee.** La instancia de cada ejecución tiene solo `tarea/`, `mision.json` (id y módulo),
   el rol y los scripts. Una prueba intercepta `open`/`os.open` durante `ejecutar` y comprueba que no se abra
   nada de `ocultas/` ni de `referencia/`.
2. **Congelado antes de evaluar.** Al terminar cada ejecución se copian a `final/` los archivos regulares de
   la copia de trabajo (sin seguir enlaces, también si el SDK falló) y los registros a `logs/`, con un
   `manifiesto.json` sha256, en solo lectura.
3. **`verificar` es otro proceso.**
   - Exige que todo esté congelado y verifica cada manifiesto.
   - Sella `congelado.json` y recién después corre visibles y ocultas sobre `final/`, en un sandbox `bwrap`
     sin red.
   - Escribe solo en `verificacion/`.
   - Después de verificar, la corrida **no admite más ejecuciones**: nada de lo verificado puede volver a
     un agente.
4. **Auditoría de fugas.** Busca los nombres de prueba y los textos propios de las ocultas en todos los
   archivos finales y registros de los agentes, y la cantidad de hallazgos se informa.
5. **Sin selección.** Ningún paso elige ganadores ni cambia prompts. La cobertura conjunta es un cálculo
   retrospectivo.

## Clasificación de cada ejecución

El estado de la ejecución sale solo de datos visibles; la evaluación sale del verificador.

| Estado | Pasa visibles y ocultas | No pasa |
|---|---|---|
| finalizada (`finish`) | **correcta** | **incorrecta** |
| incompleta (bucle, tiempo, texto sin herramienta, corte) | **potencialmente correcta sin finalizar** | **incompleta** |
| error de infraestructura o SDK (excepción del SDK, control previo, puente, plazo del orquestador) | **potencialmente correcta sin finalizar** | **error de infraestructura** |

## Salidas (`corridas/<nombre>/`)

- `resultados.json`: configuración y huellas, cada ejecución (estado, motivo, tiempos, llamadas, tokens,
  modelo cargado antes, cambio de modelo), métricas y auditoría.
- `matriz.csv`: una fila por problema y modelo.
- `informe.md`: matriz, métricas por modelo, comparación y advertencias.
- `ejecuciones/<P>__<modelo>/`: `final/` (archivos finales), `logs/` (eventos y llamadas a herramientas,
  `agente.log`, `puente.jsonl`, visibles), `correr.log`, `ejecucion.json`, `manifiesto.json` e `instancia/`
  completa.
- `verificacion/`: la salida de las ocultas, y solo acá.

**Métricas:** tasa de resolución por modelo; resueltos por ambos, solo por Qwen, solo por Gemma y no
resueltos; tiempo total y medio; llamadas y tokens (los tokenizadores son distintos, así que conviene
comparar tiempos); cambios de modelo (según `/api/ps` antes de cada ejecución); y **cobertura conjunta
teórica**, marcada como oráculo retrospectivo y no como capacidad del sistema.

## Uso

Requiere `~/oh-smoke` (con su `venv`) y `~/oh-supervisor`.

```bash
git clone --depth 1 --branch claude/zealous-cray-jqmao5 https://github.com/dieguinPinga/dieguinPinga.git /tmp/oh-src
cp -r /tmp/oh-src/oh-duo-01 ~/oh-duo-01 && rm -rf /tmp/oh-src
cd ~/oh-duo-01
python3 -m unittest discover -s tests -v          # 30 pruebas, sin GPU
```

**Ensayo pequeño** (1 problema × 2 modelos, unos 2 × 20 min como máximo):

```bash
python3 duo.py ejecutar  --corrida ensayo-p1 --problemas P1
python3 duo.py verificar --corrida ensayo-p1
less corridas/ensayo-p1/informe.md
```

**Batería completa** (12 ejecuciones; solo cuando la autoricen, y en una corrida **nueva**, porque una
corrida verificada queda cerrada):

```bash
python3 duo.py ejecutar  --corrida oh-duo-01 [--orden alterno|bloque] [--semilla 11]
python3 duo.py verificar --corrida oh-duo-01
```

- **Orden:** `alterno` (por omisión) intercala el orden por problema (P1 Qwen→Gemma, P2 Gemma→Qwen…). Implica
  hasta 11 cambios de modelo. `bloque` corre los 6 problemas con un modelo y después con el otro (1 cambio).
- **Reanudación:** si se corta, `ejecutar` con el mismo nombre retoma sin repetir lo hecho. La instancia
  cortada se conserva como `instancia.abortada-*`.
- **Configuración:** misma semilla para los dos modelos y la misma configuración en toda la corrida; si
  cambia, se rechaza.

## Verificación hecha acá (sin GPU)

- **30 pruebas:** 9 del banco, los roles y el ejecutor; 21 del orquestador con el ejecutor simulado y el
  supervisor real. Cubren:
  - las cinco categorías y sus motivos, y las métricas con cobertura teórica;
  - que la entrada sea idéntica para los dos modelos y el rol distinto;
  - instancias sin ocultas y que `ejecutar` no las lea;
  - el congelado alterado, que verificar exija todo congelado y que no se ejecute después de verificar;
  - enlaces no seguidos, auditoría de fugas, plazo del orquestador, reanudación, candado, configuración
    fija y cambio de modelo.
- **Punta a punta con la cadena real**, como usuario sin privilegios: `bubblewrap` 0.9.0 de Ubuntu 24.04,
  `relay.py` con la lista blanca de cada modelo y el verificador con `bwrap`, con un Ollama falso que repara
  con la referencia y responde JSON en texto en P3 (Qwen) y P2 (Gemma).
  - **Resultado:** 12 ejecuciones; matriz con P2 resuelto solo por Qwen y P3 solo por Gemma; 0 fugas.
  - **Rol:** cada pedido llegó con su rol.
  - **Alcance:** esto prueba la cadena; **no** dice nada sobre los modelos.
