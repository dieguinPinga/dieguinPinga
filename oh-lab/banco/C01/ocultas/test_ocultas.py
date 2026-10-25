"""Evaluación reservada de C01. Nunca se monta en el sandbox del agente."""
import sys
import unittest

sys.path.insert(0, "/work")
from promedio import promedio_ponderado  # noqa: E402


class Ocultas(unittest.TestCase):
    def test_un_valor(self):
        self.assertAlmostEqual(promedio_ponderado([5], [2]), 5.0)

    def test_pesos_cero_parciales(self):
        self.assertAlmostEqual(promedio_ponderado([1, 2, 3], [0, 0, 1]), 3.0)

    def test_pesos_decimales(self):
        self.assertAlmostEqual(promedio_ponderado([4, 8], [0.25, 0.75]), 7.0)

    def test_invalidos(self):
        for v, p in (([], []), ([1], [-1]), ([1, 2], [0, 0])):
            with self.subTest(v=v, p=p), self.assertRaises(ValueError):
                promedio_ponderado(v, p)


if __name__ == "__main__":
    unittest.main()
