'use strict';

const $ = (s) => document.querySelector(s);
const CATS = {
  voz: { name: 'Voz', icon: '🗣', color: 'var(--voz)' },
  movimiento_sonido: { name: 'Movimiento y sonido', icon: '🎬', color: 'var(--mixto)' },
  movimiento: { name: 'Movimiento', icon: '🏃', color: 'var(--movimiento)' },
  impulsivo: { name: 'Sonido impulsivo', icon: '💥', color: 'var(--impulsivo)' },
  sonido: { name: 'Sonido', icon: '🔊', color: 'var(--sonido)' },
  sin_clasificar: { name: 'Sin clasificar', icon: '❔', color: 'var(--muted)' },
};
const LABEL_NAMES = { voz: 'Voz', sonido: 'Sonido', impulsivo: 'Impulsivo', movimiento: 'Movimiento', cambio_de_luz: 'Cambio de luz' };
const TR_STATUS = { none: 'No aplica', pending: 'En cola', processing: 'Transcribiendo…', done: 'Lista', empty: 'Sin texto reconocible', error: 'Error' };

const state = { type: '', from: '', to: '', q: '', oldest: null, loading: false, done: false, events: new Map(), order: [], current: null };

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') e.className = v;
    else if (k === 'text') e.textContent = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) if (c != null) e.append(c);
  return e;
}

async function api(path, opts = {}) {
  opts.headers = Object.assign({ 'X-Vig': '1' }, opts.headers || {});
  opts.credentials = 'same-origin';
  const r = await fetch(path, opts);
  if (r.status === 401) { location.href = '/login'; throw new Error('no autenticado'); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
  return data;
}

const pad = (n) => String(n).padStart(2, '0');
function fmtTime(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleDateString('es-AR', { weekday: 'short', day: '2-digit', month: '2-digit' }) +
    ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
}
function fmtClock(ts) { const d = new Date(ts * 1000); return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds()); }
function fmtDur(s) {
  if (s == null) return '—';
  s = Math.round(s);
  const m = Math.floor(s / 60);
  return m >= 60 ? `${Math.floor(m / 60)}:${pad(m % 60)}:${pad(s % 60)}` : `${m}:${pad(s % 60)}`;
}
const fmtBytes = (b) => b > 1e9 ? (b / 1e9).toFixed(2) + ' GB' : b > 1e6 ? (b / 1e6).toFixed(1) + ' MB' : Math.round(b / 1e3) + ' kB';
const pct = (x) => x == null ? '—' : Math.round(x * 100) + '%';
const cat = (ev) => CATS[ev.category] || CATS.sin_clasificar;
const liveDuration = (ev) => ev.duration != null ? ev.duration : (Date.now() / 1000 - ev.started_at);

function statusBadge(ev) {
  if (ev.status === 'recording') return el('span', { class: 'badge rec', text: 'EN CURSO' });
  if (ev.status === 'processing') return el('span', { class: 'badge proc', text: 'PROCESANDO' });
  if (ev.status === 'error') return el('span', { class: 'badge err', text: 'ERROR' });
  return null;
}

// ---------- lista ----------
function card(ev) {
  const c = cat(ev);
  const thumb = ev.media.thumb
    ? el('img', { src: ev.media.thumb.url, alt: '', loading: 'lazy' })
    : el('div', { class: 'ph', text: ev.status === 'recording' ? '⏺' : c.icon });
  const meta = [];
  if (ev.peak_db != null) meta.push(`pico ${ev.peak_db.toFixed(0)} dBFS`);
  if (ev.has_motion) meta.push(`mov ${pct(ev.motion_max)}`);
  if (ev.has_voice) meta.push(`voz ${pct(ev.voice_ratio)}`);
  if (ev.light_change) meta.push('cambio de luz');
  const star = el('button', {
    class: 'star' + (ev.starred ? ' on' : ''), title: 'Favorito', 'aria-label': 'Favorito', text: ev.starred ? '★' : '☆',
    onclick: (e) => { e.stopPropagation(); toggleStar(ev); },
  });
  return el('li', { class: 'card', tabindex: '0', 'data-id': ev.id,
    onclick: () => openDetail(ev.id), onkeydown: (e) => { if (e.key === 'Enter') openDetail(ev.id); } },
    el('div', { class: 'thumb' }, thumb,
      ev.media.video ? el('span', { class: 'play', text: '▶ video' }) : (ev.media.audio ? el('span', { class: 'play', text: '♪ audio' }) : null),
      el('span', { class: 'dur', text: fmtDur(liveDuration(ev)) })),
    el('div', {},
      el('div', { class: 'cat' }, el('span', { class: 'dot', style: null }), c.icon + ' ' + c.name, statusBadge(ev)),
      el('div', { class: 'time', text: fmtTime(ev.started_at) }),
      el('div', { class: 'meta', text: meta.join(' · ') }),
      ev.transcript ? el('div', { class: 'snippet', text: '“' + ev.transcript + '”' }) : null),
    star);
}

