"""Posts a camera still to a Discord webhook while the printer is printing.

One post as a print starts, then one each time it reaches a multiple of
`every_layers` layers, and a final shot `final_delay` seconds after it
finishes or fails (by then the toolhead has parked). Nothing while idle,
paused or disconnected. Posts are at least `min_gap` seconds apart, which
keeps fast layers under Discord's limit of about 30 messages a minute per
channel; a round held back by the gap goes out when it ends, showing the
layer the print is on then. Several multiples passed at once make one post,
not a backlog. A round that can't reach the camera or Discord is skipped and
logged; the next multiple tries again.

Frame grabbing, printer status and the caption text are injected, so this
module depends on nothing else in the plugin. The webhook URL carries its own
secret token: it never appears in logs, status() or exception text.
"""
import json
import logging
import re
import threading
import time
from urllib.parse import urlsplit

import requests

log = logging.getLogger(__name__)

_HOSTS = {"discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"}
_TOKEN_PATH = re.compile(r"/api/webhooks/[^\s'\"?#)]*")
_BAD_URL = "That isn't a Discord webhook URL."


class _RedactWebhookFilter(logging.Filter):
    """Strips webhook tokens from a record's message. urllib3 logs the request
    path at DEBUG, and the path carries the token."""
    redacts_webhook = True   # marker so a reloaded module doesn't stack a second copy

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if _TOKEN_PATH.search(msg):
            record.msg = _TOKEN_PATH.sub("<webhook>", msg)
            record.args = ()
        return True


def _install_log_filter():
    logger = logging.getLogger("urllib3.connectionpool")
    if not any(getattr(f, "redacts_webhook", False) for f in logger.filters):
        logger.addFilter(_RedactWebhookFilter())


def validate_webhook(url) -> str:
    """Return the URL if it is a Discord webhook, else raise ValueError
    (whose message never contains the URL)."""
    if not isinstance(url, str):
        raise ValueError(_BAD_URL)
    url = url.strip()
    try:
        parts = urlsplit(url)
        host = parts.hostname
        has_userinfo = "@" in parts.netloc
        parts.port  # noqa: B018 - raises on a malformed port
    except ValueError:
        raise ValueError(_BAD_URL) from None
    if (parts.scheme != "https" or host not in _HOSTS or has_userinfo or parts.fragment
            or not parts.path.startswith("/api/webhooks/")):
        raise ValueError(_BAD_URL)
    return url


