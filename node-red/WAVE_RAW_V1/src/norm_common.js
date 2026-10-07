// =====================================================================
// WAVE_RAW_V1 · NORMALIZER COMMON (compartido por los 4 normalizers)
// ---------------------------------------------------------------------
// Entrada : frames crudos del ADAPTER { kind:'frame', conn, gen, t_recv, payload }
//           y eventos de conexión { kind:'conn', event:'open'|'close', ... }
// Salida 1: { kind:'events', exchange, conn, ekind:'trade'|'book', t_recv, frame_seq,
//             payload:[evento normalizado, ...] }   (1 mensaje por frame con datos)
//           { kind:'conn', ... } reenviado tal cual
//           { kind:'nstats', ... } contadores acumulados, 1 vez por segundo
// Salida 2: { topic:'resync', conn, reason } -> vuelve al ADAPTER (reconexión rápida)
// El libro se mantiene en memoria (escalera ordenada), pero sólo se emite
// best bid/ask + sumas top5/top10: nunca objetos gigantes.
// =====================================================================
const SYMBOL = 'BTC';

function num(v) {
    if (v === null || v === undefined || v === '') return null;
    const n = typeof v === 'number' ? v : Number(v);
    return isFinite(n) ? n : null;
}

// ISO-8601 con micro/nanosegundos -> ms epoch (con fracción sub-ms)
function parseIsoMs(s) {
    if (typeof s !== 'string') return null;
    const m = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?Z$/.exec(s);
    if (!m) {
        const v = Date.parse(s);
        return isFinite(v) ? v : null;
    }
    const base = Date.parse(m[1] + 'Z');
    if (!isFinite(base)) return null;
    return m[2] ? base + Number('0.' + m[2]) * 1000 : base;
}

const CRC_TABLE = (function () {
    const t = new Int32Array(256);
    for (let n = 0; n < 256; n++) {
        let c = n;
        for (let k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
        t[n] = c;
    }
    return t;
})();

// CRC32 (IEEE) de un string ASCII -> entero sin signo
function crc32(str) {
    let c = -1;
    for (let i = 0; i < str.length; i++) c = CRC_TABLE[(c ^ str.charCodeAt(i)) & 0xFF] ^ (c >>> 8);
    return (c ^ -1) >>> 0;
}

// ---------------------------------------------------------------------
// Ladder: un lado del libro, ordenado de PEOR a MEJOR precio.
// El mejor nivel queda al FINAL del array: los cambios cerca del top
// (la inmensa mayoría) cuestan O(distancia al top), no O(n).
// px = precio numérico, qt = cantidad, rp/rq = strings originales (checksums)
// ---------------------------------------------------------------------
function Ladder(isBid) {
    this.dir = isBid ? 1 : -1; // bids ascendente, asks descendente
    this.px = []; this.qt = []; this.rp = []; this.rq = [];
}
Ladder.prototype.clear = function () {
    this.px.length = 0; this.qt.length = 0; this.rp.length = 0; this.rq.length = 0;
};
Ladder.prototype.size = function () { return this.px.length; };
Ladder.prototype.find = function (p) {
    const a = this.px, d = this.dir;
    let lo = 0, hi = a.length - 1;
    while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        const v = (a[mid] - p) * d;
        if (v === 0) return mid;
        if (v < 0) lo = mid + 1; else hi = mid - 1;
    }
    return ~lo;
};
Ladder.prototype.set = function (p, q, rp, rq) {
    const i = this.find(p);
    if (i >= 0) {
        if (q > 0) { this.qt[i] = q; this.rp[i] = rp; this.rq[i] = rq; }
        else { this.px.splice(i, 1); this.qt.splice(i, 1); this.rp.splice(i, 1); this.rq.splice(i, 1); }
    } else if (q > 0) {
        const at = ~i;
        if (at === this.px.length) { this.px.push(p); this.qt.push(q); this.rp.push(rp); this.rq.push(rq); }
        else { this.px.splice(at, 0, p); this.qt.splice(at, 0, q); this.rp.splice(at, 0, rp); this.rq.splice(at, 0, rq); }
    }
};
// levels: [[p, q, rawP, rawQ], ...] en cualquier orden
Ladder.prototype.load = function (levels) {
    const d = this.dir;
    const lv = levels.filter(function (l) { return l[1] > 0 && isFinite(l[0]); });
    lv.sort(function (a, b) { return (a[0] - b[0]) * d; });
    this.clear();
    for (const l of lv) { this.px.push(l[0]); this.qt.push(l[1]); this.rp.push(l[2]); this.rq.push(l[3]); }
};
Ladder.prototype.truncate = function (n) {
    const x = this.px.length - n;
    if (x > 0) { this.px.splice(0, x); this.qt.splice(0, x); this.rp.splice(0, x); this.rq.splice(0, x); }
};
Ladder.prototype.idx = function (k) { return this.px.length - 1 - k; }; // k=0 => mejor nivel
Ladder.prototype.bestPx = function () { return this.px.length ? this.px[this.px.length - 1] : null; };
Ladder.prototype.bestQt = function () { return this.qt.length ? this.qt[this.qt.length - 1] : null; };
Ladder.prototype.sumTop = function (k) {
    const n = this.qt.length;
    if (n < k) return null; // no hay k niveles: no inventar
    let s = 0;
    for (let i = n - 1; i >= n - k; i--) s += this.qt[i];
    return s;
};

