#!/usr/bin/env node
// Genera node-red/prrr-market-data-stage1.json (flow importable en Node-RED).
// Uso: node node-red/tools/build-prrr-stage1.js
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
const EXCHANGES = [
  {
    id: 'binance', label: 'Binance', pairs: 'BTCUSDT, ZECUSDT',
    url: 'wss://stream.binance.com:9443/stream?streams=btcusdt@trade/zecusdt@trade&timeUnit=MICROSECOND',
    subs: [], pingMs: 0, pingPayload: '', staleMs: 30000, ignore: '',
    norm: String.raw`// Binance spot @trade (trades individuales, no aggTrade).
// timeUnit=MICROSECOND => T en microsegundos. m=true => el comprador es maker => agresor SELL.
const MAP = { BTCUSDT: ['BTC', 'USDT'], ZECUSDT: ['ZEC', 'USDT'] };
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
    symbol: m[0], quote: m[1], market: d.s, exchange: EX,
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
    symbol: m[0], quote: m[1], market: d.product_id, exchange: EX,
    price: price, quantity: qty, side: d.side === 'sell' ? 'BUY' : (d.side === 'buy' ? 'SELL' : null),
    trade_id: d.trade_id,
    exchange_timestamp: ts, exchange_ts_raw: d.time,
    local_receive_timestamp: msg.t_recv,
    raw: d
} };`,
  },
  {
    id: 'kraken', label: 'Kraken', pairs: 'BTC/USD, ZEC/USD',
    url: 'wss://ws.kraken.com/v2',
    subs: [
      { method: 'subscribe', params: { channel: 'trade', symbol: ['BTC/USD'], snapshot: false } },
      { method: 'subscribe', params: { channel: 'trade', symbol: ['ZEC/USD'], snapshot: false } },
    ],
    pingMs: 0, pingPayload: '', staleMs: 30000, ignore: '',
    norm: String.raw`// Kraken WS v2, canal "trade". side = lado del taker (agresor). timestamp RFC3339 con µs.
const MAP = { 'BTC/USD': ['BTC', 'USD'], 'ZEC/USD': ['ZEC', 'USD'] };
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
        symbol: m[0], quote: m[1], market: d.symbol, exchange: EX,
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
    id: 'okx', label: 'OKX', pairs: 'BTC-USDT, ZEC-USDT',
    // trades-all (endpoint business) = cada trade individual; "trades" en /public viene agregado por orden taker
    url: 'wss://ws.okx.com:8443/ws/v5/business',
    subs: [
      { op: 'subscribe', args: [{ channel: 'trades-all', instId: 'BTC-USDT' }] },
      { op: 'subscribe', args: [{ channel: 'trades-all', instId: 'ZEC-USDT' }] },
    ],
    pingMs: 20000, pingPayload: 'ping', staleMs: 45000, ignore: 'pong',
    norm: String.raw`// OKX v5 canal "trades-all" (trades individuales). side = lado del taker. ts en ms.
const MAP = { 'BTC-USDT': ['BTC', 'USDT'], 'ZEC-USDT': ['ZEC', 'USDT'] };
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
        symbol: m[0], quote: m[1], market: d.instId, exchange: EX,
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
    id: 'bybit', label: 'Bybit', pairs: 'BTCUSDT, ZECUSDT',
    url: 'wss://stream.bybit.com/v5/public/spot',
    subs: [
      { op: 'subscribe', args: ['publicTrade.BTCUSDT'] },
      { op: 'subscribe', args: ['publicTrade.ZECUSDT'] },
    ],
    pingMs: 20000, pingPayload: '{"op":"ping"}', staleMs: 45000, ignore: '',
    norm: String.raw`// Bybit v5 spot "publicTrade". S = lado del taker (Buy/Sell). T en ms.
const MAP = { BTCUSDT: ['BTC', 'USDT'], ZECUSDT: ['ZEC', 'USDT'] };
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
        symbol: m[0], quote: m[1], market: d.s, exchange: EX,
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
    id: 'bitfinex', label: 'Bitfinex', pairs: 'tBTCUSD, tZECUSD',
    url: 'wss://api-pub.bitfinex.com/ws/2',
    subs: [
      { event: 'subscribe', channel: 'trades', symbol: 'tBTCUSD' },
      { event: 'subscribe', channel: 'trades', symbol: 'tZECUSD' },
    ],
    pingMs: 0, pingPayload: '', staleMs: 45000, ignore: '',
    norm: String.raw`// Bitfinex WS v2 canal "trades". Se usa sólo "te" (trade executed, llega antes que "tu").
// [chanId,"te",[ID,MTS,AMOUNT,PRICE]]  AMOUNT>0 => agresor BUY, <0 => SELL.
const MAP = { tBTCUSD: ['BTC', 'USD'], tZECUSD: ['ZEC', 'USD'] };
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
    symbol: m[0], quote: m[1], market: sym, exchange: EX,
    price: price, quantity: Math.abs(amount), side: amount > 0 ? 'BUY' : (amount < 0 ? 'SELL' : null),
    trade_id: t[0],
    exchange_timestamp: ts, exchange_ts_raw: t[1],
    local_receive_timestamp: msg.t_recv,
    raw: t
} };`,
  },
];

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
            try { ws.send(typeof s === 'string' ? s : JSON.stringify(s)); }
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
            try { if (cfg.pingPayload) C.ws.send(cfg.pingPayload); else C.ws.ping(); } catch (e) {}
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
  id: TAB, type: 'tab', label: 'PRRR Market Data (Etapa 1)', disabled: false,
  info: 'Etapa 1: adquisición de trades individuales BTC/ZEC por WebSocket directo desde varios exchanges, formato normalizado común, métricas de caudal/frescura y dashboard.\n\nConsumir el stream normalizado desde otros flows con un **link in** conectado a `MD STREAM →`.\n\nContexto global disponible: `md_stats` (métricas de la última ventana de 1 s) y `md_ring` (últimos 5000 trades por símbolo, buffer circular).',
});

// Dashboard (node-red-dashboard 1.x/3.x). No se incluye ui_base: usa el existente.
const UI_TAB = 'prrr_ui_tab';
add({ id: UI_TAB, type: 'ui_tab', name: 'PRRR Market Data', icon: 'fa-bolt', order: 90, disabled: false, hidden: false });
const G = {
  btc: { id: 'prrr_ui_g_btc', name: 'BTC', order: 1, width: 12 },
  zec: { id: 'prrr_ui_g_zec', name: 'ZEC', order: 2, width: 12 },
  ctrl: { id: 'prrr_ui_g_ctrl', name: 'Control', order: 4, width: 12 },
  thr: { id: 'prrr_ui_g_thr', name: 'Throughput / frescura (1 s)', order: 5, width: 12 },
  perf: { id: 'prrr_ui_g_perf', name: 'Exchanges · caudal · frescura', order: 3, width: 24 },
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
  const g = egroup('prrr_grp_' + ex.id, ex.label + ' — ' + ex.pairs, COLORS[i % COLORS.length]);
  const ids = {
    inj: 'prrr_' + ex.id + '_autostart', sw: 'prrr_' + ex.id + '_switch', ws: 'prrr_' + ex.id + '_ws',
    norm: 'prrr_' + ex.id + '_norm', lo: 'prrr_' + ex.id + '_linkout',
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
    group: G.ctrl.id, order: 1 + i, width: 4, height: 1, passthru: true, decouple: 'false',
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
const RING_SIZE = 5000;          // últimos N trades por símbolo (buffer circular)
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
    symbols: ['BTC', 'ZEC']      // símbolos con gráfico propio en el dashboard
};
context.set('S', S, 'memory');
global.set('md_ring', S.ring, 'memory');`;

