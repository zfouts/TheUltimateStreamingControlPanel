"""The Bambu plugin's Module: wires the printer watcher, camera relay, Discord snapshots, OBS scene
helpers and wording into the deck (actions, states, a display), the OBS overlay files, the Twitch
Bot's `print` built-in and the panel's status card.

Threads:
- paho's network thread calls the watcher's on_event: it only queues (event, summary).
- the bot's main loop drains that queue (@task every 0.5 s): it dispatches bambu_print_started /
  bambu_print_ended, forwards to Discord, clears a pending stop and times the on_print_end switch.
- one "bambu-control" worker starts and stops the parts (ffmpeg -version, joins, MQTT disconnects)
  and makes the print-end OBS calls, so neither the main loop nor on_load/on_config ever blocks.
- actions run in the bot's worker threads.
- the control panel's page buttons (panel_action) run on a web request thread; the slow ones
  (camera, OBS, Discord) on a short-lived "bambu-panel" thread the request waits on briefly.

Secrets (the access code, the webhook URL) come from the environment or a file (read_secret),
never from config.yaml, and never reach a log line, an error message or snapshot(). The Discord
webhook is normally picked by id from the core Discord module's registry (Settings -> Discord),
which holds the URL; the old webhook_file / BAMBU_DISCORD_WEBHOOK is only used while no webhook
is picked (a picked one that is broken or has no URL turns snapshots off, never a silent switch).
"""
import collections
import concurrent.futures
import datetime
import os
import queue
import re
import threading
import time

import requests

from core import Module, action, chatcmd, detect, display, paths, state, task
from core.webapi import ApiError
from core.paths import overlay_dir, state_file
from core.schema import UI, Choice
from core.util import notify, read_secret
from core.webhooks import Result
from modules.overlay import write_overlay as write_overlay_file

from . import obs_scene, wording
from .camera import TRANSPORTS, CameraRelay, choose_link
from .printer import SERIAL_RE, PrinterWatcher
from .snapshots import DiscordSnapshots, validate_webhook

GROUP = "Bambu printer"
SETUP_MSG = "The Bambu plugin isn't set up yet: set bambu.host, bambu.serial and the access code"
NOT_CONNECTED = "The printer isn't connected"
NO_OBS = "OBS isn't reachable"
DISCORD_OFF = "Discord snapshots are off: pick a webhook (Settings → Discord)"
WEBHOOK_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}\Z")   # a Discord registry id
MEDIA_CHECK = 5      # seconds between looks at whether OBS's camera source gave up (socket only)
MEDIA_RESTART_GAP = 10   # seconds at least between restarts of it that those looks make
REGISTRY_CHECK = 5   # seconds between looks at whether the picked webhook's URL changed
CONFIRM_SECONDS = 3
UNLOAD_TIMEOUT = 10
OVERLAY_FILES = ("bambu", "bambu_line")
DEFAULT_NAME = "Printer"
CARD_TITLE_SUFFIX = " (Bambu printer)"

CAMERA_DEFAULTS = {"enabled": True, "port": 5004, "scene": "Printer Cam", "source": "Printer Camera",
                   "text_source": "Printer Status", "ffmpeg": "ffmpeg", "hide_access_code": True,
                   "crop_left": 0, "crop_right": 0, "transport": "socket"}
CROP_MAX = 4000                                 # pixels; more than any printer camera is wide
DISCORD_DEFAULTS = {"every_layers": 10, "min_gap": 15, "final_delay": 30}
END_DEFAULTS = {"scene": "", "after": 60}

STATES = ("printing", "paused", "active", "finished", "failed", "connected", "light", "camera")
SWITCHES = ("on", "off", "toggle")
CAMERA_ARGS = (*SWITCHES, "trust")   # trust: forget the pinned camera and MQTT certificates and re-pin
CAMERA_WORDS = {"off": "Off", "starting": "Starting", "live": "Live", "retrying": "Retrying",
                "unavailable": "Unavailable"}
STATE_WORDS = {"RUNNING": "Printing", "PAUSE": "Paused", "PREPARE": "Getting ready",
               "FINISH": "Finished", "FAILED": "Failed", "IDLE": "Idle"}
DASH = "—"
PANEL_WAIT = 4                                  # seconds a panel button waits for slow work
SETUP_FIRST = "Set up the printer first"
RELAY_RUNNING = ("starting", "live", "retrying")
FLATPAK_OBS = os.path.join("~", ".var", "app", "com.obsproject.Studio")
_valid_host = PrinterWatcher._valid_host   # bound here: tests swap PrinterWatcher for a fake


def _inside(path, folder):
    return path == folder or path.startswith(folder + os.sep)


def _obs_is_flatpak():
    """Whether the OBS in use is the Flatpak one, which can't see our $XDG_RUNTIME_DIR folder.
    The same order core/paths.py and core/detect.py use: OBS_CONFIG_DIR; else no Flatpak OBS
    installed means native, and no native settings folder means Flatpak; with both, the one
    running, else the one whose settings changed last (detect's _mark_active rule). If that
    check fails it counts as native: an error alone mustn't switch the camera to UDP."""
    flat = os.path.realpath(os.path.expanduser(FLATPAK_OBS))
    env = os.environ.get("OBS_CONFIG_DIR")
    if env:
        return _inside(os.path.realpath(os.path.expanduser(env)), flat)
    if not os.path.isdir(flat):
        return False
    native = os.path.join(paths._xdg("XDG_CONFIG_HOME", "~/.config"), "obs-studio")
    if not os.path.isdir(native):
        return True
    try:
        # Not detect.scan(): it also rebuilds the panel's table of backup sources.
        running = detect._running("/proc", True)
        if "obs:flatpak" in running or "obs:native" in running:
            return "obs:flatpak" in running
        return (detect._settings_mtime(os.path.join(flat, "config", "obs-studio"))
                >= detect._settings_mtime(native))
    except Exception:  # noqa: BLE001 - no evidence of a Flatpak: keep the socket
        return False


def _duration(minutes):
    """`1 h 2 min` / `42 min` / `<1 min`, or None."""
    if minutes is None:
        return None
    if minutes < 1:
        return "<1 min"
    h, m = divmod(minutes, 60)
    return f"{h} h {m} min" if h else f"{m} min"


