"""Eliminar repetidos.

unicos([3, 1, 3, 2, 1]) == [3, 1, 2]

Conserva la primera aparición de cada elemento y el orden original. Los elementos son hashables.
"""


def unicos(items):
    vistos = set()
    resultado = []
    for x in items:
        if x in vistos:
            continue
        resultado.append(x)
        vistos.add(x)
    return resultado
