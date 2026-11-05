"""tools/plugin.py's logic (core/plugin_install.py) and tools/fetch_deps.py as a library: plugins are
linked from the catalog or copied after asking, their pinned dependencies share one folder that is
never left half-built, and anything that could shadow the app's own imports is refused.
No network: PyPI is a fake serving wheels built here."""
from tests import helpers  # noqa: F401  (sandboxes HOME / XDG_*)

import hashlib
import io
import json
import os
import unittest
import zipfile
from unittest import mock

from tests.helpers import tmpdir

from core import paths, plugin_install, plugins
from core.plugin_install import DepsError, InstallError
from core.plugins import ManifestError

fetch_deps = plugin_install.fetch_deps
REAL_FETCH = fetch_deps.fetch


def make_wheel(dist, version, files, top_level=None):
    """A wheel (zip bytes) holding files {path: text}, its dist-info, RECORD and top_level.txt."""
    info = f"{dist.replace('-', '_')}-{version}.dist-info"
    files = dict(files)
    files[f"{info}/METADATA"] = f"Name: {dist}\nVersion: {version}\n"
    if top_level is not None:
        files[f"{info}/top_level.txt"] = "\n".join(top_level) + "\n"
    files[f"{info}/RECORD"] = "".join(f"{p},,\n" for p in files) + f"{info}/RECORD,,\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for path, text in files.items():
            z.writestr(path, text)
    return buf.getvalue()


class FakePyPI:
    """Stands in for fetch_deps.fetch: JSON metadata and wheel downloads for the wheels added."""

    def __init__(self):
        self.wheels = {}     # (name, version) -> bytes
        self.calls = []
        self.fail = set()    # names whose download fails

    def add(self, dist, version, files, top_level=None):
        data = make_wheel(dist, version, files, top_level)
        self.wheels[(dist, version)] = data
        return hashlib.sha256(data).hexdigest()

    def line(self, dist, version, files, top_level=None):
        """A requirements.txt line pinning the wheel just added."""
        return f"{dist}=={version} --hash=sha256:{self.add(dist, version, files, top_level)}\n"

    def __call__(self, url):
        self.calls.append(url)
        assert url.startswith("https://")
        if url.startswith("https://pypi.org/pypi/"):
            name, version = url.split("/")[4:6]
            data = self.wheels[(name, version)]
            return json.dumps({"urls": [{"packagetype": "bdist_wheel", "url": f"https://files.test/{name}-{version}.whl",
                                         "digests": {"sha256": hashlib.sha256(data).hexdigest()}}]}).encode()
        name, version = url.rsplit("/", 1)[1][:-4].rsplit("-", 1)
        if name in self.fail:
            raise DepsError(f"can't download {url}: connection refused")
        return self.wheels[(name, version)]


