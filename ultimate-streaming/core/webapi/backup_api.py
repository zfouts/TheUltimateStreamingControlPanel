"""backup_api: config.yaml revisions and daily bundles (see core/backups.py).

    GET  /api/backups                  {revisions: [...], daily: [...]}, newest first
    GET  /api/backups/revision?id=     {id, text}: a revision's file
    GET  /api/backups/diff?id=         {id, diff}: unified diff from the revision to the current file
    POST /api/backups/restore          {id}: put a revision back (itself undoable) -> {ok, hash}
    POST /api/backups/restore-daily    {date}: put a daily bundle's config.yaml back -> {ok, hash}
    POST /api/backups/snapshot         {label?}: keep the current file now -> {ok, revision|null}

A restore only writes the file; the daemon notices and reloads it (publishing `config`).
"""
import re

from core import backups
from core.webapi import ApiError

MAX_LABEL = 80
# Credentials inside URLs (scheme://user:password@host, ?password=...) never reach the browser.
URL_PASSWORD_RE = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s:/@'\"]*:)[^\s@'\"]+@", re.I)
QUERY_SECRET_RE = re.compile(r"([?&][^=\s&'\"]*(?:pass|secret|token|key)[^=\s&'\"]*=)[^&\s'\"]*", re.I)


def redact(text):
    """Revision text / diffs with credentials inside URLs replaced by ***."""
    if not isinstance(text, str):
        return text
    return QUERY_SECRET_RE.sub(r"\1***", URL_PASSWORD_RE.sub(r"\1***@", text))


def _call(fn, *args):
    """Run a backups call, turning its errors into API errors (bad input 400, missing 404)."""
    try:
        return fn(*args)
    except LookupError as e:
        raise ApiError(str(e), status=404)
    except ValueError as e:
        raise ApiError(str(e), status=400)


def _id(source):
    rid = source.get("id")
    if not isinstance(rid, str) or not backups.ID_RE.fullmatch(rid):
        raise ApiError("not a revision id", status=400)
    return rid


def register(router, ed):
    def path():
        return ed.bot.config_path

    @router.get("/api/backups")
    def listing(query):
        return {"revisions": backups.list_revisions(), "daily": backups.list_daily()}

    @router.get("/api/backups/revision")
    def revision(query):
        rid = _id(query)
        return {"id": rid, "text": redact(_call(backups.read_revision, rid))}

    @router.get("/api/backups/diff")
    def diff(query):
        rid = _id(query)
        return {"id": rid, "diff": redact(_call(backups.diff, rid, path()))}

    @router.post("/api/backups/restore")
    def restore(body):
        rid = _id(body)
        return {"ok": True, "hash": _call(backups.restore, rid, path())}

    @router.post("/api/backups/restore-daily")
    def restore_daily(body):
        date = body.get("date")
        if not isinstance(date, str) or not backups.DATE_RE.fullmatch(date):
            raise ApiError("not a backup date (YYYY-MM-DD)", status=400)
        return {"ok": True, "hash": _call(backups.restore_daily, date, path())}

    @router.post("/api/backups/snapshot")
    def snapshot(body):
        label = body.get("label")
        if label is not None:
            if not isinstance(label, str):
                raise ApiError("label must be text", status=400)
            label = label.strip() or None
            if label and len(label) > MAX_LABEL:
                raise ApiError(f"label is too long (at most {MAX_LABEL} characters)", status=400)
        return {"ok": True, "revision": _call(backups.record, path(), "manual", label)}
