"""core/appstop.py: exact process matching (fake /proc), pidfd signalling of real child processes,
the Flatpak path, and the autostart override written only after the original is backed up.
Nothing here can touch a real program: every test uses a fake /proc root and fake runners."""
import os
import signal
import subprocess
import sys
import tarfile
import time
import unittest
from unittest import mock

from tests.helpers import tmpdir

from core import appstop, setup_backup  # noqa: E402
from core.safefs import state_home  # noqa: E402

SC_FLATPAK = "com.core447.StreamController"


def proc_entry(root, pid, argv=(), exe=None, ppid=1):
    d = os.path.join(root, str(pid))
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "cmdline"), "wb") as f:
        f.write(b"".join(a.encode() + b"\0" for a in argv))
    with open(os.path.join(d, "stat"), "w") as f:
        f.write(f"{pid} (x y) S {ppid} 0 0\n")
    if exe:
        os.symlink(exe, os.path.join(d, "exe"))
    return d


class Fake(appstop.System):
    """Records signals instead of sending them; `stubborn` pids ignore SIGTERM."""

    def __init__(self, root, stubborn=(), flatpak_ids=(), enabled_units=()):
        self.proc_root, self.stubborn = root, set(stubborn)
        self.flatpak_ids, self.enabled = list(flatpak_ids), set(enabled_units)
        self.signals, self.flatpak_calls, self.systemctl_calls = [], [], []

    def flatpak(self, args):
        self.flatpak_calls.append(list(args))
        if args[0] == "ps":
            return 0, "".join(i + "\n" for i in self.flatpak_ids)
        if args[0] == "kill":
            self.flatpak_ids = [i for i in self.flatpak_ids if i != args[1]]
            return 0, ""
        return 1, ""

    def systemctl(self, args):
        self.systemctl_calls.append(list(args))
        if args[0] == "is-enabled":
            return (0, "enabled\n") if args[1] in self.enabled else (1, "disabled\n")
        return 0, ""

    def open(self, pid):
        return pid

    def signal(self, handle, sig):
        self.signals.append((handle, sig))
        if sig == signal.SIGKILL:
            self.stubborn.discard(handle)

    def wait(self, handles, timeout):
        return [h for h in handles if h in self.stubborn]

    def close(self, handle):
        pass


class Matching(unittest.TestCase):
    def setUp(self):
        self.root = tmpdir()

    def test_exact_argv_and_exe_only(self):
        proc_entry(self.root, 101, ["/usr/bin/streamcontroller", "--daemon"])
        proc_entry(self.root, 102, ["streamcontroller-other"])
        proc_entry(self.root, 103, ["/opt/streamcontroller2"])
        proc_entry(self.root, 104, ["python3", "/usr/local/bin/streamcontroller"])
        proc_entry(self.root, 105, ["shotwell", "streamcontroller"])           # not an interpreter
        proc_entry(self.root, 106, ["bash", "-c", "streamcontroller"])         # argv[1] is "-c"
        proc_entry(self.root, 107, ["whatever"], exe="/usr/bin/streamcontroller")
        proc_entry(self.root, 108, ["streamcontroller --x"])
        fake = Fake(self.root)
        r = appstop.stop("streamcontroller", system=fake)
        self.assertEqual(sorted(p for p, _ in fake.signals), [101, 104, 107])
        self.assertTrue(all(s == signal.SIGTERM for _, s in fake.signals))
        self.assertEqual(r["stopped"], 3)
        self.assertEqual(r["still_running"], 0)

    def test_never_self_or_ancestors(self):
        proc_entry(self.root, os.getpid(), ["streamcontroller"], ppid=os.getppid())
        proc_entry(self.root, os.getppid(), ["streamcontroller"], ppid=1)
        proc_entry(self.root, 1, ["streamcontroller"], ppid=0)
        fake = Fake(self.root)
        self.assertFalse(appstop.running("streamcontroller", system=fake))
        self.assertEqual(appstop.stop("streamcontroller", system=fake)["stopped"], 0)
        self.assertEqual(fake.signals, [])

    def test_our_daemon_is_never_matched(self):
        proc_entry(self.root, 201, ["python3", "/home/u/ultimate-streaming/main.py"])  # privacy-check: allow
        proc_entry(self.root, 202, ["python3", "main.py", "--open-editor"])
        for kind in appstop.STOPPABLE:
            fake = Fake(self.root)
            appstop.stop(kind, system=fake)
            self.assertEqual(fake.signals, [], kind)

    def test_other_uid_ignored(self):
        proc_entry(self.root, 301, ["streamcontroller"])
        fake = Fake(self.root)
        with mock.patch.object(appstop.os, "getuid", return_value=os.getuid() + 1):
            self.assertEqual(appstop.stop("streamcontroller", system=fake)["stopped"], 0)
        self.assertEqual(fake.signals, [])
        appstop.stop("streamcontroller", system=fake)
        self.assertEqual(fake.signals, [(301, signal.SIGTERM)])

    def test_sigkill_only_with_force(self):
        proc_entry(self.root, 401, ["boatswain"])
        fake = Fake(self.root, stubborn=[401])
        r = appstop.stop("boatswain", system=fake)
        self.assertEqual(fake.signals, [(401, signal.SIGTERM)])
        self.assertEqual((r["stopped"], r["still_running"]), (0, 1))
        fake = Fake(self.root, stubborn=[401])
        r = appstop.stop("boatswain", force=True, system=fake)
        self.assertEqual(fake.signals, [(401, signal.SIGTERM), (401, signal.SIGKILL)])
        self.assertEqual((r["stopped"], r["still_running"]), (1, 0))

    def test_flatpak_kill_exact_id(self):
        fake = Fake(self.root, flatpak_ids=["com.core447.StreamControllerX", SC_FLATPAK])
        self.assertTrue(appstop.running("streamcontroller", system=fake))
        r = appstop.stop("streamcontroller", system=fake)
        self.assertIn(["kill", SC_FLATPAK], fake.flatpak_calls)
        self.assertEqual([c for c in fake.flatpak_calls if c[0] == "kill"], [["kill", SC_FLATPAK]])
        self.assertEqual(r["stopped"], 1)
        fake = Fake(self.root, flatpak_ids=["com.core447.StreamControllerX"])
        appstop.stop("streamcontroller", system=fake)
        self.assertFalse([c for c in fake.flatpak_calls if c[0] == "kill"])
        fake = Fake(self.root, flatpak_ids=[SC_FLATPAK])
        appstop.stop("streamdeck_ui", system=fake)      # streamdeck-ui has no Flatpak id
        self.assertFalse([c for c in fake.flatpak_calls if c[0] == "kill"])

    def test_unknown_kind(self):
        fake = Fake(self.root)
        for kind in ("obs", "elgato", "streamdeck", "", None, "../x"):
            with self.assertRaises(ValueError):
                appstop.stop(kind, system=fake)
        with self.assertRaises(ValueError):
            appstop.running("elgato", system=fake)
        with self.assertRaises(ValueError):
            appstop.stop("boatswain", force="yes", system=fake)
        self.assertEqual(fake.signals, [])


