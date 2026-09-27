#!/usr/bin/env node
// Versión para IMPORTAR: solapa, página, grupos y nodos con IDs nuevos (no chocan con LIVE / FLOW anteriores).
// Los nodos de configuración compartidos (dashboard ui-base, tema, conexiones websocket) conservan su ID
// y su contenido ORIGINAL: en una instalación limpia se crean; si ya existen se reutilizan, así no hay
// un segundo dashboard (Dashboard 2 sirve uno solo) ni conexiones duplicadas a los exchanges.
// Uso: node node-red/build-flow-tab.js && node node-red/build-single-tab.js && node node-red/build-import.js
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const dir = __dirname;
const src = fs.readFileSync(path.join(dir, 'btc-zec-todo.flow.json'), 'utf8');
const nodes = JSON.parse(src);
const original = JSON.parse(fs.readFileSync(path.join(dir, 'btc-zec-live.flow.json'), 'utf8'));
const SHARED = new Set(['ui-base', 'ui-theme', 'websocket-client']);

// IDs deterministas derivados del ID original: el archivo no cambia entre compilaciones.
const map = {};
for (const n of nodes) if (!SHARED.has(n.type)) map[n.id] = crypto.createHash('sha1').update('btc-zec-import:' + n.id).digest('hex').slice(0, 16);
let text = src;
for (const [a, b] of Object.entries(map)) text = text.split(a).join(b);   // incluye IDs dentro del código (WS_NODES)
const out = JSON.parse(text);

for (let i = 0; i < out.length; i++) {
    if (SHARED.has(out[i].type)) out[i] = original.find(o => o.id === out[i].id);   // contenido idéntico al original
    if (out[i].type === 'ui-page') { out[i].path = '/todo'; out[i].name = 'BTC + ZEC · LIVE + FLOW'; }
}
// Validación.
const ids = new Set(out.map(n => n.id));
if (ids.size !== out.length) throw Error('IDs duplicados');
for (const n of out) {
    for (const w of (n.wires || []).flat()) if (!ids.has(w)) throw Error('Wire roto ' + n.id);
    for (const k of ['z', 'group', 'page', 'ui', 'client', 'theme']) if (n[k] && !ids.has(n[k])) throw Error('Ref rota ' + n.id + '.' + k);
    if (Object.keys(map).some(old => JSON.stringify(n).includes(old))) throw Error('Quedó un ID viejo en ' + n.id);
}
fs.writeFileSync(path.join(dir, 'BTC-ZEC-IMPORTAR.json'), JSON.stringify(out));
console.log('BTC-ZEC-IMPORTAR.json · ' + out.length + ' nodos · dashboard /btc-zec-live/todo');
