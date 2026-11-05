"""Boatswain device files (`<serial>.json`, one per deck; the profile format of GNOME Boatswain).

The active profile (else the first) is read. Its root page is the main page and every folder
(a "switch page" action holding a nested page) becomes a page of its own, with a back key added
when the folder doesn't have one. Dials and the touchscreen aren't imported. The deck model isn't
stored, so it is guessed from the number of keys. Guesses are reported in the setup's warnings.
"""
import os
import re
import shlex
import urllib.parse

from ._common import (MAX_FOLDER_DEPTH, MAX_JSON_BYTES, MAX_PAGES, ImageBudget, ImportError_,
                      clean_label, combo, needs_action, parse_lock, read_local, safe_action, safe_json,
                      url_action)
from .streamdeck_ui import BACK_KEY, guess_model, local_image

SOURCE = "boatswain"

# GdkModifierType bits.
MOD_BITS = ((1 << 0, "shift"), (1 << 2, "ctrl"), (1 << 3, "alt"), (1 << 26, "super"), (1 << 28, "super"))
# X keysym → our key name (beyond printable ASCII, which is handled by KEY_CHARS).
KEYSYMS = {0xff08: "BackSpace", 0xff09: "Tab", 0xff0d: "Return", 0xff1b: "Escape", 0xffff: "Delete",
           0xff50: "Home", 0xff51: "Left", 0xff52: "Up", 0xff53: "Right", 0xff54: "Down", 0xff55: "Prior",
           0xff56: "Next", 0xff57: "End", 0xff63: "Insert", 0xff61: "Print", 0xff13: "Pause",
           0xff8d: "KP_Enter", 0xffab: "KP_Add", 0xffad: "KP_Subtract", 0xffaa: "KP_Multiply",
           0xffaf: "KP_Divide", 0xffae: "KP_Decimal",
           0x1008ff14: "XF86AudioPlay", 0x1008ff15: "XF86AudioStop", 0x1008ff16: "XF86AudioPrev",
           0x1008ff17: "XF86AudioNext", 0x1008ff11: "XF86AudioLowerVolume",
           0x1008ff12: "XF86AudioMute", 0x1008ff13: "XF86AudioRaiseVolume"}
KEYSYMS.update({0xffbe + i: f"F{i + 1}" for i in range(24)})
KEYSYMS.update({0xffb0 + i: f"KP_{i}" for i in range(10)})
KEY_CHARS = {" ": "space", "!": "exclam", '"': "quotedbl", "#": "numbersign", "$": "dollar",
             "%": "percent", "&": "ampersand", "'": "apostrophe", "(": "parenleft", ")": "parenright",
             "*": "asterisk", "+": "plus", ",": "comma", "-": "minus", ".": "period", "/": "slash",
             ":": "colon", ";": "semicolon", "<": "less", "=": "equal", ">": "greater",
             "?": "question", "@": "at", "[": "bracketleft", "\\": "backslash", "]": "bracketright",
             "^": "asciicircum", "_": "underscore", "`": "grave", "{": "braceleft", "|": "bar",
             "}": "braceright", "~": "asciitilde"}
OBS_TOGGLES = {"obs-stream-action": "ToggleStream", "obs-record-action": "ToggleRecord",
               "obs-virtualcam-action": "ToggleVirtualCam"}
_RGB = re.compile(r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*(?:,\s*([0-9.]+)\s*)?\)")
_DESKTOP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,200}")


def _text(v, limit=256):
    return v.strip() if isinstance(v, str) and 0 < len(v.strip()) <= limit else None


def _rgb(v):
    """"#rrggbb" of a GdkRGBA string, None when transparent or unreadable."""
    m = _RGB.fullmatch(v.strip()) if isinstance(v, str) else None
    if not m:
        return None
    r, g, b = (int(m.group(i)) for i in (1, 2, 3))
    try:
        alpha = float(m.group(4)) if m.group(4) else 1.0
    except ValueError:
        return None
    if max(r, g, b) > 255 or alpha == 0:
        return None
    return "#%02x%02x%02x" % (r, g, b)


def _file_path(uri):
    """Local path of a file:// URI, or None."""
    if not isinstance(uri, str) or len(uri) > 4096:
        return None
    try:
        parts = urllib.parse.urlsplit(uri)
    except ValueError:
        return None
    if parts.scheme != "file" or parts.netloc not in ("", "localhost"):
        return None
    return urllib.parse.unquote(parts.path) or None


