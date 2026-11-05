"""tools/doctor.py check_plugins: reads plugins, never runs them, never creates folders."""
import importlib.util
import os
import shutil
import unittest

from tests import helpers  # noqa: F401  (sandbox first)
from core import paths

spec = importlib.util.spec_from_file_location("doctor_tool", os.path.join(helpers.ROOT, "tools", "doctor.py"))
doctor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(doctor)

DATA = os.path.join(os.environ["XDG_DATA_HOME"], "ultimate-streaming")
HASH = "--hash=sha256:" + "a" * 64


class CheckPluginsTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(DATA, ignore_errors=True)
        self.lines = []
        self._orig = doctor.report
        doctor.report = lambda level, what, hint="": self.lines.append((level, what, hint))
        self.addCleanup(setattr, doctor, "report", self._orig)
        self.addCleanup(shutil.rmtree, DATA, True)

    def make(self, name, init="", requirements=None, commands=None, manifest=None):
        folder = os.path.join(DATA, "plugins", name)
        os.makedirs(folder)
        with open(os.path.join(folder, "__init__.py"), "w") as f:
            f.write(init)
        if manifest is None:
            manifest = f'name: {name}\nversion: "1.2.3"\ndescription: test\napi: 1\n'
            if commands:
                manifest += "commands:\n" + "".join(f"  {k}: {v}\n" for k, v in commands.items())
        with open(os.path.join(folder, "plugin.yaml"), "w") as f:
            f.write(manifest)
        if requirements:
            with open(os.path.join(folder, "requirements.txt"), "w") as f:
                f.write(requirements)
        return folder

    def levels(self):
        return [(lvl, what) for lvl, what, _ in self.lines]

    def test_no_folder_creates_nothing(self):
        doctor.check_plugins()
        self.assertEqual(self.levels(), [("ok", "no plugins installed")])
        self.assertFalse(os.path.exists(DATA))

    def test_empty_folder(self):
        os.makedirs(os.path.join(DATA, "plugins"))
        doctor.check_plugins()
        self.assertEqual(self.levels(), [("ok", "no plugins installed")])

    def test_valid_plugin(self):
        self.make("good")
        doctor.check_plugins()
        self.assertEqual(self.levels(), [("ok", "plugin good 1.2.3")])
        self.assertFalse(os.path.exists(os.path.join(DATA, "plugin-vendor")))

    def test_dependency_present_and_missing(self):
        self.make("dep", requirements=f"Foo.Bar==1.0 {HASH}\nbaz==2.0 {HASH}\n")
        vendor = paths.plugin_vendor_dir()
        os.makedirs(os.path.join(vendor, "foo_bar-1.0.dist-info"))
        doctor.check_plugins()
        self.assertIn(("FAIL", "plugin dep: dependency baz 2.0 missing"), self.levels())
        self.assertNotIn(("FAIL", "plugin dep: dependency Foo.Bar 1.0 missing"), self.levels())
        self.assertEqual(sum(1 for lvl, _ in self.levels() if lvl == "FAIL"), 1)
        self.assertEqual(self.lines[-1][2], "python3 tools/plugin.py deps")
        os.makedirs(os.path.join(vendor, "baz-2.0.dist-info"))
        self.lines.clear()
        doctor.check_plugins()
        self.assertEqual(self.levels(), [("ok", "plugin dep 1.2.3")])

    def test_manifest_error_warns(self):
        self.make("bad", manifest="name: other\nversion: '1'\ndescription: x\napi: 1\n")
        doctor.check_plugins()
        level, what, hint = self.lines[0]
        self.assertEqual(level, "warn")
        self.assertTrue(what.startswith("plugin bad: "))
        self.assertEqual(hint, "python3 tools/plugin.py remove bad, then install it again")

    def test_commands(self):
        self.make("cmd", commands={"sh": "shell things", "no-such-program-xyz": "nothing"})
        doctor.check_plugins()
        self.assertIn(("ok", "sh (shell things)"), self.levels())
        self.assertIn(("warn", "no-such-program-xyz missing: nothing"), self.levels())

    def test_plugin_code_never_runs(self):
        marker = os.path.join(helpers.SANDBOX, "ran")
        self.make("evil", init=f"open({marker!r}, 'w').close()\nraise RuntimeError('executed')\n")
        doctor.check_plugins()
        self.assertEqual(self.levels(), [("ok", "plugin evil 1.2.3")])
        self.assertFalse(os.path.exists(marker))
        self.assertNotIn("deck_plugins.evil", __import__("sys").modules)


if __name__ == "__main__":
    unittest.main()
