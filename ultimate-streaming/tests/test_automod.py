"""Auto-mod: keyword matching (core/automod.py), the module that screens chat and bans through
a fake Twitch (modules/automod.py), the chat reader's hook (modules/chat.py), the panel routes
(core/webapi/automod_api.py) and Twitch.unban. Nothing reaches the network."""
import copy
import inspect
import os
import queue
import threading
import time
import types
import unittest
from unittest import mock

from tests.helpers import ROOT, tmpdir

import yaml  # noqa: E402

from core import automod as am  # noqa: E402
from core import configio  # noqa: E402
from core.events import Hub  # noqa: E402
from core.webapi import ApiError, Router, automod_api  # noqa: E402
from modules.automod import AutoMod  # noqa: E402
from modules.chat import Chat  # noqa: E402

ME = "1001"


def msg(text, user="Spammer", uid="777", mid="m1", badges=(), login=None):
    return {"id": mid, "user": user, "login": login or user.lower(), "user_id": uid, "text": text,
            "action": False, "color": "", "mod": "moderator" in badges or "broadcaster" in badges,
            "sub": "subscriber" in badges, "badges": list(badges)}


def matcher(*terms):
    cfg, errors = am.validate({"terms": list(terms)})
    assert not errors, errors
    return am.Matcher(cfg["terms"])


class Canon(unittest.TestCase):
    def test_folds(self):
        self.assertEqual(am.squash("ＳＴＲＥＡＭ𝐁𝐎𝐎"), "streamboo")
        self.assertEqual(am.squash("strеаmbοο"), "streamboo")          # Cyrillic е а, Greek ο
        self.assertEqual(am.squash("stréämböö"), "streamboo")          # accents
        self.assertEqual(am.squash("5tr3amb00"), "streamboo")          # digit swaps
        self.assertEqual(am.term_key("$TREAMBOO"), "streamboo")      # symbols as letters
        self.assertEqual(am.squash("$TREAMBOO!"), "treamboo")         # ... or as punctuation
        self.assertEqual(am.squash("ⓢⓣⓡⓔⓐⓜⓑⓞⓞ"), "streamboo")          # circled
        self.assertEqual(am.squash("fo1low"), am.squash("follow"))      # i, l and 1 are one letter
        self.assertEqual(am.squash("foIIow"), am.squash("follow"))

    def test_words(self):
        self.assertEqual(am.words("Best viewers on streamboo .com!"), ["best", "vlewers", "on", "streamboo", "com"])
        self.assertEqual(am.words(""), [])
        self.assertEqual(am.canon(None), "")


