"""E0 del próximo experimento de elección (PILOTO.md, «E0: ¿la evidencia alcanza, sin modelo?»).

Pregunta: con la evidencia que el COORDINADOR tiene o puede calcular (instrumento, datos, verificaciones del
coordinador, candidatas, traza), ¿una regla fija y genérica elige la instrucción causante? Si una regla no puede,
no tiene sentido pedírselo a Qwen con esa evidencia.

Cinco lecturas; cada una se ABSTIENE si su criterio no aplica o si no da una única candidata:

* R1 lugar del error (evidencia ya mostrada): el instrumento se cae y el lugar de la excepción es una candidata.
* R2 último escritor (traza ya mostrada): la candidata escribió un valor que llega a la salida reemplazando otro
  distinto, y el valor que otra candidata dejó en ese mismo nombre se pierde.
* R3 acumulación sin efecto (traza ya mostrada): una acumulación (x += e, d[k] = d.get(k) + e) que se ejecutó y
  nunca cambió el valor que escribe.
* R4 contraste clave/valor (evidencia nueva del verificador): en una agregación con clave (d[k] = …), el verificador
  de contraste dice si lo erróneo es la CLAVE (no está alineada, o la suma informada pertenece a otra clave) o el
  VALOR; la regla elige la candidata del camino estático de la clave o del valor.
* R5 omisión (intervención): si esa instrucción no se ejecutara, ¿pasan todas las verificaciones?
* R6 escala del efecto (intervención): en una acumulación (x += e, d[k] = d.get(k) + e), ¿algún factor k en
  {−1, ½, 2} sobre el efecto hace pasar todas las verificaciones?

Las intervenciones usan solo las verificaciones del coordinador. CUIDADO: los mutantes del banco son cambios de un
punto (constante ±1, operador), así que R5 (y en parte R4) tienen ventaja circular con ellos; por eso todo se
informa también por ORIGEN: «observado» (D1, D2) frente a «mutante».

Una intervención solo «repara» si la salida conserva su FORMA (mismas claves, mismos largos de listas): una salida
vacía pasa las verificaciones sin nada que verificar (se vio en E0: omitir la acumulación de sumas deja 0 ventanas).

Las reglas se escribieron mirando estos mismos casos: E0 dice qué es posible EN PRINCIPIO. Para generalizar hacen
falta defectos escritos por otros, que no se miren antes de congelar las reglas.

La identidad de la instrucción sembrada se usa solo para puntuar.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import time
from collections import Counter

from . import banco as B
from . import eleccion as E
from . import localizador as L
from . import parches as P
from . import traza as T
from . import verificaciones as V

ESCALAS = (-1, 0.5, 2)
ORDEN = ("R1_lugar_del_error", "R2_ultimo_escritor", "R3_acumulacion_sin_efecto", "R4_contraste_clave_valor",
         "R5_omision", "R6_escala")
PRESENTADA = {"R1_lugar_del_error": True, "R2_ultimo_escritor": True, "R3_acumulacion_sin_efecto": True,
              "R4_contraste_clave_valor": False, "R5_omision": False, "R6_escala": False}

# Verificador de CONTRASTE del coordinador (solo E0): igual que VERIFICADOR_FIJO, más tres hechos por ventana mal
# sumada: si el inicio está alineado (lo exige la pregunta), si la suma informada es la de otra ventana cercana y si
# es la medida con el signo opuesto.
VERIFICADOR_CONTRASTE = '''
import datetime

def medir(db, p):
    s = p["salida"]
    ancho = int(p["parametros"].get("ancho_min", 15))
    malos, ejemplos = set(), []
    total = int(db.consultar("SELECT COUNT(*) FROM lecturas").filas[0][0])
    if s["cobertura"].get("leidas") != total:
        malos.update(["leidas", "filas_analizadas"])
        ejemplos.append({"campo": "leidas", "informado": s["cobertura"].get("leidas"), "esperado": total})

    def suma(ini):
        fin = ini + datetime.timedelta(minutes=ancho)
        r = db.consultar("SELECT COALESCE(SUM(valor), 0) FROM lecturas WHERE valor > 0.5 AND valor < 900 AND "
                         "momento >= '" + ini.strftime("%Y-%m-%d %H:%M:%S") + "' AND momento < '"
                         + fin.strftime("%Y-%m-%d %H:%M:%S") + "'")
        return float(r.filas[0][0])

    for w in s["resultado"]["ventanas"]:
        ini = datetime.datetime.strptime(w["inicio"], "%Y-%m-%d %H:%M")
        medido = suma(ini)
        if abs(medido - w["suma"]) > 0.01:
            malos.update(["inicio", "suma"])
            es_de = None
            for k in (1, -1, 2, -2):
                otra = ini + datetime.timedelta(minutes=k * ancho)
                if abs(suma(otra) - w["suma"]) <= 0.01:
                    es_de = otra.strftime("%Y-%m-%d %H:%M")
                    break
            ejemplos.append({"campo": "ventana", "inicio": w["inicio"], "informado": w["suma"], "esperado": medido,
                             "inicio_alineado": ini.minute % ancho == 0, "la_suma_es_de": es_de,
                             "signo_opuesto": abs(medido + w["suma"]) <= 0.01})
    return {"resultado": {"ok": not malos, "campos": sorted(malos), "contraejemplos": ejemplos[:3]},
            "cobertura": {"filas_analizadas": total}, "limitaciones": []}
'''


# Intervenciones (transformaciones del código del instrumento; no las ve ningún modelo) -------------------------

def _reemplazar(codigo: str, linea: int, cambiar) -> str | None:
    tree = ast.parse(codigo)
    for nodo in ast.walk(tree):
        for campo in ("body", "orelse", "finalbody"):
            lista = getattr(nodo, campo, None)
            if not isinstance(lista, list):
                continue
            for i, st in enumerate(lista):
                if isinstance(st, ast.stmt) and st.lineno == linea:
                    nuevo = cambiar(st)
                    if nuevo is None:
                        return None
                    lista[i] = nuevo
                    return ast.unparse(ast.fix_missing_locations(tree)) + "\n"
    return None


def omitir(codigo: str, linea: int) -> str | None:
    return _reemplazar(codigo, linea, lambda st: ast.Pass())


def _por(k, expr):
    return ast.BinOp(left=ast.Constant(k), op=ast.Mult(), right=expr)


def acumula(st) -> bool:
    """x += e, x -= e, o x = <algo que lee x> ± e."""
    if isinstance(st, ast.AugAssign):
        return isinstance(st.op, (ast.Add, ast.Sub))
    if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.value, ast.BinOp) \
            and isinstance(st.value.op, (ast.Add, ast.Sub)):
        base = L._base(st.targets[0])
        return bool(base and base in L._nombres(st.value.left))
    return False


def escalar(codigo: str, linea: int, k) -> str | None:
    """Multiplica por k el EFECTO de una acumulación; None si la instrucción no acumula."""
    def cambiar(st):
        if not acumula(st):
            return None
        if isinstance(st, ast.AugAssign):
            st.value = _por(k, st.value)
        else:
            st.value.right = _por(k, st.value.right)
        return st
    return _reemplazar(codigo, linea, cambiar)


# Caminos estáticos de una agregación con clave ------------------------------------------------------------------

def _clausura(codigo: str, nombres: set[str]) -> set[str]:
    """Nombres de los que dependen `nombres` (definiciones del propio programa, transitivamente)."""
    usa = {}
    for u in P.instrucciones(codigo):
        d, us = L._defs_uses(u)
        for n in d:
            usa.setdefault(n, set()).update(us - {n})
    out, pend = set(), list(nombres)
    while pend:
        n = pend.pop()
        if n not in out:
            out.add(n)
            pend += list(usa.get(n, ()))
    return out


def caminos(codigo: str) -> list[dict]:
    """Cada asignación d[k] = … dentro de un bucle: la línea, los nombres de la clave y los del valor."""
    out = []
    for bucle in (n for n in ast.walk(ast.parse(codigo)) if isinstance(n, (ast.For, ast.While))):
        for st in ast.walk(bucle):
            if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Subscript):
                base = L._base(st.targets[0])
                clave = _clausura(codigo, L._nombres(st.targets[0].slice))
                valor = _clausura(codigo, L._nombres(st.value) - {base}) - clave
                out.append({"linea": st.lineno, "clave": clave, "valor": valor})
    return out


# Lecturas ---------------------------------------------------------------------------------------------------------

def _unidades(caso):
    return [P.unidad_en(caso["codigo"], ln) for ln in caso["candidatas"]]


def _cubre(u: P.Unidad, linea: int) -> bool:
    return u.lineno <= linea <= u.end_lineno


def r1_lugar(caso) -> list[int]:
    lineas = set()
    for f in caso["fallas"]:
        if isinstance(f.get("lugar"), dict) and f["lugar"].get("archivo") == "instrumento.py":
            lineas.add(int(f["lugar"]["linea"]))
        if f["id"] == "S1_medicion":
            lineas |= {int(x) for x in re.findall(r'instrumento\.py", line (\d+), in medir', f.get("detalle") or "")}
    return [u.lineno for u in _unidades(caso) if any(_cubre(u, ln) for ln in lineas)]


def r2_escritor(caso) -> list[int]:
    us = _unidades(caso)
    rs = {u.lineno: T.rasgos(caso.get("traza"), u) for u in us}
    out = []
    for u in us:
        r = rs[u.lineno]
        llega = {n for n, ok in r["llega"].items() if ok}
        if r["constante_reemplaza"] and llega and any(set(rs[o.lineno]["pierde"]) & llega for o in us if o != u):
            out.append(u.lineno)
    return out


def r3_sin_efecto(caso) -> list[int]:
    out = []
    for u in _unidades(caso):
        d = ((caso.get("traza") or {}).get("lineas") or {}).get(str(u.lineno)) or {}
        muestras = [m for m in (d.get("muestras") or []) + [d.get("ultima")] if m and m.get("despues")]
        if acumula(u.nodo) and d.get("veces", 0) > 1 and muestras and all(m["antes"] == m["despues"] for m in muestras):
            out.append(u.lineno)
    return out


def r4_contraste(caso, contraste: dict | None) -> tuple[list[int], str | None]:
    pares = [x for x in ((contraste or {}).get("contraejemplos") or []) if x.get("campo") == "ventana"]
    votos = Counter("clave" if (not x.get("inicio_alineado") or x.get("la_suma_es_de")) else "valor" for x in pares)
    if not votos or (len(votos) == 2 and votos["clave"] == votos["valor"]):
        return [], None
    camino = votos.most_common(1)[0][0]
    out = []
    for c in caminos(caso["codigo"]):
        for u in _unidades(caso):
            defs, _ = L._defs_uses(u)
            if camino == "clave" and defs & c["clave"]:
                out.append(u.lineno)
            if camino == "valor" and (u.lineno == c["linea"] or defs & c["valor"]):
                out.append(u.lineno)
    return sorted(set(out)), camino


def forma(x):
    """Claves de los dicts y largos de las listas (sin valores)."""
    if isinstance(x, dict):
        return {k: forma(v) for k, v in x.items()}
    if isinstance(x, list):
        return [forma(v) for v in x[:1]] + [len(x)]
    return None


def _sin_fallas(ev) -> bool:
    return all(c["ok"] for c in ev["checks"].values()) and not ev["invalidos"]


class Intervenciones:
    """Ejecuta versiones intervenidas del instrumento con la suite del coordinador (S1–S3 + VERIFICADOR_FIJO)."""

    def __init__(self, ent, almacen, vf_id):
        self.ent, self.alm, self.vf, self.n = ent, almacen, vf_id, 0
        self.forma = None          # forma de la salida del caso actual (la intervención debe conservarla)
        self.vacias = 0            # intervenciones que pasaron las verificaciones solo porque cambió la forma

    def repara(self, codigo: str | None) -> bool | None:
        if codigo is None:
            return None
        self.n += 1
        try:
            iid = self.alm.save(f"e0_{self.n}", codigo, B.MANIFIESTO, author="laboratorio")["id"]
        except Exception:                # noqa: BLE001 - un código intervenido que la revisión rechaza no repara
            return False
        ev = V.evaluar(self.ent, self.alm, iid, B.PARAMS_VISIBLES, [{"id": "VF", "instrumento_id": self.vf}])
        if not _sin_fallas(ev):
            return False
        if self.forma is not None and forma(ev["salida"]) != self.forma:
            self.vacias += 1
            return False
        return True


def r5_omision(caso, iv: Intervenciones) -> list[int]:
    return [ln for ln in caso["candidatas"] if iv.repara(omitir(caso["codigo"], ln))]


def r6_escala(caso, iv: Intervenciones) -> tuple[list[int], dict]:
    out, factores = [], {}
    for ln in caso["candidatas"]:
        for k in ESCALAS:
            if iv.repara(escalar(caso["codigo"], ln, k)):
                out.append(ln)
                factores[ln] = k
                break
    return out, factores


# Corrida ----------------------------------------------------------------------------------------------------------

def huella_reglas() -> str:
    """sha256 de este archivo: las reglas quedan congeladas antes de ver defectos nuevos (PILOTO.md)."""
    from pathlib import Path
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]


def version_python(cfg) -> str:
    try:
        return subprocess.run([cfg.sandbox_python, "-c", "import sys; print(sys.version.split()[0])"],
                              capture_output=True, text=True, timeout=20).stdout.strip() or "?"
    except (OSError, subprocess.SubprocessError):
        return "?"


def huellas(caso) -> dict:
    h = lambda x: hashlib.sha256((x if isinstance(x, str) else json.dumps(x, sort_keys=True, ensure_ascii=False,
                                                                          default=str)).encode()).hexdigest()[:12]
    return {k: h(caso.get(k)) for k in ("codigo", "salida", "texto_fallas", "traza")}


def correr(ent, *, incluir_mutantes: bool = True, externos: list[dict] | None = None, log=print) -> dict:
    """externos: defectos escritos por otros (defectos.cargar); si se pasan, se evalúan SOLO ellos."""
    t0 = time.time()
    clase_ext = {d["id"]: d["clase"] for d in externos or []}
    info = E.construir_casos(ent, incluir_mutantes=incluir_mutantes, solo_externos=bool(externos),
                             externos=[(d["id"], d["descripcion"], d["codigo"], d["lineas"]) for d in externos or []],
                             log=lambda *_: None)
    for c in info["casos"]:
        if c["id"] in clase_ext:
            c["clase"] = clase_ext[c["id"]]
    cobertura = {"casos": len(info["casos"]),
                 "equivalentes": [c["id"] for c in info["casos"] if c["equivalente"]],
                 "no_detectados": [c["id"] for c in info["casos"] if not c["equivalente"] and not c.get("detectado")],
                 "sembrada_fuera_de_las_candidatas": [c["id"] for c in info["casos"] if c.get("detectado")
                                                      and not c.get("sembrada_en")]}
    casos = [c for c in info["casos"] if c.get("sembrada_en")]
    alm = ent.almacen(f"e0_{int(time.time() * 1000)}")
    vf = alm.save("verificador_fijo", E.VERIFICADOR_FIJO, {"proposito": "verificador"}, author="laboratorio")["id"]
    vc = alm.save("verificador_contraste", VERIFICADOR_CONTRASTE, {"proposito": "verificador"},
                  author="laboratorio")["id"]
    iv = Intervenciones(ent, alm, vf)
    filas = []
    for caso in casos:
        a, b = caso["sembrada"]
        sembrada = [u.lineno for u in _unidades(caso) if u.lineno <= b and a <= u.edit_end]
        contraste = None
        if caso.get("salida") is not None:
            run = V._run(ent, alm, vc, {"parametros": B.PARAMS_VISIBLES, "salida": caso["salida"]})
            contraste = (run.get("output") or {}).get("resultado") if run["status"] == "ok" else None
        r4, camino = r4_contraste(caso, contraste)
        iv.forma = forma(caso["salida"]) if caso.get("salida") is not None else None
        r6, factores = r6_escala(caso, iv)
        cumplen = {"R1_lugar_del_error": r1_lugar(caso), "R2_ultimo_escritor": r2_escritor(caso),
                   "R3_acumulacion_sin_efecto": r3_sin_efecto(caso), "R4_contraste_clave_valor": r4,
                   "R5_omision": r5_omision(caso, iv), "R6_escala": r6}
        lect = {k: {"cumplen": v, "eleccion": v[0] if len(v) == 1 else None,
                    "acierta": (v[0] in sembrada) if len(v) == 1 else None} for k, v in cumplen.items()}
        combinada = next((dict(lectura=k, **lect[k]) for k in ORDEN if lect[k]["eleccion"] is not None),
                         {"lectura": None, "eleccion": None, "acierta": None})
        filas.append({"id": caso["id"], "descripcion": caso["descripcion"], "clase": caso["clase"],
                      "origen": "externo" if caso["id"] in clase_ext else
                      "mutante" if caso["id"].startswith("M") else "observado",
                      "candidatas": caso["candidatas"], "sembrada": sembrada, "lecturas": lect,
                      "combinada": combinada, "camino_contraste": camino, "factor_escala": factores,
                      "contraste": contraste, "traza_completa": (caso.get("traza") or {}).get("completa"),
                      "huellas": huellas(caso)})
        log(f"  {caso['id']:5} {caso['clase']:13} " + " ".join(
            f"{k.split('_')[0]}={'·' if v['eleccion'] is None else ('✓' if v['acierta'] else '✗')}"
            for k, v in lect.items()) + f" → combinada {combinada['lectura'] or 'se abstiene'}")
    ent.liberar(alm)
    return {"python_sandbox": version_python(ent.cfg), "huella_reglas": huella_reglas(), "cobertura": cobertura,
            "casos": filas,
            "resumen": resumir(filas),
            "ejecuciones_intervenidas": iv.n, "rechazadas_por_forma": iv.vacias, "segundos": round(time.time() - t0)}


def resumir(filas: list[dict]) -> dict:
    def grupo(fs):
        out = {"casos": len(fs)}
        for k in ORDEN + ("combinada",):
            sel = [f["combinada"] if k == "combinada" else f["lecturas"][k] for f in fs]
            resp = [s for s in sel if s["eleccion"] is not None]
            out[k] = {"responde": len(resp), "acierta": sum(1 for s in resp if s["acierta"])}
        return out
    claves = sorted({(f["clase"], f["origen"]) for f in filas})
    return {"total": grupo(filas), **{f"{c} / {o}": grupo([f for f in filas if (f["clase"], f["origen"]) == (c, o)])
                                      for c, o in claves}}


def markdown(res: dict) -> str:
    Lm = ["# E0 del experimento de elección (sin modelo)", "", f"Huella de las reglas: `{res['huella_reglas']}`", "",
          f"Python del sandbox: {res['python_sandbox']} · {len(res['casos'])} casos con la sembrada entre las "
          f"candidatas · {res['ejecuciones_intervenidas']} versiones intervenidas ({res['rechazadas_por_forma']} pasaban "
          f"las verificaciones solo porque cambiaba la forma de la salida) · {res['segundos']} s", "",
          f"Cobertura: {res['cobertura']['casos']} defectos · equivalentes {res['cobertura']['equivalentes']} · no "
          f"detectados {res['cobertura']['no_detectados']} · la causa NO quedó entre las 3 candidatas (ninguna "
          f"lectura puede acertar) {res['cobertura']['sembrada_fuera_de_las_candidatas']}", "",
          "Celda = responde / acierta (una lectura responde solo si su criterio da UNA candidata). «Se abstiene» es "
          "donde el coordinador tendría que consultar a Qwen o seguir investigando.", "",
          "| grupo | casos | " + " | ".join(k + (" (ya mostrada)" if PRESENTADA[k] else "") for k in ORDEN)
          + " | combinada |", "|" + "---|" * (len(ORDEN) + 3)]
    for g, r in res["resumen"].items():
        Lm.append(f"| {g} | {r['casos']} | " + " | ".join(f"{r[k]['responde']}/{r[k]['acierta']}"
                                                          for k in ORDEN + ("combinada",)) + " |")
    Lm += ["", "## Por caso", ""]
    for f in res["casos"]:
        Lm.append(f"- **{f['id']}** ({f['clase']}, {f['origen']}; candidatas {f['candidatas']}, sembrada "
                  f"{f['sembrada']}): " + "; ".join(
                      f"{k.split('_')[0]} cumplen {v['cumplen']}" for k, v in f["lecturas"].items())
                  + f" → **{f['combinada']['lectura'] or 'se abstiene'}**"
                  + (f" ({'✓' if f['combinada']['acierta'] else '✗'})" if f['combinada']['lectura'] else "")
                  + (f" · contraste: camino de la {f['camino_contraste']}" if f["camino_contraste"] else "")
                  + (f" · escala {f['factor_escala']}" if f["factor_escala"] else ""))
    return "\n".join(Lm) + "\n"
