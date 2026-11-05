"""core/importers/boatswain.py: a Boatswain <serial>.json → our setup / config."""
import json
import os
import shlex
import tempfile
import unittest
from urllib.parse import quote

from tests.helpers import EXAMPLE, SANDBOX

import yaml  # noqa: E402
from PIL import Image  # noqa: E402

from core import importer  # noqa: E402
from core.importers import KINDS, boatswain, to_config  # noqa: E402
from core.importers._common import ImportError_  # noqa: E402
from core.paths import state_dir  # noqa: E402

CTRL, ALT, SHIFT = 1 << 2, 1 << 3, 1


def png(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new("RGBA", (96, 96), (0, 200, 0, 255)).save(path, format="PNG")


def item(factory, action, settings=None, text=None, bg=None, file=None):
    it = {"type": "action", "factory": factory, "action": action}
    if settings is not None:
        it["settings"] = settings
    icon = {k: v for k, v in (("text", text), ("background-color", bg), ("file", file)) if v}
    if icon:
        it["custom-icon"] = icon
    return it


def page(items):
    return {"version": 1, "regions": [{"id": "main-button-grid", "region-data": {}, "items": items}]}


def grid(items, count=15):
    return items + [{}] * (count - len(items))


def write(path, doc):
    with open(path, "w") as f:
        f.write(doc if isinstance(doc, str) else json.dumps(doc))
    return path


def build():
    icon = os.path.join(SANDBOX, "pictures", "my icon.png")
    png(icon)
    other = tempfile.mkdtemp(prefix="bsw-outside-")
    outside = os.path.join(other, "evil.png")
    png(outside)
    folder = page(grid([item("default", "default-switch-page-action",
                             {"page": page(grid([item("obs-studio", "obs-switch-scene-action", {"scene-name": "Chat"}, "Chat")]))},
                             "Deeper")]))
    sub = page(grid([item("launcher", "launcher-open-url-action", {"url": "https://example.org/a b"}, "Bad"),
                     item("default", "default-page-up-action", text="Up")]))
    items = grid([
        item("obs-studio", "obs-switch-scene-action", {"scene-name": "Gameplay", "host": "localhost", "port": 4455},
             "Game", "rgb(32,32,32)", "file://" + quote(icon)),
        item("launcher", "launcher-open-url-action", {"url": "https://twitch.tv"}, "Twitch"),
        item("default", "default-switch-page-action", {"page": sub}, "Tools‮", "rgba(0,0,0,0)"),
        {},
        item("launcher", "launch-action", {"app": "org.mozilla.firefox.desktop", "files": []}, "Fx"),
        item("desktop", "desktop-keyboard-shortcut", {"modifiers": CTRL | SHIFT, "keysym": ord("m")}, "Mute"),
        item("desktop", "desktop-keyboard-shortcut", {"modifiers": ALT, "keysym": 0xffbe + 4}, "F5"),
        item("soundboard", "soundboard-play-action", {"file": "/home/u/secret-sound.wav", "behavior": 0}, "Sound"),  # privacy-check: allow
        item("obs-studio", "obs-stream-action", {"host": "localhost"}, "Live"),
        item("network", "network-http-action", {"uri": "http://x"}, "Hook"),
        item("launcher", "launcher-open-url-action", {"url": "javascript:alert(1)"}, "JS"),
        item("obs-studio", "obs-switch-scene-action", {"scene-name": "Out"}, "Out", None, "file://" + quote(outside)),
        item("default", "default-switch-page-action", {"page": folder}, "More"),
    ])
    dial = {"id": "dial-grid", "region-data": {}, "items": [item("obs-studio", "obs-record-action")]}
    prof = page(items)
    prof["regions"].append(dial)
    doc = {"active-profile": "p2", "profiles": [
        {"id": "p1", "name": "Old", "brightness": 0.5, "page": page(grid([item("launcher", "launch-action", {"app": "x.desktop"})]))},
        {"id": "p2", "name": "Streaming", "brightness": 0.8, "page": prof}]}
    return write(os.path.join(SANDBOX, "ABC123.json"), doc), outside


class RealisticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path, cls.outside = build()
        cls.setup = boatswain.read(path)
        cls.cfg, cls.info = to_config(cls.setup)
        cls.deck = cls.setup["decks"][0]

    def key(self, page, idx):
        r, c = divmod(idx, 5)
        return self.deck["pages"][page][r][c]

    @staticmethod
    def review(key):
        return key["action"]["needs_review"]["imported_action"]

    def test_structure(self):
        self.assertIs(KINDS["boatswain"], boatswain.read)
        self.assertEqual(self.deck["serial"], "ABC123")
        self.assertEqual((self.deck["model"], self.deck["grid"]), ("Stream Deck Original", (5, 3)))
        self.assertEqual(self.deck["start_page"], "Streaming")                       # active profile, not the first
        self.assertEqual(list(self.deck["pages"]), ["Streaming", "Tools", "More", "Deeper"])
        self.assertEqual(set(self.cfg), {"pages", "start_page"})

    def test_icon_label_background(self):
        k = self.key("Streaming", 0)
        self.assertEqual((k["label"], k["bg"]), ("Game", "#202020"))
        self.assertEqual(k["action"], {"obs_scene": "Gameplay"})
        self.assertTrue(k["icon"].startswith(os.path.join(state_dir(), "icons", "imported", "boatswain") + os.sep))
        with Image.open(k["icon"]) as img:
            self.assertEqual(img.format, "PNG")
        self.assertEqual(self.key("Streaming", 2)["label"], "Tools")
        self.assertNotIn("bg", self.key("Streaming", 2))                             # transparent

    def test_review_wrapped(self):
        self.assertEqual(shlex.split(self.review(self.key("Streaming", 1))["run"]), ["xdg-open", "https://twitch.tv"])
        self.assertEqual(self.review(self.key("Streaming", 4)), {"run": "gtk-launch org.mozilla.firefox"})
        self.assertEqual(self.review(self.key("Streaming", 5)), {"keys": "ctrl+shift+m"})
        self.assertEqual(self.review(self.key("Streaming", 6)), {"keys": "alt+F5"})
        self.assertEqual(self.review(self.key("Streaming", 8)), {"obs": "ToggleStream"})
        self.assertEqual(self.key("Streaming", 5)["action"]["needs_review"]["source"], "boatswain")

    def test_unsupported(self):
        for idx in (7, 9, 10):                       # soundboard, http, javascript: link
            k = self.key("Streaming", idx)
            self.assertNotIn("action", k, idx)
            self.assertIn("needs_action", k, idx)
        self.assertEqual(self.key("Streaming", 7)["needs_action"]["original_id"], "soundboard/soundboard-play-action")
        self.assertNotIn("secret-sound", json.dumps(self.key("Streaming", 7)))
        self.assertIn("needs_action", self.key("Tools", 0))                          # URL with a space

    def test_folders_and_back(self):
        self.assertEqual(self.key("Streaming", 2)["action"], {"page": "Tools"})
        self.assertEqual(self.key("Tools", 1)["action"], {"page": "back"})           # their own back key kept
        self.assertEqual(self.key("Streaming", 12)["action"], {"page": "More"})
        self.assertEqual(self.key("More", 0)["action"], {"page": "Deeper"})
        self.assertEqual(self.key("More", 1)["action"], {"page": "back"})            # added
        self.assertEqual(self.key("Deeper", 0)["action"], {"obs_scene": "Chat"})
        self.assertEqual(self.key("Deeper", 1)["action"], {"page": "back"})
        self.assertEqual(self.cfg["pages"]["streaming"][0][2]["action"], {"page": "tools"})
        self.assertEqual(self.cfg["pages"]["more"][0][0]["action"], {"page": "deeper"})

    def test_outside_image_refused(self):
        k = self.key("Streaming", 11)
        self.assertEqual(k["label"], "Out")
        self.assertNotIn("icon", k)
        self.assertTrue(any("outside your home" in w for w in self.setup["warnings"]))
        self.assertNotIn(self.outside, " ".join(self.setup["warnings"]))

    def test_warnings(self):
        text = " ".join(self.setup["warnings"])
        self.assertIn("Dials", text)
        self.assertIn("Only the active profile", text)
        self.assertIn("Assumed", text)

    def test_plan_accepts_it(self):
        with open(EXAMPLE) as f:
            current = yaml.safe_load(f)
        p = importer.plan(current, self.cfg)
        self.assertTrue(p["added"])
        importer.apply(current, self.cfg, importer.defaults(p))
        yaml.safe_dump(self.cfg, allow_unicode=True)


class VariantsTest(unittest.TestCase):
    def read(self, doc):
        return boatswain.read(write(os.path.join(tempfile.mkdtemp(), "S1.json"), doc))

    def test_falls_back_to_first_profile_and_v0_page(self):
        doc = {"active-profile": "gone", "profiles": [
            {"id": "a", "name": "First", "page": [item("obs-studio", "obs-switch-scene-action", {"scene-name": "A"})] + [{}] * 5}]}
        d = self.read(doc)["decks"][0]
        self.assertEqual((d["model"], d["start_page"]), ("Stream Deck Mini", "First"))
        self.assertEqual(d["pages"]["First"][0][0]["action"], {"obs_scene": "A"})

    def test_full_folder_has_no_back_key(self):
        sub = page([item("launcher", "launch-action", {"app": "a.desktop"})] * 6)
        doc = {"profiles": [{"id": "a", "name": "P", "page": page(
            [item("default", "default-switch-page-action", {"page": sub}, "F")] + [{}] * 5)}]}
        setup = self.read(doc)
        self.assertTrue(any("no back key" in w for w in setup["warnings"]))

    def test_failures_never_quote_content(self):
        tmp = tempfile.mkdtemp()
        for name, body in (("bad.json", '{"profiles": SECRET-TOKEN-123'),
                           ("list.json", '["SECRET-TOKEN-123"]'),
                           ("none.json", '{"profiles": [], "x": "SECRET-TOKEN-123"}'),
                           ("nogrid.json", '{"profiles": [{"id": "a", "page": {"regions": [{"id": "dial-grid"}]}}]}'),
                           ("badpage.json", '{"profiles": [{"id": "a", "page": "SECRET-TOKEN-123"}]}')):
            with self.subTest(name), self.assertRaises(ImportError_) as cm:
                boatswain.read(write(os.path.join(tmp, name), body))
            self.assertNotIn("SECRET", str(cm.exception))
        with self.assertRaises(ImportError_):
            boatswain.read(os.path.join(tmp, "missing.json"))


if __name__ == "__main__":
    unittest.main()
