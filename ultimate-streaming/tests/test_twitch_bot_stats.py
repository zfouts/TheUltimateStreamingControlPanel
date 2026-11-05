"""Twitch Bot v3: default commands, built-ins (!stats !top10 !uptime !commands) and chat-stats
recording, against the fake ChatStats from test_twitch_bot (never core/chatstats.py)."""
import datetime
import glob
import inspect
import json
import os
import re
import sqlite3
import stat
import tempfile
import threading
import types
import unittest
from unittest import mock

from tests.test_twitch_bot import BotCase, FakeChatStats, chat, command, redemption, reward, wait_for

from core import configio  # noqa: E402

from core.paths import state_dir, state_file  # noqa: E402
from core.webapi import ApiError, Router, bot_api  # noqa: E402
import modules.twitch_bot as tb  # noqa: E402

SECRET_URL = "postgresql+psycopg://bot:s3cretpw@db.example/stats"  # privacy-check: allow


def started(minutes):
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes, seconds=30)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def chatter(uid, login, display, messages, stream=0, rank=1, **kw):
    return {"user_id": uid, "login": login, "display": display, "messages": messages, "stream_messages": stream,
            "commands": 0, "redemptions": 0, "points_spent": 0, "rank": rank, "stream_rank": None,
            "first_seen": datetime.datetime(2026, 3, 3, 12).timestamp(), "last_seen": 0, **kw}


class StatsCase(BotCase):
    section = {"enabled": True}

    def say(self, text, user="Viewer", uid="2001", mid=None, badges=()):
        before = len(self.tw.sent)
        self.clock.advance(31)      # past every default cooldown and the duplicate-text window
        self.bot.dispatch("chat_message", chat(text, user=user, uid=uid, mid=mid or f"m{self.clock.t}",
                                               badges=badges))
        self.settle()
        return [t for t, _ in self.tw.sent[before:]]

    def assertQueriesOffChatThread(self):
        threads = {t for _, t in self.m.stats.queries}
        self.assertLessEqual(threads, {"twitch-bot-chat"}, self.m.stats.queries)


# ----- defaults

class Defaults(StatsCase):
    def test_absent_means_defaults_explicit_empty_means_none(self):
        self.assertEqual(self.m.cfg["commands"], tb.DEFAULT_COMMANDS)
        self.assertEqual([c["id"] for c in tb.DEFAULT_COMMANDS],
                         ["commands", "uptime", "game", "title", "stats", "top10", "lurk", "unlurk", "discord",
                          "socials", "schedule", "specs"])
        self.assertEqual(tb.validate({"commands": []})[0]["commands"], [])
        self.assertEqual(tb.validate({"commands": tb.DEFAULT_COMMANDS}), tb.validate({}))   # fully specified
        self.assertEqual(tb.validate({})[1], [])
        off = {c["id"] for c in tb.DEFAULT_COMMANDS if not c["enabled"]}
        self.assertEqual(off, {"discord", "socials", "schedule", "specs"})
        by_id = {c["id"]: c for c in tb.DEFAULT_COMMANDS}
        self.assertEqual(by_id["top10"]["aliases"], ["!top"])
        self.assertEqual((by_id["stats"]["user_cooldown_seconds"], by_id["lurk"]["user_cooldown_seconds"]), (30, 300))
        self.assertTrue(all(c["cooldown_seconds"] == 5 for c in tb.DEFAULT_COMMANDS))

    def test_defaults_route(self):
        router = Router()
        bot_api.register(router, types.SimpleNamespace(bot=self.bot))
        self.assertEqual(router.gets["/api/bot/defaults"]({}), {"commands": tb.DEFAULT_COMMANDS})

    def test_text_defaults(self):
        self.tw.stream_status = lambda: {"live": True, "started_at": started(5), "title": "Live title",
                                         "game_name": "Chess"}
        self.assertEqual(self.say("!game"), ["streamer is playing Chess."])
        self.assertEqual(self.say("!TITLE"), ["Stream title: Live title"])
        self.assertEqual(self.say("!lurk"), ["Viewer is lurking. Enjoy the stream!"])
        self.assertEqual(self.say("!unlurk"), ["Welcome back, Viewer!"])
        self.assertEqual(self.say("!discord"), [])         # off by default


# ----- validation

