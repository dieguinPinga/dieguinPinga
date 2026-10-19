"""Regresiones de la exploración real M20261018-3571c4 (0.4.3) — v0.4.4.

Observado: C35 y C36 devolvieron registros reales, pero la etapa B se truncó una y otra vez (cada intento consumía
la reserva entera de 900 tokens a ~4 tokens/s). Después Ollama CPU tardó 317580 ms «actualizando su caché de
prompts», Constructor informó timeout «al contactar a Ollama» y la misión terminó FALLIDA. Ollama completó el pedido
abandonado con HTTP 500 a los 5m18s y luego siguió respondiendo con normalidad.

Causas en Constructor que estas pruebas fijan:
* Un servidor que acepta el pedido pero no envía nada se informaba como «No se pudo contactar a Ollama» (carrera
  entre el timeout del socket y el vigilante) y el agente lo trataba como «no disponible»: misión fallida.
* El pedido siguiente no consideraba que el servidor seguía ocupado con el pedido abandonado.
* La etapa B no tenía límites de cantidad ni de largo, y repetía la misma interpretación con la misma reserva.

Las causas DEL SERVIDOR (caché de prompts, memoria, swap) no se suponen: se miden con `constructor-ia ollama-diag`,
cuyas reglas de análisis se prueban aquí con muestras sintéticas y con un proceso real.
"""
import dataclasses
import http.server
import json
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from constructor_ia import diag_ollama as D
from constructor_ia.config import ModelConfig
from constructor_ia.explorer import agent as A
from constructor_ia.explorer import context as X
from constructor_ia.explorer import validation as V
from constructor_ia.ollama import Generation, OllamaClient, OllamaTimeout, OllamaUnavailable

from . import mariadb_fixture
from .helpers import wait_for
from .test_explorer_contexto import WideBase
from .test_explorer_regresion import Script, good_report


def raw(text, **kw):
    """Respuesta del Ollama simulado con control fino (demora, truncamiento, tiempos)."""
    return {"text": text, "_raw": True, **kw}


