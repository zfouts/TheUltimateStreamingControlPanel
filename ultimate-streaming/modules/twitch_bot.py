"""Twitch Bot: scheduled chat messages (timers), !commands and channel-point rewards that run
deck actions. It posts as the linked broadcaster account (modules/twitch.py).

    twitch_bot:
      enabled: false          # master switch for timers, commands and rewards
      only_when_live: true    # timers only post while live
      timers:   [{id, messages: [...], interval_minutes: 15, min_chat_lines: 5, announce, color}]
      commands: [{id, trigger: "!discord", aliases: [], response, permission, cooldown_seconds,
                  user_cooldown_seconds, reply: true}]
      rewards:  [{id, twitch_id, managed, title, cost, prompt, input_required, actions, revert,
                  revert_after_seconds, reply, allow_unsafe_actions, ...}]

Viewers are untrusted. Chat text is a template with a fixed set of placeholders, substituted in
one pass (render); a viewer's redemption input is only ever used as a color, parsed and
normalised, in the `color` field of obs_color / ha_light. Reward actions are allow-listed
(check_reward_actions) unless the reward says allow_unsafe_actions: true.
"""
import collections
import copy
import datetime
import glob
import heapq
import os
import re
import threading
import time
import unicodedata

from core import Module, action, chatcmd, listener, state, task
from core import source as option_source
from core.module import REWARD_DENIED
from core.schema import UI, Choice, Source
from core.webapi import ApiError
from core.twitch_ids import LOGIN_RE

from modules._twitch_bot_config import (CHAT_MAX, VALUE_MAX, ERROR_MAX, ID_RE, TRIGGER_RE, TWITCH_ID_RE,
    PLACEHOLDER_RE, RENDER_RE, ANSI_RE, CTRL_RE, LEAD_RE, OUT_LEAD_RE, LONG_RE, TIMER_PH, COMMAND_PH,
    REPLY_PH, BUILTINS, KIND_PH, EXTRA_DEFAULTS, ALL_PH, TRUSTED_VALUES, DEFAULT_EXCLUDE, MAX_EXCLUDE,
    CONFIG_ONLY, MONTHS, ANNOUNCE_COLORS, LEVELS, SAFE_ACTIONS, COLOR_ACTIONS, INPUT, MAX_TIMERS,
    MAX_COMMANDS, MAX_REWARDS, MAX_MESSAGES, MAX_ALIASES, MAX_ACTIONS, MAX_NESTING, DELAY_MAX,
    DELAY_TOTAL_MAX, ACTION_DEADLINE, Skipped, _strip_format, _clean, clean_value, render, render_list,
    _Fields, _number, _scan_input, _check_list, check_reward_actions, flatten, uses_input, _with_color,
    _list, _item, _timer, _trigger, _unloaded_kind, _command, _color, _actions, _reward, _stats_section,
    _default, DEFAULT_COMMANDS, validate, _level, _uptime, _num, _date, default_database_url, _dropped)

MSG_ID_RE = re.compile(r"[A-Za-z0-9-]{1,64}")
LOOKUP_RE = re.compile(r"@?([A-Za-z0-9_]{3,25})")
REQUIRED_SCOPES = ("user:write:chat", "moderator:manage:announcements", "channel:manage:redemptions")
REWARDS_PATH = "/channel_points/custom_rewards"

ABANDON_CEILING = 5           # an abandoned action blocks new runs for at most this many deadlines
MAX_ABANDONED = 3             # this many abandoned actions still running: refuse new runs until one returns
MAX_PENDING = 50              # queued redemptions; more are refunded
MAX_REFUNDS = 500             # refunds waiting for the worker; more stay in Twitch's queue
MAX_CHAT_JOBS = 50            # chat posts waiting for the sender; more are dropped
BOT_CHAT_CAP, BOT_CHAT_WINDOW = 10, 30   # the bot's share of Twitch's 20-per-30 s chat budget
THROTTLE_RETRY = 2            # seconds to retry Twitch's one-per-second chat throttle
UNLOAD_DEADLINE = 10          # seconds for the reverts run at unload, all together
TIMER_GAP = 60                # seconds between any two timer posts
LIVE_POLL = 60                # stream_status() fallback at most this often
LIVE_RECHECK = 300            # while EventSub says live, confirm with stream_status() this often
SAME_TEXT = 30                # identical bot text within this many seconds is suppressed
EVENTS_PER_SECOND = 5
USER_COOLDOWN_CAP = 5000


def _refusal(blocked):
    """Why a run is refused while an abandoned action still blocks (see TwitchBot._blocker)."""
    return f"backend not responding (an earlier {blocked['action']} action is still running)"


class _Job:
    """One chat post for the sender thread. make() renders the text there (it may do network
    lookups); done(job) runs after the attempt, with job.error None on success."""

    def __init__(self, make, reply_to=None, announce=False, color="primary", done=None):
        self.make, self.reply_to, self.announce, self.color, self.done = make, reply_to, announce, color, done
        self.error, self.mid, self.finished = None, None, threading.Event()


class _NoPost(Skipped):
    """A built-in that can't answer (chat stats off or broken): nothing is posted."""


