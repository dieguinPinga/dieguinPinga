// Estructurar arbol Cavernicola
// Entrada esperada:
// msg.payload = cavernicola_tablero_v2
//
// Salida:
// msg.payload.arbol = [
//   rama -> subramas -> materiales
// ]

const data = msg.payload || {};
const paneles = Array.isArray(data.stockPaneles) ? data.stockPaneles : [];
const productos = Array.isArray(data.productosTerminados) ? data.productosTerminados : [];
const criterio = data.criterio && typeof data.criterio === "object" ? data.criterio : {};
const subramasProducto = criterio.subramasProducto && typeof criterio.subramasProducto === "object" ? criterio.subramasProducto : {};
const ramaOptionsGlobal = productos.map(p => {
    const pid = Number(p.id);
    const byId = {};
    const persistidas = Array.isArray(subramasProducto[String(pid)]) ? subramasProducto[String(pid)] : [];
    const panel = paneles.find(x => Number(x.id) === pid);
    const calculadas = panel && Array.isArray(panel.subramas) ? panel.subramas : [];
    persistidas.concat(calculadas).forEach(s => {
        const sid = Number(s.id);
        if (!Number.isFinite(sid)) return;
        byId[sid] = { id: sid, nombre: String(s.nombre || (sid === 0 ? "Sin subrama" : "Subrama " + sid)) };
    });
    if (!byId[0]) byId[0] = { id: 0, nombre: "Sin subrama" };
    return {
        id: pid,
        nombre: p.nombre,
        subramas: Object.values(byId).sort((a,b) => a.id - b.id)
    };
});

function n(v) {
    const x = Number(v || 0);
    return Math.round(x * 10) / 10;
}

function estadoMaterial(m) {
    if (!m) return "";
    if (n(m.excesoTn) > 0) return `SOBRA ${n(m.excesoTn)}`;
    if (m.estado === "banda_grupo") return "BANDA GRUPO";
    if (m.estado === "ok") return "OK";
    if (m.estado === "sin_minimo") return "SIN MIN";
    if (n(m.deficitTn) > 0) return `FALTA ${n(m.deficitTn)}`;
    return String(m.estado || "").toUpperCase();
}

function estadoRama(deficitTn, excesoTn) {
    if (n(deficitTn) > 0) return "FALTA COMPRA";
    if (n(excesoTn) > 0) return "SOBRA STOCK";
    return "OK";
}

function estadoSubrama(deficitTn, excesoTn) {
    if (n(deficitTn) > 0) return "CRITICO";
    if (n(excesoTn) > 0) return "SOBRA";
    return "OK";
}

const arbol = [];
const materialesPlano = [];
const asignacionesPlano = [];

function materialDedupKey(m) {
    return String(m && m.material || "").trim().toUpperCase();
}

function mergeMaterialRows(a, b) {
    const current = Object.assign({}, a || {});
    const incoming = Object.assign({}, b || {});
    const currentIsLimbo = Number(current.ramaId) === 0 || String(current.ramaNombre || "").toUpperCase().includes("LIMBO");
    const incomingIsAssigned = Number(incoming.ramaId) !== 0 && !String(incoming.ramaNombre || "").toUpperCase().includes("LIMBO");
    const out = currentIsLimbo && incomingIsAssigned ? incoming : current;
    out.obs = [current.obs, incoming.obs].filter(Boolean).filter((v, i, arr) => arr.indexOf(v) === i).join(" | ");
    return out;
}

function dedupeMaterialList(list) {
    const byKey = {};
    (list || []).forEach(m => {
        const key = materialDedupKey(m);
        if (!key) return;
        byKey[key] = byKey[key] ? mergeMaterialRows(byKey[key], m) : Object.assign({}, m);
    });
    return Object.values(byKey);
}

const resumenStock = {
    stockTotalTn: 0,
    stockPlanTn: 0,
    stockControlTn: 0,
    stockLimboTn: 0,
    materialesPlan: 0,
    materialesControl: 0,
    materialesLimbo: 0
};

