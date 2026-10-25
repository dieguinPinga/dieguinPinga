"""Despachador de OH-DUO-05: lleva las decisiones de OH-DUO-04 al buzón de la ejecución correspondiente.

Es el único que escribe en <corrida>/ejecuciones/<ejecución>/instancia/resultados/<r>/buzon/mensajes.jsonl, desde el
anfitrión (en el sandbox ese buzón es de solo lectura). Garantías:
  - sin --enviar no escribe nada en el buzón: solo registra lo que habría hecho;
  - solo corridas marcadas por oh-duo-05/ejecutar.py (archivo .oh-duo-05): nunca las de producción de OH-DUO-01;
  - el texto tiene que estar formado SOLO por frases de las plantillas de OH-DUO-04 (coordinador.json) y, como
    único dato variable, nombres de pruebas con forma test_*: no puede llevar código ni la solución;
  - por omisión solo despacha `nueva_estrategia` (con --incluir-advertencias, también `advertencia`);
  - no despacha si quedan menos de `min_restantes` iteraciones del límite;
  - un id por decisión (ejecución + tipo + iteración); nunca lo escribe dos veces;
  - escribe una línea completa por vez, sin seguir enlaces, y lo registra en informes/<corrida>/despachos.jsonl.
Solo biblioteca estándar.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

AQUI = Path(__file__).resolve().parent
MARCA = ".oh-duo-05"
TIPOS = ("nueva_estrategia",)


def plantillas(oh_duo_04: Path) -> tuple[list[str], list[re.Pattern]]:
    cfg = json.loads((oh_duo_04 / "coordinador.json").read_text(encoding="utf-8"))
    frases, patrones = [], []
    for idioma in cfg["plantillas"].values():
        for clave, t in idioma.items():
            if "{pruebas}" in t:
                antes, despues = t.split("{pruebas}")
                patrones.append(re.compile(re.escape(antes) + r"(test_\w+(?:, test_\w+)*)" + re.escape(despues)))
            else:
                frases.append(t)
    return sorted(frases, key=len, reverse=True), patrones


def validar_texto(texto: str, frases: list[str], patrones: list[re.Pattern]) -> tuple[bool, str]:
    """El texto tiene que ser una secuencia de frases de plantilla separadas por un espacio."""
    resto = texto
    while resto:
        for f in frases:
            if resto.startswith(f):
                resto = resto[len(f):]
                break
        else:
            for p in patrones:
                m = p.match(resto)
                if m:
                    resto = resto[m.end():]
                    break
            else:
                return False, f"fragmento fuera de las plantillas: {resto[:60]!r}"
        resto = resto[1:] if resto.startswith(" ") else resto
        if resto and not any(resto.startswith(f) for f in frases) and not any(p.match(resto) for p in patrones):
            return False, f"fragmento fuera de las plantillas: {resto[:60]!r}"
    return bool(texto), "ok" if texto else "vacío"


def buzon_de(corrida: Path, ejecucion: str) -> Path | None:
    if not (corrida / MARCA).is_file():
        raise SystemExit(f"{corrida} no es una corrida de oh-duo-05 (falta {MARCA}): no se escribe en buzones de producción")
    candidatos = sorted((corrida / "ejecuciones" / ejecucion / "instancia" / "resultados").glob("*/buzon/mensajes.jsonl"))
    if not candidatos:
        return None
    b = candidatos[-1]
    p = b
    while p != corrida:                       # ningún componente puede ser un enlace
        if p.is_symlink():
            raise SystemExit(f"enlace en la ruta del buzón: {p}")
        p = p.parent
    return b


class Despachador:
    def __init__(self, corrida: Path, oh_duo_04: Path, registro: Path, enviar: bool, limite: int = 30,
                 min_restantes: int = 5, tipos: tuple[str, ...] = TIPOS):
        self.corrida, self.enviar, self.limite, self.min_restantes, self.tipos = corrida, enviar, limite, min_restantes, tipos
        self.frases, self.patrones = plantillas(oh_duo_04)
        self.registro = registro
        registro.parent.mkdir(parents=True, exist_ok=True)
        self.hechos = {json.loads(l)["id"] for l in registro.read_text().splitlines() if l.strip()} if registro.is_file() else set()

    def _anotar(self, fila: dict) -> dict:
        fila = {"momento": time.time(), "enviar": self.enviar, **fila}
        with open(self.registro, "a", encoding="utf-8") as f:
            f.write(json.dumps(fila, ensure_ascii=False) + "\n")
        return fila

    def despachar(self, ejecucion: str, d: dict) -> dict | None:
        if d.get("tipo") not in self.tipos:
            return None
        ident = f"{ejecucion}-{d['tipo']}-it{d.get('iteracion')}"
        base = {"id": ident, "ejecucion": ejecucion, "tipo": d["tipo"], "iteracion": d.get("iteracion")}
        if ident in self.hechos:
            return None
        self.hechos.add(ident)
        texto = d.get("mensaje") or ""
        if hashlib.sha256(texto.encode()).hexdigest() != d.get("mensaje_sha256"):
            return self._anotar({**base, "resultado": "rechazado", "motivo": "la huella no coincide con el mensaje"})
        ok, motivo = validar_texto(texto, self.frases, self.patrones)
        if not ok:
            return self._anotar({**base, "resultado": "rechazado", "motivo": motivo})
        if d.get("iteracion") is not None and self.limite - d["iteracion"] < self.min_restantes:
            return self._anotar({**base, "resultado": "sin_margen",
                                 "motivo": f"quedan {self.limite - d['iteracion']} iteraciones (< {self.min_restantes})"})
        if not self.enviar:
            return self._anotar({**base, "resultado": "simulado", "mensaje_sha256": d["mensaje_sha256"]})
        b = buzon_de(self.corrida, ejecucion)
        if b is None:
            return self._anotar({**base, "resultado": "sin_buzon", "motivo": "la ejecución todavía no tiene carpeta"})
        linea = json.dumps({"id": ident, "texto": texto, "sha256": d["mensaje_sha256"]}, ensure_ascii=False) + "\n"
        fd = os.open(b, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
        try:
            os.write(fd, linea.encode())          # una sola escritura por línea completa
        finally:
            os.close(fd)
        return self._anotar({**base, "resultado": "escrito_en_buzon", "buzon": str(b), "mensaje_sha256": d["mensaje_sha256"]})


def seguir(dir_decisiones: Path, desp: Despachador, intervalo: float = 0.3, termina=lambda: False) -> None:
    """Sigue en vivo decisiones.jsonl de cada ejecución que OH-DUO-04 va escribiendo, hasta que cierre todas."""
    estado: dict[str, list] = {}     # ejecución → [leído, pendiente]
    while not termina():
        for d in sorted(p for p in dir_decisiones.glob("*") if (p / "decisiones.jsonl").is_file()):
            leido, pendiente = estado.setdefault(d.name, [0, ""])
            with open(d / "decisiones.jsonl", encoding="utf-8") as f:
                f.seek(leido)
                pendiente += f.read()
                estado[d.name][0] = f.tell()
            *completas, estado[d.name][1] = pendiente.split("\n")
            for l in completas:
                if l.strip():
                    r = desp.despachar(d.name, json.loads(l))
                    if r:
                        print(f"   [{r['resultado']}] {r['id']}", flush=True)
        time.sleep(intervalo)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--corrida", type=Path, required=True, help="oh-duo-05/corridas/<nombre>")
    p.add_argument("--decisiones", type=Path, required=True, help="oh-duo-04/informes/<nombre> (en vivo)")
    p.add_argument("--oh-duo-04", type=Path, default=next((x for x in (AQUI.parent / "oh-duo-04", Path.home() / "oh-duo-04")
                                                           if (x / "coordinador.json").is_file()), AQUI.parent / "oh-duo-04"))
    p.add_argument("--enviar", action="store_true", help="escribir de verdad en el buzón (sin esto, solo registra)")
    p.add_argument("--incluir-advertencias", action="store_true")
    p.add_argument("--min-restantes", type=int, default=5)
    a = p.parse_args(argv)
    corrida = a.corrida.resolve()
    if not (corrida / MARCA).is_file():
        raise SystemExit(f"{corrida} no es una corrida de oh-duo-05")
    tipos = TIPOS + (("advertencia",) if a.incluir_advertencias else ())
    desp = Despachador(corrida, a.oh_duo_04, AQUI / "informes" / corrida.name / "despachos.jsonl", a.enviar,
                       min_restantes=a.min_restantes, tipos=tipos)
    print(f"== despachador {'ENVIANDO' if a.enviar else 'en simulación'} → {corrida}", flush=True)
    try:
        seguir(a.decisiones.resolve(), desp)
    except KeyboardInterrupt:
        print("== detenido")
    return 0


if __name__ == "__main__":
    sys.exit(main())
