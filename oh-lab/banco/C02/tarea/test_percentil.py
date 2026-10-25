import unittest

from percentil import percentil


class Visibles(unittest.TestCase):
    def test_ejemplo(self):
        self.assertEqual(percentil([15, 20, 35, 40, 50], 40), 20)

    def test_maximo(self):
        self.assertEqual(percentil([15, 20, 35, 40, 50], 100), 50)

    def test_vacia(self):
        with self.assertRaises(ValueError):
            percentil([], 50)


if __name__ == "__main__":
    unittest.main()
