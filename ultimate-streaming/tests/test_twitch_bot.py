"""The Twitch Bot (modules/twitch_bot.py) and its panel routes (core/webapi/bot_api.py), against a
fake Twitch module and fake deck actions, with an injected clock so nothing waits for real."""
import copy
import inspect
import os
import threading
import time
import types
import unittest
from unittest import mock

from tests.helpers import ROOT, tmpdir

import yaml  # noqa: E402

from core import Module, action, configio  # noqa: E402
from core.bot import DeckBot  # noqa: E402
from core.webapi import ApiError, Router, bot_api  # noqa: E402
import modules.twitch_bot as tb  # noqa: E402


def wait_for(pred, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.005)
    return False


class Clock:
    def __init__(self):
        self.t = 10_000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class FakeTwitch:
    """The parts of modules/twitch.py the bot uses."""
    config_key = "fake_twitch"

    def __init__(self):
        self.channel = "streamer"
        self.user = {"id": "1001", "login": "streamer", "display_name": "Streamer"}
        self.sent, self.announced, self.calls = [], [], []
        self.live = True
        self.status_calls = 0
        self.chat_error = None
        self.chat_errors = []          # raised once each, before chat_error
        self.chat_attempts = 0
        self.send_hook = self.status_hook = None
        self.call_threads = []
        self.missing = []
        self.manageable, self.others = {}, {}
        self.n = 0
        self.lock = threading.Lock()

    def on_config(self, config):
        pass

    def linked(self):
        return True

    def me(self):
        return self.user

    def require(self, scope):
        pass

    def missing_scopes(self, network=True):
        return list(self.missing)

    def stream_status(self):
        self.status_calls += 1
        if self.status_hook:
            self.status_hook(None)
        return {"live": self.live, "viewers": 3, "started_at": None, "title": "Live title", "game_name": "Chess"}

    def channel_info(self):
        return {"title": "Offline title", "game_name": "Chess"}

    def send_chat(self, text, reply_to=None):
        self.chat_attempts += 1
        if self.send_hook:
            self.send_hook(text)
        if self.chat_errors:
            raise self.chat_errors.pop(0)
        if self.chat_error:
            raise self.chat_error
        with self.lock:
            self.n += 1
            self.sent.append((text, reply_to))
            return f"sent-{self.n}"

    def announce(self, text, color="primary"):
        self.announced.append((text, color))

    def api(self, method, path, params=None, json=None):
        params = dict(params or {})
        with self.lock:
            self.calls.append((method, path, params, copy.deepcopy(json)))
            self.call_threads.append(threading.current_thread().name)
        if method == "PATCH" and path == tb.REWARDS_PATH and set(json or {}) == {"is_paused"}:
            return {"data": [{"id": params["id"]}]}
        if path == tb.REWARDS_PATH:
            if method == "GET":
                rewards = dict(self.manageable)
                if params.get("only_manageable_rewards") != "true":
                    rewards.update(self.others)
                return {"data": [{"id": i, **r} for i, r in rewards.items()]}
            if method == "POST":
                with self.lock:
                    self.n += 1
                    rid = f"rw-{self.n}"
                self.manageable[rid] = {"title": json["title"], "cost": json["cost"], "is_enabled": json["is_enabled"]}
                return {"data": [{"id": rid}]}
            if method == "PATCH":
                self.manageable[params["id"]].update(title=json["title"], cost=json["cost"])
                return {"data": [{"id": params["id"]}]}
            if method == "DELETE":
                self.manageable.pop(params["id"], None)
                return {}
        return {}

    def patches(self):
        return [(p, j) for m, path, p, j in list(self.calls) if path == tb.REWARDS_PATH + "/redemptions"]

    def pauses(self):
        return [(p["id"], j["is_paused"]) for m, path, p, j in list(self.calls)
                if m == "PATCH" and path == tb.REWARDS_PATH and set(j or {}) == {"is_paused"}]

    def texts(self):
        return [t for t, _ in self.sent]


class FakeChatStats:
    """core/chatstats.py's ChatStats API, in memory. Records which thread called what, so tests can
    check that the chat thread only ever uses the in-memory record_* methods."""
    QUERIES = ("user", "top", "totals", "reset", "flush")

    def __init__(self, url, *, password=None, exclude=(), flush_every=10.0, clock=time.time):
        self.url, self.password, self.exclude = url, password, set(exclude)
        self.started = self.closed = False
        self.records, self.queries = [], []      # (name, args, thread name)
        self.users = {}                            # user_id -> user() dict
        self.error, self.resets = None, []

    def start(self):
        self.started = True

    def close(self, timeout=5.0):
        self.closed = True

    def _rec(self, name, *args):
        self.records.append((name, args, threading.current_thread().name))

    def record_message(self, user_id, login, display, ts=None):
        self._rec("message", user_id, login, display)

    def record_command(self, user_id, login, display):
        self._rec("command", user_id, login, display)

    def record_redemption(self, user_id, login, display, cost=0):
        self._rec("redemption", user_id, login, display, cost)

    def new_stream(self):
        self._rec("new_stream")

    def set_exclude(self, logins):
        self.exclude = set(logins)

    def _q(self, name):
        self.queries.append((name, threading.current_thread().name))

    def flush(self):
        self._q("flush")
        return True

    def sqlite_path(self):
        import re
        m = re.fullmatch(r"sqlite(?:\+\w+)?:///([^?]+)(?:\?.*)?", self.url)
        return m.group(1) if m and m.group(1) != ":memory:" else None

    def _visible(self):
        return [u for u in self.users.values() if u["login"] not in self.exclude]

    def user(self, user_id=None, login=None):
        self._q("user")
        return next((dict(u) for u in self._visible()
                     if (user_id and u["user_id"] == user_id) or (login and u["login"] == login)), None)

    def top(self, n=10, period="all"):
        self._q("top")
        key = "stream_messages" if period == "stream" else "messages"
        rows = sorted((u for u in self._visible() if u[key]), key=lambda u: (-u[key], u["first_seen"]))[:n]
        out, rank, prev = [], 0, None
        for i, u in enumerate(rows, 1):           # competition ranks: equal counts share a rank
            if u[key] != prev:
                rank, prev = i, u[key]
            out.append({"rank": rank, "user_id": u["user_id"], "login": u["login"], "display": u["display"],
                        "messages": u[key]})
        return out

    def totals(self):
        self._q("totals")
        rows = self._visible()
        return {"chatters": len(rows), "messages": sum(u["messages"] for u in rows),
                "stream_messages": sum(u["stream_messages"] for u in rows)}

    def reset(self, scope="all"):
        self._q("reset")
        self.resets.append(scope)

    def describe(self):
        import re
        return {"url": re.sub(r"(//[^:/@]+:)[^@]+@", r"\1***@", self.url),
                "backend": self.url.split(":", 1)[0].split("+", 1)[0], "error": self.error}


class Actions(Module):
    """Stand-in deck actions that record what ran; hooks make one fail or hang."""

    def __init__(self, bot):
        super().__init__(bot)
        self.ran, self.hooks = [], {}

    def _do(self, kind, arg):
        self.ran.append((kind, copy.deepcopy(arg)))
        hook = self.hooks.get(kind)
        if hook:
            hook(arg)

    @action("obs_color")
    def obs_color(self, arg):
        self._do("obs_color", arg)

    @action("ha_light")
    def ha_light(self, arg):
        self._do("ha_light", arg)

    @action("obs_scene")
    def obs_scene(self, arg):
        self._do("obs_scene", arg)

    @action("notify")
    def notify(self, arg):
        self._do("notify", arg)


def timer(tid="t1", **kw):
    return {"id": tid, "messages": ["Follow the channel!"], "interval_minutes": 2, "min_chat_lines": 0, **kw}


def command(cid="discord", **kw):
    return {"id": cid, "trigger": "!discord", "response": "{user}, join us at https://discord.example",
            "cooldown_seconds": 0, "user_cooldown_seconds": 0, **kw}


def reward(rid="bg", **kw):
    return {"id": rid, "twitch_id": "tw-bg", "managed": True, "title": "Change the background", "cost": 500,
            "input_required": True,
            "actions": [{"obs_color": {"source": "Background", "color": "{input}"}},
                        {"ha_light": {"entity_id": "light.office", "color": "{input}"}}],
            "reply": "{user} changed the background to {color}!", **kw}


def chat(text, user="viewer", uid="2001", mid="m-1", badges=()):
    return {"id": mid, "user": user, "user_id": uid, "text": text, "color": "", "badges": list(badges),
            "mod": "moderator" in badges or "broadcaster" in badges, "sub": "subscriber" in badges}


class Gate:
    """A hook that blocks an action until released."""

    def __init__(self):
        self.started, self.event = threading.Event(), threading.Event()

    def __call__(self, arg):
        self.started.set()
        self.event.wait(10)

    def release(self):
        self.event.set()


