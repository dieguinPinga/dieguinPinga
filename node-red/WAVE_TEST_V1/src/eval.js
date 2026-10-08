// =====================================================================
// WAVE_TEST_V1 · WAVE TEST EVAL
// ---------------------------------------------------------------------
// ¿Tiene WAVE poder predictivo? Mide, sin look-ahead, qué hace el mid de
// Binance tras cada observación direccional de WAVE.
//
// ENTRADA: copia (tap) de los mensajes que los 4 NORMALIZER envían al
//          WAVE RAW ENGINE: { kind:'events'|'conn'|'nstats', exchange, t_recv, payload }
// SEÑAL  : muestreo en una grilla fija de 250 ms (reloj local). En cada borde T:
//          A = ALL.delta_usd_250ms (flujo agresor BUY-SELL consolidado, 250 ms)
//          dirección = signo(A): UP / DOWN / NEUTRAL (A = 0)
//          fuerza   = |A| en USD (bruta, SIEMPRE registrada)
//          + B, C, D, movimiento de precio, spread, etc. para reanálisis.
// PRECIO : mid = (best_bid + best_ask) / 2.  Principal: Binance.
//          Control: mediana de los retornos de mid de los exchanges disponibles.
// SIN LOOK-AHEAD: todo se rige por local_receive_timestamp (t_recv).
//          Antes de aplicar un mensaje con t_recv = x se ejecuta advance(x):
//            1) se toman las muestras de los bordes T < x
//            2) se cierran los horizontes con vencimiento T+h < x
//          con el estado que contiene SOLO eventos de t_recv <= T (o <= T+h).
//          REORDER BUFFER (250 ms): los mensajes del tap se acumulan en RAM ordenados
//          por t_recv y sólo se entregan cuando t_recv <= watermark = now - 250 ms.
//          Así advance()/apply() ven un stream ESTRICTAMENTE ordenado por t_recv.
//          Un timer de 50 ms mueve el watermark aunque el feed se detenga.
//          Un mensaje que llega con t_recv <= watermark (ya entregado) NO se inserta
//          en ventanas ya procesadas: se cuenta como late_beyond_buffer y se descarta
//          sólo en el TEST. Nunca se usa un evento posterior al instante medido.
// Todo en RAM. Sin disco, sin DB, sin context store, sin órdenes.
// =====================================================================
const EXCHANGES = ['binance', 'coinbase', 'kraken', 'okx'];
const WINDOWS = [100, 250, 500, 1000];    // mismas ventanas que el WAVE RAW ENGINE
const RES_CAP = 2048;                     // requerido por Stat (copiado del engine)
const SAMPLE_MS = 250;                    // 1 observación por intervalo de 250 ms
const HORIZONS = [100, 250, 500, 1000, 2000, 5000];
const HKEYS = ['100ms', '250ms', '500ms', '1s', '2s', '5s'];
const REF_EX = 'binance';                 // precio objetivo principal
const REF_STALE_MS = 2000;                // mid con más de 2 s sin update => no válido
const WARMUP_MS = 2000;                   // las ventanas necesitan llenarse
const TICK_MS = 50;                       // timer: mueve el watermark aunque no lleguen eventos
const REORDER_MS = 250;                   // reorder buffer: retraso de entrega al evaluador
const REORDER_CAP = 50000;                // tope duro de mensajes en el buffer (sólo ante anomalías)
const LAT_CAP = 4096;                     // reservoir para p50/p95/p99 de lateness
const MAX_CATCHUP = 40;                   // máx. bordes a recuperar de golpe (10 s)
const MAX_PENDING = 400;                  // tope duro de la cola de señales pendientes
const REPORT_MS = 5000;                   // salida humana cada 5 s
const AGG_CAP = 4096;                     // reservoir para mediana/p25/p75 (media y conteos exactos)
const TERCILE_MIN_N = 60;                 // mínimo de señales previas para terciles provisionales
const RING_SAMPLES = 2000;                // últimas observaciones guardadas para DUMP
const TIE_EPS = 1e-6;                     // |retorno| < 1e-6 bps => tie (mid sin cambio)
const W250 = WINDOWS.indexOf(250);
const MID_HIST_MS = 1500;                 // historia de mids de Binance para movimientos previos
const MID_HIST_CAP = 5000;                // tope duro de esa historia

