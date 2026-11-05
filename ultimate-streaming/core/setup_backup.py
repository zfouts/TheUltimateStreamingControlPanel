"""Private archives of other programs' setups (OBS, Stream Deck apps), taken before onboarding
changes anything, and a staged, verified, rollback-safe restore.

    $XDG_STATE_HOME/ultimate-streaming/backups/external/            (0700)
        <stamp>-<slug>.tar.gz   members <kind>/<rel> + manifest.json   (0600)
        <stamp>-<slug>.json     sidecar {id, ts, label, auto, pinned, size, hash, items}   (0600)

manifest.json = {ts, label, auto, items: [{kind, root, files: [{rel, sha256, size}]}]}; `root` is
informational only and never used as a restore target.

The archives hold the OBS stream key, the obs-websocket password and browser-source tokens, so
the whole archive is a secret: it lives in the state dir (not ~/.config, which is often synced),
is never part of the daily bundles and is never offered for download. Listings and logs carry
ids, labels, counts and rel names only.
"""
import configparser
import datetime
import hashlib
import json
import logging
import os
import posixpath
import re
import secrets
import shutil
import stat
import tarfile
import tempfile
import threading
import time
import zlib
from pathlib import PurePosixPath

from .safefs import NotRegular, TooBig, inside, read_regular, state_home, walk_regular

log = logging.getLogger("deck")

KINDS = ("obs", "streamcontroller", "streamdeck_ui", "boatswain")
OBS_INCLUDE = ["basic/profiles/**", "basic/scenes/*.json", "basic/scenes/*.json.bak",
               "plugin_config/obs-websocket/config.json"]
OBS_NEVER = ("plugin_config", "obs-browser")   # CEF cache + logged-in cookies: never archived

MAX_FILE = 50 * 1024 * 1024
MAX_FILES = 20000
MAX_MANIFEST = 16 * 1024 * 1024
MAX_INI = 1024 * 1024
KEEP_AUTO = 10
KEEP_MANUAL = 10
KEEP_PRE_RESTORE = 10   # restore's own backups: their own group, so add-on clicks never push them out
PRE_RESTORE_LABEL = "before-restore"
TOTAL_CAP = 3 * 1024 ** 3
STALE_SECONDS = 3600        # leftovers of an interrupted backup/restore older than this are removed
JOURNAL = "restore-journal.json"
FREE_MARGIN = 1024 ** 3      # refuse unless free space >= 2 x estimate + this

STAMP = "%Y%m%d-%H%M%S-%f"
ID_RE = re.compile(r"^(\d{8}-\d{6}-\d{6})-([a-z0-9-]{1,40})$")
STAGE_PREFIX = ".ultimate-streaming-restore-"
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩]")
_MAGIC = re.compile(r"[*?\[]")

LOCK = threading.RLock()   # one create/restore/prune/delete at a time
_held = set()              # archive ids in use by a restore: never pruned or deleted meanwhile


class BackupError(Exception):
    """A backup or restore was refused or failed. The message is safe to show (no paths, no contents)."""


class RestoreTargetMismatch(BackupError):
    """The archive was taken from a different folder than the one a restore would write into.
    `archive_root` and `target_root` are the two (real) paths, for the user to confirm."""

    def __init__(self, archive_root, target_root):
        super().__init__("this backup came from a different folder than the one in use")
        self.archive_root, self.target_root = archive_root, target_root


class NotFound(BackupError):
    """No archive with that id."""


# ------------------------------------------------------------------ helpers

def _dir():
    d = os.path.join(state_home(), "backups", "external")
    for p in (os.path.dirname(d), d):
        os.makedirs(p, mode=0o700, exist_ok=True)
        os.chmod(p, 0o700)
    return d


def _clean(text, limit=64):
    text = _CTRL.sub("", str(text)).strip()
    return text[:limit]


def _slug(label):
    s = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40].strip("-")
    return s or "backup"


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _check_id(archive_id):
    if not isinstance(archive_id, str) or not ID_RE.fullmatch(archive_id):
        raise ValueError("invalid backup id")
    return archive_id


def _paths(archive_id):
    base = os.path.join(_dir(), archive_id)
    return base + ".tar.gz", base + ".json"


def _fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _excl_tmp(directory, tag):
    """(fd, path) of a new 0600 file created with O_EXCL next to its final name."""
    while True:
        path = os.path.join(directory, f".tmp-{tag}-{secrets.token_hex(6)}")
        try:
            return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600), path
        except FileExistsError:
            continue


