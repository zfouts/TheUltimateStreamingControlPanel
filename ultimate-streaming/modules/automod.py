"""Auto-mod: bans, times out or deletes Twitch chat messages that contain a keyword
(control panel: Twitch → Moderation). The terms and how they match: core/automod.py.

The chat module asks screen(msg) before it broadcasts each message. A match is never broadcast,
so it doesn't reach the BRB chat feed, the panel's chat, bot commands or chat stats; the ban,
timeout or delete then goes out through the linked Twitch account (modules/twitch.py) on this
module's own thread, so the chat reader never waits on the network. Without a linked account a
match is still hidden here, just not actioned on Twitch.

The broadcaster and moderators are never actioned; `exempt` (default vip) spares that level and up.
A user who is being banned or timed out has further messages hidden without a second call. The
last LOG_MAX actions are kept in memory for the panel, where a ban or timeout can be undone; a user
unbanned there isn't auto-actioned again until the daemon restarts.
"""
import collections
import copy
import queue
import threading
import time

from core import Module, configio
from core import text as textclean
from core.automod import (ACTIONS, EXEMPT, MATCHES, MAX_TERMS, MIN_ANYWHERE, MIN_WORD, SECONDS_MAX, SEEN_MAX,
                          TERM_MAX, Matcher, exempt_level, level, validate, words, canon)
from core.twitch_ids import USER_ID_RE
from core.webapi import ApiError

QUEUE_MAX = 200          # actions waiting for the worker; more are logged as failed
LOG_MAX = 100            # recent actions kept for the panel
TEXT_MAX = 200           # characters of the caught message kept in the log
DETAIL_MAX = 200
REPEAT = 60              # seconds: a user's further messages are only hidden while their ban is on its way
RECENT_MAX = 1000
RETRIES = 8              # attempts at an action Twitch rate-limits (429)
RETRY_WAIT_MAX = 5
SCOPES = {"ban": "moderator:manage:banned_users", "timeout": "moderator:manage:banned_users",
          "delete": "moderator:manage:chat_messages"}


def _detail(e):
    return textclean.clean(str(e), DETAIL_MAX) or "failed"


