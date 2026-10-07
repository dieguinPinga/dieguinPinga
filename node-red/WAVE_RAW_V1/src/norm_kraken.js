// =====================================================================
// KRAKEN WebSocket v2 · BTC/USD   (wss://ws.kraken.com/v2)
//  trade : canal 'trade' (snapshot:false). 'side' = lado de la orden TAKER
//          (agresor). exchange_timestamp = timestamp RFC3339 (µs).
//  book  : canal 'book' depth=10. 'snapshot' y luego 'update' con qty ABSOLUTA
//          (qty 0 => borrar). Tras cada mensaje se TRUNCA el libro a la
//          profundidad suscrita (Kraken no envía borrados de niveles que salen
//          del rango). Checksum CRC32 del top10 (asks asc + bids desc, precio y
//          qty sin '.' ni ceros a la izquierda) formateados con la precisión del
//          instrumento. Checksum fallido => resync.
//          Si el checksum NUNCA validó (p.ej. precisión mal configurada) se
//          desactiva solo tras 3 fallos para no entrar en un bucle de resync.
// =====================================================================
const KR_DEPTH = 10;
const KR_PRICE_DEC = 1;   // BTC/USD: tick 0.1
const KR_QTY_DEC = 8;     // BTC/USD: lote 1e-8

const KR = { book: new Book(), csumEnabled: true, csumOkTotal: 0, csumFailTotal: 0 };

function onConnReset(c) {
    if (c.id === 'book') {
        KR.book.clear();
        N.bbo.bid = N.bbo.bq = N.bbo.ask = N.bbo.aq = null;
    }
}

function extraStats() { return { checksum_enabled: KR.csumEnabled, checksum_ok_total: KR.csumOkTotal, checksum_fail_total: KR.csumFailTotal }; }

function krFmt(v, dec) { return v.toFixed(dec).replace('.', '').replace(/^0+/, ''); }

function krChecksum(book) {
    let s = '';
    const a = book.asks, b = book.bids;
    for (let k = 0; k < 10 && k < a.size(); k++) { const i = a.idx(k); s += krFmt(a.px[i], KR_PRICE_DEC) + krFmt(a.qt[i], KR_QTY_DEC); }
    for (let k = 0; k < 10 && k < b.size(); k++) { const i = b.idx(k); s += krFmt(b.px[i], KR_PRICE_DEC) + krFmt(b.qt[i], KR_QTY_DEC); }
    return crc32(s);
}

function krLevels(arr) {
    const out = [];
    for (const l of (arr || [])) out.push([+l.price, +l.qty, null, null]);
    return out;
}

function handleFrame(c, txt, t) {
    let m;
    try { m = JSON.parse(txt); } catch (e) { c.invalid++; return; }
    if (!m || typeof m !== 'object') { c.invalid++; return; }
    if (m.method) {
        if (m.success === false) warnRL('sub', m.method + ' failed: ' + (m.error || ''));
        c.control++;
        return;
    }
    const ch = m.channel;
    if (ch === 'heartbeat' || ch === 'status') { c.control++; return; }

    if (ch === 'trade') {
        if (m.type !== 'update') { c.control++; return; }
        const evs = [];
        for (const d of (m.data || [])) {
            const p = num(d.price), q = num(d.qty);
            if (!(p > 0) || !(q >= 0)) { c.invalid++; continue; }
            const side = d.side === 'buy' ? 'BUY' : (d.side === 'sell' ? 'SELL' : null);
            evs.push(makeTrade(p, q, side, parseIsoMs(d.timestamp), t, { instrument: d.symbol, trade_id: d.trade_id, ord_type: d.ord_type }));
        }
        if (evs.length) emitEvents(c, t, 'trade', evs);
        return;
    }

    if (ch === 'book') {
        if (c.awaitingResync) { c.dropped++; return; }
        const book = KR.book;
        let exTs = null;
        for (const d of (m.data || [])) {
            if (d.symbol && d.symbol !== PRODUCT) continue;
            if (m.type === 'snapshot') {
                book.bids.load(krLevels(d.bids));
                book.asks.load(krLevels(d.asks));
                book.ready = true;
            } else {
                if (!book.ready) { c.dropped++; return; }
                for (const l of (d.bids || [])) book.bids.set(+l.price, +l.qty, null, null);
                for (const l of (d.asks || [])) book.asks.set(+l.price, +l.qty, null, null);
            }
            book.bids.truncate(KR_DEPTH);
            book.asks.truncate(KR_DEPTH);
            if (KR.csumEnabled && d.checksum !== undefined && d.checksum !== null) {
                if (krChecksum(book) === (Number(d.checksum) >>> 0)) { c.csum_ok++; KR.csumOkTotal++; }
                else {
                    c.csum_fail++; KR.csumFailTotal++;
                    if (KR.csumOkTotal > 0) { requestResync(c, 'checksum mismatch'); return; }
                    if (KR.csumFailTotal >= 3) {
                        KR.csumEnabled = false;
                        warnRL('csum', 'checksum nunca validó: revisar KR_PRICE_DEC/KR_QTY_DEC. Verificación desactivada.');
                    }
                }
            }
            const ts = parseIsoMs(d.timestamp);
            if (ts !== null) exTs = ts;
        }
        if (!book.ready) { c.control++; return; }
        if (checkCross(c, book)) return;
        bboFromBook(book);
        emitEvents(c, t, 'book', [makeBook(m.type === 'snapshot' ? 'snapshot' : 'l2', depthFromBook(book), exTs, t)]);
        return;
    }
    c.invalid++;
}
