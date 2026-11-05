"""core/webapi/discord_api.py: the panel's Settings → Discord routes, over the real Discord module
(and the real Announce module for "used by") with a fake HTTP post. Webhook URLs only ever go
in; nothing reaches the network."""
import inspect
import json
import os
import stat
import types

from tests.test_announce import TOKEN, URL, URL2, Resp, dest, section
from tests.test_discord import Base, User

import yaml  # noqa: E402

from core import configio  # noqa: E402
from core.webapi import ApiError, Router, discord_api  # noqa: E402
import modules.announce as an  # noqa: E402
import modules.discord as dc  # noqa: E402


class Api(Base):
    def setUp(self):
        super().setUp()
        self.router = Router()
        discord_api.register(self.router, types.SimpleNamespace(bot=self.bot))

    def get(self):
        return self.router.gets["/api/discord"]({})

    def post(self, path, body=None):
        return self.router.posts[path](body or {})

    def status_of(self, path, body):
        with self.assertRaises(ApiError) as e:
            self.post(path, body)
        return e.exception.status, str(e.exception)

    def save(self, urls=None, config=None, base=None):
        return self.post("/api/discord/config", {"config": config or self.get()["config"], "urls": urls or {},
                                                 "hash": base or configio.file_hash(self.path)})

    def save_error(self, urls=None, config=None):
        return self.status_of("/api/discord/config", {"config": config or self.get()["config"], "urls": urls or {},
                                                      "hash": configio.file_hash(self.path)})

    def yaml_text(self):
        with open(self.path) as f:
            return f.read()

    def without(self, wid):
        cfg = self.get()["config"]
        cfg["webhooks"] = [w for w in cfg["webhooks"] if w["id"] != wid]
        return cfg


class TestRoutes(Api):
    def test_registered_in_editor(self):
        import modules.editor as editor
        self.assertIs(editor.discord_api, discord_api)
        self.assertIn("discord_api", inspect.getsource(editor.Editor.on_load))
        self.assertIn("/api/discord", self.router.gets)
        for path in ("/api/discord/config", "/api/discord/test"):
            self.assertIn(path, self.router.posts)

    def test_get_shape(self):
        body = self.get()
        self.assertEqual(set(body), {"config", "urls", "status", "used_by", "hash"})
        self.assertEqual(body["config"], {"webhooks": [{"id": "golive", "name": "Go-live"},
                                                       {"id": "other", "name": "Other"}]})
        self.assertEqual(body["urls"], {"golive": {"set": False, "host": None}, "other": {"set": False, "host": None}})
        self.assertEqual(set(body["status"]), {"golive", "other"})
        self.assertEqual(set(body["status"]["golive"]), {"state", "at", "detail"})
        self.assertEqual(body["used_by"], {"golive": [], "other": []})
        self.assertEqual(body["hash"], configio.file_hash(self.path))
        json.dumps(body)

    def test_module_missing_is_503(self):
        router = Router()
        discord_api.register(router, types.SimpleNamespace(bot=types.SimpleNamespace(get_module=lambda n: None)))
        with self.assertRaises(ApiError) as e:
            router.gets["/api/discord"]({})
        self.assertEqual(e.exception.status, 503)


