"""Importing settings from another config.yaml: find candidates, preview a merge, apply it.

The merge never overwrites what you already have unless you choose to. Every difference
becomes an item with a dotted path:

    added      the incoming config has it and yours doesn't (or yours is empty) → copied in
    conflict   both have it and they differ → you pick "current" (default) or "incoming"
    same       identical → nothing to do

Items: `section.key` for module sections (twitch:, obs:, ...), `name` for top-level
settings (brightness, start_page, ...), `keys.<name>` for saved keys, and
`layouts.<layout>.pages.<page>` / `pages.<page>` for pages. A path part containing a dot
is quoted ("a.b"). An older config with flat top-level `pages:` is mapped into the layout of
the attached deck (or the layout with the same grid) when yours uses `layouts:`.

"Fresh" means your config is still the untouched example: then the import wins wholesale
(conflicts default to "incoming", and flat incoming pages replace the example's layouts).

A config.yaml someone shared is untrusted, so the panel's import runs it through sanitize()
before planning: every key action (also dial left/right/press), state and display that isn't on
the SAFE_* allow-lists arrives wrapped in `needs_review` (the wrapper the other importers use,
core/importers/_common.py), which the bot never runs, polls or shows until the user enables it
(review_items / enable_reviewed); and settings that point at files, servers or commands are
dropped and reported (see _dropped).
"""
import copy
import hashlib
import json
import math
import os
import re
import stat
from collections import deque

import yaml

from .configio import safe_load_bounded
from .importers._common import _live_arg_ok, _plain, _text_ok, clean_label
from .models import MODELS
from .paths import ROOT, config_path, state_dir

MAX_BYTES = 1024 * 1024
MAX_DIRS = 3000
MAX_DEPTH = 3
SNIPPET = 2000
SKIP_DIRS = {"node_modules", "vendor", ".cache", "snap", "__pycache__"}
STRUCTURE = ("pages", "layouts", "keys")
NOT_IMPORTED = ("editor",)   # the panel's own port: machine-specific, and moving it would cut the panel off
GRID_BY_KEYS = {32: (8, 4), 15: (5, 3), 6: (3, 2), 8: (4, 2)}   # last resort: pages give only a key count
KNOWN_GRIDS = {(c, r) for r, c, *_ in MODELS.values()} | set(GRID_BY_KEYS.values())
# Never page names: `page: back` means "previous page", and "pages" / "start_page" are a
# layout's own fields (a page called "pages" would turn the layout into its wrapped form).
RESERVED_PAGES = ("back", "pages", "start_page")
NOT_A_CONFIG = "That file isn’t a deck config"


# ------------------------------------------------------------------ helpers

def _empty(v):
    return v is None or v == "" or v == [] or v == {}


def _blank(k):
    return k is None or k == "-" or k == ""


def _qualifies(cfg):
    return isinstance(cfg, dict) and any(isinstance(cfg.get(k), dict) and cfg[k] for k in ("pages", "layouts"))


def _path(*parts):
    return ".".join(f'"{p}"' if ("." in str(p) or '"' in str(p)) else str(p) for p in parts)


def _snippet(v):
    text = yaml.safe_dump(v, sort_keys=False, allow_unicode=True, width=100, default_flow_style=False)
    if text.endswith("...\n"):          # a bare scalar dumps as "value\n...\n"
        text = text[:-4]
    text = text.rstrip("\n")
    return text if len(text) <= SNIPPET else text[:SNIPPET - 1] + "…"


def _short(v, limit=120):
    text = yaml.safe_dump(v, sort_keys=False, allow_unicode=True, default_flow_style=True, width=10 ** 6)
    text = text.replace("\n...\n", "").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _norm_page(p):
    """A page in a form where layouts that draw the same keys compare equal: blanks (`-`,
    null, "") are all null, and trailing blanks / empty trailing rows are dropped."""
    def keys(ks):
        if not isinstance(ks, list):
            return ks
        if any(isinstance(k, list) for k in ks):
            rows = [[None if _blank(k) else k for k in r] if isinstance(r, list) else ([] if _blank(r) else [r])
                    for r in ks]
            rows = [_strip(r) for r in rows]
            while rows and not rows[-1]:
                rows.pop()
            return ("rows", rows)
        return ("flat", _strip([None if _blank(k) else k for k in ks]))
    if isinstance(p, dict):
        q = {k: v for k, v in p.items() if not _empty(v)}
        if "keys" in q:
            q["keys"] = keys(q["keys"])
        if set(q) <= {"keys"}:
            return q.get("keys", ("flat", []))
        return q
    return keys(p)


def _strip(row):
    row = list(row)
    while row and row[-1] is None:
        row.pop()
    return row


def _page_keys(p):
    """How many keys a page is written for (rows count as rows × widest row)."""
    ks = p.get("keys") if isinstance(p, dict) else p
    if not isinstance(ks, list):
        return 0
    if any(isinstance(k, list) for k in ks):
        return len(ks) * max(len(r) if isinstance(r, list) else 1 for r in ks)
    return len(ks)