function paintCard(ev, node) {
  const fresh = card(ev);
  fresh.querySelector('.dot').style.background = cat(ev).color;
  if (node) node.replaceWith(fresh);
  return fresh;
}

function renderMarkFirst() {
  const list = $('#list');
  list.querySelectorAll('.card.first').forEach((n) => n.classList.remove('first'));
  if (list.firstElementChild) list.firstElementChild.classList.add('first');
  $('#list-empty').hidden = list.children.length > 0;
}

function filtersActive() { return !!(state.type || state.from || state.to || state.q); }

function queryString(extra = {}) {
  const p = new URLSearchParams();
  if (state.type) p.set('type', state.type);
  if (state.from) p.set('from', state.from);
  if (state.to) p.set('to', state.to);
  if (state.q) p.set('q', state.q);
  for (const [k, v] of Object.entries(extra)) if (v != null) p.set(k, v);
  return p.toString();
}

async function loadMore(reset = false) {
  if (state.loading) return;
  if (reset) {
    state.oldest = null; state.done = false; state.events.clear(); state.order = [];
    $('#list').replaceChildren(); $('#new-banner').hidden = true;
  }
  if (state.done) return;
  state.loading = true;
  $('#more').disabled = true;
  try {
    const data = await api('/api/events?' + queryString({ limit: 30, before: state.oldest }));
    const list = $('#list');
    for (const ev of data.items) {
      if (state.events.has(ev.id)) continue;
      state.events.set(ev.id, ev);
      state.order.push(ev.id);
      list.append(paintCard(ev));
      state.oldest = ev.id;
    }
    state.done = data.items.length < 30;
  } catch (e) {
    toast('No se pudieron cargar los eventos: ' + e.message);
  } finally {
    state.loading = false;
    $('#more').disabled = false;
    $('#more').hidden = state.done;
    renderMarkFirst();
  }
}

function upsertEvent(ev) {
  const node = $(`#list [data-id="${ev.id}"]`);
  if (node) {
    state.events.set(ev.id, ev);
    paintCard(ev, node);
  } else if (!filtersActive()) {
    const newest = state.order.length ? state.order[0] : 0;
    if (ev.id > newest) {
      state.events.set(ev.id, ev);
      state.order.unshift(ev.id);
      $('#list').prepend(paintCard(ev));
    }
  } else {
    $('#new-banner').hidden = false;
  }
  renderMarkFirst();
  if (state.current === ev.id && $('#detail').open) renderDetail(ev, false);
}

async function toggleStar(ev) {
  try {
    const upd = await api(`/api/events/${ev.id}/star`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ starred: !ev.starred }) });
    upsertEvent(upd);
  } catch (e) { toast(e.message); }
}

// ---------- detalle ----------
async function openDetail(id) {
  state.current = id;
  let ev = state.events.get(id);
  try { ev = await api('/api/events/' + id); state.events.set(id, ev); } catch (e) { if (!ev) return toast(e.message); }
  renderDetail(ev, true);
  if (!$('#detail').open) $('#detail').showModal();
}

