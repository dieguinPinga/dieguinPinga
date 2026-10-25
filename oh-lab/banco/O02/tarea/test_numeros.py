import unittest

from numeros import leer_numeros


class Visibles(unittest.TestCase):
    def test_ejemplo(self):
        self.assertEqual(leer_numeros(["1", "# total", "", " 2.5 "]), [1.0, 2.5])

    def test_solo_numeros(self):
        self.assertEqual(leer_numeros(["3", "4"]), [3.0, 4.0])

    def test_vacia(self):
        self.assertEqual(leer_numeros([]), [])


if __name__ == "__main__":
    unittest.main()
