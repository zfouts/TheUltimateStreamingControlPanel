"""Plugin pages (core/plugin_page.py, core/webapi/plugin_api.py): a plugin module describes its
control-panel page with panel_page(); the panel lists every installed plugin in /api/status,
serves the page, saves its settings into config.yaml (secrets into private files, never echoed)
and runs its buttons."""
from tests import helpers  # noqa: F401

import copy
import json
import os
import shutil
import sys
import textwrap
import types
import unittest
from unittest import mock

from tests.helpers import ROOT, tmpdir

import yaml  # noqa: E402

from core import configio, paths, plugin_page, plugins  # noqa: E402
from core.bot import DeckBot  # noqa: E402
from core.module import Module  # noqa: E402
from core.webapi import ApiError, Router, plugin_api  # noqa: E402

SECRET = "hunter2-very-secret"  # privacy-check: allow

MANIFEST = "name: {name}\nversion: '1.2.3'\ndescription: {name} test plugin\napi: {api}\n"

GADGET = '''
from core import Module
from core.webapi import ApiError

STATE = {"configs": [], "calls": [], "page": None}

FIELDS = [
    {"key": "host", "label": "Host", "type": "text", "group": "Device", "required": True},
    {"key": "net.port", "label": "Port", "type": "number", "group": "Device",
     "min": 1024, "max": 65535, "default": 5004},
    {"key": "loud", "label": "Loud", "type": "bool", "group": "Device"},
    {"key": "mode", "label": "Mode", "type": "select", "options": [["a", "A"], ["b", "B"]]},
    {"key": "token", "label": "Token", "type": "secret", "file": "gadget_token",
     "config_key": "token_file", "env": "GADGET_TOKEN"},
]


class Gadget(Module):
    def on_config(self, config):
        super().on_config(config)
        STATE["configs"].append(dict(config))

    def panel_page(self):
        if STATE["page"] is not None:
            return STATE["page"]
        return {"title": "Gadget", "icon": "dial", "intro": "A test gadget.",
                "actions": [{"id": "go", "label": "Go", "tone": "primary"},
                            {"id": "off", "label": "Off", "enabled": False, "hint": "Never"},
                            {"id": "boom", "label": "Boom"}, {"id": "nope", "label": "Nope"},
                            {"id": "ghost", "label": "Ghost"}],
                "fields": FIELDS}

    def panel_action(self, action_id):
        STATE["calls"].append(action_id)
        if action_id == "go":
            return {"message": "went"}
        if action_id == "boom":
            raise RuntimeError("it broke")
        if action_id == "nope":
            raise ApiError("not now", 409)
        return super().panel_action(action_id)


def setup(bot):
    bot.add_module(Gadget(bot))
'''

CRASHY = '''
def setup(bot):
    raise RuntimeError("no luck")
'''

PLAIN = '''
from core import Module


class Plain(Module):
    pass


def setup(bot):
    bot.add_module(Plain(bot))
'''


def page(**over):
    base = {"title": "Thing", "icon": "dial", "intro": "Hi.",
            "actions": [{"id": "go", "label": "Go"}],
            "fields": [{"key": "host", "label": "Host", "type": "text"},
                       {"key": "cam.port", "label": "Port", "type": "number", "min": 1, "max": 10,
                        "default": 5},
                       {"key": "on", "label": "On", "type": "bool", "default": True},
                       {"key": "mode", "label": "Mode", "type": "select", "options": [["a", "A"], [2, "Two"]],
                        "default": 2},
                       {"key": "code", "label": "Code", "type": "secret", "file": "thing_code",
                        "config_key": "code_file", "required": True}]}
    base.update(over)
    return base


