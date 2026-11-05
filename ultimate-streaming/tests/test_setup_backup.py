"""External setup archives (core/setup_backup.py): create, list, retention, staged restore."""
import collections
import hashlib
import io
import json
import os
import shutil
import stat
import tarfile
import unittest
from unittest import mock

from tests.helpers import SANDBOX, tmpdir

from core import setup_backup as sb  # noqa: E402
from core.safefs import state_home  # noqa: E402

NOT_RUNNING = staticmethod(lambda kind: False)


def ext_dir():
    return os.path.join(state_home(), "backups", "external")


def write(path, data, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data.encode() if isinstance(data, str) else data)
    os.chmod(path, mode)


def read(path):
    with open(path, "rb") as f:
        return f.read()


def make_obs():
    """A fake ~/.config/obs-studio with the files we want and several we must never take."""
    parent = tmpdir()
    root = os.path.join(parent, "obs-studio")
    files = {
        "global.ini": f"[General]\nX=1\n\n[Locations]\nConfiguration={parent}\n"
                      f"SceneCollections={parent}\nProfiles={parent}\n",
        "basic/profiles/Main/basic.ini": "[General]\nName=Main\n",
        "basic/profiles/Main/service.json": '{"settings": {"key": "live_SECRET"}}',
        "basic/scenes/Main.json": '{"name": "Main"}',
        "basic/scenes/Main.json.bak": '{"name": "Main old"}',
        "basic/scenes/notes.txt": "not a collection",
        "plugin_config/obs-websocket/config.json": '{"server_password": "pw"}',
        "plugin_config/obs-browser/Cookies": "session cookies",
        "logs/2026-11-01.txt": "log",
    }
    for rel, text in files.items():
        write(os.path.join(root, rel), text, 0o600 if rel.endswith("service.json") else 0o644)
    return root


OBS_EXPECTED = ["basic/profiles/Main/basic.ini", "basic/profiles/Main/service.json",
                "basic/scenes/Main.json", "basic/scenes/Main.json.bak",
                "plugin_config/obs-websocket/config.json"]


def obs_item(root):
    return {"kind": "obs", "root": root, "include": sb.OBS_INCLUDE, "label": "OBS"}


def members(archive_id):
    with tarfile.open(os.path.join(ext_dir(), archive_id + ".tar.gz")) as tf:
        return {m.name: m for m in tf.getmembers()}


def manifest(archive_id):
    with tarfile.open(os.path.join(ext_dir(), archive_id + ".tar.gz")) as tf:
        return json.load(tf.extractfile("manifest.json"))


def write_raw_archive(archive_id, mani, entries):
    """A hand-made archive: entries = [(TarInfo, bytes or None)] plus manifest.json."""
    path = os.path.join(ext_dir(), archive_id + ".tar.gz")
    with tarfile.open(path, "w:gz") as tf:
        for info, data in entries:
            tf.addfile(info, io.BytesIO(data) if data is not None else None)
        body = json.dumps(mani).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(body)
        tf.addfile(info, io.BytesIO(body))
    os.chmod(path, 0o600)
    return archive_id


def snapshot(root):
    out = {}
    for dirpath, _, names in os.walk(root):
        for n in names:
            p = os.path.join(dirpath, n)
            out[os.path.relpath(p, root)] = read(p) if not os.path.islink(p) else ("link", os.readlink(p))
    return out


class Base(unittest.TestCase):
    is_running = NOT_RUNNING

    def setUp(self):
        shutil.rmtree(ext_dir(), ignore_errors=True)

    def restore(self, aid, kind, root, **kw):
        kw.setdefault("is_running", self.is_running)
        return sb.restore(aid, kind, roots={kind: root}, confirm=True, **kw)


