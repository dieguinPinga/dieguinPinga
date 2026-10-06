// Entradas: 'tick' · respuestas mysql · db_error · cx_snapshot_req
// Salida 1 → mysql · Salida 2 → navegador
//
// Motor 100% GMX→GMX. Reloj = md_gmx.last[symbol].source_ts.
// Serie de 1 s as-of backward: para el boundary T se usa la última
// cotización con source_ts <= T, con carry-forward hasta 10 s.
//
// CONCEPTITO_SPECTRUM_V1 (paper, en paralelo al motor MOVEMENT original)
// Base idéntica al motor MOVEMENT: SMA35/70, cooldown cross-to-cross 32 s,
// señal N / entrada N+1, maxPos 1, TP/SL/TO, MOVIMIENTO 30 min >= 66.04 bps.
//
// Habilitación MOVIMIENTO ZEC ON/OFF: rango causal de los últimos 30 min
// sobre el GMX MID de 1 s. range30_bps = (max30 - min30) / precio * 10000.
// MOVIMIENTO_ON = range30_bps >= 66.04. Con OFF (o WARMUP) los cruces se
// siguen calculando pero no abren posición; una posición abierta se
// administra normalmente hasta TP / SL / TIMEOUT.
//
// ÚNICA lógica nueva: filtro espectral CONFIRM50, evaluado en el segundo N
// del cruce, después de cooldown y MOVEMENT:
//   espectro SMA 25/50/75/100/150/300/600/1200 sobre el mismo GMX MID 1 s,
//   slope_bps = (SMA(t) / SMA(t-5s) - 1) * 10000,
//   signo +1 si slope > +0.01 · -1 si slope < -0.01 · 0 neutral.
//   CONFIRM50 = SMA25 y SMA50 acompañan la dirección del cruce
//               y al menos 3 de SMA150/300/600/1200 la acompañan.
//   Una SMA sin datos suficientes (slope no calculable) NO acompaña.
// Si CONFIRM50 no pasa no se crea pend (el cruce ya actualizó lastCrossT).

const ENGINE = 'CONCEPTITO_SPECTRUM_V1';

const E = context.get('E', 'memory');

if (!E || !E.cfg) {
    node.warn(ENGINE + ': estado E / E.cfg no inicializado (revisar On Start)');
    return null;
}

const C = E.cfg;
const now = Date.now();

const out = [null, null];

const CROSS_COOLDOWN_MS = 32000;
const GMX_MAX_AGE_MS = 10000;
// Identificador de la simulación en MariaDB (misma tabla conceptito_trades).
// Este motor lee y escribe SÓLO filas con source = SRC: el restore arranca
// limpio y nunca toca las filas del motor MOVEMENT original.
const SRC = 'GMX_SPECTRUM_V1';

const MOVE_WINDOW_MS = 30 * 60000;
const MOVE_THRESHOLD_BPS = 66.04;

// Espectro de medias (CONFIRM50)
const SPEC_PERIODS = [25, 50, 75, 100, 150, 300, 600, 1200];
const SPEC_LAG = 5;                  // slope sobre SMA(t) vs SMA(t-5 s)
const SPEC_NEUTRAL_BPS = 0.01;       // |slope| <= 0.01 bps => neutral 0
const SPEC_CONFIRM_SHORT = [25, 50]; // ambas deben acompañar
const SPEC_CONFIRM_LONG = [150, 300, 600, 1200];
const SPEC_CONFIRM_LONG_MIN = 3;     // al menos 3 de 4 largas acompañan

// Buffer de closes: SMA slow y SMA1200 + lag 5 s.
const CLOSES_MAX = Math.max(
    Number(C.slow) || 0,
    SPEC_PERIODS[SPEC_PERIODS.length - 1] + SPEC_LAG
);


// ============================================================
// INICIALIZACIÓN DEFENSIVA
// ============================================================

function def(k, v) {
    if (E[k] === undefined) {
        E[k] = v;
    }
}

def('phase', 'RESTAURANDO');

if (!Array.isArray(E.q)) E.q = [];
def('inflight', null);
def('seq', 0);
def('backoff', 0);
def('nextTry', 0);
def('dbOk', false);
def('dbErr', '');
def('dbErrors', 0);
def('saved', 0);
def('lastLogAt', 0);
def('lastItem', null);

def('pos', null);
def('pend', null);

if (!Array.isArray(E.closes)) E.closes = [];
def('sF', null);
def('sS', null);
def('lastSign', 0);
def('lastCrossT', 0);
def('last', null);
def('lastClosed', null);

if (!E.totals) {
    E.totals = { ops: 0, tp: 0, sl: 0, to: 0, gross: 0, net: 0, fees: 0 };
}

if (E.totals.gross === undefined) E.totals.gross = 0;

if (!Array.isArray(E.trades)) E.trades = [];
def('tradesRev', 0);
def('sentRev', -1);

if (!Array.isArray(E.newPts)) E.newPts = [];
def('dirty', false);
def('lastEmit', 0);

def('gaps', 0);
def('ignored', 0);
def('openRows', 0);
def('replayUntil', 0);

def('lastBucketT', 0);
def('lastGmxSourceTs', 0);
def('lastGmxQuote', null);
def('nextGmxBoundary', 0);

// MOVIMIENTO ZEC 30 min
if (!Array.isArray(E.r30)) E.r30 = [];
def('r30Since', 0);
def('r30High', null);
def('r30Low', null);
def('r30Bps', null);
def('moveState', 'WARMUP');
def('crossesBlockedMovement', 0);
def('crossesBlockedWarmup', 0);

// ESPECTRO / CONFIRM50
// spectrum      = [{ n, sma, slope, sign }] del último segundo válido
//                 (sma/slope/sign = null si no hay datos suficientes)
// spectrumWarm  = true cuando las 8 pendientes están calculadas
// spectrumState = string 25..1200, p.ej. '↑↑↑↑↑↑↓↓' (· neutral, ? sin datos)
if (!Array.isArray(E.spectrum)) E.spectrum = [];
if (typeof E.spectrumWarm !== 'boolean') E.spectrumWarm = false;
if (typeof E.spectrumState !== 'string') E.spectrumState = '';
if (typeof E.crossesBlockedSpectrum !== 'number') E.crossesBlockedSpectrum = 0;

