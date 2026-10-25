import unittest

from config import fusionar


class Visibles(unittest.TestCase):
    def test_ejemplo(self):
        self.assertEqual(fusionar({"db": {"host": "a", "port": 1}}, {"db": {"port": 2}}),
                         {"db": {"host": "a", "port": 2}})

    def test_claves_planas(self):
        self.assertEqual(fusionar({"a": 1, "b": 2}, {"b": 3}), {"a": 1, "b": 3})

    def test_vacios(self):
        self.assertEqual(fusionar({}, {}), {})


if __name__ == "__main__":
    unittest.main()
