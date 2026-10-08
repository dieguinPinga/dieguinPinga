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

const allowed = new Set(['tab', 'comment', 'function', 'inject', 'debug', 'link in', 'link out', 'http in', 'http response', 'template']);
check(all.every((n) => allowed.has(n.type)), 'sólo nodos core (tab/comment/function/inject/debug/link/http/template), sin Dashboard ni paquetes');
const code = test.filter((n) => n.type === 'function').map((n) => [n.func, n.initialize, n.finalize].join('\n')).join('\n');
const low = JSON.stringify(all).toLowerCase();
check(!/mysql/.test(low) && !all.some((n) => /^ui[_-]|dashboard/.test(n.type)), 'sin MySQL ni Dashboard');
const exp = test.find((n) => n.name === 'WAVE TEST EXPORT');
const codeNoExport = test.filter((n) => n.type === 'function' && n !== exp).map((n) => [n.func, n.initialize, n.finalize].join('\n')).join('\n');
check(!/require\(\s*['"]fs['"]|writefile|appendfile|createwritestream|fs\.promises/i.test(codeNoExport) && test.filter((n) => (n.libs || []).some((l) => l.module === 'fs')).every((n) => n === exp),
    'sólo WAVE TEST EXPORT accede a disco');
check(exp && /const REPORT_DIR = env\.get\('WAVE_REPORT_DIR'\) \|\| '\/home\/plapopepo\/wave_reports';/.test(exp.initialize) && /MAX_HISTORY = 100;/.test(exp.initialize) && /N_SAMPLES = 500;/.test(exp.initialize),
    'EXPORT: directorio /home/plapopepo/wave_reports, 500 muestras, máx. 100 históricos');
check(/\.tmp/.test(exp.initialize) && /fh\.sync\(\)/.test(exp.initialize) && /fs\.promises\.rename/.test(exp.initialize) && !/Sync\(/.test(exp.initialize.replace(/fh\.sync\(\)/g, '')),
    'EXPORT: escritura atómica (.tmp + fsync + rename) y sólo APIs asíncronas');
const auto = test.find((n) => n.name === 'AUTO EXPORT (cada 5 min)');
check(auto && auto.repeat === '300' && auto.topic === 'export_auto' && auto.wires[0][0] === exp.id, 'AUTO EXPORT cada 300 s -> EXPORT');
check(test.find((n) => n.name === 'EXPORT REPORT').wires[0][0] === exp.id, 'EXPORT REPORT (manual) -> EXPORT');
// EVAL: código idéntico a la versión ya probada (sólo cambian sus cables de salida)
const prevTest = JSON.parse(require('child_process').execSync('git show 0546b6f:node-red/WAVE_TEST_V1/WAVE_TEST_V1.json', { cwd: __dirname }).toString());
const pe = prevTest.find((n) => n.name === 'WAVE TEST EVAL'), ce = test.find((n) => n.name === 'WAVE TEST EVAL');
check(pe.initialize === ce.initialize && pe.func === ce.func && pe.finalize === ce.finalize && pe.id === ce.id, 'WAVE TEST EVAL: código e ID idénticos a la versión probada');
const lab = test.find((n) => n.name === 'WAVE LAB STORE');
check(JSON.stringify(ce.wires) === JSON.stringify([[pe.wires[0][0]], [pe.wires[1][0], exp.id, lab.id], [exp.id]]), 'EVAL: salida1 -> línea, salida2 -> detalle + EXPORT + LAB, salida3 -> EXPORT');
check(JSON.stringify(exp.wires) === JSON.stringify([[ce.id], [test.find((n) => n.name === 'WAVE TEST samples (DUMP manual)').id], [test.find((n) => n.name === 'WAVE EXPORT estado').id], [lab.id]]), 'EXPORT: salida1 -> EVAL (dump), 2 -> Debug DUMP, 3 -> Debug estado, 4 -> LAB');
check(test.find((n) => n.name === 'DUMP SAMPLES (últimas 2000)').wires[0][0] === exp.id, 'DUMP SAMPLES manual pasa por EXPORT (sólo los DUMP manuales llegan al Debug)');
check(JSON.stringify(lab.wires) === JSON.stringify([[test.find((n) => n.type === 'http response' && n.name === 'JSON').id], [ce.id]]), 'LAB STORE: salida1 -> http response, salida2 -> EVAL (reset/dump)');
check(/node\.send\(\[null, \{ topic: 'reset' \}\], false\)/.test(lab.initialize) && /topic: 'reset'/.test(test.find((n) => n.name === 'RESET TEST').topic === 'reset' ? "topic: 'reset'" : ''), 'RESET del dashboard = mismo {topic:"reset"} que el inject RESET TEST');
const changedIds = new Set([pe.id, exp.id, test.find((n) => n.name === 'DUMP SAMPLES (últimas 2000)').id]);
const noXY = (o) => JSON.stringify(Object.assign({}, o, { x: 0, y: 0 }));
check(prevTest.filter((o) => !changedIds.has(o.id)).every((o) => noXY(o) === noXY(test.find((n) => n.id === o.id))),
    'resto de nodos previos del test idénticos (sólo cambian cables de EVAL, ruteo de EXPORT y el inject DUMP)');
const https = test.filter((n) => n.type === 'http in').map((n) => n.method + ' ' + n.url).sort();
check(JSON.stringify(https) === JSON.stringify(['get /wave-lab', 'get /wave-lab/data', 'get /wave-lab/export', 'post /wave-lab/reset']), 'endpoints HTTP aislados bajo /wave-lab: ' + https.join(', '));
check(!/fs\.|require\(/.test(lab.initialize) && (lab.libs || []).length === 0, 'LAB STORE no accede a disco (descarga sólo por HTTP)');
check(/syntax: *'plain'|"syntax":"plain"/.test(JSON.stringify(test.find((n) => n.type === 'template'))) || test.find((n) => n.type === 'template').syntax === 'plain', 'template de la página sin mustache (plain)');
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
