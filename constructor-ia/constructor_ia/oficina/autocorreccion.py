"""Autocorrección con memoria de la mejor versión (OFICINA.md, «Autocorrección»). Solo la usa la oficina.

Auditoría del circuito original (engine.Engine, sin cambios): en cada reintento el modelo recibe el contrato, los
ejemplos visibles, el ÚLTIMO código (aunque haya empeorado) y prompts.failure_report, que para un caso oculto dice
solo «Caso oculto caso N: fallo». Tres carencias, generales y no propias de ninguna misión:
1. el informe de un caso oculto no dice QUÉ parte del resultado falló;
2. una versión peor reemplaza a la mejor como punto de partida (después de un 0/9 se corrige el 0/9);
3. nada señala que el mismo caso falla intento tras intento, ni que el código devuelto es idéntico a uno anterior.

MotorConMemoria es una subclase de Engine que solo cambia QUÉ se le pasa a cada reintento; el prompt, el sandbox, la
validación, el registro de intentos y la aprobación (todas las pruebas, firmadas por el arnés) son los originales:
- PISTAS SEGURAS para un caso oculto fallido, sin valores esperados, argumentos ni salidas del caso:
  · las claves de primer nivel del resultado que tienen un valor incorrecto, solo si ese nombre ya figura en el
    contrato (la descripción de la misión) y solo cuando el caso esperaba un diccionario;
  · si devolvió claves que el contrato no pide, o algo que no es un diccionario;
  · «qué ejercita» el caso: una categoría escrita por el autor de la misión («categoria»), que describe la regla del
    contrato en juego, nunca un valor;
- MEJOR VERSIÓN: puntaje = casos aprobados del intento (0 si no se pudo ejecutar). La mejor es la de mayor puntaje
  (la primera, en un empate). Si el último intento quedó por DEBAJO de la mejor, el siguiente parte de la mejor, con
  su informe y un aviso de regresión explícito. Nada se borra: cada intento conserva su código y su resultado;
- ESTANCAMIENTO: avisa si un caso oculto falla en varios intentos seguidos o si el código es idéntico a uno anterior;
- AL TERMINAR: la mejor versión queda en «mejor/solucion.py» y «mejor/resumen.json» (también si la misión falla). La
  misión solo se aprueba si un intento pasa TODAS las pruebas; la verificación central vuelve a ejecutarla.
Lo que se le envió al modelo en cada reintento queda en «intento-N/retroalimentacion.md».
"""
from __future__ import annotations

import json

from ..engine import Engine


def _claves_contrato(descripcion: str, claves) -> list[str]:
    return [k for k in claves if isinstance(k, str) and (f"'{k}'" in descripcion or f'"{k}"' in descripcion)]


def pistas(spec: dict, resultado: dict | None, descripcion: str) -> dict[int, list[str]]:
    """{n del caso oculto fallido: [pistas seguras]}."""
    out: dict[int, list[str]] = {}
    casos = spec.get("cases", [])
    for c in (resultado or {}).get("cases", []):
        if c.get("ok") or c.get("visible") or c.get("kind") not in ("caso", "script"):
            continue
        n = c.get("n")
        if not isinstance(n, int) or not 1 <= n <= len(casos):
            continue
        caso, lista = casos[n - 1], []
        esperado = caso.get("expect")
        if c.get("status") == "fallo" and isinstance(esperado, dict):
            got = c.get("got")
            try:
                obtenido = json.loads(got) if isinstance(got, str) and not got.endswith("…") else None
            except ValueError:
                obtenido = None
            if isinstance(obtenido, dict):
                mal = [k for k in esperado if k not in obtenido or obtenido[k] != esperado[k]]
                nombradas = _claves_contrato(descripcion, mal)
                if nombradas:
                    lista.append("claves del resultado con un valor incorrecto: " + ", ".join(f"'{k}'" for k in nombradas)
                                 + (" (el resto de las claves está bien)" if len(nombradas) < len(esperado) else ""))
                if set(obtenido) - set(esperado):
                    lista.append("devolvió claves que el contrato no pide")
            elif got is not None and not str(got).endswith("…"):
                lista.append("el resultado no es un diccionario como pide el contrato")
        if caso.get("categoria"):
            lista.append(f"qué ejercita este caso: {caso['categoria']}")
        if lista:
            out[n] = lista
    return out