if (!E.hist || !E.hist.cap || !E.hist.idx || !E.hist.a || !E.hist.b) {
    const cap = Math.max(60, Math.floor(Number(C.histSec) || 3600));

    E.hist = {
        cap: cap,
        idx: new Float64Array(cap),
        a: new Float64Array(cap),
        b: new Float64Array(cap)
    };
}


// ============================================================
// HELPERS
// ============================================================

function pad(n, w) {
    return String(n).padStart(w || 2, '0');
}

function dt(t) {
    const d = new Date(t);

    return (
        d.getUTCFullYear() + '-' +
        pad(d.getUTCMonth() + 1) + '-' +
        pad(d.getUTCDate()) + ' ' +
        pad(d.getUTCHours()) + ':' +
        pad(d.getUTCMinutes()) + ':' +
        pad(d.getUTCSeconds()) + '.' +
        pad(d.getUTCMilliseconds(), 3)
    );
}

function r(v, d) {
    return v == null ? null : Number(v.toFixed(d));
}

function num(v) {
    return v == null ? null : Number(v);
}

function enqueue(item) {
    E.q.push(item);

    if (E.q.length > 1000) {
        E.q.shift();
        E.dbErrors++;
        E.dbErr = 'cola DB llena: se descartó la escritura más vieja';
    }
}

function fail(reason) {
    const m = String(reason).slice(0, 200);

    if (m !== E.dbErr || now - E.lastLogAt > 60000) {
        node.warn(ENGINE + ' DB: ' + m);
        E.lastLogAt = now;
    }

    E.dbErrors++;
    E.dbErr = m;
    E.dbOk = false;

    if (E.inflight && E.inflight.item) {
        E.q.unshift(E.inflight.item);
    }

    E.inflight = null;

    E.backoff = Math.min(
        60000,
        Math.max(3000, E.backoff * 2)
    );

    E.nextTry = now + E.backoff;
}

function resetSignal() {
    E.closes = [];

    E.sF = null;
    E.sS = null;

    E.lastSign = 0;

    // El espectro vive sobre el mismo buffer de closes.
    resetSpectrum();
}


// ============================================================
// ESPECTRO DE MEDIAS / CONFIRM50
// ============================================================

function resetSpectrum() {
    E.spectrum = [];
    E.spectrumWarm = false;
    E.spectrumState = '';
}

// Promedio de los n closes que terminan 'lag' segundos antes del último.
function avgLastLag(n, lag) {
    const a = E.closes;
    const end = a.length - lag;

    if (end - n < 0) return null;

    let s = 0;

    for (let i = end - n; i < end; i++) {
        s += a[i];
    }

    return s / n;
}

// Calcula el espectro con closes hasta el segundo actual (sin look-ahead).
function computeSpectrum() {
    const arr = [];
    let str = '';
    let valid = 0;

    for (let i = 0; i < SPEC_PERIODS.length; i++) {
        const n = SPEC_PERIODS[i];
        const sNow = avgLastLag(n, 0);
        const sPrev = avgLastLag(n, SPEC_LAG);

        let slope = null;
        let sign = null;

        if (sNow != null && sPrev != null && sPrev !== 0) {
            slope = (sNow / sPrev - 1) * 10000;

            sign = slope > SPEC_NEUTRAL_BPS
                ? 1
                : (slope < -SPEC_NEUTRAL_BPS ? -1 : 0);

            valid++;
        }

        arr.push({ n: n, sma: sNow, slope: slope, sign: sign });

        str += sign == null
            ? '?'
            : (sign > 0 ? '↑' : (sign < 0 ? '↓' : '·'));
    }

    E.spectrum = arr;
    E.spectrumWarm = valid === SPEC_PERIODS.length;
    E.spectrumState = str;
}

function specSign(n) {
    const S = E.spectrum;

    for (let i = 0; i < S.length; i++) {
        if (S[i].n === n) return S[i].sign;
    }

    return null;
}

// dir = +1 LONG / -1 SHORT. Una SMA sin datos (sign null) no acompaña.
function confirm50(dir) {

    for (let i = 0; i < SPEC_CONFIRM_SHORT.length; i++) {
        if (specSign(SPEC_CONFIRM_SHORT[i]) !== dir) return false;
    }

    let k = 0;

    for (let i = 0; i < SPEC_CONFIRM_LONG.length; i++) {
        if (specSign(SPEC_CONFIRM_LONG[i]) === dir) k++;
    }

    return k >= SPEC_CONFIRM_LONG_MIN;
}

function specValidCount() {
    let k = 0;

    for (let i = 0; i < E.spectrum.length; i++) {
        if (E.spectrum[i].sign != null) k++;
    }

    return k;
}


// ============================================================
// MOVIMIENTO ZEC ON/OFF (rango causal 30 min)
// ============================================================

function resetMovement() {
    E.r30 = [];
    E.r30Since = 0;

    E.r30High = null;
    E.r30Low = null;
    E.r30Bps = null;

    E.moveState = 'WARMUP';
}

