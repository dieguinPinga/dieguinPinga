"""Oficina distribuida, etapa 1 (OFICINA.md): registro de trabajadores y comprobación de disponibilidad.

SIN inferencias: un Ollama falso por HTTP (solo se permiten lecturas: /api/version, /api/tags, /api/ps) y un «whois»
de Tailscale simulado. Base y carpeta propias; no se toca la base de la aplicación ni el laboratorio.
"""
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from constructor_ia import cli
from constructor_ia.oficina import registro as RG

from .fake_ollama import FakeOllama

REMOTO = "http://100.92.0.7:11435"


def whois_de(nodo="pulenta", nombre_dns="pulenta.tail0000.ts.net"):
    llamadas = []

    def whois(host):
        llamadas.append(host)
        return {"nodo": nodo, "nombre_dns": nombre_dns, "etiquetas": ["tag:trabajador"], "usuario": "operador"}
    whois.llamadas = llamadas
    return whois


class ClienteEspia:
    """Envuelve OllamaClient y registra qué métodos se usan (para probar que solo se lee)."""
    usados: list = []

    def __init__(self, url, destino):
        from constructor_ia.ollama import OllamaClient
        self.real, self.url = OllamaClient(destino), url

    def __getattr__(self, nombre):
        ClienteEspia.usados.append(nombre)
        return getattr(self.real, nombre)


class Reloj:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class ValidacionTests(unittest.TestCase):
    def test_only_tailnet_http_urls_with_explicit_port(self):
        self.assertEqual(RG.validar_url("http://100.92.0.7:11435/"), "http://100.92.0.7:11435")
        self.assertEqual(RG.validar_url("http://pulenta.tail0000.ts.net:11435"), "http://pulenta.tail0000.ts.net:11435")
        malas = {"https://100.92.0.7:11435": "http://", "http://100.92.0.7": "puerto",
                 "http://192.168.1.5:11435": "del tailnet", "http://8.8.8.8:11435": "del tailnet",
                 "http://ejemplo.com:11435": "del tailnet", "http://u:p@100.92.0.7:11435": "credenciales",
                 "http://100.92.0.7:11435/api/pull": "ruta", "http://127.0.0.1:11435": "--permitir-local"}
        for url, esperado in malas.items():
            with self.subTest(url=url):
                with self.assertRaisesRegex(RG.ErrorRegistro, esperado):
                    RG.validar_url(url)

    def test_loopback_only_for_tests_and_never_the_main_ollama(self):
        self.assertEqual(RG.validar_url("http://127.0.0.1:18000", permitir_local=True), "http://127.0.0.1:18000")
        with self.assertRaisesRegex(RG.ErrorRegistro, "Ollama principal"):
            RG.validar_url("http://127.0.0.1:11434", permitir_local=True)


class RegistroTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOllama(models=("gemma4:12b",))
        self.addCleanup(self.fake.close)
        self.dir = Path(tempfile.mkdtemp(prefix="cia-oficina-"))
        self.reloj = Reloj()
        self.whois = whois_de()
        ClienteEspia.usados = []
        # La URL registrada es del tailnet; el cliente espía la dirige al Ollama falso local.
        self.reg = RG.Registro(self.dir / "oficina.db", whois=self.whois,
                               cliente=lambda url: ClienteEspia(url, self.fake.url), reloj=self.reloj)
        self.addCleanup(self.reg.cerrar)

    def registrar(self, **k):
        return self.reg.registrar("pulenta", REMOTO, ["gemma4:12b"], nodo="pulenta",
                                  recursos={"gpu": "RTX 4070", "vram_gb": 8, "ram_gb": 32}, **k)

    def test_registration_checks_identity_and_fixes_mvp_capacity_and_permissions(self):
        t = self.registrar()
        self.assertEqual((t["estado"], t["capacidad"], t["permisos"], t["modelos"]),
                         ("registrado", 1, ["inferencia"], ["gemma4:12b"]))
        self.assertEqual(self.whois.llamadas, ["100.92.0.7"])
        self.assertEqual(t["identidad"]["etiquetas"], ["tag:trabajador"])
        self.assertFalse(t["disponible_ahora"], "registrado no es disponible: falta la comprobación")
        self.assertEqual(self.fake.requests, [], "registrar no llama a Ollama")

    def test_identity_mismatch_or_missing_node_is_rejected(self):
        with self.assertRaisesRegex(RG.ErrorRegistro, "la identidad no coincide"):
            self.reg.registrar("pulenta", REMOTO, ["gemma4:12b"], nodo="otra-maquina")
        with self.assertRaisesRegex(RG.ErrorRegistro, "falta --nodo"):
            self.reg.registrar("pulenta", REMOTO, ["gemma4:12b"])
        self.assertEqual(self.reg.trabajadores(), [])

    def test_the_short_dns_name_also_matches(self):
        reg = RG.Registro(self.dir / "otra.db", whois=whois_de(nodo="x", nombre_dns="pulenta.tail0000.ts.net."))
        self.addCleanup(reg.cerrar)
        self.assertEqual(reg.registrar("p", REMOTO, ["m"], nodo="pulenta")["estado"], "registrado")

    def test_duplicates_and_bad_ids_are_rejected(self):
        self.registrar()
        with self.assertRaisesRegex(RG.ErrorRegistro, "ya está registrado"):
            self.registrar()
        with self.assertRaisesRegex(RG.ErrorRegistro, "id del trabajador"):
            self.reg.registrar("Pulenta X", REMOTO, ["gemma4:12b"], nodo="pulenta")
        with self.assertRaisesRegex(RG.ErrorRegistro, "al menos un modelo"):
            self.reg.registrar("otra", REMOTO, [" "], nodo="pulenta")

    def test_availability_only_reads_and_records_the_check(self):
        self.registrar()
        t = self.reg.comprobar("pulenta")
        self.assertEqual((t["estado"], t["disponible_ahora"]), ("disponible", True))
        self.assertEqual(t["comprobacion"]["version"], "0.34.0")
        self.assertEqual(t["comprobacion"]["cargados"], [])
        self.assertEqual(sorted(set(ClienteEspia.usados)), ["loaded", "tags", "version"],
                         "solo /api/version, /api/tags y /api/ps")
        self.assertEqual(self.fake.requests, [], "ninguna inferencia (ningún POST)")
        c = self.reg.comprobaciones("pulenta")
        self.assertEqual((len(c), c[0]["disponible"], c[0]["modelos"]), (1, 1, ["gemma4:12b"]))

    def test_a_missing_model_makes_it_unavailable(self):
        self.reg.registrar("pulenta", REMOTO, ["gemma4:12b", "qwen3:8b"], nodo="pulenta")
        t = self.reg.comprobar("pulenta")
        self.assertEqual(t["estado"], "no_disponible")
        self.assertIn("faltan modelos declarados: qwen3:8b", t["motivo"])

    def test_a_disconnected_worker_is_unavailable_and_the_heartbeat_expires(self):
        self.registrar()
        self.assertTrue(self.reg.comprobar("pulenta")["disponible_ahora"])
        self.reloj.t += RG.LATIDO_S + 1
        t = self.reg.obtener("pulenta")
        self.assertEqual((t["estado"], t["latido_vigente"], t["disponible_ahora"]), ("disponible", False, False),
                         "sin una comprobación reciente no se lo considera disponible")
        self.fake.close()
        t = self.reg.comprobar("pulenta")
        self.assertEqual(t["estado"], "no_disponible")
        self.assertIn("Ollama no responde", t["motivo"])
        self.assertFalse(t["disponible_ahora"])

    def test_a_changed_identity_makes_it_unavailable(self):
        self.registrar()
        self.reg.whois = whois_de(nodo="intrusa", nombre_dns="intrusa.tail0000.ts.net")
        t = self.reg.comprobar("pulenta")
        self.assertEqual(t["estado"], "no_disponible")
        self.assertIn("la identidad cambió", t["motivo"])
        self.assertEqual(ClienteEspia.usados, [], "con otra identidad ni siquiera se le habla")

    def test_retired_workers_are_kept_with_their_history_and_can_register_again(self):
        self.registrar()
        self.reg.comprobar("pulenta")
        self.assertEqual(self.reg.retirar("pulenta")["estado"], "retirado")
        self.assertEqual(self.reg.trabajadores(), [])
        with self.assertRaisesRegex(RG.ErrorRegistro, "retirado"):
            self.reg.comprobar("pulenta")
        self.assertEqual(self.registrar()["estado"], "registrado")
        self.assertEqual(len(self.reg.comprobaciones("pulenta")), 1, "el historial se conserva")


class CliTests(unittest.TestCase):
    """«oficina registrar/estado/retirar» con un trabajador loopback de prueba (sin Tailscale, sin inferencias)."""

    def correr(self, *argv):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["--config", str(self.dir / "no-existe.toml"), "oficina", *argv,
                           "--carpeta", str(self.dir / "oficina")])
        return rc, out.getvalue(), err.getvalue()

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="cia-oficina-cli-"))
        self.fake = FakeOllama(models=("gemma4:12b",))
        self.addCleanup(self.fake.close)

    def test_register_check_and_retire(self):
        rc, out, _ = self.correr("registrar", "--id", "prueba", "--url", self.fake.url, "--modelo", "gemma4:12b",
                                 "--permitir-local", "--gpu", "RTX 4070", "--vram-gb", "8")
        self.assertEqual(rc, 0, out)
        self.assertIn("capacidad 1, permisos inferencia", out)
        rc, out, _ = self.correr("estado", "--json")
        lista = json.loads(out)
        self.assertEqual((rc, lista[0]["estado"], lista[0]["recursos"]),
                         (0, "disponible", {"gpu": "RTX 4070", "vram_gb": 8.0}))
        self.assertEqual(self.fake.requests, [])
        rc, out, _ = self.correr("estado")
        self.assertIn("prueba: disponible", out)
        self.fake.close()
        rc, out, _ = self.correr("estado", "--id", "prueba")
        self.assertEqual(rc, 3, "un trabajador no disponible da código 3")
        self.assertIn("no_disponible (Ollama no responde", out)
        self.assertEqual(self.correr("retirar", "--id", "prueba")[0], 0)
        self.assertIn("No hay trabajadores registrados", self.correr("estado")[1])

    def test_errors_are_reported_without_traceback(self):
        rc, _, err = self.correr("registrar", "--id", "x", "--url", "http://8.8.8.8:11435", "--modelo", "m")
        self.assertEqual(rc, 2)
        self.assertIn("oficina: el host tiene que ser del tailnet", err)

    def test_the_office_uses_its_own_database_only(self):
        self.correr("registrar", "--id", "prueba", "--url", self.fake.url, "--modelo", "gemma4:12b", "--permitir-local")
        archivos = sorted(p.name for p in self.dir.rglob("*") if p.is_file())
        self.assertTrue(all(n.startswith("oficina.db") for n in archivos), archivos)
        self.assertFalse((self.dir / "constructor.db").exists(), "no toca la base de la aplicación")


if __name__ == "__main__":
    unittest.main()