def _texto_pistas(p: dict[int, list[str]]) -> str:
    if not p:
        return ""
    return ("\nOrientación sobre los casos ocultos que fallaron (sin sus datos ni sus valores esperados):\n"
            + "\n".join(f"- caso oculto {n}: " + "; ".join(v) for n, v in sorted(p.items())))


class MotorConMemoria(Engine):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._estado: dict[str, dict] = {}

    # Ciclo ------------------------------------------------------------------------------------------------------
    def run(self, mid, cancel):
        try:
            estado = super().run(mid, cancel)
            st = self._estado.get(mid)
            if st and st["mejor"]:
                m = st["mejor"]
                if m["codigo"] is not None:
                    self.files.save(mid, "mejor/solucion.py", m["codigo"], "mejor", m["n"])
                self.files.save(mid, "mejor/resumen.json", json.dumps(
                    {"intento": m["n"], "aprobados": m["puntaje"], "total": m["total"], "estado_mision": estado,
                     "historial": [{"intento": h["n"], "aprobados": h["puntaje"], "total": h["total"],
                                    "base": h.get("base"), "identico_a": h.get("identico_a")} for h in st["historial"]],
                     "revisiones": [{k: v for k, v in r.items() if k not in ("mensajes", "respuesta")}
                                    for r in st.get("revisiones", [])]},
                    ensure_ascii=False, indent=2), "mejor", m["n"])
            return estado
        finally:
            self._estado.pop(mid, None)

    def _attempt(self, mid, n, max_attempts, description, spec, tests_py, last_code, report, budget, cancel):
        st = self._estado.setdefault(mid, {"mejor": None, "ultimo": None, "historial": [], "fallas": {}})
        base_n = None
        if n > 1 and st["ultimo"] is not None:
            last_code, report, base_n = self._preparar(st)
            report = self._complementar(mid, n, st, description, spec, last_code, report, cancel)
            self.files.save(mid, f"intento-{n}/retroalimentacion.md",
                            f"Punto de partida: intento {base_n}\n\n{report}", "retroalimentacion", n)
        out = super()._attempt(mid, n, max_attempts, description, spec, tests_py, last_code, report, budget, cancel)
        self._registrar(mid, n, spec, description, st, out, base_n)
        return out

    def _complementar(self, mid, n, st, description, spec, base_code, report, cancel) -> str:
        """Punto de extensión: otros roles (el revisor) pueden agregar texto al feedback del próximo intento."""
        return report

    # Interno ----------------------------------------------------------------------------------------------------
    def _registrar(self, mid, n, spec, description, st, out, base_n):
        fila = self.db.one("SELECT passed, total, test_status, result_json FROM attempts WHERE mission_id=? AND n=?",
                           (mid, n))
        ejecutado = fila is not None and fila["test_status"] in ("aprobadas", "fallidas")
        resultado = json.loads(fila["result_json"]) if ejecutado and fila["result_json"] else None
        total = (fila["total"] if ejecutado else None) or len(spec.get("cases", []))
        p = pistas(spec, resultado, description)
        codigo = out.get("code")
        entrada = {"n": n, "puntaje": (fila["passed"] or 0) if ejecutado else 0, "total": total, "codigo": codigo,
                   "informe": (out.get("report") or "") + _texto_pistas(p), "base": base_n,
                   "identico_a": next((h["n"] for h in st["historial"] if codigo is not None and h["codigo"] == codigo),
                                      None),
                   "fallidos": sorted(c["n"] for c in (resultado or {}).get("cases", [])
                                      if not c.get("ok") and not c.get("visible")),
                   "fallidos_todos": sorted(c["n"] for c in (resultado or {}).get("cases", []) if not c.get("ok")),
                   "ejecutado": ejecutado}
        for c in entrada["fallidos"]:
            st["fallas"].setdefault(c, []).append(n)
        st["historial"].append(entrada)
        st["ultimo"] = entrada
        if codigo is not None and (st["mejor"] is None or entrada["puntaje"] > st["mejor"]["puntaje"]):
            st["mejor"] = entrada

    def _preparar(self, st) -> tuple[str | None, str, int]:
        ult, mej = st["ultimo"], st["mejor"]
        avisos = []
        if mej is not None and ult["puntaje"] < mej["puntaje"]:
            base = mej
            avisos.append(f"ATENCIÓN, REGRESIÓN: tu intento {ult['n']} aprobó {ult['puntaje']} de {ult['total']} casos, "
                          f"MENOS que el intento {mej['n']} ({mej['puntaje']} de {mej['total']}). El coordinador conserva "
                          f"la mejor versión: abajo va la del intento {mej['n']} como punto de partida. Corregí solo lo "
                          "que falla en ella, sin reescribir lo que ya funciona.")
        else:
            base = ult if ult["codigo"] is not None else (mej or ult)
        if ult.get("identico_a"):
            avisos.append(f"Tu intento {ult['n']} devolvió exactamente el mismo código que el intento "
                          f"{ult['identico_a']}: las pruebas dan el mismo resultado. Hace falta cambiar la lógica, no "
                          "solo los comentarios.")
        persistentes = {c: ns for c, ns in st["fallas"].items() if len(ns) >= 2 and c in base["fallidos"]}
        for c, ns in sorted(persistentes.items()):
            avisos.append(f"El caso oculto {c} falló en los intentos {', '.join(map(str, ns))}: las correcciones "
                          "anteriores no cambiaron la regla responsable. Volvé a leer en el contrato la regla que ese "
                          "caso ejercita y comprobá que tu código la aplique tal cual, en el orden indicado.")
        informe = base["informe"] + ("\n\n" + "\n".join(avisos) if avisos else "")
        return base["codigo"], informe, base["n"]


