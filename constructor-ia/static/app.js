// Constructor IA — interfaz web (sin dependencias, sin CDN).
// Todo el contenido dinámico se inserta con textContent: nunca innerHTML con datos.
"use strict";

// La CSP prohíbe estilos en línea: los pocos ajustes se traducen a clases CSS.
const STYLE_CLASS = {
  "margin-bottom:.8rem": "mb", "margin-top:.4rem": "mt", "margin-top:.5rem": "mt", "margin-top:.6rem": "mt",
  "margin-top:.8rem": "mt2", "margin-top:1rem": "mt2", "overflow-x:auto": "scroll-x", "white-space:pre-wrap": "pre-wrap",
};
const S = { me: null, csrf: null, es: null, view: null, draft: null, timers: [], handlers: [] };
const $ = (sel, root = document) => root.querySelector(sel);

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.classList.add(...v.split(" ").filter(Boolean));
    else if (k === "style") el.classList.add(STYLE_CLASS[v] || "x");
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "text") el.textContent = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (err ? " err" : "");
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), err ? 6000 : 3000);
}

async function api(method, path, body) {
  const opts = { method, credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  if (method !== "GET" && S.csrf) opts.headers["X-CSRF-Token"] = S.csrf;
  const r = await fetch(path, opts);
  let data = {};
  try { data = await r.json(); } catch (_) { /* sin cuerpo */ }
  if (r.status === 401 && path !== "/api/login") {
    S.me = null;
    showLogin();
    throw new Error("Sesión expirada");
  }
  if (!r.ok) throw new Error(data.error || `Error HTTP ${r.status}`);
  return data;
}

const fmtDate = (ts) => (ts ? new Date(ts * 1000).toLocaleString("es-AR", { dateStyle: "short", timeStyle: "medium" }) : "—");
const fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString("es-AR");
function fmtDur(s) {
  if (s === null || s === undefined || s < 0) return "—";
  s = Math.round(s);
  const m = Math.floor(s / 60), r = s % 60;
  return m ? `${m} min ${r} s` : `${r} s`;
}
const badge = (state) => h("span", { class: `badge st-${state}`, text: state });
const ACTIVE = ["preparando", "generando", "probando", "corrigiendo", "explorando"];

function every(ms, fn) { S.timers.push(setInterval(fn, ms)); }
function onEvent(fn) { S.handlers.push(fn); }
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

// ---------------------------------------------------------------- sesión
function showLogin() {
  $("#top").hidden = true;
  if (S.es) { S.es.close(); S.es = null; }
  const err = h("p", { class: "bad small" });
  const user = h("input", { type: "text", autocomplete: "username", autocapitalize: "none", required: true });
  const pass = h("input", { type: "password", autocomplete: "current-password", required: true });
  const form = h("form", { class: "panel login" },
    h("h1", {}, h("span", { class: "logo" }, "⬢ "), "Constructor IA"),
    h("p", { class: "muted small", text: "Acceso restringido. Inicie sesión para continuar." }),
    h("label", { text: "Usuario" }), user, h("label", { text: "Contraseña" }), pass, err,
    h("button", { class: "btn", type: "submit", text: "Ingresar" }));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    err.textContent = "";
    try {
      const r = await api("POST", "/api/login", { username: user.value, password: pass.value });
      S.me = r.username; S.csrf = r.csrf;
      pass.value = "";
      startApp();
    } catch (ex) { err.textContent = ex.message; }
  });
  const v = $("#view");
  v.replaceChildren(form);
  user.focus();
}

async function boot() {
  try {
    const me = await api("GET", "/api/me");
    S.me = me.username; S.csrf = me.csrf;
    startApp();
  } catch (_) { showLogin(); }
}

function startApp() {
  $("#top").hidden = false;
  connectStream();
  route();
}

$("#logout").addEventListener("click", async () => {
  if (!confirm("¿Cerrar la sesión?")) return;
  try { await api("POST", "/api/logout", {}); } catch (_) { /* ignorar */ }
  S.me = null; S.csrf = null;
  showLogin();
});

function connectStream() {
  if (S.es) S.es.close();
  const es = new EventSource("/api/stream");
  S.es = es;
  es.addEventListener("open", () => $("#live").classList.add("on"));
  es.addEventListener("error", () => $("#live").classList.remove("on"));
  es.addEventListener("ev", (m) => {
    let ev;
    try { ev = JSON.parse(m.data); } catch (_) { return; }
    if (ev.kind === "estado") refreshRunning();
    for (const fn of S.handlers) { try { fn(ev); } catch (e) { console.error(e); } }
  });
  refreshRunning();
}

const refreshRunning = debounce(async () => {
  try {
    const r = await api("GET", "/api/missions?limit=20");
    const act = r.items.find((m) => ACTIVE.includes(m.state));
    $("#running").textContent = act ? `▶ ${act.id} · ${act.state}` : "";
  } catch (_) { /* silencioso */ }
}, 500);

// ---------------------------------------------------------------- router
window.addEventListener("hashchange", route);
function route() {
  if (!S.me) return;
  for (const t of S.timers) clearInterval(t);
  S.timers = []; S.handlers = [];
  const hash = location.hash || "#/chat";
  const parts = hash.slice(2).split("/");
  const name = parts[0];
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.nav === name ||
    (name === "mision" && a.dataset.nav === "misiones")));
  const v = $("#view");
  v.replaceChildren(h("p", { class: "muted center", text: "Cargando…" }));
  const views = { chat: viewChat, misiones: viewMissions, mision: viewMission, nueva: viewNew, aprobaciones: viewApprovals, estado: viewStatus, explorador: viewExplorer };
  (views[name] || viewChat)(v, parts.slice(1)).catch((e) => {
    v.replaceChildren(h("div", { class: "panel bad", text: e.message }));
  });
}

// ---------------------------------------------------------------- conversación
async function viewChat(v, args) {
  const convs = await api("GET", "/api/conversations");
  let cid = args[0] ? parseInt(args[0], 10) : null;
  if (!cid && convs.items.length) cid = convs.items[0].id;
  const list = h("div", { class: "list" });
  const newBtn = h("button", { class: "btn small", type: "button", text: "+ Nueva conversación", onclick: async () => {
    const r = await api("POST", "/api/conversations", {});
    location.hash = `#/chat/${r.id}`;
  } });
  for (const c of convs.items) {
    list.append(h("a", { class: "item" + (c.id === cid ? " active" : ""), href: `#/chat/${c.id}` },
      h("div", { class: "t", text: c.title }),
      h("div", { class: "muted small" }, fmtDate(c.updated_at), c.mission_id ? ` · ${c.mission_id}` : "")));
  }
  const side = h("div", { class: "panel convs" }, h("div", { class: "row between" }, h("h2", { text: "Conversaciones" }), newBtn),
    convs.items.length ? list : h("p", { class: "muted small", text: "Todavía no hay conversaciones." }));
  const main = h("div", { class: "panel" });
  v.replaceChildren(h("div", { class: "chat" }, side, main));
  if (!cid) {
    main.append(h("p", { class: "muted", text: "Creá una conversación para empezar a hablar con el asistente." }));
    return;
  }
  const msgs = h("div", { class: "msgs" });
  const input = h("textarea", { placeholder: "Escribí tu mensaje… (Ctrl+Enter para enviar)", rows: 3, maxlength: 8000 });
  const send = h("button", { class: "btn", type: "button", text: "Enviar" });
  const draftBtn = h("button", { class: "btn ghost", type: "button", text: "Convertir en borrador de misión" });
  const head = h("div", { class: "row between" });
  main.append(head,
    h("div", { class: "banner", text: "Modo conversación: solo texto. Aquí no se ejecuta código ni se modifica nada. La ejecución solo ocurre al crear y autorizar una misión." }),
    msgs, h("div", { style: "margin-top:.6rem" }, input), h("div", { class: "row", style: "margin-top:.5rem" }, send, draftBtn));

  async function load() {
    const r = await api("GET", `/api/conversations/${cid}`);
    head.replaceChildren(h("h2", { text: r.conversation.title }),
      r.conversation.mission_id ? h("a", { href: `#/mision/${r.conversation.mission_id}`, class: "small", text: `Misión ${r.conversation.mission_id}` }) : "");
    const atBottom = msgs.scrollHeight - msgs.scrollTop - msgs.clientHeight < 60;
    msgs.replaceChildren();
    for (const m of r.messages) msgs.append(renderMsg(m));
    if (!r.messages.length) msgs.append(h("p", { class: "muted small", text: "Sin mensajes todavía." }));
    if (atBottom || !load.done) msgs.scrollTop = msgs.scrollHeight;
    load.done = true;
  }
  function renderMsg(m) {
    const box = h("div", { class: `msg ${m.role}` });
    if (m.status === "pendiente" || m.status === "generando") {
      box.append(h("span", { class: "muted", text: m.status === "pendiente" ? "En cola (espera a que el modelo esté libre)…" : "Pensando…" }));
    } else if (m.status === "error") {
      box.append(h("span", { class: "bad", text: "Error: " + (m.error || "desconocido") }));
    } else {
      box.append(m.content);
    }
    if (m.meta && m.meta.draft) {
      box.append(h("div", { style: "margin-top:.5rem" }, h("button", { class: "btn small", type: "button", text: "Usar este borrador", onclick: () => {
        S.draft = Object.assign({}, m.meta.draft, { conversation_id: cid });
        location.hash = "#/nueva";
      } })));
    }
    const meta = [];
    if (m.role === "assistant" && m.model) meta.push(m.model);
    if (m.meta && m.meta.ms) meta.push(fmtDur(m.meta.ms / 1000));
    meta.push(fmtTime(m.created_at));
    box.append(h("div", { class: "meta", text: meta.join(" · ") }));
    return box;
  }
  async function post(kind) {
    const content = input.value.trim();
    if (kind === "texto" && !content) return;
    send.disabled = draftBtn.disabled = true;
    try {
      await api("POST", `/api/conversations/${cid}/messages`, { content, kind });
      input.value = "";
      await load();
    } catch (e) { toast(e.message, true); }
    finally { send.disabled = draftBtn.disabled = false; }
  }
  send.addEventListener("click", () => post("texto"));
  draftBtn.addEventListener("click", () => post("borrador"));
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) post("texto"); });
  onEvent(debounce((ev) => { if (ev.conversation_id === cid) load(); }, 300));
  await load();
}

