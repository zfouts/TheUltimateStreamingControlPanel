"""Go-live announcements (modules/announce.py) against a fake Twitch module, the real Discord
webhook registry (modules/discord.py) and a fake HTTP post, with injected clocks so retries and
cooldowns never wait for real. Nothing reaches the network."""
import copy
import io
import json
import logging
import os
import stat
import threading
import time
import types
import unittest

from tests.helpers import ROOT, tmpdir

import yaml  # noqa: E402

from core import backups, webhooks  # noqa: E402
from core.bot import DeckBot  # noqa: E402
from core.module import REWARD_DENIED  # noqa: E402
from core.webapi import ApiError  # noqa: E402
import modules.announce as an  # noqa: E402
import modules.discord as dc  # noqa: E402

TOKEN = "FAKEtoken_" + "Zq9-x_" * 10
URL = f"https://discord.com/api/webhooks/123456789012345678/{TOKEN}"
TOKEN2 = "OTHERtoken_" + "Pp7-y_" * 10
URL2 = f"https://discord.com/api/webhooks/223456789012345678/{TOKEN2}"


def wait_for(pred, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.003)
    return False


def dest(did="main", **kw):
    return {"id": did, "name": "My Discord", "webhook": "golive", "enabled": True,
            "message": "{channel} is live! {title}\n{url}", "embed": True, "ping": "none", **kw}


def section(*dests, **kw):
    return {"enabled": True, "cooldown": 30, "destinations": list(dests) or [dest()], **kw}


DISCORD = {"webhooks": [{"id": "golive", "name": "Go-live"}, {"id": "other", "name": "Other"},
                        {"id": "spare", "name": "Spare"}]}


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self.body, self.headers = status, body, headers or {}

    def json(self):
        if self.body is None:
            raise ValueError("no JSON")
        return self.body

    def close(self):
        pass


class FakeHTTP:
    """requests.post stand-in: answers from a script (default 200), records every call, and can block."""

    def __init__(self):
        self.calls, self.script = [], []
        self.gate = None             # threading.Event: post waits for it
        self.entered = threading.Event()
        self.on_call = None

    def __call__(self, url, **kw):
        self.calls.append({"url": url, **copy.deepcopy(kw)})
        self.entered.set()
        if self.on_call:
            self.on_call()
        if self.gate is not None:
            self.gate.wait(5)
        r = self.script.pop(0) if self.script else Resp(200, {"id": "1"})
        if isinstance(r, Exception):
            raise r
        return r

    def contents(self):
        return [c["json"]["content"] for c in self.calls]


class FakeTwitch:
    config_key = "fake_twitch"

    def __init__(self):
        self.channel = "streamer"
        self.user = {"id": "1001", "login": "streamer", "display_name": "Streamer"}
        self.stream = {"title": "Speedrun night", "game_name": "Celeste",
                       "thumbnail_url": "https://static-cdn.jtvnw.net/previews-ttv/live_user_streamer-{width}x{height}.jpg"}
        self.live = None
        self.block = None
        self.is_linked = True
        self.calls = []

    def on_config(self, config):
        pass

    def linked(self):
        return self.is_linked

    def me(self):
        return self.user

    def api(self, method, path, params=None, json=None):
        self.calls.append((method, path, dict(params or {})))
        if self.block is not None:
            self.block.wait(5)
        if path == "/streams":
            return {"data": [dict(self.stream)] if self.stream else []}
        raise AssertionError(path)

    def channel_info(self):
        return {"title": "Offline title", "game_name": "Chess"}


class FakeEventSub:
    config_key = "twitch_bot"

    def __init__(self):
        self.config = {"enabled": True}
        self.snap = {"connected": True, "subscriptions": ["stream.offline", "stream.online"], "error": None}

    def on_config(self, config):
        pass

    def snapshot(self):
        return dict(self.snap)


