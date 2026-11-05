"""The daemon loads installed plugins (core/plugins.py) as deck_plugins.<name>, reloads them when
any of their files change, unloads them when they are removed, and refuses any plugin that would
take something core (or another module) already has, without ever leaving half a plugin behind."""
import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from tests.helpers import ROOT, tmpdir

import yaml  # noqa: E402

from core import chatcmd, paths, plugins  # noqa: E402
from core.bot import DeckBot  # noqa: E402

MANIFEST = "name: {name}\nversion: '1.0'\ndescription: test plugin\napi: 1\n"

ALPHA_INIT = """
from core import Module, action
from .helper import greet

EVENTS = []


class Alpha(Module):
    @action("alpha_greet")
    def go(self, arg):
        EVENTS.append(greet(arg))

    def on_config(self, config):
        super().on_config(config)
        EVENTS.append(("config", dict(config)))

    def on_unload(self):
        EVENTS.append("unloaded")


def setup(bot):
    bot.add_module(Alpha(bot))
"""


def write(folder, files):
    os.makedirs(folder, exist_ok=True)
    for rel, text in files.items():
        with open(os.path.join(folder, rel), "w") as f:
            f.write(textwrap.dedent(text))


def touch_later(path, seconds=10):
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + seconds * 1_000_000_000))


