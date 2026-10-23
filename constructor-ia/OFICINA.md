# Oficina de agentes distribuidos

Roles, computadoras y misiones como entidades independientes. El diseño aprobado está en `AGENTE_DATOS.md` §15 y en
la propuesta técnica del 2026-10-23. Se implementa por etapas; cada una tiene sus pruebas y no altera el laboratorio.

**Decisiones del MVP (Diego y ChatGPT):**
- Tailscale con ACL restrictiva y comprobación de identidad, sin token adicional.
- Un turno de inferencia simultáneo por trabajador.
- Primera microtarea real: revisión de Python, con verificación independiente.

## Etapa 1: registro de trabajadores y disponibilidad (implementada, sin inferencias)

### Qué se reutiliza (verificado al releer el código)

| Pieza | Uso |
|---|---|
| `ollama.OllamaClient` | Sin cambios. `version()`, `tags()` y `loaded()` (`/api/ps`) para comprobar disponibilidad. Habla HTTP, así que Tailscale Serve tiene que exponer HTTP o TCP |
| `db.Database` | Conexión SQLite (WAL, una conexión por hilo). La oficina usa su propio esquema en su propia base; **no agrega migraciones** a la base de la aplicación |
| `ollama.InferenceGate` | Hoy es global por proceso. Se separa por destino en la etapa 3, conservando el comportamiento actual por omisión |
| `missions.py`, `workers.py` | Atados a la aplicación web (una misión pesada a la vez). Sirven como patrón de estados y de cola para la etapa 2, no para reutilizarlos directamente |
| `agente_datos/qwen.py` | Su interfaz `pedir()` es la que implementará el adaptador remoto en la etapa 3 |

### Qué funciona

- **`constructor_ia/oficina/registro.py`**, con una base propia en `<datos>/oficina/oficina.db`.
- **Registro de un trabajador:**
  - URL `http://` del tailnet (IP 100.64.0.0/10 o nombre `*.ts.net`) con puerto explícito, sin ruta ni credenciales.
    Loopback solo para pruebas (`--permitir-local`), y nunca el puerto 11434 del Ollama principal;
  - identidad comprobada con `tailscale whois --json` (solo lectura) contra el nodo esperado (`--nodo`);
  - modelos declarados y recursos (CPU, RAM, GPU, VRAM);
  - capacidad fija en 1 y permisos fijos en `inferencia`.
- **Disponibilidad (solo lectura):**
  - vuelve a comprobar la identidad y después lee `/api/version`, `/api/tags` y `/api/ps`, sin inferencias;
  - un trabajador está «disponible» si responde, sigue siendo el mismo nodo y tiene todos sus modelos declarados;
  - el latido vence a los 120 s de la última comprobación exitosa;
  - cada comprobación queda registrada, con latencia, versión, modelos, modelos cargados, faltantes, identidad y
    motivo.
- **Retiro:** el trabajador sale del registro y su historial se conserva.
- **CLI:**

```bash
constructor-ia oficina registrar --id pulenta --url http://<IP o nombre .ts.net>:11435 --modelo gemma4:12b \
    --nodo <nombre del nodo en Tailscale> --cpu "Core Ultra 9 185H" --ram-gb 32 --gpu "RTX 4070" --vram-gb 8
constructor-ia oficina estado            # todos (código 3 si alguno no está disponible); --id X; --json
constructor-ia oficina retirar --id pulenta
```

Pruebas: `tests/test_oficina.py` (14), con un Ollama falso y un `whois` simulado. Cubren:
- **URL:** solo tailnet y puerto explícito, loopback solo en pruebas y nunca 11434;
- **Identidad:** registro con identidad comprobada; identidad distinta o nodo faltante = rechazo; identidad que
  cambia = no disponible, sin hablarle;
- **Registro:** duplicados e id inválidos rechazados; retiro con historial conservado;
- **Disponibilidad:** solo lee (ningún POST), modelo faltante = no disponible, desconexión = no disponible, latido
  vencido = no disponible ahora;
- **CLI:** registrar, estado, retirar, errores sin traza;
- **Aislamiento:** la oficina solo crea su propia base.

Suite completa con esta etapa: 521 pruebas, OK (670 s).

### Qué hacen ustedes a mano (la oficina no configura Tailscale)

1. **En la notebook:** Ollama escucha solo en `127.0.0.1`, y Tailscale Serve lo publica en el puerto 11435 dentro del
   tailnet, en HTTP o TCP. **Sin Funnel.**
2. **ACL:** solo la VivoBook (por ejemplo, `tag:coordinador`) llega a `tag:trabajador:11435`. Ollama no tiene
   autenticación ni permisos por ruta: la ACL es la única barrera para `/api/pull` o `/api/delete`.
3. **En la VivoBook:**
   - `constructor-ia oficina registrar …`;
   - `constructor-ia oficina estado` (no hace inferencias; la notebook solo ve tres lecturas).

### Lo que falta (siguientes etapas)

2. Cola persistente de tareas: estados, arriendos, dependencias, intentos, vencimientos y desconexión.
3. Adaptador remoto con la interfaz `pedir()` y solo rutas de inferencia, candado por destino, y planificador por
   modelo, turno libre, permisos y RAM de la VivoBook (umbral de 3.900 MiB: si no alcanza, remoto o pendiente).
4. Coordinador concurrente con un escritor único y propuestas verificadas centralmente.
5. Prueba de punta a punta con dos misiones simultáneas (Ollama falsos con latencia), más la suite completa.
6. Procedimiento manual con la notebook: revisión de Python con verificación independiente (la corren ustedes).

**Fuera del MVP:** memoria por rol aplicada al agente de datos, misiones del agente repartidas entre trabajadores y
ejecución remota de código, que requiere aislamiento propio.
