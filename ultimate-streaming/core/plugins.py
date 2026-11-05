"""Plugins: optional, installable integrations that are not part of core.

A plugin is a folder:

    <name>/
      plugin.yaml        name, version, description, api (and optionally commands)
      __init__.py        defines setup(bot), exactly like a modules/*.py file
      requirements.txt   optional; hash-pinned like the app's own (tools/fetch_deps.py)
      *.py               more files, imported relatively (from .printer import ...)
      README.md          optional

plugin.yaml:

    name: bambu                      # ^[a-z][a-z0-9_]{1,31}$, not the name of a core module
    version: "1.0.0"
    description: Bambu Lab printer status, camera and controls
    api: 1                           # must equal core.plugins.API
    commands:                        # optional: programs it uses, for tools/doctor.py
      ffmpeg: printer camera (version 7 or newer)

Installed plugins live in core.paths.plugins_dir() ($XDG_DATA_HOME/ultimate-streaming/plugins),
one entry per plugin named after it: either a symlink to a folder of the repo's plugins/ catalog
(so `git pull` updates it) or a copied folder (a plugin from elsewhere). Their dependencies share
one folder, core.paths.plugin_vendor_dir(). tools/plugin.py installs and removes them
(core/plugin_install.py); the daemon loads them (core/bot.py, load_plugins) and notices
installs and removals on its own.

This file is the read side, shared by the loader, the installer and tools/doctor.py.
It imports no plugin code.
"""
import os
import re

from .paths import ROOT, plugins_dir

API = 1
NAME_RE = re.compile(r"[a-z][a-z0-9_]{1,31}\Z")
MANIFEST = "plugin.yaml"
MANIFEST_MAX_BYTES = 16 * 1024
PACKAGE = "deck_plugins"          # plugins import as deck_plugins.<name>
KEYS = {"name", "version", "description", "api", "commands"}
# Names a plugin may not take: they would collide with the app's own packages or config sections.
RESERVED = frozenset({"core", "modules", "tools", "ui", "obs", "plugins", "editor", "main", "vendor",
                      "tests", "version", "layouts", "pages", "keys", "brightness", "start_page",
                      PACKAGE})


class ManifestError(ValueError):
    """plugin.yaml is missing, unreadable or invalid; the message says why, in words."""


def catalog_dir():
    """The repo's plugins/ folder: plugins that ship with the app but are not installed."""
    return os.path.join(ROOT, "plugins")


def core_module_names():
    """Stems of modules/*.py (their config sections and names are taken)."""
    folder = os.path.join(ROOT, "modules")
    try:
        return {f[:-3] for f in os.listdir(folder) if f.endswith(".py") and not f.startswith("_")}
    except FileNotFoundError:
        return set()


def validate_name(name):
    """name if it is a valid, free plugin name, else ManifestError."""
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ManifestError("the plugin name must be 2-32 characters: lowercase letters, digits and _, "
                            "starting with a letter")
    if name in RESERVED or name in core_module_names():
        raise ManifestError(f"{name!r} is already used by the app itself; choose another plugin name")
    return name


