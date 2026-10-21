"""Proceso hijo NO confiable: carga un instrumento y ejecuta medir(db, parametros).

Corre dentro del sandbox, separado del arnés. Su única salida hacia afuera es una tubería de mensajes JSON con el
arnés: los pedidos de consulta viajan por ella y el arnés los firma y los pasa al anfitrión, que decide si se
ejecutan. Lo que el instrumento imprima va a stderr. Solo biblioteca estándar.

Bibliotecas: los instrumentos anteriores que el anfitrión monte en /work/code/bib se pueden importar por su nombre de
módulo (reutilización de instrumentos); además se puede importar «investiga», con las primitivas del canal.
"""
import importlib.util
import json
import linecache
import os
import sys
import traceback

MAX_LINE = 16_000_000
INVESTIGA = "/work/harness/investiga.py"
BIB = "/work/code/bib"


MAX_EXC = 8


def _rel(filename: str) -> str:
    return "bib/" + os.path.basename(filename) if filename.startswith(BIB) else "instrumento.py"


def _lugar(filename: str, lineno: int, funcion: str) -> dict:
    return {"archivo": _rel(filename), "linea": lineno, "funcion": funcion,
            "codigo": linecache.getline(filename, lineno).strip()[:200]}


class Excepciones:
    """Registra las excepciones que ocurren en el código del instrumento (y de sus bibliotecas), AUNQUE el propio
    instrumento las capture. Un programa que atrapa un error y devuelve un resultado vacío «terminó bien» pero no
    midió nada; este registro permite distinguirlo. Es un diagnóstico, no una barrera de seguridad: sólo observa
    marcos del instrumento y apaga los eventos por línea (costo bajo)."""

    def __init__(self, paths):
        self.paths = tuple(paths)
        self.seen: dict = {}      # id(excepción) → clave (una excepción que atraviesa varios marcos se cuenta una vez)
        self.keep: list = []      # referencias para que id() no se reutilice mientras se registra
        self.items: dict = {}     # clave (tipo, mensaje, archivo, línea) → registro con conteo

    def _mine(self, code) -> bool:
        return code.co_filename in self.paths or code.co_filename.startswith(BIB)

    def global_trace(self, frame, event, arg):
        if event != "call" or not self._mine(frame.f_code):
            return None
        frame.f_trace_lines = False
        return self.local_trace

    def local_trace(self, frame, event, arg):
        if event == "exception":
            exc = arg[1]
            if id(exc) not in self.seen and len(self.keep) < 1000:
                lugar = _lugar(frame.f_code.co_filename, frame.f_lineno, frame.f_code.co_name)
                key = (type(exc).__name__, str(exc)[:200], lugar["archivo"], lugar["linea"])
                self.seen[id(exc)] = key
                self.keep.append(exc)
                if key in self.items:
                    self.items[key]["veces"] += 1
                elif len(self.items) < MAX_EXC:
                    self.items[key] = {"tipo": key[0], "mensaje": key[1], **lugar, "veces": 1}
        return self.local_trace

    def discard(self, exc: BaseException) -> None:
        """La excepción que terminó el programa no fue capturada: se informa aparte, no como capturada."""
        key = self.seen.get(id(exc))
        if key in self.items:
            self.items[key]["veces"] -= 1
            if self.items[key]["veces"] <= 0:
                del self.items[key]

    def report(self) -> list:
        return list(self.items.values())


def _ultimo_lugar(exc: BaseException, paths) -> dict | None:
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename in paths or f.filename.startswith(BIB)]
    if not frames:
        return None
    f = frames[-1]
    return _lugar(f.filename, f.lineno, f.name)


def _trim_tb(exc: BaseException, paths) -> str:
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename in paths or f.filename.startswith(BIB)]
    lines = traceback.format_list(frames[-6:]) + traceback.format_exception_only(type(exc), exc)
    return "".join(lines)[-3000:]


def main() -> None:
    path = sys.argv[1]
    proto_in = os.fdopen(os.dup(0), "rb")
    proto_out = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.dup2(2, 1)

    def send(obj) -> None:
        try:
            data = json.dumps(obj, ensure_ascii=False, default=str)
        except (TypeError, ValueError) as e:
            data = json.dumps({"final": True, "ok": False, "exc": "ResultadoNoSerializable",
                               "msg": f"El resultado no es serializable a JSON: {e}"[:300]})
        proto_out.write(data + "\n")
        proto_out.flush()

    def receive() -> dict:
        line = proto_in.readline(MAX_LINE)
        if not line:
            raise SystemExit(0)
        return json.loads(line)

    init = receive()
    spec = importlib.util.spec_from_file_location("investiga", INVESTIGA)
    inv = importlib.util.module_from_spec(spec)
    sys.modules["investiga"] = inv
    spec.loader.exec_module(inv)
    inv._schema = init.get("esquema") or {}
    inv._budget = dict(init.get("presupuesto") or {})

    def transport(msg):
        send({"rpc": True, **msg})
        return receive()

    inv._transport = transport
    sys.path[:] = [BIB] + [p for p in sys.path if p.startswith("/usr/")]
    params = init.get("parametros") or {}
    exc_log = None
    try:
        spec = importlib.util.spec_from_file_location("instrumento", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["instrumento"] = mod
        spec.loader.exec_module(mod)
        fn = getattr(mod, "medir", None)
        if not callable(fn):
            send({"final": True, "ok": False, "exc": "AttributeError", "msg": "El instrumento no define medir(db, p)"})
            return
        exc_log = Excepciones((path,))
        sys.settrace(exc_log.global_trace)
        try:
            value = fn(inv, params)
        finally:
            sys.settrace(None)
    except BaseException as e:  # noqa: BLE001 - cualquier fallo del instrumento se informa
        if isinstance(e, KeyboardInterrupt):
            raise
        if exc_log:
            exc_log.discard(e)
        send({"final": True, "ok": False, "exc": type(e).__name__, "msg": str(e)[:500], "tb": _trim_tb(e, (path,)),
              "lugar": _ultimo_lugar(e, (path,)), "excepciones": exc_log.report() if exc_log else []})
        return
    send({"final": True, "ok": True, "valor": value, "excepciones": exc_log.report()})


if __name__ == "__main__":
    main()