def _write_private(path, data):
    fd, tmp = _excl_tmp(os.path.dirname(path), "json")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, path)
    except BaseException:
        _unlink(tmp)
        raise


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _manifest_hash(manifest):
    """Hash of every (kind, rel, sha256): equal hashes mean the same files with the same bytes."""
    rows = sorted((it["kind"], f["rel"], f["sha256"]) for it in manifest["items"] for f in it["files"])
    return _sha(json.dumps(rows, ensure_ascii=True).encode())


def _check_rel(rel):
    """A safe relative POSIX path: not absolute, no '..', '.', empty parts or NUL."""
    if (not isinstance(rel, str) or not rel or "\x00" in rel or rel.startswith("/")
            or posixpath.normpath(rel) != rel or any(p in ("", ".", "..") for p in rel.split("/"))):
        raise BackupError("archive contains an unsafe file name")
    return rel


# ------------------------------------------------------------------ collecting files

def _obs_redirects(root, notes):
    """{"basic/profiles": rel or None, "basic/scenes": rel or None} from global.ini [Locations].

    OBS stores profiles under <Profiles>/obs-studio/basic/profiles and collections under
    <SceneCollections>/obs-studio/basic/scenes, both defaulting to the parent of the config root.
    A location that resolves inside root maps to its rel there; one outside root is not archived
    (None, with a note), since a restore only ever writes under the item's root."""
    out = {"basic/profiles": "basic/profiles", "basic/scenes": "basic/scenes"}
    try:
        data, _ = read_regular(os.path.join(root, "global.ini"), MAX_INI)
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        cp.read_string(data.decode("utf-8-sig", "replace"))
    except (OSError, ValueError, configparser.Error):
        return out
    for key, sub in (("profiles", "basic/profiles"), ("scenecollections", "basic/scenes")):
        loc = cp.get("Locations", key, fallback="").strip()
        if not loc:
            continue
        if not os.path.isabs(loc):
            notes.append(f"{sub}: [Locations] setting is not an absolute path, using the default")
            continue
        real = os.path.realpath(os.path.join(loc, "obs-studio", *sub.split("/")))
        if inside(real, root) and real != root:
            out[sub] = os.path.relpath(real, root).replace(os.sep, "/")
        else:
            out[sub] = None
            notes.append(f"{sub}: stored outside the OBS folder ([Locations]), not backed up")
    return out


def _patterns(item, notes):
    """The item's include patterns, validated, with OBS [Locations] applied. None = everything."""
    include = item.get("include")
    if include is None:
        return None
    pats = []
    for p in include:
        if not isinstance(p, str) or not p or p.startswith("/") or ".." in p.split("/") or "\x00" in p:
            raise ValueError("invalid include pattern")
        pats.append(p)
    if item["kind"] == "obs":
        redirects = _obs_redirects(item["root"], notes)
        moved = []
        for p in pats:
            for sub, new in redirects.items():
                if p.startswith(sub + "/"):
                    p = None if new is None else new + p[len(sub):]
                    break
            if p is not None:
                moved.append(p)
        pats = moved
    return pats


def _match(rel, pattern):
    return PurePosixPath(rel).full_match(pattern)


def _excluded(kind, rel):
    parts = rel.split("/")
    if parts[0].startswith(STAGE_PREFIX):
        return True
    return kind == "obs" and tuple(parts[:2]) == OBS_NEVER


def _read_literal(root, rel, notes):
    """One named file under root with the walk's rules, or None (missing or skipped with a note)."""
    path = os.path.join(root, *rel.split("/"))
    if not os.path.lexists(path):
        return None
    if not inside(path, root):
        notes.append(f"{rel}: symlink outside the folder, skipped")
        return None
    try:
        return read_regular(path, MAX_FILE, follow=os.path.islink(path))
    except TooBig:
        notes.append(f"{rel}: larger than {MAX_FILE // (1024 * 1024)} MB, skipped")
    except (NotRegular, OSError):
        notes.append(f"{rel}: not a regular file, skipped")
    return None


def _scopes(patterns):
    """{static dir prefix: [patterns]} (prefix "" = root) and the literal (glob-free) file names."""
    scopes, literals = {}, []
    for p in patterns:
        parts = p.split("/")
        static = []
        for part in parts:
            if _MAGIC.search(part):
                break
            static.append(part)
        if len(static) == len(parts):
            literals.append(p)
        else:
            scopes.setdefault("/".join(static), []).append(p)
    return scopes, literals