class TwitchBot(Module):
    config_key = "twitch_bot"
    required_scopes = REQUIRED_SCOPES
    default_commands = DEFAULT_COMMANDS

    def __init__(self, bot):
        super().__init__(bot)
        self.now, self.wall = time.monotonic, time.time   # injectable clocks (tests)
        self.wait_step = 0.5                              # how often waits re-check the clock
        self.retry_step = 0.25                            # between retries of Twitch's chat throttle
        self.reply_wait = 3                               # a redemption waits this long for its reply
        self.deadline = ACTION_DEADLINE
        self.lock = threading.RLock()
        self.cfg, _ = validate({})
        self.triggers = {}
        self.paused = False
        self.loaded = False
        self.live_event, self.live_event_at = None, None   # from EventSub stream.online / offline
        self.live_polled, self.live_polled_at = None, None
        self.timer_state = {}          # timer id -> {last, lines, index, count, last_sent}
        self.last_timer_post = None
        self.cmd_last, self.user_last = {}, {}
        self.cmd_busy, self.user_busy = set(), set()       # a cooldown-gated reply is on its way
        self.counts = collections.Counter()   # (kind, id) -> uses this session
        self.own_ids = collections.deque(maxlen=100)
        self.own_texts = collections.deque(maxlen=100)   # (time, text) we posted
        self.bot_sent = collections.deque()              # times of bot posts (the 10-per-30 s cap)
        self.recent = collections.deque(maxlen=20)
        self.published = collections.deque()
        self.seen = collections.deque(maxlen=500)
        self.seen_set = set()
        self.channel_cache = (None, {})
        self.queue, self.refunds = collections.deque(), collections.deque()
        self.qcond = threading.Condition()
        self.busy = False
        self.abandoned = {}             # action thread still running past its deadline -> {action, at, since, released}
        self.sendq = collections.deque()
        self.scond = threading.Condition()
        self.sending = False
        self.reverts, self.rheap, self.rseq = {}, [], 0
        self.rcond = threading.Condition()
        self.revert_ticks = 0
        self.drained = False
        self.stopping = threading.Event()
        self.threads = []
        self.timer_thread = self.pause_thread = self.unload_thread = self.revert_thread = None
        self.pause_lock = threading.Lock()
        self.pause_applied = None       # (paused, twitch ids) last sent to Twitch in full, under pause_lock
        self.stats = None               # core.chatstats.ChatStats, once started
        self.stats_key = None           # the (enabled, url, password file) last asked for
        self.stats_built = None         # ... and the one self.stats was built for
        self.stats_error = None
        self.stats_factory = None       # ChatStats class (tests inject a fake)
        self.stats_thread = None
        self.stats_lock = threading.Lock()

    # ----- lifecycle

    @listener("chat_builtins_changed")
    def _builtins_changed(self):
        self.on_config(self.config)     # commands naming a just-(un)registered built-in switch on / off

    def on_config(self, config):
        super().on_config(config)
        cfg, errors = validate(config)
        for e in errors:
            self.log.warning("twitch_bot: %s (skipped)", e)
        triggers = {}
        for c in cfg["commands"]:
            if _unloaded_kind(c["builtin"]):
                self.log.debug("twitch_bot: %s uses built-in %s, which isn't loaded: skipped", c["trigger"],
                               _clean(c["builtin"], 24))
                continue
            for t in [c["trigger"]] + c["aliases"]:
                triggers[t] = c
        with self.lock:
            now = self.now()
            self.timer_state = {t["id"]: self.timer_state.get(t["id"]) or
                                {"last": now, "lines": 0, "index": 0, "count": 0, "last_sent": None}
                                for t in cfg["timers"]}
            self.triggers = triggers
        self._swap_cfg(cfg)
        if self.loaded:
            self._configure_stats()

    def _swap_cfg(self, cfg):
        """Use cfg; if that flips the master switch (after load), pause / unpause the rewards."""
        with self.lock:
            was, self.cfg = self.cfg["enabled"], cfg
        if self.loaded and was != cfg["enabled"]:
            self._pause_rewards()

    def on_load(self):
        for target, name in ((self._worker, "twitch-bot-rewards"), (self._sender, "twitch-bot-chat")):
            t = threading.Thread(target=target, daemon=True, name=name)
            t.start()
            self.threads.append(t)
        with self.rcond:
            self._start_revert_loop()
        self.loaded = True
        self._configure_stats()

    def on_unload(self):
        """Never blocks for long: reverts and refunds finish on a background thread."""
        self.stopping.set()
        with self.qcond:
            cleared, refunds = list(self.queue), list(self.refunds)
            self.queue.clear()
            self.refunds.clear()
            self.qcond.notify_all()
        with self.scond:
            jobs = list(self.sendq)
            self.sendq.clear()
            self.scond.notify_all()
        for job in jobs:
            self._finish_job(job, Skipped("the bot is stopping"))
        with self.rcond:
            reverts = sorted(self.reverts.items(), key=lambda kv: kv[1][0])
            self.reverts.clear()
            self.rheap.clear()
            self.drained = True
            self.rcond.notify_all()
        for t in self.threads + ([self.stats_thread] if self.stats_thread else []):
            t.join(timeout=0.2)
        refunds += [i for i in cleared if i["reward"]["managed"] and not i["reward"]["skip_queue"]]
        if cleared:
            self.log.info("%d queued channel-point redemptions were not run; refunding them", len(cleared))
        if reverts or refunds or self.stats or (self.stats_thread and self.stats_thread.is_alive()):
            self.unload_thread = threading.Thread(target=self._finish_unload, args=(reverts, refunds),
                                                  daemon=True, name="twitch-bot-unload")
            self.unload_thread.start()

    def _finish_unload(self, reverts, refunds):
        end = self.now() + UNLOAD_DEADLINE
        for rid, job in reverts:
            left = end - self.now()
            if left <= 0:
                self.log.warning("unloading: no time left for the revert of %s", clean_value(job[3]))
                continue
            self._run_revert(rid, job, deadline=left)
        for item in refunds:
            self._set_status(item, "CANCELED")
        with self.stats_lock:          # waits for a rebuild in progress; it won't start a new one now
            stats, self.stats = self.stats, None
        if stats:
            try:
                stats.close()
            except Exception as e:
                self.log.warning("closing chat stats: %s", type(e).__name__)

    def idle(self):
        """Nothing queued or running in the reward worker or the chat sender."""
        with self.qcond:
            worker = not (self.queue or self.refunds or self.busy)
        with self.scond:
            return worker and not (self.sendq or self.sending)

    # ----- chat stats

    def _stats_desired(self):
        """((enabled, database_url, database_password_file), exclude) the config asks for now."""
        cfg = self.cfg
        return (cfg["stats"]["enabled"], cfg["database_url"], cfg["database_password_file"]), \
            list(cfg["stats"]["exclude"])

    def _configure_stats(self):
        """(Re)build the ChatStats store when stats.enabled or the database settings changed, off
        the calling thread; otherwise just pass on the exclude list."""
        key, exclude = self._stats_desired()
        if key == self.stats_key:
            stats = self.stats
            if stats:
                try:
                    stats.set_exclude(exclude)
                except Exception as e:
                    self.log.debug("stats exclude: %s", type(e).__name__)
            return
        self.stats_key = key

        def work():
            with self.stats_lock:
                key, exclude = self._stats_desired()     # the newest wish, not the one that queued us
                if self.stats is not None and self.stats_built == key:
                    self.stats.set_exclude(exclude)
                    return
                old, self.stats, self.stats_built = self.stats, None, None
                if old:
                    try:
                        old.close()
                    except Exception as e:
                        self.log.warning("closing chat stats: %s", type(e).__name__)
                if not key[0] or self.stopping.is_set():
                    return
                try:
                    from core.util import read_secret
                    password = read_secret("DECK_DATABASE_PASSWORD", key[2] or None)
                    factory = self.stats_factory
                    if factory is None:
                        from core.chatstats import ChatStats as factory
                    stats = factory(key[1] or default_database_url(), password=password, exclude=exclude)
                    stats.start()
                except Exception as e:     # never str(e): it could carry the database URL
                    self.stats_error = f"chat stats couldn't start ({type(e).__name__})"
                    self.log.error("%s", self.stats_error)
                    return
                self.stats_error = None
                self.stats, self.stats_built = stats, key
        self.stats_thread = threading.Thread(target=work, daemon=True, name="twitch-bot-stats")
        self.stats_thread.start()

    def _recording(self):
        cfg, stats = self.cfg, self.stats
        return stats if cfg["enabled"] and cfg["stats"]["enabled"] and stats else None

    def _record(self, kind, user_id, login, display, *args):
        """record_message / record_command / record_redemption: in memory, O(1), any thread."""
        stats = self._recording()
        if not stats:
            return
        login = (login or "").strip().lower()
        if login in self.cfg["stats"]["exclude"]:
            return
        try:
            getattr(stats, "record_" + kind)(str(user_id or ""), login, display or "", *args)
        except Exception as e:
            self.log.debug("stats record: %s", type(e).__name__)

    def _stats_ready(self):
        """The ChatStats store for a query (sender / API threads), or _NoPost."""
        stats = self.stats
        if not self.cfg["stats"]["enabled"] or stats is None:
            raise _NoPost(self.stats_error or "chat stats are off")
        try:
            error = (stats.describe() or {}).get("error")
        except Exception as e:
            error = type(e).__name__
        if error:
            raise _NoPost(f"chat stats aren't working: {_clean(error)}")
        return stats

    @staticmethod
    def _query(fn, *args, **kw):
        """A ChatStats query; a failure is reported by type only (its text could carry the URL)."""
        try:
            return fn(*args, **kw)
        except Exception as e:
            raise _NoPost(f"chat stats query failed ({type(e).__name__})") from None

    # ----- helpers

    def _twitch(self, required=False):
        tw = self.bot.get_module("Twitch")
        if tw and tw.linked():
            return tw
        if required:
            raise ApiError("Twitch isn’t linked" if tw else "the Twitch module isn't loaded", 401 if tw else 503)
        return None

    def _event(self, kind, title, user=None, ok=True, error=None):
        ev = {"ts": self.wall(), "kind": kind, "title": clean_value(title), "user": clean_value(user) if user else None,
              "ok": bool(ok), "error": _clean(error) if error else None}
        (self.log.info if ok else self.log.warning)("%s %s%s: %s", kind, ev["title"],
                                                    f" by {ev['user']}" if ev["user"] else "",
                                                    "ok" if ok else ev["error"])
        now = self.now()
        with self.lock:
            self.recent.append(ev)
            while self.published and now - self.published[0] >= 1:
                self.published.popleft()
            if len(self.published) >= EVENTS_PER_SECOND:
                return ev       # coalesced: it is still in `recent`
            self.published.append(now)
        hub = getattr(self.bot, "hub", None)
        if hub:
            try:
                hub.publish("bot", ev)
            except Exception as e:
                self.log.debug("bot event publish: %s", _clean(e))
        return ev

    # ----- chat: one sender thread for every bot post

    def _submit(self, job):
        """Queue a chat post; False (job finished with Skipped) if the queue is full."""
        with self.scond:
            full = self.stopping.is_set() or len(self.sendq) >= MAX_CHAT_JOBS
            if not full:
                self.sendq.append(job)
                self.scond.notify()
        if full:
            self.log.warning("bot chat queue is full; a message was dropped")
            self._finish_job(job, Skipped("too many bot messages queued"))
        return not full

    def _finish_job(self, job, error=None):
        job.error = error
        try:
            if job.done:
                job.done(job)
        except Exception:
            self.log.exception("bot chat callback failed")
        finally:
            job.finished.set()

    def _sender(self):
        while not self.stopping.is_set():
            with self.scond:
                while not self.sendq and not self.stopping.is_set():
                    self.scond.wait(5)
                if self.stopping.is_set():
                    return
                job = self.sendq.popleft()
                self.sending = True
            try:
                error = None
                try:
                    job.mid = self._say(job.make(), job.reply_to, job.announce, job.color)
                except Exception as e:
                    error = e
                self._finish_job(job, error)
            finally:
                with self.scond:
                    self.sending = False
                    self.scond.notify_all()

    def _send_wait(self, job, timeout=15):
        """Submit and wait for the attempt; raises its error. False if it's still queued after timeout."""
        self._submit(job)
        if not job.finished.wait(timeout):
            return False
        if job.error:
            raise job.error
        return True

    def _post(self, tw, text, reply_to, announce, color):
        """send_chat / announce, retrying Twitch's one-per-second throttle for up to 2 s.
        The shared 20-per-30 s budget (also a 429) is not retried."""
        tries = max(1, int(THROTTLE_RETRY / max(self.retry_step, 1e-6)))
        for attempt in range(tries + 1):
            try:
                if announce:
                    tw.announce(text, color)
                    return None
                return tw.send_chat(text, reply_to=reply_to)
            except ApiError as e:
                if getattr(e, "status", None) == 429 and getattr(e, "retry_after", None) is not None and attempt < tries \
                        and not self.stopping.is_set():
                    self.stopping.wait(self.retry_step)
                    continue
                raise

    def _say(self, text, reply_to=None, announce=False, color="primary"):
        """Post text as the linked account. Raises Skipped for a duplicate, the bot's own
        10-per-30 s cap, or Twitch's rate limit."""
        tw = self._twitch(required=True)
        text = OUT_LEAD_RE.sub("", text or "")[:CHAT_MAX]
        if not text:
            raise Skipped("nothing to say")
        now = self.now()
        with self.lock:
            while self.own_texts and now - self.own_texts[0][0] >= SAME_TEXT:
                self.own_texts.popleft()
            while self.bot_sent and now - self.bot_sent[0] >= BOT_CHAT_WINDOW:
                self.bot_sent.popleft()
            if any(t == text for _, t in self.own_texts):
                raise Skipped(f"same message already sent in the last {SAME_TEXT}s")
            if len(self.bot_sent) >= BOT_CHAT_CAP:
                raise Skipped(f"the bot already posted {BOT_CHAT_CAP} messages in the last {BOT_CHAT_WINDOW}s")
            entry = (now, text)
            self.own_texts.append(entry)
            self.bot_sent.append(now)
        try:
            mid = self._post(tw, text, reply_to, announce, color)
        except Exception as e:
            with self.lock:
                if entry in self.own_texts:
                    self.own_texts.remove(entry)
                if now in self.bot_sent:
                    self.bot_sent.remove(now)
            if isinstance(e, ApiError) and getattr(e, "status", None) == 429:
                raise Skipped(f"chat rate limit: {_clean(e)}") from None
            raise
        if mid:
            with self.lock:
                self.own_ids.append(mid)
        return mid

    def _me_id(self):
        tw = self.bot.get_module("Twitch")
        return (getattr(tw, "user", None) or {}).get("id")

    def _is_own(self, msg):
        """A chat message this bot posted (by message id, or the same text from our own account
        before send_chat returned its id)."""
        mid = msg.get("id")
        with self.lock:
            if mid and mid in self.own_ids:
                return True
            me = self._me_id()
            if me and msg.get("user_id") == me:
                text, now = (msg.get("text") or "").strip(), self.now()
                return any(t == text and now - ts < SAME_TEXT for ts, t in self.own_texts)
        return False

    def _channel_values(self, names):
        """{channel, uptime, game, title} for a template that uses any of them; "" when unknown."""
        out = {"channel": "", "uptime": "", "game": "", "title": ""}
        tw = self.bot.get_module("Twitch")
        if not tw:
            return out
        try:
            out["channel"] = getattr(tw, "channel", "") or (tw.me()["display_name"] if tw.linked() else "")
        except Exception as e:
            self.log.debug("channel name: %s", _clean(e))
        if not (names & {"uptime", "game", "title"}) or not tw.linked():
            return out
        try:
            st = tw.stream_status()
            if st.get("live"):
                out.update(uptime=_uptime(st.get("started_at")), game=st.get("game_name") or "",
                           title=st.get("title") or "")
                return out
            out["uptime"] = "offline"
            if names & {"game", "title"}:
                at, info = self.channel_cache
                if at is None or self.now() - at > 60:
                    info = tw.channel_info()
                    self.channel_cache = (self.now(), info)
                out.update(game=info.get("game_name") or "", title=info.get("title") or "")
        except Exception as e:
            self.log.debug("stream info for a chat message: %s", _clean(e))
        return out

    def _values(self, template, user=None, count=0, color=None):
        names = set(PLACEHOLDER_RE.findall(template or ""))
        values = {"count": count}
        if user is not None:
            values["user"] = user
        if color is not None:
            values["color"] = color
        if names & {"channel", "uptime", "game", "title"}:
            values.update(self._channel_values(names))
        return values

    def _edit_config(self, edit, base=None):
        """Load config.yaml, edit(twitch_bot section) and save it against the hash it was loaded
        with; if someone else saved in between, reload and apply the edit again, once. With
        `base` (the hash the caller's view came from) a mismatch is a 409 instead, never merged."""
        from core import configio
        path = self.bot.config_path
        for attempt in (1, 2):
            this_base = configio.file_hash(path) if base is None else base
            cfg = configio.load(path)
            section = cfg.get("twitch_bot")
            if not isinstance(section, dict):
                section = cfg["twitch_bot"] = {}
            edit(section)
            try:
                return configio.save(path, cfg, this_base)
            except configio.Conflict:
                if base is not None:
                    raise ApiError("config.yaml changed since the panel loaded it; reload and try again",
                                   409) from None
                if attempt == 2:
                    raise
                self.log.info("config.yaml changed while saving the Twitch Bot; merging")

    def _find(self, kind, item_id):
        return next((x for x in self.cfg[kind] if x["id"] == item_id), None)

    # ----- master switch

    def set_enabled(self, on):
        if not isinstance(on, bool):
            raise ApiError("on must be true or false")
        self._edit_config(lambda section: section.__setitem__("enabled", on))
        self._swap_cfg({**self.cfg, "enabled": on})
        return on

    def _pause_rewards(self):
        """Pause (bot off) or unpause (bot on) the managed rewards on Twitch, in the background.
        Threads take pause_lock in no particular order, so each reads the master switch once it
        holds the lock: whichever applies last applies the current state, and one that finds that
        state already applied sends nothing."""
        if not any(r["managed"] and r["twitch_id"] for r in self.cfg["rewards"]):
            return

        def work():
            with self.pause_lock:
                cfg = self.cfg
                paused = not cfg["enabled"]
                rewards = [r for r in cfg["rewards"] if r["managed"] and r["twitch_id"]]
                ids = frozenset(r["twitch_id"] for r in rewards)
                applied = self.pause_applied
                if not rewards or (applied and applied[0] == paused and ids <= applied[1]):
                    return
                tw = self._twitch()
                if not tw:
                    return
                self.pause_applied = None       # until every reward has the new state
                failed = False
                for r in rewards:
                    try:
                        tw.api("PATCH", REWARDS_PATH, params={"broadcaster_id": tw.me()["id"], "id": r["twitch_id"]},
                               json={"is_paused": paused})
                    except Exception as e:
                        failed = True
                        self._event("error", r["title"], ok=False,
                                    error=f"couldn't {'pause' if paused else 'unpause'} it on Twitch: {_clean(e)}")
                if not failed:
                    self.pause_applied = (paused, ids)
        self.pause_thread = threading.Thread(target=work, daemon=True, name="twitch-bot-pause")
        self.pause_thread.start()

    # ----- live state

    @listener("stream_online")
    def on_stream_online(self, *_):
        with self.lock:
            self.live_event, self.live_event_at = True, self.now()
            self.counts.clear()        # {count} is per stream session
        stats = self._recording()
        if stats:
            try:
                stats.new_stream()
            except Exception as e:
                self.log.debug("stats new_stream: %s", type(e).__name__)

    @listener("stream_offline")
    def on_stream_offline(self, *_):
        with self.lock:
            self.live_event, self.live_event_at = False, self.now()

    @listener("eventsub_session")
    def on_eventsub_session(self, *_):
        """A new EventSub session may have missed online/offline: poll until the next one."""
        with self.lock:
            self.live_event = None

    def _is_live(self, tw):
        es = self.bot.get_module("EventSub")
        now = self.now()
        if self.live_event is not None:
            try:
                connected = es is None or bool((es.snapshot() or {}).get("connected"))
            except Exception:
                connected = False
            if connected:
                if self.live_event and now - (self.live_event_at or now) >= LIVE_RECHECK:
                    self.live_event_at = now
                    try:
                        if not tw.stream_status().get("live"):
                            self.live_event, self.live_polled, self.live_polled_at = None, False, now
                    except Exception as e:
                        self.log.debug("stream status: %s", _clean(e))
                if self.live_event is not None:
                    return self.live_event
        if self.live_polled_at is None or now - self.live_polled_at >= LIVE_POLL:
            self.live_polled_at = now
            try:
                self.live_polled = bool(tw.stream_status().get("live"))
            except Exception as e:
                self.log.debug("stream status: %s", _clean(e))
        return bool(self.live_polled)

    def _live_view(self):
        return self.live_event if self.live_event is not None else self.live_polled

    # ----- timers

    @task(every=5, on_page_change=False)
    def timers_task(self):
        """Decides off the deck's main loop: the work (stream status, chat post) is network."""
        cfg = self.cfg
        if not cfg["enabled"] or self.paused or not cfg["timers"]:
            return
        if self.timer_thread and self.timer_thread.is_alive():
            return
        self.timer_thread = threading.Thread(target=self.run_timers, daemon=True, name="twitch-bot-timers")
        self.timer_thread.start()

    def run_timers(self):
        """One pass: post the due timer that waited longest, if any. Returns its id, or None."""
        cfg = self.cfg
        if not cfg["enabled"] or self.paused:
            return None
        tw = self._twitch()
        if not tw or (cfg["only_when_live"] and not self._is_live(tw)):
            return None
        now = self.now()
        with self.lock:
            if self.last_timer_post is not None and now - self.last_timer_post < TIMER_GAP:
                return None
            due = [t for t in cfg["timers"] if t["enabled"] and t["id"] in self.timer_state
                   and now - self.timer_state[t["id"]]["last"] >= t["interval_minutes"] * 60
                   and self.timer_state[t["id"]]["lines"] >= t["min_chat_lines"]]
            if not due:
                return None
            t = min(due, key=lambda t: self.timer_state[t["id"]]["last"])
        try:
            return t["id"] if self._send_wait(self._timer_job(t)) else None
        except Exception:
            return None

    def _timer_job(self, t):
        """A chat job for timer t's next message; the 60 s gap is claimed now, either way."""
        with self.lock:
            st = self.timer_state.setdefault(t["id"], {"last": self.now(), "lines": 0, "index": 0, "count": 0,
                                                      "last_sent": None})
            i = st["index"] % len(t["messages"])
            count = st["count"] + 1
            self.last_timer_post = self.now()
        template = t["messages"][i]

        def done(job):
            with self.lock:
                if job.error is None:
                    st.update(last=self.now(), lines=0, index=i + 1, count=count, last_sent=self.wall())
                elif not isinstance(job.error, Skipped):
                    st["last"] = self.now()     # a broken timer waits its interval, not every pass
            self._event("timer", t["id"], ok=job.error is None, error=job.error)
        return _Job(lambda: render(template, self._values(template, count=count)),
                    announce=t["announce"], color=t["color"], done=done)

    def send_timer(self, timer_id):
        """Post a timer now, ignoring its interval, chat-activity and live gates (the chat caps
        and the duplicate check still apply)."""
        t = self._find("timers", timer_id) if isinstance(timer_id, str) else None
        if not t:
            raise ApiError(f"no timer {_clean(timer_id, 32)!r}", 404)
        self._twitch(required=True)
        try:
            self._send_wait(self._timer_job(t))
        except Skipped as e:
            raise ApiError(str(e), 429) from None

    # ----- commands

    @listener("chat_message")
    def on_chat(self, msg):
        """Runs on the chat reader's thread: only matching and bookkeeping here, never network."""
        if not isinstance(msg, dict) or not isinstance(msg.get("text"), str):
            return
        if self._is_own(msg):
            return
        badges = msg.get("badges") or []
        me = self._me_id()
        broadcaster = "broadcaster" in badges or bool(me and msg.get("user_id") == me)
        if not broadcaster:
            with self.lock:
                for st in self.timer_state.values():
                    st["lines"] = min(st["lines"] + 1, 1_000_000)
        user = msg.get("user") if isinstance(msg.get("user"), str) else ""
        if not broadcaster or self.cfg["stats"]["include_broadcaster"]:
            login = msg.get("login") if isinstance(msg.get("login"), str) and msg.get("login") else user
            self._record("message", msg.get("user_id"), login, user)
        if self.cfg["enabled"]:
            self._command(msg)

    def _command(self, msg):
        words = msg["text"].strip().split(None, 1)
        if not words or not words[0].startswith("!"):
            return
        cmd = self.triggers.get(words[0].lower())
        if not cmd or not cmd["enabled"]:
            return
        if _level(msg) < LEVELS.index(cmd["permission"]):
            return
        user = msg.get("user") if isinstance(msg.get("user"), str) else ""
        cid, ukey = cmd["id"], (cmd["id"], msg.get("user_id") or user.lower())
        global_cd, user_cd = cmd["cooldown_seconds"], cmd["user_cooldown_seconds"]
        now = self.now()
        with self.lock:
            last, ulast = self.cmd_last.get(cid), self.user_last.get(ukey)
            if last is not None and now - last < global_cd or ulast is not None and now - ulast < user_cd:
                return
            if (global_cd and cid in self.cmd_busy) or (user_cd and ukey in self.user_busy):
                return              # a reply is already on its way
            if global_cd:
                self.cmd_busy.add(cid)
            if user_cd:
                self.user_busy.add(ukey)
        mid = msg.get("id")
        reply_to = mid if cmd["reply"] and isinstance(mid, str) and MSG_ID_RE.fullmatch(mid) else None
        arg = words[1].split(None, 1)[0] if len(words) > 1 else ""     # used for lookups, never echoed
        login = msg.get("login") if isinstance(msg.get("login"), str) and msg.get("login") else user.lower()
        asker = {"user": user, "login": login, "user_id": msg.get("user_id") or "", "level": _level(msg)}

        def make():   # on the sender thread: lookups (stream status, chat stats) may block here
            return self._command_text(cmd, asker, arg, self.counts[("command", cid)] + 1)[0]

        def done(job):
            with self.lock:
                self.cmd_busy.discard(cid)
                self.user_busy.discard(ukey)
                if job.error is None or _dropped(job.error):   # only then does a cooldown start
                    t = self.now()
                    if global_cd:
                        self.cmd_last[cid] = t
                    if user_cd:
                        self._remember_user(ukey, t)
                if job.error is None:
                    self.counts[("command", cid)] += 1
            if job.error is None:
                self._record("command", asker["user_id"], asker["login"], user)
            self._event("error" if isinstance(job.error, _NoPost) else "command", cid, user,
                        ok=job.error is None, error=job.error)
        self._submit(_Job(make, reply_to=reply_to, done=done))

    def _command_text(self, cmd, asker, arg, count, preview=False):
        """(message, sample) for cmd (text or built-in). Only stored names, the bot's own data and
        fixed words reach the output: the viewer's arg only selects what is looked up. preview
        (the panel's Test) never gives up: missing stats become sample numbers (sample True)."""
        template, kind = cmd["response"], cmd["builtin"]
        if kind is None:
            return render(template, self._values(template, user=asker["user"], count=count)), False
        if kind == "uptime":
            try:
                live = bool(self._twitch(required=True).stream_status().get("live"))
            except Exception:
                if not preview:
                    raise
                live = False
            template = template if live else cmd["offline_response"]
            return render(template, self._values(template, count=count)), False
        if kind == "commands":
            triggers = [c["trigger"] for c in self.cfg["commands"]
                        if c["enabled"] and asker["level"] >= LEVELS.index(c["permission"])]
            return render_list(template, self._values(template, user=asker["user"], count=count),
                               triggers, ", "), False
        if kind not in BUILTINS:
            return self._plugin_text(kind, template, asker, arg, count, preview)
        try:
            stats = self._stats_ready()
        except _NoPost:
            if not preview:
                raise
            stats = None
        if kind == "top10":
            period = "stream" if arg.lower() == "stream" else "all"
            rows = (self._query(stats.top, n=cmd["count"], period=period) or []) if stats else []
            entries = [f"{int(r.get('rank') or i + 1)}. {clean_value(r.get('display') or r.get('login'), cap=64)} "
                       f"({_num(r.get('messages'))})" for i, r in enumerate(rows)]
            values = {**self._values(template, count=count), "period": "this stream" if period == "stream" else "all time"}
            return render_list(template, values, entries, " · ", empty="nobody yet"), False
        row, sample = (self._stats_row(stats, asker, arg) if stats else None), False
        if row is None and preview:
            top = (self._query(stats.top, n=1) or []) if stats else []
            row = self._query(stats.user, user_id=top[0]["user_id"]) if top else None
            if row is None:
                row, sample = {"display": asker["user"], "messages": 1234, "rank": 1, "stream_messages": 56,
                               "stream_rank": 1, "first_seen": self.wall(), "redemptions": 3, "commands": 12}, True
        if not row:
            template = cmd["missing_response"]
            return render(template, self._values(template, user=asker["user"], count=count)), False
        values = self._values(template, user=row.get("display") or row.get("login") or "", count=count)
        values.update(messages=_num(row.get("messages")), rank=_num(row.get("rank")),
                      stream_messages=_num(row.get("stream_messages")),
                      stream_rank=_num(row["stream_rank"]) if row.get("stream_rank") else "-",
                      first_seen=_date(row.get("first_seen")), redemptions=_num(row.get("redemptions")),
                      commands=_num(row.get("commands")))
        return render(template, values), sample

    def _plugin_text(self, kind, template, asker, arg, count, preview):
        """(message, sample) for a built-in another module registered (core/chatcmd.py). Its values
        are untrusted: only declared placeholders are taken, and render() cleans each one."""
        b = chatcmd.get(kind)
        if b is None:
            raise _NoPost(f"built-in {_clean(kind, 24)} isn't loaded")
        sample = False
        try:
            returned = b.values(arg)
        except Exception:
            if not preview:
                raise
            returned, sample = b.sample, True
        values = self._values(template, user=asker["user"], count=count)
        values.update({k: clean_value(v) for k, v in (returned or {}).items() if k in b.placeholders})
        return render(template, values), sample

    def _stats_row(self, stats, asker, arg):
        """The stats row !stats shows: @login (if lookups are allowed), else the asker's own."""
        m = LOOKUP_RE.fullmatch(arg) if self.cfg["stats"]["allow_lookup"] else None
        if m:
            return self._query(stats.user, login=m.group(1).lower())
        if re.fullmatch(r"[0-9]{1,20}", str(asker["user_id"])):
            return self._query(stats.user, user_id=str(asker["user_id"]))
        login = (asker.get("login") or asker["user"]).lower()
        return self._query(stats.user, login=login) if LOGIN_RE.fullmatch(login) else None

    def _remember_user(self, ukey, t):
        if len(self.user_last) >= USER_COOLDOWN_CAP:
            self.user_last = {k: v for k, v in self.user_last.items()
                              if t - v < (self._find("commands", k[0]) or {}).get("user_cooldown_seconds", 0)}
            if len(self.user_last) >= USER_COOLDOWN_CAP:
                self.user_last.clear()
        self.user_last[ukey] = t

    def command_test(self, command_id, user=None):
        """{text[, sample: true]}: what `command_id` would post, rendered like live (built-ins
        too) but here and now, without posting it."""
        cmd = self._find("commands", command_id) if isinstance(command_id, str) else None
        if not cmd:
            raise ApiError(f"no command {_clean(command_id, 32)!r}", 404)
        if user is None or user == "":
            user = "TestViewer"
        if not isinstance(user, str) or len(user) > VALUE_MAX:
            raise ApiError(f"user must be text of at most {VALUE_MAX} characters")
        if _unloaded_kind(cmd["builtin"]):
            raise ApiError(f"the built-in {_clean(cmd['builtin'], 24)} isn't loaded")
        count = self.counts[("command", cmd["id"])] + 1
        text, sample = self._command_text(cmd, {"user": user, "login": user.lower(), "user_id": "", "level": 0}, "", count, preview=True)
        return {"text": text, **({"sample": True} if sample else {})}

    # ----- rewards

    @listener("reward_redemption")
    def on_redemption(self, ev):
        """Runs on the EventSub thread: queue it (or queue its refund), never network here."""
        if not isinstance(ev, dict):
            return
        cfg = self.cfg
        if not cfg["enabled"]:
            return
        rid, reward_id = ev.get("id"), ev.get("reward_id")
        if not isinstance(rid, str) or not TWITCH_ID_RE.fullmatch(rid) or not isinstance(reward_id, str):
            return
        reward = next((r for r in cfg["rewards"] if r["enabled"] and r["twitch_id"] == reward_id), None)
        if not reward:
            self.log.debug("redemption of a reward the bot doesn't handle")
            return
        status = ev.get("status")
        if not (status == "unfulfilled" or (reward["skip_queue"] and status == "fulfilled")):
            return
        with self.lock:
            if rid in self.seen_set:
                return
            if len(self.seen) == self.seen.maxlen:
                self.seen_set.discard(self.seen[0])
            self.seen.append(rid)
            self.seen_set.add(rid)
        user = ev.get("user") or ev.get("user_login") or ""
        text = ev.get("input") if isinstance(ev.get("input"), str) else ""
        item = {"id": rid, "reward": reward, "user": str(user)[:VALUE_MAX], "input": text[:CHAT_MAX],
                "user_id": str(ev.get("user_id") or "")[:20], "user_login": str(ev.get("user_login") or "")[:25]}
        with self.qcond:
            full = len(self.queue) >= MAX_PENDING
            lost = full and len(self.refunds) >= MAX_REFUNDS
            if not full:
                self.queue.append(item)
            elif not lost:
                self.refunds.append(item)     # the worker refunds it before anything else
            self.qcond.notify()
        if full:
            self._event("reward", reward["title"], item["user"], ok=False,
                        error="too many redemptions queued" + ("; left in Twitch's queue" if lost else ""))

    def _worker(self):
        while not self.stopping.is_set():
            with self.qcond:
                while not (self.queue or self.refunds) and not self.stopping.is_set():
                    self.qcond.wait(5)
                if self.stopping.is_set():
                    return
                refund = bool(self.refunds)
                item = self.refunds.popleft() if refund else self.queue.popleft()
                self.busy = True
            try:
                if refund:
                    self._set_status(item, "CANCELED")
                else:
                    self._redeem(item)
            except Exception as e:
                self.log.exception("channel-point redemption failed")
                if not item.get("marked"):
                    self._event("reward", item["reward"]["title"], item["user"], ok=False, error=e)
                    self._set_status(item, "CANCELED")
            finally:
                with self.qcond:
                    self.busy = False
                    self.qcond.notify_all()

    def _prepare(self, reward, text):
        """(actions, revert, color for {color}) with {input} replaced by the parsed color.
        Raises ValueError if the actions need a color and `text` isn't one."""
        needs = uses_input(reward["actions"]) or uses_input(reward["revert"])
        hex_color = color = None
        if needs or "{color}" in reward["reply"]:
            try:
                from core.colors import canonical, parse_color, to_hex
                hex_color, color = to_hex(parse_color(text)), canonical(text)
            except (ValueError, TypeError):
                if needs:
                    raise ValueError(f"not a color: {clean_value(text)!r}") from None
        return _with_color(reward["actions"], hex_color), _with_color(reward["revert"], hex_color), color

    def _run_actions(self, actions, deadline=None, late=None):
        """Run actions in a child thread and wait at most `deadline` seconds: (ok, error).
        A child still running at the deadline is abandoned: it runs no further step (nested lists
        are flattened), late() is called when it does finish, and until then (or the ceiling, see
        _blocker) nothing new starts."""
        blocked = self._blocker()
        if blocked:
            return False, _refusal(blocked)
        steps = flatten(actions)
        done, cancel, result = threading.Event(), threading.Event(), {}
        state = {"done": False, "abandoned": False, "step": None}

        def work():
            try:
                for a in steps:
                    if cancel.is_set():
                        return
                    state["step"] = a
                    self.bot.run_action(a)
            except BaseException as e:
                result["error"] = e
            finally:
                with self.lock:
                    state["done"] = True
                    was_abandoned = state["abandoned"]
                    self.abandoned.pop(child, None)
                done.set()
                if was_abandoned and late:
                    try:
                        late()
                    except Exception:
                        self.log.exception("after an abandoned action finished")
        limit = self.deadline if deadline is None else deadline
        end = self.now() + limit            # before the child starts: the deadline counts from here
        child = threading.Thread(target=work, daemon=True, name="twitch-bot-action")
        child.start()
        while not done.wait(self.wait_step):
            if self.now() >= end:
                with self.lock:
                    if not state["done"]:
                        state["abandoned"] = True
                        step = state["step"]
                        self.abandoned[child] = {"action": _clean(next(iter(step), "?") if step else "?", 32),
                                                 "at": self.now(), "since": self.wall(), "released": False}
                        cancel.set()
                        return False, f"actions took longer than {limit:g}s"
                break
        if "error" in result:
            return False, _clean(result["error"]) or type(result["error"]).__name__
        return True, None

    def _blocker(self):
        """The abandoned action that keeps new runs from starting, or None. Each blocks for at most
        ABANDON_CEILING deadlines (then a warning names it and it no longer counts as blocking),
        unless MAX_ABANDONED are still running: then the oldest blocks until one returns."""
        released = []
        with self.lock:
            entries = sorted(self.abandoned.values(), key=lambda info: info["at"])
            blocked = None
            if len(entries) >= MAX_ABANDONED:
                blocked = entries[0]
            else:
                now, ceiling = self.now(), ABANDON_CEILING * self.deadline
                for info in entries:
                    if info["released"]:
                        continue
                    if now - info["at"] < ceiling:
                        blocked = info
                        break
                    info["released"] = True
                    released.append((info["action"], now - info["at"]))
            running = len(entries)
        for name, age in released:
            self.log.warning("twitch_bot: the %s action was abandoned %ds ago and still hasn't returned; new "
                             "redemptions run again (%d abandoned action(s) still running, at most %d allowed)",
                             name, age, running, MAX_ABANDONED - 1)
        return blocked

    def _redeem(self, item):
        reward, user = item["reward"], item["user"]
        with self.rcond:           # this redemption replaces the pending revert (put back if it fails)
            pending = self.reverts.pop(reward["id"], None)

        def fail(error):
            self._event("reward", reward["title"], user, ok=False, error=error)
            if pending:
                self._restore_revert(reward["id"], pending)
            self._set_status(item, "CANCELED")

        refused = check_reward_actions(reward["actions"], reward["allow_unsafe_actions"]) + \
            check_reward_actions(reward["revert"], reward["allow_unsafe_actions"], "revert")
        if refused:
            return fail("refused: " + refused[0])
        blocked = self._blocker()
        if blocked:
            return fail(_refusal(blocked))
        try:
            actions, revert, color = self._prepare(reward, item["input"])
        except ValueError as e:
            return fail(e)
        wants_revert = bool(reward["revert_after_seconds"] and revert)
        ok, error = self._run_actions(actions, late=(lambda: self._schedule_revert(reward, revert))
                                      if wants_revert else None)
        if not ok:
            return fail(error)
        if wants_revert:
            self._schedule_revert(reward, revert)
        with self.lock:
            self.counts[("reward", reward["id"])] += 1
            count = self.counts[("reward", reward["id"])]
        if reward["reply"]:
            template = reward["reply"]

            def done(job):
                if job.error:
                    self.log.info("reward reply not sent: %s", _clean(job.error))
            job = _Job(lambda: render(template, self._values(template, user=user, count=count,
                                                              color=color or "")), done=done)
            self._submit(job)
            job.finished.wait(self.reply_wait)    # reply first, then FULFILLED, when chat isn't backed up
        self._set_status(item, "FULFILLED")
        self._record("redemption", item.get("user_id"), item.get("user_login"), user, reward["cost"])
        self._event("reward", reward["title"], user)

    def _set_status(self, item, status):
        """FULFILLED / CANCELED (refund) on Twitch, for rewards this app manages and that wait in
        Twitch's queue. Rewards created on twitch.tv can't be changed by us, so never PATCHed."""
        reward = item["reward"]
        item["marked"] = True
        if not reward["managed"] or reward["skip_queue"]:
            return
        try:
            tw = self._twitch(required=True)
            tw.api("PATCH", REWARDS_PATH + "/redemptions",
                   params={"broadcaster_id": tw.me()["id"], "reward_id": reward["twitch_id"], "id": item["id"]},
                   json={"status": status})
        except Exception as e:
            self._event("error", reward["title"], item["user"], ok=False,
                        error=f"couldn't mark the redemption {status.lower()}: {_clean(e)}")

    def test_reward(self, reward_id, text=None):
        """Run a reward's actions as a fake redemption: no Twitch status change, no chat reply."""
        reward = self._find("rewards", reward_id) if isinstance(reward_id, str) else None
        if not reward:
            raise ApiError(f"no reward {_clean(reward_id, 32)!r}", 404)
        if text is None:
            text = ""
        if not isinstance(text, str) or len(text) > CHAT_MAX:
            raise ApiError(f"input must be text of at most {CHAT_MAX} characters")
        refused = check_reward_actions(reward["actions"], reward["allow_unsafe_actions"])
        if refused:
            return {"ok": False, "error": "refused: " + refused[0]}
        try:
            actions, revert, _ = self._prepare(reward, text)
        except ValueError as e:
            return {"ok": False, "error": _clean(e)}
        wants_revert = bool(reward["revert_after_seconds"] and revert)
        ok, error = self._run_actions(actions, late=(lambda: self._schedule_revert(reward, revert))
                                      if wants_revert else None)
        self._event("reward", reward["title"], "test", ok=ok, error=error)
        if not ok:
            return {"ok": False, "error": error}
        if wants_revert:
            self._schedule_revert(reward, revert)
        return {"ok": True}

    # ----- reverts: (due, seq, actions, title, allow_unsafe), at most one pending per reward

    def _schedule_revert(self, reward, actions):
        """A newer revert replaces the pending one."""
        job = None
        with self.rcond:
            if self.drained:
                job = (self.now(), 0, actions, reward["title"], reward["allow_unsafe_actions"])
            else:
                self.rseq += 1
                due = self.now() + reward["revert_after_seconds"]
                self.reverts[reward["id"]] = (due, self.rseq, actions, reward["title"], reward["allow_unsafe_actions"])
                heapq.heappush(self.rheap, (due, self.rseq, reward["id"]))
                self._start_revert_loop()
                self.rcond.notify()
        if job:   # unloading: revert right away
            self._run_revert(reward["id"], job)

    def _restore_revert(self, rid, job):
        """Put back a revert a failed redemption took, unless a newer one exists."""
        run_now = False
        with self.rcond:
            if self.drained:
                run_now = True
            elif rid not in self.reverts:
                self.reverts[rid] = job
                heapq.heappush(self.rheap, (job[0], job[1], rid))
                self._start_revert_loop()
                self.rcond.notify()
        if run_now:
            self._run_revert(rid, job)

    def _start_revert_loop(self):
        """Start the revert thread at load, or again if it has died (call with rcond held)."""
        t = self.revert_thread
        if (t and t.is_alive()) or self.stopping.is_set() or self.drained:
            return
        if t:
            self.log.warning("twitch_bot: the revert scheduler had stopped; restarting it")
        self.revert_thread = threading.Thread(target=self._revert_loop, daemon=True, name="twitch-bot-revert")
        self.revert_thread.start()
        self.threads.append(self.revert_thread)

    def _revert_loop(self):
        try:
            while not self.stopping.is_set():
                job = None
                with self.rcond:
                    self.revert_ticks += 1
                    while self.rheap and (self.reverts.get(self.rheap[0][2]) or (0, None))[1] != self.rheap[0][1]:
                        heapq.heappop(self.rheap)       # replaced or already run
                    if self.rheap and self.rheap[0][0] <= self.now():
                        rid = heapq.heappop(self.rheap)[2]
                        job = self.reverts.pop(rid)
                    else:
                        self.rcond.wait(self.wait_step if self.rheap else self.wait_step * 10)
                        continue
                try:
                    self._run_revert(rid, job)
                except Exception:
                    self.log.exception("twitch_bot: the revert of %s failed", clean_value(job[3]))
        except Exception:
            self.log.exception("twitch_bot: the revert scheduler stopped; it restarts with the next revert")

    def _run_revert(self, rid, job, deadline=None):
        refused = check_reward_actions(job[2], job[4], "revert")
        if refused:
            self._event("revert", job[3], ok=False, error="refused: " + refused[0])
            return
        ok, error = self._run_actions(job[2], deadline=deadline)
        self._event("revert", job[3], ok=ok, error=error)

    # ----- reward sync with Twitch

    def _rewards_on_twitch(self, tw, uid, manageable_only):
        params = {"broadcaster_id": uid}
        if manageable_only:
            params["only_manageable_rewards"] = "true"
        data = tw.api("GET", REWARDS_PATH, params=params).get("data") or []
        return {str(r["id"]): r for r in data if isinstance(r, dict) and r.get("id")}

    def _reward_body(self, r):
        body = {"title": r["title"], "cost": r["cost"], "prompt": r["prompt"], "is_enabled": r["enabled"],
                "is_paused": not self.cfg["enabled"],
                "background_color": r["background_color"], "is_user_input_required": r["input_required"],
                "is_global_cooldown_enabled": r["global_cooldown_seconds"] > 0,
                "is_max_per_stream_enabled": r["max_per_stream"] > 0,
                "is_max_per_user_per_stream_enabled": r["max_per_user_per_stream"] > 0,
                "should_redemptions_skip_request_queue": r["skip_queue"]}
        for key in ("global_cooldown_seconds", "max_per_stream", "max_per_user_per_stream"):
            if r[key] > 0:
                body[key] = r[key]
        return body

    def sync_rewards(self):
        """Create / update the managed rewards on Twitch and link unmanaged ones by title; new
        twitch_ids are written back to config.yaml. Returns {created, updated, errors}."""
        tw = self._twitch(required=True)
        tw.require("channel:manage:redemptions")
        uid = tw.me()["id"]
        manageable = self._rewards_on_twitch(tw, uid, True)
        rewards = list(self.cfg["rewards"])
        claimed = {r["twitch_id"] for r in rewards if r["twitch_id"]}
        everything, ids, errors = None, {}, []
        created = updated = 0
        for r in rewards:
            try:
                tid = r["twitch_id"]
                if r["managed"]:
                    if tid not in manageable:
                        match = next((x for i, x in manageable.items() if i not in claimed
                                      and str(x.get("title") or "").casefold() == r["title"].casefold()), None)
                        tid = str(match["id"]) if match else ""
                    body = self._reward_body(r)
                    if tid:
                        tw.api("PATCH", REWARDS_PATH, params={"broadcaster_id": uid, "id": tid}, json=body)
                        updated += 1
                    else:
                        data = tw.api("POST", REWARDS_PATH, params={"broadcaster_id": uid}, json=body).get("data")
                        tid = str((data or [{}])[0].get("id") or "")
                        if not TWITCH_ID_RE.fullmatch(tid):
                            raise RuntimeError("Twitch didn't return the new reward's id")
                        created += 1
                elif not tid:
                    if everything is None:
                        everything = self._rewards_on_twitch(tw, uid, False)
                    match = next((x for i, x in everything.items() if i not in claimed
                                  and str(x.get("title") or "").casefold() == r["title"].casefold()), None)
                    if not match:
                        errors.append(f"{r['id']}: no reward titled {r['title']!r} on your channel")
                        continue
                    tid = str(match["id"])
                if tid and tid != r["twitch_id"]:
                    ids[r["id"]] = tid
                    claimed.add(tid)
            except Exception as e:
                errors.append(f"{r['id']}: {_clean(e)}")
        if ids:
            self._write_twitch_ids(ids)
        return {"created": created, "updated": updated, "errors": errors}

    def _write_twitch_ids(self, ids):
        def edit(section):
            for item in section.get("rewards") or []:
                if isinstance(item, dict) and item.get("id") in ids:
                    item["twitch_id"] = ids[item["id"]]
        try:
            self._edit_config(edit)
        finally:   # redemptions work right away, before config.yaml is reloaded
            with self.lock:
                self.cfg = {**self.cfg, "rewards": [{**r, "twitch_id": ids.get(r["id"], r["twitch_id"])}
                                                    for r in self.cfg["rewards"]]}

    def twitch_rewards(self):
        """Every custom reward on the channel: [{id, title, cost, manageable, enabled}]."""
        tw = self._twitch(required=True)
        uid = tw.me()["id"]
        manageable = self._rewards_on_twitch(tw, uid, True)
        return [{"id": i, "title": str(x.get("title") or "")[:VALUE_MAX], "cost": x.get("cost"),
                 "manageable": i in manageable, "enabled": bool(x.get("is_enabled"))}
                for i, x in self._rewards_on_twitch(tw, uid, False).items()]

    def delete_reward(self, reward_id):
        """Remove a reward from config.yaml first, then delete it on Twitch when it is managed
        here and Twitch reports it manageable by this app. A failed Twitch delete is in `errors`."""
        if not isinstance(reward_id, str) or not ID_RE.fullmatch(reward_id):
            raise ApiError("id must be a reward id")
        from core import configio
        r = self._find("rewards", reward_id)
        raw = configio.load(self.bot.config_path).get("twitch_bot") or {}
        in_file = any(isinstance(x, dict) and x.get("id") == reward_id for x in raw.get("rewards") or []) \
            if isinstance(raw, dict) and isinstance(raw.get("rewards"), list) else False
        if not r and not in_file:
            raise ApiError(f"no reward {reward_id!r}", 404)

        def edit(section):
            if isinstance(section.get("rewards"), list):
                section["rewards"] = [x for x in section["rewards"] if not (isinstance(x, dict) and x.get("id") == reward_id)]
        self._edit_config(edit)
        with self.lock:
            self.cfg = {**self.cfg, "rewards": [x for x in self.cfg["rewards"] if x["id"] != reward_id]}
        on_twitch, errors = False, []
        if r and r["managed"] and r["twitch_id"]:
            try:
                tw = self._twitch(required=True)
                tw.require("channel:manage:redemptions")
                uid = tw.me()["id"]
                if r["twitch_id"] in self._rewards_on_twitch(tw, uid, True):
                    tw.api("DELETE", REWARDS_PATH, params={"broadcaster_id": uid, "id": r["twitch_id"]})
                    on_twitch = True
            except Exception as e:
                errors.append(f"removed here, but deleting it on Twitch failed: {_clean(e)}")
                self._event("error", r["title"], ok=False, error=errors[-1])
        return {"ok": True, "deleted_on_twitch": on_twitch, "errors": errors}

    # ----- the panel

    def config_view(self):
        """The normalised section for the panel; database_url is config.yaml-only, never shown."""
        view = copy.deepcopy(self.cfg)
        for key in CONFIG_ONLY:
            view.pop(key, None)
        return view

    def save_config(self, section, base_hash=None):
        """Validate and save the whole twitch_bot section (400 with every field error). With
        base_hash, a config.yaml that changed since is a 409 (never merged). A reward whose
        twitch_id is empty keeps the one already in config.yaml (a sync may have written it)."""
        if not isinstance(section, dict):
            raise ApiError("config must be an object")
        if base_hash is not None and not isinstance(base_hash, str):
            raise ApiError("hash must be text")
        section = {k: v for k, v in section.items() if k not in CONFIG_ONLY}   # config.yaml-only
        normalised, errors = validate(section)
        if errors:
            shown = errors[:20] + ([f"…and {len(errors) - 20} more"] if len(errors) > 20 else [])
            e = ApiError("Fix these first: " + "; ".join(shown), 400)
            e.errors = errors
            raise e

        def edit(current):
            old = {x.get("id"): x.get("twitch_id") for x in current.get("rewards") or []
                   if isinstance(x, dict) and isinstance(x.get("twitch_id"), str) and x.get("twitch_id")} \
                if isinstance(current.get("rewards"), list) else {}
            taken = {r["twitch_id"] for r in normalised["rewards"] if r["twitch_id"]}
            for r in normalised["rewards"]:
                if not r["twitch_id"] and old.get(r["id"]) and old[r["id"]] not in taken \
                        and TWITCH_ID_RE.fullmatch(old[r["id"]]):
                    r["twitch_id"] = old[r["id"]]
            kept = {k: current[k] for k in CONFIG_ONLY if current.get(k) is not None}
            for k in CONFIG_ONLY:
                v = kept.get(k)
                normalised[k] = v.strip() if isinstance(v, str) else ""
            current.clear()
            current.update({k: copy.deepcopy(v) for k, v in normalised.items() if k not in CONFIG_ONLY})
            current.update(kept)
        self._edit_config(edit, base=base_hash)
        self.on_config(normalised)
        return self.config_view()

    def stats_view(self):
        """{enabled, url (masked by ChatStats), backend, error, totals}; never the raw database_url."""
        out = {"enabled": self.cfg["stats"]["enabled"], "url": None, "backend": None,
               "error": self.stats_error, "totals": {"chatters": 0, "messages": 0, "stream_messages": 0}}
        stats = self.stats
        if stats:
            try:
                d = stats.describe() or {}
                out.update(url=d.get("url"), backend=d.get("backend"), error=d.get("error"))
                out["totals"] = stats.totals() or out["totals"]
            except Exception as e:
                out["error"] = f"chat stats aren't working ({type(e).__name__})"
        elif out["enabled"] and not out["error"]:
            out["error"] = "chat stats are starting"
        return out

    def _stats_or_503(self):
        try:
            return self._stats_ready()
        except _NoPost as e:
            raise ApiError(str(e), 503) from None

    def _api_query(self, fn, *args, **kw):
        try:
            return self._query(fn, *args, **kw)
        except _NoPost as e:
            raise ApiError(str(e), 503) from None

    def stats_top(self, period="all", n=25):
        if period not in ("all", "stream"):
            raise ApiError("period must be all or stream")
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 100:
            raise ApiError("n must be a whole number from 1 to 100")
        stats = self._stats_or_503()
        return {"top": self._api_query(stats.top, n=n, period=period) or [], "totals": self._api_query(stats.totals)}

    def stats_user(self, login):
        m = LOOKUP_RE.fullmatch(login) if isinstance(login, str) else None
        if not m:
            raise ApiError("login must be a Twitch login (3-25 of A-Z, 0-9, _)")
        row = self._api_query(self._stats_or_503().user, login=m.group(1).lower())
        if not row:
            raise ApiError("no chat stats for that login", 404)
        return row

    def reset_stats(self, scope, confirm):
        """Reset chat stats; a SQLite database is first copied to <state>/backups (scope all)."""
        if scope not in ("all", "stream"):
            raise ApiError("scope must be all or stream")
        if confirm is not True:
            raise ApiError("confirm must be true")
        stats = self._stats_or_503()
        backup = None
        if scope == "all" and (self._api_query(stats.describe) or {}).get("backend") == "sqlite":
            self._api_query(stats.flush)
            path = self._api_query(stats.sqlite_path)
            if path and os.path.exists(path):
                backup = self._backup_sqlite(path)
            elif (self._api_query(stats.totals) or {}).get("chatters"):
                raise ApiError("couldn't back up the database first; nothing was reset", 500)
        self._api_query(stats.reset, scope)
        self.log.info("chat stats reset (%s)%s", scope, f"; backup {backup}" if backup else "")
        return {"ok": True, "backup": backup}

    def _backup_sqlite(self, path):
        """Copy the database with sqlite3's backup API to backups/chatstats-YYYYMMDD-HHMMSS.db
        (0600), keeping the newest 10. Returns the file name."""
        import sqlite3
        import urllib.parse
        from core.paths import state_dir
        folder = os.path.join(state_dir(), "backups")
        os.makedirs(folder, mode=0o700, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        name, n = f"chatstats-{stamp}.db", 1
        while os.path.exists(os.path.join(folder, name)):
            name, n = f"chatstats-{stamp}-{n}.db", n + 1
        dest = os.path.join(folder, name)
        try:
            os.close(os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            src = sqlite3.connect(f"file:{urllib.parse.quote(os.path.abspath(path))}?mode=ro", uri=True)
            try:
                dst = sqlite3.connect(dest)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
            os.chmod(dest, 0o600)
        except (OSError, sqlite3.Error) as e:
            try:
                os.remove(dest)
            except OSError:
                pass
            raise ApiError(f"couldn't back up the chat stats ({type(e).__name__}); nothing was reset", 500) from None
        old = sorted(glob.glob(os.path.join(folder, "chatstats-*.db")), key=lambda f: (os.path.getmtime(f), f))
        for f in old[:-10]:
            try:
                os.remove(f)
            except OSError:
                pass
        return name

    def snapshot(self):
        blocked = self._blocker()
        now, cfg = self.now(), self.cfg
        with self.lock:
            timers = []
            for t in cfg["timers"]:
                st = self.timer_state.get(t["id"]) or {"last": now, "lines": 0, "last_sent": None}
                timers.append({"id": t["id"], "next_in": max(0, int(t["interval_minutes"] * 60 - (now - st["last"]))),
                               "lines": st["lines"], "last_sent": st["last_sent"]})
            recent = [dict(e) for e in self.recent]
        return {"enabled": cfg["enabled"], "live": self._live_view(), "timers": timers, "paused": self.paused,
                "recent": recent,
                "blocked": {"action": blocked["action"], "since": blocked["since"]} if blocked else None}

    # ----- deck keys

    @action("twitch_bot", ui=UI("Twitch Bot on / off", "Twitch", arg=Choice(
        ("toggle", "On / off"), ("on", "On"), ("off", "Off"))))
    def bot_switch(self, what):
        """twitch_bot: on | off | toggle"""
        if what not in ("on", "off", "toggle"):
            raise ValueError(f"unknown twitch_bot action {what!r}")
        self.set_enabled(not self.cfg["enabled"] if what == "toggle" else what == "on")

    @action("bot_timer", ui=UI("Post a scheduled message now", "Twitch", arg=Source("bot_timers", "Message")))
    def timer_now(self, timer_id):
        """bot_timer: <timer id>"""
        self.send_timer(timer_id)

    @action("bot_timers", ui=UI("Pause / resume scheduled messages", "Twitch", arg=Choice(
        ("toggle", "Pause / resume"), ("pause", "Pause"), ("resume", "Resume"))))
    def timers_switch(self, what):
        """bot_timers: pause | resume | toggle (in memory only)"""
        if what not in ("pause", "resume", "toggle"):
            raise ValueError(f"unknown bot_timers action {what!r}")
        self.paused = not self.paused if what == "toggle" else what == "pause"

    @state("twitch_bot", ui=UI("Twitch Bot is on", "Twitch", arg=Choice(("on", "on"))))
    def bot_state(self, what, spec):
        if what != "on":
            raise ValueError(f"unknown twitch_bot state {what!r}")
        return self.cfg["enabled"]

    @state("bot_timers", ui=UI("Scheduled messages are running", "Twitch", arg=Choice(("running", "running"))))
    def timers_state(self, what, spec):
        if what != "running":
            raise ValueError(f"unknown bot_timers state {what!r}")
        return self.cfg["enabled"] and not self.paused

    @option_source("bot_timers")
    def timer_choices(self):
        return [t["id"] for t in self.cfg["timers"]]


def setup(bot):
    bot.add_module(TwitchBot(bot))
