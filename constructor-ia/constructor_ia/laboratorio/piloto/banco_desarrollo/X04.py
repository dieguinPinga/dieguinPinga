import datetime


def medir(db, p):
    ancho = int(p.get("ancho_min", 15))
    top = int(p.get("top", 3))
    r = db.consultar("SELECT DATE_FORMAT(momento, '%Y-%m-%d %H:%i') AS minuto, valor FROM lecturas",
                     max_filas=50000)
    cobertura = {"leidas": 0, "validas": 0, "invalidas": 0}
    sumas = {}
    for minuto, valor in r.filas:
        cobertura["leidas"] += 1
        v = float(valor)
        if not 0.5 < v < 900:
            cobertura["invalidas"] += 1
            continue
        cobertura["validas"] += 1
        m = datetime.datetime.strptime(minuto, "%Y-%m-%d %H:%M")
        inicio = m - datetime.timedelta(minutes=m.minute % ancho)
        sumas[inicio] = sumas.get(inicio, 0.0) + v
    mejores = sorted(sumas.items(), key=lambda x: -x[1])[:top]
    ventanas = [{"inicio": i.strftime("%Y-%m-%d %H:%M"), "suma": round(s, 1)} for i, s in mejores]
    return {"resultado": {"ventanas": ventanas, "ancho_min": ancho},
            "cobertura": {"filas_analizadas": cobertura["leidas"], **cobertura},
            "limitaciones": ["valores fuera de (0.5, 900) se cuentan como inválidos"]}