def _iter_files(item, notes, exact=None):
    """Yield (rel, bytes, stat) for an item's files. exact: a list of rel names (restore's
    pre-backup) read one by one instead of matching patterns."""
    root, kind = item["root"], item["kind"]
    seen = set()

    def ok(rel):
        if rel in seen or _excluded(kind, rel):
            return False
        try:
            rel.encode("utf-8")
        except UnicodeEncodeError:
            notes.append("a file whose name is not valid UTF-8 was skipped")
            return False
        seen.add(rel)
        return True

    if exact is not None:
        for rel in exact:
            got = _read_literal(root, rel, notes)
            if got is not None and ok(rel):
                yield rel, *got
        return
    patterns = _patterns(item, notes)
    if patterns is None:
        scopes, literals = {"": None}, []
    else:
        scopes, literals = _scopes(patterns)
    for rel in literals:
        got = _read_literal(root, rel, notes)
        if got is not None and ok(rel):
            yield rel, *got
    for prefix, pats in sorted(scopes.items()):
        sub = os.path.join(root, *prefix.split("/")) if prefix else root
        if not os.path.isdir(sub):
            continue
        if not inside(sub, root):
            notes.append(f"{prefix}: symlink outside the folder, skipped")
            continue
        sub_notes = []
        for r, data, st in walk_regular(sub, max_file=MAX_FILE, max_files=MAX_FILES, notes=sub_notes):
            rel = posixpath.join(prefix, r.replace(os.sep, "/")) if prefix else r.replace(os.sep, "/")
            if (pats is None or any(_match(rel, p) for p in pats)) and ok(rel):
                yield rel, data, st
        for n in sub_notes:   # only notes about files we would have included
            r, sep, _ = n.rpartition(": ")
            if not sep:
                notes.append(n)
                continue
            rel = posixpath.join(prefix, r) if prefix else r
            if not _excluded(kind, rel) and (pats is None or any(_match(rel, p) for p in pats)):
                notes.append(posixpath.join(prefix, n) if prefix else n)


def _estimate(items):
    """Rough uncompressed size of what create() would read (stat only, never reads files)."""
    total = 0
    for item in items:
        root = item["root"]
        if item.get("_exact") is not None:
            dirs, files = [], item["_exact"]
        else:
            pats = item.get("include")
            if pats is None:
                dirs, files = [""], []
            else:
                scopes, files = _scopes(pats)
                dirs = list(scopes)
        for rel in files:
            try:
                st = os.stat(os.path.join(root, *rel.split("/")))
                if stat.S_ISREG(st.st_mode) and st.st_size <= MAX_FILE:
                    total += st.st_size
            except OSError:
                pass
        for prefix in dirs:
            top = os.path.join(root, *prefix.split("/")) if prefix else root
            for dirpath, _, names in os.walk(top, followlinks=False):
                for name in names:
                    try:
                        st = os.lstat(os.path.join(dirpath, name))
                    except OSError:
                        continue
                    if stat.S_ISREG(st.st_mode) and st.st_size <= MAX_FILE:
                        total += st.st_size
    return total


# ------------------------------------------------------------------ sidecars / listing

def _ids():
    """Archive ids on disk, oldest first."""
    out = []
    for name in os.listdir(_dir()):
        if name.endswith(".tar.gz") and ID_RE.fullmatch(name[:-7]):
            out.append(name[:-7])
    return sorted(out, key=lambda i: (i[:22], i))


def _rebuild_meta(archive_id):
    """Sidecar contents rebuilt from the archive's manifest (sidecar missing or unreadable)."""
    with tarfile.open(_paths(archive_id)[0], "r:gz") as tf:
        manifest = _read_manifest(tf)
    return {"id": archive_id, "ts": manifest["ts"], "label": _clean(manifest.get("label") or ""),
            "auto": bool(manifest.get("auto")), "pinned": False, "hash": _manifest_hash(manifest),
            "pre_restore": bool(manifest.get("auto")) and manifest.get("label") == PRE_RESTORE_LABEL,
            "items": [{"kind": it["kind"], "label": it["kind"], "files": len(it["files"]), "notes": []}
                      for it in manifest["items"]]}


def _meta(archive_id):
    arc, side = _paths(archive_id)
    if not os.path.exists(arc):
        raise NotFound("no such backup")
    try:
        data, _ = read_regular(side, MAX_MANIFEST)
        meta = json.loads(data)
        if not isinstance(meta, dict) or meta.get("id") != archive_id:
            raise ValueError
    except (OSError, ValueError):
        meta = _rebuild_meta(archive_id)
    meta["size"] = os.stat(arc).st_size
    return meta


