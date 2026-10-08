// =====================================================================
// WAVE_TEST_V1 · WAVE LAB STORE  (capa de visualización, sólo lectura)
// ---------------------------------------------------------------------
// NO calcula señales ni estadísticas: guarda en memoria lo que WAVE TEST EVAL
// ya publica y lo sirve por HTTP a la página WAVE LAB.
// Entradas:
//   - detalle de EVAL (salida 2, cada 5 s)          topic 'wave_test_v1/detail'
//   - muestras (DUMP) reenviadas por WAVE TEST EXPORT topic 'wave_test_v1/samples'
//   - HTTP: GET /wave-lab/data · POST /wave-lab/reset · GET /wave-lab/export
// Salidas: 1 -> http response · 2 -> WAVE TEST EVAL ({topic:'reset'} / {topic:'dump'})
// Los DUMP sólo se piden mientras alguien mira la página (poll < 20 s) cada 10 s,
// o al pedir EXPORT DATA. Nada de esto toca la ruta de eventos de alta frecuencia.
// =====================================================================
const DUMP_EVERY_MS = 10000;      // refresco de "¿llegamos tarde?" mientras la página está abierta
const VIEW_TTL_MS = 20000;        // la página se considera abierta si pidió datos hace < 20 s
const EXPORT_TIMEOUT_MS = 6000;
const N_EXPORT = 500;             // últimas muestras completas en EXPORT DATA
const N_LATE = 300;               // muestras direccionales cerradas para el perfil promedio
const N_ROWS = 15;                // filas de la tabla "¿llegamos tarde?"
const HEALTH_WINDOW_MS = 60000;   // crecimiento de contadores de salud en el último minuto

const L = {
    detail: null, detailAt: 0,
    late: null, lastDumpReq: 0, lastPoll: 0,
    exportWaiters: [],
    health: []                    // [{t, v:{...}}] últimos ~2 min
};

const H_KEYS = ['late_events', 'pending_dropped', 'horizons_expired', 'invalid_refs', 'n_no_ref'];

function healthValues(d) {
    const c = d.counts, q = d.queue;
    let inv = 0;
    for (const k in c.invalid_ref_by_h) inv += c.invalid_ref_by_h[k];
    for (const k in c.invalid_secondary_by_h) inv += c.invalid_secondary_by_h[k];
    return { late_events: q.late_events, pending_dropped: q.pending_dropped, horizons_expired: q.horizons_expired, invalid_refs: inv, n_no_ref: c.n_no_ref };
}

function onDetail(d) {
    L.detail = d;
    L.detailAt = Date.now();
    const v = healthValues(d);
    // un RESET de EVAL reinicia contadores: la historia de crecimiento también
    const prev = L.health.length ? L.health[L.health.length - 1].v : null;
    if (prev && H_KEYS.some(function (k) { return v[k] < prev[k]; })) L.health = [];
    L.health.push({ t: L.detailAt, v: v });
    while (L.health.length && L.detailAt - L.health[0].t > 2 * HEALTH_WINDOW_MS) L.health.shift();
}

function healthGrowth() {
    if (!L.health.length) return null;
    const last = L.health[L.health.length - 1];
    let base = L.health[0];
    for (const h of L.health) { if (last.t - h.t >= HEALTH_WINDOW_MS) base = h; else break; }
    const o = {};
    for (const k of H_KEYS) o[k] = { value: last.v[k], delta_60s: last.v[k] - base.v[k] };
    return o;
}

// ---------- "¿llegamos tarde?": perfil promedio del mid de Binance alrededor de t0 ----------
// pre_move.binance_mid_move_Xms_bps = (mid(t0)/mid(t0-X) - 1)·1e4  =>  mid(t0-X) relativo a t0 = -pre
// todo firmado por la dirección de WAVE: > 0 = a favor de la señal
const PRE = [[1000, -1000], [500, -500], [250, -250], [100, -100]];
const POST = [['100ms', 100], ['250ms', 250], ['500ms', 500], ['1s', 1000], ['2s', 2000]];