// ---------------------------------------------------------------- misiones
async function viewMissions(v) {
  const filter = h("select", {}, h("option", { value: "", text: "Todas" }),
    ...["pendiente", ...ACTIVE, "aprobada", "completada", "fallida", "cancelada", "interrumpida"].map((s) => h("option", { value: s, text: s })));
  const list = h("div", { class: "list" });
  v.replaceChildren(h("div", { class: "row between", style: "margin-bottom:.8rem" }, h("h1", { text: "Misiones" }),
    h("div", { class: "row" }, filter, h("a", { class: "btn", href: "#/nueva", text: "+ Nueva" }))), list);
  async function load() {
    const r = await api("GET", "/api/missions?limit=100" + (filter.value ? `&state=${encodeURIComponent(filter.value)}` : ""));
    list.replaceChildren();
    if (!r.items.length) list.append(h("p", { class: "muted", text: "No hay misiones." }));
    for (const m of r.items) {
      const end = m.finished_at || (ACTIVE.includes(m.state) ? r.now : null);
      list.append(h("a", { class: "item", href: `#/mision/${m.id}` },
        h("div", { class: "row between" }, h("span", { class: "t", text: m.title + (m.revision > 1 ? ` · r${m.revision}` : "") }), badge(m.state)),
        h("div", { class: "muted small mono" }, `${m.id} · intento ${m.attempt}/${m.max_attempts} · ${fmtDate(m.created_at)}`,
          m.started_at && end ? ` · ${fmtDur(end - m.started_at)}` : ""),
        m.error ? h("div", { class: "small bad", text: m.error }) : ""));
    }
  }
  filter.addEventListener("change", load);
  onEvent(debounce((ev) => { if (ev.mission_id) load(); }, 800));
  await load();
}

function highlight(code) {
  const pre = h("pre", { class: "code" });
  const KW = /\b(def|class|return|if|elif|else|for|while|in|not|and|or|import|from|as|try|except|finally|raise|with|lambda|yield|None|True|False|pass|break|continue|global|nonlocal|assert|is|del|async|await)\b/y;
  for (const line of code.split("\n")) {
    const ln = h("span", { class: "ln" });
    let i = 0, plain = "";
    const flush = () => { if (plain) { ln.append(plain); plain = ""; } };
    while (i < line.length) {
      const ch = line[i];
      if (ch === "#") { flush(); ln.append(h("span", { class: "tk-com", text: line.slice(i) })); i = line.length; break; }
      if (ch === '"' || ch === "'") {
        let j = i + 1;
        while (j < line.length && line[j] !== ch) { if (line[j] === "\\") j++; j++; }
        flush(); ln.append(h("span", { class: "tk-str", text: line.slice(i, j + 1) })); i = j + 1; continue;
      }
      if (/[0-9]/.test(ch) && !/[A-Za-z_0-9]/.test(line[i - 1] || "")) {
        const m = /[0-9][0-9_.eExXa-fA-F]*/y; m.lastIndex = i; const r = m.exec(line);
        flush(); ln.append(h("span", { class: "tk-num", text: r[0] })); i += r[0].length; continue;
      }
      if (/[A-Za-z_]/.test(ch) && !/[A-Za-z_0-9]/.test(line[i - 1] || "")) {
        const w = /[A-Za-z_][A-Za-z_0-9]*/y; w.lastIndex = i; const word = w.exec(line)[0];
        KW.lastIndex = 0;
        if (KW.test(word) && KW.lastIndex === word.length) { flush(); ln.append(h("span", { class: "tk-kw", text: word })); }
        else if (/^(def|class)\s+$/.test(line.slice(0, i).trimStart().slice(-6)) || /(def|class)\s+$/.test(line.slice(0, i))) { flush(); ln.append(h("span", { class: "tk-def", text: word })); }
        else plain += word;
        i += word.length; continue;
      }
      plain += ch; i++;
    }
    flush();
    pre.append(ln);
  }
  return pre;
}

