#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
REGIME STRATEGY v1 — Conceptito secuencial con filtro de régimen LONG/SHORT/NEUTRO.

Externo y experimental: solo LEE MariaDB (prrr_market.gmx_price). No toca Node-RED.

Semántica (misma que conceptito_lab.py y el motor live):
  * reloj = gmx_price.source_ts; para cada boundary T se usa la última quote con
    source_ts <= T; si T - source_ts > 10 s el segundo es stale
  * señal SMA35/70 sobre mid=(min+max)/2, stale reinicia SMA y signo
  * cooldown 32 s, todo cruce resetea el reloj
  * señal en N, entrada exactamente en N+1; LONG entra max / sale min, SHORT al revés
  * TP +25 / SL -75 realizan exactamente el nivel, timeout 100 m al precio real,
    prioridad TP > SL > TIMEOUT; fees $3 por operación; maxPos = 1

Régimen: SOLO información pasada y variables lentas (retornos 5/15/30 m, SMA
5/15/30 m, sus pendientes y volatilidad pasada). Sin flow BUY/SELL.

Protocolo anti-sobreajuste:
  * TRAIN = entradas antes de --cut (default 2026-10-04 12:49:25 UTC); las
    operaciones de TRAIN solo pueden usar precios < cut (si siguen abiertas en el
    corte se cierran al último precio previo, motivo CUT).
  * Se prueban 8 reglas fijadas de antemano. La elección usa SOLO TRAIN.
  * Recién después se simula OOS (entradas >= cut), una sola vez, para BASE y la
    regla elegida. Las demás reglas no se miran en OOS.

Uso:
  python3 regime_strategy_v1.py
  python3 regime_strategy_v1.py --gmx-csv gmx.csv --cut "2026-10-03 06:00:00"
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
CUT_DEFAULT = '2026-10-04 12:49:25'

FAST, SLOW, COOLDOWN_S = 35, 70, 32
MAX_AGE_MS = 10_000
EXPOSURE = 3000.0
TP, SL, TO_MIN = 25.0, -75.0, 100
FEE_RT = 3.0

FF_LIMIT_S = 300       # las features lentas toleran huecos de hasta 5 min (carry-forward causal)
SLOPE_LAG_S = 60       # pendiente de cada SMA lenta: cambio en los últimos 60 s
MIN_OPS_ABS = 8        # mínimo de ops TRAIN para que una regla sea elegible


# =============================================================================
# TIEMPO / CARGA (misma lógica que conceptito_lab.py)
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
    """Serie as-of backward de 1 s sobre source_ts (= motor live)."""
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
    return dict(T=T, valid=valid, mn=mn, mx=mx, mid=(mn + mx) / 2.0)


def find_crosses(g):
    """Cruces SMA35/70 que pasan CD32 (idéntico a conceptito_lab / live)."""
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
# FEATURES LENTAS DE RÉGIMEN (todas causales: usan índices <= t)
# =============================================================================

def regime_features(g):
    mid = pd.Series(g['mid']).ffill(limit=FF_LIMIT_S)
    F = {}
    for m in (5, 15, 30):
        w = m * 60
        F['ret%d' % m] = (mid / mid.shift(w) - 1).to_numpy() * 1e4                    # bps
        sma = mid.rolling(w, min_periods=int(w * 0.9)).mean()
        F['sma%d' % m] = sma.to_numpy()
        F['slope%d' % m] = ((sma / sma.shift(SLOPE_LAG_S) - 1) * 1e4).to_numpy()       # bps/60s
    lr = np.log(mid).diff()
    sig = lr.rolling(1800, min_periods=1500).std().to_numpy() * 1e4                    # bps por s
    F['vol30'] = sig
    for m in (5, 15, 30):
        with np.errstate(invalid='ignore', divide='ignore'):
            F['z%d' % m] = F['ret%d' % m] / (sig * math.sqrt(m * 60))
    F['mid'] = mid.to_numpy()
    return F


def sgn(x):
    return np.where(np.isnan(x), 0, np.sign(x)).astype(int)


