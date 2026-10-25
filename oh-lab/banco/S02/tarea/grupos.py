"""Agrupar registros por una clave.

agrupar([{"t": "a", "v": 1}, {"t": "b", "v": 2}, {"t": "a", "v": 3}], "t")
    == {"a": [{"t": "a", "v": 1}, {"t": "a", "v": 3}], "b": [{"t": "b", "v": 2}]}

Dentro de cada grupo se conserva el orden de aparición. Los registros que no tienen la clave se ignoran.
"""


def agrupar(registros, clave):
    grupos = {}
    for registro in registros:
        if clave not in registro:
            continue
        grupos[registro[clave]] = [registro]
    return grupos
