"""E1 sin inferencia: puntos de inserción, pedido directo, extracción del código y regla de etapas.
   <venv>/bin/python -I -m unittest discover -s tests -p test_e1.py -v
(El recorrido completo con bwrap real y las cinco variantes del modelo simulado lo hace validar.sh, paso 13.)
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path[:0] = [str(AQUI), str(AQUI.parent / "e1"), str(AQUI.parent / "diagnostico"), str(AQUI.parent)]
os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
import simulado as S  # noqa: E402
from ejecutar import hermano  # noqa: E402

try:
    import litellm
    from pydantic import SecretStr
    from openhands.sdk import LLM, Agent, Conversation, Tool
    from openhands.tools.file_editor import FileEditorTool
    from openhands.tools.terminal import TerminalTool
    SDK = True
except ImportError:
    SDK = False

OH1 = hermano("oh-duo-01", "duo.py")
TAREA, REF = OH1 / "banco" / "P1" / "tarea", OH1 / "banco" / "P1" / "referencia" / "intervalos.py"


class Directo(unittest.TestCase):
    def setUp(self):
        import directo as D
        self.D = D
        self.ref = REF.read_text()
        self.test = (TAREA / "test_intervalos.py").read_text()

    def test_pedido_usa_los_archivos_autenticos_y_las_opciones_del_agente(self):
        b = self.D.pedido(TAREA)
        self.assertEqual(b["options"], {"temperature": 0.2, "seed": 11, "num_ctx": 16384, "num_predict": 2048})
        self.assertFalse(b["think"])
        texto = b["messages"][0]["content"]
        self.assertIn((TAREA / "intervalos.py").read_text().rstrip("\n"), texto)
        self.assertIn(self.test.rstrip("\n"), texto)
        self.assertNotIn("max(resultado[-1][1]", texto)              # la consigna no trae la solución

    def test_extrae_solo_produccion_aunque_venga_con_las_pruebas(self):
        for t in (self.ref + "\n\n" + self.test, self.test.replace('if __name__ == "__main__":\n    unittest.main()\n', "") + "\n" + self.ref):
            x = self.D.extraer(f"Fixed:\n```python\n{t}```\nDone.")
            self.assertEqual(x["codigo"].strip(), self.ref.strip())
            self.assertTrue(x["bloque_incluia_pruebas"])

    def test_sin_codigo_valido(self):
        self.assertIsNone(self.D.extraer("I think the tests are wrong.")["codigo"])
        self.assertIsNone(self.D.extraer("```python\ndef unir_intervalos(:\n```")["codigo"])
        self.assertIsNone(self.D.extraer("```python\nprint(1)\n```")["codigo"])

    def test_los_archivos_del_banco_no_cambian(self):
        antes = {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in TAREA.iterdir()}
        self.D.pedido(TAREA)
        self.D.extraer("```python\n" + self.ref + "```")
        self.assertEqual(antes, {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in TAREA.iterdir()})


class Etapas(unittest.TestCase):
    def etapas(self, **ok):
        import analizar_e1 as AE
        r = {"etapas": {k: {"ok": v} for k, v in ok.items()}}
        return AE, r

    def test_primera_etapa_que_falla(self):
        AE, r = self.etapas(**{"0_validez": True, "1_comprension": True, "2_formulacion": True, "3_aplicacion": True, "4_verificacion": False})
        self.assertEqual(AE._etapa(r), "verificacion")            # aplicada, pero no pasa visibles y ocultas
        r["etapas"]["1_comprension"]["ok"] = False
        self.assertEqual(AE._etapa(r), "comprension")
        r["etapas"]["0_validez"]["ok"] = False
        self.assertEqual(AE._etapa(r), "no_valida")

    def test_recuperarse_de_un_rechazo_no_es_romperse(self):
        AE, r = self.etapas(**{"0_validez": True, "1_comprension": True, "2_formulacion": True, "3_aplicacion": False, "4_verificacion": True})
        self.assertEqual(AE._etapa(r), "aplicacion")              # sin recuperación registrada
        r["etapas"]["3_aplicacion"]["recuperacion"] = {"aplica_la_correccion_despues": True}
        self.assertTrue(AE._etapa(r).startswith("ninguna (se completa tras recuperarse"))


@unittest.skipUnless(SDK, "el SDK de OpenHands no está en este Python")
class Puntos(unittest.TestCase):
    """plan.puntos sobre una conversación real del SDK con la forma de A1: prueba, mira, edición rechazada."""

    def test_k_y_j(self):
        import plan as PLAN
        tmp = Path(tempfile.mkdtemp(prefix="oh5-e1p-"))
        e = tmp / "corrida" / "ejecuciones" / "P1__qwen"
        r = e / "instancia" / "resultados" / "r1"
        shutil.copytree(TAREA, e / "instancia" / "tarea")
        shutil.copytree(TAREA, tmp / "work")
        (e / "instancia" / "mision.json").write_text(json.dumps({"modulo": "intervalos"}))
        r.mkdir(parents=True)
        # fuera del sandbox no hay /work: las rutas son las del espacio de trabajo de la prueba
        pasos = [S.T("python3 -m unittest -v"),                                         # 0: ve el fallo
                 S.T("echo nada"),                                                      # 1
                 S.E(command="view", path=str(tmp / "work" / "intervalos.py")),        # 2: ve el código → k = 2
                 S.E(command="str_replace", path=str(tmp / "work" / "intervalos.py"), old_str="return [(1, 3)]",
                     new_str="return [(1, 10)]"),                                       # 3: rechazada → j
                 S.FIN]
        srv = S.Servidor(lambda n, m: pasos[min(n - 1, len(pasos) - 1)], demora=0.01)
        litellm.OllamaChatConfig(num_ctx=16384)
        llm = LLM(usage_id="agente", model="ollama_chat/qwen2.5-coder:7b", base_url=f"http://127.0.0.1:{srv.puerto}",
                  api_key=SecretStr("x"), native_tool_calling=False, temperature=0.2, seed=11, reasoning_effort="none",
                  capability_overrides={"supports_sampling_params": True}, max_input_tokens=16384, num_retries=0, timeout=30)
        agent = Agent(llm=llm, tools=[Tool(name=TerminalTool.name, params={"terminal_type": "subprocess"}), Tool(name=FileEditorTool.name)],
                      include_default_tools=["FinishTool"])
        conv = Conversation(agent=agent, workspace=str(tmp / "work"), persistence_dir=str(r / "estado" / "conversacion"),
                            max_iteration_per_run=10, stuck_detection=True, visualizer=None)
        conv.send_message("tarea")
        conv.run()
        srv.cerrar()
        p = PLAN.puntos(tmp / "corrida")
        self.assertEqual(p["k"], 2)
        self.assertTrue(p["codigo_a_la_vista_en_k"])
        self.assertEqual((p["a1_vio_el_fallo_en"], p["a1_vio_el_codigo_en"]), (0, 2))
        self.assertEqual(p["j"], 3)
        self.assertEqual(p["edicion_rechazada"]["old_str"], "return [(1, 3)]")
        self.assertIn("did not appear verbatim", p["edicion_rechazada"]["observacion"])
        g = PLAN.guion("E1-C", p["j"])
        self.assertEqual([s["id"] for s in g["sondas"]], ["A", "B"])
        self.assertNotIn("max(", PLAN.SONDA_A + PLAN.SONDA_B)            # las preguntas no traen la solución


if __name__ == "__main__":
    unittest.main()
