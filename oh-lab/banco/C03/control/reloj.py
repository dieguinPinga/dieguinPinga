"""Segundos a texto HH:MM:SS.

a_hms(3725) == "01:02:05"

total es un entero mayor o igual que cero (negativo: ValueError). Las horas pueden pasar de 99.
"""


def a_hms(total):
    if total < 0:
        raise ValueError("total negativo")
    horas = total // 3600
    minutos = (total % 3600) // 60
    segundos = total % 60
    return f"{horas:02d}:{minutos:02d}:{segundos:02d}"