// t = boundary del segundo, px = GMX MID de ese segundo.
// Sólo usa precios con t' <= t (sin look-ahead).
function movePut(t, px) {
    const R = E.r30;

    // Ventana (t - 30 min, t]
    while (R.length && R[0].t <= t - MOVE_WINDOW_MS) {
        R.shift();
    }

    if (!R.length) {
        E.r30Since = t;
    }

    R.push({ t: t, px: px });

    let hi = -Infinity;
    let lo = Infinity;

    for (let i = 0; i < R.length; i++) {
        if (R[i].px > hi) hi = R[i].px;
        if (R[i].px < lo) lo = R[i].px;
    }

    E.r30High = hi;
    E.r30Low = lo;
    E.r30Bps = (hi - lo) / px * 10000;

    // Warm-up: 30 min de historia (1800 segundos) desde que arrancó el buffer.
    const warm = t - E.r30Since >= MOVE_WINDOW_MS - 1000;

    const st = !warm
        ? 'WARMUP'
        : (E.r30Bps >= MOVE_THRESHOLD_BPS ? 'ON' : 'OFF');

    if (st !== E.moveState) {
        node.log(
            ENGINE + ': MOVEMENT ' + E.moveState + ' → ' + st +
            ' (range30 ' + E.r30Bps.toFixed(2) + ' bps · umbral ' +
            MOVE_THRESHOLD_BPS + ' · ' +
            new Date(t).toISOString().slice(11, 19) + ')'
        );

        E.moveState = st;
        E.dirty = true;
    }
}

function moveWarmSec() {
    if (!E.r30.length || !E.last) return 0;

    return Math.min(
        MOVE_WINDOW_MS / 1000,
        Math.round((E.last.t - E.r30Since) / 1000) + 1
    );
}


// ============================================================
// PERSISTENCIA
// ============================================================

const COLS = [
    'symbol',
    'source',
    'status',
    'signal_ts',
    'entry_ts',
    'exit_ts',
    'side',
    'entry_price',
    'exit_price',
    'exit_trigger_price',
    'qty',
    'margin_usd',
    'leverage',
    'exposure_usd',
    'sma_fast',
    'sma_slow',
    'sma_fast_entry',
    'sma_slow_entry',
    'tp_usd',
    'sl_usd',
    'timeout_min',
    'fee_rate',
    'gross_pnl',
    'fee_entry',
    'fee_exit',
    'fees',
    'net_pnl',
    'exit_reason',
    'duration_seconds',
    'mfe_usd',
    'mae_usd'
];

const UPD = [
    'status',
    'exit_ts',
    'exit_price',
    'exit_trigger_price',
    'gross_pnl',
    'fee_exit',
    'fees',
    'net_pnl',
    'exit_reason',
    'duration_seconds',
    'mfe_usd',
    'mae_usd'
];

function saveRow(p, x) {

    const row = [
        C.symbol,
        SRC,
        x ? 'CLOSED' : 'OPEN',

        dt(p.signalT),
        dt(p.entryT),
        x ? dt(x.t) : null,

        p.side,
        p.entryPx,
        x ? r(x.px, 8) : null,
        x ? x.trigger : null,

        r(p.qty, 10),

        p.margin,
        p.lev,
        p.exposure,

        p.fast,
        p.slow,

        r(p.sFast, 8),
        r(p.sSlow, 8),

        p.tp,
        p.sl,
        p.toMin,
        p.feeRate,

        x ? r(x.gross, 4) : null,
        r(p.feeEntry, 4),
        x ? r(x.feeExit, 4) : null,
        x ? r(x.fees, 4) : null,
        x ? r(x.net, 4) : null,

        x ? x.reason : null,
        x ? x.dur : null,

        x ? r(p.mfe, 4) : null,
        x ? r(p.mae, 4) : null
    ];

    const upsert = {
        kind: 'save',
        row: row,

        sql:
            'INSERT INTO ' + C.table +
            ' (' + COLS.join(', ') + ') VALUES ? ' +
            'ON DUPLICATE KEY UPDATE ' +
            UPD.map(function (c) {
                return c + ' = VALUES(' + c + ')';
            }).join(', '),

        params: [[row]]
    };

    if (!x) {
        enqueue(upsert);
        return;
    }

    const vals = UPD.map(function (c) {
        return row[COLS.indexOf(c)];
    });

    enqueue({
        kind: 'close',
        row: row,
        fallback: upsert,

        sql:
            'UPDATE ' + C.table +
            ' SET ' +
            UPD.map(function (c) {
                return c + ' = ?';
            }).join(', ') +
            ' WHERE symbol = ? AND source = ? AND entry_ts = ?',

        params: vals.concat([
            C.symbol,
            SRC,
            dt(p.entryT)
        ])
    });
}


// ============================================================
// SMA / HISTORIAL
// ============================================================

function avgLast(n) {
    const a = E.closes;

    if (a.length < n) return null;

    let s = 0;

    for (let i = a.length - n; i < a.length; i++) {
        s += a[i];
    }

    return s / n;
}

function histPut(t, a, b) {
    const H = E.hist;
    const sec = Math.floor(t / 1000);
    const k = sec % H.cap;

    H.idx[k] = sec;
    H.a[k] = a == null ? NaN : a;
    H.b[k] = b == null ? NaN : b;
}

function levels(p) {
    const k = p.side === 'LONG' ? 1 : -1;

    return {
        tpPx:
            p.entryPx *
            (1 + k * p.tp / p.exposure),

        slPx:
            p.entryPx *
            (1 + k * p.sl / p.exposure)
    };
}


// ============================================================
// GMX
// ============================================================

// q = cotización asociada al segundo procesado:
// { t, source_ts, min, max, mid }
function gmxPx(q, side, action) {

    if (
        !q ||
        !isFinite(q.min) ||
        !isFinite(q.max) ||
        !isFinite(q.t) ||
        !isFinite(q.source_ts) ||
        q.source_ts > q.t ||
        q.t - q.source_ts > GMX_MAX_AGE_MS
    ) {
        return null;
    }

    // LONG:
    // entrada max / salida min
    //
    // SHORT:
    // entrada min / salida max

    return action === 'ENTRY'
        ? (
            side === 'LONG'
                ? q.max
                : q.min
        )
        : (
            side === 'LONG'
                ? q.min
                : q.max
        );
}


// ============================================================
// ABRIR POSICIÓN
// ============================================================

