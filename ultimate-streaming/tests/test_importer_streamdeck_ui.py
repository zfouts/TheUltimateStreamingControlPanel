"""core/importers/streamdeck_ui.py: ~/.streamdeck_ui.json (v1 and v2) → our setup / config."""
import json
import os
import tempfile
import unittest

from tests.helpers import EXAMPLE, SANDBOX

import yaml  # noqa: E402
from PIL import Image  # noqa: E402

from core import importer  # noqa: E402
from core.importers import KINDS, streamdeck_ui, to_config  # noqa: E402
from core.importers._common import ImportError_  # noqa: E402
from core.paths import state_dir  # noqa: E402

CONFIG = os.path.join(SANDBOX, ".streamdeck_ui.json")


def png(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new("RGBA", (96, 96), (200, 0, 0, 255)).save(path, format="PNG")


def v2(**st):
    return {"state": 0, "states": {"0": st}}


def write(path, doc):
    with open(path, "w") as f:
        f.write(doc if isinstance(doc, str) else json.dumps(doc))
    return path


def full_deck(buttons_by_page, count=15):
    """A deck whose pages list all `count` buttons (empty ones as {}), like the real file."""
    pages = {}
    for p, buttons in buttons_by_page.items():
        pages[str(p)] = {str(i): buttons.get(i, v2()) for i in range(count)}
    return {"buttons": pages, "brightness": 80, "page": 0}


def build():
    icon = os.path.join(SANDBOX, "icons", "obs.png")
    png(icon)
    other = tempfile.mkdtemp(prefix="sdui-outside-")
    outside = os.path.join(other, "evil.png")
    png(outside)
    doc = {"streamdeck_ui_version": 2, "state": {"CL12K1A00042": full_deck({
        0: {0: v2(text="OBS\x07", icon=icon, command="obs --minimize", font_size=12, background_color="#202020"),
            1: v2(keys="ctrl+shift+m", text="‮Mute"),
            2: v2(switch_page=2, text="More"),
            3: v2(brightness_change=10, text="Bright+"),
            4: v2(brightness_change=-10),
            5: v2(write="hello\n", switch_page=1),
            6: v2(keys="alt+tab,delay0.5,enter"),
            7: v2(keys="ctrl+plus,ctrl+comma"),
            8: v2(keys="ctrl+f5,delay,ctrl+a"),
            9: v2(keys="a+b"),
            10: v2(icon=outside, text="Out"),
            11: v2(font_color="#ff0000", background_color="#000000", text="Red"),
            12: v2(switch_page=9, text="Nowhere")},
        1: {0: v2(text="Back", switch_page=1),
            1: {"state": 0, "states": {"0": {"text": "S0"}, "1": {"text": "S1"}}}}})}}
    return write(CONFIG, doc), outside


class RealisticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path, cls.outside = build()
        cls.setup = streamdeck_ui.read(path)
        cls.cfg, cls.info = to_config(cls.setup)
        cls.deck = cls.setup["decks"][0]

    def key(self, page, idx):
        r, c = divmod(idx, 5)
        return self.deck["pages"][page][r][c]

    @staticmethod
    def review(key):
        return key["action"]["needs_review"]["imported_action"]

    def test_structure(self):
        self.assertIs(KINDS["streamdeck_ui"], streamdeck_ui.read)
        self.assertEqual(self.deck["serial"], "CL12K1A00042")
        self.assertEqual((self.deck["model"], self.deck["grid"]), ("Stream Deck Original", (5, 3)))
        self.assertEqual(list(self.deck["pages"]), ["Page 1", "Page 2"])
        self.assertEqual(self.deck["start_page"], "Page 1")
        self.assertEqual(set(self.cfg), {"pages", "start_page"})

    def test_command_label_colors(self):
        k = self.key("Page 1", 0)
        self.assertEqual((k["label"], k["bg"]), ("OBS", "#202020"))
        self.assertEqual(self.review(k), {"run": "obs --minimize"})
        self.assertEqual(k["action"]["needs_review"]["source"], "streamdeck-ui")
        red = self.key("Page 1", 11)
        self.assertEqual((red["fg"], red.get("bg")), ("#ff0000", None))

    def test_keys_syntax(self):
        self.assertEqual(self.review(self.key("Page 1", 1)), {"keys": "ctrl+shift+m"})
        self.assertEqual(self.key("Page 1", 1)["label"], "Mute")
        self.assertEqual(self.review(self.key("Page 1", 6)),
                         [{"keys": "alt+Tab"}, {"delay": 0.5}, {"keys": "Return"}])
        self.assertEqual(self.review(self.key("Page 1", 7)), {"keys": ["ctrl+plus", "ctrl+comma"]})
        self.assertEqual(self.review(self.key("Page 1", 8)),
                         [{"keys": "ctrl+F5"}, {"delay": 0.5}, {"keys": "ctrl+a"}])
        bad = self.key("Page 1", 9)
        self.assertNotIn("action", bad)
        self.assertIn("needs_action", bad)

    def test_write_brightness_pages(self):
        self.assertEqual(self.review(self.key("Page 1", 5)), [{"type": "hello\n"}, {"page": "Page 1"}])
        self.assertEqual(self.key("Page 1", 3)["action"], {"brightness": "up"})
        self.assertEqual(self.key("Page 1", 4)["action"], {"brightness": "down"})
        self.assertEqual(self.key("Page 1", 2)["action"], {"page": "Page 2"})      # 1-based: 2 → second page
        self.assertIn("needs_action", self.key("Page 1", 12))
        self.assertEqual(self.key("Page 2", 0)["action"], {"page": "Page 1"})

    def test_pages_renamed_in_config(self):
        self.assertEqual(self.cfg["pages"]["page_1"][0][2]["action"], {"page": "page_2"})
        self.assertEqual(self.cfg["pages"]["page_2"][0][0]["action"], {"page": "page_1"})

    def test_images(self):
        icon = self.key("Page 1", 0)["icon"]
        self.assertTrue(icon.startswith(os.path.join(state_dir(), "icons", "imported", "streamdeck-ui") + os.sep))
        with Image.open(icon) as img:
            self.assertEqual(img.format, "PNG")
        out = self.key("Page 1", 10)
        self.assertEqual(out["label"], "Out")
        self.assertNotIn("icon", out)
        self.assertTrue(any("outside your home" in w for w in self.setup["warnings"]))
        self.assertNotIn(self.outside, " ".join(self.setup["warnings"]))

    def test_assumptions_reported(self):
        text = " ".join(self.setup["warnings"])
        self.assertIn("count from 1", text)
        self.assertIn("Stream Deck Original", text)
        self.assertIn("several states", text)

    def test_plan_accepts_it(self):
        with open(EXAMPLE) as f:
            current = yaml.safe_load(f)
        p = importer.plan(current, self.cfg)
        self.assertTrue(p["added"])
        importer.apply(current, self.cfg, importer.defaults(p))
        yaml.safe_dump(self.cfg, allow_unicode=True)


class VariantsTest(unittest.TestCase):
    def test_v1_flat_buttons(self):
        doc = {"streamdeck_ui_version": 1, "state": {"X1": {"buttons": {"0": {
            "0": {"text": "Hi", "keys": "f5"}, "1": {"switch_page": 2}, "2": {}, "3": {}, "4": {}, "5": {}},
            "1": {"0": {"text": "Two"}}}}}}
        d = streamdeck_ui.read(write(os.path.join(tempfile.mkdtemp(), "c.json"), doc))["decks"][0]
        self.assertEqual((d["model"], d["grid"]), ("Stream Deck Mini", (3, 2)))
        k = d["pages"]["Page 1"][0][0]
        self.assertEqual(k["action"]["needs_review"]["imported_action"], {"keys": "F5"})
        self.assertEqual(d["pages"]["Page 1"][0][1]["action"], {"page": "Page 2"})

    def test_large_deck(self):
        doc = {"streamdeck_ui_version": 2, "state": {"X": full_deck({0: {}}, count=32)}}
        d = streamdeck_ui.read(write(os.path.join(tempfile.mkdtemp(), "c.json"), doc))["decks"][0]
        self.assertEqual((d["model"], d["grid"]), ("Stream Deck XL", (8, 4)))

    def test_failures_never_quote_content(self):
        tmp = tempfile.mkdtemp()
        for name, body in (("bad.json", '{"streamdeck_ui_version": 2, SECRET-TOKEN-123'),
                           ("nover.json", '{"state": {}, "note": "SECRET-TOKEN-123"}'),
                           ("list.json", '["SECRET-TOKEN-123"]'),
                           ("empty.json", '{"streamdeck_ui_version": 2, "state": {}}'),
                           ("nobuttons.json", '{"streamdeck_ui_version": 2, "state": {"A": {"x": "SECRET-TOKEN-123"}}}')):
            with self.subTest(name), self.assertRaises(ImportError_) as cm:
                streamdeck_ui.read(write(os.path.join(tmp, name), body))
            self.assertNotIn("SECRET", str(cm.exception))
        with self.assertRaises(ImportError_):
            streamdeck_ui.read(os.path.join(tmp, "missing.json"))

    def test_keys_parser(self):
        steps, _ = streamdeck_ui.parse_keys(" Ctrl + Shift + M ")
        self.assertEqual(steps, [{"keys": "ctrl+shift+m"}])
        self.assertEqual(streamdeck_ui.parse_keys("delay2,x")[0], [{"delay": 2.0}, {"keys": "x"}])
        for bad in ("", "ctrl", "nosuchkey", "delay99,a"):
            self.assertIsNone(streamdeck_ui.parse_keys(bad)[0], bad)


if __name__ == "__main__":
    unittest.main()
