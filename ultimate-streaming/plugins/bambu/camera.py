"""The printer's camera in OBS: a supervised ffmpeg that pulls the printer's RTSPS stream and
sends it on, untouched, as MPEG-TS for an OBS media source to play.

By default it goes over a Unix socket, $XDG_RUNTIME_DIR/ultimate-streaming/bambu-camera-<port>.sock:
ffmpeg listens there (`-listen 1`, one client) and OBS connects to it. The socket file itself is
created 0775, so the access control is the 0700 folder it sits in: no other local user can reach
the socket to inject video or to watch. When OBS disconnects, ffmpeg exits ("Broken pipe") and
the supervisor starts it again at once, so OBS's reconnect finds a listener. Localhost UDP is the
fallback (chosen with camera.transport: udp, or automatically for a Flatpak OBS, which can't see
that folder, or when the socket path won't fit in sun_path): there any local user could send
video into the OBS source or bind the port first and watch. A folder that isn't safe (not ours,
writable by others, a symlink) never falls back: the camera stays off and says why.

The printer's access code is part of the camera URL, so by default the URL never goes on
ffmpeg's command line (anyone on the machine can read that from /proc): it is written to a
private ffconcat list (0600, in a 0700 folder) that ffmpeg opens with the concat demuxer
(`-f concat -safe 0 -i <list>`; the list's `option` directive, which carries
rtsp_transport=tcp, needs ffmpeg 5.1 or newer), and the list is removed when the relay stops.
`-/i <file>` would be simpler but doesn't work: ffmpeg matches `-i` as a group separator before
it looks for the `/` file prefix. With hide_access_code=False the URL goes on the command line
instead (a warning is logged). Either way, anything ffmpeg says is passed through redact()
before it is kept for the panel or written to the log.

The printer's camera presents a self-signed-looking leaf (CN=<serial>, issued by a Bambu CA it
doesn't send), and ffmpeg verifies TLS. So the leaf is pinned on first use: before a run, if
cert_file is missing, the supervisor fetches the printer's certificate and saves it; ffmpeg then
trusts only that certificate (ca_file) and checks it names the serial (verifyhost).
Verification is never turned off. forget_certificate() drops the pin so the next run re-pins.

Two things stop a dead printer from leaving the relay "live" for ever: ffmpeg gets a socket I/O
timeout (rtsp's `timeout`, in microseconds), and a watchdog terminates it when its -progress
output stops advancing for stall_timeout seconds, so the supervisor retries.

The relay keeps retrying while it is started (the printer may be off for hours), with growing
delays that start over after a run that stayed live for a while. OBS disconnecting from the
socket after a live run isn't a failure: that run is followed by a new listener straight away.

OBS doesn't reconnect to a unix: input by itself, so on the socket the relay tells its owner
(on_listening) each time a new ffmpeg is listening, for it to restart the OBS media source. And
on the socket a stall may only mean OBS isn't reading (ffmpeg's writes block): the first stall
asks for the same OBS restart (on_stall) and allows one more stall_timeout before ffmpeg is
killed. Over UDP a stall kills ffmpeg straight away.
"""
import atexit
import collections
import hashlib
import logging
import os
import re
import secrets
import shutil
import ssl
import stat
import subprocess
import threading
import time
import urllib.parse
import weakref

from core.safefs import write_atomic

log = logging.getLogger("deck")

MIN_FFMPEG = (5, 1)
DEFAULT_RETRY_DELAYS = (2, 4, 8, 16, 30)
RELISTEN_DELAY = 0.25   # seconds between OBS disconnecting and listening again (no busy loop)
TRANSPORTS = ("socket", "udp")
LISTEN_WAIT = 3         # seconds a new ffmpeg gets to create its socket before on_listening is skipped
SUN_PATH_MAX = 107      # bytes of socket path that fit in sockaddr_un.sun_path (108 with the NUL)
CAMERA_PORT = 322
VERSION_TIMEOUT = 5
CERT_TIMEOUT = 10       # seconds for fetching the printer's certificate
SOCKET_TIMEOUT_US = 10_000_000   # ffmpeg's rtsp `timeout` option: microseconds of socket silence
STALL_TIMEOUT = 15      # seconds without -progress advancing before a live run is killed
CERT_MAX = 16 * 1024    # bytes; a leaf certificate's PEM is a few KB
STDERR_LINES = 20       # kept per run; the last 3 become the detail
STDERR_LINE_MAX = 500   # characters kept from one line