for (const panel of paneles) {
    const panelStockTn = n(panel.totalTn);
    const panelMateriales = Array.isArray(panel.materiales) ? panel.materiales.length : 0;
    resumenStock.stockTotalTn = n(resumenStock.stockTotalTn + panelStockTn);
    if (panel.tipo === "plan") {
        resumenStock.stockPlanTn = n(resumenStock.stockPlanTn + panelStockTn);
        resumenStock.materialesPlan += panelMateriales;
    } else if (panel.key === "LIMBO" || Number(panel.id) === 0 || panel.tipo === "limbo") {
        resumenStock.stockLimboTn = n(resumenStock.stockLimboTn + panelStockTn);
        resumenStock.materialesLimbo += panelMateriales;
    } else {
        resumenStock.stockControlTn = n(resumenStock.stockControlTn + panelStockTn);
        resumenStock.materialesControl += panelMateriales;
    }

    const rama = {
        ramaId: panel.id,
        ramaKey: panel.key,
        ramaNombre: panel.nombre,
        tipo: panel.tipo,
        stockTn: n(panel.totalTn),
        faltanteTn: n(panel.deficitTn),
        sobraTn: n(panel.excesoTn),
        estado: estadoRama(panel.deficitTn, panel.excesoTn),
        subramas: []
    };

    const subramas = Array.isArray(panel.subramas) ? panel.subramas : [];

    for (const sub of subramas) {
        const grupo = (sub.bandasGrupo || [])[0] || null;
        const subrama = {
            ramaId: panel.id,
            ramaNombre: panel.nombre,

            subramaId: sub.id,
            subramaNombre: sub.nombre,

            stockTn: n(sub.totalTn),
            minimoTn: n(sub.minimoTn),
            maximoTn: n(sub.maximoTn),
            faltanteTn: n(sub.deficitTn),
            sobraTn: n(sub.excesoTn),
            coberturaPct: n((sub.cobertura || 0) * 100),
            estado: estadoSubrama(sub.deficitTn, sub.excesoTn),
            bandasGrupo: sub.bandasGrupo || [],
            bandaGrupoActiva: !!grupo,
            bandaGrupoKey: grupo ? grupo.key : "",
            bandaGrupoNombre: grupo ? grupo.nombre : "",
            bandaGrupoMinimoTn: grupo ? n(grupo.minimoTn) : n(sub.minimoTn),
            bandaGrupoMaximoTn: grupo ? n(grupo.maximoTn) : n(sub.maximoTn),
            bandaGrupoStockTn: grupo ? n(grupo.stockTn) : n(sub.totalTn),
            bandaGrupoDeficitTn: grupo ? n(grupo.deficitTn) : 0,
            bandaGrupoExcesoTn: grupo ? n(grupo.excesoTn) : 0,
            bandaGrupoMargenSobreMinimoTn: grupo ? n(grupo.margenSobreMinimoTn) : Math.max(0, n(sub.totalTn) - n(sub.minimoTn)),
            bandaGrupoHastaMaximoTn: grupo ? n(grupo.hastaMaximoTn) : Math.max(0, n(sub.maximoTn) - n(sub.totalTn)),
            bandaGrupoDentroDeRango: grupo ? !!grupo.dentroDeRango : (n(sub.totalTn) >= n(sub.minimoTn) && (!n(sub.maximoTn) || n(sub.totalTn) <= n(sub.maximoTn))),
            bandaGrupoMateriales: grupo && Array.isArray(grupo.materiales) ? grupo.materiales.slice() : [],
            bandaGrupoMaterialesTexto: grupo && Array.isArray(grupo.materiales) ? grupo.materiales.join(', ') : '',

            materiales: []
        };

        const materiales = Array.isArray(sub.materiales) ? sub.materiales : [];

        for (const m of materiales) {
            const material = {
                ramaId: panel.id,
                ramaKey: panel.key,
                ramaNombre: panel.nombre,

                subramaId: sub.id,
                subramaNombre: sub.nombre,
                ramaOptions: ramaOptionsGlobal,
                ramaSubramas: subramas.map(x => ({ id: Number(x.id), nombre: x.nombre })),
                destinoRamaId: Number(panel.id),
                destinoSubramaId: Number(sub.id),

                // material real
                material: m.material,
                familia: m.familia,
                stockTn: n(m.stockTn),
                paraMolerTn: n(m.paraMolerTn),
                paraLavarTn: n(m.paraLavarTn),
                lavadoTn: n(m.lavadoTn),
                extruidoTn: n(m.extruidoTn),
                totalEstadosTn: n(m.totalEstadosTn),
                minimoTn: n(m.minimoTn),
                maximoTn: n(m.maximoTn),
                faltanteTn: n(m.deficitTn),
                excesoTn: n(m.excesoTn),
                bandaGrupoKey: m.bandaGrupoKey || "",
                bandaGrupoNombre: m.bandaGrupoNombre || "",
                bandaGrupoModo: m.bandaGrupoModo || "",
                bandaGrupoMinimoTn: n(m.bandaGrupoMinimoTn),
                bandaGrupoMaximoTn: n(m.bandaGrupoMaximoTn),
                bandaGrupoStockTn: n(m.bandaGrupoStockTn),
                bandaGrupoDeficitTn: n(m.bandaGrupoDeficitTn),
                bandaGrupoExcesoTn: n(m.bandaGrupoExcesoTn),
                minimoOriginalTn: n(m.minimoOriginalTn),
                maximoOriginalTn: n(m.maximoOriginalTn),

                // lectura
                estado: estadoMaterial(m),
                obs: m.obs || "",
                productoIdActual: m.productoId,
                productoNombreActual: m.productoNombre,
                sugeridoId: m.sugeridoId,
                sugeridoNombre: m.sugeridoNombre,
                asignacionMaterial: {
                    material: m.material,
                    productoId: panel.id,
                    productoNombre: panel.nombre,
                    subramaId: sub.id,
                    subramaNombre: sub.nombre
                }
            };

            subrama.materiales.push(material);
            materialesPlano.push(material);

            asignacionesPlano.push({
                material: m.material,
                ramaId: panel.id,
                rama: panel.nombre,
                subramaId: sub.id,
                subrama: sub.nombre,
                familia: m.familia,
                stockTn: n(m.stockTn),
                minimoTn: n(m.minimoTn),
                maximoTn: n(m.maximoTn),
                faltanteTn: n(m.deficitTn),
                sobraTn: n(m.excesoTn),
                estado: estadoMaterial(m)
            });
        }

        subrama.materiales = dedupeMaterialList(subrama.materiales);
        if (grupo) {
            subrama.stockTn = n(grupo.stockTn);
            subrama.minimoTn = n(grupo.minimoTn);
            subrama.maximoTn = n(grupo.maximoTn);
            subrama.faltanteTn = n(grupo.deficitTn);
            subrama.sobraTn = n(grupo.excesoTn);
        } else {
            subrama.stockTn = n(subrama.materiales.reduce((s, m) => s + Number(m.stockTn || 0), 0));
            subrama.minimoTn = n(subrama.materiales.reduce((s, m) => s + Number(m.minimoTn || 0), 0));
            subrama.maximoTn = n(subrama.materiales.reduce((s, m) => s + Number(m.maximoTn || 0), 0));
            subrama.faltanteTn = n(subrama.materiales.reduce((s, m) => s + Number(m.faltanteTn || 0), 0));
            subrama.sobraTn = n(subrama.materiales.reduce((s, m) => s + Number(m.excesoTn || 0), 0));
        }
        subrama.coberturaPct = subrama.minimoTn > 0 ? n((subrama.stockTn / subrama.minimoTn) * 100) : 100;
        subrama.estado = estadoSubrama(subrama.faltanteTn, subrama.sobraTn);

        subrama.materiales.sort((a, b) => {
            return b.faltanteTn - a.faltanteTn ||
                   b.stockTn - a.stockTn ||
                   a.material.localeCompare(b.material);
        });

        rama.subramas.push(subrama);
    }

    rama.subramas.sort((a, b) => {
        return b.faltanteTn - a.faltanteTn ||
               b.stockTn - a.stockTn ||
               a.subramaId - b.subramaId;
    });

    rama.stockTn = n(rama.subramas.reduce((s, sub) => s + Number(sub.stockTn || 0), 0));
    rama.faltanteTn = n(rama.subramas.reduce((s, sub) => s + Number(sub.faltanteTn || 0), 0));
    rama.sobraTn = n(rama.subramas.reduce((s, sub) => s + Number(sub.sobraTn || 0), 0));
    rama.estado = estadoRama(rama.faltanteTn, rama.sobraTn);
    arbol.push(rama);
}

