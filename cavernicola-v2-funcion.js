const PLAN_FILE = "/home/gangleo81/planComprasV2.json";
const REALES_FILE = "/home/gangleo81/comprasRealesIA.json";
const STOCK_FILE = "/home/gangleo81/stockIntermediosIA.json";
const AGENDA_FILE = "/home/gangleo81/agendaComprasIA.json";

const BANDAS_MATERIAL_BASE = [
  { material: "BATERIA", productoCompra: "PP_COPO", familiaOperativa: "BATERIA", minimoTn: 20, maximoTn: 40 },
  { material: "BAZAR TUTY", productoCompra: "PP_COPO", familiaOperativa: "BAZAR", minimoTn: 20, maximoTn: 40 },
  { material: "BAZAR ROJO", productoCompra: "PP_COPO", familiaOperativa: "BAZAR", minimoTn: 10, maximoTn: 30 },
  { material: "BAZAR VERDE", productoCompra: "PP_COPO", familiaOperativa: "BAZAR", minimoTn: 10, maximoTn: 30 },
  { material: "BALDE BLANCO", productoCompra: "PP_COPO", familiaOperativa: "BALDE", minimoTn: 20, maximoTn: 50 },
  { material: "SILLA BLANCA", productoCompra: "PP_COPO", familiaOperativa: "SILLA", minimoTn: 20, maximoTn: 50 },
  { material: "SILLA TUTY", productoCompra: "PP_COPO", familiaOperativa: "SILLA", minimoTn: 0, maximoTn: 10 },
  { material: "SOPLADO TUTTY S/N", productoCompra: "SOPLADO_TUTTY", familiaOperativa: "SOPLADO", minimoTn: 10, maximoTn: 20 },
  { material: "SOPLADO TUTTY SIN NEGRO", productoCompra: "SOPLADO_TUTTY", familiaOperativa: "SOPLADO", minimoTn: 10, maximoTn: 20 },
  { material: "SOPLADO TUTTY C/NEGRO", productoCompra: "SOPLADO_TUTTY", familiaOperativa: "SOPLADO", minimoTn: 20, maximoTn: 30 },
  { material: "SOPLADO TUTTY CON NEGRO", productoCompra: "SOPLADO_TUTTY", familiaOperativa: "SOPLADO", minimoTn: 20, maximoTn: 30 },
  { material: "SOPLADO BLANCO", productoCompra: "SOPLADO_OTROS", familiaOperativa: "SOPLADO", minimoTn: 10, maximoTn: 20 },
  { material: "SOPLADO CARAMELO", productoCompra: "SOPLADO_OTROS", familiaOperativa: "SOPLADO", minimoTn: 10, maximoTn: 20 },
  { material: "SOPLADO AZUL", productoCompra: "SOPLADO_OTROS", familiaOperativa: "SOPLADO", minimoTn: 10, maximoTn: 20 },
  { material: "SOPLADO AMARILLO", productoCompra: "SOPLADO_OTROS", familiaOperativa: "SOPLADO", minimoTn: 10, maximoTn: 20 },
  { material: "STRECH CARAMELO", productoCompra: "PEMD", familiaOperativa: "STRETCH", minimoTn: 40, maximoTn: 60 },
  { material: "STRECH TUTTY", productoCompra: "PEMD", familiaOperativa: "STRETCH", minimoTn: 40, maximoTn: 60 },
  { material: "STRECH NATURAL", productoCompra: "PEMD", familiaOperativa: "STRETCH", minimoTn: 15, maximoTn: 30 },
  { material: "ADI TUTY", productoCompra: "PEMD", familiaOperativa: "ADI", minimoTn: 40, maximoTn: 60 },
  { material: "ADI CELESTE", productoCompra: "PEMD", familiaOperativa: "ADI", minimoTn: 40, maximoTn: 60 },
  { material: "ADI PARA NEGRO", productoCompra: "PEMD", familiaOperativa: "ADI", minimoTn: 30, maximoTn: 50 },
  { material: "ADI NATURAL", productoCompra: "PEMD", familiaOperativa: "ADI", minimoTn: 30, maximoTn: 40 },
  { material: "ADI ROJO", productoCompra: "PEMD", familiaOperativa: "ADI", minimoTn: 20, maximoTn: 40 },
  { material: "ROTOMOLDEO", productoCompra: "ROTOMOLDEO", familiaOperativa: "ROTOMOLDEO", minimoTn: 20, maximoTn: 60 },
  { material: "ROTOMOLDEO TUTY", productoCompra: "ROTOMOLDEO", familiaOperativa: "ROTOMOLDEO", minimoTn: 20, maximoTn: 60 },
  { material: "ROTOMOLDEO NATURAL", productoCompra: "ROTOMOLDEO", familiaOperativa: "ROTOMOLDEO", minimoTn: 20, maximoTn: 60 }
];

const PRODUCTOS_TERMINADOS = [
  { id: 0, key: "LIMBO", nombre: "Limbo / sin asignar", tipo: "limbo" },
  { id: 1, key: "PEMD", nombre: "PEMD / Cajon", tipo: "plan" },
  { id: 2, key: "PP_COPO", nombre: "PP Copo / Baldes-Bazar-Sillas", tipo: "plan" },
  { id: 3, key: "PP_HOMO", nombre: "PP Homo", tipo: "plan" },
  { id: 4, key: "SOPLADO_TUTTY", nombre: "Soplado Tutti", tipo: "control" },
  { id: 5, key: "SOPLADO_OTROS", nombre: "Soplado otros", tipo: "control" },
  { id: 6, key: "ROTOMOLDEO", nombre: "Rotomoldeo", tipo: "control" },
  { id: 7, key: "FUERA_PLAN", nombre: "Fuera del plan / control", tipo: "control" }
];

const PIE_COLORS = ["#38bdf8", "#a78bfa", "#34d399", "#f59e0b", "#f87171", "#22d3ee", "#c084fc", "#fb7185"];

function defaultCriteria() {
  return {
    version: 2,
    tipo: "cavernicola_plan_compras_v2",
    actualizado: new Date().toISOString(),
    mesKey: mesActual(),
    objetivoTotalTn: 200,
    planFamilias: [
      {
        key: "PEMD",
        nombre: "PEMD / Cajon",
        objetivoTn: 100,
        familias: ["ADI", "STRETCH"],
        criterio: "Comprar por plan mensual. El stock se interpreta desde los materiales reales del archivo."
      },
      {
        key: "PP_COPO",
        nombre: "PP Copo / Baldes-Bazar-Sillas",
        objetivoTn: 50,
        familias: ["PP Copo", "BALDE", "TAPITA", "TAPON", "NEGRO", "PP"],
        foco: "Baldes / PP Copo negro",
        criterio: "Comprar por plan mensual. El stock puede venir como BALDE, TAPITA, TAPON, NEGRO o PP."
      },
      {
        key: "PP_HOMO",
        nombre: "PP Homo",
        objetivoTn: 50,
        familias: ["PP Homo"],
        criterio: "Comprar por plan mensual."
      }
    ],
    materialMap: {},
    productosTerminados: PRODUCTOS_TERMINADOS,
    asignacionesMaterial: {},
    subramasProducto: {},
    asignacionesSubramaMaterial: {},
    bandasMaterial: BANDAS_MATERIAL_BASE,
    controlStock: [
      {
        key: "SOPLADO_OTROS",
        nombre: "Soplado otros",
        criterio: "Control: no entra en el plan 200 ni en la banda Soplado Tutti. Revisar antes de comprar."
      },
      {
        key: "ROTOMOLDEO",
        nombre: "Rotomoldeo",
        criterio: "Control: queda fuera del plan 200 salvo decision puntual."
      }
    ],
    bandasStock: [
      {
        key: "SOPLADO_TUTTY",
        nombre: "Soplado Tutti",
        familia: "SOPLADO",
        colores: ["TUTTY", "TUTTY CON NEGRO", "TUTTY SIN NEGRO"],
        minimoTn: 6,
        idealTn: 9,
        maximoTn: 12,
        criterio: "Banda permanente: si baja de 6 tn comprar hasta 9/12; si supera 12 tn frenar."
      }
    ]
  };
}

