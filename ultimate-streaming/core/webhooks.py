"""Outgoing webhooks (Discord for now): checking a URL, posting to it, and keeping it out of logs.

A webhook URL is a password: anyone holding it can post to the channel. So:

- validate(type, url) only accepts a real Discord webhook URL (https, a Discord host, the
  /api/webhooks/<id>/<token> path, no userinfo, port, query or fragment) and returns it in one
  canonical form; its ValueError message never contains the URL.
- redact(text) removes webhook paths and tokens from any text, and install_log_filter() puts a
  filter doing the same on urllib3's loggers (it logs request paths at DEBUG) and the app's.
- post(url, payload) never raises and never logs the URL or an exception traceback: it returns
  a Result whose reason is written by us (remote text only after cleaning, at most 120 chars).

    r = webhooks.post(url, {"content": "hi", "allowed_mentions": {"parse": []}})
    r.kind   # "ok" | "retry" (r.after seconds, or None) | "broken" (401/403/404) | "failed"

"retry" is only returned when the post surely didn't go through (couldn't connect, 429, 5xx); a
read timeout or a connection dropped mid-request is "failed", so a retry can never post twice.
"""
import logging
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import urlsplit

import requests
from urllib3.exceptions import ProtocolError

from . import text as textclean

log = logging.getLogger(__name__)

TYPES = ("discord",)
DISCORD_HOSTS = frozenset(("discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"))
DISCORD_PATH = re.compile(r"/api/webhooks/([0-9]{15,25})/([A-Za-z0-9_-]{20,128})")
URL_MAX = 300
TIMEOUT = (5, 10)                 # connect, read
RETRY_AFTER_MAX = 60.0
RETRY_AFTER_DEFAULT = 5.0
REMOTE_MAX = 120
MAYBE_SENT = "Discord didn't answer in time; the post may have gone through. Check the channel."
BAD_URL = "That isn't a Discord webhook URL (it should look like https://discord.com/api/webhooks/…)."
# A webhook path (plain or %-encoded), or what is left of one after its prefix was cut off.
_SECRET_RE = re.compile(r"(?i)(api(?:/|%2F))?webhooks(?:/|%2F)[^\s'\"?#)<>&,;]*")
LOGGERS = ("urllib3", "urllib3.connectionpool", "urllib3.util.retry", "requests", "deck", "core.webhooks",
           "modules.announce", "deck.Announce", "modules.discord", "deck.Discord")


def clean(text, n=REMOTE_MAX):
    """Text from the network made safe to show: ANSI escapes and control/format characters (ZWJ
    too) removed, webhook paths redacted, then cut to at most n characters (after the redaction, so
    a cut can't leave part of a path unredacted)."""
    text = "" if text is None else str(text)[:n * 4]
    # Redact before cleaning too: stripping an escape can eat the next letter ("\x1b[webhooks"
    # loses its "w"), which would hide the path from the pattern after cleaning.
    text = textclean.clean(redact(text), newlines="drop", marks=None, collapse=False, keep_zwj=False)
    return redact(text)[:n]


def redact(text, secrets=()):
    """text with every webhook path replaced by <webhook>; also each URL in `secrets` and its
    token on its own, wherever they appear."""
    text = _SECRET_RE.sub("<webhook>", "" if text is None else str(text))
    for url in secrets:
        m = DISCORD_PATH.search(url) if isinstance(url, str) else None
        if m:
            text = text.replace(m.group(2), "<webhook>")
    return text


def validate(kind, url):
    """The canonical webhook URL for `kind`, or ValueError with a message for the user (which
    never contains the URL)."""
    if kind not in TYPES:
        raise ValueError("Only Discord webhooks are supported for now.")
    if not isinstance(url, str):
        raise ValueError(BAD_URL)
    url = url.strip()
    if not url or len(url) > URL_MAX or any(c.isspace() or unicodedata.category(c)[0] == "C" for c in url):
        raise ValueError(BAD_URL)
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        raise ValueError(BAD_URL) from None
    m = DISCORD_PATH.fullmatch(parts.path)
    if (parts.scheme.lower() != "https" or host not in DISCORD_HOSTS or parts.netloc.lower() != host
            or port is not None or parts.query or parts.fragment or "#" in url or "?" in url or not m):
        raise ValueError(BAD_URL)
    return f"https://{host}/api/webhooks/{m.group(1)}/{m.group(2)}"


def host_of(url):
    """The host of a stored webhook URL, for showing which service it posts to (never the URL)."""
    try:
        host = (urlsplit(url).hostname or "").lower() if isinstance(url, str) else ""
    except ValueError:
        return None
    return host if host in DISCORD_HOSTS else None


