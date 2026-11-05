"""Chat statistics per Twitch viewer: messages (all time and this stream), commands,
channel-point redemptions. Storage only; it knows nothing about Twitch itself.

    stats = ChatStats("", exclude=["nightbot"])       # "" = sqlite:///<state_dir>/chatstats.db
    stats.start()
    stats.record_message("12345", "someviewer", "SomeViewer")   # chat thread: memory only
    stats.user(login="someviewer")    # {..., "messages": 1, "rank": 1, ...}  (sender/API threads)
    stats.top(10, "stream")
    stats.close()                     # flushes

Any SQLAlchemy URL works, e.g. ChatStats("postgresql+psycopg://user@host/db", password=...)
to move the store to a bigger database; its driver (psycopg, pymysql, ...) is not vendored and
has to be installed by the user. One ChatStats (one daemon) per database: the writer assumes it
is the only one, so two daemons must not point at the same database. A missing driver or an unreachable database never raises out of start(): the store goes
degraded (describe()["error"] says why), recording becomes a no-op and queries come back empty.

The chat thread only ever touches an in-memory dict of pending deltas. A writer thread
("chatstats-writer") folds them into the database every flush_every seconds, or early when
more than MAX_PENDING users are waiting, in one transaction. Queries flush first. The URL is
only ever shown masked; no message text is stored or logged.
"""
import logging
import math
import os
import re
import threading
import time
import urllib.parse

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from . import text as textclean
from .paths import state_dir, state_file
from .twitch_ids import LOGIN_RE, USER_ID_RE

log = logging.getLogger("deck")

SCHEMA_VERSION = "1"
DB_NAME = "chatstats.db"
MAX_PENDING = 5000          # users: early flush above this; cap on deltas kept across DB errors
MAX_TOP = 100
MAX_COST = 2**31 - 1
MAX_DISPLAY = 64
ERROR_LOG_EVERY = 60.0      # seconds between repeats of the same warning
CHUNK = 500                 # ids per IN (...) when looking up existing rows
CONNECT_TIMEOUT = 5         # seconds: an unreachable server must not hang the sender/API threads
POOL_TIMEOUT = 5
PREFLUSH_WAIT = 1.0         # queries skip their pre-flush if the writer is busy this long
DATA_ERRORS = (sa.exc.DataError, sa.exc.IntegrityError)   # the row is bad, not the database
SECRET_KEY_RE = re.compile(r"pass|secret|token|key", re.I)  # URL query keys whose values are masked


class ChatStatsError(RuntimeError):
    """A database error during a query, with a message that is safe to log or show."""


class Base(DeclarativeBase):
    pass


TABLE_ARGS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_bin"}  # emoji names; exact logins


class Chatter(Base):
    __tablename__ = "chatters"
    __table_args__ = TABLE_ARGS
    user_id: Mapped[str] = mapped_column(sa.String(20), primary_key=True)
    login: Mapped[str] = mapped_column(sa.String(25), index=True)
    display: Mapped[str] = mapped_column(sa.String(64))
    messages: Mapped[int] = mapped_column(sa.Integer, default=0, index=True)
    stream_messages: Mapped[int] = mapped_column(sa.Integer, default=0, index=True)
    commands: Mapped[int] = mapped_column(sa.Integer, default=0)
    redemptions: Mapped[int] = mapped_column(sa.Integer, default=0)
    points_spent: Mapped[int] = mapped_column(sa.BigInteger, default=0)
    first_seen: Mapped[float] = mapped_column(sa.Double)      # epoch seconds: needs 53 bits
    last_seen: Mapped[float] = mapped_column(sa.Double)


class Meta(Base):
    __tablename__ = "meta"
    __table_args__ = TABLE_ARGS
    key: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    value: Mapped[str] = mapped_column(sa.String(200))


CHATTERS = Chatter.__table__


