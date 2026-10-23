"""Oficina, etapa 3 (OFICINA.md): inferencia remota acotada, candado por destino, planificador y un trabajador de
programación sobre la cola que reutiliza el motor original (Engine) y el sandbox real (Bubblewrap + seccomp).

SIN inferencias reales: el «trabajador remoto» es un Ollama falso por HTTP que devuelve código escrito en la prueba.
El código se ejecuta en el sandbox real (con NullLimiter, como las demás pruebas del motor).
"""
import json
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from constructor_ia import cli
from constructor_ia.oficina import cola as CO
from constructor_ia.oficina import programador as PG
from constructor_ia.oficina import registro as RG
from constructor_ia.oficina import remoto as RM
from constructor_ia.oficina.misiones_programacion import REFERENCIA, mision_resumen_ventas
from constructor_ia.ollama import OllamaClient
from constructor_ia.sandbox.executor import Executor
from constructor_ia.sandbox.limiter import NullLimiter

from .fake_ollama import FakeOllama
from .helpers import bwrap_works, make_cfg

# Errores típicos, escritos a mano: el primero no filtra por fecha; el segundo excluye el día «hasta».
SIN_FILTRO = REFERENCIA.replace(
    "        if (desde is not None and fecha < desde) or (hasta is not None and fecha > hasta):\n            continue\n", "")
HASTA_EXCLUSIVO = REFERENCIA.replace("fecha > hasta", "fecha >= hasta")


def bloque(codigo):
    return f"Aquí está la solución.\n```python\n{codigo}```\n"


class RemotoTests(unittest.TestCase):
    def test_only_whitelisted_routes_reach_the_worker(self):
        fake = FakeOllama(models=("gemma4:12b",))
        self.addCleanup(fake.close)
        cli_ = RM.ClienteRemoto(fake.url)
        self.assertEqual(cli_.tags(), ["gemma4:12b"])
        self.assertEqual(cli_.loaded(), [])
        for metodo, ruta in (("GET", "/api/pull"), ("POST", "/api/pull"), ("DELETE", "/api/delete"),
                             ("POST", "/api/create"), ("POST", "/api/generate")):
            with self.subTest(ruta=ruta):
                conn = cli_._conn(2)
                with self.assertRaises(RM.RutaNoPermitida):
                    conn.request(metodo, ruta, body="{}")
        self.assertEqual(fake.requests, [], "nada llegó al servidor")

    def test_one_gate_per_destination(self):
        a, b = RM.compuerta("http://100.92.0.7:11435"), RM.compuerta("http://100.92.0.8:11435")
        self.assertIs(a, RM.compuerta("http://100.92.0.7:11435/"))
        self.assertIsNot(a, b)
        a.acquire("x")
        try:
            hecho = threading.Event()
            threading.Thread(target=lambda: (b.acquire("y"), b.release(), hecho.set())).start()
            self.assertTrue(hecho.wait(2), "otro destino no queda bloqueado")
        finally:
            a.release()

    def test_planner_rules(self):
        tarea = {"modelo": "gemma4:12b", "permiso": "inferencia"}
        remoto = {"id": "pulenta", "url": "http://100.92.0.7:11435", "modelos": ["gemma4:12b"],
                  "permisos": ["inferencia"], "disponible_ahora": True, "motivo": "ok"}
        local = dict(remoto, id="vivobook", url="http://127.0.0.1:11435")
        t, m = RM.elegir(tarea, [remoto], memoria_mb=lambda: 3650)
        self.assertEqual(t["id"], "pulenta", "lo remoto no depende de la RAM de la VivoBook")
        t, m = RM.elegir(tarea, [local], memoria_mb=lambda: 3650)
        self.assertIsNone(t)
        self.assertIn("3650 MiB disponibles (< 3900)", m)
        self.assertIn("queda pendiente", m)
        self.assertEqual(RM.elegir(tarea, [local], memoria_mb=lambda: 5000)[0]["id"], "vivobook")
        for cambio, esperado in (({"disponible_ahora": False}, "no disponible ahora"),
                                 ({"modelos": ["qwen3:4b-instruct"]}, "no tiene gemma4:12b"),
                                 ({"permisos": []}, "sin permiso")):
            t, m = RM.elegir(tarea, [dict(remoto, **cambio)])
            self.assertIsNone(t)
            self.assertIn(esperado, m)


