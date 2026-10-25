"""Evaluación reservada de C04. Nunca se monta en el sandbox del agente."""
import sys
import unittest

sys.path.insert(0, "/work")
from poligono import area  # noqa: E402


class Ocultas(unittest.TestCase):
    def test_cuadrado(self):
        self.assertAlmostEqual(area([(0, 0), (2, 0), (2, 2), (0, 2)]), 4.0)

    def test_sentido_horario(self):
        self.assertAlmostEqual(area([(0, 0), (0, 2), (3, 2), (3, 0)]), 6.0)

    def test_no_convexo(self):
        self.assertAlmostEqual(area([(0, 0), (2, 0), (2, 1), (1, 1), (1, 2), (0, 2)]), 3.0)

    def test_desplazado(self):
        self.assertAlmostEqual(area([(10, 10), (11, 10), (11, 11)]), 0.5)


if __name__ == "__main__":
    unittest.main()
