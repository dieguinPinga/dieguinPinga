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


def _caso(args, visible, kwargs=None, categoria=None):
    c = {"args": args, "expect": _resumen(*args, **(kwargs or {})), "visible": visible}
    if kwargs:
        c["kwargs"] = kwargs
    if categoria:
        c["categoria"] = categoria      # qué regla del contrato ejercita (sin valores); ver oficina/autocorreccion.py
    return c


def mision_resumen_ventas(max_intentos: int = 4) -> dict:
    casos = [_caso([CHICO], True),                                           # visibles: ejemplos chicos
             _caso([[]], True),
             _caso([REGISTROS], False, categoria="totales y agrupación por producto sin filtro de fechas"),
             _caso([REGISTROS], False, {"desde": "2026-03-01", "hasta": "2026-03-10"},
                   "filtro con «desde» y «hasta», ambos días incluidos"),
             _caso([REGISTROS], False, {"desde": "2026-03-20"}, "filtro solo con «desde», incluyendo ese día"),
             _caso([REGISTROS], False, {"desde": "2026-04-01"}, "filtro que no deja ninguna venta"),
             _caso([EMPATE], False, categoria="desempate de «mas_unidades» y «mas_importe»")]
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



# Segunda misión: conciliar_movimientos ---------------------------------------------------------------------------
# Los valores esperados están ESCRITOS A MANO (no los calcula la referencia): la comprobación previa ejecuta la
# solución de referencia contra ellos, así que dos fuentes independientes tienen que coincidir.

DESCRIPCION_CONCILIAR = (
    "Escribí la función conciliar_movimientos(movimientos, saldos_iniciales=None, conteo_fisico=None) para el "
    "inventario de un almacén.\n\n"
    "ENTRADA\n"
    "- movimientos: lista de diccionarios. Un ingreso o egreso es {'id': entero, 'fecha': 'AAAA-MM-DD', "
    "'producto': texto, 'tipo': 'ingreso' | 'egreso', 'cantidad': entero}. Una anulación es {'id': entero, "
    "'fecha': 'AAAA-MM-DD', 'producto': texto, 'tipo': 'anulacion', 'anula': id de otro movimiento} (su "
    "'producto' se ignora: vale el del movimiento anulado).\n"
    "- saldos_iniciales: {producto: entero} o None (= sin saldos iniciales).\n"
    "- conteo_fisico: {producto: entero} o None (= no hubo conteo).\n"
    "No modifiques los objetos recibidos.\n\n"
    "PROCESO\n"
    "Procesá los movimientos en orden de (fecha, id) ascendente, sin importar el orden de la lista. Un movimiento "
    "se APLICA o se IGNORA; cada uno ignorado genera una inconsistencia con el primer motivo que corresponda, "
    "revisando en este orden:\n"
    "1. 'id_duplicado': su id ya apareció en un movimiento anterior (aplicado o no);\n"
    "2. 'tipo_desconocido': el tipo no es ingreso, egreso ni anulacion;\n"
    "3. para ingreso y egreso: 'cantidad_invalida' si la cantidad no es un entero mayor que 0;\n"
    "   para egreso: 'saldo_insuficiente' si el saldo del producto quedaría negativo;\n"
    "4. para anulacion: 'anulacion_inexistente' si no hay un movimiento APLICADO anterior con ese id; "
    "'anulacion_de_anulacion' si el anulado es una anulación; 'anulacion_repetida' si ya fue anulado; "
    "'saldo_insuficiente' si revertirlo dejaría el saldo negativo.\n"
    "Aplicar un ingreso suma su cantidad; un egreso la resta; una anulación revierte por completo el efecto del "
    "movimiento anulado (y lo marca como anulado). Un saldo puede quedar exactamente en 0.\n\n"
    "SALIDA: un diccionario con exactamente estas claves:\n"
    "- 'saldos': {producto: saldo final} para cada producto de saldos_iniciales o de algún movimiento aplicado;\n"
    "- 'movimientos_aplicados': cantidad de movimientos aplicados (las anulaciones aplicadas también cuentan);\n"
    "- 'inconsistencias': lista de {'id': id, 'motivo': motivo}, en el orden de proceso;\n"
    "- 'conciliacion': None si conteo_fisico es None; si no, {producto: conteo - saldo} para cada producto de "
    "'saldos' o de conteo_fisico (el que falta en uno de los dos vale 0), incluyendo solo las diferencias "
    "distintas de 0.")


def _mov(i, fecha, producto, tipo, cantidad=None, anula=None):
    m = {"id": i, "fecha": fecha, "producto": producto, "tipo": tipo}
    if anula is not None:
        m["anula"] = anula
    else:
        m["cantidad"] = cantidad
    return m


def _salida(saldos, aplicados, inconsistencias=(), conciliacion=None):
    return {"saldos": saldos, "movimientos_aplicados": aplicados,
            "inconsistencias": [{"id": i, "motivo": m} for i, m in inconsistencias], "conciliacion": conciliacion}


