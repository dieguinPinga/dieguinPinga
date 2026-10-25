"""Leer números de un texto.

leer_numeros(["1", "# total", "", " 2.5 "]) == [1.0, 2.5]

Convierte cada línea en float. Se ignoran las líneas vacías o con solo espacios, y los comentarios: las
líneas que, después de quitar los espacios, empiezan con '#'. Cualquier otra línea no numérica produce
ValueError.
"""


def leer_numeros(lineas):
    numeros = []
    for linea in lineas:
        texto = linea.strip()
        if not texto:
            continue
        numeros.append(float(texto))
    return numeros
