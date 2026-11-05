#!/usr/bin/env python3
"""Check that everything the deck needs is installed and reachable.

    python3 tools/doctor.py

Prints one line per check: ok / warn (a feature won't work) / FAIL (the deck won't run).
Never changes anything. Exit code 1 if anything FAILed.
"""
import ctypes
import glob
import importlib
import json
import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "vendor"))
sys.path.insert(0, ROOT)

RESULTS = {"ok": 0, "warn": 0, "FAIL": 0}
COLOURS = {"ok": "\033[32m", "warn": "\033[33m", "FAIL": "\033[31m"}


def report(level, what, hint=""):
    RESULTS[level] += 1
    tty = sys.stdout.isatty()
    tag = f"{COLOURS[level]}{level:>4}\033[0m" if tty else f"{level:>4}"
    print(f"[{tag}] {what}" + (f"\n         → {hint}" if hint else ""))


def check_python():
    if sys.version_info >= (3, 14):
        report("ok", f"Python {sys.version.split()[0]}")
    else:
        report("FAIL", f"Python {sys.version.split()[0]}", "Python 3.14 or newer is required (pyenv install 3.14)")


def check_imports():
    required = {"PIL": "Pillow (python3-pil / python3-pillow / python-pillow)",
                "yaml": "PyYAML (python3-yaml / python3-pyyaml / python-yaml)",
                "requests": "requests (python3-requests / python-requests)"}
    for mod, pkg in required.items():
        try:
            m = importlib.import_module(mod)
            report("ok", f"{mod} {getattr(m, '__version__', '')}".strip())
        except ImportError:
            report("FAIL", f"python module {mod} missing", f"install {pkg}")
    for mod in ("StreamDeck", "obsws_python", "websocket"):
        try:
            importlib.import_module(mod)
            report("ok", f"{mod} (vendor/)")
        except ImportError:
            report("FAIL", f"{mod} not in vendor/", "run ./install.sh (or python3 tools/fetch_deps.py)")
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # noqa: F401
        report("ok", "GTK 3 (PyGObject) for the stream-info dialog")
    except Exception:
        report("warn", "PyGObject / GTK 3 missing: the 'Stream info' (title/category) dialog won't open",
               "install python3-gi + gir1.2-gtk-3.0 (Debian/Ubuntu), python3-gobject gtk3 (Fedora), "
               "python-gobject gtk3 (Arch)")


def check_hidapi():
    names = ["libhidapi-libusb.so.0", "libhidapi-libusb.so", "libhidapi-hidraw.so.0", "libhidapi-hidraw.so"]
    for n in names:
        try:
            ctypes.CDLL(n)
            report("ok", f"hidapi ({n})")
            return
        except OSError:
            continue
    report("FAIL", "hidapi library not found (needed to talk to the deck)",
           "install libhidapi-hidraw0 (Debian/Ubuntu) or hidapi (Fedora/Arch/openSUSE)")


def elgato_hidraw_nodes():
    nodes = []
    for uevent in glob.glob("/sys/class/hidraw/hidraw*/device/uevent"):
        try:
            text = open(uevent).read().upper()
        except OSError:
            continue
        if ":00000FD9:" in text:
            nodes.append("/dev/" + uevent.split("/")[4])
    return nodes