class PerKindPlaceholders(unittest.TestCase):
    def rejected(self, item, needle):
        out, errors = tb.validate({"commands": [command(**item)]})
        self.assertEqual(out["commands"], [], errors)
        self.assertTrue(any(needle in e for e in errors), errors)

    def ok(self, **item):
        out, errors = tb.validate({"commands": [command(**item)]})
        self.assertEqual(errors, [])
        return out["commands"][0]

    def test_text(self):
        self.rejected({"response": "{list}"}, "{list}")
        self.rejected({"response": "{messages}"}, "{messages}")
        self.rejected({"response": "{period}"}, "{period}")
        c = self.ok(response="{user} {channel} {uptime} {game} {title} {count}")
        self.assertEqual((c["builtin"], c["offline_response"], c["missing_response"], c["count"]), (None, "", "", 10))
        self.rejected({"offline_response": "x"}, "only for builtin: uptime")
        self.rejected({"missing_response": "x"}, "only for builtin: stats")

    def test_stats(self):
        c = self.ok(builtin="stats", response="{user} {messages} {rank} {stream_messages} {stream_rank} "
                                              "{first_seen} {redemptions} {commands} {count}")
        self.assertEqual(c["missing_response"], "{user}, no stats yet. Say hi!")
        self.rejected({"builtin": "stats", "response": "{list}"}, "{list}")
        self.rejected({"builtin": "stats", "missing_response": "{user} has {messages}"}, "{messages}")
        self.rejected({"builtin": "stats", "offline_response": "x"}, "only for builtin: uptime")

    def test_top10(self):
        self.assertEqual(self.ok(builtin="top10", response="{channel} {period}: {list}", count=3)["count"], 3)
        self.rejected({"builtin": "top10", "response": "{user}: {list}"}, "{user}")
        self.rejected({"builtin": "top10", "response": "{list}", "count": 11}, "count")
        self.rejected({"builtin": "top10", "response": "{list}", "count": 0}, "count")

    def test_uptime(self):
        c = self.ok(builtin="uptime", response="{channel} {uptime} {game} {title}")
        self.assertEqual(c["offline_response"], "{channel} is offline right now.")
        self.rejected({"builtin": "uptime", "response": "{user} {uptime}"}, "{user}")
        self.rejected({"builtin": "uptime", "offline_response": "offline for {uptime}"}, "{uptime}")

    def test_commands_and_unknown(self):
        self.ok(builtin="commands", response="{user}: {list} on {channel}")
        self.rejected({"builtin": "commands", "response": "{title} {list}"}, "{title}")
        self.rejected({"builtin": "Bogus!"}, "builtin must be one of")
        # A well-formed name may be a plugin's built-in that isn't loaded: kept (and skipped in chat).
        self.assertEqual(self.ok(builtin="bogus")["builtin"], "bogus")
        self.rejected({"builtin": "stats", "response": "!{messages}"}, "can't start with !")

    def test_stats_section_and_database_url(self):
        out, errors = tb.validate({"stats": {"exclude": ["NightBot", "x y"], "include_broadcaster": "yes"}})
        self.assertEqual(len(errors), 2, errors)
        self.assertEqual(out["stats"]["exclude"], ["nightbot"])
        out, errors = tb.validate({"database_url": "postgresql://u:hunter2@h/db\n"})
        self.assertTrue(errors)
        self.assertNotIn("hunter2", json.dumps(errors))
        self.assertEqual(tb.validate({"database_url": " sqlite:///x.db "})[0]["database_url"], "sqlite:///x.db")


# ----- built-ins (all run on the sender thread)