function mean(a) { return a.length ? a.reduce(function (s, x) { return s + x; }, 0) / a.length : null; }
function r3(x) { return x === null || !isFinite(x) ? null : Math.round(x * 1000) / 1000; }

function profile(rows) {
    const pts = [];
    for (const p of PRE) {
        const v = rows.map(function (s) { const m = s.pre_move['binance_mid_move_' + p[0] + 'ms_bps']; return m === null || m === undefined ? null : -m * s.sign; })
            .filter(function (x) { return x !== null; });
        pts.push({ t: p[1], mean: r3(mean(v)), n: v.length });
    }
    pts.push({ t: 0, mean: 0, n: rows.length });
    for (const p of POST) {
        const v = rows.map(function (s) { const f = s.future[p[0]]; return f && f.signed_bps !== null && f.signed_bps !== undefined ? f.signed_bps : null; })
            .filter(function (x) { return x !== null; });
        pts.push({ t: p[1], mean: r3(mean(v)), n: v.length });
    }
    return pts;
}

function onSamples(samples) {
    const now = Date.now();
    if (L.exportWaiters.length) {
        const ws = L.exportWaiters;
        L.exportWaiters = [];
        for (const w of ws) { clearTimeout(w.timer); respondExport(w.msg, samples); }
    }
    const done = samples.filter(function (s) {
        return s.sign !== 0 && s.p0 !== null && s.future && s.future['1s'] && s.future['1s'].signed_bps !== null && s.future['1s'].signed_bps !== undefined;
    }).slice(-N_LATE);
    const strong = done.filter(function (s) { return s.tercile_provisional === 'strong'; });
    const sgn = function (s, w) { const m = s.pre_move['binance_mid_move_' + w + 'ms_bps']; return m === null || m === undefined ? null : r3(m * s.sign); };
    const fut = function (s, k) { const f = s.future[k]; return f ? f.signed_bps : null; };
    L.late = {
        updated_at: now, n: done.length, n_strong: strong.length,
        path_all: profile(done), path_strong: profile(strong),
        rows: done.slice(-N_ROWS).reverse().map(function (s) {
            return { t0: s.t0, direction: s.direction, strength_abs_usd: s.strength_abs_usd, breadth: s.breadth,
                pre100: sgn(s, 100), pre250: sgn(s, 250), pre500: sgn(s, 500),
                post250: fut(s, '250ms'), post500: fut(s, '500ms'), post1s: fut(s, '1s') };
        })
    };
}

function requestDump() {
    L.lastDumpReq = Date.now();
    node.send([null, { topic: 'dump' }], false);
}

