// Servidores WebSocket simulados con el formato de cada exchange + fallos inyectados.
// SOLO para pruebas end-to-end del flow (los exchanges reales no son accesibles desde CI).
// Uso: node mock_exchanges.js <ws-module-path> <portBase>
'use strict';
const WS = require(process.argv[2] || 'ws');
const http = require('http');
const zlib = require('zlib');
const BASE = +(process.argv[3] || 18100);
const T0 = Date.now();
const el = () => Date.now() - T0;
const log = (...a) => console.log('[mock +' + (el() / 1000).toFixed(1) + 's]', ...a);
const crc32 = (s) => zlib.crc32(Buffer.from(s)) >>> 0;

// ---------- mercado sintético: un libro independiente por exchange ----------
// tick 0.1, 60 niveles por lado
function r1(x) { return Math.round(x * 10) / 10; }
function r8(x) { return Math.round(x * 1e8) / 1e8; }
const sorted = (m, desc) => [...m.entries()].sort((a, b) => desc ? b[0] - a[0] : a[0] - b[0]);
function Market(mid) {
    const M = { bids: new Map(), asks: new Map() };
    for (let i = 0; i < 60; i++) {
        M.bids.set(r1(mid - 0.1 * (i + 1)), r8(0.1 + Math.random() * 2));
        M.asks.set(r1(mid + 0.1 * (i + 1)), r8(0.1 + Math.random() * 2));
    }
    M.bestBid = () => sorted(M.bids, true)[0];
    M.bestAsk = () => sorted(M.asks, false)[0];
    // mutación aleatoria cerca del top: devuelve lista de cambios [side, price, qty]
    M.mutate = () => {
        const ch = [];
        const side = Math.random() < 0.5 ? 'bid' : 'ask';
        const book = side === 'bid' ? M.bids : M.asks;
        const top = sorted(book, side === 'bid').slice(0, 15);
        const r = Math.random();
        if (r < 0.15) { // borrar un nivel del top
            const [p] = top[Math.floor(Math.random() * 5)];
            book.delete(p); ch.push([side, p, 0]);
        } else if (r < 0.3) { // nuevo nivel cerca del top (sin cruzar)
            const bb = M.bestBid()[0], ba = M.bestAsk()[0];
            let p = side === 'bid' ? r1(bb + 0.1 - 0.1 * Math.floor(Math.random() * 6)) : r1(ba - 0.1 + 0.1 * Math.floor(Math.random() * 6));
            if (side === 'bid' && p >= ba) p = r1(ba - 0.1);
            if (side === 'ask' && p <= bb) p = r1(bb + 0.1);
            if (!(side === 'bid' ? p >= ba : p <= bb)) { const q = r8(0.01 + Math.random()); book.set(p, q); ch.push([side, p, q]); }
        } else {
            const [p] = top[Math.floor(Math.random() * top.length)];
            const q = r8(0.01 + Math.random() * 3);
            book.set(p, q); ch.push([side, p, q]);
        }
        while (book.size < 60) { // reponer profundidad por el fondo
            const sd = sorted(book, side === 'bid');
            const worst = sd[sd.length - 1][0];
            const p = side === 'bid' ? r1(worst - 0.1) : r1(worst + 0.1);
            book.set(p, 0.5); ch.push([side, p, 0.5]);
        }
        return ch;
    };
    // trade sintético: agresor BUY ejecuta en el ask, SELL en el bid
    M.trade = () => {
        const buy = Math.random() < 0.5;
        return { buy, price: buy ? M.bestAsk()[0] : M.bestBid()[0], qty: r8(0.0001 + Math.random() * 0.2) };
    };
    return M;
}
const MK = { binance: Market(60000), coinbase: Market(60010), kraken: Market(59990), okx: Market(60005) };

