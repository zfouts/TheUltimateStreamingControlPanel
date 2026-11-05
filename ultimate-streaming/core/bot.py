"""DeckBot: drives every attached Stream Deck and dispatches to modules.

Works with every Stream Deck model python-elgato-streamdeck supports (Mini, Original/MK.2,
Neo, XL, +, + XL, Studio, Pedal). Pages are picked per device from `layouts:` (by model
name or key grid, e.g. "8x4"), falling back to `pages:`. Dials and the touch strip on
the + models run per-page `dials:` actions.

Modules register actions, states, displays, button expanders and tasks with
decorators (see core/module.py). The bot hot-reloads config.yaml and any
changed module file, and attaches decks as they are plugged in (and drops them when
unplugged). Each deck is a DeckSession with its own layout, page, history and
brightness (`devices: {<serial>: {name, layout, brightness, start_page}}` in config.yaml);
modules are shared. Code that runs for a key/dial/touch sees that deck as bot.session()
(and through the back-compat bot.page, bot.goto(), ...); elsewhere that is the first deck.
"""
import contextvars
import functools
import hashlib
import importlib
import importlib.util
import json
import logging
import os
import sys
import threading
import time
import types

import yaml

from . import hid  # noqa: F401  (must run before StreamDeck.DeviceManager is imported)
from StreamDeck.DeviceManager import DeviceManager
from StreamDeck.Devices.StreamDeck import DialEventType, TouchscreenEventType

from . import backups, chatcmd, configio, paths, plugins
from .events import Hub
from .render import Renderer
from .util import notify

log = logging.getLogger("deck")

SCAN_EVERY = 3   # seconds between device enumerations (costly) while looking for new decks
UNLOAD_TIMEOUT = 10   # seconds an extension's on_unload gets on its unload-<name> thread
UNLOAD_GRACE = 0.1    # how long the main loop waits for that thread, so a quick unload ends in one pass

# Serial of the deck whose key/dial/touch is being handled (see DeckBot.session()).
_current_deck = contextvars.ContextVar("deck_serial", default=None)

MARKERS = {
    "__deck_action__": "actions",
    "__deck_state__": "state_providers",
    "__deck_display__": "display_providers",
    "__deck_expander__": "expanders",
    "__deck_source__": "sources",
}


def is_blank(key):
    return key is None or key == "-" or key == ""


def layout_grid(name):
    """(cols, rows) from a layout name like "8x4", else None."""
    try:
        c, r = (int(v) for v in str(name).lower().split("x"))
        return c, r
    except ValueError:
        return None


def resolve_key(key, lib):
    """A key as written in config → a key dict (or None for blank).
    "name" and {use: name, ...} pull in a saved key from `keys:`; extra fields override it."""
    if is_blank(key):
        return None
    if isinstance(key, str):
        key = {"use": key}
    if not isinstance(key, dict):
        return None
    if "use" in key:
        base = lib.get(key["use"])
        if not isinstance(base, dict):
            log.warning("no saved key named %r (add it under `keys:`)", key["use"])
            return {"label": f"? {key['use']}", "bg": "#5a1a1a"}
        return {**base, **{k: v for k, v in key.items() if k != "use"}}
    return key


def _config_problem(e):
    """A short, user-facing description of why config.yaml didn't load, with the line if YAML says."""
    if isinstance(e, yaml.MarkedYAMLError) and e.problem_mark is not None:
        mark = e.problem_mark
        what = " ".join(p for p in (e.context, e.problem) if p) or "invalid YAML"
        return f"line {mark.line + 1}, column {mark.column + 1}: {what}"
    if isinstance(e, yaml.YAMLError):
        return f"invalid YAML: {e}"
    return str(e) or type(e).__name__