// ======== CÓDIGO COPIADO LITERALMENTE DEL WAVE RAW ENGINE (ventanas) ========
/*__ENGINE_WINDOWS__*/
// ======================= fin del código copiado ============================

function sgn(x) { return x > 0 ? 1 : (x < 0 ? -1 : 0); }

function medianOf(a) {
    if (!a.length) return null;
    const v = a.slice().sort(function (x, y) { return x - y; });
    const m = v.length >> 1;
    return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
}

// ---------- acumulador de resultados por horizonte/grupo ----------
function Agg() { this.n = 0; this.sum = 0; this.hits = 0; this.misses = 0; this.ties = 0; this.res = []; }
Agg.prototype.add = function (v) {
    this.n++; this.sum += v;
    if (Math.abs(v) < TIE_EPS) this.ties++; else if (v > 0) this.hits++; else this.misses++;
    if (this.res.length < AGG_CAP) this.res.push(v);
    else { const j = Math.floor(Math.random() * this.n); if (j < AGG_CAP) this.res[j] = v; }
};
Agg.prototype.summary = function () {
    const dec = this.hits + this.misses;
    const o = {
        n_valid: this.n, hits: this.hits, misses: this.misses, ties: this.ties,
        hit_rate_pct: dec ? r(this.hits / dec * 100, 1) : null,
        tie_rate_pct: this.n ? r(this.ties / this.n * 100, 1) : null,
        mean_signed_bps: this.n ? r(this.sum / this.n, 3) : null,
        median_signed_bps: null, p25_signed_bps: null, p75_signed_bps: null
    };
    if (this.res.length) {
        const a = Float64Array.from(this.res).sort();
        const q = function (p) { return a[Math.min(a.length - 1, Math.round(p * (a.length - 1)))]; };
        o.median_signed_bps = r(q(0.5), 3); o.p25_signed_bps = r(q(0.25), 3); o.p75_signed_bps = r(q(0.75), 3);
    }
    return o;
};

function newResults(groups) {
    const o = {};
    for (const k of HKEYS) { o[k] = {}; for (const g of groups) o[k][g] = new Agg(); }
    return o;
}

const PRIMARY_GROUPS = ['all', 'up', 'down', 'weak', 'medium', 'strong'];

function newState() {
    const now = Date.now();
    const S = {
        t0: now, startT: now, nextT: Math.ceil(now / SAMPLE_MS) * SAMPLE_MS, lastAdv: 0, seq: 0,
        ex: {}, all: new TradeStream(),
        q: [], head: 0, ptr: HORIZONS.map(function () { return 0; }),
        ring: [], strength: new Agg(), terc: null,
        primary: newResults(PRIMARY_GROUPS),
        secondary: newResults(['all', 'up', 'down']),
        baseline: newResults(['all']),
        c: {
            n_total_samples: 0, n_warmup: 0, n_skipped_intervals: 0,
            n_directional: 0, n_up: 0, n_down: 0, n_neutral: 0,
            n_no_ref: 0, n_evaluated: 0,
            pending_max_seen: 0, pending_dropped: 0, horizons_expired: 0, late_events: 0,
            reorder_buffer_max: 0, late_beyond_buffer: 0, out_of_order_events: 0, reorder_forced: 0, reorder_delivered: 0,
            invalid_ref_by_h: {}, invalid_sec_by_h: {}
        }
    };
    for (const k of HKEYS) { S.c.invalid_ref_by_h[k] = 0; S.c.invalid_sec_by_h[k] = 0; }
    for (const e of EXCHANGES) S.ex[e] = { trades: new TradeStream(), book: new BookState(), bookT: 0 };
    // reorder buffer: [{t, seq, msg}] ordenado por (t_recv, orden de llegada)
    S.rb = { a: [], h: 0, seq: 0, wm: 0, maxSeen: 0, lateness: new LatStat(), arrival: new LatStat() };
    S.midHist = new Deque(); // [{t, mid}] de Binance, para el movimiento de mid ANTES de t0
    return S;
}

let S = newState();

// mid "as-of" el instante at: último mid conocido, si es reciente
function midOf(e, at) {
    const X = S.ex[e];
    const b = X.book;
    if (b.bid === null || b.ask === null || !X.bookT) return null;
    if (at - X.bookT > REF_STALE_MS) return null;
    return (b.bid + b.ask) / 2;
}