function server(port, name, onConn) {
    const srv = http.createServer();
    const wss = new WS.Server({ server: srv });
    wss.on('connection', (ws, req) => {
        ws.on('error', () => {});
        log(name, 'connection', req.url);
        onConn(ws, req.url);
    });
    srv.listen(port);
    return wss;
}
function every(ws, ms, fn) {
    const h = setInterval(() => { if (ws.readyState === 1) { try { fn(); } catch (e) { console.error(e); } } else clearInterval(h); }, ms);
    ws.on('close', () => clearInterval(h));
    return h;
}
const send = (ws, o) => { if (ws.readyState === 1) ws.send(typeof o === 'string' ? o : JSON.stringify(o)); };

// ================= BINANCE =================
let bnU = 1000, bnTradeConns = 0;
server(BASE + 1, 'binance', (ws, url) => {
    if (url.includes('@trade')) {
        bnTradeConns++;
        const myN = bnTradeConns;
        let id = 1;
        every(ws, 7, () => {
            const t = MK.binance.trade();
            send(ws, { stream: 'btcusdt@trade', data: { e: 'trade', E: Date.now(), s: 'BTCUSDT', t: id++, p: t.price.toFixed(2), q: t.qty.toFixed(5), T: Date.now() - 2, m: !t.buy, M: true } });
        });
        // FALLO: a los 8s el servidor corta la 1ª conexión de trades
        if (myN === 1) setTimeout(() => { log('binance: KILL trade socket'); ws.terminate(); }, 8000);
    } else {
        every(ws, 3, () => {
            MK.binance.mutate(); bnU++;
            const b = MK.binance.bestBid(), a = MK.binance.bestAsk();
            send(ws, { stream: 'btcusdt@bookTicker', data: { u: bnU, s: 'BTCUSDT', b: b[0].toFixed(2), B: b[1].toFixed(8), a: a[0].toFixed(2), A: a[1].toFixed(8) } });
        });
        every(ws, 100, () => {
            send(ws, { stream: 'btcusdt@depth10@100ms', data: { lastUpdateId: bnU,
                bids: sorted(MK.binance.bids, true).slice(0, 10).map((l) => [l[0].toFixed(2), l[1].toFixed(8)]),
                asks: sorted(MK.binance.asks, false).slice(0, 10).map((l) => [l[0].toFixed(2), l[1].toFixed(8)]) } });
        });
    }
});

// ================= COINBASE =================
let cbBookConns = 0;
server(BASE + 2, 'coinbase', (ws) => {
    let seq = 0, isBook = false, gapDone = false;
    const env = (channel, events) => ({ channel, client_id: '', timestamp: new Date().toISOString().replace('Z', '123Z'), sequence_num: seq++, events });
    ws.on('message', (d) => {
        const m = JSON.parse(d.toString());
        send(ws, env('subscriptions', [{ subscriptions: { [m.channel]: m.product_ids } }]));
        if (m.channel === 'heartbeats') {
            let c = 0;
            every(ws, 1000, () => send(ws, env('heartbeats', [{ current_time: new Date().toISOString(), heartbeat_counter: c++ }])));
        }
        if (m.channel === 'market_trades') {
            // snapshot histórico (debe ignorarse)
            send(ws, env('market_trades', [{ type: 'snapshot', trades: [{ trade_id: '1', product_id: 'BTC-USD', price: '1.00', size: '1', side: 'BUY', time: new Date().toISOString() }] }]));
            let id = 10;
            every(ws, 50, () => {
                const n = 1 + Math.floor(Math.random() * 3);
                const trades = [];
                for (let i = 0; i < n; i++) {
                    const t = MK.coinbase.trade();
                    // Advanced Trade: 'side' = lado del MAKER
                    trades.push({ trade_id: String(id++), product_id: 'BTC-USD', price: t.price.toFixed(2), size: t.qty.toFixed(8), side: t.buy ? 'SELL' : 'BUY', time: new Date(Date.now() - 30).toISOString().replace('Z', '456789Z') });
                }
                send(ws, env('market_trades', [{ type: 'update', trades }]));
            });
        }
        if (m.channel === 'level2') {
            isBook = true;
            cbBookConns++;
            const myN = cbBookConns;
            const ups = [];
            for (const [p, q] of MK.coinbase.bids) ups.push({ side: 'bid', event_time: new Date().toISOString(), price_level: p.toFixed(2), new_quantity: q.toFixed(8) });
            for (const [p, q] of MK.coinbase.asks) ups.push({ side: 'offer', event_time: new Date().toISOString(), price_level: p.toFixed(2), new_quantity: q.toFixed(8) });
            send(ws, env('l2_data', [{ type: 'snapshot', product_id: 'BTC-USD', updates: ups }]));
            every(ws, 20, () => {
                const ch = MK.coinbase.mutate();
                // FALLO: a los 10s, la 1ª conexión de libro pierde un mensaje (salto de sequence_num)
                if (myN === 1 && !gapDone && el() > 10000) { gapDone = true; seq += 1; log('coinbase: SEQUENCE GAP'); }
                send(ws, env('l2_data', [{ type: 'update', product_id: 'BTC-USD', updates: ch.map(([s, p, q]) => ({ side: s === 'bid' ? 'bid' : 'offer', event_time: new Date().toISOString(), price_level: p.toFixed(2), new_quantity: q.toFixed(8) })) }]));
            });
        }
    });
    void isBook;
});

