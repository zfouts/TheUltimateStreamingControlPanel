"""core/webapi/announce_api.py: the panel's go-live announcement routes, over the real Announce
and Discord modules with a fake Twitch and a fake HTTP post. No webhook URL passes through
these routes (they live in Settings → Discord); nothing reaches the network."""
import inspect
import json
import os
import types

from tests.test_announce import TOKEN, TOKEN2, URL, URL2, Base, Resp, dest, section

import yaml  # noqa: E402

from core import configio  # noqa: E402
from core.webapi import ApiError, Router, announce_api  # noqa: E402
import modules.announce as an  # noqa: E402
import modules.discord as dc  # noqa: E402


class Api(Base):
    section = section(dest(), dest("second", name="Second", webhook="other"))

    def setUp(self):
        super().setUp()
        self.router = Router()
        announce_api.register(self.router, types.SimpleNamespace(bot=self.bot))

    def get(self):
        return self.router.gets["/api/announce"]({})

    def post(self, path, body=None):
        return self.router.posts[path](body or {})

    def status_of(self, path, body):
        with self.assertRaises(ApiError) as e:
            self.post(path, body)
        return e.exception.status, str(e.exception)

    def save(self, config=None, base=None):
        return self.post("/api/announce/config", {"config": config or self.get()["config"],
                                                  "hash": base or configio.file_hash(self.path)})

    def save_error(self, config, **extra):
        return self.status_of("/api/announce/config", {"config": config, "hash": configio.file_hash(self.path),
                                                       **extra})

    def yaml_text(self):
        with open(self.path) as f:
            return f.read()

    def assertNoSecret(self, obj):
        text = obj if isinstance(obj, str) else json.dumps(obj)
        for secret in (TOKEN[:12], TOKEN2[:12], "123456789012345678", "223456789012345678"):
            self.assertNotIn(secret, text)


class TestRoutes(Api):
    def test_registered_in_editor(self):
        import modules.editor as editor
        self.assertIs(editor.announce_api, announce_api)
        self.assertIn("announce_api", inspect.getsource(editor.Editor.on_load))
        self.assertIn("/api/announce", self.router.gets)
        for path in ("/api/announce/config", "/api/announce/test", "/api/announce/now"):
            self.assertIn(path, self.router.posts)

    def test_editor_redacts_webhooks_in_the_activity_feed(self):
        import modules.editor as editor
        out = editor.REDACT.sub(lambda m: m.group(1) + "[redacted]", f"x {URL} y api%2Fwebhooks%2F1%2F{TOKEN}")
        self.assertNoSecret(out)

    def test_get_shape(self):
        self.write_urls({"golive": URL})
        body = self.get()
        self.assertEqual(set(body), {"config", "webhooks", "status", "placeholders", "sample", "hash"})
        self.assertEqual([d["id"] for d in body["config"]["destinations"]], ["main", "second"])
        self.assertEqual([d["webhook"] for d in body["config"]["destinations"]], ["golive", "other"])
        self.assertEqual(body["webhooks"], [{"id": "golive", "name": "Go-live", "set": True},
                                            {"id": "other", "name": "Other", "set": False},
                                            {"id": "spare", "name": "Spare", "set": False}])
        self.assertEqual(body["placeholders"], ["channel", "title", "game", "url"])
        self.assertEqual(set(body["sample"]), {"channel", "title", "game", "url"})
        self.assertEqual(body["hash"], configio.file_hash(self.path))
        self.assertEqual(set(body["status"]), {"enabled", "ready", "last", "destinations"})
        self.assertNoSecret(body)
        json.dumps(body)


