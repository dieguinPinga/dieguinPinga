// =====================================================================
// WAVE_TEST_V1 · WAVE TEST EXPORT
// ---------------------------------------------------------------------
// Exporta un reporte JSON completo para análisis externo.
// NO toca la lógica de WAVE TEST EVAL: sólo usa sus salidas existentes.
//   1) llega 'export' (inject manual) o 'export_auto' (inject cada 5 min)
//   2) espera el próximo DETALLE de EVAL (salida 2, cada 5 s) => counts/queue/stats frescos
//   3) pide un DUMP a EVAL (topic 'dump') => muestras del mismo instante (±ms)
//   4) arma el reporte con las últimas 500 muestras y lo escribe de forma ASÍNCRONA
//      y por partes (cede el event loop entre bloques): nunca en la ruta de eventos.
// Escritura atómica: <archivo>.tmp -> fsync -> rename.
//   WAVE_TEST_LATEST.json                    (manual y automático, se sobreescribe)
//   WAVE_TEST_YYYY-MM-DD_HH-mm-ss.json       (sólo manual; se conservan los últimos 100)
// Salidas: 1 -> EVAL (pedido de dump) · 2 -> Debug DUMP manual (reenvío) · 3 -> Debug estado del export
// =====================================================================
// directorio de reportes (override opcional: variable de entorno WAVE_REPORT_DIR)
const REPORT_DIR = env.get('WAVE_REPORT_DIR') || '/home/plapopepo/wave_reports';
const LATEST_NAME = 'WAVE_TEST_LATEST.json';
const HIST_PREFIX = 'WAVE_TEST_';
const HIST_RE = /^WAVE_TEST_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.json$/;
const MAX_HISTORY = 100;
const N_SAMPLES = 500;
const CHUNK = 50;                 // muestras serializadas por bloque antes de ceder el event loop
const TIMEOUT_MS = 15000;         // detalle (<= 5 s) + dump deben llegar antes
const EXPORT_VERSION = 1;

const X = {
    lastDetail: null,
    job: null,                    // { trigger, phase: 'wait_detail'|'wait_dump'|'writing', t, timer }
    busy: false,
    stats: { ok: 0, failed: 0, skipped_busy: 0, last_file: null, last_bytes: null, last_ms: null, last_error: null }
};

const pad = (n) => (n < 10 ? '0' : '') + n;
function stamp(d) {   // hora local del miniPC
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + '_' + pad(d.getHours()) + '-' + pad(d.getMinutes()) + '-' + pad(d.getSeconds());
}
const yieldLoop = () => new Promise((res) => setTimeout(res, 0)); // setImmediate no existe en el sandbox del Function node

function status(fill, text) { node.status({ fill: fill, shape: 'dot', text: text }); }
function report(o) { node.send([null, null, { topic: 'wave_test_v1/export', payload: o }], false); }

function startJob(trigger) {
    if (X.job || X.busy) {
        X.stats.skipped_busy++;
        report({ ok: false, trigger: trigger, error: 'export en curso: pedido ignorado' });
        return;
    }
    X.job = { trigger: trigger, phase: 'wait_detail', t: Date.now() };
    X.job.timer = setTimeout(function () { failJob('timeout esperando detalle/dump de WAVE TEST EVAL'); }, TIMEOUT_MS);
    status('blue', 'export ' + trigger + ': esperando detalle');
}

function failJob(err) {
    const j = X.job;
    if (j && j.timer) clearTimeout(j.timer);
    X.job = null;
    X.stats.failed++;
    X.stats.last_error = err;
    status('red', 'export falló: ' + err);
    node.warn('[WAVE EXPORT] ' + err);
    report({ ok: false, trigger: j ? j.trigger : null, error: err });
}

function onDetail(d) {
    X.lastDetail = d;
    const j = X.job;
    if (j && j.phase === 'wait_detail') {
        j.phase = 'wait_dump';
        j.detail = d;
        node.send([{ topic: 'dump' }, null, null], false);   // EVAL responde por su salida 3
    }
}

function onSamples(msg) {
    const j = X.job;
    if (!j || j.phase !== 'wait_dump') {
        node.send([null, msg, null], false);                  // DUMP manual: se reenvía al Debug como antes
        return;
    }
    clearTimeout(j.timer);
    j.phase = 'writing';
    const all = Array.isArray(msg.payload) ? msg.payload : [];
    const samples = all.slice(-N_SAMPLES);
    X.job = null;
    X.busy = true;
    write(j, samples).then(function (res) {
        X.busy = false;
        X.stats.ok++;
        X.stats.last_file = res.file; X.stats.last_bytes = res.bytes; X.stats.last_ms = res.ms; X.stats.last_error = null;
        status('green', j.trigger + ' OK ' + Math.round(res.bytes / 1024) + ' KB · ' + res.ms + ' ms · ' + new Date().toLocaleTimeString());
        report(Object.assign({ ok: true, trigger: j.trigger }, res));
    }).catch(function (e) {
        X.busy = false;
        X.stats.failed++;
        X.stats.last_error = String(e && e.message || e);
        status('red', 'export falló: ' + X.stats.last_error);
        node.warn('[WAVE EXPORT] ' + X.stats.last_error);
        report({ ok: false, trigger: j.trigger, error: X.stats.last_error });
    });
}

