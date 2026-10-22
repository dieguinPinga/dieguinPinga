"""Ciclo de investigación v1.3 (PILOTO.md, «Ciclo v1», «v1.1», «v1.2» y «v1.3»): diagnóstico estructurado →
reparación → coherencia → verificación → juez separado.

Los modelos simulados validan el CIRCUITO (aislamiento del juez, diagnóstico y coherencia, verificación en dos juegos
de parámetros, forma de la salida, confirmaciones falsas medidas, límite y reanudación, interrupciones), NO la
capacidad de un modelo: las respuestas viejas de Qwen reproducen fallos anteriores, pero no predicen cómo responderá
Qwen con el formato nuevo.
"""
import ast
import json
import re
import tempfile
import types
import unittest
from pathlib import Path

from constructor_ia.laboratorio.piloto import banco as B
from constructor_ia.laboratorio.piloto import ciclo as C
from constructor_ia.laboratorio.piloto import defectos as DF
from constructor_ia.laboratorio.piloto import e0 as E0
from constructor_ia.laboratorio.piloto.entorno import Entorno
from constructor_ia.sandbox.executor import Executor
from constructor_ia.sandbox.limiter import NullLimiter

from . import mariadb_fixture
from .helpers import bwrap_works, make_cfg

BANCO = Path(__file__).resolve().parent.parent / "constructor_ia" / "laboratorio" / "piloto" / "banco_desarrollo"
DATOS = Path(__file__).resolve().parent / "datos"
REF = B.REFERENCIA.splitlines()


def banco(*ids):
    ok, problemas = DF.cargar(BANCO)
    assert not problemas, problemas
    return [d for d in ok if not ids or d["id"] in ids]


def sugerida_del_prompt(prompt: str) -> int:
    return int(re.search(r"El coordinador sugiere investigar la línea (\d+)", prompt).group(1))


class Modelo:
    """Responde con una función (prompt, línea sugerida) → texto."""

    def __init__(self, responder, fallar_en=()):
        self.responder, self.fallar_en = responder, set(fallar_en)
        self.nombre, self.system, self.base = "simulado", "", types.SimpleNamespace(num_ctx=8192)
        self.llamadas = []

    def pedir(self, tipo, prompt, *, json_out, cancel=None, corto=False, seed=None):
        self.llamadas.append({"seed": seed, "prompt": prompt})
        if len(self.llamadas) in self.fallar_en:
            return None, {"tipo": tipo, "resultado": "error", "error": "Conexión con Ollama interrumpida"}
        return self.responder(prompt, sugerida_del_prompt(prompt)), {"tipo": tipo, "resultado": "completa",
                                                                     "reloj_ms": 1000}


def responde(linea, cambia, codigo, efecto=None, motivo="simulado"):
    return (f"DIAGNOSTICO: {linea}\nCAMBIA: {cambia}\n" + (f"EFECTO: {efecto}\n" if efecto else "")
            + f"```python\n{codigo}\n```\nMOTIVO: {motivo}")


def inocuo(prompt, linea):
    """Diagnostica la línea sugerida y propone un cambio que no altera el comportamiento (para recorrer la cola)."""
    codigo = next(d["codigo"] for d in banco("X07"))
    instr = C.instruccion(codigo, C.P.unidad_en(codigo, linea))
    destino = next(iter(C._especificas(C.escrituras(ast.parse(instr).body))))
    return responde(linea, C._nombre(destino), instr + " + 0")


# Respuesta REAL de Qwen en la VivoBook (commit 49a7e25, X01, hipótesis 19, protocolo v1.1): copió el prefijo «19 |».
QWEN_X01_V11 = ('```python\n19 |         inicio = datetime.datetime.strptime(minuto, "%Y-%m-%d %H:%M") - '
                'datetime.timedelta(minutes=m.minute % ancho)\n```\n'
                'MOTIVO: el error es usar ancho + 1 en lugar de ancho.')
# La misma reparación con el formato v1.3.
QWEN_X01_V13 = 'DIAGNOSTICO: 19\nCAMBIA: inicio\n' + QWEN_X01_V11

