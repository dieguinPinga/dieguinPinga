# Arquitectura distribuida actual y requisitos para crecer

## Responsabilidades (sin cambios)

| Equipo | Rol | Qué corre |
|---|---|---|
| **VivoBook** (Linux Mint 22.3) | coordinación, ejecución y evaluación | `lab.py` → `supervisor.py` → `correr.sh` → sandbox bwrap con OpenHands (agente, terminal, editor) → evaluación en otro sandbox. Guarda instancias, resultados e informes. |
| **Pulenta** (Windows, RTX 4070) | solo inferencia | Ollama en `100.92.153.59:11435` con `qwen2.5-coder:7b`. No ejecuta código, no ve el banco ni las pruebas y no guarda resultados del laboratorio. |

## Cómo se comunican

```
VivoBook                                                                     Pulenta
lab.py ─ supervisor.py ─ correr.sh
                           │ bwrap (sin red)
                           │   OpenHands → 127.0.0.1:11435 ─┐
                           │                                 │ socket Unix
                           └─ relay.py (anfitrión) ◄─────────┘
                                 │ HTTP/1.1, una conexión por pedido, Connection: close
                                 │ solo GET /api/version|tags|ps, POST /api/show|chat, solo qwen2.5-coder:7b
                                 └──── Tailscale (WireGuard) ────►  Ollama :11435
```

- **Un único canal:** HTTP sin TLS sobre Tailscale (el túnel WireGuard cifra el tráfico). Lo abre `relay.py`
  en la VivoBook, no el sandbox.
- **Qué viaja a Pulenta:** el historial de la conversación de cada intento (pedido, salidas de la terminal y
  contenido de los archivos de la misión) y las opciones (`num_ctx`, `temperature`, `seed`, `stop`,
  `num_predict`). No viajan las pruebas ocultas, la referencia ni los informes.
- **Qué vuelve:** el texto que genera el modelo y los contadores de tokens.
- **Límite conocido:** el puente no es aislamiento absoluto de información. Un proceso del sandbox puede
  mandar contenido a Ollama dentro de `/api/chat` (ver `oh-smoke/README.md`).
- **Un pedido a la vez:** el laboratorio es secuencial, así que Pulenta recibe una sola conversación por vez.
  No se tocó su configuración (contexto, paralelismo ni modelos).

## Requisitos para incorporar más computadoras (no implementado)

### Más nodos de inferencia (otra GPU con Ollama)

1. **Mismo modelo, de verdad:** el mismo nombre y el mismo *digest* (`/api/tags` → `digest`), la misma
   cuantización (Q4_K_M) y un `num_ctx` efectivo de al menos 16384 con la VRAM disponible. Si no, los
   resultados no son comparables.
2. **Red:** el nodo en el tailnet, con una ACL que solo permita el puerto de Ollama desde los coordinadores.
   `relay.py` acepta un destino por proceso, así que cada intento tendría que fijar el suyo.
3. **Registro de procedencia:** anotar en cada intento qué nodo atendió y la versión de Ollama
   (`/api/version`). Hoy el informe asume un único destino.
4. **Exclusión por nodo:** un intento por GPU a la vez (hoy lo garantiza el carácter secuencial del
   laboratorio). `constructor-ia/oficina` ya tiene un registro de trabajadores con latido y un candado por
   destino (`remoto.py`), que serviría de referencia.

### Más nodos de ejecución (otra máquina que corra el sandbox)

1. Linux con `bwrap` y `systemd-run --user`, Python 3.12, y el mismo `venv` instalado con
   `requisitos.lock` (los mismos hashes).
2. El mismo banco, ejecutor y supervisor, verificados por huella (el informe ya guarda `huellas`).
3. Devolver los resultados al coordinador sin perder nada (por ejemplo, empaquetar `resultados/` con
   sha256) y relojes sincronizados para que los tiempos sean comparables.
4. Que la máquina no tenga montado nada sensible que el sandbox pueda alcanzar: los mismos controles previos.

### Coordinación (lo que habría que construir después)

- Una cola persistente con **arrendamiento exclusivo** por misión (misión, intento y nodo) e identificadores
  idempotentes, para que una misión nunca corra dos veces a la vez ni se pierda si un nodo se cae.
- Un coordinador único que arme el informe consolidado. Los nodos solo devuelven resultados firmados por
  huella.
- Mantener la regla actual: las pruebas ocultas solo existen en el nodo que evalúa, nunca en el de
  inferencia.

Nada de esto existe todavía. El laboratorio actual es deliberadamente secuencial y tiene un único destino
de inferencia.
