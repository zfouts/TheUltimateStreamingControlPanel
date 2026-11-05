"""setup_api: onboarding for an existing setup — detect, back up, import / adopt, stop another app.

    GET  /api/setup/detect                      detect.scan()
    POST /api/setup/backup      {label?, include: ["obs", "<app kind>", ...]}
                                → first archive's listing entry + {archive: id, archives: [entries]}
                                  (one archive per OBS settings folder; apps go in the first)
    GET  /api/setup/backups                     {backups: setup_backup.list_archives()}
    POST /api/setup/restore     {id, item, confirm: true} → {ok, pre_backup, restored, notes}
    POST /api/setup/backups/pin {id, pinned}    → listing entry
    POST /api/setup/backups/delete {id}         → {ok}; refuses a pinned archive
    POST /api/setup/upload      raw application/octet-stream ≤ 50 MB → {upload_id}
    POST /api/setup/import/preview {kind, source_id | upload_id}
                                → {token, plan, defaults, warnings, info, review: [...]}
                                  review item: {layout|null, page, key, row, col, label, kind,
                                  argument, expect, path, conflict} at the key's destination
    POST /api/setup/import/apply   {token, choices} → {ok, hash, counts}
    POST /api/setup/review      {page, key, layout?, hash, expect, enable: true} → {ok, hash, action}
    GET  /api/setup/obs                          obs_setup.current() + {addons}
    POST /api/setup/obs/adopt   {profile?, collection?}
    POST /api/setup/obs/addon   {scene, addon}
    GET  /api/setup/apps/running                {kind: bool}
    POST /api/setup/app/stop    {kind, confirm: true, force?} → appstop.stop()

The browser never sends a path: local sources are detect's opaque source_ids, files from the
browser come through /upload (an opaque upload_id). A preview parses the source once and keeps
the sanitised result server-side under a random token; apply merges that stored object and never
reads the source again. Error messages never quote file content (importer errors are written to
be safe; anything unexpected becomes a fixed message).
"""
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from functools import partial
from types import SimpleNamespace

from core import appstop, backups, configio, detect, importer, setup_backup
from core.importers import KINDS, ImportError_, _common, to_config
from core.importers._common import parse_lock
from core.safefs import NotRegular, TooBig, read_regular, state_home

from . import ApiError
from .import_api import EXAMPLE, STALE, _current

log = logging.getLogger("deck")

UPLOAD_MAX = 50 * 1024 * 1024
UPLOAD_SECONDS = 120
UPLOAD_CHUNK = 1024 * 1024
TTL = 3600
MAX_UPLOADS = 3
MAX_PREVIEWS = 5
TOKEN = re.compile(r"[0-9a-f]{32}")
SHA256 = re.compile(r"[0-9a-f]{64}")
UNREADABLE = "That file couldn't be read as a setup to import."
PREVIEW_LOCK = threading.BoundedSemaphore(1)   # one source read + parse at a time (≤ 50 MB in memory)
PREVIEW_WAIT = 60
# obs_setup reports refusals as {ok: false, error}; the status follows the kind of refusal.
OBS_STATUS = (("already exists", 409), ("did not switch", 503), ("No OBS", 400), ("No scene", 400),
              ("Unknown add-on", 400), ("This OBS has no", 400))
clock = time.monotonic   # tests move time forward by replacing this


class _Pending:
    """Up to `limit` entries that expire `TTL` s after creation (oldest evicted first); on_drop
    is called for each entry that leaves, so files are deleted with it."""

    def __init__(self, limit, on_drop=None):
        self.limit, self.on_drop = limit, on_drop
        self.items = {}           # token -> entry (insertion order = age)
        self.lock = threading.Lock()

    def _drop(self, token):
        entry = self.items.pop(token)
        if self.on_drop:
            self.on_drop(entry)

    def purge(self):
        with self.lock:
            now = clock()
            for token in [t for t, e in self.items.items() if now - e["created"] > TTL]:
                self._drop(token)

    def add(self, entry, token=None):
        token = token or secrets.token_hex(16)    # 128 bits
        with self.lock:
            while len(self.items) >= self.limit:
                self._drop(next(iter(self.items)))
            self.items[token] = {**entry, "created": clock()}
        return token

    def get(self, token):
        self.purge()
        with self.lock:
            return self.items.get(token) if isinstance(token, str) and TOKEN.fullmatch(token) else None

    def pop(self, token):
        with self.lock:
            if token in self.items:
                self._drop(token)


