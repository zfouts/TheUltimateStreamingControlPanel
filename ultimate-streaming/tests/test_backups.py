"""Config revisions and daily bundles (core/backups.py), and their HTTP API."""
import datetime
import json
import os
import shutil
import stat
import threading
import unittest

from tests.helpers import EXAMPLE, ROOT, Deck, free_port, tmpdir

import yaml  # noqa: E402

from core import backups, configio, editor_auth  # noqa: E402

STATE = os.environ["DECK_STATE_DIR"]
BDIR = os.path.join(STATE, "backups")
REV = os.path.join(BDIR, "revisions")
DAILY = os.path.join(BDIR, "daily")

CFG_A = """\
pages:
  main: [a, b]
  twitch: [x]
  brb: [y]
keys:
  a: {label: A}
twitch: {channel: me}
"""


def reset():
    shutil.rmtree(BDIR, ignore_errors=True)


def write(path, text):
    with open(path, "w") as f:
        f.write(text)


def raw(path):
    with open(path, "rb") as f:
        return f.read()


def fake_revision(when, source="file", text="pages: {main: [a]}\n"):
    rid = f"{when.strftime(backups.STAMP)}-{source}"
    os.makedirs(REV, exist_ok=True)
    write(os.path.join(REV, rid + ".yaml"), text)
    with open(os.path.join(REV, rid + ".json"), "w") as f:
        json.dump({"ts": when.timestamp(), "source": source, "label": None, "hash": rid, "size": 1,
                   "summary": "x"}, f)
    return rid


class Base(unittest.TestCase):
    def setUp(self):
        reset()
        self.path = os.path.join(tmpdir(), "config.yaml")
        write(self.path, CFG_A)

    def tearDown(self):
        reset()


