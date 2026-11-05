#!/usr/bin/env python3
"""Run the daemon against a fake Stream Deck, for working on the control panel.

    python3 tools/devserver.py                      sandboxed copy of config.example.yaml, port 8766
    python3 tools/devserver.py --model "Stream Deck +" --port 9000
    python3 tools/devserver.py --decks "Stream Deck XL,Stream Deck Mini"     several fake decks at once
    python3 tools/devserver.py --config ~/.config/ultimate-streaming/config.yaml    preview your own pages (copied, never written)

No hardware is touched. Everything (a copy of the config, tokens, editor token) lives in a
throw-away folder, so it can run next to the real daemon; --config is copied in, never
written. Key actions and state/display providers that would reach outside the sandbox
(shell commands, audio, media players, Home Assistant, Twitch) are only logged, and
the XDG folders point into the sandbox (backups and restores never touch your real ones),
and OBS is pointed at a closed port, unless you pass --live. It prints the panel's
URL (with its access token) and, with --screenshot, saves a PNG of a view with Firefox:

    python3 tools/devserver.py --screenshot /tmp/live.png --view live --size 1400x900
"""
import argparse
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "vendor"))
sys.path.insert(0, ROOT)


class Marionette:
    """Just enough of Firefox's Marionette protocol to load a page, wait, and screenshot."""

    def __init__(self, port):
        import socket
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=30)
        self.msg_id = 0
        self._recv()  # hello

    def _recv(self):
        import json
        buf = b""
        while b":" not in buf:
            buf += self.sock.recv(1)
        length = int(buf[:-1])
        data = b""
        while len(data) < length:
            data += self.sock.recv(length - len(data))
        return json.loads(data)

    def call(self, name, params=None):
        import json
        self.msg_id += 1
        msg = json.dumps([0, self.msg_id, name, params or {}]).encode()
        self.sock.sendall(f"{len(msg)}:".encode() + msg)
        while True:
            reply = self._recv()
            if reply[0] == 1 and reply[1] == self.msg_id:
                if reply[2]:
                    raise RuntimeError(f"{name}: {reply[2]}")
                return reply[3]