const CORE_FUNC = String.raw`// ===== MD CORE =====
// Entradas: trades normalizados (topic 'trade'), 'ws_status', 'md_err', 'tick', 'render_ms', 'reset'.
// Salida 1 = stream normalizado (1 msg por trade, sin agregar). Salidas 2..8 = dashboard (desacopladas).
const S = context.get('S', 'memory');
const now = Date.now();
const topic = msg.topic;

function newWin() { return { n: 0, bySym: {}, frames: 0, latSum: 0, latN: 0, latMin: Infinity, latMax: -Infinity, pipeSum: 0, pipeMax: 0 }; }
function getEx(name) {
    let e = S.ex[name];
    if (!e) e = S.ex[name] = { name: name, state: '?', reconnects: 0, lastError: '', lastMsgAge: null, parseErrors: 0, lastParseError: '', total: 0, last: {}, w: newWin(), rates: null };
    return e;
}

// ---------------- camino caliente: 1 trade ----------------
if (topic === 'trade') {
    const t = msg.payload;
    const e = getEx(t.exchange);
    const w = e.w;
    const pipe = now - t.local_receive_timestamp;          // cola interna de Node-RED (sin depender del reloj del exchange)
    const lat = t.local_receive_timestamp - t.exchange_timestamp; // exchange -> notebook (depende de sincronía NTP)
    w.n++; e.total++; S.totalEvents++;
    w.bySym[t.symbol] = (w.bySym[t.symbol] || 0) + 1;
    w.pipeSum += pipe; if (pipe > w.pipeMax) w.pipeMax = pipe;
    if (isFinite(lat)) { w.latSum += lat; w.latN++; if (lat < w.latMin) w.latMin = lat; if (lat > w.latMax) w.latMax = lat; }
    let L = e.last[t.symbol];
    if (!L) L = e.last[t.symbol] = {};
    L.price = t.price; L.qty = t.quantity; L.side = t.side; L.tRecv = t.local_receive_timestamp; L.tEx = t.exchange_timestamp; L.quote = t.quote; L.dirty = true;
    let y = S.sym[t.symbol];
    if (!y) y = S.sym[t.symbol] = { w: 0, total: 0 };
    y.w++; y.total++; y.price = t.price; y.ex = t.exchange; y.side = t.side; y.tRecv = t.local_receive_timestamp; y.dirty = true;
    let r = S.ring[t.symbol];
    if (!r) r = S.ring[t.symbol] = { size: S.ringSize, buf: new Array(S.ringSize), idx: 0, count: 0 };
    r.buf[r.idx] = t; r.idx = (r.idx + 1) % r.size; if (r.count < r.size) r.count++;
    return [msg, null, null, null, null, null, null, null];
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
    for (const k in S.ex) { S.ex[k].total = 0; S.ex[k].parseErrors = 0; S.ex[k].lastParseError = ''; }
    for (const k in S.sym) S.sym[k].total = 0;
    const clr = { payload: [] };
    return [null, clr, RED.util.cloneMessage(clr), null, null, null, RED.util.cloneMessage(clr), RED.util.cloneMessage(clr)];
}

if (topic !== 'tick') return null;

// ---------------- reloj de UI (tick cada S.tickMs) ----------------
if (S.lastTick) {
    const lag = Math.max(0, now - S.lastTick - S.tickMs);   // retraso del event loop + cola de mensajes
    S.lag.sum += lag; S.lag.n++; if (lag > S.lag.max) S.lag.max = lag;
}
S.lastTick = now;
const out = [null, null, null, null, null, null, null, null];
const fmtP = (p) => p == null ? '—' : (p >= 1000 ? p.toFixed(2) : p >= 1 ? p.toFixed(3) : p.toPrecision(5));
const fmt = (v, d) => (v == null || !isFinite(v)) ? '—' : v.toFixed(d);

// Render de gráficos + tarjetas (a S.renderMs, independiente de la tasa de trades)
if (now - S.lastRender >= S.renderMs - S.tickMs / 2) {
    S.lastRender = now;
    S.symbols.forEach(function (sym, i) {
        const pts = [];
        const perEx = [];
        for (const k in S.ex) {
            const L = S.ex[k].last[sym];
            if (!L) continue;
            if (L.dirty) { pts.push({ topic: k, payload: L.price, timestamp: L.tRecv }); L.dirty = false; }
            perEx.push({ ex: k, price: fmtP(L.price), side: L.side, age: Math.round(now - L.tRecv), quote: L.quote });
        }
        perEx.sort((a, b) => a.ex < b.ex ? -1 : 1);
        if (pts.length) out[1 + i] = pts;
        const y = S.sym[sym];
        if (y && (y.dirty || perEx.length)) {
            y.dirty = false;
            out[3 + i] = { payload: {
                symbol: sym, price: fmtP(y.price), exchange: y.ex, side: y.side,
                age: Math.round(now - y.tRecv), evs: S.symRate ? fmt(S.symRate[sym], 1) : '—',
                perEx: perEx, renderMs: S.renderMs
            } };
        }
    });
}

// Estadísticas cada 1 s
if (now - S.lastStats >= 1000) {
    const dt = (now - S.lastStats) / 1000;
    S.lastStats = now;
    const rows = [];
    let tot = 0, latSum = 0, latN = 0, pipeMaxAll = 0;
    const symTot = {};
    const names = Object.keys(S.ex).sort();
    for (const k of names) {
        const e = S.ex[k], w = e.w;
        tot += w.n; latSum += w.latSum; latN += w.latN;
        if (w.pipeMax > pipeMaxAll) pipeMaxAll = w.pipeMax;
        for (const s in w.bySym) symTot[s] = (symTot[s] || 0) + w.bySym[s];
        const lb = e.last.BTC, lz = e.last.ZEC;
        rows.push({
            ex: k, state: e.state, reconnects: e.reconnects,
            evs: fmt(w.n / dt, 1), btc: fmt((w.bySym.BTC || 0) / dt, 1), zec: fmt((w.bySym.ZEC || 0) / dt, 1),
            fps: fmt(w.frames / dt, 1),
            pBTC: lb ? fmtP(lb.price) : '—', aBTC: lb ? fmt((now - lb.tRecv) / 1000, 1) : '—',
            pZEC: lz ? fmtP(lz.price) : '—', aZEC: lz ? fmt((now - lz.tRecv) / 1000, 1) : '—',
            latAvg: w.latN ? fmt(w.latSum / w.latN, 0) : '—', latMin: w.latN ? fmt(w.latMin, 0) : '—', latMax: w.latN ? fmt(w.latMax, 0) : '—',
            pipeAvg: w.n ? fmt(w.pipeSum / w.n, 1) : '—', pipeMax: w.n ? fmt(w.pipeMax, 0) : '—',
            lastMsgAge: e.lastMsgAge == null ? '—' : fmt(e.lastMsgAge / 1000, 1),
            errs: e.parseErrors, total: e.total,
            err: e.lastError || e.lastParseError || ''
        });
        e.rates = { evs: w.n / dt };
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
        btcEvs: fmt(S.symRate.BTC || 0, 1), peakBtc: fmt(S.peak.BTC || 0, 1),
        zecEvs: fmt(S.symRate.ZEC || 0, 1), peakZec: fmt(S.peak.ZEC || 0, 1),
        total: S.totalEvents, uptime: Math.round((now - S.t0) / 1000),
        lagAvg: fmt(lagAvg, 1), lagMax: fmt(lagMax, 0), pipeMax: fmt(pipeMaxAll, 0),
        latAvg: latN ? fmt(latSum / latN, 0) : '—', renderMs: S.renderMs
    };
    out[5] = { payload: { rows: rows, summary: summary } };
    out[6] = [
        { topic: 'TOTAL', payload: +totEvs.toFixed(2) },
        { topic: 'BTC', payload: +(S.symRate.BTC || 0).toFixed(2) },
        { topic: 'ZEC', payload: +(S.symRate.ZEC || 0).toFixed(2) }
    ];
    out[7] = [
        { topic: 'lag event-loop máx (ms)', payload: lagMax },
        { topic: 'cola Node-RED máx (ms)', payload: pipeMaxAll }
    ];
    if (latN) out[7].push({ topic: 'exchange→local prom (ms)', payload: Math.round(latSum / latN) });
    global.set('md_stats', { t: now, dt: dt, totEvs: totEvs, symRate: S.symRate, lagAvg: lagAvg, lagMax: lagMax, pipeMax: pipeMaxAll, rows: rows }, 'memory');
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
inGroup(gCore, {
  id: CORE, type: 'function', z: TAB, name: 'MD CORE (stats · ring · render)',
  func: CORE_FUNC, outputs: 8, timeout: 0, noerr: 0, initialize: CORE_INIT, finalize: '', libs: [],
  outputLabels: ['stream normalizado', 'chart BTC', 'chart ZEC', 'card BTC', 'card ZEC', 'tabla exchanges', 'chart ev/s', 'chart frescura'],
  x: CX + 110, y: 340,
  wires: [['prrr_stream_out'], ['prrr_ui_chart_btc'], ['prrr_ui_chart_zec'], ['prrr_ui_card_btc'], ['prrr_ui_card_zec'], ['prrr_ui_table'], ['prrr_ui_chart_evs'], ['prrr_ui_chart_fresh']],
});
inGroup(gCore, {
  id: 'prrr_comment_core', type: 'comment', z: TAB, name: 'Formato común del trade (msg.payload)',
  info: '```\n{\n  symbol: "BTC" | "ZEC",\n  quote: "USD" | "USDT",\n  market: símbolo nativo del exchange,\n  exchange: "binance" | "coinbase" | "kraken" | "okx" | "bybit" | "bitfinex",\n  price: Number,\n  quantity: Number,\n  side: "BUY" | "SELL" | null   // lado del AGRESOR (taker)\n  trade_id,\n  exchange_timestamp: ms epoch (Number, puede tener decimales = µs),\n  exchange_ts_raw: timestamp original sin tocar (µs/ISO),\n  local_receive_timestamp: ms epoch al recibir el frame WS,\n  raw: objeto original del exchange\n}\n```\nCada evento conserva su exchange. No hay agregación ni promedios antes del core.',
  x: CX + 80, y: 260, wires: [],
});