class _Reader:
    def __init__(self, preview=None):
        self.budget = ImageBudget(preview=preview)
        self.warnings = []
        self.assumptions = set()
        self.pages = {}              # name → grid
        self.cols = self.rows = 0

    def note(self, msg):
        if msg not in self.warnings:
            self.warnings.append(msg)

    def items_of(self, page, *, root=False):
        """The main-button-grid items of a page document (the v0 format is a bare list)."""
        if isinstance(page, list):
            return page
        regions = page.get("regions") if isinstance(page, dict) else None
        if not isinstance(regions, list):
            raise ImportError_("A page in the profile isn't in a format we understand.")
        if isinstance(page.get("version"), int) and page["version"] > 1:
            self.note("This profile is from a newer Boatswain than we know; it was read as version 1.")
        items = None
        for reg in regions:
            if not isinstance(reg, dict):
                continue
            if reg.get("id") == "main-button-grid" and isinstance(reg.get("items"), list):
                items = reg["items"]
            elif reg.get("id") in ("dial-grid", "touchscreen") and reg.get("items"):
                if any(isinstance(i, dict) and i for i in reg["items"]):
                    self.note("Dials and the touchscreen aren't imported yet.")
        if items is None:
            if root:
                raise ImportError_("The profile has no key grid.")
            return []
        return items

    def key_for(self, item, folders):
        """(key | None, action id) of one item; folders collects (key, nested page) pairs."""
        if not isinstance(item, dict):
            return None
        key = {}
        icon = item.get("custom-icon") if isinstance(item.get("custom-icon"), dict) else {}
        label = clean_label(icon.get("text"))
        if label:
            key["label"] = label
        bg = _rgb(icon.get("background-color"))
        if bg and bg != "#000000":
            key["bg"] = bg
        path = _file_path(icon.get("file"))
        if path:
            img = local_image(path, self.budget, SOURCE)
            if img:
                key["icon"] = img
        if item.get("type") != "action":
            return key or None
        factory, aid = item.get("factory"), item.get("action")
        factory = factory if isinstance(factory, str) else ""
        aid = aid if isinstance(aid, str) else ""
        s = item.get("settings") if isinstance(item.get("settings"), dict) else {}
        got = self.convert_action(factory, aid, s, folders, key)
        oid = f"{factory}/{aid}"[:200]
        if isinstance(got, str):
            key.update(needs_action(oid, got))
        elif got is not None:
            if "__url" in got:
                key.update(url_action(got["__url"], source=SOURCE, original_id=oid))
            else:
                key["action"] = safe_action(got, source=SOURCE, original_id=oid)
        return key

    def convert_action(self, factory, aid, s, folders, key):
        match (factory, aid):
            case ("launcher", "launch-action"):
                app = s.get("app")
                if not isinstance(app, str) or not _DESKTOP_ID.fullmatch(app):
                    return "An app that couldn't be read"
                self.assumptions.add("Launcher keys run the app through gtk-launch with its desktop id.")
                return {"run": "gtk-launch " + shlex.quote(app[:-8] if app.endswith(".desktop") else app)}
            case ("launcher", "launcher-open-url-action"):
                return {"__url": s.get("url")}
            case ("obs-studio", "obs-switch-scene-action"):
                scene = _text(s.get("scene-name"))
                return {"obs_scene": scene} if scene else "Switches to an OBS scene whose name couldn't be read"
            case ("obs-studio", "obs-toggle-mute-action"):
                src = _text(s.get("source-name"))
                self.assumptions.add("OBS mute keys: the source name is read from the \"source-name\" setting.")
                return {"obs_mute": src} if src else "Mutes an OBS source whose name couldn't be read"
            case ("obs-studio", a) if a in OBS_TOGGLES:
                return {"obs": OBS_TOGGLES[a]}
            case ("desktop", "desktop-keyboard-shortcut"):
                return self.shortcut(s)
            case ("soundboard", _):
                return "Plays a sound; we have no soundboard action yet"
            case ("default", "default-page-up-action"):
                return {"page": "back"}
            case ("default", "default-switch-page-action"):
                sub = s.get("page")
                if not isinstance(sub, (dict, list)):
                    return "Opens a folder that couldn't be read"
                folders.append((key, sub))
                return None
        return "This action isn't supported yet"

    def shortcut(self, s):
        mods_bits, sym = s.get("modifiers"), s.get("keysym")
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (mods_bits, sym)):
            return "A keyboard shortcut that couldn't be read"
        mods = {m for bit, m in MOD_BITS if mods_bits & bit}
        if sym in KEYSYMS:
            name = KEYSYMS[sym]
        elif 0x20 <= sym <= 0x7e:
            ch = chr(sym)
            name = KEY_CHARS.get(ch, ch.lower())
        else:
            name = None
        if name is None:
            return "A keyboard shortcut with a key we don't know"
        self.assumptions.add("Keyboard shortcuts: modifiers are GDK bits (Shift, Ctrl, Alt, Super) and keysym an X keysym.")
        return {"keys": combo(mods, name)}

    def add_page(self, name, items, depth):
        """Convert a page; returns its unique name."""
        base, i = name, 1
        while (base if i == 1 else f"{base} {i}") in self.pages:
            i += 1
        pname = base if i == 1 else f"{base} {i}"
        if len(self.pages) >= MAX_PAGES:
            raise ImportError_(f"The profile has more than {MAX_PAGES} pages.")
        grid = [[None] * self.cols for _ in range(self.rows)]
        self.pages[pname] = grid
        if len(items) > self.cols * self.rows:
            self.note("Some keys were outside the deck's grid and were skipped.")
        has_back = False
        for idx, item in enumerate(items[:self.cols * self.rows]):
            folders = []
            key = self.key_for(item, folders)
            r, c = divmod(idx, self.cols)
            if folders:
                fkey, sub = folders[0]
                if depth + 1 > MAX_FOLDER_DEPTH:
                    self.note(f"Folders nested more than {MAX_FOLDER_DEPTH} deep weren't imported.")
                    key.update(needs_action("default/default-switch-page-action", "Opens a folder nested too deeply"))
                else:
                    fkey.setdefault("label", "Folder")
                    child = self.add_page(fkey["label"], self.items_of(sub), depth + 1)
                    fkey["action"] = {"page": child}
            has_back |= bool(key) and key.get("action") == {"page": "back"}
            grid[r][c] = key or None
        if depth and not has_back:
            free = next(((r, c) for r in range(self.rows) for c in range(self.cols) if grid[r][c] is None), None)
            if free:
                grid[free[0]][free[1]] = dict(BACK_KEY)
                self.assumptions.add("Folders without a back key got one in their first free slot.")
            else:
                self.note("A folder is full, so it has no back key; use another page key to leave it.")
        return pname