class BotCase(unittest.TestCase):
    section = {}
    stats_class = FakeChatStats

    def setUp(self):
        self.make(self.section)

    def make(self, section, extra=None):
        if getattr(self, "m", None):
            self.stop()
        section = copy.deepcopy(section)
        self.path = os.path.join(tmpdir(), "config.yaml")
        with open(self.path, "w") as f:
            yaml.safe_dump({"brightness": 50, "pages": {"main": []}, "twitch_bot": section, **(extra or {})}, f,
                           sort_keys=False)
        self.bot = DeckBot(root=ROOT, config_path=self.path)
        self.bot.cfg = {"twitch_bot": section}
        self.tw = FakeTwitch()
        self.bot.modules["Twitch"] = self.tw
        self.acts = Actions(self.bot)
        self.bot.add_module(self.acts)
        self.clock = Clock()
        self.m = tb.TwitchBot(self.bot)
        self.m.now = self.clock
        self.m.wait_step = 0.002
        self.m.retry_step = 0.001
        self.m.stats_factory = self.stats_class
        self.bot.add_module(self.m)
        if self.m.stats_thread:
            self.m.stats_thread.join(5)
        self.stopped = False
        self.addCleanup(self.stop)

    def stop(self):
        if not self.stopped:
            self.stopped = True
            for hook in list(self.acts.hooks.values()) + [self.tw.send_hook, self.tw.status_hook]:
                getattr(hook, "release", lambda: None)()
            self.bot.remove_module(self.m)
            if self.m.unload_thread:
                self.m.unload_thread.join(5)

    def settle(self):
        """Wait until the reward worker and the chat sender have nothing left to do."""
        self.assertTrue(wait_for(self.m.idle), "bot never went idle")

    def revert_cycles(self, n=2):
        """Wait until the revert scheduler has looked at the (already advanced) clock n more times."""
        start = self.m.revert_ticks
        self.assertTrue(wait_for(lambda: self.m.revert_ticks >= start + n))

    def file(self):
        with open(self.path) as f:
            return yaml.safe_load(f)


# ----- render

class Render(unittest.TestCase):
    def test_single_pass_keeps_substituted_braces_literal(self):
        self.assertEqual(tb.render("Hi {user}, now: {title}", {"user": "{title}", "title": "Chess night"}),
                         "Hi {title}, now: Chess night")
        self.assertEqual(tb.render("{nope} {input} {user}", {"user": "x"}), "{nope} {input} x")
        self.assertEqual(tb.render("{user} {count}", {"count": 3}), "{user} 3")   # no value: literal

    def test_cap_at_500(self):
        out = tb.render("x" * 450 + " {user}", {"user": "y" * 300})
        self.assertEqual(len(out), 500)
        self.assertEqual(out.count("y"), 49)     # value capped at 100, then the whole message at 500
        self.assertEqual(len(tb.clean_value("y" * 300)), 100)

    def test_values_lose_control_chars_and_command_prefixes(self):
        self.assertEqual(tb.render("Hi {user}", {"user": "\x1b[31m!/.\\evil\r\nname\x07"}), "Hi evilname")
        self.assertEqual(tb.render("{user}", {"user": " /ban streamer"}), "ban streamer")
        self.assertEqual(tb.render("{user}", {"user": "\\.!me"}), "me")
        self.assertEqual(tb.render("{user}!x", {"user": "!"}), "x")    # never starts with ! or /


# ----- validation

class Validation(unittest.TestCase):
    def errors(self, **section):
        return tb.validate(section)[1]

    def assertRejected(self, kind, item, needle, **top):
        out, errors = tb.validate({kind: [item], **top})
        self.assertEqual(out[kind], [], errors)
        self.assertTrue(any(needle in e for e in errors), errors)

    def test_valid_config_is_normalised(self):
        out, errors = tb.validate({"enabled": True, "timers": [timer()], "commands": [command(aliases=["!DC"])],
                                   "rewards": [reward(background_color="purple")]})
        self.assertEqual(errors, [])
        self.assertEqual(out["commands"][0]["aliases"], ["!dc"])
        self.assertEqual(out["rewards"][0]["background_color"], "#800080")
        self.assertEqual(out["timers"][0]["min_chat_lines"], 0)
        self.assertEqual(out["timers"][0]["color"], "primary")
        self.assertEqual(tb.validate(None), tb.validate({}))

    def test_chat_text_rules(self):
        self.assertRejected("commands", command(response="!ban someone"), "can't start with !")
        self.assertRejected("commands", command(response="  /me hi"), "can't start with !")
        self.assertRejected("commands", command(response="you said {input}"), "{input}")
        self.assertRejected("timers", timer(messages=["hi {input}"]), "{input}")
        self.assertRejected("rewards", reward(reply="{user} wrote {input}"), "{input}")
        self.assertRejected("timers", timer(messages=["hi {user}"]), "{user}")
        self.assertRejected("commands", command(response="{color}"), "{color}")
        self.assertRejected("commands", command(response="two\nlines"), "one line")
        self.assertRejected("commands", command(response="x" * 501), "longer than 500")

    def test_bare_string_actions_always_rejected(self):
        self.assertTrue(tb.check_reward_actions(["echo hi"], False))
        self.assertTrue(tb.check_reward_actions(["echo hi"], True))
        self.assertTrue(tb.check_reward_actions([[{"notify": "x"}, "rm -rf ~"]], True))
        self.assertRejected("rewards", reward(actions=["echo hi"], allow_unsafe_actions=True), "shell command")

    def test_unsafe_actions_need_allow_unsafe(self):
        for kind in ("run", "keys", "http"):
            for actions in ([{kind: "x"}], [[{kind: "x"}]], [{"notify": "a"}, [[{kind: "x"}]]]):
                self.assertTrue(any("allow_unsafe_actions" in e for e in tb.check_reward_actions(actions, False)),
                                (kind, actions))
                self.assertEqual(tb.check_reward_actions(actions, True), [])
        self.assertRejected("rewards", reward(actions=[[{"run": "x"}]]), "allow_unsafe_actions")
        self.assertRejected("rewards", reward(revert=[{"http": {"url": "x"}}]), "allow_unsafe_actions")
        ok = tb.validate({"rewards": [reward(actions=[{"run": "x"}], input_required=False,
                                             allow_unsafe_actions=True)]})
        self.assertEqual(ok[1], [])

    def test_delay_and_count_caps(self):
        self.assertEqual(tb.check_reward_actions([{"delay": 24}], False), [])
        self.assertTrue(tb.check_reward_actions([{"delay": 31}], False))
        self.assertTrue(tb.check_reward_actions([[{"delay": "45"}]], True))
        self.assertTrue(tb.check_reward_actions([{"delay": True}], False))
        self.assertEqual(tb.check_reward_actions([{"notify": "x"}] * 20, False), [])
        self.assertTrue(any("at most 20" in e for e in tb.check_reward_actions([{"notify": "x"}] * 21, False)))
        self.assertTrue(tb.check_reward_actions([[{"notify": "x"}] * 21], False))

    def test_input_only_as_a_color(self):
        self.assertEqual(tb.check_reward_actions([{"obs_color": {"source": "Bg", "color": "{input}"}}], False), [])
        self.assertTrue(tb.check_reward_actions([{"obs_color": {"source": "{input}", "color": "red"}}], False))
        self.assertTrue(tb.check_reward_actions([{"obs_color": {"source": "Bg", "color": "#{input}"}}], False))
        self.assertTrue(tb.check_reward_actions([{"obs_scene": "{input}"}], False))
        self.assertTrue(tb.check_reward_actions([{"run": "echo {input}"}], True))
        self.assertRejected("rewards", reward(input_required=False), "input_required")

    def test_bad_ids_and_duplicates(self):
        for bad in ("Bad Id", "", "x" * 33, 5, None, "a/b"):
            self.assertRejected("timers", timer(tid=bad), "id must be")
        out, errors = tb.validate({"timers": [timer("a"), timer("a")]})
        self.assertEqual([t["id"] for t in out["timers"]], ["a"])
        self.assertTrue(any("used twice" in e for e in errors))
        out, errors = tb.validate({"commands": [command("a"), command("b", trigger="!Discord")]})
        self.assertEqual([c["id"] for c in out["commands"]], ["a"])
        self.assertTrue(any("already used" in e for e in errors), errors)
        out, errors = tb.validate({"commands": [command("a"), command("b", trigger="!other", aliases=["!DISCORD"])]})
        self.assertEqual([c["id"] for c in out["commands"]], ["a"])
        for bad in ("discord", "!dis cord", "!" + "x" * 26, "!dísc"):
            self.assertRejected("commands", command(trigger=bad), "trigger must be")
        out, errors = tb.validate({"rewards": [reward("a"), reward("b", twitch_id="other")]})
        self.assertTrue(any("unique titles" in e for e in errors), errors)

    def test_numeric_ranges_and_types(self):
        self.assertRejected("timers", timer(interval_minutes=1), "interval_minutes")
        self.assertRejected("timers", timer(interval_minutes=241), "interval_minutes")
        self.assertRejected("timers", timer(messages=[]), "messages")
        self.assertRejected("timers", timer(messages=["a"] * 21), "messages")
        self.assertRejected("timers", timer(announce="yes"), "announce")
        self.assertRejected("commands", command(permission="admin"), "permission")
        self.assertRejected("commands", command(aliases=["!a", "!b", "!c", "!d", "!e", "!f"]), "aliases")
        self.assertRejected("rewards", reward(cost=0), "cost")
        self.assertRejected("rewards", reward(title="x" * 46), "title")
        self.assertRejected("rewards", reward(revert_after_seconds=3601), "revert_after_seconds")
        self.assertRejected("rewards", reward(background_color="nope"), "background_color")
        self.assertEqual(tb.validate([])[1], ["twitch_bot must be a mapping"])


# ----- timers

