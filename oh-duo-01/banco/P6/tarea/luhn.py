"""Validación con el algoritmo de Luhn.

luhn_valido("4539 3195 0343 6467") is True

Se ignoran los espacios. Recorriendo desde el último dígito hacia la izquierda, se duplica uno de cada dos,
empezando por el segundo desde la derecha; si el doble pasa de 9 se le resta 9. El número es válido si la
suma de todos los dígitos así obtenidos es múltiplo de 10. Un texto con menos de dos dígitos o con
caracteres que no sean dígitos ni espacios produce ValueError.
"""


def luhn_valido(numero):
    digitos = numero.replace(" ", "")
    if len(digitos) < 2 or not digitos.isdecimal() or not digitos.isascii():
        raise ValueError("número inválido")
    suma = 0
    for posicion, caracter in enumerate(digitos):
        valor = int(caracter)
        if posicion % 2 == 1:
            valor *= 2
            if valor > 9:
                valor -= 9
        suma += valor
    return suma % 10 == 0