// Dashboard nodes
const UX = 1700;
const chartBase = {
  type: 'ui_chart', z: TAB, chartType: 'line', legend: 'true', xformat: 'HH:mm:ss', interpolate: 'step',
  nodata: 'esperando trades…', dot: false, ymin: '', ymax: '', cutout: 0, useOneColor: false, useUTC: false,
  colors: ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22'],
  outputs: 1, useDifferentColor: false, className: '', wires: [[]],
};
inGroup(gUi, Object.assign({}, chartBase, {
  id: 'prrr_ui_chart_btc', name: 'BTC chart', group: G.btc.id, order: 2, width: 12, height: 7, label: 'BTC — último precio por exchange',
  removeOlder: '10', removeOlderPoints: '2000', removeOlderUnit: '60', x: UX, y: 220,
}));
inGroup(gUi, Object.assign({}, chartBase, {
  id: 'prrr_ui_chart_zec', name: 'ZEC chart', group: G.zec.id, order: 2, width: 12, height: 7, label: 'ZEC — último precio por exchange',
  removeOlder: '10', removeOlderPoints: '2000', removeOlderUnit: '60', x: UX, y: 260,
}));

const CARD_TPL = String.raw`<style>
.prrr-card{font-family:monospace;padding:2px 6px}
.prrr-card .hd{display:flex;align-items:baseline;gap:12px}
.prrr-card .sym{font-size:20px;font-weight:bold}
.prrr-card .px{font-size:34px;font-weight:bold;line-height:1.1}
.prrr-card .up{color:#27ae60}.prrr-card .dn{color:#e74c3c}
.prrr-card .meta{opacity:.75;font-size:12px}
.prrr-card .exs{font-size:12px;margin-top:2px}
.prrr-card .exs span{display:inline-block;margin-right:12px;white-space:nowrap}
</style>
<div class="prrr-card" ng-if="msg.payload">
  <div class="hd">
    <span class="sym">{{msg.payload.symbol}}</span>
    <span class="px" ng-class="{'up': msg.payload.side==='BUY', 'dn': msg.payload.side==='SELL'}">{{msg.payload.price}}</span>
    <span class="meta">{{msg.payload.exchange}} · {{msg.payload.side || '?'}} · edad {{msg.payload.age}} ms · {{msg.payload.evs}} ev/s</span>
  </div>
  <div class="exs"><span ng-repeat="e in msg.payload.perEx track by e.ex"><b>{{e.ex}}</b> <span ng-class="{'up': e.side==='BUY', 'dn': e.side==='SELL'}">{{e.price}}</span> {{e.quote}} ({{e.age}} ms)</span></div>
</div>
<div class="prrr-card" ng-if="!msg.payload"><span class="meta">esperando trades…</span></div>`;

