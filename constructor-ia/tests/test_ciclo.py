"""Ciclo de investigación v1 (PILOTO.md, «Ciclo v1»): hipótesis → reparación → verificación → juez separado.

Los modelos simulados validan el CIRCUITO (aislamiento del juez, verificación en dos juegos de parámetros, forma de la
salida, confirmaciones falsas medidas, límite y reanudación, interrupciones), NO la capacidad de un modelo.
"""
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
REF = B.REFERENCIA.splitlines()


def banco(*ids):
    ok, problemas = DF.cargar(BANCO)
    assert not problemas, problemas
    return [d for d in ok if not ids or d["id"] in ids]


def hipotesis_del_prompt(prompt: str) -> int:
    return int(re.search(r"Instrucción de la hipótesis \(línea (\d+)", prompt).group(1))


class Modelo:
    """Responde con una función (id del defecto deducido del código, línea de la hipótesis) → texto."""

    def __init__(self, responder, fallar_en=()):
        self.responder, self.fallar_en = responder, set(fallar_en)
        self.nombre, self.system, self.base = "simulado", "", types.SimpleNamespace(num_ctx=8192)
        self.llamadas = []

    def pedir(self, tipo, prompt, *, json_out, cancel=None, corto=False, seed=None):
        self.llamadas.append({"seed": seed, "prompt": prompt})
        if len(self.llamadas) in self.fallar_en:
            return None, {"tipo": tipo, "resultado": "error", "error": "Conexión con Ollama interrumpida"}
        return self.responder(prompt, hipotesis_del_prompt(prompt)), {"tipo": tipo, "resultado": "completa",
                                                                      "reloj_ms": 1000}


def bloque(codigo):
    return f"```python\n{codigo}\n```\nMOTIVO: simulado"


# Respuesta REAL de Qwen en la VivoBook (commit 49a7e25, X01, hipótesis 19): copió el prefijo «19 |» de la vista
# numerada. El bloque es textual; el MOTIVO es un resumen del que dio Qwen.
QWEN_X01 = ('```python\n19 |         inicio = datetime.datetime.strptime(minuto, "%Y-%m-%d %H:%M") - '
            'datetime.timedelta(minutes=m.minute % ancho)\n```\n'
            'MOTIVO: el error es usar ancho + 1 en lugar de ancho.')


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

    def test_response_interpretation(self):
        self.assertEqual(C.interpretar(bloque("x = 1"))["fragmento"].strip(), "x = 1")
        self.assertEqual(C.interpretar("NO_ES_ESTA\nMOTIVO: la causa está antes")["tipo"], "no_es_esta")
        self.assertEqual(C.interpretar("ELIMINAR\nMOTIVO: sobra")["tipo"], "eliminar")
        self.assertEqual(C.interpretar("no sé")["tipo"], "ilegible")
        self.assertEqual(C.interpretar(None)["tipo"], "ilegible")

    def test_leak_audit(self):
        x04 = banco("X04")[0]
        self.assertEqual(C.auditar([("coordinador", x04["codigo"])], x04), [])
        self.assertTrue(C.auditar([("coordinador", REF[21])], x04), "la línea de la referencia que el defecto cambió")
        self.assertTrue(C.auditar([("evidencia", x04["descripcion"])], x04))
        self.assertTrue(C.auditar([("evidencia", f"Con los parámetros {B.PARAMS_RESERVA}")], x04))

    def test_prompt_shows_hypothesis_not_judge_information(self):
        x04 = banco("X04")[0]
        obs = {"candidatas": [21, 19, 20],
               "juegos": [{"params": p, "salida": {"resultado": {}}, "fallas": [{"id": "VF"}],
                           "texto": "- VF: algo (campos: suma)"} for p in C.PARAMS_COORDINADOR]}
        ps = C.partes({"id": "X04", "codigo": x04["codigo"]}, obs, 20)
        texto = "\n".join(t for _, t in ps)
        self.assertIn("TODAVÍA NO COMPROBADA", texto)
        self.assertIn(">>  20 |", texto)
        self.assertIn(str(C.PARAMS_COORDINADOR[1]), texto)
        self.assertNotIn("[modelo]", texto)
        self.assertEqual(C.auditar(ps, x04), [])
        self.assertIn("SIN números de línea", texto)
        self.assertIn("```python\nsumas[inicio] = sumas.get(inicio, 0.0) + v\n```", texto,
                      "la instrucción de la hipótesis también se muestra limpia, sin numeración")