class Matching(unittest.TestCase):
    def setUp(self):
        self.m = matcher("streamboo")

    def hit(self, text, m=None):
        r = (m or self.m).find(text)
        return r and r["term"]

    def test_real_spam(self):
        for text in ("Best viewers on streamboo .com", "Cheap viewers on streamboo(.)com", "s t r e a m b o o",
                     "STREAMB00", "streаmboo dot com", "𝐬𝐭𝐫𝐞𝐚𝐦𝐛𝐨𝐨", "ｓｔｒｅａｍｂｏｏ", "str.eam-boo", "stream boo",
                     "visit streamboo's site", "@you go to streamboo", "streamboo", "streamboo!",
                     "$TREAMBOO", "@streamboo", "|streamboo|", "streamboo!!!", "go to streamboo@com"):
            self.assertEqual(self.hit(text), "streamboo", text)

    def test_word_boundaries(self):
        for text in ("the stream boost is real", "upstream boo", "streamboocom", "xstreamboo", "hello",
                     "stream", "boo"):
            self.assertIsNone(self.hit(text), text)

    def test_anywhere(self):
        m = matcher({"term": "streamboo", "match": "anywhere"})
        for text in ("streamboocom", "xstreamboox", "the stream boost"):
            self.assertEqual(self.hit(text, m), "streamboo", text)
        self.assertIsNone(self.hit("stream bo", m))

    def test_short_word(self):
        m = matcher("ass")
        for text in ("you ass", "a s s", "ASS!", "a.s.s", "a$$", "@$$"):
            self.assertEqual(self.hit(text, m), "ass", text)
        for text in ("class act", "was solid", "assassin", "pass sword", "cla$$"):
            self.assertIsNone(self.hit(text, m), text)

    def test_symbol_terms(self):
        m = matcher("$cam")
        for text in ("scam", "$cam", "SCAM!", "s c a m"):
            self.assertEqual(self.hit(text, m), "$cam", text)
        cfg, errors = am.validate({"terms": ["scam", "$cam"]})
        self.assertEqual(len(errors), 1)                  # the same term

    def test_phrase(self):
        m = matcher("buy followers")
        for text in ("BUY f0ll0wers now", "buyfollowers", "buy   followers"):
            self.assertEqual(self.hit(text, m), "buy followers", text)
        self.assertIsNone(self.hit("buy more followers", m))

    def test_strongest_wins(self):
        m = matcher({"term": "spam", "action": "delete"}, {"term": "spam link", "action": "timeout", "seconds": 60},
                    {"term": "link", "action": "timeout", "seconds": 600})
        self.assertEqual(m.find("spam link")["seconds"], 600)
        m = matcher({"term": "spam", "action": "delete"}, {"term": "scam", "action": "ban"})
        self.assertEqual(m.find("spam scam")["action"], "ban")

    def test_disabled_terms(self):
        m = matcher({"term": "streamboo", "enabled": False})
        self.assertFalse(m)
        self.assertIsNone(m.find("streamboo"))
        self.assertIsNone(self.m.find(None))

    def test_long_message_is_fast(self):
        m = matcher(*[f"term{i}x" for i in range(am.MAX_TERMS)])
        start = time.perf_counter()
        for _ in range(50):
            m.find("a b " * 125)
        self.assertLess(time.perf_counter() - start, 2.0)


class Validate(unittest.TestCase):
    def test_defaults(self):
        cfg, errors = am.validate(None)
        self.assertEqual((cfg, errors), ({"enabled": False, "exempt": "vip", "terms": []}, []))
        cfg, errors = am.validate({"enabled": True, "terms": ["streamboo"]})
        self.assertEqual(errors, [])
        self.assertEqual(cfg["terms"], [{"term": "streamboo", "match": "word", "action": "ban", "seconds": 600,
                                         "enabled": True}])

    def test_whitespace_collapsed(self):
        cfg, _ = am.validate({"terms": [{"term": "  buy \n followers "}]})
        self.assertEqual(cfg["terms"][0]["term"], "buy followers")

    def test_errors(self):
        bad = [{"term": ""}, {"term": "x" * 101}, {"term": "ok1", "match": "regex"}, {"term": "ok2", "action": "kick"},
               {"term": "ok3", "seconds": 0}, {"term": "ok4", "seconds": 1209601}, {"term": "ok5", "seconds": True},
               {"term": "ok6", "seconds": "60"}, {"term": "ok7", "enabled": "yes"}, {"term": "x"},
               {"term": "ab", "match": "anywhere"}, {"term": "!!!"}, 5, {"nope": 1}]
        cfg, errors = am.validate({"terms": bad})
        self.assertEqual(cfg["terms"], [])
        self.assertEqual(len(errors), len(bad))
        self.assertIn("use whole-word matching", errors[10])

    def test_section_errors(self):
        for section in ({"enabled": "yes"}, {"exempt": "everyone"}, {"terms": "streamboo"}, ["x"]):
            cfg, errors = am.validate(section)
            self.assertTrue(errors, section)
            self.assertEqual(cfg["terms"], [])

    def test_duplicates(self):
        cfg, errors = am.validate({"terms": ["streamboo", "Stream Boo", "STREAMB00",
                                             {"term": "streamboo", "match": "anywhere"}]})
        self.assertEqual([t["term"] for t in cfg["terms"]], ["streamboo", "streamboo"])
        self.assertEqual(len(errors), 2)
        self.assertIn("same as terms[0]", errors[0])

    def test_too_many(self):
        cfg, errors = am.validate({"terms": [f"term{i}x" for i in range(am.MAX_TERMS + 5)]})
        self.assertEqual(len(cfg["terms"]), am.MAX_TERMS)
        self.assertEqual(len(errors), 1)