// mid de Binance as-of 'at' (último valor con t <= at) desde la historia
function binanceMidAt(at) {
    const a = S.midHist.a;
    let lo = S.midHist.h, hi = a.length - 1, ans = null;
    while (lo <= hi) {
        const m = (lo + hi) >> 1;
        if (a[m].t <= at) { ans = a[m]; lo = m + 1; } else hi = m - 1;
    }
    return ans ? ans.mid : null;
}

function pushBinanceMid(t) {
    const B = S.ex.binance.book;
    if (B.bid === null || B.ask === null) return;
    const mid = (B.bid + B.ask) / 2;
    const H = S.midHist;
    if (H.len() && H.back().mid === mid) return;
    H.push({ t: t, mid: mid });
    // conservar MID_HIST_MS + el último valor anterior (es el "as-of" del borde de la ventana)
    while (H.len() > 1 && (H.a[H.h + 1].t <= t - MID_HIST_MS || H.len() > MID_HIST_CAP)) H.shift();
    H.compact();
}

function bps(a, b) { return (a !== null && b !== null && b > 0) ? r((a / b - 1) * 1e4, 3) : null; }

// =====================================================================
// MUESTRA en el borde t0 (estado = todos los eventos con t_recv <= t0)
// =====================================================================
function sample(T) {
    S.c.n_total_samples++;
    if (T - S.startT < WARMUP_MS) { S.c.n_warmup++; return; }
    for (const e of EXCHANGES) { S.ex[e].trades.evict(T); S.ex[e].book.evict(T); }
    S.all.evict(T);

    const wa = S.all.wins[W250];
    const A = wa.buy - wa.sell;
    const sg = A > 0 ? 1 : (A < 0 ? -1 : 0);
    const per = {};
    const imb10s = [];
    const movesByW = {};
    for (const ms of WINDOWS) movesByW[ms] = [];
    let breadth = 0, active = 0;
    const mids0 = {};
    for (const e of EXCHANGES) {
        const X = S.ex[e];
        const w = X.trades.wins[W250];
        const d = w.buy - w.sell;
        const B = X.book;
        const mid = midOf(e, T);
        if (w.n > 0) active++;
        if (sg !== 0 && sgn(d) === sg) breadth++;
        const mv = {};
        for (const tw of X.trades.wins) {
            const m = tw.metrics(X.trades.last, true).move_bps;  // misma definición que el engine
            mv[tw.ms] = m;
            if (m !== null) movesByW[tw.ms].push(m);
        }
        if (B.i10 !== null) imb10s.push(B.i10);
        if (mid !== null) mids0[e] = mid;
        per[e] = {
            delta_usd_250ms: r(d, 0), trades_250ms: w.n,
            price_move_100ms_bps: mv[100], price_move_250ms_bps: mv[250], price_move_500ms_bps: mv[500], price_move_1000ms_bps: mv[1000],
            mid: mid, spread_bps: r(B.spreadBps(), 3),
            imb1: r(B.i1, 4), imb5: r(B.i5, 4), imb10: r(B.i10, 4),
            imb_vel_250ms_per_s: B.winMetrics(B.wins[W250]).imb_vel_per_s,
            book_age_ms: X.bookT ? T - X.bookT : null, stale: mid === null
        };
    }
    const p0 = midOf(REF_EX, T);
    const bn = per.binance;
    const pre = {};
    for (const ms of WINDOWS) {
        pre['price_move_' + ms + 'ms_bps_binance'] = bn['price_move_' + ms + 'ms_bps'];
        pre['price_move_' + ms + 'ms_bps_median'] = r(medianOf(movesByW[ms]), 3);
        pre['binance_mid_move_' + ms + 'ms_bps'] = p0 === null ? null : bps(p0, binanceMidAt(T - ms));
    }
    const rec = {
        id: ++S.seq,
        t0: T,
        direction: sg > 0 ? 'UP' : (sg < 0 ? 'DOWN' : 'NEUTRAL'),
        sign: sg,
        delta_usd_250ms: r(A, 0),                                // A
        strength_abs_usd: r(Math.abs(A), 0),                     // fuerza bruta (siempre)
        buy_usd_250ms: r(wa.buy, 0),
        sell_usd_250ms: r(wa.sell, 0),
        usd_250ms: r(wa.usd, 0),
        trades_250ms: wa.n,
        delta_usd_100ms: r(S.all.wins[0].buy - S.all.wins[0].sell, 0),
        delta_usd_500ms: r(S.all.wins[2].buy - S.all.wins[2].sell, 0),
        delta_usd_1000ms: r(S.all.wins[3].buy - S.all.wins[3].sell, 0),
        flow_imbalance_250ms: wa.usd > 0 ? r(A / wa.usd, 4) : null,   // B
        breadth: breadth,                                        // C: exchanges con delta_250 del mismo signo
        active_exchanges: active,                                //    exchanges con trades en 250 ms
        book_imbalance: {                                        // D
            imb1_binance: bn.imb1, imb5_binance: bn.imb5, imb10_binance: bn.imb10,
            imb10_mean: imb10s.length ? r(imb10s.reduce(function (s, x) { return s + x; }, 0) / imb10s.length, 4) : null,
            imb_vel_250ms_per_s_binance: bn.imb_vel_250ms_per_s
        },
        spread_bps_binance: bn.spread_bps,
        pre_move: pre,                                           // movimiento YA ocurrido antes de t0
        exchanges: per,
        p0: p0, p0_age_ms: bn.book_age_ms,
        tercile_provisional: null,
        future: {},
        // internos
        _rnd: 0, _done: 0, _mids0: mids0
    };
    S.ring.push(rec);
    if (S.ring.length > RING_SAMPLES) S.ring.shift();

    if (sg === 0) { S.c.n_neutral++; return; }
    S.c.n_directional++;
    if (sg > 0) S.c.n_up++; else S.c.n_down++;
    // tercil PROVISIONAL con límites calculados sólo con señales anteriores
    if (S.terc) rec.tercile_provisional = rec.strength_abs_usd <= S.terc[0] ? 'weak' : (rec.strength_abs_usd <= S.terc[1] ? 'medium' : 'strong');
    S.strength.add(rec.strength_abs_usd);
    if (p0 === null) { S.c.n_no_ref++; return; }   // Binance stale/caído: no evaluable
    rec._rnd = Math.random() < 0.5 ? -1 : 1;         // baseline aleatoria (sanity check)
    S.c.n_evaluated++;
    S.q.push(rec);
    while (S.q.length - S.head > MAX_PENDING) dropOldest();
    const live = S.q.length - S.head;
    if (live > S.c.pending_max_seen) S.c.pending_max_seen = live;
}

