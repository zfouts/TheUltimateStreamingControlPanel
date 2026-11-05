"""Discord webhooks: one list of named webhooks (Settings → Discord) that features post to.

    discord:
      webhooks:                  # at most 20
        - id: golive             # [a-z0-9][a-z0-9_-]{0,31}, unique: what features store
          name: Go-live channel  # 1-40 characters, unique (ignoring case)

A webhook URL is a password (anyone holding it can post to the channel): it lives only in the
state dir's discord_webhooks.json (0600, written atomically, set from the control panel), never
in config.yaml, logs, backups or API responses. A url/webhook key in config.yaml is ignored
with a warning. Removing a webhook from the config removes its URL on the next panel save.

Other modules and plugins get URLs only through this module:

    discord = bot.get_module("Discord")
    discord.webhooks()            # [{"id", "name", "set", "host", "state", "detail"}]: never a URL
    discord.webhook_url(id)       # the URL, or None (unknown id, no URL saved, or broken)
    discord.name_of(id)           # its display name, or None
    discord.report(id, result)    # after posting: a core.webhooks Result updates its status;
                                  # "broken" (Discord says it's gone) sticks until the URL is saved again
    discord.is_broken(id)

A module that uses a webhook says so with an optional `discord_usage(self) -> [(webhook id,
label)]`, so the panel can show "Used by" and refuse to delete a webhook that is in use.
"""
import copy
import hashlib
import json
import logging
import os
import re
import threading
import time
import unicodedata

from core import Module, configio, webhooks
from core.paths import state_file
from core.webapi import ApiError

SECRETS_FILE = "discord_webhooks.json"
STATE_FILE = "discord_state.json"
ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
MAX_WEBHOOKS = 20
NAME_MAX = 40
LABEL_MAX = 80
DETAIL_MAX = 200
TEST_COOLDOWN = 10
TEST_MESSAGE = "Test message from Ultimate Streaming ✓"
URL_KEYS = ("url", "webhook", "webhook_url")
KINDS = ("ok", "retry", "broken", "failed")


# ----- config

def _name_ok(value):
    return (isinstance(value, str) and 0 < len(value) <= NAME_MAX
            and not any(unicodedata.category(c)[0] == "C" for c in value))


def validate(section):
    """(normalised discord section, [errors]). A webhook without a usable id is left out; a bad
    name falls back to the id (with an error). A url/webhook key is always an error (and
    ignored): webhook URLs are never kept in config.yaml. Messages never echo a URL."""
    errors = []
    out = {"webhooks": []}
    if section is None:
        return out, errors
    if not isinstance(section, dict):
        return out, ["discord must be a mapping"]
    for key in URL_KEYS:
        if key in section:
            errors.append(f"discord.{key} is ignored; webhook URLs are saved from the control panel "
                          "(Settings → Discord), never in config.yaml")
    items = section.get("webhooks")
    if items is None:
        items = []
    if not isinstance(items, list):
        errors.append("discord.webhooks must be a list")
        items = []
    if len(items) > MAX_WEBHOOKS:
        errors.append(f"discord.webhooks: at most {MAX_WEBHOOKS} (the rest are ignored)")
        items = items[:MAX_WEBHOOKS]
    ids, names = set(), set()
    for i, item in enumerate(items):
        where = f"webhooks[{i}]"
        if not isinstance(item, dict):
            errors.append(f"{where} must be a mapping")
            continue
        wid = item.get("id")
        if not isinstance(wid, str) or not ID_RE.fullmatch(wid):
            errors.append(f"{where}: id must be 1-32 lowercase letters, digits, _ or -, starting with a "
                          "letter or digit")
            continue
        where = f"webhooks[{i}] ({wid})"
        if wid in ids:
            errors.append(f"{where}: another webhook already has this id")
            continue
        ids.add(wid)
        for key in URL_KEYS:
            if key in item:
                errors.append(f"{where}: {key} is ignored here; webhook URLs are saved from the control panel, "
                              "never in config.yaml")
        name = item.get("name", wid)
        name = name.strip() if isinstance(name, str) else name
        if not _name_ok(name):
            errors.append(f"{where}: name must be 1-{NAME_MAX} characters on one line")
            name = wid
        if name.casefold() in names:
            errors.append(f"{where}: another webhook is already called {name!r}")
        names.add(name.casefold())
        out["webhooks"].append({"id": wid, "name": name})
    return out, errors


