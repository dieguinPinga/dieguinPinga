"""Ciclo de investigación v1: hipótesis → reparación → verificación (PILOTO.md, «Ciclo v1»).

Primera versión de un coordinador que investiga en lugar de elegir:

1. OBSERVA: ejecuta el instrumento con su propia suite (S1–S3 y el verificador fijo VF) en DOS juegos de parámetros
   propios, sobre datos con valores de borde; si nada falla, no hay problema que investigar.
2. FORMULA HIPÓTESIS: localiza 3 candidatas y consulta las seis reglas de E0 CONGELADAS (huella ee46552429b3047e)
   como generadoras de hipótesis: si una regla da una única candidata, esa va primero; después el resto, en el
   orden del localizador. Una hipótesis NO es una respuesta.
3. PIDE UNA REPARACIÓN: por cada hipótesis, una llamada corta a Qwen: «si la causa está en esta instrucción, ¿cuál
   es el cambio mínimo?». Qwen puede contestar NO_ES_ESTA. Qwen propone; no aprueba.
4. VERIFICA (solo el coordinador, solo en el sandbox): el código reparado se acepta si
   * compila y pasa la revisión estática,
   * en los DOS juegos de parámetros del coordinador pasa toda la suite,
   * y en cada uno conserva la forma de la salida (una salida vacía pasa las verificaciones sin nada que verificar).
   La primera reparación aceptada termina la investigación («confirmada»); si ninguna, «se abstiene».
5. EL JUEZ, después y por separado: compara cada propuesta con la referencia en los parámetros visibles y en los de
   RESERVA (que el coordinador no conoce). Una propuesta aceptada que el juez rechaza es una CONFIRMACIÓN FALSA: la
   métrica principal.

Aislamiento: `investigar` recibe solo el id y el código del instrumento defectuoso. La referencia, los parámetros
de reserva, la descripción y las líneas del defecto solo entran en `juzgar`, que corre cuando la investigación ya
terminó; la auditoría de fugas revisa después cada prompt.
"""
from __future__ import annotations

import ast
import hashlib
import json
import random
import re
import textwrap
import time
from pathlib import Path

from .. import protocolo as PR
from . import banco as B
from . import defectos as DF
from . import e0 as E0
from . import eleccion as EL
from . import localizador as L
from . import parches as P
from . import traza as T
from . import v2 as V2
from . import verificaciones as V
from ...instrumentos.medicion import ejecutar

VERSION = "ciclo v1.2"                     # v1.2: retroalimentación entre intentos y revisión de hipótesis
HUELLA_REGLAS = "ee46552429b3047e"         # E0 v1, congeladas: el ciclo se niega a correr con otras
MAX_HIPOTESIS = 3                          # llamadas por defecto, por omisión (presupuesto explícito)
TOPE_LLAMADAS = 6                          # ningún presupuesto por defecto puede superar esto
MAX_POR_LINEA = 2                          # intentos sobre una misma instrucción (el inicial y un reintento)
PARAMS_COORDINADOR = (B.PARAMS_VISIBLES, {"ancho_min": 20, "top": 4})
assert all(p != B.PARAMS_RESERVA for p in PARAMS_COORDINADOR), "el segundo juego del coordinador no es el del juez"

GLOSARIO = ("Cómo leer las verificaciones (todas son del coordinador): S1 = la medición terminó y declaró filas; "
            "S2 = leidas = validas + invalidas y filas_analizadas = leidas; S3 = dos ejecuciones dan lo mismo; VF = el "
            "coordinador volvió a medir con SQL propio el total de filas y la suma de cada ventana informada: "
            "«medido» es lo que midió VF; «salida» y «suma» son lo que informó el instrumento.")


# Datos de borde ---------------------------------------------------------------------------------------------------

