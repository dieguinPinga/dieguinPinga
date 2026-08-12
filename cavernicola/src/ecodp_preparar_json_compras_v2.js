const filename = msg.filename || msg.payload || msg.file || msg.topic;
if (!filename || !String(filename).endsWith('.json')) return null;

const base = String(filename).split('/').pop();
const permitidos = [
  'planComprasV2.json',
  'comprasRealesIA.json',
  'stockIntermediosIA.json',
  'agendaComprasIA.json'
];

const esComprasV4 = base.startsWith('compras_v4_excel_') || base.startsWith('compras_v4_') || base.startsWith('Compras_Mes_');
if (!permitidos.includes(base) && !esComprasV4) return null;

msg.filename = String(filename);
msg.topic = 'archivo_modificado';
return msg;
