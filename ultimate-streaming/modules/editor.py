"""Web control panel: live deck mirror, OBS and Twitch control, and the key editor.

Open it with `python3 main.py --open-editor`, the "The Ultimate Streaming Control Panel for Linux" app-menu entry
that install.sh adds, or a deck key with `action: {editor: open}`.

Security: the panel listens on 127.0.0.1 only (remote access, below, is separate and off by
default), checks the Host and Origin headers (so other
websites and DNS-rebinding tricks can't reach it), and every API call needs the random
token from ~/.config/ultimate-streaming/editor_token, which only your user can read and which
is replaced each time the daemon starts. Whoever holds that token can do what the deck can do,
including running the shell commands your keys are configured with. Links carry a one-time
launch code, never the token (see core/editor_auth.py). Twitch tokens never leave the daemon.

If the port is taken, the server keeps trying to bind it (BIND_RETRY), and no panel link is
handed out until it has: whoever holds the port would get it.

Remote access (editor.remote, off by default) runs a second listener for other devices (phone,
laptop) on editor.remote.listen:editor.remote.port; the 127.0.0.1 panel and its launch codes are
unchanged. A device pairs: it asks for access and shows a 6-digit code; the same request pops up
in the panel here (and as a desktop notification), and someone at this computer approves it if
the codes match. The device then uses its own token. Only the panel on this computer can approve,
deny or revoke devices or change the `editor` section. core/remote_access.py has the trust model,
core/webapi/server.py the checks each remote request passes. A change to editor.remote restarts
only that listener; turning it off cancels waiting requests but keeps approved devices.

Routes live in core/webapi/*_api.py (live, obs, twitch, plugins, ...); this file keeps the config
editor's own routes, pairing, and the server lifecycle.

config.yaml (optional):
    editor:
      port: 8765
      enabled: true
      remote:
        enabled: false        # true: listen for other devices too
        listen: "0.0.0.0"     # "0.0.0.0" every IPv4 address, "::" IPv4 and IPv6, or one address
        port: 8767            # must differ from editor.port
        hosts: []             # extra names other devices type in the address bar
        device_days: 30       # how long an approval lasts (1-365)
"""
import base64
import collections
import errno
import glob
import io
import logging
import math
import os
import pwd
import re
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer

import yaml

from core import Module, action, listener, notify, open_url, task
from core import configio, editor_auth, request_context
from core.bot import is_blank, resolve_key
from core.models import as_dicts as model_table
from core.paths import ROOT, state_dir, state_file
from core.remote_access import (DEFAULT_PORT as REMOTE_PORT, Devices, Pairing, PeerLimit, Refused, clean_name,
                                is_loopback, normalize_ip, urls)
from core.render import ICON_DIRS, Renderer, valid_color
from core.safefs import write_atomic
from core.schema import UI, Choice
from core.webapi import (ApiError, Router, announce_api, backup_api, bot_api, discord_api, import_api, live_api,
                         obs_api, plugin_api, setup_api, twitch_api)
from core.webapi.server import (LOCAL_ONLY_MESSAGE, MAX_SUBSCRIBERS, AddressCache, FailureLimit, Handler,
                                RemoteServer, StreamSlots, WarnLimit)

STATIC_DIR = os.path.join(ROOT, "editor")
SECRETS = {"ha_token": ("home_assistant", "token_file"), "obs_password": ("obs", "password_file")}
STATUS_EVERY = 2
MAX_RENDER_KEYS = 256   # the library preview sends every saved key in one request
MAX_PARSE_BYTES = 1024 * 1024
BIND_RETRY = (2, 60)    # seconds: first wait after a busy port, doubling up to the second
PAIR_SWEEP_EVERY = 5    # seconds: how often requests that expired unanswered are announced as done
DEVICE_DAYS = (1, 365, 30)   # editor.remote.device_days: lowest, highest, default

# editor.remote as the listener uses it; `error` says why it can't run (None: it can, or it's off)
RemoteSettings = collections.namedtuple("RemoteSettings", "enabled listen port hosts days error")


REDACT = re.compile(r"(?i)(bearer\s+|access_token\W+|refresh_token\W+|device_code\W+|oauth:|\bkey=|"
                    r"webhooks(?:/|%2F))[A-Za-z0-9._~+/%=-]{8,}")


class _LogHandler(logging.Handler):
    """Forwards the daemon's log records (INFO and up) to the panel's activity feed,
    with anything token-shaped redacted first."""

    def __init__(self, hub):
        super().__init__(level=logging.INFO)
        self.hub = hub

    def emit(self, record):
        if record.name.startswith("deck.Editor") or record.name == "deck.http":
            return
        try:
            msg = REDACT.sub(lambda m: m.group(1) + "[redacted]", record.getMessage())[:500]
            self.hub.publish("log", {"level": record.levelname.lower(), "logger": record.name, "msg": msg})
        except Exception:
            pass


