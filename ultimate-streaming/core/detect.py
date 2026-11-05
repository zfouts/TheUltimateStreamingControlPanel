"""Read-only discovery of the user's existing OBS setup and other Stream Deck apps.

Used by onboarding to offer "back up / import / adopt". Nothing here writes, opens a network
connection, or returns secrets: the stream key is reduced to a bool, and results hold names,
counts and opaque source ids only (never file contents, URLs or keys). The browser sends a
source_id back; resolve() maps it to the real path on the server.
"""
import configparser
import hashlib
import json
import os
import shutil
import stat
import subprocess
import threading
import time
import unicodedata

from . import paths
from .safefs import NotRegular, TooBig, inside, read_regular

SMALL = 1024 * 1024                 # profile files, INI files, app configs
SCENE_MAX = 20 * 1024 * 1024        # scene collection JSON
APP_JSON_MAX = 5 * 1024 * 1024      # deck app configs (pages may embed more than OBS files)
ELGATO_MAX = 50 * 1024 * 1024
ELGATO_FILES = 50
MAX_ENTRIES = 5000                  # skip trees with more entries than this
BUDGET_SECONDS = 2.0
MAX_NAME = 100
MAX_SCENES = 200

# How to recognize a running process. "exe": exact basenames of /proc/<pid>/exe. "argv": exact
# basenames of argv[0] (or argv[1] behind an interpreter); an entry with "/" must equal argv[0]
# or argv[1] exactly. "flatpak": the app id listed by `flatpak ps`. Never substring-match.
RUNNING_MATCH = {
    "obs": {"exe": ["obs"], "argv": ["obs", "/app/bin/obs"], "flatpak": "com.obsproject.Studio"},
    "streamcontroller": {"exe": ["streamcontroller"], "argv": ["streamcontroller"],
                         "flatpak": "com.core447.StreamController"},
    "streamdeck_ui": {"exe": ["streamdeck", "streamdeck_ui"],
                      "argv": ["streamdeck", "streamdeck_ui", "streamdeck-ui"], "flatpak": None},
    "boatswain": {"exe": ["boatswain"], "argv": ["boatswain", "/app/bin/boatswain"],
                  "flatpak": "com.feaneron.Boatswain"},
}
_INTERPRETERS = ("python", "bash", "sh", "dash", "zsh")

_SOURCES: dict[str, tuple[str, str]] = {}
_LOCK = threading.Lock()


# ---- helpers ---------------------------------------------------------------------------------

def _sid(kind, real):
    return hashlib.sha256((kind + "\0" + real).encode("utf-8", "surrogateescape")).hexdigest()[:24]


def _register(table, kind, path):
    real = os.path.realpath(path)
    sid = _sid(kind, real)
    table[sid] = (kind, real)
    return sid


def resolve(source_id):
    """(kind, realpath) for a source_id from the last scan(). KeyError if unknown."""
    with _LOCK:
        return _SOURCES[source_id]


def _clean(value, limit=MAX_NAME):
    """A short single-line display string without control or bidi characters."""
    if not isinstance(value, str):
        return ""
    out = "".join(c for c in value if unicodedata.category(c) not in ("Cc", "Cf", "Cs", "Co"))
    return out.strip()[:limit]


class _Budget:
    def __init__(self, seconds):
        self.end = time.monotonic() + seconds
        self.hit = False

    def out(self):
        if time.monotonic() > self.end:
            self.hit = True
        return self.hit


def _read(path, limit, *, follow=False):
    return read_regular(path, limit, follow=follow)[0]


def _json(path, limit, *, follow=False):
    try:
        return json.loads(_read(path, limit, follow=follow))
    except TooBig:
        raise ValueError("too large") from None
    except NotRegular:
        raise ValueError("not a regular file") from None
    except OSError:
        raise
    except (ValueError, RecursionError):
        raise ValueError("not valid JSON") from None


def _ini(path):
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    cp.read_string(_read(path, SMALL).decode("utf-8-sig", "replace"))
    return cp


def _ini_get(cp, section, key):
    if cp is not None and cp.has_option(section, key):
        return cp.get(section, key).strip()
    return ""


def _err(name, exc):
    """A warning that names the file and the kind of failure, never its contents."""
    if isinstance(exc, (ValueError, configparser.Error)):
        why = str(exc) if isinstance(exc, ValueError) else "not valid INI"
    elif isinstance(exc, TooBig):
        why = "too large"
    elif isinstance(exc, NotRegular):
        why = "not a regular file"
    elif isinstance(exc, FileNotFoundError):
        why = "missing"
    elif isinstance(exc, PermissionError):
        why = "permission denied"
    else:
        why = "unreadable"
    return f"{_clean(name, 80)}: {why}"