async function viewMission(v, args) {
  const mid = args[0];
  const consoleBox = h("div", { class: "console" });
  const info = h("div", { class: "panel" });
  const attemptsBox = h("div", { class: "panel" });
  const filesBox = h("div", { class: "panel" });
  const viewer = h("div", { class: "panel", hidden: true });
  const webBox = h("div", { class: "panel", hidden: true });
  const apprBox = h("div", { class: "panel" });
  const exploreBox = h("div", { hidden: true });
  const projList = h("div"), projForm = h("div"), projDiff = h("div");
  const projectBox = h("div", { class: "panel", hidden: true }, projList, projForm, projDiff);
  v.replaceChildren(info, projectBox, exploreBox, h("div", { class: "panel" }, h("h2", { text: "Consola de progreso" }), consoleBox), attemptsBox, webBox, filesBox, viewer, apprBox);
  let data = null, lastEv = 0, clock = null;

  function addEvent(ev) {
    if (ev.id <= lastEv) return;
    lastEv = ev.id;
    const atBottom = consoleBox.scrollHeight - consoleBox.scrollTop - consoleBox.clientHeight < 40;
    consoleBox.append(h("div", { class: `k-${ev.kind}` }, h("time", { text: fmtTime(ev.ts) }), ev.message));
    if (atBottom) consoleBox.scrollTop = consoleBox.scrollHeight;
  }

  async function load() {
    data = await api("GET", `/api/missions/${encodeURIComponent(mid)}`);
    const m = data.mission;
    const active = ACTIVE.includes(m.state);
    const elapsed = h("dd", {});
    const loadedAt = Date.now();
    const tick = () => {
      const end = m.finished_at || (data.now + (Date.now() - loadedAt) / 1000);
      elapsed.textContent = m.started_at ? fmtDur(end - m.started_at) : "—";
    };
    clearInterval(clock);
    tick();
    if (active) { clock = setInterval(tick, 1000); S.timers.push(clock); }
    const actions = h("div", { class: "row" });
    if (m.state === "pendiente" || active) {
      actions.append(h("button", { class: "btn danger small", type: "button", text: "Cancelar misión", onclick: async () => {
        if (!confirm("¿Cancelar esta misión? Se detendrá la generación o las pruebas en curso.")) return;
        try { const r = await api("POST", `/api/missions/${mid}/cancel`, {}); toast(`Cancelación: ${r.result}`); load(); } catch (e) { toast(e.message, true); }
      } }));
    }
    if (["fallida", "cancelada", "interrumpida"].includes(m.state)) {
      actions.append(h("button", { class: "btn small", type: "button", text: "Reencolar (nueva misión)", onclick: async () => {
        if (!confirm("Se creará una nueva misión con la misma especificación y se ejecutará en el sandbox. ¿Continuar?")) return;
        try { const r = await api("POST", `/api/missions/${mid}/requeue`, {}); location.hash = `#/mision/${r.id}`; } catch (e) { toast(e.message, true); }
      } }));
    }
    actions.append(h("button", { class: "btn ghost small", type: "button", text: "Preguntar en el chat", onclick: async () => {
      const r = await api("POST", "/api/conversations", { title: `Sobre ${m.id}`, mission_id: m.id });
      location.hash = `#/chat/${r.id}`;
    } }));
    const spec = m.spec || {};
    info.replaceChildren(
      h("div", { class: "row between" }, h("h1", { text: m.title }), badge(m.state)),
      h("dl", { class: "kv" },
        h("dt", { text: "Identificador" }), h("dd", { class: "mono", text: m.id }),
        h("dt", { text: "Creada" }), h("dd", { text: `${fmtDate(m.created_at)} por ${m.created_by}` }),
        h("dt", { text: "Modelo" }), h("dd", { class: "mono", text: m.model }),
        h("dt", { text: spec.mode === "exploracion" ? "Paso" : "Intento" }), h("dd", { text: spec.mode === "exploracion" ? `${m.attempt} de ${spec.max_steps}` : `${m.attempt} de ${m.max_attempts}` }),
        h("dt", { text: "Tiempo" }), elapsed,
        h("dt", { text: spec.mode === "exploracion" ? "Tipo" : "Pruebas" }), h("dd", { text: spec.mode === "exploracion" ? `Exploración de «${spec.conn}» · solo lectura · hasta ${spec.max_steps} pasos, ${spec.max_queries} consultas, ${spec.max_minutes} min` : spec.mode === "web" ? `Página web / Tablero · ${(spec.checks || []).length} requisitos + verificaciones automáticas`
          : `${spec.mode === "script" ? "Programa (stdin/stdout)" : `Función ${spec.function}`} · ${(spec.cases || []).length} casos${spec.tests_py ? " + pruebas avanzadas" : ""}` }),
        m.error ? h("dt", { text: "Motivo" }) : "", m.error ? h("dd", { class: "bad", text: m.error }) : ""),
      h("details", {}, h("summary", { text: "Descripción" }), h("p", { style: "white-space:pre-wrap", text: m.description })),
      actions);
    renderAttempts();
    renderFiles();
    renderApprovals();
    renderWeb();
    if (["funcion", "script", "web"].includes(spec.mode || "funcion")) {
      projectBox.hidden = false;
      renderProject(m).catch((e) => toast(e.message, true));
    }
    if (spec.mode === "exploracion") {
      exploreBox.hidden = false;
      attemptsBox.hidden = true;
      apprBox.hidden = true;
      renderExploration(exploreBox, mid, m).catch((e) => toast(e.message, true));
    }
  }

  function renderAttempts() {
    attemptsBox.replaceChildren(h("h2", { text: "Historial de intentos" }));
    if (!data.attempts.length) { attemptsBox.append(h("p", { class: "muted small", text: "Todavía no hay intentos." })); return; }
    for (const a of data.attempts) {
      const res = a.result;
      const status = a.test_status || (a.finished_at ? "—" : "en curso");
      const det = h("details", { open: a.n === data.attempts.length },
        h("summary", {}, `Intento ${a.n} · `, h("span", { class: `badge ${status === "aprobadas" ? "ok" : (a.finished_at ? "bad" : "")}`, text: status }),
          a.total ? ` ${a.passed}/${a.total} pruebas` : "",
          a.gen_ms ? ` · generación ${fmtDur(a.gen_ms / 1000)}` : "", a.eval_tokens ? ` · ${a.eval_tokens} tokens` : ""));
      if (a.error) det.append(h("p", { class: "bad small", style: "white-space:pre-wrap", text: a.error }));
      if (a.sandbox) {
        det.append(h("p", { class: "muted small", text: `Sandbox: ${a.sandbox.status}${a.sandbox.reason ? " — " + a.sandbox.reason : ""} · límites verificados: ${a.sandbox.limits && a.sandbox.limits.verified ? "sí" : "no"} · limpieza verificada: ${a.sandbox.cleanup_verified ? "sí" : "no"}` }));
      }
      if (res && res.cases) {
        const tb = h("tbody");
        for (const c of res.cases) {
          tb.append(h("tr", {},
            h("td", {}, h("span", { class: `badge ${c.ok ? "ok" : "bad"}`, text: c.ok ? "ok" : c.status })),
            h("td", { class: "small", text: `${c.name || "#" + c.n}${c.visible ? "" : " (oculto)"}` }),
            h("td", { class: "mono small", text: c.kind === "script" ? JSON.stringify(c.stdin).slice(0, 120) : (c.args ? JSON.stringify(c.args).slice(0, 120) : "") }),
            h("td", { class: "mono small", text: c.got !== undefined ? String(c.got).slice(0, 200) : "" }),
            h("td", { class: "mono small", text: c.expected !== undefined ? String(c.expected).slice(0, 200) : "" }),
            h("td", { class: "small", style: "white-space:pre-wrap", text: [c.detail, c.tb, c.stderr].filter(Boolean).join("\n").slice(0, 1200) })));
        }
        det.append(h("div", { style: "overflow-x:auto" }, h("table", {}, h("thead", {}, h("tr", {}, ...["", "Caso", "Entrada", "Obtenido", "Esperado", "Detalle"].map((t) => h("th", { text: t })))), tb)));
        if (res.load_error) det.append(h("pre", { class: "console", text: res.load_error }));
      }
      attemptsBox.append(det);
    }
  }

  async function renderProject(m) {
    const pid = m.project_id || m.id;
    const p = await api("GET", `/api/projects/${encodeURIComponent(pid)}`);
    const spec = m.spec || {};
    const terminal = ["aprobada", "fallida", "cancelada", "interrumpida"].includes(m.state);
    const tb = h("tbody");
    for (const r of p.revisions) {
      const isWeb = r.mode === "web";
      tb.append(h("tr", {},
        h("td", { class: "mono small" }, h("a", { href: `#/mision/${r.id}`, text: `r${r.revision}` }), r.id === m.id ? " ◀" : ""),
        h("td", { class: "small pre-wrap", text: r.instruction || "Versión original" }),
        h("td", {}, badge(r.state), r.vigente ? h("span", { class: "badge ok", text: "vigente" }) : ""),
        h("td", { class: "small muted", text: `${fmtDate(r.created_at)}${r.parent_id ? ` · base ${r.parent_id}:${r.base_ref}` : ""}` }),
        h("td", {}, h("div", { class: "row" },
          r.state === "aprobada" && !r.vigente ? h("button", { class: "btn ghost small", type: "button", text: "Hacer vigente", onclick: async () => {
            if (!confirm(`¿Usar r${r.revision} como versión vigente del proyecto? No borra nada: solo cambia la base por defecto de las próximas mejoras.`)) return;
            try { await api("POST", `/api/projects/${pid}/current`, { mission_id: r.id }); toast("Versión vigente actualizada"); renderProject(m); } catch (e) { toast(e.message, true); }
          } }) : "",
          isWeb && r.state === "aprobada" ? h("a", { class: "btn ghost small", href: `/api/missions/${r.id}/zip?attempt=final`, text: "ZIP" }) : "",
          r.id !== m.id ? h("a", { class: "btn ghost small", href: `#/mision/${r.id}`, text: isWeb ? "Abrir / vista previa" : "Abrir" }) : ""))));
    }
    const versions = [];
    for (const r of p.revisions) {
      if (r.final_attempt) versions.push([`${r.id}:final`, `r${r.revision} final${r.vigente ? " (vigente)" : ""}`]);
      if (r.parent_id) versions.push([`${r.id}:base`, `r${r.revision} base (${r.parent_id}:${r.base_ref})`]);
    }
    for (const a of data.attempts) versions.push([`${m.id}:intento-${a.n}`, `r${m.revision} intento ${a.n}`]);
    const sa = h("select", {}, ...versions.map(([v, t]) => h("option", { value: v, text: t })));
    const sb = h("select", {}, ...versions.map(([v, t]) => h("option", { value: v, text: t })));
    if (versions.length > 1) { sa.value = versions[0][0]; sb.value = versions[versions.length - 1][0]; }
    const cur = p.revisions.find((r) => r.vigente);
    projList.replaceChildren(
      h("div", { class: "row between" }, h("h2", { text: `Proyecto ${pid} · revisión r${m.revision || 1}` }),
        terminal ? h("button", { class: "btn small", type: "button", text: "Mejorar esta misión", onclick: () => openImprove(m, p) }) : ""),
      m.instruction ? h("div", { class: "banner", text: `Instrucción de esta revisión: ${m.instruction}` }) : "",
      h("p", { class: "muted small", text: `Versión vigente: ${cur ? `r${cur.revision} (${cur.id})` : "ninguna aprobada todavía"}. Cada mejora crea una revisión nueva; las versiones anteriores no se modifican y una revisión fallida no reemplaza a la vigente.` }),
      h("div", { class: "scroll-x" }, h("table", {}, tb)),
      versions.length > 1 ? h("div", { class: "row mt" }, h("span", { class: "small muted", text: "Comparar versiones:" }), sa, sb,
        h("button", { class: "btn ghost small", type: "button", text: "Ver diferencias", onclick: async () => {
          try {
            const d = await api("GET", `/api/versions/diff?a=${encodeURIComponent(sa.value)}&b=${encodeURIComponent(sb.value)}`);
            projDiff.replaceChildren(h("h3", { class: "mt", text: `${d.a} → ${d.b}` }),
              ...d.files.map((f) => h("details", { class: "item", open: f.status === "modificado" },
                h("summary", {}, h("span", { class: "mono", text: f.path }), " ", h("span", { class: `badge ${f.status === "igual" ? "" : "ok"}`, text: f.status })),
                f.diff.length ? h("pre", { class: "console diff" }, ...f.diff.map((line) => h("div", {
                  class: line.startsWith("+++") || line.startsWith("---") || line.startsWith("@@") ? "hdr" : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "", text: line }))) : "")));
          } catch (e) { toast(e.message, true); }
        } })) : "");
  }

  function openImprove(m, p) {
    const spec = m.spec || {};
    const mode = spec.mode || "funcion";
    const instr = h("textarea", { rows: 4, maxlength: 4000, placeholder: "Ej.: «La pelota rebota demasiado pronto contra dos paredes. Corregí las colisiones para que toque exactamente los cuatro bordes»." });
    const base = h("select", {});
    const cur = p.revisions.find((r) => r.vigente);
    if (cur) base.append(h("option", { value: "vigente", text: `Versión vigente del proyecto (r${cur.revision})` }));
    if (m.final_attempt) base.append(h("option", { value: "final", text: `Esta misión (r${m.revision || 1}): versión final aprobada` }));
    for (const a of data.attempts.slice().reverse()) if (a.syntax_ok) base.append(h("option", { value: `intento-${a.n}`, text: `Esta misión: intento ${a.n} (${a.test_status || "sin probar"})` }));
    if (!base.options.length) { toast("Esta misión no tiene ninguna versión con archivos para mejorar", true); return; }
    const cases = h("div", { class: "cases" });
    const tests = h("textarea", { class: "code", rows: 4, placeholder: "def test_nuevo(sol):\n    assert ..." });
    const refresh = h("input", { type: "checkbox" });
    const attempts = h("input", { type: "number", min: 1, max: 6, value: 3 });
    const reviewed = h("input", { type: "checkbox" });
    const auth = h("input", { type: "checkbox" });
    const err = h("p", { class: "bad small" });
    const inherited = mode === "web" ? (spec.checks || []).length : (spec.cases || []).length;
    const send = h("button", { class: "btn", type: "button", text: "Crear revisión", onclick: async () => {
      err.textContent = "";
      let rows;
      try { rows = [...cases.children].map((r) => r.read()); } catch (e) { err.textContent = e.message; return; }
      const body = { instruction: instr.value, max_attempts: parseInt(attempts.value, 10) || 3,
        cases_reviewed: reviewed.checked, authorize_execution: auth.checked, refresh_data: refresh.checked };
      if (base.value === "vigente") body.base = "vigente"; else body.base_ref = base.value;
      if (mode === "web") body.checks = rows; else body.cases = rows;
      if (mode === "funcion" && tests.value.trim()) body.tests_py = tests.value;
      if (!confirm("Se creará una revisión nueva del proyecto: la IA modificará la versión base elegida y las pruebas se ejecutarán solo en el sandbox. ¿Continuar?")) return;
      send.disabled = true;
      try { const r = await api("POST", `/api/missions/${m.id}/revisions`, body); location.hash = `#/mision/${r.id}`; }
      catch (e) { err.textContent = e.message; }
      finally { send.disabled = false; }
    } });
    projForm.replaceChildren(h("div", { class: "panel mt" },
      h("h2", { text: "Mejorar esta misión" }),
      h("p", { class: "muted small", text: "Una revisión continúa el proyecto con una instrucción nueva. No es un intento más: tiene sus propios intentos de reparación, conserva las pruebas anteriores y guarda una versión nueva sin tocar las anteriores." }),
      h("label", { text: "¿Qué querés cambiar?" }), instr,
      h("label", { text: "Partir de" }), base,
      h("h3", { class: "mt", text: `Pruebas: se conservan las ${inherited} anteriores; podés agregar nuevas` }),
      cases,
      h("button", { class: "btn ghost small", type: "button", text: "+ Agregar prueba nueva", onclick: () => cases.append(makeCaseRow(mode, mode === "web" ? { tipo: "selector" } : {})) }),
      mode === "funcion" ? h("details", { class: "mt" }, h("summary", { text: "Pruebas avanzadas adicionales (Python)" }), tests) : "",
      spec.data_source ? h("label", { class: "check" }, refresh, `Actualizar las instantáneas de datos de «${spec.data_source.conn}» (consultas de solo lectura por el intermediario)`) : "",
      h("label", { text: "Máximo de intentos de reparación de esta revisión" }), attempts,
      h("label", { class: "check" }, reviewed, "Revisé las pruebas nuevas (si agregué alguna)."),
      h("label", { class: "check" }, auth, "Autorizo ejecutar el código resultante únicamente dentro del ejecutor aislado."),
      err, h("div", { class: "row" }, send, h("button", { class: "btn ghost", type: "button", text: "Cancelar", onclick: () => projForm.replaceChildren() }))));
    instr.focus();
  }

  let webSelected = null, webChosen = false;
  renderWeb.key = null;
  function renderWeb() {
    const m = data.mission;
    if (!m.spec || m.spec.mode !== "web") { webBox.hidden = true; return; }
    webBox.hidden = false;
    const versions = data.attempts.filter((a) => data.files.some((f) => f.rel_path.startsWith(`intento-${a.n}/web/`))).map((a) => String(a.n));
    if (data.files.some((f) => f.rel_path.startsWith("final/web/"))) versions.push("final");
    if (!versions.length) {
      webBox.replaceChildren(h("h2", { text: "Proyecto web" }), h("p", { class: "muted small", text: "Todavía no hay archivos del proyecto." }));
      return;
    }
    const key = versions.join(",");
    if (key === renderWeb.key) return;  // no reconstruir (ni cerrar la vista previa) si nada cambió
    renderWeb.key = key;
    if (!webChosen || !versions.includes(webSelected)) webSelected = versions[versions.length - 1];
    const sel = h("select", {}, ...versions.map((v) => h("option", { value: v, text: v === "final" ? "Versión aprobada (final)" : `Intento ${v}` })));
    sel.value = webSelected;
    sel.addEventListener("change", () => { webSelected = sel.value; webChosen = true; zip.href = zipHref(); frameBox.replaceChildren(); });
    const zipHref = () => `/api/missions/${encodeURIComponent(mid)}/zip?attempt=${encodeURIComponent(sel.value)}`;
    const zip = h("a", { class: "btn ghost small", href: zipHref(), text: "Descargar ZIP" });
    const frameBox = h("div", { class: "mt" });
    const open = h("button", { class: "btn small", type: "button", text: "Vista previa", onclick: async () => {
      try {
        const r = await api("POST", `/api/missions/${encodeURIComponent(mid)}/preview`, { attempt: sel.value });
        if (location.protocol === "https:" && !r.origin) {
          frameBox.replaceChildren(h("p", { class: "bad small", text: "Con HTTPS la vista previa necesita web.preview_origin (ver INSTALL.md). Mientras tanto, use la descarga ZIP." }));
          return;
        }
        const url = (r.origin || `${location.protocol}//${location.hostname}:${r.port}`) + r.path;
        frameBox.replaceChildren(
          h("div", { class: "banner warn", text: "Vista previa NO confiable: corre en un origen separado, aislada del panel, sin acceso a la sesión ni a la API. Sus datos de localStorage se comparten entre vistas previas." }),
          h("iframe", { class: "preview", src: url, title: `Vista previa ${r.label}`, referrerpolicy: "no-referrer",
            sandbox: "allow-scripts allow-same-origin allow-forms allow-modals", allow: "" }),
          h("p", { class: "small" }, h("a", { href: url, target: "_blank", rel: "noopener noreferrer", text: "Abrir en una pestaña aparte" }),
            h("span", { class: "muted", text: ` · el enlace vence en ${Math.round(r.expires_in / 60)} min` })));
      } catch (e) { frameBox.replaceChildren(h("p", { class: "bad small", text: e.message })); }
    } });
    webBox.replaceChildren(h("h2", { text: "Proyecto web" }),
      h("div", { class: "row" }, sel, open, zip), frameBox);
  }

  function renderFiles() {
    filesBox.replaceChildren(h("h2", { text: "Archivos" }));
    if (!data.files.length) { filesBox.append(h("p", { class: "muted small", text: "Sin archivos todavía." })); return; }
    const tb = h("tbody");
    for (const f of data.files) {
      tb.append(h("tr", {},
        h("td", { class: "mono small", text: f.rel_path }), h("td", { class: "small", text: f.kind }),
        h("td", { class: "small", text: `${f.size} B` }),
        h("td", {}, h("div", { class: "row" },
          h("button", { class: "btn ghost small", type: "button", text: "Ver", onclick: () => openFile(f) }),
          h("a", { class: "btn ghost small", href: `/api/files/${f.id}/download`, text: "Descargar" })))));
    }
    filesBox.append(h("div", { style: "overflow-x:auto" }, h("table", { class: "files" }, tb)));
    const versions = data.attempts.filter((a) => data.files.some((f) => f.rel_path === `intento-${a.n}/solucion.py`)).map((a) => a.n);
    if (versions.length >= 2) {
      const sa = h("select", {}, ...versions.map((n) => h("option", { value: n, text: `Intento ${n}` })));
      const sb = h("select", {}, ...versions.map((n) => h("option", { value: n, text: `Intento ${n}` })));
      sb.value = versions[versions.length - 1];
      filesBox.append(h("div", { class: "row", style: "margin-top:.6rem" }, h("span", { class: "small muted", text: "Comparar versiones:" }), sa, sb,
        h("button", { class: "btn ghost small", type: "button", text: "Ver diferencias", onclick: async () => {
          try {
            const r = await api("GET", `/api/missions/${mid}/diff?a=${sa.value}&b=${sb.value}`);
            const pre = h("pre", { class: "console diff" });
            for (const line of r.diff) {
              const cls = line.startsWith("+++") || line.startsWith("---") || line.startsWith("@@") ? "hdr" : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "";
              pre.append(h("div", { class: cls, text: line }));
            }
            if (!r.diff.length) pre.append(h("div", { text: "Sin diferencias." }));
            viewer.hidden = false;
            viewer.replaceChildren(h("h2", { text: `Diferencias intento ${sa.value} → ${sb.value}` }), pre);
            viewer.scrollIntoView({ behavior: "smooth" });
          } catch (e) { toast(e.message, true); }
        } })));
    }
  }

  async function openFile(f) {
    try {
      const r = await api("GET", `/api/files/${f.id}`);
      viewer.hidden = false;
      const body = r.content === null ? h("p", { class: "muted", text: "Archivo binario: use Descargar." })
        : (f.rel_path.endsWith(".py") ? highlight(r.content) : h("pre", { class: "console", text: r.content }));
      viewer.replaceChildren(
        h("div", { class: "row between" }, h("h2", { class: "mono", text: f.rel_path }),
          h("div", { class: "row" },
            h("button", { class: "btn ghost small", type: "button", text: "Pedir explicación", onclick: async () => {
              const c = await api("POST", "/api/conversations", { title: `Explicación ${f.rel_path}`, mission_id: mid });
              await api("POST", `/api/conversations/${c.id}/explain`, { file_id: f.id });
              location.hash = `#/chat/${c.id}`;
            } }),
            h("a", { class: "btn ghost small", href: `/api/files/${f.id}/download`, text: "Descargar" }))),
        h("p", { class: "muted small mono", text: `SHA-256 ${f.sha256}` }), body);
      viewer.scrollIntoView({ behavior: "smooth" });
    } catch (e) { toast(e.message, true); }
  }

  function renderApprovals() {
    const m = data.mission;
    apprBox.replaceChildren(h("h2", { text: "Aprobaciones humanas" }),
      h("div", { class: "banner warn", text: "Separación de responsabilidades: las pruebas aisladas ya se ejecutaron. Una solicitud de instalación o cambio requiere aprobación humana y, aun aprobada, NO se ejecuta automáticamente: se genera un paquete para aplicar manualmente." }));
    for (const a of data.approvals) {
      apprBox.append(h("div", { class: "item" }, h("div", { class: "row between" }, h("span", { text: `#${a.id} ${a.kind}` }), badge(a.state)),
        h("div", { class: "small", style: "white-space:pre-wrap", text: a.detail }),
        h("div", { class: "muted small", text: `Solicitada por ${a.requested_by} ${fmtDate(a.requested_at)}${a.decided_by ? ` · decidida por ${a.decided_by} ${fmtDate(a.decided_at)}` : ""}` })));
    }
    if (m.state === "aprobada") {
      const kind = h("select", {}, h("option", { value: "instalacion", text: "Instalación" }), h("option", { value: "cambio", text: "Cambio en un sistema" }));
      const detail = h("textarea", { rows: 3, maxlength: 2000, placeholder: "¿Dónde se instalaría y para qué?" });
      apprBox.append(h("label", { text: "Solicitar instalación o cambio real" }), kind, h("div", { style: "margin-top:.4rem" }, detail),
        h("button", { class: "btn small", type: "button", style: "margin-top:.5rem", text: "Crear solicitud", onclick: async () => {
          try { await api("POST", `/api/missions/${mid}/approvals`, { kind: kind.value, detail: detail.value }); toast("Solicitud creada"); load(); } catch (e) { toast(e.message, true); }
        } }));
    } else {
      apprBox.append(h("p", { class: "muted small", text: "Solo las misiones aprobadas por las pruebas pueden solicitar instalación." }));
    }
  }

  const evs = await api("GET", `/api/missions/${encodeURIComponent(mid)}/events`);
  for (const ev of evs.items) addEvent(ev);
  await load();
  const reload = debounce(load, 700);
  onEvent((ev) => { if (ev.mission_id === mid) { addEvent(ev); reload(); } });
}

