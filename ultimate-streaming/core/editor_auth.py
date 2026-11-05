"""The web editor's access token, launch codes and URL.

The editor can change config.yaml, and keys can run shell commands, so it is only
reachable from this computer (127.0.0.1) and every API call must carry a random
token. The token lives in ~/.config/ultimate-streaming/editor_token (mode 600), and the
daemon replaces it each time it starts (and on POST /api/session/revoke).

The token never appears in a URL or on a command line: other users on this computer can read
every process's command line (/proc/<pid>/cmdline), and so `xdg-open <url>` would show it to
them. A URL carries a launch code instead (#code=...): random, single use, valid for
LAUNCH_TTL seconds, recorded only as its sha256 in <state dir>/launch/ (mode 700), so any
process of this user can mint one (`main.py --open-editor` is not the daemon). The page trades
it for the token with POST /api/session; that only works from a socket owned by this user.

Before a URL is handed out, the listener on the port must be ours: the editor server bound in
this process, or a socket of this user's in /proc/net/tcp. Someone else holding the port
would otherwise receive the code in location.hash.
"""
import hashlib
import os
import re
import secrets
import socket
import stat
import struct
import threading
import time

from .paths import state_dir, state_file
from .safefs import write_atomic

DEFAULT_PORT = 8765
LAUNCH_TTL = 60          # seconds a launch code stays valid
LAUNCH_SKEW = 5          # tolerated clock step backwards between minting and redeeming
MAX_CODE_LEN = 128
PROC_NET = ("/proc/net/tcp", "/proc/net/tcp6")
TCP_LISTEN, TCP_TIME_WAIT = "0A", "06"

NOT_RUNNING = ("The Stream Deck daemon isn't running, so the control panel isn't either.\n"
               "Start it with:  systemctl --user start ultimate-streaming   (or: python3 main.py)")

_lock = threading.RLock()
_current = None          # the token this process set by rotate(); None until then
_here = {}               # port -> bound? for each editor server this process runs (or is trying to)
_DIGEST = re.compile(r"[0-9a-f]{64}")
# Addresses whose listener answers a connection to 127.0.0.1 (IPv4, and IPv6 dual-stack).
_LOOPBACK_V4 = {"127.0.0.1", "0.0.0.0"}
_LOOPBACK_V6 = {"::", "::ffff:127.0.0.1", "::ffff:0.0.0.0"}


class Unavailable(RuntimeError):
    """No panel URL can be handed out: the server isn't running, or the port isn't ours."""


# ----- the token

