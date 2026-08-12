function hashStr(str) {
  let h = 2166136261;
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return (h >>> 0).toString(16);
}

const filename = msg.filename || '';
const raw = String(msg.payload || '');
const base = filename.split('/').pop();
const hash = hashStr(filename + '|' + raw);
const lastKey = 'ecodp_last_hash_' + filename;
const lastHash = flow.get(lastKey);

if (hash === lastHash) {
  node.status({fill:'blue', shape:'ring', text:'sin cambio real: ' + base});
  return null;
}
flow.set(lastKey, hash);

let data = null;
try {
  data = JSON.parse(raw);
} catch (e) {
  node.status({fill:'red', shape:'ring', text:'JSON invalido: ' + base});
  msg.payload = {
    tipo: 'json_archivo_invalido',
    origen: 'node_red_watchdog_compras',
    topic: 'ecotecnica/compras/' + base.replace('.json', ''),
    archivo: filename,
    generadoEn: new Date().toISOString(),
    error: String(e.message || e),
    texto: raw.slice(0, 4000),
    hash
  };
  return msg;
}

let clase = 'json_compras_actualizado';
if (base === 'planComprasV2.json') clase = 'plan_compras_criterio';
if (base === 'comprasRealesIA.json') clase = 'plan_compras_reales';
if (base === 'stockIntermediosIA.json') clase = 'plan_compras_stock';
if (base === 'agendaComprasIA.json') clase = 'plan_compras_agenda';

// Este es el importante para ResumenDetalle.
if (data && data.realesAuditoria && data.mixMensual) {
  clase = 'compras_mes_v4';
}

msg.method = 'POST';
msg.url = 'http://100.73.116.111:8080/api/ia/ingest/plan_compras';
msg.headers = {'Content-Type': 'application/json'};
msg.payload = {
  tipo: clase,
  origen: 'node_red_watchdog_compras',
  topic: 'ecotecnica/compras/' + base.replace('.json', ''),
  archivo: filename,
  generadoEn: new Date().toISOString(),
  hash,
  data
};

node.status({fill:'green', shape:'dot', text:'POST ' + clase + ' · ' + base});
return msg;
