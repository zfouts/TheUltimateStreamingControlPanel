"""modules/chat.py: tag unescaping, cleaning of viewer text, clean channel changes;
modules/brb.py: bounded per-chatter memory; modules/obs.py: late websocket config."""
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests.helpers import ROOT  # noqa: F401  (sandbox + sys.path)

from modules import brb as brb_mod  # noqa: E402
from modules.brb import BRB  # noqa: E402
from modules.chat import Chat, parse_tags  # noqa: E402
from modules.obs import OBS  # noqa: E402


def make_chat():
    bot = mock.Mock()
    chat = Chat(bot)
    chat.channel = "somechan"
    chat.stop = threading.Event()
    chat.connected = True
    return chat, bot


def privmsg(chat, text, tags="display-name=Viewer;user-id=123;id=abc", send=None):
    line = f"@{tags} :viewer!viewer@viewer.tmi.twitch.tv PRIVMSG #somechan :{text}"  # privacy-check: allow
    chat._handle(line, send or mock.Mock())
    return chat.bot.dispatch.call_args.args[1]


class Tags(unittest.TestCase):
    def test_escapes(self):
        self.assertEqual(parse_tags(r"@a=x\sy\:z;b=p\\q;c=l1\nl2\r")["a"], "x y;z")
        t = parse_tags(r"@b=p\\q;c=l1\nl2\r")
        self.assertEqual(t["b"], "p\\q")
        self.assertEqual(t["c"], "l1\nl2\r")

    def test_escape_order(self):
        # an escaped backslash followed by "s" is backslash + "s", not a space
        self.assertEqual(parse_tags("@a=\\\\s")["a"], "\\s")
        self.assertEqual(parse_tags("@a=\\\\\\s")["a"], "\\ ")
        self.assertEqual(parse_tags("@a=\\\\:")["a"], "\\:")

    def test_odd_escapes(self):
        self.assertEqual(parse_tags("@a=x\\q")["a"], "xq")
        self.assertEqual(parse_tags("@a=x\\")["a"], "x")
        self.assertEqual(parse_tags("@a=")["a"], "")


class Handle(unittest.TestCase):
    def setUp(self):
        self.chat, self.bot = make_chat()

    def test_plain(self):
        ev = privmsg(self.chat, "hello there")
        self.assertEqual((ev["text"], ev["action"], ev["user"], ev["user_id"]), ("hello there", False, "Viewer", "123"))

    def test_action(self):
        ev = privmsg(self.chat, "\x01ACTION waves\x01")
        self.assertEqual((ev["text"], ev["action"]), ("waves", True))

    def test_action_without_closing(self):
        ev = privmsg(self.chat, "\x01ACTION waves")
        self.assertEqual((ev["text"], ev["action"]), ("waves", True))

    def test_other_ctcp_left_alone_but_clean(self):
        ev = privmsg(self.chat, "\x01VERSION\x01")
        self.assertFalse(ev["action"])
        self.assertNotIn("\x01", ev["text"])
        self.assertIn("VERSION", ev["text"])

    def test_control_bidi_zero_width(self):
        ev = privmsg(self.chat, "a\x1b[31mb‮c​d\x07e﻿ f")
        self.assertEqual(ev["text"], "ab" + "cdef")

    def test_zwj_kept(self):
        self.assertEqual(privmsg(self.chat, "a‍b")["text"], "a‍b")

    def test_zalgo_collapsed(self):
        ev = privmsg(self.chat, "e" + "́" * 40 + "x")
        self.assertEqual(ev["text"], "é́x")

    def test_length_cap(self):
        ev = privmsg(self.chat, "x" * 2000)
        self.assertEqual(len(ev["text"]), 500)

    def test_bad_user_id(self):
        for bad in ("12a", "../1", "", "1" * 21):
            with self.subTest(bad=bad):
                self.assertEqual(privmsg(self.chat, "hi", tags=f"user-id={bad}")["user_id"], "")

    def test_display_name_cleaned(self):
        ev = privmsg(self.chat, "hi", tags="display-name=Bo‮b\\sX;user-id=1")
        self.assertEqual(ev["user"], "Bob X")


class ChannelChange(unittest.TestCase):
    def test_no_failure_logged_when_channel_changes(self):
        chat, _ = make_chat()
        chat.log = mock.Mock()
        sessions = []

        def session(channel):
            sessions.append(channel)
            if len(sessions) == 1:
                chat.channel = "other"      # what on_config does, then _reconnect() closes the socket
                raise OSError("Bad file descriptor")
            chat.stop.set()

        chat._session = session
        chat.stop.wait = mock.Mock(side_effect=AssertionError("must not back off"))
        t = threading.Thread(target=chat._run)
        t.start()
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(sessions, ["somechan", "other"])
        chat.log.info.assert_not_called()

    def test_clean_eof_on_channel_change_returns(self):
        chat, _ = make_chat()
        sock = mock.Mock()

        def recv(n):
            chat.channel = "other"
            return b""
        sock.recv = recv
        with mock.patch("modules.chat.socket.create_connection"), \
                mock.patch("modules.chat.ssl.create_default_context") as ctx:
            ctx.return_value.wrap_socket.return_value = sock
            chat._session("somechan")      # returns, does not raise

    def test_real_eof_still_fails(self):
        chat, _ = make_chat()
        sock = mock.Mock()
        sock.recv.return_value = b""
        with mock.patch("modules.chat.socket.create_connection"), \
                mock.patch("modules.chat.ssl.create_default_context") as ctx:
            ctx.return_value.wrap_socket.return_value = sock
            with self.assertRaises(ConnectionError):
                chat._session("somechan")