class Timers(BotCase):
    section = {"enabled": True, "timers": [timer(messages=["one", "two"], min_chat_lines=2)]}

    def test_posts_only_when_live_elapsed_and_active(self):
        m, tw = self.m, self.tw
        tw.live = False
        self.clock.advance(121)
        self.bot.dispatch("chat_message", chat("hello", mid="a"))
        self.bot.dispatch("chat_message", chat("again", mid="b"))
        self.assertIsNone(m.run_timers())                 # offline
        tw.live = True
        self.clock.advance(61)                            # past the stream-status cache
        self.make(self.section)                           # fresh: interval restarts, no chat lines
        m, tw = self.m, self.tw
        self.assertIsNone(m.run_timers())                 # interval not elapsed
        self.clock.advance(121)
        self.assertIsNone(m.run_timers())                 # only 0 of 2 chat lines
        self.bot.dispatch("chat_message", chat("hello", mid="a"))
        self.assertIsNone(m.run_timers())                 # 1 of 2
        self.bot.dispatch("chat_message", chat("again", mid="b"))
        self.assertEqual(m.run_timers(), "t1")
        self.assertEqual(tw.texts(), ["one"])
        self.assertEqual(m.snapshot()["timers"][0]["lines"], 0)

    def test_live_status_polled_at_most_every_60s(self):
        self.clock.advance(121)
        for _ in range(5):
            self.m.run_timers()
        self.assertEqual(self.tw.status_calls, 1)
        self.bot.dispatch("stream_offline")
        self.bot.dispatch("chat_message", chat("a", mid="a"))
        self.bot.dispatch("chat_message", chat("b", mid="b"))
        self.clock.advance(61)
        self.assertIsNone(self.m.run_timers())           # EventSub says offline
        self.bot.dispatch("stream_online")
        self.assertEqual(self.m.run_timers(), "t1")

    def test_rotation_and_global_gap(self):
        self.make({"enabled": True, "only_when_live": False,
                   "timers": [timer("a", messages=["one", "two"]), timer("b", messages=["bee"])]})
        self.clock.advance(121)
        self.assertEqual(self.m.run_timers(), "a")
        self.assertIsNone(self.m.run_timers())            # 60 s between any two timer posts
        self.clock.advance(59)
        self.assertIsNone(self.m.run_timers())
        self.clock.advance(1)
        self.assertEqual(self.m.run_timers(), "b")
        self.clock.advance(60)
        self.assertEqual(self.m.run_timers(), "a")
        self.clock.advance(120)
        self.assertEqual(self.m.run_timers(), "b")       # both due: the one that waited longest goes
        self.clock.advance(60)
        self.assertEqual(self.m.run_timers(), "a")
        self.assertEqual(self.tw.texts(), ["one", "bee", "two", "bee", "one"])

    def test_paused_and_disabled(self):
        self.make({"enabled": True, "only_when_live": False, "timers": [timer()]})
        self.clock.advance(121)
        self.m.timers_switch("pause")
        self.assertTrue(self.m.snapshot()["paused"])
        self.assertFalse(self.m.timers_state("running", {}))
        self.assertIsNone(self.m.run_timers())
        self.m.timers_switch("toggle")
        self.assertTrue(self.m.timers_state("running", {}))
        self.m.cfg = {**self.m.cfg, "enabled": False}
        self.assertIsNone(self.m.run_timers())
        self.m.cfg = {**self.m.cfg, "enabled": True}
        self.assertEqual(self.m.run_timers(), "t1")

    def test_announce_and_send_now(self):
        self.make({"enabled": False, "timers": [timer(announce=True, color="purple")]})
        self.m.send_timer("t1")                           # ignores enabled, live, interval, lines
        self.assertEqual(self.tw.announced, [("Follow the channel!", "purple")])
        with self.assertRaises(ApiError) as e:
            self.m.send_timer("t1")                       # same text within 30 s
        self.assertEqual(e.exception.status, 429)
        with self.assertRaises(ApiError) as e:
            self.m.send_timer("nope")
        self.assertEqual(e.exception.status, 404)
        snap = self.m.snapshot()
        self.assertEqual(set(snap), {"enabled", "live", "timers", "paused", "recent", "blocked"})
        self.assertIsNone(snap["blocked"])
        self.assertEqual(set(snap["timers"][0]), {"id", "next_in", "lines", "last_sent"})
        self.assertEqual(snap["timers"][0]["next_in"], 120)

    def test_task_posts_in_the_background(self):
        self.make({"enabled": True, "only_when_live": False, "timers": [timer()]})
        self.clock.advance(121)
        self.m.timers_task()
        self.assertTrue(wait_for(lambda: self.tw.texts() == ["Follow the channel!"]))

    def test_rate_limit_is_a_skip(self):
        self.make({"enabled": True, "only_when_live": False, "timers": [timer()]})
        self.tw.chat_error = ApiError("Slow down", 429)
        self.clock.advance(121)
        self.assertIsNone(self.m.run_timers())
        self.assertFalse(self.m.recent[-1]["ok"])
        self.tw.chat_error = None
        self.clock.advance(60)
        self.assertEqual(self.m.run_timers(), "t1")


# ----- commands

class Commands(BotCase):
    section = {"enabled": True, "commands": [command(aliases=["!dc"])]}

    def say(self, text, **kw):
        before = len(self.tw.sent)
        self.bot.dispatch("chat_message", chat(text, **kw))
        self.settle()
        return self.tw.sent[before:]

    def test_trigger_and_alias_case_insensitive(self):
        self.assertEqual(self.say("!DISCORD please", user="Ann", mid="a1"),
                         [("Ann, join us at https://discord.example", "a1")])
        self.assertEqual(len(self.say("!Dc", user="Bob", mid="a2")), 1)
        self.assertEqual(self.say("hey !discord", user="Cy", mid="a3"), [])
        self.assertEqual(self.say("!discordx", user="Cy", mid="a4"), [])

    def test_disabled_bot_ignores_commands(self):
        self.make({"enabled": False, "commands": [command()]})
        self.assertEqual(self.say("!discord"), [])

    def test_permission_ladder(self):
        self.make({"enabled": True, "commands": [command("mods", permission="moderator", trigger="!m"),
                                                 command("subs", permission="subscriber", trigger="!s"),
                                                 command("boss", permission="broadcaster", trigger="!b")]})
        cases = [("!m", (), False), ("!m", ("vip",), False), ("!m", ("moderator",), True),
                 ("!m", ("broadcaster",), True), ("!s", (), False), ("!s", ("subscriber",), True),
                 ("!s", ("founder",), True), ("!s", ("vip",), True), ("!b", ("moderator",), False),
                 ("!b", ("broadcaster",), True)]
        for i, (text, badges, sent) in enumerate(cases):
            out = self.say(text, user=f"user{i}", uid=str(3000 + i), mid=f"p{i}", badges=badges)
            self.assertEqual(bool(out), sent, (text, badges))

    def test_global_cooldown(self):
        self.make({"enabled": True, "commands": [command(cooldown_seconds=30)]})
        self.assertTrue(self.say("!discord", user="a", uid="1", mid="c1"))
        self.clock.advance(29)
        self.assertFalse(self.say("!discord", user="b", uid="2", mid="c2"))
        self.clock.advance(1)
        self.assertTrue(self.say("!discord", user="b", uid="2", mid="c3"))

    def test_user_cooldown(self):
        self.make({"enabled": True, "commands": [command(user_cooldown_seconds=60, response="{user}: {count}")]})
        self.assertEqual(self.say("!discord", user="a", uid="1", mid="u1")[0][0], "a: 1")
        self.assertFalse(self.say("!discord", user="a", uid="1", mid="u2"))
        self.assertEqual(self.say("!discord", user="b", uid="2", mid="u3")[0][0], "b: 2")
        self.clock.advance(60)
        self.assertTrue(self.say("!discord", user="a", uid="1", mid="u4"))

    def test_own_messages_ignored_but_broadcaster_commands_work(self):
        self.assertTrue(self.say("!discord", user="a", mid="o1"))
        own = self.tw.sent and "sent-1"
        self.clock.advance(31)
        self.assertFalse(self.say("!discord", user="Streamer", uid="1001", mid=own, badges=("broadcaster",)))
        self.assertTrue(self.say("!discord", user="Streamer", uid="1001", mid="typed-1", badges=("broadcaster",)))

    def test_own_messages_dont_count_as_chat_lines(self):
        self.make({"enabled": True, "timers": [timer(min_chat_lines=1)], "commands": [command()]})
        self.say("!discord", user="a", mid="l1")                                # 1 line from a viewer
        self.bot.dispatch("chat_message", chat(self.tw.sent[0][0], user="Streamer", uid="1001", mid="sent-1"))
        self.bot.dispatch("chat_message", chat(self.tw.sent[0][0], user="Streamer", uid="1001", mid="echo"))
        self.assertEqual(self.m.snapshot()["timers"][0]["lines"], 1)

    def test_reply_threading(self):
        self.assertEqual(self.say("!discord", user="a", mid="abc-123")[0][1], "abc-123")
        self.assertIsNone(self.say("!discord", user="b", mid="not an id!")[0][1])
        self.make({"enabled": True, "commands": [command(reply=False)]})
        self.assertIsNone(self.say("!discord", user="a", mid="abc-124")[0][1])

    def test_identical_text_suppressed_for_30s(self):
        self.make({"enabled": True, "commands": [command(response="Join us at https://discord.example")]})
        self.assertTrue(self.say("!discord", user="a", uid="1", mid="s1"))
        self.clock.advance(29)
        self.assertFalse(self.say("!discord", user="b", uid="2", mid="s2"))
        self.assertFalse(self.m.recent[-1]["ok"])
        self.clock.advance(1)
        self.assertTrue(self.say("!discord", user="c", uid="3", mid="s3"))

    def test_hostile_display_name(self):
        out = self.say("!discord", user="/ban\r\nStreamer{title}", mid="h1")
        self.assertEqual(out[0][0], "banStreamer{title}, join us at https://discord.example")

    def test_command_test_does_not_post(self):
        self.assertEqual(self.m.command_test("discord", "Tess"), {"text": "Tess, join us at https://discord.example"})
        self.assertEqual(self.tw.sent, [])


