"""Twitch EventSub over a websocket: channel-point redemptions, stream online/offline and alerts, live.

Runs while Twitch is linked and either `twitch_bot: enabled: true` (redemptions, online/offline) or the
Alerts module wants events (follows, subs, resubs, gift subs, cheers, incoming raids; each only when its
scope is granted). Other modules receive:
    @listener("reward_redemption")
    def on_redeem(self, r): ...   # r: {"id", "reward_id", "reward_title", "user", "user_login",
                                  #     "user_id", "input", "status", "ts"}
    @listener("stream_online")    # {"stream_id", "type" ("live", "playlist", ...), "started_at"}: cleaned strings
    @listener("stream_offline")   # called with {}
    @listener("eventsub_session")   # {}: a fresh session was subscribed (not sent for a reconnect_url move)
    @listener("twitch_event")     # every key always present:
        # {"kind": "follow"|"sub"|"resub"|"gift"|"cheer"|"raid",
        #  "user": display name (cleaned, <= 25 chars; the login if the name is unusable; "Anonymous" for an
        #          anonymous gift/cheer; "" only if both are unusable),
        #  "user_login": lowercase login or "", "user_id": numeric string or "", "anonymous": bool,
        #  "tier": 1/2/3 for sub/resub/gift else 0, "months", "cumulative_months" (resub), "total" (gift),
        #  "bits" (cheer), "viewers" (raid): ints 0..10_000_000 (0 when not applicable or malformed),
        #  "message": cleaned resub/cheer text (<= 500 chars) else "", "ts": epoch seconds}
        # For a raid the user fields name the raiding channel. A follow alerts once per user per 24 h.

Everything in a notification comes from viewers or the network, so a message is only acted on when
it is for a subscription type and version we created, with exactly the condition we asked for, for
our own broadcaster id, on the session it arrived on, not older than 10 minutes and not seen before
(by message id and by event id). Listeners run on the websocket thread: keep them quick (queue the work).
"""
import collections
import json
import random
import re
import ssl
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import websocket

from core import Module, task
from core import text as textclean
from core.text import collapse_marks  # noqa: F401  (re-exported: modules/chat.py imports it from here)
from core.twitch_ids import LOGIN_RE, USER_ID_RE

URL = "wss://eventsub.wss.twitch.tv/ws"
REDEMPTION = "channel.channel_points_custom_reward_redemption.add"
REDEMPTION_SCOPE = "channel:manage:redemptions"
_BROADCASTER = lambda me: {"broadcaster_user_id": me}   # noqa: E731
# type -> (version, scope needed or None, condition builder(me_id))
SUBS = {
    REDEMPTION: ("1", REDEMPTION_SCOPE, _BROADCASTER),
    "stream.online": ("1", None, _BROADCASTER),
    "stream.offline": ("1", None, _BROADCASTER),
    "channel.follow": ("2", "moderator:read:followers",
                       lambda me: {"broadcaster_user_id": me, "moderator_user_id": me}),
    "channel.subscribe": ("1", "channel:read:subscriptions", _BROADCASTER),
    "channel.subscription.message": ("1", "channel:read:subscriptions", _BROADCASTER),
    "channel.subscription.gift": ("1", "channel:read:subscriptions", _BROADCASTER),
    "channel.cheer": ("1", "bits:read", _BROADCASTER),
    "channel.raid": ("1", None, lambda me: {"to_broadcaster_user_id": me}),
}
BOT_TYPES = (REDEMPTION, "stream.online", "stream.offline")          # when twitch_bot is enabled
ALERTS = {"channel.follow": "follow", "channel.subscribe": "sub", "channel.subscription.message": "resub",
          "channel.subscription.gift": "gift", "channel.cheer": "cheer", "channel.raid": "raid"}
