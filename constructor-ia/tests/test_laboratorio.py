"""Fase 1 — laboratorio experimental de instrumentos.

Estas pruebas validan el CIRCUITO del laboratorio (base sintética, verdad, sandbox, canal, puntaje, corrección,
reutilización, métricas e informe) con un Ollama SIMULADO que sigue guiones fijos. No miden la capacidad de ningún
modelo: eso se mide con los modelos reales en la VivoBook (laboratorio-instrumentos).
"""
import copy
import datetime
import json
import random
import re
import tempfile
import unittest
from pathlib import Path

from constructor_ia.laboratorio import corrida, informe, privado, sintetica as S, tareas as T
from constructor_ia.laboratorio.protocolo import Modelo, calibracion, parse_diseno, parse_json
from constructor_ia.ollama import OllamaClient
from constructor_ia.sandbox.executor import Executor
from constructor_ia.sandbox.limiter import NullLimiter

from . import mariadb_fixture
from .fake_ollama import FakeOllama
from .helpers import bwrap_works, make_cfg

ESCALA = {"dias": 32, "por_dia": 200}

INSTRUMENTOS = {
    "T1": ("concentracion", {"ancho": 2}, '''
def medir(db, p):
    a = int(p.get("ancho", 2))
    r = db.consultar("SELECT FLOOR(valor/%d)*%d AS b, COUNT(*) AS n FROM lecturas WHERE valor > 0.5 AND valor < 900 "
                     "GROUP BY b ORDER BY n DESC" % (a, a), max_filas=50)
    total = db.consultar("SELECT COUNT(*) FROM lecturas").filas[0][0]
    b, n = r.filas[0]
    return {"resultado": {"desde": float(b), "hasta": float(b) + a, "lecturas": int(n)},
            "cobertura": {"filas_analizadas": int(total), "metodo": "histograma en SQL"},
            "limitaciones": ["excluye 0 y valores mayores a 900"]}
'''),
    "T2": ("huecos", {}, '''
def medir(db, p):
    r = db.consultar("SELECT prev, momento, TIMESTAMPDIFF(SECOND, prev, momento) AS s FROM (SELECT momento, "
                     "LAG(momento) OVER (ORDER BY momento) AS prev FROM lecturas) x WHERE prev IS NOT NULL "
                     "ORDER BY s DESC LIMIT 3", max_filas=3)
    total = db.consultar("SELECT COUNT(*) FROM lecturas").filas[0][0]
    return {"resultado": {"huecos": [{"desde": str(a), "hasta": str(b), "segundos": int(s)} for a, b, s in r.filas]},
            "cobertura": {"filas_analizadas": int(total)}, "limitaciones": []}
'''),
    "T3": ("grupos", {}, '''
import statistics

def medir(db, p):
    r = db.consultar("SELECT e.zona, l.estacion_id, AVG(l.valor) AS m, COUNT(*) AS n FROM lecturas l JOIN estaciones e "
                     "ON e.id = l.estacion_id WHERE l.valor > 0.5 AND l.valor < 900 GROUP BY e.zona, l.estacion_id")
    total = db.consultar("SELECT COUNT(*) FROM lecturas").filas[0][0]
    filas = [(z, int(e), float(m)) for z, e, m, n in r.filas]
    g = statistics.mean(m for _, _, m in filas)
    altas = [(z, e) for z, e, m in filas if m > g + 10]
    zonas = sorted({z for z, _ in altas})
    return {"resultado": {"criterio": "zona", "grupo": ", ".join(zonas), "estaciones": sorted(e for _, e in altas)},
            "cobertura": {"filas_analizadas": int(total)}, "limitaciones": []}
'''),
    "T4": ("tardias", {"desde": "", "hasta": ""}, '''
def medir(db, p):
    w = []
    if p.get("desde"):
        w.append("momento >= '" + p["desde"] + "'")
    if p.get("hasta"):
        w.append("momento < '" + p["hasta"] + "'")
    where = (" WHERE " + " AND ".join(w)) if w else ""
    r = db.consultar("SELECT SUM(TIMESTAMPDIFF(SECOND, momento, recibido) > 3600) AS tarde, COUNT(*) AS n "
                     "FROM lecturas" + where)
    tarde, n = r.filas[0]
    return {"resultado": {"proporcion": float(tarde) / int(n), "tardias": int(float(tarde))},
            "cobertura": {"filas_analizadas": int(n), "ventana": [p.get("desde"), p.get("hasta")]},
            "limitaciones": []}
'''),
    "T5": ("anomalias", {}, '''
def medir(db, p):
    r = db.consultar("SELECT valor, COUNT(*) AS n FROM lecturas GROUP BY valor HAVING COUNT(*) >= 10 ORDER BY n DESC")
    total = db.consultar("SELECT COUNT(*) FROM lecturas").filas[0][0]
    return {"resultado": {"anomalias": [{"valor": float(v), "cantidad": int(n)} for v, n in r.filas]},
            "cobertura": {"filas_analizadas": int(total)}, "limitaciones": ["sólo valores repetidos 10 veces o más"]}
'''),
}


