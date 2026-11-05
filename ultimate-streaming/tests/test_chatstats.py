"""Chat statistics store (core/chatstats.py) on temp-file SQLite: hot path in memory only,
batched flushes, queries, resets, degraded mode, error retry, threads."""
import logging
import os
import sqlite3
import threading
import time
import unittest
from unittest import mock

from tests.helpers import tmpdir

import sqlalchemy as sa  # noqa: E402

from core import chatstats  # noqa: E402
from core.chatstats import ChatStats  # noqa: E402


def wait_for(pred, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.005)
    return False


class Clock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        self.t += 1.0
        return self.t


def db_url():
    return "sqlite:///" + os.path.join(tmpdir(), "stats.db")


class Base(unittest.TestCase):
    def make(self, url=None, **kw):
        kw.setdefault("flush_every", 3600)
        kw.setdefault("clock", Clock())
        c = ChatStats(db_url() if url is None else url, **kw)
        c.start()
        self.addCleanup(c.close)
        self.assertIsNone(c.describe()["error"])
        return c

    @staticmethod
    def stop_writer(c):
        """Tests that drive flush() by hand don't want the writer racing them."""
        c._stop.set()
        c._wake.set()
        c._thread.join(5)

    @staticmethod
    def rows(c):
        with c._engine.connect() as conn:
            return {r.user_id: r for r in conn.execute(sa.select(chatstats.CHATTERS))}


class HotPathTest(Base):
    def test_record_never_touches_the_db(self):
        c = self.make()
        self.stop_writer(c)
        real = c._engine
        fake = mock.MagicMock(name="engine")
        c._engine = fake
        with mock.patch.object(chatstats, "Session", side_effect=AssertionError("DB used")):
            for i in range(200):
                c.record_message(str(i), f"user{i}", f"User{i}")
                c.record_command(str(i), f"user{i}", f"User{i}")
                c.record_redemption(str(i), f"user{i}", f"User{i}", cost=100)
            c.record_message("1", "user1", "User1", ts=123.0)
        self.assertEqual(fake.mock_calls, [])
        self.assertEqual(len(c._pending), 200)
        c._engine = real
        c.flush()
        self.assertEqual(c.user("1")["messages"], 2)
        self.assertEqual(c.user("1")["points_spent"], 100)

    def test_flush_is_one_transaction(self):
        c = self.make()
        self.stop_writer(c)
        c.record_message("1", "old", "Old")
        c.flush()
        begins, statements = [], []
        sa.event.listen(c._engine, "begin", lambda conn: begins.append(1))
        sa.event.listen(c._engine, "before_cursor_execute",
                        lambda *a: statements.append(a[2]))
        for i in range(1, 301):
            for _ in range(3):
                c.record_message(str(i), f"user{i}", f"User{i}")
        c.record_command("1", "user1", "User1")
        self.assertTrue(c.flush())
        self.assertEqual(len(begins), 1)
        self.assertLessEqual(len(statements), 4)    # select existing, insert many, update many
        rows = self.rows(c)
        self.assertEqual(len(rows), 300)
        self.assertEqual(rows["1"].messages, 4)
        self.assertEqual(rows["1"].commands, 1)
        self.assertEqual(rows["1"].login, "user1")   # latest name wins
        self.assertEqual(rows["300"].messages, 3)
        self.assertFalse(c._pending)

    def test_queries_flush_first(self):
        c = self.make()
        self.stop_writer(c)
        c.record_message("7", "seven", "Seven")
        self.assertEqual(c.user(login="seven")["messages"], 1)
        c.record_message("7", "seven", "Seven")
        self.assertEqual(c.top()[0]["messages"], 2)
        c.record_message("8", "eight", "Eight")
        self.assertEqual(c.totals()["chatters"], 2)
        self.assertFalse(c._pending)


