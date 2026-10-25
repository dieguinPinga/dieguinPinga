"""Carrera OH-DUO-03 → OH-DUO-04 sobre supervision.json, con el código real de los dos. Sin SDK.
   python3 -m unittest discover -s tests -p test_carrera.py -v

El Registro real de OH-DUO-03 escribe alertas.jsonl, la línea de cierre y supervision.json (grande, para que la
escritura lleve muchas llamadas al sistema), mientras vigilar() de OH-DUO-04 lo sigue en vivo y lee
supervision.json en cuanto aparece. Con la escritura atómica, ninguna repetición puede leer un JSON incompleto.
"""
import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent))
from cadena import importar  # noqa: E402
from ejecutar import hermano  # noqa: E402

OH2, OH3, OH4 = (hermano(n, a) for n, a in (("oh-duo-02", "estancamiento.py"), ("oh-duo-03", "observador.py"),
                                             ("oh-duo-04", "coordinador.py")))
O = importar("oh_duo_03_observador_carrera", OH3 / "observador.py")
C = importar("oh_duo_04_coordinador_carrera", OH4 / "coordinador.py")
E = O.cargar_detector(OH2)
U = E.cargar_umbrales(None)
REPETICIONES = 40


class Carrera(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="oh5-carrera-"))
        # un informe real de OH-DUO-03 (reproducción del fixture de Qwen), agrandado con un campo de relleno
        cls.informe = O.reproducir(OH2 / "tests" / "fixtures" / "repro-p1" / "ejecuciones" / "P1__qwen",
                                   cls.tmp / "base", E, U)
        cls.informe["relleno_de_prueba"] = ["á" * 80] * 40000
        cls.cfg = C.cargar_config(None)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def una_vuelta(self, i: int) -> dict:
        d = self.tmp / f"e{i}"
        reg = O.Registro(d)
        listo = threading.Event()

        def observador():
            listo.wait()
            reg.anotar("cierre", {"terminacion": {"tipo": "agotamiento_de_iteraciones"}, "veredicto": {}})
            reg.cerrar(self.informe)

        h = threading.Thread(target=observador)
        h.start()
        try:
            listo.set()
            return C.vigilar(d, self.tmp / f"s{i}", self.cfg, intervalo=0.001)
        finally:
            h.join()

    def test_el_coordinador_nunca_lee_un_informe_incompleto(self):
        errores = []
        for i in range(REPETICIONES):
            try:
                r = self.una_vuelta(i)
            except ValueError as e:
                errores.append(f"vuelta {i}: {e!r}"[:120])
                continue
            self.assertTrue(r["cierre_recibido"])
        self.assertEqual(errores, [])
        # y lo escrito es el informe completo, en el formato de siempre
        datos = (self.tmp / "e0" / "supervision.json").read_bytes()
        self.assertEqual(datos, json.dumps(self.informe, ensure_ascii=False, indent=1).encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
