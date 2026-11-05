"""config.yaml over the daemon's life: a broken or missing file at start falls back to a backup
(or an empty config) instead of crashing, the panel can save over it, reloads never apply a
half-written file, and hot-reloading modules is safe while other threads read the registries."""
import os
import threading
import time
import types
import unittest
from unittest import mock

from tests.helpers import ROOT, Deck, free_port, tmpdir

import yaml  # noqa: E402

from core import backups, configio, editor_auth  # noqa: E402
from core import bot as bot_module  # noqa: E402
from core.bot import DeckBot  # noqa: E402
from core.module import Module, action, expander  # noqa: E402

BROKEN = b"pages:\n  main:\n    - [a, b\n  other: [\n"


def config(page="main", **extra):
    return {"pages": {page: [["-"]]}, **extra}


def dump(cfg):
    return yaml.safe_dump(cfg, sort_keys=False).encode()


class Sandbox(unittest.TestCase):
    """A config.yaml of its own and a revisions folder of its own (patched into core.backups)."""

    def setUp(self):
        self.dir = tmpdir()
        self.path = os.path.join(self.dir, "config.yaml")
        state = os.path.join(self.dir, "state")
        os.makedirs(state)
        patcher = mock.patch.object(backups, "state_dir", lambda: state)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, data, bump=10):
        with open(self.path, "wb") as f:
            f.write(data)
        st = os.stat(self.path)
        later = st.st_mtime + bump
        os.utime(self.path, (later, later))

    def keep(self, data, source="file"):
        """Write data as config.yaml and keep it as a revision; returns the revision id."""
        self.write(data)
        return backups.record(self.path, source)["id"]

    def bot(self):
        bot = DeckBot(root=ROOT, config_path=self.path)
        bot.deck = Deck("Stream Deck XL")
        self.addCleanup(self.unload, bot)
        return bot

    @staticmethod
    def unload(bot):
        for name in list(bot.extensions):
            bot.unload_extension(name)

    def load_editor(self, bot):
        """The real editor module, on a free port (never the default one)."""
        with mock.patch.object(editor_auth, "DEFAULT_PORT", free_port()):
            bot.load_extension("modules.editor")
        editor = bot.get_module("Editor")
        self.assertIsNotNone(editor, "the editor module didn't load")
        return editor


class InitialLoad(Sandbox):
    def test_broken_yaml_falls_back_to_the_newest_good_revision(self):
        self.keep(dump(config("older")))
        good = self.keep(dump(config("newest_good")))
        self.keep(dump({"keys": {}}))            # a revision with no pages: doesn't load either
        self.write(BROKEN)
        backups.record(self.path, "before-save")  # the broken file itself, kept by a save
        before = os.stat(self.path)
        bot = self.bot()
        with self.assertLogs("deck", "ERROR") as logs:
            self.assertFalse(bot.load_config(initial=True))
        self.assertEqual(list(bot.cfg["pages"]), ["newest_good"])
        self.assertEqual(set(bot.config_fallback), {"revision", "error"})
        self.assertEqual(bot.config_fallback["revision"], good)
        self.assertRegex(bot.config_fallback["error"], r"^line \d+, column \d+: ")
        text = "\n".join(logs.output)
        self.assertIn(self.path, text)
        self.assertIn(good, text)
        after = os.stat(self.path)
        self.assertEqual((after.st_mtime_ns, after.st_size), (before.st_mtime_ns, before.st_size))
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), BROKEN, "config.yaml must be left as it is")

    def test_schema_error_falls_back_too(self):
        good = self.keep(dump(config()))
        self.write(dump({"brightness": 50}))     # valid YAML, but no pages
        bot = self.bot()
        with self.assertLogs("deck", "ERROR"):
            bot.load_config(initial=True)
        self.assertEqual(bot.config_fallback, {"revision": good, "error": "config has no pages (or layouts)"})

    def test_no_revision_starts_empty_and_the_editor_still_loads(self):
        self.write(BROKEN)
        bot = self.bot()
        with self.assertLogs("deck", "ERROR"):
            self.assertFalse(bot.load_config(initial=True))
        self.assertEqual(bot.cfg, {})
        self.assertIsNone(bot.config_fallback["revision"])
        editor = self.load_editor(bot)
        self.assertIsNotNone(editor.server, "the control panel should be listening")
        self.assertEqual(editor.status()["config_fallback"], bot.config_fallback)
        state = editor.state()
        self.assertEqual(state["config"], {})
        self.assertEqual(state["hash"], configio.file_hash(self.path))

    def test_missing_file_at_start(self):
        bot = self.bot()
        with self.assertLogs("deck", "WARNING") as logs:
            self.assertFalse(bot.load_config(initial=True))
        self.assertEqual(bot.config_fallback, {"revision": None, "error": "config.yaml is missing"})
        self.assertEqual(sum("is missing" in line for line in logs.output), 1)
        self.assertFalse(os.path.exists(self.path))

    def test_good_file_is_not_a_fallback(self):
        self.write(dump(config()))
        bot = self.bot()
        self.assertTrue(bot.load_config(initial=True))
        self.assertIsNone(bot.config_fallback)


