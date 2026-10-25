"""Pruebas ocultas de P6. Solo las lee el verificador, al final; nunca entran al sandbox del agente."""
import sys
import unittest

sys.path.insert(0, "/work")
from luhn import luhn_valido  # noqa: E402


class Ocultas(unittest.TestCase):
    def test_largo_par_valido(self):
        self.assertTrue(luhn_valido("4111 1111 1111 1111"))

    def test_largo_impar_valido(self):
        self.assertTrue(luhn_valido("79927398713"))

    def test_largo_par_invalido(self):
        self.assertFalse(luhn_valido("4111111111111112"))

    def test_dos_digitos(self):
        self.assertTrue(luhn_valido("59"))
        self.assertFalse(luhn_valido("95"))

    def test_muy_corto_o_vacio(self):
        for numero in ("", "0", "   7  "):
            with self.subTest(numero=numero), self.assertRaises(ValueError):
                luhn_valido(numero)


if __name__ == "__main__":
    unittest.main()
