"""Escenario estándar permanente de la oficina: un almacén ficticio (OFICINA.md, «Escenario almacén»).

Un único contexto con 20 ventas de marzo de 2026, números enteros fáciles de verificar a mano:
    ventas(id, fecha, producto, cantidad, precio_unitario)
Cuatro productos a precio fijo: Arroz $2, Fideos $3, Yerba $5, Aceite $10. Total: 56 unidades y $235.
El ranking por unidades (Arroz 20 > Fideos 15 > Yerba 12 > Aceite 9) es el INVERSO del ranking por importe
(Aceite 90 > Yerba 60 > Fideos 45 > Arroz 40): «más unidades» y «más dinero» son preguntas distintas.

Misiones (todas sobre los mismos registros):
    A importe total · B unidades totales · C por producto, y D (los dos rankings, que dependen de C verificada)
    E período del 1 al 10 de marzo inclusive · F auditoría con casos sintéticos propios ($220 incorrecto, $235
    correcto): el auditor recibe los registros y la propuesta a auditar, nunca la respuesta del coordinador.

La VERDAD la calcula el coordinador en Python (verdad()) y nunca entra en la carga de una tarea; las pruebas la
comparan con los valores escritos a mano (ESPERADO). Lleva un canario para auditar fugas. Un rechazo explica qué
falló sin revelar el valor correcto.
"""
from __future__ import annotations

import secrets
from collections import defaultdict

COLUMNAS = ("id", "fecha", "producto", "cantidad", "precio_unitario")
REGISTROS = (
    (1, "2026-03-01", "Arroz", 2, 2), (2, "2026-03-02", "Fideos", 3, 3), (3, "2026-03-03", "Aceite", 1, 10),
    (4, "2026-03-04", "Yerba", 2, 5), (5, "2026-03-05", "Arroz", 5, 2), (6, "2026-03-06", "Fideos", 2, 3),
    (7, "2026-03-07", "Aceite", 2, 10), (8, "2026-03-08", "Yerba", 1, 5), (9, "2026-03-09", "Arroz", 3, 2),
    (10, "2026-03-10", "Fideos", 4, 3), (11, "2026-03-11", "Aceite", 1, 10), (12, "2026-03-12", "Yerba", 4, 5),
    (13, "2026-03-13", "Arroz", 4, 2), (14, "2026-03-14", "Fideos", 1, 3), (15, "2026-03-15", "Aceite", 3, 10),
    (16, "2026-03-16", "Yerba", 2, 5), (17, "2026-03-17", "Arroz", 6, 2), (18, "2026-03-18", "Fideos", 5, 3),
    (19, "2026-03-19", "Aceite", 2, 10), (20, "2026-03-20", "Yerba", 3, 5))
PERIODO = ("2026-03-01", "2026-03-10")          # E: ambos días incluidos
AUDITORIAS = {"auditar_220": 220, "auditar_235": 235}

# Escrito a mano (la tabla de OFICINA.md); verdad() lo recalcula aparte y las pruebas exigen que coincidan.
ESPERADO = {
    "importe_total": {"importe": 235},
    "unidades_total": {"unidades": 56},
    "por_producto": {"por_producto": {"Arroz": {"unidades": 20, "importe": 40}, "Fideos": {"unidades": 15, "importe": 45},
                                      "Yerba": {"unidades": 12, "importe": 60}, "Aceite": {"unidades": 9, "importe": 90}}},
    "mas_unidades": {"producto": "Arroz", "valor": 20},
    "mas_importe": {"producto": "Aceite", "valor": 90},
    "periodo": {"importe": 92, "unidades": 25},
    "auditar_220": {"dictamen": "incorrecto", "valor": 235},
    "auditar_235": {"dictamen": "correcto", "valor": 235},
}

CONSIGNAS = {
    "importe_total": ("¿Cuánto dinero se vendió en total (cantidad × precio_unitario, todas las ventas)?",
                      {"importe": "entero"}),
    "unidades_total": ("¿Cuántas unidades se vendieron en total?", {"unidades": "entero"}),
    "por_producto": ("Unidades e importe de cada producto.",
                     {"por_producto": {"<producto>": {"unidades": "entero", "importe": "entero"}}}),
    "mas_unidades": ("¿Qué producto vendió más UNIDADES y cuántas?", {"producto": "texto", "valor": "entero"}),
    "mas_importe": ("¿Qué producto vendió más DINERO y cuánto?", {"producto": "texto", "valor": "entero"}),
    "periodo": (f"Importe y unidades de las ventas del {PERIODO[0]} al {PERIODO[1]}, ambos días incluidos.",
                {"importe": "entero", "unidades": "entero"}),
    "auditar_220": ("Otro trabajador respondió el importe total de todas las ventas. ¿Es correcto? Si no, ¿cuál es?",
                    {"dictamen": "correcto | incorrecto", "valor": "entero (el importe total recalculado)"}),
}
CONSIGNAS["auditar_235"] = CONSIGNAS["auditar_220"]


