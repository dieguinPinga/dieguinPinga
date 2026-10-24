"""Experimento 7: revisión AUTÓNOMA de una solución existente (OFICINA.md, «Experimento 7»).

Pregunta: ¿puede el revisor descubrir por su cuenta un defecto funcional con una hipótesis verificable, y puede el
programador corregirlo con ese diagnóstico? Es una modalidad aparte: `oficina programar` no cambia.

Recorrido (un coordinador, los mismos dos roles y el mismo modelo; nada nuevo):
0. PREPARACIÓN, sin inferencias: la solución inicial sale de un archivo Python existente (--codigo) o de la
   referencia de la misión, con un DEFECTO CONTROLADO opcional del catálogo DEFECTOS. Se le quitan los comentarios
   de línea completa. El coordinador la ejecuta en el sandbox contra la batería completa (también los casos ocultos)
   y exige que falle: si no hay un defecto funcional verificable, no se gasta ninguna inferencia. El nombre y la
   explicación del defecto, y el resultado de esa batería, quedan SOLO en el registro del laboratorio.
1. REVISOR (rol de oficina/revisor.py, pedido autónomo): recibe el contrato público, la firma, los ejemplos visibles
   y el código. Ningún resultado de pruebas. Devuelve una hipótesis causal y un caso sintético mínimo.
2. CONTRASTE (oficina/contraste.py): el coordinador valida el caso contra el contrato, descarta entradas iguales a
   un caso oculto y lo ejecuta en el sandbox con la referencia y con el candidato. Discrepancia observada ≠ causa
   demostrada. Si no hay discrepancia, el revisor puede intentar otra vez (max_revisiones), con una nota que dice
   solo lo que pasó con SU caso.
3. PROGRAMADOR: solo con una discrepancia demostrada. Recibe el contrato, los ejemplos visibles, el código, el
   diagnóstico y la evidencia del caso sintético. Nada de los casos ocultos. Hasta max_reparaciones intentos; entre
   intentos, solo la evidencia pertinente: su respuesta cortada o inválida, los ejemplos visibles y (experimento 7.1)
   el resultado CONCRETO de su intento en el caso sintético: salida real y salida del contrato clave por clave, el
   cambio de código respecto de su punto de partida, repeticiones (mismo programa o misma salida) y el historial.
4. VERIFICACIÓN INDEPENDIENTE: cuando un intento elimina la discrepancia y cumple los ejemplos visibles, el
   coordinador lo ejecuta UNA vez contra la batería completa. Su resultado nunca vuelve a los modelos.

Todo queda en <carpeta>/experimento-7/corrida-<fecha>/: solución inicial, defecto, cada pedido y respuesta, cada
contraste, cada reparación, la verificación, el consumo de inferencias, una auditoría de fugas y los cuatro
criterios de éxito.
"""
from __future__ import annotations

import ast
import difflib
import hashlib
import io
import json
import threading
import time
import tokenize
from dataclasses import dataclass
from pathlib import Path

from .. import codeextract, prompts
from ..config import ModelConfig
from ..ollama import OllamaError
from . import contraste as CT
from . import revisor as RV
from .autocorreccion import _ClienteConciso, sin_comentarios
from .misiones_programacion import MISIONES, VALIDADORES

VERSION_EXPERIMENTO = "experimento 7: revisión autónoma de una solución existente"

# Defectos CONTROLADOS: un reemplazo textual exacto sobre la referencia de cada misión. El nombre y la explicación
# nunca llegan a un modelo (lo comprueba la auditoría de fugas); el código defectuoso sí, porque es lo que se revisa.
DEFECTOS = {
    "conciliar_movimientos": {
        "anulaciones_no_registradas": {
            "buscar": '        aplicados[mid] = {"id": mid, "tipo": tipo, "producto": prod, "efecto": efecto}\n',
            "reemplazar": '        if tipo != "anulacion":\n'
                          '            aplicados[mid] = {"id": mid, "tipo": tipo, "producto": prod, "efecto": efecto}\n',
            "explicacion": "las anulaciones aplicadas no se registran como aplicadas: anular una anulación da "
                           "«anulacion_inexistente» en lugar de «anulacion_de_anulacion»"},
        "orden_solo_por_fecha": {
            "buscar": 'sorted(movimientos, key=lambda x: (x["fecha"], x["id"]))',
            "reemplazar": 'sorted(movimientos, key=lambda x: x["fecha"])',
            "explicacion": "dentro de un mismo día se respeta el orden de la lista en lugar del id"},
        "saldo_cero_rechazado": {
            "buscar": "if saldos.get(prod, 0) + efecto < 0:",
            "reemplazar": "if saldos.get(prod, 0) + efecto <= 0:",
            "explicacion": "un movimiento que deja el saldo exactamente en 0 se rechaza por saldo insuficiente"},
    },
    "resumen_ventas": {
        "hasta_excluido": {
            "buscar": "(hasta is not None and fecha > hasta)",
            "reemplazar": "(hasta is not None and fecha >= hasta)",
            "explicacion": "las ventas del día «hasta» quedan afuera"},
        "desempate_invertido": {
            "buscar": "return min(por, key=lambda x: (-por[x][k], x)) if por else None",
            "reemplazar": "return max(por, key=lambda x: (por[x][k], x)) if por else None",
            "explicacion": "en un empate gana el último producto en orden alfabético"},
    },
}

