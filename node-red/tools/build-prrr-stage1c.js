#!/usr/bin/env node
// Genera node-red/prrr-market-data-stage1c.json (flow importable en Node-RED).
// Etapa 1c = 1b + más fuentes reales para GMX/XMR (perpetuos, Bitget, Hyperliquid) + dashboard 2×2.
// Mismos IDs que las etapas anteriores: al importar elegir "Replace".
// Uso: node node-red/tools/build-prrr-stage1c.js
'use strict';
const fs = require('fs');
const path = require('path');

const TAB = 'prrr_tab_main';
const SF = 'prrr_sf_wsconn';
const nodes = [];
const add = (n) => { nodes.push(n); return n; };

// ---------------------------------------------------------------------------
// Exchanges. Para agregar uno: nueva entrada acá (url, subs, normalizador).
// ---------------------------------------------------------------------------
// Bitget WS v2 público (spot y USDT-M futures comparten URL). Un conector por tipo de mercado.
function BITGET(id, label, pairs, instType, mtype, insts) {
  const MAPJS = '{ ' + insts.map((s) => s + ": ['" + s.replace(/USDT$/, '') + "', 'USDT']").join(', ') + ' }';
  return {
    id, label, pairs, url: 'wss://ws.bitget.com/v2/ws/public',
    subs: insts.map((s) => ({ op: 'subscribe', args: [{ instType, channel: 'trade', instId: s }] })),
    pingMs: 25000, pingPayload: 'ping', staleMs: 60000, ignore: 'pong',
    norm: "// Bitget WS v2 canal \"trade\" (" + instType + "). side = lado del taker. Se ignora el snapshot inicial (trades previos a suscribir).\n" +
      "const MAP = " + MAPJS + ";\n" +
      "const EX = 'bitget', SRC = '" + id + "', MT = '" + mtype + "';\n" + String.raw`if (msg.payload === 'pong') return null;
let o;
try { o = JSON.parse(msg.payload); } catch (e) { return { topic: 'md_err', exchange: SRC, payload: 'JSON inválido: ' + String(msg.payload).slice(0, 120) }; }
if (!o) return null;
if (o.event === 'error') return { topic: 'md_err', exchange: SRC, payload: 'exchange: ' + o.code + ' ' + o.msg };
if (o.event || !o.arg || o.arg.channel !== 'trade' || o.action !== 'update' || !Array.isArray(o.data)) return null;
const m = MAP[o.arg.instId];
if (!m) return null;
const out = [];
for (const d of o.data) {
    const price = +d.price, qty = +d.size, ts = +d.ts;
    if (!(price > 0) || !(qty >= 0) || !(ts > 0)) { out.push({ topic: 'md_err', exchange: SRC, payload: 'trade inválido: ' + JSON.stringify(d).slice(0, 160) }); continue; }
    out.push({ topic: 'trade', payload: {
        symbol: m[0], quote: m[1], market: o.arg.instId, exchange: EX, source: SRC, market_type: MT, kind: 'trade',
        price: price, quantity: qty, side: d.side === 'buy' ? 'BUY' : (d.side === 'sell' ? 'SELL' : null),
        trade_id: d.tradeId,
        exchange_timestamp: ts, exchange_ts_raw: d.ts,
        local_receive_timestamp: msg.t_recv,
        raw: d
    } });
}
return out.length ? [out] : null;`,
  };
}

