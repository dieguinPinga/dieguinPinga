"""Combinar configuraciones.

fusionar({"db": {"host": "a", "port": 1}}, {"db": {"port": 2}}) == {"db": {"host": "a", "port": 2}}

Devuelve un dict nuevo, sin modificar los originales. Ante claves repetidas gana `extra`, salvo que el
valor sea un dict en las dos configuraciones: en ese caso se combinan recursivamente.
"""


def fusionar(base, extra):
    resultado = dict(base)
    for clave, valor in extra.items():
        if isinstance(valor, dict) and isinstance(resultado.get(clave), dict):
            resultado[clave] = fusionar(resultado[clave], valor)
        else:
            resultado[clave] = valor
    return resultado