class NormalisePage(unittest.TestCase):
    def test_valid_page_has_every_key(self):
        p, problem = plugin_page.check_page(page())
        self.assertIsNone(problem)
        self.assertEqual(p["actions"], [{"id": "go", "label": "Go", "icon": None, "tone": "default",
                                         "confirm": None, "enabled": True, "hint": None}])
        self.assertEqual(set(p), {"title", "icon", "intro", "actions", "fields"})
        for f in p["fields"]:
            self.assertEqual(tuple(f), plugin_page.FIELD_OUT)
        text, number, flag, select, secret = p["fields"]
        self.assertEqual((text["default"], text["max_length"], text["required"], text["group"]),
                         ("", 200, False, "Settings"))
        self.assertEqual((number["default"], number["whole"]), (5, True))
        self.assertEqual((flag["default"], select["default"]), (True, 2))
        self.assertEqual((secret["default"], secret["file"], secret["config_key"], secret["required"]),
                         (None, "thing_code", "code_file", True))
        self.assertIsNone(plugin_page.check_page(None)[0])

    def test_unknown_icons_fall_back(self):
        p, _ = plugin_page.check_page(page(icon="nope", actions=[{"id": "a", "label": "A", "icon": "zzz"}]))
        self.assertEqual(p["icon"], "bolt")
        self.assertIsNone(p["actions"][0]["icon"])

    def test_invalid_pages(self):
        f = page()["fields"]
        bad = {
            "unknown keys": page(extra=1),
            "title": page(title="x" * 25),
            "id must": page(actions=[{"id": "Go", "label": "Go"}]),
            "same id": page(actions=[{"id": "go", "label": "A"}, {"id": "go", "label": "B"}]),
            "at most 12": page(actions=[{"id": f"a{i}", "label": "A"} for i in range(13)]),
            "tone": page(actions=[{"id": "go", "label": "Go", "tone": "loud"}]),
            "label": page(fields=[{"key": "a", "label": "x" * 41, "type": "text"}]),
            "type must": page(fields=[{"key": "a", "label": "A", "type": "color"}]),
            "unknown keys for a bool": page(fields=[{"key": "a", "label": "A", "type": "bool", "min": 1}]),
            "default must be at most 3": page(fields=[{"key": "a", "label": "A", "type": "number", "max": 3,
                                                  "default": 4}]),
            "unknown keys for a secret": page(fields=[dict(f[4], default="x")]),
            "file must": page(fields=[dict(f[4], file="..")]),
            "dot-separated": page(fields=[{"key": "a.b.c.d", "label": "A", "type": "text"}]),
            "overlap": page(fields=[{"key": "cam", "label": "A", "type": "text"}, f[1]]),
            "same key": page(fields=[f[0], f[0]]),
            "options": page(fields=[{"key": "m", "label": "M", "type": "select", "options": []}]),
            "max_length": page(fields=[{"key": "a", "label": "A", "type": "text", "max_length": 501}]),
            "env must": page(fields=[dict(f[4], env="lower")]),
            "one line": page(intro="two\nlines"),
            "must return a dict": ["not", "a", "page"],
        }
        for why, spec in bad.items():
            with self.subTest(why):
                p, problem = plugin_page.check_page(spec)
                self.assertIsNone(p)
                self.assertIn(why, problem)
        # Secret config_key overlapping a field's key.
        _, problem = plugin_page.check_page(page(fields=[f[0], dict(f[4], config_key="host")]))
        self.assertIn("overlap", problem)

    def test_reasons_are_logged_once_per_plugin(self):
        with mock.patch.object(plugin_page.log, "warning") as warn:
            for _ in range(3):
                self.assertIsNone(plugin_page.normalise_page(page(title=""), who="plugin x"))
            self.assertEqual(warn.call_count, 1)
            plugin_page.normalise_page(page(title=""), who="plugin y")
            self.assertEqual(warn.call_count, 2)
            self.assertIsNotNone(plugin_page.normalise_page(page(), who="plugin x"))   # fixed: forgotten
            plugin_page.normalise_page(page(title=""), who="plugin x")
            self.assertEqual(warn.call_count, 3)


FIELD_HOST = {"key": "host", "label": "Host", "type": "text"}


class FakeDiscord:
    """Exactly the registry methods plugins may use (contract section 2)."""
    def __init__(self, ids=("golive", "alerts")):
        self.hooks = [{"id": i, "name": i.title(), "set": i == "golive", "host": None, "state": "never",
                       "detail": None} for i in ids]

    def webhooks(self):
        return [dict(h) for h in self.hooks]


HOOK_FIELD = {"key": "discord.webhook", "label": "Discord webhook", "type": "discord_webhook",
              "group": "Discord"}


