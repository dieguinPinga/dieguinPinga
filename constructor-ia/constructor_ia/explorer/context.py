"""Administración del contexto del modelo del Explorador (v0.4.2).

Ollama no ofrece un contador de tokens, así que el tamaño se ESTIMA de forma
conservadora y se CALIBRA con lo que informa el propio Ollama:

* estimate_tokens: cuenta cada dígito como un token (los tokenizadores Qwen
  separan los números dígito a dígito: precios, fechas e identificadores pesan
  mucho más que el texto), cada signo o carácter no ASCII como un token y las
  palabras por tramos cortos de letras.
* El mensaje completo incluye instrucciones, datos, historial, la plantilla de
  chat y el esquema JSON de salida.
* Se reserva espacio para la respuesta (num_predict) y un margen.
* Si Ollama informa un contexto excedido, se aprende el factor real (nunca se
  reduce) y el siguiente intento es más chico: un mensaje idéntico jamás se
  reenvía.

Los niveles de reducción (0 = completo … LEVELS-1 = mínimo) solo cambian lo que
se MUESTRA al modelo; la evidencia completa sigue en la memoria persistente
(explore_queries, explore_facts).
"""
from __future__ import annotations

import hashlib
import json
import math
import re

LEVELS = 5
PER_MESSAGE = 8      # marcas de la plantilla de chat por mensaje (<|im_start|>rol … <|im_end|>)
FIXED = 24           # apertura de la respuesta del asistente y marcas varias
SAFETY = 96          # margen fijo además de la reserva para la respuesta
MAX_FACTOR = 4.0

_TOKEN_RE = re.compile(r"\d|[^\W\d_]+|\n|[ \t]+(?=\d)|[ \t]+|.", re.S)


def estimate_tokens(text: str) -> int:
    """Estimación conservadora (por exceso) de tokens para tokenizadores BPE tipo Qwen/Llama."""
    n = 0
    for m in _TOKEN_RE.finditer(text or ""):
        s = m.group()
        c = s[0]
        if c.isdigit() or c == "\n":
            n += 1
        elif c in " \t":
            # un espacio se une a la palabra siguiente, pero ante un dígito es un token propio
            n += 1 + (len(s) - 1) // 4 if s[-1:] and m.end() < len(text) and text[m.end()].isdigit() else (len(s) - 1) // 4
        elif c.isalpha():
            # palabras: tramos de ~4 letras; con acentos o ñ se fragmentan más
            if not s.isascii():
                n += math.ceil(len(s) / 3) + 1
            elif len(s) <= 2:  # tramos cortos entre dígitos o signos: «de» es un token, «xJ» son dos
                n += 1 if s.islower() else len(s)
            elif s.isupper():  # siglas y códigos: ~2 letras por token
                n += math.ceil(len(s) / 2)
            else:
                words = math.ceil(len(s) / 4) + sum(1 for ch in s[1:] if ch.isupper())
                if len(s) >= 5 and sum(ch in "aeiouAEIOU" for ch in s) < 0.3 * len(s):
                    words = max(words, math.ceil(len(s) / 2))  # códigos, hashes, letras aleatorias
                n += words
        else:
            n += 1 if c.isascii() else 2
    return n


def messages_tokens(messages: list[dict], schema=None) -> int:
    n = FIXED + sum(PER_MESSAGE + estimate_tokens(m.get("content", "")) for m in messages)
    if schema is not None:
        n += estimate_tokens(json.dumps(schema, ensure_ascii=False))
    return n


