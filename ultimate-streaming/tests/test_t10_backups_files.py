"""Crash-safe backups, labelled revisions kept, symlinked config.yaml, refused POST closes, overlay races."""
import datetime
import json
import logging
import os
import socket
import threading
import time
import types
import unittest
from http.server import ThreadingHTTPServer

from tests.helpers import free_port, tmpdir

from core import backups, configio, safefs  # noqa: E402
from core import setup_backup as sb  # noqa: E402
from core.webapi.server import FailureLimit, Handler  # noqa: E402
from modules import overlay as ov  # noqa: E402
from tests import test_backups as tb  # noqa: E402
from tests.test_setup_backup import ext_dir, read, write  # noqa: E402

CFG = tb.CFG_A


def age(path, seconds):
    t = time.time() - seconds
    os.utime(path, (t, t))


def label(meta, text):
    with open(meta) as f:
        m = json.load(f)
    m["label"] = text
    with open(meta, "w") as f:
        json.dump(m, f)


class StaleLeftovers(unittest.TestCase):
    def test_tmp_files_removed_by_age(self):
        d = sb._dir()
        old, new = os.path.join(d, ".tmp-x-old"), os.path.join(d, ".tmp-x-new")
        write(old, b"secret")
        write(new, b"secret")
        age(old, 2 * 3600)
        sb.prune()
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))
        os.unlink(new)

    def test_stale_stage_dirs_removed_at_restore(self):
        root = tmpdir()
        write(os.path.join(root, "f.json"), b"{}")
        a = sb.create([{"kind": "boatswain", "root": root, "include": None, "label": "B"}], label="t")
        old = os.path.join(root, sb.STAGE_PREFIX + "old")
        new = os.path.join(root, sb.STAGE_PREFIX + "new")
        for p in (old, new):
            os.mkdir(p)
        age(old, 2 * 3600)
        sb.restore(a["id"], "boatswain", roots={"boatswain": root}, is_running=lambda k: False, confirm=True)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))


class Journal(unittest.TestCase):
    def setUp(self):
        self.root = tmpdir()
        write(os.path.join(self.root, "f.json"), b'{"a": 1}')
        self.a = sb.create([{"kind": "boatswain", "root": self.root, "include": None, "label": "B"}], label="t")
        write(os.path.join(self.root, "f.json"), b'{"a": 2}')

    def restore(self):
        return sb.restore(self.a["id"], "boatswain", roots={"boatswain": self.root},
                          is_running=lambda k: False, confirm=True)

    def test_written_during_rename_and_removed_after(self):
        seen = []
        real = os.replace

        def spy(src, dst, *a, **k):
            if os.path.dirname(dst) == self.root:
                seen.append(sb.interrupted_restore())
            return real(src, dst, *a, **k)

        from unittest import mock
        with mock.patch("os.replace", spy):
            r = self.restore()
        j = seen[0]
        self.assertEqual(set(j), {"archive", "before", "target", "started"})
        self.assertEqual((j["archive"], j["before"], j["target"]), (self.a["id"], r["pre_backup"], os.path.realpath(self.root)))
        self.assertIsNone(sb.interrupted_restore())

    def test_removed_after_clean_rollback(self):
        from unittest import mock
        real = os.replace

        def boom(src, dst, *a, **k):
            if os.path.dirname(dst) == os.path.realpath(self.root) and os.path.basename(src).startswith(".tmp"):
                raise OSError("disk")
            return real(src, dst, *a, **k)

        with mock.patch("os.replace", boom):
            with self.assertRaises(sb.BackupError):
                self.restore()
        self.assertIsNone(sb.interrupted_restore())

    def test_left_behind_is_reported(self):
        sb._write_journal("a", "b", "/x")
        j = sb.interrupted_restore()
        self.assertEqual((j["archive"], j["before"], j["target"]), ("a", "b", "/x"))
        sb._clear_journal()
        self.assertIsNone(sb.interrupted_restore())


class LabelledRevisions(unittest.TestCase):
    def setUp(self):
        tb.reset()

    def tearDown(self):
        tb.reset()

    def test_labelled_kept_past_age_and_count(self):
        now = datetime.datetime.now()
        old = now - datetime.timedelta(days=60)
        keep = tb.fake_revision(old, "snap")
        meta = os.path.join(tb.REV, keep + ".json")
        label(meta, "My snapshot")
        gone = tb.fake_revision(old + datetime.timedelta(seconds=1), "file")
        for i in range(backups.KEEP_REVISIONS + 5):
            tb.fake_revision(now - datetime.timedelta(seconds=i + 1), "file")
        backups.prune(now)
        ids = backups._rev_ids()
        self.assertIn(keep, ids)
        self.assertNotIn(gone, ids)

    def test_labelled_capped(self):
        old = datetime.datetime.now() - datetime.timedelta(days=60)
        rids = []
        for i in range(backups.KEEP_LABELLED + 3):
            rid = tb.fake_revision(old + datetime.timedelta(seconds=i), "snap")
            meta = os.path.join(tb.REV, rid + ".json")
            label(meta, "L")
            rids.append(rid)
        backups.prune()
        self.assertEqual(backups._rev_ids(), rids[3:])