MAX_CARACTERES_DIAGNOSTICO = 2500
MARCAS_MINIMAS = 40      # la auditoría busca entradas y salidas ocultas de al menos este largo (las cortas, como
                         # «[[]]», aparecen por azar)


class ErrorExperimento(Exception):
    pass


@dataclass
class ConfigExperimento:
    programador: ModelConfig
    revisor: ModelConfig
    mision: str = "conciliar_movimientos"
    max_revisiones: int = 2
    max_reparaciones: int = 3
    max_segundos: float = 2400.0       # tiempo total de inferencia (revisor + programador)


def huella(codigo: str | None) -> str | None:
    return hashlib.sha256(codigo.encode()).hexdigest() if codigo is not None else None


def carpeta_de_corrida(carpeta: Path) -> Path:
    """Laboratorio aislado: nunca una carpeta de los experimentos anteriores, y una subcarpeta nueva por corrida."""
    carpeta = Path(carpeta)
    for previo in ("taller.db", "taller"):
        if (carpeta / previo).exists():
            raise ErrorExperimento(f"la carpeta {carpeta} tiene «{previo}» de un experimento anterior; el experimento "
                                   "7 usa una carpeta propia (por ejemplo …/oficina-revision-7)")
    base = carpeta / "experimento-7"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    nombre = time.strftime("corrida-%Y%m%d-%H%M%S")
    destino, i = base / nombre, 1
    while destino.exists():
        i += 1
        destino = base / f"{nombre}-{i}"
    destino.mkdir(mode=0o700)
    return destino


def aplicar_defecto(codigo: str, mision: str, nombre: str) -> tuple[str, dict]:
    d = DEFECTOS.get(mision, {}).get(nombre)
    if d is None:
        raise ErrorExperimento(f"defecto desconocido «{nombre}» para {mision}; existen: "
                               + (", ".join(DEFECTOS.get(mision, {})) or "ninguno"))
    if codigo.count(d["buscar"]) != 1:
        raise ErrorExperimento(f"el defecto «{nombre}» no se puede aplicar: el fragmento a reemplazar tiene que "
                               "aparecer exactamente una vez en el código inicial")
    return codigo.replace(d["buscar"], d["reemplazar"]), {"id": nombre, "explicacion": d["explicacion"]}


def _bateria(executor, codigo: str, mision: dict) -> dict:
    res = executor.run(codigo, mision["spec"], mision["tests_py"])
    r = res.result or {}
    if res.status != "ok":
        return {"ejecucion": res.status, "aprobados": None, "total": None, "ok": False, "detalle": res.reason}
    if r.get("load_error"):
        return {"ejecucion": "error_de_carga", "aprobados": 0, "total": len(mision["spec"]["cases"]), "ok": False,
                "detalle": r["load_error"][-300:]}
    return {"ejecucion": "completa", "aprobados": r.get("passed"), "total": r.get("total"),
            "ok": bool(r.get("total")) and r.get("failed") == 0 and r.get("passed") == r.get("total"),
            "fallidos": sorted(c["n"] for c in r.get("cases", []) if not c.get("ok"))}


def preparar(executor, mision: str, codigo: str | None = None, defecto: str | None = None) -> dict:
    """SIN inferencias. Arma la solución inicial y comprueba que tenga un defecto funcional verificable."""
    if mision not in MISIONES or mision not in VALIDADORES:
        raise ErrorExperimento(f"misión desconocida o sin validador de casos sintéticos: «{mision}»")
    fabrica, referencia = MISIONES[mision]
    m = fabrica()
    original = codigo if codigo is not None else referencia
    inicial, info = (aplicar_defecto(original, mision, defecto) if defecto else (original, None))
    inicial = sin_comentarios(inicial)
    problemas = codeextract.validate_code(inicial, m["spec"])
    prep = {"mision": mision, "origen": "archivo" if codigo is not None else "referencia de la misión",
            "huella_original": huella(original), "defecto": info, "codigo_inicial": inicial,
            "huella_inicial": huella(inicial), "problemas": problemas,
            "referencia": _bateria(executor, referencia, m)}
    prep["bateria_inicial"] = None if problemas else _bateria(executor, inicial, m)
    b = prep["bateria_inicial"]
    if not prep["referencia"]["ok"]:
        motivo = "la referencia de la misión no aprueba su propia batería"
    elif problemas:
        motivo = "la solución inicial no pasa la validación estática: " + "; ".join(problemas)
    elif b["ejecucion"] != "completa":
        motivo = (f"la batería sobre la solución inicial terminó «{b['ejecucion']}» ({b.get('detalle')}): un "
                  "programa que no carga o no termina no es un defecto funcional")
    elif b["ok"]:
        motivo = "la solución inicial aprueba toda la batería: no tiene un defecto funcional verificable"
    else:
        motivo = (f"la solución inicial falla la batería independiente ({b['aprobados']}/{b['total']}): tiene un "
                  "defecto funcional verificable")
    prep["defecto_verificable"] = bool(prep["referencia"]["ok"] and b is not None and b["ejecucion"] == "completa"
                                       and not b["ok"])
    prep["motivo"] = motivo
    return prep