def _list_dir(path, warnings, what):
    """Sorted os.DirEntry list, or [] (with a warning) when unreadable or too big."""
    try:
        with os.scandir(path) as it:
            entries = []
            for e in it:
                entries.append(e)
                if len(entries) > MAX_ENTRIES:
                    warnings.append(f"{what}: more than {MAX_ENTRIES} entries, skipped")
                    return []
    except OSError as e:
        warnings.append(_err(what, e))
        return []
    return sorted(entries, key=lambda e: e.name)


# ---- OBS -------------------------------------------------------------------------------------

def _obs_roots():
    home = os.path.expanduser("~")
    cands = [("native", "Native OBS", os.path.join(paths._xdg("XDG_CONFIG_HOME", "~/.config"), "obs-studio")),
             ("flatpak", "Flatpak OBS", os.path.join(home, ".var/app/com.obsproject.Studio/config/obs-studio"))]
    env = os.environ.get("OBS_CONFIG_DIR")
    if env:
        cands.append(("custom", "OBS_CONFIG_DIR", os.path.expanduser(env)))
    seen, out = set(), []
    for kind, label, p in cands:
        if os.path.isdir(p):
            real = os.path.realpath(p)
            if real not in seen:
                seen.add(real)
                out.append((kind, label, real))
    return out


def _location(cps, key, sub, root):
    """Folder for profiles/scenes. [Locations] names a parent folder that OBS appends
    obs-studio/<sub> to (real files: Profiles=$HOME/.config -> $HOME/.config/obs-studio/basic/profiles).
    A folder that already holds <sub> directly is accepted too; otherwise the default under root."""
    for cp in cps:
        val = _ini_get(cp, "Locations", key)
        if val:
            base = os.path.join(root, os.path.expanduser(val))   # an absolute value wins in join
            for cand in (os.path.join(base, "obs-studio", sub), os.path.join(base, sub)):
                if os.path.isdir(cand):
                    return cand
    return os.path.join(root, sub)


def _profile(pdir, dirname, fallback_name):
    warnings = []
    cp = None
    try:
        cp = _ini(os.path.join(pdir, "basic.ini"))
    except FileNotFoundError:
        pass
    except (OSError, configparser.Error) as e:
        warnings.append(_err("basic.ini", e))
    name = _clean(_ini_get(cp, "General", "Name")) or _clean(fallback_name)
    mode = _ini_get(cp, "Output", "Mode").lower()
    pairs = [("AdvOut", "Encoder"), ("SimpleOutput", "StreamEncoder")]
    if mode != "advanced":
        pairs.reverse()
    encoder = next((v for v in (_ini_get(cp, s, k) for s, k in pairs) if v), None)
    has_key, service = False, None
    try:
        data = _json(os.path.join(pdir, "service.json"), SMALL)
        settings = data.get("settings") if isinstance(data, dict) else None
        if not isinstance(settings, dict):
            raise ValueError("unexpected layout")
        has_key = bool(settings.get("key"))
        svc = _clean(settings.get("service"), 64)
        service = svc or None
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        warnings.append(_err("service.json", e))
    return {"name": name, "dir": dirname, "has_stream_key": has_key, "service": service,
            "encoder": _clean(encoder, 64) or None, "warnings": warnings}


def _collection(path, fname):
    warnings, scenes, sources = [], [], 0
    name = _clean(os.path.splitext(fname)[0])
    try:
        data = _json(path, SCENE_MAX)
        if not isinstance(data, dict):
            raise ValueError("unexpected layout")
        name = _clean(data.get("name")) or name
        srcs = [s for s in data.get("sources", []) if isinstance(s, dict)] \
            if isinstance(data.get("sources"), list) else []
        sources = len(srcs)
        by_name = {s.get("name"): s for s in srcs if s.get("id") == "scene"}
        order = [o.get("name") for o in data.get("scene_order", []) if isinstance(o, dict)] \
            if isinstance(data.get("scene_order"), list) else []
        names = [n for n in order if n in by_name] + [n for n in by_name if n not in order]
        scenes = [c for c in (_clean(n) for n in names[:MAX_SCENES]) if c]
    except (OSError, ValueError) as e:
        warnings.append(_err(fname, e))
    return {"name": name, "file": fname, "scenes": scenes, "sources": sources, "warnings": warnings}


