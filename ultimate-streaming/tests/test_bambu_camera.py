"""plugins/bambu/camera.py: the ffmpeg camera relay. ffmpeg itself isn't needed: a fake ffmpeg
(written out in setUp) records its argv and the ffconcat list it was handed, then behaves as the
test's control file says: report progress, fail like an unreachable or refusing printer, or
ignore SIGTERM. Nothing here talks to a network. One end-to-end test runs the real ffmpeg and
ffprobe (skipped without them) on a generated test picture over a temporary socket."""
import hashlib
import json
import os
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from tests import helpers  # noqa: F401
from tests.helpers import tmpdir

from plugins.bambu import camera  # noqa: E402

CODE = "Zq8Kx41P"
HOST = "192.168.1.50"
URL = f"rtsps://bblp:{CODE}@{HOST}:322/streaming/live/1"
SERIAL = "01P00A123456789"
CERT = "/state/bambu-camera-01P00A123456789.pem"
# Not a real certificate: nothing here parses X.509, only the PEM armour and the DER SEQUENCE tag.
DER = b"\x30\x82\x01\x0a" + bytes(range(256)) * 2
PEM = ssl.DER_cert_to_PEM_cert(DER)
PEM2 = ssl.DER_cert_to_PEM_cert(DER[:-1] + b"\x01")
FINGERPRINT = ":".join(f"{b:02X}" for b in hashlib.sha256(DER).digest())


def list_text(cert=CERT):
    return (f"ffconcat version 1.0\nfile '{URL}'\noption rtsp_transport tcp\noption timeout 10000000\noption tls_verify 1\n"
            f"option ca_file '{cert}'\noption verifyhost {SERIAL}\n")


FAKE_FFMPEG = r'''
import json, os, signal, stat, sys, time

args = sys.argv[1:]
if args == ["-version"]:
    print(os.environ.get("FAKE_FFMPEG_VERSION",
                         "ffmpeg version 7.1.1 Copyright (c) 2000-2025 the FFmpeg developers"))
    print("built with gcc 14")
    sys.exit(0)

with open(os.environ["FAKE_FFMPEG_CONTROL"]) as f:
    cfg = json.load(f)
record = {"pid": os.getpid(), "argv": sys.argv, "problems": []}
source = args[args.index("-i") + 1] if "-i" in args else None
if source is None:
    record["problems"].append("no input")
elif cfg.get("hidden", True):
    if any(cfg["secret"] in a for a in sys.argv):
        record["problems"].append("access code in argv")
    if args[args.index("-i") - 6:args.index("-i")] != [
            "-f", "concat", "-safe", "0", "-protocol_whitelist", "file,rtsp,rtsps,tls,tcp,udp,crypto"]:
        record["problems"].append("input isn't a concat list")
    record["list_file"] = source
    record["file_mode"] = stat.S_IMODE(os.stat(source).st_mode)
    record["dir_mode"] = stat.S_IMODE(os.stat(os.path.dirname(source)).st_mode)
    with open(source) as f:
        record["list"] = f.read()
    line = record["list"].splitlines()[1]
    record["url"] = line[len("file '"):-1].replace("'\\''", "'")
    ca_file = record["list"].splitlines()[5][len("option ca_file '"):-1]
else:
    i = args.index("-i")
    if args[i - 10:i - 4] != ["-rtsp_transport", "tcp", "-timeout", "10000000", "-tls_verify", "1"] \
            or args[i - 4] != "-ca_file" or args[i - 2] != "-verifyhost":
        record["problems"].append("rtsp_transport tcp or TLS options missing")
    ca_file = args[i - 3]
    record["url"] = source
try:
    with open(ca_file) as f:
        record["pinned"] = f.read()
except OSError:
    record["pinned"] = None
if args[-1].startswith("unix:"):
    record["socket_there"] = os.path.lexists(args[-1][len("unix:"):])
with open(cfg["log"], "a") as f:
    f.write(json.dumps(record) + "\n")
if record["problems"]:
    print("fake ffmpeg: " + "; ".join(record["problems"]), file=sys.stderr)
    sys.exit(2)

mode, url = cfg["mode"], record["url"]
if args[-1].startswith("unix:") and not cfg.get("no_listen"):   # listen like ffmpeg -listen 1
    import atexit, socket
    sock_path = args[-1][len("unix:"):]
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(sock_path)
    listener.listen(1)
    atexit.register(os.unlink, sock_path)


def err(text):
    print(text, file=sys.stderr, flush=True)


def progress():
    print("frame=30\nfps=30.00\nprogress=continue", flush=True)


if mode == "fail":
    err(f"[tcp @ 0x1] Connection to tcp://{url.split('@')[1].split('/')[0]} failed: Connection refused")
    err(f"access code {cfg['secret']} was in the URL")
    err(f"[in#0 @ 0x2] Error opening input: Connection refused")
    err(f"Error opening input file {url}.")
    sys.exit(1)
if mode == "cert":
    err("[tls @ 0x1] Peer certificate failed verification")
    err("[in#0 @ 0x2] Error opening input: Input/output error")
    err(f"Error opening input file {url}.")
    sys.exit(1)
if mode == "auth":
    err("[rtsp @ 0x1] method DESCRIBE failed: 401 Unauthorized")
    err(f"Error opening input file {url}.")
    sys.exit(1)
if mode == "live_for":
    end = time.monotonic() + cfg["seconds"]
    while time.monotonic() < end:
        progress()
        time.sleep(0.05)
    err("[rtsp @ 0x1] Connection reset by peer")
    sys.exit(1)
if mode == "obs_left":   # live, then OBS disconnects from the socket
    for _ in range(4):
        progress()
        time.sleep(0.05)
    err("[out#0/mpegts @ 0x1] Error submitting a packet to the muxer: Broken pipe")
    sys.exit(1)
if mode == "pipe_early":  # Broken pipe before ever going live
    err("[out#0/mpegts @ 0x1] Error writing header: Broken pipe")
    sys.exit(1)
if mode == "stall_resume":   # video stops (OBS not reading), then flows again
    progress()
    time.sleep(cfg["pause"])
if mode == "stall":      # one progress block, then silence: a printer that vanished
    progress()
    time.sleep(60)
if mode == "frozen":     # progress blocks keep coming but the timestamp never moves
    while True:
        print("out_time_us=1000\nprogress=continue", flush=True)
        time.sleep(0.05)
if mode == "moving":     # a healthy stream that prints timestamps
    n = 0
    while True:
        n += 1
        print(f"out_time_us={n * 1000}\nprogress=continue", flush=True)
        time.sleep(0.05)
if mode == "stubborn":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
else:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(255))
while True:
    progress()
    time.sleep(0.05)
'''


