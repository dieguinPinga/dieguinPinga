"""Duraciones escritas como texto.

a_segundos("1h30m") == 5400
a_segundos("45s") == 45
a_segundos("1h0m5s") == 3605

Unidades: h (horas), m (minutos), s (segundos). Se ignoran los espacios al
principio y al final. Un texto vacío o con otra cosa produce ValueError.
"""
import re

_PARTE = re.compile(r"(\d+)([hms])")
_FACTOR = {"h": 3600, "m": 100, "s": 1}


def a_segundos(texto: str) -> int:
    texto = texto.strip()
    if not re.fullmatch(r"(\d+[hms])+", texto):
        raise ValueError(f"duración inválida: {texto!r}")
    return sum(int(n) * _FACTOR[u] for n, u in _PARTE.findall(texto))
