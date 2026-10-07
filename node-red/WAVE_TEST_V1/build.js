#!/usr/bin/env node
// Genera:
//   WAVE_TEST_V1.json        -> tab nuevo y aislado (NO contiene ningún nodo de WAVE_RAW_V1)
//   WAVE_TAP_link_out.json   -> un único nodo 'link out' (TAP → WAVE_TEST), sin tab asignado
// Las clases de ventanas se copian LITERALMENTE de ../WAVE_RAW_V1/src/engine.js.
'use strict';
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const outDir = process.argv[2] || __dirname;
const id = (name) => crypto.createHash('sha1').update('WAVE_TEST_V1/' + name).digest('hex').slice(0, 16);

const engineSrc = fs.readFileSync(path.join(__dirname, '..', 'WAVE_RAW_V1', 'src', 'engine.js'), 'utf8');
const a = engineSrc.indexOf('function r(x, d) {');
const b = engineSrc.indexOf('// ---------- telemetría de recepción');
if (a < 0 || b < 0) throw new Error('no se encontraron las clases de ventanas en engine.js');
const engineWindows = engineSrc.slice(a, b).trim();
const evalSrc = fs.readFileSync(path.join(__dirname, 'src', 'eval.js'), 'utf8').replace('/*__ENGINE_WINDOWS__*/', engineWindows);

const TAB = id('tab');
const TAP = id('tap/link-out');
const LIN = id('link-in');
const EVAL = id('eval');
const DBG_LINE = id('debug/line');
const DBG_DETAIL = id('debug/detail');
const DBG_DUMP = id('debug/dump');

const ARCH = [
    'WAVE_TEST_V1 · ¿Tiene WAVE poder predictivo? (sólo medición, sin trading)',
    '',
    'WAVE_RAW_V1: NORMALIZER x4 (salida 1) --cables manuales--> [link out TAP → WAVE_TEST]',
    'WAVE_TEST_V1: [link in WAVE TAP IN] --> [WAVE TEST EVAL] --5 s--> [Debug línea] / [Debug detalle]',
    '',
    'Recibe los MISMOS eventos normalizados que el WAVE RAW ENGINE (no PULSE/TELEMETRY).',
    'Recalcula las ventanas con código copiado literalmente del engine.',
    'Muestra cada 250 ms (grilla fija de reloj local): dirección = signo(ALL.delta_usd_250ms),',
    'fuerza = |ALL.delta_usd_250ms|. Precio: mid Binance (principal) y mediana de retornos de mid (control).',
    'Horizontes +100/+250/+500/+1000/+2000/+5000 ms, cerrados "as-of" con eventos de t_recv <= t0+h.',
    'Todo en RAM. Sin disco, sin DB, sin context store, sin órdenes.'
].join('\n');

const WIRING = [
    'CABLEADO MANUAL EN WAVE_RAW_V1 (no lo hace el import):',
    '1) Abrir el tab WAVE_RAW_V1. Importar WAVE_TAP_link_out.json con "Import to: current flow"',
    '   (o crear a mano un nodo "link out" llamado TAP → WAVE_TEST, modo "Send to all connected link nodes",',
    '   y marcar como destino "WAVE TAP IN" del tab WAVE_TEST_V1).',
    '2) Agregar 4 cables, desde la SALIDA 1 (superior, "eventos -> engine") de:',
    '   NORMALIZER · BINANCE, NORMALIZER · COINBASE, NORMALIZER · KRAKEN, NORMALIZER · OKX',
    '   hacia el nodo TAP → WAVE_TEST. NO usar la salida 2 (resync -> adapter).',
    '3) Deploy. Los cables existentes hacia WAVE RAW ENGINE quedan como están.'
].join('\n');