# ----- rewards

def redemption(rid="r1", reward_id="tw-bg", text="purple", status="unfulfilled", user="Viewer"):
    return {"id": rid, "reward_id": reward_id, "reward_title": "x", "user": user, "user_login": user.lower(),
            "user_id": "2001", "input": text, "status": status, "ts": "2026-10-31T00:00:00Z"}


class Rewards(BotCase):
    section = {"enabled": True, "rewards": [reward()]}

    def redeem(self, **kw):
        self.bot.dispatch("reward_redemption", redemption(**kw))

    def statuses(self):
        return [(p["id"], j["status"]) for p, j in self.tw.patches()]

    def wait_status(self, rid, n=1):
        self.assertTrue(wait_for(lambda: len([s for s in self.statuses() if s[0] == rid]) >= n), self.statuses())
        return [s for i, s in self.statuses() if i == rid]

    def test_color_input_runs_actions_and_fulfils(self):
        self.redeem(text=" Purple ")
        self.assertEqual(self.wait_status("r1"), ["FULFILLED"])
        self.assertEqual(self.acts.ran, [("obs_color", {"source": "Background", "color": "#800080"}),
                                         ("ha_light", {"entity_id": "light.office", "color": "#800080"})])
        self.assertEqual(self.tw.texts(), ["Viewer changed the background to purple!"])
        params = self.tw.patches()[0][0]
        self.assertEqual(params, {"broadcaster_id": "1001", "reward_id": "tw-bg", "id": "r1"})
        self.assertTrue(wait_for(lambda: self.m.recent and self.m.recent[-1]["kind"] == "reward"))
        self.assertTrue(self.m.recent[-1]["ok"])

    def test_hex_input_and_reply_rate_limit_still_fulfils(self):
        self.tw.chat_error = ApiError("Slow down", 429)
        self.redeem(text="#FF00aa")
        self.assertEqual(self.wait_status("r1"), ["FULFILLED"])
        self.assertEqual(self.acts.ran[0][1]["color"], "#ff00aa")

    def test_bad_color_refunds_without_running(self):
        for i, text in enumerate(("not a color", "", "{input}", "#12345g", "x" * 40)):
            self.redeem(rid=f"bad{i}", text=text)
            self.assertEqual(self.wait_status(f"bad{i}"), ["CANCELED"])
        self.assertEqual(self.acts.ran, [])
        self.assertEqual(self.tw.sent, [])

    def test_failing_action_refunds(self):
        self.make({"enabled": True, "rewards": [reward(actions=[{"obs_scene": "Live"}], input_required=False)]})
        self.acts.hooks["obs_scene"] = lambda arg: (_ for _ in ()).throw(RuntimeError("OBS isn't running"))
        self.redeem()
        self.assertEqual(self.wait_status("r1"), ["CANCELED"])
        self.assertEqual(self.tw.sent, [])
        self.assertTrue(wait_for(lambda: any("OBS isn't running" in (e["error"] or "") for e in self.m.recent)))

    def test_hung_action_refunds_after_deadline(self):
        gate = self.acts.hooks["obs_color"] = Gate()
        self.redeem()
        self.assertTrue(gate.started.wait(5))
        self.assertEqual(self.statuses(), [])             # the clock hasn't moved: within the deadline
        self.clock.advance(31)
        self.assertEqual(self.wait_status("r1"), ["CANCELED"])
        gate.release()
        self.assertTrue(wait_for(lambda: not self.m.abandoned))
        self.assertEqual([k for k, _ in self.acts.ran], ["obs_color"])   # abandoned: no further steps
        self.redeem(rid="r2")                             # the worker carries on
        self.assertEqual(self.wait_status("r2"), ["FULFILLED"])

    def test_unmanaged_reward_never_patched(self):
        self.make({"enabled": True, "rewards": [reward(managed=False)]})
        self.redeem()
        self.assertTrue(wait_for(lambda: len(self.acts.ran) == 2))
        self.redeem(rid="r2", text="nope")
        self.settle()
        self.assertEqual(self.tw.patches(), [])

    def test_unknown_reward_and_status_ignored(self):
        self.redeem(rid="a", reward_id="someone-else")
        self.redeem(rid="b", status="fulfilled")
        self.redeem(rid="c", status="canceled")
        self.redeem(rid="bad id!")
        self.make({"enabled": True, "rewards": [reward(enabled=False)]})
        self.redeem(rid="d")
        self.make({"enabled": False, "rewards": [reward()]})
        self.redeem(rid="e")
        self.assertTrue(self.m.idle())                    # the listener queued nothing
        self.assertEqual(self.acts.ran, [])
        self.assertEqual(self.tw.patches(), [])

    def test_skip_queue_runs_fulfilled_without_patch(self):
        self.make({"enabled": True, "rewards": [reward(skip_queue=True)]})
        self.redeem(status="fulfilled")
        self.assertTrue(wait_for(lambda: len(self.acts.ran) == 2))
        self.settle()
        self.assertEqual(self.tw.patches(), [])

    def test_unexpected_error_refunds(self):
        with mock.patch.object(self.m, "_prepare", side_effect=KeyError("boom")):
            self.redeem()
            self.assertEqual(self.wait_status("r1"), ["CANCELED"])

    def test_duplicate_redemption_runs_once(self):
        self.redeem()
        self.redeem()
        self.settle()
        self.assertEqual(self.statuses(), [("r1", "FULFILLED")])
        self.assertEqual(len(self.acts.ran), 2)

    def test_queue_overflow_refunds(self):
        gate = self.acts.hooks["obs_color"] = Gate()
        self.redeem(rid="first")
        self.assertTrue(gate.started.wait(5))
        for i in range(tb.MAX_PENDING):
            self.redeem(rid=f"q{i}")
        self.assertEqual(self.statuses(), [])
        self.redeem(rid="overflow")
        self.assertEqual(self.statuses(), [])             # not on the EventSub thread
        gate.release()
        self.assertEqual(self.wait_status("overflow"), ["CANCELED"])
        refund_threads = [t for (m, path, p, j), t in zip(self.tw.calls, self.tw.call_threads)
                          if p.get("id") == "overflow"]
        self.assertEqual(refund_threads, ["twitch-bot-rewards"])
        self.assertEqual(self.statuses()[:2], [("first", "FULFILLED"), ("overflow", "CANCELED")])
        self.settle()
        self.assertEqual(len([s for s in self.statuses() if s[1] == "FULFILLED"]), 1 + tb.MAX_PENDING)

    def test_revert_runs_once_after_delay(self):
        black = {"obs_color": {"source": "Background", "color": "#202020"}}
        self.make({"enabled": True, "rewards": [reward(revert_after_seconds=10, revert=[black])]})
        reverts = lambda: [a for a in self.acts.ran if a[1].get("color") == "#202020"]  # noqa: E731
        self.redeem()
        self.wait_status("r1")
        self.clock.advance(9)
        self.revert_cycles()
        self.assertEqual(reverts(), [])
        self.clock.advance(1)
        self.assertTrue(wait_for(lambda: reverts()))
        self.clock.advance(100)
        self.assertTrue(wait_for(lambda: not self.m.reverts))
        self.revert_cycles()
        self.assertEqual(len(reverts()), 1)
        self.assertTrue(wait_for(lambda: any(e["kind"] == "revert" for e in self.m.recent)))

    def test_newer_redemption_replaces_pending_revert(self):
        black = {"obs_color": {"source": "Background", "color": "#202020"}}
        self.make({"enabled": True, "rewards": [reward(revert_after_seconds=10, revert=[black])]})
        reverts = lambda: [a for a in self.acts.ran if a[1].get("color") == "#202020"]  # noqa: E731
        self.redeem(rid="r1")
        self.wait_status("r1")
        self.clock.advance(5)
        self.redeem(rid="r2", text="red")
        self.wait_status("r2")
        self.clock.advance(6)                             # r1's revert would be due now
        self.revert_cycles()
        self.assertEqual(reverts(), [])
        self.clock.advance(4)
        self.assertTrue(wait_for(lambda: reverts()))
        self.revert_cycles()
        self.assertEqual(len(reverts()), 1)

    def test_pending_reverts_run_on_unload(self):
        black = {"obs_color": {"source": "Background", "color": "#202020"}}
        self.make({"enabled": True, "rewards": [reward(revert_after_seconds=600, revert=[black])]})
        self.redeem()
        self.wait_status("r1")
        self.stop()
        self.assertEqual(len([a for a in self.acts.ran if a[1].get("color") == "#202020"]), 1)

    def test_reward_test_no_status_no_reply(self):
        self.assertEqual(self.m.test_reward("bg", "red"), {"ok": True})
        self.assertEqual(self.acts.ran[0][1]["color"], "#ff0000")
        self.assertEqual(self.m.test_reward("bg", "nope")["ok"], False)
        self.assertEqual((self.tw.patches(), self.tw.sent), ([], []))


# ----- hub events