function open(sig, b) {

    const exposure = C.margin * C.lev;

    const px = gmxPx(
        b,
        sig.side,
        'ENTRY'
    );

    if (px == null) {
        node.warn(
            ENGINE + ': entrada omitida; GMX sin cotización válida en N+1'
        );

        return false;
    }

    E.pos = {
        side: sig.side,

        signalT: sig.t,
        entryT: b.t,

        entryPx: px,

        qty: exposure / px,

        margin: C.margin,
        lev: C.lev,
        exposure: exposure,

        fast: C.fast,
        slow: C.slow,

        sFast: sig.sF,
        sSlow: sig.sS,

        tp: C.tp,
        sl: C.sl,
        toMin: C.timeoutMin,

        feeRate: C.feeRate,
        feeBase: C.feeBase,

        feeEntry:
            exposure *
            C.feeRate,

        pnl: 0,
        mfe: 0,
        mae: 0,

        lastPx: px,
        lastT: b.t
    };

    saveRow(
        E.pos,
        null
    );

    E.trades.push({
        entryT: b.t,
        entryPx: px,
        side: sig.side
    });

    E.tradesRev++;

    node.log(
        ENGINE + ': abre ' +
        sig.side +
        ' GMX @ ' +
        px +
        ' (señal GMX MID; cruce en ' +
        new Date(sig.t)
            .toISOString()
            .slice(11, 19) +
        ' · CONFIRM50 · espectro ' +
        (sig.spec || '-') +
        ')'
    );

    return true;
}


// ============================================================
// GESTIÓN POSICIÓN
// ============================================================

function manage(b) {

    const p = E.pos;

    const k =
        p.side === 'LONG'
            ? 1
            : -1;

    const px = gmxPx(
        b,
        p.side,
        'EXIT'
    );

    if (px == null) {
        return false;
    }

    const gross =
        k *
        p.qty *
        (px - p.entryPx);

    p.pnl = gross;
    p.lastPx = px;
    p.lastT = b.t;

    if (gross > p.mfe) {
        p.mfe = gross;
    }

    if (gross < p.mae) {
        p.mae = gross;
    }

    let reason = null;
    let realized = gross;

    if (gross >= p.tp) {
        reason = 'TP';
        realized = p.tp;
    }

    else if (gross <= p.sl) {
        reason = 'SL';
        realized = p.sl;
    }

    else if (
        b.t - p.entryT >=
        p.toMin * 60000
    ) {
        reason = 'TIMEOUT';
    }

    if (!reason) {
        return false;
    }

    const exitPx =
        reason === 'TIMEOUT'
            ? px
            : (
                p.entryPx +
                k *
                realized /
                p.qty
            );

    const feeExit =
        (
            p.feeBase === 'exposure'
                ? p.exposure
                : p.qty * exitPx
        ) *
        p.feeRate;

    const fees =
        p.feeEntry +
        feeExit;

    const net =
        realized -
        fees;

    const x = {
        t: b.t,
        px: exitPx,
        trigger: px,

        gross: realized,

        feeExit: feeExit,
        fees: fees,
        net: net,

        reason: reason,

        dur: Math.round(
            (b.t - p.entryT) /
            1000
        )
    };

    saveRow(
        p,
        x
    );

    const T = E.totals;

    T.ops++;

    if (reason === 'TP') {
        T.tp++;
    }

    else if (reason === 'SL') {
        T.sl++;
    }

    else {
        T.to++;
    }

    T.gross += realized;
    T.net += net;
    T.fees += fees;

    for (
        let i = E.trades.length - 1;
        i >= 0;
        i--
    ) {

        if (
            E.trades[i].entryT ===
            p.entryT
        ) {

            Object.assign(
                E.trades[i],
                {
                    exitT: b.t,
                    exitPx: exitPx,
                    reason: reason,
                    net: net
                }
            );

            break;
        }
    }

    E.lastClosed = {
        side: p.side,
        reason: reason,

        net: net,
        gross: realized,

        t: b.t,
        dur: x.dur
    };

    E.tradesRev++;

    E.pos = null;

    node.log(
        ENGINE + ': cierra ' +
        p.side +
        ' por ' +
        reason +
        ' (close ' +
        px +
        ') · bruto ' +
        realized.toFixed(2) +
        ' · neto ' +
        net.toFixed(2)
    );

    return true;
}


// ============================================================
// SEGUNDO GMX STALE (> 10 s sin cotización)
// ============================================================

function onGmxStale(T) {

    if (
        E.lastBucketT ||
        E.closes.length ||
        E.lastSign !== 0
    ) {
        E.gaps++;
    }

    resetSignal();

    // Rompe la continuidad: el próximo segundo válido arranca warmup.
    E.lastBucketT = 0;

    // Si N+1 es stale, no se fabrica entrada con una cotización futura.
    if (E.pend) {

        node.warn(
            ENGINE + ': entrada cancelada; GMX stale en N+1 (' +
            new Date(T).toISOString().slice(11, 19) +
            ')'
        );

        E.pend = null;

        E.ignored++;
    }

    E.dirty = true;
}


// ============================================================
// SEGUNDO GMX VÁLIDO
// ============================================================