// Editor de un caso/requisito de prueba (usado por «Nueva misión» y «Mejorar esta misión»).
function makeCaseRow(modeValue, c = {}) {
  const row = h("div", { class: "case" });
  const vis = h("input", { type: "checkbox" });
  vis.checked = c.visible !== false;
  if (modeValue === "web") {
    const tipo = h("select", {}, ...[["selector", "Debe existir el elemento (selector)"], ["texto", "Debe mostrar el texto"],
      ["js_contiene", "El JavaScript debe usar"], ["css_contiene", "El CSS debe incluir"]].map(([v, t]) => h("option", { value: v, text: t })));
    tipo.value = c.tipo || "selector";
    const valor = h("input", { type: "text", class: "code", maxlength: 200, value: c.valor || "",
      placeholder: "Ej.: button   ·   textarea, input[type=text]   ·   #lista   ·   localStorage" });
    row.append(h("div", { class: "grid2" }, h("div", {}, h("label", { text: "Tipo de requisito" }), tipo),
      h("div", {}, h("label", { text: "Valor" }), valor)));
    row.read = () => {
      if (!valor.value.trim()) throw new Error("Hay un requisito sin valor");
      return { tipo: tipo.value, valor: valor.value.trim(), visible: vis.checked };
    };
  } else if (modeValue === "funcion") {
    const args = h("input", { type: "text", class: "code", placeholder: 'Ej.: 2, 3   o   "hola", [1, 2]' });
    args.value = c.args ? c.args.map((a) => JSON.stringify(a)).join(", ") : "";
    const exp = h("input", { type: "text", class: "code", placeholder: "Ej.: 5   o   \"texto\"   o   [1, 2]" });
    exp.value = c.expect !== undefined ? JSON.stringify(c.expect) : "";
    const raises = h("input", { type: "text", class: "code", placeholder: "ValueError", value: c.raises || "" });
    row.append(h("div", { class: "grid2" },
      h("div", {}, h("label", { text: "Argumentos (JSON separados por coma)" }), args),
      h("div", {}, h("label", { text: "Resultado esperado (JSON)" }), exp)),
      h("details", {}, h("summary", { class: "small", text: "Esperar una excepción en lugar de un resultado" }), raises));
    row.read = () => {
      let parsedArgs, out;
      try { parsedArgs = JSON.parse(`[${args.value}]`); } catch (_) { throw new Error(`Argumentos inválidos: ${args.value}`); }
      out = { args: parsedArgs, visible: vis.checked };
      if (raises.value.trim()) out.raises = raises.value.trim();
      else {
        try { out.expect = JSON.parse(exp.value); } catch (_) { throw new Error(`Resultado esperado inválido: ${exp.value || "(vacío)"}. Los textos van entre comillas.`); }
      }
      return out;
    };
  } else {
    const sin = h("textarea", { class: "code", rows: 2, placeholder: "Entrada" });
    sin.value = c.stdin || "";
    const sout = h("textarea", { class: "code", rows: 2, placeholder: "Salida esperada" });
    sout.value = c.stdout || "";
    row.append(h("div", { class: "grid2" }, h("div", {}, h("label", { text: "Entrada (stdin)" }), sin), h("div", {}, h("label", { text: "Salida esperada" }), sout)));
    row.read = () => ({ stdin: sin.value, stdout: sout.value, visible: vis.checked });
  }
  row.append(h("div", { class: "row between" },
    h("label", { class: "check" }, vis, "Mostrar este ejemplo al modelo (si no, es un caso oculto)"),
    h("button", { class: "btn ghost small", type: "button", text: "Quitar", onclick: () => row.remove() })));
  return row;
}