def fingerprint(url):
    """Identifies a stored URL in discord_state.json (to know a broken one was replaced) without
    keeping the URL there."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16] if isinstance(url, str) else None


# ----- the secret store

class SecretStore:
    """discord_webhooks.json: {webhook id: URL}, 0600, replaced atomically. Entries that aren't a
    valid id and Discord webhook URL are ignored when read."""

    def __init__(self, path):
        self.path = path

    def load(self, log=None):
        """{id: url}. `log`: where to warn about unusable entries (None: quietly, for the frequent
        status reads). A file other users can read is always made private, and that is logged."""
        name = os.path.basename(self.path)
        try:
            st = os.stat(self.path)
            if st.st_mode & 0o077:
                os.chmod(self.path, 0o600)
                (log or logging.getLogger("deck.Discord")).warning(
                    "discord: %s was readable by other users; made it private (0600)", name)
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            if log:
                log.warning("discord: can't read %s (%s); webhook URLs need saving again", name, type(e).__name__)
            return {}
        out = {}
        for wid, url in (data.items() if isinstance(data, dict) else ()):
            try:
                if isinstance(wid, str) and ID_RE.fullmatch(wid):
                    out[wid] = webhooks.validate("discord", url)
            except ValueError:
                if log:
                    log.warning("discord: the stored URL for %s isn't a Discord webhook URL; ignored", wid)
        return out

    def stage(self, urls):
        """Write urls to a private temp file next to the store; returns its path for commit()."""
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        tmp = f"{self.path}.tmp-{os.getpid()}-{threading.get_ident()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(urls, f, indent=1, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            self.discard(tmp)
            raise
        return tmp

    def commit(self, tmp):
        os.replace(tmp, self.path)

    @staticmethod
    def discard(tmp):
        if tmp:
            try:
                os.remove(tmp)
            except FileNotFoundError:
                pass


class Discord(Module):
    config_key = "discord"

    def __init__(self, bot):
        super().__init__(bot)
        # Injectable (tests): clocks and the HTTP post.
        self.wall, self.mono = time.time, time.monotonic
        self.http_post = None                 # None: requests.post
        self.store = SecretStore(state_file(SECRETS_FILE))
        self.state_path = state_file(STATE_FILE)
        # Lock order: never call another module while holding self.lock (they call in here too).
        self.lock = threading.RLock()         # cfg, results, testing, broken
        self.save_lock = threading.Lock()     # one panel save at a time
        self.cfg, _ = validate(None)
        self.results = {}                     # webhook id -> {"state", "at", "detail"}
        self.testing = set()                  # ids with a test post queued or running
        self.test_last = {}                   # id -> monotonic time of the last test request
        self.broken = self._load_state()      # id -> {"fp", "at", "detail"}

    # ----- lifecycle

    def on_config(self, config):
        super().on_config(config)
        cfg, errors = validate(config)
        for e in errors:
            self.log.warning("discord: %s", e)
        with self.lock:
            self.cfg = cfg
            ids = self._ids()
            for wid in [w for w in self.results if w not in ids]:
                self.results.pop(wid, None)

    def on_load(self):
        webhooks.install_log_filter()
        self.store.load(self.log)             # report an unreadable store or bad entries once

    # ----- state (discord_state.json: which webhooks Discord said are gone)

    def _load_state(self):
        broken = {}
        try:
            with open(self.state_path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return broken
        except (OSError, ValueError) as e:
            self.log.warning("discord: can't read %s (%s); starting fresh", STATE_FILE, type(e).__name__)
            return broken
        items = data.get("broken") if isinstance(data, dict) else None
        for wid, b in (items.items() if isinstance(items, dict) else ()):
            if isinstance(wid, str) and ID_RE.fullmatch(wid) and isinstance(b, dict) and isinstance(b.get("fp"), str):
                at = b.get("at")
                broken[wid] = {"fp": b["fp"][:64],
                               "at": at if isinstance(at, (int, float)) and not isinstance(at, bool) else None,
                               "detail": webhooks.clean(b.get("detail"), DETAIL_MAX) or None}
        return broken

    def _save_state(self):
        """Call with self.lock held. A failed write is logged; the in-memory state still applies."""
        try:
            configio.write_atomic(self.state_path,
                                  json.dumps({"broken": self.broken}, indent=1, sort_keys=True).encode("utf-8"))
        except OSError as e:
            self.log.warning("discord: can't save %s (%s)", STATE_FILE, type(e).__name__)

    # ----- lookups (with self.lock held)

    def _ids(self):
        return {w["id"] for w in self.cfg["webhooks"]}

    def _entry(self, wid):
        return next((w for w in self.cfg["webhooks"] if w["id"] == wid), None)

    def _broken(self, wid, url):
        b = self.broken.get(wid)
        return b if b and url and b["fp"] == fingerprint(url) else None

    def _status(self, wid, url):
        if wid in self.testing:
            return {"state": "sending", "at": None, "detail": None}
        b = self._broken(wid, url)
        if b:
            return {"state": "broken", "at": b["at"],
                    "detail": b["detail"] or "Discord says this webhook doesn't exist any more. Replace its URL."}
        if not url:
            return {"state": "never", "at": None, "detail": "No webhook URL saved yet."}
        if wid in self.results:
            return dict(self.results[wid])
        return {"state": "never", "at": None, "detail": None}

    # ----- the interface for other modules and plugins

    def webhooks(self):
        """[{"id", "name", "set", "host", "state", "detail"}] in config order; never a URL."""
        urls = self.store.load()
        out = []
        with self.lock:
            for w in self.cfg["webhooks"]:
                url = urls.get(w["id"])
                s = self._status(w["id"], url)
                out.append({"id": w["id"], "name": w["name"], "set": bool(url), "host": webhooks.host_of(url),
                            "state": s["state"], "detail": s["detail"]})
        return out

    def webhook_url(self, wid):
        """The webhook's URL, or None when the id is unknown, no URL is saved, or it is broken."""
        if not isinstance(wid, str) or not ID_RE.fullmatch(wid):
            return None
        url = self.store.load().get(wid)
        with self.lock:
            if not url or self._entry(wid) is None or self._broken(wid, url):
                return None
        return url

    def name_of(self, wid):
        with self.lock:
            entry = self._entry(wid) if isinstance(wid, str) else None
            return entry["name"] if entry else None

    def is_broken(self, wid):
        if not isinstance(wid, str) or not ID_RE.fullmatch(wid):
            return False
        url = self.store.load().get(wid)
        with self.lock:
            return self._entry(wid) is not None and self._broken(wid, url) is not None

    def report(self, wid, result, url=None):
        """A feature posted to webhook `wid` and got `result` (a core.webhooks Result). "ok" clears
        broken; "broken" marks the URL it was posted to broken (persisted) until it is replaced;
        anything else is shown as an error. `url` (optional): the URL that was posted to, so a
        result for a URL replaced in the meantime is ignored instead of blaming the new one."""
        self._record(wid, result, url)

    def _record(self, wid, result, url=None, ok_detail=None):
        kind = getattr(result, "kind", None)
        if not isinstance(wid, str) or not ID_RE.fullmatch(wid) or kind not in KINDS:
            return
        current = self.store.load().get(wid)
        if not current or (url is not None and url != current):
            self.log.debug("discord: a result for %s's previous URL was ignored", wid)
            return
        reason = webhooks.clean(getattr(result, "reason", None), DETAIL_MAX) or None
        with self.lock:
            entry = self._entry(wid)
            if entry is None:
                return
            now = self.wall()
            if kind == "ok":
                self.results[wid] = {"state": "ok", "at": now, "detail": ok_detail}
                if self.broken.pop(wid, None) is not None:
                    self._save_state()
            elif kind == "broken":
                self.broken[wid] = {"fp": fingerprint(current), "at": now, "detail": reason}
                self.results[wid] = {"state": "broken", "at": now, "detail": reason}
                self._save_state()
            else:
                self.results[wid] = {"state": "error", "at": now, "detail": reason or "The post failed."}
            name = entry["name"]
        if kind == "broken":
            self.log.warning("discord: the %s webhook is broken: %s", name, reason)

    def usage(self):
        """{webhook id: [label, ...]} from every loaded module's discord_usage(). Called without
        self.lock (the other modules take their own locks)."""
        out = {}
        for inst in list(getattr(self.bot, "modules", {}).values()):
            fn = getattr(inst, "discord_usage", None)
            if inst is self or not callable(fn):
                continue
            try:
                for item in fn() or ():
                    if not isinstance(item, (tuple, list)) or len(item) != 2:
                        continue
                    wid, label = item
                    if not isinstance(wid, str) or not ID_RE.fullmatch(wid):
                        continue
                    label = webhooks.clean(label, LABEL_MAX).strip() if isinstance(label, str) else ""
                    labels = out.setdefault(wid, [])
                    label = label or type(inst).__name__
                    if label not in labels:
                        labels.append(label)
            except Exception as e:
                self.log.debug("discord: %s.discord_usage failed (%s)", type(inst).__name__,
                               webhooks.clean(f"{type(e).__name__}: {e}"))
        return out

    # ----- test posts

    def test(self, wid):
        """Post TEST_MESSAGE to one webhook on a worker thread (even a broken one: a working test
        clears broken). One at a time per webhook, at most one every TEST_COOLDOWN seconds."""
        if not isinstance(wid, str) or not ID_RE.fullmatch(wid):
            raise ApiError("id must be a webhook id")
        url = self.store.load().get(wid)
        with self.lock:
            if self._entry(wid) is None:
                raise ApiError("no webhook with that id (save it first)", 404)
            if not url:
                raise ApiError("Save a URL for this webhook first.", 409)
            now = self.mono()
            last = self.test_last.get(wid)
            if wid in self.testing or (last is not None and now - last < TEST_COOLDOWN):
                raise ApiError("Wait a few seconds before testing again.", 429)
            self.testing.add(wid)
            self.test_last[wid] = now
        try:
            threading.Thread(target=self._run_test, args=(wid,), daemon=True, name="discord-test").start()
        except RuntimeError:
            with self.lock:
                self.testing.discard(wid)
            raise ApiError("Busy; try again in a minute.", 503) from None
        return {"queued": True}

    def _run_test(self, wid):
        try:
            url = self.store.load().get(wid)
            with self.lock:
                entry = self._entry(wid)
                name = entry["name"] if entry else wid
                if not url or entry is None:
                    self.results[wid] = {"state": "error", "at": self.wall(),
                                         "detail": "Its URL was removed before the test went out."}
                    return
            result = webhooks.post(url, {"content": TEST_MESSAGE, "allowed_mentions": {"parse": []}},
                                   http=self.http_post)
            self._record(wid, result, url, ok_detail="Test message sent.")
            if result.ok:
                self.log.info("discord: test message sent to %s", name)
            else:
                self.log.warning("discord: test message to %s failed: %s", name, result.reason)
        except Exception as e:      # never log.exception: a traceback could carry a webhook URL
            self.log.error("discord: test failed (%s)", webhooks.clean(f"{type(e).__name__}: {e}", DETAIL_MAX))
        finally:
            with self.lock:
                self.testing.discard(wid)

    # ----- panel

    def snapshot(self):
        """{"webhooks": {id: {state, at, detail}}} for the live status stream."""
        urls = self.store.load()
        with self.lock:
            return {"webhooks": {w["id"]: self._status(w["id"], urls.get(w["id"])) for w in self.cfg["webhooks"]}}

    def config_view(self):
        with self.lock:
            return copy.deepcopy(self.cfg)

    def urls_view(self):
        """{id: {"set": bool, "host": "discord.com" | None}} for every configured webhook; never a URL."""
        urls = self.store.load()
        with self.lock:
            ids = [w["id"] for w in self.cfg["webhooks"]]
        return {wid: {"set": bool(urls.get(wid)), "host": webhooks.host_of(urls.get(wid))} for wid in ids}

    def used_by(self):
        """{id: [label, ...]} for every configured webhook (an empty list when nothing uses it)."""
        usage = self.usage()
        with self.lock:
            ids = [w["id"] for w in self.cfg["webhooks"]]
        return {wid: usage.get(wid, []) for wid in ids}

    def save(self, section, urls, base_hash):
        """The panel's save: validate everything (400 listing the problems), refuse to remove a
        webhook something uses (400 naming the users), then stage the secret store, save
        config.yaml against base_hash (409 if it changed), and only then put the store in place.
        Ids absent from `urls` keep their URL; None removes it; a webhook no longer in the config
        loses its URL. A URL in `urls` (new, or the same one again) clears that webhook's "broken"."""
        if not isinstance(section, dict):
            raise ApiError("config must be an object")
        if urls is None:
            urls = {}
        if not isinstance(urls, dict):
            raise ApiError("urls must be an object")
        if not isinstance(base_hash, str):
            raise ApiError("hash must be text")
        normalised, errors = validate(section)
        ids = {w["id"] for w in normalised["webhooks"]}
        new_urls = {}
        for wid, url in urls.items():
            if not isinstance(wid, str) or not ID_RE.fullmatch(wid) or wid not in ids:
                errors.append(f"urls: {webhooks.clean(wid, 32)!r} isn't one of the webhooks")
                continue
            if url is None:
                new_urls[wid] = None
                continue
            try:
                new_urls[wid] = webhooks.validate("discord", url)
            except ValueError as e:
                errors.append(f"webhooks ({wid}): {e}")
        if errors:
            shown = errors[:20] + ([f"…and {len(errors) - 20} more"] if len(errors) > 20 else [])
            e = ApiError("Fix these first: " + "; ".join(shown), 400)
            e.errors = errors
            raise e
        with self.save_lock:
            with self.lock:
                old = {w["id"]: w["name"] for w in self.cfg["webhooks"]}
            in_use = [f"{old[wid]} is used by: {', '.join(labels)}; pick another webhook there first"
                      for wid, labels in self.usage().items() if labels and wid in old and wid not in ids]
            if in_use:
                raise ApiError("; ".join(in_use), 400)
            current = self.store.load(self.log)
            store = {wid: url for wid, url in current.items() if wid in ids}
            for wid, url in new_urls.items():
                if url is None:
                    store.pop(wid, None)
                else:
                    store[wid] = url
            tmp = self.store.stage(store)
            try:
                self._save_section(normalised, base_hash)
                self.store.commit(tmp)
                tmp = None
            finally:
                self.store.discard(tmp)
            with self.lock:
                changed = False
                for wid in list(self.broken):   # a URL submitted again (even the same one) means "it's fine now"
                    if wid not in store or wid in new_urls or self.broken[wid]["fp"] != fingerprint(store[wid]):
                        del self.broken[wid]
                        changed = True
                for wid in [w for w in self.results if w not in store or w in new_urls]:
                    self.results.pop(wid, None)
                if changed:
                    self._save_state()
        self.on_config(normalised)
        return normalised

    def _save_section(self, normalised, base_hash):
        path = self.bot.config_path
        try:
            cfg = configio.load(path)
        except FileNotFoundError:
            raise ApiError("there's no config.yaml yet; set up the deck first", 409) from None
        if not isinstance(cfg, dict):
            raise ApiError("config.yaml isn't a mapping; fix it in the Config view first")
        cfg["discord"] = copy.deepcopy(normalised)
        try:
            configio.save(path, cfg, base_hash)
        except configio.Conflict:
            raise ApiError("config.yaml changed since the panel loaded it; reload and try again", 409) from None


def setup(bot):
    bot.add_module(Discord(bot))
