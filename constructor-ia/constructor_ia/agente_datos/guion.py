"""Modelo de GUION para las pruebas sin Qwen: responde las microtareas leyendo SOLO su prompt.

Valida el CIRCUITO (memoria, vigencias, reinicio, verificación, juez, fugas), NO la capacidad de un modelo. No lee la
verdad del generador. Tiene un vocabulario mínimo que hace de «conocimiento del idioma» del modelo (por ejemplo,
que «cantidad» en una línea de pedido son unidades vendidas y que un «borrador» no está confirmado): está escrito
para ejercitar el circuito con la base B1, así que un acierto del guion NO dice nada sobre Qwen.

Perfiles (para las pruebas negativas):
- competente: describe, elige y mide bien;
- senuelo: describe los borradores como ventas y los usa para responder;
- fecha_inclusiva: filtra «entre el 1 y el 31» (el error clásico en DATETIME) y lo corrige si se le explica;
- terco: el mismo error, sin corregirlo nunca;
- multiplica: cuenta pedidos agrupando por una tabla más detallada (los JOIN duplican filas);
- sql_libre: responde con SQL propio en lugar de MEDIR;
- ambiguo: en M2 da dos listas de tablas distintas la primera vez.
"""
from __future__ import annotations

import datetime as dt
import re

from . import texto as TX

FILA_POR_NOMBRE = [("borrador", "un borrador de pedido sin confirmar (no es una venta)"),
                   ("linea", "una línea de un pedido confirmado"), ("pedido", "un pedido confirmado de un cliente"),
                   ("cliente", "un cliente del comercio"), ("producto", "un producto a la venta"),
                   ("categoria", "un rubro de productos")]
GLOSA = {"cantidad": "unidades vendidas", "precio_unitario": "precio cobrado por unidad", "fecha": "fecha de la compra",
         "precio_lista": "precio de lista", "creado": "fecha del borrador", "nombre": "nombre", "ciudad": "ciudad"}


