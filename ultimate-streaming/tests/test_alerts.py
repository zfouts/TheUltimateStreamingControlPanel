"""Twitch alerts (modules/alerts.py) against fake Twitch / BRB modules and an injected clock:
the worker's rounds are run by hand (_tick), so nothing waits for real and nothing reaches the
network. One test runs the real worker thread."""
import builtins
import json
import logging
import os
import re
import stat
import time
import unittest
from unittest import mock

from tests.helpers import tmpdir

from core.module import REWARD_DENIED  # noqa: E402
from core.webapi import ApiError  # noqa: E402
import modules.alerts as al  # noqa: E402


def wait_for(pred, timeout=3):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.005)
    return False


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class FakeTwitch:
    def __init__(self):
        self.is_linked, self.live = True, True
        self.chat, self.shoutouts, self.status_calls = [], [], 0
        self.chat_error = None
        self.shoutout_errors = []         # raised in order, then success

    def linked(self):
        return self.is_linked

    def send_chat(self, text):
        self.chat.append(text)
        if self.chat_error:
            raise self.chat_error

    def stream_status(self):
        self.status_calls += 1
        return {"live": self.live}

    def shoutout_id(self, user_id):
        self.shoutouts.append(user_id)
        if self.shoutout_errors:
            raise self.shoutout_errors.pop(0)


class FakeBRB:
    def __init__(self):
        self.added = []

    def add_hype(self, amount):
        self.added.append(amount)


class FakeHub:
    def __init__(self):
        self.events = []

    def publish(self, kind, data=None):
        self.events.append((kind, data))


class FakeBot:
    def __init__(self):
        self.twitch, self.brb, self.hub = FakeTwitch(), FakeBRB(), FakeHub()

    def get_module(self, name):
        return {"Twitch": self.twitch, "BRB": self.brb}.get(name)


def ev(kind="follow", user="Alice", **kw):
    out = {"kind": kind, "user": user, "user_login": user.lower() if isinstance(user, str) and user.isalnum() else "",
           "user_id": "1234", "anonymous": False, "tier": 0, "months": 0, "cumulative_months": 0, "total": 0,
           "bits": 0, "viewers": 0, "message": "", "ts": 1.0}
    out.update(kw)
    return out