class SaveOverBroken(Sandbox):
    def test_editor_save_over_a_broken_file_clears_the_fallback(self):
        good = self.keep(dump(config("from_backup", twitch_bot={"database_url": "postgres://u:hunter2@db/x"})))
        self.write(BROKEN)
        bot = self.bot()
        with self.assertLogs("deck", "ERROR"):
            bot.load_config(initial=True)
        self.assertEqual(bot.config_fallback["revision"], good)
        editor = self.load_editor(bot)
        q = bot.hub.subscribe()
        self.addCleanup(bot.hub.unsubscribe, q)
        state = editor.state()   # what the panel edits: the backup it runs on, secrets masked
        self.assertNotIn("hunter2", repr(state["config"]))
        with self.assertRaises(configio.Conflict):   # the hash still guards the raw bytes
            editor.save({"config": state["config"], "hash": "0" * 64})
        self.assertIsNotNone(bot.config_fallback)
        res = editor.save({"config": state["config"], "hash": state["hash"]})
        self.assertEqual(res["hash"], configio.file_hash(self.path))
        self.assertIsNone(bot.config_fallback)
        self.assertIsNone(editor.status()["config_fallback"])
        saved = configio.load(self.path)
        self.assertEqual(list(saved["pages"]), ["from_backup"])
        self.assertEqual(saved["twitch_bot"]["database_url"], "postgres://u:hunter2@db/x")
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        configs = [e["data"] for e in events if e["type"] == "config"]
        self.assertTrue(configs and configs[-1]["config_fallback"] is None, configs)
        self.assertTrue(bot.load_config())        # the daemon then runs on the saved file
        self.assertEqual(list(bot.cfg["pages"]), ["from_backup"])
        self.assertIsNone(bot.config_fallback)