class _Delta:
    __slots__ = ("login", "display", "messages", "stream_messages", "commands", "redemptions",
                 "points", "first", "last")

    def __init__(self, login, display, ts):
        self.login, self.display = login, display
        self.messages = self.stream_messages = self.commands = self.redemptions = self.points = 0
        self.first = self.last = ts

    def absorb(self, newer):
        """Fold in a delta recorded after this one (newer names win)."""
        self.login, self.display = newer.login, newer.display
        self.messages += newer.messages
        self.stream_messages += newer.stream_messages
        self.commands += newer.commands
        self.redemptions += newer.redemptions
        self.points += newer.points
        self.first, self.last = min(self.first, newer.first), max(self.last, newer.last)


def clean_display(display, login):
    """ANSI escapes and control/format characters (bidi overrides, zero-width spaces, ...) stripped,
    ZWJ kept so emoji sequences survive, capped at 64; falls back to the login when nothing is left."""
    if not isinstance(display, str):
        return login
    out = textclean.clean(display[:4 * MAX_DISPLAY], newlines="drop", marks=None, collapse=False)
    return out.strip()[:MAX_DISPLAY].strip() or login


def default_url():
    return "sqlite:///" + os.path.join(state_dir(), DB_NAME)


def driver_connect_args(url):
    """Timeouts for the drivers we know take them; nothing for the rest (an unknown keyword
    would make connect() fail). Keys already in the URL's query string are left to the user."""
    backend, driver = url.get_backend_name(), url.get_driver_name()
    args = {}
    if backend == "postgresql":
        if driver in ("psycopg", "psycopg2", "psycopg2cffi"):
            args = {"connect_timeout": CONNECT_TIMEOUT, "options": "-c statement_timeout=5000"}
        elif driver == "pg8000":
            args = {"timeout": CONNECT_TIMEOUT}
    elif backend in ("mysql", "mariadb"):
        if driver in ("mysqldb", "pymysql", "mariadbconnector"):
            args = {"connect_timeout": CONNECT_TIMEOUT, "read_timeout": 10, "write_timeout": 10}
        elif driver == "mysqlconnector":
            args = {"connection_timeout": CONNECT_TIMEOUT}
    return {k: v for k, v in args.items() if k not in url.query}