class Base(unittest.TestCase):
    section = section()

    def setUp(self):
        self.old_state = os.environ.get("DECK_STATE_DIR")
        self.state_dir = tmpdir()
        os.environ["DECK_STATE_DIR"] = self.state_dir
        self.addCleanup(self.restore_env)
        self.path = os.path.join(tmpdir(), "config.yaml")
        with open(self.path, "w") as f:
            yaml.safe_dump({"brightness": 50, "pages": {"main": []}, "discord": DISCORD, "announce": self.section}, f,
                           sort_keys=False)
        self.http = FakeHTTP()
        self.tw = FakeTwitch()
        self.es = FakeEventSub()
        self.wall, self.mono = Clock(1_800_000_000.0), Clock(5_000.0)
        self.sleeps = []
        self.m = self.d = None
        self.make(self.section)

    def restore_env(self):
        if self.old_state is None:
            os.environ.pop("DECK_STATE_DIR", None)
        else:
            os.environ["DECK_STATE_DIR"] = self.old_state

    def make(self, sec, urls=None, discord=DISCORD):
        """A fresh bot + modules on the same state dir (a daemon restart keeps the state files)."""
        self.stop()
        self.bot = DeckBot(root=ROOT, config_path=self.path)
        self.bot.cfg = {"announce": copy.deepcopy(sec), "discord": copy.deepcopy(discord)}
        self.bot.modules["Twitch"] = self.tw
        self.bot.modules["EventSub"] = self.es
        d = dc.Discord(self.bot)
        d.wall, d.mono, d.http_post = self.wall, self.mono, self.http
        self.d = d
        self.bot.add_module(d)
        m = an.Announce(self.bot)
        m.wall, m.mono, m.random = self.wall, self.mono, lambda: 0.5
        m.http_post = self.http
        m.info_timeout = 2

        def sleep(s):
            self.sleeps.append(s)
            self.mono.advance(s)
            self.wall.advance(s)
            return m.stopping.is_set()
        m.sleep = sleep
        self.m = m
        if urls is not None:
            self.write_urls(urls)
        self.bot.add_module(m)
        self.addCleanup(self.stop)
        return m

    def write_urls(self, urls):
        """The Discord module's secret store: {webhook id: url}."""
        tmp = self.d.store.stage(urls)
        self.d.store.commit(tmp)

    def stop(self):
        if self.m is not None:
            if self.http.gate is not None:
                self.http.gate.set()
            if self.tw.block is not None:
                self.tw.block.set()
            self.bot.remove_module(self.m)
            self.m = None
        if self.d is not None:
            self.bot.remove_module(self.d)
            self.d = None

    def online(self, sid="s1", kind="live"):
        self.bot.dispatch("stream_online", {"stream_id": sid, "type": kind, "started_at": "2026-11-02T20:00:00Z"})

    def idle(self):
        self.assertTrue(wait_for(lambda: self.m.jobs.unfinished_tasks == 0 and not self.m.testing),
                        "worker never went idle")

    def state_file(self, name=an.STATE_FILE):
        with open(os.path.join(self.state_dir, name)) as f:
            return json.load(f)


class WithURL(Base):
    def setUp(self):
        super().setUp()
        self.write_urls({"golive": URL})


# ----------------------------------------------------------------------- config

class TestConfig(unittest.TestCase):
    def test_defaults_when_absent(self):
        cfg, errors = an.validate(None)
        self.assertEqual(cfg, {"enabled": False, "cooldown": 30, "destinations": []})
        self.assertEqual(errors, [])

    def test_destination_defaults(self):
        cfg, errors = an.validate({"enabled": True, "destinations": [{"id": "main"}]})
        self.assertEqual(errors, [])
        self.assertEqual(cfg["destinations"], [{"id": "main", "name": "main", "webhook": "", "enabled": True,
                                                "message": an.DEFAULT_MESSAGE, "embed": True, "ping": "none"}])

    def test_invalid_values_fall_back_with_errors(self):
        cfg, errors = an.validate({"enabled": "yes", "cooldown": 5000, "destinations": [
            {"id": "a", "name": "x" * 41, "enabled": "no", "embed": 1, "message": "", "ping": "role:12"},
            {"id": "Bad Id"}, {"id": "a"}, {"id": "c", "webhook": URL}, "nope"]})
        self.assertEqual((cfg["enabled"], cfg["cooldown"]), (False, 30))
        self.assertEqual([d["id"] for d in cfg["destinations"]], ["a", "c"])
        d = cfg["destinations"][0]
        self.assertEqual((d["name"], d["enabled"], d["embed"], d["message"], d["ping"]),
                         ("a", True, True, an.DEFAULT_MESSAGE, "none"))
        self.assertEqual(cfg["destinations"][1]["webhook"], "")       # a URL is never taken as a webhook
        self.assertEqual(len(errors), 11, errors)
        self.assertNotIn(TOKEN[:12], json.dumps(errors))

    def test_webhook_is_an_id_and_type_is_dropped(self):
        cfg, errors = an.validate({"destinations": [{"id": "a", "webhook": "golive", "type": "discord"},
                                                    {"id": "b", "webhook": None}, {"id": "c", "webhook": 5}]})
        self.assertEqual([d["webhook"] for d in cfg["destinations"]], ["golive", "", ""])
        self.assertNotIn("type", cfg["destinations"][0])
        self.assertEqual(len(errors), 1, errors)

    def test_ping_forms(self):
        for ping in ("none", "everyone", "here", "role:123456789012345678"):
            self.assertEqual(an.validate({"destinations": [{"id": "a", "ping": ping}]})[0]["destinations"][0]["ping"],
                             ping)
        for ping in ("role:abc", "role:1", "@everyone", "", None, 5):
            self.assertTrue(an.validate({"destinations": [{"id": "a", "ping": ping}]})[1], ping)

    def test_at_most_ten_destinations(self):
        cfg, errors = an.validate({"destinations": [{"id": f"d{i}"} for i in range(12)]})
        self.assertEqual(len(cfg["destinations"]), 10)
        self.assertTrue(errors)

    def test_config_url_key_ignored_with_warning(self):
        bot = types.SimpleNamespace(get_module=lambda n: None)
        m = an.Announce(bot)
        with self.assertLogs("deck.Announce", "WARNING") as logs:
            m.on_config({"enabled": True, "destinations": [{"id": "main", "url": URL, "webhook": URL}]})
        self.assertEqual([d["id"] for d in m.cfg["destinations"]], ["main"])
        self.assertNotIn("url", m.cfg["destinations"][0])
        self.assertEqual(m.cfg["destinations"][0]["webhook"], "")
        out = "\n".join(logs.output)
        self.assertIn("url is ignored", out)
        self.assertIn("never its URL", out)
        self.assertNotIn(TOKEN[:12], out)
        self.assertNotIn(TOKEN[:12], json.dumps(m.snapshot()))
        self.assertEqual(m.webhooks_view(), [])
        self.assertEqual(m.discord_usage(), [])