class PluginLoader(unittest.TestCase):
    def setUp(self):
        self.plugins = paths.plugins_dir()
        for entry in os.listdir(self.plugins):
            shutil.rmtree(os.path.join(self.plugins, entry), ignore_errors=True)
        config = os.path.join(tmpdir(), "config.yaml")
        with open(config, "w") as f:
            yaml.safe_dump({"pages": {"main": []}, "alpha": {"greeting": "hi"}}, f)
        self.bot = DeckBot(root=ROOT, config_path=config)
        self.bot.load_config(initial=True)
        self.bot.package = "modules"
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for name in list(self.bot.extensions):
            self.bot.unload_extension(name)
        for entry in os.listdir(self.plugins):
            path = os.path.join(self.plugins, entry)
            if os.path.islink(path):
                os.unlink(path)
            else:
                shutil.rmtree(path, ignore_errors=True)
        for mod in [m for m in sys.modules if m.startswith(plugins.PACKAGE + ".")]:
            del sys.modules[mod]

    def plugin(self, name, files):
        folder = os.path.join(self.plugins, name)
        write(folder, {"plugin.yaml": MANIFEST.format(name=name), **files})
        return folder

    def alpha(self):
        return self.plugin("alpha", {"__init__.py": ALPHA_INIT,
                                     "helper.py": "def greet(arg):\n    return f'v1 {arg}'\n"})

    def events(self):
        return sys.modules["deck_plugins.alpha"].EVENTS

    # ---------------------------------------------------------------- loading

    def test_two_file_plugin_loads_with_its_own_config_section(self):
        self.alpha()
        self.bot.load_plugins()
        self.assertIn("plugin:alpha", self.bot.extensions)
        self.assertIn("alpha_greet", self.bot.actions)
        self.assertEqual(self.bot.modules["Alpha"].config_key, "alpha")
        self.bot.actions["alpha_greet"]("x")
        self.assertEqual(self.events(), [("config", {"greeting": "hi"}), "v1 x"])
        self.assertIn("deck_plugins.alpha.helper", sys.modules)

    def test_editing_a_submodule_reloads_the_new_code(self):
        folder = self.alpha()
        self.bot.load_plugins()
        old_events = self.events()
        helper = os.path.join(folder, "helper.py")
        with open(helper, "w") as f:
            f.write("def greet(arg):\n    return f'version two {arg}'\n")
        touch_later(helper)
        with mock.patch.object(self.bot, "invalidate") as invalidate:
            self.bot.watch_plugins()
        invalidate.assert_called()
        self.assertIn("unloaded", old_events)          # the old instance was unloaded
        self.bot.actions["alpha_greet"]("x")
        self.assertEqual(self.events()[-1], "version two x")
        self.assertEqual(len(self.bot.extensions["plugin:alpha"]["instances"]), 1)

    def test_unchanged_plugin_is_not_reloaded(self):
        self.alpha()
        self.bot.load_plugins()
        inst = self.bot.modules["Alpha"]
        with mock.patch.object(self.bot, "invalidate") as invalidate:
            self.bot.watch_plugins()
        invalidate.assert_not_called()
        self.assertIs(self.bot.modules["Alpha"], inst)

    def test_removed_plugin_is_unloaded_and_forgotten(self):
        folder = self.alpha()
        self.bot.load_plugins()
        events = self.events()
        shutil.rmtree(folder)
        self.bot.watch_plugins()
        self.assertIn("unloaded", events)
        self.assertNotIn("alpha_greet", self.bot.actions)
        self.assertNotIn("Alpha", self.bot.modules)
        self.assertNotIn("plugin:alpha", self.bot.extensions)
        self.assertFalse([m for m in sys.modules if m.startswith("deck_plugins.alpha")])

    def test_new_install_is_loaded_by_the_watcher(self):
        self.bot.load_plugins()
        self.assertNotIn("alpha_greet", self.bot.actions)
        self.alpha()
        self.bot.watch_plugins()
        self.assertIn("alpha_greet", self.bot.actions)

    def test_watcher_does_nothing_until_plugins_are_enabled(self):
        self.alpha()
        self.bot.watch_plugins()
        self.assertNotIn("plugin:alpha", self.bot.extensions)

    def test_symlinked_catalog_plugin_loads(self):
        catalog = tempfile.mkdtemp(dir=tmpdir())
        write(os.path.join(catalog, "beta"), {
            "plugin.yaml": MANIFEST.format(name="beta"),
            "__init__.py": """
                from core import Module, state

                class Beta(Module):
                    @state("beta_on")
                    def on(self, arg, spec):
                        return True

                def setup(bot):
                    bot.add_module(Beta(bot))
            """})
        os.symlink(os.path.join(catalog, "beta"), os.path.join(self.plugins, "beta"))
        with mock.patch.object(plugins, "catalog_dir", return_value=catalog):
            self.bot.load_plugins()
        self.assertIn("beta_on", self.bot.state_providers)
        self.assertEqual(self.bot.extensions["plugin:beta"]["path"], os.path.realpath(os.path.join(catalog, "beta")))

    def test_watching_core_modules_ignores_plugin_records(self):
        self.alpha()
        self.bot.load_plugins()
        with mock.patch.object(self.bot, "load_extension"):
            self.bot.watch_extensions()
        self.assertIn("plugin:alpha", self.bot.extensions)
        self.assertIn("alpha_greet", self.bot.actions)

    # ---------------------------------------------------------------- refusals

    def test_class_named_like_a_core_module_is_refused(self):
        self.bot.load_extension("modules.obs")
        core_obs = self.bot.modules["OBS"]
        self.plugin("fakeobs", {"__init__.py": """
            from core import Module, action

            class OBS(Module):
                @action("fakeobs_thing")
                def thing(self, arg): pass

            def setup(bot):
                bot.add_module(OBS(bot))
        """})
        self.bot.load_plugins()
        self.assertIs(self.bot.modules["OBS"], core_obs)
        self.assertNotIn("fakeobs_thing", self.bot.actions)
        self.assertIn("plugin:fakeobs", self.bot.extensions)   # kept, so a fixed file reloads
        self.assertEqual(self.bot.extensions["plugin:fakeobs"]["instances"], [])
        self.assertFalse([m for m in sys.modules if m.startswith("deck_plugins.fakeobs")])

    def test_redeclared_action_is_refused_with_full_rollback(self):
        self.bot.load_extension("modules.navigation")
        core_page = self.bot.actions["page"]
        self.plugin("thief", {"__init__.py": """
            from core import Module, action

            UNLOADED = []

            class First(Module):
                @action("thief_ok")
                def ok(self, arg): pass

                def on_unload(self):
                    UNLOADED.append(type(self).__name__)

            class Second(Module):
                @action("thief_other")
                def other(self, arg): pass

                @action("page")
                def page(self, arg): pass

            def setup(bot):
                bot.add_module(First(bot))
                bot.add_module(Second(bot))
        """})
        self.bot.load_plugins()
        self.assertIs(self.bot.actions["page"], core_page)
        self.assertNotIn("thief_ok", self.bot.actions)
        self.assertNotIn("thief_other", self.bot.actions)
        self.assertNotIn("First", self.bot.modules)
        self.assertNotIn("Second", self.bot.modules)

    def test_refusal_cannot_be_swallowed_by_setup(self):
        self.bot.load_extension("modules.navigation")
        self.plugin("sneaky", {"__init__.py": """
            from core import Module, action

            class Sneaky(Module):
                @action("page")
                def page(self, arg): pass

            class Fine(Module):
                @action("sneaky_fine")
                def fine(self, arg): pass

            def setup(bot):
                bot.add_module(Fine(bot))
                try:
                    bot.add_module(Sneaky(bot))
                except ValueError:
                    pass
        """})
        self.bot.load_plugins()
        self.assertNotIn("sneaky_fine", self.bot.actions)
        self.assertNotIn("Fine", self.bot.modules)

    def test_core_config_section_is_refused(self):
        self.plugin("gamma", {"__init__.py": """
            from core import Module

            class Gamma(Module):
                config_key = "obs"

            def setup(bot):
                bot.add_module(Gamma(bot))
        """})
        self.bot.load_plugins()
        self.assertNotIn("Gamma", self.bot.modules)

    def test_failing_setup_leaves_nothing_and_core_unaffected(self):
        self.bot.load_extension("modules.navigation")
        before = (dict(self.bot.actions), dict(self.bot.modules), list(self.bot.tasks))
        self.plugin("broken", {"__init__.py": """
            from core import Module, action, task

            class Broken(Module):
                @action("broken_go")
                def go(self, arg): pass

                @task(every=1)
                def tick(self): pass

            def setup(bot):
                bot.add_module(Broken(bot))
                raise RuntimeError("plugin bug")
        """})
        self.bot.load_plugins()
        self.assertEqual((self.bot.actions, self.bot.modules, self.bot.tasks), before)
        self.assertFalse([m for m in sys.modules if m.startswith("deck_plugins.broken")])

    def test_fixing_a_broken_plugin_loads_it(self):
        folder = self.plugin("broken", {"__init__.py": "raise SystemExit('nope')\n"})
        self.bot.load_plugins()   # SystemExit from plugin code doesn't stop the daemon
        self.assertIn("plugin:broken", self.bot.extensions)
        init = os.path.join(folder, "__init__.py")
        write(folder, {"__init__.py": """
            from core import Module, action

            class Broken(Module):
                @action("broken_go")
                def go(self, arg): pass

            def setup(bot):
                bot.add_module(Broken(bot))
        """})
        touch_later(init)
        self.bot.watch_plugins()
        self.assertIn("broken_go", self.bot.actions)

    def test_manifest_error_is_skipped_and_logged_once(self):
        folder = os.path.join(self.plugins, "delta")
        write(folder, {"plugin.yaml": "name: delta\nversion: '1'\ndescription: x\napi: 99\n",
                       "__init__.py": "raise AssertionError('must not be imported')\n"})
        with self.assertLogs("deck", "WARNING") as logs:
            self.bot.load_plugins()
            self.bot.watch_plugins()
            self.bot.watch_plugins()
        warnings = [r for r in logs.output if "plugin delta not loaded" in r]
        self.assertEqual(len(warnings), 1)
        self.assertNotIn("plugin:delta", self.bot.extensions)

    def test_plugin_that_gains_an_error_is_unloaded(self):
        folder = self.alpha()
        self.bot.load_plugins()
        os.remove(os.path.join(folder, "plugin.yaml"))
        self.bot.watch_plugins()
        self.assertNotIn("alpha_greet", self.bot.actions)
        self.assertNotIn("plugin:alpha", self.bot.extensions)

    def test_chat_builtin_of_a_refused_plugin_is_removed(self):
        self.bot.load_extension("modules.navigation")
        self.addCleanup(chatcmd.unregister, None, "plugchat")
        for how in ("refused", "raises"):
            with self.subTest(how):
                name = f"chatty_{how}"
                self.plugin(name, {"__init__.py": f"""
                    from core import Module, action, chatcmd

                    class Talker(Module):
                        def values(self, arg):
                            return {{}}

                        def on_load(self):
                            chatcmd.register(self.bot, chatcmd.Builtin(
                                kind="plugchat", label="x", help="x", placeholders=frozenset(),
                                default_response="x", values=self.values))

                    class Thief(Module):
                        @action("page")
                        def page(self, arg): pass

                    def setup(bot):
                        bot.add_module(Talker(bot))
                        if {how!r} == "refused":
                            bot.add_module(Thief(bot))
                        raise RuntimeError("plugin bug")
                """})
                self.bot.load_plugins()
                self.assertIsNone(chatcmd.get("plugchat"))
                self.assertNotIn("Talker", self.bot.modules)

    def test_chat_builtin_left_by_on_unload_is_removed_on_unload(self):
        self.addCleanup(chatcmd.unregister, None, "plugchat")
        folder = self.plugin("chatty", {"__init__.py": """
            from core import Module, chatcmd

            class Talker(Module):
                def values(self, arg):
                    return {}

                def on_load(self):
                    chatcmd.register(self.bot, chatcmd.Builtin(
                        kind="plugchat", label="x", help="x", placeholders=frozenset(),
                        default_response="x", values=self.values))

            def setup(bot):
                bot.add_module(Talker(bot))
        """})
        self.bot.load_plugins()
        self.assertIsNotNone(chatcmd.get("plugchat"))
        shutil.rmtree(folder)
        self.bot.watch_plugins()
        self.assertIsNone(chatcmd.get("plugchat"))

    # ---------------------------------------------------------------- sys.path

    def test_missing_dependency_loads_once_the_vendor_folder_appears(self):
        vendor = paths.plugin_vendor_dir()
        shutil.rmtree(vendor, ignore_errors=True)
        saved = list(sys.path)
        self.addCleanup(sys.path.__setitem__, slice(None), saved)
        self.addCleanup(sys.modules.pop, "deckdep_x", None)
        sys.path[:] = [p for p in sys.path if p != vendor]
        self.plugin("needy", {"__init__.py": """
            from core import Module, action
            import deckdep_x

            class Needy(Module):
                @action("needy_go")
                def go(self, arg):
                    return deckdep_x.VALUE

            def setup(bot):
                bot.add_module(Needy(bot))
        """})
        self.bot.load_plugins()
        self.assertNotIn("needy_go", self.bot.actions)
        self.bot.watch_plugins()                       # nothing changed: still not loaded
        self.assertNotIn("needy_go", self.bot.actions)
        staged = os.path.join(os.path.dirname(vendor), ".plugin-vendor-new")
        write(staged, {"deckdep_x.py": "VALUE = 42\n"})
        os.replace(staged, vendor)                     # how the installer swaps it in
        self.bot.watch_plugins()
        self.assertEqual(self.bot.actions["needy_go"](None), 42)
        self.assertEqual(sys.path.index(vendor), sys.path.index(os.path.join(ROOT, "vendor")) + 1)


    def test_plugin_vendor_goes_right_after_app_vendor_once(self):
        vendor = paths.plugin_vendor_dir()
        os.makedirs(vendor, exist_ok=True)
        app_vendor = os.path.join(ROOT, "vendor")
        saved = list(sys.path)
        self.addCleanup(sys.path.__setitem__, slice(None), saved)
        sys.path[:] = [p for p in sys.path if p != vendor]
        self.bot.load_plugins()
        self.bot.watch_plugins()
        self.bot.load_plugins()
        self.assertEqual(sys.path.count(vendor), 1)
        self.assertEqual(sys.path.index(vendor), sys.path.index(app_vendor) + 1)
        self.assertIsInstance(sys.modules[plugins.PACKAGE].__path__, list)


if __name__ == "__main__":
    unittest.main()