class Events(BotCase):
    section = {"enabled": True}

    def test_coalesced_to_five_per_second(self):
        q = self.bot.hub.subscribe()
        self.addCleanup(self.bot.hub.unsubscribe, q)
        for i in range(10):
            self.m._event("command", f"c{i}", "\x1b[31mviewer\n" + "x" * 200)
        got = [q.get_nowait() for _ in range(q.qsize())]
        self.assertEqual([e["type"] for e in got], ["bot"] * 5)
        self.assertEqual(len(self.m.recent), 10)
        self.assertEqual(set(got[0]["data"]), {"ts", "kind", "title", "user", "ok", "error"})
        self.assertNotIn("\n", got[0]["data"]["user"])
        self.assertLessEqual(len(got[0]["data"]["user"]), 100)
        self.clock.advance(1)
        self.m._event("timer", "t")
        self.assertEqual(q.get_nowait()["data"]["kind"], "timer")


# ----- reward sync with Twitch

class Sync(BotCase):
    section = {"enabled": True, "rewards": [reward(twitch_id="", global_cooldown_seconds=60)]}

    def test_creates_then_updates_and_writes_twitch_id(self):
        out = self.m.sync_rewards()
        self.assertEqual(out, {"created": 1, "updated": 0, "errors": []})
        post = [c for c in self.tw.calls if c[0] == "POST"][0]
        self.assertEqual(post[2], {"broadcaster_id": "1001"})
        body = post[3]
        self.assertEqual(body["title"], "Change the background")
        self.assertTrue(body["is_user_input_required"])
        self.assertTrue(body["is_global_cooldown_enabled"])
        self.assertEqual(body["global_cooldown_seconds"], 60)
        self.assertFalse(body["is_max_per_stream_enabled"])
        self.assertNotIn("max_per_stream", body)
        self.assertEqual(body["background_color"], "#9146FF")
        self.assertIs(body["is_paused"], False)
        self.assertFalse(body["should_redemptions_skip_request_queue"])
        cfg = self.file()
        self.assertEqual(cfg["twitch_bot"]["rewards"][0]["twitch_id"], "rw-1")
        self.assertEqual(cfg["brightness"], 50)
        self.assertEqual(self.m.cfg["rewards"][0]["twitch_id"], "rw-1")
        out = self.m.sync_rewards()
        self.assertEqual(out, {"created": 0, "updated": 1, "errors": []})
        patch = [c for c in self.tw.calls if c[0] == "PATCH"][0]
        self.assertEqual(patch[2], {"broadcaster_id": "1001", "id": "rw-1"})
        self.assertEqual(len([c for c in self.tw.calls if c[0] == "POST"]), 1)

    def test_adopts_by_title_and_links_unmanaged(self):
        self.make({"rewards": [reward(twitch_id=""), reward("hydrate", twitch_id="", managed=False,
                                                            title="Hydrate", input_required=False, actions=[])]})
        self.tw.manageable["abc"] = {"title": "change the BACKGROUND", "cost": 1, "is_enabled": True}
        self.tw.others["xyz"] = {"title": "Hydrate", "cost": 100, "is_enabled": True}
        self.assertEqual(self.m.sync_rewards(), {"created": 0, "updated": 1, "errors": []})
        ids = {r["id"]: r["twitch_id"] for r in self.file()["twitch_bot"]["rewards"]}
        self.assertEqual(ids, {"bg": "abc", "hydrate": "xyz"})
        self.assertFalse([c for c in self.tw.calls if c[0] in ("POST", "PATCH") and c[2].get("id") == "xyz"])

    def test_conflict_reloads_and_merges(self):
        real_load, calls = configio.load, []

        def load(path):
            cfg = real_load(path)
            calls.append(path)
            if len(calls) == 1:     # someone else saves while we hold the old hash
                other = real_load(path)
                other["brightness"] = 77
                other["twitch_bot"]["timers"] = [timer("theirs")]
                with open(path, "w") as f:
                    yaml.safe_dump(other, f, sort_keys=False)
            return cfg
        with mock.patch.object(configio, "load", side_effect=load):
            self.assertEqual(self.m.sync_rewards()["created"], 1)
        self.assertEqual(len(calls), 2)
        cfg = self.file()
        self.assertEqual(cfg["brightness"], 77)
        self.assertEqual(cfg["twitch_bot"]["timers"][0]["id"], "theirs")
        self.assertEqual(cfg["twitch_bot"]["rewards"][0]["twitch_id"], "rw-1")

    def test_twitch_errors_are_reported_per_reward(self):
        def fail(*a, **kw):
            if a[0] == "POST":
                raise ApiError("Twitch /channel_points/custom_rewards: 400 duplicate", 400)
            return FakeTwitch.api(self.tw, *a, **kw)
        self.tw.api = fail
        out = self.m.sync_rewards()
        self.assertEqual(out["created"], 0)
        self.assertTrue(out["errors"][0].startswith("bg: "))

    def test_delete_only_managed_and_manageable(self):
        self.make({"rewards": [reward(), reward("theirs", twitch_id="tw-x", managed=False, title="Theirs"),
                               reward("gone", twitch_id="tw-gone", title="Gone")]})
        self.tw.manageable["tw-bg"] = {"title": "Change the background", "cost": 500, "is_enabled": True}
        self.tw.others["tw-x"] = {"title": "Theirs", "cost": 1, "is_enabled": True}
        self.assertEqual(self.m.delete_reward("bg"), {"ok": True, "deleted_on_twitch": True, "errors": []})
        self.assertEqual(self.m.delete_reward("theirs"), {"ok": True, "deleted_on_twitch": False, "errors": []})
        self.assertEqual(self.m.delete_reward("gone"), {"ok": True, "deleted_on_twitch": False, "errors": []})
        deletes = [c[2]["id"] for c in self.tw.calls if c[0] == "DELETE"]
        self.assertEqual(deletes, ["tw-bg"])
        self.assertEqual(self.file()["twitch_bot"]["rewards"], [])
        with self.assertRaises(ApiError) as e:
            self.m.delete_reward("bg")
        self.assertEqual(e.exception.status, 404)

    def test_twitch_rewards_lists_everything(self):
        self.tw.manageable["a"] = {"title": "Mine", "cost": 5, "is_enabled": True}
        self.tw.others["b"] = {"title": "Theirs", "cost": 9, "is_enabled": False}
        self.assertEqual(sorted(self.m.twitch_rewards(), key=lambda r: r["id"]), [
            {"id": "a", "title": "Mine", "cost": 5, "manageable": True, "enabled": True},
            {"id": "b", "title": "Theirs", "cost": 9, "manageable": False, "enabled": False}])


# ----- deck actions and states

class DeckKeys(BotCase):
    section = {"enabled": False, "timers": [timer()]}

    def test_master_switch_persists(self):
        self.bot.run_action({"twitch_bot": "toggle"})
        self.assertTrue(self.m.bot_state("on", {}))
        self.assertTrue(self.file()["twitch_bot"]["enabled"])
        self.bot.run_action({"twitch_bot": "off"})
        self.assertFalse(self.file()["twitch_bot"]["enabled"])
        self.assertEqual(self.file()["twitch_bot"]["timers"][0]["id"], "t1")   # rest of the section untouched
        with self.assertRaises(ValueError):
            self.bot.run_action({"twitch_bot": "maybe"})

    def test_timer_key_and_source(self):
        self.bot.run_action({"bot_timer": "t1"})
        self.assertEqual(self.tw.texts(), ["Follow the channel!"])
        self.assertEqual(self.bot.sources["bot_timers"](), ["t1"])
        self.bot.run_action({"bot_timers": "pause"})
        self.assertTrue(self.m.paused)
        self.assertTrue(self.bot.check_state({"twitch_bot": "on"}) is False)


# ----- HTTP API

