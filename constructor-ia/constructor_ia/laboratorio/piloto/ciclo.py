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
from . import suite_ventanas as SV
from . import traza as T
from . import v2 as V2
from . import verificaciones as V
from ...instrumentos.medicion import ejecutar

VERSION = "ciclo v1.3.3"                   # v1.3.3: verificación del recuento y datos variados del coordinador
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
    """Lo que el coordinador puede usar: el entorno, SU almacén, SUS verificadores y SUS parámetros. Desde v1.3.3,
    también SUS datos variados (otra base privada propia, distinta de la principal y de la del juez) y la
    verificación del recuento (suite_ventanas)."""

    def __init__(self, ent, ent_variado=None):
        self.ent, self.ent_variado = ent, ent_variado
        self.alm = ent.almacen(f"ciclo_coordinador_{int(time.time() * 1000)}")
        self.vf = self.alm.save("verificador_fijo", EL.VERIFICADOR_FIJO, {"proposito": "verificador"},
                                author="laboratorio")["id"]
        self.vc = self.alm.save("verificador_contraste", E0.VERIFICADOR_CONTRASTE, {"proposito": "verificador"},
                                author="laboratorio")["id"]
        self.suite_verificacion = SV.guardar(self.alm)
        if ent_variado is not None:
            self.alm_variado = ent_variado.almacen(f"ciclo_coordinador_variado_{int(time.time() * 1000)}")
            self.suite_variada = SV.guardar(self.alm_variado)
        self.n = 0

    def guardar(self, codigo: str, almacen=None) -> int | None:
        self.n += 1
        try:
            return (almacen or self.alm).save(f"ciclo_{self.n}", codigo, B.MANIFIESTO, author="laboratorio")["id"]
        except Exception:            # noqa: BLE001 - la revisión estática rechazó el código
            return None

    def suite(self, iid: int, params: dict) -> dict:
        """La suite de OBSERVACIÓN (la de siempre: S1–S3 + VF), para detectar y localizar."""
        return V.evaluar(self.ent, self.alm, iid, params, [{"id": "VF", "instrumento_id": self.vf}])

    def liberar(self):
        self.ent.liberar(self.alm)
        if self.ent_variado is not None:
            self.ent_variado.liberar(self.alm_variado)


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


# Pedido de reparación (v1.3: diagnóstico estructurado) ------------------------------------------------------------

FORMATO = ("Respondé EXACTAMENTE con este formato:\n"
           "DIAGNOSTICO: <número de línea de UNA de las instrucciones candidatas>\n"
           "CAMBIA: <la variable o clave que tu cambio modifica, por ejemplo total o datos[\"clave\"]>\n"
           "EFECTO: <opcional: el valor que esperás en la salida después del cambio, por ejemplo campo = 10>\n"
           "```python\n<el código que REEMPLAZA a la instrucción de la línea DIAGNOSTICO, SIN números de línea, "
           "SIN «>>» y SIN «|»>\n```\n"
           "MOTIVO: <una frase>\n"
           "UNA sola propuesta por respuesta: un DIAGNOSTICO, un CAMBIA y un bloque; si dudás entre dos líneas, elegí "
           "una (una respuesta con dos propuestas distintas se rechaza sin probar ninguna).\n"
           "Si la corrección es borrar esa instrucción, en lugar del bloque escribí ELIMINAR.\n"
           "Si creés que ninguna de las candidatas es la causa, respondé solo DIAGNOSTICO: NINGUNA y MOTIVO.")


def partes(problema: dict, obs: dict, sugerida: int, historia=(), pendientes=(), compacto: bool = False
           ) -> list[tuple[str, str]]:
    """sugerida: la hipótesis del coordinador (una SUGERENCIA: Qwen puede diagnosticar otra candidata).
    historia: intentos anteriores de esta investigación (retroalimentación); pendientes: candidatas sin probar."""
    codigo = problema["codigo"]
    u = P.unidad_en(codigo, sugerida)
    evid = []
    for j in obs["juegos"]:
        if j["fallas"]:
            salida = json.dumps(j["salida"], ensure_ascii=False)[:900] if j["salida"] is not None else "(no terminó)"
            evid.append(f"Con los parámetros {j['params']}:\n  Salida: {salida}\n  Verificaciones que fallan:\n"
                        + "\n".join("  " + x for x in j["texto"].splitlines()))
    cands = []
    for ln in obs["candidatas"]:
        cu = P.unidad_en(codigo, ln)
        marca = "  ← sugerida por el coordinador" if ln == sugerida else ""
        cands.append(f"Línea {ln}{marca}:\n```python\n{instruccion(codigo, cu)}\n```")
    previo = []
    if historia:
        previo = historial(codigo, historia, compacto)
        previo.append(("historial", "Candidatas que todavía no se probaron: "
                                    + (", ".join(f"línea {x}" for x in pendientes) or "ninguna") + ".\n"))
    return [("coordinador", f"Un instrumento de medición escrito en Python responde: {B.PREGUNTA}\n"),
            ("evidencia", "\n\n".join(evid) + "\n"),
            ("coordinador", GLOSARIO + "\n"),
            *previo,
            ("coordinador", "Código actual. Los números de línea y la marca «>>» están SOLO para ubicarte; no son "
                            "parte del código:\n" + P.mostrar(codigo, u, contexto=200, completa=True) + "\n"),
            ("coordinador", "Instrucciones candidatas (solo se puede cambiar UNA de estas, completa):\n\n"
                            + "\n\n".join(cands) + "\n"),
            ("coordinador", f"El coordinador sugiere investigar la línea {sugerida}, pero es solo una sugerencia, TODAVÍA "
                            "NO COMPROBADA: primero decidí cuál de las candidatas es la causa (DIAGNOSTICO) y qué "
                            "variable o clave hay que cambiar (CAMBIA); después escribí el cambio MÍNIMO para ESA "
                            "instrucción, que haga lo que dijiste.\n" + FORMATO)]


CATEGORIAS = {"ambigua": "la respuesta tenía varias propuestas incompatibles y no se probó ninguna",
              "formato": "la respuesta no tiene un diagnóstico válido", "incoherencia": "el código no corresponde "
              "al diagnóstico", "sintaxis": "el fragmento no compila", "ensamblado": "no se pudo ensamblar en el código",
              "revision": "la revisión estática del sandbox lo rechazó", "sin_cambio": "no cambia el código",
              "comportamiento": "se ejecutó y no pasó las verificaciones"}