function n(x, fallback = 0) {
  const v = Number(String(x ?? "").replace(",", ".").trim());
  return Number.isFinite(v) ? v : fallback;
}
function r0(x) { return Math.round(n(x)); }
function r1(x) { return Math.round(n(x) * 10) / 10; }
function kgToTn(x) { return r1(n(x) / 1000); }
function tnToKg(x) { return r0(n(x) * 1000); }
function mesActual() {
  const d = new Date();
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0");
}
function addMes(mesKey, delta) {
  const m = /^(\d{4})-(\d{2})$/.exec(String(mesKey || ""));
  const d = m ? new Date(Number(m[1]), Number(m[2]) - 1, 1) : new Date();
  d.setMonth(d.getMonth() + delta);
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0");
}
function parseDateKey(v) {
  if (!v) return "";
  if (v instanceof Date) return v.toISOString().slice(0, 10);
  const s = String(v).trim();
  let m = /^(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{2,4})/.exec(s);
  if (m) {
    const yy = Number(m[3]);
    const y = yy < 100 ? 2000 + yy : yy;
    return String(y).padStart(4, "0") + "-" + String(Number(m[2])).padStart(2, "0") + "-" + String(Number(m[1])).padStart(2, "0");
  }
  m = /^(\d{4})-(\d{1,2})-(\d{1,2})/.exec(s);
  if (m) return m[1] + "-" + String(Number(m[2])).padStart(2, "0") + "-" + String(Number(m[3])).padStart(2, "0");
  return "";
}
function normText(v) {
  return String(v || "")
    .normalize("NFD").replace(/[̀-ͯ]/g, "")
    .trim().toUpperCase();
}
function normFamilia(v) {
  const k = normText(v);
  if (k === "STRECH") return "STRETCH";
  if (k === "PP COPO") return "PP Copo";
  if (k === "PP HOMO") return "PP Homo";
  if (k === "SOPLADO") return "SOPLADO";
  if (k === "ADI") return "ADI";
  if (k === "STRETCH") return "STRETCH";
  if (k === "BALDE" || k === "BALDES") return "BALDE";
  if (k === "TAPITA" || k === "TAPITAS") return "TAPITA";
  if (k === "TAPON" || k === "TAPONES") return "TAPON";
  if (k === "NEGRO") return "NEGRO";
  if (k === "PP") return "PP";
  return String(v || "").trim();
}
function normColor(v) {
  let k = normText(v || "SIN COLOR");
  if (k === "TUTTY S/N" || k === "TUTTY SIN NEG" || k === "TUTTY SIN NEG.") k = "TUTTY SIN NEGRO";
  if (k === "TUTTY C/N" || k === "TUTTY CON NEG") k = "TUTTY CON NEGRO";
  return k || "SIN COLOR";
}
function parseJsonPayload(payload, fallback) {
  if (payload && typeof payload === "object") return payload;
  try { return JSON.parse(String(payload || "")); } catch (e) { return fallback; }
}
function validMes(v) { return /^\d{4}-\d{2}$/.test(String(v || "")); }
function pieGradient(parts) {
  const total = (parts || []).reduce((s, x) => s + Math.max(0, n(x.value)), 0);
  if (total <= 0) return "conic-gradient(#1f2937 0 100%)";
  let acc = 0;
  const stops = [];
  (parts || []).filter(x => n(x.value) > 0).forEach((x, idx) => {
    const start = acc;
    acc += (n(x.value) / total) * 100;
    const color = x.color || PIE_COLORS[idx % PIE_COLORS.length];
    stops.push(color + " " + start.toFixed(2) + "% " + acc.toFixed(2) + "%");
  });
  return "conic-gradient(" + stops.join(", ") + ")";
}
function ensureCriteria(db) {
  const base = defaultCriteria();
  const out = (db && typeof db === "object") ? db : {};
  out.version = 2;
  out.tipo = out.tipo || base.tipo;
  out.actualizado = out.actualizado || new Date().toISOString();
  out.mesKey = validMes(out.mesKey) ? out.mesKey : base.mesKey;
  out.objetivoTotalTn = n(out.objetivoTotalTn, base.objetivoTotalTn);
  out.planFamilias = Array.isArray(out.planFamilias) && out.planFamilias.length ? out.planFamilias : base.planFamilias;
  const baseByKey = {};
  base.planFamilias.forEach(x => { baseByKey[x.key] = x; });
  out.planFamilias = out.planFamilias.map(x => {
    const b = baseByKey[x.key] || {};
    return Object.assign({}, b, x, {
      familias: Array.isArray(x.familias) && x.familias.length ? x.familias : (b.familias || []),
    });
  });
  out.metasPorMes = (out.metasPorMes && typeof out.metasPorMes === "object" && !Array.isArray(out.metasPorMes)) ? out.metasPorMes : {};
  if (!out.metasPorMes[out.mesKey]) saveMesMeta(out, out.mesKey);
  applyMesMeta(out, out.mesKey);
  out.materialMap = (out.materialMap && typeof out.materialMap === "object") ? out.materialMap : {};
  out.productosTerminados = mergeProductosTerminados(out.productosTerminados);
  out.asignacionesMaterial = (out.asignacionesMaterial && typeof out.asignacionesMaterial === "object") ? out.asignacionesMaterial : {};
  out.subramasProducto = mergeSubramasProducto(out.subramasProducto, out.productosTerminados);
  out.asignacionesSubramaMaterial = (out.asignacionesSubramaMaterial && typeof out.asignacionesSubramaMaterial === "object") ? out.asignacionesSubramaMaterial : {};
  out.bandasMaterial = Array.isArray(out.bandasMaterial) && out.bandasMaterial.length ? mergeBandasMaterial(out.bandasMaterial) : BANDAS_MATERIAL_BASE.map(x => Object.assign({}, x));
  out.controlStock = Array.isArray(out.controlStock) && out.controlStock.length ? out.controlStock : base.controlStock;
  out.bandasStock = Array.isArray(out.bandasStock) && out.bandasStock.length ? out.bandasStock : base.bandasStock;
  return out;
}
function metaPlanSnapshot(criteria) {
  const plan = (criteria.planFamilias || []).map(x => ({
    key: x.key,
    objetivoTn: r1(n(x.objetivoTn))
  }));
  return {
    objetivoTotalTn: r1(plan.reduce((s, x) => s + n(x.objetivoTn), 0)),
    planFamilias: plan
  };
}

function saveMesMeta(criteria, mesKey) {
  const key = validMes(mesKey) ? mesKey : (criteria.mesKey || mesActual());
  criteria.metasPorMes = (criteria.metasPorMes && typeof criteria.metasPorMes === "object" && !Array.isArray(criteria.metasPorMes)) ? criteria.metasPorMes : {};
  criteria.metasPorMes[key] = metaPlanSnapshot(criteria);
  return criteria;
}

function applyMesMeta(criteria, mesKey) {
  const key = validMes(mesKey) ? mesKey : mesActual();
  criteria.metasPorMes = (criteria.metasPorMes && typeof criteria.metasPorMes === "object" && !Array.isArray(criteria.metasPorMes)) ? criteria.metasPorMes : {};
  criteria.mesKey = key;

  const meta = criteria.metasPorMes[key];
  if (!meta) {
    saveMesMeta(criteria, key);
    return criteria;
  }

  const byKey = {};
  if (Array.isArray(meta.planFamilias)) {
    meta.planFamilias.forEach(x => { if (x && x.key) byKey[x.key] = n(x.objetivoTn); });
  } else if (meta.planFamilias && typeof meta.planFamilias === "object") {
    Object.keys(meta.planFamilias).forEach(k => { byKey[k] = n(meta.planFamilias[k]); });
  }

  let touched = false;
  criteria.planFamilias = (criteria.planFamilias || []).map(x => {
    if (!Object.prototype.hasOwnProperty.call(byKey, x.key)) return x;
    touched = true;
    return Object.assign({}, x, { objetivoTn: Math.max(0, r1(byKey[x.key])) });
  });

  if (!touched && meta.objetivoTotalTn !== undefined) {
    const actual = r1((criteria.planFamilias || []).reduce((s, x) => s + n(x.objetivoTn), 0));
    const nuevo = Math.max(0, r1(n(meta.objetivoTotalTn, actual)));
    const factor = actual > 0 ? nuevo / actual : 1;
    criteria.planFamilias = (criteria.planFamilias || []).map(x => Object.assign({}, x, { objetivoTn: r1(n(x.objetivoTn) * factor) }));
  }

  criteria.objetivoTotalTn = r1((criteria.planFamilias || []).reduce((s, x) => s + n(x.objetivoTn), 0));
  saveMesMeta(criteria, key);
  return criteria;
}

function mergeProductosTerminados(current) {
  const byId = {};
  PRODUCTOS_TERMINADOS.forEach(x => { byId[Number(x.id)] = Object.assign({}, x); });
  (Array.isArray(current) ? current : []).forEach(x => {
    const id = r0(x.id);
    byId[id] = Object.assign({}, byId[id] || { id, key: "RAMA_" + id, tipo: "control" }, x, {
      id,
      key: x.key || (byId[id] && byId[id].key) || ("RAMA_" + id),
      nombre: String(x.nombre || (byId[id] && byId[id].nombre) || ("Rama " + id)).trim(),
      tipo: x.tipo || (id === 0 ? "limbo" : "control")
    });
  });
  return Object.values(byId).sort((a, b) => Number(a.id) - Number(b.id));
}