def _public(meta):
    return {"id": meta["id"], "ts": meta["ts"], "label": meta["label"], "auto": bool(meta["auto"]),
            "pinned": bool(meta["pinned"]), "size": meta["size"],
            "items": [{"kind": it["kind"], "label": it["label"], "files": it["files"],
                       "notes": list(it.get("notes") or [])} for it in meta["items"]]}


def _save_meta(meta):
    side = {k: meta[k] for k in ("id", "ts", "label", "auto", "pinned", "hash", "items")}
    side["pre_restore"] = bool(meta.get("pre_restore"))   # older sidecars may not have it
    _write_private(_paths(meta["id"])[1], json.dumps(side, indent=1, sort_keys=True).encode())


def _all_meta():
    out = []
    for aid in _ids():
        try:
            out.append(_meta(aid))
        except (OSError, ValueError, tarfile.TarError, BackupError, KeyError, TypeError):
            log.warning("external backup %s is unreadable, skipped in the listing", aid)
    return out


def list_archives():
    """Every archive's listing entry, newest first."""
    with LOCK:
        return [_public(m) for m in reversed(_all_meta())]


# ------------------------------------------------------------------ create / retention

def _validate_items(items):
    if not isinstance(items, (list, tuple)) or not items:
        raise ValueError("nothing to back up")
    out, kinds = [], set()
    for it in items:
        if not isinstance(it, dict) or it.get("kind") not in KINDS:
            raise ValueError("unknown backup item kind")
        if it["kind"] in kinds:
            raise ValueError("each kind can only be backed up once per archive")
        kinds.add(it["kind"])
        root = it.get("root")
        if not isinstance(root, str) or not os.path.isabs(root):
            raise ValueError("backup item root must be an absolute path")
        include = it.get("include")
        if include is not None and (not isinstance(include, (list, tuple))
                                    or not all(isinstance(p, str) for p in include)):
            raise ValueError("invalid include patterns")
        out.append({"kind": it["kind"], "root": os.path.realpath(root),
                    "include": None if include is None else list(include),
                    "label": _clean(it.get("label") or it["kind"]),
                    "_exact": it.get("_exact")})
    return out


def _new_id(slug):
    ids = _ids()
    now = datetime.datetime.now()
    newest = ids[-1][:22] if ids else None
    stamp = now.strftime(STAMP)
    if newest and stamp <= newest:   # same microsecond or the clock went back: stay ordered
        stamp = (datetime.datetime.strptime(newest, STAMP)
                 + datetime.timedelta(microseconds=1)).strftime(STAMP)
    return f"{stamp}-{slug}"


def create(items, *, label, auto=False, pinned=False):
    """Archive the items' files. Returns the new (or, for an unchanged auto backup, the
    previous) archive's listing entry. Raises BackupError when there is not enough space."""
    return _create(items, label=label, auto=auto, pinned=pinned)[0]


def _group(meta):
    """Retention group: "pre_restore" (restore's own backups), "auto" or "manual"."""
    if meta.get("pre_restore"):
        return "pre_restore"
    return "auto" if meta["auto"] else "manual"


