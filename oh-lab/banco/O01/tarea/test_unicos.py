import unittest

from unicos import unicos


class Visibles(unittest.TestCase):
    def test_ejemplo(self):
        self.assertEqual(unicos([3, 1, 3, 2, 1]), [3, 1, 2])

    def test_sin_repetidos(self):
        self.assertEqual(unicos([1, 2, 3]), [1, 2, 3])

    def test_vacia(self):
        self.assertEqual(unicos([]), [])


if __name__ == "__main__":
    unittest.main()