class Builtins(StatsCase):
    def setUp(self):
        super().setUp()
        self.m.stats.users = {
            "2001": chatter("2001", "viewer", "Viewer", 1234, stream=5, rank=3, stream_rank=2),
            "2002": chatter("2002", "someone", "SomeOne", 987, stream=9, rank=4)}

    def test_stats_self(self):
        self.assertEqual(self.say("!stats"),
                         ["Viewer: 1,234 messages (#3 all time), 5 this stream. Here since Mar 3, 2026."])
        self.assertQueriesOffChatThread()

    def test_stats_other_shows_stored_name_only(self):
        out = self.say("!stats @SOMEONE")
        self.assertEqual(out, ["SomeOne: 987 messages (#4 all time), 9 this stream. Here since Mar 3, 2026."])
        self.assertNotIn("SOMEONE", out[0])
        out = self.say("!stats @nobody_here")
        self.assertEqual(out, ["Viewer, no stats yet. Say hi!"])     # the asker, never the typed name
        self.assertEqual(self.say("!stats <script>alert(1)</script>")[0][:7], "Viewer:")   # not a login: own stats

    def test_stats_unknown_asker(self):
        self.assertEqual(self.say("!stats", user="Newbie", uid="3003"), ["Newbie, no stats yet. Say hi!"])

    def test_stream_rank_placeholder(self):
        self.make({"enabled": True, "commands": [command(builtin="stats", response="{user} #{stream_rank}",
                                                         trigger="!s")]})
        self.m.stats.users = {"2001": chatter("2001", "viewer", "Viewer", 1, stream=0)}
        self.assertEqual(self.say("!s"), ["Viewer #-"])

    def test_top10(self):
        self.assertEqual(self.say("!top10"), ["Top chatters (all time): 1. Viewer (1,234) · 2. SomeOne (987)"])
        self.assertEqual(self.say("!top STREAM please"), ["Top chatters (this stream): 1. SomeOne (9) · 2. Viewer (5)"])
        self.assertEqual(self.say("!top10 week"), ["Top chatters (all time): 1. Viewer (1,234) · 2. SomeOne (987)"])
        self.m.stats.users = {}
        self.assertEqual(self.say("!top10"), ["Top chatters (all time): nobody yet"])
        self.assertQueriesOffChatThread()

    def test_top10_fits_500_with_whole_entries(self):
        self.m.stats.users = {str(i): chatter(str(i), f"u{i}", chr(65 + i) * 60, 1000 - i) for i in range(10)}
        out = self.say("!top10")[0]
        self.assertLessEqual(len(out), 500)
        entries = out.split(": ", 1)[1].split(" · ")
        self.assertLess(len(entries), 10)
        for i, e in enumerate(entries):
            self.assertRegex(e, rf"^{i + 1}\. {chr(65 + i) * 60} \({1000 - i:,}\)$")

    def test_top10_count(self):
        self.make({"enabled": True, "commands": [command(builtin="top10", response="Top: {list}", trigger="!t", count=1)]})
        self.m.stats.users = {"1": chatter("1", "a", "A", 5), "2": chatter("2", "b", "B", 3)}
        self.assertEqual(self.say("!t"), ["Top: 1. A (5)"])

    def test_uptime(self):
        self.tw.stream_status = lambda: {"live": True, "started_at": started(65), "title": "t", "game_name": "g"}
        self.assertEqual(self.say("!uptime"), ["streamer has been live for 1h 5m."])
        self.tw.stream_status = lambda: {"live": False}
        self.assertEqual(self.say("!uptime"), ["streamer is offline right now."])

    def test_commands_by_permission(self):
        self.assertEqual(self.say("!commands"), [
            "Commands: !commands, !uptime, !game, !title, !stats, !top10, !lurk, !unlurk"])
        self.make({"enabled": True, "commands": tb.DEFAULT_COMMANDS[:1] + [
            command("modonly", trigger="!ban2", permission="moderator", response="x")]})
        self.assertEqual(self.say("!commands"), ["Commands: !commands"])
        self.assertEqual(self.say("!commands", badges=("moderator",)), ["Commands: !commands, !ban2"])

    def test_commands_fits_500(self):
        many = [command(f"c{i}", trigger="!" + f"{i:02d}" + "x" * 23, response="x") for i in range(40)]
        self.make({"enabled": True, "commands": tb.DEFAULT_COMMANDS[:1] + many})
        out = self.say("!commands")[0]
        self.assertLessEqual(len(out), 500)
        triggers = {c["trigger"] for c in self.m.cfg["commands"]}
        listed = out.split(": ", 1)[1].split(", ")
        self.assertTrue(10 < len(listed) < 41)
        self.assertTrue(all(t in triggers for t in listed), listed)

    def test_query_errors_are_reported_by_type_only(self):
        def boom(*a, **kw):
            raise RuntimeError("connection to postgresql://bot:s3cretpw@db failed")
        self.m.stats.top = self.m.stats.user = boom
        self.assertEqual(self.say("!top10"), [])
        self.assertEqual(self.say("!stats"), [])
        self.assertIn("RuntimeError", self.m.recent[-1]["error"])
        self.assertNotIn("s3cretpw", json.dumps(list(self.m.recent)))
        with self.assertRaises(ApiError) as e:
            self.m.stats_top()
        self.assertEqual(e.exception.status, 503)
        self.assertNotIn("s3cretpw", str(e.exception))

    def test_stats_off_or_broken_posts_nothing(self):
        self.m.stats.error = "OperationalError: database is locked"
        self.assertEqual(self.say("!stats"), [])
        self.assertEqual(self.say("!top10"), [])
        self.assertEqual(self.m.recent[-1]["kind"], "error")
        self.assertFalse(self.m.recent[-1]["ok"])
        self.make({"enabled": True, "stats": {"enabled": False}})
        self.assertIsNone(self.m.stats)
        self.assertEqual(self.say("!stats"), [])
        self.assertEqual(self.m.recent[-1]["kind"], "error")
        self.assertEqual(self.say("!lurk"), ["Viewer is lurking. Enjoy the stream!"])   # text commands still work