def rules(F, vol_ref):
    """8 reglas fijadas de antemano. Devuelven array int: +1 LONG, -1 SHORT, 0 NEUTRO.
    vol_ref = mediana de vol30 calculada SOLO en TRAIN."""
    p, s5, s15, s30 = F['mid'], F['sma5'], F['sma15'], F['sma30']
    with np.errstate(invalid='ignore'):
        up_stack = (p > s5) & (s5 > s15) & (s15 > s30)
        dn_stack = (p < s5) & (s5 < s15) & (s15 < s30)
        r5, r15, r30 = F['ret5'], F['ret15'], F['ret30']
        z15, z30 = F['z15'], F['z30']
        sl15, sl30 = F['slope15'], F['slope30']

        def lr(lng, sht):
            return np.where(lng, 1, np.where(sht, -1, 0))

        score = (sgn(r5) + sgn(r15) + sgn(r30) + sgn(F['slope5']) + sgn(sl15) + sgn(sl30)
                 + sgn(s5 - s15) + sgn(s15 - s30))
        R = {
            'STACK':        lr(up_stack, dn_stack),
            'STACK_SLOPE':  lr(up_stack & (sl30 > 0), dn_stack & (sl30 < 0)),
            'RET_ALL':      lr((r5 > 0) & (r15 > 0) & (r30 > 0), (r5 < 0) & (r15 < 0) & (r30 < 0)),
            'RET_Z05':      lr((r5 > 0) & (z15 > 0.5) & (z30 > 0.5),
                               (r5 < 0) & (z15 < -0.5) & (z30 < -0.5)),
            'RET_Z10':      lr((z15 > 1.0) & (z30 > 1.0), (z15 < -1.0) & (z30 < -1.0)),
            'SLOPES':       lr((sl15 > 0) & (sl30 > 0) & (s15 > s30),
                               (sl15 < 0) & (sl30 < 0) & (s15 < s30)),
            'SCORE6':       lr(score >= 6, score <= -6),
            'SCORE6_VOL':   lr((score >= 6) & (F['vol30'] >= vol_ref),
                               (score <= -6) & (F['vol30'] >= vol_ref)),
        }
    return R


RULE_DESC = {
    'STACK':       'precio > SMA5m > SMA15m > SMA30m (o al revés)',
    'STACK_SLOPE': 'STACK + pendiente SMA30m a favor',
    'RET_ALL':     'ret5m, ret15m, ret30m mismo signo',
    'RET_Z05':     'ret5m a favor y z15, z30 > 0.5σ',
    'RET_Z10':     'z15, z30 > 1.0σ',
    'SLOPES':      'pendientes SMA15m y SMA30m a favor y SMA15m vs SMA30m alineadas',
    'SCORE6':      'al menos 6 de 8 votos lentos a favor (ret, pendientes, orden SMAs)',
    'SCORE6_VOL':  'SCORE6 + vol30m >= mediana TRAIN',
}


# =============================================================================
# SIMULACIÓN SECUENCIAL (maxPos = 1)
# =============================================================================

def simulate(g, cands, regime, win_lo, win_hi, data_end):
    """Entradas con T en [win_lo, win_hi). Precios usables solo con índice < data_end.
    regime=None => BASE. Devuelve (trades DataFrame, bloqueadas)."""
    T, valid = g['T'], g['valid']
    n = len(T)
    idxs = np.where(valid, np.arange(n), n)
    nv = np.minimum.accumulate(idxs[::-1])[::-1]
    last_valid_before = np.where(valid, np.arange(n), -1)
    last_valid_before = np.maximum.accumulate(last_valid_before)
    trades, blocked = [], 0
    busy_until = -1
    for i, side in cands:
        if T[i] < win_lo or T[i] >= win_hi:
            continue
        if i <= busy_until:            # posición abierta, o cerró en este mismo segundo
            continue
        if regime is not None and regime[i] != side:
            blocked += 1
            continue
        j = i + 1
        if j >= data_end or not valid[j]:
            continue
        entry = g['mx'][j] if side == 1 else g['mn'][j]
        qty = EXPOSURE / entry
        ex = g['mn'] if side == 1 else g['mx']
        p_to = j + TO_MIN * 60
        c = nv[p_to] if p_to < n else n
        cut_exit = c >= data_end
        end = (data_end - 1) if cut_exit else c
        pnl = side * qty * (ex[j + 1:end + 1] - entry)
        hit = np.flatnonzero((pnl >= TP) | (pnl <= SL))
        if len(hit):
            k = int(hit[0])
            reason = 'TP' if pnl[k] >= TP else 'SL'
            gross = TP if reason == 'TP' else SL
            exit_i = j + 1 + k
        elif cut_exit:
            exit_i = int(last_valid_before[data_end - 1])
            reason, gross = 'CUT', float(side * qty * (ex[exit_i] - entry)) if exit_i > j else 0.0
        else:
            exit_i, reason, gross = int(c), 'TO', float(pnl[-1])
        busy_until = exit_i
        trades.append(dict(side='LONG' if side == 1 else 'SHORT', signal_ts=iso(T[i]),
                           entry_ts=iso(T[j]), entry_ms=int(T[j]), entry_price=entry,
                           exit_ts=iso(T[exit_i]), reason=reason, gross=gross, fees=FEE_RT,
                           net=gross - FEE_RT, dur_s=int(exit_i - j)))
    return pd.DataFrame(trades), blocked


