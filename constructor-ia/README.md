# Constructor IA

Servidor web local para conversar con una IA local, asignarle **misiones de
programación en Python**, ver su progreso en vivo y revisar intentos, errores,
correcciones y archivos finales. Pensado para la ASUS VivoBook S15 X510UF
(Linux Mint 22.3) y accesible desde PC y celular a través de Tailscale.

- Sin APIs comerciales ni servicios en la nube: usa la instancia **Ollama CPU
  (`127.0.0.1:11435`)** con `qwen3:4b-instruct` (constructor) y `qwen3:1.7b` (chat).
- Sin dependencias externas: solo la biblioteca estándar de Python ≥ 3.11
  (servidor HTTP, SQLite, scrypt). Interfaz HTML/CSS/JS local, sin CDN.
- Independiente de Node-RED, Vigilanti, MariaDB, OpenClaw y de la instancia
  Ollama principal (11434). No los modifica.

## Tipos de misión

- **Función de Python** y **Programa (stdin/stdout)**: código verificado con casos de prueba.
- **Página web / Tablero** (Taller Web, v0.2.0): proyecto HTML/CSS/JS de varios archivos con
  verificación de estructura, sintaxis, referencias y requisitos; vista previa aislada en un
  origen separado (puerto 8767) y descarga ZIP. Ver [TALLER_WEB.md](TALLER_WEB.md).
- **Exploración de base de datos** (Explorador, v0.3.0): la IA descubre por sí misma una base MariaDB
  desconocida con herramientas de solo lectura, construye un mapa persistente (hechos comprobados vs.
  inferencias), propone aplicaciones y construye tableros con instantáneas. Ver [EXPLORADOR.md](EXPLORADOR.md).
  En 0.4.1: validación y reparación de argumentos, contraste de afirmaciones con su evidencia, comentarios de
  metadatos en el contexto, presupuesto de tiempo total y evaluación separada de proceso, calidad y objetivo.
  En 0.4.2: ciclo en dos etapas (investigación → interpretación de registros reales), contexto del modelo
  medido y reducido sin perder evidencia, sin reintentos idénticos ante un contexto excedido, y distinción
  entre registros históricos y estado actual.

## Proyectos que evolucionan (v0.4.0)

Cada misión terminada es un proyecto: **Mejorar esta misión** crea una revisión nueva (r2, r3…) a partir de
una instrucción humana, reutiliza los archivos existentes, conserva las pruebas anteriores, permite agregar
nuevas y guarda una versión nueva sin tocar las anteriores. Las revisiones (instrucciones humanas) son
distintas de los intentos (reparación automática). Ver [REVISIONES.md](REVISIONES.md).

## Ciclo de una misión

```
objetivo + casos de prueba (definidos/confirmados por el operador)
   → generación (Ollama CPU, una inferencia a la vez)
   → extracción + validación estática (ast, sin ejecutar)
   → pruebas en el ejecutor aislado (bwrap + seccomp + cgroup systemd)
   → resultados firmados (HMAC) → ¿todo pasa? → aprobada
                                 → si no: errores reales al modelo → reparación (máx. 3 por defecto)
```

Una misión se **aprueba únicamente** si el arnés del sandbox devuelve un resultado
firmado con todas las pruebas independientes superadas. El modelo nunca escribe
las pruebas que deciden la aprobación.

## Secciones de la interfaz

| Sección | Qué hace |
|---|---|
| Conversación | Chat con el modelo (solo texto, nunca ejecuta nada). Puede convertir la conversación en un *borrador* de misión, explicar archivos y continuar conversaciones previas. |
| Misiones | Lista con estado, intento actual/máximo y tiempo. |
| Misión | Datos, consola de progreso en vivo (SSE), historial de intentos con resultado de cada caso, archivos (ver/descargar/explicar), comparación de versiones, aprobaciones. |
| Nueva misión | Formulario con casos de prueba (tabla entrada → salida), casos ocultos, pruebas avanzadas opcionales y autorización explícita de ejecución aislada. |
| Aprobaciones | Solicitudes humanas de instalación/cambio. Aprobar **no ejecuta nada**: genera un paquete con SHA-256 para aplicar a mano. |
| Estado | Servidor, Ollama CPU, modelo, memoria, carga, cola, misión en curso, último error, autodiagnóstico del aislamiento, actividad. |

Estados de misión: `pendiente`, `preparando`, `generando`, `probando`,
`corrigiendo`, `aprobada`, `fallida`, `cancelada`, `interrumpida` (el servidor se
detuvo durante la misión; nunca se re-ejecuta sola, se puede *reencolar*).

## Documentación

- [INSTALL.md](INSTALL.md) — instalación por SSH, inicio/parada, acceso por Tailscale, actualización, rollback, desinstalación y recuperación.
- [ARCHITECTURE.md](ARCHITECTURE.md) — componentes, datos, cola, qué usa Ollama directo y qué OpenClaw.
- [SECURITY.md](SECURITY.md) — modelo de amenazas, controles, riesgos restantes.
- [TALLER_WEB.md](TALLER_WEB.md) — misiones web, vista previa aislada, seguridad, demostración y actualización.
- [EXPLORADOR.md](EXPLORADOR.md) — explorador de bases de datos: arquitectura, seguridad, cuenta SQL restringida, actualización y rollback.
- [REVISIONES.md](REVISIONES.md) — evolución continua de proyectos: revisiones, versión vigente, comparación, actualización y rollback.
- [TESTING.md](TESTING.md) — pruebas automáticas (con resultados) y verificaciones obligatorias en la VivoBook.

## Inicio rápido (en la VivoBook, por SSH)

```bash
git clone -b claude/modest-clarke-1dzltx https://github.com/dieguinPinga/dieguinPinga.git ~/src/dieguinPinga
bash ~/src/dieguinPinga/constructor-ia/scripts/install.sh
~/constructor-ia/bin/constructor-ia adduser diego
~/constructor-ia/bin/constructor-ia selftest
systemctl --user start constructor-ia
# Abrir http://127.0.0.1:8766 (túnel SSH) o ver INSTALL.md §6 para Tailscale
```