def _create(items, *, label, auto=False, pinned=False, pre_restore=False):
    """create(), also returning {kind: {rel: sha256}} of what the archive holds.
    pre_restore=True files the archive in restore's own retention group (implies auto)."""
    auto = auto or pre_restore
    if not isinstance(label, str):
        raise ValueError("label must be text")
    label = _clean(label) or "backup"
    items = _validate_items(items)
    with LOCK:
        directory = _dir()
        estimate = _estimate(items)
        free = shutil.disk_usage(directory).free
        if free < 2 * estimate + FREE_MARGIN:
            raise BackupError(f"not enough free disk space for a backup "
                              f"(need {(2 * estimate + FREE_MARGIN) // 1024 ** 2} MB free)")
        archive_id = _new_id(_slug(label))
        ts = time.time()
        manifest = {"ts": ts, "label": label, "auto": bool(auto), "items": []}
        listing = []
        fd, tmp = _excl_tmp(directory, archive_id)
        try:
            with os.fdopen(fd, "wb") as raw:
                with tarfile.open(fileobj=raw, mode="w:gz", format=tarfile.PAX_FORMAT) as tf:
                    for item in items:
                        notes, files = [], []
                        if not os.path.isdir(item["root"]):
                            notes.append("folder not found")
                        else:
                            for rel, data, st in _iter_files(item, notes, exact=item["_exact"]):
                                info = tarfile.TarInfo(f"{item['kind']}/{rel}")
                                info.size = len(data)
                                info.mode = stat.S_IMODE(st.st_mode) & 0o777
                                info.mtime = int(st.st_mtime)
                                info.type = tarfile.REGTYPE
                                tf.addfile(info, _Bytes(data))
                                files.append({"rel": rel, "sha256": _sha(data), "size": len(data)})
                        manifest["items"].append({"kind": item["kind"], "root": item["root"], "files": files})
                        listing.append({"kind": item["kind"], "label": item["label"],
                                        "files": len(files), "notes": notes})
                    body = json.dumps(manifest, indent=1, sort_keys=True).encode()
                    info = tarfile.TarInfo("manifest.json")
                    info.size, info.mode, info.mtime = len(body), 0o600, int(ts)
                    tf.addfile(info, _Bytes(body))
                raw.flush()
                os.fsync(raw.fileno())
            digest = _manifest_hash(manifest)
            contents = {it["kind"]: {f["rel"]: f["sha256"] for f in it["files"]} for it in manifest["items"]}
            if auto and not pinned:
                group = "pre_restore" if pre_restore else "auto"   # dedup only within the group
                prev = next((m for m in reversed(_all_meta()) if _group(m) == group), None)
                if prev is not None and prev.get("hash") == digest:
                    _unlink(tmp)
                    log.info("external backup %r unchanged since %s, not taken again", label, prev["id"])
                    return _public(prev), contents
            meta = {"id": archive_id, "ts": ts, "label": label, "auto": bool(auto), "pinned": bool(pinned),
                    "hash": digest, "items": listing, "pre_restore": bool(pre_restore)}
            arc, side = _paths(archive_id)
            _save_meta(meta)     # sidecar first: an archive never appears without its pinned state
            try:
                os.rename(tmp, arc)
            except BaseException:
                _unlink(side)
                raise
            _fsync_dir(directory)
        except BaseException:
            _unlink(tmp)
            raise
        meta["size"] = os.stat(arc).st_size
        log.info("external backup %s (%r) created: %s", archive_id, label,
                 ", ".join(f"{it['kind']} {it['files']} files" for it in listing))
        try:
            prune(keep=archive_id)
        except OSError:
            log.warning("pruning external backups failed")
        return _public(meta), contents


class _Bytes:
    """A minimal file object over bytes for TarFile.addfile (no copy of the buffer)."""

    def __init__(self, data):
        self._view, self._pos = memoryview(data), 0

    def read(self, n=-1):
        end = len(self._view) if n is None or n < 0 else min(len(self._view), self._pos + n)
        chunk = bytes(self._view[self._pos:end])
        self._pos = end
        return chunk


def prune(keep=None):
    """Retention, per group of unpinned archives: auto keeps KEEP_AUTO, manual KEEP_MANUAL and
    pre-restore KEEP_PRE_RESTORE; no group's count pushes out another's. Then, while the total is
    over TOTAL_CAP, the oldest go: auto first, then manual, and pre-restore archives (the only
    copy of what a restore overwrote) last. Pinned archives, archives in use by a restore and
    `keep` (the one just created) are never removed. Returns the removed ids."""
    with LOCK:
        _clean_stale_tmp()
        metas = _all_meta()   # oldest first
        removable = [m for m in metas if not m["pinned"] and m["id"] not in _held and m["id"] != keep]
        drop = set()
        for group, limit in (("auto", KEEP_AUTO), ("manual", KEEP_MANUAL), ("pre_restore", KEEP_PRE_RESTORE)):
            excess = len([m for m in metas if _group(m) == group and not m["pinned"]]) - limit
            for m in removable:
                if excess <= 0:
                    break
                if _group(m) == group:
                    drop.add(m["id"])
                    excess -= 1
        total = sum(m["size"] for m in metas if m["id"] not in drop)
        for group in ("auto", "manual", "pre_restore"):
            for m in removable:
                if total <= TOTAL_CAP:
                    break
                if _group(m) == group and m["id"] not in drop:
                    drop.add(m["id"])
                    total -= m["size"]
        for aid in sorted(drop):
            _remove(aid)
            log.info("external backup %s pruned", aid)
        return sorted(drop)


