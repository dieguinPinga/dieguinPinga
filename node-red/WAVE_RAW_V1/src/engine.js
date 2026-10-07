// =====================================================================
// WAVE_RAW_V1 · WAVE RAW ENGINE
// ---------------------------------------------------------------------
// PROCESAMIENTO EVENT-DRIVEN: cada mensaje que llega actualiza en el acto
// los ring buffers / ventanas móviles (100/250/500/1000 ms). Ningún cálculo
// de mercado espera a un timer.
// SALIDA HUMANA DESACOPLADA: un setInterval de 1 s SÓLO lee el estado ya
// calculado y emite:
//   salida 1 -> PULSE (resumen compacto por exchange)
//   salida 2 -> TELEMETRY (detalle: todas las ventanas, percentiles, contadores)
// Todo en memoria. Sin disco, sin DB, sin context store, sin señales LONG/SHORT.
// Base temporal de las ventanas: local_receive_timestamp (reloj local único).
// =====================================================================
const EXCHANGES = ['binance', 'coinbase', 'kraken', 'okx'];
const WINDOWS = [100, 250, 500, 1000];
const KINDS = ['trade', 'book'];
const RES_CAP = 2048;          // muestras máx. por estadística y por segundo (reservoir)
const EMIT_EVERY_MS = 1000;
const LOOP_PROBE_MS = 50;

function r(x, d) {
    if (x === null || x === undefined || !isFinite(x)) return null;
    const k = Math.pow(10, d === undefined ? 2 : d);
    return Math.round(x * k) / k;
}

// ---------- estadística por segundo: exacta en n/mean/min/max, percentiles por reservoir ----------
function Stat() { this.reset(); }
Stat.prototype.reset = function () { this.n = 0; this.sum = 0; this.min = Infinity; this.max = -Infinity; this.res = []; };
Stat.prototype.add = function (v) {
    if (!isFinite(v)) return;
    this.n++; this.sum += v;
    if (v < this.min) this.min = v;
    if (v > this.max) this.max = v;
    if (this.res.length < RES_CAP) this.res.push(v);
    else { const j = Math.floor(Math.random() * this.n); if (j < RES_CAP) this.res[j] = v; }
};
Stat.prototype.summary = function (d) {
    if (!this.n) return null;
    const a = this.res.slice().sort(function (x, y) { return x - y; });
    const q = function (p) { return a[Math.min(a.length - 1, Math.round(p * (a.length - 1)))]; };
    return { n: this.n, mean: r(this.sum / this.n, d), p50: r(q(0.5), d), p95: r(q(0.95), d), min: r(this.min, d), max: r(this.max, d) };
};

// ---------- deque O(1) amortizado sobre array con cabeza móvil ----------
function Deque() { this.a = []; this.h = 0; }
Deque.prototype.len = function () { return this.a.length - this.h; };
Deque.prototype.push = function (x) { this.a.push(x); };
Deque.prototype.front = function () { return this.a[this.h]; };
Deque.prototype.back = function () { return this.a[this.a.length - 1]; };
Deque.prototype.shift = function () { const x = this.a[this.h]; this.a[this.h++] = undefined; return x; };
Deque.prototype.pop = function () { return this.a.pop(); };
Deque.prototype.compact = function () {
    if (this.h > 1024 && this.h * 2 > this.a.length) { this.a = this.a.slice(this.h); this.h = 0; }
};