def _pages_grid(pages, hint=None):
    """(cols, rows) a set of pages is written for, or None.

    hint (a target layout's or the attached deck's grid) wins when the pages fit in it; then the
    shape of pages written as rows, if that is a known deck grid; then GRID_BY_KEYS by key count."""
    pages = [p for p in pages.values()] if isinstance(pages, dict) else []
    shapes = []
    for p in pages:
        ks = p.get("keys") if isinstance(p, dict) else p
        if isinstance(ks, list) and any(isinstance(k, list) for k in ks):
            shapes.append((max((len(r) if isinstance(r, list) else 1) for r in ks), len(ks)))
    if hint and all(c <= hint[0] and r <= hint[1] for c, r in shapes):
        return tuple(hint)
    if shapes:
        widest = (max(c for c, _ in shapes), max(r for _, r in shapes))
        if widest in KNOWN_GRIDS:
            return widest
    n = max((_page_keys(p) for p in pages), default=0)
    return GRID_BY_KEYS.get(n)


def _reserved(name):
    return isinstance(name, str) and name.casefold() in RESERVED_PAGES


def _rename_refs(v, old, new, depth=0):
    """{page: old} → {page: new} anywhere in v (in place)."""
    if depth > 40:
        return
    if isinstance(v, list):
        for x in v:
            _rename_refs(x, old, new, depth + 1)
    elif isinstance(v, dict):
        for k, x in v.items():
            if k == "page" and x == old:
                v[k] = new
            else:
                _rename_refs(x, old, new, depth + 1)


def _fix_reserved(incoming):
    """incoming with pages named back / pages / start_page (any case) renamed to "<name>_page"
    (made unique), and page actions / start_page that pointed at them following along (except
    `page: back`, which never means a page called back: that page was unreachable). Applies
    to any incoming config, flat pages and every layout. Returns a copy if anything changed."""
    if not isinstance(incoming, dict):
        return incoming
    out = None

    def fix(container, pages, keyname_ok):
        """Rename reserved keys of `pages` (a dict); container holds start_page for it."""
        nonlocal out
        bad = [n for n in pages if _reserved(n) and keyname_ok(n)]
        for old in bad:
            new, i = f"{old.casefold()}_page", 1
            while new in pages:
                i += 1
                new = f"{old.casefold()}_page_{i}"
            pages[new] = pages.pop(old)
            if old != "back":           # `page: back` always means "previous page": not a reference
                _rename_refs(out, old, new)
                if container.get("start_page") == old:
                    container["start_page"] = new
        return bool(bad)

    changed = False
    out = copy.deepcopy(incoming)
    if isinstance(out.get("pages"), dict):
        changed |= fix(out, out["pages"], lambda n: True)
    for lname, lay in (out.get("layouts") or {}).items() if isinstance(out.get("layouts"), dict) else ():
        if not isinstance(lay, dict):
            continue
        if isinstance(lay.get("pages"), dict):          # wrapped: {pages: {...}, start_page}
            changed |= fix(lay, lay["pages"], lambda n: True)
        else:
            # plain {page: rows}: "pages" / "start_page" holding rows are pages, not fields
            changed |= fix(lay, lay, lambda n: isinstance(lay.get(n), (list, dict)) or n.casefold() == "back")
    return out if changed else incoming


def _grid(name):
    """(cols, rows) of a layout named "CxR" or after a model, else None."""
    if name in MODELS:
        r, c = MODELS[name][:2]
        return c, r
    try:
        c, r = (int(v) for v in str(name).lower().split("x"))
        return c, r
    except ValueError:
        return None


def _layout_parts(lay):
    """(pages, settings, wrapped) of a layout written as {page: ...} or {pages: {...}, start_page: ...}."""
    if not isinstance(lay, dict):
        return {}, {}, False
    if "pages" in lay:
        pages = lay["pages"] if isinstance(lay["pages"], dict) else {}
        return pages, {k: v for k, v in lay.items() if k != "pages"}, True
    return lay, {}, False


# ------------------------------------------------------------------ finding configs

class _NotRegular(ValueError):
    pass


class _TooBig(ValueError):
    pass


def _read_regular(path):
    """(bytes, stat) of a regular file of at most MAX_BYTES. A named pipe or device would
    block the reading thread forever, so those are refused before opening; the file is then
    opened non-blocking and checked again, in case it was swapped in between."""
    st = os.stat(path)
    if not stat.S_ISREG(st.st_mode):
        raise _NotRegular(path)
    if st.st_size > MAX_BYTES:
        raise _TooBig(path)
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise _NotRegular(path)
        raw = f.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise _TooBig(path)
    return raw, st


def _inspect(path):
    """Candidate entry for path, or None if it isn't a deck config (or is too big / unreadable)."""
    try:
        raw, st = _read_regular(path)
        cfg = safe_load_bounded(raw)
    except (OSError, yaml.YAMLError, ValueError, RecursionError):
        return None
    if not _qualifies(cfg):
        return None
    return {"path": path, "mtime": st.st_mtime,
            "pages": len(cfg["pages"]) if isinstance(cfg.get("pages"), dict) else 0,
            "layouts": len(cfg["layouts"]) if isinstance(cfg.get("layouts"), dict) else 0,
            "sections": [k for k, v in cfg.items() if isinstance(v, dict) and k not in STRUCTURE]}


