// Test determinístico del reorder buffer de WAVE TEST EVAL.
// A: stream entregado en orden de t_recv.
// B: el MISMO stream con llegadas desordenadas (jitter hasta 200 ms entre sockets, FIFO por socket).
// => muestras, estadísticas, baseline y conteos deben ser IDÉNTICOS entre A y B.
// C: B + 20 mensajes que llegan 400 ms tarde (más allá del buffer de 250 ms)
// => late_beyond_buffer = 20 y se descartan sólo en el TEST.
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const nodes = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'WAVE_TEST_V1.json'), 'utf8'));
const EVAL = nodes.find((n) => n.name === 'WAVE TEST EVAL').initialize;

function rng(seed) { let s = seed >>> 0; return () => { s = (Math.imul(s, 1664525) + 1013904223) >>> 0; return s / 4294967296; }; }

function instance(startClock) {
    const CLOCK = { now: startClock };
    class FakeDate extends Date { constructor(...a) { if (a.length) super(...a); else super(CLOCK.now); } static now() { return CLOCK.now; } }
    const M = Object.create(Math);
    M.random = rng(777);                                        // misma semilla en todas las instancias
    const sent = [], timers = [];
    const sb = { node: { send: (m) => sent.push(m), status: () => {}, warn: () => {}, error: (e) => { throw new Error(e); } },
        setInterval: (fn) => { timers.push(fn); return timers.length; }, clearInterval: () => {},
        Date: FakeDate, Math: M, JSON, Float64Array, Object, Array, Number, console };
    vm.createContext(sb);
    vm.runInContext('(function(){\n' + EVAL + '\n})()', sb);
    const W = vm.runInContext('globalThis.__WAVE_TEST__', sb);
    const o = { CLOCK, W, sent, tick: timers[0], report: timers[1], nextTick: Math.ceil(startClock / 50) * 50 };
    // avanza el reloj virtual hasta 'to' ejecutando el timer de 50 ms en su momento
    o.runTo = (to) => { while (o.nextTick <= to) { CLOCK.now = o.nextTick; o.tick(); o.nextTick += 50; } CLOCK.now = to; };
    o.deliver = (arrival, msg) => { o.runTo(arrival); W.onMsg(msg); };
    o.detail = () => { sent.length = 0; o.report(); return sent.find((m) => m[1])[1].payload; };
    o.dump = () => { sent.length = 0; W.onMsg({ topic: 'dump' }); return sent.find((m) => m[2])[2].payload; };
    return o;
}

// ---------- stream sintético (determinístico) ----------
const R = rng(42);
const T0 = Math.ceil(Date.now() / 250) * 250 + 10000;
const DUR = 120000;
const msgs = [];
const mids = { binance: 60000, coinbase: 60010, kraken: 59990, okx: 60005 };
const conns = { binance: { book: 4, trade: 9 }, coinbase: { book: 20, trade: 13 }, kraken: { book: 25, trade: 37 }, okx: { book: 10, trade: 12 } };
let gen = 0;
for (const ex of Object.keys(conns)) {
    for (const kind of ['book', 'trade']) {
        let t = T0 + Math.floor(R() * 20);
        while (t < T0 + DUR) {
            if (kind === 'book') {
                mids[ex] += (R() - 0.5) * 2;
                const m = Math.round(mids[ex] * 10) / 10, half = 0.05 + Math.floor(R() * 3) * 0.1;
                msgs.push({ t, ex, conn: 'book', gen: gen++, msg: { kind: 'events', exchange: ex, conn: 'book', ekind: 'book', t_recv: t, payload: [{
                    kind: 'book', exchange: ex, channel: 'bbo', best_bid: Math.round((m - half) * 100) / 100, best_bid_qty: 0.1 + R() * 3,
                    best_ask: Math.round((m + half) * 100) / 100, best_ask_qty: 0.1 + R() * 3,
                    bid_qty5: 2 + R() * 10, ask_qty5: 2 + R() * 10, bid_qty10: 5 + R() * 20, ask_qty10: 5 + R() * 20,
                    exchange_timestamp: t - 3, local_receive_timestamp: t }] } });
            } else {
                const n = 1 + Math.floor(R() * 3), evs = [];
                for (let i = 0; i < n; i++) {
                    const buy = R() < 0.5, price = Math.round((mids[ex] + (buy ? 0.1 : -0.1)) * 100) / 100, qty = 0.001 + R() * 0.3;
                    evs.push({ kind: 'trade', exchange: ex, price, quantity: qty, usd: price * qty, side: buy ? 'BUY' : 'SELL', exchange_timestamp: t - 2, local_receive_timestamp: t });
                }
                msgs.push({ t, ex, conn: 'trade', gen: gen++, msg: { kind: 'events', exchange: ex, conn: 'trade', ekind: 'trade', t_recv: t, payload: evs } });
            }
            t += 1 + Math.floor(R() * conns[ex][kind] * 2);
        }
    }
}
// un cierre de socket de libro (ordenado por su t) a mitad de la corrida
msgs.push({ t: T0 + 60003, ex: 'okx', conn: 'book', gen: gen++, msg: { kind: 'conn', exchange: 'okx', conn: 'book', event: 'close', t: T0 + 60003 } });

