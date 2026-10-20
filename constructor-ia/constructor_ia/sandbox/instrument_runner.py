"""Proceso hijo NO confiable: carga un instrumento y ejecuta medir(db, parametros).

Corre dentro del sandbox, separado del arnés. Su única salida hacia afuera es una tubería de mensajes JSON con el
arnés: los pedidos de consulta viajan por ella y el arnés los firma y los pasa al anfitrión, que decide si se
ejecutan. Lo que el instrumento imprima va a stderr. Solo biblioteca estándar.

Bibliotecas: los instrumentos anteriores que el anfitrión monte en /work/code/bib se pueden importar por su nombre de
módulo (reutilización de instrumentos); además se puede importar «investiga», con las primitivas del canal.
"""
import importlib.util
import json
import os
import sys
import traceback

MAX_LINE = 16_000_000
INVESTIGA = "/work/harness/investiga.py"
BIB = "/work/code/bib"


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
    try:
        spec = importlib.util.spec_from_file_location("instrumento", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["instrumento"] = mod
        spec.loader.exec_module(mod)
        fn = getattr(mod, "medir", None)
        if not callable(fn):
            send({"final": True, "ok": False, "exc": "AttributeError", "msg": "El instrumento no define medir(db, p)"})
            return
        value = fn(inv, params)
    except BaseException as e:  # noqa: BLE001 - cualquier fallo del instrumento se informa
        if isinstance(e, KeyboardInterrupt):
            raise
        send({"final": True, "ok": False, "exc": type(e).__name__, "msg": str(e)[:500], "tb": _trim_tb(e, (path,))})
        return
    send({"final": True, "ok": True, "valor": value})


if __name__ == "__main__":
    main()
