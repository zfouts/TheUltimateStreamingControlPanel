"""core/remote_access.py: letting other devices use the panel. Address normalisation and the
loopback rule, this computer's addresses (`ip -j addr` and the resolver fallback), Host checks and
the URLs shown, device names, failure limits, pairing (limits, codes, poll states, expiry) and the
approved-device store (hashes only, 0600, expiry, revocation, the cap, corrupt files, last_seen).
Clocks are fakes and every file lives in a temp folder; no command is ever run."""
import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import threading
import unittest
from unittest import mock

from tests.helpers import tmpdir

from core import remote_access as ra  # noqa: E402

LAN_V4, LAN_V6, VPN_V4 = "192.168.1.50", "2001:db8:1::50", "100.101.102.103"
PHONE, LAPTOP = "192.168.1.23", "192.168.1.24"
PUBLIC_KEYS = {"id", "name", "ip", "created", "last_seen", "expires"}
T0 = 1_700_000_000.0


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def store_path():
    return os.path.join(tmpdir(), "remote_devices.json")


def sha(token):
    return hashlib.sha256(token.encode()).hexdigest()


# ----- addresses


class NormalizeIp(unittest.TestCase):
    def test_canonical_forms(self):
        cases = {
            "192.168.1.5": "192.168.1.5",
            "::ffff:192.168.1.5": "192.168.1.5",         # dual-stack socket's view of an IPv4 peer
            "::FFFF:192.168.1.5": "192.168.1.5",
            "::ffff:c0a8:105": "192.168.1.5",            # the same mapped address in hex
            "[::ffff:192.168.1.5]": "192.168.1.5",
            "[2001:db8::1]": "2001:db8::1",
            "2001:0DB8:0000::0001": "2001:db8::1",
            "fe80::1%eth0": "fe80::1",
            "[fe80::1%wlan0]": "fe80::1",
            "2001:db8::1%": "2001:db8::1",
            "127.0.0.1": "127.0.0.1",
            "::1": "::1",
        }
        for raw, want in cases.items():
            self.assertEqual(ra.normalize_ip(raw), want, raw)

    def test_rejects(self):
        for bad in ("", "abc", "192.168.1.5:80", "[192.168.1.5", "2001:db8::1]", " 192.168.1.5", "192.168.1.5\n",
                    "192.168.1.5%eth0", "010.1.1.1", "--json", "[]", "1.2.3.4.5", "localhost", None, 123,
                    b"192.168.1.5", ["192.168.1.5"]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                ra.normalize_ip(bad)


class IsLoopback(unittest.TestCase):
    def test_loopback(self):
        for ip in ("127.0.0.1", "127.1.2.3", "127.255.255.254", "::1", "[::1]", "0:0:0:0:0:0:0:1",
                   "::ffff:127.0.0.1", "::ffff:7f00:1", "[::ffff:127.0.0.2]", "0.0.0.0", "::"):
            self.assertTrue(ra.is_loopback(ip), ip)

    def test_junk_is_loopback(self):
        for junk in ("", "abc", "localhost", "127.0.0.1:80", None, 127, b"127.0.0.1", ["::1"]):
            self.assertTrue(ra.is_loopback(junk), repr(junk))

    def test_not_loopback(self):
        for ip in (PHONE, LAN_V6, "::ffff:192.168.1.23", "fe80::1%eth0", VPN_V4, "10.0.0.1", "128.0.0.1",
                   "::2", "::ffff:128.0.0.1"):
            self.assertFalse(ra.is_loopback(ip), ip)


# `ip -j addr show` on a host with loopback, a LAN interface (v4, link-local v6, global v6),
# a down interface without addresses, and a VPN-style point-to-point interface with a 100.x address.
IP_ADDR_JSON = json.dumps([
    {"ifindex": 1, "ifname": "lo", "flags": ["LOOPBACK", "UP", "LOWER_UP"], "mtu": 65536, "operstate": "UNKNOWN",
     "link_type": "loopback", "address": "00:00:00:00:00:00",
     "addr_info": [
         {"family": "inet", "local": "127.0.0.1", "prefixlen": 8, "scope": "host", "label": "lo",
          "valid_life_time": 4294967295, "preferred_life_time": 4294967295},
         {"family": "inet6", "local": "::1", "prefixlen": 128, "scope": "host", "noprefixroute": True,
          "valid_life_time": 4294967295, "preferred_life_time": 4294967295}]},
    {"ifindex": 2, "ifname": "enp1s0", "flags": ["BROADCAST", "MULTICAST", "UP", "LOWER_UP"], "mtu": 1500,
     "operstate": "UP", "link_type": "ether", "address": "1e:2d:3e:4f:5a:6b",
     "addr_info": [
         {"family": "inet", "local": LAN_V4, "prefixlen": 24, "broadcast": "192.168.1.255", "scope": "global",
          "dynamic": True, "label": "enp1s0", "valid_life_time": 86000, "preferred_life_time": 86000},
         {"family": "inet6", "local": LAN_V6, "prefixlen": 64, "scope": "global", "dynamic": True,
          "valid_life_time": 86000, "preferred_life_time": 14000},
         {"family": "inet6", "local": "fe80::1c2d:3eff:fe4f:5a6b", "prefixlen": 64, "scope": "link",
          "valid_life_time": 4294967295, "preferred_life_time": 4294967295}]},
    {"ifindex": 3, "ifname": "wlan0", "flags": ["NO-CARRIER", "BROADCAST", "MULTICAST", "UP"], "operstate": "DOWN",
     "addr_info": []},
    {"ifindex": 4, "ifname": "tailscale0", "flags": ["POINTOPOINT", "MULTICAST", "NOARP", "UP", "LOWER_UP"],
     "mtu": 1280, "operstate": "UNKNOWN", "link_type": "none",
     "addr_info": [
         {"family": "inet", "local": VPN_V4, "prefixlen": 32, "scope": "global", "label": "tailscale0",
          "valid_life_time": 4294967295, "preferred_life_time": 4294967295}]},
])


class FakeRun:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def addrinfo(*ips):
    out = []
    for ip in ips:
        if ":" in ip:
            out.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, 0, 0, 0)))
        else:
            out.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)))
    return out


