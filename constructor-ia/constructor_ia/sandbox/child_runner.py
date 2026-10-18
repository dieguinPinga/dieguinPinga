"""Proceso hijo NO confiable: carga solucion.py y atiende llamadas JSON.

Se ejecuta dentro del sandbox, en un proceso separado del arnés de pruebas,
para que el código generado no pueda leer los casos de prueba ni alterar los
resultados del arnés. Solo usa la biblioteca estándar.
"""
import importlib.util
import json
import os
import sys
import traceback

MAX_LINE = 1_000_000


def _trim_tb(exc: BaseException, path: str) -> str:
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename == path]
    lines = traceback.format_list(frames[-6:]) + traceback.format_exception_only(type(exc), exc)
    return "".join(lines)[-3000:]


def main() -> None:
    path = sys.argv[1]
    # Canal de protocolo en descriptores duplicados; los print() de la solución
    # van a stderr y la entrada estándar queda vacía.
    proto_in = os.fdopen(os.dup(0), "rb", buffering=0)
    proto_out = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.dup2(2, 1)

    def send(obj) -> None:
        try:
            data = json.dumps(obj, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            data = json.dumps({"id": obj.get("id"), "ok": False, "exc": "ResultadoNoSerializable",
                               "msg": f"El valor devuelto no es serializable a JSON: {e}"[:300]})
        if len(data) > MAX_LINE:
            data = json.dumps({"id": obj.get("id"), "ok": False, "exc": "ResultadoDemasiadoGrande",
                               "msg": "El valor devuelto es demasiado grande"})
        proto_out.write(data + "\n")
        proto_out.flush()

    try:
        spec = importlib.util.spec_from_file_location("solucion", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["solucion"] = mod
        spec.loader.exec_module(mod)
    except BaseException as e:  # noqa: BLE001 - se informa cualquier fallo de carga
        send({"ready": False, "error": _trim_tb(e, path), "exc": type(e).__name__})
        return
    send({"ready": True})

    buf = b""
    while True:
        chunk = proto_in.read(65536)
        if not chunk:
            return
        buf += chunk
        if len(buf) > MAX_LINE:
            return
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            req = json.loads(line)
            rid = req.get("id")
            fn = getattr(mod, str(req.get("fn")), None)
            if not callable(fn):
                send({"id": rid, "ok": False, "exc": "AttributeError",
                      "msg": f"No existe la función {req.get('fn')!r} en solucion.py"})
                continue
            try:
                value = fn(*req.get("args", []), **req.get("kwargs", {}))
            except BaseException as e:  # noqa: BLE001
                if isinstance(e, (KeyboardInterrupt,)):
                    raise
                send({"id": rid, "ok": False, "exc": type(e).__name__, "msg": str(e)[:500],
                      "tb": _trim_tb(e, path)})
                continue
            send({"id": rid, "ok": True, "value": value})


if __name__ == "__main__":
    main()
