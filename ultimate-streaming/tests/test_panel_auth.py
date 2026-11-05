"""How the control panel is opened without its token ever being in a URL or on a command line:
launch codes, POST /api/session, token rotation, who holds the port, the bind retry; and the
shell-free / bounded helpers in core.util. Runs the real daemon (minus the hardware)."""
import json
import logging
import os
import socket
import stat
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from tests.helpers import EXAMPLE, ROOT, Deck, free_port, tmpdir

import yaml  # noqa: E402

from core import editor_auth, util  # noqa: E402
from core.bot import DeckBot  # noqa: E402
from core.webapi.server import FailureLimit  # noqa: E402

PORT = free_port()
STATE = os.environ["DECK_STATE_DIR"]
ME = os.getuid()


def setUpModule():
    global BOT, ED, BASE
    config = os.path.join(tmpdir(), "config.yaml")
    with open(EXAMPLE) as f:
        cfg = yaml.safe_load(f)
    cfg["editor"] = {"port": PORT}
    with open(config, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)
    BOT = DeckBot(root=ROOT, config_path=config)
    BOT.deck = Deck("Stream Deck XL")
    BOT.load_config(initial=True)
    BOT.load_extensions("modules")
    ED = next(m for m in BOT.modules.values() if type(m).__name__ == "Editor")
    BASE = f"http://127.0.0.1:{PORT}"


def tearDownModule():
    for inst in list(BOT.modules.values()):
        try:
            inst.on_unload()
        except Exception:
            pass


def call(path, body=None, headers=None, token=None, base=None):
    h = {"X-Deck-Token": token} if token else {}
    data = None
    if body is not None:
        data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
    h.update(headers or {})
    req = urllib.request.Request((base or BASE) + path, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        with e:
            return e.code, json.loads(e.read() or b"{}")


def code_of(url):
    assert "#code=" in url and "token" not in url, url
    return url.split("#code=", 1)[1]


def hexaddr(ip, port):
    """An address the way /proc/net/tcp{,6} prints it (32-bit words in host byte order)."""
    fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
    raw = socket.inet_pton(fam, ip)
    words = "".join(f"{int.from_bytes(raw[i:i + 4], sys.byteorder):08X}" for i in range(0, len(raw), 4))
    return f"{words}:{port:04X}"


def proc_file(rows):
    """A fake /proc/net/tcp: rows of (local ip, local port, remote ip, remote port, state, uid)."""
    path = os.path.join(tmpdir(), "tcp")
    with open(path, "w") as f:
        f.write("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n")
        for i, (lip, lp, rip, rp, st, uid) in enumerate(rows):
            f.write(f"{i:4}: {hexaddr(lip, lp)} {hexaddr(rip, rp)} {st} 00000000:00000000 00:00000000 "
                    f"00000000 {uid:5}        0 {1000 + i} 1 0000000000000000 100 0 0 10 0\n")
    return path


LAUNCH = os.path.join(STATE, "launch")


class LaunchCodes(unittest.TestCase):
    def test_single_use(self):
        code = editor_auth.new_launch_code()
        self.assertTrue(editor_auth.redeem_launch_code(code))
        self.assertFalse(editor_auth.redeem_launch_code(code))

    def test_only_the_hash_is_stored_privately(self):
        code = editor_auth.new_launch_code()
        self.assertEqual(stat.S_IMODE(os.stat(LAUNCH).st_mode), 0o700)
        names = os.listdir(LAUNCH)
        self.assertNotIn(code, names)
        digest = editor_auth._digest(code)
        self.assertIn(digest, names)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(LAUNCH, digest)).st_mode), 0o600)
        editor_auth.redeem_launch_code(code)

    def test_expired(self):
        code = editor_auth.new_launch_code()
        path = os.path.join(LAUNCH, editor_auth._digest(code))
        old = time.time() - editor_auth.LAUNCH_TTL - 1
        os.utime(path, (old, old))
        self.assertFalse(editor_auth.redeem_launch_code(code))
        self.assertFalse(os.path.exists(path))

    def test_expired_files_are_swept(self):
        stale = editor_auth.new_launch_code()
        path = os.path.join(LAUNCH, editor_auth._digest(stale))
        old = time.time() - editor_auth.LAUNCH_TTL - 1
        os.utime(path, (old, old))
        fresh = editor_auth.new_launch_code()
        self.assertFalse(editor_auth.redeem_launch_code("not a real code"))
        self.assertFalse(os.path.exists(path))
        self.assertTrue(editor_auth.redeem_launch_code(fresh))

    def test_junk_refused(self):
        for junk in (None, "", 5, ["x"], "x" * 1000):
            self.assertFalse(editor_auth.redeem_launch_code(junk))

    def test_url_carries_a_code_not_the_token(self):
        url = editor_auth.url({"editor": {"port": PORT}})
        self.assertTrue(url.startswith(f"http://127.0.0.1:{PORT}/#code="))
        self.assertNotIn(ED.token, url)
        self.assertTrue(editor_auth.redeem_launch_code(code_of(url)))


