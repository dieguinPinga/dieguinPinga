"""Protocolo v2 del piloto (PILOTO.md, Enmienda 3). El protocolo v1 (brazos.py) queda intacto para reproducir el
ensayo v1.

Qué cambia, con las evidencias del ensayo v1 como casos de regresión:

1. CONTRATO sin ambigüedad: las candidatas se nombran con letras (A, B, C); si el modelo responde con un número que
   coincide con la línea de UNA sola candidata, se acepta como línea. Una respuesta ilegible tiene una reparación de
   formato (autoría «contrato»); recién la segunda es del modelo. Las compuestas se reemplazan COMPLETAS.
2. RECHAZOS QUE ENSEÑAN (igual en B, B*, C y D): el siguiente prompt incluye cada intento anterior (diff), su
   resultado y el motivo; un parche idéntico a uno ya rechazado no se ejecuta y se informa. Todo sale de la suite y
   del propio modelo: nunca del juez.
3. DISPUTA de verificadores: si solo fallan verificadores del modelo y las verificaciones del sistema pasan, el
   modelo decide (una vez por verificador) si está mal el instrumento o el verificador. Un verificador impugnado deja
   de bloquear el episodio. El juez evalúa la decisión DESPUÉS (falsas alarmas).
4. ENTORNO no es MODELO: un corte de conexión o un tiempo agotado interrumpen el episodio (se reintenta o se reanuda),
   no cuentan como intento ni como falla del modelo.
5. MEMORIA: antes de cada llamada se exige memoria disponible (o se espera y se aborta limpio); el modelo se descarga
   después de cada llamada (keep_alive por pedido) para no convivir con el sandbox.
6. CONTEXTO: antes de cada llamada se estima el prompt; si no entra, se recorta el historial; si aun así no entra, no
   se llama (falla del coordinador).
"""
from __future__ import annotations

import hashlib
import re
import time

from ... import codeextract
from ...explorer.context import estimate_tokens
from ...instrumentos import diagnostico as D
from . import banco as B
from . import brazos as BR
from . import localizador as L
from . import parches as P
from . import verificaciones as V

LETRAS = "ABCDEFGH"
FACTOR_TOKENS = 0.8      # tokens reales / estimados: 0,73 medido en el ensayo v1 (2013 reales, 2745 estimados)
RESERVA_CODIGO = 1700    # salida reservada para código: lo máximo observado (1377) + 23 %
RESERVA_CORTA = 768      # = num_predict de las respuestas cortas
HISTORIAL_COMPLETO = 1   # el último intento con diff y verificaciones; los anteriores, en una línea. Igual para
#                          todos los brazos: con 2, los brazos completos perdían historial antes que las microtareas


class EntornoInterrumpido(Exception):
    """Ollama cortó la conexión, no respondió a tiempo o similar: no es una falla del modelo."""


class MemoriaInsuficiente(Exception):
    """La guardia de memoria no dejó llamar al modelo: se aborta limpio en vez de arriesgar un OOM."""


class ContextoInsuficiente(Exception):
    """El prompt no entra en el contexto ni sin historial: falla del coordinador, no se llama."""


def _sha(texto: str) -> str:
    return hashlib.sha256(texto.encode()).hexdigest()


