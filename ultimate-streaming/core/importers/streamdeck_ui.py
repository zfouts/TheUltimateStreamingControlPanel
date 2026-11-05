"""streamdeck-ui / streamdeck-linux-gui `~/.streamdeck_ui.json` (format v1 and v2).

Buttons are numbered in reading order per page; the file never names the deck model, so the grid
is the smallest known deck with enough keys. Everything that affects the result and isn't
verified against the program is reported in the setup's warnings.
"""
import math
import os
import re

from ..models import MODELS
from ..safefs import inside
from ._common import (MAX_IMAGE_BYTES, MAX_JSON_BYTES, MAX_PAGES, ImageBudget, ImportError_,
                      clean_label, color, combo, import_image, needs_action, parse_lock, read_local,
                      safe_action, safe_json)

SOURCE = "streamdeck-ui"

# Models in the order a deck is guessed from a key count (first with at least that many keys).
KEY_COUNT_ORDER = ("Stream Deck Pedal", "Stream Deck Mini", "Stream Deck +", "Stream Deck Original",
                   "Stream Deck XL", "Stream Deck + XL")
MAX_BUTTONS = 36

# streamdeck-ui key names (evdev KEY_<NAME> lower-cased, plus legacy aliases) → X keysym names.
KEY_NAMES = {
    "enter": "Return", "return": "Return", "esc": "Escape", "escape": "Escape", "backspace": "BackSpace",
    "tab": "Tab", "space": "space", "delete": "Delete", "del": "Delete", "insert": "Insert",
    "home": "Home", "end": "End", "pageup": "Prior", "pagedown": "Next", "page_up": "Prior",
    "page_down": "Next", "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "capslock": "Caps_Lock", "numlock": "Num_Lock", "scrolllock": "Scroll_Lock", "pause": "Pause",
    "sysrq": "Print", "print": "Print", "minus": "minus", "equal": "equal", "leftbrace": "bracketleft",
    "rightbrace": "bracketright", "semicolon": "semicolon", "apostrophe": "apostrophe", "grave": "grave",
    "backslash": "backslash", "dot": "period", "period": "period", "slash": "slash",
    "plus": "plus", "comma": "comma",
    "mute": "XF86AudioMute", "volumedown": "XF86AudioLowerVolume", "volumeup": "XF86AudioRaiseVolume",
    "playpause": "XF86AudioPlay", "play": "XF86AudioPlay", "nextsong": "XF86AudioNext",
    "previoussong": "XF86AudioPrev", "stopcd": "XF86AudioStop", "stop": "XF86AudioStop",
    "kpenter": "KP_Enter", "kpplus": "KP_Add", "kpminus": "KP_Subtract", "kpasterisk": "KP_Multiply",
    "kpslash": "KP_Divide", "kpdot": "KP_Decimal",
}
MODIFIERS = {"ctrl": "ctrl", "control": "ctrl", "leftctrl": "ctrl", "rightctrl": "ctrl",
             "alt": "alt", "leftalt": "alt", "rightalt": "alt", "alt_gr": "alt", "altgr": "alt",
             "shift": "shift", "leftshift": "shift", "rightshift": "shift",
             "super": "super", "meta": "super", "win": "super", "leftmeta": "super", "rightmeta": "super",
             "cmd": "super"}
BACK_KEY = {"label": "Back", "icon": "emoji:↩️", "bg": "#444444", "action": {"page": "back"}}
_DELAY = re.compile(r"delay(\d*\.?\d*)")