class TestSave(Api):
    def test_secret_file_is_0600_and_config_yaml_has_no_url(self):
        body = self.save({"golive": URL})
        self.assertEqual(stat.S_IMODE(os.stat(self.secrets_path()).st_mode), 0o600)
        with open(self.secrets_path()) as f:
            self.assertEqual(json.load(f), {"golive": URL})
        self.assertNoSecret(self.yaml_text())
        self.assertEqual(yaml.safe_load(self.yaml_text())["discord"], {"webhooks": [
            {"id": "golive", "name": "Go-live"}, {"id": "other", "name": "Other"}]})
        self.assertEqual([f for f in os.listdir(self.state_dir) if ".tmp-" in f], [])
        self.assertEqual(body["urls"]["golive"], {"set": True, "host": "discord.com"})

    def test_api_never_returns_a_url(self):
        body = self.save({"golive": URL, "other": URL2.replace("discord.com", "canary.discord.com")})
        self.assertEqual(body["urls"], {"golive": {"set": True, "host": "discord.com"},
                                        "other": {"set": True, "host": "canary.discord.com"}})
        self.http.script = [Resp(404, {"message": f"Unknown Webhook {URL}"})]
        self.post("/api/discord/test", {"id": "golive"})
        self.idle()
        for body in (body, self.get(), self.d.snapshot(), self.d.webhooks()):
            self.assertNoSecret(body)
        self.assertEqual(self.get()["status"]["golive"]["state"], "broken")

    def test_stale_hash_is_409_and_nothing_is_stored(self):
        stale = configio.file_hash(self.path)
        self.save({"golive": URL})                                # someone else saved since
        status, msg = self.status_of("/api/discord/config",
                                     {"config": self.get()["config"], "urls": {"golive": URL2}, "hash": stale})
        self.assertEqual(status, 409)
        self.assertIn("reload", msg)
        self.assertNoSecret(msg)
        self.assertEqual(self.d.store.load(), {"golive": URL})    # the staged secret was discarded
        self.assertEqual([f for f in os.listdir(self.state_dir) if ".tmp-" in f], [])
        self.assertEqual(self.status_of("/api/discord/config", {"config": self.get()["config"], "urls": {}})[0], 400)

    def test_url_key_in_config_is_refused_and_ignored(self):
        cfg = self.get()["config"]
        cfg["webhooks"][0]["url"] = URL
        status, msg = self.save_error(config=cfg)
        self.assertEqual(status, 400)
        self.assertIn("url is ignored", msg)
        self.assertNoSecret(msg)
        self.assertNoSecret(self.yaml_text())
        # written into config.yaml by hand: the module ignores it
        raw = yaml.safe_load(self.yaml_text())
        raw["discord"]["webhooks"][0]["url"] = URL
        raw["discord"]["webhook"] = URL
        self.d.on_config(raw["discord"])
        self.assertNotIn("url", self.get()["config"]["webhooks"][0])
        self.assertEqual(self.get()["urls"]["golive"], {"set": False, "host": None})
        self.assertIsNone(self.d.webhook_url("golive"))

    def test_bad_urls_are_400_without_echo(self):
        for urls in ({"golive": "https://evil.example/api/webhooks/1/" + TOKEN}, {"golive": URL.replace("https", "http")},
                     {"golive": URL + "?x=1"}, {"golive": URL.replace("discord.com", "u@discord.com")},  # privacy-check: allow
                     {"nope": URL}, {URL: URL}, {"golive": 5}):
            status, msg = self.save_error(urls)
            self.assertEqual(status, 400, urls)
            self.assertTrue(msg.startswith("Fix these first: "), msg)
            self.assertNoSecret(msg)
        self.assertFalse(os.path.exists(self.secrets_path()))

    def test_bad_config_is_400(self):
        for cfg in ({"webhooks": [{"id": "a", "name": "Same"}, {"id": "b", "name": "same"}]},
                    {"webhooks": [{"id": "Bad Id"}]}, {"webhooks": [{"id": f"w{i}"} for i in range(21)]}):
            self.assertEqual(self.save_error(config=cfg)[0], 400, cfg)
        for body in ({"config": None, "hash": "x"}, {"config": {}, "urls": [], "hash": "x"}, {"config": {}}):
            self.assertEqual(self.status_of("/api/discord/config", body)[0], 400, body)

    def test_absent_keeps_null_removes_and_removed_webhook_loses_its_url(self):
        self.save({"golive": URL, "other": URL2})
        self.save({})                                             # absent: kept
        self.assertEqual(self.d.store.load(), {"golive": URL, "other": URL2})
        self.save({"other": None})                                # null: removed
        self.assertEqual(self.d.store.load(), {"golive": URL})
        self.save({"other": URL2})
        body = self.save({}, config=self.without("golive"))       # webhook deleted: its URL goes too
        self.assertEqual(self.d.store.load(), {"other": URL2})
        self.assertEqual(list(body["urls"]), ["other"])
        self.assertEqual([w["id"] for w in yaml.safe_load(self.yaml_text())["discord"]["webhooks"]], ["other"])

    def test_add_and_rename(self):
        cfg = self.get()["config"]
        cfg["webhooks"][0]["name"] = "Announcements"
        cfg["webhooks"].append({"id": "printer", "name": "Printer"})
        body = self.save({"printer": URL2}, config=cfg)
        self.assertEqual([w["name"] for w in body["config"]["webhooks"]], ["Announcements", "Other", "Printer"])
        self.assertEqual(self.d.name_of("golive"), "Announcements")
        self.assertEqual(self.d.webhook_url("printer"), URL2)

    def break_golive(self):
        self.http.script = [Resp(404)]
        self.post("/api/discord/test", {"id": "golive"})
        self.idle()
        self.assertEqual(self.get()["status"]["golive"]["state"], "broken")
        self.assertIn("golive", self.state_file()["broken"])
        self.mono.advance(dc.TEST_COOLDOWN)

    def test_replacing_a_url_clears_broken(self):
        self.save({"golive": URL})
        self.break_golive()
        self.save({})                                             # not submitted: still broken
        self.assertEqual(self.get()["status"]["golive"]["state"], "broken")
        self.save({"golive": URL2})
        self.assertEqual(self.get()["status"]["golive"]["state"], "never")
        self.assertEqual(self.state_file()["broken"], {})
        self.assertEqual(self.d.webhook_url("golive"), URL2)

    def test_submitting_the_same_url_again_clears_broken(self):
        self.save({"golive": URL})
        self.break_golive()
        self.save({"golive": URL})                                # "it's fine now"
        self.assertFalse(self.d.is_broken("golive"))
        self.assertEqual(self.get()["status"]["golive"]["state"], "never")
        self.assertEqual(self.state_file()["broken"], {})
        self.assertEqual(self.d.webhook_url("golive"), URL)

    def test_removing_a_broken_url_is_accepted(self):
        self.save({"golive": URL})
        self.break_golive()
        self.save({"golive": None})
        self.assertIsNone(self.d.webhook_url("golive"))
        self.assertFalse(self.d.is_broken("golive"))
        self.assertEqual(self.state_file()["broken"], {})
        self.assertEqual(self.get()["status"]["golive"]["state"], "never")

    def test_a_broken_webhook_can_be_tested_and_a_success_clears_it(self):
        self.save({"golive": URL})
        self.break_golive()
        self.assertEqual(self.post("/api/discord/test", {"id": "golive"}), {"queued": True})
        self.idle()
        self.assertEqual(len(self.http.calls), 2)
        self.assertFalse(self.d.is_broken("golive"))
        self.assertEqual(self.state_file()["broken"], {})
        self.assertEqual(self.get()["status"]["golive"]["state"], "ok")
        self.assertEqual(self.d.webhook_url("golive"), URL)


