"""Unloading a plugin never holds up the main loop: its registry entries go at once and its
on_unload runs on a background thread. A reload's new code only starts once the old copy's
on_unload is over (or has overrun its timeout); shutdown still waits for every unload."""
import os
import shutil
import sys
import textwrap
import threading
import time
import unittest
from unittest import mock

from tests.helpers import ROOT, tmpdir

import yaml  # noqa: E402

from core import bot as core_bot, paths, plugins  # noqa: E402
from core.bot import DeckBot  # noqa: E402

MANIFEST = "name: slow\nversion: '1.0'\ndescription: test plugin\napi: 1\n"

# The bot carries the shared log and the sleep, so they outlive the plugin's own code.
SLOW_INIT = """
import time

from core import Module, action, display, listener, state, task

VERSION = {version!r}


class Slow(Module):
    @action("slow_go")
    def go(self, arg):
        pass

    @state("slow_on")
    def on(self, arg):
        return False

    @display("slow_text")
    def text(self, arg):
        return ""

    @listener("slow_event")
    def heard(self):
        pass

    @task(60)
    def tick(self):
        pass

    def on_load(self):
        self.bot.unload_log.append(("load", VERSION))

    def on_unload(self):
        self.bot.unload_log.append(("unload-start", VERSION))
        time.sleep(self.bot.unload_sleep)
        self.bot.unload_log.append(("unload-end", VERSION))


def setup(bot):
    bot.add_module(Slow(bot))
"""


def touch_later(path, seconds=10):
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + seconds * 1_000_000_000))