function productoById(criteria, id) {
  const productos = criteria.productosTerminados || PRODUCTOS_TERMINADOS;
  const pid = Math.max(0, r0(id));
  return productos.find(x => Number(x.id) === pid) || productos[0] || PRODUCTOS_TERMINADOS[0];
}

function productoByKey(criteria, key) {
  return (criteria.productosTerminados || PRODUCTOS_TERMINADOS).find(x => x.key === key) || PRODUCTOS_TERMINADOS[0];
}

function mergeSubramasProducto(current, productos) {
  const out = {};
  (productos || PRODUCTOS_TERMINADOS).forEach(p => {
    const pid = String(r0(p.id));
    const existing = current && Array.isArray(current[pid]) ? current[pid] : [];
    const byId = { 0: { id: 0, nombre: "Sin subrama" } };
    existing.forEach(x => {
      const id = Math.max(0, r0(x.id));
      byId[id] = {
        id,
        nombre: String(x.nombre || (id === 0 ? "Sin subrama" : "Subrama " + id)).trim()
      };
    });
    out[pid] = Object.values(byId).sort((a, b) => Number(a.id) - Number(b.id));
  });
  return out;
}

function subramasForProduct(criteria, productoId) {
  criteria.subramasProducto = mergeSubramasProducto(criteria.subramasProducto, criteria.productosTerminados || PRODUCTOS_TERMINADOS);
  const pid = String(r0(productoId));
  return criteria.subramasProducto[pid] || [{ id: 0, nombre: "Sin subrama" }];
}

function subramaById(criteria, productoId, subId) {
  const id = Math.max(0, r0(subId));
  return subramasForProduct(criteria, productoId).find(x => Number(x.id) === id) || { id: 0, nombre: "Sin subrama" };
}

function setSubramaNombre(criteria, productoId, subId, nombre) {
  const pid = String(r0(productoId));
  const sid = Math.max(0, r0(subId));
  const clean = String(nombre || "").trim();
  criteria.subramasProducto = mergeSubramasProducto(criteria.subramasProducto, criteria.productosTerminados || PRODUCTOS_TERMINADOS);
  const list = subramasForProduct(criteria, pid);
  const exists = list.some(x => Number(x.id) === sid);
  const next = exists ? list.map(x =>
    Number(x.id) === sid ? Object.assign({}, x, { nombre: clean || x.nombre }) : x
  ) : list.concat([{ id: sid, nombre: clean || ("Subrama " + sid) }]);
  criteria.subramasProducto[pid] = next.sort((a, b) => Number(a.id) - Number(b.id));
  return criteria;
}

function subramaAssignKey(material, productoId) {
  return String(r0(productoId)) + "||" + normText(material);
}

function subramaIdForMaterial(criteria, material, productoId) {
  const key = normText(material);
  const scopedKey = subramaAssignKey(material, productoId);
  let raw = 0;
  if (criteria.asignacionesSubramaMaterial && criteria.asignacionesSubramaMaterial[scopedKey] !== undefined) {
    raw = criteria.asignacionesSubramaMaterial[scopedKey];
  } else if (criteria.asignacionesSubramaMaterial && criteria.asignacionesSubramaMaterial[key] !== undefined) {
    raw = criteria.asignacionesSubramaMaterial[key];
  }
  const maxId = subramasForProduct(criteria, productoId).reduce((m, x) => Math.max(m, r0(x.id)), 0);
  return Math.max(0, Math.min(maxId, r0(raw)));
}

function inferProductKeyForStockMaterial(row, criteria) {
  const material = materialName(row);
  const key = normText(material);
  const mapped = criteria && criteria.materialMap ? (criteria.materialMap[key] || criteria.materialMap[material]) : null;
  if (mapped) return mapped.productoCompra || mapped.producto || mapped.key || mapped;
  const banda = findBandaMaterial(material, criteria || {});
  if (banda && banda.productoCompra) return banda.productoCompra;
  const fam = familyFromMaterial(row);
  const mat = normText(material);
  if (fam === "ADI" || fam === "STRETCH") return "PEMD";
  if (fam === "PP Homo") return "PP_HOMO";
  if (fam === "PP Copo" || fam === "BALDE" || fam === "TAPITA" || fam === "TAPON" || fam === "NEGRO" || fam === "PP") return "PP_COPO";
  if (fam === "SOPLADO" && mat.includes("TUT")) return "SOPLADO_TUTTY";
  if (fam === "SOPLADO") return "SOPLADO_OTROS";
  if (fam === "ROTOMOLDEO") return "ROTOMOLDEO";
  return "LIMBO";
}

function mergeBandasMaterial(current) {
  const byKey = {};
  BANDAS_MATERIAL_BASE.forEach(x => { byKey[normText(x.material)] = Object.assign({}, x); });
  (current || []).forEach(x => {
    const key = normText(x.material);
    if (!key) return;
    byKey[key] = Object.assign({}, byKey[key] || {}, x, {
      material: x.material || (byKey[key] && byKey[key].material) || key,
      minimoTn: r1(x.minimoTn),
      maximoTn: r1(x.maximoTn)
    });
  });
  return Object.values(byKey);
}

function findBandaMaterial(material, criteria) {
  const key = normText(material);
  let exact = null;
  (criteria.bandasMaterial || []).forEach(b => {
    const bk = normText(b.material);
    if (bk && key === bk) exact = b;
  });
  if (exact) return exact;
  let best = null;
  (criteria.bandasMaterial || []).forEach(b => {
    const bk = normText(b.material);
    if (!bk) return;
    if (key.includes(bk) || bk.includes(key)) {
      if (!best || bk.length > normText(best.material).length) best = b;
    }
  });
  return best;
}

function getMesBlock(db, mesKey) {
  if (!db || typeof db !== "object") return null;
  if (db.meses && db.meses[mesKey]) return db.meses[mesKey];
  if (db.mesKey === mesKey) return db;
  return db;
}

function collectRows(source, mesKey) {
  const rows = [];
  const block = getMesBlock(source, mesKey);
  const candidates = [
    block && block.movimientos,
    block && block.items,
    block && block.detalle,
    block && block.registros,
    block && block.compras,
    source && source.movimientos,
    source && source.items,
    source && source.detalle,
    source && source.registros,
    source && source.compras,
    source && source.filas
  ];
  candidates.forEach(arr => { if (Array.isArray(arr)) rows.push(...arr); });
  return rows;
}

// === PARCHE reales v6n87n =========================================
// Localiza el bloque del mes exacto SIN caer al db entero.
function realMesBlock(realDb, mesKey) {
  if (!realDb || typeof realDb !== "object") return null;
  if (realDb.meses && realDb.meses[mesKey]) return realDb.meses[mesKey];
  if (realDb.mesKey === mesKey) return realDb;
  return null;
}

// Total real del mes: fuente de verdad = familias[].totalKilos / colores[].kilos.
// (Los movimientos del archivo son muestras/ejemplos y quedan cortos.)
function realIndexMes(realDb, mesKey) {
  const block = realMesBlock(realDb, mesKey);
  const byFamily = {}, byFamilyColor = {};
  let totalKg = 0;
  if (!block) return { byFamily, byFamilyColor, minByFamily: {}, deficitByFamily: {}, totalKg: 0 };
  const familias = Array.isArray(block.familias) ? block.familias : [];
  if (familias.length) {
    familias.forEach(f => {
      const fam = normFamilia(f.familia);
      const kg = r0(f.totalKilos || 0);
      byFamily[fam] = (byFamily[fam] || 0) + kg;
      totalKg += kg;
      (f.colores || []).forEach(c => {
        const key = fam + "||" + normColor(c.color);
        byFamilyColor[key] = (byFamilyColor[key] || 0) + r0(c.kilos || 0);
      });
    });
    return { byFamily, byFamilyColor, minByFamily: {}, deficitByFamily: {}, totalKg };
  }
  // Si no hay familias agregadas, caer al detalle de movimientos del mes.
  return indexKg(collectRows(realDb, mesKey));
}