class Base(unittest.TestCase):
    def make(self, config=None, load=True):
        self.clock = Clock()
        self.bot = FakeBot()
        self.dir = tmpdir()
        m = al.Alerts(self.bot)
        m.wall = m.mono = self.clock
        m.dir = os.path.join(self.dir, "overlay")
        m.state_path = os.path.join(self.dir, "alerts.json")
        m.start_worker = False
        m.on_config(config if config is not None else {})
        if load:
            m.on_load()
        self.m = m
        return m

    def push(self, *events, tick=True):
        for e in events:
            self.m.on_twitch_event(e)
        if tick:
            self.m._tick()

    def read(self, name):
        try:
            with open(os.path.join(self.m.dir, name + ".txt"), encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return None

    def next_alert(self):
        """Let the current alert expire and the next one show."""
        self.clock.advance(self.m.cfg["show_seconds"])
        self.m._tick()
        self.clock.advance(al.GAP)
        self.m._tick()


class TestOverlay(Base):
    def test_each_kind_text_and_latest_file(self):
        self.make({"storm": {"others": 100}})
        cases = [
            (ev("follow", "Alice"), "Alice just followed!", "latest_follower", "Alice"),
            (ev("sub", "Bob", tier=1), "Bob just subscribed!", "latest_sub", "Bob"),
            (ev("resub", "Cara", months=1, cumulative_months=14), "Cara resubscribed for 14 months!",
             "latest_sub", "Cara"),
            (ev("gift", "Dan", total=5), "Dan gifted 5 subs!", "latest_sub", "Dan ×5"),
            (ev("cheer", "Eve", bits=250), "Eve cheered 250 bits!", "latest_cheer", "Eve · 250 bits"),
            (ev("raid", "Finn", viewers=42), "Finn is raiding with 42 viewers!", "latest_raid", "Finn · 42 viewers"),
        ]
        for i, (e, text, latest, latest_text) in enumerate(cases):
            self.push(e)
            if i:
                self.next_alert()
            self.assertEqual(self.read("alert"), text)
            self.assertEqual(self.read(latest), latest_text)
            self.assertEqual(self.bot.hub.events[-1][0], "alert")
            self.assertEqual(self.bot.hub.events[-1][1]["text"], text)
            self.assertEqual(set(self.bot.hub.events[-1][1]), {"kind", "user", "text", "ts"})
        self.clock.advance(6)
        self.m._tick()
        self.assertEqual(self.read("alert"), "")
        self.assertIsNone(self.m.snapshot()["showing"])

    def test_alert_blank_on_load_and_queue_drains_one_at_a_time(self):
        self.make()
        self.assertEqual(self.read("alert"), "")
        self.push(ev("follow", "A1"), ev("follow", "A2"))
        self.assertEqual(self.read("alert"), "A1 just followed!")
        self.assertEqual(self.m.snapshot()["queued"], 1)
        self.clock.advance(5.9)
        self.m._tick()
        self.assertEqual(self.read("alert"), "A1 just followed!")
        self.clock.advance(0.1)
        self.m._tick()
        self.assertEqual(self.read("alert"), "")
        self.clock.advance(al.GAP)
        self.m._tick()
        self.assertEqual(self.read("alert"), "A2 just followed!")

    def test_custom_text_newlines_stripped(self):
        self.make({"text": {"follow": "Welcome\n{user}\r!"}})
        self.push(ev("follow", "Zed"))
        self.assertEqual(self.read("alert"), "Welcome Zed !")

    def test_show_message_off(self):
        self.make()
        self.push(ev("resub", "Ann", cumulative_months=3, message="hello there"))
        self.assertEqual(self.read("alert"), "Ann resubscribed for 3 months!")

    def test_show_message_on_wraps_and_caps(self):
        self.make({"show_message": True, "blocklist": ["badword"]})
        msg = "x" * 49 + "y" * 60 + "z" * 300
        self.push(ev("cheer", "Ann", bits=100, message=msg))
        lines = self.read("alert").split("\n")
        self.assertEqual(lines[0], "Ann cheered 100 bits!")
        self.assertEqual(lines[1:], [msg[:50], msg[50:100]])
        # newlines in the message never add lines; the follow kind never shows a message
        self.push(ev("cheer", "Bo", bits=100, message="one\ntwo\r\nthree"), ev("follow", "Cy", message="hey"))
        self.next_alert()
        self.assertEqual(self.read("alert"), "Bo cheered 100 bits!\none two three")
        self.next_alert()
        self.assertEqual(self.read("alert"), "Cy just followed!")
        # a blocklisted message is dropped, the alert still shows
        self.push(ev("resub", "Di", cumulative_months=2, message="a BadWord here"))
        self.next_alert()
        self.assertEqual(self.read("alert"), "Di resubscribed for 2 months!")

    def test_render_single_pass_and_leading_bang(self):
        self.make({"chat": {"cheer": "{user} thanks for {bits} bits", "follow": "{user} welcome"}})
        self.push(ev("cheer", "{bits}", bits=100))
        self.assertEqual(self.read("alert"), "{bits} cheered 100 bits!")
        self.assertEqual(self.bot.twitch.chat, ["{bits} thanks for 100 bits"])
        self.push(ev("follow", "!ban"))
        self.assertEqual(self.bot.twitch.chat[-1], "ban welcome")
        self.next_alert()
        self.assertFalse(self.read("alert").startswith("!"))

    def test_malformed_events_dropped(self):
        self.make()
        bad = [None, "x", ev("host"), ev(user=""), ev(user="x" * 26), ev(bits=-1), ev(bits=10_000_001),
               ev(bits=True), ev(tier=4), ev(user_id="12a"), ev(user_login="Bad Login"),
               ev(message="m" * 501), ev(ts=float("nan")), ev(anonymous="no"), ev(user=5)]
        for b in bad:
            self.m.on_twitch_event(b)
        self.assertEqual(len(self.m.inbound), 0)
        self.m.on_twitch_event(ev())
        self.assertEqual(len(self.m.inbound), 1)

    def test_queue_bounded_drops_oldest(self):
        self.make()
        for i in range(al.QUEUE_MAX + 3):
            self.m.on_twitch_event(ev("follow", f"U{i}"))
        self.assertEqual(len(self.m.inbound), al.QUEUE_MAX)
        self.assertEqual(self.m.inbound[0]["user"], "U3")
        self.assertEqual(self.m.snapshot()["dropped"], 3)


class TestWriteWarnings(Base):
    def test_overlay_write_failure_warns_once_then_recovers(self):
        m = self.make()
        real = al.write_overlay
        failing = [True]

        def flaky(path, text):
            if failing[0]:
                raise OSError(28, "No space left on device")
            return real(path, text)

        with mock.patch.object(al, "write_overlay", flaky):
            with self.assertLogs("deck.Alerts", logging.DEBUG) as logs:
                self.push(ev("follow", "Alice"))
                for _ in range(3):
                    m._tick()
            levels = [r.levelno for r in logs.records if "alert.txt" in r.getMessage()]
            self.assertEqual(levels.count(logging.WARNING), 1)
            self.assertGreaterEqual(levels.count(logging.DEBUG), 2)
            failing[0] = False
            with self.assertLogs("deck.Alerts", logging.DEBUG) as logs:
                m._tick()
                m._tick()
            infos = [r for r in logs.records if r.levelno == logging.INFO]
            self.assertEqual(len(infos), 1)
            self.assertIn("works again", infos[0].getMessage())
        self.assertEqual(self.read("alert"), "Alice just followed!")


class TestBlocklist(Base):
    def test_substring_name(self):
        self.make({"blocklist": ["HateBot"], "chat": {"follow": "thanks {user}"}})
        self.push(ev("follow", "xxhatebotxx"))
        self.assertEqual(self.read("alert"), "Someone just followed!")
        self.assertEqual(self.bot.hub.events[-1][1]["user"], "Someone")
        self.assertEqual(self.bot.twitch.chat, [])
        self.assertIsNone(self.read("latest_follower"))
        self.assertEqual(self.bot.brb.added, [3])     # still counts for hype

    def test_fullwidth_and_zero_width_evasion(self):
        self.make({"blocklist": ["hatebot"]})
        self.push(ev("follow", "ＨＡＴＥ​ＢＯＴ1"))
        self.assertEqual(self.read("alert"), "Someone just followed!")

    def test_evasions_match(self):
        self.make({"blocklist": ["bad"]})
        for name in ("b\u0336a\u0336d\u0336", "b\u0430d", "ＢＡＤ", "b\u200bad", "BÁD", "bαd"):
            with self.subTest(name=name):
                self.assertTrue(al.blocked(self.m.cfg["blocklist"], name))
        self.assertFalse(al.blocked(self.m.cfg["blocklist"], "bed"))
        # entries are normalised the same way
        self.make({"blocklist": ["Ｂ\u0430D"]})
        self.assertTrue(al.blocked(self.m.cfg["blocklist"], "xxbadxx"))

    def test_blocked_name_hides_message(self):
        self.make({"show_message": True, "blocklist": ["hatebot"]})
        self.push(ev("cheer", "hatebot9", bits=100, message="look at me"))
        self.assertEqual(self.read("alert"), "Someone cheered 100 bits!")

    def test_catastrophic_regex_refused(self):
        with self.assertLogs("deck.Alerts", logging.WARNING) as logs:
            self.make({"blocklist": ["re:(a+)+$", "re:(\\w*)*x", "re:(x)\\1", "re:^free\\s+followers"]})
        self.assertEqual(len(self.m.cfg["blocklist"]), 1)
        text = "\n".join(logs.output)
        for i in range(3):
            self.assertIn(f"blocklist[{i}]", text)
        self.assertNotIn("blocklist[3]", text)
        self.assertTrue(al.blocked(self.m.cfg["blocklist"], "FREE   followers here"))

    def test_regex_risk_judged_on_parsed_pattern(self):
        for bad in ("((a+))+", "(a|aa)+$", "(?:x(y*))*", "(a)(?(1)b|c)", "(?i:(\\w+))+", ".*a.*b.*c",
                    "(?:\\w\\w?)+!", "(?:\\w?\\w?)+!", "(?:a?){30}a{30}", "a?" * 24 + "a" * 24, "\\w*\\w*\\w*!",
                    ".*.*.*=", "\\w+\\S+\\w+!", "\\s*\\w*\\w*!",
                    # found by review and fuzzing: each ran for seconds on a message-length text
                    "\\w*\\s*x\\w*\\w*!", ".*\\s*x.*.*=", "(?:\\w*\\w*|!)\\w*!", "\\w*\\w*[一-鿿]+\\w*!",
                    "\\w*\\w*\\w?\\w?!", "(?:a|)" * 32 + "!", "(?:\\w|)" * 27 + "!", "(?:a|aa)" * 22 + "!",
                    "(?:\\w*\\w*)?\\w*!", ".*a+[a-z]{2}=", ".+.+(?:ab){2}", "\\w*\\w*$",
                    "\\w*?\\w*?(?>\\d|!)", "\\w*\\w*(?>\\d+)", "\\w*\\w*(?>(?=!))", "\\w*\\w*\\d++", "\\w*\\s*\\w*!",
                    "(?:x|\\w*\\w*)!"):
            self.assertIsNotNone(al.regex_risk(bad), bad)
        for ok in (".*spam.*", "^bot_\\d+$", "foo|bar", "(?:ab)+", "a*+b", "(?>a+)+", "x{2}y", "[a-z]+[0-9]+",
                   "^free\\s+followers\\s+at\\s+\\w+", "buy\\s+viewers\\s+\\S+", "https?://\\S+\\.\\S+", "(?:ab){3}",
                   "\\w+\\s+\\w+\\s+\\w+!", "\\bfree\\b.*\\bfollow", "colou?r", "(com|net|org)$",
                   "(free|cheap)\\s+(followers|viewers)", "^(?:buy|get) (?:cheap )?followers",
                   "\\bfree\\s+(?:followers|viewers|primes)\\b", "best\\s+(viewers|followers)\\s+on\\s+\\S+",
                   "\\d+\\s*(?:followers|viewers)\\s+for\\s+\\$?\\d+", "follow(er)?s?\\s+(at|on)\\b",
                   "(?:bit\\.ly|tinyurl\\.com)/\\w+"):
            self.assertIsNone(al.regex_risk(ok), ok)
        with self.assertLogs("deck.Alerts", logging.WARNING):
            self.make({"blocklist": ["re:((a+))+", "re:(a|aa)+$", "re:^ok$"]})
        self.assertEqual(len(self.m.cfg["blocklist"]), 1)

    def test_slow_regex_refused_by_timing(self):
        ticks = iter(range(100))
        self.assertTrue(al.regex_too_slow(re.compile("x"), clock=lambda: next(ticks)))
        self.assertFalse(al.regex_too_slow(re.compile("^free\\s+followers")))
        real = al.regex_too_slow
        al.regex_too_slow = lambda compiled: compiled.pattern == "slow"
        self.addCleanup(setattr, al, "regex_too_slow", real)
        with self.assertLogs("deck.Alerts", logging.WARNING) as logs:
            self.make({"blocklist": ["re:slow", "re:fast"]})
        self.assertIn("too slow", logs.output[0])
        self.assertEqual(len(self.m.cfg["blocklist"]), 1)

    def test_login_match_and_regex(self):
        self.make({"blocklist": ["re:^bot_\\d+$", "re:(", 5], "shoutout": {"on_raid": True, "min_viewers": 0},
                   "chat": {"raid": "welcome {user}"}})
        self.push(ev("raid", "Nice Name", user_login="bot_123", viewers=50))
        self.assertEqual(self.read("alert"), "Someone is raiding with 50 viewers!")
        self.assertIsNone(self.m.pending)
        self.assertIsNone(self.m.last_raider())
        self.assertEqual(self.bot.twitch.chat, [])
        self.assertIsNone(self.read("latest_raid"))
        self.assertEqual(len(self.m.cfg["blocklist"]), 1)

    def test_invalid_regex_warns(self):
        with self.assertLogs("deck.Alerts", logging.WARNING) as logs:
            self.make({"blocklist": ["re:[unclosed", "ok"]})
        self.assertTrue(any("blocklist[0]" in line for line in logs.output))
        self.assertEqual(len(self.m.cfg["blocklist"]), 1)


class TestStorm(Base):
    def test_follow_storm_entry_and_exit(self):
        self.make({"chat": {"follow": "hi {user}"}})
        for i in range(5):
            self.push(ev("follow", f"Bot{i}"))
            self.clock.advance(0.5)
        self.assertFalse(self.m.snapshot()["storm"]["follow"])
        self.assertEqual(self.read("latest_follower"), "Bot4")
        self.push(ev("follow", "Bot5"))
        snap = self.m.snapshot()
        self.assertTrue(snap["storm"]["follow"])
        self.assertEqual(self.read("alert"), "6 new followers!")
        self.assertEqual(snap["queued"], 0)                       # queued names dropped
        self.push(ev("follow", "Bot6"), ev("follow", "Bot7"))
        self.assertEqual(self.read("alert"), "8 new followers!")  # one updating alert
        self.assertEqual(self.m.snapshot()["queued"], 0)
        self.assertEqual(self.read("latest_follower"), "Bot4")
        self.assertEqual(self.bot.twitch.chat, ["hi Bot0"])       # per-kind cooldown, then storm
        self.assertNotIn("Bot7", self.bot.hub.events[-1][1]["text"])
        self.assertEqual(self.bot.hub.events[-1][1]["user"], "")
        # an "others" event is not affected by a follow storm
        self.push(ev("sub", "RealSub"))
        self.next_alert()
        self.assertEqual(self.read("alert"), "RealSub just subscribed!")
        # window empties: storm over, names again
        self.clock.advance(31)
        self.m._tick()
        self.assertFalse(self.m.snapshot()["storm"]["follow"])
        self.push(ev("follow", "Human"))
        self.clock.advance(al.GAP)
        self.m._tick()
        self.assertEqual(self.read("alert"), "Human just followed!")
        self.assertEqual(self.read("latest_follower"), "Human")

    def test_raid_storm_suppresses_shoutout_and_chat(self):
        self.make({"shoutout": {"on_raid": True, "min_viewers": 0}, "chat": {"raid": "welcome {user}"}})
        tw = self.bot.twitch
        tw.live = True
        self.m.so_last = self.clock() - 10        # global cooldown: the first raid waits as pending
        for i in range(3):
            self.push(ev("raid", f"Raider{i}", user_id=str(100 + i), viewers=20))
        self.assertEqual(self.m.snapshot()["shoutout"]["pending"], "raider2")
        self.push(ev("raid", "Raider3", user_id="103", viewers=20))
        snap = self.m.snapshot()
        self.assertTrue(snap["storm"]["others"])
        self.assertIsNone(snap["shoutout"]["pending"])
        self.assertEqual(self.read("alert"), "4 raids!")
        self.assertEqual(self.read("latest_raid"), "Raider2 · 20 viewers")
        self.assertEqual(tw.chat, ["welcome Raider0"])
        self.clock.advance(200)
        self.m._tick()
        self.assertEqual(tw.shoutouts, [])

    def test_mixed_others_storm_texts(self):
        self.make()
        self.push(ev("sub", "A"), ev("resub", "B"), ev("gift", "C", total=2), ev("cheer", "D", bits=5))
        self.assertTrue(self.m.snapshot()["storm"]["others"])
        self.assertEqual(self.read("alert"), "1 cheers!")
        self.assertEqual(self.m.snapshot()["queued"], 0)       # the three names were dropped
        self.push(ev("sub", "E"))
        self.assertEqual(self.m.snapshot()["queued"], 1)
        self.next_alert()
        self.assertEqual(self.read("alert"), "4 new subs!")    # sub, resub, gift and sub in the window


class TestChat(Base):
    def test_cooldowns(self):
        self.make({"storm": {"others": 100, "follows": 100},
                   "chat": {k: f"{k} {{user}}" for k in al.KINDS}})
        tw = self.bot.twitch
        self.push(ev("follow", "A"))
        self.clock.advance(5)
        self.push(ev("follow", "B"))                  # per-kind cooldown
        self.assertEqual(tw.chat, ["follow A"])
        self.clock.advance(5)
        self.push(ev("follow", "C"))
        self.assertEqual(tw.chat, ["follow A", "follow C"])
        for k in ("sub", "resub", "gift", "cheer"):
            self.push(ev(k, "D"))
        self.assertEqual(len(tw.chat), 5)             # 5 per 30 s
        self.push(ev("raid", "E", viewers=1))
        self.assertEqual(len(tw.chat), 5)
        self.clock.advance(25)
        self.push(ev("raid", "F", viewers=1))
        self.assertEqual(tw.chat[-1], "raid F")

    def test_429_dropped_not_retried(self):
        self.make({"chat": {"follow": "hi {user}"}})
        tw = self.bot.twitch
        tw.chat_error = ApiError("busy", 429)
        self.push(ev("follow", "A"))
        tw.chat_error = None
        for _ in range(5):
            self.clock.advance(3)
            self.m._tick()
        self.assertEqual(tw.chat, ["hi A"])

    def test_message_never_in_chat(self):
        viewer_text = "VIEWER-MESSAGE-TEXT"
        self.make({"show_message": True, "chat": {"resub": "{user} {message} {months}"}})
        self.push(ev("resub", "A", cumulative_months=4, message=viewer_text))
        self.assertEqual(self.bot.twitch.chat, ["A {message} 4"])
        self.assertIn(viewer_text, self.read("alert"))
        self.assertNotIn(viewer_text, json.dumps(self.bot.hub.events))

    def test_no_chat_when_unlinked_test_or_anonymous_raid(self):
        self.make({"chat": {"follow": "hi {user}", "raid": "hi {user}"}, "storm": {"others": 100}})
        tw = self.bot.twitch
        tw.is_linked = False
        self.push(ev("follow", "A"))
        tw.is_linked = True
        self.m.alerts_key("test")
        self.m._tick()
        self.push(ev("raid", "Anonymous", user_login="", user_id="", anonymous=True, viewers=3))
        self.assertEqual(tw.chat, [])
        self.bot.twitch = None
        self.clock.advance(11)
        self.push(ev("follow", "B"))                  # Twitch module missing: skipped, no crash
        self.assertEqual(self.read("latest_follower"), "B")


class TestShoutout(Base):
    SO = {"on_raid": True, "min_viewers": 10}

    def raid(self, login="raider", uid="555", viewers=20, **kw):
        self.push(ev("raid", login.capitalize(), user_login=login, user_id=uid, viewers=viewers, **kw))

    def test_off_by_default(self):
        self.make()
        self.raid()
        self.assertEqual(self.bot.twitch.shoutouts, [])
        self.assertEqual(self.bot.twitch.status_calls, 0)

    def test_success_and_gates(self):
        self.make({"shoutout": dict(self.SO, deny=["denied"]), "storm": {"others": 100}})
        tw = self.bot.twitch
        self.raid(viewers=9)
        self.raid(login="denied", uid="1")
        self.raid(login="anon", uid="")
        self.assertEqual(tw.shoutouts, [])
        self.raid()
        self.assertEqual(tw.shoutouts, ["555"])
        last = self.m.snapshot()["shoutout"]["last"]
        self.assertEqual((last["login"], last["ok"], last["error"]), ("raider", True, None))

    def test_allow_list(self):
        self.make({"shoutout": dict(self.SO, allow=["Friend"])})
        self.raid(login="stranger", uid="1")
        self.assertEqual(self.bot.twitch.shoutouts, [])
        self.raid(login="friend", uid="2")
        self.assertEqual(self.bot.twitch.shoutouts, ["2"])

    def test_not_live_or_unlinked(self):
        self.make({"shoutout": self.SO})
        tw = self.bot.twitch
        tw.live = False
        self.raid()
        self.assertEqual(tw.shoutouts, [])
        self.assertIsNone(self.m.pending)
        tw.live, tw.is_linked = True, False
        self.clock.advance(130)
        self.raid(login="other", uid="9")
        self.assertEqual(tw.shoutouts, [])
        self.assertEqual(tw.status_calls, 1)

    def test_slow_live_check_does_not_overwrite_a_newer_shoutout_time(self):
        self.make({"shoutout": self.SO})
        tw = self.bot.twitch
        tw.live = True

        def slow_status():                              # a key shouts out while the live check is in flight
            self.clock.advance(5)
            self.m.note_shoutout("777")
            return {"live": True}
        tw.stream_status = slow_status
        self.raid(login="other", uid="1")
        self.assertEqual(tw.shoutouts, ["1"])
        self.assertEqual(self.m.so_last, self.clock())

    def test_manual_shoutout_shares_cooldowns(self):
        self.make({"shoutout": self.SO})
        tw = self.bot.twitch
        self.m.note_shoutout("777")                     # a deck key shouted someone out just now
        self.raid(login="other", uid="1")
        self.assertEqual(tw.shoutouts, [])              # waits out Twitch's global cooldown
        self.assertEqual(self.m.snapshot()["shoutout"]["pending"], "other")
        self.clock.advance(al.SHOUTOUT_GAP)
        self.m._tick()
        self.assertEqual(tw.shoutouts, ["1"])
        self.clock.advance(al.SHOUTOUT_GAP)
        self.raid(login="manual", uid="777")            # same channel within the hour: skipped
        self.assertIsNone(self.m.pending)
        self.assertEqual(tw.shoutouts, ["1"])
        self.m.note_shoutout("not an id")               # ignored
        self.m.note_shoutout(5)

    def test_manual_shoutout_of_pending_raider_clears_it(self):
        self.make({"shoutout": self.SO})
        self.m.so_last = self.clock()
        self.raid(login="raider", uid="555")
        self.assertIsNotNone(self.m.pending)
        self.m.note_shoutout("555")
        self.assertIsNone(self.m.pending)

    def test_global_cooldown_pending_replaced_and_user_cooldown(self):
        self.make({"shoutout": self.SO, "storm": {"others": 100}})
        tw = self.bot.twitch
        self.raid(login="first", uid="1")
        self.assertEqual(tw.shoutouts, ["1"])
        status = tw.status_calls
        self.clock.advance(10)
        self.raid(login="second", uid="2")
        self.raid(login="third", uid="3")              # replaces the pending one
        self.assertEqual(self.m.snapshot()["shoutout"]["pending"], "third")
        self.assertEqual(tw.status_calls, status)      # cooldown checked before any API call
        self.clock.advance(110)
        self.m._tick()
        self.assertEqual(tw.shoutouts, ["1"])
        self.clock.advance(5)
        self.m._tick()
        self.assertEqual(tw.shoutouts, ["1", "3"])
        # per-user: "first" again within the hour is never queued
        self.clock.advance(1000)
        self.raid(login="first", uid="1")
        self.assertIsNone(self.m.pending)
        self.assertEqual(tw.shoutouts, ["1", "3"])
        self.clock.advance(3600)
        self.raid(login="first", uid="1")
        self.assertEqual(tw.shoutouts, ["1", "3", "1"])

    def test_pending_rechecks_current_config(self):
        cases = [("deny", {"deny": ["raider"]}, None), ("min_viewers", {"min_viewers": 50}, None),
                 ("allow", {"allow": ["someoneelse"]}, None), ("blocklist", {}, ["raid"]),
                 ("off", {"on_raid": False}, None)]
        for name, change, blocklist in cases:
            with self.subTest(name):
                self.make({"shoutout": self.SO})
                self.m.so_last = self.clock()
                self.raid()
                self.assertEqual(self.m.snapshot()["shoutout"]["pending"], "raider")
                cfg = {"shoutout": dict(self.SO, **change)}
                if blocklist:
                    cfg["blocklist"] = blocklist
                self.m.on_config(cfg)
                self.clock.advance(130)
                self.m._tick()
                self.assertIsNone(self.m.pending)
                self.assertEqual(self.bot.twitch.shoutouts, [])
                self.assertEqual(self.bot.twitch.status_calls, 0)
        # unchanged config: it goes out
        self.make({"shoutout": self.SO})
        self.m.so_last = self.clock()
        self.raid()
        self.clock.advance(130)
        self.m._tick()
        self.assertEqual(self.bot.twitch.shoutouts, ["555"])

    def test_pending_expires(self):
        self.make({"shoutout": self.SO})
        self.m.so_last = self.clock()
        self.raid()
        self.assertEqual(self.m.snapshot()["shoutout"]["pending"], "raider")
        self.clock.advance(601)
        self.m.so_last = self.clock() - 1             # still blocked when it expires
        self.m._tick()
        self.assertIsNone(self.m.pending)
        self.clock.advance(200)
        self.m._tick()
        self.assertEqual(self.bot.twitch.shoutouts, [])

    def test_errors(self):
        self.make({"shoutout": self.SO, "storm": {"others": 100}})
        tw = self.bot.twitch
        tw.shoutout_errors = [ApiError("nope", 400)]
        self.raid(login="a", uid="1")
        last = self.m.snapshot()["shoutout"]["last"]
        self.assertFalse(last["ok"])
        self.assertIn("400", last["error"])
        self.clock.advance(200)
        self.m._tick()
        self.assertEqual(tw.shoutouts, ["1"])          # no retry
        # 429: once more at the next slot, then given up
        tw.shoutout_errors = [ApiError("slow", 429), ApiError("slow", 429)]
        self.raid(login="b", uid="2")
        self.assertEqual(self.m.snapshot()["shoutout"]["pending"], "b")
        self.clock.advance(100)
        self.m._tick()
        self.assertEqual(tw.shoutouts, ["1", "2"])
        self.clock.advance(30)
        self.m._tick()
        self.assertEqual(tw.shoutouts, ["1", "2", "2"])
        self.clock.advance(200)
        self.m._tick()
        self.assertEqual(tw.shoutouts, ["1", "2", "2"])
        self.assertIsNone(self.m.pending)

    def test_last_raider_and_display(self):
        self.make()
        self.assertIsNone(self.m.last_raider())
        self.assertEqual(self.m.last_raider_display({}), {})
        self.raid(login="averyveryverylongname", uid="77", viewers=31)
        r = self.m.last_raider()
        self.assertEqual({k: r[k] for k in ("login", "user_id", "viewers")},
                         {"login": "averyveryverylongname", "user_id": "77", "viewers": 31})
        r["login"] = "changed"
        self.assertEqual(self.m.last_raider()["login"], "averyveryverylongname")
        self.assertEqual(self.m.last_raider_display({}), {"label": "Raid from\nAveryveryver\n31"})


class TestHype(Base):
    def test_amounts_and_cap(self):
        self.make({"storm": {"others": 100}})
        brb = self.bot.brb
        self.push(ev("follow"), ev("sub"), ev("resub"), ev("gift", total=3), ev("cheer", bits=150),
                  ev("raid", viewers=55), ev("gift", total=50), ev("cheer", bits=1))
        self.assertEqual(brb.added, [3, 10, 10, 12, 7.5, 5.5, 20, 0.05])

    def test_custom_and_zero(self):
        self.make({"hype": {"follow": 0, "max_per_event": 5, "sub": 50}})
        self.push(ev("follow"), ev("sub"))
        self.assertEqual(self.bot.brb.added, [5])
        self.bot.brb = None
        self.push(ev("sub", "B"))                     # no BRB module: fine


class TestDeck(Base):
    def test_pause_resume_toggle(self):
        self.make({"chat": {"follow": "hi"}})
        self.push(ev("follow", "A"), ev("follow", "B"))
        self.m.alerts_key("pause")
        self.assertTrue(self.m.alerts_state("paused", {}))
        self.m._tick()
        self.assertEqual(self.read("alert"), "")
        self.assertEqual(self.m.snapshot()["queued"], 0)
        hype, chat = len(self.bot.brb.added), len(self.bot.twitch.chat)
        self.clock.advance(20)
        self.push(ev("follow", "C"))
        self.assertEqual(self.read("alert"), "")
        self.assertEqual((len(self.bot.brb.added), len(self.bot.twitch.chat)), (hype, chat))
        with self.assertRaises(RuntimeError):
            self.m.alerts_key("test")
        self.m.alerts_key("toggle")
        self.assertFalse(self.m.alerts_state("paused", {}))
        self.push(ev("follow", "D"))
        self.assertEqual(self.read("alert"), "D just followed!")
        self.m.alerts_key("toggle")
        self.assertTrue(self.m.paused)
        self.m.alerts_key("resume")
        self.assertFalse(self.m.paused)
        with self.assertRaises(ValueError):
            self.m.alerts_key("explode")
        with self.assertRaises(ValueError):
            self.m.alerts_state("nope", {})

    def test_pause_drops_pending_shoutout(self):
        self.make({"shoutout": {"on_raid": True, "min_viewers": 0}})
        self.m.so_last = self.clock()
        self.push(ev("raid", "R", user_login="r", viewers=3))
        self.assertIsNotNone(self.m.pending)
        self.m.alerts_key("pause")
        self.clock.advance(200)
        self.m._tick()
        self.assertEqual(self.bot.twitch.shoutouts, [])

    def test_skip(self):
        self.make()
        self.push(ev("follow", "A"), ev("follow", "B"))
        self.m.alerts_key("skip")
        self.m._tick()
        self.assertEqual(self.read("alert"), "")
        self.assertEqual(self.m.snapshot()["queued"], 0)
        self.assertFalse(self.m.paused)

    def test_skip_keeps_unprocessed_events(self):
        self.make({"chat": {"raid": "welcome {user}"}})
        self.push(ev("follow", "A"))
        self.push(ev("raid", "Raider", user_login="raider", user_id="555", viewers=5), tick=False)
        self.m.alerts_key("skip")                       # pressed before the worker saw the raid
        self.m._tick()
        self.assertEqual(self.read("latest_raid"), "Raider · 5 viewers")
        self.assertEqual(self.m.last_raider()["user_id"], "555")
        self.assertEqual(self.bot.twitch.chat, ["welcome Raider"])

    def test_test_alert_is_overlay_only(self):
        self.make({"chat": {"follow": "hi {user}"}, "shoutout": {"on_raid": True}})
        self.m.alerts_key("test")
        self.m._tick()
        self.assertEqual(self.read("alert"), "TestFollower just followed!")
        self.assertEqual(self.bot.twitch.chat, [])
        self.assertEqual(self.bot.brb.added, [])
        self.assertIsNone(self.read("latest_follower"))
        self.assertFalse(os.path.exists(self.m.state_path))
        self.assertEqual(self.m.snapshot()["latest"], {})

    def test_disabled(self):
        self.make({"enabled": False})
        self.assertFalse(self.m.wants_events())
        self.push(ev("follow"))
        self.assertEqual(self.read("alert"), "")
        self.assertEqual(self.bot.brb.added, [])
        self.m.on_config({})
        self.assertTrue(self.m.wants_events())

    def test_registration(self):
        self.assertIn("alerts", REWARD_DENIED)
        ui = al.Alerts.alerts_key.__deck_ui__
        self.assertEqual(ui["group"], "Twitch")
        self.assertEqual([o["value"] for o in ui["arg"]["options"]], ["toggle", "pause", "resume", "skip", "test"])
        self.assertEqual(al.Alerts.on_twitch_event.__deck_listener__, "twitch_event")
        self.assertEqual(al.Alerts.last_raider_display.__deck_display__, "last_raider")

    def test_snapshot_shape(self):
        self.make()
        self.push(ev("follow", "A"))
        snap = self.m.snapshot()
        self.assertEqual(set(snap), {"enabled", "paused", "showing", "queued", "dropped", "storm", "latest",
                                     "last_raider", "shoutout"})
        self.assertEqual(snap["showing"], "A just followed!")
        self.assertEqual(set(snap["latest"]["follow"]), {"user", "ts"})
        json.dumps(snap)


class TestState(Base):
    def test_round_trip_and_perms(self):
        self.make({"storm": {"others": 100}})
        self.push(ev("follow", "F"), ev("gift", "G", total=4, message="secret msg"), ev("cheer", "C", bits=9),
                  ev("raid", "R", user_login="r", user_id="42", viewers=7))
        mode = stat.S_IMODE(os.stat(self.m.state_path).st_mode)
        self.assertEqual(mode, 0o600)
        with open(self.m.state_path) as f:
            raw = f.read()
        self.assertNotIn("secret", raw)
        self.assertEqual([n for n in os.listdir(self.dir) if n.endswith(".tmp")], [])
        path, odir = self.m.state_path, self.m.dir
        for name in ("latest_follower", "latest_sub", "latest_cheer", "latest_raid"):
            os.unlink(os.path.join(odir, name + ".txt"))
        m2 = al.Alerts(self.bot)
        m2.wall = m2.mono = self.clock
        m2.dir, m2.state_path, m2.start_worker = odir, path, False
        m2.on_config({})
        m2.on_load()
        self.m = m2
        self.assertEqual(self.read("latest_follower"), "F")
        self.assertEqual(self.read("latest_sub"), "G ×4")
        self.assertEqual(self.read("latest_cheer"), "C · 9 bits")
        self.assertEqual(self.read("latest_raid"), "R · 7 viewers")
        self.assertEqual(m2.last_raider(), {"login": "r", "user_id": "42", "name": "R", "viewers": 7,
                                            "ts": self.clock()})

    def write_state(self, data):
        with open(self.m.state_path, "w") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))

    def reload(self):
        m = al.Alerts(self.bot)
        m.wall = m.mono = self.clock
        m.dir, m.state_path, m.start_worker = self.m.dir, self.m.state_path, False
        m.on_config({})
        m.on_load()
        return m

    def test_tampered_state_rejected(self):
        self.make(load=False)
        now = self.clock()
        self.write_state({
            "latest": {"follow": {"user": "x" * 26, "ts": now}, "sub": {"user": "ok", "ts": now, "total": -1},
                       "cheer": {"user": "line\nbreak", "ts": now, "bits": 3},
                       "raid": {"user": "fine", "ts": now + 10 ** 7, "viewers": 1}},
            "last_raider": {"login": "Bad Login", "user_id": "1", "name": "n", "viewers": 1, "ts": now}})
        m = self.reload()
        self.assertEqual(m.snapshot()["latest"], {})
        self.assertIsNone(m.last_raider())
        self.write_state({"latest": {}, "last_raider": {"login": "ok", "user_id": "1x", "name": "n", "viewers": 1,
                                                        "ts": now}})
        self.assertIsNone(self.reload().last_raider())
        for junk in ("not json", "[1, 2]", json.dumps({"latest": "x", "last_raider": []})):
            self.write_state(junk)
            self.assertEqual(self.reload().snapshot()["latest"], {})

    def test_symlink_and_oversize_refused(self):
        self.make(load=False)
        target = os.path.join(self.dir, "elsewhere.json")
        with open(target, "w") as f:
            json.dump({"latest": {"follow": {"user": "Linked", "ts": 1.0}}}, f)
        os.symlink(target, self.m.state_path)
        self.assertEqual(self.reload().snapshot()["latest"], {})
        os.unlink(self.m.state_path)
        self.write_state(" " * (al.STATE_MAX + 1))
        self.assertEqual(self.reload().snapshot()["latest"], {})