const EXCHANGES = [
  {
    id: 'binance', label: 'Binance', pairs: 'BTCUSDT, ZECUSDT, GMXUSDT',
    url: 'wss://stream.binance.com:9443/stream?streams=btcusdt@trade/zecusdt@trade/gmxusdt@trade&timeUnit=MICROSECOND',
    subs: [], pingMs: 0, pingPayload: '', staleMs: 30000, ignore: '',
    norm: String.raw`// Binance spot @trade (trades individuales, no aggTrade).
// timeUnit=MICROSECOND => T en microsegundos. m=true => el comprador es maker => agresor SELL.
const MAP = { BTCUSDT: ['BTC', 'USDT'], ZECUSDT: ['ZEC', 'USDT'], GMXUSDT: ['GMX', 'USDT'] };
const EX = 'binance';
let o;
try { o = JSON.parse(msg.payload); } catch (e) { return { topic: 'md_err', exchange: EX, payload: 'JSON inválido: ' + String(msg.payload).slice(0, 120) }; }
const d = o && o.data ? o.data : o;
if (!d || d.e !== 'trade') return null;            // respuestas de suscripción, etc.
const m = MAP[d.s];
if (!m) return null;
const price = +d.p, qty = +d.q, T = +d.T;
if (!(price > 0) || !(qty >= 0) || !(T > 0)) return { topic: 'md_err', exchange: EX, payload: 'trade inválido: ' + msg.payload.slice(0, 160) };
return { topic: 'trade', payload: {
    symbol: m[0], quote: m[1], market: d.s, exchange: EX, kind: 'trade',
    price: price, quantity: qty, side: d.m ? 'SELL' : 'BUY', trade_id: d.t,
    exchange_timestamp: T > 1e14 ? T / 1000 : T,    // ms epoch (con decimales si viene en µs)
    exchange_ts_raw: d.T,                            // valor original sin tocar
    local_receive_timestamp: msg.t_recv,
    raw: d
} };`,
  },
  {
    id: 'coinbase', label: 'Coinbase', pairs: 'BTC-USD, ZEC-USD',
    url: 'wss://ws-feed.exchange.coinbase.com',
    // un mensaje por producto: si un producto no existe no tumba al otro
    subs: [
      { type: 'subscribe', product_ids: ['BTC-USD'], channels: ['matches', 'heartbeat'] },
      { type: 'subscribe', product_ids: ['ZEC-USD'], channels: ['matches', 'heartbeat'] },
    ],
    pingMs: 0, pingPayload: '', staleMs: 30000, ignore: '',
    norm: String.raw`// Coinbase Exchange feed, canal "matches" (trades individuales, time con µs).
// "side" es el lado del MAKER => el agresor es el opuesto.
const MAP = { 'BTC-USD': ['BTC', 'USD'], 'ZEC-USD': ['ZEC', 'USD'] };
const EX = 'coinbase';
function isoToMs(s) {                 // ISO8601 con fracción de µs/ns -> ms epoch con decimales
    const ms = Date.parse(s);
    if (!isFinite(ms)) return NaN;
    const f = /\.(\d+)/.exec(s);
    return (f && f[1].length > 3) ? ms + Number('0.' + f[1].slice(3)) : ms;
}
let d;
try { d = JSON.parse(msg.payload); } catch (e) { return { topic: 'md_err', exchange: EX, payload: 'JSON inválido: ' + String(msg.payload).slice(0, 120) }; }
if (!d) return null;
if (d.type === 'error') return { topic: 'md_err', exchange: EX, payload: 'exchange: ' + d.message + ' ' + (d.reason || '') };
if (d.type !== 'match') return null;   // heartbeat, subscriptions, last_match (trade viejo previo a suscribir)
const m = MAP[d.product_id];
if (!m) return null;
const price = +d.price, qty = +d.size, ts = isoToMs(d.time);
if (!(price > 0) || !(qty >= 0) || !isFinite(ts)) return { topic: 'md_err', exchange: EX, payload: 'trade inválido: ' + msg.payload.slice(0, 160) };
return { topic: 'trade', payload: {
    symbol: m[0], quote: m[1], market: d.product_id, exchange: EX, kind: 'trade',
    price: price, quantity: qty, side: d.side === 'sell' ? 'BUY' : (d.side === 'buy' ? 'SELL' : null),
    trade_id: d.trade_id,
    exchange_timestamp: ts, exchange_ts_raw: d.time,
    local_receive_timestamp: msg.t_recv,
    raw: d
} };`,
  },
  {
    id: 'kraken', label: 'Kraken', pairs: 'BTC/USD, ZEC/USD, GMX/USD, XMR/USD',
    url: 'wss://ws.kraken.com/v2',
    subs: [
      { method: 'subscribe', params: { channel: 'trade', symbol: ['BTC/USD'], snapshot: false } },
      { method: 'subscribe', params: { channel: 'trade', symbol: ['ZEC/USD'], snapshot: false } },
      { method: 'subscribe', params: { channel: 'trade', symbol: ['GMX/USD'], snapshot: false } },
      { method: 'subscribe', params: { channel: 'trade', symbol: ['XMR/USD'], snapshot: false } },
    ],
    pingMs: 0, pingPayload: '', staleMs: 30000, ignore: '',
    norm: String.raw`// Kraken WS v2, canal "trade". side = lado del taker (agresor). timestamp RFC3339 con µs.
const MAP = { 'BTC/USD': ['BTC', 'USD'], 'ZEC/USD': ['ZEC', 'USD'], 'GMX/USD': ['GMX', 'USD'], 'XMR/USD': ['XMR', 'USD'] };
const EX = 'kraken';
function isoToMs(s) {
    const ms = Date.parse(s);
    if (!isFinite(ms)) return NaN;
    const f = /\.(\d+)/.exec(s);
    return (f && f[1].length > 3) ? ms + Number('0.' + f[1].slice(3)) : ms;
}
let o;
try { o = JSON.parse(msg.payload); } catch (e) { return { topic: 'md_err', exchange: EX, payload: 'JSON inválido: ' + String(msg.payload).slice(0, 120) }; }
if (!o) return null;
if (o.method === 'subscribe' && o.success === false) return { topic: 'md_err', exchange: EX, payload: 'subscribe: ' + (o.error || '') };
if (o.channel !== 'trade' || o.type !== 'update' || !Array.isArray(o.data)) return null;  // heartbeat, status, snapshot
const out = [];
for (const d of o.data) {
    const m = MAP[d.symbol];
    if (!m) continue;
    const price = +d.price, qty = +d.qty, ts = isoToMs(d.timestamp);
    if (!(price > 0) || !(qty >= 0) || !isFinite(ts)) { out.push({ topic: 'md_err', exchange: EX, payload: 'trade inválido: ' + JSON.stringify(d).slice(0, 160) }); continue; }
    out.push({ topic: 'trade', payload: {
        symbol: m[0], quote: m[1], market: d.symbol, exchange: EX, kind: 'trade',
        price: price, quantity: qty, side: d.side === 'buy' ? 'BUY' : (d.side === 'sell' ? 'SELL' : null),
        trade_id: d.trade_id,
        exchange_timestamp: ts, exchange_ts_raw: d.timestamp,
        local_receive_timestamp: msg.t_recv,
        raw: d
    } });
}
return out.length ? [out] : null;`,
  },
  {
    id: 'okx', label: 'OKX', pairs: 'BTC-USDT, ZEC-USDT, GMX-USDT',
    // trades-all (endpoint business) = cada trade individual; "trades" en /public viene agregado por orden taker
    url: 'wss://ws.okx.com:8443/ws/v5/business',
    subs: [
      { op: 'subscribe', args: [{ channel: 'trades-all', instId: 'BTC-USDT' }] },
      { op: 'subscribe', args: [{ channel: 'trades-all', instId: 'ZEC-USDT' }] },
      { op: 'subscribe', args: [{ channel: 'trades-all', instId: 'GMX-USDT' }] },
    ],
    pingMs: 20000, pingPayload: 'ping', staleMs: 45000, ignore: 'pong',
    norm: String.raw`// OKX v5 canal "trades-all" (trades individuales). side = lado del taker. ts en ms.
const MAP = { 'BTC-USDT': ['BTC', 'USDT'], 'ZEC-USDT': ['ZEC', 'USDT'], 'GMX-USDT': ['GMX', 'USDT'] };
const EX = 'okx';
if (msg.payload === 'pong') return null;
let o;
try { o = JSON.parse(msg.payload); } catch (e) { return { topic: 'md_err', exchange: EX, payload: 'JSON inválido: ' + String(msg.payload).slice(0, 120) }; }
if (!o) return null;
if (o.event === 'error') return { topic: 'md_err', exchange: EX, payload: 'exchange: ' + o.code + ' ' + o.msg };
if (!o.arg || !Array.isArray(o.data)) return null;       // event subscribe, etc.
const out = [];
for (const d of o.data) {
    const m = MAP[d.instId];
    if (!m) continue;
    const price = +d.px, qty = +d.sz, ts = +d.ts;
    if (!(price > 0) || !(qty >= 0) || !(ts > 0)) { out.push({ topic: 'md_err', exchange: EX, payload: 'trade inválido: ' + JSON.stringify(d).slice(0, 160) }); continue; }
    out.push({ topic: 'trade', payload: {
        symbol: m[0], quote: m[1], market: d.instId, exchange: EX, kind: 'trade',
        price: price, quantity: qty, side: d.side === 'buy' ? 'BUY' : (d.side === 'sell' ? 'SELL' : null),
        trade_id: d.tradeId,
        exchange_timestamp: ts, exchange_ts_raw: d.ts,
        local_receive_timestamp: msg.t_recv,
        raw: d
    } });
}
return out.length ? [out] : null;`,
  },
  {
    id: 'bybit', label: 'Bybit', pairs: 'BTCUSDT, ZECUSDT, GMXUSDT',
    url: 'wss://stream.bybit.com/v5/public/spot',
    subs: [
      { op: 'subscribe', args: ['publicTrade.BTCUSDT'] },
      { op: 'subscribe', args: ['publicTrade.ZECUSDT'] },
      { op: 'subscribe', args: ['publicTrade.GMXUSDT'] },
    ],
    pingMs: 20000, pingPayload: '{"op":"ping"}', staleMs: 45000, ignore: '',
    norm: String.raw`// Bybit v5 spot "publicTrade". S = lado del taker (Buy/Sell). T en ms.
const MAP = { BTCUSDT: ['BTC', 'USDT'], ZECUSDT: ['ZEC', 'USDT'], GMXUSDT: ['GMX', 'USDT'] };
const EX = 'bybit';
let o;
try { o = JSON.parse(msg.payload); } catch (e) { return { topic: 'md_err', exchange: EX, payload: 'JSON inválido: ' + String(msg.payload).slice(0, 120) }; }
if (!o) return null;
if (o.op) {                                               // respuestas subscribe / pong
    if (o.success === false) return { topic: 'md_err', exchange: EX, payload: 'op ' + o.op + ': ' + (o.ret_msg || '') };
    return null;
}
if (typeof o.topic !== 'string' || o.topic.indexOf('publicTrade.') !== 0 || !Array.isArray(o.data)) return null;
const out = [];
for (const d of o.data) {
    const m = MAP[d.s];
    if (!m) continue;
    const price = +d.p, qty = +d.v, ts = +d.T;
    if (!(price > 0) || !(qty >= 0) || !(ts > 0)) { out.push({ topic: 'md_err', exchange: EX, payload: 'trade inválido: ' + JSON.stringify(d).slice(0, 160) }); continue; }
    out.push({ topic: 'trade', payload: {
        symbol: m[0], quote: m[1], market: d.s, exchange: EX, kind: 'trade',
        price: price, quantity: qty, side: d.S === 'Buy' ? 'BUY' : (d.S === 'Sell' ? 'SELL' : null),
        trade_id: d.i,
        exchange_timestamp: ts, exchange_ts_raw: d.T,
        local_receive_timestamp: msg.t_recv,
        raw: d
    } });
}
return out.length ? [out] : null;`,
  },
  {
    id: 'bitfinex', label: 'Bitfinex', pairs: 'tBTCUSD, tZECUSD, tXMRUSD',
    url: 'wss://api-pub.bitfinex.com/ws/2',
    subs: [
      { event: 'subscribe', channel: 'trades', symbol: 'tBTCUSD' },
      { event: 'subscribe', channel: 'trades', symbol: 'tZECUSD' },
      { event: 'subscribe', channel: 'trades', symbol: 'tXMRUSD' },
    ],
    pingMs: 0, pingPayload: '', staleMs: 45000, ignore: '',
    norm: String.raw`// Bitfinex WS v2 canal "trades". Se usa sólo "te" (trade executed, llega antes que "tu").
// [chanId,"te",[ID,MTS,AMOUNT,PRICE]]  AMOUNT>0 => agresor BUY, <0 => SELL.
const MAP = { tBTCUSD: ['BTC', 'USD'], tZECUSD: ['ZEC', 'USD'], tXMRUSD: ['XMR', 'USD'] };
const EX = 'bitfinex';
let chans = context.get('chans') || {};
let o;
try { o = JSON.parse(msg.payload); } catch (e) { return { topic: 'md_err', exchange: EX, payload: 'JSON inválido: ' + String(msg.payload).slice(0, 120) }; }
if (!Array.isArray(o)) {
    if (!o) return null;
    if (o.event === 'info' && o.version) { chans = {}; context.set('chans', chans); }  // nueva conexión
    else if (o.event === 'subscribed' && o.channel === 'trades') { chans[o.chanId] = o.symbol; context.set('chans', chans); }
    else if (o.event === 'error') return { topic: 'md_err', exchange: EX, payload: 'exchange: ' + o.code + ' ' + o.msg };
    return null;
}
if (o[1] !== 'te' || !Array.isArray(o[2])) return null;   // hb, snapshot, tu
const sym = chans[o[0]];
const m = MAP[sym];
if (!m) return null;
const t = o[2];
const amount = +t[2], price = +t[3], ts = +t[1];
if (!(price > 0) || !isFinite(amount) || !(ts > 0)) return { topic: 'md_err', exchange: EX, payload: 'trade inválido: ' + msg.payload.slice(0, 160) };
return { topic: 'trade', payload: {
    symbol: m[0], quote: m[1], market: sym, exchange: EX, kind: 'trade',
    price: price, quantity: Math.abs(amount), side: amount > 0 ? 'BUY' : (amount < 0 ? 'SELL' : null),
    trade_id: t[0],
    exchange_timestamp: ts, exchange_ts_raw: t[1],
    local_receive_timestamp: msg.t_recv,
    raw: t
} };`,
  },
  {
    id: 'gate', label: 'Gate', pairs: 'GMX_USDT, XMR_USDT',
    url: 'wss://api.gateio.ws/ws/v4/',
    subs: [
      '{"time":__NOW_S__,"channel":"spot.trades","event":"subscribe","payload":["GMX_USDT"]}',
      '{"time":__NOW_S__,"channel":"spot.trades","event":"subscribe","payload":["XMR_USDT"]}',
    ],
    pingMs: 15000, pingPayload: '{"time":__NOW_S__,"channel":"spot.ping"}', staleMs: 45000, ignore: '',
    norm: String.raw`// Gate.io WS v4 canal "spot.trades" (trades individuales). side = lado del taker.
// create_time_ms viene como string con fracción de ms ("1606292218213.4578").
const MAP = { GMX_USDT: ['GMX', 'USDT'], XMR_USDT: ['XMR', 'USDT'] };
const EX = 'gate';
let o;
try { o = JSON.parse(msg.payload); } catch (e) { return { topic: 'md_err', exchange: EX, payload: 'JSON inválido: ' + String(msg.payload).slice(0, 120) }; }
if (!o) return null;
if (o.error) return { topic: 'md_err', exchange: EX, payload: 'exchange: ' + JSON.stringify(o.error).slice(0, 160) };
if (o.channel !== 'spot.trades' || o.event !== 'update' || !o.result) return null;   // subscribe ack, spot.pong
const list = Array.isArray(o.result) ? o.result : [o.result];
const out = [];
for (const d of list) {
    const m = MAP[d.currency_pair];
    if (!m) continue;
    const price = +d.price, qty = +d.amount;
    const ts = d.create_time_ms != null ? +d.create_time_ms : (+d.create_time) * 1000;
    if (!(price > 0) || !(qty >= 0) || !(ts > 0)) { out.push({ topic: 'md_err', exchange: EX, payload: 'trade inválido: ' + JSON.stringify(d).slice(0, 160) }); continue; }
    out.push({ topic: 'trade', payload: {
        symbol: m[0], quote: m[1], market: d.currency_pair, exchange: EX, kind: 'trade',
        price: price, quantity: qty, side: d.side === 'buy' ? 'BUY' : (d.side === 'sell' ? 'SELL' : null),
        trade_id: d.id,
        exchange_timestamp: ts, exchange_ts_raw: d.create_time_ms != null ? d.create_time_ms : d.create_time,
        local_receive_timestamp: msg.t_recv,
        raw: d
    } });
}
return out.length ? [out] : null;`,
  },
  {
    id: 'poloniex', label: 'Poloniex', pairs: 'XMR_USDT',
    url: 'wss://ws.poloniex.com/ws/public',
    subs: [
      { event: 'subscribe', channel: ['trades'], symbols: ['XMR_USDT'] },
    ],
    pingMs: 20000, pingPayload: '{"event":"ping"}', staleMs: 45000, ignore: '',
    norm: String.raw`// Poloniex WS v3 canal "trades". takerSide = lado del agresor. createTime en ms.
const MAP = { XMR_USDT: ['XMR', 'USDT'] };
const EX = 'poloniex';
let o;
try { o = JSON.parse(msg.payload); } catch (e) { return { topic: 'md_err', exchange: EX, payload: 'JSON inválido: ' + String(msg.payload).slice(0, 120) }; }
if (!o) return null;
if (o.event === 'error') return { topic: 'md_err', exchange: EX, payload: 'exchange: ' + (o.message || JSON.stringify(o)).slice(0, 160) };
if (o.channel !== 'trades' || !Array.isArray(o.data)) return null;   // subscribe ack, pong
const out = [];
for (const d of o.data) {
    const m = MAP[d.symbol];
    if (!m) continue;
    const price = +d.price, qty = +d.quantity, ts = +d.createTime;
    if (!(price > 0) || !(qty >= 0) || !(ts > 0)) { out.push({ topic: 'md_err', exchange: EX, payload: 'trade inválido: ' + JSON.stringify(d).slice(0, 160) }); continue; }
    out.push({ topic: 'trade', payload: {
        symbol: m[0], quote: m[1], market: d.symbol, exchange: EX, kind: 'trade',
        price: price, quantity: qty, side: d.takerSide === 'buy' ? 'BUY' : (d.takerSide === 'sell' ? 'SELL' : null),
        trade_id: d.id,
        exchange_timestamp: ts, exchange_ts_raw: d.createTime,
        local_receive_timestamp: msg.t_recv,
        raw: d
    } });
}
return out.length ? [out] : null;`,
  },
  // ======================= Etapa 1c: más fuentes legítimas para GMX / XMR =======================
  {
    id: 'binance-perp', label: 'Binance Perp', pairs: 'GMXUSDT, XMRUSDT (USDⓈ-M perpetual)',
    // Endpoint routed /market (Binance migró los WS de USDⓈ-M en 2026; los URLs legacy se dieron de baja).
    url: 'wss://fstream.binance.com/market/stream?streams=gmxusdt@aggTrade/xmrusdt@aggTrade',
    subs: [], pingMs: 20000, pingPayload: '', staleMs: 60000, ignore: '',
    norm: String.raw`// Binance USDⓈ-M perpetual, stream @aggTrade.
// OJO: aggTrade agrupa los fills con igual precio y lado agresor. trade_count = l - f + 1 = fills reales dentro del evento.
const MAP = { GMXUSDT: ['GMX', 'USDT'], XMRUSDT: ['XMR', 'USDT'] };
const EX = 'binance', SRC = 'binance-perp';
let o;
try { o = JSON.parse(msg.payload); } catch (e) { return { topic: 'md_err', exchange: SRC, payload: 'JSON inválido: ' + String(msg.payload).slice(0, 120) }; }
const d = o && o.data ? o.data : o;
if (!d || d.e !== 'aggTrade') return null;
const m = MAP[d.s];
if (!m) return null;
const price = +d.p, qty = +d.q, T = +d.T;
if (!(price > 0) || !(qty >= 0) || !(T > 0)) return { topic: 'md_err', exchange: SRC, payload: 'trade inválido: ' + msg.payload.slice(0, 160) };
return { topic: 'trade', payload: {
    symbol: m[0], quote: m[1], market: d.s, exchange: EX, source: SRC, market_type: 'perp', kind: 'trade',
    aggregated: true, trade_count: (d.l - d.f + 1) || 1,
    price: price, quantity: qty, side: d.m ? 'SELL' : 'BUY', trade_id: d.a,
    exchange_timestamp: T, exchange_ts_raw: d.T,
    local_receive_timestamp: msg.t_recv,
    raw: d
} };`,
  },
  {
    id: 'bybit-perp', label: 'Bybit Perp', pairs: 'GMXUSDT, XMRUSDT (linear perpetual)',
    url: 'wss://stream.bybit.com/v5/public/linear',
    subs: [
      { op: 'subscribe', args: ['publicTrade.GMXUSDT'] },
      { op: 'subscribe', args: ['publicTrade.XMRUSDT'] },
    ],
    pingMs: 20000, pingPayload: '{"op":"ping"}', staleMs: 45000, ignore: '',
    norm: String.raw`// Bybit v5 linear perpetual "publicTrade" (trades individuales). S = lado del taker. v en unidades del activo.
const MAP = { GMXUSDT: ['GMX', 'USDT'], XMRUSDT: ['XMR', 'USDT'] };
const EX = 'bybit', SRC = 'bybit-perp';
let o;
try { o = JSON.parse(msg.payload); } catch (e) { return { topic: 'md_err', exchange: SRC, payload: 'JSON inválido: ' + String(msg.payload).slice(0, 120) }; }
if (!o) return null;
if (o.op) {
    if (o.success === false) return { topic: 'md_err', exchange: SRC, payload: 'op ' + o.op + ': ' + (o.ret_msg || '') };
    return null;
}
if (typeof o.topic !== 'string' || o.topic.indexOf('publicTrade.') !== 0 || !Array.isArray(o.data)) return null;
const out = [];
for (const d of o.data) {
    const m = MAP[d.s];
    if (!m) continue;
    const price = +d.p, qty = +d.v, ts = +d.T;
    if (!(price > 0) || !(qty >= 0) || !(ts > 0)) { out.push({ topic: 'md_err', exchange: SRC, payload: 'trade inválido: ' + JSON.stringify(d).slice(0, 160) }); continue; }
    out.push({ topic: 'trade', payload: {
        symbol: m[0], quote: m[1], market: d.s, exchange: EX, source: SRC, market_type: 'perp', kind: 'trade',
        price: price, quantity: qty, side: d.S === 'Buy' ? 'BUY' : (d.S === 'Sell' ? 'SELL' : null),
        trade_id: d.i,
        exchange_timestamp: ts, exchange_ts_raw: d.T,
        local_receive_timestamp: msg.t_recv,
        raw: d
    } });
}
return out.length ? [out] : null;`,
  },
  BITGET('bitget', 'Bitget', 'GMXUSDT (spot)', 'SPOT', 'spot', ['GMXUSDT']),
  BITGET('bitget-perp', 'Bitget Perp', 'GMXUSDT, XMRUSDT (USDT-M perpetual)', 'USDT-FUTURES', 'perp', ['GMXUSDT', 'XMRUSDT']),
  {
    id: 'kraken-perp', label: 'Kraken Futures', pairs: 'PF_GMXUSD, PF_XMRUSD (perpetual)',
    url: 'wss://futures.kraken.com/ws/v1',
    subs: [
      { event: 'subscribe', feed: 'trade', product_ids: ['PF_GMXUSD'] },
      { event: 'subscribe', feed: 'trade', product_ids: ['PF_XMRUSD'] },
      { event: 'subscribe', feed: 'heartbeat' },
    ],
    pingMs: 30000, pingPayload: '', staleMs: 45000, ignore: '',
    norm: String.raw`// Kraken Futures WS v1, feed "trade" (trades individuales). side = lado del taker. qty en unidades del activo (PF_ lineal).
const MAP = { PF_GMXUSD: ['GMX', 'USD'], PF_XMRUSD: ['XMR', 'USD'] };
const EX = 'kraken', SRC = 'kraken-perp';
let o;
try { o = JSON.parse(msg.payload); } catch (e) { return { topic: 'md_err', exchange: SRC, payload: 'JSON inválido: ' + String(msg.payload).slice(0, 120) }; }
if (!o) return null;
if (o.event === 'error' || o.event === 'alert') return { topic: 'md_err', exchange: SRC, payload: 'exchange: ' + (o.message || JSON.stringify(o)).slice(0, 160) };
if (o.feed !== 'trade' || !o.product_id) return null;   // trade_snapshot (histórico), heartbeat, info, subscribed
const m = MAP[o.product_id];
if (!m) return null;
const price = +o.price, qty = +o.qty, ts = +o.time;
if (!(price > 0) || !(qty >= 0) || !(ts > 0)) return { topic: 'md_err', exchange: SRC, payload: 'trade inválido: ' + msg.payload.slice(0, 160) };
return { topic: 'trade', payload: {
    symbol: m[0], quote: m[1], market: o.product_id, exchange: EX, source: SRC, market_type: 'perp', kind: 'trade',
    price: price, quantity: qty, side: o.side === 'buy' ? 'BUY' : (o.side === 'sell' ? 'SELL' : null),
    trade_id: o.uid, fill_type: o.type,
    exchange_timestamp: ts, exchange_ts_raw: o.time,
    local_receive_timestamp: msg.t_recv,
    raw: o
} };`,
  },
  {
    id: 'hyperliquid', label: 'Hyperliquid', pairs: 'XMR (perpetual USDC)',
    url: 'wss://api.hyperliquid.xyz/ws',
    // Si el mercado XMR es un HIP-3 (builder-deployed), el coin lleva prefijo "dex:XMR": ajustar acá y en MAP.
    subs: [{ method: 'subscribe', subscription: { type: 'trades', coin: 'XMR' } }],
    pingMs: 30000, pingPayload: '{"method":"ping"}', staleMs: 75000, ignore: '',
    norm: String.raw`// Hyperliquid WS canal "trades" (trades individuales). side: "B" = agresor compra, "A" = agresor vende.
const MAP = { XMR: ['XMR', 'USDC'] };
const EX = 'hyperliquid', SRC = 'hyperliquid';
let o;
try { o = JSON.parse(msg.payload); } catch (e) { return { topic: 'md_err', exchange: SRC, payload: 'JSON inválido: ' + String(msg.payload).slice(0, 120) }; }
if (!o) return null;
if (o.channel === 'error') return { topic: 'md_err', exchange: SRC, payload: 'exchange: ' + String(o.data).slice(0, 160) };
if (o.channel !== 'trades' || !Array.isArray(o.data)) return null;   // subscriptionResponse, pong
const out = [];
for (const d of o.data) {
    const m = MAP[d.coin];
    if (!m) continue;
    const price = +d.px, qty = +d.sz, ts = +d.time;
    if (!(price > 0) || !(qty >= 0) || !(ts > 0)) { out.push({ topic: 'md_err', exchange: SRC, payload: 'trade inválido: ' + JSON.stringify(d).slice(0, 160) }); continue; }
    out.push({ topic: 'trade', payload: {
        symbol: m[0], quote: m[1], market: d.coin, exchange: EX, source: SRC, market_type: 'perp', kind: 'trade',
        price: price, quantity: qty, side: d.side === 'B' ? 'BUY' : (d.side === 'A' ? 'SELL' : null),
        trade_id: d.tid,
        exchange_timestamp: ts, exchange_ts_raw: d.time,
        local_receive_timestamp: msg.t_recv,
        raw: d
    } });
}
return out.length ? [out] : null;`,
  },
];