def historial(codigo: str, historia, compacto: bool) -> list[tuple[str, str]]:
    """Retroalimentación: qué se sugirió, qué diagnosticó Qwen, qué cambió, qué falló y por qué. El código que
    propuso Qwen va en partes «propuesta_previa» (texto del propio modelo: la auditoría de fugas no lo cuenta)."""
    out = [("historial", "Intentos anteriores de esta investigación (ninguno se aceptó):\n")]
    for n, it in enumerate(historia, 1):
        d = it["diagnostico"]
        txt = f"Intento {n}: el coordinador sugirió la línea {it['sugerida']}"
        if it.get("ambiguedad"):
            txt += ("; tu respuesta tenía VARIAS propuestas incompatibles (" + "; ".join(it["ambiguedad"])
                    + "), así que no se probó ninguna: respondé con UNA sola")
        else:
            txt += (f"; diagnosticaste la línea {d['linea']}" if d.get("linea") else "; no diste un diagnóstico válido")
        txt += (f" y declaraste que cambiabas {d['cambia']}" if d.get("cambia") else "")
        txt += (f", esperando {d['efecto']}" if d.get("efecto") else "") + ".\n"
        out.append(("historial", txt))
        frag = (it.get("propuesta") or {}).get("fragmento_crudo")
        if it.get("propuesta", {}).get("tipo") == "eliminar":
            out.append(("propuesta_previa", "  Propusiste: ELIMINAR esa instrucción.\n"))
        elif frag and not compacto:
            out.append(("propuesta_previa", "  Propusiste:\n```python\n" + frag.strip()[:400] + "\n```\n"))
        elif frag:
            out.append(("propuesta_previa", "  Propusiste un reemplazo.\n"))
        if it.get("rechazo"):
            det = it.get("rechazo_detalle") or ""
            out.append(("historial", f"  Resultado: rechazada: {CATEGORIAS[it['rechazo']]}"
                                     + (f": {det[:400 if not compacto else 140]}" if det else "") + ".\n"))
        if (it.get("efecto_previo") or {}).get("compatible") is False:
            out.append(("historial", f"  Antes de ejecutar: {it['efecto_previo']['aviso']}\n"))
        for aviso in (it.get("efecto_observado") or {}).get("avisos", []):
            out.append(("historial", f"  Efecto observado: {aviso}\n"))
    return out


def instruccion(codigo: str, u: P.Unidad) -> str:
    """La instrucción completa, sin numeración ni marcas y sin la sangría del bloque."""
    return textwrap.dedent("\n".join(codigo.splitlines()[u.lineno - 1:u.end_lineno]))


RE_BLOQUE = re.compile(r"```[a-zA-Z0-9]*\n(.*?)```", re.S)
RE_CAMPO = {k: re.compile(rf"^[ \t]*\**{k}\**[ \t]*:[ \t]*(.+?)[ \t]*$", re.M | re.I)
            for k in ("DIAGNOSTICO", "CAMBIA", "EFECTO", "MOTIVO")}


def interpretar(texto: str | None) -> dict:
    """Separa los campos de la respuesta, sin decidir nada todavía. Lee TODAS las apariciones de cada campo y todos
    los bloques: si hay varias propuestas incompatibles, `ambiguedades()` lo dice (no se elige la primera ni la
    última)."""
    t = texto or ""
    afuera = RE_BLOQUE.sub("", t)                 # los campos se leen FUERA de los bloques de código
    todos = {k: [m.group(1).strip() for m in r.finditer(afuera)] for k, r in RE_CAMPO.items()}
    bloques = [m.group(1) for m in RE_BLOQUE.finditer(t) if m.group(1).strip()]
    eliminar = bool(re.search(r"^[ \t]*ELIMINAR[ \t]*$", afuera, re.M))
    campos = {k: (v[0] if v else None) for k, v in todos.items()}
    if bloques:
        tipo, frag = "reemplazar", bloques[0]
    elif eliminar:
        tipo, frag = "eliminar", None
    elif (campos["DIAGNOSTICO"] or "").upper().startswith("NINGUNA"):
        tipo, frag = "ninguna", None
    else:
        tipo, frag = "sin_codigo", None
    return {"tipo": tipo, "fragmento": frag, **{k.lower(): v for k, v in campos.items()},
            "todos": {k.lower(): v for k, v in todos.items()}, "bloques": bloques, "eliminar": eliminar}


def _diag_norm(v: str) -> str:
    m = re.match(r"(?:l[íi]nea\s*)?(\d+)\b", v, re.I)
    return m.group(1) if m else ("NINGUNA" if v.upper().startswith("NINGUNA") else v.strip().lower())


def ambiguedades(it: dict) -> list[str]:
    """Propuestas incompatibles dentro de UNA respuesta. Repetir lo mismo no es ambiguo; decir dos cosas
    distintas sí: dos DIAGNOSTICO, dos CAMBIA, dos bloques de código distintos, o código y ELIMINAR a la vez."""
    out = []
    diags = list(dict.fromkeys(_diag_norm(v) for v in it["todos"]["diagnostico"]))
    if len(diags) > 1:
        out.append("DIAGNOSTICO distintos: " + ", ".join(f"«{v}»" for v in diags))
    cambias = list(dict.fromkeys(str(objetivo_declarado(v) or v.strip()) for v in it["todos"]["cambia"]))
    if len(cambias) > 1:
        out.append("CAMBIA distintos: " + ", ".join(f"«{v}»" for v in it["todos"]["cambia"]))
    bloques = list(dict.fromkeys(P.limpiar_fragmento(b).strip() for b in it["bloques"]))
    if len(bloques) > 1:
        out.append(f"{len(bloques)} bloques de código distintos")
    if it["bloques"] and it["eliminar"]:
        out.append("un bloque de código y ELIMINAR a la vez")
    return out


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
    3. los números son consecutivos y empiezan en la línea de la instrucción diagnosticada;
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
        return None, f"la numeración {nums} no es la de la instrucción diagnosticada (desde {desde}): no se limpia"
    limpio = "\n".join(RE_NUMERADA.match(ln).group(2) if ln.strip() else "" for ln in lineas) + "\n"
    error = _compila(limpio)
    if error:
        return None, f"sin la numeración tampoco compila: {error}"
    return limpio, f"se quitó la numeración {nums[0]}–{nums[-1]}"


# Coherencia entre diagnóstico, ubicación y modificación (genérica: árbol sintáctico, no contenido) --------------

RE_OBJETIVO = re.compile(r"""^`?\s*([A-Za-z_]\w*)\s*(?:\[\s*(['"])(\w+)\2\s*\]|\.(\w+))?\s*`?$""")


def objetivo_declarado(texto: str | None) -> tuple[str, str | None] | None:
    """«total» → ("total", None); «datos["clave"]», «datos['clave']» o «datos.clave» → ("datos", "clave")."""
    m = RE_OBJETIVO.match((texto or "").strip())
    if not m:
        return None
    return m.group(1), m.group(3) or m.group(4)


