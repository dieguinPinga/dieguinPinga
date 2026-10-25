"""Pruebas ocultas de P5. Solo las lee el verificador, al final; nunca entran al sandbox del agente."""
import sys
import unittest

sys.path.insert(0, "/work")
from posiciones import tabla  # noqa: E402


class Ocultas(unittest.TestCase):
    def test_desempate_por_diferencia(self):
        partidos = [("A", 3, "C", 0), ("B", 1, "C", 0)]
        self.assertEqual([f["equipo"] for f in tabla(partidos)], ["A", "B", "C"])

    def test_diferencia_del_visitante(self):
        filas = {f["equipo"]: f for f in tabla([("A", 1, "B", 4), ("C", 2, "B", 2)])}
        self.assertEqual((filas["B"]["gf"], filas["B"]["gc"], filas["B"]["dg"], filas["B"]["pts"]), (6, 3, 3, 4))

    def test_desempate_por_goles_a_favor(self):
        partidos = [("A", 3, "B", 3), ("C", 1, "D", 1)]
        self.assertEqual([f["equipo"] for f in tabla(partidos)], ["A", "B", "C", "D"])

    def test_desempate_por_nombre(self):
        self.assertEqual([f["equipo"] for f in tabla([("Zeta", 1, "Alfa", 1)])], ["Alfa", "Zeta"])

    def test_partidos_jugados(self):
        filas = {f["equipo"]: f for f in tabla([("A", 1, "B", 0), ("B", 2, "A", 2), ("C", 0, "A", 1)])}
        self.assertEqual((filas["A"]["pj"], filas["A"]["pts"], filas["A"]["gc"]), (3, 7, 2))


if __name__ == "__main__":
    unittest.main()