// Metadata por fuente (se muestra en el dashboard: par exacto y canal que se escucha).
const META = {
  binance: { exchange: 'binance', type: 'spot', feed: '@trade', markets: { BTC: 'BTCUSDT', ZEC: 'ZECUSDT', GMX: 'GMXUSDT' } },
  coinbase: { exchange: 'coinbase', type: 'spot', feed: 'matches', markets: { BTC: 'BTC-USD', ZEC: 'ZEC-USD' } },
  kraken: { exchange: 'kraken', type: 'spot', feed: 'trade (v2)', markets: { BTC: 'BTC/USD', ZEC: 'ZEC/USD', GMX: 'GMX/USD', XMR: 'XMR/USD' } },
  okx: { exchange: 'okx', type: 'spot', feed: 'trades-all', markets: { BTC: 'BTC-USDT', ZEC: 'ZEC-USDT', GMX: 'GMX-USDT' } },
  bybit: { exchange: 'bybit', type: 'spot', feed: 'publicTrade', markets: { BTC: 'BTCUSDT', ZEC: 'ZECUSDT', GMX: 'GMXUSDT' } },
  bitfinex: { exchange: 'bitfinex', type: 'spot', feed: 'trades (te)', markets: { BTC: 'tBTCUSD', ZEC: 'tZECUSD', XMR: 'tXMRUSD' } },
  gate: { exchange: 'gate', type: 'spot', feed: 'spot.trades', markets: { GMX: 'GMX_USDT', XMR: 'XMR_USDT' } },
  poloniex: { exchange: 'poloniex', type: 'spot', feed: 'trades', markets: { XMR: 'XMR_USDT' } },
  'binance-perp': { exchange: 'binance', type: 'perp', feed: '@aggTrade*', markets: { GMX: 'GMXUSDT', XMR: 'XMRUSDT' } },
  'bybit-perp': { exchange: 'bybit', type: 'perp', feed: 'publicTrade', markets: { GMX: 'GMXUSDT', XMR: 'XMRUSDT' } },
  bitget: { exchange: 'bitget', type: 'spot', feed: 'trade', markets: { GMX: 'GMXUSDT' } },
  'bitget-perp': { exchange: 'bitget', type: 'perp', feed: 'trade', markets: { GMX: 'GMXUSDT', XMR: 'XMRUSDT' } },
  'kraken-perp': { exchange: 'kraken', type: 'perp', feed: 'trade', markets: { GMX: 'PF_GMXUSD', XMR: 'PF_XMRUSD' } },
  hyperliquid: { exchange: 'hyperliquid', type: 'perp', feed: 'trades', markets: { XMR: 'XMR' } },
};
for (const ex of EXCHANGES) if (!META[ex.id]) throw new Error('falta META de ' + ex.id);