// ================= KRAKEN v2 =================
const krFmt = (v, d) => v.toFixed(d).replace('.', '').replace(/^0+/, '');
function krSum(bids, asks) {
    let s = '';
    for (const [p, q] of asks.slice(0, 10)) s += krFmt(p, 1) + krFmt(q, 8);
    for (const [p, q] of bids.slice(0, 10)) s += krFmt(p, 1) + krFmt(q, 8);
    return crc32(s);
}
let krTradeConns = 0;
server(BASE + 3, 'kraken', (ws) => {
    let silent = false;
    ws.on('message', (d) => {
        const m = JSON.parse(d.toString());
        if (m.method === 'ping') { if (!silent) send(ws, { method: 'pong', time_in: new Date().toISOString(), time_out: new Date().toISOString() }); return; }
        send(ws, { method: 'subscribe', result: { channel: m.params.channel, symbol: 'BTC/USD' }, success: true, time_in: new Date().toISOString(), time_out: new Date().toISOString() });
        every(ws, 1000, () => { if (!silent) send(ws, { channel: 'heartbeat' }); });
        if (m.params.channel === 'trade') {
            krTradeConns++;
            const myN = krTradeConns;
            // FALLO: a los 15s la 1ª conexión de trades queda muda (socket abierto, sin mensajes)
            if (myN === 1) setTimeout(() => { silent = true; log('kraken: trade feed SILENT'); }, 15000);
            every(ws, 40, () => {
                if (silent) return;
                const t = MK.kraken.trade();
                send(ws, { channel: 'trade', type: 'update', data: [{ symbol: 'BTC/USD', side: t.buy ? 'buy' : 'sell', price: t.price, qty: t.qty, ord_type: 'market', trade_id: 1, timestamp: new Date(Date.now() - 5).toISOString().replace('Z', '123Z') }] });
            });
        }
        if (m.params.channel === 'book') {
            let prevB = sorted(MK.kraken.bids, true).slice(0, 10), prevA = sorted(MK.kraken.asks, false).slice(0, 10);
            send(ws, { channel: 'book', type: 'snapshot', data: [{ symbol: 'BTC/USD', bids: prevB.map(([price, qty]) => ({ price, qty })), asks: prevA.map(([price, qty]) => ({ price, qty })), checksum: krSum(prevB, prevA) }] });
            every(ws, 15, () => {
                MK.kraken.mutate();
                const nb = sorted(MK.kraken.bids, true).slice(0, 10), na = sorted(MK.kraken.asks, false).slice(0, 10);
                const diff = (prev, next, full) => {
                    const out = [];
                    const nm = new Map(next);
                    for (const [p, q] of next) { const pq = prev.find((l) => l[0] === p); if (!pq || pq[1] !== q) out.push({ price: p, qty: q }); }
                    for (const [p] of prev) if (!nm.has(p) && !full.has(p)) out.push({ price: p, qty: 0 }); // borrado real
                    return out;
                };
                const bu = diff(prevB, nb, MK.kraken.bids), au = diff(prevA, na, MK.kraken.asks);
                prevB = nb; prevA = na;
                if (!bu.length && !au.length) return;
                send(ws, { channel: 'book', type: 'update', data: [{ symbol: 'BTC/USD', bids: bu, asks: au, checksum: krSum(nb, na), timestamp: new Date(Date.now() - 3).toISOString().replace('Z', '321Z') }] });
            });
        }
    });
});

