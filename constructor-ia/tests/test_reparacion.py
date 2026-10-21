"""Regresiones de la primera prueba real de 0.6 (Qwen3 4B, tarea T1, escala pequeña, 1 corrección).

Lo que pasó: el instrumento comparaba un DECIMAL (llega como texto) con un float, atrapaba el TypeError y devolvía
{"desde": 0, "hasta": 0} con filas_analizadas = 0; el laboratorio lo marcó ok / cobertura demostrada; las dos
autoevaluaciones (que decían «no») se rechazaron por formato y eso disparó una reescritura del instrumento con un
diagnóstico pobre, que conservó el error.

Los instrumentos de estas pruebas imitan esos ERRORES (son fallas, no soluciones). Los guiones del Ollama simulado
validan el circuito, no la capacidad de un modelo.
"""
import json
import random
import unittest

from constructor_ia.config import ExplorerConnection
from constructor_ia.db import Database
from constructor_ia.explorer.broker import Broker
from constructor_ia.instrumentos import diagnostico as D
from constructor_ia.instrumentos.almacen import Almacen
from constructor_ia.instrumentos.medicion import ejecutar, observar_esquema
from constructor_ia.instrumentos.verificacion import verify
from constructor_ia.laboratorio import sintetica as S, tareas as T
from constructor_ia.laboratorio.protocolo import Modelo, Sesion, cerrar_json, diagnosticar_respuesta
from constructor_ia.ollama import OllamaClient
from constructor_ia.sandbox.executor import Executor
from constructor_ia.sandbox.limiter import NullLimiter

from . import mariadb_fixture
from .fake_ollama import FakeOllama
from .helpers import bwrap_works, make_cfg
from .test_explorer import pw_file

# Falla 1+2+3: compara un DECIMAL (texto) con un float, atrapa el error y devuelve un resultado vacío con 0 filas.
# La consulta de intervalos relativos al mínimo devuelve un índice 0, que antes «demostraba» filas_analizadas = 0.
ATRAPA = '''
def medir(db, p):
    try:
        h = db.consultar("SELECT FLOOR((valor - (SELECT MIN(valor) FROM lecturas)) / 2) AS i, COUNT(*) AS n "
                         "FROM lecturas GROUP BY i ORDER BY n DESC", max_filas=100)
        r = db.consultar("SELECT MIN(valor) AS mn, MAX(valor) AS mx FROM lecturas")
        mn, mx = r.filas[0]
        if mn > 0.0:
            return {"resultado": {"desde": mn, "hasta": mx}, "cobertura": {"filas_analizadas": 1}, "limitaciones": []}
    except Exception as e:
        return {"resultado": {"desde": 0.0, "hasta": 0.0}, "cobertura": {"filas_analizadas": 0},
                "limitaciones": ["Error inesperado: " + str(e)]}
'''

# La misma falla sin atraparla: el error sale con su línea.
SUELTA = '''
def medir(db, p):
    r = db.consultar("SELECT SUM(TIMESTAMPDIFF(SECOND, momento, recibido) > 3600) AS tarde, COUNT(*) AS n "
                     "FROM lecturas")
    tarde, n = r.filas[0]
    prop = tarde / n
    return {"resultado": {"proporcion": prop, "tipos": r.tipos}, "cobertura": {"filas_analizadas": n},
            "limitaciones": []}
'''

# Reparación aparente que no toca la causa (cambia otra cosa).
SUELTA_V2 = SUELTA.replace('"limitaciones": []', '"limitaciones": ["proporción global"]')
SUELTA_V3 = SUELTA_V2.replace("prop = tarde / n", "prop = round(tarde / n, 4)")
# Reparación que sí ataca la causa.
ARREGLADA = SUELTA.replace("prop = tarde / n", "prop = float(tarde) / int(n)")

