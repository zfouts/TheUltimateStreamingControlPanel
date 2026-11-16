"""HTTP handler for the control panel: access checks, static files, JSON routes, events.

Security model (see the Editor module's docstring): the panel's server only ever listens on
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
fetch() so the token still travels in the header rather than in the URL. At most
MAX_SUBSCRIBERS streams are open on the 127.0.0.1 listener; streams from other devices don't count.

Remote access (only while editor.remote.enabled is on; core/remote_access.py has the trust
model) is a second listener, RemoteServer with RemoteHandler, on editor.remote.listen:
editor.remote.port ("::" listens dual-stack). Once the first byte of a request has arrived, its
request line and headers must all arrive within HEAD_SECONDS, however the client spreads them
out, or the connection is closed unanswered (HeadDeadline; a connection that sends nothing is
closed after Handler.timeout, as on 127.0.0.1). Every request on it, static files included, first
passes these checks, in order:
  - the peer's address (normalised; an IPv4-mapped IPv6 address is IPv4) must not be loopback or
    one of this computer's own addresses (403): programs on this computer use the 127.0.0.1 panel;
  - the Host header must be exactly one of RemoteServer.allowed_hosts (421);
  - an Origin, if sent, must be http:// and one of them (403);
  - a request whose X-Deck-Token is an approved device's goes on; any other from a peer over its
    failure limit (remote_access.PeerLimit: per address, an IPv6 /64 counting as one address,
    plus a global cap) gets 429. However many addresses fail, an approved device isn't locked out.
Then POST /api/pair/start and /api/pair/poll need no token but must carry such an Origin
(browsers always send one on POST, so another website's page can't ask for access). Every other
/api call needs an approved device's token in X-Deck-Token (remote_access.Devices), else 401. The
127.0.0.1 panel's token is never accepted there, and device tokens never on 127.0.0.1. LOCAL_ONLY
routes (approving, denying and revoking devices, the launch-code exchange, signing out all panels)
are refused with 403 however the request is signed. Failed pairing starts and polls and wrong
tokens count towards the peer's failure limit. Each request from a device runs inside
core.request_context.remote(device), so config writers can refuse to change the `editor` section
from another device. Remote event streams have their own cap (MAX_REMOTE_SUBSCRIBERS), re-check
the device's token before every event (a revoke ends them), and never carry pair_request or
pair_done. At most MAX_REMOTE_CONNECTIONS connections are open at once (MAX_REMOTE_PER_PEER from
one address, an IPv6 /64 counting as one); stopping the listener shuts every open connection.
Refusals are logged at WARNING, at most once a minute per address (IPv6 /64) and reason; tokens,
secrets and pairing codes never are.
"""
import collections
import hmac
import io
import ipaddress
import json
import os
import queue
import socket
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import ApiError
from .. import editor_auth, request_context
from ..remote_access import (Refused, allowed_hosts, host_ok, is_loopback, local_addresses, normalize_ip,
                             own_addresses, peer_key)

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

MAX_REMOTE_SUBSCRIBERS = 4                     # event streams open at once from other devices
MAX_REMOTE_CONNECTIONS, MAX_REMOTE_PER_PEER = 64, 16
PAIR_BODY = 4096                               # bytes: the most a pairing request's JSON may be
HEAD_SECONDS = 10                              # a request's line and headers, from its first byte
ADDRESS_TTL = 60                               # seconds this computer's addresses are cached
WARN_EVERY = 60                                # seconds: one refusal warning per (address, reason)
PAIR_ROUTES = ("/api/pair/start", "/api/pair/poll")
LOCAL_ONLY = frozenset({"/api/remote/decide", "/api/remote/revoke", "/api/session", "/api/session/revoke"})
LOCAL_ONLY_MESSAGE = "Only the streaming computer can do this"
LOCAL_EVENTS = frozenset({"pair_request", "pair_done"})   # never sent to another device's stream


def _shown(text, n=80):
    """Untrusted text (a header) safe for a log line: at most n characters, all printable."""
    return "".join(c if c.isprintable() else "?" for c in str(text)[:n])