// Ingresado DESPUES de la foto de stock: usa movimientos fechados (si hay detalle).
function realIndexPostStock(realDb, mesKey, stockCutDate) {
  const block = realMesBlock(realDb, mesKey);
  const byFamily = {}, byFamilyColor = {};
  const cut = parseDateKey(stockCutDate);
  if (!block || !cut || !Array.isArray(block.movimientos) || !block.movimientos.length) {
    return { byFamily, byFamilyColor, minByFamily: {}, deficitByFamily: {}, totalKg: 0 };
  }
  let totalKg = 0;
  block.movimientos.forEach(m => {
    const f = parseDateKey(m.fecha || m.fechaOriginal);
    if (!f || f < cut) return;
    const fam = normFamilia(m.familia);
    const kg = r0(m.kilos || 0);
    byFamily[fam] = (byFamily[fam] || 0) + kg;
    totalKg += kg;
    const key = fam + "||" + normColor(m.color);
    byFamilyColor[key] = (byFamilyColor[key] || 0) + kg;
  });
  return { byFamily, byFamilyColor, minByFamily: {}, deficitByFamily: {}, totalKg };
}
// === fin PARCHE reales ============================================

function dateFromRow(row) {
  if (!row || typeof row !== "object") return "";
  const direct = parseDateKey(
    row.fecha ??
    row.Fecha ??
    row.fechaOriginal ??
    row.fechaIngreso ??
    row.fechaRecepcion ??
    row.fechaCompra ??
    row.fechaComprobante ??
    row.fechaRemito ??
    row.fechaMovimiento ??
    row.fechaPlan ??
    row.fechaPlanSql ??
    row.date ??
    row.Date ??
    ""
  );
  if (direct) return direct;
  const keys = Object.keys(row).filter(k => /fecha|date|fec/i.test(k));
  for (const k of keys) {
    const parsed = parseDateKey(row[k]);
    if (parsed) return parsed;
  }
  return "";
}

function rowsFromDateInclusive(rows, cutDate) {
  const cut = parseDateKey(cutDate);
  if (!cut) return [];
  return (rows || []).filter(row => {
    const f = dateFromRow(row);
    return f && f >= cut;
  });
}

function familyFromMaterial(row) {
  const direct = normFamilia(row.rama || row.Rama || row.familia || row.Familia || row.tipoMaterial || row.grupo || "");
  if (direct) return direct;
  const mat = normText(row.material || row.Material || row.nombre || row.Nombre || row.descripcion || row.Descripcion || "");
  if (mat.includes("PP") && mat.includes("COPO")) return "PP Copo";
  if (mat.includes("PP") && mat.includes("HOMO")) return "PP Homo";
  if (mat.includes("BALDE")) return "BALDE";
  if (mat.includes("TAPITA")) return "TAPITA";
  if (mat.includes("TAPON")) return "TAPON";
  if (mat.includes("NEGRO MEZCLA")) return "NEGRO";
  if (mat.includes("PP PALLET") || mat.includes("PP PALLET")) return "PP";
  if (mat.includes("SOPLADO")) return "SOPLADO";
  if (mat.includes("STRETCH") || mat.includes("STRECH")) return "STRETCH";
  if (mat.includes("ADI") || mat.includes("PEAD") || mat.includes("PEMD")) return "ADI";
  return direct || "SIN RAMA";
}
function colorFromMaterial(row) {
  const direct = normColor(row.color || row.Color || row.colour || "");
  if (direct !== "SIN COLOR") return direct;
  const mat = normText(row.material || row.Material || row.nombre || row.Nombre || row.descripcion || row.Descripcion || "");
  if (mat.includes("TUTTY SIN NEGRO")) return "TUTTY SIN NEGRO";
  if (mat.includes("TUTTY CON NEGRO")) return "TUTTY CON NEGRO";
  if (mat.includes("TUTTY")) return "TUTTY";
  if (mat.includes("NEGRO")) return "NEGRO";
  if (mat.includes("BLANCO")) return "BLANCO";
  if (mat.includes("AZUL")) return "AZUL";
  if (mat.includes("ROJO")) return "ROJO";
  if (mat.includes("VERDE")) return "VERDE";
  if (mat.includes("NATURAL")) return "NATURAL";
  return direct;
}
function materialName(row) {
  return String(row.material || row.Material || row.nombre || row.Nombre || row.descripcion || row.Descripcion || "").trim();
}
function obsFromRow(row) {
  return String(row.obs || row.Obs || row.observacion || row.observaciones || row.Observacion || row.Observaciones || "").trim();
}
function productForStockMaterial(row, criteria) {
  const material = materialName(row);
  const assigned = criteria && criteria.asignacionesMaterial ? criteria.asignacionesMaterial[normText(material)] : null;
  if (assigned !== undefined && assigned !== null) return productoById(criteria, assigned).key;
  return "LIMBO";
}
function kgFromRow(row) {
  return r0(
    row.kilos ??
    row.Kilos ??
    row.kg ??
    row.Kg ??
    row.totalKilos ??
    row["Total (kg)"] ??
    row.kgAgendado ??
    row.kgPlan ??
    row.total ??
    row.Total ??
    row.objetivoKg ??
    0
  );
}
function minKgFromRow(row) {
  return r0(
    row.stockMinimoKg ??
    row.minimoKg ??
    row.minimo ??
    row["Stock minimo (kg)"] ??
    row["Minimo (kg)"] ??
    0
  );
}
function indexKg(rows) {
  const byFamily = {};
  const byFamilyColor = {};
  const minByFamily = {};
  const deficitByFamily = {};
  rows.forEach(row => {
    const kg = kgFromRow(row);
    const minKg = minKgFromRow(row);
    if (!kg && !minKg) return;
    const fam = familyFromMaterial(row);
    const color = colorFromMaterial(row);
    byFamily[fam] = (byFamily[fam] || 0) + kg;
    minByFamily[fam] = (minByFamily[fam] || 0) + minKg;
    deficitByFamily[fam] = (deficitByFamily[fam] || 0) + Math.max(0, minKg - kg);
    const key = fam + "||" + color;
    byFamilyColor[key] = (byFamilyColor[key] || 0) + kg;
  });
  return { byFamily, byFamilyColor, minByFamily, deficitByFamily, totalKg: Object.values(byFamily).reduce((s, v) => s + v, 0) };
}
function stockRows(stockDb) {
  const snap = stockDb && stockDb.snapshotActual ? stockDb.snapshotActual : stockDb;
  if (!snap || typeof snap !== "object") return [];
  return []
    .concat(Array.isArray(snap.materiales) ? snap.materiales : [])
    .concat(Array.isArray(snap.items) ? snap.items : [])
    .concat(Array.isArray(snap.filas) ? snap.filas : []);
}

function firstValueDeep(obj, keys, depth = 0) {
  if (!obj || typeof obj !== "object" || depth > 3) return "";
  for (const k of keys) {
    if (obj[k] !== undefined && obj[k] !== null && obj[k] !== "") return obj[k];
  }
  for (const k of Object.keys(obj)) {
    const v = obj[k];
    if (v && typeof v === "object" && !Array.isArray(v)) {
      const found = firstValueDeep(v, keys, depth + 1);
      if (found !== "") return found;
    }
  }
  return "";
}

function firstDateLikeFromRows(rows) {
  for (const row of rows || []) {
    if (!row || typeof row !== "object") continue;
    const keys = Object.keys(row).filter(k => /fecha|date|fec/i.test(k));
    for (const k of keys) {
      if (row[k] !== undefined && row[k] !== null && row[k] !== "") return row[k];
    }
  }
  return "";
}

function stockSourceInfo(stockDb) {
  const snap = stockDb && stockDb.snapshotActual ? stockDb.snapshotActual : stockDb;
  const rows = stockRows(stockDb);
  const fechaStockKeys = [
    "fechaStock", "fecha_stock", "fechaCorte", "fecha_corte", "fechaFoto",
    "fecha_foto", "fechaInventario", "fecha_inventario", "fechaStockOriginal",
    "fecha_snapshot", "snapshotFecha", "fecha"
  ];
  const fechaFallbackKeys = [
    "Fecha", "fechaGeneracion", "generadoEn", "actualizado", "timestamp",
    "createdAt", "updatedAt", "date"
  ];
  const historico = Array.isArray(stockDb && stockDb.historico) ? stockDb.historico : [];
  const fechaHistorico = historico.map(x => parseDateKey(x && x.fecha)).filter(Boolean).sort().pop() || "";
  const fecha =
    (snap && snap.fecha) ||
    firstValueDeep(snap, fechaStockKeys) ||
    firstValueDeep(stockDb, fechaStockKeys) ||
    fechaHistorico ||
    firstDateLikeFromRows(rows) ||
    firstValueDeep(snap, fechaFallbackKeys) ||
    firstValueDeep(stockDb, fechaFallbackKeys);
  const campos = {};
  rows.forEach(row => {
    if (!row || typeof row !== "object") return;
    Object.keys(row).forEach(k => { campos[k] = true; });
  });
  return {
    fechaStock: fecha || "",
    fechaStockNormalizada: parseDateKey(fecha),
    fechaStockOrigen: (snap && snap.fecha) ? "snapshotActual.fecha" : (fechaHistorico ? "historico" : "detectada"),
    registros: rows.length,
    campos: Object.keys(campos).sort()
  };
}

