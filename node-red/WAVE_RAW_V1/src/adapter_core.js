// =====================================================================
// WAVE_RAW_V1 · WS ADAPTER CORE (compartido por los 4 adaptadores)
// ---------------------------------------------------------------------
// Responsabilidad ÚNICA: mantener WebSockets persistentes y emitir cada
// frame crudo como { exchange, conn, gen, t_recv, payload }.
//   * t_recv = Date.now() es la PRIMERA instrucción del callback 'message',
//     antes de parsear o tocar el frame.
//   * NO se parsea JSON aquí (lo hace el NORMALIZER, nodo siguiente).
//   * Reconexión con backoff exponencial + jitter.
//   * Watchdog: feed stale, timeout de conexión, cierre colgado.
//   * Ping de protocolo WS (y ping de aplicación si el exchange lo pide).
//   * node.send(msg, false) => sin clonar el mensaje (cero copias).
// Requiere: libs -> WS = require('ws')  (functionExternalModules: true)
// =====================================================================
const OPT = Object.assign({
    backoffBaseMs: 1000,      // 1s, 2s, 4s, 8s ... (+/-20% jitter)
    backoffMaxMs: 30000,      // techo del backoff
    stableResetMs: 30000,     // si la conexión duró >= 30s, el backoff vuelve a 1s
    resyncDelayMs: 250,       // reconexión rápida pedida por el normalizer (resync de book)
    connectTimeoutMs: 10000,  // handshake máximo
    staleMs: 20000,           // sin NINGÚN mensaje (datos/heartbeat/pong de aplicación) en 20s => matar y reconectar
    pingEveryMs: 10000,       // ping de protocolo WS (mantiene NAT/proxy; el pong NO cuenta como feed vivo)
    watchdogEveryMs: 1000,
    maxPayload: 256 * 1024 * 1024
}, typeof ADAPTER_OPT === 'object' ? ADAPTER_OPT : {});

const S = { stopped: false, conns: [], byId: {}, wd: null };

for (const spec of CONNS) {
    const c = Object.assign({
        gen: 0, ws: null, status: 'idle', attempt: 0,
        reconnects: 0, disconnects: 0, staleKills: 0, frames: 0,
        openedAt: 0, lastAliveAt: 0, lastPongAt: 0, lastPingAt: 0, lastAppPingAt: 0,
        connectStartedAt: 0, closingAt: 0, reconnectTimer: null, nextRetryAt: 0,
        fastNext: false, lastFastAt: 0, lastError: null, lastCloseCode: null
    }, spec);
    S.conns.push(c);
    S.byId[c.id] = c;
}

function connInfo(c, event, extra) {
    return Object.assign({
        topic: 'wave/conn', kind: 'conn', exchange: EXCHANGE, conn: c.id, gen: c.gen,
        event: event, t: Date.now(), status: c.status,
        reconnects: c.reconnects, disconnects: c.disconnects, stale_kills: c.staleKills,
        last_error: c.lastError, last_close_code: c.lastCloseCode, url: c.url
    }, extra || {});
}

function emitConn(c, event, extra) {
    node.send(connInfo(c, event, extra), false);
}

function scheduleReconnect(c) {
    if (S.stopped || c.reconnectTimer) return;
    const now = Date.now();
    if (c.openedAt && now - c.openedAt >= OPT.stableResetMs) c.attempt = 0;
    let delay;
    if (c.fastNext && now - c.lastFastAt > 10000) {
        delay = OPT.resyncDelayMs;
        c.lastFastAt = now;
    } else {
        const base = Math.min(OPT.backoffMaxMs, OPT.backoffBaseMs * Math.pow(2, c.attempt));
        delay = Math.round(base * (0.8 + 0.4 * Math.random()));
        c.attempt++;
    }
    c.fastNext = false;
    c.reconnects++;
    c.status = 'waiting';
    c.nextRetryAt = now + delay;
    c.reconnectTimer = setTimeout(function () {
        c.reconnectTimer = null;
        connect(c);
    }, delay);
}

function kill(c, reason) {
    c.lastError = reason;
    const ws = c.ws;
    if (!ws) return;
    if (c.status !== 'closing') {
        c.status = 'closing';
        c.closingAt = Date.now();
    }
    try { ws.terminate(); } catch (e) { /* el watchdog fuerza el cierre si hace falta */ }
}