ALERT_TYPES = tuple(ALERTS)                                          # when the Alerts module wants events
SUBSCRIBE_WINDOW = 10       # Twitch closes a session that has no subscription 10 s after its welcome
WELCOME_WAIT = 10           # how long a fresh socket may take to send its welcome
STALE = 600                 # notifications older than this are replays: drop them
DEDUP = 500                 # message ids / event ids remembered (per instance, across reconnects)
FOLLOW_TTL = 24 * 3600      # a follow alerts once per user per day (follow/unfollow loops)
FOLLOW_MAX = 5000
MAX_BACKOFF = 60
LONG_SESSION = 60           # a session that lasted longer than this resets the backoff
POLL = 1                    # recv timeout: how often the loop checks the keepalive deadline and stop flag
DRAIN = 2                   # after a reconnect-url move, read what is left on the old socket this long
INPUT_MAX = 500
LOG_MAX = 100
NAME_MAX = 25
MESSAGE_MAX = 500
COUNT_MAX = 10_000_000
TIERS = {"1000": 1, "2000": 2, "3000": 3}
HOST_RE = re.compile(r"(?:[a-z0-9-]+\.)+twitch\.tv")
TS_RE = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)", re.I)


def clean(text, n=LOG_MAX):
    """A network/viewer string made safe to pass on or log: ANSI escapes, control characters,
    line separators and format characters (bidi overrides, zero-width, BOM; ZWJ is kept) removed,
    then capped at n characters."""
    return textclean.clean(text, newlines="drop", marks=None, collapse=False)[:n]


def parse_ts(text):
    """RFC 3339 timestamp (Twitch sends nanoseconds) -> epoch seconds, or None if malformed."""
    m = TS_RE.fullmatch(text) if isinstance(text, str) else None
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    frac = float("0." + m.group(2)) if m.group(2) else 0.0
    tz = m.group(3).upper()
    offset = timedelta(0) if tz == "Z" else \
        (1 if tz[0] == "+" else -1) * timedelta(hours=int(tz[1:3]), minutes=int(tz[4:6]))
    return dt.replace(tzinfo=timezone(offset)).timestamp() + frac


def _name(value):
    if not isinstance(value, str):
        return ""
    return collapse_marks(clean(unicodedata.normalize("NFC", value), NAME_MAX).strip())


def _login(value):
    value = value.lower() if isinstance(value, str) else ""
    return value if LOGIN_RE.fullmatch(value) else ""


def _user_id(value):
    return value if isinstance(value, str) and USER_ID_RE.fullmatch(value) else ""


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= COUNT_MAX else 0


def _text(value):
    return collapse_marks(clean(value, MESSAGE_MAX)) if isinstance(value, str) else ""


def normalise(kind, event, ts, me_id):
    """One alert notification's event -> the `twitch_event` dict, or None when it must be dropped.
    kind is the EventSub type ("channel.follow", ...) or the short kind ("follow", ...); the caller has
    already checked the subscription. Pure: no I/O, no logging."""
    kind = ALERTS.get(kind, kind)
    if kind not in ALERTS.values() or not isinstance(event, dict) or not isinstance(me_id, str) or not me_id:
        return None
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return None
    if kind == "raid":
        if event.get("to_broadcaster_user_id") != me_id:
            return None
        rid, login = _user_id(event.get("from_broadcaster_user_id")), _login(event.get("from_broadcaster_user_login"))
        if not rid or rid == me_id or not login:
            return None
        user = {"user": _name(event.get("from_broadcaster_user_name")), "user_login": login, "user_id": rid}
    else:
        if event.get("broadcaster_user_id") != me_id:
            return None
        user = {"user": _name(event.get("user_name")), "user_login": _login(event.get("user_login")),
                "user_id": _user_id(event.get("user_id"))}
    if not user["user"]:
        user["user"] = user["user_login"]          # a name that cleans to nothing: show the login instead
    anonymous = kind in ("gift", "cheer") and event.get("is_anonymous") is True
    if anonymous:
        user = {"user": "Anonymous", "user_login": "", "user_id": ""}
    tier = 0
    if kind in ("sub", "resub", "gift"):
        tier = TIERS.get(event.get("tier")) if isinstance(event.get("tier"), str) else None
        if tier is None:
            return None
    if kind == "sub" and event.get("is_gift") is not False:     # a gifted sub: the gift event covers it
        return None
    message = ""
    if kind == "resub":
        body = event.get("message")
        message = _text(body.get("text")) if isinstance(body, dict) else ""
    elif kind == "cheer":
        message = _text(event.get("message"))
    return {"kind": kind, **user, "anonymous": anonymous, "tier": tier,
            "months": _count(event.get("duration_months")) if kind == "resub" else 0,
            "cumulative_months": _count(event.get("cumulative_months")) if kind == "resub" else 0,
            "total": _count(event.get("total")) if kind == "gift" else 0,
            "bits": _count(event.get("bits")) if kind == "cheer" else 0,
            "viewers": _count(event.get("viewers")) if kind == "raid" else 0,
            "message": message, "ts": float(ts)}


