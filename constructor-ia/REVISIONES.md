# Evolución continua de proyectos: revisiones (v0.4.0)

Constructor IA ya no entrega solo aplicaciones aisladas: cada misión terminada es un **proyecto** que puede
seguir evolucionando durante días o semanas, con instrucciones humanas sucesivas, sin perder el contexto ni
empezar de cero.

## Intentos ≠ revisiones

| | Intento | Revisión |
|---|---|---|
| Qué es | Reparación automática **dentro** de una misión | Misión **nueva** que continúa un proyecto terminado |
| Quién la origina | El resultado de las pruebas (errores reales) | Una **instrucción humana** («Corregí el rebote…») |
| Límite | `max_attempts` de la misión | Sin límite: cada mejora es una revisión r2, r3, … |
| Base | El intento anterior de la misma misión | Una versión elegida: la vigente del proyecto, la final de una misión o un intento concreto |
| Resultado | Aprueba o falla esa misión | Versión nueva; si falla, la vigente no cambia |

Cada revisión tiene sus propios intentos de reparación. Subir `max_attempts` no crea revisiones ni las reemplaza.

## Cómo se usa

1. Abrir una misión terminada (aprobada, fallida, cancelada o interrumpida) de tipo función, programa o web.
2. Sección **Proyecto · revisión rN** → **Mejorar esta misión**.
3. Escribir la instrucción, por ejemplo:
   - «La pelota rebota demasiado pronto contra dos paredes. Corregí las colisiones para que toque exactamente los cuatro bordes».
   - «Cambiá el color de la pelota» · «Agregale un control de velocidad» · «Agregale un contador de colisiones» · «Ahora agregale otra pelota».
4. Elegir la base: **versión vigente del proyecto**, la versión final de esta misión o uno de sus intentos.
5. Opcional: agregar **pruebas nuevas** (casos, requisitos web o pruebas Python avanzadas). Las anteriores
   se conservan siempre. Si el proyecto viene del Explorador: *actualizar las instantáneas de datos*.
6. Autorizar y **Crear revisión**.

En la sección del proyecto:
- Lista de revisiones (r1 «Versión original», r2, r3…) con su instrucción, estado, base y la marca **vigente**.
- **Hacer vigente**: vuelve a una versión aprobada anterior. No borra nada; solo cambia la base por defecto de
  las próximas mejoras.
- **Comparar versiones**: diferencias archivo por archivo (agregado / eliminado / modificado / igual) entre
  cualquier par de versiones del proyecto: finales, bases e intentos.
- **ZIP** y **vista previa** por revisión (cada revisión web tiene su sección *Proyecto web*, con las mismas
  protecciones de origen separado).

## Qué hace el motor en una revisión

1. **Recupera** la versión base y la **copia** dentro de la revisión como `base/…` (inmutable, con SHA-256).
2. **Entiende que es una modificación**: el primer intento usa un prompt de revisión con el propósito
   original, el historial de instrucciones anteriores (r2, r3…), la instrucción nueva, las pruebas (heredadas
   y nuevas) y los archivos actuales.
   - Python: el `solucion.py` actual completo.
   - Web: los archivos relevantes primero (por coincidencia de palabras de la instrucción con rutas y contenido),
     luego el resto si cabe, y la lista de los que no entran.
3. **Modifica solo lo necesario**: en web el modelo devuelve únicamente los archivos que cambia; el resto se
   reutiliza sin regenerar. La consola informa «N modificados, M reutilizados sin regenerar» y, al aprobar,
   los cambios respecto de la base. Las instantáneas `datos/*.json` siguen siendo inmutables.
4. **Verifica en el sandbox**, con las mismas protecciones (Bubblewrap, seccomp, cgroups, fallo cerrado), con
   **todas** las pruebas: heredadas y nuevas.
5. **Repara** con intentos si algo falla, usando los errores reales.
6. **Guarda** la versión nueva en `final/…`. Al aprobarse pasa a ser la **vigente**; si falla, la vigente
   sigue siendo la última aprobada.

## Persistencia

Migración `0003_revisiones.sql`:
- `missions`: `project_id` (misión original), `revision` (número), `parent_id` (misión base), `base_ref`
  (`final` o `intento-N`) e `instruction`.
- `projects`: `id` (= misión original), `title`, `current_mission` (versión vigente).
- `project_events`: revisiones creadas y cambios de versión vigente, con usuario y fecha.

Las misiones anteriores y sus archivos **nunca se modifican**. Un reintento (*reencolar*) de una revisión
conserva su proyecto, número, base e instrucción, y copia su `base/`.

## Proyectos del Explorador

Los tableros construidos desde el Explorador guardan en su especificación `data_source`: conexión, aplicación
del mapa (`fact_id`) y consultas autorizadas. **No guardan credenciales.** Las revisiones heredan esa
referencia y las instantáneas. *Actualizar las instantáneas* vuelve a ejecutar esas consultas por el
intermediario de solo lectura, con todas sus verificaciones y límites. El código generado sigue sin
credenciales ni acceso SQL.