// b = { t, source_ts, min, max, mid }
// t es el boundary exacto (múltiplo de 1000 ms) y source_ts <= t.
function onGmxSecond(b) {

    // Salvaguarda de continuidad.
    if (
        E.lastBucketT &&
        b.t - E.lastBucketT > 1000
    ) {

        resetSignal();

        E.gaps++;
    }

    E.lastBucketT = b.t;

    let closedNow = false;
    let enteredNow = false;


    // ========================================================
    // ENTRADA N+1
    // ========================================================

    if (E.pend) {

        let opened = false;

        if (b.t === E.pend.t + 1000) {

            opened =
                open(
                    E.pend,
                    b
                );
        }

        else {

            node.warn(
                ENGINE + ': entrada omitida; el segundo válido siguiente no es N+1'
            );

            E.ignored++;
        }

        E.pend = null;

        enteredNow =
            !!opened;
    }

    else if (
        E.pos &&
        b.t > E.pos.entryT
    ) {

        closedNow =
            manage(b);
    }


    // ========================================================
    // PRECIO DE SEÑAL = GMX MID DEL SEGUNDO
    // ========================================================

    const signalPx =
        b.mid;


    // ========================================================
    // SMA FAST / SLOW SOBRE GMX MID
    // ========================================================

    E.closes.push(
        signalPx
    );

    // Buffer suficiente para SMA slow y SMA1200 + lag 5 s.
    while (
        E.closes.length >
        CLOSES_MAX
    ) {
        E.closes.shift();
    }

    E.sF =
        avgLast(
            C.fast
        );

    E.sS =
        avgLast(
            C.slow
        );

    // Espectro 25..1200 con closes hasta b.t (información <= N).
    computeSpectrum();

    E.last = {
        t: b.t,
        px: signalPx
    };

    histPut(
        b.t,
        E.sF,
        E.sS
    );

    E.newPts.push(
        b.t,
        r(E.sF, 6),
        r(E.sS, 6)
    );


    // ========================================================
    // MOVIMIENTO ZEC: rango 30 min con datos hasta b.t
    // ========================================================

    movePut(
        b.t,
        signalPx
    );

    if (
        E.sF == null ||
        E.sS == null
    ) {
        return;
    }


    // ========================================================
    // CRUCE
    // ========================================================

    const d =
        E.sF -
        E.sS;

    const sg =
        d > 0
            ? 1
            : (
                d < 0
                    ? -1
                    : 0
            );

    if (sg === 0) {
        return;
    }

    const cross =
        (
            E.lastSign !== 0 &&
            sg !== E.lastSign
        )
            ? sg
            : 0;

    E.lastSign = sg;

    if (!cross) {
        return;
    }


    // ========================================================
    // FILTRO ANTI-SERRUCHO 32 SEGUNDOS
    // ========================================================

    const prevCrossT =
        E.lastCrossT ||
        0;

    const crossAllowed =
        !prevCrossT ||
        (
            b.t -
            prevCrossT >=
            CROSS_COOLDOWN_MS
        );

    // Todo cruce reinicia el reloj,
    // incluso uno descartado.
    E.lastCrossT =
        b.t;

    if (!crossAllowed) {

        E.ignored++;

        return;
    }


    // ========================================================
    // GATILLO (señal en N, entrada en N+1)
    // ========================================================

    if (
        !E.pos &&
        !E.pend &&
        !closedNow &&
        !enteredNow
    ) {

        // MOVIMIENTO OFF / WARMUP: el cruce se registra pero no abre.
        if (E.moveState !== 'ON') {

            if (E.moveState === 'WARMUP') {
                E.crossesBlockedWarmup++;
            }

            else {
                E.crossesBlockedMovement++;
            }

            E.dirty = true;

            return;
        }

        // CONFIRM50 en el segundo N: sin confirmación espectral no hay pend
        // (el cruce ya actualizó lastCrossT más arriba).
        if (!confirm50(cross)) {

            E.crossesBlockedSpectrum++;

            node.log(
                ENGINE + ': cruce ' +
                (cross > 0 ? 'LONG' : 'SHORT') +
                ' bloqueado por CONFIRM50 · espectro ' +
                E.spectrumState +
                ' (' +
                new Date(b.t).toISOString().slice(11, 19) +
                ')'
            );

            E.dirty = true;

            return;
        }

        E.pend = {
            side:
                cross > 0
                    ? 'LONG'
                    : 'SHORT',

            t: b.t,

            sF: E.sF,
            sS: E.sS,

            spec: E.spectrumState
        };
    }

    else {
        E.ignored++;
    }
}


// ============================================================
// RELOJ GMX: AS-OF BACKWARD SOBRE SEGUNDOS COMPLETADOS
// ============================================================

function pumpGmx() {

    const G =
        global.get(
            'md_gmx',
            'memory'
        );

    const z =
        G &&
        G.last &&
        G.last[C.symbol];

    if (!z) {
        return;
    }

    const s = Number(z.source_ts);
    const mn = Number(z.min);
    const mx = Number(z.max);

    if (
        !isFinite(s) ||
        !isFinite(mn) ||
        !isFinite(mx) ||
        mn <= 0 ||
        mx <= 0
    ) {
        return;
    }

    // Sólo cotizaciones nuevas (source_ts estrictamente mayor).
    if (s <= E.lastGmxSourceTs) {
        return;
    }

    const prev =
        E.lastGmxQuote;

    if (prev) {

        // Llegó una cotización con source_ts = s:
        // todos los boundaries T < s ya están completos y su
        // cotización as-of es 'prev' (source_ts <= T).
        let T =
            E.nextGmxBoundary;

        while (T < s) {

            if (
                T - prev.source_ts >
                GMX_MAX_AGE_MS
            ) {

                // Todos los T restantes < s también son stale.
                onGmxStale(T);

                T =
                    Math.ceil(
                        s /
                        1000
                    ) *
                    1000;

                break;
            }

            onGmxSecond({
                t: T,
                source_ts: prev.source_ts,
                min: prev.min,
                max: prev.max,
                mid: (prev.min + prev.max) / 2
            });

            E.dirty = true;

            T += 1000;
        }

        E.nextGmxBoundary = T;
    }

    else {

        // Primera cotización tras deploy/restore:
        // el primer boundary procesable es el primero >= s.
        E.nextGmxBoundary =
            Math.ceil(
                s /
                1000
            ) *
            1000;
    }

    E.lastGmxQuote = {
        source_ts: s,
        min: mn,
        max: mx
    };

    E.lastGmxSourceTs = s;
}


// ============================================================
// ESTADO DASHBOARD
// ============================================================

