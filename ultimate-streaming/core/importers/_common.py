"""Shared, hostile-input-safe building blocks for the importers (Elgato, StreamController, ...).

Everything an importer reads came from someone else's program or from the internet, so every
byte is treated as hostile. An importer only ever:

  * reads local files with `read_local()` (regular files only, size-capped) or bundles with
    `open_zip()` (every entry validated before anything is read);
  * parses JSON with `safe_json()` (size and nesting capped);
  * turns images into icons with `import_image()` (decoder allow-list, pixel / frame caps,
    re-encoded PNG without metadata, content-addressed under our state dir);
  * builds key dicts from `clean_label()`, `combo()` / `vk_key()`, `url_action()`,
    `safe_action()` and `needs_action()`;
  * hands its setup dict to `to_config()`, which re-checks every key and page reference and
    returns (config, info).

A setup dict (what `read()` returns) is:

    {"source": "elgato",
     "decks": [{"model": "Stream Deck XL" | None, "grid": (cols, rows),
                "pages": {"<any name>": [[key | None, ...], ...]},   # rows top to bottom
                "start_page": "<a name from pages>"}],
     "warnings": ["...", ...]}

A key is {"label", "icon", "bg", "fg", "action"} (all optional) or, when its action could not be
converted, {"label", "icon", **needs_action(original_id, note)} with no action. Page actions
may use the setup's own page names; to_config() renames them.

User-facing text (ImportError_ messages, warnings, notes) never quotes file content: at most
counts, positions, our own page slugs and the source program's action type ids.
"""
import copy
import hashlib
import io
import json
import math
import os
import re
import shlex
import shutil
import stat
import struct
import threading
import time
import unicodedata
import urllib.parse
import warnings
import zipfile
import zlib
from contextlib import contextmanager

from ..models import MODELS
from ..paths import state_dir
from ..safefs import NotRegular, TooBig, read_regular, write_atomic


class ImportError_(ValueError):
    """An import failed. The message is safe to show the user: it never quotes file content."""


# ------------------------------------------------------------------ limits (contract v2)

MAX_ENTRIES = 2000              # zip entries, shared with nested zips
MAX_ENTRY_BYTES = 20 << 20      # one zip entry, uncompressed
MAX_TOTAL_BYTES = 100 << 20     # all zip entries of one import, declared and actually read
MAX_RATIO = 100                 # uncompressed / compressed, per entry
RATIO_MIN_BYTES = 64 << 10      # ratio only judged above this size (tiny files compress oddly)
MAX_CENTRAL_DIR = MAX_ENTRIES * 1024
MAX_NAME = 512
MAX_ZIP_DEPTH = 1               # a zip inside the zip, but no deeper
MAX_JSON_BYTES = 1 << 20
MAX_JSON_DEPTH = 64
MAX_PAGES = 200
MAX_FOLDER_DEPTH = 10
MAX_LABEL = 64
MAX_IMAGE_BYTES = 20 << 20
MAX_PIXELS = 16_000_000
MAX_FRAMES = 300
ICON_PX = 288
MAX_ICONS = 2000
MAX_IMAGE_TOTAL = 100 << 20
IMAGE_FORMATS = ["PNG", "JPEG", "GIF", "WEBP"]

PARSE_LOCK = threading.BoundedSemaphore(1)
"""One parse at a time (the caps above are per import; this keeps them per process too).
Use `with parse_lock():` rather than acquiring it directly."""


@contextmanager
def parse_lock(timeout=60):
    """Hold PARSE_LOCK for one import; ImportError_ if another import is still busy after timeout s."""
    if not PARSE_LOCK.acquire(timeout=timeout):
        raise ImportError_("Another import is still being read. Try again in a moment.")
    try:
        yield
    finally:
        PARSE_LOCK.release()


def read_local(path, max_bytes=MAX_ENTRY_BYTES):
    """Bytes of a local regular file (never a symlink, pipe or device) of at most max_bytes."""
    try:
        return read_regular(path, max_bytes)[0]
    except TooBig:
        raise ImportError_(f"A file is larger than {max_bytes >> 20} MB.") from None
    except NotRegular:
        raise ImportError_("A path is not a regular file (or is a symlink).") from None
    except OSError:
        raise ImportError_("A file couldn't be read.") from None


# ------------------------------------------------------------------ JSON

_JSON_SPECIAL = re.compile(r'[\[\]{}"\\]')     # single characters only: no backtracking


def _json_depth_ok(text, max_depth):
    """True if [ / { nesting outside strings stays ≤ max_depth. One linear pass over the
    structural characters (a state machine with in-string and escape state), so hostile input
    such as an unterminated string full of \\" costs O(n)."""
    depth, in_str, escaped_at = 0, False, -1
    for m in _JSON_SPECIAL.finditer(text):
        i = m.start()
        if i == escaped_at:                 # the character right after a backslash in a string
            continue
        ch = text[i]
        if in_str:
            if ch == "\\":
                escaped_at = i + 1
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "[{":
            depth += 1
            if depth > max_depth:
                return False
        elif ch in "]}":
            depth -= 1
    return True