class TestConfig(Base):
    def test_bad_values_default_with_warning(self):
        with self.assertLogs("deck.Alerts", logging.WARNING) as logs:
            self.make({"show_seconds": 99, "enabled": "yes", "text": {"follow": 5}, "storm": {"follows": 0},
                       "hype": {"sub": -1}, "shoutout": {"allow": ["ok", "not a login"]}, "chat": "x"})
        cfg = self.m.cfg
        self.assertEqual((cfg["show_seconds"], cfg["enabled"]), (6, True))
        self.assertEqual(cfg["text"]["follow"], al.DEFAULT_TEXT["follow"])
        self.assertEqual(cfg["storm"]["follows"], 5)
        self.assertEqual(cfg["hype"]["sub"], 10)
        self.assertEqual(cfg["shoutout"]["allow"], frozenset({"ok"}))
        text = "\n".join(logs.output)
        for key in ("alerts.show_seconds", "alerts.enabled", "alerts.text.follow", "alerts.storm.follows",
                    "alerts.hype.sub", "alerts.shoutout.allow[1]", "alerts.chat"):
            self.assertIn(key, text)

    def test_not_a_mapping(self):
        with self.assertLogs("deck.Alerts", logging.WARNING):
            self.make("nonsense")
        self.assertTrue(self.m.cfg["enabled"])

    def test_blocklist_limits(self):
        with self.assertLogs("deck.Alerts", logging.WARNING):
            self.make({"blocklist": [f"w{i}" for i in range(150)] + ["x" * 201]})
        self.assertEqual(len(self.m.cfg["blocklist"]), 100)