def condition_matches(condition, expected):
    """Every key we subscribed with is there with our value; Twitch echoes the optional condition
    fields back empty (raid from_broadcaster_user_id, redemption reward_id), so any other key may only
    be "" or None."""
    if not isinstance(condition, dict):
        return False
    if any(key not in condition or condition[key] != value for key, value in expected.items()):
        return False
    return all(value in ("", None) for key, value in condition.items() if key not in expected)


def valid_reconnect_url(url):
    """Only follow a session_reconnect to wss://<something>.twitch.tv on the default port."""
    if not isinstance(url, str) or not url or len(url) > 2048:
        return False
    if any(c in url for c in "\\@ ") or any(unicodedata.category(c)[0] in "CZ" for c in url):
        return False
    try:
        p = urlparse(url)
        port = p.port
    except ValueError:
        return False
    if p.scheme != "wss" or p.username is not None or p.password is not None or port not in (None, 443):
        return False
    host = p.hostname or ""
    return host == "eventsub.wss.twitch.tv" or bool(HOST_RE.fullmatch(host))


class _Recent:
    """The last `size` keys seen."""

    def __init__(self, size=DEDUP):
        self.order = collections.deque()
        self.keys = set()
        self.size = size

    def add(self, key):
        """Remember key; False if it was already there."""
        if key in self.keys:
            return False
        if len(self.order) >= self.size:
            self.keys.discard(self.order.popleft())
        self.order.append(key)
        self.keys.add(key)
        return True


class _Expiring:
    """Keys remembered for `ttl` seconds, at most `size` of them (oldest forgotten first)."""

    def __init__(self, ttl=FOLLOW_TTL, size=FOLLOW_MAX, clock=time.monotonic):
        self.keys = collections.OrderedDict()     # key -> expiry, in insertion (= expiry) order
        self.ttl, self.size, self.clock = ttl, size, clock

    def add(self, key):
        """Remember key; False if it was remembered and hasn't expired."""
        now = self.clock()
        while self.keys and next(iter(self.keys.values())) <= now:
            self.keys.popitem(last=False)
        if key in self.keys:
            return False
        while len(self.keys) >= self.size:
            self.keys.popitem(last=False)
        self.keys[key] = now + self.ttl
        return True


class _Conn:
    """One websocket and the EventSub session it carries."""

    def __init__(self, ws, moved=False):
        self.ws = ws
        self.moved = moved               # opened from a reconnect_url: subscriptions carry over
        self.session_id = None
        self.accept = set()              # session ids whose notifications this socket may deliver
        self.keepalive = WELCOME_WAIT
        self.last = time.monotonic()     # when the last message arrived


