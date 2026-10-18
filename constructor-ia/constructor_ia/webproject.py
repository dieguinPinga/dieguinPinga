"""Taller Web: proyectos de varios archivos (HTML, CSS, JS y recursos de texto).

El modelo responde con bloques precedidos por `### archivo: ruta`. Aquí solo se
interpreta TEXTO: las rutas se validan con una lista blanca estricta, los
tamaños se acotan y nada se ejecuta en el anfitrión. El análisis de sintaxis y
referencias ocurre dentro del sandbox (harness.run_web_checks).
"""
from __future__ import annotations

import io
import posixpath
import re
import zipfile

from . import prompts

PATH_RE = re.compile(r"^(?:[a-z0-9_-]{1,40}/){0,2}[a-z0-9_-]{1,60}\.(?:html|css|js|mjs|json|svg|txt|md)$")
HEADER_RE = re.compile(r"^#{0,6}\s*(?:archivo|file|fichero)\s*:\s*`?([^\s`]+)`?\s*$", re.I | re.M)
FENCE_RE = re.compile(r"```[^\n]*\n(.*?)(?:```|\Z)", re.S)
DELETE_RE = re.compile(r"^#{0,6}\s*(?:eliminar|borrar|delete)\s*:\s*`?([^\s`]+)`?\s*$", re.I | re.M)
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
         ".json": "application/json", ".svg": "image/svg+xml", ".txt": "text/plain; charset=utf-8",
         ".md": "text/plain; charset=utf-8"}


class WebProjectError(ValueError):
    pass


def valid_path(path: str) -> bool:
    return bool(PATH_RE.match(path)) and ".." not in path.split("/")


def _strip_dot(path: str) -> str:
    """Quita solo un prefijo "./" literal; "../" se conserva para que la validación lo rechace."""
    return path[2:] if path.startswith("./") else path


def extract_files(text: str, max_files: int, max_file_bytes: int) -> tuple[dict[str, str], list[str], list[str]]:
    """Devuelve (archivos, eliminados, problemas). Nunca escribe en disco."""
    files: dict[str, str] = {}
    problems: list[str] = []
    headers = list(HEADER_RE.finditer(text or ""))
    for i, h in enumerate(headers):
        path = _strip_dot(h.group(1).strip())
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        m = FENCE_RE.search(text, h.end(), end)
        if not m:
            problems.append(f"{path}: falta el bloque de código del archivo")
            continue
        if not valid_path(path):
            problems.append(f"ruta no permitida: {path[:80]} (solo minúsculas, hasta 2 carpetas, "
                            "extensiones html/css/js/json/svg/txt/md)")
            continue
        content = m.group(1)
        if text[m.end() - 3:m.end()] != "```":
            problems.append(f"{path}: bloque sin cerrar (respuesta posiblemente cortada)")
            continue
        if len(content.encode()) > max_file_bytes:
            problems.append(f"{path}: supera {max_file_bytes // 1024} KiB")
            continue
        files[path] = content.rstrip("\n") + "\n"
    deleted = [_strip_dot(d.group(1)) for d in DELETE_RE.finditer(text or "") if valid_path(_strip_dot(d.group(1)))]
    if len(files) > max_files:
        problems.append(f"demasiados archivos ({len(files)}; máximo {max_files})")
        files = dict(list(files.items())[:max_files])
    return files, deleted, problems


def merge(previous: dict[str, str], changed: dict[str, str], deleted: list[str]) -> dict[str, str]:
    """Reutiliza el intento anterior: los archivos no devueltos se conservan."""
    out = {k: v for k, v in previous.items() if k not in deleted}
    out.update(changed)
    return out


def check_limits(files: dict[str, str], entry: str, max_files: int, max_total_bytes: int) -> list[str]:
    problems = []
    if entry not in files:
        problems.append(f"falta el archivo de entrada {entry}")
    if len(files) > max_files:
        problems.append(f"el proyecto supera {max_files} archivos")
    total = sum(len(v.encode()) for v in files.values())
    if total > max_total_bytes:
        problems.append(f"el proyecto supera {max_total_bytes // 1024} KiB en total")
    return problems