def stats(tr):
    if not len(tr):
        return dict(ops=0, tp=0, sl=0, to=0, gross=0.0, fees=0.0, net=0.0, per=float('nan'))
    r = tr['reason']
    return dict(ops=len(tr), tp=int((r == 'TP').sum()), sl=int((r == 'SL').sum()),
                to=int(r.isin(['TO', 'CUT']).sum()), gross=tr['gross'].sum(),
                fees=tr['fees'].sum(), net=tr['net'].sum(), per=tr['net'].mean())


def row(label, s, extra=''):
    return '%-24s %4d %4d %4d %4d %9.2f %8.2f %9.2f %8.2f  %s' % (
        label, s['ops'], s['tp'], s['sl'], s['to'], s['gross'], -s['fees'], s['net'],
        s['per'] if s['ops'] else float('nan'), extra)


HDR = '%-24s %4s %4s %4s %4s %9s %8s %9s %8s' % ('', 'OPS', 'TP', 'SL', 'TO', 'bruto', 'fees',
                                                 'neto', 'neto/op')


def regime_time(reg, valid, mask):
    m = mask & valid
    tot = m.sum()
    if not tot:
        return 'sin datos'
    return 'LONG %4.1f%% · SHORT %4.1f%% · NEUTRO %4.1f%%' % (
        (reg[m] == 1).sum() / tot * 100, (reg[m] == -1).sum() / tot * 100,
        (reg[m] == 0).sum() / tot * 100)


# =============================================================================
# MAIN
# =============================================================================

