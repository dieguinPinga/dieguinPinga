"""Oficina distribuida, etapa 2 (OFICINA.md): cola persistente de misiones y tareas, con trabajadores SIMULADOS.

Sin red ni inferencias. Cubre dependencias, asignación atómica (hilos y procesos), arriendos con token y
vencimiento, reintentos, recuperación tras una interrupción, verificación central y el ensayo de punta a punta.
"""
import json
import multiprocessing
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from constructor_ia import cli
from constructor_ia.oficina import cola as CO
from constructor_ia.oficina import ensayo as EN


class Reloj:
    def __init__(self, t=2_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def tarea(nombre, deps=(), modelo="gemma4:12b", **k):
    return {"nombre": nombre, "rol": "revisor", "modelo": modelo, "carga": {"texto": nombre},
            "depende_de": list(deps), **k}


def _tomar_en_proceso(ruta, quien, cola_salida):
    c = CO.Cola(Path(ruta))
    tomadas = []
    while True:
        t = c.tomar(quien, ["m"], capacidad=100)
        if t is None:
            break
        tomadas.append(t["id"])
    c.cerrar()
    cola_salida.put(tomadas)


class ColaTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="cia-cola-"))
        self.reloj = Reloj()
        self.cola = CO.Cola(self.dir / "oficina.db", reloj=self.reloj)
        self.addCleanup(self.cola.cerrar)

    def test_missions_are_validated_all_or_nothing(self):
        casos = {"ciclo": ([tarea("a", ["b"]), tarea("b", ["a"])], "ciclo"),
                 "desconocida": ([tarea("a", ["z"])], "desconocidas"),
                 "repetida": ([tarea("a"), tarea("a")], "nombre único"),
                 "permiso": ([tarea("a", permiso="python")], "solo inferencia"),
                 "origen": ([tarea("a", origen_datos="produccion")], "origen sintético"),
                 "intentos": ([tarea("a", max_intentos=0)], "max_intentos")}
        for nombre, (tareas, esperado) in casos.items():
            with self.subTest(nombre):
                with self.assertRaisesRegex(CO.ErrorCola, esperado):
                    self.cola.crear_mision("x", tareas)
        self.assertEqual(self.cola.misiones(), [], "nada a medias")

    def test_dependencies_gate_assignment_until_verified(self):
        mid = self.cola.crear_mision("cadena", [tarea("a"), tarea("b", ["a"])])
        t = self.cola.tomar("w1", ["gemma4:12b"])
        self.assertTrue(t["id"].endswith(".a"))
        self.assertIsNone(self.cola.tomar("w2", ["gemma4:12b"]), "b espera a que a esté VERIFICADA")
        self.cola.entregar(t["id"], t["token"], {"eco": "a"})
        self.assertIsNone(self.cola.tomar("w2", ["gemma4:12b"]), "una propuesta no habilita a los dependientes")
        self.cola.verificar(t["id"], True, "ok")
        t2 = self.cola.tomar("w2", ["gemma4:12b"])
        self.assertTrue(t2["id"].endswith(".b"))
        self.cola.entregar(t2["id"], t2["token"], {"eco": "b"})
        self.cola.verificar(t2["id"], True, "ok")
        self.assertEqual(self.cola.mision(mid)["estado"], "completa")
        estados = [(e["de"], e["a"]) for e in self.cola.eventos(t["id"])]
        self.assertEqual(estados, [("pendiente", "asignada"), ("asignada", "propuesta"), ("propuesta", "verificada")])

    def test_model_permission_and_capacity_are_respected(self):
        self.cola.crear_mision("m", [tarea("a"), tarea("b"), tarea("q", modelo="qwen3:4b-instruct")])
        self.assertIsNone(self.cola.tomar("w", ["otro"]))
        self.assertIsNone(self.cola.tomar("w", ["gemma4:12b"], permisos=("python",)))
        self.assertIsNotNone(self.cola.tomar("w", ["gemma4:12b"]))
        self.assertIsNone(self.cola.tomar("w", ["gemma4:12b"]), "un turno por trabajador")
        self.assertTrue(self.cola.tomar("v", ["qwen3:4b-instruct"])["id"].endswith(".q"))

    def test_only_available_registered_workers_can_take(self):
        self.cola.crear_mision("m", [tarea("a")])
        trabajador = {"id": "pulenta", "modelos": ["gemma4:12b"], "permisos": ["inferencia"], "capacidad": 1,
                      "estado": "disponible", "latido_vigente": False, "disponible_ahora": False}
        with self.assertRaisesRegex(CO.ErrorCola, "no está disponible ahora"):
            self.cola.tomar_para(trabajador)
        trabajador.update(latido_vigente=True, disponible_ahora=True)
        self.assertEqual(self.cola.tomar_para(trabajador)["trabajador_id"], "pulenta")

    def test_expired_lease_is_recovered_and_a_late_delivery_is_rejected(self):
        mid = self.cola.crear_mision("m", [tarea("a", tiempo_max_s=60)])
        t = self.cola.tomar("caido", ["gemma4:12b"])
        self.reloj.t += 30
        self.cola.latido(t["id"], t["token"])
        self.reloj.t += 59
        self.assertEqual(self.cola.recuperar(), [], "el latido renovó el arriendo")
        self.reloj.t += 2
        self.assertEqual(self.cola.recuperar(), [t["id"]])
        self.assertEqual(self.cola.tarea(t["id"])["estado"], "pendiente")
        with self.assertRaises(CO.ArriendoInvalido):
            self.cola.entregar(t["id"], t["token"], {"eco": "tarde"})
        t2 = self.cola.tomar("firme", ["gemma4:12b"])
        self.assertEqual((t2["id"], t2["intentos"]), (t["id"], 2))
        with self.assertRaises(CO.ArriendoInvalido):
            self.cola.entregar(t["id"], t["token"], {"eco": "token viejo"})
        self.cola.entregar(t2["id"], t2["token"], {"eco": "a"})
        self.assertEqual(self.cola.tarea(t["id"])["propuesta"], {"eco": "a"}, "la entrega tardía no pisó nada")
        self.assertIn("arriendo vencido", " ".join(e["detalle"] for e in self.cola.eventos(mid=mid)))

    def test_delivery_after_expiry_is_rejected_even_before_recovery(self):
        self.cola.crear_mision("m", [tarea("a", tiempo_max_s=10)])
        t = self.cola.tomar("w", ["gemma4:12b"])
        self.reloj.t += 11
        with self.assertRaisesRegex(CO.ArriendoInvalido, "venció"):
            self.cola.entregar(t["id"], t["token"], {"eco": "a"})

    def test_retries_are_bounded_and_failure_cancels_dependents(self):
        mid = self.cola.crear_mision("m", [tarea("a", max_intentos=2), tarea("b", ["a"]), tarea("c", ["b"]),
                                           tarea("suelta")])
        for intento in (1, 2):
            t = self.cola.tomar("w", ["gemma4:12b"])
            self.assertTrue(t["id"].endswith(".a"))
            self.cola.entregar(t["id"], t["token"], {"eco": "mal"})
            estado = self.cola.verificar(t["id"], False, "el eco no coincide")
            self.assertEqual(estado, "pendiente" if intento == 1 else "fallida")
        m = {x["nombre"]: x for x in self.cola.mision(mid)["tareas"]}
        self.assertEqual((m["a"]["estado"], m["b"]["estado"], m["c"]["estado"], m["suelta"]["estado"]),
                         ("fallida", "cancelada", "cancelada", "pendiente"))
        self.assertIsNone(m["a"]["propuesta"], "la propuesta rechazada no queda como resultado")
        self.assertEqual(self.cola.mision(mid)["estado"], "en_curso", "la suelta todavía puede avanzar")
        t = self.cola.tomar("w", ["gemma4:12b"])
        self.cola.fallar(t["id"], t["token"], "Ollama cortó")
        self.assertEqual(self.cola.tarea(t["id"])["estado"], "pendiente", "una falla informada se reintenta")

    def test_only_proposals_can_be_verified(self):
        self.cola.crear_mision("m", [tarea("a")])
        t = self.cola.tomar("w", ["gemma4:12b"])
        with self.assertRaisesRegex(CO.ErrorCola, "propuesta"):
            self.cola.verificar(t["id"], True, "apurado")

    def test_interrupted_coordinator_recovers_after_restart(self):
        ruta = self.dir / "oficina.db"
        self.cola.crear_mision("m", [tarea("a", tiempo_max_s=5)])
        t = self.cola.tomar("w", ["gemma4:12b"])
        self.cola.iniciar(t["id"], t["token"])
        self.cola.cerrar()                                 # el coordinador «se cae»
        reloj = Reloj(self.reloj.t + 6)
        nueva = CO.Cola(ruta, reloj=reloj)                 # y arranca de nuevo
        self.addCleanup(nueva.cerrar)
        self.assertEqual(nueva.tarea(t["id"])["estado"], "en_curso", "persistió")
        self.assertEqual(nueva.recuperar(), [t["id"]])
        self.assertEqual(nueva.tarea(t["id"])["estado"], "pendiente")

    def test_cancel_mission(self):
        mid = self.cola.crear_mision("m", [tarea("a"), tarea("b")])
        t = self.cola.tomar("w", ["gemma4:12b"])
        self.cola.cancelar_mision(mid)
        self.assertEqual(self.cola.mision(mid)["estado"], "cancelada")
        with self.assertRaises(CO.ArriendoInvalido):
            self.cola.entregar(t["id"], t["token"], {"eco": "a"})
        with self.assertRaisesRegex(CO.ErrorCola, "no existe"):
            self.cola.cancelar_mision("O-no-existe")