// ---------------------------------------------------------------- nueva misión
async function viewNew(v) {
  const d = S.draft || {};
  S.draft = null;
  const title = h("input", { type: "text", maxlength: 120, value: d.title || "", placeholder: "Ej.: Calcular el dígito verificador de un CUIT" });
  const desc = h("textarea", { rows: 6, maxlength: 6000, placeholder: "Describí qué tiene que hacer el programa, con detalles y casos especiales." });
  desc.value = d.description || "";
  const mode = h("select", {}, h("option", { value: "funcion", text: "Función de Python (recibe valores y devuelve un resultado)" }),
    h("option", { value: "script", text: "Programa (lee texto de entrada e imprime una salida)" }),
    h("option", { value: "web", text: "Página web / Tablero (HTML, CSS y JavaScript)" }));
  mode.value = d.mode || "funcion";
  const fn = h("input", { type: "text", class: "code", maxlength: 64, value: d.function || "", placeholder: "nombre_de_funcion" });
  const fnWrap = h("div", {}, h("label", { text: "Nombre de la función" }), fn);
  const cases = h("div", { class: "cases" });
  const attempts = h("input", { type: "number", min: 1, max: 6, value: 3 });
  const tests = h("textarea", { class: "code", rows: 6, placeholder: "def test_ejemplo(sol):\n    assert sol.mi_funcion(2) == 4" });
  const reviewed = h("input", { type: "checkbox" });
  const authorize = h("input", { type: "checkbox" });
  const err = h("p", { class: "bad small" });
  const submit = h("button", { class: "btn", type: "button", text: "Crear misión" });

  const caseRow = (c = {}) => makeCaseRow(mode.value, c);
  const casesTitle = h("h2", { style: "margin-top:1rem" });
  const casesHelp = h("p", { class: "muted small" });
  const advanced = h("details", { style: "margin-top:.8rem" }, h("summary", { text: "Pruebas avanzadas en Python (opcional, solo modo función)" }),
    h("p", { class: "muted small", text: "Funciones test_*(sol). Use sol.nombre(args) para llamar a la solución. Corren dentro del sandbox." }), tests);
  const example = h("button", { class: "btn ghost small", type: "button", text: "Cargar ejemplo: tablero de mensajes", onclick: () => {
    title.value = "Tablero de mensajes";
    desc.value = "Tablero web con tema oscuro. Un área de texto para escribir un mensaje y un botón para agregarlo. " +
      "Los mensajes se muestran en una lista (el más nuevo arriba) con la cantidad de palabras de cada uno, y un " +
      "contador del total de palabras. Los mensajes se guardan en localStorage y se recuperan al recargar. " +
      "Botón para borrar todo con confirmación. Diseño cómodo en celular.";
    resetCases([{ tipo: "selector", valor: "textarea" }, { tipo: "selector", valor: "button" },
      { tipo: "selector", valor: "ul, ol" }, { tipo: "js_contiene", valor: "localStorage" },
      { tipo: "js_contiene", valor: "addEventListener" }, { tipo: "css_contiene", valor: "background" }]);
  } });
  function resetCases(list) {
    cases.replaceChildren();
    const web = mode.value === "web";
    for (const c of list.length ? list : (web ? [{ tipo: "selector" }, { tipo: "js_contiene" }] : [{}, {}])) cases.append(caseRow(c));
    fnWrap.hidden = mode.value !== "funcion";
    advanced.hidden = mode.value !== "funcion";
    example.hidden = !web;
    casesTitle.textContent = web ? "Requisitos verificables de la página" : "Casos de prueba de aceptación";
    casesHelp.textContent = web
      ? "Además se verifican siempre, dentro del sandbox: estructura HTML, sintaxis de HTML/CSS/JS, que las referencias entre archivos existan, y que no haya recursos externos ni JavaScript en línea. El comportamiento en el navegador se revisa en la vista previa."
      : "Estos casos los definís vos y el modelo no puede modificarlos. La misión solo se aprueba si todos pasan en el sandbox.";
  }
  mode.addEventListener("change", () => resetCases([]));
  resetCases(d.cases || []);

  submit.addEventListener("click", async () => {
    err.textContent = "";
    let caseData;
    try { caseData = [...cases.children].map((r) => r.read()); } catch (e) { err.textContent = e.message; return; }
    const body = {
      title: title.value, description: desc.value, mode: mode.value, function: fn.value.trim(),
      cases: mode.value === "web" ? [] : caseData, checks: mode.value === "web" ? caseData : undefined,
      tests_py: tests.value, max_attempts: parseInt(attempts.value, 10) || 3,
      cases_reviewed: reviewed.checked, authorize_execution: authorize.checked,
    };
    if (d.conversation_id) body.conversation_id = d.conversation_id;
    if (d.seed && mode.value === "web") body.seed = d.seed;
    if (!confirm("Se encolará la misión: el modelo generará código y se ejecutará SOLO dentro del sandbox aislado para correr las pruebas. ¿Confirmás?")) return;
    submit.disabled = true;
    try {
      const r = await api("POST", "/api/missions", body);
      location.hash = `#/mision/${r.id}`;
    } catch (e) { err.textContent = e.message; }
    finally { submit.disabled = false; }
  });

  v.replaceChildren(h("div", { class: "panel" },
    h("h1", { text: "Nueva misión" }),
    d.title ? h("div", { class: "banner warn", text: "Borrador generado por la IA: revisá con cuidado los casos de prueba; son los que deciden si el código se aprueba." }) : "",
    d.seed ? h("div", { class: "banner", text: `Datos: al crear la misión se ejecutarán, por el intermediario de solo lectura, las consultas de la aplicación propuesta F${d.seed.fact_id} sobre «${d.seed.conn}», y se guardarán como instantáneas JSON inmutables dentro del proyecto. El JavaScript generado no recibe credenciales ni acceso a la base.` }) : "",
    h("label", { text: "Título" }), title,
    h("label", { text: "Descripción del trabajo" }), desc,
    h("label", { text: "Tipo de resultado" }), mode, fnWrap, h("div", { style: "margin-top:.5rem" }, example),
    casesTitle, casesHelp,
    cases,
    h("button", { class: "btn ghost small", type: "button", text: "+ Agregar", onclick: () => cases.append(caseRow({})) }),
    advanced,
    h("label", { text: "Máximo de intentos" }), attempts,
    h("label", { class: "check" }, reviewed, "Revisé los casos de prueba y son correctos."),
    h("label", { class: "check" }, authorize, "Autorizo ejecutar el código generado únicamente dentro del ejecutor aislado (sin red, sin acceso a mis archivos)."),
    err, submit));
}

