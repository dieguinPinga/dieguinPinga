// =====================================================================
// OKX v5 · BTC-USDT
//  trade : canal 'trades-all' en el endpoint BUSINESS (1 trade por update; el
//          canal 'trades' público agrega fills del mismo taker). 'side' = lado
//          del taker (agresor). exchange_timestamp = ts (ms).
//  book  : endpoint PUBLIC
//          'bbo-tbt' -> top-1 tick-by-tick (cada ~10ms si cambia), con ts.
//          'books'   -> 400 niveles: action 'snapshot' y luego 'update' con size
//                       ABSOLUTO ('0' => borrar). Continuidad: update.prevSeqId
//                       debe == seqId anterior; si no => resync.
//                       Checksum CRC32 (top25 intercalado bid:ask, strings
//                       originales, int32 con signo). Desde 2026-06 OKX lo envía
//                       en 0 (deprecado): 0 => no se verifica; si llega != 0 y
//                       nunca validó, se desactiva solo tras 3 fallos.
//          El BBO se toma del mensaje más reciente (por ts) entre bbo-tbt y books.
//  ping  : el adaptador envía 'ping' de texto; 'pong' se cuenta como control.
// =====================================================================
const OK = { book: new Book(), lastSeq: null, bboTs: -1, csumEnabled: true, csumOkTotal: 0, csumFailTotal: 0, csumZero: 0 };

function onConnReset(c) {
    if (c.id === 'book') {
        OK.book.clear();
        OK.lastSeq = null;
        OK.bboTs = -1;
        N.bbo.bid = N.bbo.bq = N.bbo.ask = N.bbo.aq = null;
    }
}

function extraStats() {
    return { book_levels_bid: OK.book.bids.size(), book_levels_ask: OK.book.asks.size(),
        checksum_enabled: OK.csumEnabled, checksum_ok_total: OK.csumOkTotal, checksum_fail_total: OK.csumFailTotal, checksum_zero: OK.csumZero };
}

function okChecksum(book) {
    const parts = [];
    const b = book.bids, a = book.asks;
    for (let k = 0; k < 25; k++) {
        if (k < b.size()) { const i = b.idx(k); parts.push(b.rp[i] + ':' + b.rq[i]); }
        if (k < a.size()) { const i = a.idx(k); parts.push(a.rp[i] + ':' + a.rq[i]); }
    }
    return crc32(parts.join(':')) | 0; // int32 con signo
}

function okLevels(arr) {
    const out = [];
    for (const l of (arr || [])) out.push([+l[0], +l[1], l[0], l[1]]);
    return out;
}

function handleFrame(c, txt, t) {
    if (txt === 'pong') { c.control++; return; }
    let m;
    try { m = JSON.parse(txt); } catch (e) { c.invalid++; return; }
    if (!m || typeof m !== 'object') { c.invalid++; return; }
    if (m.event) {
        if (m.event === 'error') { c.invalid++; warnRL('err', 'error ' + m.code + ': ' + m.msg); }
        else c.control++;
        return;
    }
    const ch = m.arg && m.arg.channel;
    const data = m.data;
    if (!ch || !Array.isArray(data)) { c.invalid++; return; }

    if (ch === 'trades-all' || ch === 'trades') {
        const evs = [];
        for (const d of data) {
            const p = num(d.px), q = num(d.sz);
            if (!(p > 0) || !(q >= 0)) { c.invalid++; continue; }
            const side = d.side === 'buy' ? 'BUY' : (d.side === 'sell' ? 'SELL' : null);
            evs.push(makeTrade(p, q, side, num(d.ts), t, { instrument: d.instId, trade_id: d.tradeId }));
        }
        if (evs.length) emitEvents(c, t, 'trade', evs);
        return;
    }

    if (ch === 'bbo-tbt') {
        const d = data[0];
        if (!d || !d.bids || !d.asks || !d.bids[0] || !d.asks[0]) { c.invalid++; return; }
        const ts = num(d.ts);
        if (ts !== null && ts < OK.bboTs) { c.dropped++; return; }
        if (ts !== null) OK.bboTs = ts;
        N.bbo.bid = +d.bids[0][0]; N.bbo.bq = +d.bids[0][1];
        N.bbo.ask = +d.asks[0][0]; N.bbo.aq = +d.asks[0][1];
        emitEvents(c, t, 'book', [makeBook('bbo', OK.book.ready ? depthFromBook(OK.book) : null, ts, t)]);
        return;
    }

    if (ch === 'books') {
        if (c.awaitingResync) { c.dropped++; return; }
        const d = data[0];
        if (!d) { c.invalid++; return; }
        const book = OK.book;
        const seq = num(d.seqId), prev = num(d.prevSeqId);
        if (m.action === 'snapshot') {
            book.bids.load(okLevels(d.bids));
            book.asks.load(okLevels(d.asks));
            book.ready = true;
        } else {
            if (!book.ready) { c.dropped++; return; }
            if (prev !== null && OK.lastSeq !== null && prev !== OK.lastSeq) {
                c.gaps++;
                requestResync(c, 'seq gap prev=' + prev + ' last=' + OK.lastSeq);
                return;
            }
            for (const l of (d.bids || [])) book.bids.set(+l[0], +l[1], l[0], l[1]);
            for (const l of (d.asks || [])) book.asks.set(+l[0], +l[1], l[0], l[1]);
        }
        if (seq !== null) OK.lastSeq = seq;
        const cs = num(d.checksum);
        if (cs === 0) OK.csumZero++;
        else if (cs !== null && OK.csumEnabled) {
            if (okChecksum(book) === cs) { c.csum_ok++; OK.csumOkTotal++; }
            else {
                c.csum_fail++; OK.csumFailTotal++;
                if (OK.csumOkTotal > 0) { requestResync(c, 'checksum mismatch'); return; }
                if (OK.csumFailTotal >= 3) {
                    OK.csumEnabled = false;
                    warnRL('csum', 'checksum nunca validó: verificación desactivada (se usa continuidad seqId).');
                }
            }
        }
        if (checkCross(c, book)) return;
        const ts = num(d.ts);
        if (ts === null || ts >= OK.bboTs) {
            bboFromBook(book);
            if (ts !== null) OK.bboTs = ts;
        }
        emitEvents(c, t, 'book', [makeBook('depth', depthFromBook(book), ts, t)]);
        return;
    }
    c.invalid++;
}