class Create(Base):
    def test_round_trip_content_and_modes(self):
        obs = make_obs()
        sc = tmpdir()
        write(os.path.join(sc, "pages", "Main.json"), '{"keys": {}}')
        write(os.path.join(sc, "settings", "settings.json"), "{}", 0o640)
        a = sb.create([obs_item(obs), {"kind": "streamcontroller", "root": sc, "include": None,
                                       "label": "StreamController"}], label="Onboarding", pinned=True)
        self.assertEqual(set(a), {"id", "ts", "label", "auto", "pinned", "size", "items"})
        self.assertTrue(a["pinned"])
        self.assertFalse(a["auto"])
        self.assertEqual([(i["kind"], i["files"]) for i in a["items"]], [("obs", 5), ("streamcontroller", 2)])
        self.assertTrue(a["id"].endswith("-onboarding"))
        listed = sb.list_archives()
        self.assertEqual([x["id"] for x in listed], [a["id"]])
        self.assertEqual(listed[0], a)
        self.assertNotIn("SECRET", json.dumps(listed))
        self.assertNotIn(obs, json.dumps(listed))

        before = snapshot(obs)
        svc = os.path.join(obs, "basic/profiles/Main/service.json")
        write(svc, "changed", 0o644)
        write(os.path.join(obs, "basic/scenes/Main.json"), "changed")
        os.unlink(os.path.join(obs, "plugin_config/obs-websocket/config.json"))
        r = self.restore(a["id"], "obs", obs)
        self.assertEqual({k: r[k] for k in ("ok", "restored")}, {"ok": True, "restored": 5})
        self.assertEqual(snapshot(obs), before)
        self.assertEqual(stat.S_IMODE(os.stat(svc).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(obs, "basic/scenes/Main.json")).st_mode), 0o644)
        pre = [x for x in sb.list_archives() if x["id"] == r["pre_backup"]]
        self.assertEqual(len(pre), 1)
        self.assertTrue(pre[0]["auto"])
        # the pre-restore backup holds the files as they were just before (incl. the changed key)
        self.assertEqual(sorted(f["rel"] for f in manifest(r["pre_backup"])["items"][0]["files"]),
                         ["basic/profiles/Main/basic.ini", "basic/profiles/Main/service.json",
                          "basic/scenes/Main.json", "basic/scenes/Main.json.bak"])

        r2 = self.restore(a["id"], "streamcontroller", sc)
        self.assertEqual(r2["restored"], 2)

    def test_obs_include_patterns_and_browser_never(self):
        obs = make_obs()
        a = sb.create([obs_item(obs)], label="x")
        names = sorted(n for n in members(a["id"]) if n != "manifest.json")
        self.assertEqual(names, ["obs/" + r for r in OBS_EXPECTED])
        files = manifest(a["id"])["items"][0]["files"]
        self.assertEqual(sorted(f["rel"] for f in files), OBS_EXPECTED)
        svc = read(os.path.join(obs, "basic/profiles/Main/service.json"))
        f = next(f for f in files if f["rel"].endswith("service.json"))
        self.assertEqual((f["sha256"], f["size"]), (hashlib.sha256(svc).hexdigest(), len(svc)))
        # even "everything" or an explicit pattern never takes obs-browser
        b = sb.create([{"kind": "obs", "root": obs, "include": None, "label": "OBS"}], label="all")
        c = sb.create([{"kind": "obs", "root": obs, "include": ["plugin_config/**"], "label": "OBS"}], label="pc")
        for aid in (b["id"], c["id"]):
            self.assertFalse(any("obs-browser" in n for n in members(aid)))
        self.assertIn("obs/logs/2026-11-01.txt", members(b["id"]))

    def test_obs_locations_outside_root_noted(self):
        obs = make_obs()
        elsewhere = tmpdir()
        write(os.path.join(elsewhere, "obs-studio/basic/profiles/Other/basic.ini"), "[General]\nName=Other\n")
        with open(os.path.join(obs, "global.ini"), "a") as f:
            f.write(f"\n[Locations]\nProfiles={elsewhere}\n")
        a = sb.create([obs_item(obs)], label="x")
        rels = [f["rel"] for f in manifest(a["id"])["items"][0]["files"]]
        self.assertFalse(any(r.startswith("basic/profiles") for r in rels))
        self.assertIn("basic/scenes/Main.json", rels)
        self.assertTrue(any("[Locations]" in n for n in a["items"][0]["notes"]))
        self.assertNotIn(elsewhere, json.dumps(a))

    def test_fifo_and_symlink_notes(self):
        root = tmpdir()
        outside = os.path.join(tmpdir(), "id_rsa")
        write(outside, "PRIVATE")
        write(os.path.join(root, "real.json"), "{}")
        os.mkfifo(os.path.join(root, "pipe"))
        os.symlink(outside, os.path.join(root, "out.json"))
        os.symlink(os.path.join(root, "real.json"), os.path.join(root, "in.json"))
        a = sb.create([{"kind": "boatswain", "root": root, "include": None, "label": "B"}], label="b")
        notes = a["items"][0]["notes"]
        self.assertIn("pipe: not a regular file, skipped", notes)
        self.assertIn("out.json: symlink outside the folder, skipped", notes)
        self.assertEqual(sorted(n for n in members(a["id"]) if n != "manifest.json"),
                         ["boatswain/in.json", "boatswain/real.json"])
        self.assertEqual(a["items"][0]["files"], 2)

    def test_streamdeck_ui_single_file_root(self):
        home = tmpdir()
        write(os.path.join(home, ".streamdeck_ui.json"), '{"streamdecks": {}}')
        write(os.path.join(home, "unrelated.txt"), "nope")
        a = sb.create([{"kind": "streamdeck_ui", "root": home, "include": [".streamdeck_ui.json"],
                        "label": "streamdeck-ui"}], label="sdui")
        self.assertEqual(sorted(members(a["id"])), ["manifest.json", "streamdeck_ui/.streamdeck_ui.json"])

    def test_permissions_and_location(self):
        a = sb.create([obs_item(make_obs())], label="perm")
        arc = os.path.join(ext_dir(), a["id"] + ".tar.gz")
        side = os.path.join(ext_dir(), a["id"] + ".json")
        self.assertEqual(stat.S_IMODE(os.stat(arc).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(side).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(ext_dir()).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(ext_dir())).st_mode), 0o700)
        state = os.environ["XDG_STATE_HOME"]
        self.assertTrue(state.startswith(SANDBOX))
        self.assertTrue(arc.startswith(os.path.join(state, "ultimate-streaming", "backups", "external") + os.sep))
        self.assertFalse(arc.startswith(os.environ["XDG_CONFIG_HOME"]))
        self.assertEqual([n for n in os.listdir(ext_dir()) if n.startswith(".tmp-")], [])

    def test_auto_dedup(self):
        obs = make_obs()
        a = sb.create([obs_item(obs)], label="before-build", auto=True)
        b = sb.create([obs_item(obs)], label="before-adopt", auto=True)
        self.assertEqual(b["id"], a["id"])
        self.assertEqual(len(sb.list_archives()), 1)
        m = sb.create([obs_item(obs)], label="manual")        # manual archives are always taken
        self.assertNotEqual(m["id"], a["id"])
        write(os.path.join(obs, "basic/scenes/Main.json"), '{"name": "Main", "x": 1}')
        c = sb.create([obs_item(obs)], label="before-build", auto=True)
        self.assertNotEqual(c["id"], a["id"])
        self.assertEqual(len(sb.list_archives()), 3)

    def test_disk_space_refusal(self):
        Usage = collections.namedtuple("Usage", "total used free")
        with mock.patch.object(sb.shutil, "disk_usage", return_value=Usage(0, 0, sb.FREE_MARGIN)):
            with self.assertRaisesRegex(sb.BackupError, "not enough free disk space"):
                sb.create([obs_item(make_obs())], label="x")
        self.assertEqual(sb.list_archives(), [])
        self.assertEqual(os.listdir(ext_dir()), [])

    def test_invalid_input(self):
        with self.assertRaises(ValueError):
            sb.create([{"kind": "elgato", "root": "/tmp", "include": None}], label="x")
        with self.assertRaises(ValueError):
            sb.create([{"kind": "obs", "root": "relative", "include": None}], label="x")
        with self.assertRaises(ValueError):
            sb.create([{"kind": "obs", "root": make_obs(), "include": ["../etc/*"]}], label="x")


