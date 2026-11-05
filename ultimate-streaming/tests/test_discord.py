"""The Discord webhook registry (modules/discord.py): config, the secret store, the interface other
modules use (webhooks, webhook_url, name_of, report, is_broken), "used by" and test posts. A fake
HTTP post and injected clocks: nothing reaches the network."""
import copy
import io
import json
import logging
import os
import stat
import threading
import types
import unittest

from tests.helpers import ROOT, tmpdir
from tests.test_announce import TOKEN, TOKEN2, URL, URL2, Clock, FakeHTTP, Resp, wait_for

import yaml  # noqa: E402

from core import backups, webhooks  # noqa: E402
from core.bot import DeckBot  # noqa: E402
from core.webapi import ApiError  # noqa: E402
import modules.discord as dc  # noqa: E402

SECTION = {"webhooks": [{"id": "golive", "name": "Go-live"}, {"id": "other", "name": "Other"}]}
SECRETS = (TOKEN[:12], TOKEN2[:12], "123456789012345678", "223456789012345678")


class User:
    """A module that uses webhooks (like Announce or a plugin)."""
    config_key = "user"

    def __init__(self, usage):
        self.usage = usage

    def on_config(self, config):
        pass

    def discord_usage(self):
        if isinstance(self.usage, Exception):
            raise self.usage
        return self.usage


class Base(unittest.TestCase):
    section = SECTION

    def setUp(self):
        self.old_state = os.environ.get("DECK_STATE_DIR")
        self.state_dir = tmpdir()
        os.environ["DECK_STATE_DIR"] = self.state_dir
        self.addCleanup(self.restore_env)
        self.path = os.path.join(tmpdir(), "config.yaml")
        with open(self.path, "w") as f:
            yaml.safe_dump({"brightness": 50, "pages": {"main": []}, "discord": self.section}, f, sort_keys=False)
        self.http = FakeHTTP()
        self.wall, self.mono = Clock(1_800_000_000.0), Clock(5_000.0)
        self.d = None
        self.make()

    def restore_env(self):
        if self.old_state is None:
            os.environ.pop("DECK_STATE_DIR", None)
        else:
            os.environ["DECK_STATE_DIR"] = self.old_state

    def make(self, section=None):
        """A fresh bot + module on the same state dir (a daemon restart keeps the state files)."""
        self.stop()
        self.bot = DeckBot(root=ROOT, config_path=self.path)
        self.bot.cfg = {"discord": copy.deepcopy(self.section if section is None else section)}
        d = dc.Discord(self.bot)
        d.wall, d.mono, d.http_post = self.wall, self.mono, self.http
        self.d = d
        self.bot.add_module(d)
        self.addCleanup(self.stop)
        return d

    def stop(self):
        if self.d is not None:
            if self.http.gate is not None:
                self.http.gate.set()
            self.bot.remove_module(self.d)
            self.d = None

    def write_urls(self, urls):
        self.d.store.commit(self.d.store.stage(urls))

    def secrets_path(self):
        return os.path.join(self.state_dir, dc.SECRETS_FILE)

    def state_file(self):
        with open(os.path.join(self.state_dir, dc.STATE_FILE)) as f:
            return json.load(f)

    def idle(self):
        self.assertTrue(wait_for(lambda: not self.d.testing), "the test post never finished")

    def assertNoSecret(self, obj):
        text = obj if isinstance(obj, str) else json.dumps(obj)
        for secret in SECRETS:
            self.assertNotIn(secret, text)


# ----------------------------------------------------------------------- config

