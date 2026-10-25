"""Pruebas reservadas. Nunca se montan en el sandbox del agente.

correr.sh las ejecuta al final, en otro sandbox sin red, sobre el resultado
montado en solo lectura en /work.
"""
import sys
import unittest

sys.path.insert(0, "/work")
from duracion import a_segundos  # noqa: E402


class PruebasOcultas(unittest.TestCase):
    def test_solo_horas(self):
        self.assertEqual(a_segundos("2h"), 7200)

    def test_solo_minutos(self):
        self.assertEqual(a_segundos("90m"), 5400)

    def test_minutos_y_segundos(self):
        self.assertEqual(a_segundos("2m30s"), 150)

    def test_tres_unidades(self):
        self.assertEqual(a_segundos("1h0m5s"), 3605)

    def test_espacios(self):
        self.assertEqual(a_segundos("  3m "), 180)

    def test_invalidos(self):
        for texto in ("", "5x", "m", "1h 30m"):
            with self.subTest(texto=texto), self.assertRaises(ValueError):
                a_segundos(texto)


if __name__ == "__main__":
    unittest.main()