# ----- recording

class Recording(StatsCase):
    section = {"enabled": True, "rewards": [reward()]}

    def messages(self):
        return [r for r in self.m.stats.records if r[0] == "message"]

    def test_message_recorded_on_chat_thread_without_queries(self):
        self.bot.dispatch("chat_message", chat("hello there", user="Viewer", uid="2001", mid="a"))
        self.assertEqual(self.m.stats.records, [("message", ("2001", "viewer", "Viewer"), "MainThread")])
        self.assertEqual(self.m.stats.queries, [])

    def test_non_ascii_display_name_uses_irc_login(self):
        msg = chat("hello", user="ミク", uid="2002", mid="b")
        msg["login"] = "miku_fan"
        self.bot.dispatch("chat_message", msg)
        self.assertEqual(self.messages(), [("message", ("2002", "miku_fan", "ミク"), "MainThread")])

    def test_skipped_messages(self):
        self.bot.dispatch("chat_message", chat("hi", user="Nightbot", uid="19264788", mid="a"))
        self.bot.dispatch("chat_message", chat("hi", user="Streamer", uid="1001", mid="b"))
        self.bot.dispatch("chat_message", chat("hi", user="Boss", uid="9", mid="c", badges=("broadcaster",)))
        self.assertEqual(self.messages(), [])
        self.say("!lurk")                                  # the bot's own reply comes back as sent-1
        self.bot.dispatch("chat_message", chat(self.tw.sent[0][0], user="Streamer", uid="1001", mid="sent-1"))
        self.assertEqual([r[1][0] for r in self.messages()], ["2001"])
        self.assertEqual({t for _, t in self.m.stats.queries}, set())

    def test_include_broadcaster(self):
        self.make({"enabled": True, "stats": {"include_broadcaster": True}})
        self.bot.dispatch("chat_message", chat("hi", user="Streamer", uid="1001", mid="b", badges=("broadcaster",)))
        self.assertEqual(len(self.messages()), 1)

    def test_only_when_bot_and_stats_enabled(self):
        self.make({"enabled": False})
        self.bot.dispatch("chat_message", chat("hi", mid="a"))
        self.bot.dispatch("stream_online", {})
        self.assertEqual(self.m.stats.records, [])
        self.make({"enabled": True, "stats": {"enabled": False}})
        self.bot.dispatch("chat_message", chat("hi", mid="a"))
        self.assertIsNone(self.m.stats)

    def test_command_redemption_and_new_stream(self):
        self.say("!lurk")
        self.assertIn(("command", ("2001", "viewer", "Viewer"), "twitch-bot-chat"), self.m.stats.records)
        self.say("!discord")                               # disabled: not a command use
        self.assertEqual(len([r for r in self.m.stats.records if r[0] == "command"]), 1)
        self.bot.dispatch("reward_redemption", redemption("r1"))
        self.assertTrue(wait_for(lambda: any(r[0] == "redemption" for r in self.m.stats.records)))
        self.assertIn(("redemption", ("2001", "viewer", "Viewer", 500), "twitch-bot-rewards"), self.m.stats.records)
        self.bot.dispatch("reward_redemption", redemption("r2", text="not a color"))
        self.settle()
        self.assertEqual(len([r for r in self.m.stats.records if r[0] == "redemption"]), 1)   # refunded
        self.bot.dispatch("stream_online", {})
        self.assertEqual(self.m.stats.records[-1][0], "new_stream")


# ----- lifecycle

class Lifecycle(StatsCase):
    def test_default_url_and_exclude(self):
        st = self.m.stats
        self.assertTrue(st.started)
        self.assertEqual(st.url, "sqlite:///" + state_file("chatstats.db"))
        self.assertEqual(st.exclude, set(tb.DEFAULT_EXCLUDE))

    def test_url_change_rebuilds(self):
        old = self.m.stats
        self.m.on_config({"enabled": True, "stats": {"exclude": ["x_bot"]}})
        self.assertIs(self.m.stats, old)                   # same store, new exclude list
        self.assertEqual(old.exclude, {"x_bot"})
        self.m.on_config({"enabled": True, "database_url": SECRET_URL})
        self.m.stats_thread.join(5)
        self.assertTrue(old.closed)
        self.assertEqual((self.m.stats.url, self.m.stats.started), (SECRET_URL, True))
        new = self.m.stats
        self.m.on_config({"enabled": True, "database_url": SECRET_URL, "stats": {"enabled": False}})
        self.m.stats_thread.join(5)
        self.assertTrue(new.closed)
        self.assertIsNone(self.m.stats)

    def test_closed_on_unload_thread(self):
        st = self.m.stats
        self.stop()
        self.assertTrue(st.closed)
        self.assertFalse(self.m.unload_thread.is_alive())

    def test_start_failure_never_shows_the_url(self):
        class Broken(FakeChatStats):
            def start(self):
                raise RuntimeError(f"could not connect to {self.url}")
        self.stats_class = Broken
        self.make({"enabled": True, "database_url": SECRET_URL})
        self.assertIsNone(self.m.stats)
        view = self.m.stats_view()
        self.assertIn("RuntimeError", view["error"])
        self.assertNotIn("s3cretpw", json.dumps(view))