class StreamSlots:
    """At most `limit` event streams open at once on one listener; thread-safe."""

    def __init__(self, limit):
        self.limit, self.used = limit, 0
        self._lock = threading.Lock()

    def take(self):
        with self._lock:
            if self.used >= self.limit:
                return False
            self.used += 1
            return True

    def give(self):
        with self._lock:
            self.used = max(0, self.used - 1)


class WarnLimit:
    """Called with a key such as (ip, reason): True at most once per `every` seconds for each key.
    Remembers at most `size` keys (the oldest are forgotten first); thread-safe."""

    def __init__(self, every=WARN_EVERY, size=1024, clock=time.monotonic):
        self.every, self.size, self._clock = every, size, clock
        self._last = collections.OrderedDict()   # key -> when it last said True, oldest first
        self._lock = threading.Lock()

    def __call__(self, key):
        with self._lock:
            now = self._clock()
            while self._last:
                oldest, when = next(iter(self._last.items()))
                if now - when < self.every and len(self._last) < self.size:
                    break
                del self._last[oldest]
            if key in self._last:
                return False
            self._last[key] = now
            return True


Addresses = collections.namedtuple("Addresses", "local own hostname")


class AddressCache:
    """This computer's addresses and hostname, looked up at most every `ttl` seconds. `local` is
    remote_access.local_addresses() (what other devices can reach: allowed Hosts, URLs); `own` adds
    every other address of its interfaces (loopback, link-local), to tell this computer apart as a
    peer. Thread-safe; while one thread looks them up again, the others get the previous answer.
    `run` stands in for `ip` in tests (see remote_access.local_addresses)."""

    def __init__(self, ttl=ADDRESS_TTL, run=None, hostname=socket.gethostname, clock=time.monotonic):
        self.ttl, self._run, self._hostname, self._clock = ttl, run, hostname, clock
        self._held = None   # (Addresses, when looked up)
        self._lookup = threading.Lock()

    def _fresh(self, held):
        return held is not None and 0 <= self._clock() - held[1] < self.ttl

    def get(self) -> Addresses:
        held = self._held
        if self._fresh(held):
            return held[0]
        if not self._lookup.acquire(blocking=held is None):
            return held[0]   # someone is looking them up right now
        try:
            held = self._held
            if self._fresh(held):
                return held[0]
            local = tuple(local_addresses(self._run))
            try:
                name = self._hostname() or ""
            except OSError:
                name = ""
            snap = Addresses(local, frozenset(own_addresses(self._run)) | frozenset(local), name)
            self._held = (snap, self._clock())
            return snap
        finally:
            self._lookup.release()


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


class HeadDeadline(io.RawIOBase):
    """The remote listener's socket reader, under its handler's rfile. Armed for a request, the
    request line and headers must arrive within `seconds` of its first byte, however the client
    spreads them over recv calls: each later recv waits only until then, and past it TimeoutError
    is raised (BaseHTTPRequestHandler then closes the connection). The wait for that first byte,
    and every read while disarmed (bodies, which keep their own limits), use the socket's own
    timeout. Never closes the socket."""

    def __init__(self, sock):
        self._sock = sock
        self._seconds = self._deadline = None

    def readable(self):
        return True

    def arm(self, seconds):
        self._seconds, self._deadline = seconds, None

    def disarm(self):
        self._seconds = self._deadline = None

    def readinto(self, buf):
        if self._seconds is None:
            return self._sock.recv_into(buf)
        if self._deadline is None:
            n = self._sock.recv_into(buf)
            if n:
                self._deadline = time.monotonic() + self._seconds
            return n
        left = self._deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("the request's headers took too long")
        idle = self._sock.gettimeout()
        self._sock.settimeout(left if idle is None else min(left, idle))
        try:
            return self._sock.recv_into(buf)
        finally:
            self._sock.settimeout(idle)   # (writes, e.g. an error page, keep the usual timeout)