class Api(BotCase):
    section = {"enabled": True, "timers": [timer()], "commands": [command()], "rewards": [reward()]}

    def setUp(self):
        super().setUp()
        self.router = Router()
        bot_api.register(self.router, types.SimpleNamespace(bot=self.bot))

    def get(self, path, query=None):
        return self.router.gets[path](query or {})

    def post(self, path, body=None):
        return self.router.posts[path](body or {})

    def status_of(self, path, body):
        with self.assertRaises(ApiError) as e:
            self.post(path, body)
        return e.exception.status, str(e.exception)

    def test_registered_in_editor(self):
        import modules.editor as editor
        self.assertIs(editor.bot_api, bot_api)
        self.assertIn("bot_api", inspect.getsource(editor.Editor.on_load))
        for path in ("/api/bot", "/api/bot/rewards"):
            self.assertIn(path, self.router.gets)
        for path in ("/api/bot/config", "/api/bot/timer/send", "/api/bot/command/test", "/api/bot/rewards/sync",
                     "/api/bot/reward/test", "/api/bot/reward/delete", "/api/bot/enable"):
            self.assertIn(path, self.router.posts)

    def test_get_shape(self):
        self.tw.missing = ["channel:manage:redemptions", "clips:edit"]
        out = self.get("/api/bot")
        self.assertEqual(set(out), {"config", "status", "eventsub", "scopes_missing", "hash", "stats", "builtins"})
        self.assertEqual(out["hash"], configio.file_hash(self.path))
        expected = tb.validate(self.section)[0]
        for key in tb.CONFIG_ONLY:
            del expected[key]
        self.assertEqual(out["config"], expected)
        self.assertEqual(out["scopes_missing"], ["channel:manage:redemptions"])
        self.assertEqual(out["eventsub"]["connected"], False)
        self.bot.modules["EventSub"] = types.SimpleNamespace(
            config_key="x", on_config=lambda c: None,
            snapshot=lambda: {"connected": True, "subscriptions": ["stream.online"], "error": None})
        self.assertTrue(self.get("/api/bot")["eventsub"]["connected"])

    def test_config_saves_only_the_section(self):
        new = {"enabled": False, "timers": [timer("new", interval_minutes=30)], "commands": [], "rewards": [reward()]}
        out = self.post("/api/bot/config", {"config": new})
        self.assertEqual(out["config"]["timers"][0]["id"], "new")
        cfg = self.file()
        self.assertEqual(cfg["brightness"], 50)
        self.assertEqual(cfg["pages"], {"main": []})
        expected = tb.validate(new)[0]
        for key in tb.CONFIG_ONLY:
            del expected[key]
        self.assertEqual(cfg["twitch_bot"], expected)
        self.assertEqual(self.m.cfg["timers"][0]["interval_minutes"], 30)

    def test_config_keeps_synced_twitch_id(self):
        self.post("/api/bot/config", {"config": {"rewards": [reward(twitch_id="")]}})
        self.assertEqual(self.file()["twitch_bot"]["rewards"][0]["twitch_id"], "tw-bg")

    def test_config_field_errors(self):
        before = self.file()
        status, msg = self.status_of("/api/bot/config", {"config": {
            "timers": [timer(interval_minutes=1)], "commands": [command(response="!ban x")],
            "rewards": [reward(actions=["rm -rf ~"])]}})
        self.assertEqual(status, 400)
        self.assertIn("timers[0] (t1): interval_minutes must be a whole number from 2 to 240", msg)
        self.assertIn("commands[0] (discord): response can't start with !", msg)
        self.assertIn("rewards[0] (bg): actions[0]: plain text is a shell command", msg)
        self.assertEqual(self.file(), before)
        self.assertEqual(self.status_of("/api/bot/config", {"config": "x"})[0], 400)
        self.assertEqual(self.status_of("/api/bot/config", {})[0], 400)

    def test_enable(self):
        self.assertEqual(self.post("/api/bot/enable", {"on": False}), {"ok": True, "enabled": False})
        self.assertFalse(self.file()["twitch_bot"]["enabled"])
        self.assertEqual(self.status_of("/api/bot/enable", {"on": "yes"})[0], 400)

    def test_timer_send(self):
        self.assertEqual(self.post("/api/bot/timer/send", {"id": "t1"}), {"ok": True})
        self.assertEqual(self.tw.texts(), ["Follow the channel!"])
        self.assertEqual(self.status_of("/api/bot/timer/send", {"id": "nope"})[0], 404)
        self.assertEqual(self.status_of("/api/bot/timer/send", {"id": 5})[0], 400)

    def test_command_test(self):
        self.assertEqual(self.post("/api/bot/command/test", {"id": "discord", "user": "!Zed"}),
                         {"text": "Zed, join us at https://discord.example"})
        self.assertEqual(self.tw.sent, [])
        self.assertEqual(self.status_of("/api/bot/command/test", {"id": "nope"})[0], 404)
        self.assertEqual(self.status_of("/api/bot/command/test", {"id": "discord", "user": 5})[0], 400)

    def test_rewards_routes(self):
        self.tw.manageable["tw-bg"] = {"title": "Change the background", "cost": 500, "is_enabled": True}
        self.assertEqual(self.get("/api/bot/rewards")["rewards"][0]["id"], "tw-bg")
        self.assertEqual(self.post("/api/bot/rewards/sync"), {"created": 0, "updated": 1, "errors": []})
        self.assertEqual(self.post("/api/bot/reward/test", {"id": "bg", "input": "red"}), {"ok": True})
        self.assertEqual(self.post("/api/bot/reward/test", {"id": "bg", "input": "zzz"})["ok"], False)
        self.assertEqual(self.tw.patches(), [])
        self.assertEqual(self.status_of("/api/bot/reward/test", {"id": "nope"})[0], 404)
        self.assertEqual(self.post("/api/bot/reward/delete", {"id": "bg"}),
                         {"ok": True, "deleted_on_twitch": True, "errors": []})
        self.assertEqual(self.file()["twitch_bot"]["rewards"], [])
        self.assertEqual(self.status_of("/api/bot/reward/delete", {"id": "bg"})[0], 404)

    def test_module_missing(self):
        self.bot.remove_module(self.m)
        self.stopped = True
        with self.assertRaises(ApiError) as e:
            self.get("/api/bot")
        self.assertEqual(e.exception.status, 503)


# ----- review fixes (one class per item)

BLACK = {"obs_color": {"source": "Background", "color": "#202020"}}
THROTTLE = ApiError("Slow down: chat is limited to one every 1s", 429)
THROTTLE.retry_after = 0.5
BUDGET = ApiError("Slow down: at most 20 chat messages every 30s", 429)


class ChatSender(BotCase):
    """1. The chat thread never blocks; one sender thread renders and posts."""
    section = {"enabled": True, "commands": [command(response="{user}: up {uptime}", cooldown_seconds=30)]}

    def test_chat_thread_never_blocks(self):
        status, send = Gate(), Gate()
        self.tw.status_hook, self.tw.send_hook = status, send
        start = time.time()
        self.bot.dispatch("chat_message", chat("!discord", user="a", mid="b1"))
        self.assertLess(time.time() - start, 0.5)
        self.assertTrue(status.started.wait(5))           # the lookup runs on the sender
        self.assertEqual(threading.current_thread().name, "MainThread")
        self.assertEqual(self.tw.sent, [])
        status.release()
        self.assertTrue(send.started.wait(5))
        send.release()
        self.settle()
        self.assertEqual(len(self.tw.sent), 1)

    def test_throttle_retried_budget_skipped(self):
        self.tw.chat_errors = [THROTTLE, THROTTLE]
        self.bot.dispatch("chat_message", chat("!discord", user="a", mid="t1"))
        self.settle()
        self.assertEqual((len(self.tw.sent), self.tw.chat_attempts), (1, 3))
        self.make(self.section)
        self.tw.chat_errors = [BUDGET]
        self.bot.dispatch("chat_message", chat("!discord", user="a", mid="t2"))
        self.settle()
        self.assertEqual((len(self.tw.sent), self.tw.chat_attempts), (0, 1))

    def test_cooldown_starts_only_after_send_or_definite_drop(self):
        self.tw.chat_errors = [BUDGET]
        self.bot.dispatch("chat_message", chat("!discord", user="a", mid="c1"))
        self.settle()
        self.bot.dispatch("chat_message", chat("!discord", user="b", mid="c2"))
        self.settle()
        self.assertEqual(len(self.tw.sent), 1)            # the skip didn't start the 30 s cooldown
        self.make(self.section)
        self.tw.chat_errors = [ApiError("Twitch didn't send it: msg_duplicate")]
        self.bot.dispatch("chat_message", chat("!discord", user="a", mid="d1"))
        self.settle()
        self.bot.dispatch("chat_message", chat("!discord", user="b", mid="d2"))
        self.settle()
        self.assertEqual((len(self.tw.sent), self.tw.chat_attempts), (0, 1))

    def test_one_reply_in_flight_per_cooldown(self):
        send = self.tw.send_hook = Gate()
        for i in range(5):
            self.bot.dispatch("chat_message", chat("!discord", user=f"u{i}", uid=str(i), mid=f"f{i}"))
        send.release()
        self.settle()
        self.assertEqual(len(self.tw.sent), 1)

    def test_queue_overflow_dropped(self):
        send = self.tw.send_hook = Gate()
        jobs = [tb._Job(lambda i=i: f"message {i}") for i in range(tb.MAX_CHAT_JOBS + 2)]
        self.m._submit(jobs[0])
        self.assertTrue(send.started.wait(5))             # in flight; the next 50 queue
        for job in jobs[1:]:
            self.m._submit(job)
        self.assertTrue(jobs[-1].finished.is_set())
        self.assertIsInstance(jobs[-1].error, tb.Skipped)
        self.assertFalse(jobs[-2].finished.is_set())
        send.release()
        self.settle()
        self.assertTrue(all(j.finished.is_set() for j in jobs))


class BotCap(BotCase):
    """2. The bot's own posts get at most 10 per 30 s."""
    section = {"enabled": True, "commands": [command(response="{user} hi")]}

    def test_ten_per_thirty_seconds(self):
        for i in range(12):
            self.bot.dispatch("chat_message", chat("!discord", user=f"u{i}", uid=str(i), mid=f"x{i}"))
        self.settle()
        self.assertEqual(len(self.tw.sent), 10)
        self.assertIn("10 messages", self.m.recent[-1]["error"])
        self.clock.advance(30)
        self.bot.dispatch("chat_message", chat("!discord", user="late", uid="99", mid="y"))
        self.settle()
        self.assertEqual(len(self.tw.sent), 11)


class TimerFairness(BotCase):
    """3. A failing timer waits its interval; the longest-waiting due timer goes first."""
    section = {"enabled": True, "only_when_live": False, "timers": [timer("a", messages=["aa"]),
                                                                    timer("b", messages=["bb"])]}

    def test_failure_moves_last(self):
        self.tw.chat_error = ApiError("Twitch /chat/messages: 400 nope", 400)
        self.clock.advance(121)
        self.assertIsNone(self.m.run_timers())            # a failed
        self.tw.chat_error = None
        self.clock.advance(60)
        self.assertEqual(self.m.run_timers(), "b")        # not a again
        self.assertEqual(self.tw.texts(), ["bb"])