def casos_conciliar() -> list[dict]:
    """(args, kwargs, esperado, visible), con el esperado calculado a mano."""
    c = []
    # V1 · visible: ingreso, egreso, anulación del egreso, saldo inicial y conteo con una diferencia.
    c.append(([[_mov(1, "2026-05-01", "Arroz", "ingreso", 10), _mov(2, "2026-05-02", "Arroz", "egreso", 3),
                _mov(3, "2026-05-02", "Yerba", "ingreso", 5), _mov(4, "2026-05-03", "Arroz", "anulacion", anula=2)]],
              {"saldos_iniciales": {"Arroz": 2}, "conteo_fisico": {"Arroz": 12, "Yerba": 4}},
              _salida({"Arroz": 12, "Yerba": 5}, 4, (), {"Yerba": -1}), True))
    # V2 · visible: un egreso sin saldo suficiente se ignora.
    c.append(([[_mov(1, "2026-05-01", "Aceite", "ingreso", 2), _mov(2, "2026-05-02", "Aceite", "egreso", 5)]], {},
              _salida({"Aceite": 2}, 1, [(2, "saldo_insuficiente")]), True))
    # H1 · vacío.
    c.append(([[]], {}, _salida({}, 0), False))
    # H2 · solo saldos y conteo: un producto en 0 y otro solo en el conteo.
    c.append(([[]], {"saldos_iniciales": {"Arroz": 3, "Fideos": 0}, "conteo_fisico": {"Arroz": 3, "Sal": 2}},
              _salida({"Arroz": 3, "Fideos": 0}, 0, (), {"Sal": 2}), False))
    # H3 · lista desordenada: la anulación es anterior (por fecha) al egreso que anula.
    c.append(([[_mov(5, "2026-05-03", "Arroz", "egreso", 4), _mov(2, "2026-05-01", "Arroz", "ingreso", 6),
                _mov(7, "2026-05-02", "Arroz", "anulacion", anula=5), _mov(3, "2026-05-01", "Arroz", "ingreso", 1)]],
              {}, _salida({"Arroz": 3}, 3, [(7, "anulacion_inexistente")]), False))
    # H4 · anulaciones: revertir un ingreso ya vendido, repetida, de una anulación y una posterior válida.
    c.append(([[_mov(1, "2026-05-01", "Yerba", "ingreso", 10), _mov(2, "2026-05-02", "Yerba", "egreso", 7),
                _mov(3, "2026-05-03", "Yerba", "anulacion", anula=1), _mov(4, "2026-05-04", "Yerba", "anulacion", anula=2),
                _mov(5, "2026-05-05", "Yerba", "anulacion", anula=2), _mov(6, "2026-05-06", "Yerba", "anulacion", anula=4),
                _mov(7, "2026-05-07", "Yerba", "anulacion", anula=1)]], {"conteo_fisico": {"Yerba": 0}},
              _salida({"Yerba": 0}, 4, [(3, "saldo_insuficiente"), (5, "anulacion_repetida"),
                                        (6, "anulacion_de_anulacion")], {}), False))
    # H5 · validación: id duplicado, cantidades inválidas, tipo desconocido, anulación de un movimiento ignorado.
    c.append(([[_mov(1, "2026-05-01", "Sal", "ingreso", 5), _mov(1, "2026-05-02", "Sal", "ingreso", 3),
                _mov(2, "2026-05-02", "Sal", "egreso", 0), _mov(3, "2026-05-02", "Sal", "egreso", -2),
                _mov(4, "2026-05-03", "Sal", "ingreso", "4"), _mov(5, "2026-05-03", "Sal", "devolucion", 1),
                _mov(6, "2026-05-04", "Sal", "anulacion", anula=2)]], {},
              _salida({"Sal": 5}, 1, [(1, "id_duplicado"), (2, "cantidad_invalida"), (3, "cantidad_invalida"),
                                      (4, "cantidad_invalida"), (5, "tipo_desconocido"), (6, "anulacion_inexistente")]),
              False))
    # H6 · varios productos, lista desordenada, egreso hasta 0 exacto, conteo con un producto que no tiene saldo.
    h6 = [_mov(1, "2026-06-01", "Arroz", "egreso", 5), _mov(2, "2026-06-01", "Fideos", "ingreso", 8),
          _mov(3, "2026-06-02", "Aceite", "egreso", 2), _mov(4, "2026-06-02", "Fideos", "egreso", 3),
          _mov(5, "2026-06-03", "Arroz", "ingreso", 4), _mov(6, "2026-06-03", "Fideos", "anulacion", anula=4),
          _mov(7, "2026-06-04", "Aceite", "ingreso", 3), _mov(8, "2026-06-04", "Aceite", "egreso", 2),
          _mov(9, "2026-06-05", "Arroz", "anulacion", anula=1), _mov(10, "2026-06-05", "Fideos", "egreso", 8)]
    c.append(([[h6[i] for i in (9, 2, 5, 0, 7, 3, 8, 1, 6, 4)]],
              {"saldos_iniciales": {"Arroz": 5, "Aceite": 1}, "conteo_fisico": {"Arroz": 9, "Aceite": 1, "Yerba": 2}},
              _salida({"Arroz": 9, "Aceite": 2, "Fideos": 0}, 9, [(3, "saldo_insuficiente")], {"Aceite": -1, "Yerba": 2}),
              False))
    # H7 · el mismo día manda el id, no el tipo: el egreso (id 1) va antes que el ingreso (id 2).
    c.append(([[_mov(2, "2026-07-01", "Sal", "ingreso", 5), _mov(1, "2026-07-01", "Sal", "egreso", 2)]], {},
              _salida({"Sal": 5}, 1, [(1, "saldo_insuficiente")]), False))
    return c