class _SizedDeck:
    """Just enough of a deck for the renderer: a key size."""

    def __init__(self, w, h):
        self.size = (w, h)

    def key_image_format(self):
        return {"size": self.size}


class Editor(Module):
    def on_config(self, config):
        super().on_config(config)
        wanted = (config.get("enabled", True), int(config.get("port", editor_auth.DEFAULT_PORT)))
        # Once loaded, a changed port/enabled restarts the server, or starts it if the last
        # start failed (e.g. the port was busy and the user picked another one).
        if hasattr(self, "router") and wanted != self.running_as:
            self._stop()
            self._start()
        if hasattr(self, "router"):
            self._apply_remote()

    def on_load(self):
        self.server, self.running_as = None, None
        self.server_lock, self.bind_stop = threading.Lock(), None
        # remote access: its own listener, never self.server, and never marked bound in editor_auth
        self.remote_server, self.remote_stop, self.remote_error = None, None, None
        self.remote_lock, self.remote_running_as = threading.Lock(), None
        self.remote = self._remote_settings()
        self.addresses = AddressCache()
        self.peer_limit = PeerLimit()
        self.warn_limit = WarnLimit()
        self.pairing = Pairing()
        try:
            devices_path = state_file("remote_devices.json")
        except OSError:   # an unusable state folder: approving a device fails then, with its error
            devices_path = os.path.join(state_dir(), "remote_devices.json")
        self.devices = Devices(devices_path, days=self.remote.days)   # approved devices outlive the listener
        self.pair_lock = threading.Lock()   # start/decide/sweep: pair_request before its pair_done
        self.announced = {}                 # request id -> its expiry, for requests published as pair_request
        self.devices_seen = set()           # ids of devices that have used their token this run
        self.renderers = {}
        self.icon_names = None
        self.hub = self.bot.hub
        self.last_status = None
        self.router = Router()
        self._routes()
        for api in (live_api, obs_api, twitch_api, bot_api, announce_api, discord_api, backup_api, import_api,
                    setup_api, plugin_api):
            api.register(self.router, self)
        self.log_handler = _LogHandler(self.hub)
        logging.getLogger().addHandler(self.log_handler)
        self._start()
        self._start_remote(self.remote)

    def on_unload(self):
        logging.getLogger().removeHandler(self.log_handler)
        self._stop()
        self._stop_remote()

    # ----- server lifecycle

    def _start(self):
        enabled, port = self.config.get("enabled", True), int(self.config.get("port", editor_auth.DEFAULT_PORT))
        self.running_as = (enabled, port)
        if not enabled:
            return
        try:
            self.token = editor_auth.daemon_token()   # a fresh token for each daemon run
        except OSError as e:
            self.log.error("editor: can't write its access token (%s); the control panel is off", e)
            return
        stop = self.bind_stop = threading.Event()
        editor_auth.mark_starting(port)
        if not self._bind(port, stop, first=True) and not stop.is_set():
            threading.Thread(target=self._bind_retry, args=(port, stop), daemon=True, name="editor-bind").start()

    def _bind(self, port, stop, first=False):
        """Listen on 127.0.0.1:port. True when done (bound, or given up for good); False to retry."""
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError as e:
            if e.errno != errno.EADDRINUSE:
                self.log.error("editor: can't listen on 127.0.0.1:%d (%s); set editor.port in config.yaml", port, e)
                stop.set()
                return True
            if first:
                self.log.warning("editor: 127.0.0.1:%d is taken by %s; the control panel stays off and no link "
                                 "is handed out until it's free (retrying), or set editor.port in config.yaml",
                                 port, _holder(port))
            else:
                self.log.debug("editor: 127.0.0.1:%d still taken by %s", port, _holder(port))
            return False
        server.daemon_threads = True
        server.editor = self
        server.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        server.static_dir = STATIC_DIR
        server.stopping = threading.Event()
        server.session_limit = FailureLimit()
        server.stream_slots = StreamSlots(MAX_SUBSCRIBERS)   # counts this listener's streams only
        with self.server_lock:
            if stop.is_set():   # stopped (or restarted on another port) meanwhile
                server.server_close()
                return True
            self.server = server
            editor_auth.mark_bound(port)
            threading.Thread(target=server.serve_forever, daemon=True, name="editor").start()
        self.log.info("control panel on http://127.0.0.1:%d (open it with: python3 main.py --open-editor)", port)
        return True

    def _bind_retry(self, port, stop):
        delay, longest = BIND_RETRY
        while not stop.wait(delay):
            if self._bind(port, stop):
                return
            delay = min(delay * 2, longest)

    def _stop(self):
        if getattr(self, "bind_stop", None):
            self.bind_stop.set()
        with getattr(self, "server_lock", threading.Lock()):
            server, self.server = getattr(self, "server", None), None
            if getattr(self, "running_as", None):
                editor_auth.mark_unbound(self.running_as[1])
        if server:
            server.stopping.set()
            server.shutdown()
            server.server_close()

    # ----- the remote listener's lifecycle

    def _remote_settings(self):
        """editor.remote, checked: RemoteSettings. Bad values are never widened: they keep the
        listener off with a readable `error` (device_days is clamped instead)."""
        cfg = self.config if isinstance(self.config, dict) else {}
        rc = cfg.get("remote") if isinstance(cfg.get("remote"), dict) else {}
        enabled = rc.get("enabled") is True
        listen, error = rc.get("listen"), None
        listen = "0.0.0.0" if listen is None else listen
        try:
            listen = normalize_ip(listen)
        except ValueError:
            listen = str(listen)[:80]
            error = "editor.remote.listen must be 0.0.0.0, :: or one IP address of this computer"
        port = rc.get("port")
        port = REMOTE_PORT if port is None else port
        try:
            if isinstance(port, bool) or (isinstance(port, float) and not port.is_integer()):
                raise ValueError
            port = int(port)
            if not 0 < port < 65536:
                raise ValueError
        except (TypeError, ValueError):
            port, error = REMOTE_PORT, error or "editor.remote.port must be a number from 1 to 65535"
        try:
            panel_port = int(cfg.get("port", editor_auth.DEFAULT_PORT))
        except (TypeError, ValueError):
            panel_port = None
        if error is None and port == panel_port:
            error = (f"editor.remote.port is {port}, the same as editor.port; pick another port for remote "
                     "access (the default is 8767)")
        if error is None and not cfg.get("enabled", True):
            error = "The control panel is off (editor.enabled is false), so remote access is off too"
        raw = rc.get("hosts")
        items = [raw] if isinstance(raw, str) else raw if isinstance(raw, (list, tuple)) else []
        hosts = tuple(dict.fromkeys(h for h in items if isinstance(h, str)))   # (allowed_hosts drops junk)
        low, high, default = DEVICE_DAYS
        days = rc.get("device_days", default)
        if isinstance(days, bool) or not isinstance(days, (int, float)) or not math.isfinite(days):
            days = default
        days = min(high, max(low, days))
        return RemoteSettings(enabled, listen, port, hosts, days, error if enabled else None)

    def _apply_remote(self):
        """After a config change: device_days applies at once; any other change to editor.remote
        restarts the remote listener only. Turned off: waiting requests are cancelled."""
        s = self.remote = self._remote_settings()
        self.devices.days = s.days
        if s._replace(days=None) == self.remote_running_as:
            return
        self._stop_remote()
        if not s.enabled or s.error:
            self._cancel_pairing()
        self._start_remote(s)

    def _start_remote(self, s):
        with self.remote_lock:
            self.remote_running_as = s._replace(days=None)
            self.remote_error = s.error
        if not s.enabled or s.error:
            if s.error:
                self.log.warning("remote access: %s", s.error)
            return
        if is_loopback(s.listen) and s.listen not in ("0.0.0.0", "::"):
            self.log.warning("remote access: editor.remote.listen is %s, which only this computer can reach, and "
                             "requests from this computer are refused there; use 0.0.0.0, :: or a network address",
                             s.listen)
        stop = self.remote_stop = threading.Event()
        if not self._bind_remote(s, stop, first=True) and not stop.is_set():
            threading.Thread(target=self._bind_remote_retry, args=(s, stop), daemon=True,
                             name="editor-remote-bind").start()

    def _bind_remote(self, s, stop, first=False):
        """Listen for other devices. True when done (listening, or given up for good); False to retry
        (the port is busy, or the address isn't on this computer yet, e.g. the network is still coming up)."""
        where = f"[{s.listen}]:{s.port}" if ":" in s.listen else f"{s.listen}:{s.port}"
        try:
            server = RemoteServer(s.listen, s.port, s.hosts, self, STATIC_DIR)
        except OSError as e:
            retry = e.errno in (errno.EADDRINUSE, errno.EADDRNOTAVAIL)
            if e.errno == errno.EADDRINUSE:
                why = f"Port {s.port} is in use by another program, so remote access isn't listening yet (retrying)"
            elif e.errno == errno.EADDRNOTAVAIL:
                why = (f"{s.listen} isn't an address of this computer right now, so remote access isn't "
                       "listening yet (retrying)")
            elif e.errno == errno.EAFNOSUPPORT:
                why = "This computer has no IPv6, so remote access can't listen on it; set editor.remote.listen to 0.0.0.0"
            else:
                why = f"Remote access can't listen on {where}: {e.strerror or e}"
            with self.remote_lock:
                if stop.is_set():
                    return True
                self.remote_error = why
            if first or not retry:
                self.log.warning("remote access: %s", why)
            else:
                self.log.debug("remote access: %s still can't listen: %s", where, e.strerror or e)
            return not retry
        with self.remote_lock:
            if stop.is_set():   # stopped (or restarted with other settings) meanwhile
                server.server_close()
                return True
            self.remote_server, self.remote_error = server, None
            threading.Thread(target=server.serve_forever, daemon=True, name="editor-remote").start()
        self.log.info("remote access: listening on %s for other devices; approve each one in the panel here", where)
        return True

    def _bind_remote_retry(self, s, stop):
        delay, longest = BIND_RETRY
        while not stop.wait(delay):
            if self._bind_remote(s, stop):
                return
            delay = min(delay * 2, longest)

    def _stop_remote(self):
        stop = getattr(self, "remote_stop", None)
        if stop:
            stop.set()
        with getattr(self, "remote_lock", threading.Lock()):
            server, self.remote_server = getattr(self, "remote_server", None), None
        if server:
            server.stopping.set()
            server.shutdown()
            server.close_connections()   # keep-alive connections and event streams end now too
            server.server_close()
            self.hub.publish("ping")      # wakes its event streams so they notice
            self.log.info("remote access: stopped listening on %s",
                          f"[{server.listen}]:{server.port}" if ":" in server.listen else f"{server.listen}:{server.port}")

    # ----- pairing (see core/remote_access.py)

    def pair_start(self, ip, name):
        """POST /api/pair/start from the device at `ip` (the remote listener checked it): announce
        the request to the panels here and the desktop. Refused propagates."""
        with self.pair_lock:
            out = self.pairing.start(ip, name)
            event = {"id": out["request"], "name": clean_name(name), "ip": normalize_ip(ip), "code": out["code"],
                     "expires": out["expires"]}
            self.announced[out["request"]] = out["expires"]
            self.hub.publish("pair_request", event)
        self.log.info("remote access: %r (%s) asks to use the control panel", event["name"], event["ip"])
        code = event["code"]
        shown = re.sub(r"[<>&]", "", event["name"]) or "A device"   # notification bodies may be markup
        notify(f"{shown} wants to use the control panel. Code {code[:3]} {code[3:]}. Approve it in the panel.")
        return out

    def decide_pairing(self, body):
        """POST /api/remote/decide {"request", "allow"} (this computer's panel only)."""
        if request_context.remote_device() is not None:
            raise ApiError(LOCAL_ONLY_MESSAGE, 403)
        rid, allow = body.get("request"), body.get("allow")
        if not isinstance(rid, str) or not rid:
            raise ApiError("request is required")
        if not isinstance(allow, bool):
            raise ApiError("allow must be true or false")
        with self.pair_lock:
            asked = next((p for p in self.pairing.pending() if p["id"] == rid), None)
            try:
                self.pairing.decide(rid, allow, self.devices)
            except Refused as e:
                self._sweep_pairing()   # it may have just expired: say so now
                raise ApiError(e.message, e.status) from None
            except OSError as e:
                raise ApiError(f"Couldn't save the approved device ({e.strerror or e}); try again", 500) from None
            self.announced.pop(rid, None)
            self.hub.publish("pair_done", {"id": rid, "allowed": allow})
        self.log.info("remote access: %s %r (%s)", "approved" if allow else "denied",
                      asked["name"] if asked else "?", asked["ip"] if asked else "?")
        return {"ok": True}

    def revoke_device(self, body):
        """POST /api/remote/revoke {"device"} (this computer's panel only)."""
        if request_context.remote_device() is not None:
            raise ApiError(LOCAL_ONLY_MESSAGE, 403)
        did = body.get("device")
        if not isinstance(did, str) or not did:
            raise ApiError("device is required")
        known = next((d for d in self.devices.list() if d["id"] == did), None)
        try:
            found = self.devices.revoke(did)
        except OSError as e:
            self.hub.publish("ping")   # it is revoked for this run: end its event stream now
            self.log.warning("remote access: revoked %r, but couldn't save that: %s", known and known["name"], e)
            raise ApiError(f"The device is signed out, but that couldn't be saved ({e.strerror or e}), so it may "
                           "come back after the daemon restarts", 500) from None
        if not found:
            raise ApiError("No such device", 404)
        self.hub.publish("ping")   # wakes the event streams: the revoked device's own ends now
        self.log.info("remote access: revoked %r (%s)", known["name"] if known else "?", known["ip"] if known else "?")
        return {"ok": True}

    def _sweep_pairing(self):
        """pair_done (not allowed) for each announced request that expired unanswered. Lock held."""
        live = {p["id"] for p in self.pairing.pending()}
        for rid in [r for r in self.announced if r not in live]:
            del self.announced[rid]
            self.hub.publish("pair_done", {"id": rid, "allowed": False})

    def _cancel_pairing(self):
        """Remote access is off: deny every waiting request (pair_done, not allowed, for each)."""
        if not hasattr(self, "pairing"):
            return
        with self.pair_lock:
            waiting = self.pairing.pending()
            for p in waiting:
                try:
                    self.pairing.decide(p["id"], False, self.devices)
                except Refused:
                    pass   # expired meanwhile
            for rid in list(self.announced):
                self.hub.publish("pair_done", {"id": rid, "allowed": False})
            self.announced.clear()
        if waiting:
            self.log.info("remote access: off; cancelled %d waiting request%s", len(waiting), "" if len(waiting) == 1 else "s")

    @task(every=PAIR_SWEEP_EVERY, on_page_change=False)
    def expire_pairing(self):
        """Requests that ran out of time unanswered: their pop-ups close in every panel here."""
        if not getattr(self, "announced", None):
            return
        with self.pair_lock:
            self._sweep_pairing()

    def remote_seen(self, device, ip):
        """A device's token was accepted: logged the first time each device uses it in this run."""
        with self.remote_lock:
            first = device["id"] not in self.devices_seen
            self.devices_seen.add(device["id"])
        if first:
            self.log.info("remote access: %r is using the control panel (from %s)", device["name"], ip)

    def remote_warn(self, ip, reason, fmt, *args):
        """A refusal on the remote listener, logged at most once a minute per (address, reason);
        `ip` is the peer's remote_access.peer_key, so an IPv6 /64 counts as one address."""
        if self.warn_limit((ip, reason)):
            self.log.warning(fmt, *args)

    def remote_status(self):
        """GET /api/status's "remote" part. A device sees no waiting requests, and which device it is.
        (Before on_load, as in some tests: the settings, and nothing running.)"""
        s = getattr(self, "remote", None) or self._remote_settings()
        server, device = getattr(self, "remote_server", None), request_context.remote_device()
        cache, devices, pairing = (getattr(self, name, None) for name in ("addresses", "devices", "pairing"))
        snap = cache.get() if cache else None
        local = list(snap.local) if snap else []
        return {"enabled": s.enabled, "listen": s.listen, "port": s.port,
                "urls": urls(server.listen, server.port, local, snap.hostname) if server and snap else [],
                "addresses": local, "error": getattr(self, "remote_error", None),
                "devices": devices.list() if devices else [],
                "pending": pairing.pending() if pairing and not device else [],
                "this_device": device["id"] if device else None}

    # ----- events for the panel

    @task(every=STATUS_EVERY, on_page_change=False)
    def push_status(self):
        """Every module's snapshot(), published as one "status" event when it changes."""
        if not (self.server or getattr(self, "remote_server", None)) or not self.hub.subscribers:
            return
        snap = self.snapshot_all()
        if snap != self.last_status:
            self.last_status = snap
            self.hub.publish("status", snap)

    def snapshot_all(self):
        out = {}
        for inst in list(self.bot.modules.values()):
            try:
                s = inst.snapshot()
            except Exception as e:
                self.log.debug("%s.snapshot: %s", type(inst).__name__, e)
                s = None
            if s is not None:
                out[inst.config_key] = s
        return out

    @listener("chat_message")
    def on_chat(self, msg):
        self.hub.publish("chat", {"id": msg.get("id"), "user": msg.get("user"), "user_id": msg.get("user_id"),
                                  "text": msg.get("text"), "color": msg.get("color") or "",
                                  "mod": bool(msg.get("mod")), "sub": bool(msg.get("sub")),
                                  "badges": list(msg.get("badges") or [])})

    # ----- the editor's own routes

    def _routes(self):
        r = self.router
        r.add_get("/api/state", lambda q: {**self.state(), "secrets": self.secrets_present()})
        r.add_get("/api/status", lambda q: self.status())
        r.add_get("/api/options", lambda q: {"options": self.options(q.get("name", ""))})
        r.add_get("/api/icons", lambda q: {"icons": self.icons(q.get("q", ""))})
        r.add_post("/api/render", self.render)
        r.add_post("/api/save", self.save)
        r.add_post("/api/run", self.run)
        r.add_post("/api/secret", self.secret)
        r.add_post("/api/session/revoke", self.revoke_sessions)
        r.add_post("/api/remote/decide", self.decide_pairing)
        r.add_post("/api/remote/revoke", self.revoke_device)
        r.add_post("/api/obs/build", self.build_obs)
        r.add_post("/api/client-error", self.client_error)
        r.add_post("/api/validate", lambda b: {"problems": self.validate(b.get("config") or {})})
        r.add_post("/api/yaml/parse", self.parse_yaml)
        r.add_post("/api/yaml/dump", lambda b: {"text": yaml.safe_dump(b.get("value"), sort_keys=False,
                                                                        allow_unicode=True, width=100)})

    def parse_yaml(self, body):
        text = body.get("text") or ""
        if not isinstance(text, str):
            raise ApiError("text must be a string")
        if len(text.encode("utf-8", "surrogatepass")) > MAX_PARSE_BYTES:
            raise ApiError("that YAML is too big (over 1 MB)", 413)
        return {"value": configio.safe_load_bounded(text)}

    @action("editor", ui=UI("Open this editor", "Deck", arg=Choice(("open", "open"))))
    def open_editor(self, arg):
        server = self.server
        if not server:
            self.log.warning("editor: the control panel isn't listening (see the log above), so it can't be opened")
            return
        try:
            open_url(editor_auth.url({"editor": {"port": server.server_address[1]}}))
        except (editor_auth.Unavailable, OSError) as e:
            self.log.warning("editor: can't open the control panel: %s", e)

    def revoke_sessions(self, body):
        """Sign out every open panel: a new token, unused links stop working, and every approved
        remote device is revoked (this computer's panel only)."""
        if request_context.remote_device() is not None:
            raise ApiError(LOCAL_ONLY_MESSAGE, 403)
        self.token = editor_auth.rotate()
        failed = None
        try:
            self.devices.revoke_all()
        except OSError as e:   # revoked in memory all the same
            failed = e
        self.hub.publish("signed_out", {})   # wakes the event streams so they end now
        self.log.info("signed out all control panels")
        if failed:
            self.log.warning("remote access: signed out every device, but couldn't save that: %s", failed)
            raise ApiError(f"Every panel is signed out, but the list of approved devices couldn't be saved "
                           f"({failed.strerror or failed}), so they may come back after the daemon restarts", 500)
        self.log.info("remote access: revoked every approved device")
        return {"ok": True}

    # ----- API

    def catalog(self):
        def entries(registry, kind):
            out = {}
            for name, fn in sorted(list(registry.items())):   # a snapshot: hot-reload may swap it
                ui = getattr(fn, "__deck_ui__", None) or {"label": name, "group": "Other"}
                entry = {"name": name, **ui}
                if kind == "action":
                    entry["on_release"] = bool(fn.__deck_action__[1])
                out[name] = entry
            return out
        return {"actions": entries(self.bot.actions, "action"), "states": entries(self.bot.state_providers, "state"),
                "displays": entries(self.bot.display_providers, "display"),
                "expanders": entries(self.bot.expanders, "expander"), "sources": sorted(self.bot.sources)}

    def device(self):
        deck = self.bot.deck
        if not deck:
            return None
        rows, cols = deck.key_layout()
        info = {"model": deck.deck_type(), "rows": rows, "cols": cols, "dials": deck.dial_count(),
                "touch_keys": deck.touch_key_count(), "visual": deck.is_visual(), "layout": self.bot.layout_name}
        if deck.is_visual():
            info["key_size"] = list(deck.key_image_format()["size"])
        return info

    def decks(self):
        """info() of every attached deck; [] on a core without multi-deck support (hot-reload)."""
        sessions = getattr(self.bot, "sessions", None)
        return [s.info() for s in list(sessions.values())] if sessions else []

    def state(self):
        path = self.bot.config_path
        try:
            cfg = configio.load(path)
        except (FileNotFoundError, yaml.YAMLError, ValueError):
            # config.yaml is missing or broken: show what the daemon runs on (the backup it fell
            # back to, see status()["config_fallback"]) so it can be fixed and saved over it.
            cfg = self.bot.cfg
        return {"config": bot_api.mask_config(cfg), "hash": configio.file_hash(path), "path": path,
                "comments": configio.comment_count(path, os.path.join(self.bot.root, "config.example.yaml")),
                "catalog": self.catalog(), "device": self.device(), "models": model_table(),
                "page": self.bot.page, "devices": self.decks()}

    def options(self, name):
        fn = self.bot.sources.get(name)
        if not fn:
            raise LookupError(f"no source {name!r}")
        out = []
        for item in fn() or []:
            out.append(item if isinstance(item, dict) else {"value": item, "label": str(item)})
        return out

    def render(self, body):
        """Render keys exactly as the deck would draw them (live expanders included)."""
        keys = body.get("keys") or []
        if not isinstance(keys, list):
            raise ApiError("keys must be a list")
        if len(keys) > MAX_RENDER_KEYS:
            raise ApiError(f"at most {MAX_RENDER_KEYS} keys per render")
        w, h = (max(16, min(512, int(v))) for v in body.get("size") or (96, 96))
        renderer = self.renderers.get((w, h))
        if not renderer:
            if len(self.renderers) > 8:
                self.renderers.clear()
            renderer = self.renderers[(w, h)] = Renderer(_SizedDeck(w, h), ROOT, self.bot.cfg.get("font"),
                                                         self.bot.cfg.get("emoji_font"))
        lib = body.get("library") or {}
        images = []
        for key in keys:
            try:
                btn = resolve_key(key, lib)
                if btn:
                    expanded = self.bot._expand(btn)
                    if expanded is None:  # e.g. scene_slot 7 when OBS has 6 scenes
                        kind = next((k for k in list(self.bot.expanders) if k in btn), None)
                        expanded = {"label": f"{kind.replace('_', ' ')}\n{btn[kind]}" if kind else "?",
                                    "bg": "#2a2a30", "fg": "#9a9aa5"}
                    if body.get("active") and isinstance(expanded.get("active"), dict):
                        expanded = {**expanded, **expanded["active"]}
                    btn = expanded
                if not btn:
                    images.append(None)
                    continue
                buf = io.BytesIO()
                renderer.image(btn).save(buf, "PNG")
                images.append("data:image/png;base64," + base64.b64encode(buf.getvalue()).decode())
            except Exception as e:
                self.log.debug("render %r: %s", key, e)
                images.append(None)
        return {"images": images}

    def validate(self, cfg):
        """Problems worth showing before saving: unknown actions, missing pages/saved keys, bad colors."""
        problems = []
        lib = cfg.get("keys") if isinstance(cfg.get("keys"), dict) else {}
        layouts = dict(cfg.get("layouts") or {})
        if cfg.get("pages"):
            layouts["(all decks)"] = {"pages": cfg["pages"]}

        def check_action(a, where):
            for step in (a if isinstance(a, list) else [a]):
                if isinstance(step, dict):
                    for kind, arg in step.items():
                        if kind not in self.bot.actions:
                            problems.append(f"{where}: unknown action '{kind}'")
                        if kind == "page" and arg != "back" and arg not in page_names:
                            problems.append(f"{where}: goes to page '{arg}', which this layout doesn't have")

        def check_colors(btn, where):
            # `on:` is the YAML boolean True as a key (see DeckSession.appearance).
            looks = [btn] + [btn[k] for k in ("active", "on", True) if isinstance(btn.get(k), dict)]
            for look in looks:
                for f in ("bg", "fg"):
                    if f in look and look[f] is not None and not valid_color(look[f]):
                        problems.append(f"{where}: {f} {look[f]!r} isn't a color (use e.g. #9146ff or purple)")

        for lname, lay in layouts.items():
            pages = lay.get("pages", lay) if isinstance(lay, dict) else {}
            if not isinstance(pages, dict):
                continue
            page_names = set(pages)
            for pname, page in pages.items():
                rows = page.get("keys", []) if isinstance(page, dict) else page
                flat = [k for r in (rows or []) for k in (r if isinstance(r, list) else [r])]
                if isinstance(page, dict):
                    flat += list(page.get("touch_keys") or []) + list(page.get("dials") or [])
                for key in flat:
                    if is_blank(key):
                        continue
                    ref = key if isinstance(key, str) else (key.get("use") if isinstance(key, dict) else None)
                    if ref is not None and ref not in lib:
                        problems.append(f"{lname} › {pname}: no saved key named '{ref}'")
                        continue
                    btn = resolve_key(key, lib) or {}
                    where = f"{lname} › {pname} › {btn.get('label', '?')}".replace("\n", " ")
                    for f in ("action", "left", "right", "press"):
                        if f in btn:
                            check_action(btn[f], where)
                    check_colors(btn, where)
        return sorted(set(problems))

    def save(self, body):
        cfg = body.get("config")
        if not isinstance(cfg, dict) or not (cfg.get("pages") or cfg.get("layouts")):
            raise ValueError("the config needs at least one layout with a page")
        try:
            on_disk = configio.load(self.bot.config_path)
        except (FileNotFoundError, yaml.YAMLError, ValueError):
            on_disk = {}   # missing or broken: saving over it is how it gets fixed
        # state() sent the database URL masked; from a broken file it sent the running config's
        cfg = bot_api.restore_secrets(cfg, on_disk or self.bot.cfg)
        new_hash = configio.save(self.bot.config_path, cfg, body.get("hash"))   # hash: of the raw bytes
        if getattr(self.bot, "config_fallback", None) or getattr(self.bot, "config_error", None):
            self.bot.clear_config_fallback()
        return {"hash": new_hash, "problems": self.validate(cfg)}

    def run(self, body):
        """Run actions now ("Try it"). Waits briefly so quick errors can be shown."""
        result = {}

        def work():
            try:
                self.bot.run_action(body.get("action"))
                result["ok"] = True
            except Exception as e:
                result["error"] = str(e)
        t = threading.Thread(target=work, daemon=True)
        t.start()
        t.join(4)
        if t.is_alive():
            return {"ok": True, "running": True}
        return {"ok": "error" not in result, "error": result.get("error")}

    def status(self):
        cfg = self.bot.cfg
        # {"revision": id or None, "error": "..."} while config.yaml didn't load at start, else None
        st = {"deck": self.device(), "decks": self.decks(),
              "config_fallback": getattr(self.bot, "config_fallback", None),
              # {"error": "...", "since": unix time} while a reload of config.yaml failed, else None
              "config_error": getattr(self.bot, "config_error", None)}
        obs = self.bot.get_module("OBS")
        try:
            if not obs:
                raise RuntimeError("OBS module not loaded")
            obs.send("GetVersion")
            collections = obs.send("GetSceneCollectionList")
            want = (cfg.get("obs_setup") or {}).get("collection", "Stream Deck")
            prefixes = ("pulse_", "alsa_", "jack_", "pipewire-audio", "coreaudio", "wasapi")
            all_inputs = obs.send("GetInputList").get("inputs", [])
            audio = sorted({i["inputName"] for i in all_inputs if i.get("inputKind", "").startswith(prefixes)}
                           | {v for v in (obs.send("GetSpecialInputs") or {}).values() if v})
            radio = self.bot.get_module("Radio")
            music = (radio.source in {i["inputName"] for i in all_inputs}) if radio else None
            st["obs"] = {"ok": True, "collection": collections.get("currentSceneCollectionName"),
                         "scenes_built": want in collections.get("sceneCollections", []), "collection_name": want,
                         "audio": audio, "music": music}
        except Exception as e:
            st["obs"] = {"ok": False, "error": str(e) or type(e).__name__}
        tw = self.bot.get_module("Twitch")
        chat = self.bot.get_module("Chat")
        st["twitch"] = {"channel": bool(tw and tw.channel), "client_id": bool(tw and tw.client_id),
                        "linked": bool(tw and tw.linked()), "chat": bool(chat and chat.connected)}
        ha = self.bot.get_module("HomeAssistant")
        st["home_assistant"] = {"configured": bool(ha and ha.url and ha.headers)}
        try:
            st["plugins"] = plugin_api.plugins_status(self.bot)
        except Exception as e:
            self.log.warning("listing plugins for the panel failed: %s", e)
            st["plugins"] = []
        st["remote"] = self.remote_status()
        return st

    def client_error(self, body):
        """The page reports its uncaught JS errors here so they show up in the daemon's log."""
        self.log.warning("panel error: %s\n%s", str(body.get("message", ""))[:500], str(body.get("stack", ""))[:2000])
        return {"ok": True}

    def icons(self, query):
        if self.icon_names is None:
            names = set()
            for base in ICON_DIRS:
                for p in glob.iglob(os.path.join(base, "**", "*.png"), recursive=True):
                    names.add(os.path.splitext(os.path.basename(p))[0])
            self.icon_names = sorted(names)
        q = query.lower().strip()
        hits = [n for n in self.icon_names if q in n.lower()] if q else []
        hits.sort(key=lambda n: (not n.lower().startswith(q), len(n)))
        return hits[:60]

    def secret(self, body):
        name, value = body.get("name"), (body.get("value") or "").strip()
        if name not in SECRETS:
            raise ValueError("unknown secret")
        path = state_file(name)
        if not value:
            if os.path.exists(path):
                os.remove(path)
            return {"path": None}
        write_atomic(path, (value + "\n").encode(), mode=0o600)
        return {"path": path.replace(os.path.expanduser("~"), "~", 1), "section": SECRETS[name][0],
                "field": SECRETS[name][1]}

    def secrets_present(self):
        return {name: os.path.exists(state_file(name)) for name in SECRETS}

    def build_obs(self, body):
        cmd = [sys.executable, os.path.join(ROOT, "obs", "build_scenes.py"), "--config", self.bot.config_path]
        if body.get("audio"):
            cmd += ["--audio"]
        elif body.get("scene"):
            cmd += ["--scene", str(body["scene"])]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            return {"ok": False, "output": "timed out after 3 minutes"}
        return {"ok": p.returncode == 0, "output": (p.stdout + p.stderr).strip()}


def _holder(port):
    """Who listens on 127.0.0.1:port, for the log: 'uid 1001 (alice)', or 'another program'."""
    uids = editor_auth.listener_uids(port)
    if not uids:
        return "another program"
    names = []
    for uid in sorted(uids):
        try:
            names.append(f"uid {uid} ({pwd.getpwuid(uid).pw_name})")
        except KeyError:
            names.append(f"uid {uid}")
    return ", ".join(names) + (" (you)" if uids == {os.getuid()} else "")


def setup(bot):
    bot.add_module(Editor(bot))