class EventSub(Module):
    config_key = "twitch_bot"

    def __init__(self, bot):
        super().__init__(bot)
        self._lock = threading.Lock()        # connection state read by snapshot() / the stop path
        self._sync_lock = threading.Lock()   # _sync runs from the main loop and from config reloads
        self._loaded = False
        self._worker = None
        self._stop = None
        self._socks = set()                  # every open websocket, so stopping can wake recv()
        self._conn = None
        self._connected = False
        self._subs = {}                      # type -> subscription id, for the current session chain
        self._types = None                   # the wanted types the current session was subscribed for
        self._scopes = None                  # the granted scopes, among those types need, it was subscribed with
        self._skipped = []                   # optional types not subscribed (scope missing / refused)
        self._skip_logged = set()            # (type, reason) already logged, so reconnects don't repeat it
        self._error = None
        self._me_id = None
        self._seen_msgs = _Recent()
        self._seen_events = _Recent()
        self._seen_follows = _Expiring()
        self._skew_logged = False
        self._backoff = 1
        self._session_started = None
        self._published = None
        self._connect = lambda url: websocket.create_connection(
            url, timeout=10, sslopt={"context": ssl.create_default_context()})   # cert + hostname verified

    # ----- lifecycle

    def on_config(self, config):
        super().on_config(config)
        if self._loaded:
            self._sync()

    def on_load(self):
        self._loaded = True
        self._sync()

    def on_unload(self):
        self._loaded = False
        self._stop_worker()
        worker = self._worker
        if worker and worker is not threading.current_thread():
            worker.join(timeout=2)

    @task(every=5, on_page_change=False)
    def supervise(self):
        """Start or stop the websocket as Twitch is linked/unlinked and the bot or alerts enabled/disabled,
        and restart it when the set of wanted subscription types changes."""
        self._sync()

    def _twitch(self):
        return self.bot.get_module("Twitch")

    def _alerts_wanted(self):
        try:
            alerts = self.bot.get_module("Alerts")
            return bool(alerts and alerts.wants_events())
        except Exception:
            return False

    def _desired(self):
        """The subscription types wanted now, before scopes are checked."""
        types = BOT_TYPES if self.config.get("enabled") is True else ()
        return types + (ALERT_TYPES if self._alerts_wanted() else ())

    def _relevant_scopes(self, desired, scopes=None):
        """The granted scopes that the wanted types need: a re-link that adds one restarts the session."""
        try:
            if scopes is None:
                twitch = self._twitch()
                scopes = twitch.granted_scopes(network=False) if twitch and twitch.linked() else ()
        except Exception:
            return None
        need = {SUBS[kind][1] for kind in desired} - {None}
        return frozenset(need & set(scopes))

    def _wanted(self, desired=None):
        if not self._loaded or not (self._desired() if desired is None else desired):
            return False
        twitch = self._twitch()
        try:
            return bool(twitch and twitch.linked())
        except Exception:
            return False

    def _sync(self):
        with self._sync_lock:       # check-then-start must be atomic: never two workers
            desired = self._desired()
            wanted = self._wanted(desired)
            running = self._worker is not None and self._worker.is_alive()
            if wanted and running and not self._stop.is_set():
                with self._lock:
                    active, active_scopes = self._types, self._scopes
                scopes = self._relevant_scopes(desired) if active == desired else None
                changed = active is not None and active != desired
                regranted = (active == desired and active_scopes is not None and scopes is not None
                             and scopes != active_scopes)
                if changed or regranted:
                    self.log.info("EventSub: %s; restarting the session",
                                  "the wanted subscriptions changed" if changed else "the Twitch permissions changed")
                    self._stop_worker()
                    if self._worker is not threading.current_thread():
                        self._worker.join(timeout=2)
                    # Never two workers (they share the socket set): if the old one is still winding
                    # down, the next supervise tick starts the new one.
                    running = self._worker.is_alive()
            if wanted and not running:
                self._stop = threading.Event()
                self._worker = threading.Thread(target=self._run, args=(self._stop,), daemon=True,
                                                name="twitch-eventsub")
                self._worker.start()
            elif not wanted and running and not self._stop.is_set():
                self.log.info("EventSub stopping")
                self._stop_worker()

    def _stop_worker(self):
        if self._stop:
            self._stop.set()
        with self._lock:
            socks = list(self._socks)
        for ws in socks:
            try:
                ws.abort()          # wakes the worker blocked in recv()
            except Exception:
                pass

    # ----- the socket loop

    def _run(self, stop):
        while not stop.is_set() and self._wanted():
            self._session_started = None
            try:
                self._session(stop)
            except Exception as e:
                if not stop.is_set():
                    self.log.info("EventSub disconnected (%s: %s)", type(e).__name__, clean(e))
            finally:
                self._close_all()
                with self._lock:
                    self._conn = None
                    self._connected = False
                self._publish()
            if stop.is_set():
                break
            started = self._session_started
            delay = self._next_delay(time.monotonic() - started if started else 0)
            self.log.debug("EventSub reconnecting in %.1fs", delay)
            stop.wait(delay)
        with self._lock:
            self._subs = {}
            self._types = self._scopes = None
            self._skipped = []
            self._connected = False
        self._publish()

    def _next_delay(self, session_seconds):
        """Seconds to wait before reconnecting: 1, 2, 4 ... 60 with jitter; back to 1 after a long session."""
        if session_seconds > LONG_SESSION:
            self._backoff = 1
        delay = min(MAX_BACKOFF, self._backoff * random.uniform(1.0, 1.25))
        self._backoff = min(self._backoff * 2, MAX_BACKOFF)
        return delay

    def _open(self, url, moved=False):
        ws = self._connect(url)
        with self._lock:
            self._socks.add(ws)
        ws.settimeout(POLL)
        return _Conn(ws, moved)

    def _close(self, conn):
        if conn is None:
            return
        with self._lock:
            self._socks.discard(conn.ws)
        try:
            conn.ws.close()
        except Exception:
            pass

    def _close_all(self):
        with self._lock:
            socks = list(self._socks)
            self._socks.clear()
        for ws in socks:
            try:
                ws.close()
            except Exception:
                pass

    def _recv(self, conn):
        """The next text message on conn, or None on a poll tick. Raises when the socket closed or
        nothing (not even a keepalive) arrived for keepalive_timeout_seconds + 5."""
        try:
            raw = conn.ws.recv()
        except (websocket.WebSocketTimeoutException, TimeoutError):
            if time.monotonic() - conn.last > conn.keepalive + 5:
                raise ConnectionError("keepalive timeout")
            return None
        if not raw and not getattr(conn.ws, "connected", True):
            raise ConnectionError("closed by server")
        conn.last = time.monotonic()
        return raw if isinstance(raw, str) else None    # binary frames are not part of the protocol

    def _session(self, stop):
        conn = self._open(URL)
        with self._lock:
            self._conn = conn
        while not stop.is_set():
            raw = self._recv(conn)
            if raw is None:
                continue
            result = self._on_message(raw, conn)
            if result == "reconnect":
                return
            if isinstance(result, tuple) and result[0] == "move":
                conn = self._move(conn, result[1], stop)

    def _move(self, old, url, stop):
        """Follow a session_reconnect: open the new socket, keep the old one until the new welcome,
        then read what is left on the old one and close it. Subscriptions carry over."""
        new = self._open(url, moved=True)
        deadline = time.monotonic() + WELCOME_WAIT
        while new.session_id is None:
            if stop.is_set():
                raise ConnectionError("stopped")
            if time.monotonic() > deadline:
                raise ConnectionError("no welcome after reconnect")
            raw = self._recv(new)
            if raw is not None and self._on_message(raw, new) == "reconnect":
                raise ConnectionError("reconnect failed")
        with self._lock:
            self._conn = new
        # Notifications may still name the previous session in subscription.transport for a while:
        # the new socket accepts every session id of this chain (and only those), the old one the new id.
        new.accept |= old.accept
        old.accept.add(new.session_id)
        end = time.monotonic() + DRAIN
        while time.monotonic() < end and not stop.is_set():
            try:
                raw = self._recv(old)
            except Exception:
                break
            if raw is not None:
                self._on_message(raw, old)
        self._close(old)
        self.log.info("EventSub moved to a new session")
        return new

    # ----- messages (no network here except the subscribe calls on welcome)

    def _on_message(self, raw_json, conn=None):
        """Handle one EventSub message. Returns None, "reconnect" (end this session) or
        ("move", url) (follow a session_reconnect)."""
        conn = conn or self._conn
        self.log.debug("EventSub raw: %r", raw_json)
        try:
            msg = json.loads(raw_json)
        except (TypeError, ValueError):
            self.log.info("EventSub: dropped a message that isn't JSON")
            return None
        meta = msg.get("metadata") if isinstance(msg, dict) else None
        payload = msg.get("payload") if isinstance(msg, dict) else None
        if not isinstance(meta, dict) or not isinstance(payload, dict) or conn is None:
            self.log.info("EventSub: dropped a malformed message")
            return None
        kind = meta.get("message_type")
        if kind == "session_keepalive":
            return None
        mid = meta.get("message_id")
        if not isinstance(mid, str) or not mid:
            self.log.info("EventSub: dropped a message without an id")
            return None
        if not self._seen_msgs.add(mid):
            self.log.debug("EventSub: duplicate message %s", clean(mid))
            return None
        if kind == "session_welcome":
            return self._on_welcome(conn, payload.get("session"))
        if kind == "session_reconnect":
            return self._on_reconnect(conn, payload.get("session"))
        if kind == "notification":
            self._on_notification(conn, meta, payload)
            return None
        if kind == "revocation":
            self._on_revocation(conn, payload.get("subscription"))
            return None
        self.log.info("EventSub: ignored message type %r", clean(kind, 40))
        return None

    def _on_welcome(self, conn, session):
        if conn.session_id is not None:
            self.log.info("EventSub: ignored a second welcome on one socket")
            return None
        sid = session.get("id") if isinstance(session, dict) else None
        if not isinstance(sid, str) or not sid or len(sid) > 200 or clean(sid, 200) != sid:
            self.log.info("EventSub: welcome without a usable session id")
            return "reconnect"
        ka = session.get("keepalive_timeout_seconds")
        conn.keepalive = ka if isinstance(ka, int) and not isinstance(ka, bool) and 1 <= ka <= 600 else 10
        conn.session_id = sid
        conn.accept.add(sid)
        welcomed = time.monotonic()
        if conn.moved:
            with self._lock:
                self._connected = True
            self._publish()
            return None
        if self._session_started is None:
            self._session_started = welcomed
        return self._subscribe(conn, welcomed)

    def _skip(self, kind, reason):
        """Note an optional type that isn't subscribed; logged once per type and reason."""
        if (kind, reason) not in self._skip_logged:
            self._skip_logged.add((kind, reason))
            self.log.info("EventSub: skipping %s (%s %s)", kind, SUBS[kind][1] or "scope", reason)

    def _subscribe(self, conn, welcomed):
        twitch = self._twitch()
        desired = self._desired()
        with self._lock:
            self._subs = {}
            self._types = desired
            self._skipped = []
        try:
            if not twitch or not twitch.linked():
                raise RuntimeError("Twitch isn't linked")
            me_id = str(twitch.me()["id"])
            scopes = twitch.granted_scopes(network=False)
        except Exception as e:
            self.log.info("EventSub: can't read the Twitch account (%s: %s)", type(e).__name__, clean(e))
            self._set_error("account")
            return "reconnect"
        self._me_id = me_id
        with self._lock:
            self._scopes = self._relevant_scopes(desired, scopes)
        error = None
        types, skipped = [], []
        for kind in desired:
            scope = SUBS[kind][1]
            if scope is None or scope in scopes:
                types.append(kind)
            elif kind == REDEMPTION:
                self.log.info("EventSub: %s not granted; re-link Twitch for channel-point rewards", REDEMPTION_SCOPE)
                error = "relink"
            else:
                self._skip(kind, "not granted")
                skipped.append(kind)
        subs = {}
        for kind in types:
            if time.monotonic() - welcomed > SUBSCRIBE_WINDOW:
                self.log.info("EventSub: subscribing took longer than %d s; reconnecting", SUBSCRIBE_WINDOW)
                self._set_error(error or "subscribe")
                return "reconnect"
            try:
                version, _, condition = SUBS[kind]
                r = twitch.api("POST", "/eventsub/subscriptions", json={
                    "type": kind, "version": version, "condition": condition(me_id),
                    "transport": {"method": "websocket", "session_id": conn.session_id}})
                data = (r or {}).get("data") or [{}]
                subs[kind] = clean(data[0].get("id") if isinstance(data[0], dict) else "", 100)
                self._skip_logged = {k for k in self._skip_logged if k[0] != kind}
            except Exception as e:
                status = getattr(e, "status", None)
                if kind in ALERTS and status == 403:      # optional: skip just this one
                    self._skip(kind, "refused by Twitch")
                    skipped.append(kind)
                    continue
                self.log.info("EventSub: subscribing to %s failed (%s: %s)", kind, type(e).__name__, clean(e))
                if status == 401:
                    # The login is dead: the other types would fail the same way. Next cycle hits SessionExpired.
                    error = "relink"
                    break
                if kind == REDEMPTION and status == 403:
                    error = "relink"
                elif error is None:
                    error = "subscribe"
        with self._lock:
            self._subs = subs
            self._skipped = skipped
            self._connected = bool(subs)
            self._error = error
        self._publish()
        if not subs:
            return "reconnect"
        self.log.info("EventSub connected (%s)", ", ".join(sorted(subs)))
        self.bot.dispatch("eventsub_session", {})     # a fresh session: listeners drop stale live state
        return None

    def _on_reconnect(self, conn, session):
        sid = session.get("id") if isinstance(session, dict) else None
        if conn.session_id is None or sid != conn.session_id:
            self.log.info("EventSub: ignored a reconnect for another session")
            return None
        url = session.get("reconnect_url")
        if valid_reconnect_url(url):
            return ("move", url)
        self.log.info("EventSub: ignored an unexpected reconnect_url (%s); reconnecting normally",
                      clean(url, LOG_MAX))
        with self._lock:
            self._subs = {}     # a normal reconnect is a new session: subscribe again
        return "reconnect"

    def _check_sub(self, conn, sub, meta=None):
        """The subscription object if it is one of ours (type, version and exact condition), for our
        broadcaster, on this socket's session."""
        if not isinstance(sub, dict):
            return None
        kind = sub.get("type")
        with self._lock:
            ours = isinstance(kind, str) and kind in self._subs and kind in SUBS
        if not ours or (meta is not None and meta.get("subscription_type") != kind):
            self.log.info("EventSub: dropped a message for subscription type %r", clean(kind, 80))
            return None
        version, _, condition = SUBS[kind]
        if (meta is not None and meta.get("subscription_version") != version) or \
                ("version" in sub and sub.get("version") != version):
            self.log.info("EventSub: dropped a %s message for another subscription version", kind)
            return None
        if not self._me_id or not condition_matches(sub.get("condition"), condition(self._me_id)):
            self.log.info("EventSub: dropped a %s message for another broadcaster or condition", kind)
            return None
        transport = sub.get("transport") if isinstance(sub.get("transport"), dict) else {}
        if transport.get("session_id") not in conn.accept:
            self.log.info("EventSub: dropped a %s message for another session", kind)
            return None
        return sub

    def _on_notification(self, conn, meta, payload):
        ts = parse_ts(meta.get("message_timestamp"))
        if ts is None:
            self.log.info("EventSub: dropped a notification without a valid timestamp")
            return
        age = time.time() - ts
        if age > STALE:
            if not self._skew_logged:
                self._skew_logged = True
                self.log.warning("EventSub: dropped a notification %d s old; if this repeats, check the "
                                 "system clock (clock skew)", int(age))
            else:
                self.log.debug("EventSub: dropped a stale notification (%d s)", int(age))
            return
        sub = self._check_sub(conn, payload.get("subscription"), meta)
        event = payload.get("event")
        if sub is None or not isinstance(event, dict):
            return
        kind = sub["type"]
        if "broadcaster_user_id" in event and event.get("broadcaster_user_id") != self._me_id:
            self.log.info("EventSub: dropped a %s event for another broadcaster", kind)
            return
        eid = event.get("id")
        if kind == REDEMPTION and (not isinstance(eid, str) or not eid):
            self.log.info("EventSub: dropped a redemption without an id")
            return
        if isinstance(eid, str) and eid and not self._seen_events.add(f"{kind}:{eid}"):
            self.log.debug("EventSub: duplicate %s event %s", kind, clean(eid))
            return
        if kind in ALERTS:
            self._on_alert(kind, event, ts)
        elif kind == REDEMPTION:
            reward = event.get("reward") if isinstance(event.get("reward"), dict) else {}
            redemption = {
                "id": clean(eid, 100), "reward_id": clean(reward.get("id"), 100),
                "reward_title": clean(reward.get("title"), 100), "user": clean(event.get("user_name"), 100),
                "user_login": clean(event.get("user_login"), 100), "user_id": clean(event.get("user_id"), 100),
                "input": clean(event.get("user_input"), INPUT_MAX), "status": clean(event.get("status"), 32),
                "ts": parse_ts(event.get("redeemed_at")) or ts}
            self.log.info("redemption of %r by %s (input %r)", redemption["reward_title"],
                          redemption["user"], redemption["input"][:LOG_MAX])
            self.bot.dispatch("reward_redemption", redemption)
        elif kind == "stream.online":
            online = {"stream_id": clean(event.get("id"), 64), "type": clean(event.get("type"), 32),
                      "started_at": clean(event.get("started_at"), 40)}
            self.log.info("EventSub: stream online (%s)", online["type"] or "no type")
            self.bot.dispatch("stream_online", online)
        elif kind == "stream.offline":
            self.log.info("EventSub: stream offline")
            self.bot.dispatch("stream_offline", {})

    def _on_alert(self, kind, event, ts):
        ev = normalise(kind, event, ts, self._me_id)
        if ev is None:
            self.log.info("EventSub: dropped an invalid %s event", kind)
            return
        if ev["kind"] == "follow":
            if not ev["user_id"]:
                self.log.info("EventSub: dropped a follow without a user id")
                return
            if not self._seen_follows.add(f"follow:{ev['user_id']}"):
                self.log.debug("EventSub: repeat follow by %s within a day", ev["user"])
                return
        self.log.debug("EventSub: %s from %s", ev["kind"], ev["user"] or "?")
        self.bot.dispatch("twitch_event", ev)

    def _on_revocation(self, conn, sub):
        sub = self._check_sub(conn, sub)
        if sub is None:
            return
        status = clean(sub.get("status"), 60)
        self.log.info("EventSub: Twitch revoked %s (%s); it is resubscribed on the next connect",
                      sub["type"], status)
        with self._lock:
            self._subs.pop(sub["type"], None)
            self._connected = bool(self._subs)
            self._error = "relink" if sub["type"] == REDEMPTION and status == "authorization_revoked" \
                else self._error or "revoked"
        self._publish()

    # ----- state

    def _set_error(self, error):
        with self._lock:
            self._error = error
        self._publish()

    def snapshot(self):
        with self._lock:
            return {"connected": bool(self._connected), "subscriptions": sorted(self._subs),
                    "skipped": sorted(self._skipped), "error": self._error}

    def _publish(self):
        snap = self.snapshot()
        if snap != self._published:
            self._published = snap
            try:
                self.bot.hub.publish("eventsub", snap)
            except Exception:
                self.log.exception("EventSub: publishing status failed")


def setup(bot):
    bot.add_module(EventSub(bot))
