#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CONCEPTITO LAB — laboratorio externo y experimental (NO toca Node-RED).

Idea:  cruce SMA = evento candidato.  Cada cruce que pasa el cooldown se trata
como un experimento INDEPENDIENTE (sin maxPos): se abre hipotéticamente en N+1
y se sigue su camino futuro sin bloquear a ningún otro cruce.  Para cada cruce
se guardan features causales (<= T) y resultados futuros (MFE/MAE, barreras,
simulaciones TP/SL/timeout).  Después se analiza qué features separan cruces
buenos de malos, con controles de estabilidad temporal.

Semántica temporal (idéntica al motor live):
  * reloj autoritativo = gmx_price.source_ts
  * para cada boundary T (múltiplo de 1000 ms) se usa la ÚLTIMA quote con
    source_ts <= T; si T - source_ts > 10 s el segundo es STALE
  * un segundo stale rompe la continuidad: la SMA vuelve a calentar
  * señal en N, entrada exactamente en N+1 (si N+1 no es válido, se omite)
  * LONG entra a max_price y sale a min_price; SHORT al revés
  * señal sobre el mid = (min_price + max_price) / 2

Uso típico:
  python3 conceptito_lab.py
  python3 conceptito_lab.py --split-ts "2026-10-05 00:00:00"
  python3 conceptito_lab.py --gmx-csv gmx.csv --mkt-csv mkt.csv   # sin DB