class Deadlines(BotCase):
    """4. Delays stay under the deadline; an abandoned run blocks new ones and still reverts."""
    nested = [[{"obs_color": {"source": "Background", "color": "{input}"}},
               {"ha_light": {"entity_id": "light.office", "color": "{input}"}}]]
    section = {"enabled": True, "rewards": [reward(actions=nested, revert_after_seconds=10, revert=[BLACK])]}

    def test_total_delay(self):
        self.assertEqual(tb.check_reward_actions([{"delay": 12}, [{"delay": 12.5}]], False), [])
        errors = tb.check_reward_actions([{"delay": 20}, [{"notify": "x"}, [{"delay": 5}]]], False)
        self.assertTrue(any("add up to 25s" in e for e in errors), errors)
        out, errors = tb.validate({"rewards": [reward(revert=[{"delay": 25}], revert_after_seconds=5)]})
        self.assertEqual(out["rewards"], [])

    def test_abandoned_child(self):
        gate = self.acts.hooks["obs_color"] = Gate()
        self.bot.dispatch("reward_redemption", redemption("r1"))
        self.assertTrue(gate.started.wait(5))
        self.clock.advance(31)
        self.assertTrue(wait_for(lambda: ("r1", "CANCELED") in Rewards.statuses(self)))
        self.bot.dispatch("reward_redemption", redemption("r2"))
        self.assertTrue(wait_for(lambda: ("r2", "CANCELED") in Rewards.statuses(self)))
        self.assertTrue(any("not responding" in (e["error"] or "") for e in self.m.recent))
        self.assertEqual(len(self.acts.ran), 1)           # r2 started nothing
        gate.release()
        self.assertTrue(wait_for(lambda: not self.m.abandoned and "bg" in self.m.reverts))
        self.assertEqual([k for k, _ in self.acts.ran], ["obs_color"])   # the nested ha_light never ran
        self.clock.advance(10)
        self.assertTrue(wait_for(lambda: len(self.acts.ran) == 2 and self.acts.ran[1][1]["color"] == "#202020"))


class RevertRace(BotCase):
    """5. A redemption takes the pending revert; a failed one puts it back."""
    section = {"enabled": True, "rewards": [reward(revert_after_seconds=10, revert=[BLACK])]}

    def reverts(self):
        return [a for a in self.acts.ran if a[1].get("color") == "#202020"]

    def test_running_redemption_holds_off_old_revert(self):
        self.bot.dispatch("reward_redemption", redemption("r1"))
        self.assertTrue(wait_for(lambda: "bg" in self.m.reverts))
        gate = self.acts.hooks["obs_color"] = Gate()
        self.clock.advance(5)
        self.bot.dispatch("reward_redemption", redemption("r2", text="red"))
        self.assertTrue(gate.started.wait(5))
        self.assertNotIn("bg", self.m.reverts)
        self.clock.advance(20)                            # r1's revert was due at +10
        self.revert_cycles()
        self.assertEqual(self.reverts(), [])
        del self.acts.hooks["obs_color"]
        gate.release()
        self.assertTrue(wait_for(lambda: "bg" in self.m.reverts))
        self.clock.advance(10)
        self.assertTrue(wait_for(lambda: self.reverts()))
        self.revert_cycles()
        self.assertEqual(len(self.reverts()), 1)

    def test_failed_redemption_restores_revert(self):
        self.bot.dispatch("reward_redemption", redemption("r1"))
        self.assertTrue(wait_for(lambda: "bg" in self.m.reverts))
        due = self.m.reverts["bg"][0]
        self.bot.dispatch("reward_redemption", redemption("r2", text="not a color"))
        self.assertTrue(wait_for(lambda: ("r2", "CANCELED") in Rewards.statuses(self)))
        self.assertEqual(self.m.reverts["bg"][0], due)
        self.clock.advance(10)
        self.assertTrue(wait_for(lambda: self.reverts()))


class Unload(BotCase):
    """6. Unloading never blocks: reverts and refunds finish on a background thread."""
    section = {"enabled": True, "rewards": [reward(revert_after_seconds=600, revert=[BLACK])]}

    def test_hung_revert_does_not_block(self):
        self.bot.dispatch("reward_redemption", redemption("r1"))
        self.assertTrue(wait_for(lambda: "bg" in self.m.reverts))
        gate = self.acts.hooks["obs_color"] = Gate()
        start = time.time()
        self.bot.remove_module(self.m)
        self.stopped = True
        self.assertLess(time.time() - start, 1.5)
        self.assertTrue(gate.started.wait(5))
        gate.release()
        self.m.unload_thread.join(5)
        self.assertEqual([a[1]["color"] for a in self.acts.ran if a[0] == "obs_color"], ["#800080", "#202020"])

    def test_queued_redemptions_refunded(self):
        gate = self.acts.hooks["obs_color"] = Gate()
        for rid in ("r1", "r2", "r3"):
            self.bot.dispatch("reward_redemption", redemption(rid))
        self.assertTrue(gate.started.wait(5))
        start = time.time()
        self.bot.remove_module(self.m)
        self.stopped = True
        self.assertLess(time.time() - start, 1.5)
        self.m.unload_thread.join(5)
        self.assertEqual(Rewards.statuses(self), [("r2", "CANCELED"), ("r3", "CANCELED")])
        gate.release()


class LiveFlag(BotCase):
    """7. A new EventSub session forgets live; live is re-confirmed every 5 minutes."""
    section = {"enabled": True, "timers": [timer()]}

    def test_new_session_falls_back_to_polling(self):
        self.bot.dispatch("stream_online", {})
        self.tw.live = False
        self.clock.advance(121)
        self.bot.dispatch("eventsub_session", {})
        self.assertIsNone(self.m.live_event)
        self.assertIsNone(self.m.run_timers())
        self.assertEqual(self.tw.status_calls, 1)

    def test_live_rechecked_every_five_minutes(self):
        self.bot.dispatch("stream_online", {})
        self.tw.live = False
        self.clock.advance(121)
        self.assertEqual(self.m.run_timers(), "t1")       # EventSub's word, within 5 min
        self.assertEqual(self.tw.status_calls, 0)
        self.clock.advance(300)
        self.assertIsNone(self.m.run_timers())
        self.assertEqual(self.tw.status_calls, 1)
        self.assertIsNone(self.m.live_event)


class SaveConflicts(Api):
    """8. GET returns the file hash; POST with a stale hash is a 409, never merged."""

    def test_hash(self):
        h = self.get("/api/bot")["hash"]
        new = {"enabled": True, "timers": [timer("x")]}
        out = self.post("/api/bot/config", {"config": new, "hash": h})
        self.assertEqual(out["config"]["timers"][0]["id"], "x")
        self.assertNotEqual(out["hash"], h)
        before = self.file()
        status, _ = self.status_of("/api/bot/config", {"config": {"enabled": False}, "hash": h})
        self.assertEqual(status, 409)
        self.assertEqual(self.file(), before)
        self.assertEqual(self.status_of("/api/bot/config", {"config": {}, "hash": 5})[0], 400)
        self.post("/api/bot/config", {"config": {"enabled": False}})    # no hash: saved as before
        self.assertFalse(self.file()["twitch_bot"]["enabled"])


class PauseRewards(BotCase):
    """9. Turning the bot off pauses its managed rewards on Twitch; on unpauses them."""
    section = {"enabled": True, "rewards": [reward(), reward("theirs", managed=False, twitch_id="tw-x", title="Theirs"),
                                            reward("new", twitch_id="", title="New")]}

    def test_flip_pauses_and_unpauses(self):
        self.m.set_enabled(False)
        self.m.pause_thread.join(5)
        self.assertEqual(self.tw.pauses(), [("tw-bg", True)])
        self.m.save_config({**self.section, "enabled": True})
        self.m.pause_thread.join(5)
        self.assertEqual(self.tw.pauses(), [("tw-bg", True), ("tw-bg", False)])
        thread = self.m.pause_thread
        self.m.on_config({**self.section, "enabled": True})          # no flip: nothing sent
        self.assertIs(self.m.pause_thread, thread)

    def test_errors_become_events(self):
        def fail(*a, **kw):
            raise ApiError("Twitch: 403 nope", 403)
        self.tw.api = fail
        self.m.on_config({**self.section, "enabled": False})
        self.m.pause_thread.join(5)
        self.assertTrue(any(e["kind"] == "error" and "pause" in e["error"] for e in self.m.recent))


class Hardening(BotCase):
    """10. Allowlist, defence in depth, broadcaster lines, format characters."""
    section = {"enabled": True, "timers": [timer(min_chat_lines=1)], "rewards": [reward()]}

    def test_say_not_allowed(self):
        self.assertTrue(tb.check_reward_actions([{"say": "hi"}], False))
        self.assertNotIn("say", tb.SAFE_ACTIONS)

    def test_actions_rechecked_before_running(self):
        self.m.cfg = {**self.m.cfg, "rewards": [{**self.m.cfg["rewards"][0], "actions": [{"run": "touch /tmp/x"}]}]}
        self.bot.dispatch("reward_redemption", redemption("r1"))
        self.assertTrue(wait_for(lambda: ("r1", "CANCELED") in Rewards.statuses(self)))
        self.assertEqual(self.acts.ran, [])
        self.m._run_revert("bg", (0, 0, [{"http": {"url": "https://example.com"}}], "Bg", False))
        self.assertIn("refused", self.m.recent[-1]["error"])
        self.assertEqual(self.acts.ran, [])

    def test_broadcaster_lines_dont_count(self):
        self.bot.dispatch("chat_message", chat("hi", user="Streamer", uid="9", mid="a", badges=("broadcaster",)))
        self.bot.dispatch("chat_message", chat("hi", user="Streamer", uid="1001", mid="b"))
        self.assertEqual(self.m.snapshot()["timers"][0]["lines"], 0)
        self.bot.dispatch("chat_message", chat("hi", mid="c"))
        self.assertEqual(self.m.snapshot()["timers"][0]["lines"], 1)

    def test_format_characters_stripped(self):
        family = "\U0001F468‍\U0001F469"
        self.assertEqual(tb.clean_value("a​b‮c⁦d﻿e" + family), "abcde" + family)
        self.assertEqual(tb._clean("x​y؜z"), "xyz")