class Reloads(Sandbox):
    def setUp(self):
        super().setUp()
        self.write(dump(config()))
        self.b = self.bot()
        self.assertTrue(self.b.load_config(initial=True))

    def test_missing_file_is_logged_once_and_again_when_back(self):
        os.remove(self.path)
        with self.assertLogs("deck", "INFO") as logs:
            for _ in range(5):
                self.assertFalse(self.b.load_config())
        self.assertEqual(len([line for line in logs.output if "WARNING" in line]), 1, logs.output)
        self.assertNotIn("Traceback", "\n".join(logs.output))
        self.assertEqual(list(self.b.cfg["pages"]), ["main"])
        self.assertIsNone(self.b.config_fallback)
        self.write(dump(config("back")))
        with self.assertLogs("deck", "INFO") as logs:
            self.assertTrue(self.b.load_config())
            self.assertFalse(self.b.load_config())
        self.assertEqual(sum("is back" in line for line in logs.output), 1)
        self.assertEqual(list(self.b.cfg["pages"]), ["back"])

    def test_bad_reload_keeps_the_running_config_and_logs_once(self):
        self.write(BROKEN)
        with self.assertLogs("deck", "ERROR") as logs:
            for _ in range(3):
                self.assertFalse(self.b.load_config())
        self.assertEqual(sum("config not loaded" in line for line in logs.output), 1)
        self.assertEqual(list(self.b.cfg["pages"]), ["main"])
        self.assertIsNone(self.b.config_fallback)

    def test_a_file_that_changes_while_read_is_not_applied(self):
        self.write(dump(config("half")))
        real_stat, calls = os.stat, []

        def stat(path, *a, **kw):
            st = real_stat(path, *a, **kw)
            if path != self.path:
                return st
            calls.append(path)
            if len(calls) == 2:   # the stat after the read: the writer wasn't done
                return types.SimpleNamespace(st_mtime_ns=st.st_mtime_ns, st_size=st.st_size + 7)
            return st

        with mock.patch.object(bot_module.os, "stat", stat):
            self.assertFalse(self.b.load_config())
        self.assertEqual(len(calls), 2)
        self.assertEqual(list(self.b.cfg["pages"]), ["main"], "a half-written file must not be applied")
        self.assertTrue(self.b.load_config(), "it is read again on the next pass")
        self.assertEqual(list(self.b.cfg["pages"]), ["half"])

    def test_change_is_seen_by_size_with_the_same_mtime(self):
        st = os.stat(self.path)
        with open(self.path, "wb") as f:
            f.write(dump(config("longer_name")))
        os.utime(self.path, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertTrue(self.b.load_config())
        self.assertEqual(list(self.b.cfg["pages"]), ["longer_name"])

    def test_identical_content_is_not_applied_again(self):
        with open(self.path, "rb") as f:
            data = f.read()
        self.write(data)
        self.assertFalse(self.b.load_config())


class Busy(Module):
    """Many registrations, so a reader iterating the registries overlaps a hot-reload."""


for _i in range(150):
    setattr(Busy, f"x{_i}", expander(f"busy_x{_i}")(lambda self, arg, btn: btn))
    setattr(Busy, f"a{_i}", action(f"busy_a{_i}")(lambda self, arg: None))


class HotReload(Sandbox):
    def test_reloading_modules_while_threads_iterate_the_registries(self):
        from modules.editor import Editor
        self.write(dump(config()))
        bot = self.bot()
        bot.load_config(initial=True)
        bot.load_extension("modules.system")
        catalog = types.SimpleNamespace(bot=bot)
        errors, stop = [], threading.Event()

        def reader():
            btn = {"label": "x", "nothing_expands_this": 1}
            while not stop.is_set():
                try:
                    bot._expand(btn)
                    Editor.catalog(catalog)
                    for registry in bot_module.MARKERS.values():
                        sorted(list(getattr(bot, registry).items()), key=lambda kv: kv[0])
                    bot.dispatch("nothing_listens")
                    list(bot.modules.values())
                except Exception as e:
                    errors.append(e)
                    return

        threads = [threading.Thread(target=reader, daemon=True) for _ in range(3)]
        for t in threads:
            t.start()
        try:
            # A fixed amount of work, not a time window: a slow CI runner must do the same rounds.
            deadline = time.monotonic() + 30
            rounds = 0
            while rounds < 30 and time.monotonic() < deadline and not errors:
                bot.add_module(Busy(bot))
                bot.remove_module(bot.get_module("Busy"))
                if rounds % 10 == 0:
                    bot.reload_extension("modules.system")
                rounds += 1
        finally:
            stop.set()
            for t in threads:
                t.join(5)
        self.assertEqual(errors, [])
        self.assertEqual(rounds, 30)
        self.assertIn("System", bot.modules)
        self.assertNotIn("busy_x0", bot.expanders)
        self.assertIn("run", bot.actions)

    def test_unloading_a_module_whose_entries_were_replaced_by_plain_functions(self):
        # tools/devserver.py's dry run swaps actions and state/display providers for plain
        # functions (no __self__); unloading the module must still remove them, not crash.
        self.write(dump(config()))
        bot = self.bot()
        bot.load_config(initial=True)
        bot.load_extension("modules.system")
        bot.actions["run"] = lambda arg: None
        bot.state_providers["cmd"] = lambda *a: False
        bot.unload_extension("modules.system")
        self.assertNotIn("System", bot.modules)
        self.assertNotIn("run", bot.actions)
        self.assertNotIn("cmd", bot.state_providers)
        self.assertNotIn("audio", bot.state_providers)
        bot.load_extension("modules.system")   # and it loads again cleanly
        self.assertIn("run", bot.actions)


if __name__ == "__main__":
    unittest.main()