// ---------------------------------------------------------------- aprobaciones
async function viewApprovals(v) {
  const r = await api("GET", "/api/approvals");
  const list = h("div", { class: "list" });
  for (const a of r.items) {
    const note = h("input", { type: "text", maxlength: 1000, placeholder: "Nota (opcional)" });
    const decide = (decision) => async () => {
      const msg = decision === "aprobar"
        ? "¿Aprobar? Esto NO instala ni ejecuta nada: solo genera un paquete verificado para aplicar manualmente."
        : "¿Rechazar esta solicitud?";
      if (!confirm(msg)) return;
      try { const res = await api("POST", `/api/approvals/${a.id}/decide`, { decision, note: note.value, confirm: true }); toast(res.nota || "Listo"); route(); } catch (e) { toast(e.message, true); }
    };
    list.append(h("div", { class: "item" },
      h("div", { class: "row between" }, h("a", { href: `#/mision/${a.mission_id}`, text: `${a.title} (${a.mission_id})` }), badge(a.state)),
      h("div", { class: "small", style: "white-space:pre-wrap", text: `${a.kind}: ${a.detail}` }),
      h("div", { class: "muted small", text: `Solicitada por ${a.requested_by} ${fmtDate(a.requested_at)}${a.note ? " · nota: " + a.note : ""}` }),
      a.state === "pendiente" ? h("div", { class: "row", style: "margin-top:.5rem" }, note,
        h("button", { class: "btn small", type: "button", text: "Aprobar", onclick: decide("aprobar") }),
        h("button", { class: "btn danger small", type: "button", text: "Rechazar", onclick: decide("rechazar") })) : ""));
  }
  v.replaceChildren(h("h1", { text: "Aprobaciones" }),
    h("div", { class: "banner warn", text: "En esta versión ninguna aprobación ejecuta cambios en el sistema anfitrión. La aplicación real se hace manualmente por SSH." }),
    r.items.length ? list : h("p", { class: "muted", text: "No hay solicitudes." }));
}

// ---------------------------------------------------------------- estado
async function viewStatus(v) {
  async function load() {
    const [s, act] = await Promise.all([api("GET", "/api/status"), api("GET", "/api/activity")]);
    const gb = (b) => (b ? (b / 1073741824).toFixed(2) + " GiB" : "—");
    const o = s.ollama;
    const q = s.queue || {};
    const checks = h("tbody");
    for (const c of s.sandbox.checks || []) {
      checks.append(h("tr", {}, h("td", {}, h("span", { class: `badge ${c.ok ? "ok" : "bad"}`, text: c.ok ? "ok" : "falla" })),
        h("td", { class: "small", text: c.name }), h("td", { class: "small mono", text: c.detail })));
    }
    const acts = h("tbody");
    for (const a of act.items.slice(0, 40)) {
      acts.append(h("tr", {}, h("td", { class: "small", text: fmtDate(a.ts) }), h("td", { class: "small", text: a.username || "" }),
        h("td", { class: "small", text: a.action }), h("td", { class: "small mono", text: a.target || "" })));
    }
    v.replaceChildren(h("h1", { text: "Estado del sistema" }),
      h("div", { class: "grid2" },
        h("div", { class: "panel" }, h("h2", { text: "Constructor IA" }), h("dl", { class: "kv" },
          h("dt", { text: "Versión" }), h("dd", { text: s.version }),
          h("dt", { text: "Activo desde" }), h("dd", { text: fmtDur(s.uptime_s) }),
          h("dt", { text: "Trabajadores" }), h("dd", { text: `misiones ${s.workers.missions ? "activo" : "DETENIDO"} · chat ${s.workers.chat ? "activo" : "DETENIDO"}` }),
          h("dt", { text: "En ejecución" }), h("dd", { class: "mono", text: s.running || "ninguna" }),
          h("dt", { text: "Inferencia" }), h("dd", { text: s.inference || "libre" }),
          h("dt", { text: "Cola" }), h("dd", { text: `${q.pendiente || 0} pendientes · ${q.aprobada || 0} aprobadas · ${q.fallida || 0} fallidas` }),
          h("dt", { text: "Último error" }), h("dd", { class: s.last_error ? "bad small" : "", text: s.last_error ? `${fmtDate(s.last_error.ts)} ${s.last_error.mission || ""}: ${s.last_error.error}` : "ninguno" }))),
        h("div", { class: "panel" }, h("h2", { text: "Ollama CPU" }), h("dl", { class: "kv" },
          h("dt", { text: "Disponible" }), h("dd", { class: o.available ? "ok" : "bad", text: o.available ? `sí (v${o.version})` : `no — ${o.error || ""}` }),
          h("dt", { text: "URL" }), h("dd", { class: "mono", text: o.url }),
          h("dt", { text: "Constructor" }), h("dd", { class: "mono", text: `${s.models.constructor}${o.available ? (o.builder_present ? " ✓" : " (no instalado)") : ""}` }),
          h("dt", { text: "Chat" }), h("dd", { class: "mono", text: `${s.models.chat}${o.available ? (o.chat_present ? " ✓" : " (no instalado)") : ""}` }),
          h("dt", { text: "Cargado" }), h("dd", { class: "mono", text: (o.loaded || []).join(", ") || "ninguno" }))),
        h("div", { class: "panel" }, h("h2", { text: "Equipo" }), h("dl", { class: "kv" },
          h("dt", { text: "Memoria disponible" }), h("dd", { text: `${gb(s.memory.MemAvailable)} de ${gb(s.memory.MemTotal)}` }),
          h("dt", { text: "Swap libre" }), h("dd", { text: `${gb(s.memory.SwapFree)} de ${gb(s.memory.SwapTotal)}` }),
          h("dt", { text: "Carga" }), h("dd", { text: `${s.load.join(" / ")} (${s.cpus} CPU)` })))),
      h("div", { class: "panel" }, h("div", { class: "row between" }, h("h2", { text: "Ejecutor aislado" }),
          h("button", { class: "btn ghost small", type: "button", text: "Repetir autodiagnóstico", onclick: async () => {
            try { const r = await api("POST", "/api/selftest", {}); toast(r.ok ? "Aislamiento operativo" : "Ejecución bloqueada", !r.ok); load(); } catch (e) { toast(e.message, true); }
          } })),
        h("p", { class: s.sandbox.enabled ? "ok" : "bad", text: s.sandbox.enabled ? "Ejecución de pruebas HABILITADA" : `Ejecución BLOQUEADA: ${s.sandbox.reason}` }),
        h("p", { class: "muted small", text: `Limitador: ${s.sandbox.limiter} · memoria ${s.sandbox.limits.memory_mb} MiB · tareas ${s.sandbox.limits.tasks} · CPU ${s.sandbox.limits.cpu_percent}% · tiempo ${s.sandbox.limits.timeout_s} s` }),
        h("div", { style: "overflow-x:auto" }, h("table", {}, checks))),
      h("div", { class: "panel" }, h("h2", { text: "Actividad reciente" }), h("div", { style: "overflow-x:auto" }, h("table", {}, acts))));
  }
  await load();
  every(30000, load);
}

// ---------------------------------------------------------------- explorador
const DEMO_EXPLORACION = "Explorá la base de datos que tenés disponible. No te voy a explicar qué contiene. Descubrí su estructura, investigá su información y construí un mapa de lo que encontrás. Identificá relaciones, patrones y oportunidades para desarrollar herramientas útiles. Cuando termines, presentame tus descubrimientos y tres aplicaciones que podrías construir.";
const CERT = { comprobado: "comprobado", inferencia: "inferencia", hipotesis: "hipótesis", descartado: "descartado", sin_verificar: "sin verificar" };
const QUALITY = { cumplido: "ok", parcial: "", insuficiente: "bad" };
const certBadge = (c) => h("span", { class: `badge cert-${c}`, text: CERT[c] || c });

function factItem(f, extra) {
  return h("div", { class: "item" },
    h("div", { class: "row between" }, h("span", { class: "small mono", text: `F${f.id}` }), certBadge(f.certainty)),
    h("div", { class: "small pre-wrap", text: f.text }),
    f.data && f.data.verificacion && f.data.verificacion !== "respaldada" ? h("div", { class: "bad small", text: `No respaldada: ${f.data.detalle || f.data.verificacion}` }) : "",
    f.evidence && f.evidence.length ? h("div", { class: "muted small", text: `Evidencia: ${f.evidence.join(", ")}` }) : "",
    extra || "");
}

