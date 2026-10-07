// =====================================================================
// COINBASE · BTC-USD
//  trade : Coinbase EXCHANGE feed (wss://ws-feed.exchange.coinbase.com), canal
//          'matches': UN mensaje {type:'match'} por trade, en tiempo real (sin el
//          batching de ~250 ms de Advanced Trade market_trades, que ya NO se usa).
//          SEMÁNTICA DE LADO: en Coinbase Exchange 'side' es el lado de la orden
//          MAKER => agresor/taker = lado opuesto ('sell' => BUY, 'buy' => SELL).
//          Verificado en vivo con side_check (precio del trade vs BBO previo).
//          El primer 'last_match' tras suscribir es histórico => se ignora.
//          Continuidad: trade_id consecutivo por producto; el 'heartbeat' (1/s)
//          trae last_trade_id => trades perdidos se cuentan como gaps.
//          exchange_timestamp = time (ISO con µs).
//  book  : Advanced Trade (wss://advanced-trade-ws.coinbase.com) canal level2
//          (mensajes channel 'l2_data'): primer evento 'snapshot' con el libro
//          COMPLETO y luego 'update' con new_quantity ABSOLUTA por nivel
//          (0 => borrar nivel). Se mantiene el libro completo en memoria (escalera
//          ordenada) y se emite sólo BBO + top5/top10.
//          Huecos de sequence_num o libro cruzado persistente => resync.
//          heartbeats: mantiene viva la conexión en momentos sin actividad.
// =====================================================================
const CB_SIDE_FIELD_IS_MAKER = true;

const CB = { book: new Book(), snapshots: 0, histTradesSkipped: 0, seqResync: true, gapTimes: [], lastTradeId: null, missedTrades: 0 };

// Salvaguarda: si los "huecos" de sequence_num fueran frecuentísimos (semántica distinta
// a la esperada), no entrar en un bucle de resync: se cuentan y se desactiva el resync por gap.
function cbGap(c) {
    const now = Date.now();
    CB.gapTimes.push(now);
    while (CB.gapTimes.length && now - CB.gapTimes[0] > 120000) CB.gapTimes.shift();
    if (CB.seqResync && CB.gapTimes.length > 5) {
        CB.seqResync = false;
        warnRL('seq', '>5 huecos de sequence_num en 120s: resync por gap desactivado (sigue la protección por libro cruzado)');
    }
    if (CB.seqResync) requestResync(c, 'sequence gap');
}

function onConnReset(c) {
    if (c.id === 'trade') CB.lastTradeId = null; // tras reconectar, la continuidad de trade_id arranca de nuevo
    if (c.id === 'book') {
        CB.book.clear();
        N.bbo.bid = N.bbo.bq = N.bbo.ask = N.bbo.aq = null;
    }
}

function extraStats() {
    return { book_levels_bid: CB.book.bids.size(), book_levels_ask: CB.book.asks.size(),
        snapshots: CB.snapshots, seq_resync_enabled: CB.seqResync, hist_trades_skipped: CB.histTradesSkipped, missed_trades: CB.missedTrades, side_field_is_maker: CB_SIDE_FIELD_IS_MAKER };
}

function cbAggressor(raw) {
    if (raw === 'buy') return CB_SIDE_FIELD_IS_MAKER ? 'SELL' : 'BUY';
    if (raw === 'sell') return CB_SIDE_FIELD_IS_MAKER ? 'BUY' : 'SELL';
    return null;
}

// ---------- TRADE: Coinbase Exchange 'matches' (1 trade por frame) ----------
function cbTradeId(m) {
    const id = num(m.trade_id);
    if (id === null) return;
    if (CB.lastTradeId !== null && id > CB.lastTradeId + 1) {
        CB.missedTrades += id - CB.lastTradeId - 1;
        N.conns.trade.gaps++;
    }
    if (CB.lastTradeId === null || id > CB.lastTradeId) CB.lastTradeId = id;
}