function state() {

    const p = E.pos;

    const o = {
        engine: ENGINE,

        phase: E.phase,

        t:
            E.last &&
            E.last.t,

        px:
            E.last &&
            E.last.px,

        sF:
            r(E.sF, 6),

        sS:
            r(E.sS, 6),

        warm:
            Math.min(
                E.closes.length,
                C.slow
            ),

        totals: {
            ops: E.totals.ops,
            tp: E.totals.tp,
            sl: E.totals.sl,
            to: E.totals.to,

            gross:
                r(
                    E.totals.gross,
                    2
                ),

            net:
                r(
                    E.totals.net,
                    2
                ),

            fees:
                r(
                    E.totals.fees,
                    2
                )
        },

        pend:
            E.pend
                ? {
                    side: E.pend.side,
                    t: E.pend.t
                }
                : null,

        lastClosed:
            E.lastClosed,

        // MOVIMIENTO ZEC
        movement_state:
            E.moveState,

        movement_on:
            E.moveState === 'ON',

        range30_bps:
            r(E.r30Bps, 2),

        range30_pct:
            E.r30Bps == null
                ? null
                : r(E.r30Bps / 100, 4),

        movement_threshold_bps:
            MOVE_THRESHOLD_BPS,

        range30_high:
            r(E.r30High, 6),

        range30_low:
            r(E.r30Low, 6),

        range30_warm_sec:
            moveWarmSec(),

        range30_window_sec:
            MOVE_WINDOW_MS / 1000,

        crosses_blocked_movement:
            E.crossesBlockedMovement,

        crosses_blocked_warmup:
            E.crossesBlockedWarmup,

        // ESPECTRO / CONFIRM50
        spectrum_state:
            E.spectrumState,

        spectrum_warm:
            E.spectrumWarm,

        spectrum_valid:
            specValidCount(),

        spectrum_periods:
            SPEC_PERIODS,

        spectrum:
            E.spectrum.map(function (x) {
                return {
                    n: x.n,
                    sma: r(x.sma, 6),
                    slope: r(x.slope, 4),
                    sign: x.sign
                };
            }),

        crosses_blocked_spectrum:
            E.crossesBlockedSpectrum,

        closes_len:
            E.closes.length,

        closes_max:
            CLOSES_MAX,

        db: {
            ok:
                E.dbOk,

            err:
                E.dbErr,

            errors:
                E.dbErrors,

            queue:
                E.q.length +
                (
                    E.inflight
                        ? 1
                        : 0
                ),

            saved:
                E.saved
        }
    };

    if (p) {

        const L =
            levels(p);

        o.pos = {
            side:
                p.side,

            entryT:
                p.entryT,

            entryPx:
                p.entryPx,

            lastPx:
                p.lastPx,

            pnl:
                r(
                    p.pnl,
                    2
                ),

            toMin:
                p.toMin,

            tp:
                p.tp,

            sl:
                p.sl,

            tpPx:
                L.tpPx,

            slPx:
                L.slPx
        };
    }

    return o;
}

function cfgOut() {
    return {
        fast: C.fast,
        slow: C.slow,

        margin: C.margin,
        lev: C.lev,

        exposure:
            C.margin *
            C.lev,

        tp: C.tp,
        sl: C.sl,

        timeoutMin:
            C.timeoutMin,

        feeRate:
            C.feeRate,

        engine:
            ENGINE,

        movementThresholdBps:
            MOVE_THRESHOLD_BPS,

        movementWindowMin:
            MOVE_WINDOW_MS / 60000,

        source:
            SRC,

        spectrumPeriods:
            SPEC_PERIODS,

        spectrumLagSec:
            SPEC_LAG,

        spectrumNeutralBps:
            SPEC_NEUTRAL_BPS,

        spectrumFilter:
            'CONFIRM50'
    };
}

function recentTrades() {

    const lim =
        now -
        C.histSec *
        1000;

    return E.trades.filter(
        function (t) {
            return (
                t.exitT ||
                now
            ) >= lim;
        }
    );
}

function mmss(sec) {
    return pad(Math.floor(sec / 60)) + ':' + pad(sec % 60);
}

// WARMUP → MOVEMENT OFF → MOVEMENT ON
function moveTag() {

    if (E.moveState === 'WARMUP') {
        return (
            'WARMUP ' +
            mmss(moveWarmSec()) +
            '/' +
            mmss(MOVE_WINDOW_MS / 1000)
        );
    }

    return (
        'MOVEMENT ' +
        E.moveState +
        ' ' +
        (E.r30Bps == null ? '-' : E.r30Bps.toFixed(1)) +
        '/' +
        MOVE_THRESHOLD_BPS +
        'bps'
    );
}

function statusText() {

    const p =
        E.pos;

    if (
        E.phase !==
        'OPERANDO'
    ) {

        return {
            fill: 'yellow',
            shape: 'ring',

            text:
                ENGINE +
                ' · restaurando estado (MariaDB)' +
                (
                    E.dbErr
                        ? ': ' +
                        E.dbErr.slice(
                            0,
                            40
                        )
                        : ''
                )
        };
    }

    const tail =
        ' · ops ' +
        E.totals.ops +
        ' · neto ' +
        E.totals.net.toFixed(2) +
        ' · bloq ' +
        E.crossesBlockedMovement +
        ' · bloqS ' +
        E.crossesBlockedSpectrum +
        ' · ' +
        (E.spectrumState || '-');

    if (p) {

        return {
            fill:
                p.side === 'LONG'
                    ? 'green'
                    : 'red',

            shape: 'dot',

            text:
                moveTag() +
                ' · ' +
                p.side +
                ' @ ' +
                p.entryPx +
                ' · PnL ' +
                p.pnl.toFixed(2) +
                tail
        };
    }

    return {
        fill:
            E.moveState === 'ON'
                ? 'blue'
                : (
                    E.moveState === 'OFF'
                        ? 'grey'
                        : 'yellow'
                ),

        shape:
            E.moveState === 'ON'
                ? 'dot'
                : 'ring',

        text:
            moveTag() +
            ' · ' +
            (
                E.sS == null
                    ? (
                        'calentando SMA ' +
                        E.closes.length +
                        '/' +
                        C.slow
                    )
                    : 'esperando cruce'
            ) +
            tail
    };
}


// ============================================================
// ERROR DB
// ============================================================

