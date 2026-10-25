"""Puente de mensajes con el SDK real (OpenHands 1.58.1) y el modelo simulado, en proceso. Sin sandbox ni inferencia.
   <venv>/bin/python -I -m unittest discover -s tests -v      (se saltea si el venv no tiene el SDK)
"""
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI))
sys.path.insert(0, str(AQUI.parent / "ejecutor"))
os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
import simulado as S  # noqa: E402

try:
    import litellm
    from pydantic import SecretStr
    from openhands.sdk import LLM, Agent, Conversation, Tool
    from openhands.sdk.event import ActionEvent, MessageEvent
    from openhands.tools.terminal import TerminalTool
    SDK = True
except ImportError:
    SDK = False

import puente  # noqa: E402


def linea(ident: str, texto: str, huella: str | None = None) -> str:
    return json.dumps({"id": ident, "texto": texto, "sha256": huella or hashlib.sha256(texto.encode()).hexdigest()}) + "\n"


@unittest.skipUnless(SDK, "el SDK de OpenHands no está en este Python")
class Puente(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="oh5-puente-"))
        (self.tmp / "work").mkdir()
        self.buzon = self.tmp / "buzon.jsonl"
        self.buzon.write_text("")
        self.registro = self.tmp / "entregas.jsonl"

    def conversacion(self, respuesta, max_iter=8, demora=0.3):
        self.srv = S.Servidor(respuesta, demora)
        self.addCleanup(self.srv.cerrar)
        litellm.OllamaChatConfig(num_ctx=16384)
        llm = LLM(usage_id="a", model="ollama_chat/simulado", base_url=f"http://127.0.0.1:{self.srv.puerto}",
                  api_key=SecretStr("x"), native_tool_calling=False, reasoning_effort="none",
                  capability_overrides={"supports_sampling_params": True}, max_input_tokens=16384,
                  num_retries=0, timeout=30)
        agent = Agent(llm=llm, tools=[Tool(name=TerminalTool.name, params={"terminal_type": "subprocess"})],
                      include_default_tools=["FinishTool"])
        conv = Conversation(agent=agent, workspace=str(self.tmp / "work"), persistence_dir=str(self.tmp / "estado"),
                            max_iteration_per_run=max_iter, stuck_detection=False, visualizer=None)
        conv.send_message("tarea inicial")
        self.canal = puente.Puente(conv, self.buzon, self.registro, intervalo=0.1)
        return conv

    def escribir_en(self, segundos, texto):
        def f():
            time.sleep(segundos)
            with open(self.buzon, "a") as b:
                b.write(texto)
        threading.Thread(target=f, daemon=True).start()

    def registro_(self):
        return [json.loads(l) for l in self.registro.read_text().splitlines()] if self.registro.is_file() else []

    def del_coordinador(self, conv):
        return [e for e in conv.state.events if isinstance(e, MessageEvent) and (e.sender or "").startswith("oh-duo-04:")]

    def test_entrega_durante_run_sin_cambiar_el_limite(self):
        conv = self.conversacion(lambda n, m: S.CMD(n))
        self.canal.iniciar()
        self.escribir_en(1.2, linea("e1-a", "REVISA TU ESTRATEGIA"))
        conv.run()
        self.canal.detener()
        r = {x["resultado"]: x for x in self.registro_()}
        self.assertIn("entregado", r)
        self.assertIn("accion_posterior", r)
        self.assertGreater(r["accion_posterior"]["evento"], r["entregado"]["evento"])
        self.assertEqual(r["accion_posterior"]["herramienta"], "terminal")
        msg = self.del_coordinador(conv)
        self.assertEqual([m.sender for m in msg], ["oh-duo-04:e1-a"])
        # el modelo lo recibió en el pedido siguiente
        self.assertTrue(any("REVISA TU ESTRATEGIA" in json.dumps(p) for p in self.srv.pedidos))
        # mismo run(), mismo presupuesto: 8 acciones como máximo
        self.assertEqual(sum(isinstance(e, ActionEvent) for e in conv.state.events), 8)
        self.assertEqual(self.canal.resumen()["sin_accion_posterior"], [])

    def test_duplicado_ignorado(self):
        conv = self.conversacion(lambda n, m: S.CMD(n))
        self.canal.iniciar()
        self.escribir_en(1.0, linea("e1-a", "MENSAJE") * 2)
        self.escribir_en(1.8, linea("e1-a", "MENSAJE"))
        conv.run()
        self.canal.detener()
        cuenta = self.canal.resumen()["cuenta"]
        self.assertEqual(cuenta.get("entregado"), 1)
        self.assertEqual(cuenta.get("duplicado_ignorado"), 2)
        self.assertEqual(len(self.del_coordinador(conv)), 1)

    def test_despues_de_finish_se_descarta_y_el_estado_no_cambia(self):
        conv = self.conversacion(lambda n, m: S.FIN if n >= 2 else S.CMD(n), demora=0.1)
        self.canal.iniciar()
        conv.run()
        self.assertEqual(conv.state.execution_status.value, "finished")
        with open(self.buzon, "a") as b:
            b.write(linea("e1-tarde", "LLEGA TARDE"))
        time.sleep(0.6)
        self.canal.detener()
        r = self.registro_()
        self.assertEqual([(x["resultado"], x.get("estado")) for x in r], [("descartado", "finished")])
        self.assertEqual(conv.state.execution_status.value, "finished")     # no volvió a idle
        self.assertEqual(self.del_coordinador(conv), [])

    def test_formato_invalido_y_linea_incompleta(self):
        conv = self.conversacion(lambda n, m: S.CMD(n), max_iter=6)
        self.canal.iniciar()
        malas = (linea("e1-a", "TEXTO", huella="0" * 64) + linea("../../x", "TEXTO") + "no es json\n"
                 + json.dumps({"id": "e1-b"}) + "\n" + linea("e1-c", "x" * 2001))
        self.escribir_en(0.5, malas)
        completa = linea("e1-d", "PARTIDA")
        self.escribir_en(0.8, completa[:10])          # mitad de línea: se espera el resto
        self.escribir_en(1.6, completa[10:])
        conv.run()
        self.canal.detener()
        r = self.registro_()
        self.assertEqual(sum(x["resultado"] == "rechazado_formato" for x in r), 5)
        self.assertEqual([x["id"] for x in r if x["resultado"] == "entregado"], ["e1-d"])
        self.assertEqual([m.sender for m in self.del_coordinador(conv)], ["oh-duo-04:e1-d"])

    def test_sin_buzon_no_falla(self):
        conv = self.conversacion(lambda n, m: S.FIN if n >= 2 else S.CMD(n), demora=0.1)
        self.buzon.unlink()
        self.canal.iniciar()
        conv.run()
        self.canal.detener()
        self.assertEqual(self.registro_(), [])
        self.assertEqual(self.canal.resumen(), {"cuenta": {}, "sin_accion_posterior": []})


if __name__ == "__main__":
    unittest.main()
