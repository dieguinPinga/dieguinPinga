"""Evaluación reservada de O01. Nunca se monta en el sandbox del agente."""
import sys
import unittest

sys.path.insert(0, "/work")
from unicos import unicos  # noqa: E402


class Ocultas(unittest.TestCase):
    def test_todos_iguales(self):
        self.assertEqual(unicos(["a"] * 5), ["a"])

    def test_orden_de_primera_aparicion(self):
        self.assertEqual(unicos([5, 4, 5, 3, 4, 2]), [5, 4, 3, 2])

    def test_tuplas(self):
        self.assertEqual(unicos([(1, 2), (1, 2), (2, 1)]), [(1, 2), (2, 1)])

    def test_textos(self):
        self.assertEqual(unicos(["b", "a", "b", "c", "a"]), ["b", "a", "c"])


if __name__ == "__main__":
    unittest.main()