class FakeTwitch:
    def __init__(self):
        self.user = {"id": ME, "login": "streamer"}
        self.is_linked = True
        self.scopes = ["moderator:manage:banned_users", "moderator:manage:chat_messages"]
        self.calls = []
        self.fail = []          # ApiErrors raised by the next calls, in order
        self.called = threading.Event()

    def linked(self):
        return self.is_linked

    def granted_scopes(self, network=True):
        return list(self.scopes)

    def _call(self, *call):
        self.calls.append(call)
        self.called.set()
        if self.fail:
            raise self.fail.pop(0)

    def ban(self, user_id, seconds=None, reason="", permanent=False):
        self._call("ban", user_id, seconds, reason, permanent)

    def delete_message(self, mid):
        self._call("delete", mid)

    def unban(self, user_id):
        self._call("unban", user_id)


class Base(unittest.TestCase):
    section = {"enabled": True, "terms": [{"term": "streamboo", "action": "ban"},
                                          {"term": "buy followers", "action": "timeout", "seconds": 300},
                                          {"term": "spoiler", "action": "delete"}]}

    def setUp(self):
        self.path = os.path.join(tmpdir(), "config.yaml")
        with open(self.path, "w") as f:
            yaml.safe_dump({"brightness": 50, "pages": {"main": []}, "automod": self.section}, f, sort_keys=False)
        self.tw = FakeTwitch()
        self.chat = types.SimpleNamespace(channel="streamer", connected=True)
        self.events = []
        hub = Hub()
        hub.publish = lambda kind, data=None: self.events.append((kind, copy.deepcopy(data)))
        self.modules = {"Twitch": self.tw, "Chat": self.chat}
        self.bot = types.SimpleNamespace(get_module=self.modules.get, hub=hub, config_path=self.path)
        self.m = AutoMod(self.bot)
        self.m.on_config(copy.deepcopy(self.section))

    def run_jobs(self):
        """Run every queued action on this thread (no worker)."""
        while True:
            try:
                job = self.m.jobs.get_nowait()
            except queue.Empty:
                return
            self.m._act(job)

    def log(self):
        return self.m.state()["log"]


