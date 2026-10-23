"""Misiones de programación de la oficina (etapa 3). La primera usa el MISMO contexto del almacén.

Formato: el de las misiones del motor original (engine.py), modo «funcion»: casos visibles (ejemplos que ve el
modelo) y casos OCULTOS («visible»: false) que solo ve el sandbox. El informe de fallos que recibe el modelo
(prompts.failure_report) nunca incluye el valor esperado de un caso oculto.

Cada misión trae una SOLUCIÓN DE REFERENCIA escrita por nosotros: la comprobación previa la ejecuta en el sandbox
contra todos los casos para demostrar que las pruebas son correctas ANTES de gastar una inferencia. No es código
generado por la IA.
"""
from __future__ import annotations

from . import almacen as AL

REGISTROS = [list(r) for r in AL.REGISTROS]


def _resumen(regs, desde=None, hasta=None):
    regs = [r for r in regs if (desde is None or r[1] >= desde) and (hasta is None or r[1] <= hasta)]
    por = {}
    for _, _, prod, cant, precio in regs:
        p = por.setdefault(prod, {"unidades": 0, "importe": 0})
        p["unidades"] += cant
        p["importe"] += cant * precio

    def mejor(k):
        return min(por, key=lambda x: (-por[x][k], x)) if por else None
    return {"unidades": sum(v["unidades"] for v in por.values()), "importe": sum(v["importe"] for v in por.values()),
            "por_producto": por, "mas_unidades": mejor("unidades"), "mas_importe": mejor("importe")}


DESCRIPCION = (
    "Escribí la función resumen_ventas(ventas, desde=None, hasta=None) para un almacén.\n"
    "Cada venta es una lista [id, fecha, producto, cantidad, precio_unitario]; la fecha es un texto 'AAAA-MM-DD'; "
    "cantidad y precio_unitario son enteros. El importe de una venta es cantidad * precio_unitario.\n"
    "desde y hasta (textos 'AAAA-MM-DD' u omitidos) filtran por fecha, AMBOS DÍAS INCLUIDOS.\n"
    "Devolvé un diccionario con exactamente estas claves:\n"
    "- 'unidades': total de unidades vendidas (entero);\n"
    "- 'importe': total de dinero (entero);\n"
    "- 'por_producto': {producto: {'unidades': entero, 'importe': entero}}, solo con productos que tengan ventas;\n"
    "- 'mas_unidades': el producto con más unidades; 'mas_importe': el producto con más importe. Si hay empate, el "
    "primero en orden alfabético. Si no hay ventas, ambos son None (y los totales, 0).\n"
    "No modifiques la lista recibida.")

CHICO = [[1, "2026-03-01", "Arroz", 2, 2], [2, "2026-03-02", "Aceite", 1, 10], [3, "2026-03-03", "Arroz", 1, 2]]
EMPATE = [[1, "2026-04-01", "Yerba", 2, 5], [2, "2026-04-02", "Fideos", 10, 1], [3, "2026-04-03", "Arroz", 5, 2]]


def _caso(args, visible, kwargs=None):
    c = {"args": args, "expect": _resumen(*args, **(kwargs or {})), "visible": visible}
    if kwargs:
        c["kwargs"] = kwargs
    return c


def mision_resumen_ventas(max_intentos: int = 4) -> dict:
    casos = [_caso([CHICO], True),                                           # visibles: ejemplos chicos
             _caso([[]], True),
             _caso([REGISTROS], False),                                       # ocultos: los 20 registros
             _caso([REGISTROS], False, {"desde": "2026-03-01", "hasta": "2026-03-10"}),
             _caso([REGISTROS], False, {"desde": "2026-03-20"}),
             _caso([REGISTROS], False, {"desde": "2026-04-01"}),
             _caso([EMPATE], False)]                                          # empate de importe: Arroz y Yerba 10
    return {"title": "Almacén: resumen_ventas", "description": DESCRIPCION,
            "spec": {"mode": "funcion", "function": "resumen_ventas", "cases": casos},
            "tests_py": "", "max_attempts": max_intentos}


REFERENCIA = '''def resumen_ventas(ventas, desde=None, hasta=None):
    por = {}
    for _, fecha, prod, cant, precio in ventas:
        if (desde is not None and fecha < desde) or (hasta is not None and fecha > hasta):
            continue
        p = por.setdefault(prod, {"unidades": 0, "importe": 0})
        p["unidades"] += cant
        p["importe"] += cant * precio

    def mejor(k):
        return min(por, key=lambda x: (-por[x][k], x)) if por else None
    return {"unidades": sum(v["unidades"] for v in por.values()),
            "importe": sum(v["importe"] for v in por.values()),
            "por_producto": por, "mas_unidades": mejor("unidades"), "mas_importe": mejor("importe")}
'''

MISIONES = {"resumen_ventas": (mision_resumen_ventas, REFERENCIA)}