# ----------------------------------------------------------------------- rendering

class TestRender(unittest.TestCase):
    INFO = {"channel": "Streamer", "login": "streamer", "title": "Speedrun", "game": "Celeste", "thumbnail": None}

    def payload(self, info=None, test=False, **kw):
        return an.build_payload(dest(**kw), {**self.INFO, **(info or {})}, test=test, buster=1234)

    def test_mentions_and_links_in_a_title_are_inert(self):
        p = self.payload({"title": "@everyone free stuff [click](https://evil.example) <@&1> <@123> **bold**"})
        content = p["content"]
        self.assertIn(r"\@everyone", content)
        self.assertIn(r"\[click\]\(https://evil.example\)", content)
        self.assertNotIn("<@", content)
        self.assertNotIn("**", content.replace(r"\*", ""))
        self.assertEqual(p["allowed_mentions"], {"parse": []})
        self.assertIn(r"\@everyone", p["embeds"][0]["title"])

    def test_unknown_and_attribute_placeholders_stay_literal(self):
        p = self.payload(message="{title.__class__} {title} {user} {TITLE} {0} {}")
        self.assertTrue(p["content"].startswith("{title.__class__} Speedrun {user} {TITLE} {0} {}"))

    def test_single_pass(self):
        p = self.payload({"title": "{url} {channel}"}, message="{title}|{channel}")
        self.assertEqual(p["content"], "{url} {channel}|Streamer")

    def test_values_cleaned_and_capped(self):
        p = self.payload({"title": "a\x1b[31m‮b\nc\r​d" + "t" * 300, "channel": "c" * 40, "game": "g" * 200},
                         message="{title}|{channel}|{game}")
        title, channel, game = p["content"].split("|")
        # ANSI is stripped and a lone CR becomes a space (core.text, both tightened in the C1 refactor)
        self.assertTrue(title.startswith("ab c d"), title[:12])
        self.assertEqual(len(title), 140)
        self.assertEqual((len(channel), len(game)), (25, 100))

    def test_template_markdown_is_kept_and_output_capped(self):
        p = self.payload(message="**{channel}** is __live__")
        self.assertEqual(p["content"], "**Streamer** is __live__")
        p = self.payload(message="x" * 1500 + "{title}" * 100)
        self.assertEqual(len(p["content"]), 2000)

    def test_url_is_validated_login(self):
        self.assertEqual(an.placeholder_values({"login": "My_Name"})["url"], "https://twitch.tv/my_name")
        for bad in ("evil.example/x", "a b", "x" * 26, None, ""):
            self.assertEqual(an.placeholder_values({"login": bad})["url"], "")

    def test_pings(self):
        p = self.payload(ping="everyone")
        self.assertTrue(p["content"].startswith("@everyone Streamer"))
        self.assertEqual(p["allowed_mentions"], {"parse": ["everyone"]})
        p = self.payload(ping="here")
        self.assertTrue(p["content"].startswith("@here "))
        self.assertEqual(p["allowed_mentions"], {"parse": ["everyone"]})
        p = self.payload(ping="role:123456789012345678")
        self.assertTrue(p["content"].startswith("<@&123456789012345678> "))
        self.assertEqual(p["allowed_mentions"], {"parse": [], "roles": ["123456789012345678"]})

    def test_test_post_prefix_never_pings(self):
        p = self.payload(test=True, ping="everyone")
        self.assertTrue(p["content"].startswith("[Test] @everyone "))
        self.assertEqual(p["allowed_mentions"], {"parse": []})

    def test_embed(self):
        thumb = "https://static-cdn.jtvnw.net/previews-ttv/live_user_streamer-{width}x{height}.jpg"
        e = self.payload({"thumbnail": thumb})["embeds"][0]
        self.assertEqual(e["title"], "Speedrun")
        self.assertEqual(e["url"], "https://twitch.tv/streamer")
        self.assertEqual(e["description"], "Celeste")
        self.assertEqual(e["image"]["url"],
                         "https://static-cdn.jtvnw.net/previews-ttv/live_user_streamer-1280x720.jpg?t=1234")
        self.assertNotIn("username", self.payload())
        self.assertNotIn("avatar_url", self.payload())
        e = self.payload({"title": "", "game": ""})["embeds"][0]
        self.assertEqual(e["title"], "Streamer is live")
        self.assertNotIn("description", e)
        self.assertNotIn("embeds", self.payload(embed=False))

    def test_escaped_text_fits_discord_limits(self):
        p = self.payload({"title": "*" * 300}, message="x" * 1999 + "{title}")
        self.assertEqual(p["embeds"][0]["title"], "\\*" * 70)       # capped after escaping: never 280 chars
        for text in (p["content"], p["embeds"][0]["title"]):
            self.assertLessEqual(len(text), 2000)
            trailing = len(text) - len(text.rstrip("\\"))
            self.assertEqual(trailing % 2, 0, "ends in half an escape")
        self.assertEqual(an.cut("ab\\*", 3), "ab")
        self.assertEqual(an.cut("a\\\\b", 3), "a\\\\")

    def test_thumbnail_host_checked(self):
        for bad in ("http://static-cdn.jtvnw.net/x.jpg", "https://evil.example/x.jpg",
                    "https://static-cdn.jtvnw.net.evil.example/x.jpg", "https://u@static-cdn.jtvnw.net/x.jpg",  # privacy-check: allow
                    "https://static-cdn.jtvnw.net:444/x.jpg", "https://static-cdn.jtvnw.net/x.jpg?a=1",
                    "https://static-cdn.jtvnw.net/{other}.jpg", None, 5):
            self.assertIsNone(an.thumbnail(bad, 1), bad)