def _cleanup_icons(entry=None):
    """Icons written for previews that are gone (expired, evicted or applied), if the importers
    offer that cleanup."""
    fn = getattr(_common, "cleanup_preview_icons", None)
    if fn is None:
        return
    try:
        fn(older_than=TTL)
    except Exception as e:
        log.warning("cleaning up preview icons failed: %s", type(e).__name__)


def _unlink(entry):
    try:
        os.unlink(entry["path"])
    except OSError:
        pass


def upload_dir():
    """<XDG state>/ultimate-streaming/uploads (0700): pending uploads, each a 0600 file."""
    d = os.path.join(state_home(), "uploads")
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def _clear_uploads():
    """Leftovers from an earlier run (their ids died with it)."""
    d = upload_dir()
    for name in os.listdir(d):
        try:
            os.unlink(os.path.join(d, name))
        except OSError:
            pass


# ------------------------------------------------------------------ roots from detect

def _boatswain_files(root):
    """The Boatswain device files in a data folder (the native one is all of ~/.local/share)."""
    names = []
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError:
        return names
    for e in entries:
        if not e.name.endswith(".json") or not e.is_file(follow_symlinks=False):
            continue
        try:
            data = json.loads(read_regular(e.path, detect.APP_JSON_MAX)[0])
        except (OSError, ValueError, RecursionError):
            continue
        if isinstance(data, dict) and isinstance(data.get("profiles"), list) and "active-profile" in data:
            names += [e.name, f"backups/{e.name[:-5]}.bak.v*"]
    return names


def _app_items(scan):
    """{kind: setup_backup item} for the deck apps a scan found: StreamController's data folder, the
    streamdeck-ui file (its folder + exact name) and Boatswain's device files. Paths come from
    detect's server-side map, never the browser."""
    out = {}
    for app in scan["apps"]:
        kind = app["kind"]
        if kind in out or kind not in setup_backup.KINDS or kind == "obs":
            continue
        try:
            _, real = detect.resolve(app["source_id"])
        except KeyError:
            continue
        if kind == "streamdeck_ui":
            item = {"root": os.path.dirname(real), "include": [os.path.basename(real)]}
        elif kind == "boatswain":
            item = {"root": real, "include": _boatswain_files(real)}
        else:
            item = {"root": real, "include": None}
        out[kind] = {"kind": kind, "label": app["label"], **item}
    return out


def _obs_items(scan):
    """[(root kind, setup_backup item)] for every OBS settings folder, the active one first."""
    info = {r["source_id"]: r for r in scan["obs"]["roots"]}
    out = []
    for sid, real in detect.obs_roots(scan):
        r = info.get(sid) or {}
        out.append((r.get("kind") or "obs", {"kind": "obs", "root": real, "include": list(setup_backup.OBS_INCLUDE),
                                             "label": r.get("label") or "OBS"}))
    return out


def _restore_root(kind, scan):
    """Where a restore of kind writes: OBS's active folder (else its default), every other kind
    its fixed default folder, so a restore still works after the files were deleted. The
    archive's own record of the folder is never used."""
    if kind == "obs":
        return detect.active_obs_root(scan) or detect.default_root("obs")
    return detect.default_root(kind)


def _is_running(kind):
    return appstop.running(kind)


# ------------------------------------------------------------------ review