// cierre del horizonte k de la señal s, con el estado as-of t0 + h
function resolve(s, k) {
    const key = HKEYS[k];
    const at = s.t0 + HORIZONS[k];
    const f = { p: null, ret_bps: null, signed_bps: null, outcome: null, sec_signed_bps: null };
    const p1 = midOf(REF_EX, at);
    if (p1 === null) {
        S.c.invalid_ref_by_h[key]++;
    } else {
        const ret = (p1 / s.p0 - 1) * 1e4;
        const signed = ret * s.sign;
        f.p = p1; f.ret_bps = r(ret, 3); f.signed_bps = r(signed, 3);
        f.outcome = Math.abs(signed) < TIE_EPS ? 'tie' : (signed > 0 ? 'hit' : 'miss');
        const P = S.primary[key];
        P.all.add(signed);
        P[s.sign > 0 ? 'up' : 'down'].add(signed);
        if (s.tercile_provisional) P[s.tercile_provisional].add(signed);
        S.baseline[key].all.add(ret * s._rnd);
    }
    // control secundario: mediana de retornos de mid por exchange (>= 2 exchanges no stale)
    const rets = [];
    for (const e of EXCHANGES) {
        const m0 = s._mids0[e];
        if (m0 === undefined) continue;
        const m1 = midOf(e, at);
        if (m1 !== null) rets.push((m1 / m0 - 1) * 1e4);
    }
    if (rets.length >= 2) {
        const v = medianOf(rets) * s.sign;
        f.sec_signed_bps = r(v, 3);
        S.secondary[key].all.add(v);
        S.secondary[key][s.sign > 0 ? 'up' : 'down'].add(v);
    } else {
        S.c.invalid_sec_by_h[key]++;
    }
    s.future[key] = f;
    s._done++;
}