class BrbPruning(unittest.TestCase):
    def make(self):
        bot = mock.Mock()
        bot.get_module.return_value = None
        b = BRB(bot)
        b.on_config({})
        b.on_load()
        return b

    def test_old_entries_pruned(self):
        b = self.make()
        now = time.time()
        b.hype_seen = {("old", False): now - 60, ("cmd", True): now - 11, ("new", False): now - 1}
        b.refresh()
        self.assertEqual(set(b.hype_seen), {("new", False)})

    def test_capped(self):
        b = self.make()
        now = time.time()
        b.hype_seen = {(f"u{i}", False): now - 5 + i * 1e-6 for i in range(brb_mod.HYPE_SEEN_MAX + 100)}
        with b.lock:
            b.prune_hype_seen(now)
        self.assertEqual(len(b.hype_seen), brb_mod.HYPE_SEEN_MAX)
        self.assertIn((f"u{brb_mod.HYPE_SEEN_MAX + 99}", False), b.hype_seen)
        self.assertNotIn(("u0", False), b.hype_seen)

    def test_chat_still_adds_hype_and_writes_outside_lock(self):
        b = self.make()
        held = []
        b.write = lambda name, text: held.append(b.lock.locked())
        b.on_chat({"user": "a", "text": "hi"})
        self.assertGreater(b.hype, 0)
        self.assertEqual(held, [False])

    def test_no_makedirs_unless_configured(self):
        b = self.make()
        with tempfile.TemporaryDirectory() as d, mock.patch("os.makedirs") as mk:
            b.config = {}
            b.media_dir = lambda kind: os.path.join(d, "nope", kind)
            b.playlist()
            mk.assert_not_called()


class FakeClient:
    made = []
    fail_if = None

    def __init__(self, **kw):
        self.kw = kw
        if FakeClient.fail_if and FakeClient.fail_if(kw):
            raise ConnectionRefusedError("refused")
        FakeClient.made.append(self)

    def send(self, request, data=None, raw=True):
        return {"ok": request}

    def disconnect(self):
        pass


class ObsLateConfig(unittest.TestCase):
    def setUp(self):
        FakeClient.made, FakeClient.fail_if = [], None
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "global.json")
        for p in (mock.patch("modules.obs.obs_websocket_config", lambda: self.path),
                  mock.patch("obsws_python.ReqClient", FakeClient)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.dir.cleanup)

    def obs(self, config=None):
        o = OBS(mock.Mock())
        o.on_config(config or {})
        o.on_load()
        return o

    def write(self, **c):
        with open(self.path, "w") as f:
            json.dump(c, f)

    def test_password_set_after_start(self):
        o = self.obs()                      # no OBS config yet
        FakeClient.fail_if = lambda kw: kw["password"] != "secret"
        self.write(server_port=4466, auth_required=True, server_password="secret")
        with self.assertRaises(ConnectionRefusedError):
            o.send("GetVersion")            # inside the 30 s window: no re-read
        o._ws_read_at = time.time() - 31
        o.retry_at = 0
        self.assertEqual(o.send("GetVersion"), {"ok": "GetVersion"})
        self.assertEqual(FakeClient.made[0].kw["password"], "secret")
        self.assertEqual(FakeClient.made[0].kw["port"], 4466)

    def test_reread_at_most_every_30s(self):
        o = self.obs()
        FakeClient.fail_if = lambda kw: True
        o._ws_read_at = time.time() - 31
        with mock.patch.object(o, "_ws_defaults", wraps=o._ws_defaults) as rd:
            for _ in range(3):
                o.retry_at = 0
                with self.assertRaises(ConnectionRefusedError):
                    o.send("GetVersion")
            self.assertEqual(rd.call_count, 1)

    def test_explicit_settings_never_overridden(self):
        o = self.obs({"port": 4455})
        o.explicit["password"] = "mine"
        o.settings["password"] = "mine"
        FakeClient.fail_if = lambda kw: True
        self.write(server_port=9999, auth_required=True, server_password="other")
        o._ws_read_at = time.time() - 31
        with mock.patch.object(o, "_ws_defaults") as rd:
            with self.assertRaises(ConnectionRefusedError):
                o.send("GetVersion")
            rd.assert_not_called()           # port and password both set explicitly

    def test_on_config_waits_for_connect_in_flight(self):
        o = self.obs()
        started, release = threading.Event(), threading.Event()

        class Slow(FakeClient):
            def __init__(self, **kw):
                started.set()
                release.wait(5)
                super().__init__(**kw)

        with mock.patch("obsws_python.ReqClient", Slow):
            t = threading.Thread(target=o.send, args=("GetVersion",))
            t.start()
            started.wait(5)
            c = threading.Thread(target=o.on_config, args=({},))
            c.start()
            time.sleep(0.2)
            self.assertTrue(c.is_alive())    # blocked on the lock
            release.set()
            t.join(5)
            c.join(5)
        self.assertIsNone(o.client)          # the stale client was dropped


if __name__ == "__main__":
    unittest.main()
