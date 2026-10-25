import unittest

from paginacion import paginar


class Visibles(unittest.TestCase):
    def test_ejemplo(self):
        self.assertEqual(paginar(list(range(10)), tamano=4, pagina=3),
                         {"items": [8, 9], "pagina": 3, "paginas": 3, "total": 10})

    def test_division_exacta(self):
        self.assertEqual(paginar(list(range(8)), tamano=4, pagina=2)["items"], [4, 5, 6, 7])

    def test_tamano_invalido(self):
        with self.assertRaises(ValueError):
            paginar([1], tamano=0, pagina=1)


if __name__ == "__main__":
    unittest.main()
