"""Puente de mensajes de OH-DUO-05: buzón (solo lectura) → conversation.send_message() durante el mismo run().

Corre como hilo dentro de agente.py, en el sandbox. Reglas:
  - lee solo líneas completas del buzón (/buzon/mensajes.jsonl, montado en solo lectura);
  - cada línea es {"id", "texto", "sha256"}; si el formato o la huella no cierran, se rechaza;
  - deduplica por id: un id se entrega, se descarta o se rechaza UNA vez;
  - comprueba el estado BAJO EL CANDADO de la conversación (el mismo FIFOLock que usa run()) y entrega solo si
    está RUNNING; en otro estado (finished, paused, error, stuck…) descarta sin tocar nada, porque
    send_message() volvería FINISHED/STUCK a IDLE;
  - después de cada entrega, registra la primera acción posterior del agente (prueba de que el modelo la recibió).
No pausa ni reinicia la conversación: el contador de iteraciones del run() en curso sigue igual.
Registro: /estado/entregas.jsonl (una línea por entrega, descarte, rechazo, duplicado o acción posterior).
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path

PREFIJO = "oh-duo-04:"
ID_VALIDO = re.compile(r"[A-Za-z0-9_.:-]{1,80}")
MAX_TEXTO = 2000


class Puente:
    def __init__(self, conv, buzon: Path, registro: Path, intervalo: float = 0.5):
        self.conv, self.buzon, self.registro, self.intervalo = conv, buzon, registro, intervalo
        self._fin = threading.Event()
        self._hilo = threading.Thread(target=self._bucle, daemon=True, name="puente-oh-duo-05")
        self._leido, self._pendiente = 0, ""
        self.vistos: set[str] = set()
        self.esperando: dict[str, int] = {}          # id entregado → índice de su MessageEvent
        self.eventos: list[dict] = []

    # ---- ciclo
    def iniciar(self) -> None:
        self._hilo.start()

    def detener(self, espera: float = 3.0) -> None:
        self._fin.set()
        self._hilo.join(espera)
        try:
            self._revisar_posteriores()
        except Exception as e:   # nunca tumbar al agente
            self._anotar({"resultado": "error_del_puente", "detalle": repr(e)})

    def _bucle(self) -> None:
        while not self._fin.is_set():
            try:
                self._vuelta()
            except Exception as e:
                self._anotar({"resultado": "error_del_puente", "detalle": repr(e)})
            self._fin.wait(self.intervalo)

    # ---- trabajo
    def _vuelta(self) -> None:
        if self.buzon.is_file():
            with open(self.buzon, encoding="utf-8") as f:
                f.seek(self._leido)
                self._pendiente += f.read()
                self._leido = f.tell()
            *completas, self._pendiente = self._pendiente.split("\n")   # una línea a medio escribir espera
            for linea in completas:
                if linea.strip():
                    self._procesar(linea)
        self._revisar_posteriores()

    def _procesar(self, linea: str) -> None:
        try:
            m = json.loads(linea)
            ident, texto, huella = m["id"], m["texto"], m["sha256"]
            assert isinstance(ident, str) and ID_VALIDO.fullmatch(ident)
            assert isinstance(texto, str) and 0 < len(texto) <= MAX_TEXTO
            assert hashlib.sha256(texto.encode()).hexdigest() == huella
        except (ValueError, KeyError, TypeError, AssertionError):
            self._anotar({"resultado": "rechazado_formato", "linea": linea[:200]})
            return
        if ident in self.vistos:
            self._anotar({"id": ident, "resultado": "duplicado_ignorado"})
            return
        self.vistos.add(ident)
        with self.conv.state:                                  # mismo candado que run(): sin carreras
            estado = self.conv.state.execution_status
            if getattr(estado, "value", str(estado)) != "running":
                self._anotar({"id": ident, "sha256": huella, "resultado": "descartado",
                              "estado": getattr(estado, "value", str(estado))})
                return
            self.conv.send_message(texto, sender=PREFIJO + ident)
            k = len(self.conv.state.events) - 1
        self.esperando[ident] = k
        self._anotar({"id": ident, "sha256": huella, "resultado": "entregado", "evento": k,
                      "iteracion_al_entregar": self._iteracion(k)})

    def _revisar_posteriores(self) -> None:
        if not self.esperando:
            return
        with self.conv.state:
            eventos = list(self.conv.state.events)
        for ident, k in list(self.esperando.items()):
            for i in range(k + 1, len(eventos)):
                e = eventos[i]
                if type(e).__name__ == "ActionEvent" or (type(e).__name__ == "MessageEvent" and e.source == "agent"):
                    self._anotar({"id": ident, "resultado": "accion_posterior", "evento": i,
                                  "herramienta": getattr(e, "tool_name", None), "iteracion": self._iteracion(i, eventos)})
                    del self.esperando[ident]
                    break

    def _iteracion(self, hasta: int, eventos=None) -> int:
        """Iteraciones del agente (respuestas distintas del LLM) hasta el evento `hasta`, inclusive."""
        if eventos is None:
            with self.conv.state:
                eventos = list(self.conv.state.events)
        vistas = set()
        for e in eventos[: hasta + 1]:
            if type(e).__name__ in ("ActionEvent", "MessageEvent") and getattr(e, "source", None) == "agent":
                vistas.add(getattr(e, "llm_response_id", None) or id(e))
        return len(vistas)

    def _anotar(self, datos: dict) -> None:
        fila = {"momento": time.time(), **datos}
        self.eventos.append(fila)
        with open(self.registro, "a", encoding="utf-8") as f:
            f.write(json.dumps(fila, ensure_ascii=False) + "\n")

    def resumen(self) -> dict:
        cuenta = {}
        for e in self.eventos:
            cuenta[e["resultado"]] = cuenta.get(e["resultado"], 0) + 1
        return {"cuenta": cuenta, "sin_accion_posterior": sorted(self.esperando)}
