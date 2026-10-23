"""Verificador: dos capas SEPARADAS (ajuste de la revisión del diseño).

1. TÉCNICA — ¿la consulta es válida y su resultado es aritméticamente coherente? Antes de ejecutar: nombres reales,
   solo lectura, solo las tablas elegidas, costo estimado (EXPLAIN). Después: ejecución sin error ni recorte y
   contrapruebas derivadas MECÁNICAMENTE del plan MEDIR. Las invariantes son matemáticamente válidas para cualquier
   base (no suponen nada sobre los valores; por ejemplo, una SUMA puede superar la cantidad de filas):
   - multiplicación por JOIN: filas unidas = filas distintas de la tabla raíz (si cada fila raíz se une con a lo
     sumo una fila de cada tabla, son iguales; si no, los JOIN duplicaron filas). Solo importa para agregados
     sensibles a duplicados (conteo, suma, promedio);
   - partición: la suma de los conteos (o sumas) de todos los grupos = el total con el mismo filtro (los grupos
     de GROUP BY particionan las filas filtradas);
   - cotas: si las k filas de un grupo valen entre mín y máx, su suma está entre k·mín y k·máx, y su promedio
     entre mín y máx;
   - ranking: el primero informado es realmente el extremo entre TODOS los grupos;
   - contraprueba: el agregado del grupo ganador recalculado con otra consulta (WHERE grupo = valor, sin GROUP BY,
     ORDER ni LIMIT) es igual al informado.
   Una consulta SQL libre no permite derivar estas contrapruebas: su verificación técnica queda incompleta.

2. SEMÁNTICA — ¿la respuesta corresponde a la PREGUNTA? Un SQL correcto puede responder otra cosa. Se comprueba lo
   que se puede comprobar sin conocer el dominio:
   S1 período pedido = intervalo efectivo del filtro (incluido el error clásico de «entre A y B» en DATETIME);
   S2 intención (mayor/menor, conteo, promedio) = orden y agregado del plan;
   S3 la entidad preguntada («qué producto») corresponde a la tabla por la que se agrupa;
   S4 cada término de contenido de la pregunta tiene RESPALDO en el plan: nombres del esquema, la ficha de una
      tabla del plan (interpretación del modelo: respaldo débil, se informa) o un término del glosario vinculado
      en una respuesta verificada. Un vínculo solo DECLARADO por el modelo no es respaldo;
   S5 ninguna otra tabla describe lo mismo con igual o mayor respaldo (alternativa no descartada);
   S6 la respuesta redactada cita el valor y el grupo del resultado, y ningún número ajeno;
   S7 la columna por la que se agrupa identifica filas distintas (si no, puede juntar entidades distintas);
   S8 no hay empate en el primer puesto.

Cada chequeo: ok | falla | no_verificable. Nivel de la respuesta:
- rechazada (técnica o semántica): hay una falla → se reintenta con el motivo o la misión se abstiene;
- incierta: ninguna falla, pero algo no se pudo verificar → se informa con la lista exacta de lo no verificado;
- verificada: todo ok. Aun así, lo respaldado por fichas es interpretación del modelo: el juez (en el laboratorio)
  mide si de verdad era correcta; una «verificada» incorrecta es una CONFIRMACIÓN FALSA.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from ..explorer import knowledge as K
from ..explorer import sqlguard
from ..explorer import validation as V
from . import medir as MD
from . import texto as TX

INMUNES_A_DUPLICADOS = ("distintos", "maximo", "minimo")


def ch(cid: str, capa: str, estado: str, detalle: str, respaldo: str | None = None) -> dict:
    return {"id": cid, "capa": capa, "estado": estado, "detalle": detalle, "respaldo": respaldo}


def num(v):
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None


def _igual(a, b) -> bool:
    a, b = num(a), num(b)
    if a is None or b is None:
        return False
    return abs(a - b) <= Decimal("0.000001") * max(Decimal(1), abs(a), abs(b))


# Técnica ----------------------------------------------------------------------------------------------------------

def previa_sql(sql: str, schema: dict, base: str, elegidas: list[str]) -> list[dict]:
    out = []
    try:
        sqlguard.check(sql)
        out.append(ch("solo_lectura", "tecnica", "ok", "una sentencia de lectura"))
    except sqlguard.SqlRejected as e:
        return [ch("solo_lectura", "tecnica", "falla", str(e))]
    pre = V.precheck_sql(sql, schema["tablas"], base)
    out.append(ch("nombres", "tecnica", "falla" if pre else "ok", pre or "tablas existentes en la base conectada"))
    usadas = K.sql_shape(sql, schema["tablas"])["tablas"]
    ajenas = [t for t in usadas if t not in elegidas]
    out.append(ch("tablas_elegidas", "tecnica", "falla" if ajenas else "ok",
                  f"usa tablas no elegidas: {', '.join(ajenas)}" if ajenas else "solo tablas elegidas"))
    return out


def costo(broker, sql: str, tope: int, mid: str) -> dict:
    o = broker.run("EXPLAIN " + sql, mission_id=mid, origin="sistema", tool="explicar",
                   question="¿Cuánto costaría la consulta?", rationale="verificación técnica previa")
    if o.status != "ok":
        return ch("costo", "tecnica", "falla", f"EXPLAIN falló: {o.error[:200]}")
    try:
        i = o.columns.index("rows")
        producto = 1
        for r in o.rows:
            producto *= max(1, int(r[i] or 1))
    except (ValueError, TypeError):
        return ch("costo", "tecnica", "no_verificable", "EXPLAIN sin estimación de filas")
    return ch("costo", "tecnica", "ok" if producto <= tope else "falla",
              f"estimación del optimizador: {producto} combinaciones de filas (tope {tope})")


def posterior(o) -> list[dict]:
    if o.status != "ok":
        return [ch("ejecucion", "tecnica", "falla", f"{o.status}: {o.error[:300]}")]
    out = [ch("ejecucion", "tecnica", "ok", f"{len(o.rows)} filas")]
    out.append(ch("recorte", "tecnica", "falla" if o.truncated else "ok",
                  f"resultado recortado ({o.truncated_reason})" if o.truncated else "resultado completo"))
    out.append(ch("filas", "tecnica", "ok" if o.rows else "falla",
                  "con filas" if o.rows else "el resultado está vacío: no responde la pregunta"))
    return out


def invariantes(run, c: dict, o) -> list[dict]:
    """run(sql, herramienta) -> QueryOutcome. c: plan compilado. o: resultado principal (ya ejecutado sin errores)."""
    out = []
    func = c["plan"]["medida"]["funcion"]
    # Multiplicación por JOIN.
    if func in INMUNES_A_DUPLICADOS:
        out.append(ch("multiplicacion", "tecnica", "ok", f"«{func}» no cambia con filas duplicadas"))
    elif not c["joins"]:
        out.append(ch("multiplicacion", "tecnica", "ok", "sin JOIN"))
    else:
        q = MD.sql_filas(c)
        if q is None:
            out.append(ch("multiplicacion", "tecnica", "no_verificable",
                          f"{c['raiz']} no tiene clave primaria de una columna: no se puede contar filas distintas"))
        else:
            r = run(q, "multiplicacion")
            filas, distintas = (r.rows[0] if r.status == "ok" else (None, None))
            if filas is None:
                out.append(ch("multiplicacion", "tecnica", "no_verificable", f"no se pudo medir: {r.error[:120]}"))
            elif int(filas) == int(distintas):
                out.append(ch("multiplicacion", "tecnica", "ok", f"{filas} filas unidas = {distintas} filas de "
                                                                 f"{c['raiz']}"))
            else:
                out.append(ch("multiplicacion", "tecnica", "falla",
                              f"los JOIN multiplicaron filas: {filas} filas unidas para {distintas} filas distintas de "
                              f"{c['raiz']}; el «{func}» las cuenta varias veces. Medí desde la tabla más detallada o "
                              "usá «distintos»"))
    if not c["gby"]:
        return out
    if o.truncated:
        return out
    grupos = run(MD.sql_grupos_completo(c), "grupos")
    total = run(MD.sql_total(c), "total")
    if grupos.status != "ok" or total.status != "ok" or grupos.truncated:
        out.append(ch("contrapruebas", "tecnica", "no_verificable", "no se pudieron obtener todos los grupos "
                                                                    "(error o recorte)"))
        return out
    ng = len(c["gby"])
    filas_g = [(tuple(r[:ng]), r[ng], int(r[ng + 1])) for r in grupos.rows]
    tot = dict(zip(total.columns, total.rows[0]))
    # Partición.
    if func in ("conteo", "suma"):
        suma_grupos = sum((num(v) or Decimal(0)) for _, v, _ in filas_g)
        ok = _igual(suma_grupos, tot["valor"] or 0)
        out.append(ch("particion", "tecnica", "ok" if ok else "falla",
                      f"suma de los {len(filas_g)} grupos = {suma_grupos}; total con el mismo filtro = {tot['valor']}"))
    # Cotas.
    if func in ("suma", "promedio") and tot.get("minimo") is not None:
        mn, mx = num(tot["minimo"]), num(tot["maximo"])
        malos = []
        for g, v, k in filas_g:
            v = num(v)
            lo, hi = (k * mn, k * mx) if func == "suma" else (mn, mx)
            if v is None or v < lo - abs(lo) * Decimal("1e-9") or v > hi + abs(hi) * Decimal("1e-9"):
                malos.append(g)
        out.append(ch("cotas", "tecnica", "falla" if malos else "ok",
                      f"grupos fuera de [k·mín, k·máx]: {malos[:3]}" if malos else
                      f"cada grupo dentro de sus cotas (mín {mn}, máx {mx} por fila)"))
    # Ranking y empate.
    if c["plan"]["orden"] and o.rows:
        top = num(o.rows[0][ng])
        valores = sorted((num(v) for _, v, _ in filas_g if num(v) is not None), reverse=c["plan"]["orden"] == "mayor")
        extremo = valores[0] if valores else None
        ok = extremo is not None and _igual(top, extremo)
        out.append(ch("ranking", "tecnica", "ok" if ok else "falla",
                      f"el primero informado vale {top}; el extremo entre todos los grupos es {extremo}"))
        empatados = sum(1 for v in valores if _igual(v, extremo))
        out.append(ch("empate", "semantica", "no_verificable" if empatados > 1 else "ok",
                      f"{empatados} grupos empatan en el primer puesto: la pregunta tiene varias respuestas"
                      if empatados > 1 else "sin empate en el primer puesto"))
        # Contraprueba del ganador.
        g = run(MD.sql_ganador(c, list(o.rows[0][:ng])), "contraprueba")
        okg = g.status == "ok" and g.rows and _igual(g.rows[0][0], o.rows[0][ng])
        out.append(ch("contraprueba", "tecnica", "ok" if okg else "falla",
                      f"recalculado solo para el ganador: {g.rows[0][0] if g.status == 'ok' and g.rows else g.error}; "
                      f"informado: {o.rows[0][ng]}"))
    return out


# Semántica --------------------------------------------------------------------------------------------------------

def _raices_objeto(tabla: str, columna: str | None = None) -> set[str]:
    return TX.raices(tabla) | (TX.raices(columna) if columna else set())


def _raices_ficha(f: dict | None) -> set[str]:
    if not f:
        return set()
    return TX.raices(f["fila"])


def semantica(pregunta: str, c: dict, schema: dict, fichas: dict, glosario: list[dict], perfiles: dict) -> list[dict]:
    """c: plan compilado MEDIR. fichas: {tabla: ficha vigente}. glosario: entradas de la conexión."""
    out = []
    # S1 período.
    q = TX.periodo(pregunta)
    iv = c["intervalos"]
    if q and q.get("varios"):
        out.append(ch("S1_periodo", "semantica", "no_verificable", "la pregunta menciona varios períodos"))
    elif not q and not iv:
        out.append(ch("S1_periodo", "semantica", "ok", "la pregunta no pide un período y la consulta no filtra fechas"))
    elif q and not iv:
        out.append(ch("S1_periodo", "semantica", "falla", f"la pregunta pide {q['texto']} y la consulta no filtra por "
                                                          "una columna de fecha"))
    elif not q and iv:
        out.append(ch("S1_periodo", "semantica", "falla", "la consulta filtra fechas que la pregunta no pide"))
    elif len(iv) > 1:
        out.append(ch("S1_periodo", "semantica", "falla", "la consulta filtra por más de una fecha"))
    else:
        i = iv[0]
        t, col = i["objeto"].split(".")
        rol = ((fichas.get(t) or {}).get("roles", {}).get(col) or {}).get("rol")
        if i["inicio"] == q["desde"] and i["fin"] == q["hasta"] and not i["fin_incluido"]:
            estado, det = "ok", f"filtro [{q['desde']:%Y-%m-%d}, {q['hasta']:%Y-%m-%d}) = {q['texto']}"
        else:
            fin = f"{i['fin']:%Y-%m-%d %H:%M:%S}" + (" inclusive" if i["fin_incluido"] else " exclusive")
            extra = (" (termina a las 00:00:00 del último día: deja afuera casi todo ese día)"
                     if i["fin_incluido"] and i["fin"].time().isoformat() == "00:00:00" else "")
            estado, det = "falla", (f"la pregunta pide [{q['desde']:%Y-%m-%d}, {q['hasta']:%Y-%m-%d}) y el filtro "
                                    f"efectivo es [{i['inicio']:%Y-%m-%d %H:%M:%S}, {fin}]{extra}")
        if estado == "ok" and rol not in (None, "tiempo"):
            estado, det = "no_verificable", det + f"; pero la ficha dice que {i['objeto']} es «{rol}», no «tiempo»"
        out.append(ch("S1_periodo", "semantica", estado, det))
    # S2 intención.
    it = TX.intencion(pregunta)
    func, orden = c["plan"]["medida"]["funcion"], c["plan"]["orden"]
    problemas, reconocido = [], False
    if it["orden"]:
        reconocido = True
        if orden != it["orden"]:
            problemas.append(f"la pregunta pide el {it['orden']} y la consulta ordena «{orden or 'sin orden'}»")
        if TX.entidad(pregunta) and not c["gby"]:
            problemas.append("la pregunta pide cuál entidad y la consulta no agrupa")
    if it["agregado"] == "conteo":
        reconocido = True
        if func not in ("conteo", "distintos", "suma"):
            problemas.append(f"la pregunta pide cuántos y la medida es «{func}»")
    if it["agregado"] == "promedio":
        reconocido = True
        if func != "promedio":
            problemas.append(f"la pregunta pide un promedio y la medida es «{func}»")
    out.append(ch("S2_intencion", "semantica", "falla" if problemas else ("ok" if reconocido else "no_verificable"),
                  "; ".join(problemas) or ("orden y agregado coherentes con la pregunta" if reconocido else
                                           "no se reconoce la intención de la pregunta")))
    # S3 entidad.
    ent = TX.entidad(pregunta)
    if not c["gby"]:
        out.append(ch("S3_entidad", "semantica", "ok" if not ent else "no_verificable",
                      "sin agrupación" if not ent else f"la pregunta pregunta por «{ent}» y la consulta no agrupa"))
    elif not ent:
        out.append(ch("S3_entidad", "semantica", "no_verificable", "no se reconoce qué entidad pregunta la pregunta"))
    else:
        t, col = c["grupos"][0]
        r = TX.raiz(ent)
        if r in _raices_objeto(t, col):
            out.append(ch("S3_entidad", "semantica", "ok", f"«{ent}» ↔ {t}.{col}", "esquema"))
        elif r in _raices_ficha(fichas.get(t)):
            out.append(ch("S3_entidad", "semantica", "ok", f"«{ent}» ↔ ficha de {t}",
                          f"ficha F{fichas[t]['id']} (interpretación del modelo)"))
        else:
            out.append(ch("S3_entidad", "semantica", "no_verificable", f"no se puede vincular «{ent}» con {t}.{col}"))
    # S4 términos.
    plan_tablas = set(c["tablas"])
    objetos = {f"{t}.{col}" for t, col in c["grupos"]} | {f"{t}.{x}" for t, x in c["plan"]["medida"]["columnas"]} | {
        f"{f['objeto'][0]}.{f['objeto'][1]}" for f in c["plan"]["filtros"]}
    esquema_r = set()
    for o_ in objetos:
        t, col = o_.split(".")
        esquema_r |= _raices_objeto(t, col)
    for t in plan_tablas:
        esquema_r |= TX.raices(t)
    ficha_r = {t: _raices_ficha(fichas.get(t)) for t in plan_tablas}
    glos = {g["raiz"]: g for g in glosario if g["origen"] == "verificada" and
            (g["objeto"] in objetos or g["objeto"].split(".")[0] in plan_tablas)}
    sin_respaldo, respaldos = [], []
    for r in TX.terminos(pregunta):
        if r in esquema_r:
            respaldos.append(f"«{r}…»: esquema")
        elif r in glos:
            respaldos.append(f"«{r}…»: glosario G{glos[r]['id']} (verificado en {', '.join(glos[r]['misiones'])})")
        elif any(r in fr for fr in ficha_r.values()):
            t = next(t for t, fr in ficha_r.items() if r in fr)
            respaldos.append(f"«{r}…»: ficha F{fichas[t]['id']} de {t} (interpretación del modelo)")
        else:
            sin_respaldo.append(r)
    out.append(ch("S4_terminos", "semantica", "no_verificable" if sin_respaldo else "ok",
                  (f"sin respaldo en el plan: {', '.join(f'«{r}…»' for r in sin_respaldo)}" if sin_respaldo else
                   "todos los términos tienen respaldo"), "; ".join(respaldos) or None))
    # S5 alternativas.
    terms = set(TX.terminos(pregunta))
    medida_t = set(c["medida_tablas"]) or {c["raiz"]}

    def soporte(t):
        return len(terms & (TX.raices(t) | _raices_ficha(fichas.get(t)) |
                            {r for col in schema["tablas"][t]["columnas"] for r in TX.raices(col["nombre"])}))
    base = max(soporte(t) for t in medida_t)
    alternativas = [t for t, f in fichas.items() if t not in plan_tablas and t in schema["tablas"]
                    and any(r["rol"] == "medida" for r in f["roles"].values()) and base > 0 and soporte(t) >= base]
    out.append(ch("S5_alternativas", "semantica", "no_verificable" if alternativas else "ok",
                  f"otra(s) tabla(s) describen lo mismo con igual respaldo: {', '.join(alternativas)}; la elección "
                  "depende de una interpretación que el coordinador no puede comprobar" if alternativas else
                  "ninguna otra tabla con igual respaldo"))
    # S7 identidad del grupo.
    if c["gby"]:
        t, col = c["grupos"][0]
        p = perfiles.get(t, {})
        pc = p.get("columnas", {}).get(col, {})
        es_pk = [x["nombre"] for x in schema["tablas"][t]["columnas"] if x["clave"] == "PRI"] == [col]
        unico = es_pk or (pc and pc.get("distintos") == p.get("filas"))
        out.append(ch("S7_identidad", "semantica", "ok" if unico else "no_verificable",
                      f"{t}.{col} distingue cada fila ({pc.get('distintos')} valores en {p.get('filas')} filas)" if unico
                      else f"{t}.{col} tiene {pc.get('distintos')} valores distintos en {p.get('filas')} filas: agrupar "
                           "por ella puede juntar filas distintas"))
    return out


def semantica_sql_libre(pregunta: str) -> list[dict]:
    return [ch("semantica_sql", "semantica", "no_verificable",
               "consulta SQL libre: el coordinador no puede derivar el período, la intención ni la entidad del plan")]


_NUM = re.compile(r"(?<![\w.])-?\d+(?:[.,]\d+)?(?![\w])")


def fidelidad(respuesta: str, columnas: list, filas: list, ng: int) -> dict:
    """S6: la respuesta cita el grupo y el valor del primer resultado, y ningún número ajeno a las filas."""
    texto = TX.fold(respuesta)
    top = filas[0]
    faltan = [str(v) for v in top[:ng] if v is not None and TX.fold(str(v)) not in texto]
    valores = {num(v) for r in filas for v in r if num(v) is not None}
    valor_top = num(top[ng]) if len(top) > ng else None
    citados = [num(x.replace(",", ".")) for x in _NUM.findall(respuesta)]
    ajenos = [str(x) for x in citados if x is not None and not any(_igual(x, v) for v in valores)
              and not (1900 <= x <= 2100)]
    cita_valor = valor_top is None or any(_igual(x, valor_top) for x in citados if x is not None)
    problemas = ([f"no menciona {', '.join(faltan)}"] if faltan else []) + (
        [f"cita números que no están en el resultado: {', '.join(ajenos[:3])}"] if ajenos else []) + (
        [f"no cita el valor {top[ng]}"] if not cita_valor else [])
    return ch("S6_fidelidad", "semantica", "falla" if problemas else "ok",
              "; ".join(problemas) or "cita el grupo y el valor del resultado")


def nivel(chequeos: list[dict]) -> tuple[str, list[str]]:
    fallas = [x for x in chequeos if x["estado"] == "falla"]
    if any(x["capa"] == "tecnica" for x in fallas):
        return "rechazada_tecnica", [f"{x['id']}: {x['detalle']}" for x in fallas]
    if fallas:
        return "rechazada_semantica", [f"{x['id']}: {x['detalle']}" for x in fallas]
    dudas = [f"{x['id']}: {x['detalle']}" for x in chequeos if x["estado"] == "no_verificable"]
    return ("incierta", dudas) if dudas else ("verificada", [])