# ----------------------------------------------------------------------- triggers and dedupe

class TestTrigger(WithURL):
    def test_announces_once_per_stream_id_even_after_restart(self):
        self.online("s1")
        self.idle()
        self.assertEqual(len(self.http.calls), 1)
        self.online("s1")            # EventSub replay
        self.idle()
        self.wall.advance(3600)      # long past the cooldown: the same stream is still not announced again
        self.make(self.section)      # daemon restart: the state is persisted
        self.online("s1")
        self.idle()
        self.assertEqual(len(self.http.calls), 1)
        self.assertEqual(self.state_file()["last_stream_id"], "s1")

    def test_clock_going_back_is_within_the_cooldown(self):
        self.online("s1")
        self.idle()
        self.wall.advance(-10 * 60)  # the clock was put back: last_at is now in the future
        self.online("s2")
        self.idle()
        self.assertEqual(len(self.http.calls), 1)
        self.assertEqual(self.state_file()["last_stream_id"], "s1")
        self.wall.advance(-30 * 60)  # further back than a whole cooldown: announced again
        self.online("s3")
        self.idle()
        self.assertEqual(len(self.http.calls), 2)

    def test_reconnect_within_cooldown_gives_one_post(self):
        self.online("s1")
        self.idle()
        self.wall.advance(5 * 60)
        self.online("s2")            # a quick restart of the stream: a new stream id
        self.idle()
        self.assertEqual(len(self.http.calls), 1)
        self.wall.advance(30 * 60)
        self.online("s3")
        self.idle()
        self.assertEqual(len(self.http.calls), 2)

    def test_only_type_live(self):
        for kind in ("playlist", "rerun", "premiere", "", None):
            self.online("x", kind=kind)
        self.bot.dispatch("stream_online", {})
        self.bot.dispatch("stream_online")
        self.idle()
        self.assertEqual(self.http.calls, [])

    def test_master_switch(self):
        self.make(section(enabled=False))
        self.online("s1")
        self.idle()
        self.assertEqual(self.http.calls, [])

    def test_content_and_payload(self):
        self.online("s1")
        self.idle()
        call = self.http.calls[0]
        self.assertEqual(call["url"], URL + "?wait=true")
        self.assertEqual(call["json"]["content"], "Streamer is live! Speedrun night\nhttps://twitch.tv/streamer")
        self.assertEqual(call["json"]["allowed_mentions"], {"parse": []})
        self.assertIn("1280x720.jpg?t=", call["json"]["embeds"][0]["image"]["url"])
        self.assertEqual(self.m.snapshot()["destinations"]["main"]["state"], "ok")
        self.assertEqual(self.m.snapshot()["last"], {"at": self.wall.t, "stream_id": "s1", "kind": "live"})

    def test_falls_back_to_channel_info(self):
        self.tw.stream = None
        self.online("s1")
        self.idle()
        self.assertIn("Offline title", self.http.contents()[0])

    def test_twitch_too_slow_posts_without_title(self):
        self.tw.block = threading.Event()
        self.m.info_timeout = 0.05
        self.online("s1")
        self.idle()
        self.assertEqual(self.http.contents(), ["streamer is live! \nhttps://twitch.tv/streamer"])
        self.tw.block.set()

    def test_listener_returns_fast_while_post_blocks(self):
        self.http.gate = threading.Event()
        start = time.perf_counter()
        self.online("s1")
        self.assertTrue(self.http.entered.wait(2))
        took = []
        for i in range(6):           # one in flight, 4 queued, the rest dropped: never blocks
            t = time.perf_counter()
            self.online(f"n{i}")
            took.append(time.perf_counter() - t)
        self.assertLess(time.perf_counter() - start, 2)
        self.assertLess(max(took), 0.01)
        self.assertEqual(self.m.jobs.qsize(), an.QUEUE_MAX)
        self.http.gate.set()
        self.idle()

    def test_state_recorded_before_posting(self):
        seen = {}
        self.http.on_call = lambda: seen.update(self.state_file())
        self.online("s1")
        self.idle()
        self.assertEqual(seen["last_stream_id"], "s1")

    def test_announce_now_ignores_dedupe_and_records(self):
        self.online("s1")
        self.idle()
        self.assertEqual(self.m.announce_now(), {"queued": True})
        self.idle()
        self.assertEqual(len(self.http.calls), 2)
        self.assertEqual(self.state_file()["last_kind"], "manual")
        self.online("s2")           # within the cooldown of the manual announcement
        self.idle()
        self.assertEqual(len(self.http.calls), 2)

    def test_announce_now_without_twitch(self):
        self.tw.is_linked = False
        self.bot.actions["announce_now"]("now")
        self.idle()
        self.assertEqual(self.http.contents(), ["streamer is live! \nhttps://twitch.tv/streamer"])

    def test_announce_now_needs_switch_and_destination(self):
        self.make(section(enabled=False))
        with self.assertRaises(ApiError):
            self.m.announce_now()
        self.make(section(dest(enabled=False)))
        with self.assertRaises(ApiError):
            self.m.announce_now()

    def test_announce_now_not_allowed_in_rewards(self):
        self.assertIn("announce_now", REWARD_DENIED)
        self.assertEqual(self.bot.actions["announce_now"].__deck_ui__["group"], "Twitch")

    def test_disabled_and_unusable_destinations_skipped(self):
        self.make(section(dest(), dest("off", enabled=False, webhook="other"), dest("nourl", webhook="spare"),
                          dest("unknown", webhook="gone"), dest("nohook", name="No hook", webhook=""),
                          dest("offnohook", name="Off no hook", enabled=False, webhook="")),
                  urls={"golive": URL, "other": URL2})
        with self.assertLogs("deck.Announce", "INFO") as logs:
            self.online("s1")
            self.idle()
        self.assertEqual([c["url"] for c in self.http.calls], [URL + "?wait=true"])
        self.assertTrue([line for line in logs.output if "No hook skipped" in line])
        self.assertFalse([line for line in logs.output if "Off no hook" in line])
        snap = self.m.snapshot()["destinations"]
        self.assertEqual(snap["off"]["state"], "never")
        self.assertEqual(snap["offnohook"], {"state": "never", "at": None, "detail": None})
        self.assertEqual(snap["nourl"], {"state": "error", "at": None,
                                         "detail": "The Spare webhook has no URL yet; add it in Settings → Discord"})
        for did in ("unknown", "nohook"):
            self.assertEqual(snap[did], {"state": "error", "at": None,
                                         "detail": "Pick a Discord webhook (Settings → Discord)"})

    def test_unknown_webhook_destination(self):
        self.make(section(dest(webhook="gone")), urls={"golive": URL, "gone": URL2})   # a URL alone isn't enough
        self.assertEqual(self.m.snapshot()["destinations"]["main"]["detail"], an.PICK_WEBHOOK)
        with self.assertRaises(ApiError) as e:
            self.m.test("main")
        self.assertEqual((e.exception.status, str(e.exception)), (409, an.PICK_WEBHOOK))
        with self.assertRaises(ApiError):
            self.m.announce_now()
        self.online("s1")
        self.idle()
        self.assertEqual(self.http.calls, [])
        self.assertEqual(self.d.webhook_url("gone"), None)

    def test_results_reported_to_discord(self):
        self.assertEqual(self.d.snapshot()["webhooks"]["golive"]["state"], "never")
        self.online("s1")
        self.idle()
        self.assertEqual(self.d.snapshot()["webhooks"]["golive"]["state"], "ok")
        self.http.script = [Resp(400, {"message": "Invalid Form Body"})]
        self.m.announce_now()
        self.idle()
        hook = self.d.snapshot()["webhooks"]["golive"]
        self.assertEqual(hook["state"], "error")
        self.assertIn("HTTP 400", hook["detail"])
        self.assertEqual(self.m.snapshot()["destinations"]["main"]["state"], "error")

    def test_destinations_sharing_a_webhook_are_spaced(self):
        self.make(section(dest(), dest("again", name="Again")), urls={"golive": URL})
        self.online("s1")
        self.idle()
        self.assertEqual(len(self.http.calls), 2)
        self.assertEqual(self.sleeps, [an.DEST_GAP])
        self.assertEqual(self.d.usage(), {"golive": ["Announcements: My Discord", "Announcements: Again"]})


