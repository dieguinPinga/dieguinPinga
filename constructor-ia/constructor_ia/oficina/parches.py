"""Experimento 7.3: reparación mediante PARCHES MÍNIMOS (OFICINA.md, «Experimento 7.3»).

En lugar de regenerar el archivo, el programador propone cambios localizados que el coordinador valida y aplica de
forma DETERMINISTA sobre una copia del código. Nada de lo que escribe el modelo se ejecuta como instrucción: el
parche es un dato (texto original exacto → texto de reemplazo) y la única operación es un reemplazo de líneas.

1. LOCALIZACIÓN (`localizar`), solo con información permitida y análisis estático:
   - anclas: las citas del revisor que el coordinador comprobó en el código, y las líneas con literales de texto que
     aparecen en la diferencia del caso sintético (salida de la referencia y del candidato para ESE caso público).
     Una cita se ubica con el MISMO criterio con que se verificó (revisor.cita_en_codigo: texto normalizado), en la
     ventana mínima de líneas consecutivas que la contiene: una cita de varias líneas ancla todas sus líneas. Cada
     cita queda registrada con sus líneas o con el motivo por el que no ancló nada (corrección A del 7.3);
   - cada ancla se lleva a su sentencia en el árbol sintáctico; si su bloque tiene menos de MIN_HERMANAS
     sentencias, se sube a la sentencia que lo contiene; la zona es esa sentencia con hasta HERMANAS vecinas antes y
     después (se achica hasta caber en MAX_LINEAS_ZONA);
   - zonas superpuestas o contiguas se unen. Sin anclas: la función completa (queda registrado).
   Nunca se usan las pruebas ocultas.
2. PARCHE (`interpretar_parche` + `aplicar`): JSON {"cambios": [{"original", "reemplazo"}], "justificacion"}.
   Se rechaza si: no es JSON o falta algo; no hay cambios o son más de MAX_CAMBIOS; un original está vacío, no
   aparece (comparando líneas completas; solo se ignoran los espacios finales) o aparece más de una vez; un cambio
   cae fuera de las zonas autorizadas; dos cambios se superponen; un reemplazo es igual al original o demasiado
   largo; el resultado no es Python válido o ya no define la función. Recién entonces se aplica.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re

from .. import codeextract, prompts

HERMANAS = 2
MAX_VENTANA_CITA = 6       # líneas consecutivas en las que se busca una cita (revisor acepta citas de hasta 2 líneas,
                           # pero una cita escrita en una línea puede abarcar varias líneas del código)
MIN_HERMANAS = 3
MAX_LINEAS_ZONA = 30
MAX_LINEAS_ZONAS = 80
MAX_CAMBIOS = 3
MAX_LINEAS_REEMPLAZO = 25
MAX_JUSTIFICACION = 600


class ParcheRechazado(ValueError):
    pass


# Localización ------------------------------------------------------------------------------------------------------

def _cadena(tree: ast.AST, linea: int) -> list[tuple[list, int, ast.AST]]:
    """[(bloque, índice, nodo dueño del bloque)] desde el módulo hasta la sentencia más profunda que contiene la línea."""
    cadena, cuerpo, dueno = [], tree.body, tree
    while True:
        for i, st in enumerate(cuerpo):
            if st.lineno <= linea <= (st.end_lineno or st.lineno):
                cadena.append((cuerpo, i, dueno))
                hijos = [getattr(st, campo) for campo in ("body", "orelse", "finalbody") if getattr(st, campo, None)]
                hijos += [h.body for h in getattr(st, "handlers", [])]
                sub = next((b for b in hijos if any(s.lineno <= linea <= (s.end_lineno or s.lineno) for s in b)), None)
                if sub is None:
                    return cadena
                cuerpo, dueno = sub, st
                break
        else:
            return cadena


def _zona(tree: ast.AST, linea: int) -> tuple[int, int] | None:
    cadena = _cadena(tree, linea)
    if not cadena:
        return None
    # Un bloque muy corto (p. ej. «ignorar(...); continue») dice poco: se sube a la sentencia que lo contiene.
    while len(cadena) > 1 and len(cadena[-1][0]) < MIN_HERMANAS and not isinstance(
            cadena[-1][2], (ast.FunctionDef, ast.AsyncFunctionDef)):
        cadena.pop()
    cuerpo, i, _ = cadena[-1]
    for k in range(HERMANAS, -1, -1):
        a = cuerpo[max(0, i - k)].lineno
        b = cuerpo[min(len(cuerpo) - 1, i + k)].end_lineno or cuerpo[i].lineno
        if b - a + 1 <= MAX_LINEAS_ZONA:
            return a, b
    return max(1, linea - 5), linea + 5


def _norm(t: str) -> str:
    return " ".join(t.split())


def lineas_de_cita(codigo: str, cita: str) -> list[int]:
    """Líneas (1..n) donde aparece la cita, con el criterio de revisor.cita_en_codigo (espacios normalizados): para
    cada aparición, la ventana MÍNIMA de líneas consecutivas cuyo texto unido la contiene. Así una cita de varias
    líneas ancla todas ellas, y una de una línea se comporta como antes (todas las líneas que la contienen)."""
    n = _norm(cita)
    if not n:
        return []
    lineas = [_norm(x) for x in codigo.splitlines()]
    for w in range(1, MAX_VENTANA_CITA + 1):
        encontradas = []
        for i in range(len(lineas) - w + 1):
            if n in " ".join(x for x in lineas[i:i + w] if x):
                encontradas.extend(range(i + 1, i + w + 1))
        if encontradas:
            return sorted(set(encontradas))
    return []


def anclas(codigo: str, citas: list[str], literales: list[str], registro: list | None = None) -> list[dict]:
    """Líneas del código que anclan la localización: citas comprobadas y literales de la evidencia sintética.
    registro (opcional) recibe, por cada cita, sus líneas o el motivo por el que no ancló."""
    out = []
    for c in citas:
        ls = lineas_de_cita(codigo, c)
        if registro is not None:
            registro.append({"texto": c, "verificada": True, "lineas": ls,
                             "motivo": None if ls else f"no se ubicó en {MAX_VENTANA_CITA} líneas consecutivas"})
        for i in ls:
            out.append({"linea": i, "origen": "cita del revisor", "texto": c})
    try:
        tree = ast.parse(codigo)
    except SyntaxError:
        return out
    buscados = set(literales)
    for nodo in ast.walk(tree):
        if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str) and nodo.value in buscados:
            out.append({"linea": nodo.lineno, "origen": "literal del caso sintético", "texto": nodo.value})
    vistos, unicas = set(), []
    for a in out:
        if a["linea"] not in vistos:
            vistos.add(a["linea"])
            unicas.append(a)
    return unicas


def literales_de_la_diferencia(comp: dict | None) -> list[str]:
    """Textos (hojas str) de los valores que difieren en el caso sintético: lo que falta y lo que sobra."""
    out = []

    def hojas(v):
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict):
            for x in v.values():
                hojas(x)
        elif isinstance(v, list):
            for x in v:
                hojas(x)
    for d in ((comp or {}).get("detalle") or {}).values():
        for k in ("faltan", "sobran") if "faltan" in d else ("contrato", "tu_intento"):
            hojas(d.get(k))
    return sorted(set(out))


def localizar(codigo: str, funcion: str, citas: list[str], literales: list[str],
              citas_no_verificadas: list[str] = ()) -> dict:
    """{"zonas": [(a, b)], "anclas": [...], "citas": [...], "metodo": ...}. Determinista y sin pruebas ocultas.
    «citas» registra cada cita del revisor: las verificadas con sus líneas (o por qué no anclaron) y las no
    verificadas como descartadas."""
    tree = ast.parse(codigo)
    registro: list[dict] = []
    an = anclas(codigo, citas, literales, registro)
    registro += [{"texto": c, "verificada": False, "lineas": [], "motivo": "no verificada: no aparece en el código"}
                 for c in citas_no_verificadas]
    rangos = sorted(z for z in (_zona(tree, a["linea"]) for a in an) if z)
    unidas: list[list[int]] = []
    for a, b in rangos:
        if unidas and a <= unidas[-1][1] + 1:
            unidas[-1][1] = max(unidas[-1][1], b)
        else:
            unidas.append([a, b])
    zonas, total = [], 0
    for a, b in unidas:
        if total + (b - a + 1) > MAX_LINEAS_ZONAS:
            continue
        zonas.append((a, b))
        total += b - a + 1
    if zonas:
        return {"zonas": zonas, "anclas": an, "citas": registro,
                "metodo": "anclas (citas comprobadas y evidencia sintética)"}
    fn = next((n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == funcion),
              None)
    a, b = (fn.lineno, fn.end_lineno) if fn else (1, len(codigo.splitlines()))
    return {"zonas": [(a, b)], "anclas": an, "citas": registro, "metodo": "sin anclas: la función completa"}


def contexto_de_lectura(codigo: str, funcion: str, zonas: list[tuple[int, int]]) -> list[int]:
    """Líneas de solo lectura: la firma de la función y sus asignaciones de primer nivel fuera de las zonas."""
    tree = ast.parse(codigo)
    fn = next((n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == funcion),
              None)
    if fn is None:
        return []
    lineas = list(range(fn.lineno, fn.body[0].lineno))
    for st in fn.body:
        if isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            lineas += range(st.lineno, (st.end_lineno or st.lineno) + 1)
    return sorted(n for n in set(lineas) if not any(a <= n <= b for a, b in zonas))


def texto_zonas(codigo: str, funcion: str, loc: dict) -> str:
    lineas = codigo.splitlines()
    partes = []
    ctx = contexto_de_lectura(codigo, funcion, loc["zonas"])
    if ctx:
        partes.append("Contexto de SOLO LECTURA (no se puede cambiar):\n"
                      + "\n".join(f"{n:3d}| {lineas[n - 1]}" for n in ctx))
    for k, (a, b) in enumerate(loc["zonas"], 1):
        partes.append(f"ZONA AUTORIZADA {k} (líneas {a}–{b}):\n"
                      + "\n".join(f"{n:3d}| {lineas[n - 1]}" for n in range(a, b + 1)))
    return "\n\n".join(partes)


# Parche -------------------------------------------------------------------------------------------------------------

def interpretar_parche(texto: str, funcion: str) -> dict:
    t = (texto or "").strip()
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        raise ParcheRechazado("la respuesta no contiene un objeto JSON")
    try:
        d = json.loads(m.group(0))
    except ValueError as e:
        raise ParcheRechazado(f"JSON ilegible: {e}") from None
    if not isinstance(d, dict) or not isinstance(d.get("cambios"), list):
        raise ParcheRechazado("falta la lista «cambios»")
    cambios = d["cambios"]
    if not cambios:
        raise ParcheRechazado("el parche está vacío: no tiene cambios")
    if len(cambios) > MAX_CAMBIOS:
        raise ParcheRechazado(f"el parche tiene {len(cambios)} cambios; el máximo es {MAX_CAMBIOS}")
    out = []
    for i, c in enumerate(cambios, 1):
        if not isinstance(c, dict) or not isinstance(c.get("original"), str) or not isinstance(c.get("reemplazo"), str):
            raise ParcheRechazado(f"cambio {i}: necesita «original» y «reemplazo» como textos")
        if not c["original"].strip():
            raise ParcheRechazado(f"cambio {i}: el fragmento original está vacío")
        if re.search(rf"^\s*def\s+{re.escape(funcion)}\s*\(", c["reemplazo"], re.M):
            raise ParcheRechazado(f"cambio {i}: el reemplazo reescribe la función completa; se pide un parche mínimo")
        if len(c["reemplazo"].splitlines()) > MAX_LINEAS_REEMPLAZO:
            raise ParcheRechazado(f"cambio {i}: el reemplazo supera {MAX_LINEAS_REEMPLAZO} líneas")
        out.append({"original": c["original"], "reemplazo": c["reemplazo"]})
    return {"cambios": out, "justificacion": str(d.get("justificacion") or "").strip()[:MAX_JUSTIFICACION]}


def _lineas(texto: str) -> list[str]:
    ls = [x.rstrip() for x in texto.replace("\r\n", "\n").split("\n")]
    while ls and not ls[0].strip():
        ls.pop(0)
    while ls and not ls[-1].strip():
        ls.pop()
    return ls


def clave(parche: dict) -> str:
    """Huella del parche (sin espacios finales): detecta parches repetidos."""
    norm = [["\n".join(_lineas(c["original"])), "\n".join(_lineas(c["reemplazo"]))] for c in parche["cambios"]]
    return hashlib.sha256(json.dumps(norm, ensure_ascii=False).encode()).hexdigest()


def aplicar(codigo: str, parche: dict, zonas: list[tuple[int, int]], spec: dict) -> tuple[str, list[dict]]:
    """Aplica el parche sobre una COPIA del código, o lanza ParcheRechazado con el motivo."""
    lineas = codigo.splitlines()
    sin_fin = [x.rstrip() for x in lineas]
    aplicados = []
    for i, c in enumerate(parche["cambios"], 1):
        orig = _lineas(c["original"])
        if not orig:
            raise ParcheRechazado(f"cambio {i}: el fragmento original está vacío")
        k = len(orig)
        pos = [j for j in range(len(sin_fin) - k + 1) if sin_fin[j:j + k] == orig]
        if not pos:
            raise ParcheRechazado(f"cambio {i}: el fragmento original no aparece en el código (copialo exacto, con su "
                                  "sangría y líneas completas)")
        if len(pos) > 1:
            raise ParcheRechazado(f"cambio {i}: el fragmento original aparece {len(pos)} veces (líneas "
                                  f"{', '.join(str(p + 1) for p in pos)}): incluí más líneas para que sea inequívoco")
        a, b = pos[0] + 1, pos[0] + k
        if not any(za <= a and b <= zb for za, zb in zonas):
            raise ParcheRechazado(f"cambio {i}: las líneas {a}–{b} están fuera de las zonas autorizadas")
        nuevo = _lineas(c["reemplazo"]) if c["reemplazo"].strip() else []
        if nuevo == orig:
            raise ParcheRechazado(f"cambio {i}: el reemplazo es igual al original")
        aplicados.append({"cambio": i, "lineas": [a, b], "reemplazo": nuevo})
    aplicados.sort(key=lambda x: x["lineas"][0])
    for x, y in zip(aplicados, aplicados[1:]):
        if y["lineas"][0] <= x["lineas"][1]:
            raise ParcheRechazado(f"los cambios {x['cambio']} y {y['cambio']} se superponen")
    resultado = list(lineas)
    for x in reversed(aplicados):
        a, b = x["lineas"]
        resultado[a - 1:b] = x["reemplazo"]
    nuevo_codigo = "\n".join(resultado) + "\n"
    problemas = codeextract.validate_code(nuevo_codigo, spec)
    if problemas:
        raise ParcheRechazado("el código parcheado no es válido: " + "; ".join(problemas))
    return nuevo_codigo, [{"cambio": x["cambio"], "lineas": x["lineas"], "lineas_nuevas": len(x["reemplazo"])}
                          for x in aplicados]


# Pedido al programador ---------------------------------------------------------------------------------------------

SISTEMA_PARCHE = (
    "Sos el PROGRAMADOR del equipo. Corregí el defecto con un PARCHE MÍNIMO, no con el archivo completo.\n"
    "Solo podés cambiar líneas de las ZONAS AUTORIZADAS que muestra el coordinador. Cada cambio reemplaza un fragmento "
    "ORIGINAL, copiado EXACTAMENTE del código (líneas completas, con la misma sangría y sin los números de línea), "
    "por un REEMPLAZO escrito con la sangría correcta.\n"
    "Respondé SOLO con un objeto JSON con esta forma:\n"
    '{"cambios": [{"original": "<líneas exactas del código actual>", "reemplazo": "<líneas nuevas>"}], '
    '"justificacion": "<por qué el parche hace que el caso dé lo que pide el contrato>"}\n'
    f"Como máximo {MAX_CAMBIOS} cambios. Cada original tiene que aparecer una sola vez en el código. No escribas el "
    "programa completo.")
PEDIDO_DE_PARCHE = "Proponé el parche mínimo y respondé solo con el JSON pedido."


def mensajes_parche(descripcion: str, spec: dict, codigo: str, loc: dict, diagnostico: str, evidencia: str,
                    aviso: str, budget: int, historial: list[str] | None = None, base_n: int = 0) -> list[dict]:
    """El mismo material que el pedido de reparación del 7.1, con las zonas autorizadas en lugar del archivo."""
    origen = "la versión inicial" if not base_n else f"tu intento {base_n}"
    usuario = (f"Tarea:\n{prompts.clip(descripcion, budget // 4)}\n\n{prompts._sig(spec)}\n\n"
               f"Ejemplos:\n{prompts._examples(spec, budget // 6) or '(sin ejemplos visibles)'}\n\n"
               f"CÓDIGO ACTUAL de solucion.py ({origen}): solo ves lo necesario para el parche. Los números de línea "
               f"NO son parte del código.\n\n{prompts.clip(texto_zonas(codigo, spec['function'], loc), budget // 3)}\n\n"
               "Un revisor de código examinó la versión inicial y el coordinador comprobó su caso en un entorno "
               "aislado. No hay otros resultados de pruebas.\n\n"
               f"{diagnostico}\n\n{evidencia}\n\n"
               + ("TUS REPARACIONES ANTERIORES (todas en el caso del revisor; no repitas un cambio que ya probaste):\n"
                  + "\n".join(f"- {h}" for h in historial) + "\n\n" if historial else "")
               + (f"AVISO DEL COORDINADOR:\n{aviso}\n\n" if aviso else "")
               + PEDIDO_DE_PARCHE)
    return [{"role": "system", "content": SISTEMA_PARCHE}, {"role": "user", "content": usuario}]