def escrituras(nodos) -> set[tuple[str, str | None]]:
    """Lo que escriben unas instrucciones: (nombre, None) y, para d["clave"] = …, también (d, "clave")."""
    out = set()

    def objetivo(t):
        if isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                objetivo(e)
        elif isinstance(t, ast.Name):
            out.add((t.id, None))
        elif isinstance(t, ast.Subscript):
            base = L._base(t)
            if base:
                out.add((base, None))
                if isinstance(t.slice, ast.Constant) and isinstance(t.slice.value, str):
                    out.add((base, t.slice.value))
        elif isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name):
            out.add((t.value.id, None))
            out.add((t.value.id, t.attr))
    for n in nodos:
        for x in ast.walk(n):
            if isinstance(x, ast.Assign):
                for t in x.targets:
                    objetivo(t)
            elif isinstance(x, (ast.AugAssign, ast.AnnAssign)):
                objetivo(x.target)
            elif isinstance(x, (ast.For, ast.comprehension)):
                objetivo(x.target)
    return out


def _nombre(obj) -> str:
    return obj[0] if obj[1] is None else f'{obj[0]}["{obj[1]}"]'


def _especificas(esc: set) -> set:
    """La forma más precisa de cada escritura: la clave si la hay; si no, la variable."""
    return {x for x in esc if x[1] is not None or not any(y[0] == x[0] and y[1] is not None for y in esc)}


def coherencia(codigo: str, u: P.Unidad, tipo: str, fragmento: str | None, declarado) -> list[str]:
    """Contradicciones entre lo que Qwen dijo (CAMBIA) y lo que su código hace en la instrucción que diagnosticó:
    1. lo declarado tiene que existir en el programa;
    2. el código tiene que ESCRIBIR lo declarado (si propone eliminar, la instrucción eliminada tiene que escribirlo);
    3. el reemplazo no puede dejar de escribir, sin declararlo, algo que la instrucción original escribía.
    Vacía = coherente. Solo puede RECHAZAR: la aceptación sigue siendo la verificación en el sandbox."""
    original = escrituras(ast.parse(instruccion(codigo, u)).body)
    nuevas = set() if tipo == "eliminar" else escrituras(ast.parse(P.limpiar_fragmento(fragmento)).body)
    arbol = ast.parse(codigo)
    if declarado not in escrituras(arbol.body) and declarado[0] not in L._nombres(arbol):
        return [f"declaraste que cambiabas {_nombre(declarado)}, pero eso no existe en el programa"]
    problemas = []
    if tipo == "eliminar":
        if declarado not in original:
            problemas.append(f"declaraste que cambiabas {_nombre(declarado)}, pero la instrucción que proponés "
                             f"eliminar (línea {u.lineno}) no lo escribe")
    elif declarado not in nuevas:
        problemas.append(f"declaraste que cambiabas {_nombre(declarado)}, pero tu código escribe "
                         f"{', '.join(sorted(_nombre(x) for x in _especificas(nuevas))) or 'nada'}")
    perdidas = {x for x in _especificas(original) - nuevas
                if x != declarado and not (declarado[1] is None and x[0] == declarado[0])}
    if perdidas and tipo != "eliminar":
        problemas.append(f"tu reemplazo de la línea {u.lineno} deja de escribir "
                         f"{', '.join(sorted(_nombre(x) for x in perdidas))}, que la instrucción original escribía y "
                         "no declaraste cambiar")
    return problemas


def _hojas(x, ruta=""):
    if isinstance(x, dict):
        for k, v in x.items():
            yield from _hojas(v, f"{ruta}.{k}" if ruta else str(k))
    elif isinstance(x, list):
        for i, v in enumerate(x[:5]):
            yield from _hojas(v, f"{ruta}[{i}]")
    else:
        yield ruta, x


def _clase(v) -> str:
    return ("booleano" if isinstance(v, bool) else "número" if isinstance(v, (int, float)) else
            "texto" if isinstance(v, str) else "vacío" if v is None else type(v).__name__)


RE_EFECTO = re.compile(r"^\s*`?([\w.\[\]\"']+)`?\s*=\s*(-?\d+(?:\.\d+)?)\s*$")


def efecto(salida_antes, salida_despues, declarado: str | None) -> dict:
    """Compara la salida reparada con la del defecto (mismos parámetros visibles): cambios de CLASE de valor
    (número → booleano, por ejemplo) y, si Qwen declaró EFECTO «campo = número», si se cumplió. Solo informa."""
    avisos = []
    antes = dict(_hojas(salida_antes)) if salida_antes is not None else {}
    despues = dict(_hojas(salida_despues)) if salida_despues is not None else {}
    for ruta, v in antes.items():
        if ruta in despues and _clase(v) != _clase(despues[ruta]):
            avisos.append(f"{ruta} pasó de {_clase(v)} ({str(v)[:30]}) a {_clase(despues[ruta])} "
                          f"({str(despues[ruta])[:30]})")
    cumplido = None
    m = RE_EFECTO.match(declarado or "")
    if m and despues:
        campo, valor = re.findall(r"\w+", m.group(1))[-1], float(m.group(2))
        rutas = [r for r in despues if r.split(".")[-1] == campo]
        if rutas:
            obtenido = despues[rutas[0]]
            cumplido = isinstance(obtenido, (int, float)) and not isinstance(obtenido, bool) and \
                abs(obtenido - valor) <= 1e-6 * max(1.0, abs(valor))
            if not cumplido:
                avisos.append(f"esperabas {campo} = {m.group(2)} y quedó {rutas[0]} = {str(obtenido)[:30]}")
    return {"avisos": avisos[:6], "efecto_cumplido": cumplido}


def referencias(obs: dict) -> dict:
    """Valores que el COORDINADOR ya midió y le mostró a Qwen (contraejemplos {'salida', 'medido'} de sus
    verificaciones, con los parámetros visibles), por campo. Nada del juez."""
    refs = {}
    for f in obs["juegos"][0]["fallas"]:
        try:
            ej = ast.literal_eval(f.get("detalle") or "")
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            continue
        if isinstance(ej, list) and len(ej) == 1 and isinstance(ej[0], dict) and set(ej[0]) == {"salida", "medido"} \
                and isinstance(ej[0]["medido"], (int, float)):
            for campo in f.get("campos") or []:
                refs[campo] = ej[0]["medido"]
    return refs


def contrastar_efecto(declarado: str | None, refs: dict) -> dict:
    """¿El EFECTO declarado contradice lo que las verificaciones del coordinador ya midieron? Solo AVISA: no rechaza
    ni acepta (Qwen puede declarar mal el efecto de una reparación correcta)."""
    m = RE_EFECTO.match(declarado or "")
    if not m:
        return {"compatible": None, "aviso": "sin EFECTO numérico declarado"}
    campo, valor = re.findall(r"\w+", m.group(1))[-1], float(m.group(2))
    if campo not in refs:
        return {"compatible": None, "aviso": f"las verificaciones no miden {campo}"}
    ref = refs[campo]
    ok = abs(ref - valor) <= 1e-6 * max(1.0, abs(ref))
    return {"compatible": ok, "referencia": {campo: ref},
            "aviso": "coincide con lo que midió VF" if ok else
                     f"tu EFECTO espera {campo} = {m.group(2)}, pero la verificación VF midió {campo} = {ref}"}