// orden de referencia: por t_recv y, a igual t_recv, por orden de llegada al tap
const sorted = msgs.slice().sort((a, b) => a.t - b.t || a.gen - b.gen);

// llegadas desordenadas: jitter 0..200 ms por socket, respetando FIFO dentro de cada socket
function arrivals(list, extraLate) {
    const J = rng(99), lastBySock = {};
    const out = list.map((m, i) => {
        const sock = m.ex + '/' + m.conn;
        let a = m.t + (J() < 0.35 ? Math.floor(J() * 200) : Math.floor(J() * 3));
        if (extraLate && extraLate.has(i)) return { a: m.t + 400, m };   // llega tarde sin arrastrar a los siguientes
        if (lastBySock[sock] !== undefined && a < lastBySock[sock]) a = lastBySock[sock];
        lastBySock[sock] = a;
        return { a, m };
    });
    return out.sort((x, y) => x.a - y.a || x.m.gen - y.m.gen);
}

function run(order) {
    const I = instance(T0 - 5000);
    for (const x of order) I.deliver(x.a, x.m.msg);
    I.runTo(T0 + DUR + 7000);
    return { detail: I.detail(), dump: I.dump() };
}

const A = run(sorted.map((m) => ({ a: m.t, m })));
const shuffled = arrivals(sorted);
let inversions = 0, maxSeen = 0;
for (const x of shuffled) { if (x.m.t < maxSeen) inversions++; maxSeen = Math.max(maxSeen, x.m.t); }
const B = run(shuffled);

const strip = (d) => { const o = JSON.parse(JSON.stringify(d)); delete o.ts; delete o.iso; delete o.queue; delete o.uptime_s; return o; };
assert(inversions > 1000, 'el stream B realmente llega desordenado (' + inversions + ' inversiones)');
assert.strictEqual(A.detail.counts.n_directional > 300, true, 'hay suficientes señales direccionales');
function firstDiff(x, y, p) { if (JSON.stringify(x) === JSON.stringify(y)) return null; if (typeof x !== 'object' || x === null || typeof y !== 'object' || y === null) return p + ': ' + JSON.stringify(x) + ' vs ' + JSON.stringify(y); for (const k of new Set(Object.keys(x).concat(Object.keys(y)))) { const d = firstDiff(x[k], y[k], p + '.' + k); if (d) return d; } return p; }
if (process.env.DIFF) { console.log('len', A.dump.length, B.dump.length); console.log(firstDiff(A.dump, B.dump, 'dump')); console.log(firstDiff(strip(A.detail), strip(B.detail), 'detail')); }
// comparación por contenido serializado (cada instancia vive en su propio contexto vm => prototipos distintos)
assert.strictEqual(JSON.stringify(B.dump), JSON.stringify(A.dump), 'muestras (todas, con pre_move y future) idénticas');
assert.strictEqual(JSON.stringify(strip(B.detail)), JSON.stringify(strip(A.detail)), 'counts, strength, primary, secondary y baseline idénticos');
assert.strictEqual(A.detail.queue.late_events, 0);
assert.strictEqual(B.detail.queue.late_events, 0, 'late_events = 0 con el reorder buffer');
assert.strictEqual(B.detail.queue.late_beyond_buffer, 0);
assert(B.detail.queue.out_of_order_events > 1000);

// C: 20 mensajes más allá del buffer
const lateIdx = new Set();
for (let i = 2000; lateIdx.size < 20; i += 997) if (sorted[i].msg.kind === 'events') lateIdx.add(i);
const C = run(arrivals(sorted, lateIdx));
assert.strictEqual(C.detail.queue.late_beyond_buffer, 20, 'los 20 mensajes con 400 ms de retraso se cuentan como late_beyond_buffer');
assert.strictEqual(C.detail.queue.late_events, 0, 'y no contaminan las ventanas ya procesadas (late_events = 0)');

const q = B.detail.queue;
console.log('mensajes:', sorted.length, '· inversiones de orden en B:', inversions);
console.log('A == B: muestras', A.dump.length, '· n_directional', A.detail.counts.n_directional, '· n_valid 1s', A.detail.counts.n_valid_by_h['1s'],
    '· mean signed 1s', A.detail.primary_binance_mid['1s'].all.mean_signed_bps);
console.log('B reorder: current', q.reorder_buffer_current, '· max', q.reorder_buffer_max, '· out_of_order', q.out_of_order_events,
    '· lateness ms', JSON.stringify(q.lateness_ms), '· arrival_delay ms', JSON.stringify(q.arrival_delay_ms));
console.log('C: late_beyond_buffer', C.detail.queue.late_beyond_buffer, '· n_directional', C.detail.counts.n_directional);
console.log('OK');
