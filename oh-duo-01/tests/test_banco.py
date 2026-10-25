"""Banco, roles y ejecutor de OH-DUO-01. Sin Ollama ni bwrap.   python3 -m unittest discover -s tests -v"""
import ast
import difflib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

AQUI = Path(__file__).resolve().parent
RAIZ = AQUI.parent
sys.path.insert(0, str(RAIZ))
import duo  # noqa: E402

BANCO = RAIZ / "banco"


def correr(modulo_py: Path, prueba: Path, ocultas: Path | None) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as t:
        shutil.copy(modulo_py, t)
        shutil.copy(prueba, t)
        rc, salida = duo.correr_pruebas(Path(t), ocultas, prueba.name, bwrap=None)
        return rc, salida


class Banco(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.problemas = duo.cargar_banco(BANCO)

    def test_seis_problemas_distintos(self):
        self.assertEqual([p["id"] for p in self.problemas], ["P1", "P2", "P3", "P4", "P5", "P6"])
        self.assertEqual(len({p["modulo"] for p in self.problemas}), 6)

    def test_original_falla_y_referencia_aprueba(self):
        for p in self.problemas:
            d, mod = p["dir"], p["modulo"]
            prueba = d / "tarea" / f"test_{mod}.py"
            with self.subTest(problema=p["id"]):
                for nombre, ocu in (("visibles", None), ("ocultas", d / "ocultas")):
                    rc_o, out_o = correr(d / "tarea" / f"{mod}.py", prueba, ocu)
                    rc_r, out_r = correr(d / "referencia" / f"{mod}.py", prueba, ocu)
                    self.assertNotEqual(rc_o, 0, f"{nombre}: el original aprueba")
                    self.assertRegex(out_o, r"Ran [1-9]")
                    self.assertNotIn("ImportError", out_o)
                    self.assertEqual(rc_r, 0, f"{nombre}: la referencia falla\n{out_r}")

    def test_un_defecto_pequenio(self):
        for p in self.problemas:
            d, mod = p["dir"], p["modulo"]
            a = (d / "referencia" / f"{mod}.py").read_text().splitlines()
            b = (d / "tarea" / f"{mod}.py").read_text().splitlines()
            bloques = [g for g in difflib.SequenceMatcher(a=a, b=b).get_opcodes() if g[0] != "equal"]
            with self.subTest(problema=p["id"]):
                self.assertEqual(len(bloques), 1)
                self.assertLessEqual(max(bloques[0][2] - bloques[0][1], bloques[0][4] - bloques[0][3]), 2)

    def test_visibles_y_ocultas_no_comparten_casos(self):
        for p in self.problemas:
            nombres = lambda f: {n.name for n in ast.walk(ast.parse(f.read_text())) if isinstance(n, ast.FunctionDef)}
            with self.subTest(problema=p["id"]):
                self.assertFalse(nombres(p["dir"] / "tarea" / f"test_{p['modulo']}.py")
                                 & nombres(p["dir"] / "ocultas" / "test_ocultas.py"))

    def test_lo_visible_no_menciona_el_defecto_ni_las_ocultas(self):
        for p in self.problemas:
            texto = "".join(f.read_text() for f in (p["dir"] / "tarea").iterdir()).lower()
            with self.subTest(problema=p["id"]):
                for palabra in ("oculta", "defecto", "bug", "referencia"):
                    self.assertNotIn(palabra, texto)
                self.assertFalse(duo.literales_ocultos(p) & set(texto.split()))

    def test_no_reutiliza_el_banco_anterior(self):
        anterior = RAIZ.parent / "oh-lab" / "banco"
        if not anterior.is_dir():
            self.skipTest("oh-lab/banco no está al lado")
        viejos = {json.loads((d / "mision.json").read_text())["modulo"] for d in anterior.iterdir() if d.is_dir()}
        funciones_viejas = {json.loads((d / "mision.json").read_text())["funcion"] for d in anterior.iterdir() if d.is_dir()}
        for p in self.problemas:
            arbol = ast.parse((p["dir"] / "tarea" / f"{p['modulo']}.py").read_text())
            definidas = {n.name for n in ast.walk(arbol) if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
            with self.subTest(problema=p["id"]):
                self.assertNotIn(p["modulo"], viejos)
                self.assertFalse(definidas & funciones_viejas)


class RolesYEjecutor(unittest.TestCase):
    def test_roles_difieren_en_metodo_no_en_informacion(self):
        textos = {k: (RAIZ / "roles" / m["rol"]).read_text() for k, m in duo.MODELOS.items()}
        largos = [len(t.split()) for t in textos.values()]
        self.assertLessEqual(max(largos) - min(largos), 8)
        for k, t in textos.items():
            with self.subTest(rol=k):
                self.assertIn("{CODIGO}", t)
                for p in duo.cargar_banco(BANCO):   # ninguna pista de un problema concreto
                    self.assertNotIn(p["modulo"], t)

    def test_ejecutor_sin_ocultas_ni_referencia(self):
        correr_sh = (RAIZ / "ejecutor" / "correr.sh").read_text()
        for prohibido in ("/ocultas /ocultas", "ocultas.exit", "ocultas.txt", "$BASE/control", "referencia.",
                          "5_ocultas_pasan"):
            self.assertNotIn(prohibido, correr_sh)
        self.assertIn('--ro-bind "$BASE/enfoque.txt" /opt/oh/enfoque.txt', correr_sh)

    def test_agente_solo_agrega_el_enfoque(self):
        original = RAIZ.parent / "oh-lab" / "ejecutor" / "agente.py"
        if not original.is_file():
            self.skipTest("oh-lab/ejecutor no está al lado")
        def sentencias(p):   # el módulo sin su docstring, sentencia por sentencia
            cuerpo = ast.parse(p.read_text()).body
            return [ast.dump(s) for s in cuerpo[1:]] if isinstance(cuerpo[0], ast.Expr) else [ast.dump(s) for s in cuerpo]
        antes, despues = sentencias(original), sentencias(RAIZ / "ejecutor" / "agente.py")
        ops = [o for o in difflib.SequenceMatcher(a=antes, b=despues).get_opcodes() if o[0] != "equal"]
        self.assertEqual([(o[0], o[2] - o[1], o[4] - o[3]) for o in ops], [("insert", 0, 2)])
        nuevas = ast.parse((RAIZ / "ejecutor" / "agente.py").read_text()).body[1:][ops[0][3]:ops[0][4]]
        self.assertEqual([n.targets[0].id for n in nuevas], ["ENFOQUE", "TAREA"])


if __name__ == "__main__":
    unittest.main()