def fingerprint(messages: list[dict], schema=None) -> str:
    return hashlib.sha256(json.dumps([messages, schema], ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class ContextManager:
    """Presupuesto de tokens de un modelo: estimación, reserva para la respuesta y calibración."""

    def __init__(self, num_ctx: int, factor: float = 1.0):
        self.num_ctx = int(num_ctx)
        self.factor = min(MAX_FACTOR, max(1.0, float(factor or 1.0)))

    def count(self, messages: list[dict], schema=None) -> tuple[int, int]:
        """(estimación bruta, estimación calibrada)."""
        raw = messages_tokens(messages, schema)
        return raw, math.ceil(raw * self.factor)

    def input_budget(self, num_predict: int) -> int:
        """Tokens disponibles para el mensaje: contexto − respuesta reservada − margen (5 % + fijo)."""
        return int((self.num_ctx - int(num_predict)) * 0.95) - SAFETY

    def learn(self, raw: int, actual: int | None) -> bool:
        """Ajusta el factor cuando el tamaño real supera la estimación. Devuelve True si cambió."""
        if not raw:
            return False
        if actual is None:  # Ollama no dio cifras: endurecer igual
            new = self.factor * 1.25
        else:
            new = actual / raw * 1.05
        new = min(MAX_FACTOR, new)
        if new > self.factor:
            self.factor = new
            return True
        return False


def clip_cell(v, width: int) -> str:
    s = "NULL" if v is None else str(v)
    s = s.replace("<<<", "‹‹‹").replace(">>>", "›››")
    s = " ".join(s.split())
    return s if len(s) <= width else s[:width] + "…"


# Lo que se muestra de un resultado en cada nivel: (filas, ancho de celda, columnas)
RESULT_VIEW = {0: (10, 40, 40), 1: (6, 28, 30), 2: (4, 18, 20), 3: (2, 12, 12), 4: (1, 10, 8)}


def rows_view(columns: list, rows: list, level: int, max_rows: int | None = None) -> str:
    """Tabla compacta de un resultado; indica explícitamente lo que no se muestra."""
    n_rows, width, n_cols = RESULT_VIEW[min(level, LEVELS - 1)]
    if max_rows is not None:
        n_rows = min(n_rows, max_rows)
    cols = list(columns)[:n_cols]
    lines = [" | ".join(clip_cell(c, width) for c in cols)
             + (f" | (+{len(columns) - n_cols} columnas no mostradas)" if len(columns) > n_cols else "")]
    for r in rows[:n_rows]:
        lines.append(" | ".join(clip_cell(v, width) for v in list(r)[:n_cols]))
    if len(rows) > n_rows:
        lines.append(f"… ({len(rows) - n_rows} filas más registradas en la evidencia, no mostradas aquí)")
    return "\n".join(lines)


# Vista por columnas: (valores distintos mostrados, ancho)
COLUMN_VIEW = {0: (5, 40), 1: (4, 28), 2: (3, 20), 3: (2, 14), 4: (1, 10)}


_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
_DATE_OR_NUM_RE = re.compile(r"[-+]?[\d.:T Z-]{1,30}")  # números y fechas: nunca se recortan


def columns_view(columns: list, rows: list, level: int) -> str:
    """Resumen columna por columna de un resultado ancho.

    Cada línea muestra los valores distintos (en orden de aparición), o «igual en las N filas», o para columnas
    numéricas variadas su mínimo y máximo. Los números nunca se recortan: deben poder contrastarse textualmente
    con la evidencia registrada. Solo el nivel mínimo omite columnas (y lo dice).
    """
    k, width = COLUMN_VIEW[min(level, LEVELS - 1)]
    cap = len(columns) if level < LEVELS - 1 else 16
    lines = [f"(vista por columnas de {len(rows)} filas)"]
    # Primero las columnas más informativas (constantes y de texto); al final las numéricas que varían en cada fila.
    def rank(i):
        d = {"NULL" if r[i] is None else str(r[i]) for r in rows if i < len(r)}
        numeric = all(_NUM_RE.fullmatch(v) for v in d)
        return (0 if len(d) <= 1 else (1 if not numeric else 2), i)
    order = sorted(range(len(columns)), key=rank)
    for i in order[:cap]:
        c = columns[i]
        vals = [r[i] if i < len(r) else None for r in rows]
        distinct = list(dict.fromkeys("NULL" if v is None else str(v) for v in vals))
        name = clip_cell(c, 30)
        if len(distinct) == 1:
            lines.append(f"{name}: {clip_cell(distinct[0], max(width, 24))} (igual en las {len(rows)} filas)")
            continue
        nums = [(float(v), v) for v in distinct if _NUM_RE.fullmatch(v)]
        if len(nums) == len(distinct) and len(distinct) > k:
            examples = {0: 2, 1: 1}.get(level, 0)
            ex = f"; p. ej. {', '.join(distinct[:examples])}" if examples else ""
            span = f"; mínimo {min(nums)[1]}, máximo {max(nums)[1]}" if level < LEVELS - 1 else ""
            lines.append(f"{name}: {len(distinct)} valores numéricos distintos{span}{ex}")
            continue
        shown = ", ".join(v if _DATE_OR_NUM_RE.fullmatch(v) else clip_cell(v, width) for v in distinct[:k])
        more = f" … ({len(distinct)} valores distintos)" if len(distinct) > k else ""
        lines.append(f"{name}: {shown}{more}")
    if len(columns) > cap:
        lines.append(f"(+{len(columns) - cap} columnas no mostradas; están en la evidencia registrada)")
    return "\n".join(lines)


def result_view(columns: list, rows: list, level: int, max_rows: int | None = None) -> str:
    """Filas completas mientras entran; con muchas columnas o en niveles reducidos, vista por columnas."""
    n_cols = RESULT_VIEW[min(level, LEVELS - 1)][2]
    if len(columns) > n_cols or (level >= 2 and len(rows) > 2):
        return columns_view(columns, rows, level)
    return rows_view(columns, rows, level, max_rows=max_rows)