def _walk(home):
    """config.yaml files 1..MAX_DEPTH directories below home, breadth first, at most MAX_DIRS dirs."""
    found, visited = [], 0
    queue = deque([(home, 0)])
    while queue and visited < MAX_DIRS:
        d, depth = queue.popleft()
        visited += 1
        try:
            with os.scandir(d) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        for e in entries:
            try:
                if depth >= 1 and e.name == "config.yaml" and e.is_file():
                    found.append(e.path)
                elif (depth < MAX_DEPTH and e.is_dir(follow_symlinks=False)
                      and not e.name.startswith(".") and e.name not in SKIP_DIRS):
                    queue.append((e.path, depth + 1))
            except OSError:
                continue
    return found


def candidates(home=None, current_path=None):
    """Deck configs on this machine, newest first; the one in use has current: true."""
    home = home or os.path.expanduser("~")
    current_path = current_path or config_path()
    current = os.path.realpath(current_path)
    paths = [current_path, os.path.join(ROOT, "config.yaml"), os.path.join(state_dir(), "config.yaml")]
    paths += _walk(home)
    out, seen = [], set()
    for p in paths:
        real = os.path.realpath(p)
        if real in seen:
            continue
        seen.add(real)
        entry = _inspect(p)
        if entry:
            entry["current"] = real == current
            out.append(entry)
    out.sort(key=lambda c: (-c["mtime"], c["path"]))
    return out


def load_source(path=None, text=None):
    """The config to import, from a file or pasted text. ValueError if it isn't a deck config."""
    if (path is None) == (text is None):
        raise ValueError("give either a file or the config text")
    if path is not None:
        if not isinstance(path, str) or not path:
            raise ValueError("no file given")
        path = os.path.expanduser(path)
        try:
            raw, _ = _read_regular(path)
        except _TooBig:
            raise ValueError("That file is too big for a deck config (over 1 MB)") from None
        except _NotRegular:
            raise ValueError(NOT_A_CONFIG) from None
        except FileNotFoundError:
            raise ValueError("That file doesn’t exist") from None
        except IsADirectoryError:
            raise ValueError(NOT_A_CONFIG) from None
        except OSError:
            raise ValueError("That file can’t be read") from None
    else:
        if not isinstance(text, str):
            raise ValueError(NOT_A_CONFIG)
        raw = text.encode("utf-8", "surrogatepass")
    if len(raw) > MAX_BYTES:
        raise ValueError("That file is too big for a deck config (over 1 MB)")
    try:
        cfg = safe_load_bounded(raw)
    except (yaml.YAMLError, RecursionError):
        raise ValueError(NOT_A_CONFIG) from None   # the parser's message quotes the file
    except ValueError as e:
        raise ValueError(f"{NOT_A_CONFIG}: {e}") from None
    if not _qualifies(cfg):
        raise ValueError(NOT_A_CONFIG)
    return cfg


def is_fresh(cfg, example_path):
    """True if cfg is the example config, apart from `editor:` and empty strings."""
    try:
        with open(example_path, "rb") as f:
            example = safe_load_bounded(f.read()) or {}
    except (OSError, yaml.YAMLError, ValueError):
        return False

    def clean(v):
        if isinstance(v, dict):
            return {k: clean(x) for k, x in v.items() if x != ""}
        if isinstance(v, list):
            return [clean(x) for x in v]
        return v

    def top(c):
        return clean({k: v for k, v in (c or {}).items() if k != "editor"}) if isinstance(c, dict) else c
    return top(cfg) == top(example)


# ------------------------------------------------------------------ untrusted content

REVIEW_SOURCE = "config.yaml"
ACTION_FIELDS = ("action", "left", "right", "press")      # a key's action; a dial's turn / push
REVIEW_FIELDS = ACTION_FIELDS + ("state", "display")
REVIEW_ROOTS = ("pages", "layouts", "keys")
MAX_REVIEW_PATH = 8
_TIMERS = ("countdown", "brb_timer")
_DURATION = re.compile(r"\d{1,4}(:[0-5]?\d)?")


def _number(v, lo, hi):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and lo <= v <= hi


def _choice(*values):
    return lambda a: isinstance(a, str) and a in values


def _name(a):
    return _text_ok(a, 256)


def _live(kind):
    return lambda a: _live_arg_ok(kind, a)


def _seconds(a):
    return _number(a, 0, 86400) or (isinstance(a, str) and bool(_DURATION.fullmatch(a)))


def _fields(arg, checks):
    """arg is a dict whose every key has a check in `checks` that passes."""
    return isinstance(arg, dict) and all(isinstance(k, str) and k in checks and checks[k](v) for k, v in arg.items())


_TIMER_ARGS = {"seconds": _seconds, "name": _choice(*_TIMERS), "stop": lambda a: isinstance(a, bool)}