# Usa excepciones como control de flujo legítimo y mide bien.
CONTROL = '''
def medir(db, p):
    r = db.consultar("SELECT estacion_id, COUNT(*) AS n FROM lecturas GROUP BY estacion_id")
    conteo = {}
    for e, n in r.filas:
        try:
            conteo[e] += n
        except KeyError:
            conteo[e] = n
    return {"resultado": {"estaciones": len(conteo)}, "cobertura": {"filas_analizadas": sum(conteo.values())},
            "limitaciones": []}
'''

# T1 reparada (lo que el guion simulado devuelve tras leer el diagnóstico).
T1_REPARADA = '''
def medir(db, p):
    h = db.consultar("SELECT FLOOR(valor/2)*2 AS b, COUNT(*) AS n FROM lecturas WHERE valor > 0.5 AND valor < 900 "
                     "GROUP BY b ORDER BY n DESC", max_filas=50)
    total = db.consultar("SELECT COUNT(*) FROM lecturas").filas[0][0]
    b, n = h.filas[0]
    return {"resultado": {"desde": float(b), "hasta": float(b) + 2}, "cobertura": {"filas_analizadas": int(total)},
            "limitaciones": []}
'''


def bloques(code, nombre="medida", params=None, causa=None):
    m = {"nombre": nombre, "proposito": "medir", "parametros": params or {}, "plan": "", "muestreo": "ninguno",
         "bibliotecas": []}
    if causa:
        m["causa"] = causa
    return f"```json\n{json.dumps(m)}\n```\n```python\n{code.strip()}\n```"


class UnitTests(unittest.TestCase):
    def test_zero_coverage_is_null_even_if_an_aggregate_returned_zero(self):
        acc = {"consultas": [{"op": "consultar", "estado": "ok", "filas": 7, "agregada": True}]}
        out = {"resultado": {"desde": 0.0, "hasta": 0.0}, "cobertura": {"filas_analizadas": 0}, "limitaciones": []}
        v = verify(out, acc, {"0", "310"}, 4096)
        self.assertEqual(v["cobertura"], "nula", "con 0.6 era «demostrada» porque un índice de intervalo valía 0")
        self.assertEqual(v["medicion"], "sin_evidencia")
        exc = [{"tipo": "TypeError", "mensaje": "'>' not supported", "linea": 7}]
        self.assertEqual(verify(out, acc, {"0"}, 4096, exc)["medicion"], "error_de_calculo")
        bueno = {"resultado": {"n": 5}, "cobertura": {"filas_analizadas": 310}, "limitaciones": []}
        self.assertEqual(verify(bueno, acc, {"310"}, 4096)["medicion"], "valida")
        v = verify(bueno, acc, {"310"}, 4096, [{"tipo": "KeyError", "mensaje": "3", "veces": 2}])
        self.assertEqual(v["medicion"], "valida", "una excepción capturada que no vacía el resultado no lo invalida")
        self.assertTrue(v["advertencias"], "pero queda advertida")
        menciona = {**bueno, "limitaciones": ["Error inesperado: '>' not supported"]}
        self.assertEqual(verify(menciona, acc, {"310"}, 4096, exc)["medicion"], "error_de_calculo")

    def test_unreadable_json_is_diagnosed_and_rescued(self):
        largo = '{"responde": "no", "motivo": "' + "el intervalo obtenido es vacío porque " * 40
        d = diagnosticar_respuesta(largo, {"resultado": "truncada", "tokens_generados": 768})
        self.assertEqual(d["causa"], "cortada_por_limite")
        self.assertIn("768", d["detalle"])
        self.assertEqual(d["crudo"], largo, "se conserva el texto completo (antes: 300 caracteres)")
        self.assertEqual(diagnosticar_respuesta('{"responde": "no" "motivo": 1}', {"resultado": "completa"})["causa"],
                         "json_invalido")
        self.assertEqual(diagnosticar_respuesta('{"motivo": "x"}', {"resultado": "completa"})["causa"],
                         "falta_responde")
        self.assertEqual(diagnosticar_respuesta(None, {"resultado": "error", "error": "x"})["causa"], "sin_respuesta")
        self.assertEqual(cerrar_json(largo)["responde"], "no")

    def test_review_reads_a_0_6_0_run_without_changing_it(self):
        import tempfile
        from pathlib import Path

        from constructor_ia.laboratorio import informe

        d = Path(tempfile.mkdtemp())
        viejo = {"version": "0.6.0-exp1", "estado": "completa", "sesiones": [{"modelo": "qwen3:4b-instruct",
                 "repeticion": 1, "registros": [{"tarea": "T1", "puntaje": 0.0,
                 "detalle_puntaje": "sin respuesta en la forma pedida",
                 "llamadas": [{"tipo": "evaluacion-T1", "resultado": "truncada", "tokens_generados": 768,
                               "formato_ok": False}],
                 "evaluaciones": [{"formato_ok": False, "crudo": '{"responde": "no", "motivo": "el interv'}]}]}]}
        (d / "resultados.json").write_text(json.dumps(viejo))
        antes = (d / "resultados.json").read_bytes()
        out = informe.revisar(d)
        self.assertIn("llamada evaluacion-T1: truncada, 768 tokens generados, formato INVÁLIDO", out)
        self.assertIn('{"responde": "no"', out)
        self.assertEqual((d / "resultados.json").read_bytes(), antes)

    def test_failure_signature_ignores_line_numbers(self):
        a = {"status": "error", "verdict": {"falla": {"tipo": "TypeError", "mensaje": "x at 12"}}}
        b = {"status": "error", "verdict": {"falla": {"tipo": "TypeError", "mensaje": "x at 15"}}}
        self.assertEqual(D.firma(a), D.firma(b))
        self.assertEqual(D.firma({"status": "ok", "verdict": {"medicion": "valida"}}), "")
        c = D.cambio("a\nb\n", "a\nc\n")
        self.assertEqual((c["identico"], c["lineas_cambiadas"]), (False, 2))
        self.assertTrue(D.cambio("a\n", "a\n\n")["identico"])


