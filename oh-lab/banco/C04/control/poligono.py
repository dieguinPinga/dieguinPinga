"""Área de un polígono simple.

area([(0, 0), (4, 0), (4, 3)]) == 6.0

Los vértices vienen en orden, horario o antihorario. Con menos de tres vértices: ValueError.
"""


def area(puntos):
    if len(puntos) < 3:
        raise ValueError("se necesitan al menos tres vértices")
    suma = 0
    for (x1, y1), (x2, y2) in zip(puntos, puntos[1:] + puntos[:1]):
        suma += x1 * y2 - x2 * y1
    return abs(suma) / 2
