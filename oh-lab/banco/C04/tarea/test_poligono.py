import unittest

from poligono import area


class Visibles(unittest.TestCase):
    def test_triangulo(self):
        self.assertAlmostEqual(area([(0, 0), (4, 0), (4, 3)]), 6.0)

    def test_degenerado(self):
        self.assertAlmostEqual(area([(0, 0), (1, 1), (2, 2)]), 0.0)

    def test_pocos_vertices(self):
        with self.assertRaises(ValueError):
            area([(0, 0), (1, 0)])


if __name__ == "__main__":
    unittest.main()
