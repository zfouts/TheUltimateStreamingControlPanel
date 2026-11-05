"""The live API across several decks: listing, per-deck mirror/press/page, 404/409, identify."""
import os
import unittest
from unittest import mock

from tests.helpers import EXAMPLE, ROOT, Deck, tmpdir

import yaml  # noqa: E402

from core.bot import DeckBot  # noqa: E402
from core.webapi import ApiError, Router, live_api  # noqa: E402

XL, MINI = "XL-0001", "MINI-0001"


def page(label, action=None):
    return {"label": label, **({"action": action} if action else {})}


class DevicesApi(unittest.TestCase):
    def setUp(self):
        with open(EXAMPLE) as f:
            cfg = yaml.safe_load(f)
        cfg["layouts"] = {
            "8x4": {"start_page": "main", "pages": {"main": [page("XL main", {"page": "two"})],
                                                    "two": [page("XL two")]}},
            "3x2": {"pages": {"home": [page("Mini home", {"page": "other"})],
                              "other": [page("Mini other")]}},
        }
        cfg.pop("devices", None)
        path = os.path.join(tmpdir(), "config.yaml")
        with open(path, "w") as f:
            yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)
        self.bot = DeckBot(root=ROOT, config_path=path)
        self.bot.load_config(initial=True)
        self.bot.load_extensions("modules")
        self.addCleanup(lambda: [m.on_unload() for m in list(self.bot.modules.values())])
        self.bot.attach_deck(Deck("Stream Deck XL", serial=XL))
        self.bot.attach_deck(Deck("Stream Deck Mini", serial=MINI))
        self.ed = self.bot.get_module("Editor")
        self.router = Router()
        live_api.register(self.router, self.ed)

    def get(self, path, **q):
        return self.router.gets[path](q)

    def post(self, path, body):
        return self.router.posts[path](body)

    def test_decks_listed(self):
        decks = self.get("/api/live/decks")["decks"]
        self.assertEqual([d["serial"] for d in decks], [XL, MINI])

    def test_mini_mirror(self):
        d = self.get("/api/live/deck", deck=MINI)
        self.assertEqual((d["serial"], len(d["keys"]), d["page"]), (MINI, 6, "home"))
        self.assertEqual(d["keys"][0]["label"], "Mini home")
        self.assertEqual(d["pages"], ["home", "other"])

    def test_default_is_primary(self):
        d = self.get("/api/live/deck")
        self.assertEqual((d["serial"], len(d["keys"]), d["page"]), (XL, 32, "main"))

    def test_press_only_changes_that_deck(self):
        self.post("/api/live/press", {"deck": MINI, "page": "home", "key": 0})
        deadline = __import__("time").time() + 3
        while self.bot.session(MINI).page != "other" and __import__("time").time() < deadline:
            __import__("time").sleep(0.02)
        self.assertEqual(self.bot.session(MINI).page, "other")
        self.assertEqual(self.bot.session(XL).page, "main")

    def test_stale_page_409(self):
        with self.assertRaises(ApiError) as cm:
            self.post("/api/live/press", {"deck": MINI, "page": "main", "key": 0})
        self.assertEqual(cm.exception.status, 409)
        with self.assertRaises(ApiError) as cm:
            self.get("/api/live/key", deck=MINI, page="main", key="0")
        self.assertEqual(cm.exception.status, 409)

    def test_unknown_deck_404(self):
        for call in (lambda: self.get("/api/live/deck", deck="NOPE"),
                     lambda: self.post("/api/live/page", {"deck": "NOPE", "page": "home"}),
                     lambda: self.post("/api/devices/identify", {"serial": "NOPE"})):
            with self.assertRaises(ApiError) as cm:
                call()
            self.assertEqual(cm.exception.status, 404)

    def test_page_and_brightness_per_deck(self):
        self.assertEqual(self.post("/api/live/page", {"deck": MINI, "page": "other"})["page"], "other")
        self.assertEqual(self.bot.session(XL).page, "main")
        self.assertEqual(self.post("/api/live/brightness", {"deck": MINI, "level": 20})["brightness"], 20)
        self.assertNotEqual(self.bot.session(XL).brightness, 20)

    def test_identify_flashes_then_redraws(self):
        sess = self.bot.session(MINI)
        events = []
        with mock.patch.object(sess, "_write", side_effect=lambda fn, *a: events.append("white")), \
                mock.patch.object(sess, "draw_page", side_effect=lambda: events.append("page")), \
                mock.patch.object(live_api.time, "sleep"):
            thread_started = []
            real = live_api.threading.Thread

            def spy(*a, **kw):
                t = real(*a, **kw)
                thread_started.append(t)
                return t
            with mock.patch.object(live_api.threading, "Thread", spy):
                self.assertEqual(self.post("/api/devices/identify", {"serial": MINI}), {"ok": True})
            thread_started[0].join(3)
        self.assertEqual(events, (["white"] * 6 + ["page"]) * 3)

    def test_state_and_status_list_devices(self):
        self.assertEqual(len(self.ed.state()["devices"]), 2)
        self.assertEqual(len(self.ed.status()["decks"]), 2)


if __name__ == "__main__":
    unittest.main()
