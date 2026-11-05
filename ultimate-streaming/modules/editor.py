"""Web control panel: live deck mirror, OBS and Twitch control, and the key editor.

Open it with `python3 main.py --open-editor`, the "The Ultimate Streaming Control Panel for Linux" app-menu entry
that install.sh adds, or a deck key with `action: {editor: open}`.

Security: it listens on 127.0.0.1 only, checks the Host and Origin headers (so other
websites and DNS-rebinding tricks can't reach it), and every API call needs the random
token from ~/.config/ultimate-streaming/editor_token, which only your user can read and which
is replaced each time the daemon starts. Whoever holds that token can do what the deck can do,
including running the shell commands your keys are configured with. Links carry a one-time
launch code, never the token (see core/editor_auth.py). Twitch tokens never leave the daemon.

If the port is taken, the server keeps trying to bind it (BIND_RETRY), and no panel link is
handed out until it has: whoever holds the port would get it.

Routes live in core/webapi/*_api.py (live, obs, twitch, plugins, ...); this file keeps the config
editor's own routes and the server lifecycle.

config.yaml (optional):
    editor:
      port: 8765
      enabled: true
"""
import base64
import errno
import glob
import io
import logging
import os
import pwd
import re
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer

import yaml

from core import Module, action, listener, open_url, task
from core import configio, editor_auth
from core.bot import is_blank, resolve_key
from core.models import as_dicts as model_table
from core.paths import ROOT, state_file
from core.render import ICON_DIRS, Renderer, valid_color
from core.safefs import write_atomic
from core.schema import UI, Choice
from core.webapi import (ApiError, Router, announce_api, backup_api, bot_api, discord_api, import_api, live_api,
                         obs_api, plugin_api, setup_api, twitch_api)
from core.webapi.server import FailureLimit, Handler

STATIC_DIR = os.path.join(ROOT, "editor")
SECRETS = {"ha_token": ("home_assistant", "token_file"), "obs_password": ("obs", "password_file")}
STATUS_EVERY = 2
MAX_RENDER_KEYS = 256   # the library preview sends every saved key in one request
MAX_PARSE_BYTES = 1024 * 1024
BIND_RETRY = (2, 60)    # seconds: first wait after a busy port, doubling up to the second


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

    def on_load(self):
        self.server, self.running_as = None, None
        self.server_lock, self.bind_stop = threading.Lock(), None
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

    def on_unload(self):
        logging.getLogger().removeHandler(self.log_handler)
        self._stop()

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

    # ----- events for the panel

    @task(every=STATUS_EVERY, on_page_change=False)
    def push_status(self):
        """Every module's snapshot(), published as one "status" event when it changes."""
        if not self.server or not self.hub.subscribers:
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
        """Sign out every open panel: a new token, and unused links stop working."""
        self.token = editor_auth.rotate()
        self.hub.publish("signed_out", {})   # wakes the event streams so they end now
        self.log.info("signed out all control panels")
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
