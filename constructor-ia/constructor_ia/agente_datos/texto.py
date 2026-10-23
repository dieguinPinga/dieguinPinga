"""Utilidades de lenguaje GENÉRICAS para leer una pregunta en español: sin nombres de tablas ni de dominios.

Solo reconocen palabras funcionales del idioma (artículos, preposiciones, interrogativos), meses, años y palabras de
intención («más», «menos», «cuántos», «promedio»). Lo demás son términos de contenido, que se comparan por raíz.
La raíz es un prefijo de 4 letras sin acentos: tosca a propósito (se documenta su límite: puede confundir palabras
que empiezan igual y no reconoce sinónimos).
"""
from __future__ import annotations

import datetime as dt
import re
import unicodedata

PALABRAS_FUNCIONALES = {
    "el", "la", "los", "las", "un", "una", "unos", "unas", "lo", "al", "del", "de", "en", "por", "para", "con", "sin",
    "y", "o", "e", "u", "a", "que", "qué", "cual", "cuales", "quien", "quienes", "como", "donde", "cuando", "fue",
    "fueron", "es", "son", "era", "eran", "ha", "han", "hay", "se", "su", "sus", "mi", "tu", "durante", "entre",
    "desde", "hasta", "este", "esta", "ese", "esa", "año", "ano", "mes", "dia", "total", "cada", "todo", "toda",
    "todos", "todas", "registrado", "registrada", "registrados", "registradas",
}
MESES = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
         "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12}
MAYOR = {"mas", "mayor", "maximo", "maxima", "maximos", "mejor", "top"}
MENOR = {"menos", "menor", "minimo", "minima", "peor"}
CONTEO = {"cuantos", "cuantas"}
PROMEDIO = {"promedio", "media"}
INTERROGATIVOS = {"que", "cual", "cuales", "quien", "quienes"}


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def palabras(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", fold(s))


def raiz(palabra: str) -> str:
    p = fold(palabra)
    return p[:4] if len(p) > 4 else p


def raices(texto: str) -> set[str]:
    """Raíces de las palabras de contenido de un texto (nombres con guion bajo incluidos)."""
    return {raiz(w) for w in re.split(r"[^a-z0-9]+", fold(texto)) if len(w) >= 3 and w not in _FUNC}


_FUNC = {fold(w) for w in PALABRAS_FUNCIONALES}


def periodo(pregunta: str) -> dict | None:
    """«marzo de 2026», «marzo 2026» → mes completo; «en 2026» → año completo. Intervalo semiabierto [desde, hasta).
    Devuelve {"varios": True} si hay más de un período (no se interpreta)."""
    t = fold(pregunta)
    meses = [(MESES[m.group(1)], int(m.group(2))) for m in
             re.finditer(r"\b(" + "|".join(MESES) + r")\s+(?:de(?:l)?\s+)?(\d{4})\b", t)]
    if len(meses) > 1:
        return {"varios": True}
    if meses:
        mes, anio = meses[0]
        desde = dt.datetime(anio, mes, 1)
        hasta = dt.datetime(anio + (mes == 12), mes % 12 + 1, 1)
        return {"desde": desde, "hasta": hasta, "texto": f"{mes:02d}/{anio}"}
    anios = re.findall(r"\b(?:en|del|durante)\s+(?:el\s+)?(?:ano\s+)?(\d{4})\b", t)
    if len(anios) > 1:
        return {"varios": True}
    if anios:
        a = int(anios[0])
        return {"desde": dt.datetime(a, 1, 1), "hasta": dt.datetime(a + 1, 1, 1), "texto": str(a)}
    return None


def intencion(pregunta: str) -> dict:
    ws = set(palabras(pregunta))
    orden = "mayor" if ws & MAYOR else ("menor" if ws & MENOR else None)
    agregado = "conteo" if ws & CONTEO else ("promedio" if ws & PROMEDIO else None)
    return {"orden": orden, "agregado": agregado}


def entidad(pregunta: str) -> str | None:
    """El sustantivo preguntado: la primera palabra de contenido después de «qué», «cuál», «quién»."""
    ws = palabras(pregunta)
    for i, w in enumerate(ws):
        if w in INTERROGATIVOS:
            for x in ws[i + 1:]:
                if x not in _FUNC and x not in MAYOR | MENOR and not x.isdigit():
                    return x
            return None
    return None


def terminos(pregunta: str) -> list[str]:
    """Raíces de contenido de la pregunta, sin funcionales, período, intención ni la entidad preguntada."""
    ent = entidad(pregunta)
    out = []
    for w in palabras(pregunta):
        if (w in _FUNC or w in MESES or w.isdigit() or w in MAYOR | MENOR | CONTEO | PROMEDIO | INTERROGATIVOS
                or w == ent or len(w) < 3):
            continue
        r = raiz(w)
        if r not in out:
            out.append(r)
    return out


def normalizar_pregunta(pregunta: str) -> str:
    return " ".join(palabras(pregunta))
