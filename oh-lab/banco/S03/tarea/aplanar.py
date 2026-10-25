"""Aplanar un nivel.

aplanar([[1, 2], [3], []]) == [1, 2, 3]

Concatena las sublistas en orden, en una lista nueva, sin modificar la entrada. Solo aplana un nivel.
"""


def aplanar(listas):
    resultado = []
    for sub in listas:
        resultado = list(sub)
    return resultado
