import unittest

from intervalos import unir_intervalos


class Visibles(unittest.TestCase):
    def test_ejemplo(self):
        self.assertEqual(unir_intervalos([(1, 3), (2, 6), (8, 10)]), [(1, 6), (8, 10)])

    def test_intervalo_contenido(self):
        self.assertEqual(unir_intervalos([(1, 10), (2, 3)]), [(1, 10)])

    def test_invalido(self):
        with self.assertRaises(ValueError):
            unir_intervalos([(5, 1)])


if __name__ == "__main__":
    unittest.main()
