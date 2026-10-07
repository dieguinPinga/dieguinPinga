#!/usr/bin/env node
// Validación estática de WAVE_TEST_V1.json + WAVE_TAP_link_out.json
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const dir = process.argv[2] || __dirname;
const test = JSON.parse(fs.readFileSync(path.join(dir, 'WAVE_TEST_V1.json'), 'utf8'));
const tap = JSON.parse(fs.readFileSync(path.join(dir, 'WAVE_TAP_link_out.json'), 'utf8'));
const raw = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'WAVE_RAW_V1', 'WAVE_RAW_V1.json'), 'utf8'));
const errors = [], ok = [];
const check = (c, m) => (c ? ok : errors).push(m);

const all = test.concat(tap);
const ids = new Set();
for (const n of all) { if (ids.has(n.id)) errors.push('ID duplicado ' + n.id); ids.add(n.id); }
check(ids.size === all.length, 'IDs únicos en el test + tap (' + all.length + ')');
const rawIds = new Set(raw.map((n) => n.id));
check(all.every((n) => !rawIds.has(n.id)), 'ningún ID coincide con nodos de WAVE_RAW_V1 (el import no puede reemplazar nada)');

const tab = test.filter((n) => n.type === 'tab');
check(tab.length === 1 && tab[0].label === 'WAVE_TEST_V1', 'un único tab nuevo WAVE_TEST_V1');
check(test.every((n) => n.type === 'tab' || n.z === tab[0].id), 'WAVE_TEST_V1.json: todos los nodos en el tab nuevo (nada en WAVE_RAW_V1)');
check(!test.some((n) => n.type === 'link out'), 'el JSON del test no contiene el link out (va en snippet separado)');

const byId = new Map(all.map((n) => [n.id, n]));
let w = 0;
for (const n of test) for (const o of (n.wires || [])) for (const t of o) { w++; if (!byId.has(t) || byId.get(t).z !== tab[0].id) errors.push('wire inválido ' + n.id + ' -> ' + t); }
check(!errors.some((e) => e.startsWith('wire')), 'los ' + w + ' wires del test apuntan a nodos del tab');

check(tap.length === 1 && tap[0].type === 'link out' && tap[0].mode === 'link' && !('z' in tap[0]) && (tap[0].wires || []).length === 0,
    'snippet: un único link out, sin tab asignado y sin wires');
const lin = test.find((n) => n.type === 'link in');
check(lin && tap[0].links.length === 1 && tap[0].links[0] === lin.id && lin.links.includes(tap[0].id), 'link out TAP → WAVE_TEST enlazado con WAVE TAP IN');

const allowed = new Set(['tab', 'comment', 'function', 'inject', 'debug', 'link in', 'link out']);
check(all.every((n) => allowed.has(n.type)), 'sólo nodos core (tab/comment/function/inject/debug/link)');
const code = test.filter((n) => n.type === 'function').map((n) => [n.func, n.initialize, n.finalize].join('\n')).join('\n');
const low = JSON.stringify(all).toLowerCase();
check(!/mysql/.test(low) && !all.some((n) => /^ui[_-]|dashboard/.test(n.type)), 'sin MySQL ni Dashboard');
check(!/require\(\s*['"]fs['"]|writefile|appendfile|createwritestream/i.test(code), 'sin escritura a disco');
check(!/\b(flow|global|context)\.set\(/.test(code), 'sin context store');
check(!/\/order|create_order|place_order|addorder/i.test(code), 'sin órdenes');

// las clases de ventanas son copia literal del engine
const engineSrc = fs.readFileSync(path.join(__dirname, '..', 'WAVE_RAW_V1', 'src', 'engine.js'), 'utf8');
const slice = engineSrc.slice(engineSrc.indexOf('function r(x, d) {'), engineSrc.indexOf('// ---------- telemetría de recepción')).trim();
const ev = test.find((n) => n.name === 'WAVE TEST EVAL');
check(ev.initialize.includes(slice), 'WAVE TEST EVAL contiene copia literal de las ventanas del engine');
check(/HORIZONS = \[100, 250, 500, 1000, 2000, 5000\]/.test(ev.initialize) && /SAMPLE_MS = 250;/.test(ev.initialize), 'horizontes exactos y muestreo de 250 ms');
check(/MAX_PENDING = 400;/.test(ev.initialize) && /AGG_CAP = 4096;/.test(ev.initialize) && /RING_SAMPLES = 2000;/.test(ev.initialize), 'límites de RAM presentes');

// raw intacto: comparar con el JSON publicado (sólo informativo)
check(raw.filter((n) => n.type === 'function').length === 9, 'WAVE_RAW_V1.json de referencia intacto (9 function)');

for (const n of test.filter((x) => x.type === 'function')) {
    try {
        new vm.Script('(async function(msg,__send__,__done__){ var node={};\n' + n.func + '\n})');
        new vm.Script('(async function(__send__){ var node={};\n' + n.initialize + '\n})');
        new vm.Script('(function(){ var node={};\n' + n.finalize + '\n})');
        ok.push('sintaxis OK: ' + n.name);
    } catch (e) { errors.push('sintaxis ' + n.name + ': ' + e.message); }
}
for (const o of ok) console.log('  ✔ ' + o);
for (const e of errors) console.log('  ✘ ' + e);
console.log(errors.length ? '\nFALLÓ (' + errors.length + ')' : '\nVALIDACIÓN OK');
process.exit(errors.length ? 1 : 0);