# X07 con v1.2 (commit b954b2f): la línea 12 es TEXTUAL (la citaron Diego y ChatGPT); las de 17 y 15 son
# RECONSTRUCCIONES de su descripción («explicó que el defecto era leidas, pero modificó validas / invalidas»).
X07_V12_L12 = 'cobertura["leidas"] = r.filas[0][0] is not None and len(r.filas) > 0'
X07_V12_L17_RECONSTRUIDA = 'cobertura["validas"] = cobertura["leidas"] - cobertura["invalidas"]'
X07_V12_L15_RECONSTRUIDA = 'cobertura["invalidas"] = cobertura["leidas"] - cobertura["validas"]'


class UnitTests(unittest.TestCase):
    def test_rules_are_the_frozen_ones(self):
        self.assertEqual(E0.huella_reglas(), C.HUELLA_REGLAS)

    def test_coordinator_params_are_not_the_judges(self):
        self.assertNotIn(B.PARAMS_RESERVA, C.PARAMS_COORDINADOR)
        self.assertEqual(C.PARAMS_COORDINADOR[0], B.PARAMS_VISIBLES)

    def test_omissions_are_scored_by_insertion_point(self):
        por = {d["id"]: d for d in banco()}
        self.assertEqual(DF.objetivo(por["X11"]), {14, 15}, "falta «continue» al final del cuerpo del if")
        self.assertEqual(DF.objetivo(por["X12"]), {14, 17}, "falta una instrucción entre el if y la l.17")
        self.assertEqual(DF.objetivo(por["X04"]), {22})
        sin = dict(por["X11"], insercion={"despues_de": 14, "sangria": 12})
        self.assertEqual(DF.objetivo(sin), {14, 15}, "hueco al principio del cuerpo: la siguiente y el dueño")

    def test_bad_insertion_is_rejected(self):
        d = Path(tempfile.mkdtemp())
        (d / "a.py").write_text(B.REFERENCIA)
        (d / "manifiesto.json").write_text(json.dumps([{"id": "Y1", "archivo": "a.py", "lineas": [3, 3],
                                                        "clase": "omisión", "insercion": {"despues_de": 999}}]))
        ok, problemas = DF.cargar(d)
        self.assertEqual(ok, [])
        self.assertIn("insercion", problemas[0])

    def test_response_fields_are_read_outside_the_code_block(self):
        it = C.interpretar('DIAGNOSTICO: 12\nCAMBIA: cobertura["leidas"]\nEFECTO: leidas = 15000\n'
                           '```python\n# DIAGNOSTICO: 99\ncobertura["leidas"] += 1\n```\nMOTIVO: acumula')
        self.assertEqual((it["tipo"], it["diagnostico"], it["cambia"], it["efecto"], it["motivo"]),
                         ("reemplazar", "12", 'cobertura["leidas"]', "leidas = 15000", "acumula"))
        self.assertEqual(C.interpretar("DIAGNOSTICO: 12\nCAMBIA: x\nELIMINAR\nMOTIVO: sobra")["tipo"], "eliminar")
        self.assertEqual(C.interpretar("DIAGNOSTICO: NINGUNA\nMOTIVO: no")["tipo"], "ninguna")
        self.assertEqual(C.interpretar("**DIAGNOSTICO:** 12\nCAMBIA: x")["tipo"], "sin_codigo")
        self.assertEqual(C.interpretar(None)["tipo"], "sin_codigo")

    def test_declared_target_forms(self):
        self.assertEqual(C.objetivo_declarado("total"), ("total", None))
        for forma in ('cobertura["leidas"]', "cobertura['leidas']", "cobertura.leidas", '`cobertura["leidas"]`'):
            self.assertEqual(C.objetivo_declarado(forma), ("cobertura", "leidas"), forma)
        self.assertIsNone(C.objetivo_declarado("el contador de lecturas"))

    def test_leak_audit(self):
        x04 = banco("X04")[0]
        self.assertEqual(C.auditar([("coordinador", x04["codigo"])], x04), [])
        self.assertTrue(C.auditar([("coordinador", REF[21])], x04), "la línea de la referencia que el defecto cambió")
        self.assertTrue(C.auditar([("evidencia", x04["descripcion"])], x04))
        self.assertTrue(C.auditar([("evidencia", f"Con los parámetros {B.PARAMS_RESERVA}")], x04))

    def test_prompt_offers_all_candidates_and_only_suggests_one(self):
        x04 = banco("X04")[0]
        obs = {"candidatas": [21, 19, 20],
               "juegos": [{"params": p, "salida": {"resultado": {}}, "fallas": [{"id": "VF"}],
                           "texto": "- VF: algo (campos: suma)"} for p in C.PARAMS_COORDINADOR]}
        ps = C.partes({"id": "X04", "codigo": x04["codigo"]}, obs, 20)
        texto = "\n".join(t for _, t in ps)
        self.assertIn("es solo una sugerencia, TODAVÍA NO COMPROBADA", texto)
        self.assertIn("Línea 20  ← sugerida por el coordinador:\n```python\nsumas[inicio] = sumas.get(inicio, 0.0) + v"
                      "\n```", texto)
        self.assertIn("Línea 21:\n```python\nmejores = sorted", texto)
        self.assertIn("DIAGNOSTICO: <número de línea de UNA de las instrucciones candidatas>", texto)
        self.assertIn("SIN números de línea", texto)
        self.assertIn(str(C.PARAMS_COORDINADOR[1]), texto)
        self.assertNotIn("[modelo]", texto)
        self.assertEqual(C.auditar(ps, x04), [])

    def test_a_folder_from_another_version_is_not_resumed(self):
        d = Path(tempfile.mkdtemp())
        (d / "llamadas.jsonl").write_text('{"clave": "X01|h0|L19", "respuesta": "x"}\n')
        with self.assertRaisesRegex(RuntimeError, "ciclo v1»"):
            C.correr(None, None, defectos=[], salida=d)
        self.assertEqual((d / "llamadas.jsonl").read_text(), '{"clave": "X01|h0|L19", "respuesta": "x"}\n',
                         "la corrida original no se toca")
        self.assertFalse((d / "version.json").exists())
        for anterior in ("ciclo v1.1", "ciclo v1.2"):
            e = Path(tempfile.mkdtemp())
            (e / "version.json").write_text(json.dumps({"version": anterior}))
            with self.assertRaisesRegex(RuntimeError, f"{anterior}»"):
                C.correr(None, None, defectos=[], salida=e)

    def test_cli_rejects_an_unbounded_budget_before_doing_anything(self):
        from constructor_ia import cli
        ns = types.SimpleNamespace(sin_modelo=False, limite_llamadas=3, completo=False, llamadas_por_defecto=7,
                                   reanudar=None, defectos=str(BANCO), ids="X07", yes=True)
        self.assertEqual(cli.cmd_lab_cycle(ns), 2)


