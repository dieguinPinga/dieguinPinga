"""Evaluación reservada de S02. Nunca se monta en el sandbox del agente."""
import sys
import unittest

sys.path.insert(0, "/work")
from grupos import agrupar  # noqa: E402


class Ocultas(unittest.TestCase):
    def test_un_solo_grupo(self):
        regs = [{"k": 1, "n": i} for i in range(4)]
        self.assertEqual(agrupar(regs, "k"), {1: regs})

    def test_orden_dentro_del_grupo(self):
        regs = [{"k": "x", "n": 3}, {"k": "y", "n": 1}, {"k": "x", "n": 1}, {"k": "x", "n": 2}]
        self.assertEqual([r["n"] for r in agrupar(regs, "k")["x"]], [3, 1, 2])

    def test_mezcla_con_registros_sin_clave(self):
        regs = [{"k": 1}, {"z": 0}, {"k": 1}]
        self.assertEqual(agrupar(regs, "k"), {1: [{"k": 1}, {"k": 1}]})


if __name__ == "__main__":
    unittest.main()