class Retention(Base):
    def counter_root(self):
        root = tmpdir()
        n = [0]

        def make(kind="boatswain", auto=False, label="m", pinned=False, payload=b""):
            n[0] += 1
            write(os.path.join(root, "f.json"), f'{{"n": {n[0]}}}'.encode() + payload)
            return sb.create([{"kind": kind, "root": root, "include": None, "label": "B"}],
                             label=label, auto=auto, pinned=pinned)
        return make

    def test_keep_counts_and_pinned_survives(self):
        make = self.counter_root()
        with mock.patch.multiple(sb, KEEP_AUTO=3, KEEP_MANUAL=2):
            first = make(label="onboarding", pinned=True)
            autos = [make(auto=True, label="before-x")["id"] for _ in range(5)]
            manual = [make()["id"] for _ in range(4)]
        ids = [a["id"] for a in sb.list_archives()]
        self.assertIn(first["id"], ids)
        self.assertEqual([i for i in ids if i in autos], autos[:-4:-1])
        self.assertEqual([i for i in ids if i in manual], manual[:-3:-1])
        self.assertEqual(len(ids), 6)
        for aid in set(autos[:2] + manual[:2]):
            self.assertFalse(os.path.exists(os.path.join(ext_dir(), aid + ".json")))

    def test_total_cap(self):
        make = self.counter_root()
        blob = os.urandom(200_000)   # incompressible, so archive sizes are predictable
        first = make(label="onboarding", pinned=True, payload=blob)
        size = first["size"]
        with mock.patch.object(sb, "TOTAL_CAP", int(size * 3.5)):
            made = [make(payload=blob)["id"] for _ in range(5)]
        listed = sb.list_archives()
        ids = [a["id"] for a in listed]
        self.assertIn(first["id"], ids)
        self.assertEqual([i for i in ids if i in made], made[:-3:-1])   # the pinned one counts too
        self.assertLessEqual(sum(a["size"] for a in listed), int(size * 3.5))

    def test_delete_and_pin(self):
        make = self.counter_root()
        a = make(label="onboarding", pinned=True)
        with self.assertRaisesRegex(sb.BackupError, "pinned"):
            sb.delete(a["id"])
        self.assertTrue(os.path.exists(os.path.join(ext_dir(), a["id"] + ".tar.gz")))
        before = read(os.path.join(ext_dir(), a["id"] + ".tar.gz"))
        self.assertFalse(sb.pin(a["id"], False)["pinned"])
        self.assertEqual(read(os.path.join(ext_dir(), a["id"] + ".tar.gz")), before)   # sidecar only
        sb.delete(a["id"])
        self.assertEqual(sb.list_archives(), [])
        with self.assertRaises(sb.NotFound):
            sb.delete(a["id"])
        with self.assertRaises(ValueError):
            sb.delete("../../etc/passwd")
        with self.assertRaises(ValueError):
            sb.pin("nope")

    def test_source_held_during_restore(self):
        """The pre-restore backup's pruning must not remove the archive being restored."""
        root = tmpdir()
        write(os.path.join(root, "f.json"), "old")
        with mock.patch.object(sb, "KEEP_AUTO", 1):
            src = sb.create([{"kind": "boatswain", "root": root, "include": None, "label": "B"}],
                            label="before-x", auto=True)
            write(os.path.join(root, "f.json"), "new")
            r = sb.restore(src["id"], "boatswain", roots={"boatswain": root}, is_running=lambda k: False,
                           confirm=True)
            ids = [a["id"] for a in sb.list_archives()]
            self.assertIn(src["id"], ids)
            self.assertIn(r["pre_backup"], ids)
            self.assertEqual(read(os.path.join(root, "f.json")), b"old")
            sb.create([{"kind": "boatswain", "root": root, "include": ["x"], "label": "B"}],
                      label="before-y", auto=True)   # back to normal retention afterwards
            autos = [a["id"] for a in sb.list_archives() if a["auto"]]
            self.assertNotIn(src["id"], autos)
            self.assertIn(r["pre_backup"], autos)   # its own group: not pushed out by before-y
            self.assertEqual(len(autos), 2)