const materialesPlanoDedup = dedupeMaterialList(materialesPlano).sort((a, b) => {
    return b.faltanteTn - a.faltanteTn ||
           a.ramaId - b.ramaId ||
           a.subramaId - b.subramaId ||
           b.stockTn - a.stockTn;
});

const asignacionesPlanoDedup = dedupeMaterialList(asignacionesPlano).sort((a, b) => {
    return a.ramaId - b.ramaId ||
           a.subramaId - b.subramaId ||
           a.material.localeCompare(b.material);
});

arbol.sort((a, b) => {
    return b.faltanteTn - a.faltanteTn ||
           b.stockTn - a.stockTn ||
           a.ramaId - b.ramaId;
});


msg.payload = {
    mesKey: data.mesKey || "",
    generadoEn: data.generadoEn || new Date().toISOString(),
    mixMensual: data.mixMensual || {},
    modeloCompras: data.modeloCompras || {},
    plan: data.plan || [],
    realesAuditoria: data.realesAuditoria || {
        fuente: "",
        info: { mesKey: data.mesKey || "", registros: 0 },
        porMaterial: {},
        filas: []
    },

    resumen: {
        ramas: arbol.length,
        subramas: arbol.reduce((s, r) => s + r.subramas.length, 0),
        materiales: materialesPlanoDedup.length,
        stockTotalTn: n(resumenStock.stockTotalTn),
        stockPlanTn: n(resumenStock.stockPlanTn),
        stockControlTn: n(resumenStock.stockControlTn),
        stockLimboTn: n(resumenStock.stockLimboTn),
        materialesPlan: resumenStock.materialesPlan,
        materialesControl: resumenStock.materialesControl,
        materialesLimbo: resumenStock.materialesLimbo
    },

    arbol,

    planos: {
        materiales: materialesPlanoDedup,
        asignaciones: asignacionesPlanoDedup
    },

    stockAuditoria: data.stockAuditoria || {
        fuente: "",
        info: { fechaStock: "", registros: 0, campos: [] },
        filas: []
    }
};

return msg;