class DiscordSnapshots:
    def __init__(self, webhook_url, *, grab_frame, printer_snapshot, caption,
                 every_layers: int = 10, min_gap: float = 15, final_delay: float = 30,
                 poll: float = 1, post=None):
        self._url = validate_webhook(webhook_url)
        _install_log_filter()
        self.grab_frame = grab_frame              # () -> JPEG bytes; raises if unavailable
        self.printer_snapshot = printer_snapshot  # () -> summary dict
        self.caption = caption                    # (summary, ended) -> str
        self.every_layers = every_layers
        self.min_gap = min_gap
        self.final_delay = final_delay
        self.poll = poll                          # seconds between looks at the layer
        self._http_post = post or requests.post
        self._lock = threading.Lock()             # guards the state below
        self._post_lock = threading.Lock()        # one upload at a time
        self._kick = threading.Event()
        self._stopping = threading.Event()
        self._thread = None
        self._final = None                        # pending threading.Timer
        self._last_post = None   # monotonic time of the last successful post
        self._last_round = None  # monotonic time the loop last tried to post
        self._mark = None        # layer // every_layers at the last round; None until a print is seen
        self._error = None       # why the last attempt failed, in words for the page

    def status(self) -> dict:
        now = time.monotonic()
        summary = self._summary()
        printing = bool(summary.get("connected") and summary.get("active"))
        with self._lock:
            return {
                "printing": printing,
                "every_layers": self.every_layers,
                "last_post_ago": int(now - self._last_post) if self._last_post is not None else None,
                "next_layer": self._next_layer(summary) if printing else None,
                "error": self._error,
            }

    def _summary(self) -> dict:
        try:
            return self.printer_snapshot() or {}
        except Exception:
            log.exception("printer status unavailable")
            return {}

    def _next_layer(self, summary: dict):
        if self._mark is None:
            return None
        layer = (self._mark + 1) * self.every_layers
        total = summary.get("total_layers")
        # Past the last multiple only the final shot is left.
        return layer if not total or layer <= total else None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="bambu-snapshots", daemon=True)
        self._thread.start()

    def stop(self):
        self._stopping.set()
        self._cancel_final()
        self._kick.set()
        thread, self._thread = self._thread, None
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5)

    def on_event(self, event: str):
        """Printer event handler; safe to call from any thread."""
        if event == "started":
            # A final shot still waiting from the last print would now show the new one.
            self._cancel_final()
            with self._lock:
                self._starting = True
            self._kick.set()
        elif event == "ended":
            timer = threading.Timer(self.final_delay, self._final_shot)
            timer.daemon = True
            with self._lock:
                if self._final:
                    self._final.cancel()
                if self._stopping.is_set():
                    return
                self._final = timer
            timer.start()

    _starting = False

    def _run(self):
        while not self._stopping.is_set():
            self._kick.wait(self.poll)
            if self._stopping.is_set():
                break
            self._kick.clear()
            try:
                self._round()
            except Exception:
                log.exception("snapshot round failed")

    def _round(self):
        """One look at the printer; posts if a post is due."""
        with self._lock:
            starting, self._starting = self._starting, False
        summary = self._summary()
        if not (summary.get("connected") and summary.get("active")):
            return
        with self._lock:
            if starting:
                # a new print: count its layers from here
                layer = summary.get("layer")
                self._mark = None if layer is None else layer // self.every_layers
            due = starting or self._layer_due(summary.get("layer"))
            if due:
                self._last_round = time.monotonic()
        if due:
            self._post_safely()

    def _layer_due(self, layer) -> bool:
        """True when the print has reached a multiple of every_layers it
        hasn't had a post for, and the last round is min_gap behind us.
        Call with the lock held."""
        if layer is None:
            return False
        mark = layer // self.every_layers
        if self._mark is None or mark < self._mark:
            # First look at this print (started mid-print, or a new print began
            # while the printer was out of reach): start counting.
            self._mark = mark
            return False
        if mark == self._mark:
            return False
        if self._last_round is not None and time.monotonic() - self._last_round < self.min_gap:
            return False
        self._mark = mark
        return True

    def _final_shot(self):
        with self._lock:
            self._final = None
        if not self._stopping.is_set():
            self._post_safely(ended=True)

    def _cancel_final(self):
        with self._lock:
            timer, self._final = self._final, None
        if timer:
            timer.cancel()

    def _post_safely(self, ended: bool = False):
        # One bad round must never end the loop.
        try:
            self.post(ended=ended)
        except Exception:
            log.exception("snapshot round failed")

    def post(self, ended: bool = False) -> bool:
        """Grab a frame and post it now. True if Discord accepted it."""
        with self._post_lock:
            text = self.caption(self._summary(), ended)
            try:
                frame = self.grab_frame()
            except Exception as e:
                log.warning("snapshot skipped, camera unavailable: %s", self._redact(f"{type(e).__name__}: {e}"))
                return self._failed("Couldn't grab a frame from the camera.")

            payload = {"content": text, "allowed_mentions": {"parse": []}}
            filename = time.strftime("print-%Y%m%d-%H%M%S.jpg")
            try:
                r = self._http_post(
                    self._url,
                    data={"payload_json": json.dumps(payload)},
                    files={"files[0]": (filename, frame, "image/jpeg")},
                    timeout=30,
                )
            except Exception as e:
                log.warning("Discord post failed: %s", self._redact(f"{type(e).__name__}: {e}"))
                return self._failed("Couldn't reach Discord.")
            if r.status_code >= 300:
                log.warning("Discord post failed: HTTP %s", r.status_code)
                return self._failed(f"Discord rejected the post (HTTP {r.status_code}). Check the webhook URL.")
            log.info("posted snapshot to Discord: %s", text)
            with self._lock:
                self._last_post = time.monotonic()
                self._error = None
            return True

    def _failed(self, reason: str) -> bool:
        with self._lock:
            self._error = reason
        return False

    def _redact(self, text: str) -> str:
        # requests quotes just the path in connection errors, so cover that too.
        parts = urlsplit(self._url)
        pathq = parts.path + ("?" + parts.query if parts.query else "")
        for secret in (self._url, pathq, parts.path):
            text = text.replace(secret, "<webhook>")
        return _TOKEN_PATH.sub("<webhook>", text)
