import unittest

from aplanar import aplanar


class Visibles(unittest.TestCase):
    def test_ejemplo(self):
        self.assertEqual(aplanar([[1, 2], [3], []]), [1, 2, 3])

    def test_vacia(self):
        self.assertEqual(aplanar([]), [])

    def test_una_sublista(self):
        self.assertEqual(aplanar([[4, 5]]), [4, 5])


if __name__ == "__main__":
    unittest.main()
