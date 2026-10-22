"""E0 del próximo experimento de elección (PILOTO.md, «E0») y corrección de la traza cuando el instrumento se cae.

Las reglas son genéricas; las pruebas usan instrumentos que NO son del banco. Validan el circuito, no la capacidad
de un modelo (E0 no usa modelo).
"""
import ast
import tempfile
import time
import unittest
from pathlib import Path

from constructor_ia.instrumentos.medicion import ejecutar
import json

from constructor_ia.laboratorio.piloto import banco as B
from constructor_ia.laboratorio.piloto import defectos as DF
from constructor_ia.laboratorio.piloto import e0 as E0
from constructor_ia.laboratorio.piloto import parches as P
from constructor_ia.laboratorio.piloto import traza as T
from constructor_ia.laboratorio.piloto.entorno import Entorno
from constructor_ia.sandbox.executor import Executor
from constructor_ia.sandbox.limiter import NullLimiter

from . import mariadb_fixture
from .helpers import bwrap_works, make_cfg

# Agregación con clave, desconocida para el banco: cuenta lecturas por estación.
AGREGA = '''def medir(db, p):
    r = db.consultar("SELECT estacion_id, valor FROM lecturas", max_filas=50000)
    conteo = {}
    total = 0
    for est, valor in r.filas:
        x = float(valor)
        clave = int(est) * 10
        conteo[clave] = conteo.get(clave, 0) + x
        total += 1
    return {"resultado": {"por_estacion": conteo}, "cobertura": {"filas_analizadas": total}, "limitaciones": []}
'''

# Se cae a mitad del bucle; antes atrapa una excepción propia (eso no corta la traza).
SE_CAE = '''def medir(db, p):
    r = db.consultar("SELECT valor FROM lecturas", max_filas=50000)
    total = 0
    try:
        int("x")
    except ValueError:
        pass
    for i, (valor,) in enumerate(r.filas):
        total += 1
        if i == 5:
            y = [1][3]
    return {"resultado": {"total": total}, "cobertura": {"filas_analizadas": total}, "limitaciones": []}
'''


def linea(codigo, texto):
    return next(i for i, t in enumerate(codigo.splitlines(), 1) if t.strip() == texto)


def carpeta_defectos(items):
    d = Path(tempfile.mkdtemp(prefix="cia-defectos-"))
    manifiesto = []
    for did, codigo, lineas, clase in items:
        (d / f"{did}.py").write_text(codigo)
        manifiesto.append({"id": did, "archivo": f"{did}.py", "lineas": lineas, "clase": clase,
                           "descripcion": f"defecto {did}"})
    (d / "manifiesto.json").write_text(json.dumps(manifiesto))
    return d


def sobrescribe_sumas():
    """Un defecto que ninguna regla conoce de antemano: borra las sumas después del bucle."""
    lineas = B.REFERENCIA.splitlines()
    i = next(n for n, t in enumerate(lineas) if t.strip().startswith("mejores ="))
    codigo = "\n".join(lineas[:i] + ["    sumas = {k: 0.0 for k in sumas}"] + lineas[i:]) + "\n"
    return codigo, [i + 1, i + 1]