# Actions, states and displays an imported config.yaml may bring in switched on: each only acts
# on the deck, OBS or the app's own overlay / BRB screen, never runs a command, opens a URL or
# touches a file, and only with an argument checked here. Everything else (run, keys, type, hold,
# http, obs, obs_screenshot, ha, steam, media, volume, mic, notify, twitch*, editor, every plugin
# action, ...) waits for review. name -> argument check.
SAFE_ACTIONS = {
    "page": _live("page"),
    "brightness": _live("brightness"),
    "delay": _live("delay"),
    "obs_scene": _live("obs_scene"),
    "obs_mute": _live("obs_mute"),
    "obs_studio": _live("obs_studio"),
    # countdown names an overlay file: only the two built-in timers
    "countdown": lambda a: a == "stop" or _seconds(a) or _fields(a, _TIMER_ARGS),
    "countdown_toggle": lambda a: _fields(a, {"seconds": _seconds, "name": _choice(*_TIMERS), "scene": _name}),
    "brb_poll": lambda a: isinstance(a, int) and not isinstance(a, bool) and 0 <= a < 100,
    "brb_guess": _choice("start"),
    "brb_game": _choice("next", "end", "clear"),
    "brb_hype": _choice("reset", "fill"),
    "brb_video": _choice("next"),
    "radio": lambda a: (_choice("toggle", "next", "next_station", "prev_station", "on", "off")(a)
                        or (_fields(a, {"station": _name}) and "station" in a)),
    "alerts": _choice("toggle", "pause", "resume", "skip", "test"),
}
SAFE_STATES = {
    "deck_asleep": lambda a: a is True,
    "obs": _choice("stream", "record", "replay", "virtualcam", "studio"),
    "obs_scene": _name,
    "obs_muted": _name,
    "brb": _choice("hype_full", "game_open"),
    "radio": _name,
    "alerts": _choice("paused"),
    "twitch": _choice("linked"),
    "chat": _choice("connected"),
    "twitch_bot": _choice("on"),
    "bot_timers": _choice("running"),
}
_EVERY = {"every": lambda a: _number(a, 0.1, 3600)}
# display source -> the options it may carry besides `every`
SAFE_DISPLAYS = {
    "clock": {"show": _choice("time", "date")},
    "obs_uptime": {},
    "obs_health": {},
    "countdown": {"timer": _choice(*_TIMERS)},
    "radio": {"track": lambda a: isinstance(a, bool)},
    "brb_hype": {},
    "brb_game": {},
    "last_raider": {},
}


def _safe_action(action):
    if isinstance(action, list):
        return bool(action) and all(isinstance(a, dict) and _safe_action(a) for a in action)
    return isinstance(action, dict) and bool(action) and _fields(action, SAFE_ACTIONS)


def _safe_state(spec):
    return (isinstance(spec, dict) and any(k in SAFE_STATES for k in spec)
            and _fields(spec, {**SAFE_STATES, **_EVERY}))


def _safe_display(spec):
    if isinstance(spec, str):
        return spec in SAFE_DISPLAYS
    if not isinstance(spec, dict) or spec.get("source") not in SAFE_DISPLAYS:
        return False
    return _fields({k: v for k, v in spec.items() if k != "source"}, {**SAFE_DISPLAYS[spec["source"]], **_EVERY})


def _wrap(value):
    return {"needs_review": {"imported_action": value, "source": REVIEW_SOURCE, "original_id": ""}}


def _waiting(spec):
    """The needs_review dict of a wrapped action / state / display, else None."""
    nr = spec.get("needs_review") if isinstance(spec, dict) else None
    return nr if isinstance(nr, dict) and isinstance(nr.get("imported_action"), (dict, list)) else None


_DROP = object()


def _safe_field(field, value):
    """The value an imported key field may have (unchanged, or wrapped for review), or _DROP."""
    try:
        value = _plain(value)
    except ValueError:
        return _DROP                    # too deep / not plain data: nothing to show for review
    nr = _waiting(value)
    if nr is not None:                  # already waiting (e.g. exported after an Elgato import)
        return _wrap(nr["imported_action"])
    if field in ACTION_FIELDS:
        if isinstance(value, str):
            value = {"run": value}      # a plain string action is a shell command (DeckBot.run_action)
        if not isinstance(value, (dict, list)):
            return _DROP
        return value if _safe_action(value) else _wrap(value)
    if field == "state":
        if not isinstance(value, dict):
            return _DROP
        return value if _safe_state(value) else _wrap(value)
    if _safe_display(value):
        return value
    if isinstance(value, str):
        value = {"source": value}
    return _wrap(value) if isinstance(value, dict) else _DROP


# A key's `icon` goes through render.find_icon: "emoji:..." is drawn from a font, a bare name is
# looked up in the state dir's icons/ and the system icon themes, and an absolute or ~ path is
# opened as any image on this machine. Only the last (and anything climbing out with "..") is
# unsafe in a shared config, so those are dropped; the rest can't name an arbitrary file.
IMAGE_FIELDS = ("icon", "image")     # the key fields render.py opens as image files
ICON_OVERRIDES = ("active", "on", "off", True, False)      # a state's look (YAML `on:` loads as True)


