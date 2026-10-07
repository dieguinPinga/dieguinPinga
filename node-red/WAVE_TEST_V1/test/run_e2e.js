#!/usr/bin/env node
// E2E: Node-RED real con WAVE_RAW_V1 (URLs a mocks) + WAVE_TEST_V1 + tap.
// Los 4 cables NORMALIZER(salida 1) -> TAP se agregan aquí por código, simulando el paso manual.
// Uso: node test/run_e2e.js <dir-node-red> <segundos> [plant] [notap]
'use strict';
const fs = require('fs');
const path = require('path');
const os = require('os');
const { spawn, execFileSync } = require('child_process');

const NR_DIR = path.resolve(process.argv[2]);
const SECS = +(process.argv[3] || 60);
const PLANT = process.argv.includes('plant');
const NOTAP = process.argv.includes('notap');
const RAW = path.join(__dirname, '..', '..', 'WAVE_RAW_V1');
const PORT = 18100;
const work = fs.mkdtempSync(path.join(os.tmpdir(), 'wave-test-e2e-'));
const userDir = path.join(work, 'nr');
fs.mkdirSync(userDir);

const urls = {
    binance: { trade: `ws://127.0.0.1:${PORT + 1}/stream?streams=btcusdt@trade`, book: `ws://127.0.0.1:${PORT + 1}/stream?streams=btcusdt@bookTicker/btcusdt@depth10@100ms` },
    coinbase: { trade: `ws://127.0.0.1:${PORT + 2}`, book: `ws://127.0.0.1:${PORT + 2}` },
    kraken: { trade: `ws://127.0.0.1:${PORT + 3}/v2`, book: `ws://127.0.0.1:${PORT + 3}/v2` },
    okx: { trade: `ws://127.0.0.1:${PORT + 4}/ws/v5/business`, book: `ws://127.0.0.1:${PORT + 4}/ws/v5/public` }
};
fs.writeFileSync(path.join(work, 'urls.json'), JSON.stringify(urls));
execFileSync('node', [path.join(RAW, 'build.js'), '--urls', path.join(work, 'urls.json'), '--out', path.join(work, 'raw.json')], { stdio: 'inherit' });
execFileSync('node', [path.join(__dirname, '..', 'build.js'), work], { stdio: 'inherit' });

const raw = JSON.parse(fs.readFileSync(path.join(work, 'raw.json'), 'utf8'));
const test = JSON.parse(fs.readFileSync(path.join(work, 'WAVE_TEST_V1.json'), 'utf8'));
const tap = JSON.parse(fs.readFileSync(path.join(work, 'WAVE_TAP_link_out.json'), 'utf8'))[0];
const rawTab = raw.find((n) => n.type === 'tab').id;
const testTab = test.find((n) => n.type === 'tab').id;
let flow = raw.slice();
if (!NOTAP) {
    tap.z = rawTab;                                         // = importar con "current flow" estando en WAVE_RAW_V1
    flow.push(tap);
    for (const n of raw.filter((x) => /^NORMALIZER · /.test(x.name))) n.wires[0].push(tap.id);   // = los 4 cables manuales
    flow = flow.concat(test);
}
// captura para la prueba
const engine = raw.find((n) => n.name === 'WAVE RAW ENGINE');
flow.push({ id: 'e2ecapraw0000001', type: 'function', z: rawTab, name: 'CAP RAW', outputs: 0, libs: [],
    func: 'node.log("E2E_RAW " + JSON.stringify(msg.payload.node)); return null;', wires: [] });
engine.wires[0].push('e2ecapraw0000001');
if (!NOTAP) {
    const ev = test.find((n) => n.name === 'WAVE TEST EVAL');
    flow.push({ id: 'e2ecaptest000001', type: 'function', z: testTab, name: 'CAP TEST', outputs: 0, libs: [],
        func: 'node.log("E2E_TEST_" + (msg.topic.endsWith("line") ? "LINE " + msg.payload : "DETAIL " + JSON.stringify(msg.payload))); return null;', wires: [] });
    ev.wires[0].push('e2ecaptest000001');
    ev.wires[1].push('e2ecaptest000001');
}
fs.writeFileSync(path.join(userDir, 'flows.json'), JSON.stringify(flow));
fs.writeFileSync(path.join(userDir, 'settings.js'), `module.exports = { uiPort: 18882, uiHost: '127.0.0.1', flowFile: 'flows.json',
  credentialSecret: false, functionExternalModules: true, httpAdminRoot: false, httpNodeRoot: false,
  logging: { console: { level: 'info', metrics: false, audit: false } } };`);

const mock = spawn('node', [path.join(RAW, 'test', 'mock_exchanges.js'), path.join(NR_DIR, 'node_modules', 'ws'), String(PORT)],
    { stdio: ['ignore', 'pipe', 'inherit'], env: Object.assign({}, process.env, { PLANT: PLANT ? '1' : '0' }) });
mock.stdout.on('data', () => {});
const out = [];
setTimeout(() => {
    const nr = spawn('node', [path.join(NR_DIR, 'node_modules', 'node-red', 'red.js'), '-u', userDir, '-s', path.join(userDir, 'settings.js')], { stdio: ['ignore', 'pipe', 'pipe'] });
    let buf = '';
    const onData = (d) => {
        buf += d.toString();
        let i;
        while ((i = buf.indexOf('\n')) >= 0) {
            const line = buf.slice(0, i); buf = buf.slice(i + 1);
            out.push(line);
            if (/E2E_TEST_LINE/.test(line)) console.log(line.replace(/^.*E2E_TEST_LINE /, ''));
            else if (/\[(warn|error)\]/.test(line) && !/Projects disabled|unencrypted/.test(line)) console.log('[nr]', line);
        }
    };
    nr.stdout.on('data', onData);
    nr.stderr.on('data', onData);
    setTimeout(() => {
        nr.kill('SIGINT');
        setTimeout(() => {
            mock.kill();
            fs.writeFileSync(path.join(work, 'out.log'), out.join('\n'));
            console.log('E2E_OUTPUT_FILE=' + path.join(work, 'out.log'));
            process.exit(0);
        }, 3000);
    }, SECS * 1000);
}, 500);
