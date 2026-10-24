"""Experimento 7.5: capacidad real de REPARACIÓN con la causa explícita (OFICINA.md, «Experimento 7.5»).

Pregunta: ¿el programador (qwen2.5-coder:7b) produce un parche mínimo y correcto cuando se le DA la causa? Experimento
aislado que reutiliza piezas existentes, sin tocar el coordinador ni `revisar-existente`:
- las fuentes de una corrida 7.3 terminada, en solo lectura (sonda_comprension.fuentes): código defectuoso, zonas
  autorizadas del primer intento (27–37 y 41–46 en 7.3c) y el caso sintético demostrado por el revisor;
- el formato, la validación por coincidencia exacta y la aplicación controlada de parches (parches.*);
- el contraste en el sandbox (contraste.contrastar), la retroalimentación del 7.1 y la batería independiente.

Recorrido, a lo sumo MAX_INTENTOS intentos, cada uno sobre el MISMO código defectuoso y las MISMAS zonas:
1. pedido: contrato, ejemplos visibles, las zonas, la CAUSA EXPLÍCITA (CAUSAS, escrita por el equipo) y, desde el
   segundo intento, lo que pasó con su parche anterior (rechazo, o su resultado en el caso sintético y los ejemplos
   visibles). Solo un parche JSON; nunca el programa completo. Auditoría de fugas antes de enviar.
2. el parche se valida y se aplica sobre una copia; si se rechaza, se registra el motivo;
3. si se aplica: caso sintético y ejemplos visibles; si los dos dan bien, la batería independiente completa, UNA
   vez. Éxito = parche aplicado, programa distinto del defectuoso y la batería completa aprobada. Si la batería
   falla, o se agotan los intentos, se registra por qué y se detiene.

Experimento 7.6 (`normalizar=True`, `--normalizar-sangria`): si un cambio del parche no coincide LITERALMENTE con el
código, se intenta una normalización limitada a la SANGRÍA INICIAL (`normalizar_sangria`). Es estrictamente de
formato: el contenido de cada línea (después de la sangría) tiene que ser idéntico, la sangría relativa entre las
líneas del fragmento tiene que coincidir, el fragmento tiene que aparecer UNA sola vez en todo el código y dentro de
las zonas autorizadas, y el reemplazo se desplaza la misma cantidad de espacios sin tocar su contenido. Ante
cualquier ambigüedad (tabulaciones, varias apariciones, sangría del reemplazo distinta de la del original, sangría
negativa) se rechaza. El parche normalizado pasa después por la misma validación y aplicación de siempre.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .. import prompts
from ..ollama import OllamaError
from . import contraste as CT
from . import parches as PA
from . import revision_existente as RE
from . import sonda_comprension as SC
from .misiones_programacion import MISIONES, VALIDADORES

VERSION_REPARACION = "experimento 7.5: reparación guiada con la causa explícita (parche mínimo)"
VERSION_NORMALIZACION = ("experimento 7.6: reparación guiada con la causa explícita y normalización segura de la "
                         "sangría (parche mínimo)")
MAX_INTENTOS = 3

# La causa, escrita por el equipo para cada defecto controlado (nunca la referencia ni datos de la batería). Las
# «lineas_clave» tienen que estar dentro de las zonas autorizadas de la corrida de origen.
CAUSAS = {
    ("conciliar_movimientos", "anulaciones_no_registradas"): {
        "texto": ("Las anulaciones válidas no quedan registradas en `aplicados`: la escritura "
                  "`aplicados[mid] = {...}` está condicionada por `if tipo != \"anulacion\":`. Por eso, cuando una "
                  "anulación referencia (con 'anula') a otra anulación que sí se aplicó, `aplicados.get(...)` devuelve "
                  "None y se ignora como 'anulacion_inexistente', en lugar de llegar a la rama "
                  "`ref[\"tipo\"] == \"anulacion\"` ('anulacion_de_anulacion')."),
        "lineas_clave": ['if tipo != "anulacion":', 'ref = aplicados.get(m.get("anula"))'],
    },
}
TITULO_CAUSA = "CAUSA DEL DEFECTO (confirmada por el equipo; no hace falta volver a diagnosticarla)"
PEDIDO = ("Proponé el parche mínimo que corrige esa causa y respondé solo con el JSON pedido. No escribas el programa "
          "completo.")


class ErrorReparacion(Exception):
    pass


def preparar(corrida: Path) -> dict:
    """Fuentes (solo lectura), la causa explícita y el caso sintético demostrado de la corrida de origen."""
    try:
        f = SC.fuentes(Path(corrida))
    except SC.ErrorSonda as e:
        raise ErrorReparacion(str(e)) from None
    clave = (f["mision"], (f["defecto"] or {}).get("id"))
    if clave not in CAUSAS:
        raise ErrorReparacion(f"no hay una causa explícita escrita para {clave}")
    causa = CAUSAS[clave]
    for linea in causa["lineas_clave"]:
        ls = PA.lineas_de_cita(f["codigo"], linea)
        if not ls or not all(any(a <= x <= b for a, b in f["zonas"]) for x in ls):
            raise ErrorReparacion(f"la línea clave «{linea}» no está dentro de las zonas autorizadas {f['zonas']}")
    rev = json.loads((Path(corrida) / f"revision-{f['revision']}" / "revision.json").read_text())
    f["caso"] = rev["contraste"]["caso"]
    f["causa"] = causa["texto"]
    return f


def mensajes(f: dict, aviso: str = "") -> list[dict]:
    fabrica, _ = MISIONES[f["mision"]]
    m = fabrica()
    spec = m["spec"]
    usuario = (f"Tarea:\n{m['description']}\n\n{prompts._sig(spec)}\n\n"
               f"Ejemplos:\n{prompts._examples(spec, 3000) or '(sin ejemplos visibles)'}\n\n"
               "CÓDIGO ACTUAL de solucion.py: solo ves lo necesario para el parche. Los números de línea NO son parte "
               f"del código.\n\n{PA.texto_zonas(f['codigo'], spec['function'], {'zonas': f['zonas']})}\n\n"
               f"{f.get('titulo_causa', TITULO_CAUSA)}:\n{f['causa']}\n\n"
               + (f"AVISO DEL COORDINADOR:\n{aviso}\n\n" if aviso else "")
               + f.get("pedido", PEDIDO))
    return [{"role": "system", "content": PA.SISTEMA_PARCHE}, {"role": "user", "content": usuario}]


# Experimento 7.6: normalización de la sangría ----------------------------------------------------------------------

def _sangria(linea: str) -> tuple[int, str]:
    """(espacios iniciales, contenido). ParcheRechazado si la sangría tiene tabulaciones (no hay un ancho único)."""
    contenido = linea.lstrip(" ")
    if contenido.startswith("\t"):
        raise PA.ParcheRechazado("normalización de sangría: hay tabulaciones en la sangría (ambiguo)")
    return len(linea) - len(contenido), contenido


def _forma(lineas: list[str]) -> list[tuple[int | None, str]]:
    """Por línea: (sangría relativa a la primera, contenido); las líneas vacías son (None, "")."""
    base = _sangria(lineas[0])[0]
    out = []
    for ln in lineas:
        if not ln.strip():
            out.append((None, ""))
        else:
            s, c = _sangria(ln)
            out.append((s - base, c))
    return out


def normalizar_sangria(codigo: str, parche: dict, zonas: list[tuple[int, int]]) -> tuple[dict, list[dict]]:
    """Ajusta SOLO la sangría inicial de los cambios que no coinciden literalmente. Devuelve (parche normalizado,
    justificación por cambio) o lanza ParcheRechazado. Nunca toca el contenido de una línea."""
    codigo_ls = [x.rstrip() for x in codigo.splitlines()]
    cambios, registro = [], []
    for i, c in enumerate(parche["cambios"], 1):
        orig = PA._lineas(c["original"])
        k = len(orig)
        literales = [j for j in range(len(codigo_ls) - k + 1) if codigo_ls[j:j + k] == orig]
        if len(literales) == 1:
            cambios.append(dict(c))
            registro.append({"cambio": i, "normalizado": False, "justificacion": "coincide literalmente"})
            continue
        if len(literales) > 1:
            raise PA.ParcheRechazado(f"cambio {i}: el fragmento original aparece {len(literales)} veces; no se "
                                     "normaliza un fragmento ambiguo")
        if not orig or not orig[0].strip():
            raise PA.ParcheRechazado(f"cambio {i}: normalización de sangría: fragmento original vacío")
        forma = _forma(orig)
        candidatos = []
        for j in range(len(codigo_ls) - k + 1):
            ventana = codigo_ls[j:j + k]
            if not ventana[0].strip():
                continue
            try:
                if _forma(ventana) == forma:
                    candidatos.append(j)
            except PA.ParcheRechazado:
                continue
        if not candidatos:
            raise PA.ParcheRechazado(f"cambio {i}: el fragmento original no aparece en el código ni siquiera "
                                     "ajustando la sangría: el contenido o la sangría relativa de sus líneas es "
                                     "distinto (la normalización no corrige el contenido)")
        if len(candidatos) > 1:
            raise PA.ParcheRechazado(f"cambio {i}: ajustando la sangría, el fragmento aparece {len(candidatos)} veces "
                                     f"(líneas {', '.join(str(j + 1) for j in candidatos)}): ambiguo, no se normaliza")
        a, b = candidatos[0] + 1, candidatos[0] + k
        if not any(za <= a and b <= zb for za, zb in zonas):
            raise PA.ParcheRechazado(f"cambio {i}: ajustando la sangría, el fragmento está en las líneas {a}–{b}, "
                                     "fuera de las zonas autorizadas")
        desplazamiento = _sangria(codigo_ls[candidatos[0]])[0] - _sangria(orig[0])[0]
        reemplazo = PA._lineas(c["reemplazo"]) if c["reemplazo"].strip() else []
        if reemplazo and _sangria(reemplazo[0])[0] != _sangria(orig[0])[0]:
            raise PA.ParcheRechazado(f"cambio {i}: la primera línea del reemplazo no tiene la misma sangría que la "
                                     "del original: no se puede saber cuánto desplazarla (ambiguo)")
        nuevo = []
        for ln in reemplazo:
            if not ln.strip():
                nuevo.append("")
                continue
            s, contenido = _sangria(ln)
            if s + desplazamiento < 0:
                raise PA.ParcheRechazado(f"cambio {i}: ajustando la sangría, una línea del reemplazo quedaría con "
                                         "sangría negativa")
            nuevo.append(" " * (s + desplazamiento) + contenido)
        cambios.append({"original": "\n".join(codigo_ls[candidatos[0]:candidatos[0] + k]), "reemplazo": "\n".join(nuevo)})
        registro.append({"cambio": i, "normalizado": True, "lineas": [a, b], "desplazamiento": desplazamiento,
                         "justificacion": (f"el fragmento coincide solo en la sangría inicial: mismo contenido y "
                                           f"misma sangría relativa en sus {k} línea(s), una única aparición en todo "
                                           f"el código (líneas {a}–{b}) y dentro de las zonas autorizadas; original y "
                                           f"reemplazo se desplazan {desplazamiento:+d} espacios sin cambiar su "
                                           "contenido")})
    return dict(parche, cambios=cambios), registro


def _visibles(executor, codigo: str, spec: dict) -> dict:
    vis = dict(spec, cases=[c for c in spec["cases"] if c.get("visible", True)])
    res = executor.run(codigo, vis, "")
    r = res.result or {}
    ok = res.status == "ok" and not r.get("load_error") and bool(r.get("total")) and r.get("failed") == 0
    return {"ok": ok, "aprobados": r.get("passed"), "total": r.get("total"),
            "informe": "" if ok else (prompts.failure_report(r, [], 2000) if res.status == "ok" else
                                      f"La ejecución terminó «{res.status}»: {res.reason}")}


def carpeta_de_reparacion(carpeta: Path, corrida: Path, experimento: str = "experimento-7-5") -> Path:
    carpeta, corrida = Path(carpeta).resolve(), Path(corrida).resolve()
    if carpeta == corrida or corrida in carpeta.parents:
        raise ErrorReparacion("el experimento no escribe dentro de la corrida original: usá otra carpeta")
    for previo in ("taller.db", "taller"):
        if (carpeta / previo).exists():
            raise ErrorReparacion(f"la carpeta {carpeta} tiene «{previo}» de un experimento anterior; usá una propia")
    base = carpeta / experimento
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    nombre = time.strftime("reparacion-%Y%m%d-%H%M%S")
    destino, i = base / nombre, 1
    while destino.exists():
        i += 1
        destino = base / f"{nombre}-{i}"
    destino.mkdir(mode=0o700)
    return destino


def correr(destino: Path, f: dict, executor, cliente, gate, modelo, max_segundos: float = 2400.0,
           cancel: threading.Event | None = None, normalizar: bool = False, retroalimentacion=None,
           revision=None) -> dict:
    """retroalimentacion (opcional, 7.8): función que arma el aviso para el intento siguiente cuando un parche se
    aplica pero no corrige el defecto; por omisión, la retroalimentación del 7.1 de siempre.
    revision (opcional, 7.9): función que revisa un parche YA validado y aplicado por el coordinador; devuelve
    {"decision": "aprobar" | "rechazar" | None, "aviso": texto para el intento siguiente si rechaza, "registro": …,
    "segundos": …}. Un rechazo pasa al intento siguiente sin verificar; una aprobación (o None) sigue con las
    comprobaciones deterministas de siempre. Por omisión, sin revisión."""
    destino = Path(destino)

    def guardar(rel, contenido):
        p = destino / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(contenido if isinstance(contenido, str) else
                     json.dumps(contenido, ensure_ascii=False, indent=2, default=str))
    fabrica, referencia = MISIONES[f["mision"]]
    m = fabrica()
    spec, funcion = m["spec"], m["spec"]["function"]
    contraste = CT.desde_mision(fabrica, referencia, VALIDADORES[f["mision"]])
    codigo = f["codigo"]
    original = RE.programa(codigo)
    ref_caso = CT.contrastar(executor, contraste, codigo, f["caso"], None)
    esperado = (ref_caso["referencia"] or {}).get("salida")
    inf = {"experimento": VERSION_NORMALIZACION if normalizar else VERSION_REPARACION,
           "normalizacion_de_sangria": normalizar, "corrida": f["corrida"], "mision": f["mision"], "zonas": f["zonas"],
           "causa": f["causa"], "modelo": modelo.name, "max_intentos": MAX_INTENTOS, "max_segundos": max_segundos,
           "archivos_leidos": f["archivos"], "caso_sintetico": f["caso"],
           "caso_en_el_codigo_defectuoso": ref_caso["discrepancia"], "intentos": [], "llamadas": [],
           "verificacion": None, "exito": False, "motivo": None, "inicio": time.time()}
    claves, aviso, pedidos = [], "", []
    for n in range(1, MAX_INTENTOS + 1):
        usados = sum(ll["segundos"] or 0 for ll in inf["llamadas"] + inf.get("llamadas_revision", []))
        if usados >= max_segundos:
            inf["motivo"] = f"límite de tiempo de inferencia ({usados:.0f} s de {max_segundos:.0f} s)"
            break
        msgs = mensajes(f, aviso)
        pedidos.append(msgs)
        auditoria = RE.auditar(pedidos, spec, f["defecto"], m["description"])
        guardar(f"intento-{n}/pedido.json", msgs)
        if not auditoria["sin_fugas"]:
            inf["motivo"] = "la auditoría encontró información prohibida en el pedido: " + ", ".join(
                auditoria["hallazgos"])
            break
        it = {"intento": n, "estado": None}
        inf["intentos"].append(it)
        t0 = time.monotonic()
        try:
            gate.acquire("reparación 7.5", cancel)
            try:
                gen = cliente.chat(modelo, msgs, cancel, fmt="json")
            finally:
                gate.release()
        except (OllamaError, OSError) as e:
            inf["llamadas"].append({"intento": n, "segundos": round(time.monotonic() - t0, 1), "error": str(e)})
            it.update(estado="sin_respuesta", detalle=str(e))
            inf["motivo"] = f"el trabajador no respondió: {e}"
            break
        inf["llamadas"].append({"intento": n, "segundos": round(time.monotonic() - t0, 1),
                                "tokens_prompt": gen.prompt_tokens, "tokens_generados": gen.eval_tokens,
                                "fin": gen.done_reason, "cortada": bool(gen.truncated), "tiempos": gen.timings()})
        guardar(f"intento-{n}/respuesta.txt", gen.text)
        reg = {"aplicado": False}
        try:
            parche = PA.interpretar_parche(gen.text, funcion)
            reg.update(parche)
            k = PA.clave(parche)
            if k in claves:
                reg["repite_el_intento"] = claves.index(k) + 1
            claves.append(k)
            try:
                nuevo, aplicacion = PA.aplicar(codigo, parche, f["zonas"], spec)
            except PA.ParcheRechazado as e:
                # 7.6: solo un fallo de coincidencia literal habilita la normalización de la sangría.
                if not normalizar or "no aparece en el código" not in str(e):
                    raise
                reg["rechazo_literal"] = str(e)
                normalizado, justificacion = normalizar_sangria(codigo, parche, f["zonas"])
                reg.update(parche_normalizado=normalizado, normalizacion=justificacion)
                nuevo, aplicacion = PA.aplicar(codigo, normalizado, f["zonas"], spec)
            reg.update(aplicado=True, aplicacion=aplicacion)
        except PA.ParcheRechazado as e:
            reg["motivo"] = str(e) + (" (la respuesta se cortó por el límite de tokens)" if gen.truncated else "")
            nuevo = None
        guardar(f"intento-{n}/parche.json", reg)
        if nuevo is None:
            it.update(estado="rechazado", motivo=reg["motivo"])
            aviso = (f"Tu parche del intento {n} fue rechazado: {reg['motivo']}"
                     + (f" Es el mismo parche que el del intento {reg['repite_el_intento']}."
                        if reg.get("repite_el_intento") else ""))
            continue
        guardar(f"intento-{n}/solucion.py", nuevo)
        ct = CT.contrastar(executor, contraste, nuevo, f["caso"], None)
        vis = _visibles(executor, nuevo, spec)
        disc = {"no_observada": "eliminada", "demostrada": "persiste"}.get(ct["discrepancia"], "inconclusa")
        it.update(estado="aplicado", modifica_el_programa=RE.programa(nuevo) != original,
                  sangria_normalizada="parche_normalizado" in reg,
                  discrepancia_sintetica=disc, visibles={k: vis[k] for k in ("ok", "aprobados", "total")},
                  repite_el_intento=reg.get("repite_el_intento"))
        if revision is not None:
            rv = revision(n=n, f=f, parche=reg.get("parche_normalizado") or {k: reg[k] for k in ("cambios",
                                                                                                 "justificacion")},
                          original=codigo, nuevo=nuevo)
            inf.setdefault("llamadas_revision", []).append({"intento": n, "segundos": rv.get("segundos") or 0})
            it["revision"] = rv["registro"]
            guardar(f"intento-{n}/revision.json", rv["registro"])
            if rv.get("decision") == "rechazar":
                it["estado"] = "rechazado_por_revisora"
                aviso = rv["aviso"]
                guardar(f"intento-{n}/retroalimentacion.md", aviso)
                continue
        if disc == "eliminada" and vis["ok"]:
            ver = RE._bateria(executor, nuevo, m)
            inf["verificacion"] = dict(ver, intento=n)
            it["bateria"] = {k: ver.get(k) for k in ("ok", "aprobados", "total")}
            inf["exito"] = bool(ver["ok"] and it["modifica_el_programa"])
            inf["motivo"] = (f"parche del intento {n} aplicado; la batería independiente aprobó "
                             f"{ver['aprobados']}/{ver['total']}" if inf["exito"] else
                             f"el parche del intento {n} eliminó la discrepancia del caso y cumple los ejemplos "
                             f"visibles, pero la batería independiente no aprobó ({ver['aprobados']}/{ver['total']})")
            break
        cand = ct["candidato"] or {"ejecucion": ct["discrepancia"], "salida": None, "detalle": ct["motivo"]}
        salida = cand.get("salida") if cand.get("ejecucion") == "completa" else None
        mismo = [0] if RE.programa(nuevo) == original else []
        retro = RE.texto_retroalimentacion(n, 0, funcion, f["caso"], esperado, cand,
                                           RE.comparar_salidas(esperado, salida), RE.cambio_de_codigo(codigo, nuevo),
                                           mismo, [0] if salida is not None and salida == ref_caso["candidato"]
                                           ["salida"] else [], disc)
        if retroalimentacion is not None:
            aviso = retroalimentacion(n=n, caso=f["caso"], funcion=funcion, esperado=esperado, cand=cand, disc=disc,
                                      visibles=vis, normalizado="parche_normalizado" in reg)
            guardar(f"intento-{n}/retroalimentacion.md", aviso)
            continue
        guardar(f"intento-{n}/retroalimentacion.md", retro)
        aviso = (f"Tu parche del intento {n} se aplicó"
                 + (" (el coordinador ajustó solo su sangría)" if "parche_normalizado" in reg else "")
                 + ", pero no corrige el defecto. Tu próximo parche se aplica de "
                 "nuevo sobre el código de arriba (el original).\n" + retro
                 + ("" if vis["ok"] else "\nAdemás no cumple los ejemplos visibles del contrato:\n" + vis["informe"]))
    if inf["motivo"] is None:
        inf["motivo"] = f"ningún parche corrigió el defecto en {len(inf['intentos'])} intentos"
    inf["auditoria_de_fugas"] = RE.auditar(pedidos, spec, f["defecto"], m["description"])
    inf["criterios"] = {
        "parche_valido_y_aplicado": any(i["estado"] == "aplicado" for i in inf["intentos"]),
        "modifica_el_codigo_defectuoso": any(i.get("modifica_el_programa") for i in inf["intentos"]),
        "bateria_independiente_completa": bool(inf["verificacion"] and inf["verificacion"]["ok"]),
    }
    inf["exito"] = all(inf["criterios"].values()) and inf["auditoria_de_fugas"]["sin_fugas"]
    verificado = next((i for i in inf["intentos"] if inf["verificacion"] and i["intento"] ==
                       inf["verificacion"]["intento"]), None)
    inf["clasificacion"] = (None if not inf["exito"] else
                            "reparación automática con normalización de formato (sangría)"
                            if verificado and verificado.get("sangria_normalizada") else "reparación autónoma")
    inf["fin"] = time.time()
    guardar("informe.json", inf)
    return inf