function resultTable(res) {
  if (!res) return "";
  if (res.status !== "ok") return h("p", { class: "bad small", text: `${res.status}: ${res.error || ""}` });
  const tb = h("tbody");
  for (const r of (res.rows || []).slice(0, 10)) tb.append(h("tr", {}, ...r.map((c) => h("td", { class: "mono small", text: c === null ? "NULL" : String(c) }))));
  return h("div", { class: "scroll-x" }, h("table", {}, h("thead", {}, h("tr", {}, ...(res.columns || []).map((c) => h("th", { text: c })))), tb),
    h("p", { class: "muted small", text: `${res.row_count} filas${res.truncated ? ` (recortado: ${res.truncated_reason})` : ""} · ${res.ms} ms` }));
}

async function renderExploration(box, mid, m) {
  const d = await api("GET", `/api/missions/${encodeURIComponent(mid)}/exploration`);
  const p = d.progress;
  const elapsed = m.started_at ? ((m.finished_at || Date.now() / 1000) - m.started_at) / 60 : 0;
  const last = d.steps[d.steps.length - 1];
  const qById = Object.fromEntries(d.queries.map((q) => [q.id, q]));
  const actions = h("div", { class: "row" });
  if (ACTIVE.includes(m.state) || m.state === "pendiente") {
    actions.append(h("button", { class: "btn danger small", type: "button", text: "Detener exploración", onclick: async () => {
      if (!confirm("¿Detener la exploración ahora? Se interrumpe la consulta en curso y se guarda un informe parcial.")) return;
      try { await api("POST", `/api/missions/${mid}/cancel`, {}); toast("Detención solicitada"); } catch (e) { toast(e.message, true); }
    } }));
  } else {
    actions.append(h("button", { class: "btn small", type: "button", text: "Continuar exploración", onclick: async () => {
      if (!confirm("Se creará una nueva exploración que continúa este mapa (solo lectura). ¿Continuar?")) return;
      try { const r = await api("POST", `/api/explorer/continue/${mid}`, {}); location.hash = `#/mision/${r.id}`; } catch (e) { toast(e.message, true); }
    } }));
  }
  actions.append(h("a", { class: "btn ghost small", href: `#/explorador/mapa/${encodeURIComponent(d.spec.conn)}`, text: "Ver mapa del sistema" }));
  const ev = d.evaluation;
  const evalBox = ev ? h("div", { class: "panel" },
    h("div", { class: "row between" }, h("h2", { text: "Evaluación del resultado" }), h("span", { class: `badge ${QUALITY[ev.quality] || ""}`, text: `resultado: ${ev.quality}` })),
    h("dl", { class: "kv" },
      h("dt", { text: "Proceso" }), h("dd", { text: `finalizó por ${ev.ended_reason} · ${(ev.elapsed_s / 60).toFixed(1)} de ${(ev.budget_s / 60).toFixed(1)} min` }),
      h("dt", { text: "Objetivo" }), h("dd", { class: ev.objective_met ? "ok" : "bad", text: ev.objective_met ? "cumplido: leyó registros, hallazgos respaldados e informe completo" : "NO cumplido" }),
      h("dt", { text: "Informe" }), h("dd", { text: `${ev.report_status} · ${ev.metrics.aplicaciones_validas} aplicaciones válidas` }),
      h("dt", { text: "Lectura real" }), h("dd", { text: `${ev.metrics.tablas_leidas.length}/${ev.metrics.tablas_totales} tablas leídas (${ev.metrics.tablas_leidas.join(", ") || "ninguna"}) · ${ev.metrics.consultas_con_datos} consultas con datos, ${ev.metrics.consultas_vacias} vacías, ${ev.metrics.consultas_con_error} con error, ${ev.metrics.consultas_rechazadas} rechazadas` }),
      h("dt", { text: "Evidencia" }), h("dd", { text: `${ev.metrics.hallazgos_respaldados} hallazgos respaldados · ${ev.metrics.afirmaciones_sin_verificar} sin verificar · ${ev.metrics.hechos_comprobados_calculados} hechos calculados` }),
      h("dt", { text: "Modelo" }), h("dd", { text: `${ev.metrics.respuestas_invalidas} respuestas inválidas · ${ev.metrics.reparaciones} reparaciones de argumentos` })),
    ev.reasons.length ? h("ul", { class: "small" }, ...ev.reasons.map((r) => h("li", { text: r }))) : "") : "";
  const steps = h("div", { class: "list" });
  for (const st of d.steps.slice().reverse()) {
    if (st.status === "invalida") {
      steps.append(h("details", { class: "item" },
        h("summary", {}, `Paso ${st.n} · `, h("span", { class: "badge bad", text: "respuesta inválida" }), ` ${st.analysis || ""}`.slice(0, 160)),
        h("p", { class: "muted small", text: `Reparaciones intentadas: ${st.repairs || 0}. No se ejecutó ninguna consulta.` }),
        st.raw ? h("pre", { class: "console", text: st.raw }) : ""));
      continue;
    }
    const q = qById[st.query_id];
    const ev = st.evidence || {};
    steps.append(h("details", { class: "item", open: st === last },
      h("summary", {}, `Paso ${st.n} · `, h("span", { class: `badge ${st.status === "ok" ? "ok" : (st.status === "en_curso" ? "" : "bad")}`, text: st.status }), ` ${st.question || ""}`),
      h("p", { class: "small" }, h("strong", { text: "Por qué: " }), st.rationale || "—"),
      h("p", { class: "muted small", text: `Evidencia citada: ${(ev.citada || []).join(", ") || "ninguna"}${(ev.inexistente || []).length ? ` · sin respaldo: ${ev.inexistente.join(", ")}` : ""}` }),
      h("p", { class: "small mono", text: `Herramienta: ${st.tool} ${JSON.stringify(st.args)}${st.repairs ? ` · ${st.repairs} reparación(es) de argumentos` : ""}` }),
      (ev.correcciones || []).length ? h("p", { class: "small muted", text: `Correcciones automáticas: ${ev.correcciones.join("; ")}` }) : "",
      q ? h("pre", { class: "console", text: q.sql_text }) : "",
      q ? resultTable(q.result) : "",
      st.analysis ? h("p", { class: "small ok", text: `Comprobado por el sistema: ${st.analysis}` }) : ""));
  }
  const unverified = d.facts.filter((f) => f.certainty === "sin_verificar");
  const kinds = { hallazgo: "Descubrimientos", relacion: "Relaciones", estadistica: "Estadísticas", advertencia: "Advertencias", oportunidad: "Aplicaciones propuestas", cambio: "Cambios de esquema" };
  const facts = h("div");
  for (const [k, title] of Object.entries(kinds)) {
    const fs = d.facts.filter((f) => f.kind === k && f.certainty !== "sin_verificar");
    if (!fs.length) continue;
    facts.append(h("h3", { class: "mt", text: `${title} (${fs.length})` }), h("div", { class: "list" }, ...fs.map((f) => factItem(f, k === "oportunidad" ? buildButton(d.spec.conn, f) : ""))));
  }
  if (unverified.length) facts.append(h("h3", { class: "mt bad", text: `Afirmaciones sin verificar (${unverified.length}) — no son descubrimientos` }), h("div", { class: "list" }, ...unverified.map((f) => factItem(f))));
  box.replaceChildren(
    evalBox,
    h("div", { class: "panel" },
      h("h2", { text: "Exploración" }),
      h("div", { class: "banner", text: "La IA solo usa consultas de lectura a través del intermediario controlado: nunca ve credenciales ni modifica la base. «Comprobado» = calculado por el sistema con una consulta registrada; «inferencia» = interpretación del modelo." }),
      h("dl", { class: "kv" },
        h("dt", { text: "Progreso" }), h("dd", { text: `${p.steps}/${p.max_steps} pasos · ${p.queries}/${p.max_queries} consultas · ${elapsed.toFixed(1)}/${p.max_minutes} min` }),
        h("dt", { text: "Pregunta actual" }), h("dd", { text: last ? last.question : "observando la estructura…" }),
        h("dt", { text: "Justificación" }), h("dd", { text: last ? last.rationale : "—" }),
        h("dt", { text: "Consulta" }), h("dd", { class: "mono small", text: last && qById[last.query_id] ? qById[last.query_id].sql_text : "—" })),
      actions),
    h("div", { class: "panel" }, h("h2", { text: "Pasos (pregunta → consulta → resultado)" }), steps.children.length ? steps : h("p", { class: "muted small", text: "Sin pasos todavía." })),
    h("div", { class: "panel" }, h("h2", { text: "Hallazgos de esta exploración" }), facts.children.length ? facts : h("p", { class: "muted small", text: "Todavía no hay hallazgos." }),
      h("h3", { class: "mt", text: "Incertidumbres" }), h("div", { class: "list" }, ...d.uncertainties.map((f) => factItem(f)))));
}

function buildButton(conn, f) {
  if (!f.data || !(f.data.consultas || []).some((c) => c.valida)) return h("p", { class: "muted small", text: "Sin consultas válidas para construir." });
  return h("div", { class: "mt" },
    ...(f.data.consultas || []).map((c) => h("pre", { class: "console", text: (c.valida ? "" : `⚠ ${c.motivo}\n`) + c.sql })),
    h("button", { class: "btn small", type: "button", text: "Construir en el Taller Web", onclick: () => {
      S.draft = { title: f.data.nombre, mode: "web", description: `${f.data.descripcion}\n\nAplicación propuesta por el Explorador (F${f.id}). Usar solo los datos incluidos.`,
        cases: [{ tipo: "js_contiene", valor: "fetch" }, { tipo: "selector", valor: "h1" }], seed: { conn, fact_id: f.id } };
      location.hash = "#/nueva";
    } }));
}