class Symlinked(unittest.TestCase):
    def setUp(self):
        tb.reset()
        d = tmpdir()
        self.target = os.path.join(d, "dotfiles-config.yaml")
        write(self.target, CFG)
        self.link = os.path.join(d, "config.yaml")
        os.symlink(self.target, self.link)

    def tearDown(self):
        tb.reset()

    def test_save_keeps_link(self):
        cfg = configio.safe_load_bounded(CFG)
        cfg["twitch"] = {"channel": "changed"}
        configio.save(self.link, cfg)
        self.assertTrue(os.path.islink(self.link))
        self.assertIn("changed", read(self.target).decode())

    def test_restore_keeps_link(self):
        r = backups.record(self.link, "start")
        write(self.target, CFG.replace("me", "other"))
        backups.restore(r["id"], self.link)
        self.assertTrue(os.path.islink(self.link))
        self.assertEqual(read(self.target).decode(), CFG)

    def test_default_still_replaces_link(self):
        safefs.write_atomic(self.link, b"x")
        self.assertFalse(os.path.islink(self.link))


class RefusedPost(unittest.TestCase):
    def test_closes_connection(self):
        port = free_port()
        editor = types.SimpleNamespace(token="tok", log=logging.getLogger("t"),
                                       router=types.SimpleNamespace(uploads={}, find_post=lambda p: None))
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        srv.editor, srv.allowed_hosts, srv.session_limit = editor, {f"127.0.0.1:{port}"}, FailureLimit()
        srv.static_dir, srv.stopping = tmpdir(), threading.Event()
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            for headers in ("X-Deck-Token: wrong\r\n", "X-Deck-Token: tok\r\n"):
                body = b'{"a": 1}'
                s = socket.create_connection(("127.0.0.1", port), timeout=5)
                s.sendall((f"POST /api/x HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n{headers}"
                           f"Content-Type: text/plain\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body)
                data = b""
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                s.close()
                self.assertIn(b"Connection: close", data)
                self.assertEqual(data.count(b"HTTP/1.1 "), 1)
        finally:
            srv.shutdown()
            srv.server_close()


class FakeBot:
    def run_action(self, a):
        pass


def make_overlay(config=None):
    m = ov.Overlay(FakeBot())
    m.config = config or {}
    m.dir = tmpdir()
    from core import paths
    real = paths.overlay_dir
    paths.overlay_dir = lambda: m.dir
    ov.overlay_dir = paths.overlay_dir
    try:
        m.on_load()
    finally:
        paths.overlay_dir = real
        ov.overlay_dir = real
    return m


def text(m, name):
    with open(m.path(name), encoding="utf-8") as f:
        return f.read()


class OverlayTimers(unittest.TestCase):
    def test_stop_during_tick_does_not_leave_countdown_text(self):
        m = make_overlay()
        m.start_timer("brb_timer", 60)
        in_remaining, go = threading.Event(), threading.Event()
        real = m.remaining

        def slow(name="countdown"):
            left = real(name)
            if threading.current_thread().name == "ticker":
                in_remaining.set()
                go.wait(2)
            return left

        m.remaining = slow
        t = threading.Thread(target=m.tick, name="ticker")
        t.start()
        self.assertTrue(in_remaining.wait(2))
        stopper = threading.Thread(target=m.stop_timer, args=("brb_timer",))
        stopper.start()
        time.sleep(0.1)   # stop must wait for the tick's lock
        go.set()
        t.join(2)
        stopper.join(2)
        self.assertEqual(text(m, "brb_timer"), "Back soon")
        self.assertNotIn("brb_timer", m.timers)

    def test_tick_after_stop_is_fine(self):
        m = make_overlay()
        m.start_timer("countdown", 60)
        m.stop_timer("countdown")
        m.tick()
        self.assertEqual(text(m, "countdown"), "Get comfy, we're about to start")

    def test_bad_format_logged_once_and_default_used(self):
        m = make_overlay({"timers": {"countdown": {"format": "Soon {nope}"}}})
        with self.assertLogs("deck.Overlay", logging.WARNING) as logs:
            m.start_timer("countdown", 60)
            for _ in range(5):
                m.tick()
        self.assertEqual(len(logs.records), 1)
        self.assertTrue(text(m, "countdown").startswith("Starting in "))

    def test_bad_names_rejected(self):
        m = make_overlay()
        for bad in ("../../x", "A", "", "a" * 33, "a/b", "a.b"):
            with self.assertRaises(ValueError):
                m.path(bad)
            with self.assertRaises(ValueError):
                m._parse({"name": bad, "seconds": 5})
            with self.assertRaises(ValueError):
                m.start_timer(bad, 5)
        self.assertEqual(m._parse({"name": "brb_timer", "seconds": 5})[0], "brb_timer")


if __name__ == "__main__":
    unittest.main()
