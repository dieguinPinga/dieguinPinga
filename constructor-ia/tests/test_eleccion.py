"""Experimento de elección (PILOTO.md, «Experimento de elección»): traza genérica, Etapa 0 y Etapa 1.

Los modelos simulados validan el CIRCUITO (una sola diferencia entre condiciones, semillas pareadas, contrabalanceo,
límite, reanudación, auditoría y análisis), NO la capacidad de un modelo.
"""
import json
import re
import tempfile
import time
import types
import unittest
from pathlib import Path

from constructor_ia.instrumentos.medicion import ejecutar
from constructor_ia.laboratorio.piloto import eleccion as E
from constructor_ia.laboratorio.piloto import parches as P
from constructor_ia.laboratorio.piloto import traza as T
from constructor_ia.laboratorio.piloto.entorno import Entorno
from constructor_ia.sandbox.executor import Executor
from constructor_ia.sandbox.limiter import NullLimiter

from . import mariadb_fixture
from .helpers import bwrap_works, make_cfg

# Un instrumento que NO es del banco: la traza tiene que servir para código desconocido.
DESCONOCIDO = '''
def medir(db, p):
    r = db.consultar("SELECT estacion_id, valor FROM lecturas", max_filas=50000)
    total = 0
    maximos = {}
    for e, v in r.filas:
        x = float(v)
        total += 1
        if x > maximos.get(e, -1.0):
            maximos[e] = x
    nunca = 0
    if total < 0:
        nunca = 1
    if total > 0:
        total = 0
    pares = [k for k in maximos if k % 2 == 0]
    return {"resultado": {"estaciones": len(maximos), "pares": len(pares), "nunca": nunca},
            "cobertura": {"filas_analizadas": total}, "limitaciones": []}
'''


class Modelo:
    """Modelo simulado: decide con una función del prompt; registra semillas y prompts."""

    def __init__(self, decidir, fallar_en=()):
        self.decidir, self.fallar_en = decidir, set(fallar_en)
        self.nombre, self.system, self.base = "simulado", "", types.SimpleNamespace(num_ctx=8192)
        self.llamadas = []

    def pedir(self, tipo, prompt, *, json_out, cancel=None, corto=False, seed=None):
        self.llamadas.append({"seed": seed, "prompt": prompt})
        if len(self.llamadas) in self.fallar_en:
            return None, {"tipo": tipo, "resultado": "error", "error": "Conexión con Ollama interrumpida: reset"}
        return self.decidir(prompt), {"tipo": tipo, "resultado": "completa", "tokens_prompt": 900,
                                      "tokens_generados": 12, "reloj_ms": 1500}


def lee_la_traza(prompt):
    """Sin traza elige siempre la última opción (sesgo de posición); con traza, la que reemplazó por una constante."""
    if "Evidencia de ejecución" in prompt:
        bloques = prompt.split("Evidencia de ejecución")[1]
        for letra in "ABC":
            trozo = bloques.split(f"{letra}) línea")[1].split(") línea")[0] if f"{letra}) línea" in bloques else ""
            if "reemplazó un valor distinto" in trozo:
                return f"CANDIDATO: {letra}\nMOTIVO: sobrescribe el acumulado"
    return "CANDIDATO: C\nMOTIVO: parece"