class Handler(BaseHTTPRequestHandler):
    """The 127.0.0.1 panel. Expects on the server object: .editor (has .log, .token, .router, .hub),
    .allowed_hosts, .static_dir, .stopping (a threading.Event set when the server shuts down),
    .session_limit (a FailureLimit for POST /api/session) and .stream_slots (StreamSlots for
    its event streams; without it, the hub's subscriber count is the cap)."""

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

    def _body(self, limit=MAX_BODY):
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            raise ApiError("expected JSON")
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0:
            raise ApiError("bad Content-Length")
        if length > limit:
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

    def _events(self, still_ok=None, skip=(), full="too many open control panels; close one"):
        """The event stream. still_ok() is asked before every event (default: the panel's token
        is still the current one); events whose type is in `skip` aren't sent."""
        hub, stopping = self.server.editor.hub, self.server.stopping
        if still_ok is None:
            sent = self.headers.get("X-Deck-Token", "")
            still_ok = lambda: self._token_ok(sent)   # noqa: E731
        slots = getattr(self.server, "stream_slots", None)
        if not (slots.take() if slots is not None else hub.subscribers < MAX_SUBSCRIBERS):
            return self._error(503, full)
        try:
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
                    if not still_ok():
                        break   # signed out (token rotated or device revoked): the reconnect gets a 401
                    if event["type"] in skip:
                        continue
                    self._write_event(event)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                hub.unsubscribe(q)
                self.close_connection = True
        finally:
            if slots is not None:
                slots.give()

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


# ----- the remote listener

class RemoteServer(ThreadingHTTPServer):
    """The remote listener (see the module docstring) on `listen` (an address, normalised:
    "0.0.0.0", "::" for dual-stack IPv4 and IPv6, or one address) and `port`, serving
    RemoteHandler. OSError if it can't listen. `editor` has what Handler expects plus .devices,
    .pairing, .peer_limit (remote_access), .addresses (an AddressCache), pair_start(ip, name),
    remote_seen(device, ip) and remote_warn(ip, reason, fmt, *args). peer_ip() and peer_refused()
    are the seams tests replace to stand in for another device."""

    daemon_threads = True

    def __init__(self, listen, port, hosts, editor, static_dir):
        addr = ipaddress.ip_address(listen)
        self.address_family = socket.AF_INET6 if addr.version == 6 else socket.AF_INET
        self._dual = addr.version == 6 and addr.is_unspecified
        self.listen, self.extra_hosts = str(addr), tuple(hosts or ())
        self.editor, self.static_dir = editor, static_dir
        self.stopping = threading.Event()
        self.stream_slots = StreamSlots(MAX_REMOTE_SUBSCRIBERS)
        self.allowed_hosts, self._hosts_from = set(), None
        self._hosts_lock = threading.Lock()
        self._conns, self._per_peer = {}, collections.Counter()   # open connection -> its peer
        self._conns_lock = threading.Lock()
        super().__init__((self.listen, port), RemoteHandler)
        self.port = self.server_address[1]

    def server_bind(self):
        if self.address_family == socket.AF_INET6:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0 if self._dual else 1)
        socketserver.TCPServer.server_bind(self)   # (HTTPServer's would look up this computer's DNS name)
        self.server_name, self.server_port = self.listen, self.server_address[1]

    # -- connections

    def verify_request(self, request, client_address):
        """At most MAX_REMOTE_CONNECTIONS open, MAX_REMOTE_PER_PEER from one address (peer_key: an
        IPv6 /64 is one), so nobody on the network can tie up every handler thread; beyond that a
        connection is closed unanswered."""
        try:
            key = peer_key(client_address[0])
        except (IndexError, TypeError):
            key = "?"
        with self._conns_lock:
            full = (self.stopping.is_set() or len(self._conns) >= MAX_REMOTE_CONNECTIONS
                    or self._per_peer[key] >= MAX_REMOTE_PER_PEER)
            if not full:
                self._conns[request] = key
                self._per_peer[key] += 1
        if full and not self.stopping.is_set():
            self.editor.remote_warn(key, "connections", "remote access: refused a connection from %s: "
                                    "too many open connections", key)
        return not full

    def shutdown_request(self, request):
        with self._conns_lock:
            key = self._conns.pop(request, None)
            if key is not None:
                self._per_peer[key] -= 1
                if self._per_peer[key] <= 0:
                    del self._per_peer[key]
        super().shutdown_request(request)

    def handle_error(self, request, client_address):
        """A connection that broke is the peer's business: no traceback on stderr for it."""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            self.editor.log.debug("remote access: connection from %s ended: %s", client_address[0], exc)
            return
        super().handle_error(request, client_address)

    def close_connections(self):
        """Shut every open connection: once stopped, no keep-alive request or event stream goes on."""
        with self._conns_lock:
            conns = list(self._conns)
        for sock in conns:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    # -- who may ask

    def current_hosts(self):
        """allowed_hosts, rebuilt whenever this computer's addresses have been looked up again."""
        snap = self.editor.addresses.get()
        with self._hosts_lock:
            if snap is not self._hosts_from:
                self.allowed_hosts = allowed_hosts(self.listen, self.port, snap.local, snap.hostname,
                                                   self.extra_hosts)
                self._hosts_from = snap
            return self.allowed_hosts

    def peer_ip(self, client_address):
        """The peer's address, normalised; ValueError if it isn't one."""
        return normalize_ip(client_address[0])

    def peer_refused(self, ip, here=None):
        """Why the peer at `ip` is refused: "loopback", or "self" (an address of this computer:
        the one the connection arrived on, `here`, the listen address or any other); else None."""
        if is_loopback(ip):
            return "loopback"
        if ip in (here, self.listen) or ip in self.editor.addresses.get().own:
            return "self"
        return None


