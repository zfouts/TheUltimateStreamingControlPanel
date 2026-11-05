"""Event hub: how the daemon tells the web control panel what just happened.

Anything in the daemon can publish (thread-safe); every open browser tab subscribes
and receives the events as a Server-Sent Events stream (core/webapi/server.py).

    bot.hub.publish("page", {"page": "twitch"})

Event types and payloads (the contract the frontend relies on):
    key          {page, key}            a deck key was redrawn (state/display changed)
    page         {page}                 the deck switched page
    status       {<module_key>: {...}}  every module's snapshot(), published when it changes
    chat         {id, user, user_id, text, color, mod, sub, badges, ts}
    log          {ts, level, logger, msg}
    config       {hash}                 config.yaml was reloaded
    twitch_auth  {...}                  the Twitch login flow progressed (same shape as GET /api/twitch/auth)
    ping         {}                     keep-alive every 15 s

"chat" and "log" keep a history ring so a freshly opened tab can show the recent past.
"""
import collections
import queue
import threading
import time

HISTORY = {"chat": 300, "log": 300}
MAX_QUEUE = 500   # a tab that stops reading is dropped rather than growing without bound


class Hub:
    def __init__(self):
        self._lock = threading.Lock()
        self._subs = set()
        self._history = {t: collections.deque(maxlen=n) for t, n in HISTORY.items()}

    def publish(self, type, data=None):
        data = dict(data or {})
        ts = time.time()
        if type in self._history:
            data.setdefault("ts", ts)
        event = {"type": type, "ts": ts, "data": data}
        with self._lock:
            if type in self._history:
                self._history[type].append(event)
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                self._drop(q)

    def _drop(self, q):
        """Unsubscribe a subscriber that stopped reading and tell its stream to end."""
        self.unsubscribe(q)
        try:
            q.get_nowait()       # make room for the end-of-stream sentinel (None)
        except queue.Empty:
            pass
        try:
            q.put_nowait(None)
        except queue.Full:
            pass

    def is_subscribed(self, q):
        with self._lock:
            return q in self._subs

    def subscribe(self):
        q = queue.Queue(maxsize=MAX_QUEUE)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subs.discard(q)

    def history(self, type, n=200):
        with self._lock:
            items = list(self._history.get(type, ()))
        return items[-n:] if n else items

    @property
    def subscribers(self):
        with self._lock:
            return len(self._subs)