async function viewExplorer(v, args) {
  if (args[0] === "mapa") return viewMap(v, decodeURIComponent(args[1] || ""));
  const [cs, ms] = await Promise.all([api("GET", "/api/explorer/connections"), api("GET", "/api/missions?limit=100")]);
  const lim = cs.limits;
  const cards = h("div", { class: "grid2" });
  for (const c of cs.items) {
    const st = c.status || {};
    cards.append(h("div", { class: "panel" },
      h("div", { class: "row between" }, h("h2", { class: "mono", text: c.name }), h("span", { class: `badge ${st.verified ? "ok" : "bad"}`, text: st.verified ? "verificada" : "bloqueada" })),
      h("dl", { class: "kv" },
        h("dt", { text: "Base" }), h("dd", { class: "mono", text: c.database }),
        h("dt", { text: "Usuario SQL" }), h("dd", { class: "mono", text: c.user }),
        h("dt", { text: "Servidor" }), h("dd", { class: "mono", text: `${c.endpoint}${st.server_version ? " · " + st.server_version : ""}` }),
        h("dt", { text: "Sesión" }), h("dd", { class: "small", text: st.session ? `solo lectura=${st.session.tx_read_only} · tiempo ${st.session.max_statement_time}s · filas ${st.session.sql_select_limit - 1} · max_join_size ${st.session.max_join_size}` : "—" }),
        h("dt", { text: "Sonda escritura" }), h("dd", { class: "small", text: st.write_probe || "—" }),
        h("dt", { text: "Mapa" }), h("dd", { text: `${c.map} hechos registrados` }),
        st.reason ? h("dt", { text: "Motivo" }) : "", st.reason ? h("dd", { class: "bad small", text: st.reason }) : ""),
      st.grants ? h("details", {}, h("summary", { class: "small", text: "Privilegios verificados" }), h("pre", { class: "console", text: st.grants.join("\n") })) : "",
      h("div", { class: "row mt" },
        h("button", { class: "btn ghost small", type: "button", text: "Verificar ahora", onclick: async () => {
          try { const r = await api("POST", `/api/explorer/connections/${c.name}/verify`, {}); toast(r.verified ? "Conexión verificada" : `Bloqueada: ${r.reason}`, !r.verified); route(); } catch (e) { toast(e.message, true); }
        } }),
        h("a", { class: "btn ghost small", href: `#/explorador/mapa/${encodeURIComponent(c.name)}`, text: "Ver mapa" }))));
  }
  const conn = h("select", {}, ...cs.items.map((c) => h("option", { value: c.name, text: c.name })));
  const title = h("input", { type: "text", maxlength: 120, value: "Exploración inicial" });
  const desc = h("textarea", { rows: 5, maxlength: 6000 });
  desc.value = DEMO_EXPLORACION;
  const num = (val, max) => h("input", { type: "number", min: 1, max, value: val });
  const steps = num(Math.min(25, lim.max_steps), lim.max_steps), queries = num(Math.min(60, lim.max_queries), lim.max_queries), minutes = num(Math.min(45, lim.max_minutes), lim.max_minutes);
  const auth = h("input", { type: "checkbox" });
  const err = h("p", { class: "bad small" });
  const go = h("button", { class: "btn", type: "button", text: "Iniciar exploración", onclick: async () => {
    err.textContent = "";
    if (!confirm("La IA ejecutará consultas de SOLO LECTURA, con límites, sobre la base elegida. ¿Iniciar?")) return;
    try {
      const r = await api("POST", "/api/missions", { mode: "exploracion", conn: conn.value, title: title.value, description: desc.value,
        max_steps: parseInt(steps.value, 10), max_queries: parseInt(queries.value, 10), max_minutes: parseInt(minutes.value, 10),
        authorize_execution: auth.checked });
      location.hash = `#/mision/${r.id}`;
    } catch (e) { err.textContent = e.message; }
  } });
  const list = h("div", { class: "list" });
  for (const m of ms.items.filter((x) => x.mode === "exploracion")) {
    list.append(h("a", { class: "item", href: `#/mision/${m.id}` }, h("div", { class: "row between" }, h("span", { class: "t", text: m.title }), badge(m.state)),
      h("div", { class: "muted small mono", text: `${m.id} · ${fmtDate(m.created_at)}` })));
  }
  v.replaceChildren(h("h1", { text: "Explorador de bases de datos" }),
    h("div", { class: "banner", text: `Linterna: consultas de solo lectura vía intermediario (máx. ${lim.max_rows} filas, ${lim.max_result_kb} KiB y ${lim.query_timeout_s} s por consulta; una a la vez). Las credenciales nunca llegan al modelo ni al código generado.` }),
    cs.items.length ? cards : h("div", { class: "panel" }, h("p", { text: "No hay conexiones configuradas. Ver EXPLORADOR.md para crear el usuario SQL de solo lectura y la conexión." })),
    cs.items.length ? h("div", { class: "panel" }, h("h2", { text: "Nueva exploración" }),
      h("label", { text: "Conexión" }), conn, h("label", { text: "Título" }), title,
      h("label", { text: "Misión para la IA" }), desc,
      h("div", { class: "grid2" }, h("div", {}, h("label", { text: "Pasos máximos" }), steps), h("div", {}, h("label", { text: "Consultas máximas" }), queries), h("div", {}, h("label", { text: "Minutos máximos" }), minutes)),
      h("label", { class: "check" }, auth, "Autorizo a la IA a ejecutar consultas de solo lectura, con límites, sobre esta conexión."),
      err, go) : "",
    h("div", { class: "panel" }, h("h2", { text: "Exploraciones" }), list.children.length ? list : h("p", { class: "muted small", text: "Todavía no hay exploraciones." })));
}

async function viewMap(v, conn) {
  const d = await api("GET", `/api/explorer/maps/${encodeURIComponent(conn)}`);
  const snap = d.snapshot;
  const byKind = (k) => d.facts.filter((f) => f.kind === k && f.certainty !== "sin_verificar");
  const tables = h("div", { class: "list" });
  if (snap) {
    for (const [name, t] of Object.entries(snap.schema.tablas)) {
      const tb = h("tbody");
      for (const c of t.columnas) tb.append(h("tr", {}, h("td", { class: "mono small", text: c.nombre }), h("td", { class: "mono small", text: c.tipo }),
        h("td", { class: "small", text: [c.clave === "PRI" ? "PK" : c.clave, c.nulo ? "nulo" : "", c.extra].filter(Boolean).join(" ") })));
      tables.append(h("details", { class: "item" },
        h("summary", {}, h("span", { class: "mono", text: name }), ` · ${t.tipo} · ~${t.filas_estimadas ?? "?"} filas · ${t.columnas.length} columnas`),
        h("div", { class: "scroll-x" }, h("table", {}, tb)),
        Object.keys(t.indices).length ? h("p", { class: "muted small", text: "Índices: " + Object.entries(t.indices).map(([k, i]) => `${k}(${i.columnas.join(", ")})${i.unico ? " único" : ""}`).join(" · ") }) : ""));
    }
  }
  const section = (title, items, extra) => h("div", { class: "panel" }, h("h2", { text: `${title} (${items.length})` }),
    items.length ? h("div", { class: "list" }, ...items.map((f) => factItem(f, extra ? extra(f) : ""))) : h("p", { class: "muted small", text: "Nada registrado." }));
  const rels = byKind("relacion").sort((a, b) => ["comprobado", "hipotesis", "descartado"].indexOf(a.certainty) - ["comprobado", "hipotesis", "descartado"].indexOf(b.certainty));
  const qtb = h("tbody");
  for (const q of d.queries) qtb.append(h("tr", {},
    h("td", { class: "small mono", text: `C${q.id}` }), h("td", { class: "small", text: fmtDate(q.created_at) }),
    h("td", { class: "small", text: `${q.origin}/${q.tool}` }), h("td", {}, h("span", { class: `badge ${q.status === "ok" ? "ok" : "bad"}`, text: q.status })),
    h("td", { class: "small mono pre-wrap", text: q.sql_text.slice(0, 300) }), h("td", { class: "small", text: q.rationale || q.question || "" })));
  v.replaceChildren(
    h("div", { class: "row between mb" }, h("h1", { text: `Mapa: ${conn}` }), h("a", { class: "btn ghost small", href: "#/explorador", text: "← Explorador" })),
    h("div", { class: "panel" }, h("dl", { class: "kv" },
      h("dt", { text: "Base" }), h("dd", { class: "mono", text: d.database }),
      h("dt", { text: "Última observación" }), h("dd", { text: snap ? fmtDate(snap.observed_at) : "nunca" }),
      h("dt", { text: "Huella del esquema" }), h("dd", { class: "mono small", text: snap ? snap.fingerprint.slice(0, 16) : "—" }),
      h("dt", { text: "Observaciones" }), h("dd", { text: `${d.snapshots.length}` })),
      h("p", { class: "muted small", text: "comprobado = calculado por el sistema con una consulta registrada · inferencia = interpretación del modelo · hipótesis = sin verificar · descartado = refutado por una consulta" })),
    h("div", { class: "panel" }, h("h2", { text: "Estructura" }), snap ? tables : h("p", { class: "muted", text: "Todavía no se exploró." })),
    section("Relaciones", rels),
    section("Estadísticas", byKind("estadistica")),
    section("Advertencias", byKind("advertencia")),
    section("Cambios de esquema", byKind("cambio")),
    section("Descubrimientos (inferencias del modelo)", byKind("hallazgo")),
    section("Aplicaciones propuestas", byKind("oportunidad"), (f) => buildButton(conn, f)),
    section("Incertidumbres", byKind("incertidumbre")),
    section("Afirmaciones sin verificar (no son descubrimientos)", d.facts.filter((f) => f.certainty === "sin_verificar")),
    d.obsolete.length ? section("Obsoleto (ya no existe en el esquema)", d.obsolete) : "",
    h("div", { class: "panel" }, h("h2", { text: "Historial de consultas" }), h("div", { class: "scroll-x" }, h("table", {}, qtb))));
}

boot();