class RemoteHandler(Handler):
    """Requests on the remote listener (see the module docstring). Expects a RemoteServer."""

    _peer = None      # this request's peer address, normalised (None: not an address)
    _checked = None   # (headers, device or None): _remote_ok's X-Deck-Token check, for those headers

    def setup(self):
        super().setup()
        self.rfile.close()   # makefile's reader (closing it leaves the socket open), replaced by
        self._head = HeadDeadline(self.connection)   # one that holds requests to HEAD_SECONDS
        self.rfile = io.BufferedReader(self._head)

    def handle_one_request(self):
        self._head.arm(HEAD_SECONDS)
        try:
            super().handle_one_request()
        finally:
            self._head.disarm()

    def parse_request(self):
        try:
            return super().parse_request()
        finally:
            self._head.disarm()   # the headers are in: the body and the answer have their own limits

    def _warn(self, reason, fmt, *args):
        # at most once a minute per peer_key (an IPv6 /64 is one peer) and reason
        self.server.editor.remote_warn(peer_key(self._peer) if self._peer else "?", reason, fmt, *args)

    def _here(self):
        try:
            return normalize_ip(self.connection.getsockname()[0])
        except (OSError, ValueError, IndexError, TypeError):
            return None

    def _remote_ok(self):
        """The checks every request passes first (see the module docstring); False once the
        refusal is sent. Keeps its X-Deck-Token check for _device()."""
        srv = self.server
        editor = srv.editor
        self._checked = None
        if srv.stopping.is_set():
            self.close_connection = True
            self._error(503, "Remote access is off")
            return False
        try:
            self._peer = srv.peer_ip(self.client_address)
        except (ValueError, IndexError, TypeError):
            self._peer = None
        why = srv.peer_refused(self._peer, self._here()) if self._peer else "junk"
        if why:
            self._warn("self", "remote access: refused a request from this computer itself (%s); "
                       "programs here use the panel on 127.0.0.1", self._peer or "not an address")
            self._error(403, "Requests from this computer itself aren't accepted here.")
            return False
        hosts = srv.current_hosts()
        given = self.headers.get_all("Host") or []
        if len(given) != 1 or not host_ok(given[0], hosts):
            self._warn("host", "remote access: refused %s: Host %r isn't an address or name of this computer "
                       "(add it to editor.remote.hosts if it should be)", self._peer, _shown(given[0] if given else ""))
            self._error(421, "The panel doesn't answer on this address; use one shown under Settings → Access "
                             "on the streaming computer")
            return False
        origins = self.headers.get_all("Origin") or []
        if origins and not (len(origins) == 1 and origins[0].startswith("http://")
                            and host_ok(origins[0][len("http://"):], hosts)):
            self._warn("origin", "remote access: refused %s: a request from another website (Origin %r)",
                       self._peer, _shown(origins[0]))
            self._error(403, "cross-origin request refused")
            return False
        # An approved device's token is checked before the failure limit, so that others failing
        # (from one address or from very many) can never lock it out; everyone else is limited.
        sent = self.headers.get("X-Deck-Token", "")
        device = editor.devices.check(sent, self._peer) if sent else None
        self._checked = (self.headers, device)
        if device is None and editor.peer_limit.blocked(self._peer):
            self._warn("limited", "remote access: refusing %s for a minute: too many failed attempts", self._peer)
            self._error(429, "Too many failed attempts; wait a minute and try again")
            return False
        return True

    def _device(self):
        """The approved device whose token this request carries (as _remote_ok found it); else
        None, once the 401 is sent."""
        editor = self.server.editor
        checked = self._checked
        if checked is not None and checked[0] is self.headers:   # this request's own check, never a previous one's
            device = checked[1]
        else:
            device = editor.devices.check(self.headers.get("X-Deck-Token", ""), self._peer)
        if device is None:
            editor.peer_limit.fail(self._peer)
            self._warn("token", "remote access: refused %s: missing or wrong device token", self._peer)
            self._error(401, "This device isn't approved, or its approval ran out; ask for access again")
            return None
        editor.remote_seen(device, self._peer)
        return device

    def _pair(self, path):
        """POST /api/pair/start {"name"} and /api/pair/poll {"request", "secret"}: no token, but
        only a page of ours may send them (an Origin is required; _remote_ok checked its value)."""
        editor, ip = self.server.editor, self._peer
        if self.headers.get("Origin") is None:
            editor.peer_limit.fail(ip)
            self._warn("origin", "remote access: refused a pairing request from %s without an Origin", ip)
            return self._error(403, "Ask for access from the panel's page")
        try:
            body = self._body(PAIR_BODY)
            if path == "/api/pair/start":
                out = editor.pair_start(ip, body.get("name"))
            else:
                out = editor.pairing.poll(body.get("request"), body.get("secret"))
        except Refused as e:
            editor.peer_limit.fail(ip)
            return self._error(e.status, e.message)
        except ApiError as e:
            editor.peer_limit.fail(ip)
            return self._error(e.status, str(e))
        except (ValueError, json.JSONDecodeError) as e:
            editor.peer_limit.fail(ip)
            return self._error(400, f"bad JSON: {e}")
        self._send(200, out)

    def do_GET(self):
        if not self._remote_ok():
            return
        url = urlparse(self.path)
        if not url.path.startswith("/api/"):
            return self._static(url.path)
        device = self._device()
        if device is None:
            return
        with request_context.remote(device):
            if url.path == "/api/events":
                return self._remote_events()
            fn = self.server.editor.router.find_get(url.path)
            if not fn:
                return self._error(404, "not found")
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            self._call(fn, query)

    def _remote_events(self):
        devices, stopping, ip = self.server.editor.devices, self.server.stopping, self._peer
        sent = self.headers.get("X-Deck-Token", "")
        # before every event: a revoke, an expiry or remote access turned off ends the stream
        return self._events(lambda: not stopping.is_set() and devices.check(sent, ip) is not None,
                            skip=LOCAL_EVENTS, full="too many panels are open on other devices; close one")

    def do_POST(self):
        self._body_read = False
        path = urlparse(self.path).path
        upload = self.server.editor.router.uploads.get(path)
        self._unread = True if upload else None   # refusals of an upload go through _reject
        if not self._remote_ok():
            return
        if path in PAIR_ROUTES:
            self._unread = None
            return self._pair(path)
        if path in LOCAL_ONLY:
            return self._error(403, LOCAL_ONLY_MESSAGE)
        device = self._device()
        if device is None:
            return
        with request_context.remote(device):
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

    def _other_method(self):
        self.close_connection = True
        if self._remote_ok():
            self._error(405, "method not allowed")

    do_HEAD = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _other_method
