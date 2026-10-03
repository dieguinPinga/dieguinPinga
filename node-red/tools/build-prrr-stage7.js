#!/usr/bin/env node
// Genera node-red/prrr-market-data-stage7.json (flow importable en Node-RED).
// Etapa 7 = etapa 6b + CONCEPTITO LIVE (paper trader ZEC SMA39/76) con panel y marcadores en el gráfico ZEC de MERCADO.
// Etapa 6b = etapa 6 intacta + serie GMX (ZEC_GMX mid) en el gráfico ZEC de MERCADO. 100 % aditivo: no modifica nodos.
// Etapa 6 = etapa 5 intacta + ZEC_GMX: precio de ejecución GMX (oracle keeper) → gmx_price + diagnóstico.
// Etapa 5 = etapa 4 intacta + LABORATORIO histórico (sólo lectura de market_1s).
// Etapa 4 = etapa 3 intacta + analítica 25/50 (SMA, Δ, volumen, presión) sólo para observar.
// Etapa 3 = 2e intacta + persistencia de buckets 1 s en MariaDB (rama adicional desacoplada).
// 2e = 2d + ventana visible (5m/15m/1h/4h), intervalo de ploteo (100–1000 ms, min/máx preservados),
//      pausa / volver a vivo e historial de gráficos en 2 resoluciones. Sólo presentación.
// 2c = 2b + pestaña MERCADO (4 columnas precio + 4 deltas, pensada para 1920×1080 con Edge 67 %);
//      el resto del dashboard pasa a la pestaña DIAGNÓSTICO. Sólo presentación.
// Etapa 2 = etapa 1c intacta + NORMALIZADOR TEMPORAL 1 s + MEMORIA 30 min (módulo aditivo).
// Etapa 1c = 1b + más fuentes reales para GMX/XMR (perpetuos, Bitget, Hyperliquid) + dashboard 2×2.
// Mismos IDs que las etapas anteriores: al importar elegir "Replace".
// Uso: node node-red/tools/build-prrr-stage7.js
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
add({ id: 'prrr_ui_tab_mkt', type: 'ui_tab', name: 'MERCADO', icon: 'fa-line-chart', order: 1, disabled: false, hidden: false });
add({ id: UI_TAB, type: 'ui_tab', name: 'DIAGNÓSTICO', icon: 'fa-stethoscope', order: 2, disabled: false, hidden: false });
const G = {
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
  const cfg = Object.assign({ windowMs: 300000, maxPts: 1500, step: true, ymin0: false, title: '',
    staleMs: 60000,     // serie sin trades hace más de esto: no define la escala (se dibuja atenuada)
    padPct: 0.10,       // padding arriba y abajo
    shrinkMs: 1500      // constante de tiempo de la contracción suave (la expansión es inmediata)
  }, opts);
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
  var st = { s: {}, order: [], now: 0, raf: 0, yLo: null, yHi: null, lastDraw: 0, staleKey: '' };
  function root() { return document.getElementById(ID); }
  function color(name) { return COL[name] || EXTRA[st.order.indexOf(name) % EXTRA.length]; }
  function sched() { if (!st.raf) st.raf = requestAnimationFrame(draw); }
  function fmtY(v, span) { var d = span >= 100 ? 0 : span >= 1 ? 2 : span >= 0.01 ? 4 : 6; return v.toFixed(d); }
  function fmtT(t) { var d = new Date(t); return ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2) + ':' + ('0' + d.getSeconds()).slice(-2); }
  function legend() {
    var r = root(); if (!r) return;
    var h = '';
    var sl = st.staleKey ? st.staleKey.split(',') : [];
    st.order.forEach(function (n) { var sv = sl.indexOf(n) >= 0; h += '<span' + (sv ? ' style="opacity:.45" title="sin trades recientes: no define la escala"' : '') + '><i style="background:' + color(n) + '"></i>' + n + (sv ? ' (stale)' : '') + '</span>'; });
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
    // --- Autoescala Y (sólo presentación): min/max de los datos VISIBLES de series con datos actuales ---
    var lo = Infinity, hi = -Infinity, alo = Infinity, ahi = -Infinity, stale = {};
    st.order.forEach(function (n) {
      var d = st.s[n];
      var k = 0; while (k < d.length && d[k][0] < tmin) k++;           // lo que salió de la ventana deja de existir para la vista
      if (k > 0) d.splice(0, k);
      if (!d.length) return;
      var fresh = tmax - d[d.length - 1][0] <= CFG.staleMs;
      if (!fresh) stale[n] = true;
      for (var i = 0; i < d.length; i++) {
        var y = d[i][1];
        if (y < alo) alo = y; if (y > ahi) ahi = y;
        if (fresh) { if (y < lo) lo = y; if (y > hi) hi = y; }
      }
    });
    if (!isFinite(lo)) { lo = alo; hi = ahi; }                        // todas stale: usar igualmente lo visible
    var sk = Object.keys(stale).sort().join(','); if (sk !== st.staleKey) { st.staleKey = sk; legend(); }
    var L = 62, R = 8, T = 6, B = 18, pw = W - L - R, ph = H - T - B;
    g.font = '10px monospace'; g.fillStyle = fg; g.strokeStyle = fg;
    if (!isFinite(lo)) { st.yLo = st.yHi = null; g.globalAlpha = .6; g.fillText('sin trades en la ventana', L + 8, T + 14); g.globalAlpha = 1; return; }
    if (CFG.ymin0) lo = Math.min(0, lo);
    var ctr = (hi + lo) / 2, minSpan = CFG.ymin0 ? 1 : Math.max(Math.abs(ctr) * 2e-5, 1e-9);   // p.ej. BTC 60000 → rango mínimo 1.2 USD
    if (hi - lo < minSpan) { lo = ctr - minSpan / 2; hi = ctr + minSpan / 2; if (CFG.ymin0 && lo < 0 && ahi >= 0) { hi -= lo; lo = 0; } }
    var pad = (hi - lo) * CFG.padPct;
    var tLo = CFG.ymin0 && lo >= 0 ? lo : lo - pad, tHi = hi + pad;
    // expandir inmediatamente; contraer suave (exponencial) cuando un extremo viejo sale de la ventana
    var nowMs = (window.performance && performance.now()) || Date.now();
    var dt = st.lastDraw ? Math.min(nowMs - st.lastDraw, 1000) : 1000; st.lastDraw = nowMs;
    var a = 1 - Math.exp(-dt / CFG.shrinkMs);
    if (st.yLo === null || tLo < st.yLo) st.yLo = tLo; else st.yLo += (tLo - st.yLo) * a;
    if (st.yHi === null || tHi > st.yHi) st.yHi = tHi; else st.yHi += (tHi - st.yHi) * a;
    var eps = (tHi - tLo) * 0.002;
    var animating = Math.abs(st.yLo - tLo) > eps || Math.abs(st.yHi - tHi) > eps;
    if (!animating) { st.yLo = tLo; st.yHi = tHi; }
    lo = st.yLo; hi = st.yHi;
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
      g.globalAlpha = stale[n] ? 0.35 : 1;                              // stale: se ve, pero no define la escala
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
    g.restore(); g.globalAlpha = 1;
    if (animating) sched();                                             // sigue contrayendo cuadro a cuadro hasta converger
  }
  scope.$watch('msg', function (m) {
    if (!m || !m.payload) return;
    var p = m.payload;
    if (p.reset) { st.s = {}; st.order = []; st.yLo = st.yHi = null; st.staleKey = ''; legend(); }
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
// ===========================================================================
// Pestaña MERCADO — vista principal diseñada para 1920×1080 con Edge al 67 %
// (viewport CSS ≈ 2866 × 1350 px). Un único widget con grilla CSS propia:
//   FILA 1: BTC | ZEC | GMX | XMR  — precio PRRR (autoescala Y)
//   FILA 2: BTC Δ | ZEC Δ | GMX Δ | XMR Δ — delta BUY−SELL USD · 60 s
// 4 columnas fijas (grid repeat(4, minmax(0,1fr))): nunca hacen wrap. Sólo presentación.
// ===========================================================================
const MKT_SYMS = ['BTC', 'ZEC', 'GMX', 'XMR'];

function mktTpl() {
  const cols = MKT_SYMS.map((s) => `
  <div class="col" id="mkt_${s}">
    <div class="hd"><span class="sym">${s}</span><span class="px">—</span><span class="meta">esperando trades…</span></div>
    <div class="ex"></div>
    <div class="lg"></div>
    <div class="pcv"><canvas></canvas><div class="hist"></div></div>
    <div class="an" id="mkta_${s}"><span class="w">25/50: calentando…</span></div>
    <div class="dwrap" id="mktd_${s}">${s === 'ZEC' ? '<div class="cx" id="mktcx_ZEC"></div>' : ''}<div class="dhd"><b>${s}</b> Δ BUY−SELL USD · últimos 60 s</div><div class="dcv"><canvas></canvas></div></div>
  </div>`).join('');
  return String.raw`<style>
/* MERCADO = capa fija a pantalla completa (100vw) debajo de la barra superior; ningún padre limita su ancho. */
#prrr_mkt{position:fixed;left:0;right:0;bottom:0;top:48px;width:100vw;max-width:none;margin:0;box-sizing:border-box;padding:4px 8px 8px 8px;z-index:50;
  display:flex;flex-direction:column;gap:6px;overflow:hidden;font-family:monospace}
#prrr_mkt .bar{flex:none;height:34px;display:flex;align-items:center;gap:18px;font-size:14px;white-space:nowrap;overflow:hidden}
#prrr_mkt .bar .grp{display:flex;align-items:center;gap:4px}
#prrr_mkt .bar button{font:inherit;font-size:13px;padding:3px 9px;border:1px solid rgba(128,128,128,.5);border-radius:3px;background:transparent;color:inherit;cursor:pointer}
#prrr_mkt .bar button.on{background:#1f77b4;border-color:#1f77b4;color:#fff}
#prrr_mkt .bar button.pz.on{background:#e6a700;border-color:#e6a700}
#prrr_mkt .bar .hint{opacity:.65;font-size:12px}
#prrr_mkt .bar .st{font-weight:bold}
#prrr_mkt .bar .st.live{color:#27ae60}#prrr_mkt .bar .st.paused{color:#e6a700}
#prrr_mkt .grid4{flex:1;min-height:0;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));grid-template-rows:minmax(0,1fr);gap:8px}
#prrr_mkt .col{display:flex;flex-direction:column;min-width:0;min-height:0;border:1px solid rgba(128,128,128,.3);border-radius:4px;padding:6px 8px;box-sizing:border-box}
#prrr_mkt .hd{display:flex;align-items:baseline;gap:10px;white-space:nowrap;overflow:hidden;height:42px;flex:none}
#prrr_mkt .sym{font-size:22px;font-weight:bold}
#prrr_mkt .px{font-size:34px;font-weight:bold;line-height:1.1}
#prrr_mkt .meta{font-size:15px;opacity:.85;overflow:hidden;text-overflow:ellipsis}
#prrr_mkt .up{color:#27ae60}#prrr_mkt .dn{color:#e74c3c}
#prrr_mkt .ex{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:3px 8px;margin-top:5px;font-size:14px;line-height:1.25;height:76px;overflow:hidden;align-content:start;flex:none}
#prrr_mkt .ex .c{overflow:hidden;white-space:nowrap;text-overflow:ellipsis;border-left:3px solid #888;padding-left:4px}
#prrr_mkt .ex .c.st{opacity:.45}
#prrr_mkt .ex .n{opacity:.8}
#prrr_mkt .lg{font-size:13px;line-height:18px;margin-top:5px;height:36px;overflow:hidden;flex:none}
#prrr_mkt .lg span{margin-right:11px;white-space:nowrap;display:inline-block}
#prrr_mkt .lg i{display:inline-block;width:12px;height:4px;margin-right:3px;vertical-align:middle}
#prrr_mkt .pcv{flex:72 1 0;position:relative;min-height:0;margin-top:2px}
#prrr_mkt .pcv .hist{position:absolute;left:90px;top:8px;font-size:12px;opacity:.75;pointer-events:none}
#prrr_mkt .an{flex:none;height:40px;display:grid;grid-template-columns:1fr 1fr .8fr;gap:10px;margin-top:6px;padding-top:4px;border-top:1px solid rgba(128,128,128,.35);font-size:13px;line-height:18px;white-space:nowrap;overflow:hidden}
#prrr_mkt .an .w{opacity:.65}
#prrr_mkt .an b.pos{color:#27ae60}#prrr_mkt .an b.neg{color:#e74c3c}
#prrr_mkt .dwrap{flex:28 1 0;display:flex;flex-direction:column;min-height:0;border-top:1px solid rgba(128,128,128,.35);margin-top:6px;padding-top:4px}
#prrr_mkt .dcv{flex:1;position:relative;min-height:0}
#prrr_mkt canvas{position:absolute;left:0;top:0;width:100%;height:100%}
#prrr_mkt .dhd{font-size:14px;white-space:nowrap;overflow:hidden}
/* CONCEPTITO LIVE (etapa 7): zona compacta del panel ZEC, entre las métricas 25/50 y el Δ BUY−SELL */
#prrr_mkt .cx{flex:none;font-size:12px;line-height:18px;padding:0 0 5px 0;margin-bottom:4px;border-bottom:1px solid rgba(128,128,128,.35);white-space:nowrap;overflow:hidden}
#prrr_mkt .cx .t{font-weight:bold;letter-spacing:.5px}
#prrr_mkt .cx .w{opacity:.65}
#prrr_mkt .cx .cxst{display:inline-block;min-width:66px;padding:0 5px;border-radius:3px;font-weight:bold;color:#fff;background:#78909c;text-align:center}
#prrr_mkt .cx .cxst.L{background:#00a152}#prrr_mkt .cx .cxst.S{background:#d50000}#prrr_mkt .cx .cxst.A{background:#e6a700}
#prrr_mkt .cx b.pos{color:#27ae60}#prrr_mkt .cx b.neg{color:#e74c3c}
#prrr_mkt .cx i.sw{display:inline-block;width:14px;height:3px;vertical-align:middle;margin:0 3px 0 6px}
#prrr_mkt .cx .cxpg{display:inline-block;width:48px;height:6px;background:rgba(128,128,128,.25);vertical-align:middle;margin-left:8px;border-radius:3px;overflow:hidden}
#prrr_mkt .cx .cxpg i{display:block;height:100%;background:#1f77b4}
</style>
<div id="prrr_mkt">
  <div class="bar">
    <span class="grp"><b>Ventana visible</b>
      <button data-w="300000">5 min</button><button data-w="900000">15 min</button><button data-w="3600000">1 h</button><button data-w="14400000">4 h</button></span>
    <span class="grp"><b>Ploteo</b>
      <button data-p="100">100 ms</button><button data-p="250">250 ms</button><button data-p="500">500 ms</button><button data-p="1000">1000 ms</button>
      <span class="hint">= cada cuánto se envían puntos (min · máx · último del intervalo: no se pierden picos) · <span class="rd"></span></span></span>
    <span class="grp"><button class="pz">⏸ Pausar</button><button class="lv">● Volver a vivo</button></span>
    <span class="st live">EN VIVO</span>
    <span class="hint">Δ BUY−SELL: siempre los últimos 60 s (buckets de 1 s), independiente de la ventana</span>
  </div>
  <div class="grid4">` + cols + String.raw`
  </div>
</div>
<script>
(function (scope) {
  var SYMS = ` + JSON.stringify(MKT_SYMS) + `, COL = ` + JSON.stringify(SRC_COLORS) + String.raw`;
  var CFG = { staleMs: 60000, padPct: 0.10, shrinkMs: 1500,
              tiers: { A: { slotMs: 250, cap: 3600 }, B: { slotMs: 5000, cap: 2880 } } };   // igual que el servidor
  var EXTRA = ['#1f77b4','#ff7f0e','#2ca02c','#d62728','#9467bd','#8c564b','#e377c2','#7f7f7f','#bcbd22','#17becf'];
  // Historial de ploteo del navegador: vive en window → sobrevive a cambios de pestaña del Dashboard
  var G = window.__prrrPlot || (window.__prrrPlot = { series: {}, order: {}, now: 0, plotMs: 250, snapAsked: false });
  var UI = window.__prrrUI || (window.__prrrUI = { windowMs: 900000, paused: false, frozenAt: 0 });
  try { var w0 = +localStorage.getItem('prrr_window_ms'); if ([300000, 900000, 3600000, 14400000].indexOf(w0) >= 0 && !window.__prrrUIinit) UI.windowMs = w0; } catch (e) {}
  window.__prrrUIinit = true;
  SYMS.forEach(function (s) { if (!G.series[s]) { G.series[s] = {}; G.order[s] = []; } });

  // SMA 25/50 por segundo (4 h) — vive en window: sobrevive a cambios de pestaña
  var SMA = window.__prrrSMA || (window.__prrrSMA = {});
  SYMS.forEach(function (s) { if (!SMA[s]) SMA[s] = { cap: 14400, idx: new Float64Array(14400).fill(-1), s25: new Float64Array(14400), s50: new Float64Array(14400) }; });
  function smaPut(sym, sec, a, b) { var H = SMA[sym], k = sec % H.cap; H.idx[k] = sec; H.s25[k] = a == null ? NaN : a; H.s50[k] = b == null ? NaN : b; }
  // ---------- CONCEPTITO LIVE (etapa 7, sólo ZEC) — datos en window.__prrrCX (los escribe el nodo "CONCEPTITO → navegador") ----------
  var CXC = { f: '#00acc1', s: '#c2185b' };              // SMA39 cian continua · SMA76 magenta a rayas
  var cxTimer = 0, cxRev = -1, cxSec = -1;
  function cxMoney(v) { var a = Math.abs(v); return (v > 0 ? '+' : v < 0 ? '−' : '') + '$' + a.toFixed(2); }
  function cxPx(v) { return v == null ? '—' : '$' + (v >= 100 ? v.toFixed(2) : v.toFixed(3)); }
  function cxMS(ms) { ms = Math.max(0, ms); var t = Math.floor(ms / 1000); return two(Math.floor(t / 60)) + ':' + two(t % 60); }
  function cxPanel() {
    var el = $('mktcx_ZEC'); if (!el) return;
    var X = window.__prrrCX, st = X && X.state, sec = Math.floor((G.now || 0) / 1000);
    if ((X ? X.rev : -1) === cxRev && sec === cxSec) return;
    cxRev = X ? X.rev : -1; cxSec = sec;
    var c = (X && X.cfg) || { fast: 39, slow: 76, margin: 1000, lev: 3, exposure: 3000, tp: 32.1, sl: -84.25, timeoutMin: 18 };
    var h = '<div><span class="t">CONCEPTITO LIVE</span> <span class="w">SMA</span><i class="sw" style="background:' + CXC.f + '"></i>' + c.fast
      + '<i class="sw" style="background:' + CXC.s + '"></i>' + c.slow + ' <span class="w">·</span> $' + c.margin + ' x' + c.lev + ' · EXP $' + c.exposure
      + ' <span class="w">·</span> TP ' + cxMoney(c.tp) + ' · SL ' + cxMoney(c.sl) + ' · TO ' + c.timeoutMin + 'm</div>';
    if (!st) { el.innerHTML = h + '<div>ESTADO: <span class="cxst A">SIN DATOS</span> <span class="w">esperando el motor Conceptito…</span></div><div>&nbsp;</div>'; return; }
    var p = st.pos, l2;
    if (st.phase !== 'OPERANDO') l2 = '<span class="cxst A">RESTAURANDO</span> <span class="w">leyendo estado en MariaDB' + (st.db && st.db.err ? ': ' + esc(String(st.db.err)).slice(0, 90) : '…') + '</span>';
    else if (p) {
      var el_ = (G.now || 0) - p.entryT, tot = p.toMin * 60000;
      l2 = '<span class="cxst ' + (p.side === 'LONG' ? 'L' : 'S') + '">' + p.side + '</span> <span class="w">Entrada</span> ' + cxPx(p.entryPx) + ' <span class="w">Actual</span> ' + cxPx(p.lastPx)
        + ' <span class="w">PnL</span> <b class="' + (p.pnl >= 0 ? 'pos' : 'neg') + '">' + cxMoney(p.pnl) + '</b> <span class="w">Tiempo</span> ' + cxMS(el_) + ' / ' + cxMS(tot)
        + '<span class="cxpg"><i style="width:' + Math.min(100, Math.max(0, el_ / tot * 100)).toFixed(1) + '%"></i></span>';
    } else if (st.pend) l2 = '<span class="cxst ' + (st.pend.side === 'LONG' ? 'L' : 'S') + '">SEÑAL ' + st.pend.side + '</span> <span class="w">cruce SMA' + c.fast + '/' + c.slow + ' confirmado · entra al close del próximo segundo</span>';
    else if (st.sS == null) l2 = '<span class="cxst A">CALENTANDO</span> <span class="w">SMA ' + st.warm + '/' + c.slow + ' s</span>';
    else {
      var d = st.sF - st.sS, lc = st.lastClosed;
      l2 = '<span class="cxst">ESPERANDO</span> <span class="w">SMA' + c.fast + (d >= 0 ? ' > ' : ' < ') + 'SMA' + c.slow + ' (Δ ' + (d >= 0 ? '+' : '−') + Math.abs(d).toFixed(4) + ')</span>'
        + (lc ? ' <span class="w">· última</span> ' + lc.side + ' ' + (lc.reason === 'TIMEOUT' ? 'TO' : lc.reason) + ' <b class="' + (lc.net >= 0 ? 'pos' : 'neg') + '">' + cxMoney(lc.net) + '</b>' : '');
    }
    var T = st.totals || {};
    el.innerHTML = h + '<div>ESTADO: ' + l2 + '</div><div><span class="w">ACUM</span> OPS <b>' + (T.ops || 0) + '</b> · TP <b>' + (T.tp || 0) + '</b> · SL <b>' + (T.sl || 0)
      + '</b> · TO <b>' + (T.to || 0) + '</b> · NETO <b class="' + ((T.net || 0) >= 0 ? 'pos' : 'neg') + '">' + cxMoney(T.net || 0) + '</b> · FEES $' + (T.fees || 0).toFixed(2)
      + (st.db && !st.db.ok && st.db.err ? ' <span style="color:#e74c3c">· DB: ' + esc(String(st.db.err)).slice(0, 60) + '</span>' : '') + '</div>';
  }
  function cxMarks(g, CX, tmin, tmax, mpc, L, T, pw, ph, Y) {   // ▲ LONG · ▼ SHORT · TP / SL / TO · niveles de la posición abierta
    var Xt = function (t) { return L + (t - tmin) / mpc; };
    var trs = CX.trades || [], st = CX.state || {};
    g.font = 'bold 10px monospace'; g.textAlign = 'center'; g.textBaseline = 'middle';
    for (var i = 0; i < trs.length; i++) {
      var tr = trs[i], te = tr.entryT + 1000, op = tr.exitT == null, tx = op ? tmax : tr.exitT + 1000;
      if (tx < tmin || te > tmax) continue;
      var xe = Xt(te), ye = Y(tr.entryPx), up = tr.side === 'LONG';
      g.globalAlpha = .55; g.lineWidth = 1; g.setLineDash([3, 3]);
      g.strokeStyle = op ? (up ? '#00a152' : '#d50000') : (tr.net >= 0 ? '#00a152' : '#d50000');
      g.beginPath(); g.moveTo(xe, ye); g.lineTo(Xt(tx), op ? ye : Y(tr.exitPx)); g.stroke(); g.setLineDash([]); g.globalAlpha = 1;
      if (te >= tmin) {
        g.fillStyle = up ? '#00a152' : '#d50000'; g.strokeStyle = '#ffffff'; g.lineWidth = 1.5; g.beginPath();
        if (up) { g.moveTo(xe, ye + 3); g.lineTo(xe - 6, ye + 13); g.lineTo(xe + 6, ye + 13); } else { g.moveTo(xe, ye - 3); g.lineTo(xe - 6, ye - 13); g.lineTo(xe + 6, ye - 13); }
        g.closePath(); g.stroke(); g.fill();
      }
      if (!op && tx >= tmin && tx <= tmax) {
        var xx = Xt(tx), yx = Y(tr.exitPx), lab = tr.reason === 'TIMEOUT' ? 'TO' : tr.reason, col = tr.reason === 'TP' ? '#00a152' : tr.reason === 'SL' ? '#d50000' : '#607d8b';
        g.fillStyle = col; g.strokeStyle = '#ffffff'; g.lineWidth = 1.5; g.beginPath(); g.rect(xx - 11, yx - 7, 22, 14); g.stroke(); g.fill();
        g.fillStyle = '#ffffff'; g.fillText(lab, xx, yx + 0.5);
      }
    }
    if (st.pos) {
      var p = st.pos, xs = Math.max(L, Xt(p.entryT + 1000)), xr = L + pw;
      [[p.tpPx, '#00a152', 'TP'], [p.slPx, '#d50000', 'SL']].forEach(function (lv) {
        var y = Y(lv[0]), inR = y >= T && y <= T + ph;
        if (inR) { g.globalAlpha = .7; g.strokeStyle = lv[1]; g.lineWidth = 1; g.setLineDash([2, 4]); g.beginPath(); g.moveTo(xs, y); g.lineTo(xr, y); g.stroke(); g.setLineDash([]); }
        var yl = inR ? y - 7 : (y < T ? T + 8 : T + ph - 8), lab = lv[2] + (inR ? '' : (y < T ? ' ▲ ' : ' ▼ ') + cxPx(lv[0]));
        g.globalAlpha = .9; g.fillStyle = lv[1]; g.textAlign = 'right'; g.fillText(lab, xr - 2, yl); g.textAlign = 'center'; g.globalAlpha = 1;
      });
    }
    g.textBaseline = 'alphabetic'; g.textAlign = 'left';
  }
  function sgn(v) { return v > 0 ? 'pos' : v < 0 ? 'neg' : ''; }
  function money(v) { var a = Math.abs(v), s = v < 0 ? '−' : v > 0 ? '+' : ''; return s + '$' + (a >= 1e6 ? (a / 1e6).toFixed(2) + 'M' : a >= 1e3 ? (a / 1e3).toFixed(1) + 'k' : a.toFixed(0)); }
  function vol(v) { return '$' + (v >= 1e6 ? (v / 1e6).toFixed(2) + 'M' : v >= 1e3 ? (v / 1e3).toFixed(1) + 'k' : v.toFixed(0)); }
  function pct(v, d) { return (v > 0 ? '+' : v < 0 ? '−' : '') + Math.abs(v).toFixed(d) + '%'; }
  function anStrip(sym, r) {
    var el = document.getElementById('mkta_' + sym); if (!el || !r) return;
    var w = function (n, d, v, p) { return '<span><span class="w">' + n + (r.seconds < +n.slice(0, 2) ? ' (' + r.seconds + 's)' : '') + '</span> Δ <b class="' + sgn(d) + '">' + money(d) + '</b> · VOL ' + vol(v) + '<br><span class="w">presión</span> <b class="' + sgn(p) + '">' + pct(p * 100, 1) + '</b></span>'; };
    el.innerHTML = w('25s', r.delta_25, r.volume_25, r.pressure_25) + w('50s', r.delta_50, r.volume_50, r.pressure_50)
      + '<span><span class="w">SMA25−SMA50</span><br>' + (r.price_sma_spread_pct == null ? '<span class="w">calentando</span>' : '<b class="' + sgn(r.price_sma_spread_pct) + '">' + pct(r.price_sma_spread_pct, 3) + '</b>') + '</span>';
  }
  function $(id) { return document.getElementById(id); }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function fmtY(v, span) { var d = span >= 100 ? 0 : span >= 1 ? 2 : span >= 0.01 ? 4 : 6; return v.toFixed(d); }
  function two(n) { return ('0' + n).slice(-2); }
  function fmtT(t, secs) { var d = new Date(t); return two(d.getHours()) + ':' + two(d.getMinutes()) + (secs ? ':' + two(d.getSeconds()) : ''); }
  function fmtDur(ms) { var m = Math.floor(ms / 60000); return m >= 60 ? Math.floor(m / 60) + ' h ' + (m % 60) + ' min' : m >= 1 ? m + ' min' : Math.round(ms / 1000) + ' s'; }
  function fmtU(v) { var a = Math.abs(v); return a >= 1e6 ? (v / 1e6).toFixed(2) + 'M' : a >= 1e3 ? (v / 1e3).toFixed(1) + 'k' : v.toFixed(0); }
  function fit(cv, box) {
    var W = box.clientWidth, H = box.clientHeight; if (W < 10 || H < 10) return null;
    var dpr = window.devicePixelRatio || 1;
    if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) { cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); }
    var g = cv.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, W, H);
    return { g: g, W: W, H: H };
  }
  // ---------- historial por serie: 2 niveles, typed arrays de tamaño fijo ----------
  function newTier(d) { return { slotMs: d.slotMs, cap: d.cap, idx: new Float64Array(d.cap).fill(-1), lo: new Float64Array(d.cap), hi: new Float64Array(d.cap), last: new Float64Array(d.cap) }; }
  function getSer(sym, src) {
    var S = G.series[sym][src];
    if (!S) { S = G.series[sym][src] = { A: newTier(CFG.tiers.A), B: newTier(CFG.tiers.B), lastT: 0, first: Infinity }; G.order[sym].push(src); }
    return S;
  }
  function tadd(T, t, y, isLast) {
    var s = Math.floor(t / T.slotMs), k = s % T.cap;
    if (T.idx[k] !== s) { T.idx[k] = s; T.lo[k] = y; T.hi[k] = y; T.last[k] = y; }
    else { if (y < T.lo[k]) T.lo[k] = y; if (y > T.hi[k]) T.hi[k] = y; if (isLast) T.last[k] = y; }
  }
  function ingest(sym, pts) {
    for (var i = 0; i < pts.length; i++) {
      var q = pts[i], S = getSer(sym, q.s), isLast = q.x >= S.lastT;
      tadd(S.A, q.x, q.y, isLast); tadd(S.B, q.x, q.y, isLast);
      if (isLast) S.lastT = q.x;
      if (q.x < S.first) S.first = q.x;
    }
  }
  function loadSnapshot(snap) {
    SYMS.forEach(function (sym) {
      var src = snap.syms[sym] || {};
      Object.keys(src).forEach(function (name) {
        var o = src[name], S = getSer(sym, name);
        ['A', 'B'].forEach(function (tn) {
          var T = S[tn], a = o[tn] || [];
          for (var i = 0; i < a.length; i += 4) {
            var s = a[i], k = s % T.cap;
            if (T.idx[k] > s) continue;                   // ya hay algo más nuevo del navegador
            T.idx[k] = s; T.lo[k] = a[i + 1]; T.hi[k] = a[i + 2]; T.last[k] = a[i + 3];
            var t = s * T.slotMs; if (t < S.first) S.first = t;
          }
        });
        if (o.lastT > S.lastT) S.lastT = o.lastT;
      });
    });
    if (snap.now > G.now) G.now = snap.now;
  }

  // ---------- gráfico de precio ----------
  function PriceChart(sym) {
    var root = $('mkt_' + sym), self = this;
    this.yLo = null; this.yHi = null; this.lastDraw = 0; this.staleKey = null; this.raf = 0; this.win = 0; this.buf = {};
    this.color = function (n) { return COL[n] || EXTRA[G.order[sym].indexOf(n) % EXTRA.length]; };
    this.sched = function () { if (!self.raf) self.raf = requestAnimationFrame(function () { self.raf = 0; self.draw(); }); };
    this.legend = function (stale) {
      var h = '';
      G.order[sym].forEach(function (n) { var sv = !!stale[n]; h += '<span' + (sv ? ' style="opacity:.4"' : '') + '><i style="background:' + self.color(n) + '"></i>' + esc(n) + (sv ? ' (stale)' : '') + '</span>'; });
      h += '<span><b>━ SMA25s</b></span><span><b>╍ SMA50s</b></span>';
      root.querySelector('.lg').innerHTML = h;
    };
    this.draw = function () {
      var t0 = performance.now();
      var r = fit(root.querySelector('.pcv canvas'), root.querySelector('.pcv')); if (!r) return;
      var g = r.g, W = r.W, H = r.H, fg = getComputedStyle(root).color || '#888';
      var L = 82, R = 8, T = 8, B = 22, pw = Math.max(10, Math.floor(W - L - R)), ph = H - T - B;
      var win = UI.windowMs, tmax = UI.paused ? UI.frozenAt : G.now, tmin = tmax - win;
      if (self.win !== win) { self.win = win; self.yLo = self.yHi = null; }
      // nivel de detalle: A (250 ms) si cubre el rango visible; si no, B (5 s)
      var A = CFG.tiers.A, tier = (win <= A.slotMs * A.cap && tmin >= G.now - A.slotMs * (A.cap - 2)) ? 'A' : 'B';
      var msPerCol = win / pw, gapMs = Math.max(CFG.staleMs, 3 * CFG.tiers[tier].slotMs, 3 * msPerCol);
      // 1) agregación por columna de píxel: min / max / último (nunca más puntos que la resolución útil)
      var lo = Infinity, hi = -Infinity, alo = Infinity, ahi = -Infinity, stale = {}, earliest = Infinity, anyData = false;
      var srcs = G.order[sym], cols = {};
      for (var si = 0; si < srcs.length; si++) {
        var n = srcs[si], S = G.series[sym][n], Tt = S[tier];
        var b = self.buf[n]; if (!b || b.lo.length !== pw) b = self.buf[n] = { lo: new Float64Array(pw), hi: new Float64Array(pw), last: new Float64Array(pw), has: new Uint8Array(pw) };
        b.has.fill(0);
        var s0 = Math.floor(tmin / Tt.slotMs), s1 = Math.floor(tmax / Tt.slotMs), cnt = 0, lastT = -Infinity;
        if (s1 - s0 + 1 > Tt.cap) s0 = s1 - Tt.cap + 1;
        for (var s = s0; s <= s1; s++) {
          var k = s % Tt.cap; if (Tt.idx[k] !== s) continue;
          var tc = s * Tt.slotMs + Tt.slotMs / 2; if (tc > tmax) tc = tmax;
          var c = Math.floor((tc - tmin) / msPerCol); if (c < 0) continue; if (c >= pw) c = pw - 1;
          var l = Tt.lo[k], h = Tt.hi[k], z = Tt.last[k];
          if (!b.has[c]) { b.has[c] = 1; b.lo[c] = l; b.hi[c] = h; b.last[c] = z; }
          else { if (l < b.lo[c]) b.lo[c] = l; if (h > b.hi[c]) b.hi[c] = h; b.last[c] = z; }
          cnt++; lastT = s * Tt.slotMs;
          if (s * Tt.slotMs < earliest) earliest = Math.max(s * Tt.slotMs, tmin);
        }
        if (!cnt) continue;
        anyData = true; cols[n] = b;
        var fresh = Math.min(S.lastT, tmax) >= tmax - CFG.staleMs && lastT >= tmax - CFG.staleMs - Tt.slotMs;
        if (!fresh) stale[n] = true;
        for (var c2 = 0; c2 < pw; c2++) if (b.has[c2]) {
          if (b.lo[c2] < alo) alo = b.lo[c2]; if (b.hi[c2] > ahi) ahi = b.hi[c2];
          if (fresh) { if (b.lo[c2] < lo) lo = b.lo[c2]; if (b.hi[c2] > hi) hi = b.hi[c2]; }
        }
      }
      // SMA 25/50: 1 valor por segundo → último valor por columna de píxel
      var HS = SMA[sym], sm = self.smaBuf; if (!sm || sm.a.length !== pw) sm = self.smaBuf = { a: new Float64Array(pw), b: new Float64Array(pw), has: new Uint8Array(pw) };
      sm.has.fill(0);
      var q0 = Math.max(Math.floor(tmin / 1000), Math.floor(tmax / 1000) - HS.cap + 1), q1 = Math.floor(tmax / 1000);
      for (var qs = q0; qs <= q1; qs++) {
        var kq = qs % HS.cap; if (HS.idx[kq] !== qs) continue;
        var cq = Math.floor((qs * 1000 + 500 - tmin) / msPerCol); if (cq < 0) continue; if (cq >= pw) cq = pw - 1;
        sm.has[cq] = 1; sm.a[cq] = HS.s25[kq]; sm.b[cq] = HS.s50[kq];
        if (anyData && isFinite(lo)) { var va = HS.s25[kq], vb = HS.s50[kq];
          if (va === va) { if (va < lo) lo = va; if (va > hi) hi = va; } if (vb === vb) { if (vb < lo) lo = vb; if (vb > hi) hi = vb; } }
      }
      // CONCEPTITO (etapa 7, sólo ZEC): SMA39/76 por segundo → último valor por columna; entran en la autoescala igual que SMA25/50
      var CX = sym === 'ZEC' ? window.__prrrCX : null, cxs = null;
      if (CX) {
        cxs = self.cxBuf; if (!cxs || cxs.a.length !== pw) cxs = self.cxBuf = { a: new Float64Array(pw), b: new Float64Array(pw), has: new Uint8Array(pw) };
        cxs.has.fill(0);
        var x0 = Math.max(Math.floor(tmin / 1000), Math.floor(tmax / 1000) - CX.cap + 1), x1 = Math.floor(tmax / 1000);
        for (var xq = x0; xq <= x1; xq++) {
          var kx = xq % CX.cap; if (CX.idx[kx] !== xq) continue;
          var cq2 = Math.floor((xq * 1000 + 500 - tmin) / msPerCol); if (cq2 < 0) continue; if (cq2 >= pw) cq2 = pw - 1;
          cxs.has[cq2] = 1; cxs.a[cq2] = CX.a[kx]; cxs.b[cq2] = CX.b[kx];
          if (anyData && isFinite(lo)) { var xa = CX.a[kx], xb = CX.b[kx];
            if (xa === xa) { if (xa < lo) lo = xa; if (xa > hi) hi = xa; } if (xb === xb) { if (xb < lo) lo = xb; if (xb > hi) hi = xb; } }
        }
      }
      var sk = Object.keys(stale).sort().join(',') + '|' + srcs.length; if (sk !== self.staleKey) { self.staleKey = sk; self.legend(stale); }
      var hist = root.querySelector('.hist');
      g.font = '13px monospace'; g.fillStyle = fg; g.strokeStyle = fg;
      if (!anyData) { self.yLo = self.yHi = null; hist.textContent = ''; g.globalAlpha = .6; g.fillText('sin trades en la ventana', L + 8, T + 36); g.globalAlpha = 1; return; }
      if (!isFinite(lo)) { lo = alo; hi = ahi; }
      // historial menor que la ventana: se muestra lo disponible, sin rellenar hacia atrás
      hist.textContent = (earliest > tmin + Math.max(2 * msPerCol, 2000)) ? 'historial disponible: ' + fmtDur(tmax - earliest) + ' de ' + fmtDur(win) : '';
      // 2) autoescala Y (visible, sin stale, ±10 %, expande ya / contrae suave)
      var ctr = (hi + lo) / 2, minSpan = Math.max(Math.abs(ctr) * 2e-5, 1e-9);
      if (hi - lo < minSpan) { lo = ctr - minSpan / 2; hi = ctr + minSpan / 2; }
      var pad = (hi - lo) * CFG.padPct, tLo = lo - pad, tHi = hi + pad;
      var nowMs = performance.now(), dt = self.lastDraw ? Math.min(nowMs - self.lastDraw, 1000) : 1000; self.lastDraw = nowMs;
      var a = 1 - Math.exp(-dt / CFG.shrinkMs);
      if (self.yLo === null || tLo < self.yLo) self.yLo = tLo; else self.yLo += (tLo - self.yLo) * a;
      if (self.yHi === null || tHi > self.yHi) self.yHi = tHi; else self.yHi += (tHi - self.yHi) * a;
      var eps = (tHi - tLo) * 0.002, animating = Math.abs(self.yLo - tLo) > eps || Math.abs(self.yHi - tHi) > eps;
      if (!animating) { self.yLo = tLo; self.yHi = tHi; }
      lo = self.yLo; hi = self.yHi;
      var Y = function (v) { return T + (hi - v) / (hi - lo) * ph; };
      // 3) ejes
      g.globalAlpha = .18; g.lineWidth = 1; g.beginPath();
      for (var q = 0; q <= 5; q++) { var yy = T + ph * q / 5; g.moveTo(L, yy); g.lineTo(L + pw, yy); }
      for (var q2 = 0; q2 <= 6; q2++) { var xx = L + pw * q2 / 6; g.moveTo(xx, T); g.lineTo(xx, T + ph); }
      g.stroke(); g.globalAlpha = .8;
      g.textAlign = 'right'; for (var q3 = 0; q3 <= 5; q3++) { var v = hi - (hi - lo) * q3 / 5; g.fillText(fmtY(v, hi - lo), L - 4, T + ph * q3 / 5 + 4); }
      g.textAlign = 'center'; for (var q4 = 0; q4 <= 6; q4++) { var tt = tmin + win * q4 / 6; g.fillText(fmtT(tt, win <= 900000), Math.min(Math.max(L + pw * q4 / 6, L + 30), L + pw - 30), H - 5); }
      // 4) series: por columna, rango vertical min–máx + escalón desde el último anterior; huecos largos = corte
      g.lineWidth = 1.5; g.save(); g.beginPath(); g.rect(L, T, pw, ph); g.clip();
      g.globalAlpha = 1; g.lineWidth = 1.5;
      for (var sj = 0; sj < srcs.length; sj++) {
        var nm = srcs[sj], bb = cols[nm]; if (!bb) continue;
        g.globalAlpha = stale[nm] ? 0.35 : 1; g.strokeStyle = self.color(nm); g.fillStyle = self.color(nm);
        g.beginPath();
        var prevC = -1, prevY = 0, lx = 0, ly = 0;
        for (var cc = 0; cc < pw; cc++) {
          if (!bb.has[cc]) continue;
          var x = L + cc + 0.5, yl = Y(bb.lo[cc]), yh = Y(bb.hi[cc]), yz = Y(bb.last[cc]);
          if (prevC < 0 || (cc - prevC) * msPerCol > gapMs) g.moveTo(x, yz);
          else g.lineTo(x, prevY);
          if (yh !== yl) { g.lineTo(x, yh); g.lineTo(x, yl); }
          g.lineTo(x, yz);
          prevC = cc; prevY = yz; lx = x; ly = yz;
        }
        g.stroke();
        g.beginPath(); g.arc(lx, ly, 2.4, 0, 6.283); g.fill();        // último dato real (no se extiende hacia adelante)
      }
      // SMA 25/50 encima del precio: finas, con borde claro para que se lean sobre zonas densas
      [['a', []], ['b', [10, 7]]].forEach(function (ln) {
        var arr = sm[ln[0]];
        [[5.2, 'rgba(255,255,255,0.9)'], [2.6, '#111']].forEach(function (pass) {
          var prev = -1;                                                // une columnas; sólo corta en huecos reales > 5 s
          g.globalAlpha = 1; g.strokeStyle = pass[1]; g.lineWidth = pass[0]; g.setLineDash(ln[1]); g.beginPath();
          for (var cs = 0; cs < pw; cs++) {
            if (!sm.has[cs] || arr[cs] !== arr[cs]) continue;
            var xs = L + cs + 0.5, ys = Y(arr[cs]);
            if (prev < 0 || (cs - prev) * msPerCol > 5000) g.moveTo(xs, ys); else g.lineTo(xs, ys);
            prev = cs;
          }
          g.stroke();
        });
        g.setLineDash([]);
      });
      if (CX) {                                                       // CONCEPTITO: SMA39/76 con borde claro + marcadores
        [['a', CXC.f, []], ['b', CXC.s, [12, 5]]].forEach(function (ln) {
          var arr = cxs[ln[0]];
          [[5, 'rgba(255,255,255,0.9)'], [2.4, ln[1]]].forEach(function (pass) {
            var prev = -1; g.globalAlpha = 1; g.strokeStyle = pass[1]; g.lineWidth = pass[0]; g.setLineDash(ln[2]); g.beginPath();
            for (var cz = 0; cz < pw; cz++) {
              if (!cxs.has[cz] || arr[cz] !== arr[cz]) continue;
              var xz = L + cz + 0.5, yz2 = Y(arr[cz]);
              if (prev < 0 || (cz - prev) * msPerCol > 5000) g.moveTo(xz, yz2); else g.lineTo(xz, yz2);
              prev = cz;
            }
            g.stroke();
          });
          g.setLineDash([]);
        });
        cxMarks(g, CX, tmin, tmax, msPerCol, L, T, pw, ph, Y);
      }
      g.restore(); g.globalAlpha = 1;
      window.__prrrDrawMs = (window.__prrrDrawMs || 0) * 0.9 + (performance.now() - t0) * 0.1;   // medición de costo
      if (animating) self.sched();
    };
  }

  // ---------- Δ BUY−SELL 60 s ----------
  function DeltaChart(sym) {
    var root = $('mktd_' + sym), self = this; this.data = null; this.raf = 0;
    this.sched = function () { if (!self.raf) self.raf = requestAnimationFrame(function () { self.raf = 0; self.draw(); }); };
    this.feed = function (p) { self.data = p; if (!UI.paused) self.sched(); };
    this.draw = function () {
      var data = self.data; if (!data) return;
      root.querySelector('.dhd').innerHTML = '<b>' + sym + '</b> Δ BUY−SELL USD · últimos 60 s (buckets 1 s) · último <b>' + esc(data.last || '—') + '</b>';
      var r = fit(root.querySelector('.dcv canvas'), root.querySelector('.dcv')); if (!r) return;
      var g = r.g, W = r.W, H = r.H, fg = getComputedStyle(root).color || '#888';
      var pts = data.pts || [], mx = 0;
      for (var i = 0; i < pts.length; i++) mx = Math.max(mx, Math.abs(pts[i].v));
      var L = 64, T = 4, B = 4, pw = W - L - 4, ph = H - T - B, mid = T + ph / 2;
      g.font = '13px monospace'; g.fillStyle = fg; g.strokeStyle = fg; g.globalAlpha = .7; g.textAlign = 'right';
      g.fillText(mx ? '+' + fmtU(mx) : '0', L - 4, T + 12); g.fillText(mx ? '-' + fmtU(mx) : '0', L - 4, T + ph - 2);
      g.globalAlpha = .3; g.beginPath(); g.moveTo(L, mid); g.lineTo(L + pw, mid); g.stroke(); g.globalAlpha = 1;
      var bw = pw / 60;
      for (var j = 0; j < pts.length; j++) {
        var p = pts[j], x = L + (60 - pts.length + j) * bw;
        if (p.syn) { g.fillStyle = '#9e9e9e'; g.globalAlpha = .6; g.fillRect(x + bw * .3, mid - 1, Math.max(1, bw * .4), 2); g.globalAlpha = 1; continue; }
        var h = mx ? Math.abs(p.v) / mx * (ph / 2) : 0;
        g.fillStyle = p.v >= 0 ? '#27ae60' : '#e74c3c';
        g.fillRect(x + 1, p.v >= 0 ? mid - h : mid, Math.max(1, bw - 2), Math.max(1, h));
      }
    };
  }

  // ---------- cabecera + exchanges ----------
  function card(sym, p) {
    var root = $('mkt_' + sym); if (!root || !p) return;
    var cls = p.side === 'BUY' ? 'up' : p.side === 'SELL' ? 'dn' : '';
    var px = root.querySelector('.px'); px.textContent = p.price; px.className = 'px ' + cls;
    root.querySelector('.meta').innerHTML = esc(p.exchange) + ' · <b class="' + cls + '">' + esc(p.side || '?') + '</b> · edad ' + esc(p.age) + ' · <b>' + esc(p.evs) + '</b> ev/s · <b>' + esc(p.perMin) + '</b> trades/min';
    var h = '';
    (p.perEx || []).forEach(function (e) {
      var c = e.side === 'BUY' ? 'up' : e.side === 'SELL' ? 'dn' : '';
      h += '<div class="c' + (e.off || !e.pm ? ' st' : '') + '" style="border-left-color:' + (COL[e.ex] || '#888') + '" title="' + esc(e.ex) + ': ' + e.pm + ' trades en 60 s">'
        + '<span class="n">' + esc(e.ex) + '</span> <b>' + e.pm + '</b><span class="n">/m</span><br><b class="' + c + '">' + esc(e.price) + '</b> <span class="n">' + esc(e.age) + '</span></div>';
    });
    root.querySelector('.ex').innerHTML = h;
  }

  // ---------- controles compartidos ----------
  var P = {}, D = {}, lastAll = 0, pendAll = 0;
  // Redibujado limitado por resolución útil: nunca más seguido que el ploteo, y como máximo cada 1 s
  // cuando 1 píxel representa varios segundos (p.ej. 4 h: 1 px ≈ 21 s). Los datos se ingieren igual.
  function redrawMs() {
    var c = $('mkt_' + SYMS[0]), w = c ? Math.max(200, c.querySelector('.pcv').clientWidth - 90) : 600;
    return Math.max(G.plotMs, Math.min(1000, UI.windowMs / w / 4));
  }
  function drawAll(force) {
    var now = performance.now(), iv = redrawMs();
    if (!force && now - lastAll < iv - 5) { if (!pendAll) pendAll = setTimeout(function () { pendAll = 0; drawAll(true); }, iv - (now - lastAll)); return; }
    lastAll = now; SYMS.forEach(function (s) { if (P[s]) P[s].sched(); });
    var hint = $('prrr_mkt') && $('prrr_mkt').querySelector('.rd'); if (hint) hint.textContent = 'redibujo cada ' + Math.round(iv) + ' ms';
  }
  function bar() {
    var el = $('prrr_mkt'); if (!el) return;
    el.querySelectorAll('button[data-w]').forEach(function (b) { b.classList.toggle('on', +b.getAttribute('data-w') === UI.windowMs); });
    el.querySelectorAll('button[data-p]').forEach(function (b) { b.classList.toggle('on', +b.getAttribute('data-p') === G.plotMs); });
    el.querySelector('.pz').classList.toggle('on', UI.paused);
    var st = el.querySelector('.st');
    st.className = 'st ' + (UI.paused ? 'paused' : 'live');
    st.textContent = UI.paused ? 'PAUSADO · vista fija en ' + fmtT(UI.frozenAt, true) + ' (los datos siguen entrando)' : 'EN VIVO';
  }
  function wire() {
    var el = $('prrr_mkt'); if (!el || el.__wired) return; el.__wired = true;
    el.querySelectorAll('button[data-w]').forEach(function (b) { b.addEventListener('click', function () {
      UI.windowMs = +b.getAttribute('data-w'); try { localStorage.setItem('prrr_window_ms', UI.windowMs); } catch (e) {} bar(); drawAll(true); }); });
    el.querySelectorAll('button[data-p]').forEach(function (b) { b.addEventListener('click', function () {
      scope.send({ topic: 'render_ms', payload: +b.getAttribute('data-p') }); }); });
    el.querySelector('.pz').addEventListener('click', function () {
      if (!UI.paused) { UI.paused = true; UI.frozenAt = G.now; } else { UI.paused = false; }
      bar(); drawAll(true); SYMS.forEach(function (s) { if (D[s]) D[s].sched(); }); });
    el.querySelector('.lv').addEventListener('click', function () {
      UI.paused = false; bar(); drawAll(true); SYMS.forEach(function (s) { if (D[s]) D[s].sched(); }); });
  }
  function place() {
    var el = $('prrr_mkt'); if (!el) return;
    var tb = document.querySelector('md-toolbar'), top = 0;
    if (tb) { var r = tb.getBoundingClientRect(); if (r.bottom > 0 && r.bottom < window.innerHeight / 3) top = Math.round(r.bottom); }
    el.style.top = top + 'px';
    if (!el.style.background) {
      var bg = '', cands = [document.querySelector('md-content'), document.body, document.documentElement];
      for (var i = 0; i < cands.length && !bg; i++) { if (!cands[i]) continue; var c = getComputedStyle(cands[i]).backgroundColor; if (c && c !== 'transparent' && c !== 'rgba(0, 0, 0, 0)') bg = c; }
      el.style.background = bg || '#ffffff';
    }
  }
  function init() {
    SYMS.forEach(function (s) { if (!P[s] && $('mkt_' + s)) { P[s] = new PriceChart(s); D[s] = new DeltaChart(s); } });
    wire(); bar();
    if (!cxTimer) cxTimer = setInterval(cxPanel, 250);              // etapa 7: zona CONCEPTITO del panel ZEC
    if (!G.snapAsked) { G.snapAsked = true; scope.send({ topic: 'snapshot_req' }); }   // página nueva: pedir historial al servidor
  }
  scope.$watch('msg', function (m) {
    if (!m || !m.payload) return;
    init();
    var b = m.payload;
    if (b.snapshot) { loadSnapshot(b.snapshot); drawAll(true); return; }
    if (b.smaSnapshot) { SYMS.forEach(function (s) { var a = b.smaSnapshot[s] || []; for (var i = 0; i < a.length; i += 3) smaPut(s, a[i], a[i + 1] === null ? null : a[i + 1], a[i + 2] === null ? null : a[i + 2]); }); drawAll(true); return; }
    if (b.an) SYMS.forEach(function (s) { var r = b.an[s]; if (!r) return; smaPut(s, Math.floor(r.t / 1000), r.price_sma_25, r.price_sma_50); if (!UI.paused) anStrip(s, r); });
    var plotChanged = false;
    SYMS.forEach(function (s) {
      var c = b.charts && b.charts[s];
      if (c) {
        if (c.now > G.now) G.now = c.now;
        if (c.plotMs && c.plotMs !== G.plotMs) { G.plotMs = c.plotMs; plotChanged = true; }
        if (c.pts && c.pts.length) ingest(s, c.pts);        // se ingiere siempre, aun en pausa
      }
      if (b.cards && b.cards[s]) card(s, b.cards[s]);
      if (b.deltas && b.deltas[s] && D[s]) D[s].feed(b.deltas[s]);
    });
    if (plotChanged) bar();
    if (!UI.paused) drawAll();
  });
  var onRes = function () { place(); drawAll(true); SYMS.forEach(function (s) { if (D[s]) D[s].sched(); }); };
  window.addEventListener('resize', onRes);
  scope.$on('$destroy', function () { window.removeEventListener('resize', onRes); if (cxTimer) { clearInterval(cxTimer); cxTimer = 0; } });
  setTimeout(function () { init(); onRes(); }, 0);
  setTimeout(onRes, 500);
})(scope);
</script>`;
}

// Multiplexor: junta los mensajes de un mismo ciclo de render en 1 solo mensaje hacia el navegador.
const MUX_FUNC = String.raw`// Entradas etiquetadas por los nodos "→ MERCADO": msg.topic = 'chart' | 'card' | 'delta', msg.sym = activo.
// Los puntos de precio se ACUMULAN entre envíos (no se pierde ningún punto); tarjetas y deltas: último valor.
// Se envía 1 msg ~20 ms después del primer mensaje del ciclo → 1 redibujado por ciclo de render.
let B = context.get('B');
if (!B) { B = { charts: {}, cards: {}, deltas: {}, pending: false }; context.set('B', B); }
const sym = msg.sym, p = msg.payload;
if (!sym || !p) return null;
if (msg.topic === 'chart') {
    let c = B.charts[sym];
    if (!c || p.reset) c = B.charts[sym] = { now: p.now, pts: [], reset: !!p.reset };
    c.now = p.now; if (p.plotMs) c.plotMs = p.plotMs;
    if (p.pts && p.pts.length) c.pts = c.pts.concat(p.pts);
} else if (msg.topic === 'card') B.cards[sym] = p;
else if (msg.topic === 'an') B.an = p;
else if (msg.topic === 'delta') B.deltas[sym] = p;
if (!B.pending) {
    B.pending = true;
    setTimeout(function () {
        const out = { payload: { charts: B.charts, cards: B.cards, deltas: B.deltas, an: B.an } };
        B.charts = {}; B.cards = {}; B.deltas = {}; B.an = null; B.pending = false;
        node.send(out);
    }, 20);
}
return null;`;

function tagNode(id, name, topic, sym, x, y) {
  return { id, type: 'change', z: TAB, name,
    rules: [{ t: 'set', p: 'topic', pt: 'msg', to: topic, tot: 'str' }, { t: 'set', p: 'sym', pt: 'msg', to: sym, tot: 'str' }],
    action: '', property: '', from: '', to: '', reg: false, x, y, wires: [['prrr_ui_mkt_mux']] };
}
SYMS.forEach((s, i) => {
  const S_ = s.toUpperCase();
  inGroup(gUi, tagNode('prrr_ui_card_' + s, 'card ' + S_ + ' → MERCADO', 'card', S_, UX, 200 + i * 40));
  inGroup(gUi, Object.assign(tagNode('prrr_ui_chart_' + s, 'precio ' + S_ + ' (core, sin uso: lo reemplaza PLOT feeder)', 'chart_core', S_, UX + 200, 200 + i * 40), { wires: [[]] }));
});
// MERCADO: 1 grupo (id reutilizado del viejo grupo BTC) con 1 widget de grilla propia
add({ id: 'prrr_ui_g_btc', type: 'ui_group', name: 'MERCADO', tab: 'prrr_ui_tab_mkt', order: 1, disp: false, width: '6', collapse: false, className: '' });
inGroup(gUi, {
  id: 'prrr_ui_mkt_mux', type: 'function', z: TAB, name: 'MERCADO mux (1 msg por ciclo de render)',
  func: MUX_FUNC, outputs: 1, timeout: 0, noerr: 0, initialize: '', finalize: '', libs: [], x: UX + 430, y: 260, wires: [['prrr_ui_mkt']],
});
inGroup(gUi, Object.assign({}, tplBase, {
  id: 'prrr_ui_mkt', name: 'MERCADO (capa fija 100vw: 4 columnas precio + Δ)', group: 'prrr_ui_g_btc', order: 1, width: 6, height: 1,
  format: mktTpl(), storeOutMessages: false, className: '', x: UX + 640, y: 260, wires: [[CORE, 'prrr_plot_feeder', 'prrr_an_2550']],
}));
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
  id: 'prrr_stream_out', type: 'link out', z: TAB, name: 'MD STREAM →', mode: 'link', links: ['prrr_example_in', 'prrr_s2_stream_in', 'prrr_plot_in'], x: UX - 20, y: 120, wires: [],
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
  id: 'prrr_render_init', type: 'inject', z: TAB, name: 'ploteo inicial 250 ms',
  props: [{ p: 'payload' }], repeat: '', crontab: '', once: true, onceDelay: '1', topic: '', payload: '250', payloadType: 'num',
  x: 170, y: GY, wires: [['prrr_ui_render']],
});
inGroup(gCtl, {
  id: 'prrr_ui_render', type: 'ui_dropdown', z: TAB, name: 'Frecuencia de render', label: 'Ploteo', tooltip: 'Intervalo de ploteo: cada cuánto se envían/dibujan puntos (min/máx/último por intervalo). No afecta la adquisición ni los cálculos', place: '',
  group: G.ctrl.id, order: 50, width: 6, height: 1, passthru: true, multiple: false,
  options: [100, 250, 500, 1000].map((v) => ({ label: v + ' ms', value: v, type: 'num' })),
  payload: '', topic: 'render_ms', topicType: 'str', className: '', x: 420, y: GY, wires: [[CORE, 'prrr_plot_feeder']],
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

// ===========================================================================
// ETAPA 2 — NORMALIZADOR TEMPORAL 1 s + MEMORIA (RAM)
// RAW (MD STREAM) → buckets exactos de 1 s por activo → memoria circular de 30 min.
// Módulo aditivo: no modifica adquisición, core ni la visualización existente.
// ===========================================================================
const NORM = 'prrr_s2_norm', MEMN = 'prrr_s2_mem', MEMUI = 'prrr_s2_ui';
const S2_IN = 'prrr_s2_stream_in', S2_OUT = 'prrr_s2_buckets_out';
const S2_SYMS = ['BTC', 'ZEC', 'GMX', 'XMR'];

const NORM_INIT = String.raw`// ===== NORMALIZADOR TEMPORAL 1 s =====
// Reloj común = local_receive_timestamp (reloj de la notebook): todos los activos y exchanges quedan
// alineados sobre los mismos segundos aunque los relojes de los exchanges difieran entre sí.
// Un bucket [t, t+1000) se cierra cuando el reloj local pasa t + 1000 + GRACE_MS (margen para la cola interna).
const CFG = {
    symbols: ['BTC', 'ZEC', 'GMX', 'XMR'],
    graceMs: 500,           // trades que llegan después del cierre = "tardíos" (se cuentan, no se inventan ni se mueven)
    maxCatchUpSec: 1800     // tras una suspensión larga no se generan más buckets que los que caben en memoria
};
const N = {
    cfg: CFG,
    nextSec: Math.floor(Date.now() / 1000) + 1,   // el primer segundo parcial se descarta (no sería un bucket exacto)
    open: {}, lastClose: {},
    stats: { late: {}, preStart: 0, closed: 0, badTrades: 0, startedAt: Date.now() }
};
for (const sy of CFG.symbols) { N.open[sy] = new Map(); N.lastClose[sy] = null; N.stats.late[sy] = 0; }
context.set('N', N, 'memory');
global.set('md_norm_1s_stats', N.stats, 'memory');`;

const NORM_FUNC = String.raw`// Entradas: trades del RAW (topic 'trade') y 'tick' (cada 100 ms). No modifica el mensaje RAW.
// Salida: 1 msg por segundo cerrado: { topic:'bucket_1s', t, payload:[bucket BTC, ZEC, GMX, XMR] }
const N = context.get('N', 'memory');
const now = Date.now();

function newAcc() {
    return { o: 0, h: -Infinity, l: Infinity, c: 0, tO: Infinity, tC: -Infinity, n: 0, events: 0,
        vol: 0, buy: 0, sell: 0, unk: 0, ex: new Set(), src: new Set(), spot: 0, perp: 0 };
}

if (msg.topic === 'trade') {
    const t = msg.payload;
    const acc = N.open[t.symbol];
    if (!acc) return null;                                   // activo fuera de la memoria
    const ts = t.local_receive_timestamp, sec = Math.floor(ts / 1000);
    const usd = t.price * t.quantity;
    if (!isFinite(ts) || !(t.price > 0) || !isFinite(usd) || usd < 0) { N.stats.badTrades++; return null; }
    if (sec < N.nextSec) {                                   // su segundo ya se cerró
        if (ts < N.stats.startedAt + 1000) N.stats.preStart++; else N.stats.late[t.symbol]++;
    } else {
        let a = acc.get(sec);
        if (!a) { a = newAcc(); acc.set(sec, a); }
        const fills = t.trade_count || 1;                    // Binance perp @aggTrade: fills reales dentro del evento
        if (ts < a.tO) { a.tO = ts; a.o = t.price; }
        if (ts >= a.tC) { a.tC = ts; a.c = t.price; }
        if (t.price > a.h) a.h = t.price;
        if (t.price < a.l) a.l = t.price;
        a.n += fills; a.events++; a.vol += usd;
        if (t.side === 'BUY') a.buy += usd;
        else if (t.side === 'SELL') a.sell += usd;
        else a.unk += usd;                                   // lado desconocido: cuenta en total, nunca en BUY/SELL
        a.ex.add(t.exchange); a.src.add(t.source || t.exchange);
        if (t.market_type === 'perp') a.perp += fills; else a.spot += fills;
    }
}

// Cierre de buckets (en ticks y también al llegar trades)
const out = [];
const r2 = (v) => Math.round(v * 100) / 100;
if (now - N.nextSec * 1000 > N.cfg.maxCatchUpSec * 1000) N.nextSec = Math.floor(now / 1000) - N.cfg.maxCatchUpSec;
while (now >= (N.nextSec + 1) * 1000 + N.cfg.graceMs) {
    const sec = N.nextSec, arr = [];
    for (const sy of N.cfg.symbols) {
        const m = N.open[sy], a = m.get(sec);
        m.delete(sec);
        for (const k of m.keys()) if (k < sec) m.delete(k); // higiene: nunca quedan segundos viejos abiertos
        let b;
        if (a && a.n > 0) {
            b = { symbol: sy, t: sec * 1000, open: a.o, high: a.h, low: a.l, close: a.c,
                trades: a.n, events: a.events, vol_usd: r2(a.vol), buy_usd: r2(a.buy), sell_usd: r2(a.sell),
                delta_usd: r2(a.buy - a.sell), unknown_side_usd: r2(a.unk),
                exchanges: a.ex.size, sources: a.src.size, spot_trades: a.spot, perp_trades: a.perp,
                synthetic: false, status: 'trade' };
            N.lastClose[sy] = a.c;
        } else {
            const p = N.lastClose[sy];                       // precio ARRASTRADO, no actividad
            b = { symbol: sy, t: sec * 1000, open: p, high: p, low: p, close: p,
                trades: 0, events: 0, vol_usd: 0, buy_usd: 0, sell_usd: 0, delta_usd: 0, unknown_side_usd: 0,
                exchanges: 0, sources: 0, spot_trades: 0, perp_trades: 0,
                synthetic: true, status: p == null ? 'no_price' : 'no_trade' };
        }
        arr.push(b);
    }
    N.nextSec++; N.stats.closed++;
    out.push({ topic: 'bucket_1s', t: sec * 1000, payload: arr });
}
return out.length ? [out] : null;`;

const MEM_INIT = String.raw`// ===== MEMORIA 1 s (RAM) =====
// Buffer circular por activo de tamaño fijo: nunca crece. Estructura pensada para persistir después
// (cada bucket es una fila plana: symbol, t, open, high, low, close, trades, vol_usd, buy_usd, sell_usd, ...).
// Lectura desde otros nodos: const M = global.get('md_mem_1s', 'memory');  M.series.BTC → { size, buf, idx, count }
//   el más nuevo:  buf[(idx - 1 + size) % size]   ·   el más viejo:  buf[(idx - count + size) % size]
const SIZE = 1800;   // 30 min de buckets de 1 s
const MEM = { resolutionMs: 1000, size: SIZE, series: {}, lastT: null, stored: 0 };
for (const sy of ['BTC', 'ZEC', 'GMX', 'XMR']) MEM.series[sy] = { size: SIZE, buf: new Array(SIZE), idx: 0, count: 0 };
context.set('MEM', MEM, 'memory');
global.set('md_mem_1s', MEM, 'memory');`;

const MEM_FUNC = String.raw`// Guarda cada bucket en su buffer circular.
// Salida 1: el mismo msg (hacia BUCKETS 1s → : persistencia futura / indicadores).
// Salida 2: aviso para la vista del dashboard.
const MEM = context.get('MEM', 'memory');
if (msg.topic !== 'bucket_1s' || !Array.isArray(msg.payload)) return null;
for (const b of msg.payload) {
    let r = MEM.series[b.symbol];
    if (!r) r = MEM.series[b.symbol] = { size: MEM.size, buf: new Array(MEM.size), idx: 0, count: 0 };
    r.buf[r.idx] = b; r.idx = (r.idx + 1) % r.size; if (r.count < r.size) r.count++;
    MEM.stored++;
}
MEM.lastT = msg.t;
return [msg, { topic: 'mem_updated', t: msg.t }];`;

const MEMUI_FUNC = String.raw`// Vista de validación (1 vez por segundo): tabla + delta BUY−SELL de los últimos 60 buckets por activo.
const MEM = global.get('md_mem_1s', 'memory');
const NS = global.get('md_norm_1s_stats', 'memory') || { late: {} };
if (!MEM) return null;
const now = Date.now();
const SYMS = ['BTC', 'ZEC', 'GMX', 'XMR'];
const usd = (v) => v == null ? '—' : (Math.abs(v) >= 1e6 ? (v / 1e6).toFixed(2) + 'M' : Math.abs(v) >= 1e3 ? (v / 1e3).toFixed(1) + 'k' : v.toFixed(0));
const px = (p) => p == null ? '—' : (p >= 1000 ? p.toFixed(2) : p >= 1 ? p.toFixed(3) : p.toPrecision(5));
const hms = (t) => { const d = new Date(t); return ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2) + ':' + ('0' + d.getSeconds()).slice(-2); };
const age = (ms) => ms < 60000 ? (ms / 1000).toFixed(0) + ' s' : Math.floor(ms / 60000) + ' min ' + Math.floor(ms % 60000 / 1000) + ' s';
const rows = [], charts = [];
for (const sy of SYMS) {
    const r = MEM.series[sy];
    if (!r || !r.count) { rows.push({ sym: sy, count: 0 }); charts.push({ payload: { sym: sy, pts: [] } }); continue; }
    const last = r.buf[(r.idx - 1 + r.size) % r.size], oldest = r.buf[(r.idx - r.count + r.size) % r.size];
    let syn = 0, tr60 = 0;
    const pts = [];
    for (let i = 0; i < r.count; i++) {
        const b = r.buf[(r.idx - 1 - i + r.size * 2) % r.size];
        if (b.synthetic) syn++;
        if (i < 60) { pts.push({ t: b.t, v: b.delta_usd, syn: b.synthetic }); tr60 += b.trades; }
    }
    pts.reverse();
    rows.push({ sym: sy, count: r.count, cover: age(r.count * 1000), oldestAge: age(now - oldest.t), lastT: hms(last.t), lastAge: ((now - last.t) / 1000).toFixed(1),
        status: last.status, price: px(last.close), trades: last.trades, vol: usd(last.vol_usd), buy: usd(last.buy_usd), sell: usd(last.sell_usd),
        delta: usd(last.delta_usd), dsign: last.delta_usd > 0 ? 1 : last.delta_usd < 0 ? -1 : 0, unk: usd(last.unknown_side_usd),
        ex: last.exchanges, spot: last.spot_trades, perp: last.perp_trades,
        synPct: (syn * 100 / r.count).toFixed(0), tr60: tr60, late: NS.late[sy] || 0 });
    charts.push({ payload: { sym: sy, pts: pts, last: usd(last.delta_usd) } });
}
return [{ payload: { rows: rows, size: MEM.size, stored: MEM.stored } }].concat(charts);`;

const MEM_TABLE_TPL = String.raw`<style>
.prrr-mem{width:100%;border-collapse:collapse;font-family:monospace;font-size:11.5px}
.prrr-mem th,.prrr-mem td{padding:2px 5px;text-align:right;border-bottom:1px solid rgba(128,128,128,.25);white-space:nowrap}
.prrr-mem th{opacity:.8}
.prrr-mem .l{text-align:left}
.prrr-mem .up{color:#27ae60}.prrr-mem .dn{color:#e74c3c}
.prrr-mem .syn{color:#e6a700;font-weight:bold}.prrr-mem .real{color:#27ae60;font-weight:bold}
.prrr-mem-note{font-family:monospace;font-size:11px;opacity:.7;margin-top:2px}
</style>
<div ng-if="msg.payload" style="overflow-x:auto">
<table class="prrr-mem">
<tr><th class="l">activo</th><th>buckets</th><th>cobertura</th><th>más viejo</th><th>último bucket</th><th class="l">estado</th><th>close</th>
<th>trades</th><th>vol USD</th><th>BUY USD</th><th>SELL USD</th><th>delta</th><th>sin lado USD</th><th>exch</th><th>spot/perp</th><th>trades 60 s</th><th>no_trade %</th><th>tardíos</th></tr>
<tr ng-repeat="r in msg.payload.rows track by r.sym">
  <td class="l"><b>{{r.sym}}</b></td><td>{{r.count}}/{{msg.payload.size}}</td><td>{{r.cover}}</td><td>{{r.oldestAge}}</td><td>{{r.lastT}} <small>({{r.lastAge}} s)</small></td>
  <td class="l" ng-class="r.status==='trade' ? 'real' : 'syn'">{{r.status}}</td><td>{{r.price}}</td>
  <td><b>{{r.trades}}</b></td><td>{{r.vol}}</td><td class="up">{{r.buy}}</td><td class="dn">{{r.sell}}</td>
  <td ng-class="{'up': r.dsign>0, 'dn': r.dsign<0}"><b>{{r.delta}}</b></td><td>{{r.unk}}</td><td>{{r.ex}}</td><td>{{r.spot}}/{{r.perp}}</td>
  <td>{{r.tr60}}</td><td>{{r.synPct}}</td><td>{{r.late}}</td>
</tr>
</table>
<div class="prrr-mem-note">buckets exactos de 1 s (reloj local de recepción) · último bucket = segundo recién cerrado · no_trade = precio arrastrado sin actividad (trades y volúmenes en 0) · sin lado = volumen cuyo agresor no se pudo determinar (no suma a BUY/SELL) · USD incluye pares USDT/USDC</div>
</div>
<div ng-if="!msg.payload" style="font-family:monospace">esperando el primer bucket…</div>`;

function deltaTpl(domId) {
  return `<style>
#${domId}{width:100%;height:100%;display:flex;flex-direction:column;font:11px monospace}
#${domId} .hd{white-space:nowrap;overflow:hidden}
#${domId} .cv{flex:1;position:relative;min-height:0}
#${domId} canvas{position:absolute;left:0;top:0;width:100%;height:100%}
</style>
<div id="${domId}"><div class="hd"></div><div class="cv"><canvas></canvas></div></div>
<script>
(function (scope) {
  var ID = ${JSON.stringify(domId)}, data = null, raf = 0;
  function sched() { if (!raf) raf = requestAnimationFrame(draw); }
  function fmt(v) { var a = Math.abs(v); return a >= 1e6 ? (v / 1e6).toFixed(2) + 'M' : a >= 1e3 ? (v / 1e3).toFixed(1) + 'k' : v.toFixed(0); }
  function draw() {
    raf = 0;
    var r = document.getElementById(ID); if (!r || !data) return;
    r.querySelector('.hd').innerHTML = '<b>' + data.sym + '</b> Δ BUY−SELL USD · 60 s · último ' + (data.last || '—');
    var cv = r.querySelector('canvas'), box = r.querySelector('.cv');
    var W = box.clientWidth, H = box.clientHeight; if (W < 10 || H < 10) return;
    var dpr = window.devicePixelRatio || 1;
    if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) { cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); }
    var g = cv.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, W, H);
    var fg = getComputedStyle(r).color || '#888';
    var pts = data.pts || [], mx = 0;
    for (var i = 0; i < pts.length; i++) mx = Math.max(mx, Math.abs(pts[i].v));
    var L = 44, T = 4, B = 4, pw = W - L - 2, ph = H - T - B, mid = T + ph / 2;
    g.font = '10px monospace'; g.fillStyle = fg; g.strokeStyle = fg; g.globalAlpha = .6; g.textAlign = 'right';
    g.fillText(mx ? '+' + fmt(mx) : '0', L - 4, T + 9); g.fillText(mx ? '-' + fmt(mx) : '0', L - 4, T + ph - 2);
    g.globalAlpha = .3; g.beginPath(); g.moveTo(L, mid); g.lineTo(L + pw, mid); g.stroke(); g.globalAlpha = 1;
    var bw = pw / 60;
    for (var j = 0; j < pts.length; j++) {
      var p = pts[j], x = L + (60 - pts.length + j) * bw;
      if (p.syn) { g.fillStyle = '#9e9e9e'; g.globalAlpha = .6; g.fillRect(x + bw * .3, mid - 1, Math.max(1, bw * .4), 2); g.globalAlpha = 1; continue; }
      var h = mx ? Math.abs(p.v) / mx * (ph / 2) : 0;
      g.fillStyle = p.v >= 0 ? '#27ae60' : '#e74c3c';
      g.fillRect(x + 1, p.v >= 0 ? mid - h : mid, Math.max(1, bw - 2), Math.max(1, h));
    }
  }
  scope.$watch('msg', function (m) { if (m && m.payload) { data = m.payload; sched(); } });
  var onRes = function () { sched(); };
  window.addEventListener('resize', onRes);
  scope.$on('$destroy', function () { window.removeEventListener('resize', onRes); if (raf) cancelAnimationFrame(raf); });
})(scope);
</script>`;
}

// UI: grupo nuevo al final del tab (no toca los grupos existentes)
const G_S2 = { id: 'prrr_ui_g_s2', name: 'MEMORIA / NORMALIZADOR', order: 9, width: 24 };
add({ id: G_S2.id, type: 'ui_group', name: G_S2.name, tab: UI_TAB, order: G_S2.order, disp: true, width: String(G_S2.width), collapse: false, className: '' });

const gS2 = egroup('prrr_grp_s2', 'ETAPA 2 — NORMALIZADOR TEMPORAL 1 s → MEMORIA 30 min (RAM)', '#e377c2');
const S2Y = GY + 320;
inGroup(gS2, {
  id: S2_IN, type: 'link in', z: TAB, name: 'MD STREAM (RAW)', links: ['prrr_stream_out'], x: 190, y: S2Y, wires: [[NORM]],
});
inGroup(gS2, {
  id: 'prrr_s2_tick', type: 'inject', z: TAB, name: 'tick 100 ms', props: [{ p: 'topic', vt: 'str' }],
  repeat: '0.1', crontab: '', once: true, onceDelay: '0.5', topic: 'tick', x: 180, y: S2Y + 60, wires: [[NORM]],
});
inGroup(gS2, {
  id: NORM, type: 'function', z: TAB, name: 'NORMALIZADOR 1 s (OHLC · vol USD · BUY/SELL · delta)',
  func: NORM_FUNC, outputs: 1, timeout: 0, noerr: 0, initialize: NORM_INIT, finalize: '', libs: [],
  outputLabels: ['bucket_1s (4 activos por segundo)'], x: 480, y: S2Y + 20, wires: [[MEMN]],
});
inGroup(gS2, {
  id: MEMN, type: 'function', z: TAB, name: 'MEMORIA 1 s (RAM · 1800 × 4)',
  func: MEM_FUNC, outputs: 2, timeout: 0, noerr: 0, initialize: MEM_INIT, finalize: '', libs: [],
  outputLabels: ['bucket_1s → persistencia / indicadores', 'aviso vista'], x: 800, y: S2Y + 20, wires: [[S2_OUT], [MEMUI]],
});
inGroup(gS2, {
  id: S2_OUT, type: 'link out', z: TAB, name: 'BUCKETS 1s →', mode: 'link', links: ['prrr_s2_example_in', 'prrr_db_in', 'prrr_an_in'], x: 1010, y: S2Y, wires: [],
});
inGroup(gS2, {
  id: 'prrr_s2_example_in', type: 'link in', z: TAB, name: 'ejemplo consumidor buckets', links: [S2_OUT], x: 1170, y: S2Y, wires: [['prrr_s2_example_dbg']],
});
inGroup(gS2, {
  id: 'prrr_s2_example_dbg', type: 'debug', z: TAB, name: 'buckets 1 s (activar para ver)', active: false, tosidebar: true, console: false, tostatus: false,
  complete: 'payload', targetType: 'msg', statusVal: '', statusType: 'auto', x: 1420, y: S2Y, wires: [],
});
inGroup(gS2, {
  id: MEMUI, type: 'function', z: TAB, name: 'vista MEMORIA (tabla + delta 60 s)',
  func: MEMUI_FUNC, outputs: 5, timeout: 0, noerr: 0, initialize: '', finalize: '', libs: [],
  outputLabels: ['tabla', 'delta BTC', 'delta ZEC', 'delta GMX', 'delta XMR'],
  x: 1060, y: S2Y + 100, wires: [['prrr_s2_ui_table'], ['prrr_s2_ui_d_btc'], ['prrr_s2_ui_d_zec'], ['prrr_s2_ui_d_gmx'], ['prrr_s2_ui_d_xmr']],
});
inGroup(gS2, Object.assign({}, tplBase, {
  id: 'prrr_s2_ui_table', name: 'MEMORIA tabla', group: G_S2.id, order: 1, width: 24, height: 3, format: MEM_TABLE_TPL, x: 1370, y: S2Y + 60,
}));
S2_SYMS.forEach((sy, i) => {
  inGroup(gS2, tagNode('prrr_s2_ui_d_' + sy.toLowerCase(), 'delta ' + sy + ' → MERCADO', 'delta', sy, 1370, S2Y + 100 + i * 40));
});
inGroup(gS2, {
  id: 'prrr_s2_comment', type: 'comment', z: TAB, name: 'Formato del bucket 1 s (una fila por activo y segundo)',
  info: '```\n{\n  symbol, t: inicio del segundo (ms epoch, reloj local de recepción),\n  open, high, low, close,        // si no hubo trades: último precio conocido (arrastrado)\n  trades,                        // fills reales (Binance perp: fills dentro de @aggTrade)\n  events,                        // mensajes de trade recibidos\n  vol_usd, buy_usd, sell_usd,    // USD = price × quantity (USDT/USDC tratados como USD)\n  delta_usd = buy_usd − sell_usd,\n  unknown_side_usd,              // agresor indeterminado: suma a vol_usd, nunca a BUY/SELL\n  exchanges, sources,            // exchanges distintos / fuentes distintas (spot y perp por separado)\n  spot_trades, perp_trades,\n  synthetic: bool, status: "trade" | "no_trade" | "no_price"\n}\n```\nMemoria: `global.get("md_mem_1s","memory")` → series[SYM] = { size:1800, buf, idx, count } (buffer circular).\nPersistencia futura: enganchar un link in a `BUCKETS 1s →` (1 msg/s con los 4 buckets).',
  x: 500, y: S2Y + 100, wires: [],
});

// ===========================================================================
// PLOT FEEDER — capa de REPRESENTACIÓN (no toca RAW, core, buckets ni memoria)
// Lee cada trade del MD STREAM (copia) y arma, por activo y fuente:
//  · agregado del intervalo de ploteo: min / max / último con su hora real (preserva picos)
//  · historial para gráficos en 2 niveles de resolución (acotados, typed arrays):
//      A = slots de 250 ms × 3600 (15 min)   ·   B = slots de 5 s × 2880 (4 h)
// Intervalo de ploteo (100/250/500/1000 ms) = cada cuánto se envían puntos al navegador.
// ===========================================================================
const PLOT = 'prrr_plot_feeder';
const PLOT_INIT = String.raw`// Estado del alimentador de ploteo (sólo presentación)
const F = {
    plotMs: 250, lastFlush: 0,
    syms: ['BTC', 'ZEC', 'GMX', 'XMR'],
    tiers: [{ n: 'A', slotMs: 250, cap: 3600 }, { n: 'B', slotMs: 5000, cap: 2880 }],
    series: {}, iv: {}, ingested: 0
};
for (const s of F.syms) { F.series[s] = {}; F.iv[s] = {}; }
context.set('F', F, 'memory');`;

const PLOT_FUNC = String.raw`// Entradas: trades (copia del MD STREAM), 'tick' 50 ms, 'render_ms' (intervalo de ploteo), 'snapshot_req' (desde el navegador)
// Salida 1: puntos del intervalo → MERCADO mux   ·   Salida 2: snapshot de historial → sólo al navegador que lo pidió
const F = context.get('F', 'memory');
const now = Date.now();

if (msg.topic === 'trade') {
    const t = msg.payload, sym = t.symbol;
    const ser = F.series[sym];
    if (!ser || !(t.price > 0)) return null;
    const src = t.source || t.exchange, ts = t.local_receive_timestamp, y = t.price;
    let S = ser[src];
    if (!S) {
        S = ser[src] = { lastT: 0 };
        for (const d of F.tiers) S[d.n] = { slotMs: d.slotMs, cap: d.cap, idx: new Float64Array(d.cap).fill(-1), lo: new Float64Array(d.cap), hi: new Float64Array(d.cap), last: new Float64Array(d.cap) };
    }
    for (const d of F.tiers) {                           // historial: min/max/último por slot
        const T = S[d.n], s = Math.floor(ts / T.slotMs), k = s % T.cap;
        if (T.idx[k] !== s) { T.idx[k] = s; T.lo[k] = y; T.hi[k] = y; T.last[k] = y; }
        else { if (y < T.lo[k]) T.lo[k] = y; if (y > T.hi[k]) T.hi[k] = y; if (ts >= S.lastT) T.last[k] = y; }
    }
    if (ts >= S.lastT) S.lastT = ts;
    let a = F.iv[sym][src];                               // intervalo de ploteo actual: extremos con su hora real
    if (!a) a = F.iv[sym][src] = { lo: y, loT: ts, hi: y, hiT: ts, last: y, lastT: ts };
    else {
        if (y < a.lo) { a.lo = y; a.loT = ts; }
        if (y > a.hi) { a.hi = y; a.hiT = ts; }
        if (ts >= a.lastT) { a.last = y; a.lastT = ts; }
    }
    F.ingested++;
    return null;
}

if (msg.topic === 'render_ms') {
    const v = Number(msg.payload);
    if (v >= 50 && v <= 5000) F.plotMs = v;
    return null;
}

if (msg.topic === 'snapshot_req') {                      // navegador recién abierto: le mando el historial disponible
    const snap = { now: now, plotMs: F.plotMs, tiers: {}, syms: {} };
    for (const d of F.tiers) snap.tiers[d.n] = { slotMs: d.slotMs, cap: d.cap };
    for (const sym of F.syms) {
        snap.syms[sym] = {};
        for (const src in F.series[sym]) {
            const S = F.series[sym][src], o = { lastT: S.lastT };
            for (const d of F.tiers) {
                const T = S[d.n], minS = Math.floor(now / T.slotMs) - T.cap + 1, arr = [];
                for (let k = 0; k < T.cap; k++) if (T.idx[k] >= minS) arr.push(T.idx[k], T.lo[k], T.hi[k], T.last[k]);
                o[d.n] = arr;
            }
            snap.syms[sym][src] = o;
        }
    }
    return [null, { socketid: msg.socketid, payload: { snapshot: snap } }];
}

if (msg.topic !== 'tick') return null;
if (now - F.lastFlush < F.plotMs - 25) return null;
F.lastFlush = now;
const out = [];
for (const sym of F.syms) {
    const pts = [], iv = F.iv[sym];
    for (const src in iv) {
        const a = iv[src], p = [[a.loT, a.lo], [a.hiT, a.hi], [a.lastT, a.last]];
        p.sort(function (u, v) { return u[0] - v[0]; });
        let prev = null;
        for (const q of p) { const key = q[0] + ':' + q[1]; if (key !== prev) pts.push({ s: src, x: q[0], y: q[1] }); prev = key; }
    }
    F.iv[sym] = {};
    out.push({ topic: 'chart', sym: sym, payload: { now: now, plotMs: F.plotMs, pts: pts } });   // siempre (el eje de tiempo avanza)
}
return [out, null];`;

const gPlot = egroup('prrr_grp_plot', 'PLOT FEEDER — representación (min/máx/último por intervalo + historial de gráficos)', '#17becf');
const PY = S2Y + 240;
inGroup(gPlot, { id: 'prrr_plot_in', type: 'link in', z: TAB, name: 'MD STREAM (RAW)', links: ['prrr_stream_out'], x: 190, y: PY, wires: [[PLOT]] });
inGroup(gPlot, { id: 'prrr_plot_tick', type: 'inject', z: TAB, name: 'tick 50 ms', props: [{ p: 'topic', vt: 'str' }],
  repeat: '0.05', crontab: '', once: true, onceDelay: '0.5', topic: 'tick', x: 180, y: PY + 60, wires: [[PLOT]] });
inGroup(gPlot, { id: PLOT, type: 'function', z: TAB, name: 'PLOT feeder (intervalo de ploteo · historial 15 min/250 ms + 4 h/5 s)',
  func: PLOT_FUNC, outputs: 2, timeout: 0, noerr: 0, initialize: PLOT_INIT, finalize: '', libs: [],
  outputLabels: ['puntos del intervalo → MERCADO', 'snapshot de historial → navegador que lo pidió'],
  x: 540, y: PY + 20, wires: [['prrr_ui_mkt_mux'], ['prrr_ui_mkt']] });

// ===========================================================================
// ETAPA 3 — PERSISTENCIA MariaDB (rama adicional, desacoplada del PRRR)
// BUCKETS 1s → (existente) → DB writer (cola acotada, lotes) → nodo mysql (node-red-node-mysql)
// Sin contraseñas en código: host/base en el config node, usuario/contraseña en sus credentials.
// ===========================================================================
const DBW = 'prrr_db_writer', DBM = 'prrr_db_mysql', DBCFG = 'prrr_db_cfg';
const DB_INIT = String.raw`// ===== DB WRITER: estado =====
const W = {
    cfg: {
        table: 'market_1s',
        batchMax: 400,          // filas por INSERT múltiple
        flushMs: 5000,          // como mucho cada 5 s (20 filas: 4 activos × 5 s)
        maxQueue: 14400,        // 1 h de buckets (4/s). Si MariaDB está caída más tiempo se descartan los más viejos (contados)
        timeoutMs: 30000,       // un lote sin respuesta en 30 s = error y se reintenta
        backoffMin: 5000, backoffMax: 60000
    },
    q: [], inflight: null, seq: 0, schema: null,
    lastFlush: Date.now(), backoff: 0, nextTry: 0,
    saved: 0, errors: 0, dropped: 0, skippedNoPrice: 0,
    lastSavedT: null, lastOkAt: 0, lastErr: '', link: 'desconocido'
};
context.set('W', W, 'memory');
node.status({ fill: 'grey', shape: 'ring', text: 'esperando esquema' });`;

const DB_FUNC = String.raw`// Entradas: 'bucket_1s' (BUCKETS 1s), 'tick' (1 s), resultado del nodo mysql (msg.dbKind), 'db_error' (catch), 'db_status' (status).
// Salida 1 → nodo mysql (1 query a la vez)   ·   Salida 2 → indicador DIAGNÓSTICO
// Nunca bloquea: sólo encola en RAM (acotada) y manda a lo sumo 1 lote en vuelo.
const W = context.get('W', 'memory'), C = W.cfg, now = Date.now();
const COLS = ['ts', 'symbol', 'open_price', 'high_price', 'low_price', 'close_price', 'trades', 'volume_usd', 'buy_usd', 'sell_usd',
    'delta_usd', 'exchanges', 'spot_trades', 'perp_trades', 'no_trade'];
const out = [null, null];

function fail(reason) {
    const r = String(reason).slice(0, 200);
    if (r !== W.lastErr || now - (W.lastLogAt || 0) > 60000) { node.warn('MariaDB: ' + r + ' · cola ' + W.q.length + ' filas · reintento en ≤ ' + Math.round(Math.min(C.backoffMax, Math.max(C.backoffMin, W.backoff * 2)) / 1000) + ' s'); W.lastLogAt = now; }
    W.errors++; W.lastErr = r; W.link = 'error';
    if (W.inflight && W.inflight.kind === 'insert') {          // el lote vuelve al frente de la cola (sin superar el tope)
        W.q = W.inflight.rows.concat(W.q);
        if (W.q.length > C.maxQueue) { const ex = W.q.length - C.maxQueue; W.q.splice(0, ex); W.dropped += ex; }
    }
    W.inflight = null;
    W.backoff = Math.min(C.backoffMax, Math.max(C.backoffMin, W.backoff * 2));
    W.nextTry = now + W.backoff;
    node.status({ fill: 'red', shape: 'ring', text: 'error: ' + W.lastErr.slice(0, 40) + ' · reintento ' + Math.round(W.backoff / 1000) + ' s' });
}
function pad(n, w) { return String(n).padStart(w || 2, '0'); }
function tsValue(t) {                                       // según el tipo real de la columna ts
    const ty = W.schema.tsType;
    if (ty === 'datetime' || ty === 'timestamp') { const d = new Date(t); return d.getUTCFullYear() + '-' + pad(d.getUTCMonth() + 1) + '-' + pad(d.getUTCDate()) + ' ' + pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes()) + ':' + pad(d.getUTCSeconds()); }
    if (ty === 'bigint' || ty === 'decimal') return t;      // ms epoch
    return Math.floor(t / 1000);                            // int / double: segundos epoch
}
function row(b) {
    return [tsValue(b.t), b.symbol, b.open, b.high, b.low, b.close, b.trades, b.vol_usd, b.buy_usd, b.sell_usd,
        b.delta_usd, b.exchanges, b.spot_trades, b.perp_trades, b.synthetic ? 1 : 0];
}
function indicator() {
    const ok = W.link === 'conectada' && now - W.lastOkAt < 20000;
    out[1] = { payload: { db: ok ? 'conectada' : (W.link === 'error' ? 'error' : W.link === 'conectando' ? 'conectando' : 'desconectada'),
        ok: ok, lastSaved: W.lastSavedT, saved: W.saved, errors: W.errors, lastErr: W.lastErr, queue: W.q.length, dropped: W.dropped } };
}

// ---- buckets nuevos: sólo encolar ----
if (msg.topic === 'bucket_1s' && Array.isArray(msg.payload)) {
    for (const b of msg.payload) {
        if (b.status === 'no_price' || b.close == null) { W.skippedNoPrice++; continue; }   // nunca hubo precio: no hay nada que guardar
        W.q.push(b);
    }
    if (W.q.length > C.maxQueue) { const ex = W.q.length - C.maxQueue; W.q.splice(0, ex); W.dropped += ex; }
    return null;
}

// ---- error del nodo mysql (catch). Va ANTES que las respuestas: el msg del catch conserva dbKind ----
if (msg.topic === 'db_error' || msg.error) {
    if (W.inflight && msg.dbBatch === W.inflight.id) fail((msg.error && msg.error.message) || 'error DB');
    return null;
}

// ---- respuesta OK del nodo mysql ----
if (msg.dbKind) {
    if (!W.inflight || msg.dbBatch !== W.inflight.id) return null;   // respuesta vieja (tras timeout): se ignora
    if (msg.dbKind === 'schema') {
        const rows = Array.isArray(msg.payload) ? msg.payload.filter(function (r) { return r && r.COLUMN_NAME; }) : [];
        const have = {}; rows.forEach(function (r) { have[String(r.COLUMN_NAME).toLowerCase()] = String(r.DATA_TYPE).toLowerCase(); });
        const missing = COLS.filter(function (c) { return !have[c]; });
        W.inflight = null;
        if (!rows.length) { fail('tabla ' + C.table + ' no encontrada en la base configurada'); return out; }
        if (missing.length) { fail('faltan columnas en ' + C.table + ': ' + missing.join(', ')); return out; }
        W.schema = { tsType: have.ts };
        W.link = 'conectada'; W.lastOkAt = now; W.backoff = 0;
        node.log('market_1s: columna ts = ' + have.ts + (have.ts === 'datetime' || have.ts === 'timestamp' ? ' (se guarda en UTC)' : have.ts === 'bigint' || have.ts === 'decimal' ? ' (ms epoch)' : ' (segundos epoch)'));
        node.status({ fill: 'green', shape: 'dot', text: 'ts=' + have.ts + ' · listo' });
        return null;
    }
    // insert OK
    const n = W.inflight.rows.length;
    W.saved += n; W.lastSavedT = W.inflight.maxT; W.lastOkAt = now; W.link = 'conectada'; W.backoff = 0;
    W.inflight = null;
    if (W.q.length >= C.batchMax) W.lastFlush = 0; else W.lastFlush = now;   // hay atraso: siguiente lote en el próximo tick
    node.status({ fill: 'green', shape: 'dot', text: W.saved + ' filas · cola ' + W.q.length });
    return null;
}

if (msg.topic === 'db_status') {                             // desde el status del nodo mysql
    const st = msg.status || {}, tx = String(st.text || '').toLowerCase();
    if (st.fill === 'green') { W.link = 'conectada'; }
    else if (st.fill === 'grey' || tx.indexOf('connecting') >= 0) { if (W.link !== 'error') W.link = 'conectando'; }
    else if (st.fill === 'red') { W.link = 'error'; }
    return null;
}

if (msg.topic !== 'tick') return null;
// ---- tick 1 s: timeout, envío de lotes, indicador ----
if (W.inflight && now - W.inflight.sentAt > C.timeoutMs) fail('timeout: MariaDB no respondió en ' + (C.timeoutMs / 1000) + ' s');
if (!W.inflight && now >= W.nextTry) {
    if (!W.schema) {
        W.inflight = { id: ++W.seq, kind: 'schema', sentAt: now };
        out[0] = { topic: 'SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = ?',
            payload: [C.table], dbKind: 'schema', dbBatch: W.inflight.id };
    } else if (W.q.length && (W.q.length >= C.batchMax || now - W.lastFlush >= C.flushMs)) {
        const rows = W.q.splice(0, C.batchMax);
        let maxT = 0; for (const b of rows) if (b.t > maxT) maxT = b.t;
        W.inflight = { id: ++W.seq, kind: 'insert', rows: rows, sentAt: now, maxT: maxT };
        const upd = COLS.slice(2).map(function (c) { return c + ' = VALUES(' + c + ')'; }).join(', ');
        const pre = W.schema.tsType === 'timestamp' ? "SET time_zone = '+00:00'; " : '';
        out[0] = { topic: pre + 'INSERT INTO ' + C.table + ' (' + COLS.join(', ') + ') VALUES ? ON DUPLICATE KEY UPDATE ' + upd,
            payload: [rows.map(row)], dbKind: 'insert', dbBatch: W.inflight.id };
    }
}
indicator();
return out;`;

const DB_TPL = String.raw`<style>
.prrr-db{font-family:monospace;font-size:13px;line-height:1.55}
.prrr-db .ok{color:#27ae60;font-weight:bold}.prrr-db .bad{color:#e74c3c;font-weight:bold}.prrr-db .mid{color:#e6a700;font-weight:bold}
</style>
<div class="prrr-db" ng-if="msg.payload" title="cola: {{msg.payload.queue}} filas · descartadas por tope: {{msg.payload.dropped}} · último error: {{msg.payload.lastErr || '—'}}">
  DB: <span ng-class="msg.payload.ok ? 'ok' : (msg.payload.db==='conectando' ? 'mid' : 'bad')">{{msg.payload.db}}</span><br>
  último bucket guardado: <b>{{msg.payload.lastSaved ? (msg.payload.lastSaved | date:'HH:mm:ss') : '—'}}</b><br>
  filas guardadas: <b>{{msg.payload.saved}}</b><br>
  errores DB: <b ng-class="{'bad': msg.payload.errors > 0}">{{msg.payload.errors}}</b>
</div>
<div class="prrr-db" ng-if="!msg.payload">DB: esperando…</div>`;

add({ id: DBCFG, type: 'MySQLdatabase', name: 'MariaDB prrr_market (localhost)', host: '127.0.0.1', port: '3306', db: 'prrr_market', tz: '', charset: 'UTF8' });
const G_DB = { id: 'prrr_ui_g_db', name: 'PERSISTENCIA DB', order: 10, width: 6 };
add({ id: G_DB.id, type: 'ui_group', name: G_DB.name, tab: UI_TAB, order: G_DB.order, disp: true, width: String(G_DB.width), collapse: false, className: '' });
const gDb = egroup('prrr_grp_db', 'ETAPA 3 — PERSISTENCIA MariaDB (buckets 1 s → market_1s, desacoplado)', '#8c564b');
const DY = PY + 160;
inGroup(gDb, { id: 'prrr_db_in', type: 'link in', z: TAB, name: 'BUCKETS 1s', links: ['prrr_s2_buckets_out'], x: 180, y: DY, wires: [[DBW]] });
inGroup(gDb, { id: 'prrr_db_tick', type: 'inject', z: TAB, name: 'tick 1 s', props: [{ p: 'topic', vt: 'str' }],
  repeat: '1', crontab: '', once: true, onceDelay: '3', topic: 'tick', x: 170, y: DY + 50, wires: [[DBW]] });
inGroup(gDb, { id: DBW, type: 'function', z: TAB, name: 'DB writer (cola acotada · lotes · reintento)',
  func: DB_FUNC, outputs: 2, timeout: 0, noerr: 0, initialize: DB_INIT, finalize: '', libs: [],
  outputLabels: ['query → mysql', 'indicador'], x: 430, y: DY + 20, wires: [[DBM], ['prrr_db_ui']] });
inGroup(gDb, { id: DBM, type: 'mysql', z: TAB, mydb: DBCFG, name: 'MariaDB market_1s', x: 690, y: DY, wires: [[DBW]] });
inGroup(gDb, { id: 'prrr_db_catch', type: 'catch', z: TAB, name: 'errores mysql', scope: [DBM], uncaught: false, x: 680, y: DY + 50, wires: [['prrr_db_err_tag']] });
inGroup(gDb, { id: 'prrr_db_err_tag', type: 'change', z: TAB, name: 'topic = db_error',
  rules: [{ t: 'set', p: 'topic', pt: 'msg', to: 'db_error', tot: 'str' }], action: '', property: '', from: '', to: '', reg: false, x: 870, y: DY + 50, wires: [[DBW]] });
inGroup(gDb, { id: 'prrr_db_status', type: 'status', z: TAB, name: 'estado mysql', scope: [DBM], x: 680, y: DY + 95, wires: [['prrr_db_st_tag']] });
inGroup(gDb, { id: 'prrr_db_st_tag', type: 'change', z: TAB, name: 'topic = db_status',
  rules: [{ t: 'set', p: 'topic', pt: 'msg', to: 'db_status', tot: 'str' }], action: '', property: '', from: '', to: '', reg: false, x: 870, y: DY + 95, wires: [[DBW]] });
inGroup(gDb, Object.assign({}, tplBase, { id: 'prrr_db_ui', name: 'indicador PERSISTENCIA DB', group: G_DB.id, order: 1, width: 6, height: 2, format: DB_TPL, x: 690, y: DY + 145 }));
inGroup(gDb, { id: 'prrr_db_comment', type: 'comment', z: TAB, name: 'Mapeo bucket → market_1s',
  info: '| market_1s | bucket |\n|---|---|\n| ts | t (inicio del segundo). DATETIME/TIMESTAMP: UTC · BIGINT: ms epoch · INT: s epoch (detectado en information_schema) |\n| symbol | symbol |\n| open/high/low/close_price | open/high/low/close |\n| trades | trades (fills reales) |\n| volume_usd | vol_usd |\n| buy_usd / sell_usd / delta_usd | buy_usd / sell_usd / delta_usd |\n| exchanges | exchanges |\n| spot_trades / perp_trades | spot_trades / perp_trades |\n| no_trade | 1 si synthetic (segundo sin trades, precio arrastrado) |\n\nLos buckets `no_price` (todavía no hubo ningún precio) no se guardan.\n\n`INSERT ... VALUES ? ON DUPLICATE KEY UPDATE` en lotes de hasta 400 filas cada ≤ 5 s. Cola en RAM acotada a 14400 filas (1 h); un solo lote en vuelo; reintento con backoff 5 → 60 s; timeout 30 s.\n\nRequiere `node-red-node-mysql`. Usuario y contraseña se cargan en el config node (credentials), nunca en código.',
  x: 450, y: DY + 145, wires: [] });

// ===========================================================================
// ETAPA 4 — ANALÍTICA 25/50 (sólo calcular y observar). Rama nueva desde BUCKETS 1s.
// No toca RAW, normalizador, MariaDB ni el camino crítico del PRRR.
// ===========================================================================
const AN = 'prrr_an_2550';
const AN_INIT = String.raw`// ===== ANALÍTICA 25/50: estado =====
// buf: últimos 50 buckets por activo (tal cual los genera el normalizador)
// hist: SMA25/SMA50 por segundo durante 4 h (sólo para dibujar las líneas al recargar la página)
const A = { windows: [25, 50], syms: ['BTC', 'ZEC', 'GMX', 'XMR'], buf: {}, hist: {}, histCap: 14400, last: {} };
for (const s of A.syms) {
    A.buf[s] = [];
    A.hist[s] = { idx: new Float64Array(A.histCap).fill(-1), s25: new Float64Array(A.histCap), s50: new Float64Array(A.histCap) };
}
context.set('A', A, 'memory');`;

const AN_FUNC = String.raw`// Entradas: 'bucket_1s' (los 4 buckets del segundo cerrado) y 'snapshot_req' (navegador recién abierto).
// Salida 1 → MERCADO mux (topic 'an')   ·   Salida 2 → historial SMA al navegador que lo pidió
const A = context.get('A', 'memory');

// --- módulo de indicadores: funciones puras sobre una lista de buckets (más viejo → más nuevo) ---
function sma(buckets, n) {                     // media de close de los últimos n buckets con precio (no_trade incluido: precio arrastrado)
    let sum = 0, k = 0;
    for (let i = buckets.length - 1; i >= 0 && k < n; i--) { const c = buckets[i].close; if (c != null) { sum += c; k++; } }
    return k === n ? sum / n : null;          // null = todavía no hay n segundos válidos
}
function flow(buckets, n) {                    // Δ y volumen de los últimos n segundos (no_trade aporta 0)
    let d = 0, v = 0; const w = buckets.slice(-n);
    for (const b of w) { d += b.delta_usd || 0; v += b.vol_usd || 0; }
    return { delta: d, volume: v, pressure: v > 0 ? d / v : 0, seconds: w.length };
}

if (msg.topic === 'snapshot_req') {
    const snap = {};
    for (const s of A.syms) {
        const H = A.hist[s], arr = [];
        for (let k = 0; k < A.histCap; k++) if (H.idx[k] >= 0) arr.push(H.idx[k], H.s25[k], H.s50[k]);
        snap[s] = arr;
    }
    return [null, { socketid: msg.socketid, payload: { smaSnapshot: snap } }];
}
if (msg.topic !== 'bucket_1s' || !Array.isArray(msg.payload)) return null;

const out = { t: msg.t };
for (const b of msg.payload) {
    const buf = A.buf[b.symbol];
    if (!buf) continue;
    buf.push(b);
    if (buf.length > 50) buf.shift();
    const s25 = sma(buf, 25), s50 = sma(buf, 50), f25 = flow(buf, 25), f50 = flow(buf, 50);
    const r = {
        t: b.t,
        price_sma_25: s25, price_sma_50: s50,
        price_sma_spread_pct: (s25 != null && s50 != null) ? (s25 / s50 - 1) * 100 : null,
        delta_25: f25.delta, delta_50: f50.delta,
        volume_25: f25.volume, volume_50: f50.volume,
        pressure_25: f25.pressure, pressure_50: f50.pressure,
        seconds: buf.length
    };
    A.last[b.symbol] = r; out[b.symbol] = r;
    if (s25 != null || s50 != null) {           // historial para las líneas del gráfico
        const H = A.hist[b.symbol], sec = Math.floor(b.t / 1000), k = sec % A.histCap;
        H.idx[k] = sec; H.s25[k] = s25 == null ? NaN : s25; H.s50[k] = s50 == null ? NaN : s50;
    }
}
global.set('md_an_2550', A.last, 'memory');
return [{ topic: 'an', sym: 'ALL', payload: out }, null];`;

const gAn = egroup('prrr_grp_an', 'ETAPA 4 — ANALÍTICA 25/50 (sólo observar: SMA · Δ · volumen · presión)', '#bcbd22');
const AY = DY + 260;
inGroup(gAn, { id: 'prrr_an_in', type: 'link in', z: TAB, name: 'BUCKETS 1s', links: ['prrr_s2_buckets_out'], x: 180, y: AY, wires: [[AN]] });
inGroup(gAn, { id: AN, type: 'function', z: TAB, name: 'ANALÍTICA 25/50 (SMA · Δ · vol · presión)',
  func: AN_FUNC, outputs: 2, timeout: 0, noerr: 0, initialize: AN_INIT, finalize: '', libs: [],
  outputLabels: ['valores 25/50 → MERCADO', 'historial SMA → navegador que lo pidió'],
  x: 450, y: AY, wires: [['prrr_ui_mkt_mux'], ['prrr_ui_mkt']] });
inGroup(gAn, { id: 'prrr_an_comment', type: 'comment', z: TAB, name: 'Definiciones 25/50',
  info: 'Fuente: buckets de 1 s del normalizador (no RAW).\n\n* `price_sma_25/50`: media simple de `close` de los últimos 25/50 segundos con precio (los `no_trade` participan con su precio arrastrado). `null` hasta tener 25/50 segundos.\n* `price_sma_spread_pct = (SMA25 / SMA50 − 1) × 100`\n* `delta_25/50 = Σ delta_usd` · `volume_25/50 = Σ vol_usd` de los últimos 25/50 segundos (`no_trade` aporta 0)\n* `pressure_25/50 = delta / volumen` (0 si el volumen es 0), rango ≈ −1…+1\n\nNo es señal de compra/venta. Valores en `global.get("md_an_2550","memory")`. No se escriben en MariaDB (derivables de `market_1s`).',
  x: 450, y: AY + 50, wires: [] });

// ===========================================================================
// ETAPA 5 — LABORATORIO HISTÓRICO (sólo lectura de market_1s; estadística descriptiva)
// Todo el cálculo pesado corre DENTRO de MariaDB (otro proceso) y vuelve a Node-RED sólo un
// resultado chico (conteos + resumen + 1 página de filas): el event loop del PRRR no se bloquea.
// ===========================================================================
const LABC = 'prrr_lab_ctrl', LABM = 'prrr_lab_mysql', LABUI = 'prrr_lab_ui';
const LAB_INIT = String.raw`// ===== LAB: constructor de SQL (módulo reutilizable) =====
// buildLab({ featureSymbol, labelSymbol, periodSec, horizons, page, pageSize, tsType })
//   featureSymbol: activo cuyas variables se miden en T
//   labelSymbol:   activo cuyo precio futuro se mide (hoy = el mismo; mañana: BTC → ZEC)
const L = { schema: null, busy: null, pending: null, seq: 0 };
L.SYMS = ['BTC', 'ZEC', 'GMX', 'XMR'];
L.PERIODS = { '1h': 3600, '6h': 21600, '24h': 86400, '7d': 604800, 'all': 0 };
L.HORIZONS = [60, 300, 900, 3600];              // +1m +5m +15m +1h (segundos)
L.CONDS = [                                       // condiciones descriptivas (NO son señales)
    ['todas (referencia)', '1=1'],
    ['pressure25 > 0', 'p25 > 0'],
    ['pressure25 < 0', 'p25 < 0'],
    ['SMA25 > SMA50', 'sma25 > sma50'],
    ['SMA25 < SMA50', 'sma25 < sma50'],
    ['SMA25 > SMA50 y pressure25 > 0', 'sma25 > sma50 AND p25 > 0'],
    ['SMA25 < SMA50 y pressure25 < 0', 'sma25 < sma50 AND p25 < 0']
];
L.buildLab = function (o) {
    const T = o.tsType, dt = (T === 'datetime' || T === 'timestamp'), ms = (T === 'bigint' || T === 'decimal');
    const plus = (e, s) => dt ? '(' + e + ' + INTERVAL ' + s + ' SECOND)' : ms ? '(' + e + ' + ' + s * 1000 + ')' : '(' + e + ' + ' + s + ')';
    const minus = (e, s) => dt ? '(' + e + ' - INTERVAL ' + s + ' SECOND)' : ms ? '(' + e + ' - ' + s * 1000 + ')' : '(' + e + ' - ' + s + ')';
    const secOf = (e) => dt ? 'TO_SECONDS(' + e + ')' : ms ? '(' + e + ' DIV 1000)' : e;
    const tsOut = (e) => dt ? "DATE_FORMAT(" + e + ", '%Y-%m-%dT%H:%i:%sZ')" : ms ? e : '(' + e + ' * 1000)';
    const FS = "'" + o.featureSymbol + "'", LS = "'" + o.labelSymbol + "'", H = o.horizons;
    const lim = 'SET STATEMENT max_statement_time = ' + o.maxSec + ' FOR\n';
    // 0) inicio del período y muestreo (constantes → lectura por rango de la PK)
    const q0 = 'SET @t_from = ' + (o.periodSec ? minus('(SELECT MAX(ts) FROM market_1s WHERE symbol = ' + FS + ')', o.periodSec) : '(SELECT MIN(ts) FROM market_1s WHERE symbol = ' + FS + ')') + ';\n' +
        'SET @n_rows = (SELECT COUNT(*) FROM market_1s WHERE symbol = ' + FS + ' AND ts >= @t_from);\n' +
        'SET @stride = GREATEST(1, CEIL(@n_rows / ' + o.maxObs + '))';
    // contigüidad: los 50 segundos T-49..T existen ⇔ el ts de 49 filas atrás es exactamente T-49 s (ts es único en la PK)
    const histOk = 'COALESCE(LAG(ts, 49) OVER (ORDER BY ts) = ' + minus('ts', 49) + ', 0)';
    const futJoins = (alias) => H.map((h, i) => '  LEFT JOIN market_1s q' + i + ' ON q' + i + '.symbol = ' + LS + ' AND q' + i + '.ts = ' + plus(alias + '.ts', h)).join('\n');
    // VARIABLES EN T: ventanas ROWS 24/49 PRECEDING .. CURRENT ROW → sólo pasado y presente (sin look-ahead)
    const featCTE = (fromVar) =>
        'WITH base AS (SELECT m.ts, m.close_price * 1e0 AS close_price, m.delta_usd * 1e0 AS delta_usd, m.volume_usd * 1e0 AS volume_usd,\n' +
        '           m.trades, m.exchanges, m.spot_trades, m.perp_trades\n' +
        '         FROM market_1s m WHERE m.symbol = ' + FS + ' AND m.ts >= ' + minus(fromVar, 49) + '),\n' +
        'feat AS (SELECT ts, close_price AS price,\n' +
        '    AVG(close_price) OVER w25 AS sma25, AVG(close_price) OVER w50 AS sma50,\n' +
        '    SUM(delta_usd) OVER w25 AS d25, SUM(delta_usd) OVER w50 AS d50,\n' +
        '    SUM(volume_usd) OVER w25 AS v25, SUM(volume_usd) OVER w50 AS v50,\n' +
        '    SUM(trades) OVER w25 AS tr25, SUM(trades) OVER w50 AS tr50,\n' +
        '    exchanges AS ex_now, AVG(exchanges) OVER w25 AS ex_avg25,\n' +
        '    SUM(spot_trades) OVER w25 AS spot25, SUM(perp_trades) OVER w25 AS perp25,\n' +
        '    ' + histOk + ' AS hist_ok\n' +
        '  FROM base\n' +
        '  WINDOW w25 AS (ORDER BY ts ROWS BETWEEN 24 PRECEDING AND CURRENT ROW),\n' +
        '         w50 AS (ORDER BY ts ROWS BETWEEN 49 PRECEDING AND CURRENT ROW)),\n';
    // LABELS FUTUROS: precio exacto del activo-label en T + h (si esa fila no existe → NULL, no se inventa)
    const labCTE = (where) =>
        'obs AS (SELECT f.*,\n' + H.map((h, i) => '    q' + i + '.close_price * 1e0 AS fut' + i).join(',\n') + '\n' +
        '  FROM feat f\n' + futJoins('f') + '\n' +
        '  WHERE ' + where + '),\n' +
        'lab AS (SELECT ts, price, sma25, sma50, (sma25 / sma50 - 1) * 100 AS spread, d25, d50, v25, v50,\n' +
        '    CASE WHEN v25 > 0 THEN d25 / v25 ELSE 0 END AS p25, CASE WHEN v50 > 0 THEN d50 / v50 ELSE 0 END AS p50,\n' +
        '    tr25, tr50, ex_now, ex_avg25, spot25, perp25,\n' +
        H.map((h, i) => '    (fut' + i + ' / price - 1) * 100 AS r' + i).join(',\n') + '\n' +
        '  FROM obs)\n';
    // 1) COBERTURA (todas las observaciones del período, sin muestreo; liviana: sin ventanas de precio)
    const q1 = lim +
        'WITH base AS (SELECT ts FROM market_1s WHERE symbol = ' + FS + ' AND ts >= ' + minus('@t_from', 49) + '),\n' +
        'h AS (SELECT ts, ' + histOk + ' AS hist_ok FROM base),\n' +
        'obs AS (SELECT h.ts, h.hist_ok,\n' + H.map((x, i) => '    q' + i + '.ts IS NOT NULL AS has' + i).join(',\n') + '\n' +
        '  FROM h\n' + futJoins('h') + '\n  WHERE h.ts >= @t_from)\n' +
        'SELECT ' + tsOut('@t_from') + ' AS t_from, ' + tsOut('MIN(ts)') + ' AS t_first, ' + tsOut('MAX(ts)') + ' AS t_last, @stride AS stride,\n' +
        '  COUNT(*) AS rows_avail, SUM(hist_ok = 0) AS no_hist, SUM(hist_ok = 1) AS usable,\n' +
        H.map((x, i) => '  SUM(hist_ok = 1 AND has' + i + ') AS lab' + i + ', SUM(hist_ok = 1 AND NOT has' + i + ') AS nofut' + i).join(',\n') + '\n' +
        'FROM obs';
    // 2) RESUMEN por condición y horizonte: N, media, mediana exacta, % > 0 (sobre 1 de cada @stride segundos)
    const conds = L.CONDS.map((c, k) => 'SELECT ' + k + ' AS k').join(' UNION ALL ');
    const hz = H.map((h, i) => 'SELECT ' + i + ' AS i').join(' UNION ALL ');
    const q2 = lim + featCTE('@t_from') + labCTE('f.ts >= @t_from AND f.hist_ok = 1 AND MOD(' + secOf('f.ts') + ', @stride) = 0') +
        ', cnd AS (' + conds + '), hz AS (' + hz + '),\n' +
        'lng AS (SELECT c.k, z.i, CASE z.i ' + H.map((h, i) => 'WHEN ' + i + ' THEN l.r' + i).join(' ') + ' END AS r\n' +
        '  FROM lab l JOIN cnd c ON (' + L.CONDS.map((c, k) => '(c.k = ' + k + ' AND ' + c[1].replace(/(sma25|sma50|p25)/g, 'l.$1') + ')').join(' OR ') + ')\n' +
        '  CROSS JOIN hz z),\n' +
        'rk AS (SELECT k, i, r, ROW_NUMBER() OVER (PARTITION BY k, i ORDER BY r) AS rn, COUNT(*) OVER (PARTITION BY k, i) AS cnt FROM lng WHERE r IS NOT NULL)\n' +
        'SELECT k, i, COUNT(*) AS n, AVG(r) AS mean,\n' +
        '  AVG(CASE WHEN rn = FLOOR((cnt + 1) / 2) OR rn = FLOOR(cnt / 2) + 1 THEN r END) AS med,\n' +   // mediana exacta (par: promedio de los 2 centrales)
        '  SUM(r > 0) AS pos\n' +
        'FROM rk GROUP BY k, i';
    // 3) PÁGINA de observaciones (todas, sin muestreo): sólo se calcula el tramo reciente necesario (búsqueda por índice)
    const q3 = 'SET @t_page = GREATEST(@t_from, COALESCE((SELECT ts FROM market_1s WHERE symbol = ' + FS + ' AND ts >= @t_from ORDER BY ts DESC LIMIT 1 OFFSET ' + ((o.page + 1) * o.pageSize + 300) + '), @t_from));\n' +
        lim + featCTE('@t_page') + labCTE('f.ts >= @t_page AND f.hist_ok = 1') +
        'SELECT ' + tsOut('ts') + ' AS ts, price, sma25, sma50, spread, d25, d50, v25, v50, p25, p50, tr25, tr50, ex_now, ex_avg25, spot25, perp25, ' +
        H.map((h, i) => 'r' + i).join(', ') + '\nFROM lab ORDER BY ts DESC LIMIT ' + o.pageSize + ' OFFSET ' + (o.page * o.pageSize);
    return q0 + ';\n' + q1 + ';\n' + q2 + ';\n' + q3;
};
context.set('L', L, 'memory');`;

const LAB_FUNC = String.raw`// Entradas: 'lab_run' (desde LABORATORIO), resultado/errores del nodo mysql del laboratorio.
// Salida 1 → mysql (1 consulta a la vez, con tiempo máximo)   ·   Salida 2 → LABORATORIO (sólo al navegador que pidió)
const L = context.get('L', 'memory'), now = Date.now();
const MAXSEC = 120, PAGE = 100;
function toUI(p, sid) { return [null, { socketid: sid, payload: p }]; }

function start(req) {
    if (!L.schema) {
        L.busy = { id: ++L.seq, kind: 'schema', req: req, at: now };
        return [{ topic: "SELECT DATA_TYPE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'market_1s' AND COLUMN_NAME = 'ts'",
            payload: [], labKind: 'schema', labId: L.busy.id }, { socketid: req.sid, payload: { status: 'consultando', at: now } }];
    }
    const sql = L.buildLab({ featureSymbol: req.symbol, labelSymbol: req.symbol, periodSec: L.PERIODS[req.period], horizons: L.HORIZONS,
        page: req.page, pageSize: PAGE, tsType: L.schema, maxSec: MAXSEC, maxObs: 120000 });
    L.busy = { id: ++L.seq, kind: 'lab', req: req, at: now };
    return [{ topic: sql, payload: [], labKind: 'lab', labId: L.busy.id }, { socketid: req.sid, payload: { status: 'consultando', at: now, req: req } }];
}
function next() { if (L.pending) { const r = L.pending; L.pending = null; return start(r); } return [null, null]; }

if (msg.topic === 'lab_run') {
    const p = msg.payload || {};
    const req = { symbol: L.SYMS.indexOf(p.symbol) >= 0 ? p.symbol : 'BTC', period: L.PERIODS.hasOwnProperty(p.period) ? p.period : '1h',
        page: Math.max(0, Math.min(10000, parseInt(p.page, 10) || 0)), sid: msg.socketid };
    if (L.busy && now - L.busy.at < (MAXSEC + 30) * 1000) { L.pending = req; return toUI({ status: 'en cola', req: req }, req.sid); }
    return start(req);
}

if (msg.topic === 'lab_error' || msg.error) {
    if (!L.busy || msg.labId !== L.busy.id) return null;
    const b = L.busy; L.busy = null;
    const e = (msg.error && msg.error.message) || 'error';
    node.warn('LABORATORIO: ' + e);
    const out = next();
    out[1] = out[1] || null;
    return [out[0], { socketid: b.req.sid, payload: { status: 'error', error: /max_statement_time|interrupted/i.test(e) ? 'La consulta superó ' + MAXSEC + ' s y fue cancelada: elegí un período menor.' : e, req: b.req } }];
}

if (msg.labKind) {
    if (!L.busy || msg.labId !== L.busy.id) return null;
    const b = L.busy; L.busy = null;
    if (msg.labKind === 'schema') {
        const r = Array.isArray(msg.payload) && msg.payload[0];
        if (!r) return [null, { socketid: b.req.sid, payload: { status: 'error', error: 'tabla market_1s no encontrada' } }];
        L.schema = String(r.DATA_TYPE).toLowerCase();
        return start(b.req);
    }
    // resultado: [ [] (SET STATEMENT), cobertura, [] , resumen, [], página ] — se toman sólo los conjuntos de filas
    const sets = (Array.isArray(msg.payload) ? msg.payload : []).filter(function (x) { return Array.isArray(x); });
    const cov = (sets[0] && sets[0][0]) || {}, sum = sets[1] || [], rows = sets[2] || [];
    const res = {
        status: 'ok', req: b.req, ms: now - b.at, horizons: L.HORIZONS, conds: L.CONDS.map(function (c) { return c[0]; }), pageSize: PAGE,
        coverage: cov, summary: sum, rows: rows
    };
    const out = next();
    return [out[0], { socketid: b.req.sid, payload: res }];
}
return null;`;

const LAB_TPL = String.raw`<style>
#prrr_lab{font-family:monospace;font-size:13px}
#prrr_lab .bar{display:flex;flex-wrap:wrap;align-items:center;gap:14px;margin-bottom:8px}
#prrr_lab .bar button{font:inherit;padding:3px 10px;border:1px solid rgba(128,128,128,.5);border-radius:3px;background:transparent;color:inherit;cursor:pointer}
#prrr_lab .bar button.on{background:#1f77b4;border-color:#1f77b4;color:#fff}
#prrr_lab .bar .go{background:#2ca02c;border-color:#2ca02c;color:#fff;font-weight:bold}
#prrr_lab .st{opacity:.8}
#prrr_lab .note{opacity:.7;font-size:12px;margin:4px 0 10px 0}
#prrr_lab .cov span{display:inline-block;margin-right:22px}
#prrr_lab .warn{color:#e6a700;font-weight:bold}
#prrr_lab table{border-collapse:collapse;width:100%;margin:6px 0 12px 0}
#prrr_lab th,#prrr_lab td{padding:2px 7px;text-align:right;border-bottom:1px solid rgba(128,128,128,.22);white-space:nowrap}
#prrr_lab th{opacity:.8}
#prrr_lab .l{text-align:left}
#prrr_lab .pos{color:#27ae60}#prrr_lab .neg{color:#e74c3c}
#prrr_lab .ins{color:#e6a700}
#prrr_lab td.hz,#prrr_lab th.hz{border-left:1px solid rgba(128,128,128,.4)}
#prrr_lab h4{margin:10px 0 2px 0;font-size:14px}
</style>
<div id="prrr_lab">
  <div class="bar">
    <span><b>ACTIVO</b> <button data-s="BTC">BTC</button><button data-s="ZEC">ZEC</button><button data-s="GMX">GMX</button><button data-s="XMR">XMR</button></span>
    <span><b>PERÍODO</b> <button data-p="1h">última 1h</button><button data-p="6h">6h</button><button data-p="24h">24h</button><button data-p="7d">7d</button><button data-p="all">todo disponible</button></span>
    <button class="go">ACTUALIZAR</button>
    <span class="st"></span>
  </div>
  <div class="note">Estadística histórica <b>descriptiva</b> sobre market_1s. No es una señal, ni una probabilidad de compra, ni una recomendación. Las variables en T usan sólo T y los segundos anteriores; los retornos +1m…+1h son labels futuros usados únicamente para analizar el pasado. Horas en tu hora local (la base está en UTC).</div>
  <div class="body"></div>
</div>
<script>
(function (scope) {
  var S = window.__prrrLab || (window.__prrrLab = { symbol: 'BTC', period: '1h', page: 0, last: null });
  var HZ = ['+1m', '+5m', '+15m', '+1h'], HS = [60, 300, 900, 3600], MIN_EFF = 30;
  function $(q) { var r = document.getElementById('prrr_lab'); return r ? r.querySelector(q) : null; }
  function f(v, d) { return v == null ? '—' : (+v).toFixed(d); }
  function sg(v) { return v > 0 ? 'pos' : v < 0 ? 'neg' : ''; }
  function money(v) { if (v == null) return '—'; var a = Math.abs(v), s = v < 0 ? '−' : v > 0 ? '+' : ''; return s + '$' + (a >= 1e6 ? (a / 1e6).toFixed(2) + 'M' : a >= 1e3 ? (a / 1e3).toFixed(1) + 'k' : a.toFixed(0)); }
  function vol(v) { return v == null ? '—' : '$' + (v >= 1e6 ? (v / 1e6).toFixed(2) + 'M' : v >= 1e3 ? (v / 1e3).toFixed(1) + 'k' : (+v).toFixed(0)); }
  function px(p) { return p == null ? '—' : p >= 1000 ? (+p).toFixed(2) : p >= 1 ? (+p).toFixed(4) : (+p).toPrecision(6); }
  function ts(v) { if (v == null) return '—'; var d = new Date(typeof v === 'string' ? Date.parse(v) : +v); if (isNaN(d)) return String(v);
    var p = function (n) { return ('0' + n).slice(-2); }; return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds()); }
  function run(page) { S.page = page || 0; bar(); scope.send({ topic: 'lab_run', payload: { symbol: S.symbol, period: S.period, page: S.page } }); }
  function bar() {
    var r = document.getElementById('prrr_lab'); if (!r) return;
    r.querySelectorAll('button[data-s]').forEach(function (b) { b.classList.toggle('on', b.getAttribute('data-s') === S.symbol); });
    r.querySelectorAll('button[data-p]').forEach(function (b) { b.classList.toggle('on', b.getAttribute('data-p') === S.period); });
  }
  function render(p) {
    var st = $('.st'), body = $('.body'); if (!st || !body) return;
    if (p.status === 'consultando' || p.status === 'en cola') { st.textContent = p.status === 'en cola' ? 'en cola (hay otra consulta en curso)…' : 'consultando MariaDB…'; return; }
    if (p.status === 'error') { st.innerHTML = '<span class="neg">error: ' + String(p.error).replace(/</g, '&lt;') + '</span>'; return; }
    S.last = p;
    var c = p.coverage || {}, usable = +c.usable || 0;
    st.textContent = 'listo · ' + p.req.symbol + ' · ' + p.req.period + ' · ' + (p.ms / 1000).toFixed(1) + ' s';
    var h = '<div class="cov"><span>desde <b>' + ts(c.t_first) + '</b></span><span>hasta <b>' + ts(c.t_last) + '</b></span>'
      + '<span>registros disponibles <b>' + (c.rows_avail || 0) + '</b></span><span>observaciones utilizables <b>' + usable + '</b></span>'
      + '<span>descartadas por falta de historia (50 s contiguos) <b>' + (c.no_hist || 0) + '</b></span></div>'
      + (+c.stride > 1 ? '<div class="note">Período largo: el resumen usa 1 de cada <b>' + c.stride + '</b> segundos (muestreo sistemático) para que la consulta no sobrecargue la notebook. Las variables de cada T se calculan igual con todos los segundos; como observaciones vecinas se solapan casi por completo, el N independiente prácticamente no cambia.</div>' : '')
      + '<div class="cov">' + HZ.map(function (z, i) { return '<span>' + z + ': con futuro <b>' + (c['lab' + i] || 0) + '</b> · sin futuro todavía/hueco <b>' + (c['nofut' + i] || 0) + '</b></span>'; }).join('') + '</div>';
    if (!usable) h += '<p class="warn">Todavía no hay observaciones utilizables en este período.</p>';
    // resumen
    var M = {}; (p.summary || []).forEach(function (s) { M[s.k + '_' + s.i] = s; });
    h += '<h4>Resumen descriptivo: retorno futuro (%) según la condición observada en T</h4>'
      + '<div class="note">N = observaciones (1 por segundo, o 1 cada k s si hay muestreo; se solapan). N indep. ≈ N × k s / horizonte: cuántas ventanas no solapadas representan. Si N indep. &lt; ' + MIN_EFF + ' se marca muestra insuficiente.</div>'
      + '<table><tr><th class="l">condición en T</th>' + HZ.map(function (z) { return '<th class="hz">' + z + ' N</th><th>N indep.</th><th>media</th><th>mediana</th><th>% &gt; 0</th>'; }).join('') + '</tr>';
    (p.conds || []).forEach(function (name, k) {
      h += '<tr><td class="l">' + name + '</td>';
      for (var i = 0; i < HZ.length; i++) {
        var s = M[k + '_' + i], n = s ? +s.n : 0, eff = Math.floor(n * Math.max(1, +c.stride || 1) / HS[i]);
        if (!n) { h += '<td class="hz">0</td><td>0</td><td>—</td><td>—</td><td>—</td>'; continue; }
        var ins = eff < MIN_EFF;
        h += '<td class="hz">' + n + '</td><td class="' + (ins ? 'ins' : '') + '"' + (ins ? ' title="Muestra insuficiente: ' + n + ' observaciones (≈' + eff + ' independientes)"' : '') + '>' + eff + (ins ? ' ⚠' : '') + '</td>'
          + '<td class="' + sg(s.mean) + '">' + f(s.mean, 4) + '</td><td class="' + sg(s.med) + '">' + f(s.med, 4) + '</td><td>' + f(100 * s.pos / n, 1) + '</td>';
      }
      h += '</tr>';
    });
    h += '</table>';
    var insAll = HZ.map(function (z, i) { var s = M['0_' + i], n = s ? +s.n : 0; return Math.floor(n * Math.max(1, +c.stride || 1) / HS[i]) < MIN_EFF ? z + ' (' + n + ' obs.)' : null; }).filter(Boolean);
    if (insAll.length) h += '<p class="warn">Muestra insuficiente para ' + insAll.join(', ') + '. No sacar conclusiones todavía: la herramienta se vuelve útil a medida que MariaDB acumula días/semanas.</p>';
    // observaciones
    var rows = p.rows || [], pg = p.req.page, ps = p.pageSize;
    h += '<h4>Observaciones (más recientes primero) · página ' + (pg + 1) + ' de ' + Math.max(1, Math.ceil(usable / ps)) + '</h4>'
      + '<div class="bar"><button class="prev"' + (pg ? '' : ' disabled') + '>◀ más recientes</button><button class="nextp"' + ((pg + 1) * ps < usable ? '' : ' disabled') + '>más antiguas ▶</button></div>'
      + '<table><tr><th class="l">T (hora local)</th><th>precio</th><th>SMA25</th><th>SMA50</th><th>spread %</th><th>Δ25</th><th>Δ50</th><th>VOL25</th><th>VOL50</th><th>P25 %</th><th>P50 %</th>'
      + HZ.map(function (z, i) { return '<th' + (i ? '' : ' class="hz"') + '>ret ' + z + ' %</th>'; }).join('') + '<th class="hz">trades25</th><th>trades50</th><th>exch (T / prom25)</th><th>spot/perp 25</th></tr>';
    rows.forEach(function (r) {
      h += '<tr><td class="l">' + ts(r.ts) + '</td><td>' + px(r.price) + '</td><td>' + px(r.sma25) + '</td><td>' + px(r.sma50) + '</td><td class="' + sg(r.spread) + '">' + f(r.spread, 4) + '</td>'
        + '<td class="' + sg(r.d25) + '">' + money(r.d25) + '</td><td class="' + sg(r.d50) + '">' + money(r.d50) + '</td><td>' + vol(r.v25) + '</td><td>' + vol(r.v50) + '</td>'
        + '<td class="' + sg(r.p25) + '">' + f(r.p25 * 100, 1) + '</td><td class="' + sg(r.p50) + '">' + f(r.p50 * 100, 1) + '</td>'
        + [0, 1, 2, 3].map(function (i) { var v = r['r' + i]; return '<td' + (i ? '' : ' class="hz"') + '><span class="' + sg(v) + '">' + (v == null ? '<span title="todavía no existe ese futuro (o hay un hueco)">—</span>' : f(v, 4)) + '</span></td>'; }).join('')
        + '<td class="hz">' + r.tr25 + '</td><td>' + r.tr50 + '</td><td>' + r.ex_now + ' / ' + f(r.ex_avg25, 1) + '</td><td>' + r.spot25 + '/' + r.perp25 + '</td></tr>';
    });
    h += '</table>';
    body.innerHTML = h;
    var pv = $('.prev'), nx = $('.nextp');
    if (pv) pv.addEventListener('click', function () { run(Math.max(0, S.page - 1)); });
    if (nx) nx.addEventListener('click', function () { run(S.page + 1); });
  }
  function wire() {
    var r = document.getElementById('prrr_lab'); if (!r || r.__w) return; r.__w = true;
    r.querySelectorAll('button[data-s]').forEach(function (b) { b.addEventListener('click', function () { S.symbol = b.getAttribute('data-s'); run(0); }); });
    r.querySelectorAll('button[data-p]').forEach(function (b) { b.addEventListener('click', function () { S.period = b.getAttribute('data-p'); run(0); }); });
    r.querySelector('.go').addEventListener('click', function () { run(S.page); });
  }
  scope.$watch('msg', function (m) { if (m && m.payload) { wire(); render(m.payload); } });
  setTimeout(function () { wire(); bar(); if (S.last) render(S.last); run(S.page); }, 0);   // al abrir la pestaña: consulta
})(scope);
</script>`;

// ---- nodos ----
add({ id: 'prrr_ui_tab_lab', type: 'ui_tab', name: 'LABORATORIO', icon: 'fa-flask', order: 3, disabled: false, hidden: false });
add({ id: 'prrr_ui_g_lab', type: 'ui_group', name: 'LABORATORIO HISTÓRICO · market_1s', tab: 'prrr_ui_tab_lab', order: 1, disp: true, width: '30', collapse: false, className: '' });
const gLab = egroup('prrr_grp_lab', 'ETAPA 5 — LABORATORIO HISTÓRICO (sólo lectura de market_1s; cálculo dentro de MariaDB)', '#7f7f7f');
const LBY = AY + 160;
inGroup(gLab, Object.assign({}, tplBase, { id: LABUI, name: 'LABORATORIO (selector · cobertura · resumen · observaciones)', group: 'prrr_ui_g_lab', order: 1, width: 30, height: 26,
  format: LAB_TPL, storeOutMessages: false, x: 220, y: LBY, wires: [[LABC]] }));
inGroup(gLab, { id: LABC, type: 'function', z: TAB, name: 'LAB controlador (SQL 25/50 + labels futuros · 1 consulta a la vez)',
  func: LAB_FUNC, outputs: 2, timeout: 0, noerr: 0, initialize: LAB_INIT, finalize: '', libs: [],
  outputLabels: ['consulta → mysql', 'resultado → LABORATORIO'], x: 560, y: LBY, wires: [[LABM], [LABUI]] });
inGroup(gLab, { id: LABM, type: 'mysql', z: TAB, mydb: DBCFG, name: 'MariaDB (laboratorio, sólo SELECT)', x: 880, y: LBY, wires: [[LABC]] });
inGroup(gLab, { id: 'prrr_lab_catch', type: 'catch', z: TAB, name: 'errores laboratorio', scope: [LABM], uncaught: false, x: 870, y: LBY + 50, wires: [['prrr_lab_err_tag']] });
inGroup(gLab, { id: 'prrr_lab_err_tag', type: 'change', z: TAB, name: 'topic = lab_error',
  rules: [{ t: 'set', p: 'topic', pt: 'msg', to: 'lab_error', tot: 'str' }], action: '', property: '', from: '', to: '', reg: false, x: 1060, y: LBY + 50, wires: [[LABC]] });
inGroup(gLab, { id: 'prrr_lab_comment', type: 'comment', z: TAB, name: 'Laboratorio: definiciones y garantías',
  info: 'Fuente única: `market_1s` (sólo SELECT). Requiere MariaDB ≥ 10.2 (funciones de ventana). Usuario con SELECT basta: no crea tablas.\n\n**Variables en T** (ventanas `ROWS BETWEEN 24/49 PRECEDING AND CURRENT ROW`, sólo pasado y presente; contigüidad: `LAG(ts,49) = T−49 s`): SMA25, SMA50, spread %, Δ25/50, VOL25/50, P25/50 (0 si VOL = 0), trades25/50, exchanges en T y promedio 25 s, spot/perp 25 s.\n\nUna observación es utilizable sólo si los 50 segundos T−49…T existen y son contiguos (huecos de Node-RED apagado → se descarta).\n\n**Labels futuros**: precio exacto en T+60/300/900/3600 s (LEFT JOIN por clave primaria). Si esa fila no existe todavía (o hay un hueco) el retorno es NULL y la observación no cuenta para ese horizonte.\n\n**Modularidad**: `buildLab({featureSymbol, labelSymbol, …})`: para BTC → ZEC basta pasar featureSymbol=BTC, labelSymbol=ZEC (no implementado en la UI todavía).\n\nCada consulta (una conexión): cobertura de todo el período (liviana), resumen con mediana exacta (ROW_NUMBER) y 1 página de 100 filas (sólo el tramo reciente), cada sentencia con `max_statement_time = 120 s`. Para períodos de más de 120 000 segundos el resumen usa 1 de cada k segundos (muestreo sistemático, k = ⌈filas/120 000⌉) y lo indica en pantalla; las variables de cada T siguen calculándose con todos los segundos. Una sola consulta en vuelo; sólo se ejecuta al abrir la pestaña, al cambiar parámetros o con ACTUALIZAR.',
  x: 560, y: LBY + 50, wires: [] });

// ===========================================================================
// ETAPA 6 — ZEC_GMX: precio de EJECUCIÓN de GMX v2 (Arbitrum) para ZEC/USD
// Fuente: oracle keeper de GMX  GET https://arbitrum-api.gmxinfra.io/prices/tickers  (REST; GMX no ofrece WebSocket)
//   minPrice / maxPrice = bid / ask del reporte de oráculo (Chainlink Data Streams) en formato de 30 decimales
//   USD = raw / 10^(30 − decimales del token)   · ZEC sintético, 8 decimales → raw / 10^22
// Totalmente independiente del PRRR: no toca WebSockets, normalizadores, buckets ni market_1s.
// tick 1 s → GMX poller → [http request] → GMX poller → (registros nuevos) → GMX DB writer → [mysql gmx_price]
//                                                     → (1 Hz) indicador DIAGNÓSTICO
// ===========================================================================
const GXP = 'prrr_gmx_poll', GXH = 'prrr_gmx_http', GXW = 'prrr_gmx_writer', GXM = 'prrr_gmx_mysql', GXUI = 'prrr_gmx_ui';
const GX_INIT = String.raw`// ===== ZEC_GMX POLLER: configuración y estado =====
const X = {
    cfg: {
        // Oracle keeper oficial de GMX para Arbitrum + fallbacks oficiales (mismos que usa la interfaz de GMX)
        hosts: ['https://arbitrum-api.gmxinfra.io', 'https://arbitrum-api-fallback.gmxinfra.io', 'https://arbitrum-api-fallback.gmxinfra2.io'],
        path: '/prices/tickers',
        // Tokens a seguir (dirección on-chain = identificador inequívoco; decimales del token en GMX)
        tokens: [{ symbol: 'ZEC', address: '0x6eabbaa3278556dc5b19c034dc26c0eab60d65b5', decimals: 8 }],
        pollMs: 1000,           // igual que la interfaz oficial (PRICES_UPDATE_INTERVAL = 1000 ms). Mínimo práctico 250 (tick). No bajar de 500 contra la API real.
        timeoutMs: 4000,        // una consulta sin respuesta en 4 s = error
        failSwitch: 3,          // errores seguidos → pasar al siguiente host
        primaryRetryMs: 600000, // estando en un fallback, volver a probar el primario cada 10 min
        backoffMax: 30000,      // con errores: 1 → 2 → 4 … ≤ 30 s entre consultas
        staleMs: 10000,         // dato GMX con más de 10 s = viejo (indicador en rojo, comparación marcada)
        ringSize: 3600          // últimos N registros en RAM (global md_gmx)
    },
    hostIdx: 0, hostSince: Date.now(), inflight: null, seq: 0, nextAt: 0, lastInd: 0, backoff: 0, errRun: 0,
    polls: 0, ok: 0, errors: 0, switches: 0, skippedBusy: 0, lastErr: '', lastErrAt: 0, lastLogAt: 0,
    rttLast: null, rttSum: 0, rttN: 0,
    sym: {}            // por símbolo: último registro, contadores, intervalos entre actualizaciones de la fuente
};
const R = { last: {}, ring: {} };
for (const t of X.cfg.tokens) {
    X.sym[t.symbol] = { last: null, updates: 0, repeats: 0, older: 0, sameTsChanged: 0, missing: 0, invalid: 0, ivals: [], recv: [] };
    R.ring[t.symbol] = { size: X.cfg.ringSize, buf: new Array(X.cfg.ringSize), idx: 0, count: 0 };
}
context.set('X', X, 'memory');
global.set('md_gmx', R, 'memory');
node.status({ fill: 'grey', shape: 'ring', text: 'esperando primer tick' });`;

const GX_FUNC = String.raw`// Entradas: 'tick' (250 ms; el período real de consulta es cfg.pollMs) · respuesta del nodo http request (msg.gmxReq) · 'gmx_http_error' (catch del http request)
// Salida 1 → http request (1 consulta en vuelo)  ·  Salida 2 → registros nuevos → GMX DB writer  ·  Salida 3 → indicador
// Sólo se emite un registro cuando la fuente publica una actualización nueva (updatedAt / min / max distintos):
// nada de ticks artificiales, interpolación ni relleno.
const X = context.get('X', 'memory'), C = X.cfg, now = Date.now();
const R = global.get('md_gmx', 'memory');
const out = [null, null, null];

function host() { return C.hosts[X.hostIdx]; }
function hostName(h) { return String(h).replace(/^https?:\/\//, ''); }
function toDec(raw, scale) {                       // BigInt → string decimal exacto (sin pasar por float)
    const neg = raw < 0n; let s = (neg ? -raw : raw).toString();
    if (scale > 0) { s = s.padStart(scale + 1, '0'); s = s.slice(0, s.length - scale) + '.' + s.slice(s.length - scale); s = s.replace(/\.?0+$/, ''); }
    return (neg ? '-' : '') + s;
}
function fail(reason) {
    const r = String(reason).slice(0, 200);
    X.errors++; X.errRun++; X.lastErr = r + ' @ ' + hostName(host()); X.lastErrAt = now;
    if (now - X.lastLogAt > 60000) { node.warn('ZEC_GMX: ' + X.lastErr); X.lastLogAt = now; }
    X.inflight = null;
    X.backoff = Math.min(C.backoffMax, X.backoff ? X.backoff * 2 : 1000);
    X.nextAt = now + X.backoff;
    if (X.errRun >= C.failSwitch && C.hosts.length > 1) {           // reconexión: siguiente host oficial
        X.hostIdx = (X.hostIdx + 1) % C.hosts.length; X.hostSince = now; X.switches++; X.errRun = 0;
        X.backoff = 0; X.nextAt = now + 1000;                       // el host nuevo se prueba enseguida (1 → 2 → 4 s por host)
        node.log('ZEC_GMX: cambio de host → ' + host());
    }
}
function median(a) { if (!a.length) return null; const s = a.slice().sort(function (x, y) { return x - y; }); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; }

// ---- error de red / timeout del nodo http request (catch) ----
if (msg.topic === 'gmx_http_error') {
    if (X.inflight && msg.gmxReq === X.inflight.id) fail((msg.error && msg.error.message) || 'error HTTP');
    return null;
}

// ---- respuesta del oracle keeper ----
if (msg.gmxReq !== undefined) {
    if (!X.inflight || msg.gmxReq !== X.inflight.id) return null;           // respuesta vieja (tras timeout)
    const tRecv = now, rtt = now - X.inflight.sentAt, src = X.inflight.host;
    X.inflight = null;
    if (msg.statusCode !== 200) { fail('HTTP ' + msg.statusCode); return null; }
    if (!Array.isArray(msg.payload)) { fail('respuesta no es un array JSON'); return null; }
    X.ok++; X.errRun = 0; X.backoff = 0; X.rttLast = rtt; X.rttSum += rtt; X.rttN++;
    const recs = [];
    for (const t of C.tokens) {
        const S = X.sym[t.symbol];
        let e = null;
        for (const it of msg.payload) if (it && String(it.tokenAddress).toLowerCase() === t.address) { e = it; break; }
        if (!e) { S.missing++; X.lastErr = t.symbol + ' no está en la respuesta'; X.lastErrAt = now; continue; }
        let mn, mx;
        try { mn = BigInt(e.minPrice); mx = BigInt(e.maxPrice); } catch (err) { S.invalid++; continue; }
        const srcTs = Number(e.updatedAt);
        if (mn <= 0n || mx < mn || !isFinite(srcTs) || srcTs <= 0) { S.invalid++; continue; }
        const L = S.last;
        if (L && srcTs === L.source_ts && e.minPrice === L.minRaw && e.maxPrice === L.maxRaw) { S.repeats++; continue; }  // misma publicación
        if (L && srcTs < L.source_ts) { S.older++; continue; }       // host atrasado (p. ej. tras cambiar a fallback): no retroceder
        if (L && srcTs === L.source_ts) S.sameTsChanged++;            // mismo updatedAt con precio distinto (se actualiza la fila)
        const scale = 30 - t.decimals;
        const minS = toDec(mn, scale), maxS = toDec(mx, scale), midS = toDec((mn + mx) * 5n, scale + 1);
        const r = {
            symbol: t.symbol, ts: tRecv, source_ts: srcTs,
            min: Number(minS), max: Number(maxS), mid: Number(midS), minS: minS, maxS: maxS, midS: midS,
            minRaw: e.minPrice, maxRaw: e.maxPrice,
            age_ms: tRecv - srcTs, rtt_ms: rtt, source: hostName(src) + C.path
        };
        if (L) { S.ivals.push(srcTs - L.source_ts); if (S.ivals.length > 300) S.ivals.shift(); }
        S.recv.push(tRecv); while (S.recv.length && S.recv[0] < now - 60000) S.recv.shift();
        S.last = r; S.updates++;
        R.last[t.symbol] = r;
        const g = R.ring[t.symbol]; g.buf[g.idx] = r; g.idx = (g.idx + 1) % g.size; if (g.count < g.size) g.count++;
        recs.push(r);
    }
    if (recs.length) out[1] = { topic: 'gmx_price', payload: recs };
    return out;
}

if (msg.topic !== 'tick') return null;
// ---- tick 250 ms: watchdog, volver al primario, consulta cada pollMs, indicador 1 Hz ----
if (X.inflight && now - X.inflight.sentAt > C.timeoutMs) fail('timeout ' + C.timeoutMs + ' ms');
if (X.hostIdx !== 0 && now - X.hostSince > C.primaryRetryMs) { X.hostIdx = 0; X.hostSince = now; X.switches++; node.log('ZEC_GMX: reintento host primario'); }
if (now >= X.nextAt) {
  if (X.inflight) { if (!X.inflight.late) { X.inflight.late = true; X.skippedBusy++; } }   // tocaba consultar y la anterior sigue en vuelo: no se apilan
  else {
    X.inflight = { id: ++X.seq, sentAt: now, host: host() };
    X.polls++; X.nextAt = now + C.pollMs;
    out[0] = { url: host() + C.path, method: 'GET', headers: { accept: 'application/json' }, requestTimeout: C.timeoutMs, gmxReq: X.inflight.id };
  }
}
if (now - X.lastInd < 1000) return out[0] ? out : null;
X.lastInd = now;

// indicador + comparación con el PRRR (último trade ZEC del stream multi-exchange; sólo lectura de md_ring)
const ring = global.get('md_ring', 'memory') || {};
const syms = [];
for (const t of C.tokens) {
    const S = X.sym[t.symbol], L = S.last;
    let prrr = null;
    const rr = ring[t.symbol];
    if (rr && rr.count) { const tr = rr.buf[(rr.idx - 1 + rr.size) % rr.size]; if (tr) prrr = { price: tr.price, t: tr.local_receive_timestamp, src: tr.source || tr.exchange }; }
    const row = { symbol: t.symbol, updates: S.updates, repeats: S.repeats, older: S.older, missing: S.missing, invalid: S.invalid, sameTsChanged: S.sameTsChanged,
        rate60: S.recv.length, ivalMed: median(S.ivals), prrr: prrr };
    if (L) {
        row.min = L.min; row.max = L.max; row.mid = L.mid; row.source_ts = L.source_ts; row.recv = L.ts;
        row.ageNow = now - L.source_ts; row.ageAtRecv = L.age_ms; row.spreadPct = (L.max - L.min) / L.mid * 100;
        if (prrr) { row.diff = L.mid - prrr.price; row.diffPct = (L.mid / prrr.price - 1) * 100; }
        row.stale = row.ageNow > C.staleMs;                          // comparación con dato GMX viejo: se marca, no se oculta
    }
    syms.push(row);
}
const z = syms[0] || {};
const fresh = z.ageNow != null && z.ageNow <= C.staleMs;
const state = X.errRun > 0 ? 'error' : fresh ? 'ok' : (X.errors && now - X.lastErrAt < 15000) ? 'error' : (X.ok ? 'sin actualizar' : 'conectando');
out[2] = { payload: { db: global.get('md_gmx_db', 'memory') || null, state: state, host: hostName(host()), polls: X.polls, ok: X.ok, errors: X.errors, switches: X.switches, skippedBusy: X.skippedBusy,
    lastErr: X.lastErr, lastErrAt: X.lastErrAt, rttLast: X.rttLast, rttAvg: X.rttN ? X.rttSum / X.rttN : null, backoff: X.backoff, syms: syms, now: now } };
node.status({ fill: state === 'ok' ? 'green' : state === 'error' ? 'red' : 'yellow', shape: 'dot',
    text: (z.mid != null ? 'ZEC ' + z.mid.toFixed(3) + ' · edad ' + (z.ageNow / 1000).toFixed(1) + ' s · ' : '') + X.ok + ' ok / ' + X.errors + ' err' });
return out;`;

const GXW_INIT = String.raw`// ===== GMX DB WRITER (gmx_price): estado =====
const W = {
    cfg: { table: 'gmx_price', batchMax: 200, flushMs: 5000, maxQueue: 7200, timeoutMs: 30000, backoffMin: 5000, backoffMax: 60000 },
    q: [], inflight: null, seq: 0, schema: null, lastFlush: Date.now(), backoff: 0, nextTry: 0,
    saved: 0, errors: 0, dropped: 0, lastSavedT: null, lastOkAt: 0, lastErr: '', lastLogAt: 0, link: 'desconocido'
};
context.set('W', W, 'memory');
node.status({ fill: 'grey', shape: 'ring', text: 'esperando esquema' });`;

const GXW_FUNC = String.raw`// Entradas: 'gmx_price' (registros nuevos del poller) · 'tick' 1 s · respuesta mysql (msg.dbKind) · 'db_error' (catch)
// Salida 1 → nodo mysql (1 query en vuelo) · Salida 2 → estado para el indicador (global md_gmx_db)
const W = context.get('W', 'memory'), C = W.cfg, now = Date.now();
const COLS = ['ts', 'source_ts', 'symbol', 'price', 'min_price', 'max_price', 'age_ms', 'rtt_ms', 'source'];
const out = [null];

function fail(reason) {
    const r = String(reason).slice(0, 200);
    if (r !== W.lastErr || now - W.lastLogAt > 60000) { node.warn('gmx_price: ' + r + ' · cola ' + W.q.length); W.lastLogAt = now; }
    W.errors++; W.lastErr = r; W.link = 'error';
    if (W.inflight && W.inflight.kind === 'insert') {
        W.q = W.inflight.rows.concat(W.q);
        if (W.q.length > C.maxQueue) { const ex = W.q.length - C.maxQueue; W.q.splice(0, ex); W.dropped += ex; }
    }
    W.inflight = null;
    W.backoff = Math.min(C.backoffMax, Math.max(C.backoffMin, W.backoff * 2));
    W.nextTry = now + W.backoff;
    node.status({ fill: 'red', shape: 'ring', text: 'error: ' + r.slice(0, 40) });
}
function pad(n, w) { return String(n).padStart(w || 2, '0'); }
function tsValue(t, ty) {
    if (ty === 'bigint' || ty === 'decimal') return t;                 // ms epoch
    const d = new Date(t);                                              // DATETIME(3)/TIMESTAMP(3) en UTC
    return d.getUTCFullYear() + '-' + pad(d.getUTCMonth() + 1) + '-' + pad(d.getUTCDate()) + ' ' + pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes()) + ':' + pad(d.getUTCSeconds()) + '.' + pad(d.getUTCMilliseconds(), 3);
}
function row(r) { return [tsValue(r.ts, W.schema.ts), tsValue(r.source_ts, W.schema.source_ts), r.symbol, r.midS, r.minS, r.maxS, r.age_ms, r.rtt_ms, r.source]; }
function publish() {
    global.set('md_gmx_db', { db: W.link === 'conectada' && now - W.lastOkAt < 20000 ? 'conectada' : W.link, saved: W.saved, errors: W.errors, queue: W.q.length,
        dropped: W.dropped, lastSaved: W.lastSavedT, lastErr: W.lastErr }, 'memory');
}

if (msg.topic === 'gmx_price' && Array.isArray(msg.payload)) {
    for (const r of msg.payload) W.q.push(r);
    if (W.q.length > C.maxQueue) { const ex = W.q.length - C.maxQueue; W.q.splice(0, ex); W.dropped += ex; }
    return null;
}
if (msg.topic === 'db_error' || msg.error) {                           // antes que las respuestas: el msg del catch conserva dbKind
    if (W.inflight && msg.dbBatch === W.inflight.id) fail((msg.error && msg.error.message) || 'error DB');
    publish(); return null;
}
if (msg.dbKind) {
    if (!W.inflight || msg.dbBatch !== W.inflight.id) return null;
    if (msg.dbKind === 'schema') {
        const have = {};
        (Array.isArray(msg.payload) ? msg.payload : []).forEach(function (r) { if (r && r.COLUMN_NAME) have[String(r.COLUMN_NAME).toLowerCase()] = String(r.DATA_TYPE).toLowerCase(); });
        W.inflight = null;
        if (!Object.keys(have).length) { fail('tabla ' + C.table + ' no existe (crearla con el SQL del README, etapa 6)'); publish(); return null; }
        const missing = COLS.filter(function (c) { return !have[c]; });
        if (missing.length) { fail('faltan columnas en ' + C.table + ': ' + missing.join(', ')); publish(); return null; }
        W.schema = { ts: have.ts, source_ts: have.source_ts };
        W.link = 'conectada'; W.lastOkAt = now; W.backoff = 0;
        node.status({ fill: 'green', shape: 'dot', text: 'ts=' + have.ts + ' · listo' });
        publish(); return null;
    }
    const n = W.inflight.rows.length;
    W.saved += n; W.lastSavedT = W.inflight.maxT; W.lastOkAt = now; W.link = 'conectada'; W.backoff = 0;
    W.inflight = null;
    W.lastFlush = W.q.length >= C.batchMax ? 0 : now;
    node.status({ fill: 'green', shape: 'dot', text: W.saved + ' filas · cola ' + W.q.length });
    publish(); return null;
}
if (msg.topic !== 'tick') return null;
if (W.inflight && now - W.inflight.sentAt > C.timeoutMs) fail('timeout: MariaDB no respondió en ' + (C.timeoutMs / 1000) + ' s');
if (!W.inflight && now >= W.nextTry) {
    if (!W.schema) {
        W.inflight = { id: ++W.seq, kind: 'schema', sentAt: now };
        out[0] = { topic: 'SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = ?',
            payload: [C.table], dbKind: 'schema', dbBatch: W.inflight.id };
    } else if (W.q.length && (W.q.length >= C.batchMax || now - W.lastFlush >= C.flushMs)) {
        const rows = W.q.splice(0, C.batchMax);
        let maxT = 0; for (const r of rows) if (r.source_ts > maxT) maxT = r.source_ts;
        W.inflight = { id: ++W.seq, kind: 'insert', rows: rows, sentAt: now, maxT: maxT };
        const pre = (W.schema.ts === 'timestamp' || W.schema.source_ts === 'timestamp') ? "SET time_zone = '+00:00'; " : '';
        // clave (symbol, source_ts): una fila por publicación de la fuente. Si se repite, se conserva la hora de recepción original.
        out[0] = { topic: pre + 'INSERT INTO ' + C.table + ' (' + COLS.join(', ') + ') VALUES ? ON DUPLICATE KEY UPDATE price = VALUES(price), min_price = VALUES(min_price), max_price = VALUES(max_price)',
            payload: [rows.map(row)], dbKind: 'insert', dbBatch: W.inflight.id };
    }
}
publish();
return out;`;

const GX_TPL = String.raw`<style>
.prrr-gx{font-family:monospace;font-size:13px;line-height:1.5}
.prrr-gx .ok{color:#27ae60;font-weight:bold}.prrr-gx .bad{color:#e74c3c;font-weight:bold}.prrr-gx .mid{color:#e6a700;font-weight:bold}
.prrr-gx table{border-collapse:collapse}.prrr-gx td{padding:0 10px 0 0;white-space:nowrap}.prrr-gx .k{opacity:.7}
.prrr-gx .sep{border-top:1px solid rgba(128,128,128,.35);margin:3px 0}
</style>
<div class="prrr-gx" ng-if="msg.payload">
 <div>ZEC_GMX · oracle keeper <b>{{msg.payload.host}}</b> ·
  <span ng-class="msg.payload.state==='ok' ? 'ok' : (msg.payload.state==='error' ? 'bad' : 'mid')">{{msg.payload.state}}</span></div>
 <table ng-repeat="z in msg.payload.syms">
  <tr><td class="k">min (bid)</td><td><b>{{z.min != null ? (z.min | number:4) : '—'}}</b></td><td class="k">max (ask)</td><td><b>{{z.max != null ? (z.max | number:4) : '—'}}</b></td></tr>
  <tr><td class="k">mid</td><td><b>{{z.mid != null ? (z.mid | number:4) : '—'}}</b></td><td class="k">spread</td><td>{{z.spreadPct != null ? (z.spreadPct | number:4) + ' %' : '—'}}</td></tr>
  <tr><td class="k">updatedAt fuente</td><td>{{z.source_ts ? (z.source_ts | date:'HH:mm:ss.sss') : '—'}}</td><td class="k">recibido</td><td>{{z.recv ? (z.recv | date:'HH:mm:ss.sss') : '—'}}</td></tr>
  <tr><td class="k">edad dato</td><td ng-class="z.stale ? 'bad' : (z.ageNow > 3000 ? 'mid' : '')">{{z.ageNow != null ? (z.ageNow/1000 | number:1) + ' s' : '—'}}</td>
      <td class="k">edad al recibir</td><td>{{z.ageAtRecv != null ? z.ageAtRecv + ' ms' : '—'}}</td></tr>
  <tr><td class="k">actualizaciones</td><td>{{z.updates}} <span class="k">({{z.rate60}} en 60 s)</span></td><td class="k">intervalo mediano</td><td>{{z.ivalMed != null ? (z.ivalMed | number:0) + ' ms' : '—'}}</td></tr>
  <tr><td colspan="4"><div class="sep"></div></td></tr>
  <tr><td class="k">PRRR {{z.symbol}} últ. trade</td><td><b>{{z.prrr ? (z.prrr.price | number:4) : '—'}}</b></td><td colspan="2" class="k">{{z.prrr ? z.prrr.src + ' · hace ' + ((msg.payload.now - z.prrr.t)/1000 | number:1) + ' s' : ''}}</td></tr>
  <tr><td class="k">GMX mid − PRRR</td><td><b>{{z.diff != null ? (z.diff >= 0 ? '+' : '') + (z.diff | number:4) + ' USD' : '—'}}</b></td>
      <td class="k">diferencia %</td><td><b>{{z.diffPct != null ? (z.diffPct >= 0 ? '+' : '') + (z.diffPct | number:4) + ' %' : '—'}}</b> <span class="bad" ng-if="z.stale">dato GMX viejo</span></td></tr>
 </table>
 <div class="sep"></div>
 <div>consultas <b>{{msg.payload.ok}}</b> ok / <b ng-class="{'bad': msg.payload.errors > 0}">{{msg.payload.errors}}</b> err ·<br>
  RTT {{msg.payload.rttLast != null ? msg.payload.rttLast + ' ms' : '—'}} (prom {{msg.payload.rttAvg != null ? (msg.payload.rttAvg | number:0) : '—'}}) ·
  consultas demoradas {{msg.payload.skippedBusy}} · cambios de host {{msg.payload.switches}}<span ng-if="msg.payload.backoff"> · backoff {{msg.payload.backoff/1000}} s</span></div>
 <div ng-if="msg.payload.db">DB gmx_price: <span ng-class="msg.payload.db.db==='conectada' ? 'ok' : 'bad'">{{msg.payload.db.db}}</span> ·
  filas <b>{{msg.payload.db.saved}}</b> · cola {{msg.payload.db.queue}} · errores <b ng-class="{'bad': msg.payload.db.errors > 0}">{{msg.payload.db.errors}}</b><br>
  último guardado {{msg.payload.db.lastSaved ? (msg.payload.db.lastSaved | date:'HH:mm:ss') : '—'}}</div>
 <div ng-if="msg.payload.lastErr" class="k" title="{{msg.payload.lastErr}}">último error: {{msg.payload.lastErr | limitTo:90}} {{msg.payload.lastErrAt ? '(' + (msg.payload.lastErrAt | date:'HH:mm:ss') + ')' : ''}}</div>
</div>
<div class="prrr-gx" ng-if="!msg.payload">ZEC_GMX: esperando…</div>`;

// ---- nodos ----
const G_GX = { id: 'prrr_ui_g_gmx', name: 'ZEC_GMX · precio de ejecución GMX (oráculo)', order: 11, width: 12 };
add({ id: G_GX.id, type: 'ui_group', name: G_GX.name, tab: UI_TAB, order: G_GX.order, disp: true, width: String(G_GX.width), collapse: false, className: '' });
const gGx = egroup('prrr_grp_gmx', 'ETAPA 6 — ZEC_GMX (oracle keeper GMX → gmx_price · independiente del PRRR)', '#2ca02c');
const GXY = LBY + 160;
inGroup(gGx, { id: 'prrr_gmx_tick', type: 'inject', z: TAB, name: 'tick 250 ms', props: [{ p: 'topic', vt: 'str' }],
  repeat: '0.25', crontab: '', once: true, onceDelay: '2', topic: 'tick', x: 170, y: GXY, wires: [[GXP, GXW]] });
inGroup(gGx, { id: GXP, type: 'function', z: TAB, name: 'ZEC_GMX poller (oracle keeper /prices/tickers · sólo cambios reales)',
  func: GX_FUNC,
  outputs: 3, timeout: 0, noerr: 0, initialize: GX_INIT, finalize: '', libs: [],
  outputLabels: ['consulta → http request', 'registros nuevos → gmx_price', 'indicador → DIAGNÓSTICO'], x: 490, y: GXY + 20, wires: [[GXH], [GXW], [GXUI]] });
inGroup(gGx, { id: GXH, type: 'http request', z: TAB, name: 'GET oracle keeper', method: 'use', ret: 'obj', paytoqs: 'ignore', url: '', tls: '', persist: true,
  proxy: '', insecureHTTPParser: false, authType: '', senderr: true, headers: [], x: 850, y: GXY - 20, wires: [[GXP]] });
inGroup(gGx, { id: 'prrr_gmx_http_catch', type: 'catch', z: TAB, name: 'errores HTTP', scope: [GXH], uncaught: false, x: 840, y: GXY + 25, wires: [['prrr_gmx_http_tag']] });
inGroup(gGx, { id: 'prrr_gmx_http_tag', type: 'change', z: TAB, name: 'topic = gmx_http_error',
  rules: [{ t: 'set', p: 'topic', pt: 'msg', to: 'gmx_http_error', tot: 'str' }], action: '', property: '', from: '', to: '', reg: false, x: 1050, y: GXY + 25, wires: [[GXP]] });
inGroup(gGx, { id: GXW, type: 'function', z: TAB, name: 'GMX DB writer (gmx_price · lotes · reintento)',
  func: GXW_FUNC, outputs: 1, timeout: 0, noerr: 0, initialize: GXW_INIT, finalize: '', libs: [],
  outputLabels: ['query → mysql'], x: 840, y: GXY + 90, wires: [[GXM]] });
inGroup(gGx, { id: GXM, type: 'mysql', z: TAB, mydb: DBCFG, name: 'MariaDB gmx_price', x: 1090, y: GXY + 90, wires: [[GXW]] });
inGroup(gGx, { id: 'prrr_gmx_db_catch', type: 'catch', z: TAB, name: 'errores mysql gmx', scope: [GXM], uncaught: false, x: 1090, y: GXY + 140, wires: [['prrr_gmx_db_tag']] });
inGroup(gGx, { id: 'prrr_gmx_db_tag', type: 'change', z: TAB, name: 'topic = db_error',
  rules: [{ t: 'set', p: 'topic', pt: 'msg', to: 'db_error', tot: 'str' }], action: '', property: '', from: '', to: '', reg: false, x: 1290, y: GXY + 140, wires: [[GXW]] });
inGroup(gGx, Object.assign({}, tplBase, { id: GXUI, name: 'indicador ZEC_GMX', group: G_GX.id, order: 1, width: 12, height: 7, format: GX_TPL, x: 840, y: GXY + 190 }));
inGroup(gGx, { id: 'prrr_gmx_comment', type: 'comment', z: TAB, name: 'ZEC_GMX: fuente, campos y tabla',
  info: '**Fuente**: oracle keeper oficial de GMX (`/prices/tickers`, Arbitrum) con los fallbacks oficiales. Es la misma API que usa la interfaz de GMX (polling 1 s) y publica el min/max (bid/ask) de los reportes de oráculo firmados (Chainlink Data Streams) que los keepers envían on-chain para ejecutar órdenes; `Oracle.sol` los valida y la ejecución usa min o max según el lado.\n\n**Campos**: `minPrice`/`maxPrice` (30 decimales; USD = raw / 10^(30−8) para ZEC) y `updatedAt` (ms). Conversión exacta con BigInt (sin float).\n\n**Registro**: sólo cuando cambia `updatedAt`/min/max (una fila por publicación de la fuente). No hay ticks artificiales, interpolación ni relleno.\n\n* `ts`: recepción local\n* `source_ts`: `updatedAt`\n* `price`: (min+max)/2 (derivado)\n* `age_ms`: ts − source_ts (incluye desfase de relojes)\n* `rtt_ms`: ida y vuelta HTTP\n* `source`: host + ruta\n\nComparación con el PRRR: último trade ZEC de `md_ring`, sólo lectura. RAM: `global.get("md_gmx","memory")` → last[SYM], ring[SYM] (3600 registros).',
  x: 490, y: GXY + 190, wires: [] });

// ===========================================================================
// ETAPA 6b — serie "GMX" en el gráfico ZEC de MERCADO (sólo visual, 100 % aditivo)
// Lee el feed ZEC_GMX existente (global md_gmx, escrito por el poller de la etapa 6) sin tocarlo.
// No hay poller nuevo y no se modifica ningún nodo: ni el template MERCADO, ni PRRR, SMA, señales, market_1s o gmx_price.
// tick 500 ms → GMX → MERCADO (publicaciones nuevas) → [ui_template invisible en el grupo MERCADO]
//   → en el navegador agrega la serie "GMX" al almacén de series del gráfico ZEC (window.__prrrPlot),
//     que el template MERCADO ya dibuja con sus reglas (ventana, autoescala, stale, cortes, leyenda).
// ===========================================================================
const GXMK = 'prrr_gmx_mkt', GXMKUI = 'prrr_gmx_mkt_ui';
const GXMK_INIT = String.raw`// ===== GMX → MERCADO: estado =====
// t/v: [hora de recepción local, mid] de cada publicación real de ZEC_GMX, últimas 4 h (= ventana máxima del gráfico)
const M = { lastT: 0, t: [], v: [], keepMs: 4 * 3600000 + 60000 };
context.set('M', M, 'memory');`;

const GXMK_FUNC = String.raw`// Entradas: 'tick' (500 ms) · 'gmx_snapshot_req' {since} (desde el template GMX del navegador)
// Salida → template GMX: { gmx: [t, mid, …] } (sólo publicaciones nuevas, a todos) o el historial al navegador que lo pidió.
const M = context.get('M', 'memory'), now = Date.now();

function collect() {                                   // publicaciones del ring de md_gmx posteriores a la última enviada
    const R = global.get('md_gmx', 'memory'), g = R && R.ring && R.ring.ZEC;
    if (!g || !g.count) return [];
    const f = [];
    for (let i = 1; i <= g.count; i++) {
        const r = g.buf[(g.idx - i + g.size) % g.size];
        if (!r || r.ts <= M.lastT) break;
        f.push(r);
    }
    return f.reverse();
}

if (msg.topic === 'tick') {
    let cut = 0; while (cut < M.t.length && M.t[cut] < now - M.keepMs) cut++;
    if (cut) { M.t.splice(0, cut); M.v.splice(0, cut); }
    const f = collect();
    if (!f.length) return null;
    const flat = [];
    for (const r of f) { M.lastT = r.ts; if (!(r.mid > 0)) continue; M.t.push(r.ts); M.v.push(r.mid); flat.push(r.ts, r.mid); }
    node.status({ fill: 'green', shape: 'dot', text: M.t.length + ' pts en 4 h · último ' + new Date(M.lastT).toISOString().slice(11, 19) + ' UTC' });
    return flat.length ? { payload: { gmx: flat } } : null;
}
if (msg.topic === 'gmx_snapshot_req') {                // navegador: lo que le falte desde 'since' (0 = todo)
    const since = Number(msg.since) || 0, flat = [];
    for (let i = 0; i < M.t.length; i++) if (M.t[i] > since) flat.push(M.t[i], M.v[i]);
    return { socketid: msg.socketid, payload: { gmxSnapshot: flat } };
}
return null;`;

const GXMK_TPL = String.raw`<div id="prrr_gmx_series" style="display:none"></div>
<script>
(function (scope) {
  // Etapa 6b: agrega la serie "GMX" (ZEC_GMX mid = (min+max)/2, publicaciones reales) al gráfico ZEC de MERCADO.
  // No modifica el template MERCADO: escribe en su almacén de series del navegador (window.__prrrPlot) con la
  // misma estructura que usa para cada exchange, así se dibuja con sus mismas reglas. Sin interpolación ni relleno:
  // 1 punto por publicación real, en su hora de recepción (mismo reloj que las series PRRR).
  var SYM = 'ZEC', NAME = 'GMX', TIERS = { A: { slotMs: 250, cap: 3600 }, B: { slotMs: 5000, cap: 2880 } };   // = CFG.tiers del gráfico
  var Q = window.__prrrGMXq || (window.__prrrGMXq = { pend: [], lastT: 0 });
  function newTier(d) { return { slotMs: d.slotMs, cap: d.cap, idx: new Float64Array(d.cap).fill(-1), lo: new Float64Array(d.cap), hi: new Float64Array(d.cap), last: new Float64Array(d.cap) }; }
  function tadd(T, t, y, isLast) {
    var s = Math.floor(t / T.slotMs), k = s % T.cap;
    if (T.idx[k] > s) return;                            // ya hay algo más nuevo en ese casillero (historial viejo): no pisar
    if (T.idx[k] !== s) { T.idx[k] = s; T.lo[k] = y; T.hi[k] = y; T.last[k] = y; }
    else { if (y < T.lo[k]) T.lo[k] = y; if (y > T.hi[k]) T.hi[k] = y; if (isLast) T.last[k] = y; }
  }
  function ser() {                                       // null hasta que el template MERCADO creó su almacén
    var G = window.__prrrPlot; if (!G || !G.series || !G.series[SYM] || !G.order || !G.order[SYM]) return null;
    var S = G.series[SYM][NAME];
    if (!S) { S = G.series[SYM][NAME] = { A: newTier(TIERS.A), B: newTier(TIERS.B), lastT: 0, first: Infinity }; G.order[SYM].push(NAME); }
    return S;
  }
  function add(S, t, y) {
    var isLast = t >= S.lastT;
    tadd(S.A, t, y, isLast); tadd(S.B, t, y, isLast);
    if (isLast) S.lastT = t; if (t < S.first) S.first = t;
    if (t > Q.lastT) Q.lastT = t;
  }
  function put(a, all) {                                 // a = [t, v, t, v, …]; all = historial (acepta puntos viejos)
    var S = ser();
    if (!S) { for (var i = 0; i < a.length; i += 2) Q.pend.push(a[i], a[i + 1]); return; }
    if (Q.pend.length) { var p = Q.pend; Q.pend = []; for (var j = 0; j < p.length; j += 2) add(S, p[j], p[j + 1]); }
    for (var k = 0; k < a.length; k += 2) { if (!(a[k + 1] > 0)) continue; if (!all && a[k] <= Q.lastT) continue; add(S, a[k], a[k + 1]); }
  }
  scope.$watch('msg', function (m) {
    if (!m || !m.payload) return;
    if (m.payload.gmxSnapshot) put(m.payload.gmxSnapshot, true);
    else if (m.payload.gmx) put(m.payload.gmx, false);
  });
  var tm = setInterval(function () { if (Q.pend.length && ser()) put([], false); }, 300);
  scope.$on('$destroy', function () { clearInterval(tm); });
  // al (re)aparecer el widget: pedir sólo lo que falta (página nueva → todo el historial de 4 h)
  setTimeout(function () { scope.send({ topic: 'gmx_snapshot_req', since: Q.lastT }); }, 0);
})(scope);
</script>`;

const gGxMk = egroup('prrr_grp_gmx_mkt', 'ETAPA 6b — serie GMX en el gráfico ZEC de MERCADO (lee ZEC_GMX existente · sólo visual)', '#d62728');
const GMY = GXY + 270;
inGroup(gGxMk, { id: 'prrr_gmx_mkt_tick', type: 'inject', z: TAB, name: 'tick 500 ms', props: [{ p: 'topic', vt: 'str' }],
  repeat: '0.5', crontab: '', once: true, onceDelay: '3', topic: 'tick', x: 180, y: GMY, wires: [[GXMK]] });
inGroup(gGxMk, { id: GXMK, type: 'function', z: TAB, name: 'GMX → MERCADO (publicaciones nuevas de md_gmx · historial 4 h)',
  func: GXMK_FUNC, outputs: 1, timeout: 0, noerr: 0, initialize: GXMK_INIT, finalize: '', libs: [],
  outputLabels: ['serie GMX → navegador'], x: 500, y: GMY, wires: [[GXMKUI]] });
inGroup(gGxMk, Object.assign({}, tplBase, { id: GXMKUI, name: 'serie GMX en gráfico ZEC (invisible)', group: 'prrr_ui_g_btc', order: 2, width: 1, height: 1,
  format: GXMK_TPL, storeOutMessages: false, resendOnRefresh: false, x: 820, y: GMY, wires: [[GXMK]] }));

// ===========================================================================
// ETAPA 7 — CONCEPTITO LIVE (paper trader ZEC, sin dinero real)
// Señal y ejecución paper sobre los buckets PRRR ZEC de 1 s que YA existen (global md_mem_1s, sólo lectura):
// no hay feeds nuevos ni se toca el normalizador, la analítica 25/50, GMX, market_1s o gmx_price.
// tick 250 ms → CONCEPTITO motor (SMA39/76 · cruces · posición · TP/SL/TO) ─→ [mysql] conceptito_trades
//                                                                        └─→ [ui_template invisible] → panel ZEC
// ===========================================================================
const CXE = 'prrr_cx_engine', CXM = 'prrr_cx_mysql', CXUI = 'prrr_cx_ui';
const CX_INIT = String.raw`// ===== CONCEPTITO LIVE: configuración y estado =====
const C = {
    symbol: 'ZEC',
    fast: 39, slow: 76,          // SMA en segundos (buckets 1 s, close; no_trade cuenta con su precio arrastrado)
    margin: 1000, lev: 3,        // exposición = margin × lev = 3000 USD
    tp: 32.10, sl: -84.25,       // USD de PnL BRUTO
    timeoutMin: 18,
    maxPos: 1,
    feeRate: 0.0005,             // 0,05 % por lado
    feeBase: 'notional',         // 'notional' = sobre el nocional de cada lado (entrada 3000; salida qty × precio de salida) · 'exposure' = siempre sobre 3000
    table: 'conceptito_trades',
    histSec: 14400               // historial SMA39/76 para el gráfico (4 h = ventana máxima)
};
const E = {
    cfg: C, phase: 'RESTAURANDO',            // RESTAURANDO (esperando la DB) → OPERANDO
    lastT: 0, replayUntil: 0, lastBucketT: 0, gaps: 0,
    closes: [], sF: null, sS: null, lastSign: 0, ignored: 0, last: null, pend: null,
    pos: null, totals: { ops: 0, tp: 0, sl: 0, to: 0, net: 0, fees: 0 }, trades: [], tradesRev: 0, lastClosed: null,
    hist: { cap: C.histSec, idx: new Float64Array(C.histSec).fill(-1), a: new Float64Array(C.histSec), b: new Float64Array(C.histSec) },
    newPts: [], dirty: true, lastEmit: 0,
    // DB: cola FIFO, 1 query en vuelo
    q: [], inflight: null, seq: 0, backoff: 0, nextTry: 0, saved: 0, dbErrors: 0, dbErr: '', lastLogAt: 0, dbOk: false, openRows: 0
};
context.set('E', E, 'memory');
node.status({ fill: 'grey', shape: 'ring', text: 'restaurando estado desde MariaDB…' });`;

const CX_FUNC = String.raw`// Entradas: 'tick' (250 ms) · respuestas del nodo mysql (msg.cxKind) · 'db_error' (catch) · 'cx_snapshot_req' (navegador)
// Salida 1 → mysql (1 query en vuelo)  ·  Salida 2 → navegador (estado 1/s, eventos, historial)
const E = context.get('E', 'memory'), C = E.cfg, now = Date.now();
const out = [null, null];

function pad(n, w) { return String(n).padStart(w || 2, '0'); }
function dt(t) { const d = new Date(t); return d.getUTCFullYear() + '-' + pad(d.getUTCMonth() + 1) + '-' + pad(d.getUTCDate()) + ' ' + pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes()) + ':' + pad(d.getUTCSeconds()) + '.' + pad(d.getUTCMilliseconds(), 3); }
function r(v, d) { return v == null ? null : Number(v.toFixed(d)); }
function num(v) { return v == null ? null : Number(v); }
function enqueue(item) { E.q.push(item); if (E.q.length > 1000) { E.q.shift(); E.dbErrors++; E.dbErr = 'cola DB llena: se descartó la escritura más vieja'; } }
function fail(reason) {
    const m = String(reason).slice(0, 200);
    if (m !== E.dbErr || now - E.lastLogAt > 60000) { node.warn('Conceptito DB: ' + m); E.lastLogAt = now; }
    E.dbErrors++; E.dbErr = m; E.dbOk = false;
    if (E.inflight && E.inflight.item) E.q.unshift(E.inflight.item);       // se reintenta en orden
    E.inflight = null;
    E.backoff = Math.min(60000, Math.max(3000, E.backoff * 2)); E.nextTry = now + E.backoff;
}

// ---------- persistencia: una fila por operación (OPEN al abrir, CLOSED al cerrar; upsert por symbol+entry_ts) ----------
const COLS = ['symbol', 'source', 'status', 'signal_ts', 'entry_ts', 'exit_ts', 'side', 'entry_price', 'exit_price', 'exit_trigger_price', 'qty', 'margin_usd', 'leverage', 'exposure_usd',
    'sma_fast', 'sma_slow', 'sma_fast_entry', 'sma_slow_entry', 'tp_usd', 'sl_usd', 'timeout_min', 'fee_rate',
    'gross_pnl', 'fee_entry', 'fee_exit', 'fees', 'net_pnl', 'exit_reason', 'duration_seconds', 'mfe_usd', 'mae_usd'];
const UPD = ['status', 'exit_ts', 'exit_price', 'exit_trigger_price', 'gross_pnl', 'fee_exit', 'fees', 'net_pnl', 'exit_reason', 'duration_seconds', 'mfe_usd', 'mae_usd'];
function saveRow(p, x) {
    const row = [C.symbol, 'PRRR', x ? 'CLOSED' : 'OPEN', dt(p.signalT), dt(p.entryT), x ? dt(x.t) : null, p.side, p.entryPx, x ? r(x.px, 8) : null, x ? x.trigger : null, r(p.qty, 10),
        p.margin, p.lev, p.exposure, p.fast, p.slow, r(p.sFast, 8), r(p.sSlow, 8), p.tp, p.sl, p.toMin, p.feeRate,
        x ? r(x.gross, 4) : null, r(p.feeEntry, 4), x ? r(x.feeExit, 4) : null, x ? r(x.fees, 4) : null, x ? r(x.net, 4) : null,
        x ? x.reason : null, x ? x.dur : null, x ? r(p.mfe, 4) : null, x ? r(p.mae, 4) : null];
    const upsert = { kind: 'save', row: row, sql: 'INSERT INTO ' + C.table + ' (' + COLS.join(', ') + ') VALUES ? ON DUPLICATE KEY UPDATE ' + UPD.map(function (c) { return c + ' = VALUES(' + c + ')'; }).join(', '),
        params: [[row]] };
    if (!x) { enqueue(upsert); return; }
    // cierre: UPDATE de la fila OPEN (ids correlativos); si no existe (se perdió el INSERT), upsert completo
    const vals = UPD.map(function (c) { return row[COLS.indexOf(c)]; });
    enqueue({ kind: 'close', row: row, fallback: upsert, sql: 'UPDATE ' + C.table + ' SET ' + UPD.map(function (c) { return c + ' = ?'; }).join(', ') + ' WHERE symbol = ? AND entry_ts = ?',
        params: vals.concat([C.symbol, dt(p.entryT)]) });
}

// ---------- SMA / historial ----------
function avgLast(n) { const a = E.closes; if (a.length < n) return null; let s = 0; for (let i = a.length - n; i < a.length; i++) s += a[i]; return s / n; }
function histPut(t, a, b) { const H = E.hist, sec = Math.floor(t / 1000), k = sec % H.cap; H.idx[k] = sec; H.a[k] = a == null ? NaN : a; H.b[k] = b == null ? NaN : b; }
function levels(p) { const k = p.side === 'LONG' ? 1 : -1; return { tpPx: p.entryPx * (1 + k * p.tp / p.exposure), slPx: p.entryPx * (1 + k * p.sl / p.exposure) }; }

// ---------- posición paper ----------
function open(sig, b) {                                // sig = cruce confirmado en el bucket anterior (N); b = bucket N+1
    const exposure = C.margin * C.lev, px = b.close;
    E.pos = { side: sig.side, signalT: sig.t, entryT: b.t, entryPx: px, qty: exposure / px, margin: C.margin, lev: C.lev, exposure: exposure,
        fast: C.fast, slow: C.slow, sFast: sig.sF, sSlow: sig.sS, tp: C.tp, sl: C.sl, toMin: C.timeoutMin, feeRate: C.feeRate, feeBase: C.feeBase,
        feeEntry: exposure * C.feeRate, pnl: 0, mfe: 0, mae: 0, lastPx: px, lastT: b.t };
    saveRow(E.pos, null);
    E.trades.push({ entryT: b.t, entryPx: px, side: sig.side }); E.tradesRev++;
    node.log('Conceptito: abre ' + sig.side + ' @ ' + px + ' (cruce en ' + new Date(sig.t).toISOString().slice(11, 19) + ' · SMA' + C.fast + ' ' + sig.sF.toFixed(6) + ' / SMA' + C.slow + ' ' + sig.sS.toFixed(6) + ')');
}
function manage(b) {                                    // devuelve true si cerró en este bucket
    const p = E.pos, k = p.side === 'LONG' ? 1 : -1, px = b.close;
    const gross = k * p.qty * (px - p.entryPx);         // = ±exposición × (precio/entrada − 1)
    p.pnl = gross; p.lastPx = px; p.lastT = b.t;
    if (gross > p.mfe) p.mfe = gross; if (gross < p.mae) p.mae = gross;
    // el close detecta el umbral; en TP/SL el PnL realizado se fija EXACTAMENTE en el valor configurado (como el backtest)
    let reason = null, realized = gross;
    if (gross >= p.tp) { reason = 'TP'; realized = p.tp; }
    else if (gross <= p.sl) { reason = 'SL'; realized = p.sl; }
    else if (b.t - p.entryT >= p.toMin * 60000) reason = 'TIMEOUT';        // TIMEOUT: PnL real al close de ese bucket
    if (!reason) return false;
    const exitPx = reason === 'TIMEOUT' ? px : p.entryPx + k * realized / p.qty;   // precio equivalente al PnL realizado
    const feeExit = (p.feeBase === 'exposure' ? p.exposure : p.qty * exitPx) * p.feeRate, fees = p.feeEntry + feeExit, net = realized - fees;
    const x = { t: b.t, px: exitPx, trigger: px, gross: realized, feeExit: feeExit, fees: fees, net: net, reason: reason, dur: Math.round((b.t - p.entryT) / 1000) };
    saveRow(p, x);
    const T = E.totals; T.ops++; if (reason === 'TP') T.tp++; else if (reason === 'SL') T.sl++; else T.to++; T.net += net; T.fees += fees;
    for (let i = E.trades.length - 1; i >= 0; i--) if (E.trades[i].entryT === p.entryT) { Object.assign(E.trades[i], { exitT: b.t, exitPx: exitPx, reason: reason, net: net }); break; }
    E.lastClosed = { side: p.side, reason: reason, net: net, gross: realized, t: b.t, dur: x.dur };
    E.tradesRev++; E.pos = null;
    node.log('Conceptito: cierra ' + p.side + ' por ' + reason + ' (close ' + px + ') · bruto ' + realized.toFixed(2) + ' · neto ' + net.toFixed(2));
    return true;
}
function onBucket(b, live) {
    if (b.close == null || b.status === 'no_price') return;
    if (E.lastBucketT && b.t - E.lastBucketT > 1000) { E.closes = []; E.lastSign = 0; E.gaps++; }   // hueco: la SMA vuelve a calentar
    E.lastBucketT = b.t;
    E.closes.push(b.close); if (E.closes.length > C.slow) E.closes.shift();
    E.sF = avgLast(C.fast); E.sS = avgLast(C.slow);
    E.last = { t: b.t, px: b.close };
    histPut(b.t, E.sF, E.sS); E.newPts.push(b.t, r(E.sF, 6), r(E.sS, 6));
    let closedNow = false, enteredNow = false;
    if (E.pend) { open(E.pend, b); E.pend = null; enteredNow = true; }       // entrada = close del bucket siguiente al cruce (N+1)
    else if (E.pos && b.t > E.pos.entryT) closedNow = manage(b);
    if (E.sF == null || E.sS == null) return;
    const d = E.sF - E.sS, sg = d > 0 ? 1 : d < 0 ? -1 : 0;
    if (sg === 0) return;                                // empate exacto: no cambia el lado
    const cross = (E.lastSign !== 0 && sg !== E.lastSign) ? sg : 0;
    E.lastSign = sg;
    if (!cross) return;
    if (live && !E.pos && !E.pend && !closedNow && !enteredNow) E.pend = { side: cross > 0 ? 'LONG' : 'SHORT', t: b.t, sF: E.sF, sS: E.sS };
    else E.ignored++;                                    // posición abierta, entró/cerró en este mismo segundo, o historial previo al arranque
}

// ---------- estado para el navegador ----------
function state() {
    const p = E.pos, o = { phase: E.phase, t: E.last && E.last.t, px: E.last && E.last.px, sF: r(E.sF, 6), sS: r(E.sS, 6), warm: Math.min(E.closes.length, C.slow),
        totals: { ops: E.totals.ops, tp: E.totals.tp, sl: E.totals.sl, to: E.totals.to, net: r(E.totals.net, 2), fees: r(E.totals.fees, 2) },
        pend: E.pend ? { side: E.pend.side, t: E.pend.t } : null, lastClosed: E.lastClosed, db: { ok: E.dbOk, err: E.dbErr, errors: E.dbErrors, queue: E.q.length + (E.inflight ? 1 : 0), saved: E.saved } };
    if (p) { const L = levels(p); o.pos = { side: p.side, entryT: p.entryT, entryPx: p.entryPx, lastPx: p.lastPx, pnl: r(p.pnl, 2), toMin: p.toMin, tp: p.tp, sl: p.sl, tpPx: L.tpPx, slPx: L.slPx }; }
    return o;
}
function cfgOut() { return { fast: C.fast, slow: C.slow, margin: C.margin, lev: C.lev, exposure: C.margin * C.lev, tp: C.tp, sl: C.sl, timeoutMin: C.timeoutMin, feeRate: C.feeRate }; }
function recentTrades() { const lim = now - C.histSec * 1000; return E.trades.filter(function (t) { return (t.exitT || now) >= lim; }); }
function statusText() {
    const p = E.pos;
    if (E.phase !== 'OPERANDO') return { fill: 'yellow', shape: 'ring', text: 'restaurando estado (MariaDB)' + (E.dbErr ? ': ' + E.dbErr.slice(0, 40) : '') };
    if (p) return { fill: p.side === 'LONG' ? 'green' : 'red', shape: 'dot', text: p.side + ' @ ' + p.entryPx + ' · PnL ' + p.pnl.toFixed(2) + ' · ops ' + E.totals.ops + ' · neto ' + E.totals.net.toFixed(2) };
    return { fill: E.sS == null ? 'yellow' : 'blue', shape: 'dot', text: (E.sS == null ? 'calentando SMA ' + E.closes.length + '/' + C.slow : 'esperando cruce') + ' · ops ' + E.totals.ops + ' · neto ' + E.totals.net.toFixed(2) };
}

// ---------- error / respuestas mysql ----------
if (msg.topic === 'db_error' || msg.error) {
    if (E.inflight && msg.cxBatch === E.inflight.id) fail((msg.error && msg.error.message) || 'error DB');
    return null;
}
if (msg.cxKind) {
    if (!E.inflight || msg.cxBatch !== E.inflight.id) return null;
    const kind = E.inflight.kind; E.lastItem = E.inflight.item; E.inflight = null; E.backoff = 0; E.dbOk = true; E.dbErr = '';
    if (kind === 'restore') {
        const sets = (Array.isArray(msg.payload) ? msg.payload : []).filter(Array.isArray);
        const op = (sets[0] || [])[0], tot = (sets[1] || [])[0] || {}, recent = sets[2] || [];
        E.openRows = Number(tot.open_rows || 0);
        if (E.openRows > 1) node.warn('Conceptito: hay ' + E.openRows + ' filas OPEN en ' + C.table + '; se retoma la más reciente');
        if (op) {
            E.pos = { side: op.side, signalT: op.signal_ms == null ? Number(op.entry_ms) - 1000 : Number(op.signal_ms), entryT: Number(op.entry_ms), entryPx: num(op.entry_price), qty: num(op.qty), margin: num(op.margin_usd), lev: num(op.leverage),
                exposure: num(op.exposure_usd), fast: Number(op.sma_fast), slow: Number(op.sma_slow), sFast: num(op.sma_fast_entry), sSlow: num(op.sma_slow_entry),
                tp: num(op.tp_usd), sl: num(op.sl_usd), toMin: num(op.timeout_min), feeRate: num(op.fee_rate), feeBase: C.feeBase, feeEntry: num(op.fee_entry),
                pnl: 0, mfe: 0, mae: 0, lastPx: num(op.entry_price), lastT: Number(op.entry_ms) };
            node.log('Conceptito: posición ' + E.pos.side + ' abierta restaurada (entrada ' + new Date(E.pos.entryT).toISOString() + ' @ ' + E.pos.entryPx + ')');
        }
        E.totals = { ops: Number(tot.ops || 0), tp: Number(tot.tp || 0), sl: Number(tot.sl || 0), to: Number(tot.tmo || 0), net: num(tot.net) || 0, fees: num(tot.fees) || 0 };
        E.trades = recent.map(function (x) { return x.exit_ms == null ? { entryT: Number(x.entry_ms), entryPx: num(x.entry_price), side: x.side }
            : { entryT: Number(x.entry_ms), entryPx: num(x.entry_price), side: x.side, exitT: Number(x.exit_ms), exitPx: num(x.exit_price), reason: x.exit_reason, net: num(x.net_pnl) }; });
        E.tradesRev++;
        // a partir de acá: se re-procesan los buckets ya en memoria (calientan la SMA y, si hay posición restaurada, evalúan su salida);
        // sólo los buckets posteriores pueden abrir operaciones nuevas.
        const M = global.get('md_mem_1s', 'memory'), g = M && M.series && M.series[C.symbol];
        E.replayUntil = (g && g.count) ? g.buf[(g.idx - 1 + g.size) % g.size].t : 0;
        E.lastT = 0; E.phase = 'OPERANDO'; E.dirty = true;
        node.status(statusText());
        return null;
    }
    if (kind === 'close' && msg.payload && msg.payload.affectedRows === 0 && E.lastItem && E.lastItem.fallback) E.q.unshift(E.lastItem.fallback);
    else E.saved++;                                      // escritura OK
    return null;
}

// ---------- historial para un navegador ----------
if (msg.topic === 'cx_snapshot_req') {
    const H = E.hist, since = Math.floor((Number(msg.since) || 0) / 1000), nowSec = Math.floor(now / 1000), arr = [];
    for (let s = Math.max(since + 1, nowSec - H.cap + 1); s <= nowSec; s++) { const k = s % H.cap; if (H.idx[k] === s) arr.push(s * 1000, r(H.a[k], 6), r(H.b[k], 6)); }
    out[1] = { socketid: msg.socketid, payload: { cxSnap: { cfg: cfgOut(), sma: arr, trades: recentTrades(), state: state() } } };
    return out;
}

if (msg.topic !== 'tick') return null;
// ---------- tick: DB, restauración, buckets nuevos, emisión ----------
if (E.inflight && now - E.inflight.sentAt > 30000) fail('timeout: MariaDB no respondió en 30 s');
if (E.phase === 'RESTAURANDO' && !E.inflight && !E.q.some(function (i) { return i.kind === 'restore'; }) && now >= E.nextTry) {
    const T = C.table, ms = function (c) { return 'TIMESTAMPDIFF(MICROSECOND, \'1970-01-01 00:00:00\', ' + c + ') DIV 1000'; };
    E.q.unshift({ kind: 'restore', sql:
        'SELECT ' + ms('entry_ts') + ' AS entry_ms, CASE WHEN signal_ts IS NULL THEN NULL ELSE ' + ms('signal_ts') + ' END AS signal_ms, side, entry_price, qty, margin_usd, leverage, exposure_usd, sma_fast, sma_slow, sma_fast_entry, sma_slow_entry, tp_usd, sl_usd, timeout_min, fee_rate, fee_entry FROM ' + T + ' WHERE symbol = ? AND status = \'OPEN\' ORDER BY entry_ts DESC LIMIT 1; ' +
        'SELECT SUM(status = \'CLOSED\') ops, SUM(exit_reason = \'TP\') tp, SUM(exit_reason = \'SL\') sl, SUM(exit_reason = \'TIMEOUT\') tmo, SUM(CASE WHEN status = \'CLOSED\' THEN net_pnl END) net, SUM(CASE WHEN status = \'CLOSED\' THEN fees END) fees, SUM(status = \'OPEN\') open_rows FROM ' + T + ' WHERE symbol = ?; ' +
        'SELECT ' + ms('entry_ts') + ' AS entry_ms, CASE WHEN exit_ts IS NULL THEN NULL ELSE ' + ms('exit_ts') + ' END AS exit_ms, side, entry_price, exit_price, exit_reason, net_pnl FROM ' + T + ' WHERE symbol = ? AND (exit_ts IS NULL OR exit_ts >= ?) ORDER BY entry_ts',
        params: [C.symbol, C.symbol, C.symbol, dt(now - C.histSec * 1000)] });
}
if (!E.inflight && E.q.length && now >= E.nextTry) {
    const it = E.q.shift();
    E.inflight = { id: ++E.seq, kind: it.kind, item: it, sentAt: now };
    out[0] = { topic: it.sql, payload: it.params, cxKind: it.kind, cxBatch: E.inflight.id, cxRow: it.row };
}
if (E.phase === 'OPERANDO') {
    const M = global.get('md_mem_1s', 'memory'), g = M && M.series && M.series[C.symbol];
    if (g && g.count) {
        const nb = [];
        for (let i = 1; i <= g.count; i++) { const b = g.buf[(g.idx - i + g.size) % g.size]; if (!b || b.t <= E.lastT) break; nb.push(b); }
        for (let j = nb.length - 1; j >= 0; j--) { const b = nb[j]; onBucket(b, b.t > E.replayUntil); E.lastT = b.t; E.dirty = true; }
    }
}
if (E.dirty && now - E.lastEmit >= 250) {
    E.dirty = false; E.lastEmit = now;
    const p = { cx: { state: state(), pts: E.newPts } };
    E.newPts = [];
    if (E.tradesRev !== E.sentRev) { p.cx.trades = recentTrades(); p.cx.cfg = cfgOut(); E.sentRev = E.tradesRev; }
    out[1] = { payload: p };
    node.status(statusText());
}
return (out[0] || out[1]) ? out : null;`;

const CX_IO_TPL = String.raw`<div id="prrr_cx_io" style="display:none"></div>
<script>
(function (scope) {
  // Etapa 7: puente de datos CONCEPTITO → window.__prrrCX (lo dibuja el template MERCADO en el panel ZEC).
  var X = window.__prrrCX || (window.__prrrCX = { cap: 14400, idx: new Float64Array(14400).fill(-1), a: new Float64Array(14400), b: new Float64Array(14400),
                                                    state: null, cfg: null, trades: [], rev: 0, lastPt: 0, at: 0 });
  function pts(a) {
    for (var i = 0; i < a.length; i += 3) {
      var s = Math.floor(a[i] / 1000), k = s % X.cap;
      if (X.idx[k] > s) continue;
      X.idx[k] = s; X.a[k] = a[i + 1] == null ? NaN : a[i + 1]; X.b[k] = a[i + 2] == null ? NaN : a[i + 2];
      if (a[i] > X.lastPt) X.lastPt = a[i];
    }
  }
  scope.$watch('msg', function (m) {
    if (!m || !m.payload) return;
    var s = m.payload.cxSnap, c = m.payload.cx;
    if (s) { if (s.cfg) X.cfg = s.cfg; pts(s.sma || []); X.trades = s.trades || []; X.state = s.state; }
    else if (c) { pts(c.pts || []); if (c.trades) X.trades = c.trades; if (c.cfg) X.cfg = c.cfg; X.state = c.state; }
    else return;
    X.at = Date.now(); X.rev++;
  });
  setTimeout(function () { scope.send({ topic: 'cx_snapshot_req', since: X.lastPt }); }, 0);   // lo que falte (página nueva: 4 h)
})(scope);
</script>`;

const gCx = egroup('prrr_grp_cx', 'ETAPA 7 — CONCEPTITO LIVE (paper trader ZEC · SMA39/76 · TP/SL/TO · conceptito_trades)', '#00838f');
const CXY = GMY + 160;
inGroup(gCx, { id: 'prrr_cx_tick', type: 'inject', z: TAB, name: 'tick 250 ms', props: [{ p: 'topic', vt: 'str' }],
  repeat: '0.25', crontab: '', once: true, onceDelay: '3', topic: 'tick', x: 180, y: CXY, wires: [[CXE]] });
inGroup(gCx, { id: CXE, type: 'function', z: TAB, name: 'CONCEPTITO motor (lee md_mem_1s ZEC · SMA39/76 · paper 1 pos · persiste)',
  func: CX_FUNC, outputs: 2, timeout: 0, noerr: 0, initialize: CX_INIT, finalize: '', libs: [],
  outputLabels: ['query → mysql', 'estado → navegador'], x: 520, y: CXY, wires: [[CXM], [CXUI]] });
inGroup(gCx, { id: CXM, type: 'mysql', z: TAB, mydb: DBCFG, name: 'MariaDB conceptito_trades', x: 880, y: CXY - 30, wires: [[CXE]] });
inGroup(gCx, { id: 'prrr_cx_catch', type: 'catch', z: TAB, name: 'errores mysql conceptito', scope: [CXM], uncaught: false, x: 880, y: CXY + 20, wires: [['prrr_cx_err_tag']] });
inGroup(gCx, { id: 'prrr_cx_err_tag', type: 'change', z: TAB, name: 'topic = db_error',
  rules: [{ t: 'set', p: 'topic', pt: 'msg', to: 'db_error', tot: 'str' }], action: '', property: '', from: '', to: '', reg: false, x: 1090, y: CXY + 20, wires: [[CXE]] });
inGroup(gCx, Object.assign({}, tplBase, { id: CXUI, name: 'CONCEPTITO → navegador (invisible)', group: 'prrr_ui_g_btc', order: 3, width: 1, height: 1,
  format: CX_IO_TPL, storeOutMessages: false, resendOnRefresh: false, x: 880, y: CXY + 70, wires: [[CXE]] }));
inGroup(gCx, { id: 'prrr_cx_comment', type: 'comment', z: TAB, name: 'Conceptito: reglas exactas',
  info: '**Paper trader, sin dinero real.** Fuente única: buckets PRRR ZEC 1 s (`md_mem_1s`, los mismos que van a `market_1s`).\n\n* SMA39 / SMA76 = media del `close` de los últimos 39 / 76 buckets (definición idéntica a la analítica 25/50; los segundos sin trades cuentan con su precio arrastrado). Si hay un hueco de segundos, la SMA vuelve a calentar.\n* Cruce: cambio de signo de SMA39 − SMA76 (empates exactos no cambian el lado). Arriba → LONG, abajo → SHORT.\n* Entrada (como el backtest: entry_idx = idx + 1): el cruce se confirma en el bucket N y la entrada es el `close` del bucket siguiente N+1; `signal_ts` = N, `entry_ts` = N+1. Exposición 1000 × 3 = 3000 USD, qty = 3000 / entrada.\n* Con posición abierta (o entrada pendiente) los cruces se ignoran; tampoco cuenta un cruce en el mismo segundo de entrada o de salida. Después de cerrar espera un cruce NUEVO.\n* Salida, evaluada en cada `close` 1 s posterior a la entrada: el close detecta bruto ≥ +32,10 → TP con bruto realizado EXACTO +32,10; bruto ≤ −84,25 → SL con −84,25 exacto; si no, 18 min desde la entrada → TIMEOUT con el PnL real de ese close. `exit_price` = precio equivalente al PnL realizado; `exit_trigger_price` = close que disparó.\n* Fees: 0,05 % del nocional de entrada (1,50) + 0,05 % del nocional de salida (qty × exit_price; con `feeBase: exposure` serían 1,50 + 1,50). Neto = bruto realizado − fees.\n* Persistencia `conceptito_trades`: fila OPEN al abrir (upsert) y CLOSED al cerrar. Al arrancar o en cada Deploy restaura la posición OPEN y el acumulado; no opera hasta haber leído la tabla.\n* Al arrancar re-procesa los buckets ya en memoria (calienta la SMA y evalúa la salida de una posición restaurada); sólo los buckets nuevos abren operaciones.',
  x: 520, y: CXY + 50, wires: [] });

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

const outFile = path.join(__dirname, '..', 'prrr-market-data-stage7.json');
fs.writeFileSync(outFile, JSON.stringify(all, null, 2) + '\n');
console.log('OK', all.length, 'nodos ->', outFile);
// Addon: sólo los nodos de la etapa 6 (para agregarlos a un flow etapa 5 en marcha sin reemplazar nada)
const addon = all.filter((n) => n.id === 'prrr_grp_gmx' || n.id === 'prrr_ui_g_gmx' || n.g === 'prrr_grp_gmx');
const addFile = path.join(__dirname, '..', 'prrr-market-data-stage6-addon.json');
fs.writeFileSync(addFile, JSON.stringify(addon, null, 2) + '\n');
console.log('OK', addon.length, 'nodos ->', addFile);
// Addon 6b: sólo nodos nuevos (ninguno existente). Para importar sobre la etapa 6 en marcha sin reemplazar nada.
const addon6b = all.filter((n) => n.id === 'prrr_grp_gmx_mkt' || n.g === 'prrr_grp_gmx_mkt');
const add6bFile = path.join(__dirname, '..', 'prrr-market-data-stage6b-addon.json');
fs.writeFileSync(add6bFile, JSON.stringify(addon6b, null, 2) + '\n');
console.log('OK', addon6b.length, 'nodos ->', add6bFile);
// Etapa 7: addon (sólo nodos nuevos) + template MERCADO completo para pegar en el nodo existente
const addon7 = all.filter((n) => n.id === 'prrr_grp_cx' || n.g === 'prrr_grp_cx');
const add7File = path.join(__dirname, '..', 'prrr-market-data-stage7-addon.json');
fs.writeFileSync(add7File, JSON.stringify(addon7, null, 2) + '\n');
console.log('OK', addon7.length, 'nodos ->', add7File);
const tplFile = path.join(__dirname, '..', 'mercado-template-stage7.html');
fs.writeFileSync(tplFile, all.find((n) => n.id === 'prrr_ui_mkt').format);
console.log('OK template MERCADO ->', tplFile);
