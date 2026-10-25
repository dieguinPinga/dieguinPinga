import unittest

from luhn import luhn_valido


class Visibles(unittest.TestCase):
    def test_ejemplo(self):
        self.assertTrue(luhn_valido("4539 3195 0343 6467"))

    def test_invalido(self):
        self.assertFalse(luhn_valido("8273 1232 7352 0569"))

    def test_caracteres_invalidos(self):
        with self.assertRaises(ValueError):
            luhn_valido("4539-3195")


if __name__ == "__main__":
    unittest.main()
