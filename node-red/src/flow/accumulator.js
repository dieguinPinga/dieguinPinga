// FLOW · Acumulador de agresión Binance (BTCUSDT + ZECUSDT).
// RAM privada de este nodo Function (su propio contexto vm): no flow/global context, no disco.
// Cada trade individual entra al acumulador; el bucket de 1 s es una agregación posterior.
// Salidas: 1 → plantilla BTC, 2 → plantilla ZEC, 3 → buckets terminados (Debug).
const WINDOW = 120000;   // ventana visible y de retención de precios
const RING = 150;        // buckets de 1 s conservados (120 visibles + margen)
const GRACE = 750;       // espera tras fin de segundo antes de cerrar un bucket sin trades nuevos
const WS_NODES = __WS_NODES__;
const OUT = { BTCUSDT: 0, ZECUSDT: 1 };
const now = Date.now();
if (!globalThis.__bzFlow) globalThis.__bzFlow = {};
const all = globalThis.__bzFlow;

function state(symbol) {
    if (!all[symbol]) all[symbol] = {
        symbol, epoch: now.toString(36) + Math.random().toString(36).slice(2),
        // Precio por trade: [seq, tradeTimeMs, price, segment], seq contiguo.
        rows: [], head: 0, seq: 0, segment: 0,
        lastId: null, gaps: 0, invalid: 0, latest: null, lastRx: 0, lag: null,
        ws: 'Esperando conexión', connected: false, connectedSince: 0,
        open: new Map(), closedUpTo: null, ring: [], ver: 0, cvd: 0, lastClose: null,
        trades: 0, late: 0, tooLate: 0
    };
    return all[symbol];
}

function prune(st) {
    while (st.head < st.rows.length && st.rows[st.head][1] < now - WINDOW) st.head++;
    if (st.head > 4096 && st.head * 2 > st.rows.length) { st.rows = st.rows.slice(st.head); st.head = 0; }
}

function newAcc(ts) {
    return {
        ts, buy_usd: 0, sell_usd: 0, buy_qty: 0, sell_qty: 0, buy_trades: 0, sell_trades: 0,
        open: null, openT: Infinity, openId: Infinity, close: null, closeT: -Infinity, closeId: -Infinity,
        firstId: null, lastId: null, carry: null, late: 0, connected: true
    };
}

// m = false → comprador taker → BUY agresivo. m = true → vendedor taker → SELL agresivo.
function apply(a, tr) {
    const price = Number(tr.p), qty = Number(tr.q), usd = price * qty;
    if (tr.m) { a.sell_usd += usd; a.sell_qty += qty; a.sell_trades++; }
    else { a.buy_usd += usd; a.buy_qty += qty; a.buy_trades++; }
    if (tr.T < a.openT || (tr.T === a.openT && tr.t < a.openId)) { a.openT = tr.T; a.openId = tr.t; a.open = price; }
    if (tr.T > a.closeT || (tr.T === a.closeT && tr.t > a.closeId)) { a.closeT = tr.T; a.closeId = tr.t; a.close = price; }
    a.firstId = a.firstId === null ? tr.t : Math.min(a.firstId, tr.t);
    a.lastId = a.lastId === null ? tr.t : Math.max(a.lastId, tr.t);
}

function build(symbol, a, cvd) {
    const n = a.buy_trades + a.sell_trades, total = a.buy_usd + a.sell_usd;
    const open = n ? a.open : a.carry, close = n ? a.close : a.carry;
    return {
        symbol, timestamp: a.ts, open_price: open, close_price: close,
        price_change: open === null ? null : Number((close - open).toFixed(8)),
        buy_usd: a.buy_usd, sell_usd: a.sell_usd, delta_usd: a.buy_usd - a.sell_usd, total_usd: total,
        buy_qty: a.buy_qty, sell_qty: a.sell_qty,
        buy_trades: a.buy_trades, sell_trades: a.sell_trades, total_trades: n,
        buy_percent: total ? a.buy_usd / total * 100 : null,
        sell_percent: total ? a.sell_usd / total * 100 : null,
        cvd_usd: cvd, first_trade_id: a.firstId, last_trade_id: a.lastId,
        price_carried: !n && open !== null, late_trades: a.late, revised: a.late > 0,
        ws_connected: a.connected, ver: 0
    };
}

