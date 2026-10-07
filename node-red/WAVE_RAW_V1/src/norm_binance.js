// =====================================================================
// BINANCE spot · BTCUSDT
//  trade : <sym>@trade        -> 1 trade por mensaje (máxima granularidad, no aggTrade)
//          m = "is buyer the market maker": m=true => comprador pasivo => agresor SELL
//          m=false => agresor BUY. exchange_timestamp = T (trade time, ms)
//  book  : <sym>@bookTicker   -> BBO en tiempo real (sin timestamp en spot), id 'u'
//          <sym>@depth10@100ms-> snapshot parcial top10 cada 100ms (no requiere
//          snapshot REST + deltas: cada mensaje es completo). id 'lastUpdateId'
//  Ambos ids pertenecen a la secuencia de updates del libro: si el depth es más
//  nuevo que el último bookTicker, su top reemplaza al BBO.
// =====================================================================
const BN = {
    lastU: -1,          // último 'u' de bookTicker aplicado
    depth: null,        // última profundidad { b5, a5, b10, a10 }
    depthId: -1,
    stale: 0            // bookTickers descartados por llegar fuera de orden
};

function onConnReset(c) {
    if (c.id === 'book') {
        BN.lastU = -1; BN.depth = null; BN.depthId = -1;
        N.bbo.bid = N.bbo.bq = N.bbo.ask = N.bbo.aq = null;
    }
}

function extraStats() { return { bookticker_out_of_order: BN.stale }; }

function sumLv(arr, k) {
    if (!Array.isArray(arr) || arr.length < k) return null;
    let s = 0;
    for (let i = 0; i < k; i++) s += +arr[i][1];
    return s;
}

function handleFrame(c, txt, t) {
    let m;
    try { m = JSON.parse(txt); } catch (e) { c.invalid++; return; }
    if (!m || typeof m !== 'object') { c.invalid++; return; }
    const d = m.data, s = m.stream;
    if (!d || typeof s !== 'string') {
        if ('result' in m || 'id' in m) { c.control++; return; }
        c.invalid++;
        return;
    }
    if (s.endsWith('@trade')) {
        const p = num(d.p), q = num(d.q);
        if (!(p > 0) || !(q >= 0)) { c.invalid++; return; }
        const side = d.m === true ? 'SELL' : (d.m === false ? 'BUY' : null);
        emitEvents(c, t, 'trade', [makeTrade(p, q, side, num(d.T), t, { instrument: d.s, trade_id: d.t })]);
        return;
    }
    if (s.endsWith('@bookTicker')) {
        const u = num(d.u);
        if (u !== null && u <= BN.lastU) { BN.stale++; c.dropped++; return; }
        const bid = num(d.b), bq = num(d.B), ask = num(d.a), aq = num(d.A);
        if (bid === null || ask === null) { c.invalid++; return; }
        BN.lastU = u === null ? BN.lastU : u;
        N.bbo.bid = bid; N.bbo.bq = bq; N.bbo.ask = ask; N.bbo.aq = aq;
        emitEvents(c, t, 'book', [makeBook('bbo', BN.depth, null, t)]);
        return;
    }
    if (s.indexOf('@depth') > 0) {
        const bids = d.bids, asks = d.asks;
        if (!Array.isArray(bids) || !Array.isArray(asks) || !bids.length || !asks.length) { c.invalid++; return; }
        const id = num(d.lastUpdateId);
        if (id !== null && id <= BN.depthId) { c.dropped++; return; }
        BN.depthId = id === null ? BN.depthId : id;
        BN.depth = { b5: sumLv(bids, 5), a5: sumLv(asks, 5), b10: sumLv(bids, 10), a10: sumLv(asks, 10) };
        if (id !== null && id > BN.lastU) {
            N.bbo.bid = +bids[0][0]; N.bbo.bq = +bids[0][1];
            N.bbo.ask = +asks[0][0]; N.bbo.aq = +asks[0][1];
        }
        emitEvents(c, t, 'book', [makeBook('depth', BN.depth, null, t)]);
        return;
    }
    c.invalid++;
}