class Guardia:
    """Protección preventiva de memoria (solo lectura de /proc/meminfo y de /api/ps de Ollama).

    * antes_de_llamada: si el modelo no está cargado, exige MemAvailable ≥ minimo_mb; espera hasta espera_s y, si no
      alcanza, lanza MemoriaInsuficiente (no se llama: mejor abortar limpio que un OOM global).
    * antes_de_sandbox: espera (sin fallar) a que el modelo se haya descargado, para que no conviva con el sandbox.
    No puede evitar un OOM si otro proceso crece DURANTE una generación; reduce el riesgo y deja registro."""

    def __init__(self, ollama=None, minimo_mb: int = 3900, espera_s: float = 180, intervalo_s: float = 5,
                 meminfo: str = "/proc/meminfo", dormir=time.sleep):
        self.ollama, self.minimo_mb, self.espera_s, self.intervalo_s = ollama, minimo_mb, espera_s, intervalo_s
        self.meminfo, self.dormir = meminfo, dormir
        self.registro: list[dict] = []

    def disponible_mb(self) -> int | None:
        try:
            for linea in open(self.meminfo):
                if linea.startswith("MemAvailable:"):
                    return int(linea.split()[1]) // 1024
        except OSError:
            return None
        return None

    def cargado(self) -> bool:
        if self.ollama is None:
            return False
        try:
            return bool(self.ollama.loaded())
        except Exception:  # noqa: BLE001 — si /api/ps no responde, se asume no cargado (exige el mínimo completo)
            return False

    def antes_de_llamada(self) -> dict:
        if self.minimo_mb <= 0:
            return {"guardia": "desactivada"}
        esperado = 0.0
        while True:
            disp, carg = self.disponible_mb(), self.cargado()
            necesario = 256 if carg else self.minimo_mb
            if disp is None or disp >= necesario:
                r = {"mem_disponible_mb": disp, "modelo_cargado": carg, "espera_s": round(esperado, 1)}
                self.registro.append(r)
                return r
            if esperado >= self.espera_s:
                self.registro.append({"mem_disponible_mb": disp, "modelo_cargado": carg, "abortado": True})
                raise MemoriaInsuficiente(f"memoria disponible {disp} MiB < {necesario} MiB durante {esperado:.0f} s")
            self.dormir(self.intervalo_s)
            esperado += self.intervalo_s

    def antes_de_sandbox(self, espera_s: float = 60) -> None:
        esperado = 0.0
        while self.cargado() and esperado < espera_s:
            self.dormir(1)
            esperado += 1


class ModeloV2(BR.ModeloConPresupuesto):
    """El envoltorio de v1 más: guardia de memoria, control de contexto y clasificación de fallas del entorno."""

    def __init__(self, modelo, max_llamadas, max_segundos, guardia: Guardia | None = None):
        super().__init__(modelo, max_llamadas, max_segundos)
        self.guardia = guardia or Guardia(minimo_mb=0)
        self.interrupciones: list[dict] = []

    def tokens_estimados(self, prompt: str) -> int:
        return int((estimate_tokens(self.inner.system or "") + estimate_tokens(prompt)) * FACTOR_TOKENS)

    def cabe(self, prompt: str, corto: bool) -> bool:
        return self.tokens_estimados(prompt) + (RESERVA_CORTA if corto else RESERVA_CODIGO) <= self.inner.base.num_ctx

    def pedir(self, tipo, prompt, *, json_out, cancel=None, corto: bool = False, seed: int | None = None):
        if len(self.llamadas) >= self.max_llamadas or self.segundos >= self.max_segundos:
            raise BR.Agotado(f"presupuesto agotado ({len(self.llamadas)} llamadas, {self.segundos:.0f} s)")
        if not self.cabe(prompt, corto or json_out):
            raise ContextoInsuficiente(f"{tipo}: ~{self.tokens_estimados(prompt)} tokens de entrada no entran en "
                                       f"num_ctx={self.inner.base.num_ctx} con la reserva de salida")
        mem = self.guardia.antes_de_llamada()
        t0 = time.monotonic()
        extra = {"seed": seed} if seed is not None else {}
        text, call = self.inner.pedir(tipo, prompt, json_out=json_out, cancel=cancel, corto=corto, **extra)
        if call.get("resultado") in ("error", "contexto_excedido"):
            self.interrupciones.append({"tipo": tipo, **{k: v for k, v in call.items() if k != "ts"}})
            if call["resultado"] == "contexto_excedido":
                raise ContextoInsuficiente(f"Ollama rechazó el contexto: {call.get('error')}")
            raise EntornoInterrumpido(call.get("error") or "falla de Ollama")
        self.segundos += (call.get("reloj_ms") or (time.monotonic() - t0) * 1000) / 1000
        partes, self._partes = self._partes or [("sin_etiquetar", prompt)], None
        call = {**call, "memoria": mem, "tokens_estimados": self.tokens_estimados(prompt)}
        self.llamadas.append({**call, "prompt": prompt, "partes": partes, "respuesta": text})
        return text, call


# Prompts ----------------------------------------------------------------------------------------------------

def _historial(intentos: list[dict], completos: int) -> list[tuple[str, str]]:
    if not intentos:
        return []
    partes = [("coordinador", "Intentos anteriores en este episodio (no repitas un cambio ya rechazado):\n")]
    for i, it in enumerate(intentos, 1):
        cab = f"- intento {i}: {it['descripcion']} → {it['resultado'].upper()}: {it['razon']}\n"
        partes.append(("coordinador", cab))
        if i > len(intentos) - completos:
            if it.get("diff"):
                partes.append(("codigo", f"  cambio:\n{it['diff']}\n"))
            if it.get("fallas_despues"):
                partes.append(("evidencia", f"  verificaciones después de ese cambio:\n{it['fallas_despues']}\n"))
    return partes


