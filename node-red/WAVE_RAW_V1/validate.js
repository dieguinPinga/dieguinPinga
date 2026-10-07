#!/usr/bin/env node
// Validación estática del flow generado. Uso: node validate.js [WAVE_RAW_V1.json]
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const file = process.argv[2] || path.join(__dirname, 'WAVE_RAW_V1.json');
const nodes = JSON.parse(fs.readFileSync(file, 'utf8'));
const errors = [];
const ok = [];
const check = (cond, msg) => (cond ? ok : errors).push(msg);

const byId = new Map();
for (const n of nodes) {
    if (byId.has(n.id)) errors.push('ID duplicado: ' + n.id);
    byId.set(n.id, n);
}
check(byId.size === nodes.length, 'todos los IDs son únicos (' + nodes.length + ')');

const tabs = nodes.filter((n) => n.type === 'tab');
check(tabs.length === 1 && tabs[0].label === 'WAVE_RAW_V1', 'un único tab llamado WAVE_RAW_V1');
const tabId = tabs[0] && tabs[0].id;
check(nodes.every((n) => n.type === 'tab' || n.z === tabId), 'todos los nodos pertenecen al tab nuevo (sin dependencias externas)');

let wireCount = 0;
for (const n of nodes) {
    for (const out of (n.wires || [])) {
        for (const target of out) {
            wireCount++;
            if (!byId.has(target)) errors.push('wire roto: ' + n.id + ' -> ' + target);
            else if (byId.get(target).z !== tabId) errors.push('wire fuera del tab: ' + n.id + ' -> ' + target);
        }
    }
    if (n.type === 'function') {
        if ((n.wires || []).length !== n.outputs) errors.push('outputs != wires.length en ' + n.name);
    }
}
check(errors.filter((e) => e.startsWith('wire')).length === 0, 'los ' + wireCount + ' wires apuntan a nodos existentes del tab');