class Guion:
    def __init__(self, perfil: str = "competente"):
        self.perfil = perfil
        self.nombre, self.system = f"guion:{perfil}", ""
        self.llamadas = []

    def pedir(self, tipo, prompt, *, json_out=False, cancel=None, corto=False, seed=None):
        self.llamadas.append(tipo)
        mt = tipo.split(":")[-1]
        resp = {"M1": self.m1, "M2": self.m2, "M3": self.m3, "M4": self.m4}[mt](prompt)
        return resp, {"tipo": tipo, "resultado": "completa", "reloj_ms": 1}

    # M1 -------------------------------------------------------------------------------------------------------
    def m1(self, prompt: str) -> str:
        rels = dict(re.findall(r"^- (\w+\.\w+) → (\w+\.\w+) \([^)]*contención comprobada", prompt, re.M))
        out = []
        for m in re.finditer(r"=== TABLA (\w+) \(P\d+\) ===\n(.*?)(?=\n=== TABLA |\nRelaciones medidas|\Z)", prompt, re.S):
            tabla, cuerpo = m.group(1), m.group(2)
            fila = next((f for k, f in FILA_POR_NOMBRE if k in tabla), f"un registro de {tabla}")
            senuelo = self.perfil == "senuelo" and "borrador" in tabla
            if senuelo:
                fila = "una venta de un producto a un cliente"
            cols = re.findall(r"^- (\w+) (\S+)( clave PRI)?(?=([^\n]*· frecuentes)?)[^\n]*$", cuerpo, re.M)
            glosas, lineas = [], []
            for nombre, tipo, pk, frec in cols:
                t = tipo.lower()
                if pk:
                    rol = "identificador"
                elif f"{tabla}.{nombre}" in rels:
                    rol = f"referencia {rels[tabla + '.' + nombre]}"
                elif t.startswith(("date", "timestamp")):
                    rol = "tiempo"
                elif t.startswith(("int", "decimal", "float", "double", "tinyint", "bigint", "smallint")):
                    rol = "medida"
                elif frec:
                    rol = "categoria"
                else:
                    rol = "texto"
                lineas.append(f"COLUMNA: {nombre} = {rol}")
                g = GLOSA.get(nombre)
                if g and not pk and not rol.startswith("referencia"):
                    if nombre == "cantidad" and "borrador" in tabla and not senuelo:
                        g = "unidades del borrador"
                    glosas.append(g)
            out.append(f"TABLA: {tabla}\nFILA: {fila}" + (f": {', '.join(glosas)}" if glosas else "") + "\n"
                       + "\n".join(lineas))
        return "\n\n".join(out)

    # M2 -------------------------------------------------------------------------------------------------------
    def m2(self, prompt: str) -> str:
        pregunta = re.search(r"PREGUNTA: (.*)", prompt).group(1)
        fichas = {m.group(1): (m.group(2), m.group(3)) for m in
                  re.finditer(r"^- (\w+): (.*)\n  columnas: (.*)$", prompt, re.M)}
        if self.perfil == "ambiguo" and "TU RESPUESTA ANTERIOR" not in prompt:
            return "TABLAS: lineas_pedido, productos\nTABLAS: pedidos_borrador, productos\nPOR_QUE: dudo"
        return f"TABLAS: {', '.join(self._tablas(pregunta, fichas))}\nPOR_QUE: medida, entidad y fecha"

    def _tablas(self, pregunta: str, fichas: dict) -> list[str]:
        ent = TX.raiz(TX.entidad(pregunta) or "")
        terms = set(TX.terminos(pregunta))
        entidad = next((t for t in fichas if TX.raiz(t) == ent or ent in TX.raices(t)), None)

        def puntaje(t):
            fila, cols = fichas[t]
            return (len(terms & TX.raices(fila)), cols.count("(medida"))
        candidatas = [t for t in fichas if t != entidad]
        if self.perfil == "senuelo":
            medida = next(t for t in fichas if "borrador" in t)
        else:
            medida = max(sorted(candidatas), key=puntaje)
        tablas = [medida] + ([entidad] if entidad else [])
        if TX.periodo(pregunta) and "(tiempo" not in fichas[medida][1]:
            tiempo = next((t for t in sorted(fichas) if "(tiempo" in fichas[t][1] and t not in tablas
                           and "borrador" not in t), None)
            if tiempo:
                tablas.append(tiempo)
        return tablas

    # M3 -------------------------------------------------------------------------------------------------------
    def m3(self, prompt: str) -> str:
        pregunta = re.search(r"PREGUNTA: (.*)", prompt).group(1)
        bloques = re.findall(r"^- (\w+): .*?\n<<<DATOS[^\n]*\n((?:  - .*\n)+)<<<FIN", prompt, re.M)
        cols = {t: re.findall(r"^  - (\w+) (\S+).*?· rol (\w+)", cuerpo, re.M) for t, cuerpo in bloques}
        terms = set(TX.terminos(pregunta))
        ent = TX.raiz(TX.entidad(pregunta) or "")
        medidas = [(t, c) for t, cs in cols.items() for c, _, rol in cs if rol == "medida"]
        entidad = next((t for t in cols if TX.raiz(t) == ent), None)
        if self.perfil == "multiplica":
            return (f"MEDIR: agregado\nMEDIDA: conteo pedidos.id\nAGRUPAR: productos.nombre\n"
                    f"FILTRO: pedidos.fecha desde 2026-03-01 hasta 2026-04-01\nORDEN: mayor\nLIMITE: 1")
        if terms & {"gast", "impo", "mont", "pago"}:
            t = next(t for t, _ in medidas if t != entidad)
            par = [c for tt, c in medidas if tt == t][:2]
            medida = f"suma {t}.{par[0]} * {t}.{par[1]}"
        else:
            t, c = next((t, c) for t, c in medidas if t != entidad and "precio" not in c)
            medida = f"suma {t}.{c}"
        grupo = next((f"{entidad}.{c}" for c, _, rol in cols.get(entidad, []) if rol in ("texto", "categoria")), None)
        tiempo = next((f"{t}.{c}" for t, cs in cols.items() for c, _, rol in cs if rol == "tiempo"), None)
        lineas = ["MEDIR: agregado", f"MEDIDA: {medida}"]
        if grupo:
            lineas.append(f"AGRUPAR: {grupo}")
        per = TX.periodo(pregunta)
        if per and tiempo:
            ultimo = per["hasta"] - dt.timedelta(days=1)
            corrige = self.perfil == "fecha_inclusiva" and "deja afuera" in prompt
            if self.perfil in ("fecha_inclusiva", "terco") and not corrige:
                lineas.append(f"FILTRO: {tiempo} entre {per['desde']:%Y-%m-%d} y {ultimo:%Y-%m-%d}")
            else:
                lineas.append(f"FILTRO: {tiempo} desde {per['desde']:%Y-%m-%d} hasta {per['hasta']:%Y-%m-%d}")
        it = TX.intencion(pregunta)
        if it["orden"]:
            lineas += [f"ORDEN: {it['orden']}", "LIMITE: 1"]
        for w in TX.palabras(pregunta):
            if TX.raiz(w) in terms:
                lineas.append(f"INTERPRETA: {w} = {medida}")
        if self.perfil == "sql_libre":
            return self._sql(medida, grupo, tiempo, per)
        return "\n".join(lineas)

    @staticmethod
    def _sql(medida, grupo, tiempo, per) -> str:
        # Escrito a mano para la base B1 (es un guion): ventas por producto en el período.
        return ("SQL: SELECT p.nombre, SUM(l.cantidad) AS unidades FROM lineas_pedido l "
                "JOIN pedidos o ON l.pedido_id = o.id JOIN productos p ON l.producto_id = p.id "
                f"WHERE o.fecha >= '{per['desde']:%Y-%m-%d}' AND o.fecha < '{per['hasta']:%Y-%m-%d}' "
                "GROUP BY p.nombre ORDER BY unidades DESC, p.nombre LIMIT 1")

    # M4 -------------------------------------------------------------------------------------------------------
    def m4(self, prompt: str) -> str:
        m = re.search(r"<<<DATOS NO CONFIABLES[^\n]*\n(.*?)\n(.*?)\n", prompt, re.S)
        cab = [x.strip() for x in m.group(1).split("|")]
        fila = [x.strip() for x in m.group(2).split("|")]
        if len(fila) == 1:
            return f"RESPUESTA: El resultado es {fila[0]}."
        return f"RESPUESTA: Fue «{' / '.join(fila[:-1])}», con {fila[-1]}."
