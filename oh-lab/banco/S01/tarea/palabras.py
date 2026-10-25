"""Frecuencia de palabras.

contar_palabras("Hola hola, mundo") == {"hola": 2, "mundo": 1}

Una palabra es una secuencia de letras, incluidas las acentuadas y la ñ. No se distinguen mayúsculas y
los números se ignoran.
"""
import re


def contar_palabras(texto):
    conteo = {}
    for palabra in re.findall(r"[^\W\d_]+", texto.lower()):
        conteo[palabra] = 1
    return conteo