function renderDetail(ev, resetMedia) {
  const c = cat(ev);
  $('#d-title').textContent = `${c.icon} ${c.name} · ${fmtTime(ev.started_at)}`;
  const body = $('#d-body');
  const mediaKey = [ev.id, ev.media.video?.id, ev.media.audio?.id, ev.media.thumb?.id, ev.status].join(':');
  if (!resetMedia && body.dataset.mediaKey === mediaKey) {
    // mismos archivos: no interrumpir la reproducción, sólo refrescar datos
    body.querySelector('.info')?.replaceWith(detailInfo(ev));
  } else {
    body.dataset.mediaKey = mediaKey;
    body.replaceChildren();
    if (ev.media.video) {
      body.append(el('video', { src: ev.media.video.url, controls: true, playsinline: true, preload: 'metadata',
        poster: ev.media.thumb && ev.media.thumb.mime === 'image/jpeg' ? ev.media.thumb.url : null }));
    }
    if (ev.media.audio) {
      body.append(el('div', { class: 'muted', text: ev.media.video ? 'Sólo audio:' : 'Audio:' }),
        el('audio', { src: ev.media.audio.url, controls: true, preload: 'metadata' }));
      if (!ev.media.video && ev.media.thumb) body.append(el('img', { src: ev.media.thumb.url, alt: 'Forma de onda', class: 'wave' }));
    }
    if (!ev.media.video && !ev.media.audio) {
      body.append(el('p', { class: 'muted', text: ev.status === 'recording' ? 'El evento se está grabando…' :
        ev.status === 'processing' ? 'Procesando archivos…' : 'Este evento no tiene archivos multimedia.' }));
    }
    body.append(detailInfo(ev));
  }
  const s = $('#d-star');
  s.textContent = ev.starred ? '★ Favorito' : '☆ Favorito';
  s.onclick = () => toggleStar(ev);
  const t = $('#d-transcribe');
  t.hidden = !ev.media.audio;
  t.disabled = ev.transcript_status === 'pending' || ev.transcript_status === 'processing';
  t.textContent = ev.transcript_status === 'done' ? 'Transcribir de nuevo' : 'Transcribir';
  if (aiState && !aiState.activa) t.textContent += ' (en cola: IA pausada)';
  t.onclick = async () => {
    try { upsertEvent(await api(`/api/events/${ev.id}/transcribe`, { method: 'POST' })); } catch (e) { toast(e.message); }
  };
  for (const [id, m, ext] of [['#d-dl-video', ev.media.video, 'mp4'], ['#d-dl-audio', ev.media.audio, 'm4a']]) {
    const a = $(id);
    a.hidden = !m;
    if (m) { a.href = m.url; a.setAttribute('download', `${ev.uid}.${ext}`); }
  }
  const del = $('#d-delete');
  del.disabled = ev.status === 'recording' || ev.status === 'processing';
  del.onclick = async () => {
    if (!confirm('¿Eliminar este evento y sus archivos? No se puede deshacer.')) return;
    try {
      await api(`/api/events/${ev.id}/delete`, { method: 'POST' });
      $(`#list [data-id="${ev.id}"]`)?.remove();
      state.events.delete(ev.id);
      state.order = state.order.filter((x) => x !== ev.id);
      renderMarkFirst();
      $('#detail').close();
    } catch (e) { toast(e.message); }
  };
  const idx = state.order.indexOf(ev.id);
  $('#d-prev').disabled = idx <= 0;
  $('#d-next').disabled = idx < 0 || idx >= state.order.length - 1;
}

function detailInfo(ev) {
  const rows = [
    ['Inicio', fmtTime(ev.started_at)],
    ['Fin', ev.ended_at ? fmtTime(ev.ended_at) : 'en curso'],
    ['Duración', fmtDur(liveDuration(ev))],
    ['Estado', { recording: 'Grabando', processing: 'Procesando', ready: 'Listo', error: 'Error' }[ev.status] || ev.status],
    ['Intensidad pico', ev.peak_db != null ? ev.peak_db + ' dBFS' : '—'],
    ['Intensidad media', ev.mean_db != null ? ev.mean_db + ' dBFS' : '—'],
    ['Ruido ambiente', ev.noise_floor_db != null ? ev.noise_floor_db.toFixed(1) + ' dBFS' : '—'],
    ['Movimiento máx.', ev.has_motion ? pct(ev.motion_max) + ' de la imagen' : 'no'],
    ['Tiempo con movimiento', ev.has_motion ? pct(ev.motion_ratio) : '—'],
    ['Proporción de voz (VAD)', ev.has_voice ? pct(ev.voice_ratio) : (ev.voice_ratio ? pct(ev.voice_ratio) + ' (insuficiente)' : 'no')],
    ['Transcripción', TR_STATUS[ev.transcript_status] || ev.transcript_status],
    ['Identificador', ev.uid],
  ];
  if (ev.continuation_of) rows.push(['Continúa el evento', '#' + ev.continuation_of]);
  if (ev.error) rows.push(['Error', ev.error]);
  return el('div', { class: 'info' },
    ev.labels.length ? el('div', { class: 'labels' }, ev.labels.map((l) =>
      el('div', { class: 'label' }, (LABEL_NAMES[l.name] || l.name) + (l.conf != null ? ` · ${pct(l.conf)}` : ''),
        el('small', { text: `${l.source}${l.note ? ' — ' + l.note : ''}` })))) : null,
    ev.transcript ? el('div', {}, el('h3', { text: 'Transcripción' }), el('div', { class: 'transcript', text: ev.transcript })) : null,
    el('table', { class: 'kv' }, el('tbody', {}, rows.map(([k, v]) => el('tr', {}, el('td', { text: k }), el('td', { text: String(v) }))))));
}