class TestThreading(Base):
    def test_listener_does_no_io(self):
        self.make({"chat": {"follow": "hi"}})
        boom = mock.Mock(side_effect=AssertionError("I/O on the listener thread"))
        with mock.patch.object(al, "write_overlay", boom), mock.patch.object(builtins, "open", boom), \
                mock.patch.object(os, "open", boom), mock.patch.object(os, "replace", boom):
            self.m.on_twitch_event(ev("follow", "A"))
            self.m.on_twitch_event(ev("raid", "B", user_login="b", viewers=99))
        self.assertEqual(len(self.m.inbound), 2)
        self.assertEqual(self.bot.twitch.chat, [])
        self.assertEqual(self.bot.twitch.status_calls, 0)
        self.assertEqual(self.bot.brb.added, [])
        self.assertEqual(self.bot.hub.events, [])
        boom.assert_not_called()

    def test_real_worker(self):
        self.make(load=False)
        self.m.wall, self.m.mono = time.time, time.monotonic
        self.m.start_worker = True
        self.m.on_load()
        try:
            self.assertEqual(self.m.worker.name, "alerts")
            self.m.on_twitch_event(ev("follow", "Live"))
            self.assertTrue(wait_for(lambda: self.read("alert") == "Live just followed!"))
        finally:
            start = time.monotonic()
            self.m.on_unload()
            self.assertLess(time.monotonic() - start, 2)
        self.assertFalse(self.m.worker.is_alive())
        self.assertEqual(self.read("alert"), "")


if __name__ == "__main__":
    unittest.main()
