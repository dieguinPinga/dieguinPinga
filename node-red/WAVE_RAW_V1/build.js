#!/usr/bin/env node
// Genera WAVE_RAW_V1.json (flow importable de Node-RED) a partir de src/*.js
// Uso: node build.js [--urls urls.json] [--out archivo.json]
//   --urls permite sustituir las URLs de los exchanges (sólo para pruebas con servidores simulados)
'use strict';
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const args = process.argv.slice(2);
const argVal = (k) => { const i = args.indexOf(k); return i >= 0 ? args[i + 1] : null; };
const urlOverride = argVal('--urls') ? JSON.parse(fs.readFileSync(argVal('--urls'), 'utf8')) : {};
const outFile = argVal('--out') || path.join(__dirname, 'WAVE_RAW_V1.json');

const src = (f) => fs.readFileSync(path.join(__dirname, 'src', f), 'utf8');
const ADAPTER_CORE = src('adapter_core.js');
const NORM_COMMON = src('norm_common.js');
const ENGINE = src('engine.js');

// IDs deterministas (16 hex) derivados de un nombre: únicos y estables entre builds
const id = (name) => crypto.createHash('sha1').update('WAVE_RAW_V1/' + name).digest('hex').slice(0, 16);
const TAB = id('tab');

const EXCHANGES = {
    binance: {
        title: 'BINANCE',
        product: 'BTCUSDT',
        conns: [
            { id: 'trade', url: 'wss://stream.binance.com:9443/stream?streams=btcusdt@trade', subs: [], appPing: null },
            { id: 'book', url: 'wss://stream.binance.com:9443/stream?streams=btcusdt@bookTicker/btcusdt@depth10@100ms', subs: [], appPing: null }
        ],
        note: '// Binance envía ping de protocolo cada ~20s; la librería ws responde pong solo.\n' +
              '// Si tu servidor está en EE.UU. (HTTP 451) usa wss://stream.binance.us:9443 (BTCUSD/BTCUSDT).\n'
    },
    coinbase: {
        title: 'COINBASE',
        product: 'BTC-USD',
        conns: [
            { id: 'trade', url: 'wss://advanced-trade-ws.coinbase.com', appPing: null, subs: [
                { type: 'subscribe', product_ids: ['BTC-USD'], channel: 'market_trades' },
                { type: 'subscribe', product_ids: ['BTC-USD'], channel: 'heartbeats' }] },
            { id: 'book', url: 'wss://advanced-trade-ws.coinbase.com', appPing: null, subs: [
                { type: 'subscribe', product_ids: ['BTC-USD'], channel: 'level2' },
                { type: 'subscribe', product_ids: ['BTC-USD'], channel: 'heartbeats' }] }
        ],
        note: '// Canales de mercado públicos: no requieren JWT. Hay que suscribirse dentro de 5s tras conectar.\n' +
              '// trades y level2 van en conexiones separadas (el snapshot L2 es grande y no debe frenar los trades).\n'
    },
    kraken: {
        title: 'KRAKEN',
        product: 'BTC/USD',
        conns: [
            { id: 'trade', url: 'wss://ws.kraken.com/v2', appPing: { everyMs: 15000, payload: '{"method":"ping"}' }, subs: [
                { method: 'subscribe', params: { channel: 'trade', symbol: ['BTC/USD'], snapshot: false } }] },
            { id: 'book', url: 'wss://ws.kraken.com/v2', appPing: { everyMs: 15000, payload: '{"method":"ping"}' }, subs: [
                { method: 'subscribe', params: { channel: 'book', symbol: ['BTC/USD'], depth: 10, snapshot: true } }] }
        ],
        note: '// Kraken v2 emite heartbeat ~1/s; además se envía {"method":"ping"} de aplicación.\n'
    },
    okx: {
        title: 'OKX',
        product: 'BTC-USDT',
        conns: [
            { id: 'trade', url: 'wss://ws.okx.com:8443/ws/v5/business', appPing: { everyMs: 15000, payload: 'ping' }, subs: [
                { op: 'subscribe', args: [{ channel: 'trades-all', instId: 'BTC-USDT' }] }] },
            { id: 'book', url: 'wss://ws.okx.com:8443/ws/v5/public', appPing: { everyMs: 15000, payload: 'ping' }, subs: [
                { op: 'subscribe', args: [{ channel: 'bbo-tbt', instId: 'BTC-USDT' }, { channel: 'books', instId: 'BTC-USDT' }] }] }
        ],
        note: '// OKX corta la conexión si no recibe nada en 30s: se envía "ping" de texto cada 15s (responde "pong").\n' +
              '// trades-all vive en el endpoint /business; bbo-tbt y books en /public.\n' +
              '// Cuentas de OKX US / EEA pueden requerir otro host (p.ej. wsus.okx.com / wseea.okx.com).\n'
    }
};

