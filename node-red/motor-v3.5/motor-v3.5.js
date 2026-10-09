// === Motor navegación v3.5 – MENUS COMPARTIDOS (deriva de v3.4 STICKY + CONFIRM + puestos + BOTONASO) ===
const M = flow.get('wiz_map') || {};
let st = flow.get('wiz_state') || { current: '/inicio', formData: {}, history: [], _prevBlock: null };
const p = msg.payload || {};
const FINAL_BLOCK = '/seleccion';

// 🆕 v3.5 – Familias de máquinas que comparten menú.
// Se guarda igual la máquina exacta (puesto/maquina/path); solo cambia el bloque al que se navega.
// Si el menú compartido todavía no existe en el cuestionario, se mantiene el comportamiento v3.4.
const FAMILY_MENUS = [
  { re: /^\/(?:psto|puesto|maquina)_MOLINO_/i,    menu: '/MENU_MOLINOS' },
  { re: /^\/(?:psto|puesto|maquina)_COLOIDAL_/i,  menu: '/MENU_COLOIDALES' },
  { re: /^\/(?:psto|puesto|maquina)_EXTRUSORA_/i, menu: '/MENU_EXTRUSORAS' }
];
function familyMenu(cmd){
  const f = FAMILY_MENUS.find(f => f.re.test(cmd));
  return (f && M[f.menu]) ? f.menu : null;
}

if (p.type === 'render') return null;

// Helpers
function render() {
  const block = M[st.current] || M['/inicio'] || null;
  return { type: 'render', block, state: { current: st.current, formData: st.formData } };
}
function pushHistory(prev){ if(!st.history) st.history=[]; if(!st.history.length || st.history[st.history.length-1]!==prev) st.history.push(prev); }
function popHistory(){ if(!st.history || !st.history.length) return '/inicio'; return st.history.pop(); }
function parseDirective(line){
  const m = line.match(/^\$([A-Z0-9_]+)(?:\s+(.+))?$/i); if(!m) return null;
  const cmd=m[1], args={}; if(m[2]) m[2].split(/\s+/).forEach(pair=>{const i=pair.indexOf('='); if(i>0) args[pair.slice(0,i)]=pair.slice(i+1); else args[pair]=true;});
  return { cmd, args };
}
function emitDirectives(block){
  if(!block || !Array.isArray(block.directives) || !block.directives.length) return [];
  const blockName = block.name || st.current;
  return block.directives.map(parseDirective).filter(Boolean).map(d=>({
    payload:{ type:'directive', directive:d.cmd, args:d.args||{}, context:{ current:st.current, formData:st.formData, block:blockName } }
  }));
}

// === 1) Inputs "/:clave valor"
if (p.type === 'input' && p.key) {
  const origKey = String(p.key);
  let key = origKey;
  if (key === 'kilos_descuento') key = 'kilos_desc';

  const val = String(p.value || '').trim();
  st.formData[key] = val;
  if (origKey !== key) st.formData[origKey] = val;

  if (key === 'kilos_desc' || key === 'peso_bruto') {
    const bruto = Number(st.formData.peso_bruto) || 0;
    const desc  = Number(st.formData.kilos_desc) || 0;
    st.formData.peso_neto = Math.max(0, +(bruto - desc).toFixed(1));
  }
}

let sideMsgs = null;
function pushSide(m){ if(!m) return; if(!sideMsgs) sideMsgs=[]; Array.isArray(m)? sideMsgs.push(...m): sideMsgs.push(m); }