def mensajes_reparacion(descripcion: str, spec: dict, codigo: str, diagnostico: str, evidencia: str, aviso: str,
                        budget: int, historial: list[str] | None = None, base_n: int = 0) -> list[dict]:
    """Pedido al PROGRAMADOR: el contrato, los ejemplos visibles, el código, el diagnóstico y la evidencia del caso
    sintético (en un reintento, el resultado concreto de su último intento y el historial). Ningún resultado de la
    batería."""
    origen = "la versión inicial" if not base_n else f"tu intento {base_n}"
    usuario = (f"Tarea:\n{prompts.clip(descripcion, budget // 4)}\n\n{prompts._sig(spec)}\n\n"
               f"Ejemplos:\n{prompts._examples(spec, budget // 6) or '(sin ejemplos visibles)'}\n\n"
               f"Versión actual de solucion.py ({origen}):\n```python\n{prompts.clip(codigo, budget // 3)}```\n\n"
               "Un revisor de código examinó la versión inicial y el coordinador comprobó su caso en un entorno "
               "aislado. No hay otros resultados de pruebas.\n\n"
               f"{diagnostico}\n\n{evidencia}\n\n"
               + ("TUS REPARACIONES ANTERIORES (todas en el caso del revisor; no repitas un cambio que ya probaste):\n"
                  + "\n".join(f"- {h}" for h in historial) + "\n\n" if historial else "")
               + (f"AVISO DEL COORDINADOR:\n{aviso}\n\n" if aviso else "")
               + "Corregí el programa y devolvé el archivo completo en un único bloque ```python.")
    return [{"role": "system", "content": prompts.BUILDER_SYSTEM}, {"role": "user", "content": usuario}]


# Experimento 7.1: retroalimentación ESPECÍFICA entre intentos del programador ----------------------------------------
# Cuando una reparación se ejecuta pero no elimina la discrepancia, el programador recibe el resultado concreto de SU
# intento en el caso sintético (salida real y salida que pide el contrato, clave por clave), el cambio de código que
# hizo respecto de su punto de partida y si repitió un programa o una salida ya probados. Todo sale del caso
# sintético (público) y del código del propio programador: nunca de la batería oculta.

MAX_JSON_RETRO = 700
MAX_VALOR_CLAVE = 300
MAX_DIFF = 1600


def _json(v, limite: int = MAX_JSON_RETRO) -> str:
    return prompts.clip(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False), limite)


def programa(codigo: str | None) -> str | None:
    """Huella del PROGRAMA (árbol sintáctico): iguala versiones que solo difieren en espacios o comentarios."""
    if codigo is None:
        return None
    try:
        return hashlib.sha256(ast.dump(ast.parse(codigo)).encode()).hexdigest()
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return huella(codigo)


def comparar_salidas(esperado, obtenido) -> dict:
    """Qué claves de la salida siguen distintas y, para listas, qué elementos faltan o sobran."""
    if not isinstance(esperado, dict) or not isinstance(obtenido, dict):
        return {"comparable": False, "distintas": [], "coinciden": [], "detalle": {}}
    distintas = [k for k in esperado if k not in obtenido or obtenido[k] != esperado[k]]
    sobran = [k for k in obtenido if k not in esperado]
    detalle = {}
    for k in distintas:
        e, o = esperado[k], obtenido.get(k, "<falta la clave>")
        d = {"contrato": e, "tu_intento": o}
        if isinstance(e, list) and isinstance(o, list):
            d["faltan"] = [x for x in e if x not in o]
            d["sobran"] = [x for x in o if x not in e]
        elif isinstance(e, dict) and isinstance(o, dict):
            d["subclaves_distintas"] = sorted({str(s) for s in set(e) | set(o) if e.get(s) != o.get(s)})
        detalle[k] = d
    return {"comparable": True, "distintas": distintas + sobran, "coinciden": [k for k in esperado
                                                                               if k not in distintas],
            "sobran_claves": sobran, "detalle": detalle}


def _lineas_de_codigo(codigo: str) -> list[str]:
    """Las líneas tal como las escribió el programador, sin comentarios, sin espacios finales ni líneas vacías."""
    cortes = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(codigo).readline):
            if tok.type == tokenize.COMMENT:
                cortes[tok.start[0]] = tok.start[1]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    lineas = [ln[:cortes.get(i, len(ln))].rstrip() for i, ln in enumerate(codigo.splitlines(), 1)]
    return [ln for ln in lineas if ln.strip()]