def _clean_stale_tmp():
    """Remove .tmp-* files in the backup folder that a killed create left behind (they can hold
    stream keys and are counted nowhere)."""
    d = _dir()
    for name in os.listdir(d):
        if not name.startswith(".tmp-"):
            continue
        p = os.path.join(d, name)
        try:
            if time.time() - os.lstat(p).st_mtime > STALE_SECONDS:
                if os.path.isdir(p) and not os.path.islink(p):
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    os.unlink(p)
                log.info("removed stale temp file %s from the backup folder", name)
        except OSError:
            pass


def _clean_stale_stages(root):
    """Remove restore staging folders in `root` that a killed restore left behind."""
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        if not name.startswith(STAGE_PREFIX):
            continue
        p = os.path.join(root, name)
        try:
            st = os.lstat(p)
            if stat.S_ISDIR(st.st_mode) and time.time() - st.st_mtime > STALE_SECONDS:
                shutil.rmtree(p, ignore_errors=True)
                log.info("removed a stale restore staging folder")
        except OSError:
            pass


def _journal_path():
    return os.path.join(_dir(), JOURNAL)


def _write_journal(archive_id, pre_id, root):
    _write_private(_journal_path(), json.dumps(
        {"archive": archive_id, "before": pre_id, "target": root, "started": time.time()}).encode())


def _clear_journal():
    _unlink(_journal_path())


def interrupted_restore():
    """The journal of a restore that was killed while replacing files, as
    {"archive", "before", "target", "started"}, or None. `before` is the before-restore archive
    that holds the files it overwrote."""
    try:
        data, _ = read_regular(_journal_path(), 64 * 1024)
        j = json.loads(data)
    except (OSError, ValueError, NotRegular, TooBig):
        return None
    if not isinstance(j, dict) or not all(k in j for k in ("archive", "before", "target", "started")):
        return None
    return {k: j[k] for k in ("archive", "before", "target", "started")}


def _remove(archive_id):
    arc, side = _paths(archive_id)
    os.unlink(arc)
    _unlink(side)


def delete(archive_id):
    """Remove an archive. Refuses a pinned one (unpin it first) or one in use by a restore."""
    _check_id(archive_id)
    with LOCK:
        meta = _meta(archive_id)
        if meta["pinned"]:
            raise BackupError("this backup is pinned; unpin it before deleting")
        if archive_id in _held:
            raise BackupError("this backup is in use by a restore")
        _remove(archive_id)
        log.info("external backup %s deleted", archive_id)


def pin(archive_id, pinned=True):
    """Pin (never pruned, never deleted) or unpin an archive. Returns its listing entry."""
    _check_id(archive_id)
    with LOCK:
        meta = _meta(archive_id)
        meta["pinned"] = bool(pinned)
        _save_meta(meta)
        log.info("external backup %s %s", archive_id, "pinned" if pinned else "unpinned")
        return _public(meta)


# ------------------------------------------------------------------ reading archives

def _read_member(tf, member, limit):
    if not member.isreg():
        raise BackupError("archive contains something that is not a regular file")
    if member.size > limit:
        raise BackupError("archive contains a file that is too large")
    f = tf.extractfile(member)
    if f is None:
        raise BackupError("archive contains something that is not a regular file")
    data = f.read(limit + 1)
    if len(data) > limit or len(data) != member.size:
        raise BackupError("archive contains a file of the wrong size")
    return data


def _read_manifest(tf):
    try:
        member = tf.getmember("manifest.json")
    except KeyError:
        raise BackupError("archive has no manifest") from None
    try:
        manifest = json.loads(_read_member(tf, member, MAX_MANIFEST))
        if not isinstance(manifest, dict) or not isinstance(manifest.get("items"), list):
            raise ValueError
        for it in manifest["items"]:
            if not isinstance(it, dict) or it.get("kind") not in KINDS or not isinstance(it.get("files"), list):
                raise ValueError
            for f in it["files"]:
                if (not isinstance(f, dict) or not isinstance(f.get("sha256"), str)
                        or not isinstance(f.get("size"), int)):
                    raise ValueError
                _check_rel(f.get("rel"))
    except (ValueError, RecursionError):
        raise BackupError("archive manifest is damaged") from None
    return manifest


_DAMAGED = (tarfile.TarError, EOFError, zlib.error, OSError)