def tree(folder):
    """{relative path: (is_link, content or link target)} of everything under folder."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(folder):
        for entry in dirnames + filenames:
            full = os.path.join(dirpath, entry)
            rel = os.path.relpath(full, folder)
            if os.path.islink(full):
                out[rel] = (True, os.readlink(full))
            elif os.path.isfile(full):
                with open(full, "rb") as f:
                    out[rel] = (False, f.read())
            else:
                out[rel] = (False, None)
    return out


class PluginInstallTest(unittest.TestCase):
    def setUp(self):
        self.base = tmpdir()
        for patcher in (mock.patch.dict(os.environ, {"XDG_DATA_HOME": os.path.join(self.base, "data")}),
                        mock.patch.object(plugins, "catalog_dir", lambda: self.catalog),
                        mock.patch.object(plugin_install, "APP_VENDOR", os.path.join(self.base, "app-vendor")),
                        mock.patch.object(fetch_deps, "fetch", self._fetch)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.catalog = os.path.join(self.base, "catalog")
        os.makedirs(self.catalog)
        os.makedirs(os.path.join(self.base, "app-vendor", "obsws_python"))
        os.makedirs(os.path.join(self.base, "app-vendor", "obsws_python-1.8.0.dist-info"))
        self.pypi = FakePyPI()
        self.out = []
        self.mqtt = self.pypi.line("paho-mqtt", "2.1.0", {"paho/__init__.py": "", "paho/mqtt.py": "X = 1\n"}, ["paho"])

    def _fetch(self, url):
        return self.pypi(url)

    def plugin(self, folder, name, requirements="", version="1.0.0"):
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "plugin.yaml"), "w") as f:
            f.write(f'name: {name}\nversion: "{version}"\ndescription: test plugin {name}\napi: 1\n')
        with open(os.path.join(folder, "__init__.py"), "w") as f:
            f.write("def setup(bot):\n    pass\n")
        if requirements:
            with open(os.path.join(folder, "requirements.txt"), "w") as f:
                f.write(requirements)
        return folder

    def catalog_plugin(self, name, requirements=""):
        return self.plugin(os.path.join(self.catalog, name), name, requirements)

    def install(self, source, **kw):
        kw.setdefault("out", self.out.append)
        return plugin_install.install(source, **kw)

    def entries(self):
        return sorted(os.listdir(paths.plugins_dir()))

    def vendor(self):
        return paths.plugin_vendor_dir()


class CatalogInstall(PluginInstallTest):
    def test_links_into_the_catalog_and_changes_nothing_there(self):
        self.catalog_plugin("printer", self.mqtt)
        before = tree(self.catalog)
        manifest = self.install("printer")
        self.assertEqual(manifest["name"], "printer")
        entry = os.path.join(paths.plugins_dir(), "printer")
        self.assertTrue(os.path.islink(entry))
        self.assertEqual(os.path.realpath(entry), os.path.realpath(os.path.join(self.catalog, "printer")))
        self.assertEqual(tree(self.catalog), before)
        self.assertTrue(os.path.isfile(os.path.join(self.vendor(), "paho", "mqtt.py")))
        self.assertEqual(self.entries(), ["printer"])
        [item] = plugins.installed()
        self.assertTrue(item["link"])
        self.assertIsNone(item["error"])

    def test_plugin_without_requirements_needs_no_vendor_folder(self):
        self.catalog_plugin("clock")
        self.install("clock")
        self.assertFalse(os.path.exists(self.vendor()))
        self.assertEqual(self.pypi.calls, [])

    def test_already_installed_is_refused(self):
        self.catalog_plugin("printer", self.mqtt)
        self.install("printer")
        with self.assertRaisesRegex(InstallError, "already installed; remove it first"):
            self.install("printer")
        copy = self.plugin(os.path.join(self.base, "elsewhere", "printer"), "printer")
        with self.assertRaisesRegex(InstallError, "already installed"):
            self.install(copy, yes=True)
        self.assertTrue(os.path.islink(os.path.join(paths.plugins_dir(), "printer")))

    def test_git_url_is_refused(self):
        for url in ("https://github.com/someone/plugin.git", "git@github.com:someone/plugin.git",  # privacy-check: allow
                    "git+https://example.com/x", "someone/plugin.git"):
            with self.assertRaisesRegex(InstallError, "git URL isn't supported yet; clone it"):
                self.install(url, yes=True)
        self.assertEqual(self.entries(), [])

    def test_unknown_name_is_refused(self):
        with self.assertRaisesRegex(InstallError, "no plugin called 'nope' in the catalog"):
            self.install("nope")


class PathInstall(PluginInstallTest):
    def setUp(self):
        super().setUp()
        self.source = self.plugin(os.path.join(self.base, "src", "myplug"), "myplug", self.mqtt)
        with open(os.path.join(self.source, "extra.py"), "w") as f:
            f.write("Y = 2\n")
        for skipped in ("__pycache__", ".git", "vendor"):
            os.makedirs(os.path.join(self.source, skipped))
            with open(os.path.join(self.source, skipped, "junk"), "w") as f:
                f.write("junk")

    def test_copies_after_confirmation(self):
        asked = []
        self.install(self.source, confirm=lambda q: asked.append(q) or "y")
        self.assertEqual(asked, ["Install it? [y/N] "])
        text = "\n".join(self.out)
        self.assertIn(self.source, text)
        self.assertIn("extra.py", text)
        self.assertIn(plugin_install.WARNING, text)
        self.assertNotIn("junk", text)
        entry = os.path.join(paths.plugins_dir(), "myplug")
        self.assertFalse(os.path.islink(entry))
        self.assertEqual(sorted(os.listdir(entry)), ["__init__.py", "extra.py", "plugin.yaml", "requirements.txt"])
        self.assertEqual(os.stat(entry).st_mode & 0o777, 0o700)
        self.assertTrue(os.path.isfile(os.path.join(self.vendor(), "paho", "mqtt.py")))
        [item] = plugins.installed()
        self.assertFalse(item["link"])
        self.assertIsNone(item["error"])

    def test_yes_skips_the_question(self):
        self.install(self.source, yes=True, confirm=lambda q: self.fail("asked"))
        self.assertEqual(self.entries(), ["myplug"])

    def test_refused_without_yes_or_confirmation(self):
        for answer in ("", "n", "no", "maybe"):
            with self.assertRaisesRegex(InstallError, "not installed"):
                self.install(self.source, confirm=lambda q: answer)

        def eof(q):
            raise EOFError
        with self.assertRaises(InstallError):
            self.install(self.source, confirm=eof)
        self.assertEqual(self.entries(), [])
        self.assertFalse(os.path.exists(self.vendor()))
        self.assertEqual(self.pypi.calls, [])

    def test_source_containing_a_symlink_is_refused(self):
        os.symlink(os.path.expanduser("~"), os.path.join(self.source, "home"))
        with self.assertRaisesRegex(InstallError, "symlink"):
            self.install(self.source, yes=True)
        self.assertEqual(self.entries(), [])
        self.assertEqual(self.pypi.calls, [])

    def test_remove_deletes_the_copy(self):
        self.install(self.source, yes=True)
        plugin_install.remove("myplug", out=self.out.append)
        self.assertEqual(self.entries(), [])
        self.assertTrue(os.path.isfile(os.path.join(self.source, "extra.py")))
        self.assertFalse(os.path.exists(self.vendor()))
        self.assertIn(plugin_install.RESTART, self.out[-1])


class Names(PluginInstallTest):
    def test_reserved_and_core_names_are_refused(self):
        for name in ("core", "tools", "deck_plugins", "obs"):
            folder = self.plugin(os.path.join(self.base, "src", name), name)
            with self.assertRaisesRegex(ManifestError, "already used by the app"):
                self.install(folder, yes=True)
        self.plugin(os.path.join(self.catalog, "vendor"), "vendor")
        with self.assertRaises(ManifestError):
            self.install("vendor")
        with self.assertRaises(ManifestError):
            plugin_install.remove("core")
        self.assertEqual(self.entries(), [])

    def test_catalog_folder_name_must_match_manifest(self):
        self.plugin(os.path.join(self.catalog, "printer"), "other")
        with self.assertRaisesRegex(ManifestError, "installed as 'printer'"):
            self.install("printer")


class DependencyChecks(PluginInstallTest):
    def setUp(self):
        super().setUp()
        self.catalog_plugin("good", self.mqtt)
        self.install("good")
        self.before = tree(self.vendor())

    def assert_refused(self, requirements, pattern):
        self.catalog_plugin("bad", requirements)
        with self.assertRaisesRegex(DepsError, pattern):
            self.install("bad")
        self.assertEqual(self.entries(), ["good"])
        self.assertEqual(tree(self.vendor()), self.before, "plugin-vendor must be untouched")
        self.assertFalse(os.path.exists(self.vendor() + ".new"))

    def test_stdlib_clash_refused(self):
        self.assert_refused(self.pypi.line("evil-json", "1.0", {"json/__init__.py": ""}, ["json"]),
                            r"evil-json \(needed by bad\) provides 'json', which is a Python standard library")

    def test_system_import_clash_refused(self):
        self.assert_refused(self.pypi.line("pyyaml-fork", "1.0", {"yaml.py": ""}, ["yaml"]),
                            "provides 'yaml', which is a system package the app imports")

    def test_app_vendor_clash_refused(self):
        self.assert_refused(self.pypi.line("obsws-fork", "1.0", {"obsws_python/__init__.py": ""}),
                            "provides 'obsws_python', which is a package the app ships in vendor/")

    def test_reserved_name_clash_refused(self):
        self.assert_refused(self.pypi.line("corelib", "1.0", {"core/__init__.py": ""}, ["corelib"]),
                            "provides 'core', which is part of the app itself")

    def test_top_level_txt_cannot_hide_what_the_wheel_unpacks(self):
        self.assert_refused(self.pypi.line("innocent", "1.0", {"innocent.py": "", "requests/__init__.py": ""},
                                           ["innocent"]),
                            "provides 'requests'")

    def test_two_wheels_providing_the_same_module_refused(self):
        self.assert_refused(self.pypi.line("paho-clone", "1.0", {"paho/__init__.py": ""}, ["paho"]),
                            "provides 'paho', which is also provided by paho-mqtt")

    def test_conflicting_versions_between_plugins_refused(self):
        newer = self.pypi.line("paho-mqtt", "2.2.0", {"paho/__init__.py": ""}, ["paho"])
        self.assert_refused(newer, r"plugins bad, good need conflicting dependencies: .*2\.1\.0.*2\.2\.0")

    def test_same_pin_in_two_plugins_is_fetched_once(self):
        self.catalog_plugin("also", self.mqtt)
        self.pypi.calls.clear()
        self.install("also")
        self.assertEqual(self.entries(), ["also", "good"])
        self.assertEqual(len([u for u in self.pypi.calls if u.endswith(".whl")]), 1)

    def test_failed_fetch_leaves_nothing_behind(self):
        self.pypi.fail.add("other")
        self.catalog_plugin("bad", self.pypi.line("other", "1.0", {"other.py": ""}))
        with self.assertRaisesRegex(DepsError, "connection refused"):
            self.install("bad")
        self.assertEqual(self.entries(), ["good"])
        self.assertEqual(tree(self.vendor()), self.before)
        source = self.plugin(os.path.join(self.base, "src", "bad2"), "bad2", self.pypi.line("other", "1.0", {"other.py": ""}))
        with self.assertRaises(DepsError):
            self.install(source, yes=True)
        self.assertEqual(self.entries(), ["good"], "no .staging-* and no plugin entry")

    def test_hash_mismatch_refused(self):
        line = self.pypi.line("other", "1.0", {"other.py": ""})
        self.pypi.add("other", "1.0", {"other.py": "tampered = True\n"})
        self.assert_refused(line, "doesn't match|no wheel on PyPI matches")

    def test_remove_unlinks_without_touching_the_catalog(self):
        before = tree(self.catalog)
        plugin_install.remove("good", out=self.out.append)
        self.assertEqual(self.entries(), [])
        self.assertEqual(tree(self.catalog), before)
        self.assertFalse(os.path.exists(self.vendor()), "no requirements left: plugin-vendor removed")
        self.assertIn("stay loaded until the service restarts", self.out[-1])

    def test_remove_keeps_the_other_plugins_dependencies(self):
        self.catalog_plugin("other", self.pypi.line("six", "1.0", {"six.py": ""}))
        self.install("other")
        self.assertTrue(os.path.isfile(os.path.join(self.vendor(), "six.py")))
        plugin_install.remove("other", out=self.out.append)
        self.assertFalse(os.path.exists(os.path.join(self.vendor(), "six.py")))
        self.assertTrue(os.path.isfile(os.path.join(self.vendor(), "paho", "mqtt.py")))

    def test_remove_of_missing_plugin_refused(self):
        with self.assertRaisesRegex(InstallError, "no plugin called 'gone' is installed"):
            plugin_install.remove("gone")

    def test_listing(self):
        self.catalog_plugin("spare")
        rows = {row["name"]: row for row in plugin_install.listing()}
        self.assertEqual(rows["good"]["state"], "linked")
        self.assertEqual(rows["good"]["version"], "1.0.0")
        self.assertEqual(rows["spare"]["state"], "available")


class FetchDepsLibrary(PluginInstallTest):
    def requirements(self, text):
        path = os.path.join(tmpdir(), "requirements.txt")
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_errors_raise_deps_error_not_system_exit(self):
        target = os.path.join(tmpdir(), "vendor")
        os.makedirs(target)
        with open(os.path.join(target, "keep.py"), "w") as f:
            f.write("")
        bad = [self.requirements("paho-mqtt>=2\n"),
               self.requirements('paho-mqtt==2.1.0 ; sys_platform == "linux" --hash=sha256:' + "0" * 64 + "\n"),
               self.requirements("paho-mqtt==2.1.0 --hash=sha256:" + "0" * 64 + "\n"),
               os.path.join(tmpdir(), "missing.txt")]
        for path in bad:
            try:
                fetch_deps.install([path], target)
            except DepsError:
                pass
            except SystemExit:
                self.fail(f"SystemExit from {path}")
            else:
                self.fail(f"no DepsError for {path}")
            self.assertEqual(os.listdir(target), ["keep.py"])
            self.assertFalse(os.path.exists(target + ".new"))

    def test_non_https_refused(self):
        with self.assertRaisesRegex(DepsError, "non-HTTPS"):
            REAL_FETCH("http://pypi.org/pypi/paho-mqtt/2.1.0/json")

    def test_install_replaces_target_and_dedupes(self):
        target = os.path.join(tmpdir(), "vendor")
        os.makedirs(target)
        with open(os.path.join(target, "stale.py"), "w") as f:
            f.write("")
        a, b = self.requirements(self.mqtt), self.requirements("# same pin\n" + self.mqtt)
        wheels = fetch_deps.install([a, b], target)
        self.assertEqual([(w["name"], w["version"], w["top_level"]) for w in wheels], [("paho-mqtt", "2.1.0", {"paho"})])
        self.assertEqual(sorted(os.listdir(target)), ["paho", "paho_mqtt-2.1.0.dist-info"])
        self.assertFalse(os.path.exists(target + ".new") or os.path.exists(target + ".old"))

    def test_same_name_two_versions_names_both(self):
        a = self.requirements(self.mqtt)
        b = self.requirements(self.pypi.line("Paho_MQTT", "2.2.0", {"paho/__init__.py": ""}))
        with self.assertRaisesRegex(DepsError, r"pinned to two versions: 2\.1\.0 .* and 2\.2\.0"):
            fetch_deps.install([a, b], os.path.join(tmpdir(), "vendor"))

    def test_top_level_without_top_level_txt(self):
        data = make_wheel("multi", "1.0", {"alpha/__init__.py": "", "beta.py": "", "_speedups.cpython-314-x86_64-linux-gnu.so": "",
                                           "multi-1.0.data/scripts/tool": "", "LICENSE": ""})
        self.assertEqual(fetch_deps.top_level_names(data), {"alpha", "beta", "_speedups"})



if __name__ == "__main__":
    unittest.main()