class Revisions(Base):
    def test_first_revision_and_layout(self):
        r = backups.record(self.path, "start")
        self.assertEqual(r["summary"], "first revision")
        self.assertEqual(r["source"], "start")
        self.assertEqual(r["hash"], configio.file_hash(self.path))
        self.assertRegex(r["id"], r"^\d{8}-\d{6}-\d{6}-start$")
        y, j = os.path.join(REV, r["id"] + ".yaml"), os.path.join(REV, r["id"] + ".json")
        self.assertEqual(raw(y), raw(self.path))
        self.assertEqual(stat.S_IMODE(os.stat(y).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(REV).st_mode), 0o700)
        with open(j) as f:
            self.assertEqual(set(json.load(f)), {"ts", "source", "label", "hash", "size", "summary"})

    def test_dedupe(self):
        self.assertIsNotNone(backups.record(self.path, "start"))
        self.assertIsNone(backups.record(self.path, "file"))
        self.assertEqual(len(backups.list_revisions()), 1)

    def test_labelled_snapshot_kept_even_if_unchanged(self):
        """"Snapshot now" and "Before import" are bookmarks the panel's Undo relies on."""
        backups.record(self.path, "start")
        snap = backups.record(self.path, "manual", label="Before stream")
        self.assertIsNotNone(snap)
        self.assertEqual(backups.list_revisions()[0]["label"], "Before stream")
        self.assertEqual(len(backups.list_revisions()), 2)

    def test_missing_file_and_bad_source(self):
        self.assertIsNone(backups.record(self.path + ".nope", "file"))
        with self.assertRaises(ValueError):
            backups.record(self.path, "../evil")

    def test_summary_names_changed_pages_and_sections(self):
        backups.record(self.path, "start")
        cfg = yaml.safe_load(CFG_A)
        cfg["pages"]["twitch"] = ["z"]
        cfg["pages"]["brb"].append("w")
        cfg["twitch"]["channel"] = "you"
        write(self.path, yaml.safe_dump(cfg, sort_keys=False))
        r = backups.record(self.path, "file")
        self.assertEqual(r["summary"], "pages: twitch, brb · twitch")
        cfg["keys"]["new"] = {"label": "N"}
        del cfg["pages"]["main"]
        write(self.path, yaml.safe_dump(cfg, sort_keys=False))
        self.assertEqual(backups.record(self.path, "file")["summary"], "pages: main · keys: new")

    def test_summary_layouts_long_and_broken(self):
        self.assertEqual(backups.summarize(b"layouts: {xl: {pages: {main: [a], two: [b]}}}",
                                           b"layouts: {xl: {pages: {main: [a], two: [c]}}}"), "layouts: xl/two")
        long = backups.summarize(b"pages: {}", yaml.safe_dump({"pages": {f"page{i}": [i] for i in range(50)}}).encode())
        self.assertLessEqual(len(long), 120)
        self.assertTrue(long.endswith("…"))
        self.assertEqual(backups.summarize(b"pages: [", b"pages: {}"), "changed")
        self.assertEqual(backups.summarize(b"a: 1  # x\n", b"a: 1\n"), "formatting or comments only")

    def test_newest_first_and_ordered_within_one_microsecond(self):
        for i in range(20):
            write(self.path, CFG_A + f"n: {i}\n")
            backups.record(self.path, "panel" if i % 2 else "before-save")
        revs = backups.list_revisions()
        self.assertEqual(len(revs), 20)
        self.assertEqual(backups.read_revision(revs[0]["id"]), CFG_A + "n: 19\n")
        self.assertEqual([r["id"] for r in revs], sorted((r["id"] for r in revs), reverse=True))
        self.assertEqual(len({r["id"][:22] for r in revs}), 20, "stamps must be unique")

    def test_concurrent_records(self):
        def work(i):
            for j in range(5):
                write(self.path + f".{i}", f"pages: {{p{i}: [{j}]}}\n")
                backups.record(self.path + f".{i}", "file")
        ts = [threading.Thread(target=work, args=(i,)) for i in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        revs = backups.list_revisions()
        self.assertEqual(len(revs), 20)
        self.assertEqual(len({r["id"] for r in revs}), 20)

    def test_legacy_files_listed(self):
        os.makedirs(BDIR, exist_ok=True)
        write(os.path.join(BDIR, "config-20250101-120000-123456.yaml"), "pages: {old: [a]}\n")
        write(os.path.join(BDIR, "config-20250102-120000.yaml"), "pages: {older: [a]}\n")
        backups.record(self.path, "start")
        revs = backups.list_revisions()
        self.assertEqual([r["source"] for r in revs], ["start", "legacy", "legacy"])
        self.assertEqual(revs[1]["id"], "config-20250102-120000")
        self.assertEqual(backups.read_revision("config-20250101-120000-123456"), "pages: {old: [a]}\n")
        self.assertIn("+  twitch: [x]", backups.diff("config-20250101-120000-123456", self.path))

    def test_traversal_ids_rejected(self):
        for bad in ("../x", "config-../../etc", "config-../../etc/passwd", "../../twitch",
                    "20261031-120000-000000-start/../../x", "", None, "20261031-120000-000000-START",
                    "20261031-120000-000000-start\n"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                backups.read_revision(bad)
        with self.assertRaises(LookupError):
            backups.read_revision("20200101-000000-000000-start")
        with self.assertRaises(ValueError):
            backups.restore("../x", self.path)
        with self.assertRaises(ValueError):
            backups.restore_daily("../2026-01-01", self.path)
        with self.assertRaises(LookupError):
            backups.restore_daily("2001-01-01", self.path)

    def test_diff(self):
        r = backups.record(self.path, "start")
        write(self.path, CFG_A.replace("main: [a, b]", "main: [a, c]"))
        d = backups.diff(r["id"], self.path)
        self.assertIn(f"--- revision {r['id']}", d)
        self.assertIn("+++ current", d)
        self.assertIn("-  main: [a, b]", d)
        self.assertIn("+  main: [a, c]", d)

    def test_diff_is_capped(self):
        r = backups.record(self.path, "start")
        write(self.path, "pages:\n" + "".join(f"  p{i}: [{'x' * 40}]\n" for i in range(10000)))
        d = backups.diff(r["id"], self.path)
        self.assertLessEqual(len(d.encode()), backups.MAX_DIFF + 100)
        self.assertIn("diff truncated", d)


class Restore(Base):
    def test_restore_exact_bytes_and_undo(self):
        # odd formatting and comments must come back byte for byte, not re-dumped
        original = "# my precious comment\npages:\n    main:   [a,   b]   # inline\n"
        write(self.path, original)
        first = backups.record(self.path, "start")
        h_new = configio.save(self.path, {"pages": {"main": ["z"]}}, configio.file_hash(self.path))
        edited = raw(self.path)
        self.assertNotEqual(edited, original.encode())

        h = backups.restore(first["id"], self.path)
        self.assertEqual(raw(self.path), original.encode())
        self.assertEqual(h, configio.file_hash(self.path))
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        newest = backups.list_revisions()[0]
        self.assertEqual(newest["source"], "restore")
        self.assertEqual(newest["label"], f"Restored {first['id']}")
        self.assertEqual(newest["hash"], h)

        # undo: the version from just before the restore is a revision too
        before = [r for r in backups.list_revisions() if r["hash"] == h_new]
        self.assertTrue(before)
        backups.restore(before[0]["id"], self.path)
        self.assertEqual(raw(self.path), edited)

    def test_restore_refuses_a_broken_revision(self):
        backups.record(self.path, "start")
        write(self.path, "just: text\n")
        bad = backups.record(self.path, "file")
        write(self.path, CFG_A)
        for text in ("just: text\n", "pages: [\n"):
            with open(os.path.join(REV, bad["id"] + ".yaml"), "w") as f:
                f.write(text)
            with self.assertRaises(ValueError):
                backups.restore(bad["id"], self.path)
            self.assertEqual(raw(self.path), CFG_A.encode(), "a refused restore must not touch the file")

    def test_restore_of_identical_content_is_harmless(self):
        r = backups.record(self.path, "start")
        h = backups.restore(r["id"], self.path)
        self.assertEqual(h, configio.file_hash(self.path))
        self.assertEqual(raw(self.path), CFG_A.encode())


class Daily(Base):
    def test_once_per_day_and_no_secrets(self):
        for name in ("twitch.json", "editor_token", "ha_token", "obs_password", "env"):
            write(os.path.join(STATE, name), "SECRET")
        write(os.path.join(STATE, "stream_history.json"), "[1]")
        try:
            now = datetime.datetime(2026, 10, 31, 9, 0)
            d = backups.daily(self.path, now=now)
            self.assertEqual(d["date"], "2026-10-31")
            self.assertIsNone(backups.daily(self.path, now=now.replace(hour=23)))
            ddir = os.path.join(DAILY, "2026-10-31")
            self.assertEqual(sorted(os.listdir(ddir)), ["config.yaml", "manifest.json", "stream_history.json"])
            self.assertEqual(stat.S_IMODE(os.stat(ddir).st_mode), 0o700)
            self.assertEqual(raw(os.path.join(ddir, "config.yaml")), CFG_A.encode())
            for root, _, files in os.walk(BDIR):
                for f in files:
                    self.assertNotIn(b"SECRET", raw(os.path.join(root, f)), f)
            with open(os.path.join(ddir, "manifest.json")) as f:
                m = json.load(f)
            self.assertEqual(m["files"], ["config.yaml", "stream_history.json"])
            self.assertEqual(m["hash"], configio.file_hash(self.path))
            self.assertIsNotNone(backups.daily(self.path, now=now + datetime.timedelta(days=1)))
            self.assertEqual([d["date"] for d in backups.list_daily()], ["2026-11-01", "2026-10-31"])
            self.assertFalse([n for n in os.listdir(DAILY) if n.startswith(".tmp")])
        finally:
            for name in ("twitch.json", "editor_token", "ha_token", "obs_password", "env", "stream_history.json"):
                os.remove(os.path.join(STATE, name))

    def test_leftover_tmp_is_rebuilt(self):
        os.makedirs(os.path.join(DAILY, ".tmp-2026-10-31"))
        write(os.path.join(DAILY, ".tmp-2026-10-31", "junk"), "x")
        backups.daily(self.path, now=datetime.datetime(2026, 10, 31))
        self.assertNotIn("junk", os.listdir(os.path.join(DAILY, "2026-10-31")))

    def test_restore_daily(self):
        backups.daily(self.path, now=datetime.datetime(2026, 10, 30))
        write(self.path, "pages: {other: [b]}\n")
        backups.restore_daily("2026-10-30", self.path)
        self.assertEqual(raw(self.path), CFG_A.encode())
        revs = backups.list_revisions()
        self.assertEqual(revs[0]["label"], "Restored daily 2026-10-30")
        self.assertEqual(revs[1]["source"], "before-save")
        self.assertEqual(backups.read_revision(revs[1]["id"]), "pages: {other: [b]}\n")

    def test_keeps_30_daily(self):
        start = datetime.datetime(2026, 1, 1)
        for i in range(35):
            backups.daily(self.path, now=start + datetime.timedelta(days=i))
        dates = [d["date"] for d in backups.list_daily()]
        self.assertEqual(len(dates), 30)
        self.assertEqual(dates[0], "2026-02-04")
        self.assertEqual(dates[-1], "2026-01-06")


class Prune(Base):
    def test_keeps_newest_200_and_last_14_days(self):
        now = datetime.datetime(2026, 10, 31, 12, 0)
        old = [fake_revision(now - datetime.timedelta(days=100, minutes=i)) for i in range(250)]
        recent = [fake_revision(now - datetime.timedelta(days=13, minutes=i)) for i in range(220)]
        backups.prune(now=now)
        ids = {r["id"] for r in backups.list_revisions(limit=10000)}
        self.assertEqual(ids, set(recent), "everything younger than 14 days stays; old beyond 200 go")
        self.assertFalse([n for n in os.listdir(REV) if n.endswith(".json") and n[:-5] not in ids])
        reset()
        old = [fake_revision(now - datetime.timedelta(days=100, minutes=i)) for i in range(250)]
        recent = [fake_revision(now - datetime.timedelta(days=1, minutes=i)) for i in range(10)]
        backups.prune(now=now)
        ids = {r["id"] for r in backups.list_revisions(limit=10000)}
        self.assertEqual(len(ids), 200)
        self.assertTrue(set(recent) <= ids)
        self.assertEqual(ids - set(recent), set(sorted(old)[-190:]), "the newest of the old ones stay")

    def test_legacy_left_alone(self):
        os.makedirs(BDIR, exist_ok=True)
        write(os.path.join(BDIR, "config-20200101-000000.yaml"), "pages: {}\n")
        backups.prune(now=datetime.datetime(2026, 10, 31))
        self.assertTrue(os.path.exists(os.path.join(BDIR, "config-20200101-000000.yaml")))


class Save(Base):
    def test_save_records_before_and_after(self):
        configio.save(self.path, {"pages": {"main": ["-"]}}, configio.file_hash(self.path))
        revs = backups.list_revisions()
        self.assertEqual([r["source"] for r in revs], ["panel", "before-save"])
        self.assertEqual(backups.read_revision(revs[1]["id"]), CFG_A)
        self.assertEqual(revs[0]["hash"], configio.file_hash(self.path))
        self.assertEqual(revs[0]["summary"], "pages: main, twitch, brb · keys · twitch")
        self.assertFalse([n for n in os.listdir(BDIR) if n.startswith("config-")], "no more legacy copies")

    def test_save_source_and_dedupe_with_start(self):
        backups.record(self.path, "start")
        configio.save(self.path, {"pages": {"main": ["-"]}}, None, source="import")
        self.assertEqual([r["source"] for r in backups.list_revisions()], ["import", "start"])

    def test_conflict_records_nothing(self):
        h = configio.file_hash(self.path)
        write(self.path, CFG_A + "# hand edit\n")
        with self.assertRaises(configio.Conflict):
            configio.save(self.path, {"pages": {"main": ["-"]}}, h)
        self.assertEqual(backups.list_revisions(), [])

    def test_save_new_file(self):
        p = os.path.join(tmpdir(), "config.yaml")
        configio.save(p, {"pages": {"main": ["-"]}})
        self.assertEqual([r["source"] for r in backups.list_revisions()], ["panel"])


# ------------------------------------------------------------------ HTTP API (real server)

PORT = free_port()


def setUpModule():
    global BOT, CONFIG, BASE, TOKEN
    from core.bot import DeckBot
    CONFIG = os.path.join(tmpdir(), "config.yaml")
    with open(EXAMPLE) as f:
        cfg = yaml.safe_load(f)
    cfg["editor"] = {"port": PORT}
    write(CONFIG, yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
    BOT = DeckBot(root=ROOT, config_path=CONFIG)
    BOT.deck = Deck("Stream Deck XL")
    BOT.load_config(initial=True)
    BOT.load_extensions("modules")
    BASE, TOKEN = f"http://127.0.0.1:{PORT}", editor_auth.token()


def tearDownModule():
    for inst in list(BOT.modules.values()):
        try:
            inst.on_unload()
        except Exception:
            pass


def call(path, body=None):
    import urllib.error
    import urllib.request
    h = {"X-Deck-Token": TOKEN}
    data = None
    if body is not None:
        data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        with e:
            return e.code, json.loads(e.read() or b"{}")


class Api(unittest.TestCase):
    def setUp(self):
        reset()
        with open(EXAMPLE) as f:
            cfg = yaml.safe_load(f)
        cfg["editor"] = {"port": PORT}
        write(CONFIG, yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
        self.original = raw(CONFIG)

    def tearDown(self):
        write(CONFIG, self.original.decode())
        reset()

    def test_bot_records_start_and_daily(self):
        self.assertIsNotNone(BOT.backup("record", "start"))
        BOT.backup("daily")
        status, body = call("/api/backups")
        self.assertEqual(status, 200)
        self.assertEqual(body["revisions"][0]["source"], "start")
        self.assertEqual(body["daily"][0]["date"], datetime.date.today().isoformat())
        self.assertEqual(set(body["revisions"][0]), {"id", "ts", "source", "label", "hash", "size", "summary"})

    def test_bot_backup_failure_is_not_fatal(self):
        saved, BOT.config_path = BOT.config_path, os.path.join(tmpdir(), "missing", "config.yaml")
        try:
            BOT.backup("daily")  # FileNotFoundError is logged, not raised
        finally:
            BOT.config_path = saved

    def test_snapshot_diff_restore_round_trip(self):
        status, body = call("/api/backups/snapshot", {"label": "  known good  "})
        self.assertEqual(status, 200)
        rev = body["revision"]
        self.assertEqual((rev["source"], rev["label"]), ("manual", "known good"))
        self.assertEqual(call("/api/backups/snapshot", {})[1], {"ok": True, "revision": None})

        write(CONFIG, self.original.decode() + "extra_section: 1\n")
        status, body = call(f"/api/backups/diff?id={rev['id']}")
        self.assertEqual(status, 200)
        self.assertIn("+extra_section: 1", body["diff"])
        status, body = call(f"/api/backups/revision?id={rev['id']}")
        self.assertEqual(body["text"], self.original.decode())

        status, body = call("/api/backups/restore", {"id": rev["id"]})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["hash"], configio.file_hash(CONFIG))
        self.assertEqual(raw(CONFIG), self.original)
        revs = call("/api/backups")[1]["revisions"]
        self.assertEqual([r["source"] for r in revs[:3]], ["restore", "before-save", "manual"])

        # and undo it
        status, body = call("/api/backups/restore", {"id": revs[1]["id"]})
        self.assertEqual(status, 200)
        self.assertTrue(raw(CONFIG).endswith(b"extra_section: 1\n"))

    def test_restore_daily(self):
        BOT.backup("daily")
        write(CONFIG, self.original.decode() + "extra_section: 1\n")
        status, body = call("/api/backups/restore-daily", {"date": datetime.date.today().isoformat()})
        self.assertEqual(status, 200)
        self.assertEqual(raw(CONFIG), self.original)

    def test_bad_input(self):
        for path, body in (("/api/backups/revision?id=../x", None),
                           ("/api/backups/diff?id=config-../../etc", None),
                           ("/api/backups/revision", None),
                           ("/api/backups/restore", {"id": "../../twitch"}),
                           ("/api/backups/restore", {"id": 5}),
                           ("/api/backups/restore-daily", {"date": "../2026-01-01"}),
                           ("/api/backups/snapshot", {"label": "x" * 81}),
                           ("/api/backups/snapshot", {"label": 3})):
            with self.subTest(path=path, body=body):
                self.assertEqual(call(path, body)[0], 400)
        self.assertEqual(raw(CONFIG), self.original)

    def test_missing(self):
        self.assertEqual(call("/api/backups/revision?id=20200101-000000-000000-start")[0], 404)
        self.assertEqual(call("/api/backups/diff?id=config-20200101-000000")[0], 404)
        self.assertEqual(call("/api/backups/restore", {"id": "20200101-000000-000000-start"})[0], 404)
        self.assertEqual(call("/api/backups/restore-daily", {"date": "2001-01-01"})[0], 404)
        self.assertEqual(raw(CONFIG), self.original)


if __name__ == "__main__":
    unittest.main()


class RedactTest(unittest.TestCase):
    def test_url_credentials_hidden_in_revision_text(self):
        from core.webapi.backup_api import redact
        text = ('  database_url: "postgresql+psycopg://bob:s3cr3t@db/x?sslpassword=pw&app=y"\n'
                '  url: http://homeassistant.local:8123\n')
        out = redact(text)
        self.assertNotIn("s3cr3t", out)
        self.assertNotIn("=pw", out)
        self.assertIn("bob:***@db/x?sslpassword=***&app=y", out)
        self.assertIn("http://homeassistant.local:8123", out)
