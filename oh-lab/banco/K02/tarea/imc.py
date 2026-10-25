"""Clasificación del índice de masa corporal.

bajo: menos de 18.5; normal: de 18.5 a menos de 25; sobrepeso: de 25 a menos de 30; obesidad: 30 o más.
Un valor menor o igual que cero produce ValueError.
"""


def clasificar(imc):
    if imc <= 0:
        raise ValueError("imc inválido")
    if imc < 18.5:
        return "bajo"
    if imc <= 25:
        return "normal"
    if imc < 30:
        return "sobrepeso"
    return "obesidad"