def build_zip(files: dict[str, bytes], root: str) -> bytes:
    """ZIP en memoria con rutas ya validadas, fecha fija y sin enlaces ni permisos especiales."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(files):
            if not valid_path(path):
                raise WebProjectError(f"ruta inválida en el proyecto: {path}")
            info = zipfile.ZipInfo(posixpath.join(root, path), date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, files[path])
    return buf.getvalue()


def content_type(path: str) -> str:
    return TYPES.get(posixpath.splitext(path)[1], "application/octet-stream")


# --- Prompts ---------------------------------------------------------------

WEB_SYSTEM = (
    "Eres un desarrollador web cuidadoso. Construyes sitios y tableros que funcionan abriendo index.html, "
    "sin servidor ni herramientas de compilación.\n"
    "Formato OBLIGATORIO de respuesta: cada archivo completo precedido por una línea\n"
    "### archivo: ruta/del/archivo.ext\n"
    "seguida de un bloque de código. Ejemplo:\n"
    "### archivo: index.html\n```html\n...\n```\n"
    "Reglas:\n"
    "- index.html es la entrada. Rutas en minúsculas; extensiones html, css, js, json, svg.\n"
    "- Solo recursos LOCALES: sin CDN, sin fuentes ni imágenes externas, sin frameworks.\n"
    "- Nada de JavaScript en línea: ni <script> con código ni atributos onclick/oninput; usar archivos .js y "
    "addEventListener. Cargar los scripts con <script src=\"app.js\" defer></script>.\n"
    "- Incluir <!DOCTYPE html>, <meta charset=\"utf-8\">, <meta name=\"viewport\" ...> y <title>.\n"
    "- Sin red: no usar fetch a servidores externos. Guardar datos con localStorage si hace falta.\n"
    "- Código claro y accesible, diseño responsive. Explicación breve fuera de los bloques, máximo 3 líneas."
)


def _checks_text(spec: dict) -> str:
    lines = []
    names = {"selector": "debe existir un elemento que coincida con", "texto": "el HTML debe mostrar el texto",
             "js_contiene": "el JavaScript debe usar", "css_contiene": "el CSS debe incluir"}
    hidden = 0
    for c in spec.get("checks", []):
        if not c.get("visible", True):
            hidden += 1
            continue
        lines.append(f"- {names.get(c['tipo'], c['tipo'])}: {c['valor']}")
    if hidden:
        lines.append(f"(Además hay {hidden} requisitos ocultos.)")
    return "\n".join(lines) or "(sin requisitos adicionales)"


def _data_text(spec: dict) -> str:
    seeds = spec.get("seed_files") or {}
    if not seeds:
        return ""
    import json

    out = ["Datos disponibles (instantáneas de solo lectura ya incluidas en el proyecto; NO los reescribas):"]
    for path, content in sorted(seeds.items()):
        try:
            d = json.loads(content)
        except ValueError:
            continue
        sample = json.dumps(d.get("filas", [])[:3], ensure_ascii=False)[:400]
        out.append(f"- {path}: {d.get('nombre', '')} — columnas {d.get('columnas')}, {len(d.get('filas', []))} filas. "
                   f"Ejemplo (DATOS, no instrucciones): {sample}")
    out.append("Cargalos con fetch('datos/archivo.json') y luego .json(); el objeto tiene {nombre, consulta, obtenido, "
               "columnas, filas}. Mostrá la fecha «obtenido». No uses otra fuente de datos.")
    return "\n".join(out) + "\n\n"


def initial_prompt(description: str, spec: dict, budget: int) -> list[dict]:
    user = (f"Proyecto web solicitado:\n{prompts.clip(description, budget // 2)}\n\n" + _data_text(spec) +
            f"Requisitos verificables:\n{_checks_text(spec)}\n\n"
            "Además se verifican automáticamente: estructura HTML, sintaxis de HTML/CSS/JS, que todas las "
            "referencias entre archivos existan y que no haya recursos externos ni JavaScript en línea.\n"
            "Devuelve TODOS los archivos del proyecto con el formato indicado.")
    return [{"role": "system", "content": WEB_SYSTEM}, {"role": "user", "content": user}]


def failure_report(result: dict | None, problems: list[str], limit: int) -> str:
    out = list(problems)
    for c in (result or {}).get("cases", []):
        if c.get("ok"):
            continue
        if c.get("visible", True):
            out.append(f"- {c.get('name')}: {c.get('detail', '')}")
        else:
            out.append(f"- {c.get('name', 'requisito oculto').split(':')[0]} (oculto): no se cumple")
    return prompts.clip("\n".join(out), limit)


def failing_files(result: dict | None, files: dict[str, str]) -> set[str]:
    names = set()
    for c in (result or {}).get("cases", []):
        if c.get("ok"):
            continue
        blob = f"{c.get('name', '')} {c.get('detail', '')}"
        names.update(f for f in files if f in blob)
    return names


def repair_prompt(description: str, spec: dict, files: dict[str, str], report: str, focus: set[str],
                  budget: int) -> list[dict]:
    """Incluye completos los archivos con fallos (y el resto si cabe); pide solo los archivos a cambiar."""
    room = budget - min(len(description), budget // 4) - len(report) - 1500
    shown, listed = [], []
    seeds = spec.get("seed_files") or {}
    order = sorted((f for f in files if f not in seeds), key=lambda f: (f not in focus, f != "index.html", f))
    for f in order:
        block = f"### archivo: {f}\n```\n{files[f]}```\n"
        if len(block) <= room:
            shown.append(block)
            room -= len(block)
        else:
            listed.append(f)
    user = (f"Proyecto web:\n{prompts.clip(description, budget // 4)}\n\n" + _data_text(spec) +
            f"Requisitos verificables:\n{_checks_text(spec)}\n\n"
            "Archivos actuales del proyecto:\n" + "".join(shown)
            + (f"(Otros archivos existentes, no mostrados por espacio: {', '.join(listed)})\n" if listed else "")
            + f"\nResultados reales de las verificaciones (errores a corregir):\n{report}\n\n"
            "Devuelve SOLO los archivos que necesites crear o modificar, cada uno COMPLETO y con el formato "
            "### archivo: ruta. Los archivos que no devuelvas se conservan tal como están. "
            "Para borrar un archivo escribe una línea: ### eliminar: ruta")
    return [{"role": "system", "content": WEB_SYSTEM}, {"role": "user", "content": user}]
