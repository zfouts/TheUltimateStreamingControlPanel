"""The event hub and the web panel's HTTP hardening: SSE stream, static files, body limits.
Runs the real daemon (minus the hardware) in a sandbox, like tests/test_editor.py."""
import json
import logging
import os
import socket
import time
import unittest
import urllib.error
import urllib.request

from tests.helpers import EXAMPLE, ROOT, Deck, free_port, tmpdir

import yaml  # noqa: E402

from core import editor_auth  # noqa: E402
from core.bot import DeckBot  # noqa: E402
from core.events import MAX_QUEUE, Hub  # noqa: E402
from core.webapi.server import MAX_SUBSCRIBERS  # noqa: E402

PORT = free_port()


def setUpModule():
    global BOT, BASE, TOKEN
    config = os.path.join(tmpdir(), "config.yaml")
    with open(EXAMPLE) as f:
        cfg = yaml.safe_load(f)
    cfg["editor"] = {"port": PORT}
    with open(config, "w") as f:
        f.write(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
    BOT = DeckBot(root=ROOT, config_path=config)
    BOT.deck = Deck("Stream Deck XL")
    BOT.load_config(initial=True)
    BOT.load_extensions("modules")
    BASE, TOKEN = f"http://127.0.0.1:{PORT}", editor_auth.token()


def tearDownModule():
    for inst in list(BOT.modules.values()):
        try:
            inst.on_unload()
        except Exception:
            pass


def call(path, body=None, headers=None, token=True, method=None):
    h = {"X-Deck-Token": TOKEN} if token else {}
    data = None
    if body is not None:
        data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
    h.update(headers or {})
    req = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        with e:
            return e.code, e.headers, e.read()


def raw_request(path, extra="", method="GET", body=b"", token=True):
    """One HTTP request over a bare socket; returns (status, header text, body)."""
    head = f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\nConnection: close\r\n"
    if token:
        head += f"X-Deck-Token: {TOKEN}\r\n"
    s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    try:
        s.sendall((head + extra + "\r\n").encode() + body)
        buf = b""
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    header, _, payload = buf.partition(b"\r\n\r\n")
    text = header.decode("latin-1")
    return int(text.split()[1]), text, payload


class Stream:
    """An open /api/events connection read with a deadline."""

    def __init__(self, token=True):
        self.sock = socket.create_connection(("127.0.0.1", PORT), timeout=1)
        req = f"GET /api/events HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\n"
        if token:
            req += f"X-Deck-Token: {TOKEN}\r\n"
        self.sock.sendall((req + "\r\n").encode())
        self.buf = b""
        self.head = None

    def close(self):
        self.sock.close()

    def _fill(self, deadline):
        while time.monotonic() < deadline:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                return False
            self.buf += chunk
            return True
        return False

    def headers(self, timeout=3):
        deadline = time.monotonic() + timeout
        while b"\r\n\r\n" not in self.buf:
            if not self._fill(deadline):
                raise AssertionError("no response headers: %r" % self.buf)
        raw, _, self.buf = self.buf.partition(b"\r\n\r\n")
        self.head = raw.decode("latin-1")
        return self.head

    def block(self, timeout=3):
        """The next complete SSE block as (event, data) or None on timeout."""
        if self.head is None:
            self.headers(timeout)
        deadline = time.monotonic() + timeout
        while b"\n\n" not in self.buf:
            if not self._fill(deadline):
                return None
        raw, _, self.buf = self.buf.partition(b"\n\n")
        fields = dict(line.split(": ", 1) for line in raw.decode().split("\n") if ": " in line)
        return fields["event"], json.loads(fields["data"])

    def wait_for(self, event, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            blk = self.block(max(0.05, deadline - time.monotonic()))
            if blk is None:
                break
            if blk[0] == event:
                yield blk[1]
                return
        raise AssertionError(f"no {event!r} event within {timeout}s")


def drain_subscribers():
    """Handlers of closed sockets only notice on their next write; nudge them with pings."""
    deadline = time.monotonic() + 3
    while BOT.hub.subscribers and time.monotonic() < deadline:
        BOT.hub.publish("ping")
        time.sleep(0.02)
    assert BOT.hub.subscribers == 0, "event subscribers leaked"


class HubTests(unittest.TestCase):
    def test_publish_reaches_all_subscribers(self):
        hub = Hub()
        a, b = hub.subscribe(), hub.subscribe()
        hub.publish("page", {"page": "x"})
        for q in (a, b):
            self.assertEqual(q.get_nowait()["data"], {"page": "x"})

    def test_event_shape(self):
        hub = Hub()
        q = hub.subscribe()
        before = time.time()
        hub.publish("key", {"page": "p", "key": 1})
        ev = q.get_nowait()
        self.assertEqual(set(ev), {"type", "ts", "data"})
        self.assertEqual(ev["type"], "key")
        self.assertGreaterEqual(ev["ts"], before)
        hub.publish("ping")
        self.assertEqual(q.get_nowait()["data"], {})

    def test_unsubscribe_stops_delivery(self):
        hub = Hub()
        a, b = hub.subscribe(), hub.subscribe()
        hub.unsubscribe(a)
        hub.unsubscribe(a)   # idempotent
        hub.publish("page", {})
        self.assertTrue(a.empty())
        self.assertFalse(b.empty())
        self.assertEqual(hub.subscribers, 1)

    def test_history_keeps_last_n(self):
        hub = Hub()
        for i in range(400):
            hub.publish("chat", {"i": i})
        hub.publish("page", {})
        full = hub.history("chat")
        self.assertEqual([e["data"]["i"] for e in full], list(range(200, 400)))   # default n=200
        self.assertEqual(len(hub.history("chat", 0)), 300)                        # ring size
        self.assertEqual([e["data"]["i"] for e in hub.history("chat", 2)], [398, 399])
        self.assertEqual(hub.history("page"), [])   # no ring for other types

    def test_history_log_ring(self):
        hub = Hub()
        hub.publish("log", {"msg": "a"})
        self.assertEqual(hub.history("log")[0]["data"]["msg"], "a")

    def test_history_data_has_ts(self):
        hub = Hub()
        hub.publish("chat", {"text": "hi"})
        hub.publish("log", {"ts": 5, "msg": "x"})
        item = hub.history("chat")[0]
        self.assertEqual(item["data"]["ts"], item["ts"])
        self.assertEqual(hub.history("log")[0]["data"]["ts"], 5)

    def test_reconnect_shuts_down_socket(self):
        from modules.chat import Chat
        calls = []

        class FakeSock:
            def shutdown(self, how): calls.append(("shutdown", how))
            def close(self): calls.append("close")

        chat = Chat.__new__(Chat)
        chat.sock = FakeSock()
        chat._reconnect()
        self.assertEqual(calls, [("shutdown", socket.SHUT_RDWR), "close"])

    def test_full_queue_drops_subscriber(self):
        hub = Hub()
        slow, healthy = hub.subscribe(), hub.subscribe()
        for i in range(MAX_QUEUE):
            hub.publish("page", {"i": i})
            healthy.get_nowait()
        self.assertEqual(slow.qsize(), MAX_QUEUE)
        self.assertEqual(hub.subscribers, 2)
        hub.publish("page", {"i": "overflow"})
        self.assertEqual(hub.subscribers, 1)
        self.assertEqual(healthy.get_nowait()["data"], {"i": "overflow"})
        hub.publish("page", {})
        self.assertEqual(slow.qsize(), MAX_QUEUE)   # nothing more delivered


class ServerTests(unittest.TestCase):
    # ----- access control

    def test_events_requires_token(self):
        self.assertEqual(call("/api/events", token=False)[0], 401)

    def test_events_bad_host(self):
        self.assertEqual(call("/api/events", headers={"Host": f"evil.example:{PORT}"})[0], 421)

    def test_events_bad_origin(self):
        self.assertEqual(call("/api/events", headers={"Origin": "http://evil.example"})[0], 403)

    # ----- the stream

    def test_stream_headers_and_hello(self):
        s = Stream()
        try:
            head = s.headers()
            self.assertIn(" 200 ", head.split("\r\n")[0])
            low = head.lower()
            self.assertIn("content-type: text/event-stream", low)
            self.assertIn("content-security-policy:", low)
            self.assertIn("x-frame-options: deny", low)
            self.assertNotIn("access-control-allow-origin", low)
            event, data = s.block()
            self.assertEqual(event, "hello")
            self.assertIn("subscribers", data)
        finally:
            s.close()

    def test_page_event_arrives(self):
        s = Stream()
        try:
            self.assertEqual(s.block()[0], "hello")
            BOT.hub.publish("page", {"page": "twitch"})
            self.assertEqual(next(s.wait_for("page")), {"page": "twitch"})
        finally:
            s.close()

    def test_log_event_is_redacted(self):
        secret = "abcdefghijklmnopqrstuvwxyz123456"  # privacy-check: allow (fake token for the redaction test)
        lg = logging.getLogger("deck.test")
        old = lg.level
        lg.setLevel(logging.INFO)
        s = Stream()
        try:
            self.assertEqual(s.block()[0], "hello")
            lg.info("token access_token=%s", secret)
            deadline = time.monotonic() + 3
            found = None
            while time.monotonic() < deadline and not found:
                blk = s.block(max(0.05, deadline - time.monotonic()))
                if blk is None:
                    break
                if blk[0] == "log" and blk[1].get("logger") == "deck.test":
                    found = blk[1]
            self.assertIsNotNone(found, "no log event arrived")
            self.assertIn("[redacted]", found["msg"])
            self.assertNotIn(secret, json.dumps(found))
        finally:
            lg.setLevel(old)
            s.close()

    def test_subscriber_limit(self):
        drain_subscribers()
        streams = []
        try:
            for _ in range(MAX_SUBSCRIBERS):
                s = Stream()
                streams.append(s)
                self.assertEqual(s.block()[0], "hello")
            self.assertEqual(BOT.hub.subscribers, MAX_SUBSCRIBERS)
            self.assertEqual(call("/api/events")[0], 503)
        finally:
            for s in streams:
                s.close()
        drain_subscribers()

    def test_overflowed_stream_is_closed(self):
        s = Stream()
        try:
            self.assertEqual(s.block()[0], "hello")
            blob = "x" * 20000
            for i in range(MAX_QUEUE * 2):      # we don't read, so the server blocks and its queue fills
                BOT.hub.publish("page", {"i": i, "pad": blob})
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    chunk = s.sock.recv(1 << 20)
                except socket.timeout:
                    continue
                if not chunk:
                    break
            else:
                self.fail("stream still open after its queue overflowed")
        finally:
            s.close()

    def test_static_nul_byte_is_404(self):
        self.assertEqual(call("/%00x.js", token=False)[0], 404)

    # ----- static files

    def test_static_files(self):
        for path, ctype in (("/js/main.js", "text/javascript"), ("/css/live.css", "text/css"),
                            ("/style.css", "text/css"), ("/", "text/html")):
            status, headers, body = call(path, token=False)
            self.assertEqual(status, 200, path)
            self.assertTrue(headers["Content-Type"].startswith(ctype), path)
            self.assertTrue(body or path.endswith(".css"), path)
            self.assertEqual(headers["X-Frame-Options"], "DENY")

    def test_static_refuses_outside_and_disallowed(self):
        for path in ("/js/../core/paths.py", "/..%2fmain.py", "/README.md", "/vendor/",
                     "/%2e%2e/main.py", "/js/%2e%2e/%2e%2e/core/paths.py"):
            status, _, _ = raw_request(path, token=False)
            self.assertEqual(status, 404, path)
        self.assertEqual(call("/..%2fmain.py", token=False)[0], 404)
        self.assertEqual(call("/README.md", token=False)[0], 404)

    # ----- POST hardening

    def test_negative_content_length(self):
        status, _, _ = raw_request("/api/client-error", "Content-Type: application/json\r\nContent-Length: -5\r\n",
                                   method="POST")
        self.assertEqual(status, 400)

    def test_oversized_body(self):
        status, _, _ = raw_request("/api/client-error",
                                   f"Content-Type: application/json\r\nContent-Length: {4 * 1024 * 1024 + 1}\r\n",
                                   method="POST")
        self.assertEqual(status, 413)

    def test_array_body_refused(self):
        status, _, raw = call("/api/client-error", [1, 2])
        self.assertEqual(status, 400, raw)

    def test_malformed_json_refused(self):
        status, _, _ = raw_request("/api/client-error", "Content-Type: application/json\r\nContent-Length: 5\r\n",
                                   method="POST", body=b"{nope")
        self.assertEqual(status, 400)

    def test_non_json_content_type_refused(self):
        status, _, _ = call("/api/client-error", headers={"Content-Type": "text/plain"}, method="POST")
        self.assertEqual(status, 400)
        req = urllib.request.Request(BASE + "/api/client-error", data=b"{}",
                                     headers={"X-Deck-Token": TOKEN, "Content-Type": "text/plain"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 400)
        cm.exception.close()

    def test_client_error_ok(self):
        status, _, raw = call("/api/client-error", {"message": "boom", "stack": "at x (y.js:1)"})
        self.assertEqual((status, json.loads(raw)), (200, {"ok": True}))

    def test_unknown_routes(self):
        self.assertEqual(call("/api/nope")[0], 404)
        self.assertEqual(call("/api/nope", {})[0], 404)


if __name__ == "__main__":
    unittest.main()