class UnitTests(unittest.TestCase):
    def test_external_defects_are_validated_not_interpreted(self):
        codigo, lineas = sobrescribe_sumas()
        d = carpeta_defectos([("X01", codigo, lineas, "sobrescritura"), ("M01", codigo, lineas, "cálculo"),
                              ("X02", "def otra():\n    pass\n", [1, 1], "cálculo"),
                              ("X03", codigo, [0, 2], "cálculo"), ("X04", codigo, lineas, "inventada")])
        ok, problemas = DF.cargar(d)
        self.assertEqual([x["id"] for x in ok], ["X01", "X04"])
        self.assertEqual(ok[1]["clase"], "otra", "una clase desconocida se informa como «otra»")
        self.assertEqual(len(problemas), 3)
        self.assertTrue(any("M01" in p for p in problemas), "no se aceptan ids del banco")

    def test_rules_fingerprint_is_stable(self):
        self.assertRegex(E0.huella_reglas(), r"^[0-9a-f]{16}$")
        self.assertEqual(E0.huella_reglas(), E0.huella_reglas())

    def test_interventions_are_generic_ast_transforms(self):
        ln = linea(AGREGA, "conteo[clave] = conteo.get(clave, 0) + x")
        self.assertIn("conteo[clave] = conteo.get(clave, 0) + 0.5 * x", E0.escalar(AGREGA, ln, 0.5))
        self.assertIn("total += -1 * 1", E0.escalar(AGREGA, linea(AGREGA, "total += 1"), -1))
        self.assertIsNone(E0.escalar(AGREGA, linea(AGREGA, "clave = int(est) * 10"), 2), "no acumula")
        sin = E0.omitir(AGREGA, linea(AGREGA, "total += 1"))
        self.assertNotIn("total += 1", sin)
        ast.parse(sin)

    def test_static_paths_of_a_keyed_aggregation(self):
        (c,) = E0.caminos(AGREGA)
        self.assertEqual(c["linea"], linea(AGREGA, "conteo[clave] = conteo.get(clave, 0) + x"))
        self.assertIn("clave", c["clave"])
        self.assertIn("est", c["clave"])
        self.assertIn("x", c["valor"])
        self.assertNotIn("clave", c["valor"])

    def test_contrast_reader_picks_the_key_or_the_value_path(self):
        cand = [linea(AGREGA, t) for t in ("clave = int(est) * 10", "x = float(valor)", "total += 1")]
        caso = {"codigo": AGREGA, "candidatas": cand}
        clave = {"contraejemplos": [{"campo": "ventana", "inicio_alineado": True, "la_suma_es_de": "otra"}] * 2}
        valor = {"contraejemplos": [{"campo": "ventana", "inicio_alineado": True, "la_suma_es_de": None,
                                     "signo_opuesto": True}]}
        self.assertEqual(E0.r4_contraste(caso, clave), ([cand[0]], "clave"))
        self.assertEqual(E0.r4_contraste(caso, valor), ([cand[1]], "valor"))
        self.assertEqual(E0.r4_contraste(caso, {"contraejemplos": [{"campo": "leidas"}]}), ([], None),
                         "sin pares con clave, se abstiene")

    def test_inert_accumulation_reader(self):
        ln = linea(AGREGA, "total += 1")
        caso = {"codigo": AGREGA, "candidatas": [ln], "traza": {"lineas": {str(ln): {
            "veces": 900, "muestras": [{"antes": {"total": "3"}, "despues": {"total": "3"}}],
            "ultima": {"antes": {"total": "3"}, "despues": {"total": "3"}}}}}}
        self.assertEqual(E0.r3_sin_efecto(caso), [ln])
        caso["traza"]["lineas"][str(ln)]["ultima"]["despues"] = {"total": "4"}
        self.assertEqual(E0.r3_sin_efecto(caso), [])

    def test_shape_ignores_values_but_not_emptiness(self):
        a = {"resultado": {"v": [{"x": 1}, {"x": 2}]}, "c": 3}
        self.assertEqual(E0.forma(a), E0.forma({"resultado": {"v": [{"x": 9}, {"x": 8}]}, "c": 0}))
        self.assertNotEqual(E0.forma(a), E0.forma({"resultado": {"v": []}, "c": 3}))