// tope duro: descartar la señal pendiente más antigua (sólo ante anomalías)
function dropOldest() {
    const s = S.q[S.head];
    for (let k = 0; k < HORIZONS.length; k++) {
        if (S.ptr[k] <= S.head) { S.ptr[k] = S.head + 1; S.c.horizons_expired++; }
    }
    S.q[S.head] = undefined;
    S.head++;
    S.c.pending_dropped++;
    void s;
}

function advance(x) {
    S.lastAdv = x;
    if (x - S.nextT > MAX_CATCHUP * SAMPLE_MS) {   // salto de reloj / event loop bloqueado
        const skip = Math.floor((x - S.nextT) / SAMPLE_MS);
        S.c.n_skipped_intervals += skip;
        S.nextT += skip * SAMPLE_MS;
    }
    while (S.nextT < x) { sample(S.nextT); S.nextT += SAMPLE_MS; }
    // cerrar horizontes vencidos (cada puntero avanza en orden temporal)
    for (let k = 0; k < HORIZONS.length; k++) {
        const h = HORIZONS[k];
        while (S.ptr[k] < S.q.length && S.q[S.ptr[k]].t0 + h < x) { resolve(S.q[S.ptr[k]], k); S.ptr[k]++; }
    }
    // retirar las señales con todos sus horizontes cerrados
    while (S.head < S.q.length && S.q[S.head]._done >= HORIZONS.length) { S.q[S.head] = undefined; S.head++; }
    if (S.head > 512) {
        S.q = S.q.slice(S.head);
        for (let k = 0; k < S.ptr.length; k++) S.ptr[k] -= S.head;
        S.head = 0;
    }
}

// aplica eventos: mismas reglas que WAVE RAW ENGINE.onEvents (trades -> ventanas, book -> estado)
function apply(msg) {
    const X = S.ex[msg.exchange];
    if (!X) return;
    const t = msg.t_recv;
    const evs = msg.payload;
    let first = true;
    for (let i = 0; i < evs.length; i++) {
        const ev = evs[i];
        if (ev.kind === 'trade') {
            const rec = { t: t, p: ev.price, usd: ev.usd, s: ev.side === 'BUY' ? 1 : (ev.side === 'SELL' ? -1 : 0), f: first };
            first = false;
            X.trades.push(rec);
            S.all.push({ t: t, p: null, usd: rec.usd, s: rec.s, f: rec.f });
        } else if (ev.kind === 'book') {
            X.book.push(ev, t);
            X.bookT = t;
            if (X === S.ex.binance) pushBinanceMid(t);
        }
    }
}

// ---------- entrega al evaluador (lógica original de onMsg, ahora en orden estricto) ----------
function deliver(msg) {
    if (msg.kind === 'events') {
        const t = msg.t_recv;
        if (t > S.lastAdv) advance(t);
        else if (t < S.lastAdv) S.c.late_events++;   // con el reorder buffer debe quedar en 0
        apply(msg);
    } else if (msg.kind === 'conn') {
        const X = S.ex[msg.exchange];
        if (X && msg.conn === 'book' && msg.event === 'close') {
            X.book.clear(); X.bookT = 0;   // libro desconocido hasta el próximo snapshot => mid no válido
            if (X === S.ex.binance) { S.midHist = new Deque(); }
        }
    }
}

// ---------- reorder buffer ----------
// reservoir con generador propio (LCG): NO consume Math.random, así la secuencia aleatoria
// del evaluador (baseline) es exactamente la misma que sin reorder buffer
function LatStat() { this.n = 0; this.max = 0; this.res = []; this.rng = 12345; }
LatStat.prototype.add = function (v) {
    this.n++;
    if (v > this.max) this.max = v;
    if (this.res.length < LAT_CAP) this.res.push(v);
    else {
        this.rng = (Math.imul(this.rng, 1664525) + 1013904223) >>> 0;
        const j = this.rng % this.n;
        if (j < LAT_CAP) this.res[j] = v;
    }
};
LatStat.prototype.summary = function () {
    if (!this.n) return { n: 0, p50: null, p95: null, p99: null, max: null };
    const a = Float64Array.from(this.res).sort();
    const q = function (p) { return a[Math.min(a.length - 1, Math.round(p * (a.length - 1)))]; };
    return { n: this.n, p50: q(0.5), p95: q(0.95), p99: q(0.99), max: this.max };
};

