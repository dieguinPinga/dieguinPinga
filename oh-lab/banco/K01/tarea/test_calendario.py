import unittest

from calendario import es_bisiesto


class Visibles(unittest.TestCase):
    def test_comun_divisible_por_4(self):
        self.assertTrue(es_bisiesto(2024))

    def test_siglo_no_bisiesto(self):
        self.assertFalse(es_bisiesto(1900))

    def test_impar(self):
        self.assertFalse(es_bisiesto(2023))


if __name__ == "__main__":
    unittest.main()