class Screen(Base):
    def test_ban(self):
        self.assertTrue(self.m.screen(msg("Best viewers on streamboo .com")))
        entry = self.log()[0]
        self.assertEqual((entry["state"], entry["action"], entry["term"], entry["user_id"]),
                         ("queued", "ban", "streamboo", "777"))
        self.assertEqual(self.tw.calls, [])            # nothing on the chat thread
        self.run_jobs()
        self.assertEqual(self.tw.calls, [("ban", "777", None, 'Auto-mod: matched "streamboo"', True)])
        entry = self.log()[0]
        self.assertEqual(entry["state"], "done")
        self.assertTrue(entry["can_unban"])
        self.assertNotIn("message_id", entry)
        self.assertEqual([e[0] for e in self.events], ["automod", "automod"])
        self.assertEqual(self.events[-1][1]["entry"]["state"], "done")

    def test_timeout_and_delete(self):
        self.assertTrue(self.m.screen(msg("buy followers here", uid="801")))
        self.assertTrue(self.m.screen(msg("SPOILER: he dies", uid="802", mid="m9")))
        self.run_jobs()
        self.assertEqual(self.tw.calls, [("ban", "801", 300, 'Auto-mod: matched "buy followers"', False),
                                         ("delete", "m9")])
        self.assertFalse(self.log()[0]["can_unban"])   # a delete can't be undone

    def test_no_match(self):
        self.assertFalse(self.m.screen(msg("hello there")))
        self.assertFalse(self.m.screen(msg("the stream boost is real")))
        self.assertEqual((self.log(), self.events), ([], []))

    def test_off(self):
        self.m.on_config({**self.section, "enabled": False})
        self.assertFalse(self.m.screen(msg("streamboo")))
        self.m.on_config({"enabled": True, "terms": []})
        self.assertFalse(self.m.screen(msg("streamboo")))

    def test_exempt(self):
        for badges in (("broadcaster",), ("moderator",), ("vip",)):
            self.assertFalse(self.m.screen(msg("streamboo", badges=badges)), badges)
        self.assertTrue(self.m.screen(msg("streamboo", badges=("subscriber",), uid="1")))
        self.m.on_config({**self.section, "exempt": "subscriber"})
        self.assertFalse(self.m.screen(msg("streamboo", badges=("subscriber",), uid="2")))
        self.assertFalse(self.m.screen(msg("streamboo", badges=("founder",), uid="3")))
        self.m.on_config({**self.section, "exempt": "moderator"})
        self.assertTrue(self.m.screen(msg("streamboo", badges=("vip",), uid="4")))
        self.assertFalse(self.m.screen(msg("streamboo", badges=("moderator",), uid="5")))

    def test_own_account_never(self):
        self.assertFalse(self.m.screen(msg("streamboo", uid=ME)))

    def test_bad_messages(self):
        for bad in (None, "streamboo", {}, {"text": 5}):
            self.assertFalse(self.m.screen(bad))

    def test_repeat_is_hidden_without_a_second_ban(self):
        for i in range(5):
            self.assertTrue(self.m.screen(msg("streamboo", mid=f"m{i}")))
        self.run_jobs()
        self.assertEqual(len(self.tw.calls), 1)
        self.assertEqual(len(self.log()), 1)
        self.assertEqual(self.m.state()["status"]["caught"], 5)

    def test_repeat_window_ends(self):
        clock = [100.0]
        self.m.now = lambda: clock[0]
        self.m.screen(msg("streamboo"))
        clock[0] += am_repeat() + 1
        self.m.screen(msg("streamboo"))
        self.run_jobs()
        self.assertEqual(len(self.tw.calls), 2)

    def test_deletes_are_not_deduplicated(self):
        self.m.screen(msg("spoiler one", mid="a"))
        self.m.screen(msg("spoiler two", mid="b"))
        self.run_jobs()
        self.assertEqual(self.tw.calls, [("delete", "a"), ("delete", "b")])

    def test_log_is_capped(self):
        import modules.automod as mod
        for i in range(mod.LOG_MAX + 10):
            self.m.screen(msg("spoiler", mid=f"m{i}"))
        log = self.log()
        self.assertEqual(len(log), mod.LOG_MAX)
        self.assertEqual(log[0]["id"], mod.LOG_MAX + 10)    # newest first

    def test_raid_bigger_than_the_log(self):
        import modules.automod as mod
        n = mod.LOG_MAX + 50
        for i in range(n):
            self.assertTrue(self.m.screen(msg("streamboo", uid=str(5000 + i), mid=f"m{i}")))
        self.run_jobs()
        self.assertEqual(len(self.tw.calls), n)          # every one banned, though the log keeps 100
        self.assertEqual({e["state"] for e in self.log()}, {"done"})

    def test_queue_full(self):
        import modules.automod as mod
        with mock.patch.object(self.m, "jobs", queue.Queue(maxsize=1)):
            self.assertTrue(self.m.screen(msg("streamboo", uid="1")))
            self.assertTrue(self.m.screen(msg("streamboo", uid="2")))
        self.assertEqual(self.log()[0]["state"], "failed")
        self.assertIn("too many", self.log()[0]["detail"])
        self.assertTrue(mod.QUEUE_MAX >= 100)

    def test_text_is_capped_and_cleaned(self):
        self.m.screen(msg("streamboo \x1b[31m" + "x" * 500))
        text = self.log()[0]["text"]
        self.assertLessEqual(len(text), 200)
        self.assertNotIn("\x1b", text)


def am_repeat():
    import modules.automod as mod
    return mod.REPEAT


