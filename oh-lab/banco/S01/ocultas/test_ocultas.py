"""Evaluación reservada de S01. Nunca se monta en el sandbox del agente."""
import sys
import unittest

sys.path.insert(0, "/work")
from palabras import contar_palabras  # noqa: E402


class Ocultas(unittest.TestCase):
    def test_repetidas(self):
        self.assertEqual(contar_palabras("a a a b"), {"a": 3, "b": 1})

    def test_acentos_y_enie(self):
        self.assertEqual(contar_palabras("Ñandú ñandú ÁRBOL árbol árbol"), {"ñandú": 2, "árbol": 3})

    def test_numeros_ignorados(self):
        self.assertEqual(contar_palabras("3 gatos y 3 gatos"), {"gatos": 2, "y": 1})

    def test_puntuacion(self):
        self.assertEqual(contar_palabras("¡sí! ¿sí? no."), {"sí": 2, "no": 1})


if __name__ == "__main__":
    unittest.main()
