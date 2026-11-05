"""Stop another Stream Deck program that holds the deck, and turn off its autostart.

Only ever on the user's explicit click in the panel (onboarding: "StreamController is running and
holds the deck — stop it and turn off its autostart?"). What may be stopped is a fixed list
(STOPPABLE); how a process is recognized is detect.RUNNING_MATCH, matched exactly:

  * Flatpak apps: `flatpak kill <exact app id>`, only when `flatpak ps` lists that exact id.
  * Native processes: a /proc scan, same uid only, matched on the exact basename of
    /proc/<pid>/exe or of argv[0] (argv[1] behind an interpreter named exactly python*, bash, sh,
    dash or zsh). Never a substring match, never `pkill -f`. Our own process and its ancestors are
    never signalled. Each process is opened as a pidfd and re-checked through that pidfd's /proc
    entry before any signal, so a recycled pid can't be hit; SIGTERM, wait 5 s, and SIGKILL only
    with force=True (the panel's second confirmation).

Autostart: the exact .desktop names each program installs (AUTOSTART) are overridden in
~/.config/autostart/ with `Hidden=true` (XDG autostart: a user file hides the system file of the
same name). The user's own file, and any system file of that name, are archived into the external
backups (core/setup_backup.py, pinned; the newest KEEP_PINNED per program stay pinned) first; if
that backup fails nothing is changed. systemd user
units are disabled only by their exact known names (UNITS), and only when enabled.

Sources for the names (format notes in the onboarding scratchpad formats/*.md describe the data
files only; autostart was taken from upstream sources):
  StreamController  autostart.py setup_autostart_desktop_entry(): $HOME/.config/autostart/
                    StreamController.desktop; its Flatpak asks the Background portal, which writes
                    $XDG_CONFIG_HOME/autostart/com.core447.StreamController.desktop.
  streamdeck-ui     docs/installation/systemd.md: user unit `streamdeck.service`; README: "add
                    `streamdeck` to your autostart list" → streamdeck.desktop (inferred).
  Boatswain         bs-application.c requests the Background portal (ACTIVATABLE); a portal
                    autostart entry is com.feaneron.Boatswain.desktop.
"""
import logging
import os
import re
import secrets
import select
import shutil
import signal
import subprocess
import time

from . import setup_backup
from .detect import RUNNING_MATCH

log = logging.getLogger("deck")

STOPPABLE = ("streamcontroller", "streamdeck_ui", "boatswain")
AUTOSTART = {
    "streamcontroller": ("StreamController.desktop", "com.core447.StreamController.desktop"),
    "streamdeck_ui": ("streamdeck.desktop",),
    "boatswain": ("com.feaneron.Boatswain.desktop",),
}
UNITS = {"streamcontroller": (), "streamdeck_ui": ("streamdeck.service",), "boatswain": ()}
NAMES = {"streamcontroller": "StreamController", "streamdeck_ui": "streamdeck-ui", "boatswain": "Boatswain"}
AUTOSTART_LABEL = "autostart files"   # item label of the pre-override backups (never restored by the panel)
KEEP_PINNED = 5                       # newest autostart archives per kind kept pinned; older ones may be pruned

TERM_WAIT = 5.0
KILL_WAIT = 2.0
_INTERPRETER = re.compile(r"(python[0-9.]*|bash|sh|dash|zsh)")
_MAX_CMDLINE = 4096


class StopError(Exception):
    """Stopping or disabling autostart was refused or failed. The message is safe to show."""


def override(kind):
    """The bytes of our Hidden=true override for kind."""
    return (f"[Desktop Entry]\nType=Application\nName={NAMES[kind]}\n"
            f"Comment=Autostart turned off by Ultimate Streaming (the original is in its external backups)\n"
            f"Hidden=true\n").encode()


class System:
    """The operating-system side, replaceable in tests: the /proc root, the `flatpak` and
    `systemctl --user` runners, and opening / signalling / waiting for a process."""

    proc_root = "/proc"

    def flatpak(self, args):
        """(returncode, stdout) of `flatpak <args>`, or None when flatpak isn't installed."""
        return self._run("flatpak", args)

    def systemctl(self, args):
        """(returncode, stdout) of `systemctl --user <args>`, or None when systemctl is missing."""
        return self._run("systemctl", ["--user", *args])

    @staticmethod
    def _run(prog, args):
        exe = shutil.which(prog)
        if not exe:
            return None
        try:
            r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.returncode, r.stdout

    def open(self, pid):
        """A handle that pins the process (a pidfd). OSError if it is gone."""
        return os.pidfd_open(pid)

    def signal(self, handle, sig):
        signal.pidfd_send_signal(handle, sig)

    def wait(self, handles, timeout):
        """Wait up to timeout s for the processes to exit; returns the handles still alive."""
        alive = list(handles)
        end = time.monotonic() + timeout
        while alive:
            left = end - time.monotonic()
            if left <= 0:
                break
            p = select.poll()
            for h in alive:
                p.register(h, select.POLLIN)
            done = {fd for fd, _ in p.poll(int(left * 1000) + 1)}
            alive = [h for h in alive if h not in done]
        return alive

    def close(self, handle):
        try:
            os.close(handle)
        except OSError:
            pass