ERR_UNREACHABLE = "Couldn't reach the printer's camera."
ERR_REFUSED = "The printer refused the camera connection (check the access code)."
ERR_STOPPED = "ffmpeg stopped unexpectedly."
ERR_STALLED = "The printer's camera stopped sending video."
ERR_MISSING = "ffmpeg isn't installed"
ERR_PRIVATE_FILE = "Couldn't write the camera's private settings file."
ERR_STALE_SOCKET = "Couldn't prepare the camera's private socket."
ERR_SOCKET = ("The camera's private folder ({path}) isn't safe: {reason}. Fix its owner or "
              "permissions; the camera stays off until then.")
ERR_CERT = ("The printer's camera certificate doesn't match the one saved for it, or the serial "
            "number is wrong. Check the serial; if the printer was reset or replaced, use Trust "
            "printer cert again.")
WARN_VISIBLE = ("camera: the access code is visible to other local users in the ffmpeg command "
                "line (bambu.camera.hide_access_code is off)")
_UDP_RISK = "other local users could inject video."
WARN_FLATPAK = f"The camera uses UDP because OBS is a Flatpak; {_UDP_RISK}"
WARN_TOO_LONG = f"The camera uses UDP because its socket path is too long; {_UDP_RISK}"
PROTOCOLS = "file,rtsp,rtsps,tls,tcp,udp,crypto"

_VERSION_RE = re.compile(r"^ffmpeg version\s+n?(\d+)\.(\d+)")
_HOST_RE = re.compile(r"[A-Za-z0-9.:-]+")
_USERINFO_RE = re.compile(r"bblp:[^@\s/]*@", re.IGNORECASE)
_SERIAL_RE = re.compile(r"[A-Za-z0-9]{1,32}")
_PEM_RE = re.compile(r"-----BEGIN CERTIFICATE-----\r?\n[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----\s*")
# GnuTLS/OpenSSL in ffmpeg: "Peer certificate failed verification", "The certificate's owner
# does not match hostname X", "certificate verify failed".
_CERT_RE = re.compile(r"peer certificate|certificate.*(verif|match|trust)", re.IGNORECASE)
_AUTH_RE = re.compile(r"\b(401|403)\b|Unauthorized|Forbidden", re.IGNORECASE)
_UNREACHABLE_HINTS = (
    "connection refused", "connection timed out", "timed out", "no route to host",
    "network is unreachable", "host is unreachable", "connection reset", "error opening input",
    "input/output error", "end of file", "name or service not known", "impossible to open",
    "rtsps://",
)