if (
    msg.topic === 'db_error' ||
    msg.error
) {

    if (
        E.inflight &&
        msg.cxEngine ===
        ENGINE &&
        msg.cxBatch ===
        E.inflight.id
    ) {

        fail(
            (
                msg.error &&
                msg.error.message
            ) ||
            'error DB'
        );
    }

    return null;
}


// ============================================================
// RESPUESTAS MYSQL
// ============================================================

if (msg.cxKind) {

    // Sólo respuestas de queries emitidas por ESTE motor.
    if (
        msg.cxEngine !==
        ENGINE ||
        !E.inflight ||
        msg.cxBatch !==
        E.inflight.id
    ) {
        return null;
    }

    const kind =
        E.inflight.kind;

    E.lastItem =
        E.inflight.item;

    E.inflight =
        null;

    E.backoff =
        0;

    E.dbOk =
        true;

    E.dbErr =
        '';


    // ========================================================
    // RESTORE
    // ========================================================

    if (
        kind ===
        'restore'
    ) {

        const sets =
            (
                Array.isArray(
                    msg.payload
                )
                    ? msg.payload
                    : []
            ).filter(
                Array.isArray
            );

        const op =
            (
                sets[0] ||
                []
            )[0];

        const tot =
            (
                sets[1] ||
                []
            )[0] ||
            {};

        const recent =
            sets[2] ||
            [];

        E.openRows =
            Number(
                tot.open_rows ||
                0
            );

        if (
            E.openRows >
            1
        ) {

            node.warn(
                ENGINE + ': hay ' +
                E.openRows +
                ' filas OPEN ' +
                SRC +
                ' en ' +
                C.table +
                '; se retoma la más reciente'
            );
        }


        if (op) {

            E.pos = {
                side:
                    op.side,

                signalT:
                    op.signal_ms == null
                        ? Number(
                            op.entry_ms
                        ) -
                        1000
                        : Number(
                            op.signal_ms
                        ),

                entryT:
                    Number(
                        op.entry_ms
                    ),

                entryPx:
                    num(
                        op.entry_price
                    ),

                qty:
                    num(
                        op.qty
                    ),

                margin:
                    num(
                        op.margin_usd
                    ),

                lev:
                    num(
                        op.leverage
                    ),

                exposure:
                    num(
                        op.exposure_usd
                    ),

                fast:
                    Number(
                        op.sma_fast
                    ),

                slow:
                    Number(
                        op.sma_slow
                    ),

                sFast:
                    num(
                        op.sma_fast_entry
                    ),

                sSlow:
                    num(
                        op.sma_slow_entry
                    ),

                tp:
                    num(
                        op.tp_usd
                    ),

                sl:
                    num(
                        op.sl_usd
                    ),

                toMin:
                    num(
                        op.timeout_min
                    ),

                feeRate:
                    num(
                        op.fee_rate
                    ),

                feeBase:
                    C.feeBase,

                feeEntry:
                    num(
                        op.fee_entry
                    ),

                pnl: 0,
                mfe: 0,
                mae: 0,

                lastPx:
                    num(
                        op.entry_price
                    ),

                lastT:
                    Number(
                        op.entry_ms
                    )
            };

            node.log(
                ENGINE + ': posición ' +
                E.pos.side +
                ' abierta restaurada (entrada ' +
                new Date(
                    E.pos.entryT
                ).toISOString() +
                ' @ ' +
                E.pos.entryPx +
                ')'
            );
        }


        E.totals = {
            ops:
                Number(
                    tot.ops ||
                    0
                ),

            tp:
                Number(
                    tot.tp ||
                    0
                ),

            sl:
                Number(
                    tot.sl ||
                    0
                ),

            to:
                Number(
                    tot.tmo ||
                    0
                ),

            gross:
                num(
                    tot.gross
                ) ||
                0,

            net:
                num(
                    tot.net
                ) ||
                0,

            fees:
                num(
                    tot.fees
                ) ||
                0
        };


        E.trades =
            recent.map(
                function (x) {

                    if (
                        x.exit_ms ==
                        null
                    ) {

                        return {
                            entryT:
                                Number(
                                    x.entry_ms
                                ),

                            entryPx:
                                num(
                                    x.entry_price
                                ),

                            side:
                                x.side
                        };
                    }

                    return {
                        entryT:
                            Number(
                                x.entry_ms
                            ),

                        entryPx:
                            num(
                                x.entry_price
                            ),

                        side:
                            x.side,

                        exitT:
                            Number(
                                x.exit_ms
                            ),

                        exitPx:
                            num(
                                x.exit_price
                            ),

                        reason:
                            x.exit_reason,

                        net:
                            num(
                                x.net_pnl
                            )
                    };
                }
            );

        E.tradesRev++;


        // Sin replay: reloj GMX y SMA arrancan desde cero.
        E.replayUntil = 0;

        E.lastGmxSourceTs = 0;
        E.lastGmxQuote = null;
        E.nextGmxBoundary = 0;

        E.lastBucketT = 0;

        resetSignal();

        // Rango 30 min y cooldown también arrancan desde cero:
        // WARMUP → MOVEMENT OFF / ON.
        resetMovement();

        E.lastCrossT = 0;

        E.pend = null;

        E.phase =
            'OPERANDO';

        node.log(
            ENGINE +
            ': operando (' +
            SRC +
            ' · ops ' +
            E.totals.ops +
            ') · WARMUP SMA ' +
            C.fast + '/' + C.slow +
            ' + rango ' +
            (MOVE_WINDOW_MS / 60000) +
            ' min → MOVEMENT OFF / ON (umbral ' +
            MOVE_THRESHOLD_BPS +
            ' bps) · filtro CONFIRM50 (espectro ' +
            SPEC_PERIODS.join('/') +
            ', buffer ' +
            CLOSES_MAX +
            ' s)'
        );

        E.dirty =
            true;

        node.status(
            statusText()
        );

        return null;
    }


    if (
        kind === 'close' &&
        msg.payload &&
        msg.payload.affectedRows === 0 &&
        E.lastItem &&
        E.lastItem.fallback
    ) {

        E.q.unshift(
            E.lastItem.fallback
        );
    }

    else {
        E.saved++;
    }

    return null;
}


