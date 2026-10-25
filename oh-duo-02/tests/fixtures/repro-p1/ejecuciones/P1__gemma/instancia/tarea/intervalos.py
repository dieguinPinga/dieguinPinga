"""Unión de intervalos cerrados.

unir_intervalos([(1, 3), (2, 6), (8, 10)]) == [(1, 6), (8, 10)]

Cada intervalo es una tupla (inicio, fin) con inicio <= fin; si no, ValueError. El resultado está ordenado
por inicio y no tiene intervalos que se superpongan ni que se toquen: (1, 2) y (2, 5) se unen en (1, 5).
La entrada no se modifica.
"""


def unir_intervalos(intervalos):
    for inicio, fin in intervalos:
        if inicio > fin:
            raise ValueError(f"intervalo inválido: {(inicio, fin)}")
    resultado = []
    for inicio, fin in sorted(intervalos):
        if resultado and inicio <= resultado[-1][1]:
            resultado[-1] = (resultado[-1][0], fin)
        else:
            resultado.append((inicio, fin))
    return resultado
