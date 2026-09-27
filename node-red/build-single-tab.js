#!/usr/bin/env node
// Une LIVE + FLOW en UNA solapa de Node-RED y UNA página de dashboard.
// Entrada: btc-zec-live.flow.json (generado por build-flow-tab.js). Salida: btc-zec-todo.flow.json
// Uso: node node-red/build-flow-tab.js && node node-red/build-single-tab.js
const fs = require('fs');
const path = require('path');

const dir = __dirname;
const src = JSON.parse(fs.readFileSync(path.join(dir, 'btc-zec-live.flow.json'), 'utf8'));

const LIVE_TAB = 'ead59d1fb172c92a', FLOW_TAB = 'bf10000000000001';
const LIVE_PAGE = '1f74d393c0f09613', FLOW_PAGE = 'bf10000000000002';
const TITLE = 'BTC + ZEC · LIVE + FLOW';
// Orden en el dashboard: por activo, primero el precio multi-exchange y debajo el flujo agresivo.
const GROUP_ORDER = {
    '43ac40e76e01c418': 1,   // BTC · LIVE (Binance + Coinbase)
    'bf10000000000003': 2,   // BTC · FLOW
    'f10ac727ac1a99c4': 3,   // ZEC · LIVE (Binance + GMX)
    'bf10000000000004': 4    // ZEC · FLOW
};
const FLOW_Y_OFFSET = 1340;  // coloca los nodos FLOW debajo de los de LIVE en el editor

const out = [];
for (const n0 of src) {
    if (n0.id === FLOW_TAB || n0.id === FLOW_PAGE) continue;
    const n = JSON.parse(JSON.stringify(n0));
    if (n.z === FLOW_TAB) { n.z = LIVE_TAB; n.y += FLOW_Y_OFFSET; }
    if (n.type === 'ui-group' && n.page === FLOW_PAGE) n.page = LIVE_PAGE;
    if (n.id in GROUP_ORDER) n.order = GROUP_ORDER[n.id];
    if (n.id === LIVE_TAB) {
        n.label = TITLE;
        n.info = 'FLOW COMPLETO AUTÓNOMO, una sola solapa y una sola página de dashboard (/btc-zec-live/live).\n' +
            'LIVE: precio por trade, BTC Binance + Coinbase, ZEC Binance + GMX (oráculo), 2 min.\n' +
            'FLOW: compra agresiva vs venta agresiva por segundo, BTCUSDT y ZECUSDT Binance, reutilizando las mismas conexiones WebSocket.\n' +
            'Retención en RAM, sin indicadores, señales ni almacenamiento permanente. Requiere @flowfuse/node-red-dashboard.';
    }
    if (n.id === LIVE_PAGE) n.name = TITLE;
    if (n.id === 'b12dc5781b4d9e20') n.name = TITLE;
    out.push(n);
}

// Validación: una solapa, una página, sin referencias rotas.
const ids = new Set(out.map(n => n.id));
const tabs = out.filter(n => n.type === 'tab'), pages = out.filter(n => n.type === 'ui-page');
if (tabs.length !== 1 || pages.length !== 1) throw Error('Se esperaba 1 solapa y 1 página');
for (const n of out) {
    if (n.z && n.z !== LIVE_TAB) throw Error('Nodo fuera de la solapa: ' + n.id);
    for (const w of (n.wires || []).flat()) if (!ids.has(w)) throw Error('Wire roto: ' + n.id + ' → ' + w);
    for (const k of ['group', 'page', 'ui', 'client', 'theme']) if (n[k] && !ids.has(n[k])) throw Error('Referencia rota: ' + n.id + '.' + k);
    for (const s of n.scope || []) if (!ids.has(s)) throw Error('Scope roto: ' + n.id);
}
fs.writeFileSync(path.join(dir, 'btc-zec-todo.flow.json'), JSON.stringify(out, null, 2) + '\n');
console.log('btc-zec-todo.flow.json · ' + out.length + ' nodos · 1 solapa · 1 página');