# ----- API

class StatsApi(StatsCase):
    section = {"enabled": True, "database_url": SECRET_URL}

    def setUp(self):
        super().setUp()
        self.router = Router()
        bot_api.register(self.router, types.SimpleNamespace(bot=self.bot))
        self.m.stats.users = {"2001": chatter("2001", "viewer", "Viewer", 10, stream=2)}

    def get(self, path, query=None):
        out = self.router.gets[path](query or {})
        self.assertNotIn("s3cretpw", json.dumps(out))
        self.assertNotIn(SECRET_URL, json.dumps(out))
        return out

    def post(self, path, body):
        out = self.router.posts[path](body)
        self.assertNotIn("s3cretpw", json.dumps(out))
        return out

    def error(self, fn, arg):
        with self.assertRaises(ApiError) as e:
            fn(arg)
        self.assertNotIn("s3cretpw", str(e.exception))
        return e.exception.status

    def test_get_has_stats_but_no_database_url(self):
        out = self.get("/api/bot")
        self.assertNotIn("database_url", out["config"])
        self.assertEqual(set(out["stats"]), {"enabled", "url", "backend", "error", "totals"})
        self.assertEqual(out["stats"]["url"], "postgresql+psycopg://bot:***@db.example/stats")
        self.assertEqual(out["stats"]["totals"], {"chatters": 1, "messages": 10, "stream_messages": 2})

    def test_config_post_keeps_database_url(self):
        out = self.post("/api/bot/config", {"config": {"enabled": True, "database_url": "sqlite:////tmp/evil.db"}})
        self.assertNotIn("database_url", out["config"])
        self.assertEqual(self.file()["twitch_bot"]["database_url"], SECRET_URL)
        self.assertEqual(self.m.stats.url, SECRET_URL)

    def test_stats_routes(self):
        self.assertEqual(self.get("/api/bot/stats")["top"][0]["display"], "Viewer")
        self.assertEqual(self.get("/api/bot/stats", {"period": "stream", "n": "1"})["top"][0]["messages"], 2)
        for q in ({"n": "0"}, {"n": "101"}, {"n": "x"}, {"period": "week"}):
            self.assertEqual(self.error(self.router.gets["/api/bot/stats"], q), 400)
        self.assertEqual(self.get("/api/bot/stats/user", {"login": "@Viewer"})["user_id"], "2001")
        self.assertEqual(self.error(self.router.gets["/api/bot/stats/user"], {"login": "nobody"}), 404)
        self.assertEqual(self.error(self.router.gets["/api/bot/stats/user"], {"login": "a b"}), 400)

    def test_reset(self):
        reset = self.router.posts["/api/bot/stats/reset"]
        self.assertEqual(self.error(reset, {"scope": "all"}), 400)                  # no confirm
        self.assertEqual(self.error(reset, {"scope": "everything", "confirm": True}), 400)
        self.assertEqual(self.post("/api/bot/stats/reset", {"scope": "all", "confirm": True}),
                         {"ok": True, "backup": None})                             # not SQLite: no backup
        self.assertEqual(self.m.stats.resets, ["all"])
        self.make({"enabled": True, "stats": {"enabled": False}})
        self.router = Router()
        bot_api.register(self.router, types.SimpleNamespace(bot=self.bot))
        self.assertEqual(self.error(self.router.posts["/api/bot/stats/reset"], {"scope": "all", "confirm": True}), 503)
        self.assertEqual(self.error(self.router.gets["/api/bot/stats"], {}), 503)