// ---------- ventana móvil de trades ----------
// sumas incrementales + deques monótonos para max/min intraventana (O(1) amortizado)
function TradeWin(ms) {
    this.ms = ms; this.q = new Deque(); this.mx = new Deque(); this.mn = new Deque();
    this.n = 0; this.frames = 0; this.usd = 0; this.buy = 0; this.sell = 0; this.ref = null;
}
TradeWin.prototype.push = function (rec) {
    this.q.push(rec);
    this.n++; this.usd += rec.usd;
    if (rec.s > 0) this.buy += rec.usd; else if (rec.s < 0) this.sell += rec.usd;
    if (rec.f) this.frames++;
    if (rec.p !== null) {
        while (this.mx.len() && this.mx.back().p <= rec.p) this.mx.pop();
        this.mx.push(rec);
        while (this.mn.len() && this.mn.back().p >= rec.p) this.mn.pop();
        this.mn.push(rec);
    }
};
TradeWin.prototype.evict = function (now) {
    const cut = now - this.ms, q = this.q;
    while (q.len() && q.front().t <= cut) {
        const x = q.shift();
        this.n--; this.usd -= x.usd;
        if (x.s > 0) this.buy -= x.usd; else if (x.s < 0) this.sell -= x.usd;
        if (x.f) this.frames--;
        if (x.p !== null) this.ref = x.p; // precio vigente al inicio de la ventana
        if (this.mx.len() && this.mx.front() === x) this.mx.shift();
        if (this.mn.len() && this.mn.front() === x) this.mn.shift();
    }
    if (this.n === 0) { this.usd = 0; this.buy = 0; this.sell = 0; this.frames = 0; } // sin deriva de coma flotante
    q.compact(); this.mx.compact(); this.mn.compact();
};
TradeWin.prototype.metrics = function (last, withPrice) {
    const s = this.ms / 1000;
    const o = {
        msgs: this.frames, trades: this.n, tps: r(this.n / s, 1),
        usd: r(this.usd, 0), buy_usd: r(this.buy, 0), sell_usd: r(this.sell, 0), delta_usd: r(this.buy - this.sell, 0)
    };
    if (!withPrice) return o;
    const ref = this.ref !== null ? this.ref : (this.q.len() ? this.q.front().p : null);
    if (ref === null || last === null || !(ref > 0)) {
        o.move_bps = null; o.max_up_bps = null; o.max_dn_bps = null; o.range_bps = null;
        return o;
    }
    const hi = Math.max(ref, this.mx.len() ? this.mx.front().p : ref);
    const lo = Math.min(ref, this.mn.len() ? this.mn.front().p : ref);
    o.move_bps = r((last - ref) / ref * 1e4, 2);
    o.max_up_bps = r((hi - ref) / ref * 1e4, 2);
    o.max_dn_bps = r((lo - ref) / ref * 1e4, 2);
    o.range_bps = r((hi - lo) / ref * 1e4, 2);
    return o;
};

function TradeStream() { this.wins = WINDOWS.map(function (ms) { return new TradeWin(ms); }); this.last = null; this.total = 0; this.usdTotal = 0; }
TradeStream.prototype.push = function (rec) {
    for (const w of this.wins) { w.push(rec); w.evict(rec.t); }
    if (rec.p !== null) this.last = rec.p;
    this.total++; this.usdTotal += rec.usd;
};
TradeStream.prototype.evict = function (now) { for (const w of this.wins) w.evict(now); };

// ---------- estado del libro por exchange + velocidad del imbalance ----------
function BookWin(ms) { this.ms = ms; this.q = new Deque(); this.n = 0; this.ref1 = null; this.ref10 = null; }
BookWin.prototype.evict = function (now) {
    const cut = now - this.ms;
    while (this.q.len() && this.q.front().t <= cut) {
        const x = this.q.shift();
        this.n--; this.ref1 = x.i1; this.ref10 = x.i10;
    }
    this.q.compact();
};

function imb(b, a) { return (b !== null && a !== null && b + a > 0) ? (b - a) / (b + a) : null; }