def shot(url, args, sandbox):
    """Open the panel in headless Firefox, wait, save a PNG, and print any JS errors."""
    import base64
    import socket

    firefox = shutil.which("firefox") or shutil.which("firefox-esr")
    if not firefox:
        sys.exit("firefox is needed for --screenshot")
    profile = os.path.join(sandbox, "ff-profile")
    os.makedirs(profile)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        mport = s.getsockname()[1]
    with open(os.path.join(profile, "user.js"), "w") as f:
        f.write(f'user_pref("marionette.port", {mport});\nuser_pref("browser.shell.checkDefaultBrowser", false);\n'
                'user_pref("datareporting.policy.dataSubmissionEnabled", false);\n')
    w, h = (int(v) for v in args.size.lower().split("x"))
    proc = subprocess.Popen([firefox, "--headless", "--marionette", "--profile", profile, "--no-remote",
                             "--window-size", f"{w},{h}", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={**os.environ, "MOZ_HEADLESS": "1"})
    try:
        for _ in range(60):
            try:
                m = Marionette(mport)
                break
            except OSError:
                time.sleep(0.5)
        else:
            sys.exit("Firefox's marionette port never opened")
        m.call("WebDriver:NewSession", {"capabilities": {"alwaysMatch": {"acceptInsecureCerts": True}}})
        m.call("WebDriver:SetWindowRect", {"width": w, "height": h})
        m.call("WebDriver:ExecuteScript", {"script": "window.__errs = []; window.addEventListener('error', e => __errs.push(e.message));", "args": []})
        m.call("WebDriver:Navigate", {"url": f"{url}&view={args.view}"})
        time.sleep(args.wait)
        png = m.call("WebDriver:TakeScreenshot", {"full": False})["value"]
        with open(args.screenshot, "wb") as f:
            f.write(base64.b64decode(png))
        print(f"saved {args.screenshot}")
        errs = m.call("WebDriver:ExecuteScript", {"script": "return window.__errs || []", "args": []})["value"]
        for e in errs:
            print(f"JS error: {e}")
        try:
            m.call("Marionette:Quit", {"flags": ["eForceQuit"]})
        except Exception:
            pass
    finally:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# Actions that stay inside the sandbox. Everything else (shell commands, key combos, audio,
# apps, Home Assistant, Twitch, OBS, the network) is only logged in a dry run, so the panel
# behaves normally without touching your desktop or accounts. An allow-list, so actions
# added later are dry by default: that includes the Twitch Bot's own deck actions (twitch_bot,
# bot_timer, bot_timers) and every action a channel-point reward runs (they go through
# bot.run_action too). The bot's chat posts don't go through an action, so they are made dry
# separately below.
INSIDE = {"page", "countdown", "countdown_toggle", "brb_poll", "brb_guess", "brb_game", "brb_hype", "delay"}


# State and display providers that run commands, read the desktop or call the network. Like
# actions, they are logged and answer False / blank in a dry run (obs.* talk to the closed port).
DRY_STATES = {"cmd", "audio", "media", "ha", "twitch_chat"}
DRY_DISPLAYS = {"twitch_viewers"}


def dry_run(bot):
    for kind, names, providers, empty in (("state", DRY_STATES, bot.state_providers, False),
                                          ("display", DRY_DISPLAYS, bot.display_providers, {})):
        for name in names & set(providers):
            if getattr(providers[name], "__dry__", False):
                continue

            def fake(*args, _kind=kind, _name=name, _empty=empty):
                logging.getLogger("deck.devserver").info("dry run: %s %s (use --live to really run it)", _kind, _name)
                return _empty
            fake.__dry__ = True
            providers[name] = fake
    def wrap(kind, fn):
        def fake(*args):
            logging.getLogger("deck.devserver").info("dry run: %s %r (use --live to really run it)", kind, args[:1])
        fake.__deck_action__ = fn.__deck_action__
        if hasattr(fn, "__deck_ui__"):   # keep the panel's form for the action
            fake.__deck_ui__ = fn.__deck_ui__
        fake.__self__ = fn.__self__
        fake.__dry__ = True
        return fake
    for kind in list(bot.actions):
        if kind not in INSIDE and not getattr(bot.actions[kind], "__dry__", False):
            bot.actions[kind] = wrap(kind, bot.actions[kind])
    tb = bot.get_module("TwitchBot")
    if tb and not getattr(tb._say, "__dry__", False):   # scheduled messages, command and reward replies
        def say(text, reply_to=None, announce=False, color="primary"):
            logging.getLogger("deck.devserver").info("dry run: the Twitch Bot would post %r (use --live to really post)",
                                                     str(text)[:80])
        say.__dry__ = True
        tb._say = say


def sandbox_overrides(cfg, port, live):
    """cfg with the panel on `port` and, unless live, OBS pointed at port 9 (discard): never a websocket."""
    cfg = {**cfg, "editor": {**(cfg.get("editor") or {}), "port": port}}
    if not live:
        cfg["obs"] = {**(cfg.get("obs") or {}), "host": "127.0.0.1", "port": 9}
    return cfg


def prepare_config(config, port, live):
    """Sandbox the copy of the config at `config`: panel port, OBS target. True if it was rewritten.
    A file that can't be parsed (YAML error, not a mapping) is left as it is and False is returned:
    the daemon then runs on its fallback, and the caller forces the same settings after loading."""
    import yaml
    log = logging.getLogger("deck.devserver")
    try:
        with open(config) as f:
            cfg = yaml.safe_load(f) or {}
        if not isinstance(cfg, dict):
            raise ValueError("the top level is not a mapping")
    except (yaml.YAMLError, ValueError, UnicodeDecodeError) as e:
        log.warning("config can't be parsed (%s); using it as it is. The panel will show the daemon's "
                    "fallback (newest good revision, or an empty config)%s", str(e).splitlines()[0] if str(e) else e,
                    "" if live else "; OBS and the panel port are forced after it loads")
        return False
    with open(config, "w") as f:
        yaml.safe_dump(sandbox_overrides(cfg, port, live), f, sort_keys=False, allow_unicode=True)
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="Stream Deck XL")
    ap.add_argument("--decks", metavar="MODELS",
                    help='comma-separated models to attach at once, e.g. "Stream Deck XL,Stream Deck Mini" '
                         "(default: just --model); each gets its own serial (DEV1-..., DEV2-...)")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--config", help="config.yaml to use (default: a sandboxed copy of config.example.yaml)")
    ap.add_argument("--state", help="state dir for tokens etc. (default: a temp folder; your real one needs --live)")
    ap.add_argument("--screenshot", metavar="PNG", help="save a screenshot of the panel and exit")
    ap.add_argument("--view", default="live", help="view to screenshot: live, deck, twitch, bot (Twitch Bot), obs, settings; "
                         "add &theme=light for the light theme, e.g. --view 'bot&theme=light'")
    ap.add_argument("--size", default="1400x900", help="screenshot window size")
    ap.add_argument("--wait", type=float, default=4.0, help="seconds to let the page load before the screenshot")
    ap.add_argument("--live", action="store_true",
                    help="really run key actions (shell commands, key combos, volume...) and talk to your real OBS "
                         "(default: a dry run that only logs them, and OBS is pointed at a closed port)")
    args = ap.parse_args()

    from core.paths import state_dir as real_state_dir
    if args.state and not args.live and os.path.realpath(os.path.expanduser(args.state)) == \
            os.path.realpath(real_state_dir()):
        sys.exit("--state is your real state folder (Twitch token, backups); that needs --live.\n"
                 "Two daemons sharing one Twitch login would log each other out.")
    sandbox = tempfile.mkdtemp(prefix="streamdeck-dev-")
    state = args.state or os.path.join(sandbox, "state")
    os.environ["DECK_STATE_DIR"] = state
    if not args.live:   # backups, plugin data and OBS restores never reach your real folders
        os.environ["DECK_DRY_RUN"] = "1"
        os.environ["OBS_CONFIG_DIR"] = os.path.join(sandbox, "obs-studio")   # paths.CONFIG_HOME was read at import
        for var, sub in (("XDG_STATE_HOME", "xdg-state"), ("XDG_DATA_HOME", "xdg-data"), ("XDG_CONFIG_HOME", "xdg-config")):
            os.environ[var] = os.path.join(sandbox, sub)
    config = os.path.join(sandbox, "config.yaml")   # always a copy: the original is never written
    shutil.copy(os.path.expanduser(args.config) if args.config else os.path.join(ROOT, "config.example.yaml"), config)
    os.environ["DECK_CONFIG"] = config

    from core import editor_auth
    from core.bot import DeckBot
    from core.models import MODELS, FakeDeck, fake_serial

    models = [m.strip() for m in args.decks.split(",") if m.strip()] if args.decks else [args.model]
    for m in models:
        if m not in MODELS:
            sys.exit(f"unknown model {m!r}; choose from: {', '.join(MODELS)}")

    class Deck(FakeDeck):
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def set_key_image(self, *a): pass
        def set_touchscreen_image(self, *a, **kw): pass
        def set_screen_image(self, *a): pass
        def set_brightness(self, *a): pass

    logging.basicConfig(level=os.environ.get("DECK_LOG", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parsed = prepare_config(config, args.port, args.live)

    bot = DeckBot(root=ROOT, config_path=config)
    if not parsed:
        # The file is as it was given, so the daemon's own loader decides what runs (the newest good
        # revision, or {}). Whatever it picks, force the panel port and the dry-run OBS target.
        apply_config = bot._apply_config

        def forced(cfg, digest, fallback):
            apply_config(sandbox_overrides(cfg, args.port, args.live), digest, fallback)
        bot._apply_config = forced
    bot.load_config(initial=True)
    for i, m in enumerate(models):
        # One fake deck keeps the model's default serial; several get distinct ones.
        bot.attach_deck(Deck(m, serial=fake_serial(m) if len(models) == 1 else f"DEV{i + 1}-{fake_serial(m)}"))
    bot.load_extensions("modules")
    bot.load_plugins()   # installed plugins ($XDG_DATA_HOME/ultimate-streaming/plugins)
    if not args.live:
        dry_run(bot)
    url = editor_auth.url(bot.cfg)
    print(f"control panel: {url}", flush=True)

    stopping = threading.Event()   # set before unloading, so the loop doesn't load everything again

    def loop():
        last = 0
        while not stopping.is_set():
            try:
                if time.time() - last > 1:
                    last = time.time()
                    bot.watch_extensions()
                    bot.watch_plugins()
                    if not args.live:
                        dry_run(bot)   # reloaded modules re-register real actions
                    bot.load_config()
                bot.poll()
            except Exception:
                logging.exception("poll")
            time.sleep(0.25)

    worker = threading.Thread(target=loop, daemon=True)
    worker.start()

    def unload_all():
        stopping.set()
        worker.join(timeout=10)
        for name in list(bot.extensions):
            bot.unload_extension(name)

    if args.screenshot:
        shot(url, args, sandbox)
        unload_all()
        return

    signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    unload_all()


if __name__ == "__main__":
    main()