function stockAuditRows(stockDb, criteria) {
  return stockRows(stockDb).map((row, idx) => {
    const out = {
      nro: idx + 1,
      materialNormalizado: materialName(row),
      familiaNormalizada: familyFromMaterial(row),
      productoCalculado: productForStockMaterial(row, criteria),
      stockTnCalculado: kgToTn(kgFromRow(row)),
      minimoTnOrigen: kgToTn(minKgFromRow(row)),
      obsNormalizada: obsFromRow(row)
    };
    Object.keys(row || {}).sort().forEach(k => {
      out[k] = row[k];
    });
    return out;
  });
}
function realAuditRows(rows) {
  return (rows || []).map((row, idx) => {
    const out = {
      nro: idx + 1,
      fechaNormalizada: dateFromRow(row),
      materialNormalizado: materialName(row),
      familiaNormalizada: familyFromMaterial(row),
      kgCalculado: r0(kgFromRow(row)),
      tnCalculado: kgToTn(kgFromRow(row)),
      obsNormalizada: obsFromRow(row)
    };
    Object.keys(row || {}).sort().forEach(k => {
      out[k] = row[k];
    });
    return out;
  });
}

function realRowsByMaterial(rows) {
  const byMaterial = {};
  (rows || []).forEach(row => {
    const material = normText(materialName(row));
    if (!material) return;
    byMaterial[material] = r0((byMaterial[material] || 0) + kgFromRow(row));
  });
  return byMaterial;
}

function grupoCompraPlanFromRow(row) {
  const fam = normText(familyFromMaterial(row));
  const mat = normText(materialName(row));
  const texto = fam + " " + mat;
  if (texto.includes("PP HOMO") || texto.includes("BAZAR TUTY") || texto.includes("BAZAR TUTTY")) return "PP HOMO";
  if (
    texto.includes("PP COPO") ||
    texto.includes("BALDE") ||
    texto.includes("BAZAR") ||
    texto.includes("TAPITA") ||
    texto.includes("TAPON") ||
    texto.includes("SILLA") ||
    fam === "NEGRO" ||
    fam === "PP"
  ) return "PP COPO";
  return "PE";
}

function realRowsByCompraGrupo(rows) {
  const out = { "PE": 0, "PP COPO": 0, "PP HOMO": 0 };
  (rows || []).forEach(row => {
    const grupo = grupoCompraPlanFromRow(row);
    out[grupo] = r0((out[grupo] || 0) + kgFromRow(row));
  });
  return out;
}
function indexStockByProduct(stockDb, criteria) {
  const byProduct = {};
  const componentsByProduct = {};
  const revisar = [];
  stockRows(stockDb).forEach(row => {
    const material = materialName(row) || "SIN MATERIAL";
    const banda = findBandaMaterial(material, criteria || {});
    const producto = productForStockMaterial(row, criteria);
    const productoObj = productoByKey(criteria, producto);
    const subramaId = subramaIdForMaterial(criteria, material, productoObj.id);
    const subrama = subramaById(criteria, productoObj.id, subramaId);
    const sugeridoKey = inferProductKeyForStockMaterial(row, criteria);
    const sugeridoObj = productoByKey(criteria, sugeridoKey);
    const fam = (banda && banda.familiaOperativa) || familyFromMaterial(row);
    const kg = kgFromRow(row);
    const minimoKg = banda ? tnToKg(banda.minimoTn) : minKgFromRow(row);
    const maximoKg = banda ? tnToKg(banda.maximoTn) : 0;
    const deficitKg = Math.max(0, minimoKg - kg);
    const excesoKg = maximoKg > 0 ? Math.max(0, kg - maximoKg) : 0;
    const comp = {
      material,
      familia: fam,
      producto,
      productoId: productoObj.id,
      productoNombre: productoObj.nombre,
      subramaId,
      subramaNombre: subrama.nombre,
      sugeridoKey,
      sugeridoId: sugeridoObj.id,
      sugeridoNombre: sugeridoObj.nombre,
      obs: obsFromRow(row),
      stockTn: kgToTn(kg),
      minimoTn: kgToTn(minimoKg),
      maximoTn: kgToTn(maximoKg),
      deficitTn: kgToTn(deficitKg),
      excesoTn: kgToTn(excesoKg),
      estado: minimoKg > 0 ? (kg < minimoKg ? "critico" : (excesoKg > 0 ? "alto" : "ok")) : "sin_minimo"
    };
    if (producto === "LIMBO") revisar.push(comp);
    byProduct[producto] = (byProduct[producto] || 0) + kg;
    if (!componentsByProduct[producto]) componentsByProduct[producto] = [];
    componentsByProduct[producto].push(comp);
  });
  Object.keys(componentsByProduct).forEach(k => {
    componentsByProduct[k].sort((a, b) => b.deficitTn - a.deficitTn || b.stockTn - a.stockTn || a.material.localeCompare(b.material));
  });
  return { byProduct, componentsByProduct, revisar };
}
function agendaRows(agendaDb, mesKey) {
  const rows = [];
  if (!agendaDb || typeof agendaDb !== "object") return rows;
  if (Array.isArray(agendaDb.turnos)) rows.push(...agendaDb.turnos.filter(x => !x.mesKey || x.mesKey === mesKey));
  if (Array.isArray(agendaDb.resumenRamaColor)) rows.push(...agendaDb.resumenRamaColor);
  if (Array.isArray(agendaDb.items)) rows.push(...agendaDb.items);
  return rows;
}

function sumFamilies(idx, families) {
  return (families || []).reduce((sum, f) => sum + (idx.byFamily[normFamilia(f)] || 0), 0);
}
function stockComponents(idx, families, bandas) {
  const bandByFamily = {};
  (bandas || []).forEach(b => { bandByFamily[normFamilia(b.familia)] = b; });
  return (families || []).map(f => {
    const fam = normFamilia(f);
    const stockKg = idx.byFamily[fam] || 0;
    const banda = bandByFamily[fam] || null;
    const minimoKg = banda ? tnToKg(banda.minimoTn) : (idx.minByFamily[fam] || 0);
    const idealKg = banda ? tnToKg(banda.idealTn || banda.minimoTn) : minimoKg;
    const maximoKg = banda ? tnToKg(banda.maximoTn || 0) : 0;
    const deficitKg = Math.max(0, minimoKg - stockKg);
    let estado = "sin_minimo";
    if (minimoKg > 0) {
      if (stockKg < minimoKg) estado = "critico";
      else if (idealKg > minimoKg && stockKg < idealKg) estado = "justo";
      else if (maximoKg > 0 && stockKg > maximoKg) estado = "alto";
      else estado = "ok";
    }
    return {
      familia: fam,
      stockTn: kgToTn(stockKg),
      minimoTn: kgToTn(minimoKg),
      idealTn: kgToTn(idealKg),
      maximoTn: kgToTn(maximoKg),
      deficitTn: kgToTn(deficitKg),
      estado
    };
  }).filter(x => x.stockTn || x.minimoTn);
}
function sumBand(idx, band) {
  const fam = normFamilia(band.familia);
  return (band.colores || []).reduce((sum, c) => sum + (idx.byFamilyColor[fam + "||" + normColor(c)] || 0), 0);
}

