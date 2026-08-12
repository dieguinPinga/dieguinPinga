// Ensambla cavernicola/flow.json a partir de:
//  - build.js (metadata + nodos chicos, aca abajo)
//  - src/*.js y src/*.html (cuerpos grandes, en texto plano legible)
// El escapado a JSON lo hace JSON.stringify => siempre valido.
const fs = require('fs');
const path = require('path');
const SRC = path.join(__dirname, 'src');
const read = f => fs.readFileSync(path.join(SRC, f), 'utf8');

const func5905   = read('5905-criterio-simple.js');
const func35280  = read('35280-criterio-simple.js');
const funcA778   = read('a778-estructurar-arbol.js');
const tplB6a7    = read('b6a7-tablero.html');
const tpl1256    = read('1256-plan-sugerido.html');

const flow = [
  { id:"8b27e6e670a085da", type:"tab", label:"Cavernicola v2", disabled:false,
    info:"Tablero simplificado de compras: plan por familias y bandas criticas." },

  { id:"5905aa64c391a99e", type:"function", z:"8b27e6e670a085da", d:true,
    name:"Cavernicola v2 - criterio simple", func:func5905, outputs:2, noerr:0,
    initialize:"", finalize:"", libs:[], x:1210, y:720, wires:[[],[]] },

  { id:"ab79180e111b046c", type:"inject", z:"8b27e6e670a085da", name:"Cargar v2 al inicio",
    props:[{p:"payload"},{p:"accion",v:"cargarCriterio",vt:"str"}],
    repeat:"", crontab:"", once:true, onceDelay:"1", topic:"", payload:"",
    payloadType:"date", x:1570, y:560, wires:[["35280b2d61b594de"]] },

  { id:"b87c55151ce87869", type:"ui_button", z:"8b27e6e670a085da", name:"Recargar criterio v2",
    group:"cavernicola_v2_group", order:1, width:"4", height:"1", passthru:false,
    label:"Recargar criterio", tooltip:"Lee planComprasV2.json", color:"#e5e7eb",
    bgcolor:"#334155", className:"", icon:"refresh", payload:"", payloadType:"date",
    topic:"", topicType:"str", x:1570, y:610, wires:[["b73c6a0a3ccec1ec"]] },

  { id:"b73c6a0a3ccec1ec", type:"function", z:"8b27e6e670a085da", name:"accion cargarCriterio",
    func:"msg.accion = 'cargarCriterio';\nreturn msg;", outputs:1, noerr:0,
    initialize:"", finalize:"", libs:[], x:1800, y:610, wires:[["54ce3b2eb271fcfb"]] },

  { id:"54ce3b2eb271fcfb", type:"file in", z:"8b27e6e670a085da", name:"Leer planComprasV2.json",
    filename:"/home/gangleo81/planComprasV2.json", filenameType:"str", format:"utf8",
    chunk:false, sendError:false, encoding:"none", allProps:false, x:2060, y:590,
    wires:[["35280b2d61b594de"]] },

  { id:"fccf70aaabe26544", type:"ui_button", z:"8b27e6e670a085da", name:"Recargar reales",
    group:"cavernicola_v2_group", order:2, width:"4", height:"1", passthru:false,
    label:"Recargar reales", tooltip:"Lee comprasRealesIA.json", color:"#e5e7eb",
    bgcolor:"#334155", className:"", icon:"refresh", payload:"", payloadType:"date",
    topic:"", topicType:"str", x:1570, y:670, wires:[["faa7ffcdbbe822ac"]] },

  { id:"faa7ffcdbbe822ac", type:"function", z:"8b27e6e670a085da", name:"accion cargarReales",
    func:"msg.accion = 'cargarReales';\nreturn msg;", outputs:1, noerr:0,
    initialize:"", finalize:"", libs:[], x:1800, y:670, wires:[["be94ae13551df9c3"]] },

  { id:"be94ae13551df9c3", type:"file in", z:"8b27e6e670a085da", name:"Leer comprasRealesIA.json",
    filename:"/home/gangleo81/comprasRealesIA.json", filenameType:"str", format:"utf8",
    chunk:false, sendError:false, encoding:"none", allProps:false, x:2060, y:670,
    wires:[["35280b2d61b594de"]] },

  { id:"b55c3750a0c45f60", type:"ui_button", z:"8b27e6e670a085da", name:"Recargar stock",
    group:"cavernicola_v2_group", order:3, width:"4", height:"1", passthru:false,
    label:"Recargar stock", tooltip:"Lee stockIntermediosIA.json", color:"#e5e7eb",
    bgcolor:"#334155", className:"", icon:"refresh", payload:"", payloadType:"date",
    topic:"", topicType:"str", x:1570, y:730, wires:[["358995cf57e02aac"]] },

  { id:"358995cf57e02aac", type:"function", z:"8b27e6e670a085da", name:"accion cargarStock",
    func:"msg.accion = 'cargarStock';\nreturn msg;", outputs:1, noerr:0,
    initialize:"", finalize:"", libs:[], x:1800, y:730, wires:[["5cf138b2ff85f2a8"]] },

  { id:"5cf138b2ff85f2a8", type:"file in", z:"8b27e6e670a085da", name:"Leer stockIntermediosIA.json",
    filename:"/home/gangleo81/stockIntermediosIA.json", filenameType:"str", format:"utf8",
    chunk:false, sendError:false, encoding:"none", allProps:false, x:2060, y:730,
    wires:[["35280b2d61b594de"]] },

  { id:"207bc5145376c49d", type:"ui_button", z:"8b27e6e670a085da", name:"Recargar agenda",
    group:"cavernicola_v2_group", order:4, width:"4", height:"1", passthru:false,
    label:"Recargar agenda", tooltip:"Lee agendaComprasIA.json", color:"#e5e7eb",
    bgcolor:"#334155", className:"", icon:"refresh", payload:"", payloadType:"date",
    topic:"", topicType:"str", x:1570, y:790, wires:[["732f67c9841f145f"]] },

  { id:"732f67c9841f145f", type:"function", z:"8b27e6e670a085da", name:"accion cargarAgenda",
    func:"msg.accion = 'cargarAgenda';\nreturn msg;", outputs:1, noerr:0,
    initialize:"", finalize:"", libs:[], x:1800, y:790, wires:[["98260dc85e987b2a"]] },

  { id:"98260dc85e987b2a", type:"file in", z:"8b27e6e670a085da", name:"Leer agendaComprasIA.json",
    filename:"/home/gangleo81/agendaComprasIA.json", filenameType:"str", format:"utf8",
    chunk:false, sendError:false, encoding:"none", allProps:false, x:2060, y:790,
    wires:[["35280b2d61b594de"]] },

  { id:"35280b2d61b594de", type:"function", z:"8b27e6e670a085da", name:"Cavernicola v2 - criterio simple",
    func:func35280, outputs:2, noerr:0, initialize:"", finalize:"", libs:[], x:2360, y:690,
    wires:[["a77807b90f04b3ca"],["8a9413cde56ae487"]] },

  { id:"8a9413cde56ae487", type:"file", z:"8b27e6e670a085da", name:"Guardar planComprasV2.json",
    filename:"filename", filenameType:"msg", appendNewline:false, createDir:true,
    overwriteFile:"true", encoding:"utf8", x:2640, y:760, wires:[[]] },

  { id:"a77807b90f04b3ca", type:"function", z:"8b27e6e670a085da", name:"function 8",
    func:funcA778, outputs:1, timeout:0, noerr:0, initialize:"", finalize:"", libs:[],
    x:2680, y:980, wires:[["28407f6b2128045a","b6a7cce012889f34","1256065432905bef"]] },

  { id:"28407f6b2128045a", type:"debug", z:"8b27e6e670a085da", name:"debug 23", active:false,
    tosidebar:true, console:false, tostatus:false, complete:"false", statusVal:"",
    statusType:"auto", x:2800, y:880, wires:[] },

  { id:"b6a7cce012889f34", type:"ui_template", z:"8b27e6e670a085da", group:"cavernicola_v2_group",
    name:"aca mostramos", order:1, width:"42", height:"37", format:tplB6a7,
    storeOutMessages:false, fwdInMessages:false, resendOnRefresh:true, templateScope:"local",
    className:"", x:2850, y:1060, wires:[["35280b2d61b594de"]] },

  { id:"1256065432905bef", type:"ui_template", z:"8b27e6e670a085da", group:"cavernicola_v2_group",
    name:"plan sugerido 200 tn", order:2, width:"42", height:"14", format:tpl1256,
    storeOutMessages:false, fwdInMessages:false, resendOnRefresh:true, templateScope:"local",
    className:"", x:2850, y:1120, wires:[[]] },

  { id:"ecodp_watch_plan", type:"watch", z:"8b27e6e670a085da", d:true, name:"watch planComprasV2.json",
    files:"/home/gangleo81/planComprasV2.json", recursive:false, x:1240, y:1350,
    wires:[["ecodp_preparar_watch_plan"]] },

  { id:"ecodp_watch_reales", type:"watch", z:"8b27e6e670a085da", d:true, name:"watch comprasRealesIA.json",
    files:"/home/gangleo81/comprasRealesIA.json", recursive:false, x:1240, y:1400,
    wires:[["ecodp_preparar_watch_plan"]] },

  { id:"ecodp_watch_stock", type:"watch", z:"8b27e6e670a085da", d:true, name:"watch stockIntermediosIA.json",
    files:"/home/gangleo81/stockIntermediosIA.json", recursive:false, x:1240, y:1450,
    wires:[["ecodp_preparar_watch_plan"]] },

  { id:"ecodp_watch_agenda", type:"watch", z:"8b27e6e670a085da", d:true, name:"watch agendaComprasIA.json",
    files:"/home/gangleo81/agendaComprasIA.json", recursive:false, x:1240, y:1500,
    wires:[["ecodp_preparar_watch_plan"]] },

  { id:"ecodp_preparar_watch_plan", type:"function", z:"8b27e6e670a085da", d:true,
    name:"preparar lectura JSON modificado",
    func:"const filename = msg.filename || msg.payload || msg.file || msg.topic;\nif (!filename || !String(filename).endsWith('.json')) return null;\nmsg.filename = String(filename);\nmsg.topic = 'archivo_modificado';\nreturn msg;",
    outputs:1, x:1640, y:1415, wires:[["ecodp_delay_watch_plan"]] },

  { id:"ecodp_delay_watch_plan", type:"delay", z:"8b27e6e670a085da", d:true,
    name:"esperar escritura completa", pauseType:"delay", timeout:"1", timeoutUnits:"seconds",
    rate:"1", nbRateUnits:"1", rateUnits:"second", randomFirst:"1", randomLast:"5",
    randomUnits:"seconds", drop:false, allowrate:false, outputs:1, x:1900, y:1415,
    wires:[["ecodp_filein_watch_plan"]] },

  { id:"ecodp_filein_watch_plan", type:"file in", z:"8b27e6e670a085da", d:true,
    name:"leer JSON modificado", filename:"filename", filenameType:"msg", format:"utf8",
    chunk:false, sendError:true, encoding:"none", allProps:false, x:2130, y:1415,
    wires:[["ecodp_wrap_plan_minipc"]] },

  { id:"ecodp_post_plan_minipc", type:"http request", z:"8b27e6e670a085da", d:true,
    name:"POST JSON compras a miniPC", method:"use", ret:"obj", paytoqs:"ignore", url:"",
    tls:"", persist:false, proxy:"", authType:"", x:2620, y:1415,
    wires:[["ecodp_debug_plan_minipc"]] },

  { id:"ecodp_debug_plan_minipc", type:"debug", z:"8b27e6e670a085da", d:true,
    name:"respuesta miniPC watchdog compras", active:true, tosidebar:true, console:false,
    tostatus:true, complete:"payload", targetType:"msg", x:3070, y:1480, wires:[] },

  { id:"ecodp_watch_archivos_compras_v2", type:"watch", z:"8b27e6e670a085da",
    name:"watch carpeta JSON compras", files:"/home/gangleo81", recursive:false, x:1200, y:1920,
    wires:[["ecodp_preparar_json_compras_v2"]] },

  { id:"ecodp_delay_json_compras_v2", type:"delay", z:"8b27e6e670a085da",
    name:"esperar escritura completa", pauseType:"delay", timeout:"1", timeoutUnits:"seconds",
    rate:"1", nbRateUnits:"1", rateUnits:"second", randomFirst:"1", randomLast:"5",
    randomUnits:"seconds", drop:false, allowrate:false, outputs:1, x:1730, y:1920,
    wires:[["ecodp_filein_json_compras_v2"]] },

  { id:"ecodp_filein_json_compras_v2", type:"file in", z:"8b27e6e670a085da",
    name:"leer JSON compras", filename:"filename", filenameType:"msg", format:"utf8",
    chunk:false, sendError:true, encoding:"none", allProps:false, x:1960, y:1920,
    wires:[["ecodp_wrap_json_compras_v2"]] },

  { id:"ecodp_post_json_compras_v2", type:"http request", z:"8b27e6e670a085da",
    name:"POST JSON compras a miniPC", method:"use", ret:"obj", paytoqs:"ignore", url:"",
    tls:"", persist:false, proxy:"", authType:"", x:2490, y:1920,
    wires:[["ecodp_debug_json_compras_v2"]] },

  { id:"ecodp_debug_json_compras_v2", type:"debug", z:"8b27e6e670a085da",
    name:"respuesta miniPC watchdog compras v2", active:true, tosidebar:true, console:false,
    tostatus:true, complete:"payload", targetType:"msg", x:2790, y:1920, wires:[] },

  { id:"cavernicola_v2_group", type:"ui_group", name:"Compras v2", tab:"cavernicola_v2_ui_tab",
    order:1, disp:true, width:"42", collapse:false, className:"" },

  { id:"cavernicola_v2_ui_tab", type:"ui_tab", name:"Cavernicola v2", icon:"dashboard",
    order:1, disabled:false, hidden:false },

  { id:"ebcd0ee2a06f5ed8", type:"global-config", env:[],
    modules:{ "node-red-dashboard":"3.6.6" } }
];