class UnitTests(unittest.TestCase):
    def test_mcnemar_exact(self):
        self.assertEqual(E.mcnemar_exacto(0, 0), 1.0)
        self.assertAlmostEqual(E.mcnemar_exacto(10, 0), 2 / 1024)
        self.assertEqual(E.mcnemar_exacto(5, 5), 1.0)
        self.assertLess(E.mcnemar_exacto(12, 2), 0.05)

    def test_verdict_follows_the_preregistered_rule(self):
        casos = {"X": {"clase": "cálculo", "codigo": "def medir(db, p):\n    a = 1\n", "sembrada": [2, 2],
                       "traza": None}}

        def ensayos(pares):
            out = []
            for i, (c, t) in enumerate(pares):
                for cond, ok in (("control", c), ("tratamiento", t)):
                    out.append({"caso": "X", "rotacion": i % 3, "repeticion": i, "condicion": cond, "correcto": ok,
                                "posicion_elegida": 0, "ilegible": False, "fugas": [], "orden": [2],
                                "reloj_ms": 1000})
            return out
        self.assertEqual(E.analizar(ensayos([(False, True)] * 12 + [(True, True)] * 4), casos)["veredicto"], "ayuda")
        self.assertEqual(E.analizar(ensayos([(False, True)] * 3 + [(True, True)] * 13), casos)["veredicto"],
                         "inconcluso", "3 discordantes no alcanzan p < 0,05")
        self.assertEqual(E.analizar(ensayos([(True, False)] * 12 + [(True, True)] * 4), casos)["veredicto"],
                         "perjudica")

    def test_choice_parsing_and_selection_rule(self):
        self.assertEqual(E.interpretar("CANDIDATO: b\nMOTIVO: x", [22, 17, 15]), 1)
        self.assertEqual(E.interpretar("CANDIDATO: 17", [22, 17, 15]), 1, "línea de una sola candidata")
        self.assertIsNone(E.interpretar("CANDIDATO: 99", [22, 17, 15]))
        self.assertIsNone(E.interpretar("no sé", [22, 17, 15]))
        casos = [{"id": "D1", "equivalente": False, "sembrada_en": 2, "sembrada": [19, 19], "clase": "cálculo"},
                 {"id": "D2", "equivalente": False, "sembrada_en": None, "sembrada": [21, 22], "clase": "x"},
                 {"id": "M001", "equivalente": False, "sembrada_en": 1, "sembrada": [10, 10], "clase": "cálculo"},
                 {"id": "M002", "equivalente": False, "sembrada_en": 1, "sembrada": [10, 10], "clase": "cálculo"},
                 {"id": "M003", "equivalente": False, "sembrada_en": 3, "sembrada": [12, 12], "clase": "cálculo"},
                 {"id": "M004", "equivalente": True, "sembrada_en": 1, "sembrada": [13, 13], "clase": "cálculo"}]
        self.assertEqual(E.seleccionar(casos, maximo=3), ["D1", "M001", "M003"], "líneas distintas primero")
        self.assertEqual(E.seleccionar(casos, maximo=8), ["D1", "M001", "M003", "M002"])

    def test_plan_is_counterbalanced_paired_and_reproducible(self):
        casos = [{"id": f"C{i}", "candidatas": [10 + i, 20 + i, 30 + i]} for i in range(8)]
        plan = E.ensayos_planificados(casos, [c["id"] for c in casos])
        self.assertEqual(len(plan), 8 * E.ROTACIONES * E.REPETICIONES * 2)
        self.assertEqual(plan, E.ensayos_planificados(casos, [c["id"] for c in casos]), "orden fijo por semilla")
        pares = {}
        for e in plan:
            pares.setdefault((e["caso"], e["rotacion"], e["repeticion"]), []).append(e)
        for par in pares.values():
            self.assertEqual({e["condicion"] for e in par}, {"control", "tratamiento"})
            self.assertEqual(len({e["semilla"] for e in par}), 1, "misma semilla en las dos condiciones")
            self.assertEqual(len({tuple(e["orden"]) for e in par}), 1, "mismo orden en las dos condiciones")
        primeras = {e["orden"][0] for e in plan if e["caso"] == "C0"}
        self.assertEqual(primeras, {10, 20, 30}, "cada candidata aparece primera en alguna rotación")
        self.assertNotEqual([e["condicion"] for e in plan[:8]], ["control"] * 8, "condiciones intercaladas")

    def test_leak_audit(self):
        caso = {"referencia_lineas": ["inicio = m - datetime.timedelta(minutes=m.minute % ancho)"]}
        self.assertEqual(E._auditar(caso, [("coordinador", "algo"), ("evidencia", "x")]), [])
        self.assertTrue(E._auditar(caso, [("traza", "usa inicio = m - datetime.timedelta(minutes=m.minute % "
                                                    "ancho)")]))