class DiscordWebhookField(unittest.TestCase):
    def setUp(self):
        self.fields = plugin_page.check_page(page(fields=[HOOK_FIELD]))[0]["fields"]
        self.required = plugin_page.check_page(page(fields=[dict(HOOK_FIELD, required=True)]))[0]["fields"]

    def test_normalised(self):
        f = self.fields[0]
        self.assertEqual((f["type"], f["default"], f["required"]), ("discord_webhook", "", False))
        self.assertTrue(self.required[0]["required"])
        _page, why = plugin_page.check_page(page(fields=[dict(HOOK_FIELD, options=[["a", "A"]])]))
        self.assertIn("unknown keys", why)

    def test_apply_accepts_empty_or_a_known_id(self):
        section = {}
        self.assertEqual(plugin_page.apply_settings(self.fields, section, {"discord.webhook": "golive"}, {},
                                                    ["golive"]), [])
        self.assertEqual(section, {"discord": {"webhook": "golive"}})
        self.assertEqual(plugin_page.apply_settings(self.fields, section, {"discord.webhook": ""}, {},
                                                    ["golive"]), [])
        self.assertEqual(section["discord"]["webhook"], "")

    def test_apply_rejects_unknown_and_wrong_types(self):
        for bad in ("nope", 5, None, "https://discord.com/api/webhooks/1/x"):
            errors = plugin_page.apply_settings(self.fields, {}, {"discord.webhook": bad}, {}, ["golive"])
            self.assertEqual(len(errors), 1, bad)
            self.assertTrue(errors[0].startswith("Discord webhook: must be"), errors)
        # No registry at all: nothing but "" goes in.
        self.assertEqual(len(plugin_page.apply_settings(self.fields, {}, {"discord.webhook": "golive"}, {})), 1)

    def test_required_means_non_empty(self):
        self.assertEqual(plugin_page.apply_settings(self.required, {}, {"discord.webhook": ""}, {}, ["golive"]),
                         ["Discord webhook: is required"])

    def test_values_fall_back_to_none_for_an_unknown_id(self):
        section = {"discord": {"webhook": "gone"}}
        self.assertEqual(plugin_page.field_values(self.fields, section, ["golive"]), {"discord.webhook": ""})
        self.assertEqual(plugin_page.field_values(self.fields, {"discord": {"webhook": "golive"}}, ["golive"]),
                         {"discord.webhook": "golive"})


