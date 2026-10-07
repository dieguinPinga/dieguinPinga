#!/usr/bin/env node
// Prueba end-to-end: Node-RED real + exchanges simulados con fallos inyectados.
// Uso: node test/run_e2e.js <dir-con-node-red-instalado> [segundos]
'use strict';
const fs = require('fs');
const path = require('path');
const os = require('os');
const { spawn, execFileSync } = require('child_process');

const NR_DIR = path.resolve(process.argv[2]);
const SECS = +(process.argv[3] || 50);
const ROOT = path.join(__dirname, '..');
const PORT = 18100;
const work = fs.mkdtempSync(path.join(os.tmpdir(), 'wave-e2e-'));
const userDir = path.join(work, 'nr');
fs.mkdirSync(userDir);

// 1) flow apuntando a los mocks (sólo cambia la URL; el resto es idéntico al JSON entregado)
const urls = {
    binance: { trade: `ws://127.0.0.1:${PORT + 1}/stream?streams=btcusdt@trade`, book: `ws://127.0.0.1:${PORT + 1}/stream?streams=btcusdt@bookTicker/btcusdt@depth10@100ms` },
    coinbase: { trade: `ws://127.0.0.1:${PORT + 2}`, book: `ws://127.0.0.1:${PORT + 2}` },
    kraken: { trade: `ws://127.0.0.1:${PORT + 3}/v2`, book: `ws://127.0.0.1:${PORT + 3}/v2` },
    okx: { trade: `ws://127.0.0.1:${PORT + 4}/ws/v5/business`, book: `ws://127.0.0.1:${PORT + 4}/ws/v5/public` }
};
fs.writeFileSync(path.join(work, 'urls.json'), JSON.stringify(urls));
const flowFile = path.join(userDir, 'flows.json');
execFileSync('node', [path.join(ROOT, 'build.js'), '--urls', path.join(work, 'urls.json'), '--out', flowFile], { stdio: 'inherit' });

// 2) nodo de captura SÓLO para la prueba (registra el PULSE/TELEMETRY por consola)
const flow = JSON.parse(fs.readFileSync(flowFile, 'utf8'));
const tab = flow.find((n) => n.type === 'tab').id;
const engine = flow.find((n) => n.name === 'WAVE RAW ENGINE');
flow.push({ id: 'e2ecapture000001', type: 'function', z: tab, name: 'E2E CAPTURE', outputs: 0, libs: [],
    func: 'node.log("E2E " + msg.topic + " " + JSON.stringify(msg.payload)); return null;', x: 1300, y: 400, wires: [] });
engine.wires[0].push('e2ecapture000001');
engine.wires[1].push('e2ecapture000001');
fs.writeFileSync(flowFile, JSON.stringify(flow));

fs.writeFileSync(path.join(userDir, 'settings.js'), `module.exports = {
  uiPort: 18880, uiHost: '127.0.0.1', flowFile: 'flows.json', credentialSecret: false,
  functionExternalModules: true, httpAdminRoot: false, httpNodeRoot: false,
  logging: { console: { level: 'info', metrics: false, audit: false } }
};`);

const mock = spawn('node', [path.join(__dirname, 'mock_exchanges.js'), path.join(NR_DIR, 'node_modules', 'ws'), String(PORT)], { stdio: ['ignore', 'pipe', 'pipe'] });
const mockLog = [];
mock.stdout.on('data', (d) => { mockLog.push(d.toString()); process.stdout.write(d); });
mock.stderr.on('data', (d) => process.stderr.write(d));

const out = [];
setTimeout(() => {
    const nr = spawn('node', [path.join(NR_DIR, 'node_modules', 'node-red', 'red.js'), '-u', userDir, '-s', path.join(userDir, 'settings.js')], { stdio: ['ignore', 'pipe', 'pipe'] });
    let buf = '';
    const onData = (d) => {
        buf += d.toString();
        let i;
        while ((i = buf.indexOf('\n')) >= 0) {
            const line = buf.slice(0, i);
            buf = buf.slice(i + 1);
            out.push(line);
            if (!line.includes('E2E wave_raw_v1/')) console.log('[nr]', line.slice(0, 300));
        }
    };
    nr.stdout.on('data', onData);
    nr.stderr.on('data', onData);
    setTimeout(() => {
        nr.kill('SIGINT');
        setTimeout(() => {
            mock.kill();
            const res = path.join(work, 'nr-output.log');
            fs.writeFileSync(res, out.join('\n'));
            console.log('\nE2E_OUTPUT_FILE=' + res);
            process.exit(0);
        }, 3000);
    }, SECS * 1000);
}, 500);
