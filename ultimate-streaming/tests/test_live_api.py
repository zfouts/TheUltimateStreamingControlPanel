"""The web panel's live deck mirror: rendering, pressing (incl. confirm keys), paging,
brightness, module snapshots and log/chat history. Handlers are called directly."""
import os
import threading
import unittest

from tests.helpers import EXAMPLE, ROOT, Deck, tmpdir

import yaml  # noqa: E402

from core.bot import DeckBot  # noqa: E402
from core.render import Renderer  # noqa: E402
from core.webapi import ApiError, Router, live_api  # noqa: E402

PNG = "data:image/png;base64,"


def setUpModule():
    global BOT, ED, ROUTER
    config = os.path.join(tmpdir(), "config.yaml")
    with open(EXAMPLE) as f:
        cfg = yaml.safe_load(f)
    with open(config, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)
    BOT = DeckBot(root=ROOT, config_path=config)
    BOT.deck = Deck("Stream Deck XL")
    BOT.renderer = Renderer(BOT.deck, ROOT, None, None)
    BOT.load_config(initial=True)
    BOT.load_extensions("modules")
    ED = BOT.get_module("Editor")
    ROUTER = Router()
    live_api.register(ROUTER, ED)


def tearDownModule():
    for inst in list(BOT.modules.values()):
        try:
            inst.on_unload()
        except Exception:
            pass


def get(path, **query):
    return ROUTER.gets[path](query)


def post(path, body):
    return ROUTER.posts[path](body)


