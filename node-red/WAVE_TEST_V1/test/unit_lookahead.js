// Test determinístico de WAVE TEST EVAL: ejecuta el código "On Start" del JSON en un sandbox
// con timers controlados y eventos de t_recv exactos. Verifica ausencia de look-ahead,
// bordes (t == t0, t == t0+h), ties, stale, colas acotadas y conteos.
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const nodes = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'WAVE_TEST_V1.json'), 'utf8'));
const ev = nodes.find((n) => n.name === 'WAVE TEST EVAL');

const sent = [];
const timers = [];
const sb = {
    node: { send: (m) => sent.push(m), status: () => {}, warn: () => {}, error: (e) => { throw new Error(e); } },
    setInterval: (fn) => { timers.push(fn); return timers.length; }, clearInterval: () => {},
    Date, Math, JSON, Float64Array, Object, Array, Number, console
};
vm.createContext(sb);
vm.runInContext('(function(){\n' + ev.initialize + '\n})()', sb);
const W = sb.globalThis ? sb.globalThis.__WAVE_TEST__ : vm.runInContext('globalThis.__WAVE_TEST__', sb);
const report = timers[1]; // [0] = tick 50 ms (no se usa: sólo camino event-driven), [1] = reporte 5 s
const near = (a, b, m) => assert(a !== null && Math.abs(a - b) < 1e-9, m + ' (' + a + ' vs ' + b + ')');
const lastDetail = () => { sent.length = 0; report(); return sent[0][1].payload; };

const base = Math.ceil(Date.now() / 250) * 250 + 3000;   // pasado el warm-up
const T1 = base + 250;
const book = (ex, t, bid, ask) => W.onMsg({ kind: 'events', exchange: ex, ekind: 'book', t_recv: t,
    payload: [{ kind: 'book', exchange: ex, channel: 'bbo', best_bid: bid, best_bid_qty: 1, best_ask: ask, best_ask_qty: 1,
        bid_qty5: null, ask_qty5: null, bid_qty10: null, ask_qty10: null, exchange_timestamp: null, local_receive_timestamp: t }] });
const trade = (ex, t, side, usd, price) => W.onMsg({ kind: 'events', exchange: ex, ekind: 'trade', t_recv: t,
    payload: [{ kind: 'trade', exchange: ex, price: price, quantity: usd / price, usd: usd, side: side, exchange_timestamp: t - 1, local_receive_timestamp: t }] });

// --- escenario ---
book('binance', base + 1, 99.99, 100.01);          // mid 100.00
book('kraken', base + 2, 99.99, 100.01);           // mid 100.00 (control secundario)
trade('coinbase', base + 10, 'BUY', 1000, 100);     // dentro de (T1-250, T1]
trade('okx', base + 20, 'BUY', 300, 100);
trade('kraken', base + 30, 'SELL', 200, 100);       // A = 1000 + 300 - 200 = +1100 => UP, breadth = 2
trade('binance', T1, 'BUY', 50, 100);               // t == t0: INCLUIDO (t_recv <= t0) => A = +1150, breadth 3
trade('binance', T1 + 1, 'SELL', 999999, 100);      // t0+1: NO debe entrar en la muestra de T1
book('binance', T1 + 50, 100.09, 100.11);           // mid 100.10
book('binance', T1 + 100, 100.19, 100.21);          // mid 100.20 en t == t0+100: INCLUIDO en +100ms
book('binance', T1 + 101, 99.89, 99.91);            // mid 99.90 en t0+101: EXCLUIDO de +100ms
book('kraken', T1 + 101, 100.04, 100.06);           // kraken mid 100.05
book('binance', T1 + 400, 99.89, 99.91);            // dispara el cierre de +250ms (as-of t0+250 = 99.90)
book('binance', T1 + 1900, 99.89, 99.91);           // último update de Binance: el mid sigue en 99.90 (sin cambio)
trade('okx', T1 + 7000, 'BUY', 1, 100);             // avanza el reloj: +500ms..+5s vencidos