def _icon_ok(value, field="icon"):
    """True if an imported icon / image value can't name a file outside the app's icons/ folder."""
    if value is None:
        return True
    if not isinstance(value, str):
        return False                    # fail closed: only text is a name
    if field == "icon" and value.startswith("emoji:"):
        return True                     # drawn from a font, never opened (raw value: no leading blanks)
    for s in (value, value.strip()):
        if (s.startswith(("~", "/", "\\")) or os.path.isabs(s) or ".." in s or "\0" in s
                or re.match(r"^[A-Za-z]:", s)):
            return False
    icons = os.path.realpath(os.path.join(ROOT, "icons"))
    target = os.path.realpath(os.path.join(icons, value.strip()))
    return target == icons or target.startswith(icons + os.sep)     # e.g. a symlink out of it


def _screen_icon(d, where, dropped):
    """d (a dict: a key or one of its state overrides) without an icon / image that could name any file."""
    for f in IMAGE_FIELDS:
        if dropped is not None and f in d and not _icon_ok(d[f], f):
            d = {k: v for k, v in d.items() if k != f}
            dropped.append(f"{where}.{f}")
    return d


def _safe_key(key, where="", dropped=None):
    if not isinstance(key, dict):
        return key                      # blank, or the name of a saved key
    out = _screen_icon(dict(key), where, dropped)
    for o in ICON_OVERRIDES:
        if isinstance(out.get(o), dict):
            out[o] = _screen_icon(out[o], f"{where}.{o}", dropped)
    for f in REVIEW_FIELDS:
        if f in out and not _empty(out[f]):
            v = _safe_field(f, out[f])
            if v is _DROP:
                del out[f]
            else:
                out[f] = v
    return out


def _key_name(k, i):
    label = k.get("label") if isinstance(k, dict) else None
    return label.replace("\n", " ") if isinstance(label, str) and label.strip() else str(i)


def _safe_keys(keys, page="", dropped=None):
    if not isinstance(keys, list):
        return keys
    out = []
    for i, r in enumerate(keys):
        if isinstance(r, list):
            out.append([_safe_key(k, f"{page}/{_key_name(k, f'{i}.{j}')}", dropped) for j, k in enumerate(r)])
        else:
            out.append(_safe_key(r, f"{page}/{_key_name(r, i)}", dropped))
    return out


def _safe_page(page, name="", dropped=None):
    if isinstance(page, list):
        return _safe_keys(page, name, dropped)
    if isinstance(page, dict):
        out = dict(page)
        for f in ("keys", "dials", "touch_keys"):
            if f in out:
                out[f] = _safe_keys(out[f], name, dropped)
        return out
    return page


def _safe_layout(lay, name="", dropped=None):
    if not isinstance(lay, dict):
        return lay
    if "pages" in lay:
        out = dict(lay)
        if isinstance(lay["pages"], dict):
            out["pages"] = {n: _safe_page(p, f"{name}/{n}", dropped) for n, p in lay["pages"].items()}
        return out
    return {n: _safe_page(p, f"{name}/{n}", dropped) if isinstance(p, (list, dict)) else p
            for n, p in lay.items()}


# Settings that point at files, folders, servers or commands. A shared config could aim them at
# this machine's secrets (token_file: ~/.config/ultimate-streaming/twitch.json next to an evil
# home_assistant.url) or at a program to run (steam.command), so they are never imported.
DROP_SUFFIXES = ("_file", "_url", "_dir")
DROP_NAMES = {"url", "host", "port", "password", "dir", "command", "cmd", "font", "emoji_font",
              "allow_unsafe_actions", "ffmpeg", "transport"}       # the last: a Twitch Bot reward running any action
MAX_SETTING_DEPTH = 40


def _drops(name):
    return isinstance(name, str) and (name in DROP_NAMES or name.endswith(DROP_SUFFIXES))


def _dropped(value, prefix, out, depth=0):
    """value (a copy) without the settings _drops() names, anywhere inside; their paths go to out."""
    if depth > MAX_SETTING_DEPTH:
        return value
    if isinstance(value, dict):
        res = {}
        for k, v in value.items():
            path = f"{prefix}.{_path(k)}" if prefix else _path(k)
            if _drops(k):
                out.append(path)
            else:
                res[k] = _dropped(v, path, out, depth + 1)
        return res
    if isinstance(value, list):
        return [_dropped(v, f"{prefix}[{i}]", out, depth + 1) for i, v in enumerate(value)]
    return value