@unittest.skipUnless(bwrap_works() and mariadb_fixture.available(), "bubblewrap o MariaDB no disponibles")
class TraceAndChoiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = make_cfg(sandbox_timeout_s=60)
        cls.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        cls.ex = Executor(cls.cfg, limiter=NullLimiter(), allow_unverified_limits=True)
        assert cls.ex.self_test()["ok"]
        cls.dir = Path(tempfile.mkdtemp(prefix="cia-eleccion-"))
        cls.ent = Entorno(cls.cfg, cls.ex, mariadb_fixture.server(), cls.dir / "ent", base="eleccion_t",
                          escala={"dias": 3, "por_dia": 300})
        cls.alm = cls.ent.almacen("t")
        cls.info = E.construir_casos(cls.ent, incluir_mutantes=False, log=lambda *_: None)

    @classmethod
    def tearDownClass(cls):
        cls.ent.cerrar()

    def trazar(self, codigo, lineas):
        us = [P.unidad_en(codigo, ln) for ln in lineas]
        iid = self.alm.save(f"x{time.time_ns()}", codigo, {"proposito": "prueba"}, author="prueba")["id"]
        run = ejecutar(executor=self.ex, broker=self.alm.broker, almacen=self.alm, cfg=self.cfg, instrument_id=iid,
                       params={}, schema=self.ent.schema, traza=T.pedido(us, codigo))
        return run, {u.lineno: T.rasgos(run["verdict"]["traza"], u) for u in us}, us

    def test_trace_is_generic_for_unknown_instruments(self):
        code = DESCONOCIDO.strip() + "\n"
        n = {t: i for i, t in enumerate(code.splitlines(), 1)}
        linea = lambda txt: next(i for t, i in n.items() if t.strip() == txt)   # noqa: E731
        lineas = [linea("total += 1"), linea("total = 0"), linea("if total < 0:"), linea("nunca = 1"),
                  linea("pares = [k for k in maximos if k % 2 == 0]"), linea("maximos[e] = x")]
        lineas.remove(linea("total = 0"))
        sobre = [i for t, i in n.items() if t.strip() == "total = 0"][-1]   # la del final, dentro del if
        run, r, us = self.trazar(code, lineas + [sobre])
        self.assertEqual(run["status"], "ok")
        self.assertEqual(r[linea("total += 1")]["veces"], 900)
        self.assertEqual(r[linea("total += 1")]["pierde"], ["total"], "su valor se pierde después")
        self.assertTrue(r[sobre]["constante_reemplaza"], "reemplazó 900 por una constante")
        self.assertEqual(r[linea("if total < 0:")]["verdadera"], 0)
        self.assertEqual(r[linea("nunca = 1")]["veces"], 0, "nunca se ejecutó")
        self.assertEqual(r[linea("pares = [k for k in maximos if k % 2 == 0]")]["veces"], 1,
                         "una comprensión es UNA ejecución")
        self.assertLess(r[linea("maximos[e] = x")]["veces"], 900, "solo cuando la condición fue verdadera")
        txt = T.texto(run["verdict"]["traza"], [{"unidad": u} for u in us], "ABCDEFG")
        self.assertIn("Su condición fue verdadera en 0 de 1 evaluaciones", txt)
        self.assertIn("se ejecutó 0 veces", txt)
        self.assertNotIn("causa:", txt.lower())
        sin, _, _ = self.trazar.__func__(self, code, []) if False else (None, None, None)
        iid = self.alm.save("sin_traza", code, {"proposito": "prueba"}, author="prueba")["id"]
        run2 = ejecutar(executor=self.ex, broker=self.alm.broker, almacen=self.alm, cfg=self.cfg, instrument_id=iid,
                        params={}, schema=self.ent.schema)
        self.assertNotIn("traza", run2["verdict"], "sin pedido, no hay traza")

    def test_trace_cost_is_bounded_on_long_loops(self):
        code = ("def medir(db, p):\n    acumulado = {}\n    for i in range(200000):\n"
                "        acumulado[i % 5000] = acumulado.get(i % 5000, 0) + i\n"
                "    return {\"resultado\": {\"n\": len(acumulado)}, \"cobertura\": {\"filas_analizadas\": 1},"
                " \"limitaciones\": []}\n")
        t0 = time.monotonic()
        run, r, _ = self.trazar(code, [4])
        self.assertEqual(run["status"], "ok", run.get("error"))
        self.assertEqual(r[4]["veces"], 200000)
        self.assertLess(time.monotonic() - t0, 45, "la representación acotada no recorre el diccionario entero")
        self.assertLess(len(json.dumps(run["verdict"]["traza"])), 4000, "traza acotada")

    def test_stage0_cases_from_the_bank(self):
        casos = {c["id"]: c for c in self.info["casos"]}
        self.assertEqual(casos["D2"]["sembrada_en"], 1)
        self.assertTrue(casos["D2"]["traza_distingue"])
        self.assertTrue(casos["D2"]["senal_exclusiva"])
        self.assertEqual(casos["D1"]["sembrada_en"], 2)
        self.assertEqual(self.info["seleccion"], ["D1", "D2"])
        self.assertTrue(all(isinstance(c.get("traza"), dict) for c in casos.values()))

    def test_stage1_single_difference_seeds_limit_resume_and_analysis(self):
        salida = self.dir / "e1"
        m1 = Modelo(lee_la_traza)
        res = E.correr(self.cfg, crear_modelo=lambda: m1, salida=salida, casos_info=self.info, repeticiones=1,
                       limite=4, log=lambda *_: None)
        self.assertEqual((res["estado"], res["hechos"]), ("limite", 4), "prueba de rendimiento: solo 4 llamadas")
        self.assertEqual(len(m1.llamadas), 4)
        m2 = Modelo(lee_la_traza)
        res = E.correr(self.cfg, crear_modelo=lambda: m2, salida=salida, casos_info=self.info, repeticiones=1,
                       log=lambda *_: None)
        self.assertEqual((res["estado"], res["hechos"]), ("completo", 12), "2 casos × 3 rotaciones × 2 condiciones")
        self.assertEqual(len(m2.llamadas), 8, "reanudar no repite ensayos")
        ensayos = [json.loads(x) for x in (salida / "ensayos.jsonl").read_text().splitlines()]
        for e in ensayos:
            control = E.partes(next(c for c in self.info["casos"] if c["id"] == e["caso"]), e["orden"], "control")
            self.assertEqual([tuple(x) for x in e["partes"] if x[0] != "traza"], control,
                             "la ÚNICA diferencia es la traza")
            self.assertEqual(any(x[0] == "traza" for x in e["partes"]), e["condicion"] == "tratamiento")
            self.assertEqual(e["semilla"], E.semilla(e["caso"], e["rotacion"], e["repeticion"]))
        a = res["analisis"]
        self.assertEqual(a["fugas"], 0)
        self.assertEqual(a["posicion_elegida"]["control"], [0, 0, 6], "el control simulado tiene sesgo de posición")
        d2 = [e for e in ensayos if e["caso"] == "D2" and e["condicion"] == "tratamiento"]
        self.assertTrue(all(e["correcto"] for e in d2), "con la traza, D2 se elige en todas las rotaciones")
        self.assertIn(a["veredicto"], ("ayuda", "inconcluso"))
        self.assertIn("Veredicto preregistrado", (salida / "resumen.md").read_text())

    def test_stage1_environment_interruptions(self):
        m = Modelo(lee_la_traza, fallar_en=(2,))
        res = E.correr(self.cfg, crear_modelo=lambda: m, salida=self.dir / "e2", casos_info=self.info,
                       repeticiones=1, log=lambda *_: None)
        self.assertEqual(len(res["interrupciones"]), 1)
        self.assertEqual(res["hechos"], 11, "el ensayo interrumpido queda pendiente para reanudar")
        m2 = Modelo(lee_la_traza, fallar_en=(1, 2))
        res = E.correr(self.cfg, crear_modelo=lambda: m2, salida=self.dir / "e3", casos_info=self.info,
                       repeticiones=1, log=lambda *_: None)
        self.assertEqual(res["estado"], "detenido_por_entorno", "dos cortes seguidos detienen")


