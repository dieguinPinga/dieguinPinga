#!/usr/bin/env node
// Validación de WAVE_RAW_PLUS_TEST_V1.json contra las fuentes WAVE_RAW_V1.json y WAVE_TEST_V1.json
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const R = path.join(__dirname, '..');
const text = fs.readFileSync(path.join(__dirname, 'WAVE_RAW_PLUS_TEST_V1.json'), 'utf8');
let all;
const errors = [], ok = [];
const check = (c, m) => (c ? ok : errors).push(m);
try { all = JSON.parse(text); check(Array.isArray(all), 'JSON válido: array de nodos (formato de import de Node-RED)'); } catch (e) { console.log('JSON inválido', e.message); process.exit(1); }
const raw = JSON.parse(fs.readFileSync(path.join(R, 'WAVE_RAW_V1', 'WAVE_RAW_V1.json'), 'utf8'));
const test = JSON.parse(fs.readFileSync(path.join(R, 'WAVE_TEST_V1', 'WAVE_TEST_V1.json'), 'utf8'));
const byId = new Map();
for (const n of all) { if (byId.has(n.id)) errors.push('ID duplicado ' + n.id); byId.set(n.id, n); }
check(byId.size === all.length, 'IDs únicos (' + all.length + ' nodos)');
check(all.every((n) => typeof n.id === 'string' && typeof n.type === 'string'), 'todos los nodos tienen id y type');

const tabs = all.filter((n) => n.type === 'tab');
check(tabs.length === 2 && tabs.some((t) => t.id === '35b7a74832889057' && t.label === 'WAVE_RAW_V1') && tabs.some((t) => t.label === 'WAVE_TEST_V1'), 'exactamente 2 tabs: WAVE_RAW_V1 (35b7a74832889057) y WAVE_TEST_V1');
check(all.every((n) => n.type === 'tab' || tabs.some((t) => t.id === n.z)), 'todo nodo pertenece a uno de los 2 tabs');

let wires = 0;
for (const n of all) for (const o of (n.wires || [])) for (const t of o) {
    wires++;
    if (!byId.has(t)) errors.push('wire roto ' + n.id + ' -> ' + t);
    else if (byId.get(t).z !== n.z) errors.push('wire entre tabs ' + n.id + ' -> ' + t);
}
check(!errors.some((e) => /^wire/.test(e)), wires + ' wires: ninguno roto ni entre tabs');

const tap = all.filter((n) => n.type === 'link out');
const lin = all.filter((n) => n.type === 'link in');
check(tap.length === 1 && lin.length === 1, 'un link out y un link in');
check(tap[0].z === '35b7a74832889057' && tap[0].name === 'TAP → WAVE_TEST' && tap[0].mode === 'link', 'TAP → WAVE_TEST en el tab WAVE_RAW_V1, modo link');
check(lin[0].z === tabs.find((t) => t.label === 'WAVE_TEST_V1').id && lin[0].name === 'WAVE TAP IN', 'WAVE TAP IN en el tab WAVE_TEST_V1');
check(JSON.stringify(tap[0].links) === JSON.stringify([lin[0].id]) && JSON.stringify(lin[0].links) === JSON.stringify([tap[0].id]), 'link out ↔ link in enlazados en ambos sentidos');

const engine = all.find((n) => n.name === 'WAVE RAW ENGINE');
const NORMS = { BINANCE: '750b61939c35bf48', COINBASE: '4ef238d4e8163455', KRAKEN: 'e784b0aa97209004', OKX: 'bc40f0ca0ddf2a65' };
const rawById = new Map(raw.map((n) => [n.id, n]));
for (const [ex, id] of Object.entries(NORMS)) {
    const n = byId.get(id), o = rawById.get(id);
    check(n && n.name === 'NORMALIZER · ' + ex, 'NORMALIZER ' + ex + ' conserva el ID ' + id);
    check(JSON.stringify(n.wires[0]) === JSON.stringify([engine.id, tap[0].id]), ex + ': salida 1 -> [WAVE RAW ENGINE, TAP → WAVE_TEST]');
    check(JSON.stringify(n.wires[1]) === JSON.stringify(o.wires[1]) && byId.get(n.wires[1][0]).name === 'WS ADAPTER · ' + ex, ex + ': salida 2 (resync -> adapter) intacta');
}

