"""Experimento B1: efecto del coordinador frente a los controles históricos de A1. SOLO LECTURA.

  <venv>/bin/python -I comparar_b1.py --a1 informes/a1-p1-s11-<et> --b1 informes/b1-p1-s11-<et>

Diseño: A1 mostró que, con arranque en frío, Qwen recorre siempre la misma trayectoria. Esa trayectoria es el
contrafáctico de cada corrida B1 («lo que habría pasado sin el mensaje»). Por corrida B1:
  validez   prefijo idéntico a A1 hasta el mensaje (pedido y respuesta, llamada por llamada; distingue «idéntico»,
            «solo ruido del entorno» y «distinto»); en la llamada en que entra el mensaje, el pedido tiene que ser
            el de A1 MÁS el mensaje y nada más; mismo código, versión de Ollama y modelo (digest) que A1;
            arranque en frío; sin contaminación ni corte por tiempo; ≤ 30 iteraciones.
  conducta  ¿la respuesta del modelo cambia respecto de A1 desde que tiene el mensaje? ¿cuántas llamadas después?
            ¿edita el código productivo después del mensaje?
  resolución  categoría con pruebas ocultas (ejecutar.py verificar), frente a la de los controles.
  terminación finish / agotamiento de iteraciones / stuck del SDK (y si la ventana del stuck empezó en el mensaje).
Las tres cosas se informan por separado: cambiar de conducta no es resolver, y terminar antes no es mejorar.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI))
sys.path.insert(0, str(AQUI.parent))
import comparar_a1 as C  # noqa: E402
from comparar_a1 import A  # noqa: E402   analizar_par

# Archivos que solo analizan resultados: pueden cambiar entre A1 y B1 sin afectar las corridas.
SOLO_ANALISIS = ("comparar_a1.py", "comparar_b1.py")


def crudas_con_mensajes(resultado: Path) -> list[dict]:
    filas = C.llamadas_crudas(resultado)
    for f in filas:
        try:
            f["_mensajes"] = json.loads((resultado / "estado" / "llm" / f["archivo"]).read_text(encoding="utf-8")).get("raw_messages")
        except (OSError, ValueError):
            f["_mensajes"] = None
    return filas


def sin_mensaje(mensajes, texto: str):
    """El pedido sin las entradas que contienen el texto del coordinador."""
    clave = json.dumps(texto, ensure_ascii=False)[1:-1]
    return [m for m in (mensajes or []) if clave not in json.dumps(m, ensure_ascii=False) and texto not in str(m)]


def huella(mensajes) -> tuple[str, str]:
    t = str(mensajes)
    return C.sha(t), C.sha(C.sin_ruido(t))


def cargar(nombre: str, corridas: Path) -> dict:
    an = A.analizar_corrida(corridas / nombre)
    r = Path(an["resultado"]) if an["resultado"] else None
    final = A.leer(r / "resultado_final.json") if r else None
    api_ps = A.leer(r / "api_ps.json") if r else None
    digest = next((m.get("digest") for m in (api_ps or {}).get("models", []) if (m.get("name") or m.get("model")) == "qwen2.5-coder:7b"), None)
    return {"nombre": nombre, "analisis": an, "crudas": crudas_con_mensajes(r) if r else [],
            "vencio_tiempo": (final or {}).get("vencio_tiempo"), "digest": digest, "corrida": corridas / nombre}


def categoria(c: dict, oh_duo_01: Path | None) -> str | None:
    if not oh_duo_01:
        return None
    import intervenciones
    return intervenciones.categorias(c["corrida"], oh_duo_01).get(c["analisis"]["ejecucion"])


def terminacion(an: dict) -> str:
    """finish · agotamiento_de_iteraciones (MaxIterationsReached; OH-DUO-01 lo llama «error») · stuck · otro."""
    est = an["estado_final_sdk"]
    if est == "stuck":
        return "stuck"
    if est == "finished":
        return "finish"
    if any(e.get("codigo") == "MaxIterationsReached" for e in an["errores"]):
        return "agotamiento_de_iteraciones"
    return f"otro ({est})"


def clasificar_pedido(a, b) -> str:
    ha, hb = huella(a), huella(b)
    return "identico" if ha[0] == hb[0] else "solo_ruido_del_entorno" if ha[1] == hb[1] else "distinto"


def comparar_corrida(b: dict, ref: dict, a1_categoria: str | None, b_categoria: str | None) -> dict:
    an, rc, bc = b["analisis"], ref["crudas"], b["crudas"]
    coord = [m for m in an["mensajes_de_usuario_posteriores"] if m["tipo"] == "coordinador"]
    problemas, avisos = [], []
    r = {"corrida": b["nombre"], "mensajes": len(coord)}
    if not bc:
        problemas.append("sin registro crudo de pedidos y respuestas")
    if not coord:
        r.update(valida=False, problemas=problemas + ["el coordinador no llegó a intervenir (no hay mensaje en la conversación)"],
                 avisos=avisos, terminacion=terminacion(an), categoria=b_categoria)
        return r
    texto = coord[0]["texto"][:120]          # analizar_par lo recorta a 400; con el comienzo alcanza para ubicarlo
    # 1. ¿en qué llamada entra el mensaje al contexto?
    m = next((k for k, f in enumerate(bc) if any(texto in json.dumps(x, ensure_ascii=False) or texto in str(x)
                                                 for x in (f["_mensajes"] or []))), None)
    r["llamada_con_mensaje"] = m
    r["iteracion_de_entrega"] = next((e.get("iteracion_al_entregar") for e in an["entregas_del_puente"] if e.get("resultado") == "entregado"), None)
    if m is None:
        problemas.append("el mensaje nunca aparece en un pedido al modelo")
    # 2. prefijo: llamadas 0..m-1 iguales a A1
    prefijo = []
    for k in range(min(m if m is not None else len(bc), len(rc), len(bc))):
        prefijo.append({"pedido": clasificar_pedido(rc[k]["_mensajes"], bc[k]["_mensajes"]),
                        "respuesta_igual": rc[k]["respuesta_sha"] == bc[k]["respuesta_sha"]})
    r["prefijo"] = {"llamadas": len(prefijo),
                    "pedidos": {x: sum(1 for p in prefijo if p["pedido"] == x) for x in ("identico", "solo_ruido_del_entorno", "distinto")},
                    "respuestas_distintas": [k for k, p in enumerate(prefijo) if not p["respuesta_igual"]]}
    if r["prefijo"]["respuestas_distintas"] or r["prefijo"]["pedidos"]["distinto"]:
        problemas.append(f"la trayectoria difiere de A1 ANTES del mensaje (respuestas distintas en las llamadas "
                         f"{r['prefijo']['respuestas_distintas']}, pedidos distintos: {r['prefijo']['pedidos']['distinto']})")
    if r["prefijo"]["pedidos"]["solo_ruido_del_entorno"]:
        avisos.append(f"{r['prefijo']['pedidos']['solo_ruido_del_entorno']} pedido(s) del prefijo difieren solo por ruido del entorno")
    # 3. la llamada con el mensaje: el pedido es el de A1 más el mensaje
    if m is not None and m < len(rc):
        sin = sin_mensaje(bc[m]["_mensajes"], texto)
        r["pedido_con_mensaje"] = {"igual_a_a1_salvo_el_mensaje": clasificar_pedido(rc[m]["_mensajes"], sin),
                                   "entradas_agregadas": len(bc[m]["_mensajes"] or []) - len(sin),
                                   "entradas_en_a1": len(rc[m]["_mensajes"] or []), "entradas_en_b1": len(bc[m]["_mensajes"] or [])}
        if r["pedido_con_mensaje"]["igual_a_a1_salvo_el_mensaje"] == "distinto":
            problemas.append("en la llamada del mensaje, el pedido difiere de A1 en algo más que el mensaje")
        if r["pedido_con_mensaje"]["entradas_agregadas"] != 1:
            avisos.append(f"el mensaje ocupa {r['pedido_con_mensaje']['entradas_agregadas']} entradas del pedido (se esperaba 1)")
    elif m is not None:
        problemas.append("el mensaje entró después de la última llamada de A1: no hay contrafáctico")
    # 4. conducta: ¿desde qué llamada responde distinto que A1?
    cambio = next((k for k in range(m, min(len(bc), len(rc))) if bc[k]["respuesta_sha"] != rc[k]["respuesta_sha"]), None) if m is not None else None
    if cambio is None and m is not None and len(bc) != len(rc):
        cambio = min(len(bc), len(rc))
    acc_msg = coord[0]["acciones_antes"]
    edita = [a for a in an["acciones"][acc_msg:] if a["herramienta"] == "file_editor" and "intervalos.py" in a["argumentos"]
             and '"view"' not in a["argumentos"]]
    edita_a1 = [a for a in ref["analisis"]["acciones"][acc_msg:] if a["herramienta"] == "file_editor" and "intervalos.py" in a["argumentos"]
                and '"view"' not in a["argumentos"]]
    pa = (bc[m]["respuesta"] if m is not None and m < len(bc) else "")
    r["conducta"] = {"cambio": cambio is not None, "llamadas_hasta_el_cambio": None if cambio is None else cambio - m,
                     "primera_respuesta_con_mensaje": " ".join(pa.split())[:300],
                     "misma_respuesta_que_a1_en_esa_llamada": None if m is None or m >= len(rc) else bc[m]["respuesta_sha"] == rc[m]["respuesta_sha"],
                     "ediciones_de_intervalos_py_despues": len(edita), "en_a1_mismo_tramo": len(edita_a1)}
    # 5. terminación y resolución, por separado
    st = an["stuck"]
    r["terminacion"] = {"tipo": terminacion(an), "estado_sdk": an["estado_final_sdk"], "estado_ejecucion": an["estado_ejecucion"],
                        "stuck": st.get("stuck_tras_evento") is not None,
                        "stuck_patron": st.get("patron"),
                        "ventana_del_stuck_empieza_tras_el_mensaje": (st.get("ventana_desde_evento") or -1) > coord[0]["evento"]
                        if st.get("stuck_tras_evento") is not None else None,
                        "acciones": len(an["acciones"]), "control": terminacion(ref["analisis"]),
                        "acciones_control": len(ref["analisis"]["acciones"])}
    r["resolucion"] = {"categoria": b_categoria, "control": a1_categoria}
    # 6. condiciones generales
    if b["vencio_tiempo"]:
        problemas.append("la corrida llegó al límite de tiempo (900 s): la terminación no es comparable")
    if len({x["llamada"] for x in an["acciones"]}) > 30:
        problemas.append("más de 30 iteraciones")
    if b["digest"] and ref["digest"] and b["digest"] != ref["digest"]:
        problemas.append(f"el modelo cambió desde A1 (digest {ref['digest'][:19]} → {b['digest'][:19]})")
    elif not (b["digest"] and ref["digest"]):
        avisos.append("no se pudo comparar el digest del modelo con A1")
    r.update(valida=not problemas, problemas=problemas, avisos=avisos)
    return r


def analizar(a1: Path, b1: Path, corridas_a1: Path, corridas_b1: Path, oh_duo_01: Path | None = None) -> dict:
    ea, eb = json.loads((a1 / "experimento.json").read_text()), json.loads((b1 / "experimento.json").read_text())
    globales = []
    # A1 tiene que haber sido reproducible: si no, no hay contrafáctico único
    ra = C.analizar(a1, corridas_a1)
    if ra["veredicto"] != "REPRODUCIBLE":
        globales.append(f"A1 no es REPRODUCIBLE ({ra['veredicto']}): no hay un contrafáctico único")
    # mismo código que afecta a las corridas
    distintos = [p for p, h in ea.get("huellas", {}).items() if not p.endswith(SOLO_ANALISIS) and eb.get("huellas", {}).get(p) not in (None, h)]
    faltan = [p for p in ea.get("huellas", {}) if not p.endswith(SOLO_ANALISIS) and p not in eb.get("huellas", {})]
    if distintos or faltan:
        globales.append(f"el código que afecta a las corridas cambió desde A1: {distintos + faltan}")
    for k in ("problema", "modelo", "semilla", "temperatura", "limites"):
        va, vb = ea.get(k), eb.get(k)
        if k == "limites":
            va, vb = ({x: y for x, y in (v or {}).items() if x != "REGISTRO_LLM"} for v in (va, vb))
        if va != vb:
            globales.append(f"{k} distinto: A1 {va!r} / B1 {vb!r}")
    va, vb = A.leer(a1 / "ollama_version.json"), A.leer(b1 / "ollama_version.json")
    if (va or {}).get("version") != (vb or {}).get("version"):
        globales.append(f"versión de Ollama distinta: A1 {va} / B1 {vb}")
    estados = {f"A1:{n}": A.leer(a1 / "estado_ollama" / f"{n}-antes.json") for n in ea["corridas"]}
    estados |= {f"B1:{n}": A.leer(b1 / "estado_ollama" / f"{n}-antes.json") for n in eb["corridas"]}
    eq = C.equivalencia(estados)
    if not eq["equivalentes"]:
        globales.append("condiciones iniciales no equivalentes: " + "; ".join(eq["problemas"]))
    contaminadas = [n for n in eb["corridas"] if (A.leer(b1 / "estado_ollama" / f"{n}-despues.json") or {}).get("contaminada")]
    if contaminadas:
        globales.append(f"corridas contaminadas: {contaminadas}")

    ref = cargar(ea["corridas"][0], corridas_a1)
    a1_cats = {n: categoria(cargar(n, corridas_a1), oh_duo_01) for n in ea["corridas"]}
    filas = []
    for n in eb["corridas"]:
        b = cargar(n, corridas_b1)
        f = comparar_corrida(b, ref, a1_cats.get(ea["corridas"][0]), categoria(b, oh_duo_01))
        if n in contaminadas:
            f["valida"] = False
            f["problemas"].append("contaminada")
        filas.append(f)
    # réplicas de B1 entre sí (el punto de inyección depende de la latencia)
    bs = [cargar(n, corridas_b1) for n in eb["corridas"]]
    replicas = []
    for i in range(len(bs)):
        for j in range(i + 1, len(bs)):
            x, y = ({"nombre": z["nombre"], "analisis": z["analisis"], "crudas": z["crudas"]} for z in (bs[i], bs[j]))
            p = C.comparar_par(x, y)
            replicas.append({"par": p["par"], "identicas": p["identicas"], "primera_llamada_distinta": p["primera_llamada_distinta"]})
    validas = [f for f in filas if f["valida"]] if not globales else []
    return {"a1": a1.name, "b1": b1.name, "a1_veredicto": ra["veredicto"], "problemas_globales": globales,
            "equivalencia": eq, "corridas": filas, "replicas_b1": replicas,
            "resumen": {"validas": len(validas), "total": len(filas),
                        "con_cambio_de_conducta": sum(1 for f in validas if f["conducta"]["cambio"]),
                        "resueltas": sum(1 for f in validas if (f["resolucion"]["categoria"] or "") == "correcta"),
                        "resueltas_en_a1": sum(1 for c in a1_cats.values() if c == "correcta"),
                        "terminaciones": {t: sum(1 for f in validas if f["terminacion"]["tipo"] == t)
                                          for t in sorted({f["terminacion"]["tipo"] for f in validas})},
                        "stuck_tras_el_mensaje": sum(1 for f in validas if f["terminacion"]["ventana_del_stuck_empieza_tras_el_mensaje"]),
                        "llamada_con_mensaje": [f.get("llamada_con_mensaje") for f in filas]},
            "categorias_a1": a1_cats}


def markdown(r: dict) -> str:
    ls = [f"# Experimento B1 `{r['b1']}` frente a A1 `{r['a1']}` (A1: {r['a1_veredicto']})", ""]
    if r["problemas_globales"]:
        ls += ["**COMPARACIÓN NO VÁLIDA**:", ""] + [f"- {p}" for p in r["problemas_globales"]] + [""]
    s = r["resumen"]
    ls += [f"Corridas válidas: **{s['validas']} / {s['total']}** · con cambio de conducta: {s['con_cambio_de_conducta']} · "
           f"resueltas (ocultas): {s['resueltas']} (controles A1 resueltos: {s['resueltas_en_a1']}) · terminaciones: {s['terminaciones']} · "
           f"llamada en que entró el mensaje: {s['llamada_con_mensaje']}", "",
           "| corrida | válida | prefijo (idénticos / ruido / distintos) | entra en la llamada | pedido = A1 + mensaje | cambio de conducta | ediciones de intervalos.py (A1) | terminación (A1) | stuck tras el mensaje | categoría (A1) |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for f in r["corridas"]:
        if "prefijo" not in f:
            ls.append(f"| {f['corrida']} | **no** | — | — | — | — | — | {f['terminacion']} | — | {f.get('categoria')} |")
            continue
        p, c, t = f["prefijo"]["pedidos"], f["conducta"], f["terminacion"]
        ls.append(f"| {f['corrida']} | {'sí' if f['valida'] else '**no**'} | {p['identico']} / {p['solo_ruido_del_entorno']} / {p['distinto']} | "
                  f"{f['llamada_con_mensaje']} | {(f.get('pedido_con_mensaje') or {}).get('igual_a_a1_salvo_el_mensaje', '—')} | "
                  f"{'sí, a ' + str(c['llamadas_hasta_el_cambio']) + ' llamada(s)' if c['cambio'] else 'no'} | "
                  f"{c['ediciones_de_intervalos_py_despues']} ({c['en_a1_mismo_tramo']}) | {t['tipo']}, {t['acciones']} acc. ({t['control']}, {t['acciones_control']}) | "
                  f"{t['ventana_del_stuck_empieza_tras_el_mensaje'] if t['stuck'] else '—'} | {f['resolucion']['categoria']} ({f['resolucion']['control']}) |")
    for f in r["corridas"]:
        if f["problemas"] or f["avisos"]:
            ls.append(f"- **{f['corrida']}**: " + "; ".join([*f["problemas"], *[f"aviso: {a}" for a in f["avisos"]]]))
    for f in r["corridas"]:
        if "conducta" in f:
            ls.append(f"- {f['corrida']}, primera respuesta con el mensaje: `{f['conducta']['primera_respuesta_con_mensaje'][:200]}`")
    ls += ["", f"Réplicas de B1 entre sí: {[(' / '.join(x['par']), x['identicas'], x['primera_llamada_distinta']) for x in r['replicas_b1']]} "
               "(pueden diferir si el mensaje entró en llamadas distintas)", "",
           "Lectura: la **conducta** cambia si, con el mensaje en el contexto, el modelo responde distinto que en A1; la "
           "**resolución** es la categoría con pruebas ocultas; la **terminación** distingue finish, agotamiento de las "
           "30 iteraciones y stuck del SDK. Un stuck cuya ventana empieza en el mensaje es un corte del SDK "
           "(el mensaje reinicia su ventana), no un efecto deseado del coordinador.", ""]
    return "\n".join(ls)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--a1", type=Path, required=True)
    ap.add_argument("--b1", type=Path, required=True)
    ap.add_argument("--corridas-a1", type=Path)
    ap.add_argument("--corridas-b1", type=Path)
    ap.add_argument("--oh-duo-01", type=Path)
    a = ap.parse_args(argv)
    ea, eb = (json.loads((x / "experimento.json").read_text()) for x in (a.a1, a.b1))
    ca = a.corridas_a1 or Path(ea.get("corridas_dir") or AQUI.parent / "corridas")
    cb = a.corridas_b1 or Path(eb.get("corridas_dir") or AQUI.parent / "corridas")
    oh1 = a.oh_duo_01
    if oh1 is None:
        from ejecutar import hermano
        oh1 = hermano("oh-duo-01", "duo.py")
    r = analizar(a.a1, a.b1, ca, cb, oh1)
    (a.b1 / "comparacion_b1.json").write_text(json.dumps(r, ensure_ascii=False, indent=1, default=str))
    (a.b1 / "comparacion_b1.md").write_text(markdown(r))
    print(markdown(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
