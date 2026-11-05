"""Icon decoding is restricted to safe formats and sizes; unreviewed imported actions never run;
keys awaiting review wear an amber badge."""
import os
import struct
import unittest
import zlib

from tests.helpers import ROOT, Deck, tmpdir

from PIL import Image  # noqa: E402

from core.bot import DeckBot  # noqa: E402
from core.render import AMBER, Renderer  # noqa: E402

MSG = "needs to be reviewed"


def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def huge_png(path, w=10000, h=10000):
    """A valid PNG header declaring w x h, with no pixel data (decoding would fail)."""
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)))


class Safety(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir()
        self.r = Renderer(Deck("Stream Deck XL"), ROOT, None, None)

    def path(self, name):
        return os.path.join(self.dir, name)

    def icon(self, path):
        return self.r._icon(path, 64)

    def test_eps_named_png_refused(self):
        p = self.path("evil.png")
        with open(p, "wb") as f:
            f.write(b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\nshowpage\n")
        self.assertIsNone(self.icon(p))

    def test_tiff_refused(self):
        p = self.path("a.png")
        Image.new("RGB", (8, 8), "red").save(p, format="TIFF")
        self.assertIsNone(self.icon(p))

    def test_svg_refused(self):
        p = self.path("a.svg")
        with open(p, "w") as f:
            f.write('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>')
        self.assertIsNone(self.icon(p))
        # the page still renders
        self.r.image({"label": "x", "icon": p})

    def test_oversized_png_refused_without_decoding(self):
        p = self.path("big.png")
        huge_png(p)
        self.assertIsNone(self.icon(p))
        self.assertIsNone(self.r._cover(p, 96, 96))
        self.r.image({"label": "x", "icon": p, "image": p})

    def test_normal_png_renders(self):
        p = self.path("ok.png")
        Image.new("RGBA", (32, 32), (255, 0, 0, 255)).save(p)
        ic = self.icon(p)
        self.assertIsNotNone(ic)
        self.assertEqual(ic.size, (32, 32))
        self.assertEqual(self.r.image({"icon": p}).getpixel((48, 48))[:3], (255, 0, 0))


class Review(unittest.TestCase):
    def setUp(self):
        self.bot = DeckBot.__new__(DeckBot)
        self.bot.actions = {}
        self.ran = []
        self.bot.actions["mark"] = self.ran.append
        self.pending = {"needs_review": {"imported_action": {"mark": "evil"}, "source": "s", "original_id": "1"}}

    def test_single_refused(self):
        with self.assertRaisesRegex(ValueError, MSG):
            self.bot.run_action(self.pending)
        self.assertEqual(self.ran, [])

    def test_second_in_list_runs_nothing(self):
        with self.assertRaisesRegex(ValueError, MSG):
            self.bot.run_action([{"mark": "first"}, self.pending])
        self.assertEqual(self.ran, [])

    def test_dict_key_refused(self):
        with self.assertRaisesRegex(ValueError, MSG):
            self.bot.run_action({"mark": "first", **self.pending})
        self.assertEqual(self.ran, [])

    def test_normal_still_runs(self):
        self.bot.run_action([{"mark": "a"}, {"mark": "b"}])
        self.assertEqual(self.ran, ["a", "b"])


class Badge(unittest.TestCase):
    def setUp(self):
        self.r = Renderer(Deck("Stream Deck XL"), ROOT, None, None)

    def corner(self, btn):
        img = self.r.image(btn)
        return img.getpixel((img.width - 12 + 5, 12))  # beside the "!" glyph

    def test_badge(self):
        plain = self.corner({"label": "x"})
        self.assertNotEqual(plain, AMBER[:3])
        pending = {"needs_review": {"imported_action": {"run": "x"}, "source": "s", "original_id": "1"}}
        for btn in ({"action": pending}, {"action": [{"run": "a"}, pending]}, {"needs_action": {"why": "x"}}):
            self.assertEqual(self.corner({"label": "x", **btn}), AMBER[:3])


if __name__ == "__main__":
    unittest.main()


class VolumeInjection(unittest.TestCase):
    def test_free_text_never_reaches_the_shell(self):
        from unittest import mock
        from modules import system
        mod = system.System(mock.Mock())
        with mock.patch.object(system, "sh") as sh:
            for bad in ("50%; rm -rf ~", "$(id)", "`id`", "50% && x", "loud"):
                with self.assertRaises(ValueError):
                    mod.volume(bad)
            sh.assert_not_called()
            mod.volume("150%")
            mod.volume("up")
        self.assertEqual([c.args[0] for c in sh.call_args_list],
                         ["pactl set-sink-volume @DEFAULT_SINK@ 150%", "pactl set-sink-volume @DEFAULT_SINK@ +5%"])