function step(delta) {
  const idx = state.order.indexOf(state.current);
  const next = state.order[idx + delta];
  if (next != null) openDetail(next);
}

// ---------- estadísticas ----------
async function loadStats() {
  try {
    const s = await api('/api/stats?days=' + $('#stats-days').value);
    const tot = s.totals;
    const bytes = s.storage.reduce((a, r) => a + r.bytes, 0);
    const period = s.per_day.reduce((a, r) => a + r.total, 0);
    $('#stats-totals').replaceChildren(
      ...[['Eventos en el período', period], ['Eventos guardados', tot.total], ['Favoritos', tot.favoritos], ['Espacio usado', fmtBytes(bytes)]]
        .map(([k, v]) => el('div', { class: 'kpi' }, el('b', { text: String(v) }), el('span', { class: 'muted', text: k }))));
    const days = [];
    for (let i = s.days - 1; i >= 0; i--) {
      const d = new Date(); d.setDate(d.getDate() - i);
      days.push(`${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`);
    }
    const byDay = Object.fromEntries(s.per_day.map((r) => [r.day, r]));
    const max = Math.max(1, ...s.per_day.map((r) => r.total));
    $('#stats-days-chart').replaceChildren(...days.map((d) => {
      const r = byDay[d] || { total: 0, voz: 0, sonido: 0, movimiento: 0 };
      const fill = el('div', { class: 'bar-fill' });
      fill.style.width = (100 * r.total / max) + '%';
      const row = el('div', { class: 'bar-row', title: `voz ${r.voz || 0} · sonido ${r.sonido || 0} · movimiento ${r.movimiento || 0}` },
        el('span', { text: d.slice(5).split('-').reverse().join('/') }), el('div', { class: 'bar-track' }, fill), el('b', { text: String(r.total) }));
      return row;
    }));
    const byHour = Object.fromEntries(s.per_hour.map((r) => [r.hour, r.total]));
    const hmax = Math.max(1, ...Object.values(byHour));
    const hours = $('#stats-hours-chart');
    hours.replaceChildren(...Array.from({ length: 24 }, (_, h) => {
      const b = el('div', { class: 'hour', title: `${pad(h)}:00 — ${byHour[h] || 0} eventos` });
      b.style.height = (100 * (byHour[h] || 0) / hmax) + '%';
      return b;
    }));
    if (!hours.nextElementSibling?.classList.contains('hours-axis')) {
      hours.after(el('div', { class: 'hours-axis' }, Array.from({ length: 24 }, (_, h) => el('span', { text: h % 3 === 0 ? String(h) : '' }))));
    }
    const kinds = { video: 'Video', audio: 'Audio', thumb: 'Miniaturas' };
    $('#stats-storage').replaceChildren(el('tbody', {}, s.storage.map((r) =>
      el('tr', {}, el('td', { text: kinds[r.kind] || r.kind }), el('td', { text: `${r.files} archivos · ${fmtBytes(r.bytes)}` }))),
      tot.desde ? el('tr', {}, el('td', { text: 'Evento más antiguo' }), el('td', { text: fmtTime(tot.desde) })) : null));
  } catch (e) { toast(e.message); }
}