// ============================================================
// SNAPSHOT DASHBOARD
// ============================================================

if (
    msg.topic ===
    'cx_snapshot_req'
) {

    const H =
        E.hist;

    const since =
        Math.floor(
            (
                Number(
                    msg.since
                ) ||
                0
            ) /
            1000
        );

    const nowSec =
        Math.floor(
            now /
            1000
        );

    const arr =
        [];

    for (
        let s =
            Math.max(
                since + 1,
                nowSec -
                H.cap +
                1
            );

        s <= nowSec;

        s++
    ) {

        const k =
            s %
            H.cap;

        if (
            H.idx[k] ===
            s
        ) {

            arr.push(
                s * 1000,
                r(H.a[k], 6),
                r(H.b[k], 6)
            );
        }
    }

    out[1] = {
        socketid:
            msg.socketid,

        payload: {
            cxSnap: {
                cfg:
                    cfgOut(),

                sma:
                    arr,

                trades:
                    recentTrades(),

                state:
                    state()
            }
        }
    };

    return out;
}


// ============================================================
// SÓLO TICK
// ============================================================

if (
    msg.topic !==
    'tick'
) {
    return null;
}


// ============================================================
// DB TIMEOUT
// ============================================================

if (
    E.inflight &&
    now -
    E.inflight.sentAt >
    30000
) {

    fail(
        'timeout: MariaDB no respondió en 30 s'
    );
}


// ============================================================
// RESTAURAR ESTADO
// ============================================================

if (
    E.phase ===
    'RESTAURANDO' &&

    !E.inflight &&

    !E.q.some(
        function (i) {
            return (
                i.kind ===
                'restore'
            );
        }
    ) &&

    now >=
    E.nextTry
) {

    const T =
        C.table;

    const ms =
        function (c) {

            return (
                'TIMESTAMPDIFF(MICROSECOND, ' +
                '\'1970-01-01 00:00:00\', ' +
                c +
                ') DIV 1000'
            );
        };

    E.q.unshift({
        kind:
            'restore',

        sql:
            'SELECT ' +
            ms('entry_ts') +
            ' AS entry_ms, ' +

            'CASE WHEN signal_ts IS NULL THEN NULL ELSE ' +
            ms('signal_ts') +
            ' END AS signal_ms, ' +

            'side, entry_price, qty, margin_usd, leverage, exposure_usd, ' +
            'sma_fast, sma_slow, sma_fast_entry, sma_slow_entry, ' +
            'tp_usd, sl_usd, timeout_min, fee_rate, fee_entry ' +

            'FROM ' +
            T +
            ' WHERE symbol = ? ' +
            'AND source = ? ' +
            'AND status = \'OPEN\' ' +
            'ORDER BY entry_ts DESC LIMIT 1; ' +


            'SELECT ' +
            'SUM(status = \'CLOSED\') ops, ' +
            'SUM(exit_reason = \'TP\') tp, ' +
            'SUM(exit_reason = \'SL\') sl, ' +
            'SUM(exit_reason = \'TIMEOUT\') tmo, ' +

            'SUM(CASE WHEN status = \'CLOSED\' THEN gross_pnl END) gross, ' +
            'SUM(CASE WHEN status = \'CLOSED\' THEN net_pnl END) net, ' +
            'SUM(CASE WHEN status = \'CLOSED\' THEN fees END) fees, ' +
            'SUM(status = \'OPEN\') open_rows ' +

            'FROM ' +
            T +
            ' WHERE symbol = ? ' +
            'AND source = ?; ' +


            'SELECT ' +
            ms('entry_ts') +
            ' AS entry_ms, ' +

            'CASE WHEN exit_ts IS NULL THEN NULL ELSE ' +
            ms('exit_ts') +
            ' END AS exit_ms, ' +

            'side, entry_price, exit_price, exit_reason, net_pnl ' +

            'FROM ' +
            T +
            ' WHERE symbol = ? ' +
            'AND source = ? ' +
            'AND (exit_ts IS NULL OR exit_ts >= ?) ' +
            'ORDER BY entry_ts',

        params: [
            C.symbol,
            SRC,

            C.symbol,
            SRC,

            C.symbol,
            SRC,
            dt(
                now -
                C.histSec *
                1000
            )
        ]
    });
}


// ============================================================
// ENVIAR QUERY MYSQL
// ============================================================

if (
    !E.inflight &&
    E.q.length &&
    now >=
    E.nextTry
) {

    const it =
        E.q.shift();

    E.inflight = {
        id:
            ++E.seq,

        kind:
            it.kind,

        item:
            it,

        sentAt:
            now
    };

    out[0] = {
        topic:
            it.sql,

        payload:
            it.params,

        cxKind:
            it.kind,

        cxBatch:
            E.inflight.id,

        cxEngine:
            ENGINE,

        cxRow:
            it.row
    };
}


// ============================================================
// PROCESAR SEGUNDOS GMX
// ============================================================

if (
    E.phase ===
    'OPERANDO'
) {
    pumpGmx();
}


// ============================================================
// EMITIR DASHBOARD
// ============================================================

if (
    E.dirty &&
    now -
    E.lastEmit >=
    250
) {

    E.dirty =
        false;

    E.lastEmit =
        now;

    const p = {
        cx: {
            state:
                state(),

            pts:
                E.newPts
        }
    };

    E.newPts =
        [];

    if (
        E.tradesRev !==
        E.sentRev
    ) {

        p.cx.trades =
            recentTrades();

        p.cx.cfg =
            cfgOut();

        E.sentRev =
            E.tradesRev;
    }

    out[1] = {
        payload:
            p
    };

    node.status(
        statusText()
    );
}

return (
    out[0] ||
    out[1]
)
    ? out
    : null;
