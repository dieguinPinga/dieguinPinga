"""Evaluación reservada de K02. Nunca se monta en el sandbox del agente."""
import sys
import unittest

sys.path.insert(0, "/work")
from imc import clasificar  # noqa: E402


class Ocultas(unittest.TestCase):
    def test_limites(self):
        casos = {17: "bajo", 18.5: "normal", 24.99: "normal", 25.0: "sobrepeso", 29.99: "sobrepeso",
                 30: "obesidad"}
        for imc, esperado in casos.items():
            with self.subTest(imc=imc):
                self.assertEqual(clasificar(imc), esperado)

    def test_invalidos(self):
        for imc in (0, -3):
            with self.subTest(imc=imc), self.assertRaises(ValueError):
                clasificar(imc)


if __name__ == "__main__":
    unittest.main()