def read_manifest(folder, expect_name=None):
    """The validated plugin.yaml of a plugin folder as
    {"name", "version", "description", "api", "commands": {cmd: purpose}}.
    expect_name: the name the folder is installed under (must match). ManifestError otherwise."""
    from .configio import safe_load_bounded   # yaml is imported lazily (tools run without it)
    path = os.path.join(folder, MANIFEST)
    try:
        with open(path, "rb") as f:
            raw = f.read(MANIFEST_MAX_BYTES + 1)
    except FileNotFoundError:
        raise ManifestError(f"no {MANIFEST} in {folder}") from None
    except OSError as e:
        raise ManifestError(f"can't read {path}: {e.strerror or e}") from None
    if len(raw) > MANIFEST_MAX_BYTES:
        raise ManifestError(f"{MANIFEST} is larger than {MANIFEST_MAX_BYTES // 1024} KB")
    if not os.path.isfile(os.path.join(folder, "__init__.py")):
        raise ManifestError(f"no __init__.py in {folder}")
    try:
        data = safe_load_bounded(raw, max_nodes=200)
    except Exception as e:
        raise ManifestError(f"{MANIFEST} isn't valid YAML: {str(e)[:200]}") from None
    if not isinstance(data, dict):
        raise ManifestError(f"{MANIFEST} must be a mapping (name: ..., version: ...)")
    unknown = sorted(str(k) for k in data if k not in KEYS)
    if unknown:
        raise ManifestError(f"{MANIFEST} has unknown keys: {', '.join(unknown)}")
    name = validate_name(data.get("name"))
    if expect_name is not None and name != expect_name:
        raise ManifestError(f"installed as {expect_name!r} but {MANIFEST} says {name!r}")
    out = {"name": name}
    for key, cap in (("version", 32), ("description", 200)):
        value = data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and key == "version":
            value = str(value)
        if not isinstance(value, str) or not value.strip() or len(value) > cap or "\n" in value:
            raise ManifestError(f"{MANIFEST}: {key} must be one line of text (at most {cap} characters)")
        out[key] = value.strip()
    api = data.get("api")
    if isinstance(api, bool) or not isinstance(api, int):
        raise ManifestError(f"{MANIFEST}: api must be a whole number")
    if api != API:
        raise ManifestError(f"this plugin is written for plugin API {api}, but this app provides API {API}")
    out["api"] = api
    commands = data.get("commands") or {}
    if not isinstance(commands, dict) or len(commands) > 20 or not all(
            isinstance(k, str) and re.fullmatch(r"[A-Za-z0-9._+-]{1,64}", k)
            and isinstance(v, str) and 0 < len(v) <= 200 for k, v in commands.items()):
        raise ManifestError(f"{MANIFEST}: commands must map program names to what they're for")
    out["commands"] = dict(commands)
    return out


def _resolve(entry_path):
    """(real folder, is_link) for an entry of plugins_dir(), or ManifestError if a symlink
    points anywhere but straight into the catalog."""
    if os.path.islink(entry_path):
        real = os.path.realpath(entry_path)
        catalog = os.path.realpath(catalog_dir())
        if os.path.dirname(real) != catalog:
            raise ManifestError("this plugin is a link to somewhere other than the app's plugins/ catalog")
        if not os.path.isdir(real):
            raise ManifestError("this plugin links to a catalog folder that no longer exists")
        return real, True
    if not os.path.isdir(entry_path):
        raise ManifestError("not a folder")
    return os.path.realpath(entry_path), False


def installed():
    """Every installed plugin, sorted by name, as
    {"name", "path" (real folder), "link" (True for a catalog symlink),
     "manifest" (read_manifest result, or None), "error" (str, or None)}.
    Entries starting with "." or "_" (staging, backups) are skipped."""
    root = plugins_dir()
    out = []
    for entry in sorted(os.listdir(root)):
        if entry.startswith((".", "_")):
            continue
        item = {"name": entry, "path": os.path.join(root, entry), "link": False, "manifest": None, "error": None}
        try:
            validate_name(entry)
            item["path"], item["link"] = _resolve(os.path.join(root, entry))
            item["manifest"] = read_manifest(item["path"], expect_name=entry)
        except ManifestError as e:
            item["error"] = str(e)
        out.append(item)
    return out


def catalog():
    """Plugins shipped in the repo's plugins/ folder, as {"name", "path", "manifest", "error"}."""
    root = catalog_dir()
    out = []
    try:
        entries = sorted(os.listdir(root))
    except FileNotFoundError:
        return out
    for entry in entries:
        path = os.path.join(root, entry)
        if entry.startswith((".", "_")) or not os.path.isdir(path):
            continue
        item = {"name": entry, "path": path, "manifest": None, "error": None}
        try:
            item["manifest"] = read_manifest(path, expect_name=entry)
        except ManifestError as e:
            item["error"] = str(e)
        out.append(item)
    return out
