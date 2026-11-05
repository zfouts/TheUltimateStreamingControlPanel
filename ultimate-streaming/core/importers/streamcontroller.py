"""StreamController data folder (`pages/*.json`, `settings/pages.json`).

A page's identity in StreamController is its absolute file path; here pages are named by their
file name without `.json`. Pages are not bound to decks: each deck (serial) in
`settings/pages.json` "default-pages" gets its default page plus every page reachable from it
through "change page" keys; pages nobody reaches go to the first deck. The deck model isn't
stored, so it is guessed from the key positions. Both the states format and the pre-1.5 flat
format are read. Guesses are reported in the setup's warnings.
"""
import os
import re
import shlex

from ..models import MODELS
from ..safefs import inside
from ._common import (MAX_IMAGE_BYTES, MAX_JSON_BYTES, MAX_PAGES, ImageBudget, ImportError_,
                      clean_label, combo, import_image, needs_action, parse_lock, read_local,
                      safe_action, safe_json, url_action)

SOURCE = "streamcontroller"

# Linux evdev key codes (linux/input-event-codes.h) → X keysym names.
EVDEV_KEYS = {1: "Escape", 11: "0", 12: "minus", 13: "equal", 14: "BackSpace", 15: "Tab", 26: "bracketleft",
              27: "bracketright", 28: "Return", 39: "semicolon", 40: "apostrophe", 41: "grave",
              43: "backslash", 51: "comma", 52: "period", 53: "slash", 55: "KP_Multiply", 57: "space",
              58: "Caps_Lock", 69: "Num_Lock", 70: "Scroll_Lock", 74: "KP_Subtract", 78: "KP_Add",
              83: "KP_Decimal", 96: "KP_Enter", 98: "KP_Divide", 99: "Print", 102: "Home", 103: "Up",
              104: "Prior", 105: "Left", 106: "Right", 107: "End", 108: "Down", 109: "Next",
              110: "Insert", 111: "Delete", 113: "XF86AudioMute", 114: "XF86AudioLowerVolume",
              115: "XF86AudioRaiseVolume", 119: "Pause", 163: "XF86AudioNext", 164: "XF86AudioPlay",
              165: "XF86AudioPrev", 166: "XF86AudioStop", 87: "F11", 88: "F12"}
EVDEV_KEYS.update({2 + i: str(i + 1) for i in range(9)})                         # 1-9
for _row, _code in (("qwertyuiop", 16), ("asdfghjkl", 30), ("zxcvbnm", 44)):
    EVDEV_KEYS.update({_code + i: ch for i, ch in enumerate(_row)})
EVDEV_KEYS.update({59 + i: f"F{i + 1}" for i in range(10)})                      # F1-F10
EVDEV_KEYS.update({183 + i: f"F{13 + i}" for i in range(12)})                    # F13-F24
EVDEV_KEYS.update({71: "KP_7", 72: "KP_8", 73: "KP_9", 75: "KP_4", 76: "KP_5", 77: "KP_6",
                   79: "KP_1", 80: "KP_2", 81: "KP_3", 82: "KP_0"})
EVDEV_MODIFIERS = {29: "ctrl", 97: "ctrl", 42: "shift", 54: "shift", 56: "alt", 100: "alt",
                   125: "super", 126: "super"}

PREFIX = "com_core447_"
MEDIA = {"Play": "play", "Pause": "pause", "PlayPause": "play_pause", "Next": "next", "Previous": "previous"}
KEY_RE = re.compile(r"(\d{1,2})x(\d{1,2})")
MAX_GRID = 16
ASSET_DIR = os.path.join("Assets", "AssetManager", "Assets")


def _text(v, limit=256):
    return v.strip() if isinstance(v, str) and 0 < len(v.strip()) <= limit else None


def _stem(path):
    """Page name (file name without .json) of a page path, or None."""
    if not isinstance(path, str) or "\0" in path:
        return None
    base = os.path.basename(path.replace("\\", "/"))
    return base[:-5] if base.lower().endswith(".json") and len(base) > 5 else None