class PreRestoreRetention(Base):
    """Restore's own backups are a separate group: add-on clicks never push them out."""

    def setUp(self):
        super().setUp()
        self.root = tmpdir()
        self.n = 0

    def item(self):
        return [{"kind": "boatswain", "root": self.root, "include": None, "label": "B"}]

    def put(self, payload=b""):
        self.n += 1
        write(os.path.join(self.root, "f.json"), f'{{"n": {self.n}}}'.encode() + payload)

    def pre_restore(self, payload=b""):
        """A restore whose pre-restore backup holds the current f.json (+ payload)."""
        self.put()
        src = sb.create(self.item(), label="src")
        self.put(payload)
        r = sb.restore(src["id"], "boatswain", roots={"boatswain": self.root}, is_running=lambda k: False,
                       confirm=True)
        sb.pin(src["id"], True)   # keep the sources out of the counts below
        return r["pre_backup"]

    def ids(self):
        return [a["id"] for a in sb.list_archives()]

    def test_addon_clicks_never_prune_pre_restore(self):
        with mock.patch.object(sb, "KEEP_AUTO", 2):
            pre = self.pre_restore()
            addons = []
            for _ in range(12):
                self.put()
                addons.append(sb.create(self.item(), label="before-addon", auto=True)["id"])
        ids = self.ids()
        self.assertIn(pre, ids)
        self.assertEqual([i for i in ids if i in addons], addons[:-3:-1])
        self.assertEqual(next(a for a in sb.list_archives() if a["id"] == pre)["label"], "before-restore")

    def test_pre_restore_keeps_its_own_newest(self):
        with mock.patch.multiple(sb, KEEP_PRE_RESTORE=2, KEEP_AUTO=1):
            self.put()
            auto = sb.create(self.item(), label="before-addon", auto=True)["id"]
            pres = [self.pre_restore() for _ in range(4)]
        ids = self.ids()
        self.assertEqual([i for i in ids if i in pres], pres[:-3:-1])
        self.assertIn(auto, ids)   # pre-restore archives don't count against the auto group either

    def test_pre_restore_dedups_only_within_its_group(self):
        self.put()
        auto = sb.create(self.item(), label="before-addon", auto=True)["id"]
        src = sb.create(self.item(), label="src")
        r = sb.restore(src["id"], "boatswain", roots={"boatswain": self.root}, is_running=lambda k: False,
                       confirm=True)
        self.assertNotEqual(r["pre_backup"], auto)   # same bytes, but its own archive
        again = sb.create(self.item(), label="before-addon", auto=True)["id"]
        self.assertEqual(again, auto)

    def test_cap_prunes_auto_then_manual_then_pre_restore(self):
        blob = os.urandom(200_000)
        pre = self.pre_restore(blob)                 # the oldest unpinned archive
        size = next(a for a in sb.list_archives() if a["id"] == pre)["size"]
        autos = []
        for _ in range(2):
            self.put(blob)
            autos.append(sb.create(self.item(), label="before-addon", auto=True)["id"])
        self.put(blob)
        manual = sb.create(self.item(), label="manual")["id"]
        pinned = sum(a["size"] for a in sb.list_archives() if a["pinned"])
        with mock.patch.object(sb, "TOTAL_CAP", pinned + int(size * 2.5)):
            sb.prune()
        ids = self.ids()
        self.assertFalse(set(autos) & set(ids))
        self.assertIn(pre, ids)
        self.assertIn(manual, ids)
        with mock.patch.object(sb, "TOTAL_CAP", pinned + int(size * 1.5)):
            sb.prune()
        self.assertEqual([i for i in self.ids() if i in (pre, manual)], [pre])
        with mock.patch.object(sb, "TOTAL_CAP", pinned + int(size * 0.5)):
            sb.prune()
        self.assertNotIn(pre, self.ids())   # still counted in the cap once nothing else is left