class QueryTest(Base):
    def setUp(self):
        self.c = self.make()
        # first_seen order: alice, bob, carol, dave (the clock ticks per record)
        for uid, login, n in (("1", "alice", 5), ("2", "bob", 5), ("3", "carol", 7), ("4", "dave", 1)):
            for _ in range(n):
                self.c.record_message(uid, login, login.title())
        self.c.record_command("5", "erin", "Erin")     # never chatted

    def test_user_rank_and_ties(self):
        c = self.c
        carol = c.user(login="carol")
        self.assertEqual((carol["rank"], carol["stream_rank"], carol["messages"]), (1, 1, 7))
        self.assertEqual(c.user("1")["rank"], 2)
        self.assertEqual(c.user(login="BOB")["rank"], 2)       # tie shares the rank
        self.assertEqual(c.user("4")["rank"], 4)
        erin = c.user(login="erin")
        self.assertEqual((erin["messages"], erin["commands"], erin["stream_rank"]), (0, 1, None))
        self.assertEqual(set(carol), {"user_id", "login", "display", "messages", "stream_messages",
                                      "commands", "redemptions", "points_spent", "first_seen",
                                      "last_seen", "rank", "stream_rank"})
        self.assertLess(carol["first_seen"], carol["last_seen"])
        self.assertIsNone(c.user(login="nobody"))
        self.assertIsNone(c.user(login="bad login!"))
        self.assertIsNone(c.user(user_id="x1"))
        self.assertIsNone(c.user())

    def test_top_all_and_stream(self):
        c = self.c
        top = c.top()
        self.assertEqual([(r["rank"], r["login"], r["messages"]) for r in top],
                         [(1, "carol", 7), (2, "alice", 5), (2, "bob", 5), (4, "dave", 1)])
        self.assertEqual(set(top[0]), {"rank", "user_id", "login", "display", "messages"})
        self.assertNotIn("erin", [r["login"] for r in top])     # zero rows omitted
        c.new_stream()
        self.assertEqual(c.top(period="stream"), [])
        c.record_message("4", "dave", "Dave")
        c.record_message("4", "dave", "Dave")
        c.record_message("2", "bob", "Bob")
        self.assertEqual([(r["login"], r["messages"]) for r in c.top(10, "stream")],
                         [("dave", 2), ("bob", 1)])
        self.assertEqual(c.user(login="bob")["stream_rank"], 2)
        self.assertEqual(c.user(login="bob")["messages"], 6)

    def test_top_bounds(self):
        c = self.c
        for i in range(10, 130):
            c.record_message(str(i), f"u{i}", f"U{i}")
        self.assertEqual(len(c.top(0)), 1)
        self.assertEqual(len(c.top(-5)), 1)
        self.assertEqual(len(c.top(1000)), 100)
        self.assertEqual(len(c.top(3)), 3)
        for bad in ("10", 2.5, True, None):
            with self.assertRaises(ValueError):
                c.top(bad)
        with self.assertRaises(ValueError):
            c.top(10, "week")

    def test_new_stream_resets_stream_counters(self):
        c = self.c
        c.flush()
        c.record_message("1", "alice", "Alice")         # before the stream starts: all-time only
        c.new_stream()
        c.record_message("1", "alice", "Alice")
        a = c.user(login="alice")
        self.assertEqual((a["messages"], a["stream_messages"]), (7, 1))
        self.assertEqual(c.totals(), {"chatters": 5, "messages": 20, "stream_messages": 1})
        c.new_stream()
        self.assertEqual(c.totals()["stream_messages"], 0)

    def test_reset(self):
        c = self.c
        c.record_message("1", "alice", "Alice")
        c.reset("stream")
        t = c.totals()
        self.assertEqual((t["messages"], t["stream_messages"], t["chatters"]), (19, 0, 5))
        c.record_message("1", "alice", "Alice")
        self.assertEqual(c.user("1")["stream_messages"], 1)
        c.record_message("2", "bob", "Bob")
        c.reset("all")
        self.assertEqual(c.totals(), {"chatters": 0, "messages": 0, "stream_messages": 0})
        self.assertFalse(c._pending)
        c.flush()
        self.assertEqual(self.rows(c), {})
        with self.assertRaises(ValueError):
            c.reset("everything")

    def test_totals(self):
        self.assertEqual(self.c.totals(), {"chatters": 5, "messages": 18, "stream_messages": 18})


