"""Proceso hijo NO confiable: carga un instrumento y ejecuta medir(db, parametros).

Corre dentro del sandbox, separado del arnés. Su única salida hacia afuera es una tubería de mensajes JSON con el
arnés: los pedidos de consulta viajan por ella y el arnés los firma y los pasa al anfitrión, que decide si se
ejecutan. Lo que el instrumento imprima va a stderr. Solo biblioteca estándar.

Bibliotecas: los instrumentos anteriores que el anfitrión monte en /work/code/bib se pueden importar por su nombre de
módulo (reutilización de instrumentos); además se puede importar «investiga», con las primitivas del canal.
"""
import importlib.util
import itertools
import json
import linecache
import os
import sys
import traceback

MAX_LINE = 16_000_000
INVESTIGA = "/work/harness/investiga.py"
BIB = "/work/code/bib"


MAX_EXC = 8
MAX_LINEAS_TRAZA = 8
_AUSENTE = object()


def corto(v, n: int = 90, profundidad: int = 0) -> str:
    """Representación ACOTADA de un valor: nunca recorre ni ordena una colección entera (costo casi constante,
    aunque el diccionario tenga miles de claves)."""
    if v is _AUSENTE:
        return "(sin definir)"
    try:
        if isinstance(v, dict):
            if profundidad:
                return f"{{…{len(v)} claves}}"
            items = [f"{corto(k, 30, 1)}: {corto(x, 40, 1)}" for k, x in itertools.islice(v.items(), 4)]
            s = "{" + ", ".join(items) + (f", …+{len(v) - 4}" if len(v) > 4 else "") + "}"
        elif isinstance(v, (list, tuple, set, frozenset)):
            if profundidad:
                return f"[…{len(v)} elementos]"
            items = [corto(x, 40, 1) for x in itertools.islice(iter(v), 4)]
            s = "[" + ", ".join(items) + (f", …+{len(v) - 4}" if len(v) > 4 else "") + "]"
        elif isinstance(v, str):
            s = repr(v[:n])
        else:
            s = repr(v)
    except Exception:  # noqa: BLE001 — una representación que falla no rompe la medición
        s = f"<{type(v).__name__}>"
    return s if len(s) <= n else s[:n - 1] + "…"


