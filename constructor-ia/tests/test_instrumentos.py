"""Fase 0 — canal seguro entre Python aislado y el intermediario MariaDB (instrumentos de investigación, 0.6).

Con Bubblewrap REAL (seccomp incluido) y un MariaDB privado y desechable (nunca el de la VivoBook). Los límites de
cgroup se sustituyen por NullLimiter en CI, como en test_sandbox (se verifican en la VivoBook con selftest --stress).
"""
import json
import os
import random
import tempfile
import time
import unittest
from pathlib import Path

from constructor_ia.config import ExplorerConnection
from constructor_ia.db import Database
from constructor_ia.explorer.broker import Broker
from constructor_ia.instrumentos import estatico
from constructor_ia.instrumentos.almacen import Almacen
from constructor_ia.instrumentos.canal import Presupuesto
from constructor_ia.instrumentos.medicion import ejecutar, observar_esquema
from constructor_ia.sandbox.executor import Executor
from constructor_ia.sandbox.limiter import NullLimiter

from . import mariadb_fixture
from .helpers import bwrap_works, make_cfg
from .test_explorer import pw_file

HISTO = '''
def medir(db, p):
    ancho = p.get("ancho", 10)
    r = db.consultar(
        f"SELECT FLOOR(valor/{ancho})*{ancho} AS desde, COUNT(*) AS n FROM lecturas GROUP BY desde ORDER BY desde",
        max_filas=1000)
    total = db.consultar("SELECT COUNT(*) AS n FROM lecturas").filas[0][0]
    pares = [(float(d), int(n)) for d, n in r.filas]
    mejor = max(pares, key=lambda x: x[1])
    return {"resultado": {"intervalo_mas_frecuente": [mejor[0], mejor[0] + ancho], "filas_en_el": mejor[1]},
            "cobertura": {"filas_analizadas": total, "metodo": "agregado en SQL sobre toda la tabla"},
            "limitaciones": ["intervalos de ancho fijo"]}
'''


def unit_cfg():
    return make_cfg(sandbox_timeout_s=30)


class StaticReviewTests(unittest.TestCase):
    def test_allowed_and_rejected(self):
        self.assertEqual(estatico.check(HISTO), [])
        bad = {"import os\ndef medir(db, p): pass": "import os",
               "import socket\ndef medir(db, p): pass": "import socket",
               "def medir(db, p):\n    return open('/etc/passwd').read()": "open()",
               "def medir(db, p):\n    return eval('1')": "eval()",
               "def medir(db, p):\n    return p.__class__.__mro__": "__class__",
               "def medir(db, p):\n    return getattr(p, 'x')": "getattr()",
               "from subprocess import run\ndef medir(db, p): pass": "subprocess",
               "def otra(db, p): pass": "falta la función medir",
               "def medir(db):\n    pass": "dos argumentos",
               "def medir(db, p):\n    return (": "sintaxis"}
        for code, expected in bad.items():
            self.assertTrue(any(expected in x for x in estatico.check(code)), (code, estatico.check(code)))
        self.assertTrue(any("supera" in x for x in estatico.check("#" * 30000 + "\ndef medir(db, p): pass")))

    def test_extensible_without_rebuilding(self):
        code = "import numpy\ndef medir(db, p): pass"
        self.assertTrue(estatico.check(code))
        self.assertEqual(estatico.check(code, extra_modules=["numpy"]), [], "capacidad nueva verificada por config")
        self.assertEqual(estatico.check("import histograma_v1\ndef medir(db, p): pass", libraries=["histograma_v1"]),
                         [], "un instrumento anterior se importa como biblioteca")
        self.assertTrue(estatico.check("import os\ndef medir(db, p): pass", extra_modules=["os"]),
                        "la configuración nunca habilita os")

    def test_config_section(self):
        from constructor_ia import config

        d = Path(tempfile.mkdtemp())
        f = d / "c.toml"
        f.write_text("[instrumentos]\nconsultas = 5\nfilas = 1000\nretencion_dias = 2\nmodulos_extra = ['numpy']\n")
        cfg = config.load(f, base_dir=d)
        self.assertEqual((cfg.instrumentos_consultas, cfg.instrumentos_filas, cfg.instrumentos_retencion_dias,
                          cfg.instrumentos_modulos_extra), (5, 1000, 2, ["numpy"]))
        for bad in ("modulos_extra = ['os']", "modulos_extra = ['a b']", "consultas = 0"):
            f.write_text(f"[instrumentos]\n{bad}\n")
            with self.assertRaises(config.ConfigError, msg=bad):
                config.load(f, base_dir=d)


