import unittest

from promedio import promedio_ponderado


class Visibles(unittest.TestCase):
    def test_ejemplo(self):
        self.assertAlmostEqual(promedio_ponderado([10, 20], [1, 3]), 17.5)

    def test_pesos_iguales(self):
        self.assertAlmostEqual(promedio_ponderado([2, 4], [1, 1]), 3.0)

    def test_largos_distintos(self):
        with self.assertRaises(ValueError):
            promedio_ponderado([1, 2], [1])


if __name__ == "__main__":
    unittest.main()
