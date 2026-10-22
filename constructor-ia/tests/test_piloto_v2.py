"""Protocolo v2 del piloto (PILOTO.md, Enmienda 3). Casos de regresión tomados del ensayo técnico v1 en la VivoBook:

* B*: Qwen respondió dos veces «CANDIDATO: 21» con un bloque if completo (la ambigüedad índice/línea lo hacía
  ilegible en v1; en v2 se interpreta, se ensambla y el umbral lo rechaza: el parche no corregía);
* B: el mismo parche rechazado se repetía porque el siguiente prompt era idéntico;
* D: después de una corrección aceptada (juez: corregido), V1 seguía fallando y obligaba a otra llamada;
* entorno: Ollama murió por OOM y la conexión cortada se registró como falla del modelo.

Los guiones de modelo validan el circuito, NO la capacidad de un modelo.
"""
import json
import tempfile
import types
import unittest
from pathlib import Path

from constructor_ia.laboratorio.piloto import banco as B
from constructor_ia.laboratorio.piloto import informe as PI
from constructor_ia.laboratorio.piloto import parches as P
from constructor_ia.laboratorio.piloto import piloto as PL
from constructor_ia.laboratorio.piloto import v2 as V2
from constructor_ia.laboratorio.piloto.entorno import Entorno
from constructor_ia.laboratorio.protocolo import Modelo
from constructor_ia.ollama import OllamaClient
from constructor_ia.sandbox.executor import Executor
from constructor_ia.sandbox.limiter import NullLimiter

from . import mariadb_fixture
from .fake_ollama import FakeOllama
from .helpers import bwrap_works, make_cfg
from .test_piloto import VERIFICADOR, Guion, arreglar

# Respuesta textual de Qwen3 4B en B* (ensayo v1, recuperada de prompts.jsonl).
QWEN_BX = ('CANDIDATO: 21\nACCION: reemplazar\n```python\n'
           'if "cobertura" not in cobertura or cobertura["leidas"] == 0:\n'
           '    cobertura = {"leidas": 0, "validas": 0, "invalidas": 0}\n```')

# Un verificador MÁS EXIGENTE que el contrato (cuenta como «leídas» solo los valores plausibles): falla también con
# la referencia, es decir, es una falsa alarma. Reproduce la situación de D en el ensayo.
VERIF_FALSA_ALARMA = '''
def medir(db, p):
    s = p["salida"]
    plausibles = int(db.consultar("SELECT COUNT(*) FROM lecturas WHERE valor > 0.5 AND valor < 900").filas[0][0])
    ok = s["cobertura"].get("leidas") == plausibles
    return {"resultado": {"ok": ok, "campos": [] if ok else ["leidas"],
                          "contraejemplos": [] if ok else [{"salida": s["cobertura"].get("leidas"),
                                                            "medido": plausibles}]},
            "cobertura": {"filas_analizadas": plausibles}, "limitaciones": []}
'''


class _Inner:
    """Modelo interno mínimo para probar ModeloV2 sin red."""

    def __init__(self, respuestas, num_ctx=4096):
        self.respuestas, self.nombre, self.system, self.llamadas = list(respuestas), "x", "", 0
        self.base = types.SimpleNamespace(num_ctx=num_ctx)

    def pedir(self, tipo, prompt, *, json_out, cancel=None, corto=False):
        self.llamadas += 1
        return self.respuestas.pop(0)