# Qué regla del contrato ejercita cada caso oculto, en el orden de casos_conciliar() (sin valores ni datos). La
# autocorrección (oficina/autocorreccion.py) se lo muestra al modelo solo cuando ese caso falla.
CATEGORIAS_CONCILIAR = {
    2: "entrada vacía",
    3: "saldos iniciales y conteo sin movimientos",
    4: "orden de proceso por (fecha, id) y anulaciones que apuntan a un movimiento todavía no procesado",
    5: "anulaciones: revertir dejando saldo negativo, anular dos veces y anular una anulación; precedencia de motivos",
    6: "validación: id repetido, cantidades inválidas, tipo desconocido y anulación de un movimiento ignorado",
    7: "varios productos, saldo exacto en 0 y conciliación con productos que faltan de un lado",
    8: "orden de proceso dentro del mismo día",
}


def mision_conciliar_movimientos(max_intentos: int = 4) -> dict:
    casos = []
    for i, (args, kwargs, esperado, visible) in enumerate(casos_conciliar()):
        caso = {"args": args, "expect": esperado, "visible": visible}
        if not visible and i in CATEGORIAS_CONCILIAR:
            caso["categoria"] = CATEGORIAS_CONCILIAR[i]
        if kwargs:
            caso["kwargs"] = kwargs
        casos.append(caso)
    return {"title": "Almacén: conciliar_movimientos", "description": DESCRIPCION_CONCILIAR,
            "spec": {"mode": "funcion", "function": "conciliar_movimientos", "cases": casos},
            "tests_py": "", "max_attempts": max_intentos}


REFERENCIA_CONCILIAR = '''def conciliar_movimientos(movimientos, saldos_iniciales=None, conteo_fisico=None):
    saldos = dict(saldos_iniciales or {})
    vistos, aplicados, anulados = set(), {}, set()
    inconsistencias = []
    n = 0

    def ignorar(m, motivo):
        inconsistencias.append({"id": m["id"], "motivo": motivo})

    for m in sorted(movimientos, key=lambda x: (x["fecha"], x["id"])):
        mid, tipo = m["id"], m["tipo"]
        if mid in vistos:
            ignorar(m, "id_duplicado")
            continue
        vistos.add(mid)
        if tipo not in ("ingreso", "egreso", "anulacion"):
            ignorar(m, "tipo_desconocido")
            continue
        if tipo in ("ingreso", "egreso"):
            cant = m.get("cantidad")
            if not isinstance(cant, int) or isinstance(cant, bool) or cant <= 0:
                ignorar(m, "cantidad_invalida")
                continue
            efecto = cant if tipo == "ingreso" else -cant
            prod = m["producto"]
        else:
            ref = aplicados.get(m.get("anula"))
            if ref is None:
                ignorar(m, "anulacion_inexistente")
                continue
            if ref["tipo"] == "anulacion":
                ignorar(m, "anulacion_de_anulacion")
                continue
            if ref["id"] in anulados:
                ignorar(m, "anulacion_repetida")
                continue
            prod, efecto = ref["producto"], -ref["efecto"]
        if saldos.get(prod, 0) + efecto < 0:
            ignorar(m, "saldo_insuficiente")
            continue
        saldos[prod] = saldos.get(prod, 0) + efecto
        if tipo == "anulacion":
            anulados.add(ref["id"])
        aplicados[mid] = {"id": mid, "tipo": tipo, "producto": prod, "efecto": efecto}
        n += 1
    conciliacion = None
    if conteo_fisico is not None:
        conciliacion = {}
        for prod in set(saldos) | set(conteo_fisico):
            d = conteo_fisico.get(prod, 0) - saldos.get(prod, 0)
            if d != 0:
                conciliacion[prod] = d
    return {"saldos": saldos, "movimientos_aplicados": n, "inconsistencias": inconsistencias,
            "conciliacion": conciliacion}
'''

MISIONES = {"resumen_ventas": (mision_resumen_ventas, REFERENCIA),
            "conciliar_movimientos": (mision_conciliar_movimientos, REFERENCIA_CONCILIAR)}