// Inserta los dos nodos wrap (con cuerpo grande) leidos de src.
const wrapPlan = read('ecodp_wrap_plan_minipc.js');
const wrapJson = read('ecodp_wrap_json_compras_v2.js');
const prepJson = read('ecodp_preparar_json_compras_v2.js');

function insertAfter(id, node){
  const i = flow.findIndex(n => n.id === id);
  flow.splice(i+1, 0, node);
}
insertAfter("ecodp_filein_watch_plan", {
  id:"ecodp_wrap_plan_minipc", type:"function", z:"8b27e6e670a085da", d:true,
  name:"envolver + dedupe + POST", func:wrapPlan, outputs:1, x:2370, y:1415,
  wires:[["ecodp_post_plan_minipc"]] });
insertAfter("ecodp_watch_archivos_compras_v2", {
  id:"ecodp_preparar_json_compras_v2", type:"function", z:"8b27e6e670a085da",
  name:"filtrar JSON compras", func:prepJson, outputs:1, x:1480, y:1920,
  wires:[["ecodp_delay_json_compras_v2"]] });
insertAfter("ecodp_filein_json_compras_v2", {
  id:"ecodp_wrap_json_compras_v2", type:"function", z:"8b27e6e670a085da",
  name:"envolver + detectar tipo + POST", func:wrapJson, outputs:1, x:2220, y:1920,
  wires:[["ecodp_post_json_compras_v2"]] });

fs.writeFileSync(path.join(__dirname,'flow.json'), JSON.stringify(flow, null, 4));
console.log('flow.json escrito con', flow.length, 'nodos');
