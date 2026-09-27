#!/usr/bin/env node
// Genera la solapa "BTC + ZEC · FLOW" a partir de src/flow/ y la agrega al flow completo.
// Los nodos de la solapa LIVE no se modifican: sólo se reemplazan los nodos propios de FLOW.
// Uso: node node-red/build-flow-tab.js
const fs = require('fs');
const path = require('path');

const dir = __dirname;
const FULL = path.join(dir, 'btc-zec-live.flow.json');
const TAB_ONLY = path.join(dir, 'btc-zec-flow.tab.json');

// Nodos de configuración existentes que se reutilizan (no se duplican).
const UI_BASE = 'b12dc5781b4d9e20';
const THEME = 'a17c8d1199aa6270';
const WS_BTC = 'e4354bbccf441970';   // wss://stream.binance.com:9443/ws/btcusdt@trade
const WS_ZEC = '04eab09e2202cc64';   // wss://stream.binance.com:9443/ws/zecusdt@trade

const id = n => 'bf10' + String(n).padStart(12, '0');
const ID = {
    tab: id(1), page: id(2), groupBtc: id(3), groupZec: id(4),
    inBtc: id(5), inZec: id(6), status: id(7), acc: id(8), tick: id(9),
    tplBtc: id(10), tplZec: id(11), debug: id(12), comment: id(13)
};

const accumulator = fs.readFileSync(path.join(dir, 'src/flow/accumulator.js'), 'utf8')
    .replace('__WS_NODES__', JSON.stringify({ [ID.inBtc]: 'BTCUSDT', [ID.inZec]: 'ZECUSDT' }));
const template = fs.readFileSync(path.join(dir, 'src/flow/template.vue'), 'utf8');
const tpl = v => template
    .replaceAll('__TITLE__', v.title).replaceAll('__SYMBOL__', v.symbol)
    .replaceAll('__ASSET__', v.asset).replaceAll('__COLOR__', v.color);