def _calcular(registros) -> dict:
    por = defaultdict(lambda: {"unidades": 0, "importe": 0})
    for _, _, prod, cant, precio in registros:
        por[prod]["unidades"] += cant
        por[prod]["importe"] += cant * precio
    return dict(por)


def verdad() -> dict:
    """La verdad del coordinador, calculada en Python desde los registros (independiente de ESPERADO)."""
    por = _calcular(REGISTROS)
    per = _calcular([r for r in REGISTROS if PERIODO[0] <= r[1] <= PERIODO[1]])
    total = sum(v["importe"] for v in por.values())
    mu = max(por.items(), key=lambda kv: kv[1]["unidades"])
    mi = max(por.items(), key=lambda kv: kv[1]["importe"])
    out = {"importe_total": {"importe": total},
           "unidades_total": {"unidades": sum(v["unidades"] for v in por.values())},
           "por_producto": {"por_producto": por},
           "mas_unidades": {"producto": mu[0], "valor": mu[1]["unidades"]},
           "mas_importe": {"producto": mi[0], "valor": mi[1]["importe"]},
           "periodo": {"importe": sum(v["importe"] for v in per.values()),
                       "unidades": sum(v["unidades"] for v in per.values())}}
    for nombre, auditado in AUDITORIAS.items():
        out[nombre] = {"dictamen": "correcto" if auditado == total else "incorrecto", "valor": total}
    out["_canario"] = "VERDAD-ALMACEN-" + secrets.token_hex(8)
    return out


def _tarea(tipo: str, modelo: str, max_intentos: int, tiempo_max_s: int, deps=()) -> dict:
    consigna, formato = CONSIGNAS[tipo]
    carga = {"tipo": tipo, "consigna": consigna, "columnas": list(COLUMNAS), "registros": [list(r) for r in REGISTROS],
             "formato": formato}
    if tipo == "periodo":
        carga["desde"], carga["hasta"] = PERIODO
    if tipo in AUDITORIAS:
        carga["propuesta_a_auditar"] = {"pregunta": "importe total de todas las ventas", "importe": AUDITORIAS[tipo]}
    return {"nombre": tipo, "rol": "auditor" if tipo in AUDITORIAS else "calculista", "modelo": modelo, "carga": carga,
            "depende_de": list(deps), "max_intentos": max_intentos, "tiempo_max_s": tiempo_max_s}


def misiones(modelo: str = "simulado", max_intentos: int = 5, tiempo_max_s: int = 1) -> list[tuple[str, list[dict]]]:
    """Las misiones en su orden de creación (= orden de asignación de la cola)."""
    t = lambda tipo, deps=(): _tarea(tipo, modelo, max_intentos, tiempo_max_s, deps)  # noqa: E731
    return [("E · período del 1 al 10 de marzo", [t("periodo")]),
            ("A · importe total", [t("importe_total")]),
            ("B · unidades totales", [t("unidades_total")]),
            ("C+D · por producto y rankings", [t("por_producto"), t("mas_unidades", ["por_producto"]),
                                               t("mas_importe", ["por_producto"])]),
            ("F · auditoría ($220 y $235)", [t("auditar_220"), t("auditar_235")])]


def verificar(tipo: str, propuesta, v: dict) -> tuple[bool, str]:
    """El coordinador compara con SU verdad. El motivo de un rechazo nunca incluye el valor correcto."""
    esperado = v[tipo]
    if not isinstance(propuesta, dict) or set(propuesta) != set(esperado):
        return False, f"formato inválido: se esperaban exactamente las claves {sorted(esperado)}"
    if tipo == "por_producto":
        pp = propuesta["por_producto"]
        if not isinstance(pp, dict) or set(pp) != set(esperado["por_producto"]):
            return False, "formato inválido: tiene que haber una entrada por cada producto de los registros"
        malos = [p for p, x in pp.items() if x != esperado["por_producto"][p]]
        return (not malos, "coincide con el cálculo independiente" if not malos else
                f"no coincide con el cálculo independiente en: {', '.join(sorted(malos))}")
    for k, val in esperado.items():
        if isinstance(val, int) and (not isinstance(propuesta[k], int) or isinstance(propuesta[k], bool)):
            return False, f"formato inválido: «{k}» tiene que ser un entero"
    malos = [k for k in esperado if propuesta[k] != esperado[k]]
    return (not malos, "coincide con el cálculo independiente" if not malos else
            f"no coincide con el cálculo independiente en: {', '.join(malos)}")