class InputTest(Base):
    def test_invalid_input_ignored(self):
        c = self.make()
        self.stop_writer(c)
        for uid, login in (("abc", "ok"), ("", "ok"), (123, "ok"), ("1" * 21, "ok"), (None, "ok"),
                           ("1", "bad-login"), ("1", "a" * 26), ("1", ""), ("1", None), ("1", 5)):
            c.record_message(uid, login, "X")
        for cost in (-1, True, "5", 2.0, 2**31):
            c.record_redemption("1", "ok", "Ok", cost)
        self.assertFalse(c._pending)
        c.record_message("1", "MixedCase", "Mixed")
        c.record_message("2", "ok", "Ok", ts=float("nan"))
        c.record_redemption("2", "ok", "Ok", 0)
        self.assertEqual(c.user(login="mixedcase")["login"], "mixedcase")
        ok = c.user("2")
        self.assertEqual((ok["messages"], ok["redemptions"], ok["points_spent"]), (1, 1, 0))
        self.assertGreater(ok["first_seen"], 1_000_000_000)

    def test_exclude(self):
        c = self.make(exclude=["NightBot", "not a login!", 7])
        self.assertEqual(c._exclude, {"nightbot"})
        c.record_message("1", "nightbot", "Nightbot")
        c.record_message("2", "viewer", "Viewer")
        c.record_message("3", "streamelements", "SE")
        c.flush()
        self.assertEqual(set(self.rows(c)), {"2", "3"})
        c.set_exclude(["streamelements"])
        c.record_message("3", "streamelements", "SE")       # pending one dropped too
        self.assertEqual([r["login"] for r in c.top()], ["viewer"])
        self.assertIsNone(c.user("3"))
        self.assertEqual(c.totals()["chatters"], 1)
        self.assertEqual(self.rows(c)["3"].messages, 1)     # hidden, not deleted

    def test_display_sanitised(self):
        clean = chatstats.clean_display
        self.assertEqual(clean("‮Evil​⁦Name⁩", "x"), "EvilName")
        self.assertEqual(clean("Bad\x00\n\tName ", "x"), "BadName")
        self.assertEqual(clean("\U0001F468‍\U0001F469", "x"), "\U0001F468‍\U0001F469")
        self.assertEqual(clean("ｶﾀｶﾅ名前", "x"), "ｶﾀｶﾅ名前")
        self.assertEqual(clean("y" * 500, "x"), "y" * 64)
        self.assertEqual(clean("​‮  ", "login"), "login")
        self.assertEqual(clean(None, "login"), "login")
        c = self.make()
        c.record_message("1", "viewer", "‮Viewer" + "z" * 100)
        self.assertEqual(c.user("1")["display"], "Viewer" + "z" * 58)


class SqliteFileTest(Base):
    def test_file_private_and_wal(self):
        old = os.umask(0)
        try:
            path = os.path.join(tmpdir(), "s.db")
            c = self.make("sqlite:///" + path)
        finally:
            os.umask(old)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        with c._engine.connect() as conn:
            self.assertEqual(conn.exec_driver_sql("PRAGMA journal_mode").scalar(), "wal")
            self.assertEqual(conn.exec_driver_sql("PRAGMA busy_timeout").scalar(), 5000)
        c.record_message("1", "a", "A")
        c.flush()
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='schema_version'")
                             .fetchone()[0], "1")
            self.assertEqual(db.execute("SELECT messages FROM chatters").fetchone()[0], 1)

    def test_default_url_in_state_dir(self):
        from core.paths import state_dir
        path = os.path.join(state_dir(), "chatstats.db")
        for p in (path, path + "-wal", path + "-shm"):
            if os.path.exists(p):
                os.remove(p)
        c = self.make("")
        self.assertEqual(c.describe(), {"url": "sqlite:///" + path, "backend": "sqlite", "error": None})
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_close_flushes(self):
        url = db_url()
        c = ChatStats(url, flush_every=3600)
        c.start()
        c.record_message("1", "a", "A")
        c.record_redemption("1", "a", "A", 250)
        c.close()
        c.close()                                          # idempotent
        self.assertFalse(c._thread)
        d = self.make(url)
        self.assertEqual((d.user("1")["messages"], d.user("1")["points_spent"]), (1, 250))

    def test_in_memory_shares_one_connection(self):
        c = self.make("sqlite://")
        c.record_message("1", "a", "A")
        res = []
        t = threading.Thread(target=lambda: res.append(c.user("1")))
        t.start()
        t.join()
        self.assertEqual(res[0]["messages"], 1)