// WAVE_RAW_V1: idéntico a la fuente salvo wires[0] de los 4 normalizers
let diffs = 0;
for (const o of raw) {
    const n = byId.get(o.id);
    if (!n) { errors.push('falta nodo raw ' + o.id); continue; }
    const a = JSON.parse(JSON.stringify(o)), b = JSON.parse(JSON.stringify(n));
    if (Object.values(NORMS).includes(o.id)) { a.wires[0] = []; b.wires[0] = []; }
    if (JSON.stringify(a) !== JSON.stringify(b)) { diffs++; errors.push('nodo raw modificado: ' + (o.name || o.label)); }
}
check(diffs === 0, 'WAVE_RAW_V1: los ' + raw.length + ' nodos idénticos a la versión actual (adapters, normalizers, engine, PULSE, TELEMETRY, RESET STATS, RECONNECT ALL) salvo salida 1 de los 4 normalizers');
check(test.every((o) => JSON.stringify(o) === JSON.stringify(byId.get(o.id))), 'WAVE_TEST_V1: los ' + test.length + ' nodos idénticos a la versión probada');

const cbA = all.find((n) => n.name === 'WS ADAPTER · COINBASE');
const conns = JSON.parse(/const CONNS = (\[[\s\S]*?\]);\n/.exec(cbA.initialize)[1]);
const tr = conns.find((c) => c.id === 'trade');
check(tr.url === 'wss://ws-feed.exchange.coinbase.com' && /"matches"/.test(JSON.stringify(tr.subs)) && !/market_trades/.test(JSON.stringify(tr.subs)), 'Coinbase TRADE = Exchange feed "matches" (sin market_trades)');
check(/ty === 'match'/.test(all.find((n) => n.name === 'NORMALIZER · COINBASE').initialize), 'NORMALIZER Coinbase procesa mensajes match');

const types = new Set(['tab', 'comment', 'function', 'inject', 'debug', 'link in', 'link out']);
check(all.every((n) => types.has(n.type)), 'sólo nodos core: ' + [...new Set(all.map((n) => n.type))].join(', '));
const code = all.filter((n) => n.type === 'function').map((n) => [n.func, n.initialize, n.finalize].join('\n')).join('\n');
check(!all.some((n) => /mysql/i.test(n.type)) && !/require\(['"]mysql|mysql\./i.test(code) && !all.some((n) => (n.libs || []).some((l) => /mysql/i.test(l.module))), 'sin MySQL (ni nodos, ni libs, ni código; sólo aparece en comentarios "NO hay MySQL")');
check(!all.some((n) => /^ui[_-]|dashboard/.test(n.type)), 'sin Dashboard');
check(!/require\(\s*['"]fs['"]|writeFile|appendFile|createWriteStream/.test(code) && !all.some((n) => n.type === 'file'), 'sin escritura a disco');
check(!/\b(flow|global|context)\.set\(/.test(code), 'sin context store');
check(!/\/order|create_order|place_order|addorder|"op":"order"/i.test(code), 'sin órdenes');
check(all.filter((n) => n.type === 'debug').every((d) => all.some((n) => (n.name === 'WAVE RAW ENGINE' || n.name === 'WAVE TEST EVAL') && n.wires.some((o) => o.includes(d.id)))), 'Debug sólo en salidas 1 Hz del ENGINE y 5 s/manual del TEST');
for (const n of all.filter((x) => x.type === 'function')) {
    try {
        new vm.Script('(async function(msg,__send__,__done__){ var node={};\n' + n.func + '\n})');
        if (n.initialize) new vm.Script('(async function(__send__){ var node={};\n' + n.initialize + '\n})');
        if (n.finalize) new vm.Script('(function(){ var node={};\n' + n.finalize + '\n})');
    } catch (e) { errors.push('sintaxis ' + n.name + ': ' + e.message); }
}
check(!errors.some((e) => /^sintaxis/.test(e)), 'sintaxis OK en las ' + all.filter((x) => x.type === 'function').length + ' function');
for (const o of ok) console.log('  ✔ ' + o);
for (const e of errors) console.log('  ✘ ' + e);
console.log(errors.length ? '\nFALLÓ (' + errors.length + ')' : '\nVALIDACIÓN OK');
process.exit(errors.length ? 1 : 0);