def ffmpeg_version(ffmpeg="ffmpeg"):
    """(major, minor) from the first line of `ffmpeg -version`, or None if it isn't installed,
    won't run, or names a version we can't read (e.g. a git build's "N-12345-g...")."""
    try:
        out = subprocess.run([ffmpeg, "-version"], stdin=subprocess.DEVNULL,
                             capture_output=True, timeout=VERSION_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    first = out.stdout.decode("utf-8", "replace").splitlines()[:1]
    m = _VERSION_RE.match(first[0].strip()) if first else None
    return (int(m.group(1)), int(m.group(2))) if m else None


def _check_port(port):
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValueError(f"camera port must be a number from 1024 to 65535, not {port!r}")
    return port


def _check_socket_path(path):
    if (not isinstance(path, str) or not os.path.isabs(path) or any(c in path for c in "\r\n\0")
            or len(os.fsencode(path)) >= SUN_PATH_MAX):
        raise ValueError(f"camera socket path must be absolute, one line and under {SUN_PATH_MAX} bytes")
    return path


def output_url(port, socket_path=None):
    """Where ffmpeg sends the stream: the Unix socket it listens on, or UDP, where 1316 bytes is
    7 TS packets, one datagram each."""
    if socket_path is not None:
        return f"unix:{_check_socket_path(socket_path)}"
    return f"udp://127.0.0.1:{_check_port(port)}?pkt_size=1316"


def obs_input_url(port, socket_path=None):
    """What the OBS media source reads: the relay's Unix socket, or UDP, where localaddr binds the
    receiving socket to loopback, so nobody on the LAN can send video into the stream."""
    if socket_path is not None:
        return f"unix:{_check_socket_path(socket_path)}"
    return (f"udp://127.0.0.1:{_check_port(port)}"
            "?localaddr=127.0.0.1&overrun_nonfatal=1&fifo_size=5000000")


class FolderError(OSError):
    """The socket's folder isn't safe to use; `path` and `reason` are for people."""

    def __init__(self, path, reason):
        super().__init__(f"{path}: {reason}")
        self.path, self.reason = path, reason


class Link(collections.namedtuple("Link", "transport socket_path warning error", defaults=(None,))):
    """How the relay and OBS talk: transport "socket" (socket_path set) or "udp"; warning says,
    for people, why it is UDP when socket was asked for (None otherwise). error (socket_path
    None) means there is no safe link: the camera stays off, and it never quietly becomes UDP."""

    def _usable(self):
        if self.error:
            raise RuntimeError(self.error)

    def output_url(self, port):
        self._usable()
        return output_url(port, self.socket_path)

    def obs_url(self, port):
        self._usable()
        return obs_input_url(port, self.socket_path)


def socket_path(port):
    """<$XDG_RUNTIME_DIR/ultimate-streaming>/bambu-camera-<port>.sock (the port keeps several
    printers apart). FolderError if XDG_RUNTIME_DIR isn't an absolute folder of ours that only we
    can write to (whoever can rename things in it could swap our private folder for theirs), or
    our folder in it isn't ours."""
    _check_port(port)
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime or not os.path.isabs(runtime):
        raise FolderError("$XDG_RUNTIME_DIR", "it isn't set to an absolute path")
    try:
        st = os.stat(runtime)
    except OSError as e:
        raise FolderError(runtime, e.strerror or "it can't be read") from None
    if not stat.S_ISDIR(st.st_mode):
        raise FolderError(runtime, "it isn't a folder")
    if st.st_uid != os.getuid():
        raise FolderError(runtime, "it belongs to another user")
    if st.st_mode & 0o022:
        raise FolderError(runtime, "other users can write to it")
    return os.path.join(_make_private(os.path.join(runtime, "ultimate-streaming")),
                        f"bambu-camera-{port}.sock")


def choose_link(transport, port, *, obs_flatpak=False):
    """The Link for camera.transport `transport` ("socket" or "udp") and this port. "socket"
    becomes UDP, with one warning logged, only when OBS is a Flatpak (it can't see our runtime
    folder) or the socket path won't fit in sun_path. A folder that isn't safe is an error
    Link (logged once): falling back to UDP there would hand the stream to whoever made it so."""
    _check_port(port)
    if transport not in TRANSPORTS:
        raise ValueError(f"camera transport must be socket or udp, not {transport!r}")
    if transport == "udp":
        return Link("udp", None, None)
    why = None
    if obs_flatpak:
        warning = WARN_FLATPAK
    else:
        try:
            path = socket_path(port)
        except OSError as e:
            where, reason = ((e.path, e.reason) if isinstance(e, FolderError)
                             else (e.filename or "$XDG_RUNTIME_DIR", e.strerror or str(e)))
            error = ERR_SOCKET.format(path=where, reason=reason)
            log.warning("camera: %s", error)
            return Link("socket", None, None, error)
        else:
            if len(os.fsencode(path)) < SUN_PATH_MAX:
                return Link("socket", path, None)
            warning, why = WARN_TOO_LONG, path
    log.warning("camera: %s%s", warning, f" ({why})" if why else "")
    return Link("udp", None, warning)


def _clear_stale_socket(path):
    """Remove a socket left at `path` by an ffmpeg that was killed, so the new one can bind.
    Only a socket we own is removed (lstat: a symlink is never followed); anything else there
    is an OSError, and stays."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.getuid():
        raise OSError(f"{path} is in the way and isn't our camera socket")
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def camera_url(host, access_code):
    host = f"[{host}]" if ":" in host else host
    return f"rtsps://bblp:{urllib.parse.quote(access_code, safe='')}@{host}:{CAMERA_PORT}/streaming/live/1"


def _check_serial(serial):
    if not isinstance(serial, str) or not _SERIAL_RE.fullmatch(serial):
        raise ValueError("printer serial must be 1 to 32 letters and digits")
    return serial


def _check_cert_file(cert_file):
    if not isinstance(cert_file, str) or not cert_file or any(c in cert_file for c in "'\r\n\0"):
        raise ValueError("camera certificate path can't be empty or contain ', line breaks or NUL")
    return cert_file


def _quote(value):
    """Single-quoted for av_get_token(): everything up to the next ' is taken verbatim
    (backslashes included), so a ' is written as '\\'' (close, escaped quote, reopen)."""
    return "'" + value.replace("'", "'\\''") + "'"


def ffconcat_list(url, ca_file, serial):
    """An ffconcat list naming the camera URL, with RTSP over TCP and TLS verified against the
    pinned certificate (ca_file) and the printer's serial (verifyhost). camera_url()
    percent-encodes the code and the host is checked, so neither ' nor \\ can actually occur in
    the URL; ca_file can't hold a ' and serial is letters and digits."""
    if any(c in url for c in "\r\n\0"):
        raise ValueError("camera URL can't contain line breaks")
    return (f"ffconcat version 1.0\nfile {_quote(url)}\noption rtsp_transport tcp\n"
            f"option timeout {SOCKET_TIMEOUT_US}\noption tls_verify 1\noption ca_file {_quote(_check_cert_file(ca_file))}\n"
            f"option verifyhost {_check_serial(serial)}\n")


def ffmpeg_command(ffmpeg, port, *, list_file=None, url=None, ca_file=None, serial=None,
                   socket_path=None):
    """With list_file, ffmpeg reads the camera URL (and the TLS options) from that ffconcat
    list, so the access code isn't in argv; with url (hide_access_code off) it is, and ca_file
    and serial are required for the same TLS options. Exactly one of list_file and url.
    With socket_path, ffmpeg listens on that Unix socket for one client (OBS); without, it
    sends UDP to the port."""
    if (list_file is None) == (url is None):
        raise ValueError("give exactly one of list_file and url")
    if list_file is not None:
        source = ["-f", "concat", "-safe", "0", "-protocol_whitelist", PROTOCOLS, "-i", list_file]
    else:
        source = ["-rtsp_transport", "tcp", "-timeout", str(SOCKET_TIMEOUT_US), "-tls_verify", "1",
                  "-ca_file", _check_cert_file(ca_file), "-verifyhost", _check_serial(serial), "-i", url]
    # `-listen 1` is the unix protocol's option; a `?listen=1` in the URL is ignored.
    sink = ["-listen", "1"] if socket_path is not None else []
    return [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
            *source, "-map", "0:v", "-c", "copy", "-f", "mpegts", *sink,
            output_url(port, socket_path)]


def describe_failure(stderr_lines):
    text = "\n".join(stderr_lines)
    if _CERT_RE.search(text):
        return ERR_CERT
    if _AUTH_RE.search(text):
        return ERR_REFUSED
    lower = text.lower()
    if any(hint in lower for hint in _UNREACHABLE_HINTS):
        return ERR_UNREACHABLE
    return ERR_STOPPED


def _private_dir():
    """$XDG_RUNTIME_DIR/ultimate-streaming (memory-backed, gone at logout), else the state dir;
    created 0700, and refused if it is a symlink or someone else's."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and os.path.isabs(runtime) and os.path.isdir(runtime):
        d = os.path.join(runtime, "ultimate-streaming")
    else:
        from core import paths
        d = os.path.dirname(paths.state_file("camera"))
    return _make_private(d)


def _make_private(d):
    """Folder `d`, created 0700 if missing; OSError if it is a symlink or someone else's."""
    try:
        os.mkdir(d, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(d)
    if stat.S_ISLNK(st.st_mode):
        raise FolderError(d, "it is a symlink")
    if not stat.S_ISDIR(st.st_mode):
        raise FolderError(d, "it isn't a folder")
    if st.st_uid != os.getuid():
        raise FolderError(d, "it belongs to another user")
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(d, 0o700)
    return d


def _write_list_file(url, ca_file, serial):
    text = ffconcat_list(url, ca_file, serial)
    path = os.path.join(_private_dir(), f"bambu-camera-{secrets.token_hex(8)}.ffconcat")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
    except BaseException:
        _remove(path)
        raise
    return path


def pem_fingerprint(pem):
    """Colon-hex SHA-256 of the certificate's DER, as browsers and openssl show it."""
    return ":".join(f"{b:02X}" for b in hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).digest())


def checked_pem(pem):
    """The printer's certificate as one clean PEM block, or ValueError if it isn't one."""
    if not isinstance(pem, str) or len(pem.encode("utf-8", "replace")) >= CERT_MAX:
        raise ValueError("the printer's certificate is missing or too big")
    if pem.count("-----BEGIN") != 1 or not _PEM_RE.fullmatch(pem.strip() + "\n"):
        raise ValueError("the printer didn't send a single PEM certificate")
    der = ssl.PEM_cert_to_DER_cert(pem.strip())
    if not der or der[0] != 0x30:   # an X.509 certificate is a DER SEQUENCE
        raise ValueError("the printer's certificate isn't DER encoded")
    return ssl.DER_cert_to_PEM_cert(der)


def _remove(path):
    if path:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("camera: couldn't remove %s: %s", path, e)


_relays = weakref.WeakSet()


@atexit.register
def _stop_all():
    """The supervisor is a daemon thread, so its finally doesn't run at exit: stop ffmpeg and
    remove the list files here."""
    for relay in list(_relays):
        try:
            relay.stop()
        except Exception:  # noqa: BLE001 - exiting anyway; one relay mustn't block the rest
            log.exception("camera: stopping relay at exit failed")


class CameraRelay:
    """States: off, starting, live, retrying, unavailable (ffmpeg missing or too old)."""

    def __init__(self, host, access_code, port, ffmpeg="ffmpeg", retry_delays=DEFAULT_RETRY_DELAYS,
                 live_reset=60, term_timeout=5, hide_access_code=True, *, serial, cert_file,
                 stall_timeout=STALL_TIMEOUT, socket_path=None, link_error=None,
                 on_listening=None, on_stall=None):
        """socket_path: listen there for OBS (see choose_link()); None sends UDP to `port`.
        link_error: there is no safe link (a Link's error); start() then only reports it.
        on_listening(relay) / on_stall(relay), socket only, called on the relay's own threads
        (so they must return quickly): a new ffmpeg is listening; video stalled, so OBS may have
        stopped reading. Either way the caller should restart the OBS media source."""
        if not isinstance(host, str) or not _HOST_RE.fullmatch(host):
            raise ValueError("printer host must be a hostname or IP address")
        if not isinstance(access_code, str) or not access_code:
            raise ValueError("printer access code is required")
        if any(c in access_code for c in "\r\n\0"):
            raise ValueError("printer access code can't contain line breaks")
        if not retry_delays:
            raise ValueError("retry_delays must not be empty")
        self.serial, self.cert_file = _check_serial(serial), _check_cert_file(cert_file)
        self.host, self.port, self.ffmpeg = host, _check_port(port), ffmpeg
        self._access_code = access_code
        self.retry_delays = tuple(retry_delays)
        self.live_reset = live_reset
        self.term_timeout = term_timeout
        self.hide_access_code = hide_access_code
        self.stall_timeout = stall_timeout
        self.socket_path = None if socket_path is None else _check_socket_path(socket_path)
        self.link_error = link_error
        self.on_listening, self.on_stall = on_listening, on_stall

        self._control = threading.Lock()   # serialises start() / stop()
        self._lock = threading.Lock()      # everything below
        self._state, self._error, self._detail = "off", None, None
        self._live_since = None
        self._thread = None
        self._stopping = None    # the current run's stop Event; set = this run is over
        self._proc = None
        self._list_file = None

    def __repr__(self):   # keep the access code out of tracebacks and debug logs
        return f"<CameraRelay {self.host} port={self.port} state={self._state}>"

    def redact(self, text):
        text = _USERINFO_RE.sub("<access code>@", text)
        for secret in {self._access_code, urllib.parse.quote(self._access_code, safe="")}:
            text = text.replace(secret, "<access code>")
        return text

    def status(self):
        with self._lock:
            live = self._live_since if self._state == "live" else None
            return {
                "state": self._state,
                "error": self._error,
                "detail": self._detail,
                "live_seconds": int(time.monotonic() - live) if live is not None else None,
                "port": self.port,
            }

    def listening(self):
        """True while an ffmpeg of ours is running and its socket is there for OBS to connect to."""
        if self.socket_path is None:
            return False
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return False
        try:
            return stat.S_ISSOCK(os.lstat(self.socket_path).st_mode)
        except OSError:
            return False

    def _tell(self, callback, what):
        if callback is None:
            return
        try:
            callback(self)
        except Exception:  # noqa: BLE001 - the owner's problem mustn't stop the relay
            log.exception("camera: the %s callback failed", what)

    def start(self):
        with self._control:
            with self._lock:
                if self._thread and self._thread.is_alive() and not self._stopping.is_set():
                    return
            if self.link_error:
                self._set(state="unavailable", error=self.link_error, detail=None)
                return
            version = ffmpeg_version(self.ffmpeg)
            if version is None or version < MIN_FFMPEG:
                if version is None and shutil.which(self.ffmpeg) is None:
                    error = ERR_MISSING
                else:
                    found = f"{version[0]}.{version[1]}" if version else "an unrecognized version"
                    error = f"The printer camera needs ffmpeg 5.1 or newer (found {found})"
                log.warning("camera: %s", error)
                self._set(state="unavailable", error=error, detail=None)
                return
            url, list_file = camera_url(self.host, self._access_code), None
            if self.hide_access_code:
                try:
                    list_file = _write_list_file(url, self.cert_file, self.serial)
                except OSError as e:
                    log.warning("camera: %s %s", ERR_PRIVATE_FILE, self.redact(str(e)))
                    self._set(state="unavailable", error=ERR_PRIVATE_FILE, detail=self.redact(str(e)))
                    return
                cmd = ffmpeg_command(self.ffmpeg, self.port, list_file=list_file,
                                     socket_path=self.socket_path)
            else:
                log.warning(WARN_VISIBLE)
                cmd = ffmpeg_command(self.ffmpeg, self.port, url=url, ca_file=self.cert_file,
                                     serial=self.serial, socket_path=self.socket_path)
            stopping = threading.Event()
            thread = threading.Thread(target=self._supervise, args=(stopping, cmd, list_file),
                                      name=f"bambu-camera-{self.port}", daemon=True)
            with self._lock:
                self._state, self._error, self._detail, self._live_since = "starting", None, None, None
                self._stopping, self._thread, self._list_file = stopping, thread, list_file
            _relays.add(self)
            log.info("camera: relaying %s to %s", self.host, output_url(self.port, self.socket_path))
            thread.start()

    def stop(self):
        with self._control:
            with self._lock:
                thread, proc, list_file = self._thread, self._proc, self._list_file
                if self._stopping:
                    self._stopping.set()
                self._thread = self._proc = self._list_file = None
                self._state, self._error, self._detail, self._live_since = "off", None, None, None
            if proc:
                self._terminate(proc)
            if thread and thread is not threading.current_thread():
                thread.join(self.term_timeout + 2)
                if thread.is_alive():
                    log.warning("camera: relay thread didn't finish in time")
            _remove(list_file)
            _relays.discard(self)
            if thread:
                log.info("camera: stopped")

    def forget_certificate(self):
        """Drop the pinned certificate; the next run trusts whatever the printer presents then.
        Stop the relay first: a stopped run never writes its pin (_pin_certificate checks for
        the stop under the lock), so the forget can't be undone by a fetch still in flight."""
        with self._lock:
            try:
                os.unlink(self.cert_file)
            except FileNotFoundError:
                pass
        log.info("camera: forgot the printer's certificate; it is trusted again on the next connection")

    def _pin_certificate(self, stopping):
        """Make sure cert_file holds the printer's certificate, fetching it if missing (trust on
        first use). False if it couldn't be fetched or saved; the state says why."""
        if os.path.exists(self.cert_file):
            return True
        try:
            pem = checked_pem(ssl.get_server_certificate((self.host, CAMERA_PORT),
                                                         timeout=CERT_TIMEOUT))
        except (OSError, ValueError) as e:
            self._set(stopping, state="retrying", error=ERR_UNREACHABLE,
                      detail=f"Couldn't get the camera's certificate: {self.redact(str(e))}",
                      live_since=None)
            return False
        with self._lock:
            # Under the lock, so stop() + forget_certificate() can't be undone by this run.
            if stopping.is_set():
                return False
            try:
                write_atomic(self.cert_file, pem.encode("ascii"))
            except OSError as e:
                self._state, self._error, self._live_since = "retrying", ERR_PRIVATE_FILE, None
                self._detail = self.redact(str(e))
                return False
        log.info("camera: trusted the printer's certificate (sha256 %s)", pem_fingerprint(pem))
        return True

    def _set(self, stopping=None, **fields):
        """Update state; a supervisor passes its stop Event so it can't write after stop()."""
        with self._lock:
            if stopping is not None and stopping.is_set():
                return False
            for key, value in fields.items():
                setattr(self, f"_{key}", value)
            return True

    def _supervise(self, stopping, cmd, list_file):
        failures = 0
        try:
            while not stopping.is_set():
                try:
                    live_for, disconnected = (self._run_once(stopping, cmd)
                                              if self._pin_certificate(stopping) else (0.0, False))
                except Exception:  # noqa: BLE001 - keep retrying rather than die looking "live"
                    log.exception("camera: relay run failed")
                    self._set(stopping, state="retrying", error=ERR_STOPPED, detail=None,
                              live_since=None)
                    live_for, disconnected = 0.0, False
                if stopping.is_set():
                    break
                if disconnected:
                    # OBS left (scene restart, OBS closed): listen again now, so its reconnect
                    # finds us. Only a run that went live gets here, so it can't spin.
                    failures = 0
                    log.info("camera: OBS disconnected; listening for it again")
                    if stopping.wait(RELISTEN_DELAY):
                        break
                    continue
                if live_for >= self.live_reset:
                    failures = 0
                delay = self.retry_delays[min(failures, len(self.retry_delays) - 1)]
                failures += 1
                with self._lock:
                    detail = self._detail
                log.warning("camera: relay dropped (%s); retrying in %ss", detail, delay)
                if stopping.wait(delay):
                    break
                self._set(stopping, state="starting")
        finally:
            _remove(list_file)

    def _run_once(self, stopping, cmd):
        """Run ffmpeg until it exits; return (how long it was live, 0 if never; whether it ended
        because OBS disconnected from the socket after going live)."""
        with self._lock:
            # Under the lock, so stop() either sees this process or we see its stop.
            if stopping.is_set():
                return 0.0, False
            if self.socket_path is not None:
                try:
                    _clear_stale_socket(self.socket_path)
                except OSError as e:
                    self._state, self._error, self._detail = "retrying", ERR_STALE_SOCKET, self.redact(str(e))
                    return 0.0, False
            try:
                proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, start_new_session=True)
            except OSError as e:
                self._state, self._error, self._detail = "retrying", ERR_STOPPED, self.redact(str(e))
                return 0.0, False
            self._proc = proc

        tail = collections.deque(maxlen=STDERR_LINES)

        def read_stderr():
            for raw in proc.stderr:
                text = raw.decode("utf-8", "replace").strip()[:STDERR_LINE_MAX]
                if text:
                    tail.append(self.redact(text))

        reader = threading.Thread(target=read_stderr, name="bambu-camera-stderr", daemon=True)
        reader.start()
        went_live = None
        advance = [None]      # monotonic time of the last sign of progress (watchdog reads it)
        stalled = threading.Event()
        finished = threading.Event()

        kicked = [None]       # the advance[0] the last OBS kick was for (socket only)

        def watchdog():
            period = max(0.05, min(1.0, self.stall_timeout / 4))
            while not finished.wait(period):
                last = advance[0]
                if last is None or time.monotonic() - last <= self.stall_timeout:
                    continue
                if self.socket_path is not None and kicked[0] is None:
                    # Maybe OBS just isn't reading: ask for it back and allow one more period.
                    log.warning("camera: no video for %ss; asking OBS to reconnect", self.stall_timeout)
                    advance[0] = kicked[0] = time.monotonic()
                    self._tell(self.on_stall, "on_stall")
                    continue
                stalled.set()
                log.warning("camera: no video for %ss; restarting ffmpeg", self.stall_timeout)
                self._terminate(proc)
                return

        def await_socket():
            end = time.monotonic() + LISTEN_WAIT
            while not finished.is_set() and time.monotonic() < end:
                try:
                    if stat.S_ISSOCK(os.lstat(self.socket_path).st_mode):
                        if not stopping.is_set():
                            self._tell(self.on_listening, "on_listening")
                        return
                except FileNotFoundError:
                    pass
                except OSError:
                    return
                finished.wait(0.05)

        dog = threading.Thread(target=watchdog, name="bambu-camera-watchdog", daemon=True)
        dog.start()
        if self.socket_path is not None:
            threading.Thread(target=await_socket, name="bambu-camera-listen", daemon=True).start()
        out_time = None       # the last out_time_us value; None until ffmpeg prints one
        try:
            for line in proc.stdout:
                if line.startswith(b"out_time_us="):
                    value = line[len(b"out_time_us="):].strip()
                    if value != out_time and value != b"N/A":
                        advance[0], kicked[0] = time.monotonic(), None
                    out_time = value
                elif line.startswith(b"progress=") and out_time is None:
                    advance[0], kicked[0] = time.monotonic(), None   # no timestamps: a block is progress
                if went_live is None and line.startswith(b"progress="):
                    went_live = time.monotonic()
                    advance[0] = advance[0] or went_live
                    if self._set(stopping, state="live", error=None, detail=None, live_since=went_live):
                        log.info("camera: live")
            proc.wait()
        finally:
            finished.set()
            if proc.poll() is None:
                self._terminate(proc)
            reader.join(1)
            dog.join(self.term_timeout + 2)
            proc.stdout.close()
            proc.stderr.close()
            with self._lock:
                if self._proc is proc:
                    self._proc = None

        lines = list(tail)
        live_for = time.monotonic() - went_live if went_live is not None else 0.0
        if (went_live is not None and self.socket_path is not None and not stalled.is_set()
                and any("broken pipe" in line.lower() for line in lines)):
            self._set(stopping, state="starting", error=None, detail=None, live_since=None)
            return live_for, True
        detail = "\n".join(lines[-3:]) if lines else f"ffmpeg exited with code {proc.returncode}"
        if stalled.is_set():
            error, detail = ERR_STALLED, f"No video for {self.stall_timeout} seconds."
        else:
            error = describe_failure(lines)
        self._set(stopping, state="retrying", error=error, detail=detail, live_since=None)
        return live_for, False

    def _terminate(self, proc):
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(self.term_timeout)
        except ProcessLookupError:
            return
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(self.term_timeout)
            except subprocess.TimeoutExpired:
                log.warning("camera: ffmpeg (pid %s) didn't exit after SIGKILL", proc.pid)
