"""Pruebas ocultas de P3. Solo las lee el verificador, al final; nunca entran al sandbox del agente."""
import sys
import unittest

sys.path.insert(0, "/work")
from paginacion import paginar  # noqa: E402


class Ocultas(unittest.TestCase):
    def test_vacia(self):
        self.assertEqual(paginar([], tamano=5, pagina=1), {"items": [], "pagina": 1, "paginas": 1, "total": 0})

    def test_tamano_mayor_que_el_total(self):
        self.assertEqual(paginar([1, 2], tamano=10, pagina=1)["paginas"], 1)

    def test_ultima_de_un_elemento(self):
        r = paginar(list("abcdefg"), tamano=3, pagina=3)
        self.assertEqual((r["items"], r["paginas"]), (["g"], 3))

    def test_fuera_de_rango(self):
        for pagina in (0, 4):
            with self.subTest(pagina=pagina), self.assertRaises(ValueError):
                paginar(list(range(7)), tamano=3, pagina=pagina)

    def test_cantidad_de_paginas(self):
        self.assertEqual(paginar(list(range(11)), tamano=5, pagina=1)["paginas"], 3)


if __name__ == "__main__":
    unittest.main()
