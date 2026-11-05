"""Several Stream Decks at once: each has its own layout, page, history and brightness, while
modules are shared. Keys act on the deck they were pressed on; one deck failing or being
unplugged leaves the others running; module and config errors never cost a deck."""
import os
import queue
import time
import unittest
from unittest import mock

from tests.helpers import ROOT, Deck, tmpdir

import yaml  # noqa: E402

from core import Module, task  # noqa: E402
from core.bot import DeckBot  # noqa: E402

XL, MINI = "XL-0001", "MINI-0001"


def config(**extra):
    page = lambda label, action=None: {"label": label, **({"action": action} if action else {})}  # noqa: E731
    return {
        "brightness": 70,
        "layouts": {
            "8x4": {"start_page": "main", "pages": {
                "main": [page("XL main", {"page": "two"})],
                "two": [page("XL two")],
            }},
            "3x2": {"pages": {
                "home": [page("Mini home", {"page": "other"})],
                "other": [page("Mini other")],
            }},
        },
        **extra,
    }


class Recorder(Deck):
    """A fake deck that remembers its brightness and can be made to fail like unplugged hardware."""
    level = None
    broken = False

    def set_brightness(self, level):
        self.level = level

    def set_key_image(self, *a):
        if self.broken:
            raise OSError("device gone")


class Boom(Module):
    @task(every=0.01)
    def explode(self):
        raise RuntimeError("module bug")


def make_bot(cfg, *decks):
    path = os.path.join(tmpdir(), "config.yaml")
    with open(path, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)
    bot = DeckBot(root=ROOT, config_path=path)
    bot.load_config(initial=True)
    bot.package = "modules"
    bot.load_extension("modules.navigation")
    for deck in decks:
        bot.attach_deck(deck)
    return bot