class DeleteOrder(BotCase):
    """11. delete_reward saves the config first; a failed Twitch delete is reported."""
    section = {"rewards": [reward()]}

    def test_twitch_failure_reported(self):
        self.tw.manageable["tw-bg"] = {"title": "Change the background", "cost": 500, "is_enabled": True}

        def api(method, path, **kw):
            if method == "DELETE":
                self.assertEqual(self.file()["twitch_bot"]["rewards"], [])   # config saved first
                raise ApiError("Twitch /channel_points/custom_rewards: 502 down", 502)
            return FakeTwitch.api(self.tw, method, path, **kw)
        self.tw.api = api
        out = self.m.delete_reward("bg")
        self.assertEqual((out["ok"], out["deleted_on_twitch"]), (True, False))
        self.assertIn("502 down", out["errors"][0])
        self.assertEqual(self.m.cfg["rewards"], [])



class Hang:
    """A hook that blocks every call until released, counting the calls that entered."""

    def __init__(self):
        self.entered, self.event = threading.Semaphore(0), threading.Event()

    def __call__(self, arg):
        self.entered.release()
        self.event.wait(10)

    def release(self):
        self.event.set()


class AbandonCeiling(BotCase):
    """12. An abandoned action blocks new runs for at most 5 deadlines; 3 still running block until one returns."""
    section = Rewards.section
    redeem, statuses, wait_status = Rewards.redeem, Rewards.statuses, Rewards.wait_status
    ceiling = tb.ABANDON_CEILING * tb.ACTION_DEADLINE

    def hang(self, rid):
        """Redeem rid, let its first action hang past the deadline: refunded and abandoned."""
        self.redeem(rid=rid)
        self.assertTrue(self.acts.hooks["obs_color"].entered.acquire(timeout=5))
        self.clock.advance(tb.ACTION_DEADLINE + 1)
        self.assertEqual(self.wait_status(rid), ["CANCELED"])

    def test_released_after_ceiling(self):
        hang = self.acts.hooks["obs_color"] = Hang()
        since = self.m.wall()
        self.hang("r1")
        blocked = self.m.snapshot()["blocked"]
        self.assertEqual(blocked["action"], "obs_color")
        self.assertGreaterEqual(blocked["since"], since)
        del self.acts.hooks["obs_color"]
        self.clock.advance(self.ceiling - 1)
        out = self.m.test_reward("bg", "red")
        self.assertFalse(out["ok"])
        self.assertIn("not responding", out["error"])
        self.assertIn("obs_color", out["error"])
        self.assertEqual(len(self.acts.ran), 1)                      # nothing new started
        self.clock.advance(1)
        with self.assertLogs("deck.TwitchBot", "WARNING") as logs:
            self.assertEqual(self.m.test_reward("bg", "red"), {"ok": True})
            self.assertIsNone(self.m.snapshot()["blocked"])
            self.redeem(rid="r2")
            self.assertEqual(self.wait_status("r2"), ["FULFILLED"])
        released = [r for r in logs.output if "abandoned" in r]
        self.assertEqual(len(released), 1, logs.output)              # one warning, naming the action
        self.assertIn("obs_color", released[0])
        self.assertEqual(len(self.m.abandoned), 1)                   # still running, still counted
        hang.release()
        self.assertTrue(wait_for(lambda: not self.m.abandoned))

    def test_three_abandoned_keep_refusing(self):
        hang = self.acts.hooks["obs_color"] = Hang()
        for i in range(tb.MAX_ABANDONED):
            if i:
                self.clock.advance(self.ceiling)                     # the previous one no longer blocks
            self.hang(f"r{i}")
        self.assertEqual(len(self.m.abandoned), tb.MAX_ABANDONED)
        self.clock.advance(self.ceiling * 10)
        self.redeem(rid="late")
        self.assertEqual(self.wait_status("late"), ["CANCELED"])
        self.assertEqual(len(self.acts.ran), tb.MAX_ABANDONED)       # "late" started nothing
        self.assertTrue(any("not responding" in (e["error"] or "") for e in self.m.recent))
        self.assertEqual(self.m.snapshot()["blocked"]["action"], "obs_color")
        hang.release()
        self.assertTrue(wait_for(lambda: not self.m.abandoned))
        self.assertIsNone(self.m.snapshot()["blocked"])
        self.redeem(rid="after")
        self.assertEqual(self.wait_status("after"), ["FULFILLED"])


class FirstWaits:
    """A lock whose first taker waits for `go` before taking it: a thread started later gets in first."""

    def __init__(self):
        self.lock, self.count_lock = threading.Lock(), threading.Lock()
        self.first, self.go = threading.Event(), threading.Event()
        self.n = 0

    def __enter__(self):
        with self.count_lock:
            self.n += 1
            first = self.n == 1
        if first:
            self.first.set()
            self.go.wait(5)
        self.lock.acquire()

    def __exit__(self, *exc):
        self.lock.release()


class PauseOrder(BotCase):
    """13. Quick on / off toggles always end in the current state, whatever order their threads run in."""
    section = PauseRewards.section

    def test_older_thread_applying_last_applies_current_state(self):
        self.m.pause_lock = gate = FirstWaits()
        self.m.set_enabled(False)                    # thread A: started while off ...
        self.assertTrue(gate.first.wait(5))
        older = self.m.pause_thread
        self.m.set_enabled(True)                     # ... thread B: started while on, runs first
        self.m.pause_thread.join(5)
        self.assertEqual(self.tw.pauses(), [("tw-bg", False)])
        gate.go.set()                                # A runs last: the bot is on, so it leaves them unpaused
        older.join(5)
        self.assertEqual(self.tw.pauses(), [("tw-bg", False)])

    def test_waiting_toggles_apply_the_latest_once(self):
        go, inside = threading.Event(), threading.Event()

        def api(method, path, **kw):
            if not inside.is_set():
                inside.set()
                go.wait(5)
            return FakeTwitch.api(self.tw, method, path, **kw)
        self.tw.api = api
        self.m.set_enabled(False)                    # applying "paused" when the others queue up
        self.assertTrue(inside.wait(5))
        threads = [self.m.pause_thread]
        for on in (True, False, True):
            self.m.set_enabled(on)
            threads.append(self.m.pause_thread)
        go.set()
        for t in threads:
            t.join(5)
        self.assertEqual(self.tw.pauses(), [("tw-bg", True), ("tw-bg", False)])

    def test_failed_pause_is_sent_again(self):
        def fail(*a, **kw):
            raise ApiError("Twitch: 502 down", 502)
        self.tw.api = fail
        self.m.set_enabled(False)
        self.m.pause_thread.join(5)
        del self.tw.api
        self.assertIsNone(self.m.pause_applied)      # a failed pause doesn't count as applied
        self.m.set_enabled(True)
        self.m.pause_thread.join(5)
        self.m.set_enabled(False)
        self.m.pause_thread.join(5)
        self.assertEqual(self.tw.pauses(), [("tw-bg", False), ("tw-bg", True)])


class Boom:
    def __add__(self, other):
        raise RuntimeError("boom")


class RevertResilience(BotCase):
    """14. An exception in one revert doesn't stop the next; a dead revert loop restarts with the next revert."""
    section = RevertRace.section
    reverts = RevertRace.reverts

    def test_failing_revert_does_not_stop_the_next(self):
        real, calls = self.m._run_revert, []

        def flaky(rid, job, deadline=None):
            calls.append(rid)
            if len(calls) == 1:
                raise RuntimeError("revert blew up")
            return real(rid, job, deadline)
        self.m._run_revert = flaky
        loop = self.m.revert_thread
        with self.assertLogs("deck.TwitchBot", "ERROR") as logs:
            self.bot.dispatch("reward_redemption", redemption("r1"))
            self.assertTrue(wait_for(lambda: "bg" in self.m.reverts))
            self.clock.advance(10)
            self.assertTrue(wait_for(lambda: len(calls) == 1 and "bg" not in self.m.reverts))
            self.revert_cycles()
        self.assertTrue(any("revert blew up" in r for r in logs.output), logs.output)
        self.assertEqual(self.reverts(), [])
        self.assertTrue(loop.is_alive())
        self.bot.dispatch("reward_redemption", redemption("r2"))
        self.assertTrue(wait_for(lambda: "bg" in self.m.reverts))
        self.clock.advance(10)
        self.assertTrue(wait_for(lambda: self.reverts()))
        self.assertIs(self.m.revert_thread, loop)

    def test_dead_loop_restarts_on_next_revert(self):
        dead = self.m.revert_thread
        with self.assertLogs("deck.TwitchBot", "ERROR"):
            self.m.revert_ticks = Boom()
            dead.join(5)
        self.assertFalse(dead.is_alive())
        self.m.revert_ticks = 0
        with self.assertLogs("deck.TwitchBot", "WARNING") as logs:
            self.bot.dispatch("reward_redemption", redemption("r1"))
            self.assertTrue(wait_for(lambda: "bg" in self.m.reverts))
        self.assertTrue(any("restarting" in r for r in logs.output), logs.output)
        self.assertIsNot(self.m.revert_thread, dead)
        self.assertTrue(self.m.revert_thread.is_alive())
        self.clock.advance(10)
        self.assertTrue(wait_for(lambda: self.reverts()))


if __name__ == "__main__":
    unittest.main()
