"""Pruebas del detector. Sin Ollama ni SDK: solo eventos ya registrados.   python3 -m unittest discover -s tests -v

fixtures/repro-p1: eventos REALES del SDK 1.58.1 (sandbox real, oh-duo-01) con un Ollama simulado que reproduce
lo observado en ensayo-p1 (Qwen: pruebas repetidas, edición de la prueba protegida, ediciones rechazadas,
30 iteraciones; Gemma: éxito en 6 llamadas). No son los registros de ensayo-p1, que no están en el repositorio.
"""
import copy
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent))
import estancamiento as E  # noqa: E402

FIX = AQUI / "fixtures" / "repro-p1"
ORIGINAL = 'def f(x):\n    return x + 1\n'
PRUEBA = 'import unittest\nfrom mod import f\n'


def arbol(raiz: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(raiz.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(raiz)).encode() + p.read_bytes())
    return h.hexdigest()


class Constructor:
    """Eventos sintéticos con el mismo esquema que persiste el SDK (kind, tool_call_id, observation…)."""

    def __init__(self):
        self.ev, self.n = [], 0
        self.add({"kind": "SystemPromptEvent", "source": "agent"})
        self.add({"kind": "MessageEvent", "source": "user", "llm_message": {"content": [{"text": "tarea"}]}})

    def add(self, e):
        self.ev.append(e)

    def accion(self, herr, accion, obs=None, error=None):
        self.n += 1
        tc = f"toolu_{self.n:02d}"
        self.add({"kind": "ActionEvent", "source": "agent", "tool_name": herr, "tool_call_id": tc,
                  "llm_response_id": f"resp-{self.n}", "action": accion, "thought": []})
        if error is not None:
            self.add({"kind": "AgentErrorEvent", "source": "agent", "tool_name": herr, "tool_call_id": tc, "error": error})
        elif obs is not None:
            self.add({"kind": "ObservationEvent", "source": "environment", "tool_name": herr, "tool_call_id": tc,
                      "observation": obs})
        return self

    def pruebas(self, ok, cmd="python3 -m unittest -v", falla="test_a"):
        texto = "Ran 2 tests in 0.001s\n\nOK" if ok else f"FAIL: {falla} (t.T)\nRan 2 tests\n\nFAILED (failures=1)"
        return self.accion("terminal", {"command": cmd, "kind": "TerminalAction"},
                           {"content": [{"text": texto}], "exit_code": 0 if ok else 1, "kind": "TerminalObservation"})

    def editar(self, ruta, viejo, nuevo, error=False):
        obs = ({"content": [{"text": "No replacement was performed."}], "is_error": True, "command": "str_replace"} if error
               else {"content": [{"text": "edited"}], "is_error": False, "command": "str_replace", "path": ruta,
                     "old_content": viejo, "new_content": nuevo})
        return self.accion("file_editor", {"command": "str_replace", "path": ruta, "old_str": viejo[:10],
                                           "new_str": nuevo[:10], "kind": "FileEditorAction"}, obs)

    def ver(self, ruta):
        return self.accion("file_editor", {"command": "view", "path": ruta, "kind": "FileEditorAction"},
                           {"content": [{"text": "contenido"}], "is_error": False, "command": "view"})

    def finish(self):
        return self.accion("finish", {"message": "listo", "kind": "FinishAction"},
                           {"content": [{"text": "listo"}], "kind": "FinishObservation"})

    def error_conversacion(self, codigo, detalle="x"):
        self.add({"kind": "ConversationErrorEvent", "source": "environment", "code": codigo, "detail": detalle})
        return self

    def escribir(self, destino: Path, estado="finished", saltar=None, modelo="m1"):
        conv = destino / "instancia" / "resultados" / "r1" / "estado" / "conversacion" / "c1"
        (conv / "events").mkdir(parents=True)
        for i, e in enumerate(self.ev):
            if saltar is not None and i == saltar:
                continue
            e = {"id": str(uuid.uuid4()), "timestamp": f"2026-01-01T00:00:{i:02d}", **e}
            (conv / "events" / f"event-{i:05d}-{e['id']}.json").write_text(json.dumps(e))
        (conv / "base_state.json").write_text(json.dumps({"execution_status": estado}))
        (destino / "instancia" / "tarea").mkdir(parents=True)
        (destino / "instancia" / "tarea" / "mod.py").write_text(ORIGINAL)
        (destino / "instancia" / "tarea" / "test_mod.py").write_text(PRUEBA)
        (destino / "instancia" / "mision.json").write_text(json.dumps({"id": "X", "modulo": "mod"}))
        (destino / "ejecucion.json").write_text(json.dumps({"problema": "X", "programador": "p", "modelo": modelo,
                                                            "estado_ejecucion": "error_infraestructura"}))
        return destino