class Settings:
    """One parsed `bambu:` section. Immutable once built; replaced whole on every config change."""

    def __init__(self, host=None, serial=None, code=None, name=DEFAULT_NAME, camera=None, webhook=None,
                 webhook_problem=None, discord=None, end=None, webhook_pick="", webhook_id=None,
                 registry_url=None):
        self.host, self.serial, self._code = host, serial, code
        self.name = name
        self.camera = dict(CAMERA_DEFAULTS, **(camera or {}))
        self._webhook = webhook
        self.webhook_problem = webhook_problem
        self.webhook_pick = webhook_pick      # the registry id configured ("" = none)
        self.webhook_id = webhook_id          # the registry id the URL came from (None: the file fallback)
        self.registry_url = registry_url      # what the registry gave for the pick, to spot a replacement
        self.discord = dict(DISCORD_DEFAULTS, **(discord or {}))
        self.end = dict(END_DEFAULTS, **(end or {}))

    def __repr__(self):   # keep secrets out of tracebacks and debug logs
        return f"<bambu Settings host={self.host} configured={self.configured}>"

    @property
    def configured(self):
        return bool(self.host and self.serial and self._code)

    @property
    def code(self):
        return self._code

    @property
    def webhook(self):
        return self._webhook

    # What each part depends on: a part restarts only when its key changes.
    def watcher_key(self):
        return (self.host, self.serial, self._code)

    def relay_key(self):
        c = self.camera
        return (self.configured, self.host, self.serial, self._code, c["enabled"], c["port"],
                c["ffmpeg"], c["hide_access_code"], c["transport"])

    def link_key(self):
        return (self.camera["transport"], self.camera["port"])

    def snapshots_key(self):
        d = self.discord
        return (self.configured, self._webhook, self.webhook_id, d["every_layers"], d["min_gap"], d["final_delay"])