// ---------- estado ----------
const COMP_NAMES = { captura: 'Captura y eventos', audio: 'Micrófono', video: 'Cámara', worker: 'Transcripción (Whisper)', web: 'Servidor web' };
function renderStatus(st) {
  const comps = st.components;
  const a = comps.audio || {}, v = comps.video || {};
  setInd('#ind-mic', a.state);
  setInd('#ind-cam', v.state);
  renderAi(st.ia_local);
  $('#status-list').replaceChildren(...Object.keys(COMP_NAMES).map((k) => {
    const c = comps[k] || { state: 'sin datos', detail: {} };
    const cls = c.state === 'ok' ? 'ok' : c.state === 'error' ? 'error' : 'other';
    const detail = Object.entries(c.detail || {}).filter(([, val]) => val !== '' && val !== null)
      .map(([key, val]) => `${key}: ${typeof val === 'object' ? JSON.stringify(val) : val}`).join('\n');
    return el('div', { class: 'st' },
      el('div', { class: 'st-head' }, el('span', { text: COMP_NAMES[k] }), el('span', { class: 'st-state ' + cls, text: c.state })),
      c.age_s != null && k !== 'web' ? el('div', { class: 'muted', text: `actualizado hace ${Math.round(c.age_s)} s` }) : null,
      detail ? el('pre', { text: detail }) : null);
  }), el('div', { class: 'st' }, el('div', { class: 'st-head' }, el('span', { text: 'Cola de transcripción' })),
    el('pre', { text: Object.entries(st.transcripciones || {}).map(([k, n]) => `${TR_STATUS[k] || k}: ${n}`).join('\n') || '—' }),
    el('div', { class: 'muted', text: `Retención: ${st.retencion_dias} días` })));
  const d = a.detail || {};
  if (d.level_db != null) {
    const toPct = (db) => Math.max(0, Math.min(100, (db + 80) / 80 * 100));
    $('#meter-bar').style.width = toPct(d.level_db) + '%';
    if (d.floor_db != null) $('#meter-floor').style.left = toPct(d.floor_db) + '%';
    $('#meter-text').textContent = `Nivel ${d.level_db} dBFS · ruido ambiente ${d.floor_db ?? '—'} dBFS · VAD ${d.vad ? 'activo' : 'no disponible'} (se actualiza cada ~3 s)`;
  }
}
// ---------- interruptor de IA local ----------
let aiState = null;
function renderAi(ai) {
  if (!ai) return;
  aiState = ai;
  const chip = $('#ind-ai'), btn = $('#ai-toggle'), lbl = $('#ai-state');
  const active = ai.habilitada_config && ai.activa;
  chip.classList.toggle('on', active);
  chip.classList.toggle('off', !active);
  chip.classList.toggle('paused', ai.habilitada_config && !ai.activa);
  chip.setAttribute('aria-pressed', String(active));
  chip.disabled = btn.disabled = !ai.habilitada_config;
  if (!ai.habilitada_config) {
    chip.title = 'IA local deshabilitada en config.toml ([transcription] enabled = false)';
    lbl.textContent = 'deshabilitada en config'; lbl.className = 'st-state other';
    btn.textContent = 'Deshabilitada en la configuración';
    return;
  }
  chip.title = active ? 'IA local activa — tocar para pausar' : 'IA local pausada — tocar para reanudar';
  lbl.textContent = active ? 'activa' : 'pausada';
  lbl.className = 'st-state ' + (active ? 'ok' : 'other');
  btn.textContent = active ? 'Pausar IA local' : 'Reanudar IA local';
}
async function toggleAi() {
  if (!aiState || !aiState.habilitada_config) return;
  const enable = !aiState.activa;
  if (!enable && !confirm('¿Pausar la IA local? Se corta la transcripción en curso y los eventos quedan en cola.')) return;
  try {
    renderStatus(await api('/api/settings/ai', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: enable }) }));
  } catch (e) { toast(e.message); }
}

function setInd(sel, s) {
  const n = $(sel);
  n.classList.toggle('on', s === 'ok');
  n.classList.toggle('err', s !== 'ok' && s !== 'deshabilitado' && s !== 'detenido');
  n.classList.toggle('off', s !== 'ok');
  n.title = (sel === '#ind-mic' ? 'Micrófono: ' : 'Cámara: ') + (s === 'ok' ? 'grabando' : (s || 'sin datos'));
}