def safe_json(data, *, max_bytes=MAX_JSON_BYTES, max_depth=MAX_JSON_DEPTH):
    """Parse JSON from bytes (UTF-8, optional BOM): ≤ max_bytes, nesting ≤ max_depth, no NaN/Infinity.

    Raises ImportError_ for anything else. Nesting is measured before parsing, so a depth bomb
    never reaches the recursive parser (RecursionError is caught as well)."""
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("safe_json takes bytes")
    if len(data) > max_bytes:
        raise ImportError_(f"A settings file is larger than {max_bytes >> 10} KB.")
    try:
        text = bytes(data).decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ImportError_("A settings file isn't valid UTF-8 text.") from None
    if not _json_depth_ok(text, max_depth):
        raise ImportError_(f"A settings file is nested more than {max_depth} levels deep.")

    def no_constant(name):
        raise ValueError("non-finite number")

    try:
        return json.loads(text, parse_constant=no_constant)
    except RecursionError:
        raise ImportError_(f"A settings file is nested more than {max_depth} levels deep.") from None
    except ValueError:      # JSONDecodeError, huge ints, NaN
        raise ImportError_("A settings file isn't valid JSON.") from None


# ------------------------------------------------------------------ zip

class _ZipBudget:
    """Counters shared by a zip and the zips nested in it."""

    def __init__(self):
        self.entries = 0
        self.declared = 0
        self.read = 0


def _eocd_check(data):
    """Bound the central directory before zipfile parses it (it builds one object per record
    and ignores the declared count). Mirrors zipfile's own end-record search."""
    tail = len(data) - 22
    if tail < 0:
        raise ImportError_("That file isn't a valid zip archive.")
    if data[tail:tail + 4] == b"PK\x05\x06" and data[-2:] == b"\0\0":
        i = tail
    else:
        i = data.rfind(b"PK\x05\x06", max(0, len(data) - 65536 - 22))
        if i < 0:
            raise ImportError_("That file isn't a valid zip archive.")
    if len(data) - i < 22:                  # a signature too close to the end to be a record
        raise ImportError_("That file isn't a valid zip archive.")
    _, disk, disk_cd, _, n_total, size_cd, off_cd, _ = struct.unpack("<4sHHHHIIH", data[i:i + 22])
    if n_total == 0xFFFF or size_cd == 0xFFFFFFFF or off_cd == 0xFFFFFFFF:
        loc = data[i - 20:i] if i >= 20 else b""
        if len(loc) != 20 or loc[:4] != b"PK\x06\x07":
            raise ImportError_("That file isn't a valid zip archive.")
        off64 = struct.unpack("<4sIQI", loc)[2]
        rec = data[off64:off64 + 56]
        if len(rec) != 56 or rec[:4] != b"PK\x06\x06":
            # zipfile also looks right before the locator; accept that spot too
            rec = data[i - 20 - 56:i - 20] if i >= 76 else b""
            if len(rec) != 56 or rec[:4] != b"PK\x06\x06":
                raise ImportError_("That file isn't a valid zip archive.")
        _, _, _, _, disk, disk_cd, _, n_total, size_cd, _ = struct.unpack("<4sQHHIIQQQQ", rec)
    if disk or disk_cd:
        raise ImportError_("Multi-part zip archives aren't supported.")
    if n_total > MAX_ENTRIES or size_cd > MAX_CENTRAL_DIR:
        raise ImportError_(f"The archive has more than {MAX_ENTRIES} files.")


def _entry_name(info):
    """Normalised name of a zip entry ("\\" → "/", "." parts dropped), or ImportError_."""
    raw = info.orig_filename
    if "\0" in raw:
        raise ImportError_("The archive contains a file name with a NUL character.")
    name = raw.replace("\\", "/")
    if len(name) > MAX_NAME or not name.strip("/"):
        raise ImportError_("The archive contains an invalid file name.")
    if name.startswith("/") or re.match(r"[A-Za-z]:", name):
        raise ImportError_("The archive contains an absolute file path.")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ImportError_("The archive contains a file path that leaves the archive (\"..\").")
    if any(ord(c) < 32 or c == "\x7f" for c in name):
        raise ImportError_("The archive contains a file name with control characters.")
    return "/".join(parts) + ("/" if name.endswith("/") else "")


def _check_local_header(data, info):
    """The local header must agree with the central directory (other unzippers trust the local one)."""
    off = info.header_offset
    head = data[off:off + 30]
    if len(head) != 30 or head[:4] != b"PK\x03\x04":
        raise ImportError_("The archive is damaged.")
    _, _, flags, method, _, _, crc, csize, usize, nlen, _ = struct.unpack("<4sHHHHHIIIHH", head)
    if flags & 0x41:
        raise ImportError_("The archive contains encrypted files.")
    raw = data[off + 30:off + 30 + nlen]
    try:
        local_name = raw.decode("utf-8" if flags & 0x800 else "cp437")
    except UnicodeDecodeError:
        raise ImportError_("The archive is damaged.") from None
    if method != info.compress_type or local_name != info.orig_filename:
        raise ImportError_("The archive's file headers don't match its directory.")
    if not flags & 0x08 and csize != 0xFFFFFFFF and usize != 0xFFFFFFFF:
        if (csize, usize, crc) != (info.compress_size, info.file_size, info.CRC):
            raise ImportError_("The archive's file sizes don't match its directory.")