class AutoMod(Module):
    config_key = "automod"

    def __init__(self, bot):
        super().__init__(bot)
        self.now, self.wall = time.monotonic, time.time   # injectable clocks (tests)
        self.lock = threading.Lock()          # cfg, matcher, entries, recent, pardoned, caught
        self.save_lock = threading.Lock()     # one panel save at a time
        self.cfg, _ = validate(None)
        self.matcher = Matcher([])
        self.jobs = queue.Queue(maxsize=QUEUE_MAX)
        self.stopping = threading.Event()
        self.worker = None
        self.entries = collections.OrderedDict()   # id -> entry, oldest first
        self.next_id = 1
        self.recent = {}          # user id -> monotonic time its ban/timeout was queued
        self.pardoned = set()     # user ids unbanned from the panel this run
        self.caught = 0

    # ----- lifecycle

    def on_config(self, config):
        super().on_config(config)
        cfg, errors = validate(config)
        for e in errors:
            self.log.warning("automod: %s", e)
        with self.lock:
            self.cfg, self.matcher = cfg, Matcher(cfg["terms"])

    def on_load(self):
        self.stopping.clear()
        self.worker = threading.Thread(target=self._run, daemon=True, name="automod")
        self.worker.start()

    def on_unload(self):
        self.stopping.set()
        try:
            self.jobs.put_nowait(None)
        except queue.Full:
            pass
        worker = self.worker
        if worker and worker is not threading.current_thread():
            worker.join(timeout=2)

    # ----- screening (chat reader thread)

    def screen(self, msg):
        """True if `msg` matched a term: the chat module then drops it. Runs on the chat reader's
        thread, so only matching and bookkeeping happen here; Twitch is called from the worker."""
        if not isinstance(msg, dict) or not isinstance(msg.get("text"), str):
            return False
        with self.lock:
            cfg, matcher = self.cfg, self.matcher
        if not cfg["enabled"] or not matcher:
            return False
        uid = msg.get("user_id") if isinstance(msg.get("user_id"), str) else ""
        if level(msg) >= exempt_level(cfg["exempt"]) or (uid and uid == self._me_id()):
            return False
        rule = matcher.find(msg["text"])
        if rule is None:
            return False
        with self.lock:
            if uid in self.pardoned:
                return False
            self.caught += 1
            now = self.now()
            if rule["action"] != "delete" and uid:
                last = self.recent.get(uid)
                if last is not None and now - last < REPEAT:
                    return True          # their ban is already on its way: just hide this one
                if len(self.recent) >= RECENT_MAX:
                    self.recent = {k: t for k, t in self.recent.items() if now - t < REPEAT}
                self.recent[uid] = now
            entry = self._add(msg, rule)
        try:
            self.jobs.put_nowait(dict(entry))   # its own copy: the log may drop the entry before it runs
            self.log.info("automod: %s %s (matched \"%s\")", self._verb(entry), entry["login"] or entry["user"],
                          entry["term"])
        except queue.Full:
            self._finish(entry["id"], "failed", "too many at once; it was hidden but not actioned")
            return True
        self._publish(entry["id"])
        return True

    def _me_id(self):
        tw = self.bot.get_module("Twitch")
        user = getattr(tw, "user", None) if tw else None
        uid = user.get("id") if isinstance(user, dict) else None
        return uid if isinstance(uid, str) else None

    def _add(self, msg, rule):
        """A new log entry for a caught message (self.lock held)."""
        eid, self.next_id = self.next_id, self.next_id + 1
        user_id, mid = msg.get("user_id"), msg.get("id")
        entry = {"id": eid, "at": self.wall(),
                 "user": textclean.clean(msg.get("user") if isinstance(msg.get("user"), str) else "", 40),
                 "login": msg.get("login") if isinstance(msg.get("login"), str) else "",
                 "user_id": user_id if isinstance(user_id, str) and USER_ID_RE.fullmatch(user_id) else "",
                 "message_id": mid if isinstance(mid, str) else "",
                 "text": textclean.clean(msg["text"], TEXT_MAX),
                 "term": rule["term"], "action": rule["action"],
                 "seconds": rule["seconds"] if rule["action"] == "timeout" else None,
                 "state": "queued", "detail": None}
        self.entries[eid] = entry
        while len(self.entries) > LOG_MAX:
            self.entries.popitem(last=False)
        return entry

    @staticmethod
    def _verb(entry):
        return {"ban": "banning", "timeout": f"timing out ({entry['seconds']}s)", "delete": "deleting a message from"}[
            entry["action"]]

    # ----- the worker (Twitch calls)

    def _run(self):
        while not self.stopping.is_set():
            try:
                job = self.jobs.get(timeout=1)
            except queue.Empty:
                continue
            if job is None:
                break
            try:
                self._act(job)
            except Exception as e:      # never let one action stop the worker
                self.log.exception("automod: action failed")
                self._finish(job["id"], "failed", _detail(e), job.get("user_id"))

    def _act(self, entry):
        """Carry out one queued entry (a copy of the log entry, as queued)."""
        eid = entry["id"]
        tw = self.bot.get_module("Twitch")
        if not tw or not tw.linked():
            self._finish(eid, "failed", "Twitch isn’t linked, so it was only hidden", entry["user_id"])
            return
        action = entry["action"]
        if action == "delete" and not entry["message_id"]:
            self._finish(eid, "failed", "the message had no id")
            return
        if action != "delete" and not entry["user_id"]:
            self._finish(eid, "failed", "the message had no user id")
            return
        for attempt in range(RETRIES):
            try:
                if action == "delete":
                    tw.delete_message(entry["message_id"])
                else:
                    tw.ban(entry["user_id"], seconds=entry["seconds"], permanent=action == "ban",
                           reason=f"Auto-mod: matched \"{entry['term']}\""[:500])
            except ApiError as e:
                if e.status == 429 and attempt < RETRIES - 1:
                    wait = getattr(e, "retry_after", None)
                    wait = wait if isinstance(wait, (int, float)) and wait > 0 else 1
                    if self.stopping.wait(min(wait, RETRY_WAIT_MAX) + 0.05):
                        self._finish(eid, "failed", "the daemon stopped first", entry["user_id"])
                        return
                    continue
                text = str(e)
                if action != "delete" and "already banned" in text.lower():
                    self._finish(eid, "done", "already banned")
                else:
                    self.log.warning("automod: %s %s failed: %s", self._verb(entry), entry["login"] or entry["user"],
                                     _detail(e))
                    self._finish(eid, "failed", _detail(e), entry["user_id"])
                return
            self._finish(eid, "done", None)
            return

    def _finish(self, eid, state, detail, user_id=None):
        with self.lock:
            entry = self.entries.get(eid)
            if entry is not None:
                entry["state"], entry["detail"] = state, detail
                user_id = entry["user_id"]
            if state == "failed" and user_id:
                self.recent.pop(user_id, None)   # let their next message try again
        if entry is not None:
            self._publish(eid)

    def _publish(self, eid):
        hub = getattr(self.bot, "hub", None)
        view = self._view(eid)
        if hub is not None and view is not None:
            hub.publish("automod", {"entry": view})

    def _view(self, eid):
        with self.lock:
            e = self.entries.get(eid)
            if e is None:
                return None
            out = {k: v for k, v in e.items() if k != "message_id"}
        out["can_unban"] = out["action"] in ("ban", "timeout") and out["state"] == "done" and bool(out["user_id"])
        return out

    # ----- the panel (web request threads)

    def readiness(self):
        """{ready, detail}: whether a match can be actioned on Twitch right now (no network)."""
        chat = self.bot.get_module("Chat")
        tw = self.bot.get_module("Twitch")
        if chat is None or not getattr(chat, "channel", ""):
            return {"ready": False, "detail": "Set your Twitch channel (Twitch → Channel) so chat can be read."}
        if not tw or not tw.linked():
            return {"ready": False, "detail": "Link Twitch (Twitch → Channel) to ban and delete. Until then, "
                                              "matching messages are only hidden from your overlay and this panel."}
        try:
            have = set(tw.granted_scopes(network=False))
        except Exception:
            have = set(SCOPES.values())
        missing = sorted(set(SCOPES.values()) - have)
        if missing:
            return {"ready": False, "detail": "Link Twitch again to allow " + " and ".join(missing) + "."}
        if not getattr(chat, "connected", False):
            return {"ready": True, "detail": "Connecting to chat…"}
        return {"ready": True, "detail": None}

    def state(self):
        with self.lock:
            cfg = copy.deepcopy(self.cfg)
            ids = list(self.entries)[::-1]
            caught = self.caught
        return {"config": cfg, "status": {"enabled": cfg["enabled"], "caught": caught, **self.readiness()},
                "log": [v for v in map(self._view, ids) if v is not None],
                "limits": {"max_terms": MAX_TERMS, "term_max": TERM_MAX, "seconds_max": SECONDS_MAX,
                           "min_anywhere": MIN_ANYWHERE, "min_word": MIN_WORD, "actions": list(ACTIONS),
                           "matches": list(MATCHES), "exempt": list(EXEMPT)}}

    def test(self, text, section=None):
        """What `text` would trigger, without touching Twitch: against `section` (the panel's unsaved
        copy) when given, else the saved terms. Exemptions and the on/off switch are not applied."""
        if not isinstance(text, str) or not text.strip():
            raise ApiError("type a message to test")
        text = textclean.clean(text, 500)
        if section is None:
            with self.lock:
                cfg = copy.deepcopy(self.cfg)
        else:
            cfg, errors = validate(section)
            if errors:
                raise self._invalid(errors)
        rule = Matcher(cfg["terms"]).find(text)
        hit = None if rule is None else {k: rule[k] for k in ("term", "match", "action", "seconds")}
        return {"hit": hit, "seen": " ".join(words(canon(text), canonical=True))[:SEEN_MAX]}

    def unban(self, eid):
        """Undo the ban or timeout of log entry `eid`; that user isn't auto-actioned again this run."""
        if isinstance(eid, bool) or not isinstance(eid, int):
            raise ApiError("not a log entry id")
        view = self._view(eid)
        if view is None:
            raise ApiError("that entry is no longer in the log", 404)
        if not view["can_unban"]:
            raise ApiError("only a ban or timeout that went through can be undone")
        tw = self.bot.get_module("Twitch")
        if not tw:
            raise ApiError("the Twitch module isn't loaded", 503)
        try:
            tw.unban(view["user_id"])
        except ApiError as e:
            if "not banned" not in str(e).lower():   # a timeout that already ran out
                raise
        with self.lock:
            self.pardoned.add(view["user_id"])
            self.recent.pop(view["user_id"], None)
            for entry in self.entries.values():
                if entry["user_id"] == view["user_id"] and entry["state"] == "done" and entry["action"] != "delete":
                    entry["state"] = "unbanned"
            changed = [i for i, e in self.entries.items() if e["user_id"] == view["user_id"]]
        for i in changed:
            self._publish(i)
        self.log.info("automod: unbanned %s from the panel", view["login"] or view["user"])

    def save(self, section, base_hash):
        """The panel's save: validate (400 listing the problems), then write config.yaml against
        base_hash (409 if it changed since the panel loaded it)."""
        if not isinstance(section, dict):
            raise ApiError("config must be an object")
        if not isinstance(base_hash, str):
            raise ApiError("hash must be text")
        normalised, errors = validate(section)
        if errors:
            raise self._invalid(errors)
        with self.save_lock:
            path = self.bot.config_path
            try:
                cfg = configio.load(path)
            except FileNotFoundError:
                raise ApiError("there's no config.yaml yet; set up the deck first", 409) from None
            if not isinstance(cfg, dict):
                raise ApiError("config.yaml isn't a mapping; fix it in the Config view first")
            cfg["automod"] = copy.deepcopy(normalised)
            try:
                configio.save(path, cfg, base_hash)
            except configio.Conflict:
                raise ApiError("config.yaml changed since the panel loaded it; reload and try again", 409) from None
        self.on_config(normalised)
        return normalised

    @staticmethod
    def _invalid(errors):
        shown = errors[:20] + ([f"…and {len(errors) - 20} more"] if len(errors) > 20 else [])
        return ApiError("Fix these first: " + "; ".join(shown), 400, errors=errors)


def setup(bot):
    bot.add_module(AutoMod(bot))
