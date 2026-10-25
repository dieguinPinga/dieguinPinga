import unittest

from imc import clasificar


class Visibles(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(clasificar(22), "normal")

    def test_limite_sobrepeso(self):
        self.assertEqual(clasificar(25), "sobrepeso")

    def test_obesidad(self):
        self.assertEqual(clasificar(31), "obesidad")


if __name__ == "__main__":
    unittest.main()
