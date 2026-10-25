"""Solución de referencia. Solo la usa el control previo de correr.sh para comprobar que la evaluación
puede aprobar una solución correcta (y no da falsos negativos). Nunca se monta en el sandbox del agente."""
import re

_PARTE = re.compile(r"(\d+)([hms])")
_FACTOR = {"h": 3600, "m": 60, "s": 1}


def a_segundos(texto: str) -> int:
    texto = texto.strip()
    if not re.fullmatch(r"(\d+[hms])+", texto):
        raise ValueError(f"duración inválida: {texto!r}")
    return sum(int(n) * _FACTOR[u] for n, u in _PARTE.findall(texto))