def cambio_de_codigo(anterior: str, nuevo: str) -> dict:
    """Diferencia unificada entre el punto de partida y el intento, sin comentarios ni líneas vacías. Si los dos son
    el mismo programa (mismo árbol sintáctico), no hay cambios."""
    if programa(anterior) == programa(nuevo):
        diff = []
    else:
        diff = list(difflib.unified_diff(_lineas_de_codigo(anterior), _lineas_de_codigo(nuevo), "version_anterior.py",
                                         "tu_intento.py", n=1, lineterm=""))
    agregadas = sum(1 for x in diff if x.startswith("+") and not x.startswith("+++"))
    quitadas = sum(1 for x in diff if x.startswith("-") and not x.startswith("---"))
    return {"lineas_agregadas": agregadas, "lineas_quitadas": quitadas,
            "diff": prompts.clip("\n".join(diff), MAX_DIFF), "sin_cambios": not diff}


def _nombre(n: int) -> str:
    return "la versión inicial" if n == 0 else f"tu intento {n}"


def texto_retroalimentacion(n: int, base_n: int, funcion: str, caso: dict, esperado, cand: dict, comp: dict,
                            cambio: dict, mismo_programa: list[int], misma_salida: list[int],
                            discrepancia: str) -> str:
    """El resultado CONCRETO del intento n en el caso sintético. Describe lo observado; no propone la solución."""
    l = [f"════ RESULTADO DE TU INTENTO {n} EN EL CASO DEL REVISOR (caso sintético; NO es una prueba oculta) ════",
         f"Caso: {funcion}({_llamada(caso)})",
         f"Salida que pide el contrato: {_json(esperado)}"]
    if cand.get("ejecucion") == "completa" and not cand.get("salida_recortada"):
        l.append(f"Salida de tu intento {n}: {_json(cand['salida'])}")
    else:
        l.append(f"Tu intento {n} no devolvió una salida comparable: {cand.get('ejecucion')}"
                 + (f" — {_json(cand.get('salida') or cand.get('detalle'), 400)}"
                    if cand.get("salida") or cand.get("detalle") else ""))
    if discrepancia == "eliminada":
        l.append("RESULTADO: en este caso tu intento ya devuelve lo que pide el contrato.")
    elif comp["comparable"]:
        l.append("RESULTADO: SIGUE DISTINTA en " + ", ".join(f"'{k}'" for k in comp["distintas"])
                 + (". Ya coinciden: " + ", ".join(f"'{k}'" for k in comp["coinciden"]) if comp["coinciden"] else "")
                 + ".")
        for k in comp["distintas"]:
            d = comp["detalle"].get(k)
            if d is None:
                l.append(f"  - '{k}': tu intento devuelve esta clave y el contrato no la pide")
            elif "faltan" in d:
                l.append(f"  - '{k}': falta {_json(d['faltan'], MAX_VALOR_CLAVE)} · sobra "
                         f"{_json(d['sobran'], MAX_VALOR_CLAVE)}")
            elif "subclaves_distintas" in d:
                l.append(f"  - '{k}': difieren {', '.join(repr(s) for s in d['subclaves_distintas'][:8])} · contrato "
                         f"{_json(d['contrato'], MAX_VALOR_CLAVE)} · tu intento {_json(d['tu_intento'], MAX_VALOR_CLAVE)}")
            else:
                l.append(f"  - '{k}': contrato {_json(d['contrato'], MAX_VALOR_CLAVE)} · tu intento "
                         f"{_json(d['tu_intento'], MAX_VALOR_CLAVE)}")
    if mismo_programa:
        l.append(f"REPETICIÓN: tu intento {n} es el MISMO programa que "
                 + " y ".join(_nombre(k) for k in mismo_programa)
                 + " (solo cambian espacios o comentarios). Repetirlo no aporta información nueva.")
    elif misma_salida and discrepancia != "eliminada":
        l.append(f"SIN EFECTO EN ESTE CASO: tu intento {n} devuelve exactamente la misma salida que "
                 + " y ".join(_nombre(k) for k in misma_salida)
                 + ": el código que cambiaste no modifica el resultado para esta entrada.")
    if cambio["sin_cambios"]:
        l.append(f"QUÉ CAMBIASTE respecto de {_nombre(base_n)}: nada (sin contar comentarios).")
    else:
        l.append(f"QUÉ CAMBIASTE respecto de {_nombre(base_n)} (+{cambio['lineas_agregadas']} / "
                 f"-{cambio['lineas_quitadas']} líneas, sin comentarios):\n{cambio['diff']}")
    if discrepancia != "eliminada":
        l.append("Para evaluarlo: seguí esta entrada paso a paso por tu código y fijate si las líneas que cambiaste "
                 "llegan a ejecutarse y qué valor dejan en las claves que siguen distintas.")
    l.append("════ FIN DEL RESULTADO ════")
    return "\n".join(l)