class UnitTests(unittest.TestCase):
    def test_candidate_contract_letters_and_unambiguous_line_numbers(self):
        d2 = B.DEFECTOS["D2"].codigo
        uno = [{"unidad": P.unidad_en(d2, 21), "motivo": "oráculo"}]
        e, nota = V2.interpretar_micro(QWEN_BX, uno)
        self.assertIsNotNone(e, "«CANDIDATO: 21» es la línea de la única candidata")
        self.assertEqual((e["letra"], e["accion"]), ("A", "reemplazar"))
        self.assertIn("interpretado como la línea", nota)
        self.assertIsNotNone(V2.interpretar_micro("CANDIDATO: A\nACCION: eliminar", uno)[0])
        self.assertIsNone(V2.interpretar_micro("CANDIDATO: 2\nACCION: eliminar", uno)[0], "ni letra ni línea")
        self.assertIsNone(V2.interpretar_micro("CANDIDATO: C\nACCION: eliminar", uno)[0], "letra inexistente")
        self.assertIsNone(V2.interpretar_micro("no sé", uno)[0])

    def test_counterfactual_qwen_patch_does_not_fix_d2(self):
        """Evidencia de B*: ensamblada en v2 (compuesta completa) la corrección de Qwen compila, pero mantiene la
        condición siempre verdadera: el contador se sigue reiniciando (lo confirma el circuito con el juez)."""
        d2 = B.DEFECTOS["D2"].codigo
        e, _ = V2.interpretar_micro(QWEN_BX, [{"unidad": P.unidad_en(d2, 21), "motivo": ""}])
        ens = P.aplicar(d2, e["unidad"], e["accion"], e["fragmento"], completa=True)
        self.assertIsNone(ens.atribucion, ens.detalle)
        self.assertIn('if "cobertura" not in cobertura or cobertura["leidas"] == 0:', ens.codigo)
        v1 = P.aplicar(d2, e["unidad"], e["accion"], e["fragmento"])
        self.assertEqual(v1.atribucion, "modelo", "en v1 solo se reemplazaba el encabezado")

    def test_memory_guard(self):
        d = Path(tempfile.mkdtemp())
        mem = d / "meminfo"
        g = V2.Guardia(minimo_mb=3900, espera_s=10, intervalo_s=5, meminfo=str(mem), dormir=lambda s: None)
        mem.write_text("MemTotal: 6000000 kB\nMemAvailable: 4300000 kB\n")
        self.assertEqual(g.antes_de_llamada()["mem_disponible_mb"], 4199)
        mem.write_text("MemAvailable: 3000000 kB\n")
        with self.assertRaises(V2.MemoriaInsuficiente):
            g.antes_de_llamada()
        self.assertTrue(g.registro[-1]["abortado"])
        cargado = V2.Guardia(types.SimpleNamespace(loaded=lambda: ["qwen3:4b-instruct"]), minimo_mb=3900,
                             meminfo=str(mem), dormir=lambda s: None)
        self.assertTrue(cargado.antes_de_llamada()["modelo_cargado"], "con el modelo ya cargado basta un margen")
        self.assertEqual(V2.Guardia(minimo_mb=0).antes_de_llamada(), {"guardia": "desactivada"})

    def test_environment_failures_are_not_model_failures(self):
        m = V2.ModeloV2(_Inner([(None, {"resultado": "error", "error": "Conexión con Ollama interrumpida: reset"})]),
                        6, 999)
        with self.assertRaises(V2.EntornoInterrumpido):
            m.pedir("completo", "x", json_out=False)
        self.assertEqual(m.llamadas, [], "no cuenta como llamada del modelo")
        self.assertEqual(len(m.interrupciones), 1)
        m = V2.ModeloV2(_Inner([(None, {"resultado": "contexto_excedido", "error": "n_ctx"})]), 6, 999)
        with self.assertRaises(V2.ContextoInsuficiente):
            m.pedir("completo", "x", json_out=False)
        inner = _Inner([("ok", {"resultado": "completa"})], num_ctx=1200)
        with self.assertRaises(V2.ContextoInsuficiente):
            V2.ModeloV2(inner, 6, 999).pedir("completo", "palabra " * 400, json_out=False)
        self.assertEqual(inner.llamadas, 0, "si no entra, no se llama")

    def test_history_is_trimmed_to_fit_the_context(self):
        intentos = [{"descripcion": f"intento {i}", "resultado": "rechazado", "razon": "no mejora",
                     "diff": "@@\n-a\n+b\n" * 30, "fallas_despues": "- [sistema] S1"} for i in range(4)]
        def base(hist):
            return [("coordinador", "base\n")] + hist
        completo = len("\n".join(t for _, t in base(V2._historial(intentos, V2.HISTORIAL_COMPLETO))))
        ctx = types.SimpleNamespace(modelo=types.SimpleNamespace(cabe=lambda texto, corto: len(texto) < completo))
        partes, nivel = V2._con_historial(ctx, base, intentos, True)
        self.assertEqual(nivel, 0, "no entraba con el último intento detallado: quedan solo los resúmenes")
        self.assertIn("intento 4:", "".join(t for _, t in partes), "los resúmenes de una línea siguen")
        ctx.modelo.cabe = lambda texto, corto: False
        with self.assertRaises(V2.ContextoInsuficiente):
            V2._con_historial(ctx, lambda hist: [("coordinador", "base\n")] + hist, intentos, True)

    def test_keep_alive_is_sent_per_request_only_when_set(self):
        fake = FakeOllama(responses=["hola", "hola"])
        try:
            cfg = make_cfg()
            Modelo(OllamaClient(fake.url), cfg.explorer_model, "qwen3:4b-instruct", num_ctx=4096, temperatura=0.2,
                   timeout_s=30, keep_alive="0s").pedir("x", "y", json_out=False)
            Modelo(OllamaClient(fake.url), cfg.explorer_model, "qwen3:4b-instruct", num_ctx=4096, temperatura=0.2,
                   timeout_s=30).pedir("x", "y", json_out=False)
            self.assertEqual(fake.requests[0].get("keep_alive"), "0s")
            self.assertNotIn("keep_alive", fake.requests[1], "por defecto no cambia nada")
            self.assertEqual(fake.requests[0]["options"]["num_ctx"], 4096)
        finally:
            fake.close()

    def test_protocol_folders_never_mix(self):
        d = Path(tempfile.mkdtemp())
        PL.preparar_salida(d / "v1", True)                    # como el ensayo original: sin marca de protocolo
        (d / "v1" / "PROTOCOLO").unlink()
        (d / "v1" / "episodios.jsonl").write_text("{}\n")
        with self.assertRaises(ValueError):
            PL.preparar_salida(d / "v1", True, "v2")
        PL.preparar_salida(d / "v2", True, "v2")
        (d / "v2" / "episodios.jsonl").write_text("{}\n")
        with self.assertRaises(ValueError):
            PL.preparar_salida(d / "v2", True, "v1")
        self.assertEqual((d / "v2" / "PROTOCOLO").read_text().strip(), "v2")