# ----------------------------------------------------------------------- retries

class TestRetries(WithURL):
    def test_429_then_200(self):
        self.http.script = [Resp(429, {"retry_after": 1.5}), Resp(200, {"id": "1"})]
        self.online("s1")
        self.idle()
        self.assertEqual(len(self.http.calls), 2)
        self.assertGreaterEqual(sum(self.sleeps), 2)          # retry_after, but never under the 2 s gap
        self.assertEqual(self.m.snapshot()["destinations"]["main"]["state"], "ok")

    def test_retry_after_used_when_longer_than_gap(self):
        self.http.script = [Resp(429, {"retry_after": 12}), Resp(200, {})]
        self.online("s1")
        self.idle()
        self.assertAlmostEqual(sum(self.sleeps), 12)

    def test_5xx_retried_three_times_with_backoff(self):
        self.http.script = [Resp(503)] * 5
        self.online("s1")
        self.idle()
        self.assertEqual(len(self.http.calls), an.ATTEMPTS)
        self.assertEqual(self.sleeps, [5 * 1.1, 30 * 1.1])
        snap = self.m.snapshot()["destinations"]["main"]
        self.assertEqual(snap["state"], "error")
        self.assertIn("HTTP 503", snap["detail"])

    def test_gives_up_after_ten_minutes(self):
        self.http.script = [Resp(429, {"retry_after": 60})] * 3
        self.wall.advance(0)
        self.http.on_call = lambda: self.wall.advance(590)     # each attempt "takes" a long time
        self.online("s1")
        self.idle()
        self.assertEqual(len(self.http.calls), 2)
        self.assertEqual(self.m.snapshot()["destinations"]["main"]["state"], "error")

    def test_404_marks_the_webhook_broken_until_replaced(self):
        self.http.script = [Resp(404, {"message": "Unknown Webhook"})]
        self.online("s1")
        self.idle()
        self.assertEqual(len(self.http.calls), 1)
        broken = "The Go-live webhook is broken; replace it in Settings → Discord"
        snap = self.m.snapshot()["destinations"]["main"]
        self.assertEqual((snap["state"], snap["detail"]), ("broken", broken))
        self.assertTrue(self.d.is_broken("golive"))
        self.assertIn("HTTP 404", self.d.snapshot()["webhooks"]["golive"]["detail"])
        self.assertIn("golive", self.state_file(dc.STATE_FILE)["broken"])        # kept by the Discord module
        self.assertNotIn("broken", self.state_file())
        for name in (dc.STATE_FILE, an.STATE_FILE):
            self.assertNotIn(TOKEN[:12], json.dumps(self.state_file(name)))
        # skipped (persisted across a restart) ...
        self.make(self.section)
        self.wall.advance(3600)
        self.online("s2")
        self.idle()
        self.assertEqual(len(self.http.calls), 1)
        with self.assertRaises(ApiError):
            self.m.announce_now()
        with self.assertRaises(ApiError) as e:
            self.m.test("main")
        self.assertEqual((e.exception.status, str(e.exception)), (409, broken))
        self.assertEqual(self.m.snapshot()["destinations"]["main"]["state"], "broken")
        # ... until its URL is replaced
        self.write_urls({"golive": URL2})
        self.assertEqual(self.m.snapshot()["destinations"]["main"]["state"], "never")
        self.m.announce_now()
        self.idle()
        self.assertEqual(self.http.calls[-1]["url"], URL2 + "?wait=true")

    def test_broken_webhook_skipped_others_still_post(self):
        self.make(section(dest(), dest("second", name="Second", webhook="other"), dest("third", name="Third")),
                  urls={"golive": URL, "other": URL2})
        self.http.script = [Resp(404)]          # the first post (main, on golive) finds it deleted
        self.online("s1")
        self.idle()
        self.assertEqual([c["url"] for c in self.http.calls], [URL + "?wait=true", URL2 + "?wait=true"])
        snap = self.m.snapshot()["destinations"]
        self.assertEqual((snap["main"]["state"], snap["second"]["state"], snap["third"]["state"]),
                         ("broken", "ok", "broken"))
        self.assertEqual(snap["third"]["detail"], "The Go-live webhook is broken; replace it in Settings → Discord")
        self.m.announce_now()                   # third shares the broken webhook: never posted to
        self.idle()
        self.assertEqual([c["url"] for c in self.http.calls][2:], [URL2 + "?wait=true"])

    def test_result_for_a_replaced_url_is_not_blamed_on_the_new_one(self):
        self.http.script = [Resp(404)]
        self.http.on_call = lambda: self.write_urls({"golive": URL2})     # replaced while the post was out
        self.online("s1")
        self.idle()
        self.assertFalse(self.d.is_broken("golive"))
        self.assertEqual(self.d.webhook_url("golive"), URL2)

    def test_network_error_is_redacted_everywhere(self):
        import requests
        err = requests.ConnectionError(f"HTTPSConnectionPool(host='discord.com', port=443): Max retries exceeded "
                                       f"with url: /api/webhooks/123456789012345678/{TOKEN}?wait=true")
        self.http.script = [err, err, err]
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(name)s %(message)s"))
        loggers = [logging.getLogger(n) for n in ("deck.Announce", "deck.Discord", "core.webhooks")]
        for lg in loggers:
            lg.addHandler(handler)
            lg.setLevel(logging.DEBUG)
            lg.propagate = False
        try:
            self.online("s1")
            self.idle()
        finally:
            for lg in loggers:
                lg.removeHandler(handler)
                lg.setLevel(logging.NOTSET)
                lg.propagate = True
        self.assertEqual(len(self.http.calls), 3)
        self.assertIn("ConnectionError", stream.getvalue())
        for text in (stream.getvalue(), json.dumps(self.m.snapshot()), json.dumps(self.state_file()),
                     json.dumps(self.d.snapshot()), json.dumps(self.d.webhooks())):
            self.assertNotIn(TOKEN[:12], text)
            self.assertNotIn("123456789012345678", text)