def _scan_obs_root(kind, label, root, table, budget):
    sid = _register(table, "obs", root)
    res = {"root": {"source_id": sid, "kind": kind, "label": label}, "profiles": [], "collections": [],
           "current_profile": None, "current_collection": None, "warnings": []}
    w = res["warnings"]
    cps = []
    for fname in ("user.ini", "global.ini"):    # user.ini wins (OBS 31+), global.ini is the older home
        try:
            cps.append(_ini(os.path.join(root, fname)))
        except FileNotFoundError:
            cps.append(None)
        except (OSError, configparser.Error) as e:
            cps.append(None)
            w.append(_err(fname, e))
    user, glob = cps
    def current(name_key, id_key):      # per file: user.ini wins, then global.ini
        for cp in (user, glob):
            n, i = _ini_get(cp, "Basic", name_key), _ini_get(cp, "Basic", id_key)
            if n or i:
                return _clean(n), os.path.basename(i.rstrip("/"))
        return "", ""
    cur_p, cur_pdir = current("Profile", "ProfileDir")
    cur_c, cur_cfile = current("SceneCollection", "SceneCollectionFile")
    res["current_profile"], res["current_collection"] = cur_p or None, cur_c or None

    pdir_root = _location(cps, "Profiles", "basic/profiles", root)
    sdir_root = _location(cps, "SceneCollections", "basic/scenes", root)

    pdir_real = os.path.realpath(pdir_root)
    for e in _list_dir(pdir_root, w, "profiles"):
        if budget.out():
            w.append("time limit reached, list may be incomplete")
            break
        try:
            if not e.is_dir(follow_symlinks=False) and not (e.is_symlink() and e.is_dir()):
                continue
        except OSError:
            continue
        if e.is_symlink() and not inside(e.path, pdir_real):
            w.append(f"{_clean(e.name, 60)}: symlink outside the folder, skipped")
            continue
        res["profiles"].append(_profile(e.path, e.name, e.name))
    if not cur_p and cur_pdir:
        for p in res["profiles"]:
            if p["dir"] == cur_pdir:
                res["current_profile"] = p["name"]

    sdir_real = os.path.realpath(sdir_root)
    for e in _list_dir(sdir_root, w, "scenes"):
        if budget.out():
            w.append("time limit reached, list may be incomplete")
            break
        if not e.name.endswith(".json"):
            continue
        try:
            if e.is_symlink() and not inside(e.path, sdir_real):
                w.append(f"{_clean(e.name, 60)}: symlink outside the folder, skipped")
                continue
            if not e.is_file(follow_symlinks=True):
                w.append(f"{_clean(e.name, 60)}: not a regular file, skipped")
                continue
        except OSError:
            continue
        res["collections"].append(_collection(e.path, e.name))
    if not cur_c and cur_cfile:
        base = cur_cfile
        base = base if base.endswith(".json") else base + ".json"
        for c in res["collections"]:
            if c["file"] == base:
                res["current_collection"] = c["name"]
    return res


# ---- other apps ------------------------------------------------------------------------------

def _app(table, kind, path, label, **kw):
    entry = {"source_id": _register(table, kind, path), "kind": kind, "label": label,
             "decks": 0, "pages": 0, "keys": 0, "running": False, "warnings": []}
    entry.update(kw)
    return entry


def _scan_streamcontroller(table):
    data = os.path.join(os.path.expanduser("~"), ".var/app/com.core447.StreamController/data")
    if not os.path.isdir(data):
        return []
    app = _app(table, "streamcontroller", data, "StreamController")
    w = app["warnings"]
    real = os.path.realpath(data)
    pages_dir = os.path.join(real, "pages")
    for e in _list_dir(pages_dir, w, "pages") if os.path.isdir(pages_dir) else []:
        if not e.name.endswith(".json"):
            continue
        try:
            page = _json(e.path, SMALL, follow=inside(e.path, real))
            keys = page.get("keys") if isinstance(page, dict) else None
            if not isinstance(keys, dict):
                raise ValueError("unexpected layout")
        except (OSError, ValueError) as ex:
            w.append(_err(e.name, ex))
            continue
        app["pages"] += 1
        app["keys"] += len(keys)
    serials = set()
    try:
        sett = _json(os.path.join(real, "settings", "pages.json"), SMALL)
        dp = sett.get("default-pages") if isinstance(sett, dict) else None
        if isinstance(dp, dict):
            serials.update(dp)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as ex:
        w.append(_err("settings/pages.json", ex))
    decks_dir = os.path.join(real, "settings", "decks")
    if os.path.isdir(decks_dir):
        serials.update(e.name[:-5] for e in _list_dir(decks_dir, w, "settings/decks")
                       if e.name.endswith(".json"))
    app["decks"] = len(serials)
    return [app]