class MotorConRevisor(MotorConMemoria):
    """MotorConMemoria + el rol REVISOR (oficina/revisor.py), que interviene solo ante un estancamiento."""

    def __init__(self, *a, revisor=None, **k):
        super().__init__(*a, **k)
        self.revisor = revisor

    def _complementar(self, mid, n, st, description, spec, base_code, report, cancel) -> str:
        from . import revisor as RV
        cfg = self.revisor
        if cfg is None:
            return report
        si, criterio = RV.debe_revisar([h for h in st["historial"] if h["ejecutado"]])
        if not si:
            return report
        revs = st.setdefault("revisiones", [])
        reg = {"antes_del_intento": n, "criterio": criterio, "estado": "omitida"}
        usados = sum(r.get("segundos", 0) for r in revs)
        tam = sum(len(m["content"]) for m in RV.mensajes(description, spec, base_code or "", report))
        if len([r for r in revs if r["estado"] != "omitida"]) >= cfg.max_revisiones:
            reg["motivo"] = f"límite de {cfg.max_revisiones} revisiones por misión"
        elif usados >= cfg.max_segundos:
            reg["motivo"] = f"límite de tiempo de revisión ({usados:.0f} s de {cfg.max_segundos:.0f} s)"
        elif base_code is None:
            reg["motivo"] = "no hay código candidato para revisar"
        elif tam / prompts_chars() > cfg.modelo.num_ctx - cfg.modelo.num_predict:
            reg["motivo"] = "el pedido al revisor no entra en el contexto"
        else:
            r = RV.revisar(self.ollama, self.gate, cfg, description, spec, base_code, report, cancel)
            reg.update({k: v for k, v in r.items() if k != "ok"}, estado="hecha" if r["ok"] else "fallida")
        revs.append(reg)
        self.files.save(mid, f"intento-{n}/revision.json", json.dumps(reg, ensure_ascii=False, indent=2,
                                                                      default=str), "revision", n)
        self._event(mid, "revision", f"Revisor antes del intento {n}: {reg['estado']}"
                    + (f" ({reg['motivo']})" if reg.get("motivo") else "") + f" · {criterio}")
        if reg["estado"] == "hecha":
            return report + "\n\n" + RV.texto_para_programador(reg["diagnostico"], cfg.max_caracteres)
        return report


def prompts_chars() -> float:
    from .. import prompts
    return prompts.CHARS_PER_TOKEN
