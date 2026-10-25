import unittest

from reloj import a_hms


class Visibles(unittest.TestCase):
    def test_ejemplo(self):
        self.assertEqual(a_hms(3725), "01:02:05")

    def test_menos_de_un_minuto(self):
        self.assertEqual(a_hms(59), "00:00:59")

    def test_minutos(self):
        self.assertEqual(a_hms(600), "00:10:00")


if __name__ == "__main__":
    unittest.main()