def _con_historial(ctx, base_fn, intentos, corto: bool):
    """Arma el prompt con todo el historial que entre en el contexto: completo, después reducido y por último sin
    historial. Si ni así entra, ContextoInsuficiente."""
    for completos in sorted({HISTORIAL_COMPLETO, 0}, reverse=True):
        for con in (True, False) if completos == 0 else (True,):
            partes = base_fn(_historial(intentos, completos) if con else [])
            if ctx.modelo.cabe(BR._texto(partes), corto):
                return partes, (completos if con else -1)
    raise ContextoInsuficiente("el prompt no entra ni sin historial")


def _candidatas_texto(codigo, cands, oraculo: bool) -> str:
    out = []
    for i, c in enumerate(cands):
        u = c["unidad"]
        tipo = " (compuesta: se reemplaza COMPLETA, encabezado y cuerpo)" if u.compuesta else ""
        motivo = f"{BR.MARCA_ORACULO} el defecto está en esta instrucción" if oraculo else \
            f"sospechosa: {c['motivo']} — puede no ser la causa"
        out.append(f"{LETRAS[i]}) línea {u.lineno}{tipo} — {motivo}\n{P.mostrar(codigo, u, completa=True)}")
    return "\n\n".join(out)


FORMATO_MICRO = ("Corregí UNA candidata. Respondé EXACTAMENTE:\nCANDIDATO: <letra>\nACCION: reemplazar | eliminar\n"
                 "```python\n<el código nuevo de esa candidata (si es compuesta, con su cuerpo)>\n```\n"
                 "(si ACCION es eliminar, no incluyas código)")
RE_CAND = re.compile(r"CANDIDATO:\s*([A-Ha-h]\b|\d+)")
RE_ACC = re.compile(r"ACCI[OÓ]N:\s*(reemplazar|eliminar)", re.I)
RE_BLOQUE = re.compile(r"```[a-zA-Z0-9]*\s*\n(.*?)```", re.S)


def interpretar_micro(text: str, cands) -> tuple[dict | None, str]:
    """(elección, nota). La elección es {"unidad", "accion", "fragmento"}; None si no se puede interpretar."""
    m, a = RE_CAND.search(text or ""), RE_ACC.search(text or "")
    if not m or not a:
        return None, "falta CANDIDATO o ACCION"
    v, nota = m.group(1), ""
    if v.isalpha():
        i = LETRAS.index(v.upper())
        if i >= len(cands):
            return None, f"CANDIDATO {v.upper()} no existe (hay {len(cands)})"
    else:
        coinciden = [j for j, c in enumerate(cands) if c["unidad"].lineno == int(v)]
        if len(coinciden) != 1:
            return None, f"CANDIDATO {v} no es una letra ni la línea de una candidata"
        i, nota = coinciden[0], f"«{v}» interpretado como la línea de la candidata {LETRAS[coinciden[0]]}"
    accion = a.group(1).lower()
    frag = ""
    if accion == "reemplazar":
        b = RE_BLOQUE.search(text)
        frag = b.group(1) if b else text[a.end():]
    return {"unidad": cands[i]["unidad"], "accion": accion, "fragmento": frag, "letra": LETRAS[i]}, nota


def _micro(ctx, codigo, cands, ev, oraculo, intentos, paso) -> str | None:
    def base(hist):
        return BR._contexto(ev["salida"]) + BR._evidencia(ev) + hist + [
            ("oraculo" if oraculo else "coordinador",
             "Instrucciones candidatas («>>» marca lo que se reemplaza):\n" + _candidatas_texto(codigo, cands, oraculo)
             + "\n"), ("coordinador", FORMATO_MICRO)]
    partes, paso["historial_incluido"] = _con_historial(ctx, base, intentos, corto=True)
    text, _ = ctx.modelo.con_partes(partes).pedir("micro", BR._texto(partes), json_out=False, corto=True)
    eleccion, nota = interpretar_micro(text, cands)
    if eleccion is None:
        paso["contrato"] = nota
        reparar = [("coordinador", f"Tu respuesta anterior no se pudo interpretar: {nota}.\nTu respuesta fue:\n"
                                   f"{(text or '')[:800]}\n\nLas candidatas son: "
                                   + ", ".join(f"{LETRAS[i]} = línea {c['unidad'].lineno}" for i, c in enumerate(cands))
                                   + ".\nNo cambies tu corrección: repetila en el formato pedido.\n" + FORMATO_MICRO)]
        text, _ = ctx.modelo.con_partes(reparar).pedir("micro-formato", BR._texto(reparar), json_out=False, corto=True)
        eleccion, nota2 = interpretar_micro(text, cands)
        if eleccion is None:
            paso.update({"atribucion": "modelo", "detalle": f"formato: {nota2} (después de reparar el formato)"})
            return None
    paso.update({"candidato": eleccion["letra"], "linea": eleccion["unidad"].lineno, "accion": eleccion["accion"],
                 "interpretacion": nota or None})
    ens = P.aplicar(codigo, eleccion["unidad"], eleccion["accion"], eleccion["fragmento"], completa=True)
    paso.update({"atribucion": ens.atribucion, "detalle": ens.detalle})
    paso["descripcion"] = (f"candidata {eleccion['letra']} (línea {eleccion['unidad'].lineno}), "
                           f"{eleccion['accion']}")
    return ens.codigo


