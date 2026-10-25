"""Pruebas ocultas de P4. Solo las lee el verificador, al final; nunca entran al sandbox del agente."""
import sys
import unittest

sys.path.insert(0, "/work")
from rle import codificar, decodificar  # noqa: E402


class Ocultas(unittest.TestCase):
    def test_ida_y_vuelta_larga(self):
        texto = "x" * 25 + "y" + "z" * 10
        self.assertEqual(decodificar(codificar(texto)), texto)

    def test_varias_de_dos_cifras(self):
        self.assertEqual(decodificar("10x2y11z"), "x" * 10 + "yy" + "z" * 11)

    def test_codigos_invalidos(self):
        for codigo in ("a", "3", "2a4"):
            with self.subTest(codigo=codigo), self.assertRaises(ValueError):
                decodificar(codigo)

    def test_vacio(self):
        self.assertEqual((codificar(""), decodificar("")), ("", ""))

    def test_espacios(self):
        self.assertEqual(codificar("  a"), "2 1a")


if __name__ == "__main__":
    unittest.main()