const z = ID.tab;
const nodes = [
    {
        id: ID.tab, type: 'tab', label: 'BTC + ZEC · FLOW', disabled: false,
        info: 'Presión de COMPRA AGRESIVA vs VENTA AGRESIVA por segundo, BTCUSDT y ZECUSDT de Binance. ' +
            'Reutiliza las conexiones WebSocket de la solapa LIVE (mismos nodos de configuración): no abre conexiones nuevas. ' +
            'Cada trade entra al acumulador; el bucket de 1 s es una agregación posterior. Sin indicadores, señales ni almacenamiento permanente.'
    },
    {
        id: ID.page, type: 'ui-page', name: 'BTC + ZEC · FLOW', ui: UI_BASE, path: '/flow', icon: 'stacked_bar_chart',
        layout: 'grid', theme: THEME, order: 2, className: '', visible: 'true', disabled: 'false',
        breakpoints: [
            { name: 'Default', px: 0, cols: 3 },
            { name: 'Tablet', px: 576, cols: 6 },
            { name: 'Desktop', px: 1024, cols: 12 }
        ]
    },
    {
        id: ID.groupBtc, type: 'ui-group', name: 'BTC FLOW', page: ID.page, width: 12, height: 1, order: 1,
        showTitle: false, className: '', visible: true, disabled: false, groupType: 'default'
    },
    {
        id: ID.groupZec, type: 'ui-group', name: 'ZEC FLOW', page: ID.page, width: 12, height: 1, order: 2,
        showTitle: false, className: '', visible: true, disabled: false, groupType: 'default'
    },
    {
        id: ID.comment, type: 'comment', z, name: 'FLOW · compra agresiva vs venta agresiva · /btc-zec-live/flow',
        info: 'm = false → comprador taker → BUY agresivo. m = true → vendedor taker → SELL agresivo.\n' +
            'notional_usd = price * quantity (cotizado en USDT).\n' +
            'Los websocket-in de esta solapa usan los MISMOS nodos de configuración websocket-client que LIVE: ' +
            'Node-RED mantiene una sola conexión por cliente y entrega cada mensaje a todos sus websocket-in.\n' +
            'El Debug "FLOW · bucket 1 s" está desactivado: activarlo para ver cada bucket terminado.',
        x: 520, y: 60, wires: []
    },
    {
        id: ID.inBtc, type: 'websocket in', z, name: 'BTC · Binance @trade (conexión de LIVE)',
        server: '', client: WS_BTC, x: 210, y: 140, wires: [[ID.acc]]
    },
    {
        id: ID.inZec, type: 'websocket in', z, name: 'ZEC · Binance @trade (conexión de LIVE)',
        server: '', client: WS_ZEC, x: 210, y: 200, wires: [[ID.acc]]
    },
    {
        id: ID.status, type: 'status', z, name: 'FLOW · Estado conexión', scope: [ID.inBtc, ID.inZec],
        x: 180, y: 260, wires: [[ID.acc]]
    },
    {
        id: ID.tick, type: 'inject', z, name: 'FLOW · Cerrar segundos (reloj 250 ms)',
        props: [{ p: 'topic', vt: 'str' }], repeat: '0.25', crontab: '', once: true, onceDelay: 0.1,
        topic: 'flow:tick', x: 210, y: 320, wires: [[ID.acc]]
    },
    {
        id: ID.acc, type: 'function', z, name: 'FLOW · Acumulador agresivo 1 s (BTC + ZEC)',
        func: accumulator, outputs: 3, timeout: 0, noerr: 0,
        initialize: 'globalThis.__bzFlow = null;', finalize: 'globalThis.__bzFlow = null;',
        libs: [], x: 560, y: 200, wires: [[ID.tplBtc], [ID.tplZec], [ID.debug]]
    },
    {
        id: ID.tplBtc, type: 'ui-template', z, group: ID.groupBtc, page: '', ui: '',
        name: 'BTC · PRICE + AGGRESSIVE FLOW · 2 MIN', order: 1, width: 12, height: 13, head: '',
        format: tpl({ title: 'BTC · BINANCE BTC/USDT · PRECIO vs FLUJO AGRESIVO · 2 MIN', symbol: 'BTCUSDT', asset: 'BTC', color: '#f5b544' }),
        storeOutMessages: false, passthru: false, resendOnRefresh: false, templateScope: 'local', className: '',
        x: 930, y: 140, wires: [[ID.acc]]
    },
    {
        id: ID.tplZec, type: 'ui-template', z, group: ID.groupZec, page: '', ui: '',
        name: 'ZEC · PRICE + AGGRESSIVE FLOW · 2 MIN', order: 1, width: 12, height: 13, head: '',
        format: tpl({ title: 'ZEC · BINANCE ZEC/USDT · PRECIO vs FLUJO AGRESIVO · 2 MIN', symbol: 'ZECUSDT', asset: 'ZEC', color: '#55c6be' }),
        storeOutMessages: false, passthru: false, resendOnRefresh: false, templateScope: 'local', className: '',
        x: 930, y: 220, wires: [[ID.acc]]
    },
    {
        id: ID.debug, type: 'debug', z, name: 'FLOW · bucket 1 s (auditoría)', active: false,
        tosidebar: true, console: false, tostatus: false, complete: 'payload', targetType: 'msg',
        statusVal: '', statusType: 'auto', x: 910, y: 300, wires: []
    }
];

const own = new Set(Object.values(ID));
const base = JSON.parse(fs.readFileSync(FULL, 'utf8')).filter(n => !own.has(n.id) && n.z !== ID.tab);
fs.writeFileSync(FULL, JSON.stringify([...base, ...nodes], null, 2) + '\n');
fs.writeFileSync(TAB_ONLY, JSON.stringify(nodes, null, 2) + '\n');
console.log('LIVE: ' + base.length + ' nodos intactos · FLOW: ' + nodes.length + ' nodos');
