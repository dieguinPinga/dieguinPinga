import unittest

from grupos import agrupar


class Visibles(unittest.TestCase):
    def test_ejemplo(self):
        regs = [{"t": "a", "v": 1}, {"t": "b", "v": 2}, {"t": "a", "v": 3}]
        self.assertEqual(agrupar(regs, "t"),
                         {"a": [{"t": "a", "v": 1}, {"t": "a", "v": 3}], "b": [{"t": "b", "v": 2}]})

    def test_vacio(self):
        self.assertEqual(agrupar([], "t"), {})

    def test_sin_clave(self):
        self.assertEqual(agrupar([{"x": 1}], "t"), {})


if __name__ == "__main__":
    unittest.main()
