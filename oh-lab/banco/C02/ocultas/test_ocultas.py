"""Evaluación reservada de C02. Nunca se monta en el sandbox del agente."""
import sys
import unittest

sys.path.insert(0, "/work")
from percentil import percentil  # noqa: E402


class Ocultas(unittest.TestCase):
    def test_minimo(self):
        self.assertEqual(percentil([15, 20, 35, 40, 50], 0), 15)

    def test_mediana(self):
        self.assertEqual(percentil([15, 20, 35, 40, 50], 50), 35)

    def test_desordenada(self):
        self.assertEqual(percentil([3, 1, 2, 4], 75), 3)

    def test_un_elemento(self):
        self.assertEqual(percentil([7], 30), 7)

    def test_fuera_de_rango(self):
        for p in (-1, 101):
            with self.subTest(p=p), self.assertRaises(ValueError):
                percentil([1, 2], p)


if __name__ == "__main__":
    unittest.main()
