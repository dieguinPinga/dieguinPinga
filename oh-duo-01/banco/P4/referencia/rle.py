"""Codificación por longitud de corridas.

codificar("aaabcc") == "3a1b2c"
decodificar("3a1b2c") == "aaabcc"

El texto a codificar no puede tener dígitos (ValueError). Un código inválido (un carácter sin cantidad o
una cantidad sin carácter) produce ValueError. Para cualquier texto válido,
decodificar(codificar(texto)) == texto.
"""


def codificar(texto):
    if any(c.isdigit() for c in texto):
        raise ValueError("el texto no puede tener dígitos")
    partes = []
    i = 0
    while i < len(texto):
        j = i
        while j < len(texto) and texto[j] == texto[i]:
            j += 1
        partes.append(f"{j - i}{texto[i]}")
        i = j
    return "".join(partes)


def decodificar(codigo):
    salida, numero = [], ""
    for caracter in codigo:
        if caracter.isdigit():
            numero += caracter
        else:
            if not numero:
                raise ValueError("falta la cantidad")
            salida.append(caracter * int(numero))
            numero = ""
    if numero:
        raise ValueError("cantidad sin carácter")
    return "".join(salida)