class StalledServer:
    """Servidor HTTP que acepta el pedido y no responde durante `delay` segundos (como Ollama trabado)."""

    def __init__(self, delay: float, partial: bool = False):
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                try:
                    if partial:  # empieza a responder y se traba a mitad de la generación
                        self.send_response(200)
                        self.end_headers()
                        self.wfile.write(b'{"message": {"content": "{\\"res"}, "done": false}\n')
                        self.wfile.flush()
                    time.sleep(delay)
                    if not partial:
                        self.send_response(500)
                        self.end_headers()
                except OSError:
                    pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class ClientTests(unittest.TestCase):
    def test_busy_server_is_a_timeout_not_unavailable(self):
        srv = StalledServer(4)
        self.addCleanup(srv.close)
        c = OllamaClient(srv.url)
        for t in (1.2, 2.3):  # plazos fraccionarios: antes el socket se adelantaba al vigilante
            t0 = time.monotonic()
            with self.assertRaises(OllamaTimeout) as cm:
                c.chat(ModelConfig("m", timeout_s=t), [{"role": "user", "content": "x"}])
            self.assertEqual(cm.exception.phase, "espera")
            self.assertNotIn("contactar", str(cm.exception))
            self.assertLess(time.monotonic() - t0, t + 1)

    def test_stall_during_generation_is_reported_as_such(self):
        srv = StalledServer(4, partial=True)
        self.addCleanup(srv.close)
        with self.assertRaises(OllamaTimeout) as cm:
            OllamaClient(srv.url).chat(ModelConfig("m", timeout_s=1.5), [{"role": "user", "content": "x"}])
        self.assertEqual(cm.exception.phase, "generacion")

    def test_closed_port_is_unavailable(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with self.assertRaises(OllamaUnavailable):
            OllamaClient(f"http://127.0.0.1:{port}").chat(ModelConfig("m", timeout_s=3), [{"role": "user", "content": "x"}])

    def test_timings_reveal_unexplained_server_time(self):
        g = Generation("x", True, "stop", 2500, 120, duration_ms=330_000, truncated=False, load_ms=100,
                       prompt_eval_ms=9_000, eval_ms=30_000, total_ms=330_000, first_byte_ms=300_000)
        t = g.timings()
        self.assertEqual(t["sin_explicar_ms"], 330_000 - 39_100)
        self.assertEqual(t["generacion_tokens_s"], 4.0)
        self.assertAlmostEqual(t["prompt_tokens_s"], 277.78, places=1)


class SalvageAndBudgetTests(unittest.TestCase):
    def test_truncated_interpretation_keeps_only_complete_parts(self):
        text = ('{"resumen": "Diez filas con duración 1080.", "hallazgos": [{"texto": "Duración 1080", "tipo": '
                '"hallazgo", "evidencia": ["C35"]}, {"texto": "El motivo es «TIMEO')
        d = V.salvage_json(text)
        self.assertEqual(d["resumen"], "Diez filas con duración 1080.")
        self.assertEqual([h["texto"] for h in d["hallazgos"]], ["Duración 1080"], "nunca se completa un texto cortado")
        self.assertIsNone(V.salvage_json('{"resumen": "Diez fil'))
        self.assertIsNone(V.salvage_json("sin json"))

    def test_bounded_schemas_fit_their_reserves(self):
        def maximal(sc):
            txt = "Las operaciones registradas muestran una duración constante de mil ochenta segundos. " * 9
            if sc.get("type") == "object":
                return {k: maximal(v) for k, v in sc["properties"].items()}
            if sc.get("type") == "array":
                return [maximal(sc["items"]) for _ in range(sc["maxItems"])]
            return max(sc["enum"], key=len) if "enum" in sc else txt[:sc["maxLength"]]

        for schema, reserve in ((A.INTERPRET_SCHEMA, 512), (A.INTERPRET_MIN_SCHEMA, A.INTERPRET_MIN_PREDICT)):
            biggest = X.estimate_tokens(json.dumps(maximal(schema), ensure_ascii=False))
            self.assertLessEqual(biggest, reserve, "la respuesta más larga posible entra en la reserva")
        self.assertIn("maxLength", A.DECIDE_SCHEMA["properties"]["sql"])

    def test_reserve_follows_measured_speed(self):
        ex = A.Explorer.__new__(A.Explorer)
        ex._rates = {"gen": [], "prompt": []}
        self.assertEqual(ex._fit_predict("B", 512, 2000, 300)[0], 512, "sin mediciones no se cambia nada")
        ex._rates = {"gen": [4.0, 2.0, 5.0], "prompt": [20.0]}
        np_, why = ex._fit_predict("B", 512, 2000, 300)
        self.assertEqual(np_, int(2.0 * (300 - 100 - 5) * 0.9))
        self.assertIn("2.0 tokens/s", why)
        self.assertEqual(ex._fit_predict("B", 512, 6000, 300)[0], A.MIN_PREDICT["B"], "nunca baja del piso")

    def test_ollama_summary(self):
        calls = [{"resultado": "completa", "reloj_ms": 100_000, "generacion_tokens_s": 4.0, "prompt_tokens_s": 30.0,
                  "sin_explicar_ms": 1000, "carga_ms": 0},
                 {"resultado": "truncada", "reloj_ms": 200_000, "generacion_tokens_s": 3.0, "prompt_tokens_s": 20.0,
                  "sin_explicar_ms": 50_000, "carga_ms": 2000},
                 {"resultado": "timeout", "reloj_ms": 300_000}]
        s = A.ollama_summary(calls)
        self.assertEqual((s["completas"], s["truncadas"], s["timeouts"]), (1, 1, 1))
        self.assertEqual(s["reloj_total_s"], 600.0)
        self.assertEqual(s["sin_explicar_max_s"], 50.0)


def synthetic(n=60, swap_in_pages=0, psi_full=0.0, cpu_ticks_per_s=0, cg_high=0, swap_used=0, psi_cpu=0.0):
    """Muestras sintéticas de ollama-diag (1 por segundo)."""
    out = []
    for i in range(n):
        out.append({
            "t": 1000.0 + i, "cpu_total": 400 * i, "load": ["1", "1", "1"],
            "mem": {"MemTotal": 5800, "MemAvailable": 300, "SwapTotal": 2048, "SwapFree": 2048 - swap_used},
            "vm": {"pswpin": swap_in_pages * i, "pswpout": 0, "pgmajfault": 10 * i},
            "psi": {"memory": {"some": {"avg10": psi_full, "total_us": int(psi_full * 1e4 * i)},
                               "full": {"avg10": psi_full, "total_us": int(psi_full * 1e4 * i)}},
                    "cpu": {"some": {"avg10": psi_cpu, "total_us": 0}}, "io": {"some": {"avg10": 0, "total_us": 0},
                                                                               "full": {"avg10": 0, "total_us": 0}}},
            "ollama": {"rss_mib": 4200, "swap_mib": 300 if swap_in_pages else 0, "majflt": (swap_in_pages // 2) * i,
                       "cpu_ticks": cpu_ticks_per_s * i, "estados": "S", "procesos": 1},
            "cgroup": {"memory.events": {"high": cg_high * i, "max": 0, "oom_kill": 0}},
        })
    return {"meta": {"nucleos": 4, "clk": 100, "pid": 123}, "muestras": out}


class DiagnosisTests(unittest.TestCase):
    def test_memory_pressure_is_identified_only_with_measured_swap_activity(self):
        r = D.analyze(synthetic(swap_in_pages=2000, psi_full=30.0, cpu_ticks_per_s=20))
        self.assertTrue(any(i.startswith("PRESIÓN DE MEMORIA") for i in r["indicios"]), r["indicios"])

    def test_swap_in_use_without_reads_does_not_explain_slowness(self):
        r = D.analyze(synthetic(swap_used=1024, cpu_ticks_per_s=380))
        joined = " ".join(r["indicios"])
        self.assertNotIn("PRESIÓN DE MEMORIA", joined)
        self.assertIn("el swap ocupado NO explica la lentitud", joined)

    def test_cgroup_limits_and_cpu_contention(self):
        self.assertTrue(any("LÍMITE DEL CGROUP" in i for i in D.analyze(synthetic(cg_high=3))["indicios"]))
        self.assertTrue(any("CONTENCIÓN DE CPU" in i for i in D.analyze(synthetic(psi_cpu=60, cpu_ticks_per_s=50))["indicios"]))

    def test_server_side_stall_without_system_pressure(self):
        busy = D.analyze(synthetic(cpu_ticks_per_s=390))["indicios"][0]
        self.assertIn("su propio cómputo", busy)
        idle = D.analyze(synthetic(cpu_ticks_per_s=1))["indicios"][0]
        self.assertIn("esperando algo interno", idle)

    def test_each_call_window_is_analyzed(self):
        data = synthetic(n=120, cpu_ticks_per_s=390)
        calls = [{"etapa": "B", "resultado": "timeout", "ts": 1010.0, "reloj_ms": 60_000}]
        r = D.analyze(data, calls)
        self.assertEqual(len(r["llamadas"]), 1)
        self.assertGreaterEqual(r["llamadas"][0]["medido"]["segundos"], 59)
        self.assertIn("etapa B timeout", D.render(r))

    def test_refuses_main_ollama(self):
        with self.assertRaises(SystemExit):
            D.collect("http://127.0.0.1:11434", 1, 0.5, None, Path("/dev/null"), log=lambda *a: None)

    def test_live_collection_against_a_real_process(self):
        code = ("import sys, time; sys.path.insert(0, '.'); from tests.fake_ollama import FakeOllama; "
                "f = FakeOllama([]); print(f.port, flush=True); time.sleep(30)")
        proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        self.addCleanup(proc.kill)
        port = int(proc.stdout.readline())
        out = Path(self._tmpdir()) / "m.jsonl"
        data = D.collect(f"http://127.0.0.1:{port}", 2.5, 0.5, None, out, log=lambda *a: None)
        self.assertEqual(data["meta"]["pid"], proc.pid, "identifica el proceso que escucha en el puerto")
        self.assertGreaterEqual(len(data["muestras"]), 3)
        self.assertTrue(any(s.get("api_ps", {}).get("ok") for s in data["muestras"]))
        r = D.analyze(data)
        self.assertIn("ollama_rss_max_mib", r["total"])
        self.assertGreater(r["total"]["ollama_rss_max_mib"], 0)

    def _tmpdir(self):
        import tempfile

        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return d


@unittest.skipUnless(mariadb_fixture.available(), "MariaDB no instalado")
class StageBAndServerTests(WideBase):
    def run_ollama(self, script, timeout_s=None, serial=False, durations=None, limits=None, setup=None):
        app, fake, spec = self.make(self.cave, [], limits=limits)
        if timeout_s:
            app.cfg.explorer_model = dataclasses.replace(app.cfg.explorer_model, timeout_s=timeout_s)
        fake.default, fake.serial, fake.durations = script, serial, durations
        if setup:
            setup(app, fake)
        mid = self.start_mission(app, spec)
        wait_for(lambda: app.missions.get(mid)["state"] in ("completada", "fallida", "cancelada"), timeout=120)
        self.app, self.fake, self.mid = app, fake, mid
        run = app.db.one("SELECT * FROM explore_runs WHERE mission_id=?", (mid,))
        return app.missions.get(mid), (dict(run) if run else None)

    def steps_after(self):
        return [lambda p, i: {"pregunta": "¿Qué registros hay?", "por_que": "leer", "evidencia": [], "herramienta": "muestra",
                              "tabla": self.ops},
                lambda p, i: {"pregunta": "¿Cómo se distribuye la duración?", "por_que": "verificar", "evidencia": [],
                              "herramienta": "perfil", "tabla": self.ops, "columna": "duracion"},
                lambda p, i: {"pregunta": "fin", "por_que": "listo", "evidencia": [], "herramienta": "terminar"}]

    def b_requests(self):
        return [r for r in self.fake.requests if "ETAPA B:" in r["messages"][-1]["content"]]

    def test_truncated_interpretation_is_salvaged_without_repeating_it(self):
        def interpret(p, i):
            if "SELECT * FROM" not in p:
                return {"resumen": "Duración constante.", "hallazgos": []}
            return raw('{"resumen": "Diez filas con duración 1080.", "hallazgos": [{"texto": "Las diez filas tienen '
                       f'duración 1080", "tipo": "hallazgo", "evidencia": ["C{i["q"]}"]}}, {{"texto": "El motivo es «VEN',
                       done_reason="length", eval_count=512)

        m, run = self.run_ollama(Script(self.steps_after(), report=good_report, interpret=interpret))
        self.assertEqual(m["state"], "completada", m["error"])
        first_b = [r for r in self.b_requests() if "SELECT * FROM" in r["messages"][-1]["content"]]
        self.assertEqual(len(first_b), 1, "no se repite la interpretación que ya se truncó")
        self.assertEqual(first_b[0]["options"]["num_predict"], 512)
        step = self.app.db.one("SELECT interpretation FROM explore_steps WHERE mission_id=? AND tool='muestra'", (self.mid,))
        rec = json.loads(step[0])
        self.assertEqual(rec["forma"], "recuperada")
        self.assertEqual(rec["respaldados"], 1)
        metrics = json.loads(run["metrics_json"])
        self.assertEqual(metrics["ciclos_completos"], 1)
        self.assertEqual(metrics["interpretaciones_recuperadas"], 1)
        self.assertEqual(metrics["ollama"]["truncadas"], 1)

    def test_unrecoverable_truncation_falls_back_to_one_minimal_interpretation(self):
        def interpret(p, i):
            if "Respondé lo MÍNIMO" in p:
                return {"resumen": "Diez filas.", "hallazgos": [{"texto": "Duración 1080 en todas", "tipo": "hallazgo",
                                                               "evidencia": [f"C{i['q']}"]}]}
            if "SELECT * FROM" in p:
                return raw('{"resumen": "Diez fil', done_reason="length", eval_count=512)
            return {"resumen": "Duración constante.", "hallazgos": []}

        m, run = self.run_ollama(Script(self.steps_after(), report=good_report, interpret=interpret))
        mins = [r for r in self.b_requests() if "Respondé lo MÍNIMO" in r["messages"][-1]["content"]]
        self.assertEqual(len(mins), 1)
        self.assertEqual(mins[0]["options"]["num_predict"], A.INTERPRET_MIN_PREDICT)
        self.assertEqual(mins[0]["format"]["properties"]["hallazgos"]["maxItems"], 1)
        rec = json.loads(self.app.db.one("SELECT interpretation FROM explore_steps WHERE mission_id=? AND tool='muestra'",
                                         (self.mid,))[0])
        self.assertEqual(rec["forma"], "minima")
        self.assertEqual(json.loads(run["metrics_json"])["ciclos_completos"], 1)

    def test_stalled_server_does_not_fail_the_mission(self):
        # Reproducción de M20261018-3571c4: Ollama acepta el pedido y no responde (caché de prompts); el cliente
        # abandona; el servidor sigue ocupado con ese pedido (un solo slot) y recién después atiende el siguiente.
        calls = {"n": 0}

        def interpret(p, i):
            calls["n"] += 1
            if calls["n"] == 1:
                return raw(json.dumps({"resumen": "tarde", "hallazgos": []}), header_delay=4.0)
            return {"resumen": "Duración 1080 en las filas leídas.", "hallazgos": [
                {"texto": "Las filas tienen duración 1080", "tipo": "hallazgo", "evidencia": [f"C{i['q']}"]}]}

        steps = [self.steps_after()[0], self.steps_after()[0]] + self.steps_after()[1:]
        # Plazo fraccionario, como en la exploración real (min(300, tiempo restante)): ahí el timeout del socket se
        # adelantaba al vigilante y el error decía «No se pudo contactar», que el agente trataba como caída.
        m, run = self.run_ollama(Script(steps, report=good_report, interpret=interpret), timeout_s=2.3, serial=True)
        self.assertEqual(m["state"], "completada", "un servidor ocupado no es un servidor caído")
        metrics = json.loads(run["metrics_json"])
        self.assertEqual(metrics["ollama"]["timeouts"], 1)
        self.assertGreaterEqual(metrics["ciclos_completos"], 1, "el ciclo se completa después del bloqueo")
        ev = [e[0] for e in self.app.db.all("SELECT message FROM events WHERE mission_id=? ORDER BY id", (self.mid,))]
        self.assertTrue(any("no respondió" in e and "servidor sigue vivo" in e for e in ev))
        attempts = [a for s in self.app.db.all("SELECT context_json FROM explore_steps WHERE mission_id=?", (self.mid,))
                    if s[0] for st in json.loads(s[0]).values() if isinstance(st, dict) for a in st.get("intentos", [])]
        self.assertTrue(any(a.get("plazo_s", 0) > 2.3 for a in attempts), "el pedido siguiente tuvo más plazo")

    def test_persistent_stall_ends_with_an_honest_reason(self):
        t0 = time.monotonic()
        m, run = self.run_ollama(Script(self.steps_after(), report=good_report), timeout_s=1.5, serial=True,
                                 setup=lambda app, fake: setattr(fake, "header_delay", 30.0))
        self.assertEqual(m["state"], "completada", "el proceso termina; el resultado dice por qué es insuficiente")
        self.assertEqual(run["ended_reason"], "ollama_sin_respuesta")
        self.assertTrue(any("no respondió a tiempo" in r for r in json.loads(run["reasons_json"])))
        self.assertLess(time.monotonic() - t0, 30, "no se insiste contra un servidor trabado")
        self.assertIsNotNone(self.app.files.read_rel(self.mid, "exploracion/informe.md"))
        calls = json.loads(self.app.files.read_rel(self.mid, "exploracion/llamadas_ollama.json"))
        self.assertEqual([c["resultado"] for c in calls], ["timeout", "timeout"], "ni siquiera se pide el informe")

    def test_refused_connection_still_fails_after_checking(self):
        def first(p, i):
            self.dead_port_app.ollama.port = self.dead_port
            return {"pregunta": "ver", "por_que": "leer", "evidencia": [], "herramienta": "muestra", "tabla": self.ops}

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.dead_port = s.getsockname()[1]
        s.close()
        with mock.patch.object(A, "RECOVERY_WAIT_S", 0.1):
            m, run = self.run_ollama(Script([first], report=good_report),
                                     setup=lambda app, fake: setattr(self, "dead_port_app", app))
        self.assertEqual(m["state"], "fallida")
        self.assertIn("no disponible", m["error"])

    def test_reserve_shrinks_when_measured_generation_is_slow(self):
        slow = lambda body, text: {"eval_duration": int(60e9), "prompt_eval_duration": int(5e9),  # noqa: E731
                                   "load_duration": 0, "total_duration": int(65e9)}
        m, run = self.run_ollama(Script(self.steps_after(), report=good_report), durations=slow)
        sent = [r["options"]["num_predict"] for r in self.fake.requests]
        self.assertGreater(sent[0], sent[-2], "tras medir la velocidad, la reserva se ajusta al plazo")
        ctx = [json.loads(s[0]) for s in self.app.db.all("SELECT context_json FROM explore_steps WHERE mission_id=?"
                                                          " AND context_json IS NOT NULL", (self.mid,))]
        notes = [a.get("ajuste_reserva", "") for c in ctx for st in c.values() if isinstance(st, dict)
                 for a in st.get("intentos", [])]
        self.assertTrue(any("tokens/s medidos" in n for n in notes))

    def test_timings_and_stable_prefix(self):
        timed = lambda body, text: {"load_duration": int(1e6), "prompt_eval_duration": int(2e8),  # noqa: E731
                                    "eval_duration": int(3e8), "total_duration": int(5e8)}
        m, run = self.run_ollama(Script(self.steps_after(), report=good_report), durations=timed)
        reqs = self.fake.requests
        systems = {r["messages"][0]["content"] for r in reqs}
        self.assertEqual(systems, {A.SYSTEM}, "un único mensaje de sistema para A, B y el informe")
        header = reqs[0]["messages"][-1]["content"].split("ESTRUCTURA OBSERVADA")[0]
        self.assertTrue(header.startswith("Misión del operador:"))
        self.assertTrue(all(r["messages"][-1]["content"].startswith(header) for r in reqs),
                        "todas las etapas comparten el mismo comienzo del mensaje")
        self.assertNotIn("LEÍDA", reqs[-2]["messages"][-1]["content"].split("MAPA")[0])
        calls = json.loads(self.app.files.read_rel(self.mid, "exploracion/llamadas_ollama.json"))
        self.assertTrue(all("generacion_tokens_s" in c for c in calls if c["resultado"] == "completa"))
        self.assertIn("ollama", json.loads(run["metrics_json"]))


if __name__ == "__main__":
    unittest.main()
