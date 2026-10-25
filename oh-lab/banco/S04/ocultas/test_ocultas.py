"""Evaluación reservada de S04. Nunca se monta en el sandbox del agente."""
import sys
import unittest

sys.path.insert(0, "/work")
from config import fusionar  # noqa: E402


class Ocultas(unittest.TestCase):
    def test_tres_niveles(self):
        base = {"a": {"b": {"c": 1, "d": 2}}}
        self.assertEqual(fusionar(base, {"a": {"b": {"d": 3}}}), {"a": {"b": {"c": 1, "d": 3}}})

    def test_valor_simple_reemplaza_dict(self):
        self.assertEqual(fusionar({"a": {"x": 1}}, {"a": 5}), {"a": 5})

    def test_dict_reemplaza_valor_simple(self):
        self.assertEqual(fusionar({"a": 5}, {"a": {"x": 1}}), {"a": {"x": 1}})

    def test_no_modifica_los_originales(self):
        base, extra = {"s": {"x": 1}}, {"s": {"y": 2}}
        fusionar(base, extra)
        self.assertEqual((base, extra), ({"s": {"x": 1}}, {"s": {"y": 2}}))

    def test_claves_nuevas_anidadas(self):
        self.assertEqual(fusionar({"s": {"x": 1}}, {"s": {"y": 2}, "t": 3}), {"s": {"x": 1, "y": 2}, "t": 3})


if __name__ == "__main__":
    unittest.main()