def _completo(ctx, codigo, cands, ev, oraculo, intentos, paso) -> str | None:
    if oraculo:
        u = cands[0]["unidad"]
        loc = [("oraculo", f"{BR.MARCA_ORACULO} El defecto está en la línea {u.lineno}:\n"
                           f"{P.mostrar(codigo, u, 0, completa=True)}\n")]
    else:
        loc = [("coordinador", "Instrucciones sospechosas (localizadas automáticamente, pueden no ser la causa):\n"
                + "\n".join(f"- línea {c['unidad'].lineno}: {c['motivo']}" for c in cands) + "\n")]

    def base(hist):
        return BR._contexto(ev["salida"]) + [("codigo", f"Instrumento actual:\n```python\n{codigo}```\n")] + \
            BR._evidencia(ev) + hist + loc + [("coordinador", "Devolvé el instrumento COMPLETO corregido, en un solo "
                                                              "bloque ```python```, sin texto adicional.")]
    partes, paso["historial_incluido"] = _con_historial(ctx, base, intentos, corto=False)
    text, call = ctx.modelo.con_partes(partes).pedir("completo", BR._texto(partes), json_out=False)
    paso["descripcion"] = "instrumento completo"
    nuevo, nota = codeextract.extract_code(text or "")
    if nuevo is None:
        limite = call.get("resultado") == "truncada"
        paso.update({"atribucion": "limite" if limite else "modelo",
                     "detalle": ("respuesta cortada por el límite de tokens: " if limite else "") + nota})
        return None
    paso.update({"atribucion": None, "detalle": nota or "ok"})
    return nuevo


FORMATO_DISPUTA = ("¿Qué está mal: el INSTRUMENTO o el VERIFICADOR? Respondé EXACTAMENTE:\n"
                   "DECISION: instrumento | verificador\nMOTIVO: <una frase>")
RE_DECISION = re.compile(r"DECISI[OÓ]N:\s*(instrumento|verificador)", re.I)


def _disputa(ctx, ev, falla, codigo_verificador) -> dict:
    partes = BR._contexto(ev["salida"]) + [
        ("coordinador", f"Las verificaciones del sistema pasan. Solo falla el verificador {falla['id']}, que escribiste "
                        "sin ver el instrumento:\n"),
        ("codigo", f"```python\n{codigo_verificador}```\n"),
        ("evidencia", f"Su reclamo: {falla['detalle']}" + (f" (campos: {', '.join(falla['campos'])})"
                                                            if falla["campos"] else "") + "\n"),
        ("coordinador", FORMATO_DISPUTA)]
    text, _ = ctx.modelo.con_partes(partes).pedir("disputa", BR._texto(partes), json_out=False, corto=True)
    m = RE_DECISION.search(text or "")
    return {"verificador": falla["id"], "decision": m.group(1).lower() if m else "ilegible",
            "respuesta": (text or "")[:300]}


# Episodio ---------------------------------------------------------------------------------------------------

