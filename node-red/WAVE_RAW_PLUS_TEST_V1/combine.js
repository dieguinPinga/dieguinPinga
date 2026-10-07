#!/usr/bin/env node
// Une WAVE_RAW_V1.json + WAVE_TEST_V1.json + el link out TAP → WAVE_TEST en un solo JSON importable.
// Única modificación sobre WAVE_RAW_V1: wires[0] de los 4 NORMALIZER agrega el TAP (además del ENGINE).
'use strict';
const fs = require('fs');
const path = require('path');
const R = path.join(__dirname, '..');
const raw = JSON.parse(fs.readFileSync(path.join(R, 'WAVE_RAW_V1', 'WAVE_RAW_V1.json'), 'utf8'));
const test = JSON.parse(fs.readFileSync(path.join(R, 'WAVE_TEST_V1', 'WAVE_TEST_V1.json'), 'utf8'));
const tap = JSON.parse(fs.readFileSync(path.join(R, 'WAVE_TEST_V1', 'WAVE_TAP_link_out.json'), 'utf8'))[0];
const RAW_TAB = '35b7a74832889057';
const NORMS = ['750b61939c35bf48', '4ef238d4e8163455', 'e784b0aa97209004', 'bc40f0ca0ddf2a65'];
if (!raw.some((n) => n.id === RAW_TAB && n.type === 'tab')) throw new Error('tab WAVE_RAW_V1 no encontrado');
const out = raw.map((n) => JSON.parse(JSON.stringify(n)));
for (const id of NORMS) {
    const n = out.find((x) => x.id === id);
    if (!n || !/^NORMALIZER · /.test(n.name)) throw new Error('NORMALIZER no encontrado: ' + id);
    n.wires[0] = n.wires[0].concat([tap.id]);
}
tap.z = RAW_TAB;
tap.x = 1030; tap.y = 440;
out.push(tap);
const final = out.concat(test);
fs.writeFileSync(path.join(__dirname, 'WAVE_RAW_PLUS_TEST_V1.json'), JSON.stringify(final, null, 4) + '\n');
console.log('OK ->', final.length, 'nodos');
