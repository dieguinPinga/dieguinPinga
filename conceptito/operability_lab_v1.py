#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OPERABILITY LAB v1 — ¿cuándo está el mercado OPERABLE para Conceptito?

Hipótesis: no predecir LONG/SHORT. Detectar si hay "movimiento suficiente y
serrucho" (OPERABLE) o no (NEUTRO) y dejar que el cruce SMA35/70 decida el lado.

Externo y experimental: solo LEE MariaDB (prrr_market.gmx_price). No toca Node-RED.

Semántica (= conceptito_lab.py / live):
  * reloj gmx_price.source_ts, as-of backward por segundo, stale > 10 s
  * SMA35/70 sobre mid, cooldown 32 s (todo cruce resetea), señal N -> entrada N+1
  * LONG entra max / sale min, SHORT al revés
  * referencia: TP +25 / SL -75 exactos, TO 100 m al precio real, fees $3, $3000

Protocolo:
  1. Dataset de cruces INDEPENDIENTES. En TRAIN solo entran señales cuyo
     resultado a 100 m se conoce ANTES del corte (ningún precio post-corte).
  2. Análisis univariado de features lentas (solo TRAIN), con estabilidad entre
     las dos mitades de TRAIN.
  3. Máximo 3 reglas OPERABLE elegidas con un procedimiento fijo (solo TRAIN).
  4. Simulación secuencial maxPos=1: BASE vs cada regla, TRAIN y POST-CORTE
     por separado. Lo post-corte no interviene en ninguna elección.

Uso:
  python3 operability_lab_v1.py
  python3 operability_lab_v1.py --gmx-csv gmx.csv --cut "2026-10-03 04:00:00"