class _Archive:
    """An opened archive: its manifest and members by name (duplicates refused)."""

    def __init__(self, archive_id):
        arc = _paths(archive_id)[0]
        if not os.path.exists(arc):
            raise NotFound("no such backup")
        try:
            self.tf = tarfile.open(arc, "r:gz")
        except _DAMAGED:
            raise BackupError("archive is damaged") from None
        try:
            self.members = {}
            for m in self.tf.getmembers():
                if m.name in self.members:
                    raise BackupError("archive contains duplicate entries")
                self.members[m.name] = m
            self.manifest = _read_manifest(self.tf)
        except _DAMAGED:
            self.tf.close()
            raise BackupError("archive is damaged") from None
        except BaseException:
            self.tf.close()
            raise

    def item(self, kind):
        found = [it for it in self.manifest["items"] if it["kind"] == kind]
        if len(found) != 1:
            raise BackupError(f"this backup has no {kind} part" if not found else "archive manifest is damaged")
        rels = [f["rel"] for f in found[0]["files"]]
        if len(set(rels)) != len(rels):
            raise BackupError("archive manifest is damaged")
        return found[0]

    def files(self, kind, entries):
        """Yield (entry, bytes, mode) for manifest entries, sha256-verified, in archive order."""
        rows = []
        for e in entries:
            m = self.members.get(f"{kind}/{e['rel']}")
            if m is None:
                raise BackupError("archive is missing a file listed in its manifest")
            rows.append((m.offset, e, m))
        for _, e, m in sorted(rows, key=lambda r: r[0]):
            try:
                data = _read_member(self.tf, m, MAX_FILE)
            except _DAMAGED:
                raise BackupError("archive is damaged") from None
            if len(data) != e["size"] or _sha(data) != e["sha256"]:
                raise BackupError("archive is damaged: a file does not match its checksum")
            yield e, data, m.mode & 0o777

    def close(self):
        self.tf.close()


# ------------------------------------------------------------------ restore

def _target(root, rel):
    """(logical path, real destination) for rel under root; refused if it leaves root."""
    path = os.path.join(root, *rel.split("/"))
    dest = os.path.realpath(path)
    if not inside(dest, root) or dest == root:
        raise BackupError("a file in the backup would be written outside the folder")
    return path, dest


def _write_new(directory, data, mode):
    fd, tmp = _excl_tmp(directory, "restore")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
    except BaseException:
        _unlink(tmp)
        raise
    return tmp


def _makedirs(path, root, created):
    """Create path's missing directories (0700), appending each new one to created."""
    missing = []
    p = path
    while not os.path.isdir(p):
        if os.path.lexists(p):
            raise BackupError("a folder in the backup is a file in the target")
        missing.append(p)
        p = os.path.dirname(p)
    for d in reversed(missing):
        os.mkdir(d, 0o700)
        created.append(d)
        if not inside(d, root):
            raise BackupError("a file in the backup would be written outside the folder")


def _rollback(pre_id, kind, done, existed, staged_sha):
    """Put back what the renames changed: old bytes from the pre-restore backup for files that
    existed, removal (if still ours) for files that did not. Returns the number of failures."""
    failures = 0
    old = {}
    try:
        arc = _Archive(pre_id)
        try:
            item = arc.item(kind)
            wanted = [e for e in item["files"] if e["rel"] in existed]
            for e, data, mode in arc.files(kind, wanted):
                old[e["rel"]] = (data, mode)
        finally:
            arc.close()
    except (OSError, tarfile.TarError, BackupError):
        log.error("restore rollback: pre-restore backup %s is unreadable", pre_id)
        return len(done)
    for rel, dest in reversed(done):
        try:
            if rel in existed:
                data, mode = old[rel]
                try:
                    tmp = _write_new(os.path.dirname(dest), data, mode)
                    try:
                        os.replace(tmp, dest)
                    except BaseException:
                        _unlink(tmp)
                        raise
                except OSError:
                    with open(dest, "wb") as f:   # last resort: rewrite in place
                        f.write(data)
                    os.chmod(dest, mode)
            else:
                cur, _ = read_regular(dest, MAX_FILE)
                if _sha(cur) == staged_sha[rel]:
                    os.unlink(dest)
        except (OSError, KeyError):
            failures += 1
    return failures


