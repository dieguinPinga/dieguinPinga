import unittest

from duracion import a_segundos


class PruebasVisibles(unittest.TestCase):
    def test_horas_y_minutos(self):
        self.assertEqual(a_segundos("1h30m"), 5400)

    def test_segundos(self):
        self.assertEqual(a_segundos("45s"), 45)

    def test_invalido(self):
        with self.assertRaises(ValueError):
            a_segundos("abc")


if __name__ == "__main__":
    unittest.main()
