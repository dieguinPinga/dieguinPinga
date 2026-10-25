"""Validez del banco: cada original falla y cada referencia aprueba, con las mismas pruebas. Sin Ollama ni bwrap.

    python3 -m unittest discover -s tests -v      (desde oh-lab/)
"""
import difflib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent))
import lab  # noqa: E402

BANCO = AQUI.parent / "banco"
PY = sys.executable


def correr(modulo_py: Path, prueba: Path, ocultas: Path | None) -> subprocess.CompletedProcess:
    """Copia el módulo y la prueba visible a un directorio nuevo y corre las visibles, o las ocultas si se pasan
    (las ocultas buscan el módulo en /work, que no existe acá, y siguen con el directorio agregado antes)."""
    with tempfile.TemporaryDirectory() as t:
        shutil.copy(modulo_py, t)
        shutil.copy(prueba, t)
        if ocultas is None:
            args = ["-m", "unittest", "discover", "-s", t, "-p", prueba.name]
        else:
            args = ["-c", "import sys, unittest; sys.path.insert(0, sys.argv[1]); "
                          "unittest.main(module=None, argv=['x', 'discover', '-s', sys.argv[2], '-p', 'test_*.py'])",
                    t, str(ocultas)]
        return subprocess.run([PY, "-I", "-B", *args], capture_output=True, text=True, timeout=60)


class Banco(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.misiones = lab.cargar_banco(BANCO)

    def test_doce_misiones_con_la_distribucion_pedida(self):
        self.assertEqual(len(self.misiones), 12)
        cuenta = {}
        for m in self.misiones:
            cuenta[m["categoria"]] = cuenta.get(m["categoria"], 0) + 1
        self.assertEqual(cuenta, {"cálculo": 4, "sobrescritura": 4, "condición": 2, "omisión": 2})

    def test_programas_distintos(self):
        self.assertEqual(len({m["modulo"] for m in self.misiones}), 12)
        self.assertEqual(len({m["funcion"] for m in self.misiones}), 12)

    def test_original_falla_y_referencia_aprueba(self):
        for m in self.misiones:
            d, mod = m["dir"], m["modulo"]
            prueba, ocultas = d / "tarea" / f"test_{mod}.py", d / "ocultas"
            with self.subTest(mision=m["id"]):
                for nombre, ocu in (("visibles", None), ("ocultas", ocultas)):
                    orig = correr(d / "tarea" / f"{mod}.py", prueba, ocu)
                    ref = correr(d / "control" / f"{mod}.py", prueba, ocu)
                    self.assertNotEqual(orig.returncode, 0, f"{nombre}: el original aprueba")
                    self.assertNotIn("ImportError", orig.stderr, f"{nombre}: error de importación")
                    self.assertRegex(orig.stderr, r"Ran [1-9]")
                    self.assertEqual(ref.returncode, 0, f"{nombre}: la referencia falla\n{ref.stderr}")

    def test_un_solo_defecto_pequenio(self):
        """El original difiere de la referencia en una sola zona de 1 o 2 líneas."""
        for m in self.misiones:
            d, mod = m["dir"], m["modulo"]
            a = (d / "control" / f"{mod}.py").read_text().splitlines()
            b = (d / "tarea" / f"{mod}.py").read_text().splitlines()
            bloques = [g for g in difflib.SequenceMatcher(a=a, b=b).get_opcodes() if g[0] != "equal"]
            with self.subTest(mision=m["id"]):
                self.assertEqual(len(bloques), 1)
                tag, i1, i2, j1, j2 = bloques[0]
                self.assertLessEqual(max(i2 - i1, j2 - j1), 2)
                if m["categoria"] == "omisión":
                    self.assertEqual(tag, "delete")   # falta código: el original tiene menos líneas

    def test_visibles_y_ocultas_son_conjuntos_distintos(self):
        for m in self.misiones:
            d, mod = m["dir"], m["modulo"]
            vis = (d / "tarea" / f"test_{mod}.py").read_text()
            ocu = (d / "ocultas" / "test_ocultas.py").read_text()
            nombres = lambda s: {l.split("(")[0].strip() for l in s.splitlines() if l.strip().startswith("def test_")}
            with self.subTest(mision=m["id"]):
                self.assertFalse(nombres(vis) & nombres(ocu))
                self.assertNotIn("ocultas", vis.lower())

    def test_lo_visible_no_revela_la_categoria_ni_el_defecto(self):
        for m in self.misiones:
            d, mod = m["dir"], m["modulo"]
            texto = ((d / "tarea" / f"{mod}.py").read_text() + (d / "tarea" / f"test_{mod}.py").read_text()).lower()
            with self.subTest(mision=m["id"]):
                for palabra in ("defecto", "bug", "error de", m["categoria"], "sobrescrit", "omisi"):
                    self.assertNotIn(palabra, texto)

    def test_banco_invalido_se_rechaza(self):
        with tempfile.TemporaryDirectory() as t:
            shutil.copytree(BANCO, Path(t) / "b")
            shutil.rmtree(Path(t) / "b" / "C01" / "ocultas")
            with self.assertRaises(lab.BancoInvalido):
                lab.cargar_banco(Path(t) / "b")
        with tempfile.TemporaryDirectory() as t:
            shutil.copytree(BANCO, Path(t) / "b")
            shutil.rmtree(Path(t) / "b" / "K02")
            with self.assertRaises(lab.BancoInvalido):   # distribución incompleta
                lab.cargar_banco(Path(t) / "b")
            self.assertEqual(len(lab.cargar_banco(Path(t) / "b", completo=False)), 11)


if __name__ == "__main__":
    unittest.main()
