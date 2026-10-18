"""Arnés de pruebas CONFIABLE que corre dentro del sandbox.

Protocolo:
  * Lee por stdin una línea JSON con {nonce, mode, spec, tests_py, ...}.
    Los casos (incluidos los ocultos) solo existen en la memoria de este
    proceso: no hay archivo de pruebas visible para el código generado.
  * Se marca como no volcable (PR_SET_DUMPABLE=0) para que el proceso hijo,
    aunque comparta UID, no pueda leer su memoria ni sus descriptores vía /proc.
  * Ejecuta la solución en procesos hijos con límites rlimit propios.
  * Escribe una única línea final "CIA-RESULT <json> <hmac>" firmada con el
    nonce recibido, que el anfitrión verifica.

Solo usa la biblioteca estándar; se ejecuta con `python3 -I -S`.
"""
import ctypes
import hashlib
import hmac
import json
import math
import os
import resource
import select
import signal
import socket
import subprocess
import sys
import threading
import time

PY = sys.executable
CHILD = "/work/harness/child_runner.py"
CODE = "/work/code/solucion.py"
OUT_CAP = 64 * 1024
ERR_CAP = 16 * 1024


def set_nondumpable() -> bool:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(4, 0, 0, 0, 0) == 0  # PR_SET_DUMPABLE = 4
    except Exception:
        return False


def child_limits(cpu_s: int):
    def apply():
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (8 * 1024 * 1024, 8 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s + 1))
    return apply


class Drain(threading.Thread):
    """Lee un descriptor hasta EOF guardando como máximo `cap` bytes."""

    def __init__(self, fh, cap):
        super().__init__(daemon=True)
        self.fh, self.cap, self.data, self.overflow = fh, cap, bytearray(), False
        self.start()

    def run(self):
        while True:
            try:
                chunk = self.fh.read1(65536) if hasattr(self.fh, "read1") else self.fh.read(65536)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            room = self.cap - len(self.data)
            if room > 0:
                self.data += chunk[:room]
            if len(chunk) > room:
                self.overflow = True

    def text(self):
        t = self.data.decode("utf-8", "replace")
        return t + ("\n[... salida truncada]" if self.overflow else "")