class _Reader:
    def __init__(self, root, preview=None):
        self.root = root
        self.real = os.path.realpath(root)
        self.budget = ImageBudget(preview=preview)
        self.warnings = []
        self.assumptions = set()

    def note(self, msg):
        if msg not in self.warnings:
            self.warnings.append(msg)

    # ------------------------------------------------------------ files

    def read_pages(self):
        folder = os.path.join(self.root, "pages")
        try:
            entries = sorted((e for e in os.scandir(folder) if e.name.lower().endswith(".json")),
                             key=lambda e: e.name)
        except OSError:
            raise ImportError_("That folder has no pages folder; is it StreamController's data folder?") from None
        pages = {}
        for e in entries:
            if e.is_symlink() or not e.is_file():
                self.note("Some page files were links or not regular files and were skipped.")
                continue
            if len(pages) >= MAX_PAGES:
                raise ImportError_(f"There are more than {MAX_PAGES} pages.")
            doc = safe_json(read_local(e.path, MAX_JSON_BYTES))
            if not isinstance(doc, dict):
                raise ImportError_("A page file isn't a JSON object.")
            pages[e.name[:-5]] = doc
        if not pages:
            raise ImportError_("That folder has no pages.")
        return pages

    def default_pages(self, pages):
        """[(serial, page name)] from settings/pages.json."""
        path = os.path.join(self.root, "settings", "pages.json")
        if not os.path.lexists(path):
            return []
        doc = safe_json(read_local(path, MAX_JSON_BYTES))
        defaults = doc.get("default-pages") if isinstance(doc, dict) else None
        if not isinstance(defaults, dict):
            return []
        out = []
        for serial, p in defaults.items():
            name = _stem(p)
            if name in pages and isinstance(serial, str):
                out.append((serial, name))
            else:
                self.note("A deck's default page doesn't exist and that deck was skipped.")
        return out

    # ------------------------------------------------------------ images

    def media(self, path):
        if not isinstance(path, str) or not path or "\0" in path:
            return None
        if not os.path.isabs(path) or not inside(path, self.real):
            self.budget.warn("Some key images are outside StreamController's data folder and were skipped.")
            return None
        if not os.path.lexists(path):
            alt = os.path.join(self.real, ASSET_DIR, os.path.basename(path))
            if os.path.isfile(alt) and not os.path.islink(alt):
                self.assumptions.add("Images missing from their recorded path were looked up by file name in Assets.")
                path = alt
        try:
            data = read_local(path, MAX_IMAGE_BYTES)
        except ImportError_:
            self.budget.warn("Some key images are missing or can't be read and were skipped.")
            return None
        return import_image(data, source=SOURCE, budget=self.budget)

    # ------------------------------------------------------------ actions

    def hotkey(self, s):
        keys = s.get("keys")
        if not isinstance(keys, list) or not keys or len(keys) > 64:
            return "A hotkey with no keys set"
        held, combos = set(), []
        for ev in keys:
            if not (isinstance(ev, (list, tuple)) and len(ev) == 2 and all(isinstance(x, int) for x in ev)):
                return "A hotkey that couldn't be read"
            code, down = ev
            if code in EVDEV_MODIFIERS:
                (held.add if down else held.discard)(EVDEV_MODIFIERS[code])
            elif down:
                name = EVDEV_KEYS.get(code)
                if name is None:
                    return "A hotkey with a key we don't know"
                combos.append(combo(held, name))
        if not combos:
            return "A hotkey of modifier keys only"
        self.assumptions.add("Hotkeys are lists of [evdev key code, 1=press / 0=release].")
        return {"keys": combos[0] if len(combos) == 1 else combos}

    def convert_action(self, a, pages):
        """Our action, or a str note for needs_action."""
        aid = a.get("id") if isinstance(a.get("id"), str) else ""
        s = a.get("settings") if isinstance(a.get("settings"), dict) else {}
        plugin, _, suffix = aid.partition("::")
        match (plugin, suffix):
            case ("com_core447_OSPlugin", "Hotkey"):
                return self.hotkey(s)
            case ("com_core447_OSPlugin", "RunCommand"):
                cmd = s.get("command")
                if not isinstance(cmd, str) or not cmd.strip() or len(cmd) > 4096:
                    return "A command that couldn't be read"
                return {"run": cmd}
            case ("com_core447_OSPlugin", "Launch"):
                app = s.get("app_path")
                if not isinstance(app, str) or not app.strip() or len(app) > 1024 or "\0" in app:
                    return "A program that couldn't be read"
                self.assumptions.add("Launch keys: app_path is run as a program, or via gtk-launch when it is a .desktop file.")
                if app.endswith(".desktop"):
                    return {"run": "gtk-launch " + shlex.quote(os.path.basename(app)[:-8])}
                return {"run": shlex.quote(app)}
            case ("com_core447_OSPlugin", "OpenInBrowser"):
                return {"__url": s.get("url")}
            case ("com_core447_OSPlugin", "WriteText"):
                text = s.get("text")
                if not isinstance(text, str) or not text or len(text) > 2000:
                    return "Types text that couldn't be read"
                return {"type": "".join(c for c in text.replace("\r\n", "\n") if c in "\n\t" or c.isprintable())}
            case ("com_core447_DeckPlugin", "ChangePage"):
                name = _stem(s.get("selected_page"))
                if name in pages:
                    return {"page": name}
                return "Goes to a page that wasn't found"
            case ("com_core447_DeckPlugin", "GoToPreviousPage"):
                return {"page": "back"}
            case ("com_core447_OBSPlugin", "SwitchScene"):
                scene = _text(s.get("scene"))
                return {"obs_scene": scene} if scene else "Switches to an OBS scene whose name couldn't be read"
            case ("com_core447_OBSPlugin", "ToggleStream"):
                return {"obs": "ToggleStream"}
            case ("com_core447_OBSPlugin", "ToggleRecord"):
                return {"obs": "ToggleRecord"}
            case ("com_core447_OBSPlugin", "ToggleInputMute"):
                src = _text(s.get("input"))
                self.assumptions.add("OBS mute keys: the source name is read from the \"input\" setting.")
                return {"obs_mute": src} if src else "Mutes an OBS source whose name couldn't be read"
            case ("com_core447_MediaPlugin", sfx) if sfx in MEDIA:
                return {"media": MEDIA[sfx]}
        return "This action isn't supported yet"

    def convert_key(self, st, pages):
        key = {}
        labels = st.get("labels") if isinstance(st.get("labels"), dict) else {}
        texts, fg = [], None
        for pos in ("top", "center", "bottom"):
            lab = labels.get(pos)
            t = lab.get("text") if isinstance(lab, dict) else None
            if isinstance(t, str) and t.strip():
                texts.append(t.strip())
                c = self.rgba(lab.get("color"))
                if c and c != "#ffffff" and fg is None:
                    fg = c
        label = clean_label("\n".join(texts))
        if label:
            key["label"] = label
        if fg:
            key["fg"] = fg
        bgd = st.get("background")
        bg = self.rgba(bgd.get("color")) if isinstance(bgd, dict) else None
        if bg and bg != "#000000":
            key["bg"] = bg
        media = st.get("media")
        icon = self.media(media.get("path")) if isinstance(media, dict) else None
        if icon:
            key["icon"] = icon
        acts = [a for a in st.get("actions") or [] if isinstance(a, dict)] if isinstance(st.get("actions"), list) else []
        got, urls = [], []
        for a in acts:
            res = self.convert_action(a, pages)
            if isinstance(res, str):
                key.update(needs_action(a.get("id"), res))
                return key
            if "__url" in res:
                urls.append(res["__url"])
            got.append(res)
        if not got:
            return key
        if len(got) == 1 and urls:
            key.update(url_action(urls[0], source=SOURCE, original_id=acts[0].get("id")))
            return key
        if urls:
            key.update(needs_action(acts[0].get("id"), "Several actions including a link"))
            return key
        key["action"] = safe_action(got[0] if len(got) == 1 else got, source=SOURCE,
                                    original_id=acts[0].get("id") if len(acts) == 1 else "multiple")
        return key

    @staticmethod
    def rgba(v):
        if (isinstance(v, (list, tuple)) and len(v) in (3, 4)
                and all(isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= 255 for x in v)):
            if len(v) == 4 and v[3] == 0:
                return None
            return "#%02x%02x%02x" % tuple(v[:3])
        return None

    # ------------------------------------------------------------ pages

    def state(self, key_doc):
        """The state-0 dict of an input (states format) or the input itself (flat, pre-1.5)."""
        if not isinstance(key_doc, dict):
            return {}
        states = key_doc.get("states")
        if isinstance(states, dict):
            if len(states) > 1:
                self.note("Keys with several states were imported with their first state only.")
            st = states.get("0")
            if st is None and states:
                st = states[sorted(states, key=str)[0]]
            return st if isinstance(st, dict) else {}
        self.assumptions.add("Some pages use the old format without key states.")
        return key_doc

    def keys_of(self, doc):
        keys = doc.get("keys")
        out = []
        for pos, kd in (keys.items() if isinstance(keys, dict) else ()):
            m = KEY_RE.fullmatch(pos) if isinstance(pos, str) else None
            if m:
                out.append((int(m.group(1)), int(m.group(2)), kd))
            else:
                self.note("Some keys had a position we don't understand and were skipped.")
        if any(isinstance(doc.get(k), dict) and doc[k] for k in ("dials", "touchscreens", "touch_keys", "screens")):
            self.note("Dials and touchscreens aren't imported yet.")
        return out

    def reachable(self, start, pages):
        seen, queue = {start}, [start]
        while queue:
            for _, _, kd in self.keys_of(pages[queue.pop(0)]):
                for a in self.state(kd).get("actions") or []:
                    if isinstance(a, dict) and isinstance(a.get("id"), str) \
                            and a["id"].endswith("DeckPlugin::ChangePage"):
                        s = a.get("settings") if isinstance(a.get("settings"), dict) else {}
                        t = _stem(s.get("selected_page"))
                        if t in pages and t not in seen:
                            seen.add(t)
                            queue.append(t)
        return seen

    def build_deck(self, serial, names, start, pages):
        coords = [(x, y) for n in names for x, y, _ in self.keys_of(pages[n])]
        xmax = max((x for x, _ in coords), default=-1)
        ymax = max((y for _, y in coords), default=-1)
        need = (xmax + 1, ymax + 1)
        model, (cols, rows) = None, (1, 1)
        for m in ("Stream Deck Mini", "Stream Deck Original", "Stream Deck XL"):
            r, c = MODELS[m][:2]
            if need[0] <= c and need[1] <= r:
                model, (cols, rows) = m, (c, r)
                break
        if model:
            self.assumptions.add(f"The deck model isn't stored; it was read as a {model} from the key positions.")
        else:
            cols, rows = min(max(need[0], 1), MAX_GRID), min(max(need[1], 1), MAX_GRID)
            self.note("The keys don't fit a known deck; the grid is the keys' extent (at most 16×16).")
        out = {}
        for n in names:
            grid = [[None] * cols for _ in range(rows)]
            for x, y, kd in self.keys_of(pages[n]):
                if x >= cols or y >= rows:
                    self.note("Some keys were outside the deck's grid and were skipped.")
                    continue
                grid[y][x] = self.convert_key(self.state(kd), pages) or None
            out[n] = grid
        deck = {"model": model, "grid": (cols, rows), "pages": out, "start_page": start}
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", serial or ""):
            deck["serial"] = serial
        return deck