// ---------------------------------------------------------------------------
// Subflow: conector WebSocket genérico (usa el módulo npm "ws")
// ---------------------------------------------------------------------------
const CONN_INIT = String.raw`// ===== Conector WebSocket genérico =====
// Se configura con las variables de entorno de cada instancia del subflow.
// Salida 1: frames crudos {payload:string, t_recv, exchange}
// Salida 2: estado cada 1 s {topic:'ws_status', payload:{...}}
// Salida 3: status visual de la instancia
// Arranca habilitado salvo que antes se lo haya apagado (estado guardado en global 'prrr_ws_enabled').
let subs = [];
try { subs = JSON.parse(env.get('SUBSCRIBE') || '[]'); } catch (e) { node.error('SUBSCRIBE no es JSON válido'); }
if (!Array.isArray(subs)) subs = [subs];
const cfg = {
    exchange: env.get('EXCHANGE') || 'unknown',
    url: env.get('WS_URL') || '',
    subs: subs,
    pingMs: Number(env.get('PING_MS')) || 0,
    pingPayload: env.get('PING_PAYLOAD') || '',
    staleMs: Number(env.get('STALE_MS')) || 30000,
    ignore: env.get('IGNORE_TEXT') || ''
};
const C = {
    cfg: cfg, ws: null, enabled: false, connected: false, stopped: false,
    reconnects: 0, attempt: 0, frames: 0, bytes: 0,
    lastMsgAt: 0, lastPingAt: 0, lastError: '', lastState: '',
    rcTimer: null, timer: null
};

// __NOW_S__ / __NOW_MS__ en SUBSCRIBE o PING_PAYLOAD se reemplazan por la hora actual (Gate lo pide)
C.fill = function (s) {
    if (s.indexOf('__NOW_') < 0) return s;
    const t = Date.now();
    return s.split('__NOW_MS__').join(String(t)).split('__NOW_S__').join(String(Math.floor(t / 1000)));
};

C.state = function () {
    if (!C.enabled) return 'off';
    if (C.connected) return 'connected';
    return C.ws ? 'connecting' : 'down';
};

C.pushStatus = function () {
    const now = Date.now();
    const st = C.state();
    const p = {
        exchange: cfg.exchange, state: st, enabled: C.enabled, connected: C.connected,
        reconnects: C.reconnects, frames: C.frames, bytes: C.bytes,
        lastMsgAge: C.lastMsgAt ? now - C.lastMsgAt : null,
        lastError: C.lastError, url: cfg.url
    };
    C.frames = 0; C.bytes = 0;
    let vis = null;
    const txt = st + ' · rc ' + C.reconnects;
    if (txt !== C.lastState) {
        C.lastState = txt;
        const fill = st === 'connected' ? 'green' : st === 'off' ? 'grey' : st === 'connecting' ? 'yellow' : 'red';
        vis = { payload: { fill: fill, shape: C.connected ? 'dot' : 'ring', text: txt } };
    }
    node.send([null, { topic: 'ws_status', exchange: cfg.exchange, payload: p }, vis], false);
};

C.drop = function () {                    // cierra el socket actual sin disparar reconexión
    const ws = C.ws;
    C.ws = null; C.connected = false;
    if (ws) {
        ws.removeAllListeners();
        ws.on('error', function () {});   // nunca dejar un 'error' sin listener (tumbaría Node-RED)
        try { ws.terminate(); } catch (e) {}
    }
};

C.scheduleReconnect = function () {
    if (C.stopped || !C.enabled || C.rcTimer) return;
    const delay = Math.min(30000, 1000 * Math.pow(2, C.attempt)) + Math.floor(Math.random() * 500);
    C.attempt = Math.min(C.attempt + 1, 5);
    C.reconnects++;
    C.rcTimer = setTimeout(function () { C.rcTimer = null; C.connect(); }, delay);
};

C.connect = function () {
    if (C.stopped || !C.enabled || C.ws) return;
    if (!cfg.url) { C.lastError = 'WS_URL vacío'; return; }
    let ws;
    try {
        ws = new WS(cfg.url, { perMessageDeflate: false, handshakeTimeout: 10000, maxPayload: 32 * 1024 * 1024 });
    } catch (e) {
        C.lastError = String(e.message || e);
        C.scheduleReconnect();
        return;
    }
    C.ws = ws;
    ws.on('open', function () {
        if (C.ws !== ws) return;
        C.connected = true;
        C.lastMsgAt = C.lastPingAt = Date.now();
        C.lastError = '';
        for (const s of cfg.subs) {
            try { ws.send(C.fill(typeof s === 'string' ? s : JSON.stringify(s))); }
            catch (e) { C.lastError = 'subscribe: ' + e.message; }
        }
        C.pushStatus();
    });
    ws.on('message', function (data) {
        const t = Date.now();              // timestamp local lo antes posible
        if (C.ws !== ws) return;
        C.lastMsgAt = t; C.frames++; C.bytes += data.length;
        C.attempt = 0;                     // hay datos: resetea el backoff
        const s = data.toString();
        if (cfg.ignore && s === cfg.ignore) return;
        node.send([{ topic: 'raw', exchange: cfg.exchange, t_recv: t, payload: s }, null, null], false);
    });
    ws.on('ping', function () { C.lastMsgAt = Date.now(); });   // ws responde pong solo
    ws.on('pong', function () { C.lastMsgAt = Date.now(); });
    ws.on('error', function (e) { C.lastError = String((e && e.message) || e); });
    ws.on('close', function (code) {
        if (C.ws !== ws) return;
        C.ws = null; C.connected = false;
        if (!C.lastError) C.lastError = 'close ' + code;
        C.pushStatus();
        C.scheduleReconnect();
    });
};

C.tick = function () {                    // watchdog + ping + status, cada 1 s
    const now = Date.now();
    if (C.ws && C.connected) {
        if (now - C.lastMsgAt > cfg.staleMs) {
            C.lastError = 'stale: ' + (now - C.lastMsgAt) + ' ms sin datos';
            C.drop();
            C.scheduleReconnect();
        } else if (cfg.pingMs && now - C.lastPingAt >= cfg.pingMs) {
            C.lastPingAt = now;
            try { if (cfg.pingPayload) C.ws.send(C.fill(cfg.pingPayload)); else C.ws.ping(); } catch (e) {}
        }
    }
    C.pushStatus();
};

C.timer = setInterval(C.tick, 1000);
context.set('C', C, 'memory');
// Recordar on/off entre redeploys parciales (el inject de autostart sólo corre al iniciar el flow completo)
const want = (global.get('prrr_ws_enabled', 'memory') || {})[cfg.exchange];
if (want !== false) { C.enabled = true; C.connect(); }
C.pushStatus();`;

const CONN_FUNC = String.raw`// Control: {topic:'enable', payload:true|false}  |  {topic:'reconnect'}
const C = context.get('C', 'memory');
if (!C) return null;
const topic = String(msg.topic || 'enable').toLowerCase();
if (topic === 'reconnect') {
    if (C.enabled) {
        if (C.rcTimer) { clearTimeout(C.rcTimer); C.rcTimer = null; }
        C.lastError = 'reconexión manual';
        C.drop();
        C.reconnects++;
        C.attempt = 0;
        C.connect();
    }
} else {
    let v = msg.payload;
    if (typeof v === 'string') v = /^(true|on|1|enable|enabled)$/i.test(v.trim());
    v = !!v;
    if (v && !C.enabled) {
        C.enabled = true; C.attempt = 0; C.lastError = '';
        C.connect();
    } else if (!v && C.enabled) {
        C.enabled = false;
        if (C.rcTimer) { clearTimeout(C.rcTimer); C.rcTimer = null; }
        C.drop();
        C.lastError = '';
    }
}
const en = global.get('prrr_ws_enabled', 'memory') || {};
en[C.cfg.exchange] = C.enabled;
global.set('prrr_ws_enabled', en, 'memory');
C.pushStatus();
return null;`;

const CONN_FINAL = String.raw`const C = context.get('C', 'memory');
if (C) {
    C.stopped = true; C.enabled = false;
    clearInterval(C.timer);
    if (C.rcTimer) clearTimeout(C.rcTimer);
    C.drop();
}
context.set('C', undefined, 'memory');`;

add({
  id: SF, type: 'subflow', name: 'PRRR WS Connector',
  info: 'Cliente WebSocket genérico con reconexión (backoff exponencial 1s→30s), watchdog de datos viejos (STALE_MS), ping de aplicación opcional y habilitación on/off por mensaje.\n\n**Entrada**: `{topic:"enable", payload:true|false}` o `{topic:"reconnect"}`.\n\n**Salida 1**: frame crudo `{payload:string, t_recv:ms, exchange}`.\n\n**Salida 2**: estado cada 1 s `{topic:"ws_status", payload:{state, reconnects, frames, bytes, lastMsgAge, lastError}}`.\n\nRequiere el módulo npm `ws` (Node-RED lo instala solo la primera vez: `functionExternalModules: true`).',
  category: '', in: [{ x: 60, y: 80, wires: [{ id: 'prrr_sf_fn' }] }],
  out: [
    { x: 460, y: 60, wires: [{ id: 'prrr_sf_fn', port: 0 }] },
    { x: 460, y: 100, wires: [{ id: 'prrr_sf_fn', port: 1 }] },
  ],
  env: [
    { name: 'EXCHANGE', type: 'str', value: '' },
    { name: 'WS_URL', type: 'str', value: '' },
    { name: 'SUBSCRIBE', type: 'str', value: '[]' },
    { name: 'PING_MS', type: 'str', value: '0' },
    { name: 'PING_PAYLOAD', type: 'str', value: '' },
    { name: 'STALE_MS', type: 'str', value: '30000' },
    { name: 'IGNORE_TEXT', type: 'str', value: '' },
  ],
  meta: {}, color: '#E2D96E', icon: 'font-awesome/fa-plug',
  inputLabels: ['control (enable / reconnect)'],
  outputLabels: ['frames crudos', 'ws_status'],
  status: { x: 460, y: 140, wires: [{ id: 'prrr_sf_fn', port: 2 }] },
});
add({
  id: 'prrr_sf_fn', type: 'function', z: SF, name: 'WS client (ws)',
  func: CONN_FUNC, outputs: 3, timeout: 0, noerr: 0,
  initialize: CONN_INIT, finalize: CONN_FINAL,
  libs: [{ var: 'WS', module: 'ws' }],
  x: 250, y: 100, wires: [[], [], []],
});

// ---------------------------------------------------------------------------
// Tab principal
// ---------------------------------------------------------------------------
add({
  id: TAB, type: 'tab', label: 'PRRR Market Data (BTC · ZEC · GMX · XMR)', disabled: false,
  info: 'Etapa 1: adquisición de trades individuales BTC/ZEC por WebSocket directo desde varios exchanges, formato normalizado común, métricas de caudal/frescura y dashboard.\n\nConsumir el stream normalizado desde otros flows con un **link in** conectado a `MD STREAM →`.\n\nContexto global disponible: `md_stats` (métricas de la última ventana de 1 s) y `md_ring` (últimos 5000 trades por símbolo, buffer circular).',
});

// Dashboard (node-red-dashboard 1.x/3.x). No se incluye ui_base: usa el existente.
const UI_TAB = 'prrr_ui_tab';
add({ id: UI_TAB, type: 'ui_tab', name: 'PRRR Market Data', icon: 'fa-bolt', order: 90, disabled: false, hidden: false });
const G = {
  btc: { id: 'prrr_ui_g_btc', name: 'BTC', order: 1, width: 12 },
  zec: { id: 'prrr_ui_g_zec', name: 'ZEC', order: 2, width: 12 },
  gmx: { id: 'prrr_ui_g_gmx', name: 'GMX', order: 3, width: 12 },
  xmr: { id: 'prrr_ui_g_xmr', name: 'XMR', order: 4, width: 12 },
  thr: { id: 'prrr_ui_g_thr', name: 'Throughput / frescura (1 s)', order: 5, width: 24 },
  perf: { id: 'prrr_ui_g_perf', name: 'Mercados escuchados: par exacto · canal · trades/min reales', order: 6, width: 24 },
  ctrl: { id: 'prrr_ui_g_ctrl', name: 'Controles WebSocket', order: 7, width: 24 },
  tech: { id: 'prrr_ui_g_tech', name: 'Métricas técnicas', order: 8, width: 24 },
};
for (const g of Object.values(G)) {
  add({ id: g.id, type: 'ui_group', name: g.name, tab: UI_TAB, order: g.order, disp: true, width: String(g.width), collapse: false, className: '' });
}