def linea_de_historial(n: int, base_n: int, comp: dict | None, cambio: dict | None, mismo_programa: list[int],
                       misma_salida: list[int], discrepancia: str | None, problema: str | None = None) -> str:
    if problema:
        return f"intento {n}: no se pudo ejecutar ({problema[:160]})"
    partes = [f"intento {n} (desde {_nombre(base_n)}): "
              + ("sin cambios de código" if cambio["sin_cambios"] else
                 f"+{cambio['lineas_agregadas']}/-{cambio['lineas_quitadas']} líneas")]
    if discrepancia == "eliminada":
        partes.append("el caso ya da lo que pide el contrato")
    elif comp and comp["comparable"]:
        partes.append("el caso sigue distinto en " + ", ".join(f"'{k}'" for k in comp["distintas"]))
    else:
        partes.append("el caso no dio una salida comparable")
    if mismo_programa:
        partes.append("mismo programa que " + " y ".join(_nombre(k) for k in mismo_programa))
    elif misma_salida and discrepancia != "eliminada":
        partes.append("misma salida que " + " y ".join(_nombre(k) for k in misma_salida))
    return "; ".join(partes)


def _llamada(caso: dict) -> str:
    return ", ".join([json.dumps(a, ensure_ascii=False) for a in caso["args"]]
                     + [f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in caso["kwargs"].items()])


def nota_para_revisor(rev: dict) -> str:
    """Lo que el revisor puede saber de su intento anterior: solo lo que pasó con SU caso, nunca datos ocultos."""
    if rev["estado"] != "hecha":
        return (f"Tu revisión anterior no se pudo usar ({rev.get('motivo')}). Respondé con el JSON completo, "
                "incluido el caso sintético.")
    ct = rev["contraste"]
    if ct["discrepancia"] == "no_observada":
        return (f"Tu caso anterior ({_llamada(ct['caso'])}) no mostró ninguna discrepancia: para esa entrada el "
                "código devuelve lo que pide el contrato. Tu hipótesis no tiene respaldo ahí: revisá otra regla del "
                "contrato y proponé otro caso.")
    if ct["referencia"] is None and ct["validez"]["valido"]:
        motivo = "elegí otra entrada"                 # coincide con un caso oculto: no se dice
    else:
        motivo = ct["validez"]["motivo"] or "no dio una salida comparable"
    return f"Tu caso anterior no se pudo usar: {motivo}. Proponé otro caso mínimo que respete el contrato."


def _textos(mensajes: list[list[dict]]) -> list[str]:
    return [m["content"] for msgs in mensajes for m in msgs]


def auditar(mensajes: list[list[dict]], spec: dict, defecto: dict | None, descripcion: str = "") -> dict:
    """Comprueba que ningún pedido a un modelo contenga el defecto (nombre o explicación) ni datos de los casos
    ocultos: entradas, salidas esperadas o la categoría que ejercitan. Un dato oculto que también es público (por
    ejemplo, la misma salida que un ejemplo visible) no cuenta como fuga."""
    textos = _textos(mensajes)
    hallazgos = []
    marcas = []
    publico = [descripcion] + [json.dumps(c.get(k), ensure_ascii=False, separators=sep)
                               for c in spec["cases"] if c.get("visible", True) for k in ("args", "expect")
                               for sep in ((", ", ": "), (",", ":"))]
    if defecto:
        marcas += [("nombre del defecto", defecto["id"]), ("explicación del defecto", defecto["explicacion"])]
    for i, c in enumerate(spec["cases"], 1):
        if c.get("visible", True):
            continue
        for etiqueta, valor in (("entrada", c.get("args")), ("salida esperada", c.get("expect"))):
            for sep in ((", ", ": "), (",", ":")):
                s = json.dumps(valor, ensure_ascii=False, separators=sep)
                if len(s) >= MARCAS_MINIMAS and not any(s in x for x in publico):
                    marcas.append((f"{etiqueta} del caso oculto {i}", s))
        if c.get("categoria"):
            marcas.append((f"categoría del caso oculto {i}", c["categoria"]))
    for etiqueta, marca in marcas:
        if any(marca in t for t in textos):
            hallazgos.append(etiqueta)
    return {"pedidos_revisados": len(mensajes), "marcas_buscadas": len(marcas), "hallazgos": sorted(set(hallazgos)),
            "sin_fugas": not hallazgos}


