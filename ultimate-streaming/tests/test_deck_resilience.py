"""One bad key setting or one slow integration never freezes or kills the deck: bad colors and
sizes render with defaults, nothing escapes the key/dial/touch callbacks (the StreamDeck library's
reader thread dies on any exception), one bad key doesn't stop a page drawing, slow or failing
states/displays are backed off, and the panel's press doesn't hold the bot lock while acting."""
import logging
import os
import threading
import types
import unittest
from unittest import mock

from tests.helpers import ROOT, Deck, tmpdir

import yaml  # noqa: E402

from core.bot import DeckBot, DialEventType, TouchscreenEventType  # noqa: E402
from core.render import Renderer, valid_color  # noqa: E402
from core.webapi import Router, live_api  # noqa: E402
from modules import home_assistant, media  # noqa: E402
from modules.editor import Editor  # noqa: E402

SERIAL = "XL-RES"


class Recorder(Deck):
    """Remembers which keys were drawn."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.drawn = []

    def set_key_image(self, key, img):
        self.drawn.append(key)


def make_bot(keys, dials=None):
    page = {"keys": [keys]}
    if dials:
        page["dials"] = dials
    cfg = {"layouts": {"8x4": {"start_page": "main", "pages": {"main": page}}}}
    path = os.path.join(tmpdir(), "config.yaml")
    with open(path, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)
    bot = DeckBot(root=ROOT, config_path=path)
    bot.load_config(initial=True)
    deck = Recorder("Stream Deck XL", serial=SERIAL)
    bot.attach_deck(deck)
    return bot, bot.session(SERIAL), deck


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class BadSettings(unittest.TestCase):
    def setUp(self):
        self.r = Renderer(Deck("Stream Deck XL"), ROOT)

    def test_bad_colors_render_with_the_defaults(self):
        for bad in ("9146FF", "", "transparent", 12, [1, 2], ["a", "b", "c"]):
            img = self.r.image({"label": f"bad {bad!r}", "bg": bad, "fg": bad})
            self.assertEqual(img.getpixel((1, 1)), (0, 0, 0))
        self.assertEqual(self.r.image({"bg": "#9146ff"}).getpixel((1, 1)), (145, 70, 255))
        self.assertEqual(self.r.image({"bg": "purple"}).getpixel((1, 1)), (128, 0, 128))

    def test_bad_color_warns_once_per_key_and_value(self):
        with self.assertLogs("deck", logging.WARNING) as logs:
            for _ in range(3):
                self.r.image({"label": "once only", "bg": "nope-1"})
            self.r.image({"label": "once only", "bg": "nope-2"})
            self.r.image({"label": "another key", "bg": "nope-1"})
        self.assertEqual(len([m for m in logs.output if "isn't a color" in m]), 3)

    def test_bad_font_size_falls_back(self):
        for bad in ("big", -4, 0, True, [], float("nan"), float("inf"), 3, 10 ** 9):
            img = self.r.image({"label": "12:00", "size": bad})
            self.assertEqual(img.size, (96, 96))

    def test_bad_dial_colors_on_the_touch_strip(self):
        deck = Deck("Stream Deck +")
        deck.is_touch = lambda: True
        deck.touchscreen_image_format = lambda: {"size": (800, 100)}
        r = Renderer(deck, ROOT)
        with mock.patch("StreamDeck.ImageHelpers.PILHelper.to_native_touchscreen_format", lambda d, img: img):
            img = r.strip([{"label": "Vol", "bg": "oops", "fg": ""}], 4)
        self.assertEqual(img.getpixel((10, 10)), (0x1f, 0x1f, 0x24))

    def test_valid_color(self):
        for good in ("#fff", "#9146ff", "#9146ff80", "purple", "rgb(1, 2, 3)", (1, 2, 3), [1, 2, 3, 4]):
            self.assertTrue(valid_color(good), good)
        for bad in ("9146FF", "", "transparent", None, 5, (1, 2), (1, 2, 300), (True, 0, 0)):
            self.assertFalse(valid_color(bad), bad)


class Callbacks(unittest.TestCase):
    def setUp(self):
        self.ran = threading.Event()
        self.bot, self.s, self.deck = make_bot([
            {"label": "bad", "bg": "9146FF", "action": {"probe": 1}},
            {"label": "ok"},
            {"label": "also ok"},
        ], dials=[{"label": "Vol", "press": {"probe": 2}}])

        def probe(arg):
            self.ran.set()
        probe.__deck_action__ = ("probe", False)
        self.bot.actions["probe"] = probe

    def test_bad_color_key_still_works(self):
        self.bot.on_key(self.deck, 0, True)
        self.bot.on_key(self.deck, 0, False)
        self.assertTrue(self.ran.wait(3))

    def test_on_key_never_raises(self):
        with mock.patch.object(self.s.renderer, "render", side_effect=RuntimeError("render bug")), \
                self.assertLogs("deck", logging.ERROR):
            self.bot.on_key(self.deck, 0, True)
            self.bot.on_key(self.deck, 0, False)
        self.assertTrue(self.ran.wait(3))   # only the press feedback failed
        with mock.patch.object(self.s, "buttons", side_effect=RuntimeError("module bug")), \
                self.assertLogs("deck", logging.ERROR):
            self.bot.on_key(self.deck, 1, True)

    def test_on_dial_and_on_touch_never_raise(self):
        with mock.patch.object(self.s, "dials", side_effect=RuntimeError("module bug")), \
                self.assertLogs("deck", logging.ERROR) as logs:
            self.bot.on_dial(self.deck, 0, DialEventType.PUSH, True)
            self.bot.on_touch(self.deck, TouchscreenEventType.SHORT, {"x": 5})
        self.assertEqual(len(logs.output), 2)
        with mock.patch.object(self.s.renderer, "strip_size", side_effect=RuntimeError("bug")), \
                self.assertLogs("deck", logging.ERROR):
            self.bot.on_touch(self.deck, TouchscreenEventType.SHORT, {"x": 5})

    def test_one_bad_key_does_not_stop_draw_page(self):
        real = self.s.renderer.render

        def render(btn, pressed=False):
            if btn.get("label") == "ok":
                raise RuntimeError("one bad key")
            return real(btn, pressed)
        self.deck.drawn.clear()
        with mock.patch.object(self.s.renderer, "render", render), self.assertLogs("deck", logging.ERROR):
            self.s.draw_page()
        self.assertEqual(self.deck.drawn, [k for k in range(32) if k != 1])
        self.assertFalse(self.s.failed)


class PollBackoff(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.calls = {"ok": 0, "fail": 0, "slow": 0}
        self.failing = True

        def ok(value, spec):
            self.calls["ok"] += 1
            return True

        def fail(value, spec):
            self.calls["fail"] += 1
            if self.failing:
                raise ConnectionError("Home Assistant is down")
            return False

        def slow(value, spec):
            self.calls["slow"] += 1
            self.clock.t += 1.2
            return True
        self.providers = {"okp": ok, "failp": fail, "slowp": slow}

    def bot(self, keys):
        bot, s, deck = make_bot(keys)
        bot.state_providers.update(self.providers)
        s.clock = self.clock
        return bot, s

    def poll_at(self, s, t):
        self.clock.t = t
        s.poll()

    def test_failing_provider_is_backed_off_and_keeps_its_last_value(self):
        bot, s = self.bot([{"label": "ok", "state": {"okp": 1, "every": 0}},
                           {"label": "ha", "state": {"failp": 1, "every": 0}}])
        s.states[("main", 1)] = True                        # its last value
        with self.assertLogs("deck", logging.WARNING):
            self.poll_at(s, 1000)
        self.assertEqual(self.calls, {"ok": 1, "fail": 1, "slow": 0})
        self.assertIs(s.states[("main", 1)], True)
        for t, fails in ((1001, 1), (1001.9, 1), (1002.1, 2), (1005, 2), (1006.2, 3), (1014, 3), (1014.3, 4)):
            self.poll_at(s, t)
            self.assertEqual(self.calls["fail"], fails, t)
        self.assertEqual(self.calls["ok"], 8)              # polled every pass meanwhile
        self.assertIs(s.states[("main", 1)], True)
        waits = []
        for _ in range(6):
            self.clock.t = s.backoff[s._provider_key("state", {"failp": 1, "every": 0})][1]
            waits.append(s.backoff[s._provider_key("state", {"failp": 1, "every": 0})][0])
            s.poll()
        self.assertEqual(waits, [16, 32, 60, 60, 60, 60])  # capped
        self.failing = False
        self.clock.t = s.backoff[s._provider_key("state", {"failp": 1, "every": 0})][1]
        with self.assertLogs("deck", logging.INFO):
            s.poll()
        self.assertEqual(s.backoff, {})
        self.assertIs(s.states[("main", 1)], False)
        n = self.calls["fail"]
        self.poll_at(s, self.clock.t + 0.1)
        self.assertEqual(self.calls["fail"], n + 1)        # reset: polled again right away

    def test_slow_provider_is_backed_off(self):
        bot, s = self.bot([{"label": "slow", "state": {"slowp": 1, "every": 0}},
                           {"label": "ok", "state": {"okp": 1, "every": 0}}])
        with self.assertLogs("deck", logging.WARNING) as logs:
            self.poll_at(s, 1000)
        self.assertIn("took 1.2s", logs.output[0])
        self.assertIs(s.states[("main", 0)], True)         # a slow answer is still used
        self.poll_at(s, 1001.5)
        self.assertEqual((self.calls["slow"], self.calls["ok"]), (1, 2))
        self.poll_at(s, 1003.3)
        self.assertEqual(self.calls["slow"], 2)

    def test_keys_sharing_a_spec_share_the_backoff(self):
        bot, s = self.bot([{"label": "a", "state": {"failp": 1}}, {"label": "b", "state": {"failp": 1}},
                           {"label": "c", "state": {"failp": 2}}])
        with self.assertLogs("deck", logging.WARNING):
            self.poll_at(s, 1000)
        self.assertEqual(self.calls["fail"], 2)            # "a" and "c"; "b" waits with "a"

    def test_failing_display_is_backed_off(self):
        calls = []

        def show(spec):
            calls.append(spec)
            raise TimeoutError("slow API")
        bot, s = self.bot([{"label": "d", "display": {"source": "flaky", "every": 0}}])
        bot.display_providers["flaky"] = show
        s.displays[("main", 0)] = {"label": "last"}
        with self.assertLogs("deck", logging.WARNING):
            self.poll_at(s, 1000)
        self.poll_at(s, 1001)
        self.assertEqual(len(calls), 1)
        self.assertEqual(s.appearance(0)["label"], "last")

    def test_a_pass_stops_starting_calls_after_its_budget(self):
        def medium(value, spec):
            self.calls.setdefault("medium", []).append(value)
            self.clock.t += 0.8                             # under POLL_SLOW: never backed off
            return True
        bot, s = self.bot([{"label": str(i), "state": {"medium": i, "every": 10}} for i in range(4)])
        bot.state_providers["medium"] = medium
        self.poll_at(s, 1000)
        self.assertEqual(self.calls["medium"], [0, 1])      # 1.6 s in: keys 2 and 3 wait a pass
        s.poll()
        self.assertEqual(self.calls["medium"], [0, 1, 2, 3])
        self.assertEqual(s.backoff, {})

    def test_config_change_forgets_the_backoff(self):
        bot, s = self.bot([{"label": "ha", "state": {"failp": 1, "every": 0}}])
        with self.assertLogs("deck", logging.WARNING):
            self.poll_at(s, 1000)
        s.invalidate()
        self.poll_at(s, 1000.5)
        self.assertEqual(self.calls["fail"], 2)


class Integrations(unittest.TestCase):
    def test_home_assistant_state_uses_a_short_timeout(self):
        ha = home_assistant.HomeAssistant.__new__(home_assistant.HomeAssistant)
        ha.url, ha.headers = "http://ha.invalid:8123", {"Authorization": "Bearer x"}
        resp = mock.Mock(**{"json.return_value": {"state": "on"}})
        with mock.patch.object(home_assistant.requests, "get", return_value=resp) as get:
            self.assertTrue(ha.entity_state("light.office", {}))
        connect, read = get.call_args.kwargs["timeout"]
        self.assertLessEqual(connect, 1)
        self.assertLessEqual(read, 2)

    def test_media_dbus_calls_have_a_one_second_timeout(self):
        m = media.Media.__new__(media.Media)
        with mock.patch.object(media, "sh", return_value=(0, "")) as sh:
            self.assertFalse(m.media_state("playing", {}))
        self.assertEqual(sh.call_args.kwargs["timeout"], 1)
        with mock.patch.object(media, "sh", return_value=(124, "")):
            with self.assertRaises(TimeoutError):     # a hung D-Bus fails (and is backed off), not "off"
                m.media_state("playing", {})


class LivePress(unittest.TestCase):
    def test_press_does_not_hold_the_lock_during_the_action(self):
        bot, s, deck = make_bot([{"label": "slow", "action": {"held": 1}}])
        free = []

        def held(arg, down):
            # Another thread (the run loop) must be able to take the lock meanwhile.
            t = threading.Thread(target=lambda: free.append(bot.lock.acquire(timeout=2) and not bot.lock.release()))
            t.start()
            t.join()
        held.__deck_action__ = ("held", True)
        bot.actions["held"] = held
        router = Router()
        live_api.register(router, types.SimpleNamespace(bot=bot))
        self.assertEqual(router.posts["/api/live/press"]({"page": "main", "key": 0}), {"ok": True})
        self.assertEqual(free, [True, True])


class ValidateColors(unittest.TestCase):
    def test_bad_colors_are_reported(self):
        ed = types.SimpleNamespace(bot=types.SimpleNamespace(actions={}))
        cfg = {"keys": {"saved": {"label": "Saved", "fg": "transparent"}},
               "pages": {"main": [[{"label": "Twitch", "bg": "9146FF"}, {"label": "Fine", "bg": "#9146ff"},
                                   {"label": "Live", "on": {"bg": ""}}, "saved"]]}}
        problems = Editor.validate(ed, cfg)
        self.assertEqual(len(problems), 3, problems)
        self.assertTrue(any("Twitch" in p and "'9146FF'" in p for p in problems))
        self.assertTrue(any("Live" in p and "bg ''" in p for p in problems))
        self.assertTrue(any("Saved" in p and "fg 'transparent'" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