def preparar(texto: str | None, codigo: str, autorizadas: list[int], usados: dict) -> dict:
    """Etapas: propuesta → diagnóstico (línea autorizada, CAMBIA) → limpieza → fragmento → COHERENCIA → ensamblado.
    Sin un diagnóstico válido NO se aplica nada (ni a la línea sugerida)."""
    it = interpretar(texto)
    reg = {"propuesta": {"tipo": it["tipo"], "fragmento_crudo": it.get("fragmento")},
           "diagnostico": {"texto": it["diagnostico"], "linea": None, "cambia": it["cambia"], "efecto": it["efecto"],
                           "motivo": it["motivo"], "valido": False, "problema": None},
           "limpieza": {"aplicada": False, "detalle": "no corresponde"},
           "fragmento": {"valido": None, "error": None},
           "coherencia": {"revisada": False, "problemas": []},
           "ensamblado": {"realizado": False, "ok": False, "atribucion": None, "detalle": "no corresponde"}}
    d = reg["diagnostico"]

    def invalido(problema):
        d["problema"] = problema
        reg["ensamblado"]["detalle"] = f"no se aplica nada: {problema}"
        return reg
    amb = ambiguedades(it)
    reg["ambiguedad"] = amb
    if amb:
        return invalido("respuesta ambigua: " + "; ".join(amb))
    if it["tipo"] == "ninguna":
        d["valido"] = True
        reg["ensamblado"]["detalle"] = "Qwen diagnosticó que ninguna candidata es la causa"
        return reg
    if not it["diagnostico"]:
        return invalido("falta DIAGNOSTICO")
    num = re.match(r"(?:l[íi]nea\s*)?(\d+)\b", it["diagnostico"], re.I)
    if not num:
        return invalido(f"DIAGNOSTICO «{it['diagnostico'][:40]}» no es un número de línea")
    n = int(num.group(1))
    u = P.unidad_en(codigo, n) if 1 <= n <= len(codigo.splitlines()) else None
    if u is None or u.lineno not in autorizadas:
        return invalido(f"la línea {n} no es una de las candidatas autorizadas {autorizadas}")
    if usados.get(u.lineno, 0) >= MAX_POR_LINEA:
        return invalido(f"la línea {u.lineno} ya tuvo {MAX_POR_LINEA} intentos")
    d["linea"] = u.lineno
    if it["tipo"] == "sin_codigo":
        return invalido("falta el bloque de código (o ELIMINAR)")
    declarado = objetivo_declarado(it["cambia"])
    if declarado is None:
        return invalido("falta CAMBIA, o no es una variable o clave («total», «datos[\"clave\"]»)")
    d["valido"] = True
    fragmento = it.get("fragmento") or ""
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
    problemas = coherencia(codigo, u, it["tipo"], fragmento, declarado)
    reg["coherencia"] = {"revisada": True, "problemas": problemas}
    if problemas:
        reg["ensamblado"]["detalle"] = "no se ensambla: " + "; ".join(problemas)
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
    """Acepta solo si, en los DOS juegos de parámetros, sobre los datos principales Y sobre los datos variados del
    coordinador, pasa toda la suite (S1–S3, VF y, desde v1.3.3, VC_cobertura) y conserva la forma de la salida.
    `rechazada_por` dice qué verificación, con qué datos y con qué parámetros."""
    if nuevo == codigo:
        return {"aceptada": False, "motivo": "no cambia el código", "categoria": "sin_cambio", "juegos": []}
    iid = co.guardar(nuevo)
    if iid is None:
        return {"aceptada": False, "motivo": "la revisión estática lo rechazó", "categoria": "revision", "juegos": []}
    suite = SV.con_limite(co.suite_verificacion, nuevo)
    res = []
    for j in obs["juegos"]:
        ev = V.evaluar(co.ent, co.alm, iid, j["params"], suite)
        fallas = [f["id"] for f in V.fallas(ev)] + [f"{x['id']}(inválido)" for x in ev["invalidos"]]
        detalle = "; ".join(f"{f['id']}: {f['detalle'][:200]}" for f in V.fallas(ev))
        if ev["run"]["status"] == "rechazado":            # la revisión estática corre al ejecutar
            return {"aceptada": False, "motivo": f"la revisión estática lo rechazó: {ev['run'].get('error')}",
                    "categoria": "revision", "detalle": str(ev["run"].get("error") or "")[:300], "juegos": res,
                    "rechazada_por": {"datos": "principales", "params": j["params"], "verificaciones": ["revisión"]}}
        if ev["salida"] is None:
            forma_ok = False
        elif j["forma"] is not None:
            forma_ok = E0.forma(ev["salida"]) == j["forma"]
        else:                        # el defecto no terminaba con estos parámetros: al menos, nada vacío
            forma_ok = _listas_llenas((ev["salida"] or {}).get("resultado"))
        res.append({"params": j["params"], "fallas": fallas, "forma_ok": forma_ok, "salida": ev["salida"]})
        if fallas or not forma_ok:
            motivo = (f"con {j['params']} falla {fallas}" if fallas else
                      f"con {j['params']} pasa, pero cambió la forma de la salida")
            det = (f"con los parámetros {j['params']}: " + (detalle if fallas else
                   "pasa las verificaciones, pero la salida cambió de forma (claves o cantidad de elementos)"))
            return {"aceptada": False, "motivo": motivo, "categoria": "comportamiento", "detalle": det[:600],
                    "juegos": res, "rechazada_por": {"datos": "principales", "params": j["params"],
                                                     "verificaciones": fallas or ["forma"]}}
    if co.ent_variado is not None:
        iid_v = co.guardar(nuevo, co.alm_variado)
        suite_v = SV.con_limite(co.suite_variada, nuevo)
        for j, principal in zip(obs["juegos"], res):
            ev = V.evaluar(co.ent_variado, co.alm_variado, iid_v, j["params"], suite_v)
            fallas = [f["id"] for f in V.fallas(ev)] + [f"{x['id']}(inválido)" for x in ev["invalidos"]]
            forma_ok = ev["salida"] is not None and E0.forma(ev["salida"]) == E0.forma(principal["salida"])
            if fallas or not forma_ok:
                campos = sorted({c for f in V.fallas(ev) for c in f["campos"]})
                det = ("con los DATOS VARIADOS del coordinador (otra base propia; sus valores no se muestran) y los "
                       f"parámetros {j['params']}: " + (f"fallan {', '.join(fallas)}"
                                                        + (f" (campos: {', '.join(campos)})" if campos else "")
                                                        if fallas else "la salida cambió de forma")
                       + ". Una reparación que solo reproduce los resultados de los datos conocidos no pasa acá.")
                return {"aceptada": False, "motivo": f"con los datos variados y {j['params']}: falla "
                                                     f"{fallas or ['forma']}",
                        "categoria": "comportamiento", "detalle": det, "juegos": res,
                        "rechazada_por": {"datos": "variados", "params": j["params"],
                                          "verificaciones": fallas or ["forma"]}}
    return {"aceptada": True, "motivo": "pasa toda la suite y conserva la forma en los dos juegos"
            + (", con los datos principales y con los variados" if co.ent_variado is not None else ""),
            "categoria": None, "juegos": res}


