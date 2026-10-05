#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ma_spectrum_lab_v1.py
=====================

Laboratorio autónomo del ESPECTRO DE MEDIAS MÓVILES para ZEC / GMX.

  * Lee MariaDB local (prrr_market.gmx_price) con PyMySQL.
  * DESCRIBE gmx_price para validar el esquema.
  * UNA sola carga masiva de ZEC, cierra la conexión, y todo lo demás en RAM
    con numpy/pandas.
  * Grilla causal de 1 s sobre source_ts: para cada boundary T se usa la
    última cotización con source_ts <= T; si tiene > 10 s es STALE y rompe la
    continuidad de todas las medias (como el motor live).
  * Espectro SMA 25/50/75/100/150/300/600/1200: valor, pendiente 5 s en bps,
    signo, edad desde el último cambio de signo, conteos y spectrum_string.
  * Estudio de PROPAGACIÓN del giro a través de las escalas.
  * Estrategia BASE SMA35/70 + filtros de espectro (sin optimizar).
  * Distribución de TODAS las operaciones BASE por alineación 0/8..8/8,
    LONG/SHORT, contexto SMA1200, y validación EARLY / LATE.

Uso:
    cd ~/conceptito
    export PRRR_DB_PASSWORD='...'
    ./venv/bin/python ma_spectrum_lab_v1.py

    Opciones útiles:  --since 2026-08-01  --until 2026-10-01
                      --symbol ZEC  --symbol-col market
                      --where "market = 'ZEC/USD [ZEC-USDC]'"
                      --show-at "2026-09-12 14:03:00"
                      --export-grid grid.csv   (exporta TODOS los segundos; grande)
                      --from-csv datos.csv     (source_ts,min_price,max_price; sin DB)

Salidas (en el directorio actual o --out-dir):
    ma_spectrum_lab_v1_results.csv       variantes x período x lado
    ma_spectrum_lab_v1_trades.csv        todas las operaciones + espectro en signal_ts
    ma_spectrum_lab_v1_distribution.csv  operaciones BASE agrupadas por alineación
    ma_spectrum_lab_v1_propagation.csv   estudio descriptivo de propagación y estados

