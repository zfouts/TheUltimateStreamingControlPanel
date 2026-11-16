"""Remote access to the control panel: pairing other devices, their tokens, and the checks the
remote listener makes on every request.

The remote listener binds editor.remote.listen ("0.0.0.0", "::" or one address of this computer)
on editor.remote.port, separately from the 127.0.0.1 panel. It needs no VPN.

Trust model. A device asks for access (Pairing.start) and is shown a 6-digit code; the same
request, with that code, the device's name and its address, appears in the panel on the streaming
computer. A person there approves it only if the code matches the one on the device. The code is
not a password (nobody types it): it tells the person which of the waiting requests is theirs, so a
request someone else sends at the same moment can't be approved by mistake. Codes are unique among
waiting requests. The device collects its result with the request's secret, which only it holds
(stored here only as sha256, compared in constant time). An approved device gets a token of its
own (Devices): one per device, kept on disk only as sha256 (remote_devices.json, mode 600),
expiring after Devices.days and revocable one by one or all at once. Approval is the only way in;
waiting requests live in memory, so a daemon restart drops them.

Collecting an approval is idempotent: every poll with the request's secret gets the same token
until the approval is ttl old, not just the first one. A poll whose answer is lost on the way
(a dropped connection, a reloaded page) can then simply be repeated, where a token handed out only
once would have left an approved device that never got it. That is safe because the secret is 192
random bits that only the device holds. When the approval is forgotten, its device is revoked only
if no poll ever collected the token, since then nobody holds it.

Limits per peer (failures, open connections, waiting requests, pairing starts) count an IPv4 peer
by its address and an IPv6 peer by its /64 (peer_key): one host usually holds a whole /64 and can
use any address in it. Each address (each /64) may start at most MAX_STARTS_PER_PEER pairing
requests per START_WINDOW, however they are answered, so nobody can flood this computer's panels
and desktop with requests.

Loopback peers are refused on the remote listener (is_loopback). The local panel trusts only
sockets owned by this user (core.editor_auth); the remote listener can't tell local programs
apart, so every program of every user on this computer, a sandboxed app, or a local proxy would
arrive as one "127.0.0.1" asking for access, and an approval meant for a phone could land on one
of them. Local use belongs on the local panel. Junk addresses count as loopback (fail closed).

Plain HTTP. The remote listener speaks HTTP without TLS, so anyone who can watch or redirect the
traffic (a shared or public Wi-Fi, a spoofed name on the network) can read a device's token and use
the panel, which can run commands on this computer. Prefer a network you trust, or reach this
computer over a VPN (WireGuard, Tailscale or similar), and revoke any device you no longer use.

Fail closed. Anything that doesn't parse is refused: a Host header not exactly one of
allowed_hosts(), an address that isn't one, a token or secret of the wrong shape, a corrupt
device file (which starts the store empty: every device has to pair again). Refusals the caller
should send to the client are Refused(status, message), and messages say nothing about this
computer. Everything here is safe to call from many handler threads at once.
"""
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import unicodedata
from collections import deque

from .paths import state_file
from .safefs import NotRegular, TooBig, read_regular, write_atomic

log = logging.getLogger("deck")

DEFAULT_PORT = 8767
PAIR_TTL = 300            # seconds a pairing request waits for approval
DEVICE_DAYS = 30          # default lifetime of an approved device
MAX_PENDING_PER_IP, MAX_PENDING = 3, 10     # waiting requests per peer (see peer_key), and in all
MAX_STARTS_PER_PEER, START_WINDOW = 3, 300   # pairing requests a peer may start per window (seconds)
MAX_DEVICES = 32