@unittest.skipUnless(bwrap_works() and mariadb_fixture.available(), "bubblewrap o MariaDB no disponibles")
class RepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = mariadb_fixture.server()
        cls.base = f"rep_{random.randint(0, 10**8)}"
        cls.info = S.generar(cls.srv, cls.base, dias=32, por_dia=150)
        cls.cfg = make_cfg(sandbox_timeout_s=30)
        cls.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        cls.db = Database(cls.cfg.db_path)
        cls.db.migrate()
        cls.ex = Executor(cls.cfg, limiter=NullLimiter(), allow_unverified_limits=True)
        assert cls.ex.self_test()["ok"]
        pw = pw_file(cls.cfg.data_dir, cls.info["clave"])
        cls.broker = Broker(cls.cfg, ExplorerConnection("lab", cls.base, cls.info["usuario"], pw,
                                                        socket=cls.srv.socket), cls.db)
        assert cls.broker.verify()["verified"], cls.broker.status
        cls.schema = observar_esquema(cls.broker)
        cls.store = Almacen(cls.db, cls.cfg)
        cls.fakes = []

    @classmethod
    def tearDownClass(cls):
        for f in cls.fakes:
            f.close()
        cls.srv.sql(f"DROP DATABASE IF EXISTS `{cls.base}`", check=False)

    def run_code(self, code, name):
        inst = self.store.save(name, code.strip() + "\n", {"proposito": "prueba"}, author="prueba")
        return ejecutar(executor=self.ex, broker=self.broker, almacen=self.store, cfg=self.cfg,
                        instrument_id=inst["id"], params={}, schema=self.schema)

    def sesion(self, responses, **kw):
        fake = FakeOllama(responses=responses)
        self.fakes.append(fake)
        modelo = Modelo(OllamaClient(fake.url), self.cfg.explorer_model, "modelo", num_ctx=8192, temperatura=0.2,
                        timeout_s=60)
        logs = []
        ses = Sesion(cfg=self.cfg, modelo=modelo, executor=self.ex, broker=self.broker, almacen=self.store,
                     server=self.srv, info=self.info, schema=self.schema, log=logs.append, **kw)
        return ses, fake, logs

    def prompts(self, fake):
        return [b["messages"][-1]["content"] for b in fake.requests]

    # 1 y 2 -------------------------------------------------------------------------------------------------
    def test_1_2_caught_exception_with_empty_result_and_zero_coverage(self):
        run = self.run_code(ATRAPA, "atrapa")
        v = run["verdict"]
        self.assertEqual(run["status"], "ok", "técnicamente terminó")
        self.assertEqual(v["medicion"], "error_de_calculo", v)
        self.assertEqual(v["cobertura"], "nula", "0 filas no se «demuestran» con un índice 0")
        exc = v["excepciones_capturadas"]
        self.assertEqual(len(exc), 1, exc)
        self.assertEqual(exc[0]["tipo"], "TypeError")
        self.assertIn("'>' not supported between instances of 'str' and 'float'", exc[0]["mensaje"])
        self.assertEqual(exc[0]["codigo"], "if mn > 0.0:")
        self.assertEqual(exc[0]["linea"], ATRAPA.lstrip("\n").splitlines().index("        if mn > 0.0:") + 1)
        self.assertEqual(D.firma(run), "TypeError: '>' not supported between instances of 'str' and 'float'")
        texto = D.texto(D.diagnosticar(run))
        self.assertIn("CAPTURADA", texto)
        self.assertIn("mn DECIMAL→str", texto, "el tipo con el que llegó el valor está a la vista")
        bien = self.run_code(CONTROL, "control")
        self.assertEqual(bien["verdict"]["medicion"], "valida", bien["verdict"])
        self.assertEqual(bien["verdict"]["cobertura"], "demostrada")
        self.assertTrue(bien["verdict"]["advertencias"], "KeyError usado como control de flujo: sólo advertencia")

    # 3 -----------------------------------------------------------------------------------------------------
    def test_3_numeric_sql_value_arriving_as_text(self):
        run = self.run_code(SUELTA, "suelta")
        self.assertEqual(run["status"], "error")
        falla = run["verdict"]["falla"]
        self.assertEqual(falla["tipo"], "TypeError")
        self.assertEqual(falla["lugar"]["codigo"], "prop = tarde / n")
        self.assertEqual(falla["lugar"]["archivo"], "instrumento.py")
        q = [x for x in run["accounting"]["consultas"] if x["op"] == "consultar"][0]
        cols = {c["nombre"]: c for c in q["columnas"]}
        self.assertEqual((cols["tarde"]["tipo_mariadb"], cols["tarde"]["llega_como"]), ("DECIMAL", "str"))
        self.assertEqual((cols["n"]["tipo_mariadb"], cols["n"]["llega_como"]), ("BIGINT", "int"))
        texto = D.texto(D.diagnosticar(run))
        self.assertIn("línea 5 (medir): `prop = tarde / n`", texto)
        self.assertNotIn("CAPTURADA", texto, "el error que terminó el programa no se informa como capturado")
        self.assertEqual(run["verdict"]["excepciones_capturadas"], [])
        self.assertIn("tarde DECIMAL→str", texto)
        ok = self.run_code(ARREGLADA, "suelta")
        self.assertEqual(ok["verdict"]["medicion"], "valida")
        self.assertEqual(ok["output"]["resultado"]["tipos"], ["DECIMAL", "BIGINT"], "Resultado.tipos en el sandbox")

    # 4 -----------------------------------------------------------------------------------------------------
    def test_4_unreadable_self_evaluation_does_not_rewrite_the_instrument(self):
        cortada = '{"responde": "si", "motivo": "' + "la medición cubre toda la tabla y " * 60
        verdad = T.TAREAS["T4"].verdad(self.srv, self.base, self.info)

        def eval_ok(body):
            return json.dumps({"responde": "si", "motivo": "ok", "respuesta": {"proporcion": verdad["proporcion"]},
                               "mejora": ""})

        # a) cortada por el límite → reparación del FORMATO → legible; el instrumento no se reescribe
        ses, fake, logs = self.sesion([bloques(ARREGLADA, "tardias_a"), {"text": cortada, "done_reason": "length"},
                                       eval_ok])
        reg = ses.correr(T.TAREAS["T4"], verdad)
        self.assertEqual([c["tipo"] for c in reg["llamadas"]], ["diseno-T4", "evaluacion-T4", "formato-T4"])
        self.assertEqual(len(reg["versiones"]), 1, "ninguna versión nueva por un problema de formato")
        ev = reg["evaluaciones"][0]
        self.assertEqual(ev["formato"], "reparado")
        self.assertEqual(ev["diagnostico_formato"][0]["causa"], "cortada_por_limite")
        self.assertEqual(ev["diagnostico_formato"][0]["crudo"].strip(), cortada.strip(), "texto completo conservado")
        self.assertIn("No cambies tu conclusión ni el instrumento", self.prompts(fake)[2])
        self.assertEqual(reg["puntaje"], 1.0)
        # b) la reparación también se corta → se rescata lo completo (la conclusión), sin reescribir
        ses, fake, _ = self.sesion([bloques(ARREGLADA, "tardias_b"), {"text": cortada, "done_reason": "length"},
                                    {"text": cortada, "done_reason": "length"}])
        reg = ses.correr(T.TAREAS["T4"], verdad)
        self.assertEqual(reg["evaluaciones"][0]["formato"], "rescatado")
        self.assertEqual(reg["autoevaluacion_final"], "si")
        self.assertEqual(len(reg["versiones"]), 1)
        self.assertEqual(reg["puntaje"], 0.0, "sin respuesta en la forma pedida: no se inventa")
        # c) nada legible → «ilegible»: la tarea termina sin tocar el instrumento
        ses, fake, _ = self.sesion([bloques(ARREGLADA, "tardias_c"), "no sé", "tampoco"])
        reg = ses.correr(T.TAREAS["T4"], verdad)
        self.assertTrue(reg["evaluacion_ilegible"])
        self.assertEqual(reg["evaluaciones"][0]["formato"], "ilegible")
        self.assertEqual([d["causa"] for d in reg["evaluaciones"][0]["diagnostico_formato"]],
                         ["json_invalido", "json_invalido"])
        self.assertEqual(len(reg["versiones"]), 1)
        self.assertEqual(reg["correcciones"], 0)

    # 5 -----------------------------------------------------------------------------------------------------
    def test_5_repair_that_does_not_fix_the_error_is_shown_and_bounded(self):
        verdad = T.TAREAS["T4"].verdad(self.srv, self.base, self.info)
        ses, fake, logs = self.sesion([bloques(SUELTA, "tardias_r"),
                                       bloques(SUELTA_V2, "tardias_r", causa="agregué una limitación"),
                                       bloques(SUELTA_V3, "tardias_r", causa="redondeé"),
                                       bloques(ARREGLADA, "tardias_r")],
                                      max_correcciones=5, max_mismo_error=3)
        reg = ses.correr(T.TAREAS["T4"], verdad)
        self.assertEqual([i["resultado"] for i in reg["intentos"]], ["primera", "mismo_error", "mismo_error"])
        self.assertEqual(reg["error_persistente"]["veces"], 3, "se corta antes de agotar el presupuesto")
        self.assertEqual(len(fake.requests), 3, "la cuarta respuesta nunca se pidió")
        p = self.prompts(fake)
        self.assertIn("DIAGNÓSTICO", p[1])
        self.assertIn("`prop = tarde / n`", p[1])
        self.assertIn("tarde DECIMAL→str", p[1])
        self.assertIn("MISMO ERROR", p[2])
        self.assertIn("Último cambio", p[2])
        self.assertNotIn("evaluacion", " ".join(c["tipo"] for c in reg["llamadas"]), "no se evalúa lo que no midió")
        self.assertEqual(reg["intentos"][1]["causa_declarada"], "agregué una limitación")
        # La misma falla, reparada en el segundo intento: una primera falla no es incapacidad.
        ses, fake, _ = self.sesion([bloques(SUELTA, "tardias_s"), bloques(SUELTA, "tardias_s"),
                                    bloques(ARREGLADA, "tardias_s"),
                                    json.dumps({"responde": "si", "motivo": "ok", "mejora": "",
                                                "respuesta": {"proporcion": verdad["proporcion"]}})])
        reg = ses.correr(T.TAREAS["T4"], verdad)
        self.assertEqual([i["resultado"] for i in reg["intentos"]], ["primera", "sin_cambios", "corregido"])
        self.assertIn("código IDÉNTICO", self.prompts(fake)[2])
        self.assertEqual((reg["puntaje"], reg["correccion_exitosa"]), (1.0, True))

    # 6 -----------------------------------------------------------------------------------------------------
    def test_6_valid_measurement_that_does_not_answer_the_question(self):
        verdad = T.TAREAS["C"].verdad(self.srv, self.base, self.info)
        no = json.dumps({"responde": "no", "motivo": "un resumen global no distingue grupos", "respuesta": {},
                         "mejora": "agrupar por zona"})
        ses, fake, _ = self.sesion([no, bloques(T.INSUFICIENTE, "resumen_lecturas"), no], max_correcciones=1)
        reg = ses.correr(T.TAREAS["C"], verdad)
        v0 = reg["versiones"][0]
        self.assertEqual((v0["estado"], v0["medicion"], v0["cobertura"]), ("ok", "valida", "demostrada"),
                         "la verificación determinista dice que hubo datos, no que responda")
        self.assertTrue(reg["detecto_insuficiencia"])
        self.assertEqual([c["tipo"] for c in reg["llamadas"]], ["evaluacion-C", "revision-C", "evaluacion-C"])
        self.assertIn("Revisá el MÉTODO", self.prompts(fake)[1])
        self.assertEqual(reg.get("revisiones"), 1)
        self.assertIsNone(reg.get("reparaciones"))
        self.assertEqual(reg["intentos"][1]["resultado"], "sin_cambios")
        self.assertEqual((reg["puntaje"], reg["calibracion"]), (0.0, "calibrada"))

    # La secuencia de la corrida real, con el circuito nuevo -------------------------------------------------
    def test_t1_run_sequence_now_repairs_with_a_diagnosis(self):
        verdad = T.TAREAS["T1"].verdad(self.srv, self.base, self.info)

        def repara(body):
            u = body["messages"][-1]["content"]
            assert "mn DECIMAL→str" in u and "`if mn > 0.0:`" in u, u
            return bloques(T1_REPARADA, "rango_lecturas", causa="MIN(valor) llega como texto")

        def evalua(body):
            out = json.loads(body["messages"][-1]["content"].split("SALIDA:\n```json\n")[1].split("\n```")[0])
            return json.dumps({"responde": "si", "motivo": "histograma completo", "respuesta": out["resultado"],
                               "mejora": ""})

        ses, fake, logs = self.sesion([bloques(ATRAPA, "rango_lecturas"), repara, evalua], max_correcciones=1)
        reg = ses.correr(T.TAREAS["T1"], verdad)
        self.assertEqual([c["tipo"] for c in reg["llamadas"]], ["diseno-T1", "reparacion-T1", "evaluacion-T1"],
                         "con 0.6.0: diseño, evaluación ilegible, corrección sin diagnóstico, evaluación ilegible")
        self.assertEqual([(v["estado"], v["medicion"]) for v in reg["versiones"]],
                         [("ok", "error_de_calculo"), ("ok", "valida")])
        self.assertEqual([i["resultado"] for i in reg["intentos"]], ["primera", "corregido"])
        self.assertEqual(reg["puntaje"], 1.0, reg["detalle_puntaje"])


if __name__ == "__main__":
    unittest.main()