class ResetBackup(StatsCase):
    def setUp(self):
        self.backups = os.path.join(state_dir(), "backups")
        for f in glob.glob(os.path.join(self.backups, "chatstats-*.db")):
            os.remove(f)
        self.db = os.path.join(os.path.dirname(state_file("x")), "test-chatstats.db")
        with sqlite3.connect(self.db) as c:
            c.execute("DROP TABLE IF EXISTS chatters")
            c.execute("CREATE TABLE chatters (user_id TEXT PRIMARY KEY, messages INTEGER)")
            c.execute("INSERT INTO chatters VALUES (?, ?)", ("2001", 42))
        c.close()
        self.section = {"enabled": True, "database_url": f"sqlite:///{self.db}"}
        super().setUp()
        self.m.stats.users = {"2001": chatter("2001", "viewer", "Viewer", 42)}

    def test_all_backs_up_then_resets(self):
        os.makedirs(self.backups, exist_ok=True)
        for i in range(11):
            old = os.path.join(self.backups, f"chatstats-20000101-0000{i:02d}.db")
            open(old, "w").close()
            os.utime(old, (946684800 + i, 946684800 + i))
        out = self.m.reset_stats("all", True)
        self.assertRegex(out["backup"], r"^chatstats-\d{8}-\d{6}(-\d+)?\.db$")
        path = os.path.join(self.backups, out["backup"])
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with sqlite3.connect(path) as c:
            self.assertEqual(c.execute("SELECT user_id, messages FROM chatters").fetchall(), [("2001", 42)])
        c.close()
        kept = glob.glob(os.path.join(self.backups, "chatstats-*.db"))
        self.assertEqual(len(kept), 10)
        self.assertIn(path, kept)
        names = [q for q, _ in self.m.stats.queries]
        self.assertLess(names.index("flush"), names.index("reset"))
        self.assertEqual(self.m.stats.resets, ["all"])

    def test_stream_scope_has_no_backup(self):
        self.assertEqual(self.m.reset_stats("stream", True), {"ok": True, "backup": None})
        self.assertEqual(glob.glob(os.path.join(self.backups, "chatstats-*.db")), [])

    def test_no_backup_possible_refuses_while_rows_exist(self):
        os.remove(self.db)
        with self.assertRaises(ApiError) as e:
            self.m.reset_stats("all", True)
        self.assertEqual(e.exception.status, 500)
        self.assertIn("couldn't back up the database first", str(e.exception))
        self.assertEqual(self.m.stats.resets, [])
        self.m.stats.sqlite_path = lambda: None
        with self.assertRaises(ApiError):
            self.m.reset_stats("all", True)
        self.m.stats.users = {}                           # nothing to lose: reset without a copy
        self.assertEqual(self.m.reset_stats("all", True), {"ok": True, "backup": None})
        self.assertEqual(self.m.stats.resets, ["all"])


# ----- review round 2

class Secrets(StatsCase):
    """1. The database password lives in a file or the environment, never config.yaml or a browser."""

    def test_password_from_env_or_file(self):
        with mock.patch.dict(os.environ, {"DECK_DATABASE_PASSWORD": "envpw"}):
            self.make({"enabled": True, "database_url": "postgresql+psycopg://bot@db/stats"})
        self.assertEqual(self.m.stats.password, "envpw")
        pw = os.path.join(tempfile.mkdtemp(dir=os.environ["HOME"]), "db.pw")
        with open(os.open(pw, os.O_WRONLY | os.O_CREAT, 0o600), "w") as f:
            f.write("filepw\n")
        self.make({"enabled": True, "database_url": "postgresql+psycopg://bot@db/stats", "database_password_file": pw})
        self.assertEqual(self.m.stats.password, "filepw")
        self.assertNotIn("database_password_file", self.m.config_view())
        self.m.save_config({"enabled": True, "database_password_file": "/tmp/other"})
        self.assertEqual(self.file()["twitch_bot"]["database_password_file"], pw)
        self.assertEqual(self.m.stats.password, "filepw")
        self.make({"enabled": True})
        self.assertIsNone(self.m.stats.password)

    def test_mask(self):
        mask = bot_api.mask_database_url
        self.assertEqual(mask(SECRET_URL), "postgresql+psycopg://bot:***@db.example/stats")
        self.assertEqual(mask("mysql+pymysql://u@h/db?ssl_key=/k&sslpassword=pw&charset=utf8"),
                         "mysql+pymysql://u@h/db?charset=utf8&ssl_key=%2A%2A%2A&sslpassword=%2A%2A%2A")
        self.assertEqual(mask("not a url"), "***")
        self.assertEqual(mask(""), "")

    def test_general_editor_masks_and_restores(self):
        from modules.editor import Editor
        with open(self.path) as f:
            cfg = configio.safe_load_bounded(f.read())
        cfg["twitch_bot"] = {"enabled": True, "database_url": SECRET_URL, "database_password_file": "~/.pw"}
        configio.save(self.path, cfg)
        ed = Editor(self.bot)
        ed.catalog = lambda: {}
        ed.device = lambda: None
        ed.decks = lambda: []
        with mock.patch.object(type(self.bot), "page", new=None, create=True):
            state = ed.state()
        self.assertNotIn("s3cretpw", json.dumps(state, default=str))
        sent = state["config"]
        self.assertEqual(sent["twitch_bot"]["database_url"], "postgresql+psycopg://bot:***@db.example/stats")
        self.assertEqual(sent["twitch_bot"]["database_password_file"], "~/.pw")      # a path is fine
        ed.validate = lambda cfg: []
        ed.save({"config": sent, "hash": state["hash"]})                   # unchanged, masked
        self.assertEqual(self.file()["twitch_bot"]["database_url"], SECRET_URL)
        without = dict(sent, twitch_bot={k: v for k, v in sent["twitch_bot"].items() if k != "database_url"})
        ed.save({"config": without, "hash": configio.file_hash(self.path)})   # missing: kept too
        self.assertEqual(self.file()["twitch_bot"]["database_url"], SECRET_URL)
        changed = dict(sent, twitch_bot={**sent["twitch_bot"], "database_url": "sqlite:////tmp/new.db"})
        ed.save({"config": changed, "hash": configio.file_hash(self.path)})   # a real edit wins
        self.assertEqual(self.file()["twitch_bot"]["database_url"], "sqlite:////tmp/new.db")


