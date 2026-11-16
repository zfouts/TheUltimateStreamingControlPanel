"""The remote listener (editor.remote): other devices pair with a 6-digit code approved on this
computer and then use the panel with tokens of their own, while the 127.0.0.1 panel works exactly
as before. Runs the real daemon (minus the hardware) with both listeners on 127.0.0.1:
RemoteServer.peer_ip is replaced so requests seem to come from another device (PEER), and this
computer's addresses come from a fake `ip -j addr show`."""
import contextlib
import copy
import datetime
import itertools
import json
import logging
import os
import queue
import re
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from tests.helpers import EXAMPLE, ROOT, Deck, free_port, tmpdir

import yaml  # noqa: E402

from core import backups, configio, editor_auth, request_context, setup_backup  # noqa: E402
from core import remote_access as ra  # noqa: E402
from core.bot import DeckBot  # noqa: E402
from core.webapi import server as srv  # noqa: E402
from core.webapi.server import (LOCAL_ONLY, LOCAL_ONLY_MESSAGE, MAX_REMOTE_SUBSCRIBERS, MAX_SUBSCRIBERS,  # noqa: E402
                                AddressCache, FailureLimit, HeadDeadline, RemoteServer, StreamSlots, WarnLimit)

PORT = free_port()
RPORT = next(p for p in iter(free_port, None) if p != PORT)
DEVICE, OTHER = "198.51.100.23", "198.51.100.24"     # other devices (TEST-NET-2)
LAN, LINK_LOCAL = "192.0.2.50", "fe80::50"           # this computer's own addresses, per IP_JSON
IP_JSON = json.dumps([
    {"ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1"}, {"family": "inet6", "local": "::1"}]},
    {"ifname": "eth0", "addr_info": [{"family": "inet", "local": LAN}, {"family": "inet6", "local": LINK_LOCAL}]}])
PEER = {"ip": DEVICE}   # where remote requests seem to come from; None: their real address (127.0.0.1)
NOTES = []              # desktop notifications sent
REAL_PEER_IP = RemoteServer.peer_ip
REMOTE_SETTINGS = request_context.REMOTE_SETTINGS


def fake_peer_ip(self, client_address):
    return REAL_PEER_IP(self, (PEER["ip"], 0) if PEER["ip"] else client_address)