class RealProcesses(unittest.TestCase):
    """The default System (pidfd_open + pidfd_send_signal) against our own child processes, found
    through a fake /proc that lists only them."""

    def spawn(self, argv0, code="import time; time.sleep(60)"):
        p = subprocess.Popen([argv0, "-c", f"import sys; {code}; "], executable=sys.executable,
                             stdout=subprocess.PIPE)
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.wait()))
        self.addCleanup(p.stdout.close)
        end = time.monotonic() + 5
        argv = []
        while not argv[:1] == [argv0] and time.monotonic() < end:   # empty until the child has exec'd
            with open(f"/proc/{p.pid}/cmdline", "rb") as f:
                argv = [a.decode() for a in f.read().split(b"\0") if a]
            time.sleep(0.01)
        proc_entry(self.root, p.pid, argv, exe=sys.executable, ppid=os.getpid())
        return p

    def setUp(self):
        self.root = tmpdir()
        root = self.root

        class S(appstop.System):
            proc_root = root

            def flatpak(self, args):
                return None

            def systemctl(self, args):
                return None
        self.system = S()

    def test_sigterm_stops_match_only(self):
        target = self.spawn("streamcontroller")
        other = self.spawn("streamcontroller-other")
        r = appstop.stop("streamcontroller", system=self.system)
        self.assertEqual(r["stopped"], 1)
        self.assertEqual(target.wait(5), -signal.SIGTERM)
        self.assertIsNone(other.poll())

    def test_stubborn_needs_force(self):
        p = self.spawn("boatswain", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                                    "print('ready', flush=True); time.sleep(60)")
        self.assertEqual(p.stdout.readline().strip(), b"ready")
        with mock.patch.object(appstop, "TERM_WAIT", 0.3):
            r = appstop.stop("boatswain", system=self.system)
            self.assertEqual((r["stopped"], r["still_running"]), (0, 1))
            self.assertIsNone(p.poll())
            r = appstop.stop("boatswain", force=True, system=self.system)
        self.assertEqual((r["stopped"], r["still_running"]), (1, 0))
        self.assertEqual(p.wait(5), -signal.SIGKILL)

    def test_recycled_pid_rechecked(self):
        """The entry is re-read after the pidfd is open: a process that no longer matches is skipped."""
        p = self.spawn("streamcontroller")
        entry = os.path.join(self.root, str(p.pid))
        real_open = self.system.open

        def open_then_change(pid):
            fd = real_open(pid)
            with open(os.path.join(entry, "cmdline"), "wb") as f:
                f.write(b"bash\0")
            return fd
        self.system.open = open_then_change
        self.assertEqual(appstop.stop("streamcontroller", system=self.system)["stopped"], 0)
        time.sleep(0.1)
        self.assertIsNone(p.poll())


