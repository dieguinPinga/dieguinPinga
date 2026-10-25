import unittest

from rle import codificar, decodificar


class Visibles(unittest.TestCase):
    def test_ejemplo(self):
        self.assertEqual(codificar("aaabcc"), "3a1b2c")
        self.assertEqual(decodificar("3a1b2c"), "aaabcc")

    def test_corrida_larga(self):
        self.assertEqual(decodificar("12a"), "a" * 12)

    def test_digitos_en_el_texto(self):
        with self.assertRaises(ValueError):
            codificar("a1")


if __name__ == "__main__":
    unittest.main()