def agregar_bordes(ent, *, semilla: int = 11) -> dict:
    """Agrega a la base privada lecturas EXACTAMENTE en los límites de validez de la pregunta (0,5 y 900), que la
    base sintética no tiene: una de 0,5 cada 30 minutos y seis de 900. Con ellas, un límite mal puesto
    (`<=` en lugar de `<`) cambia la salida. Comparten los datos el coordinador y el juez."""
    rng = random.Random(semilla)
    info = ent.info
    base = info["base"]
    import datetime as dt
    from .. import sintetica as SI
    filas, lid = [], int(info["ultimo_id"])
    for k in range(info["dias"] * 48):
        m = SI.BASE + dt.timedelta(minutes=30 * k + 7)
        lid += 1
        filas.append((lid, 1 + k % SI.ESTACIONES, m, 0.5, m + dt.timedelta(minutes=1)))
    for i in range(6):
        m = SI.BASE + dt.timedelta(days=i * info["dias"] // 6, hours=12 + i, minutes=7 + rng.randrange(40))
        d = i
        lid += 1
        filas.append((lid, 1 + d % SI.ESTACIONES, m, 900.0, m + dt.timedelta(minutes=1)))
    valores = ",".join(f"({i},{e},'{m:%Y-%m-%d %H:%M:%S}',{v:.3f},'{r:%Y-%m-%d %H:%M:%S}')" for i, e, m, v, r in filas)
    ent.servidor.sql(f"INSERT INTO lecturas (id, estacion_id, momento, valor, recibido) VALUES {valores};\n"
                     "ANALYZE TABLE lecturas;", database=base)
    info["ultimo_id"] = lid
    return {"filas": len(filas), "valor_0_5": sum(1 for f in filas if f[3] == 0.5),
            "valor_900": sum(1 for f in filas if f[3] == 900.0)}


# Observación (solo el coordinador) --------------------------------------------------------------------------------

class Coordinador:
    """Lo que el coordinador puede usar: el entorno, SU almacén, SUS verificadores y SUS parámetros."""

    def __init__(self, ent):
        self.ent = ent
        self.alm = ent.almacen(f"ciclo_coordinador_{int(time.time() * 1000)}")
        self.vf = self.alm.save("verificador_fijo", EL.VERIFICADOR_FIJO, {"proposito": "verificador"},
                                author="laboratorio")["id"]
        self.vc = self.alm.save("verificador_contraste", E0.VERIFICADOR_CONTRASTE, {"proposito": "verificador"},
                                author="laboratorio")["id"]
        self.n = 0

    def guardar(self, codigo: str) -> int | None:
        self.n += 1
        try:
            return self.alm.save(f"ciclo_{self.n}", codigo, B.MANIFIESTO, author="laboratorio")["id"]
        except Exception:            # noqa: BLE001 - la revisión estática rechazó el código
            return None

    def suite(self, iid: int, params: dict) -> dict:
        return V.evaluar(self.ent, self.alm, iid, params, [{"id": "VF", "instrumento_id": self.vf}])


def _texto_fallas(ev) -> str:
    # En el ciclo todas las verificaciones son del coordinador (v1 rotulaba VF como «modelo»).
    return "\n".join(f"- {f['id']}: {f['detalle']}" + (f" (campos: {', '.join(f['campos'])})" if f["campos"] else "")
                     for f in V.fallas(ev)) or "(ninguna)"


def _listas_llenas(x) -> bool:
    if isinstance(x, dict):
        return all(_listas_llenas(v) for v in x.values())
    if isinstance(x, list):
        return bool(x) and all(_listas_llenas(v) for v in x)
    return True


def observar(co: Coordinador, problema: dict) -> dict:
    """problema = {"id", "codigo"}: NADA más."""
    codigo = problema["codigo"]
    iid = co.guardar(codigo)
    if iid is None:
        return {"id": problema["id"], "detectado": True, "estado": "rechazado_por_la_revision", "juegos": []}
    juegos = []
    for params in PARAMS_COORDINADOR:
        ev = co.suite(iid, params)
        juegos.append({"params": params, "salida": ev["salida"], "forma": E0.forma(ev["salida"])
                       if ev["salida"] is not None else None, "fallas": V.fallas(ev), "texto": _texto_fallas(ev),
                       "invalidos": ev["invalidos"]})
    detectado = any(j["fallas"] for j in juegos)
    obs = {"id": problema["id"], "huella": hashlib.sha256(codigo.encode()).hexdigest()[:12], "detectado": detectado,
           "juegos": juegos}
    if not detectado:
        return {**obs, "estado": "no_detectado"}
    base = next(j for j in juegos if j["fallas"])
    cands = L.localizar(codigo, base["fallas"], k=3)
    unidades = [c["unidad"] for c in cands]
    run = ejecutar(executor=co.ent.executor, broker=co.alm.broker, almacen=co.alm, cfg=co.ent.cfg, instrument_id=iid,
                   params=base["params"], schema=co.ent.schema, traza=T.pedido(unidades, codigo))
    caso = {"codigo": codigo, "candidatas": [u.lineno for u in unidades], "salida": juegos[0]["salida"],
            "fallas": [{k: f.get(k) for k in ("id", "autor", "campos", "detalle", "lugar")} for f in juegos[0]["fallas"]],
            "traza": (run.get("verdict") or {}).get("traza")}
    contraste = None
    if caso["salida"] is not None:
        r = V._run(co.ent, co.alm, co.vc, {"parametros": B.PARAMS_VISIBLES, "salida": caso["salida"]})
        contraste = (r.get("output") or {}).get("resultado") if r["status"] == "ok" else None
    iv = E0.Intervenciones(co.ent, co.alm, co.vf)
    iv.forma = juegos[0]["forma"]
    r4, camino = E0.r4_contraste(caso, contraste)
    r6, _ = E0.r6_escala(caso, iv)
    cumplen = {"R1_lugar_del_error": E0.r1_lugar(caso), "R2_ultimo_escritor": E0.r2_escritor(caso),
               "R3_acumulacion_sin_efecto": E0.r3_sin_efecto(caso), "R4_contraste_clave_valor": r4,
               "R5_omision": E0.r5_omision(caso, iv), "R6_escala": r6}
    regla = next(({"lectura": k, "eleccion": cumplen[k][0]} for k in E0.ORDEN if len(cumplen[k]) == 1), None)
    hipotesis = ([regla["eleccion"]] if regla else []) + [ln for ln in caso["candidatas"]
                                                          if not regla or ln != regla["eleccion"]]
    return {**obs, "estado": "investigable", "candidatas": caso["candidatas"],
            "motivos": [c["motivo"] for c in cands], "reglas_v1": {"cumplen": cumplen, "respuesta": regla,
                                                                   "camino_contraste": camino},
            "hipotesis": hipotesis[:MAX_HIPOTESIS], "traza_completa": (caso["traza"] or {}).get("completa")}


# Pedido de reparación ---------------------------------------------------------------------------------------------

def partes(problema: dict, obs: dict, linea: int, historia=(), pendientes=(), compacto: bool = False
           ) -> list[tuple[str, str]]:
    """historia: intentos anteriores de ESTA investigación (retroalimentación); pendientes: hipótesis sin probar."""
    codigo = problema["codigo"]
    u = P.unidad_en(codigo, linea)
    autorizadas = ", ".join(str(x) for x in obs["candidatas"])
    evid = []
    for j in obs["juegos"]:
        if j["fallas"]:
            salida = json.dumps(j["salida"], ensure_ascii=False)[:900] if j["salida"] is not None else "(no terminó)"
            evid.append(f"Con los parámetros {j['params']}:\n  Salida: {salida}\n  Verificaciones que fallan:\n"
                        + "\n".join("  " + x for x in j["texto"].splitlines()))
    rango = f"{u.lineno}" if u.end_lineno == u.lineno else f"{u.lineno}–{u.end_lineno}"
    previo = []
    if historia:
        previo = historial(codigo, historia, compacto)
        previo.append(("historial", "Hipótesis pendientes (sin probar): "
                                    + (", ".join(f"línea {x}" for x in pendientes) or "ninguna") + ".\n"))
    return [("coordinador", f"Un instrumento de medición escrito en Python responde: {B.PREGUNTA}\n"),
            ("evidencia", "\n\n".join(evid) + "\n"),
            ("coordinador", GLOSARIO + "\n"),
            *previo,
            ("coordinador", "Código actual. Los números de línea y la marca «>>» están SOLO para ubicarte; no son "
                            "parte del código:\n" + P.mostrar(codigo, u, contexto=200, completa=True) + "\n"),
            ("coordinador", f"Instrucción de la hipótesis (línea {rango}), tal como está en el código:\n"
                            f"```python\n{instruccion(codigo, u)}\n```\n"),
            ("coordinador", f"Hipótesis del coordinador, TODAVÍA NO COMPROBADA: la causa de las fallas está en esa "
                            f"instrucción (línea {rango}).\n"
                            "Si es así, proponé el cambio MÍNIMO: escribí el código que REEMPLAZA a esa instrucción "
                            "completa (puede tener más de una línea, por ejemplo para agregar junto a ella una "
                            "instrucción que falta). Escribí solo código Python: SIN números de línea, SIN «>>» y SIN "
                            "«|».\n"
                            "Si la corrección es borrarla, respondé ELIMINAR.\n"
                            "Si creés que la causa NO está en esa instrucción, respondé NO_ES_ESTA y, si creés que está "
                            f"en otra, agregá PROPONGO_LINEA: <número>, usando SOLO una de estas líneas candidatas: "
                            f"{autorizadas}. El coordinador decide si la prueba.\n"
                            "Formato (una de las dos):\n```python\n<reemplazo>\n```\nMOTIVO: <una frase>\n"
                            "o bien\nNO_ES_ESTA\nPROPONGO_LINEA: <número>\nMOTIVO: <una frase>")]


CATEGORIAS = {"sintaxis": "el fragmento no compila", "ensamblado": "no se pudo ensamblar en el código",
              "revision": "la revisión estática del sandbox lo rechazó", "sin_cambio": "no cambia el código",
              "comportamiento": "se ejecutó y no pasó las verificaciones", "ilegible": "la respuesta no se entendió"}


def historial(codigo: str, historia, compacto: bool) -> list[tuple[str, str]]:
    """Retroalimentación: qué se probó, qué se propuso, qué falló y por qué. Lo que Qwen propuso va en partes
    «propuesta_previa» (texto del propio modelo, no del coordinador: la auditoría de fugas no lo cuenta)."""
    out = [("historial", "Intentos anteriores de esta investigación (ninguno se aceptó):\n")]
    for n, it in enumerate(historia, 1):
        u = P.unidad_en(codigo, it["hipotesis"])
        out.append(("historial", f"Intento {n}: hipótesis línea {it['hipotesis']}: `{instruccion(codigo, u)}`\n"))
        prop = it["propuesta"]
        if prop["tipo"] in ("reemplazar", "eliminar"):
            if prop["tipo"] == "eliminar":
                out.append(("propuesta_previa", "  Propusiste: ELIMINAR esa instrucción.\n"))
            elif not compacto:
                out.append(("propuesta_previa", "  Propusiste:\n```python\n"
                            + (prop.get("fragmento_crudo") or "").strip()[:400] + "\n```\n"))
            else:
                out.append(("propuesta_previa", "  Propusiste un reemplazo de esa instrucción.\n"))
        elif prop["tipo"] == "no_es_esta":
            red = it.get("redireccion")
            txt = "  Respondiste NO_ES_ESTA"
            if red and red.get("pedida") is not None:
                txt += (f" y propusiste la línea {red['pedida']}: "
                        + ("el coordinador la tomó como próxima hipótesis." if red["valida"] else
                           f"el coordinador NO la tomó ({red['motivo']})."))
            out.append(("historial", txt + "\n"))
        if it.get("rechazo"):
            det = it.get("rechazo_detalle") or ""
            out.append(("historial", f"  Resultado: rechazada: {CATEGORIAS[it['rechazo']]}"
                                     + (f": {det[:300 if not compacto else 120]}" if det else "") + ".\n"))
    return out


def instruccion(codigo: str, u: P.Unidad) -> str:
    """La instrucción completa, sin numeración ni marcas y sin la sangría del bloque."""
    return textwrap.dedent("\n".join(codigo.splitlines()[u.lineno - 1:u.end_lineno]))


RE_BLOQUE = re.compile(r"```[a-zA-Z0-9]*\n(.*?)```", re.S)


def interpretar(texto: str | None) -> dict:
    t = texto or ""
    m = RE_BLOQUE.search(t)
    if m and m.group(1).strip():
        return {"tipo": "reemplazar", "fragmento": m.group(1)}
    cabeza = t.strip().splitlines()[0].strip().upper() if t.strip() else ""
    if cabeza.startswith("NO_ES_ESTA") or (not m and "NO_ES_ESTA" in t):
        return {"tipo": "no_es_esta"}
    if cabeza.startswith("ELIMINAR"):
        return {"tipo": "eliminar"}
    return {"tipo": "ilegible"}


# Prefijo de la vista numerada que muestra el prompt («>>  19 | código» o «    19 | código»).
RE_NUMERADA = re.compile(r"^(?:>>)?[ \t]*(\d{1,4})[ \t]?\|[ \t]?(.*)$")


def _compila(fragmento: str) -> str | None:
    """None si el fragmento compila solo; si no, el error."""
    try:
        ast.parse(P.limpiar_fragmento(fragmento))
        return None
    except SyntaxError as e:
        return f"{e.msg} (línea {e.lineno})"


def limpiar_numeracion(fragmento: str, desde: int) -> tuple[str | None, str]:
    """Quita el prefijo «N |» SOLO si se cumplen las cuatro condiciones (si no, devuelve None y el motivo):
    1. el fragmento NO compila tal como llegó;
    2. TODAS sus líneas no vacías tienen el prefijo de la vista numerada;
    3. los números son consecutivos y empiezan en la línea de la instrucción marcada;
    4. sin el prefijo, compila.
    No adivina: cualquier otra forma queda como fragmento inválido."""
    if _compila(fragmento) is None:
        return None, "compila tal como llegó: no se limpia"
    lineas = fragmento.splitlines()
    llenas = [ln for ln in lineas if ln.strip()]
    marcas = [RE_NUMERADA.match(ln) for ln in llenas]
    if not llenas or not all(marcas):
        return None, "no todas las líneas tienen el prefijo «N |»: no se limpia"
    nums = [int(m.group(1)) for m in marcas]
    if nums != list(range(desde, desde + len(nums))):
        return None, f"la numeración {nums} no es la de la instrucción marcada (desde {desde}): no se limpia"
    limpio = "\n".join(RE_NUMERADA.match(ln).group(2) if ln.strip() else "" for ln in lineas) + "\n"
    error = _compila(limpio)
    if error:
        return None, f"sin la numeración tampoco compila: {error}"
    return limpio, f"se quitó la numeración {nums[0]}–{nums[-1]}"


def preparar(texto: str | None, codigo: str, u: P.Unidad) -> dict:
    """Etapas separadas: propuesta → limpieza → fragmento válido o no → ensamblado (con motivo). La verificación la
    hace después `verificar`, solo si el ensamblado salió."""
    it = interpretar(texto)
    reg = {"propuesta": {"tipo": it["tipo"], "fragmento_crudo": it.get("fragmento")},
           "limpieza": {"aplicada": False, "detalle": "no corresponde"},
           "fragmento": {"valido": None, "error": None},
           "ensamblado": {"realizado": False, "ok": False, "atribucion": None, "detalle": "no corresponde"}}
    if it["tipo"] not in ("reemplazar", "eliminar"):
        motivo = {"no_es_esta": "Qwen respondió NO_ES_ESTA", "ilegible": "no hay un bloque de código ni una "
                  "respuesta reconocible"}[it["tipo"]]
        reg["ensamblado"]["detalle"] = motivo
        return reg
    fragmento = it.get("fragmento", "")
    if it["tipo"] == "reemplazar":
        error = _compila(fragmento)
        if error:
            limpio, detalle = limpiar_numeracion(fragmento, u.lineno)
            reg["limpieza"] = {"aplicada": limpio is not None, "detalle": detalle}
            if limpio is not None:
                fragmento, error = limpio, None
        reg["fragmento"] = {"valido": error is None, "error": error}
        if error:
            reg["ensamblado"]["detalle"] = f"no se ensambla: el fragmento no compila ({error})"
            return reg
    ens = P.aplicar(codigo, u, it["tipo"], fragmento, completa=True)
    reg["ensamblado"] = {"realizado": True, "ok": ens.codigo is not None, "atribucion": ens.atribucion,
                         "detalle": ens.detalle}
    if ens.codigo is not None:
        reg["codigo_propuesto"] = ens.codigo
    return reg


def semilla(defecto_id: str, i: int) -> int:
    return int(hashlib.sha256(f"ciclo|{defecto_id}|{i}".encode()).hexdigest(), 16) % 2 ** 31


# Verificación (solo el coordinador, solo en el sandbox) -----------------------------------------------------------

def verificar(co: Coordinador, obs: dict, codigo: str, nuevo: str) -> dict:
    if nuevo == codigo:
        return {"aceptada": False, "motivo": "no cambia el código", "categoria": "sin_cambio", "juegos": []}
    iid = co.guardar(nuevo)
    if iid is None:
        return {"aceptada": False, "motivo": "la revisión estática lo rechazó", "categoria": "revision", "juegos": []}
    res = []
    for j in obs["juegos"]:
        ev = co.suite(iid, j["params"])
        fallas = [f["id"] for f in V.fallas(ev)] + [f"{x['id']}(inválido)" for x in ev["invalidos"]]
        detalle = "; ".join(f"{f['id']}: {f['detalle'][:160]}" for f in V.fallas(ev))
        if ev["run"]["status"] == "rechazado":            # la revisión estática corre al ejecutar
            return {"aceptada": False, "motivo": f"la revisión estática lo rechazó: {ev['run'].get('error')}",
                    "categoria": "revision", "detalle": str(ev["run"].get("error") or "")[:300], "juegos": res}
        if ev["salida"] is None:
            forma_ok = False
        elif j["forma"] is not None:
            forma_ok = E0.forma(ev["salida"]) == j["forma"]
        else:                        # el defecto no terminaba con estos parámetros: al menos, nada vacío
            forma_ok = _listas_llenas((ev["salida"] or {}).get("resultado"))
        res.append({"params": j["params"], "fallas": fallas, "forma_ok": forma_ok})
        if fallas or not forma_ok:
            motivo = (f"con {j['params']} falla {fallas}" if fallas else
                      f"con {j['params']} pasa, pero cambió la forma de la salida")
            det = (f"con los parámetros {j['params']}: " + (detalle if fallas else
                   "pasa las verificaciones, pero la salida cambió de forma (claves o cantidad de elementos)"))
            return {"aceptada": False, "motivo": motivo, "categoria": "comportamiento", "detalle": det[:500],
                    "juegos": res}
    return {"aceptada": True, "motivo": "pasa toda la suite y conserva la forma en los dos juegos", "categoria": None,
            "juegos": res}


# Investigación de un defecto --------------------------------------------------------------------------------------

def etapas(reg: dict) -> str:
    """Una línea legible: propuesta → limpieza → fragmento → ensamblado → verificación."""
    out = [f"propuesta {reg['propuesta']['tipo']}"]
    if reg["limpieza"]["aplicada"]:
        out.append(f"limpieza: {reg['limpieza']['detalle']}")
    if reg["fragmento"]["valido"] is not None:
        out.append("fragmento válido" if reg["fragmento"]["valido"] else
                   f"fragmento inválido ({reg['fragmento']['error']}"
                   + (f"; {reg['limpieza']['detalle']}" if not reg["limpieza"]["aplicada"]
                      and reg["limpieza"]["detalle"] != "no corresponde" else "") + ")")
    e = reg["ensamblado"]
    if e["realizado"]:
        out.append("ensamblado ok" if e["ok"] else f"ensamblado fallido ({e['atribucion']}: {e['detalle']})")
    v = reg.get("verificacion") or {}
    out.append(("ACEPTADA" if v.get("aceptada") else f"rechazada: {v.get('motivo')}") if v.get("realizada")
               else f"verificación no realizada: {v.get('motivo') or e['detalle']}")
    red = reg.get("redireccion")
    if red:
        out.append(f"propuso la línea {red['pedida']}: " + ("tomada" if red["valida"] else f"NO tomada ({red['motivo']})"))
    d = reg.get("decision")
    if d:
        out.append(f"siguiente: {('línea ' + str(d['siguiente'])) if d['siguiente'] else 'ninguna'} ({d['porque']})")
    return " → ".join(out)


class Detener(Exception):
    """El entorno obliga a parar la corrida (se reanuda después)."""


RE_PROPONGO = re.compile(r"PROPONGO_LINEA\s*:\s*(\S+)")


def redireccion(texto: str | None, codigo: str, autorizadas: list[int], actual: int, usados: dict) -> dict | None:
    """Valida un PROPONGO_LINEA. El coordinador NO modifica cualquier línea que Qwen sugiera: solo la toma como
    próxima hipótesis si existe, cae en una instrucción editable, esa instrucción es una de las candidatas
    autorizadas (v1.2: las 3 originales), no es la hipótesis actual y no agotó sus intentos."""
    m = RE_PROPONGO.search(texto or "")
    if not m:
        return None
    pedida = m.group(1).rstrip(".,;")
    if not pedida.isdigit():
        return {"pedida": pedida, "valida": False, "motivo": "no es un número de línea"}
    n = int(pedida)
    if not 1 <= n <= len(codigo.splitlines()):
        return {"pedida": n, "valida": False, "motivo": "esa línea no existe"}
    u = P.unidad_en(codigo, n)
    if u is None:
        return {"pedida": n, "valida": False, "motivo": "no es una instrucción editable"}
    if u.lineno not in autorizadas:
        return {"pedida": n, "valida": False, "motivo": f"no es una de las candidatas autorizadas {autorizadas}"}
    if u.lineno == actual:
        return {"pedida": n, "valida": False, "motivo": "es la misma hipótesis que se acaba de probar"}
    if usados.get(u.lineno, 0) >= MAX_POR_LINEA:
        return {"pedida": n, "valida": False, "motivo": f"esa hipótesis ya tuvo {MAX_POR_LINEA} intentos"}
    return {"pedida": n, "valida": True, "motivo": "aceptada como próxima hipótesis", "linea": u.lineno}


def decidir(reg: dict, cola: list[int], usados: dict, fallas_iniciales: set) -> tuple[int | None, str]:
    """La regla (fija) que usa el resultado de cada intento para elegir el siguiente:
    1. una redirección de Qwen VALIDADA por el coordinador;
    2. reintentar la misma hipótesis si no llegó a probarse (sintaxis, ensamblado, revisión, sin cambio, ilegible),
       o si la reparación se ejecutó y REDUJO las verificaciones que fallan con los parámetros visibles;
    3. la siguiente hipótesis pendiente de la cola (reglas congeladas + localizador);
    4. nada: la investigación termina."""
    red = reg.get("redireccion")
    if red and red["valida"]:
        return red["linea"], "redirección de Qwen validada por el coordinador"
    L = reg["hipotesis"]
    if usados.get(L, 0) < MAX_POR_LINEA:
        if reg.get("rechazo") in ("sintaxis", "ensamblado", "revision", "sin_cambio", "ilegible"):
            return L, f"reintento: la hipótesis no llegó a probarse ({CATEGORIAS[reg['rechazo']]})"
        juegos = (reg.get("verificacion") or {}).get("juegos") or []
        if reg.get("rechazo") == "comportamiento" and juegos:
            ahora = set(juegos[0]["fallas"])
            if ahora < fallas_iniciales:
                return L, f"reintento: la reparación redujo las fallas ({sorted(fallas_iniciales)} → {sorted(ahora)})"
    for x in cola:
        if usados.get(x, 0) == 0:
            return x, "siguiente hipótesis pendiente"
    return None, "no quedan hipótesis por probar"


def _rechazo(reg: dict) -> tuple[str | None, str]:
    if reg["propuesta"]["tipo"] == "no_es_esta":
        return None, ""
    if reg["propuesta"]["tipo"] == "ilegible":
        return "ilegible", ""
    if reg["fragmento"]["valido"] is False:
        return "sintaxis", reg["fragmento"]["error"] or ""
    if reg["ensamblado"]["realizado"] and not reg["ensamblado"]["ok"]:
        return "ensamblado", reg["ensamblado"]["detalle"]
    v = reg.get("verificacion") or {}
    if v.get("aceptada"):
        return None, ""
    return v.get("categoria") or "comportamiento", v.get("detalle") or v.get("motivo") or ""


NO_CABE = object()


def investigar(co: Coordinador, problema: dict, obs: dict, pedir, hechas: dict, *,
               llamadas: int = MAX_HIPOTESIS, log=print) -> dict:
    """Investigación con retroalimentación (v1.2). pedir(clave, partes, semilla) -> texto | None | NO_CABE.
    None: el límite GLOBAL de llamadas cortó (incompleto). NO_CABE: el prompt no entra en el contexto.
    hechas: respuestas ya obtenidas (reanudación): el camino se reconstruye igual, sin pedir de nuevo."""
    codigo = problema["codigo"]
    llamadas = max(1, min(int(llamadas), TOPE_LLAMADAS))
    cola = list(obs["hipotesis"])
    usados: dict = {}
    fallas_iniciales = {f["id"] for f in obs["juegos"][0]["fallas"]}
    intentos, siguiente, porque = [], (cola[0] if cola else None), "primera hipótesis"
    for k in range(llamadas):
        if siguiente is None:
            return {"id": problema["id"], "estado": "se_abstiene", "motivo": porque, "intentos": intentos}
        linea = siguiente
        pendientes = [x for x in cola if usados.get(x, 0) == 0 and x != linea]
        clave = f"{problema['id']}|c{k}|L{linea}"
        ps = partes(problema, obs, linea, intentos, pendientes)
        if clave in hechas:
            resp, ps = hechas[clave]["respuesta"], hechas[clave].get("partes") or ps
        else:
            resp = pedir(clave, ps, semilla(problema["id"], k))
            if resp is NO_CABE:                        # se resume el historial y se intenta una vez más
                ps = partes(problema, obs, linea, intentos, pendientes, compacto=True)
                resp = pedir(clave, ps, semilla(problema["id"], k))
            if resp is NO_CABE:
                return {"id": problema["id"], "estado": "se_abstiene", "intentos": intentos,
                        "motivo": "el pedido no entra en el contexto ni con el historial resumido"}
            if resp is None:
                return {"id": problema["id"], "estado": "incompleto", "intentos": intentos,
                        "pendientes": [linea] + pendientes}
        reg = {"clave": clave, "hipotesis": linea, "por_que_esta_hipotesis": porque, "respuesta": resp,
               "partes": ps, **preparar(resp, codigo, P.unidad_en(codigo, linea))}
        reg["interpretacion"] = reg["propuesta"]["tipo"]
        if reg["ensamblado"]["ok"]:
            reg["verificacion"] = {"realizada": True, **verificar(co, obs, codigo, reg["codigo_propuesto"])}
        else:
            reg["verificacion"] = {"realizada": False, "aceptada": None, "motivo": reg["ensamblado"]["detalle"]}
        usados[linea] = usados.get(linea, 0) + 1
        if reg["propuesta"]["tipo"] == "no_es_esta":
            reg["redireccion"] = redireccion(resp, codigo, obs["candidatas"], linea, usados)
        reg["rechazo"], reg["rechazo_detalle"] = _rechazo(reg)
        intentos.append(reg)
        if reg["verificacion"].get("aceptada"):
            log(f"    {clave}: {etapas(reg)}")
            return {"id": problema["id"], "estado": "confirmada", "linea": linea, "intentos": intentos}
        siguiente, porque = decidir(reg, cola, usados, fallas_iniciales)
        reg["decision"] = {"siguiente": siguiente, "porque": porque}
        log(f"    {clave}: {etapas(reg)}")
    return {"id": problema["id"], "estado": "se_abstiene", "intentos": intentos,
            "motivo": f"se agotó el presupuesto del defecto ({llamadas} llamadas)"}


# Juez (después, por separado) -------------------------------------------------------------------------------------

def _referencia_solo(defecto: dict) -> set[str]:
    """Líneas de la referencia que no están en el defecto (si aparecen en un prompt, hubo fuga)."""
    return ({x.strip() for x in B.REFERENCIA.splitlines() if x.strip()}
            - {x.strip() for x in defecto["codigo"].splitlines() if x.strip()})


def auditar(prompt_partes, defecto: dict) -> list[str]:
    fugas = []
    # «propuesta_previa» es texto que escribió el propio modelo en un intento anterior: no es una fuga del juez.
    texto = "\n".join(t for o, t in prompt_partes if o != "propuesta_previa")
    lineas = {x.strip() for x in texto.splitlines()}
    for ref in _referencia_solo(defecto):
        if ref in lineas or (len(ref) >= 12 and ref in texto):
            fugas.append(f"línea de la referencia: {ref[:60]}")
    if defecto.get("descripcion") and defecto["descripcion"] in texto:
        fugas.append("la descripción del defecto")
    for k, v in B.PARAMS_RESERVA.items():
        if f"'{k}': {v}" in texto or f'"{k}": {v}' in texto:
            fugas.append(f"parámetro de reserva {k}={v}")
    return fugas


class Juez:
    def __init__(self, ent):
        self.ent = ent
        self.alm = ent.almacen(f"ciclo_juez_{int(time.time() * 1000)}")
        rid = self.alm.save("referencia", B.REFERENCIA, B.MANIFIESTO, author="banco")["id"]
        self.ref = {k: V._run(ent, self.alm, rid, p).get("output") for k, p in self._juegos()}
        self.n = 0

    @staticmethod
    def _juegos():
        return (("visible", B.PARAMS_VISIBLES), ("reserva", B.PARAMS_RESERVA))

    def correcto(self, codigo: str) -> bool:
        self.n += 1
        try:
            iid = self.alm.save(f"j_{self.n}", codigo, B.MANIFIESTO, author="juez")["id"]
        except Exception:            # noqa: BLE001
            return False
        return all(B.comparar(V._run(self.ent, self.alm, iid, p).get("output"), self.ref[k])["igual"]
                   for k, p in self._juegos())


def juzgar(juez: Juez, defecto: dict, obs: dict, res: dict | None) -> dict:
    obj = DF.objetivo(defecto)
    out = {"id": defecto["id"], "clase": defecto["clase"], "objetivo": sorted(obj),
           "equivalente": juez.correcto(defecto["codigo"]), "estado": (res or obs)["estado"]}
    if obs.get("estado") != "investigable":
        return out
    regla = obs["reglas_v1"]["respuesta"]
    out.update({"causa_entre_candidatas": bool(obj & set(obs["candidatas"])),
                "reglas_v1_punta_a_punta": "se abstiene" if not regla else
                ("acierta" if regla["eleccion"] in obj else "se equivoca"),
                "regla_v1": regla})
    incompleto = bool(res) and res["estado"] == "incompleto"
    intentos = []
    for it in (res or {}).get("intentos", []):
        j = {"clave": it["clave"], "hipotesis_en_objetivo": it["hipotesis"] in obj,
             "interpretacion": it["interpretacion"], "etapas": etapas(it),
             "verificacion_realizada": bool((it.get("verificacion") or {}).get("realizada")),
             "aceptada": (it.get("verificacion") or {}).get("aceptada"), "redireccion": it.get("redireccion"),
             "fugas": auditar(it["partes"], defecto)}
        if "codigo_propuesto" in it and not incompleto:   # una investigación sin terminar no se juzga
            j["correcta_segun_juez"] = juez.correcto(it["codigo_propuesto"])
        intentos.append(j)
    out["intentos"] = intentos
    if res and res.get("motivo"):
        out["motivo"] = res["motivo"]
    if incompleto:
        out.update({"pendientes": res["pendientes"],
                    "resultado": f"incompleto: el límite de llamadas cortó la investigación (hipótesis pendientes "
                                 f"{res['pendientes']}); no se juzga ni cuenta como abstención"})
        return out
    acept = next((j for j in intentos if j["aceptada"]), None)
    out["resultado"] = ("confirmación verdadera" if acept and acept.get("correcta_segun_juez") else
                        "CONFIRMACIÓN FALSA" if acept else
                        "abstención con una reparación correcta rechazada" if any(j.get("correcta_segun_juez")
                                                                                  for j in intentos) else
                        "abstención" if res and res["estado"] == "se_abstiene" else (res or {}).get("estado"))
    return out


# Corrida ----------------------------------------------------------------------------------------------------------

def _jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.exists() else []


def _agregar(path: Path, obj: dict) -> None:
    with path.open("a") as f:
        f.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")


def correr(cfg, ent, *, defectos: list[dict], salida: Path, crear_modelo=None, limite_llamadas: int | None = None,
           llamadas_por_defecto: int = MAX_HIPOTESIS, guardia=None, log=print) -> dict:
    """defectos: los de defectos.cargar (con etiquetas). A la investigación solo pasan id y código.
    crear_modelo None: sin modelo (observación, reglas y juez; sirve como E0 v1 de punta a punta)."""
    if E0.huella_reglas() != HUELLA_REGLAS:
        raise RuntimeError(f"las reglas de E0 cambiaron ({E0.huella_reglas()} ≠ {HUELLA_REGLAS}): el ciclo v1 usa "
                           "las congeladas; otra versión de reglas es otra versión del ciclo")
    salida = Path(salida)
    salida.mkdir(parents=True, exist_ok=True, mode=0o700)
    vpath = salida / "version.json"
    previa = json.loads(vpath.read_text()).get("version") if vpath.exists() else (
        "ciclo v1" if (salida / "llamadas.jsonl").exists() else None)
    if previa not in (None, VERSION):
        raise RuntimeError(f"la carpeta es de «{previa}» y este código es «{VERSION}»: no se reanuda (los resultados "
                           "de otra versión no se mezclan ni se modifican); usar una carpeta nueva")
    vpath.write_text(json.dumps({"version": VERSION}))
    bordes = agregar_bordes(ent)            # la base privada es nueva en cada corrida: también al reanudar
    (salida / "bordes.json").write_text(json.dumps(bordes))
    co = Coordinador(ent)
    hechas = {x["clave"]: x for x in _jsonl(salida / "llamadas.jsonl")}
    terminados = {x["id"]: x for x in _jsonl(salida / "resultados.jsonl")}
    modelo = None
    if crear_modelo is not None:
        modelo = V2.ModeloV2(crear_modelo(), 10 ** 6, 10 ** 9, guardia or V2.Guardia(minimo_mb=0))
        modelo.system = PR.SYSTEM.replace("{resultado_kb}", str(cfg.instrumentos_resultado_kb))
    estado = {"nuevas": 0, "corrida": "completa", "interrupciones": []}

    def pedir(clave, ps, sem):
        if modelo is None:
            return None
        if limite_llamadas is not None and estado["nuevas"] >= limite_llamadas:
            estado["corrida"] = "limite"
            return None
        texto = "".join(t for _, t in ps)
        if not modelo.cabe(texto, True):
            return NO_CABE
        for reintento in (0, 1):
            try:
                resp, call = modelo.con_partes(ps).pedir("reparacion", texto, json_out=False, corto=True, seed=sem)
            except V2.EntornoInterrumpido as ex:
                estado["interrupciones"].append({"clave": clave, "tipo": "EntornoInterrumpido", "detalle": str(ex)[:300]})
                _agregar(salida / "interrupciones.jsonl", estado["interrupciones"][-1])
                if reintento:
                    raise Detener("dos interrupciones seguidas de Ollama")
                continue
            except (V2.MemoriaInsuficiente, V2.ContextoInsuficiente) as ex:
                estado["interrupciones"].append({"clave": clave, "tipo": type(ex).__name__, "detalle": str(ex)[:300]})
                _agregar(salida / "interrupciones.jsonl", estado["interrupciones"][-1])
                raise Detener(type(ex).__name__)
            estado["nuevas"] += 1
            reg = {"clave": clave, "respuesta": resp, "semilla": sem, "partes": ps,
                   **{k: call.get(k) for k in ("tokens_prompt", "tokens_generados", "reloj_ms", "memoria")}}
            _agregar(salida / "llamadas.jsonl", reg)
            hechas[clave] = reg
            modelo.guardia.antes_de_sandbox()
            return resp
        return None

    observaciones, resultados = {}, dict(terminados)
    try:
        for d in defectos:
            problema = {"id": d["id"], "codigo": d["codigo"]}          # lo ÚNICO que ve la investigación
            obs = observar(co, problema)
            observaciones[d["id"]] = obs
            log(f"  {d['id']}: {obs['estado']}" + (f" · hipótesis {obs['hipotesis']} · regla v1 "
                                                   f"{(obs['reglas_v1']['respuesta'] or {}).get('lectura', 'se abstiene')}"
                                                   if obs["estado"] == "investigable" else ""))
            if d["id"] in terminados or obs["estado"] != "investigable" or modelo is None:
                continue
            r = investigar(co, problema, obs, pedir, hechas, llamadas=llamadas_por_defecto, log=log)
            resultados[d["id"]] = r
            if r["estado"] == "incompleto":
                break
            _agregar(salida / "resultados.jsonl", {k: v for k, v in r.items() if k != "intentos"} |
                     {"intentos": [{k: v for k, v in it.items() if k != "partes"} for it in r["intentos"]]})
    except Detener as ex:
        estado["corrida"] = f"detenido_por_entorno: {ex}"
    (salida / "observaciones.json").write_text(json.dumps(observaciones, ensure_ascii=False, indent=1, default=str))
    # El juez entra recién ahora, con la investigación terminada.
    juez = Juez(ent)
    for v in resultados.values():                                      # los intentos con sus prompts (para auditar)
        for it in v["intentos"]:
            it.setdefault("partes", (hechas.get(it["clave"]) or {}).get("partes", []))
    juicio = [juzgar(juez, d, observaciones[d["id"]], resultados.get(d["id"])) for d in defectos
              if d["id"] in observaciones]
    ent.liberar(juez.alm)
    ent.liberar(co.alm)
    res = {"version": VERSION, "huella_reglas": E0.huella_reglas(), "python_sandbox": E0.version_python(cfg),
           "bordes": bordes, "params_coordinador": PARAMS_COORDINADOR, "corrida": estado["corrida"],
           "llamadas_por_defecto": llamadas_por_defecto, "llamadas_nuevas": estado["nuevas"],
           "llamadas_totales": len(hechas),
           "interrupciones": estado["interrupciones"], "juicio": juicio, "resumen": resumir(juicio)}
    (salida / "juez.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    (salida / "resumen.md").write_text(markdown(res))
    return res


def resumir(juicio: list[dict]) -> dict:
    from collections import Counter
    inv = [j for j in juicio if "reglas_v1_punta_a_punta" in j]
    return {"defectos": len(juicio), "equivalentes": [j["id"] for j in juicio if j["equivalente"]],
            "no_detectados": [j["id"] for j in juicio if j["estado"] == "no_detectado" and not j["equivalente"]],
            "causa_fuera_de_las_candidatas": [j["id"] for j in inv if not j["causa_entre_candidatas"]],
            "reglas_v1_punta_a_punta": dict(Counter(j["reglas_v1_punta_a_punta"] for j in inv)),
            "resultados": dict(Counter(j["resultado"] for j in juicio if j.get("resultado"))),
            "confirmaciones_falsas": [j["id"] for j in juicio if j.get("resultado") == "CONFIRMACIÓN FALSA"],
            "incompletos": [j["id"] for j in juicio if "pendientes" in j],
            "redirecciones": dict(Counter("tomada" if x["redireccion"]["valida"] else "no tomada"
                                          for j in juicio for x in j.get("intentos", []) if x.get("redireccion"))),
            "fugas": sum(len(x["fugas"]) for j in juicio for x in j.get("intentos", []))}


def markdown(res: dict) -> str:
    r = res["resumen"]
    out = [f"# {res['version']}: hipótesis → reparación → verificación", "",
           f"Reglas E0 `{res['huella_reglas']}` · Python del sandbox {res['python_sandbox']} · corrida: "
           f"**{res['corrida']}** · llamadas nuevas {res['llamadas_nuevas']} (total {res['llamadas_totales']}) · "
           f"datos de borde: {res['bordes']}", "",
           f"- **Confirmaciones falsas: {len(r['confirmaciones_falsas'])}** {r['confirmaciones_falsas']}",
           f"- Resultados: {r['resultados']}",
           f"- Incompletos por el límite de llamadas (sin juzgar): {r['incompletos']}",
           f"- Redirecciones propuestas por Qwen: {r['redirecciones']}",
           f"- Reglas v1 de punta a punta (sin filtro): {r['reglas_v1_punta_a_punta']}",
           f"- Equivalentes según el juez: {r['equivalentes']} · no detectados: {r['no_detectados']} · causa fuera "
           f"de las candidatas: {r['causa_fuera_de_las_candidatas']} · fugas: {r['fugas']}", "", "## Por defecto", ""]
    for j in res["juicio"]:
        linea = f"- **{j['id']}** ({j['clase']}; objetivo {j['objetivo']}): {j.get('resultado') or j['estado']}"
        if "regla_v1" in j:
            linea += f" · regla v1: {j['reglas_v1_punta_a_punta']}"
        if j.get("motivo"):
            linea += f" · {j['motivo']}"
        for it in j.get("intentos", []):
            linea += (f"\n  - {it['clave']}: {it['etapas']}"
                      + (f" · juez: {'correcta' if it['correcta_segun_juez'] else 'incorrecta'}"
                         if "correcta_segun_juez" in it else "")
                      + (f" · FUGAS {it['fugas']}" if it["fugas"] else ""))
        for ln in j.get("pendientes", []):
            linea += f"\n  - hipótesis L{ln}: pendiente (no se pidió: límite de llamadas)"
        out.append(linea)
    return "\n".join(out) + "\n"