class NumberedFragmentTests(unittest.TestCase):
    """Regresiones de la interfaz: el prefijo «N |» se quita SOLO en el caso estricto y verificable."""

    def setUp(self):
        self.x01 = banco("X01")[0]
        self.u = C.P.unidad_en(self.x01["codigo"], 19)

    def preparar(self, texto, u=None):
        return C.preparar(texto, self.x01["codigo"], u or self.u)

    def test_qwens_real_answer_is_cleaned_and_assembled(self):
        r = self.preparar(QWEN_X01)
        self.assertEqual(r["propuesta"]["tipo"], "reemplazar")
        self.assertTrue(r["propuesta"]["fragmento_crudo"].startswith("19 |"), "la propuesta queda tal como llegó")
        self.assertEqual(r["limpieza"], {"aplicada": True, "detalle": "se quitó la numeración 19–19"})
        self.assertTrue(r["fragmento"]["valido"])
        self.assertTrue(r["ensamblado"]["ok"])
        self.assertEqual(r["codigo_propuesto"].splitlines()[18].strip(),
                         'inicio = datetime.datetime.strptime(minuto, "%Y-%m-%d %H:%M") - '
                         'datetime.timedelta(minutes=m.minute % ancho)')
        antes = r["codigo_propuesto"].splitlines()
        self.assertEqual(antes[:18] + antes[19:], self.x01["codigo"].splitlines()[:18]
                         + self.x01["codigo"].splitlines()[19:], "solo cambia la línea 19")

    def test_cleaning_is_strict(self):
        casos = {
            "otra numeración": "```python\n20 |         inicio = m\n```",
            "prefijo en algunas líneas": "```python\n19 |   inicio = m\nx = 1\n```",
            "no compila ni sin prefijo": "```python\n19 | inicio = (\n```",
            "numeración no consecutiva": "```python\n19 | inicio = m\n21 | x = 1\n```",
        }
        for nombre, texto in casos.items():
            with self.subTest(nombre):
                r = self.preparar(texto)
                self.assertFalse(r["limpieza"]["aplicada"])
                self.assertIn("no se limpia" if nombre != "no compila ni sin prefijo" else "tampoco compila",
                              r["limpieza"]["detalle"])
                self.assertFalse(r["fragmento"]["valido"])
                self.assertFalse(r["ensamblado"]["realizado"], "un fragmento inválido no se ensambla")
                self.assertNotIn("codigo_propuesto", r)

    def test_valid_python_that_looks_numbered_is_left_alone(self):
        r = self.preparar("```python\ninicio = 19 | 2\n```")
        self.assertFalse(r["limpieza"]["aplicada"])
        self.assertTrue(r["fragmento"]["valido"])
        r = self.preparar("```python\n19 | 2\n```")          # una expresión válida (OR de bits): no se toca
        self.assertEqual((r["limpieza"]["aplicada"], r["fragmento"]["valido"]), (False, True))

    def test_numbered_multiline_keeps_relative_indentation(self):
        x11 = banco("X11")[0]
        u = C.P.unidad_en(x11["codigo"], 14)                       # el if (completo: encabezado y cuerpo)
        texto = ("```python\n14 |         if not 0.5 < v < 900:\n15 |             cobertura[\"invalidas\"] += 1\n"
                 "16 |             continue\n```")
        r = C.preparar(texto, x11["codigo"], u)
        self.assertTrue(r["limpieza"]["aplicada"])
        self.assertTrue(r["ensamblado"]["ok"])
        self.assertIn("            continue", r["codigo_propuesto"])

    def test_every_stage_is_recorded_when_nothing_is_proposed(self):
        for texto, tipo in (("NO_ES_ESTA\nMOTIVO: no", "no_es_esta"), ("no sé", "ilegible")):
            r = self.preparar(texto)
            self.assertEqual(r["propuesta"]["tipo"], tipo)
            self.assertFalse(r["ensamblado"]["realizado"])
            r["verificacion"] = {"realizada": False, "aceptada": None, "motivo": r["ensamblado"]["detalle"]}
            self.assertIn("verificación no realizada", C.etapas(r))

    def test_a_folder_from_another_version_is_not_resumed(self):
        d = Path(tempfile.mkdtemp())
        (d / "llamadas.jsonl").write_text('{"clave": "X01|h0|L19", "respuesta": "x"}\n')
        with self.assertRaisesRegex(RuntimeError, "ciclo v1»"):
            C.correr(None, None, defectos=[], salida=d)
        self.assertEqual((d / "llamadas.jsonl").read_text(), '{"clave": "X01|h0|L19", "respuesta": "x"}\n',
                         "la corrida original no se toca")
        self.assertFalse((d / "version.json").exists())
        e = Path(tempfile.mkdtemp())
        (e / "version.json").write_text('{"version": "ciclo v1.1"}')
        with self.assertRaisesRegex(RuntimeError, "ciclo v1.1»"):
            C.correr(None, None, defectos=[], salida=e)

    def test_cli_rejects_an_unbounded_budget_before_doing_anything(self):
        from constructor_ia import cli
        ns = types.SimpleNamespace(sin_modelo=False, limite_llamadas=3, completo=False, llamadas_por_defecto=7,
                                   reanudar=None, defectos=str(BANCO), ids="X07", yes=True)
        self.assertEqual(cli.cmd_lab_cycle(ns), 2)