class ForensicTests(unittest.TestCase):
    """forense.py: análisis de SOLO LECTURA de una Etapa 1 guardada (sin modelo), genérico y sin D1/D2."""

    CODIGO = "def medir(db, p):\n    a = 1\n    b = a + 1\n    b = 0\n    return {'resultado': {'b': b}}\n"

    def _partes(self, orden, cond):
        ps = [("coordinador", "Un instrumento responde algo.\n"),
              ("evidencia", 'Salida actual: {"resultado": {"b": 0}}\n'),
              ("evidencia", "Verificaciones que fallan:\n- [modelo] VF: [{'b': 0, 'recontado': 2}] (campos: b)\n"),
              ("coordinador", "Instrucciones candidatas:\n" + "\n".join(f"{l}) instrucción de la línea {n}"
                                                                         for l, n in zip("ABC", orden)))]
        bloques = {2: "se ejecutó 1 vez.\n   Al terminar medir, a conserva el valor que dejó esta línea.",
                   3: "se ejecutó 1 vez.\n   Al terminar medir, b ya no tiene el valor que dejó esta línea "
                      "(cambió después).",
                   4: "se ejecutó 1 vez.\n   Al terminar medir, b conserva el valor que dejó esta línea.\n"
                      "   Asigna un valor constante y reemplazó un valor distinto."}
        if cond == "tratamiento":
            ps.append(("traza", "Evidencia de ejecución:\n" + "\n".join(f"{l}) línea {n}: {bloques[n]}"
                                                                        for l, n in zip("ABC", orden)) + "\n"))
        return ps + [("coordinador", "¿Cuál? CANDIDATO: <letra>")]

    def _carpeta(self):
        d = Path(tempfile.mkdtemp(prefix="cia-forense-"))
        caso = {"id": "X1", "codigo": self.CODIGO, "sembrada": [4, 4], "candidatas": [4, 3, 2]}
        (d / "casos.json").write_text(json.dumps({"casos": [caso], "seleccion": ["X1"]}))
        filas = []
        for rot, orden in enumerate(([4, 3, 2], [3, 2, 4])):
            for cond, linea, resp in (
                    ("control", 4, "CANDIDATO: {L}\nMOTIVO: la línea 4 pone b en cero."),
                    ("tratamiento", 3, "CANDIDATO: {L}\nMOTIVO: la línea 4 reinicia b, así que la línea 3 se pierde."
                     if rot == 0 else "CANDIDATO: {L}\nMOTIVO: falta el campo recontado en el resultado.")):
                pos = orden.index(linea)
                filas.append({"clave": f"X1|{rot}|1|{cond}", "caso": "X1", "rotacion": rot, "repeticion": 1,
                              "condicion": cond, "orden": orden, "respuesta": resp.replace("{L}", "ABC"[pos]),
                              "posicion_elegida": pos, "linea_elegida": linea, "correcto": linea == 4,
                              "ilegible": False, "partes": self._partes(orden, cond)})
        (d / "ensayos.jsonl").write_text("\n".join(json.dumps(f, ensure_ascii=False) for f in filas) + "\n")
        return d

    def test_coherence_sources_and_trace_labels(self):
        from constructor_ia.laboratorio.piloto import forense as F
        d = self._carpeta()
        res = F.analizar_carpeta(d)
        self.assertEqual([x["efecto"] for x in res["discordantes"]], ["perjudicó", "perjudicó"])
        t0, t1 = (res["discordantes"][i]["tratamiento"] for i in (0, 1))
        # el motivo explica el mecanismo con la sembrada (4) y elige la víctima (3)
        self.assertEqual((t0["nombradas"], t0["coherencia"], t0["nombra_sembrada"]),
                         ([4, 3], "nombra varias, incluida la elegida", True))
        self.assertEqual(t0["etiqueta_elegida"], ["se pierde"])
        self.assertEqual(t0["etiqueta_sembrada"], [["conserva", "constante que reemplaza"]])
        # la explicación sale de un término que solo está en la verificación
        self.assertEqual((t1["coherencia"], t1["terminos_solo_verificacion"]), ("sin referencia", ["recontado"]))
        self.assertTrue(t1["elegida_bloque_ultimo"] is False and t0["elegida_bloque_ultimo"] is False)
        r = res["por_condicion"]["tratamiento"]
        self.assertEqual((r["incorrectas"], r["incorrectas_que_nombran_la_sembrada"]), (2, 1))
        self.assertEqual(res["traza"]["etiqueta_de_la_elegida_si_incorrecta"], {"se pierde": 2})
        self.assertIn("la traza perjudicó", F.markdown(res))

    def test_cli_is_read_only(self):
        import hashlib
        from constructor_ia import cli
        d = self._carpeta()
        antes = {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in d.iterdir()}
        ns = types.SimpleNamespace(carpeta=str(d), salida=str(d / "forense.md"), json=None)
        self.assertEqual(cli.cmd_lab_choice_forensic(ns), 2, "no escribe dentro de la carpeta del experimento")
        fuera = Path(tempfile.mkdtemp(prefix="cia-forense-out-"))
        ns = types.SimpleNamespace(carpeta=str(d), salida=str(fuera / "f.md"), json=str(fuera / "f.json"))
        self.assertEqual(cli.cmd_lab_choice_forensic(ns), 0)
        self.assertTrue((fuera / "f.md").exists() and (fuera / "f.json").exists())
        self.assertEqual(antes, {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in d.iterdir()})


if __name__ == "__main__":
    unittest.main()