def correr_suite(ctx, brazo: str, defecto: B.Defecto, verificadores: list[dict], oraculo_lineas,
                 codigos_verificadores: dict) -> dict:
    """Brazos B, B*, C y D con el protocolo v2."""
    alm, guardia = ctx.almacen, ctx.modelo.guardia
    nombre = f"ventanas_{brazo.replace('*', 'x')}"
    inst = alm.save(nombre, defecto.codigo, B.MANIFIESTO, author="banco")
    impugnados: set[str] = set()
    disputados: set[str] = set()

    def suite(iid):
        guardia.antes_de_sandbox()
        return V.evaluar(ctx.ent, alm, iid, B.PARAMS_VISIBLES, [v for v in verificadores if v["id"] not in impugnados])

    ev = suite(inst["id"])
    vigente = {"id": inst["id"], "codigo": defecto.codigo}
    pasos, intentos, rechazados, disputas = [], [], {}, []
    firmas, motivo_fin = [], "sin_fallas"
    while V.fallas(ev):
        fs = V.fallas(ev)
        solo_modelo = all(f["autor"] == "modelo" for f in fs)
        pendiente = next((f for f in fs if f["id"] not in disputados), None) if solo_modelo else None
        try:
            if pendiente is not None:
                disputados.add(pendiente["id"])
                d = _disputa(ctx, ev, pendiente, codigos_verificadores.get(pendiente["id"], "(no disponible)"))
                disputas.append(d)
                if d["decision"] == "verificador":
                    impugnados.add(pendiente["id"])
                    ev = {**ev, "checks": {k: c for k, c in ev["checks"].items() if k not in impugnados}}
                continue
            if brazo in BR.ORACULO:
                u = P.unidad_en(vigente["codigo"], oraculo_lineas[0]) if oraculo_lineas else None
                cands = [{"unidad": u, "motivo": "oráculo"}] if u else []
            else:
                cands = L.localizar(vigente["codigo"], fs, k=3)
            if not cands:
                motivo_fin = "sin_candidatas"
                break
            paso = {"candidatas": [c["unidad"].lineno for c in cands], "fallas_antes": sorted(f["id"] for f in fs)}
            nuevo = (_micro if brazo in BR.MICRO else _completo)(ctx, vigente["codigo"], cands, ev,
                                                                 brazo in BR.ORACULO, intentos, paso)
        except BR.Agotado:
            motivo_fin = "presupuesto"
            break
        intento = {"descripcion": paso.get("descripcion", "respuesta"), "diff": None, "fallas_despues": None}
        if nuevo is None:
            paso["resultado"] = "ensamblado_fallido"
            intento.update({"resultado": "inválido", "razon": paso.get("detalle", "")})
        elif nuevo.strip() == vigente["codigo"].strip():
            paso.update({"resultado": "sin_cambios", "atribucion": "modelo"})
            intento.update({"resultado": "sin cambios", "razon": "el código quedó idéntico al vigente"})
        elif _sha(nuevo) in rechazados:
            n = rechazados[_sha(nuevo)]
            paso.update({"resultado": "repetido", "atribucion": "modelo", "repite_intento": n})
            intento.update({"resultado": "repetido", "razon": f"es exactamente el mismo cambio del intento {n}, ya "
                                                               "rechazado: no se volvió a ejecutar"})
        else:
            nid = alm.save(nombre, nuevo, B.MANIFIESTO, author=f"modelo:{ctx.modelo.nombre}",
                           parent_id=vigente["id"])["id"]
            ev2 = suite(nid)
            ok, razon = V.umbral(ev, ev2)
            paso.update({"version": nid, "aceptado": ok, "razon": razon, "resultado": "aceptado" if ok else "rechazado",
                         "fallas_despues": sorted(f["id"] for f in V.fallas(ev2))})
            intento.update({"resultado": "aceptado" if ok else "rechazado", "razon": razon,
                            "diff": D.cambio(vigente["codigo"], nuevo, max_lineas=14)["diff"],
                            "fallas_despues": V.texto_fallas(ev2)[:700]})
            if ok:
                vigente, ev = {"id": nid, "codigo": nuevo}, ev2
            else:
                rechazados[_sha(nuevo)] = len(intentos) + 1
        pasos.append(paso)
        intentos.append(intento)
        firmas.append(V.firma(ev))
        if len(firmas) >= BR.CORTE_SIN_PROGRESO and len(set(firmas[-BR.CORTE_SIN_PROGRESO:])) == 1 and \
                not any(p.get("aceptado") for p in pasos[-BR.CORTE_SIN_PROGRESO:]):
            motivo_fin = "sin_progreso"
            break
    return {"vigente": vigente, "pasos": pasos, "motivo_fin": motivo_fin, "disputas": disputas,
            "impugnados": sorted(impugnados), "fallas_finales": sorted(f["id"] for f in V.fallas(ev)),
            "invalidos": ev["invalidos"]}