class Values(unittest.TestCase):
    def setUp(self):
        self.fields = plugin_page.check_page(page())[0]["fields"]
        self.env = mock.patch.dict(os.environ)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_field_values_use_config_or_defaults(self):
        values = plugin_page.field_values(self.fields, {"host": " box ", "cam": {"port": 99}, "on": "yes"})
        self.assertEqual(values, {"host": "box", "cam.port": 5, "on": True, "mode": 2})
        self.assertEqual(plugin_page.field_values(self.fields, None)["host"], "")
        self.assertNotIn("code", values)

    def test_secret_status(self):
        folder = tmpdir()
        full, empty = os.path.join(folder, "full"), os.path.join(folder, "empty")
        with open(full, "w") as f:
            f.write(SECRET + "\n")
        with open(empty, "w") as f:
            f.write("\n")
        fields = plugin_page.check_page(page(fields=[dict(page()["fields"][4], env="THING_CODE")]))[0]["fields"]
        status = plugin_page.secret_status
        self.assertEqual(status(fields, {}), {"code": {"set": False, "source": None}})
        self.assertEqual(status(fields, {"code_file": full}), {"code": {"set": True, "source": "file"}})
        self.assertEqual(status(fields, {"code_file": empty})["code"]["set"], False)
        self.assertEqual(status(fields, {"code_file": "/nonexistent/x"})["code"]["set"], False)
        self.assertEqual(status(fields, {}, environ={"THING_CODE": "x"}), {"code": {"set": True, "source": "env"}})
        self.assertNotIn(SECRET, json.dumps(status(fields, {"code_file": full})))

    def test_apply_settings(self):
        section = {"mode": 2, "cam": {"other": 1}}
        errors = plugin_page.apply_settings(self.fields, section,
                                            {"host": " box ", "cam.port": 5, "on": True, "mode": 2},
                                            {"code": SECRET})
        self.assertEqual(errors, [])
        # cam.port and on equal their defaults and weren't in the section: not written; mode was.
        self.assertEqual(section, {"mode": 2, "cam": {"other": 1}, "host": "box",
                                   "code_file": plugin_page.secret_path(self.fields[4])})
        errors = plugin_page.apply_settings(self.fields, section, {"cam.port": 7.0}, {})
        self.assertEqual((errors, section["cam"]["port"]), ([], 7))
        self.assertIsInstance(section["cam"]["port"], int)

    def test_apply_settings_reports_every_problem(self):
        section = {"cam": "oops"}
        before = copy.deepcopy(section)
        errors = plugin_page.apply_settings(
            self.fields, section,
            {"host": "a\x07b", "cam.port": 4.5, "on": 1, "mode": "c", "bogus": 1, "code": "x"},
            {"code": None, "host": "x", "nope": "y"})
        self.assertEqual(errors, [
            "Host: must be one line of text without control characters",
            "Port: must be a whole number", "On: must be true or false", "Mode: must be one of the listed choices",
            "bogus: not a setting of this plugin", "Code: is a secret; send it in secrets",
            "Code: is required", "host: not a secret of this plugin", "nope: not a secret of this plugin"])
        self.assertEqual(section, before)
        # Writing under something that isn't a mapping is refused, never overwritten.
        errors = plugin_page.apply_settings(self.fields, section, {"cam.port": 3}, {})
        self.assertIn("other than a mapping", errors[0])
        self.assertEqual(section, before)
        self.assertEqual(plugin_page.apply_settings(self.fields, {}, [], {}), ["values must be an object"])

    def test_secret_problems_never_quote_the_value(self):
        for bad in ("", "   ", "a\nb", "x" * 501, 5):
            errors = plugin_page.apply_settings(self.fields, {}, {}, {"code": bad})
            self.assertEqual(len(errors), 1)
            if isinstance(bad, str) and bad.strip():
                self.assertNotIn(bad.strip(), errors[0])

    def test_required_text_and_dotted_helpers(self):
        fields = plugin_page.check_page(page(fields=[{"key": "host", "label": "Host", "type": "text",
                                                      "required": True}]))[0]["fields"]
        self.assertEqual(plugin_page.apply_settings(fields, {}, {"host": "  "}, {}), ["Host: is required"])
        d = {}
        plugin_page.set_path(d, "a.b.c", 1)
        self.assertEqual(plugin_page.get_path(d, "a.b.c"), 1)
        self.assertIs(plugin_page.get_path(d, "a.x"), plugin_page.MISSING)
        plugin_page.del_path(d, "a.b.c")
        plugin_page.del_path(d, "q.r")
        self.assertEqual(d, {"a": {"b": {}}})


class ModuleDefaults(unittest.TestCase):
    def test_no_page_and_no_actions(self):
        m = Module(types.SimpleNamespace())
        self.assertIsNone(m.panel_page())
        with self.assertRaises(ApiError) as cm:
            m.panel_action("x")
        self.assertEqual((cm.exception.status, str(cm.exception)), (404, "no such action"))