class Autostart(unittest.TestCase):
    def setUp(self):
        self.user = os.path.join(os.environ["XDG_CONFIG_HOME"], "autostart")
        os.makedirs(self.user, exist_ok=True)
        for n in os.listdir(self.user):
            os.unlink(os.path.join(self.user, n))
        self.sysdir = tmpdir()
        os.makedirs(os.path.join(self.sysdir, "autostart"))
        patcher = mock.patch.dict(os.environ, {"XDG_CONFIG_DIRS": self.sysdir})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fake = Fake(tmpdir())

    def write(self, path, text):
        with open(path, "w") as f:
            f.write(text)

    def read(self, path):
        with open(path, "rb") as f:
            return f.read()

    def archive_members(self, aid):
        path = os.path.join(state_home(), "backups", "external", aid + ".tar.gz")
        with tarfile.open(path) as tf:
            return {m.name: tf.extractfile(m).read() for m in tf.getmembers() if m.isreg()}

    def test_override_after_backup(self):
        user_file = os.path.join(self.user, "StreamController.desktop")
        sys_file = os.path.join(self.sysdir, "autostart", "com.core447.StreamController.desktop")
        self.write(user_file, "[Desktop Entry]\nExec=streamcontroller\n")
        self.write(sys_file, "[Desktop Entry]\nExec=flatpak run x\n")
        self.write(os.path.join(self.user, "streamcontroller-other.desktop"), "keep")
        seen = []
        real_create = setup_backup.create

        def create(items, **kw):
            seen.append(self.read(user_file))   # still the original when the backup is taken
            return real_create(items, **kw)
        with mock.patch.object(setup_backup, "create", side_effect=create):
            r = appstop.stop("streamcontroller", system=self.fake)
        self.assertEqual(seen[0], b"[Desktop Entry]\nExec=streamcontroller\n")
        self.assertEqual(sorted(r["autostart_disabled"]),
                         ["StreamController.desktop", "com.core447.StreamController.desktop"])
        for n in r["autostart_disabled"]:
            text = self.read(os.path.join(self.user, n)).decode()
            self.assertIn("\nHidden=true\n", text)
            self.assertTrue(text.startswith("[Desktop Entry]\n"))
        self.assertEqual(self.read(sys_file), b"[Desktop Entry]\nExec=flatpak run x\n")   # never touched
        self.assertEqual(self.read(os.path.join(self.user, "streamcontroller-other.desktop")), b"keep")
        members = self.archive_members(r["backup"])
        self.assertEqual(members["streamcontroller/StreamController.desktop"],
                         b"[Desktop Entry]\nExec=streamcontroller\n")
        listing = {a["id"]: a for a in setup_backup.list_archives()}
        self.assertTrue(listing[r["backup"]]["pinned"])
        self.assertEqual(listing[r["backup"]]["items"][0]["label"], appstop.AUTOSTART_LABEL)
        self.assertEqual(len(seen), 2)   # the user file and the system file, each archived
        # Again: already ours, so no new backup and nothing rewritten.
        before = len(setup_backup.list_archives())
        r2 = appstop.stop("streamcontroller", system=self.fake)
        self.assertIsNone(r2["backup"])
        self.assertEqual(len(setup_backup.list_archives()), before)

    def test_backup_failure_changes_nothing(self):
        user_file = os.path.join(self.user, "com.feaneron.Boatswain.desktop")
        self.write(user_file, "orig")
        proc_entry(self.fake.proc_root, 501, ["boatswain"])
        with mock.patch.object(setup_backup, "create", side_effect=setup_backup.BackupError("no space")):
            with self.assertRaises(setup_backup.BackupError):
                appstop.stop("boatswain", system=self.fake)
        self.assertEqual(self.read(user_file), b"orig")
        self.assertEqual(self.fake.signals, [])

    def test_symlink_outside_is_not_overwritten_unbacked(self):
        target = os.path.join(tmpdir(), "real.desktop")
        self.write(target, "elsewhere")
        os.symlink(target, os.path.join(self.user, "com.feaneron.Boatswain.desktop"))
        with self.assertRaises(appstop.StopError):
            appstop.stop("boatswain", system=self.fake)
        self.assertTrue(os.path.islink(os.path.join(self.user, "com.feaneron.Boatswain.desktop")))
        self.assertEqual(self.read(target), b"elsewhere")

    def test_nothing_installed_writes_nothing(self):
        r = appstop.stop("boatswain", system=self.fake)
        self.assertEqual((r["autostart_disabled"], r["backup"]), ([], None))
        self.assertEqual(os.listdir(self.user), [])

    def test_systemd_exact_unit(self):
        fake = Fake(tmpdir(), enabled_units=["streamdeck.service"])
        r = appstop.stop("streamdeck_ui", system=fake)
        self.assertIn(["disable", "streamdeck.service"], fake.systemctl_calls)
        self.assertEqual(r["autostart_disabled"], ["streamdeck.service"])
        fake = Fake(tmpdir())
        appstop.stop("streamdeck_ui", system=fake)
        self.assertFalse([c for c in fake.systemctl_calls if c[0] == "disable"])
        fake = Fake(tmpdir(), enabled_units=["streamdeck.service"])
        appstop.stop("streamcontroller", system=fake)
        self.assertEqual(fake.systemctl_calls, [])


if __name__ == "__main__":
    unittest.main()
