// Regenera prrr_cx_engine.json a partir de func.js + initialize.js
const fs = require('fs');
const f = __dirname + '/prrr_cx_engine.json';
const nodes = JSON.parse(fs.readFileSync(f, 'utf8'));
const n = nodes.find(x => x.id === 'prrr_cx_engine');
n.name = 'CONCEPTITO_MOVEMENT_V1 motor (GMX MID 1s ZEC · SMA35/70 · CD32 · MOV30≥66.04bps · paper 1 pos · persiste)';
n.func = fs.readFileSync(__dirname + '/func.js', 'utf8').replace(/\n$/, '');
n.initialize = fs.readFileSync(__dirname + '/initialize.js', 'utf8').replace(/\n$/, '');
fs.writeFileSync(f, JSON.stringify(nodes, null, 4) + '\n');
console.log('ok', n.name);
