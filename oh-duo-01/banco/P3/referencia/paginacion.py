"""Paginación.

paginar(list(range(10)), tamano=4, pagina=3) == {"items": [8, 9], "pagina": 3, "paginas": 3, "total": 10}

Las páginas se numeran desde 1 y la última puede estar incompleta. Una lista vacía tiene 1 página, vacía.
Un tamaño menor que 1 o una página fuera de 1..paginas produce ValueError.
"""


def paginar(items, tamano, pagina):
    if tamano < 1:
        raise ValueError("tamaño inválido")
    total = len(items)
    paginas = max(1, -(-total // tamano))
    if not 1 <= pagina <= paginas:
        raise ValueError("página fuera de rango")
    inicio = (pagina - 1) * tamano
    return {"items": items[inicio:inicio + tamano], "pagina": pagina, "paginas": paginas, "total": total}
