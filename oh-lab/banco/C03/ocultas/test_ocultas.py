"""Evaluación reservada de C03. Nunca se monta en el sandbox del agente."""
import sys
import unittest

sys.path.insert(0, "/work")
from reloj import a_hms  # noqa: E402


class Ocultas(unittest.TestCase):
    def test_hora_exacta(self):
        self.assertEqual(a_hms(3600), "01:00:00")

    def test_fin_del_dia(self):
        self.assertEqual(a_hms(86399), "23:59:59")

    def test_muchas_horas(self):
        self.assertEqual(a_hms(360000), "100:00:00")

    def test_cero(self):
        self.assertEqual(a_hms(0), "00:00:00")

    def test_negativo(self):
        with self.assertRaises(ValueError):
            a_hms(-1)


if __name__ == "__main__":
    unittest.main()