class AtomicidadTests(unittest.TestCase):
    def test_threads_never_take_the_same_task(self):
        d = Path(tempfile.mkdtemp(prefix="cia-cola-hilos-"))
        cola = CO.Cola(d / "oficina.db")
        cola.crear_mision("m", [tarea(f"t{i}", modelo="m") for i in range(60)])
        cola.cerrar()
        tomadas, lock = [], threading.Lock()

        def trabajar(quien):
            c = CO.Cola(d / "oficina.db")
            while True:
                t = c.tomar(quien, ["m"], capacidad=100)
                if t is None:
                    break
                with lock:
                    tomadas.append(t["id"])
            c.cerrar()
        hilos = [threading.Thread(target=trabajar, args=(f"w{i}",)) for i in range(8)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join(60)
        self.assertEqual(len(tomadas), 60)
        self.assertEqual(len(set(tomadas)), 60, "ninguna tarea asignada dos veces")

    def test_separate_processes_never_take_the_same_task(self):
        d = Path(tempfile.mkdtemp(prefix="cia-cola-procesos-"))
        cola = CO.Cola(d / "oficina.db")
        cola.crear_mision("m", [tarea(f"t{i}", modelo="m") for i in range(40)])
        cola.cerrar()
        ctx = multiprocessing.get_context("spawn")
        salida = ctx.Queue()
        procs = [ctx.Process(target=_tomar_en_proceso, args=(str(d / "oficina.db"), f"p{i}", salida)) for i in range(4)]
        for p in procs:
            p.start()
        todas = [x for _ in procs for x in salida.get(timeout=120)]
        for p in procs:
            p.join(60)
        self.assertEqual((len(todas), len(set(todas))), (40, 40))


class EnsayoTests(unittest.TestCase):
    def test_end_to_end_with_simulated_workers(self):
        r = EN.correr(Path(tempfile.mkdtemp(prefix="cia-ensayo-")), log=lambda *_: None)
        self.assertTrue(r["ok"], r)
        self.assertEqual(set(r["misiones"].values()), {"completa"})
        self.assertGreaterEqual(r["recuperadas"], 1, "la tarea del trabajador caído volvió a la cola")
        self.assertGreaterEqual(r["propuestas_rechazadas"], 1, "la propuesta incorrecta se rechazó centralmente")
        self.assertTrue(r["entrega_tardia"].startswith("rechazada"))
        self.assertTrue(r["solape_entre_misiones"], "las dos misiones avanzaron a la vez")
        self.assertTrue(any(i["como"] == "abandonada" for i in r["intervalos"]))


class CliColaTests(unittest.TestCase):
    def correr(self, *argv):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["--config", str(self.dir / "no-existe.toml"), "oficina", *argv,
                           "--carpeta", str(self.dir / "oficina")])
        return rc, out.getvalue(), err.getvalue()

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="cia-cola-cli-"))

    def test_create_list_recover_cancel(self):
        plan = self.dir / "plan.json"
        plan.write_text(json.dumps({"titulo": "revisión", "tareas": [tarea("a"), tarea("b", ["a"])]}))
        rc, out, _ = self.correr("mision", "--archivo", str(plan))
        self.assertEqual(rc, 0, out)
        mid = out.split()[1]
        rc, out, _ = self.correr("cola")
        self.assertIn("«revisión»: pendiente", out)
        self.assertIn("- b [revisor, gemma4:12b]: pendiente · intento 0 de 3 · depende de a", out)
        self.assertEqual(json.loads(self.correr("cola", "--json")[1])[0]["id"], mid)
        self.assertIn("0 arriendo(s) vencido(s)", self.correr("recuperar")[1])
        self.assertEqual(self.correr("cancelar", "--mision", mid)[0], 0)
        self.assertIn(": cancelada", self.correr("cola")[1])
        plan.write_text(json.dumps({"titulo": "mala", "tareas": [tarea("a", ["a"])]}))
        rc, _, err = self.correr("mision", "--archivo", str(plan))
        self.assertEqual(rc, 2)
        self.assertIn("oficina:", err)

    def test_the_rehearsal_uses_its_own_disposable_database(self):
        rc, out, _ = self.correr("ensayo-cola")
        self.assertEqual(rc, 0, out)
        self.assertIn("Resultado: OK", out)
        self.assertFalse((self.dir / "oficina" / "oficina.db").exists(), "el ensayo no toca la base real de la oficina")
        self.assertEqual(len(list((self.dir / "oficina" / "ensayos").glob("cola-*/ensayo-cola.db"))), 1)


if __name__ == "__main__":
    unittest.main()