const debugOut = [];
function emit(b) { debugOut.push({ topic: 'flow:bucket ' + b.symbol, payload: { ...b } }); }

// Cierra todos los segundos S <= last. Los segundos sin trades producen un bucket vacío.
function finalizeUpTo(st, last) {
    if (st.closedUpTo === null || last <= st.closedUpTo) return;
    // Tras una desconexión larga, saltar segundos vacíos que ya no entran en la ventana.
    const minOpen = st.open.size ? Math.min(...st.open.keys()) : Infinity;
    const jump = Math.min(last - RING * 1000, minOpen - 1000);
    if (jump > st.closedUpTo) { st.closedUpTo = jump; st.ring = []; }
    for (let S = st.closedUpTo + 1000; S <= last; S += 1000) {
        const a = st.open.get(S) || newAcc(S);
        st.open.delete(S);
        a.connected = st.connected && st.connectedSince <= S + (st.lag || 0);
        if (a.buy_trades + a.sell_trades) st.lastClose = a.close; else a.carry = st.lastClose;
        const b = build(st.symbol, a, 0);
        st.cvd += b.delta_usd;
        b.cvd_usd = st.cvd;
        b.ver = ++st.ver;
        st.ring.push({ a, b });
        emit(b);
    }
    if (st.ring.length > RING) st.ring.splice(0, st.ring.length - RING);
    st.closedUpTo = last;
}

// Trade de un segundo ya cerrado: se suma a ese bucket (revisado), nunca se descarta.
function revise(st, S, tr) {
    const i = st.ring.length ? (S - st.ring[0].a.ts) / 1000 : -1;
    if (!(i >= 0 && i < st.ring.length && st.ring[i].a.ts === S)) { st.tooLate++; return; }
    const e = st.ring[i], before = e.b.delta_usd;
    apply(e.a, tr);
    e.a.late++;
    const dd = e.a.buy_usd - e.a.sell_usd - before;
    const nb = build(st.symbol, e.a, e.b.cvd_usd + dd);
    nb.ver = ++st.ver;
    e.b = nb;
    for (let j = i + 1; j < st.ring.length; j++) { st.ring[j].b.cvd_usd += dd; st.ring[j].b.ver = ++st.ver; }
    st.cvd += dd;
    st.late++;
    emit(nb);
}

function metrics(st) {
    const r = st.ring, last = r.length ? r[r.length - 1].b : null;
    let buy = 0, sell = 0, n = 0;
    for (let j = Math.max(0, r.length - 10); j < r.length; j++) { buy += r[j].b.buy_usd; sell += r[j].b.sell_usd; n += r[j].b.total_trades; }
    return {
        last: last && { timestamp: last.timestamp, buy_usd: last.buy_usd, sell_usd: last.sell_usd, delta_usd: last.delta_usd, total_trades: last.total_trades, price_change: last.price_change },
        ten: { buy_usd: buy, sell_usd: sell, delta_usd: buy - sell, total_trades: n, seconds: Math.min(10, r.length) }
    };
}

function finish(reply, symbol) {
    const out = [null, null, debugOut.length ? debugOut : null];
    if (reply) out[OUT[symbol]] = reply;
    return out;
}

// Estado de conexión de los websocket-in de esta solapa.
if (msg.status) {
    const symbol = WS_NODES[msg.status.source?.id];
    if (!symbol) return null;
    const st = state(symbol), up = msg.status.event === 'connect' || msg.status.fill === 'green';
    if (up && !st.connected) st.connectedSince = now;
    if (!up) st.segment++;
    st.connected = up;
    st.ws = up ? 'Conectado' : 'Desconectado / reconectando';
    return null;
}