def _bloques(nombre, params, code):
    manifest = {"nombre": nombre, "proposito": f"medir {nombre}", "parametros": params, "plan": "agregar en SQL",
                "muestreo": "ninguno", "bibliotecas": []}
    return f"```json\n{json.dumps(manifest)}\n```\n```python\n{code.strip()}\n```"


def _user(body):
    return body["messages"][-1]["content"]


def _tarea(text):
    return re.search(r"Tarea (\w+):", text).group(1)


def _salida(text):
    m = re.search(r"SALIDA:\n```json\n(.*?)\n```", text, re.S)
    return json.loads(m.group(1)) if m else {}


def competente(body):
    """Guion de un modelo que hace lo correcto (con un error en T2 que corrige)."""
    u = _user(body)
    t = _tarea(u)
    if '"responde"' in u:  # evaluación
        out = _salida(u)
        res = out.get("resultado") or {}
        if "minimo" in res:
            return json.dumps({"responde": "no", "motivo": "un resumen global no distingue grupos",
                               "respuesta": {}, "mejora": "agrupar por estación y por zona"})
        return json.dumps({"responde": "si", "motivo": "la medición cubre toda la tabla", "respuesta": res,
                           "mejora": ""})
    if "No escribas código nuevo" in u:  # reutilización
        idx = json.loads(re.search(r"Instrumentos existentes: (\[.*\])\n", u).group(1))
        inst = next(i for i in idx if i["modulo"].startswith("tardias"))
        d, h = re.search(r"entre (\S+) \(incluido\) y (\S+) \(excluido\)", u).groups()
        return json.dumps({"instrumento_id": inst["id"], "parametros": {"desde": d, "hasta": h}, "motivo": "mismo"})
    tid = "T3" if t == "C" else t
    nombre, params, code = INSTRUMENTOS[tid]
    if t == "T2" and "Instrumento actual" not in u:
        return _bloques(nombre, params, "def medir(db, p):\n    return 1 / 0\n")
    return _bloques(nombre, params, code)


def incompetente(body):
    """Guion de un modelo que escribe código prohibido, confía de más y no reutiliza."""
    u = _user(body)
    if '"responde"' in u:
        return json.dumps({"responde": "si", "motivo": "listo", "respuesta": {"grupo": "norte", "estaciones": [1]},
                           "mejora": ""})
    if "No escribas código nuevo" in u:
        return json.dumps({"instrumento_id": 999, "parametros": {}})
    return _bloques("malo", {}, "import os\ndef medir(db, p):\n    return os.listdir('/')\n")


