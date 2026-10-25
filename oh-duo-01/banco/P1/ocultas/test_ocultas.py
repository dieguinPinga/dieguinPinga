"""Pruebas ocultas de P1. Solo las lee el verificador, al final; nunca entran al sandbox del agente."""
import sys
import unittest

sys.path.insert(0, "/work")
from intervalos import unir_intervalos  # noqa: E402


class Ocultas(unittest.TestCase):
    def test_se_tocan(self):
        self.assertEqual(unir_intervalos([(1, 2), (2, 5)]), [(1, 5)])

    def test_desordenados(self):
        self.assertEqual(unir_intervalos([(8, 9), (1, 4), (3, 5)]), [(1, 5), (8, 9)])

    def test_varios_contenidos(self):
        self.assertEqual(unir_intervalos([(0, 20), (1, 2), (5, 7), (19, 25)]), [(0, 25)])

    def test_contenido_y_despues_otro(self):
        self.assertEqual(unir_intervalos([(1, 10), (2, 3), (4, 12)]), [(1, 12)])

    def test_vacio_y_unico(self):
        self.assertEqual(unir_intervalos([]), [])
        self.assertEqual(unir_intervalos([(3, 3)]), [(3, 3)])

    def test_no_modifica_la_entrada(self):
        entrada = [(4, 6), (1, 2)]
        unir_intervalos(entrada)
        self.assertEqual(entrada, [(4, 6), (1, 2)])


if __name__ == "__main__":
    unittest.main()
