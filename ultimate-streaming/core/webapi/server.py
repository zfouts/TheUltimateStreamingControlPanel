"""HTTP handler for the control panel: access checks, static files, JSON routes, events.

Security model (see the Editor module's docstring): the server only ever listens on
127.0.0.1; the Host header must be ours (defeats DNS rebinding); a present Origin must
be ours (defeats other websites); every /api call carries the random token in the
X-Deck-Token header, except POST /api/session, which trades a one-time launch code (from the
page's URL, see core.editor_auth) for the token: it is still Host/Origin checked, answers only a
client socket owned by this user (per /proc/net/tcp), and refuses everything for a minute after
SESSION_FAILURES failed tries; POST bodies must be JSON and small, except on upload routes
(Router.upload), which take a raw application/octet-stream body with a Content-Length of at
most that route's own limit and a total time limit, streamed to the handler. An upload refused
before its body was read gets its error with `Connection: close`, then the write side is shut and
a little of the body drained, so the browser reads the error instead of a reset. Static files come only from
the editor/ folder, by an allow-list of extensions, with path traversal refused.

The event stream (GET /api/events) is Server-Sent Events. The page reads it with
fetch() so the token still travels in the header rather than in the URL.
"""
import collections
import hmac
import json
import os
import queue
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, unquote, urlparse

from . import ApiError
from .. import editor_auth

MAX_BODY = 4 * 1024 * 1024
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
                ".woff2": "font/woff2", ".ico": "image/x-icon", ".webmanifest": "application/manifest+json"}
CSP = ("default-src 'self'; img-src 'self' data: https://static-cdn.jtvnw.net; style-src 'self'; "
       "script-src 'self'; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; "
       "base-uri 'none'; form-action 'none'")
PING_SECONDS = 15
MAX_SUBSCRIBERS = 8
DRAIN_SECONDS = 2.0          # after refusing an upload: how long / how much of its body to drain
DRAIN_BYTES = 64 * 1024
SESSION_FAILURES, SESSION_WINDOW = 10, 60   # failed launch-code tries allowed per window (seconds)


class FailureLimit:
    """At most `limit` failures in any `window` seconds; thread-safe, in memory."""

    def __init__(self, limit=SESSION_FAILURES, window=SESSION_WINDOW):
        self.limit, self.window = limit, window
        self._times = collections.deque()
        self._lock = threading.Lock()

    def _prune(self, now):
        while self._times and now - self._times[0] >= self.window:
            self._times.popleft()

    def blocked(self):
        with self._lock:
            self._prune(time.monotonic())
            return len(self._times) >= self.limit

    def fail(self):
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            self._times.append(now)


class BodyStream:
    """The raw request body of an upload route: read() never goes past Content-Length, and raises
    ApiError(408) once `deadline` (time.monotonic) has passed, however slowly the client sends."""

    def __init__(self, rfile, length, sock=None, deadline=None, read_timeout=None):
        self._rfile, self.length, self.remaining = rfile, length, length
        self._sock, self._deadline, self._read_timeout = sock, deadline, read_timeout

    def read(self, n=-1):
        n = self.remaining if n is None or n < 0 else min(n, self.remaining)
        if n <= 0:
            return b""
        if self._deadline is not None:
            left = self._deadline - time.monotonic()
            if left <= 0:
                raise ApiError("the upload took too long", 408)
            if self._sock is not None:
                self._sock.settimeout(min(left, self._read_timeout or left))
        try:
            # read1: at most one recv, so the deadline also bounds a client trickling bytes
            data = self._rfile.read1(n) if hasattr(self._rfile, "read1") else self._rfile.read(n)
        except TimeoutError:
            raise ApiError("the upload took too long", 408) from None
        self.remaining -= len(data)
        if not data:
            self.remaining = 0   # the client went away: a short body
        return data