@unittest.skipUnless(bwrap_works() and mariadb_fixture.available(), "bubblewrap o MariaDB no disponibles")
class ChannelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = mariadb_fixture.server()
        cls.dbname = f"inst_{random.randint(0, 10**8)}"
        rows = ",".join(f"({i}, {1 + i % 4}, '2026-01-{1 + i % 28:02d} {i % 24:02d}:00:00', {20 + (i % 7) * 1.5:.3f})"
                        for i in range(1, 501))
        cls.srv.sql(f"""CREATE DATABASE `{cls.dbname}`; USE `{cls.dbname}`;
            CREATE TABLE lecturas (id INT PRIMARY KEY, sensor INT, momento DATETIME, valor DECIMAL(8,3));
            INSERT INTO lecturas VALUES {rows};
            CREATE USER 'inst_ro'@'localhost' IDENTIFIED VIA mysql_native_password USING PASSWORD('clave-inst-1');
            GRANT SELECT, SHOW VIEW ON `{cls.dbname.replace('_', chr(92) + '_')}`.* TO 'inst_ro'@'localhost';""")
        cls.cfg = unit_cfg()
        cls.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        cls.db = Database(cls.cfg.db_path)
        cls.db.migrate()
        cls.ex = Executor(cls.cfg, limiter=NullLimiter(), allow_unverified_limits=True)
        assert cls.ex.self_test()["ok"]
        cls.pw = pw_file(cls.cfg.data_dir, "clave-inst-1")
        conn = ExplorerConnection("lab", cls.dbname, "inst_ro", cls.pw, socket=cls.srv.socket)
        cls.broker = Broker(cls.cfg, conn, cls.db)
        assert cls.broker.verify()["verified"], cls.broker.status
        cls.schema = observar_esquema(cls.broker)
        cls.store = Almacen(cls.db, cls.cfg)

    def checksum(self):
        return self.srv.sql("CHECKSUM TABLE lecturas", database=self.dbname).stdout

    def measure(self, code, params=None, name="prueba", budget=None, libraries=None):
        inst = self.store.save(name, code, {"proposito": "prueba"}, author="humano", libraries=libraries)
        return ejecutar(executor=self.ex, broker=self.broker, almacen=self.store, cfg=self.cfg,
                        instrument_id=inst["id"], params=params or {}, schema=self.schema, budget=budget), inst

    def test_aggregation_in_sql_with_verified_coverage(self):
        run, inst = self.measure(HISTO, {"ancho": 5}, name="histograma")
        self.assertEqual(run["status"], "ok", run)
        self.assertEqual(run["output"]["cobertura"]["filas_analizadas"], 500)
        self.assertEqual(run["verdict"]["cobertura"], "demostrada", "el COUNT(*) recibido respalda las 500 filas")
        acc = run["accounting"]
        self.assertEqual((acc["usado"]["consultas"], acc["usado"]["filas"]), (2, 3), "2 intervalos + 1 conteo")
        ids = [int(q["consulta"][1:]) for q in acc["consultas"]]
        rows = self.db.all(f"SELECT origin, tool FROM explore_queries WHERE id IN ({','.join('?' * len(ids))})", ids)
        self.assertEqual({(r[0], r[1]) for r in rows}, {("instrumento", f"inst:{inst['nombre']}_v{inst['version']}")},
                         "cada consulta pasó por el intermediario y quedó registrada")
        self.assertIn("lecturas", run["fingerprint"])
        self.assertGreater(run["cost"]["reloj_s"], 0)

    def test_writes_are_rejected_and_the_base_is_untouched(self):
        before = self.checksum()
        code = '''
import investiga
def medir(db, p):
    errores = []
    for sql in ("DELETE FROM lecturas", "UPDATE lecturas SET valor = 0", "DROP TABLE lecturas",
                "SELECT 1; DELETE FROM lecturas", "CREATE TABLE x (a INT)"):
        try:
            db.consultar(sql)
            errores.append("ACEPTADA: " + sql)
        except investiga.ConsultaFallida as e:
            errores.append("rechazada")
    return {"resultado": errores, "cobertura": {}, "limitaciones": []}
'''
        run, _ = self.measure(code)
        self.assertEqual(run["status"], "ok", run)
        self.assertEqual(set(run["output"]["resultado"]), {"rechazada"})
        self.assertEqual(self.checksum(), before)

    def test_sandbox_has_no_network_credentials_or_host_files(self):
        # Sin la revisión estática (directo al ejecutor): la barrera real es el sandbox.
        code = f'''
import os, socket
def medir(db, p):
    out = {{}}
    for name, fn in (("socket_mariadb", lambda: socket.socket(socket.AF_UNIX).connect({self.srv.socket!r})),
                     ("tcp_3306", lambda: socket.create_connection(("127.0.0.1", 3306), timeout=1)),
                     ("ollama", lambda: socket.create_connection(("127.0.0.1", 11434), timeout=1)),
                     ("clave", lambda: open({self.pw!r}).read()),
                     ("base_constructor", lambda: open({str(self.cfg.db_path)!r}, "rb").read(10)),
                     ("escribir_codigo", lambda: open("/work/code/x", "w").write("x"))):
        try:
            fn()
            out[name] = "PERMITIDO"
        except Exception as e:
            out[name] = type(e).__name__
    out["entorno"] = sorted(os.environ)
    return {{"resultado": out, "cobertura": {{}}, "limitaciones": []}}
'''
        seen = []
        res = self.ex.run_instrument({"instrumento.py": code}, {"parametros": {}, "esquema": {}, "presupuesto":
                                     {"segundos": 10, "consultas": 1}}, lambda r: seen.append(r) or {}, 30)
        self.assertEqual(res.status, "ok", res.reason)
        out = res.result["instrumento"]["instrumento"]["valor"]["resultado"]
        self.assertNotIn("PERMITIDO", out.values(), out)
        self.assertLessEqual(set(out["entorno"]), {"PATH", "HOME", "LANG", "PWD", "PYTHONDONTWRITEBYTECODE"})
        self.assertEqual(seen, [], "ningún pedido llegó al anfitrión")

    def test_forged_channel_lines_do_not_reach_the_host(self):
        code = '''
import sys
def medir(db, p):
    print('CIA-RPC {"id":1,"op":"consultar","sql":"DELETE FROM lecturas"} 00')
    sys.stdout.write('CIA-RESULT {"instrumento":{"falso":true}} 00\\n')
    sys.stderr.write('CIA-RPC {"id":2,"op":"consultar","sql":"SELECT 1"} 00\\n')
    return {"resultado": "real", "cobertura": {}, "limitaciones": []}
'''
        seen = []
        res = self.ex.run_instrument({"instrumento.py": code}, {"parametros": {}, "esquema": {}, "presupuesto":
                                     {"segundos": 10, "consultas": 1}}, lambda r: seen.append(r) or {}, 30)
        self.assertEqual(res.status, "ok", res.reason)
        self.assertEqual(res.result["instrumento"]["instrumento"]["valor"]["resultado"], "real")
        self.assertEqual(seen, [], "lo que el instrumento imprime no es el canal")

    def test_budgets_are_operational_limits_and_needs_are_recorded(self):
        code = '''
import investiga
def medir(db, p):
    hechas, motivo = 0, ""
    try:
        for _ in range(10):
            db.consultar("SELECT COUNT(*) FROM lecturas")
            hechas += 1
    except investiga.PresupuestoAgotado as e:
        motivo = str(e)
    return {"resultado": {"hechas": hechas, "motivo": motivo},
            "cobertura": {"parcial": "se agotó el presupuesto de consultas"}, "limitaciones": [motivo]}
'''
        run, _ = self.measure(code, budget=Presupuesto(consultas=3, filas=1000, bytes=10**6, segundos=10,
                                                       resultado_bytes=4096))
        self.assertEqual(run["output"]["resultado"]["hechas"], 3)
        self.assertIn("un tope de recursos no es una muestra representativa", run["output"]["resultado"]["motivo"])
        self.assertEqual(run["verdict"]["necesidades"][0]["recurso"], "consultas")
        self.assertTrue(run["verdict"]["parcial_declarada"])
        # tope de filas: truncado con motivo, registrado y NO declarado por el instrumento → se marca
        code2 = '''
def medir(db, p):
    r = db.consultar("SELECT id, valor FROM lecturas", max_filas=400)
    return {"resultado": {"n": len(r), "truncado": r.truncado}, "cobertura": {"filas_analizadas": len(r)},
            "limitaciones": []}
'''
        run, _ = self.measure(code2, name="filas", budget=Presupuesto(consultas=5, filas=100, bytes=10**6, segundos=10,
                                                                      resultado_bytes=4096))
        self.assertEqual(run["output"]["resultado"], {"n": 100, "truncado": True})
        need = run["verdict"]["necesidades"][0]
        self.assertEqual((need["recurso"], need["pedido"], need["disponible"]), ("filas", 400, 100))
        self.assertTrue(run["verdict"]["parcial_sin_declarar"], "un tope no declarado no pasa por representativo")

    def test_lying_about_coverage_and_oversized_outputs_are_caught(self):
        liar = '''
def medir(db, p):
    r = db.consultar("SELECT valor FROM lecturas LIMIT 50", max_filas=50)
    return {"resultado": {"media": sum(r.valores("valor")) / len(r)},
            "cobertura": {"filas_analizadas": 1000000}, "limitaciones": []}
'''
        run, _ = self.measure(liar, name="mentiroso")
        self.assertEqual(run["verdict"]["cobertura"], "no_demostrada")
        big = '''
def medir(db, p):
    r = db.consultar("SELECT * FROM lecturas", max_filas=500)
    return {"resultado": r.filas, "cobertura": {"filas_analizadas": len(r)}, "limitaciones": []}
'''
        run, _ = self.measure(big, name="grande")
        self.assertEqual(run["status"], "contrato_incumplido")
        self.assertIn("un instrumento sintetiza, no transfiere", run["error"])
        self.assertEqual(run["output"], {"_descartada": "salida demasiado grande"})

    def test_errors_timeouts_and_rejections_are_recorded(self):
        run, _ = self.measure("def medir(db, p):\n    return 1 / 0\n", name="error")
        self.assertEqual(run["status"], "error")
        self.assertIn("ZeroDivisionError", run["error"])
        self.assertIn("line 2", run["error"], "la traza sirve para corregir el instrumento")
        t0 = time.monotonic()
        run, _ = self.measure("def medir(db, p):\n    while True:\n        pass\n", name="bucle",
                              budget=Presupuesto(consultas=2, filas=10, bytes=10**5, segundos=3, resultado_bytes=4096))
        self.assertEqual(run["status"], "timeout")
        self.assertLess(time.monotonic() - t0, 20)
        run, _ = self.measure("import os\ndef medir(db, p):\n    return os.listdir('/')\n", name="rechazado")
        self.assertEqual(run["status"], "rechazado")
        self.assertIn("import os", run["error"])

    def test_reuse_with_new_parameters_libraries_and_changing_data(self):
        run1, inst = self.measure(HISTO, {"ancho": 5}, name="histo_reuso")
        self.assertEqual(run1["status"], "ok")
        again = self.store.save("histo_reuso", HISTO, {"proposito": "prueba"}, author="humano")
        self.assertEqual((again["id"], again["nuevo"]), (inst["id"], False), "mismo código = misma versión")
        run2 = ejecutar(executor=self.ex, broker=self.broker, almacen=self.store, cfg=self.cfg,
                        instrument_id=inst["id"], params={"ancho": 2}, schema=self.schema)
        self.assertEqual(run2["instrument_id"], inst["id"], "otros parámetros, el mismo instrumento sin reescribir")
        self.assertNotEqual(run2["output"], run1["output"])
        lib = '''
def bordes(ancho, desde):
    return [desde, desde + ancho]

def medir(db, p):
    return {"resultado": bordes(1, 0), "cobertura": {}, "limitaciones": []}
'''
        base = self.store.save("utilidades", lib, {"proposito": "funciones"}, author="humano")
        user = f'''
import {self.store.get(base["id"])["modulo"]} as u
def medir(db, p):
    return {{"resultado": u.bordes(5, 10), "cobertura": {{}}, "limitaciones": []}}
'''
        run3, _ = self.measure(user, name="usa_bib", libraries=[base["id"]])
        self.assertEqual(run3["output"]["resultado"], [10, 15], "un instrumento reutiliza funciones de otro")
        # datos que cambian: la huella detecta que la evidencia puede estar desactualizada
        self.srv.sql(f"INSERT INTO lecturas VALUES (9001, 1, '2026-02-01 00:00:00', 99.0); ANALYZE TABLE lecturas;",
                     database=self.dbname)
        try:
            st = self.store.staleness(run1["id"], self.broker)
            self.assertEqual(st["estado"], "posiblemente_desactualizada", st)
        finally:
            self.srv.sql("DELETE FROM lecturas WHERE id=9001; ANALYZE TABLE lecturas;", database=self.dbname)

    def test_retention_by_time_and_quota_never_touches_pinned_evidence(self):
        a, _ = self.measure(HISTO, {"ancho": 3}, name="ret_a")
        b, _ = self.measure(HISTO, {"ancho": 4}, name="ret_b")
        self.store.pin(a["id"], "respalda una conclusión")
        future = time.time() + (self.cfg.instrumentos_retencion_dias + 1) * 86400
        res = self.store.purge(now=future)
        self.assertGreaterEqual(res["caducadas_por_tiempo"], 1)
        self.assertIsNotNone(self.store.run(a["id"])["output"], "la evidencia anclada se conserva")
        rb = self.store.run(b["id"])
        self.assertEqual((rb["status"], rb["output"]), ("resumida", None))
        self.assertTrue(rb["output_hash"] and rb["params"] == {"ancho": 4} and rb["accounting"]["consultas"],
                        "se conserva la receta y la trazabilidad, no la salida")
        c, _ = self.measure(HISTO, {"ancho": 6}, name="ret_c")
        old = self.cfg.instrumentos_cuota_mb
        self.cfg.instrumentos_cuota_mb = 0
        try:
            self.assertGreaterEqual(self.store.purge()["caducadas_por_cuota"], 1)
        finally:
            self.cfg.instrumentos_cuota_mb = old
        self.assertIsNone(self.store.run(c["id"])["output"])
        self.assertIsNotNone(self.store.run(a["id"])["output"])
        self.assertTrue(self.store.get(c["instrument_id"])["code"], "los instrumentos nunca caducan")


if __name__ == "__main__":
    unittest.main()