class UnitTests(unittest.TestCase):
    def test_parsing(self):
        m, code, problems = parse_diseno("<think>x</think>" + _bloques("a", {"k": 1}, "def medir(db, p):\n    pass"))
        self.assertEqual((m["nombre"], m["parametros"], problems), ("a", {"k": 1}, []))
        self.assertIn("def medir", code)
        _, code, problems = parse_diseno("sin bloques")
        self.assertIsNone(code)
        self.assertEqual(len(problems), 2)
        self.assertEqual(parse_json('texto {"responde": "si"} más'), {"responde": "si"})
        self.assertIsNone(parse_json("nada"))

    def test_scorers_against_known_truth(self):
        t1 = {"intervalos_aceptables": [[18.0, 20.0], [20.0, 22.0]]}
        self.assertEqual(T.TAREAS["T1"].puntuar({"desde": 18, "hasta": 20}, t1)[0], 1.0)
        self.assertEqual(T.TAREAS["T1"].puntuar({"desde": 10, "hasta": 22}, t1)[0], 0.5)
        self.assertEqual(T.TAREAS["T1"].puntuar({"desde": 30, "hasta": 40}, t1)[0], 0.0)
        t2 = {"desde": "2026-01-31 01:58:00", "hasta": "2026-01-31 09:03:00", "segundos": 25500}
        self.assertEqual(T.TAREAS["T2"].puntuar({"huecos": [{"desde": "2026-01-31 02:00", "hasta": "2026-01-31 09:00"}]},
                                                t2)[0], 1.0)
        self.assertEqual(T.TAREAS["T2"].puntuar({"huecos": [{"desde": "2026-01-30", "hasta": "2026-02-02"}]}, t2)[0], 0.5)
        t3 = {"estaciones": [3, 8, 13, 18], "zonas": ["alta"]}
        self.assertEqual(T.TAREAS["T3"].puntuar({"grupo": "zona ALTA"}, t3)[0], 1.0)
        self.assertEqual(T.TAREAS["T3"].puntuar({"estaciones": [3, 8]}, t3)[0], 0.5)
        self.assertEqual(T.TAREAS["T4"].puntuar({"proporcion": 2.76}, {"proporcion": 0.0276})[0], 1.0, "porcentaje")
        self.assertEqual(T.TAREAS["T4"].puntuar({"proporcion": 0.05}, {"proporcion": 0.0276})[0], 0.0)
        t5 = {"anomalias": {"999.9": 40, "0.0": 100}}
        self.assertEqual(T.TAREAS["T5"].puntuar({"anomalias": [{"valor": 999.9, "cantidad": 40}]}, t5)[0], 0.5)
        self.assertEqual(T.TAREAS["T5"].puntuar({"anomalias": [{"valor": 999.9, "cantidad": 40},
                                                               {"valor": 0, "cantidad": 97}]}, t5)[0], 1.0)
        self.assertEqual(T.TAREAS["T5"].puntuar("texto", t5)[0], 0.0)
        self.assertEqual(T.TAREAS["T1"].puntuar({"desde": "x"}, t1)[0], 0.0)

    def test_calibration_and_selection(self):
        self.assertEqual(calibracion("si", 1.0), "calibrada")
        self.assertEqual(calibracion("si", 0.5), "sobreconfianza")
        self.assertEqual(calibracion("no", 1.0), "subconfianza")
        self.assertEqual(calibracion("parcial", 0), "calibrada")
        self.assertIsNone(calibracion(None, 1))
        self.assertEqual([t.id for t in T.seleccionar(["r", "t1"])], ["T1", "T4", "R"], "R necesita el instrumento de T4")
        with self.assertRaises(ValueError):
            T.seleccionar(["T9"])


@unittest.skipUnless(mariadb_fixture.available(), "MariaDB no disponible")
class SyntheticTruthTests(unittest.TestCase):
    def test_sown_phenomena_are_present_and_data_changes(self):
        srv = mariadb_fixture.server()
        base = f"sint_{random.randint(0, 10**8)}"
        info = S.generar(srv, base, **ESCALA)
        try:
            q = lambda sql: S.consultar_verdad(srv, base, sql)  # noqa: E731
            self.assertEqual(q("SELECT COUNT(*) FROM lecturas WHERE valor = 999.9"), [["40"]])
            self.assertEqual(q("SELECT COUNT(*) FROM lecturas WHERE momento >= '2026-01-31 02:00' AND "
                               "momento < '2026-01-31 09:00'"), [["0"]], "hueco global sembrado")
            v2 = T.TAREAS["T2"].verdad(srv, base, info)
            self.assertTrue(v2["desde"].startswith("2026-01-31 0") and v2["segundos"] >= 7 * 3600, v2)
            self.assertEqual(T.TAREAS["T3"].verdad(srv, base, info)["estaciones"], list(S.ALTA), "relación oculta")
            stuck = q("SELECT COUNT(DISTINCT estacion_id), MIN(estacion_id) FROM lecturas WHERE valor = 0")
            self.assertEqual(stuck, [["1", str(S.TRABADA)]])
            late = q("SELECT estacion_id FROM lecturas GROUP BY estacion_id ORDER BY "
                     "AVG(TIMESTAMPDIFF(SECOND, momento, recibido) > 3600) DESC LIMIT 1")
            self.assertEqual(late, [[str(S.TARDIA_ESTACION)]])
            self.assertNotIn("clave", json.dumps(q("SELECT USER()")))
            antes = T.TAREAS["T4"].verdad(srv, base, info)
            info2 = S.avanzar(srv, info)
            self.assertEqual(info2["dias"], info["dias"] + 1)
            cierre = (S.BASE + datetime.timedelta(days=info["dias"])).strftime("%Y-%m-%d")
            tard = q(f"SELECT COUNT(*) FROM lecturas WHERE id > {info['ultimo_id']} AND momento < '{cierre}'")
            self.assertGreater(int(tard[0][0]), 0, "registros tardíos dentro de días ya cerrados")
            self.assertNotEqual(T.TAREAS["T4"].verdad(srv, base, info2)["filas"], antes["filas"])
        finally:
            srv.sql(f"DROP DATABASE IF EXISTS `{base}`")