const NORM_FILES = { binance: 'norm_binance.js', coinbase: 'norm_coinbase.js', kraken: 'norm_kraken.js', okx: 'norm_okx.js' };

function adapterIni(ex, cfg) {
    const conns = cfg.conns.map((c) => Object.assign({}, c, { kind: c.id, url: (urlOverride[ex] && urlOverride[ex][c.id]) || c.url }));
    return '// ===== CONFIG ' + cfg.title + ' (editable) =====\n' +
        "const EXCHANGE = '" + ex + "';\n" +
        cfg.note +
        'const CONNS = ' + JSON.stringify(conns, null, 4) + ';\n\n' +
        ADAPTER_CORE;
}

function normIni(ex, cfg) {
    return '// ===== CONFIG ' + cfg.title + ' =====\n' +
        "const EXCHANGE = '" + ex + "';\n" +
        "const PRODUCT = '" + cfg.product + "';\n" +
        "const CONN_IDS = ['trade', 'book'];\n\n" +
        NORM_COMMON + '\n' + src(NORM_FILES[ex]);
}

const ADAPTER_FUNC = '// Control: topic "resync" (desde el normalizer) o "reconnect" (inject manual).\n' +
    '// Los frames NO pasan por aquí: salen directo del callback ws.on("message") definido en "On Start".\n' +
    'const A = globalThis.__WAVE_ADAPTER__;\nif (A) A.control(msg);\nreturn null;';
const ADAPTER_FIN = 'const A = globalThis.__WAVE_ADAPTER__;\nif (A) A.stop();\nglobalThis.__WAVE_ADAPTER__ = null;';
const NORM_FUNC = '// Toda la lógica vive en "On Start". Aquí sólo se despacha cada frame.\n' +
    'globalThis.__WAVE_NORM__.onInput(msg);\nreturn null;';
const NORM_FIN = 'if (globalThis.__WAVE_NORM__) globalThis.__WAVE_NORM__.stop();';
const ENGINE_FUNC = '// Event-driven: CADA mensaje se procesa inmediatamente (ventanas, telemetría).\n' +
    '// La salida 1 Hz la genera un timer en "On Start" que sólo LEE el estado.\n' +
    'globalThis.__WAVE_ENGINE__.onMsg(msg);\nreturn null;';
const ENGINE_FIN = 'if (globalThis.__WAVE_ENGINE__) globalThis.__WAVE_ENGINE__.stop();';