def sanitize(incoming):
    """(config, dropped_settings): a copy of an untrusted incoming config that is safe to plan and
    apply. Key actions / states / displays not on the allow-lists are wrapped in needs_review;
    settings that point at files, servers or commands (anywhere outside pages, layouts and
    keys) are left out and listed by dotted path."""
    if not isinstance(incoming, dict):
        return incoming, []
    out, dropped = {}, []
    for top, value in incoming.items():
        if top == "pages":
            out[top] = {n: _safe_page(p, n, dropped) for n, p in value.items()} if isinstance(value, dict) else value
        elif top == "layouts":
            out[top] = {n: _safe_layout(lay, n, dropped) for n, lay in value.items()} if isinstance(value, dict) else value
        elif top == "keys":
            out[top] = {n: _safe_key(k, f"keys/{n}", dropped) for n, k in value.items()} if isinstance(value, dict) else value
        elif top in NOT_IMPORTED:
            out[top] = value            # never planned: nothing to drop or report
        elif _drops(top):
            dropped.append(_path(top))
        else:
            kept = _dropped(copy.deepcopy(value), _path(top), dropped)
            if not (kept == {} and value != {}):     # a section left empty by the drops adds nothing
                out[top] = kept
    return out, dropped


def review_hash(action):
    """What enable_reviewed() must be sent back: binds the click to exactly what was shown
    (the same hash as core/webapi/setup_api.expect_hash)."""
    return hashlib.sha256(json.dumps(action, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _kinds(field, spec):
    if field == "display":
        return [str(spec.get("source"))] if isinstance(spec, dict) else []
    steps = spec if isinstance(spec, list) else [spec]
    return [str(k) for s in steps if isinstance(s, dict) for k in s if k != "every"]


def review_items(cfg):
    """Every key action / dial action / state / display in cfg's pages, layouts and saved keys
    that waits for review: {path, field, where, label, kinds, argument, expect}. path (a list
    of dict keys and list indexes from the top of cfg) and field address it for
    enable_reviewed(); argument is exactly what it would run, as text (for textContent)."""
    out = []

    def key(node, path, where):
        if not isinstance(node, dict):
            return
        for f in REVIEW_FIELDS:
            nr = _waiting(node.get(f))
            if nr is None:
                continue
            spec = nr["imported_action"]
            out.append({"path": path, "field": f, "where": where, "label": clean_label(node.get("label")),
                        "kinds": _kinds(f, spec), "argument": json.dumps(spec, ensure_ascii=False, indent=2),
                        "expect": review_hash(spec)})

    def keys(rows, path, what, where):
        if not isinstance(rows, list):
            return
        nested = any(isinstance(r, list) for r in rows)
        for i, r in enumerate(rows):
            if nested and isinstance(r, list):
                for j, k in enumerate(r):
                    key(k, [*path, i, j], f"{where}, row {i + 1}, {what} {j + 1}")
            else:
                key(r, [*path, i], f"{where}, {what} {i + 1}")

    def page(p, path, where):
        if isinstance(p, dict):
            keys(p.get("keys"), [*path, "keys"], "key", where)
            keys(p.get("dials"), [*path, "dials"], "dial", where)
            keys(p.get("touch_keys"), [*path, "touch_keys"], "touch key", where)
        else:
            keys(p, path, "key", where)

    if isinstance(cfg.get("pages"), dict):
        for n, p in cfg["pages"].items():
            page(p, ["pages", n], f"Page {n}")
    if isinstance(cfg.get("layouts"), dict):
        for ln, lay in cfg["layouts"].items():
            pages, _, wrapped = _layout_parts(lay)
            for n, p in pages.items():
                if isinstance(p, (list, dict)):
                    page(p, ["layouts", ln, *(["pages"] if wrapped else []), n], f"Layout {ln}, page {n}")
    if isinstance(cfg.get("keys"), dict):
        for n, k in cfg["keys"].items():
            key(k, ["keys", n], f"Saved key {n}")
    return out


class ReviewChanged(ValueError):
    """What's at that place isn't what the user reviewed (or nothing waits there any more)."""


def enable_reviewed(cfg, path, field, expect):
    """Replace the needs_review wrapper at cfg[path...][field] with the action / state / display
    it holds, in place, if that is exactly what the user was shown (review_hash == expect).
    LookupError: no such place; ReviewChanged: nothing waits there, or something else does."""
    if (not isinstance(path, list) or not 2 <= len(path) <= MAX_REVIEW_PATH or path[0] not in REVIEW_ROOTS
            or any(isinstance(p, bool) or not isinstance(p, (str, int)) for p in path)):
        raise LookupError("no such key")
    if field not in REVIEW_FIELDS:
        raise LookupError("no such field")
    node = cfg
    for p in path:
        if isinstance(node, dict) and isinstance(p, str) and p in node:
            node = node[p]
        elif isinstance(node, list) and isinstance(p, int) and 0 <= p < len(node):
            node = node[p]
        else:
            raise LookupError("no such key")
    if not isinstance(node, dict):
        raise LookupError("no such key")
    nr = _waiting(node.get(field))
    if nr is None:
        raise ReviewChanged("nothing there is waiting for review")
    if review_hash(nr["imported_action"]) != expect:
        raise ReviewChanged("that isn't what you reviewed")
    node[field] = copy.deepcopy(nr["imported_action"])
    return node[field]


# ------------------------------------------------------------------ planning

def _items(current, incoming, target_layout=None, fresh=False, target_grid=None):
    """Every difference, in incoming's order, with how to apply it. Returns (items, same, target).

    Item: {path, kind: "added"|"conflict", value (incoming), current, ops}; an op is
    ("set", container_path, key, value), ("wrap", layout) or ("fresh_pages",)."""
    items, same = [], [0]
    cur_layouts = current.get("layouts") if isinstance(current.get("layouts"), dict) and current["layouts"] else {}
    cur_pages = current.get("pages") if isinstance(current.get("pages"), dict) and current["pages"] else None

    seen = set()

    def compare(path, container, key, cur_container, value, summary=None, extra_ops=()):
        if path in seen:
            return                  # already compared from another source (first one wins)
        seen.add(path)
        present = isinstance(cur_container, dict) and key in cur_container
        cur = cur_container.get(key) if present else None
        if present and (cur == value or (summary == "page" and _norm_page(cur) == _norm_page(value))):
            same[0] += 1
            return
        if present and not _empty(cur):
            if _empty(value):
                same[0] += 1        # nothing to bring over; never blank out a value you have
                return
            items.append({"path": path, "kind": "conflict", "current": cur, "value": value,
                          "ops": [*extra_ops, ("set", container, key, value)]})
            return
        if _empty(value) and (present or not isinstance(value, dict)):
            return                  # an empty setting adds nothing
        items.append({"path": path, "kind": "added", "value": value, "summary": _summary(summary, value),
                      "ops": [*extra_ops, ("set", container, key, value)]})

    def merge_pages(prefix, container, cur_container, pages, extra_ops=()):
        for name, page in pages.items():
            compare(_path(*prefix, name), container, name, cur_container, page, "page", extra_ops)

    hint = tuple(target_grid) if target_grid else _grid(target_layout) if target_layout else None

    def layout_target(want):
        """Where flat incoming pages written for grid `want` go when the current config uses layouts."""
        if isinstance(cur_layouts.get(target_layout), dict):
            return target_layout
        if want:
            for name in cur_layouts:
                if _grid(name) == want and isinstance(cur_layouts[name], dict):
                    return name
        return None

    # Your plain {page: ...} layouts that take an incoming layout setting (start_page) become
    # {pages: {...}, start_page: ...}; decided up front so every page in them lands in the same place.
    inc_layouts = incoming.get("layouts") if isinstance(incoming.get("layouts"), dict) else {}
    to_wrap = {name for name, lay in inc_layouts.items()
               if isinstance(cur_layouts.get(name), dict) and not _layout_parts(cur_layouts[name])[2]
               and any(not _empty(v) for v in _layout_parts(lay)[1].values())}

    def dest(lname):
        """(container, extra ops) for pages of your layout lname."""
        if lname in to_wrap:
            return ("layouts", lname, "pages"), (("wrap", lname),)
        wrapped = _layout_parts(cur_layouts[lname])[2]
        return (("layouts", lname, "pages") if wrapped else ("layouts", lname)), ()

    fresh_pages = (fresh and cur_layouts and isinstance(incoming.get("pages"), dict) and incoming["pages"]
                   and not (isinstance(incoming.get("layouts"), dict) and incoming["layouts"]))
    used_target = None
    order = list(incoming.items())
    if "layouts" in incoming and "pages" in incoming:
        # A deck with a matching layout shows it, not the flat pages: compare the layouts first so
        # their pages win where both land on the same page of yours.
        order = [kv for kv in order if kv[0] not in ("layouts", "pages")]
        order += [("layouts", incoming["layouts"]), ("pages", incoming["pages"])]
    for top, value in order:
        if top in NOT_IMPORTED:
            continue
        if top == "keys":
            if isinstance(value, dict) and isinstance(current.get("keys"), (dict, type(None))):
                merge = current.get("keys") or {}
                for name, key in value.items():
                    compare(_path("keys", name), ("keys",), name, merge, key, "key")
            else:
                compare("keys", (), "keys", current, value)
        elif top == "pages":
            if not isinstance(value, dict) or not value:
                continue
            if fresh_pages:
                items.append({"path": "layouts", "kind": "added", "value": value,
                              "summary": "Replaces the example layouts with your pages: " + ", ".join(map(str, value)),
                              "ops": [("fresh_pages",)]})
                continue
            if cur_layouts:
                want = _pages_grid(value, hint)
                target = layout_target(want)
                if target is not None:
                    used_target = target
                    container, wrap = dest(target)
                    merge_pages(("layouts", target, "pages"), container, _layout_parts(cur_layouts[target])[0],
                                value, wrap)
                    continue
                if want and cur_pages is None:
                    name = "%dx%d" % want   # no layout for this grid yet: add one
                    used_target = name
                    compare(_path("layouts", name), ("layouts",), name, cur_layouts, dict(value), "layout")
                    continue
            merge_pages(("pages",), ("pages",), cur_pages or {}, value)
        elif top == "layouts":
            if not isinstance(value, dict):
                continue
            # Your config has flat pages only: the incoming layout for the same grid merges into
            # them page by page (added whole it would hide them on that deck).
            flat_grid = None
            if not cur_layouts and cur_pages:
                flat_grid = _pages_grid(cur_pages, hint)
            for lname, lay in value.items():
                if flat_grid and _grid(lname) == flat_grid:
                    pages, _, _ = _layout_parts(lay)
                    merge_pages(("pages",), ("pages",), cur_pages, pages)
                    continue
                if lname not in cur_layouts or not isinstance(cur_layouts[lname], dict):
                    compare(_path("layouts", lname), ("layouts",), lname, cur_layouts, lay, "layout")
                    continue
                pages, settings, _ = _layout_parts(lay)
                cpages, csettings, _ = _layout_parts(cur_layouts[lname])
                container, wrap = dest(lname)
                for sname, sval in settings.items():
                    if _empty(sval):
                        continue
                    compare(_path("layouts", lname, sname), ("layouts", lname), sname, csettings, sval,
                            None, wrap)
                merge_pages(("layouts", lname, "pages"), container, cpages, pages, wrap)
        elif isinstance(value, dict):
            cur = current.get(top)
            if isinstance(cur, dict) and cur:
                for k, v in value.items():
                    compare(_path(top, k), (top,), k, cur, v)
            else:
                compare(_path(top), (), top, current, value, "section")
        else:
            compare(_path(top), (), top, current, value)
    return items, same[0], used_target


def _summary(kind, value):
    if kind == "page":
        ks = value.get("keys") if isinstance(value, dict) else value
        flat = [k for r in (ks or []) for k in (r if isinstance(r, list) else [r])] if isinstance(ks, list) else []
        n = sum(1 for k in flat if not _blank(k))
        return f"New page ({n} key{'s' if n != 1 else ''})"
    if kind == "layout":
        pages, _, _ = _layout_parts(value)
        return f"New layout ({len(pages)} page{'s' if len(pages) != 1 else ''})"
    if kind == "section" and isinstance(value, dict):
        return f"New section ({len(value)} setting{'s' if len(value) != 1 else ''})"
    if kind == "key" and isinstance(value, dict) and value.get("label"):
        return f"Saved key “{str(value['label']).replace(chr(10), ' ')}”"
    return _short(value)


def plan(current, incoming, target_layout=None, fresh=False, target_grid=None):
    """The merge preview. target_layout: the attached deck's layout name; target_grid: its
    (cols, rows), used to place flat incoming pages when no layout name fits. Incoming pages
    named back / pages / start_page are renamed (see _fix_reserved)."""
    current = current if isinstance(current, dict) else {}
    incoming = _fix_reserved(incoming)
    items, same, target = _items(current, incoming, target_layout, fresh, target_grid)
    return {"added": [{"path": i["path"], "summary": i["summary"]} for i in items if i["kind"] == "added"],
            "conflicts": [{"path": i["path"], "current": _snippet(i["current"]), "incoming": _snippet(i["value"])}
                          for i in items if i["kind"] == "conflict"],
            "same": same, "target_layout": target, "fresh": bool(fresh)}


def defaults(plan):
    choice = "incoming" if plan.get("fresh") else "current"
    return {c["path"]: choice for c in plan.get("conflicts", [])}


# ------------------------------------------------------------------ applying

def _container(cfg, path):
    node = cfg
    for part in path:
        nxt = node.get(part)
        if nxt is None:
            nxt = node[part] = {}
        elif not isinstance(nxt, dict):
            raise ValueError("can’t merge into " + _path(*path))
        node = nxt
    return node


def apply(current, incoming, choices, target_layout=None, fresh=False, target_grid=None):
    """Merge incoming into a copy of current. Returns (merged, {added, replaced, kept})."""
    incoming = _fix_reserved(incoming)
    choices = choices or {}
    if not isinstance(choices, dict):
        raise ValueError("choices must be an object")
    for p, c in choices.items():
        if c not in ("current", "incoming"):
            raise ValueError(f"choice for {p} must be 'current' or 'incoming'")
    merged = copy.deepcopy(current if isinstance(current, dict) else {})
    items, _, _ = _items(merged, incoming, target_layout, fresh, target_grid)
    counts = {"added": 0, "replaced": 0, "kept": 0}
    default = "incoming" if fresh else "current"
    todo = []
    for item in items:
        if item["kind"] == "conflict":
            if choices.get(item["path"], default) == "current":
                counts["kept"] += 1
                continue
            counts["replaced"] += 1
        else:
            counts["added"] += 1
        todo.append(item)
    # Plan first, then write: every decision is made against the untouched config.
    wrapped = set()
    for item in todo:
        for op in item["ops"]:
            if op[0] == "set":
                _, path, key, value = op
                _container(merged, path)[key] = copy.deepcopy(value)
            elif op[0] == "wrap":
                lname = op[1]
                if lname not in wrapped:
                    wrapped.add(lname)
                    merged["layouts"][lname] = {"pages": merged["layouts"][lname]}
            elif op[0] == "fresh_pages":
                merged.pop("layouts", None)
                merged["pages"] = copy.deepcopy(incoming["pages"])
                if not _empty(incoming.get("start_page")):
                    merged["start_page"] = incoming["start_page"]
    return merged, counts