class PluginUnload(unittest.TestCase):
    def setUp(self):
        self.plugins = paths.plugins_dir()
        for entry in os.listdir(self.plugins):
            shutil.rmtree(os.path.join(self.plugins, entry), ignore_errors=True)
        config = os.path.join(tmpdir(), "config.yaml")
        with open(config, "w") as f:
            yaml.safe_dump({"pages": {"main": []}}, f)
        self.bot = DeckBot(root=ROOT, config_path=config)
        self.bot.load_config(initial=True)
        self.bot.package = "modules"
        self.bot.unload_log = []
        self.bot.unload_sleep = 0
        self.folder = os.path.join(self.plugins, "slow")
        self.write("v1")
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.bot.unload_sleep = 0
        self.bot.unload_all()
        for t in threading.enumerate():
            if t.name.startswith("unload-"):
                t.join(5)
        shutil.rmtree(self.folder, ignore_errors=True)
        for mod in [m for m in sys.modules if m.startswith(plugins.PACKAGE + ".")]:
            del sys.modules[mod]

    def write(self, version):
        os.makedirs(self.folder, exist_ok=True)
        with open(os.path.join(self.folder, "plugin.yaml"), "w") as f:
            f.write(MANIFEST)
        init = os.path.join(self.folder, "__init__.py")
        existed = os.path.exists(init)
        with open(init, "w") as f:
            f.write(textwrap.dedent(SLOW_INIT.format(version=version)))
        if existed:
            touch_later(init)

    def timed_watch(self):
        start = time.monotonic()
        self.bot.watch_plugins()
        return time.monotonic() - start

    def watch_until(self, done, timeout=10):
        deadline = time.monotonic() + timeout
        while not done():
            self.assertLess(time.monotonic(), deadline, "timed out")
            self.bot.watch_plugins()
            time.sleep(0.05)

    def assert_unregistered(self):
        self.assertNotIn("slow_go", self.bot.actions)
        self.assertNotIn("slow_on", self.bot.state_providers)
        self.assertNotIn("slow_text", self.bot.display_providers)
        self.assertEqual(self.bot.listeners.get("slow_event", []), [])
        self.assertFalse([t for t in self.bot.tasks if type(t[0].__self__).__name__ == "Slow"])
        self.assertNotIn("Slow", self.bot.modules)
        self.assertNotIn("plugin:slow", self.bot.extensions)

    def unload_threads(self):
        return [t for t in threading.enumerate() if t.name == "unload-slow" and t.is_alive()]

    def test_slow_unload_does_not_block_the_watcher(self):
        self.bot.load_plugins()
        self.assertIn("slow_go", self.bot.actions)
        self.bot.unload_sleep = 2
        shutil.rmtree(self.folder)
        self.assertLess(self.timed_watch(), 0.5)
        self.assert_unregistered()                  # gone at once, before on_unload is over
        self.assertEqual(self.bot.unload_log, [("load", "v1"), ("unload-start", "v1")])
        self.assertEqual(len(self.unload_threads()), 1)
        self.watch_until(lambda: not self.bot._unloading)
        self.assertEqual(self.bot.unload_log[-1], ("unload-end", "v1"))
        self.assertFalse([m for m in sys.modules if m.startswith("deck_plugins.slow")])

    def test_reload_waits_for_the_old_unload(self):
        self.bot.load_plugins()
        self.bot.unload_sleep = 1
        self.write("v2")
        self.assertLess(self.timed_watch(), 0.5)
        self.assert_unregistered()
        self.assertNotIn(("load", "v2"), self.bot.unload_log)   # queued behind the unload
        self.assertLess(self.timed_watch(), 0.5)                # still unloading: still queued
        self.assertNotIn(("load", "v2"), self.bot.unload_log)
        self.watch_until(lambda: "Slow" in self.bot.modules)
        self.assertEqual(self.bot.unload_log, [("load", "v1"), ("unload-start", "v1"),
                                               ("unload-end", "v1"), ("load", "v2")])
        self.assertIn("slow_go", self.bot.actions)
        self.assertEqual(len(self.bot.extensions["plugin:slow"]["instances"]), 1)

    def test_removed_while_its_reload_is_queued_is_not_loaded(self):
        self.bot.load_plugins()
        self.bot.unload_sleep = 0.5
        self.write("v2")
        self.bot.watch_plugins()
        shutil.rmtree(self.folder)
        self.watch_until(lambda: not self.bot._unloading)
        self.bot.watch_plugins()
        self.assertNotIn(("load", "v2"), self.bot.unload_log)
        self.assert_unregistered()

    def test_overrun_is_logged_and_the_load_goes_ahead(self):
        self.bot.load_plugins()
        self.bot.unload_sleep = 2
        self.write("v2")
        with mock.patch.object(core_bot, "UNLOAD_TIMEOUT", 0.3), \
                self.assertLogs("deck", "WARNING") as logs:
            self.assertLess(self.timed_watch(), 0.5)
            self.watch_until(lambda: "Slow" in self.bot.modules, timeout=1.5)
        self.assertTrue([r for r in logs.output
                         if "WARNING" in r and "plugin:slow didn't finish unloading" in r])
        self.assertEqual(self.bot.unload_log, [("load", "v1"), ("unload-start", "v1"), ("load", "v2")])

    def test_shutdown_waits_for_unloads(self):
        self.bot.load_plugins()
        self.bot.unload_sleep = 1
        start = time.monotonic()
        self.bot.unload_all()
        self.assertGreaterEqual(time.monotonic() - start, 1)
        self.assertEqual(self.bot.unload_log[-1], ("unload-end", "v1"))
        self.assert_unregistered()

    def test_shutdown_waits_for_a_background_unload_and_loads_nothing(self):
        self.bot.load_plugins()
        self.bot.unload_sleep = 1
        self.write("v2")
        self.bot.watch_plugins()                    # the reload's unload is now in the background
        self.bot.unload_all()
        self.assertEqual(self.bot.unload_log, [("load", "v1"), ("unload-start", "v1"),
                                               ("unload-end", "v1")])
        self.assertFalse(self.unload_threads())
        self.assertEqual(self.bot._queued, {})
        self.assertFalse([m for m in sys.modules if m.startswith("deck_plugins.slow")])


if __name__ == "__main__":
    unittest.main()