def main():
    ap = argparse.ArgumentParser(description='Conceptito secuencial con filtro de régimen')
    ap.add_argument('--cut', default=CUT_DEFAULT, help='corte TRAIN/OOS (UTC)')
    ap.add_argument('--gmx-csv', help='leer gmx_price desde CSV en vez de MariaDB')
    ap.add_argument('--gmx-source', help='filtrar gmx_price.source')
    ap.add_argument('--out-dir', default=os.path.dirname(os.path.abspath(__file__)))
    a = ap.parse_args()

    cut_ms = int(to_ms([a.cut], 'cut')[0])
    G = load_gmx(a)
    g = build_grid(G)
    T, valid = g['T'], g['valid']
    n = len(T)
    cut_i = int(np.searchsorted(T, cut_ms, side='left'))     # primer índice >= corte
    print('REGIME v1 · %s · SMA%d/%d CD%d maxPos1 · $%.0f · TP+%.0f SL%.0f TO%dm · fees $%.0f'
          % (SYMBOL, FAST, SLOW, COOLDOWN_S, EXPOSURE, TP, SL, TO_MIN, FEE_RT))
    print('Datos %s → %s UTC · corte %s · TRAIN %.1f h · OOS %.1f h'
          % (iso(T[0]), iso(T[-1]), iso(cut_ms), cut_i / 3600, (n - cut_i) / 3600))

    cands = find_crosses(g)
    F = regime_features(g)
    train_mask = np.arange(n) < cut_i
    vol_ref = float(np.nanmedian(F['vol30'][train_mask & valid]))   # SOLO TRAIN
    R = rules(F, vol_ref)

    # ---------------- TRAIN: selección ----------------
    t_lo, t_mid = int(T[0]), int(T[0] + (cut_ms - T[0]) // 2)

    def train_run(reg):
        tr, bl = simulate(g, cands, reg, t_lo, cut_ms, cut_i)
        h1 = tr[tr.entry_ms < t_mid]['net'].sum() if len(tr) else 0.0
        h2 = tr[tr.entry_ms >= t_mid]['net'].sum() if len(tr) else 0.0
        return tr, bl, h1, h2

    base_tr, _, b1, b2 = train_run(None)
    base_s = stats(base_tr)
    min_ops = max(MIN_OPS_ABS, int(math.ceil(0.3 * base_s['ops'])))
    print('\nTRAIN — selección (OOS NO mirado). Criterio: ops >= %d y máximo de '
          'min(neto 1ª mitad, neto 2ª mitad) de TRAIN' % min_ops)
    print(HDR + '  bloq  neto½1/½2')
    print(row('BASE', base_s, '   -  %7.2f / %7.2f' % (b1, b2)))
    res = {}
    for name, reg in R.items():
        tr, bl, h1, h2 = train_run(reg)
        s = stats(tr)
        res[name] = (s, bl, h1, h2)
        flag = '' if s['ops'] >= min_ops else ' (pocas ops)'
        print(row(name, s, '%4d  %7.2f / %7.2f%s' % (bl, h1, h2, flag)))

    elig = [k for k, v in res.items() if v[0]['ops'] >= min_ops]
    if not elig:
        print('\nNinguna regla tiene suficientes operaciones en TRAIN. Sin elección.')
        return
    best = max(elig, key=lambda k: (min(res[k][2], res[k][3]), res[k][0]['net']))
    print('\nElegida: %s — %s' % (best, RULE_DESC[best]))
    if res[best][0]['net'] <= base_s['net'] or min(res[best][2], res[best][3]) <= min(b1, b2):
        print('⚠ La regla elegida NO supera claramente al BASE en TRAIN: el filtro de régimen '
              'no muestra ventaja ni siquiera in-sample.')
    if res[best][0]['net'] <= 0:
        print('⚠ La regla elegida pierde plata en TRAIN: se eligió "la que menos pierde", '
              'no una estrategia con ventaja.')

    # ---------------- OOS: una sola vez ----------------
    reg = R[best]
    best_tr, best_bl, _, _ = train_run(reg)
    oos_base, _ = simulate(g, cands, None, cut_ms, int(T[-1]) + 1, n)
    oos_best, oos_bl = simulate(g, cands, reg, cut_ms, int(T[-1]) + 1, n)

    print('\n' + '=' * 92)
    print('RESULTADO  (CUT = operación TRAIN abierta en el corte, cerrada al último precio previo;'
          ' cuenta en TO)')
    print('=' * 92)
    print(HDR)
    print(row('TRAIN  BASE', base_s))
    print(row('TRAIN  ' + best, stats(best_tr)))
    print(row('OOS    BASE', stats(oos_base)))
    print(row('OOS    ' + best, stats(oos_best)))
    if not len(oos_base):
        print('(OOS sin operaciones: todavía no hay datos suficientes después del corte)')
    print('\nRégimen %s — tiempo en cada estado (segundos GMX válidos):' % best)
    print('  TRAIN: ' + regime_time(reg, valid, train_mask))
    print('  OOS  : ' + regime_time(reg, valid, ~train_mask))
    print('Cruces bloqueados por régimen (estando flat): TRAIN %d · OOS %d' % (best_bl, oos_bl))
    print('vol30 de referencia (mediana TRAIN): %.3f bps/s' % vol_ref)

    os.makedirs(a.out_dir, exist_ok=True)
    out = []
    for per, lab, tr in (('TRAIN', 'BASE', base_tr), ('TRAIN', best, best_tr),
                         ('OOS', 'BASE', oos_base), ('OOS', best, oos_best)):
        if len(tr):
            out.append(tr.assign(period=per, strategy=lab))
    p = os.path.join(a.out_dir, 'regime_v1_trades.csv')
    if out:
        pd.concat(out, ignore_index=True).to_csv(p, index=False)
        print('\nOperaciones: %s' % p)


if __name__ == '__main__':
    main()