def run_bounded(cmd, stdin_bytes, timeout, cpu_s):
    """Ejecuta un proceso con entrada fija, tiempo máximo y salida acotada."""
    t0 = time.monotonic()
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         cwd="/tmp", preexec_fn=child_limits(cpu_s), start_new_session=True)
    out, err = Drain(p.stdout, OUT_CAP), Drain(p.stderr, ERR_CAP)
    try:
        p.stdin.write(stdin_bytes)
        p.stdin.close()
    except OSError:
        pass
    timed_out = False
    try:
        p.wait(timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_group(p)
        p.wait()
    out.join(1)
    err.join(1)
    return {"code": p.returncode, "timeout": timed_out, "stdout": out.text(), "stderr": err.text(),
            "overflow": out.overflow, "ms": int((time.monotonic() - t0) * 1000)}


def kill_group(p):
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        p.kill()
    except OSError:
        pass


class Child:
    """Proceso hijo que importa solucion.py y responde llamadas JSON."""

    def __init__(self, timeout, cpu_s):
        self.timeout = timeout
        self.p = subprocess.Popen([PY, "-I", "-S", CHILD, CODE], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd="/tmp",
                                  preexec_fn=child_limits(cpu_s), start_new_session=True)
        self.err = Drain(self.p.stderr, ERR_CAP)
        self.buf = b""
        self.next_id = 0
        self.ready = None

    def read_line(self, timeout):
        deadline = time.monotonic() + timeout
        fd = self.p.stdout.fileno()
        while b"\n" not in self.buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout", None
            r, _, _ = select.select([fd], [], [], remaining)
            if not r:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                return "eof", None
            self.buf += chunk
            if len(self.buf) > 2_000_000:
                return "overflow", None
        line, self.buf = self.buf.split(b"\n", 1)
        try:
            return "ok", json.loads(line)
        except ValueError:
            return "garbage", None

    def start(self):
        status, msg = self.read_line(self.timeout * 2)
        if status != "ok":
            self.ready = {"ready": False, "error": self.describe_death(status), "exc": status}
        else:
            self.ready = msg
        return self.ready

    def call(self, fn, args, kwargs):
        self.next_id += 1
        rid = self.next_id
        req = json.dumps({"id": rid, "fn": fn, "args": args, "kwargs": kwargs}) + "\n"
        t0 = time.monotonic()
        try:
            self.p.stdin.write(req.encode())
            self.p.stdin.flush()
        except OSError:
            return "dead", None, 0
        status, msg = self.read_line(self.timeout)
        ms = int((time.monotonic() - t0) * 1000)
        if status == "ok" and (not isinstance(msg, dict) or msg.get("id") != rid):
            status = "garbage"
        return status, msg, ms

    def describe_death(self, status):
        if status == "timeout":
            return f"Tiempo agotado ({self.timeout} s)"
        if status in ("eof", "dead"):
            try:
                code = self.p.wait(1)
            except subprocess.TimeoutExpired:
                code = None
            if code is not None and code < 0:
                sig = -code
                name = {9: "SIGKILL (posible límite de memoria o tiempo de CPU)", 24: "SIGXCPU (tiempo de CPU)",
                        25: "SIGXFSZ (tamaño de archivo)", 11: "SIGSEGV"}.get(sig, f"señal {sig}")
                return f"El proceso de la solución terminó por {name}"
            return f"El proceso de la solución terminó inesperadamente (código {code})"
        return "Respuesta inválida del proceso de la solución"

    def stop(self):
        kill_group(self.p)
        try:
            self.p.wait(2)
        except subprocess.TimeoutExpired:
            pass
        self.err.join(1)


def short(value, limit=400):
    try:
        s = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        s = repr(value)
    return s if len(s) <= limit else s[:limit] + "…"


def equal(a, b, tol):
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b if isinstance(a, bool) and isinstance(b, bool) else False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if isinstance(a, float) or isinstance(b, float):
            if math.isnan(a) and math.isnan(b):
                return True
            return math.isclose(a, b, rel_tol=tol, abs_tol=tol)
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(equal(x, y, tol) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(equal(a[k], b[k], tol) for k in a)
    return a == b


def run_function_cases(spec, timeout, cpu_s):
    results = []
    fn = spec["function"]
    tol = float(spec.get("float_tol", 1e-9))
    child, load_error = None, None
    restarts = 0
    for i, case in enumerate(spec.get("cases", [])):
        entry = {"n": i + 1, "kind": "caso", "visible": bool(case.get("visible", True)),
                 "args": case.get("args", []), "kwargs": case.get("kwargs", {})}
        if child is None:
            if restarts > 8:
                entry.update(ok=False, status="error", detail="Demasiados reinicios del proceso de la solución")
                results.append(entry)
                continue
            child = Child(timeout, cpu_s)
            ready = child.start()
            restarts += 1
            if not ready.get("ready"):
                load_error = ready.get("error", "Error al cargar la solución")
                child.stop()
                child = None
                entry.update(ok=False, status="error", detail="No se pudo cargar solucion.py:\n" + load_error)
                results.append(entry)
                continue
        status, msg, ms = child.call(fn, entry["args"], entry["kwargs"])
        entry["ms"] = ms
        if status != "ok":
            entry.update(ok=False, status="timeout" if status == "timeout" else "error",
                         detail=child.describe_death(status))
            stderr = child.err.text()
            child.stop()
            if stderr:
                entry["stderr"] = stderr[-2000:]
            child = None
        elif "raises" in case and case["raises"]:
            want = case["raises"]
            if msg.get("ok"):
                entry.update(ok=False, status="fallo", got=short(msg.get("value")), expected=f"excepción {want}",
                             detail=f"Se esperaba la excepción {want} y se devolvió un valor")
            elif msg.get("exc") == want:
                entry.update(ok=True, status="ok", got=f"excepción {want}")
            else:
                entry.update(ok=False, status="fallo", got=f"excepción {msg.get('exc')}", expected=f"excepción {want}",
                             detail=f"Se esperaba {want} y se lanzó {msg.get('exc')}: {msg.get('msg', '')}",
                             tb=msg.get("tb"))
        elif not msg.get("ok"):
            entry.update(ok=False, status="error", got=f"excepción {msg.get('exc')}",
                         expected=short(case.get("expect")),
                         detail=f"{msg.get('exc')}: {msg.get('msg', '')}", tb=msg.get("tb"))
        else:
            ok = equal(msg.get("value"), case.get("expect"), tol)
            entry.update(ok=ok, status="ok" if ok else "fallo", got=short(msg.get("value")),
                         expected=short(case.get("expect")))
            if not ok:
                entry["detail"] = "Resultado distinto del esperado"
        results.append(entry)
    if child is not None:
        stderr = child.err.text()
        child.stop()
        if stderr and results:
            results[-1].setdefault("stderr", stderr[-2000:])
    return results, load_error


def normalize_output(text):
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def run_script_cases(spec, timeout, cpu_s):
    results = []
    for i, case in enumerate(spec.get("cases", [])):
        entry = {"n": i + 1, "kind": "script", "visible": bool(case.get("visible", True)),
                 "stdin": case.get("stdin", "")}
        r = run_bounded([PY, "-I", "-S", CODE], case.get("stdin", "").encode(), timeout, cpu_s)
        entry["ms"] = r["ms"]
        got, want = normalize_output(r["stdout"]), normalize_output(case.get("stdout", ""))
        entry.update(got=got[:2000], expected=want[:2000])
        if r["timeout"]:
            entry.update(ok=False, status="timeout", detail=f"Tiempo agotado ({timeout} s)")
        elif r["code"] != 0:
            entry.update(ok=False, status="error", detail=f"El programa terminó con código {r['code']}",
                         stderr=r["stderr"][-2000:])
        elif r["overflow"]:
            entry.update(ok=False, status="fallo", detail="Salida demasiado grande")
        else:
            ok = got == want
            entry.update(ok=ok, status="ok" if ok else "fallo")
            if not ok:
                entry["detail"] = "La salida no coincide con la esperada"
        results.append(entry)
    return results


class RemoteError(Exception):
    pass


def run_operator_tests(tests_py, timeout, cpu_s):
    """Pruebas Python escritas por el operador (confiables, ejecutadas en el arnés)."""
    results = []
    ns = {"__name__": "pruebas_operador"}
    try:
        exec(compile(tests_py, "pruebas_operador.py", "exec"), ns)
    except Exception as e:  # noqa: BLE001
        return [{"n": 1, "kind": "prueba", "visible": False, "ok": False, "status": "error",
                 "detail": f"Las pruebas del operador no compilan: {type(e).__name__}: {e}"}]
    names = sorted(k for k, v in ns.items() if k.startswith("test_") and callable(v))
    import builtins

    for i, name in enumerate(names):
        child = Child(timeout, cpu_s)
        ready = child.start()
        entry = {"n": i + 1, "kind": "prueba", "name": name, "visible": False}
        if not ready.get("ready"):
            child.stop()
            entry.update(ok=False, status="error", detail="No se pudo cargar solucion.py:\n" + ready.get("error", ""))
            results.append(entry)
            continue

        class Proxy:
            def __getattr__(self, fn):
                def call(*args, **kwargs):
                    status, msg, _ = child.call(fn, list(args), kwargs)
                    if status != "ok":
                        raise RemoteError(child.describe_death(status))
                    if msg.get("ok"):
                        return msg.get("value")
                    exc = getattr(builtins, str(msg.get("exc")), None)
                    if isinstance(exc, type) and issubclass(exc, Exception):
                        raise exc(msg.get("msg", ""))
                    raise RemoteError(f"{msg.get('exc')}: {msg.get('msg', '')}")
                return call

        t0 = time.monotonic()
        try:
            ns[name](Proxy())
            entry.update(ok=True, status="ok")
        except AssertionError as e:
            entry.update(ok=False, status="fallo", detail=f"Aserción fallida: {e}"[:800])
        except RemoteError as e:
            entry.update(ok=False, status="error", detail=str(e)[:800])
        except Exception as e:  # noqa: BLE001
            entry.update(ok=False, status="error", detail=f"{type(e).__name__}: {e}"[:800])
        entry["ms"] = int((time.monotonic() - t0) * 1000)
        child.stop()
        results.append(entry)
    return results


# --- Taller Web: verificación estática de proyectos HTML/CSS/JS -------------
# Todo el análisis de archivos generados ocurre aquí, dentro del sandbox.

WEB_ROOT = "/work/code"
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
OPTIONAL_END = {"html", "head", "body", "p", "li", "dt", "dd", "option", "optgroup", "tr", "td", "th", "thead",
                "tbody", "tfoot", "colgroup", "rp", "rt", "caption"}


def load_project():
    files = {}
    for dirpath, _dirs, names in os.walk(WEB_ROOT):
        for name in names:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, WEB_ROOT)
            with open(full, "rb") as fh:
                files[rel] = fh.read(1_000_001).decode("utf-8", "replace")
    return files


def is_external(url):
    u = url.strip().lower()
    return u.startswith(("http:", "https:", "ftp:", "//", "ws:", "wss:"))


def resolve_ref(base_file, url):
    """Ruta local relativa al proyecto, o None si no es un archivo local (ancla, mailto, data:, etc.)."""
    u = url.strip()
    if not u or u.startswith(("#", "mailto:", "tel:", "data:", "blob:")):
        return None
    u = u.split("#", 1)[0].split("?", 1)[0]
    if not u:
        return None
    if u.startswith("/"):
        return os.path.normpath(u.lstrip("/"))
    return os.path.normpath(os.path.join(os.path.dirname(base_file), u))


class HtmlScan:
    def __init__(self, text):
        import html.parser

        scan = self
        self.elements, self.errors, self.text_parts = [], [], []
        self.doctype = False
        self.inline_scripts = 0
        stack = []
        self._in_script = False

        class P(html.parser.HTMLParser):
            def handle_decl(self, decl):
                if decl.lower().startswith("doctype html"):
                    scan.doctype = True

            def handle_starttag(self, tag, attrs):
                a = {k.lower(): (v or "") for k, v in attrs}
                scan.elements.append((tag, a))
                for k, v in a.items():
                    if k.startswith("on"):
                        scan.errors.append(f"atributo de evento en línea <{tag} {k}> (bloqueado por CSP; usar addEventListener)")
                    if v.strip().lower().startswith("javascript:"):
                        scan.errors.append(f"URL javascript: en <{tag} {k}>")
                if tag == "base":
                    scan.errors.append("<base> no está permitido")
                if tag == "script":
                    scan._in_script = not a.get("src")
                    scan._script_text = ""
                if tag not in VOID_TAGS:
                    stack.append((tag, self.getpos()[0]))

            def handle_startendtag(self, tag, attrs):
                self.handle_starttag(tag, attrs)
                if tag not in VOID_TAGS and stack and stack[-1][0] == tag:
                    stack.pop()

            def handle_endtag(self, tag):
                if tag in VOID_TAGS:
                    return
                if tag == "script" and scan._in_script:
                    if scan._script_text.strip():
                        scan.inline_scripts += 1
                    scan._in_script = False
                names = [t for t, _ in stack]
                if tag not in names:
                    scan.errors.append(f"etiqueta de cierre </{tag}> sin apertura (línea {self.getpos()[0]})")
                    return
                while stack:
                    t, line = stack.pop()
                    if t == tag:
                        break
                    if t not in OPTIONAL_END:
                        scan.errors.append(f"<{t}> abierta en la línea {line} no se cerró antes de </{tag}>")

            def handle_data(self, data):
                if scan._in_script:
                    scan._script_text += data
                elif not stack or stack[-1][0] not in ("style", "script"):
                    scan.text_parts.append(data)

        p = P(convert_charrefs=True)
        try:
            p.feed(text)
            p.close()
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"HTML no analizable: {type(e).__name__}: {e}")
        for t, line in stack:
            if t not in OPTIONAL_END:
                self.errors.append(f"<{t}> abierta en la línea {line} nunca se cerró")
        if self.inline_scripts:
            self.errors.append(f"{self.inline_scripts} script(s) en línea: el JavaScript debe ir en archivos .js")

    def tags(self, name):
        return [a for t, a in self.elements if t == name]

    @property
    def ids(self):
        return [a["id"] for _, a in self.elements if a.get("id")]

    @property
    def text(self):
        return " ".join(" ".join(self.text_parts).split())