class TestConfig(unittest.TestCase):
    def test_defaults_when_absent(self):
        self.assertEqual(dc.validate(None), ({"webhooks": []}, []))
        self.assertEqual(dc.validate({}), ({"webhooks": []}, []))
        self.assertEqual(dc.validate([])[0], {"webhooks": []})

    def test_ids_and_names(self):
        cfg, errors = dc.validate({"webhooks": [
            {"id": "golive", "name": " Go-live "}, {"id": "plain"}, {"id": "Bad Id", "name": "x"},
            {"id": "golive", "name": "dup id"}, {"id": "long", "name": "n" * 41}, {"id": "same", "name": "GO-LIVE"},
            {"id": "ctl", "name": "a\x1bb"}, "nope"]})
        self.assertEqual(cfg["webhooks"], [{"id": "golive", "name": "Go-live"}, {"id": "plain", "name": "plain"},
                                           {"id": "long", "name": "long"}, {"id": "same", "name": "GO-LIVE"},
                                           {"id": "ctl", "name": "ctl"}])
        self.assertEqual(len(errors), 6, errors)
        self.assertTrue(any("already called" in e for e in errors), errors)

    def test_at_most_twenty(self):
        cfg, errors = dc.validate({"webhooks": [{"id": f"w{i}"} for i in range(25)]})
        self.assertEqual(len(cfg["webhooks"]), dc.MAX_WEBHOOKS)
        self.assertTrue(errors)

    def test_url_keys_in_config_ignored_with_a_warning(self):
        bot = types.SimpleNamespace(get_module=lambda n: None, modules={})
        old = os.environ.get("DECK_STATE_DIR")
        os.environ["DECK_STATE_DIR"] = tmpdir()
        try:
            d = dc.Discord(bot)
        finally:
            if old is None:
                os.environ.pop("DECK_STATE_DIR", None)
            else:
                os.environ["DECK_STATE_DIR"] = old
        with self.assertLogs("deck.Discord", "WARNING") as logs:
            d.on_config({"url": URL, "webhooks": [{"id": "golive", "name": "Go-live", "url": URL, "webhook": URL2,
                                                   "webhook_url": URL}]})
        self.assertEqual(d.cfg, {"webhooks": [{"id": "golive", "name": "Go-live"}]})
        out = "\n".join(logs.output)
        self.assertIn("url is ignored", out)
        self.assertNoSecretIn(out)
        self.assertIsNone(d.webhook_url("golive"))
        self.assertEqual(d.webhooks(), [{"id": "golive", "name": "Go-live", "set": False, "host": None,
                                         "state": "never", "detail": "No webhook URL saved yet."}])
        self.assertNoSecretIn(json.dumps([d.webhooks(), d.snapshot(), d.config_view(), d.urls_view()]))

    def assertNoSecretIn(self, text):
        for secret in SECRETS:
            self.assertNotIn(secret, text)


# ----------------------------------------------------------------------- secret store

class TestSecretStore(Base):
    def test_secret_file_is_0600_and_atomic(self):
        self.write_urls({"golive": URL})
        self.assertEqual(stat.S_IMODE(os.stat(self.secrets_path()).st_mode), 0o600)
        self.assertEqual(self.d.store.load(), {"golive": URL})
        self.assertEqual([f for f in os.listdir(self.state_dir) if ".tmp-" in f], [])

    def test_loose_mode_fixed_and_bad_entries_ignored(self):
        with open(self.secrets_path(), "w") as f:
            json.dump({"golive": URL, "evil": "https://evil.example/api/webhooks/1/x", "BAD": URL}, f)
        os.chmod(self.secrets_path(), 0o644)
        with self.assertLogs("deck.Discord", "WARNING") as logs:
            self.assertEqual(self.d.store.load(self.d.log), {"golive": URL})
        self.assertEqual(stat.S_IMODE(os.stat(self.secrets_path()).st_mode), 0o600)
        self.assertIn("made it private", "\n".join(logs.output))
        self.assertNoSecret("\n".join(logs.output))

    def test_corrupt_store_is_empty(self):
        with open(self.secrets_path(), "w") as f:
            f.write("{nope")
        os.chmod(self.secrets_path(), 0o600)
        self.assertEqual(self.d.store.load(), {})
        self.assertIsNone(self.d.webhook_url("golive"))

    def test_never_in_daily_backups(self):
        self.assertNotIn(dc.SECRETS_FILE, backups.DAILY_STATE_FILES)
        self.assertNotIn(dc.STATE_FILE, backups.DAILY_STATE_FILES)

    def test_logger_redacted(self):
        self.assertIn("deck.Discord", webhooks.LOGGERS)
        self.assertTrue(any(getattr(f, "redacts_webhook", False) for f in logging.getLogger("deck.Discord").filters))


# ----------------------------------------------------------------------- the interface