_BTN_FIELDS = ("text", "icon", "keys", "write", "command", "switch_page", "brightness_change")


def _sdui_button_used(btn):
    if not isinstance(btn, dict):
        return False
    states = btn.get("states")
    cands = [s for s in states.values() if isinstance(s, dict)] if isinstance(states, dict) else [btn]
    return any(s.get(f) for s in cands for f in _BTN_FIELDS)


def _scan_streamdeck_ui(table):
    path = os.environ.get("STREAMDECK_UI_CONFIG") or os.path.join(os.path.expanduser("~"), ".streamdeck_ui.json")
    path = os.path.expanduser(path)
    if not os.path.lexists(path):
        return []
    app = _app(table, "streamdeck_ui", path, "streamdeck-ui")
    w = app["warnings"]
    try:
        data = _json(path, APP_JSON_MAX, follow=True)    # dotfile managers often symlink this
        state = data.get("state") if isinstance(data, dict) else None
        if not isinstance(state, dict):
            raise ValueError("unexpected layout")
        version = data.get("streamdeck_ui_version")
        if version not in (1, 2):
            w.append("unsupported streamdeck_ui_version, counts may be off")
        pages = 0
        for deck in state.values():
            buttons = deck.get("buttons") if isinstance(deck, dict) else None
            if not isinstance(buttons, dict):
                continue
            app["decks"] += 1
            for page in buttons.values():
                if isinstance(page, dict):
                    pages += 1
                    app["keys"] += sum(1 for b in page.values() if _sdui_button_used(b))
        app["pages"] = pages
    except (OSError, ValueError) as e:
        w.append(_err(os.path.basename(path), e))
    return [app]


def _boat_page(page, depth, acc):
    """Count pages and action items in a Boatswain page, following folders (nested pages)."""
    acc[0] += 1
    if depth > 10 or acc[0] > 2000:
        return
    regions = page.get("regions") if isinstance(page, dict) else None
    if isinstance(regions, list):
        items = [i for r in regions if isinstance(r, dict) and isinstance(r.get("items"), list)
                 for i in r["items"]]
    elif isinstance(page, list):               # v0 legacy: a bare array of items
        items = page
    else:
        return
    for item in items:
        if isinstance(item, dict) and item.get("type") == "action":
            acc[1] += 1
            sub = (item.get("settings") or {}).get("page") if isinstance(item.get("settings"), dict) else None
            if isinstance(sub, (dict, list)):
                _boat_page(sub, depth + 1, acc)


def _scan_boatswain(table, budget):
    home = os.path.expanduser("~")
    dirs = [("Boatswain", paths._xdg("XDG_DATA_HOME", "~/.local/share"), False),
            ("Boatswain (Flatpak)", os.path.join(home, ".var/app/com.feaneron.Boatswain/data"), True)]
    out, seen = [], set()
    for label, d, flat in dirs:
        if not os.path.isdir(d) or os.path.realpath(d) in seen:
            continue
        seen.add(os.path.realpath(d))
        warnings = []
        decks = pages = keys = 0
        for e in _list_dir(d, warnings, "data folder"):
            if budget.out():
                warnings.append("time limit reached, list may be incomplete")
                break
            if not e.name.endswith(".json") or not e.is_file(follow_symlinks=False):
                continue
            try:
                data = _json(e.path, APP_JSON_MAX)
            except (OSError, ValueError) as ex:
                if flat:
                    warnings.append(_err(e.name, ex))
                continue                          # native data dir holds unrelated JSON
            if not (isinstance(data, dict) and isinstance(data.get("profiles"), list)
                    and "active-profile" in data):
                continue
            decks += 1
            acc = [0, 0]
            for prof in data["profiles"]:
                if isinstance(prof, dict):
                    _boat_page(prof.get("page"), 0, acc)
            pages += acc[0]
            keys += acc[1]
        if decks or (flat and warnings):
            out.append(_app(table, "boatswain", d, label, decks=decks, pages=pages, keys=keys,
                            warnings=warnings))
    return out