def restore(archive_id, kind, *, roots, is_running, confirm=False, allow_other_target=False):
    """Restore one part (`kind`) of an archive into roots[kind].

    Every file is staged next to the target and checked against the manifest first; then,
    with the app confirmed stopped, the current files are backed up (pre_backup) and the staged
    files renamed into place. A failure part-way puts the previous files back. Files in the
    target that the archive doesn't have are left alone. An archive that recorded a different
    source folder is refused (RestoreTargetMismatch) unless allow_other_target is True."""
    if confirm is not True:
        raise ValueError("restore needs confirm=True")
    _check_id(archive_id)
    if kind not in KINDS:
        raise ValueError("unknown backup item kind")
    root = (roots or {}).get(kind)
    if not isinstance(root, str) or not os.path.isabs(root):
        raise ValueError(f"no {kind} folder to restore into")
    root = os.path.realpath(root)
    if not os.path.isdir(root):
        raise ValueError(f"the {kind} folder does not exist")
    with LOCK:
        if is_running(kind):
            raise BackupError(f"{kind} is running; close it before restoring")
        _held.add(archive_id)
        pre_id = None
        stage = None
        created = []
        try:
            arc = _Archive(archive_id)
            try:
                item = arc.item(kind)
                notes = []
                recorded = item.get("root")
                if isinstance(recorded, str) and recorded and os.path.realpath(recorded) != root:
                    if allow_other_target is not True:
                        raise RestoreTargetMismatch(os.path.realpath(recorded), root)
                    notes.append("this backup was taken from a different folder; restoring into the current one")
                    log.warning("external backup %s: %s part was taken from a different folder", archive_id, kind)
                plan = []
                for e in item["files"]:
                    if _excluded(kind, e["rel"]):
                        raise BackupError("archive contains a file that is never restored")
                    path, dest = _target(root, e["rel"])
                    plan.append((e, path, dest))
                if len({d for _, _, d in plan}) != len(plan):
                    raise BackupError("two files in the backup point at the same target")
                _clean_stale_stages(root)
                stage = tempfile.mkdtemp(prefix=STAGE_PREFIX, dir=root)
                staged = {}
                for e, data, mode in arc.files(kind, [p[0] for p in plan]):
                    staged[e["rel"]] = (_write_new(stage, data, mode), e["sha256"])
            finally:
                arc.close()

            existed = set()
            for e, path, dest in plan:
                try:
                    st = os.lstat(dest)
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    raise BackupError("a file in the backup would replace something that is not a file")
                if st.st_size > MAX_FILE:
                    raise BackupError("a current file is too large to back up first; not restoring")
                existed.add(e["rel"])

            if is_running(kind):
                raise BackupError(f"{kind} is running; close it before restoring")
            pre, contents = _create([{"kind": kind, "root": root, "include": None, "label": "current files",
                                      "_exact": sorted(existed)}],
                                    label=PRE_RESTORE_LABEL, pre_restore=True)
            pre_id = pre["id"]
            _held.add(pre_id)
            if set(contents.get(kind, {})) != existed:
                raise BackupError("could not back up every current file first; not restoring")

            stage_dev = os.stat(stage).st_dev
            for e, path, dest in plan:
                _makedirs(os.path.dirname(dest), root, created)
                if os.stat(os.path.dirname(dest)).st_dev != stage_dev:
                    raise BackupError("a target folder is on another disk; not restoring")

            if is_running(kind):   # last check, right before anything is replaced
                raise BackupError(f"{kind} is running; close it before restoring")
            done = []
            _write_journal(archive_id, pre_id, root)
            try:
                for e, path, dest in plan:
                    os.replace(staged[e["rel"]][0], dest)
                    done.append((e["rel"], dest))
            except OSError:
                failed = _rollback(pre_id, kind, done, existed, {r: s for r, (_, s) in staged.items()})
                for d in reversed(created):
                    try:
                        os.rmdir(d)
                    except OSError:
                        pass
                created = []
                if not failed:
                    _clear_journal()
                if failed:
                    log.error("external backup %s: restore of %s failed and %d files could not be put back",
                              archive_id, kind, failed)
                    raise BackupError(f"restore failed and {failed} files could not be put back; "
                                      f"restore backup {pre_id} to recover") from None
                log.error("external backup %s: restore of %s failed, previous files put back", archive_id, kind)
                raise BackupError("restore failed; the previous files were put back") from None
            for d in {os.path.dirname(dest) for _, _, dest in plan}:
                _fsync_dir(d)
            _clear_journal()
            log.info("external backup %s: restored %d %s files (pre-restore backup %s)",
                     archive_id, len(done), kind, pre_id)
            return {"ok": True, "pre_backup": pre_id, "restored": len(done), "notes": notes}
        except BaseException:
            for d in reversed(created):   # refused before any rename: drop folders we made
                try:
                    os.rmdir(d)
                except OSError:
                    pass
            raise
        finally:
            _held.discard(archive_id)
            if pre_id is not None:
                _held.discard(pre_id)
            if stage is not None:
                shutil.rmtree(stage, ignore_errors=True)