Regla de oro: todo feature usado en la señal N usa sólo información <= N.
Los retornos "forward" del estudio de propagación son RESULTADOS medidos, nunca
features de decisión.
"""

import argparse
import gc
import math
import os
import sys
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

# =============================================================================
# CONFIGURACIÓN
# =============================================================================

DB_HOST = os.environ.get("PRRR_DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("PRRR_DB_PORT", "3306"))
DB_NAME = os.environ.get("PRRR_DB_NAME", "prrr_market")
DB_USER = os.environ.get("PRRR_DB_USER", "prrr")
DB_PASSWORD_ENV = "PRRR_DB_PASSWORD"
TABLE = "gmx_price"
DEFAULT_SYMBOL = "ZEC"

# Columnas candidatas (se detectan con DESCRIBE)
TS_COL_CANDIDATES = ("source_ts",)
MIN_COL_CANDIDATES = ("min_price", "minprice", "price_min")
MAX_COL_CANDIDATES = ("max_price", "maxprice", "price_max")
SYMBOL_COL_CANDIDATES = ("symbol", "token", "token_symbol", "market", "market_symbol",
                         "asset", "ticker", "pair", "coin", "index_token", "name")

# Grilla
STALE_MAX_AGE_S = 10          # cotización con > 10 s de antigüedad => STALE

# Espectro
SPECTRUM = (25, 50, 75, 100, 150, 300, 600, 1200)
SHORT_SET = (25, 50, 75, 100)
LONG_SET = (150, 300, 600, 1200)
SLOPE_LAG_S = 5               # pendiente sobre los últimos 5 s
NEUTRAL_BPS = 0.01            # |slope| <= esto => signo 0 (neutra)

# Estrategia BASE
FAST, SLOW = 35, 70
COOLDOWN_S = 32
# Semántica LIVE de GMX_MOV_V1 (oficial): cooldown CROSS-TO-CROSS.
#   Un cruce está permitido si no hubo cruce anterior o si pasaron >= 32 s desde el cruce anterior.
#   TODO cruce actualiza lastCrossT, aunque quede bloqueado por cooldown, MOVEMENT, posición abierta,
#   entrada pendiente o filtro de variante.
# Los modos "exit"/"entry" existen SÓLO como diagnóstico NO-LIVE (--cooldown-diag).
COOLDOWN_MODE = "live"
RANGE_WIN_S = 1800            # 30 minutos causales
RANGE_MIN_BPS = 66.04
EXPOSURE_USD = 3000.0
TP_USD = 25.0
SL_USD = -75.0
TIMEOUT_S = 100 * 60
FEES_RT_USD = 3.0

# Estudio descriptivo
PROP_WINDOW_S = 300           # cascada 25->50->75->100 debe completarse en <= 300 s
PROP_RECENT_S = 20            # PROP20: giro de SMA25 o SMA50 en los últimos 20 s
FWD_HORIZONS = (60, 300, 900, 1800)
MIN_OPS_STABLE = 20           # menos ops en un período => POCAS_OPS

CH_UP, CH_DOWN, CH_NEUTRAL, CH_NA = "↑", "↓", "·", "?"

OUT_PREFIX = "ma_spectrum_lab_v1"


# =============================================================================
# UTILIDADES
# =============================================================================

def ts_to_iso(sec):
    if sec is None or (isinstance(sec, float) and not np.isfinite(sec)):
        return ""
    return datetime.fromtimestamp(float(sec), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def parse_user_time(s):
    """'YYYY-MM-DD[ HH:MM[:SS]]' (UTC) -> epoch seconds."""
    if s is None:
        return None
    s = s.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
    raise SystemExit(f"Fecha inválida: {s!r} (usar 'YYYY-MM-DD[ HH:MM[:SS]]' UTC)")


def rss_mb():
    try:
        import psutil
        return psutil.Process(os.getpid()).memory_info().rss / 1e6
    except Exception:
        pass
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1e3
    except Exception:
        pass
    return float("nan")


def peak_rss_mb():
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e3  # KB en Linux
    except Exception:
        return float("nan")


def hr(title=""):
    line = "=" * 100
    if title:
        print("\n" + line + "\n" + title + "\n" + line)
    else:
        print(line)


def print_df(df, max_rows=200):
    with pd.option_context("display.max_rows", max_rows, "display.max_columns", 60,
                           "display.width", 250, "display.float_format", "{:,.2f}".format):
        print(df.to_string(index=False))


# =============================================================================
# CARGA DE DATOS
# =============================================================================

def _pick(cols, candidates):
    for c in candidates:
        if c in cols:
            return cols[c]["name"]
    return None


def _is_int_type(t):
    return any(t.startswith(p) for p in ("tinyint", "smallint", "mediumint", "int", "bigint"))


def _is_num_type(t):
    return _is_int_type(t) or any(t.startswith(p) for p in ("decimal", "numeric", "double", "float", "real"))


def _is_dt_type(t):
    return t.startswith("datetime") or t.startswith("timestamp") or t.startswith("date")


def _unit_divisor(sample):
    """Detecta unidad de un epoch numérico por magnitud."""
    v = abs(float(sample))
    if v > 1e17:
        return 1e9, "ns"
    if v > 1e14:
        return 1e6, "us"
    if v > 1e11:
        return 1e3, "ms"
    return 1.0, "s"


def load_from_db(args):
    try:
        import pymysql
        import pymysql.cursors
    except ImportError:
        raise SystemExit("Falta PyMySQL:  ./venv/bin/pip install pymysql")

    if DB_PASSWORD_ENV not in os.environ:
        raise SystemExit(f"Falta la variable de entorno {DB_PASSWORD_ENV}.  export {DB_PASSWORD_ENV}='...'")
    password = os.environ[DB_PASSWORD_ENV]

    print(f"Conectando a MariaDB {DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME} ...")
    conn = pymysql.connect(host=DB_HOST, port=DB_PORT, user=DB_USER, password=password,
                           database=DB_NAME, charset="utf8mb4", autocommit=True,
                           connect_timeout=15)
    info = {}
    try:
        with conn.cursor() as cur:
            cur.execute("SET time_zone = '+00:00'")
            cur.execute(f"DESCRIBE `{TABLE}`")
            desc = cur.fetchall()

        cols = {}
        print(f"\nDESCRIBE {TABLE}:")
        for r in desc:
            name, typ, nul, key, default, extra = (list(r) + [None] * 6)[:6]
            typ = typ.decode() if isinstance(typ, (bytes, bytearray)) else str(typ)
            cols[str(name).lower()] = {"name": str(name), "type": typ.lower(), "key": key,
                                       "extra": (extra or "").lower()}
            print(f"   {str(name):<28} {typ:<28} {nul or '':<4} {key or '':<4} {extra or ''}")

        ts_col = _pick(cols, TS_COL_CANDIDATES)
        min_col = _pick(cols, MIN_COL_CANDIDATES)
        max_col = _pick(cols, MAX_COL_CANDIDATES)
        missing = [n for n, c in (("source_ts", ts_col), ("min_price", min_col), ("max_price", max_col)) if c is None]
        if missing:
            raise SystemExit(f"Esquema inesperado: faltan columnas {missing}. Columnas: {[c['name'] for c in cols.values()]}")

        id_col = None
        for c in cols.values():
            if "auto_increment" in c["extra"]:
                id_col = c["name"]
                break
        if id_col is None and "id" in cols and _is_int_type(cols["id"]["type"]):
            id_col = cols["id"]["name"]

        if args.symbol_col:
            if args.symbol_col.lower() not in cols:
                raise SystemExit(f"--symbol-col {args.symbol_col!r} no existe en {TABLE}")
            sym_col = cols[args.symbol_col.lower()]["name"]
        else:
            sym_col = _pick(cols, SYMBOL_COL_CANDIDATES)

        ts_type = cols[ts_col.lower()]["type"]
        qts = f"`{ts_col}`"

        # --- filtro de símbolo
        where, params = [], []
        if args.where:
            where.append(f"({args.where})")
            print(f"\nFiltro manual --where: {args.where}")
        elif sym_col is not None:
            sym_value = args.symbol
            with conn.cursor() as cur:
                cur.execute(f"SELECT 1 FROM `{TABLE}` WHERE `{sym_col}` = %s LIMIT 1", (sym_value,))
                exact = cur.fetchone() is not None
            if not exact:
                # agregado pequeño para ver qué valores parecidos existen
                with conn.cursor() as cur:
                    cur.execute(f"SELECT `{sym_col}`, COUNT(*) FROM `{TABLE}` "
                                f"WHERE UPPER(`{sym_col}`) LIKE %s GROUP BY `{sym_col}` ORDER BY COUNT(*) DESC",
                                (f"%{sym_value.upper()}%",))
                    cands = cur.fetchall()
                if not cands:
                    raise SystemExit(f"No hay filas con {sym_col} = {sym_value!r} ni parecidas. "
                                     f"Usá --symbol-col / --symbol / --where.")
                print(f"\n[AVISO] No hay {sym_col} = {sym_value!r} exacto. Valores parecidos:")
                for v, n in cands:
                    print(f"           {v!r:<40} {n:>12,} filas")
                sym_value = cands[0][0]
                if len(cands) > 1:
                    print(f"[AVISO] Varios mercados coinciden; NO se mezclan. Se usa el más frecuente: {sym_value!r}")
                    print("        (forzá otro con --symbol '<valor exacto>' o --where)")
            where.append(f"`{sym_col}` = %s")
            params.append(sym_value)
            info["symbol_filter"] = f"{sym_col} = {sym_value!r}"
            print(f"\nFiltro de símbolo: {info['symbol_filter']}")
        else:
            print(f"\n[AVISO] {TABLE} no tiene columna de símbolo reconocible: se asume que TODO es ZEC. "
                  f"(usá --symbol-col o --where si no es así)")

        # --- expresión de timestamp
        divisor, unit = 1.0, "s"
        if _is_dt_type(ts_type):
            ts_expr = f"CAST(ROUND(UNIX_TIMESTAMP({qts}) * 1000) AS SIGNED)"
            divisor, unit = 1e3, "ms(datetime UTC)"
            to_sql_ts = lambda sec: datetime.fromtimestamp(sec, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        else:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {qts} FROM `{TABLE}` WHERE {qts} IS NOT NULL"
                            + (" AND " + " AND ".join(where) if where else "") + " LIMIT 1", params)
                row = cur.fetchone()
            if row is None:
                raise SystemExit("No hay filas para el filtro elegido.")
            sample = row[0]
            if isinstance(sample, (bytes, bytearray)):
                sample = sample.decode()
            try:
                sample_f = float(sample)
                divisor, unit = _unit_divisor(sample_f)
                ts_expr = qts if _is_int_type(ts_type) else f"({qts} + 0E0)"
                to_sql_ts = lambda sec, d=divisor: (int(sec * d) if d > 1 else sec)
            except (TypeError, ValueError):
                # string con fecha
                ts_expr = f"CAST(ROUND(UNIX_TIMESTAMP({qts}) * 1000) AS SIGNED)"
                divisor, unit = 1e3, "ms(string fecha UTC)"
                to_sql_ts = lambda sec: datetime.fromtimestamp(sec, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        print(f"source_ts tipo={ts_type}  unidad={unit}")

        since = parse_user_time(args.since)
        until = parse_user_time(args.until)
        if since is not None:
            where.append(f"{qts} >= %s")
            params.append(to_sql_ts(since))
        if until is not None:
            where.append(f"{qts} < %s")
            params.append(to_sql_ts(until))

        sel = [ts_expr, f"(`{min_col}` + 0E0)", f"(`{max_col}` + 0E0)"]
        if id_col:
            sel.append(f"`{id_col}`")
        sql = f"SELECT {', '.join(sel)} FROM `{TABLE}`"
        if where:
            sql += " WHERE " + " AND ".join(where)
        print(f"\nCarga masiva: {sql}   params={params}")

        chunks = []
        n = 0
        cur = conn.cursor(pymysql.cursors.SSCursor)
        try:
            cur.execute(sql, params)
            while True:
                rows = cur.fetchmany(250_000)
                if not rows:
                    break
                chunks.append(np.array(rows, dtype=np.float64))
                n += len(rows)
                if len(chunks) % 8 == 0:
                    print(f"   ... {n:,} filas")
        finally:
            cur.close()
    finally:
        conn.close()
        print("Conexión MariaDB cerrada.")

    if not chunks:
        raise SystemExit("La carga no devolvió filas.")
    data = np.concatenate(chunks)
    del chunks
    ts = data[:, 0] / divisor
    mn = data[:, 1].copy()
    mx = data[:, 2].copy()
    ids = data[:, 3].copy() if data.shape[1] > 3 else None
    del data
    info.update({"source": "mariadb", "ts_unit": unit})
    return ts, mn, mx, ids, info


def load_from_csv(path, args):
    df = pd.read_csv(path)
    cols = {c.lower(): c for c in df.columns}
    for need in ("source_ts", "min_price", "max_price"):
        if need not in cols:
            raise SystemExit(f"CSV sin columna {need}")
    if args.symbol_col and args.symbol_col.lower() in cols:
        df = df[df[cols[args.symbol_col.lower()]] == args.symbol]
    raw = df[cols["source_ts"]]
    if np.issubdtype(raw.dtype, np.number):
        ts = raw.to_numpy(np.float64)
        d, _ = _unit_divisor(np.nanmedian(ts))
        ts = ts / d
    else:
        ts = pd.to_datetime(raw, utc=True).astype("int64").to_numpy() / 1e9
    mn = pd.to_numeric(df[cols["min_price"]], errors="coerce").to_numpy(np.float64)
    mx = pd.to_numeric(df[cols["max_price"]], errors="coerce").to_numpy(np.float64)
    ids = df[cols["id"]].to_numpy(np.float64) if "id" in cols else None
    since, until = parse_user_time(args.since), parse_user_time(args.until)
    m = np.ones(len(ts), bool)
    if since is not None:
        m &= ts >= since
    if until is not None:
        m &= ts < until
    return ts[m], mn[m], mx[m], (ids[m] if ids is not None else None), {"source": f"csv:{path}"}


def clean_sort(ts, mn, mx, ids):
    ok = np.isfinite(ts) & np.isfinite(mn) & np.isfinite(mx) & (mn > 0) & (mx > 0)
    dropped = int((~ok).sum())
    ts, mn, mx = ts[ok], mn[ok], mx[ok]
    if ids is not None:
        ids = ids[ok]
        order = np.lexsort((ids, ts))      # empates de source_ts: el último insertado gana
    else:
        order = np.argsort(ts, kind="stable")
    ts, mn, mx = ts[order], mn[order], mx[order]
    inverted = int((mn > mx).sum())
    return ts, mn, mx, dropped, inverted


# =============================================================================
# GRILLA CAUSAL DE 1 SEGUNDO
# =============================================================================

class Grid:
    pass


def build_grid(ts, mn, mx):
    G = Grid()
    t0 = int(math.ceil(ts[0]))
    t1 = int(math.floor(ts[-1]))
    if t1 <= t0:
        raise SystemExit("Rango temporal demasiado corto.")
    L = t1 - t0 + 1
    G.t0, G.L = t0, L
    T = np.arange(t0, t1 + 1, dtype=np.float64)
    qi = np.searchsorted(ts, T, side="right") - 1        # última cotización con source_ts <= T
    qage = T - ts[qi]
    del T
    G.stale = qage > STALE_MAX_AGE_S
    G.valid = ~G.stale
    del qage
    G.qmin = mn[qi]
    G.qmax = mx[qi]
    del qi
    G.qmin[G.stale] = np.nan
    G.qmax[G.stale] = np.nan
    G.mid = (G.qmin + G.qmax) * 0.5

    idx = np.arange(L, dtype=np.int64)
    last_stale = np.where(G.stale, idx, -1)
    np.maximum.accumulate(last_stale, out=last_stale)
    G.run_len = (idx - last_stale).astype(np.int32)      # segundos válidos consecutivos hasta t (0 si stale)
    del last_stale
    nv = np.where(G.valid, idx, L)
    G.next_valid = np.minimum.accumulate(nv[::-1])[::-1].copy()   # primer índice válido >= t (L si no hay)
    del nv, idx
    G.n_segments = int((G.valid & np.r_[True, G.stale[:-1]]).sum())
    return G


def make_sma_fn(G):
    """Devuelve sma(N): SMA causal que se reinicia en cada hueco STALE."""
    ref = float(np.nanmedian(G.mid[G.valid][: 1_000_000])) if G.valid.any() else 0.0
    x = np.where(G.valid, G.mid - ref, 0.0)
    csp = np.empty(G.L + 1, dtype=np.float64)
    csp[0] = 0.0
    np.cumsum(x, out=csp[1:])
    del x

    def sma(N):
        out = np.full(G.L, np.nan)
        if N <= G.L:
            out[N - 1:] = (csp[N:] - csp[:G.L + 1 - N]) / N + ref
        out[G.run_len < N] = np.nan
        return out

    return sma, csp


def compute_spectrum(G, neutral_bps):
    K = len(SPECTRUM)
    L = G.L
    G.SMA = np.empty((K, L), dtype=np.float64)
    G.SLOPE = np.empty((K, L), dtype=np.float32)
    G.SIGN = np.zeros((K, L), dtype=np.int8)
    G.SVALID = np.zeros((K, L), dtype=bool)
    G.AGE = np.full((K, L), -1, dtype=np.int32)
    G.GENUINE = np.zeros((K, L), dtype=bool)      # último cambio fue un giro real (no arranque de segmento)

    sma, csp = make_sma_fn(G)
    idx = np.arange(L, dtype=np.int64)
    for k, N in enumerate(SPECTRUM):
        s = sma(N)
        sl = np.full(L, np.nan)
        sl[SLOPE_LAG_S:] = (s[SLOPE_LAG_S:] / s[:-SLOPE_LAG_S] - 1.0) * 1e4   # NaN si SMA(t-5) no válida
        v = np.isfinite(sl)
        sg = np.zeros(L, dtype=np.int8)
        sg[v & (sl > neutral_bps)] = 1
        sg[v & (sl < -neutral_bps)] = -1
        prev_v = np.r_[False, v[:-1]]
        prev_s = np.r_[np.int8(0), sg[:-1]]
        change = v & (~prev_v | (sg != prev_s))
        last = np.where(change, idx, -1)
        np.maximum.accumulate(last, out=last)
        age = np.where(v, idx - last, -1)
        gen_at = change & prev_v
        genuine = np.zeros(L, dtype=bool)
        lv = last >= 0
        genuine[lv] = gen_at[last[lv]]
        G.SMA[k] = s
        G.SLOPE[k] = sl
        G.SIGN[k] = sg
        G.SVALID[k] = v
        G.AGE[k] = age
        G.GENUINE[k] = genuine & v
        del s, sl, v, sg, prev_v, prev_s, change, last, age, gen_at, genuine

    ks = [SPECTRUM.index(n) for n in SHORT_SET]
    kl = [SPECTRUM.index(n) for n in LONG_SET]
    up = (G.SIGN == 1)
    dn = (G.SIGN == -1)
    G.up_count = up.sum(0).astype(np.int8)
    G.down_count = dn.sum(0).astype(np.int8)
    G.short_up = up[ks].sum(0).astype(np.int8)
    G.short_down = dn[ks].sum(0).astype(np.int8)
    G.long_up = up[kl].sum(0).astype(np.int8)
    G.long_down = dn[kl].sum(0).astype(np.int8)
    G.n_valid_sma = G.SVALID.sum(0).astype(np.int8)
    del up, dn

    # Estrategia BASE
    G.fast = sma(FAST)
    G.slow = sma(SLOW)
    del csp
    rmax = pd.Series(G.mid).rolling(RANGE_WIN_S, min_periods=1).max().to_numpy()
    rmin = pd.Series(G.mid).rolling(RANGE_WIN_S, min_periods=1).min().to_numpy()
    G.range_bps = ((rmax - rmin) / G.mid * 1e4).astype(np.float32)   # NaN si mid(t) stale

    # WARMUP de MOVEMENT (semántica live GMX_MOV_V1):
    #   movePut sólo en segundos válidos; r30 = precios en (t-30min, t].
    #   r30Since = primer segundo válido, y se reinicia SÓLO cuando, al podar en un segundo válido,
    #   la ventana r30 queda vacía (el válido anterior está a >= 30 min).  Un STALE corto no resetea.
    #   MOVEMENT puede ser ON/OFF recién cuando t - r30Since >= 30min - 1s; antes es WARMUP.
    G.move_warm = np.zeros(L, dtype=bool)
    vi = np.flatnonzero(G.valid)
    if vi.size:
        reset = np.r_[True, np.diff(vi) >= RANGE_WIN_S]
        since = vi[np.flatnonzero(reset)][np.cumsum(reset) - 1]
        G.move_warm[vi] = (vi - since) >= RANGE_WIN_S - 1
    del vi
    del rmax, rmin


def spectrum_strings(G, ii):
    """spectrum_string para índices ii (vectorizado). ↑ ↓ · ?"""
    ii = np.asarray(ii, dtype=np.int64)
    if ii.size == 0:
        return np.array([], dtype="<U8")
    code = G.SIGN[:, ii].astype(np.int8) + 2          # 1:↓ 2:· 3:↑
    code[~G.SVALID[:, ii]] = 0                         # 0:?
    lut = np.array([CH_NA, CH_DOWN, CH_NEUTRAL, CH_UP], dtype="<U1")
    chars = np.ascontiguousarray(lut[code].T)          # (n, 8)
    return chars.view(f"<U{len(SPECTRUM)}").ravel()


def print_vertical_spectrum(G, i, title=""):
    s = spectrum_strings(G, [i])[0]
    print(f"\n{title}  t={ts_to_iso(G.t0 + i)} UTC   mid={G.mid[i]:.6g}   spectrum={s}")
    for k, N in enumerate(SPECTRUM):
        if G.SVALID[k, i]:
            print(f"   SMA{N:<5} {s[k]}   sma={G.SMA[k, i]:<14.6g} slope={G.SLOPE[k, i]:+8.3f} bps   age={G.AGE[k, i]:>6} s")
        else:
            print(f"   SMA{N:<5} {s[k]}   (sin datos suficientes en el segmento)")
    print(f"   up={G.up_count[i]} down={G.down_count[i]}  short {G.short_up[i]}↑/{G.short_down[i]}↓  "
          f"long {G.long_up[i]}↑/{G.long_down[i]}↓")


# =============================================================================
# SEÑALES Y SIMULACIÓN
# =============================================================================

def base_signals(G):
    """
    Todos los cruces SMA35/70 (sin filtrar) + máscaras evaluadas en el cruce N:
      cd_ok    cooldown LIVE cross-to-cross: primer cruce, o >= COOLDOWN_S desde el cruce ANTERIOR
               (cualquiera, permitido o no: todo cruce actualiza lastCrossT).
      move_ok  MOVEMENT ON: warmup de 30 min completo (G.move_warm) y rango causal 30 min >= RANGE_MIN_BPS.
      move_warm MOVEMENT fuera de WARMUP (para el embudo).
    """
    d = G.fast - G.slow
    prev = np.r_[np.nan, d[:-1]]
    bull = (d > 0) & (prev <= 0)
    bear = (d < 0) & (prev >= 0)
    cross_idx = np.flatnonzero(bull | bear)
    dirs = np.where(bull[cross_idx], 1, -1).astype(np.int8)
    move_warm = G.move_warm[cross_idx]
    move_ok = move_warm & (G.range_bps[cross_idx] >= RANGE_MIN_BPS)
    gap = np.diff(cross_idx, prepend=cross_idx[0] - COOLDOWN_S - 1) if cross_idx.size else cross_idx
    cd_ok = gap >= COOLDOWN_S
    return cross_idx, dirs, cd_ok, move_ok, move_warm


def signal_features(G, idx, dirs):
    """Features del espectro en la señal N (sólo información <= N)."""
    S = G.SIGN[:, idx]                      # (8, n)
    aligned = (S == dirs[None, :]) & G.SVALID[:, idx]
    ks = [SPECTRUM.index(n) for n in SHORT_SET]
    kl = [SPECTRUM.index(n) for n in LONG_SET]
    k25, k50 = SPECTRUM.index(25), SPECTRUM.index(50)
    a25, a50 = G.AGE[k25, idx], G.AGE[k50, idx]
    F = {
        "align": aligned.sum(0),
        "short_align": aligned[ks].sum(0),
        "long_align": aligned[kl].sum(0),
        "s25": aligned[k25],
        "s50": aligned[k50],
        "recent2550": ((a25 >= 0) & (a25 <= PROP_RECENT_S)) | ((a50 >= 0) & (a50 <= PROP_RECENT_S)),
    }
    return F


def variant_masks(F):
    la = F["long_align"]
    return {
        "BASE": np.ones_like(F["align"], dtype=bool),
        "ALIGN4": F["align"] >= 4,
        "ALIGN5": F["align"] >= 5,
        "ALIGN6": F["align"] >= 6,
        "ALIGN7": F["align"] >= 7,
        "MACRO3": la >= 3,
        "CONFIRM50": F["s25"] & F["s50"] & (la >= 3),
        "EARLY_WAVE": F["s25"] & ~F["s50"] & (la >= 3),
        "PROP20": F["s25"] & F["s50"] & (la >= 3) & F["recent2550"],
    }


def simulate(G, sig_idx, sig_dir, cooldown_mode="live"):
    """
    maxPos=1, señal en N, entrada en N+1, TP/SL brutos exactos, timeout.
    cooldown_mode="live": el cooldown cross-to-cross YA se aplicó sobre todos los cruces
    (base_signals); acá sólo se descartan cruces con posición abierta / entrada pendiente
    o con entrada STALE.  "exit"/"entry": diagnóstico NO-LIVE.
    """
    L = G.L
    trades = []
    busy_until = -1
    last_exit = -10 ** 12
    last_entry = -10 ** 12
    skipped_busy = skipped_cd = skipped_stale = 0
    for n, d in zip(sig_idx.tolist(), sig_dir.tolist()):
        if n <= busy_until:
            skipped_busy += 1
            continue
        if cooldown_mode != "live":
            ref = last_exit if cooldown_mode == "exit" else last_entry
            if n - ref < COOLDOWN_S:
                skipped_cd += 1
                continue
        e = n + 1
        if e >= L or not G.valid[e]:
            skipped_stale += 1
            continue
        pe = G.qmax[e] if d == 1 else G.qmin[e]
        exit_arr = G.qmin if d == 1 else G.qmax
        deadline = e + TIMEOUT_S
        hi = min(deadline, L - 1)
        j = -1
        reason = None
        gross = np.nan
        if e + 1 <= hi:
            px = exit_arr[e + 1: hi + 1]
            pnl = EXPOSURE_USD * (px / pe - 1.0) if d == 1 else EXPOSURE_USD * (1.0 - px / pe)
            hit = np.flatnonzero((pnl >= TP_USD) | (pnl <= SL_USD))
            if hit.size:
                h = int(hit[0])
                j = e + 1 + h
                if pnl[h] >= TP_USD:
                    reason, gross = "TP", TP_USD
                else:
                    reason, gross = "SL", SL_USD
        if reason is None:
            if deadline <= L - 1 and G.next_valid[deadline] < L:
                j = int(G.next_valid[deadline])
                px = exit_arr[j]
                gross = EXPOSURE_USD * (px / pe - 1.0) if d == 1 else EXPOSURE_USD * (1.0 - px / pe)
                reason = "TIMEOUT"
            else:
                reason = "OPEN"
                j = L - 1
        if reason in ("TP", "SL"):
            xp = pe * (1.0 + gross / EXPOSURE_USD) if d == 1 else pe * (1.0 - gross / EXPOSURE_USD)
        elif reason == "TIMEOUT":
            xp = float(exit_arr[j])
        else:
            xp = np.nan
            gross = np.nan
        fees = FEES_RT_USD if reason != "OPEN" else np.nan
        trades.append((n, e, j, d, pe, xp, reason, gross, fees))
        busy_until = j
        last_exit = j
        last_entry = e
    cols = ["sig_i", "entry_i", "exit_i", "dir", "entry_price", "exit_price", "exit_reason", "gross", "fees"]
    df = pd.DataFrame(trades, columns=cols)
    df["net"] = df["gross"] - df["fees"]
    stats = {"skipped_busy": skipped_busy, "skipped_entry_stale": skipped_stale}
    if cooldown_mode != "live":
        stats["skipped_cooldown_nolive"] = skipped_cd
    return df, stats


def enrich_trades(G, df, variant, split_ts):
    if df.empty:
        return df
    n = df["sig_i"].to_numpy(np.int64)
    d = df["dir"].to_numpy(np.int8)
    F = signal_features(G, n, d)
    k1200 = SPECTRUM.index(1200)
    s1200 = G.SIGN[k1200, n]
    v1200 = G.SVALID[k1200, n]
    out = pd.DataFrame({
        "variant": variant,
        "side": np.where(d == 1, "LONG", "SHORT"),
        "signal_ts": G.t0 + n,
        "signal_utc": [ts_to_iso(G.t0 + x) for x in n],
        "entry_ts": G.t0 + df["entry_i"].to_numpy(),
        "exit_ts": G.t0 + df["exit_i"].to_numpy(),
        "exit_utc": [ts_to_iso(G.t0 + x) for x in df["exit_i"].to_numpy()],
        "hold_s": df["exit_i"].to_numpy() - df["entry_i"].to_numpy(),
        "period": np.where(G.t0 + n < split_ts, "EARLY", "LATE"),
        "entry_price": df["entry_price"].to_numpy(),
        "exit_price": df["exit_price"].to_numpy(),
        "exit_reason": df["exit_reason"].to_numpy(),
        "gross": df["gross"].to_numpy(),
        "fees": df["fees"].to_numpy(),
        "net": df["net"].to_numpy(),
        "signal_mid": G.mid[n],
        "sma35": G.fast[n],
        "sma70": G.slow[n],
        "range30m_bps": G.range_bps[n],
        "align_count": F["align"],
        "short_align": F["short_align"],
        "long_align": F["long_align"],
        "ctx1200": np.where(~v1200, "SMA1200_NA", np.where(s1200 == 1, "SMA1200_UP",
                            np.where(s1200 == -1, "SMA1200_DOWN", "SMA1200_FLAT"))),
        "spectrum_string": spectrum_strings(G, n),
        "up_count": G.up_count[n],
        "down_count": G.down_count[n],
        "short_up": G.short_up[n],
        "short_down": G.short_down[n],
        "long_up": G.long_up[n],
        "long_down": G.long_down[n],
    })
    for k, N in enumerate(SPECTRUM):
        out[f"sma{N}"] = G.SMA[k, n].astype(np.float64)
    for k, N in enumerate(SPECTRUM):
        out[f"slope{N}"] = np.where(G.SVALID[k, n], G.SLOPE[k, n], np.nan).round(4)
    for k, N in enumerate(SPECTRUM):
        out[f"sign{N}"] = pd.array(np.where(G.SVALID[k, n], G.SIGN[k, n], 0), dtype="Int8")
        out.loc[~G.SVALID[k, n], f"sign{N}"] = pd.NA
    for k, N in enumerate(SPECTRUM):
        out[f"age{N}"] = G.AGE[k, n]
    return out


# =============================================================================
# RESÚMENES
# =============================================================================

def summarize(t):
    c = t[t["exit_reason"] != "OPEN"]
    ops = len(c)
    net = float(c["net"].sum()) if ops else 0.0
    if ops:
        eq = c.sort_values("exit_ts")["net"].cumsum().to_numpy()
        maxdd = float((np.maximum.accumulate(np.r_[0.0, eq]) - np.r_[0.0, eq]).max())
    else:
        maxdd = 0.0
    return {
        "ops": ops,
        "TP": int((c["exit_reason"] == "TP").sum()),
        "SL": int((c["exit_reason"] == "SL").sum()),
        "TIMEOUT": int((c["exit_reason"] == "TIMEOUT").sum()),
        "open": int((t["exit_reason"] == "OPEN").sum()),
        "gross": float(c["gross"].sum()) if ops else 0.0,
        "fees": float(c["fees"].sum()) if ops else 0.0,
        "net": net,
        "net_per_op": net / ops if ops else np.nan,
        "winrate": float((c["net"] > 0).mean()) if ops else np.nan,
        "maxdd": maxdd,
    }


def stability_flag(e, l, key="net"):
    if e["ops"] < MIN_OPS_STABLE or l["ops"] < MIN_OPS_STABLE:
        return "POCAS_OPS"
    if e[key] > 0 and l[key] > 0:
        return "ESTABLE_POSITIVO"
    if e[key] <= 0 and l[key] <= 0:
        return "ESTABLE_NEGATIVO"
    return "INESTABLE"


def build_results(all_trades, variants, skip_stats):
    rows = []
    per = {}
    for v in variants:
        t = all_trades[all_trades["variant"] == v]
        for period in ("ALL", "EARLY", "LATE"):
            tp = t if period == "ALL" else t[t["period"] == period]
            for side in ("ALL", "LONG", "SHORT"):
                ts_ = tp if side == "ALL" else tp[tp["side"] == side]
                s = summarize(ts_)
                per[(v, period, side)] = s
                rows.append({"variant": v, "period": period, "side": side, **s})
    res = pd.DataFrame(rows)

    flags, vsb = {}, {}
    for v in variants:
        flags[v] = stability_flag(per[(v, "EARLY", "ALL")], per[(v, "LATE", "ALL")])
        if v == "BASE":
            vsb[v] = "-"
            continue
        dE = per[(v, "EARLY", "ALL")]["net_per_op"] - per[("BASE", "EARLY", "ALL")]["net_per_op"]
        dL = per[(v, "LATE", "ALL")]["net_per_op"] - per[("BASE", "LATE", "ALL")]["net_per_op"]
        if per[(v, "EARLY", "ALL")]["ops"] < MIN_OPS_STABLE or per[(v, "LATE", "ALL")]["ops"] < MIN_OPS_STABLE:
            vsb[v] = "POCAS_OPS"
        elif dE > 0 and dL > 0:
            vsb[v] = "MEJORA_EN_AMBOS"
        elif dE <= 0 and dL <= 0:
            vsb[v] = "EMPEORA_EN_AMBOS"
        else:
            vsb[v] = "INESTABLE"
    res["stability"] = res["variant"].map(flags)
    res["vs_base_net_per_op"] = res["variant"].map(vsb)
    base_netop = {(p, s): per[("BASE", p, s)]["net_per_op"] for p in ("ALL", "EARLY", "LATE") for s in ("ALL", "LONG", "SHORT")}
    res["base_net_per_op"] = [base_netop[(p, s)] for p, s in zip(res["period"], res["side"])]
    res["delta_net_per_op_vs_base"] = res["net_per_op"] - res["base_net_per_op"]
    for k in skip_stats[variants[0]]:
        res[k] = res["variant"].map(lambda v: skip_stats[v][k])
    return res, per, flags, vsb


def build_distribution(base):
    rows = []
    sides = ("ALL", "LONG", "SHORT")
    ctxs = ("ALL", "SMA1200_UP", "SMA1200_DOWN", "SMA1200_FLAT", "SMA1200_NA")
    periods = ("ALL", "EARLY", "LATE")
    for side in sides:
        b1 = base if side == "ALL" else base[base["side"] == side]
        for ctx in ctxs:
            b2 = b1 if ctx == "ALL" else b1[b1["ctx1200"] == ctx]
            for period in periods:
                b3 = b2 if period == "ALL" else b2[b2["period"] == period]
                for a in list(range(len(SPECTRUM) + 1)) + ["TOTAL"]:
                    b4 = b3 if a == "TOTAL" else b3[b3["align_count"] == a]
                    s = summarize(b4)
                    rows.append({"table": "align8", "side": side, "ctx1200": ctx, "period": period,
                                 "group": f"{a}/8" if a != "TOTAL" else "TOTAL", **s})
    for period in periods:
        b3 = base if period == "ALL" else base[base["period"] == period]
        for side in sides:
            b4 = b3 if side == "ALL" else b3[b3["side"] == side]
            for sa in range(5):
                for la in range(5):
                    s = summarize(b4[(b4["short_align"] == sa) & (b4["long_align"] == la)])
                    rows.append({"table": "short_x_long", "side": side, "ctx1200": "ALL", "period": period,
                                 "group": f"S{sa}/4_L{la}/4", **s})
    dist = pd.DataFrame(rows)
    return dist


# =============================================================================
# ESTUDIO DE PROPAGACIÓN (descriptivo)
# =============================================================================

def fwd_returns(G, t, d, h):
    tt = t + h
    r = np.full(t.size, np.nan)
    ok = tt < G.L
    r[ok] = d[ok] * (G.mid[tt[ok]] / G.mid[t[ok]] - 1.0) * 1e4   # NaN si stale en t+h
    return r


def propagation_study(G, split_ts):
    """
    Evento = SMA100 gira (giro real, no arranque de segmento) hacia d en t.
    Categorías en t (información <= t):
      ORDERED            SMA25, 50, 75 ya están en d y giraron en orden 25 <= 50 <= 75 <= 100,
                         con la cascada completa dentro de PROP_WINDOW_S.
      ALIGNED_UNORDERED  25/50/75 en d pero sin ese orden / fuera de ventana.
      NOT_ALIGNED        alguna de 25/50/75 no está en d.
    long_align = cuántas de 150/300/600/1200 ya acompañan d en t.
    Resultado medido: retorno forward direccional (bps) en t+h — sólo descriptivo.
    """
    k25, k50, k75, k100 = (SPECTRUM.index(n) for n in (25, 50, 75, 100))
    kl = [SPECTRUM.index(n) for n in LONG_SET]
    v = G.SVALID[k100]
    s = G.SIGN[k100]
    ev = v & np.r_[False, v[:-1]] & (s != np.r_[np.int8(0), s[:-1]]) & (s != 0)
    t = np.flatnonzero(ev)
    d = s[t].astype(np.int8)
    al = np.ones(t.size, bool)
    for k in (k25, k50, k75):
        al &= G.SVALID[k, t] & (G.SIGN[k, t] == d) & G.GENUINE[k, t]
    f25 = t - G.AGE[k25, t]
    f50 = t - G.AGE[k50, t]
    f75 = t - G.AGE[k75, t]
    ordered = al & (f25 <= f50) & (f50 <= f75) & (t - f25 <= PROP_WINDOW_S)
    cat = np.where(ordered, "ORDERED", np.where(al, "ALIGNED_UNORDERED", "NOT_ALIGNED"))
    la = np.zeros(t.size, dtype=np.int8)
    for k in kl:
        la += (G.SVALID[k, t] & (G.SIGN[k, t] == d)).astype(np.int8)
    ev_df = pd.DataFrame({
        "t": t, "dir": d, "cat": cat, "long_align": la,
        "period": np.where(G.t0 + t < split_ts, "EARLY", "LATE"),
        "lag_25_50": np.where(ordered, f50 - f25, np.nan),
        "lag_50_75": np.where(ordered, f75 - f50, np.nan),
        "lag_75_100": np.where(ordered, t - f75, np.nan),
    })
    for h in FWD_HORIZONS:
        ev_df[f"fwd{h}"] = fwd_returns(G, t, d, h)

    rows = []

    def agg(sub, **dims):
        r = {"table": "prop_sma100_flip", **dims, "n": len(sub)}
        for h in FWD_HORIZONS:
            x = sub[f"fwd{h}"].dropna()
            r[f"n_fwd{h}"] = len(x)
            r[f"mean_fwd{h}_bps"] = x.mean() if len(x) else np.nan
            r[f"median_fwd{h}_bps"] = x.median() if len(x) else np.nan
            r[f"hit_fwd{h}"] = (x > 0).mean() if len(x) else np.nan
        if dims.get("cat") == "ORDERED":
            r["median_lag_25_50_s"] = sub["lag_25_50"].median()
            r["median_lag_50_75_s"] = sub["lag_50_75"].median()
            r["median_lag_75_100_s"] = sub["lag_75_100"].median()
        rows.append(r)

    for period in ("ALL", "EARLY", "LATE"):
        p = ev_df if period == "ALL" else ev_df[ev_df["period"] == period]
        for side in ("ALL", "LONG", "SHORT"):
            q = p if side == "ALL" else p[p["dir"] == (1 if side == "LONG" else -1)]
            for c in ("ALL_FLIPS", "ORDERED", "ALIGNED_UNORDERED", "NOT_ALIGNED"):
                r_ = q if c == "ALL_FLIPS" else q[q["cat"] == c]
                for lg in ["ALL"] + list(range(5)):
                    z = r_ if lg == "ALL" else r_[r_["long_align"] == lg]
                    agg(z, period=period, side=side, cat=c, long_align=lg)
    prop = pd.DataFrame(rows)
    return prop, ev_df


def state_frequency(G, split_ts, top=25):
    """Frecuencia de cada estado del espectro (3^8) y retorno forward bruto (no direccional)."""
    allv = G.SVALID.all(0)
    code = np.zeros(G.L, dtype=np.int16)
    for k in range(len(SPECTRUM)):
        code += (G.SIGN[k].astype(np.int16) + 1) * (3 ** k)
    t = np.flatnonzero(allv)
    if t.size == 0:
        return pd.DataFrame()
    c = code[t]
    nstates = 3 ** len(SPECTRUM)
    cnt = np.bincount(c, minlength=nstates)
    early = (G.t0 + t) < split_ts
    res = {"state_code": np.arange(nstates), "seconds": cnt, "share_pct": cnt / cnt.sum() * 100}
    one = np.ones(t.size, dtype=np.int8)
    for h in (60, 300):
        r = fwd_returns(G, t, one, h)
        ok = np.isfinite(r)
        for name, m in (("", ok), ("_early", ok & early), ("_late", ok & ~early)):
            n_ = np.bincount(c[m], minlength=nstates)
            s_ = np.bincount(c[m], weights=r[m], minlength=nstates)
            with np.errstate(invalid="ignore", divide="ignore"):
                res[f"mean_fwd{h}_bps{name}"] = s_ / n_
            if name == "":
                res[f"n_fwd{h}"] = n_
    df = pd.DataFrame(res)
    df = df[df["seconds"] > 0].sort_values("seconds", ascending=False).head(top).copy()
    chars = {0: CH_DOWN, 1: CH_NEUTRAL, 2: CH_UP}
    df.insert(1, "spectrum_string", ["".join(chars[(int(x) // (3 ** k)) % 3] for k in range(len(SPECTRUM)))
                                     for x in df["state_code"]])
    df.insert(0, "table", "state_freq")
    return df


# =============================================================================
# MAIN
# =============================================================================

def main():
    ap = argparse.ArgumentParser(description="Laboratorio del espectro de medias móviles ZEC/GMX (v1)")
    ap.add_argument("--symbol", default=DEFAULT_SYMBOL)
    ap.add_argument("--symbol-col", default=None, help="columna de símbolo (auto-detecta si se omite)")
    ap.add_argument("--where", default=None, help="condición SQL manual (reemplaza el filtro de símbolo)")
    ap.add_argument("--since", default=None, help="UTC 'YYYY-MM-DD[ HH:MM:SS]'")
    ap.add_argument("--until", default=None, help="UTC 'YYYY-MM-DD[ HH:MM:SS]' (exclusivo)")
    ap.add_argument("--from-csv", default=None, help="cargar desde CSV (source_ts,min_price,max_price) en vez de MariaDB")
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--neutral-bps", type=float, default=NEUTRAL_BPS)
    ap.add_argument("--cooldown-diag", choices=("exit", "entry"), default=None,
                    help="SÓLO DIAGNÓSTICO NO-LIVE: cooldown desde la última salida/entrada en vez del "
                         "cross-to-cross live. Los CSV llevan sufijo _NOLIVE_<modo>.")
    ap.add_argument("--split", default=None, help="corte EARLY/LATE en UTC (default: punto medio temporal)")
    ap.add_argument("--show-at", default=None, help="imprime el espectro vertical en ese instante UTC")
    ap.add_argument("--export-grid", default=None, help="CSV con TODOS los segundos válidos (puede ser enorme)")
    args = ap.parse_args()

    cd_mode = args.cooldown_diag or COOLDOWN_MODE
    nolive = cd_mode != "live"
    prefix = OUT_PREFIX + (f"_NOLIVE_{cd_mode}" if nolive else "")

    T_start = time.time()
    hr("MA SPECTRUM LAB v1  —  ZEC / GMX")
    print(f"Espectro: {SPECTRUM}   slope {SLOPE_LAG_S}s   neutral |slope|<={args.neutral_bps} bps   stale>{STALE_MAX_AGE_S}s")
    if nolive:
        print("!" * 100)
        print(f"!!  MODO DIAGNÓSTICO NO-LIVE: cooldown desde la última {'salida' if cd_mode == 'exit' else 'entrada'}.")
        print("!!  Estos resultados NO reproducen GMX_MOV_V1. El resultado oficial es sin --cooldown-diag.")
        print("!" * 100)
    cd_txt = "cross-to-cross LIVE" if not nolive else f"NO-LIVE desde {cd_mode}"
    print(f"BASE: SMA{FAST}/{SLOW}  cooldown {COOLDOWN_S}s ({cd_txt})  range30m>={RANGE_MIN_BPS}bps  "
          f"exp ${EXPOSURE_USD:.0f}  TP +${TP_USD:.0f}  SL ${SL_USD:.0f}  timeout {TIMEOUT_S // 60}m  fees ${FEES_RT_USD:.0f}")

    # ---------------- carga
    t_load0 = time.time()
    if args.from_csv:
        ts, mn, mx, ids, info = load_from_csv(args.from_csv, args)
    else:
        ts, mn, mx, ids, info = load_from_db(args)
    n_raw = len(ts)
    ts, mn, mx, dropped, inverted = clean_sort(ts, mn, mx, ids)
    del ids
    t_load = time.time() - t_load0
    if len(ts) < 2:
        raise SystemExit("Muy pocas filas válidas.")

    hr("DATOS")
    print(f"Fuente:                 {info.get('source')}  {info.get('symbol_filter', '')}")
    print(f"Filas GMX cargadas:     {n_raw:,}   (descartadas por NULL/<=0: {dropped:,};  min>max: {inverted:,})")
    print(f"Rango temporal:         {ts_to_iso(ts[0])}  ->  {ts_to_iso(ts[-1])} UTC   "
          f"({(ts[-1] - ts[0]) / 86400:.2f} días)")
    print(f"Tiempo de carga:        {t_load:.1f} s")
    print(f"RAM tras carga:         {rss_mb():,.0f} MB")

    # ---------------- cálculo
    t_calc0 = time.time()
    G = build_grid(ts, mn, mx)
    del ts, mn, mx
    gc.collect()
    n_valid = int(G.valid.sum())
    print(f"Segundos de grilla:     {G.L:,}   válidos {n_valid:,} ({n_valid / G.L * 100:.1f}%)   "
          f"STALE {G.L - n_valid:,}   segmentos continuos {G.n_segments:,}")
    est = G.L * (8 * (8 + 4 + 1 + 1 + 4 + 1) + 6 + 8 * 6 + 4 + 8) / 1e6
    print(f"RAM estimada features:  ~{est:,.0f} MB")
    if G.L > 40_000_000:
        print("[AVISO] Grilla muy grande; considerá --since/--until para limitar la RAM.")

    compute_spectrum(G, args.neutral_bps)
    gc.collect()
    t_calc = time.time() - t_calc0

    valid_idx = np.flatnonzero(G.valid)
    first_t, last_t = G.t0 + valid_idx[0], G.t0 + valid_idx[-1]
    split_ts = parse_user_time(args.split) if args.split else (first_t + last_t) / 2.0
    n_early = int(((G.t0 + valid_idx) < split_ts).sum())
    print(f"Corte EARLY/LATE:       {ts_to_iso(split_ts)} UTC   (segundos válidos EARLY {n_early:,} / LATE {n_valid - n_early:,})")
    del valid_idx

    # ---------------- señales y simulaciones
    t_sim0 = time.time()
    sig_idx, sig_dir, cd_ok, move_ok, move_warm = base_signals(G)
    n_cross = int(sig_idx.size)
    F = signal_features(G, sig_idx, sig_dir)
    masks = variant_masks(F)
    variants = list(masks.keys())
    all_tr, skip_stats = [], {}
    for v in variants:
        fm = masks[v]
        if nolive:
            m = move_ok & fm
            st_pre = {"crosses": n_cross, "blocked_movement_warmup": int((~move_warm).sum()),
                      "blocked_movement_off": int((move_warm & ~move_ok).sum()),
                      "blocked_filter": int((move_ok & ~fm).sum())}
        else:
            # orden de atribución: cooldown live -> movement -> filtro de variante -> posición -> entrada stale
            m = cd_ok & move_ok & fm
            st_pre = {"crosses": n_cross, "blocked_cooldown_live": int((~cd_ok).sum()),
                      "blocked_movement_warmup": int((cd_ok & ~move_warm).sum()),
                      "blocked_movement_off": int((cd_ok & move_warm & ~move_ok).sum()),
                      "blocked_filter": int((cd_ok & move_ok & ~fm).sum())}
        tr, st = simulate(G, sig_idx[m], sig_dir[m], cd_mode)
        skip_stats[v] = {**st_pre, "signals_in": int(m.sum()), **st}
        all_tr.append(enrich_trades(G, tr, v, split_ts))
    trades = pd.concat([t for t in all_tr if not t.empty], ignore_index=True) if any(not t.empty for t in all_tr) \
        else pd.DataFrame(columns=["variant", "period", "side", "exit_reason", "gross", "fees", "net", "exit_ts",
                                   "align_count", "short_align", "long_align", "ctx1200"])
    t_sim = time.time() - t_sim0

    hr("TIEMPOS Y MEMORIA")
    print(f"Tiempo de carga:        {t_load:.1f} s")
    print(f"Tiempo de cálculo:      {t_calc:.1f} s (grilla + espectro)   simulación {t_sim:.1f} s")
    print(f"RAM actual (RSS):       {rss_mb():,.0f} MB    pico: {peak_rss_mb():,.0f} MB")
    print(f"Cruces SMA{FAST}/{SLOW}: {n_cross:,}   permitidos por cooldown cross-to-cross: {int(cd_ok.sum()):,}   "
          f"cooldown+movement: {int((cd_ok & move_ok).sum()):,}   (sólo movement: {int(move_ok.sum()):,})")

    # ---------------- ejemplo de espectro
    last_full = np.flatnonzero(G.SVALID.all(0))
    if last_full.size:
        print_vertical_spectrum(G, int(last_full[-1]), "Espectro en el último segundo con las 8 SMA válidas:")
    if args.show_at:
        i = int(parse_user_time(args.show_at) - G.t0)
        if 0 <= i < G.L:
            print_vertical_spectrum(G, i, "Espectro en --show-at:")
        else:
            print(f"--show-at fuera de rango")
    del last_full

    out = lambda name: os.path.join(args.out_dir, f"{prefix}_{name}.csv")

    # ---------------- resultados por variante
    results, per, flags, vsb = build_results(trades, variants, skip_stats)
    results.to_csv(out("results"), index=False)
    trades.to_csv(out("trades"), index=False)

    hr("RESULTADOS POR VARIANTE (sin optimizar; EARLY/LATE cronológico)")
    rows = []
    for v in variants:
        a, e, l = per[(v, "ALL", "ALL")], per[(v, "EARLY", "ALL")], per[(v, "LATE", "ALL")]
        rows.append({"variant": v, "ops": a["ops"], "TP": a["TP"], "SL": a["SL"], "TO": a["TIMEOUT"],
                     "gross": a["gross"], "fees": a["fees"], "net": a["net"], "net/op": a["net_per_op"],
                     "win%": a["winrate"] * 100 if a["ops"] else np.nan, "maxDD": a["maxdd"],
                     "E_ops": e["ops"], "E_net/op": e["net_per_op"], "L_ops": l["ops"], "L_net/op": l["net_per_op"],
                     "estabilidad": flags[v], "vs_BASE": vsb[v]})
    print_df(pd.DataFrame(rows))
    print("\nLONG / SHORT:")
    rows = []
    for v in variants:
        r = {"variant": v}
        for side in ("LONG", "SHORT"):
            s = per[(v, "ALL", side)]
            r[f"{side}_ops"] = s["ops"]
            r[f"{side}_net"] = s["net"]
            r[f"{side}_net/op"] = s["net_per_op"]
            r[f"{side}_E_net/op"] = per[(v, "EARLY", side)]["net_per_op"]
            r[f"{side}_L_net/op"] = per[(v, "LATE", side)]["net_per_op"]
        rows.append(r)
    print_df(pd.DataFrame(rows))
    if len(trades):
        keys = {v: tuple(trades.loc[trades["variant"] == v, "signal_ts"]) for v in variants}
        for i, a in enumerate(variants):
            for b in variants[i + 1:]:
                if keys[a] and keys[a] == keys[b]:
                    print(f"[NOTA] {b} produce exactamente las mismas operaciones que {a} (filtro redundante con este disparador).")
    print("\nEmbudo de cruces (cooldown live -> movement -> filtro -> en posición/pendiente -> entrada stale):")
    print_df(pd.DataFrame([{"variant": v, **skip_stats[v]} for v in variants]))

    # ---------------- distribución BASE
    base = trades[trades["variant"] == "BASE"] if len(trades) else trades
    dist = build_distribution(base)
    dist.to_csv(out("distribution"), index=False)

    def dist_view(side="ALL", ctx="ALL"):
        q = dist[(dist["table"] == "align8") & (dist["side"] == side) & (dist["ctx1200"] == ctx)]
        a = q[q["period"] == "ALL"].set_index("group")
        e = q[q["period"] == "EARLY"].set_index("group")
        l = q[q["period"] == "LATE"].set_index("group")
        v = a[["ops", "TP", "SL", "TIMEOUT", "gross", "fees", "net", "net_per_op"]].copy()
        v["win%"] = a["winrate"] * 100
        v["E_ops"] = e["ops"]
        v["E_net/op"] = e["net_per_op"]
        v["L_ops"] = l["ops"]
        v["L_net/op"] = l["net_per_op"]
        v["estab"] = [stability_flag(e.loc[g], l.loc[g]) for g in v.index]
        return v.reset_index()

    hr("DISTRIBUCIÓN BASE: cuántas de las 8 SMA acompañaban la señal (todas las ops BASE)")
    print_df(dist_view())
    for side in ("LONG", "SHORT"):
        print(f"\n--- {side}")
        print_df(dist_view(side=side))
    for ctx in ("SMA1200_UP", "SMA1200_DOWN"):
        print(f"\n--- Contexto {ctx} (LONG+SHORT)")
        print_df(dist_view(ctx=ctx))
        for side in ("LONG", "SHORT"):
            v = dist_view(side=side, ctx=ctx)
            v = v[v["ops"] > 0]
            print(f"    {side} con {ctx}:")
            if v.empty:
                print("       (sin operaciones)")
                continue
            print_df(v[["group", "ops", "TP", "SL", "TIMEOUT", "net", "net_per_op", "E_net/op", "L_net/op", "estab"]])

    hr("BASE: short_align (25/50/75/100) x long_align (150/300/600/1200) — net/op [ops]")
    q = dist[(dist["table"] == "short_x_long") & (dist["side"] == "ALL") & (dist["period"] == "ALL")].copy()
    q["S"] = q["group"].str.slice(1, 2)
    q["L"] = q["group"].str.slice(-3, -2)
    cell = q.apply(lambda r: f"{r['net_per_op']:+.2f} [{r['ops']}]" if r["ops"] else "-", axis=1)
    piv = pd.DataFrame({"S": q["S"], "L": q["L"], "cell": cell}).pivot(index="S", columns="L", values="cell")
    piv.index = [f"short {i}/4" for i in piv.index]
    piv.columns = [f"long {c}/4" for c in piv.columns]
    print(piv.to_string())

    # ---------------- propagación
    prop, ev_df = propagation_study(G, split_ts)
    states = state_frequency(G, split_ts)
    prop_all = pd.concat([prop, states], ignore_index=True, sort=False)
    prop_all.to_csv(out("propagation"), index=False)

    hr(f"PROPAGACIÓN: giros de SMA100 (eventos {len(ev_df):,}).  Retorno forward DIRECCIONAL en bps (sólo descriptivo)")
    print("ORDERED = 25 -> 50 -> 75 -> 100 giraron en orden en <= "
          f"{PROP_WINDOW_S}s.   long_align = largas (150..1200) que ya acompañaban.")
    cols = ["cat", "long_align", "n"] + [f"mean_fwd{h}_bps" for h in FWD_HORIZONS] + [f"hit_fwd{h}" for h in (300, 900)]
    q = prop[(prop["period"] == "ALL") & (prop["side"] == "ALL")][cols]
    print_df(q[q["n"] > 0])
    print("\nEstabilidad temporal (mean fwd300 / fwd900, side ALL):")
    rows = []
    for c in ("ALL_FLIPS", "ORDERED", "ALIGNED_UNORDERED", "NOT_ALIGNED"):
        for lg in ["ALL", 3, 4]:
            r = {"cat": c, "long_align": lg}
            for period in ("EARLY", "LATE"):
                z = prop[(prop["period"] == period) & (prop["side"] == "ALL") & (prop["cat"] == c) & (prop["long_align"] == lg)]
                r[f"{period}_n"] = int(z["n"].iloc[0])
                r[f"{period}_fwd300"] = z["mean_fwd300_bps"].iloc[0]
                r[f"{period}_fwd900"] = z["mean_fwd900_bps"].iloc[0]
            e3, l3 = r["EARLY_fwd300"], r["LATE_fwd300"]
            r["estab300"] = ("POCOS" if min(r["EARLY_n"], r["LATE_n"]) < MIN_OPS_STABLE else
                             "ESTABLE+" if e3 > 0 and l3 > 0 else "ESTABLE-" if e3 <= 0 and l3 <= 0 else "INESTABLE")
            rows.append(r)
    print_df(pd.DataFrame(rows))
    o = prop[(prop["period"] == "ALL") & (prop["side"] == "ALL") & (prop["cat"] == "ORDERED") & (prop["long_align"] == "ALL")]
    if len(o) and o["n"].iloc[0] > 0:
        print(f"\nLatencia mediana de la cascada ORDERED: 25->50 {o['median_lag_25_50_s'].iloc[0]:.0f}s   "
              f"50->75 {o['median_lag_50_75_s'].iloc[0]:.0f}s   75->100 {o['median_lag_75_100_s'].iloc[0]:.0f}s")

    if len(states):
        hr("ESTADOS DEL ESPECTRO MÁS FRECUENTES (orden 25..1200).  fwd = retorno bruto del mid en bps (no direccional)")
        print_df(states[["spectrum_string", "seconds", "share_pct", "mean_fwd60_bps", "mean_fwd300_bps",
                         "mean_fwd300_bps_early", "mean_fwd300_bps_late"]], max_rows=40)

    # ---------------- export opcional de la grilla
    if args.export_grid:
        print(f"\nExportando grilla a {args.export_grid} ...")
        vi = np.flatnonzero(G.valid)
        first = True
        for c0 in range(0, vi.size, 500_000):
            ii = vi[c0:c0 + 500_000]
            g = pd.DataFrame({"ts": G.t0 + ii, "utc": [ts_to_iso(G.t0 + x) for x in ii],
                              "min_price": G.qmin[ii], "max_price": G.qmax[ii], "mid": G.mid[ii],
                              "range30m_bps": G.range_bps[ii], "spectrum_string": spectrum_strings(G, ii),
                              "up_count": G.up_count[ii], "down_count": G.down_count[ii],
                              "short_up": G.short_up[ii], "short_down": G.short_down[ii],
                              "long_up": G.long_up[ii], "long_down": G.long_down[ii]})
            for k, N in enumerate(SPECTRUM):
                g[f"sma{N}"] = G.SMA[k, ii]
                g[f"slope{N}"] = np.where(G.SVALID[k, ii], G.SLOPE[k, ii], np.nan)
                g[f"age{N}"] = G.AGE[k, ii]
            g.to_csv(args.export_grid, mode="w" if first else "a", header=first, index=False)
            first = False

    hr("ARCHIVOS")
    for name in ("results", "trades", "distribution", "propagation"):
        print(f"   {out(name)}")
    if args.export_grid:
        print(f"   {args.export_grid}")
    print(f"\nTiempo total: {time.time() - T_start:.1f} s    RAM pico: {peak_rss_mb():,.0f} MB")
    print("Recordatorio: nada de esto es un ganador elegido; mirá la columna de estabilidad EARLY/LATE.")


if __name__ == "__main__":
    main()