def read(path, *, preview=None):
    """A setup dict (see _common) from a Boatswain `<serial>.json`. ImportError_ on failure.
    preview: a preview token; icons then go to the preview folder (see _common.ImageBudget)."""
    with parse_lock():
        doc = safe_json(read_local(path, MAX_JSON_BYTES))
        profiles = doc.get("profiles") if isinstance(doc, dict) else None
        profiles = [p for p in profiles if isinstance(p, dict)] if isinstance(profiles, list) else []
        if not profiles:
            raise ImportError_("That file has no Boatswain profiles.")
        active = doc.get("active-profile")
        profile = next((p for p in profiles if isinstance(active, str) and p.get("id") == active), profiles[0])
        r = _Reader(preview)
        root_items = r.items_of(profile.get("page"), root=True)
        model, (r.cols, r.rows) = guess_model(max(len(root_items), 1))
        r.assumptions.add(f"The deck model isn't stored; it was read as a {model} from the key count." if model
                          else "No known deck has that many keys; the grid is a guess.")
        if len(profiles) > 1:
            r.note("Only the active profile was imported.")
        title = clean_label(profile.get("name")) or "Main"
        start = r.add_page(title, root_items, 0)
        deck = {"model": model, "grid": (r.cols, r.rows), "pages": r.pages, "start_page": start}
        stem = os.path.basename(os.fspath(path))[:-5]
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", stem):
            deck["serial"] = stem
        warnings = r.warnings + r.budget.warnings
        if r.assumptions:
            warnings.append("Assumed (not verified against Boatswain): " + " ".join(sorted(r.assumptions)))
        return {"source": SOURCE, "decks": [deck], "warnings": warnings}