const allowed = new Set(['tab', 'comment', 'function', 'inject', 'debug']);
check(nodes.every((n) => allowed.has(n.type)), 'sólo tipos core: tab/comment/function/inject/debug');
const all = JSON.stringify(nodes).toLowerCase();
check(!/mysql/.test(nodes.map((n) => n.type).join(' ')) && !/require\(['"]mysql/.test(all), 'sin MySQL');
check(!nodes.some((n) => /^ui[_-]|dashboard/.test(n.type)), 'sin nodos Dashboard');
check(!/\/api\/v3\/order|\/api\/v5\/trade\/order|"op":"order"|create_order|addorder|place_order|\/orders/.test(all), 'sin endpoints/ops de ejecución de órdenes');
const code = nodes.filter((n) => n.type === 'function').map((n) => [n.func, n.initialize, n.finalize].join('\n')).join('\n');
check(!/require\(\s*['"]fs['"]|writeFile|appendFile|createWriteStream/.test(code), 'sin escritura a disco');
check(!/\b(flow|global|context)\.set\(/.test(code), 'sin context store (nada se persiste)');

// debug nodes: sólo pueden colgar del ENGINE (salidas 1 Hz)
const engine = nodes.find((n) => n.name === 'WAVE RAW ENGINE');
check(!!engine, 'existe el Function node "WAVE RAW ENGINE"');
const debugIds = new Set(nodes.filter((n) => n.type === 'debug').map((n) => n.id));
const debugFeeders = nodes.filter((n) => (n.wires || []).some((o) => o.some((t) => debugIds.has(t))));
check(debugFeeders.length === 1 && debugFeeders[0] === engine, 'los Debug sólo reciben la salida 1 Hz del ENGINE');

// timestamp local como primera instrucción del callback de recepción
for (const n of nodes.filter((x) => (x.libs || []).some((l) => l.module === 'ws'))) {
    const m = /ws\.on\('message',\s*function\s*\(data\)\s*\{\s*\n\s*const t = Date\.now\(\);/.exec(n.initialize);
    check(!!m, n.name + ': const t = Date.now() es la 1ª instrucción de ws.on("message")');
    const body = n.initialize.slice(n.initialize.indexOf("ws.on('message'"));
    const cb = body.slice(0, body.indexOf('});'));
    check(!/JSON\.parse/.test(cb), n.name + ': el callback de recepción no parsea JSON');
    check(/scheduleReconnect/.test(n.initialize) && /Math\.pow\(2, c\.attempt\)/.test(n.initialize), n.name + ': reconexión con backoff exponencial');
    check(/staleMs/.test(n.initialize) && /setInterval\(watchdog/.test(n.initialize), n.name + ': watchdog de feed stale');
    check(/c\.ws\.ping\(\)/.test(n.initialize), n.name + ': ping de protocolo');
}

// Coinbase TRADE: feed individual de Coinbase Exchange 'matches', NO Advanced Trade market_trades
{
    const ad = nodes.find((n) => n.name === 'WS ADAPTER · COINBASE');
    const nz = nodes.find((n) => n.name === 'NORMALIZER · COINBASE');
    const m = /const CONNS = (\[[\s\S]*?\]);\n/.exec(ad.initialize);
    const conns = JSON.parse(m[1]);
    const tr = conns.find((c) => c.id === 'trade');
    const subs = JSON.stringify(tr.subs);
    check(tr.url === 'wss://ws-feed.exchange.coinbase.com' && /"matches"/.test(subs) && !/market_trades/.test(subs),
        'Coinbase TRADE usa ws-feed.exchange.coinbase.com canal "matches" (no market_trades)');
    check(!/ch === 'market_trades'|channel: 'market_trades'/.test(nz.initialize), 'el normalizer de Coinbase ya no procesa market_trades');
    check(/ty === 'match'/.test(nz.initialize) && /if \(raw === 'buy'\) return CB_SIDE_FIELD_IS_MAKER \? 'SELL'/.test(nz.initialize) && /const CB_SIDE_FIELD_IS_MAKER = true;/.test(nz.initialize),
        'Coinbase match: side maker invertido a taker (buy->SELL, sell->BUY)');
    const bk = conns.find((c) => c.id === 'book');
    check(bk.url === 'wss://advanced-trade-ws.coinbase.com' && /"level2"/.test(JSON.stringify(bk.subs)), 'Coinbase BOOK sigue en Advanced Trade level2');
}

// el procesamiento principal no depende de timers: el body del engine procesa cada msg
check(/onMsg\(msg\)/.test(engine.func) && /function onEvents\(msg\)/.test(engine.initialize), 'ENGINE procesa cada mensaje en el body (event-driven)');
check(/setInterval\(emit, EMIT_EVERY_MS\)/.test(engine.initialize) && !/onEvents\(/.test(engine.initialize.slice(engine.initialize.indexOf('function emit()'), engine.initialize.indexOf('// sonda de lag'))),
    'el timer de 1s sólo emite (no procesa mercado)');

// sintaxis: compilar cada script tal como lo envuelve Node-RED
for (const n of nodes.filter((x) => x.type === 'function')) {
    try {
        new vm.Script('(async function(msg,__send__,__done__){ var node={};\n' + n.func + '\n})', { filename: n.name + ' body' });
        if (n.initialize) new vm.Script('(async function(__send__){ var node={};\n' + n.initialize + '\n})', { filename: n.name + ' setup' });
        if (n.finalize) new vm.Script('(function(){ var node={};\n' + n.finalize + '\n})', { filename: n.name + ' cleanup' });
        ok.push('sintaxis OK: ' + n.name);
    } catch (e) {
        errors.push('sintaxis ' + n.name + ': ' + e.message);
    }
}

for (const o of ok) console.log('  ✔ ' + o);
for (const e of errors) console.log('  ✘ ' + e);
console.log(errors.length ? '\nFALLÓ (' + errors.length + ' errores)' : '\nVALIDACIÓN OK');
process.exit(errors.length ? 1 : 0);