class DeckBot:
    def __init__(self, root, config_path):
        self.root, self.config_path = root, config_path
        self.sessions = {}     # serial -> DeckSession, in attach order (the first is the primary)
        self.cfg = {}
        self._cfg_key = None   # (st_mtime_ns, st_size) of config.yaml when it was last read whole
        self._cfg_hash = None  # sha256 of the config.yaml bytes last applied
        self._cfg_problem = None    # (key, message) of the last bad config.yaml logged
        self._cfg_missing = False   # config.yaml is gone (logged once, and once more when it's back)
        # Not None while the daemon runs on something other than config.yaml because that file
        # didn't load at start: {"revision": <backups revision id, or None for an empty config>,
        # "error": <what is wrong with config.yaml>}. /api/status shows it to the panel.
        self.config_fallback = None
        # Not None after a reload of config.yaml failed while the daemon runs on the config from
        # before: {"error": <what is wrong>, "since": <unix time of the first failure>}.
        self.config_error = None
        self._brightness = 60  # top-level `brightness:` (what bot.brightness says with no deck)
        self.lock = threading.RLock()   # one lock for every deck and the module registries
        self._remembered = {}  # serial -> (page, history, brightness) of a detached deck, for a replug
        self._ignored = set()  # device ids the legacy `device:` filter rejected (until a config reload)
        self._last_watch, self._last_daily, self._last_scan = 0, time.time(), 0
        self.modules = {}      # class name -> Module instance
        self.extensions = {}   # "modules.obs" -> {"path", "mtime", "instances"};
                               # "plugin:<name>" -> {"path", "fingerprint", "instances", "plugin"}
        # The registries below (and `modules`) are copy-on-write: add_module/remove_module build
        # new ones and swap them in under self.lock, so key and web threads can iterate them
        # while a module is hot-reloaded.
        self.actions, self.state_providers, self.display_providers, self.expanders = {}, {}, {}, {}
        self.sources = {}      # editor dropdown name -> fn()
        # (registry, name) -> the Module instance that registered it. Kept apart from the entry,
        # which others may replace by a plain function (tools/devserver.py's dry run does).
        self._owners = {}
        self.tasks = []        # [bound fn, every, next_run]
        self.listeners = {}    # event name -> [bound fn]
        self.hub = Hub()       # events for the web control panel (core/events.py)
        self._loading = None
        self._plugins_on = False   # load_plugins() was called: watch_plugins() keeps them in sync
        self._plugin_errors = {}   # plugin name -> the error last logged for it (logged once)
        # Extensions whose on_unload runs on a background thread (unload_extension(wait=False)):
        # name -> {"thread", "deadline", "ext"}; and the load to run once each one is over.
        self._unloading = {}
        self._queued = {}          # name -> load(), run by _finish_unloads()

    # ------------------------------------------------------------------ modules

    def load_extensions(self, package):
        """Load every modules/*.py file (files starting with _ are skipped)."""
        self.package = package
        for fname in sorted(os.listdir(os.path.join(self.root, package))):
            if fname.endswith(".py") and not fname.startswith("_"):
                self.load_extension(f"{package}.{fname[:-3]}")

    def load_extension(self, name):
        """Import a module file and call its setup(bot), like discord.py's load_extension."""
        path = os.path.join(self.root, *name.split(".")) + ".py"
        ext = {"path": path, "mtime": os.path.getmtime(path), "instances": []}
        with self.lock:
            self.extensions = {**self.extensions, name: ext}
        self._loading = name
        try:
            pymod = importlib.reload(sys.modules[name]) if name in sys.modules else importlib.import_module(name)
            pymod.setup(self)
            log.info("loaded %s", name)
        except Exception:
            log.exception("failed to load %s", name)
        finally:
            self._loading = None

    def unload_extension(self, name, wait=True):
        """Unload an extension in two steps. Its modules' actions, states, displays, listeners,
        tasks and chat built-ins leave the registries at once, so nothing new reaches them; then
        their on_unload runs. With wait=False on_unload runs on a thread named unload-<name>
        (it may wait seconds for the module's own threads) and _finish_unloads(), called from
        the watchers, completes the unload when that thread is done or UNLOAD_TIMEOUT is up."""
        with self.lock:
            ext = self.extensions.get(name)
            if ext is None:
                return
            self.extensions = {k: v for k, v in self.extensions.items() if k != name}
        instances = list(ext["instances"])
        for inst in instances:
            self._detach_module(inst)
        if ext.get("plugin"):
            chatcmd.unregister_owned(self, instances)   # in case its on_unload doesn't
        if wait:
            self._run_unload(instances)
            self._unloaded(ext)
            return
        thread = threading.Thread(target=self._run_unload, args=(instances,), daemon=True,
                                  name=f"unload-{ext.get('plugin') or name}")
        with self.lock:
            self._unloading[name] = {"thread": thread, "ext": ext,
                                     "deadline": time.monotonic() + UNLOAD_TIMEOUT}
        thread.start()
        thread.join(UNLOAD_GRACE)
        self._finish_unloads(only=name)

    def reload_extension(self, name, wait=True):
        """Unload `name` and load its code again. The new code's on_load never runs before the
        old instance's on_unload is over: with wait=False the load is queued behind it."""
        ext = self.extensions.get(name) or {}
        self.unload_extension(name, wait=wait)
        if ext.get("plugin"):
            self._load_after_unload(name, functools.partial(self.load_plugin, ext["plugin"], ext["path"]))
        else:
            self._load_after_unload(name, functools.partial(self.load_extension, name))
        self.invalidate()

    def unload_all(self):
        """Daemon shutdown: unload every extension, waiting for each (and for unloads already
        running in the background). Nothing queued is loaded."""
        with self.lock:
            self._queued = {}
        self._finish_unloads(wait=True)
        for name in list(self.extensions):
            self.unload_extension(name)

    def _run_unload(self, instances):
        for inst in instances:
            try:
                inst.on_unload()
            except Exception:
                log.exception("%s failed to unload", type(inst).__name__)

    def _unloaded(self, ext):
        """The last step of an unload, once on_unload is over (or overran): forget plugin code."""
        if ext.get("plugin"):
            self._purge_plugin(ext["plugin"])

    def _load_after_unload(self, name, load):
        """Run load() now, or, while `name` is still unloading in the background, queue it for
        _finish_unloads() (a later request replaces an earlier one). True if it ran now."""
        with self.lock:
            if name in self._unloading:
                self._queued[name] = load
                return False
        load()
        return True

    def _finish_unloads(self, wait=False, only=None):
        """Complete the background unloads (or just `only`'s) that are over: their thread
        finished or its UNLOAD_TIMEOUT is up (logged; it is left to finish on its own). Then run
        the load queued behind each. With wait, wait for every one instead, and load nothing
        (shutdown)."""
        with self.lock:
            jobs = [(k, v) for k, v in self._unloading.items() if only is None or k == only]
        for name, job in jobs:
            thread = job["thread"]
            if wait:
                thread.join(max(0.0, job["deadline"] - time.monotonic()))
            if thread.is_alive() and time.monotonic() < job["deadline"]:
                continue
            with self.lock:
                if self._unloading.get(name) is not job:
                    continue   # another caller completed it
                del self._unloading[name]
                load = None if wait else self._queued.pop(name, None)
            if thread.is_alive():
                log.warning("%s didn't finish unloading within %s s; carrying on without it",
                            name, UNLOAD_TIMEOUT)
            self._unloaded(job["ext"])
            if load is not None:
                try:
                    load()
                except Exception:
                    log.exception("loading %s failed", name)
                self.invalidate()

    def add_module(self, inst):
        """Register a Module instance (call from an extension's setup).

        A plugin's module may not take anything core or another module already has (its class
        name, config section, or any action/state/display/expander/source name): it is refused
        with ValueError, which fails the plugin's load. Core modules override each other with
        a warning."""
        name = type(inst).__name__
        ext = self.extensions.get(self._loading) if self._loading else None
        plugin = ext.get("plugin") if ext else None
        marked, tasks, listeners = [], [], []   # marked: (registry name, key, bound fn)
        for attr in dir(type(inst)):
            fn = getattr(inst, attr)
            for marker, registry in MARKERS.items():
                if hasattr(fn, marker):
                    key = getattr(fn, marker)
                    marked.append((registry, key[0] if isinstance(key, tuple) else key, fn))
            if hasattr(fn, "__deck_task__"):
                tasks.append([fn, fn.__deck_task__, 0])
            if hasattr(fn, "__deck_listener__"):
                listeners.append(fn)
        if plugin:
            inst.config_key = inst.config_key or plugin
            with self.lock:
                problem = self._plugin_conflict(inst, name, marked)
            if problem:
                ext["refused"] = f"plugin {plugin}: {problem}"
                raise ValueError(ext["refused"])
        old = self.modules.get(name)
        if old is not None:
            self.remove_module(old)
        with self.lock:
            if ext is not None:
                ext["instances"] = [*ext["instances"], inst]
                inst.config_key = inst.config_key or self._loading.rsplit(".", 1)[-1]
            regs = {registry: dict(getattr(self, registry)) for registry in MARKERS.values()}
            owners = dict(self._owners)
            for registry, key, fn in marked:
                owner = self._registrant(registry, key)
                if owner is not None and owner is not inst:
                    log.warning("%s overrides %s's %r", name, type(owner).__name__, key)
                regs[registry][key] = fn
                owners[registry, key] = inst
            new_listeners = {event: list(fns) for event, fns in self.listeners.items()}
            for fn in listeners:
                new_listeners.setdefault(fn.__deck_listener__, []).append(fn)
            for registry, reg in regs.items():
                setattr(self, registry, reg)
            self._owners = owners
            self.tasks = [*self.tasks, *tasks]
            self.listeners = new_listeners
            self.modules = {**self.modules, name: inst}
        try:
            inst.on_config(self.cfg.get(inst.config_key) or {})
            inst.on_load()
        except Exception:
            log.exception("%s failed to start", name)

    def _plugin_conflict(self, inst, name, marked):
        """Why the plugin module `inst` (being loaded) may not be registered, or None."""
        mine = set(map(id, self.extensions[self._loading]["instances"]))
        if name in self.modules:
            return f"module class {name!r} is already used by {self._owner_of(self.modules[name])}"
        key = inst.config_key
        if key in plugins.RESERVED or key in plugins.core_module_names():
            return f"config section {key!r} belongs to the app itself"
        for other in list(self.modules.values()):
            if other.config_key == key and id(other) not in mine:
                return f"config section {key!r} is already used by {self._owner_of(other)}"
        for registry, key, _fn in marked:
            owner = self._registrant(registry, key)
            if owner is not None and owner is not inst:
                return f"{name} declares {registry[:-1].replace('_', ' ')} {key!r}, already owned by {type(owner).__name__}"
        return None

    def _owner_of(self, inst):
        for ext_name, ext in list(self.extensions.items()):
            if any(i is inst for i in ext["instances"]):
                return ext_name
        return type(inst).__name__

    def _registrant(self, registry, key):
        """The Module instance that registered `key` in `registry` (None if nothing has)."""
        fn = getattr(self, registry).get(key)
        if fn is None:
            return None
        return self._owners.get((registry, key)) or getattr(fn, "__self__", None)

    def remove_module(self, inst):
        try:
            inst.on_unload()
        except Exception:
            log.exception("%s failed to unload", type(inst).__name__)
        self._detach_module(inst)

    def _detach_module(self, inst):
        """Take everything `inst` registered out of the registries (fast; under the lock)."""
        with self.lock:
            for registry in MARKERS.values():
                setattr(self, registry, {k: fn for k, fn in getattr(self, registry).items()
                                         if self._registrant(registry, k) is not inst})
            self._owners = {k: owner for k, owner in self._owners.items() if owner is not inst}
            self.tasks = [t for t in self.tasks if t[0].__self__ is not inst]
            self.listeners = {event: [fn for fn in fns if fn.__self__ is not inst]
                              for event, fns in self.listeners.items()}
            name = type(inst).__name__
            if self.modules.get(name) is inst:
                self.modules = {k: v for k, v in self.modules.items() if k != name}

    def dispatch(self, event, *args):
        """Call every module's @listener(event) with args; one failing listener doesn't stop the rest."""
        for fn in list(self.listeners.get(event, [])):
            try:
                fn(*args)
            except Exception:
                log.exception("listener %s for %r failed", fn.__qualname__, event)

    def get_module(self, name):
        """Another module by class name, like discord.py's get_cog."""
        return self.modules.get(name)

    def watch_extensions(self):
        """Reload module files that changed on disk, and load new ones. Unloads run in the
        background (unload_extension(wait=False)), so a slow on_unload doesn't hold the loop."""
        self._finish_unloads()
        folder = os.path.join(self.root, self.package)
        present = set()
        for fname in os.listdir(folder):
            name = f"{self.package}.{fname[:-3]}"
            if fname.endswith(".py") and not fname.startswith("_"):
                present.add(name)
                if name not in self.extensions and self._load_after_unload(
                        name, functools.partial(self.load_extension, name)):
                    self.invalidate()
        with self.lock:
            self._queued = {k: v for k, v in self._queued.items()
                            if k.startswith("plugin:") or k in present}
        for name, ext in list(self.extensions.items()):
            if ext.get("plugin"):
                continue   # watch_plugins() looks after these
            try:
                if os.path.getmtime(ext["path"]) != ext["mtime"]:
                    log.info("reloading %s", name)
                    self.reload_extension(name, wait=False)
            except FileNotFoundError:
                log.info("unloading removed %s", name)
                self.unload_extension(name, wait=False)
                self.invalidate()

    # ------------------------------------------------------------------ plugins

    def load_plugins(self):
        """Load every installed plugin (core/plugins.py) as the package deck_plugins.<name>, and
        from now on let watch_plugins() load, reload and unload them as they change on disk."""
        self._plugins_on = True
        if plugins.PACKAGE not in sys.modules:
            stub = types.ModuleType(plugins.PACKAGE, "Installed plugins (see core/plugins.py).")
            stub.__path__ = []   # nothing is found by name: each plugin is imported from its folder
            sys.modules[plugins.PACKAGE] = stub
        self._sync_plugins()

    def watch_plugins(self):
        """Load new plugins, reload changed ones, unload removed or broken ones (after load_plugins()).
        Unloads run in the background and a reload's load waits for its unload (see
        unload_extension), so this never waits long on a plugin's on_unload."""
        if self._plugins_on:
            self._finish_unloads()
            self._sync_plugins()

    def _sync_plugins(self):
        self._plugin_vendor_path()
        items = plugins.installed()
        wanted, changed = set(), False
        for name in set(self._plugin_errors) - {item["name"] for item in items}:
            del self._plugin_errors[name]
        for item in items:
            name, key = item["name"], f"plugin:{item['name']}"
            if item["error"]:
                if self._plugin_errors.get(name) != item["error"]:
                    self._plugin_errors[name] = item["error"]
                    log.warning("plugin %s not loaded: %s", name, item["error"])
                if key in self.extensions:
                    self.unload_extension(key, wait=False)
                    changed = True
                continue
            self._plugin_errors.pop(name, None)
            wanted.add(key)
            ext = self.extensions.get(key)
            load = functools.partial(self.load_plugin, name, item["path"])
            if ext is None:
                changed |= self._load_after_unload(key, load)   # queued while its old copy unloads
            elif ext["path"] != item["path"] or self._fingerprint(item["path"]) != ext["fingerprint"]:
                log.info("reloading plugin %s", name)
                self.unload_extension(key, wait=False)
                self._load_after_unload(key, load)
                changed = True
        for key in [k for k, ext in self.extensions.items() if ext.get("plugin") and k not in wanted]:
            log.info("unloading removed plugin %s", self.extensions[key]["plugin"])
            self.unload_extension(key, wait=False)
            changed = True
        with self.lock:   # a plugin removed or broken while its reload was queued: don't load it
            self._queued = {k: v for k, v in self._queued.items()
                            if not k.startswith("plugin:") or k in wanted}
        if changed:
            self.invalidate()

    def load_plugin(self, name, path):
        """Import the plugin folder `path` as deck_plugins.<name> and call its setup(bot). On any
        failure everything it registered is removed again and its code forgotten; the record is
        kept, so fixing a file loads it."""
        key, modname = f"plugin:{name}", f"{plugins.PACKAGE}.{name}"
        ext = {"path": path, "fingerprint": self._fingerprint(path), "instances": [], "plugin": name}
        with self.lock:
            self.extensions = {**self.extensions, key: ext}
        self._purge_plugin(name)
        importlib.invalidate_caches()   # a just-installed folder or dependency must be seen
        self._loading = key
        try:
            spec = importlib.util.spec_from_file_location(
                modname, os.path.join(path, "__init__.py"), submodule_search_locations=[path])
            pymod = importlib.util.module_from_spec(spec)
            sys.modules[modname] = pymod   # before exec: its relative imports look it up
            spec.loader.exec_module(pymod)
            pymod.setup(self)
            if ext.get("refused"):   # setup() caught add_module's ValueError: still a failure
                raise ValueError(ext["refused"])
            log.info("loaded plugin %s", name)
            return True
        except (Exception, SystemExit):
            log.exception("failed to load plugin %s", name)
            for inst in reversed(ext["instances"]):
                self.remove_module(inst)
            chatcmd.unregister_owned(self, ext["instances"])
            ext["instances"] = []
            ext.pop("refused", None)
            self._purge_plugin(name)
            return False
        finally:
            self._loading = None

    def _purge_plugin(self, name):
        """Forget deck_plugins.<name> and its submodules, so the next import runs the files again."""
        modname = f"{plugins.PACKAGE}.{name}"
        for mod in [m for m in sys.modules if m == modname or m.startswith(modname + ".")]:
            del sys.modules[mod]
        package = sys.modules.get(plugins.PACKAGE)
        if package is not None and name in vars(package):
            delattr(package, name)

    def _plugin_vendor_path(self):
        """Put the plugins' shared dependency folder on sys.path right after the app's vendor/
        (so the app's own pinned copies win), once."""
        vendor = paths.plugin_vendor_dir()
        if vendor in sys.path or not os.path.isdir(vendor):
            return
        app_vendor = os.path.join(self.root, "vendor")
        at = sys.path.index(app_vendor) + 1 if app_vendor in sys.path else len(sys.path)
        sys.path.insert(at, vendor)

    @staticmethod
    def _fingerprint(path):
        """(relative path, mtime_ns, size) of every .py file in a plugin folder, sorted, plus the
        identity of the plugin-vendor folder: the installer swaps that in by rename, so new
        dependencies reload every plugin (and retry one that failed for lack of them)."""
        try:
            st = os.stat(paths.plugin_vendor_dir())
            vendor = (st.st_ino, st.st_mtime_ns)
        except OSError:
            vendor = None
        out = []
        for folder, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", "vendor")]
            for f in files:
                if f.endswith(".py"):
                    full = os.path.join(folder, f)
                    try:
                        st = os.stat(full)
                    except OSError:
                        continue
                    out.append((os.path.relpath(full, path), st.st_mtime_ns, st.st_size))
        return tuple(sorted(out)), vendor

    # ------------------------------------------------------------------ config

    def load_config(self, initial=False):
        """Read config.yaml if it changed on disk and apply it; True if a new config was applied.

        Never raises for a bad file. A reload that fails keeps the config already running. At
        start (initial=True) there is nothing to keep, so the newest kept revision that loads is
        used instead, in memory only (config.yaml is left as it is for the panel to fix), or an
        empty config if none does: either way the modules, and with them the control panel and
        its restore page, still come up. That is recorded in config_fallback.

        A change is noticed by (mtime_ns, size), and only a file that didn't change while it was
        read is applied (otherwise it's read again on the next call), so a save caught halfway
        is never applied; a rewrite with identical content isn't applied twice."""
        data, key = None, None
        for _ in range(5 if initial else 1):
            try:
                st = os.stat(self.config_path)
                key = (st.st_mtime_ns, st.st_size)
                if key == self._cfg_key:
                    return False
                with open(self.config_path, "rb") as f:
                    data = f.read()
                st = os.stat(self.config_path)
            except FileNotFoundError:
                return self._config_gone(initial)
            except OSError as e:   # e.g. no permission: report it like a broken file
                data, problem = None, f"can't read it: {e.strerror or e}"
                break
            if (st.st_mtime_ns, st.st_size) == key and len(data) == key[1]:
                break
            log.debug("config.yaml changed while it was being read; reading it again")
            if not initial:
                return False
            data = None   # still unstable after the retries: treated as broken for now
            time.sleep(0.05)
        else:
            key, problem = None, "it kept changing while it was being read"
        if self._cfg_missing:
            self._cfg_missing = False
            log.info("config.yaml is back (%s)", self.config_path)
        if data is not None:
            digest = hashlib.sha256(data).hexdigest()
            if digest == self._cfg_hash and not self.config_fallback:
                self._cfg_key = key   # same bytes as what's running: nothing to apply
                if self._set_config_error(None):
                    self.publish_config()
                return False
            try:
                cfg = self._parse_config(data)
            except Exception as e:
                problem = _config_problem(e)
            else:
                self._cfg_key, self._cfg_problem = key, None
                was = self.config_fallback
                self._apply_config(cfg, digest, None)
                if was:
                    log.info("config.yaml loads again; no longer running on %s",
                             f"the backup {was['revision']}" if was["revision"] else "an empty config")
                self.backup("record", "start" if initial else "file")
                if initial:
                    self.backup("daily")
                self.publish_config()
                return True
        self._cfg_key = key   # a broken file is reported once, not on every call (None: retry)
        if initial or (key, problem) != self._cfg_problem:
            self._cfg_problem = (key, problem)
            log.error("config not loaded: %s: %s", self.config_path, problem)
            if not initial and not self.config_fallback:
                log.error("still running on the config loaded before; fix config.yaml (or restore a "
                          "backup in the control panel) before the daemon next restarts")
        if initial:
            self._start_on_fallback(problem)
            return False
        with self.lock:
            fallback = self.config_fallback
            if fallback and fallback["error"] != problem:
                self.config_fallback = {**fallback, "error": problem}
            else:
                fallback = None
            changed = self._set_config_error(problem)
        if fallback or changed:
            self.publish_config()
        return False

    def _set_config_error(self, problem):
        """Record (or, with None, clear) config_error; `since` is kept while the same error
        persists. True if it changed."""
        with self.lock:
            cur = self.config_error
            if problem is None:
                new = None
            elif cur and cur["error"] == problem:
                new = cur
            else:
                new = {"error": problem, "since": int(time.time())}
            self.config_error = new
            return new != cur

    def _config_gone(self, initial):
        """config.yaml is missing: logged once (and once more when it's back), never fatal."""
        self._cfg_key = None
        if not self._cfg_missing:
            self._cfg_missing = True
            log.warning("config.yaml is missing (%s); %s", self.config_path,
                        "starting on the newest backup that loads" if initial
                        else "still running on the config loaded before")
        if initial:
            self._start_on_fallback("config.yaml is missing")
        return False

    @staticmethod
    def _parse_config(data):
        """config.yaml's bytes → the config dict to run; raises (YAMLError, ValueError) if it's unusable."""
        cfg = configio.safe_load_bounded(data) or {}
        if not isinstance(cfg, dict):
            raise ValueError("config.yaml must be a mapping of settings (`pages:`, `keys:`, ...)")
        if not cfg.get("pages") and not cfg.get("layouts"):
            raise ValueError("config has no pages (or layouts)")
        ver = cfg.get("version", 1)
        if isinstance(ver, int) and ver > configio.CONFIG_VERSION:
            log.warning("config.yaml is version %d but this daemon understands up to %d; "
                        "update ultimate-streaming (some settings may be ignored)", ver, configio.CONFIG_VERSION)
        return configio.upgrade(cfg)

    def _start_on_fallback(self, problem):
        """config.yaml didn't load at start: run on the newest revision that loads (in memory;
        nothing is written), else on an empty config, and say so in config_fallback."""
        rid, cfg, digest = None, {}, None
        try:
            ids = backups._rev_ids()
        except OSError as e:
            log.warning("couldn't list the config backups: %s", e)
            ids = []
        broken = self._file_hash()
        for candidate in reversed(ids):
            try:
                data = backups._read_rev(candidate)
                h = hashlib.sha256(data).hexdigest()
                if h == broken:
                    continue   # the broken file itself, kept by an earlier save
                cfg, rid, digest = self._parse_config(data), candidate, h
                break
            except Exception as e:
                log.debug("backup %s doesn't load either: %s", candidate, e)
        self._apply_config(cfg, digest, {"revision": rid, "error": problem})
        if rid:
            log.error("running on the backup %s until config.yaml is fixed (config.yaml was not changed; "
                      "fix it, or restore a backup, in the control panel)", rid)
        else:
            log.error("no backup of config.yaml loads either: running with an empty config; "
                      "fix config.yaml, or restore a backup, in the control panel")
        self.publish_config()

    def _apply_config(self, cfg, digest, fallback):
        with self.lock:
            self.cfg, self._cfg_hash, self.config_fallback = cfg, digest, fallback
            self.config_error = None
            self._brightness = cfg.get("brightness", 60)
            self._ignored.clear()
            for s in list(self.sessions.values()):
                s.brightness = s.configured_brightness()
            self.select_layout()
            for inst in list(self.modules.values()):
                try:
                    inst.on_config(cfg.get(inst.config_key) or {})
                except Exception:
                    log.exception("%s rejected the config", type(inst).__name__)
        log.info("config loaded (%d pages, layout %s)", len(self.pages), self.layout_name)

    def clear_config_fallback(self):
        """The panel saved a good config.yaml over the broken one: no longer on a fallback (the
        next load_config applies the saved file)."""
        with self.lock:
            had_error = self.config_error is not None
            self.config_error = None
            if not self.config_fallback:
                if had_error:
                    self._cfg_problem = None
                    self.publish_config()
                return
            self.config_fallback = None
            self._cfg_problem = None
        log.info("config.yaml was saved from the control panel; loading it")
        self.publish_config()

    def publish_config(self):
        self.hub.publish("config", {"hash": self._file_hash(), "config_fallback": self.config_fallback,
                                      "config_error": self.config_error})
        self.publish_decks()

    def _file_hash(self):
        """config.yaml's hash ("" if it's missing or can't be read)."""
        try:
            return configio.file_hash(self.config_path)
        except OSError:
            return ""

    def backup(self, what, *args):
        """backups.record / backups.daily for config.yaml; a failure is logged, never fatal."""
        try:
            return getattr(backups, what)(self.config_path, *args)
        except Exception as e:
            log.warning("config backup (%s) failed: %s", what, e)

    # ------------------------------------------------------------------ decks

    def session(self, serial=None):
        """The DeckSession to act on: `serial`'s (None if it isn't attached); else the deck whose
        key/dial/touch is being handled (or run_action(serial=)); else the primary deck."""
        sessions = self.sessions
        if serial is not None:
            return sessions.get(serial)
        current = _current_deck.get()
        if current is not None and current in sessions:
            return sessions[current]
        return next(iter(sessions.values()), None)

    @property
    def primary(self):
        """The first attached deck (what single-deck code means by "the deck")."""
        return next(iter(self.sessions.values()), None)

    def publish_decks(self):
        self.hub.publish("decks", {"decks": [s.info() for s in self.sessions.values()]})

    def device_config(self, serial):
        """devices[serial] from config.yaml ({} if absent). YAML may have read a serial as a number."""
        devices = self.cfg.get("devices")
        if not isinstance(devices, dict) or serial is None:
            return {}
        entry = devices.get(serial)
        if entry is None:
            entry = next((v for k, v in devices.items() if str(k) == str(serial)), None)
        return entry if isinstance(entry, dict) else {}

    def wanted(self, serial, model):
        """Legacy `device: {serial, model}`: when set, only matching decks are driven."""
        want = self.cfg.get("device") or {}
        if not isinstance(want, dict):
            return True
        return want.get("serial") in (None, serial) and want.get("model") in (None, model)

    # Back-compat: single-deck code reads and writes these on the bot; they act on self.session().

    def _state(name, default):
        def get(self):
            s = self.session()
            return getattr(s, name) if s else default()

        def put(self, value):
            s = self.session()
            if s:
                setattr(s, name, value)
        return property(get, put)

    renderer = _state("renderer", lambda: None)
    page = _state("page", lambda: None)
    pages = _state("pages", dict)
    page_dials = _state("page_dials", dict)
    layout_name = _state("layout_name", lambda: None)
    history = _state("history", list)
    states = _state("states", dict)
    displays = _state("displays", dict)
    next_poll = _state("next_poll", dict)
    armed = _state("armed", lambda: None)
    del _state

    @property
    def deck(self):
        s = self.session()
        return s.deck if s else None

    @deck.setter
    def deck(self, deck):
        """`bot.deck = X` (tests, tools): X replaces the current deck and becomes the primary;
        `bot.deck = None` detaches the current deck."""
        current = self.session()
        if current and current.deck is deck:
            return
        if current:
            self.detach_deck(current.serial)
        if deck is not None:
            self._attach(deck, first=True)

    @property
    def brightness(self):
        s = self.session()
        return s.brightness if s else self._brightness

    @brightness.setter
    def brightness(self, level):
        s = self.session()
        if s:
            s.brightness = level
        else:
            self._brightness = level

    def grid(self):
        """(rows, cols) of the current deck; (4, 8) before one is attached."""
        s = self.session()
        return s.deck.key_layout() if s else (4, 8)

    def select_layout(self):
        """Pick every attached deck's pages (see DeckSession.select_layout)."""
        with self.lock:
            for s in list(self.sessions.values()):
                s.select_layout()

    def library(self):
        """Saved keys: `keys:` in config.yaml, used on pages by name or as {use: name, ...overrides}."""
        lib = self.cfg.get("keys")
        return lib if isinstance(lib, dict) else {}

    @staticmethod
    def _flatten(page, keys, rows, cols, extra=0, offset=0):
        """A page is a flat list of keys (filled left→right, top→bottom), a list of rows
        (lists), or a dict {keys: ..., dials: [...], touch_keys: [...]}. Returns a flat list
        for this grid, followed by the `extra` touch keys (the Neo's two). Keys are left
        unresolved (names / {use:} are looked up by the caller); `-` and null are blanks.
        `offset` shifts every row right, to center a layout made for a narrower deck."""
        touch = []
        if isinstance(keys, dict):
            touch = list((keys.get("touch_keys") or [])[:extra])
            keys = keys.get("keys") or []
        keys = keys or []
        touch += [None] * (extra - len(touch))
        if not any(isinstance(k, list) for k in keys):
            flat = list(keys[:rows * cols])
            if len(keys) > rows * cols:
                log.warning("page %r has %d keys; this deck has %d", page, len(keys), rows * cols)
            return flat + [None] * (rows * cols - len(flat)) + touch
        flat = [None] * (rows * cols)
        for r, row in enumerate(k if isinstance(k, list) else [k] for k in keys):
            width = cols - offset if r < rows else 0
            if len(row) > width and any(not is_blank(k) for k in row[width:]):
                log.warning("page %r row %d doesn't fit a %dx%d deck; extra keys dropped", page, r + 1, cols, rows)
            for c, key in enumerate(row[:width]):
                flat[r * cols + c + offset] = key
        return flat + touch

    # ------------------------------------------------------------------ buttons, drawing, navigation
    # (back-compat: these act on self.session(); invalidate() on every deck)

    def raw_buttons(self, page=None):
        """The page's button list exactly as written in config.yaml."""
        s = self.session()
        return s.raw_buttons(page) if s else []

    def buttons(self, page=None):
        s = self.session()
        return s.buttons(page) if s else {}

    def _expand(self, btn):
        for name, fn in list(self.expanders.items()):
            if name in btn:
                try:
                    return fn(btn[name], {k: v for k, v in btn.items() if k != name})
                except Exception as e:
                    log.debug("expanding %s: %s", name, e)
                    return None
        return btn

    def appearance(self, key, page=None):
        s = self.session()
        return s.appearance(key, page) if s else {}

    def dials(self, page=None):
        s = self.session()
        return s.dials(page) if s else []

    def draw_key(self, key, pressed=False):
        s = self.session()
        if s:
            s.draw_key(key, pressed)

    def draw_page(self):
        s = self.session()
        if s:
            s.draw_page()

    def draw_extras(self):
        s = self.session()
        if s:
            s.draw_extras()

    def invalidate(self):
        """Forget cached states/displays and redraw every deck (e.g. after buttons changed)."""
        with self.lock:
            for s in list(self.sessions.values()):
                try:
                    s.invalidate()
                except DeckLost as e:
                    log.warning("redrawing %s: %s", s.name, e)

    def goto(self, page):
        s = self.session()
        if s:
            return s.goto(page)
        if page != "back":
            raise ValueError(f"no such page {page!r}")

    def set_brightness(self, level):
        s = self.session()
        if s:
            s.set_brightness(level)
        else:
            self._brightness = max(0, min(100, int(level)))

    def flash_error(self, key, page):
        s = self.session()
        if s:
            s.flash_error(key, page)

    def page_changed(self):
        """A deck changed page: tasks marked on_page_change run again right away."""
        for t in self.tasks:
            if getattr(t[0], "__deck_task_on_page__", True):
                t[2] = 0

    # ------------------------------------------------------------------ actions

    @staticmethod
    def _unreviewed(spec):
        """True for an imported action / state / display still waiting for review in the panel
        ({needs_review: ...}): it never runs, is never polled and never shows anything."""
        return isinstance(spec, dict) and "needs_review" in spec

    @staticmethod
    def _refuse_unreviewed(action):
        """Imported actions wait for review in the panel; check a whole list before running any of it."""
        items = action if isinstance(action, list) else [action]
        for a in items:
            if DeckBot._unreviewed(a):
                log.warning("refusing action needs_review (imported, not reviewed yet)")  # never the argument
                raise ValueError("this imported action needs to be reviewed in the panel before it can run")
            if isinstance(a, list):
                DeckBot._refuse_unreviewed(a)

    def run_action(self, action, serial=None):
        """Run an action; with `serial`, "as" that deck (page/brightness actions act on it)."""
        if serial is not None:
            token = _current_deck.set(serial)
            try:
                return self.run_action(action)
            finally:
                _current_deck.reset(token)
        self._refuse_unreviewed(action)
        if isinstance(action, list):
            for a in action:
                self.run_action(a)
            return
        if isinstance(action, str):
            action = {"run": action}
        for kind, arg in action.items():
            fn = self.actions.get(kind)
            if not fn:
                raise ValueError(f"unknown action {kind!r} (is its module loaded?)")
            log.info("action %s", kind)   # the panel's activity feed shows INFO; the argument may be private
            log.debug("action %s: %s", kind, arg)
            fn(arg)

    def run_in_background(self, action, what):
        def work():
            try:
                self.run_action(action)
            except Exception as e:
                log.error("%s failed: %s", what, e)
                notify(f"{what}: {e}")
        start_thread(work)

    def _session_for(self, deck):
        """The session driving `deck` (a device object from a callback; None if it was detached);
        no deck (the web panel's press) means self.session()."""
        if deck is None:
            return self.session()
        return next((s for s in self.sessions.values() if s.deck is deck), None)

    def _as_deck(self, deck, method, *args):
        s = self._session_for(deck)
        if s is None:
            return
        token = _current_deck.set(s.serial)
        try:
            getattr(s, method)(*args)
        finally:
            _current_deck.reset(token)

    def on_key(self, deck, key, down):
        self._as_deck(deck, "on_key", key, down)

    def on_dial(self, deck, dial, event, value):
        """dials: [{label, left: action, right: action, press: action}, ...] on the page."""
        self._as_deck(deck, "on_dial", dial, event, value)

    def on_touch(self, deck, event, value):
        """Tapping the touch strip above a dial = pressing that dial."""
        self._as_deck(deck, "on_touch", event, value)

    # ------------------------------------------------------------------ polling

    def check_state(self, spec):
        if self._unreviewed(spec):
            return False            # imported, not reviewed yet: no provider is called
        providers = self.state_providers   # one snapshot: a hot-reload swaps in a new dict
        for name, value in spec.items():
            if name in providers:
                return bool(providers[name](value, spec))
        raise ValueError(f"no module provides state {spec}")

    def read_display(self, spec):
        if self._unreviewed(spec):
            return {}               # imported, not reviewed yet: no provider is called
        fn = self.display_providers.get(spec["source"])
        if not fn:
            raise ValueError(f"no module provides display {spec['source']!r}")
        return fn(spec) or {}

    def poll(self):
        """Expire confirm keys, run due tasks, then poll every deck's states/displays. A deck whose
        I/O fails is marked failed (the run loop detaches it); nothing here raises."""
        sessions = list(self.sessions.values())
        for s in sessions:
            self._poll_session(s, s.expire_armed)
        now = time.time()
        for t in list(self.tasks):
            if t[2] <= now:
                t[2] = now + t[1]
                try:
                    t[0]()
                except Exception:
                    log.exception("task %s failed", t[0].__qualname__)
        for s in sessions:
            self._poll_session(s, s.poll)

    def _poll_session(self, s, fn):
        token = _current_deck.set(s.serial)
        try:
            fn()
        except DeckLost as e:
            log.warning("%s", e)
        except Exception:
            log.exception("polling %s failed", s.name)
        finally:
            _current_deck.reset(token)

    # ------------------------------------------------------------------ device lifecycle

    def attach_deck(self, deck):
        """Start driving `deck`: open, reset, brightness, renderer, layout, draw, callbacks."""
        return self._attach(deck)

    def _attach(self, deck, first=False):
        is_open = getattr(deck, "is_open", None)
        if not (is_open and is_open()):
            deck.open()
        deck.reset()
        serial = _serial_of(deck)
        with self.lock:
            old = self.sessions.get(serial)
            if old and old.deck is deck:
                return old
        if old and old.id != _deck_id(deck):
            n = 2
            while f"{serial}#{n}" in self.sessions:
                n += 1
            log.warning("two decks report serial %r; calling this one %s#%d", serial, serial, n)
            serial = f"{serial}#{n}"
        elif old:
            self.detach_deck(serial)
        s = DeckSession(self, deck, serial)
        page, history, brightness = self._remembered.pop(serial, (None, [], None))
        s.page, s.history = page, history
        s.brightness = s.configured_brightness() if brightness is None else brightness
        if deck.is_visual():
            deck.set_brightness(max(0, min(100, int(s.brightness))))
        with self.lock:
            s.select_layout()
            s.renderer = Renderer(deck, self.root, self.cfg.get("font"), self.cfg.get("emoji_font"))
            if first:
                self.sessions = {serial: s, **self.sessions}
            else:
                # The biggest deck is the primary (what single-deck features and the panel default
                # to), whatever order USB happened to list them in; ties keep attach order.
                merged = {**self.sessions, serial: s}
                self.sessions = dict(sorted(merged.items(), key=lambda kv: -_key_count(kv[1].deck)))
            s.invalidate()
        deck.set_key_callback(self.on_key)
        if deck.dial_count():
            deck.set_dial_callback(self.on_dial)
        if deck.is_touch():
            deck.set_touchscreen_callback(self.on_touch)
        rows, cols = deck.key_layout()
        log.info("attached %s, %dx%d keys, %d dials (serial %s); layout %s",
                 deck.deck_type(), cols, rows, deck.dial_count(), serial, s.layout_name)
        self.publish_decks()
        return s

    def detach_deck(self, serial):
        """Stop driving a deck (unplugged, failed, or replaced). Its page is kept for a replug."""
        with self.lock:
            s = self.sessions.get(serial)
            if not s:
                return
            self.sessions = {k: v for k, v in self.sessions.items() if k != serial}
            s.detached = True
            self._remembered[serial] = (s.page, s.history, s.brightness)
        try:
            s.deck.close()
        except Exception as e:
            log.debug("closing %s: %s", s.name, e)
        log.info("detached %s (serial %s)", s.name, serial)
        self.publish_decks()

    def scan_devices(self):
        """Attach decks that appeared (and pass the legacy `device:` filter). Costly: the run
        loop calls it at most every SCAN_EVERY seconds."""
        try:
            found = DeviceManager().enumerate()
        except Exception as e:
            log.debug("listing decks: %s", e)
            return
        attached = {s.id for s in self.sessions.values()}
        ids = [_deck_id(d) for d in found]
        self._ignored &= set(ids)
        for deck, did in zip(found, ids):
            if did in attached or did in self._ignored:
                continue
            try:
                deck.open()
                serial, model = deck.get_serial_number(), deck.deck_type()
            except Exception as e:
                log.debug("opening %s: %s", did, e)
                _close(deck)
                continue
            if not self.wanted(serial, model):
                log.info("not driving %s (serial %s): config `device:` picks another deck", model, serial)
                self._ignored.add(did)
                _close(deck)
                continue
            try:
                self.attach_deck(deck)
            except Exception as e:
                log.warning("couldn't attach %s (serial %s): %s", model, serial, e)
                _close(deck)

    def apply_config(self):
        """After a config reload: new renderer (fonts), brightness and a redraw on every deck."""
        for s in list(self.sessions.values()):
            try:
                with self.lock:
                    s.renderer = Renderer(s.deck, self.root, self.cfg.get("font"), self.cfg.get("emoji_font"))
                    if s.deck.is_visual():
                        s.set_brightness(s.brightness)
                    s.invalidate()
            except DeckLost as e:
                log.warning("%s", e)
            except Exception:
                log.exception("applying config to %s failed", s.name)

    def tick(self):
        """One pass of the run loop. Only a deck's own I/O failing (or it vanishing) detaches it;
        module, config and editor errors are logged and never cost a deck."""
        now = time.time()
        if now - self._last_daily > 3600:
            self._last_daily = now
            self.backup("daily")
        if now - self._last_scan >= SCAN_EVERY:
            self._last_scan = now
            self.scan_devices()
        for s in list(self.sessions.values()):
            if s.failed or not s.present():
                log.warning("deck lost: %s (serial %s)%s; waiting for it", s.name, s.serial,
                            " stopped responding" if s.failed else " unplugged")
                self.detach_deck(s.serial)
        if now - self._last_watch > 1:
            self._last_watch = now
            try:
                self.watch_extensions()
            except Exception:
                log.exception("reloading modules failed")
            try:
                self.watch_plugins()
            except Exception:
                log.exception("reloading plugins failed")
            try:
                changed = self.load_config()
            except Exception:
                log.exception("reloading config failed")
                changed = False
            if changed:
                self.apply_config()
        if self.sessions:
            self.poll()

    def run(self):
        self._last_watch, self._last_daily, self._last_scan = 0, time.time(), 0
        while True:
            try:
                self.tick()
                time.sleep(0.25 if self.sessions else 1)
            except KeyboardInterrupt:
                break
            except Exception:
                log.exception("main loop")
                try:
                    time.sleep(1)
                except KeyboardInterrupt:
                    break
        for s in list(self.sessions.values()):
            try:
                with s.deck:
                    s.deck.reset()
            except Exception:
                pass
            self.detach_deck(s.serial)
        self.unload_all()