# Investigación de un defecto (v1.3) ------------------------------------------------------------------------------

MAX_SIN_DIAGNOSTICO = 2      # respuestas sin diagnóstico válido toleradas por investigación (después, se abstiene)


def etapas(reg: dict) -> str:
    """Una línea legible: sugerida → diagnóstico → limpieza → fragmento → coherencia → ensamblado → verificación →
    efecto → siguiente."""
    d = reg["diagnostico"]
    out = [f"sugerida L{reg['sugerida']}"]
    if reg["propuesta"]["tipo"] == "ninguna":
        out.append("diagnóstico: NINGUNA de las candidatas")
    elif d["valido"]:
        out.append(f"diagnóstico L{d['linea']}, cambia {d['cambia']}" + (f", espera {d['efecto']}" if d["efecto"] else ""))
    else:
        out.append(f"diagnóstico inválido ({d['problema']})")
    if reg["limpieza"]["aplicada"]:
        out.append(f"limpieza: {reg['limpieza']['detalle']}")
    if (reg.get("efecto_previo") or {}).get("compatible") is False:
        out.append(f"aviso previo: {reg['efecto_previo']['aviso']}")
    if reg["fragmento"]["valido"] is not None:
        out.append("fragmento válido" if reg["fragmento"]["valido"] else f"fragmento inválido ({reg['fragmento']['error']})")
    if reg["coherencia"]["revisada"]:
        out.append("coherente" if not reg["coherencia"]["problemas"] else
                   "INCOHERENTE: " + "; ".join(reg["coherencia"]["problemas"]))
    e = reg["ensamblado"]
    if e["realizado"]:
        out.append("ensamblado ok" if e["ok"] else f"ensamblado fallido ({e['atribucion']}: {e['detalle']})")
    v = reg.get("verificacion") or {}
    if v.get("realizada"):
        out.append("ACEPTADA" if v.get("aceptada") else f"rechazada: {v.get('motivo')}")
    elif reg["propuesta"]["tipo"] != "ninguna":
        out.append("verificación no realizada")
    for aviso in (reg.get("efecto_observado") or {}).get("avisos", []):
        out.append(f"efecto: {aviso}")
    dec = reg.get("decision")
    if dec:
        out.append(f"siguiente: {('sugerir L' + str(dec['siguiente'])) if dec['siguiente'] else 'ninguna'} "
                   f"({dec['porque']})")
    return " → ".join(out)


class Detener(Exception):
    """El entorno obliga a parar la corrida (se reanuda después)."""


def _rechazo(reg: dict) -> tuple[str | None, str]:
    d = reg["diagnostico"]
    if reg["propuesta"]["tipo"] == "ninguna":
        return None, ""
    if reg.get("ambiguedad"):
        return "ambigua", d["problema"] or ""
    if not d["valido"]:
        return "formato", d["problema"] or ""
    if reg["fragmento"]["valido"] is False:
        return "sintaxis", reg["fragmento"]["error"] or ""
    if reg["coherencia"]["problemas"]:
        return "incoherencia", "; ".join(reg["coherencia"]["problemas"])
    if reg["ensamblado"]["realizado"] and not reg["ensamblado"]["ok"]:
        return "ensamblado", reg["ensamblado"]["detalle"]
    v = reg.get("verificacion") or {}
    if v.get("aceptada"):
        return None, ""
    return v.get("categoria") or "comportamiento", v.get("detalle") or v.get("motivo") or ""


def decidir(reg: dict, cola: list[int], usados: dict, fallas_iniciales: set) -> tuple[int | None, str]:
    """Qué SUGERIR en la llamada siguiente (Qwen puede diagnosticar otra candidata). Regla fija:
    1. la línea que Qwen diagnosticó, si el intento no llegó a probarse (sintaxis, incoherencia, ensamblado,
       revisión, sin cambio) o si se ejecutó y REDUJO las verificaciones que fallan, y le quedan intentos;
    2. si la respuesta no tuvo diagnóstico válido, la misma sugerencia (reintento con la retroalimentación);
    3. la siguiente candidata sin probar de la cola;
    4. ninguna."""
    d, rechazo = reg["diagnostico"], reg.get("rechazo")
    if rechazo in ("formato", "ambigua"):
        return reg["sugerida"], ("reintento: la respuesta tuvo varias propuestas incompatibles" if rechazo == "ambigua"
                                 else "reintento: la respuesta no tuvo un diagnóstico válido")
    L = d.get("linea")
    if L and usados.get(L, 0) < MAX_POR_LINEA:
        if rechazo in ("sintaxis", "incoherencia", "ensamblado", "revision", "sin_cambio"):
            return L, f"reintento sobre el diagnóstico: no llegó a probarse ({CATEGORIAS[rechazo]})"
        juegos = (reg.get("verificacion") or {}).get("juegos") or []
        if rechazo == "comportamiento" and juegos and set(juegos[0]["fallas"]) < fallas_iniciales:
            return L, f"reintento sobre el diagnóstico: redujo las fallas ({sorted(fallas_iniciales)} → " \
                      f"{sorted(juegos[0]['fallas'])})"
    for x in cola:
        if usados.get(x, 0) == 0:
            return x, "siguiente candidata sin probar"
    return None, "no quedan candidatas por probar"


NO_CABE = object()