def read(path, *, preview=None):
    """A setup dict (see _common) from StreamController's data folder. ImportError_ on failure.
    preview: a preview token; icons then go to the preview folder (see _common.ImageBudget)."""
    with parse_lock():
        if not isinstance(path, (str, os.PathLike)) or not os.path.isdir(path):
            raise ImportError_("That isn't a folder.")
        reader = _Reader(os.fspath(path), preview)
        pages = reader.read_pages()
        defaults = reader.default_pages(pages)
        if not defaults:
            reader.note("No deck has a default page, so all pages were put on one deck.")
            defaults = [(None, sorted(pages)[0])]
        decks, assigned = [], set()
        for serial, start in defaults:
            reach = reader.reachable(start, pages)
            assigned |= reach
            decks.append((serial, start, [start] + [n for n in sorted(pages) if n in reach and n != start]))
        rest = [n for n in sorted(pages) if n not in assigned]
        if rest:
            decks[0][2].extend(rest)
            reader.note("Pages no deck can reach were added to the first deck.")
        built = [reader.build_deck(serial, names, start, pages) for serial, start, names in decks]
        warnings = reader.warnings + reader.budget.warnings
        if reader.assumptions:
            warnings.append("Assumed (not verified against StreamController): " + " ".join(sorted(reader.assumptions)))
        return {"source": SOURCE, "decks": built, "warnings": warnings}