const tplBase = { type: 'ui_template', z: TAB, storeOutMessages: true, fwdInMessages: false, resendOnRefresh: true, templateScope: 'local', className: '', wires: [[]] };
inGroup(gUi, Object.assign({}, tplBase, { id: 'prrr_ui_card_btc', name: 'BTC ticker', group: G.btc.id, order: 1, width: 12, height: 2, format: CARD_TPL, x: UX, y: 300 }));
inGroup(gUi, Object.assign({}, tplBase, { id: 'prrr_ui_card_zec', name: 'ZEC ticker', group: G.zec.id, order: 1, width: 12, height: 2, format: CARD_TPL, x: UX, y: 340 }));

const TABLE_TPL = String.raw`<style>
.prrr-t{width:100%;border-collapse:collapse;font-family:monospace;font-size:12px}
.prrr-t th,.prrr-t td{padding:2px 6px;text-align:right;border-bottom:1px solid rgba(128,128,128,.25);white-space:nowrap}
.prrr-t th{font-weight:bold;opacity:.8}
.prrr-t .l{text-align:left}
.prrr-t .connected{color:#27ae60;font-weight:bold}.prrr-t .off{color:#888}
.prrr-t .connecting{color:#e6a700}.prrr-t .down{color:#e74c3c;font-weight:bold}
.prrr-t .err{color:#e67e22;max-width:260px;overflow:hidden;text-overflow:ellipsis}
.prrr-sum{font-family:monospace;font-size:13px;margin:2px 0 6px 0}
.prrr-sum span{display:inline-block;margin-right:18px}
</style>
<div ng-if="msg.payload" style="overflow-x:auto">
<div class="prrr-sum">
  <span><b>TOTAL</b> {{msg.payload.summary.totEvs}} ev/s (pico {{msg.payload.summary.peakTot}})</span>
  <span><b>BTC</b> {{msg.payload.summary.btcEvs}} ev/s (pico {{msg.payload.summary.peakBtc}})</span>
  <span><b>ZEC</b> {{msg.payload.summary.zecEvs}} ev/s (pico {{msg.payload.summary.peakZec}})</span>
  <span><b>lag event-loop</b> {{msg.payload.summary.lagAvg}} / {{msg.payload.summary.lagMax}} ms (prom/máx)</span>
  <span><b>cola NR máx</b> {{msg.payload.summary.pipeMax}} ms</span>
  <span><b>exch→local prom</b> {{msg.payload.summary.latAvg}} ms</span>
  <span><b>eventos</b> {{msg.payload.summary.total}} en {{msg.payload.summary.uptime}} s</span>
</div>
<table class="prrr-t">
<tr>
  <th class="l">Exchange</th><th class="l">WS</th><th>reconex</th><th>ev/s</th><th>BTC ev/s</th><th>ZEC ev/s</th><th>frames/s</th>
  <th>últ. BTC</th><th>edad BTC s</th><th>últ. ZEC</th><th>edad ZEC s</th><th>últ. frame s</th>
  <th>exch→local ms prom/mín/máx</th><th>cola NR ms prom/máx</th><th>err parse</th><th>total</th><th class="l">último error</th>
</tr>
<tr ng-repeat="r in msg.payload.rows track by r.ex">
  <td class="l"><b>{{r.ex}}</b></td><td class="l" ng-class="r.state">{{r.state}}</td><td>{{r.reconnects}}</td>
  <td><b>{{r.evs}}</b></td><td>{{r.btc}}</td><td>{{r.zec}}</td><td>{{r.fps}}</td>
  <td>{{r.pBTC}}</td><td>{{r.aBTC}}</td><td>{{r.pZEC}}</td><td>{{r.aZEC}}</td><td>{{r.lastMsgAge}}</td>
  <td>{{r.latAvg}} / {{r.latMin}} / {{r.latMax}}</td><td>{{r.pipeAvg}} / {{r.pipeMax}}</td>
  <td>{{r.errs}}</td><td>{{r.total}}</td><td class="l err" title="{{r.err}}">{{r.err}}</td>
</tr>
</table>
</div>
<div ng-if="!msg.payload" style="font-family:monospace">esperando estadísticas…</div>`;
inGroup(gUi, Object.assign({}, tplBase, { id: 'prrr_ui_table', name: 'Tabla exchanges / rendimiento', group: G.perf.id, order: 1, width: 24, height: 5, format: TABLE_TPL, x: UX, y: 380 }));