class Worker(Base):
    def test_not_linked(self):
        self.tw.is_linked = False
        self.m.screen(msg("streamboo"))
        self.run_jobs()
        e = self.log()[0]
        self.assertEqual((e["state"], self.tw.calls), ("failed", []))
        self.assertIn("isn’t linked", e["detail"])
        self.assertFalse(e["can_unban"])
        self.m.screen(msg("streamboo", mid="m2"))       # a failure lets the next message try again
        self.assertEqual(len(self.log()), 2)

    def test_no_twitch_module(self):
        del self.modules["Twitch"]
        self.m.screen(msg("streamboo"))
        self.run_jobs()
        self.assertEqual(self.log()[0]["state"], "failed")

    def test_rate_limited_then_ok(self):
        e = ApiError("Slow down", 429)
        e.retry_after = 0.01
        self.tw.fail = [e, ApiError("Twitch /moderation/bans: 429 Too Many Requests", 429)]
        self.m.screen(msg("streamboo"))
        self.run_jobs()
        self.assertEqual(len(self.tw.calls), 3)
        self.assertEqual(self.log()[0]["state"], "done")

    def test_gives_up_after_retries(self):
        import modules.automod as mod
        self.tw.fail = [ApiError("Slow down", 429) for _ in range(mod.RETRIES)]
        for err in self.tw.fail:
            err.retry_after = 0.001
        self.m.screen(msg("streamboo"))
        self.run_jobs()
        self.assertEqual(len(self.tw.calls), mod.RETRIES)
        self.assertEqual(self.log()[0]["state"], "failed")

    def test_already_banned(self):
        self.tw.fail = [ApiError("Twitch /moderation/bans: 400 The user specified in the user_id field is already "
                                 "banned.", 400)]
        self.m.screen(msg("streamboo"))
        self.run_jobs()
        e = self.log()[0]
        self.assertEqual((e["state"], e["detail"]), ("done", "already banned"))

    def test_other_error(self):
        self.tw.fail = [ApiError("Re-link Twitch to allow moderator:manage:banned_users", 403)]
        self.m.screen(msg("streamboo"))
        self.run_jobs()
        e = self.log()[0]
        self.assertEqual(e["state"], "failed")
        self.assertIn("Re-link", e["detail"])

    def test_unexpected_exception_keeps_worker(self):
        self.m.on_load()
        self.addCleanup(self.m.on_unload)
        self.tw.fail = [RuntimeError("boom")]
        self.m.screen(msg("streamboo", uid="1"))
        self.m.screen(msg("streamboo", uid="2"))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and [e["state"] for e in self.log()] != ["done", "failed"]:
            time.sleep(0.01)
        self.assertEqual([e["state"] for e in self.log()], ["done", "failed"])

    def test_missing_ids(self):
        self.m.screen(msg("streamboo", uid=""))
        self.m.screen(msg("spoiler", mid=""))
        self.run_jobs()
        self.assertEqual(self.tw.calls, [])
        self.assertEqual([e["state"] for e in self.log()], ["failed", "failed"])

    def test_unload_stops_worker(self):
        self.m.on_load()
        worker = self.m.worker
        self.m.on_unload()
        self.assertFalse(worker.is_alive())


class Unban(Base):
    def banned(self, uid="777"):
        self.m.screen(msg("streamboo", uid=uid))
        self.run_jobs()
        return self.log()[0]["id"]

    def test_unban_pardons_for_the_run(self):
        eid = self.banned()
        self.m.unban(eid)
        self.assertEqual(self.tw.calls[-1], ("unban", "777"))
        e = self.log()[0]
        self.assertEqual((e["state"], e["can_unban"]), ("unbanned", False))
        self.assertFalse(self.m.screen(msg("streamboo again", mid="m2")))     # pardoned
        self.assertTrue(self.m.screen(msg("streamboo", uid="778", mid="m3")))

    def test_not_banned_any_more_is_fine(self):
        eid = self.banned()
        self.tw.fail = [ApiError("Twitch /moderation/bans: 400 The user specified in the user_id field is not "
                                 "banned.", 400)]
        self.m.unban(eid)
        self.assertEqual(self.log()[0]["state"], "unbanned")

    def test_refused(self):
        with self.assertRaises(ApiError):
            self.m.unban("1")
        with self.assertRaises(ApiError) as e:
            self.m.unban(99)
        self.assertEqual(e.exception.status, 404)
        self.tw.is_linked = False
        self.m.screen(msg("streamboo", uid="9"))
        self.run_jobs()
        with self.assertRaises(ApiError):               # a failed ban has nothing to undo
            self.m.unban(self.log()[0]["id"])
        self.tw.is_linked = True
        self.m.screen(msg("spoiler", uid="10", mid="d"))
        self.run_jobs()
        with self.assertRaises(ApiError):               # nor has a delete
            self.m.unban(self.log()[0]["id"])

    def test_twitch_error_passes_through(self):
        eid = self.banned()
        self.tw.fail = [ApiError("Re-link Twitch", 403)]
        with self.assertRaises(ApiError):
            self.m.unban(eid)
        self.assertEqual(self.log()[0]["state"], "done")