class RebuildRace(StatsCase):
    """3. A rebuild builds for the newest settings, once."""

    def test_latest_settings_win(self):
        built = []

        class Counting(FakeChatStats):
            def __init__(self, url, **kw):
                super().__init__(url, **kw)
                built.append(url)
        self.m.stats_factory = Counting
        with self.m.stats_lock:                          # both rebuilds queue behind this
            self.m.on_config({"enabled": True, "database_url": "sqlite:////tmp/a.db"})
            first = self.m.stats_thread
            self.m.on_config({"enabled": True, "database_url": "sqlite:////tmp/b.db"})
        first.join(5)
        self.m.stats_thread.join(5)
        self.assertEqual(built, ["sqlite:////tmp/b.db"])
        self.assertEqual(self.m.stats.url, "sqlite:////tmp/b.db")


class ListFit(unittest.TestCase):
    """4. {list} keeps whole entries, a leading ! survives, and nothing starts with ! or /."""
    triggers = ["!" + f"{i:02d}" + "x" * 23 for i in range(40)]

    def test_list_first_is_rejected(self):
        for kind in ("commands", "top10"):
            for template in ("{list}", "{list}!", "  {list} are the commands"):
                out, errors = tb.validate({"commands": [command(builtin=kind, response=template)]})
                self.assertEqual(out["commands"], [], template)
                self.assertTrue(any("can't start with {list}" in e for e in errors), errors)

    def test_fit_with_40_commands(self):
        out = tb.render_list("Commands: {list}", {}, self.triggers, ", ")
        self.assertTrue(out.startswith("Commands: !00"))
        listed = out[len("Commands: "):].split(", ")
        self.assertLessEqual(len(out), 500)
        self.assertEqual(listed, self.triggers[:len(listed)])
        for template in ("{list}", "{list}!"):           # rendered anyway (e.g. hand-built): still safe
            out = tb.render_list(template, {}, self.triggers, ", ")
            self.assertLessEqual(len(out), 500)
            self.assertFalse(out.startswith(("!", "/")), out)
            body = out[:-1] if template.endswith("!") else out
            self.assertFalse(body.endswith((",", ", ")))
            self.assertTrue(all(t.lstrip("!") in self.triggers[i] for i, t in enumerate(body.split(", "))), body)
            self.assertTrue(out.endswith("x!" if template.endswith("!") else "x"))


class Lookup(StatsCase):
    """5. stats.allow_lookup: false ignores !stats @name."""

    def test_lookup_off(self):
        self.make({"enabled": True, "stats": {"allow_lookup": False}})
        self.m.stats.users = {"2001": chatter("2001", "viewer", "Viewer", 1),
                              "2002": chatter("2002", "someone", "SomeOne", 2)}
        self.assertTrue(self.say("!stats @someone")[0].startswith("Viewer: 1 messages"))


class Parity(unittest.TestCase):
    """6. The fake keeps the real ChatStats' signatures."""

    def test_signatures(self):
        from core.chatstats import ChatStats

        def shape(fn):
            return [(p.name, p.kind, p.default) for p in inspect.signature(fn).parameters.values()]
        public = [n for n, v in vars(ChatStats).items() if callable(v) and not n.startswith("_")] + ["__init__"]
        for name in public:
            self.assertTrue(hasattr(FakeChatStats, name), name)
            self.assertEqual(shape(getattr(FakeChatStats, name)), shape(getattr(ChatStats, name)), name)