function connect(c) {
    if (S.stopped) return;
    c.gen += 1;
    const gen = c.gen;
    c.status = 'connecting';
    c.connectStartedAt = Date.now();
    c.openedAt = 0;
    let ws;
    try {
        ws = new WS(c.url, { perMessageDeflate: false, handshakeTimeout: OPT.connectTimeoutMs, maxPayload: OPT.maxPayload });
    } catch (e) {
        c.lastError = 'ctor: ' + (e && e.message || e);
        c.status = 'closed';
        scheduleReconnect(c);
        return;
    }
    c.ws = ws;

    ws.on('message', function (data) {
        const t = Date.now(); // timestamp local de recepción: ANTES de cualquier parseo
        if (c.ws !== ws) return;
        c.lastAliveAt = t;
        c.frames++;
        try {
            node.send({ topic: 'wave/frame', kind: 'frame', exchange: EXCHANGE, conn: c.id, gen: gen, t_recv: t, payload: data }, false);
        } catch (e) {
            c.lastError = 'send: ' + (e && e.message || e);
        }
    });

    ws.on('open', function () {
        try {
            if (c.ws !== ws) return;
            const now = Date.now();
            c.status = 'open';
            c.openedAt = now;
            c.lastAliveAt = now;
            c.lastPingAt = now;
            c.lastAppPingAt = now;
            c.lastError = null;
            emitConn(c, 'open'); // va por el mismo cable que los frames => el normalizer resetea antes del primer frame
            for (const s of (c.subs || [])) ws.send(typeof s === 'string' ? s : JSON.stringify(s));
        } catch (e) {
            kill(c, 'open handler: ' + (e && e.message || e));
        }
    });

    // 'ws' responde automáticamente a los ping del servidor. Ping/pong de protocolo sólo se registran:
    // un socket que responde pong pero no entrega mensajes es justamente un feed stale.
    ws.on('pong', function () { if (c.ws === ws) c.lastPongAt = Date.now(); });
    ws.on('error', function (err) { c.lastError = String(err && err.message || err); }); // NUNCA quitar: evita crash por 'error' sin listener

    ws.on('close', function (code, reason) {
        try {
            if (c.ws !== ws) return;
            c.ws = null;
            c.status = 'closed';
            c.disconnects++;
            c.lastCloseCode = code;
            emitConn(c, 'close', { code: code, reason: reason ? reason.toString() : '', uptime_ms: c.openedAt ? Date.now() - c.openedAt : 0 });
            scheduleReconnect(c);
        } catch (e) {
            node.error('WAVE adapter close handler: ' + (e && e.message || e));
        }
    });
}

function watchdog() {
    const now = Date.now();
    let open = 0;
    let frames = 0;
    for (const c of S.conns) {
        frames += c.frames;
        if (c.status === 'connecting' && now - c.connectStartedAt > OPT.connectTimeoutMs + 2000) {
            kill(c, 'connect timeout');
        } else if (c.status === 'open') {
            open++;
            if (now - c.lastAliveAt > OPT.staleMs) {
                c.staleKills++;
                kill(c, 'stale ' + (now - c.lastAliveAt) + 'ms');
            } else {
                if (OPT.pingEveryMs && now - c.lastPingAt >= OPT.pingEveryMs) {
                    c.lastPingAt = now;
                    try { c.ws.ping(); } catch (e) { /* noop */ }
                }
                if (c.appPing && now - c.lastAppPingAt >= c.appPing.everyMs) {
                    c.lastAppPingAt = now;
                    try { c.ws.send(c.appPing.payload); } catch (e) { /* noop */ }
                }
            }
        } else if (c.status === 'closing' && now - c.closingAt > 5000) {
            // el socket no emitió 'close': lo abandonamos y reconectamos igual
            const ws = c.ws;
            c.ws = null;
            c.status = 'closed';
            c.disconnects++;
            if (ws) { try { ws.removeAllListeners('message'); } catch (e) { /* noop */ } }
            emitConn(c, 'close', { code: -1, reason: 'forced (no close event)' });
            scheduleReconnect(c);
        }
    }
    const fps = frames - (S.lastFrames || 0);
    S.lastFrames = frames;
    node.status({
        fill: open === S.conns.length ? 'green' : (open ? 'yellow' : 'red'),
        shape: 'dot',
        text: S.conns.map(function (c) { return c.id + ':' + c.status + (c.reconnects ? '(r' + c.reconnects + ')' : ''); }).join(' ') + ' | ' + fps + ' fr/s'
    });
}

const API = {
    control: function (msg) {
        const topic = msg && msg.topic;
        if (topic === 'resync') {
            const c = S.byId[msg.conn];
            if (!c) return;
            c.fastNext = true;
            if (c.ws) kill(c, 'resync: ' + (msg.reason || ''));
        } else if (topic === 'reconnect') {
            for (const c of S.conns) {
                c.attempt = 0;
                if (c.ws) kill(c, 'manual reconnect');
                else if (c.reconnectTimer) { clearTimeout(c.reconnectTimer); c.reconnectTimer = null; connect(c); }
            }
        }
    },
    stop: function () {
        S.stopped = true;
        if (S.wd) clearInterval(S.wd);
        for (const c of S.conns) {
            if (c.reconnectTimer) clearTimeout(c.reconnectTimer);
            c.reconnectTimer = null;
            const ws = c.ws;
            c.ws = null;
            if (ws) {
                try { ws.removeAllListeners('message'); } catch (e) { /* noop */ }
                try { ws.terminate(); } catch (e) { /* noop */ }
            }
        }
    }
};

// API compartida con el cuerpo "On Message" y "On Stop" (mismo sandbox vm del Function node).
// No se usa el context store de Node-RED: uno persistente intentaría serializar sockets.
globalThis.__WAVE_ADAPTER__ = API;

S.wd = setInterval(watchdog, OPT.watchdogEveryMs);
for (const c of S.conns) connect(c);
