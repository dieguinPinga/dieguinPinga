"""Años bisiestos del calendario gregoriano.

es_bisiesto(2024) is True
es_bisiesto(1900) is False
es_bisiesto(2000) is True

Es bisiesto si es divisible por 4, salvo que sea divisible por 100 y no por 400.
"""


def es_bisiesto(anio):
    return anio % 4 == 0 and (anio % 100 != 0 and anio % 400 == 0)