class Panel(Base):
    def test_readiness(self):
        self.assertEqual(self.m.readiness(), {"ready": True, "detail": None})
        self.chat.connected = False
        self.assertTrue(self.m.readiness()["ready"])
        self.assertIn("Connecting", self.m.readiness()["detail"])
        self.tw.scopes = ["moderator:manage:banned_users"]
        r = self.m.readiness()
        self.assertFalse(r["ready"])
        self.assertIn("moderator:manage:chat_messages", r["detail"])
        self.tw.is_linked = False
        self.assertIn("Link Twitch", self.m.readiness()["detail"])
        self.chat.channel = ""
        self.assertIn("channel", self.m.readiness()["detail"])

    def test_state_shape(self):
        st = self.m.state()
        self.assertEqual(set(st), {"config", "status", "log", "limits"})
        self.assertEqual(set(st["status"]), {"enabled", "caught", "ready", "detail"})
        self.assertEqual(st["limits"]["matches"], ["word", "anywhere"])

    def test_test_uses_draft(self):
        r = self.m.test("Best viewers on streamboo .com")
        self.assertEqual(r["hit"], {"term": "streamboo", "match": "word", "action": "ban", "seconds": 600})
        self.assertEqual(r["seen"], "best vlewers on streamboo com")
        draft = {"enabled": False, "terms": [{"term": "viewers", "action": "delete"}]}
        self.assertEqual(self.m.test("Best viewers", draft)["hit"]["action"], "delete")   # switch off: still tested
        self.assertIsNone(self.m.test("hello", draft)["hit"])
        with self.assertRaises(ApiError) as e:
            self.m.test("x", {"terms": [{"term": "x"}]})
        self.assertEqual(e.exception.status, 400)
        self.assertTrue(e.exception.extra["errors"])
        for bad in ("", "   ", None, 5):
            with self.assertRaises(ApiError):
                self.m.test(bad)
        self.assertEqual(self.tw.calls, [])

    def test_save(self):
        new = {"enabled": True, "exempt": "subscriber", "terms": [{"term": "streamboo"}, {"term": "scam link",
                                                                                         "action": "delete"}]}
        self.m.save(new, configio.file_hash(self.path))
        with open(self.path) as f:
            saved = yaml.safe_load(f)
        self.assertEqual(saved["automod"]["exempt"], "subscriber")
        self.assertEqual([t["term"] for t in saved["automod"]["terms"]], ["streamboo", "scam link"])
        self.assertEqual(saved["brightness"], 50)
        self.assertEqual(self.m.state()["config"]["exempt"], "subscriber")
        self.assertTrue(self.m.screen(msg("scam link here")))

    def test_save_refusals(self):
        h = configio.file_hash(self.path)
        with self.assertRaises(ApiError) as e:
            self.m.save({"terms": [{"term": "x"}]}, h)
        self.assertEqual(e.exception.status, 400)
        self.assertIn("errors", e.exception.extra)
        with self.assertRaises(ApiError) as e:
            self.m.save(self.section, "stale")
        self.assertEqual(e.exception.status, 409)
        for section, base in ((None, h), ([], h), (self.section, None)):
            with self.assertRaises(ApiError):
                self.m.save(section, base)


