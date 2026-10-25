import unittest

from palabras import contar_palabras


class Visibles(unittest.TestCase):
    def test_ejemplo(self):
        self.assertEqual(contar_palabras("Hola hola, mundo"), {"hola": 2, "mundo": 1})

    def test_vacio(self):
        self.assertEqual(contar_palabras(""), {})

    def test_una_palabra(self):
        self.assertEqual(contar_palabras("sol"), {"sol": 1})


if __name__ == "__main__":
    unittest.main()