class Referencia(unittest.TestCase):
    """Caso de referencia: la reproducción de ensayo-p1."""

    @classmethod
    def setUpClass(cls):
        cls.u = dict(E.UMBRALES)
        cls.q = E.analizar_ejecucion(FIX / "ejecuciones" / "P1__qwen", cls.u)
        cls.g = E.analizar_ejecucion(FIX / "ejecuciones" / "P1__gemma", cls.u)

    def test_qwen_estancado_por_agotamiento(self):
        self.assertEqual(self.q["fuente"]["tipo"], "eventos_del_sdk")
        self.assertEqual((self.q["terminacion"]["tipo"], self.q["terminacion"]["codigo"]),
                         ("agotamiento_de_iteraciones", "MaxIterationsReached"))
        self.assertEqual(self.q["iteraciones"], 30)
        self.assertIs(self.q["veredicto"]["estancamiento"], True)
        self.assertEqual(self.q["veredicto"]["confianza"], "alta")
        for regla in ("A1_repeticion_equivalente_sin_cambio", "A3_edicion_de_archivo_protegido",
                      "A4_ediciones_rechazadas_o_sin_efecto", "A5_pruebas_aprueban_solo_tras_editar_lo_protegido",
                      "A6_iteraciones_finales_sin_progreso"):
            self.assertIn(regla, self.q["veredicto"]["patrones"])

    def test_corrige_la_clasificacion_registrada(self):
        self.assertEqual((self.q["estado_registrado"], self.q["estado_ejecucion_sugerido"]),
                         ("error_infraestructura", "incompleta"))

    def test_evidencia_con_iteracion(self):
        a3 = next(a for a in self.q["alertas"] if a["regla"] == "A3_edicion_de_archivo_protegido")
        self.assertEqual(a3["evidencia"][0]["iteracion"], 3)
        self.assertEqual(a3["evidencia"][0]["efecto"], "efectiva")
        self.assertEqual(a3["evidencia"][0]["ruta"], "test_intervalos.py")
        self.assertTrue(all(e["efecto"] == "rechazada" for e in a3["evidencia"][1:]))
        a6 = next(a for a in self.q["alertas"] if a["regla"] == "A6_iteraciones_finales_sin_progreso")
        self.assertEqual(a6["detalle"], "el código productivo nunca cambió")
        self.assertEqual([p["iteracion"] for p in self.q["cronologia"]], list(range(1, 31)))

    def test_gemma_sin_alertas(self):
        self.assertEqual(self.g["terminacion"]["tipo"], "finalizacion_con_finish")
        self.assertIs(self.g["veredicto"]["estancamiento"], False)
        self.assertEqual(self.g["alertas"], [])
        self.assertEqual(self.g["iteraciones"], 6)
        self.assertEqual(self.g["estado_ejecucion_sugerido"], "finalizada")

    def test_umbrales_configurables(self):
        u = {**self.u, "repeticion_equivalente_sin_cambio": 50, "ediciones_rechazadas_o_sin_efecto": 50,
             "iteraciones_finales_sin_progreso": 50}
        q = E.analizar_ejecucion(FIX / "ejecuciones" / "P1__qwen", u)
        self.assertEqual({a["regla"] for a in q["alertas"]},
                         {"A3_edicion_de_archivo_protegido", "A5_pruebas_aprueban_solo_tras_editar_lo_protegido"})

    def test_cli_solo_lectura_y_salidas(self):
        antes = arbol(FIX)
        with tempfile.TemporaryDirectory() as t:
            E.main(["--corrida", str(FIX), "--salida", t])
            datos = json.loads(Path(t, "estancamiento.json").read_text())
            md = Path(t, "estancamiento.md").read_text()
        self.assertEqual(arbol(FIX), antes)
        self.assertEqual([r["ejecucion"] for r in datos["ejecuciones"]], ["P1__gemma", "P1__qwen"])
        self.assertIn("| P1__qwen | eventos_del_sdk | 30 | agotamiento_de_iteraciones | **sí** (alta)", md)
        with self.assertRaises(SystemExit):   # nunca escribir dentro de los registros
            E.main(["--corrida", str(FIX), "--salida", str(FIX / "informes")])

    def test_fuente_alternativa_resumen(self):
        with tempfile.TemporaryDirectory() as t:
            dst = Path(t) / "P1__qwen"
            shutil.copytree(FIX / "ejecuciones" / "P1__qwen", dst)
            shutil.rmtree(dst / "instancia" / "resultados")
            r = E.analizar_ejecucion(dst, self.u)
        self.assertEqual(r["fuente"]["tipo"], "resumen_agente")
        self.assertEqual(r["terminacion"]["tipo"], "agotamiento_de_iteraciones_probable")
        self.assertIs(r["veredicto"]["estancamiento"], True)
        self.assertTrue(any("recortadas" in n for n in r["notas"]))

    def test_independiente_del_modelo(self):
        with tempfile.TemporaryDirectory() as t:
            dst = Path(t) / "x"
            shutil.copytree(FIX / "ejecuciones" / "P1__qwen", dst)
            reg = json.loads((dst / "ejecucion.json").read_text())
            (dst / "ejecucion.json").write_text(json.dumps({**reg, "modelo": "otro-modelo", "programador": "z"}))
            r = E.analizar_ejecucion(dst, self.u)
        for k in ("veredicto", "alertas", "terminacion", "cronologia"):
            self.assertEqual(r[k], self.q[k])