def wait_for(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir()
        self.ffmpeg = os.path.join(self.dir, "ffmpeg")
        with open(self.ffmpeg, "w") as f:
            f.write(f"#!{sys.executable}\n{FAKE_FFMPEG}")
        os.chmod(self.ffmpeg, 0o755)
        self.runtime = os.path.join(self.dir, "run")
        os.mkdir(self.runtime, 0o700)
        self.control = os.path.join(self.dir, "control.json")
        self.log = os.path.join(self.dir, "runs.jsonl")
        self.mode("live")
        env = mock.patch.dict(os.environ, {"FAKE_FFMPEG_CONTROL": self.control,
                                           "XDG_RUNTIME_DIR": self.runtime})
        env.start()
        self.addCleanup(env.stop)
        self.cert = os.path.join(self.dir, "state", f"bambu-camera-{SERIAL}.pem")
        os.mkdir(os.path.dirname(self.cert), 0o700)
        fetch = mock.patch.object(camera.ssl, "get_server_certificate", return_value=PEM)
        self.fetch = fetch.start()
        self.addCleanup(fetch.stop)
        self.relays = []

    def tearDown(self):
        for relay in self.relays:
            relay.stop()

    def mode(self, mode, **extra):
        extra.setdefault("hidden", True)
        with open(self.control, "w") as f:
            json.dump({"mode": mode, "secret": CODE, "log": self.log, **extra}, f)

    def relay(self, **kw):
        kw.setdefault("ffmpeg", self.ffmpeg)
        kw.setdefault("term_timeout", 1)
        kw.setdefault("serial", SERIAL)
        kw.setdefault("cert_file", self.cert)
        r = camera.CameraRelay(HOST, CODE, 5600, **kw)
        self.relays.append(r)
        return r

    def runs(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(line) for line in f]

    def state(self, relay, state, timeout=5.0):
        self.assertTrue(wait_for(lambda: relay.status()["state"] == state, timeout),
                        f"never reached {state!r}: {relay.status()}")


class Version(Base):
    def version_of(self, line):
        with mock.patch.dict(os.environ, {"FAKE_FFMPEG_VERSION": line}):
            return camera.ffmpeg_version(self.ffmpeg)

    def test_formats(self):
        cases = {
            "ffmpeg version 7.1.1 Copyright (c) 2000-2025 the FFmpeg developers": (7, 1),
            "ffmpeg version n7.0 Copyright (c) 2000-2024 the FFmpeg developers": (7, 0),
            "ffmpeg version 7.1-static https://johnvansickle.com/ffmpeg/": (7, 1),
            "ffmpeg version 7.1.1-1+b1 Copyright (c) 2000-2025": (7, 1),
            "ffmpeg version 6.1.1-3ubuntu5 Copyright (c) 2000-2023": (6, 1),
            "ffmpeg version 5.0.3 Copyright": (5, 0),
            "ffmpeg version 10.2 Copyright": (10, 2),
            "ffmpeg version N-118000-gabcdef0 Copyright": None,
            "something else entirely": None,
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                self.assertEqual(self.version_of(line), expected)

    def test_missing_binary(self):
        self.assertIsNone(camera.ffmpeg_version(os.path.join(self.dir, "nope")))

    def test_ffmpeg_5_0_refused(self):
        r = self.relay()
        with mock.patch.dict(os.environ, {"FAKE_FFMPEG_VERSION": "ffmpeg version 5.0.3 Copyright"}):
            r.start()
        s = r.status()
        self.assertEqual(s["state"], "unavailable")
        self.assertEqual(s["error"], "The printer camera needs ffmpeg 5.1 or newer (found 5.0)")
        self.assertEqual(self.runs(), [])
        self.assertEqual(os.listdir(self.runtime), [])   # no list file written

    def test_ffmpeg_5_1_and_7_accepted(self):
        for line in ("ffmpeg version 5.1.6-0+deb12u1 Copyright", "ffmpeg version n7.1 Copyright"):
            with self.subTest(line=line), mock.patch.dict(os.environ, {"FAKE_FFMPEG_VERSION": line}):
                r = self.relay()
                r.start()
                self.state(r, "live")
                r.stop()

    def test_missing_ffmpeg_refused(self):
        r = self.relay(ffmpeg=os.path.join(self.dir, "nope"))
        r.start()
        self.assertEqual(r.status()["state"], "unavailable")
        self.assertEqual(r.status()["error"], "ffmpeg isn't installed")
        r.stop()
        self.assertEqual(r.status()["state"], "off")


PIN = {"serial": SERIAL, "cert_file": CERT}


class Urls(unittest.TestCase):
    def test_command(self):
        self.assertEqual(camera.ffmpeg_command("/usr/bin/ffmpeg", 5600, list_file="/run/x.ffconcat"), [
            "/usr/bin/ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats",
            "-progress", "pipe:1", "-f", "concat", "-safe", "0",
            "-protocol_whitelist", "file,rtsp,rtsps,tls,tcp,udp,crypto", "-i", "/run/x.ffconcat",
            "-map", "0:v", "-c", "copy", "-f", "mpegts", "udp://127.0.0.1:5600?pkt_size=1316"])

    def test_command_with_visible_url(self):
        self.assertEqual(camera.ffmpeg_command("ffmpeg", 5600, url=URL, ca_file=CERT, serial=SERIAL), [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats",
            "-progress", "pipe:1", "-rtsp_transport", "tcp", "-timeout", "10000000", "-tls_verify", "1", "-ca_file", CERT,
            "-verifyhost", SERIAL, "-i", URL,
            "-map", "0:v", "-c", "copy", "-f", "mpegts", "udp://127.0.0.1:5600?pkt_size=1316"])

    def test_visible_url_needs_the_pin(self):
        for kw in ({}, {"ca_file": CERT}, {"serial": SERIAL}, {"ca_file": "/a'b", "serial": SERIAL},
                   {"ca_file": CERT, "serial": "AB-12"}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                camera.ffmpeg_command("ffmpeg", 5600, url=URL, **kw)

    def test_command_needs_exactly_one_source(self):
        for kw in ({}, {"list_file": "/x", "url": URL}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                camera.ffmpeg_command("ffmpeg", 5600, **kw)

    def test_ffconcat_list(self):
        self.assertEqual(camera.ffconcat_list(URL, CERT, SERIAL), list_text())
        self.assertTrue(camera.ffconcat_list("rtsps://a'b", CERT, SERIAL).startswith(
                        "ffconcat version 1.0\nfile 'rtsps://a'\\''b'\noption rtsp_transport tcp\n"))
        self.assertIn("option ca_file '/a b\\c/x.pem'\n", camera.ffconcat_list(URL, "/a b\\c/x.pem", SERIAL))
        for bad in ("rtsps://a\nfile /etc/passwd", "rtsps://a\rb", "rtsps://a\0b"):
            with self.subTest(url=bad), self.assertRaises(ValueError):
                camera.ffconcat_list(bad, CERT, SERIAL)
        for bad in ("/a'b.pem", "/a\nfile /etc/passwd", "/a\rb", "/a\0b", "", None):
            with self.subTest(cert=bad), self.assertRaises(ValueError):
                camera.ffconcat_list(URL, bad, SERIAL)
        for bad in ("", "AB 12", "AB\n12", "A" * 33, "AB-12", "ÄB12", None, 123):
            with self.subTest(serial=bad), self.assertRaises(ValueError):
                camera.ffconcat_list(URL, CERT, bad)

    def test_url_quotes_odd_codes(self):
        self.assertEqual(camera.camera_url(HOST, "a'b\\c/@:"),
                         f"rtsps://bblp:a%27b%5Cc%2F%40%3A@{HOST}:322/streaming/live/1")

    def test_obs_input_url(self):
        self.assertEqual(camera.obs_input_url(5600), "udp://127.0.0.1:5600?localaddr=127.0.0.1"
                         "&overrun_nonfatal=1&fifo_size=5000000")
        self.assertIn("1024", camera.obs_input_url(1024))
        self.assertIn("65535", camera.obs_input_url(65535))

    def test_port_validation(self):
        for bad in (0, 80, 1023, 65536, -1, "5600", 5600.0, True, None):
            with self.subTest(port=bad):
                with self.assertRaises(ValueError):
                    camera.obs_input_url(bad)
                with self.assertRaises(ValueError):
                    camera.ffmpeg_command("ffmpeg", bad, list_file="/x")
                with self.assertRaises(ValueError):
                    camera.CameraRelay(HOST, CODE, bad, **PIN)

    def test_bad_host_or_code(self):
        for host in ("", "a/b", "x@y", "a b", "host\n"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                camera.CameraRelay(host, CODE, 5600, **PIN)
        for code in ("", "abc\ndef", "abc\r", "a\0b"):
            with self.subTest(code=code), self.assertRaises(ValueError):
                camera.CameraRelay(HOST, code, 5600, **PIN)

    def test_serial_and_cert_file_are_checked(self):
        with self.assertRaises(TypeError):
            camera.CameraRelay(HOST, CODE, 5600)   # both are required
        for serial in ("", "AB 12", "AB-12", "A" * 33, "AB\n1", None):
            with self.subTest(serial=serial), self.assertRaises(ValueError):
                camera.CameraRelay(HOST, CODE, 5600, serial=serial, cert_file=CERT)
        for cert in ("", "/a'b", "/a\nb", "/a\0b", None):
            with self.subTest(cert=cert), self.assertRaises(ValueError):
                camera.CameraRelay(HOST, CODE, 5600, serial=SERIAL, cert_file=cert)
        r = camera.CameraRelay(HOST, CODE, 5600, serial="A" * 32, cert_file=CERT)
        self.assertEqual((r.serial, r.cert_file), ("A" * 32, CERT))

    def test_describe_failure(self):
        for lines in (["[tls @ 0x1] Peer certificate failed verification",
                       "[in#0 @ 0x2] Error opening input: Input/output error"],
                      ["[tls @ 0x1] The certificate's owner does not match hostname 01P00A123456789",
                       "Error opening input: Input/output error"],
                      ["[tls @ 0x1] error:0A000086:SSL routines::certificate verify failed"]):
            with self.subTest(lines=lines):
                self.assertEqual(camera.describe_failure(lines), camera.ERR_CERT)
        self.assertEqual(camera.describe_failure(["Error opening input: Input/output error"]),
                         camera.ERR_UNREACHABLE)
        self.assertEqual(camera.describe_failure(["method DESCRIBE failed: 401 Unauthorized"]),
                         camera.ERR_REFUSED)
        self.assertEqual(camera.describe_failure([]), camera.ERR_STOPPED)

    def test_checked_pem(self):
        self.assertEqual(camera.checked_pem(PEM), PEM)
        self.assertEqual(camera.checked_pem("\n" + PEM.replace("\n", "\r\n") + "\n"), PEM)
        self.assertEqual(camera.pem_fingerprint(PEM), FINGERPRINT)
        self.assertRegex(FINGERPRINT, r"^([0-9A-F]{2}:){31}[0-9A-F]{2}$")
        big = ssl.DER_cert_to_PEM_cert(b"\x30" + bytes(13000))
        not_der = ssl.DER_cert_to_PEM_cert(b"\x02\x01\x00")
        key = PEM.replace("CERTIFICATE", "PRIVATE KEY")
        for bad in ("", None, b"x", PEM + PEM, "junk\n" + PEM, PEM + "junk", big, not_der, key,
                    PEM.replace("MI", "M!", 1)):
            with self.subTest(pem=bad), self.assertRaises(ValueError):
                camera.checked_pem(bad)

    def test_repr_hides_code(self):
        self.assertNotIn(CODE, repr(camera.CameraRelay(HOST, CODE, 5600, **PIN)))


class Links(unittest.TestCase):
    def setUp(self):
        self.runtime = tmpdir()
        os.chmod(self.runtime, 0o700)
        env = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.runtime})
        env.start()
        self.addCleanup(env.stop)

    def test_socket_path_is_named_by_port_in_the_private_folder(self):
        path = camera.socket_path(5004)
        self.assertEqual(path, os.path.join(self.runtime, "ultimate-streaming", "bambu-camera-5004.sock"))
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
        self.assertNotEqual(camera.socket_path(5005), path)   # two printers, two sockets
        with self.assertNoLogs("deck", "WARNING"):
            link = camera.choose_link("socket", 5004)
        self.assertEqual(link, ("socket", path, None, None))
        self.assertEqual(link.output_url(5004), f"unix:{path}")
        self.assertEqual(link.obs_url(5004), f"unix:{path}")

    def check_udp(self, warning, **kw):
        with self.assertLogs("deck", "WARNING") as logs:
            link = camera.choose_link("socket", 5004, **kw)
        self.assertEqual(link, ("udp", None, warning, None))
        self.assertEqual(sum(warning in line for line in logs.output), 1, logs.output)
        self.assertEqual(link.obs_url(5004), camera.obs_input_url(5004))
        self.assertTrue(link.output_url(5004).startswith("udp://127.0.0.1:5004"))

    def test_too_long_falls_back_to_udp(self):
        # <runtime>/<a...>/ultimate-streaming/bambu-camera-5004.sock: make it 106 bytes, then 107.
        tail = len(os.fsencode(os.path.join("ultimate-streaming", "bambu-camera-5004.sock")))
        fits = os.path.join(self.runtime, "a" * (camera.SUN_PATH_MAX - 3 - tail - len(self.runtime)))
        long = fits + "a"
        for d in (fits, long):
            os.mkdir(d, 0o700)
        os.environ["XDG_RUNTIME_DIR"] = fits
        link = camera.choose_link("socket", 5004)
        self.assertEqual(len(os.fsencode(link.socket_path)), camera.SUN_PATH_MAX - 1)
        os.environ["XDG_RUNTIME_DIR"] = long
        self.check_udp(camera.WARN_TOO_LONG)
        with self.assertRaises(ValueError):
            camera.output_url(5004, os.path.join(long, "ultimate-streaming", "bambu-camera-5004.sock"))

    def check_unsafe(self, path, reason):
        with self.assertLogs("deck", "WARNING") as logs:
            link = camera.choose_link("socket", 5004)
        error = (f"The camera's private folder ({path}) isn't safe: {reason}. Fix its owner or "
                 "permissions; the camera stays off until then.")
        self.assertEqual(link, ("socket", None, None, error))   # never a quiet fall back to UDP
        self.assertEqual(sum(error in line for line in logs.output), 1, logs.output)
        self.assertNotIn("UDP", " ".join(logs.output))
        for url in (link.obs_url, link.output_url):
            with self.assertRaisesRegex(RuntimeError, "isn't safe"):
                url(5004)

    def test_unsafe_runtime_dir_turns_the_camera_off(self):
        os.environ.pop("XDG_RUNTIME_DIR")
        self.check_unsafe("$XDG_RUNTIME_DIR", "it isn't set to an absolute path")
        os.environ["XDG_RUNTIME_DIR"] = "relative/run"
        self.check_unsafe("$XDG_RUNTIME_DIR", "it isn't set to an absolute path")
        missing = os.path.join(self.runtime, "missing")
        os.environ["XDG_RUNTIME_DIR"] = missing
        self.check_unsafe(missing, "No such file or directory")
        os.environ["XDG_RUNTIME_DIR"] = self.runtime
        os.chmod(self.runtime, 0o777)    # others could swap our folder for theirs
        self.check_unsafe(self.runtime, "other users can write to it")
        os.chmod(self.runtime, 0o720)
        self.check_unsafe(self.runtime, "other users can write to it")
        os.chmod(self.runtime, 0o700)
        with mock.patch.object(camera.os, "getuid", return_value=os.getuid() + 1):
            self.check_unsafe(self.runtime, "it belongs to another user")

    def test_unsafe_private_folder_turns_the_camera_off(self):
        ours = os.path.join(self.runtime, "ultimate-streaming")
        elsewhere = tmpdir()
        os.symlink(elsewhere, ours)
        self.check_unsafe(ours, "it is a symlink")
        os.unlink(ours)
        with open(ours, "w"):
            pass
        self.check_unsafe(ours, "it isn't a folder")
        os.unlink(ours)
        os.mkdir(ours, 0o755)
        real_lstat = os.lstat

        def lstat(path, *a, **kw):   # as if another user had made it
            st = real_lstat(path, *a, **kw)
            if path == ours:
                return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid + 1,
                                       *tuple(st)[5:10]))
            return st
        with mock.patch.object(camera.os, "lstat", lstat):
            self.check_unsafe(ours, "it belongs to another user")
        link = camera.choose_link("socket", 5004)          # ours again: tightened to 0700, used
        self.assertEqual(link.socket_path, os.path.join(ours, "bambu-camera-5004.sock"))
        self.assertEqual(stat.S_IMODE(os.stat(ours).st_mode), 0o700)

    def test_relay_with_no_safe_link_stays_off(self):
        error = camera.ERR_SOCKET.format(path="/run/x", reason="it belongs to another user")
        r = camera.CameraRelay(HOST, CODE, 5600, ffmpeg="/nonexistent/ffmpeg", serial=SERIAL,
                               cert_file=CERT, link_error=error)
        r.start()
        s = r.status()
        self.assertEqual((s["state"], s["error"]), ("unavailable", error))
        self.assertIsNone(r._thread)
        r.stop()

    def test_flatpak_obs_falls_back_to_udp(self):
        self.check_udp(camera.WARN_FLATPAK, obs_flatpak=True)
        self.assertEqual(camera.WARN_FLATPAK, "The camera uses UDP because OBS is a Flatpak; "
                                              "other local users could inject video.")

    def test_udp_chosen(self):
        with self.assertNoLogs("deck", "WARNING"):
            self.assertEqual(camera.choose_link("udp", 5004, obs_flatpak=True), ("udp", None, None, None))
        with self.assertRaises(ValueError):
            camera.choose_link("tcp", 5004)
        with self.assertRaises(ValueError):
            camera.choose_link("socket", 80)

    def test_stale_socket_removed_only_when_ours(self):
        path = camera.socket_path(5004)
        camera._clear_stale_socket(path)                  # nothing there: fine
        with socket.socket(socket.AF_UNIX) as s:
            s.bind(path)
        self.assertTrue(stat.S_ISSOCK(os.lstat(path).st_mode))
        with mock.patch.object(camera.os, "getuid", return_value=os.lstat(path).st_uid + 1):
            with self.assertRaises(OSError):
                camera._clear_stale_socket(path)          # someone else's socket stays
        self.assertTrue(os.path.lexists(path))
        camera._clear_stale_socket(path)
        self.assertFalse(os.path.lexists(path))

        with open(path, "w") as f:                        # a regular file stays
            f.write("keep")
        with self.assertRaises(OSError):
            camera._clear_stale_socket(path)
        self.assertTrue(os.path.isfile(path))
        os.unlink(path)

        target = os.path.join(self.runtime, "other.sock")   # a symlink to a socket: neither goes
        with socket.socket(socket.AF_UNIX) as s:
            s.bind(target)
        os.symlink(target, path)
        with self.assertRaises(OSError):
            camera._clear_stale_socket(path)
        self.assertTrue(os.path.islink(path) and stat.S_ISSOCK(os.lstat(target).st_mode))

    def test_ffmpeg_command_for_both_transports(self):
        path = camera.socket_path(5004)
        cmd = camera.ffmpeg_command("ffmpeg", 5004, list_file="/run/l.ffconcat", socket_path=path)
        self.assertEqual(cmd[cmd.index("-c"):], ["-c", "copy", "-f", "mpegts", "-listen", "1", f"unix:{path}"])
        cmd = camera.ffmpeg_command("ffmpeg", 5004, url=URL, ca_file=CERT, serial=SERIAL, socket_path=path)
        self.assertEqual(cmd[cmd.index("-c"):], ["-c", "copy", "-f", "mpegts", "-listen", "1", f"unix:{path}"])
        cmd = camera.ffmpeg_command("ffmpeg", 5004, list_file="/run/l.ffconcat")
        self.assertEqual(cmd[cmd.index("-c"):], ["-c", "copy", "-f", "mpegts", "udp://127.0.0.1:5004?pkt_size=1316"])
        self.assertNotIn("-listen", cmd)

    def test_obs_url_for_both_transports(self):
        path = camera.socket_path(5004)
        self.assertEqual(camera.obs_input_url(5004, path), f"unix:{path}")
        self.assertEqual(camera.obs_input_url(5004),
                         "udp://127.0.0.1:5004?localaddr=127.0.0.1&overrun_nonfatal=1&fifo_size=5000000")
        for bad in ("relative.sock", "/run/a\nb.sock", "/run/a\0b.sock"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                camera.obs_input_url(5004, bad)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg and ffprobe")
class EndToEnd(unittest.TestCase):
    """The relay's real command (concat list in, -listen 1 unix: out) on a generated test
    picture instead of the printer, read back the way OBS reads it."""

    def test_socket_relay_carries_video(self):
        runtime = tmpdir()
        os.chmod(runtime, 0o700)
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": runtime}):
            link = camera.choose_link("socket", 5600)
        self.assertEqual(link.transport, "socket")
        clip = os.path.join(runtime, "test.ts")
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                        "-i", "testsrc=size=320x240:rate=25", "-t", "4", "-c:v", "mpeg2video",
                        "-f", "mpegts", clip], check=True, timeout=60, stdin=subprocess.DEVNULL)
        listing = os.path.join(runtime, "test.ffconcat")
        with open(listing, "w") as f:
            f.write(f"ffconcat version 1.0\nfile {camera._quote(clip)}\n")
        cmd = camera.ffmpeg_command("ffmpeg", 5600, list_file=listing, socket_path=link.socket_path)
        relay = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE)
        try:
            self.assertTrue(wait_for(lambda: os.path.exists(link.socket_path), 15),
                            "ffmpeg never listened")
            self.assertTrue(stat.S_ISSOCK(os.lstat(link.socket_path).st_mode))
            self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(link.socket_path)).st_mode), 0o700)
            probe = subprocess.run(["ffprobe", "-v", "error", "-f", "mpegts", "-show_entries",
                                    "stream=codec_type,width,height", "-of", "json", link.obs_url(5600)],
                                   capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
            self.assertEqual(probe.returncode, 0, probe.stderr)
            streams = json.loads(probe.stdout)["streams"]
            video = [st for st in streams if st.get("codec_type") == "video"]
            self.assertEqual([(st["width"], st["height"]) for st in video], [(320, 240)], streams)
        finally:
            relay.kill()
            relay.wait(10)
            relay.stderr.close()


class Relay(Base):
    def test_live_and_list_file(self):
        r = self.relay()
        with self.assertNoLogs("deck", "WARNING"):
            r.start()
            self.state(r, "live")
        [run] = self.runs()
        self.assertEqual(run["problems"], [])
        self.assertNotIn(CODE, " ".join(run["argv"]))
        i = run["argv"].index("-i")
        self.assertEqual(run["argv"][i - 6:i + 2], [
            "-f", "concat", "-safe", "0", "-protocol_whitelist",
            "file,rtsp,rtsps,tls,tcp,udp,crypto", "-i", run["list_file"]])
        self.assertEqual(run["list"], list_text(self.cert))
        self.assertEqual(run["pinned"], PEM)
        self.assertEqual(run["url"], URL)
        self.assertEqual(run["file_mode"], 0o600)
        self.assertEqual(run["dir_mode"], 0o700)
        self.assertEqual(os.path.dirname(run["list_file"]),
                         os.path.join(self.runtime, "ultimate-streaming"))
        self.assertEqual(run["argv"][-1], "udp://127.0.0.1:5600?pkt_size=1316")

        s = r.status()
        self.assertEqual((s["error"], s["port"]), (None, 5600))
        self.assertEqual(s["live_seconds"], 0)
        self.assertTrue(wait_for(lambda: r.status()["live_seconds"] == 1, 3))

        r.stop()
        self.assertEqual(r.status(), {"state": "off", "error": None, "detail": None,
                                      "live_seconds": None, "port": 5600})
        self.assertFalse(os.path.exists(run["list_file"]))
        self.assertFalse(alive(run["pid"]))

    def test_start_is_idempotent(self):
        r = self.relay()
        r.start()
        self.state(r, "live")
        r.start()
        time.sleep(0.2)
        self.assertEqual(len(self.runs()), 1)
        self.assertEqual(r.status()["state"], "live")

    def test_restart_after_stop_uses_new_file(self):
        r = self.relay()
        r.start()
        self.state(r, "live")
        r.stop()
        r.start()
        self.state(r, "live")
        first, second = self.runs()
        self.assertNotEqual(first["list_file"], second["list_file"])
        self.assertFalse(os.path.exists(first["list_file"]))

    def test_state_dir_fallback(self):
        os.environ.pop("XDG_RUNTIME_DIR")
        r = self.relay()
        r.start()
        self.state(r, "live")
        [run] = self.runs()
        self.assertEqual(os.path.dirname(run["list_file"]), os.environ["DECK_STATE_DIR"])
        self.assertEqual((run["file_mode"], run["dir_mode"]), (0o600, 0o700))
        r.stop()
        self.assertFalse(os.path.exists(run["list_file"]))

    def test_visible_access_code_fallback(self):
        self.mode("live", hidden=False)
        r = self.relay(hide_access_code=False)
        with self.assertLogs("deck", "WARNING") as logs:
            r.start()
        self.assertEqual(logs.output, ["WARNING:deck:camera: the access code is visible to other local "
                                       "users in the ffmpeg command line (bambu.camera.hide_access_code is off)"])
        self.state(r, "live")
        [run] = self.runs()
        self.assertEqual(run["problems"], [])
        i = run["argv"].index("-i")
        self.assertEqual(run["argv"][i - 10:i + 2], [
            "-rtsp_transport", "tcp", "-timeout", "10000000", "-tls_verify", "1", "-ca_file",
            self.cert, "-verifyhost", SERIAL, "-i", URL])
        self.assertEqual(run["pinned"], PEM)
        self.assertNotIn("concat", run["argv"])
        self.assertEqual(os.listdir(self.runtime), [])   # no list file
        r.stop()

    def test_fallback_failure_is_redacted(self):
        self.mode("fail", hidden=False)
        r = self.relay(hide_access_code=False, retry_delays=(30,))
        with self.assertLogs("deck", "WARNING") as logs:
            r.start()
            self.state(r, "retrying")
            self.assertTrue(wait_for(lambda: any("retrying in" in o for o in logs.output)))
        self.assertEqual(r.status()["error"], "Couldn't reach the printer's camera.")
        self.assertNotIn(CODE, json.dumps(r.status()))
        self.assertIn("<access code>", "\n".join(logs.output))
        self.assertNotIn(CODE, "\n".join(logs.output))

    def test_unreachable_is_redacted(self):
        self.mode("fail")
        r = self.relay(retry_delays=(30,))
        r.start()
        self.state(r, "retrying")
        s = r.status()
        self.assertEqual(s["error"], "Couldn't reach the printer's camera.")
        self.assertIsNone(s["live_seconds"])
        self.assertEqual(len(s["detail"].splitlines()), 3)
        self.assertIn("Error opening input file rtsps://<access code>@192.168.1.50:322", s["detail"])
        self.assertIn("access code <access code> was in", s["detail"])
        self.assertNotIn(CODE, json.dumps(s))
        self.assertNotIn("bblp:", json.dumps(s))

    def test_refused(self):
        self.mode("auth")
        r = self.relay(retry_delays=(30,))
        r.start()
        self.state(r, "retrying")
        s = r.status()
        self.assertEqual(s["error"],
                         "The printer refused the camera connection (check the access code).")
        self.assertNotIn(CODE, json.dumps(s))

    def test_drop_after_live(self):
        self.mode("live_for", seconds=0.3)
        r = self.relay(retry_delays=(30,))
        r.start()
        self.state(r, "live")
        self.state(r, "retrying")
        s = r.status()
        self.assertIsNone(s["live_seconds"])
        self.assertEqual(s["error"], "Couldn't reach the printer's camera.")
        self.assertIn("Connection reset", s["detail"])

    def test_stop_during_retry_is_prompt(self):
        self.mode("fail")
        r = self.relay(retry_delays=(30,))
        r.start()
        self.state(r, "retrying")
        [run] = self.runs()
        began = time.monotonic()
        r.stop()
        self.assertLess(time.monotonic() - began, 1.0)
        self.assertEqual(r.status()["state"], "off")
        self.assertFalse(os.path.exists(run["list_file"]))
        self.assertEqual(os.listdir(os.path.join(self.runtime, "ultimate-streaming")), [])

    def test_stubborn_ffmpeg_is_killed(self):
        self.mode("stubborn")
        r = self.relay(term_timeout=0.3)
        r.start()
        self.state(r, "live")
        [run] = self.runs()
        began = time.monotonic()
        r.stop()
        self.assertLess(time.monotonic() - began, 2.0)
        self.assertFalse(alive(run["pid"]))

    def test_timeout_option_in_both_paths(self):
        self.assertIn("option timeout 10000000\n", camera.ffconcat_list(URL, CERT, SERIAL))
        cmd = camera.ffmpeg_command("ffmpeg", 5600, url=URL, ca_file=CERT, serial=SERIAL)
        self.assertEqual(cmd[cmd.index("-timeout") + 1], "10000000")
        self.assertLess(cmd.index("-timeout"), cmd.index("-i"))

    def test_watchdog_kills_a_stalled_ffmpeg(self):
        self.mode("stall")
        r = self.relay(stall_timeout=0.4, retry_delays=(30,))
        r.start()
        self.state(r, "live")
        [run] = self.runs()
        self.state(r, "retrying", timeout=5)
        st = r.status()
        self.assertEqual(st["error"], camera.ERR_STALLED)
        self.assertIsNone(st["live_seconds"])
        self.assertTrue(wait_for(lambda: not alive(run["pid"])))
        r.stop()

    def test_udp_stall_doesnt_ask_obs(self):
        self.mode("stall")
        stalls = []
        r = self.relay(stall_timeout=0.4, retry_delays=(30,), on_stall=stalls.append)
        r.start()
        self.state(r, "live")
        self.state(r, "retrying", timeout=5)
        self.assertEqual(stalls, [])

    def test_socket_stall_asks_obs_first_then_kills(self):
        self.mode("stall")
        stalls, listening = [], []
        r = self.relay(socket_path=self.sock(), stall_timeout=0.4, retry_delays=(30,),
                       on_stall=stalls.append, on_listening=listening.append)
        r.start()
        self.state(r, "live")
        began = time.monotonic()
        self.assertTrue(wait_for(lambda: stalls == [r], 3))
        self.assertEqual(r.status()["state"], "live")          # one more period first
        self.state(r, "retrying", timeout=5)
        self.assertGreater(time.monotonic() - began, 0.75)    # two stall periods, not one
        self.assertEqual(r.status()["error"], camera.ERR_STALLED)
        self.assertEqual(stalls, [r])                          # asked once, then killed
        self.assertEqual(listening, [r])

    def test_socket_stall_that_recovers_isnt_killed(self):
        self.mode("stall_resume", pause=0.7)
        stalls = []
        r = self.relay(socket_path=self.sock(), stall_timeout=0.4, retry_delays=(30,),
                       on_stall=stalls.append)
        r.start()
        self.state(r, "live")
        self.assertTrue(wait_for(lambda: stalls == [r], 3))
        time.sleep(1.5)
        self.assertEqual(r.status()["state"], "live")
        self.assertEqual(len(self.runs()), 1)
        self.assertEqual(stalls, [r])

    def test_watchdog_kills_frozen_timestamps_but_not_moving_ones(self):
        self.mode("frozen")
        r = self.relay(stall_timeout=0.4, retry_delays=(30,))
        r.start()
        self.state(r, "retrying", timeout=5)
        self.assertEqual(r.status()["error"], camera.ERR_STALLED)
        r.stop()
        self.mode("moving")
        r = self.relay(stall_timeout=0.4, retry_delays=(30,))
        r.start()
        self.state(r, "live")
        time.sleep(1.2)
        self.assertEqual(r.status()["state"], "live")
        r.stop()

    def test_retries_forever_at_last_delay(self):
        self.mode("fail")
        r = self.relay(retry_delays=(0.05, 0.1))
        r.start()
        self.assertTrue(wait_for(lambda: len(self.runs()) >= 5, 5), self.runs())
        self.assertIn(r.status()["state"], ("retrying", "starting"))

    def test_backoff_grows_without_long_live_runs(self):
        # Short live runs don't reset the sequence: the second delay (30 s) holds the third run.
        self.mode("live_for", seconds=0.1)
        r = self.relay(retry_delays=(0.1, 30), live_reset=5)
        r.start()
        self.assertTrue(wait_for(lambda: len(self.runs()) == 2, 5))
        self.state(r, "retrying")
        time.sleep(1.0)
        self.assertEqual(len(self.runs()), 2)

    def sock(self):
        return camera.socket_path(5600)

    def test_socket_relay_listens_and_clears_a_stale_socket(self):
        path = self.sock()
        with socket.socket(socket.AF_UNIX) as s:      # left by an ffmpeg that was killed
            s.bind(path)
        r = self.relay(socket_path=path)
        with self.assertNoLogs("deck", "WARNING"):
            r.start()
            self.state(r, "live")
        [run] = self.runs()
        self.assertEqual(run["problems"], [])
        self.assertEqual(run["argv"][-4:], ["mpegts", "-listen", "1", f"unix:{path}"])
        self.assertIs(run["socket_there"], False)

    def test_socket_in_the_way_isnt_removed(self):
        path = self.sock()
        with open(path, "w") as f:
            f.write("not ours to delete")
        r = self.relay(socket_path=path, retry_delays=(30,))
        r.start()
        self.state(r, "retrying")
        self.assertEqual(r.status()["error"], camera.ERR_STALE_SOCKET)
        self.assertEqual(self.runs(), [])
        with open(path) as f:
            self.assertEqual(f.read(), "not ours to delete")

    def test_obs_disconnect_relistens_at_once(self):
        # Every run goes live and ends with OBS leaving: each one is followed straight away by
        # the next, never by the 30 s backoff, and never shown as a failure.
        self.mode("obs_left")
        r = self.relay(socket_path=self.sock(), retry_delays=(30,), live_reset=3600)
        seen = set()
        with self.assertNoLogs("deck", "WARNING"):
            r.start()
            self.assertTrue(wait_for(lambda: seen.add(r.status()["state"]) or len(self.runs()) >= 4, 8),
                            self.runs())
        self.assertNotIn("retrying", seen)
        self.assertIsNone(r.status()["error"])
        r.stop()

        class Stopping:   # the supervisor's stop Event, recording each wait between runs
            def __init__(self):
                self.waits = []

            def is_set(self):
                return len(self.waits) >= 5

            def wait(self, delay):
                self.waits.append(delay)
                return self.is_set()

        stopping = Stopping()
        with mock.patch.object(r, "_run_once", return_value=(0.2, True)) as run_once:
            r._supervise(stopping, ["ffmpeg"], None)
        self.assertEqual(stopping.waits, [camera.RELISTEN_DELAY] * 5)   # no backoff, no growth
        self.assertEqual(run_once.call_count, 5)

    def test_on_listening_once_per_listening_ffmpeg(self):
        listening = []
        r = self.relay(socket_path=self.sock(), on_listening=listening.append)
        self.assertFalse(r.listening())
        r.start()
        self.state(r, "live")
        self.assertTrue(wait_for(lambda: listening == [r], 3))
        self.assertTrue(r.listening())
        r.stop()
        self.assertFalse(r.listening())
        # Each relisten after OBS leaves is announced too.
        self.mode("obs_left")
        listening.clear()
        r = self.relay(socket_path=self.sock(), on_listening=listening.append, live_reset=3600)
        r.start()
        self.assertTrue(wait_for(lambda: len(self.runs()) >= 4, 8))
        r.stop()
        self.assertGreaterEqual(len(listening), len(self.runs()) - 2)   # the last may be mid-start
        self.assertTrue(all(x is r for x in listening))

    def test_on_listening_needs_the_socket(self):
        self.mode("live", no_listen=True)
        listening = []
        with mock.patch.object(camera, "LISTEN_WAIT", 0.3):
            r = self.relay(socket_path=self.sock(), on_listening=listening.append)
            r.start()
            self.state(r, "live")
            time.sleep(0.6)
        self.assertEqual(listening, [])
        self.assertFalse(r.listening())

    def test_on_listening_never_over_udp_and_a_failing_callback_is_contained(self):
        listening = []
        r = self.relay(on_listening=listening.append)
        r.start()
        self.state(r, "live")
        time.sleep(0.3)
        self.assertEqual(listening, [])
        self.assertFalse(r.listening())
        r.stop()

        def boom(relay):
            raise RuntimeError("owner broke")
        r = self.relay(socket_path=self.sock(), on_listening=boom)
        with self.assertLogs("deck", "ERROR"):
            r.start()
            self.state(r, "live")
            time.sleep(0.3)
        self.assertEqual(r.status()["state"], "live")

    def test_broken_pipe_without_going_live_backs_off(self):
        self.mode("pipe_early")
        r = self.relay(socket_path=self.sock(), retry_delays=(30,))
        r.start()
        self.state(r, "retrying")
        time.sleep(0.5)
        self.assertEqual(len(self.runs()), 1)

    def test_broken_pipe_over_udp_backs_off(self):
        self.mode("obs_left")
        r = self.relay(retry_delays=(30,))
        r.start()
        self.state(r, "retrying")
        time.sleep(0.5)
        self.assertEqual(len(self.runs()), 1)

    def test_live_reset_resets_backoff(self):
        # Every run stays live past live_reset, so each retry waits the first delay again.
        self.mode("live_for", seconds=0.4)
        r = self.relay(retry_delays=(0.1, 30), live_reset=0.2)
        r.start()
        self.assertTrue(wait_for(lambda: len(self.runs()) >= 4, 8), self.runs())


class Pinning(Base):
    def test_pinned_once_off_the_callers_thread_and_reused(self):
        threads = []
        self.fetch.side_effect = lambda *a, **kw: threads.append(threading.current_thread()) or PEM
        self.mode("live_for", seconds=0.1)
        r = self.relay(retry_delays=(0.05,))
        with self.assertLogs("deck", "INFO") as logs:
            r.start()
            self.assertTrue(wait_for(lambda: len(self.runs()) >= 3), self.runs())
        self.fetch.assert_called_once_with((HOST, 322), timeout=camera.CERT_TIMEOUT)
        self.assertNotIn(threading.current_thread(), threads)
        self.assertIn(f"INFO:deck:camera: trusted the printer's certificate (sha256 {FINGERPRINT})",
                      logs.output)
        self.assertTrue(all(run["pinned"] == PEM for run in self.runs()))
        st = os.stat(self.cert)
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)

    def test_existing_pin_is_used_without_fetching(self):
        with open(self.cert, "w") as f:
            f.write("saved pin")
        r = self.relay()
        r.start()
        self.state(r, "live")
        self.fetch.assert_not_called()
        self.assertEqual(self.runs()[0]["pinned"], "saved pin")

    def test_fetch_failure_retries_as_unreachable(self):
        self.fetch.side_effect = ConnectionRefusedError(111, "Connection refused")
        r = self.relay(retry_delays=(0.05,))
        r.start()
        self.state(r, "retrying")
        s = r.status()
        self.assertEqual(s["error"], camera.ERR_UNREACHABLE)
        self.assertIn("certificate", s["detail"])
        self.assertTrue(wait_for(lambda: self.fetch.call_count >= 3))   # the normal backoff
        self.assertEqual(self.runs(), [])   # ffmpeg never ran without a pin
        self.assertFalse(os.path.exists(self.cert))

        self.fetch.side_effect = None   # the printer comes back
        self.state(r, "live")
        self.assertEqual(self.runs()[0]["pinned"], PEM)

    def test_bad_certificate_is_not_saved(self):
        self.fetch.return_value = PEM + PEM
        r = self.relay(retry_delays=(30,))
        r.start()
        self.state(r, "retrying")
        self.assertEqual(r.status()["error"], camera.ERR_UNREACHABLE)
        self.assertFalse(os.path.exists(self.cert))
        self.assertEqual(self.runs(), [])

    def test_unwritable_pin(self):
        r = self.relay(retry_delays=(30,), cert_file=os.path.join(self.dir, "missing", "x.pem"))
        r.start()
        self.state(r, "retrying")
        self.assertEqual(r.status()["error"], camera.ERR_PRIVATE_FILE)
        self.assertEqual(self.runs(), [])

    def test_certificate_mismatch(self):
        self.mode("cert")
        r = self.relay(retry_delays=(30,))
        r.start()
        self.state(r, "retrying")
        s = r.status()
        self.assertEqual(s["error"], camera.ERR_CERT)
        self.assertIn("Peer certificate failed verification", s["detail"])
        self.assertTrue(os.path.exists(self.cert))   # never replaced on its own

    def test_forget_then_repin(self):
        r = self.relay()
        r.start()
        self.state(r, "live")
        r.stop()
        with self.assertLogs("deck", "INFO") as logs:
            r.forget_certificate()
        self.assertIn("forgot", logs.output[0])
        self.assertFalse(os.path.exists(self.cert))
        r.forget_certificate()   # nothing to forget is fine
        self.fetch.return_value = PEM2
        r.start()
        self.state(r, "live")
        self.assertEqual(self.fetch.call_count, 2)
        self.assertEqual([run["pinned"] for run in self.runs()], [PEM, PEM2])

    def test_stopped_run_doesnt_write_its_pin_back(self):
        fetching, release = threading.Event(), threading.Event()

        def slow_fetch(*a, **kw):
            fetching.set()
            release.wait(5)
            return PEM
        self.fetch.side_effect = slow_fetch
        r = self.relay(term_timeout=0.1)
        r.start()
        self.assertTrue(fetching.wait(5))
        r.stop()   # the thread is still fetching after the join times out
        r.forget_certificate()
        release.set()
        time.sleep(0.3)
        self.assertFalse(os.path.exists(self.cert))
        self.assertEqual(self.runs(), [])


if __name__ == "__main__":
    unittest.main()