class LocalAddresses(unittest.TestCase):
    def fallback(self, answer):
        """Patch the resolver: `answer` is getaddrinfo's list, or an exception for it to raise."""
        kw = {"side_effect": answer} if isinstance(answer, BaseException) else {"return_value": answer}
        return mock.patch.object(ra.socket, "getaddrinfo", **kw)

    def test_parses_ip_json(self):
        run = FakeRun(IP_ADDR_JSON)
        with self.fallback(AssertionError("the resolver must not be asked")):
            self.assertEqual(ra.local_addresses(run), [VPN_V4, LAN_V4, LAN_V6])
        self.assertEqual(run.calls, [["ip", "-j", "addr", "show"]])

    def test_sorted_v4_first_and_deduplicated(self):
        data = [{"ifname": "a", "addr_info": [
            {"family": "inet6", "local": "2001:db8::2"}, {"family": "inet", "local": "10.0.0.9"},
            {"family": "inet6", "local": "2001:db8::1"}, {"family": "inet", "local": "10.0.0.10"},
            {"family": "inet", "local": "10.0.0.9"}, {"family": "inet", "local": "169.254.3.4"},
            {"family": "inet6", "local": "::ffff:10.0.0.11"}]}]
        self.assertEqual(ra.local_addresses(FakeRun(json.dumps(data))),
                         ["10.0.0.9", "10.0.0.10", "10.0.0.11", "2001:db8::1", "2001:db8::2"])

    def test_junk_entries_are_skipped(self):
        data = [None, 5, "eth0", {"ifname": "x"}, {"addr_info": "nope"},
                {"addr_info": [None, {}, {"family": "inet"}, {"family": "inet", "local": 5},
                               {"family": "inet", "local": "not-an-ip"}, {"family": "link", "local": "10.0.0.1"},
                               {"family": "inet", "local": "10.0.0.2"}]}]
        self.assertEqual(ra.local_addresses(FakeRun(json.dumps(data))), ["10.0.0.2"])

    def test_only_loopback_is_empty_without_fallback(self):
        data = [{"ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1"}]}]
        with self.fallback(addrinfo(LAN_V4)):
            self.assertEqual(ra.local_addresses(FakeRun(json.dumps(data))), [])

    def test_fallback_when_ip_fails(self):
        resolved = addrinfo("127.0.1.1", LAN_V4, LAN_V4, "fe80::1%enp1s0", LAN_V6, "::1")
        resolved.append((socket.AF_UNIX, socket.SOCK_STREAM, 0, "", "/run/x"))
        for answer in (OSError("ip: command not found"), "not json", "{}", "[1", json.dumps({"addr_info": []}),
                       b"\xff\xfe", RuntimeError("anything")):
            with self.fallback(resolved), mock.patch.object(ra.socket, "gethostname", return_value="streampc"):
                self.assertEqual(ra.local_addresses(FakeRun(answer)), [LAN_V4, LAN_V6], repr(answer))

    def test_fallback_resolves_the_hostname(self):
        with self.fallback(addrinfo(LAN_V4)) as gai, mock.patch.object(ra.socket, "gethostname", return_value="streampc"):
            ra.local_addresses(FakeRun(OSError("no ip")))
        gai.assert_called_once_with("streampc", None)

    def test_never_raises(self):
        for err in (socket.gaierror(-2, "Name or service not known"), OSError("x"), UnicodeError("x"),
                    RuntimeError("x")):
            with self.fallback(err):
                self.assertEqual(ra.local_addresses(FakeRun(OSError("no ip"))), [])
        with mock.patch.object(ra.socket, "gethostname", side_effect=OSError("x")):
            self.assertEqual(ra.local_addresses(FakeRun(OSError("no ip"))), [])
        with self.fallback([("junk",), None]):
            self.assertEqual(ra.local_addresses(FakeRun(OSError("no ip"))), [])

    def test_default_runner_without_ip_falls_back(self):
        with mock.patch.object(ra.shutil, "which", return_value=None), self.fallback(addrinfo(LAN_V4)), \
                mock.patch.object(ra.subprocess, "run", side_effect=AssertionError("nothing may run")):
            self.assertEqual(ra.local_addresses(), [LAN_V4])

    def test_default_runner(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append((argv, kw))
            return subprocess.CompletedProcess(argv, 0, stdout=IP_ADDR_JSON, stderr="")

        with mock.patch.object(ra.shutil, "which", return_value="/usr/sbin/ip"), \
                mock.patch.object(ra.subprocess, "run", side_effect=fake_run):
            self.assertEqual(ra.local_addresses(), [VPN_V4, LAN_V4, LAN_V6])
        (argv, kw), = calls
        self.assertEqual(argv, ["/usr/sbin/ip", "-j", "addr", "show"])
        self.assertNotIn("shell", kw)
        self.assertEqual(kw["stdin"], subprocess.DEVNULL)
        self.assertTrue(0 < kw["timeout"] <= 5)

    def test_default_runner_failures_fall_back(self):
        failures = [subprocess.CompletedProcess(["ip"], 1, stdout="", stderr="boom"),
                    subprocess.TimeoutExpired(["ip"], 2), OSError("exec format error"),
                    subprocess.SubprocessError("x")]
        for failure in failures:
            kw = {"side_effect": failure} if isinstance(failure, BaseException) else {"return_value": failure}
            with mock.patch.object(ra.shutil, "which", return_value="/usr/sbin/ip"), \
                    mock.patch.object(ra.subprocess, "run", **kw), self.fallback(addrinfo(LAN_V4)):
                self.assertEqual(ra.local_addresses(), [LAN_V4], repr(failure))


# ----- Host headers and URLs

ADDRS = [LAN_V4, VPN_V4, LAN_V6]
BASE = {"192.168.1.50:8767", "100.101.102.103:8767", "[2001:db8:1::50]:8767", "streampc:8767", "streampc.local:8767"}


class AllowedHosts(unittest.TestCase):
    def test_wildcards(self):
        for listen in ("0.0.0.0", "::", "[::]", "0:0::0"):
            self.assertEqual(ra.allowed_hosts(listen, 8767, ADDRS, "Streampc"), BASE, listen)

    def test_specific_listen_address_is_included(self):
        self.assertEqual(ra.allowed_hosts(LAN_V4, 8767, ADDRS, "streampc"), BASE)
        self.assertEqual(ra.allowed_hosts("10.0.0.5", 8767, ADDRS, "streampc"), BASE | {"10.0.0.5:8767"})
        self.assertEqual(ra.allowed_hosts("[2001:DB8:1::99]", 8767, ADDRS, "streampc"), BASE | {"[2001:db8:1::99]:8767"})
        self.assertEqual(ra.allowed_hosts("::ffff:10.0.0.5", 8767, [], ""), {"10.0.0.5:8767"})

    def test_junk_listen_adds_nothing(self):
        for listen in ("", "junk", None, "0.0.0.0:80", "*"):
            self.assertEqual(ra.allowed_hosts(listen, 8767, ADDRS, "streampc"), BASE, repr(listen))

    def test_extra_names(self):
        extra = ["Panel.Example.COM.", "192.168.1.60", "::ffff:10.0.0.7", "2001:db8::7", "", None, 5,
                 "*.example.com", "bad name", "x:1", "-bad.example", "a..b", "a" * 64 + ".com"]
        self.assertEqual(ra.allowed_hosts("0.0.0.0", 8767, ADDRS, "streampc", extra),
                         BASE | {"panel.example.com:8767", "192.168.1.60:8767", "10.0.0.7:8767", "[2001:db8::7]:8767"})
        # a single string is one name, not a list of letters
        self.assertEqual(ra.allowed_hosts("0.0.0.0", 8767, [], "", "panel.lan"), {"panel.lan:8767"})
        self.assertEqual(ra.allowed_hosts("0.0.0.0", 8767, [], "", None), set())

    def test_hostnames(self):
        self.assertEqual(ra.allowed_hosts("0.0.0.0", 8767, [], "Streampc.LAN."), {"streampc.lan:8767", "streampc.local:8767"})
        self.assertEqual(ra.allowed_hosts("0.0.0.0", 8767, [], "streampc.local"), {"streampc.local:8767"})
        for nothing in ("", None, "bad name", "*", 5):
            self.assertEqual(ra.allowed_hosts("0.0.0.0", 8767, [], nothing), set(), repr(nothing))

    def test_addresses_normalised_and_junk_skipped(self):
        got = ra.allowed_hosts("0.0.0.0", 80, ["::ffff:192.168.1.50", "2001:0DB8:1::50", "junk", None, 5, ""], "")
        self.assertEqual(got, {"192.168.1.50:80", "[2001:db8:1::50]:80"})
        self.assertEqual(ra.allowed_hosts("0.0.0.0", 8767, None, ""), set())

    def test_no_wildcards_and_all_lowercase(self):
        got = ra.allowed_hosts("0.0.0.0", 8767, ADDRS + ["2001:DB8::AB"], "STREAMPC", ["PANEL.LAN"])
        for h in got:
            self.assertEqual(h, h.lower())
            self.assertNotIn("*", h)
        self.assertNotIn("0.0.0.0:8767", got)
        self.assertNotIn("[::]:8767", got)

    def test_port(self):
        self.assertEqual(ra.allowed_hosts("0.0.0.0", "8767", [LAN_V4], ""), {"192.168.1.50:8767"})
        for bad in (0, -1, 65536, "x", "", None, True, 8767.5, [8767]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                ra.allowed_hosts("0.0.0.0", bad, ADDRS, "streampc")


class HostOk(unittest.TestCase):
    def setUp(self):
        self.allowed = ra.allowed_hosts("0.0.0.0", 8767, ADDRS, "streampc", ["streampc.example.com"])

    def test_accepted(self):
        for host in ("streampc:8767", "STREAMPC:8767", "streampc.local:8767", "Streampc.Local:8767", "streampc.local.:8767",
                     "192.168.1.50:8767", "100.101.102.103:8767", "[2001:db8:1::50]:8767", "[2001:DB8:1::50]:8767",
                     "streampc.example.com:8767", "streampc.example.com.:8767", "streampc.:8767"):
            self.assertTrue(ra.host_ok(host, self.allowed), host)

    def test_refused(self):
        for host in (None, "", 8767, b"streampc:8767", "streampc", "streampc.", "streampc:8766", "streampc:", ":8767",
                     "streampc..:8767", "evil.streampc:8767", "streampc.evil.com:8767", "*.local:8767", "local:8767",
                     "2001:db8:1::50:8767", "[2001:db8:1::50].:8767", "192.168.1.50", " streampc:8767",
                     "streampc:8767 ", "streampc:08767", "127.0.0.1:8767", "localhost:8767", "[::1]:8767",
                     "0.0.0.0:8767", "192.168.1.50:8767@evil.com"):  # privacy-check: allow
            self.assertFalse(ra.host_ok(host, self.allowed), repr(host))

    def test_ignores_case_of_allowed_too(self):
        self.assertTrue(ra.host_ok("streampc:1", {"Streampc:1"}))
        self.assertFalse(ra.host_ok("streampc:1", set()))
        self.assertFalse(ra.host_ok("streampc:1", {None, 5}))


class Urls(unittest.TestCase):
    def test_all_v4(self):
        self.assertEqual(ra.urls("0.0.0.0", 8767, ADDRS, "streampc"),
                         ["http://streampc.local:8767", "http://100.101.102.103:8767", "http://192.168.1.50:8767"])

    def test_all(self):
        self.assertEqual(ra.urls("::", 8767, ADDRS, "Streampc"),
                         ["http://streampc.local:8767", "http://100.101.102.103:8767", "http://192.168.1.50:8767",
                          "http://[2001:db8:1::50]:8767"])

    def test_specific(self):
        self.assertEqual(ra.urls(LAN_V4, 8767, ADDRS, "streampc"), ["http://streampc.local:8767", "http://192.168.1.50:8767"])
        self.assertEqual(ra.urls("[2001:db8:1::50]", 9000, ADDRS, "streampc"),
                         ["http://streampc.local:9000", "http://[2001:db8:1::50]:9000"])
        self.assertEqual(ra.urls("10.0.0.5", 8767, ADDRS, ""), ["http://10.0.0.5:8767"])

    def test_hostname_forms(self):
        self.assertEqual(ra.urls("0.0.0.0", 8767, [], "streampc.lan"), ["http://streampc.local:8767"])
        self.assertEqual(ra.urls("0.0.0.0", 8767, [], "STREAMPC.local."), ["http://streampc.local:8767"])
        for nothing in ("", None, "bad name"):
            self.assertEqual(ra.urls("0.0.0.0", 8767, [LAN_V4], nothing), ["http://192.168.1.50:8767"])

    def test_unreachable_and_junk_addresses_skipped(self):
        addrs = ["127.0.0.1", "::1", "fe80::1%eth0", "169.254.1.1", "junk", None, LAN_V4, "::ffff:192.168.1.50"]
        self.assertEqual(ra.urls("::", 8767, addrs, ""), ["http://192.168.1.50:8767"])

    def test_junk_listen(self):
        for listen in ("", "junk", None, "*", "0.0.0.0:8767"):
            self.assertEqual(ra.urls(listen, 8767, ADDRS, "streampc"), [], repr(listen))

    def test_bad_port(self):
        with self.assertRaises(ValueError):
            ra.urls("0.0.0.0", 0, ADDRS, "streampc")


# ----- device names


class CleanName(unittest.TestCase):
    def test_kept(self):
        for name in ("Zach's iPhone", "Pixel 8 Pro", "📱 Phone", "Büro-PC", "a"):
            self.assertEqual(ra.clean_name(name), name)

    def test_cleaned(self):
        cases = {
            "  phone  ": "phone",
            "a\x00b\nc\td\x7f\x1b[31m": "abcd[31m",
            "‮enohp": "enohp",            # a bidi override can't reorder what the approver sees
            "​ph‍one﻿": "phone",  # zero-width characters
            "a 　b": "a b",           # other spaces are plain spaces, runs collapsed
            "a     b": "a b",
            "line break": "linebreak",
            "\ud800bad": "bad",               # a lone surrogate (JSON can send one)
        }
        for raw, want in cases.items():
            self.assertEqual(ra.clean_name(raw), want, repr(raw))

    def test_length(self):
        self.assertEqual(ra.clean_name("x" * 100), "x" * 60)
        self.assertEqual(ra.clean_name("a" * 10_000), "a" * 60)
        got = ra.clean_name("a" * 59 + " b")
        self.assertEqual(got, "a" * 59)       # cut at 60, then trimmed
        self.assertLessEqual(len(ra.clean_name("é" * 61)), 60)

    def test_unnamed(self):
        for nothing in ("", "   ", "\n\t\x00", "​", None, 123, ["a"], b"phone", {"name": "x"}):
            self.assertEqual(ra.clean_name(nothing), "Unnamed device", repr(nothing))


# ----- failure limits


class PeerKey(unittest.TestCase):
    def test_ipv4_is_its_address(self):
        for raw, want in ((PHONE, PHONE), ("::ffff:" + PHONE, PHONE), ("[::ffff:192.168.1.23]", PHONE),
                          ("10.0.0.1", "10.0.0.1")):
            self.assertEqual(ra.peer_key(raw), want, raw)
        self.assertNotEqual(ra.peer_key(PHONE), ra.peer_key(LAPTOP))   # no IPv4 prefix is shared

    def test_ipv6_is_its_64(self):
        self.assertEqual(ra.peer_key(LAN_V6), "2001:db8:1::/64")
        for same in ("2001:db8:1::1", "2001:db8:1:0:ffff:ffff:ffff:ffff", "[2001:DB8:1::abcd]", "2001:db8:1::5%eth0"):
            self.assertEqual(ra.peer_key(same), ra.peer_key(LAN_V6), same)
        for other in ("2001:db8:1:1::50", "2001:db8:2::50", "2001:db9:1::50", "fe80::50"):
            self.assertNotEqual(ra.peer_key(other), ra.peer_key(LAN_V6), other)

    def test_junk_is_itself_clipped(self):
        self.assertEqual(ra.peer_key("junk"), "junk")
        self.assertEqual(ra.peer_key(None), "None")
        self.assertEqual(len(ra.peer_key("x" * 1000)), 64)


class PeerLimitTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(1000.0)

    def test_per_peer(self):
        lim = ra.PeerLimit(per_peer=3, window=60, global_limit=100, clock=self.clock)
        for _ in range(2):
            lim.fail(PHONE)
        self.assertFalse(lim.blocked(PHONE))
        lim.fail(PHONE)
        self.assertTrue(lim.blocked(PHONE))
        self.assertFalse(lim.blocked(LAPTOP))
        self.clock.advance(60)
        self.assertFalse(lim.blocked(PHONE))

    def test_sliding_window(self):
        lim = ra.PeerLimit(per_peer=2, window=60, global_limit=100, clock=self.clock)
        t0 = self.clock()
        for offset in (0, 10, 50):
            self.clock.t = t0 + offset
            lim.fail(PHONE)
        self.clock.t = t0 + 65   # failures at 10 and 50 are inside the window
        self.assertTrue(lim.blocked(PHONE))
        self.clock.t = t0 + 71   # only 50 is
        self.assertFalse(lim.blocked(PHONE))

    def test_global(self):
        lim = ra.PeerLimit(per_peer=10, window=60, global_limit=5, clock=self.clock)
        for i in range(4):
            lim.fail(f"10.0.0.{i}")
        self.assertFalse(lim.blocked(PHONE))
        lim.fail("10.0.0.9")
        for ip in (PHONE, "10.0.0.0", LAN_V6):
            self.assertTrue(lim.blocked(ip), ip)
        self.clock.advance(60)
        self.assertFalse(lim.blocked(PHONE))

    def test_spellings_of_one_address_share_a_count(self):
        lim = ra.PeerLimit(per_peer=3, clock=self.clock)
        lim.fail("::ffff:" + PHONE)
        lim.fail("[::ffff:" + PHONE + "]")
        lim.fail(PHONE)
        self.assertTrue(lim.blocked(PHONE))
        self.assertTrue(lim.blocked("::FFFF:" + PHONE))

    def test_an_ipv6_64_is_one_peer(self):
        lim = ra.PeerLimit(per_peer=10, global_limit=100, clock=self.clock)
        for i in range(10):   # every failure from a different address of one /64
            self.assertFalse(lim.blocked(f"2001:db8:1::{i + 1:x}"))
            lim.fail(f"2001:db8:1::{i + 1:x}")
        for same in (LAN_V6, "2001:db8:1::ffff", "2001:db8:1:0:1:2:3:4"):
            self.assertTrue(lim.blocked(same), same)
        for other in ("2001:db8:1:1::1", "2001:db8:2::1", PHONE):
            self.assertFalse(lim.blocked(other), other)
        self.assertEqual(list(lim._peers), ["2001:db8:1::/64"])

    def test_ipv4_neighbours_are_separate_peers(self):
        lim = ra.PeerLimit(per_peer=2, global_limit=100, clock=self.clock)
        for i in range(20):
            lim.fail(f"192.168.1.{i}")
        self.assertFalse(lim.blocked("192.168.1.0"))
        self.assertFalse(lim.blocked(PHONE))

    def test_junk_addresses_are_counted_not_crashing(self):
        lim = ra.PeerLimit(per_peer=2, clock=self.clock)
        lim.fail(None)
        lim.fail(None)
        self.assertTrue(lim.blocked(None))
        self.assertFalse(lim.blocked(PHONE))

    def test_table_is_bounded(self):
        lim = ra.PeerLimit(per_peer=10, global_limit=10_000, clock=self.clock)
        with mock.patch.object(ra, "_TABLE_MAX", 4):
            for i in range(10):
                lim.fail(f"10.0.0.{i}")
            self.assertLessEqual(len(lim._peers), 4)
            self.assertIn("10.0.0.9", lim._peers)
        # and peers whose failures are all out of the window are dropped as others fail
        self.clock.advance(60)
        lim.fail(PHONE)
        self.assertEqual(list(lim._peers), [PHONE])

    def test_thread_safe(self):
        lim = ra.PeerLimit(per_peer=10_000, global_limit=10_000, clock=self.clock)

        def work(n):
            for _ in range(200):
                lim.fail(f"10.0.{n}.1")
                lim.blocked(f"10.0.{n}.1")

        threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(lim._all), 1600)


# ----- pairing


class Refusal(unittest.TestCase):
    def test_fields(self):
        e = ra.Refused(429, "slow down")
        self.assertEqual((e.status, e.message, str(e)), (429, "slow down", "slow down"))


class PairingBase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.path = store_path()
        self.devices = ra.Devices(path=self.path, clock=self.clock)
        self.pairing = ra.Pairing(clock=self.clock)

    def refused(self, status, fn, *args):
        with self.assertRaises(ra.Refused) as cm:
            fn(*args)
        self.assertEqual(cm.exception.status, status)
        return cm.exception


class PairingStart(PairingBase):
    def test_shape(self):
        r = self.pairing.start(PHONE, "Phone")
        self.assertEqual(set(r), {"request", "code", "secret", "expires"})
        self.assertRegex(r["code"], r"\A\d{6}\Z")
        self.assertEqual(r["expires"], T0 + ra.PAIR_TTL)
        self.assertIsInstance(r["request"], str)
        self.assertGreaterEqual(len(r["request"]), 12)
        self.assertGreaterEqual(len(r["secret"]), 32)

    def test_pending_lists_it_without_the_secret(self):
        r = self.pairing.start("::ffff:" + PHONE, "  Zach's\nphone ")
        self.assertEqual(self.pairing.pending(), [
            {"id": r["request"], "name": "Zach'sphone", "ip": PHONE, "code": r["code"], "expires": T0 + 300}])
        self.assertNotIn(r["secret"], repr(self.pairing.pending()))
        self.assertNotIn(r["secret"], repr(vars(self.pairing)))   # only its hash is kept

    def test_pending_oldest_first(self):
        a = self.pairing.start(PHONE, "a")
        b = self.pairing.start(LAPTOP, "b")          # same instant: order of arrival
        self.clock.advance(1)
        c = self.pairing.start("192.168.1.25", "c")
        self.assertEqual([p["id"] for p in self.pairing.pending()], [a["request"], b["request"], c["request"]])

    def test_per_ip_limit(self):
        for _ in range(ra.MAX_PENDING_PER_IP):
            self.pairing.start(PHONE, "phone")
        self.refused(429, self.pairing.start, PHONE, "phone")
        self.refused(429, self.pairing.start, "::ffff:" + PHONE, "phone")   # the same address
        self.pairing.start(LAPTOP, "laptop")

    def test_per_ip_limit_counts_an_ipv6_64_as_one(self):
        with mock.patch.object(ra, "MAX_STARTS_PER_PEER", 100):   # (only the waiting requests here)
            for i in range(ra.MAX_PENDING_PER_IP):
                self.pairing.start(f"2001:db8:1::{i + 1:x}", "phone")
            e = self.refused(429, self.pairing.start, "2001:db8:1::ffff", "phone")
            self.assertIn("already has requests waiting", e.message)
            self.pairing.start("2001:db8:1:2::1", "another network")

    def test_start_limit(self):
        def deny_all():
            for p in self.pairing.pending():
                self.pairing.decide(p["id"], False, self.devices)

        for _ in range(ra.MAX_STARTS_PER_PEER):
            self.pairing.start(PHONE, "phone")
            deny_all()   # answered at once: no request is left waiting
        self.assertEqual(self.pairing.pending(), [])
        e = self.refused(429, self.pairing.start, PHONE, "phone")
        self.assertEqual(e.message, "Too many requests from this device; wait a few minutes")
        self.refused(429, self.pairing.start, "::ffff:" + PHONE, "phone")
        self.pairing.start(LAPTOP, "laptop")   # another device isn't affected
        self.clock.advance(ra.START_WINDOW - 1)
        self.refused(429, self.pairing.start, PHONE, "phone")
        self.clock.advance(1)                  # the first start is out of the window
        self.pairing.start(PHONE, "phone")

    def test_start_limit_counts_an_ipv6_64_as_one(self):
        for i in range(ra.MAX_STARTS_PER_PEER):
            self.pairing.start(f"2001:db8:1::{i + 1:x}", "x")
            for p in self.pairing.pending():
                self.pairing.decide(p["id"], False, self.devices)
        e = self.refused(429, self.pairing.start, "2001:db8:1::abcd", "x")
        self.assertEqual(e.message, "Too many requests from this device; wait a few minutes")
        self.pairing.start("2001:db8:1:1::1", "x")

    def test_only_started_requests_count_against_the_start_limit(self):
        for i in range(ra.MAX_PENDING):
            self.pairing.start(f"10.0.0.{i}", "d")
        for _ in range(5):   # refused (everyone is waiting): they don't use up PHONE's starts
            self.refused(429, self.pairing.start, PHONE, "phone")
        for p in self.pairing.pending():
            self.pairing.decide(p["id"], False, self.devices)
        for _ in range(ra.MAX_STARTS_PER_PEER):
            self.pairing.start(PHONE, "phone")

    def test_start_limit_forgets_starts_when_the_clock_steps_back(self):
        for _ in range(ra.MAX_STARTS_PER_PEER):
            self.pairing.start(PHONE, "phone")
            for p in self.pairing.pending():
                self.pairing.decide(p["id"], False, self.devices)
        self.refused(429, self.pairing.start, PHONE, "phone")
        self.clock.advance(-3600)   # rather than refusing the device for an hour and five minutes
        self.pairing.start(PHONE, "phone")

    def test_global_limit(self):
        for i in range(ra.MAX_PENDING):
            self.pairing.start(f"10.0.0.{i}", "d")
        e = self.refused(429, self.pairing.start, "10.0.1.1", "d")
        self.assertNotIn("10.0.", e.message)

    @mock.patch.object(ra, "MAX_STARTS_PER_PEER", 100)   # (only the waiting requests' limit here)
    def test_expired_and_answered_requests_free_their_slots(self):
        reqs = [self.pairing.start(PHONE, "phone") for _ in range(ra.MAX_PENDING_PER_IP)]
        self.pairing.decide(reqs[0]["request"], False, self.devices)
        self.pairing.start(PHONE, "phone")
        self.refused(429, self.pairing.start, PHONE, "phone")
        self.clock.advance(ra.PAIR_TTL)
        for _ in range(ra.MAX_PENDING_PER_IP):
            self.pairing.start(PHONE, "phone")

    def test_unique_codes(self):
        first = self.pairing.start(PHONE, "a")["code"]
        n = int(first)
        with mock.patch.object(ra.secrets, "randbelow", side_effect=[n, n, n, (n + 1) % 10 ** 6]) as rb:
            second = self.pairing.start(LAPTOP, "b")["code"]
        self.assertNotEqual(first, second)
        self.assertEqual(second, f"{(n + 1) % 10 ** 6:06d}")
        self.assertEqual(rb.call_count, 4)
        rb.assert_called_with(10 ** 6)

    def test_codes_keep_leading_zeros(self):
        with mock.patch.object(ra.secrets, "randbelow", return_value=42):
            self.assertEqual(self.pairing.start(PHONE, "a")["code"], "000042")

    def test_loopback_and_junk_addresses_refused(self):
        for ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "0.0.0.0", "junk", "", None):
            self.refused(403, self.pairing.start, ip, "x")
        self.assertEqual(self.pairing.pending(), [])

    def test_ttl(self):
        p = ra.Pairing(clock=self.clock, ttl=10)
        self.assertEqual(p.start(PHONE, "x")["expires"], T0 + 10)
        for bad in (0, -1, "300", None, True, float("nan"), float("inf")):
            with self.assertRaises(ValueError, msg=repr(bad)):
                ra.Pairing(ttl=bad)