class Session(unittest.TestCase):
    def setUp(self):
        ED.server.session_limit = FailureLimit()

    def test_code_for_token(self):
        status, body = call("/api/session", {"code": code_of(editor_auth.url(BOT.cfg))})
        self.assertEqual(status, 200)
        self.assertEqual(body, {"token": ED.token})
        self.assertEqual(call("/api/status", token=body["token"])[0], 200)

    def test_used_or_wrong_code(self):
        code = code_of(editor_auth.url(BOT.cfg))
        self.assertEqual(call("/api/session", {"code": code})[0], 200)
        self.assertEqual(call("/api/session", {"code": code})[0], 403)
        self.assertEqual(call("/api/session", {"code": "nope"})[0], 403)
        self.assertEqual(call("/api/session", {})[0], 403)
        self.assertNotIn("token", call("/api/session", {"code": "nope"})[1])

    def test_host_and_origin_still_checked(self):
        code = code_of(editor_auth.url(BOT.cfg))
        self.assertEqual(call("/api/session", {"code": code}, headers={"Host": f"evil.example:{PORT}"})[0], 421)
        self.assertEqual(call("/api/session", {"code": code}, headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(call("/api/session", {"code": code})[0], 200)   # the refusals didn't use it up

    def test_rate_limited(self):
        for _ in range(10):
            self.assertEqual(call("/api/session", {"code": "guess"})[0], 403)
        good = code_of(editor_auth.url(BOT.cfg))
        self.assertEqual(call("/api/session", {"code": good})[0], 429)
        ED.server.session_limit = FailureLimit()   # a minute later
        self.assertEqual(call("/api/session", {"code": good})[0], 200)

    def test_other_users_client_refused(self):
        code = code_of(editor_auth.url(BOT.cfg))
        with mock.patch.object(editor_auth, "peer_uids", return_value={ME + 1}):
            self.assertEqual(call("/api/session", {"code": code})[0], 403)
        self.assertEqual(call("/api/session", {"code": code})[0], 200)   # not used up by the refusal

    def test_real_client_found_as_ours(self):
        with socket.create_connection(("127.0.0.1", PORT)) as c:
            time.sleep(0.05)
            self.assertEqual(editor_auth.peer_uids(c.getsockname(), PORT), {ME})

    def test_token_route_still_needs_token(self):
        self.assertEqual(call("/api/status")[0], 401)
        self.assertEqual(call("/api/session/revoke", {})[0], 401)


class Rotation(unittest.TestCase):
    def test_daemon_start_wrote_a_fresh_private_token(self):
        path = os.path.join(STATE, "editor_token")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path) as f:
            self.assertEqual(f.read().strip(), ED.token)

    def test_rotate_writes_atomically_over_a_symlink(self):
        path = os.path.join(STATE, "editor_token")
        victim = os.path.join(tmpdir(), "victim")
        with open(victim, "w") as f:
            f.write("keep")
        os.replace(path, path + ".bak")
        os.symlink(victim, path)
        try:
            ED.token = editor_auth.rotate()
            self.assertFalse(os.path.islink(path))
            with open(victim) as f:
                self.assertEqual(f.read(), "keep")
        finally:
            os.unlink(path + ".bak")

    def test_revoke_signs_out_everyone(self):
        ED.server.session_limit = FailureLimit()
        old = ED.token
        unused = code_of(editor_auth.url(BOT.cfg))
        status, body = call("/api/session/revoke", {}, token=old)
        self.assertEqual((status, body), (200, {"ok": True}))
        self.assertNotEqual(ED.token, old)
        self.assertEqual(call("/api/status", token=old)[0], 401)
        self.assertEqual(call("/api/status", token=ED.token)[0], 200)
        self.assertEqual(call("/api/session", {"code": unused})[0], 403)   # unused links died too
        with open(os.path.join(STATE, "editor_token")) as f:
            self.assertEqual(f.read().strip(), ED.token)


class Listener(unittest.TestCase):
    def test_ours(self):
        p = proc_file([("127.0.0.1", 9000, "0.0.0.0", 0, "0A", ME), ("127.0.0.1", 9001, "0.0.0.0", 0, "0A", ME + 1)])
        self.assertIs(editor_auth.listener_is_ours(9000, [p]), True)
        self.assertEqual(editor_auth.listener_uids(9000, [p]), {ME})

    def test_someone_elses(self):
        p = proc_file([("127.0.0.1", 9001, "0.0.0.0", 0, "0A", ME + 1)])
        self.assertIs(editor_auth.listener_is_ours(9001, [p]), False)

    def test_wildcard_and_ipv6_dual_stack_count(self):
        p4 = proc_file([("0.0.0.0", 9002, "0.0.0.0", 0, "0A", ME + 1)])
        p6 = proc_file([("::", 9003, "::", 0, "0A", ME + 1), ("::1", 9004, "::", 0, "0A", ME + 1)])
        self.assertIs(editor_auth.listener_is_ours(9002, [p4]), False)
        self.assertIs(editor_auth.listener_is_ours(9003, [p4, p6]), False)
        self.assertIsNone(editor_auth.listener_is_ours(9004, [p4, p6]))   # ::1 never gets 127.0.0.1's traffic

    def test_not_listening_or_unreadable(self):
        p = proc_file([("127.0.0.1", 9005, "127.0.0.1", 40000, "01", ME + 1)])   # a connection, not a listener
        self.assertIsNone(editor_auth.listener_is_ours(9005, [p]))
        self.assertIsNone(editor_auth.listener_is_ours(9005, ["/nonexistent/tcp"]))
        self.assertIsNone(editor_auth.listener_uids(9005, ["/nonexistent/tcp"]))

    def test_the_real_table(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen()
            self.assertIs(editor_auth.listener_is_ours(s.getsockname()[1]), True)

    def test_peer_uids(self):
        p = proc_file([("127.0.0.1", 41000, "127.0.0.1", 9006, "01", ME + 1),
                       ("127.0.0.1", 41001, "127.0.0.1", 9006, "06", 0)])
        self.assertEqual(editor_auth.peer_uids(("127.0.0.1", 41000), 9006, [p]), {ME + 1})
        self.assertEqual(editor_auth.peer_uids(("127.0.0.1", 41001), 9006, [p]), set())
        self.assertIsNone(editor_auth.peer_uids(("127.0.0.1", 41000), 9006, ["/nonexistent/tcp"]))

    def test_no_url_for_someone_elses_port(self):
        port = free_port()
        p = proc_file([("127.0.0.1", port, "0.0.0.0", 0, "0A", ME + 1)])
        with mock.patch.object(editor_auth, "PROC_NET", (p,)):
            with self.assertRaisesRegex(editor_auth.Unavailable, "Another user"):
                editor_auth.url({"editor": {"port": port}})
        with self.assertRaisesRegex(editor_auth.Unavailable, "isn't running"):
            editor_auth.url({"editor": {"port": free_port()}})

    def test_open_editor_refuses_someone_elses_port(self):
        port = free_port()
        p = proc_file([("127.0.0.1", port, "0.0.0.0", 0, "0A", ME + 1)])
        cfg = os.path.join(tmpdir(), "config.yaml")
        with open(cfg, "w") as f:
            yaml.safe_dump({"editor": {"port": port}}, f)
        script = ("import sys, runpy; sys.argv = ['main.py', '--open-editor']; from core import editor_auth; "
                  f"editor_auth.PROC_NET = ({p!r},); runpy.run_path({os.path.join(ROOT, 'main.py')!r}, run_name='__main__')")
        env = {**os.environ, "DECK_CONFIG": cfg, "PATH": tmpdir()}   # no xdg-open to run
        out = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env, capture_output=True, text=True,
                             timeout=30)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("Another user", out.stderr)


class BindRetry(unittest.TestCase):
    def test_retries_until_free_and_hands_out_no_link_meanwhile(self):
        g = ED._bind_retry.__globals__
        saved = g["BIND_RETRY"]
        g["BIND_RETRY"] = (0.1, 0.2)
        held = socket.socket()
        held.bind(("127.0.0.1", 0))
        held.listen()
        port = held.getsockname()[1]
        try:
            with self.assertLogs("deck.Editor", logging.DEBUG) as logs:
                ED.on_config({**ED.config, "port": port})
                self.assertIsNone(ED.server)
                with self.assertRaisesRegex(editor_auth.Unavailable, "isn't listening"):
                    editor_auth.url({"editor": {"port": port}})
                ED.open_editor("open")   # the deck key: refuses, opens nothing
                time.sleep(0.5)
            warnings = [r for r in logs.records if r.levelno == logging.WARNING and "taken" in r.getMessage()]
            self.assertEqual(len(warnings), 1, [r.getMessage() for r in logs.records])
            self.assertIn(f"uid {ME}", warnings[0].getMessage())
            self.assertTrue(any(r.levelno == logging.DEBUG and "still taken" in r.getMessage() for r in logs.records))
            self.assertFalse(any("#" in r.getMessage() for r in logs.records))   # never a link while unbound
            held.close()
            deadline = time.time() + 5
            while ED.server is None and time.time() < deadline:
                time.sleep(0.05)
            self.assertIsNotNone(ED.server)
            ED.server.session_limit = FailureLimit()
            code = code_of(editor_auth.url({"editor": {"port": port}}))
            self.assertEqual(call("/api/session", {"code": code}, base=f"http://127.0.0.1:{port}")[0], 200)
        finally:
            held.close()
            g["BIND_RETRY"] = saved
            ED.on_config({**ED.config, "port": PORT})
        self.assertIsNotNone(ED.server)

    def test_open_action_only_when_bound(self):
        g = ED.open_editor.__globals__
        with mock.patch.dict(g, {"open_url": mock.Mock()}):
            ED.open_editor("open")
            g["open_url"].assert_called_once()
            self.assertIn("#code=", g["open_url"].call_args.args[0])
            g["open_url"].reset_mock()
            server, ED.server = ED.server, None
            try:
                ED.open_editor("open")
            finally:
                ED.server = server
            g["open_url"].assert_not_called()


class ReadSecret(unittest.TestCase):
    def own(self, name, text="secret-value\n"):
        path = os.path.join(STATE, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, 0o600)
        return path

    def test_own_secret_files_refused(self):
        for name in ("twitch.json", "editor_token", "discord_webhooks.json", "alerts.json", "announce_state.json"):
            path = self.own(name) if name != "editor_token" else os.path.join(STATE, name)
            with self.subTest(name), self.assertLogs("deck", logging.WARNING):
                self.assertIsNone(util.read_secret("NO_SUCH_ENV_VAR", path))

    def test_through_a_symlink_or_the_launch_dir(self):
        link = os.path.join(tmpdir(), "innocent")
        os.symlink(self.own("twitch.json"), link)
        with self.assertLogs("deck", logging.WARNING):
            self.assertIsNone(util.read_secret("NO_SUCH_ENV_VAR", link))
        code = editor_auth.new_launch_code()
        with self.assertLogs("deck", logging.WARNING):
            self.assertIsNone(util.read_secret("NO_SUCH_ENV_VAR", os.path.join(LAUNCH, editor_auth._digest(code))))
        editor_auth.redeem_launch_code(code)

    def test_ordinary_secret_files_still_read(self):
        self.assertEqual(util.read_secret("NO_SUCH_ENV_VAR", self.own("ha_token")), "secret-value")
        elsewhere = os.path.join(tmpdir(), "twitch.json")   # same name, not ours
        with open(elsewhere, "w") as f:
            f.write("abc\n")
        os.chmod(elsewhere, 0o600)
        self.assertEqual(util.read_secret("NO_SUCH_ENV_VAR", elsewhere), "abc")


class Shell(unittest.TestCase):
    def test_output(self):
        self.assertEqual(util.sh("echo hi; exit 3"), (3, "hi"))

    def test_timeout_kills_the_whole_pipeline(self):
        start = time.monotonic()
        self.assertEqual(util.sh('sh -c "sleep 30 | cat"', timeout=1), (124, ""))
        self.assertLess(time.monotonic() - start, 3)

    def test_timeout_kills_grandchildren(self):
        pidfile = os.path.join(tmpdir(), "pid")
        self.assertEqual(util.sh(f'sleep 30 & echo $! > {pidfile}; wait', timeout=1)[0], 124)
        with open(pidfile) as f:
            pid = int(f.read())
        deadline = time.time() + 2
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail(f"grandchild {pid} survived the timeout")

    def test_open_url_and_notify_use_no_shell(self):
        with mock.patch.object(util.subprocess, "Popen") as popen:
            util.open_url("https://example.com/a b;$(id)")
            util.notify("it's $(id)")
        (a1, k1), (a2, k2) = popen.call_args_list[0], popen.call_args_list[1]
        self.assertEqual(a1[0], ["xdg-open", "https://example.com/a b;$(id)"])
        self.assertEqual(a2[0], ["notify-send", "--", "Stream Deck", "it's $(id)"])
        for k in (k1, k2):
            self.assertFalse(k.get("shell"))
            self.assertTrue(k["start_new_session"])
            self.assertIs(k["stdin"], subprocess.DEVNULL)

    def test_open_url_refuses_an_option(self):
        with mock.patch.object(util.subprocess, "Popen") as popen, self.assertLogs("deck", logging.WARNING):
            util.open_url("--help")
        popen.assert_not_called()

    def test_missing_program_is_logged(self):
        with mock.patch.object(util.subprocess, "Popen", side_effect=FileNotFoundError("xdg-open")), \
                self.assertLogs("deck", logging.WARNING):
            util.open_url("https://example.com")


if __name__ == "__main__":
    unittest.main()