function BookState() {
    this.bid = null; this.bq = null; this.ask = null; this.aq = null;
    this.b5 = null; this.a5 = null; this.b10 = null; this.a10 = null;
    this.i1 = null; this.i5 = null; this.i10 = null; this.total = 0; this.channels = {};
    this.wins = WINDOWS.map(function (ms) { return new BookWin(ms); });
}
BookState.prototype.push = function (ev, t) {
    if (ev.best_bid !== null && ev.best_ask !== null) {
        this.bid = ev.best_bid; this.bq = ev.best_bid_qty; this.ask = ev.best_ask; this.aq = ev.best_ask_qty;
    }
    if (ev.bid_qty5 !== null && ev.ask_qty5 !== null) { this.b5 = ev.bid_qty5; this.a5 = ev.ask_qty5; }
    if (ev.bid_qty10 !== null && ev.ask_qty10 !== null) { this.b10 = ev.bid_qty10; this.a10 = ev.ask_qty10; }
    this.i1 = imb(this.bq, this.aq);
    this.i5 = imb(this.b5, this.a5);
    this.i10 = imb(this.b10, this.a10);
    this.total++;
    this.channels[ev.channel] = (this.channels[ev.channel] || 0) + 1;
    const smp = { t: t, i1: this.i1, i10: this.i10 };
    for (const w of this.wins) { w.q.push(smp); w.n++; w.evict(t); }
};
BookState.prototype.evict = function (now) { for (const w of this.wins) w.evict(now); };
BookState.prototype.clear = function () {
    this.bid = this.bq = this.ask = this.aq = null;
    this.b5 = this.a5 = this.b10 = this.a10 = null;
    this.i1 = this.i5 = this.i10 = null;
};
BookState.prototype.spreadBps = function () {
    if (this.bid === null || this.ask === null) return null;
    const mid = (this.bid + this.ask) / 2;
    return mid > 0 ? (this.ask - this.bid) / mid * 1e4 : null;
};
BookState.prototype.winMetrics = function (w) {
    const s = w.ms / 1000;
    const ref1 = w.ref1 !== null ? w.ref1 : (w.q.len() ? w.q.front().i1 : null);
    const ref10 = w.ref10 !== null ? w.ref10 : (w.q.len() ? w.q.front().i10 : null);
    return {
        updates: w.n, ups: r(w.n / s, 1),
        imb_vel_per_s: (ref1 !== null && this.i1 !== null) ? r((this.i1 - ref1) / s, 3) : null,
        imb10_vel_per_s: (ref10 !== null && this.i10 !== null) ? r((this.i10 - ref10) / s, 3) : null
    };
};

// ---------- telemetría de recepción por exchange y tipo ----------
function Tel() {
    this.events = 0; this.frames = 0; this.lastFrameT = 0; this.lastEventT = 0;
    this.sec = { events: 0, frames: 0, ia: new Stat(), lat: new Stat(), pipe: new Stat() };
}
Tel.prototype.resetSec = function () {
    this.sec.events = 0; this.sec.frames = 0;
    this.sec.ia.reset(); this.sec.lat.reset(); this.sec.pipe.reset();
};

function newState() {
    const st = { t0: Date.now(), ex: {}, all: new TradeStream(), global: { msgs: 0, events: 0, pipe: new Stat(), loop: new Stat(), unknown: 0 } };
    for (const e of EXCHANGES) {
        st.ex[e] = {
            trades: new TradeStream(), book: new BookState(),
            tel: { trade: new Tel(), book: new Tel() },
            conns: {}, nstats: null, prevFrames: {}, prevInvalid: {}
        };
    }
    return st;
}

let ST = newState();

// =====================================================================
// HOT PATH: se ejecuta por CADA mensaje (event-driven)
// =====================================================================
function onEvents(msg) {
    const X = ST.ex[msg.exchange];
    if (!X) { ST.global.unknown++; return; }
    const now = Date.now();
    const t = msg.t_recv;
    const tel = X.tel[msg.ekind];
    const pipe = now - t; // cola Node-RED: recepción en socket -> procesamiento aquí
    ST.global.msgs++;
    ST.global.pipe.add(pipe);
    if (tel) {
        tel.frames++; tel.sec.frames++;
        if (tel.lastFrameT) tel.sec.ia.add(t - tel.lastFrameT);
        tel.lastFrameT = t;
        tel.sec.pipe.add(pipe);
    }
    const evs = msg.payload;
    let first = true;
    for (let i = 0; i < evs.length; i++) {
        const ev = evs[i];
        ST.global.events++;
        if (tel) {
            tel.events++; tel.sec.events++; tel.lastEventT = t;
            if (typeof ev.exchange_timestamp === 'number') tel.sec.lat.add(ev.local_receive_timestamp - ev.exchange_timestamp);
        }
        if (ev.kind === 'trade') {
            const rec = { t: t, p: ev.price, usd: ev.usd, s: ev.side === 'BUY' ? 1 : (ev.side === 'SELL' ? -1 : 0), f: first };
            first = false;
            X.trades.push(rec);
            ST.all.push({ t: t, p: null, usd: rec.usd, s: rec.s, f: rec.f }); // consolidado: flujos (precios no mezclables USD/USDT)
        } else if (ev.kind === 'book') {
            X.book.push(ev, t);
        }
    }
}