// Editor groups (cajas visuales) — se calculan sus bounds al final.
const editorGroups = [];
function egroup(id, name, color) {
  const g = { id, type: 'group', z: TAB, name, style: { label: true, stroke: color, fill: color, 'fill-opacity': '0.12', color: '#000000' }, nodes: [], x: 0, y: 0, w: 0, h: 0 };
  editorGroups.push(g);
  return g;
}
function inGroup(g, n) { n.g = g.id; g.nodes.push(n.id); return add(n); }

// Core ids
const LINK_IN = 'prrr_link_bus_in';
const CORE = 'prrr_core';
const linkOuts = [];

// ---------------------------------------------------------------------------
// Exchange groups
// ---------------------------------------------------------------------------
const COLORS = ['#3f93cf', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b', '#e377c2', '#17becf'];
const connectorIds = [];
EXCHANGES.forEach((ex, i) => {
  const y = 160 + i * 110;
  const nid = ex.id.replace(/-/g, '_');
  const g = egroup('prrr_grp_' + nid, ex.label + ' — ' + ex.pairs, COLORS[i % COLORS.length]);
  const ids = {
    inj: 'prrr_' + nid + '_autostart', sw: 'prrr_' + nid + '_switch', ws: 'prrr_' + nid + '_ws',
    norm: 'prrr_' + nid + '_norm', lo: 'prrr_' + nid + '_linkout',
  };
  connectorIds.push(ids.ws);
  inGroup(g, {
    id: ids.inj, type: 'inject', z: TAB, name: 'autostart ' + ex.id + ' = ON',
    props: [{ p: 'payload' }, { p: 'topic', vt: 'str' }],
    repeat: '', crontab: '', once: true, onceDelay: '1', topic: 'enable', payload: 'true', payloadType: 'bool',
    x: 170, y, wires: [[ids.sw]],
  });
  inGroup(g, {
    id: ids.sw, type: 'ui_switch', z: TAB, name: ex.label + ' on/off', label: ex.label, tooltip: 'Habilita / deshabilita el WebSocket de ' + ex.label,
    group: G.ctrl.id, order: 1 + i, width: 3, height: 1, passthru: true, decouple: 'false',
    topic: 'enable', topicType: 'str', style: '', onvalue: 'true', onvalueType: 'bool', onicon: '', oncolor: '',
    offvalue: 'false', offvalueType: 'bool', officon: '', offcolor: '', animate: false, className: '',
    x: 390, y, wires: [[ids.ws]],
  });
  inGroup(g, {
    id: ids.ws, type: 'subflow:' + SF, z: TAB, name: ex.label + ' WS',
    env: [
      { name: 'EXCHANGE', type: 'str', value: ex.id },
      { name: 'WS_URL', type: 'str', value: ex.url },
      { name: 'SUBSCRIBE', type: 'str', value: JSON.stringify(ex.subs) },
      { name: 'PING_MS', type: 'str', value: String(ex.pingMs) },
      { name: 'PING_PAYLOAD', type: 'str', value: ex.pingPayload },
      { name: 'STALE_MS', type: 'str', value: String(ex.staleMs) },
      { name: 'IGNORE_TEXT', type: 'str', value: ex.ignore },
    ],
    x: 600, y, wires: [[ids.norm], [ids.lo]],
  });
  inGroup(g, {
    id: ids.norm, type: 'function', z: TAB, name: 'normalizar ' + ex.id,
    func: ex.norm, outputs: 1, timeout: 0, noerr: 0, initialize: '', finalize: '', libs: [],
    x: 820, y: y - 20, wires: [[ids.lo]],
  });
  inGroup(g, {
    id: ids.lo, type: 'link out', z: TAB, name: ex.id + ' → MD BUS', mode: 'link', links: [LINK_IN],
    x: 1000, y, wires: [],
  });
  linkOuts.push(ids.lo);
});

// ---------------------------------------------------------------------------
// Core
// ---------------------------------------------------------------------------
const CORE_INIT = String.raw`// ===== MD CORE: estado en memoria (nunca crece sin límite) =====
const RING_SIZE = 5000;          // últimos N trades por símbolo (buffer circular, sin cambios desde etapa 1)
const META = __META__;           // fuentes configuradas: exchange, tipo (spot/perp), canal y par exacto por símbolo
const S = {
    t0: Date.now(),
    tickMs: 50,                  // período del inject "tick"
    renderMs: 500,               // frecuencia de render UI (ajustable desde el dashboard)
    lastRender: 0, lastStats: Date.now(), lastTick: 0,
    lag: { max: 0, sum: 0, n: 0 },
    ex: {}, sym: {},
    totalEvents: 0,
    peak: { TOTAL: 0 },
    ring: {}, ringSize: RING_SIZE,
    symbols: ['BTC', 'ZEC', 'GMX', 'XMR'],   // instrumentos con gráfico propio (el orden = salidas del nodo)
    meta: META, expected: {}
};
for (const sy of S.symbols) S.expected[sy] = Object.keys(META).filter(function (k) { return !!META[k].markets[sy]; });
context.set('S', S, 'memory');
global.set('md_ring', S.ring, 'memory');`;

const CORE_FUNC = String.raw`// ===== MD CORE =====
// Entradas: trades normalizados (topic 'trade'), 'ws_status', 'md_err', 'tick', 'render_ms', 'reset'.
// Salida 1 = stream normalizado (1 msg por trade, sin agregar).
// Salidas 2..5 charts (BTC,ZEC,GMX,XMR) · 6..9 tarjetas · 10 tabla fuentes · 11 chart ev/s · 12 chart frescura · 13 métricas técnicas.
// Nada se inventa: los gráficos sólo reciben un punto cuando llegó un trade nuevo de esa fuente.
const S = context.get('S', 'memory');
const now = Date.now();
const topic = msg.topic;
const N = S.symbols.length;
const NOUT = 5 + 2 * N;

function newWin() { return { n: 0, bySym: {}, fillsBySym: {}, frames: 0, latSum: 0, latN: 0, latMin: Infinity, latMax: -Infinity, pipeSum: 0, pipeMax: 0 }; }
function getEx(name) {
    let e = S.ex[name];
    if (!e) {
        e = S.ex[name] = { name: name, state: '?', reconnects: 0, lastError: '', lastMsgAge: null, parseErrors: 0, lastParseError: '', total: 0, last: {}, w: newWin(), m1: {} };
        for (const sy of S.symbols) e.m1[sy] = { b: new Array(60).fill(0), i: 0, sum: 0, n: 0 };
    }
    return e;
}

// ---------------- camino caliente: 1 trade ----------------
if (topic === 'trade') {
    const t = msg.payload;
    const e = getEx(t.source || t.exchange);
    const w = e.w;
    const pipe = now - t.local_receive_timestamp;          // cola interna de Node-RED (sin depender del reloj del exchange)
    const lat = t.local_receive_timestamp - t.exchange_timestamp; // exchange -> notebook (depende de sincronía NTP)
    w.n++; e.total++; S.totalEvents++;
    w.bySym[t.symbol] = (w.bySym[t.symbol] || 0) + 1;
    w.fillsBySym[t.symbol] = (w.fillsBySym[t.symbol] || 0) + (t.trade_count || 1);
    w.pipeSum += pipe; if (pipe > w.pipeMax) w.pipeMax = pipe;
    if (isFinite(lat)) { w.latSum += lat; w.latN++; if (lat < w.latMin) w.latMin = lat; if (lat > w.latMax) w.latMax = lat; }
    let L = e.last[t.symbol];
    if (!L) L = e.last[t.symbol] = {};
    L.price = t.price; L.qty = t.quantity; L.side = t.side; L.tRecv = t.local_receive_timestamp; L.tEx = t.exchange_timestamp; L.quote = t.quote; L.dirty = true;
    let y = S.sym[t.symbol];
    if (!y) y = S.sym[t.symbol] = { w: 0, total: 0 };
    y.w++; y.total++; y.price = t.price; y.ex = t.source || t.exchange; y.side = t.side; y.tRecv = t.local_receive_timestamp; y.dirty = true;
    let r = S.ring[t.symbol];
    if (!r) r = S.ring[t.symbol] = { size: S.ringSize, buf: new Array(S.ringSize), idx: 0, count: 0 };
    r.buf[r.idx] = t; r.idx = (r.idx + 1) % r.size; if (r.count < r.size) r.count++;
    const o = new Array(NOUT).fill(null); o[0] = msg;
    return o;
}

if (topic === 'ws_status') {
    const p = msg.payload || {};
    const e = getEx(p.exchange || msg.exchange);
    e.state = p.state; e.reconnects = p.reconnects; e.lastError = p.lastError || '';
    e.lastMsgAge = p.lastMsgAge; e.w.frames += (p.frames || 0);
    return null;
}

if (topic === 'md_err') {
    const e = getEx(msg.exchange || '?');
    e.parseErrors++; e.lastParseError = String(msg.payload).slice(0, 200);
    return null;
}

if (topic === 'render_ms') {
    const v = Number(msg.payload);
    if (v >= 50 && v <= 10000) S.renderMs = v;
    return null;
}

if (topic === 'reset') {
    S.peak = { TOTAL: 0 }; S.totalEvents = 0; S.t0 = now;
    S.lag = { max: 0, sum: 0, n: 0 };
    for (const k in S.ex) {
        const e = S.ex[k];
        e.total = 0; e.parseErrors = 0; e.lastParseError = '';
        for (const sy of S.symbols) e.m1[sy] = { b: new Array(60).fill(0), i: 0, sum: 0, n: 0 };
    }
    for (const k in S.sym) S.sym[k].total = 0;
    const o = new Array(NOUT).fill(null);
    for (let i = 0; i < N; i++) o[1 + i] = { payload: { reset: true, now: now } };
    o[2 + 2 * N] = { payload: { reset: true, now: now } }; o[3 + 2 * N] = { payload: { reset: true, now: now } };
    return o;
}

if (topic !== 'tick') return null;

// ---------------- reloj de UI (tick cada S.tickMs) ----------------
if (S.lastTick) {
    const lag = Math.max(0, now - S.lastTick - S.tickMs);   // retraso del event loop + cola de mensajes
    S.lag.sum += lag; S.lag.n++; if (lag > S.lag.max) S.lag.max = lag;
}
S.lastTick = now;
const OUT_TABLE = 1 + 2 * N, OUT_EVS = 2 + 2 * N, OUT_FRESH = 3 + 2 * N, OUT_TECH = 4 + 2 * N;
const out = new Array(NOUT).fill(null);
const fmtP = (p) => p == null ? '—' : (p >= 1000 ? p.toFixed(2) : p >= 1 ? p.toFixed(3) : p.toPrecision(5));
const fmt = (v, d) => (v == null || !isFinite(v)) ? '—' : v.toFixed(d);
const fmtAge = (ms) => ms < 1000 ? Math.round(ms) + ' ms' : ms < 60000 ? (ms / 1000).toFixed(1) + ' s' : ms < 3600000 ? Math.floor(ms / 60000) + ' min' : '>1 h';
const perMinOf = (e, sy) => e && e.m1[sy] ? e.m1[sy].sum : 0;

// Render de gráficos + tarjetas (a S.renderMs, independiente de la tasa de trades)
if (now - S.lastRender >= S.renderMs - S.tickMs / 2) {
    S.lastRender = now;
    S.symbols.forEach(function (sym, i) {
        const pts = [];
        const perEx = [];
        let symMin = 0;
        const srcs = S.expected[sym].slice();
        for (const k in S.ex) if (S.ex[k].last[sym] && srcs.indexOf(k) < 0) srcs.push(k);
        for (const k of srcs) {
            const e = S.ex[k];
            const L = e ? e.last[sym] : null;
            const meta = S.meta[k] || {};
            const pm = perMinOf(e, sym);
            symMin += pm;
            if (L && L.dirty) { pts.push({ s: k, x: L.tRecv, y: L.price }); L.dirty = false; }   // último trade real de la fuente en este ciclo
            perEx.push({ ex: k, perp: meta.type === 'perp', price: L ? fmtP(L.price) : '—', side: L ? L.side : null,
                age: L ? fmtAge(now - L.tRecv) : 'sin trades', pm: pm, off: !e || e.state === 'off' });
        }
        out[1 + i] = { payload: { now: now, pts: pts } };   // 1 mensaje = 1 redibujado (el eje de tiempo avanza aunque no haya trades)
        const y = S.sym[sym];
        out[1 + N + i] = { payload: {
            symbol: sym, price: y ? fmtP(y.price) : '—', exchange: y ? y.ex : '—', side: y ? y.side : null,
            age: y ? fmtAge(now - y.tRecv) : '—', evs: S.symRate ? fmt(S.symRate[sym] || 0, 1) : '—',
            perMin: symMin, perEx: perEx
        } };
    });
}

// Estadísticas cada 1 s
if (now - S.lastStats >= 1000) {
    const dt = (now - S.lastStats) / 1000;
    S.lastStats = now;
    const rows = [], tech = [];
    let tot = 0, latSum = 0, latN = 0, pipeMaxAll = 0;
    const symTot = {}, symMin = {};
    const names = Object.keys(S.meta).concat(Object.keys(S.ex).filter(function (k) { return !S.meta[k]; }));
    for (const k of names) {
        const e = getEx(k), w = e.w, meta = S.meta[k] || { exchange: k, type: '?', feed: '?', markets: {} };
        tot += w.n; latSum += w.latSum; latN += w.latN;
        if (w.pipeMax > pipeMaxAll) pipeMaxAll = w.pipeMax;
        for (const s in w.bySym) symTot[s] = (symTot[s] || 0) + w.bySym[s];
        const syms = S.symbols.map(function (sy) {
            const b = e.m1[sy];                       // ventana deslizante de 60 s (fills reales)
            const c = w.fillsBySym[sy] || 0;
            b.sum += c - b.b[b.i]; b.b[b.i] = c; b.i = (b.i + 1) % 60; if (b.n < 60) b.n++;
            symMin[sy] = (symMin[sy] || 0) + b.sum;
            const L = e.last[sy], mk = meta.markets[sy];
            if (!mk && !L) return { sym: sy, on: false };
            return { sym: sy, on: true, market: mk || '?', evs: fmt((w.bySym[sy] || 0) / dt, 1), pm: b.sum,
                partial: b.n < 60, price: L ? fmtP(L.price) : '—', age: L ? fmtAge(now - L.tRecv) : 'sin trades' };
        });
        rows.push({ ex: k, exchange: meta.exchange, type: meta.type, feed: meta.feed, state: e.state, reconnects: e.reconnects,
            evs: fmt(w.n / dt, 1), syms: syms, lastMsgAge: e.lastMsgAge == null ? '—' : fmt(e.lastMsgAge / 1000, 1) });
        tech.push({ ex: k, state: e.state, fps: fmt(w.frames / dt, 1), evs: fmt(w.n / dt, 1),
            latAvg: w.latN ? fmt(w.latSum / w.latN, 0) : '—', latMin: w.latN ? fmt(w.latMin, 0) : '—', latMax: w.latN ? fmt(w.latMax, 0) : '—',
            pipeAvg: w.n ? fmt(w.pipeSum / w.n, 1) : '—', pipeMax: w.n ? fmt(w.pipeMax, 0) : '—',
            errs: e.parseErrors, total: e.total, reconnects: e.reconnects, err: e.lastError || e.lastParseError || '' });
        e.w = newWin();
    }
    const totEvs = tot / dt;
    S.symRate = {};
    for (const s of S.symbols) S.symRate[s] = (symTot[s] || 0) / dt;
    for (const s in symTot) if (!(s in S.symRate)) S.symRate[s] = symTot[s] / dt;
    if (totEvs > S.peak.TOTAL) S.peak.TOTAL = totEvs;
    for (const s in S.symRate) if (!(S.peak[s] >= S.symRate[s])) S.peak[s] = S.symRate[s];
    const lagAvg = S.lag.n ? S.lag.sum / S.lag.n : 0, lagMax = S.lag.max;
    S.lag = { max: 0, sum: 0, n: 0 };
    const summary = {
        totEvs: fmt(totEvs, 1), peakTot: fmt(S.peak.TOTAL, 1),
        syms: S.symbols.map(function (sy) { return { sym: sy, evs: fmt(S.symRate[sy] || 0, 1), peak: fmt(S.peak[sy] || 0, 1), pm: symMin[sy] || 0 }; }),
        total: S.totalEvents, uptime: Math.round((now - S.t0) / 1000),
        lagAvg: fmt(lagAvg, 1), lagMax: fmt(lagMax, 0), pipeMax: fmt(pipeMaxAll, 0),
        latAvg: latN ? fmt(latSum / latN, 0) : '—', renderMs: S.renderMs
    };
    // Tabla de mercados: un bloque por instrumento, una fila por mercado escuchado, ordenado por caudal real (trades/min)
    const blocks = S.symbols.map(function (sy) {
        const list = [];
        for (const r of rows) {
            const c = r.syms[S.symbols.indexOf(sy)];
            if (c && c.on) list.push({ ex: r.ex, type: r.type, feed: r.feed, state: r.state, market: c.market, pm: c.pm, partial: c.partial, evs: c.evs, price: c.price, age: c.age });
        }
        list.sort(function (a, b) { return b.pm - a.pm || (a.ex < b.ex ? -1 : 1); });
        return { sym: sy, pm: symMin[sy] || 0, evs: fmt(S.symRate[sy] || 0, 1), rows: list };
    });
    out[OUT_TABLE] = { payload: { blocks: blocks } };
    out[OUT_TECH] = { payload: { rows: tech, summary: summary } };
    out[OUT_EVS] = { payload: { now: now, pts: [{ s: 'TOTAL', x: now, y: +totEvs.toFixed(2) }].concat(
        S.symbols.map(function (sy) { return { s: sy, x: now, y: +(S.symRate[sy] || 0).toFixed(2) }; })) } };
    const fr = [
        { s: 'lag event-loop máx', x: now, y: lagMax },
        { s: 'cola Node-RED máx', x: now, y: pipeMaxAll }
    ];
    if (latN) fr.push({ s: 'exchange→local prom', x: now, y: Math.round(latSum / latN) });
    out[OUT_FRESH] = { payload: { now: now, pts: fr } };
    global.set('md_stats', { t: now, dt: dt, totEvs: totEvs, symRate: S.symRate, perMin: symMin, lagAvg: lagAvg, lagMax: lagMax, pipeMax: pipeMaxAll, rows: rows, tech: tech }, 'memory');
}
return out;`;

const gCore = egroup('prrr_grp_core', 'MD CORE — bus común, métricas, buffer circular, reloj de UI', '#555555');
const gUi = egroup('prrr_grp_ui', 'Dashboard (render desacoplado)', '#1f77b4');
const gOut = egroup('prrr_grp_out', 'Salida para etapas futuras (indicadores se enganchan acá)', '#2ca02c');
const gCtl = egroup('prrr_grp_ctl', 'Controles globales', '#7f7f7f');
const gErr = egroup('prrr_grp_err', 'Errores', '#d62728');

const CX = 1250;
inGroup(gCore, {
  id: LINK_IN, type: 'link in', z: TAB, name: 'MD BUS', links: linkOuts.slice(), x: CX - 130, y: 320, wires: [[CORE]],
});
inGroup(gCore, {
  id: 'prrr_tick', type: 'inject', z: TAB, name: 'tick UI 50 ms',
  props: [{ p: 'topic', vt: 'str' }], repeat: '0.05', crontab: '', once: true, onceDelay: '0.5', topic: 'tick',
  x: CX - 110, y: 380, wires: [[CORE]],
});
const SYMS = ['btc', 'zec', 'gmx', 'xmr'];
inGroup(gCore, {
  id: CORE, type: 'function', z: TAB, name: 'MD CORE (stats · ring · render)',
  func: CORE_FUNC, outputs: 5 + 2 * SYMS.length, timeout: 0, noerr: 0,
  initialize: CORE_INIT.replace('__META__', JSON.stringify(META)), finalize: '', libs: [],
  outputLabels: ['stream normalizado'].concat(SYMS.map((s) => 'chart ' + s.toUpperCase()), SYMS.map((s) => 'card ' + s.toUpperCase()),
    ['tabla fuentes', 'chart ev/s', 'chart frescura', 'métricas técnicas']),
  x: CX + 110, y: 340,
  wires: [['prrr_stream_out']].concat(SYMS.map((s) => ['prrr_ui_chart_' + s]), SYMS.map((s) => ['prrr_ui_card_' + s]),
    [['prrr_ui_table'], ['prrr_ui_chart_evs'], ['prrr_ui_chart_fresh'], ['prrr_ui_tech']]),
});
inGroup(gCore, {
  id: 'prrr_comment_core', type: 'comment', z: TAB, name: 'Formato común del trade (msg.payload)',
  info: '```\n{\n  symbol: "BTC" | "ZEC" | "GMX" | "XMR",\n  quote: "USD" | "USDT" | "USDC",\n  market: par nativo del exchange,\n  exchange: "binance" | "coinbase" | "kraken" | "okx" | "bybit" | "bitfinex" | "gate" | "poloniex" | "bitget" | "hyperliquid",\n  source: fuente = exchange o exchange-perp (ej. "binance-perp"),\n  market_type: "spot" | "perp",\n  kind: "trade",\n  price: Number,\n  quantity: Number (unidades del activo),\n  side: "BUY" | "SELL" | null   // lado del AGRESOR (taker)\n  trade_id,\n  aggregated / trade_count: sólo Binance perp (@aggTrade agrupa fills),\n  exchange_timestamp: ms epoch (Number, puede tener decimales = µs),\n  exchange_ts_raw: timestamp original sin tocar,\n  local_receive_timestamp: ms epoch al recibir el frame WS,\n  raw: objeto original del exchange\n}\n```\nCada evento conserva su exchange y su fuente. No hay agregación ni promedios antes del core.',
  x: CX + 80, y: 260, wires: [],
});

// ---------------------------------------------------------------------------
// Dashboard: grilla 2×2 simétrica (BTC|ZEC / GMX|XMR) + bloques a ancho completo
// ---------------------------------------------------------------------------
const UX = 1720;
// Gráfico canvas liviano: 1 mensaje por ciclo de render = 1 redibujado (ui_chart redibuja por cada punto).
// Buffers acotados en el navegador (ventana de tiempo + máximo de puntos por serie).
const SRC_COLORS = {
  binance: '#f0b90b', 'binance-perp': '#b8860b', coinbase: '#1652f0', kraken: '#7132f5', 'kraken-perp': '#b39ddb',
  okx: '#607d8b', bybit: '#f7a600', 'bybit-perp': '#ff6f00', bitfinex: '#16b157', gate: '#e53935', poloniex: '#00897b',
  bitget: '#00bcd4', 'bitget-perp': '#006064', hyperliquid: '#97fce4',
  TOTAL: '#1f77b4', BTC: '#ff7f0e', ZEC: '#2ca02c', GMX: '#d62728', XMR: '#9467bd',
};
function chartTpl(domId, opts) {
  const cfg = Object.assign({ windowMs: 300000, maxPts: 1500, step: true, ymin0: false, title: '' }, opts);
  return `<style>
#${domId}{position:relative;width:100%;height:100%;display:flex;flex-direction:column}
#${domId} .lg{font:11px monospace;line-height:1.3;min-height:15px}
#${domId} .lg span{display:inline-block;margin-right:10px;white-space:nowrap}
#${domId} .lg i{display:inline-block;width:10px;height:3px;margin-right:4px;vertical-align:middle}
#${domId} .cv{flex:1;position:relative;min-height:0}
#${domId} canvas{position:absolute;left:0;top:0;width:100%;height:100%}
</style>
<div id="${domId}"><div class="lg"></div><div class="cv"><canvas></canvas></div></div>
<script>
(function (scope) {
  var ID = ${JSON.stringify(domId)}, CFG = ${JSON.stringify(cfg)}, COL = ${JSON.stringify(SRC_COLORS)};
  var EXTRA = ['#1f77b4','#ff7f0e','#2ca02c','#d62728','#9467bd','#8c564b','#e377c2','#7f7f7f','#bcbd22','#17becf'];
  var st = { s: {}, order: [], now: 0, raf: 0 };
  function root() { return document.getElementById(ID); }
  function color(name) { return COL[name] || EXTRA[st.order.indexOf(name) % EXTRA.length]; }
  function sched() { if (!st.raf) st.raf = requestAnimationFrame(draw); }
  function fmtY(v, span) { var d = span >= 100 ? 0 : span >= 1 ? 2 : span >= 0.01 ? 4 : 6; return v.toFixed(d); }
  function fmtT(t) { var d = new Date(t); return ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2) + ':' + ('0' + d.getSeconds()).slice(-2); }
  function legend() {
    var r = root(); if (!r) return;
    var h = '';
    st.order.forEach(function (n) { h += '<span><i style="background:' + color(n) + '"></i>' + n + '</span>'; });
    r.querySelector('.lg').innerHTML = h || (CFG.title ? '' : '<span>esperando trades…</span>');
  }
  function draw() {
    st.raf = 0;
    var r = root(); if (!r) return;
    var cv = r.querySelector('canvas'), box = r.querySelector('.cv');
    var W = box.clientWidth, H = box.clientHeight; if (W < 10 || H < 10) return;
    var dpr = window.devicePixelRatio || 1;
    if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) { cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); }
    var g = cv.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, W, H);
    var fg = getComputedStyle(r).color || '#888';
    var tmax = st.now || Date.now(), tmin = tmax - CFG.windowMs;
    var lo = Infinity, hi = -Infinity;
    st.order.forEach(function (n) {
      var d = st.s[n];
      var k = 0; while (k < d.length - 1 && d[k + 1][0] < tmin) k++;   // conserva 1 punto previo a la ventana (continuidad del escalón)
      if (k > 0) d.splice(0, k);
      for (var i = 0; i < d.length; i++) { var y = d[i][1]; if (y < lo) lo = y; if (y > hi) hi = y; }
    });
    var L = 62, R = 8, T = 6, B = 18, pw = W - L - R, ph = H - T - B;
    g.font = '10px monospace'; g.fillStyle = fg; g.strokeStyle = fg;
    if (!isFinite(lo)) { g.globalAlpha = .6; g.fillText('sin trades en la ventana', L + 8, T + 14); g.globalAlpha = 1; return; }
    if (CFG.ymin0) lo = Math.min(0, lo);
    if (hi - lo < Math.abs(hi) * 1e-4 || hi === lo) { var c = (hi + lo) / 2, e = Math.max(Math.abs(c) * 5e-4, 1e-6); lo = c - e; hi = c + e; }
    var pad = (hi - lo) * 0.06; lo -= CFG.ymin0 && lo >= 0 ? 0 : pad; hi += pad;
    var X = function (t) { return L + (t - tmin) / (tmax - tmin) * pw; }, Y = function (v) { return T + (hi - v) / (hi - lo) * ph; };
    g.globalAlpha = .18; g.lineWidth = 1; g.beginPath();
    for (var q = 0; q <= 4; q++) { var yy = T + ph * q / 4; g.moveTo(L, yy); g.lineTo(L + pw, yy); }
    for (var q2 = 0; q2 <= 5; q2++) { var xx = L + pw * q2 / 5; g.moveTo(xx, T); g.lineTo(xx, T + ph); }
    g.stroke(); g.globalAlpha = .75;
    g.textAlign = 'right'; for (var q3 = 0; q3 <= 4; q3++) { var v = hi - (hi - lo) * q3 / 4; g.fillText(fmtY(v, hi - lo), L - 4, T + ph * q3 / 4 + 3); }
    g.textAlign = 'center'; for (var q4 = 0; q4 <= 5; q4++) { var tt = tmin + (tmax - tmin) * q4 / 5; g.fillText(fmtT(tt), Math.min(Math.max(L + pw * q4 / 5, L + 24), L + pw - 24), H - 5); }
    g.globalAlpha = 1; g.lineWidth = 1.4;
    g.save(); g.beginPath(); g.rect(L, T, pw, ph); g.clip();
    st.order.forEach(function (n) {
      var d = st.s[n]; if (!d.length) return;
      g.strokeStyle = color(n); g.fillStyle = color(n); g.beginPath();
      var px = X(d[0][0]), py = Y(d[0][1]); g.moveTo(px, py);
      for (var i = 1; i < d.length; i++) {
        var nx = X(d[i][0]), ny = Y(d[i][1]);
        if (CFG.step) g.lineTo(nx, py);
        g.lineTo(nx, ny); px = nx; py = ny;
      }
      g.stroke();
      g.beginPath(); g.arc(px, py, 2.2, 0, 6.283); g.fill();          // último dato real (sin extender el precio hacia adelante)
    });
    g.restore();
  }
  scope.$watch('msg', function (m) {
    if (!m || !m.payload) return;
    var p = m.payload;
    if (p.reset) { st.s = {}; st.order = []; legend(); }
    if (p.now) st.now = p.now;
    var pts = p.pts || [], newSeries = false;
    for (var i = 0; i < pts.length; i++) {
      var q = pts[i], d = st.s[q.s];
      if (!d) { d = st.s[q.s] = []; st.order.push(q.s); newSeries = true; }
      if (d.length && q.x < d[d.length - 1][0]) continue;          // descarta fuera de orden
      d.push([q.x, q.y]);
      if (d.length > CFG.maxPts) d.splice(0, d.length - CFG.maxPts);
    }
    if (newSeries) legend();
    sched();
  });
  var onRes = function () { sched(); };
  window.addEventListener('resize', onRes);
  scope.$on('$destroy', function () { window.removeEventListener('resize', onRes); if (st.raf) cancelAnimationFrame(st.raf); });
  setTimeout(function () { legend(); sched(); }, 0);
})(scope);
</script>`;
}
const tplBase = { type: 'ui_template', z: TAB, storeOutMessages: true, fwdInMessages: false, resendOnRefresh: true, templateScope: 'local', className: '', wires: [[]] };

const CARD_TPL = String.raw`<style>
.prrr-card{font-family:monospace;padding:0 4px;height:100%;overflow:hidden;box-sizing:border-box}
.prrr-card .hd{display:flex;align-items:baseline;gap:10px;flex-wrap:nowrap;white-space:nowrap;overflow:hidden}
.prrr-card .sym{font-size:17px;font-weight:bold}
.prrr-card .px{font-size:26px;font-weight:bold;line-height:1.1}
.prrr-card .up{color:#27ae60}.prrr-card .dn{color:#e74c3c}
.prrr-card .meta{opacity:.8;font-size:12px;overflow:hidden;text-overflow:ellipsis}
.prrr-card .grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:2px 5px;margin-top:3px;font-size:10.5px;line-height:1.2}
.prrr-card .cell{overflow:hidden;white-space:nowrap;text-overflow:ellipsis;border-left:2px solid rgba(128,128,128,.35);padding-left:4px}
.prrr-card .cell.perp{border-left-color:#8e7cc3}
.prrr-card .cell.off{opacity:.4}
.prrr-card .cell .n{opacity:.75}
.prrr-card .cell .p{font-weight:bold}
</style>
<div class="prrr-card" ng-if="msg.payload">
  <div class="hd">
    <span class="sym">{{msg.payload.symbol}}</span>
    <span class="px" ng-class="{'up': msg.payload.side==='BUY', 'dn': msg.payload.side==='SELL'}">{{msg.payload.price}}</span>
    <span class="meta">{{msg.payload.exchange}} · <b ng-class="{'up': msg.payload.side==='BUY', 'dn': msg.payload.side==='SELL'}">{{msg.payload.side || '?'}}</b> · edad {{msg.payload.age}} · <b>{{msg.payload.evs}}</b> ev/s · <b>{{msg.payload.perMin}}</b> trades/min</span>
  </div>
  <div class="grid">
    <div class="cell" ng-repeat="e in msg.payload.perEx track by e.ex" ng-class="{'perp': e.perp, 'off': e.off}" title="{{e.ex}}: {{e.pm}} trades reales en los últimos 60 s (/m = por minuto)">
      <span class="n">{{e.ex}}</span> <b>{{e.pm}}</b><span class="n">/m</span><br><span class="p" ng-class="{'up': e.side==='BUY', 'dn': e.side==='SELL'}">{{e.price}}</span> <span class="n">{{e.age}}</span>
    </div>
  </div>
</div>
<div class="prrr-card" ng-if="!msg.payload"><span class="meta">esperando trades…</span></div>`;

const TABLE_TPL = String.raw`<style>
.prrr-mk{display:flex;flex-wrap:wrap;gap:6px 14px;font-family:monospace;font-size:11.5px}
.prrr-mk .blk{flex:1 1 calc(50% - 14px);min-width:0;overflow:hidden}
.prrr-mk .ttl{font-size:13px;margin:2px 0}
.prrr-mk table{width:100%;border-collapse:collapse}
.prrr-mk th,.prrr-mk td{padding:1px 5px;text-align:right;border-bottom:1px solid rgba(128,128,128,.22);white-space:nowrap}
.prrr-mk th{opacity:.75;font-weight:bold}
.prrr-mk .l{text-align:left}
.prrr-mk .perp{color:#8e7cc3;font-weight:bold}
.prrr-mk .off{color:#888;text-decoration:line-through}.prrr-mk .connecting{color:#e6a700}.prrr-mk .down{color:#e74c3c}
.prrr-mk .zero{opacity:.45}
.prrr-mk .note{flex-basis:100%;opacity:.7;font-size:11px}
</style>
<div class="prrr-mk" ng-if="msg.payload">
  <div class="blk" ng-repeat="b in msg.payload.blocks track by b.sym">
    <div class="ttl"><b>{{b.sym}}</b> · {{b.rows.length}} mercados · <b>{{b.pm}}</b> trades/min · {{b.evs}} ev/s</div>
    <table>
      <tr><th class="l">fuente</th><th class="l">tipo</th><th class="l">par exacto</th><th class="l">canal</th><th>trades/min</th><th>ev/s</th><th>último</th><th>edad</th></tr>
      <tr ng-repeat="r in b.rows track by r.ex" ng-class="{'zero': !r.pm}">
        <td class="l" ng-class="r.state" title="WS: {{r.state}}"><b>{{r.ex}}</b></td><td class="l" ng-class="{'perp': r.type==='perp'}">{{r.type}}</td>
        <td class="l">{{r.market}}</td><td class="l">{{r.feed}}</td>
        <td><b>{{r.pm}}</b><span ng-if="r.partial">*</span></td><td>{{r.evs}}</td><td>{{r.price}}</td><td>{{r.age}}</td>
      </tr>
    </table>
  </div>
  <div class="note">trades/min = trades reales (fills) en los últimos 60 s · * = ventana aún incompleta · * en canal: binance-perp usa @aggTrade, 1 evento puede agrupar varios fills (se cuentan los fills reales) · fuente en rojo/amarillo/tachada = WS caído/conectando/apagado · nada se interpola ni se repite</div>
</div>
<div ng-if="!msg.payload" style="font-family:monospace">esperando estadísticas…</div>`;

const TECH_TPL = String.raw`<style>
.prrr-t{width:100%;border-collapse:collapse;font-family:monospace;font-size:12px}
.prrr-t th,.prrr-t td{padding:2px 6px;text-align:right;border-bottom:1px solid rgba(128,128,128,.25);white-space:nowrap}
.prrr-t th{font-weight:bold;opacity:.8}
.prrr-t .l{text-align:left}
.prrr-t .connected{color:#27ae60;font-weight:bold}.prrr-t .off{color:#888}
.prrr-t .connecting{color:#e6a700}.prrr-t .down{color:#e74c3c;font-weight:bold}
.prrr-t .err{color:#e67e22;max-width:420px;overflow:hidden;text-overflow:ellipsis}
.prrr-sum{font-family:monospace;font-size:13px;margin:2px 0 6px 0}
.prrr-sum span{display:inline-block;margin-right:18px}
</style>
<div ng-if="msg.payload" style="overflow-x:auto">
<div class="prrr-sum">
  <span><b>TOTAL</b> {{msg.payload.summary.totEvs}} ev/s (pico {{msg.payload.summary.peakTot}})</span>
  <span ng-repeat="y in msg.payload.summary.syms track by y.sym"><b>{{y.sym}}</b> {{y.evs}} ev/s · {{y.pm}} trades/min (pico {{y.peak}} ev/s)</span>
</div>
<div class="prrr-sum">
  <span><b>lag event-loop</b> {{msg.payload.summary.lagAvg}} / {{msg.payload.summary.lagMax}} ms (prom/máx)</span>
  <span><b>cola NR máx</b> {{msg.payload.summary.pipeMax}} ms</span>
  <span><b>exch→local prom</b> {{msg.payload.summary.latAvg}} ms</span>
  <span><b>render</b> {{msg.payload.summary.renderMs}} ms</span>
  <span><b>eventos</b> {{msg.payload.summary.total}} en {{msg.payload.summary.uptime}} s</span>
</div>
<table class="prrr-t">
<tr>
  <th class="l">Fuente</th><th class="l">WS</th><th>reconex</th><th>frames/s</th><th>ev/s</th>
  <th>exch→local ms prom/mín/máx</th><th>cola NR ms prom/máx</th><th>err parse</th><th>eventos</th><th class="l">último error</th>
</tr>
<tr ng-repeat="r in msg.payload.rows track by r.ex">
  <td class="l"><b>{{r.ex}}</b></td><td class="l" ng-class="r.state">{{r.state}}</td><td>{{r.reconnects}}</td><td>{{r.fps}}</td><td><b>{{r.evs}}</b></td>
  <td>{{r.latAvg}} / {{r.latMin}} / {{r.latMax}}</td><td>{{r.pipeAvg}} / {{r.pipeMax}}</td>
  <td>{{r.errs}}</td><td>{{r.total}}</td><td class="l err" title="{{r.err}}">{{r.err}}</td>
</tr>
</table>
</div>
<div ng-if="!msg.payload" style="font-family:monospace">esperando estadísticas…</div>`;

// 4 instrumentos idénticos: cabecera + precios por fuente (card, alto 3) + gráfico (alto 6)
SYMS.forEach((s, i) => {
  const S_ = s.toUpperCase();
  inGroup(gUi, Object.assign({}, tplBase, { id: 'prrr_ui_card_' + s, name: S_ + ' ticker', group: G[s].id, order: 1, width: 12, height: 2, format: CARD_TPL, x: UX, y: 200 + i * 40 }));
  inGroup(gUi, Object.assign({}, tplBase, {
    id: 'prrr_ui_chart_' + s, name: S_ + ' chart (canvas)', group: G[s].id, order: 2, width: 12, height: 7,
    format: chartTpl('prrr_cv_' + s, { windowMs: 300000, maxPts: 1500, step: true }), storeOutMessages: false, x: UX + 200, y: 200 + i * 40,
  }));
});
inGroup(gUi, Object.assign({}, tplBase, {
  id: 'prrr_ui_chart_evs', name: 'Eventos/s (canvas)', group: G.thr.id, order: 1, width: 12, height: 5,
  format: '<div style="font:12px monospace;opacity:.8">Eventos/s (1 s) — últimos 15 min</div><div style="height:calc(100% - 16px)">' + chartTpl('prrr_cv_evs', { windowMs: 900000, maxPts: 1000, step: false, ymin0: true, title: 'x' }) + '</div>',
  storeOutMessages: false, x: UX, y: 380,
}));
inGroup(gUi, Object.assign({}, tplBase, {
  id: 'prrr_ui_chart_fresh', name: 'Frescura (canvas)', group: G.thr.id, order: 2, width: 12, height: 5,
  format: '<div style="font:12px monospace;opacity:.8">Frescura (ms): lag event-loop · cola Node-RED · exchange→local — últimos 15 min</div><div style="height:calc(100% - 16px)">' + chartTpl('prrr_cv_fresh', { windowMs: 900000, maxPts: 1000, step: false, ymin0: true, title: 'x' }) + '</div>',
  storeOutMessages: false, x: UX + 200, y: 380,
}));
inGroup(gUi, Object.assign({}, tplBase, { id: 'prrr_ui_table', name: 'Tabla mercados (pares, trades/min)', group: G.perf.id, order: 1, width: 24, height: 8, format: TABLE_TPL, x: UX, y: 420 }));
inGroup(gUi, Object.assign({}, tplBase, { id: 'prrr_ui_tech', name: 'Métricas técnicas', group: G.tech.id, order: 1, width: 24, height: 7, format: TECH_TPL, x: UX + 200, y: 420 }));

// Salida hacia futuras etapas
inGroup(gOut, {
  id: 'prrr_stream_out', type: 'link out', z: TAB, name: 'MD STREAM →', mode: 'link', links: ['prrr_example_in'], x: UX - 20, y: 120, wires: [],
});
inGroup(gOut, {
  id: 'prrr_example_in', type: 'link in', z: TAB, name: 'ejemplo consumidor', links: ['prrr_stream_out'], x: UX + 160, y: 120, wires: [['prrr_example_dbg']],
});
inGroup(gOut, {
  id: 'prrr_example_dbg', type: 'debug', z: TAB, name: 'trades normalizados (activar para ver)', active: false, tosidebar: true, console: false, tostatus: false,
  complete: 'payload', targetType: 'msg', statusVal: '', statusType: 'auto', x: UX + 420, y: 120, wires: [],
});

// Controles globales (dashboard)
const GY = 160 + EXCHANGES.length * 110 + 40;
inGroup(gCtl, {
  id: 'prrr_render_init', type: 'inject', z: TAB, name: 'render inicial 500 ms',
  props: [{ p: 'payload' }], repeat: '', crontab: '', once: true, onceDelay: '1', topic: '', payload: '500', payloadType: 'num',
  x: 170, y: GY, wires: [['prrr_ui_render']],
});
inGroup(gCtl, {
  id: 'prrr_ui_render', type: 'ui_dropdown', z: TAB, name: 'Frecuencia de render', label: 'Render UI', tooltip: 'Cada cuánto se redibuja (no afecta la adquisición)', place: '',
  group: G.ctrl.id, order: 50, width: 6, height: 1, passthru: true, multiple: false,
  options: [100, 250, 500, 1000, 2000].map((v) => ({ label: v + ' ms', value: v, type: 'num' })),
  payload: '', topic: 'render_ms', topicType: 'str', className: '', x: 420, y: GY, wires: [[CORE]],
});
inGroup(gCtl, {
  id: 'prrr_ui_reset', type: 'ui_button', z: TAB, name: 'Reset stats', group: G.ctrl.id, order: 51, width: 3, height: 1, passthru: false,
  label: 'Reset stats', tooltip: 'Reinicia picos, contadores y gráficos', color: '', bgcolor: '', className: '', icon: 'fa-eraser',
  payload: 'reset', payloadType: 'str', topic: 'reset', topicType: 'str', x: 400, y: GY + 50, wires: [[CORE]],
});
inGroup(gCtl, {
  id: 'prrr_ui_reconnect', type: 'ui_button', z: TAB, name: 'Reconectar todos', group: G.ctrl.id, order: 52, width: 3, height: 1, passthru: false,
  label: 'Reconectar', tooltip: 'Fuerza reconexión de todos los WS habilitados', color: '', bgcolor: '', className: '', icon: 'fa-refresh',
  payload: 'reconnect', payloadType: 'str', topic: 'reconnect', topicType: 'str', x: 410, y: GY + 100, wires: [connectorIds.slice()],
});

// Errores
inGroup(gErr, {
  id: 'prrr_catch', type: 'catch', z: TAB, name: 'errores de este tab', scope: null, uncaught: false, x: 180, y: GY + 190, wires: [['prrr_catch_dbg']],
});
inGroup(gErr, {
  id: 'prrr_catch_dbg', type: 'debug', z: TAB, name: 'errores', active: true, tosidebar: true, console: false, tostatus: false,
  complete: 'error', targetType: 'msg', statusVal: '', statusType: 'auto', x: 400, y: GY + 190, wires: [],
});

// Comentario general
add({
  id: 'prrr_comment_main', type: 'comment', z: TAB, name: 'PRRR — Exchange WS → normalizador → MD BUS → MD CORE → (stream | dashboard)',
  info: 'Cada fuente: `autostart → switch dashboard → WS Connector (subflow) → normalizador → link out MD BUS`.\n\nFuente = exchange + tipo de mercado (spot o perp). Los perpetuos se muestran como fuentes separadas (ej. `binance-perp`) y nunca se mezclan con el spot.\n\nPara agregar una fuente: copiar un grupo, cambiar env vars de la instancia del subflow (EXCHANGE, WS_URL, SUBSCRIBE, PING_*), escribir su normalizador al formato común, sumar su entrada en META (On Start del core) y enlazar el link out al `MD BUS`.',
  x: 360, y: 60, wires: [],
});

// ---------------------------------------------------------------------------
// Bounds de los grupos del editor
// ---------------------------------------------------------------------------
const byId = Object.fromEntries(nodes.map((n) => [n.id, n]));
for (const g of editorGroups) {
  const ns = g.nodes.map((id) => byId[id]);
  const minX = Math.min(...ns.map((n) => n.x)), maxX = Math.max(...ns.map((n) => n.x));
  const minY = Math.min(...ns.map((n) => n.y)), maxY = Math.max(...ns.map((n) => n.y));
  g.x = minX - 120; g.y = minY - 45; g.w = maxX - minX + 250; g.h = maxY - minY + 80;
}
const all = [...nodes.filter((n) => n.type === 'subflow' || n.z === SF), ...nodes.filter((n) => n.type === 'tab'), ...editorGroups,
  ...nodes.filter((n) => n.type !== 'subflow' && n.z !== SF && n.type !== 'tab')];

const outFile = path.join(__dirname, '..', 'prrr-market-data-stage1c.json');
fs.writeFileSync(outFile, JSON.stringify(all, null, 2) + '\n');
console.log('OK', all.length, 'nodos ->', outFile);
