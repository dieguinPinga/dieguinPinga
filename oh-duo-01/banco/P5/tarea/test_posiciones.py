import unittest

from posiciones import tabla


class Visibles(unittest.TestCase):
    def test_un_partido(self):
        self.assertEqual(tabla([("A", 2, "B", 1)]), [
            {"equipo": "A", "pj": 1, "pts": 3, "gf": 2, "gc": 1, "dg": 1},
            {"equipo": "B", "pj": 1, "pts": 0, "gf": 1, "gc": 2, "dg": -1},
        ])

    def test_empate_suma_uno(self):
        self.assertEqual([f["pts"] for f in tabla([("A", 0, "B", 0)])], [1, 1])

    def test_sin_partidos(self):
        self.assertEqual(tabla([]), [])


if __name__ == "__main__":
    unittest.main()