NAME_MAX = 60             # characters in a device's name
_UNNAMED = "Unnamed device"
_MAX_TOKEN_LEN = 256      # longer tokens and secrets are refused without hashing
_MAX_ID_LEN = 64
_MAX_FILE = 1024 * 1024   # remote_devices.json larger than this is treated as corrupt
_SEEN_EVERY = 60          # a device's last_seen is written to disk at most this often (seconds)
_TABLE_MAX = 1024         # peers tracked by PeerLimit
_FILE_VERSION = 1
_IP_TIMEOUT = 2.0         # seconds `ip -j addr show` may take
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_LABEL = re.compile(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?")
_PUBLIC = ("id", "name", "ip", "created", "last_seen", "expires")


class Refused(Exception):
    """A request refused with an HTTP status; `message` is safe to show the client."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def _clip(text, n=64):
    """Text safe for a log line: at most n characters, all printable."""
    text = text if isinstance(text, str) else repr(text)
    return "".join(c if c.isprintable() else "?" for c in text[:n])


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


# ----- addresses and names

def normalize_ip(ip: str) -> str:
    """The canonical text of an address: [brackets] and an IPv6 %zone dropped, an IPv4-mapped
    IPv6 address (::ffff:a.b.c.d, as a dual-stack socket reports IPv4 peers) turned into IPv4.
    ValueError if it isn't an IP address."""
    if not isinstance(ip, str):
        raise ValueError(f"not an IP address: {_clip(ip)}")
    text = ip[1:-1] if ip.startswith("[") and ip.endswith("]") else ip
    if ":" in text and "%" in text:
        text = text.split("%", 1)[0]
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        raise ValueError(f"not an IP address: {_clip(ip)!r}") from None
    if addr.version == 6 and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return str(addr)


def _addr(ip):
    return ipaddress.ip_address(normalize_ip(ip))


def is_loopback(ip: str) -> bool:
    """True for a loopback peer (127.0.0.0/8, ::1, ::ffff:127.x.y.z), the unspecified address,
    and anything that isn't an address at all (fail closed)."""
    try:
        addr = _addr(ip)
    except ValueError:
        return True
    return addr.is_loopback or addr.is_unspecified


def _usable(addr):
    """An address worth offering to other devices: not loopback, link-local, unspecified or multicast."""
    return not (addr.is_loopback or addr.is_link_local or addr.is_unspecified or addr.is_multicast)


def _sorted_ips(raw):
    """Distinct usable addresses from `raw` (junk skipped), normalised, IPv4 first, in numeric order."""
    seen = {}
    for ip in raw:
        try:
            addr = _addr(ip)
        except ValueError:
            continue
        if _usable(addr):
            seen[str(addr)] = addr
    return [str(a) for a in sorted(seen.values(), key=lambda a: (a.version, int(a)))]


def _run_ip(argv):
    """stdout of `ip ...` (no shell). OSError if it's missing, fails or takes too long."""
    exe = shutil.which(argv[0]) or shutil.which(argv[0], path="/usr/sbin:/sbin:/usr/bin:/bin")
    if not exe:
        raise OSError(f"{argv[0]}: command not found")
    try:
        r = subprocess.run([exe, *argv[1:]], capture_output=True, encoding="utf-8", errors="replace",
                           timeout=_IP_TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise OSError(f"`{' '.join(argv)}` took longer than {_IP_TIMEOUT:g}s") from None
    except subprocess.SubprocessError as e:
        raise OSError(f"couldn't run `{' '.join(argv)}`: {e}") from None
    if r.returncode != 0:
        raise OSError(f"`{' '.join(argv)}` failed (exit {r.returncode})")
    return r.stdout


def _from_ip_json(text):
    """Addresses from `ip -j addr show`. ValueError unless it is the list of interfaces iproute2 prints."""
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("`ip -j addr` didn't print a list")
    out = []
    for link in data:
        infos = link.get("addr_info") if isinstance(link, dict) else None
        for info in infos if isinstance(infos, list) else ():
            if isinstance(info, dict) and info.get("family") in ("inet", "inet6") and isinstance(info.get("local"), str):
                out.append(info["local"])
    return out


def _from_getaddrinfo():
    out = []
    for family, _, _, _, sockaddr in socket.getaddrinfo(socket.gethostname(), None):
        if family in (socket.AF_INET, socket.AF_INET6) and sockaddr and isinstance(sockaddr[0], str):
            out.append(sockaddr[0])
    return out


def local_addresses(run=None) -> list:
    """This computer's addresses other devices could reach it on (no loopback or link-local),
    normalised, IPv4 first. From `ip -j addr show` (iproute2; `run(argv) -> stdout` stands in for
    it in tests), else from resolving this computer's hostname. Best effort: [] if neither works."""
    try:
        return _sorted_ips(_from_ip_json((run or _run_ip)(["ip", "-j", "addr", "show"])))
    except Exception as e:   # (any failure means: ask the resolver instead)
        log.debug("remote access: `ip -j addr show` gave no addresses (%s); resolving the hostname", _clip(str(e), 200))
    try:
        return _sorted_ips(_from_getaddrinfo())
    except Exception as e:
        log.debug("remote access: couldn't resolve this computer's hostname: %s", _clip(str(e), 200))
        return []


def own_addresses(run=None) -> set:
    """Every address of this computer's interfaces, normalised, loopback and link-local included
    (local_addresses() leaves those out): for telling whether a peer is this computer itself.
    Same sources as local_addresses(); best effort: an empty set if neither works."""
    try:
        raw = _from_ip_json((run or _run_ip)(["ip", "-j", "addr", "show"]))
    except Exception:
        try:
            raw = _from_getaddrinfo()
        except Exception:
            return set()
    out = set()
    for ip in raw:
        try:
            out.add(normalize_ip(ip))
        except ValueError:
            continue
    return out


def _port(port):
    if isinstance(port, bool):
        raise ValueError("port must be a number from 1 to 65535")
    try:
        p = int(port)
    except (TypeError, ValueError):
        raise ValueError("port must be a number from 1 to 65535") from None
    if not 0 < p < 65536 or (isinstance(port, float) and p != port):
        raise ValueError("port must be a number from 1 to 65535")
    return p


def _hostname(name):
    """A DNS name lowercased without its trailing dot, or "" if it isn't a plausible one."""
    if not isinstance(name, str):
        return ""
    name = name.strip().lower()
    if name.endswith("."):
        name = name[:-1]
    if not 0 < len(name) <= 253 or not all(_LABEL.fullmatch(label) for label in name.split(".")):
        return ""
    return name


def _mdns_name(hostname):
    """The name this computer has on the local network (hostname.local; a dotted hostname
    contributes its first label, as mDNS responders publish it), or "" if there's none."""
    name = _hostname(hostname)
    if not name or name.endswith(".local"):
        return name
    return name.split(".", 1)[0] + ".local"


def _host_port(ip, port):
    return f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"


def _listen_addr(listen):
    """The listen address normalised, or None if it isn't one."""
    try:
        return _addr(listen)
    except ValueError:
        return None


def allowed_hosts(listen: str, port: int, addresses, hostname: str, extra=()) -> set:
    """The Host headers the remote listener accepts (defeats DNS rebinding), all lowercase
    "host:port": each of `addresses` ("[v6]:port"), the hostname and its .local name, each extra
    name (or address), and the listen address when it is a specific one. Junk is skipped, never
    widened: no wildcards. ValueError for a bad port."""
    port = _port(port)
    out = set()
    for ip in addresses or ():
        try:
            out.add(_host_port(normalize_ip(ip), port))
        except ValueError:
            continue
    name = _hostname(hostname)
    if name:
        out.add(f"{name}:{port}")
        out.add(f"{_mdns_name(name)}:{port}")
    for item in (extra,) if isinstance(extra, str) else (extra or ()):
        try:
            out.add(_host_port(normalize_ip(item), port))
            continue
        except ValueError:
            pass
        item = _hostname(item)
        if item:
            out.add(f"{item}:{port}")
    addr = _listen_addr(listen)
    if addr is not None and not addr.is_unspecified:
        out.add(_host_port(str(addr), port))
    return out


def host_ok(host, allowed: set) -> bool:
    """True if a Host header is exactly one of `allowed`, ignoring case and a trailing dot on
    the name ("streampc.local.:8767"). No wildcards, and the port must be there."""
    if not isinstance(host, str) or not host:
        return False
    h = host.lower()
    name, sep, port = h.rpartition(":")
    if sep and name.endswith(".") and not name.startswith("["):
        h = f"{name[:-1]}:{port}"
    return h in {a.lower() for a in allowed if isinstance(a, str)}


def urls(listen, port, addresses, hostname) -> list:
    """The addresses to show the user for opening the panel on another device: http://hostname.local
    first, then each address the listener covers ("0.0.0.0": the IPv4 ones, "::": all of them,
    otherwise just the listen address). [] if `listen` isn't an address."""
    port = _port(port)
    addr = _listen_addr(listen)
    if addr is None:
        return []
    out = []
    mdns = _mdns_name(hostname)
    if mdns:
        out.append(f"http://{mdns}:{port}")
    if addr.is_unspecified:
        covered = [ip for ip in _sorted_ips(addresses or ()) if addr.version == 6 or ":" not in ip]
    else:
        covered = [str(addr)]
    out.extend(f"http://{_host_port(ip, port)}" for ip in covered)
    return out


def clean_name(name) -> str:
    """A device's label as the page sent it, made safe to show: control and format characters
    (newlines, bidi overrides, zero-width marks) dropped, other spaces made plain and runs of them
    collapsed, trimmed, at most NAME_MAX characters. Anything else, or nothing left: "Unnamed device"."""
    if not isinstance(name, str):
        return _UNNAMED
    chars = []
    for c in name[:NAME_MAX * 8]:
        if c.isprintable():
            chars.append(c)
        elif unicodedata.category(c) == "Zs":
            chars.append(" ")
    text = re.sub(r" {2,}", " ", "".join(chars)).strip()
    return text[:NAME_MAX].rstrip() or _UNNAMED


# ----- failure limits

def peer_key(ip) -> str:
    """What per-peer limits count `ip` as: an IPv4 address (IPv4-mapped included) as itself, an
    IPv6 address as its /64 ("2001:db8:1::/64"), since one host usually holds a whole /64 and can
    use any address in it. Anything else as itself, clipped (counted, never a crash)."""
    try:
        addr = _addr(ip)
    except ValueError:
        return _clip(ip)
    if addr.version == 6:
        return str(ipaddress.ip_network((addr, 64), strict=False))
    return str(addr)


class PeerLimit:
    """Failure limit per peer (peer_key: an IPv4 address, an IPv6 /64) plus a global cap (the
    windowed idea of server.FailureLimit). Thread-safe, in memory."""

    def __init__(self, per_peer=10, window=60, global_limit=100, clock=time.monotonic):
        self.per_peer, self.window, self.global_limit = per_peer, window, global_limit
        self._clock = clock
        self._lock = threading.Lock()
        # Only the newest `limit` failure times matter: `limit` of them inside the window is a block.
        self._all = deque(maxlen=max(global_limit, 0))
        self._peers = {}      # ip -> deque of its newest failure times, least recently failed first

    def _fresh(self, times, now):
        while times and now - times[0] >= self.window:
            times.popleft()

    def blocked(self, ip) -> bool:
        """True once `ip` has failed per_peer times, or everyone global_limit times, in the window."""
        key = peer_key(ip)
        with self._lock:
            now = self._clock()
            self._fresh(self._all, now)
            if len(self._all) >= self.global_limit:
                return True
            times = self._peers.get(key)
            if times is not None:
                self._fresh(times, now)
            return len(times or ()) >= self.per_peer

    def fail(self, ip) -> None:
        key = peer_key(ip)
        with self._lock:
            now = self._clock()
            self._all.append(now)
            times = self._peers.pop(key, None)
            if times is None:
                times = deque(maxlen=max(self.per_peer, 0))
            times.append(now)
            self._peers[key] = times
            # drop peers whose last failure is out of the window, and bound the table
            while self._peers:
                oldest = self._peers[next(iter(self._peers))]
                if len(self._peers) <= _TABLE_MAX and oldest and now - oldest[-1] < self.window:
                    break
                del self._peers[next(iter(self._peers))]


# ----- secrets

def _digest(text):
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def _secret_shape(value):
    return isinstance(value, str) and 0 < len(value) <= _MAX_TOKEN_LEN and value.isascii()


# ----- pairing

class _Request:
    __slots__ = ("id", "name", "ip", "key", "code", "digest", "created", "expires", "seq",
                 "state", "done_at", "token", "collected", "device_id", "devices")

    def __init__(self, rid, name, ip, code, digest, created, expires, seq):
        self.id, self.name, self.ip, self.code, self.digest = rid, name, ip, code, digest
        self.key = peer_key(ip)
        self.created, self.expires, self.seq = created, expires, seq
        self.state = "pending"       # then "approved" or "denied"; "expired" is pending past `expires`
        self.done_at = None          # when it was answered
        self.token = None            # an approved request's token, until the approval is forgotten
        self.collected = False       # a poll has handed the token out
        self.device_id = self.devices = None

    def expired(self, now):
        return self.state == "pending" and now >= self.expires


class Pairing:
    """Pending requests for access. Thread-safe. In memory only (a daemon restart drops them).

    A request is "pending" until it is approved or denied (decide) or its ttl runs out ("expired").
    Its device learns the outcome from poll(): an approved request answers every poll with the
    same token until the approval is `ttl` seconds old, then is forgotten (see the module
    docstring); a denied or expired one answers that once and is gone, or is forgotten `ttl`
    seconds later if nobody polls for it. When an approval no poll collected is forgotten, its
    device is revoked (nobody holds its token). Each peer (peer_key) starts at most
    MAX_STARTS_PER_PEER requests per START_WINDOW seconds."""

    def __init__(self, clock=time.time, ttl=PAIR_TTL):
        if not _number(ttl) or ttl <= 0:
            raise ValueError("ttl must be a positive number of seconds")
        self._clock, self._ttl = clock, float(ttl)
        self._lock = threading.Lock()
        self._requests = {}   # id -> _Request, oldest first
        self._started = deque()   # (when, peer_key) of each request started, kept for START_WINDOW
        self._seq = 0

    def _prune(self, now):
        """Forget outcomes reached ttl ago; revoke the devices of approvals no poll collected.
        (Called with the lock held; Devices takes its own lock after ours, never the other way.)"""
        orphans = []
        for rid, r in list(self._requests.items()):
            done = r.done_at if r.state != "pending" else (r.expires if r.expired(now) else None)
            if done is not None and now - done >= self._ttl:
                del self._requests[rid]
                if r.state == "approved":
                    r.token = None
                    if not r.collected:
                        orphans.append(r)
        for r in orphans:
            try:
                r.devices.revoke(r.device_id)
            except OSError as e:   # the device is revoked in memory either way
                log.warning("remote access: couldn't save revoking an approval nobody collected: %s", e)
            log.info("remote access: approval for %r (%s) wasn't collected; device removed", _clip(r.name), r.ip)

    def _pending(self, now):
        return [r for r in self._requests.values() if r.state == "pending" and not r.expired(now)]

    def start(self, ip: str, name: str) -> dict:
        """A new request from the device at `ip`. Its code is shown on the device and in the panel;
        its secret goes only to the device (kept here as sha256). Refused(429) beyond
        MAX_PENDING_PER_IP waiting requests from the peer (peer_key(ip)), beyond
        MAX_STARTS_PER_PEER requests it started in the last START_WINDOW seconds (only the ones
        that were started count), or beyond MAX_PENDING waiting overall; Refused(403) for a
        loopback or junk address (the listener refuses those before this)."""
        if is_loopback(ip):
            raise Refused(403, "Requests from this computer itself aren't accepted here.")
        ip = normalize_ip(ip)
        key = peer_key(ip)
        name = clean_name(name)
        secret = secrets.token_urlsafe(24)
        with self._lock:
            now = self._clock()
            self._prune(now)
            # (a clock stepped back forgets starts rather than refusing a peer for that long)
            self._started = deque((t, k) for t, k in self._started if 0 <= now - t < START_WINDOW)
            waiting = self._pending(now)
            if sum(1 for r in waiting if r.key == key) >= MAX_PENDING_PER_IP:
                raise Refused(429, "This device already has requests waiting; answer or let them expire first.")
            if sum(1 for _, k in self._started if k == key) >= MAX_STARTS_PER_PEER:
                raise Refused(429, "Too many requests from this device; wait a few minutes")
            if len(waiting) >= MAX_PENDING:
                raise Refused(429, "Too many devices are waiting for approval; try again in a few minutes.")
            codes = {r.code for r in self._requests.values()}
            code = f"{secrets.randbelow(10 ** 6):06d}"
            while code in codes:
                code = f"{secrets.randbelow(10 ** 6):06d}"
            rid = secrets.token_urlsafe(9)
            while rid in self._requests:
                rid = secrets.token_urlsafe(9)
            self._seq += 1
            r = _Request(rid, name, ip, code, _digest(secret), now, now + self._ttl, self._seq)
            self._requests[rid] = r
            self._started.append((now, key))
            return {"request": rid, "code": code, "secret": secret, "expires": r.expires}

    def poll(self, request_id: str, secret: str) -> dict:
        """The request's state for its device: {"state": "pending" | "denied" | "expired"} or
        {"state": "approved", "token": ...}. An approval answers every poll the same way until it
        is ttl old (a lost answer can be asked for again); denied and expired are answered once,
        then the request is gone. Refused(404) for an unknown id or a wrong secret
        (indistinguishable)."""
        if not (isinstance(request_id, str) and 0 < len(request_id) <= _MAX_ID_LEN and _secret_shape(secret)):
            raise Refused(404, "No such request")
        digest = _digest(secret)
        with self._lock:
            now = self._clock()
            self._prune(now)
            r = self._requests.get(request_id)
            if r is None or not hmac.compare_digest(r.digest, digest):
                raise Refused(404, "No such request")
            if r.state == "pending" and not r.expired(now):
                return {"state": "pending"}
            if r.state == "approved":   # kept until _prune forgets it, ttl after the approval
                r.collected = True
                return {"state": "approved", "token": r.token}
            del self._requests[request_id]
            return {"state": "expired" if r.state == "pending" else r.state}

    def decide(self, request_id: str, allow: bool, devices: "Devices") -> "dict | None":
        """Approve (allow=True: the device is added to `devices`, its token kept for its polls until
        the approval is ttl old; returns the device without its token) or deny (allow=False;
        returns None) a waiting request.
        Refused(404) if it is unknown, expired or already answered; TypeError unless allow is a bool."""
        if not isinstance(allow, bool):
            raise TypeError("allow must be True or False")
        with self._lock:
            now = self._clock()
            self._prune(now)
            r = self._requests.get(request_id) if isinstance(request_id, str) else None
            if r is None:
                raise Refused(404, "No such request")
            if r.state != "pending":
                raise Refused(404, "That request was already answered")
            if r.expired(now):
                raise Refused(404, "That request has expired")
            if not allow:
                r.state, r.done_at = "denied", now
                return None
            # Under our lock: of two approvers clicking at once, only one adds a device. If adding
            # fails, the request stays pending and the approver can try again.
            device, token = devices.add(r.name, r.ip)
            r.state, r.done_at = "approved", now
            r.token, r.device_id, r.devices = token, device["id"], devices
            return device

    def pending(self) -> list:
        """Requests waiting for an answer, oldest first: {"id", "name", "ip", "code", "expires"}."""
        with self._lock:
            now = self._clock()
            self._prune(now)
            return [{"id": r.id, "name": r.name, "ip": r.ip, "code": r.code, "expires": r.expires}
                    for r in sorted(self._pending(now), key=lambda r: (r.created, r.seq))]


# ----- approved devices

class Devices:
    """Approved devices, persisted to state_file("remote_devices.json") (0600, atomic). Only sha256
    of each token is stored. Thread-safe.

    File: {"version": 1, "devices": [{"id", "name", "ip", "token_sha256", "created", "last_seen",
    "last_ip", "expires"}, ...]}. Device dicts handed out carry only id, name, ip (where it was
    approved from), created, last_seen and expires: never a token or its hash."""

    def __init__(self, path=None, clock=time.time, days=DEVICE_DAYS):
        self.path = path or state_file("remote_devices.json")
        self._clock = clock
        self._lock = threading.Lock()
        self.days = days
        self._devices = {}    # id -> record (with "digest" and "last_ip")
        self._saved = {}      # id -> when its last_seen was last written
        self._load()

    @property
    def days(self) -> float:
        """How long a device added from now on stays approved. ValueError unless a positive number."""
        return self._days

    @days.setter
    def days(self, value):
        if not _number(value) or value <= 0:
            raise ValueError("device days must be a positive number")
        self._days = float(value)

    # -- disk

    def _load(self):
        now = self._clock()
        try:
            raw, _ = read_regular(self.path, _MAX_FILE)
        except FileNotFoundError:
            return
        except OSError as e:
            why = {NotRegular: "not a regular file", TooBig: "too large"}.get(type(e)) or e.strerror or str(e)
            log.warning("remote access: can't read %s (%s); starting with no approved devices", self.path, why)
            return
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError):
            data = None
        version = data.get("version") if isinstance(data, dict) else None
        entries = data.get("devices") if type(version) is int and version == _FILE_VERSION else None
        if not isinstance(entries, list):
            log.warning("remote access: %s is corrupt; starting with no approved devices "
                        "(each device has to pair again)", self.path)
            return
        records, bad, ids, digests = [], 0, set(), set()
        for entry in entries:
            rec = self._record(entry)
            if rec is None or rec["id"] in ids or rec["digest"] in digests:
                bad += 1
                continue
            ids.add(rec["id"])
            digests.add(rec["digest"])
            if rec["expires"] > now:
                records.append(rec)
        if bad:
            log.warning("remote access: ignored %d corrupt entr%s in %s", bad, "y" if bad == 1 else "ies", self.path)
        records.sort(key=lambda r: r["last_seen"], reverse=True)
        for rec in sorted(records[:MAX_DEVICES], key=lambda r: r["created"]):
            self._devices[rec["id"]] = rec
            self._saved[rec["id"]] = now

    @staticmethod
    def _record(entry):
        """A record from one file entry, or None if any field is missing or malformed."""
        if not isinstance(entry, dict):
            return None
        did, name, ip, last_ip, digest = (entry.get(k) for k in ("id", "name", "ip", "last_ip", "token_sha256"))
        times = [entry.get(k) for k in ("created", "last_seen", "expires")]
        if not (isinstance(did, str) and _ID.fullmatch(did) and isinstance(name, str)
                and isinstance(digest, str) and _HEX64.fullmatch(digest) and all(_number(t) for t in times)):
            return None
        try:
            ip = normalize_ip(ip)
            last_ip = normalize_ip(last_ip) if last_ip != "" else ""
        except ValueError:
            return None
        created, last_seen, expires = (float(t) for t in times)
        return {"id": did, "name": clean_name(name), "ip": ip, "digest": digest, "created": created,
                "last_seen": last_seen, "last_ip": last_ip, "expires": expires}

    def _save(self, now):
        """Write every unexpired device (lock held). Expired ones are dropped from memory too."""
        for did in [d for d, rec in self._devices.items() if rec["expires"] <= now]:
            del self._devices[did]
            self._saved.pop(did, None)
        rows = [{"id": r["id"], "name": r["name"], "ip": r["ip"], "token_sha256": r["digest"],
                 "created": r["created"], "last_seen": r["last_seen"], "last_ip": r["last_ip"],
                 "expires": r["expires"]} for r in self._devices.values()]
        data = json.dumps({"version": _FILE_VERSION, "devices": rows}, indent=1, ensure_ascii=False)
        write_atomic(self.path, data.encode("utf-8") + b"\n", mode=0o600)
        for did in self._devices:
            self._saved[did] = now

    @staticmethod
    def _public(rec):
        return {k: rec[k] for k in _PUBLIC}

    # -- the API

    def add(self, name, ip) -> tuple:
        """(device, token) for a newly approved device; only sha256(token) is kept. Expired devices
        are dropped, and beyond MAX_DEVICES the least recently seen one. If the file can't be
        written, nothing changes and the error is raised. ValueError if `ip` isn't an address."""
        name, ip = clean_name(name), normalize_ip(ip)
        token = secrets.token_urlsafe(32)
        with self._lock:
            now = self._clock()
            before, saved_before = dict(self._devices), dict(self._saved)
            for did in [d for d, rec in self._devices.items() if rec["expires"] <= now]:
                del self._devices[did]
            while len(self._devices) >= MAX_DEVICES:
                stale = min(self._devices.values(), key=lambda r: (r["last_seen"], r["created"]))
                del self._devices[stale["id"]]
                self._saved.pop(stale["id"], None)
            did = secrets.token_urlsafe(9)
            while did in self._devices:
                did = secrets.token_urlsafe(9)
            rec = {"id": did, "name": name, "ip": ip, "digest": _digest(token), "created": now,
                   "last_seen": now, "last_ip": ip, "expires": now + self._days * 86400}
            self._devices[did] = rec
            try:
                self._save(now)
            except BaseException:
                self._devices, self._saved = before, saved_before
                raise
            return self._public(rec), token

    def check(self, token: str, ip: str = "") -> "dict | None":
        """The device `token` belongs to, if it is unexpired; else None. Notes the device as seen
        (from `ip`, if that is an address) in memory every time, on disk at most once a minute."""
        if not _secret_shape(token):
            return None
        digest = _digest(token)
        with self._lock:
            now = self._clock()
            match = None
            for rec in self._devices.values():   # every record, each in constant time
                if hmac.compare_digest(rec["digest"], digest):
                    match = rec
            if match is None or now >= match["expires"]:
                return None
            match["last_seen"] = now
            if ip:
                try:
                    match["last_ip"] = normalize_ip(ip)
                except ValueError:
                    pass
            saved = self._saved.get(match["id"])
            if saved is None or not 0 <= now - saved < _SEEN_EVERY:
                try:
                    self._save(now)
                except OSError as e:   # the token is still good; try again in a minute
                    self._saved[match["id"]] = now
                    log.warning("remote access: couldn't record when a device was last seen: %s", e)
            return self._public(match)

    def revoke(self, device_id) -> bool:
        """Remove one device; True if there was one. It stops working at once; if the file can't be
        written the error is raised (and it stays revoked for this run)."""
        with self._lock:
            if not isinstance(device_id, str) or device_id not in self._devices:
                return False
            del self._devices[device_id]
            self._saved.pop(device_id, None)
            self._save(self._clock())
            return True

    def revoke_all(self) -> None:
        """Remove every device (and replace the file, even a corrupt one). Raises if it can't be written."""
        with self._lock:
            self._devices.clear()
            self._saved.clear()
            self._save(self._clock())

    def list(self):   # (no `-> list`: in this class body that name is this method)
        """Unexpired devices, newest first: a list of {"id", "name", "ip", "created", "last_seen", "expires"}."""
        with self._lock:
            now = self._clock()
            live = [r for r in self._devices.values() if r["expires"] > now]
            return [self._public(r) for r in sorted(live, key=lambda r: r["created"], reverse=True)]