const d = lastDetail();
// buscar la muestra de T1 en el DUMP
sent.length = 0;
W.onMsg({ topic: 'dump' });
const ring = sent[0][2].payload;
const S = ring.find((x) => x.t0 === T1);
assert(S, 'existe la muestra en t0 = T1');
assert.strictEqual(S.direction, 'UP');
assert.strictEqual(S.delta_usd_250ms, 1150, 'el trade en t0 entra; el de t0+1 (999999 SELL) NO');
assert.strictEqual(S.strength_abs_usd, 1150);
assert.strictEqual(S.buy_usd_250ms, 1350);
assert.strictEqual(S.sell_usd_250ms, 200);
assert.strictEqual(S.breadth, 3);
assert.strictEqual(S.active_exchanges, 4);
near(S.p0, 100, 'S.p0');
near(S.future['100ms'].p, 100.2, '+100ms incluye el update en t0+100 y excluye el de t0+101');
assert.strictEqual(S.future['100ms'].ret_bps, 20);
assert.strictEqual(S.future['100ms'].outcome, 'hit');
near(S.future['250ms'].p, 99.9, 'p 250ms');
assert.strictEqual(S.future['250ms'].outcome, 'miss');
assert.strictEqual(S.future['250ms'].signed_bps, -10);
near(S.future['1s'].p, 99.9, 'p 1s');
near(S.future['2s'].p, 99.9, 'mid de Binance con 100 ms de antigüedad (< 2000) => válido');
assert.strictEqual(S.future['5s'].p, null, 'a t0+5000 el mid de Binance tiene 3100 ms => stale => inválido');
// secundario: mediana de [binance -10, kraken +5] = -2.5 a +250ms
assert.strictEqual(S.future['250ms'].sec_signed_bps, -2.5);
// muestra siguiente (T1+250): el SELL de 999999 en T1+1 sí entra => DOWN
const S2 = ring.find((x) => x.t0 === T1 + 250);
assert.strictEqual(S2.direction, 'DOWN');
assert.strictEqual(S2.delta_usd_250ms, -999999);
// pre-movimiento de mid de Binance en S2: p0(T1+250)=99.90 vs mid as-of T1+250-250=T1 => 100.00 => -10 bps
assert.strictEqual(S2.pre_move.binance_mid_move_250ms_bps, -10);
assert.strictEqual(S2.pre_move.binance_mid_move_100ms_bps, 0, 'as-of T1+150 el mid ya era 99.90');
assert.strictEqual(S.pre_move.binance_mid_move_250ms_bps, null, 'sin historia de mid antes de t0-250 => null (no se inventa)');
assert.strictEqual(S2.future['100ms'].outcome, 'tie', 'mid sin cambio => tie');
// conteos y cola
assert.strictEqual(d.counts.n_directional, 2);
assert.strictEqual(d.counts.n_up, 1);
assert.strictEqual(d.counts.n_down, 1);
assert.strictEqual(d.counts.n_evaluated, 2);
assert.strictEqual(d.counts.n_valid_by_h['100ms'], 2);
assert.strictEqual(d.counts.n_valid_by_h['5s'], 0);
assert.strictEqual(d.counts.invalid_ref_by_h['5s'], 2);
assert.strictEqual(d.queue.pending, 0, 'todas las señales cerradas tras +5 s => cola vacía');
const h100 = d.primary_binance_mid['100ms'];
assert.deepStrictEqual([h100.all.hits, h100.all.misses, h100.all.ties], [1, 0, 1]);
assert.strictEqual(h100.all.hit_rate_pct, 100, 'hit rate excluye ties');
assert.strictEqual(h100.all.tie_rate_pct, 50);
assert.strictEqual(h100.up.n_valid, 1);
assert.strictEqual(h100.down.n_valid, 1);
console.log('line:', sent.length, d.counts.n_total_samples, 'samples');

console.log('OK');