def wait_for(cond, timeout=3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def events(q, kind):
    out = []
    while True:
        try:
            ev = q.get_nowait()
        except queue.Empty:
            return out
        if ev and ev["type"] == kind:
            out.append(ev["data"])


class XLAndMini(unittest.TestCase):
    def setUp(self):
        self.xl, self.mini = Recorder("Stream Deck XL", serial=XL), Recorder("Stream Deck Mini", serial=MINI)
        self.bot = make_bot(config(), self.xl, self.mini)
        self.a, self.b = self.bot.session(XL), self.bot.session(MINI)

    def test_each_deck_gets_its_layout(self):
        self.assertEqual(list(self.bot.sessions), [XL, MINI])
        self.assertEqual((self.a.layout_name, self.a.page), ("8x4", "main"))
        self.assertEqual((self.b.layout_name, self.b.page), ("3x2", "home"))
        self.assertEqual(len(self.b.raw_buttons()), 6)

    def test_pages_are_independent(self):
        self.a.goto("two")
        self.assertEqual((self.a.page, self.b.page), ("two", "home"))
        self.b.goto("other")
        self.b.goto("back")
        self.assertEqual((self.a.page, self.b.page), ("two", "home"))
        with self.assertRaises(ValueError):
            self.b.goto("two")   # the XL's page, not the Mini's

    def test_key_acts_on_its_own_deck(self):
        self.bot.on_key(self.mini, 0, True)
        self.bot.on_key(self.mini, 0, False)
        self.assertTrue(wait_for(lambda: self.b.page == "other"))
        self.assertEqual(self.a.page, "main")
        self.bot.on_key(self.xl, 0, True)
        self.bot.on_key(self.xl, 0, False)
        self.assertTrue(wait_for(lambda: self.a.page == "two"))
        self.assertEqual(self.b.page, "other")

    def test_brightness_action_acts_on_its_own_deck(self):
        self.bot.run_action({"brightness": 20}, serial=MINI)
        self.assertEqual((self.a.brightness, self.b.brightness), (70, 20))
        self.assertEqual(self.mini.level, 20)
        self.bot.run_action({"brightness": "sleep"}, serial=MINI)
        self.assertEqual((self.a.brightness, self.b.brightness), (70, 0))
        # deck_asleep is polled per deck: lit on the Mini only.
        for s in (self.a, self.b):
            s.pages[s.page][1] = {"label": "zz", "state": {"deck_asleep": True}}
        self.bot.poll()
        self.assertEqual((self.a.states.get((self.a.page, 1), False), self.b.states.get((self.b.page, 1))),
                         (False, True))
        self.bot.run_action({"brightness": "sleep"}, serial=MINI)   # wake: back to its own level
        self.assertEqual((self.a.brightness, self.b.brightness), (70, 20))

    def test_back_compat_is_the_primary(self):
        self.assertIs(self.bot.primary, self.a)
        self.assertIs(self.bot.deck, self.xl)
        self.assertEqual(self.bot.page, "main")
        self.assertEqual(self.bot.buttons(), self.a.buttons())
        self.bot.goto("two")
        self.assertEqual((self.a.page, self.b.page), ("two", "home"))
        self.assertEqual(self.bot.history, ["main"])
        self.assertEqual(self.bot.brightness, 70)

    def test_events_carry_the_serial(self):
        q = self.bot.hub.subscribe()
        self.addCleanup(self.bot.hub.unsubscribe, q)
        self.b.goto("other")
        self.assertEqual(events(q, "page"), [{"deck": MINI, "page": "other"}])
        self.a.draw_key(0)
        self.assertEqual(events(q, "key"), [{"deck": XL, "page": "main", "key": 0}])
        self.bot.detach_deck(MINI)
        decks = events(q, "decks")
        self.assertEqual([[d["serial"] for d in e["decks"]] for e in decks], [[XL]])
        info = decks[0]["decks"][0]
        self.assertEqual((info["model"], info["rows"], info["cols"], info["page"]), ("Stream Deck XL", 4, 8, "main"))

    def test_detaching_one_leaves_the_other(self):
        self.bot.detach_deck(XL)
        self.assertEqual(list(self.bot.sessions), [MINI])
        self.assertIs(self.bot.primary, self.b)
        self.bot.goto("other")
        self.assertEqual(self.b.page, "other")
        self.bot.attach_deck(self.xl)   # replugged: comes back on the page it was on
        # ... and, being the bigger deck, is the primary again (not "last plugged in")
        self.assertEqual(list(self.bot.sessions), [XL, MINI])

    def test_failing_deck_is_detached_alone(self):
        self.mini.broken = True
        with mock.patch("core.bot.DeviceManager") as dm:
            dm.return_value.enumerate.return_value = []
            self.b.next_poll.clear()
            self.bot.invalidate()        # the Mini's writes fail; it's marked failed
            self.bot.tick()
        self.assertEqual(list(self.bot.sessions), [XL])

    def test_module_errors_never_detach(self):
        self.bot.add_module(Boom(self.bot))
        with mock.patch("core.bot.DeviceManager") as dm, \
                mock.patch.object(self.bot, "watch_extensions", side_effect=RuntimeError("reload bug")), \
                self.assertLogs("deck", "ERROR"):
            dm.return_value.enumerate.return_value = []
            for _ in range(3):
                self.bot._last_watch = 0
                self.bot.tick()
                time.sleep(0.02)
        self.assertEqual(list(self.bot.sessions), [XL, MINI])

    def test_key_task_error_never_detaches(self):
        def bad(arg):
            raise OSError("action failed")
        bad.__deck_action__ = ("bad", False)
        self.bot.actions["bad"] = bad
        self.a.pages["main"][1] = {"label": "Bad", "action": {"bad": 1}}
        self.bot.on_key(self.xl, 1, True)
        time.sleep(0.2)
        with mock.patch("core.bot.DeviceManager") as dm:
            dm.return_value.enumerate.return_value = []
            self.bot.tick()
        self.assertEqual(list(self.bot.sessions), [XL, MINI])


class Config(unittest.TestCase):
    def test_device_layout_brightness_and_start_page(self):
        cfg = config(devices={"XL-2": {"name": "Side", "layout": "3x2", "brightness": 30, "start_page": "other"}})
        one, two = Recorder("Stream Deck XL", serial="XL-1"), Recorder("Stream Deck XL", serial="XL-2")
        bot = make_bot(cfg, one, two)
        a, b = bot.session("XL-1"), bot.session("XL-2")
        self.assertEqual((a.layout_name, a.page, a.brightness, a.name), ("8x4", "main", 70, "Stream Deck XL"))
        self.assertEqual((b.layout_name, b.page, b.brightness, b.name), ("3x2", "other", 30, "Side"))
        self.assertEqual((one.level, two.level), (70, 30))
        self.assertEqual(b.raw_buttons("home")[0]["label"], "Mini home")
        a.goto("two")
        self.assertEqual(b.page, "other")

    def test_unknown_device_layout_falls_back(self):
        bot = make_bot(config(devices={"XL-1": {"layout": "nope"}}), Recorder("Stream Deck XL", serial="XL-1"))
        self.assertEqual(bot.primary.layout_name, "8x4")

    def test_legacy_device_drives_only_that_deck(self):
        xl, mini = Recorder("Stream Deck XL", serial=XL), Recorder("Stream Deck Mini", serial=MINI)
        bot = make_bot(config(device={"serial": MINI}))
        q = bot.hub.subscribe()
        self.addCleanup(bot.hub.unsubscribe, q)
        with mock.patch("core.bot.DeviceManager") as dm:
            dm.return_value.enumerate.return_value = [xl, mini]
            bot.scan_devices()
            bot.scan_devices()
        self.assertEqual(list(bot.sessions), [MINI])
        self.assertEqual(len(events(q, "decks")), 1)

    def test_scan_attaches_every_deck_once(self):
        xl, mini = Recorder("Stream Deck XL", serial=XL), Recorder("Stream Deck Mini", serial=MINI)
        bot = make_bot(config())
        with mock.patch("core.bot.DeviceManager") as dm:
            dm.return_value.enumerate.return_value = [xl]
            bot.tick()
            dm.return_value.enumerate.return_value = [xl, mini]
            bot.tick()                              # too soon to enumerate again
            self.assertEqual(list(bot.sessions), [XL])
            bot._last_scan = 0
            bot.tick()
        self.assertEqual(list(bot.sessions), [XL, MINI])

    def test_reload_publishes_decks_and_keeps_pages(self):
        bot = make_bot(config(), Recorder("Stream Deck XL", serial=XL), Recorder("Stream Deck Mini", serial=MINI))
        bot.session(MINI).goto("other")
        q = bot.hub.subscribe()
        self.addCleanup(bot.hub.unsubscribe, q)
        with open(bot.config_path, "w") as f:
            yaml.safe_dump(config(brightness=40), f)
        os.utime(bot.config_path, (time.time() + 5, time.time() + 5))
        self.assertTrue(bot.load_config())
        bot.apply_config()
        self.assertEqual(len(events(q, "decks")), 1)
        self.assertEqual(bot.session(MINI).page, "other")
        self.assertEqual([s.brightness for s in bot.sessions.values()], [40, 40])

    def test_assigning_deck_replaces_the_primary(self):
        bot = make_bot(config())
        bot.deck = Deck("Stream Deck XL")
        self.assertEqual(bot.primary.serial, "FAKE-STREAM-DECK-XL")
        bot.attach_deck(Deck("Stream Deck Mini", serial=MINI))
        bot.deck = Deck("Stream Deck XL", serial="OTHER")
        self.assertEqual(list(bot.sessions), ["OTHER", MINI])
        bot.deck = None
        self.assertEqual(list(bot.sessions), [MINI])


class PrimaryIsBiggest(unittest.TestCase):
    def test_xl_is_primary_even_if_the_mini_is_found_first(self):
        import tempfile, os
        from core.bot import DeckBot
        from tests.helpers import Deck, EXAMPLE, ROOT
        bot = DeckBot(root=ROOT, config_path=EXAMPLE)
        bot.load_config(initial=True)
        bot.attach_deck(Deck("Stream Deck Mini", serial="MINI-FIRST"))
        bot.attach_deck(Deck("Stream Deck XL", serial="XL-SECOND"))
        self.assertEqual(bot.primary.serial, "XL-SECOND")
        self.assertEqual(list(bot.sessions), ["XL-SECOND", "MINI-FIRST"])


if __name__ == "__main__":
    unittest.main()
