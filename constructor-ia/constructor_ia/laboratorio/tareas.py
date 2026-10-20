"""Tareas del laboratorio: preguntas sobre la base sintética y su puntaje contra la VERDAD medida directamente.

La verdad se mide con SQL como root sobre el MariaDB privado, fuera del sandbox y sin pasar por el modelo. El puntaje
es 1 (resuelta), 0.5 (parcial) o 0 y siempre va acompañado del detalle que lo justifica. Las preguntas no revelan
los fenómenos sembrados: el modelo tiene que diseñar el experimento que los encuentre.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from . import sintetica as S


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _rows(server, base, sql):
    return S.consultar_verdad(server, base, sql)


@dataclass
class Tarea:
    id: str
    titulo: str
    pregunta: str
    forma: dict                       # forma de la respuesta que se pide (ejemplo con tipos)
    pista_parametros: str = ""
    requiere: tuple = ()
    extra: dict = field(default_factory=dict)

    def verdad(self, server, base: str, info: dict) -> dict:
        return VERDADES[self.id](server, base, info)

    def puntuar(self, respuesta, verdad: dict) -> tuple[float, str]:
        if not isinstance(respuesta, dict):
            return 0.0, "sin respuesta en la forma pedida"
        try:
            return PUNTAJES[self.id](respuesta, verdad)
        except Exception as e:  # noqa: BLE001 — una respuesta mal formada no rompe el laboratorio
            return 0.0, f"respuesta no interpretable: {type(e).__name__}: {e}"


# Verdades -------------------------------------------------------------------------------------------------

def _v_t1(server, base, info):
    rows = _rows(server, base, "SELECT FLOOR(valor/2)*2 AS b, COUNT(*) FROM lecturas WHERE valor > 0.5 AND valor < 900 "
                               "GROUP BY b ORDER BY 2 DESC")
    top = int(rows[0][1])
    return {"intervalos_aceptables": [[float(b), float(b) + 2] for b, n in rows if int(n) >= 0.85 * top],
            "moda_ancho_2": [float(rows[0][0]), float(rows[0][0]) + 2]}


def _v_t2(server, base, info):
    rows = _rows(server, base, "SELECT prev, momento, TIMESTAMPDIFF(SECOND, prev, momento) FROM (SELECT momento, "
                               "LAG(momento) OVER (ORDER BY momento) AS prev FROM lecturas) x WHERE prev IS NOT NULL "
                               "ORDER BY 3 DESC LIMIT 1")
    return {"desde": rows[0][0], "hasta": rows[0][1], "segundos": int(rows[0][2])}


def _v_t3(server, base, info):
    g = _f(_rows(server, base, "SELECT AVG(valor) FROM lecturas WHERE valor > 0.5 AND valor < 900")[0][0])
    rows = _rows(server, base, "SELECT l.estacion_id, e.zona, AVG(l.valor) FROM lecturas l JOIN estaciones e ON "
                               "e.id = l.estacion_id WHERE l.valor > 0.5 AND l.valor < 900 GROUP BY l.estacion_id, e.zona")
    dist = [(int(e), z) for e, z, a in rows if _f(a) > g + 10]
    return {"estaciones": sorted(e for e, _ in dist), "zonas": sorted({z for _, z in dist}), "promedio_global": g}


def _ventana(info, extra):
    if extra.get("ventana_dias"):
        hasta = S.BASE + dt.timedelta(days=info["dias"])
        return (hasta - dt.timedelta(days=extra["ventana_dias"])).strftime("%Y-%m-%d"), hasta.strftime("%Y-%m-%d")
    return None


def _v_t4(server, base, info, ventana=None):
    where = f" WHERE momento >= '{ventana[0]}' AND momento < '{ventana[1]}'" if ventana else ""
    rows = _rows(server, base, "SELECT AVG(TIMESTAMPDIFF(SECOND, momento, recibido) > 3600), COUNT(*) FROM lecturas"
                 + where)
    return {"proporcion": _f(rows[0][0]), "filas": int(rows[0][1]), "ventana": list(ventana) if ventana else None}


def _v_t5(server, base, info):
    rows = _rows(server, base, "SELECT valor, COUNT(*) FROM lecturas WHERE valor > 900 OR valor = 0 GROUP BY valor")
    return {"anomalias": {str(_f(v)): int(n) for v, n in rows}}


def _v_r(server, base, info):
    return _v_t4(server, base, info, _ventana(info, {"ventana_dias": 3}))


VERDADES = {"T1": _v_t1, "T2": _v_t2, "T3": _v_t3, "T4": _v_t4, "T5": _v_t5, "R": _v_r, "C": _v_t3}


# Puntajes -------------------------------------------------------------------------------------------------

def _p_t1(r, v):
    d, h = _f(r.get("desde")), _f(r.get("hasta"))
    if d is None or h is None or h <= d:
        return 0.0, "faltan «desde» y «hasta» válidos"
    hit = any(d <= (a + b) / 2 <= h for a, b in v["intervalos_aceptables"])
    if hit and h - d <= 6:
        return 1.0, f"[{d}, {h}] contiene la concentración real {v['intervalos_aceptables']}"
    if hit and h - d <= 12:
        return 0.5, f"[{d}, {h}] contiene la concentración pero es demasiado ancho"
    return 0.0, f"[{d}, {h}] no contiene la concentración real {v['intervalos_aceptables']}"


def _ts(s):
    try:
        return dt.datetime.fromisoformat(str(s).strip().replace("T", " ")[:19])
    except ValueError:
        return None


def _p_t2(r, v):
    huecos = r.get("huecos") or ([r] if r.get("desde") else [])
    td, th = _ts(v["desde"]), _ts(v["hasta"])
    best, desc = 0.0, "ningún hueco reportado coincide con el real"
    for g in huecos if isinstance(huecos, list) else []:
        if not isinstance(g, dict):
            continue
        d, h = _ts(g.get("desde")), _ts(g.get("hasta"))
        if not d or not h or h <= d:
            continue
        overlap = (min(h, th) - max(d, td)).total_seconds()
        if overlap <= 0:
            continue
        frac = overlap / v["segundos"]
        if frac >= 0.5 and (h - d).total_seconds() <= 2 * v["segundos"] + 3600:
            return 1.0, f"hueco {d} → {h} coincide con el real {v['desde']} → {v['hasta']}"
        best, desc = 0.5, f"hueco {d} → {h} se superpone con el real pero no lo delimita"
    return best, desc


def _p_t3(r, v):
    texto = " ".join(str(x) for x in (r.get("grupo"), r.get("criterio"))).lower()
    est = {int(x) for x in (r.get("estaciones") or []) if str(x).strip().lstrip("-").isdigit()}
    real = set(v["estaciones"])
    jac = len(est & real) / len(est | real) if est else 0.0
    if any(z in texto for z in v["zonas"]) or jac >= 0.75:
        return 1.0, f"identificó el grupo distinto (zona {v['zonas']}, estaciones {sorted(real)}; Jaccard {jac:.2f})"
    if jac >= 0.4:
        return 0.5, f"identificó parte del grupo (Jaccard {jac:.2f} con {sorted(real)})"
    return 0.0, f"no identificó el grupo {v['zonas']} / {sorted(real)}"


def _p_prop(r, v):
    p = _f(r.get("proporcion"))
    if p is None:
        return 0.0, "falta «proporcion»"
    t = v["proporcion"]
    if p > 1 and t < 1:
        p = p / 100  # informada como porcentaje
    err = abs(p - t)
    if err <= max(0.002, 0.1 * t):
        return 1.0, f"{p:.4f} frente a {t:.4f} (error {err:.4f})"
    if err <= 0.3 * t:
        return 0.5, f"{p:.4f} frente a {t:.4f}: aproximada"
    return 0.0, f"{p:.4f} frente a {t:.4f}"


def _p_t5(r, v):
    rep = {}
    for a in r.get("anomalias") or []:
        if isinstance(a, dict) and _f(a.get("valor")) is not None:
            rep[_f(a.get("valor"))] = _f(a.get("cantidad"))
    score, notes = 0.0, []
    for val, n in v["anomalias"].items():
        got = rep.get(float(val))
        tolerancia = 0.05 * n if float(val) == 0 else 0  # la estación trabada: conteo aproximado aceptable
        if got is not None and abs(got - n) <= tolerancia:
            score += 0.5
            notes.append(f"{val}: {int(got)} correcto")
        else:
            notes.append(f"{val}: real {n}, informado {got}")
    return score, "; ".join(notes)


PUNTAJES = {"T1": _p_t1, "T2": _p_t2, "T3": _p_t3, "T4": _p_prop, "T5": _p_t5, "R": _p_prop, "C": _p_t3}

PISTA_VENTANA = ("El instrumento debe aceptar los parámetros opcionales «desde» y «hasta» (texto 'AAAA-MM-DD'; "
                 "momento >= desde y momento < hasta) para poder reutilizarlo después sobre otro período.")

TAREAS = {
    "T1": Tarea("T1", "concentración de valores",
                "¿En qué intervalo de valores se concentra la mayoría de las lecturas de la tabla lecturas?",
                {"desde": 0.0, "hasta": 0.0}),
    "T2": Tarea("T2", "huecos temporales",
                "¿Hubo períodos en los que no llegó ninguna lectura de ninguna estación? Indicá el más largo.",
                {"huecos": [{"desde": "AAAA-MM-DD HH:MM:SS", "hasta": "AAAA-MM-DD HH:MM:SS"}]}),
    "T3": Tarea("T3", "grupo con comportamiento distinto",
                "¿Hay algún grupo de estaciones cuyas lecturas se comporten de manera claramente distinta al resto? "
                "¿Qué tienen en común?",
                {"criterio": "texto", "grupo": "texto", "estaciones": [0]}),
    "T4": Tarea("T4", "lecturas tardías",
                "¿Qué proporción de las lecturas llegó («recibido») más de una hora después de su «momento»?",
                {"proporcion": 0.0}, pista_parametros=PISTA_VENTANA),
    "T5": Tarea("T5", "valores anómalos",
                "¿Hay valores imposibles o repetidos de forma sospechosa (fallas de sensor)? Indicá cada valor y "
                "cuántas veces aparece.",
                {"anomalias": [{"valor": 0.0, "cantidad": 0}]}),
    "R": Tarea("R", "reutilización con datos nuevos",
               "Llegaron datos nuevos. ¿Qué proporción de las lecturas con «momento» entre {desde} (incluido) y "
               "{hasta} (excluido) llegó más de una hora tarde?",
               {"proporcion": 0.0}, requiere=("T4",), extra={"ventana_dias": 3}),
    "C": Tarea("C", "corrección de un instrumento insuficiente",
               "¿Hay algún grupo de estaciones cuyas lecturas se comporten de manera claramente distinta al resto? "
               "¿Qué tienen en común?",
               {"criterio": "texto", "grupo": "texto", "estaciones": [0]}),
}

ORDEN = ("T1", "T2", "T3", "T4", "T5", "C", "R")

# Instrumento sembrado para la tarea C: corre bien y cumple el contrato, pero un resumen global no puede responder
# qué GRUPO se comporta distinto. Se espera que el modelo lo note y escriba una versión nueva.
INSUFICIENTE = '''
def medir(db, p):
    r = db.consultar("SELECT MIN(valor) AS minimo, MAX(valor) AS maximo, AVG(valor) AS promedio, COUNT(*) AS n "
                     "FROM lecturas")
    mn, mx, pr, n = r.filas[0]
    return {"resultado": {"minimo": float(mn), "maximo": float(mx), "promedio": round(float(pr), 3)},
            "cobertura": {"filas_analizadas": int(n), "metodo": "agregado global en SQL"},
            "limitaciones": ["resumen de toda la tabla"]}
'''


def seleccionar(ids: list[str]) -> list[Tarea]:
    """Tareas pedidas en el orden canónico, agregando sus requisitos (R necesita el instrumento de T4)."""
    want = set()
    for i in ids:
        i = i.strip().upper()
        if i not in TAREAS:
            raise ValueError(f"tarea desconocida: {i} (válidas: {', '.join(ORDEN)})")
        want.add(i)
        want.update(TAREAS[i].requiere)
    return [TAREAS[i] for i in ORDEN if i in want]


def pregunta(t: Tarea, info: dict) -> str:
    v = _ventana(info, t.extra)
    return t.pregunta.format(desde=v[0], hasta=v[1]) if v else t.pregunta
