import unittest

from tests.helpers import ROOT  # noqa: F401  (sandbox + sys.path)

from core import colors  # noqa: E402


class ParseColor(unittest.TestCase):
    def test_hex_forms(self):
        for text in ("#abc", "#aabbcc", "aabbcc", " #AABBCC "):
            self.assertEqual(colors.parse_color(text), (0xAA, 0xBB, 0xCC), text)

    def test_names_ignore_case_and_spaces(self):
        self.assertEqual(colors.parse_color("Light Blue"), colors.parse_color("lightblue"))
        self.assertEqual(colors.parse_color("PURPLE"), (0x80, 0, 0x80))
        self.assertEqual(colors.parse_color("rebeccapurple"), (0x66, 0x33, 0x99))

    def test_all_148_names_parse(self):
        self.assertEqual(len(colors.NAMES), 148)
        for name in colors.NAMES:
            colors.parse_color(name)

    def test_rejects(self):
        for bad in ("", "   ", "#12", "#gggggg", "#12345", "notacolor", "x" * 33, "#" + "0" * 40,
                    "red; rm -rf", None, 123, b"red", ["red"]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                colors.parse_color(bad)


class Conversions(unittest.TestCase):
    def test_to_hex(self):
        self.assertEqual(colors.to_hex((1, 2, 255)), "#0102ff")

    def test_obs_int(self):
        self.assertEqual(colors.obs_int(colors.parse_color("#112233")), 0xFF332211)
        self.assertEqual(colors.obs_int((0, 0, 0)), 0xFF000000)

    def test_canonical(self):
        self.assertEqual(colors.canonical("Light Blue"), "lightblue")
        self.assertEqual(colors.canonical("#ABC"), "#aabbcc")
        self.assertEqual(colors.canonical("AABBCC"), "#aabbcc")


if __name__ == "__main__":
    unittest.main()