class DiagnosisAndCoherenceTests(unittest.TestCase):
    """v1.3: el diagnóstico decide DÓNDE; la coherencia comprueba que el código haga lo que se dijo."""

    def setUp(self):
        self.x07 = banco("X07")[0]["codigo"]          # candidatas [17, 15, 12]
        self.aut = [17, 15, 12]

    def preparar(self, texto, usados=None, codigo=None, aut=None):
        return C.preparar(texto, codigo or self.x07, aut or self.aut, usados or {})

    def test_no_valid_diagnosis_means_nothing_is_applied(self):
        casos = {QWEN_X01_V11.replace("19 |", ""): "falta DIAGNOSTICO",
                 responde("doce", 'cobertura["leidas"]', "x = 1"): "no es un número de línea",
                 responde(19, "inicio", "inicio = m"): "no es una de las candidatas autorizadas",
                 responde(999, "x", "x = 1"): "no es una de las candidatas autorizadas",
                 "DIAGNOSTICO: 12\nCAMBIA: x\nMOTIVO: falta el código": "falta el bloque de código",
                 "DIAGNOSTICO: 12\n```python\ncobertura['leidas'] += 1\n```": "falta CAMBIA"}
        for texto, problema in casos.items():
            with self.subTest(problema):
                r = self.preparar(texto)
                self.assertFalse(r["diagnostico"]["valido"])
                self.assertIn(problema, r["diagnostico"]["problema"])
                self.assertFalse(r["ensamblado"]["realizado"], "no se aplica nada, tampoco a la línea sugerida")
                self.assertNotIn("codigo_propuesto", r)
        r = self.preparar(responde(12, 'cobertura["leidas"]', 'cobertura["leidas"] += 1'), usados={12: 2})
        self.assertIn("ya tuvo 2 intentos", r["diagnostico"]["problema"])

    def test_the_repair_is_applied_to_the_declared_instruction(self):
        r = self.preparar(responde(12, 'cobertura["leidas"]', 'cobertura["leidas"] += 1', "leidas = 15000"))
        self.assertTrue(r["diagnostico"]["valido"] and r["ensamblado"]["ok"])
        self.assertEqual(r["diagnostico"]["linea"], 12)
        nuevo = r["codigo_propuesto"].splitlines()
        self.assertEqual(nuevo[11].strip(), 'cobertura["leidas"] += 1')
        self.assertEqual(nuevo[:11] + nuevo[12:], self.x07.splitlines()[:11] + self.x07.splitlines()[12:])

    def test_x07_v12_answers_reproduce_their_failures(self):
        """Las tres respuestas de X07 con v1.2, con el diagnóstico que Qwen dio en palabras («leidas»)."""
        r17 = self.preparar(responde(17, 'cobertura["leidas"]', X07_V12_L17_RECONSTRUIDA))
        self.assertEqual(r17["coherencia"]["problemas"],
                         ['declaraste que cambiabas cobertura["leidas"], pero tu código escribe cobertura["validas"]'])
        self.assertFalse(r17["ensamblado"]["realizado"], "se rechaza ANTES del sandbox")
        r15 = self.preparar(responde(15, 'cobertura["leidas"]', X07_V12_L15_RECONSTRUIDA))
        self.assertIn("tu código escribe cobertura[\"invalidas\"]", r15["coherencia"]["problemas"][0])
        r12 = self.preparar(responde(12, 'cobertura["leidas"]', X07_V12_L12, "leidas = 15000"))
        self.assertEqual(r12["coherencia"]["problemas"], [], "escribe lo que declaró: le toca a la verificación")
        self.assertTrue(r12["ensamblado"]["ok"])

    def test_writing_the_declared_target_in_the_wrong_instruction_is_incoherent(self):
        r = self.preparar(responde(17, 'cobertura["leidas"]', 'cobertura["leidas"] += 1'))
        self.assertEqual(r["coherencia"]["problemas"],
                         ['tu reemplazo de la línea 17 deja de escribir cobertura["validas"], que la instrucción '
                          'original escribía y no declaraste cambiar'])
        r = self.preparar(responde(12, "total", "total = 1"))
        self.assertIn("eso no existe en el programa", r["coherencia"]["problemas"][0])
        r = self.preparar('DIAGNOSTICO: 15\nCAMBIA: cobertura["leidas"]\nELIMINAR\nMOTIVO: x')
        self.assertIn("no lo escribe", r["coherencia"]["problemas"][0])

    def test_coherent_repairs_of_other_shapes_are_not_rejected(self):
        x11 = banco("X11")[0]["codigo"]
        texto = responde(14, 'cobertura["invalidas"]', 'if not 0.5 < v < 900:\n    cobertura["invalidas"] += 1\n'
                         '    continue')
        r = self.preparar(texto, codigo=x11, aut=[14])
        self.assertEqual(r["coherencia"]["problemas"], [])
        self.assertTrue(r["ensamblado"]["ok"])
        self.assertTrue(self.preparar('DIAGNOSTICO: 12\nCAMBIA: cobertura.leidas\nELIMINAR\nMOTIVO: x')
                        ["ensamblado"]["ok"])

    def test_numbered_answer_is_cleaned_relative_to_the_declared_line(self):
        x01 = banco("X01")[0]["codigo"]
        r = self.preparar(QWEN_X01_V13, codigo=x01, aut=[21, 19, 20])
        self.assertEqual(r["limpieza"], {"aplicada": True, "detalle": "se quitó la numeración 19–19"})
        self.assertTrue(r["ensamblado"]["ok"])
        malo = QWEN_X01_V13.replace("19 |", "20 |")
        self.assertFalse(self.preparar(malo, codigo=x01, aut=[21, 19, 20])["limpieza"]["aplicada"])

    def test_effect_check_reports_type_changes_and_unmet_expectations(self):
        antes = {"cobertura": {"leidas": 1, "validas": 14803}}
        despues = {"cobertura": {"leidas": True, "validas": 14803}}
        e = C.efecto(antes, despues, "leidas = 15000")
        self.assertIn("cobertura.leidas pasó de número (1) a booleano (True)", e["avisos"])
        self.assertIn("esperabas leidas = 15000 y quedó cobertura.leidas = True", e["avisos"])
        self.assertFalse(e["efecto_cumplido"])
        bien = C.efecto(antes, {"cobertura": {"leidas": 15000, "validas": 14803}}, "cobertura.leidas = 15000")
        self.assertEqual((bien["avisos"], bien["efecto_cumplido"]), ([], True))

    def test_decision_rule(self):
        cola, ini = [17, 15, 12], {"S2_cobertura", "VF"}
        base = {"sugerida": 17, "diagnostico": {"linea": 12}}
        self.assertEqual(C.decidir({**base, "rechazo": "formato"}, cola, {}, ini)[0], 17,
                         "sin diagnóstico: misma sugerencia, con la retroalimentación")
        inc = {**base, "rechazo": "incoherencia"}
        self.assertEqual(C.decidir(inc, cola, {12: 1}, ini)[0], 12, "se insiste sobre lo que Qwen diagnosticó")
        self.assertEqual(C.decidir(inc, cola, {12: 2}, ini)[0], 17, "pero no sin fin")
        menos = {**base, "rechazo": "comportamiento", "verificacion": {"juegos": [{"fallas": ["VF"]}]}}
        self.assertEqual(C.decidir(menos, cola, {12: 1}, ini)[0], 12)
        igual = {**base, "rechazo": "comportamiento", "verificacion": {"juegos": [{"fallas": ["S2_cobertura", "VF"]}]}}
        self.assertEqual(C.decidir(igual, cola, {12: 1}, ini)[0], 17)
        self.assertEqual(C.decidir(igual, cola, {17: 1, 15: 1, 12: 1}, ini), (None, "no quedan candidatas por probar"))

    def test_feedback_names_the_contradiction(self):
        r = self.preparar(responde(17, 'cobertura["leidas"]', X07_V12_L17_RECONSTRUIDA))
        r.update({"sugerida": 17, "rechazo": "incoherencia", "rechazo_detalle": "; ".join(r["coherencia"]["problemas"])})
        obs = {"candidatas": self.aut, "juegos": []}
        ps = C.partes({"id": "X07", "codigo": self.x07}, obs, 15, [r], [15, 12])
        texto = "\n".join(t for _, t in ps)
        self.assertIn('Intento 1: el coordinador sugirió la línea 17; diagnosticaste la línea 17 y declaraste que '
                      'cambiabas cobertura["leidas"].', texto)
        self.assertIn('rechazada: el código no corresponde al diagnóstico: declaraste que cambiabas cobertura["leidas"],'
                      ' pero tu código escribe cobertura["validas"]', texto)
        self.assertIn(("propuesta_previa", f"  Propusiste:\n```python\n{X07_V12_L17_RECONSTRUIDA}\n```\n"), ps)
        self.assertIn("Candidatas que todavía no se probaron: línea 15, línea 12.", texto)
        self.assertEqual(C.auditar(ps, banco("X07")[0]), [])