function buildDashboard(criteria, realDb, stockDb, agendaDb, aviso) {
  const mesKey = validMes(criteria.mesKey) ? criteria.mesKey : mesActual();
  const realesRows = collectRows(realDb, mesKey);
  const stockInfo = stockSourceInfo(stockDb);
  const stockCutDate = stockInfo.fechaStockNormalizada || parseDateKey(stockInfo.fechaStock);
  const realesDesdeStockRows = rowsFromDateInclusive(realesRows, stockCutDate);
  const realesIdx = realIndexMes(realDb, mesKey);
  const realesPostStockIdx = realIndexPostStock(realDb, mesKey, stockCutDate);
  const stockIdx = indexKg(stockRows(stockDb));
  const stockProductos = indexStockByProduct(stockDb, criteria);
  const agendaIdx = indexKg(agendaRows(agendaDb, mesKey));

  const plan = (criteria.planFamilias || []).map(rule => {
    const objetivoKg = tnToKg(rule.objetivoTn);
    const realKg = sumFamilies(realesIdx, rule.familias);
    const realPostStockKg = sumFamilies(realesPostStockIdx, rule.familias);
    const agendaKg = sumFamilies(agendaIdx, rule.familias);
    const stockKg = stockProductos.byProduct[rule.key] || sumFamilies(stockIdx, rule.familias);
    const componentesStock = stockProductos.componentsByProduct[rule.key] || stockComponents(stockIdx, rule.familias, rule.stockBandas);
    const stockDeficitTn = r1(componentesStock.reduce((s, x) => s + x.deficitTn, 0));
    const stockEstado = componentesStock.some(x => x.estado === "critico") ? "critico" :
      (componentesStock.some(x => x.estado === "justo") ? "justo" : "ok");
    const cubiertoKg = realKg + agendaKg;
    const faltanteKg = Math.max(0, objetivoKg - cubiertoKg);
    let estado = "comprar";
    let accion = "Llamar proveedores";
    if (faltanteKg <= 0) { estado = "ok"; accion = "No comprar por plan"; }
    else if (faltanteKg <= objetivoKg * 0.15) { estado = "mirar"; accion = "Completar fino"; }
    return {
      key: rule.key,
      nombre: rule.nombre,
      objetivoTn: r1(rule.objetivoTn),
      realTn: kgToTn(realKg),
      realPostStockTn: kgToTn(realPostStockKg),
      agendaTn: kgToTn(agendaKg),
      stockTn: kgToTn(stockKg),
      cubiertoTn: kgToTn(cubiertoKg),
      faltanteTn: kgToTn(faltanteKg),
      avance: objetivoKg > 0 ? Math.min(1, cubiertoKg / objetivoKg) : 0,
      familias: rule.familias || [],
      componentesStock,
      stockDeficitTn,
      stockEstado,
      foco: rule.foco || "",
      criterio: rule.criterio || "",
      estado,
      accion
    };
  });

  const bandas = (criteria.bandasStock || []).map(rule => {
    const stockKg = sumBand(stockIdx, rule);
    const agendaKg = sumBand(agendaIdx, rule);
    const disponibleKg = stockKg + agendaKg;
    const minKg = tnToKg(rule.minimoTn);
    const idealKg = tnToKg(rule.idealTn);
    const maxKg = tnToKg(rule.maximoTn);
    let estado = "ok";
    let accion = "Mantener";
    let sugeridoKg = 0;
    if (disponibleKg < minKg) {
      estado = "urgente";
      sugeridoKg = Math.max(0, idealKg - disponibleKg);
      accion = "Comprar hasta volver a banda";
    } else if (disponibleKg > maxKg) {
      estado = "frenar";
      accion = "Frenar compras";
    }
    return {
      key: rule.key,
      nombre: rule.nombre,
      familia: rule.familia,
      colores: rule.colores || [],
      minimoTn: r1(rule.minimoTn),
      idealTn: r1(rule.idealTn),
      maximoTn: r1(rule.maximoTn),
      stockTn: kgToTn(stockKg),
      agendaTn: kgToTn(agendaKg),
      disponibleTn: kgToTn(disponibleKg),
      sugeridoTn: kgToTn(sugeridoKg),
      estado,
      accion,
      criterio: rule.criterio || ""
    };
  });

  const controles = (criteria.controlStock || []).map(rule => {
    const componentes = stockProductos.componentsByProduct[rule.key] || [];
    const stockTn = r1(componentes.reduce((s, x) => s + x.stockTn, 0));
    const minimoTn = r1(componentes.reduce((s, x) => s + x.minimoTn, 0));
    const deficitTn = r1(componentes.reduce((s, x) => s + x.deficitTn, 0));
    const estado = deficitTn > 0 ? "revisar" : (stockTn > 0 ? "ok" : "sin_stock");
    return {
      key: rule.key,
      nombre: rule.nombre,
      criterio: rule.criterio || "",
      stockTn,
      minimoTn,
      deficitTn,
      estado,
      componentes
    };
  }).filter(x => x.stockTn || x.minimoTn || x.componentes.length);

  const objetivoTotalTn = plan.reduce((s, x) => s + x.objetivoTn, 0);
  const realTotalTn = r1(plan.reduce((s, x) => s + x.realTn, 0));
  const agendaTotalTn = r1(plan.reduce((s, x) => s + x.agendaTn, 0));
  const faltanteTotalTn = r1(plan.reduce((s, x) => s + x.faltanteTn, 0));
  const tareas = []
    .concat(bandas.filter(x => x.estado === "urgente").map(x => ({
      prioridad: 1,
      tipo: "banda_stock",
      titulo: x.nombre,
      accion: x.accion,
      tn: x.sugeridoTn,
      motivo: "Stock por debajo de " + x.minimoTn + " tn"
    })))
    .concat(plan.filter(x => x.faltanteTn > 0 || x.stockDeficitTn > 0).map(x => ({
      prioridad: x.stockDeficitTn > 0 ? 1 : (x.estado === "comprar" ? 2 : 3),
      tipo: "plan_mensual",
      titulo: x.nombre,
      accion: x.accion,
      tn: Math.max(x.faltanteTn, x.stockDeficitTn || 0),
      motivo: x.stockDeficitTn > 0 ? ("Stock interno abajo de minimo: " + x.stockDeficitTn + " tn") : "Faltante contra objetivo mensual"
    })))
    .sort((a, b) => a.prioridad - b.prioridad || b.tn - a.tn);

  const stockMapeo = Object.values(stockProductos.componentsByProduct)
    .flat()
    .sort((a, b) => a.productoId - b.productoId || b.stockTn - a.stockTn || a.material.localeCompare(b.material));
  const stockPaneles = (criteria.productosTerminados || PRODUCTOS_TERMINADOS).map(p => {
    const materiales = stockMapeo
      .filter(m => Number(m.productoId) === Number(p.id))
      .sort((a, b) => a.subramaId - b.subramaId || b.stockTn - a.stockTn || a.material.localeCompare(b.material));
    const subramasBase = subramasForProduct(criteria, p.id);
    let subramas = subramasBase.map(s => {
      const mats = materiales
        .filter(m => Number(m.subramaId) === Number(s.id))
        .sort((a, b) => b.stockTn - a.stockTn || a.material.localeCompare(b.material));
      const totalTn = r1(mats.reduce((sum, m) => sum + n(m.stockTn), 0));
      const minimoTn = r1(mats.reduce((sum, m) => sum + n(m.minimoTn), 0));
      const deficitTn = r1(mats.reduce((sum, m) => sum + n(m.deficitTn), 0));
      const maximoTn = r1(mats.reduce((sum, m) => sum + n(m.maximoTn), 0));
      return {
        id: s.id,
        nombre: s.nombre,
        totalTn,
        minimoTn,
        maximoTn,
        deficitTn,
        cobertura: minimoTn > 0 ? Math.min(1, totalTn / minimoTn) : 1,
        estado: deficitTn > 0 ? "critico" : "ok",
        materiales: mats
      };
    })
      .sort((a, b) => b.totalTn - a.totalTn || Number(a.id) - Number(b.id));
    const panelTotalTn = r1(materiales.reduce((s, m) => s + n(m.stockTn), 0));
    subramas = subramas.map((s, idx) => Object.assign({}, s, {
      color: PIE_COLORS[idx % PIE_COLORS.length],
      pct: panelTotalTn > 0 ? r1((s.totalTn / panelTotalTn) * 100) : 0
    }));
    const pieParts = subramas.map(s => ({ value: s.totalTn, color: s.color }));
    return {
      id: p.id,
      key: p.key,
      nombre: p.nombre,
      tipo: p.tipo,
      totalTn: panelTotalTn,
      deficitTn: r1(materiales.reduce((s, m) => s + n(m.deficitTn), 0)),
      pieGradient: pieGradient(pieParts),
      subramas,
      materiales
    };
  })
    .filter(p => p.materiales.length || Number(p.id) === 0)
    .sort((a, b) => b.totalTn - a.totalTn || Number(a.id) - Number(b.id));

  msg.payload = {
    version: 2,
    tipo: "cavernicola_tablero_v2",
    generadoEn: new Date().toISOString(),
    mesKey,
    aviso: aviso || "",
    meta: {
      objetivoTotalTn,
      realTotalTn,
      agendaTotalTn,
      faltanteTotalTn,
      fuentes: {
        criterio: !!criteria.actualizado,
        reales: !!(realDb && Object.keys(realDb).length),
        stock: !!(stockDb && Object.keys(stockDb).length),
        agenda: !!(agendaDb && Object.keys(agendaDb).length)
      }
    },
    criterio: criteria,
    plan,
    bandas,
    controles,
    tareas,
    stockMapeo,
    stockPaneles,
    productosTerminados: criteria.productosTerminados || PRODUCTOS_TERMINADOS,
    stockRevisar: stockProductos.revisar,
    stockAuditoria: {
      fuente: STOCK_FILE,
      info: stockInfo,
      filas: stockAuditRows(stockDb, criteria)
    },
    realesAuditoria: {
      fuente: REALES_FILE,
      info: {
        mesKey,
        fechaStockCorte: stockInfo.fechaStock || "",
        fechaStockCorteNormalizada: stockCutDate,
        fechaStockOrigen: stockInfo.fechaStockOrigen || "",
        postStockModo: stockCutDate ? "desde_stock_inclusive" : "sin_fecha_stock",
        registrosMes: realesRows.length,
        registrosPostStock: realesDesdeStockRows.length
      },
      porMaterial: realRowsByMaterial(realesDesdeStockRows),
      porMaterialMes: realRowsByMaterial(realesRows),
      porGrupoCompra: realRowsByCompraGrupo(realesDesdeStockRows),
      porGrupoCompraMes: realRowsByCompraGrupo(realesRows),
      filasPostStock: realAuditRows(realesDesdeStockRows),
      filas: realAuditRows(realesRows)
    }
  };
  return msg;
}

