"""Coordinador de misión (AGENTE_DATOS.md, §4): conduce el flujo, decide con una regla fija y registra cada decisión.

F0 recordar: memoria de la conexión + huellas actuales (estructura y datos, por tabla)
F1 descubrir: perfiles y relaciones SOLO de lo nuevo o cambiado (cartógrafo, sin modelo)
F2 comprender: M1 para las tablas sin ficha vigente (lotes de hasta 3)
F3 requisitos de la pregunta por regla (si no se pueden determinar sin ambigüedad: aclaración y abstención) →
   elegir: M2 + COBERTURA de los requisitos (tiempo, entidad, medida) antes de avanzar (o una consulta aprendida
   para la misma pregunta: plantilla que se vuelve a ejecutar y verificar)
F4 consultar: M3 → normalizaciones de forma registradas → orden y límite completados por regla (registrados) →
   requisitos del plan ANTES de ejecutar → compilación MEDIR (o SQL libre) → verificación técnica previa →
   intermediario
F5 verificar: técnica del resultado y semántica de la respuesta (separadas)
F6 redactar: M4 → fidelidad (o respuesta determinista si M4 falla)
F7 registrar: memoria (solo lo verificado como verificado)
F8 informe

Regla de decisión: hasta 2 intentos por microtarea con el motivo del rechazo; una falla técnica o semántica vuelve
a M3 con el motivo; agotados los intentos o el presupuesto de llamadas, la misión se ABSTIENE (no es un fracaso
oculto: el informe dice por qué).

Estado de la misión (distinto del NIVEL de la respuesta):
- completa: el circuito llegó a un resultado (verificada, incierta o abstención por verificación o presupuesto);
- interrumpida: el ENTORNO cortó la misión (Ollama, guardia de memoria, Ctrl-C): no hay resultado y no debe leerse
  como una abstención del agente. No hay reanudación: una misión interrumpida no se continúa;
- error: falla del coordinador (por ejemplo, un pedido que no entra en el contexto) o del cartógrafo;
- bloqueada: la conexión no pasó la verificación de solo lectura (no se llamó al modelo).
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from pathlib import Path

from ..explorer import knowledge as K
from . import VERSION
from . import cartografo as CA
from . import medir as MD
from . import microtareas as MT
from . import requisitos as RQ
from . import texto as TX
from . import verificador as VE

INTENTOS = 2
LOTE_M1 = 3


class SinPresupuesto(RuntimeError):
    pass


class Interrumpida(RuntimeError):
    """El entorno cortó la misión (Ollama, memoria, Ctrl-C). No es una respuesta del modelo."""


class ContextoExcedido(RuntimeError):
    """Un pedido no entra en el contexto del modelo: falla del coordinador, no se llama."""


def _j(v) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


class Coordinador:
    def __init__(self, cfg, broker, memoria, modelo, *, conexion: str, base: str, carpeta: Path,
                 max_llamadas: int = 8, log=print):
        self.cfg, self.broker, self.mem, self.modelo = cfg, broker, memoria, modelo
        self.conexion, self.base, self.raiz_carpeta = conexion, base, Path(carpeta)
        self.max_llamadas, self.log = max_llamadas, log

    # Registro -------------------------------------------------------------------------------------------------
    def _decidir(self, etapa: str, regla: str, porque: str, datos=None):
        self.mem.decision(self.mid, etapa, regla, porque, datos)
        with open(self.carpeta / "decisiones.jsonl", "a", encoding="utf-8") as fh:
            fh.write(_j({"etapa": etapa, "regla": regla, "porque": porque, "datos": datos}) + "\n")
        self.log(f"  [{etapa}] {regla}: {porque}")

    def _cabe(self, partes: list) -> bool:
        cabe = getattr(self.modelo, "cabe", None)
        return cabe is None or cabe("".join(t for _, t in partes))

    def _pedir(self, tipo: str, partes: list) -> str:
        if self.llamadas >= self.max_llamadas:
            raise SinPresupuesto(f"se agotó el presupuesto de {self.max_llamadas} llamadas al modelo")
        if not self._cabe(partes):
            raise ContextoExcedido(f"el pedido {tipo} no entra en el contexto del modelo con la reserva de respuesta")
        # La semilla depende de la pregunta, la microtarea y su número de pedido: la misma microtarea de la misma
        # pregunta usa la misma semilla con memoria o sin ella (comparaciones pareadas).
        n = self.por_tipo.get(tipo, 0)
        self.llamadas += 1
        self.por_tipo[tipo] = n + 1
        semilla = int(hashlib.sha256(f"{self.pnorm}|{tipo}|{n}".encode()).hexdigest(), 16) % 2 ** 31
        texto = "".join(t for _, t in partes)
        t0 = time.monotonic()
        try:
            resp, call = self.modelo.pedir(f"agente:{tipo}", texto, json_out=False, corto=True, seed=semilla)
        except (Interrumpida, ContextoExcedido) as e:
            with open(self.carpeta / "llamadas.jsonl", "a", encoding="utf-8") as fh:
                fh.write(_j({"mision": self.mid, "tipo": tipo, "semilla": semilla, "partes": partes,
                             "respuesta": None, "ms": int((time.monotonic() - t0) * 1000),
                             "llamada": {"resultado": type(e).__name__, "error": str(e)[:300]}}) + "\n")
            raise
        with open(self.carpeta / "llamadas.jsonl", "a", encoding="utf-8") as fh:
            fh.write(_j({"mision": self.mid, "tipo": tipo, "semilla": semilla, "partes": partes, "respuesta": resp,
                         "ms": int((time.monotonic() - t0) * 1000), "llamada": call}) + "\n")
        for k, v in (("prompt", "tokens_prompt"), ("generados", "tokens_generados"), ("modelo_ms", "reloj_ms")):
            self.tokens[k] += int((call or {}).get(v) or 0)
        if (call or {}).get("resultado") == "truncada":
            self.truncadas += 1
        if resp is None:
            raise Interrumpida(f"el modelo no respondió ({(call or {}).get('error', 'sin detalle')})")
        return resp

    def _run(self, sql: str, herramienta: str, pregunta: str = ""):
        self.consultas += 1
        return self.broker.run(sql, mission_id=self.mid, origin="agente", tool=herramienta, question=pregunta,
                               rationale="coordinador")

    # Misión ---------------------------------------------------------------------------------------------------
    def ejecutar(self, pregunta: str) -> dict:
        self.mid = time.strftime("A%Y%m%d-%H%M%S-") + secrets.token_hex(3)
        self.carpeta = self.raiz_carpeta / self.mid
        self.carpeta.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.pregunta, self.pnorm = pregunta, TX.normalizar_pregunta(pregunta)
        self.llamadas, self.consultas, self.por_tipo = 0, 0, {}
        self.tokens, self.truncadas = {"prompt": 0, "generados": 0, "modelo_ms": 0}, 0
        self.uso_memoria = {"mediciones_reutilizadas": [], "relaciones_reutilizadas": 0, "fichas_reutilizadas": [],
                            "glosario_mostrado": [], "consulta_aprendida": None}
        self.nuevo = {"tablas_perfiladas": [], "relaciones_medidas": 0, "fichas_nuevas": [], "terminos": []}
        self.mem.mision_nueva(self.mid, self.conexion, pregunta)
        t0 = time.monotonic()
        estado = self.broker.verify()
        if not estado.get("verified"):
            return self._cerrar("bloqueada", "abstencion", None, [f"conexión no verificada: {estado.get('reason')}"],
                                [], t0)
        self.cart = CA.Cartografo(self.broker, self.mem, self.conexion, self.mid)
        try:
            self._recordar_y_descubrir()
            self._comprender()
            return self._responder(t0)
        except SinPresupuesto as e:
            self.mem.fallo(self.conexion, self.mid, "presupuesto", str(e))
            self._decidir("fin", "presupuesto", str(e))
            return self._cerrar("completa", "abstencion", None, [str(e)], [], t0)
        except CA.ErrorCartografo as e:
            self.mem.fallo(self.conexion, self.mid, "cartografo", str(e))
            return self._cerrar("error", "sin_resultado", None, [f"cartógrafo: {e}"], [], t0)
        except ContextoExcedido as e:
            self.mem.fallo(self.conexion, self.mid, "contexto", str(e))
            self._decidir("fin", "error_contexto", str(e))
            return self._cerrar("error", "sin_resultado", None, [str(e)], [], t0)
        except (Interrumpida, KeyboardInterrupt) as e:
            motivo = str(e) or "interrumpida por el operador (Ctrl-C o límite de tiempo)"
            self._decidir("fin", "interrumpida", f"{motivo}: SIN resultado; no es una abstención del agente")
            return self._cerrar("interrumpida", "sin_resultado", None, [motivo], [], t0)

    # F0 y F1 --------------------------------------------------------------------------------------------------
    def _recordar_y_descubrir(self):
        self.schema = self.cart.esquema()
        tablas = self.schema["tablas"]
        self.huellas, self.huellas_datos, self.perfiles, cambios = {}, {}, {}, {}
        for t, td in tablas.items():
            he = CA.huella_estructura(td)
            hd = self.cart.huella_datos(t, td)
            self.huellas[t], self.huellas_datos[t] = he, hd.get("huella")
            med = self.mem.medicion(self.conexion, t)
            if med is None:
                motivo = "nueva"
            elif med["huella_estructura"] != he:
                motivo = "estructura"
            elif hd.get("huella") is None or med["huella_datos"] != hd["huella"]:
                motivo = "datos"
            else:
                motivo = None
            cambios[t] = motivo
            if motivo is None:
                self.perfiles[t] = med["perfil"]
                self.perfiles[t]["_muestra"] = med["muestra"]
                self.uso_memoria["mediciones_reutilizadas"].append(t)
                self._decidir("F0", "medicion_vigente", f"{t}: misma estructura y mismos datos que en "
                                                        f"{med['mision_id']}: se reutiliza su perfil")
                continue
            porque = {"nueva": "tabla sin mediciones previas",
                      "estructura": "cambió la ESTRUCTURA desde la última medición",
                      "datos": ("cambiaron los DATOS (misma estructura): las mediciones anteriores no siguen "
                                "actualizadas" if hd.get("huella") else hd.get("motivo", "huella no calculable"))}[motivo]
            self._decidir("F1", f"medir_{motivo}", f"{t}: {porque}")
            perfil, muestra, ids = self.cart.perfilar(t, td)
            self.mem.guardar_medicion(self.conexion, t, he, hd.get("huella"), perfil, muestra, ids, self.mid)
            self.perfiles[t] = perfil
            self.perfiles[t]["_muestra"] = muestra
            self.nuevo["tablas_perfiladas"].append(t)
            if motivo == "estructura":
                f = self.mem.ficha(self.conexion, t)
                if f is not None:
                    self.mem.marcar_ficha(self.conexion, t, "revisar", f"cambió la estructura (misión {self.mid})")
                    self._decidir("F1", "ficha_a_revisar", f"{t}: su ficha F{f['id']} describía otra estructura")
        # Tablas que ya no existen.
        for f in self.mem.fichas(self.conexion):
            if f["tabla"] not in tablas:
                self.mem.marcar_ficha(self.conexion, f["tabla"], "obsoleta", f"la tabla no existe ({self.mid})")
                self.mem.retirar_medicion(self.conexion, f["tabla"])
                self.mem.retirar_relaciones(self.conexion, f["tabla"])
                self._decidir("F1", "tabla_eliminada", f"{f['tabla']}: ficha obsoleta")
        # Relaciones: vigentes si las dos tablas tienen los mismos datos y estructura que cuando se midieron.
        previas = {(r["tabla"], r["columna"], r["tabla_ref"], r["columna_ref"]): r for r in
                   self.mem.relaciones(self.conexion)}
        self.rels = []
        cands = self.cart.candidatas(self.schema)
        for c in cands:
            k = (c["tabla"], c["columna"], c["tabla_ref"], c["columna_ref"])
            p = previas.get(k)
            hd_act = {c["tabla"]: self.huellas_datos[c["tabla"]], c["tabla_ref"]: self.huellas_datos[c["tabla_ref"]]}
            if (p is not None and p["huellas_datos"] == hd_act and None not in hd_act.values()
                    and not cambios[c["tabla"]] and not cambios[c["tabla_ref"]]):
                self.rels.append(p)
                self.uso_memoria["relaciones_reutilizadas"] += 1
                continue
            r = self.cart.medir_relacion(c, self.schema, self.perfiles)
            r["huellas_datos"] = hd_act
            r["id"] = self.mem.guardar_relacion(self.conexion, r, self.mid)
            self.rels.append(r)
            self.nuevo["relaciones_medidas"] += 1
        actuales = {(c["tabla"], c["columna"], c["tabla_ref"], c["columna_ref"]) for c in cands}
        for k in previas:
            if k not in actuales:
                self.mem.retirar_relacion(self.conexion, *k)
                self._decidir("F1", "relacion_retirada", f"{k[0]}.{k[1]} → {k[2]}.{k[3]} ya no es candidata")
        self._decidir("F1", "relaciones", f"{self.uso_memoria['relaciones_reutilizadas']} reutilizadas, "
                                          f"{self.nuevo['relaciones_medidas']} medidas")

    # F2 -------------------------------------------------------------------------------------------------------
    def _comprender(self):
        self.fichas = {}
        pendientes = []
        for t in sorted(self.schema["tablas"]):
            f = self.mem.ficha(self.conexion, t, estados=("vigente",))
            if f is not None and f["huella_estructura"] == self.huellas[t]:
                self.fichas[t] = f
                self.uso_memoria["fichas_reutilizadas"].append(t)
            else:
                pendientes.append(t)
        if self.uso_memoria["fichas_reutilizadas"]:
            self._decidir("F2", "fichas_vigentes", "se reutilizan las fichas de "
                                                   + ", ".join(self.uso_memoria["fichas_reutilizadas"]))
        def lote_de(tablas):
            return [{"tabla": t, "t": self.schema["tablas"][t], "perfil": self.perfiles[t],
                     "muestra": self.perfiles[t].get("_muestra"), "evidencia": f"P{self.perfiles[t]['consulta_id']}"}
                    for t in tablas]
        i = 0
        while i < len(pendientes):
            maximo = n = min(LOTE_M1, len(pendientes) - i)
            while n > 1 and not self._cabe(MT.prompt_m1(lote_de(pendientes[i:i + n]), self.rels)):
                n -= 1          # el lote no entra en el contexto: menos tablas por pedido
            lote = lote_de(pendientes[i:i + n])
            i += n
            nombres = [x["tabla"] for x in lote]
            self._decidir("F2", "describir", f"M1 para {', '.join(nombres)}: sin ficha vigente para su estructura"
                          + (f" (lote reducido de {maximo} a {n} tablas para entrar en el contexto)" if n < maximo
                             else ""))
            retro = ""
            for intento in range(INTENTOS):
                partes = MT.prompt_m1(lote, self.rels)
                if retro:
                    partes.append(("coordinador", f"\nTU RESPUESTA ANTERIOR SE RECHAZÓ: {retro}\nCorregila.\n"))
                resp = self._pedir("M1", partes)
                try:
                    out = MT.parsear_m1(resp, lote, self.rels)
                except MT.Rechazo as e:
                    retro = e.motivo
                    self._decidir("F2", "m1_rechazada" + ("_ambigua" if e.ambigua else ""), e.motivo,
                                  {"intento": intento + 1})
                    continue
                for t, d in out.items():
                    fid = self.mem.guardar_ficha(self.conexion, t, self.huellas[t], d["fila"], d["roles"],
                                                 d["problemas"], {"perfil": f"P{self.perfiles[t]['consulta_id']}",
                                                                  "muestra": self.perfiles[t].get("_muestra", {}).get(
                                                                      "consulta_id")}, self.mid)
                    self.fichas[t] = self.mem.ficha(self.conexion, t)
                    self.nuevo["fichas_nuevas"].append(t)
                    self._decidir("F2", "ficha_guardada", f"{t}: F{fid} (interpretación del modelo; roles "
                                                          "compatibles con tipos y perfiles)",
                                  {"problemas": d["problemas"]})
                break
            else:
                self.mem.fallo(self.conexion, self.mid, "F2", f"M1 rechazada {INTENTOS} veces para "
                                                              f"{', '.join(nombres)}: {retro}")
                self._decidir("F2", "sin_ficha", f"{', '.join(nombres)} quedan sin ficha: {retro}")

    # F3 a F8 --------------------------------------------------------------------------------------------------
    def _recuerdos(self) -> dict:
        terms = set(TX.terminos(self.pregunta)) | ({TX.raiz(TX.entidad(self.pregunta))}
                                                   if TX.entidad(self.pregunta) else set())
        glos = [g for g in self.mem.glosario(self.conexion) if g["raiz"] in terms]
        self.uso_memoria["glosario_mostrado"] = [f"G{g['id']} {g['raiz']}→{g['objeto']} ({g['origen']})" for g in glos]
        return {"glosario": glos, "fallos": self.mem.fallos(self.conexion, 3)}

    def _responder(self, t0) -> dict:
        self.req = RQ.deducir(self.pregunta)
        if self.req["aclaraciones"]:
            motivo = "aclaración necesaria: " + "; ".join(self.req["aclaraciones"])
            self.mem.fallo(self.conexion, self.mid, "F3", motivo)
            self._decidir("F3", "aclaracion_necesaria", motivo + " (no se inventan parámetros; no se llama al modelo "
                                                                  "para elegir tablas)")
            return self._cerrar("completa", "abstencion", None, [motivo], [], t0)
        self._decidir("F3", "requisitos", "deducidos por regla: " + ("; ".join(self.req["deducido"]) or "ninguno"))
        recuerdos = self._recuerdos()
        aprendida = self.mem.consulta_aprendida(self.conexion, self.pnorm)
        retro: list[str] = []
        chequeos_finales, nivel, dudas = [], "abstencion", []
        if aprendida and aprendida["medir_texto"]:
            self.uso_memoria["consulta_aprendida"] = f"Q{aprendida['id']} ({aprendida['nivel']}, {aprendida['mision_id']})"
            self._decidir("F3", "consulta_aprendida", f"la misma pregunta se respondió en {aprendida['mision_id']} "
                                                      f"(nivel {aprendida['nivel']}): se vuelve a ejecutar y verificar "
                                                      "con los datos y el esquema actuales")
            try:
                plan = MD.parsear(aprendida["medir_texto"])
                faltan = RQ.faltantes(self.req, plan, self.schema, aprendida["tablas"], self.fichas)
                r = ({"nivel": "rechazada_semantica", "motivos": [m for m, _ in faltan]} if faltan else
                     self._intentar({"tipo": "medir", "plan": plan, "texto": aprendida["medir_texto"],
                                     "interpreta": {}}, aprendida["tablas"]))
            except MD.Aclaracion as e:
                r = {"nivel": "rechazada_tecnica", "motivos": [f"la consulta aprendida ya no se puede leer: {e}"]}
            if r["nivel"] in ("verificada", "incierta"):
                self.mem.usar_consulta(aprendida["id"])
                return self._redactar_y_cerrar(r, aprendida["tablas"], t0)
            retro.append("; ".join(r["motivos"]))
            self._decidir("F3", "consulta_aprendida_rechazada", "; ".join(r["motivos"])[:300])
        tablas = self._elegir(recuerdos)
        if tablas is None:
            return self._cerrar("completa", "abstencion", None, ["no se pudieron elegir tablas"], [], t0)
        ultimo, ultimo_motivo = None, None
        for intento in range(INTENTOS):
            resp = self._pedir("M3", MT.prompt_m3(self.pregunta, tablas, self.schema, self.fichas, self.perfiles,
                                                  self.rels, recuerdos, retro, self.req))
            try:
                prop = MT.parsear_m3(resp, self._resolver(tablas))
            except MT.Rechazo as e:
                retro.append(MT.retro_m3(resp, e.motivo))
                ultimo_motivo = e.motivo
                self._decidir("F4", "m3_rechazada" + ("_ambigua" if e.ambigua else ""), e.motivo)
                continue
            if prop["tipo"] == "medir":
                plan = prop["plan"]
                if plan["normalizaciones"]:
                    self._decidir("F4", "forma_normalizada", "; ".join(plan["normalizaciones"]) + " (el contenido se "
                                                                                                  "valida igual)")
                completado = []
                if self.req["orden"] and plan["orden"] is None:
                    plan["orden"] = self.req["orden"]
                    completado.append(f"ORDEN: {plan['orden']}")
                if self.req["limite"] and plan["limite"] is None:
                    plan["limite"] = self.req["limite"]
                    completado.append(f"LIMITE: {plan['limite']}")
                if completado:
                    self._decidir("F4", "completado_por_regla", f"{', '.join(completado)} (deducidos de la pregunta: "
                                                                f"{'; '.join(self.req['deducido'])})")
                prop["texto"] = MD.texto_canonico(plan)
                faltan = RQ.faltantes(self.req, plan, self.schema, tablas, self.fichas)
                if faltan:
                    motivo = "; ".join(m for m, _ in faltan)
                    retro.append(MT.retro_m3(resp, motivo, "; ".join(c for _, c in faltan)))
                    self._decidir("F4", "m3_requisito_incompleto", motivo + " (no se ejecuta nada)")
                    ultimo, ultimo_motivo = None, motivo
                    continue
            ultimo = self._intentar(prop, tablas)
            if ultimo["nivel"] in ("verificada", "incierta"):
                return self._redactar_y_cerrar(ultimo, tablas, t0)
            ultimo_motivo = "; ".join(ultimo["motivos"])
            retro.append(MT.retro_m3(resp, ultimo_motivo[:600]))
            self._decidir("F5", ultimo["nivel"], "; ".join(ultimo["motivos"])[:400], {"intento": intento + 1})
        motivos = [ultimo_motivo] if ultimo_motivo else ["sin propuesta válida"]
        self.mem.fallo(self.conexion, self.mid, "F4-F5", "; ".join(motivos)[:500], {"tablas": tablas})
        self._decidir("fin", "abstencion", f"{INTENTOS} propuestas rechazadas: {'; '.join(motivos)[:300]}")
        return self._cerrar("completa", "abstencion", None, motivos, (ultimo or {}).get("chequeos", []), t0)

    def _elegir(self, recuerdos) -> list[str] | None:
        fichas = list(self.fichas.values())
        semilla = int(hashlib.sha256(self.pnorm.encode()).hexdigest(), 16) % 2 ** 31
        retro = ""
        for intento in range(INTENTOS):
            partes = MT.prompt_m2(self.pregunta, fichas, self.rels, recuerdos, semilla)
            if retro:
                partes.append(("coordinador", f"\nTU RESPUESTA ANTERIOR SE RECHAZÓ: {retro}\n"))
            resp = self._pedir("M2", partes)
            try:
                out = MT.parsear_m2(resp, self.schema, self.rels)
            except MT.Rechazo as e:
                retro = e.motivo
                self._decidir("F3", "m2_rechazada" + ("_ambigua" if e.ambigua else ""), e.motivo)
                continue
            falta = RQ.cobertura(self.req, out["tablas"], self.fichas, self.rels)
            if falta:
                retro = (f"elegiste {', '.join(out['tablas'])}; " + " ".join(falta)
                         + " Elegí de nuevo (hasta 3 tablas conectadas) cubriendo lo que pide la pregunta.")
                self._decidir("F3", "m2_cobertura_incompleta", " ".join(falta)[:500])
                continue
            self._decidir("F3", "tablas_elegidas", f"{', '.join(out['tablas'])} (orden de presentación barajado con "
                                                   f"semilla {semilla}; el coordinador no sugirió ninguna)",
                          {"por_que": out["por_que"]})
            return out["tablas"]
        self.mem.fallo(self.conexion, self.mid, "F3", retro)
        return None

    def _resolver(self, tablas: list[str]):
        """Completa una columna sin tabla SOLO si está en exactamente una de las tablas elegidas."""
        def resolver(nombre: str) -> str:
            hits = [f"{t}.{c['nombre']}" for t in tablas for c in self.schema["tablas"][t]["columnas"]
                    if c["nombre"].lower() == nombre.lower()]
            if len(hits) == 1:
                return hits[0]
            raise MD.Aclaracion(f"«{nombre}» debe escribirse como tabla.columna" + (
                f": está en varias tablas elegidas ({', '.join(hits)})" if hits else
                f": no es una columna de las tablas elegidas ({', '.join(tablas)})"))
        return resolver

    def _intentar(self, prop: dict, tablas: list[str]) -> dict:
        """Una propuesta → verificación técnica previa → ejecución → técnica posterior → semántica."""
        chequeos = []
        if prop["tipo"] == "medir":
            try:
                c = MD.compilar(prop["plan"], self.schema, self.rels)
            except MD.Aclaracion as e:
                return {"nivel": "rechazada_tecnica", "motivos": [f"MEDIR no compila: {e}"], "chequeos": []}
            ajenas = [t for t in c["tablas"] if t not in tablas]
            if ajenas:
                chequeos.append(VE.ch("tablas_elegidas", "tecnica", "no_verificable",
                                      f"el plan necesita además {', '.join(ajenas)} para unir las tablas"))
            sql = c["sql"]
        else:
            c = None
            sql = prop["sql"]
            chequeos += VE.previa_sql(sql, self.schema, self.base, tablas)
            if any(x["estado"] == "falla" for x in chequeos):
                return {"nivel": "rechazada_tecnica", "motivos": [f"{x['id']}: {x['detalle']}" for x in chequeos
                                                                  if x["estado"] == "falla"], "chequeos": chequeos}
        chequeos.append(VE.costo(self.broker, sql, self.cfg.explorer_max_join_size, self.mid))
        self.consultas += 1
        if chequeos[-1]["estado"] == "falla":
            return {"nivel": "rechazada_tecnica", "motivos": [chequeos[-1]["detalle"]], "chequeos": chequeos}
        o = self._run(sql, prop["tipo"], self.pregunta)
        chequeos += VE.posterior(o)
        if any(x["estado"] == "falla" for x in chequeos):
            n, motivos = VE.nivel(chequeos)
            return {"nivel": n, "motivos": motivos, "chequeos": chequeos, "sql": sql}
        if c is not None:
            chequeos += VE.invariantes(lambda q, h: self._run(q, h, "contraprueba"), c, o)
            chequeos += VE.semantica(self.pregunta, c, self.schema, self.fichas, self.mem.glosario(self.conexion),
                                     self.perfiles, self.req)
        else:
            chequeos.append(VE.ch("invariantes", "tecnica", "no_verificable", "consulta SQL libre: el coordinador no "
                                                                              "puede derivar contrapruebas"))
            chequeos += VE.semantica_sql_libre(self.pregunta)
        n, motivos = VE.nivel(chequeos)
        return {"nivel": n, "motivos": motivos, "chequeos": chequeos, "sql": sql, "compilado": c, "propuesta": prop,
                "resultado": o}

    def _redactar_y_cerrar(self, r: dict, tablas: list[str], t0) -> dict:
        o, c = r["resultado"], r.get("compilado")
        ng = len(c["gby"]) if c else max(0, len(o.columns) - 1)
        filas = o.rows[:10]
        respuesta, fid = None, None
        retro = ""
        for intento in range(INTENTOS):
            partes = MT.prompt_m4(self.pregunta, o.columns, filas)
            if retro:
                partes.append(("coordinador", f"\nTU RESPUESTA ANTERIOR SE RECHAZÓ: {retro}\n"))
            try:
                texto = MT.parsear_m4(self._pedir("M4", partes))
            except MT.Rechazo as e:
                retro = e.motivo
                continue
            except SinPresupuesto as e:
                retro = str(e)
                break
            fid = VE.fidelidad(texto, o.columns, filas, ng)
            if fid["estado"] == "ok":
                respuesta = texto
                break
            retro = fid["detalle"]
            self._decidir("F6", "m4_rechazada", fid["detalle"])
        if respuesta is None:
            respuesta = "Resultado: " + "; ".join(f"{k} = {v}" for k, v in zip(o.columns, filas[0]))
            fid = VE.ch("S6_fidelidad", "semantica", "ok", f"respuesta determinista del coordinador ({retro})")
            self._decidir("F6", "respuesta_determinista", f"la redacción del modelo no pasó: {retro}")
        chequeos = r["chequeos"] + [fid]
        nivel, dudas = VE.nivel(chequeos)
        if nivel.startswith("rechazada"):
            nivel = "abstencion"
        estructurada = {"grupo": list(filas[0][:ng]), "valor": filas[0][ng] if len(filas[0]) > ng else None}
        self._registrar(r, tablas, nivel, chequeos)
        self._decidir("fin", nivel, "; ".join(dudas)[:400] or "todos los chequeos técnicos y semánticos pasaron")
        return self._cerrar("completa", nivel, {"texto": respuesta, "estructurada": estructurada, "sql": r["sql"],
                                                "consulta_id": o.query_id}, dudas, chequeos, t0)

    def _registrar(self, r: dict, tablas: list[str], nivel: str, chequeos: list[dict]):
        prop, c = r["propuesta"], r.get("compilado")
        usadas = c["tablas"] if c else tablas
        h = K.result_hash(r["resultado"].columns, r["resultado"].rows)
        if nivel in ("verificada", "incierta"):
            self.mem.guardar_consulta(self.conexion, self.pregunta, self.pnorm, usadas,
                                      prop.get("texto") if prop["tipo"] == "medir" else None, r["sql"],
                                      {t: self.huellas[t] for t in usadas}, h, nivel, self.mid)
        if nivel == "verificada" and c is not None:
            for t in usadas:
                if t in self.fichas:
                    self.mem.confirmar_ficha(self.fichas[t]["id"], self.mid)
            objetos = [f"{t}.{x}" for t, x in c["plan"]["medida"]["columnas"]] or [c["raiz"]]
            for raiz_ in TX.terminos(self.pregunta):
                self.mem.registrar_termino(self.conexion, raiz_, objetos[0], "verificada", self.mid)
                self.nuevo["terminos"].append(f"{raiz_}→{objetos[0]} (verificada)")
        elif nivel == "incierta":
            for k, v in (prop.get("interpreta") or {}).items():
                self.mem.registrar_termino(self.conexion, TX.raiz(k), v[:80], "declarada", self.mid)
                self.nuevo["terminos"].append(f"{TX.raiz(k)}→{v[:40]} (declarada)")

    def _cerrar(self, estado: str, nivel: str, respuesta, dudas: list[str], chequeos: list[dict], t0) -> dict:
        decisiones = self.mem.decisiones(self.mid)
        informe = {"version": VERSION, "mision": self.mid, "conexion": self.conexion, "pregunta": self.pregunta,
                   "modelo": getattr(self.modelo, "nombre", "?"), "estado": estado, "nivel": nivel,
                   "verificada_por_el_coordinador": nivel == "verificada", "respuesta": respuesta,
                   "dudas_o_motivos": dudas, "chequeos": chequeos, "uso_de_memoria": self.uso_memoria,
                   "nuevo": self.nuevo,
                   "costo": {"llamadas": self.llamadas, "por_microtarea": self.por_tipo,
                             "consultas": self.consultas + getattr(getattr(self, "cart", None), "consultas", 0),
                             "tokens": self.tokens, "respuestas_truncadas": self.truncadas,
                             "segundos": round(time.monotonic() - t0, 2)},
                   "rechazos": [f"{d['etapa']} {d['regla']}: {d['porque'][:200]}" for d in decisiones
                                if "rechazada" in d["regla"] or "incomplet" in d["regla"]],
                   "requisitos": {k: v for k, v in (getattr(self, "req", None) or {}).items()
                                  if k in ("deducido", "aclaraciones")},
                   "normalizaciones": [d["porque"] for d in decisiones if d["regla"] in ("forma_normalizada",
                                                                                         "completado_por_regla")],
                   "interrupciones_del_entorno": list(getattr(self.modelo, "interrupciones", [])),
                   "decisiones": len(decisiones)}
        self.mem.terminar_mision(self.mid, estado, nivel, informe)
        (self.carpeta / "informe.json").write_text(_j(informe))
        (self.carpeta / "informe.md").write_text(markdown(informe))
        return informe


def markdown(inf: dict) -> str:
    r = inf.get("respuesta") or {}
    out = [f"# Misión {inf['mision']} ({inf['version']})", "", f"**Pregunta:** {inf['pregunta']}", "",
           f"**Nivel:** {inf['nivel']} · estado {inf['estado']}", "",
           f"**Respuesta:** {r.get('texto', '— (se abstiene)')}", ""]
    if inf["dudas_o_motivos"]:
        out += ["## Lo que no se pudo verificar o por qué se abstuvo", ""] + [f"- {d}" for d in inf["dudas_o_motivos"]]
    out += ["", "## Chequeos", "", "| capa | chequeo | estado | detalle |", "|---|---|---|---|"]
    out += [f"| {c['capa']} | {c['id']} | {c['estado']} | {c['detalle'][:160]}"
            + (f" · respaldo: {c['respaldo'][:100]}" if c.get("respaldo") else "") + " |" for c in inf["chequeos"]]
    out += ["", "## Memoria", "", f"- Usado: {json.dumps(inf['uso_de_memoria'], ensure_ascii=False)}",
            f"- Nuevo: {json.dumps(inf['nuevo'], ensure_ascii=False)}", "",
            f"Costo: {json.dumps(inf['costo'], ensure_ascii=False)}"]
    return "\n".join(out) + "\n"
