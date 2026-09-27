<template>
  <section class="bzflow-card">
    <h3>__TITLE__</h3>
    <div class="bzflow-metrics">
      <div><span>Precio actual</span><strong :style="{color:'__COLOR__'}">{{ m.price }}</strong></div>
      <div><span>BUY agresivo 1s</span><strong class="bzflow-buy">{{ m.buy1 }}</strong></div>
      <div><span>SELL agresivo 1s</span><strong class="bzflow-sell">{{ m.sell1 }}</strong></div>
      <div><span>DELTA 1s</span><strong :class="m.delta1c">{{ m.delta1 }}</strong></div>
      <div><span>Trades/s</span><strong>{{ m.tps }}</strong></div>
    </div>
    <div class="bzflow-metrics bzflow-small">
      <div><span>BUY últimos 10s</span><strong class="bzflow-buy">{{ m.buy10 }}</strong></div>
      <div><span>SELL últimos 10s</span><strong class="bzflow-sell">{{ m.sell10 }}</strong></div>
      <div><span>DELTA últimos 10s</span><strong :class="m.delta10c">{{ m.delta10 }}</strong></div>
    </div>
    <div class="bzflow-state">{{ state }}</div>
    <div class="bzflow-label">__ASSET__ PRICE · 2 MIN · cada trade · USDT</div>
    <canvas ref="price" @mousemove="hover" @mouseleave="leave" style="width:100%;height:190px;display:block"
      aria-label="Precio __ASSET__ por trade, últimos 120 segundos"></canvas>
    <div class="bzflow-label">
      __ASSET__ AGGRESSIVE FLOW · 1s · <span class="bzflow-buy">BUY ▲</span> / <span class="bzflow-sell">SELL ▼</span> · USD(T)
      <label><input type="checkbox" v-model="showDelta"> <span class="bzflow-delta">Delta</span></label>
      <label><input type="checkbox" v-model="showCvd"> CVD</label>
    </div>
    <canvas ref="flow" @mousemove="hover" @mouseleave="leave" style="width:100%;height:220px;display:block"
      aria-label="Compra y venta agresiva __ASSET__ por segundo, últimos 120 segundos"></canvas>
    <canvas v-show="showCvd" ref="cvd" @mousemove="hover" @mouseleave="leave" style="width:100%;height:64px;display:block"
      aria-label="CVD __ASSET__"></canvas>
    <div class="bzflow-tip">{{ tip || 'Tiempo local · pasar el mouse para ver precio y bucket de ese segundo' }}</div>
    <small>{{ detail }}</small>
  </section>
