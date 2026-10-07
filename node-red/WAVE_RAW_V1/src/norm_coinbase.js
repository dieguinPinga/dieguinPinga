// =====================================================================
// COINBASE Advanced Trade · BTC-USD   (wss://advanced-trade-ws.coinbase.com)
//  trade : canal market_trades. El primer evento 'snapshot' trae trades HISTÓRICOS
//          (se ignoran como control). Los 'update' son trades nuevos (en lotes).
//          SEMÁNTICA DE LADO: la doc de Advanced Trade define 'side' como el lado
//          del MAKER => agresor = lado opuesto. Configurable abajo y verificado en
//          vivo con side_check (precio del trade vs BBO previo).
//          exchange_timestamp = trade.time (ISO con µs/ns)
//  book  : canal level2 (mensajes channel 'l2_data'): primer evento 'snapshot' con
//          el libro COMPLETO y luego 'update' con new_quantity ABSOLUTA por nivel
//          (0 => borrar nivel). Se mantiene el libro completo en memoria (escalera
//          ordenada) y se emite sólo BBO + top5/top10.
//          Huecos de sequence_num o libro cruzado persistente => resync.
//  heartbeats: mantiene viva la conexión en momentos sin actividad.
// =====================================================================
const CB_SIDE_FIELD_IS_MAKER = true;

const CB = { book: new Book(), snapshots: 0, histTradesSkipped: 0, seqResync: true, gapTimes: [] };

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
    if (c.id === 'book') {
        CB.book.clear();
        N.bbo.bid = N.bbo.bq = N.bbo.ask = N.bbo.aq = null;
    }
}

function extraStats() {
    return { book_levels_bid: CB.book.bids.size(), book_levels_ask: CB.book.asks.size(),
        snapshots: CB.snapshots, seq_resync_enabled: CB.seqResync, hist_trades_skipped: CB.histTradesSkipped, side_field_is_maker: CB_SIDE_FIELD_IS_MAKER };
}

function cbAggressor(raw) {
    if (raw === 'BUY') return CB_SIDE_FIELD_IS_MAKER ? 'SELL' : 'BUY';
    if (raw === 'SELL') return CB_SIDE_FIELD_IS_MAKER ? 'BUY' : 'SELL';
    return null;
}

function handleFrame(c, txt, t) {
    let m;
    try { m = JSON.parse(txt); } catch (e) { c.invalid++; return; }
    if (!m || typeof m !== 'object') { c.invalid++; return; }
    if (m.type === 'error') { c.invalid++; warnRL('err', 'error: ' + (m.message || txt.slice(0, 200))); return; }
    const ch = m.channel;
    const seqOk = seqCheck(c, ch, m.sequence_num);
    if (!seqOk && c.id === 'book') cbGap(c); // en la conexión del libro cualquier hueco invalida el libro
    if (ch === 'heartbeats' || ch === 'subscriptions') { c.control++; return; }

    if (ch === 'market_trades') {
        const evs = [];
        for (const e of (m.events || [])) {
            if (e.type !== 'update') { CB.histTradesSkipped += (e.trades || []).length; continue; }
            for (const tr of (e.trades || [])) {
                if (tr.product_id && tr.product_id !== PRODUCT) continue;
                const p = num(tr.price), q = num(tr.size);
                if (!(p > 0) || !(q >= 0)) { c.invalid++; continue; }
                evs.push(makeTrade(p, q, cbAggressor(tr.side), parseIsoMs(tr.time), t, { instrument: tr.product_id, trade_id: tr.trade_id, raw_side: tr.side }));
            }
        }
        if (evs.length) emitEvents(c, t, 'trade', evs);
        else c.control++;
        return;
    }

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