def investigar(co: Coordinador, problema: dict, obs: dict, pedir, hechas: dict, *,
               llamadas: int = MAX_HIPOTESIS, log=print) -> dict:
    """Investigación v1.3. pedir(clave, partes, semilla) -> texto | None | NO_CABE.
    None: el límite GLOBAL cortó (incompleto). NO_CABE: no entra en el contexto.
    hechas: respuestas ya obtenidas (reanudación): el camino se reconstruye igual, sin pedir de nuevo."""
    codigo = problema["codigo"]
    llamadas = max(1, min(int(llamadas), TOPE_LLAMADAS))
    cola, autorizadas = list(obs["hipotesis"]), list(obs["candidatas"])
    usados: dict = {}
    sin_diagnostico = 0
    fallas_iniciales = {f["id"] for f in obs["juegos"][0]["fallas"]}
    intentos, sugerida, porque = [], (cola[0] if cola else None), "primera sugerencia"
    for k in range(llamadas):
        if sugerida is None:
            return {"id": problema["id"], "estado": "se_abstiene", "motivo": porque, "intentos": intentos}
        pendientes = [x for x in cola if usados.get(x, 0) == 0]
        clave = f"{problema['id']}|c{k}|S{sugerida}"
        ps = partes(problema, obs, sugerida, intentos, pendientes)
        if clave in hechas:
            resp, ps = hechas[clave]["respuesta"], hechas[clave].get("partes") or ps
        else:
            resp = pedir(clave, ps, semilla(problema["id"], k))
            if resp is NO_CABE:                        # se resume el historial y se intenta una vez más
                ps = partes(problema, obs, sugerida, intentos, pendientes, compacto=True)
                resp = pedir(clave, ps, semilla(problema["id"], k))
            if resp is NO_CABE:
                return {"id": problema["id"], "estado": "se_abstiene", "intentos": intentos,
                        "motivo": "el pedido no entra en el contexto ni con el historial resumido"}
            if resp is None:
                return {"id": problema["id"], "estado": "incompleto", "intentos": intentos, "pendientes": pendientes}
        reg = {"clave": clave, "sugerida": sugerida, "por_que_esta_sugerencia": porque, "respuesta": resp,
               "partes": ps, **preparar(resp, codigo, autorizadas, usados)}
        if reg["diagnostico"]["valido"] and reg["diagnostico"]["efecto"]:
            reg["efecto_previo"] = contrastar_efecto(reg["diagnostico"]["efecto"], referencias(obs))
        reg["hipotesis"] = reg["diagnostico"]["linea"] or sugerida      # la instrucción efectivamente investigada
        reg["linea_reparada"] = reg["diagnostico"]["linea"] if reg["ensamblado"]["ok"] else None
        reg["interpretacion"] = reg["propuesta"]["tipo"]
        if reg["ensamblado"]["ok"]:
            reg["verificacion"] = {"realizada": True, **verificar(co, obs, codigo, reg["codigo_propuesto"])}
            j0 = (reg["verificacion"].get("juegos") or [{}])[0]
            reg["efecto_observado"] = efecto(obs["juegos"][0]["salida"], j0.get("salida"),
                                             reg["diagnostico"]["efecto"])
        else:
            reg["verificacion"] = {"realizada": False, "aceptada": None, "motivo": reg["ensamblado"]["detalle"]}
        if reg["diagnostico"]["linea"]:
            usados[reg["diagnostico"]["linea"]] = usados.get(reg["diagnostico"]["linea"], 0) + 1
        reg["rechazo"], reg["rechazo_detalle"] = _rechazo(reg)
        intentos.append(reg)
        if reg["verificacion"].get("aceptada"):
            log(f"    {clave}: {etapas(reg)}")
            return {"id": problema["id"], "estado": "confirmada", "linea": reg["linea_reparada"], "intentos": intentos}
        if reg["propuesta"]["tipo"] == "ninguna":
            log(f"    {clave}: {etapas(reg)}")
            return {"id": problema["id"], "estado": "se_abstiene", "intentos": intentos,
                    "motivo": "Qwen diagnosticó que ninguna candidata es la causa"}
        if reg["rechazo"] in ("formato", "ambigua"):
            sin_diagnostico += 1
            if sin_diagnostico >= MAX_SIN_DIAGNOSTICO:
                motivo = f"{MAX_SIN_DIAGNOSTICO} respuestas sin un diagnóstico válido y único"
                reg["decision"] = {"siguiente": None, "porque": motivo}
                log(f"    {clave}: {etapas(reg)}")
                return {"id": problema["id"], "estado": "se_abstiene", "intentos": intentos, "motivo": motivo}
        sugerida, porque = decidir(reg, cola, usados, fallas_iniciales)
        reg["decision"] = {"siguiente": sugerida, "porque": porque}
        log(f"    {clave}: {etapas(reg)}")
    return {"id": problema["id"], "estado": "se_abstiene", "intentos": intentos,
            "motivo": f"se agotó el presupuesto del defecto ({llamadas} llamadas)"}


# Juez (después, por separado) -------------------------------------------------------------------------------------

def _referencia_solo(defecto: dict) -> set[str]:
    """Líneas de la referencia que no están en el defecto (si aparecen en un prompt, hubo fuga)."""
    return ({x.strip() for x in B.REFERENCIA.splitlines() if x.strip()}
            - {x.strip() for x in defecto["codigo"].splitlines() if x.strip()})


def auditar(prompt_partes, defecto: dict, reservado: dict | None = None) -> list[str]:
    """reservado: lo que solo sabe el juez (nombre y totales de su base independiente)."""
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
    for nombre, valor in (reservado or {}).items():
        if re.search(rf"(?<![\w.]){re.escape(str(valor))}(?![\w.])", texto):
            fugas.append(f"dato reservado del juez: {nombre}")
    return fugas


# Datos independientes del juez (v1.3.2) ---------------------------------------------------------------------------
# Una segunda base privada, reproducible, que existe SOLO durante el juicio: el coordinador y Qwen nunca la ven (se
# crea cuando la investigación terminó, con otro usuario de solo lectura, y se borra al juzgar). No alcanza con otra
# semilla: cambian explícitamente el total de lecturas, el reparto válidas/inválidas, los valores y su distribución,
# la distribución temporal y los casos de borde.

DATOS_JUEZ = {"semilla": 20261023, "dias": 19, "por_dia": 437}       # el coordinador: semilla 7, 30 días × 500