class DegradedTest(unittest.TestCase):
    def check_degraded(self, url, secret=None):
        with self.assertLogs("deck", level="DEBUG") as logs:
            c = ChatStats(url, flush_every=0.01)
            c.start()                                       # must not raise
            logging.getLogger("deck").debug("marker")
            d = c.describe()
            self.assertTrue(d["error"])
            c.record_message("1", "a", "A")
            c.record_command("1", "a", "A")
            c.record_redemption("1", "a", "A", 5)
            c.new_stream()
            self.assertFalse(c._pending)
            self.assertTrue(c.flush())
            self.assertIsNone(c.user("1"))
            self.assertIsNone(c.user(login="a"))
            self.assertEqual(c.top(), [])
            self.assertEqual(c.top(5, "stream"), [])
            self.assertEqual(c.totals(), {"chatters": 0, "messages": 0, "stream_messages": 0})
            c.reset("all")
            c.close()
        if secret:
            self.assertNotIn(secret, repr(d))
            self.assertNotIn(secret, "\n".join(logs.output))
            self.assertIn("***", d["url"])
        return d

    def test_missing_driver(self):
        d = self.check_degraded("postgresql+nosuchdriver://u:secret@h/db", "secret")
        self.assertEqual(d["url"], "postgresql+nosuchdriver://u:***@h/db")
        self.assertEqual(d["backend"], "postgresql")

    def test_unreachable_path(self):
        d = self.check_degraded("sqlite:////nonexistent-dir/deeper/stats.db")
        self.assertEqual(d["backend"], "sqlite")

    def test_not_a_database(self):
        path = os.path.join(tmpdir(), "garbage.db")
        with open(path, "wb") as f:
            f.write(b"this is not sqlite" * 100)
        self.check_degraded("sqlite:///" + path)

    def test_connection_failure_hides_password(self):
        # A driver that loads but whose connect fails, mentioning the URL in its error.
        url = "sqlite:///" + os.path.join(tmpdir(), "x.db")
        secret_url = "postgresql://u:secret@h/db"

        def boom(*a, **kw):
            raise sa.exc.OperationalError("connect", {}, Exception(f"cannot reach {secret_url} secret"))

        c = ChatStats(secret_url)
        with mock.patch.object(c, "_make_engine",
                               lambda: sa.create_engine(url, creator=boom)), \
                self.assertLogs("deck", level="DEBUG") as logs:
            c.start()
            logging.getLogger("deck").debug("marker")
        self.assertTrue(c.describe()["error"])
        self.assertNotIn("secret", repr(c.describe()))
        self.assertNotIn("secret", "\n".join(logs.output))
        self.assertEqual(c.top(), [])

    def test_invalid_url(self):
        c = ChatStats("::not a url secret::")
        c.start()
        self.assertEqual(c.describe(), {"url": "", "backend": None, "error": "invalid database URL"})
        self.assertEqual(c.top(), [])


class FlushErrorTest(Base):
    def failing_apply(self):
        real = ChatStats._apply

        def apply(s, batch, stream=True):
            real(s, batch, stream)                          # written, then the transaction fails
            raise sa.exc.OperationalError("UPDATE", {}, Exception("disk I/O error"))

        return mock.patch.object(ChatStats, "_apply", staticmethod(apply))

    def test_rollback_and_retry(self):
        c = self.make()
        self.stop_writer(c)
        c.record_message("1", "a", "A")
        c.record_message("1", "a", "A")
        c.flush()
        c.record_message("1", "a", "A")
        c.record_message("2", "b", "B")
        with self.failing_apply(), self.assertLogs("deck", level="WARNING") as logs:
            self.assertFalse(c.flush())
            self.assertFalse(c.flush())                    # logged once per minute, not per flush
        self.assertEqual(len([l for l in logs.output if "flush failed" in l]), 1)
        self.assertEqual(self.rows(c)["1"].messages, 2)     # rolled back
        self.assertNotIn("2", self.rows(c))
        c.record_message("1", "a", "A2")                    # recorded while the DB was failing
        c.new_stream()
        c.record_message("2", "b", "B")
        self.assertTrue(c.flush())
        rows = self.rows(c)
        self.assertEqual((rows["1"].messages, rows["1"].stream_messages, rows["1"].display), (4, 0, "A2"))
        self.assertEqual((rows["2"].messages, rows["2"].stream_messages), (2, 1))
        self.assertFalse(c._pending)

    def test_deltas_capped(self):
        c = self.make()
        self.stop_writer(c)
        for i in range(1, 6001):
            c.record_message(str(i), f"u{i}", f"U{i}")
        c.record_message("1", "u1", "U1")                   # now recently active: kept
        with self.failing_apply(), self.assertLogs("deck", level="WARNING") as logs:
            self.assertFalse(c.flush())
        self.assertTrue(any("dropped the pending stats of 1000 " in l for l in logs.output))
        self.assertEqual(len(c._pending), 5000)
        self.assertIn("1", c._pending)
        self.assertNotIn("2", c._pending)
        self.assertNotIn("1001", c._pending)
        self.assertIn("1002", c._pending)
        self.assertEqual(c._pending["1"].messages, 2)
        self.assertTrue(c.flush())
        self.assertEqual(c.totals()["chatters"], 5000)

    def test_query_error_is_sanitised(self):
        c = self.make()
        self.stop_writer(c)
        with mock.patch.object(chatstats, "Session", side_effect=sa.exc.OperationalError(
                "SELECT", {}, Exception(f"lost {c._raw_url}"))):
            with self.assertRaises(chatstats.ChatStatsError) as cm:
                c.top()
        self.assertNotIn(c._raw_url, str(cm.exception))
        self.assertIsNone(cm.exception.__cause__)

    def test_writer_survives_errors(self):
        c = self.make(flush_every=0.01)
        with self.failing_apply(), self.assertLogs("deck", level="WARNING"):
            c.record_message("1", "a", "A")
            c._wake.set()
            self.assertTrue(wait_for(lambda: "flush" in c._last_log))
        self.assertTrue(c._thread.is_alive())
        self.assertTrue(wait_for(lambda: "1" in self.rows(c)))
        self.assertEqual(self.rows(c)["1"].messages, 1)