def _scan_elgato(table, warnings, budget):
    home = os.path.expanduser("~")
    found = []

    def walk(d, depth, seen):
        if len(found) >= ELGATO_FILES or budget.out():
            return
        try:
            with os.scandir(d) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            return
        seen[0] += len(entries)
        subdirs = []
        for e in entries:                       # files at this level first, however big the folder
            if len(found) >= ELGATO_FILES:
                return
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):
                if depth < 2 and not e.name.startswith("."):
                    subdirs.append(e.path)
            elif (stat.S_ISREG(st.st_mode) and e.name.lower().endswith(".streamdeckprofile")
                    and st.st_size <= ELGATO_MAX):
                found.append(e.path)
        for sub in subdirs:
            if seen[0] > MAX_ENTRIES:           # each top folder (Downloads, ...) has its own cap
                warnings.append("a folder with many files was only partly searched for Elgato exports")
                return
            walk(sub, depth + 1, seen)

    for sub in ("Downloads", "Documents", "Desktop"):
        walk(os.path.join(home, sub), 0, [0])
    return [_app(table, "elgato", p, _clean(os.path.basename(p)), decks=1,
                 warnings=["contents read when you preview"]) for p in found]


# ---- running processes -----------------------------------------------------------------------

def _argv_match(argv, patterns):
    if not argv:
        return False
    first = os.path.basename(argv[0])
    cands = [argv[0]]
    names = [first]
    if len(argv) > 1 and any(first.startswith(i) for i in _INTERPRETERS):
        cands.append(argv[1])
        names.append(os.path.basename(argv[1]))
    for p in patterns:
        if "/" in p:
            if p in cands:
                return True
        elif p in names:
            return True
    return False


def _running(proc_root, use_flatpak):
    """Set of RUNNING_MATCH kinds with a live process owned by us."""
    found = set()
    uid = os.getuid()
    try:
        names = os.listdir(proc_root)
    except OSError:
        names = []
    for pid in names:
        if not pid.isdigit():
            continue
        base = os.path.join(proc_root, pid)
        try:
            if os.stat(base).st_uid != uid:
                continue
        except OSError:
            continue
        try:
            exe_path = os.readlink(os.path.join(base, "exe"))
        except OSError:
            exe_path = ""
        exe = os.path.basename(exe_path)
        try:
            with open(os.path.join(base, "cmdline"), "rb") as f:
                argv = [a.decode("utf-8", "replace") for a in f.read(4096).split(b"\0") if a]
        except OSError:
            argv = []
        for kind, m in RUNNING_MATCH.items():
            if (exe and exe in m["exe"]) or _argv_match(argv, m["argv"]):
                found.add(kind)
                if kind == "obs":   # which install: Flatpak apps live under /app inside their sandbox
                    flat = exe_path.startswith("/app/") or (argv and argv[0].startswith("/app/"))
                    found.add("obs:flatpak" if flat else "obs:native")
    if use_flatpak and shutil.which("flatpak"):
        try:
            r = subprocess.run(["flatpak", "ps", "--columns=application"], capture_output=True,
                               text=True, timeout=3, stdin=subprocess.DEVNULL)
            ids = {ln.strip() for ln in r.stdout.splitlines()}
            found.update(k for k, m in RUNNING_MATCH.items() if m["flatpak"] and m["flatpak"] in ids)
            if RUNNING_MATCH["obs"]["flatpak"] in ids:
                found.add("obs:flatpak")
        except (OSError, subprocess.SubprocessError):
            pass
    return found


# ---- which folder to back up / restore ---------------------------------------------------------

def _settings_mtime(root):
    best = 0.0
    for rel in ("user.ini", "global.ini", "basic/scenes", "basic/profiles"):
        try:
            best = max(best, os.stat(os.path.join(root, rel)).st_mtime)
        except OSError:
            pass
    return best


def _mark_active(roots, running, table):
    """Set root["active"] on exactly one OBS root: the install that is running, else the one whose
    settings changed most recently (a leftover folder from an old install loses)."""
    if not roots:
        return
    want = "flatpak" if "obs:flatpak" in running else "native" if "obs:native" in running else None
    pick = None
    if want:
        pick = next((r for r in roots if r["kind"] == want), None)
        if want == "native" and pick is None:
            pick = next((r for r in roots if r["kind"] == "custom"), None)
    if pick is None:
        pick = max(roots, key=lambda r: _settings_mtime(table[r["source_id"]][1]))
    for r in roots:
        r["active"] = r is pick


