"""Evaluación reservada de S03. Nunca se monta en el sandbox del agente."""
import sys
import unittest

sys.path.insert(0, "/work")
from aplanar import aplanar  # noqa: E402


class Ocultas(unittest.TestCase):
    def test_dos_sublistas(self):
        self.assertEqual(aplanar([[1], [2]]), [1, 2])

    def test_un_solo_nivel(self):
        self.assertEqual(aplanar([[[1]], [2, [3]]]), [[1], 2, [3]])

    def test_no_modifica_la_entrada(self):
        entrada = [[1], [2, 3]]
        salida = aplanar(entrada)
        salida.append(9)
        self.assertEqual(entrada, [[1], [2, 3]])

    def test_textos(self):
        self.assertEqual(aplanar([["a"], [], ["b", "c"]]), ["a", "b", "c"])


if __name__ == "__main__":
    unittest.main()