class PairingPoll(PairingBase):
    def test_pending(self):
        r = self.pairing.start(PHONE, "phone")
        for _ in range(3):
            self.assertEqual(self.pairing.poll(r["request"], r["secret"]), {"state": "pending"})

    def test_approved_token_is_delivered_to_every_poll_until_the_approval_is_ttl_old(self):
        # (idempotent: a poll whose answer was lost can be repeated; see the module docstring)
        r = self.pairing.start(PHONE, "Phone")
        self.clock.advance(10)
        device = self.pairing.decide(r["request"], True, self.devices)
        self.assertEqual(set(device), PUBLIC_KEYS)
        self.assertEqual((device["name"], device["ip"]), ("Phone", PHONE))
        self.assertEqual(self.pairing.pending(), [])
        got = self.pairing.poll(r["request"], r["secret"])
        self.assertEqual(set(got), {"state", "token"})
        self.assertEqual(got["state"], "approved")
        self.assertEqual(self.devices.check(got["token"], PHONE)["id"], device["id"])
        for _ in range(3):
            self.assertEqual(self.pairing.poll(r["request"], r["secret"]), got)
        self.clock.advance(ra.PAIR_TTL - 1)   # still the same answer, a second before it's forgotten
        self.assertEqual(self.pairing.poll(r["request"], r["secret"]), got)
        self.refused(404, self.pairing.poll, r["request"], "wrong-secret")
        self.clock.advance(1)
        self.refused(404, self.pairing.poll, r["request"], r["secret"])
        self.assertEqual(self.pairing._requests, {})

    def test_a_collected_approval_keeps_its_device_when_forgotten(self):
        r = self.pairing.start(PHONE, "Phone")
        device = self.pairing.decide(r["request"], True, self.devices)
        token = self.pairing.poll(r["request"], r["secret"])["token"]
        held = self.pairing._requests[r["request"]]
        self.clock.advance(ra.PAIR_TTL)
        with self.assertNoLogs("deck", "INFO"):
            self.assertEqual(self.pairing.pending(), [])
        self.assertIsNone(held.token)          # the forgotten request lets go of the token
        self.assertEqual([d["id"] for d in self.devices.list()], [device["id"]])
        self.assertEqual(self.devices.check(token)["id"], device["id"])
        self.assertEqual([d["id"] for d in ra.Devices(path=self.path, clock=self.clock).list()], [device["id"]])

    def test_a_lost_answer_is_collected_by_the_next_poll(self):
        r = self.pairing.start(PHONE, "Phone")
        self.pairing.decide(r["request"], True, self.devices)
        lost = self.pairing.poll(r["request"], r["secret"])   # its answer never reached the device
        self.clock.advance(30)
        again = self.pairing.poll(r["request"], r["secret"])
        self.assertEqual(again, lost)
        self.assertIsNotNone(self.devices.check(again["token"]))

    def test_wrong_secret_or_id(self):
        r = self.pairing.start(PHONE, "phone")
        other = self.pairing.start(LAPTOP, "laptop")
        for rid, secret in ((r["request"], other["secret"]), (r["request"], r["secret"] + "x"),
                            (r["request"], r["secret"][:-1]), (r["request"], ""), (r["request"], None),
                            (r["request"], "é" * 10), (r["request"], "\ud800"), (r["request"], "a" * 10_000),
                            ("nope", r["secret"]), ("", r["secret"]), (None, r["secret"]), (5, r["secret"]),
                            ("x" * 1000, r["secret"])):
            e = self.refused(404, self.pairing.poll, rid, secret)
            self.assertEqual(e.message, "No such request")
        # wrong guesses consume nothing
        self.pairing.decide(r["request"], True, self.devices)
        self.assertEqual(self.pairing.poll(r["request"], r["secret"])["state"], "approved")

    def test_secret_compared_in_constant_time(self):
        r = self.pairing.start(PHONE, "phone")
        with mock.patch.object(ra.hmac, "compare_digest", wraps=ra.hmac.compare_digest) as cd:
            self.refused(404, self.pairing.poll, r["request"], "wrong-secret")
        self.assertEqual(cd.call_count, 1)

    def test_denied(self):
        r = self.pairing.start(PHONE, "phone")
        self.assertIsNone(self.pairing.decide(r["request"], False, self.devices))
        self.assertEqual(self.pairing.pending(), [])
        self.assertEqual(self.pairing.poll(r["request"], r["secret"]), {"state": "denied"})
        self.refused(404, self.pairing.poll, r["request"], r["secret"])
        self.assertEqual(self.devices.list(), [])

    def test_expired(self):
        r = self.pairing.start(PHONE, "phone")
        self.clock.advance(ra.PAIR_TTL - 1)
        self.assertEqual(self.pairing.poll(r["request"], r["secret"]), {"state": "pending"})
        self.clock.advance(1)
        self.assertEqual(self.pairing.pending(), [])
        self.assertEqual(self.pairing.poll(r["request"], r["secret"]), {"state": "expired"})
        self.refused(404, self.pairing.poll, r["request"], r["secret"])

    def test_outcomes_nobody_polls_for_are_forgotten(self):
        denied = self.pairing.start(PHONE, "a")
        expired = self.pairing.start(LAPTOP, "b")
        self.pairing.decide(denied["request"], False, self.devices)
        self.clock.advance(ra.PAIR_TTL)        # `expired` has just expired; `denied` was answered ttl ago
        self.refused(404, self.pairing.poll, denied["request"], denied["secret"])
        self.assertEqual(self.pairing.poll(expired["request"], expired["secret"]), {"state": "expired"})
        late = self.pairing.start(PHONE, "c")
        self.clock.advance(2 * ra.PAIR_TTL)
        self.refused(404, self.pairing.poll, late["request"], late["secret"])
        self.assertEqual(self.pairing._requests, {})

    def test_approval_collected_late_but_in_time(self):
        r = self.pairing.start(PHONE, "phone")
        self.clock.advance(ra.PAIR_TTL - 1)
        self.pairing.decide(r["request"], True, self.devices)
        self.clock.advance(ra.PAIR_TTL - 1)    # well past the request's own expiry
        self.assertEqual(self.pairing.poll(r["request"], r["secret"])["state"], "approved")

    def test_uncollected_approval_revokes_its_device(self):
        r = self.pairing.start(PHONE, "phone")
        device = self.pairing.decide(r["request"], True, self.devices)
        self.assertEqual([d["id"] for d in self.devices.list()], [device["id"]])
        self.refused(404, self.pairing.poll, r["request"], "wrong-secret")   # a wrong secret collects nothing
        self.clock.advance(ra.PAIR_TTL)
        with self.assertLogs("deck", "INFO") as logs:
            self.assertEqual(self.pairing.pending(), [])
        self.assertIn("wasn't collected; device removed", "\n".join(logs.output))
        self.assertEqual(self.devices.list(), [])
        self.refused(404, self.pairing.poll, r["request"], r["secret"])
        self.assertEqual(ra.Devices(path=self.path, clock=self.clock).list(), [])


