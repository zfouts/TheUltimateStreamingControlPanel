"""install.sh --uninstall and --purge, run for real in a throw-away HOME. No install is done (that
needs the network); the folders an install would have left are created by hand. systemctl is
hidden by a PATH holding only the tools the script needs, so no user service is touched.
FullInstall (opt-in: US_FULL_INSTALL_TEST=1, needs the network) runs the whole install, uninstall and purge."""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(ROOT, "install.sh")
BASH = shutil.which("bash")
TOOLS = ("sed", "rm", "cat", "dirname", "id", "readlink", "grep", "mkdir", "head")


@unittest.skipUnless(BASH, "bash not available")
class InstallScript(unittest.TestCase):
    def setUp(self):
        self.home = os.path.realpath(tempfile.mkdtemp(prefix="install-sh-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        bindir = os.path.join(self.home, ".fakebin")      # only the tools install.sh needs: no systemctl
        os.mkdir(bindir)
        for t in TOOLS:
            if shutil.which(t):
                os.symlink(shutil.which(t), os.path.join(bindir, t))
        self.bindir = bindir
        self.conf = os.path.join(self.home, ".config", "ultimate-streaming")
        self.state = os.path.join(self.home, ".local", "state", "ultimate-streaming")
        self.data = os.path.join(self.home, ".local", "share", "ultimate-streaming")
        for d, f in ((self.conf, "config.yaml"), (self.state, "backups/external/a.tar"),
                     (self.data, "plugin-vendor/x.py")):
            os.makedirs(os.path.dirname(os.path.join(d, f)))
            with open(os.path.join(d, f), "w") as fh:
                fh.write("x")
        self.legacy = os.path.join(self.home, ".config", "streamdeck")
        os.symlink(self.conf, self.legacy)
        self.unit = os.path.join(self.home, ".config", "systemd", "user", "ultimate-streaming.service")
        os.makedirs(os.path.dirname(self.unit))
        open(self.unit, "w").close()

    def run_sh(self, *args, stdin="", env=None):
        e = {"HOME": self.home, "PATH": self.bindir, "XDG_CONFIG_HOME": "", "XDG_DATA_HOME": "",
             "XDG_STATE_HOME": "", **(env or {})}
        return subprocess.run([BASH, INSTALL, *args], input=stdin, capture_output=True, text=True,
                              env=e, cwd=self.home, timeout=60)

    def test_uninstall_keeps_data_and_says_where(self):
        r = self.run_sh("--uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(self.unit))
        self.assertFalse(os.path.lexists(self.legacy))          # our old-name link is gone
        for d in (self.conf, self.state, self.data):
            self.assertTrue(os.path.isdir(d))
            self.assertIn(d, r.stdout)
        self.assertIn("stream key", r.stdout)

    def test_uninstall_leaves_foreign_streamdeck_link(self):
        os.remove(self.legacy)
        other = os.path.join(self.home, "elsewhere")
        os.mkdir(other)
        os.symlink(other, self.legacy)
        self.assertEqual(self.run_sh("--uninstall").returncode, 0)
        self.assertTrue(os.path.islink(self.legacy))

    def test_purge_yes_deletes_everything(self):
        r = self.run_sh("--purge", "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        for d in (self.conf, self.state, self.data, self.unit, self.legacy):
            self.assertFalse(os.path.lexists(d), d)
        self.assertTrue(os.path.isdir(os.path.join(self.home, ".config")))

    def test_purge_asks_and_needs_yes(self):
        r = self.run_sh("--purge", stdin="no\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(os.path.isdir(self.conf) and os.path.isdir(self.state) and os.path.isdir(self.data))
        r = self.run_sh("--purge", stdin="")                    # no answer at all: nothing deleted
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(os.path.isdir(self.state))
        r = self.run_sh("--purge", stdin="yes\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(self.state))

    def test_purge_refuses_folders_outside_home(self):
        outside = os.path.realpath(tempfile.mkdtemp(prefix="install-sh-out-"))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        open(os.path.join(outside, "keep"), "w").close()
        shutil.rmtree(self.state)
        os.symlink(outside, self.state)                         # a state dir that resolves outside HOME
        r = self.run_sh("--purge", "--yes")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Refusing", r.stderr)
        self.assertTrue(os.path.exists(os.path.join(outside, "keep")))
        self.assertTrue(os.path.isdir(self.conf))               # nothing was deleted

    def test_purge_refuses_xdg_pointing_outside_home(self):
        outside = os.path.realpath(tempfile.mkdtemp(prefix="install-sh-out-"))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        os.mkdir(os.path.join(outside, "ultimate-streaming"))
        r = self.run_sh("--purge", "--yes", env={"XDG_STATE_HOME": outside})
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(os.path.isdir(os.path.join(outside, "ultimate-streaming")))
        self.assertTrue(os.path.isdir(self.conf))

    def test_yes_alone_is_refused_and_unknown_option_too(self):
        self.assertEqual(self.run_sh("--yes").returncode, 2)
        self.assertEqual(self.run_sh("--bogus").returncode, 2)
        self.assertTrue(os.path.isdir(self.conf))

    def test_help_lists_purge(self):
        r = self.run_sh("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("--purge", r.stdout)
        self.assertIn("--uninstall", r.stdout)


FULL_TOOLS = TOOLS + ("cp", "chmod", "find", "basename", "env", "sh", "ls", "uname", "tr", "sort", "date", "awk")


@unittest.skipUnless(BASH, "bash not available")
@unittest.skipUnless(os.environ.get("US_FULL_INSTALL_TEST") == "1",
                     "set US_FULL_INSTALL_TEST=1 to run a real install (fetches dependencies from PyPI)")
class FullInstall(unittest.TestCase):
    def test_install_uninstall_purge(self):
        home = os.path.realpath(tempfile.mkdtemp(prefix="install-full-"))
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        bindir = os.path.join(home, ".fakebin")      # no systemctl: nothing outside HOME is touched
        os.mkdir(bindir)
        for t in FULL_TOOLS:
            if shutil.which(t):
                os.symlink(shutil.which(t), os.path.join(bindir, t))
        os.symlink(sys.executable, os.path.join(bindir, "python3"))
        env = {"HOME": home, "PATH": bindir, "XDG_CONFIG_HOME": "", "XDG_DATA_HOME": "", "XDG_STATE_HOME": "",
               "PYTHON": sys.executable}

        def run(*args, **kw):
            return subprocess.run(list(args), capture_output=True, text=True, env=env, cwd=home, timeout=600, **kw)

        r = run(BASH, INSTALL, "--no-service")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        conf = os.path.join(home, ".config", "ultimate-streaming")
        self.assertTrue(os.path.isfile(os.path.join(conf, "config.yaml")))
        self.assertTrue(os.path.isfile(os.path.join(home, ".local", "share", "applications", "ultimate-streaming.desktop")))
        self.assertTrue(os.path.isdir(os.path.join(ROOT, "vendor")))
        r = run(sys.executable, os.path.join(ROOT, "main.py"), "--version")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ultimate-streaming", r.stdout)

        self.assertEqual(run(BASH, INSTALL, "--uninstall").returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(home, ".local", "share", "applications", "ultimate-streaming.desktop")))
        self.assertTrue(os.path.isdir(conf))                    # data is kept
        r = run(BASH, INSTALL, "--purge", "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(conf))


if __name__ == "__main__":
    unittest.main()