class RedirectionAndFeedbackTests(unittest.TestCase):
    """v1.2: Qwen puede cuestionar la hipótesis; el coordinador valida y decide; cada intento informa al siguiente."""

    def setUp(self):
        self.x07 = banco("X07")[0]["codigo"]          # candidatas [17, 15, 12]
        self.aut = [17, 15, 12]

    def test_redirection_is_validated_not_obeyed(self):
        r = lambda t, usados=None: C.redireccion(t, self.x07, self.aut, 17, usados or {})   # noqa: E731
        self.assertEqual(r("NO_ES_ESTA\nPROPONGO_LINEA: 12\nMOTIVO: se reinicia el contador")["linea"], 12)
        casos = {"PROPONGO_LINEA: doce": "no es un número", "PROPONGO_LINEA: 999": "no existe",
                 "PROPONGO_LINEA: 2": "no es una instrucción editable",
                 "PROPONGO_LINEA: 19": "no es una de las candidatas autorizadas",
                 "PROPONGO_LINEA: 17": "es la misma hipótesis"}
        for texto, motivo in casos.items():
            with self.subTest(texto):
                red = r(texto)
                self.assertFalse(red["valida"])
                self.assertIn(motivo, red["motivo"])
        self.assertIn("ya tuvo 2 intentos", r("PROPONGO_LINEA: 12", {12: 2})["motivo"])
        self.assertIsNone(r("NO_ES_ESTA\nMOTIVO: no sé"))

    def test_decision_rule_uses_the_result_of_each_attempt(self):
        cola, ini = [17, 15, 12], {"S2_cobertura", "VF"}
        base = {"hipotesis": 17, "rechazo": None}
        red = {**base, "redireccion": {"valida": True, "linea": 12}}
        self.assertEqual(C.decidir(red, cola, {17: 1}, ini)[0], 12)
        rev = {**base, "rechazo": "revision"}
        self.assertEqual(C.decidir(rev, cola, {17: 1}, ini), (17, "reintento: la hipótesis no llegó a probarse "
                                                                "(la revisión estática del sandbox lo rechazó)"))
        self.assertEqual(C.decidir(rev, cola, {17: 2}, ini)[0], 15, "una hipótesis no se reintenta sin fin")
        menos = {**base, "rechazo": "comportamiento", "verificacion": {"juegos": [{"fallas": ["VF"]}]}}
        self.assertEqual(C.decidir(menos, cola, {17: 1}, ini)[0], 17, "redujo las fallas: vale reintentar")
        igual = {**base, "rechazo": "comportamiento", "verificacion": {"juegos": [{"fallas": ["S2_cobertura", "VF"]}]}}
        self.assertEqual(C.decidir(igual, cola, {17: 1}, ini)[0], 15)
        self.assertEqual(C.decidir(igual, cola, {17: 1, 15: 1, 12: 1}, ini), (None, "no quedan hipótesis por probar"))

    def test_feedback_says_what_was_tried_proposed_and_why_it_failed(self):
        ref = 'cobertura["leidas"] += 1'
        intento = {"hipotesis": 12, "propuesta": {"tipo": "reemplazar", "fragmento_crudo": ref},
                   "rechazo": "comportamiento", "rechazo_detalle": "con los parámetros {'ancho_min': 20, 'top': 4}: "
                   "S2_cobertura: aritmética"}
        obs = {"candidatas": self.aut, "juegos": []}
        ps = C.partes({"id": "X07", "codigo": self.x07}, obs, 15, [intento], [17])
        texto = "\n".join(t for _, t in ps)
        self.assertIn('Intento 1: hipótesis línea 12: `cobertura["leidas"] = 1`', texto)
        self.assertIn("rechazada: se ejecutó y no pasó las verificaciones: con los parámetros", texto)
        self.assertIn("Hipótesis pendientes (sin probar): línea 17.", texto)
        self.assertIn("PROPONGO_LINEA: <número>, usando SOLO una de estas líneas candidatas: 17, 15, 12", texto)
        self.assertIn(("propuesta_previa", f"  Propusiste:\n```python\n{ref}\n```\n"), ps)
        self.assertEqual(C.auditar(ps, banco("X07")[0]), [],
                         "la línea de la referencia vino de Qwen, no del juez: no es una fuga")
        corto = "\n".join(t for _, t in C.partes({"id": "X07", "codigo": self.x07}, obs, 15, [intento], [17],
                                                  compacto=True))
        self.assertNotIn(ref, corto)
        self.assertIn("Propusiste un reemplazo", corto)


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

    def test_true_and_false_confirmations_are_told_apart_by_the_judge(self):
        vistos = []
        original = C.investigar

        def espia(co, problema, obs, *a, **k):
            vistos.append(set(problema))
            return original(co, problema, obs, *a, **k)

        def responder(prompt, linea):
            if 'cobertura["leidas"] = 1' in prompt and linea == 12:        # X07: la reparación correcta
                return bloque('cobertura["leidas"] += 1')
            if 'cobertura["validas"] = 1' in prompt and linea == 17:       # X05: engaña a S2 y a VF
                return bloque('cobertura["validas"] += 2\ncobertura["invalidas"] -= 1')
            return "NO_ES_ESTA\nMOTIVO: no parece"
        C.investigar = espia
        try:
            res = self.correr("confirmaciones", banco("X07", "X05"), Modelo(responder))
        finally:
            C.investigar = original
        self.assertEqual(vistos, [{"id", "codigo"}] * 2, "la investigación recibe SOLO id y código")
        por = {j["id"]: j for j in res["juicio"]}
        self.assertEqual(por["X07"]["resultado"], "confirmación verdadera")
        self.assertEqual(por["X05"]["resultado"], "CONFIRMACIÓN FALSA",
                         "pasa toda la suite del coordinador, pero el juez ve validas e invalidas mal")
        self.assertEqual(res["resumen"]["confirmaciones_falsas"], ["X05"])
        self.assertEqual(res["resumen"]["fugas"], 0)
        llamadas = [json.loads(x) for x in (self.dir / "confirmaciones" / "llamadas.jsonl").read_text().splitlines()]
        for ll in llamadas:
            texto = "\n".join(t for _, t in ll["partes"])
            self.assertNotIn(str(B.PARAMS_RESERVA), texto)
            self.assertNotIn("Cada fila reemplaza", texto, "la descripción del defecto no llega al modelo")
        self.assertIn("CONFIRMACIÓN FALSA", (self.dir / "confirmaciones" / "resumen.md").read_text())

    def test_qwens_real_answer_now_reaches_verification_and_the_judge(self):
        m = Modelo(lambda prompt, linea: QWEN_X01 if linea == 19 else "NO_ES_ESTA")
        res = self.correr("qwen_x01", banco("X01"), m)
        (j,) = res["juicio"]
        self.assertEqual(len(m.llamadas), 1, "R4 pone la línea 19 primero")
        self.assertIn("limpieza: se quitó la numeración 19–19 → fragmento válido → ensamblado ok → ACEPTADA",
                      j["intentos"][0]["etapas"])
        self.assertEqual(j["resultado"], "confirmación verdadera")

    def test_x07_redirect_feedback_and_repair(self):
        """El caso real: Qwen sabía que era la línea 12. Ahora lo dice, el coordinador lo valida, la primera
        reparación falla en la revisión y la segunda, con esa retroalimentación, se confirma."""
        def responder(prompt, linea):
            if linea == 17:
                return "NO_ES_ESTA\nPROPONGO_LINEA: 12\nMOTIVO: se reinicia el contador de lecturas"
            if linea == 12 and "acceso a atributo interno" not in prompt:
                return bloque('cobertura["leidas"] = r.filas.__len__()')
            if linea == 12:
                return bloque('cobertura["leidas"] += 1')
            return "NO_ES_ESTA"
        m = Modelo(responder)
        res = self.correr("x07_v12", banco("X07"), m)
        (j,) = res["juicio"]
        self.assertEqual([hipotesis_del_prompt(x["prompt"]) for x in m.llamadas], [17, 12, 12])
        self.assertEqual(j["resultado"], "confirmación verdadera")
        self.assertIn("propuso la línea 12: tomada", j["intentos"][0]["etapas"])
        self.assertIn("siguiente: línea 12 (redirección de Qwen validada por el coordinador)", j["intentos"][0]["etapas"])
        self.assertIn("revisión estática", j["intentos"][1]["etapas"])
        self.assertIn("siguiente: línea 12 (reintento: la hipótesis no llegó a probarse", j["intentos"][1]["etapas"])
        p2, p3 = m.llamadas[1]["prompt"], m.llamadas[2]["prompt"]
        self.assertIn("propusiste la línea 12: el coordinador la tomó como próxima hipótesis", p2)
        self.assertIn('cobertura["leidas"] = r.filas.__len__()', p3, "ve lo que propuso")
        self.assertIn("la revisión estática del sandbox lo rechazó", p3, "y por qué falló")
        self.assertIn("Hipótesis pendientes (sin probar): línea 15.", p3)
        self.assertEqual(res["resumen"]["redirecciones"], {"tomada": 1})
        self.assertEqual(res["resumen"]["fugas"], 0)

    def test_invalid_redirects_fall_back_to_the_coordinators_queue(self):
        def responder(prompt, linea):
            return {17: "NO_ES_ESTA\nPROPONGO_LINEA: 19", 15: "NO_ES_ESTA\nPROPONGO_LINEA: 15"}.get(linea, "NO_ES_ESTA")
        m = Modelo(responder)
        res = self.correr("redir_invalida", banco("X07"), m)
        (j,) = res["juicio"]
        self.assertEqual([hipotesis_del_prompt(x["prompt"]) for x in m.llamadas], [17, 15, 12],
                         "una línea no autorizada o repetida no se prueba")
        self.assertIn("NO tomada (no es una de las candidatas autorizadas", j["intentos"][0]["etapas"])
        self.assertIn("NO tomada (es la misma hipótesis", j["intentos"][1]["etapas"])
        self.assertEqual(res["resumen"]["redirecciones"], {"no tomada": 2})
        self.assertEqual(j["resultado"], "abstención")

    def test_per_defect_budget_is_explicit_and_bounded(self):
        m = Modelo(lambda p, ln: "NO_ES_ESTA")
        res = self.correr("presupuesto", banco("X07"), m, llamadas_por_defecto=2)
        (j,) = res["juicio"]
        self.assertEqual(len(m.llamadas), 2)
        self.assertEqual(j["motivo"], "se agotó el presupuesto del defecto (2 llamadas)")
        self.assertEqual(j["resultado"], "abstención", "agotar SU presupuesto termina la investigación")
        m = Modelo(lambda p, ln: "NO_ES_ESTA\nPROPONGO_LINEA: 12" if ln != 12 else bloque("x = (1"))
        res = self.correr("tope", banco("X07"), m, llamadas_por_defecto=99)
        self.assertLessEqual(len(m.llamadas), C.TOPE_LLAMADAS)
        self.assertLessEqual(sum(1 for x in m.llamadas if hipotesis_del_prompt(x["prompt"]) == 12), C.MAX_POR_LINEA)

    def test_prompt_that_does_not_fit_is_not_sent(self):
        m = Modelo(lambda p, ln: "NO_ES_ESTA")
        m.base.num_ctx = 900
        res = self.correr("contexto", banco("X07"), m)
        self.assertEqual(len(m.llamadas), 0)
        self.assertIn("no entra en el contexto", res["juicio"][0]["motivo"])

    def test_x04_compensations_in_wrong_lines_are_not_accepted(self):
        compensa = {20: "sumas[inicio] = round(sumas.get(inicio, 0.0) + v, 1)",
                    21: "mejores = sorted(sumas.items(), key=lambda x: -round(x[1], 1))[:top]",
                    19: "inicio = m - datetime.timedelta(minutes=m.minute % ancho)"}
        m = Modelo(lambda p, ln: bloque(compensa.get(ln, "x = 1")))
        res = self.correr("x04_comp", banco("X04"), m)
        (j,) = res["juicio"]
        self.assertTrue(all(it["verificacion_realizada"] and not it["aceptada"] for it in j["intentos"]))
        self.assertEqual(res["resumen"]["confirmaciones_falsas"], [])
        self.assertIn("se ejecutó y no pasó las verificaciones", m.llamadas[1]["prompt"])

    def test_known_hole_x09_compensation_passes_the_suite_and_only_the_judge_sees_it(self):
        """Caracterización, no deseo: filtrar el 0,5 en la acumulación (línea 20) pasa S1–S3 y VF en los dos
        juegos, aunque la causa es la condición de la línea 14. La suite no controla el reparto válidas/inválidas
        (como en X05). El coordinador la acepta por SU verificación, no porque Qwen lo diga; el juez la marca."""
        m = Modelo(lambda p, ln: bloque("sumas[inicio] = sumas.get(inicio, 0.0) + (v if v > 0.5 else 0.0)")
                   if ln == 20 else "NO_ES_ESTA")
        res = self.correr("x09_hueco", banco("X09"), m)
        (j,) = res["juicio"]
        self.assertTrue(j["intentos"][0]["verificacion_realizada"])
        self.assertEqual(j["resultado"], "CONFIRMACIÓN FALSA")
        self.assertEqual(res["resumen"]["confirmaciones_falsas"], ["X09"])

    def test_call_limit_and_resume_without_repeating_calls(self):
        nunca = lambda prompt, linea: "NO_ES_ESTA"                          # noqa: E731
        m1 = Modelo(nunca)
        r1 = self.correr("reanudar", banco("X07"), m1, limite_llamadas=2)
        self.assertEqual((r1["corrida"], len(m1.llamadas)), ("limite", 2))
        (j1,) = r1["juicio"]
        self.assertTrue(j1["resultado"].startswith("incompleto"), "el límite NO es una abstención")
        self.assertEqual(j1["pendientes"], [12])
        self.assertTrue(all("correcta_segun_juez" not in it for it in j1["intentos"]), "no se juzga a medias")
        self.assertEqual(r1["resumen"]["incompletos"], ["X07"])
        self.assertNotIn("abstención", r1["resumen"]["resultados"])
        self.assertIn("pendiente (no se pidió: límite de llamadas)", (self.dir / "reanudar" / "resumen.md").read_text())
        m2 = Modelo(nunca)
        r2 = self.correr("reanudar", banco("X07"), m2)
        self.assertEqual(len(m2.llamadas), 1, "solo la hipótesis que faltaba")
        self.assertEqual(r2["llamadas_totales"], 3)
        self.assertEqual(r2["juicio"][0]["resultado"], "abstención")
        self.assertEqual(len({ll["seed"] for ll in m1.llamadas + m2.llamadas}), 3, "una semilla por hipótesis")

    def test_two_consecutive_ollama_failures_stop_the_run(self):
        m = Modelo(lambda p, ln: "NO_ES_ESTA", fallar_en=(1, 2))
        res = self.correr("cortes", banco("X07"), m)
        self.assertTrue(res["corrida"].startswith("detenido_por_entorno"))
        self.assertEqual(len(res["interrupciones"]), 2)
        self.assertEqual(res["llamadas_totales"], 0, "un corte no es una respuesta del modelo")

    def test_without_model_it_is_the_end_to_end_rule_evaluation(self):
        res = self.correr("sin_modelo", banco("X04", "X01"))
        por = {j["id"]: j for j in res["juicio"]}
        self.assertEqual(res["llamadas_totales"], 0)
        self.assertEqual(por["X01"]["reglas_v1_punta_a_punta"], "acierta")
        self.assertEqual(por["X04"]["reglas_v1_punta_a_punta"], "se equivoca",
                         "el error que el filtro de E0 escondía queda a la vista")
        self.assertFalse(por["X04"]["causa_entre_candidatas"])


if __name__ == "__main__":
    unittest.main()
