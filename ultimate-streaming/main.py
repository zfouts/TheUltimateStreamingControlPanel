#!/usr/bin/env python3
"""Stream Deck daemon: loads every module in modules/ and runs the deck.

    python3 main.py                 run the daemon (normally done by the systemd user service)
    python3 main.py --open-editor   open the web editor in your browser
    python3 main.py --editor-url    print a one-time link to the web editor (valid for a minute)
    python3 main.py --version       print the version

config.yaml and module files are hot-reloaded on save; no restart needed.
Config is read from $DECK_CONFIG or ~/.config/ultimate-streaming/config.yaml (see README.md).
"""
import argparse
import logging
import os
import signal
import subprocess
import sys

if sys.version_info < (3, 14):
    sys.exit("The Ultimate Streaming Control Panel needs Python 3.14 or newer "
             f"(found {sys.version_info.major}.{sys.version_info.minor}). Tip: pyenv install 3.14")

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "vendor"))
sys.path.insert(0, ROOT)

from core.paths import config_path, migrate_legacy  # noqa: E402


def editor(open_browser):
    import yaml

    from core import editor_auth
    try:
        cfg = yaml.safe_load(open(config_path())) or {}
    except (OSError, yaml.YAMLError):
        cfg = {}
    try:
        # Checks that the daemon (a socket of this user's) holds the port, then mints a one-time
        # launch code: the URL, and so xdg-open's command line, never carries the access token.
        url = editor_auth.url(cfg)
    except editor_auth.Unavailable as e:
        sys.exit(str(e))
    except OSError as e:
        sys.exit(f"couldn't make a link for the control panel: {e}")
    if not open_browser:
        print(url)
        return
    subprocess.Popen(["xdg-open", url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    print("Opened the editor in your browser.")


_lock = None


def single_instance():
    """Two daemons would fight over the deck, so only one may run per user."""
    global _lock
    import fcntl

    from core.paths import state_file
    _lock = open(state_file("daemon.lock"), "a+")
    try:
        fcntl.flock(_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        _lock.seek(0)
        pid = _lock.read().strip()
        sys.exit(f"The Stream Deck daemon is already running{f' (pid {pid})' if pid else ''}.\n"
                 "If it's the service:  systemctl --user stop ultimate-streaming   (then run this again)")
    _lock.seek(0)
    _lock.truncate()
    _lock.write(f"{os.getpid()}\n")
    _lock.flush()


def main():
    from core import __version__
    ap = argparse.ArgumentParser(description="Stream Deck daemon")
    ap.add_argument("--open-editor", action="store_true", help="open the web editor in your browser")
    ap.add_argument("--editor-url", action="store_true", help="print a one-time link to the web editor (valid for a minute)")
    ap.add_argument("--version", action="version", version=f"ultimate-streaming {__version__}")
    args = ap.parse_args()
    try:
        moved = migrate_legacy()   # ~/.config/streamdeck → ~/.config/ultimate-streaming (older versions)
        if moved:
            print(f"moved your settings to {moved} (the old folder now links there)", file=sys.stderr)
    except OSError as e:
        print(f"couldn't move the old settings folder: {e}", file=sys.stderr)
    if args.open_editor or args.editor_url:
        return editor(args.open_editor)

    from core import DeckBot
    logging.basicConfig(level=os.environ.get("DECK_LOG", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    path = config_path()
    if not os.path.exists(path):
        # Not fatal: the service would only restart into the same error. The daemon starts on
        # the newest backup (or nothing), so the control panel is there to set things up.
        logging.getLogger("deck").error("No config at %s. Run ./install.sh, copy config.example.yaml "
                                         "there, or restore a backup in the control panel.", path)
    single_instance()
    bot = DeckBot(root=ROOT, config_path=path)
    bot.load_config(initial=True)   # never raises: a broken config.yaml starts on a backup
    bot.load_extensions("modules")
    bot.load_plugins()
    bot.run()


if __name__ == "__main__":
    main()
