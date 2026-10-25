"""Pruebas ocultas de P2. Solo las lee el verificador, al final; nunca entran al sandbox del agente."""
import sys
import unittest

sys.path.insert(0, "/work")
from inventario import Inventario  # noqa: E402


class Ocultas(unittest.TestCase):
    def test_retiro_excesivo_no_cambia_nada(self):
        inv = Inventario()
        inv.agregar("a", 3)
        with self.assertRaises(ValueError):
            inv.retirar("a", 5)
        self.assertEqual((inv.stock("a"), inv.productos()), (3, ["a"]))

    def test_desconocido_no_aparece_despues_de_fallar(self):
        inv = Inventario()
        inv.agregar("b", 1)
        with self.assertRaises(ValueError):
            inv.retirar("c", 2)
        self.assertEqual(inv.productos(), ["b"])
        self.assertEqual(inv.stock("c"), 0)

    def test_retirar_todo_lo_quita(self):
        inv = Inventario()
        inv.agregar("z", 2)
        inv.retirar("z", 2)
        self.assertEqual(inv.productos(), [])

    def test_tipos_invalidos(self):
        inv = Inventario()
        for c in (True, 1.5, -1, "3"):
            with self.subTest(c=c), self.assertRaises(ValueError):
                inv.agregar("x", c)

    def test_orden(self):
        inv = Inventario()
        for p in ("c", "a", "b"):
            inv.agregar(p, 1)
        self.assertEqual(inv.productos(), ["a", "b", "c"])


if __name__ == "__main__":
    unittest.main()