class Restore(Base):
    def setUp(self):
        super().setUp()
        self.obs = make_obs()
        self.archive = sb.create([obs_item(self.obs)], label="onboarding", pinned=True)

    def changed(self):
        write(os.path.join(self.obs, "basic/scenes/Main.json"), "changed")
        write(os.path.join(self.obs, "plugin_config/obs-websocket/config.json"), "changed ws")
        return snapshot(self.obs)

    def assert_no_new_archives(self):
        self.assertEqual([a["id"] for a in sb.list_archives()], [self.archive["id"]])

    def assert_no_staging(self, root):
        self.assertEqual([n for n in os.listdir(root) if n.startswith(sb.STAGE_PREFIX)], [])

    def test_confirm_required(self):
        with self.assertRaises(ValueError):
            sb.restore(self.archive["id"], "obs", roots={"obs": self.obs}, is_running=lambda k: False)
        with self.assertRaises(ValueError):
            sb.restore(self.archive["id"], "obs", roots={}, is_running=lambda k: False, confirm=True)

    def test_refused_while_running(self):
        cur = self.changed()
        with self.assertRaisesRegex(sb.BackupError, "running"):
            self.restore(self.archive["id"], "obs", self.obs, is_running=lambda k: k == "obs")
        self.assertEqual(snapshot(self.obs), cur)
        self.assert_no_new_archives()

    def test_running_rechecked_after_staging(self):
        cur = self.changed()
        seen = []

        def is_running(kind):
            staged = [n for n in os.listdir(self.obs) if n.startswith(sb.STAGE_PREFIX)]
            seen.append(bool(staged and os.listdir(os.path.join(self.obs, staged[0]))))
            return len(seen) > 1   # OBS starts while we were staging

        with self.assertRaisesRegex(sb.BackupError, "running"):
            self.restore(self.archive["id"], "obs", self.obs, is_running=is_running)
        self.assertEqual(seen[0], False)
        self.assertTrue(seen[-1])
        self.assertEqual(snapshot(self.obs), cur)
        self.assert_no_staging(self.obs)

    def test_running_rechecked_right_before_rename(self):
        cur = self.changed()
        calls = []

        def is_running(kind):
            calls.append(len([a for a in sb.list_archives()]))
            return len(calls) >= 3   # only the last check, after the pre-restore backup

        with self.assertRaisesRegex(sb.BackupError, "running"):
            self.restore(self.archive["id"], "obs", self.obs, is_running=is_running)
        self.assertEqual(calls[-1], 2)   # the pre-restore backup already exists at the final check
        self.assertEqual(snapshot(self.obs), cur)

    def test_corrupted_archive_restores_nothing(self):
        cur = self.changed()
        aid = self.archive["id"]
        path = os.path.join(ext_dir(), aid + ".tar.gz")
        entries = []
        with tarfile.open(path) as tf:
            for m in tf.getmembers():
                data = tf.extractfile(m).read()
                if m.name == "obs/basic/scenes/Main.json.bak":
                    data = b"tampered!" + data[9:]
                entries.append((m, data))
        os.unlink(path)
        with tarfile.open(path, "w:gz") as tf:
            for m, data in entries:
                tf.addfile(m, io.BytesIO(data))
        with self.assertRaisesRegex(sb.BackupError, "checksum"):
            self.restore(aid, "obs", self.obs)
        self.assertEqual(snapshot(self.obs), cur)
        self.assert_no_new_archives()
        self.assert_no_staging(self.obs)

    def unsafe(self, rel, info=None, data=b"evil"):
        aid = "20260101-000000-000000-evil"
        if info is None:
            info = tarfile.TarInfo("obs/" + rel)
            info.size = len(data)
        mani = {"ts": 0, "label": "evil", "auto": False, "items": [{"kind": "obs", "root": self.obs, "files": [
            {"rel": rel, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}]}]}
        write_raw_archive(aid, mani, [(info, data if info.isreg() else None)])
        return aid

    def test_unsafe_members_rejected(self):
        cur = snapshot(self.obs)
        parent = os.path.dirname(self.obs)
        link = tarfile.TarInfo("obs/basic/scenes/Main.json")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        cases = [("../evil.json", None, "unsafe file name"), ("basic/../../evil.json", None, "unsafe file name"),
                 (os.path.join(parent, "evil.json"), None, "unsafe file name"),
                 ("basic/scenes/Main.json", link, "not a regular file")]
        for rel, info, msg in cases:
            aid = self.unsafe(rel, info)
            with self.assertRaisesRegex(sb.BackupError, msg):
                self.restore(aid, "obs", self.obs)
            os.unlink(os.path.join(ext_dir(), aid + ".tar.gz"))
        self.assertFalse(os.path.exists(os.path.join(parent, "evil.json")))
        self.assertEqual(snapshot(self.obs), cur)
        self.assert_no_new_archives()

    def test_symlinked_target_dir_outside_refused(self):
        outside = tmpdir()
        scenes = os.path.join(self.obs, "basic/scenes")
        shutil.move(scenes, os.path.join(outside, "scenes"))
        os.symlink(os.path.join(outside, "scenes"), scenes)
        write(os.path.join(outside, "scenes", "Main.json"), "outside")
        with self.assertRaisesRegex(sb.BackupError, "outside the folder"):
            self.restore(self.archive["id"], "obs", self.obs)
        self.assertEqual(read(os.path.join(outside, "scenes", "Main.json")), b"outside")

    def test_symlinked_target_inside_written_through(self):
        target = os.path.join(self.obs, "basic/scenes/Main.json")
        real = os.path.join(self.obs, "basic/scenes/real-main.json")
        os.rename(target, real)
        write(real, "changed")
        os.symlink(real, target)
        self.restore(self.archive["id"], "obs", self.obs)
        self.assertTrue(os.path.islink(target))
        self.assertEqual(read(real), b'{"name": "Main"}')

    def fail_second_rename(self):
        real = os.replace
        count = [0]

        def fake(src, dst, *a, **kw):
            if os.path.dirname(src).startswith(os.path.join(self.obs, sb.STAGE_PREFIX)):
                count[0] += 1
                if count[0] == 2:
                    raise OSError(5, "simulated I/O error")
            return real(src, dst, *a, **kw)
        return mock.patch("os.replace", side_effect=fake)

    def test_rollback_when_rename_fails(self):
        cur = self.changed()
        with self.fail_second_rename():
            with self.assertRaisesRegex(sb.BackupError, "previous files were put back"):
                self.restore(self.archive["id"], "obs", self.obs)
        self.assertEqual(snapshot(self.obs), cur)
        self.assert_no_staging(self.obs)

    def test_rollback_removes_created_files_and_dirs(self):
        shutil.rmtree(os.path.join(self.obs, "plugin_config/obs-websocket"))   # restored first
        cur = snapshot(self.obs)
        with self.fail_second_rename():
            with self.assertRaises(sb.BackupError):
                self.restore(self.archive["id"], "obs", self.obs)
        self.assertEqual(snapshot(self.obs), cur)
        self.assertFalse(os.path.exists(os.path.join(self.obs, "plugin_config/obs-websocket")))

    def test_extra_target_files_untouched(self):
        extra = os.path.join(self.obs, "basic/scenes/New.json")
        write(extra, '{"name": "New"}')
        self.changed()
        self.restore(self.archive["id"], "obs", self.obs)
        self.assertEqual(read(extra), b'{"name": "New"}')
        self.assertEqual(read(os.path.join(self.obs, "basic/scenes/Main.json")), b'{"name": "Main"}')

    def test_other_root_refused_with_both_paths(self):
        cur = self.changed()
        other = os.path.join(tmpdir(), "obs-studio")
        os.makedirs(other)
        with self.assertRaises(sb.RestoreTargetMismatch) as cm:
            self.restore(self.archive["id"], "obs", other)
        self.assertEqual((cm.exception.archive_root, cm.exception.target_root),
                         (os.path.realpath(self.obs), os.path.realpath(other)))
        self.assertIsInstance(cm.exception, sb.BackupError)
        self.assertEqual(os.listdir(other), [])
        self.assertEqual(snapshot(self.obs), cur)
        self.assert_no_new_archives()

    def test_allow_other_target_restores(self):
        cur = self.changed()
        other = os.path.join(tmpdir(), "obs-studio")
        os.makedirs(other)
        r = self.restore(self.archive["id"], "obs", other, allow_other_target=True)
        self.assertEqual(r["restored"], 5)
        self.assertTrue(r["notes"])
        self.assertEqual(read(os.path.join(other, "basic/profiles/Main/service.json")),
                         b'{"settings": {"key": "live_SECRET"}}')
        self.assertEqual(snapshot(self.obs), cur)   # the folder named in the manifest is never touched

    def test_matching_root_unchanged(self):
        self.changed()
        link = os.path.join(tmpdir(), "alias")
        os.symlink(self.obs, link)   # a different spelling of the same folder is still a match
        for root in (self.obs, link):
            r = self.restore(self.archive["id"], "obs", root)
            self.assertEqual(r["restored"], 5)
            self.assertFalse(r["notes"])
            self.changed()

    def test_archive_without_root_unchanged(self):
        data = b'{"name": "Main"}'
        info = tarfile.TarInfo("obs/basic/scenes/Main.json")
        info.size = len(data)
        mani = {"ts": 0, "label": "old", "auto": False, "items": [{"kind": "obs", "files": [
            {"rel": "basic/scenes/Main.json", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}]}]}
        aid = write_raw_archive("20260101-000000-000000-noroot", mani, [(info, data)])
        other = os.path.join(tmpdir(), "obs-studio")
        os.makedirs(other)
        r = self.restore(aid, "obs", other)
        self.assertEqual(r["restored"], 1)
        self.assertEqual(read(os.path.join(other, "basic/scenes/Main.json")), data)

    def test_missing_kind_and_unknown_id(self):
        with self.assertRaisesRegex(sb.BackupError, "no streamcontroller part"):
            self.restore(self.archive["id"], "streamcontroller", tmpdir())
        with self.assertRaises(sb.NotFound):
            self.restore("20200101-000000-000000-gone", "obs", self.obs)


if __name__ == "__main__":
    unittest.main()