class Experimento:
    def __init__(self, destino: Path, executor, cliente, gate, cfg: ConfigExperimento, log=print):
        self.dir, self.executor, self.gate, self.cfg, self.log = Path(destino), executor, gate, cfg, log
        self.cliente = cliente                                   # el revisor: sin reglas de concisión
        self.cliente_programador = _ClienteConciso(cliente, cfg.programador.num_predict)
        fabrica, referencia = MISIONES[cfg.mision]
        self.mision = fabrica()
        self.spec, self.descripcion = self.mision["spec"], self.mision["description"]
        self.contraste = CT.desde_mision(fabrica, referencia, VALIDADORES[cfg.mision])
        self.llamadas: list[dict] = []
        self.pedidos: list[list[dict]] = []

    # Registro ---------------------------------------------------------------------------------------------------
    def _guardar(self, rel: str, contenido) -> None:
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(contenido if isinstance(contenido, str) else
                     json.dumps(contenido, ensure_ascii=False, indent=2, default=str))

    def _segundos(self) -> float:
        return sum(ll["segundos"] or 0 for ll in self.llamadas)

    def _sin_tiempo(self) -> str | None:
        usados = self._segundos()
        return (f"límite de tiempo de inferencia ({usados:.0f} s de {self.cfg.max_segundos:.0f} s)"
                if usados >= self.cfg.max_segundos else None)

    # Roles ------------------------------------------------------------------------------------------------------
    def _revisar(self, k: int, codigo: str, nota: str, cancel) -> dict:
        msgs = RV.mensajes_autonomos(self.descripcion, self.spec, codigo, nota)
        self.pedidos.append(msgs)
        cfg = RV.ConfigRevisor(self.cfg.revisor, hipotesis_verificables=True)
        r = RV.revisar(self.cliente, self.gate, cfg, self.descripcion, self.spec, codigo, "", cancel, msgs=msgs)
        self.llamadas.append({"rol": "revisor", "revision": k, "modelo": self.cfg.revisor.name,
                              "segundos": r.get("segundos"), "tokens_prompt": r.get("tokens_prompt"),
                              "tokens_generados": r.get("tokens_generados"), "fin": r.get("fin"),
                              "cortada": r.get("cortada"), "error": None if "respuesta" in r else r.get("motivo")})
        rev = {"revision": k, "nota_del_coordinador": nota or None, "estado": "hecha" if r["ok"] else "fallida",
               "motivo": r.get("motivo"), "diagnostico": r.get("diagnostico")}
        self._guardar(f"revision-{k}/pedido.json", msgs)
        if r.get("respuesta") is not None:
            self._guardar(f"revision-{k}/respuesta.txt", r["respuesta"])
        if r["ok"]:
            caso, motivo = CT.leer_caso(r["diagnostico"].get("caso_sintetico"))
            rev["contraste"] = CT.contrastar(self.executor, self.contraste, codigo, caso, motivo)
            rev["contraste"]["hipotesis"] = r["diagnostico"]["principal"]["propiedad_a_cambiar"]
        self._guardar(f"revision-{k}/revision.json", rev)
        ct = rev.get("contraste")
        self.log(f"  revisión {k}: {rev['estado']}" + (f" · {rev['motivo']}" if rev.get("motivo") else "")
                 + (f" · discrepancia {ct['discrepancia']} · hipótesis causal {ct['hipotesis_causal']} · {ct['motivo']}"
                    if ct else ""))
        return rev

    def _programar(self, n: int, msgs: list[dict], cancel):
        self.pedidos.append(msgs)
        self._guardar(f"reparacion-{n}/pedido.json", msgs)
        t0 = time.monotonic()
        try:
            self.gate.acquire("programador", cancel)
            try:
                gen = self.cliente_programador.chat(self.cfg.programador, msgs, cancel)
            finally:
                self.gate.release()
        except (OllamaError, OSError) as e:
            self.llamadas.append({"rol": "programador", "reparacion": n, "modelo": self.cfg.programador.name,
                                  "segundos": round(time.monotonic() - t0, 1), "tokens_prompt": None,
                                  "tokens_generados": None, "fin": None, "cortada": None, "error": str(e)})
            return None, str(e)
        self.llamadas.append({"rol": "programador", "reparacion": n, "modelo": self.cfg.programador.name,
                              "segundos": round(time.monotonic() - t0, 1), "tokens_prompt": gen.prompt_tokens,
                              "tokens_generados": gen.eval_tokens, "fin": gen.done_reason,
                              "cortada": bool(gen.truncated), "error": None})
        self._guardar(f"reparacion-{n}/respuesta.md", gen.text)
        return gen, None

    def _visibles(self, codigo: str) -> dict:
        spec = dict(self.spec, cases=[c for c in self.spec["cases"] if c.get("visible", True)])
        res = self.executor.run(codigo, spec, "")
        r = res.result or {}
        ok = res.status == "ok" and not r.get("load_error") and bool(r.get("total")) and r.get("failed") == 0
        informe = "" if ok else (prompts.failure_report(r, [], 2000) if res.status == "ok" else
                                 f"La ejecución terminó «{res.status}»: {res.reason}")
        return {"ok": ok, "ejecutable": res.status == "ok" and not r.get("load_error"),
                "aprobados": r.get("passed"), "total": r.get("total"), "informe": informe}

    # Recorrido --------------------------------------------------------------------------------------------------
    def correr(self, prep: dict, cancel: threading.Event | None = None) -> dict:
        cancel = cancel or threading.Event()
        inicial = prep["codigo_inicial"]
        self._guardar("inicial/solucion.py", inicial)
        self._guardar("inicial/preparacion.json", {k: v for k, v in prep.items() if k != "codigo_inicial"})
        inf = {"experimento": VERSION_EXPERIMENTO, "mision": self.cfg.mision, "inicio": time.time(),
               "limites": {"max_revisiones": self.cfg.max_revisiones, "max_reparaciones": self.cfg.max_reparaciones,
                           "max_segundos": self.cfg.max_segundos, "num_predict_programador": self.cfg.programador.num_predict,
                           "num_predict_revisor": self.cfg.revisor.num_predict},
               "inicial": {"origen": prep["origen"], "huella": prep["huella_inicial"], "defecto": prep["defecto"],
                           "bateria": prep["bateria_inicial"], "defecto_verificable": prep["defecto_verificable"]},
               "revisiones": [], "reparaciones": [], "verificacion": None, "detenido": None}
        if not prep["defecto_verificable"]:
            inf["detenido"] = prep["motivo"]
            return self._cerrar(inf, prep)

        # 1-2. Revisor y contraste ---------------------------------------------------------------------------------
        demostrada, nota = None, ""
        for k in range(1, self.cfg.max_revisiones + 1):
            if (motivo := self._sin_tiempo()):
                inf["detenido"] = motivo
                break
            rev = self._revisar(k, inicial, nota, cancel)
            inf["revisiones"].append(rev)
            if rev["estado"] == "hecha" and rev["contraste"]["discrepancia"] == "demostrada":
                demostrada = rev
                break
            nota = nota_para_revisor(rev)
        if demostrada is None:
            inf["detenido"] = inf["detenido"] or (f"el revisor no demostró ninguna discrepancia en "
                                                  f"{len(inf['revisiones'])} revisión(es)")
            return self._cerrar(inf, prep)

        # 3. Programador con el diagnóstico y la evidencia ---------------------------------------------------------
        ct = demostrada["contraste"]
        propiedad = demostrada["diagnostico"]["principal"]["propiedad_a_cambiar"]
        diagnostico = RV.texto_para_programador(demostrada["diagnostico"], MAX_CARACTERES_DIAGNOSTICO, "demostrada")
        evidencia = CT.texto_para_programador(ct, self.contraste.funcion, propiedad)
        budget = prompts.budget_chars(self.cfg.programador.num_ctx, self.cfg.programador.num_predict)
        base, base_n, aviso, candidato = inicial, 0, "", None
        esperado = ct["referencia"]["salida"]
        # Versiones ya probadas (la inicial es la 0): su programa y su salida en el caso sintético.
        versiones = [{"n": 0, "programa": programa(inicial), "salida": ct["candidato"]["salida"]}]
        historial: list[str] = []
        avisado_de_repeticion = False
        for n in range(1, self.cfg.max_reparaciones + 1):
            if (motivo := self._sin_tiempo()):
                inf["detenido"] = motivo
                break
            msgs = mensajes_reparacion(self.descripcion, self.spec, base, diagnostico, evidencia, aviso, budget,
                                       historial, base_n)
            gen, error = self._programar(n, msgs, cancel)
            rep = {"intento": n, "base": base_n}
            inf["reparaciones"].append(rep)
            if gen is None:
                rep.update(estado="sin_respuesta", detalle=error)
                if "contactar" in error or "no disponible" in error:
                    inf["detenido"] = f"el trabajador no responde: {error}"
                    break
                aviso = "La generación anterior falló. Respondé con el archivo completo, más corto."
                continue
            codigo, nota_ext = codeextract.extract_code(gen.text)
            problemas = [nota_ext] if codigo is None else codeextract.validate_code(codigo, self.spec)
            if gen.truncated:
                problemas.insert(0, f"Tu respuesta se cortó al llegar al límite de {self.cfg.programador.num_predict} "
                                    "tokens y quedó incompleta: escribí una versión más corta, sin comentarios.")
            if codigo is not None:
                self._guardar(f"reparacion-{n}/solucion.py", codigo)
            rep.update(huella=huella(codigo), cortada=bool(gen.truncated),
                       modifico=codigo is not None and sin_comentarios(codigo) != sin_comentarios(base))
            if problemas:
                rep.update(estado="no_ejecutable", detalle="; ".join(problemas)[:500])
                aviso = f"Tu intento {n} no se pudo usar:\n" + "\n".join(problemas)
                historial.append(linea_de_historial(n, base_n, None, None, [], [], None, problemas[0]))
                self.log(f"  reparación {n}: no ejecutable · {rep['detalle'][:160]}")
                self._guardar(f"reparacion-{n}/reparacion.json", rep)
                continue
            otra = CT.contrastar(self.executor, self.contraste, codigo, ct["caso"], None)
            vis = self._visibles(codigo)
            disc = {"no_observada": "eliminada", "demostrada": "persiste"}.get(otra["discrepancia"], "inconclusa")
            cand = otra["candidato"] or {"ejecucion": otra["discrepancia"], "salida": None, "detalle": otra["motivo"]}
            salida = cand.get("salida") if cand.get("ejecucion") == "completa" else None
            comp = comparar_salidas(esperado, salida)
            cambio = cambio_de_codigo(base, codigo)
            prog = programa(codigo)
            mismo_programa = [v["n"] for v in versiones if v["programa"] == prog]
            misma_salida = [v["n"] for v in versiones if salida is not None and v["salida"] == salida]
            retro = texto_retroalimentacion(n, base_n, self.contraste.funcion, ct["caso"], esperado, cand, comp,
                                            cambio, mismo_programa, misma_salida, disc)
            rep.update(estado="ejecutada", contraste=otra, visibles={k: vis[k] for k in ("ok", "aprobados", "total")},
                       discrepancia_sintetica=disc, claves_distintas=comp["distintas"],
                       cambio={k: v for k, v in cambio.items() if k != "diff"}, mismo_programa_que=mismo_programa,
                       misma_salida_que=misma_salida)
            self._guardar(f"reparacion-{n}/reparacion.json", rep)
            self._guardar(f"reparacion-{n}/retroalimentacion.md", retro)
            self.log(f"  reparación {n}: modificó {'sí' if rep['modifico'] else 'no'} · discrepancia sintética "
                     f"{disc} · claves distintas {comp['distintas']} · ejemplos visibles "
                     f"{vis['aprobados']}/{vis['total']}"
                     + (f" · mismo programa que {mismo_programa}" if mismo_programa else "")
                     + (f" · misma salida que {misma_salida}" if misma_salida else ""))
            if disc == "eliminada" and vis["ok"]:
                candidato = (n, codigo)
                break
            historial.append(linea_de_historial(n, base_n, comp, cambio, mismo_programa, misma_salida, disc))
            if mismo_programa and avisado_de_repeticion:
                # Ya se le había mostrado una repetición y volvió a repetir: otro pedido no traería información nueva.
                inf["detenido"] = (f"el intento {n} repitió un programa ya probado después del aviso de repetición: "
                                   "no se gastan más inferencias sin información nueva")
                break
            avisado_de_repeticion = avisado_de_repeticion or bool(mismo_programa)
            # Lo que se le muestra ahora es el resultado de SU intento (reemplaza la evidencia de la versión inicial).
            evidencia = retro
            aviso = "" if vis["ok"] else ("Tu intento " + str(n) + " no cumple los ejemplos visibles del contrato:\n"
                                          + vis["informe"])
            if vis["ejecutable"]:
                if not mismo_programa:
                    versiones.append({"n": n, "programa": prog, "salida": salida})
                base, base_n = codigo, n

        # 4. Verificación independiente ----------------------------------------------------------------------------
        if candidato is not None:
            n, codigo = candidato
            self._guardar("final/solucion.py", codigo)
            inf["verificacion"] = dict(_bateria(self.executor, codigo, self.mision), intento=n, huella=huella(codigo))
        elif base_n:
            # Informativa: la última versión ejecutable, aunque no haya eliminado la discrepancia.
            inf["verificacion_informativa"] = dict(_bateria(self.executor, base, self.mision), intento=base_n)
            inf["detenido"] = inf["detenido"] or "ningún intento eliminó la discrepancia cumpliendo los ejemplos visibles"
        else:
            inf["detenido"] = inf["detenido"] or "ningún intento del programador se pudo ejecutar"
        return self._cerrar(inf, prep)

    def _cerrar(self, inf: dict, prep: dict) -> dict:
        reps, ver = inf["reparaciones"], inf.get("verificacion")
        demostradas = [r for r in inf["revisiones"] if r["estado"] == "hecha"
                       and r["contraste"]["discrepancia"] == "demostrada"]
        final = next((r for r in reps if ver and r["intento"] == ver["intento"]), None)
        inf["criterios"] = {
            "1_defecto_funcional_verificable": prep["defecto_verificable"],
            "2_caso_reproduce_discrepancia_real": bool(demostradas),
            "3_programador_modifico_la_solucion": any(r.get("modifico") for r in reps),
            "4_discrepancia_eliminada_y_verificacion_independiente": bool(
                final and final.get("discrepancia_sintetica") == "eliminada" and ver["ok"]),
        }
        inf["auditoria_de_fugas"] = auditar(self.pedidos, self.spec, prep["defecto"], self.descripcion)
        inf["exito"] = all(inf["criterios"].values()) and inf["auditoria_de_fugas"]["sin_fugas"]
        por_rol = {}
        for ll in self.llamadas:
            r = por_rol.setdefault(ll["rol"], {"llamadas": 0, "segundos": 0.0, "tokens_prompt": 0,
                                               "tokens_generados": 0, "cortadas": 0, "errores": 0})
            r["llamadas"] += 1
            r["segundos"] = round(r["segundos"] + (ll["segundos"] or 0), 1)
            r["tokens_prompt"] += ll["tokens_prompt"] or 0
            r["tokens_generados"] += ll["tokens_generados"] or 0
            r["cortadas"] += bool(ll["cortada"])
            r["errores"] += bool(ll["error"])
        inf["consumo"] = {"llamadas": self.llamadas, "por_rol": por_rol, "total_llamadas": len(self.llamadas),
                          "segundos": round(self._segundos(), 1)}
        inf["fin"] = time.time()
        self._guardar("informe.json", inf)
        return inf