const ARCH = [
    'WAVE_RAW_V1 · SENSOR DE ALTA FRECUENCIA (Etapa A trades + Etapa B order book)',
    '',
    '[WS ADAPTER x4] --frames crudos--> [NORMALIZER x4] --eventos--> [WAVE RAW ENGINE] --1 Hz--> [Debug]',
    '        ^                                  |',
    '        +------------- resync -------------+',
    '',
    '1) WS ADAPTER (uno por exchange, 2 sockets: trade y book). Usa el módulo npm "ws".',
    '   En ws.on("message") la PRIMERA instrucción es const t = Date.now(); y se emite',
    '   {exchange, conn, gen, t_recv, payload} sin parsear y sin clonar (node.send(msg,false)).',
    '   Reconexión con backoff exponencial 1s..30s + jitter, watchdog de feed stale (20s),',
    '   timeout de conexión, ping de protocolo cada 10s y ping de aplicación donde aplica (OKX, Kraken).',
    '2) NORMALIZER (uno por exchange): parsea JSON, aplica la semántica de cada API',
    '   (lado agresor, snapshot + deltas, secuencias, checksums) y emite un mensaje por frame con',
    '   payload = [eventos normalizados]. Mantiene el libro en memoria pero sólo emite BBO + top5/top10.',
    '   Si detecta libro corrupto (gap de secuencia, checksum, libro cruzado) pide RESYNC al adaptador.',
    '   Cada 1s emite contadores acumulados (frames, inválidos, descartados, gaps, checksums, side_check).',
    '3) WAVE RAW ENGINE: procesa CADA evento al llegar (ventanas móviles 100/250/500/1000 ms con',
    '   sumas incrementales y deques monótonos). Un timer de 1s SÓLO lee el estado y emite:',
    '   salida 1 = PULSE (compacto), salida 2 = TELEMETRY (detalle).',
    '',
    'NO hay: dashboard, MySQL, escritura a disco, context store, simulador, órdenes, señales LONG/SHORT,',
    'SMA ni velas. Ningún Debug recibe trades individuales.',
    '',
    'Símbolos: Binance BTCUSDT, Coinbase BTC-USD, Kraken BTC/USD, OKX BTC-USDT (USD vs USDT difieren',
    'unos bps: por eso el consolidado suma flujos y usa la MEDIANA de los movimientos por exchange).',
    'Latencia aparente = local_receive_timestamp - exchange_timestamp: depende de la sincronización NTP',
    'del servidor (chrony/timesyncd). Binance spot bookTicker/depth no traen timestamp => latencia null.'
].join('\n');

const nodes = [];
nodes.push({ id: TAB, type: 'tab', label: 'WAVE_RAW_V1', disabled: false,
    info: 'Sensor de alta frecuencia BTC (trades + order book) para Binance, Coinbase, Kraken y OKX.\n\n' + ARCH });

nodes.push({ id: id('comment/arch'), type: 'comment', z: TAB, name: 'ARQUITECTURA · WAVE_RAW_V1 (abrir para leer)', info: ARCH, x: 260, y: 40, wires: [] });
nodes.push({ id: id('comment/adapters'), type: 'comment', z: TAB, name: '1) WS ADAPTERS · t_recv=Date.now() antes de parsear · backoff · watchdog · ping',
    info: 'Cada adaptador abre 2 WebSockets (trade / book). Editar URLs o símbolos en la pestaña "On Start" (bloque CONFIG).\n' +
          'Entrada: topic "resync" (del normalizer, con msg.conn) o "reconnect" (manual).\n' +
          'Salida: frames crudos + eventos de conexión (open/close) por el MISMO cable, en orden.', x: 330, y: 100, wires: [] });
nodes.push({ id: id('comment/norm'), type: 'comment', z: TAB, name: '2) NORMALIZERS · semántica por exchange · snapshot+deltas · resync',
    info: 'Salida 1 -> ENGINE (eventos, conexión, contadores 1 Hz). Salida 2 -> ADAPTER (resync).\n' +
          'side_check: compara cada trade contra el BBO previo (precio >= ask => BUY, <= bid => SELL).\n' +
          'Un agree% alto confirma la semántica BUY/SELL; si fuera bajo (<30%) la semántica estaría invertida.', x: 680, y: 100, wires: [] });