</template>
<script>
export default {
  data() {
    return {
      symbol: '__SYMBOL__', showDelta: true, showCvd: true, tip: '', detail: '', state: 'Esperando Node-RED…',
      m: { price:'—', buy1:'—', sell1:'—', delta1:'—', delta1c:'', tps:'—', buy10:'—', sell10:'—', delta10:'—', delta10c:'' }
    }
  },
  mounted() {
    this.st = { active:true, rows:[], head:0, buckets:new Map(), epoch:'', after:0, bver:0, waiting:false, requested:0,
      token:'', clock:Date.now(), anchor:performance.now(), lastReply:0, info:null, fps:20, lastFrame:0, hoverPx:null, plot:null };
    this.onMsg = msg => this.receive(msg);
    this.$socket.on('msg-input:' + this.id, this.onMsg);
    this.pullTimer = setInterval(() => this.pull(), 200);
    this.frameTimer = setInterval(() => this.draw(), 50);
    this.onVis = () => { this.resetLocal(); if (!document.hidden) this.pull(); };
    document.addEventListener('visibilitychange', this.onVis);
    this.pull();
  },
  unmounted() {
    this.st.active = false;
    clearInterval(this.pullTimer); clearInterval(this.frameTimer);
    this.$socket.off('msg-input:' + this.id, this.onMsg);
    document.removeEventListener('visibilitychange', this.onVis);
  },
  methods: {
    resetLocal() {
      const s = this.st;
      s.rows = []; s.head = 0; s.buckets.clear(); s.epoch = ''; s.after = 0; s.bver = 0; s.token = ''; s.waiting = false;
    },
    pull() {
      const s = this.st;
      if (!s?.active || document.hidden || !this.$socket.connected) return;
      if (s.waiting && performance.now() - s.requested < 3000) return;
      s.waiting = true; s.requested = performance.now();
      s.token = this.id + ':' + Math.random().toString(36).slice(2);
      this.send({ topic:'flow:pull', payload:{ symbol:this.symbol, epoch:s.epoch, after:s.after, bver:s.bver, token:s.token } });
    },
    receive(msg) {
      const s = this.st, p = msg?.payload;
      if (!s?.active || msg?.topic !== 'flow:data' || !p || p.symbol !== this.symbol || p.token !== s.token) return;
      s.waiting = false;
      if (p.reset || p.epoch !== s.epoch) { s.rows = []; s.head = 0; s.buckets.clear(); }
      s.epoch = p.epoch; s.after = p.after; s.bver = p.bver; s.clock = p.now; s.anchor = performance.now(); s.lastReply = s.anchor;
      s.info = p;
      const from = s.rows.length;
      for (const r of p.rows) s.rows.push(r);
      for (let i = Math.max(s.head + 1, from); i < s.rows.length; i++) {
        if (s.rows[i][1] < s.rows[i - 1][1]) { s.rows = s.rows.slice(s.head).sort((a, b) => a[1] - b[1] || a[0] - b[0]); s.head = 0; break; }
      }
      // Sólo buckets terminados (nuevos o revisados): como máximo uno por segundo.
      for (const b of p.buckets) s.buckets.set(b.timestamp, b);
      const m = this.m, last = p.metrics.last, ten = p.metrics.ten;
      m.price = p.price == null ? '—' : Number(p.price).toLocaleString('es-AR', { minimumFractionDigits:2, maximumFractionDigits:8 });
      if (last) {
        m.buy1 = this.usd(last.buy_usd); m.sell1 = this.usd(last.sell_usd);
        m.delta1 = this.signed(last.delta_usd); m.delta1c = this.cls(last.delta_usd); m.tps = last.total_trades;
      }
      if (ten.seconds) {
        m.buy10 = this.usd(ten.buy_usd); m.sell10 = this.usd(ten.sell_usd);
        m.delta10 = this.signed(ten.delta_usd); m.delta10c = this.cls(ten.delta_usd);
      }
      if (p.more) this.pull();
    },
    usd(v) { return '$' + Math.round(v).toLocaleString('es-AR'); },
    signed(v) { const r = Math.round(v); return (r > 0 ? '+' : r < 0 ? '−' : '') + '$' + Math.abs(r).toLocaleString('es-AR'); },
    compact(v) {
      const a = Math.abs(v), sign = v < 0 ? '−' : '';
      return sign + '$' + (a >= 1e6 ? (a / 1e6).toFixed(a >= 1e7 ? 1 : 2) + 'M' : a >= 1e3 ? (a / 1e3).toFixed(a >= 1e4 ? 0 : 1) + 'K' : a.toFixed(0));
    },
    cls(v) { return Math.round(v) > 0 ? 'bzflow-buy' : Math.round(v) < 0 ? 'bzflow-sell' : ''; },
    time(t) {
      const d = new Date(t), pad = (n, k = 2) => String(n).padStart(k, '0');
      return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds()) + '.' + pad(d.getMilliseconds(), 3);
    },
    hover(e) { this.st.hoverPx = e.clientX - e.target.getBoundingClientRect().left; },
    leave() { this.st.hoverPx = null; this.tip = ''; },
    canvas(ref, h) {
      const cv = this.$refs[ref];
      if (!cv) return null;
      const w = Math.floor(cv.clientWidth), dpr = window.devicePixelRatio || 1;
      if (w < 100) return null;
      if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
      const c = cv.getContext('2d');
      c.setTransform(dpr, 0, 0, dpr, 0, 0);
      c.fillStyle = '#101821'; c.fillRect(0, 0, w, h);
      c.font = '11px system-ui'; c.lineWidth = 1;
      return { c, w, h };
    },
    // Misma escala temporal y mismos márgenes en los tres gráficos: alineados verticalmente.
    grid(g, P, top, bottom, labels) {
      const c = g.c, ticks = g.w > 650 ? 4 : 2;
      c.strokeStyle = '#1d2935';
      for (let i = 0; i <= ticks; i++) {
        const xx = P.left + (P.right - P.left) * i / ticks;
        c.beginPath(); c.moveTo(xx, top); c.lineTo(xx, bottom); c.stroke();
        if (labels) {
          c.fillStyle = '#aebecb'; c.textAlign = i === 0 ? 'left' : i === ticks ? 'right' : 'center';
          c.fillText(this.time(P.cutoff + 120000 * i / ticks), xx, g.h - 6);
        }
      }
    },
    cursor(g, P, top, bottom) {
      if (P.hx == null) return;
      const c = g.c;
      c.strokeStyle = '#e2edf5'; c.setLineDash([3, 3]);
      c.beginPath(); c.moveTo(P.hx, top); c.lineTo(P.hx, bottom); c.stroke();
      c.setLineDash([]);
    },
    draw() {
      const s = this.st;
      if (!s?.active || document.hidden) return;
      const start = performance.now();
      if (start - s.lastFrame < 1000 / s.fps - 1) return;
      s.lastFrame = start;
      const now = s.lastReply ? s.clock + start - s.anchor : Date.now(), cutoff = now - 120000;
      while (s.head < s.rows.length && s.rows[s.head][1] < cutoff) s.head++;
      if (s.head > 4096 && s.head * 2 > s.rows.length) { s.rows = s.rows.slice(s.head); s.head = 0; }
      for (const k of s.buckets.keys()) if (k + 1000 < cutoff) s.buckets.delete(k);
      const p = s.info, alive = this.$socket.connected && s.lastReply && start - s.lastReply <= 3000;
      if (!alive) {
        this.state = 'SIN RESPUESTA DE NODE-RED';
        for (const k of Object.keys(this.m)) if (!k.endsWith('c')) this.m[k] = '—';
      } else {
        const age = p.lastRx ? Math.max(0, Math.floor(now - p.lastRx)) : Infinity;
        this.state = 'Binance ' + this.symbol + ' · ' + p.ws + (age > 5000 ? ' · SIN TRADES RECIENTES' : ' · LIVE') +
          ' · último trade hace ' + (Number.isFinite(age) ? age + ' ms' : '—');
        const notes = [];
        if (p.more) notes.push('cargando pendientes');
        if (p.gaps) notes.push(p.gaps + ' saltos de ID');
        if (p.invalid) notes.push(p.invalid + ' mensajes inválidos');
        if (p.late) notes.push(p.late + ' trades tardíos sumados a su segundo');
        if (p.tooLate) notes.push(p.tooLate + ' trades fuera del buffer de buckets');
        this.detail = 'Trades procesados: ' + p.trades.toLocaleString('es-AR') + ' · puntos de precio: ' +
          (s.rows.length - s.head).toLocaleString('es-AR') + ' · buckets: ' + s.buckets.size + ' · ' + s.fps + ' FPS máx.' +
          (notes.length ? ' · ' + notes.join(' · ') : '');
      }
      const gp = this.canvas('price', 190), gf = this.canvas('flow', 220), gc = this.showCvd ? this.canvas('cvd', 64) : null;
      if (!gp || !gf) return;
      const P = { left:82, right:gp.w - 12, cutoff, now };
      P.x = t => P.left + (t - cutoff) / 120000 * (P.right - P.left);
      P.hx = s.hoverPx != null && s.hoverPx >= P.left && s.hoverPx <= P.right ? s.hoverPx : null;
      this.drawPrice(gp, P, s);
      const vis = [...s.buckets.values()].filter(b => b.timestamp + 1000 >= cutoff && b.timestamp <= now).sort((a, b) => a.timestamp - b.timestamp);
      this.drawFlow(gf, P, vis);
      if (gc) this.drawCvd(gc, P, vis);
      this.tooltip(P, s);
      const elapsed = performance.now() - start;
      if (elapsed > 25 && s.fps > 2) s.fps = Math.max(2, Math.floor(s.fps / 2));
      else if (elapsed < 8 && s.fps < 20) s.fps = Math.min(20, s.fps + 1);
    },
    drawPrice(g, P, s) {
      const c = g.c, top = 10, bottom = g.h - 8;
      let min = Infinity, max = -Infinity;
      for (let i = s.head; i < s.rows.length; i++) { const r = s.rows[i]; if (r[1] <= P.now) { if (r[2] < min) min = r[2]; if (r[2] > max) max = r[2]; } }
      const has = Number.isFinite(min);
      if (!has) { min = 0.99; max = 1.01; }
      const pad = Math.max((max - min) * 0.08, Math.abs(max) * 0.0000001, 0.00000001); min -= pad; max += pad;
      const y = v => bottom - (v - min) / (max - min) * (bottom - top);
      this.grid(g, P, top, bottom, false);
      for (let i = 0; i <= 3; i++) {
        const yy = top + (bottom - top) * i / 3;
        c.strokeStyle = '#273442'; c.beginPath(); c.moveTo(P.left, yy); c.lineTo(P.right, yy); c.stroke();
        c.fillStyle = '#aebecb'; c.textAlign = 'right';
        if (has) c.fillText((max - (max - min) * i / 3).toFixed(Math.min(8, Math.max(2, Math.ceil(-Math.log10(max - min)) + 2))), P.left - 7, yy + 4);
      }
      c.save(); c.beginPath(); c.rect(P.left, top, P.right - P.left, bottom - top); c.clip();
      c.strokeStyle = '__COLOR__'; c.lineWidth = 1.25; c.beginPath();
      let prev = null;
      for (let i = s.head; i < s.rows.length; i++) {
        const r = s.rows[i];
        if (r[1] > P.now) continue;
        if (!prev || prev[3] !== r[3]) c.moveTo(P.x(r[1]), y(r[2])); else c.lineTo(P.x(r[1]), y(r[2]));
        prev = r;
      }
      c.stroke();
      if (prev) { c.fillStyle = '__COLOR__'; c.beginPath(); c.arc(P.x(prev[1]), y(prev[2]), 2, 0, Math.PI * 2); c.fill(); }
      c.restore();
      if (!has) { c.fillStyle = '#aebecb'; c.textAlign = 'center'; c.fillText('Sin trades en los últimos 120 segundos', g.w / 2, g.h / 2); }
      this.cursor(g, P, top, bottom);
    },
    drawFlow(g, P, vis) {
      const c = g.c, top = 10, bottom = g.h - 22;
      let peak = 0;
      for (const b of vis) peak = Math.max(peak, b.buy_usd, b.sell_usd);
      if (!peak) peak = 1;
      peak *= 1.08;
      const zero = (top + bottom) / 2, y = v => zero - v / peak * (bottom - top) / 2;
      this.grid(g, P, top, bottom, true);
      c.textAlign = 'right';
      for (const f of [1, 0.5, 0, -0.5, -1]) {
        const yy = y(peak * f);
        c.strokeStyle = f === 0 ? '#6b7d8e' : '#273442'; c.beginPath(); c.moveTo(P.left, yy); c.lineTo(P.right, yy); c.stroke();
        c.fillStyle = '#aebecb'; c.fillText(f === 0 ? '0' : this.compact(peak * f), P.left - 7, yy + 4);
      }
      c.save(); c.beginPath(); c.rect(P.left, top, P.right - P.left, bottom - top); c.clip();
      const bw = (P.right - P.left) / 120, gap = bw > 4 ? 1 : 0;
      for (const b of vis) {
        const x0 = P.x(b.timestamp) + gap;
        if (!b.ws_connected) { c.fillStyle = 'rgba(174,190,203,0.10)'; c.fillRect(x0, top, bw - gap, bottom - top); }
        c.fillStyle = '#3fb68b'; c.fillRect(x0, y(b.buy_usd), bw - 2 * gap, zero - y(b.buy_usd));
        c.fillStyle = '#ef5d68'; c.fillRect(x0, zero, bw - 2 * gap, y(-b.sell_usd) - zero);   // plot_sell = -sell_usd
      }
      if (this.showDelta) {
        c.strokeStyle = '#f5d76e'; c.lineWidth = 1.5; c.beginPath();
        let prev = null;
        for (const b of vis) {
          const xx = P.x(b.timestamp + 500), yy = y(b.delta_usd);
          if (prev === null || b.timestamp - prev !== 1000) c.moveTo(xx, yy); else c.lineTo(xx, yy);
          prev = b.timestamp;
        }
        c.stroke();
      }
      c.restore();
      if (!vis.length) { c.fillStyle = '#aebecb'; c.textAlign = 'center'; c.fillText('Esperando el primer segundo completo…', g.w / 2, zero - 8); }
      this.cursor(g, P, top, bottom);
    },
    drawCvd(g, P, vis) {
      const c = g.c, top = 6, bottom = g.h - 6;
      let min = Infinity, max = -Infinity;
      for (const b of vis) { min = Math.min(min, b.cvd_usd); max = Math.max(max, b.cvd_usd); }
      this.grid(g, P, top, bottom, false);
      c.fillStyle = '#aebecb'; c.textAlign = 'right';
      c.fillText('CVD', P.left - 7, top + 10);
      if (!vis.length) return;
      if (max - min < 1) { min -= 1; max += 1; }
      const y = v => bottom - (v - min) / (max - min) * (bottom - top);
      c.fillText(this.compact(vis[vis.length - 1].cvd_usd), P.left - 7, bottom - 2);
      c.save(); c.beginPath(); c.rect(P.left, top, P.right - P.left, bottom - top); c.clip();
      c.strokeStyle = '#9fb3c8'; c.lineWidth = 1.25; c.beginPath();
      vis.forEach((b, i) => { const xx = P.x(b.timestamp + 1000), yy = y(b.cvd_usd); if (i) c.lineTo(xx, yy); else c.moveTo(xx, yy); });
      c.stroke(); c.restore();
      this.cursor(g, P, top, bottom);
    },
    tooltip(P, s) {
      if (P.hx == null) { if (s.hoverPx != null) this.tip = ''; return; }
      const t = P.cutoff + (P.hx - P.left) / (P.right - P.left) * 120000, parts = [this.time(t)];
      let lo = s.head, hi = s.rows.length;
      while (lo < hi) { const mid = (lo + hi) >>> 1; if (s.rows[mid][1] < t) lo = mid + 1; else hi = mid; }
      let k = Math.min(lo, s.rows.length - 1);
      if (k > s.head && Math.abs(s.rows[k - 1][1] - t) < Math.abs(s.rows[k][1] - t)) k--;
      if (k >= s.head && s.rows[k]) parts.push('Precio ' + s.rows[k][2]);
      const b = s.buckets.get(Math.floor(t / 1000) * 1000);
      if (b) parts.push('BUY ' + this.usd(b.buy_usd), 'SELL ' + this.usd(b.sell_usd), 'Δ ' + this.signed(b.delta_usd),
        b.total_trades + ' trades', 'Δprecio 1s ' + (b.price_change ?? '—') + (b.revised ? ' · revisado' : '') + (b.ws_connected ? '' : ' · WS desconectado'));
      this.tip = parts.join(' · ');
    }
  }
}
</script>
<style>
.bzflow-card { background:#101821; color:#e2edf5; padding:14px; border-radius:8px; font-family:system-ui,sans-serif; }
.bzflow-card h3 { font-size:15px; margin:0 0 12px; font-weight:600; }
.bzflow-metrics { display:flex; gap:24px; flex-wrap:wrap; font-size:12px; color:#aebecb; }
.bzflow-metrics strong { display:block; color:#fff; font-size:21px; font-variant-numeric:tabular-nums; }
.bzflow-small { margin-top:8px; }
.bzflow-small strong { font-size:16px; }
.bzflow-metrics strong.bzflow-buy, .bzflow-buy { color:#3fb68b; }
.bzflow-metrics strong.bzflow-sell, .bzflow-sell { color:#ef5d68; }
.bzflow-delta { color:#f5d76e; }
.bzflow-state { margin:10px 0 6px; font-size:12px; color:#c8d7e3; }
.bzflow-label { font-size:11px; color:#aebecb; margin:8px 0 3px; letter-spacing:.02em; }
.bzflow-label label { margin-left:12px; cursor:pointer; }
.bzflow-tip { min-height:20px; font-size:11px; color:#aebecb; margin-top:4px; font-variant-numeric:tabular-nums; }
.bzflow-card small { font-size:11px; color:#aebecb; }
</style>
