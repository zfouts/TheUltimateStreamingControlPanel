"""core/importers/streamcontroller.py: a StreamController data folder → our setup / config."""
import json
import os
import shlex
import tempfile
import unittest

from tests.helpers import EXAMPLE, SANDBOX

import yaml  # noqa: E402
from PIL import Image  # noqa: E402

from core import importer  # noqa: E402
from core.importers import KINDS, streamcontroller, to_config  # noqa: E402
from core.importers._common import ImportError_  # noqa: E402
from core.paths import state_dir  # noqa: E402

DATA = os.path.join(SANDBOX, ".var", "app", "com.core447.StreamController", "data")
ASSETS = os.path.join(DATA, "Assets", "AssetManager", "Assets")
OS_, DECK, OBS, MEDIA = ("com_core447_OSPlugin::", "com_core447_DeckPlugin::", "com_core447_OBSPlugin::",
                         "com_core447_MediaPlugin::")


def page_path(name):
    return os.path.join(DATA, "pages", name + ".json")


def action(aid, **settings):
    return {"id": aid, "settings": settings}


def state(*actions, top=None, center=None, bottom=None, media=None, bg=None, color=None):
    labels = {k: {"text": v, "color": color or [255, 255, 255, 255]}
              for k, v in (("top", top), ("center", center), ("bottom", bottom)) if v}
    st = {"labels": labels, "actions": list(actions)}
    if media:
        st["media"] = {"path": media, "size": 1.0}
    if bg:
        st["background"] = {"color": bg}
    return {"states": {"0": st}}