class SafeZip:
    """A zip archive whose entries were all validated by open_zip(). Read-only, in memory.

    Names are normalised ("\\" → "/"); directories are not listed. Every read counts against
    the import's shared byte budget (also for nested zips)."""

    def __init__(self, zf, entries, budget, depth):
        self._zf, self._entries, self._budget, self._depth = zf, entries, budget, depth
        self._folded = {}
        for n in entries:
            self._folded.setdefault(n.casefold(), n)

    def names(self):
        """Normalised names of all file entries, in archive order."""
        return list(self._entries)

    def lookup(self, name):
        """The archive name matching `name` exactly, else case-insensitively, else None.

        `name` may come from the file (e.g. an image path in a manifest): it is normalised the
        same way, and anything absolute or containing ".." simply isn't found."""
        if not isinstance(name, str) or "\0" in name or len(name) > MAX_NAME:
            return None
        n = name.replace("\\", "/")
        if n.startswith("/") or re.match(r"[A-Za-z]:", n):
            return None
        parts = [p for p in n.split("/") if p not in ("", ".")]
        if not parts or ".." in parts:
            return None
        n = "/".join(parts)
        return n if n in self._entries else self._folded.get(n.casefold())

    def size(self, name):
        """Declared uncompressed size of an entry (KeyError if there is no such entry)."""
        return self._entries[name].file_size

    def read(self, name, max_bytes=MAX_ENTRY_BYTES):
        """The bytes of entry `name` (a value from names()/lookup(); KeyError otherwise).

        ImportError_ if it is larger than max_bytes, would exceed the import's total, is damaged
        (CRC / size mismatch) or its real size differs from the declared one."""
        info = self._entries[name]
        limit = min(max_bytes, MAX_ENTRY_BYTES)
        if info.file_size > limit:
            raise ImportError_(f"A file in the archive is larger than {limit >> 10} KB.")
        chunks, got = [], 0
        try:
            with self._zf.open(info) as f:
                while True:
                    chunk = f.read(min(1 << 16, limit + 1 - got))
                    if not chunk:
                        break
                    got += len(chunk)
                    self._budget.read += len(chunk)
                    if got > limit or got > info.file_size:
                        raise ImportError_("A file in the archive is larger than it claims.")
                    if self._budget.read > MAX_TOTAL_BYTES:
                        raise ImportError_(f"The archive unpacks to more than {MAX_TOTAL_BYTES >> 20} MB.")
                    chunks.append(chunk)
        except ImportError_:
            raise
        except (zipfile.BadZipFile, zlib.error, EOFError, OSError, RuntimeError, NotImplementedError,
                ValueError, struct.error):
            raise ImportError_("A file in the archive is damaged.") from None
        if got != info.file_size:
            raise ImportError_("A file in the archive is damaged.")
        return b"".join(chunks)

    def open_nested(self, name):
        """A SafeZip of the zip stored in entry `name`. Only one level of nesting; it shares this
        archive's entry and byte budgets."""
        if self._depth >= MAX_ZIP_DEPTH:
            raise ImportError_("The archive contains zips nested too deeply.")
        return _open(self.read(name), self._budget, self._depth + 1)


def open_zip(data):
    """Validate every entry of a zip held in memory and return a SafeZip.

    Rejected (ImportError_): not a zip, multi-part, > 2000 entries, encrypted entries, methods
    other than stored/deflate, symlinks or other special files, duplicate names (after
    normalising, case-insensitively), NUL / control characters, absolute or drive paths, "..",
    local headers that disagree with the directory, any declared entry > 20 MB, a declared total
    > 100 MB, or a compression ratio > 100. Actual sizes are checked again on every read."""
    return _open(data, _ZipBudget(), 0)


def _open(data, budget, depth):
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("open_zip takes bytes")
    data = bytes(data)
    if len(data) > MAX_TOTAL_BYTES:
        raise ImportError_(f"The archive is larger than {MAX_TOTAL_BYTES >> 20} MB.")
    _eocd_check(data)
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        infos = zf.infolist()
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, ValueError, EOFError, struct.error,
            NotImplementedError):
        raise ImportError_("That file isn't a valid zip archive.") from None
    budget.entries += len(infos)
    if len(infos) > MAX_ENTRIES or budget.entries > MAX_ENTRIES:
        raise ImportError_(f"The archive has more than {MAX_ENTRIES} files.")
    entries, seen = {}, set()
    for info in infos:
        name = _entry_name(info)
        folded = name.rstrip("/").casefold()
        if folded in seen:
            raise ImportError_("The archive contains the same file name twice.")
        seen.add(folded)
        if info.flag_bits & 0x41:
            raise ImportError_("The archive contains encrypted files.")
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise ImportError_("The archive uses a compression method that isn't supported.")
        kind = stat.S_IFMT(info.external_attr >> 16)
        if kind and kind not in (stat.S_IFREG, stat.S_IFDIR):
            raise ImportError_("The archive contains a symlink or special file.")
        _check_local_header(data, info)
        if name.endswith("/"):
            continue
        if info.file_size > MAX_ENTRY_BYTES:
            raise ImportError_(f"A file in the archive is larger than {MAX_ENTRY_BYTES >> 20} MB.")
        if info.file_size > RATIO_MIN_BYTES and info.file_size > MAX_RATIO * max(info.compress_size, 1):
            raise ImportError_("A file in the archive is compressed suspiciously well (zip bomb?).")
        budget.declared += info.file_size
        if budget.declared > MAX_TOTAL_BYTES:
            raise ImportError_(f"The archive unpacks to more than {MAX_TOTAL_BYTES >> 20} MB.")
        entries[name] = info
    return SafeZip(zf, entries, budget, depth)


# ------------------------------------------------------------------ text

