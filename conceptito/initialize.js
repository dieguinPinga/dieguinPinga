// ===== CONCEPTITO_MOVEMENT_V1: configuración y estado =====
const C = {
    symbol: 'ZEC',

    // Señal: GMX MID en buckets de 1 segundo
    fast: 35,
    slow: 70,

    margin: 1000,
    lev: 3,                    // exposición = 3000 USD

    tp: 25.00,                 // PnL BRUTO
    sl: -75.00,                // PnL BRUTO
    timeoutMin: 100,

    maxPos: 1,

    feeRate: 0.0005,           // 0,05 % por lado
    feeBase: 'exposure',       // $1.50 entrada + $1.50 salida

    table: 'conceptito_trades',
    histSec: 14400
};

const E = {
    cfg: C,
    phase: 'RESTAURANDO',

    lastT: 0,
    replayUntil: 0,
    lastBucketT: 0,
    gaps: 0,

    closes: [],
    sF: null,
    sS: null,
    lastSign: 0,
    lastCrossT: 0,

    ignored: 0,
    last: null,
    pend: null,

    pos: null,

    totals: {
        ops: 0,
        tp: 0,
        sl: 0,
        to: 0,
        gross: 0,
        net: 0,
        fees: 0
    },

    trades: [],
    tradesRev: 0,
    lastClosed: null,

    hist: {
        cap: C.histSec,
        idx: new Float64Array(C.histSec).fill(-1),
        a: new Float64Array(C.histSec),
        b: new Float64Array(C.histSec)
    },

    newPts: [],
    dirty: true,
    lastEmit: 0,

    q: [],
    inflight: null,
    seq: 0,
    backoff: 0,
    nextTry: 0,
    saved: 0,
    dbErrors: 0,
    dbErr: '',
    lastLogAt: 0,
    dbOk: false,
    openRows: 0,

    // MOVIMIENTO ZEC 30 min (rango causal, umbral 66.04 bps)
    r30: [],
    r30Since: 0,
    r30High: null,
    r30Low: null,
    r30Bps: null,
    moveState: 'WARMUP',
    crossesBlockedMovement: 0,
    crossesBlockedWarmup: 0
};

context.set('E', E, 'memory');

node.status({
    fill: 'grey',
    shape: 'ring',
    text: 'CONCEPTITO_MOVEMENT_V1 · restaurando estado desde MariaDB…'
});
