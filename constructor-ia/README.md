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
  En 0.4.3: argumentos de relleno («null») tratados como ausentes, nombres reales como opciones del esquema,
  reparaciones precisas sin bucles de errores idénticos y respuestas crudas del modelo para diagnóstico.
  En 0.4.4: Ollama ocupado ≠ caído (la misión no falla por un bloqueo del servidor), interpretaciones acotadas
  que no se truncan, tiempos de cada llamada y `constructor-ia ollama-diag` (memoria, swap, PSI, CPU).
  En 0.4.5: conocimiento acumulativo — ¿la consulta responde la pregunta?, ¿repite lo ya hecho?, ¿aporta algo
  nuevo?, ¿la conclusión excede su evidencia? — con procedencia, certeza e historial de cada hecho preservados.
  En 0.4.6: la reparación de «falta la columna» da al modelo los nombres válidos y el campo obligatorio, y usa
  (con aviso) la columna que él mismo nombró; el informe separa conocimiento nuevo, histórico y propuestas.
  En 0.4.7: el alcance sale del SQL realmente ejecutado (un LIMIT interior impide certificar la tabla completa),
  nombres SQL distintos de valores, frases sin cortar, reformulaciones que no cuentan como nuevas y
  `explorer-reevaluar` (simulación primero) para reevaluar la memoria sin cambiar los hechos existentes.
  En 0.4.8: recuperación autónoma — el mismo atasco se reconoce aunque cambie la herramienta, la reparación
  muestra la contradicción y cómo resolverla, el modelo puede abandonar una pregunta (queda registrada) y seguir,
  las reparaciones tienen límites propios y, sin resultados, el cierre es un diagnóstico breve sin llamar a Ollama.
- 0.5.0-exp1: **investigador experimental** (`constructor-ia investigar`, ver INVESTIGADOR.md), separado del
  Explorador: Qwen3 interpreta la evidencia real y formula su siguiente pregunta en la misma respuesta; un motor
  determinista mide sin inventar; una memoria de investigaciones registra ciclos, dudas, dependencias y costo.
- 0.6.0-exp1: **instrumentos de investigación** (ver INSTRUMENTOS.md). Fase 0: canal seguro entre Python aislado
  y el intermediario MariaDB (presupuestos operativos, cobertura verificada, versiones inmutables, caducidad por
  tiempo y cuota, evidencia anclada). Fase 1: `constructor-ia laboratorio-instrumentos`, que compara modelos
  escribiendo, evaluando, corrigiendo y reutilizando instrumentos sobre una base sintética con verdad conocida, en un
  MariaDB privado y desechable. La Fase 2 (investigación autónoma con instrumentos) no está implementada.
  En 0.6.1, tras la primera prueba real: «terminó» ≠ «midió» (errores capturados, cobertura nula), diagnóstico
  compacto para reparar instrumentos (error, línea, datos con sus tipos, intentos anteriores), fallas de formato
  reparadas sin reescribir el instrumento, corte ante el mismo error y `laboratorio-revisar` (solo lectura).
- Piloto de microtareas verificables (rama `claude/constructor-ia-0.7-piloto-microtareas`, experimental, solo en
  `laboratorio/piloto/`): preregistro en [PILOTO.md](PILOTO.md) (commiteado antes del código), cinco brazos
  (A = 0.6.1, B, B*, C, D), juez aislado con auditoría de fugas, `laboratorio-mutantes` (sin modelo) y
  `laboratorio-piloto` (reanudable, con `--ensayo` técnico excluido del análisis). Se corre SIN instalar la rama, con
  `scripts/piloto.sh` desde un checkout aparte (PILOTO.md, Anexo 2).
  Protocolo v2 (Enmienda 3, tras el ensayo técnico v1 con Qwen3 4B real): contrato sin ambigüedad, rechazos que
  enseñan, disputa de verificadores con falsas alarmas medidas por el juez, entorno separado del modelo, guardia de
  memoria con `keep_alive` por pedido y contexto de 4096 controlado.
  Experimento de elección (PILOTO.md, «Etapas 0 y 1»): ¿la evidencia de ejecución, genérica y calculada sin el juez,
  ayuda a Qwen a elegir la instrucción causante? Pares control/tratamiento con una única diferencia, McNemar exacto;
  `laboratorio-eleccion-etapa0` (sin modelo) y `laboratorio-eleccion` (exige `--limite` o `--completo`).
  Ciclo de investigación v1 (PILOTO.md, «Ciclo v1»): hipótesis de las reglas congeladas → reparación propuesta por
  Qwen → verificación del coordinador en el sandbox con dos juegos de parámetros propios → juez separado;
  `laboratorio-ciclo` (`--sin-modelo`, `--limite-llamadas N` o `--completo`). Métrica principal: confirmaciones falsas.
  v1.2: Qwen puede cuestionar la hipótesis (PROPONGO_LINEA, validada por el coordinador) y recibe la
  retroalimentación de cada intento; presupuesto por defecto explícito (`--llamadas-por-defecto`, tope 6).
  v1.3: diagnóstico estructurado (DIAGNOSTICO/CAMBIA/EFECTO; la línea del coordinador es solo una sugerencia) y
  controles de coherencia entre lo declarado y el código, antes del sandbox; la verificación sigue siendo lo único que acepta.
  v1.3.2: el juez compara también con una base sintética independiente (otros totales, reparto, valores, tiempos y
  bordes), que existe solo durante el juicio y a la que el coordinador no tiene acceso.
  v1.3.3: el coordinador recuenta válidas e inválidas con SQL propio (`suite_ventanas.py`) y prueba cada reparación
  también con datos variados propios, para no aceptar constantes copiadas de la evidencia.
  v1.3.4: el coordinador sugiere una línea solo con evidencia (una regla congelada o un diagnóstico previo de Qwen
  sin probar); si no, no marca ninguna. El historial de una respuesta ambigua muestra todas sus propuestas, sin
  ejecutar ninguna.

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