function Book() {
    this.bids = new Ladder(true);
    this.asks = new Ladder(false);
    this.ready = false;
}
Book.prototype.clear = function () { this.bids.clear(); this.asks.clear(); this.ready = false; };
Book.prototype.crossed = function () {
    const b = this.bids.bestPx(), a = this.asks.bestPx();
    return b !== null && a !== null && b >= a;
};

// ---------------------------------------------------------------------
// Estado del normalizer y contadores
// ---------------------------------------------------------------------
const N = {
    frameSeq: 0,
    conns: {},
    // BBO actual del exchange (para emitir y para el chequeo de lado agresor)
    bbo: { bid: null, bq: null, ask: null, aq: null },
    // validación empírica de la semántica BUY/SELL: precio del trade vs BBO previo
    side: { agree: 0, disagree: 0, unknown: 0 },
    warnAt: {}
};

for (const id of CONN_IDS) {
    N.conns[id] = {
        id: id, gen: -1, frames: 0, data: 0, control: 0, invalid: 0, dropped: 0,
        gaps: 0, resyncs: 0, csum_ok: 0, csum_fail: 0, crossed: 0, opens: 0, closes: 0,
        lastSeq: null, lastSeqCh: {}, awaitingResync: false, lastResyncReq: 0, crossRun: 0
    };
}

function warnRL(key, text) {
    const now = Date.now();
    if (N.warnAt[key] && now - N.warnAt[key] < 30000) return;
    N.warnAt[key] = now;
    node.warn('[WAVE ' + EXCHANGE + '] ' + text);
}

function resetConn(c, gen) {
    c.gen = gen;
    c.lastSeq = null;
    c.lastSeqCh = {};
    c.awaitingResync = false;
    c.crossRun = 0;
    onConnReset(c);
}

function requestResync(c, reason) {
    c.awaitingResync = true; // descartar deltas hasta la próxima conexión/snapshot
    const now = Date.now();
    if (now - c.lastResyncReq < 3000) return;
    c.lastResyncReq = now;
    c.resyncs++;
    warnRL('resync-' + c.id, 'resync ' + c.id + ': ' + reason);
    node.send([null, { topic: 'resync', exchange: EXCHANGE, conn: c.id, reason: reason }], false);
}

function emitEvents(c, t, ekind, events) {
    c.data++;
    N.frameSeq++;
    node.send([{ topic: 'wave/events', kind: 'events', exchange: EXCHANGE, conn: c.id, ekind: ekind, t_recv: t, frame_seq: N.frameSeq, payload: events }, null], false);
}

function makeTrade(price, qty, side, exTs, t, extra) {
    const ev = {
        kind: 'trade', exchange: EXCHANGE, symbol: SYMBOL,
        price: price, quantity: qty, usd: price * qty, side: side,
        exchange_timestamp: exTs, local_receive_timestamp: t
    };
    if (extra) Object.assign(ev, extra);
    // chequeo empírico del lado agresor contra el BBO conocido ANTES del trade
    const b = N.bbo;
    if (side && b.bid !== null && b.ask !== null) {
        const inferred = price >= b.ask ? 'BUY' : (price <= b.bid ? 'SELL' : null);
        if (inferred === null) N.side.unknown++;
        else if (inferred === side) N.side.agree++;
        else N.side.disagree++;
    }
    return ev;
}