# ----------------------------------------------------------------------- test posts

class TestTestPost(WithURL):
    def test_prefix_no_dedupe_no_state(self):
        self.assertEqual(self.m.test("main"), {"queued": True})
        self.idle()
        content = self.http.contents()[0]
        self.assertTrue(content.startswith("[Test] Streamer is live!"))
        self.assertFalse(os.path.exists(os.path.join(self.state_dir, an.STATE_FILE)))
        self.assertEqual(self.m.snapshot()["destinations"]["main"]["state"], "ok")
        self.online("s1")           # a real announcement still goes out
        self.idle()
        self.assertEqual(len(self.http.calls), 2)

    def test_cooldown_and_one_in_flight(self):
        self.http.gate = threading.Event()
        self.m.test("main")
        self.assertTrue(self.http.entered.wait(2))
        self.mono.advance(60)         # past the cooldown, but the first is still in flight
        with self.assertRaises(ApiError) as e:
            self.m.test("main")
        self.assertEqual(e.exception.status, 429)
        self.http.gate.set()
        self.idle()
        with self.assertRaises(ApiError) as e:     # 10 s after the last request
            self.mono.advance(-55)
            self.m.test("main")
        self.assertEqual((e.exception.status, str(e.exception)), (429, "Wait a few seconds before testing again."))
        self.mono.advance(an.TEST_COOLDOWN)
        self.assertEqual(self.m.test("main"), {"queued": True})
        self.idle()
        self.assertEqual(len(self.http.calls), 2)

    def test_errors(self):
        for bad in (None, 5, "BAD ID", "unknown"):
            with self.assertRaises(ApiError):
                self.m.test(bad)
        self.write_urls({})
        with self.assertRaises(ApiError) as e:
            self.m.test("main")
        self.assertEqual(e.exception.status, 409)
        self.assertIn("no URL yet", str(e.exception))

    def test_url_removed_before_the_test_ran(self):
        route, calls = self.m._route, []

        def removed_after_accepting(dest, discord=None):
            calls.append(dest["id"])
            out = route(dest, discord)
            if len(calls) == 1:                   # test() accepted it; the URL goes before the post
                self.write_urls({})
            return out
        self.m._route = removed_after_accepting
        self.assertEqual(self.m.test("main"), {"queued": True})
        self.idle()
        self.assertEqual(calls, ["main", "main"])
        self.assertEqual(self.http.calls, [])
        snap = self.m.snapshot()["destinations"]["main"]
        self.assertEqual((snap["state"], snap["detail"]),
                         ("error", "The Go-live webhook has no URL yet; add it in Settings → Discord"))

    def test_uses_the_sample_values_and_never_asks_twitch(self):
        self.tw.live = {"live": True, "title": "Cached title", "game_name": "Cached game"}
        self.m.test("main")
        self.idle()
        self.assertEqual(self.tw.calls, [])
        payload = self.http.calls[0]["json"]
        self.assertEqual(payload["content"], "[Test] Streamer is live! Cached title\nhttps://twitch.tv/streamer")
        self.assertEqual(payload["embeds"][0]["description"], "Cached game")
        self.tw.is_linked, self.tw.live = False, None   # nothing cached: the panel's examples
        self.mono.advance(an.TEST_COOLDOWN)
        self.m.test("main")
        self.idle()
        self.assertEqual(self.http.contents()[1], "[Test] YourChannel is live! Building a stream deck from scratch\n"
                                                  "https://twitch.tv/yourchannel")
        self.assertEqual(self.tw.calls, [])

    def test_tests_never_crowd_out_a_go_live(self):
        names = ("b", "c", "e", "f", "g", "h")
        self.make(section(dest(), *(dest(n, name=n.upper()) for n in names), cooldown=0))
        self.http.gate = threading.Event()
        self.m.announce_now()                     # the worker is stuck in a slow post
        self.assertTrue(self.http.entered.wait(2))
        for n in names:                           # more tests than the queue has room for
            self.assertEqual(self.m.test(n), {"queued": True})
        self.assertEqual(self.m.jobs.qsize(), 0)
        self.online("s9")
        self.assertEqual(self.m.jobs.qsize(), 1)
        self.http.gate.set()
        self.idle()
        contents = self.http.contents()
        self.assertEqual(sum(c.startswith("[Test] ") for c in contents), len(names))
        live = [c for c in contents if not c.startswith("[Test] ")]
        self.assertEqual(len(live), 2 * (1 + len(names)))     # announce now, then the go-live, to all 7
        self.assertEqual(self.state_file()["last_stream_id"], "s9")

    def test_failure_shown_without_retry(self):
        self.http.script = [Resp(500)]
        self.m.test("main")
        self.idle()
        self.assertEqual(len(self.http.calls), 1)
        snap = self.m.snapshot()["destinations"]["main"]
        self.assertEqual(snap["state"], "error")


