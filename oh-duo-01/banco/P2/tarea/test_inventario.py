import unittest

from inventario import Inventario


class Visibles(unittest.TestCase):
    def test_ejemplo(self):
        inv = Inventario()
        inv.agregar("tornillo", 10)
        inv.retirar("tornillo", 4)
        self.assertEqual(inv.stock("tornillo"), 6)

    def test_retiro_de_producto_desconocido(self):
        inv = Inventario()
        with self.assertRaises(ValueError):
            inv.retirar("tuerca", 1)
        self.assertEqual(inv.productos(), [])

    def test_cantidad_invalida(self):
        with self.assertRaises(ValueError):
            Inventario().agregar("x", 0)


if __name__ == "__main__":
    unittest.main()