class Traza:
    """Evidencia de ejecución por línea candidata (pedida por el anfitrión, sin saber cuál es la causa): cuántas
    veces se ejecutó, qué valores tenían antes y después los nombres que define, qué valores usó (dos primeras
    ejecuciones y la última), cuántas veces fue verdadera la condición de una instrucción compuesta y, al terminar
    medir, el valor final de cada nombre. Solo observa el código del instrumento."""

    def __init__(self, spec: dict):
        self.lineas = {}
        for k, v in list((spec or {}).items())[:MAX_LINEAS_TRAZA]:
            try:
                cuerpo = v.get("cuerpo")
                self.lineas[int(k)] = {"define": [str(x) for x in (v.get("define") or [])][:4],
                                       "usa": [str(x) for x in (v.get("usa") or [])][:6],
                                       "cuerpo": [int(cuerpo[0]), int(cuerpo[1])] if cuerpo else None}
            except (TypeError, ValueError, AttributeError, IndexError):
                continue
        self.datos = {l: {"veces": 0, "verdadera": 0, "interrumpida": 0, "muestras": [], "ultima": None}
                      for l in self.lineas}
        self.pendientes: dict = {}
        self.ultima_linea: dict = {}
        self.final: dict = {}

    def _valores(self, frame, nombres) -> dict:
        loc = frame.f_locals
        return {n: corto(loc.get(n, _AUSENTE)) for n in nombres}

    def _cerrar(self, frame, pendiente, siguiente) -> None:
        linea, antes, usa, interrumpida = pendiente
        spec, d = self.lineas[linea], self.datos[linea]
        if interrumpida:      # una excepción la cortó: no terminó, y «después» no es lo que habría dejado
            d["interrumpida"] += 1
        if spec["cuerpo"] and siguiente is not None and spec["cuerpo"][0] <= siguiente <= spec["cuerpo"][1]:
            d["verdadera"] += 1
        muestra = {"usa": usa, "antes": antes, "despues": self._valores(frame, spec["define"]),
                   **({"interrumpida": True} if interrumpida else {})}
        if len(d["muestras"]) < 2:
            d["muestras"].append(muestra)
        d["ultima"] = muestra

    INTERNOS = ("<lambda>", "<listcomp>", "<dictcomp>", "<setcomp>", "<genexpr>")

    def linea(self, frame) -> None:
        # Una ejecución = el INICIO de una instrucción en su propio marco. Las comprensiones (en línea desde
        # Python 3.12) y las lambdas repiten eventos sobre la misma línea: no son ejecuciones nuevas.
        if frame.f_code.co_name in self.INTERNOS:
            return
        fid = id(frame)
        if self.ultima_linea.get(fid) == frame.f_lineno:
            return
        self.ultima_linea[fid] = frame.f_lineno
        pendiente = self.pendientes.pop(fid, None)
        if pendiente:
            self._cerrar(frame, pendiente, frame.f_lineno)
        spec = self.lineas.get(frame.f_lineno)
        if spec is not None:
            self.datos[frame.f_lineno]["veces"] += 1
            self.pendientes[id(frame)] = (frame.f_lineno, self._valores(frame, spec["define"]),
                                          self._valores(frame, spec["usa"]), False)

    def excepcion(self, frame) -> None:
        if frame.f_code.co_name in self.INTERNOS:
            return
        pendiente = self.pendientes.get(id(frame))
        if pendiente and pendiente[0] == frame.f_lineno:
            self.pendientes[id(frame)] = pendiente[:3] + (True,)

    def retorno(self, frame) -> None:
        if frame.f_code.co_name in self.INTERNOS:
            return
        self.ultima_linea.pop(id(frame), None)
        pendiente = self.pendientes.pop(id(frame), None)
        if pendiente:
            self._cerrar(frame, pendiente, None)
        if frame.f_code.co_name == "medir":
            nombres = {n for s in self.lineas.values() for n in s["define"]}
            self.final = self._valores(frame, sorted(nombres))

    def report(self, completa: bool = True) -> dict:
        return {"lineas": {str(l): d for l, d in self.datos.items()}, "final": self.final, "completa": completa}


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

    def __init__(self, paths, traza: "Traza | None" = None):
        self.paths = tuple(paths)
        self.traza = traza
        self.seen: dict = {}      # id(excepción) → clave (una excepción que atraviesa varios marcos se cuenta una vez)
        self.keep: list = []      # referencias para que id() no se reutilice mientras se registra
        self.items: dict = {}     # clave (tipo, mensaje, archivo, línea) → registro con conteo

    def _mine(self, code) -> bool:
        return code.co_filename in self.paths or code.co_filename.startswith(BIB)

    def global_trace(self, frame, event, arg):
        if event != "call" or not self._mine(frame.f_code):
            return None
        # Eventos por línea solo si se pidió una traza y en el archivo del instrumento (costo acotado).
        frame.f_trace_lines = bool(self.traza) and frame.f_code.co_filename in self.paths
        return self.local_trace

    def local_trace(self, frame, event, arg):
        if self.traza is not None and frame.f_code.co_filename in self.paths:
            if event == "line":
                self.traza.linea(frame)
            elif event == "return":
                self.traza.retorno(frame)
            elif event == "exception":
                self.traza.excepcion(frame)
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
        traza = Traza(init["traza"]) if isinstance(init.get("traza"), dict) else None
        exc_log = Excepciones((path,), traza)
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
              "lugar": _ultimo_lugar(e, (path,)), "excepciones": exc_log.report() if exc_log else [],
              "traza": exc_log.traza.report(completa=False) if exc_log and exc_log.traza else None})
        return
    send({"final": True, "ok": True, "valor": value, "excepciones": exc_log.report(),
          "traza": exc_log.traza.report() if exc_log.traza else None})


if __name__ == "__main__":
    main()