class DeckLost(IOError):
    """Writing to a deck failed: the run loop detaches that deck (and only that one)."""


def _key_count(deck):
    try:
        return deck.key_count()
    except Exception:
        return 0


class DeckSession:
    """One attached deck: its device, renderer, pages and navigation state."""

    POLL_SLOW = 1.0      # a state/display call slower than this (s) is backed off like a failing one
    POLL_BUDGET = 1.5    # a poll pass starts no new call after this long (s); the rest wait a pass
    BACKOFF_MAX = 60     # longest wait (s) before retrying a failing/slow provider
    clock = staticmethod(time.monotonic)   # polling schedule (a test swaps in a fake clock)

    def __init__(self, bot, deck, serial):
        self.bot, self.deck, self.serial = bot, deck, serial
        self.id = _deck_id(deck)
        self.model = deck.deck_type()
        self.renderer = None
        self.layout_name, self.pages, self.page_dials = None, {}, {}
        self.page, self.history = None, []
        self.states = {}       # (page, key) -> bool from a `state:`
        self.displays = {}     # (page, key) -> appearance overrides from a `display:`
        self.next_poll = {}    # (page, key[, "d"]) -> when to poll again (self.clock())
        self.backoff = {}      # (provider, spec json) -> (seconds, retry at) while it fails or is slow
        self.armed = None      # (page, key, expiry) while a `confirm:` key waits for a 2nd press
        self.brightness = 60
        self.failed = False    # a write to the device failed: the run loop detaches it
        self.detached = False

    @property
    def name(self):
        return self.config().get("name") or self.model

    def config(self):
        """devices[<serial>] from config.yaml."""
        return self.bot.device_config(self.serial)

    def configured_brightness(self):
        return self.config().get("brightness", self.bot.cfg.get("brightness", 60))

    def info(self):
        rows, cols = self.deck.key_layout()
        visual = bool(self.deck.is_visual())
        return {"serial": self.serial, "model": self.model, "name": self.name, "rows": rows, "cols": cols,
                "dials": self.deck.dial_count(), "touch_keys": self.deck.touch_key_count(), "visual": visual,
                "key_size": list(self.deck.key_image_format()["size"]) if visual else None,
                "layout": self.layout_name, "page": self.page, "brightness": self.brightness}

    def present(self):
        """Cheap unplug check: deck.connected() enumerates every HID device, which
        costs ~10% CPU at our poll rate; the hidraw node vanishes on unplug instead."""
        device = getattr(self.deck, "device", None)
        if device is None:
            return True   # a FakeDeck
        try:
            path = device.path()
            path = path.decode() if isinstance(path, bytes) else path
            return os.path.exists(path) if path.startswith("/dev/") else self.deck.connected()
        except Exception:
            return False

    def _write(self, fn, *args):
        """A device write; any failure means this deck is gone (DeckLost)."""
        try:
            with self.deck:
                fn(*args)
        except Exception as e:
            self.failed = True
            raise DeckLost(f"{self.name} (serial {self.serial}): {e}") from e

    # ------------------------------------------------------------------ layout

    def select_layout(self):
        """Pick this deck's pages: devices[serial].layout → layouts[<model>] → layouts["<cols>x<rows>"]
        → top-level pages → the biggest "CxR" layout that fits."""
        cfg = self.bot.cfg
        rows, cols = self.deck.key_layout()
        layouts = cfg.get("layouts") or {}
        dev = self.config()
        chosen, self.layout_name = None, "pages"
        want = dev.get("layout")
        if want is not None:
            if want in layouts:
                chosen, self.layout_name = layouts[want], want
            elif want == "pages" and cfg.get("pages"):
                chosen = {"pages": cfg["pages"]}
            else:
                log.warning("%s: no layout %r (devices.%s.layout); picking one by model", self.name, want,
                            self.serial)
        names = [self.model, f"{cols}x{rows}"]
        if chosen is None:
            for name in names:
                if name in layouts:
                    chosen, self.layout_name = layouts[name], name
                    break
        if chosen is None and cfg.get("pages"):
            chosen = {"pages": cfg["pages"]}
        if chosen is None and not layouts:
            # No config yet (a deck attached before load_config).
            self.layout_name, self.pages, self.page_dials, self.page, self.history = None, {}, {}, None, []
            return
        if chosen is None:
            # No exact match: the biggest "CxR" layout that fits on this deck (centered),
            # e.g. 8x4 on a + XL (9x4), 4x2 on a Studio (16x2).
            fits = []
            for name in layouts:
                grid = layout_grid(name)
                if grid and grid[0] <= cols and grid[1] <= rows:
                    fits.append((grid[0] * grid[1], name))
            self.layout_name = max(fits)[1] if fits else next(iter(layouts))
            chosen = layouts[self.layout_name]
            log.info("no layout named %s; using %r", " or ".join(repr(n) for n in names), self.layout_name)
        pages = chosen.get("pages", chosen) if isinstance(chosen, dict) else {}
        extra = self.deck.touch_key_count()
        lib = self.bot.library()
        declared = layout_grid(self.layout_name)
        offset = max(0, (cols - declared[0]) // 2) if declared else 0
        flatten = self.bot._flatten
        self.pages = {name: [resolve_key(k, lib) for k in flatten(name, keys, rows, cols, extra, offset)]
                      for name, keys in (pages or {}).items()}
        self.page_dials = {name: [resolve_key(d, lib) for d in
                                  ((keys.get("dials") if isinstance(keys, dict) else None) or [])]
                           for name, keys in (pages or {}).items()}
        start = dev.get("start_page") if dev.get("start_page") in self.pages else None
        start = start or (chosen.get("start_page") if isinstance(chosen, dict) and "pages" in chosen else None) \
            or cfg.get("start_page")
        if self.page not in self.pages:
            self.page, self.history = start if start in self.pages else next(iter(self.pages), None), []

    # ------------------------------------------------------------------ buttons

    def raw_buttons(self, page=None):
        """The page's button list exactly as written in config.yaml."""
        return self.pages.get(page or self.page) or []

    def buttons(self, page=None):
        out = {}
        token = _current_deck.set(self.serial)
        try:
            for i, btn in enumerate(self.raw_buttons(page)):
                btn = self.bot._expand(btn) if btn else None
                if btn:
                    out[i] = btn
        finally:
            _current_deck.reset(token)
        return out

    def appearance(self, key, page=None):
        page = page or self.page
        btn = self.buttons(page).get(key)
        if not btn:
            return {}
        # `on:` would be parsed by YAML as the boolean True, so accept that key too.
        active = btn.get("active") or btn.get(True) or btn.get("on")
        if self.states.get((page, key)) and active:
            btn = {**btn, **active}
        btn = {**btn, **self.displays.get((page, key), {})}
        if self.armed and self.armed[:2] == (page, key):
            btn = {**btn, "label": btn["confirm"] if isinstance(btn["confirm"], str) else "Press\nagain",
                   "bg": "#d07000"}
        return btn

    def dials(self, page=None):
        return self.page_dials.get(page or self.page) or []

    # ------------------------------------------------------------------ drawing & navigation

    def draw_key(self, key, pressed=False):
        with self.bot.lock:
            if self.detached or not self.deck.is_visual() or key >= self.deck.key_count():
                return
            btn = self.appearance(key)
            img = self.renderer.render(btn, pressed) if btn else None
            self._write(self.deck.set_key_image, key, img)
            page = self.page
        if not pressed:
            self.bot.hub.publish("key", {"deck": self.serial, "page": page, "key": key})

    def draw_page(self):
        """Every key, each on its own: one key that can't be drawn doesn't blank the rest.
        A device write failing (DeckLost) still stops it, as the deck is gone."""
        if self.deck.is_visual():
            for key in range(self.deck.key_count()):
                try:
                    self.draw_key(key)
                except DeckLost:
                    raise
                except Exception:
                    log.exception("drawing key %d of page %s on %s failed", key, self.page, self.name)
            self.draw_extras()

    def draw_extras(self):
        """The + models' touch strip (dial labels) and the Neo's info screen (page name)."""
        try:
            if self.deck.is_touch() and self.deck.dial_count():
                img = self.renderer.strip(self.dials(), self.deck.dial_count())
                with self.deck:
                    self.deck.set_touchscreen_image(img, 0, 0, *self.renderer.strip_size())
            if self.renderer.screen_size():
                img = self.renderer.screen(str(self.page).replace("_", " ").title())
                with self.deck:
                    self.deck.set_screen_image(img)
        except Exception as e:
            log.debug("drawing touch strip / screen: %s", e)

    def invalidate(self):
        """Forget cached states/displays and redraw (e.g. after buttons changed)."""
        with self.bot.lock:
            self.states.clear()
            self.displays.clear()
            self.next_poll.clear()
            self.backoff.clear()   # the change may be what fixes a failing state/display
            self.draw_page()

    def goto(self, page):
        with self.bot.lock:
            if page == "back":
                if not self.history:
                    return
                self.page = self.history.pop()
            elif page in self.pages:
                if page != self.page:
                    self.history.append(self.page)
                self.page = page
            else:
                raise ValueError(f"no such page {page!r}")
            self.next_poll.clear()
            self.bot.page_changed()
            self.bot.hub.publish("page", {"deck": self.serial, "page": self.page})
            self.draw_page()

    def set_brightness(self, level):
        self.brightness = max(0, min(100, int(level)))
        if self.deck.is_visual() and not self.detached:
            self._write(self.deck.set_brightness, self.brightness)

    def flash_error(self, key, page):
        with self.bot.lock:
            if page != self.page or self.detached or not self.deck.is_visual() or key >= self.deck.key_count():
                return
            img = self.renderer.render({"label": "error", "bg": "#a00000"})
            self._write(self.deck.set_key_image, key, img)
        time.sleep(0.8)
        if page == self.page:
            self.draw_key(key)

    # ------------------------------------------------------------------ input (called with this deck current)
    # on_key / on_dial / on_touch run on the StreamDeck library's reader thread, which dies for good
    # on any exception but TransportError (and the deck still looks attached), so they never raise.

    def on_key(self, key, down):
        try:
            self._on_key(key, down)
        except DeckLost as e:
            log.warning("%s", e)
        except Exception:
            log.exception("key %s on %s failed", key, self.name)

    def on_dial(self, dial, event, value):
        try:
            self._on_dial(dial, event, value)
        except DeckLost as e:
            log.warning("%s", e)
        except Exception:
            log.exception("dial %s on %s failed", dial, self.name)

    def on_touch(self, event, value):
        try:
            self._on_touch(event, value)
        except DeckLost as e:
            log.warning("%s", e)
        except Exception:
            log.exception("touch strip on %s failed", self.name)

    def _on_key(self, key, down):
        bot = self.bot
        with bot.lock:
            page = self.page
            btn = self.buttons().get(key)
        if not btn:
            return
        try:
            self.draw_key(key, pressed=down)
        except DeckLost:
            raise
        except Exception:
            # Only the press feedback failed to draw; the key still does its job.
            log.exception("drawing key %d of page %s on %s failed", key, page, self.name)
        action = btn.get("action")
        if isinstance(action, dict) and len(action) == 1:
            kind, arg = next(iter(action.items()))
            fn = bot.actions.get(kind)
            if fn and fn.__deck_action__[1]:
                try:
                    fn(arg, down)
                except Exception as e:
                    log.error("key %d on %s failed: %s", key, page, e)
                return
        if not down or action is None:
            return
        if btn.get("confirm"):
            armed = self.armed and self.armed[:2] == (page, key) and time.time() < self.armed[2]
            self.armed = None if armed else (page, key, time.time() + 3)
            self.draw_key(key)
            if not armed:
                return

        def work():
            try:
                bot.run_action(action)
            except Exception as e:
                log.error("key %d on %s failed: %s", key, page, e)
                notify(f"{btn.get('label', 'Key')}: {e}".replace("\n", " "))
                try:
                    self.flash_error(key, page)
                except DeckLost:
                    pass
            # Re-check this button's state right away so toggles feel instant.
            self.next_poll[(page, key)] = 0

        start_thread(work)

    def _on_dial(self, dial, event, value):
        with self.bot.lock:
            dials = self.dials()
        spec = dials[dial] if dial < len(dials) else None
        if not spec:
            return
        if event == DialEventType.TURN and value:
            action = spec.get("right" if value > 0 else "left")
            if action is not None:
                # A fast spin reports several ticks at once; run the action per tick (capped).
                self.bot.run_in_background([action] * min(abs(int(value)), 5), f"dial {dial + 1}")
        elif event == DialEventType.PUSH and value and spec.get("press") is not None:
            self.bot.run_in_background(spec["press"], f"dial {dial + 1}")

    def _on_touch(self, event, value):
        if event not in (TouchscreenEventType.SHORT, TouchscreenEventType.LONG):
            return
        width = self.renderer.strip_size()[0] or 1
        n = self.deck.dial_count() or 1
        dial = min(n - 1, int(value.get("x", 0)) * n // width)
        self.on_dial(dial, DialEventType.PUSH, True)

    # ------------------------------------------------------------------ polling (called with this deck current)

    def expire_armed(self):
        if self.armed and time.time() > self.armed[2]:
            page, key, _ = self.armed
            self.armed = None
            if page == self.page:
                self.draw_key(key)

    def _provider_key(self, field, spec):
        """What a backoff is kept for: (provider name, the spec as JSON). Keys sharing a spec share it."""
        if field == "display":
            name = spec.get("source") if isinstance(spec, dict) else spec
        else:
            name = next((n for n in spec if n in self.bot.state_providers), None) if isinstance(spec, dict) else None
        try:
            dump = json.dumps(spec, sort_keys=True, default=str)
        except (TypeError, ValueError):
            dump = repr(spec)
        return f"{field}:{name}", dump

    def _poll(self, field, key_suffix, read, store, default, deadline=None):
        """Read every due `field` spec on the page and redraw keys whose value changed.

        A provider that raises or takes longer than POLL_SLOW is skipped for 2, 4, 8 … BACKOFF_MAX s
        (its keys keep their last value) and is retried after that; one quick success resets it.
        No new call starts after `deadline`: the keys left stay due and go first next pass, so the
        run loop (overlay timers, confirm expiry, unplug checks, reloads) keeps ticking."""
        now = self.clock()
        with self.bot.lock:
            page = self.page
            todo = [(k, b[field]) for k, b in self.buttons().items()
                    if field in b and self.next_poll.get((page, k) + key_suffix, 0) <= now]
        for i, (key, spec) in enumerate(todo):
            if deadline is not None and self.clock() > deadline:
                log.debug("poll pass over budget; %s for %d more key(s) next pass", field, len(todo) - i)
                return
            todo_key = (page, key) + key_suffix
            if field == "display" and not isinstance(spec, dict):
                spec = {"source": spec}
            try:
                every = float(spec.get("every", 2)) if isinstance(spec, dict) else 2.0
            except (TypeError, ValueError):
                every = 2.0
            self.next_poll[todo_key] = now + every
            pkey = self._provider_key(field, spec)
            held = self.backoff.get(pkey)
            if held and self.clock() < held[1]:
                continue    # backing off: the key keeps showing its last value
            last = store.get((page, key), default)
            start = self.clock()
            try:
                val, ok = read(spec), True
            except Exception as e:
                log.debug("%s for key %d: %s", field, key, e)
                val, ok = last, False
            took = self.clock() - start
            if ok and took <= self.POLL_SLOW:
                if held:
                    del self.backoff[pkey]
                    log.info("%s for key %d on %s works again", pkey[0], key, self.name)
            else:
                wait = min(self.BACKOFF_MAX, held[0] * 2 if held else 2)
                self.backoff[pkey] = (wait, self.clock() + wait)
                if not held:
                    log.warning("%s for key %d on %s %s; retrying it less often (up to every %ds)", pkey[0],
                                key, self.name, "failed" if not ok else f"took {took:.1f}s", self.BACKOFF_MAX)
            if last != val:
                store[(page, key)] = val
                if page == self.page:
                    self.draw_key(key)

    def poll(self):
        deadline = self.clock() + self.POLL_BUDGET
        self._poll("display", ("d",), self.bot.read_display, self.displays, {}, deadline)
        self._poll("state", (), self.bot.check_state, self.states, False, deadline)


def start_thread(fn):
    """A daemon thread running fn in a copy of this context (so it acts on the same deck)."""
    threading.Thread(target=contextvars.copy_context().run, args=(fn,), daemon=True).start()


def _deck_id(deck):
    try:
        return deck.id()
    except Exception:
        return f"object:{id(deck)}"


def _serial_of(deck):
    try:
        serial = deck.get_serial_number()
    except Exception as e:
        log.debug("reading serial: %s", e)
        serial = None
    serial = str(serial).strip("\x00 ") if serial is not None else ""
    return serial or _deck_id(deck)


def _close(deck):
    try:
        deck.close()
    except Exception:
        pass
