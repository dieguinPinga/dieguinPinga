#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BREADTH LAB v1 — ¿cuántos exchanges de ZEC acompañan la señal GMX?

Única variable nueva: BREADTH MULTI-EXCHANGE. Todo lo demás es la estrategia
BASE actual, sin re-optimizar:

  ZEC · señal y ejecución GMX · SMA35/70 · cooldown 32 s · MOVEMENT ON
  (range_30m >= 66.04 bps, igual que CONCEPTITO_MOVEMENT_V1) · señal N →
  entrada N+1 · maxPos=1 · TP +25 / SL -75 exactos · TO 100 m · $3000 · fees $3 ·
  LONG entra max / sale min, SHORT al revés.

Reutiliza de operability_lab_v1.py (misma carpeta): carga GMX, conversión de
tiempos, grilla causal 1 s, cruces con cooldown y la función de operación
(trade_from: TP/SL/TO y ejecución GMX). Lo único que se agrega acá es:
  1. MOV66 con la definición exacta del motor live MOVEMENT_V1;
  2. detección de la tabla histórica por exchange en MariaDB;
  3. el breadth causal y el filtro.

Breadth en la señal N (boundary T_N):
  * cada venue (exchange, y spot/perp si existe) se compara SOLO contra sí mismo:
        ret_L = p(T_N) / p(T_N - L) - 1,   L = 3, 5, 10 s
    p(x) = último precio del venue DISPONIBLE en x (ver "disponibilidad").
  * un venue es válido si p(T_N) y p(T_N - L) tienen <= FRESH_S de antigüedad.
  * LONG: breadth = (# venues con ret > 0) / (# válidos). SHORT: ret < 0.
    Un venue válido sin cambio (ret == 0) participa pero no acompaña.
  * se exigen >= 3 venues válidos; si hay menos, la entrada se bloquea y se
    cuenta aparte (blocked_few_ex).
  * disponibilidad: si la tabla tiene una columna de recepción se usa
    max(ts, recv); si los ts son buckets de 1 s exactos (inicio del segundo)
    el bucket recién se considera conocido en ts + 1 s. Nunca se usa un dato
    con disponibilidad > T_N (la entrada es en T_N + 1 s).

Uso:
  python3 breadth_lab_v1.py                      # detecta la tabla sola
  python3 breadth_lab_v1.py --list-tables        # sólo inspecciona la base
  python3 breadth_lab_v1.py --ex-table trades_raw --ex-ts ts --ex-exchange exchange \
          --ex-price price [--ex-market market] [--ex-recv recv_ts]
"""

import argparse
import math
import os
import sys

import numpy as np
import pandas as pd

import operability_lab_v1 as OL   # lógica ya validada (carga, grilla, cruces, ejecución)


# =============================================================================
# CONFIGURACIÓN (BASE fija: no se optimiza)
# =============================================================================

CUT_DEFAULT = OL.CUT_DEFAULT           # mismo TRAIN/POST-CORTE que operability_lab_v1
MOVE_WINDOW_S = 1800
MOVE_THRESHOLD_BPS = 66.04

LAGS = (3, 5, 10)
THRESHOLDS = (0.50, 0.60, 0.70)
MIN_VENUES = 3
FRESH_S = 10                            # mismo stale máximo que GMX

TIME_NAMES = ['ts', 'trade_ts', 'event_ts', 'timestamp', 'time', 't', 'ts_ms', 'exchange_ts']
RECV_NAMES = ['recv_ts', 'received_ts', 'recv', 'ingest_ts', 'local_ts', 'arrival_ts']
SYM_NAMES = ['symbol', 'sym', 'asset', 'base', 'coin', 'pair', 'instrument']
EX_NAMES = ['exchange', 'ex', 'venue', 'source', 'src', 'exch']
PX_NAMES = ['price', 'px', 'last', 'last_price', 'close', 'close_price', 'mid', 'trade_price']
MKT_NAMES = ['market', 'market_type', 'type', 'kind', 'instrument_type', 'is_perp', 'spot_perp',
             'product', 'contract_type']
SKIP_TABLES = {'market_1s', 'conceptito_trades'}

PROPOSED_DDL = """
-- Tabla propuesta (escribirla desde el normalizador de la etapa 1, que ya tiene
-- cada trade con su exchange en memoria — md_ring — pero no lo persiste):
CREATE TABLE IF NOT EXISTS prrr_market.market_ex_1s (
  ts            DATETIME(3)   NOT NULL COMMENT 'inicio del segundo, UTC',
  symbol        VARCHAR(16)   NOT NULL,
  exchange      VARCHAR(32)   NOT NULL,
  market        ENUM('spot','perp') NOT NULL,
  last_price    DECIMAL(20,8) NOT NULL COMMENT 'precio del último trade del segundo',
  last_trade_ts DATETIME(3)   NOT NULL COMMENT 'hora del exchange del último trade',
  recv_ts       DATETIME(3)   NOT NULL COMMENT 'hora local de recepción del último trade',
  trades        INT           NOT NULL,
  buy_usd       DOUBLE        NOT NULL,
  sell_usd      DOUBLE        NOT NULL,
  PRIMARY KEY (symbol, exchange, market, ts),
  KEY k_sym_ts (symbol, ts)
) ENGINE=InnoDB;
-- Una fila por (exchange, market, segundo) sólo cuando hubo trades.
-- Con recv_ts el lab puede usar la disponibilidad real (sin look-ahead) y
-- con market separa spot de perp. ~3-8 filas/s para ZEC.
"""


# =============================================================================
# DB
# =============================================================================

def connect():
    import pymysql
    D = OL.DB
    return pymysql.connect(host=D['host'], port=D['port'], user=D['user'],
                           password=D['password'], database=D['database'], charset='utf8mb4')


def q(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        return pd.DataFrame(list(cur.fetchall()), columns=cols)


def pick(cols, names):
    low = {c.lower(): c for c in cols}
    for n in names:
        if n in low:
            return low[n]
    return None


def inspect_db(conn, verbose):
    """SHOW TABLES / SHOW COLUMNS y detección de tablas con (tiempo, símbolo, exchange, precio)."""
    tabs = q(conn, 'SHOW TABLES').iloc[:, 0].tolist()
    print('MariaDB %s: %d tablas' % (OL.DB['database'], len(tabs)))
    found = []
    for t in tabs:
        cols = q(conn, 'SHOW COLUMNS FROM `%s`' % t)
        names = cols['Field'].tolist()
        if verbose:
            print('  %-28s %s' % (t, ', '.join('%s:%s' % (r.Field, r.Type) for r in cols.itertuples())))
        if t in SKIP_TABLES:
            continue
        m = dict(table=t, ts=pick(names, TIME_NAMES), sym=pick(names, SYM_NAMES),
                 ex=pick(names, EX_NAMES), px=pick(names, PX_NAMES),
                 mkt=pick(names, MKT_NAMES), recv=pick(names, RECV_NAMES))
        if m['recv'] == m['ts']:
            m['recv'] = None
        if m['ts'] and m['ex'] and m['px']:
            found.append(m)
    return tabs, found


def probe(conn, m, symbol):
    where, params = '', ()
    if m['sym']:
        where, params = 'WHERE `%s` LIKE %%s' % m['sym'], ('%' + symbol + '%',)
    r = q(conn, 'SELECT COUNT(*) n, COUNT(DISTINCT `%s`) nex, MIN(`%s`) t0, MAX(`%s`) t1 '
                'FROM `%s` %s' % (m['ex'], m['ts'], m['ts'], m['table'], where), params)
    return r.iloc[0].to_dict()


def report_missing(tabs, found, probes):
    print('\n' + '=' * 90)
    print('NO HAY HISTÓRICO POR EXCHANGE SUFICIENTE PARA BREADTH — no se inventa nada.')
    print('=' * 90)
    print('Tablas en la base: %s' % ', '.join(tabs))
    if found:
        print('Candidatas revisadas (tiene tiempo + exchange + precio):')
        for m, p in zip(found, probes):
            print('  %-24s exchanges distintos ZEC=%s filas=%s' % (m['table'], p.get('nex'), p.get('n')))
    print('market_1s mezcla exchanges: su columna `exchanges` es sólo la CANTIDAD de exchanges '
          'del segundo, no el precio de cada uno.')
    print('\nFalta, como mínimo: timestamp · symbol · exchange · precio por exchange '
          '(idealmente spot/perp y hora de recepción).')
    print(PROPOSED_DDL)


# =============================================================================
# MOV66 — definición exacta de CONCEPTITO_MOVEMENT_V1 (movePut)
# =============================================================================

def movement_on(g):
    """ON en T si range30 = (max - min) / px * 1e4 >= 66.04 sobre los segundos VÁLIDOS
    con t' en (T - 30 min, T], y el buffer tiene >= 1799 s de historia (warm-up).
    Los segundos stale no entran al buffer; el buffer sólo se vacía tras >= 30 min
    sin datos (como el live, que no lo resetea por stale)."""
    T, valid, mid = g['T'], g['valid'], g['mid']
    s = pd.Series(mid)
    hi = s.rolling(MOVE_WINDOW_S, min_periods=1).max().to_numpy()
    lo = s.rolling(MOVE_WINDOW_S, min_periods=1).min().to_numpy()
    rng = (hi - lo) / mid * 1e4
    n = len(T)
    since = np.zeros(n, dtype='int64')
    last_valid_t, cur_since = None, None
    for i in range(n):
        if valid[i]:
            if last_valid_t is None or T[i] - last_valid_t >= MOVE_WINDOW_S * 1000:
                cur_since = T[i]                    # buffer vacío -> arranca de nuevo
            last_valid_t = T[i]
            since[i] = cur_since
    warm = valid & (T - since >= MOVE_WINDOW_S * 1000 - 1000)
    on = warm & (rng >= MOVE_THRESHOLD_BPS)
    return on, warm, rng


# =============================================================================
# BREADTH CAUSAL
# =============================================================================

def load_exchange(conn, m, symbol, t_lo_ms, t_hi_ms):
    cols = ['`%s` AS ts' % m['ts'], '`%s` AS ex' % m['ex'], '`%s` AS px' % m['px']]
    if m['mkt']:
        cols.append('`%s` AS mkt' % m['mkt'])
    if m['recv']:
        cols.append('`%s` AS recv' % m['recv'])
    where, params = '', ()
    if m['sym']:
        where, params = 'WHERE `%s` LIKE %%s' % m['sym'], ('%' + symbol + '%',)
    df = q(conn, 'SELECT %s FROM `%s` %s' % (', '.join(cols), m['table'], where), params)
    return prepare_exchange(df, t_lo_ms, t_hi_ms)


def prepare_exchange(df, t_lo_ms=None, t_hi_ms=None):
    X = pd.DataFrame({'ts': OL.to_ms(df['ts'], 'exchange.ts'),
                      'px': pd.to_numeric(df['px'], errors='coerce').astype(float).to_numpy(),
                      'ex': df['ex'].astype(str).str.lower().to_numpy()})
    if 'mkt' in df.columns:
        X['venue'] = X['ex'] + ':' + df['mkt'].astype(str).str.lower().to_numpy()
    else:
        X['venue'] = X['ex']
    if 'recv' in df.columns:
        X['avail'] = np.fmax(X['ts'], OL.to_ms(df['recv'], 'exchange.recv'))
        mode = 'recv'
    else:
        X['avail'] = X['ts']
        mode = 'ts'
    X = X[np.isfinite(X.ts) & (X.px > 0)]
    # buckets de 1 s (inicio del segundo): se conocen recién al cerrar el segundo
    if mode == 'ts' and len(X) and (X.ts % 1000 == 0).mean() > 0.99:
        X['avail'] = X['ts'] + 1000
        mode = 'bucket(ts+1s)'
    if t_lo_ms is not None:
        X = X[(X.avail >= t_lo_ms) & (X.avail <= t_hi_ms)]
    return X.sort_values(['venue', 'avail', 'ts'], kind='mergesort').reset_index(drop=True), mode


def breadth_at(X, Ts):
    """Para cada T en Ts y cada lag: (#válidos, #suben, #bajan). Sólo datos con avail <= T."""
    out = {L: dict(valid=np.zeros(len(Ts), int), up=np.zeros(len(Ts), int),
                   dn=np.zeros(len(Ts), int)) for L in LAGS}
    Ts = np.asarray(Ts, dtype='float64')
    for venue, v in X.groupby('venue', sort=False):
        av = v['avail'].to_numpy(float)
        px = v['px'].to_numpy(float)

        def asof(t):
            k = np.searchsorted(av, t, side='right') - 1
            ok = k >= 0
            kc = np.where(ok, k, 0)
            fresh = ok & (t - av[kc] <= FRESH_S * 1000)
            return np.where(fresh, px[kc], np.nan)

        p0 = asof(Ts)
        for L in LAGS:
            pL = asof(Ts - L * 1000)
            ok = np.isfinite(p0) & np.isfinite(pL)
            r = np.where(ok, p0 / np.where(ok, pL, 1) - 1, 0.0)
            out[L]['valid'] += ok
            out[L]['up'] += ok & (r > 0)
            out[L]['dn'] += ok & (r < 0)
    return out


# =============================================================================
# SIMULACIÓN SECUENCIAL (= motor: maxPos=1, cruces ignorados con posición abierta
# o en el segundo de salida; MOVEMENT OFF/WARMUP bloquea)
# =============================================================================

def simulate(g, cands, mov_on, B, variant, lo_ms, hi_ms, data_end):
    T, valid = g['T'], g['valid']
    trades = []
    c = dict(blocked_mov=0, blocked_breadth=0, blocked_few_ex=0, checked=0, venues_sum=0,
             covered=0)
    busy_until = -1
    for k, (i, side) in enumerate(cands):
        if T[i] < lo_ms or T[i] >= hi_ms or i <= busy_until:
            continue
        if not mov_on[i]:
            c['blocked_mov'] += 1
            continue
        if variant is not None:
            thr, L = variant
            nv = int(B[L]['valid'][k])
            c['checked'] += 1
            c['venues_sum'] += nv
            if nv < MIN_VENUES:
                c['blocked_few_ex'] += 1
                continue
            c['covered'] += 1
            agree = B[L]['up'][k] if side == 1 else B[L]['dn'][k]
            if agree / nv < thr:
                c['blocked_breadth'] += 1
                continue
        j = i + 1
        if j >= data_end or not valid[j]:
            continue
        reason, gross, ei, _, _, _ = OL.trade_from(g, j, side, data_end)
        busy_until = ei
        trades.append(dict(side='LONG' if side == 1 else 'SHORT', signal_ms=int(T[i]),
                           signal_ts=OL.iso(T[i]), entry_ts=OL.iso(T[j]), exit_ts=OL.iso(T[ei]),
                           reason=reason, gross=gross, fees=OL.FEE_RT,
                           net=gross - OL.FEE_RT, dur_s=int(ei - j),
                           venues=int(B[variant[1]]['valid'][k]) if variant else None))
    return pd.DataFrame(trades), c


def st(tr):
    if not len(tr):
        return dict(ops=0, tp=0, sl=0, to=0, gross=0.0, fees=0.0, net=0.0, per=np.nan)
    r = tr['reason']
    return dict(ops=len(tr), tp=int((r == 'TP').sum()), sl=int((r == 'SL').sum()),
                to=int(r.isin(['TO', 'CUT']).sum()), gross=float(tr.gross.sum()),
                fees=float(tr.fees.sum()), net=float(tr.net.sum()), per=float(tr.net.mean()))


def fmt(v, nd=2):
    return '-' if v is None or (isinstance(v, float) and not np.isfinite(v)) else \
        ('{:.%df}' % nd).format(v)


# =============================================================================
# MAIN
# =============================================================================

def main():
    ap = argparse.ArgumentParser(description='Breadth multi-exchange sobre la BASE MOV66')
    ap.add_argument('--cut', default=CUT_DEFAULT)
    ap.add_argument('--list-tables', action='store_true', help='sólo inspeccionar la base')
    ap.add_argument('--ex-table'), ap.add_argument('--ex-ts'), ap.add_argument('--ex-exchange')
    ap.add_argument('--ex-price'), ap.add_argument('--ex-market'), ap.add_argument('--ex-symbol')
    ap.add_argument('--ex-recv')
    ap.add_argument('--gmx-csv', help='GMX desde CSV (pruebas)')
    ap.add_argument('--ex-csv', help='exchanges desde CSV con columnas ts,exchange,price[,market,recv]')
    ap.add_argument('--gmx-source')
    ap.add_argument('--out-dir', default=os.path.dirname(os.path.abspath(__file__)))
    a = ap.parse_args()
    sym = OL.SYMBOL

    # ---------------- 1) detectar datos por exchange ----------------
    conn = None
    if not a.ex_csv:
        conn = connect()
        tabs, found = inspect_db(conn, verbose=True)
        if a.list_tables:
            return
        if a.ex_table:
            found = [dict(table=a.ex_table, ts=a.ex_ts, ex=a.ex_exchange, px=a.ex_price,
                          mkt=a.ex_market, sym=a.ex_symbol, recv=a.ex_recv)]
        probes = []
        good = None
        for m in found:
            try:
                p = probe(conn, m, sym)
            except Exception as e:      # columna mal detectada, permisos, etc.
                p = dict(n=0, nex=0, err=str(e)[:80])
            probes.append(p)
            print('  candidata %-22s ts=%s ex=%s px=%s sym=%s mkt=%s recv=%s → filas ZEC %s · '
                  'exchanges %s · %s → %s' % (m['table'], m['ts'], m['ex'], m['px'], m['sym'],
                                              m['mkt'], m['recv'], p.get('n'), p.get('nex'),
                                              p.get('t0'), p.get('t1')))
            if good is None and (p.get('nex') or 0) >= MIN_VENUES and (p.get('n') or 0) > 1000:
                good = m
        if good is None:
            report_missing(tabs, found, probes)
            sys.exit(2)
        print('Usando tabla por exchange: %s' % good['table'])

    # ---------------- 2) BASE: GMX, cruces, MOV66 ----------------
    cut_ms = int(OL.to_ms([a.cut], 'cut')[0])
    g = OL.build_grid(OL.load_gmx(argparse.Namespace(gmx_csv=a.gmx_csv, gmx_source=a.gmx_source)))
    T = g['T']
    n = len(T)
    cut_i = int(np.searchsorted(T, cut_ms, side='left'))
    cands = OL.find_crosses(g)
    mov_on, warm, _ = movement_on(g)

    if a.ex_csv:
        X, mode = prepare_exchange(pd.read_csv(a.ex_csv).rename(
            columns={'exchange': 'ex', 'price': 'px', 'market': 'mkt'}))
    else:
        X, mode = load_exchange(conn, good, sym, float(T[0]) - 60_000, float(T[-1]))
        conn.close()
    nven = X['venue'].nunique()
    print('Exchanges: %d filas · %d venues (%s) · disponibilidad=%s'
          % (len(X), nven, ', '.join(sorted(X.venue.unique())[:12]), mode))
    if nven < MIN_VENUES:
        print('Menos de %d venues con datos ZEC: breadth imposible.' % MIN_VENUES)
        print(PROPOSED_DDL)
        sys.exit(2)

    # ventana de evaluación común a TODAS las estrategias (incluida BASE):
    # donde hay GMX y datos de exchanges
    lo_ms = max(int(T[0]), int(X.avail.min()) + 60_000)
    hi_ms = min(int(T[-1]) + 1, int(X.avail.max()) + 1)
    print('BASE · %s · SMA%d/%d CD%d · MOV range30>=%.2f · TP+%.0f SL%.0f TO%dm · $%.0f · '
          'fees $%.0f' % (sym, OL.FAST, OL.SLOW, OL.COOLDOWN_S, MOVE_THRESHOLD_BPS, OL.TP,
                          OL.SL, OL.TO_MIN, OL.EXPOSURE, OL.FEE_RT))
    print('GMX %s → %s · corte %s · ventana evaluada %s → %s (intersección GMX ∩ exchanges)'
          % (OL.iso(T[0]), OL.iso(T[-1]), OL.iso(cut_ms), OL.iso(lo_ms), OL.iso(hi_ms - 1)))
    if lo_ms >= cut_ms:
        print('⚠ los datos por exchange empiezan después del corte: no hay TRAIN para breadth.')

    B = breadth_at(X, [T[i] for i, _ in cands])

    variants = [('BASE', None)] + [('B%d_%ds' % (round(t * 100), L), (t, L))
                                   for L in LAGS for t in THRESHOLDS]
    t_mid = lo_ms + (min(cut_ms, hi_ms) - lo_ms) // 2
    rows, all_tr = [], []
    for name, var in variants:
        tr_t, c_t = simulate(g, cands, mov_on, B, var, lo_ms, min(cut_ms, hi_ms), cut_i)
        tr_p, c_p = simulate(g, cands, mov_on, B, var, max(cut_ms, lo_ms), hi_ms, n)
        s_t, s_p = st(tr_t), st(tr_p)
        h1 = st(tr_t[tr_t.signal_ms < t_mid]) if len(tr_t) else st(tr_t)
        h2 = st(tr_t[tr_t.signal_ms >= t_mid]) if len(tr_t) else st(tr_t)
        chk = c_t['checked'] + c_p['checked']
        r = dict(estrategia=name)
        for pre, s in (('TRAIN', s_t), ('POST', s_p)):
            r.update({'%s ops' % pre: s['ops'], '%s TP' % pre: s['tp'], '%s SL' % pre: s['sl'],
                      '%s TO' % pre: s['to'], '%s gross' % pre: s['gross'],
                      '%s fees' % pre: s['fees'], '%s net' % pre: s['net'],
                      '%s net/op' % pre: s['per']})
        r.update({'blocked_by_breadth': c_t['blocked_breadth'] + c_p['blocked_breadth'],
                  'blocked_few_ex': c_t['blocked_few_ex'] + c_p['blocked_few_ex'],
                  'blocked_by_mov': c_t['blocked_mov'] + c_p['blocked_mov'],
                  'avg_valid_exchanges': (c_t['venues_sum'] + c_p['venues_sum']) / chk if chk else np.nan,
                  'breadth_coverage_pct': (c_t['covered'] + c_p['covered']) / chk * 100 if chk else np.nan,
                  'TRAIN_h1 net': h1['net'], 'TRAIN_h1 ops': h1['ops'],
                  'TRAIN_h2 net': h2['net'], 'TRAIN_h2 ops': h2['ops']})
        for sd in ('LONG', 'SHORT'):
            for pre, tr in (('TRAIN', tr_t), ('POST', tr_p)):
                s = st(tr[tr.side == sd]) if len(tr) else st(tr)
                r['%s %s ops' % (pre, sd)] = s['ops']
                r['%s %s net' % (pre, sd)] = s['net']
        rows.append(r)
        for pre, tr in (('TRAIN', tr_t), ('POST', tr_p)):
            if len(tr):
                all_tr.append(tr.assign(period=pre, estrategia=name))
    R = pd.DataFrame(rows)

    # ---------------- robustez (sin elegir ganador) ----------------
    b = R.iloc[0]
    def robust(r):
        if r.estrategia == 'BASE':
            return np.nan, ''
        d1 = r['TRAIN_h1 net'] - b['TRAIN_h1 net']
        d2 = r['TRAIN_h2 net'] - b['TRAIN_h2 net']
        per_ok = np.isfinite(r['TRAIN net/op']) and np.isfinite(b['TRAIN net/op']) and \
            r['TRAIN net/op'] > b['TRAIN net/op']
        enough = r['TRAIN ops'] >= max(8, 0.3 * b['TRAIN ops'])
        flag = 'INTERESANTE' if (d1 > 0 and d2 > 0 and per_ok and enough) else ''
        if flag and np.isfinite(r['POST net/op']) and np.isfinite(b['POST net/op']) and \
                r['POST net/op'] <= b['POST net/op']:
            flag = 'INTERESANTE · POST contradice'
        if not enough:
            flag = 'pocas ops'
        return min(d1, d2), flag
    rb = R.apply(robust, axis=1, result_type='expand')
    R['robust_min_half_delta'] = rb[0]
    R['nota'] = rb[1]

    # ---------------- impresión ----------------
    print('\n' + '=' * 150)
    print('%-9s | %4s %3s %3s %3s %8s %7s %8s %7s | %4s %3s %3s %3s %8s %7s %8s %7s | %5s %5s %5s %6s | %s'
          % ('estrat', 'ops', 'TP', 'SL', 'TO', 'gross', 'fees', 'net', 'net/op',
             'ops', 'TP', 'SL', 'TO', 'gross', 'fees', 'net', 'net/op',
             'bBrd', 'bFew', 'avgEx', 'cov%', 'nota'))
    print('%-9s | %-52s | %-52s |' % ('', 'TRAIN', 'POST-CORTE'))
    print('-' * 150)
    for _, r in R.iterrows():
        cells = []
        for pre in ('TRAIN', 'POST'):
            cells.append('%4d %3d %3d %3d %8.2f %7.2f %8.2f %7s' % (
                r['%s ops' % pre], r['%s TP' % pre], r['%s SL' % pre], r['%s TO' % pre],
                r['%s gross' % pre], -r['%s fees' % pre], r['%s net' % pre],
                fmt(r['%s net/op' % pre])))
        print('%-9s | %s | %s | %5d %5d %5s %6s | %s'
              % (r.estrategia, cells[0], cells[1], r.blocked_by_breadth, r.blocked_few_ex,
                 fmt(r.avg_valid_exchanges, 1), fmt(r.breadth_coverage_pct, 1), r.nota))
    print('bBrd = bloqueadas por breadth · bFew = bloqueadas por < %d exchanges válidos · '
          'avgEx/cov%% sobre cruces que llegaron al chequeo de breadth' % MIN_VENUES)

    print('\nLONG / SHORT (ops · neto) y mitades de TRAIN (neto · ops):')
    print('%-9s | %-15s %-15s | %-15s %-15s | %-15s %-15s'
          % ('estrat', 'TRAIN LONG', 'TRAIN SHORT', 'POST LONG', 'POST SHORT', 'TRAIN ½1',
             'TRAIN ½2'))
    for _, r in R.iterrows():
        print('%-9s | %3d %10.2f %3d %10.2f | %3d %10.2f %3d %10.2f | %8.2f (%3d) %8.2f (%3d)'
              % (r.estrategia, r['TRAIN LONG ops'], r['TRAIN LONG net'], r['TRAIN SHORT ops'],
                 r['TRAIN SHORT net'], r['POST LONG ops'], r['POST LONG net'],
                 r['POST SHORT ops'], r['POST SHORT net'], r['TRAIN_h1 net'],
                 r['TRAIN_h1 ops'], r['TRAIN_h2 net'], r['TRAIN_h2 ops']))

    os.makedirs(a.out_dir, exist_ok=True)
    p1 = os.path.join(a.out_dir, 'breadth_lab_v1_results.csv')
    p2 = os.path.join(a.out_dir, 'breadth_lab_v1_trades.csv')
    R.to_csv(p1, index=False)
    if all_tr:
        pd.concat(all_tr, ignore_index=True).to_csv(p2, index=False)

    # ---------------- resumen ----------------
    print('\n' + '=' * 90)
    print('RESUMEN')
    print('=' * 90)
    print('BASE: TRAIN %d ops · neto %s · %s/op  |  POST %d ops · neto %s · %s/op'
          % (b['TRAIN ops'], fmt(b['TRAIN net']), fmt(b['TRAIN net/op']), b['POST ops'],
             fmt(b['POST net']), fmt(b['POST net/op'])))
    V = R.iloc[1:].sort_values('robust_min_half_delta', ascending=False).head(3)
    print('Top 3 por robustez = mayor mejora MÍNIMA vs BASE entre las dos mitades de TRAIN '
          '(no es una elección de ganador):')
    for _, r in V.iterrows():
        print('  %-9s Δmin½ %+8.2f · TRAIN %d ops neto %s (%s/op) · POST %d ops neto %s (%s/op)  %s'
              % (r.estrategia, r.robust_min_half_delta, r['TRAIN ops'], fmt(r['TRAIN net']),
                 fmt(r['TRAIN net/op']), r['POST ops'], fmt(r['POST net']),
                 fmt(r['POST net/op']), r.nota))
        if r.robust_min_half_delta > 0 and r['POST ops'] and np.isfinite(b['POST net/op']) \
                and r['POST net/op'] <= b['POST net/op']:
            print('      ⚠ POST contradice TRAIN: en POST no mejora el neto/op del BASE')
    if not (R.nota.str.startswith('INTERESANTE')).any():
        print('Ninguna variante mejora al BASE de forma consistente en ambas mitades de TRAIN.')
    print('\nArchivos: %s · %s' % (p1, p2))


if __name__ == '__main__':
    main()
