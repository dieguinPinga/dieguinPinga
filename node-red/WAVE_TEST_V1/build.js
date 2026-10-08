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
const exportSrc = fs.readFileSync(path.join(__dirname, 'src', 'export.js'), 'utf8');
const labSrc = fs.readFileSync(path.join(__dirname, 'src', 'lab_store.js'), 'utf8');
const labPage = fs.readFileSync(path.join(__dirname, 'src', 'lab_page.html'), 'utf8');

const TAB = id('tab');
const TAP = id('tap/link-out');
const LIN = id('link-in');
const EVAL = id('eval');
const DBG_LINE = id('debug/line');
const DBG_DETAIL = id('debug/detail');
const DBG_DUMP = id('debug/dump');
const EXPORT = id('export');
const DBG_EXPORT = id('debug/export');
const LAB = id('lab/store');
const LAB_PAGE = id('lab/page');
const LAB_RES_HTML = id('lab/res-html');
const LAB_RES_JSON = id('lab/res-json');

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
        x: 380, y: 180, wires: [[DBG_LINE], [DBG_DETAIL, EXPORT, LAB], [EXPORT]] },
    { id: id('inject/reset'), type: 'inject', z: TAB, name: 'RESET TEST', props: [{ p: 'topic', vt: 'str' }],
        repeat: '', crontab: '', once: false, onceDelay: 0.1, topic: 'reset', x: 150, y: 240, wires: [[EVAL]] },
    { id: id('inject/dump'), type: 'inject', z: TAB, name: 'DUMP SAMPLES (últimas 2000)', props: [{ p: 'topic', vt: 'str' }],
        repeat: '', crontab: '', once: false, onceDelay: 0.1, topic: 'dump', x: 180, y: 280, wires: [[EXPORT]] },
    { id: DBG_LINE, type: 'debug', z: TAB, name: 'WAVE TEST (5 s)', active: true, tosidebar: true, console: false, tostatus: false,
        complete: 'payload', targetType: 'msg', statusVal: '', statusType: 'auto', x: 640, y: 140, wires: [] },
    { id: DBG_DETAIL, type: 'debug', z: TAB, name: 'WAVE TEST detalle (5 s)', active: false, tosidebar: true, console: false, tostatus: false,
        complete: 'payload', targetType: 'msg', statusVal: '', statusType: 'auto', x: 650, y: 180, wires: [] },
    { id: DBG_DUMP, type: 'debug', z: TAB, name: 'WAVE TEST samples (DUMP manual)', active: true, tosidebar: true, console: false, tostatus: false,
        complete: 'payload', targetType: 'msg', statusVal: '', statusType: 'auto', x: 670, y: 480, wires: [] },
    // ---------------- EXPORT de reportes (no toca la lógica de EVAL) ----------------
    { id: id('comment/export'), type: 'comment', z: TAB, name: 'EXPORT · reporte JSON completo a disco (manual + cada 5 min)',
        info: 'WAVE_TEST_LATEST.json se sobreescribe en cada export (manual o automático).\n' +
              'EXPORT REPORT (manual) crea además WAVE_TEST_YYYY-MM-DD_HH-mm-ss.json; se conservan los últimos 100.\n' +
              'Directorio: /home/plapopepo/wave_reports (se crea solo; override opcional con la variable de entorno WAVE_REPORT_DIR).\n' +
              'Contenido: generated_at, uptime_s, config, counts, queue, strength, primary_binance_mid, secondary_median_mid,\n' +
              'baseline_random_direction y las últimas 500 muestras completas (pre_move + future).\n' +
              'Escritura atómica (.tmp + fsync + rename), asíncrona y por bloques: no bloquea el procesamiento de eventos.\n' +
              'Flujo: pedido -> espera el próximo detalle de EVAL (<= 5 s) -> pide DUMP -> escribe.', x: 260, y: 340, wires: [] },
    { id: id('inject/export'), type: 'inject', z: TAB, name: 'EXPORT REPORT', props: [{ p: 'topic', vt: 'str' }],
        repeat: '', crontab: '', once: false, onceDelay: 0.1, topic: 'export', x: 150, y: 400, wires: [[EXPORT]] },
    { id: id('inject/export-auto'), type: 'inject', z: TAB, name: 'AUTO EXPORT (cada 5 min)', props: [{ p: 'topic', vt: 'str' }],
        repeat: '300', crontab: '', once: false, onceDelay: 0.1, topic: 'export_auto', x: 170, y: 440, wires: [[EXPORT]] },
    { id: EXPORT, type: 'function', z: TAB, name: 'WAVE TEST EXPORT',
        func: '// Recibe: export / export_auto (injects), detalle 5 s y DUMP de WAVE TEST EVAL.\n' +
              '// Toda la lógica y la escritura (asíncrona) viven en "On Start".\n' +
              'globalThis.__WAVE_EXPORT__.onMsg(msg);\nreturn null;',
        outputs: 4, timeout: 0, noerr: 0, initialize: exportSrc,
        finalize: 'if (globalThis.__WAVE_EXPORT__) globalThis.__WAVE_EXPORT__.stop();',
        libs: [{ var: 'fs', module: 'fs' }, { var: 'path', module: 'path' }],
        outputLabels: ['pedido de dump -> EVAL', 'DUMP manual -> Debug', 'estado del export', 'muestras -> WAVE LAB'],
        x: 420, y: 420, wires: [[EVAL], [DBG_DUMP], [DBG_EXPORT], [LAB]] },
    { id: DBG_EXPORT, type: 'debug', z: TAB, name: 'WAVE EXPORT estado', active: true, tosidebar: true, console: false, tostatus: false,
        complete: 'payload', targetType: 'msg', statusVal: '', statusType: 'auto', x: 660, y: 420, wires: [] },
    // ---------------- WAVE LAB: dashboard HTTP (sólo nodos core, sin paquetes) ----------------
    { id: id('comment/lab'), type: 'comment', z: TAB, name: 'WAVE LAB · dashboard en http://<host>:1880/wave-lab (sólo visualización)',
        info: 'Página oscura y horizontal servida por nodos core (http in / template / http response): no requiere Dashboard 2.0 ni paquetes.\n' +
              'Consume SÓLO el detalle de WAVE TEST EVAL (cada 5 s) y, mientras la página está abierta, un DUMP cada 10 s para "¿llegamos tarde?".\n' +
              'GET  /wave-lab          página\nGET  /wave-lab/data     estado JSON (la página lo consulta cada 2 s; responde desde memoria)\n' +
              'POST /wave-lab/reset    envía a EVAL el mismo {topic:"reset"} que el inject RESET TEST\n' +
              'GET  /wave-lab/export   descarga JSON (config, counts, queue, strength, resultados, baseline, últimas 500 muestras). No escribe a disco.\n' +
              'Las rutas cuelgan de httpNodeRoot (por defecto "/"). Si tenés httpNodeAuth, también aplica aquí.', x: 270, y: 540, wires: [] },
    { id: id('lab/http-page'), type: 'http in', z: TAB, name: 'GET /wave-lab', url: '/wave-lab', method: 'get', upload: false, swaggerDoc: '', x: 140, y: 600, wires: [[LAB_PAGE]] },
    { id: LAB_PAGE, type: 'template', z: TAB, name: 'WAVE LAB página', field: 'payload', fieldType: 'msg', format: 'html', syntax: 'plain',
        template: labPage, output: 'str', x: 360, y: 600, wires: [[LAB_RES_HTML]] },
    { id: LAB_RES_HTML, type: 'http response', z: TAB, name: 'HTML', statusCode: '', headers: { 'content-type': 'text/html; charset=utf-8' }, x: 560, y: 600, wires: [] },
    { id: id('lab/http-data'), type: 'http in', z: TAB, name: 'GET /wave-lab/data', url: '/wave-lab/data', method: 'get', upload: false, swaggerDoc: '', x: 150, y: 660, wires: [[LAB]] },
    { id: id('lab/http-reset'), type: 'http in', z: TAB, name: 'POST /wave-lab/reset', url: '/wave-lab/reset', method: 'post', upload: false, swaggerDoc: '', x: 160, y: 700, wires: [[LAB]] },
    { id: id('lab/http-export'), type: 'http in', z: TAB, name: 'GET /wave-lab/export', url: '/wave-lab/export', method: 'get', upload: false, swaggerDoc: '', x: 160, y: 740, wires: [[LAB]] },
    { id: LAB, type: 'function', z: TAB, name: 'WAVE LAB STORE',
        func: '// Sólo lectura: guarda el detalle de EVAL / las muestras y atiende las rutas /wave-lab/*.\n' +
              'globalThis.__WAVE_LAB__.onMsg(msg);\nreturn null;',
        outputs: 2, timeout: 0, noerr: 0, initialize: labSrc,
        finalize: 'if (globalThis.__WAVE_LAB__) globalThis.__WAVE_LAB__.stop();', libs: [],
        outputLabels: ['http response', 'reset / dump -> EVAL'],
        x: 420, y: 700, wires: [[LAB_RES_JSON], [EVAL]] },
    { id: LAB_RES_JSON, type: 'http response', z: TAB, name: 'JSON', statusCode: '', headers: {}, x: 640, y: 700, wires: [] }
];

// snippet separado: sin 'z' => el editor lo coloca en el tab ACTIVO al importar con "current flow"
const tapNodes = [
    { id: TAP, type: 'link out', name: 'TAP → WAVE_TEST', mode: 'link', links: [LIN], x: 1030, y: 420, wires: [] }
];

fs.writeFileSync(path.join(outDir, 'WAVE_TEST_V1.json'), JSON.stringify(testNodes, null, 4) + '\n');
fs.writeFileSync(path.join(outDir, 'WAVE_TAP_link_out.json'), JSON.stringify(tapNodes, null, 4) + '\n');
console.log('OK ->', path.join(outDir, 'WAVE_TEST_V1.json'), '(' + testNodes.length + ' nodos) +', path.join(outDir, 'WAVE_TAP_link_out.json'), '(1 nodo)');
