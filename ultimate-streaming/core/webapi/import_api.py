"""import_api: bring settings over from another config.yaml (core/importer.py does the merge).

    GET  /api/import/candidates   {candidates: [...], fresh}   deck configs found on this machine
    POST /api/import/preview      {path | text}                → {plan, defaults, dropped_settings, review}
    POST /api/import/apply        {path | text, choices: {path: current|incoming}, hash}
                                  → {ok, hash, counts, review}; 409 if config.yaml changed since `hash`
    POST /api/import/review       {path: [...], field, hash, expect, enable: true} → {ok, hash, value}

The incoming config is someone else's file: it goes through importer.sanitize() before the
merge, so actions, states and displays that could do harm arrive wrapped in needs_review and
settings that point at files or servers are left out (dropped_settings: their dotted paths).
review lists everything waiting (importer.review_items; in the preview at the incoming
config's own places, after apply at their places in config.yaml); /review enables one of them,
bound to the config's hash and to `expect`, the hash of exactly what was shown.
"""
import hashlib
import logging
import os

import yaml

from core import backups, configio, importer
from core.paths import ROOT

from . import ApiError

log = logging.getLogger("deck")

EXAMPLE = os.path.join(ROOT, "config.example.yaml")
STALE = "config.yaml was changed since the preview; preview again"
CHANGED = "config.yaml was changed; reload and try again"
SHA256_LEN = 64


def _source(body):
    """(sanitized incoming config, dropped_settings)."""
    path, text = body.get("path"), body.get("text")
    if (path is None) == (text is None):
        raise ApiError("send either a path or the config text")
    if text is not None:
        if not isinstance(text, str):
            raise ApiError("text must be a string")
        if len(text.encode("utf-8", "surrogatepass")) > importer.MAX_BYTES:
            raise ApiError("That config is too big (over 1 MB)", 413)
    elif not isinstance(path, str) or not path:
        raise ApiError("path must be a file path")
    try:
        return importer.sanitize(importer.load_source(path=path, text=text))
    except ValueError as e:
        raise ApiError(str(e)) from None


def _current(path):
    """(config, hash) from the same bytes, so the merge base is exactly the file that was hashed."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return {}, ""
    try:
        cfg = configio.safe_load_bounded(raw) or {}
    except yaml.YAMLError:
        raise ApiError("config.yaml isn’t valid YAML; fix it before importing") from None
    except ValueError as e:
        raise ApiError(f"config.yaml can’t be used ({e}); fix it before importing") from None
    if not isinstance(cfg, dict):
        raise ApiError("config.yaml isn’t a deck config; fix it before importing")
    return cfg, hashlib.sha256(raw).hexdigest()


def _target(ed, cfg):
    """The attached deck's layout, if the current config has it."""
    layouts = cfg.get("layouts")
    name = getattr(ed.bot, "layout_name", None)
    return name if isinstance(layouts, dict) and name in layouts else None


def register(router, ed):
    @router.get("/api/import/candidates")
    def candidates(query):
        cfg, _ = _current(ed.bot.config_path)
        return {"candidates": importer.candidates(current_path=ed.bot.config_path),
                "fresh": importer.is_fresh(cfg, EXAMPLE)}

    @router.post("/api/import/preview")
    def preview(body):
        incoming, dropped = _source(body)
        cfg, _ = _current(ed.bot.config_path)
        plan = importer.plan(cfg, incoming, _target(ed, cfg), importer.is_fresh(cfg, EXAMPLE))
        return {"plan": plan, "defaults": importer.defaults(plan), "dropped_settings": dropped,
                "review": importer.review_items(incoming)}

    @router.post("/api/import/apply")
    def apply(body):
        choices = body.get("choices")
        choices = {} if choices is None else choices
        if not isinstance(choices, dict) or any(c not in ("current", "incoming") for c in choices.values()):
            raise ApiError("each choice must be 'current' or 'incoming'")
        base = body.get("hash")
        if not isinstance(base, str):
            raise ApiError("hash is required (the config's hash when you previewed)")
        incoming, _ = _source(body)
        path = ed.bot.config_path
        cfg, current_hash = _current(path)
        if current_hash != base:
            raise ApiError(STALE, 409)
        try:
            merged, counts = importer.apply(cfg, incoming, choices, _target(ed, cfg),
                                            importer.is_fresh(cfg, EXAMPLE))
        except ValueError as e:
            raise ApiError(str(e)) from None
        backups.record(path, "manual", label="Before import")
        try:
            new_hash = configio.save(path, merged, base, source="import")
        except configio.Conflict:
            raise ApiError(STALE, 409) from None
        return {"ok": True, "hash": new_hash, "counts": counts, "review": importer.review_items(merged)}

    @router.post("/api/import/review")
    def review(body):
        if body.get("enable") is not True:
            raise ApiError("enable: true is required")
        where, field = body.get("path"), body.get("field")
        if not isinstance(where, list) or not isinstance(field, str):
            raise ApiError("path (a list) and field are required")
        base, expect = body.get("hash"), body.get("expect")
        if not isinstance(base, str) or not base:
            raise ApiError("hash is required (the config's hash when you looked at it)")
        if not (isinstance(expect, str) and len(expect) == SHA256_LEN and all(c in "0123456789abcdef" for c in expect)):
            raise ApiError("expect is required (from the review list)")
        path = ed.bot.config_path
        cfg, current_hash = _current(path)
        if base != current_hash:
            raise ApiError(CHANGED, 409)
        try:
            value = importer.enable_reviewed(cfg, where, field, expect)
        except LookupError:
            raise ApiError("no such key", 404) from None
        except importer.ReviewChanged:
            raise ApiError("that isn't what you reviewed any more; reload and look again", 409) from None
        try:
            new_hash = configio.save(path, cfg, current_hash, source="panel")
        except configio.Conflict:
            raise ApiError(CHANGED, 409) from None
        log.info("imported %s enabled", field)   # never the action itself, nor where it came from
        return {"ok": True, "hash": new_hash, "value": value}