# ----------------------------------------------------------------------- secrets, readiness, files

class TestSecretsAndState(Base):
    def test_announce_keeps_no_urls(self):
        self.assertFalse(hasattr(an, "SECRETS_FILE"))
        self.assertFalse(hasattr(self.m, "store"))
        self.write_urls({"golive": URL})
        self.online("s1")
        self.idle()
        self.assertEqual(sorted(os.listdir(self.state_dir)), sorted([an.STATE_FILE, dc.SECRETS_FILE]))
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.state_dir, dc.SECRETS_FILE)).st_mode), 0o600)
        self.assertNotIn(TOKEN[:12], json.dumps(self.state_file()))

    def test_never_in_daily_backups(self):
        for name in (dc.SECRETS_FILE, dc.STATE_FILE, an.STATE_FILE):
            self.assertNotIn(name, backups.DAILY_STATE_FILES)

    def test_corrupt_state_file(self):
        with open(os.path.join(self.state_dir, an.STATE_FILE), "w") as f:
            f.write("{nope")
        self.make(self.section)
        self.assertIsNone(self.m.snapshot()["last"]["at"])

    def test_readiness(self):
        ready = self.m.snapshot()["ready"]
        self.assertEqual(ready, {"eventsub": True, "twitch": True, "detail": None})
        self.es.snap = {"connected": True, "subscriptions": ["stream.offline"], "error": None}
        self.assertFalse(self.m.snapshot()["ready"]["eventsub"])
        self.es.config = {"enabled": False}
        self.es.snap = {"connected": False, "subscriptions": [], "error": None}
        self.assertIn("Twitch Bot is on", self.m.snapshot()["ready"]["detail"])
        self.tw.is_linked = False
        ready = self.m.snapshot()["ready"]
        self.assertFalse(ready["twitch"])
        self.assertIn("Announce now", ready["detail"])

    def test_snapshot_shape(self):
        snap = self.m.snapshot()
        self.assertEqual(set(snap), {"enabled", "ready", "last", "destinations"})
        self.assertEqual(snap["last"], {"at": None, "stream_id": None, "kind": None})
        self.assertEqual(snap["destinations"]["main"]["state"], "error")        # its webhook has no URL yet
        self.write_urls({"golive": URL})
        self.assertEqual(self.m.snapshot()["destinations"]["main"], {"state": "never", "at": None, "detail": None})
        json.dumps(snap)

    def test_editor_log_redaction(self):
        import modules.editor as editor
        out = editor.REDACT.sub(lambda m: m.group(1) + "[redacted]", f"posting to {URL} failed")
        self.assertNotIn(TOKEN[:12], out)


if __name__ == "__main__":
    unittest.main()