_KEEP = {"\n", "‍"}        # line breaks and ZWJ (emoji sequences)


def clean_label(text, max_len=MAX_LABEL):
    """Display text: control, format (bidi overrides, isolates, zero-width), surrogate,
    private-use, unassigned and line/paragraph-separator characters removed (ZWJ and "\\n"
    kept), trimmed, at most max_len characters. Non-strings give ""."""
    if not isinstance(text, str):
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out = "".join(c for c in text if c in _KEEP
                  or unicodedata.category(c) not in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"))
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out[:max_len].strip()


_SLUG = re.compile(r"[^a-z0-9_-]+")
# `page: back` means "previous page"; "pages" / "start_page" are a layout's own fields.
RESERVED_PAGES = {"back", "pages", "start_page"}


def page_name(name, taken):
    """A page name for our config: [a-z0-9_-], 1..32 characters, not reserved (back, pages,
    start_page — any case; those get a suffix), not in `taken`.

    `taken` is a set of names already used in the same layout; the result is added to it."""
    base = unicodedata.normalize("NFKD", name if isinstance(name, str) else "")
    base = base.encode("ascii", "ignore").decode().lower()
    base = _SLUG.sub("_", base).strip("_-")[:32].strip("_-") or "page"
    cand, n = base, 1
    while cand in taken or cand in RESERVED_PAGES:
        n += 1
        suffix = f"_{n}"
        cand = base[:32 - len(suffix)] + suffix
    taken.add(cand)
    return cand


# ------------------------------------------------------------------ keys

_MOD_ORDER = ("ctrl", "alt", "shift", "super")
_MOD_ALIASES = {"ctrl": "ctrl", "control": "ctrl", "ctl": "ctrl", "alt": "alt", "option": "alt",
                "opt": "alt", "shift": "shift", "super": "super", "win": "super", "meta": "super",
                "cmd": "super", "command": "super"}


def combo(mods, key):
    """A combo in our `keys:` syntax, e.g. combo({"shift", "ctrl"}, "a") -> "ctrl+shift+a".

    mods: modifier names (ctrl/control, alt/option, shift, super/win/meta/cmd), any case.
    key: an X keysym name ("F5", "Return", "minus", "XF86AudioPlay") or one printable character
    other than "+". ValueError for anything else."""
    names = set()
    for m in mods:
        canon = _MOD_ALIASES.get(str(m).lower())
        if not canon:
            raise ValueError("unknown modifier")
        names.add(canon)
    if not isinstance(key, str) or not (re.fullmatch(r"[A-Za-z0-9_]{1,32}", key)
                                        or (len(key) == 1 and key.isprintable() and key not in "+ ")):
        raise ValueError("unsupported key")
    if len(key) == 1 and key.isalpha():
        key = key.lower()
    return "+".join([m for m in _MOD_ORDER if m in names] + [key])


# Windows virtual-key codes (Elgato hotkeys store these) → X keysym names.
VK_KEYS = {8: "BackSpace", 9: "Tab", 12: "Clear", 13: "Return", 19: "Pause", 20: "Caps_Lock",
           27: "Escape", 32: "space", 33: "Prior", 34: "Next", 35: "End", 36: "Home", 37: "Left",
           38: "Up", 39: "Right", 40: "Down", 44: "Print", 45: "Insert", 46: "Delete",
           96: "KP_0", 97: "KP_1", 98: "KP_2", 99: "KP_3", 100: "KP_4", 101: "KP_5", 102: "KP_6",
           103: "KP_7", 104: "KP_8", 105: "KP_9", 106: "KP_Multiply", 107: "KP_Add",
           109: "KP_Subtract", 110: "KP_Decimal", 111: "KP_Divide", 144: "Num_Lock",
           145: "Scroll_Lock", 173: "XF86AudioMute", 174: "XF86AudioLowerVolume",
           175: "XF86AudioRaiseVolume", 176: "XF86AudioNext", 177: "XF86AudioPrev",
           178: "XF86AudioStop", 179: "XF86AudioPlay", 186: "semicolon", 187: "equal", 188: "comma",
           189: "minus", 190: "period", 191: "slash", 192: "grave", 219: "bracketleft",
           220: "backslash", 221: "bracketright", 222: "apostrophe"}
VK_KEYS.update({c: chr(c).lower() for c in range(65, 91)})          # A-Z
VK_KEYS.update({c: chr(c) for c in range(48, 58)})                  # 0-9
VK_KEYS.update({111 + n: f"F{n}" for n in range(1, 25)})            # F1-F24
VK_MODIFIERS = {16: "shift", 160: "shift", 161: "shift", 17: "ctrl", 162: "ctrl", 163: "ctrl",
                18: "alt", 164: "alt", 165: "alt", 91: "super", 92: "super"}


def vk_key(code):
    """X keysym name for a Windows virtual-key code, or None if we don't know it."""
    return VK_KEYS.get(code) if isinstance(code, int) and not isinstance(code, bool) else None


# ------------------------------------------------------------------ actions

LIVE_ACTIONS = {"page", "brightness", "delay", "notify", "obs_scene", "obs_mute", "obs_studio",
                "media", "volume", "mic"}
_MEDIA = {"play_pause", "next", "previous", "prev", "stop", "play", "pause"}
_MAX_PLAIN_DEPTH = 8


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _text_ok(v, limit):
    return isinstance(v, str) and 0 < len(v) <= limit and clean_label(v, limit) == v.strip() == v


def _live_arg_ok(kind, arg):
    """The argument of a live action is one the action handles safely. Some live actions build
    shell commands from their argument (volume → pactl), so free-form values are not live."""
    match kind:
        case "page":
            return isinstance(arg, str) and 0 < len(arg) <= 200
        case "brightness":
            return arg in ("cycle", "up", "down", "sleep") or (_num(arg) and 0 <= arg <= 100 and arg == int(arg))
        case "delay":
            return _num(arg) and 0 <= arg <= 60
        case "notify":
            return _text_ok(arg, 200) and not arg.startswith("-")
        case "obs_scene" | "obs_mute":
            return _text_ok(arg, 256)
        case "obs_studio":
            return arg == "toggle"
        case "media":
            return arg in _MEDIA
        case "volume":
            return arg in ("up", "down", "mute") or (isinstance(arg, str) and re.fullmatch(r"\d{1,3}%", arg)
                                                    and int(arg[:-1]) <= 150)
        case "mic":
            return arg == "mute"
    return False


def _is_live(action):
    if isinstance(action, list):
        return bool(action) and all(isinstance(a, dict) and _is_live(a) for a in action)
    return (isinstance(action, dict) and bool(action)
            and all(k in LIVE_ACTIONS and _live_arg_ok(k, v) for k, v in action.items()))


def _plain(v, depth=0):
    """A deep copy of v if it is plain data (str/int/float/bool/None, lists, str-keyed dicts)."""
    if depth > _MAX_PLAIN_DEPTH:
        raise ValueError("action nested too deeply")
    if v is None or isinstance(v, (str, bool, int)) or (isinstance(v, float) and math.isfinite(v)):
        return v
    if isinstance(v, (list, tuple)):
        return [_plain(x, depth + 1) for x in v]
    if isinstance(v, dict) and all(isinstance(k, str) for k in v):
        return {k: _plain(x, depth + 1) for k, x in v.items()}
    raise ValueError("action contains non-plain data")


def _clean_id(original_id):
    return clean_label(str(original_id) if original_id is not None else "", 200)


def safe_action(action, *, source, original_id):
    """An imported action as it may enter our config (contract v2 "Imported actions").

    `action` is our action syntax: a dict ({"keys": "ctrl+c"}) or a list of dicts. If every
    part is in LIVE_ACTIONS with an argument that action handles safely, a plain copy is
    returned and will run as is. Anything else (run, keys, type, obs, http, ..., a list
    containing any of them, a live action with an odd argument) comes back wrapped:

        {"needs_review": {"imported_action": <action>, "source": source, "original_id": id}}

    which the bot refuses to run until the panel replaces it with imported_action after the
    user's explicit per-key click. TypeError for string actions or non-plain data."""
    if isinstance(action, str) or not isinstance(action, (dict, list)):
        raise TypeError("importers emit dict (or list) actions only")
    try:
        action = _plain(action)
    except ValueError as e:
        raise TypeError(str(e)) from None
    if _is_live(action):
        return action
    return {"needs_review": {"imported_action": action, "source": str(source),
                             "original_id": _clean_id(original_id)}}


def needs_action(original_id, note):
    """Fields marking a key whose action couldn't be converted; merge into the key, no `action`:

        {"label": "Discord", **needs_action("com.elgato.discord.mute", "Discord isn't supported")}"""
    return {"needs_action": {"original_id": _clean_id(original_id), "note": clean_label(note, 200)}}


def safe_url(url):
    """url if it is an absolute http(s) URL without whitespace or control characters, else None."""
    if not isinstance(url, str) or not 0 < len(url) <= 2048 or url != url.strip():
        return None
    if any(c.isspace() or unicodedata.category(c) in ("Cc", "Cf") for c in url):
        return None
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    return url


def url_command(url):
    """Our action for opening a web page, {"run": "xdg-open '<url>'"} (shell-quoted; not live, so
    safe_action wraps it for review), or None if url isn't a plain http(s) URL. There is no
    dedicated open-URL action in modules/, so this is the closest existing one."""
    ok = safe_url(url)
    return {"run": f"xdg-open {shlex.quote(ok)}"} if ok else None


def url_action(url, *, source, original_id):
    """Key fields for a key that opens a web page: {"action": <needs_review run xdg-open>}, or
    a needs_action marker for anything that isn't http(s). Merge into the key dict."""
    run = url_command(url)
    if not run:
        return needs_action(original_id, "Opens a link that isn't http/https")
    return {"action": safe_action(run, source=source, original_id=original_id)}


# ------------------------------------------------------------------ images

class ImageBudget:
    """Per-import image limits: at most max_icons icons and max_bytes of image input.

    Pass the same budget to every import_image() call of one import. Images over budget are
    skipped (None) with a warning. `warnings` collects the user-facing reasons images were
    skipped; the importer copies them into its setup's warnings.

    preview: a preview token ([A-Za-z0-9_-], 8..128). Icons then go to
    <state_dir>/icons/imported/.preview/<token>/<source>/ instead of the final folder; on apply
    call commit_preview_icons(config, token), and call cleanup_preview_icons() now and then for
    previews nobody applied. None (default) writes straight to the final folder."""

    def __init__(self, max_icons=MAX_ICONS, max_bytes=MAX_IMAGE_TOTAL, preview=None):
        if preview is not None and not (isinstance(preview, str) and _TOKEN.fullmatch(preview)):
            raise ValueError("bad preview token")
        self.preview = preview
        self.max_icons, self.max_bytes = max_icons, max_bytes
        self.icons = 0
        self.bytes = 0
        self.warnings = []
        self._done = {}          # sha256 of input → output path (same image used twice)

    def warn(self, msg):
        if msg not in self.warnings:
            self.warnings.append(msg)


_SOURCE = re.compile(r"[a-z0-9_-]{1,32}")       # never starts with ".", so never ".preview"
_TOKEN = re.compile(r"[A-Za-z0-9_-]{8,128}")
_ICON_NAME = re.compile(r"[0-9a-f]{64}\.png")
PREVIEW_DIR = ".preview"


def _imported_root():
    return os.path.join(state_dir(), "icons", "imported")


def icons_dir(source, preview=None):
    """<state_dir>/icons/imported/<source> (or .preview/<token>/<source> for a preview),
    created with 0700 directories."""
    if not isinstance(source, str) or not _SOURCE.fullmatch(source):
        raise ValueError("bad importer source name")
    if preview is not None and not (isinstance(preview, str) and _TOKEN.fullmatch(preview)):
        raise ValueError("bad preview token")
    base = os.path.join(state_dir(), "icons")
    dirs = [base, os.path.join(base, "imported")]
    if preview is not None:
        dirs += [os.path.join(dirs[-1], PREVIEW_DIR), os.path.join(dirs[-1], PREVIEW_DIR, preview)]
    dirs.append(os.path.join(dirs[-1], source))
    os.makedirs(dirs[-1], mode=0o700, exist_ok=True)
    for d in dirs:
        os.chmod(d, 0o700)
    return dirs[-1]


def commit_preview_icons(config, token):
    """A copy of config with every icon from preview `token` moved into the final folder
    (<state_dir>/icons/imported/<source>/<sha>.png) and its path rewritten; the preview folder is
    then removed. Icons that are gone (cleaned up) are dropped from their keys. Call it with the
    stored preview config right before core.importer.apply."""
    if not (isinstance(token, str) and _TOKEN.fullmatch(token)):
        raise ValueError("bad preview token")
    root = os.path.realpath(os.path.join(_imported_root(), PREVIEW_DIR, token))

    def fix(v, depth=0):
        if depth > 12:
            return
        if isinstance(v, list):
            for x in v:
                fix(x, depth + 1)
        elif isinstance(v, dict):
            icon = v.get("icon")
            if isinstance(icon, str) and os.path.isabs(icon) and os.path.realpath(icon).startswith(root + os.sep):
                real = os.path.realpath(icon)
                parts = os.path.relpath(real, root).split(os.sep)
                dest = None
                if len(parts) == 2 and _SOURCE.fullmatch(parts[0]) and _ICON_NAME.fullmatch(parts[1]):
                    source, name = parts
                    final = os.path.join(icons_dir(source), name)
                    try:
                        st = os.lstat(real)
                        if stat.S_ISREG(st.st_mode):
                            os.replace(real, final)
                            dest = final
                    except FileNotFoundError:
                        dest = final if os.path.isfile(final) else None    # moved by an earlier key
                if dest:
                    v["icon"] = dest
                else:
                    del v["icon"]
            for k, x in v.items():
                if k != "icon":
                    fix(x, depth + 1)

    out = copy.deepcopy(config)
    fix(out)
    shutil.rmtree(root, ignore_errors=True)
    return out


def cleanup_preview_icons(older_than=3600, now=None):
    """Remove preview icon folders not written to for older_than seconds (previews that were
    never applied). Returns how many were removed."""
    base = os.path.join(_imported_root(), PREVIEW_DIR)
    now = time.time() if now is None else now
    removed = 0
    try:
        names = os.listdir(base)
    except FileNotFoundError:
        return 0
    for name in names:
        path = os.path.join(base, name)
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            continue
        if not _TOKEN.fullmatch(name) or not stat.S_ISDIR(st.st_mode):
            continue
        if st.st_mtime < now - older_than:
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    return removed


def _looks_svg(data):
    head = data[:2048].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return head.startswith(b"<") and (b"<svg" in head or head.startswith(b"<?xml"))


def import_image(data, *, source, budget):
    """Store an untrusted image as one of our icons; its absolute path, or None (with a warning
    in budget.warnings) if it was refused.

    Only PNG / JPEG / GIF / WEBP decoders are used; size (≤ 16 Mpx) is checked from the header
    before any pixel is decoded; Pillow's DecompressionBombWarning is an error; animations
    > 300 frames are refused. The first frame is scaled to ≤ 288 px, copied into a fresh image
    (no EXIF, ICC, text chunks...) and saved as PNG at
    <state_dir>/icons/imported/<source>/<sha256 of that PNG>.png (file 0600, dirs 0700), or
    under .preview/<token>/ when the budget has a preview token.
    SVG is refused. ImportError_ only if the icon can't be written to disk."""
    from PIL import Image, UnidentifiedImageError

    if not isinstance(data, (bytes, bytearray)) or not data:
        budget.warn("Some icons were empty and were skipped.")
        return None
    data = bytes(data)
    digest_in = hashlib.sha256(data).hexdigest()
    if digest_in in budget._done:
        return budget._done[digest_in]
    if _looks_svg(data):
        budget.warn("Some icons are SVG images, which aren't imported; those keys have no icon.")
        return None
    if len(data) > MAX_IMAGE_BYTES:
        budget.warn(f"Some icons are larger than {MAX_IMAGE_BYTES >> 20} MB and were skipped.")
        return None
    if budget.icons >= budget.max_icons:
        budget.warn(f"More than {budget.max_icons} icons; the rest were skipped.")
        return None
    if budget.bytes + len(data) > budget.max_bytes:
        budget.warn(f"Icons add up to more than {budget.max_bytes >> 20} MB; the rest were skipped.")
        return None
    budget.bytes += len(data)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=IMAGE_FORMATS) as img:
                w, h = img.size
                if w <= 0 or h <= 0 or w * h > MAX_PIXELS:
                    budget.warn("Some icons are too large (over 16 megapixels) and were skipped.")
                    return None
                if getattr(img, "n_frames", 1) > MAX_FRAMES:
                    budget.warn(f"Some animated icons have more than {MAX_FRAMES} frames and were skipped.")
                    return None
                img.seek(0)
                if img.format == "JPEG":
                    img.draft("RGB", (ICON_PX, ICON_PX))
                img.load()
                frame = img.convert("RGBA")
    except UnidentifiedImageError:
        budget.warn("Some icons aren't PNG, JPEG, GIF or WEBP images and were skipped.")
        return None
    except (Image.DecompressionBombWarning, Image.DecompressionBombError):
        budget.warn("Some icons look like decompression bombs and were skipped.")
        return None
    except (OSError, ValueError, EOFError, SyntaxError, MemoryError, struct.error, IndexError, KeyError,
            TypeError, zlib.error):
        budget.warn("Some icons are damaged and were skipped.")
        return None
    frame.thumbnail((ICON_PX, ICON_PX), Image.LANCZOS)
    clean = Image.new("RGBA", frame.size)
    clean.paste(frame)                       # a fresh image: nothing of the original's info survives
    buf = io.BytesIO()
    clean.save(buf, format="PNG")
    out = buf.getvalue()
    sha = hashlib.sha256(out).hexdigest()
    try:
        folder = icons_dir(source, budget.preview)
        path = os.path.join(folder, sha + ".png")
        write_atomic(path, out)
        if budget.preview is not None:          # the preview's age, for cleanup_preview_icons
            os.utime(os.path.dirname(folder))
    except OSError:
        raise ImportError_("Couldn't save the imported icons (is the disk full?).") from None
    budget.icons += 1
    budget._done[digest_in] = path
    return path