// escribe en <final>.tmp, fsync, y recién entonces rename => nunca queda un JSON truncado
async function writeAtomic(finalPath, writer) {
    const tmp = finalPath + '.tmp';
    const fh = await fs.promises.open(tmp, 'w');
    let bytes = 0;
    try {
        await writer(async function (s) { await fh.write(s); bytes += Buffer.byteLength(s); });
        await fh.sync();
    } finally {
        await fh.close();
    }
    await fs.promises.rename(tmp, finalPath);
    return bytes;
}

async function write(j, samples) {
    const t0 = Date.now();
    const d = j.detail;
    const now = new Date();
    const head = {
        export_version: EXPORT_VERSION,
        generated_at: now.toISOString(),
        generated_at_local: stamp(now),
        trigger: j.trigger,
        uptime_s: d.uptime_s,
        detail_ts: d.ts,
        detail_iso: d.iso,
        config: d.config,
        counts: d.counts,
        queue: d.queue,
        strength: d.strength,
        primary_binance_mid: d.primary_binance_mid,
        secondary_median_mid: d.secondary_median_mid,
        baseline_random_direction: d.baseline_random_direction,
        samples_meta: {
            n: samples.length, max: N_SAMPLES,
            first_t0: samples.length ? samples[0].t0 : null,
            last_t0: samples.length ? samples[samples.length - 1].t0 : null,
            note: 'incluye muestras NEUTRAL; las más recientes pueden tener horizontes aún abiertos (future incompleto)'
        }
    };
    await fs.promises.mkdir(REPORT_DIR, { recursive: true });
    const latest = path.join(REPORT_DIR, LATEST_NAME);
    const bytes = await writeAtomic(latest, async function (put) {
        const h = JSON.stringify(head);
        await put(h.slice(0, -1) + ',"samples":[');
        for (let i = 0; i < samples.length; i += CHUNK) {
            const part = samples.slice(i, i + CHUNK).map(function (s) { return JSON.stringify(s); }).join(',');
            await put((i ? ',' : '') + part);
            await yieldLoop();                                 // cede el event loop entre bloques
        }
        await put(']}');
    });
    const res = { file: latest, bytes: bytes, samples: samples.length };
    if (j.trigger === 'manual') {
        const hist = path.join(REPORT_DIR, HIST_PREFIX + stamp(now) + '.json');
        await fs.promises.copyFile(latest, hist + '.tmp');
        await fs.promises.rename(hist + '.tmp', hist);
        res.history_file = hist;
        res.history_deleted = await prune();
    }
    res.ms = Date.now() - t0;
    return res;
}

// conserva sólo los últimos MAX_HISTORY históricos (orden por nombre = orden cronológico)
async function prune() {
    const names = (await fs.promises.readdir(REPORT_DIR)).filter(function (n) { return HIST_RE.test(n); }).sort();
    const extra = names.slice(0, Math.max(0, names.length - MAX_HISTORY));
    for (const n of extra) await fs.promises.unlink(path.join(REPORT_DIR, n));
    return extra.length;
}

// limpieza de .tmp huérfanos de una ejecución interrumpida
fs.promises.readdir(REPORT_DIR).then(function (names) {
    return Promise.all(names.filter(function (n) { return /^WAVE_TEST_.*\.json\.tmp$/.test(n); })
        .map(function (n) { return fs.promises.unlink(path.join(REPORT_DIR, n)).catch(function () {}); }));
}).catch(function () { /* el directorio todavía no existe */ });

status('grey', 'listo · dir ' + REPORT_DIR);

globalThis.__WAVE_EXPORT__ = {
    onMsg: function (msg) {
        if (!msg) return;
        if (msg.topic === 'export') startJob('manual');
        else if (msg.topic === 'export_auto') startJob('auto');
        else if (msg.topic === 'wave_test_v1/detail') onDetail(msg.payload);
        else if (msg.topic === 'wave_test_v1/samples') onSamples(msg);
    },
    stop: function () { if (X.job && X.job.timer) clearTimeout(X.job.timer); X.job = null; }
};