def _on_disk():
    try:
        fd = os.open(state_file("editor_token"), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    with os.fdopen(fd) as f:
        tok = f.read().strip()
    return tok if len(tok) >= 32 else None


def token():
    """The current token: the one this process last set, else the one on disk (made if missing)."""
    with _lock:
        return _current or _on_disk() or rotate()


def rotate():
    """A fresh token, written atomically (0600). Every open panel and unused launch code stops working."""
    global _current
    with _lock:
        tok = secrets.token_urlsafe(32)
        write_atomic(state_file("editor_token"), tok.encode(), mode=0o600)
        _current = tok
        _clear_codes()
        return tok


def daemon_token():
    """The token for this daemon run: rotated on the first call in this process, then kept
    (rotated again if its file was removed or changed meanwhile)."""
    with _lock:
        if _current and _on_disk() == _current:
            return _current
        return rotate()


# ----- launch codes

def _launch_dir():
    d = os.path.join(state_dir(), "launch")
    state_file("launch")   # creates the state dir (0700)
    try:
        os.mkdir(d, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise OSError(f"{d} is not a folder of yours; remove it")
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(d, 0o700)
    return d


def _digest(code):
    return hashlib.sha256(code.encode()).hexdigest()


def _expired(st, now):
    age = now - st.st_mtime
    return age > LAUNCH_TTL or age < -LAUNCH_SKEW


def _prune(d, now=None, everything=False):
    now = time.time() if now is None else now
    try:
        names = os.listdir(d)
    except FileNotFoundError:
        return
    for name in names:
        if not _DIGEST.fullmatch(name):
            continue
        path = os.path.join(d, name)
        try:
            if everything or _expired(os.lstat(path), now):
                os.unlink(path)
        except FileNotFoundError:
            pass


def _clear_codes():
    d = os.path.join(state_dir(), "launch")
    if os.path.isdir(d) and not os.path.islink(d):
        _prune(d, everything=True)


def new_launch_code():
    """A fresh single-use code; only its sha256 is stored (<state dir>/launch/<hex>, 0600)."""
    d = _launch_dir()
    _prune(d)
    code = secrets.token_urlsafe(24)
    fd = os.open(os.path.join(d, _digest(code)),
                 os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    os.close(fd)
    return code


def redeem_launch_code(code):
    """True once for a code minted under LAUNCH_TTL seconds ago, False otherwise.
    The unlink is the single-use test: of two concurrent redeemers only one removes the file.
    Expired codes are deleted on the way."""
    try:
        d = _launch_dir()
    except OSError:
        return False
    now = time.time()
    _prune(d, now)
    if not isinstance(code, str) or not 0 < len(code) <= MAX_CODE_LEN:
        return False
    path = os.path.join(d, _digest(code))
    try:
        st = os.lstat(path)
        os.unlink(path)
    except FileNotFoundError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and not _expired(st, now)


# ----- who is listening

def _addr(hexaddr):
    """An address from /proc/net/tcp{,6}: 32-bit words in host byte order."""
    host, port = hexaddr.split(":")
    raw = b"".join(struct.pack("=I", int(host[i:i + 8], 16)) for i in range(0, len(host), 8))
    if len(raw) == 4:
        return socket.inet_ntop(socket.AF_INET, raw), int(port, 16)
    return socket.inet_ntop(socket.AF_INET6, raw), int(port, 16)


def _sockets(paths=None):
    """(local (ip, port), remote (ip, port), state, uid) for each TCP socket, or None if no
    table could be read."""
    out, read_any = [], False
    for path in paths or PROC_NET:
        try:
            with open(path) as f:
                lines = f.read().splitlines()[1:]
        except OSError:
            continue
        read_any = True
        for line in lines:
            fields = line.split()
            if len(fields) < 8:
                continue
            try:
                out.append((_addr(fields[1]), _addr(fields[2]), fields[3], int(fields[7])))
            except (ValueError, OSError, struct.error):
                continue
    return out if read_any else None


def listener_uids(port, paths=None):
    """The uids of the sockets listening where a connection to 127.0.0.1:port would land
    (empty: nobody), or None if /proc/net/tcp can't be read."""
    socks = _sockets(paths)
    if socks is None:
        return None
    return {uid for (ip, p), _, st, uid in socks
            if st == TCP_LISTEN and p == port and ip in _LOOPBACK_V4 | _LOOPBACK_V6}


def listener_is_ours(port, paths=None):
    """True if 127.0.0.1:port is held by this user only, False if another user holds it,
    None if that can't be told (no /proc, or nobody is listening)."""
    uids = listener_uids(port, paths)
    if not uids:
        return None
    return uids == {os.getuid()}


def peer_uids(client, server_port, paths=None):
    """The uids owning the client end of a loopback connection to server_port (client is the
    (ip, port) the server saw): empty if it isn't in the table (the client has closed it, so it
    can't read a reply either), None if /proc/net/tcp can't be read. Any state counts (a client
    that shut its write side is still listening for the reply) except TIME_WAIT, whose rows
    carry no owner."""
    socks = _sockets(paths)
    if socks is None:
        return None
    cip, cport = client[0], client[1]
    return {uid for (ip, p), (_, rp), st, uid in socks
            if p == cport and rp == server_port and st != TCP_TIME_WAIT and ip in (cip, f"::ffff:{cip}")}


def mark_starting(port):
    """This process will serve the panel on `port` but isn't listening yet: no link until it is."""
    with _lock:
        _here[port] = False


def mark_bound(port):
    with _lock:
        _here[port] = True


def mark_unbound(port):
    with _lock:
        _here.pop(port, None)


def is_bound(port):
    with _lock:
        return _here.get(port, False)


def check_listener(port):
    """Raise Unavailable unless the panel on 127.0.0.1:port is ours: bound in this process, or
    (when this process doesn't run one on that port) held only by a socket of this user's."""
    with _lock:
        here = _here.get(port)
    if here:
        return
    if here is False:
        raise Unavailable(f"The control panel isn't listening on 127.0.0.1:{port} yet (something else holds "
                          "the port; see the log), so no link is handed out.")
    uids = listener_uids(port)
    if uids is None:
        raise Unavailable(f"Can't tell who is listening on 127.0.0.1:{port} (/proc/net/tcp is unreadable), "
                          "so the panel's link isn't handed out.")
    if not uids:
        raise Unavailable(NOT_RUNNING)
    others = sorted(u for u in uids if u != os.getuid())
    if others:
        raise Unavailable(f"Another user (uid {', '.join(map(str, others))}) is listening on 127.0.0.1:{port}, "
                          "not the Stream Deck daemon, so the panel's link isn't handed to it.\n"
                          "Pick another port with editor: {port: ...} in config.yaml.")


# ----- the URL

def port(cfg):
    return int(((cfg or {}).get("editor") or {}).get("port", DEFAULT_PORT))


def url(cfg):
    """http://127.0.0.1:<port>/#code=<launch code>. Raises Unavailable if the listener isn't ours."""
    p = port(cfg)
    check_listener(p)
    return f"http://127.0.0.1:{p}/#code={new_launch_code()}"
