"""Every version of config.yaml, kept so a working setup is never lost.

    backups/revisions/<YYYYmmdd-HHMMSS-ffffff>-<source>.yaml   the exact bytes (0600)
    backups/revisions/<same stem>.json                         {ts, source, label, hash, size, summary}
    backups/daily/<YYYY-MM-DD>/                                 config.yaml + a few state files
                                                                + manifest.json {ts, files, hash}

A revision is recorded whenever the file changes, whoever changed it (daemon start, a
hand edit, the panel, an import, a restore); identical content is not recorded twice
in a row. A restore records the file first, so it can itself be undone. Secrets
(tokens, passwords) are never copied: the daily bundle is an explicit allow-list.

Older backups/config-*.yaml files (from before revisions existed) are listed as
"legacy" and left alone.
"""
import datetime
import difflib
import hashlib
import json
import os
import re
import shutil
import threading
import time

import yaml

from . import configio
from .paths import state_dir

SOURCES = ("start", "panel", "file", "import", "restore", "manual", "before-save")
KEEP_DAYS = 14          # revisions younger than this are always kept ...
KEEP_REVISIONS = 200    # ... and so are the newest this many
KEEP_DAILY = 30
KEEP_LABELLED = 100     # labelled revisions (snapshots, "Before import", "Restored ...") are exempt from the age/count limits, up to this many
DAILY_STATE_FILES = ("stream_history.json", "radio.json")   # allow-list: never add a secret here
MAX_DIFF = 200 * 1024
MAX_DIFF_INPUT = 512 * 1024   # difflib is quadratic at worst; bigger files aren't diffed
MAX_SUMMARY = 120

LOCK = threading.RLock()  # one writer at a time: record, restore, daily, prune, configio.save