def write(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(doc if isinstance(doc, str) else json.dumps(doc))


def png(path, color=(10, 20, 30, 255)):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new("RGBA", (96, 96), color).save(path, format="PNG")


def build():
    """Two decks: A starts at Main (→ Twitch), B at Solo; Orphan is reachable from nowhere."""
    png(os.path.join(ASSETS, "live.png"))
    outside = tempfile.mkdtemp(prefix="sc-outside-")
    png(os.path.join(outside, "evil.png"))
    write(page_path("Main"), {"keys": {
        "0x0": state(action(OS_ + "Hotkey", keys=[[29, 1], [42, 1], [50, 1], [50, 0], [42, 0], [29, 0]]),
                     top="Mute", bottom="Mic", media=os.path.join(ASSETS, "live.png"), bg=[40, 0, 0, 255]),
        "1x0": state(action(DECK + "ChangePage", selected_page=page_path("Twitch")), center="‮Twitch\x07"),
        "2x0": state(action(OS_ + "OpenInBrowser", url="https://twitch.tv/x")),
        "3x0": state(action(OS_ + "OpenInBrowser", url="file:///etc/passwd")),
        "4x0": state(action(OS_ + "RunCommand", command="notify-send hi")),
        "0x1": state(action(OS_ + "WriteText", text="gg")),
        "1x1": state(action(OS_ + "Launch", app_path="/usr/bin/firefox")),
        "2x1": state(action(MEDIA + "PlayPause")),
        "3x1": state(action(OBS + "SwitchScene", scene="Gameplay")),
        "4x1": state(action(OBS + "ToggleStream")),
        "0x2": state(action(OBS + "ToggleRecord")),
        "1x2": state(action(OBS + "ToggleInputMute", input="Mic/Aux")),
        "2x2": state(action("com_other_Plugin::Weird", secret="hunter2"), top="Odd"),
        "3x2": state(action(DECK + "ChangePage", selected_page=page_path("Gone"))),
        "4x2": state(action(OS_ + "Hotkey", keys=[[29, 1], [29, 0]])),
        "0x0x": {},
    }, "dials": {"0": state(action(OBS + "InputDial"))}})
    write(page_path("Twitch"), {"keys": {
        "0x0": state(action(DECK + "GoToPreviousPage"), center="Back"),
        "1x0": state(action(DECK + "ChangePage", selected_page=page_path("Main"))),
        "2x0": state(media=os.path.join(outside, "evil.png"), top="Out"),
    }})
    # pre-1.5 flat format: no "states"
    write(page_path("Solo"), {"keys": {"0x0": {"labels": {"top": {"text": "Flat"}},
                                                "actions": [action(OBS + "SwitchScene", scene="Chat")]}}})
    write(page_path("Orphan"), {"keys": {"0x0": state(top="Lost")}})
    write(os.path.join(DATA, "settings", "pages.json"), {"default-pages": {
        "AAA111": page_path("Main"), "BBB222": page_path("Solo"), "CCC333": page_path("Missing")}})
    return outside


class RealisticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.outside = build()
        cls.setup = streamcontroller.read(DATA)
        cls.cfg, cls.info = to_config(cls.setup)

    def deck(self, serial):
        return next(d for d in self.setup["decks"] if d.get("serial") == serial)

    def key(self, page, x, y, serial="AAA111"):
        return self.deck(serial)["pages"][page][y][x]

    @staticmethod
    def review(key):
        return key["action"]["needs_review"]["imported_action"]

    def test_structure(self):
        self.assertIs(KINDS["streamcontroller"], streamcontroller.read)
        self.assertEqual([d["serial"] for d in self.setup["decks"]], ["AAA111", "BBB222"])
        a, b = self.setup["decks"]
        self.assertEqual((a["start_page"], b["start_page"]), ("Main", "Solo"))
        self.assertEqual(list(a["pages"]), ["Main", "Twitch", "Orphan"])      # unreachable → first deck
        self.assertEqual(list(b["pages"]), ["Solo"])
        self.assertEqual(a["model"], "Stream Deck Original")                  # 5 × 3 keys used

    def test_labels_cleaned_and_joined(self):
        self.assertEqual(self.key("Main", 0, 0)["label"], "Mute\nMic")
        self.assertEqual(self.key("Main", 1, 0)["label"], "Twitch")
        self.assertEqual(self.key("Main", 0, 0)["bg"], "#280000")

    def test_hotkeys_need_review(self):
        k = self.key("Main", 0, 0)
        self.assertEqual(self.review(k), {"keys": "ctrl+shift+m"})
        self.assertEqual(k["action"]["needs_review"]["source"], "streamcontroller")
        self.assertIn("needs_action", self.key("Main", 4, 2))                  # modifiers only

    def test_commands_urls_text(self):
        self.assertEqual(shlex.split(self.review(self.key("Main", 2, 0))["run"]), ["xdg-open", "https://twitch.tv/x"])
        self.assertIn("needs_action", self.key("Main", 3, 0))
        self.assertEqual(self.review(self.key("Main", 4, 0)), {"run": "notify-send hi"})
        self.assertEqual(self.review(self.key("Main", 0, 1)), {"type": "gg"})
        self.assertEqual(self.review(self.key("Main", 1, 1)), {"run": "/usr/bin/firefox"})

    def test_live_actions(self):
        self.assertEqual(self.key("Main", 2, 1)["action"], {"media": "play_pause"})
        self.assertEqual(self.key("Main", 3, 1)["action"], {"obs_scene": "Gameplay"})
        self.assertEqual(self.review(self.key("Main", 4, 1)), {"obs": "ToggleStream"})
        self.assertEqual(self.review(self.key("Main", 0, 2)), {"obs": "ToggleRecord"})
        self.assertEqual(self.key("Main", 1, 2)["action"], {"obs_mute": "Mic/Aux"})

    def test_unknown_and_missing_page(self):
        k = self.key("Main", 2, 2)
        self.assertNotIn("action", k)
        self.assertEqual(k["needs_action"]["original_id"], "com_other_Plugin::Weird")
        self.assertNotIn("hunter2", json.dumps(k))
        self.assertIn("needs_action", self.key("Main", 3, 2))

    def test_flat_format(self):
        k = self.key("Solo", 0, 0, "BBB222")
        self.assertEqual((k["label"], k["action"]), ("Flat", {"obs_scene": "Chat"}))

    def test_pages_renamed_in_config(self):
        self.assertEqual(set(self.cfg["layouts"]), {"5x3", "3x2"})     # by grid, so they merge into the user's
        pages = self.cfg["layouts"]["5x3"]["pages"]
        self.assertEqual(pages["main"][0][1]["action"], {"page": "twitch"})
        self.assertEqual(pages["twitch"][0][0]["action"], {"page": "back"})
        self.assertEqual(pages["twitch"][0][1]["action"], {"page": "main"})

    def test_images(self):
        icon = self.key("Main", 0, 0)["icon"]
        self.assertTrue(icon.startswith(os.path.join(state_dir(), "icons", "imported", "streamcontroller") + os.sep))
        with Image.open(icon) as img:
            self.assertEqual(img.format, "PNG")
        out = self.key("Twitch", 2, 0)
        self.assertEqual(out["label"], "Out")
        self.assertNotIn("icon", out)
        self.assertTrue(any("outside StreamController" in w for w in self.setup["warnings"]))
        self.assertNotIn(self.outside, " ".join(self.setup["warnings"]))

    def test_warnings(self):
        text = " ".join(self.setup["warnings"])
        self.assertIn("Dials", text)
        self.assertIn("default page", text)
        self.assertIn("Assumed", text)

    def test_plan_accepts_it(self):
        with open(EXAMPLE) as f:
            current = yaml.safe_load(f)
        p = importer.plan(current, self.cfg)
        self.assertTrue(p["added"])
        importer.apply(current, self.cfg, importer.defaults(p))
        yaml.safe_dump(self.cfg, allow_unicode=True)


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sc-data-")

    def test_malformed_page(self):
        write(os.path.join(self.tmp, "pages", "Bad.json"), '{"keys": SECRET-TOKEN-123')
        with self.assertRaises(ImportError_) as cm:
            streamcontroller.read(self.tmp)
        self.assertNotIn("SECRET", str(cm.exception))

    def test_malformed_settings(self):
        write(os.path.join(self.tmp, "pages", "Ok.json"), {"keys": {}})
        write(os.path.join(self.tmp, "settings", "pages.json"), "not json SECRET-TOKEN-123")
        with self.assertRaises(ImportError_) as cm:
            streamcontroller.read(self.tmp)
        self.assertNotIn("SECRET", str(cm.exception))

    def test_not_a_data_folder(self):
        with self.assertRaises(ImportError_):
            streamcontroller.read(self.tmp)
        with self.assertRaises(ImportError_):
            streamcontroller.read(os.path.join(self.tmp, "missing"))

    def test_no_settings_puts_everything_on_one_deck(self):
        write(os.path.join(self.tmp, "pages", "B.json"), {"keys": {"0x0": state(top="b")}})
        write(os.path.join(self.tmp, "pages", "A.json"), {"keys": {"0x0": state(top="a")}})
        setup = streamcontroller.read(self.tmp)
        self.assertEqual(len(setup["decks"]), 1)
        self.assertEqual(setup["decks"][0]["start_page"], "A")
        cfg, _ = to_config(setup)
        self.assertEqual(set(cfg), {"pages", "start_page"})
        self.assertEqual(cfg["start_page"], "a")


class EvdevTest(unittest.TestCase):
    def test_table_samples(self):
        e = streamcontroller.EVDEV_KEYS
        self.assertEqual((e[30], e[44], e[16], e[2], e[11], e[59], e[88], e[28], e[57]),
                         ("a", "z", "q", "1", "0", "F1", "F12", "Return", "space"))


if __name__ == "__main__":
    unittest.main()