class RealChatStats(BotCase):
    """6. The bot against the real core/chatstats.py on a temporary SQLite file."""
    section = {"enabled": True}

    def setUp(self):
        from core.chatstats import ChatStats
        self.stats_class = ChatStats
        self.db = os.path.join(tempfile.mkdtemp(dir=os.environ["HOME"]), "stats.db")
        self.section = {"enabled": True, "database_url": f"sqlite:///{self.db}",
                        "stats": {"exclude": ["nightbot"]}}
        super().setUp()

    def talk(self, text, user, uid, login=None):
        msg = chat(text, user=user, uid=uid, mid=f"m{self.clock.t}")
        msg["login"] = login or user.lower()
        self.clock.advance(31)
        before = len(self.tw.sent)
        self.bot.dispatch("chat_message", msg)
        self.settle()
        return [t for t, _ in self.tw.sent[before:]]

    def test_stats_top10_ties_and_excludes(self):
        self.assertIsNone(self.m.stats.describe()["error"])
        for _ in range(3):
            self.talk("hi", "Alice", "11")
            self.talk("hi", "Bob", "12")
            self.talk("hi", "Nightbot", "13")
        self.talk("hi", "Carol", "14")
        top = self.talk("!top10", "Dave", "15")
        self.assertEqual(top, ["Top chatters (all time): 1. Alice (3) · 1. Bob (3) · 3. Carol (1) · 3. Dave (1)"])
        out = self.talk("!stats", "Alice", "11")[0]
        self.assertTrue(out.startswith("Alice: 4 messages (#1 all time), 4 this stream. Here since "), out)
        self.assertEqual(self.talk("!stats @nightbot", "Bob", "12"), ["Bob, no stats yet. Say hi!"])

    def test_degraded_url_posts_nothing(self):
        self.make({"enabled": True, "database_url": "postgresql+nosuchdriver://u@localhost/x"})
        self.assertTrue(self.m.stats.describe()["error"])
        self.assertEqual(self.talk("!stats", "Alice", "11"), [])
        self.assertEqual(self.talk("!top10", "Alice", "11"), [])
        self.assertEqual(self.m.recent[-1]["kind"], "error")


class StatsApiNits(StatsApi):
    """7. n is 1-3 digits only."""

    def test_n(self):
        for n in (" 5", "5 ", "1000", "+5", "٥"):
            self.assertEqual(self.error(self.router.gets["/api/bot/stats"], {"n": n}), 400, n)
        self.assertEqual(len(self.get("/api/bot/stats", {"n": "100"})["top"]), 1)

    def test_null_commands_means_none(self):
        self.assertEqual(tb.validate({"commands": None})[0]["commands"], [])


class CommandPreview(StatsCase):
    """8. command/test renders built-ins like live, synchronously, without posting."""

    def preview(self, cid):
        return self.m.command_test(cid)

    def test_builtins(self):
        out = self.preview("stats")
        self.assertTrue(out.pop("sample"))
        self.assertTrue(out["text"].startswith("TestViewer: 1,234 messages (#1 all time), 56 this stream. Here since "))
        self.assertEqual(self.preview("top10"), {"text": "Top chatters (all time): nobody yet"})
        self.m.stats.users = {"2001": chatter("2001", "viewer", "Viewer", 9, stream=2, rank=1)}
        self.assertEqual(self.preview("stats"), {
            "text": "Viewer: 9 messages (#1 all time), 2 this stream. Here since Mar 3, 2026."})   # the top chatter
        self.assertEqual(self.preview("top10"), {"text": "Top chatters (all time): 1. Viewer (9)"})
        self.assertTrue(self.preview("commands")["text"].startswith("Commands: !commands, !uptime"))
        self.tw.stream_status = lambda: {"live": False}
        self.assertEqual(self.preview("uptime"), {"text": "streamer is offline right now."})
        self.tw.stream_status = lambda: {"live": True, "started_at": started(65)}
        self.assertEqual(self.preview("uptime"), {"text": "streamer has been live for 1h 5m."})
        self.assertEqual(self.tw.sent, [])
        self.make({"enabled": True, "stats": {"enabled": False}})
        self.assertTrue(self.preview("stats")["sample"])
        self.assertEqual(self.preview("top10"), {"text": "Top chatters (all time): nobody yet"})


if __name__ == "__main__":
    unittest.main()
