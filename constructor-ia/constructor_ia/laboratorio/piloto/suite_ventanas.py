"""Verificaciones del COORDINADOR para la pregunta de las ventanas (PILOTO.md, «Ciclo v1.3.3»).

Encapsuladas acá para reutilizarlas: dependen de la PREGUNTA (qué es una lectura válida, qué cuenta la cobertura),
nunca de un defecto en particular. Cada verificación es un instrumento que corre en el sandbox, recibe
p["parametros"] y p["salida"], vuelve a medir con SQL propio y devuelve {"ok", "campos", "contraejemplos"}.

* VF (verificador fijo, sin cambios desde la Etapa 1): el total de filas y la suma de cada ventana informada.
* VC_cobertura (v1.3.3): el RECUENTO, contra SQL independiente del código investigado:
  - `leidas` = lecturas que el instrumento procesó: todas las de la tabla o, si su límite efectivo de filas
    (`p["limite_filas"]`, deducido del código) es menor, ese límite;
  - `validas` = las que cumplen 0.5 < valor < 900; `invalidas` = las restantes;
  - `filas_analizadas` = `leidas`.
  No alcanza con leidas = validas + invalidas: una reparación puede compensar un contador con otro. Si el
  instrumento recorta, el reparto no es verificable (las filas recortadas no tienen orden definido) y se informa.
"""
from __future__ import annotations

import ast

from . import eleccion as EL

LIMITE_POR_OMISION = 10000          # db.consultar(sql) sin max_filas (constructor_ia/sandbox/investiga.py)

VERIFICADOR_COBERTURA = '''
def medir(db, p):
    s = p["salida"]
    cob = s.get("cobertura") or {}
    total = int(db.consultar("SELECT COUNT(*) FROM lecturas").filas[0][0])
    validas_sql = int(db.consultar("SELECT COUNT(*) FROM lecturas WHERE valor > 0.5 AND valor < 900").filas[0][0])
    limite = p.get("limite_filas")
    recorta = limite is not None and total > limite
    esperado = {"leidas": limite if recorta else total, "filas_analizadas": limite if recorta else total}
    if not recorta:
        esperado["validas"] = validas_sql
        esperado["invalidas"] = total - validas_sql
    malos, ejemplos = [], []
    for campo, valor in esperado.items():
        if cob.get(campo) != valor:
            malos.append(campo)
            ejemplos.append({"campo": campo, "informado": cob.get(campo), "esperado": valor})
    if recorta and (cob.get("validas") or 0) + (cob.get("invalidas") or 0) != limite:
        malos += ["validas", "invalidas"]
        ejemplos.append({"campo": "validas+invalidas", "informado": (cob.get("validas") or 0) + (cob.get("invalidas") or 0),
                         "esperado": limite})
    return {"resultado": {"ok": not malos, "campos": sorted(set(malos)), "contraejemplos": ejemplos[:4]},
            "cobertura": {"filas_analizadas": total},
            "limitaciones": ["reparto válidas/inválidas no verificable: el instrumento recorta"] if recorta else []}
'''


def limite_filas(codigo: str) -> int | None:
    """El límite efectivo de filas del instrumento: el menor max_filas de sus llamadas a consultar (10000 si una
    llamada no lo indica). None si un max_filas no es un número literal (no se puede deducir sin ejecutar)."""
    limites = []
    for n in ast.walk(ast.parse(codigo)):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "consultar":
            kw = next((k for k in n.keywords if k.arg == "max_filas"), None)
            arg = kw.value if kw else (n.args[1] if len(n.args) > 1 else None)
            if arg is None:
                limites.append(LIMITE_POR_OMISION)
            elif isinstance(arg, ast.Constant) and isinstance(arg.value, int):
                limites.append(arg.value)
            else:
                return None
    return min(limites) if limites else None


def guardar(almacen) -> list[dict]:
    """Guarda las verificaciones en un almacén y devuelve su especificación para verificaciones.evaluar."""
    vf = almacen.save("verificador_fijo", EL.VERIFICADOR_FIJO, {"proposito": "verificador"}, author="laboratorio")["id"]
    vc = almacen.save("verificador_cobertura", VERIFICADOR_COBERTURA, {"proposito": "verificador"},
                      author="laboratorio")["id"]
    return [{"id": "VF", "instrumento_id": vf}, {"id": "VC_cobertura", "instrumento_id": vc}]


def con_limite(suite: list[dict], codigo: str) -> list[dict]:
    """La misma suite, con el límite efectivo de filas del código que se verifica."""
    lim = limite_filas(codigo)
    return [{**v, "extra": {"limite_filas": lim}} if v["id"] == "VC_cobertura" else v for v in suite]