"""

import argparse
import math
import os

import numpy as np
import pandas as pd


# =============================================================================
# CONFIGURACIÓN
# =============================================================================

DB = dict(host='127.0.0.1', port=3306, database='prrr_market',
          user='prrr', password='prrr_local_2026')

SYMBOL = 'ZEC'
CUT_DEFAULT = '2026-10-04 21:15:45'

FAST, SLOW, COOLDOWN_S = 35, 70, 32
MAX_AGE_MS = 10_000
EXPOSURE = 3000.0
TP, SL, TO_MIN = 25.0, -75.0, 100
FEE_RT = 3.0

FF_LIMIT_S = 300          # features lentas toleran huecos de hasta 5 min (carry-forward causal)
N_BANDS = 5
MIN_BAND_N = 30
MIN_COVER = 0.30          # una regla debe dejar pasar >= 30 % de las señales TRAIN
PCTS = (33, 50)           # umbrales candidatos (percentiles TRAIN) para reglas de 1 feature

FEATURES = ['rv_5m', 'rv_15m', 'rv_30m',
            'range_5m', 'range_15m', 'range_30m',
            'comp_5m', 'comp_15m', 'comp_30m',
            'eff_5m', 'eff_15m', 'eff_30m',
            'absret_5m', 'absret_15m', 'absret_30m',
            'volratio_5_30', 'rev_5m', 'rev_15m']
MOV = ['rv_5m', 'rv_15m', 'rv_30m', 'range_5m', 'range_15m', 'range_30m',
       'absret_5m', 'absret_15m', 'absret_30m', 'volratio_5_30']
SAW = ['comp_5m', 'comp_15m', 'comp_30m', 'rev_5m', 'rev_15m', 'eff_5m', 'eff_15m', 'eff_30m']
# dirección que predice la hipótesis "movimiento + serrucho" (+1: más es mejor)
HYP_DIR = {**{k: 1 for k in MOV}, **{k: (-1 if k.startswith('eff') else 1) for k in SAW}}


# =============================================================================
# TIEMPO / CARGA / GRILLA / CRUCES (misma lógica que conceptito_lab.py)
# =============================================================================

def iso(ms):
    return pd.Timestamp(int(ms), unit='ms').strftime('%Y-%m-%d %H:%M:%S')


def to_ms(col, name):
    """epoch-ms explícito para cualquier datetime64 (ns/us/ms), string o número.
    Evita el bug 2026 -> 1970. DATETIME sin zona = UTC."""
    s = pd.Series(col).reset_index(drop=True)
    if pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_datetime64_any_dtype(s):
        v = s.astype('float64').to_numpy()
        med = np.nanmedian(v)
        v = v / 1e6 if med > 1e17 else v / 1e3 if med > 1e14 else v if med > 1e11 else v * 1e3
        out = np.round(v)
    else:
        dt = pd.to_datetime(s, errors='coerce')
        if getattr(dt.dt, 'tz', None) is not None:
            dt = dt.dt.tz_convert('UTC').dt.tz_localize(None)
        out = np.round(((dt - pd.Timestamp('1970-01-01')) / pd.Timedelta(milliseconds=1))
                       .to_numpy(dtype='float64'))
    ok = out[np.isfinite(out)]
    if len(ok):
        y0 = pd.Timestamp(int(ok.min()), unit='ms').year
        y1 = pd.Timestamp(int(ok.max()), unit='ms').year
        if y0 < 2020 or y1 > 2035:
            raise SystemExit('ERROR: %s convertido a años %d..%d (¿bug 1970?)' % (name, y0, y1))
    return out


def load_gmx(a):
    if a.gmx_csv:
        g = pd.read_csv(a.gmx_csv)
        if 'symbol' in g.columns:
            g = g[g['symbol'] == SYMBOL]
    else:
        import pymysql
        conn = pymysql.connect(host=DB['host'], port=DB['port'], user=DB['user'],
                               password=DB['password'], database=DB['database'],
                               charset='utf8mb4')
        try:
            with conn.cursor() as cur:
                cur.execute('SELECT ts, source_ts, min_price, max_price, source FROM gmx_price '
                            'WHERE symbol=%s ORDER BY source_ts, ts', (SYMBOL,))
                cols = [d[0] for d in cur.description]
                g = pd.DataFrame(list(cur.fetchall()), columns=cols)
        finally:
            conn.close()
    if a.gmx_source and 'source' in g.columns:
        g = g[g['source'] == a.gmx_source]
    g = g.reset_index(drop=True)
    G = pd.DataFrame({
        'src': to_ms(g['source_ts'], 'source_ts'),
        'recv': to_ms(g['ts'], 'ts') if 'ts' in g.columns else np.nan,
        'mn': pd.to_numeric(g['min_price'], errors='coerce').astype(float).to_numpy(),
        'mx': pd.to_numeric(g['max_price'], errors='coerce').astype(float).to_numpy(),
    })
    return G[np.isfinite(G.src) & (G.mn > 0) & (G.mx >= G.mn)]


def build_grid(G):
    G = G.sort_values(['src', 'recv'], kind='mergesort').drop_duplicates('src', keep='first')
    src = G['src'].to_numpy(dtype='float64')
    t0 = int(math.ceil(src[0] / 1000.0) * 1000)
    t1 = int(math.ceil(src[-1] / 1000.0) * 1000) - 1000
    T = np.arange(t0, t1 + 1, 1000, dtype='int64')
    idx = np.searchsorted(src, T, side='right') - 1
    ok = idx >= 0
    ic = np.where(ok, idx, 0)
    age = np.where(ok, T - src[ic], np.inf)
    valid = ok & (age >= 0) & (age <= MAX_AGE_MS)
    mn = np.where(valid, G['mn'].to_numpy()[ic], np.nan)
    mx = np.where(valid, G['mx'].to_numpy()[ic], np.nan)
    n = len(T)
    nv = np.minimum.accumulate(np.where(valid, np.arange(n), n)[::-1])[::-1]
    lv = np.maximum.accumulate(np.where(valid, np.arange(n), -1))
    return dict(T=T, valid=valid, mn=mn, mx=mx, mid=(mn + mx) / 2.0, nv=nv, lv=lv)


def find_crosses(g):
    mid = pd.Series(g['mid'])
    sF = mid.rolling(FAST, min_periods=FAST).mean().to_numpy()
    sS = mid.rolling(SLOW, min_periods=SLOW).mean().to_numpy()
    T, valid = g['T'], g['valid']
    last_sign, last_cross_t, cands = 0, 0, []
    for i in range(len(T)):
        if not valid[i]:
            last_sign = 0
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
        allowed = (not last_cross_t) or (T[i] - last_cross_t >= COOLDOWN_S * 1000)
        last_cross_t = int(T[i])
        if allowed:
            cands.append((i, 1 if cross > 0 else -1))
    return cands


# =============================================================================
# FEATURES LENTAS (no direccionales, causales: índices <= t)
# =============================================================================

def slow_features(g):
    mid = pd.Series(g['mid']).ffill(limit=FF_LIMIT_S)
    lr = np.log(mid).diff()
    lr2 = lr ** 2
    d = mid.diff()
    F = {}
    for m in (5, 15, 30):
        w = m * 60
        mp = int(w * 0.9)
        rmax = mid.rolling(w + 1, min_periods=mp).max()
        rmin = mid.rolling(w + 1, min_periods=mp).min()
        rng = rmax - rmin
        path = d.abs().rolling(w, min_periods=mp).sum()
        F['rv_%dm' % m] = (np.sqrt(lr2.rolling(w, min_periods=mp).sum()) * 1e4).to_numpy()
        F['range_%dm' % m] = (rng / mid * 1e4).to_numpy()                          # bps
        F['comp_%dm' % m] = (path / rng.where(rng > 0)).to_numpy()
        F['eff_%dm' % m] = ((mid - mid.shift(w)).abs() / path.where(path > 0)).to_numpy()
        F['absret_%dm' % m] = ((mid / mid.shift(w) - 1).abs() * 1e4).to_numpy()   # bps
    v5 = lr2.rolling(300, min_periods=270).mean()
    v30 = lr2.rolling(1800, min_periods=1620).mean()
    F['volratio_5_30'] = np.sqrt(v5 / v30.where(v30 > 0)).to_numpy()
    # reversiones: cambios de signo entre movimientos 1 s no nulos
    s = np.sign(d).replace(0, np.nan)
    last = s.ffill()
    rev = ((s.notna()) & (last.shift(1).notna()) & (s != last.shift(1))).astype(float)
    F['rev_5m'] = rev.rolling(300, min_periods=270).sum().to_numpy()
    F['rev_15m'] = rev.rolling(900, min_periods=810).sum().to_numpy()
    return F


# =============================================================================
# RESULTADO INDEPENDIENTE DE CADA CRUCE
# =============================================================================

def trade_from(g, j, side, data_end):
    """Opera desde j (N+1) con TP/SL/TO. Solo usa índices < data_end.
    Devuelve (reason, gross, exit_i, mfe, mae, complete)."""
    n = len(g['T'])
    entry = g['mx'][j] if side == 1 else g['mn'][j]
    qty = EXPOSURE / entry
    ex = g['mn'] if side == 1 else g['mx']
    p_to = j + TO_MIN * 60
    c = g['nv'][p_to] if p_to < n else n
    complete = c < data_end
    end = c if complete else data_end - 1
    pnl = side * qty * (ex[j + 1:end + 1] - entry)
    fin = np.isfinite(pnl)
    mfe = float(np.nanmax(pnl)) if fin.any() else np.nan
    mae = float(np.nanmin(pnl)) if fin.any() else np.nan
    hit = np.flatnonzero((pnl >= TP) | (pnl <= SL))
    if len(hit):
        k = int(hit[0])
        reason = 'TP' if pnl[k] >= TP else 'SL'
        return reason, (TP if reason == 'TP' else SL), j + 1 + k, mfe, mae, True
    if complete:
        return 'TO', float(pnl[-1]), int(c), mfe, mae, True
    ei = int(g['lv'][data_end - 1])
    gross = float(side * qty * (ex[ei] - entry)) if ei > j else 0.0
    return 'CUT', gross, ei, mfe, mae, False


def build_dataset(g, cands, F, cut_i):
    T, valid = g['T'], g['valid']
    n = len(T)
    rows = []
    for i, side in cands:
        j = i + 1
        if j >= n or not valid[j]:
            continue
        train = j < cut_i
        data_end = cut_i if train else n
        reason, gross, ei, mfe, mae, complete = trade_from(g, j, side, data_end)
        # una señal TRAIN que necesita precios post-corte para resolverse queda fuera
        if not complete:
            continue
        r = dict(signal_ms=int(T[i]), signal_ts=iso(T[i]), side='LONG' if side == 1 else 'SHORT',
                 period='TRAIN' if train else 'POST', reason=reason, gross=gross,
                 net=gross - FEE_RT, mfe_100m=mfe, mae_100m=mae,
                 tp25_first=int(reason == 'TP'))
        for k in FEATURES:
            r[k] = F[k][i]
        rows.append(r)
    return pd.DataFrame(rows)


# =============================================================================
# ESTADÍSTICA
# =============================================================================

def spearman(x, y):
    m = np.isfinite(x) & np.isfinite(y)
    if m.sum() < 10:
        return np.nan
    a = pd.Series(x[m]).rank().to_numpy()
    b = pd.Series(y[m]).rank().to_numpy()
    if a.std() == 0 or b.std() == 0:
        return np.nan
    return float(np.corrcoef(a, b)[0, 1])


def fmt(v, nd=2):
    return '-' if v is None or not np.isfinite(v) else ('{:.%df}' % nd).format(v)


def univariate(tr):
    half = int(np.median(tr.signal_ms))
    h1 = (tr.signal_ms < half).to_numpy()
    y = tr['net'].to_numpy(float)
    out = []
    print('\n' + '=' * 96)
    print('1) CRUCES INDEPENDIENTES — TRAIN (n=%d, mitades separadas en %s UTC)'
          % (len(tr), iso(half)))
    print('   neto medio por señal (TP25/SL75/TO100, fees $3) por quintil de cada feature')
    print('=' * 96)
    print('%-14s %7s %7s %7s  %-48s %s' % ('feature', 'rho', 'rho½1', 'rho½2',
                                         'neto medio Q1 .. Q5', 'TP25% Q1..Q5'))
    for k in FEATURES:
        x = tr[k].to_numpy(float)
        r, r1, r2 = spearman(x, y), spearman(x[h1], y[h1]), spearman(x[~h1], y[~h1])
        ok = np.isfinite(x)
        qn, qt, nmin = [], [], 0
        try:
            b = pd.qcut(x[ok], N_BANDS, labels=False, duplicates='drop')
            sub = tr[ok]
            cnt = np.bincount(b)
            nmin = int(cnt.min())
            for q in range(b.max() + 1):
                qn.append(sub['net'].to_numpy()[b == q].mean())
                qt.append(sub['tp25_first'].to_numpy()[b == q].mean() * 100)
        except ValueError:
            pass
        stable = (min(abs(r1), abs(r2)) if np.isfinite(r1) and np.isfinite(r2)
                  and np.sign(r1) == np.sign(r2) else 0.0)
        out.append(dict(feature=k, rho=r, rho_h1=r1, rho_h2=r2, stable=stable,
                        min_band_n=nmin, hyp_dir=HYP_DIR[k]))
        warn = ' ⚠n<%d' % MIN_BAND_N if nmin and nmin < MIN_BAND_N else ''
        print('%-14s %7s %7s %7s  %-48s %s%s'
              % (k, fmt(r, 3), fmt(r1, 3), fmt(r2, 3), ' '.join('%7s' % fmt(v) for v in qn),
                 ' '.join('%3.0f' % v for v in qt), warn))
    U = pd.DataFrame(out)

    # veredicto de la hipótesis por grupo
    print('\nHipótesis "movimiento + serrucho" (signo esperado de rho vs signo observado,'
          ' mismo signo en ambas mitades):')
    for grp, cols in (('MOVIMIENTO', MOV), ('SERRUCHO', SAW)):
        u = U[U.feature.isin(cols)]
        agree = int(((np.sign(u.rho_h1) == u.hyp_dir) & (np.sign(u.rho_h2) == u.hyp_dir)).sum())
        against = int(((np.sign(u.rho_h1) == -u.hyp_dir) & (np.sign(u.rho_h2) == -u.hyp_dir)).sum())
        print('  %-10s  a favor en ambas mitades: %d/%d · en contra en ambas: %d/%d · mixto: %d'
              % (grp, agree, len(u), against, len(u), len(u) - agree - against))
    return U


# =============================================================================
# REGLAS OPERABLE (procedimiento fijo, solo TRAIN)
# =============================================================================

def eval_mask(tr, mask, half):
    a = tr[mask]
    e = a[a.signal_ms < half]
    l = a[a.signal_ms >= half]
    return dict(cover=mask.mean(), n=len(a), net=a.net.mean(), net_h1=e.net.mean(),
                net_h2=l.net.mean(), tp=a.tp25_first.mean() * 100)


def choose_rules(tr, U):
    """Máx. 3 reglas:
      R1, R2: las 2 features con rho más estable entre mitades (mismo signo), cada una
              con umbral en P33 o P50 de TRAIN hacia el lado favorable; se elige el umbral
              con mejor min(neto½1, neto½2), cobertura >= 30 %.
      R3:     combinación de la hipótesis, con umbrales fijos en la mediana TRAIN:
              mejor feature de MOVIMIENTO del lado favorable Y mejor feature de SERRUCHO
              del lado favorable (si ambas tienen signo estable)."""
    half = int(np.median(tr.signal_ms))
    base = eval_mask(tr, np.ones(len(tr), bool), half)
    print('\n' + '=' * 96)
    print('2) SELECCIÓN DE REGLAS OPERABLE (solo TRAIN, señales independientes)')
    print('=' * 96)
    print('%-44s %6s %5s %7s %7s %7s %6s' % ('regla', 'cubre', 'n', 'neto', 'neto½1',
                                           'neto½2', 'TP%'))
    print('%-44s %5.0f%% %5d %7s %7s %7s %6.1f' % ('BASE (todas)', 100, base['n'],
          fmt(base['net']), fmt(base['net_h1']), fmt(base['net_h2']), base['tp']))

    Us = U[(U.stable > 0) & (U.min_band_n >= MIN_BAND_N)].sort_values('stable', ascending=False)
    rules = []

    def make(feat, direction, thr):
        lab = '%s %s %.4g' % (feat, '>=' if direction > 0 else '<=', thr)
        return dict(label=lab, conds=[(feat, direction, thr)])

    def mask_of(rule, df):
        m = np.ones(len(df), bool)
        for feat, direction, thr in rule['conds']:
            x = df[feat].to_numpy(float)
            with np.errstate(invalid='ignore'):
                m &= (x >= thr) if direction > 0 else (x <= thr)
        return m

    for _, u in Us.head(2).iterrows():
        k, dirn = u.feature, int(np.sign(u.rho))
        x = tr[k].to_numpy(float)
        best = None
        for p in PCTS:
            thr = np.nanpercentile(x, p if dirn > 0 else 100 - p)
            r = make(k, dirn, thr)
            e = eval_mask(tr, mask_of(r, tr), half)
            print('  cand  %-38s %5.0f%% %5d %7s %7s %7s %6.1f'
                  % (r['label'], e['cover'] * 100, e['n'], fmt(e['net']), fmt(e['net_h1']),
                     fmt(e['net_h2']), e['tp']))
            if e['cover'] >= MIN_COVER and np.isfinite(e['net_h1']) and np.isfinite(e['net_h2']):
                sc = min(e['net_h1'], e['net_h2'])
                if best is None or sc > best[0]:
                    best = (sc, r)
        if best:
            rules.append(best[1])

    mv = Us[Us.feature.isin(MOV)].head(1)
    sw = Us[Us.feature.isin(SAW)].head(1)
    if len(mv) and len(sw):
        conds = []
        for _, u in pd.concat([mv, sw]).iterrows():
            conds.append((u.feature, int(np.sign(u.rho)), float(np.nanmedian(tr[u.feature]))))
        lab = ' & '.join('%s %s %.4g' % (f, '>=' if d > 0 else '<=', t) for f, d, t in conds)
        rules.append(dict(label=lab, conds=conds))
    else:
        print('  (sin regla combinada: no hay feature estable de MOVIMIENTO y de SERRUCHO a la vez)')

    print('\nReglas elegidas:')
    for n_, r in enumerate(rules, 1):
        e = eval_mask(tr, mask_of(r, tr), half)
        r['name'] = 'R%d' % n_
        print('  %s  %-46s cubre %4.0f%% · neto %s (½1 %s / ½2 %s)'
              % (r['name'], r['label'], e['cover'] * 100, fmt(e['net']), fmt(e['net_h1']),
                 fmt(e['net_h2'])))
        if not (e['net_h1'] > base['net_h1'] and e['net_h2'] > base['net_h2']):
            print('      ⚠ no mejora al BASE en ambas mitades de TRAIN')
    if not rules:
        print('  ninguna: ninguna feature tiene relación estable en TRAIN')
    return rules, mask_of


# =============================================================================
# SIMULACIÓN SECUENCIAL maxPos=1
# =============================================================================

def simulate(g, cands, allow, win_lo, win_hi, data_end):
    T, valid = g['T'], g['valid']
    trades, blocked, busy_until = [], 0, -1
    for i, side in cands:
        if T[i] < win_lo or T[i] >= win_hi or i <= busy_until:
            continue
        if allow is not None and not allow[i]:
            blocked += 1
            continue
        j = i + 1
        if j >= data_end or not valid[j]:
            continue
        reason, gross, ei, _, _, _ = trade_from(g, j, side, data_end)
        busy_until = ei
        trades.append(dict(side='LONG' if side == 1 else 'SHORT', signal_ts=iso(T[i]),
                           entry_ts=iso(T[j]), exit_ts=iso(T[ei]), reason=reason,
                           gross=gross, fees=FEE_RT, net=gross - FEE_RT, dur_s=int(ei - j)))
    return pd.DataFrame(trades), blocked


def stats(tr):
    if not len(tr):
        return (0, 0, 0, 0, 0.0, 0.0, 0.0, float('nan'))
    r = tr['reason']
    return (len(tr), int((r == 'TP').sum()), int((r == 'SL').sum()),
            int(r.isin(['TO', 'CUT']).sum()), tr.gross.sum(), tr.fees.sum(), tr.net.sum(),
            tr.net.mean())


def allow_series(F, rule):
    n = len(next(iter(F.values())))
    m = np.ones(n, bool)
    for feat, direction, thr in rule['conds']:
        x = F[feat]
        with np.errstate(invalid='ignore'):
            m &= (x >= thr) if direction > 0 else (x <= thr)
    return m


# =============================================================================
# MAIN
# =============================================================================

def main():
    ap = argparse.ArgumentParser(description='Detector OPERABLE/NEUTRO para Conceptito')
    ap.add_argument('--cut', default=CUT_DEFAULT, help='corte de desarrollo (UTC)')
    ap.add_argument('--gmx-csv', help='leer gmx_price desde CSV en vez de MariaDB')
    ap.add_argument('--gmx-source', help='filtrar gmx_price.source')
    ap.add_argument('--out-dir', default=os.path.dirname(os.path.abspath(__file__)))
    a = ap.parse_args()

    cut_ms = int(to_ms([a.cut], 'cut')[0])
    g = build_grid(load_gmx(a))
    T, valid = g['T'], g['valid']
    n = len(T)
    cut_i = int(np.searchsorted(T, cut_ms, side='left'))
    print('OPERABILITY v1 · %s · SMA%d/%d CD%d · TP+%.0f SL%.0f TO%dm · $%.0f · fees $%.0f'
          % (SYMBOL, FAST, SLOW, COOLDOWN_S, TP, SL, TO_MIN, EXPOSURE, FEE_RT))
    print('Datos %s → %s UTC · corte %s · TRAIN %.1f h · POST %.1f h'
          % (iso(T[0]), iso(T[-1]), iso(cut_ms), cut_i / 3600, (n - cut_i) / 3600))

    cands = find_crosses(g)
    F = slow_features(g)
    ds = build_dataset(g, cands, F, cut_i)
    tr = ds[ds.period == 'TRAIN'].reset_index(drop=True)
    po = ds[ds.period == 'POST'].reset_index(drop=True)
    if len(tr) < 2 * MIN_BAND_N:
        raise SystemExit('Muy pocas señales TRAIN completas (%d)' % len(tr))

    U = univariate(tr)
    rules, mask_of = choose_rules(tr, U)

    # independiente POST (solo informativo, nada se elige acá)
    if len(po) and rules:
        print('\nIndependiente POST-CORTE (no usado para elegir): n=%d · BASE neto medio %s'
              % (len(po), fmt(po.net.mean())))
        for r in rules:
            m = mask_of(r, po)
            print('  %s cubre %4.0f%% · neto medio %s (n=%d)'
                  % (r['name'], m.mean() * 100, fmt(po[m].net.mean()), m.sum()))

    # ---------------- secuencial maxPos=1 ----------------
    print('\n' + '=' * 96)
    print('3) SECUENCIAL maxPos=1 — BASE vs OPERABLE  (CUT = abierta en el corte/fin de datos,'
          ' cerrada al último precio; cuenta en TO)')
    print('=' * 96)
    hdr = '%-12s %-6s %4s %4s %4s %4s %9s %8s %9s %8s %6s %6s' % (
        'periodo', 'regla', 'OPS', 'TP', 'SL', 'TO', 'bruto', 'fees', 'neto', 'neto/op',
        'OPER%', 'bloq')
    print(hdr)
    train_mask = np.arange(n) < cut_i
    out = []
    for per, lo, hi, dend, pm in (('TRAIN', int(T[0]), cut_ms, cut_i, train_mask),
                                  ('POST-CORTE', cut_ms, int(T[-1]) + 1, n, ~train_mask)):
        for name, allow in [('BASE', None)] + [(r['name'], allow_series(F, r)) for r in rules]:
            trd, bl = simulate(g, cands, allow, lo, hi, dend)
            s = stats(trd)
            vm = pm & valid
            oper = (allow[vm].mean() * 100) if allow is not None and vm.any() else 100.0
            print('%-12s %-6s %4d %4d %4d %4d %9.2f %8.2f %9.2f %8s %5.1f%% %6s'
                  % (per, name, s[0], s[1], s[2], s[3], s[4], -s[5], s[6], fmt(s[7]), oper,
                     '-' if allow is None else bl))
            if len(trd):
                out.append(trd.assign(period=per, rule=name))
        print()
    for r in rules:
        print('%s = %s' % (r['name'], r['label']))
    print('OPER% = % del tiempo (segundos GMX válidos) marcado OPERABLE · bloq = cruces '
          'rechazados estando flat')

    os.makedirs(a.out_dir, exist_ok=True)
    p1 = os.path.join(a.out_dir, 'operability_v1_signals.csv')
    p2 = os.path.join(a.out_dir, 'operability_v1_trades.csv')
    ds.to_csv(p1, index=False)
    if out:
        pd.concat(out, ignore_index=True).to_csv(p2, index=False)
    print('\nArchivos: %s · %s' % (p1, p2))


if __name__ == '__main__':
    main()
