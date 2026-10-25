"""Promedio ponderado.

promedio_ponderado([10, 20], [1, 3]) == 17.5

Las dos listas tienen el mismo largo y no están vacías. Los pesos no pueden ser negativos ni sumar cero.
Si no se cumple, ValueError.
"""


def promedio_ponderado(valores, pesos):
    if not valores or len(valores) != len(pesos):
        raise ValueError("listas vacías o de distinto largo")
    if any(p < 0 for p in pesos) or sum(pesos) == 0:
        raise ValueError("pesos inválidos")
    total = sum(v * p for v, p in zip(valores, pesos))
    return total / len(pesos)