def strip_css_comments(text):
    out, i, errors = [], 0, []
    while i < len(text):
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j < 0:
                errors.append("comentario /* sin cerrar")
                break
            i = j + 2
            continue
        out.append(text[i])
        i += 1
    return "".join(out), errors


def check_css(text):
    body, errors = strip_css_comments(text)
    depth, quote = 0, None
    for ch in body:
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                errors.append("llave '}' de más")
                depth = 0
    if quote:
        errors.append("cadena sin cerrar")
    if depth:
        errors.append(f"{depth} llave(s) '{{' sin cerrar")
    import re

    refs = re.findall(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", body)
    for m in re.findall(r"@import\s+(?:url\()?\s*['\"]?([^'\");\s]+)", body):
        refs.append(m)
    return errors, refs


JS_KEYWORDS_BEFORE_REGEX = {"return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw",
                            "case", "do", "else", "yield", "await"}


def js_heuristic(text):
    """Comprobación conservadora sin Node: cadenas, comentarios, plantillas y paréntesis balanceados."""
    errors, stack, i, n = [], [], 0, len(text)
    pairs = {")": "(", "]": "[", "}": "{"}
    last = ""  # último token significativo, para distinguir / de división y de regex
    line = 1
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
        if c in " \t\r\n":
            i += 1
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j < 0:
                errors.append(f"comentario sin cerrar (línea {line})")
                break
            line += text.count("\n", i, j)
            i = j + 2
            continue
        if c in "\"'":
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            if j >= n or text[j] != c:
                errors.append(f"cadena sin cerrar (línea {line})")
                break
            i, last = j + 1, "str"
            continue
        if c == "`":
            stack.append(("`", line))
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == "`":
                    stack.pop()
                    i += 1
                    break
                if text.startswith("${", i):
                    stack.append(("${", line))
                    i += 2
                    break
                if text[i] == "\n":
                    line += 1
                i += 1
            last = "str"
            continue
        if c == "/" and (last in ("", "(", ",", "=", ":", "[", "!", "&", "|", "?", "{", "}", ";", "op")
                         or last in JS_KEYWORDS_BEFORE_REGEX):
            j, in_class = i + 1, False
            while j < n and text[j] != "\n":
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "[":
                    in_class = True
                elif text[j] == "]":
                    in_class = False
                elif text[j] == "/" and not in_class:
                    break
                j += 1
            if j >= n or text[j] != "/":
                errors.append(f"expresión regular sin cerrar (línea {line})")
                break
            i, last = j + 1, "re"
            continue
        if c in "([{":
            stack.append((c, line))
            last = c
        elif c in ")]}":
            if c == "}" and stack and stack[-1][0] == "${":
                stack.pop()
                # continuar dentro de la plantilla
                i += 1
                while i < n:
                    if text[i] == "\\":
                        i += 2
                        continue
                    if text[i] == "`":
                        stack.pop()
                        i += 1
                        break
                    if text.startswith("${", i):
                        stack.append(("${", line))
                        i += 2
                        break
                    i += 1
                last = "str"
                continue
            if not stack or stack[-1][0] != pairs[c]:
                errors.append(f"'{c}' inesperado (línea {line})")
                break
            stack.pop()
            last = c
        elif c.isalnum() or c in "_$":
            j = i
            while j < n and (text[j].isalnum() or text[j] in "_$"):
                j += 1
            last = text[i:j]
            i = j
            continue
        else:
            last = "op" if c in "+-*%<>=!&|^~?:" else c
        i += 1
    if not errors and stack:
        errors.append(f"'{stack[-1][0]}' abierto en la línea {stack[-1][1]} sin cerrar")
    return errors


def node_check(node, path, module, timeout, cpu_s):
    if module:
        with open(path, "rb") as fh:
            data = fh.read()
        r = run_bounded([node, "--input-type=module", "--check"], data, timeout, cpu_s)
    else:
        r = run_bounded([node, "--check", path], b"", timeout, cpu_s)
    if r["timeout"]:
        return [f"node --check superó el tiempo ({timeout} s)"]
    if r["code"] != 0:
        lines = [ln for ln in r["stderr"].splitlines()
                 if ln.strip() and not ln.startswith("Node.js v") and not ln.lstrip().startswith("at ")]
        import re

        where = next((ln for ln in lines if re.search(r":\d+$", ln)), "")
        err = next((ln for ln in lines if "Error" in ln), lines[-1] if lines else "")
        msg = " | ".join(x for x in (where, err) if x)[:600] or f"código {r['code']}"
        rel = os.path.relpath(path, WEB_ROOT)
        return ["error de sintaxis: " + msg.replace("[stdin]", rel).replace(WEB_ROOT + "/", "")]
    return []


def match_simple_selector(selector, elements):
    import re

    for alt in selector.split(","):
        alt = alt.strip()
        m = re.fullmatch(r"([a-zA-Z][a-zA-Z0-9-]*|\*)?((?:[#.][A-Za-z0-9_-]+|\[[a-zA-Z-]+(?:=[\"']?[^\]\"']*[\"']?)?\])*)", alt)
        if not alt or not m:
            continue
        tag = (m.group(1) or "*").lower()
        parts = re.findall(r"[#.][A-Za-z0-9_-]+|\[[^\]]+\]", m.group(2))
        for t, a in elements:
            if tag != "*" and t != tag:
                continue
            ok = True
            for part in parts:
                if part[0] == "#":
                    ok = a.get("id") == part[1:]
                elif part[0] == ".":
                    ok = part[1:] in a.get("class", "").split()
                else:
                    name, _, val = part[1:-1].partition("=")
                    val = val.strip("\"'")
                    ok = name.lower() in a and (not _ or a[name.lower()] == val)
                if not ok:
                    break
            if ok:
                return True
    return False


def run_web_checks(spec, node, timeout, cpu_s):
    import re

    files = load_project()
    entry = spec.get("entry", "index.html")
    results = []

    def add(name, errors, visible=True, kind="web", ok_detail=""):
        results.append({"n": len(results) + 1, "kind": kind, "name": name, "visible": visible,
                        "ok": not errors, "status": "ok" if not errors else "fallo",
                        "detail": "\n".join(errors[:8]) if errors else ok_detail})

    html_files = sorted(f for f in files if f.endswith(".html"))
    css_files = sorted(f for f in files if f.endswith(".css"))
    js_files = sorted(f for f in files if f.endswith(".js"))
    struct = []
    if entry not in files:
        struct.append(f"falta el archivo de entrada {entry}")
    scans = {f: HtmlScan(files[f]) for f in html_files}
    if entry in scans:
        sc = scans[entry]
        if not sc.doctype:
            struct.append("falta <!DOCTYPE html>")
        for tag in ("html", "head", "body"):
            if not sc.tags(tag):
                struct.append(f"falta <{tag}>")
        if not sc.tags("title"):
            struct.append("falta <title>")
        if not any("charset" in a for a in sc.tags("meta")):
            struct.append('falta <meta charset="utf-8">')
        if not any(a.get("name", "").lower() == "viewport" for a in sc.tags("meta")):
            struct.append('falta <meta name="viewport"> (diseño para celular)')
    add("estructura", struct, ok_detail=f"{len(files)} archivos: {', '.join(sorted(files))[:300]}")

    modules = set()
    refs_errors, policy = [], []
    for f, sc in scans.items():
        add(f"html:{f}", sc.errors)
        dup = {i for i in sc.ids if sc.ids.count(i) > 1}
        if dup:
            refs_errors.append(f"{f}: id duplicado: {', '.join(sorted(dup))}")
        for tag, attr in (("script", "src"), ("link", "href"), ("img", "src"), ("source", "src"),
                          ("a", "href"), ("iframe", "src"), ("form", "action"), ("audio", "src"), ("video", "src")):
            for a in sc.tags(tag):
                url = a.get(attr, "")
                if not url:
                    continue
                if is_external(url):
                    if tag == "a":
                        continue  # un enlace externo no carga recursos
                    policy.append(f"{f}: recurso externo no permitido <{tag} {attr}=\"{url[:80]}\"> (usar archivos locales)")
                    continue
                target = resolve_ref(f, url)
                if target is None:
                    continue
                if url.strip().startswith("/"):
                    refs_errors.append(f"{f}: <{tag} {attr}=\"{url}\"> usa una ruta absoluta; usar rutas relativas")
                elif target.startswith(".."):
                    refs_errors.append(f"{f}: referencia fuera del proyecto: {url}")
                elif target not in files and not (tag == "a" and target + "/index.html" in files):
                    refs_errors.append(f"{f}: <{tag} {attr}=\"{url}\"> apunta a un archivo inexistente")
                elif tag == "script" and a.get("type", "").lower() == "module":
                    modules.add(target)
    all_ids = {i for sc in scans.values() for i in sc.ids}
    for f in css_files:
        errors, refs = check_css(files[f])
        add(f"css:{f}", errors)
        for url in refs:
            if is_external(url):
                policy.append(f"{f}: recurso externo no permitido en CSS: {url[:80]}")
            else:
                t = resolve_ref(f, url)
                if t is not None and t not in files:
                    refs_errors.append(f"{f}: url({url}) apunta a un archivo inexistente")
    for f in js_files:
        text = files[f]
        for m in re.findall(r"""(?:^|[;\s])(?:import|export)\s[^;'"]*?from\s*['"]([^'"]+)['"]|import\(\s*['"]([^'"]+)['"]""", text):
            url = m[0] or m[1]
            if is_external(url):
                policy.append(f"{f}: import externo no permitido: {url[:80]}")
            else:
                t = resolve_ref(f, url)
                modules.add(f)
                if t is not None and t not in files:
                    refs_errors.append(f"{f}: import '{url}' apunta a un archivo inexistente")
                elif t is not None:
                    modules.add(t)
        for url in re.findall(r"""(?:fetch|EventSource|WebSocket|importScripts)\(\s*['"`]([^'"`]+)""", text):
            if is_external(url):
                policy.append(f"{f}: conexión externa no permitida: {url[:80]}")
        for el_id in re.findall(r"""getElementById\(\s*['"]([^'"]+)['"]\s*\)""", text) + \
                re.findall(r"""querySelector(?:All)?\(\s*['"]#([A-Za-z0-9_-]+)['"]\s*\)""", text):
            if el_id not in all_ids:
                refs_errors.append(f"{f}: busca el id '{el_id}', que no existe en ningún HTML")
    for f in js_files:
        if node:
            errors = node_check(node, os.path.join(WEB_ROOT, f), f in modules, timeout, cpu_s)
            add(f"js:{f}", errors, ok_detail="sintaxis verificada con node --check")
        else:
            errors = js_heuristic(files[f])
            add(f"js:{f}", errors, ok_detail="comprobación heurística (node no disponible en el sandbox)")
    add("referencias", sorted(set(refs_errors)))
    add("politica_recursos_locales", sorted(set(policy)))

    entry_scan = scans.get(entry)
    js_all = "\n".join(files[f] for f in js_files)
    css_all = "\n".join(files[f] for f in css_files)
    for i, chk in enumerate(spec.get("checks", []), 1):
        kind, value = chk.get("tipo"), chk.get("valor", "")
        vis = bool(chk.get("visible", True))
        name = f"requisito {i}: {kind} {value}"[:120]
        if kind == "selector":
            ok = any(match_simple_selector(value, sc.elements) for sc in scans.values())
            err = [] if ok else [f"ningún elemento coincide con «{value}»"]
        elif kind == "texto":
            ok = entry_scan is not None and value.lower() in entry_scan.text.lower()
            err = [] if ok else [f"el texto «{value}» no aparece en {entry}"]
        elif kind == "js_contiene":
            err = [] if value in js_all else [f"el JavaScript no contiene «{value}»"]
        elif kind == "css_contiene":
            err = [] if value in css_all else [f"el CSS no contiene «{value}»"]
        else:
            err = [f"tipo de requisito desconocido: {kind}"]
        add(name, err, visible=vis, kind="requisito")
    return results


# --- Sondas de aislamiento (autodiagnóstico) -------------------------------

def probe(opts, timeout, cpu_s):
    r = {}
    r["uid"] = os.getuid()
    r["hostname"] = socket.gethostname()
    r["root_listing"] = sorted(os.listdir("/"))
    sensitive = ["/home", "/root", "/etc/passwd", "/etc/shadow", "/etc/ssh", "/run", "/var", "/sys", "/media",
                 "/mnt", "/srv", "/opt", "/snap", "/boot", "/home/diego", "/home/diego/vigilancia",
                 "/home/diego/.ollama", "/home/diego/.openclaw", "/run/user", "/var/run/docker.sock",
                 "/var/lib/mysql", "/run/mysqld", "/work/tests", "/dev/sda", "/dev/nvme0n1", "/dev/mem"]
    r["visible_sensitive"] = [p for p in sensitive if os.path.lexists(p)]
    blocked = []
    for host, port in [("1.1.1.1", 53), ("127.0.0.1", 11434), ("127.0.0.1", 11435), ("127.0.0.1", 18789),
                       ("127.0.0.1", 1880), ("127.0.0.1", 3306), ("100.100.100.100", 53)]:
        try:
            s = socket.create_connection((host, port), timeout=1)
            s.close()
            blocked.append({"dest": f"{host}:{port}", "blocked": False})
        except OSError as e:
            blocked.append({"dest": f"{host}:{port}", "blocked": True, "err": e.__class__.__name__})
    r["network"] = blocked
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect("\0/tmp/.X11-unix/X0")
        r["abstract_unix_blocked"] = False
    except OSError:
        r["abstract_unix_blocked"] = True
    writes = {}
    for p in ["/usr/cia_probe", "/work/code/cia_probe", "/work/harness/cia_probe", "/cia_probe", "/tmp/cia_probe"]:
        try:
            with open(p, "w") as fh:
                fh.write("x")
            writes[p] = True
        except OSError:
            writes[p] = False
    r["writable"] = writes
    r["pids_visible"] = len([d for d in os.listdir("/proc") if d.isdigit()])
    status = {}
    with open("/proc/self/status") as fh:
        for line in fh:
            k, _, v = line.partition(":")
            if k in ("NoNewPrivs", "Seccomp", "CapEff", "CapPrm", "CapBnd"):
                status[k] = v.strip()
    r["status"] = status
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        rc = libc.ptrace(0, 0, None, None)
        r["ptrace_blocked"] = rc != 0
        r["ptrace_errno"] = ctypes.get_errno()
    except Exception as e:  # noqa: BLE001
        r["ptrace_blocked"] = None
        r["ptrace_errno"] = str(e)
    try:
        os.unshare(os.CLONE_NEWUSER)
        r["userns_blocked"] = False
    except (OSError, AttributeError):
        r["userns_blocked"] = True
    r["dev"] = sorted(os.listdir("/dev"))
    r["env_keys"] = sorted(os.environ)
    stress = opts.get("stress") or []
    if "mem" in stress:
        code = "x = bytearray(%d)\nfor i in range(0, len(x), 4096): x[i] = 1\nprint('sobrevivio')" % (
            int(opts.get("mem_bytes", 600 * 1024 * 1024)))
        r["stress_mem"] = run_bounded([PY, "-I", "-S", "-c", code], b"", timeout, 60)
    if "fork" in stress:
        code = ("import os,time\nn=0\ntry:\n    for _ in range(400):\n        if os.fork()==0:\n"
                "            time.sleep(30); os._exit(0)\n        n+=1\nexcept OSError as e:\n"
                "    print('limite', n, e.errno)\nelse:\n    print('sin_limite', n)\n")
        r["stress_fork"] = run_bounded([PY, "-I", "-S", "-c", code], b"", timeout, 60)
    if "orphan" in stress:
        # Deja un nieto en otra sesión durmiendo: el anfitrión debe limpiarlo.
        subprocess.Popen([PY, "-I", "-S", "-c", "import time; time.sleep(600)"], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        r["stress_orphan"] = "lanzado"
    if "spin" in stress:
        while True:
            pass
    return r


def main():
    line = sys.stdin.buffer.readline(4_000_000)
    req = json.loads(line)
    nonce = bytes.fromhex(req["nonce"])
    nondump = set_nondumpable()
    timeout = int(req.get("case_timeout", 5))
    cpu_s = timeout + 1
    t0 = time.monotonic()
    result = {"harness_nondumpable": nondump}
    mode = req.get("mode")
    if mode == "probe":
        result["probe"] = probe(req.get("probe", {}), timeout, cpu_s)
    else:
        spec = req["spec"]
        cases, load_error = [], None
        if spec.get("mode") == "web":
            node = req.get("node") or ""
            cases = run_web_checks(spec, node if node and os.path.exists(node) else "", timeout, cpu_s)
        elif spec.get("mode") == "script":
            cases = run_script_cases(spec, timeout, cpu_s)
        else:
            cases, load_error = run_function_cases(spec, timeout, cpu_s)
        if req.get("tests_py"):
            cases += run_operator_tests(req["tests_py"], timeout, cpu_s)
        result.update(cases=cases, load_error=load_error, total=len(cases),
                      passed=sum(1 for c in cases if c.get("ok")),
                      failed=sum(1 for c in cases if not c.get("ok")))
    result["duration_ms"] = int((time.monotonic() - t0) * 1000)
    body = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    mac = hmac.new(nonce, body.encode(), hashlib.sha256).hexdigest()
    sys.stdout.write("CIA-RESULT " + body + " " + mac + "\n")
    sys.stdout.flush()
    wait_for_host()


def wait_for_host(limit_s=30):
    """Mantiene vivo el cgroup hasta que el anfitrión lea memory.events y cierre stdin."""
    deadline = time.monotonic() + limit_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        r, _, _ = select.select([0], [], [], remaining)
        if not r:
            return
        try:
            if not os.read(0, 4096):
                return
        except OSError:
            return


if __name__ == "__main__":
    main()
