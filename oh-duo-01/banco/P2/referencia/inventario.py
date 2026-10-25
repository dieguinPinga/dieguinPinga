"""Inventario de un depósito.

inv = Inventario()
inv.agregar("tornillo", 10)
inv.retirar("tornillo", 4)
inv.stock("tornillo") == 6

Las cantidades son enteros positivos (si no, ValueError). Retirar más de lo disponible produce ValueError
y no cambia nada. productos() devuelve, ordenados, los productos con stock mayor que cero. El stock de un
producto desconocido es 0.
"""


class Inventario:
    def __init__(self):
        self._stock = {}

    def agregar(self, producto, cantidad):
        _validar(cantidad)
        self._stock[producto] = self._stock.get(producto, 0) + cantidad

    def retirar(self, producto, cantidad):
        _validar(cantidad)
        disponible = self._stock.get(producto, 0)
        if cantidad > disponible:
            raise ValueError(f"stock insuficiente de {producto!r}")
        if cantidad == disponible:
            del self._stock[producto]
        else:
            self._stock[producto] = disponible - cantidad

    def stock(self, producto):
        return self._stock.get(producto, 0)

    def productos(self):
        return sorted(self._stock)


def _validar(cantidad):
    if isinstance(cantidad, bool) or not isinstance(cantidad, int) or cantidad <= 0:
        raise ValueError(f"cantidad inválida: {cantidad!r}")
