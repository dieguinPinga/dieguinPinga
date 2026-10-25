"""Evaluación reservada de K01. Nunca se monta en el sandbox del agente."""
import sys
import unittest

sys.path.insert(0, "/work")
from calendario import es_bisiesto  # noqa: E402


class Ocultas(unittest.TestCase):
    def test_divisible_por_400(self):
        self.assertTrue(es_bisiesto(2000))
        self.assertTrue(es_bisiesto(1600))

    def test_siglos(self):
        for anio in (1700, 1800, 2100, 2200):
            with self.subTest(anio=anio):
                self.assertFalse(es_bisiesto(anio))

    def test_otros(self):
        self.assertTrue(es_bisiesto(4))
        self.assertTrue(es_bisiesto(1996))
        self.assertFalse(es_bisiesto(2022))


if __name__ == "__main__":
    unittest.main()