def obs_roots(result):
    """[(source_id, realpath)] of the OBS settings folders in a scan() result, the active one first.
    Back up all of them (cheap, and a leftover folder may be the one someone still needs)."""
    roots = sorted(result.get("obs", {}).get("roots", []), key=lambda r: not r.get("active"))
    out = []
    for r in roots:
        try:
            out.append((r["source_id"], resolve(r["source_id"])[1]))
        except KeyError:
            continue
    return out


def active_obs_root(result):
    """realpath of the OBS folder actually in use, or None."""
    roots = obs_roots(result)
    return roots[0][1] if roots else None


def default_root(kind):
    """Where `kind` keeps its settings, whether or not that folder exists (restoring after the files
    were deleted must still work). obs: Flatpak if Flatpak OBS is installed, else native."""
    home = os.path.expanduser("~")
    data_home = paths._xdg("XDG_DATA_HOME", "~/.local/share")
    if kind == "obs":
        env = os.environ.get("OBS_CONFIG_DIR")
        if env:
            return os.path.realpath(os.path.expanduser(env))
        if os.path.isdir(os.path.join(home, ".var/app/com.obsproject.Studio")):
            return os.path.realpath(os.path.join(home, ".var/app/com.obsproject.Studio/config/obs-studio"))
        return os.path.realpath(os.path.join(paths._xdg("XDG_CONFIG_HOME", "~/.config"), "obs-studio"))
    if kind == "streamcontroller":
        return os.path.realpath(os.path.join(home, ".var/app/com.core447.StreamController/data"))
    if kind == "streamdeck_ui":     # the folder holding the single config file
        f = os.environ.get("STREAMDECK_UI_CONFIG") or os.path.join(home, ".streamdeck_ui.json")
        return os.path.realpath(os.path.dirname(os.path.expanduser(f)))
    if kind == "boatswain":
        if os.path.isdir(os.path.join(home, ".var/app/com.feaneron.Boatswain")):
            return os.path.realpath(os.path.join(home, ".var/app/com.feaneron.Boatswain/data"))
        return os.path.realpath(data_home)
    raise KeyError(kind)


# ---- entry point -----------------------------------------------------------------------------

def scan(*, proc_root="/proc", use_flatpak=True):
    """Discover OBS and deck apps. Never raises for a bad source; see the module docstring.

    proc_root and use_flatpak exist so tests can fake the process list."""
    budget = _Budget(BUDGET_SECONDS)
    table: dict[str, tuple[str, str]] = {}
    warnings: list[str] = []
    obs = {"roots": [], "profiles": [], "collections": [], "current_profile": None,
           "current_collection": None, "running": False, "warnings": []}
    apps = []

    try:
        for kind, label, root in _obs_roots():
            try:
                r = _scan_obs_root(kind, label, root, table, budget)
            except Exception as e:      # one root must not sink the scan
                warnings.append(f"OBS {kind}: {type(e).__name__}")
                continue
            sid = r["root"]["source_id"]
            obs["roots"].append(r["root"])
            obs["profiles"] += [{**p, "source_id": sid} for p in r["profiles"]]
            obs["collections"] += [{**c, "source_id": sid} for c in r["collections"]]
            obs["warnings"] += r["warnings"]
            obs["current_profile"] = obs["current_profile"] or r["current_profile"]
            obs["current_collection"] = obs["current_collection"] or r["current_collection"]
    except Exception as e:
        warnings.append(f"OBS: {type(e).__name__}")

    for name, fn in (("StreamController", _scan_streamcontroller), ("streamdeck-ui", _scan_streamdeck_ui),
                     ("Boatswain", lambda t: _scan_boatswain(t, budget)),
                     ("Elgato exports", lambda t: _scan_elgato(t, warnings, budget))):
        try:
            apps += fn(table)
        except Exception as e:
            warnings.append(f"{name}: {type(e).__name__}")

    try:
        running = _running(proc_root, use_flatpak)
    except Exception:
        running = set()
        warnings.append("process scan failed")
    obs["running"] = "obs" in running
    _mark_active(obs["roots"], running, table)
    for a in apps:
        a["running"] = a["kind"] in running
    if budget.hit:
        warnings.append("scan stopped early: time limit reached")

    with _LOCK:
        _SOURCES.clear()
        _SOURCES.update(table)
    return {"scanned_at": time.time(), "obs": obs, "apps": apps, "warnings": warnings}