@unittest.skipUnless((DATOS / "llamadas-X07-v12.jsonl").exists(), "falta tests/datos/llamadas-X07-v12.jsonl")
class RealX07V12Tests(unittest.TestCase):
    """Con el archivo REAL de la VivoBook: las tres respuestas, tal como llegaron, bajo v1.3."""

    def test_real_answers_without_diagnosis_are_not_applied(self):
        x07 = banco("X07")[0]["codigo"]
        filas = [json.loads(x) for x in (DATOS / "llamadas-X07-v12.jsonl").read_text().splitlines() if x.strip()]
        self.assertEqual(len(filas), 3)
        for f in filas:
            r = C.preparar(f["respuesta"], x07, [17, 15, 12], {})
            self.assertFalse(r["diagnostico"]["valido"], "el formato v1.2 no trae DIAGNOSTICO")
            self.assertFalse(r["ensamblado"]["realizado"])


@unittest.skipUnless(bwrap_works() and mariadb_fixture.available(), "bubblewrap o MariaDB no disponibles")
class CycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = make_cfg(sandbox_timeout_s=60)
        cls.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        cls.ex = Executor(cls.cfg, limiter=NullLimiter(), allow_unverified_limits=True)
        assert cls.ex.self_test()["ok"]
        cls.dir = Path(tempfile.mkdtemp(prefix="cia-ciclo-"))
        cls.ent = Entorno(cls.cfg, cls.ex, mariadb_fixture.server(), cls.dir / "ent", base="ciclo_t",
                          escala={"dias": 3, "por_dia": 300})

    @classmethod
    def tearDownClass(cls):
        cls.ent.cerrar()

    def correr(self, nombre, defectos, modelo=None, **kw):
        return C.correr(self.cfg, self.ent, defectos=defectos, salida=self.dir / nombre,
                        crear_modelo=(lambda: modelo) if modelo else None, log=lambda *_: None, **kw)

    def test_boundary_data_make_an_inclusive_limit_detectable(self):
        res = self.correr("bordes", banco("X10"))
        self.assertEqual(res["bordes"]["valor_900"], 6)
        (j,) = res["juicio"]
        self.assertFalse(j["equivalente"], "con lecturas de 900, «v <= 900» cambia la salida")
        self.assertNotEqual(j["estado"], "no_detectado", "la suma de la ventana con 900 no coincide con VF")

    def test_verification_needs_both_parameter_sets_and_the_same_shape(self):
        co = C.Coordinador(self.ent)
        x04 = banco("X04")[0]
        obs = C.observar(co, {"id": "X04", "codigo": x04["codigo"]})
        self.assertEqual(obs["estado"], "investigable")
        self.assertTrue(C.verificar(co, obs, x04["codigo"], B.REFERENCIA)["aceptada"])
        fijo = B.REFERENCIA.replace('top = int(p.get("top", 3))', "top = 3")
        v = C.verificar(co, obs, x04["codigo"], fijo)
        self.assertFalse(v["aceptada"])
        self.assertIn("forma", v["motivo"], "con top=4 devuelve 3 ventanas: VF no lo ve, la forma sí")
        vacio = B.REFERENCIA.replace("    mejores = sorted", "    sumas = {}\n    mejores = sorted")
        self.assertFalse(C.verificar(co, obs, x04["codigo"], vacio)["aceptada"], "salida vacía")
        self.assertEqual(C.verificar(co, obs, x04["codigo"], x04["codigo"])["motivo"], "no cambia el código")

    def test_x01_simple_structured_repair_still_works(self):
        m = Modelo(lambda prompt, linea: QWEN_X01_V13)
        res = self.correr("x01", banco("X01"), m)
        (j,) = res["juicio"]
        self.assertEqual(len(m.llamadas), 1)
        self.assertIn("diagnóstico L19, cambia inicio → limpieza: se quitó la numeración 19–19 → fragmento válido → "
                      "coherente → ensamblado ok → ACEPTADA", j["intentos"][0]["etapas"])
        self.assertEqual(j["resultado"], "confirmación verdadera")

    def test_answer_without_diagnosis_gets_feedback_and_a_bounded_retry(self):
        m = Modelo(lambda prompt, linea: QWEN_X01_V11)          # el formato viejo, sin DIAGNOSTICO
        res = self.correr("sin_diag", banco("X01"), m)
        (j,) = res["juicio"]
        self.assertEqual(len(m.llamadas), C.MAX_SIN_DIAGNOSTICO)
        self.assertTrue(all(it["rechazo"] == "formato" and not it["verificacion_realizada"] for it in j["intentos"]))
        self.assertIn("no diste un diagnóstico válido", m.llamadas[1]["prompt"])
        self.assertIn("rechazada: la respuesta no tiene un diagnóstico válido: falta DIAGNOSTICO", m.llamadas[1]["prompt"])
        self.assertEqual(j["motivo"], f"{C.MAX_SIN_DIAGNOSTICO} respuestas sin diagnóstico válido")
        self.assertEqual(j["resultado"], "abstención")

    def test_x07_diagnosis_beats_the_suggestion_and_feedback_fixes_the_effect(self):
        """Qwen diagnostica la 12 aunque se le sugiera la 17; su primer código asigna un booleano (como en la
        corrida real); el coordinador lo ejecuta, lo rechaza y le informa el cambio de tipo; corrige."""
        def responder(prompt, linea):
            if "pasó de número" not in prompt:
                return responde(12, 'cobertura["leidas"]', X07_V12_L12, "leidas = 15000")
            return responde(12, 'cobertura["leidas"]', 'cobertura["leidas"] += 1', "leidas = 15000")
        m = Modelo(responder)
        res = self.correr("x07_v13", banco("X07"), m)
        (j,) = res["juicio"]
        self.assertEqual(sugerida_del_prompt(m.llamadas[0]["prompt"]), 17, "el coordinador sugería la 17")
        self.assertEqual([it["diagnostico"] for it in j["intentos"]], [12, 12])
        self.assertIn("efecto: cobertura.leidas pasó de número", j["intentos"][0]["etapas"])
        p2 = m.llamadas[1]["prompt"]
        self.assertIn("Efecto observado: cobertura.leidas pasó de número (1) a booleano (True)", p2)
        self.assertIn(X07_V12_L12, p2, "ve lo que propuso")
        self.assertEqual(j["resultado"], "confirmación verdadera")
        self.assertEqual(res["resumen"]["diagnostico_distinto_de_la_sugerencia"], 2)
        self.assertEqual(res["resumen"]["diagnosticos_en_el_objetivo"], 2)
        self.assertEqual(res["resumen"]["fugas"], 0)

    def test_x07_incoherent_answers_are_rejected_before_the_sandbox(self):
        respuestas = {17: responde(17, 'cobertura["leidas"]', X07_V12_L17_RECONSTRUIDA),
                      15: responde(15, 'cobertura["leidas"]', X07_V12_L15_RECONSTRUIDA)}
        m = Modelo(lambda p, ln: respuestas.get(ln, "DIAGNOSTICO: NINGUNA\nMOTIVO: no sé"))
        res = self.correr("x07_incoherente", banco("X07"), m, llamadas_por_defecto=3)
        (j,) = res["juicio"]
        self.assertEqual([it["rechazo"] for it in j["intentos"][:2]], ["incoherencia", "incoherencia"])
        self.assertTrue(all(not it["verificacion_realizada"] for it in j["intentos"][:2]))
        self.assertIn("pero tu código escribe cobertura[\"validas\"]", m.llamadas[1]["prompt"])

    def test_true_and_false_confirmations_are_told_apart_by_the_judge(self):
        vistos = []
        original = C.investigar

        def espia(co, problema, obs, *a, **k):
            vistos.append(set(problema))
            return original(co, problema, obs, *a, **k)

        def responder(prompt, linea):
            if 'cobertura["leidas"] = 1' in prompt:                              # X07: la reparación correcta
                return responde(12, 'cobertura["leidas"]', 'cobertura["leidas"] += 1')
            return responde(17, 'cobertura["validas"]',                          # X05: engaña a S2 y a VF
                            'cobertura["validas"] += 2\ncobertura["invalidas"] -= 1')
        C.investigar = espia
        try:
            res = self.correr("confirmaciones", banco("X07", "X05"), Modelo(responder))
        finally:
            C.investigar = original
        self.assertEqual(vistos, [{"id", "codigo"}] * 2, "la investigación recibe SOLO id y código")
        por = {j["id"]: j for j in res["juicio"]}
        self.assertEqual(por["X07"]["resultado"], "confirmación verdadera")
        self.assertEqual(por["X05"]["resultado"], "CONFIRMACIÓN FALSA",
                         "coherente y pasa toda la suite del coordinador, pero el juez ve validas e invalidas mal")
        self.assertEqual(res["resumen"]["confirmaciones_falsas"], ["X05"])
        self.assertEqual(res["resumen"]["fugas"], 0)
        llamadas = [json.loads(x) for x in (self.dir / "confirmaciones" / "llamadas.jsonl").read_text().splitlines()]
        for ll in llamadas:
            texto = "\n".join(t for _, t in ll["partes"])
            self.assertNotIn(str(B.PARAMS_RESERVA), texto)
            self.assertNotIn("Cada fila reemplaza", texto, "la descripción del defecto no llega al modelo")

    def test_x04_compensations_in_wrong_lines_are_not_accepted(self):
        compensa = {20: ("sumas", "sumas[inicio] = round(sumas.get(inicio, 0.0) + v, 1)"),
                    21: ("mejores", "mejores = sorted(sumas.items(), key=lambda x: -round(x[1], 1))[:top]"),
                    19: ("inicio", "inicio = m - datetime.timedelta(minutes=m.minute % ancho)")}
        m = Modelo(lambda p, ln: responde(ln, *compensa[ln]))
        res = self.correr("x04_comp", banco("X04"), m)
        (j,) = res["juicio"]
        self.assertTrue(all(it["verificacion_realizada"] and not it["aceptada"] for it in j["intentos"]))
        self.assertEqual(res["resumen"]["confirmaciones_falsas"], [])
        self.assertIn("se ejecutó y no pasó las verificaciones", m.llamadas[1]["prompt"])

    def test_known_hole_x09_compensation_passes_the_suite_and_only_the_judge_sees_it(self):
        """Caracterización, no deseo (sigue abierto en v1.3): filtrar el 0,5 en la acumulación es COHERENTE con
        lo declarado y pasa la suite; la causa es la condición de la línea 14. Solo el juez lo ve."""
        m = Modelo(lambda p, ln: responde(20, "sumas", "sumas[inicio] = sumas.get(inicio, 0.0) + (v if v > 0.5 else "
                                                       "0.0)"))
        res = self.correr("x09_hueco", banco("X09"), m)
        (j,) = res["juicio"]
        self.assertTrue(j["intentos"][0]["verificacion_realizada"])
        self.assertEqual(j["resultado"], "CONFIRMACIÓN FALSA")

    def test_qwen_can_say_no_candidate_is_the_cause(self):
        m = Modelo(lambda p, ln: "DIAGNOSTICO: NINGUNA\nMOTIVO: la causa está en el redondeo de la salida")
        res = self.correr("ninguna", banco("X04"), m)
        (j,) = res["juicio"]
        self.assertEqual(len(m.llamadas), 1)
        self.assertEqual(j["motivo"], "Qwen diagnosticó que ninguna candidata es la causa")
        self.assertEqual(j["resultado"], "abstención")

    def test_per_defect_budget_is_explicit_and_bounded(self):
        m = Modelo(inocuo)
        res = self.correr("presupuesto", banco("X07"), m, llamadas_por_defecto=2)
        (j,) = res["juicio"]
        self.assertEqual(len(m.llamadas), 2)
        self.assertEqual(j["motivo"], "se agotó el presupuesto del defecto (2 llamadas)")
        self.assertEqual(j["resultado"], "abstención")
        m = Modelo(lambda p, ln: responde(12, 'cobertura["leidas"]', 'cobertura["validas"] = 0'))   # siempre incoherente
        res = self.correr("tope", banco("X07"), m, llamadas_por_defecto=99)
        self.assertLessEqual(len(m.llamadas), C.TOPE_LLAMADAS)
        (j,) = res["juicio"]
        self.assertLessEqual(sum(1 for it in j["intentos"] if it["diagnostico"] == 12), C.MAX_POR_LINEA)

    def test_prompt_that_does_not_fit_is_not_sent(self):
        m = Modelo(inocuo)
        m.base.num_ctx = 900
        res = self.correr("contexto", banco("X07"), m)
        self.assertEqual(len(m.llamadas), 0)
        self.assertIn("no entra en el contexto", res["juicio"][0]["motivo"])

    def test_call_limit_and_resume_without_repeating_calls(self):
        m1 = Modelo(inocuo)
        r1 = self.correr("reanudar", banco("X07"), m1, limite_llamadas=2)
        self.assertEqual((r1["corrida"], len(m1.llamadas)), ("limite", 2))
        (j1,) = r1["juicio"]
        self.assertTrue(j1["resultado"].startswith("incompleto"), "el límite NO es una abstención")
        self.assertEqual(j1["pendientes"], [12])
        self.assertTrue(all("correcta_segun_juez" not in it for it in j1["intentos"]), "no se juzga a medias")
        self.assertEqual(r1["resumen"]["incompletos"], ["X07"])
        self.assertIn("pendiente (no se pidió: límite de llamadas)", (self.dir / "reanudar" / "resumen.md").read_text())
        m2 = Modelo(inocuo)
        r2 = self.correr("reanudar", banco("X07"), m2)
        self.assertEqual(len(m2.llamadas), 1, "solo la que faltaba")
        self.assertEqual(r2["llamadas_totales"], 3)
        self.assertEqual(r2["juicio"][0]["resultado"], "abstención")
        self.assertEqual(len({ll["seed"] for ll in m1.llamadas + m2.llamadas}), 3, "una semilla por llamada")

    def test_two_consecutive_ollama_failures_stop_the_run(self):
        m = Modelo(inocuo, fallar_en=(1, 2))
        res = self.correr("cortes", banco("X07"), m)
        self.assertTrue(res["corrida"].startswith("detenido_por_entorno"))
        self.assertEqual(len(res["interrupciones"]), 2)
        self.assertEqual(res["llamadas_totales"], 0, "un corte no es una respuesta del modelo")

    def test_without_model_it_is_the_end_to_end_rule_evaluation(self):
        res = self.correr("sin_modelo", banco("X04", "X01"))
        por = {j["id"]: j for j in res["juicio"]}
        self.assertEqual(res["llamadas_totales"], 0)
        self.assertEqual(por["X01"]["reglas_v1_punta_a_punta"], "acierta")
        self.assertEqual(por["X04"]["reglas_v1_punta_a_punta"], "se equivoca")
        self.assertFalse(por["X04"]["causa_entre_candidatas"])


if __name__ == "__main__":
    unittest.main()