@dataclass(frozen=True)
class Result:
    kind: str                     # ok | retry | broken | failed
    reason: str | None = None     # for the panel and the log; written by us
    after: float | None = None    # retry: seconds Discord asked us to wait (None: use our backoff)
    status: int | None = None     # the HTTP status, if there was a response

    @property
    def ok(self):
        return self.kind == "ok"


def _retry_after(response):
    """Seconds from a 429's JSON retry_after (seconds, float) or its Retry-After header; capped."""
    value = None
    try:
        body = response.json()
        if isinstance(body, dict):
            value = body.get("retry_after")
    except Exception:
        value = None
    if value is None:
        value = (getattr(response, "headers", None) or {}).get("Retry-After")
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return RETRY_AFTER_DEFAULT
    if seconds != seconds or seconds < 0:      # NaN, negative
        return RETRY_AFTER_DEFAULT
    return min(seconds, RETRY_AFTER_MAX)


def _remote_message(response):
    try:
        body = response.json()
    except Exception:
        return ""
    msg = body.get("message") if isinstance(body, dict) else None
    return clean(msg) if isinstance(msg, str) else ""


def _maybe_sent(e):
    """True when the request may already have reached Discord: no answer in time (ReadTimeout),
    a broken answer, or the connection dropped after it was made (requests wraps urllib3's
    ProtocolError or a socket error as ConnectionError; a failure to connect comes wrapped in
    MaxRetryError instead). Not connecting at all (ConnectTimeout, DNS, refused, TLS) is safe to retry."""
    if isinstance(e, requests.ConnectTimeout):
        return False
    if isinstance(e, (requests.Timeout, requests.exceptions.ChunkedEncodingError,
                      requests.exceptions.ContentDecodingError)):
        return True
    if isinstance(e, requests.ConnectionError) and not isinstance(e, requests.exceptions.SSLError):
        cause = e.args[0] if e.args else None
        return isinstance(cause, (ProtocolError, OSError))
    return False


def post(url, payload, *, http=None):
    """POST payload as JSON to the webhook (?wait=true, no redirects, 5 s connect / 10 s read).
    Never raises; returns a Result. `http` replaces requests.post (tests)."""
    http = http or requests.post
    try:
        r = http(url + "?wait=true", json=payload, timeout=TIMEOUT, allow_redirects=False)
    except Exception as e:
        log.warning("webhook post failed: %s", clean(redact(f"{type(e).__name__}: {e}", (url,)), 200))
        if _maybe_sent(e):        # retrying could post it twice
            return Result("failed", MAYBE_SENT)
        return Result("retry", "Couldn't reach Discord.")
    try:
        code = int(r.status_code)
        if 200 <= code < 300:
            return Result("ok", status=code)
        if code == 429:
            after = _retry_after(r)
            log.info("webhook rate limited; retry in %.1f s", after)
            return Result("retry", "Discord asked us to slow down (HTTP 429).", after=after, status=code)
        if code >= 500:
            log.warning("webhook post failed: HTTP %d", code)
            return Result("retry", f"Discord had a problem (HTTP {code}).", status=code)
        if code in (401, 403, 404):
            log.warning("webhook post failed: HTTP %d (the webhook was deleted or its URL is wrong)", code)
            return Result("broken", f"Discord says this webhook doesn't exist any more (HTTP {code}). "
                                    "Replace its URL.", status=code)
        if 300 <= code < 400:
            log.warning("webhook post failed: HTTP %d (redirects are not followed)", code)
            return Result("failed", f"Discord answered with a redirect (HTTP {code}).", status=code)
        msg = _remote_message(r)
        log.warning("webhook post failed: HTTP %d%s", code, f" ({msg})" if msg else "")
        return Result("failed", f"Discord rejected the post (HTTP {code})" + (f": {msg}" if msg else "."),
                      status=code)
    finally:
        close = getattr(r, "close", None)
        if close:
            try:
                close()
            except Exception:
                pass


class RedactFilter(logging.Filter):
    """Strips webhook paths/tokens from a record's message, exception text and stack."""
    redacts_webhook = True        # marker: install_log_filter() never stacks a second copy

    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if _SECRET_RE.search(msg):
            record.msg, record.args = redact(msg), ()
        if record.exc_info and not record.exc_text:
            try:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            except Exception:
                record.exc_text = None
        if record.exc_text and _SECRET_RE.search(record.exc_text):
            record.exc_text = redact(record.exc_text)
        if record.stack_info and _SECRET_RE.search(record.stack_info):
            record.stack_info = redact(record.stack_info)
        return True


def install_log_filter(names=LOGGERS):
    """Add the redacting filter to these loggers, once each (safe to call again, e.g. on reload).
    A logger's filters only see records logged on that logger itself, hence the list."""
    for name in names:
        logger = logging.getLogger(name)
        if not any(getattr(f, "redacts_webhook", False) for f in logger.filters):
            logger.addFilter(RedactFilter())


install_log_filter()