def variar_datos_juez(servidor, info: dict, semilla: int) -> dict:
    """Variaciones explícitas sobre la base generada (como root en el servidor PRIVADO)."""
    import datetime as dt
    from .. import sintetica as SI
    rng = random.Random(semilla)
    lid, filas = int(info["ultimo_id"]), []

    def agregar(m, v):
        nonlocal lid
        lid += 1
        filas.append((lid, 1 + lid % SI.ESTACIONES, m, v, m + dt.timedelta(minutes=2)))
    for d in range(2, info["dias"], max(1, info["dias"] // 3)):            # ráfagas: muchas lecturas en una hora
        for _ in range(150):
            agregar(SI.BASE + dt.timedelta(days=d, hours=10, seconds=rng.randrange(3600)), round(rng.uniform(30, 70), 3))
    for k, v in enumerate((0.5, 900.0, 0.501, 899.999, 0.499, 900.001)):  # bordes, en bordes de ventana
        for minuto in (0, 14, 15, 29, 30, 59):
            agregar(SI.BASE + dt.timedelta(days=1 + k, hours=3 + k, minutes=minuto), v)
    valores = ",".join(f"({i},{e},'{m:%Y-%m-%d %H:%M:%S}',{v:.3f},'{r:%Y-%m-%d %H:%M:%S}')" for i, e, m, v, r in filas)
    servidor.sql(
        "UPDATE lecturas SET valor = ROUND(valor * 1.37 + 2.5, 3) WHERE valor > 0.5 AND valor < 600;\n"  # valores
        "UPDATE lecturas SET valor = -3.25 WHERE MOD(id, 23) = 0;\n"                     # más inválidas (bajas)
        "UPDATE lecturas SET valor = 1234.5 WHERE MOD(id, 41) = 0;\n"                    # más inválidas (altas)
        "UPDATE lecturas SET momento = momento + INTERVAL 7 MINUTE WHERE MOD(id, 3) = 0;\n"   # tiempo
        f"INSERT INTO lecturas (id, estacion_id, momento, valor, recibido) VALUES {valores};\n"
        "ANALYZE TABLE lecturas;", database=info["base"])
    info["ultimo_id"] = lid
    r = servidor.sql("SELECT COUNT(*), SUM(valor > 0.5 AND valor < 900), SUM(NOT (valor > 0.5 AND valor < 900)), "
                     "SUM(valor IN (0.5, 900)) FROM lecturas;", database=info["base"])
    total, validas, invalidas, bordes = (int(float(x)) for x in r.stdout.split())
    return {"lecturas": total, "validas": validas, "invalidas": invalidas, "valores_de_borde_exactos": bordes,
            "agregadas": len(filas)}


def crear_datos_juez(ent, datos: dict = DATOS_JUEZ):
    """La base independiente del juez, en el MISMO servidor privado, con otro nombre y otro usuario."""
    from .. import sintetica as SI
    from .entorno import Entorno
    info = SI.generar(ent.servidor, f"{ent.info['base']}_juez", semilla=datos["semilla"], dias=datos["dias"],
                      por_dia=datos["por_dia"], usuario="lab_juez")
    resumen = variar_datos_juez(ent.servidor, info, datos["semilla"])
    return Entorno(ent.cfg, ent.executor, ent.servidor, ent.dir / "juez_independiente", info=info), resumen


# Datos variados del COORDINADOR (v1.3.3): una tercera base privada, propia del coordinador, distinta de la
# principal y de la del juez. Sirve para comprobar que una reparación CALCULA y no reproduce una constante de los
# datos conocidos. Qwen sabe qué verificación falló ahí y en qué campos, pero no ve sus valores.

DATOS_VARIADOS = {"semilla": 4242, "dias": 11, "por_dia": 389}       # principal: 7, 30 × 500 · juez: 20261023, 19 × 437


def crear_datos_variados(ent, datos: dict = DATOS_VARIADOS):
    import datetime as dt
    from .. import sintetica as SI
    from .entorno import Entorno
    info = SI.generar(ent.servidor, f"{ent.info['base']}_variada", semilla=datos["semilla"], dias=datos["dias"],
                      por_dia=datos["por_dia"], usuario="lab_co2")
    rng = random.Random(datos["semilla"])
    lid, filas = int(info["ultimo_id"]), []

    def agregar(m, v):
        nonlocal lid
        lid += 1
        filas.append((lid, 1 + lid % SI.ESTACIONES, m, v, m + dt.timedelta(minutes=3)))
    for d in range(1, info["dias"], max(1, info["dias"] // 3)):            # ráfagas, a otra hora que las del juez
        for _ in range(200):
            agregar(SI.BASE + dt.timedelta(days=d, hours=16, seconds=rng.randrange(3600)), round(rng.uniform(5, 95), 3))
    for k, v in enumerate((0.5, 900.0, 0.502, 899.998, 0.498, 900.002)):  # bordes, en otros minutos
        for minuto in (1, 13, 16, 44):
            agregar(SI.BASE + dt.timedelta(days=k % max(1, info["dias"]), hours=20 - k, minutes=minuto), v)
    valores = ",".join(f"({i},{e},'{m:%Y-%m-%d %H:%M:%S}',{v:.3f},'{r:%Y-%m-%d %H:%M:%S}')" for i, e, m, v, r in filas)
    ent.servidor.sql(
        "UPDATE lecturas SET valor = ROUND(valor * 0.83 + 11, 3) WHERE valor > 0.5 AND valor < 900;\n"
        "UPDATE lecturas SET valor = 950.25 WHERE MOD(id, 17) = 0;\n"
        "UPDATE lecturas SET valor = 0.25 WHERE MOD(id, 29) = 0;\n"
        "UPDATE lecturas SET momento = momento + INTERVAL 11 MINUTE WHERE MOD(id, 4) = 1;\n"
        f"INSERT INTO lecturas (id, estacion_id, momento, valor, recibido) VALUES {valores};\n"
        "ANALYZE TABLE lecturas;", database=info["base"])
    info["ultimo_id"] = lid
    r = ent.servidor.sql("SELECT COUNT(*), SUM(valor > 0.5 AND valor < 900) FROM lecturas;", database=info["base"])
    total, validas = (int(float(x)) for x in r.stdout.split())
    return (Entorno(ent.cfg, ent.executor, ent.servidor, ent.dir / "coordinador_variado", info=info),
            {"lecturas": total, "validas": validas, "invalidas": total - validas})


class Juez:
    """Compara con la referencia en los parámetros visibles y de reserva, sobre los datos compartidos con el
    coordinador Y sobre los datos independientes (si los hay). Correcta = igual en todos."""

    def __init__(self, ent, ent_independiente=None):
        self.bases = {"compartidos": ent}
        if ent_independiente is not None:
            self.bases["independientes"] = ent_independiente
        self.alm = {k: e.almacen(f"ciclo_juez_{k}_{int(time.time() * 1000)}") for k, e in self.bases.items()}
        self.ref = {}
        for k, e in self.bases.items():
            rid = self.alm[k].save("referencia", B.REFERENCIA, B.MANIFIESTO, author="banco")["id"]
            self.ref[k] = {n: V._run(e, self.alm[k], rid, p).get("output") for n, p in self._juegos()}
        self.n = 0

    @staticmethod
    def _juegos():
        return (("visible", B.PARAMS_VISIBLES), ("reserva", B.PARAMS_RESERVA))

    def evaluar(self, codigo: str) -> dict:
        """{"compartidos": bool, "independientes": bool} (sin la segunda clave si no hay datos independientes)."""
        self.n += 1
        out = {}
        for k, e in self.bases.items():
            try:
                iid = self.alm[k].save(f"j_{self.n}", codigo, B.MANIFIESTO, author="juez")["id"]
            except Exception:            # noqa: BLE001
                out[k] = False
                continue
            out[k] = all(B.comparar(V._run(e, self.alm[k], iid, p).get("output"), self.ref[k][n])["igual"]
                         for n, p in self._juegos())
        return out

    def correcto(self, codigo: str) -> bool:
        return all(self.evaluar(codigo).values())

    def liberar(self):
        for k, e in self.bases.items():
            e.liberar(self.alm[k])


def juzgar(juez: Juez, defecto: dict, obs: dict, res: dict | None) -> dict:
    obj = DF.objetivo(defecto)
    eq = juez.evaluar(defecto["codigo"])
    out = {"id": defecto["id"], "clase": defecto["clase"], "objetivo": sorted(obj),
           "equivalente": all(eq.values()), "equivalente_por_datos": eq, "estado": (res or obs)["estado"]}
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
             "aceptada": (it.get("verificacion") or {}).get("aceptada"), "sugerida": it.get("sugerida"),
             "diagnostico": (it.get("diagnostico") or {}).get("linea"),
             "diagnostico_en_objetivo": (it.get("diagnostico") or {}).get("linea") in obj,
             "rechazo": it.get("rechazo"), "ambiguedad": it.get("ambiguedad") or [],
             "rechazada_por": (it.get("verificacion") or {}).get("rechazada_por"),
             "efecto_previo_compatible": (it.get("efecto_previo") or {}).get("compatible"),
             "fugas": auditar(it["partes"], defecto, getattr(juez, "reservado", None))}
        if "codigo_propuesto" in it and not incompleto:   # una investigación sin terminar no se juzga
            por = juez.evaluar(it["codigo_propuesto"])
            j["correcta_por_datos"] = por
            j["correcta_segun_juez"] = all(por.values())
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
    if acept is not None:
        out["aceptada_por_el_coordinador"] = True
        por = acept.get("correcta_por_datos") or {}
        out["falsa_solo_con_datos_independientes"] = (por.get("compartidos") is True
                                                     and por.get("independientes") is False)
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
           llamadas_por_defecto: int = MAX_HIPOTESIS, guardia=None, datos_juez: dict = DATOS_JUEZ,
           datos_variados: dict = DATOS_VARIADOS, log=print) -> dict:
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
    ent_variado, datos_variados_info = crear_datos_variados(ent, datos_variados)
    co = Coordinador(ent, ent_variado)
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
    # El juez entra recién ahora, con la investigación terminada. Sus datos independientes se crean recién ahora.
    ent_juez, datos = crear_datos_juez(ent, datos_juez)
    juez = Juez(ent, ent_juez)
    juez.reservado = {"base del juez": ent_juez.info["base"], "lecturas del juez": datos["lecturas"],
                      "válidas del juez": datos["validas"], "inválidas del juez": datos["invalidas"]}
    for v in resultados.values():                                      # los intentos con sus prompts (para auditar)
        for it in v["intentos"]:
            it.setdefault("partes", (hechas.get(it["clave"]) or {}).get("partes", []))
    juicio = [juzgar(juez, d, observaciones[d["id"]], resultados.get(d["id"])) for d in defectos
              if d["id"] in observaciones]
    juez.liberar()
    ent_juez.cerrar()                                       # borra la base independiente
    co.liberar()
    ent_variado.cerrar()                                    # borra la base variada del coordinador
    res = {"version": VERSION, "huella_reglas": E0.huella_reglas(), "python_sandbox": E0.version_python(cfg),
           "bordes": bordes, "params_coordinador": PARAMS_COORDINADOR, "corrida": estado["corrida"],
           "datos_independientes_del_juez": {**datos_juez, **datos},
           "datos_variados_del_coordinador": {**datos_variados, **datos_variados_info},
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
            "aceptadas_por_el_coordinador": [j["id"] for j in juicio if j.get("aceptada_por_el_coordinador")],
            "confirmaciones_verdaderas": [j["id"] for j in juicio if j.get("resultado") == "confirmación verdadera"],
            "confirmaciones_falsas": [j["id"] for j in juicio if j.get("resultado") == "CONFIRMACIÓN FALSA"],
            "falsas_que_solo_ven_los_datos_independientes": [j["id"] for j in juicio
                                                              if j.get("falsa_solo_con_datos_independientes")],
            "incompletos": [j["id"] for j in juicio if "pendientes" in j],
            "rechazos": dict(Counter(x["rechazo"] for j in juicio for x in j.get("intentos", []) if x.get("rechazo"))),
            "rechazos_por_verificacion": dict(Counter(
                f"{x['rechazada_por']['datos']}:{v}" for j in juicio for x in j.get("intentos", [])
                if x.get("rechazada_por") for v in x["rechazada_por"]["verificaciones"])),
            "efecto_declarado_contra_vf": dict(Counter(
                {True: "coincide", False: "contradice"}[x["efecto_previo_compatible"]] for j in juicio
                for x in j.get("intentos", []) if x.get("efecto_previo_compatible") is not None)),
            "diagnostico_distinto_de_la_sugerencia": sum(1 for j in juicio for x in j.get("intentos", [])
                                                         if x.get("diagnostico") and x["diagnostico"] != x["sugerida"]),
            "diagnosticos_en_el_objetivo": sum(1 for j in juicio for x in j.get("intentos", [])
                                               if x.get("diagnostico_en_objetivo")),
            "fugas": sum(len(x["fugas"]) for j in juicio for x in j.get("intentos", []))}


def markdown(res: dict) -> str:
    r = res["resumen"]
    out = [f"# {res['version']}: hipótesis → reparación → verificación", "",
           f"Reglas E0 `{res['huella_reglas']}` · Python del sandbox {res['python_sandbox']} · corrida: "
           f"**{res['corrida']}** · llamadas nuevas {res['llamadas_nuevas']} (total {res['llamadas_totales']}) · "
           f"datos de borde: {res['bordes']}", "",
           f"- Aceptadas por el coordinador: {r['aceptadas_por_el_coordinador']} · confirmaciones verdaderas (juez): "
           f"{r['confirmaciones_verdaderas']}",
           f"- **Confirmaciones falsas (juez): {len(r['confirmaciones_falsas'])}** {r['confirmaciones_falsas']} · de "
           f"ellas, solo visibles con los datos independientes: {r['falsas_que_solo_ven_los_datos_independientes']}",
           f"- Datos independientes del juez (creados y borrados en el juicio): {res['datos_independientes_del_juez']}",
           f"- Datos variados del coordinador: {res['datos_variados_del_coordinador']} · rechazos por verificación: "
           f"{r['rechazos_por_verificacion']}",
           f"- Resultados: {r['resultados']}",
           f"- Incompletos por el límite de llamadas (sin juzgar): {r['incompletos']}",
           f"- Rechazos por categoría: {r['rechazos']} · diagnósticos distintos de la sugerencia: "
           f"{r['diagnostico_distinto_de_la_sugerencia']} · diagnósticos que dan con la causa (según el juez): "
           f"{r['diagnosticos_en_el_objetivo']} · EFECTO declarado frente a lo que midió VF: "
           f"{r['efecto_declarado_contra_vf']}",
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
                      + (f" · juez: {'correcta' if it['correcta_segun_juez'] else 'incorrecta'} "
                         f"{it['correcta_por_datos']}" if "correcta_segun_juez" in it else "")
                      + (f" · FUGAS {it['fugas']}" if it["fugas"] else ""))
        for ln in j.get("pendientes", []):
            linea += f"\n  - hipótesis L{ln}: pendiente (no se pidió: límite de llamadas)"
        out.append(linea)
    return "\n".join(out) + "\n"