nodes.push({ id: id('comment/engine'), type: 'comment', z: TAB, name: '3) ENGINE event-driven · salida humana 1 Hz',
    info: 'PULSE: por exchange trade_eps/book_eps (eventos/s), *_fps (frames WS crudos/s), inter-arrival de frames con datos (media/p50/p95),\n' +
          'latencia aparente p50, edad del último evento, flujos 250ms, desplazamiento de precio, BBO, spread, imbalance L1/5/10 y su velocidad.\n' +
          'node.pipeline_lag_*: tiempo desde el socket hasta el ENGINE. Si crece de forma sostenida => Node-RED acumula cola.\n' +
          'node.loop_lag_*: retraso del event loop (sonda de 50 ms).\n' +
          'TELEMETRY: todas las ventanas 100/250/500/1000 ms, percentiles, contadores del normalizer, estado de conexión.\n' +
          'Inject "RESET STATS" reinicia las estadísticas en memoria.', x: 1030, y: 100, wires: [] });

const ENGINE_ID = id('engine');
const ADAPTER_IDS = [];
let y = 160;
for (const ex of Object.keys(EXCHANGES)) {
    const cfg = EXCHANGES[ex];
    const aId = id('adapter/' + ex), nId = id('norm/' + ex);
    ADAPTER_IDS.push(aId);
    nodes.push({
        id: aId, type: 'function', z: TAB, name: 'WS ADAPTER · ' + cfg.title,
        func: ADAPTER_FUNC, outputs: 1, timeout: 0, noerr: 0,
        initialize: adapterIni(ex, cfg), finalize: ADAPTER_FIN,
        libs: [{ var: 'WS', module: 'ws' }],
        x: 330, y: y, wires: [[nId]]
    });
    nodes.push({
        id: nId, type: 'function', z: TAB, name: 'NORMALIZER · ' + cfg.title,
        func: NORM_FUNC, outputs: 2, timeout: 0, noerr: 0,
        initialize: normIni(ex, cfg), finalize: NORM_FIN, libs: [],
        outputLabels: ['eventos -> engine', 'resync -> adapter'],
        x: 680, y: y, wires: [[ENGINE_ID], [aId]]
    });
    y += 80;
}

nodes.push({ id: id('inject/reconnect'), type: 'inject', z: TAB, name: 'RECONNECT ALL (manual)',
    props: [{ p: 'topic', vt: 'str' }], repeat: '', crontab: '', once: false, onceDelay: 0.1, topic: 'reconnect',
    x: 120, y: y + 20, wires: [ADAPTER_IDS] });

nodes.push({
    id: ENGINE_ID, type: 'function', z: TAB, name: 'WAVE RAW ENGINE',
    func: ENGINE_FUNC, outputs: 2, timeout: 0, noerr: 0,
    initialize: ENGINE, finalize: ENGINE_FIN, libs: [],
    outputLabels: ['PULSE 1 Hz', 'TELEMETRY 1 Hz'],
    x: 1030, y: 280, wires: [[id('debug/pulse')], [id('debug/telemetry')]]
});
nodes.push({ id: id('inject/reset'), type: 'inject', z: TAB, name: 'RESET STATS',
    props: [{ p: 'topic', vt: 'str' }], repeat: '', crontab: '', once: false, onceDelay: 0.1, topic: 'reset',
    x: 1030, y: 200, wires: [[ENGINE_ID]] });
nodes.push({ id: id('debug/pulse'), type: 'debug', z: TAB, name: 'WAVE PULSE (1 Hz)', active: true, tosidebar: true, console: false, tostatus: false,
    complete: 'payload', targetType: 'msg', statusVal: '', statusType: 'auto', x: 1300, y: 260, wires: [] });
nodes.push({ id: id('debug/telemetry'), type: 'debug', z: TAB, name: 'WAVE TELEMETRY detalle (1 Hz, activar si se necesita)', active: false, tosidebar: true, console: false, tostatus: false,
    complete: 'payload', targetType: 'msg', statusVal: '', statusType: 'auto', x: 1360, y: 300, wires: [] });

fs.writeFileSync(outFile, JSON.stringify(nodes, null, 4) + '\n');
console.log('OK ->', outFile, '(' + nodes.length + ' nodos)');