@unittest.skipUnless(bwrap_works() and mariadb_fixture.available(), "bubblewrap o MariaDB no disponibles")
class SandboxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = make_cfg(sandbox_timeout_s=60)
        cls.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        cls.ex = Executor(cls.cfg, limiter=NullLimiter(), allow_unverified_limits=True)
        assert cls.ex.self_test()["ok"]
        cls.dir = Path(tempfile.mkdtemp(prefix="cia-e0-"))
        cls.ent = Entorno(cls.cfg, cls.ex, mariadb_fixture.server(), cls.dir / "ent", base="e0_t",
                          escala={"dias": 3, "por_dia": 300})
        cls.alm = cls.ent.almacen("t")

    @classmethod
    def tearDownClass(cls):
        cls.ent.cerrar()

    def test_partial_trace_survives_a_crash(self):
        lineas = [linea(SE_CAE, t) for t in ("total += 1", "y = [1][3]", 'int("x")')]
        us = [P.unidad_en(SE_CAE, ln) for ln in lineas]
        iid = self.alm.save(f"x{time.time_ns()}", SE_CAE, {"proposito": "prueba"}, author="prueba")["id"]
        run = ejecutar(executor=self.ex, broker=self.alm.broker, almacen=self.alm, cfg=self.cfg, instrument_id=iid,
                       params={}, schema=self.ent.schema, traza=T.pedido(us, SE_CAE))
        self.assertEqual(run["status"], "error")
        tz = run["verdict"]["traza"]
        self.assertIsNotNone(tz, "antes de 0.7-e0 la traza se perdía y la plantilla decía «0 veces»")
        self.assertFalse(tz["completa"])
        d = tz["lineas"]
        self.assertEqual(d[str(lineas[0])]["veces"], 6)
        self.assertEqual((d[str(lineas[1])]["veces"], d[str(lineas[1])]["interrumpida"]), (1, 1))
        self.assertTrue(d[str(lineas[1])]["ultima"]["interrumpida"])
        self.assertEqual(d[str(lineas[2])]["interrumpida"], 1, "la excepción atrapada también la cortó")
        self.assertEqual(d[str(lineas[0])]["interrumpida"], 0)
        self.assertEqual(run["verdict"]["falla"]["lugar"]["linea"], lineas[1])

    def test_trace_of_a_successful_run_is_complete(self):
        ln = linea(AGREGA, "total += 1")
        us = [P.unidad_en(AGREGA, ln)]
        iid = self.alm.save(f"x{time.time_ns()}", AGREGA, {"proposito": "prueba"}, author="prueba")["id"]
        run = ejecutar(executor=self.ex, broker=self.alm.broker, almacen=self.alm, cfg=self.cfg, instrument_id=iid,
                       params={}, schema=self.ent.schema, traza=T.pedido(us, AGREGA))
        self.assertEqual(run["status"], "ok")
        self.assertTrue(run["verdict"]["traza"]["completa"])
        self.assertEqual(run["verdict"]["traza"]["lineas"][str(ln)]["interrumpida"], 0)

    def test_e0_on_the_observed_defects(self):
        res = E0.correr(self.ent, incluir_mutantes=False, log=lambda *_: None)
        por = {f["id"]: f for f in res["casos"]}
        d1, d2 = por["D1"], por["D2"]
        self.assertEqual((d1["combinada"]["lectura"], d1["combinada"]["acierta"]),
                         ("R4_contraste_clave_valor", True))
        self.assertEqual(d1["camino_contraste"], "clave", "la suma informada es la de la ventana siguiente")
        self.assertEqual(d1["lecturas"]["R5_omision"]["cumplen"], [],
                         "omitir la acumulación vacía la salida: no cuenta como reparación")
        self.assertGreater(res["rechazadas_por_forma"], 0)
        self.assertEqual((d2["combinada"]["lectura"], d2["combinada"]["acierta"]), ("R2_ultimo_escritor", True))
        self.assertEqual(d2["lecturas"]["R5_omision"]["cumplen"], d2["sembrada"])
        self.assertTrue(res["python_sandbox"][0].isdigit())
        self.assertEqual(set(d1["huellas"]), {"codigo", "salida", "texto_fallas", "traza"})
        self.assertIn("| total |", E0.markdown(res))

    def test_blind_evaluation_reports_coverage_and_origin(self):
        codigo, lineas = sobrescribe_sumas()
        equivalente = B.REFERENCIA.replace("mejores", "elegidas")
        externos, problemas = DF.cargar(carpeta_defectos([("X01", codigo, lineas, "sobrescritura"),
                                                          ("X02", equivalente, [1, 1], "otra")]))
        self.assertEqual(problemas, [])
        res = E0.correr(self.ent, externos=externos, log=lambda *_: None)
        self.assertEqual(res["cobertura"]["casos"], 2, "solo los externos: ni D1, D2 ni mutantes")
        self.assertEqual(res["cobertura"]["equivalentes"], ["X02"])
        self.assertEqual(res["huella_reglas"], E0.huella_reglas())
        for f in res["casos"]:
            self.assertEqual((f["origen"], f["clase"]), ("externo", "sobrescritura"))
        md = E0.markdown(res)
        self.assertIn("Huella de las reglas", md)
        self.assertIn("NO quedó entre las 3 candidatas", md)


if __name__ == "__main__":
    unittest.main()
