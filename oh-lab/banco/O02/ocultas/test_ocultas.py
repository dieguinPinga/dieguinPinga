"""Evaluación reservada de O02. Nunca se monta en el sandbox del agente."""
import sys
import unittest

sys.path.insert(0, "/work")
from numeros import leer_numeros  # noqa: E402


class Ocultas(unittest.TestCase):
    def test_comentario_con_espacios(self):
        self.assertEqual(leer_numeros(["   # nota", "7"]), [7.0])

    def test_solo_comentarios(self):
        self.assertEqual(leer_numeros(["#a", "#b"]), [])

    def test_formatos(self):
        self.assertEqual(leer_numeros(["-3", "1e3", "\t0.5\n"]), [-3.0, 1000.0, 0.5])

    def test_no_numerico(self):
        with self.assertRaises(ValueError):
            leer_numeros(["abc"])


if __name__ == "__main__":
    unittest.main()