class TestInterface(Base):
    def test_webhook_url(self):
        self.write_urls({"golive": URL, "stray": URL2})
        self.assertEqual(self.d.webhook_url("golive"), URL)
        self.assertIsNone(self.d.webhook_url("other"))          # no URL saved
        self.assertIsNone(self.d.webhook_url("stray"))          # a URL, but not a configured webhook
        for bad in ("unknown", "", None, 5, "Bad Id", "../x"):
            self.assertIsNone(self.d.webhook_url(bad), bad)

    def test_webhook_url_none_when_broken(self):
        self.write_urls({"golive": URL})
        self.d.report("golive", webhooks.Result("broken", "Discord says this webhook doesn't exist any more"))
        self.assertIsNone(self.d.webhook_url("golive"))
        self.assertTrue(self.d.is_broken("golive"))
        self.assertFalse(self.d.is_broken("other"))
        self.assertFalse(self.d.is_broken("unknown"))

    def test_webhooks_and_name_of(self):
        self.write_urls({"golive": URL.replace("discord.com", "canary.discord.com")})
        self.assertEqual(self.d.webhooks(), [
            {"id": "golive", "name": "Go-live", "set": True, "host": "canary.discord.com", "state": "never",
             "detail": None},
            {"id": "other", "name": "Other", "set": False, "host": None, "state": "never",
             "detail": "No webhook URL saved yet."}])
        self.assertEqual((self.d.name_of("golive"), self.d.name_of("nope"), self.d.name_of(None)),
                         ("Go-live", None, None))
        self.assertNoSecret(self.d.webhooks())

    def test_report_broken_is_persisted_until_the_url_is_replaced(self):
        self.write_urls({"golive": URL})
        self.d.report("golive", webhooks.Result("broken", f"HTTP 404 {URL}", status=404))
        state = self.state_file()
        self.assertIn("golive", state["broken"])
        self.assertNoSecret(state)
        self.assertEqual(self.d.snapshot()["webhooks"]["golive"]["state"], "broken")
        self.assertNoSecret(self.d.snapshot())
        self.make()                                             # restart
        self.assertTrue(self.d.is_broken("golive"))
        self.assertIsNone(self.d.webhook_url("golive"))
        self.write_urls({"golive": URL2})                       # replaced
        self.assertFalse(self.d.is_broken("golive"))
        self.assertEqual(self.d.webhook_url("golive"), URL2)

    def test_report_ok_and_errors(self):
        self.write_urls({"golive": URL})
        self.d.report("golive", webhooks.Result("failed", "Discord rejected the post (HTTP 400)."))
        self.assertEqual(self.d.snapshot()["webhooks"]["golive"],
                         {"state": "error", "at": self.wall.t, "detail": "Discord rejected the post (HTTP 400)."})
        self.d.report("golive", webhooks.Result("broken", "gone"))
        self.wall.advance(5)
        self.d.report("golive", webhooks.Result("ok"))          # e.g. a working test: no longer broken
        self.assertFalse(self.d.is_broken("golive"))
        self.assertEqual(self.state_file()["broken"], {})
        self.assertEqual(self.d.snapshot()["webhooks"]["golive"], {"state": "ok", "at": self.wall.t, "detail": None})

    def test_report_for_a_replaced_url_is_ignored(self):
        self.write_urls({"golive": URL2})
        self.d.report("golive", webhooks.Result("broken", "gone"), url=URL)
        self.assertFalse(self.d.is_broken("golive"))
        self.d.report("golive", webhooks.Result("broken", "gone"), url=URL2)
        self.assertTrue(self.d.is_broken("golive"))

    def test_report_ignores_nonsense(self):
        self.write_urls({"golive": URL})
        for wid, result in (("unknown", webhooks.Result("broken")), ("golive", None), ("golive", "broken"),
                            (None, webhooks.Result("ok")), ("golive", types.SimpleNamespace(kind="weird"))):
            self.d.report(wid, result)
        self.assertEqual(self.d.snapshot()["webhooks"]["golive"]["state"], "never")
        self.assertFalse(os.path.exists(os.path.join(self.state_dir, dc.STATE_FILE)))

    def test_removed_from_config_forgets_status(self):
        self.write_urls({"golive": URL})
        self.d.report("golive", webhooks.Result("ok"))
        self.d.on_config({"webhooks": [{"id": "other"}]})
        self.assertEqual(self.d.results, {})
        self.assertIsNone(self.d.webhook_url("golive"))

    def test_usage_collected_from_modules(self):
        self.bot.modules["A"] = User([("golive", "Announcements: My server"), ("golive", "Announcements: My server"),
                                      ("other", f"Bambu {URL}"), ("BAD", "x"), ("golive",), "junk", ("other", 5)])
        self.bot.modules["B"] = User(RuntimeError(f"boom {URL}"))
        self.bot.modules["C"] = User(None)
        usage = self.d.usage()
        self.assertEqual(usage["golive"], ["Announcements: My server"])
        self.assertEqual(usage["other"][1], "User")
        self.assertNotIn("BAD", usage)
        self.assertNoSecret(usage)
        self.assertEqual(self.d.used_by(), {"golive": ["Announcements: My server"], "other": usage["other"]})