class PairingDecide(PairingBase):
    def test_unknown(self):
        for rid in ("nope", "", None, 5):
            self.refused(404, self.pairing.decide, rid, True, self.devices)
        self.assertEqual(self.devices.list(), [])

    def test_expired(self):
        r = self.pairing.start(PHONE, "phone")
        self.clock.advance(ra.PAIR_TTL)
        self.refused(404, self.pairing.decide, r["request"], True, self.devices)
        self.refused(404, self.pairing.decide, r["request"], False, self.devices)
        self.assertEqual(self.devices.list(), [])
        self.assertEqual(self.pairing.poll(r["request"], r["secret"]), {"state": "expired"})

    def test_answered_once(self):
        r = self.pairing.start(PHONE, "phone")
        self.pairing.decide(r["request"], True, self.devices)
        self.refused(404, self.pairing.decide, r["request"], True, self.devices)
        self.refused(404, self.pairing.decide, r["request"], False, self.devices)
        self.assertEqual(len(self.devices.list()), 1)
        d = self.pairing.start(LAPTOP, "laptop")
        self.pairing.decide(d["request"], False, self.devices)
        self.refused(404, self.pairing.decide, d["request"], True, self.devices)
        self.assertEqual(len(self.devices.list()), 1)
        self.assertEqual(self.pairing.poll(d["request"], d["secret"]), {"state": "denied"})

    def test_allow_must_be_a_bool(self):
        r = self.pairing.start(PHONE, "phone")
        for bad in ("false", "true", 1, 0, None, [True]):
            with self.assertRaises(TypeError, msg=repr(bad)):
                self.pairing.decide(r["request"], bad, self.devices)
        self.assertEqual(len(self.pairing.pending()), 1)
        self.assertEqual(self.devices.list(), [])

    def test_failed_add_leaves_the_request_pending(self):
        r = self.pairing.start(PHONE, "phone")
        with mock.patch.object(ra, "write_atomic", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                self.pairing.decide(r["request"], True, self.devices)
        self.assertEqual(self.devices.list(), [])
        self.assertEqual(self.pairing.poll(r["request"], r["secret"]), {"state": "pending"})
        self.assertIsNotNone(self.pairing.decide(r["request"], True, self.devices))
        self.assertEqual(self.pairing.poll(r["request"], r["secret"])["state"], "approved")

    def test_device_gets_the_requests_name_and_address(self):
        r = self.pairing.start("::ffff:" + PHONE, "‮My phone")
        device = self.pairing.decide(r["request"], True, self.devices)
        self.assertEqual((device["name"], device["ip"]), ("My phone", PHONE))


class PairingThreads(PairingBase):
    def run_threads(self, n, fn):
        barrier = threading.Barrier(n)
        results, errors = [None] * n, []

        def work(i):
            barrier.wait()
            try:
                results[i] = ("ok", fn(i))
            except ra.Refused as e:
                results[i] = ("refused", e.status)
            except Exception as e:   # anything else is a bug
                errors.append(e)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(errors, [])
        return results

    def test_concurrent_starts_respect_the_limit_and_get_unique_codes(self):
        results = self.run_threads(16, lambda i: self.pairing.start(f"10.0.0.{i}", f"d{i}"))
        ok = [r for kind, r in results if kind == "ok"]
        self.assertEqual(len(ok), ra.MAX_PENDING)
        self.assertEqual(sorted(r for kind, r in results if kind == "refused"), [429] * (16 - ra.MAX_PENDING))
        self.assertEqual(len({r["code"] for r in ok}), ra.MAX_PENDING)
        self.assertEqual(len({r["request"] for r in ok}), ra.MAX_PENDING)

    def test_concurrent_approvals_add_one_device(self):
        r = self.pairing.start(PHONE, "phone")
        results = self.run_threads(8, lambda i: self.pairing.decide(r["request"], i % 2 == 0, self.devices))
        self.assertEqual(sum(1 for kind, _ in results if kind == "ok"), 1)
        self.assertEqual(sum(1 for kind, s in results if kind == "refused" and s == 404), 7)
        self.assertLessEqual(len(self.devices.list()), 1)

    def test_concurrent_polls_all_get_the_same_token(self):
        r = self.pairing.start(PHONE, "phone")
        self.pairing.decide(r["request"], True, self.devices)
        results = self.run_threads(8, lambda i: self.pairing.poll(r["request"], r["secret"]))
        self.assertEqual({kind for kind, _ in results}, {"ok"})
        tokens = {res["token"] for _, res in results}
        self.assertEqual(len(tokens), 1)
        self.assertIsNotNone(self.devices.check(tokens.pop()))
        self.assertEqual(len(self.devices.list()), 1)

    def test_concurrent_starts_from_one_peer_respect_the_start_limit(self):
        def start_and_deny(i):
            out = self.pairing.start(f"2001:db8:1::{i + 1:x}", "x")
            self.pairing.decide(out["request"], False, self.devices)
            return out
        results = self.run_threads(12, start_and_deny)
        self.assertEqual(sum(1 for kind, _ in results if kind == "ok"), ra.MAX_STARTS_PER_PEER)
        self.assertEqual(sorted(s for kind, s in results if kind == "refused"), [429] * (12 - ra.MAX_STARTS_PER_PEER))

    def test_mixed_load(self):
        def work(i):
            req = self.pairing.start(f"10.0.1.{i}", "x")
            for _ in range(20):
                self.pairing.poll(req["request"], req["secret"])
                self.pairing.pending()
            self.pairing.decide(req["request"], i % 2 == 0, self.devices)
            return self.pairing.poll(req["request"], req["secret"])["state"]

        results = self.run_threads(ra.MAX_PENDING, work)
        states = sorted(r for _, r in results)
        self.assertEqual(states, ["approved"] * 5 + ["denied"] * 5)
        self.assertEqual(len(self.devices.list()), 5)


# ----- approved devices


def entry(token, **over):
    row = {"id": "dev1", "name": "Phone", "ip": PHONE, "token_sha256": sha(token), "created": T0,
           "last_seen": T0, "last_ip": PHONE, "expires": T0 + 86400}
    row.update(over)
    return row


def write_store(path, rows, version=1):
    with open(path, "w") as f:
        json.dump({"version": version, "devices": rows}, f)


class DevicesBase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.path = store_path()
        self.devices = ra.Devices(path=self.path, clock=self.clock)

    def on_disk(self):
        with open(self.path) as f:
            return json.load(f)

    def disk_text(self):
        with open(self.path) as f:
            return f.read()


class DevicesBasics(DevicesBase):
    def test_add_and_check(self):
        device, token = self.devices.add("Phone", PHONE)
        self.assertEqual(set(device), PUBLIC_KEYS)
        self.assertEqual(device, {"id": device["id"], "name": "Phone", "ip": PHONE, "created": T0,
                                  "last_seen": T0, "expires": T0 + ra.DEVICE_DAYS * 86400})
        self.assertIsInstance(token, str)
        self.assertGreaterEqual(len(token), 43)
        self.assertEqual(self.devices.check(token, PHONE)["id"], device["id"])
        other_device, other = self.devices.add("Laptop", LAPTOP)
        self.assertNotEqual(token, other)
        self.assertNotEqual(device["id"], other_device["id"])
        self.assertEqual(self.devices.check(other)["id"], other_device["id"])

    def test_bad_tokens(self):
        _, token = self.devices.add("Phone", PHONE)
        for bad in ("", None, 5, b"x", token + "x", token[:-1], token.upper(), " " + token, "a" * 10_000,
                    "é" * 43, "\ud800", sha(token), [token]):
            self.assertIsNone(self.devices.check(bad), repr(bad)[:40])
        self.assertIsNone(self.devices.check(""))

    def test_dicts_never_carry_a_token_or_its_hash(self):
        device, token = self.devices.add("Phone", PHONE)
        for d in (device, self.devices.check(token, PHONE), *self.devices.list()):
            self.assertEqual(set(d), PUBLIC_KEYS)
            self.assertNotIn(token, repr(d))
            self.assertNotIn(sha(token), repr(d))

    def test_add_validates(self):
        for bad in ("junk", "", None, "192.168.1.5:80"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.devices.add("x", bad)
        device, _ = self.devices.add("  ‮Phone\n", "::ffff:" + PHONE)
        self.assertEqual((device["name"], device["ip"]), ("Phone", PHONE))
        self.assertEqual(self.devices.add(None, PHONE)[0]["name"], "Unnamed device")

    def test_list_newest_first(self):
        ids = []
        for i in range(3):
            ids.append(self.devices.add(f"d{i}", PHONE)[0]["id"])
            self.clock.advance(10)
        self.assertEqual([d["id"] for d in self.devices.list()], ids[::-1])

    def test_default_path(self):
        d = ra.Devices(clock=self.clock)
        self.assertEqual(d.path, os.path.join(os.environ["DECK_STATE_DIR"], "remote_devices.json"))


class DevicesDisk(DevicesBase):
    def test_only_hashes_on_disk(self):
        _, token = self.devices.add("Phone", PHONE)
        _, token2 = self.devices.add("Laptop", LAPTOP)
        text = self.disk_text()
        for t in (token, token2):
            self.assertNotIn(t, text)
            self.assertIn(sha(t), text)
        data = self.on_disk()
        self.assertEqual(data["version"], 1)
        self.assertEqual({k for row in data["devices"] for k in row},
                         {"id", "name", "ip", "token_sha256", "created", "last_seen", "last_ip", "expires"})

    def test_file_is_private(self):
        self.devices.add("Phone", PHONE)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        os.chmod(self.path, 0o644)
        self.devices.add("Laptop", LAPTOP)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual([n for n in os.listdir(os.path.dirname(self.path)) if n.endswith(".tmp")], [])

    def test_missing_file_is_empty_and_not_created(self):
        self.assertEqual(self.devices.list(), [])
        self.assertFalse(os.path.exists(self.path))

    def test_persists_across_instances(self):
        device, token = self.devices.add("Phone", PHONE)
        again = ra.Devices(path=self.path, clock=self.clock)
        self.assertEqual(again.list(), [device])
        self.assertEqual(again.check(token, PHONE)["id"], device["id"])
        self.devices.revoke(device["id"])
        self.assertIsNone(ra.Devices(path=self.path, clock=self.clock).check(token))

    def test_failed_add_changes_nothing(self):
        kept, kept_token = self.devices.add("Phone", PHONE)
        before = self.disk_text()
        with mock.patch.object(ra, "write_atomic", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                self.devices.add("Laptop", LAPTOP)
        self.assertEqual(self.devices.list(), [kept])
        self.assertEqual(self.disk_text(), before)
        self.assertIsNotNone(self.devices.check(kept_token))

    def test_failed_add_at_the_cap_keeps_the_device_it_would_have_dropped(self):
        tokens = []
        for i in range(ra.MAX_DEVICES):
            tokens.append(self.devices.add(f"d{i}", PHONE)[1])
            self.clock.advance(1)
        with mock.patch.object(ra, "write_atomic", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.devices.add("new", PHONE)
        self.assertEqual(len(self.devices.list()), ra.MAX_DEVICES)
        self.assertIsNotNone(self.devices.check(tokens[0]))


class DevicesExpiry(DevicesBase):
    def test_expiry(self):
        self.devices.days = 1
        device, token = self.devices.add("Phone", PHONE)
        self.assertEqual(device["expires"], T0 + 86400)
        self.clock.advance(86400 - 1)
        self.assertIsNotNone(self.devices.check(token))
        self.clock.advance(1)
        self.assertIsNone(self.devices.check(token))
        self.assertEqual(self.devices.list(), [])
        self.assertEqual(ra.Devices(path=self.path, clock=self.clock).list(), [])

    def test_expired_dropped_on_add_and_load(self):
        self.devices.days = 1
        old, old_token = self.devices.add("old", PHONE)
        self.clock.advance(86400)
        self.devices.days = 30
        self.devices.add("new", LAPTOP)
        self.assertNotIn(old["id"], self.disk_text())
        write_store(self.path, [entry(old_token, expires=self.clock() - 1)])
        with self.assertNoLogs("deck", "WARNING"):
            again = ra.Devices(path=self.path, clock=self.clock)
        self.assertEqual(again.list(), [])
        self.assertIsNone(again.check(old_token))

    def test_days_applies_to_devices_added_afterwards(self):
        a, _ = self.devices.add("a", PHONE)
        self.devices.days = 2.5
        b, _ = self.devices.add("b", PHONE)
        self.assertEqual(a["expires"], T0 + 30 * 86400)
        self.assertEqual(b["expires"], T0 + 2.5 * 86400)
        self.assertEqual(self.devices.days, 2.5)
        self.assertEqual({d["id"]: d["expires"] for d in self.devices.list()}[a["id"]], T0 + 30 * 86400)

    def test_days_validation(self):
        for bad in (0, -1, "1", None, True, float("nan"), float("inf")):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.devices.days = bad
            with self.assertRaises(ValueError, msg=repr(bad)):
                ra.Devices(path=store_path(), days=bad)
        self.assertEqual(ra.Devices(path=store_path(), days=7).days, 7.0)


class DevicesRevoke(DevicesBase):
    def test_revoke(self):
        a, ta = self.devices.add("a", PHONE)
        b, tb = self.devices.add("b", LAPTOP)
        self.assertTrue(self.devices.revoke(a["id"]))
        self.assertIsNone(self.devices.check(ta))
        self.assertIsNotNone(self.devices.check(tb))
        self.assertFalse(self.devices.revoke(a["id"]))
        for bad in ("nope", "", None, 5, [a["id"]]):
            self.assertFalse(self.devices.revoke(bad))
        self.assertEqual([d["id"] for d in ra.Devices(path=self.path, clock=self.clock).list()], [b["id"]])

    def test_revoke_all(self):
        tokens = [self.devices.add(f"d{i}", PHONE)[1] for i in range(3)]
        self.devices.revoke_all()
        self.assertEqual(self.devices.list(), [])
        for t in tokens:
            self.assertIsNone(self.devices.check(t))
        self.assertEqual(self.on_disk()["devices"], [])
        self.assertEqual(ra.Devices(path=self.path, clock=self.clock).list(), [])

    def test_revoke_all_replaces_a_corrupt_file(self):
        with open(self.path, "w") as f:
            f.write("{garbage")
        with self.assertLogs("deck", "WARNING"):
            d = ra.Devices(path=self.path, clock=self.clock)
        d.revoke_all()
        self.assertEqual(self.on_disk(), {"version": 1, "devices": []})

    def test_revoke_takes_effect_even_if_the_file_cant_be_written(self):
        a, token = self.devices.add("a", PHONE)
        with mock.patch.object(ra, "write_atomic", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.devices.revoke(a["id"])
            self.assertIsNone(self.devices.check(token))
            with self.assertRaises(OSError):
                self.devices.revoke_all()
        self.assertEqual(self.devices.list(), [])


class DevicesCap(DevicesBase):
    def test_least_recently_seen_is_dropped(self):
        added = []
        for i in range(ra.MAX_DEVICES):
            added.append(self.devices.add(f"d{i}", PHONE))
            self.clock.advance(1)
        # the two oldest are used again; d2 is now the least recently seen
        self.assertIsNotNone(self.devices.check(added[0][1]))
        self.assertIsNotNone(self.devices.check(added[1][1]))
        self.clock.advance(1)
        new, new_token = self.devices.add("new", PHONE)
        ids = {d["id"] for d in self.devices.list()}
        self.assertEqual(len(ids), ra.MAX_DEVICES)
        self.assertNotIn(added[2][0]["id"], ids)
        self.assertIsNone(self.devices.check(added[2][1]))
        for kept in (0, 1, 3, ra.MAX_DEVICES - 1):
            self.assertIn(added[kept][0]["id"], ids)
        self.assertIsNotNone(self.devices.check(new_token))
        self.assertEqual(len(self.on_disk()["devices"]), ra.MAX_DEVICES)

    def test_load_keeps_the_most_recently_seen(self):
        tokens = [f"token-{i:02d}-" + "x" * 32 for i in range(ra.MAX_DEVICES + 5)]
        write_store(self.path, [entry(t, id=f"d{i}", last_seen=T0 + i) for i, t in enumerate(tokens)])
        d = ra.Devices(path=self.path, clock=self.clock)
        self.assertEqual(len(d.list()), ra.MAX_DEVICES)
        self.assertIsNone(d.check(tokens[0]))
        self.assertIsNotNone(d.check(tokens[-1]))

    def test_expired_entries_dont_take_a_place_on_load(self):
        day = 86400
        live = [f"live-{i:02d}-" + "x" * 32 for i in range(ra.MAX_DEVICES)]
        rows = [entry(t, id=f"live{i}", created=T0 - 25 * day, last_seen=T0 - 20 * day, expires=T0 + 5 * day)
                for i, t in enumerate(live)]
        # expired ten days ago, but seen more recently than any live device
        rows.append(entry("gone" + "x" * 40, id="gone", created=T0 - 40 * day, last_seen=T0 - 10 * day - 1,
                          expires=T0 - 10 * day))
        write_store(self.path, rows)
        d = ra.Devices(path=self.path, clock=self.clock)
        self.assertEqual(len(d.list()), ra.MAX_DEVICES)
        for t in live:
            self.assertIsNotNone(d.check(t))


class DevicesCorrupt(DevicesBase):
    def load(self, content):
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(self.path, mode) as f:
            f.write(content)
        with self.assertLogs("deck", "WARNING") as logs:
            d = ra.Devices(path=self.path, clock=self.clock)
        self.assertEqual(d.list(), [])
        return d, logs

    def test_corrupt_files_start_empty(self):
        token = "t" * 43
        good_row = entry(token)
        for content in ("{not json", "", "null", "[]", '"x"', json.dumps({"devices": [good_row]}),
                        json.dumps({"version": 2, "devices": [good_row]}),
                        json.dumps({"version": True, "devices": [good_row]}),
                        json.dumps({"version": 1.0, "devices": [good_row]}),
                        json.dumps({"version": "1", "devices": [good_row]}),
                        json.dumps({"version": 1, "devices": {"dev1": good_row}}),
                        json.dumps({"version": 1}), b"\xff\xfe\x00garbage", "[" * 100_000):
            d, _ = self.load(content)
            self.assertIsNone(d.check(token), repr(content)[:40])

    def test_recovers_by_writing_a_good_file(self):
        d, logs = self.load("{not json")
        self.assertIn("corrupt", "\n".join(logs.output))
        _, token = d.add("Phone", PHONE)
        self.assertIsNotNone(ra.Devices(path=self.path, clock=self.clock).check(token))

    def test_corrupt_entries_are_ignored_entirely(self):
        good = "g" * 43
        bad = {f"bad{i}": "b" * 40 + f"{i:03d}" for i in range(20)}
        rows = [entry(good, id="good")]
        broken = [
            {k: v for k, v in entry(bad["bad0"], id="b0").items() if k != "name"},
            entry(bad["bad1"], id="b1", expires="never"),
            entry(bad["bad2"], id="b2", expires=float("nan")),
            entry(bad["bad3"], id="b3", expires=float("inf")),
            entry(bad["bad4"], id="b4", created=True),
            entry(bad["bad5"], id="bad id!"),
            entry(bad["bad6"], id="b6", token_sha256=sha(bad["bad6"]).upper()),
            entry(bad["bad7"], id="b7", token_sha256=sha(bad["bad7"])[:40]),
            entry(bad["bad8"], id="b8", ip="junk"),
            entry(bad["bad9"], id="b9", last_ip=None),
            entry(bad["bad10"], id="good"),                       # duplicate id
            entry(bad["bad11"], id="b11", token_sha256=sha(good)),  # duplicate hash
            entry(bad["bad12"], id="b12", name=5),
            entry(bad["bad13"], id=""),
            entry(bad["bad14"], id="b14", last_seen=None),
            "a string", None, 5, [],
        ]
        with open(self.path, "w") as f:   # (json.dump writes NaN/Infinity, which json.load reads back)
            json.dump({"version": 1, "devices": rows + broken}, f)
        with self.assertLogs("deck", "WARNING") as logs:
            d = ra.Devices(path=self.path, clock=self.clock)
        self.assertIn(f"ignored {len(broken)} corrupt entries", "\n".join(logs.output))
        self.assertEqual([x["id"] for x in d.list()], ["good"])
        self.assertIsNotNone(d.check(good))
        for token in bad.values():
            self.assertIsNone(d.check(token))

    def test_names_from_the_file_are_cleaned(self):
        token = "n" * 43
        write_store(self.path, [entry(token, name="‮evil\nname", last_ip="")])
        d = ra.Devices(path=self.path, clock=self.clock)
        self.assertEqual(d.check(token)["name"], "evilname")

    def test_not_a_regular_file(self):
        os.mkdir(self.path)
        with self.assertLogs("deck", "WARNING"):
            self.assertEqual(ra.Devices(path=self.path, clock=self.clock).list(), [])

    def test_symlink_is_not_followed(self):
        token = "s" * 43
        target = os.path.join(tmpdir(), "elsewhere.json")
        write_store(target, [entry(token)])
        os.symlink(target, self.path)
        with self.assertLogs("deck", "WARNING"):
            d = ra.Devices(path=self.path, clock=self.clock)
        self.assertIsNone(d.check(token))
        d.add("Phone", PHONE)        # replaces the link, never writes through it
        self.assertFalse(os.path.islink(self.path))
        with open(target) as f:
            self.assertEqual(json.load(f)["devices"][0]["token_sha256"], sha(token))

    def test_pipe_does_not_block(self):
        os.mkfifo(self.path)
        with self.assertLogs("deck", "WARNING"):
            self.assertEqual(ra.Devices(path=self.path, clock=self.clock).list(), [])

    def test_too_big(self):
        token = "z" * 43
        with open(self.path, "w") as f:
            json.dump({"version": 1, "devices": [entry(token)], "pad": "x" * (ra._MAX_FILE + 1)}, f)
        with self.assertLogs("deck", "WARNING"):
            d = ra.Devices(path=self.path, clock=self.clock)
        self.assertIsNone(d.check(token))

    def test_unreadable(self):
        write_store(self.path, [entry("u" * 43)])
        with mock.patch.object(ra, "read_regular", side_effect=PermissionError(13, "Permission denied")):
            with self.assertLogs("deck", "WARNING") as logs:
                d = ra.Devices(path=self.path, clock=self.clock)
        self.assertEqual(d.list(), [])
        self.assertIn("Permission denied", "\n".join(logs.output))


class DevicesLastSeen(DevicesBase):
    def test_throttled_to_once_a_minute(self):
        device, token = self.devices.add("Phone", PHONE)
        with mock.patch.object(ra, "write_atomic", wraps=ra.write_atomic) as writes:
            self.clock.advance(10)
            got = self.devices.check(token, LAPTOP)
            self.assertEqual(got["last_seen"], T0 + 10)        # in memory at once
            self.assertEqual(self.devices.list()[0]["last_seen"], T0 + 10)
            self.clock.advance(40)
            self.assertEqual(self.devices.check(token)["last_seen"], T0 + 50)
            self.assertEqual(writes.call_count, 0)
            self.assertEqual(self.on_disk()["devices"][0]["last_seen"], T0)
            self.clock.advance(10)                              # a minute since it was written
            self.devices.check(token, "::ffff:192.168.1.99")
            self.assertEqual(writes.call_count, 1)
            row = self.on_disk()["devices"][0]
            self.assertEqual((row["last_seen"], row["last_ip"], row["ip"]), (T0 + 60, "192.168.1.99", PHONE))
            for _ in range(5):
                self.clock.advance(5)
                self.devices.check(token)
            self.assertEqual(writes.call_count, 1)

    def test_per_device(self):
        _, a = self.devices.add("a", PHONE)
        self.clock.advance(30)
        _, b = self.devices.add("b", LAPTOP)                    # writes both: a's minute starts again
        with mock.patch.object(ra, "write_atomic", wraps=ra.write_atomic) as writes:
            self.clock.advance(59)
            self.devices.check(a)
            self.devices.check(b)
            self.assertEqual(writes.call_count, 0)
            self.clock.advance(1)
            self.devices.check(a)
            self.devices.check(b)                                # just written along with a
            self.assertEqual(writes.call_count, 1)

    def test_junk_ip_keeps_the_last_one(self):
        _, token = self.devices.add("Phone", PHONE)
        self.devices.check(token, "192.168.1.77")
        self.devices.check(token, "junk")
        self.devices.check(token)
        self.clock.advance(60)
        self.devices.check(token, None)
        self.assertEqual(self.on_disk()["devices"][0]["last_ip"], "192.168.1.77")

    def test_clock_stepping_back_still_writes(self):
        _, token = self.devices.add("Phone", PHONE)
        with mock.patch.object(ra, "write_atomic", wraps=ra.write_atomic) as writes:
            self.clock.advance(-3600)
            self.devices.check(token)
            self.assertEqual(writes.call_count, 1)

    def test_a_failed_write_keeps_the_device_working(self):
        _, token = self.devices.add("Phone", PHONE)
        self.clock.advance(60)
        with mock.patch.object(ra, "write_atomic", side_effect=OSError("disk")) as writes:
            with self.assertLogs("deck", "WARNING"):
                self.assertIsNotNone(self.devices.check(token))
            self.clock.advance(30)
            self.assertIsNotNone(self.devices.check(token))     # not retried within the minute
            self.assertEqual(writes.call_count, 1)

    def test_expired_device_is_not_touched(self):
        self.devices.days = 1
        _, token = self.devices.add("Phone", PHONE)
        self.clock.advance(86400 + 120)
        with mock.patch.object(ra, "write_atomic", wraps=ra.write_atomic) as writes:
            self.assertIsNone(self.devices.check(token, LAPTOP))
        self.assertEqual(writes.call_count, 0)


class DevicesThreads(DevicesBase):
    def test_concurrent_adds_and_checks(self):
        errors, tokens, lock = [], [], threading.Lock()
        barrier = threading.Barrier(8)

        def work(i):
            try:
                barrier.wait()
                for j in range(3):
                    _, t = self.devices.add(f"d{i}-{j}", f"10.0.{i}.{j + 1}")
                    with lock:
                        tokens.append(t)
                    for _ in range(10):
                        self.clock.advance(7)   # (not atomic, but only ever moves forward)
                        if self.devices.check(t, f"10.0.{i}.9") is None:
                            raise AssertionError("a fresh token didn't check")
                        self.devices.list()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(len(self.devices.list()), 24)
        again = ra.Devices(path=self.path, clock=self.clock)
        self.assertEqual(sorted(d["id"] for d in again.list()), sorted(d["id"] for d in self.devices.list()))
        for t in tokens:
            self.assertIsNotNone(again.check(t))


class Docstring(unittest.TestCase):
    def test_explains_the_trust_model(self):
        doc = ra.__doc__.lower()
        for words in ("code", "approve", "sha256", "revoc", "loopback", "plain http", "vpn",
                      "idempotent", "lost", "192", "/64", "max_starts_per_peer"):
            self.assertIn(words, doc)
        with open(ra.__file__) as f:
            source = f.read()
        self.assertFalse(re.search(r"^\s*(import|from)\s.*tailscale", source, re.M | re.I))


if __name__ == "__main__":
    unittest.main()
