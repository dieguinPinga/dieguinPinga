"""Construcción de prompts con presupuesto de contexto acotado (num_ctx ~4096)."""
from __future__ import annotations

import json

CHARS_PER_TOKEN = 3.2

BUILDER_SYSTEM = (
    "Eres un programador Python cuidadoso. Escribes el archivo completo solucion.py.\n"
    "Reglas:\n"
    "- Responde con UN solo bloque ```python que contenga el archivo completo.\n"
    "- Usa solo la biblioteca estándar de Python 3.12. No hay red ni archivos externos.\n"
    "- No incluyas pruebas ni código que se ejecute al importar el módulo, salvo que se pida un programa.\n"
    "- Código claro, con manejo de casos borde. Explicación breve fuera del bloque, como máximo 3 líneas."
)

CHAT_SYSTEM = (
    "Eres el asistente de Constructor IA, en español rioplatense neutro y conciso. "
    "En esta conversación SOLO conversas: no ejecutas código, no instalas nada y no modificas el sistema. "
    "Si el usuario quiere que se programe y pruebe algo, sugerí crear una misión con casos de prueba; "
    "la ejecución solo ocurre cuando el usuario la autoriza en el formulario de misión."
)

DRAFT_SYSTEM = (
    "Convierte la conversación en un borrador de misión de programación Python. "
    "Responde SOLO con un objeto JSON con las claves: title (texto corto), description (texto detallado), "
    "mode (\"funcion\" o \"script\"), function (nombre de función en snake_case, si mode es funcion), "
    "cases (lista de 3 a 6 casos; en modo funcion cada caso es {\"args\": [...], \"expect\": valor}; "
    "en modo script cada caso es {\"stdin\": texto, \"stdout\": texto}). Los valores deben ser JSON válido."
)


def budget_chars(num_ctx: int, num_predict: int) -> int:
    return max(2000, int((num_ctx - num_predict - 200) * CHARS_PER_TOKEN))


def clip(text: str, limit: int, tail: bool = False) -> str:
    if len(text) <= limit:
        return text
    return ("…[recortado]\n" + text[-limit:]) if tail else (text[:limit] + "\n…[recortado]")


def _sig(spec: dict) -> str:
    if spec.get("mode") == "script":
        return ("Debe ser un PROGRAMA que lee de la entrada estándar (stdin) y escribe el resultado "
                "en la salida estándar (stdout) con print().")
    return f"Debe definir la función `{spec['function']}` en el nivel superior del módulo."


def _examples(spec: dict, limit: int) -> str:
    lines = []
    for c in spec.get("cases", []):
        if not c.get("visible", True):
            continue
        if spec.get("mode") == "script":
            lines.append(f"- Entrada:\n{c['stdin']}\n  Salida esperada:\n{c['stdout']}")
        else:
            call = ", ".join([json.dumps(a, ensure_ascii=False) for a in c.get("args", [])] +
                             [f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in c.get("kwargs", {}).items()])
            want = f"lanza {c['raises']}" if c.get("raises") else json.dumps(c.get("expect"), ensure_ascii=False)
            lines.append(f"- {spec['function']}({call}) → {want}")
    text = "\n".join(lines)
    hidden = sum(1 for c in spec.get("cases", []) if not c.get("visible", True))
    if hidden:
        text += f"\n(Además hay {hidden} casos ocultos que también deben pasar.)"
    return clip(text, limit)


def initial(description: str, spec: dict, budget: int) -> list[dict]:
    user = (f"Tarea:\n{clip(description, budget // 2)}\n\n{_sig(spec)}\n"
            "Los valores se intercambian como JSON (las tuplas se comparan como listas).\n\n"
            f"Ejemplos que debe cumplir:\n{_examples(spec, budget // 3) or '(sin ejemplos visibles)'}")
    return [{"role": "system", "content": BUILDER_SYSTEM}, {"role": "user", "content": user}]


def failure_report(result: dict | None, problems: list[str], limit: int) -> str:
    """Resume fallos para el modelo, sin revelar valores esperados de casos ocultos."""
    out = list(problems)
    if result:
        if result.get("load_error"):
            out.append("Error al cargar el módulo:\n" + clip(result["load_error"], 1200, tail=True))
        for c in result.get("cases", []):
            if c.get("ok"):
                continue
            if c.get("visible"):
                if c["kind"] == "script":
                    desc = f"Caso {c['n']}: entrada {c.get('stdin', '')!r}"
                    if c.get("status") == "fallo":
                        desc += f"\n  salida obtenida: {c.get('got', '')!r}\n  esperada: {c.get('expected', '')!r}"
                else:
                    desc = f"Caso {c['n']}: args={json.dumps(c.get('args'), ensure_ascii=False)[:300]}"
                    if c.get("status") == "fallo":
                        desc += f" → obtenido {c.get('got')} ; esperado {c.get('expected')}"
                desc += f"\n  {c.get('detail', '')}"
            else:
                desc = f"Caso oculto {c['kind']} {c['n']}: {c.get('status')}"
                if c.get("status") != "fallo":
                    desc += f" — {c.get('detail', '')}"
            if c.get("tb"):
                desc += "\n  " + clip(c["tb"], 600, tail=True)
            elif c.get("stderr"):
                desc += "\n  stderr: " + clip(c["stderr"], 400, tail=True)
            out.append(desc)
    return clip("\n".join(out), limit)


def repair(description: str, spec: dict, code: str, report: str, budget: int) -> list[dict]:
    user = (f"Tarea:\n{clip(description, budget // 4)}\n\n{_sig(spec)}\n\n"
            f"Ejemplos:\n{_examples(spec, budget // 6) or '(sin ejemplos visibles)'}\n\n"
            f"Versión anterior de solucion.py:\n```python\n{clip(code, budget // 3)}```\n\n"
            f"Resultados reales de las pruebas (errores a corregir):\n{report}\n\n"
            "Corrige el programa y devuelve el archivo completo en un único bloque ```python.")
    return [{"role": "system", "content": BUILDER_SYSTEM}, {"role": "user", "content": user}]


def _history_text(spec: dict) -> str:
    hist = (spec.get("revision") or {}).get("history") or []
    if not hist:
        return ""
    return "Cambios ya incorporados en revisiones anteriores:\n" + "\n".join(
        f"- r{h['revision']}: {h['instruccion']}" for h in hist) + "\n\n"


def revision(description: str, spec: dict, code: str, budget: int) -> list[dict]:
    """Primer intento de una REVISIÓN: modificar el programa existente según la instrucción humana."""
    rev = spec["revision"]
    user = (f"Propósito original del programa:\n{clip(description, budget // 5)}\n\n" + _history_text(spec)
            + f"NUEVA MODIFICACIÓN PEDIDA POR EL OPERADOR:\n{clip(rev['instruction'], 1500)}\n\n"
            "Esto es una MODIFICACIÓN del programa existente, NO un programa nuevo. Conservá todo el "
            "comportamiento que no se pide cambiar: todas las pruebas anteriores deben seguir pasando.\n"
            f"{_sig(spec)}\n\nEjemplos que debe cumplir (incluye las pruebas nuevas):\n"
            f"{_examples(spec, budget // 6) or '(sin ejemplos visibles)'}\n\n"
            f"Programa actual (solucion.py):\n```python\n{clip(code, budget // 2)}```\n\n"
            "Devolvé el archivo solucion.py completo y modificado en un único bloque ```python.")
    return [{"role": "system", "content": BUILDER_SYSTEM}, {"role": "user", "content": user}]