def check_device():
    nodes = elgato_hidraw_nodes()
    if not nodes:
        report("warn", "no Elgato Stream Deck plugged in (the daemon waits for one)")
        return
    blocked = [n for n in nodes if not os.access(n, os.R_OK | os.W_OK)]
    if blocked:
        report("FAIL", f"no permission to open {', '.join(blocked)}",
               "install the udev rule: sudo cp packaging/70-streamdeck.rules /etc/udev/rules.d/ && "
               "sudo udevadm control --reload && sudo udevadm trigger, then re-plug the deck")
        return
    try:
        from core import hid  # noqa: F401
        from StreamDeck.DeviceManager import DeviceManager
        decks = DeviceManager().enumerate()
        for d in decks:
            rows, cols = d.key_layout()
            extras = []
            if d.dial_count():
                extras.append(f"{d.dial_count()} dials")
            if d.is_touch():
                extras.append("touch strip")
            if d.touch_key_count():
                extras.append(f"{d.touch_key_count()} touch keys")
            if not d.is_visual():
                extras.append("no display")
            layout = f"{cols}x{rows}"
            try:
                d.open()
                extras.append(f"serial {d.get_serial_number()}")
                d.close()
            except Exception:
                pass  # in use by the running daemon; the service log shows the serial
            report("ok", f"{d.deck_type()}: {cols}x{rows} keys" + (f", {', '.join(extras)}" if extras else "")
                   + f"  (config layout key: '{d.deck_type()}' or '{layout}')")
        if not decks:
            report("warn", "deck visible but the library found none (is another app using it?)")
    except Exception as e:
        report("warn", f"couldn't enumerate decks: {e}")


def check_keyboard():
    if os.access("/dev/uinput", os.W_OK):
        report("ok", "/dev/uinput writable (key-press actions use a virtual keyboard)")
    else:
        report("warn", "/dev/uinput not writable: key actions fall back to X11 XTest (not on pure Wayland)",
               "the udev rule in packaging/70-streamdeck.rules grants it")
    for lib in ("libX11.so.6", "libXtst.so.6"):
        try:
            ctypes.CDLL(lib)
        except OSError:
            report("warn", f"{lib} missing: `keys:`/`type:`/`hold:` actions won't work",
                   "install libx11-6 libxtst6 (Debian/Ubuntu), libX11 libXtst (Fedora), libx11 libxtst (Arch)")
            return
    if not os.environ.get("DISPLAY"):
        report("warn", "DISPLAY not set in this shell: key actions need an X / XWayland display",
               "if the systemd service lacks it: systemctl --user import-environment DISPLAY XAUTHORITY")
    else:
        report("ok", "X11 libraries for key actions")


def check_commands():
    tools = {
        "pactl": ("volume / mic keys", "pulseaudio-utils (Debian/Ubuntu/Fedora), libpulse (Arch); works with PipeWire"),
        "dbus-send": ("media keys (MPRIS)", "dbus (Debian/Ubuntu/Arch), dbus-tools (Fedora)"),
        "notify-send": ("desktop notifications", "libnotify-bin (Debian/Ubuntu), libnotify (Fedora/Arch)"),
        "xdg-open": ("opening Twitch pages / links", "xdg-utils"),
        "fc-match": ("finding fonts", "fontconfig"),
    }
    for cmd, (feature, pkg) in tools.items():
        if shutil.which(cmd):
            report("ok", f"{cmd} ({feature})")
        else:
            report("warn", f"{cmd} missing: {feature} won't work", f"install {pkg}")


def check_fonts():
    try:
        from core.render import FALLBACK_EMOJI, FALLBACK_FONTS, find_font
    except Exception as e:
        report("warn", f"couldn't check fonts: {e}")
        return
    if find_font("DejaVu Sans:bold", FALLBACK_FONTS):
        report("ok", "label font (DejaVu Sans Bold)")
    else:
        report("warn", "DejaVu Sans not found: labels use Pillow's built-in font",
               "install fonts-dejavu-core (Debian/Ubuntu), dejavu-sans-fonts (Fedora), ttf-dejavu (Arch), "
               "or set `font:` in config.yaml")
    if find_font("Noto Color Emoji", FALLBACK_EMOJI):
        report("ok", "emoji font (Noto Color Emoji)")
    else:
        report("warn", "Noto Color Emoji not found: emoji icons are hidden",
               "install fonts-noto-color-emoji (Debian/Ubuntu), google-noto-color-emoji-fonts (Fedora), "
               "noto-fonts-emoji (Arch)")