function handleTradeFrame(c, txt, t) {
    let m;
    try { m = JSON.parse(txt); } catch (e) { c.invalid++; return; }
    if (!m || typeof m !== 'object') { c.invalid++; return; }
    const ty = m.type;
    if (ty === 'match') {
        if (m.product_id && m.product_id !== PRODUCT) { c.control++; return; }
        const p = num(m.price), q = num(m.size);
        if (!(p > 0) || !(q >= 0)) { c.invalid++; return; }
        cbTradeId(m);
        emitEvents(c, t, 'trade', [makeTrade(p, q, cbAggressor(m.side), parseIsoMs(m.time), t,
            { instrument: m.product_id, trade_id: m.trade_id, raw_side: m.side, sequence: m.sequence })]);
        return;
    }
    if (ty === 'last_match') { // histórico: sólo fija la referencia de trade_id
        CB.histTradesSkipped++;
        const id = num(m.trade_id);
        if (id !== null && CB.lastTradeId === null) CB.lastTradeId = id;
        c.control++;
        return;
    }
    if (ty === 'heartbeat') {
        const lt = num(m.last_trade_id);
        if (lt !== null && CB.lastTradeId !== null && lt > CB.lastTradeId) { // el feed ya emitió trades que no vimos
            CB.missedTrades += lt - CB.lastTradeId;
            c.gaps++;
            CB.lastTradeId = lt;
        }
        c.control++;
        return;
    }
    if (ty === 'subscriptions') { c.control++; return; }
    if (ty === 'error') { c.invalid++; warnRL('err-trade', 'error: ' + (m.message || '') + ' ' + (m.reason || '')); return; }
    c.invalid++;
}

// ---------- BOOK: Advanced Trade level2 ----------
function handleFrame(c, txt, t) {
    if (c.id === 'trade') { handleTradeFrame(c, txt, t); return; }
    let m;
    try { m = JSON.parse(txt); } catch (e) { c.invalid++; return; }
    if (!m || typeof m !== 'object') { c.invalid++; return; }
    if (m.type === 'error') { c.invalid++; warnRL('err', 'error: ' + (m.message || txt.slice(0, 200))); return; }
    const ch = m.channel;
    const seqOk = seqCheck(c, ch, m.sequence_num);
    if (!seqOk && c.id === 'book') cbGap(c); // en la conexión del libro cualquier hueco invalida el libro
    if (ch === 'heartbeats' || ch === 'subscriptions') { c.control++; return; }

    if (ch === 'l2_data') {
        if (c.awaitingResync) { c.dropped++; return; }
        const book = CB.book;
        let touched = false;
        for (const e of (m.events || [])) {
            if (e.product_id && e.product_id !== PRODUCT) continue;
            const ups = e.updates || [];
            if (e.type === 'snapshot') {
                const b = [], a = [];
                for (const u of ups) {
                    const lv = [+u.price_level, +u.new_quantity, null, null];
                    if (u.side === 'bid') b.push(lv); else if (u.side === 'offer' || u.side === 'ask') a.push(lv);
                }
                book.bids.load(b);
                book.asks.load(a);
                book.ready = true;
                CB.snapshots++;
                touched = true;
            } else if (e.type === 'update') {
                if (!book.ready) { c.dropped++; continue; }
                for (const u of ups) {
                    const p = +u.price_level, q = +u.new_quantity;
                    if (!isFinite(p) || !isFinite(q)) { c.invalid++; continue; }
                    if (u.side === 'bid') book.bids.set(p, q, null, null);
                    else if (u.side === 'offer' || u.side === 'ask') book.asks.set(p, q, null, null);
                }
                touched = true;
            }
        }
        if (!touched || !book.ready) { c.control++; return; }
        if (checkCross(c, book)) return;
        bboFromBook(book);
        emitEvents(c, t, 'book', [makeBook('l2', depthFromBook(book), parseIsoMs(m.timestamp), t)]);
        return;
    }
    c.invalid++;
}