const testNodes = [
    { id: TAB, type: 'tab', label: 'WAVE_TEST_V1', disabled: false, info: ARCH + '\n\n' + WIRING },
    { id: id('comment/arch'), type: 'comment', z: TAB, name: 'ARQUITECTURA · WAVE_TEST_V1 (abrir para leer)', info: ARCH, x: 230, y: 40, wires: [] },
    { id: id('comment/wiring'), type: 'comment', z: TAB, name: 'CABLEADO MANUAL requerido en WAVE_RAW_V1 (abrir)', info: WIRING, x: 230, y: 80, wires: [] },
    { id: id('comment/out'), type: 'comment', z: TAB, name: 'Salidas cada 5 s: 1) línea compacta 2) detalle 3) DUMP de muestras (a pedido)',
        info: 'Línea: WAVE TEST | n=<muestras> dir=<direccionales> eval=<evaluables> | <h> <mean signed bps> <hit% sin ties> t<tie%> ...\n' +
              'Detalle: counts, queue, strength (fuerza bruta + terciles PROVISIONALES), primary_binance_mid (all/up/down/weak/medium/strong por horizonte),\n' +
              'secondary_median_mid, baseline_random_direction (sólo sanity check), last_samples.\n' +
              'DUMP: últimas 2000 muestras completas (inject manual).', x: 610, y: 80, wires: [] },
    { id: LIN, type: 'link in', z: TAB, name: 'WAVE TAP IN', links: [TAP], x: 140, y: 180, wires: [[EVAL]] },
    { id: EVAL, type: 'function', z: TAB, name: 'WAVE TEST EVAL',
        func: '// Event-driven: cada mensaje del tap avanza el reloj de muestreo/cierre y luego se aplica.\n' +
              '// La salida humana (cada 5 s) la genera un timer en "On Start" que sólo LEE acumuladores.\n' +
              'globalThis.__WAVE_TEST__.onMsg(msg);\nreturn null;',
        outputs: 3, timeout: 0, noerr: 0, initialize: evalSrc,
        finalize: 'if (globalThis.__WAVE_TEST__) globalThis.__WAVE_TEST__.stop();', libs: [],
        outputLabels: ['línea 5 s', 'detalle 5 s', 'dump muestras'],
        x: 380, y: 180, wires: [[DBG_LINE], [DBG_DETAIL], [DBG_DUMP]] },
    { id: id('inject/reset'), type: 'inject', z: TAB, name: 'RESET TEST', props: [{ p: 'topic', vt: 'str' }],
        repeat: '', crontab: '', once: false, onceDelay: 0.1, topic: 'reset', x: 150, y: 240, wires: [[EVAL]] },
    { id: id('inject/dump'), type: 'inject', z: TAB, name: 'DUMP SAMPLES (últimas 2000)', props: [{ p: 'topic', vt: 'str' }],
        repeat: '', crontab: '', once: false, onceDelay: 0.1, topic: 'dump', x: 180, y: 280, wires: [[EVAL]] },
    { id: DBG_LINE, type: 'debug', z: TAB, name: 'WAVE TEST (5 s)', active: true, tosidebar: true, console: false, tostatus: false,
        complete: 'payload', targetType: 'msg', statusVal: '', statusType: 'auto', x: 640, y: 140, wires: [] },
    { id: DBG_DETAIL, type: 'debug', z: TAB, name: 'WAVE TEST detalle (5 s)', active: false, tosidebar: true, console: false, tostatus: false,
        complete: 'payload', targetType: 'msg', statusVal: '', statusType: 'auto', x: 650, y: 180, wires: [] },
    { id: DBG_DUMP, type: 'debug', z: TAB, name: 'WAVE TEST samples (DUMP manual)', active: true, tosidebar: true, console: false, tostatus: false,
        complete: 'payload', targetType: 'msg', statusVal: '', statusType: 'auto', x: 670, y: 220, wires: [] }
];

// snippet separado: sin 'z' => el editor lo coloca en el tab ACTIVO al importar con "current flow"
const tapNodes = [
    { id: TAP, type: 'link out', name: 'TAP → WAVE_TEST', mode: 'link', links: [LIN], x: 1030, y: 420, wires: [] }
];

fs.writeFileSync(path.join(outDir, 'WAVE_TEST_V1.json'), JSON.stringify(testNodes, null, 4) + '\n');
fs.writeFileSync(path.join(outDir, 'WAVE_TAP_link_out.json'), JSON.stringify(tapNodes, null, 4) + '\n');
console.log('OK ->', path.join(outDir, 'WAVE_TEST_V1.json'), '(' + testNodes.length + ' nodos) +', path.join(outDir, 'WAVE_TAP_link_out.json'), '(1 nodo)');