## Producción

Las revisiones trabajan solo dentro de Constructor IA. Llevar una versión a un sistema real sigue
requiriendo la solicitud de aprobación humana de esa misión y la aplicación manual por SSH (sin cambios
respecto de 0.1).

## API

| Método | Ruta | Uso |
|---|---|---|
| POST | `/api/missions/<id>/revisions` | `{instruction, base_ref \| base:"vigente", cases \| checks, tests_py, cases_reviewed, authorize_execution, max_attempts, refresh_data}` |
| GET | `/api/projects/<id>` | Proyecto, revisiones (con `vigente`) y eventos |
| POST | `/api/projects/<id>/current` | `{mission_id}`: elegir una versión aprobada como vigente |
| GET | `/api/versions/diff?a=MISION:ref&b=MISION:ref` | Diferencias por archivo (`ref` = `final`, `base`, `intento-N`); solo dentro del mismo proyecto |

Todas exigen sesión; las de cambio, además, CSRF y mismo origen.

## Pruebas

`tests/test_revisions.py` (8):
- **Python:**
  - reutiliza el código anterior y el prompt lo incluye junto con la instrucción;
  - conserva las 2 pruebas heredadas y agrega 1 nueva, y las 3 se ejecutan;
  - la revisión queda vigente y la versión anterior intacta (SHA-256);
  - una revisión que falla con 2 intentos no cambia la vigente, y se puede continuar desde su último intento;
  - el historial de instrucciones llega al prompt;
  - se puede elegir otra versión vigente, y se rechaza una ajena;
  - validaciones de la instrucción.
- **Web:**
  - solo cambia `js/app.js`, mientras `index.html` y el CSS se reutilizan byte a byte;
  - las diferencias muestran «igual / modificado»;
  - se agrega un requisito nuevo;
  - un proyecto con instantáneas conserva `data_source` y los datos siguen inmutables.
- **API:**
  - sesión y CSRF obligatorios;
  - flujo completo, elección de la vigente y base «vigente»;
  - diferencias limitadas al mismo proyecto, y referencias inválidas rechazadas;
  - el reencolado conserva el linaje y la base.

En Chromium, con la interfaz real y un modelo simulado, se recorrió el escenario de la pelota que rebota antes
de tocar dos paredes. Al recibir la instrucción, el modelo devolvió solo `app.js` corregido; las diferencias
muestran solo ese archivo; pasaron 9/9 verificaciones y la vista previa funciona.
**No se probó con Qwen3 real.**

## Actualización por SSH (0.3.0 → 0.4.0)

```bash
cd ~/src/dieguinPinga
git fetch origin claude/constructor-ia-0.4-revisiones
git checkout claude/constructor-ia-0.4-revisiones
git pull --ff-only
bash ~/src/dieguinPinga/constructor-ia/scripts/update.sh     # copia de la base, reinstala, autodiagnóstico
~/constructor-ia/bin/constructor-ia selftest
```
- Migración automática `0003` (columnas y tablas nuevas; no reescribe datos). Las misiones existentes pasan
  a ser la revisión r1 de su propio proyecto; su versión vigente se calcula la primera vez que se abren.
- `config.toml` no cambia.

## Rollback (0.4.0 → 0.3.0)

```bash
# 1) Cancelar revisiones pendientes o en curso (la 0.3.0 las ejecutaría sin su base):
~/constructor-ia/venv/bin/python3 -c "
import sqlite3, time; c = sqlite3.connect('$HOME/constructor-ia/data/constructor.db')
c.execute(\"UPDATE missions SET state='cancelada', finished_at=?, error='rollback a 0.3.0' WHERE project_id IS NOT NULL AND state IN ('pendiente','preparando','generando','probando','corrigiendo')\", (time.time(),)); c.commit()"
# 2) Volver al código anterior:
~/constructor-ia/bin/rollback.sh
```
- La 0.3.0 ignora las columnas y tablas nuevas (`user_version = 3`). Las revisiones terminadas siguen siendo
  misiones normales visibles, con sus archivos.
- Para volver exactamente a los datos anteriores: restaurar la copia de `~/constructor-ia/data/backups/`
  que hizo `update.sh` (INSTALL.md §10).

## Limitaciones

- La selección de archivos «relevantes» es léxica (palabras de la instrucción). Con proyectos grandes,
  algunos archivos solo se listan por nombre. El modelo puede pedir cambiarlos, pero no los «ve» completos
  en ese intento.
- Las pruebas estáticas web no ejecutan el JavaScript: la corrección de un comportamiento como el rebote se
  verifica en la vista previa o con requisitos declarados (p. ej. `js_contiene`), no con una simulación de física.
- El historial que recibe el modelo se limita a las últimas 8 instrucciones de la línea de la versión base,
  por el contexto de 4096–6144 tokens.
- Las pruebas heredadas no se pueden quitar desde la interfaz (a propósito). Para cambiar un requisito
  equivocado, crear una misión nueva.
- Las exploraciones no tienen revisiones: se **continúan** (Explorador).