// ---------- vista reciente ----------
async function refreshSnap() {
  const img = $('#snap');
  const url = '/api/snapshot?t=' + Date.now();
  try {
    const r = await fetch(url, { credentials: 'same-origin' });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || 'HTTP ' + r.status);
    const blob = await r.blob();
    if (img.dataset.obj) URL.revokeObjectURL(img.dataset.obj);
    img.dataset.obj = URL.createObjectURL(blob);
    img.src = img.dataset.obj;
    $('#snap-info').textContent = 'Cuadro de hace ≤ 8 s · ' + fmtClock(Date.now() / 1000);
  } catch (e) { $('#snap-info').textContent = 'Sin imagen: ' + e.message; }
}

// ---------- en vivo ----------
let es = null;
function connectStream() {
  es = new EventSource('/api/stream');
  es.addEventListener('open', () => $('#ind-live').classList.add('on'));
  es.addEventListener('error', () => $('#ind-live').classList.remove('on'));
  es.addEventListener('ev', (m) => upsertEvent(JSON.parse(m.data)));
  es.addEventListener('status', (m) => renderStatus(JSON.parse(m.data)));
}

let toastTimer;
function toast(msg) {
  let t = $('#toast');
  if (!t) { t = el('div', { id: 'toast' }); document.body.append(t); Object.assign(t.style, { position: 'fixed', left: '16px', right: '16px', bottom: '16px', background: 'var(--err)', color: '#fff', padding: '10px 14px', borderRadius: '8px', zIndex: 50 }); }
  t.textContent = msg; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, 5000);
}

// ---------- inicio ----------
function init() {
  document.querySelectorAll('.tabs button').forEach((b) => b.addEventListener('click', () => {
    document.querySelectorAll('.tabs button').forEach((x) => x.classList.toggle('active', x === b));
    for (const t of ['events', 'stats', 'status']) $('#tab-' + t).hidden = t !== b.dataset.tab;
    if (b.dataset.tab === 'stats') loadStats();
    if (b.dataset.tab === 'status') api('/api/status').then(renderStatus).catch((e) => toast(e.message));
  }));
  $('#type-chips').addEventListener('click', (e) => {
    const b = e.target.closest('.chip'); if (!b) return;
    document.querySelectorAll('#type-chips .chip').forEach((x) => x.classList.toggle('active', x === b));
    state.type = b.dataset.type; loadMore(true);
  });
  $('#filters').addEventListener('submit', (e) => {
    e.preventDefault();
    state.from = $('#f-from').value; state.to = $('#f-to').value; state.q = $('#f-q').value.trim();
    loadMore(true);
  });
  $('#f-clear').addEventListener('click', () => {
    $('#f-from').value = ''; $('#f-to').value = ''; $('#f-q').value = '';
    state.from = state.to = state.q = '';
    loadMore(true);
  });
  $('#new-banner').addEventListener('click', () => loadMore(true));
  $('#more').addEventListener('click', () => loadMore());
  new IntersectionObserver((ents) => { if (ents[0].isIntersecting && !state.done) loadMore(); }).observe($('#more'));
  $('#d-close').addEventListener('click', () => $('#detail').close());
  $('#d-prev').addEventListener('click', () => step(-1));
  $('#d-next').addEventListener('click', () => step(1));
  $('#detail').addEventListener('close', () => { $('#d-body').querySelectorAll('video,audio').forEach((m) => m.pause()); state.current = null; });
  $('#snap-box').addEventListener('toggle', () => { if ($('#snap-box').open) refreshSnap(); });
  $('#snap-refresh').addEventListener('click', refreshSnap);
  $('#stats-days').addEventListener('change', loadStats);
  $('#ind-ai').addEventListener('click', toggleAi);
  $('#ai-toggle').addEventListener('click', toggleAi);
  setInterval(() => {  // refresca duraciones de eventos en curso
    for (const ev of state.events.values()) if (ev.status === 'recording') upsertEvent(ev);
  }, 5000);
  setInterval(() => { if ($('#snap-box').open && !document.hidden) refreshSnap(); }, 10000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden && es && es.readyState === 2) connectStream(); });
  loadMore(true);
  api('/api/status').then(renderStatus).catch(() => {});
  connectStream();
}
init();