class Bambu(Module):
    def __init__(self, bot):
        super().__init__(bot)
        self.settings = Settings()
        self._lock = threading.Lock()          # parts, stop confirmation, print-end timer
        self._watcher = self._relay = self._snaps = None
        self._watcher_problem = None           # why the watcher couldn't start, in words
        self._camera_wanted = False
        self._link = None                      # (link_key, camera.Link) last chosen
        self._media_restarted = 0.0            # monotonic time OBS's camera source was last restarted
        self._media_check_pending = False      # a _check_media job is queued or running
        self._loaded = self._closing = False
        self._jobs = queue.Queue()
        self._worker = None
        self._events = collections.deque(maxlen=100)   # (event, summary) from paho's thread
        self._stop_pending = None              # (monotonic deadline, printer state when armed)
        self._end_at = None                    # monotonic time the print-end switch is due
        self._end_gen = 0                      # bumped by every start / end, so a stale switch is dropped
        self._last_state = None
        self._registry_looked = 0.0            # monotonic time of the last registry look
        self._written = {}                     # overlay file name -> text last written

    # ------------------------------------------------------------------ config

    def on_config(self, config):
        config = config if isinstance(config, dict) else {}
        # Don't keep a secret someone pasted into config.yaml, even ignored.
        safe = {k: v for k, v in config.items() if k not in ("access_code", "webhook")}
        if isinstance(safe.get("discord"), dict):   # discord.webhook is an id; anything else may be a URL
            safe["discord"] = {k: v for k, v in safe["discord"].items()
                               if k != "webhook" or self._is_webhook_id(v)}
        super().on_config(safe)
        new = self._parse(config)
        old, self.settings = self.settings, new
        if self._loaded:
            self._apply(old, new)

    @staticmethod
    def _is_webhook_id(value):
        return isinstance(value, str) and bool(WEBHOOK_ID_RE.match(value))

    def _registry(self):
        return self.bot.get_module("Discord")

    def _registry_url(self, pick):
        """The picked webhook's URL from the Discord registry; None if none is picked, there is no
        registry, or the webhook isn't set or is broken."""
        discord = self._registry() if pick else None
        if discord is None:
            return None
        try:
            return discord.webhook_url(pick) or None
        except Exception as e:   # a registry fault must not take the plugin's config down
            self.log.warning("Discord registry: webhook lookup failed (%s)", type(e).__name__)
            return None

    def _registry_name(self, pick):
        """The picked webhook's display name in the registry, or its id when that is unknown."""
        discord = self._registry()
        try:
            name = discord.name_of(pick) if discord is not None else None
        except Exception as e:
            self.log.warning("Discord registry: name lookup failed (%s)", type(e).__name__)
            name = None
        return name if isinstance(name, str) and name else pick

    def _parse(self, config):
        if config.get("access_code"):
            self.log.warning("bambu.access_code in config.yaml is ignored; use access_code_file "
                             "or BAMBU_ACCESS_CODE")
        discord_cfg = self._section(config, "discord")
        if config.get("webhook"):
            self.log.warning("a Discord webhook URL in config.yaml is ignored; pick one in Settings → Discord")

        host = config.get("host")
        if host is not None and not (isinstance(host, str) and host.strip()
                                     and _valid_host(host.strip())):
            self.log.warning("bambu.host must be a hostname or IP address; ignoring it")
            host = None
        host = host.strip() if host else None

        serial = os.environ.get("BAMBU_SERIAL") or config.get("serial")
        if serial is not None:
            serial = str(serial).strip().upper()
            if not SERIAL_RE.match(serial):
                self.log.warning("bambu.serial must be 8-20 letters and digits; ignoring it")
                serial = None

        code = read_secret("BAMBU_ACCESS_CODE", self._string(config, "access_code_file", None, "bambu."))

        name = config.get("name", DEFAULT_NAME)
        cleaned = wording.clean_text(name, 32) if isinstance(name, str) else ""
        if not cleaned:
            if "name" in config:
                self.log.warning("bambu.name must be text; using %r", DEFAULT_NAME)
            cleaned = DEFAULT_NAME

        cam_cfg = self._section(config, "camera")
        camera = {
            "enabled": self._bool(cam_cfg, "enabled", "bambu.camera."),
            "hide_access_code": self._bool(cam_cfg, "hide_access_code", "bambu.camera."),
            "port": self._number(cam_cfg, "port", CAMERA_DEFAULTS, 1024, 65535, "bambu.camera.", whole=True),
            "scene": self._string(cam_cfg, "scene", CAMERA_DEFAULTS["scene"], "bambu.camera.", empty=False),
            "source": self._string(cam_cfg, "source", CAMERA_DEFAULTS["source"], "bambu.camera.", empty=False),
            "text_source": self._string(cam_cfg, "text_source", CAMERA_DEFAULTS["text_source"], "bambu.camera."),
            "ffmpeg": self._string(cam_cfg, "ffmpeg", CAMERA_DEFAULTS["ffmpeg"], "bambu.camera.", empty=False),
            "crop_left": self._number(cam_cfg, "crop_left", CAMERA_DEFAULTS, 0, CROP_MAX, "bambu.camera.", whole=True),
            "crop_right": self._number(cam_cfg, "crop_right", CAMERA_DEFAULTS, 0, CROP_MAX, "bambu.camera.",
                                       whole=True),
            "transport": self._choice(cam_cfg, "transport", TRANSPORTS, "bambu.camera."),
        }
        if camera["scene"] == camera["source"]:
            self.log.warning("bambu.camera.scene and bambu.camera.source are both %r; OBS needs "
                             "different names for a scene and a source", camera["scene"])

        pick = discord_cfg.get("webhook")
        if pick is None or pick == "":
            pick = ""
        elif not self._is_webhook_id(pick):   # never echo it: it may be a URL someone pasted
            self.log.warning("bambu.discord.webhook must be the id of a webhook from Settings → Discord; ignored")
            pick = ""
        registry_url = self._registry_url(pick)
        webhook, from_registry, problem = registry_url, bool(registry_url), None
        if not pick:   # the old way, only while nothing is picked: never a silent switch of channel
            webhook = read_secret("BAMBU_DISCORD_WEBHOOK",
                                  self._string(discord_cfg, "webhook_file", None, "bambu.discord."))
        elif not webhook:
            problem = (f"The {self._registry_name(pick)} webhook is broken or has no URL; "
                       "fix it in Settings → Discord")
        if webhook:
            try:
                webhook = validate_webhook(webhook)
            except ValueError as e:   # the message never contains the URL
                self.log.warning("Discord snapshots off: %s", e)
                webhook, from_registry, problem = None, False, f"Discord snapshots are off: {e}"
        discord = {
            "every_layers": self._number(discord_cfg, "every_layers", DISCORD_DEFAULTS, 1, 1000,
                                         "bambu.discord.", whole=True),
            "min_gap": self._number(discord_cfg, "min_gap", DISCORD_DEFAULTS, 5, 3600, "bambu.discord."),
            "final_delay": self._number(discord_cfg, "final_delay", DISCORD_DEFAULTS, 0, 3600, "bambu.discord."),
        }

        end_cfg = self._section(config, "on_print_end")
        end = {"scene": self._string(end_cfg, "scene", "", "bambu.on_print_end."),
               "after": self._number(end_cfg, "after", END_DEFAULTS, 0, 86400, "bambu.on_print_end.")}
        return Settings(host, serial, code, cleaned, camera, webhook, problem, discord, end,
                        webhook_pick=pick, webhook_id=pick if from_registry else None,
                        registry_url=registry_url)

    def _section(self, config, key):
        value = config.get(key)
        if value is None:
            return {}
        if not isinstance(value, dict):
            self.log.warning("bambu.%s must be a mapping; using the defaults", key)
            return {}
        return value

    def _bool(self, cfg, key, where):
        value = cfg.get(key, CAMERA_DEFAULTS[key])
        if not isinstance(value, bool):
            self.log.warning("%s%s must be true or false; using %s", where, key, CAMERA_DEFAULTS[key])
            return CAMERA_DEFAULTS[key]
        return value

    def _number(self, cfg, key, defaults, low, high, where, whole=False):
        value = cfg.get(key, defaults[key])
        ok = (not isinstance(value, bool) and isinstance(value, (int, float))
              and (not whole or float(value).is_integer()) and low <= value <= high)
        if not ok:
            self.log.warning("%s%s must be a %s from %s to %s; using %s", where, key,
                             "whole number" if whole else "number", low, high, defaults[key])
            return defaults[key]
        return int(value) if whole else value

    def _choice(self, cfg, key, choices, where):
        value = cfg.get(key, CAMERA_DEFAULTS[key])
        if value not in choices:
            self.log.warning("%s%s must be one of %s; using %s", where, key, ", ".join(choices),
                             CAMERA_DEFAULTS[key])
            return CAMERA_DEFAULTS[key]
        return value

    def _string(self, cfg, key, default, where, empty=True):
        value = cfg.get(key, default)
        if value is None:
            return default
        if not isinstance(value, str) or len(value) > 256 or (not empty and not value.strip()):
            self.log.warning("%s%s must be %stext; using %r", where, key, "" if empty else "non-empty ", default)
            return default
        return value.strip()

    # ------------------------------------------------------------------ lifecycle

    def on_load(self):
        chatcmd.register(self.bot, chatcmd.Builtin(
            kind="print", label="3D print status", help="Says what the 3D printer is doing.",
            placeholders=frozenset({"printer", "status", "percent", "layer", "time_left", "finish", "job"}),
            default_response="{printer}: {status}",
            values=self._chat_values,
            sample={"printer": "Bob", "status": "Printing 58% · 1 h 2 min left", "percent": "58%",
                    "layer": "701/1058", "time_left": "1 h 2 min left", "finish": "done ~11:45 PM",
                    "job": "Benchy"}))
        self._loaded = True
        self._apply(None, self.settings)

    def on_unload(self):
        chatcmd.unregister(self.bot, "print")
        with self._lock:
            self._closing = True
            self._stop_pending = None
            self._end_at = None
            worker = self._worker
        # Nothing queued is worth running now; stop everything instead.
        while True:
            try:
                item = self._jobs.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                item[0].cancel()
        self._written.clear()
        self._clear_overlay()
        if worker is None:
            self._stop_all(time.monotonic() + UNLOAD_TIMEOUT)
            return
        deadline = time.monotonic() + UNLOAD_TIMEOUT
        self._jobs.put((concurrent.futures.Future(), self._stop_all, (deadline,)))
        self._jobs.put(None)
        worker.join(max(0.0, deadline - time.monotonic()))
        if worker.is_alive():
            self.log.warning("the Bambu plugin's parts didn't stop within %s s; leaving them to finish",
                             UNLOAD_TIMEOUT)

    def _stop_all(self, deadline):
        """Stop every part at once (each may take a few seconds), waiting until `deadline` at most."""
        with self._lock:
            parts = [p for p in (self._snaps, self._relay, self._watcher) if p is not None]
            stoppers = [p.stop for p in parts]
            self._snaps = self._relay = self._watcher = None
        threads = [threading.Thread(target=self._quietly, args=(fn,), name="bambu-stop", daemon=True)
                   for fn in stoppers]
        for t in threads:
            t.start()
        for t in threads:
            t.join(max(0.0, deadline - time.monotonic()))

    def _quietly(self, fn, *args):
        try:
            fn(*args)
        except Exception:
            self.log.exception("stopping a Bambu part failed")

    # ------------------------------------------------------------------ control worker

    def _submit(self, fn, *args):
        """Run fn(*args) on the bambu-control worker; returns a Future."""
        fut = concurrent.futures.Future()
        with self._lock:
            if self._closing:
                fut.set_exception(RuntimeError("The Bambu plugin is shutting down"))
                return fut
            if self._worker is None:
                self._worker = threading.Thread(target=self._work, name="bambu-control", daemon=True)
                self._worker.start()
            self._jobs.put((fut, fn, args))
        return fut

    def _work(self):
        while True:
            item = self._jobs.get()
            if item is None:
                return
            fut, fn, args = item
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fut.set_result(fn(*args))
            except BaseException as e:   # noqa: BLE001 - handed to whoever waits on the future
                fut.set_exception(e)
                # Only the type: a message might carry the host's or a library's view of a secret.
                self.log.warning("bambu-control: %s failed (%s)", getattr(fn, "__name__", "job"),
                                 type(e).__name__)

    def _flush(self, timeout=10):
        """Wait until every job queued so far has run (tests)."""
        self._submit(lambda: None).result(timeout)

    def _apply(self, old, new):
        """Queue a restart for each part whose settings changed."""
        if old is None or old.watcher_key() != new.watcher_key():
            self._submit(self._restart_watcher, new)
        if old is None or old.relay_key() != new.relay_key():
            self._submit(self._restart_relay, new)
            # The restart re-chooses socket or UDP, so point OBS there too: at the start, on a
            # transport or port change, and when the printer is first set up.
            self._submit(self._apply_url, new)
        if old is None or old.snapshots_key() != new.snapshots_key():
            self._submit(self._restart_snapshots, new)
        crop = ("source", "crop_left", "crop_right")
        if old is not None and any(old.camera[k] != new.camera[k] for k in crop):
            self._submit(self._apply_crop, new)

    def _choose_link(self, s):
        """Decide (again) how the relay and OBS talk for these settings, and remember it, so OBS
        is pointed where the relay listens. Logs a warning when socket falls back to UDP."""
        transport, port = s.link_key()
        flatpak = transport == "socket" and _obs_is_flatpak()
        link = choose_link(transport, port, obs_flatpak=flatpak)
        self._link = (s.link_key(), link)
        return link

    def _current_link(self, s):
        remembered = self._link
        if remembered and remembered[0] == s.link_key() and not remembered[1].error:
            return remembered[1]
        return self._choose_link(s)

    def _obs_url(self, s):
        return self._current_link(s).obs_url(s.camera["port"])

    def _apply_url(self, s):
        """At start and when the transport or port changed, point the OBS media source at where
        the relay now sends (e.g. from UDP to the socket after an upgrade) when OBS is
        connected; otherwise Set up OBS scene (or the next time it runs) applies it."""
        if not s.configured:
            return
        c = s.camera
        try:
            url = self._obs_url(s)   # RuntimeError if there is no safe link: OBS keeps what it has
            if self._obs_call(obs_scene.apply_url, c["source"], url):
                self.log.info("OBS camera source now reads %s", url)
        except RuntimeError as e:
            self.log.info("OBS camera URL not applied now (%s); Set up OBS scene applies it", e)

    def _apply_crop(self, s):
        """A changed crop goes straight to OBS when it's connected; otherwise Set up OBS scene
        (or the next time it runs) applies it."""
        c = s.camera
        try:
            if self._obs_call(obs_scene.apply_crop, c["source"], c["crop_left"], c["crop_right"]):
                self.log.info("OBS camera crop: left %s px, right %s px", c["crop_left"], c["crop_right"])
        except RuntimeError as e:
            self.log.info("OBS camera crop not applied now (%s); Set up OBS scene applies it", e)

    def _check_registry(self, force=False):
        """Re-read the config when the Discord registry's URL for the picked webhook changed (replaced,
        removed or marked broken), as the registry has no config.yaml change to announce it."""
        now = time.monotonic()
        if not self._loaded or (not force and now - self._registry_looked < REGISTRY_CHECK):
            return
        self._registry_looked = now
        s = self.settings
        if s.webhook_pick and self._registry_url(s.webhook_pick) != s.registry_url:
            self.on_config(self.config)

    def _swap(self, attr, part):
        with self._lock:
            old = getattr(self, attr)
            setattr(self, attr, part)
        return old

    def _restart_watcher(self, s):
        old = self._swap("_watcher", None)
        if old:
            self._quietly(old.stop)
        self._watcher_problem = None
        self._last_state = None
        if self._closing or not s.configured:
            return
        holder = []

        def on_event(event, summary):   # paho's network thread: queue it, nothing more
            if holder and holder[0] is self._watcher and not self._closing:
                self._events.append((event, summary))

        try:
            watcher = PrinterWatcher(s.host, s.serial, s.code, on_event=on_event,
                                     cert_file=state_file(f"bambu-mqtt-{s.serial}.pem"))
            holder.append(watcher)
            with self._lock:
                if self._closing:
                    return
                self._watcher = watcher
            watcher.start()
        except ImportError:
            self.log.error("paho-mqtt isn't installed; install the plugins' dependencies: python3 tools/plugin.py deps")
            self._watcher_problem = "paho-mqtt isn't installed: run python3 tools/plugin.py deps"
            self._swap("_watcher", None)
        except Exception as e:
            self.log.error("couldn't start the printer connection: %s",
                           holder[0].redact(e) if holder else type(e).__name__)
            self._watcher_problem = "Couldn't start the printer connection"
            self._swap("_watcher", None)

    def _restart_relay(self, s):
        old = self._swap("_relay", None)
        if old:
            self._quietly(old.stop)
        self._camera_wanted = s.camera["enabled"]
        if self._camera_wanted:
            self._start_relay(s)

    def _new_relay(self, s):
        """A relay for these settings (not started), or None. The pinned camera certificate is
        per serial, so a new serial means a new pin."""
        if not s.configured:
            return None
        try:
            link = self._choose_link(s)
            return CameraRelay(s.host, s.code, s.camera["port"], ffmpeg=s.camera["ffmpeg"],
                               hide_access_code=s.camera["hide_access_code"], serial=s.serial,
                               cert_file=state_file(f"bambu-camera-{s.serial}.pem"),
                               socket_path=link.socket_path, link_error=link.error,
                               on_listening=self._relay_listening, on_stall=self._relay_stalled)
        except ValueError as e:
            self.log.error("camera relay not started: %s", e)
            return None

    # OBS doesn't reconnect a unix: input by itself: restart its camera source whenever a new
    # ffmpeg listens, when video stalls (OBS may have stopped reading), and when a look every
    # MEDIA_CHECK s finds the source given up (OBS started after us, or it dropped the connection).

    def _relay_listening(self, relay):   # the relay's thread: queue it, nothing more
        self._submit(self._restart_media, relay, "the relay is listening")

    def _relay_stalled(self, relay):     # the relay's thread: queue it, nothing more
        self._submit(self._restart_media, relay, "no video")

    def _restart_media(self, relay, why):
        if self._relay is not relay or relay.socket_path is None:
            return False                  # an old relay's news
        try:
            if self._obs_call(obs_scene.restart_media, self.settings.camera["source"]):
                self._media_restarted = time.monotonic()
                self.log.info("OBS camera source restarted (%s)", why)
                return True
        except RuntimeError as e:
            self.log.info("OBS camera source not restarted (%s): %s", why, e)
        return False

    def _check_media(self, relay):
        try:
            if self._relay is not relay or not relay.listening():
                return
            if time.monotonic() - self._media_restarted < MEDIA_RESTART_GAP:
                return
            try:
                state = self._obs_call(obs_scene.media_state, self.settings.camera["source"])
            except RuntimeError:
                return                    # OBS not there: nothing to restart, and not worth a log line
            if state in obs_scene.DEAD_STATES:
                self._restart_media(relay, f"OBS showed {state}")
        finally:
            self._media_check_pending = False

    def _start_relay(self, s, relay=None):
        if self._closing or not s.configured:
            return
        relay = relay or self._new_relay(s)
        if relay is None:
            return
        with self._lock:
            if self._closing:
                return
            self._relay = relay
        relay.start()

    def _camera_job(self, arg):
        """bambu_camera on/off/toggle/trust, on the worker. Returns the relay's status (or None)."""
        s = self.settings
        if arg == "trust":
            return self._trust_job(s)
        want = (not self._camera_wanted) if arg == "toggle" else arg == "on"
        self._camera_wanted = want
        relay = self._relay
        if not want:
            if relay:
                self._swap("_relay", None)
                relay.stop()
            return None
        if relay is None:
            self._start_relay(s)
        elif relay.link_error:
            self._swap("_relay", None)   # look at the socket's folder again: it may be fixed
            relay.stop()
            self._start_relay(s)
        elif relay.status()["state"] in ("off", "unavailable"):
            relay.start()   # e.g. ffmpeg was installed since
        relay = self._relay
        return relay.status() if relay else None

    def _trust_job(self, s):
        """Forget both pinned certificates (camera and MQTT), then restart the watcher and, if
        the camera is wanted, the relay, so they pin what the printer presents now. The old
        ones are stopped first, so they can't write a pin back after the forget."""
        old = self._swap("_relay", None)
        if old:
            old.stop()
        old_watcher = self._swap("_watcher", None)
        if old_watcher:
            self._quietly(old_watcher.stop)
        if s.serial:
            try:
                os.unlink(state_file(f"bambu-mqtt-{s.serial}.pem"))
            except FileNotFoundError:
                pass
            except OSError as e:
                self.log.warning("couldn't forget the printer's certificate: %s", e)
        self._restart_watcher(s)
        relay = self._new_relay(s)
        if relay is None:
            return None
        relay.forget_certificate()
        if self._camera_wanted:
            self._start_relay(s, relay)
        relay = self._relay
        return relay.status() if relay else None

    def _restart_snapshots(self, s):
        old = self._swap("_snaps", None)
        if old:
            self._quietly(old.stop)
        if self._closing or not (s.configured and s.webhook):
            return
        d = s.discord
        extra = {"post": self._reporting_post(s.webhook_id)} if s.webhook_id else {}
        snaps = DiscordSnapshots(s.webhook, grab_frame=self._grab_frame, printer_snapshot=self._summary,
                                 caption=self._caption, every_layers=d["every_layers"],
                                 min_gap=d["min_gap"], final_delay=d["final_delay"], **extra)
        with self._lock:
            if self._closing:
                return
            self._snaps = snaps
        snaps.start()

    def _reporting_post(self, webhook_id):
        """requests.post for DiscordSnapshots, reporting each outcome to the Discord registry (so a
        deleted webhook shows as broken there). No redirects; the response goes back untouched."""
        def post(url, **kw):
            try:
                r = requests.post(url, allow_redirects=False, **kw)
            except Exception:
                self._report(webhook_id, Result("retry", "Couldn't reach Discord."), url)
                raise
            self._report(webhook_id, self._result_of(r.status_code), url)
            return r
        return post

    @staticmethod
    def _result_of(code):
        if 200 <= code < 300:
            return Result("ok", status=code)
        if code == 429 or code >= 500:
            return Result("retry", f"Discord asked us to wait or had a problem (HTTP {code}).", status=code)
        if code in (401, 403, 404):
            return Result("broken", f"Discord says this webhook doesn't exist any more (HTTP {code}). "
                                    "Replace its URL.", status=code)
        return Result("failed", f"Discord rejected the post (HTTP {code}).", status=code)

    def _report(self, webhook_id, result, url=None):
        """`url`: the one actually posted to, so a result for a URL replaced meanwhile blames nothing."""
        discord = self._registry()
        if discord is None:
            return
        try:
            discord.report(webhook_id, result, url=url)
        except Exception as e:
            self.log.warning("Discord registry: report failed (%s)", type(e).__name__)

    def discord_usage(self):
        """[(webhook id, label)] for the Discord page's "Used by" and its delete guard."""
        pick = self.settings.webhook_pick
        return [(pick, "Bambu Printer: snapshots")] if pick else []

    # Injected into DiscordSnapshots; they read the current settings at call time.

    def _grab_frame(self):
        obs = self.bot.get_module("OBS")
        if obs is None:
            raise RuntimeError(NO_OBS)
        return obs_scene.grab_frame(obs, self.settings.camera["source"])

    def _caption(self, summary, ended):
        return wording.status_line(summary, self.settings.name, ended)

    def _summary(self):
        w = self._watcher
        return w.snapshot() if w else {}

    # ------------------------------------------------------------------ main-loop tasks

    @task(every=0.5, on_page_change=False)
    def drain_events(self):
        """Printer events, on the main loop: quick only (the OBS calls go to the worker)."""
        self._check_registry()
        while True:
            try:
                event, summary = self._events.popleft()
            except IndexError:
                break
            with self._lock:
                self._stop_pending = None
                self._end_gen += 1
                if event == "ended" and self.settings.end["scene"]:
                    self._end_at = time.monotonic() + self.settings.end["after"]
                else:
                    self._end_at = None
            self.bot.dispatch("bambu_print_started" if event == "started" else "bambu_print_ended", summary)
            snaps = self._snaps
            if snaps:
                try:
                    snaps.on_event(event)
                except Exception:
                    self.log.exception("Discord snapshots failed to take the printer event")

        w = self._watcher
        current = w.snapshot().get("state") if w else None
        with self._lock:
            if current != self._last_state:
                self._last_state = current
                self._stop_pending = None
            due = self._end_at is not None and time.monotonic() >= self._end_at
            if due:
                self._end_at = None
                gen = self._end_gen
        if due:
            self._submit(self._end_switch, gen, self.settings)

    @task(every=MEDIA_CHECK, on_page_change=False)
    def check_obs_camera(self):
        """Main loop, quick: queue a look at OBS's camera source while the relay listens on the
        socket, unless one is pending or the source was restarted under MEDIA_RESTART_GAP ago."""
        relay = self._relay
        if relay is None or not relay.listening():
            return
        with self._lock:
            if (self._media_check_pending
                    or time.monotonic() - self._media_restarted < MEDIA_RESTART_GAP):
                return
            self._media_check_pending = True
        self._submit(self._check_media, relay)

    def _end_switch(self, gen, s):
        """The print-end scene switch, on the worker: only if no print started since, and OBS still
        shows the camera scene."""
        if gen != self._end_gen or not s.end["scene"]:
            return
        if self._summary().get("active"):
            return
        obs = self.bot.get_module("OBS")
        if obs is None:
            return
        try:
            current = obs.request("GetCurrentProgramScene", {}).get("sceneName")
            if current != s.camera["scene"]:
                return
            obs_scene.switch_scene(obs, s.end["scene"])
            self.log.info("print ended: switched OBS to %r", s.end["scene"])
        except Exception as e:
            self.log.warning("print ended, but switching OBS scene failed: %s", type(e).__name__)

    @task(every=1, on_page_change=False)
    def write_overlay(self):
        s = self.settings
        w = self._watcher
        if not s.configured or w is None:
            self._clear_overlay()
            return
        summary = w.snapshot()
        texts = {"bambu": wording.overlay_text(summary, datetime.datetime.now(), s.name),
                 "bambu_line": wording.status_line(summary, s.name)}
        folder = overlay_dir()
        for name, text in texts.items():
            if self._written.get(name) == text:
                continue
            try:
                os.makedirs(folder, exist_ok=True)
                self._write_file(os.path.join(folder, f"{name}.txt"), text)
                self._written[name] = text
            except OSError as e:
                self.log.warning("couldn't write the %s overlay file: %s", name, e)

    def _clear_overlay(self):
        """Empty the overlay files (once), so OBS doesn't keep showing an old status. A file that
        doesn't exist is left alone: nothing stale to clear."""
        for name in OVERLAY_FILES:
            if self._written.get(name) == "":
                continue
            path = os.path.join(overlay_dir(), f"{name}.txt")
            try:
                if os.path.exists(path):
                    self._write_file(path, "")
                self._written[name] = ""
            except OSError as e:
                self.log.warning("couldn't clear the %s overlay file: %s", name, e)

    @staticmethod
    def _write_file(path, text):
        """Atomically replace an overlay file, unless it already holds `text`."""
        write_overlay_file(path, text)

    # ------------------------------------------------------------------ actions

    def _connected_watcher(self):
        if not self.settings.configured:
            raise RuntimeError(SETUP_MSG)
        w = self._watcher
        if w is None or not w.connected:
            raise RuntimeError(NOT_CONNECTED)
        return w

    def _command(self, fn, *args):
        try:
            fn(*args)
        except RuntimeError as e:
            raise RuntimeError(NOT_CONNECTED if "not connected" in str(e)
                               else "The printer didn't accept the command") from None

    def _obs(self):
        if not self.settings.configured:
            raise RuntimeError(SETUP_MSG)
        obs = self.bot.get_module("OBS")
        if obs is None:
            raise RuntimeError(NO_OBS)
        return obs

    def _obs_call(self, fn, *args, **kw):
        obs = self._obs()
        try:
            return fn(obs, *args, **kw)
        except ValueError as e:   # obs_scene's name clashes: written for people
            raise RuntimeError(str(e)) from None
        except Exception as e:
            if obs.is_request_error(e):
                raise RuntimeError("OBS refused the request") from None
            raise RuntimeError(NO_OBS) from None

    @action("bambu_pause", ui=UI("Pause the print", GROUP), rewards=False)
    def pause(self, arg):
        self._command(self._connected_watcher().pause)

    @action("bambu_resume", ui=UI("Resume the print", GROUP))
    def resume(self, arg):
        self._command(self._connected_watcher().resume)

    @action("bambu_stop", ui=UI("Stop the print (press twice)", GROUP,
                                help="Press again within 3 s to cancel the print"), rewards=False)
    def stop(self, arg):
        w = self._connected_watcher()
        current = w.snapshot().get("state")
        now = time.monotonic()
        with self._lock:
            pending, self._stop_pending = self._stop_pending, None
            confirmed = pending is not None and now < pending[0] and pending[1] == current
            if not confirmed:
                self._stop_pending = (now + CONFIRM_SECONDS, current)
        if confirmed:
            self._command(w.stop_print)
            self.log.info("stopping the print on %s", self.settings.name)
        else:
            notify(f"Press again within {CONFIRM_SECONDS} s to stop the print on {self.settings.name}")

    @action("bambu_light", ui=UI("Chamber light", GROUP, arg=Choice(*SWITCHES, label="Light"), default="toggle"))
    def light(self, arg):
        arg = "toggle" if arg in (None, "") else arg
        if arg not in SWITCHES:
            raise ValueError("bambu_light takes on, off or toggle")
        w = self._connected_watcher()
        on = arg == "on" or (arg == "toggle" and w.snapshot().get("light") != "on")
        self._command(w.set_light, on)

    @action("bambu_camera", ui=UI("Printer camera relay", GROUP, arg=Choice(*CAMERA_ARGS, label="Camera"),
                                  default="toggle"))
    def camera(self, arg):
        arg = "toggle" if arg in (None, "") else arg
        if arg not in CAMERA_ARGS:
            raise ValueError("bambu_camera takes on, off, toggle or trust")
        if not self.settings.configured:
            raise RuntimeError(SETUP_MSG)
        status = self._submit(self._camera_job, arg).result(UNLOAD_TIMEOUT + 5)
        if status and status["state"] == "unavailable":
            raise RuntimeError(status["error"] or "The camera relay can't run")

    @action("bambu_scene", ui=UI("Show the printer camera in OBS", GROUP,
                                 help="Sets up the camera scene if needed, then switches to it"))
    def scene(self, arg):
        s = self.settings
        if not self._obs_call(obs_scene.scene_exists, s.camera["scene"]):
            self._setup_obs(s)
        self._obs_call(obs_scene.switch_scene, s.camera["scene"])

    @action("bambu_setup_obs", ui=UI("Set up the printer camera scene in OBS", GROUP,
                                     help="Creates or repairs the scene, its media source and text source"))
    def setup_obs(self, arg):
        self._setup_obs(self.settings)

    def _setup_obs(self, s):
        c = s.camera
        if c["scene"] == c["source"]:
            self._obs()
            raise RuntimeError("bambu.camera.scene and bambu.camera.source need different names in OBS")
        text_file = os.path.join(overlay_dir(), "bambu.txt") if c["text_source"] else None
        result = self._obs_call(obs_scene.ensure_camera, scene=c["scene"], source=c["source"],
                                url=self._obs_url(s), text_source=c["text_source"] or None,
                                text_file=text_file, crop_left=c["crop_left"], crop_right=c["crop_right"])
        if result["created"] or result["updated"]:
            self.log.info("OBS camera scene: created %s, updated %s", result["created"], result["updated"])

    @action("bambu_snapshot", ui=UI("Post a camera still to Discord", GROUP))
    def snapshot_now(self, arg):
        if not self.settings.configured:
            raise RuntimeError(SETUP_MSG)
        snaps = self._snaps
        if snaps is None:
            raise RuntimeError(self.settings.webhook_problem or DISCORD_OFF)
        if not snaps.post():
            raise RuntimeError(snaps.status().get("error") or "Couldn't post to Discord")

    # ------------------------------------------------------------------ states, display, chat

    @state("bambu", ui=UI("Bambu printer is…", GROUP, arg=Choice(*STATES, label="When")))
    def printer_state(self, arg, spec):
        if arg not in STATES:
            raise ValueError(f"bambu state must be one of {', '.join(STATES)}")
        if arg == "camera":
            relay = self._relay
            return bool(relay and relay.status()["state"] == "live")
        w = self._watcher
        if w is None or not self.settings.configured:
            return False
        s = w.snapshot()
        if arg == "connected":
            return bool(s["connected"])
        if not s["connected"]:
            return False
        if arg == "light":
            return s.get("light") == "on"
        if arg == "active":
            return bool(s.get("active"))
        return s.get("state") == {"printing": "RUNNING", "paused": "PAUSE", "finished": "FINISH",
                                  "failed": "FAILED"}[arg]

    @display("bambu_status", ui=UI("Print progress", GROUP))
    def status_display(self, spec):
        s = self.settings
        w = self._watcher
        if not s.configured:
            return {"label": "Bambu\nsetup"}
        summary = w.snapshot() if w else {"connected": False}
        if not summary.get("connected"):
            return {"label": f"{s.name}\noffline"}
        state = summary.get("state")
        pct = f"{summary['percent']}%" if summary.get("percent") is not None else None
        if state == "RUNNING":
            layer = None
            if summary.get("layer") is not None and summary.get("total_layers"):
                layer = f"L {summary['layer']}/{summary['total_layers']}"
            lines = [pct, layer, _duration(summary.get("remaining_min"))]
            return {"label": "\n".join(p for p in lines if p) or "Printing"}
        if state == "PAUSE":
            return {"label": "Paused" + (f"\n{pct}" if pct else "")}
        if state == "PREPARE":
            return {"label": "Getting\nready"}
        if state == "FINISH":
            return {"label": "Done"}
        if state == "FAILED":
            return {"label": "Failed"}
        return {"label": f"{s.name}\nidle"}

    def _chat_values(self, arg):
        """The `print` built-in; arg (the viewer's text) is ignored."""
        s = self.settings
        w = self._watcher
        if not s.configured or w is None:
            raise RuntimeError("printer not set up")
        return wording.print_values(w.snapshot(), s.name)

    # ------------------------------------------------------------------ panel

    def snapshot(self):
        s = self.settings
        w, relay, snaps = self._watcher, self._relay, self._snaps
        printer = w.snapshot() if w else None
        camera = None
        if relay:
            camera = relay.status()
            for key in ("error", "detail"):
                if camera.get(key):
                    text = relay.redact(str(camera[key]))
                    camera[key] = w.redact(text) if w else text
        discord = snaps.status() if snaps else None
        return {"printer": printer, "camera": camera, "discord": discord,
                "configured": s.configured, "card": self._card(s, printer, camera, discord)}

    def _card(self, s, printer, camera, discord):
        title = wording.clean_text(s.name, 40 - len(CARD_TITLE_SUFFIX)) + CARD_TITLE_SUFFIX
        p = printer or {}
        connected = bool(p.get("connected"))
        state = p.get("state")
        if not s.configured:
            state_word = "Not configured"
        elif not connected:
            state_word = "Offline" if p.get("error") or self._watcher_problem else "Connecting…"
        else:
            state_word = STATE_WORDS.get(state) or (state.title() if state else "Idle")
        live = connected and p.get("active")
        progress = f"{p['percent']}%" if live and p.get("percent") is not None else DASH
        layer = (f"{p['layer']} / {p['total_layers']}" if live and p.get("layer") is not None
                 and p.get("total_layers") else DASH)
        left = (_duration(p.get("remaining_min")) if live and state == "RUNNING" else None) or DASH
        temps = [f"{round(t)}°C" if t is not None else DASH
                 for t in (p.get("nozzle_temp"), p.get("bed_temp"))] if connected else [DASH, DASH]
        if camera:
            camera_word = CAMERA_WORDS.get(camera.get("state"), DASH)
        else:
            camera_word = "Off"
        if not discord:
            discord_word = "Off"
        elif discord.get("error"):
            discord_word = "Error"
        elif discord.get("last_post_ago") is None:
            discord_word = "On"
        else:
            ago = discord["last_post_ago"]
            discord_word = f"On, last post {_duration(ago // 60) if ago >= 60 else f'{ago} s'} ago"

        cam_error = camera.get("error") if camera and camera.get("state") in ("retrying", "unavailable") else None
        discord_error = (discord or {}).get("error") or s.webhook_problem
        printer_error = self._watcher_problem or (p.get("error") if s.configured and not connected else None)
        if not s.configured:
            note, tone = SETUP_MSG, "warn"
        elif printer_error:
            note, tone = printer_error, "error"
        else:
            note = cam_error or discord_error
            if state == "FAILED":
                tone = "warn"
            elif not connected:
                tone = "idle"
            else:
                tone = "warn" if note else ("ok" if p.get("active") else "idle")
        link = self._link
        if s.configured and s.camera["enabled"] and link and link[0] == s.link_key() and link[1].warning:
            note = f"{note} {link[1].warning}" if note else link[1].warning   # why it's UDP
        percent = p.get("percent")
        bar = (max(0, min(100, percent)) if live and isinstance(percent, (int, float))
               and not isinstance(percent, bool) else None)
        return {"title": title, "tone": tone, "note": note, "progress": bar,
                "rows": [["State", state_word], ["Progress", progress], ["Layer", layer],
                         ["Time left", left], ["Nozzle / Bed", " / ".join(temps)],
                         ["Camera", camera_word], ["Discord", discord_word]]}

    # ------------------------------------------------------------------ panel page

    def _relay_running(self):
        relay = self._relay
        return bool(relay and relay.status().get("state") in RELAY_RUNNING)

    def panel_page(self):
        s = self.settings
        w = self._watcher
        p = (w.snapshot() if w else None) or {}
        connected = s.configured and bool(p.get("connected"))
        state = p.get("state") if connected else None

        def button(action_id, label, icon, ok, why, **extra):
            if not s.configured:
                ok, why = False, SETUP_FIRST
            return {"id": action_id, "label": label, "icon": icon, "enabled": bool(ok),
                    "hint": None if ok else why, **extra}

        offline = NOT_CONNECTED
        running = self._relay_running()
        actions = [
            button("pause", "Pause", "pause", state == "RUNNING",
                   "Only while printing" if connected else offline),
            button("resume", "Resume", "play", state == "PAUSE",
                   "Only while paused" if connected else offline),
            button("stop", "Stop", "stop", connected and p.get("active"),
                   "No print is running" if connected else offline, tone="danger",
                   confirm=f"Stop the print on {s.name}? This cancels it and can't be undone."),
            button("light_on", "Light on", "sun", connected, offline),
            button("light_off", "Light off", "moon", connected, offline),
            button("camera", "Stop camera" if running else "Start camera", "camera", s.camera["enabled"],
                   "The camera relay is turned off in the settings below"),
            button("trust_camera", "Trust printer cert again", "lock", s.camera["enabled"],
                   "The camera relay is turned off in the settings below",
                   confirm="Trust the certificates the printer presents now? Only do this if "
                           "the printer was reset or replaced."),
            button("setup_obs", "Set up OBS scene", "obs", True, None),
            button("scene", "Show printer cam", "monitor", True, None),
            button("snapshot", "Post to Discord", "send", self._snaps is not None,
                   s.webhook_problem or "Pick a Discord webhook in the settings below first"),
        ]
        return {
            "title": "Bambu Printer", "icon": "layers",
            "intro": "Status, camera and controls for your Bambu Lab printer.",
            "actions": actions,
            "fields": [
                {"key": "host", "label": "Printer IP address", "type": "text", "group": "Printer",
                 "required": True, "max_length": 253, "placeholder": "192.168.1.50",
                 "help": "On the printer's screen: Settings → Network (WLAN)."},
                {"key": "serial", "label": "Serial number", "type": "text", "group": "Printer",
                 "required": True, "max_length": 20, "placeholder": "01P00A123456789",
                 "help": "On the printer's screen: Settings → Device. 8 to 20 letters and digits."},
                {"key": "access_code", "label": "Access code", "type": "secret", "group": "Printer",
                 "required": True, "file": "bambu_access_code", "config_key": "access_code_file",
                 "env": "BAMBU_ACCESS_CODE",
                 "help": "The LAN access code, on the printer's screen next to LAN Only mode."},
                {"key": "name", "label": "Printer name", "type": "text", "group": "Printer",
                 "default": DEFAULT_NAME, "max_length": 32,
                 "help": "What it's called on screen, in chat and on Discord."},
                {"key": "camera.enabled", "label": "Camera relay", "type": "bool", "group": "Camera",
                 "default": CAMERA_DEFAULTS["enabled"],
                 "help": "Relays the printer's camera to OBS (needs ffmpeg 5.1 or newer)."},
                {"key": "camera.transport", "label": "Relay link", "type": "select", "group": "Camera",
                 "options": [["socket", "Private socket"], ["udp", "Localhost UDP"]],
                 "default": CAMERA_DEFAULTS["transport"],
                 "help": "The private socket only you can reach. With UDP other local users can send "
                         "video into OBS (used anyway for a Flatpak OBS)."},
                {"key": "camera.port", "label": "Relay port", "type": "number", "group": "Camera",
                 "min": 1024, "max": 65535, "whole": True, "default": CAMERA_DEFAULTS["port"],
                 "help": "Names the socket, or the local UDP port between the relay and OBS."},
                {"key": "camera.scene", "label": "OBS scene", "type": "text", "group": "Camera",
                 "required": True, "max_length": 256, "default": CAMERA_DEFAULTS["scene"]},
                {"key": "camera.source", "label": "OBS camera source", "type": "text", "group": "Camera",
                 "required": True, "max_length": 256, "default": CAMERA_DEFAULTS["source"],
                 "help": "Must differ from the scene name."},
                {"key": "camera.text_source", "label": "OBS status text source", "type": "text",
                 "group": "Camera", "max_length": 256, "default": CAMERA_DEFAULTS["text_source"],
                 "help": "Leave empty for no status text."},
                {"key": "camera.crop_right", "label": "Hide from the right (px)", "type": "number",
                 "group": "Camera", "min": 0, "max": CROP_MAX, "whole": True, "default": CAMERA_DEFAULTS["crop_right"],
                 "help": "Cuts this many pixels off the camera picture's right edge, e.g. to keep a window "
                         "or your room off stream (the P2S picture is 1920 wide). Snapshots are cropped too."},
                {"key": "camera.crop_left", "label": "Hide from the left (px)", "type": "number",
                 "group": "Camera", "min": 0, "max": CROP_MAX, "whole": True, "default": CAMERA_DEFAULTS["crop_left"],
                 "help": "The same for the left edge."},
                {"key": "camera.ffmpeg", "label": "ffmpeg program", "type": "text", "group": "Camera",
                 "required": True, "max_length": 256, "default": CAMERA_DEFAULTS["ffmpeg"]},
                {"key": "camera.hide_access_code", "label": "Hide the access code from other users",
                 "type": "bool", "group": "Camera", "default": CAMERA_DEFAULTS["hide_access_code"],
                 "help": "Off puts it where other local users can see it; only as a fallback."},
                {"key": "discord.webhook", "label": "Discord webhook", "type": "discord_webhook",
                 "group": "Discord", "help": "Where snapshots are posted. None: no Discord posts."},
                {"key": "discord.every_layers", "label": "Post every N layers", "type": "number",
                 "group": "Discord", "min": 1, "max": 1000, "whole": True,
                 "default": DISCORD_DEFAULTS["every_layers"]},
                {"key": "discord.min_gap", "label": "Seconds between posts (at least)", "type": "number",
                 "group": "Discord", "min": 5, "max": 3600, "whole": False,
                 "default": DISCORD_DEFAULTS["min_gap"]},
                {"key": "discord.final_delay", "label": "Seconds after a print for the last post",
                 "type": "number", "group": "Discord", "min": 0, "max": 3600, "whole": False,
                 "default": DISCORD_DEFAULTS["final_delay"]},
                {"key": "on_print_end.scene", "label": "Then switch OBS to scene", "type": "text",
                 "group": "After a print", "max_length": 256, "default": END_DEFAULTS["scene"],
                 "help": "Only if OBS still shows the camera scene. Empty: stay."},
                {"key": "on_print_end.after", "label": "Wait first (seconds)", "type": "number",
                 "group": "After a print", "min": 0, "max": 86400, "whole": False,
                 "default": END_DEFAULTS["after"], "help": "A new print starting cancels the switch."},
            ],
        }

    def panel_action(self, action_id):
        """The page's buttons. Printer commands are quick MQTT publishes and run here; the camera,
        OBS and Discord ones wait PANEL_WAIT s at most, then carry on in the background. Stop
        stops at once: the panel has already asked to confirm (the deck key still needs two
        presses)."""
        quick = {"pause": (lambda: self.pause(None), "Pausing the print"),
                 "resume": (lambda: self.resume(None), "Resuming the print"),
                 "stop": (self._stop_now, "Stopping the print"),
                 "light_on": (lambda: self.light("on"), "Light on"),
                 "light_off": (lambda: self.light("off"), "Light off")}
        slow = {"camera": (self._panel_camera, None),
                "trust_camera": (lambda: self.camera("trust"), "The printer's certificates will be trusted on the next connection"),
                "setup_obs": (lambda: self.setup_obs(None), "The OBS scene is set up"),
                "scene": (lambda: self.scene(None), "OBS shows the printer camera"),
                "snapshot": (lambda: self.snapshot_now(None), "Posted to Discord")}
        if action_id in quick:
            fn, done = quick[action_id]
            self._panel_call(fn)
            return {"message": done}
        if action_id in slow:
            return self._panel_run(action_id, *slow[action_id])
        return super().panel_action(action_id)

    def _stop_now(self):
        w = self._connected_watcher()
        with self._lock:
            self._stop_pending = None
        self._command(w.stop_print)
        self.log.info("stopping the print on %s (from the control panel)", self.settings.name)

    def _panel_camera(self):
        on = not self._relay_running()
        self.camera("on" if on else "off")
        return "Camera relay started" if on else "Camera relay stopped"

    @staticmethod
    def _panel_call(fn):
        try:
            return fn()
        except RuntimeError as e:   # this module's RuntimeErrors are written for people
            raise ApiError(str(e)) from None

    def _panel_run(self, action_id, fn, done):
        fut = concurrent.futures.Future()

        def work():
            try:
                fut.set_result(fn())
            except BaseException as e:   # noqa: BLE001 - handed to the waiting request
                fut.set_exception(e)
                self.log.warning("panel button %s failed: %s", action_id,
                                 e if isinstance(e, RuntimeError) else type(e).__name__)
        threading.Thread(target=work, name="bambu-panel", daemon=True).start()
        try:
            result = fut.result(PANEL_WAIT)
        except concurrent.futures.TimeoutError:
            return {"message": "Working on it; the status updates when it's done"}
        except RuntimeError as e:
            raise ApiError(str(e)) from None
        return {"message": result if isinstance(result, str) else done}


def setup(bot):
    bot.add_module(Bambu(bot))