def guess_model(count):
    """(model | None, (cols, rows)) of the smallest known deck with at least `count` keys."""
    for model in KEY_COUNT_ORDER:
        r, c = MODELS[model][:2]
        if r * c >= count:
            return model, (c, r)
    return None, (min(max(count, 1), 16), (count + 15) // 16)


def key_name(name):
    """X keysym name for a streamdeck-ui key name, or None."""
    if name in KEY_NAMES:
        return KEY_NAMES[name]
    if re.fullmatch(r"[a-z0-9]", name):
        return name
    if re.fullmatch(r"f([1-9]|1\d|2[0-4])", name):
        return name.upper()
    m = re.fullmatch(r"(?:numpad_?|kp_?)(\d)", name)
    if m:
        return f"KP_{m.group(1)}"
    return None


def parse_keys(text):
    """(steps, note): steps is a list of {"keys": combo} / {"delay": seconds}; note is a str
    (and steps None) when the sequence can't be converted."""
    steps = []
    for section in re.sub(r"\s+", "", text.lower()).split(","):
        if not section:
            continue
        m = _DELAY.fullmatch(section)
        if m:
            try:
                secs = float(m.group(1)) if m.group(1) else 0.5
            except ValueError:
                return None, "A delay in the key sequence couldn't be read"
            if not math.isfinite(secs) or secs > 60:
                return None, "A delay in the key sequence is longer than 60 seconds"
            steps.append({"delay": round(secs, 3)})
            continue
        mods, keys = set(), []
        for part in section.split("+"):
            if part in MODIFIERS:
                mods.add(MODIFIERS[part])
            elif key_name(part):
                keys.append(key_name(part))
            else:
                return None, "A key sequence with a key we don't know"
        if len(keys) != 1:
            return None, "A key sequence that presses no key, or several non-modifier keys together"
        try:
            steps.append({"keys": combo(mods, keys[0])})
        except ValueError:
            return None, "A key sequence with a key we can't press"
    if not any("keys" in s for s in steps):
        return None, "A key sequence with no keys"
    return steps, None


def keys_action(steps):
    """One of our actions for parse_keys steps."""
    if all("keys" in s for s in steps):
        combos = [s["keys"] for s in steps]
        return {"keys": combos[0] if len(combos) == 1 else combos}
    return steps


def local_image(path, budget, source, *, roots=None):
    """Icon path of a user-chosen image file, or None (budget.warnings says why). Only regular
    files below the home folder or /usr/share are read."""
    if not isinstance(path, str) or not path:
        return None
    path = os.path.expanduser(path) if path.startswith("~/") else path
    if "\0" in path or not os.path.isabs(path):
        budget.warn("Some icons had a path we can't use and were skipped.")
        return None
    allowed = roots or (os.path.realpath(os.path.expanduser("~")), "/usr/share")
    if not any(inside(path, r) for r in allowed):
        budget.warn("Some icons are outside your home folder and were skipped.")
        return None
    try:
        data = read_local(path, MAX_IMAGE_BYTES)
    except ImportError_:
        budget.warn("Some icon files are missing or can't be read and were skipped.")
        return None
    return import_image(data, source=source, budget=budget)


class _Reader:
    def __init__(self, budget):
        self.budget = budget
        self.warnings = []
        self.assumptions = set()

    def note(self, msg):
        if msg not in self.warnings:
            self.warnings.append(msg)

    def button_state(self, button):
        """The ButtonState dict of a v1 or v2 button."""
        if isinstance(button.get("states"), dict):
            states = button["states"]
            if len(states) > 1:
                self.note("Buttons with several states were imported with their first state only.")
            st = states.get("0")
            if st is None and states:
                st = states[sorted(states, key=str)[0]]
            return st if isinstance(st, dict) else {}
        return button

    def convert_button(self, st, pages_by_idx):
        key = {}
        label = clean_label(st.get("text"))
        if label:
            key["label"] = label
        icon = local_image(st.get("icon"), self.budget, SOURCE)
        if icon:
            key["icon"] = icon
        bg = color(st.get("background_color"))
        if bg and bg != "#000000":
            key["bg"] = bg
        fg = color(st.get("font_color"))
        if fg and fg != "#ffffff":
            key["fg"] = fg
        parts, bad = [], None
        cmd = st.get("command")
        if isinstance(cmd, str) and cmd.strip():
            if len(cmd) > 4096:
                bad = "A command that is too long"
            else:
                parts.append({"run": cmd})
        keys = st.get("keys")
        if isinstance(keys, str) and keys.strip():
            steps, why = parse_keys(keys)
            if steps is None:
                bad = bad or why
            else:
                got = keys_action(steps)
                parts.extend(got if isinstance(got, list) else [got])
        text = st.get("write")
        if isinstance(text, str) and text:
            if len(text) > 2000:
                bad = bad or "Text that is too long to type"
            else:
                text = "".join(c for c in text.replace("\r\n", "\n") if c in "\n\t" or c.isprintable())
                parts.append({"type": text})
        delta = st.get("brightness_change")
        if isinstance(delta, int) and not isinstance(delta, bool) and delta:
            self.assumptions.add("Brightness keys become \"brighter\" / \"dimmer\" (the step size isn't kept).")
            parts.append({"brightness": "up" if delta > 0 else "down"})
        target = st.get("switch_page")
        if isinstance(target, int) and not isinstance(target, bool) and target:
            self.assumptions.add("Page numbers in switch_page count from 1 (unverified).")
            name = pages_by_idx.get(target - 1)
            if name is None:
                bad = bad or "Goes to a page that doesn't exist"
            else:
                parts.append({"page": name})
        sw = st.get("switch_state")
        if isinstance(sw, int) and not isinstance(sw, bool) and sw:
            self.note("Keys that switch the button state were imported without that part.")
        if bad:
            key.update(needs_action("streamdeck_ui", bad))
        elif parts:
            key["action"] = safe_action(parts[0] if len(parts) == 1 else parts,
                                        source=SOURCE, original_id="streamdeck_ui")
        return key

    def convert_deck(self, serial, deck):
        buttons = deck.get("buttons")
        if not isinstance(buttons, dict):
            raise ImportError_("A deck in the file has no buttons.")
        pages = {}                                  # page index → {button index → state dict}
        for pk, page in buttons.items():
            if not (isinstance(pk, str) and pk.isdigit() and len(pk) <= 4 and isinstance(page, dict)):
                self.note("Some pages weren't in a format we understand and were skipped.")
                continue
            idxs = {}
            for bk, button in page.items():
                if isinstance(bk, str) and bk.isdigit() and len(bk) <= 4 and isinstance(button, dict):
                    idxs[int(bk)] = self.button_state(button)
                else:
                    self.note("Some buttons weren't in a format we understand and were skipped.")
            pages[int(pk)] = idxs
        if not pages:
            raise ImportError_("A deck in the file has no pages.")
        if len(pages) > MAX_PAGES:
            raise ImportError_(f"A deck has more than {MAX_PAGES} pages.")
        top = max((max(p, default=-1) for p in pages.values()), default=-1) + 1
        counts = max((len(p) for p in pages.values()), default=0)
        model, (cols, rows) = guess_model(max(top, counts, 1))
        if model:
            self.assumptions.add(f"The deck model isn't stored; it was read as a {model} from the button count.")
        else:
            self.note(f"A deck has more than {MAX_BUTTONS} buttons per page; the extra buttons were skipped.")
        names = {i: f"Page {i + 1}" for i in sorted(pages)}
        out = {}
        for i in sorted(pages):
            grid = [[None] * cols for _ in range(rows)]
            for idx, st in pages[i].items():
                r, c = divmod(idx, cols)
                if r >= rows:
                    continue
                key = self.convert_button(st, names)
                grid[r][c] = key or None
            out[names[i]] = grid
        result = {"model": model, "grid": (cols, rows), "pages": out, "start_page": names[min(names)]}
        if isinstance(serial, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", serial):
            result["serial"] = serial
        return result


def read(path, *, preview=None):
    """A setup dict (see _common) from the path of a streamdeck-ui JSON file. ImportError_ on failure.
    preview: a preview token; icons then go to the preview folder (see _common.ImageBudget)."""
    with parse_lock():
        doc = safe_json(read_local(path, MAX_JSON_BYTES))
        if not isinstance(doc, dict) or not isinstance(doc.get("state"), dict):
            raise ImportError_("That file isn't a streamdeck-ui configuration.")
        version = doc.get("streamdeck_ui_version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ImportError_("That file isn't a streamdeck-ui configuration (no version).")
        budget = ImageBudget(preview=preview)
        reader = _Reader(budget)
        if version > 2:
            reader.note("This file is from a newer streamdeck-ui than we know; it was read as version 2.")
        decks = [reader.convert_deck(serial, deck) for serial, deck in doc["state"].items()
                 if isinstance(deck, dict)]
        if not decks:
            raise ImportError_("That file has no decks.")
        warnings = reader.warnings + budget.warnings
        if reader.assumptions:
            warnings.append("Assumed (not verified against streamdeck-ui): " + " ".join(sorted(reader.assumptions)))
        return {"source": SOURCE, "decks": decks, "warnings": warnings}