# ------------------------------------------------------------------ setup → config

_COLOR = re.compile(r"#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})")
KEY_FIELDS = ("label", "icon", "bg", "fg", "action", "needs_action")


def color(v):
    """"#rrggbb" (lower case) from "#rgb" / "#rrggbb" / "rrggbb", else None."""
    m = _COLOR.fullmatch(v.strip()) if isinstance(v, str) else None
    if not m:
        return None
    h = m.group(1)
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return "#" + h.lower()


class _MissingPage(Exception):
    pass


def _rename_pages(action, rename):
    """action with every {page: X} pointing at rename[X] ("back" kept); _MissingPage otherwise."""
    if isinstance(action, list):
        return [_rename_pages(a, rename) for a in action]
    if isinstance(action, dict):
        out = {}
        for k, v in action.items():
            if k == "page":
                if v == "back":
                    out[k] = v
                elif isinstance(v, str) and v in rename:
                    out[k] = rename[v]
                else:
                    raise _MissingPage()
            elif k == "needs_review" and isinstance(v, dict):
                out[k] = {**v, "imported_action": _rename_pages(v.get("imported_action"), rename)}
            else:
                out[k] = v
        return out
    return action


def _review_ok(action):
    """A well-formed needs_review wrapper."""
    if not (isinstance(action, dict) and set(action) == {"needs_review"} and isinstance(action["needs_review"], dict)):
        return False
    nr = action["needs_review"]
    return set(nr) == {"imported_action", "source", "original_id"} and isinstance(nr["imported_action"], (dict, list))