function onConn(msg) {
    const X = ST.ex[msg.exchange];
    if (!X) return;
    X.conns[msg.conn] = { status: msg.event, t: msg.t, reconnects: msg.reconnects, disconnects: msg.disconnects,
        stale_kills: msg.stale_kills, last_error: msg.last_error, last_close_code: msg.last_close_code };
    if (msg.event === 'close' && msg.conn === 'book') X.book.clear(); // libro desconocido hasta el próximo snapshot
}

function onNstats(msg) {
    const X = ST.ex[msg.exchange];
    if (X) X.nstats = msg;
}

// =====================================================================
// SALIDA 1 Hz: sólo lectura del estado + estadísticas del último segundo
// =====================================================================
function median(a) {
    const v = a.filter(function (x) { return x !== null && isFinite(x); }).sort(function (x, y) { return x - y; });
    if (!v.length) return null;
    const m = v.length >> 1;
    return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
}

function winKey(ms) { return ms + 'ms'; }

function emit() {
    const now = Date.now();
    const dt = Math.max(0.001, (now - (ST.lastEmit || ST.t0)) / 1000);
    ST.lastEmit = now;
    const pulse = { ts: now, iso: new Date(now).toISOString(), uptime_s: Math.round((now - ST.t0) / 1000) };
    const detail = { ts: now, windows_ms: WINDOWS, exchanges: {} };
    const moves = {};
    for (const ms of WINDOWS) moves[ms] = [];
    const rank = { trade_eps: [], book_eps: [], trade_ia_p50: [], book_ia_p50: [], trade_lat_p50: [], book_lat_p50: [] };

    for (const e of EXCHANGES) {
        const X = ST.ex[e];
        X.trades.evict(now);
        X.book.evict(now);
        const ns = X.nstats;
        const out = { conn: {} };
        const det = { conns: X.conns, normalizer: ns ? { conns: ns.conns, side_check: ns.side_check, extra: ns.extra, age_ms: now - ns.t } : null };

        for (const k of KINDS) {
            const tl = X.tel[k];
            const nsc = ns && ns.conns[k];
            let rawFps = null, invPs = null;
            if (nsc) {
                if (X.prevFrames[k] !== undefined) rawFps = r((nsc.frames - X.prevFrames[k]) / dt, 1);
                if (X.prevInvalid[k] !== undefined) invPs = r((nsc.invalid - X.prevInvalid[k]) / dt, 1);
                X.prevFrames[k] = nsc.frames;
                X.prevInvalid[k] = nsc.invalid;
            }
            const ia = tl.sec.ia.summary(1), lat = tl.sec.lat.summary(1), pipe = tl.sec.pipe.summary(1);
            const eps = r(tl.sec.events / dt, 1);
            const age = tl.lastFrameT ? now - tl.lastFrameT : null;
            const c = X.conns[k];
            out.conn[k] = c ? c.status : 'unknown';
            out[k + '_eps'] = eps;
            out[k + '_fps'] = rawFps;
            out[k + '_interarrival_ms'] = ia ? ia.mean : null;
            out[k + '_ia_p50_ms'] = ia ? ia.p50 : null;
            out[k + '_ia_p95_ms'] = ia ? ia.p95 : null;
            out[k + '_latency_p50_ms'] = lat ? lat.p50 : null;
            out[k + '_age_ms'] = age;
            det[k] = {
                events_ps: eps, data_frames_ps: r(tl.sec.frames / dt, 1), raw_frames_ps: rawFps, invalid_ps: invPs,
                events_per_frame: tl.sec.frames ? r(tl.sec.events / tl.sec.frames, 2) : null,
                interarrival_ms: ia, latency_ms: lat, pipeline_lag_ms: pipe,
                age_ms: age, events_total: tl.events, data_frames_total: tl.frames,
                normalizer: nsc || null, connection: c || null
            };
            rank[k + '_eps'].push([e, eps]);
            if (ia) rank[k + '_ia_p50'].push([e, ia.p50]);
            if (lat) rank[k + '_lat_p50'].push([e, lat.p50]);
            tl.resetSec();
        }

        // ---- trades ----
        const last = X.trades.last;
        out.last_price = last;
        det.trade.windows = {};
        for (const w of X.trades.wins) {
            const m = w.metrics(last, true);
            det.trade.windows[winKey(w.ms)] = m;
            moves[w.ms].push(m.move_bps);
        }
        const w250 = det.trade.windows['250ms'], w1000 = det.trade.windows['1000ms'];
        out.trades_250ms = w250.trades;
        out.buy_usd_250ms = w250.buy_usd;
        out.sell_usd_250ms = w250.sell_usd;
        out.delta_usd_250ms = w250.delta_usd;
        out.price_move_250ms_bps = w250.move_bps;
        out.max_excursion_250ms_bps = w250.max_up_bps === null ? null : r(Math.max(w250.max_up_bps, -w250.max_dn_bps), 2);
        out.delta_usd_1000ms = w1000.delta_usd;
        out.price_move_1000ms_bps = w1000.move_bps;

        // ---- book ----
        const B = X.book;
        out.best_bid = B.bid;
        out.best_ask = B.ask;
        out.spread_bps = r(B.spreadBps(), 3);
        out.book_imbalance = r(B.i1, 3);
        out.book_imbalance_5 = r(B.i5, 3);
        out.book_imbalance_10 = r(B.i10, 3);
        det.book.state = {
            best_bid: B.bid, best_bid_qty: B.bq, best_ask: B.ask, best_ask_qty: B.aq, spread_bps: r(B.spreadBps(), 3),
            bid_qty5: r(B.b5, 4), ask_qty5: r(B.a5, 4), bid_qty10: r(B.b10, 4), ask_qty10: r(B.a10, 4),
            imbalance_1: r(B.i1, 4), imbalance_5: r(B.i5, 4), imbalance_10: r(B.i10, 4), channels: B.channels
        };
        det.book.windows = {};
        for (const w of B.wins) det.book.windows[winKey(w.ms)] = B.winMetrics(w);
        out.imb_vel_250ms_per_s = det.book.windows['250ms'].imb_vel_per_s;

        // ---- calidad ----
        if (ns) {
            const sc = ns.side_check, tot = sc.agree + sc.disagree;
            out.side_check_agree_pct = tot ? r(sc.agree / tot * 100, 1) : null;
            out.invalid_total = ns.conns.trade.invalid + ns.conns.book.invalid;
            out.gaps_total = ns.conns.trade.gaps + ns.conns.book.gaps;
            out.resyncs_total = ns.conns.trade.resyncs + ns.conns.book.resyncs;
        }
        out.reconnects = (X.conns.trade ? X.conns.trade.reconnects : 0) + (X.conns.book ? X.conns.book.reconnects : 0);

        pulse[e] = out;
        detail.exchanges[e] = det;
    }

    // ---- consolidado (flujos sumados; precio = mediana de los movimientos por exchange) ----
    ST.all.evict(now);
    const all = {}, allDet = {};
    for (const w of ST.all.wins) {
        const m = w.metrics(null, false);
        m.median_move_bps = r(median(moves[w.ms]), 2);
        allDet[winKey(w.ms)] = m;
    }
    all.trade_eps = r(EXCHANGES.reduce(function (s, e) { return s + (pulse[e].trade_eps || 0); }, 0), 1);
    all.book_eps = r(EXCHANGES.reduce(function (s, e) { return s + (pulse[e].book_eps || 0); }, 0), 1);
    all.usd_250ms = allDet['250ms'].usd;
    all.buy_usd_250ms = allDet['250ms'].buy_usd;
    all.sell_usd_250ms = allDet['250ms'].sell_usd;
    all.delta_usd_250ms = allDet['250ms'].delta_usd;
    all.median_price_move_250ms_bps = allDet['250ms'].median_move_bps;
    all.delta_usd_1000ms = allDet['1000ms'].delta_usd;
    pulse.ALL = all;
    detail.consolidated = allDet;

    // ---- ranking de frecuencia/latencia ----
    const desc = function (a) { return a.sort(function (x, y) { return (y[1] || 0) - (x[1] || 0); }).map(function (x) { return x[0] + ':' + x[1]; }); };
    const asc = function (a) { return a.sort(function (x, y) { return x[1] - y[1]; }).map(function (x) { return x[0] + ':' + x[1]; }); };
    pulse.ranking = {
        trade_eps: desc(rank.trade_eps), book_eps: desc(rank.book_eps),
        trade_ia_p50_ms: asc(rank.trade_ia_p50), book_ia_p50_ms: asc(rank.book_ia_p50),
        trade_latency_p50_ms: asc(rank.trade_lat_p50), book_latency_p50_ms: asc(rank.book_lat_p50)
    };

    // ---- salud de Node-RED ----
    const pipe = ST.global.pipe.summary(2), loop = ST.global.loop.summary(1);
    pulse.node = {
        msgs_ps: r(ST.global.msgs / dt, 1), events_ps: r(ST.global.events / dt, 1),
        pipeline_lag_p50_ms: pipe ? pipe.p50 : null, pipeline_lag_p95_ms: pipe ? pipe.p95 : null, pipeline_lag_max_ms: pipe ? pipe.max : null,
        loop_lag_p95_ms: loop ? loop.p95 : null, loop_lag_max_ms: loop ? loop.max : null
    };
    detail.node = { msgs_ps: pulse.node.msgs_ps, events_ps: pulse.node.events_ps, pipeline_lag_ms: pipe, event_loop_lag_ms: loop, unknown_msgs: ST.global.unknown };
    ST.global.msgs = 0; ST.global.events = 0; ST.global.pipe.reset(); ST.global.loop.reset();

    node.send([{ topic: 'wave_raw_v1/pulse', payload: pulse }, { topic: 'wave_raw_v1/telemetry', payload: detail }], false);
    node.status({
        fill: (pulse.node.pipeline_lag_p95_ms !== null && pulse.node.pipeline_lag_p95_ms > 50) ? 'yellow' : 'green', shape: 'dot',
        text: pulse.node.events_ps + ' ev/s · cola p95 ' + pulse.node.pipeline_lag_p95_ms + 'ms · loop max ' + pulse.node.loop_lag_max_ms + 'ms'
    });
}

// sonda de lag del event loop: si Node-RED se atrasa, este timer llega tarde
let probeAt = Date.now();
const probe = setInterval(function () {
    const n = Date.now();
    ST.global.loop.add(Math.max(0, n - probeAt - LOOP_PROBE_MS));
    probeAt = n;
}, LOOP_PROBE_MS);

const emitter = setInterval(emit, EMIT_EVERY_MS);

globalThis.__WAVE_ENGINE__ = {
    onMsg: function (msg) {
        const k = msg && msg.kind;
        if (k === 'events') onEvents(msg);
        else if (k === 'conn') onConn(msg);
        else if (k === 'nstats') onNstats(msg);
        else if (msg && msg.topic === 'reset') { ST = newState(); probeAt = Date.now(); }
    },
    stop: function () { clearInterval(probe); clearInterval(emitter); }
};