class ConcurrencyTest(Base):
    def test_threads_exact_totals(self):
        c = self.make(flush_every=0.005)                   # writer flushing while threads record
        users = 50

        def work(t):
            for i in range(1000):
                uid = str((t * 1000 + i) % users + 1)
                c.record_message(uid, f"u{uid}", f"U{uid}")

        threads = [threading.Thread(target=work, args=(t,)) for t in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(c.totals(), {"chatters": users, "messages": 8000, "stream_messages": 8000})
        self.assertEqual(sum(r["messages"] for r in c.top(100)), 8000)
        self.assertEqual({r["messages"] for r in c.top(100)}, {160})

    def test_early_flush_over_5000_users(self):
        c = self.make(flush_every=3600)
        for i in range(1, 5001):
            c.record_message(str(i), f"u{i}", f"U{i}")
        time.sleep(0.2)
        self.assertEqual(len(c._pending), 5000)            # not yet: only above 5000
        c.record_message("5001", "u5001", "U5001")
        self.assertTrue(wait_for(lambda: len(self.rows(c)) == 5001))
        self.assertFalse(c._pending)



class ReviewTest(Base):
    """Fixes from the code and security reviews."""

    # -- password parameter, masking ---------------------------------------------------------

    def test_password_parameter_never_shown(self):
        pw = "hunter2@pw"
        with self.assertLogs("deck", level="DEBUG") as logs:
            c = ChatStats("postgresql+nosuchdriver://u@h/db", password=pw)
            c.start()
            logging.getLogger("deck").debug("marker")
        self.assertEqual(c._url.password, pw)
        self.assertNotIn(pw, repr(c.describe()))
        self.assertIn("***", c.describe()["url"])
        self.assertNotIn(pw, "\n".join(logs.output))
        self.assertNotIn(pw, c._safe(Exception(f"auth failed for {pw} / {c._url}")))
        self.assertNotIn("hunter2", c._safe(Exception("u:hunter2%40pw@h")))   # quoted form too

    def test_password_scrubbed_from_query_errors(self):
        pw = "s3cr3t-value"
        c = self.make(db_url())
        c._password = pw                                   # as if it had been passed in
        self.stop_writer(c)
        with mock.patch.object(chatstats, "Session", side_effect=sa.exc.OperationalError(
                "SELECT", {}, Exception(f"password {pw} rejected"))):
            with self.assertRaises(chatstats.ChatStatsError) as cm:
                c.totals()
        self.assertNotIn(pw, str(cm.exception))

    def test_query_string_secrets_masked(self):
        url = ("postgresql+nosuchdriver://u@h/db?password=x1secret&sslpassword=y2secret"
               "&sslmode=require&api_key=k3secret&Token=t4secret")
        with self.assertLogs("deck", level="DEBUG") as logs:
            c = ChatStats(url)
            c.start()
            logging.getLogger("deck").debug("marker")
        d = c.describe()
        self.assertIn("sslmode=require", d["url"])
        self.assertIn("sslpassword=%2A%2A%2A", d["url"])   # "***", URL-encoded
        safe = c._safe(Exception(f"{url} x1secret y2secret"))
        for secret in ("x1secret", "y2secret", "k3secret", "t4secret"):
            self.assertNotIn(secret, repr(d))
            self.assertNotIn(secret, "\n".join(logs.output))
            self.assertNotIn(secret, safe)

    # -- sqlite_path -----------------------------------------------------------------------

    def test_sqlite_path(self):
        path = lambda u: ChatStats(u).sqlite_path()       # noqa: E731
        self.assertEqual(path("sqlite:////tmp/a/b.db"), "/tmp/a/b.db")
        self.assertEqual(path("sqlite:///rel.db"), os.path.abspath("rel.db"))
        self.assertEqual(path("sqlite:///file:/tmp/c%20d.db?uri=true"), "/tmp/c d.db")
        self.assertEqual(path("sqlite:///file:/tmp/e.db?mode=rwc&uri=true"), "/tmp/e.db")
        for u in ("sqlite://", "sqlite:///:memory:", "sqlite:///file::memory:?uri=true",
                  "sqlite:///file:mem1?mode=memory&cache=shared&uri=true",
                  "sqlite:///file://otherhost/x.db?uri=true", "postgresql://u@h/db", "::bad::"):
            self.assertIsNone(path(u), u)
        from core.paths import state_dir
        self.assertEqual(path(""), os.path.join(state_dir(), "chatstats.db"))

    def test_uri_file_url_is_private(self):
        p = os.path.join(tmpdir(), "uri.db")
        c = self.make(f"sqlite:///file:{p}?uri=true")
        self.assertEqual(c.sqlite_path(), p)
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
        c.record_message("1", "a", "A")
        self.assertEqual(c.totals()["messages"], 1)

    # -- schema --------------------------------------------------------------------------------

    def test_ddl_double_precision_and_utf8mb4(self):
        from sqlalchemy.dialects import mysql, postgresql
        my = str(sa.schema.CreateTable(chatstats.CHATTERS).compile(dialect=mysql.dialect()))
        self.assertIn("first_seen DOUBLE NOT NULL", my)
        self.assertIn("last_seen DOUBLE NOT NULL", my)
        self.assertIn("CHARSET=utf8mb4", my)
        self.assertIn("COLLATE utf8mb4_bin", my)
        meta = str(sa.schema.CreateTable(chatstats.Meta.__table__).compile(dialect=mysql.dialect()))
        self.assertIn("CHARSET=utf8mb4", meta)
        pg = str(sa.schema.CreateTable(chatstats.CHATTERS).compile(dialect=postgresql.dialect()))
        self.assertIn("first_seen DOUBLE PRECISION NOT NULL", pg)
        self.assertIn("last_seen DOUBLE PRECISION NOT NULL", pg)
        self.assertIn("points_spent BIGINT NOT NULL", pg)

    def test_timestamps_keep_full_precision(self):
        c = self.make(clock=lambda: 1_793_489_757.913_871_5)
        c.record_message("1", "a", "A")
        self.assertEqual(c.user("1")["first_seen"], 1_793_489_757.913_871_5)

    # -- one bad row ---------------------------------------------------------------------------

    def bad_delta(self, uid="666"):
        d = chatstats._Delta("bad", "Bad", 1.0)
        d.login = None                                      # NOT NULL: IntegrityError on insert
        d.messages = d.stream_messages = 1
        with self.stats._lock:
            self.stats._pending[uid] = d

    def test_bad_row_dropped_not_wedged(self):
        c = self.stats = self.make()
        self.stop_writer(c)
        c.record_message("1", "a", "A")
        c.flush()
        c.record_message("1", "a", "A")
        self.bad_delta()
        c.record_message("2", "b", "B")
        c.new_stream()
        c.record_message("2", "b", "B")
        with self.assertLogs("deck", level="WARNING") as logs:
            self.assertTrue(c.flush())
        self.assertTrue(any("user id 666" in l and "rejected" in l for l in logs.output))
        self.assertFalse(c._pending)                        # never kept for a retry
        rows = self.rows(c)
        self.assertNotIn("666", rows)
        self.assertEqual((rows["1"].messages, rows["1"].stream_messages), (2, 0))
        self.assertEqual((rows["2"].messages, rows["2"].stream_messages), (2, 1))
        c.record_message("3", "c", "C")                     # the store keeps working
        self.assertEqual(c.totals()["chatters"], 3)

    def test_bad_row_warning_rate_limited(self):
        c = self.stats = self.make()
        self.stop_writer(c)
        with self.assertLogs("deck", level="DEBUG") as logs:
            self.bad_delta("1")
            self.bad_delta("2")
            c.flush()
            self.bad_delta("3")
            c.flush()
        warnings = [l for l in logs.output if l.startswith("WARNING") and "rejected" in l]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(len([l for l in logs.output if "rejected" in l]), 3)

    def test_bad_row_in_reset_stream(self):
        c = self.stats = self.make()
        self.stop_writer(c)
        c.record_message("1", "a", "A")
        c.flush()
        c.record_message("1", "a", "A")
        self.bad_delta()
        with self.assertLogs("deck", level="WARNING"):
            c.reset("stream")
        rows = self.rows(c)
        self.assertEqual((rows["1"].messages, rows["1"].stream_messages), (2, 0))
        self.assertNotIn("666", rows)
        self.assertFalse(c._pending)

    def test_db_failure_during_row_retry_keeps_the_rest(self):
        c = self.make()
        self.stop_writer(c)
        for uid in ("1", "2", "3"):
            c.record_message(uid, f"u{uid}", f"U{uid}")
        real, calls = ChatStats._apply, []

        def apply(s, batch, stream=True):
            calls.append(list(batch))
            if len(calls) == 1:
                raise sa.exc.IntegrityError("INSERT", {}, Exception("constraint"))
            if list(batch) == ["2"]:
                raise sa.exc.OperationalError("INSERT", {}, Exception("database is locked"))
            real(s, batch, stream)

        with mock.patch.object(ChatStats, "_apply", staticmethod(apply)), \
                self.assertLogs("deck", level="WARNING"):
            self.assertFalse(c.flush())
        self.assertEqual(list(self.rows(c)), ["1"])
        self.assertEqual(list(c._pending), ["2", "3"])      # not a data error: kept
        self.assertTrue(c.flush())
        self.assertEqual(c.totals()["messages"], 3)

    # -- timeouts ------------------------------------------------------------------------------

    def test_driver_connect_args(self):
        args = lambda u: chatstats.driver_connect_args(sa.engine.make_url(u))   # noqa: E731
        pg = {"connect_timeout": 5, "options": "-c statement_timeout=5000"}
        self.assertEqual(args("postgresql+psycopg://u@h/db"), pg)
        self.assertEqual(args("postgresql+psycopg2://u@h/db"), pg)
        self.assertEqual(args("postgresql://u@h/db"), pg)          # default driver is psycopg(2)
        self.assertEqual(args("postgresql+pg8000://u@h/db"), {"timeout": 5})
        my = {"connect_timeout": 5, "read_timeout": 10, "write_timeout": 10}
        self.assertEqual(args("mysql+pymysql://u@h/db"), my)
        self.assertEqual(args("mysql+mysqldb://u@h/db"), my)
        self.assertEqual(args("mariadb+mariadbconnector://u@h/db"), my)
        self.assertEqual(args("mysql+mysqlconnector://u@h/db"), {"connection_timeout": 5})
        self.assertEqual(args("mssql+pyodbc://u@h/db"), {})        # unknown: nothing it may reject
        self.assertEqual(args("postgresql+psycopg://u@h/db?connect_timeout=30"),
                         {"options": "-c statement_timeout=5000"})   # the user's value wins

    def test_engine_kwargs_for_servers(self):
        c = ChatStats("postgresql+psycopg://u@h/db", password="pw")
        with mock.patch.object(sa, "create_engine") as ce:
            c._make_engine()
        kw = ce.call_args.kwargs
        self.assertEqual((kw["pool_pre_ping"], kw["pool_timeout"]), (True, 5))
        self.assertEqual(kw["connect_args"]["connect_timeout"], 5)
        self.assertEqual(ce.call_args.args[0].password, "pw")

    def test_query_skips_preflush_when_writer_busy(self):
        c = self.make()
        self.stop_writer(c)
        c.record_message("1", "a", "A")
        c.flush()
        c.record_message("1", "a", "A")
        held, release = threading.Event(), threading.Event()

        def hold():
            with c._write_lock:
                held.set()
                release.wait(10)

        t = threading.Thread(target=hold)
        t.start()
        try:
            held.wait(5)
            t0 = time.monotonic()
            self.assertEqual(c.user("1")["messages"], 1)    # stored state, no pre-flush
            self.assertLess(time.monotonic() - t0, 3)
            self.assertIn("1", c._pending)
        finally:
            release.set()
            t.join()
        self.assertEqual(c.user("1")["messages"], 2)

    def test_close_is_bounded(self):
        c = self.make()
        self.stop_writer(c)
        c.record_message("1", "a", "A")
        stuck = threading.Event()
        self.addCleanup(stuck.set)

        def apply(s, batch, stream=True):
            stuck.wait(10)
            raise sa.exc.OperationalError("INSERT", {}, Exception("gone"))

        with mock.patch.object(ChatStats, "_apply", staticmethod(apply)), \
                self.assertLogs("deck", level="WARNING") as logs:
            t0 = time.monotonic()
            c.close(timeout=0.3)
            elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 2)
        self.assertTrue(any("did not finish" in l for l in logs.output))
        self.assertIsNone(c._engine)
        c.record_message("1", "a", "A")                     # closed: no-op
        self.assertEqual(c.top(), [])

    # -- file permissions ------------------------------------------------------------------------

    def test_existing_file_tightened(self):
        p = os.path.join(tmpdir(), "loose.db")
        with open(p, "wb"):
            pass
        os.chmod(p, 0o644)
        self.make("sqlite:///" + p)
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)

    def test_symlink_refused(self):
        d = tmpdir()
        target, link = os.path.join(d, "target"), os.path.join(d, "link.db")
        with open(target, "w") as f:
            f.write("keep")
        os.chmod(target, 0o644)
        os.symlink(target, link)
        c = ChatStats("sqlite:///" + link)
        with self.assertLogs("deck", level="WARNING"):
            c.start()
        self.assertTrue(c.describe()["error"])
        self.assertEqual(os.stat(target).st_mode & 0o777, 0o644)
        with open(target) as f:
            self.assertEqual(f.read(), "keep")

    def test_default_url_creates_state_dir(self):
        state = os.path.join(tmpdir(), "fresh", "state")
        with mock.patch.dict(os.environ, {"DECK_STATE_DIR": state}):
            c = self.make("")
            self.assertEqual(c.sqlite_path(), os.path.join(state, "chatstats.db"))
        self.assertEqual(os.stat(state).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(c.sqlite_path()).st_mode & 0o777, 0o600)

    # -- ordering ------------------------------------------------------------------------------

    def test_new_stream_during_failing_flush(self):
        c = self.make()
        self.stop_writer(c)
        c.record_message("1", "a", "A")
        c.flush()
        c.record_message("1", "a", "A")                     # old stream
        c.record_message("2", "b", "B")

        def apply(s, batch, stream=True):
            c.new_stream()                                  # the stream goes live mid-flush
            c.record_message("2", "b", "B")
            raise sa.exc.OperationalError("INSERT", {}, Exception("database is locked"))

        with mock.patch.object(ChatStats, "_apply", staticmethod(apply)), \
                self.assertLogs("deck", level="WARNING"):
            self.assertFalse(c.flush())
        self.assertTrue(c._pending_new_stream)
        self.assertTrue(c.flush())
        rows = self.rows(c)
        self.assertEqual((rows["1"].messages, rows["1"].stream_messages), (2, 0))
        self.assertEqual((rows["2"].messages, rows["2"].stream_messages), (2, 1))

    def test_reset_stream_with_pending_deltas(self):
        c = self.make()
        self.stop_writer(c)
        c.record_message("1", "a", "A")
        c.flush()
        c.record_message("1", "a", "A")                     # pending, before the reset
        c.record_message("2", "b", "B")                     # pending, new user
        c.reset("stream")
        self.assertFalse(c._pending)
        rows = self.rows(c)
        self.assertEqual((rows["1"].messages, rows["1"].stream_messages), (2, 0))
        self.assertEqual((rows["2"].messages, rows["2"].stream_messages), (1, 0))
        c.record_message("2", "b", "B")
        self.assertEqual(c.top(period="stream")[0]["login"], "b")

    def test_top_ties_competition_rank_first_seen(self):
        c = self.make()
        for uid in ("9", "1", "5"):                         # first_seen: 9, then 1, then 5
            c.record_message(uid, f"u{uid}", f"U{uid}")
        for uid in ("1", "9", "1", "9"):
            c.record_message(uid, f"u{uid}", f"U{uid}")
        c.record_message("7", "u7", "U7")
        c.record_message("7", "u7", "U7")
        top = [(r["rank"], r["user_id"], r["messages"]) for r in c.top()]
        self.assertEqual(top, [(1, "9", 3), (1, "1", 3), (3, "7", 2), (4, "5", 1)])
        self.assertEqual([c.user(u)["rank"] for u in ("9", "1", "7", "5")], [1, 1, 3, 4])
        c.new_stream()
        for uid in ("5", "7"):
            c.record_message(uid, f"u{uid}", f"U{uid}")
        self.assertEqual([(r["rank"], r["user_id"]) for r in c.top(period="stream")],
                         [(1, "5"), (1, "7")])              # ties: earlier first_seen first
        self.assertEqual(c.user("7")["stream_rank"], 1)


if __name__ == "__main__":
    unittest.main()