class MisionTests(unittest.TestCase):
    def test_hidden_cases_are_not_in_the_prompt_material(self):
        m = mision_resumen_ventas()
        casos = m["spec"]["cases"]
        self.assertEqual((sum(c["visible"] for c in casos), sum(not c["visible"] for c in casos)), (2, 5))
        oculto = next(c for c in casos if not c["visible"] and not c.get("kwargs"))
        self.assertEqual((oculto["expect"]["importe"], oculto["expect"]["unidades"]), (235, 56))
        from constructor_ia import prompts
        texto = json.dumps(prompts.initial(m["description"], m["spec"], 20000), ensure_ascii=False)
        self.assertNotIn("235", texto)
        self.assertIn("Además hay 5 casos ocultos", texto)


@unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
class ProgramadorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = make_cfg(sandbox_timeout_s=20, sandbox_case_timeout_s=3)
        cls.ex = Executor(cls.cfg, limiter=NullLimiter(), allow_unverified_limits=True)
        cls.ex.self_test()
        assert cls.ex.enabled, cls.ex.disabled_reason

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="cia-programacion-"))
        self.cola = CO.Cola(self.dir / "oficina.db")
        self.addCleanup(self.cola.cerrar)

    def encolar(self, max_intentos=2):
        return self.cola.crear_mision("programación: resumen_ventas", [{
            "nombre": "resumen_ventas", "rol": "programador", "modelo": "gemma4:12b",
            "carga": {"mision": "resumen_ventas"}, "max_intentos": max_intentos, "tiempo_max_s": 300}])

    def trabajador(self, fake):
        return {"id": "pulenta", "url": fake.url, "modelos": ["gemma4:12b"], "permisos": ["inferencia"],
                "capacidad": 1, "disponible_ahora": True}

    def programador(self, **k):
        return PG.Programador(self.dir, self.cfg, self.ex, self.cola, log=lambda *_: None,
                              cliente_para=lambda t: OllamaClient(t["url"]), **k)

    def fake(self, respuestas, delay=0.0):
        f = FakeOllama([bloque(c) for c in respuestas], delay=delay, models=("gemma4:12b",))
        self.addCleanup(f.close)
        return f

    def test_reference_solution_passes_every_case_and_wrong_ones_do_not(self):
        c = PG.comprobar_mision(self.ex)
        self.assertEqual((c["ok"], c["casos"], c["visibles"], c["ocultos"]), (True, "7/7", 2, 5))
        m = mision_resumen_ventas()
        for malo in (SIN_FILTRO, HASTA_EXCLUSIVO):
            r = self.ex.run(malo, m["spec"], "").result
            self.assertGreater(r["failed"], 0, "los casos ocultos detectan el error")

    def test_generate_execute_evaluate_and_correct_without_human_intervention(self):
        fake = self.fake([SIN_FILTRO, HASTA_EXCLUSIVO, REFERENCIA])
        mid_cola = self.encolar()
        r = self.programador().atender(self.trabajador(fake))
        self.assertEqual(r["resultado"], "propuesta", r)
        inf = r["informe"]
        self.assertEqual((inf["estado"], inf["intento_final"]), ("aprobada", 3))
        self.assertEqual([i["test_status"] for i in inf["intentos"]], ["fallidas", "fallidas", "aprobadas"])
        self.assertEqual(len(fake.requests), 3)
        self.assertEqual(fake.requests[0]["model"], "gemma4:12b")
        self.assertEqual(fake.requests[0]["options"]["num_ctx"], 8192)
        # El informe de corrección llega al modelo, pero nunca la verdad oculta.
        reparacion = fake.requests[1]["messages"][1]["content"]
        self.assertIn("Resultados reales de las pruebas", reparacion)
        self.assertIn("Caso oculto", reparacion)
        for req in fake.requests:
            texto = json.dumps(req["messages"], ensure_ascii=False)
            self.assertNotIn("235", texto)
            self.assertNotIn("2026-03-20", texto, "ni los argumentos de los casos ocultos")
        t = self.cola.tarea(r["tarea"])
        self.assertEqual(t["estado"], "propuesta")
        ok, detalle = PG.verificar(self.cola, self.dir, self.cfg, self.ex, r["tarea"])
        self.assertTrue(ok, detalle)
        self.assertIn("reejecución independiente aprobada (7/7 casos, incluidos los ocultos)", detalle)
        self.assertEqual(self.cola.mision(mid_cola)["estado"], "completa")
        ej = self.cola.db.all("SELECT * FROM ejecuciones")
        self.assertEqual([(e["intento"], e["estado"]) for e in ej], [(1, "aprobada")])
        taller = PG.Taller(self.dir, self.cfg, self.ex, fake.url, PG.modelo_remoto("gemma4:12b"), cliente=object())
        self.addCleanup(taller.cerrar)
        archivos = {x["rel_path"] for x in taller.db.all("SELECT rel_path FROM files")}
        for n in (1, 2, 3):
            self.assertIn(f"intento-{n}/solucion.py", archivos)
            self.assertIn(f"intento-{n}/respuesta_modelo.md", archivos)
        self.assertIn("final/solucion.py", archivos)

    def test_a_mission_that_never_passes_is_rejected_and_requeued_never_completed(self):
        fake = self.fake([SIN_FILTRO] * 4)
        self.encolar()
        r = self.programador().atender(self.trabajador(fake))
        self.assertEqual((r["resultado"], r["informe"]["estado"]), ("propuesta", "fallida"))
        ok, detalle = PG.verificar(self.cola, self.dir, self.cfg, self.ex, r["tarea"])
        self.assertFalse(ok)
        self.assertIn("sin superar las pruebas", detalle)
        self.assertEqual(self.cola.tarea(r["tarea"])["estado"], "pendiente", "queda un intento de la cola")

    def test_a_tampered_proposal_is_rejected_by_the_independent_check(self):
        fake = self.fake([REFERENCIA])
        self.encolar()
        r = self.programador().atender(self.trabajador(fake))
        self.cola.db.execute("UPDATE tareas SET propuesta_json=? WHERE id=?",
                             (json.dumps(dict(self.cola.tarea(r["tarea"])["propuesta"], huella_final="0" * 64)),
                              r["tarea"]))
        ok, detalle = PG.verificar(self.cola, self.dir, self.cfg, self.ex, r["tarea"])
        self.assertEqual((ok, detalle), (False, "el código final no coincide con la huella entregada"))

    def test_an_unreachable_worker_is_an_environment_failure_not_an_answer(self):
        fake = self.fake([REFERENCIA])
        self.encolar()
        fake.close()
        r = self.programador().atender(self.trabajador(fake))
        self.assertEqual(r["resultado"], "falla_de_entorno → pendiente")
        self.assertIsNone(self.cola.tarea(r["tarea"])["propuesta"])

    def test_an_interrupted_execution_is_never_completed_nor_duplicated(self):
        fake = self.fake([REFERENCIA])
        self.encolar()
        # Un proceso anterior tomó la tarea, creó su misión en el taller y murió a mitad de camino.
        t = self.cola.tomar("pulenta", ["gemma4:12b"], roles=["programador"])
        taller = PG.Taller(self.dir, self.cfg, self.ex, fake.url, PG.modelo_remoto("gemma4:12b"), cliente=object())
        vieja = taller.missions.create(mision_resumen_ventas(), "gemma4:12b", "oficina:pulenta")
        taller.missions.set_state(vieja, "generando", attempt=1)
        taller.cerrar()
        self.programador()                               # crea la tabla de ejecuciones
        self.cola.db.execute("INSERT INTO ejecuciones (tarea_id, intento, trabajador_id, mision_taller, estado, inicio) "
                             "VALUES (?,?,?,?,?,?)", (t["id"], 1, "pulenta", vieja, "en_curso", time.time()))
        self.cola.db.execute("UPDATE tareas SET arriendo_vence=? WHERE id=?", (time.time() - 1, t["id"]))
        r = self.programador().atender(self.trabajador(fake))
        self.assertEqual(r["resultado"], "propuesta")
        ej = {e["intento"]: e["estado"] for e in self.cola.db.all("SELECT * FROM ejecuciones")}
        self.assertEqual(ej, {1: "interrumpida", 2: "aprobada"})
        taller = PG.Taller(self.dir, self.cfg, self.ex, fake.url, PG.modelo_remoto("gemma4:12b"), cliente=object())
        self.addCleanup(taller.cerrar)
        self.assertEqual(taller.missions.get(vieja)["state"], "interrumpida", "nunca «aprobada»")
        aprobadas = taller.db.all("SELECT id FROM missions WHERE state='aprobada'")
        self.assertEqual(len(aprobadas), 1, "una sola misión aprobada: sin duplicados")
        with self.assertRaises(CO.ArriendoInvalido):
            self.cola.entregar(t["id"], t["token"], {"tarde": True})

    def test_a_lost_lease_cancels_the_running_mission(self):
        fake = self.fake([REFERENCIA], delay=1.5)
        mid_cola = self.encolar()
        prog = self.programador(latido_s=0.2)
        threading.Timer(0.5, lambda: CO.Cola(self.dir / "oficina.db").cancelar_mision(mid_cola)).start()
        r = prog.atender(self.trabajador(fake))
        self.assertEqual(r["resultado"], "arriendo_perdido")
        self.assertEqual(r["informe"]["estado"], "cancelada")
        self.assertEqual(self.cola.mision(mid_cola)["estado"], "cancelada")

    def test_an_unavailable_worker_takes_nothing(self):
        fake = self.fake([REFERENCIA])
        self.encolar()
        self.assertIsNone(self.programador().atender(dict(self.trabajador(fake), disponible_ahora=False)))
        self.assertEqual(fake.requests, [])


@unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
class CliProgramacionTests(unittest.TestCase):
    def correr(self, *argv):
        out, err = StringIO(), StringIO()
        ex = lambda cfg: Executor(cfg, limiter=NullLimiter(), allow_unverified_limits=True)  # noqa: E731
        with redirect_stdout(out), redirect_stderr(err), mock.patch("constructor_ia.sandbox.executor.Executor", ex), \
                mock.patch.object(RM, "memoria_disponible_mb", lambda: 8000):
            rc = cli.main(["--config", str(self.dir / "no-existe.toml"), "oficina", *argv,
                           "--carpeta", str(self.dir / "oficina")])
        return rc, out.getvalue(), err.getvalue()

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="cia-prog-cli-"))
        self.fake = FakeOllama([bloque(REFERENCIA)], models=("gemma4:12b",))
        self.addCleanup(self.fake.close)
        reg = RG.Registro(self.dir / "oficina" / "oficina.db")
        reg.registrar("prueba", self.fake.url, ["gemma4:12b"], permitir_local=True)
        reg.cerrar()

    def test_check_without_inference_then_programming_requires_explicit_authorization(self):
        rc, out, _ = self.correr("comprobar-programacion", "--trabajador", "prueba")
        self.assertEqual(rc, 0, out)
        self.assertIn("[OK] misión «resumen_ventas» con la solución de REFERENCIA: 7/7 casos (2 visibles, 5 ocultos)", out)
        self.assertIn("listo para autorizar", out)
        self.assertEqual(self.fake.requests, [], "la comprobación no infiere")
        rc, out, _ = self.correr("encolar-programacion")
        self.assertEqual(rc, 0, out)
        rc, _, err = self.correr("programar", "--trabajador", "prueba")
        self.assertEqual(rc, 2)
        self.assertIn("--autorizo-inferencia-remota", err)
        self.assertEqual(self.fake.requests, [], "sin autorización no se envía nada")
        rc, out, _ = self.correr("programar", "--trabajador", "prueba", "--autorizo-inferencia-remota")
        self.assertEqual(rc, 0, out)
        self.assertIn("Verificación central: ACEPTADA", out)
        self.assertEqual(len(self.fake.requests), 1)


if __name__ == "__main__":
    unittest.main()