class GuionVariable(Guion):
    """Guion que puede simular un corte de Ollama en ciertas llamadas (devuelve el error como el cliente real)."""

    def __init__(self, responder, cortes=()):
        super().__init__(responder)
        self.cortes, self.n = set(cortes), 0

    def pedir(self, tipo, prompt, *, json_out, cancel=None, corto=False):
        self.n += 1
        if self.n in self.cortes:
            self.pedidos.append((tipo, prompt))
            return None, {"tipo": tipo, "resultado": "error", "error": "Conexión con Ollama interrumpida: reset"}
        return super().pedir(tipo, prompt, json_out=json_out, cancel=cancel, corto=corto)


@unittest.skipUnless(bwrap_works() and mariadb_fixture.available(), "bubblewrap o MariaDB no disponibles")
class V2CircuitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = make_cfg(sandbox_timeout_s=30)
        cls.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        cls.ex = Executor(cls.cfg, limiter=NullLimiter(), allow_unverified_limits=True)
        assert cls.ex.self_test()["ok"]
        cls.dir = Path(tempfile.mkdtemp(prefix="cia-piloto-v2-"))
        cls.ent = Entorno(cls.cfg, cls.ex, mariadb_fixture.server(), cls.dir / "ent", base="piloto_v2",
                          escala={"dias": 3, "por_dia": 300})

    @classmethod
    def tearDownClass(cls):
        cls.ent.cerrar()

    def correr(self, responder, nombre, *, protocolo="v2", guiones=None, crear=None, **kw):
        guiones = [] if guiones is None else guiones

        def crear_def():
            g = Guion(responder)
            guiones.append(g)
            return g
        res = PL.correr(self.cfg, ent=self.ent, crear_modelo=crear or crear_def, salida=self.dir / nombre,
                        protocolo=protocolo, log=lambda *_: None, **kw)
        return res, guiones

    def test_bx_evidence_contract_feedback_and_repetition(self):
        def qwen(tipo, prompt):
            if tipo == "verificador":
                return f"```python\n{VERIFICADOR.strip()}\n```"
            return QWEN_BX
        res, guiones = self.correr(qwen, "bx_v2", defectos=("D2",), repeticiones=1, brazos=("B*",))
        e = res["episodios"][0]
        p1, p2 = e["pasos"][0], e["pasos"][1]
        self.assertEqual(p1["linea"], 21)
        self.assertIn("interpretado como la línea", p1["interpretacion"])
        self.assertEqual(p1["resultado"], "rechazado", "el parche de Qwen no corrige: el umbral lo rechaza")
        self.assertIsNone(p1["atribucion"], "ya no es un problema de contrato ni de ensamblado")
        self.assertEqual((p2["resultado"], p2["repite_intento"]), ("repetido", 1),
                         "el mismo parche no se vuelve a ejecutar")
        self.assertFalse(e["juez"]["corregido"])
        micro = [p for t, p in guiones[-1].pedidos if t == "micro"]
        self.assertNotEqual(micro[0], micro[1], "después de un rechazo el prompt cambia")
        self.assertIn("intento 1:", micro[1])
        self.assertIn("RECHAZADO", micro[1])
        self.assertIn("verificaciones después de ese cambio", micro[1])
        self.assertIn('>>  22 |         cobertura = {"leidas": 0', micro[0], "la compuesta se muestra completa")
        self.assertEqual(res["protocolo"], "v2")
        self.assertIn("(protocolo v2)", PI.markdown(res, None))
        # La misma evidencia con el protocolo v1 se conserva tal como corrió en la VivoBook.
        res1, _ = self.correr(qwen, "bx_v1", protocolo="v1", defectos=("D2",), repeticiones=1, brazos=("B*",))
        self.assertEqual(res1["episodios"][0]["pasos"][0]["detalle"], "respuesta sin CANDIDATO/ACCION válidos")

    def test_false_alarm_dispute_and_posthoc_judge(self):
        def responder(decision):
            def r(tipo, prompt):
                if tipo == "verificador":
                    return f"```python\n{VERIF_FALSA_ALARMA.strip()}\n```"
                if tipo == "completo":
                    code = prompt.split("Instrumento actual:\n```python\n")[1].split("```")[0]
                    return f"```python\n{arreglar(code)}```"
                if tipo == "disputa":
                    return f"DECISION: {decision}\nMOTIVO: el verificador cuenta solo valores plausibles"
                return ""
            return r
        res, guiones = self.correr(responder("verificador"), "fa_bien", defectos=("D2",), repeticiones=1,
                                   brazos=("D",), presupuesto=PL.Presupuesto(llamadas=4, deteccion=1))
        e = res["episodios"][0]
        self.assertTrue(e["juez"]["corregido"])
        self.assertEqual(e["pasos"][0]["fallas_despues"], ["V1"], "como en el ensayo: corregido y V1 sigue fallando")
        self.assertEqual(e["impugnados"], ["V1"])
        self.assertEqual(e["motivo_fin"], "sin_fallas", "un verificador impugnado deja de bloquear")
        self.assertEqual([t for t, _ in guiones[-1].pedidos], ["completo", "disputa"])
        self.assertTrue(res["falsas_alarmas"]["D2_r1"]["V1"]["falsa_alarma"], "el juez confirma la falsa alarma")
        d = res["lectura"]["disputas"][0]
        self.assertTrue(d["decision_correcta"])
        self.assertTrue(any("falsa alarma" in c for c in res["lectura"]["contra_la_hipotesis"]),
                        "una falsa alarma del modelo se informa en contra")
        # Si el modelo defiende al verificador equivocado, la disputa queda registrada como incorrecta y el
        # episodio sigue acotado por el presupuesto.
        res2, _ = self.correr(responder("instrumento"), "fa_mal", defectos=("D2",), repeticiones=1, brazos=("D",),
                              presupuesto=PL.Presupuesto(llamadas=4, deteccion=1))
        e2 = res2["episodios"][0]
        self.assertLessEqual(len(e2["llamadas"]), 4)
        self.assertFalse(res2["lectura"]["disputas"][0]["decision_correcta"])
        self.assertTrue(any("el juez dice lo contrario" in c for c in res2["lectura"]["contra_la_hipotesis"]))

    def test_environment_interruption_is_retried_then_stops_and_resumes(self):
        def bueno(tipo, prompt):
            if tipo == "verificador":
                return f"```python\n{VERIFICADOR.strip()}\n```"
            code = prompt.split("Instrumento actual:\n```python\n")[1].split("```")[0]
            return f"```python\n{arreglar(code)}```"
        guiones = []

        def un_corte():
            g = GuionVariable(bueno, cortes=(1,) if len(guiones) == 1 else ())   # corta la 1.ª llamada del brazo
            guiones.append(g)
            return g
        res, _ = self.correr(None, "corte1", crear=un_corte, defectos=("D2",), repeticiones=1, brazos=("D",))
        e = res["episodios"][0]
        self.assertEqual(res["estado"], "completo")
        self.assertTrue(e["reintento_por_entorno"])
        self.assertTrue(e["juez"]["corregido"])
        self.assertEqual(len(res["interrupciones"]), 1)
        self.assertEqual(res["interrupciones"][0]["tipo"], "EntornoInterrumpido")
        self.assertFalse(any(p.get("atribucion") == "modelo" for p in e["pasos"]), "el corte no se culpa al modelo")
        # Dos cortes seguidos detienen la corrida (protege la máquina); se reanuda sin repetir lo hecho.
        siempre = []

        def cortes():
            g = GuionVariable(bueno, cortes=() if not siempre else (1,))
            siempre.append(g)
            return g
        res2, _ = self.correr(None, "corte2", crear=cortes, defectos=("D2",), repeticiones=1, brazos=("D",))
        self.assertEqual(res2["estado"], "detenido_por_entorno")
        self.assertEqual(res2["episodios"], [])
        self.assertEqual(len((self.dir / "corte2" / "interrupciones.jsonl").read_text().splitlines()), 2)
        self.assertFalse(res2["lectura"]["integridad"]["apto_para_experimento_completo"])
        res3, _ = self.correr(bueno, "corte2", defectos=("D2",), repeticiones=1, brazos=("D",))
        self.assertEqual((res3["estado"], len(res3["episodios"])), ("completo", 1), "reanudado")

    def test_memory_guard_stops_before_any_model_call(self):
        mem = self.dir / "meminfo"
        mem.write_text("MemAvailable: 2000000 kB\n")
        guardia = V2.Guardia(minimo_mb=3900, espera_s=0, meminfo=str(mem), dormir=lambda s: None)
        res, guiones = self.correr(lambda t, p: "", "memoria", defectos=("D2",), repeticiones=1, brazos=("B",),
                                   guardia=guardia)
        self.assertEqual(res["estado"], "detenido_por_entorno")
        self.assertEqual(res["interrupciones"][0]["tipo"], "MemoriaInsuficiente")
        self.assertEqual(sum(len(g.pedidos) for g in guiones), 0, "no se llamó al modelo")

    def test_context_that_does_not_fit_is_a_coordinator_fault(self):
        creados = []

        def crear():   # la detección (1.ª) entra; el brazo C, con el instrumento completo, no
            creados.append(1)
            return Guion(lambda t, p: f"```python\n{VERIFICADOR.strip()}\n```" if t == "verificador" else "",
                         num_ctx=8192 if len(creados) == 1 else 2600)
        res, _ = self.correr(None, "contexto", crear=crear, defectos=("D2",), repeticiones=1, brazos=("C",))
        e = res["episodios"][0]
        self.assertFalse(e["valido"])
        self.assertIn("coordinador", e["motivo_invalido"])
        self.assertEqual(e["llamadas"], [])
        self.assertEqual(res["lectura"]["autoria"].get("coordinador (episodio inválido)"), 1)


if __name__ == "__main__":
    unittest.main()