def _clean_key(key, rename, source, icon_root, problems):
    if key is None:
        return None
    if not isinstance(key, dict):
        problems.add("Some keys weren't in a format we understand and were left empty.")
        return None
    out = {}
    label = clean_label(key.get("label"))
    if label:
        out["label"] = label
    icon = key.get("icon")
    if isinstance(icon, str) and icon:
        if icon.startswith("emoji:") and 6 < len(icon) <= 32 and clean_label(icon, 32) == icon:
            out["icon"] = icon
        elif os.path.isabs(icon) and os.path.realpath(icon).startswith(icon_root + os.sep) \
                and os.path.isfile(icon):
            out["icon"] = os.path.realpath(icon)
        else:
            problems.add("Some icons pointed outside the imported icons folder and were dropped.")
    for f in ("bg", "fg"):
        c = color(key.get(f))
        if c:
            out[f] = c
    marker = key.get("needs_action")
    if marker:
        m = marker if isinstance(marker, dict) else {}
        out.update(needs_action(m.get("original_id"), m.get("note") or "This action needs to be set up again."))
        return out
    action = key.get("action")
    if action is None:
        return out
    try:
        action = _rename_pages(_plain(action), rename)
    except _MissingPage:
        out.update(needs_action("page", "Went to a page that wasn't imported"))
        return out
    except ValueError:
        out.update(needs_action("", "The action couldn't be read"))
        return out
    if isinstance(action, str) or not isinstance(action, (dict, list)):
        out.update(needs_action("", "The action couldn't be read"))
        return out
    if _review_ok(action):
        nr = action["needs_review"]
        out["action"] = {"needs_review": {"imported_action": nr["imported_action"], "source": source,
                                          "original_id": _clean_id(nr["original_id"])}}
    else:
        out["action"] = safe_action(action, source=source, original_id="")
    return out