// ---------- HTTP ----------
function sendJson(msg, code, obj, extraHeaders) {
    msg.statusCode = code;
    msg.headers = Object.assign({ 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' }, extraHeaders || {});
    msg.payload = typeof obj === 'string' ? obj : JSON.stringify(obj);
    node.send([msg, null], false);
}

function currentState(d) {
    const s = d && d.last_samples && d.last_samples.length ? d.last_samples[d.last_samples.length - 1] : null;
    if (!s) return null;
    const b = s.exchanges && s.exchanges.binance;
    return {
        t0: s.t0, direction: s.direction, sign: s.sign,
        strength_abs_usd: s.strength_abs_usd, delta_usd_250ms: s.delta_usd_250ms,
        buy_usd_250ms: s.buy_usd_250ms, sell_usd_250ms: s.sell_usd_250ms, flow_imbalance_250ms: s.flow_imbalance_250ms,
        breadth: s.breadth, active_exchanges: s.active_exchanges,
        mid_binance: s.p0 !== null ? s.p0 : (b ? b.mid : null),
        spread_bps_binance: s.spread_bps_binance,
        imb1_binance: s.book_imbalance ? s.book_imbalance.imb1_binance : null,
        imb10_binance: s.book_imbalance ? s.book_imbalance.imb10_binance : null
    };
}

function onData(msg) {
    const now = Date.now();
    L.lastPoll = now;
    if (now - L.lastDumpReq >= DUMP_EVERY_MS) requestDump();
    const d = L.detail;
    if (!d) { sendJson(msg, 200, { ok: false, reason: 'esperando el primer reporte de WAVE TEST EVAL (<= 5 s)', server_ts: now }); return; }
    sendJson(msg, 200, {
        ok: true, server_ts: now, detail_ts: d.ts, detail_age_ms: now - L.detailAt, uptime_s: d.uptime_s,
        current: currentState(d),
        config: d.config, counts: d.counts, queue: d.queue, strength: d.strength,
        primary: d.primary_binance_mid,
        health: healthGrowth(),
        late: L.late
    });
}

function onReset(msg) {
    // exactamente el mismo mensaje que el inject RESET TEST
    node.send([null, { topic: 'reset' }], false);
    L.health = []; L.late = null;
    sendJson(msg, 200, { ok: true, reset_at: Date.now() });
}

const pad = function (n) { return (n < 10 ? '0' : '') + n; };
function stamp(t) { const d = new Date(t); return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + '_' + pad(d.getHours()) + '-' + pad(d.getMinutes()) + '-' + pad(d.getSeconds()); }

function respondExport(msg, samples) {
    const d = L.detail;
    if (!d) { sendJson(msg, 503, { ok: false, reason: 'todavía no hay reporte de WAVE TEST EVAL' }); return; }
    const now = Date.now();
    const last = samples.slice(-N_EXPORT);
    const body = {
        export_version: 1, source: 'WAVE LAB (descarga desde navegador)',
        generated_at: new Date(now).toISOString(), uptime_s: d.uptime_s, detail_ts: d.ts, detail_iso: d.iso,
        config: d.config, counts: d.counts, queue: d.queue, strength: d.strength,
        primary_binance_mid: d.primary_binance_mid, secondary_median_mid: d.secondary_median_mid,
        baseline_random_direction: d.baseline_random_direction,
        samples_meta: { n: last.length, max: N_EXPORT, first_t0: last.length ? last[0].t0 : null, last_t0: last.length ? last[last.length - 1].t0 : null },
        samples: last
    };
    sendJson(msg, 200, body, { 'Content-Disposition': 'attachment; filename="WAVE_TEST_' + stamp(now) + '.json"' });
}

function onExport(msg) {
    const w = { msg: msg };
    w.timer = setTimeout(function () {
        L.exportWaiters = L.exportWaiters.filter(function (x) { return x !== w; });
        sendJson(msg, 504, { ok: false, reason: 'timeout esperando muestras de WAVE TEST EVAL' });
    }, EXPORT_TIMEOUT_MS);
    L.exportWaiters.push(w);
    requestDump();
}

const timer = setInterval(function () {
    if (Date.now() - L.lastPoll < VIEW_TTL_MS && Date.now() - L.lastDumpReq >= DUMP_EVERY_MS) requestDump();
}, 2000);

globalThis.__WAVE_LAB__ = {
    onMsg: function (msg) {
        if (msg.req && msg.res) {
            const p = (msg.req.path || '').replace(/\/+$/, '');
            const m = (msg.req.method || 'GET').toUpperCase();
            if (p.endsWith('/wave-lab/data') && m === 'GET') onData(msg);
            else if (p.endsWith('/wave-lab/reset') && m === 'POST') onReset(msg);
            else if (p.endsWith('/wave-lab/export') && m === 'GET') onExport(msg);
            else sendJson(msg, 404, { ok: false });
            return;
        }
        if (msg.topic === 'wave_test_v1/detail') onDetail(msg.payload);
        else if (msg.topic === 'wave_test_v1/samples' && Array.isArray(msg.payload)) onSamples(msg.payload);
    },
    stop: function () {
        clearInterval(timer);
        for (const w of L.exportWaiters) clearTimeout(w.timer);
        L.exportWaiters = [];
    }
};