STAMP = "%Y%m%d-%H%M%S-%f"
REV_RE = re.compile(r"^(\d{8}-\d{6}-\d{6})-([a-z-]+)$")
ID_RE = re.compile(r"^(\d{8}-\d{6}-\d{6}-[a-z-]+|config-\d{8}-\d{6}(-\d{6})?)$")
LEGACY_RE = re.compile(r"^config-(\d{8}-\d{6})(?:-(\d{6}))?$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MISSING = object()


def _dir(*parts):
    d = os.path.join(state_dir(), "backups", *parts)
    if not os.path.isdir(d):
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
    return d


def _rev_dir():
    return _dir("revisions")


def _daily_dir():
    return _dir("daily")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def _write_json(path, obj):
    configio.write_atomic(path, json.dumps(obj, indent=1, sort_keys=True).encode())


def _stamp_time(stamp):
    return datetime.datetime.strptime(stamp, STAMP).timestamp()


# ------------------------------------------------------------------ revisions

def _rev_ids():
    """Revision ids on disk, oldest first (the stamp sorts; record() keeps stamps unique)."""
    out = []
    for name in os.listdir(_rev_dir()):
        stem, ext = os.path.splitext(name)
        if ext == ".yaml" and REV_RE.fullmatch(stem):
            out.append(stem)
    return sorted(out)


def _meta(rid):
    """A revision's sidecar; rebuilt from the yaml if the sidecar is missing or unreadable."""
    base = os.path.join(_rev_dir(), rid)
    try:
        with open(base + ".json") as f:
            meta = json.load(f)
        if isinstance(meta, dict) and meta.get("hash"):
            return meta
    except (OSError, ValueError):
        pass
    data = _read(base + ".yaml")
    stamp, source = REV_RE.fullmatch(rid).groups()
    return {"ts": _stamp_time(stamp), "source": source, "label": None, "hash": _sha(data),
            "size": len(data), "summary": "changed"}


def _names(a, b):
    """Names of the entries that differ between two mappings (b's order, then removed ones)."""
    if not isinstance(a, dict) or not isinstance(b, dict):
        return []
    return [str(n) for n in [*b, *(n for n in a if n not in b)] if a.get(n, _MISSING) != b.get(n, _MISSING)]


def _layout_pages(lay):
    return lay["pages"] if isinstance(lay, dict) and isinstance(lay.get("pages"), dict) else lay


def _layout_names(a, b):
    out = []
    a, b = (a if isinstance(a, dict) else {}), (b if isinstance(b, dict) else {})
    for name in _names(a, b):
        la, lb = a.get(name), b.get(name)
        pa, pb = _layout_pages(la), _layout_pages(lb)
        rest_a = {k: v for k, v in la.items() if k != "pages"} if isinstance(la, dict) and pa is not la else None
        rest_b = {k: v for k, v in lb.items() if k != "pages"} if isinstance(lb, dict) and pb is not lb else None
        if isinstance(pa, dict) and isinstance(pb, dict) and rest_a == rest_b:
            out += [f"{name}/{p}" for p in _names(pa, pb)]
        else:
            out.append(name)
    return out


def summarize(old, new):
    """What changed between two versions of the file (bytes), in a few words."""
    if old is None:
        return "first revision"
    try:
        a, b = configio.safe_load_bounded(old), configio.safe_load_bounded(new)
        if not isinstance(a, dict) or not isinstance(b, dict):
            return "changed"
        parts = []
        for k in [*b, *(k for k in a if k not in b)]:
            va, vb = a.get(k, _MISSING), b.get(k, _MISSING)
            if va == vb:
                continue
            if k in ("pages", "keys"):
                names = _names(va, vb)
            elif k == "layouts":
                names = _layout_names(va, vb)
            else:
                names = []
            parts.append(f"{k}: {', '.join(names)}" if names else str(k))
        text = " · ".join(parts) or "formatting or comments only"
        return text if len(text) <= MAX_SUMMARY else text[:MAX_SUMMARY - 1] + "…"
    except Exception:
        return "changed"


def record(path, source, label=None):
    """Keep the file's current bytes as a new revision. None if the file is missing or
    identical to the newest revision; otherwise the revision's listing entry."""
    if source not in SOURCES:
        raise ValueError(f"unknown backup source {source!r}")
    with LOCK:
        try:
            data = _read(path)
        except FileNotFoundError:
            return None
        h = _sha(data)
        ids = _rev_ids()
        prev = None
        if ids:
            try:
                newest = _meta(ids[-1])
                if newest.get("hash") == h and not label:   # a labelled snapshot is a bookmark: always kept
                    return None
                prev = _read(os.path.join(_rev_dir(), ids[-1] + ".yaml"))
            except OSError:
                prev = b""  # unreadable previous revision: still record this one
        now = time.time()
        stamp = datetime.datetime.fromtimestamp(now).strftime(STAMP)
        if ids and stamp <= ids[-1][:22]:  # same microsecond or the clock went back: stay ordered
            stamp = (datetime.datetime.strptime(ids[-1][:22], STAMP)
                     + datetime.timedelta(microseconds=1)).strftime(STAMP)
        rid = f"{stamp}-{source}"
        base = os.path.join(_rev_dir(), rid)
        meta = {"ts": now, "source": source, "label": label, "hash": h, "size": len(data),
                "summary": summarize(prev, data) if prev != b"" else "changed"}
        configio.write_atomic(base + ".yaml", data)
        _write_json(base + ".json", meta)
        try:
            prune()
        except OSError:
            pass  # pruning is housekeeping; the revision is safely written
        return {"id": rid, **meta}


def _legacy():
    bdir = _dir()
    out = []
    for name in os.listdir(bdir):
        stem, ext = os.path.splitext(name)
        m = LEGACY_RE.fullmatch(stem)
        if ext != ".yaml" or not m:
            continue
        try:
            data = _read(os.path.join(bdir, name))
            ts = datetime.datetime.strptime(m.group(1) + "-" + (m.group(2) or "000000"), STAMP).timestamp()
        except (OSError, ValueError):
            continue
        out.append({"id": stem, "ts": ts, "source": "legacy", "label": None, "hash": _sha(data),
                    "size": len(data), "summary": "backup from before revisions were kept"})
    return out


def list_revisions(limit=200):
    """Revisions newest first, including legacy backups/config-*.yaml copies."""
    out = []
    for rid in _rev_ids():
        try:
            m = _meta(rid)
        except OSError:
            continue  # pruned while we looked
        out.append({"id": rid, "ts": m.get("ts"), "source": m.get("source"), "label": m.get("label"),
                    "hash": m.get("hash"), "size": m.get("size"), "summary": m.get("summary")})
    out += _legacy()
    out.sort(key=lambda r: (r["ts"] or 0, r["id"]), reverse=True)
    return out[:limit]


def _rev_path(rid):
    if not isinstance(rid, str) or not ID_RE.fullmatch(rid):
        raise ValueError("not a revision id")
    return os.path.join(_dir() if rid.startswith("config-") else _rev_dir(), rid + ".yaml")


def _read_rev(rid):
    try:
        return _read(_rev_path(rid))
    except FileNotFoundError:
        raise LookupError(f"no revision {rid}") from None


def read_revision(rid):
    return _read_rev(rid).decode("utf-8", errors="replace")


def _clip_diff(text):
    if len(text.encode()) <= MAX_DIFF:
        return text
    return text.encode()[:MAX_DIFF].decode("utf-8", errors="ignore") + "\n… diff truncated\n"


def diff(rid, path):
    """Unified diff from a revision to the current file."""
    old = read_revision(rid)
    try:
        cur = _read(path).decode("utf-8", errors="replace")
    except FileNotFoundError:
        cur = ""
    if max(len(old.encode()), len(cur.encode())) > MAX_DIFF_INPUT:
        return f"(not compared: one side is over {MAX_DIFF_INPUT // 1024} KB)\n"
    lines = difflib.unified_diff(old.splitlines(keepends=True), cur.splitlines(keepends=True),
                                 fromfile=f"revision {rid}", tofile="current")
    return _clip_diff("".join(line if line.endswith("\n") else line + "\n" for line in lines))


def _restore_bytes(data, path, label):
    try:
        cfg = configio.safe_load_bounded(data)
    except yaml.YAMLError as e:
        raise ValueError(f"that backup isn't valid YAML: {e}") from None
    except ValueError as e:
        raise ValueError(f"that backup can't be restored: {e}") from None
    if not isinstance(cfg, dict) or not (cfg.get("pages") or cfg.get("layouts")):
        raise ValueError("that backup has no pages or layouts, so it can't be restored")
    with LOCK:
        record(path, "before-save")  # so the restore can be undone; if this fails, don't overwrite
        configio.write_atomic(path, data, follow_symlink=True)
        record(path, "restore", label=label)
    return configio.file_hash(path)


def restore(rid, path):
    """Put a revision's exact bytes back as config.yaml. Returns the new file hash."""
    return _restore_bytes(_read_rev(rid), path, f"Restored {rid}")


# ------------------------------------------------------------------ daily bundles

def _date_of(now):
    if now is None:
        return datetime.datetime.now()
    if isinstance(now, (int, float)):
        return datetime.datetime.fromtimestamp(now)
    return now


def daily(path, now=None):
    """Once a day, bundle config.yaml and a few state files. None if today's exists."""
    when = _date_of(now)
    date = when.strftime("%Y-%m-%d")
    with LOCK:
        ddir = _daily_dir()
        final = os.path.join(ddir, date)
        if os.path.exists(final):
            return None
        data = _read(path)
        tmp = os.path.join(ddir, f".tmp-{date}")
        shutil.rmtree(tmp, ignore_errors=True)
        os.mkdir(tmp, 0o700)
        try:
            configio.write_atomic(os.path.join(tmp, "config.yaml"), data)
            files = ["config.yaml"]
            for name in DAILY_STATE_FILES:
                src = os.path.join(state_dir(), name)
                try:
                    configio.write_atomic(os.path.join(tmp, name), _read(src))
                except FileNotFoundError:
                    continue
                files.append(name)
            manifest = {"ts": when.timestamp(), "files": files, "hash": _sha(data)}
            _write_json(os.path.join(tmp, "manifest.json"), manifest)
            os.rename(tmp, final)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        try:
            prune(now)
        except OSError:
            pass
        return {"date": date, **manifest}


def list_daily():
    out = []
    ddir = _daily_dir()
    for date in sorted(os.listdir(ddir), reverse=True):
        if not DATE_RE.fullmatch(date):
            continue
        try:
            with open(os.path.join(ddir, date, "manifest.json")) as f:
                m = json.load(f)
        except (OSError, ValueError):
            m = {}
        if not isinstance(m, dict):
            m = {}
        out.append({"date": date, "ts": m.get("ts"), "files": m.get("files") or [], "hash": m.get("hash")})
    return out


def restore_daily(date, path):
    """Put a daily bundle's config.yaml back (state files are not touched)."""
    if not isinstance(date, str) or not DATE_RE.fullmatch(date):
        raise ValueError("not a backup date (YYYY-MM-DD)")
    try:
        data = _read(os.path.join(_daily_dir(), date, "config.yaml"))
    except FileNotFoundError:
        raise LookupError(f"no daily backup for {date}") from None
    return _restore_bytes(data, path, f"Restored daily {date}")


# ------------------------------------------------------------------ pruning

def prune(now=None):
    """Drop unlabelled revisions older than KEEP_DAYS beyond the newest KEEP_REVISIONS (labelled
    ones are kept, up to the newest KEEP_LABELLED), and daily
    bundles beyond the newest KEEP_DAILY. Legacy config-*.yaml files are left alone."""
    cutoff = _date_of(now).timestamp() - KEEP_DAYS * 86400
    with LOCK:
        rdir = _rev_dir()
        ids = _rev_ids()
        labelled = []
        for rid in ids:
            try:
                if _meta(rid).get("label"):
                    labelled.append(rid)
            except OSError:
                pass
        drop = set(labelled[:-KEEP_LABELLED])
        kept = set(labelled[-KEEP_LABELLED:])
        for rid in ids[:-KEEP_REVISIONS]:
            if rid not in kept and _stamp_time(rid[:22]) < cutoff:
                drop.add(rid)
        for rid in sorted(drop):
            for ext in (".yaml", ".json"):
                try:
                    os.remove(os.path.join(rdir, rid + ext))
                except FileNotFoundError:
                    pass
        keep = set(ids) - drop
        for name in os.listdir(rdir):  # sidecars whose revision is gone
            stem, ext = os.path.splitext(name)
            if ext == ".json" and REV_RE.fullmatch(stem) and stem not in keep:
                os.remove(os.path.join(rdir, name))
        ddir = _daily_dir()
        dates = sorted(d for d in os.listdir(ddir) if DATE_RE.fullmatch(d))
        for d in dates[:-KEEP_DAILY]:
            shutil.rmtree(os.path.join(ddir, d))
        for name in os.listdir(ddir):  # bundles abandoned half-built on an earlier day
            p = os.path.join(ddir, name)
            if name.startswith(".tmp-") and time.time() - os.path.getmtime(p) > 86400:
                shutil.rmtree(p, ignore_errors=True)