class LiveDeck(unittest.TestCase):
    def setUp(self):
        BOT.armed = None
        self.ran = threading.Event()
        self.calls = []

        def fake(arg):
            self.calls.append(arg)
            self.ran.set()
        fake.__deck_action__ = ("fake", False)
        BOT.actions["fake"] = fake
        self.addCleanup(BOT.actions.pop, "fake", None)
        BOT.goto(next(iter(BOT.pages)))
        self.page = BOT.page
        self.orig = list(BOT.pages[self.page])
        self.addCleanup(BOT.pages.__setitem__, self.page, self.orig)

    def put(self, index, btn):
        BOT.pages[self.page] = [*self.orig[:index], btn, *self.orig[index + 1:]]

    def test_key_matches_deck(self):
        d = get("/api/live/deck")
        for i in (0, 5, 31):
            r = get("/api/live/key", page=BOT.page, key=str(i))
            self.assertEqual(r["page"], BOT.page)
            self.assertEqual(r["key"], d["keys"][i])

    def test_key_errors(self):
        with self.assertRaises(ApiError) as cm:
            get("/api/live/key", page="nope", key="0")
        self.assertEqual(cm.exception.status, 409)
        for bad in ("99", "-1", "x", None):
            with self.assertRaises(ApiError) as cm:
                get("/api/live/key", page=BOT.page, key=bad)
            self.assertEqual(cm.exception.status, 400)

    def test_deck_builds_buttons_once(self):
        calls = []
        sess = BOT.primary
        real = sess.buttons

        def counted(*a, **kw):
            calls.append(a)
            return real(*a, **kw)
        sess.buttons = counted
        try:
            get("/api/live/deck")
        finally:
            del sess.buttons
        self.assertEqual(len(calls), 1)

    def test_deck(self):
        d = get("/api/live/deck")
        self.assertTrue(d["attached"])
        self.assertEqual(d["page"], BOT.page)
        self.assertEqual(len(d["keys"]), 32)
        self.assertEqual((d["rows"], d["cols"]), (4, 8))
        self.assertIn(self.page, d["pages"])
        shown = [k for k in d["keys"] if k["image"]]
        self.assertTrue(shown)
        for k in shown:
            self.assertTrue(k["image"].startswith(PNG))
        for k in d["keys"]:
            if not k["image"]:
                self.assertEqual(k["label"], "")
        self.assertFalse(any(k["armed"] for k in d["keys"]))

    def test_press_rejects_bad_requests(self):
        with self.assertRaises(ApiError) as e:
            post("/api/live/press", {"page": "nope", "key": 0})
        self.assertEqual(e.exception.status, 409)
        for key in (-1, 32, "1", None, True):
            with self.assertRaises(ApiError) as e:
                post("/api/live/press", {"page": self.page, "key": key})
            self.assertEqual(e.exception.status, 400)
        with self.assertRaises(ApiError):
            post("/api/live/press", {"page": 3, "key": 0})

    def test_press_without_deck(self):
        deck, BOT.deck = BOT.deck, None
        try:
            with self.assertRaises(ApiError) as e:
                post("/api/live/press", {"page": self.page, "key": 0})
            self.assertEqual(e.exception.status, 503)
            self.assertFalse(get("/api/live/deck")["attached"])
        finally:
            BOT.deck = deck

    def test_press_runs_action(self):
        self.put(0, {"label": "T", "action": {"fake": 1}})
        self.assertEqual(post("/api/live/press", {"page": self.page, "key": 0}), {"ok": True})
        self.assertTrue(self.ran.wait(3))
        self.assertEqual(self.calls, [1])

    def test_confirm_key_needs_two_presses(self):
        self.put(1, {"label": "Go", "action": {"fake": 2}, "confirm": True})
        post("/api/live/press", {"page": self.page, "key": 1})
        self.assertEqual(BOT.armed[:2], (self.page, 1))
        self.assertFalse(self.ran.wait(0.3))
        key = get("/api/live/deck")["keys"][1]
        self.assertTrue(key["armed"] and key["confirm"] and key["has_action"])
        post("/api/live/press", {"page": self.page, "key": 1})
        self.assertTrue(self.ran.wait(3))
        self.assertEqual(self.calls, [2])
        self.assertIsNone(BOT.armed)

    def test_pages(self):
        other = next(p for p in BOT.pages if p != self.page)
        self.assertEqual(post("/api/live/page", {"page": other}), {"ok": True, "page": other})
        self.assertEqual(BOT.page, other)
        self.assertTrue(get("/api/live/deck")["can_back"])
        self.assertEqual(post("/api/live/page", {"page": "back"})["page"], self.page)
        with self.assertRaises(ApiError) as e:
            post("/api/live/page", {"page": "no-such-page"})
        self.assertEqual(e.exception.status, 400)

    def test_brightness_clamps(self):
        self.assertEqual(post("/api/live/brightness", {"level": 250})["brightness"], 100)
        self.assertEqual(post("/api/live/brightness", {"level": -5})["brightness"], 0)
        self.assertEqual(post("/api/live/brightness", {"level": 42})["brightness"], 42)
        with self.assertRaises(ApiError):
            post("/api/live/brightness", {"level": "high"})

    def test_snapshot_shapes(self):
        snap = get("/api/live/snapshot")
        for key in ("brb", "overlay", "radio"):
            self.assertIn(key, snap)
        brb = snap["brb"]
        self.assertEqual(set(brb), {"game", "hype", "video", "chat_lines"})
        self.assertIsNone(brb["game"]["kind"])
        self.assertEqual(set(brb["hype"]), {"value", "max", "full_until"})
        self.assertIn("countdown", snap["overlay"]["timers"])
        self.assertIn("brb_timer", snap["overlay"]["timers"])
        self.assertEqual(set(snap["radio"]), {"on", "station", "stations", "now_playing"})
        self.assertIsInstance(snap["radio"]["stations"], list)

    def test_brb_poll_snapshot(self):
        brb = BOT.get_module("BRB")
        brb.start_poll({"question": "Q?", "options": ["A", "B"]})
        brb.game["votes"].update({"x": 1, "y": 1, "z": 0})
        game = get("/api/live/snapshot")["brb"]["game"]
        self.assertEqual(game["kind"], "poll")
        self.assertEqual(game["options"], [{"label": "A", "votes": 1}, {"label": "B", "votes": 2}])
        self.assertEqual(game["total_votes"], 3)
        self.assertIsNone(game["winner"])
        brb.game_control("end")
        self.assertIn("B", get("/api/live/snapshot")["brb"]["game"]["winner"])
        brb.game_control("clear")

    def test_overlay_timer_snapshot(self):
        overlay = BOT.get_module("Overlay")
        overlay.start_timer("countdown", 90)
        self.addCleanup(overlay.stop_timer, "countdown")
        left = get("/api/live/snapshot")["overlay"]["timers"]["countdown"]
        self.assertTrue(88 <= left <= 90)
        self.assertIsNone(get("/api/live/snapshot")["overlay"]["timers"]["brb_timer"])

    def test_history(self):
        BOT.hub.publish("log", {"level": "info", "logger": "t", "msg": "hello"})
        BOT.hub.publish("chat", {"id": "1", "user": "u", "text": "hi"})
        lines = get("/api/live/log", n="5")["lines"]
        self.assertIsInstance(lines, list)
        self.assertEqual(lines[-1]["msg"], "hello")
        self.assertEqual(set(lines[-1]), {"ts", "level", "logger", "msg"})
        msgs = get("/api/live/chat", n="5")["messages"]
        self.assertEqual(msgs[-1]["text"], "hi")
        self.assertEqual(len(get("/api/live/log", n="0")["lines"]), 1)
        with self.assertRaises(ApiError):
            get("/api/live/log", n="x")


if __name__ == "__main__":
    unittest.main()