class Handler(BaseHTTPRequestHandler):
    """Expects on the server object: .editor (has .log, .token, .router, .hub), .allowed_hosts,
    .static_dir, .stopping (a threading.Event set when the server shuts down) and
    .session_limit (a FailureLimit for POST /api/session)."""

    server_version = "StreamDeckPanel"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 30   # a stalled client can't hold a handler thread forever (the event stream pings instead)
    _body_read = False  # this POST's body has been consumed (else a refusal closes the connection)
    _unread = None  # an upload request whose body hasn't been read: True, or its BodyStream

    def log_message(self, fmt, *args):
        self.server.editor.log.debug("http: " + fmt, *args)

    # ----- responses

    def _headers(self, code, ctype, length=None, close=False):
        self.send_response(code)
        if close:
            self.send_header("Connection", "close")
        self.send_header("Content-Type", ctype)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        self.end_headers()

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self._headers(code, ctype, len(data))
        self.wfile.write(data)

    def _error(self, code, message, extra=None):
        pending = self._unread
        if pending is None and self.command == "POST" and not self._body_read:
            return self._reject(code, message)   # a body we never read must not become the next request
        if pending is True or (pending is not None and pending.remaining > 0):
            return self._reject(code, message)
        self._send(code, {**(extra or {}), "error": message})

    def _reject(self, code, message):
        """Refuse an upload whose body is still (partly) unsent: answer with Connection: close,
        shut our write side, drain up to DRAIN_BYTES for DRAIN_SECONDS, and let the server close.
        Closing with unread data would send a reset that can destroy the response in flight."""
        self._unread = None
        data = json.dumps({"error": message}).encode()
        self._headers(code, "application/json; charset=utf-8", len(data), close=True)
        self.close_connection = True
        try:
            self.wfile.write(data)
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)
            end, got = time.monotonic() + DRAIN_SECONDS, 0
            while got < DRAIN_BYTES:
                left = end - time.monotonic()
                if left <= 0:
                    break
                self.connection.settimeout(left)
                chunk = self.connection.recv(min(16384, DRAIN_BYTES - got))
                if not chunk:
                    break
                got += len(chunk)
        except OSError:
            pass

    # ----- checks

    def _host_ok(self):
        if self.headers.get("Host") not in self.server.allowed_hosts:
            self._error(421, "wrong host")
            return False
        origin = self.headers.get("Origin")
        if origin and origin.split("://", 1)[-1] not in self.server.allowed_hosts:
            self._error(403, "cross-origin request refused")
            return False
        return True

    def _token_ok(self, sent):
        tok = self.server.editor.token or ""
        return bool(tok) and hmac.compare_digest(sent.encode(), tok.encode())

    def _authorised(self):
        if not self._token_ok(self.headers.get("X-Deck-Token", "")):
            self._error(401, "missing or wrong token: open the editor with `python3 main.py --open-editor`")
            return False
        return True

    def _body(self):
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            raise ApiError("expected JSON")
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0:
            raise ApiError("bad Content-Length")
        if length > MAX_BODY:
            raise ApiError("request too large", 413)
        raw = self.rfile.read(length)
        self._body_read = True
        data = json.loads(raw or b"{}")
        if not isinstance(data, dict):
            raise ApiError("expected a JSON object")
        return data

    # ----- static files

    def _static(self, path):
        rel = unquote(path).lstrip("/") or "index.html"
        if "\x00" in rel:
            return self._error(404, "not found")
        base = os.path.realpath(self.server.static_dir)
        full = os.path.realpath(os.path.join(base, rel))
        ext = os.path.splitext(full)[1].lower()
        if not full.startswith(base + os.sep) or ext not in STATIC_TYPES or ".." in rel.split("/"):
            return self._error(404, "not found")
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            return self._error(404, "not found")
        self._send(200, data, STATIC_TYPES[ext])

    # ----- event stream

    def _events(self):
        hub, stopping = self.server.editor.hub, self.server.stopping
        sent = self.headers.get("X-Deck-Token", "")
        if hub.subscribers >= MAX_SUBSCRIBERS:
            return self._error(503, "too many open control panels; close one")
        q = hub.subscribe()
        try:
            self._headers(200, "text/event-stream; charset=utf-8")
            self._write_event({"type": "hello", "ts": time.time(), "data": {"subscribers": hub.subscribers}})
            while not stopping.is_set():
                try:
                    event = q.get(timeout=PING_SECONDS)
                except queue.Empty:
                    event = {"type": "ping", "ts": time.time(), "data": {}}
                if event is None or not hub.is_subscribed(q):
                    break   # dropped for not reading: end the stream so the browser reconnects and resyncs
                if not self._token_ok(sent):
                    break   # signed out (token rotated): the reconnect gets a 401
                self._write_event(event)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            hub.unsubscribe(q)
            self.close_connection = True

    def _write_event(self, event):
        payload = json.dumps(event["data"], default=str)
        self.wfile.write(f"event: {event['type']}\nid: {event['ts']}\ndata: {payload}\n\n".encode())
        self.wfile.flush()

    # ----- routing

    def do_GET(self):
        if not self._host_ok():
            return
        url = urlparse(self.path)
        if not url.path.startswith("/api/"):
            return self._static(url.path)
        if not self._authorised():
            return
        if url.path == "/api/events":
            return self._events()
        fn = self.server.editor.router.find_get(url.path)
        if not fn:
            return self._error(404, "not found")
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        self._call(fn, query)

    def _upload(self, fn, max_bytes, max_seconds=None):
        """An upload route: checks the headers before reading anything; an unread body is never
        parsed as the next request (see _reject)."""
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        length = (self.headers.get("Content-Length") or "").strip()
        error = None
        if ctype != "application/octet-stream":
            error = (415, "expected application/octet-stream")
        elif self.headers.get("Transfer-Encoding") or not length.isdigit():
            error = (411, "Content-Length is required")
        elif int(length) > max_bytes:
            error = (413, f"upload too large (over {max_bytes // (1024 * 1024)} MB)")
        elif int(length) == 0:
            error = (400, "empty upload")
        if error:
            return self._reject(*error)
        deadline = time.monotonic() + max_seconds if max_seconds else None
        stream = BodyStream(self.rfile, int(length), self.connection, deadline, self.timeout)
        self._unread = stream
        try:
            self._call(fn, stream)
        finally:
            self._unread = None
            if stream.remaining:
                self.close_connection = True
            try:
                self.connection.settimeout(self.timeout)   # BodyStream shortened it for the deadline
            except OSError:
                pass

    def _session(self):
        """POST /api/session {"code": ...} -> {"token": ...}; see the module docstring."""
        limit, editor = self.server.session_limit, self.server.editor
        if limit.blocked():
            return self._error(429, "Too many tries; wait a minute")
        try:
            code = self._body().get("code")
        except ApiError as e:
            limit.fail()
            return self._error(e.status, str(e))
        except (ValueError, json.JSONDecodeError) as e:
            limit.fail()
            return self._error(400, f"bad JSON: {e}")
        owners = editor_auth.peer_uids(self.client_address, self.server.server_address[1])
        if owners and owners != {os.getuid()}:
            limit.fail()
            editor.log.warning("refused a panel sign-in from another user's program (uid %s)",
                               ", ".join(map(str, sorted(owners))))
            return self._error(403, "This panel belongs to another user")
        if not editor_auth.redeem_launch_code(code):
            limit.fail()
            return self._error(403, "This link has expired or was already used")
        self._send(200, {"token": editor.token})

    def do_POST(self):
        self._body_read = False
        path = urlparse(self.path).path
        if path == "/api/session":
            self._unread = None
            return self._session() if self._host_ok() else None
        upload = self.server.editor.router.uploads.get(path)
        self._unread = True if upload else None   # refusals of an upload go through _reject
        if not (self._host_ok() and self._authorised()):
            return
        if upload:
            return self._upload(*upload)
        fn = self.server.editor.router.find_post(path)
        if not fn:
            return self._error(404, "not found")
        try:
            body = self._body()
        except ApiError as e:
            return self._error(e.status, str(e))
        except (ValueError, json.JSONDecodeError) as e:
            return self._error(400, f"bad JSON: {e}")
        self._call(fn, body)

    def _call(self, fn, arg):
        try:
            return self._send(200, fn(arg))
        except ApiError as e:
            return self._error(e.status, str(e), e.extra)
        except Exception as e:
            status = getattr(e, "status", None)
            if type(e).__name__ == "Conflict":
                status = 409
            return self._error(status or 400, str(e) or type(e).__name__)