class Captured(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append((record.levelno, record.getMessage()))


@contextlib.contextmanager
def captured(name="deck", level=logging.DEBUG):
    """Every log record of `name` and below while the body runs: a list of (level, message)."""
    lg, handler = logging.getLogger(name), Captured()
    old = lg.level, lg.propagate
    lg.addHandler(handler)
    lg.setLevel(level)
    lg.propagate = False   # (the root's handler would print them)
    try:
        yield handler.records
    finally:
        lg.removeHandler(handler)
        lg.setLevel(old[0])
        lg.propagate = old[1]


def remote_cfg(**over):
    return {"enabled": True, "listen": "127.0.0.1", "port": RPORT, **over}


def setUpModule():
    global BOT, ED, CONFIG, PATCHES
    CONFIG = os.path.join(tmpdir(), "config.yaml")
    with open(EXAMPLE) as f:
        cfg = yaml.safe_load(f)
    cfg["editor"] = {"port": PORT, "remote": remote_cfg()}
    with open(CONFIG, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)
    PATCHES = [mock.patch.object(RemoteServer, "peer_ip", fake_peer_ip)]
    for p in PATCHES:
        p.start()
    BOT = DeckBot(root=ROOT, config_path=CONFIG)
    BOT.deck = Deck("Stream Deck XL")
    BOT.load_config(initial=True)
    BOT.load_extensions("modules")
    ED = next(m for m in BOT.modules.values() if type(m).__name__ == "Editor")
    ED.addresses = AddressCache(run=lambda argv: IP_JSON, hostname=lambda: "streampc")
    PATCHES.append(mock.patch.dict(ED.pair_start.__globals__, {"notify": NOTES.append}))
    PATCHES[-1].start()
    assert ED.remote_server is not None, ED.remote_error


def tearDownModule():
    for inst in list(BOT.modules.values()):
        try:
            inst.on_unload()
        except Exception:
            pass
    for p in reversed(PATCHES):
        p.stop()


# ----- HTTP helpers

def request(port, path, body=None, token=None, headers=None, method=None, host="127.0.0.1"):
    h = {"X-Deck-Token": token} if token else {}
    data = None
    if body is not None:
        data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
    h.update(headers or {})
    req = urllib.request.Request(f"http://{host}:{port}{path}", data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, parsed(r.read())
    except urllib.error.HTTPError as e:
        with e:
            return e.code, parsed(e.read())


def parsed(raw):
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        return raw


ORIGIN = {"Origin": f"http://127.0.0.1:{RPORT}"}


def remote(path, body=None, token=None, headers=None, origin=True, **kw):
    """A request to the remote listener; a POST carries the page's Origin unless origin=False."""
    h = dict(ORIGIN) if origin and body is not None else {}
    h.update(headers or {})
    return request(RPORT, path, body, token, h, **kw)


def local(path, body=None, token=True, headers=None):
    return request(PORT, path, body, ED.token if token is True else token, headers)


def pair(name="Phone", ip=DEVICE):
    """Pair a device at `ip` the way the pages do; returns (its token, its device id)."""
    PEER["ip"] = ip
    status, started = remote("/api/pair/start", {"name": name})
    assert status == 200, (status, started)
    status, out = local("/api/remote/decide", {"request": started["request"], "allow": True})
    assert (status, out) == (200, {"ok": True}), (status, out)
    status, polled = remote("/api/pair/poll", {"request": started["request"], "secret": started["secret"]})
    assert polled.get("state") == "approved", (status, polled)
    device = next(d for d in ED.devices.list() if d["name"] == ra.clean_name(name))
    return polled["token"], device["id"]


def events(q, kind):
    """The data of every queued hub event of type `kind`."""
    out = []
    while True:
        try:
            ev = q.get_nowait()
        except queue.Empty:
            return out
        if ev and ev["type"] == kind:
            out.append(ev["data"])


def reconfigure(**remote_settings):
    """Apply editor.remote = remote_cfg(**remote_settings), as a config reload would."""
    ED.on_config({**ED.config, "port": PORT, "remote": remote_cfg(**remote_settings)})


def read_response(sock):
    """One HTTP response (with a Content-Length) from a raw socket: (status, body)."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            raise AssertionError("connection closed before a response: %r" % buf)
        buf += chunk
    head, _, body = buf.partition(b"\r\n\r\n")
    length = int(re.search(rb"(?im)^content-length:\s*(\d+)", head).group(1))
    while len(body) < length:
        chunk = sock.recv(65536)
        if not chunk:
            raise AssertionError("connection closed in the middle of a response")
        body += chunk
    return int(head.split()[1]), body


def wait_for(cond, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


class Stream:
    """An open /api/events connection read with a deadline (as in tests/test_events.py)."""

    def __init__(self, port, token, host=None):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=1)
        self.sock.sendall((f"GET /api/events HTTP/1.1\r\nHost: {host or f'127.0.0.1:{port}'}\r\n"
                           f"X-Deck-Token: {token}\r\n\r\n").encode())
        self.buf, self.head = b"", None

    def close(self):
        self.sock.close()

    def _fill(self, deadline):
        while time.monotonic() < deadline:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                return False
            if not chunk:
                return False
            self.buf += chunk
            return True
        return False

    def status(self, timeout=3):
        deadline = time.monotonic() + timeout
        while b"\r\n\r\n" not in self.buf:
            if not self._fill(deadline):
                raise AssertionError("no response headers: %r" % self.buf)
        raw, _, self.buf = self.buf.partition(b"\r\n\r\n")
        self.head = raw.decode("latin-1")
        return int(self.head.split()[1])

    def block(self, timeout=3):
        """The next SSE block as (event, data), or None on timeout or end of stream."""
        if self.head is None:
            self.status(timeout)
        deadline = time.monotonic() + timeout
        while b"\n\n" not in self.buf:
            if not self._fill(deadline):
                return None
        raw, _, self.buf = self.buf.partition(b"\n\n")
        fields = dict(line.split(": ", 1) for line in raw.decode().split("\n") if ": " in line)
        return fields["event"], json.loads(fields["data"])

    def until(self, kind, timeout=3):
        """Every event type seen, up to and including the first `kind`."""
        seen, deadline = [], time.monotonic() + timeout
        while time.monotonic() < deadline:
            blk = self.block(max(0.05, deadline - time.monotonic()))
            if blk is None:
                break
            seen.append(blk[0])
            if blk[0] == kind:
                return seen
        raise AssertionError(f"no {kind!r} event within {timeout}s (saw {seen})")

    def ended(self, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                return True
            if not chunk:
                return True
        return False


def drain_streams():
    """Handlers of closed sockets only notice on their next write; nudge them with pings."""
    def busy():
        return any(s is not None and s.stream_slots.used for s in (ED.server, ED.remote_server))
    deadline = time.monotonic() + 3
    while busy() and time.monotonic() < deadline:
        BOT.hub.publish("ping")
        time.sleep(0.02)
    assert not busy(), "event streams leaked"


class Base(unittest.TestCase):
    def setUp(self):
        PEER["ip"] = DEVICE
        if ED.remote_running_as != ED._remote_settings()._replace(days=None) or ED.remote_server is None:
            reconfigure()
        self.assertIsNotNone(ED.remote_server, ED.remote_error)
        ED.peer_limit = ra.PeerLimit()
        ED.warn_limit = WarnLimit()
        ED.server.session_limit = FailureLimit()
        with ED.pair_lock:
            ED.pairing = ra.Pairing()
            ED.announced.clear()
        ED.devices.revoke_all()
        ED.devices_seen.clear()
        NOTES.clear()

    def tearDown(self):
        PEER["ip"] = DEVICE
        if ED.config.get("remote") != remote_cfg():
            reconfigure()


# ----- the 127.0.0.1 panel is unchanged

class LocalPanel(Base):
    def test_launch_code_sign_in(self):
        code = editor_auth.url(BOT.cfg).split("#code=", 1)[1]
        status, body = local("/api/session", {"code": code}, token=None)
        self.assertEqual((status, body), (200, {"token": ED.token}))
        self.assertEqual(local("/api/status")[0], 200)
        self.assertEqual(local("/api/session", {"code": code}, token=None)[0], 403)   # single use

    def test_host_and_origin_checks(self):
        self.assertEqual(local("/api/status", headers={"Host": f"evil.example:{PORT}"})[0], 421)
        self.assertEqual(local("/api/status", headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(local("/", token=None, headers={"Host": f"127.0.0.1:{RPORT}"})[0], 421)   # not our port
        code = editor_auth.url(BOT.cfg).split("#code=", 1)[1]
        self.assertEqual(local("/api/session", {"code": code}, token=None,
                               headers={"Host": f"evil.example:{PORT}"})[0], 421)
        self.assertEqual(local("/api/session", {"code": code}, token=None)[0], 200)

    def test_device_tokens_refused(self):
        token, _ = pair()
        self.assertEqual(local("/api/status", token=token)[0], 401)
        self.assertEqual(remote("/api/status", token=token)[0], 200)

    def test_pairing_routes_only_on_the_remote_listener(self):
        self.assertEqual(local("/api/pair/start", {"name": "x"})[0], 404)
        self.assertEqual(local("/api/pair/poll", {"request": "x", "secret": "y"})[0], 404)
        self.assertEqual(local("/api/pair/start", {"name": "x"}, token=None)[0], 401)

    def test_two_listeners(self):
        self.assertEqual(ED.server.server_address[:2], ("127.0.0.1", PORT))
        self.assertEqual(ED.remote_server.server_address[:2], ("127.0.0.1", RPORT))
        self.assertIsNot(ED.server, ED.remote_server)
        self.assertTrue(editor_auth.is_bound(PORT))
        self.assertFalse(editor_auth.is_bound(RPORT))   # the remote listener is never a panel link's target

    def test_local_status_shows_remote_access(self):
        status, st = local("/api/status")
        self.assertEqual(status, 200)
        r = st["remote"]
        self.assertEqual(set(r), {"enabled", "listen", "port", "urls", "addresses", "error", "devices", "pending",
                                  "this_device"})
        self.assertEqual((r["enabled"], r["listen"], r["port"], r["error"], r["this_device"]),
                         (True, "127.0.0.1", RPORT, None, None))
        self.assertEqual(r["urls"], [f"http://streampc.local:{RPORT}", f"http://127.0.0.1:{RPORT}"])
        self.assertEqual(r["addresses"], [LAN])


# ----- pairing

class PairingFlow(Base):
    def test_start_decide_poll_status(self):
        q = BOT.hub.subscribe()
        self.addCleanup(BOT.hub.unsubscribe, q)
        with captured() as logs:
            status, started = remote("/api/pair/start", {"name": "Phone‮\n"})
            self.assertEqual(status, 200, started)
            self.assertEqual(set(started), {"request", "code", "secret", "expires"})
            code = started["code"]
            self.assertRegex(code, r"^\d{6}$")
            self.assertEqual(events(q, "pair_request"), [{"id": started["request"], "name": "Phone", "ip": DEVICE,
                                                          "code": code, "expires": started["expires"]}])
            self.assertEqual(NOTES, [f"Phone wants to use the control panel. Code {code[:3]} {code[3:]}. "
                                     "Approve it in the panel."])
            poll = {"request": started["request"], "secret": started["secret"]}
            self.assertEqual(remote("/api/pair/poll", poll), (200, {"state": "pending"}))
            self.assertEqual([p["id"] for p in local("/api/status")[1]["remote"]["pending"]], [started["request"]])

            self.assertEqual(local("/api/remote/decide", {"request": started["request"], "allow": True}),
                             (200, {"ok": True}))
            self.assertEqual(events(q, "pair_done"), [{"id": started["request"], "allowed": True}])
            status, got = remote("/api/pair/poll", poll)
            self.assertEqual((status, got["state"]), (200, "approved"))
            token = got["token"]
            self.assertEqual(remote("/api/pair/poll", poll), (200, got))   # a lost answer can be asked for again

            PEER["ip"] = OTHER
            self.assertEqual(remote("/api/pair/start", {"name": "Laptop"})[0], 200)   # someone else waiting
            PEER["ip"] = DEVICE
            status, st = remote("/api/status", token=token)
            self.assertEqual(status, 200)
            mine = next(d for d in ED.devices.list() if d["name"] == "Phone")
            self.assertEqual(st["remote"]["this_device"], mine["id"])
            self.assertEqual(st["remote"]["pending"], [])
            self.assertEqual([d["id"] for d in st["remote"]["devices"]], [mine["id"]])
            here = local("/api/status")[1]["remote"]
            self.assertEqual([p["name"] for p in here["pending"]], ["Laptop"])
            self.assertIsNone(here["this_device"])
        text = "\n".join(m for _, m in logs)
        self.assertIn("'Phone' (198.51.100.23) asks to use the control panel", text)
        self.assertIn("approved 'Phone' (198.51.100.23)", text)
        self.assertIn("'Phone' is using the control panel", text)
        self.assertNotRegex(text, rf"\b{code}\b|\b{code[:3]} {code[3:]}\b")
        for secret in (started["secret"], token):
            self.assertNotIn(secret, text)

    def test_first_use_logged_once_per_run(self):
        token, _ = pair()

        def first_uses():
            with captured() as logs:
                for _ in range(3):
                    self.assertEqual(remote("/api/status", token=token)[0], 200)
            return [lv for lv, m in logs if "is using the control panel" in m]
        self.assertEqual(first_uses(), [logging.INFO])
        self.assertEqual(first_uses(), [])
        ED.devices_seen.clear()   # a new daemon run
        self.assertEqual(first_uses(), [logging.INFO])

    def test_deny(self):
        q = BOT.hub.subscribe()
        try:
            _, started = remote("/api/pair/start", {"name": "Tablet"})
            with self.assertLogs("deck.Editor", logging.INFO) as cm:
                self.assertEqual(local("/api/remote/decide", {"request": started["request"], "allow": False}),
                                 (200, {"ok": True}))
            self.assertTrue(any("denied 'Tablet'" in m for m in cm.output))
            self.assertEqual(events(q, "pair_done"), [{"id": started["request"], "allowed": False}])
        finally:
            BOT.hub.unsubscribe(q)
        self.assertEqual(remote("/api/pair/poll", {"request": started["request"], "secret": started["secret"]}),
                         (200, {"state": "denied"}))
        self.assertEqual(ED.devices.list(), [])

    def test_decide_validates(self):
        _, started = remote("/api/pair/start", {"name": "Tablet"})
        self.assertEqual(local("/api/remote/decide", {"request": started["request"], "allow": "yes"})[0], 400)
        self.assertEqual(local("/api/remote/decide", {"request": "nope", "allow": True})[0], 404)
        self.assertEqual(local("/api/remote/decide", {"allow": True})[0], 400)
        self.assertEqual(local("/api/remote/decide", {"request": started["request"], "allow": True}, token=None)[0],
                         401)

    def test_expiry_publishes_pair_done(self):
        clock = [time.time()]
        with ED.pair_lock:
            ED.pairing = ra.Pairing(clock=lambda: clock[0])
        q = BOT.hub.subscribe()
        try:
            _, started = remote("/api/pair/start", {"name": "Phone"})
            ED.expire_pairing()
            self.assertEqual(events(q, "pair_done"), [])
            clock[0] += ra.PAIR_TTL + 1
            ED.expire_pairing()
            self.assertEqual(events(q, "pair_done"), [{"id": started["request"], "allowed": False}])
            ED.expire_pairing()
            self.assertEqual(events(q, "pair_done"), [])   # once
        finally:
            BOT.hub.unsubscribe(q)
        self.assertEqual(ED.announced, {})
        self.assertEqual(remote("/api/pair/poll", {"request": started["request"], "secret": started["secret"]}),
                         (200, {"state": "expired"}))

    def test_deciding_an_expired_request_closes_it_at_once(self):
        clock = [time.time()]
        with ED.pair_lock:
            ED.pairing = ra.Pairing(clock=lambda: clock[0])
        _, started = remote("/api/pair/start", {"name": "Phone"})
        clock[0] += ra.PAIR_TTL + 1
        q = BOT.hub.subscribe()
        try:
            self.assertEqual(local("/api/remote/decide", {"request": started["request"], "allow": True})[0], 404)
            self.assertEqual(events(q, "pair_done"), [{"id": started["request"], "allowed": False}])
        finally:
            BOT.hub.unsubscribe(q)
        self.assertEqual(ED.devices.list(), [])

    def test_a_lost_poll_answer_can_be_asked_for_again(self):
        clock = [time.time()]
        with ED.pair_lock:
            ED.pairing = ra.Pairing(clock=lambda: clock[0])
        _, started = remote("/api/pair/start", {"name": "Phone"})
        self.assertEqual(local("/api/remote/decide", {"request": started["request"], "allow": True}),
                         (200, {"ok": True}))
        poll = {"request": started["request"], "secret": started["secret"]}
        first = remote("/api/pair/poll", poll)       # suppose this answer never reached the device
        self.assertEqual((first[0], first[1]["state"]), (200, "approved"))
        clock[0] += ra.PAIR_TTL - 1
        self.assertEqual(remote("/api/pair/poll", poll), first)
        clock[0] += 1                                # the approval is forgotten now
        self.assertEqual(remote("/api/pair/poll", poll)[0], 404)
        self.assertEqual(remote("/api/status", token=first[1]["token"])[0], 200)   # collected: the device stays

    def test_start_limit(self):
        PEER["ip"] = OTHER
        for _ in range(ra.MAX_STARTS_PER_PEER):
            status, started = remote("/api/pair/start", {"name": "Laptop"})
            self.assertEqual(status, 200, started)
            self.assertEqual(local("/api/remote/decide", {"request": started["request"], "allow": False})[0], 200)
        self.assertEqual(remote("/api/pair/start", {"name": "Laptop"}),
                         (429, {"error": "Too many requests from this device; wait a few minutes"}))
        self.assertEqual(len(NOTES), ra.MAX_STARTS_PER_PEER)   # no more pop-ups or notifications
        PEER["ip"] = DEVICE
        self.assertEqual(remote("/api/pair/start", {"name": "Phone"})[0], 200)

    def test_start_limit_counts_an_ipv6_64_as_one_device(self):
        for i in range(ra.MAX_STARTS_PER_PEER):
            PEER["ip"] = f"2001:db8:5:6::{i + 1:x}"
            status, started = remote("/api/pair/start", {"name": "Tablet"})
            self.assertEqual(status, 200, started)
            local("/api/remote/decide", {"request": started["request"], "allow": False})
        PEER["ip"] = "2001:db8:5:6::abcd"
        self.assertEqual(remote("/api/pair/start", {"name": "Tablet"})[0], 429)
        PEER["ip"] = "2001:db8:5:7::1"
        self.assertEqual(remote("/api/pair/start", {"name": "Tablet"})[0], 200)

    def test_notification_name_has_no_markup(self):
        _, started = remote("/api/pair/start", {"name": "<b>Evil</b> & co"})
        code = started["code"]
        self.assertEqual(NOTES, [f"bEvil/b  co wants to use the control panel. Code {code[:3]} {code[3:]}. "
                                 "Approve it in the panel."])

    def test_revoke(self):
        token, did = pair()
        with self.assertLogs("deck.Editor", logging.INFO) as cm:
            self.assertEqual(local("/api/remote/revoke", {"device": did}), (200, {"ok": True}))
        self.assertTrue(any("revoked 'Phone' (198.51.100.23)" in m for m in cm.output))
        self.assertEqual(remote("/api/status", token=token)[0], 401)
        self.assertEqual(local("/api/remote/revoke", {"device": did})[0], 404)
        self.assertEqual(local("/api/remote/revoke", {})[0], 400)

    def test_sign_out_all_panels_revokes_every_device(self):
        token, _ = pair()
        old = ED.token
        self.assertEqual(local("/api/session/revoke", {}), (200, {"ok": True}))
        self.assertNotEqual(ED.token, old)
        self.assertEqual(remote("/api/status", token=token)[0], 401)
        self.assertEqual(ED.devices.list(), [])


# ----- tokens and routes on the remote listener

class RemoteAuth(Base):
    def test_loopback_token_refused(self):
        self.assertEqual(remote("/api/status", token=ED.token)[0], 401)
        self.assertEqual(remote("/api/status")[0], 401)
        self.assertEqual(remote("/api/save", {"config": {}}, token=ED.token)[0], 401)

    def test_loopback_only_routes(self):
        token, _ = pair()
        before = ED.token
        for path in sorted(LOCAL_ONLY):
            for tok in (token, ED.token, None):
                status, body = remote(path, {"request": "x", "allow": True, "device": "x", "code": "x"}, token=tok)
                self.assertEqual((status, body), (403, {"error": LOCAL_ONLY_MESSAGE}), (path, tok))
        self.assertEqual(ED.token, before)
        self.assertEqual(len(ED.devices.list()), 1)
        with request_context.remote({"id": "x"}):   # and the route functions refuse by themselves too
            for fn, body in ((ED.decide_pairing, {"request": "x", "allow": True}),
                             (ED.revoke_device, {"device": "x"}), (ED.revoke_sessions, {})):
                with self.assertRaises(srv.ApiError) as cm:
                    fn(body)
                self.assertEqual(cm.exception.status, 403)

    def test_static_files_and_api_with_a_device_token(self):
        token, _ = pair()
        status, page = remote("/")
        self.assertEqual(status, 200)
        self.assertIn(b"<html", page.lower())
        self.assertEqual(remote("/api/state", token=token)[0], 200)
        self.assertEqual(remote("/api/nope", token=token)[0], 404)
        self.assertEqual(remote("/../core/paths.py")[0], 404)

    def test_other_methods(self):
        self.assertEqual(remote("/", method="OPTIONS")[0], 405)
        PEER["ip"] = None
        self.assertEqual(remote("/", method="OPTIONS")[0], 403)   # still checked first


class RemoteChecks(Base):
    def test_bad_host(self):
        token, _ = pair()
        for path, tok in (("/", None), ("/api/status", token)):
            self.assertEqual(remote(path, token=tok, headers={"Host": f"evil.example:{RPORT}"})[0], 421)
            self.assertEqual(remote(path, token=tok, headers={"Host": "127.0.0.1"})[0], 421)   # no port
        self.assertEqual(remote("/api/pair/start", {"name": "x"}, headers={"Host": f"evil.example:{RPORT}"})[0], 421)

    def test_allowed_hosts(self):
        for host in (f"127.0.0.1:{RPORT}", f"streampc.local:{RPORT}", f"STREAMPC:{RPORT}", f"{LAN}:{RPORT}",
                     f"streampc.local.:{RPORT}"):
            self.assertEqual(remote("/", headers={"Host": host})[0], 200, host)
        self.assertEqual(remote("/", headers={"Host": f"streampc.lan:{RPORT}"})[0], 421)   # not in editor.remote.hosts

    def test_two_host_headers(self):
        s = socket.create_connection(("127.0.0.1", RPORT), timeout=3)
        try:
            s.sendall(f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{RPORT}\r\nHost: evil.example:{RPORT}\r\n"
                      "Connection: close\r\n\r\n".encode())
            self.assertIn(b" 421 ", s.recv(4096).split(b"\r\n")[0] + b" ")
        finally:
            s.close()

    def test_origin(self):
        self.assertEqual(remote("/api/pair/start", {"name": "x"}, origin=False)[0], 403)
        for origin in ("http://evil.example", "null", f"https://127.0.0.1:{RPORT}", f"http://127.0.0.1:{PORT}",
                       f"http://127.0.0.1:{RPORT}/x"):
            self.assertEqual(remote("/api/pair/start", {"name": "x"}, origin=False, headers={"Origin": origin})[0],
                             403, origin)
            self.assertEqual(remote("/", headers={"Origin": origin})[0], 403, origin)
        self.assertEqual(remote("/api/pair/poll", {"request": "x", "secret": "y"}, origin=False)[0], 403)
        self.assertEqual(remote("/api/pair/start", {"name": "x"})[0], 200)
        self.assertEqual(remote("/api/pair/start", {"name": "x"},
                                headers={"Origin": f"http://streampc.local:{RPORT}"})[0], 200)

    def test_loopback_peer_refused(self):
        PEER["ip"] = None   # the real peer: 127.0.0.1
        self.assertEqual(remote("/")[0], 403)
        self.assertEqual(remote("/api/pair/start", {"name": "x"})[0], 403)
        self.assertEqual(ED.pairing.pending(), [])

    def test_this_computers_own_addresses_refused(self):
        for ip in (LAN, LINK_LOCAL, "::ffff:" + LAN, "127.0.0.2"):
            PEER["ip"] = ip
            self.assertEqual(remote("/")[0], 403, ip)
            self.assertEqual(remote("/api/pair/start", {"name": "x"})[0], 403, ip)
        PEER["ip"] = DEVICE
        self.assertEqual(remote("/")[0], 200)

    def test_peer_refused_seam(self):
        s = ED.remote_server
        self.assertEqual(s.peer_refused("127.0.0.1"), "loopback")
        self.assertEqual(s.peer_refused("::"), "loopback")
        self.assertEqual(s.peer_refused(LAN), "self")
        self.assertEqual(s.peer_refused(DEVICE, here=DEVICE), "self")   # the address it connected to
        self.assertIsNone(s.peer_refused(DEVICE, here="127.0.0.1"))

    def test_peer_limit(self):
        for _ in range(10):
            self.assertEqual(remote("/api/status", token="x" * 43)[0], 401)
        self.assertEqual(remote("/")[0], 429)
        self.assertEqual(remote("/api/pair/start", {"name": "x"})[0], 429)
        PEER["ip"] = OTHER
        self.assertEqual(remote("/")[0], 200)
        for _ in range(10):
            self.assertEqual(remote("/api/pair/poll", {"request": "nope", "secret": "x"})[0], 404)
        self.assertEqual(remote("/")[0], 429)

    def test_bad_tokens_from_many_ipv4_addresses_dont_lock_out_a_device(self):
        token, _ = pair()
        statuses = []
        with captured():   # (a warning for each address)
            for i in range(ED.peer_limit.global_limit + 20):   # one failure from each of 120 addresses
                PEER["ip"] = f"203.0.113.{i + 1}"
                statuses.append(remote("/api/status", token="x" * 43)[0])
        limit = ED.peer_limit.global_limit
        self.assertEqual(statuses, [401] * limit + [429] * 20)   # everyone else is refused now...
        PEER["ip"] = "203.0.113.250"
        self.assertEqual(remote("/")[0], 429)
        self.assertEqual(remote("/api/pair/start", {"name": "x"})[0], 429)
        PEER["ip"] = DEVICE                                     # ...but the approved device isn't
        self.assertEqual(remote("/api/status", token=token)[0], 200)
        self.assertEqual(remote("/api/state", token=token)[0], 200)
        self.assertEqual(remote("/api/status", token="x" * 43)[0], 429)   # a wrong token still is
        s = Stream(RPORT, token)
        try:
            self.assertEqual(s.status(), 200)
        finally:
            s.close()
            drain_streams()

    def test_bad_tokens_from_one_ipv6_64_dont_lock_out_a_device_in_it(self):
        token, _ = pair(ip="2001:db8:5:6::1")
        statuses = []
        with captured():
            for i in range(30):   # a new address each time, all in one /64
                PEER["ip"] = f"2001:db8:5:6::{i + 2:x}"
                statuses.append(remote("/api/status", token="x" * 43)[0])
        self.assertEqual(statuses, [401] * 10 + [429] * 20)   # one /64 is one peer
        PEER["ip"] = "2001:db8:5:6:ffff:ffff:ffff:ffff"
        self.assertEqual(remote("/")[0], 429)
        self.assertEqual(remote("/api/pair/start", {"name": "x"})[0], 429)
        PEER["ip"] = "2001:db8:5:7::1"                          # the next /64 isn't blocked
        self.assertEqual(remote("/")[0], 200)
        PEER["ip"] = "2001:db8:5:6::1"                          # nor the approved device inside this one
        self.assertEqual(remote("/api/status", token=token)[0], 200)
        PEER["ip"] = "2001:db8:5:6::99"                         # wherever in it it comes from
        self.assertEqual(remote("/api/status", token=token)[0], 200)

    def test_a_device_over_its_own_failure_limit_still_gets_in(self):
        token, _ = pair()
        for _ in range(10):
            self.assertEqual(remote("/api/status", token="x" * 43)[0], 401)
        self.assertEqual(remote("/")[0], 429)                               # no token: limited
        self.assertEqual(remote("/api/pair/poll", {"request": "x", "secret": "y"})[0], 429)
        self.assertEqual(remote("/api/status", token="x" * 43)[0], 429)     # a wrong token: limited
        self.assertEqual(remote("/api/status", token=token)[0], 200)        # its own token: never
        self.assertEqual(remote("/api/nope", token=token)[0], 404)
        self.assertEqual(remote("/api/session/revoke", {}, token=token)[0], 403)   # still LOCAL_ONLY

    def test_a_valid_token_is_checked_after_host_and_origin(self):
        token, _ = pair()
        for _ in range(10):
            remote("/api/status", token="x" * 43)
        self.assertEqual(remote("/api/status", token=token, headers={"Host": f"evil.example:{RPORT}"})[0], 421)
        self.assertEqual(remote("/api/status", token=token, headers={"Origin": "http://evil.example"})[0], 403)
        PEER["ip"] = LAN
        self.assertEqual(remote("/api/status", token=token)[0], 403)   # this computer itself

    def test_connection_cap_counts_an_ipv6_64_as_one_peer(self):
        s = ED.remote_server
        self.assertTrue(wait_for(lambda: not s._conns))
        held = []
        cases = (("2001:db8:5:6::1", True), ("2001:db8:5:6::2", True), ("2001:db8:5:6::ffff", False),
                 ("::ffff:198.51.100.1", True), ("2001:db8:5:7::1", True),
                 ("198.51.100.1", True), ("198.51.100.1", False), ("198.51.100.2", True))
        with mock.patch.object(srv, "MAX_REMOTE_PER_PEER", 2), captured():
            try:
                for ip, want in cases:
                    sock = mock.Mock()
                    ok = s.verify_request(sock, (ip, 0, 0, 0) if ":" in ip else (ip, 0))
                    self.assertEqual(ok, want, ip)
                    if ok:
                        held.append(sock)
                self.assertEqual(s._per_peer["2001:db8:5:6::/64"], 2)
                self.assertEqual(s._per_peer["198.51.100.1"], 2)
            finally:
                for sock in held:
                    s.shutdown_request(sock)
        self.assertEqual(dict(s._per_peer), {})

    def test_failed_starts_count(self):
        PEER["ip"] = OTHER
        for _ in range(10):
            self.assertEqual(remote("/api/pair/start", {"name": "x"}, origin=False)[0], 403)
        self.assertEqual(remote("/api/pair/start", {"name": "x"})[0], 429)

    def test_refusals_logged_once_a_minute(self):
        with self.assertLogs("deck.Editor", logging.WARNING) as cm:
            for _ in range(3):
                remote("/", headers={"Host": f"evil.example:{RPORT}"})
                remote("/api/status", token="secret-looking-token-" + "x" * 30)  # privacy-check: allow
            PEER["ip"] = OTHER
            remote("/", headers={"Host": f"evil.example:{RPORT}"})
        hosts = [m for m in cm.output if "evil.example" in m]
        self.assertEqual(len(hosts), 2, cm.output)   # once for each address
        self.assertEqual(sum("wrong device token" in m for m in cm.output), 1)
        self.assertFalse(any("secret-looking-token" in m for m in cm.output))

    def test_pair_body_is_small(self):
        self.assertEqual(remote("/api/pair/start", {"name": "x" * (srv.PAIR_BODY + 10)})[0], 413)
        self.assertEqual(remote("/api/pair/start", [1])[0], 400)

    def test_connection_cap(self):
        held = []
        self.assertTrue(wait_for(lambda: not ED.remote_server._conns))
        with mock.patch.object(srv, "MAX_REMOTE_PER_PEER", 2):
            try:
                for _ in range(2):
                    held.append(socket.create_connection(("127.0.0.1", RPORT), timeout=3))
                self.assertTrue(wait_for(lambda: len(ED.remote_server._conns) == 2))
                extra = socket.create_connection(("127.0.0.1", RPORT), timeout=3)
                held.append(extra)
                extra.sendall(f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{RPORT}\r\n\r\n".encode())
                try:
                    self.assertEqual(extra.recv(4096), b"")   # closed unanswered
                except ConnectionResetError:
                    pass
            finally:
                for s in held:
                    s.close()
        self.assertTrue(wait_for(lambda: remote("/")[0] == 200))


# ----- a request's line and headers have HEAD_SECONDS

class HeadTimeout(Base):
    def test_the_deadline(self):
        self.assertEqual(srv.HEAD_SECONDS, 10)
        # each recv waits up to the handler's timeout, so only an overall deadline stops a trickle
        self.assertGreater(srv.RemoteHandler.timeout, srv.HEAD_SECONDS)

    def test_trickled_headers_are_cut_off(self):
        got, closed_at = b"", None
        with mock.patch.object(srv, "HEAD_SECONDS", 1.0):
            s = socket.create_connection(("127.0.0.1", RPORT), timeout=3)
            try:
                start = time.monotonic()
                s.sendall(f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{RPORT}\r\n".encode())
                s.settimeout(0.1)
                trickle = itertools.cycle(b"X-Slow: y\r\n")   # header after header, never the blank line
                while time.monotonic() - start < 6:
                    try:
                        s.sendall(bytes([next(trickle)]))     # a byte every 0.1 s: each recv is quick
                        chunk = s.recv(4096)
                    except socket.timeout:
                        continue
                    except OSError:
                        closed_at = time.monotonic()
                        break
                    if not chunk:
                        closed_at = time.monotonic()
                        break
                    got += chunk
            finally:
                s.close()
        self.assertIsNotNone(closed_at, "a client trickling its headers kept the connection")
        self.assertEqual(got, b"")                       # closed unanswered
        self.assertGreater(closed_at - start, 0.8)       # by the deadline...
        self.assertLess(closed_at - start, 1.0 + 1.5)    # ...plus slack
        self.assertEqual(remote("/")[0], 200)            # and the listener is fine

    def test_silence_isnt_a_request(self):
        with mock.patch.object(srv, "HEAD_SECONDS", 0.5), mock.patch.object(srv.RemoteHandler, "timeout", 1.0):
            s = socket.create_connection(("127.0.0.1", RPORT), timeout=3)
            try:
                start = time.monotonic()
                try:
                    self.assertEqual(s.recv(4096), b"")   # closed by the handler's own timeout
                except ConnectionResetError:
                    pass
                self.assertGreater(time.monotonic() - start, 0.8)
            finally:
                s.close()

    def test_only_the_head_is_timed(self):
        token, _ = pair()
        with mock.patch.object(srv, "HEAD_SECONDS", 0.5):
            s = socket.create_connection(("127.0.0.1", RPORT), timeout=3)
            try:
                for piece in (b"GET / HT", f"TP/1.1\r\nHost: 127.0.0.1:{RPORT}\r\n".encode(), b"\r\n"):
                    s.sendall(piece)
                    time.sleep(0.1)   # slow, but all within the deadline
                self.assertEqual(read_response(s)[0], 200)
                time.sleep(1.0)       # idle between requests is no request's head
                s.sendall(f"GET /api/status HTTP/1.1\r\nHost: 127.0.0.1:{RPORT}\r\nX-Deck-Token: {token}\r\n\r\n"
                          .encode())
                self.assertEqual(read_response(s)[0], 200)
            finally:
                s.close()
            stream = Stream(RPORT, token)   # and an answer may take as long as it takes
            try:
                self.assertEqual(stream.block()[0], "hello")
                time.sleep(1.0)
                BOT.hub.publish("page", {"page": "marker"})
                self.assertEqual(stream.until("page")[-1], "page")
            finally:
                stream.close()
                drain_streams()

    def test_the_local_panel_is_unchanged(self):
        for name in ("setup", "handle_one_request", "parse_request"):
            self.assertNotIn(name, vars(srv.Handler), name)   # only the remote listener has the deadline
            self.assertIn(name, vars(srv.RemoteHandler), name)


# ----- event streams

class Streams(Base):
    def tearDown(self):
        drain_streams()
        super().tearDown()

    def test_revoke_ends_the_stream(self):
        token, did = pair()
        s = Stream(RPORT, token)
        try:
            self.assertEqual(s.status(), 200)
            self.assertEqual(s.block()[0], "hello")
            self.assertEqual(local("/api/remote/revoke", {"device": did}), (200, {"ok": True}))
            self.assertTrue(s.ended(), "the revoked device's stream is still open")
        finally:
            s.close()
        again = Stream(RPORT, token)
        try:
            self.assertEqual(again.status(), 401)
        finally:
            again.close()

    def test_remote_cap_and_local_cap(self):
        token, _ = pair()
        streams = []
        try:
            for _ in range(MAX_REMOTE_SUBSCRIBERS):
                streams.append(Stream(RPORT, token))
                self.assertEqual(streams[-1].block()[0], "hello")
            extra = Stream(RPORT, token)
            streams.append(extra)
            self.assertEqual(extra.status(), 503)
            for _ in range(MAX_SUBSCRIBERS):   # other devices don't use up this computer's panels
                streams.append(Stream(PORT, ED.token))
                self.assertEqual(streams[-1].block()[0], "hello")
            extra = Stream(PORT, ED.token)
            streams.append(extra)
            self.assertEqual(extra.status(), 503)
        finally:
            for s in streams:
                s.close()

    def test_no_pairing_events_on_remote_streams(self):
        token, _ = pair()
        mine, here = Stream(RPORT, token), Stream(PORT, ED.token)
        try:
            self.assertEqual(mine.block()[0], "hello")
            self.assertEqual(here.block()[0], "hello")
            PEER["ip"] = OTHER
            _, started = remote("/api/pair/start", {"name": "Laptop"})
            local("/api/remote/decide", {"request": started["request"], "allow": False})
            BOT.hub.publish("page", {"page": "marker"})
            seen_here = here.until("page")
            self.assertIn("pair_request", seen_here)
            self.assertIn("pair_done", seen_here)
            seen_remote = mine.until("page")
            self.assertFalse({"pair_request", "pair_done"} & set(seen_remote), seen_remote)
        finally:
            mine.close()
            here.close()

    def test_turning_off_ends_streams_and_keep_alive_connections(self):
        token, _ = pair()
        s = Stream(RPORT, token)
        keep = socket.create_connection(("127.0.0.1", RPORT), timeout=3)
        try:
            self.assertEqual(s.block()[0], "hello")
            keep.sendall(f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{RPORT}\r\n\r\n".encode())
            time.sleep(0.2)
            self.assertIn(b" 200 ", keep.recv(65536).split(b"\r\n")[0] + b" ")
            reconfigure(enabled=False)
            self.assertTrue(s.ended())
            got = b"x"
            deadline = time.monotonic() + 3
            while got and time.monotonic() < deadline:
                try:
                    keep.sendall(f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{RPORT}\r\n\r\n".encode())
                    got = keep.recv(65536)
                except OSError:
                    got = b""
            self.assertEqual(got, b"", "a keep-alive connection outlived remote access")
        finally:
            s.close()
            keep.close()


# ----- turning it on and off, and changing it

class Lifecycle(Base):
    def test_a_remote_change_restarts_only_the_remote_listener(self):
        panel, before = ED.server, ED.remote_server
        reconfigure(hosts=["streampc.lan"])
        self.assertIs(ED.server, panel)
        self.assertIsNotNone(ED.remote_server)
        self.assertIsNot(ED.remote_server, before)
        self.assertEqual(remote("/", headers={"Host": f"streampc.lan:{RPORT}"})[0], 200)
        self.assertEqual(local("/api/status")[0], 200)

    def test_device_days(self):
        before = ED.remote_server
        for days, want in ((7, 7.0), (1000, 365.0), (0, 1.0), ("x", 30.0), (True, 30.0), (2.5, 2.5)):
            reconfigure(device_days=days)
            self.assertEqual(ED.devices.days, want, days)
        self.assertIs(ED.remote_server, before)   # no restart for it

    def test_same_port_as_the_panel(self):
        panel = ED.server
        reconfigure(port=PORT)
        self.assertIsNone(ED.remote_server)
        st = local("/api/status")[1]["remote"]
        self.assertIn("editor.port", st["error"])
        self.assertEqual((st["enabled"], st["port"], st["urls"]), (True, PORT, []))
        self.assertIs(ED.server, panel)
        self.assertTrue(editor_auth.is_bound(PORT))

    def test_bad_settings_keep_it_off(self):
        for over, needle in (({"listen": "not-an-address"}, "editor.remote.listen"),
                             ({"port": 70000}, "editor.remote.port"), ({"port": True}, "editor.remote.port"),
                             ({"port": 8767.5}, "editor.remote.port")):
            reconfigure(**over)
            self.assertIsNone(ED.remote_server, over)
            self.assertIn(needle, local("/api/status")[1]["remote"]["error"], over)
        ED.on_config({**ED.config, "enabled": False, "remote": remote_cfg()})   # the panel itself off
        try:
            self.assertIsNone(ED.remote_server)
            self.assertIn("editor.enabled", ED.remote_error)
        finally:
            ED.on_config({**ED.config, "enabled": True, "port": PORT, "remote": remote_cfg()})
        self.assertTrue(wait_for(lambda: ED.server is not None))

    def test_settings_parsing(self):
        ed = type(ED)(BOT)   # not loaded: nothing listens

        def settings(**remote):
            ed.config = {"port": 8765, "remote": remote}
            return ed._remote_settings()
        s = settings(enabled=True)
        self.assertEqual((s.enabled, s.listen, s.port, s.hosts, s.days, s.error), (True, "0.0.0.0", 8767, (), 30, None))
        s = settings(enabled=True, listen=None, port=None, device_days=None)   # `key:` with no value: the default
        self.assertEqual((s.listen, s.port, s.days, s.error), ("0.0.0.0", 8767, 30, None))
        s = settings(enabled=True, listen="[::FFFF:192.0.2.5]", port="9000", hosts="pc.lan")
        self.assertEqual((s.listen, s.port, s.hosts), ("192.0.2.5", 9000, ("pc.lan",)))
        self.assertEqual(settings(enabled=True, hosts=["a", 5, "a", None, "b"]).hosts, ("a", "b"))
        self.assertEqual(settings(enabled=True, hosts={"a": 1}).hosts, ())
        self.assertFalse(settings(enabled="yes").enabled)   # only true turns it on
        self.assertIsNone(settings(enabled=False, port=8765).error)   # off: nothing to complain about
        self.assertIn("editor.port", settings(enabled=True, port=8765).error)
        ed.config = {"remote": {"enabled": True, "port": editor_auth.DEFAULT_PORT}}   # editor.port's default
        self.assertIn("editor.port", ed._remote_settings().error)
        st = ed.remote_status()
        self.assertEqual((st["urls"], st["devices"], st["pending"], st["this_device"]), ([], [], [], None))

    def test_turning_off_cancels_waiting_requests_and_keeps_devices(self):
        token, did = pair()
        PEER["ip"] = OTHER
        _, started = remote("/api/pair/start", {"name": "Laptop"})
        q = BOT.hub.subscribe()
        try:
            reconfigure(enabled=False)
            self.assertEqual(events(q, "pair_done"), [{"id": started["request"], "allowed": False}])
        finally:
            BOT.hub.unsubscribe(q)
        self.assertIsNone(ED.remote_server)
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", RPORT), timeout=1).close()
        st = local("/api/status")[1]["remote"]
        self.assertEqual((st["enabled"], st["urls"], st["pending"], st["error"]), (False, [], [], None))
        self.assertEqual([d["id"] for d in st["devices"]], [did])
        reconfigure()
        PEER["ip"] = DEVICE
        self.assertEqual(remote("/api/status", token=token)[0], 200)   # no need to pair again

    def test_busy_port_is_retried(self):
        g = ED._bind_remote_retry.__globals__
        held = socket.socket()
        held.bind(("127.0.0.1", 0))
        held.listen()
        port = held.getsockname()[1]
        with mock.patch.dict(g, {"BIND_RETRY": (0.1, 0.2)}):
            try:
                with self.assertLogs("deck.Editor", logging.DEBUG) as cm:
                    reconfigure(port=port)
                    self.assertIsNone(ED.remote_server)
                    self.assertIn("in use", ED.remote_error)
                    time.sleep(0.4)
                self.assertEqual(sum(r.levelno == logging.WARNING and "in use" in r.getMessage()
                                     for r in cm.records), 1)
                held.close()
                self.assertTrue(wait_for(lambda: ED.remote_server is not None))
                self.assertIsNone(ED.remote_error)
                self.assertEqual(ED.remote_server.port, port)
            finally:
                held.close()

    def test_ipv6_dual_stack(self):
        if not socket.has_ipv6:
            self.skipTest("no IPv6 on this platform")
        try:
            with socket.socket(socket.AF_INET6) as probe:
                probe.bind(("::1", 0))
        except OSError:
            self.skipTest("no IPv6 on this platform")
        reconfigure(listen="::", hosts=["127.0.0.1", "::1"])
        s = ED.remote_server
        self.assertIsNotNone(s, ED.remote_error)
        self.assertEqual(s.socket.family, socket.AF_INET6)
        self.assertEqual(s.socket.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY), 0)
        self.assertEqual(remote("/")[0], 200)                                     # over IPv4
        self.assertEqual(request(RPORT, "/", host="[::1]")[0], 200)                # over IPv6
        PEER["ip"] = None
        self.assertEqual(remote("/")[0], 403)   # the real peer, ::ffff:127.0.0.1, is loopback
        self.assertEqual(request(RPORT, "/", host="[::1]")[0], 403)
        self.assertEqual(local("/api/status")[1]["remote"]["listen"], "::")


# ----- other devices can't rewrite the `editor` section

class ConfigGuard(Base):
    def test_save_through_the_panel(self):
        token, _ = pair()
        status, state = remote("/api/state", token=token)
        self.assertEqual(status, 200)
        before = configio.file_hash(CONFIG)
        for change in (lambda c: c["editor"]["remote"].update(port=RPORT + 1),
                       lambda c: c["editor"]["remote"].update(enabled=1),            # 1 is not true
                       lambda c: c["editor"].update(port=float(PORT)),
                       lambda c: c.pop("editor")):
            cfg = copy.deepcopy(state["config"])
            change(cfg)
            self.assertEqual(remote("/api/save", {"config": cfg, "hash": state["hash"]}, token=token),
                             (403, {"error": REMOTE_SETTINGS}))
        self.assertEqual(configio.file_hash(CONFIG), before)
        cfg = copy.deepcopy(state["config"])
        cfg["brightness"] = 42
        status, saved = remote("/api/save", {"config": cfg, "hash": state["hash"]}, token=token)
        self.assertEqual(status, 200, saved)
        on_disk = configio.load(CONFIG)
        self.assertEqual(on_disk["brightness"], 42)
        self.assertEqual(on_disk["editor"], state["config"]["editor"])

    def test_save_directly(self):
        path = os.path.join(tmpdir(), "config.yaml")
        base = {"pages": {"main": []}, "editor": {"port": 8765}}
        configio.save(path, base)
        device = {"id": "dev1", "name": "Phone"}
        with request_context.remote(device):
            with self.assertRaises(request_context.RemoteRefused) as cm:
                configio.save(path, {**base, "editor": {"port": 8765, "remote": {"enabled": True}}})
            self.assertEqual((cm.exception.status, str(cm.exception)), (403, REMOTE_SETTINGS))
            configio.save(path, {**base, "brightness": 10})   # the rest may change
            missing = os.path.join(tmpdir(), "config.yaml")
            configio.save(missing, {"pages": {"main": []}, "editor": None})   # none either way
            with self.assertRaises(request_context.RemoteRefused):
                configio.save(os.path.join(tmpdir(), "config.yaml"), base)
            with open(path, "w") as f:
                f.write("pages: [unclosed\n")
            with self.assertRaisesRegex(request_context.RemoteRefused, "can't be read"):
                configio.save(path, base)
        configio.save(path, {**base, "editor": {"port": 9999}})   # on this computer: as before
        self.assertEqual(configio.load(path)["editor"], {"port": 9999})

    def test_backup_restores(self):
        original = open(CONFIG, "rb").read()
        same = backups.record(CONFIG, "manual", label="same editor")["id"]
        changed = yaml.safe_load(original)
        changed["editor"]["remote"]["port"] = RPORT + 1
        with open(CONFIG, "w") as f:
            yaml.safe_dump(changed, f, sort_keys=False)
        other = backups.record(CONFIG, "manual", label="other editor")["id"]
        backups.daily(CONFIG, now=datetime.datetime(2001, 2, 3, 12))
        with open(CONFIG, "wb") as f:
            f.write(original)
        token, _ = pair()
        try:
            self.assertEqual(remote("/api/backups/restore", {"id": other}, token=token),
                             (403, {"error": REMOTE_SETTINGS}))
            self.assertEqual(open(CONFIG, "rb").read(), original)
            self.assertEqual(remote("/api/backups/restore-daily", {"date": "2001-02-03"}, token=token),
                             (403, {"error": REMOTE_SETTINGS}))
            self.assertEqual(open(CONFIG, "rb").read(), original)
            status, body = remote("/api/backups/restore", {"id": same}, token=token)
            self.assertEqual(status, 200, body)
            status, body = local("/api/backups/restore", {"id": other})   # on this computer: allowed
            self.assertEqual(status, 200, body)
            self.assertEqual(configio.load(CONFIG)["editor"]["remote"]["port"], RPORT + 1)
        finally:
            with open(CONFIG, "wb") as f:
                f.write(original)

    def test_setup_backup_restore(self):
        args = ("20250101-000000-000000-obs", "obs")
        kw = {"roots": {"obs": tmpdir()}, "is_running": lambda kind: False, "confirm": True}
        with request_context.remote({"id": "dev1"}):
            with self.assertRaises(request_context.RemoteRefused) as cm:
                setup_backup.restore(*args, **kw)
            self.assertEqual(cm.exception.status, 403)
            with self.assertRaises(request_context.RemoteRefused):
                setup_backup.restore(*args, **{**kw, "confirm": False})   # refused before anything else
        with self.assertRaises(setup_backup.NotFound):   # on this computer it goes on as before
            setup_backup.restore(*args, **kw)


# ----- the pieces

class RequestContext(unittest.TestCase):
    def test_set_and_always_reset(self):
        self.assertIsNone(request_context.remote_device())
        a, b = {"id": "a"}, {"id": "b"}
        with request_context.remote(a):
            self.assertIs(request_context.remote_device(), a)
            with request_context.remote(b):
                self.assertIs(request_context.remote_device(), b)
            self.assertIs(request_context.remote_device(), a)
            with self.assertRaises(request_context.RemoteRefused):
                request_context.refuse_remote()
        self.assertIsNone(request_context.remote_device())
        with self.assertRaises(KeyError):
            with request_context.remote(a):
                raise KeyError
        self.assertIsNone(request_context.remote_device())
        request_context.refuse_remote()   # nothing to refuse
        with self.assertRaises(TypeError):
            with request_context.remote(None):
                pass

    def test_other_threads_dont_see_it(self):
        seen = []
        with request_context.remote({"id": "a"}):
            t = threading.Thread(target=lambda: seen.append(request_context.remote_device()))
            t.start()
            t.join()
        self.assertEqual(seen, [None])


class OwnAddresses(unittest.TestCase):
    def test_every_address_including_loopback_and_link_local(self):
        self.assertEqual(ra.own_addresses(run=lambda argv: IP_JSON), {"127.0.0.1", "::1", LAN, LINK_LOCAL})
        self.assertEqual(ra.local_addresses(run=lambda argv: IP_JSON), [LAN])

    def test_junk_skipped_and_fallback(self):
        junk = json.dumps([{"addr_info": [{"family": "inet", "local": "nope"}, {"family": "inet6",
                                                                               "local": "::ffff:10.0.0.1"}]}])
        self.assertEqual(ra.own_addresses(run=lambda argv: junk), {"10.0.0.1"})

        def broken(argv):
            raise OSError("no ip")
        with mock.patch.object(ra, "_from_getaddrinfo", return_value=["192.0.2.9", "fe80::9%eth0"]):
            self.assertEqual(ra.own_addresses(run=broken), {"192.0.2.9", "fe80::9"})
        with mock.patch.object(ra, "_from_getaddrinfo", side_effect=OSError("no resolver")):
            self.assertEqual(ra.own_addresses(run=broken), set())


class Pieces(unittest.TestCase):
    def test_address_cache(self):
        calls, now = [], [0.0]

        def run(argv):
            calls.append(argv)
            return IP_JSON
        cache = AddressCache(ttl=60, run=run, hostname=lambda: "box", clock=lambda: now[0])
        first = cache.get()
        self.assertEqual((first.local, first.hostname), ((LAN,), "box"))
        self.assertEqual(first.own, {"127.0.0.1", "::1", LAN, LINK_LOCAL})
        n = len(calls)
        now[0] = 59
        self.assertIs(cache.get(), first)
        self.assertEqual(len(calls), n)
        now[0] = 61
        self.assertIsNot(cache.get(), first)
        self.assertGreater(len(calls), n)

        def no_name():
            raise OSError
        self.assertEqual(AddressCache(run=run, hostname=no_name).get().hostname, "")

    def test_warn_limit(self):
        now = [0.0]
        limit = WarnLimit(every=60, size=3, clock=lambda: now[0])
        self.assertTrue(limit(("a", "host")))
        self.assertFalse(limit(("a", "host")))
        self.assertTrue(limit(("a", "token")))
        self.assertTrue(limit(("b", "host")))
        now[0] = 61
        self.assertTrue(limit(("a", "host")))
        for i in range(10):   # bounded
            limit((str(i), "x"))
        self.assertLessEqual(len(limit._last), 3)

    def test_head_deadline(self):
        a, b = socket.socketpair()
        try:
            a.settimeout(5)
            head, buf = HeadDeadline(a), bytearray(16)
            b.sendall(b"x")
            self.assertEqual(head.readinto(buf), 1)   # disarmed: a plain recv
            head.arm(0.3)
            time.sleep(0.5)                           # waiting for the first byte isn't timed
            b.sendall(b"y")
            start = time.monotonic()
            self.assertEqual(head.readinto(buf), 1)   # it starts the clock
            b.sendall(b"z")
            self.assertEqual(head.readinto(buf), 1)
            with self.assertRaises(TimeoutError):
                head.readinto(buf)                    # nothing more: it waits only until the deadline
            self.assertLess(time.monotonic() - start, 1.5)
            self.assertEqual(a.gettimeout(), 5)       # the socket's own timeout is back
            b.sendall(b"w")
            with self.assertRaises(TimeoutError):
                head.readinto(buf)                    # past the deadline nothing more is read
            head.disarm()
            self.assertEqual(head.readinto(buf), 1)   # (bodies are read disarmed)
            self.assertEqual(bytes(buf[:1]), b"w")
            head.arm(10)
            b.close()
            self.assertEqual(head.readinto(buf), 0)   # the client went away
            self.assertTrue(head.readable())
            head.close()
            self.assertGreaterEqual(a.fileno(), 0)    # closing the reader leaves the socket open
        finally:
            a.close()
            b.close()

    def test_stream_slots(self):
        slots = StreamSlots(2)
        self.assertTrue(slots.take())
        self.assertTrue(slots.take())
        self.assertFalse(slots.take())
        slots.give()
        self.assertTrue(slots.take())
        for _ in range(5):
            slots.give()
        self.assertEqual(slots.used, 0)

    def test_docstring_describes_the_second_listener(self):
        doc = srv.__doc__
        for words in ("second listener", "loopback", "429", "421", "Origin", "/api/pair/start", "401",
                      "LOCAL_ONLY", "MAX_REMOTE_SUBSCRIBERS", "pair_request", "request_context", "HEAD_SECONDS",
                      "/64", "approved device isn't locked out"):
            self.assertIn(words, doc)
        self.assertTrue(re.search(r"never accepted there", doc))


if __name__ == "__main__":
    unittest.main()