class Routes(Base):
    def setUp(self):
        super().setUp()
        self.modules["AutoMod"] = self.m
        self.router = Router()
        automod_api.register(self.router, types.SimpleNamespace(bot=self.bot))

    def test_registered_in_editor(self):
        import modules.editor as editor
        self.assertIs(editor.automod_api, automod_api)
        self.assertIn("automod_api", inspect.getsource(editor.Editor.on_load))

    def test_routes(self):
        st = self.router.gets["/api/automod"]({})
        self.assertEqual(st["hash"], configio.file_hash(self.path))
        self.assertEqual(len(st["config"]["terms"]), 3)
        r = self.router.posts["/api/automod/test"]({"text": "streamboo"})
        self.assertEqual(r["hit"]["action"], "ban")
        st = self.router.posts["/api/automod/config"]({"config": {"enabled": False, "terms": []},
                                                       "hash": st["hash"]})
        self.assertEqual(st["config"]["terms"], [])
        self.assertNotEqual(st["hash"], configio.file_hash(self.path) + "x")
        with self.assertRaises(ApiError):
            self.router.posts["/api/automod/unban"]({"id": 1})

    def test_unban_route(self):
        self.m.screen(msg("streamboo"))
        self.run_jobs()
        st = self.router.posts["/api/automod/unban"]({"id": self.log()[0]["id"]})
        self.assertEqual(st["log"][0]["state"], "unbanned")

    def test_module_missing(self):
        del self.modules["AutoMod"]
        with self.assertRaises(ApiError) as e:
            self.router.gets["/api/automod"]({})
        self.assertEqual(e.exception.status, 503)


class ChatHook(unittest.TestCase):
    """modules/chat.py drops what the auto-mod catches, before anything else sees it."""

    def setUp(self):
        self.bot = mock.Mock()
        self.chat = Chat(self.bot)
        self.chat.channel, self.chat.stop, self.chat.connected = "somechan", threading.Event(), True

    def line(self, text):
        self.chat._handle("@display-name=Spammer;user-id=777;id=abc :spammer!spammer@spammer.tmi.twitch.tv "  # privacy-check: allow
                          f"PRIVMSG #somechan :{text}", mock.Mock())

    def test_caught_is_not_dispatched(self):
        automod = mock.Mock()
        automod.screen.side_effect = lambda m: "streamboo" in m["text"]
        self.bot.get_module.side_effect = lambda name: automod if name == "AutoMod" else None
        self.line("Best viewers on streamboo .com")
        self.bot.dispatch.assert_not_called()
        self.assertEqual(automod.screen.call_args.args[0]["user_id"], "777")
        self.line("hello")
        self.assertEqual(self.bot.dispatch.call_args.args[1]["text"], "hello")

    def test_no_automod(self):
        self.bot.get_module.return_value = None
        self.line("streamboo")
        self.assertEqual(self.bot.dispatch.call_args.args[0], "chat_message")

    def test_broken_automod_shows_the_message(self):
        automod = mock.Mock()
        automod.screen.side_effect = RuntimeError("boom")
        self.bot.get_module.return_value = automod
        self.line("streamboo")
        self.assertEqual(self.bot.dispatch.call_args.args[0], "chat_message")

    def test_real_module(self):
        bot = types.SimpleNamespace(get_module=lambda name: None, hub=None, config_path="")
        automod = AutoMod(bot)
        automod.on_config({"enabled": True, "terms": ["streamboo"]})
        self.bot.get_module.side_effect = lambda name: automod if name == "AutoMod" else None
        self.line("s t r e a m b o o dot com")
        self.bot.dispatch.assert_not_called()
        self.assertEqual(automod.state()["log"][0]["state"], "queued")


class TwitchUnban(unittest.TestCase):
    def test_unban(self):
        from modules.twitch import Twitch
        tw = Twitch.__new__(Twitch)
        tw.require = mock.Mock()
        tw.me = mock.Mock(return_value={"id": ME})
        tw._throttle = mock.Mock()
        tw.api = mock.Mock(return_value={})
        tw.unban("777")
        tw.require.assert_called_once_with("moderator:manage:banned_users")
        tw.api.assert_called_once_with("DELETE", "/moderation/bans",
                                       params={"broadcaster_id": ME, "moderator_id": ME, "user_id": "777"})
        tw.unban(778)
        for bad in ("abc", "", None, True, "1" * 30):
            with self.assertRaises(ApiError):
                tw.unban(bad)
        self.assertEqual(tw.api.call_count, 2)


class Docs(unittest.TestCase):
    def test_example_config_validates(self):
        with open(os.path.join(ROOT, "config.example.yaml")) as f:
            cfg = yaml.safe_load(f)
        section = cfg.get("automod")
        if section is not None:
            self.assertEqual(am.validate(section)[1], [])


if __name__ == "__main__":
    unittest.main()