class PluginApi(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("GADGET_TOKEN", None)
        self.plugins = paths.plugins_dir()
        for entry in os.listdir(self.plugins):
            self._remove(os.path.join(self.plugins, entry))
        self._install("gadget", GADGET)
        self._install("crashy", CRASHY)
        self._install("plain", PLAIN)
        self._install("broken", PLAIN, api=99)
        shutil.rmtree(paths.state_dir(), ignore_errors=True)
        self.config_path = os.path.join(tmpdir(), "config.yaml")
        with open(self.config_path, "w") as f:
            yaml.safe_dump({"pages": {"main": []}, "gadget": {"host": "old", "mode": "a"}}, f)
        os.utime(self.config_path, (1000, 1000))
        self.bot = DeckBot(root=ROOT, config_path=self.config_path)
        self.bot.load_config(initial=True)
        self.bot.package = "modules"
        self.addCleanup(self.cleanup)
        self.bot.load_plugins()
        self.state = sys.modules["deck_plugins.gadget"].STATE
        self.router = Router()
        plugin_api.register(self.router, types.SimpleNamespace(bot=self.bot))

    def _install(self, name, code, api=1):
        folder = os.path.join(self.plugins, name)
        os.makedirs(folder)
        with open(os.path.join(folder, "plugin.yaml"), "w") as f:
            f.write(MANIFEST.format(name=name, api=api))
        with open(os.path.join(folder, "__init__.py"), "w") as f:
            f.write(textwrap.dedent(code))

    @staticmethod
    def _remove(path):
        if os.path.islink(path):
            os.unlink(path)
        else:
            shutil.rmtree(path, ignore_errors=True)

    def cleanup(self):
        for name in list(self.bot.extensions):
            self.bot.unload_extension(name)
        for entry in os.listdir(self.plugins):
            self._remove(os.path.join(self.plugins, entry))
        for mod in [m for m in sys.modules if m.startswith(plugins.PACKAGE + ".")]:
            del sys.modules[mod]

    def get(self, name="gadget"):
        return self.router.find_get(f"/api/plugins/{name}")({})

    def post(self, what, body, name="gadget"):
        return self.router.find_post(f"/api/plugins/{name}/{what}")(body)

    def error(self, fn, *args):
        with self.assertRaises(ApiError) as cm:
            fn(*args)
        return cm.exception.status, str(cm.exception)

    def on_disk(self):
        return configio.load(self.config_path)

    # ---------------------------------------------------------------- discord_webhook field

    def test_discord_webhook_field_lists_the_registry_and_validates(self):
        self.state["page"] = {"title": "Gadget", "fields": [FIELD_HOST]}
        self.assertNotIn("discord_webhooks", self.get())   # not asked for without the field...
        self.state["page"] = {"title": "Gadget", "fields": [HOOK_FIELD]}
        body = self.get()
        self.assertEqual(body["discord_webhooks"], [])   # ...and no Discord module: empty list
        self.assertEqual(body["values"], {"discord.webhook": ""})
        self.bot.modules["Discord"] = FakeDiscord()
        self.addCleanup(self.bot.modules.pop, "Discord", None)
        body = self.get()
        self.assertEqual(body["discord_webhooks"], [{"id": "golive", "name": "Golive", "set": True},
                                                    {"id": "alerts", "name": "Alerts", "set": False}])
        saved = self.post("settings", {"values": {"discord.webhook": "alerts"}, "hash": body["hash"]})
        self.assertEqual(saved["values"]["discord.webhook"], "alerts")
        self.assertEqual(self.on_disk()["gadget"]["discord"], {"webhook": "alerts"})
        status, text = self.error(self.post, "settings",
                                  {"values": {"discord.webhook": "ghost"}, "hash": saved["hash"]})
        self.assertEqual(status, 400)
        self.assertIn("Discord webhook: must be one of the Discord webhooks", text)
        self.assertEqual(self.on_disk()["gadget"]["discord"], {"webhook": "alerts"})

    # ---------------------------------------------------------------- status

    def test_status_lists_every_installed_plugin(self):
        listed = plugin_api.plugins_status(self.bot)
        self.assertEqual([p["name"] for p in listed], ["broken", "crashy", "gadget", "plain"])
        by = {p["name"]: p for p in listed}
        self.assertEqual(by["gadget"], {"name": "gadget", "title": "Gadget", "icon": "dial", "key": "gadget",
                                        "loaded": True, "error": None, "has_page": True})
        self.assertEqual(by["plain"], {"name": "plain", "title": "plain", "icon": "bolt", "key": "plain",
                                       "loaded": True, "error": None, "has_page": False})
        self.assertFalse(by["crashy"]["loaded"])
        self.assertIn("failed to load", by["crashy"]["error"])
        self.assertEqual((by["crashy"]["key"], by["crashy"]["has_page"]), (None, False))
        self.assertIn("plugin API 99", by["broken"]["error"])
        self.assertLessEqual(len(by["broken"]["error"]), 300)
        # Sorted by title: rename the gadget's page.
        self.state["page"] = {"title": "Zebra"}
        self.assertEqual([p["title"] for p in plugin_api.plugins_status(self.bot)][-1], "Zebra")

    def test_editor_status_has_the_plugins(self):
        from modules.editor import Editor
        st = Editor(self.bot).status()
        self.assertEqual([p["name"] for p in st["plugins"]], ["broken", "crashy", "gadget", "plain"])

    def test_an_invalid_or_failing_page_is_no_page(self):
        self.state["page"] = {"title": "x" * 99}
        self.assertFalse({p["name"]: p for p in plugin_api.plugins_status(self.bot)}["gadget"]["has_page"])
        with mock.patch.object(type(self.bot.modules["Gadget"]), "panel_page", side_effect=KeyError("x")):
            body = self.get()
        self.assertEqual((body["title"], body["actions"], body["fields"]), ("gadget", [], []))

    # ---------------------------------------------------------------- the page

    def test_page(self):
        body = self.get()
        self.assertEqual(set(body), {"name", "version", "description", "title", "icon", "intro", "key", "loaded",
                                     "error", "actions", "fields", "values", "secrets", "hash"})
        self.assertEqual((body["version"], body["description"], body["intro"], body["key"]),
                         ("1.2.3", "gadget test plugin", "A test gadget.", "gadget"))
        self.assertEqual(body["values"], {"host": "old", "net.port": 5004, "loud": False, "mode": "a"})
        self.assertEqual(body["secrets"], {"token": {"set": False, "source": None}})
        self.assertEqual(body["hash"], configio.file_hash(self.config_path))
        json.dumps(body)

    def test_unknown_and_not_loaded_plugins(self):
        self.assertIsNone(self.router.find_get("/api/plugins/nothere/x"))
        self.assertIsNone(self.router.find_get("/api/plugins/Bad"))
        self.assertIsNone(self.router.find_post("/api/plugins/gadget"))
        self.assertEqual(self.error(self.get, "nothere")[0], 404)
        body = self.get("crashy")
        self.assertEqual((body["loaded"], body["actions"], body["fields"], body["values"], body["secrets"]),
                         (False, [], [], {}, {}))
        self.assertIn("failed to load", body["error"])
        self.assertEqual(self.get("broken")["version"], None)
        self.assertEqual(self.error(self.post, "action", {"id": "go"}, "crashy")[0], 409)

    # ---------------------------------------------------------------- settings

    def test_save_settings_and_secret(self):
        h = self.get()["hash"]
        body = self.post("settings", {"values": {"host": "  new  ", "net.port": 5004, "loud": True},
                                      "secrets": {"token": f"  {SECRET}  "}, "hash": h})
        self.assertNotIn(SECRET, json.dumps(body))
        self.assertEqual(body["values"], {"host": "new", "net.port": 5004, "loud": True, "mode": "a"})
        self.assertEqual(body["secrets"], {"token": {"set": True, "source": "file"}})
        self.assertNotEqual(body["hash"], h)
        path = paths.state_file("gadget_token")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        with open(path) as f:
            self.assertEqual(f.read(), SECRET + "\n")
        section = self.on_disk()["gadget"]
        # net.port is its default and wasn't in config.yaml: not written.
        self.assertEqual(section, {"host": "new", "mode": "a", "loud": True, "token_file": path})
        with open(self.config_path) as f:
            self.assertNotIn(SECRET, f.read())
        self.assertEqual([n for n in os.listdir(paths.state_dir()) if ".tmp" in n], [])
        # The daemon has it already.
        self.assertEqual(self.bot.modules["Gadget"].config["host"], "new")
        # Remove it again.
        body = self.post("settings", {"secrets": {"token": None}, "hash": body["hash"]})
        self.assertEqual(body["secrets"], {"token": {"set": False, "source": None}})
        self.assertFalse(os.path.exists(path))
        self.assertNotIn("token_file", self.on_disk()["gadget"])

    def test_secret_alone_reapplies_the_config(self):
        h = self.post("settings", {"secrets": {"token": "one"}, "hash": self.get()["hash"]})["hash"]
        before = len(self.state["configs"])
        body = self.post("settings", {"secrets": {"token": "two"}, "hash": h})
        self.assertEqual(body["hash"], h)   # config.yaml unchanged: the path is the same
        self.assertGreater(len(self.state["configs"]), before)
        with open(paths.state_file("gadget_token")) as f:
            self.assertEqual(f.read(), "two\n")

    def test_a_secret_you_named_yourself_is_not_deleted(self):
        own = os.path.join(tmpdir(), "mine")
        with open(own, "w") as f:
            f.write("x\n")
        cfg = self.on_disk()
        cfg["gadget"]["token_file"] = own
        configio.save(self.config_path, cfg)
        self.post("settings", {"secrets": {"token": None}, "hash": self.get()["hash"]})
        self.assertTrue(os.path.exists(own))
        self.assertNotIn("token_file", self.on_disk()["gadget"])

    def test_env_secret(self):
        os.environ["GADGET_TOKEN"] = "abc"
        self.assertEqual(self.get()["secrets"], {"token": {"set": True, "source": "env"}})

    def test_stale_hash_is_409_and_changes_nothing(self):
        h = self.get()["hash"]
        cfg = self.on_disk()
        cfg["gadget"]["host"] = "elsewhere"
        configio.save(self.config_path, cfg)
        status, message = self.error(self.post, "settings",
                                     {"values": {"host": "mine"}, "secrets": {"token": SECRET}, "hash": h})
        self.assertEqual((status, message),
                         (409, "config.yaml changed since the panel loaded it; reload and try again"))
        self.assertEqual(self.on_disk()["gadget"]["host"], "elsewhere")
        self.assertFalse(os.path.exists(os.path.join(paths.state_dir(), "gadget_token")))
        # A conflict found by configio.save itself (after the early check) is a 409 as well.
        with mock.patch.object(configio, "save", side_effect=configio.Conflict("x")):
            status, _ = self.error(self.post, "settings", {"values": {"host": "mine"}, "secrets": {"token": SECRET},
                                                           "hash": self.get()["hash"]})
        self.assertEqual(status, 409)
        self.assertFalse(os.path.exists(os.path.join(paths.state_dir(), "gadget_token")))
        self.assertEqual([n for n in os.listdir(paths.state_dir()) if ".tmp" in n], [])

    def test_bad_settings_are_400_with_every_problem(self):
        h = self.get()["hash"]
        status, message = self.error(self.post, "settings", {"values": {"host": "", "net.port": 80, "zap": 1},
                                                             "secrets": {"token": "a\nb"}, "hash": h})
        self.assertEqual(status, 400)
        self.assertEqual(message, "Fix these first: Host: is required; Port: must be from 1024 to 65535; "
                                  "zap: not a setting of this plugin; Token: must be one line without control "
                                  "characters")
        self.assertEqual(self.on_disk()["gadget"], {"host": "old", "mode": "a"})
        self.assertEqual(self.error(self.post, "settings", {"values": {}})[0], 400)        # no hash
        self.assertEqual(self.error(self.post, "settings", {"values": [], "hash": h})[0], 400)

    def test_defaults_are_not_written(self):
        self.post("settings", {"values": {"net.port": 5004, "loud": False, "mode": "a"},
                               "hash": self.get()["hash"]})
        self.assertEqual(self.on_disk()["gadget"], {"host": "old", "mode": "a"})

    # ---------------------------------------------------------------- actions

    def test_actions(self):
        self.assertEqual(self.post("action", {"id": "go"}), {"ok": True, "message": "went"})
        for action_id in ("off", "missing"):
            self.assertEqual(self.error(self.post, "action", {"id": action_id}),
                             (400, "That button isn't available right now"))
        self.assertEqual(self.error(self.post, "action", {"id": "boom"}), (400, "it broke"))
        self.assertEqual(self.error(self.post, "action", {"id": "nope"}), (409, "not now"))
        self.assertEqual(self.error(self.post, "action", {"id": "ghost"}), (404, "no such action"))
        self.assertEqual(self.error(self.post, "action", {"id": 5})[0], 400)
        self.assertEqual(self.state["calls"], ["go", "boom", "nope", "ghost"])   # never "off"
        self.assertEqual(self.error(self.post, "action", {"id": "go"}, "plain"),
                         (400, "That button isn't available right now"))


if __name__ == "__main__":
    unittest.main()