def _check(kind, allowed=STOPPABLE):
    if not isinstance(kind, str) or kind not in allowed:
        raise ValueError("unknown app")
    return kind


# ------------------------------------------------------------------ processes

def _argv(base):
    try:
        with open(os.path.join(base, "cmdline"), "rb") as f:
            raw = f.read(_MAX_CMDLINE)
    except OSError:
        return []
    return [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]


def _is(base, m, uid):
    """True if the /proc/<pid> entry at base is ours (uid) and exactly matches RUNNING_MATCH entry m."""
    try:
        if os.stat(base).st_uid != uid:
            return False
    except OSError:
        return False
    try:
        exe = os.path.basename(os.readlink(os.path.join(base, "exe")))
    except OSError:
        exe = ""
    if exe and exe in m["exe"]:
        return True
    argv = _argv(base)
    if not argv:
        return False
    cands = [argv[0]]
    if len(argv) > 1 and _INTERPRETER.fullmatch(os.path.basename(argv[0])):
        cands.append(argv[1])
    for p in m["argv"]:
        if "/" in p:
            if p in cands:
                return True
        elif p in (os.path.basename(c) for c in cands):
            return True
    return False


def _ppid(proc_root, pid):
    try:
        with open(os.path.join(proc_root, str(pid), "stat"), "rb") as f:
            raw = f.read(4096)
        return int(raw[raw.rindex(b")") + 2:].split()[1])
    except (OSError, ValueError, IndexError):
        return 0


def _protected(proc_root):
    """Our pid and every ancestor's: never signalled."""
    out = {1, os.getpid(), os.getppid()}
    for root in {proc_root, "/proc"}:
        pid = os.getppid()
        for _ in range(64):
            pid = _ppid(root, pid)
            if pid <= 1 or pid in out:
                break
            out.add(pid)
    return out


def _pids(kind, system):
    root, m, uid = system.proc_root, RUNNING_MATCH[kind], os.getuid()
    skip = _protected(root)
    try:
        names = os.listdir(root)
    except OSError:
        return []
    return sorted(int(n) for n in names
                  if n.isdigit() and int(n) not in skip and _is(os.path.join(root, n), m, uid))


def _flatpak_running(app_id, system):
    if not app_id:
        return False
    r = system.flatpak(["ps", "--columns=application"])
    if not r or r[0] != 0:
        return False
    return app_id in {ln.strip() for ln in r[1].splitlines()}


def running(kind, *, system=None):
    """Whether the program holds a running process of ours. kind: a STOPPABLE kind, or "obs"
    (read-only check, used before restoring OBS files; OBS is never stopped here)."""
    _check(kind, STOPPABLE + ("obs",))
    system = system or System()
    return bool(_pids(kind, system)) or _flatpak_running(RUNNING_MATCH[kind]["flatpak"], system)


def _terminate(kind, force, system):
    """(stopped, still_running) for the native processes of kind."""
    m, uid, root = RUNNING_MATCH[kind], os.getuid(), system.proc_root
    handles = []
    try:
        for pid in _pids(kind, system):
            try:
                h = system.open(pid)
            except OSError:
                continue            # already gone
            # The pidfd pins this exact process: check again that it is still the one we matched.
            if not _is(os.path.join(root, str(pid)), m, uid):
                system.close(h)
                continue
            handles.append((pid, h))
        sent = []
        for pid, h in handles:
            try:
                system.signal(h, signal.SIGTERM)
                sent.append(h)
            except ProcessLookupError:
                continue
            except OSError as e:
                log.warning("couldn't stop %s (pid %d): %s", kind, pid, e.strerror or type(e).__name__)
        alive = system.wait(sent, TERM_WAIT) if sent else []
        if alive and force:
            for h in alive:
                try:
                    system.signal(h, signal.SIGKILL)
                except OSError:
                    pass
            alive = system.wait(alive, KILL_WAIT)
        return len(sent) - len(alive), len(alive)
    finally:
        for _, h in handles:
            system.close(h)


# ------------------------------------------------------------------ autostart

def _user_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "autostart")


def _system_dirs():
    dirs = [d for d in (os.environ.get("XDG_CONFIG_DIRS") or "/etc/xdg").split(":") if os.path.isabs(d)]
    return [os.path.join(d, "autostart") for d in dirs]


