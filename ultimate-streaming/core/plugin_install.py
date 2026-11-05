"""Installing and removing plugins (tools/plugin.py is the command line for it).

A plugin is installed from the repo's plugins/ catalog (a symlink into it, so `git pull` updates
it) or from a folder elsewhere (copied, after showing what it contains and asking). Every
installed plugin's requirements.txt goes into one shared, hash-pinned folder,
core.paths.plugin_vendor_dir(), rebuilt as a whole by tools/fetch_deps.py whenever the set of
plugins changes; a dependency that would shadow the standard library, a system package the app
imports or one of the app's own packages is refused.

The daemon loads whatever is in plugins_dir() and polls it, so the order matters: on install the
dependencies are in place before the plugin's entry appears (one os.replace from a hidden
.staging-<name>); on removal the entry disappears first (one rename) and the dependencies after.
Only one install / removal runs at a time (a lock file in data_dir()).
"""
import contextlib
import fcntl
import importlib.util
import os
import re
import shutil
import sys

from . import plugins
from .paths import ROOT, data_dir, plugin_vendor_dir, plugins_dir


def _load_fetch_deps():
    """tools/fetch_deps.py (tools/ isn't a package), as the one "fetch_deps" module of the process."""
    path = os.path.join(ROOT, "tools", "fetch_deps.py")
    mod = sys.modules.get("fetch_deps")
    if mod is not None and os.path.realpath(getattr(mod, "__file__", "") or "") == os.path.realpath(path):
        return mod
    spec = importlib.util.spec_from_file_location("fetch_deps", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fetch_deps"] = mod
    spec.loader.exec_module(mod)
    return mod


fetch_deps = _load_fetch_deps()
DepsError = fetch_deps.DepsError

# What the app imports from the system Python: a plugin dependency must not shadow these.
SYSTEM_IMPORTS = frozenset({"requests", "urllib3", "yaml", "PIL", "gi", "certifi", "idna",
                            "charset_normalizer", "chardet"})
STARTUP_HOOKS = frozenset({"sitecustomize", "usercustomize"})
APP_VENDOR = os.path.join(ROOT, "vendor")
REQUIREMENTS = "requirements.txt"
COPY_IGNORE = ("__pycache__", ".git", "vendor")
LIST_CAP = 200
WARNING = "A plugin runs with the same rights as the app, and can read your tokens."
RESTART = "systemctl --user restart ultimate-streaming"
URL = re.compile(r"^(?:[a-z][a-z0-9+.-]*://|git@|git\+)", re.IGNORECASE)


class InstallError(Exception):
    """An install or removal was refused or canceled; the message says why. Nothing was changed."""


@contextlib.contextmanager
def _locked():
    """One install / removal / rebuild at a time (they share plugin-vendor.new and .staging-*)."""
    fd = os.open(os.path.join(data_dir(), ".plugins.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstallError("another plugin install or removal is running; try again when it's done") from None
        yield
    finally:
        os.close(fd)


def _app_vendor_names():
    """Top-level importable names in the app's own vendor/."""
    try:
        entries = os.listdir(APP_VENDOR)
    except FileNotFoundError:
        return set()
    names = set()
    for entry in entries:
        if os.path.isdir(os.path.join(APP_VENDOR, entry)):
            if not entry.endswith((".dist-info", ".data")) and entry != "__pycache__":
                names.add(entry)
        elif entry.endswith(fetch_deps.MODULE_SUFFIXES):
            names.add(entry.split(".", 1)[0])
    return names


def check_top_levels(wheels, owner_by_name):
    """DepsError unless every wheel's top-level names are free: not the standard library, not a
    system package the app imports, not the app's own packages or vendor/, not another wheel's.
    owner_by_name: normalized project name -> the plugins that need it (for the message)."""
    app_vendor = _app_vendor_names()
    stdlib = set(sys.stdlib_module_names) | set(sys.builtin_module_names)
    provided = {}
    for wheel in wheels:
        owners = owner_by_name.get(fetch_deps.normalize(wheel["name"])) or []
        who = f"{wheel['name']} (needed by {', '.join(sorted(owners))})" if owners else wheel["name"]
        for top in sorted(wheel["top_level"]):
            if top in stdlib:
                clash = "a Python standard library module"
            elif top in SYSTEM_IMPORTS:
                clash = "a system package the app imports"
            elif top in plugins.RESERVED:
                clash = "part of the app itself"
            elif top in app_vendor:
                clash = "a package the app ships in vendor/"
            elif top in STARTUP_HOOKS:
                clash = "a Python startup hook"
            elif top in provided:
                clash = f"also provided by {provided[top]}"
            else:
                provided[top] = wheel["name"]
                continue
            raise DepsError(f"{who} provides {top!r}, which is {clash}; refusing to install it")


def _requirement_files(extra=None):
    """[(plugin name, requirements.txt path)] of every installed plugin with a valid manifest,
    plus the plugin folder `extra` (being installed)."""
    out = []
    for item in plugins.installed():
        if item["manifest"] is not None:
            out.append((item["name"], os.path.join(item["path"], REQUIREMENTS)))
    if extra is not None:
        out.append((plugins.read_manifest(extra)["name"], os.path.join(extra, REQUIREMENTS)))
    return [(name, path) for name, path in out if os.path.isfile(path)]


def _remove_vendor():
    target = plugin_vendor_dir()
    for path in (target + ".new", target + ".old", target):
        if os.path.islink(path):
            os.unlink(path)
        elif os.path.isdir(path):
            shutil.rmtree(path)


def _rebuild_deps(extra=None, out=print):
    files = _requirement_files(extra)
    owner_by_name = {}
    for plugin, path in files:
        for name, _version, _hashes in fetch_deps.parse(path):
            owner_by_name.setdefault(fetch_deps.normalize(name), []).append(plugin)
    if not owner_by_name:
        _remove_vendor()
        return []
    for key, owners in owner_by_name.items():
        if len(owners) > 1:
            try:
                fetch_deps.merge([path for plugin, path in files if plugin in owners])
            except DepsError as e:
                raise DepsError(f"plugins {', '.join(sorted(owners))} need conflicting dependencies: {e}") from None
    return fetch_deps.install([path for _plugin, path in files], plugin_vendor_dir(),
                              check=lambda wheels: check_top_levels(wheels, owner_by_name), log=out)


def rebuild_deps(extra=None, out=print):
    """Rebuild plugin_vendor_dir() from the requirements.txt of every installed plugin (and of the
    plugin folder `extra`, about to be installed). Removes it when nothing needs anything.
    DepsError (folder left as it was) if a requirement can't be fetched, conflicts or clashes."""
    with _locked():
        return _rebuild_deps(extra, out=out)


def _source_kind(source):
    """("catalog", catalog folder) or ("path", absolute folder) for what the user typed."""
    if not isinstance(source, str) or not source.strip():
        raise InstallError("say which plugin to install: a name from the catalog or a folder")
    if URL.match(source.strip()) or (source.rstrip("/").endswith(".git") and not os.path.isdir(source)):
        raise InstallError("installing from a git URL isn't supported yet; clone it and install the folder")
    if os.sep not in source and not source.startswith((".", "~")):
        for item in plugins.catalog():
            if item["name"] == source:
                return "catalog", item["path"]
    path = os.path.abspath(os.path.expanduser(source))
    if os.path.isdir(path):
        return "path", path
    raise InstallError(f"there's no plugin called {source!r} in the catalog, and no folder {path}")


def _walk(folder):
    """Every entry under folder (relative paths, sorted), skipping COPY_IGNORE folders.
    InstallError on a symlink or anything that isn't a plain file or folder."""
    found = []
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in COPY_IGNORE)
        for entry in dirnames + sorted(filenames):
            full = os.path.join(dirpath, entry)
            rel = os.path.relpath(full, folder)
            if os.path.islink(full):
                raise InstallError(f"{rel} in {folder} is a symlink; refusing to install a plugin containing links")
            if not (os.path.isdir(full) or os.path.isfile(full)):
                raise InstallError(f"{rel} in {folder} isn't a regular file or folder; refusing to install it")
            found.append(rel + os.sep if os.path.isdir(full) else rel)
    return sorted(found)


def _clear(path):
    """Remove a staging / removed entry of plugins_dir(): unlink a link or file, rmtree a real folder."""
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)


def _copy(source, staging):
    shutil.copytree(source, staging, symlinks=True, ignore=shutil.ignore_patterns(*COPY_IGNORE))
    _walk(staging)   # the copy, not just the source, must be free of links (the source may have changed)
    os.chmod(staging, 0o700)
    for dirpath, dirnames, filenames in os.walk(staging):
        for d in dirnames:
            os.chmod(os.path.join(dirpath, d), 0o700)
        for f in filenames:
            path = os.path.join(dirpath, f)
            os.chmod(path, (os.stat(path).st_mode & 0o700) | 0o600)


def install(source, *, yes=False, confirm=input, out=print):
    """Install a plugin from the catalog (by name) or from a folder (copied, after asking unless
    yes). Its dependencies are installed first; the plugin appears in plugins_dir() last, in one
    step. Returns its manifest. ManifestError / DepsError / InstallError / OSError when refused or
    failed, with nothing changed (except dependencies already rebuilt, if only the last step failed)."""
    kind, folder = _source_kind(source)
    manifest = plugins.read_manifest(folder, expect_name=os.path.basename(folder) if kind == "catalog" else None)
    name = manifest["name"]
    if kind == "catalog" and os.path.dirname(os.path.realpath(folder)) != os.path.realpath(plugins.catalog_dir()):
        raise InstallError(f"catalog entry {name!r} links outside the catalog; refusing to install it")
    if kind == "path":
        files = _walk(folder)
        out(f"Installing plugin {name} {manifest['version']} by copying {folder}:")
        for rel in files[:LIST_CAP]:
            out(f"  {rel}")
        if len(files) > LIST_CAP:
            out(f"  ... and {len(files) - LIST_CAP} more")
        out(WARNING)
        if not yes:
            try:
                answer = confirm("Install it? [y/N] ")
            except EOFError:
                answer = ""
            if (answer or "").strip().lower() not in ("y", "yes"):
                raise InstallError("not installed")

    with _locked():
        root = plugins_dir()
        final = os.path.join(root, name)
        if os.path.lexists(final):
            raise InstallError(f"a plugin called {name!r} is already installed; remove it first "
                               f"(tools/plugin.py remove {name})")
        staging = os.path.join(root, f".staging-{name}")
        try:
            _clear(staging)
            if kind == "catalog":
                os.symlink(os.path.realpath(folder), staging)
            else:
                _copy(folder, staging)
                plugins.read_manifest(staging, expect_name=name)   # what is actually installed
            _rebuild_deps(extra=staging, out=out)
            os.replace(staging, final)
        except BaseException:
            with contextlib.suppress(OSError):
                _clear(staging)
            raise
    return manifest


def remove(name, *, out=print):
    """Remove an installed plugin (a catalog link is unlinked, its target untouched; a copied
    folder is deleted), then rebuild the shared dependencies without its requirements."""
    plugins.validate_name(name)
    with _locked():
        root = os.path.realpath(plugins_dir())
        entry = os.path.join(root, name)
        if os.path.dirname(entry) != root or not os.path.lexists(entry):
            raise InstallError(f"no plugin called {name!r} is installed")
        gone = os.path.join(root, f".removing-{name}")
        _clear(gone)
        os.rename(entry, gone)          # disappears from the daemon's view in one step
        _clear(gone)                    # a link is unlinked; rmtree never follows a symlink
        out(f"Removed plugin {name}.")
        try:
            _rebuild_deps(out=out)
        except DepsError as e:
            raise DepsError(f"{name} was removed, but its dependencies couldn't be cleaned up: {e} "
                            f"(try again later with: tools/plugin.py deps)") from None
    out(f"Dependencies it had already imported stay loaded until the service restarts ({RESTART}).")


def listing():
    """Rows for `tools/plugin.py list`: every installed plugin, then catalog plugins not installed.
    Each: {"name", "version" (or None), "state": "linked" | "copied" | "available", "error" (or None)}."""
    rows = []
    for item in plugins.installed():
        manifest = item["manifest"] or {}
        rows.append({"name": item["name"], "version": manifest.get("version"),
                     "state": "linked" if item["link"] else "copied", "error": item["error"]})
    taken = {row["name"] for row in rows}
    for item in plugins.catalog():
        if item["name"] not in taken:
            manifest = item["manifest"] or {}
            rows.append({"name": item["name"], "version": manifest.get("version"),
                         "state": "available", "error": item["error"]})
    return rows
