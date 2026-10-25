"""Tabla de posiciones de un torneo.

Cada partido es (local, goles_local, visitante, goles_visitante). Victoria: 3 puntos; empate: 1 para cada
uno; derrota: 0. tabla() devuelve una lista de dicts {"equipo", "pj", "pts", "gf", "gc", "dg"}, con
dg = gf - gc, ordenada por puntos, después por diferencia de gol, después por goles a favor (todo de mayor
a menor) y por último por nombre en orden alfabético.
"""


def tabla(partidos):
    filas = {}

    def fila(equipo):
        return filas.setdefault(equipo, {"equipo": equipo, "pj": 0, "pts": 0, "gf": 0, "gc": 0})

    for local_nombre, goles_local, visitante_nombre, goles_visitante in partidos:
        local, visitante = fila(local_nombre), fila(visitante_nombre)
        local["pj"] += 1
        visitante["pj"] += 1
        local["gf"] += goles_local
        local["gc"] += goles_visitante
        visitante["gf"] += goles_visitante
        visitante["gc"] += goles_visitante
        if goles_local > goles_visitante:
            local["pts"] += 3
        elif goles_local < goles_visitante:
            visitante["pts"] += 3
        else:
            local["pts"] += 1
            visitante["pts"] += 1
    resultado = [dict(f, dg=f["gf"] - f["gc"]) for f in filas.values()]
    return sorted(resultado, key=lambda f: (-f["pts"], -f["dg"], -f["gf"], f["equipo"]))