def _same(path, data):
    try:
        if not os.path.isfile(path) or os.path.islink(path) or os.path.getsize(path) != len(data):
            return False
        with open(path, "rb") as f:
            return f.read(len(data) + 1) == data
    except OSError:
        return False


def _backup(kind, root, names, label):
    """Archive root/<names> (pinned: the only copy of what we overwrite). Raises unless every file
    made it into the archive."""
    entry = setup_backup.create([{"kind": kind, "root": root, "include": list(names), "label": AUTOSTART_LABEL}],
                                label=label, auto=True, pinned=True)
    got = sum(it["files"] for it in entry["items"])
    if got != len(names):
        raise StopError(f"couldn't back up {NAMES[kind]}'s autostart file first; autostart not changed")
    return entry["id"]


def _unpin_old(kind):
    """Keep the newest KEEP_PINNED autostart archives of kind pinned; unpin the rest (they then
    follow the normal retention of automatic backups)."""
    mine = [a for a in setup_backup.list_archives()          # newest first
            if a["pinned"] and a["items"] and all(i["kind"] == kind and i["label"] == AUTOSTART_LABEL
                                                  for i in a["items"])]
    for a in mine[KEEP_PINNED:]:
        try:
            setup_backup.pin(a["id"], False)
        except (OSError, ValueError, setup_backup.BackupError):
            log.warning("couldn't unpin old autostart backup %s", a["id"])


def _write(path, data):
    d = os.path.dirname(path)
    tmp = os.path.join(d, f".{os.path.basename(path)}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)     # replaces a symlink itself, never writes through it
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _disable_autostart(kind, system):
    """(backup id | None, [names disabled]). Backs up first; nothing is written if that fails."""
    data = override(kind)
    user = _user_dir()
    names = AUTOSTART[kind]
    in_user = [n for n in names if os.path.lexists(os.path.join(user, n))]
    in_system = {d: [n for n in names if os.path.isfile(os.path.join(d, n))] for d in _system_dirs()}
    in_system = {d: ns for d, ns in in_system.items() if ns}
    targets = [n for n in names if n in in_user or any(n in ns for ns in in_system.values())]
    todo = [n for n in targets if not _same(os.path.join(user, n), data)]   # ours already: nothing to do
    backup = None
    if todo:
        label = f"before-stop-{kind.replace('_', '-')}"
        originals = [n for n in in_user if n in todo]
        if originals:
            backup = _backup(kind, os.path.realpath(user), originals, label)
        for d, ns in in_system.items():
            ns = [n for n in ns if n in todo]
            if ns:
                bid = _backup(kind, os.path.realpath(d), ns, label)
                backup = backup or bid
        if backup:
            _unpin_old(kind)
        os.makedirs(user, mode=0o755, exist_ok=True)
        for n in todo:
            _write(os.path.join(user, n), data)
            log.info("autostart of %s turned off (%s)", NAMES[kind], n)
    disabled = list(targets)
    for unit in UNITS[kind]:
        r = system.systemctl(["is-enabled", unit])
        if r and r[1].strip() == "enabled":
            r = system.systemctl(["disable", unit])
            if r and r[0] == 0:
                disabled.append(unit)
                log.info("systemd user unit %s disabled", unit)
            else:
                log.warning("couldn't disable systemd user unit %s", unit)
    return backup, disabled


# ------------------------------------------------------------------ entry point

def stop(kind, *, force=False, system=None):
    """Turn off kind's autostart (after backing up its files), then stop it.

    Returns {"stopped": n processes/instances, "autostart_disabled": [names], "backup": id | None,
    "still_running": n}; still_running > 0 means it ignored SIGTERM and force=True is needed.
    ValueError for an unknown kind; StopError / setup_backup.BackupError when the backup fails
    (then nothing was changed or stopped)."""
    _check(kind)
    if not isinstance(force, bool):
        raise ValueError("force must be true or false")
    system = system or System()
    backup, disabled = _disable_autostart(kind, system)
    stopped = still = 0
    app_id = RUNNING_MATCH[kind]["flatpak"]
    if _flatpak_running(app_id, system):
        r = system.flatpak(["kill", app_id])
        end = time.monotonic() + TERM_WAIT
        while r and r[0] == 0 and _flatpak_running(app_id, system) and time.monotonic() < end:
            time.sleep(0.2)
        if _flatpak_running(app_id, system):
            still += 1
        else:
            stopped += 1
    n, left = _terminate(kind, force, system)
    stopped, still = stopped + n, still + left
    log.info("%s: %d stopped, %d still running", NAMES[kind], stopped, still)
    return {"stopped": stopped, "autostart_disabled": disabled, "backup": backup, "still_running": still}