def expect_hash(action):
    """What /review must be sent back: binds the click to the exact action the user was shown."""
    return hashlib.sha256(json.dumps(action, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _arg_text(action):
    """(kinds, argument as text) of an imported action, for display only (textContent)."""
    steps = action if isinstance(action, list) else [action]
    kinds = [k for s in steps if isinstance(s, dict) for k in s]
    if isinstance(action, dict) and len(action) == 1:
        arg = next(iter(action.values()))
        text = arg if isinstance(arg, str) else json.dumps(arg, ensure_ascii=False)
    else:
        text = json.dumps(action, ensure_ascii=False)
    return ", ".join(kinds), text


def _flat(rows):
    if not isinstance(rows, list):
        return []
    if any(isinstance(r, list) for r in rows):
        return [k for r in rows for k in (r if isinstance(r, list) else [r])]
    return list(rows)


def _is_page(value):
    return isinstance(value, list) or (isinstance(value, dict) and "keys" in value)


def _landing(current, incoming, target, fresh, grid=None):
    """[(layout | None, page, page value, item path, conflict)]: where each incoming page lands when
    the plan is applied, read from the plan's own ops (so a flat page merged into a layout, or an
    incoming layout merged into your flat pages, is reported where it really ends up)."""
    items, _, _ = importer._items(current, incoming, target, fresh, grid)
    out = []
    for it in items:
        where = (it["path"], it["kind"] == "conflict")
        for op in it["ops"]:
            if op[0] == "fresh_pages":
                out += [(None, n, p, *where) for n, p in incoming["pages"].items()]
            elif op[0] != "set":
                continue
            elif op[1] == ("pages",) and _is_page(op[3]):
                out.append((None, op[2], op[3], *where))
            elif op[1] == ("layouts",):
                pages, _, _ = importer._layout_parts(op[3])
                out += [(op[2], n, p, *where) for n, p in pages.items() if _is_page(p)]
            elif op[1][:1] == ("layouts",) and len(op[1]) in (2, 3) and _is_page(op[3]):
                out.append((op[1][1], op[2], op[3], *where))
    return out


def _review_list(landing):
    """Every key whose action needs review, at its destination, with exactly what it would run."""
    out = []
    for lname, pname, page, path, conflict in landing:
        rows = page.get("keys") if isinstance(page, dict) else page
        cols = max((len(r) for r in rows if isinstance(r, list)), default=0) if isinstance(rows, list) else 0
        for i, key in enumerate(_flat(rows)):
            act = key.get("action") if isinstance(key, dict) else None
            if not (isinstance(act, dict) and isinstance(act.get("needs_review"), dict)):
                continue
            imported = act["needs_review"].get("imported_action")
            kinds, text = _arg_text(imported)
            out.append({"layout": lname, "page": pname, "key": i,
                        "row": i // cols if cols else 0, "col": i % cols if cols else i,
                        "label": key.get("label") or "", "kind": kinds, "argument": text,
                        "expect": expect_hash(imported), "path": path, "conflict": conflict})
    return out


def _find_key(cfg, layout, page):
    """The key list (rows) of layouts[layout].pages[page], or of top-level pages when layout is None."""
    if layout is None:
        pages = cfg.get("pages")
    else:
        layouts = cfg.get("layouts")
        lay = layouts.get(layout) if isinstance(layouts, dict) else None
        pages = lay.get("pages", lay) if isinstance(lay, dict) else None
    p = pages.get(page) if isinstance(pages, dict) else None
    if p is None:
        raise ApiError("no such page", 404)
    return p.get("keys") if isinstance(p, dict) else p


def _key_at(rows, index):
    """The key dict at a flat index (row-major), in place."""
    if not isinstance(rows, list):
        raise ApiError("no such key", 404)
    n = 0
    nested = any(isinstance(r, list) for r in rows)
    for r in rows:
        for k in (r if nested and isinstance(r, list) else [r]):
            if n == index:
                return k
            n += 1
    raise ApiError("no such key", 404)


# ------------------------------------------------------------------ routes

def _read_source(uploads, body, kind):
    """What the importer reads: bytes for elgato, a path for the others. Plus a cleanup."""
    sid, uid = body.get("source_id"), body.get("upload_id")
    if (sid is None) == (uid is None):
        raise ApiError("send either a source_id (from detect) or an upload_id")
    if uid is not None:
        if kind != "elgato":
            raise ApiError("only Elgato profiles can be uploaded")
        entry = uploads.get(uid)
        if entry is None:
            raise ApiError("unknown or expired upload; upload the file again", 404)
        try:
            data = read_regular(entry["path"], UPLOAD_MAX)[0]
        except (OSError, TooBig, NotRegular):
            raise ApiError("unknown or expired upload; upload the file again", 404) from None
        return data, lambda: uploads.pop(uid)
    try:
        skind, real = detect.resolve(sid) if isinstance(sid, str) else (None, None)
    except KeyError:
        skind = None
    if skind is None:
        raise ApiError("unknown source; scan again", 404)
    if skind != kind:
        raise ApiError("that source is not of this kind")
    if kind == "elgato":
        try:
            return read_regular(real, detect.ELGATO_MAX)[0], lambda: None
        except TooBig:
            raise ApiError(f"That file is larger than {detect.ELGATO_MAX >> 20} MB.", 413) from None
        except OSError:
            raise ApiError("That file couldn't be read.") from None
    return real, lambda: None


def register(router, ed):
    uploads = _Pending(MAX_UPLOADS, on_drop=_unlink)
    previews = _Pending(MAX_PREVIEWS, on_drop=_cleanup_icons)
    uploading = threading.BoundedSemaphore(1)
    last = {"scan": None}
    _clear_uploads()

    def purged(fn):
        def wrapper(arg):
            uploads.purge()
            previews.purge()
            return fn(arg)
        return wrapper

    def get(path):
        return lambda fn: router.get(path)(purged(fn))

    def post(path):
        return lambda fn: router.post(path)(purged(fn))

    def scan():
        last["scan"] = detect.scan()
        return last["scan"]

    def grid():
        """(cols, rows) of the primary attached deck, or None."""
        primary = getattr(ed.bot, "primary", None)
        try:
            rows, cols = primary.deck.key_layout()
            return cols, rows
        except Exception:
            return None

    def target(cfg):
        """The effective layout of the primary attached deck, if the current config has it."""
        primary = getattr(ed.bot, "primary", None)
        name = getattr(primary, "layout_name", None) if primary else None
        layouts = cfg.get("layouts")
        return name if isinstance(layouts, dict) and name in layouts else None

    def obs_send():
        mod = ed.bot.get_module("OBS")
        if mod is None:
            raise ApiError("the OBS module isn't loaded", 503)
        return mod.send

    def obs_setup():
        from core import obs_setup as mod   # lazy: optional at import time
        return mod

    def archive_id(body):
        aid = body.get("id")
        if not isinstance(aid, str) or not setup_backup.ID_RE.fullmatch(aid):
            raise ApiError("invalid backup id")
        return aid

    def obs_call(fn, *a, **kw):
        """An obs_setup call: its {ok: false, error} refusals and OBS failures as ApiErrors."""
        try:
            result = fn(*a, **kw)
        except ApiError:
            raise
        except setup_backup.BackupError as e:
            raise ApiError(str(e), 409) from None
        except Exception as e:
            raise ApiError(f"OBS isn't reachable ({type(e).__name__})", 503) from None
        if isinstance(result, dict) and result.get("ok") is False:
            msg = str(result.get("error") or "OBS refused that")
            raise ApiError(msg, next((code for part, code in OBS_STATUS if part in msg), 400))
        return result

    def backup_call(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except setup_backup.NotFound:
            raise ApiError("no such backup", 404) from None
        except setup_backup.RestoreTargetMismatch as e:
            raise ApiError(str(e), 409, mismatch={"archive_root": e.archive_root,
                                                  "target_root": e.target_root}) from None
        except setup_backup.BackupError as e:
            raise ApiError(str(e), 409) from None
        except ValueError as e:
            raise ApiError(str(e)) from None

    c = SimpleNamespace(router=router, ed=ed, uploads=uploads, previews=previews, uploading=uploading,
                        last=last, purged=purged, get=get, post=post, scan=scan, grid=grid, target=target,
                        obs_send=obs_send, obs_setup=obs_setup, archive_id=archive_id,
                        obs_call=obs_call, backup_call=backup_call)
    _backup_routes(c)
    _upload_routes(c)
    _import_routes(c)
    _obs_routes(c)
    _app_routes(c)


def _backup_routes(c):
    last, get, post, scan, archive_id, backup_call = c.last, c.get, c.post, c.scan, c.archive_id, c.backup_call

    @get("/api/setup/detect")
    def detect_(query):
        return scan()

    @post("/api/setup/backup")
    def backup(body):
        include = body.get("include")
        if not isinstance(include, list) or not include or not all(isinstance(k, str) for k in include):
            raise ApiError("include must be a list of kinds")
        bad = [k for k in include if k not in setup_backup.KINDS]
        if bad:
            raise ApiError("can only back up: " + ", ".join(setup_backup.KINDS))
        label = body.get("label")
        if label is not None and not isinstance(label, str):
            raise ApiError("label must be text")
        found = last["scan"] or scan()
        include = list(dict.fromkeys(include))
        apps = _app_items(found)
        obs = _obs_items(found) if "obs" in include else []
        missing = [k for k in include if (k == "obs" and not obs) or (k != "obs" and k not in apps)]
        if missing:
            raise ApiError("not found on this computer: " + ", ".join(missing), 404)
        first = not any(not a["auto"] for a in setup_backup.list_archives())
        base = label or ("Before onboarding" if first else "Manual backup")
        # setup_backup takes one item per kind: one archive per OBS folder, the apps in the first.
        groups = [([item], f"{base} ({rkind})") for rkind, item in obs] or [([], base)]
        groups[0][0].extend(apps[k] for k in include if k != "obs")
        entries = [backup_call(setup_backup.create, items, label=lbl, pinned=first) for items, lbl in groups]
        return {**entries[0], "archive": entries[0]["id"], "archives": entries}

    @get("/api/setup/backups")
    def backups_(query):
        return {"backups": setup_backup.list_archives(),
                "interrupted_restore": setup_backup.interrupted_restore()}

    @post("/api/setup/restore")
    def restore(body):
        if os.environ.get("DECK_DRY_RUN") == "1":   # set by tools/devserver.py unless --live
            raise ApiError("dry run: restoring is disabled in the dev server (use --live)", 403)
        if body.get("confirm") is not True:
            raise ApiError("confirm: true is required to restore")
        aid, kind = archive_id(body), body.get("item")
        if kind not in setup_backup.KINDS:
            raise ApiError("unknown backup item")
        entry = next((a for a in setup_backup.list_archives() if a["id"] == aid), None)
        if entry is None:
            raise ApiError("no such backup", 404)
        item = next((i for i in entry["items"] if i["kind"] == kind), None)
        if item is None:
            raise ApiError("that backup has no such item", 404)
        if item["label"] == appstop.AUTOSTART_LABEL:
            raise ApiError("this backup holds autostart files and can't be restored from here")
        root = _restore_root(kind, scan() if kind == "obs" else None)
        try:
            os.makedirs(root, mode=0o700, exist_ok=True)   # restoring after the folder was deleted
        except OSError:
            raise ApiError(f"the {kind} folder can't be created") from None
        allow = body.get("allow_other_target", False)
        if not isinstance(allow, bool):
            raise ApiError("allow_other_target must be true or false")
        return backup_call(setup_backup.restore, aid, kind, roots={kind: root}, is_running=_is_running,
                           confirm=True, allow_other_target=allow)

    @post("/api/setup/backups/pin")
    def pin(body):
        pinned = body.get("pinned")
        if not isinstance(pinned, bool):
            raise ApiError("pinned must be true or false")
        return backup_call(setup_backup.pin, archive_id(body), pinned)

    @post("/api/setup/backups/delete")
    def delete(body):
        backup_call(setup_backup.delete, archive_id(body))
        return {"ok": True}


def _upload_routes(c):
    router, uploads, uploading, purged = c.router, c.uploads, c.uploading, c.purged

    @router.upload("/api/setup/upload", max_bytes=UPLOAD_MAX, max_seconds=UPLOAD_SECONDS)
    @purged
    def upload(stream):
        if not uploading.acquire(blocking=False):
            raise ApiError("another upload is in progress", 429)
        try:
            path = os.path.join(upload_dir(), secrets.token_hex(16) + ".part")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            size = 0
            try:
                with os.fdopen(fd, "wb") as f:
                    while size <= UPLOAD_MAX:
                        chunk = stream.read(UPLOAD_CHUNK)
                        if not chunk:
                            break
                        size += len(chunk)
                        f.write(chunk)
                if size != stream.length or size > UPLOAD_MAX:
                    raise ApiError("the upload was cut short")
            except BaseException:
                _unlink({"path": path})
                raise
            return {"upload_id": uploads.add({"path": path, "size": size})}
        finally:
            uploading.release()


def _import_routes(c):
    ed, uploads, previews, post, grid, target = c.ed, c.uploads, c.previews, c.post, c.grid, c.target
    read_source = partial(_read_source, uploads)

    @post("/api/setup/import/preview")
    def preview(body):
        if "path" in body:
            raise ApiError("paths aren't accepted; use a source_id from detect or an upload_id")
        kind = body.get("kind")
        reader = KINDS.get(kind) if isinstance(kind, str) else None
        if reader is None:
            raise ApiError("can import: " + ", ".join(sorted(KINDS)))
        _cleanup_icons()                    # opportunistic: previews never applied
        token = secrets.token_hex(16)       # before reading: the reader writes icons under it
        # The source (up to 50 MB) is read only once this preview's turn has come.
        if not PREVIEW_LOCK.acquire(timeout=PREVIEW_WAIT):
            raise ApiError("another import is still being read; try again in a moment", 429)
        try:
            source, done = read_source(body, kind)
            try:
                setup = reader(source, preview=token)   # each reader holds parse_lock itself
                source = None
                with parse_lock():
                    incoming, info = to_config(setup)
            except ImportError_ as e:
                raise ApiError(str(e)) from None
            except Exception as e:
                log.warning("import preview of %s failed: %s", kind, type(e).__name__)   # never the message
                raise ApiError(UNREADABLE) from None
        finally:
            PREVIEW_LOCK.release()
        incoming = importer._fix_reserved(incoming)   # as plan/apply see it, so review names match
        cfg, base = _current(ed.bot.config_path)
        fresh = importer.is_fresh(cfg, EXAMPLE)
        tgt, cells = target(cfg), grid()
        plan = importer.plan(cfg, incoming, tgt, fresh, cells)
        review = _review_list(_landing(cfg, incoming, tgt, fresh, cells))
        warnings = [w for w in setup.get("warnings") or [] if isinstance(w, str)]
        previews.add({"kind": kind, "config": incoming, "plan": plan, "info": info, "warnings": warnings,
                      "hash": base, "target": tgt, "grid": cells, "fresh": fresh}, token)
        done()
        return {"token": token, "plan": plan, "defaults": importer.defaults(plan), "warnings": warnings,
                "info": info, "review": review}

    @post("/api/setup/import/apply")
    def apply(body):
        stored = previews.get(body.get("token"))
        if stored is None:
            raise ApiError("unknown or expired preview; preview again", 404)
        choices = body.get("choices")
        choices = {} if choices is None else choices
        if not isinstance(choices, dict) or any(c not in ("current", "incoming") for c in choices.values()):
            raise ApiError("each choice must be 'current' or 'incoming'")
        path = ed.bot.config_path
        cfg, current_hash = _current(path)
        if current_hash != stored["hash"]:
            raise ApiError(STALE, 409)
        # Icons move from the preview folder to their final one; keep the result so a retry
        # (after a conflict) doesn't lose them.
        token = body["token"]
        stored["config"] = _common.commit_preview_icons(stored["config"], token)
        try:
            merged, counts = importer.apply(cfg, stored["config"], choices, stored["target"], stored["fresh"],
                                            stored["grid"])
        except ValueError as e:
            raise ApiError(str(e)) from None
        backups.record(path, "manual", label=f"Before import {stored['kind']}")
        try:
            new_hash = configio.save(path, merged, stored["hash"], source="import")
        except configio.Conflict:
            raise ApiError(STALE, 409) from None
        previews.pop(token)
        return {"ok": True, "hash": new_hash, "counts": counts}

    @post("/api/setup/review")
    def review(body):
        if body.get("enable") is not True:
            raise ApiError("enable: true is required")
        page, index, layout = body.get("page"), body.get("key"), body.get("layout")
        if not isinstance(page, str) or isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ApiError("page (text) and key (index) are required")
        if layout is not None and not isinstance(layout, str):
            raise ApiError("layout must be text")
        base, expect = body.get("hash"), body.get("expect")
        if not isinstance(base, str) or not base:
            raise ApiError("hash is required (the config's hash when you looked at the key)")
        if not isinstance(expect, str) or not SHA256.fullmatch(expect):
            raise ApiError("expect is required (from the review list)")
        path = ed.bot.config_path
        cfg, current_hash = _current(path)
        if base != current_hash:
            raise ApiError("config.yaml was changed; reload and try again", 409)
        key = _key_at(_find_key(cfg, layout, page), index)
        act = key.get("action") if isinstance(key, dict) else None
        nr = act.get("needs_review") if isinstance(act, dict) and set(act) == {"needs_review"} else None
        if not isinstance(nr, dict) or not isinstance(nr.get("imported_action"), (dict, list)):
            raise ApiError("that key has no action waiting for review", 409)
        if expect_hash(nr["imported_action"]) != expect:
            raise ApiError("that key's action isn't the one you reviewed; reload and look again", 409)
        key["action"] = nr["imported_action"]
        try:
            new_hash = configio.save(path, cfg, current_hash, source="panel")
        except configio.Conflict:
            raise ApiError("config.yaml was changed; reload and try again", 409) from None
        log.info("imported action enabled on %s key %d", page, index)   # never the action itself
        return {"ok": True, "hash": new_hash, "action": key["action"]}


def _obs_routes(c):
    ed, get, post, obs_send, obs_setup, obs_call = c.ed, c.get, c.post, c.obs_send, c.obs_setup, c.obs_call

    @get("/api/setup/obs")
    def obs(query):
        mod = obs_setup()
        return {**obs_call(mod.current, obs_send()), "addons": mod.ADDONS}

    @post("/api/setup/obs/adopt")
    def adopt(body):
        profile, collection = body.get("profile"), body.get("collection")
        if any(v is not None and not isinstance(v, str) for v in (profile, collection)):
            raise ApiError("profile and collection must be text")
        return obs_call(obs_setup().adopt, obs_send(), profile=profile, collection=collection)

    @post("/api/setup/obs/addon")
    def addon(body):
        mod = obs_setup()
        scene, name = body.get("scene"), body.get("addon")
        if not isinstance(scene, str) or not scene:
            raise ApiError("scene is required")
        if name not in mod.ADDONS:
            raise ApiError("unknown add-on")
        return obs_call(mod.add_addon, obs_send(), scene, name, ed.bot.cfg)   # the full config, as is


def _app_routes(c):
    get, post = c.get, c.post

    @get("/api/setup/apps/running")
    def apps_running(query):
        return {k: appstop.running(k) for k in appstop.STOPPABLE}

    @post("/api/setup/app/stop")
    def app_stop(body):
        kind, force = body.get("kind"), body.get("force", False)
        if kind not in appstop.STOPPABLE:
            raise ApiError("unknown app")
        if body.get("confirm") is not True:
            raise ApiError("confirm: true is required")
        if not isinstance(force, bool):
            raise ApiError("force must be true or false")
        try:
            return appstop.stop(kind, force=force)
        except (appstop.StopError, setup_backup.BackupError) as e:
            raise ApiError(str(e), 409) from None