# ----------------------------------------------------------------------- test posts

class TestTestPost(Base):
    def setUp(self):
        super().setUp()
        self.write_urls({"golive": URL})

    def test_posts_the_test_message_without_mentions(self):
        self.assertEqual(self.d.test("golive"), {"queued": True})
        self.idle()
        self.assertEqual(len(self.http.calls), 1)
        call = self.http.calls[0]
        self.assertEqual(call["url"], URL + "?wait=true")
        self.assertEqual(call["json"], {"content": "Test message from Ultimate Streaming ✓",
                                        "allowed_mentions": {"parse": []}})
        self.assertFalse(call["allow_redirects"])
        self.assertEqual(call["timeout"], webhooks.TIMEOUT)
        self.assertEqual(self.d.snapshot()["webhooks"]["golive"],
                         {"state": "ok", "at": self.wall.t, "detail": "Test message sent."})

    def test_cooldown_and_one_in_flight(self):
        self.http.gate = threading.Event()
        self.d.test("golive")
        self.assertTrue(self.http.entered.wait(2))
        self.assertEqual(self.d.snapshot()["webhooks"]["golive"]["state"], "sending")
        self.mono.advance(60)                 # past the cooldown, but the first is still in flight
        with self.assertRaises(ApiError) as e:
            self.d.test("golive")
        self.assertEqual(e.exception.status, 429)
        self.http.gate.set()
        self.idle()
        self.mono.advance(-55)                # 5 s after the last request
        with self.assertRaises(ApiError) as e:
            self.d.test("golive")
        self.assertEqual((e.exception.status, str(e.exception)), (429, "Wait a few seconds before testing again."))
        self.mono.advance(dc.TEST_COOLDOWN)
        self.assertEqual(self.d.test("golive"), {"queued": True})
        self.idle()
        self.assertEqual(len(self.http.calls), 2)

    def test_errors(self):
        for bad in (None, 5, "BAD ID"):
            with self.assertRaises(ApiError) as e:
                self.d.test(bad)
            self.assertEqual(e.exception.status, 400)
        with self.assertRaises(ApiError) as e:
            self.d.test("unknown")
        self.assertEqual(e.exception.status, 404)
        with self.assertRaises(ApiError) as e:
            self.d.test("other")              # no URL saved
        self.assertEqual(e.exception.status, 409)
        self.assertEqual(self.http.calls, [])

    def test_404_marks_broken_and_a_later_success_clears_it(self):
        self.http.script = [Resp(404, {"message": f"Unknown Webhook {URL}"})]
        self.d.test("golive")
        self.idle()
        self.assertTrue(self.d.is_broken("golive"))
        snap = self.d.snapshot()["webhooks"]["golive"]
        self.assertEqual(snap["state"], "broken")
        self.assertIn("HTTP 404", snap["detail"])
        self.assertNoSecret(snap)
        self.mono.advance(dc.TEST_COOLDOWN)
        self.d.test("golive")                 # a broken webhook can still be tested
        self.idle()
        self.assertFalse(self.d.is_broken("golive"))

    def test_network_error_never_logs_the_url(self):
        import requests
        self.http.script = [requests.ConnectionError(
            f"HTTPSConnectionPool(host='discord.com', port=443): Max retries exceeded with url: "
            f"/api/webhooks/123456789012345678/{TOKEN}?wait=true")]
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(name)s %(message)s"))
        loggers = [logging.getLogger(n) for n in ("deck.Discord", "core.webhooks")]
        for lg in loggers:
            lg.addHandler(handler)
            lg.setLevel(logging.DEBUG)
            lg.propagate = False
        try:
            self.d.test("golive")
            self.idle()
        finally:
            for lg in loggers:
                lg.removeHandler(handler)
                lg.setLevel(logging.NOTSET)
                lg.propagate = True
        self.assertIn("ConnectionError", stream.getvalue())
        self.assertEqual(self.d.snapshot()["webhooks"]["golive"]["state"], "error")
        for text in (stream.getvalue(), json.dumps(self.d.snapshot()), json.dumps(self.d.webhooks())):
            self.assertNoSecret(text)

    def test_url_removed_before_the_test_ran(self):
        self.d.testing.add("golive")          # queued by test(), then the URL went before the worker ran
        self.write_urls({})
        self.d._run_test("golive")
        self.assertEqual(self.http.calls, [])
        self.assertEqual(self.d.testing, set())
        self.assertEqual(self.d.results["golive"]["state"], "error")

if __name__ == "__main__":
    unittest.main()