function rbInsert(t, msg) {
    const R = S.rb;
    const now = Date.now();
    // lateness = cuánto llega "detrás" del t_recv más nuevo ya visto (0 si llega en orden)
    const late = t < R.maxSeen ? R.maxSeen - t : 0;
    if (t > R.maxSeen) R.maxSeen = t;
    R.lateness.add(late);
    R.arrival.add(Math.max(0, now - t));
    if (late > 0) S.c.out_of_order_events++;
    if (t <= R.wm) { S.c.late_beyond_buffer++; return; }   // ya entregado hasta el watermark: se descarta sólo en el TEST
    const it = { t: t, seq: ++R.seq, msg: msg };
    const a = R.a;
    let i = a.length;
    if (i === R.h || a[i - 1].t <= t) a.push(it);           // caso normal: llega en orden
    else {                                                  // inserción binaria por (t, seq)
        let lo = R.h, hi = i;
        while (lo < hi) { const m = (lo + hi) >> 1; if (a[m].t <= t) lo = m + 1; else hi = m; }
        a.splice(lo, 0, it);
    }
    const len = a.length - R.h;
    if (len > S.c.reorder_buffer_max) S.c.reorder_buffer_max = len;
    while (a.length - R.h > REORDER_CAP) {                  // tope duro: entregar el más viejo
        const x = a[R.h]; a[R.h] = undefined; R.h++;
        S.c.reorder_forced++;
        if (x.t > R.wm) R.wm = x.t;
        S.c.reorder_delivered++;
        deliver(x.msg);
    }
}

// entrega en orden todo lo que tenga t_recv <= W y luego avanza el reloj del evaluador hasta W
function flush(W) {
    const R = S.rb;
    if (W <= R.wm) return;
    const a = R.a;
    while (R.h < a.length && a[R.h].t <= W) {
        const x = a[R.h]; a[R.h] = undefined; R.h++;
        S.c.reorder_delivered++;
        deliver(x.msg);
    }
    R.wm = W;
    if (R.h > 4096 && R.h * 2 > a.length) { R.a = a.slice(R.h); R.h = 0; }
    if (W > S.lastAdv) advance(W);
}

function onMsg(msg) {
    if (!msg) return;
    if (msg.kind === 'events') {
        const t = msg.t_recv;
        if (typeof t !== 'number' || !Array.isArray(msg.payload)) return;
        rbInsert(t, msg);
        flush(Date.now() - REORDER_MS);
    } else if (msg.kind === 'conn') {
        rbInsert(typeof msg.t === 'number' ? msg.t : Date.now(), msg);   // ordenado con los eventos
        flush(Date.now() - REORDER_MS);
    } else if (msg.topic === 'reset') {
        S = newState();
        report();
    } else if (msg.topic === 'dump') {
        node.send([null, null, { topic: 'wave_test_v1/samples', payload: S.ring.map(compactRec) }], false);
    }
}

function compactRec(s) {
    const o = {};
    for (const k in s) if (k.charAt(0) !== '_') o[k] = s[k];
    return o;
}

// =====================================================================
// SALIDA HUMANA cada 5 s (sólo lectura de acumuladores)
// =====================================================================
function fmtH(sm) {
    if (!sm || !sm.n_valid) return 'n/a';
    const m = sm.mean_signed_bps;
    return (m >= 0 ? '+' : '') + m.toFixed(2) + 'bps ' + (sm.hit_rate_pct === null ? '-' : sm.hit_rate_pct.toFixed(1) + '%') + ' t' + sm.tie_rate_pct.toFixed(0) + '%';
}