class ChatStats:
    def __init__(self, url: str, *, password=None, exclude=(), flush_every=10.0, clock=time.time):
        self._raw_url = url or default_url()
        self._password = password or None
        self._default = not url                 # the state dir may not exist yet: create it
        self.flush_every = float(flush_every)
        self.clock = clock
        self._lock = threading.Lock()           # pending deltas (hot path: held for O(1) work)
        self._write_lock = threading.RLock()    # one DB writer: flush, reset
        self._pending = {}                      # user_id -> _Delta, least recently active first
        self._pending_new_stream = False
        self._exclude = frozenset()
        self._engine = None
        self._url = None
        self._error = None
        self._last_log = {}                     # warning kind -> monotonic time last logged
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        try:
            self._url = sa.engine.make_url(self._raw_url)
            if self._password is not None:
                self._url = self._url.set(password=self._password)
        except Exception:
            self._error = "invalid database URL"
        self.set_exclude(exclude)

    # ---- lifecycle -------------------------------------------------------------------------

    def start(self):
        """Create the engine and tables and start the writer. Never raises: on failure the
        store is degraded (describe()["error"])."""
        if self._engine is not None or self._url is None or self._thread is not None:
            return
        try:
            engine = self._make_engine()
        except Exception as e:
            self._fail("cannot open the database", e)
            return
        try:
            Base.metadata.create_all(engine)
            with Session(engine) as s, s.begin():
                row = s.get(Meta, "schema_version")
                if row is None:
                    s.add(Meta(key="schema_version", value=SCHEMA_VERSION))
                elif row.value != SCHEMA_VERSION:
                    log.warning("chat stats: schema_version %s in the database (expected %s)",
                                row.value[:20], SCHEMA_VERSION)
        except Exception as e:
            engine.dispose()
            self._fail("cannot open the database", e)
            return
        self._error = None
        self._engine = engine
        self._stop.clear()
        self._thread = threading.Thread(target=self._writer, name="chatstats-writer", daemon=True)
        self._thread.start()
        log.info("chat stats: %s", self.describe()["url"])

    def close(self, timeout=5.0):
        """Flush what's pending, stop the writer, release the engine. Returns within about
        `timeout` seconds even if the database hangs (what couldn't be written is lost, logged)."""
        deadline = time.monotonic() + timeout
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        engine = self._engine
        if engine is None:
            return
        done = threading.Event()

        def final():
            try:
                self.flush()
            except Exception as e:
                log.warning("chat stats: final flush failed: %s", self._safe(e))
            finally:
                done.set()

        threading.Thread(target=final, name="chatstats-close", daemon=True).start()
        if not done.wait(max(0.0, deadline - time.monotonic())):
            log.warning("chat stats: the final flush did not finish within %g s; closing anyway",
                        timeout)
        self._engine = None
        engine.dispose()

    def _make_engine(self):
        url = self._url
        if url.get_backend_name() != "sqlite":
            return sa.create_engine(url, pool_pre_ping=True, pool_timeout=POOL_TIMEOUT,
                                    connect_args=driver_connect_args(url))
        kw = {"connect_args": {"check_same_thread": False}}
        path = self.sqlite_path()
        if path is None:
            kw["poolclass"] = sa.pool.StaticPool    # one shared connection, or each sees its own DB
        else:
            if self._default:
                state_file(DB_NAME)                 # creates the state dir (0700)
            self._private_file(path)
        engine = sa.create_engine(url, **kw)

        @sa.event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

        return engine

    @staticmethod
    def _private_file(path):
        """Create the DB file 0600 before SQLite sees it (its -wal/-shm copy the mode), and
        tighten an existing one. A symlink is refused rather than followed."""
        fd = os.open(path, os.O_CREAT | os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)

    def sqlite_path(self) -> str | None:
        """Absolute path of the SQLite DB file; None for memory DBs, other backends, or URI
        forms that don't name a local file."""
        url = self._url
        if url is None or url.get_backend_name() != "sqlite":
            return None
        db = url.database or ""
        if str(url.query.get("uri", "")).lower() in ("true", "1"):
            parts = urllib.parse.urlsplit(db)
            if parts.scheme != "file" or parts.netloc not in ("", "localhost"):
                return None
            if "memory" in urllib.parse.parse_qs(parts.query).get("mode", []) or \
                    url.query.get("mode") == "memory":
                return None
            db = urllib.parse.unquote(parts.path)
        if db in ("", ":memory:"):
            return None
        return os.path.abspath(db)

    # ---- errors (never with credentials) -----------------------------------------------------

    def _secrets(self):
        out = {self._raw_url, self._password}
        u = self._url
        if u is not None:
            out.add(u.render_as_string(hide_password=False))
            if u.password is not None:
                out.add(str(u.password))
            for k, v in u.query.items():
                if SECRET_KEY_RE.search(k):
                    out.update(v if isinstance(v, tuple) else (v,))
        quoted = {urllib.parse.quote(x, safe="") for x in out if x}
        quoted |= {urllib.parse.quote_plus(x) for x in out if x}
        return sorted((x for x in out | quoted if x), key=len, reverse=True)

    def _safe(self, exc):
        text = f"{type(exc).__name__}: {exc}"
        for secret in self._secrets():
            text = text.replace(secret, "***")
        return text.replace("\n", " ")[:300]

    def _warn_limited(self, kind, msg, *args):
        """A warning at most once per ERROR_LOG_EVERY per kind; debug in between."""
        now = time.monotonic()
        if now - self._last_log.get(kind, -math.inf) >= ERROR_LOG_EVERY:
            self._last_log[kind] = now
            log.warning(msg, *args)
        else:
            log.debug(msg, *args)

    def _fail(self, what, exc):
        self._error = f"{what}: {self._safe(exc)}"
        log.warning("chat stats disabled (%s): %s", self.describe()["url"], self._error)

    # ---- hot path ------------------------------------------------------------------------------

    def record_message(self, user_id, login, display, ts=None):
        if not isinstance(ts, (int, float)) or isinstance(ts, bool) or not math.isfinite(ts):
            ts = None
        self._record(user_id, login, display, ts, messages=1)

    def record_command(self, user_id, login, display):
        self._record(user_id, login, display, None, commands=1)

    def record_redemption(self, user_id, login, display, cost=0):
        if not isinstance(cost, int) or isinstance(cost, bool) or not 0 <= cost <= MAX_COST:
            log.debug("chat stats: ignored a redemption with an invalid cost")
            return
        self._record(user_id, login, display, None, redemptions=1, points=cost)

    def _record(self, user_id, login, display, ts, messages=0, commands=0, redemptions=0, points=0):
        if self._engine is None:
            return
        if not isinstance(user_id, str) or not USER_ID_RE.fullmatch(user_id):
            log.debug("chat stats: ignored an invalid user id")
            return
        login = login.lower() if isinstance(login, str) else ""
        if not LOGIN_RE.fullmatch(login):
            log.debug("chat stats: ignored an invalid login")
            return
        if login in self._exclude:
            return
        display = clean_display(display, login)
        if ts is None:
            ts = self.clock()
        with self._lock:
            d = self._pending.pop(user_id, None)    # re-inserted: dict order = least recently active first
            if d is None:
                d = _Delta(login, display, ts)
            else:
                d.login, d.display = login, display
                d.first, d.last = min(d.first, ts), max(d.last, ts)
            self._pending[user_id] = d
            d.messages += messages
            d.stream_messages += messages
            d.commands += commands
            d.redemptions += redemptions
            d.points += points
            early = len(self._pending) > MAX_PENDING
        if early:
            self._wake.set()

    def new_stream(self):
        """The stream went live: per-stream counters restart. Queued with the deltas, so messages
        recorded before this call count all-time but not towards the new stream."""
        if self._engine is None:
            return
        with self._lock:
            self._pending_new_stream = True
            for d in self._pending.values():
                d.stream_messages = 0

    def set_exclude(self, logins):
        ex = frozenset(l.lower() for l in (logins or ()) if isinstance(l, str)
                       and LOGIN_RE.fullmatch(l.lower()))
        with self._lock:
            self._exclude = ex
            for uid in [uid for uid, d in self._pending.items() if d.login in ex]:
                del self._pending[uid]

    # ---- writer --------------------------------------------------------------------------------

    def _writer(self):
        while not self._stop.is_set():
            self._wake.wait(self.flush_every)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                ok = self.flush()
            except Exception as e:   # never let the writer die
                log.warning("chat stats: writer error: %s", self._safe(e))
                ok = False
            if not ok:
                self._stop.wait(self.flush_every)    # back off: don't hammer a failing database

    def flush(self):
        """Write every pending delta in one transaction. On a DB error the transaction is rolled
        back and the deltas are kept for the next flush (capped at MAX_PENDING users, least
        recently active dropped). Returns True on success (or nothing to do)."""
        with self._write_lock:
            engine = self._engine
            if engine is None:
                return True
            with self._lock:
                batch, self._pending = self._pending, {}
                new_stream, self._pending_new_stream = self._pending_new_stream, False
            if not batch and not new_stream:
                return True
            err = self._write(engine, batch, zero_stream=new_stream, stream=True, restore_flag=new_stream)
            if err is not None:
                self._warn_limited("flush", "chat stats: flush failed, will retry: %s", self._safe(err))
                return False
            return True

    def _write(self, engine, batch, *, zero_stream, stream, restore_flag):
        """One transaction for everything; if the database rejects a row (DataError /
        IntegrityError), the reset statement alone and then each row in its own transaction, so
        one bad row can't wedge the store: rows that still fail are dropped (never kept for a
        retry). Any other error puts what wasn't written back into pending and is returned."""
        zero = sa.update(CHATTERS).where(CHATTERS.c.stream_messages != 0).values(stream_messages=0)
        try:
            with Session(engine) as s, s.begin():
                if zero_stream:
                    s.execute(zero)
                self._apply(s, batch, stream)
            return None
        except DATA_ERRORS:
            pass
        except Exception as e:
            self._restore(batch, restore_flag)
            return e
        if zero_stream:
            try:
                with Session(engine) as s, s.begin():
                    s.execute(zero)
            except Exception as e:
                self._restore(batch, restore_flag)
                return e
        if not stream:              # these counts belong to the stream that was just reset
            for d in batch.values():
                d.stream_messages = 0
        items = list(batch.items())
        for i, (uid, d) in enumerate(items):
            try:
                with Session(engine) as s, s.begin():
                    self._apply(s, {uid: d}, stream)
            except DATA_ERRORS as e:
                self._warn_limited("bad-row", "chat stats: dropped the stats of user id %s, the "
                                   "database rejected them: %s", uid, self._safe(e))
            except Exception as e:
                self._restore(dict(items[i:]), False)   # the reset (if any) is committed
                return e
        return None

    @staticmethod
    def _apply(s, batch, stream=True):
        """Upsert the deltas: select which ids exist, then one executemany each for inserts and
        increments. Safe because this writer is the only one. stream=False drops the per-stream
        counts (they belong to a stream that was just reset)."""
        ids = list(batch)
        existing = set()
        for i in range(0, len(ids), CHUNK):
            existing.update(s.scalars(sa.select(CHATTERS.c.user_id)
                                      .where(CHATTERS.c.user_id.in_(ids[i:i + CHUNK]))))
        inserts = [{"user_id": uid, "login": d.login, "display": d.display, "messages": d.messages,
                    "stream_messages": d.stream_messages if stream else 0, "commands": d.commands,
                    "redemptions": d.redemptions, "points_spent": d.points,
                    "first_seen": d.first, "last_seen": d.last}
                   for uid, d in batch.items() if uid not in existing]
        updates = [{"uid": uid, "nlogin": d.login, "ndisplay": d.display, "dm": d.messages,
                    "dsm": d.stream_messages if stream else 0, "dc": d.commands, "dr": d.redemptions,
                    "dp": d.points, "last": d.last}
                   for uid, d in batch.items() if uid in existing]
        if inserts:
            s.execute(sa.insert(CHATTERS), inserts)
        if updates:
            c, bp, i = CHATTERS.c, sa.bindparam, sa.Integer   # typed: no guessing on Postgres
            last = bp("last", type_=sa.Double)
            s.execute(sa.update(CHATTERS).where(c.user_id == bp("uid", type_=sa.String)).values(
                login=bp("nlogin", type_=sa.String), display=bp("ndisplay", type_=sa.String),
                messages=c.messages + bp("dm", type_=i),
                stream_messages=c.stream_messages + bp("dsm", type_=i),
                commands=c.commands + bp("dc", type_=i), redemptions=c.redemptions + bp("dr", type_=i),
                points_spent=c.points_spent + bp("dp", type_=sa.BigInteger),
                last_seen=sa.case((c.last_seen < last, last), else_=c.last_seen),
            ), updates)

    def _restore(self, batch, new_stream):
        """Put a failed batch back in front of whatever was recorded meanwhile."""
        with self._lock:
            newer = self._pending
            if self._pending_new_stream:        # a later new_stream() wipes the old stream counts
                for d in batch.values():
                    d.stream_messages = 0
            for uid, d in newer.items():
                old = batch.pop(uid, None)
                if old is not None:
                    old.absorb(d)
                    d = old
                batch[uid] = d
            for uid in [uid for uid, d in batch.items() if d.login in self._exclude]:
                del batch[uid]
            drop = len(batch) - MAX_PENDING
            if drop > 0:
                for uid in list(batch)[:drop]:
                    del batch[uid]
                self._warn_limited("dropped", "chat stats: the database is failing; dropped the "
                                   "pending stats of %d least recently active users", drop)
            self._pending = batch
            self._pending_new_stream = self._pending_new_stream or new_stream

    # ---- queries (sender/API threads) --------------------------------------------------------

    def _visible(self):
        return [CHATTERS.c.login.not_in(sorted(self._exclude))] if self._exclude else []

    def _query(self, fn):
        engine = self._engine
        if engine is None:
            return None
        if self._write_lock.acquire(timeout=PREFLUSH_WAIT):    # writer stuck: serve what's stored
            try:
                self.flush()
            finally:
                self._write_lock.release()
        try:
            with Session(engine) as s:
                return fn(s)
        except Exception as e:
            raise ChatStatsError(f"chat stats query failed: {self._safe(e)}") from None

    def user(self, user_id=None, login=None) -> dict | None:
        c = CHATTERS.c
        if user_id is not None:
            if not isinstance(user_id, str) or not USER_ID_RE.fullmatch(user_id):
                return None
            where = c.user_id == user_id
        elif isinstance(login, str) and LOGIN_RE.fullmatch(login.lower()):
            where = c.login == login.lower()
        else:
            return None

        def run(s):
            row = s.execute(sa.select(CHATTERS).where(where, *self._visible())
                            .order_by(c.last_seen.desc()).limit(1)).mappings().first()
            if row is None:
                return None
            out = {k: row[k] for k in ("user_id", "login", "display", "messages", "stream_messages",
                                       "commands", "redemptions", "points_spent",
                                       "first_seen", "last_seen")}
            out["rank"] = 1 + s.scalar(sa.select(sa.func.count()).select_from(CHATTERS)
                                       .where(c.messages > row["messages"], *self._visible()))
            out["stream_rank"] = None if not row["stream_messages"] else 1 + s.scalar(
                sa.select(sa.func.count()).select_from(CHATTERS)
                .where(c.stream_messages > row["stream_messages"], *self._visible()))
            return out

        return self._query(run)

    def top(self, n=10, period="all") -> list[dict]:
        """Highest message counts first, ties by who was here first. Ranks are competition ranks
        (equal counts share a rank), matching user()["rank"]."""
        if not isinstance(n, int) or isinstance(n, bool):
            raise ValueError("n must be an integer")
        if period not in ("all", "stream"):
            raise ValueError('period must be "all" or "stream"')
        n = max(1, min(MAX_TOP, n))
        c = CHATTERS.c
        col = c.messages if period == "all" else c.stream_messages

        def run(s):
            rows = s.execute(sa.select(c.user_id, c.login, c.display, col.label("messages"))
                             .where(col > 0, *self._visible())
                             .order_by(col.desc(), c.first_seen.asc(), c.user_id.asc())
                             .limit(n)).all()
            out, rank, prev = [], 0, None
            for i, r in enumerate(rows, 1):
                if r.messages != prev:
                    rank, prev = i, r.messages
                out.append({"rank": rank, "user_id": r.user_id, "login": r.login,
                            "display": r.display, "messages": r.messages})
            return out

        return self._query(run) or []

    def totals(self) -> dict:
        c = CHATTERS.c

        def run(s):
            row = s.execute(sa.select(sa.func.count(), sa.func.coalesce(sa.func.sum(c.messages), 0),
                                      sa.func.coalesce(sa.func.sum(c.stream_messages), 0))
                            .select_from(CHATTERS).where(*self._visible())).one()
            return {"chatters": int(row[0]), "messages": int(row[1]), "stream_messages": int(row[2])}

        return self._query(run) or {"chatters": 0, "messages": 0, "stream_messages": 0}

    def reset(self, scope="all"):
        """"all": delete every chatter row; "stream": zero every stream_messages. Pending deltas
        are folded into the same transaction (for "all" they are discarded with the rows)."""
        if scope not in ("all", "stream"):
            raise ValueError('scope must be "all" or "stream"')
        with self._write_lock:
            engine = self._engine
            if engine is None:
                return
            with self._lock:
                batch, self._pending = self._pending, {}
                new_stream, self._pending_new_stream = self._pending_new_stream, False
            if scope == "all":
                try:
                    with Session(engine) as s, s.begin():
                        s.execute(sa.delete(CHATTERS))
                    err = None
                except Exception as e:
                    self._restore(batch, new_stream)
                    err = e
            else:
                err = self._write(engine, batch, zero_stream=True, stream=False,
                                  restore_flag=new_stream)
            if err is not None:
                raise ChatStatsError(f"chat stats reset failed: {self._safe(err)}") from None
        log.info("chat stats: reset %s", scope)

    def describe(self) -> dict:
        if self._url is None:
            return {"url": "", "backend": None, "error": self._error}
        u = self._url
        masked = u.set(query={k: "***" if SECRET_KEY_RE.search(k) else v for k, v in u.query.items()})
        return {"url": masked.render_as_string(hide_password=True),
                "backend": self._url.get_backend_name(), "error": self._error}