inGroup(gUi, Object.assign({}, chartBase, {
  id: 'prrr_ui_chart_evs', name: 'Eventos/s', group: G.thr.id, order: 1, width: 12, height: 5, label: 'Eventos/s (TOTAL · BTC · ZEC)',
  ymin: '0', interpolate: 'linear', removeOlder: '15', removeOlderPoints: '1000', removeOlderUnit: '60', x: UX, y: 420,
}));
inGroup(gUi, Object.assign({}, chartBase, {
  id: 'prrr_ui_chart_fresh', name: 'Frescura', group: G.thr.id, order: 2, width: 12, height: 5, label: 'Frescura: lag event-loop · cola Node-RED · exchange→local (ms)',
  ymin: '0', interpolate: 'linear', removeOlder: '15', removeOlderPoints: '1000', removeOlderUnit: '60', x: UX, y: 460,
}));

// Salida hacia futuras etapas
inGroup(gOut, {
  id: 'prrr_stream_out', type: 'link out', z: TAB, name: 'MD STREAM →', mode: 'link', links: ['prrr_example_in'], x: UX - 20, y: 140, wires: [],
});
inGroup(gOut, {
  id: 'prrr_example_in', type: 'link in', z: TAB, name: 'ejemplo consumidor', links: ['prrr_stream_out'], x: UX + 160, y: 140, wires: [['prrr_example_dbg']],
});
inGroup(gOut, {
  id: 'prrr_example_dbg', type: 'debug', z: TAB, name: 'trades normalizados (activar para ver)', active: false, tosidebar: true, console: false, tostatus: false,
  complete: 'payload', targetType: 'msg', statusVal: '', statusType: 'auto', x: UX + 420, y: 140, wires: [],
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
  group: G.ctrl.id, order: 10, width: 6, height: 1, passthru: true, multiple: false,
  options: [100, 250, 500, 1000, 2000].map((v) => ({ label: v + ' ms', value: v, type: 'num' })),
  payload: '', topic: 'render_ms', topicType: 'str', className: '', x: 420, y: GY, wires: [[CORE]],
});
inGroup(gCtl, {
  id: 'prrr_ui_reset', type: 'ui_button', z: TAB, name: 'Reset stats', group: G.ctrl.id, order: 11, width: 3, height: 1, passthru: false,
  label: 'Reset stats', tooltip: 'Reinicia picos, contadores y gráficos', color: '', bgcolor: '', className: '', icon: 'fa-eraser',
  payload: 'reset', payloadType: 'str', topic: 'reset', topicType: 'str', x: 400, y: GY + 50, wires: [[CORE]],
});
inGroup(gCtl, {
  id: 'prrr_ui_reconnect', type: 'ui_button', z: TAB, name: 'Reconectar todos', group: G.ctrl.id, order: 12, width: 3, height: 1, passthru: false,
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
  id: 'prrr_comment_main', type: 'comment', z: TAB, name: 'PRRR Etapa 1 — Exchange WS → normalizador → MD BUS → MD CORE → (stream | dashboard)',
  info: 'Cada exchange: `autostart → switch dashboard → WS Connector (subflow) → normalizador → link out MD BUS`.\n\nPara agregar un exchange: copiar un grupo, cambiar env vars de la instancia del subflow (EXCHANGE, WS_URL, SUBSCRIBE, PING_*), escribir su normalizador al formato común y enlazar el link out al `MD BUS`.\n\nPara agregar un símbolo: sumarlo al SUBSCRIBE y al `MAP` del normalizador. El core lo mide automáticamente; para graficarlo agregar su nombre en `S.symbols` (On Start del core) y una salida/gráfico.',
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

const outFile = path.join(__dirname, '..', 'prrr-market-data-stage1.json');
fs.writeFileSync(outFile, JSON.stringify(all, null, 2) + '\n');
console.log('OK', all.length, 'nodos ->', outFile);