def check_config():
    from core.paths import config_path, obs_websocket_config
    import yaml
    path = config_path()
    if not os.path.exists(path):
        report("FAIL", f"no config at {path}", "run ./install.sh, or copy config.example.yaml there")
        return
    try:
        cfg = yaml.safe_load(open(path)) or {}
        report("ok", f"config {path}")
    except Exception as e:
        report("FAIL", f"config {path} doesn't parse: {e}")
        return
    if os.stat(path).st_mode & 0o022:
        report("warn", f"{path} is writable by other users (it can run commands)", f"chmod 600 {path}")
    tw = cfg.get("twitch") or {}
    if not (tw.get("client_id") or os.environ.get("TWITCH_CLIENT_ID")):
        report("warn", "twitch.client_id not set: clips, markers, ads, chat modes and stream info are off",
               "see README → Twitch")
    if not (tw.get("channel") or os.environ.get("TWITCH_CHANNEL")):
        report("warn", "twitch.channel not set: the chat feed on the BRB screen is off")
    ws = obs_websocket_config()
    if not os.path.exists(ws):
        report("warn", "OBS websocket config not found (OBS not installed or never started?)",
               "start OBS once, then Tools → WebSocket Server Settings → Enable WebSocket server")
    else:
        try:
            enabled = json.load(open(ws)).get("server_enabled")
        except (OSError, ValueError):
            enabled = None
        if enabled:
            report("ok", "OBS websocket server enabled")
        else:
            report("warn", "OBS websocket server is disabled: OBS keys won't work",
                   "OBS → Tools → WebSocket Server Settings → Enable WebSocket server")


def check_plugins():
    """Installed plugins: manifest, pinned dependencies, programs. Reads plugin.yaml and
    requirements.txt only; never imports plugin code and creates nothing."""
    # core.paths.plugins_dir() would create the folder, so build the same path here.
    data = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
                        "ultimate-streaming")
    if not os.path.isdir(os.path.join(data, "plugins")):
        report("ok", "no plugins installed")
        return
    from core import plugin_install, plugins
    found = plugins.installed()
    if not found:
        report("ok", "no plugins installed")
        return
    try:
        have = {n.lower() for n in os.listdir(os.path.join(data, "plugin-vendor"))}
    except OSError:
        have = set()
    for plugin in found:
        name = plugin["name"]
        if plugin["error"]:
            report("warn", f"plugin {name}: {plugin['error']}",
                   f"python3 tools/plugin.py remove {name}, then install it again")
            continue
        manifest = plugin["manifest"]
        report("ok", f"plugin {name} {manifest['version']}")
        req = os.path.join(plugin["path"], "requirements.txt")
        reqs = []
        if os.path.exists(req):
            try:
                reqs = plugin_install.fetch_deps.parse(req)
            except plugin_install.DepsError as e:
                report("FAIL", f"plugin {name}: {e}", "python3 tools/plugin.py deps")
        for pkg, version, _hashes in reqs:
            dist = f"{re.sub(r'[-.]', '_', pkg.lower())}-{version.lower()}.dist-info"
            if dist not in have:
                report("FAIL", f"plugin {name}: dependency {pkg} {version} missing", "python3 tools/plugin.py deps")
        for program, purpose in manifest["commands"].items():
            if shutil.which(program):
                report("ok", f"{program} ({purpose})")
            else:
                report("warn", f"{program} missing: {purpose}", f"install {program} with your package manager")


def main():
    print("The Ultimate Streaming Control Panel for Linux: checking prerequisites\n")
    for check in (check_python, check_imports, check_hidapi, check_device, check_keyboard,
                  check_commands, check_fonts, check_config, check_plugins):
        try:
            check()
        except Exception as e:  # a broken check shouldn't hide the others
            report("warn", f"{check.__name__} crashed: {e}")
    print(f"\n{RESULTS['ok']} ok, {RESULTS['warn']} warnings, {RESULTS['FAIL']} failures")
    sys.exit(1 if RESULTS["FAIL"] else 0)


if __name__ == "__main__":
    main()
