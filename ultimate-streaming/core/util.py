"""Small helpers shared by modules."""
import logging
import os
import shlex
import signal
import stat
import subprocess

from .paths import state_dir

log = logging.getLogger("deck")


# The app's own credentials and state, which a config's `*_file:` must never point read_secret() at
# (it would hand e.g. the Twitch tokens to whatever URL that module talks to).
OWN_SECRETS = ("twitch.json", "editor_token", "discord_webhooks.json", "discord_state.json", "alerts.json",
               "announce_state.json", "chatstats.db")
OWN_SECRET_DIRS = ("launch",)


def sh(cmd, timeout=5):
    """Run a shell command and return (exit code, stdout); (124, "") after `timeout` seconds.

    It runs in its own process group, and on timeout the whole group is killed: killing only
    the shell would leave e.g. `foo` of `foo | grep x` holding the output pipe open."""
    p = subprocess.Popen(cmd, shell=True, start_new_session=True, text=True,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, out.strip()
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.wait()
        p.stdout.close()   # not read: a process that left the group may still hold it open
        return 124, ""


def spawn(cmd):
    """Launch a command fully detached from the bot."""
    subprocess.Popen(cmd, shell=True, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _detached(argv):
    """Start argv (a list, no shell) detached from the bot. False if it couldn't be started."""
    try:
        subprocess.Popen(argv, start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError as e:
        log.warning("couldn't run %s: %s", argv[0], e)
        return False


def notify(msg):
    _detached(["notify-send", "--", "Stream Deck", str(msg)])


def open_url(url):
    url = str(url)
    if url.startswith("-"):   # xdg-open would take it as an option
        log.warning("not opening %r: not a URL", url[:80])
        return
    _detached(["xdg-open", url])


def _own_secret(path):
    """True if `path` is one of the app's own credential/state files (see OWN_SECRETS)."""
    real, base = os.path.realpath(path), os.path.realpath(state_dir())
    if os.path.dirname(real) == base and os.path.basename(real) in OWN_SECRETS:
        return True
    return any(real == os.path.join(base, d) or real.startswith(os.path.join(base, d) + os.sep)
               for d in OWN_SECRET_DIRS)


def read_secret(env_var, path=None):
    """A credential from the environment, else from a file (first line). None if neither is set.

    Credentials are never read from config.yaml itself, so the config can be shared safely.
    Warns if the file is readable by other users. Refuses (None, with a warning) the app's own
    unrelated credential files such as twitch.json.
    """
    if os.environ.get(env_var):
        return os.environ[env_var].strip()
    if not path:
        return None
    path = os.path.expanduser(path)
    if _own_secret(path):
        log.warning("not reading %s for %s: it holds the app's own credentials; point it at its own file",
                    path, env_var)
        return None
    try:
        mode = os.stat(path).st_mode
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            log.warning("%s is readable by other users; run: chmod 600 %s", path, shlex.quote(path))
        with open(path) as f:
            return f.readline().strip() or None
    except FileNotFoundError:
        log.warning("secret file %s does not exist", path)
        return None
