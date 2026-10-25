"""Percentil por rango más cercano.

percentil([15, 20, 35, 40, 50], 40) == 20

Se ordenan los valores; el rango es ceil(p / 100 * n), como mínimo 1, y se devuelve el valor que ocupa esa
posición contando desde 1. p va de 0 a 100. Lista vacía o p fuera de rango: ValueError.
"""
import math


def percentil(valores, p):
    if not valores or not 0 <= p <= 100:
        raise ValueError("lista vacía o percentil fuera de rango")
    orden = sorted(valores)
    rango = max(1, math.ceil(p / 100 * len(orden)))
    return orden[min(rango, len(orden) - 1)]