class TestSave(Api):
    def test_api_has_no_urls(self):
        self.assertNotIn("urls", self.get())
        status, msg = self.save_error(self.get()["config"], urls={"main": URL})
        self.assertEqual(status, 400)
        self.assertIn("Settings → Discord", msg)
        self.assertNoSecret(msg)
        self.assertFalse(os.path.exists(os.path.join(self.state_dir, dc.SECRETS_FILE)))
        self.assertFalse(os.path.exists(os.path.join(self.state_dir, "announce_webhooks.json")))
        self.save()                                                  # {config, hash} is all it takes

    def test_api_never_returns_a_url(self):
        self.write_urls({"golive": URL, "other": URL2})
        self.http.script = [Resp(404, {"message": f"Unknown Webhook {URL}"})]
        self.post("/api/announce/test", {"id": "main"})
        self.idle()
        for body in (self.get(), self.m.snapshot()):
            self.assertNoSecret(body)
        self.assertEqual(self.get()["status"]["destinations"]["main"]["state"], "broken")

    def test_webhook_must_be_a_known_id(self):
        for hook in ("nope", "", URL, None):
            cfg = self.get()["config"]
            cfg["destinations"][0]["webhook"] = hook
            status, msg = self.save_error(cfg)
            self.assertEqual(status, 400, hook)
            self.assertTrue(msg.startswith("Fix these first: "), msg)
            self.assertIn("pick a Discord webhook, or turn the destination off" if not hook else "Settings → Discord",
                          msg)
            self.assertNoSecret(msg)
        cfg = self.get()["config"]
        cfg["destinations"][0]["webhook"] = "spare"
        body = self.save(cfg)
        self.assertEqual(body["config"]["destinations"][0]["webhook"], "spare")
        self.assertEqual(yaml.safe_load(self.yaml_text())["announce"]["destinations"][0]["webhook"], "spare")
        self.assertEqual(self.m.discord_usage(), [("spare", "Announcements: My Discord"),
                                                  ("other", "Announcements: Second")])

    def test_a_disabled_destination_needs_no_webhook(self):
        cfg = self.get()["config"]
        cfg["destinations"][1].update(webhook="", enabled=False)
        body = self.save(cfg)
        self.assertEqual(body["config"]["destinations"][1]["webhook"], "")
        self.assertEqual(yaml.safe_load(self.yaml_text())["announce"]["destinations"][1]["webhook"], "")
        self.assertEqual(self.get()["status"]["destinations"]["second"],
                         {"state": "never", "at": None, "detail": None})
        self.assertEqual(self.m.discord_usage(), [("golive", "Announcements: My Discord")])
        cfg["destinations"][1]["webhook"] = "nope"                   # an unknown id is still a mistake
        status, msg = self.save_error(cfg)
        self.assertEqual(status, 400)
        self.assertIn("webhook nope isn't one of the Discord webhooks", msg)
        cfg["destinations"][1].update(webhook="", enabled=True)      # turned on: it needs one
        status, msg = self.save_error(cfg)
        self.assertEqual((status, msg), (400, "Fix these first: destinations (second): pick a Discord webhook, "
                                              "or turn the destination off"))

    def test_type_is_dropped(self):
        cfg = self.get()["config"]
        self.assertNotIn("type", cfg["destinations"][0])
        cfg["destinations"][0]["type"] = "discord"
        self.save(cfg)
        self.assertNotIn("type", yaml.safe_load(self.yaml_text())["announce"]["destinations"][0])

    def test_stale_hash_is_409(self):
        stale = configio.file_hash(self.path)
        self.save()                                                  # someone else saved since
        cfg = self.get()["config"]
        cfg["cooldown"] = 5
        status, msg = self.status_of("/api/announce/config", {"config": cfg, "hash": stale})
        self.assertEqual(status, 409)
        self.assertIn("reload", msg)
        self.assertEqual(yaml.safe_load(self.yaml_text())["announce"]["cooldown"], 30)
        self.assertEqual(self.status_of("/api/announce/config", {"config": self.get()["config"]})[0], 400)

    def test_url_key_in_config_is_refused_and_ignored(self):
        cfg = self.get()["config"]
        cfg["destinations"][0]["url"] = URL
        status, msg = self.save_error(cfg)
        self.assertEqual(status, 400)
        self.assertIn("url is ignored", msg)
        self.assertNoSecret(msg)
        self.assertNoSecret(self.yaml_text())
        # written into config.yaml by hand: the module ignores it
        raw = yaml.safe_load(self.yaml_text())
        raw["announce"]["destinations"][0]["url"] = URL
        raw["announce"]["destinations"][1]["webhook"] = URL2
        self.m.on_config(raw["announce"])
        dests = self.get()["config"]["destinations"]
        self.assertNotIn("url", dests[0])
        self.assertEqual(dests[1]["webhook"], "")
        self.assertNoSecret(self.get())

    def test_changing_a_webhook_clears_its_result(self):
        self.write_urls({"golive": URL, "other": URL2})
        self.post("/api/announce/test", {"id": "main"})
        self.idle()
        self.assertEqual(self.get()["status"]["destinations"]["main"]["state"], "ok")
        cfg = self.get()["config"]
        cfg["destinations"][0]["webhook"] = "other"
        self.save(cfg)
        self.assertEqual(self.get()["status"]["destinations"]["main"]["state"], "never")


class TestActions(Api):
    def test_test_post_and_its_cooldown(self):
        self.assertEqual(self.status_of("/api/announce/test", {"id": "main"})[0], 409)   # no URL yet
        self.write_urls({"golive": URL})
        self.assertEqual(self.post("/api/announce/test", {"id": "main"}), {"queued": True})
        self.idle()
        self.assertEqual(self.status_of("/api/announce/test", {"id": "main"}),
                         (429, "Wait a few seconds before testing again."))
        self.mono.advance(an.TEST_COOLDOWN)
        self.assertEqual(self.post("/api/announce/test", {"id": "main"}), {"queued": True})
        self.idle()
        self.assertEqual(len(self.http.calls), 2)
        self.assertTrue(all(c.startswith("[Test] ") for c in self.http.contents()))
        self.assertEqual(self.get()["status"]["destinations"]["main"]["state"], "ok")
        self.assertEqual(self.status_of("/api/announce/test", {"id": "unknown"})[0], 404)

    def test_announce_now(self):
        self.write_urls({"golive": URL})
        self.assertEqual(self.post("/api/announce/now", {}), {"queued": True})
        self.idle()
        self.assertEqual(len(self.http.calls), 1)
        self.assertEqual(self.get()["status"]["last"]["kind"], "manual")

    def test_module_missing_is_503(self):
        router = Router()
        announce_api.register(router, types.SimpleNamespace(bot=types.SimpleNamespace(get_module=lambda n: None)))
        with self.assertRaises(ApiError) as e:
            router.gets["/api/announce"]({})
        self.assertEqual(e.exception.status, 503)


if __name__ == "__main__":
    import unittest
    unittest.main()