// === 2) Comandos
if (p.type === 'cmd' && p.cmd) {
  const cmd = String(p.cmd).trim();

  // sistema
  if (cmd === '/__inicio') {
    st.history = [];
    st.current = '/inicio';

  } else if (cmd === '/__volver') {
    st.current = popHistory();

  // 🆕 BOTONASO - Repetir último con peso actual
  } else if (cmd === '/cat1_botonaso') {
    const lastConfirmed = flow.get('lastConfirmed') || {};

    if (!lastConfirmed.tipo_movimiento) {
      // No hay último registro, volver a inicio
      st.history = [];
      st.current = '/inicio';
    } else {
      // Copiar todo del último registro
      const now = new Date();
      const yyyy = now.getFullYear();
      const mm = String(now.getMonth()+1).padStart(2,'0');
      const dd = String(now.getDate()).padStart(2,'0');
      const hh = String(now.getHours()).padStart(2,'0');
      const mi = String(now.getMinutes()).padStart(2,'0');
      const ss = String(now.getSeconds()).padStart(2,'0');

      st.formData = {
        ...lastConfirmed,
        // Resetear kilos (se pedirán con directiva)
        peso_bruto: '',
        peso_neto: '',
        kilos_desc: '',
        kilos_descuento: '',
        // Nueva fecha/hora
        fecha: `${yyyy}-${mm}-${dd}`,
        hora: `${hh}:${mi}:${ss}`
      };

      st.history = [];
      pushHistory('/inicio');
      st.current = '/cat1_botonaso';

      // 🔥 EMITIR directiva de peso AHORA
      pushSide({
        payload: {
          type: 'directive',
          directive: 'PEDIR_PESO_GLOBAL',
          args: {},
          context: {
            current: '/cat1_botonaso',
            formData: st.formData,
            block: '/cat1_botonaso'
          }
        }
      });
    }

  } else if (cmd === '/confirmar') {
    const F = st.formData || {};
    const numOrUndef=(v)=>{ if(v===undefined||v===null||v==='') return undefined; const n=Number(String(v).replace(',','.')); return Number.isFinite(n)? +n.toFixed(1): undefined; };
    const k_bruto = numOrUndef(F.peso_bruto);
    const k_desc  = numOrUndef(F.kilos_desc);
    let   k_neto  = numOrUndef(F.peso_neto);
    if (k_neto===undefined && k_bruto!==undefined && k_desc!==undefined) k_neto = +(k_bruto - k_desc).toFixed(1);

    pushSide({
      topic:'wizard/confirm',
      payload:{
        tipo_movimiento: F.tipo_movimiento || '',
        proveedor: F.proveedor || '',
        cliente:  F.cliente  || '',
        patente:  F.patente  || '',
        remito:   F.remito   || '',
        NroParteProd: F.NroParteProd || '',
        fecha:    F.fecha || new Date().toISOString().slice(0,10),
        hora:     F.hora  || '',
        ne:       F.ne || '',
        operador: F.operador || '',
        puesto:   F.puesto || '',
        maquina:  F.maquina || '',
        path:     F.path || [],
        id: F.id || '', lote: F.lote || '',
        k_bruto, k_neto, k_desc,
        producto_id: (F.producto_id || (F.producto ? String(F.producto).replace(/^\//,'') : '')) || '',
        estado: F.estado || '', color: F.color || '', limpieza: F.limpieza || ''
      }
    });

    // 🆕 GUARDAR copia para BOTONASO (SIN los kilos)
    const lastConfirmed = {
      tipo_movimiento: F.tipo_movimiento,
      proveedor: F.proveedor,
      cliente: F.cliente,
      patente: F.patente,
      ne: F.ne,
      operador: F.operador,
      puesto: F.puesto,
      maquina: F.maquina,
      path: F.path || [],
      NroParteProd: F.NroParteProd,
      producto: F.producto,
      producto_id: F.producto_id,
      estado: F.estado,
      color: F.color,
      limpieza: F.limpieza
    };
    flow.set('lastConfirmed', lastConfirmed);

    const stickyBy = {
      'COMPRAS':    ['proveedor','ne','operador'],
      'DESPACHOS':  ['cliente','patente','operador'],
      'PRODUCCIÓN': ['operador','NroParteProd','patente','puesto','maquina']
    };
    const scope = F.tipo_movimiento || 'COMPRAS';
    const keep  = stickyBy[scope] || [];
    const sticky = {}; keep.forEach(k => { if (F[k] != null) sticky[k] = F[k]; });
    const ST = flow.get('wiz_sticky') || {}; ST[scope] = sticky; flow.set('wiz_sticky', ST);

    st.history = [];
    st.current = '/inicio';
    st.formData = Object.assign({ tipo_movimiento: scope }, sticky);
    st.formData.kilos_desc = '';
    st.formData.kilos_descuento = '';
  }

  // ramas
  else if (['/cat1_COMPRAS','/cat1_DESPACHOS','/cat1_PRODUCCION'].includes(cmd)) {
    const tipoMov = (cmd === '/cat1_COMPRAS') ? 'COMPRAS'
                  : (cmd === '/cat1_DESPACHOS') ? 'DESPACHOS' : 'PRODUCCIÓN';

    const ST = flow.get('wiz_sticky') || {};
    const sticky = ST[tipoMov] || {};
    st.formData = Object.assign({ tipo_movimiento: tipoMov }, sticky);

    st.history = [];
    pushHistory('/inicio');
    st.current = cmd;

    const now = new Date();
    const yyyy = now.getFullYear();
    const mm = String(now.getMonth()+1).padStart(2,'0');
    const dd = String(now.getDate()).padStart(2,'0');
    const hh = String(now.getHours()).padStart(2,'0');
    const mi = String(now.getMinutes()).padStart(2,'0');
    const ss = String(now.getSeconds()).padStart(2,'0');
    st.formData.fecha = `${yyyy}-${mm}-${dd}`;
    st.formData.hora  = `${hh}:${mi}:${ss}`;
  }

  // ===== Prefijos / ruteo =====
  const isProd = /^\/prod_/i.test(cmd);
  const isDesp = /^\/(prud|prad|blo)_/i.test(cmd);
  const isPsto = /^\/(psto|puesto|maquina)_/i.test(cmd);
  const isRama = /^\/RAMA_/i.test(cmd);

  function pushPath(token) {
    st.formData.path = Array.isArray(st.formData.path) ? st.formData.path : [];
    st.formData.path.push(token);
  }

  // ----- Puestos: guardar Y navegar -----
  if (isPsto) {
    pushHistory(st.current);
    st.formData.puesto  = cmd;
    st.formData.maquina = cmd.slice(1);
    pushPath(cmd.slice(1));

    // 🆕 v3.5: familia con menú compartido → MENU_*; si no, igual que v3.4
    const famMenu = familyMenu(cmd);
    if (famMenu) {
      st.current = famMenu;
    } else if (M[cmd]) {
      st.current = cmd;
    } else {
      st.current = FINAL_BLOCK;
    }
  }

  // ----- Ramas: navegar directo -----
  else if (isRama) {
    pushHistory(st.current);
    if (M[cmd]) {
      st.current = cmd;
    } else {
      st.current = FINAL_BLOCK;
    }
  }

  // ----- Productos -----
  else if (isProd) {
    pushHistory(st.current);
    if (M[cmd]) {
      st.current = cmd;
    } else {
      st.formData.producto = cmd;
      st.formData.producto_id = cmd.slice(1);
      pushPath(cmd.slice(1));
      st.current = '/q_estado';
    }
  }

  // ----- Despachos -----
  else if (isDesp) {
    st.formData.producto = cmd;
    st.formData.producto_id = cmd.slice(1);
    pushPath(cmd.slice(1));
    pushHistory(st.current);
    st.current = FINAL_BLOCK;
  }

  // ----- Estado/Color/Limpieza -----
  else if (cmd.startsWith('/q_estado_')) {
    st.formData.estado = cmd.replace('/q_estado_', '');
    pushPath(st.formData.estado);
    pushHistory(st.current);
    st.current = '/q_color';
  }
  else if (cmd === '/q_color_natural' || cmd === '/q_color_tutti') {
    st.formData.color = cmd.replace('/q_color_', '');
    st.formData.color_tipo = 'simple';
    pushPath(st.formData.color);
    pushHistory(st.current);
    st.current = '/q_limpieza';
  }
  else if (cmd === '/q_color_definido') {
    pushHistory(st.current);
    st.current = '/q_color_especifico';
  }
  else if (cmd.startsWith('/q_color_esp_')) {
    st.formData.color = cmd.replace('/q_color_esp_', '');
    st.formData.color_tipo = 'definido';
    pushPath(st.formData.color);
    pushHistory(st.current);
    st.current = '/q_limpieza';
  }
  else if (cmd.startsWith('/q_limp_')) {
    st.formData.limpieza = cmd.replace('/q_limp_', '');
    pushPath(st.formData.limpieza);
    pushHistory(st.current);
    st.current = FINAL_BLOCK;
  }

  // salto directo
  else if (M[cmd]) {
    pushHistory(st.current);
    st.current = cmd;
  } else {
    const cur = M[st.current] || M['/inicio'];
    if (cur && cur.jumps) {
      const j = cur.jumps.find(j => j.cmd === cmd && j.to);
      if (j && j.to) { pushHistory(st.current); st.current = j.to; }
    }
  }
}

flow.set('wiz_state', st);

const outRender = { payload: render() };

const curBlock = M[st.current] || null;
if (curBlock && curBlock.name !== st._prevBlock) {
  const ds = emitDirectives(curBlock); if (ds.length) pushSide(ds);
}
st._prevBlock = curBlock ? curBlock.name : null;
flow.set('wiz_state', st);

if (p.type === 'input') { return [{ payload: render() }, null]; }

return [outRender, sideMsgs];