class TestUsedBy(Api):
    def setUp(self):
        super().setUp()
        self.ann = an.Announce(self.bot)
        self.ann.on_config(section(dest(name="My server"), dest("two", name="Second server", enabled=False)))
        self.bot.modules["Announce"] = self.ann
        self.bot.modules["Bambu"] = User([("other", "Bambu Printer: snapshots")])

    def test_used_by_populated(self):
        self.assertEqual(self.get()["used_by"], {"golive": ["Announcements: My server", "Announcements: Second server"],
                                                 "other": ["Bambu Printer: snapshots"]})

    def test_deleting_a_webhook_in_use_is_400_naming_the_users(self):
        self.save({"golive": URL, "other": URL2})
        status, msg = self.save_error(config=self.without("golive"))
        self.assertEqual(status, 400)
        self.assertEqual(msg, "Go-live is used by: Announcements: My server, Announcements: Second server; "
                              "pick another webhook there first")
        self.assertEqual(self.d.store.load(), {"golive": URL, "other": URL2})       # nothing changed
        self.assertEqual(len(yaml.safe_load(self.yaml_text())["discord"]["webhooks"]), 2)
        status, msg = self.save_error(config={"webhooks": []})
        self.assertEqual(status, 400)
        self.assertIn("Go-live is used by", msg)
        self.assertIn("Other is used by: Bambu Printer: snapshots", msg)
        # once nothing uses it, it can go
        self.ann.on_config(section(dest(webhook="other")))
        self.bot.modules.pop("Bambu")
        body = self.save(config=self.without("golive"))
        self.assertEqual(list(body["urls"]), ["other"])
        self.assertEqual(body["used_by"], {"other": ["Announcements: My Discord"]})

    def test_removing_only_the_url_of_a_used_webhook_is_allowed(self):
        self.save({"golive": URL})
        self.save({"golive": None})
        self.assertIsNone(self.d.webhook_url("golive"))
        self.assertEqual(self.ann.snapshot()["destinations"]["main"]["detail"],
                         "The Go-live webhook has no URL yet; add it in Settings → Discord")


class TestTest(Api):
    def test_test_route_and_cooldown(self):
        self.assertEqual(self.status_of("/api/discord/test", {"id": "golive"})[0], 409)   # no URL yet
        self.save({"golive": URL})
        self.assertEqual(self.post("/api/discord/test", {"id": "golive"}), {"queued": True})
        self.idle()
        self.assertEqual(self.status_of("/api/discord/test", {"id": "golive"}),
                         (429, "Wait a few seconds before testing again."))
        self.mono.advance(dc.TEST_COOLDOWN)
        self.assertEqual(self.post("/api/discord/test", {"id": "golive"}), {"queued": True})
        self.idle()
        self.assertEqual(len(self.http.calls), 2)
        self.assertEqual(self.get()["status"]["golive"]["state"], "ok")
        self.assertEqual(self.status_of("/api/discord/test", {"id": "unknown"})[0], 404)
        self.assertEqual(self.status_of("/api/discord/test", {})[0], 400)


if __name__ == "__main__":
    import unittest
    unittest.main()