function report() {
    const now = Date.now();
    // terciles provisionales: límites desde TODAS las señales previas (ventana expansiva)
    if (S.strength.n >= TERCILE_MIN_N) {
        const a = Float64Array.from(S.strength.res).sort();
        const q = function (p) { return a[Math.min(a.length - 1, Math.round(p * (a.length - 1)))]; };
        S.terc = [q(1 / 3), q(2 / 3)];
    }
    const primary = {}, secondary = {}, baseline = {};
    for (const k of HKEYS) {
        primary[k] = {};
        for (const g of PRIMARY_GROUPS) primary[k][g] = S.primary[k][g].summary();
        secondary[k] = { all: S.secondary[k].all.summary(), up: S.secondary[k].up.summary(), down: S.secondary[k].down.summary() };
        baseline[k] = S.baseline[k].all.summary();
    }
    let dist = null;
    if (S.strength.res.length) {
        const a = Float64Array.from(S.strength.res).sort();
        const q = function (p) { return r(a[Math.min(a.length - 1, Math.round(p * (a.length - 1)))], 0); };
        dist = { n: S.strength.n, p10: q(0.1), p25: q(0.25), p50: q(0.5), p75: q(0.75), p90: q(0.9), p99: q(0.99), max: q(1) };
    }
    const c = S.c;
    const detail = {
        ts: now, iso: new Date(now).toISOString(), uptime_s: Math.round((now - S.t0) / 1000),
        config: { sample_ms: SAMPLE_MS, horizons: HKEYS, reference: 'binance_mid', secondary: 'median_mid_return_all_exchanges',
            signal: 'sign(ALL.delta_usd_250ms)', strength: 'abs(ALL.delta_usd_250ms)', ref_stale_ms: REF_STALE_MS, tie_eps_bps: TIE_EPS },
        counts: {
            n_total_samples: c.n_total_samples, n_warmup: c.n_warmup, n_skipped_intervals: c.n_skipped_intervals,
            n_directional: c.n_directional, n_up: c.n_up, n_down: c.n_down, n_neutral: c.n_neutral,
            n_no_ref: c.n_no_ref, n_evaluated: c.n_evaluated,
            n_valid_by_h: HKEYS.reduce(function (o, k) { o[k] = S.primary[k].all.n; return o; }, {}),
            invalid_ref_by_h: c.invalid_ref_by_h, invalid_secondary_by_h: c.invalid_sec_by_h
        },
        queue: { pending: S.q.length - S.head, pending_max_seen: c.pending_max_seen, max_pending: MAX_PENDING,
            pending_dropped: c.pending_dropped, horizons_expired: c.horizons_expired, late_events: c.late_events,
            reorder_buffer_ms: REORDER_MS, reorder_buffer_current: S.rb.a.length - S.rb.h, reorder_buffer_max: c.reorder_buffer_max,
            late_beyond_buffer: c.late_beyond_buffer, out_of_order_events: c.out_of_order_events,
            reorder_forced: c.reorder_forced, reorder_delivered: c.reorder_delivered,
            watermark_lag_ms: S.rb.wm ? now - S.rb.wm : null,
            lateness_ms: S.rb.lateness.summary(), arrival_delay_ms: S.rb.arrival.summary() },
        strength: { field: 'abs(ALL.delta_usd_250ms)', unit: 'USD', distribution: dist,
            terciles_provisional: { thresholds_usd: S.terc ? [r(S.terc[0], 0), r(S.terc[1], 0)] : null,
                note: 'límites expansivos que cambian durante la corrida: sólo diagnóstico, NO prueba de monotonía' } },
        primary_binance_mid: primary,
        secondary_median_mid: secondary,
        baseline_random_direction: baseline,
        last_samples: S.ring.slice(-3).map(compactRec)
    };
    const line = 'WAVE TEST | n=' + c.n_total_samples + ' dir=' + c.n_directional + ' eval=' + c.n_evaluated +
        HKEYS.map(function (k) { return ' | ' + k + ' ' + fmtH(primary[k].all); }).join('');
    node.send([{ topic: 'wave_test_v1/line', payload: line }, { topic: 'wave_test_v1/detail', payload: detail }, null], false);
    node.status({ fill: 'green', shape: 'dot', text: 'n=' + c.n_total_samples + ' dir=' + c.n_directional + ' pend=' + (S.q.length - S.head) });
}

const tick = setInterval(function () {
    flush(Date.now() - REORDER_MS);   // vacía el buffer y mueve el reloj aunque el feed se detenga
}, TICK_MS);
const rep = setInterval(report, REPORT_MS);

globalThis.__WAVE_TEST__ = { onMsg: onMsg, stop: function () { clearInterval(tick); clearInterval(rep); } };