// Construye el evento book a partir del BBO actual + sumas de profundidad
function makeBook(channel, depth, exTs, t) {
    const b = N.bbo;
    return {
        kind: 'book', exchange: EXCHANGE, symbol: SYMBOL, channel: channel,
        best_bid: b.bid, best_bid_qty: b.bq, best_ask: b.ask, best_ask_qty: b.aq,
        bid_qty5: depth ? depth.b5 : null, ask_qty5: depth ? depth.a5 : null,
        bid_qty10: depth ? depth.b10 : null, ask_qty10: depth ? depth.a10 : null,
        exchange_timestamp: exTs, local_receive_timestamp: t
    };
}

function depthFromBook(book) {
    return {
        b5: book.bids.sumTop(5), a5: book.asks.sumTop(5),
        b10: book.bids.sumTop(10), a10: book.asks.sumTop(10)
    };
}

function bboFromBook(book) {
    N.bbo.bid = book.bids.bestPx(); N.bbo.bq = book.bids.bestQt();
    N.bbo.ask = book.asks.bestPx(); N.bbo.aq = book.asks.bestQt();
}

// libro cruzado de forma persistente => estado corrupto => resync
function checkCross(c, book) {
    if (book.crossed()) {
        c.crossed++;
        if (++c.crossRun >= 3) requestResync(c, 'crossed book');
        return true;
    }
    c.crossRun = 0;
    return false;
}

// detección de huecos de secuencia: válida tanto si la secuencia es por conexión como por canal
function seqCheck(c, ch, seq) {
    if (typeof seq !== 'number') return true;
    const lc = c.lastSeq, lch = c.lastSeqCh[ch];
    c.lastSeq = seq;
    c.lastSeqCh[ch] = seq;
    if (lc === null) return true;
    if (seq === lc + 1) return true;
    if (lch !== undefined && seq === lch + 1) return true;
    if (seq <= lc) return true; // duplicado/desordenado: se ignora
    c.gaps++;
    return false;
}

function onInput(msg) {
    if (!msg) return;
    if (msg.kind === 'conn') {
        const c = N.conns[msg.conn];
        if (c) {
            if (msg.event === 'open') { c.opens++; resetConn(c, msg.gen); }
            else if (msg.event === 'close') { c.closes++; resetConn(c, -1); }
        }
        node.send([msg, null], false);
        return;
    }
    if (msg.kind !== 'frame') return;
    const c = N.conns[msg.conn];
    if (!c) return;
    if (msg.gen !== c.gen) {
        if (c.gen === -1 || msg.gen > c.gen) resetConn(c, msg.gen);
        else { c.dropped++; return; } // frame de un socket viejo
    }
    c.frames++;
    let txt = msg.payload;
    if (typeof txt !== 'string') {
        try { txt = txt.toString('utf8'); } catch (e) { c.invalid++; return; }
    }
    handleFrame(c, txt, msg.t_recv);
}

function statsSnapshot() {
    const conns = {};
    for (const id of CONN_IDS) {
        const c = N.conns[id];
        conns[id] = {
            frames: c.frames, data: c.data, control: c.control, invalid: c.invalid, dropped: c.dropped,
            gaps: c.gaps, resyncs: c.resyncs, csum_ok: c.csum_ok, csum_fail: c.csum_fail,
            crossed: c.crossed, opens: c.opens, closes: c.closes, awaiting_resync: c.awaitingResync
        };
    }
    return { topic: 'wave/nstats', kind: 'nstats', exchange: EXCHANGE, t: Date.now(), conns: conns,
        side_check: { agree: N.side.agree, disagree: N.side.disagree, unknown: N.side.unknown },
        extra: typeof extraStats === 'function' ? extraStats() : null };
}

N.timer = setInterval(function () {
    node.send([statsSnapshot(), null], false);
    let fr = 0, inv = 0;
    for (const id of CONN_IDS) { fr += N.conns[id].frames; inv += N.conns[id].invalid; }
    const fps = fr - (N.lastFr || 0);
    N.lastFr = fr;
    node.status({ fill: inv ? 'yellow' : 'green', shape: 'ring', text: fps + ' fr/s · inv ' + inv });
}, 1000);

globalThis.__WAVE_NORM__ = { onInput: onInput, stop: function () { clearInterval(N.timer); } };
