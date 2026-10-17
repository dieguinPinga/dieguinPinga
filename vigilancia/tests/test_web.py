import unittest

from vigd import auth
from vigd.web import parse_range


class AuthTest(unittest.TestCase):
    def test_password(self):
        h = auth.hash_password("una clave larga", iterations=1000)
        self.assertTrue(auth.verify_password("una clave larga", h))
        self.assertFalse(auth.verify_password("otra", h))
        self.assertFalse(auth.verify_password("x", "basura"))

    def test_token(self):
        s = b"k" * 32
        tok = auth.make_token(s, "diego", 1)
        self.assertTrue(auth.check_token(s, tok, "diego"))
        self.assertFalse(auth.check_token(s, tok, "otro"))
        self.assertFalse(auth.check_token(b"z" * 32, tok, "diego"))
        self.assertFalse(auth.check_token(s, tok[:-1] + ("0" if tok[-1] != "0" else "1"), "diego"))
        self.assertFalse(auth.check_token(s, auth.make_token(s, "diego", -1), "diego"))
        self.assertFalse(auth.check_token(s, "nada", "diego"))


class RangeTest(unittest.TestCase):
    def test_parse(self):
        self.assertIsNone(parse_range(None, 100))
        self.assertEqual(parse_range("bytes=0-9", 100), (0, 9))
        self.assertEqual(parse_range("bytes=90-", 100), (90, 99))
        self.assertEqual(parse_range("bytes=-10", 100), (90, 99))
        self.assertEqual(parse_range("bytes=0-1000", 100), (0, 99))
        self.assertEqual(parse_range("bytes=100-", 100), "invalid")
        self.assertIsNone(parse_range("bytes=a-b", 100))


if __name__ == "__main__":
    unittest.main()