// Reloj: cierra segundos aunque no lleguen trades nuevos.
if (msg.topic === 'flow:tick') {
    for (const symbol of Object.keys(OUT)) {
        const st = state(symbol);
        prune(st);
        finalizeUpTo(st, Math.floor((now - (st.lag || 0) - GRACE - 1000) / 1000) * 1000);
    }
    return finish(null);
}

// Pedido de un navegador: precio incremental + buckets nuevos o revisados, sólo a ese cliente.
if (msg.topic === 'flow:pull') {
    const req = msg.payload || {};
    if (!msg._client?.socketId || !(req.symbol in OUT)) return null;
    const st = state(req.symbol);
    prune(st);
    const first = st.head < st.rows.length ? st.rows[st.head][0] : st.seq + 1;
    const reset = req.epoch !== st.epoch || !Number.isSafeInteger(req.after) || req.after > st.seq ||
        !Number.isSafeInteger(req.bver) || req.bver > st.ver;
    const after = reset ? first - 1 : Math.max(req.after, first - 1);
    const stop = Math.min(st.seq, after + 3000);   // bloque de transporte, no un muestreo
    const start = st.head + (after + 1 - first);
    const bver = reset ? 0 : req.bver;
    return finish({
        _client: msg._client, topic: 'flow:data',
        payload: {
            symbol: st.symbol, token: req.token, epoch: st.epoch, reset, now,
            after: stop, more: stop < st.seq, expiredBeforeRead: !reset && req.after < first - 1,
            rows: st.rows.slice(start, start + (stop - after)),
            bver: st.ver, buckets: st.ring.filter(e => e.b.ver > bver).map(e => e.b),
            price: st.latest, metrics: metrics(st), ws: st.ws, lastRx: st.lastRx,
            trades: st.trades, gaps: st.gaps, invalid: st.invalid, late: st.late, tooLate: st.tooLate,
            lag: st.lag, open: st.open.size
        }
    }, req.symbol);
}

// Trades crudos del WebSocket de Binance (conexión compartida con la solapa LIVE).
if (msg._session?.type !== 'websocket') return null;
let tr;
try {
    tr = Buffer.isBuffer(msg.payload) ? JSON.parse(msg.payload.toString()) :
        typeof msg.payload === 'string' ? JSON.parse(msg.payload) : msg.payload;
} catch (_) { return null; }
const symbol = tr?.s;
if (!(symbol in OUT)) return null;
const st = state(symbol);
if (tr.e === 'serverShutdown') { st.ws = 'Binance anuncia reinicio de conexión'; st.segment++; return null; }
if (tr.e !== 'trade' || !Number.isSafeInteger(tr.T) || !Number.isSafeInteger(tr.t) ||
    !(Number(tr.p) > 0) || !Number.isFinite(Number(tr.p)) ||
    !(Number(tr.q) > 0) || !Number.isFinite(Number(tr.q)) || typeof tr.m !== 'boolean') {
    st.invalid++;
    return null;
}
st.trades++;
st.lastRx = now;
if (!st.connected) { st.connected = true; st.connectedSince = now; }
st.ws = 'Conectado';
const d = Math.max(-10000, Math.min(10000, now - tr.T));
st.lag = st.lag === null ? d : st.lag + 0.05 * (d - st.lag);
if (st.lastId !== null && tr.t > st.lastId + 1) { st.gaps++; st.segment++; }
st.lastId = st.lastId === null ? tr.t : Math.max(st.lastId, tr.t);
st.latest = Number(tr.p);
st.rows.push([++st.seq, tr.T, Number(tr.p), st.segment]);

const S = Math.floor(tr.T / 1000) * 1000;
if (st.closedUpTo === null) st.closedUpTo = S - 1000;
if (S <= st.closedUpTo) revise(st, S, tr);
else {
    if (!st.open.has(S)) st.open.set(S, newAcc(S));
    apply(st.open.get(S), tr);
    // Stream ordenado: el primer trade de un segundo nuevo cierra los anteriores.
    finalizeUpTo(st, S - 1000);
}
return debugOut.length ? finish(null) : null;
