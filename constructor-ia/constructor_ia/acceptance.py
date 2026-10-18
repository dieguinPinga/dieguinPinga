"""Validación estricta de misiones y de sus pruebas de aceptación.

Las pruebas de aceptación las define (o revisa y confirma) el operador humano;
el modelo nunca escribe ni modifica las pruebas que deciden la aprobación.
"""
from __future__ import annotations

import ast
import json
import keyword
import re

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
MAX_CASES = 40
MAX_CASE_JSON = 16_000
MAX_SPEC_JSON = 120_000
MAX_TESTS_PY = 20_000


class SpecError(ValueError):
    pass


def _text(d: dict, key: str, max_len: int, required: bool = True) -> str:
    v = d.get(key, "")
    if v is None:
        v = ""
    if not isinstance(v, str):
        raise SpecError(f"'{key}' debe ser texto")
    v = v.replace("\x00", "").strip()
    if required and not v:
        raise SpecError(f"'{key}' es obligatorio")
    if len(v) > max_len:
        raise SpecError(f"'{key}' supera {max_len} caracteres")
    return v


def _identifier(name: str, what: str) -> str:
    if not IDENT_RE.match(name or "") or keyword.iskeyword(name):
        raise SpecError(f"{what} inválido: use letras, números y guion bajo (p. ej. calcular_total)")
    return name


def _json_size(value) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError):
        raise SpecError("Los valores de los casos deben ser JSON válido (sin NaN ni infinitos)") from None


def validate_mission(data: dict, max_attempts_limit: int, default_attempts: int) -> dict:
    """Devuelve una misión normalizada o lanza SpecError con un mensaje legible."""
    if not isinstance(data, dict):
        raise SpecError("Formato inválido")
    title = _text(data, "title", 120)
    description = _text(data, "description", 6000)
    if data.get("authorize_execution") is not True:
        raise SpecError("Debe autorizar explícitamente la ejecución aislada de pruebas")
    if data.get("cases_reviewed") is not True:
        raise SpecError("Debe confirmar que revisó los casos de prueba")
    mode = data.get("mode", "funcion")
    if mode not in ("funcion", "script"):
        raise SpecError("Modo inválido")
    attempts = data.get("max_attempts", default_attempts)
    if not isinstance(attempts, int) or isinstance(attempts, bool) or not 1 <= attempts <= max_attempts_limit:
        raise SpecError(f"Intentos debe estar entre 1 y {max_attempts_limit}")

    raw_cases = data.get("cases", [])
    if not isinstance(raw_cases, list) or len(raw_cases) > MAX_CASES:
        raise SpecError(f"Se admiten como máximo {MAX_CASES} casos")
    cases = []
    spec: dict = {"mode": mode}
    if mode == "funcion":
        spec["function"] = _identifier(str(data.get("function", "")), "Nombre de función")
        for i, c in enumerate(raw_cases, 1):
            if not isinstance(c, dict):
                raise SpecError(f"Caso {i}: formato inválido")
            args = c.get("args", [])
            kwargs = c.get("kwargs", {}) or {}
            if not isinstance(args, list) or not isinstance(kwargs, dict):
                raise SpecError(f"Caso {i}: los argumentos deben ser una lista JSON")
            for k in kwargs:
                _identifier(k, f"Caso {i}: nombre de argumento")
            case = {"args": args, "kwargs": kwargs, "visible": bool(c.get("visible", True))}
            raises = c.get("raises")
            if raises:
                case["raises"] = _identifier(str(raises), f"Caso {i}: excepción")
            elif "expect" in c:
                case["expect"] = c["expect"]
            else:
                raise SpecError(f"Caso {i}: falta el resultado esperado")
            if _json_size(case) > MAX_CASE_JSON:
                raise SpecError(f"Caso {i}: demasiado grande")
            cases.append(case)
    else:
        for i, c in enumerate(raw_cases, 1):
            if not isinstance(c, dict):
                raise SpecError(f"Caso {i}: formato inválido")
            stdin = c.get("stdin", "")
            stdout = c.get("stdout", "")
            if not isinstance(stdin, str) or not isinstance(stdout, str):
                raise SpecError(f"Caso {i}: entrada y salida deben ser texto")
            if len(stdin) > 10_000 or len(stdout) > 10_000:
                raise SpecError(f"Caso {i}: demasiado grande")
            cases.append({"stdin": stdin.replace("\x00", ""), "stdout": stdout.replace("\x00", ""),
                          "visible": bool(c.get("visible", True))})
    spec["cases"] = cases
    tol = data.get("float_tol", 1e-9)
    if isinstance(tol, bool) or not isinstance(tol, (int, float)) or not 0 <= tol <= 0.1:
        raise SpecError("Tolerancia decimal inválida")
    spec["float_tol"] = float(tol)

    tests_py = data.get("tests_py") or ""
    if not isinstance(tests_py, str) or len(tests_py) > MAX_TESTS_PY:
        raise SpecError("Pruebas avanzadas demasiado largas")
    tests_py = tests_py.replace("\x00", "")
    if tests_py.strip():
        if mode != "funcion":
            raise SpecError("Las pruebas avanzadas solo están disponibles en modo función")
        try:
            tree = ast.parse(tests_py)
        except (SyntaxError, ValueError, RecursionError, MemoryError) as e:
            raise SpecError(f"Las pruebas avanzadas no son Python válido: {e}") from None
        if not any(isinstance(n, ast.FunctionDef) and n.name.startswith("test_") for n in tree.body):
            raise SpecError("Las pruebas avanzadas deben definir funciones test_*(sol)")
    if not cases and not tests_py.strip():
        raise SpecError("Defina al menos un caso de prueba: sin pruebas independientes no se puede aprobar nada")
    if _json_size(spec) > MAX_SPEC_JSON:
        raise SpecError("La especificación de pruebas es demasiado grande")
    return {"title": title, "description": description, "spec": spec, "tests_py": tests_py,
            "max_attempts": attempts}