@unittest.skipUnless(not privado.disponible(), "MariaDB no disponible")
class PrivateServerTests(unittest.TestCase):
    def test_private_server_has_no_network_and_is_removed(self):
        d = Path(tempfile.mkdtemp(prefix="cia-priv-")) / "mariadb"
        with privado.ServidorPrivado(d, buffer_mb=32) as srv:
            self.assertEqual(srv.sql("SELECT @@skip_networking, @@datadir").stdout.split("\t")[0], "1")
            self.assertIn(str(d), srv.sql("SELECT @@datadir").stdout)
            self.assertTrue(srv.socket.startswith(str(d)))
        self.assertFalse(d.exists(), "datadir desechado")
        d.mkdir(parents=True)
        (d / "algo").write_text("x")
        with self.assertRaises(privado.ServidorNoDisponible):
            privado.ServidorPrivado(d)
        self.assertTrue((d / "algo").exists(), "no borra un directorio ajeno")


@unittest.skipUnless(bwrap_works() and mariadb_fixture.available(), "bubblewrap o MariaDB no disponibles")
class LabPipelineTests(unittest.TestCase):
    """Circuito completo con guiones fijos: NO es una medición de la capacidad de un modelo."""

    @classmethod
    def setUpClass(cls):
        cls.srv = mariadb_fixture.server()
        cls.cfg = make_cfg(sandbox_timeout_s=30)
        cls.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        cls.ex = Executor(cls.cfg, limiter=NullLimiter(), allow_unverified_limits=True)
        assert cls.ex.self_test()["ok"]
        cls.fakes = {"bueno": FakeOllama(), "malo": FakeOllama()}
        cls.fakes["bueno"].default, cls.fakes["malo"].default = competente, incompetente
        mk = lambda key, name: Modelo(OllamaClient(cls.fakes[key].url), cls.cfg.explorer_model, name,  # noqa: E731
                                      num_ctx=8192, temperatura=0.2, timeout_s=60)
        cls.salida = Path(tempfile.mkdtemp(prefix="cia-lab-"))
        cls.logs = []
        cls.res = corrida.correr(cls.cfg, executor=cls.ex, servidor=cls.srv,
                                 modelos=[mk("bueno", "modelo-a"), mk("malo", "modelo-b")],
                                 tareas=T.seleccionar(list(T.ORDEN)), escala=ESCALA, salida=cls.salida,
                                 max_correcciones=2, log=cls.logs.append)
        cls.json_path, cls.md_path = informe.escribir(cls.res, cls.salida)

    @classmethod
    def tearDownClass(cls):
        for f in cls.fakes.values():
            f.close()

    def sesion(self, modelo):
        return next(s for s in self.res["sesiones"] if s["modelo"] == modelo)

    def test_competent_script_solves_every_task_through_the_real_pipeline(self):
        self.assertEqual(self.res["estado"], "completa", self.logs)
        s = self.sesion("modelo-a")
        por = {r["tarea"]: r for r in s["registros"]}
        self.assertEqual({t: r["puntaje"] for t, r in por.items()}, {t: 1.0 for t in T.ORDEN},
                         {t: r["detalle_puntaje"] for t, r in por.items()})
        for r in s["registros"]:
            for v in r["versiones"]:
                if v["estado"] == "ok":
                    self.assertEqual(v["cobertura"], "demostrada", (r["tarea"], v))
        self.assertEqual([v["estado"] for v in por["T2"]["versiones"]], ["error", "ok"], "corrigió tras el error")
        self.assertEqual(por["T2"]["versiones"][1]["version"], 2, "la corrección es una versión nueva")
        self.assertTrue(por["C"]["detecto_insuficiencia"])
        self.assertEqual(por["C"]["versiones"][0]["modulo"], "resumen_lecturas_v1")
        self.assertEqual(por["C"]["versiones"][1]["modulo"], "resumen_lecturas_v2")
        self.assertTrue(por["R"]["reutilizo"])
        self.assertEqual(por["R"]["versiones"][0]["instrumento_id"], por["T4"]["versiones"][-1]["instrumento_id"],
                         "mismo instrumento, sólo parámetros nuevos")
        self.assertEqual(por["R"]["versiones"][0]["parametros"]["hasta"], por["R"]["verdad"]["ventana"][1])
        self.assertNotEqual(por["R"]["verdad"]["proporcion"], por["T4"]["verdad"]["proporcion"],
                            "la verdad de R se mide sobre los datos nuevos")
        m = self.res["resumen_por_modelo"]["modelo-a"]
        self.assertEqual((m["resueltas"], m["autoevaluacion_calibrada"], m["correccion_exitosa"]), (7, 1.0, 1.0))

    def test_incompetent_script_is_measured_not_hidden(self):
        s = self.sesion("modelo-b")
        m = s["resumen"]
        self.assertLess(m["puntaje_medio"], 0.2)
        self.assertGreater(m["rechazadas_estatico"], 0, "el código prohibido no se ejecutó")
        self.assertGreaterEqual(m["sobreconfianza"], 1, "dijo «sí» con una respuesta equivocada")
        self.assertFalse(m["reutilizo"])
        self.assertFalse(m["detecto_insuficiencia"])
        for r in s["registros"]:
            self.assertLessEqual(r["correcciones"], 2, "máximo de correcciones respetado")

    def test_same_conditions_for_every_model(self):
        opts = [{k: v for k, v in o.items() if k != "modelo"} for o in self.res["modelos"]]
        self.assertEqual(opts[0], opts[1])
        sent = {(b["options"]["num_ctx"], b["options"]["temperature"]) for f in self.fakes.values()
                for b in f.requests}
        self.assertEqual(sent, {(8192, 0.2)})
        a, b = (self.sesion(x)["base"] for x in ("modelo-a", "modelo-b"))
        self.assertEqual((a["semilla"], a["lecturas"]), (b["semilla"], b["lecturas"]), "base idéntica")
        sys_a = {b["messages"][0]["content"] for b in self.fakes["bueno"].requests}
        sys_b = {b["messages"][0]["content"] for b in self.fakes["malo"].requests}
        self.assertEqual(sys_a, sys_b)

    def test_report_comparison_and_no_secrets(self):
        md = self.md_path.read_text()
        self.assertIn("modelo-a", md)
        self.assertIn("Puntaje por tarea", md)
        raw = self.json_path.read_text()
        self.assertNotIn('"clave"', raw)
        self.assertFalse(list(self.salida.rglob("clave-desechable")), "la clave desechable se borra")
        self.assertEqual(self.srv.sql("SHOW DATABASES LIKE 'lab\\_%'").stdout.strip(), "", "bases desechadas")
        res = json.loads(raw)
        rev = informe.revisar(self.salida)
        self.assertIn("llamada diseno-T1: completa", rev)
        self.assertIn("--- instrumento concentracion_v1", rev)
        self.assertIn("inst:concentracion_v1 [ok", rev)
        self.assertIn("Condiciones idénticas", informe.comparar(res, res))
        otra = copy.deepcopy(res)
        otra["modelos"][0]["temperatura"] = 0.7
        self.assertIn("NO son iguales", informe.comparar(res, otra))


if __name__ == "__main__":
    unittest.main()