class Sinteticos(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.u = copy.deepcopy(E.UMBRALES)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def analizar(self, c, nombre="e", **kw):
        return E.analizar_ejecucion(c.escribir(self.tmp / nombre, **kw), self.u)

    def test_repeticiones_legitimas_no_alertan(self):
        """Correr las pruebas después de cada edición efectiva es lo esperado."""
        nuevo1, nuevo2 = ORIGINAL.replace("+ 1", "+ 2"), ORIGINAL.replace("+ 1", "+ 3")
        c = (Constructor().pruebas(False).editar("/work/mod.py", ORIGINAL, nuevo1).pruebas(False)
             .editar("/work/mod.py", nuevo1, nuevo2).pruebas(True).finish())
        r = self.analizar(c)
        self.assertEqual(r["alertas"], [])
        self.assertIs(r["veredicto"]["estancamiento"], False)

    def test_exito_con_repeticiones_no_es_estancamiento(self):
        c = Constructor().ver("/work/mod.py").ver("/work/mod.py").ver("mod.py").pruebas(False)
        c.editar("/work/mod.py", ORIGINAL, ORIGINAL.replace("1", "2")).pruebas(True).finish()
        r = self.analizar(c)
        self.assertIn("A1_repeticion_equivalente_sin_cambio", r["veredicto"]["observaciones"])
        self.assertIs(r["veredicto"]["estancamiento"], False)

    def test_fallos_iguales_sin_cambio(self):
        c = Constructor()
        for _ in range(3):
            c.pruebas(False).editar("/work/mod.py", ORIGINAL, "x", error=True)
        c.error_conversacion("MaxIterationsReached")
        r = self.analizar(c, estado="error")
        reglas = {a["regla"] for a in r["alertas"]}
        self.assertTrue({"A1_repeticion_equivalente_sin_cambio", "A2_fallos_iguales_sin_cambio_del_codigo",
                         "A4_ediciones_rechazadas_o_sin_efecto"} <= reglas)
        self.assertIs(r["veredicto"]["estancamiento"], True)
        self.assertEqual(r["estado_ejecucion_sugerido"], "incompleta")

    def test_comandos_equivalentes(self):
        c = Constructor().pruebas(False, "python -m unittest").pruebas(False, "python3 -m unittest -v  ")
        c.pruebas(False, "pytest -q").error_conversacion("MaxIterationsReached")
        r = self.analizar(c, estado="error")
        a1 = next(a for a in r["alertas"] if a["regla"] == "A1_repeticion_equivalente_sin_cambio")
        self.assertEqual((a1["accion"], a1["veces"]), ("ejecutar_pruebas", 3))
        self.u["equivalencia_pruebas"] = False
        r = self.analizar(c, nombre="sin_equiv", estado="error")
        self.assertNotIn("A1_repeticion_equivalente_sin_cambio", {a["regla"] for a in r["alertas"]})

    def test_error_de_herramienta_no_es_infraestructura(self):
        c = Constructor().pruebas(False).accion("file_editor", {"command": "view", "path": "/work/nada.py"},
                                                error="Parameter 'x' is not allowed")
        c.editar("/work/mod.py", ORIGINAL, ORIGINAL.replace("1", "2")).pruebas(True).finish()
        r = self.analizar(c)
        self.assertEqual(r["errores_de_herramienta"], 1)
        self.assertEqual(r["terminacion"]["tipo"], "finalizacion_con_finish")
        self.assertIs(r["veredicto"]["estancamiento"], False)

    def test_error_real_de_infraestructura(self):
        c = Constructor().pruebas(False).error_conversacion("LLMTimeoutError", "litellm.Timeout: 300 s")
        r = self.analizar(c, estado="error")
        self.assertEqual(r["terminacion"]["tipo"], "error_de_infraestructura")
        self.assertIs(r["veredicto"]["estancamiento"], False)
        self.assertEqual(r["estado_ejecucion_sugerido"], "error_infraestructura")

    def test_eventos_faltantes_se_informan(self):
        c = Constructor().pruebas(False).ver("/work/mod.py").pruebas(False)
        r = self.analizar(c, estado="running", saltar=3)
        self.assertTrue(any("faltan eventos" in n for n in r["notas"]))
        self.assertEqual(r["terminacion"]["tipo"], "indeterminado")
        self.assertEqual(r["estado_ejecucion_sugerido"], "indeterminado")

    def test_sin_eventos(self):
        d = self.tmp / "vacia"
        d.mkdir()
        r = E.analizar_ejecucion(d, self.u)
        self.assertIsNone(r["veredicto"]["estancamiento"])
        self.assertTrue(any("no hay eventos" in n for n in r["notas"]))

    def test_edicion_protegida_por_terminal(self):
        c = Constructor().pruebas(False)
        c.accion("terminal", {"command": "sed -i 's/1/3/' test_mod.py"}, {"content": [{"text": ""}], "exit_code": 0})
        c.pruebas(True).error_conversacion("MaxIterationsReached")
        r = self.analizar(c, estado="error")
        a3 = next(a for a in r["alertas"] if a["regla"] == "A3_edicion_de_archivo_protegido")
        self.assertEqual(a3["evidencia"][0]["efecto"], "posible")

    def test_edicion_sin_efecto(self):
        c = Constructor().editar("/work/mod.py", ORIGINAL, ORIGINAL).editar("/work/mod.py", ORIGINAL, ORIGINAL)
        c.error_conversacion("MaxIterationsReached")
        r = self.analizar(c, estado="error")
        a4 = next(a for a in r["alertas"] if a["regla"] == "A4_ediciones_rechazadas_o_sin_efecto")
        self.assertEqual([e["efecto"] for e in a4["evidencia"]], ["sin_efecto", "sin_efecto"])

    def test_detector_del_sdk_y_pausa(self):
        r = self.analizar(Constructor().pruebas(False), nombre="stuck", estado="stuck")
        self.assertEqual(r["terminacion"]["tipo"], "detenido_por_el_detector_del_sdk")
        c = Constructor().pruebas(False)
        c.add({"kind": "PauseEvent", "source": "user"})
        r = self.analizar(c, nombre="pausa", estado="paused")
        self.assertEqual(r["terminacion"]["tipo"], "corte_por_tiempo")

    def test_texto_sin_herramienta(self):
        c = Constructor().pruebas(False)
        c.add({"kind": "MessageEvent", "source": "agent", "llm_response_id": "fin",
               "llm_message": {"content": [{"text": '{"name": "terminal"}'}]}})
        r = self.analizar(c)
        self.assertEqual(r["terminacion"]["tipo"], "termino_con_texto_sin_herramienta")
        self.assertEqual(r["iteraciones"], 2)

    def test_umbrales_desconocidos(self):
        p = self.tmp / "u.json"
        p.write_text(json.dumps({"no_existe": 1}))
        with self.assertRaises(SystemExit):
            E.cargar_umbrales(p)


if __name__ == "__main__":
    unittest.main()