// ================= OKX =================
function okSum(bids, asks) {
    const parts = [];
    for (let i = 0; i < 25; i++) {
        if (i < bids.length) parts.push(bids[i][0] + ':' + bids[i][1]);
        if (i < asks.length) parts.push(asks[i][0] + ':' + asks[i][1]);
    }
    return crc32(parts.join(':')) | 0;
}
let okBookConns = 0;
server(BASE + 4, 'okx', (ws, url) => {
    ws.on('message', (d) => {
        const s = d.toString();
        if (s === 'ping') { send(ws, 'pong'); return; }
        const m = JSON.parse(s);
        for (const a of m.args) send(ws, { event: 'subscribe', arg: a, connId: 'x' });
        if (url.includes('business')) {
            let id = 1;
            every(ws, 12, () => {
                const t = MK.okx.trade();
                send(ws, { arg: { channel: 'trades-all', instId: 'BTC-USDT' }, data: [{ instId: 'BTC-USDT', tradeId: String(id++), px: t.price.toFixed(1), sz: t.qty.toFixed(8), side: t.buy ? 'buy' : 'sell', ts: String(Date.now() - 4), source: '0' }] });
            });
            return;
        }
        okBookConns++;
        const myN = okBookConns;
        let seq = 100, gapDone = false;
        const lv = (l) => [l[0].toFixed(1), l[1].toFixed(8), '0', '1'];
        every(ws, 10, () => {
            const b = MK.okx.bestBid(), a = MK.okx.bestAsk();
            send(ws, { arg: { channel: 'bbo-tbt', instId: 'BTC-USDT' }, data: [{ asks: [lv(a)], bids: [lv(b)], ts: String(Date.now()), seqId: seq }] });
        });
        const snapB = sorted(MK.okx.bids, true).map(lv), snapA = sorted(MK.okx.asks, false).map(lv);
        send(ws, { arg: { channel: 'books', instId: 'BTC-USDT' }, action: 'snapshot', data: [{ asks: snapA, bids: snapB, ts: String(Date.now()), checksum: okSum(snapB, snapA), prevSeqId: -1, seqId: seq }] });
        every(ws, 100, () => {
            const ch = [];
            for (let i = 0; i < 4; i++) ch.push(...MK.okx.mutate());
            const bu = [], au = [];
            for (const [side, p, q] of ch) (side === 'bid' ? bu : au).push([p.toFixed(1), q === 0 ? '0' : q.toFixed(8), '0', q === 0 ? '0' : '1']);
            let prev = seq;
            // FALLO: a los 12s la 1ª conexión de libro "pierde" 5 mensajes (prevSeqId no encadena)
            if (myN === 1 && !gapDone && el() > 12000) { gapDone = true; prev = seq + 5; log('okx: SEQ GAP'); }
            seq = prev + 1;
            const fb = sorted(MK.okx.bids, true).map(lv), fa = sorted(MK.okx.asks, false).map(lv);
            send(ws, { arg: { channel: 'books', instId: 'BTC-USDT' }, action: 'update', data: [{ asks: au, bids: bu, ts: String(Date.now()), checksum: okSum(fb, fa), prevSeqId: prev, seqId: seq }] });
        });
    });
});

log('mock exchanges listening on', BASE + 1, '..', BASE + 4);