function persistCriteria(criteria) {
  criteria.actualizado = new Date().toISOString();
  return { filename: PLAN_FILE, payload: JSON.stringify(criteria, null, 2) };
}

let criteria = ensureCriteria(flow.get("cavernicola_v2_criteria") || {});
let realDb = flow.get("cavernicola_v2_reales") || {};
let stockDb = flow.get("cavernicola_v2_stock") || {};
let agendaDb = flow.get("cavernicola_v2_agenda") || {};
let accion = msg.accion || msg.topic || (msg.payload && msg.payload.accion) || "";

if (accion === "cargarCriterio") {
  criteria = ensureCriteria(parseJsonPayload(msg.payload, defaultCriteria()));
  flow.set("cavernicola_v2_criteria", criteria);
  return [buildDashboard(criteria, realDb, stockDb, agendaDb, "Criterio v2 cargado"), null];
}
if (accion === "cargarReales") {
  realDb = parseJsonPayload(msg.payload, {});
  flow.set("cavernicola_v2_reales", realDb);
  return [buildDashboard(criteria, realDb, stockDb, agendaDb, "Reales cargados"), null];
}
if (accion === "cargarStock") {
  stockDb = parseJsonPayload(msg.payload, {});
  flow.set("cavernicola_v2_stock", stockDb);
  return [buildDashboard(criteria, realDb, stockDb, agendaDb, "Stock cargado"), null];
}
if (accion === "cargarAgenda") {
  agendaDb = parseJsonPayload(msg.payload, {});
  flow.set("cavernicola_v2_agenda", agendaDb);
  return [buildDashboard(criteria, realDb, stockDb, agendaDb, "Agenda cargada"), null];
}
if (accion === "resetCriterio") {
  criteria = defaultCriteria();
  flow.set("cavernicola_v2_criteria", criteria);
  return [buildDashboard(criteria, realDb, stockDb, agendaDb, "Criterio base restaurado: 100 PEMD, 50 PP Copo, 50 PP Homo"), null];
}
if (accion === "mesAnterior" || accion === "mesSiguiente") {
  const mesActualCriteria = criteria.mesKey || mesActual();
  saveMesMeta(criteria, mesActualCriteria);
  const nuevoMes = addMes(mesActualCriteria, accion === "mesAnterior" ? -1 : 1);
  applyMesMeta(criteria, nuevoMes);
  flow.set("cavernicola_v2_criteria", criteria);
  return [buildDashboard(criteria, realDb, stockDb, agendaDb, "Mes ajustado: meta leida desde JSON/criterio"), null];
}
if (accion === "ajustarObjetivoTotal") {
  const delta = n((msg.payload || {}).deltaTn, 0);
  const actual = r1((criteria.planFamilias || []).reduce((s, x) => s + n(x.objetivoTn), 0));
  const nuevo = Math.max(0, r1(actual + delta));
  const factor = actual > 0 ? nuevo / actual : 1;
  criteria.planFamilias = (criteria.planFamilias || []).map(x => Object.assign({}, x, { objetivoTn: r1(n(x.objetivoTn) * factor) }));
  criteria.objetivoTotalTn = r1(criteria.planFamilias.reduce((s, x) => s + n(x.objetivoTn), 0));
  saveMesMeta(criteria, criteria.mesKey);
  flow.set("cavernicola_v2_criteria", criteria);
  return [buildDashboard(criteria, realDb, stockDb, agendaDb, "Meta total ajustada para este mes sin guardar"), null];
}
if (accion === "ajustarObjetivo") {
  const p = msg.payload || {};
  const key = String(p.key || "");
  const delta = n(p.deltaTn, 0);
  criteria.planFamilias = (criteria.planFamilias || []).map(x => x.key === key ? Object.assign({}, x, { objetivoTn: Math.max(0, r1(n(x.objetivoTn) + delta)) }) : x);
  criteria.objetivoTotalTn = r1(criteria.planFamilias.reduce((s, x) => s + n(x.objetivoTn), 0));
  saveMesMeta(criteria, criteria.mesKey);
  flow.set("cavernicola_v2_criteria", criteria);
  return [buildDashboard(criteria, realDb, stockDb, agendaDb, "Objetivo ajustado para este mes sin guardar"), null];
}
if (accion === "ajustarBanda") {
  const p = msg.payload || {};
  const material = String(p.material || "").trim();
  const campo = p.campo === "maximoTn" ? "maximoTn" : "minimoTn";
  const delta = n(p.deltaTn, 0);
  criteria.bandasMaterial = mergeBandasMaterial(criteria.bandasMaterial || []);
  let touched = false;
  criteria.bandasMaterial = criteria.bandasMaterial.map(x => {
    if (normText(x.material) !== normText(material)) return x;
    touched = true;
    const next = Object.assign({}, x);
    next[campo] = Math.max(0, r1(n(next[campo]) + delta));
    if (next.maximoTn > 0 && next.minimoTn > next.maximoTn) {
      if (campo === "minimoTn") next.maximoTn = next.minimoTn;
      else next.minimoTn = next.maximoTn;
    }
    return next;
  });
  if (!touched && material) {
    const inferred = {
      material,
      productoCompra: p.producto || "REVISAR",
      familiaOperativa: p.familia || "SIN RAMA",
      minimoTn: campo === "minimoTn" ? Math.max(0, delta) : 0,
      maximoTn: campo === "maximoTn" ? Math.max(0, delta) : 0
    };
    criteria.bandasMaterial.push(inferred);
  }
  flow.set("cavernicola_v2_criteria", criteria);
  return [buildDashboard(criteria, realDb, stockDb, agendaDb, "Banda de stock ajustada sin guardar"), null];
}
if (accion === "setBandaMaterial") {
  const p = msg.payload || {};
  const material = String(p.material || "").trim();
  if (material) {
    const exactKey = normText(material);
    const prev = findBandaMaterial(material, criteria) || {};
    let minimoTn = p.minimoTn !== undefined ? Math.max(0, r1(n(p.minimoTn, 0))) : r1(prev.minimoTn || 0);
    let maximoTn = p.maximoTn !== undefined ? Math.max(0, r1(n(p.maximoTn, 0))) : r1(prev.maximoTn || 0);
    if (maximoTn > 0 && minimoTn > maximoTn) maximoTn = minimoTn;

    const next = Object.assign({}, prev, {
      material,
      productoCompra: p.producto || prev.productoCompra || "REVISAR",
      familiaOperativa: p.familia || prev.familiaOperativa || "SIN RAMA",
      minimoTn,
      maximoTn
    });

    criteria.bandasMaterial = mergeBandasMaterial(criteria.bandasMaterial || [])
      .filter(x => normText(x.material) !== exactKey);
    criteria.bandasMaterial.push(next);
    criteria.bandasMaterial = mergeBandasMaterial(criteria.bandasMaterial);
    flow.set("cavernicola_v2_criteria", criteria);
  }
  return [buildDashboard(criteria, realDb, stockDb, agendaDb, "Banda de " + material + " actualizada sin guardar. Usar guardar JSON para persistir."), null];
}
if (accion === "ajustarProductoMaterial") {
  const p = msg.payload || {};
  const material = String(p.material || "").trim();
  const delta = r0(p.deltaId || 0);
  const key = normText(material);
  if (key) {
    criteria.asignacionesMaterial = (criteria.asignacionesMaterial && typeof criteria.asignacionesMaterial === "object") ? criteria.asignacionesMaterial : {};
    const maxId = (criteria.productosTerminados || PRODUCTOS_TERMINADOS).reduce((m, x) => Math.max(m, r0(x.id)), 0);
    const actual = criteria.asignacionesMaterial[key] !== undefined ? r0(criteria.asignacionesMaterial[key]) : r0(p.actualId || 0);
    criteria.asignacionesMaterial[key] = Math.max(0, Math.min(maxId, actual + delta));
    flow.set("cavernicola_v2_criteria", criteria);
  }
  return [buildDashboard(criteria, realDb, stockDb, agendaDb, "Asignacion de producto ajustada sin guardar"), null];
}
if (accion === "setRamaMaterial") {
  const p = msg.payload || {};
  const material = String(p.material || "").trim();
  const productoId = Math.max(0, r0(p.productoId || 0));
  const actualProductoId = Math.max(0, r0(p.actualProductoId || 0));
  if (material) {
    criteria.productosTerminados = mergeProductosTerminados(criteria.productosTerminados);
    criteria.subramasProducto = mergeSubramasProducto(criteria.subramasProducto, criteria.productosTerminados || PRODUCTOS_TERMINADOS);
    criteria.asignacionesMaterial = (criteria.asignacionesMaterial && typeof criteria.asignacionesMaterial === "object") ? criteria.asignacionesMaterial : {};
    criteria.asignacionesSubramaMaterial = (criteria.asignacionesSubramaMaterial && typeof criteria.asignacionesSubramaMaterial === "object") ? criteria.asignacionesSubramaMaterial : {};
    const maxId = (criteria.productosTerminados || PRODUCTOS_TERMINADOS).reduce((m, x) => Math.max(m, r0(x.id)), 0);
    const nextProductoId = Math.max(0, Math.min(maxId, productoId));
    const key = normText(material);
    criteria.asignacionesMaterial[key] = nextProductoId;
    delete criteria.asignacionesSubramaMaterial[subramaAssignKey(material, actualProductoId)];
    criteria.asignacionesSubramaMaterial[subramaAssignKey(material, nextProductoId)] = 0;
    flow.set("cavernicola_v2_criteria", criteria);
  }
  return [buildDashboard(criteria, realDb, stockDb, agendaDb, "Rama de " + material + " actualizada y guardada en JSON"), persistCriteria(criteria)];
}
if (accion === "setSubramaMaterial") {
  const p = msg.payload || {};
  const material = String(p.material || "").trim();
  const productoId = r0(p.productoId || 0);
  const subramaId = Math.max(0, r0(p.subramaId || 0));
  if (material) {
    criteria.subramasProducto = mergeSubramasProducto(criteria.subramasProducto, criteria.productosTerminados || PRODUCTOS_TERMINADOS);
    criteria.asignacionesSubramaMaterial = (criteria.asignacionesSubramaMaterial && typeof criteria.asignacionesSubramaMaterial === "object") ? criteria.asignacionesSubramaMaterial : {};
    const maxId = subramasForProduct(criteria, productoId).reduce((m, x) => Math.max(m, r0(x.id)), 0);
    const nextId = Math.max(0, Math.min(maxId, subramaId));
    criteria.asignacionesSubramaMaterial[subramaAssignKey(material, productoId)] = nextId;
    delete criteria.asignacionesSubramaMaterial[normText(material)];
    flow.set("cavernicola_v2_criteria", criteria);
  }
  return [buildDashboard(criteria, realDb, stockDb, agendaDb, "Subrama de " + material + " actualizada y guardada en JSON"), persistCriteria(criteria)];
}
if (accion === "ajustarSubramaMaterial") {
  const p = msg.payload || {};
  const material = String(p.material || "").trim();
  const productoId = r0(p.productoId || 0);
  const delta = r0(p.deltaId || 0);
  const key = normText(material);
  if (key) {
    criteria.subramasProducto = mergeSubramasProducto(criteria.subramasProducto, criteria.productosTerminados || PRODUCTOS_TERMINADOS);
    criteria.asignacionesSubramaMaterial = (criteria.asignacionesSubramaMaterial && typeof criteria.asignacionesSubramaMaterial === "object") ? criteria.asignacionesSubramaMaterial : {};
    const maxId = subramasForProduct(criteria, productoId).reduce((m, x) => Math.max(m, r0(x.id)), 0);
    const scopedKey = subramaAssignKey(material, productoId);
    const actual = criteria.asignacionesSubramaMaterial[scopedKey] !== undefined ? r0(criteria.asignacionesSubramaMaterial[scopedKey]) : r0(p.actualId || 0);
    criteria.asignacionesSubramaMaterial[scopedKey] = Math.max(0, Math.min(maxId, actual + delta));
    flow.set("cavernicola_v2_criteria", criteria);
  }
  return [buildDashboard(criteria, realDb, stockDb, agendaDb, "Subrama ajustada sin guardar"), null];
}
if (accion === "aplicarSugeridoMaterial") {
  const p = msg.payload || {};
  const material = String(p.material || "").trim();
  const key = normText(material);
  if (key) {
    criteria.asignacionesMaterial = (criteria.asignacionesMaterial && typeof criteria.asignacionesMaterial === "object") ? criteria.asignacionesMaterial : {};
    const maxId = (criteria.productosTerminados || PRODUCTOS_TERMINADOS).reduce((m, x) => Math.max(m, r0(x.id)), 0);
    criteria.asignacionesMaterial[key] = Math.max(0, Math.min(maxId, r0(p.sugeridoId || 0)));
    flow.set("cavernicola_v2_criteria", criteria);
  }
  return [buildDashboard(criteria, realDb, stockDb, agendaDb, "Sugerencia aplicada sin guardar"), null];
}
if (accion === "renombrarProducto") {
  const p = msg.payload || {};
  const id = r0(p.id);
  const nombre = String(p.nombre || "").trim();
  criteria.productosTerminados = mergeProductosTerminados(criteria.productosTerminados);
  criteria.productosTerminados = criteria.productosTerminados.map(x => Number(x.id) === id ? Object.assign({}, x, { nombre: nombre || x.nombre }) : x);
  flow.set("cavernicola_v2_criteria", criteria);
  return [buildDashboard(criteria, realDb, stockDb, agendaDb, "Nombre de rama ajustado sin guardar"), null];
}
if (accion === "renombrarSubrama") {
  const p = msg.payload || {};
  criteria = setSubramaNombre(criteria, p.productoId, p.subId, p.nombre);
  flow.set("cavernicola_v2_criteria", criteria);
  return [buildDashboard(criteria, realDb, stockDb, agendaDb, "Nombre de subrama ajustado sin guardar"), null];
}
if (accion === "agregarProducto") {
  criteria.productosTerminados = mergeProductosTerminados(criteria.productosTerminados);
  const nextId = criteria.productosTerminados.reduce((m, x) => Math.max(m, r0(x.id)), 0) + 1;
  criteria.productosTerminados.push({ id: nextId, key: "RAMA_" + nextId, nombre: "Nueva rama " + nextId, tipo: "control" });
  criteria.subramasProducto = mergeSubramasProducto(criteria.subramasProducto, criteria.productosTerminados);
  flow.set("cavernicola_v2_criteria", criteria);
  return [buildDashboard(criteria, realDb, stockDb, agendaDb, "Rama agregada sin guardar"), null];
}
if (accion === "agregarSubrama") {
  const p = msg.payload || {};
  const productoId = String(r0(p.productoId));
  criteria.subramasProducto = mergeSubramasProducto(criteria.subramasProducto, criteria.productosTerminados || PRODUCTOS_TERMINADOS);
  const list = subramasForProduct(criteria, productoId);
  const nextId = list.reduce((m, x) => Math.max(m, r0(x.id)), 0) + 1;
  criteria.subramasProducto[productoId] = list.concat([{ id: nextId, nombre: "Subrama " + nextId }]);
  flow.set("cavernicola_v2_criteria", criteria);
  return [buildDashboard(criteria, realDb, stockDb, agendaDb, "Subrama agregada sin guardar"), null];
}
if (accion === "guardarCriterio") {
  saveMesMeta(criteria, criteria.mesKey);
  flow.set("cavernicola_v2_criteria", criteria);
  return [buildDashboard(criteria, realDb, stockDb, agendaDb, "Criterio guardado en planComprasV2.json"), persistCriteria(criteria)];
}

flow.set("cavernicola_v2_criteria", criteria);
return [buildDashboard(criteria, realDb, stockDb, agendaDb, ""), null];