"""

import argparse
import math
import os
import sys
import time

import numpy as np
import pandas as pd


# =============================================================================
# CONFIGURACIÓN
# =============================================================================

DB = dict(host='127.0.0.1', port=3306, database='prrr_market',
          user='prrr', password='prrr_local_2026')

CFG = dict(
    symbol='ZEC',
    fast=35, slow=70, cooldown_s=32,
    max_age_ms=10_000,
    exposure=3000.0,
    fee_rt=3.0,                       # USD ida y vuelta ($1.50 + $1.50)
    horizons_min=[10, 30, 60, 100],
    tp_levels=[7, 15, 25, 40],
    sl_levels=[15, 25, 50, 75],       # se interpretan como -X USD
    # simulaciones independientes: (tp, sl, timeout_min). La primera es la principal.
    sims=[(25, -75, 100), (7, -75, 26)],
    relvol_ref_s=900,                 # 15 min para la mediana causal de volumen
    mkt_lag_s=1,                      # ver nota "market_1s ts" en README
    min_band_n=30,                    # menos señales que esto = evidencia insuficiente
    n_bands=5,
    boot_iter=500,
)

FEATURE_GROUPS = {
    'A_sma': ['sep_dir_bps',
              'slope_fast_5s_dir_bps', 'slope_fast_10s_dir_bps', 'slope_fast_20s_dir_bps',
              'slope_slow_5s_dir_bps', 'slope_slow_10s_dir_bps', 'slope_slow_20s_dir_bps',
              'sep_chg_5s_dir_bps', 'sep_chg_10s_dir_bps'],
    'B_comp': ['comp_30', 'comp_60', 'comp_120', 'eff_30', 'eff_60', 'eff_120'],
    'C_vol': ['range_pct_30', 'range_pct_60', 'range_pct_120',
              'rv_bps_30', 'rv_bps_60', 'rv_bps_120',
              'path_pct_30', 'path_pct_60', 'path_pct_120'],
    'D_flow': ['flow_dir_10', 'flow_dir_30', 'flow_dir_60',
               'balance_10', 'balance_30', 'balance_60',
               'vol_usd_10', 'vol_usd_30', 'vol_usd_60'],
    'E_relvol': ['relvol_10', 'relvol_30', 'relvol_60'],
    'F_mom': ['ret_5s_dir_bps', 'ret_10s_dir_bps', 'ret_20s_dir_bps', 'ret_30s_dir_bps'],
    'G_ctx': ['crosses_10m', 'secs_since_prev_cross', 'spread_bps', 'gmx_age_ms'],
}


# =============================================================================
# SALIDA (stdout + reporte)
# =============================================================================

REPORT = []


def say(s=''):
    print(s)
    REPORT.append(s)


def f(v, nd=2, w=None):
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        s = '-'
    else:
        s = ('{:.' + str(nd) + 'f}').format(v)
    return s.rjust(w) if w else s


def iso(ms):
    return pd.Timestamp(int(ms), unit='ms').strftime('%Y-%m-%d %H:%M:%S')


# =============================================================================
# TIEMPO: conversión explícita a epoch-ms (evita el bug 2026 -> 1970)
# =============================================================================

def to_ms(col, name):
    """Convierte una columna de tiempos a epoch milisegundos (float, NaN si falta).

    Acepta datetime64 de cualquier unidad (ns/us/ms/s), datetime de Python,
    strings, o números epoch (s/ms/us/ns, detectados por magnitud).
    Nunca hace .astype('int64') sobre un datetime64 sin saber su unidad.
    Los DATETIME sin zona se asumen UTC (igual que dt() del motor live).
    """
    s = pd.Series(col).reset_index(drop=True)
    if pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_datetime64_any_dtype(s):
        v = s.astype('float64').to_numpy()
        med = np.nanmedian(v)
        if med > 1e17:
            v = v / 1e6          # ns
        elif med > 1e14:
            v = v / 1e3          # us
        elif med > 1e11:
            pass                 # ms
        else:
            v = v * 1e3          # s
        out = np.round(v)
    else:
        dt = pd.to_datetime(s, errors='coerce')
        if getattr(dt.dt, 'tz', None) is not None:
            dt = dt.dt.tz_convert('UTC').dt.tz_localize(None)
        # resta + división por Timedelta: independiente de la unidad interna
        out = ((dt - pd.Timestamp('1970-01-01')) / pd.Timedelta(milliseconds=1)).to_numpy(dtype='float64')
        out = np.round(out)
    ok = out[np.isfinite(out)]
    if len(ok):
        y0 = pd.Timestamp(int(ok.min()), unit='ms').year
        y1 = pd.Timestamp(int(ok.max()), unit='ms').year
        if y0 < 2020 or y1 > 2035:
            raise SystemExit('ERROR: %s convertido a años %d..%d — conversión de tiempo '
                             'sospechosa (¿bug 1970?). Revisar tipo de columna.' % (name, y0, y1))
    return out


# =============================================================================
# CARGA
# =============================================================================

def db_query(sql, params):
    import pymysql
    conn = pymysql.connect(host=DB['host'], port=DB['port'], user=DB['user'],
                           password=DB['password'], database=DB['database'],
                           charset='utf8mb4')
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
    finally:
        conn.close()
    return pd.DataFrame(list(rows), columns=cols)


def load_data(a):
    sym = CFG['symbol']
    t0 = time.time()
    if a.gmx_csv:
        g = pd.read_csv(a.gmx_csv)
        if 'symbol' in g.columns:
            g = g[g['symbol'] == sym]
    else:
        g = db_query('SELECT ts, source_ts, min_price, max_price, price, age_ms, source '
                     'FROM gmx_price WHERE symbol=%s ORDER BY source_ts, ts', (sym,))
    if a.mkt_csv:
        m = pd.read_csv(a.mkt_csv)
        if 'symbol' in m.columns:
            m = m[m['symbol'] == sym]
    else:
        m = db_query('SELECT ts, close, trades, volume_usd, buy_usd, sell_usd, delta_usd '
                     'FROM market_1s WHERE symbol=%s ORDER BY ts', (sym,))
    say('Carga: gmx_price %d filas · market_1s %d filas · %.1fs' % (len(g), len(m), time.time() - t0))

    if a.gmx_source and 'source' in g.columns:
        g = g[g['source'] == a.gmx_source]
    if 'source' in g.columns:
        vc = g['source'].value_counts()
        if len(vc) > 1:
            say('  ⚠ gmx_price tiene varias fuentes: %s — usar --gmx-source para elegir una'
                % dict(vc))

    g = g.reset_index(drop=True)
    G = pd.DataFrame({
        'src': to_ms(g['source_ts'], 'gmx.source_ts'),
        'recv': to_ms(g['ts'], 'gmx.ts') if 'ts' in g.columns else np.nan,
        'mn': pd.to_numeric(g['min_price'], errors='coerce').astype(float).to_numpy(),
        'mx': pd.to_numeric(g['max_price'], errors='coerce').astype(float).to_numpy(),
    })
    G = G[np.isfinite(G.src) & (G.mn > 0) & (G.mx > 0) & (G.mx >= G.mn)]

    m = m.reset_index(drop=True)
    M = pd.DataFrame({
        'ts': to_ms(m['ts'], 'market_1s.ts'),
        'close': pd.to_numeric(m['close'], errors='coerce').astype(float).to_numpy(),
        'buy': pd.to_numeric(m['buy_usd'], errors='coerce').astype(float).fillna(0).to_numpy(),
        'sell': pd.to_numeric(m['sell_usd'], errors='coerce').astype(float).fillna(0).to_numpy(),
    })
    M = M[np.isfinite(M.ts)]
    M['sec'] = (M.ts // 1000 * 1000).astype('int64')
    M = M.groupby('sec', as_index=False).agg(close=('close', 'last'), buy=('buy', 'sum'),
                                             sell=('sell', 'sum'))
    return G, M


# =============================================================================
# SERIE CAUSAL 1s (as-of backward)
# =============================================================================

def build_grid(G, avail_mode):
    """Devuelve dict con arrays sobre la grilla de 1 s.

    avail_mode='source' (default, = motor live): una quote se considera
      conocida en su source_ts.
    avail_mode='recv': una quote sólo puede usarse cuando además fue RECIBIDA
      (ts <= T). Más estricto: elimina la ventaja de 'conocer' la quote antes de
      que llegue. Requiere que ts y source_ts estén en el mismo reloj UTC.
    """
    G = G.copy()
    if avail_mode == 'recv':
        G['avail'] = np.fmax(G['recv'].to_numpy(), G['src'].to_numpy())
        G = G.sort_values(['avail', 'src'], kind='mergesort')
        # como el live: se ignora una quote cuyo source_ts no avanza
        prev_max = G['src'].cummax().shift(1).fillna(-np.inf)
        G = G[G['src'] > prev_max]
    else:
        G = G.sort_values(['src', 'recv'], kind='mergesort')
        G = G.drop_duplicates('src', keep='first')   # el live ignora source_ts repetido
        G['avail'] = G['src']
    G = G.reset_index(drop=True)

    av = G['avail'].to_numpy(dtype='float64')
    src = G['src'].to_numpy(dtype='float64')
    t0 = int(math.ceil(av[0] / 1000.0) * 1000)
    t1 = int(math.ceil(av[-1] / 1000.0) * 1000) - 1000    # último T < última quote
    T = np.arange(t0, t1 + 1, 1000, dtype='int64')
    idx = np.searchsorted(av, T, side='right') - 1
    ok = idx >= 0
    idx_c = np.where(ok, idx, 0)
    age = np.where(ok, T - src[idx_c], np.inf)
    valid = ok & (age >= 0) & (age <= CFG['max_age_ms'])
    mn = np.where(valid, G['mn'].to_numpy()[idx_c], np.nan)
    mx = np.where(valid, G['mx'].to_numpy()[idx_c], np.nan)
    mid = (mn + mx) / 2.0

    if avail_mode == 'source' and np.isfinite(G['recv']).any():
        lag = (G['recv'] - G['src']).dropna()
        say('  gmx ts - source_ts: mediana %.0f ms · p95 %.0f ms (latencia de recepción)'
            % (lag.median(), lag.quantile(0.95)))
    return dict(T=T, valid=valid, mn=mn, mx=mx, mid=mid, age=age)


def align_market(grid, M, lag_s):
    """Coloca cada bucket market_1s (ts=X) en el boundary X + lag_s.

    Con lag_s=1 en T se usan buckets con ts <= T-1s (conservador: si ts es el
    INICIO del segundo, el bucket ts=T todavía no terminó en el instante T).
    """
    T = grid['T']
    n = len(T)
    buy = np.zeros(n)
    sell = np.zeros(n)
    present = np.zeros(n)
    close0 = np.full(n, np.nan)          # sin lag, sólo para chequear relojes
    pos = ((M['sec'].to_numpy() - T[0]) // 1000).astype('int64')
    inside = (pos >= 0) & (pos < n)
    close0[pos[inside]] = M['close'].to_numpy()[inside]
    pl = pos + lag_s
    inside = (pl >= 0) & (pl < n)
    buy[pl[inside]] = M['buy'].to_numpy()[inside]
    sell[pl[inside]] = M['sell'].to_numpy()[inside]
    present[pl[inside]] = 1.0
    grid.update(buy=buy, sell=sell, mkt_present=present, mkt_close0=close0)


def clock_check(grid):
    """Chequeo de relojes GMX vs market_1s (el bug histórico fue mezclar relojes)."""
    mid = pd.Series(grid['mid'])
    cl = pd.Series(grid['mkt_close0']).ffill(limit=5)
    both = mid.notna() & cl.notna()
    if both.sum() < 600:
        say('  ⚠ chequeo de relojes: solapamiento GMX/market insuficiente (%d s)' % both.sum())
        return
    lvl = ((cl - mid).abs() / mid)[both].median() * 100
    say('  nivel: |close_mkt - mid_gmx| mediana = %.3f %%' % lvl)
    # desfasaje en horas (zona horaria mal guardada)
    mm = mid.groupby(np.arange(len(mid)) // 60).mean()
    cm = cl.groupby(np.arange(len(cl)) // 60).mean()
    best = None
    for h in range(-12, 13):
        d = ((cm.shift(h * 60) - mm).abs() / mm).median()
        if np.isfinite(d) and (best is None or d < best[1]):
            best = (h, d)
    if best and best[0] != 0:
        say('  ⚠⚠ el mejor ajuste de nivel es con desfasaje de %+d h: posible error de '
            'zona horaria entre gmx_price y market_1s' % best[0])
    # desfasaje fino en segundos via correlación de retornos 1s
    rm = mid.diff()
    rc = cl.diff()
    res = []
    for L in range(-30, 31):
        c = rm.corr(rc.shift(L))
        if np.isfinite(c):
            res.append((L, c))
    if res:
        L, c = max(res, key=lambda x: x[1])
        c0 = dict(res).get(0, float('nan'))
        say('  correlación retornos 1s: lag 0 = %.3f · mejor lag = %+d s (%.3f)  '
            '[lag>0: market adelantado respecto de GMX]' % (c0, L, c))


# =============================================================================
# CRUCES
# =============================================================================

def find_crosses(grid):
    mid = pd.Series(grid['mid'])
    # rolling con min_periods=n: un NaN (stale) en la ventana => NaN => recalienta
    sF = mid.rolling(CFG['fast'], min_periods=CFG['fast']).mean().to_numpy()
    sS = mid.rolling(CFG['slow'], min_periods=CFG['slow']).mean().to_numpy()
    grid.update(sF=sF, sS=sS)
    T, valid = grid['T'], grid['valid']
    cd = CFG['cooldown_s'] * 1000

    last_sign = 0
    last_cross_t = 0
    all_cross = []      # índices de TODOS los cruces (incluidos los bloqueados)
    cands = []          # (i, side, prev_cross_t)
    for i in range(len(T)):
        if not valid[i]:
            last_sign = 0               # = resetSignal() del live
            continue
        a, b = sF[i], sS[i]
        if a != a or b != b:
            continue
        d = a - b
        sg = 1 if d > 0 else (-1 if d < 0 else 0)
        if sg == 0:
            continue
        cross = sg if (last_sign != 0 and sg != last_sign) else 0
        last_sign = sg
        if not cross:
            continue
        prev = last_cross_t
        allowed = (not prev) or (T[i] - prev >= cd)
        last_cross_t = int(T[i])        # todo cruce resetea el cooldown
        all_cross.append(i)
        if allowed:
            cands.append((i, 'LONG' if cross > 0 else 'SHORT', prev))
    return np.array(all_cross, dtype='int64'), cands


# =============================================================================
# FEATURES CAUSALES (todo con índices <= i, i = segundo de la señal N)
# =============================================================================

def win(x, i, w):
    """x[i-w .. i] (w+1 puntos) o None si sale de rango o hay NaN."""
    if i - w < 0:
        return None
    s = x[i - w:i + 1]
    if np.isnan(s).any():
        return None
    return s


def features(grid, i, k, all_cross, roll):
    mid, sF, sS, T = grid['mid'], grid['sF'], grid['sS'], grid['T']
    px = mid[i]
    o = {}
    sep = sF - sS if 'sep' not in grid else grid['sep']
    o['sma_fast'] = sF[i]
    o['sma_slow'] = sS[i]
    o['separation'] = sep[i]
    o['abs_separation'] = abs(sep[i])
    o['sep_dir_bps'] = k * sep[i] / px * 1e4
    o['abs_sep_bps'] = abs(sep[i]) / px * 1e4
    for w in (5, 10, 20):
        for nm, s in (('fast', sF), ('slow', sS)):
            v = (s[i] - s[i - w]) / w if i - w >= 0 else np.nan
            o['slope_%s_%ds' % (nm, w)] = v
            o['slope_%s_%ds_dir_bps' % (nm, w)] = k * v / px * 1e4
    for w in (5, 10):
        v = sep[i] - sep[i - w] if i - w >= 0 else np.nan
        o['separation_change_%ds' % w] = v
        o['sep_chg_%ds_dir_bps' % w] = k * v / px * 1e4

    for w in (30, 60, 120):
        s = win(mid, i, w)
        if s is None:
            for c in ('path_abs', 'path_pct', 'range_abs', 'range_pct', 'comp', 'eff', 'rv_bps'):
                o['%s_%d' % (c, w)] = np.nan
            continue
        path = np.abs(np.diff(s)).sum()
        rng = s.max() - s.min()
        lr = np.diff(np.log(s))
        o['path_abs_%d' % w] = path
        o['path_pct_%d' % w] = path / px * 100
        o['range_abs_%d' % w] = rng
        o['range_pct_%d' % w] = rng / px * 100
        o['comp_%d' % w] = path / rng if rng > 0 else np.nan
        o['eff_%d' % w] = abs(s[-1] - s[0]) / path if path > 0 else np.nan
        o['rv_bps_%d' % w] = math.sqrt((lr ** 2).sum()) * 1e4

    for w in (10, 30, 60):
        B = roll['buy_%d' % w][i]
        S = roll['sell_%d' % w][i]
        V = B + S
        dr = (B - S) / V if V > 0 else np.nan
        o['buy_usd_%d' % w] = B
        o['sell_usd_%d' % w] = S
        o['vol_usd_%d' % w] = V
        o['delta_ratio_%d' % w] = dr
        o['flow_dir_%d' % w] = k * dr
        o['balance_%d' % w] = 1 - abs(dr) if V > 0 else np.nan
        o['relvol_%d' % w] = roll['relvol_%d' % w][i]
    o['mkt_rows_60'] = roll['present_60'][i]

    for w in (5, 10, 20, 30):
        o['ret_%ds_dir_bps' % w] = (k * (px / mid[i - w] - 1) * 1e4) if i - w >= 0 else np.nan

    tsig = T[i]
    prior = all_cross[all_cross < i]
    o['crosses_10m'] = int(((T[prior] > tsig - 600_000)).sum())
    o['secs_since_prev_cross'] = (tsig - T[prior[-1]]) / 1000 if len(prior) else np.nan
    o['spread_bps'] = (grid['mx'][i] - grid['mn'][i]) / px * 1e4
    o['gmx_age_ms'] = grid['age'][i]
    o['hour_utc'] = pd.Timestamp(int(tsig), unit='ms').hour
    return o


def rolling_market(grid):
    n = len(grid['T'])
    out = {}

    def rsum(x, w):
        cs = np.concatenate([[0.0], np.cumsum(x)])
        r = np.full(n, np.nan)
        r[w - 1:] = cs[w:] - cs[:-w]
        return r

    for w in (10, 30, 60):
        out['buy_%d' % w] = rsum(grid['buy'], w)
        out['sell_%d' % w] = rsum(grid['sell'], w)
        vol = out['buy_%d' % w] + out['sell_%d' % w]
        ref = (pd.Series(vol).shift(w)
               .rolling(CFG['relvol_ref_s'], min_periods=int(CFG['relvol_ref_s'] * 2 / 3))
               .median().to_numpy())
        with np.errstate(divide='ignore', invalid='ignore'):
            out['relvol_%d' % w] = np.where(ref > 0, vol / ref, np.nan)
    out['present_60'] = rsum(grid['mkt_present'], 60)
    return out


# =============================================================================
# RESULTADOS FUTUROS (independientes por señal)
# =============================================================================

def outcomes(grid, j, side, entry_px, nv):
    """j = índice de entrada (N+1). Devuelve dict de resultados."""
    n = len(grid['T'])
    k = 1 if side == 'LONG' else -1
    exit_arr = grid['mn'] if side == 'LONG' else grid['mx']
    qty = CFG['exposure'] / entry_px
    o = {}

    def close_idx(sec):
        # live: timeout en el primer segundo VÁLIDO con elapsed >= H
        p = j + sec
        if p >= n:
            return None
        c = nv[p]
        return None if c >= n else int(c)

    Hmax = max(CFG['horizons_min'] + [s[2] for s in CFG['sims']]) * 60
    cmax = close_idx(Hmax)
    end = cmax if cmax is not None else n - 1
    pnl = k * qty * (exit_arr[j + 1:end + 1] - entry_px)     # pnl[p] = segundo j+1+p

    for H in CFG['horizons_min']:
        c = close_idx(H * 60)
        if c is None:
            o['complete_%dm' % H] = 0
            o['mfe_%dm' % H] = o['mae_%dm' % H] = o['pnl_at_%dm' % H] = np.nan
            continue
        seg = pnl[:c - j]
        o['complete_%dm' % H] = 1
        o['mfe_%dm' % H] = np.nanmax(seg) if np.isfinite(seg).any() else np.nan
        o['mae_%dm' % H] = np.nanmin(seg) if np.isfinite(seg).any() else np.nan
        o['pnl_at_%dm' % H] = pnl[c - j - 1]

    # barreras dentro de la ventana principal de 100 m
    Hb = CFG['horizons_min'][-1]
    cb = close_idx(Hb * 60)
    seg = pnl[:cb - j] if cb is not None else None
    for X in CFG['tp_levels']:
        if seg is None:
            o['t_tp%d' % X] = np.nan
            continue
        h = np.flatnonzero(seg >= X)
        o['t_tp%d' % X] = float(h[0] + 1) if len(h) else np.nan
    for X in CFG['sl_levels']:
        if seg is None:
            o['t_sl%d' % X] = np.nan
            continue
        h = np.flatnonzero(seg <= -X)
        o['t_sl%d' % X] = float(h[0] + 1) if len(h) else np.nan
    for a in CFG['tp_levels']:
        for b in CFG['sl_levels']:
            if seg is None:
                v = np.nan
            else:
                ta = o['t_tp%d' % a] if o['t_tp%d' % a] == o['t_tp%d' % a] else np.inf
                tb = o['t_sl%d' % b] if o['t_sl%d' % b] == o['t_sl%d' % b] else np.inf
                v = 1 if ta < tb else (-1 if tb < ta else 0)   # 1 TP primero, -1 SL, 0 ninguno
            o['tp%d_sl%d' % (a, b)] = v

    for tp, sl, to in CFG['sims']:
        key = 'sim_tp%d_sl%d_to%d' % (tp, -sl, to)
        c = close_idx(to * 60)
        if c is None:
            o[key + '_reason'] = None
            for s in ('gross', 'net', 'dur_s', 'trigger_pnl'):
                o[key + '_' + s] = np.nan
            continue
        seg2 = pnl[:c - j]
        hit = np.flatnonzero((seg2 >= tp) | (seg2 <= sl))
        if len(hit):
            p = int(hit[0])
            trig = seg2[p]
            reason = 'TP' if trig >= tp else 'SL'          # prioridad TP > SL
            gross = float(tp if reason == 'TP' else sl)
            dur = p + 1
        else:
            reason, trig = 'TIMEOUT', seg2[-1]
            gross, dur = float(trig), c - j
        o[key + '_reason'] = reason
        o[key + '_gross'] = gross
        o[key + '_net'] = gross - CFG['fee_rt']
        o[key + '_dur_s'] = dur
        o[key + '_trigger_pnl'] = float(trig)
    o['path_valid_frac'] = float(np.isfinite(pnl).mean()) if len(pnl) else np.nan
    return o


# =============================================================================
# DATASET
# =============================================================================

def build_dataset(grid, all_cross, cands):
    T, valid = grid['T'], grid['valid']
    n = len(T)
    idxs = np.where(valid, np.arange(n), n)
    nv = np.minimum.accumulate(idxs[::-1])[::-1]
    grid['sep'] = grid['sF'] - grid['sS']
    roll = rolling_market(grid)
    rows = []
    skipped = 0
    for i, side, _prev in cands:
        j = i + 1
        if j >= n or not valid[j]:
            skipped += 1
            continue
        k = 1 if side == 'LONG' else -1
        entry_px = grid['mx'][j] if side == 'LONG' else grid['mn'][j]
        r = dict(signal_ms=int(T[i]), signal_ts=iso(T[i]), side=side,
                 entry_ms=int(T[j]), entry_ts=iso(T[j]), entry_price=entry_px,
                 mid_at_signal=grid['mid'][i])
        r.update(features(grid, i, k, all_cross, roll))
        r.update(outcomes(grid, j, side, entry_px, nv))
        rows.append(r)
    return pd.DataFrame(rows), skipped


# =============================================================================
# ANÁLISIS
# =============================================================================

def rank(x):
    return pd.Series(x).rank().to_numpy()


def pearson(a, b):
    if len(a) < 3 or np.std(a) == 0 or np.std(b) == 0:
        return np.nan
    return float(np.corrcoef(a, b)[0, 1])


def spearman(x, y):
    m = np.isfinite(x) & np.isfinite(y)
    if m.sum() < 10:
        return np.nan
    return pearson(rank(x[m]), rank(y[m]))


def block_boot_ci(x, y, blocks, it, seed=7):
    """IC 95% del Spearman remuestreando BLOQUES DE 1 HORA (las señales se
    solapan en el tiempo, así que no son independientes entre sí)."""
    m = np.isfinite(x) & np.isfinite(y)
    x, y, blocks = rank(x[m]), rank(y[m]), blocks[m]
    ub = np.unique(blocks)
    if len(ub) < 5:
        return np.nan, np.nan
    groups = [np.flatnonzero(blocks == b) for b in ub]
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(it):
        pick = rng.integers(0, len(groups), len(groups))
        ii = np.concatenate([groups[p] for p in pick])
        out.append(pearson(x[ii], y[ii]))
    out = np.array(out)
    out = out[np.isfinite(out)]
    if not len(out):
        return np.nan, np.nan
    return float(np.percentile(out, 2.5)), float(np.percentile(out, 97.5))


def band_table(d, col, prim, early_mask):
    x = d[col].to_numpy(dtype=float)
    ok = np.isfinite(x)
    if ok.sum() < CFG['min_band_n']:
        return None
    try:
        b, edges = pd.qcut(x[ok], CFG['n_bands'], labels=False, retbins=True, duplicates='drop')
    except ValueError:
        return None
    sub = d[ok].copy()
    sub['_band'] = b
    em = early_mask[ok]
    rows = []
    for bi in sorted(sub['_band'].unique()):
        s = sub[sub['_band'] == bi]
        e = s[em[sub['_band'].to_numpy() == bi]]
        l = s[~em[sub['_band'].to_numpy() == bi]]
        rows.append(dict(
            feature=col, band=int(bi) + 1, lo=edges[int(bi)], hi=edges[int(bi) + 1],
            n=len(s),
            tp25_first_pct=(s['tp25_sl75'] == 1).mean() * 100,
            mfe100=s['mfe_100m'].mean(), mae100=s['mae_100m'].mean(),
            gross=s[prim + '_gross'].mean(), net=s[prim + '_net'].mean(),
            net_early=e[prim + '_net'].mean(), n_early=len(e),
            net_late=l[prim + '_net'].mean(), n_late=len(l),
        ))
    return pd.DataFrame(rows)


def analyze(d, prim, split_ms):
    c = d[d['complete_100m'] == 1].copy()
    if len(c) < 2 * CFG['min_band_n']:
        say('\n⚠ Muy pocas señales con 100 m completos (%d). Análisis omitido.' % len(c))
        return None, None
    if split_ms is None:
        split_ms = int(np.median(c['signal_ms']))
    early = (c['signal_ms'] < split_ms).to_numpy()
    blocks = (c['signal_ms'] // 3_600_000).to_numpy()
    y = c[prim + '_net'].to_numpy(dtype=float)
    y30 = c['pnl_at_30m'].to_numpy(dtype=float)

    say('\n' + '=' * 100)
    say('ANÁLISIS POR BANDAS (quintiles sobre todas las señales completas; objetivo = %s)' % prim)
    say('split temporal en %s UTC · temprano n=%d · tardío n=%d'
        % (iso(split_ms), early.sum(), (~early).sum()))
    say('Las señales se SOLAPAN en el tiempo: n no es "n independientes". IC por bootstrap '
        'de bloques de 1 h.')
    say('=' * 100)

    bands_all = []
    rank_rows = []
    for grp, cols in FEATURE_GROUPS.items():
        say('\n### %s' % grp)
        for col in cols:
            if col not in c.columns:
                continue
            x = c[col].to_numpy(dtype=float)
            bt = band_table(c, col, prim, early)
            if bt is None:
                say('\n%s: sin datos suficientes' % col)
                continue
            bands_all.append(bt)
            r_all = spearman(x, y)
            r_e = spearman(x[early], y[early])
            r_l = spearman(x[~early], y[~early])
            r30 = spearman(x, y30)
            lo, hi = block_boot_ci(x, y, blocks, CFG['boot_iter'])
            mono = pearson(bt['band'].to_numpy(float), rank(bt['net'].to_numpy()))
            min_n = int(bt['n'].min())
            min_n_half = int(min(bt['n_early'].min(), bt['n_late'].min()))
            if min_n < CFG['min_band_n'] or min_n_half < CFG['min_band_n'] // 2:
                verdict = 'INSUFICIENTE'
            elif not np.isfinite(r_all) or abs(r_all) < 0.03:
                verdict = 'SIN EFECTO'
            elif np.sign(r_e) != np.sign(r_l):
                verdict = 'INCONSISTENTE'
            elif np.isfinite(lo) and (lo > 0 or hi < 0) and abs(mono) >= 0.7 \
                    and min(abs(r_e), abs(r_l)) >= 0.04:
                verdict = 'CANDIDATA'
            else:
                verdict = 'DÉBIL'
            rank_rows.append(dict(group=grp, feature=col, n=int(np.isfinite(x).sum()),
                                  rho_net=r_all, ci_lo=lo, ci_hi=hi, rho_early=r_e,
                                  rho_late=r_l, rho_pnl30=r30, monotonic=mono,
                                  q_hi_minus_q_lo=bt['net'].iloc[-1] - bt['net'].iloc[0],
                                  min_band_n=min_n, verdict=verdict))
            say('\n%s   rho(net)=%s [%s,%s]  temprano=%s  tardío=%s  rho(pnl30)=%s  '
                'monot=%s  → %s'
                % (col, f(r_all, 3), f(lo, 3), f(hi, 3), f(r_e, 3), f(r_l, 3), f(r30, 3),
                   f(mono, 2), verdict))
            say('  band            rango          n  TP25<SL75%  MFE100  MAE100   bruto    '
                'neto | neto_temp (n) | neto_tard (n)')
            for _, b in bt.iterrows():
                warn = ' ⚠' if b['n'] < CFG['min_band_n'] else ''
                say('  Q%d  %9s .. %-9s %5d  %9s %7s %7s %7s %7s | %7s (%3d) | %7s (%3d)%s'
                    % (b['band'], f(b['lo'], 3), f(b['hi'], 3), b['n'],
                       f(b['tp25_first_pct'], 1), f(b['mfe100'], 1), f(b['mae100'], 1),
                       f(b['gross'], 2), f(b['net'], 2), f(b['net_early'], 2),
                       b['n_early'], f(b['net_late'], 2), b['n_late'], warn))

    rk = pd.DataFrame(rank_rows)
    bands = pd.concat(bands_all, ignore_index=True) if bands_all else pd.DataFrame()

    say('\n' + '=' * 100)
    say('RANKING DE FEATURES (orden por |rho| mínimo entre mitades, sólo si el signo coincide)')
    say('=' * 100)
    if len(rk):
        rk['stable_rho'] = np.where(np.sign(rk.rho_early) == np.sign(rk.rho_late),
                                    np.minimum(rk.rho_early.abs(), rk.rho_late.abs()), 0.0)
        rk = rk.sort_values('stable_rho', ascending=False)
        say('%-26s %6s %7s %15s %7s %7s %7s %6s %8s  %s'
            % ('feature', 'n', 'rho', 'IC95 bloques', 'temp', 'tard', 'pnl30', 'monot',
               'Qhi-Qlo', 'veredicto'))
        for _, r in rk.iterrows():
            say('%-26s %6d %7s %7s,%-7s %7s %7s %7s %6s %8s  %s'
                % (r.feature, r.n, f(r.rho_net, 3), f(r.ci_lo, 3), f(r.ci_hi, 3),
                   f(r.rho_early, 3), f(r.rho_late, 3), f(r.rho_pnl30, 3), f(r.monotonic, 2),
                   f(r.q_hi_minus_q_lo, 2), r.verdict))
        nf = len(rk)
        say('\nOJO comparaciones múltiples: con %d features, ~%d podrían salir "significativas" '
            'por azar al 95%%. Una CANDIDATA es una hipótesis para validar con datos NUEVOS, '
            'no un hallazgo.' % (nf, max(1, round(nf * 0.05))))

    # contexto: lado y hora
    say('\nPor lado:')
    for s, g in c.groupby('side'):
        say('  %-5s n=%4d  TP25<SL75 %5.1f%%  neto medio %7s  (temprano %7s / tardío %7s)'
            % (s, len(g), (g['tp25_sl75'] == 1).mean() * 100, f(g[prim + '_net'].mean()),
               f(g[early[c.side.to_numpy() == s]][prim + '_net'].mean()),
               f(g[~early[c.side.to_numpy() == s]][prim + '_net'].mean())))
    say('Por franja horaria UTC (4 h):')
    for h, g in c.groupby(c['hour_utc'] // 4 * 4):
        warn = ' ⚠' if len(g) < CFG['min_band_n'] else ''
        say('  %02d-%02dh n=%4d  TP25<SL75 %5.1f%%  neto medio %7s%s'
            % (h, h + 4, len(g), (g['tp25_sl75'] == 1).mean() * 100,
               f(g[prim + '_net'].mean()), warn))
    return bands, rk


def summary(grid, all_cross, cands, d, skipped, prim):
    T, valid = grid['T'], grid['valid']
    say('\n' + '=' * 100)
    say('RESUMEN')
    say('=' * 100)
    say('Serie 1s: %s → %s UTC · %d s (%.1f h) · válidos %.2f %%'
        % (iso(T[0]), iso(T[-1]), len(T), len(T) / 3600, valid.mean() * 100))
    gaps = int(((~valid[1:]) & valid[:-1]).sum())
    say('Episodios stale (>10 s sin quote): %d' % gaps)
    say('market_1s presente en %.1f %% de los segundos de la grilla'
        % (grid['mkt_present'].mean() * 100))
    say('Cruces SMA%d/%d: %d totales · %d pasan CD%d · %d omitidos por N+1 inválido'
        % (CFG['fast'], CFG['slow'], len(all_cross), len(cands), CFG['cooldown_s'], skipped))
    if not len(d):
        return
    c = d[d['complete_100m'] == 1]
    say('Señales en dataset: %d (LONG %d / SHORT %d) · con 100 m completos: %d'
        % (len(d), (d.side == 'LONG').sum(), (d.side == 'SHORT').sum(), len(c)))
    if not len(c):
        return
    say('\nMFE/MAE ($, bruto, sobre precio de salida GMX) — medias y medianas:')
    for H in CFG['horizons_min']:
        cc = d[d['complete_%dm' % H] == 1]
        say('  %3dm  n=%4d  MFE media %6s mediana %6s · MAE media %7s mediana %7s · '
            'PnL a %dm media %6s'
            % (H, len(cc), f(cc['mfe_%dm' % H].mean()), f(cc['mfe_%dm' % H].median()),
               f(cc['mae_%dm' % H].mean()), f(cc['mae_%dm' % H].median()), H,
               f(cc['pnl_at_%dm' % H].mean())))
    say('\nProbabilidad de tocar cada barrera dentro de 100 m (y mediana de segundos):')
    for X in CFG['tp_levels']:
        t = c['t_tp%d' % X]
        say('  +$%-3d  %5.1f %%  t_med %6s s' % (X, t.notna().mean() * 100, f(t.median(), 0)))
    for X in CFG['sl_levels']:
        t = c['t_sl%d' % X]
        say('  -$%-3d  %5.1f %%  t_med %6s s' % (X, t.notna().mean() * 100, f(t.median(), 0)))
    say('\n%% de señales donde el TP llega ANTES que el SL (100 m):')
    say('        ' + ''.join('SL-%-6d' % b for b in CFG['sl_levels']))
    for a in CFG['tp_levels']:
        say('  TP+%-3d ' % a + ''.join('%6.1f   ' % ((c['tp%d_sl%d' % (a, b)] == 1).mean() * 100)
                                        for b in CFG['sl_levels']))
    say('\nSimulaciones INDEPENDIENTES (cada cruce su propia operación, sin bloqueo; '
        'fees $%.0f):' % CFG['fee_rt'])
    half = int(np.median(c['signal_ms']))
    for tp, sl, to in CFG['sims']:
        k = 'sim_tp%d_sl%d_to%d' % (tp, -sl, to)
        s = d[d[k + '_reason'].notna()]
        if not len(s):
            continue
        r = s[k + '_reason'].value_counts()
        e = s[s.signal_ms < half]
        l = s[s.signal_ms >= half]
        say('  TP+%d SL%d TO%dm: n=%d · TP %d (%.1f%%) · SL %d · TO %d · bruto medio %s · '
            'neto medio %s · neto medio temprano %s / tardío %s'
            % (tp, sl, to, len(s), r.get('TP', 0), r.get('TP', 0) / len(s) * 100,
               r.get('SL', 0), r.get('TIMEOUT', 0), f(s[k + '_gross'].mean()),
               f(s[k + '_net'].mean()), f(e[k + '_net'].mean()), f(l[k + '_net'].mean())))
        sl_hits = s[s[k + '_reason'] == 'SL']
        if len(sl_hits):
            say('     (SL registrado en %d; PnL real en el segundo del disparo: media %s, '
                'peor %s → el SL "exacto" es optimista)'
                % (sl, f(sl_hits[k + '_trigger_pnl'].mean()),
                   f(sl_hits[k + '_trigger_pnl'].min())))
    say('  NOTA: la suma de netos independientes NO es el PnL de una estrategia (las '
        'operaciones se superponen); usar la media por señal.')


# =============================================================================
# MAIN
# =============================================================================

def main():
    ap = argparse.ArgumentParser(description='CONCEPTITO LAB — dataset de cruces SMA independientes')
    ap.add_argument('--gmx-csv', help='leer gmx_price desde CSV en vez de MariaDB')
    ap.add_argument('--mkt-csv', help='leer market_1s desde CSV en vez de MariaDB')
    ap.add_argument('--gmx-source', help='filtrar gmx_price.source (si hay varias)')
    ap.add_argument('--avail', choices=['source', 'recv'], default='source',
                    help="'source' (= live) o 'recv' (quote usable sólo si además ts <= T)")
    ap.add_argument('--mkt-lag', type=int, default=CFG['mkt_lag_s'],
                    help='segundos de retraso para usar un bucket market_1s (default 1)')
    ap.add_argument('--split-ts', help='fecha UTC que separa bloque temprano/tardío '
                                       '(default: mediana de las señales)')
    ap.add_argument('--since', help='sólo señales desde esta fecha UTC')
    ap.add_argument('--until', help='sólo señales hasta esta fecha UTC')
    ap.add_argument('--out-dir', default=os.path.dirname(os.path.abspath(__file__)))
    a = ap.parse_args()

    os.makedirs(a.out_dir, exist_ok=True)
    say('CONCEPTITO LAB · %s · SMA%d/%d · CD%d · exposición $%.0f · fees $%.0f · avail=%s · '
        'mkt_lag=%ds' % (CFG['symbol'], CFG['fast'], CFG['slow'], CFG['cooldown_s'],
                         CFG['exposure'], CFG['fee_rt'], a.avail, a.mkt_lag))
    G, M = load_data(a)
    if len(G) < 1000:
        raise SystemExit('ERROR: muy pocas quotes GMX válidas (%d)' % len(G))
    say('GMX %s → %s · market %s → %s'
        % (iso(G.src.min()), iso(G.src.max()), iso(M.sec.min()), iso(M.sec.max())))

    t0 = time.time()
    grid = build_grid(G, a.avail)
    align_market(grid, M, a.mkt_lag)
    say('Chequeo de relojes GMX vs market_1s:')
    clock_check(grid)
    all_cross, cands = find_crosses(grid)
    d, skipped = build_dataset(grid, all_cross, cands)
    say('Dataset construido en %.1fs' % (time.time() - t0))

    if len(d):
        if a.since:
            d = d[d.signal_ms >= to_ms([a.since], 'since')[0]]
        if a.until:
            d = d[d.signal_ms <= to_ms([a.until], 'until')[0]]

    tp, sl, to = CFG['sims'][0]
    prim = 'sim_tp%d_sl%d_to%d' % (tp, -sl, to)
    summary(grid, all_cross, cands, d, skipped, prim)

    p_ds = os.path.join(a.out_dir, 'conceptito_signals_dataset.csv')
    d.to_csv(p_ds, index=False)

    split_ms = to_ms([a.split_ts], 'split-ts')[0] if a.split_ts else None
    bands, rk = (None, None)
    if len(d):
        bands, rk = analyze(d, prim, split_ms)
    p_b = os.path.join(a.out_dir, 'conceptito_bands.csv')
    p_r = os.path.join(a.out_dir, 'conceptito_feature_ranking.csv')
    if bands is not None:
        bands.to_csv(p_b, index=False)
        rk.to_csv(p_r, index=False)

    say('\nArchivos:')
    say('  %s  (%d señales × %d columnas)' % (p_ds, len(d), d.shape[1] if len(d) else 0))
    if bands is not None:
        say('  %s\n  %s' % (p_b, p_r))
    p_rep = os.path.join(a.out_dir, 'conceptito_report.txt')
    say('  %s' % p_rep)
    with open(p_rep, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(REPORT) + '\n')


if __name__ == '__main__':
    main()