def _clean_rows(rows, cols, nrows, rename, source, icon_root, problems):
    if not isinstance(rows, list):
        problems.add("Some pages weren't in a format we understand and were left empty.")
        rows = []
    if len(rows) > nrows or any(isinstance(r, list) and len(r) > cols for r in rows):
        problems.add("Some keys were outside the deck's grid and were dropped.")
    out = []
    for r in rows[:nrows]:
        r = r if isinstance(r, list) else []
        row = [_clean_key(k, rename, source, icon_root, problems) for k in r[:cols]]
        out.append(row + [None] * (cols - len(row)))
    return out + [[None] * cols for _ in range(nrows - len(out))]


_SERIAL = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def to_config(setup):
    """(config, info): an incoming config for core.importer.plan / apply, and what was detected.

    One deck → top-level {pages, start_page}, always (plan(target_layout=...) maps flat pages
    into the layout the user's attached deck uses, so nothing of theirs gets hidden).

    Several decks → {layouts: {"COLSxROWS": {pages, start_page}}}: named by grid, never by
    model, so they merge page by page into the user's grid layouts (the bot prefers a model-named
    layout, so one would silently replace theirs). A second deck with the same grid gets its own
    layout "COLSxROWS-2" (then -3, ...) and, when the setup knows its serial
    (deck["serial"]), `devices: {serial: {layout: "COLSxROWS-2"}}` so that deck shows it;
    without a serial a warning says to pick the layout in the panel.

    info is informational only and must not be planned: {"source", "model", "grid": [cols, rows]}
    for one deck; {"source", "decks": [{"model", "grid", "layout"}, ...]} for several (model is
    None when unknown). It is returned separately, not as a key of config, because plan()
    treats every unknown top-level key as a settings section to add.

    Page names become unique [a-z0-9_-] slugs (page_name, never back/pages/start_page) and
    every {page: X} — also inside lists and needs_review — follows the rename; a page action to
    a page that doesn't exist turns the key into a needs_action key. Every key is re-checked:
    only label/icon/bg/fg/action/needs_action survive, labels cleaned, colors normalised, icons
    kept only inside our imported icons folder (or "emoji:..."), actions passed through
    safe_action. At most 200 pages in all. Notes about dropped content are appended to
    setup["warnings"]."""
    if not isinstance(setup, dict):
        raise TypeError("setup must be a dict")
    source = setup.get("source")
    if not isinstance(source, str) or not _SOURCE.fullmatch(source):
        raise ValueError("bad importer source name")
    warn = setup.setdefault("warnings", [])
    decks = [d for d in setup.get("decks") or [] if isinstance(d, dict)]
    if not decks:
        raise ImportError_("No pages were found to import.")
    icon_root = os.path.realpath(os.path.join(state_dir(), "icons", "imported"))
    problems = set()
    layouts, devices, detected, total = {}, {}, [], 0
    for deck in decks:
        grid = deck.get("grid")
        try:
            cols, nrows = (int(v) for v in grid)
        except (TypeError, ValueError):
            cols = nrows = 0
        model = deck.get("model") if deck.get("model") in MODELS else None
        if model:
            nrows, cols = MODELS[model][:2]
        if not (1 <= cols <= 32 and 1 <= nrows <= 32):
            problems.add("A deck with an unknown key grid was skipped.")
            continue
        base = lname = f"{cols}x{nrows}"
        n = 1
        while lname in layouts:
            n += 1
            lname = f"{base}-{n}"
        pages = deck.get("pages") if isinstance(deck.get("pages"), dict) else {}
        taken, rename = set(), {}
        for orig in pages:
            if total >= MAX_PAGES:
                problems.add(f"More than {MAX_PAGES} pages; the rest weren't imported.")
                break
            rename[orig] = page_name(orig, taken)
            total += 1
        if not rename:
            continue
        lay = {"pages": {new: _clean_rows(pages[orig], cols, nrows, rename, source, icon_root, problems)
                         for orig, new in rename.items()}}
        start = rename.get(deck.get("start_page")) if isinstance(deck.get("start_page"), str) else None
        lay["start_page"] = start or next(iter(rename.values()))
        layouts[lname] = lay
        detected.append({"model": model, "grid": [cols, nrows], "layout": lname})
        if lname != base:
            serial = deck.get("serial")
            if isinstance(serial, str) and _SERIAL.fullmatch(serial):
                devices[serial] = {"layout": lname}
            else:
                problems.add(f"Two decks have the same key grid; the second one's pages are in layout {lname}. "
                             "Choose that layout for the deck in the panel.")
    warn.extend(sorted(problems))
    if not layouts:
        raise ImportError_("No pages were found to import.")
    if len(decks) == 1:
        (lay,) = layouts.values()
        d = detected[0]
        return ({"pages": lay["pages"], "start_page": lay["start_page"]},
                {"source": source, "model": d["model"], "grid": d["grid"]})
    config = {"layouts": layouts}
    if devices:
        config["devices"] = devices
    return config, {"source": source, "decks": detected}
