import json
import os
import shutil
import sys
import unittest

from tests import helpers  # noqa: E402,F401  (sandboxes HOME and XDG before core is imported)

from core import detect  # noqa: E402

HOME = os.environ["HOME"]
KEY = "live_SECRETKEY_123456"
SERVER = "rtmp://secret.example/app"


def put(path, text="", mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text if isinstance(text, str) else json.dumps(text))


def obs_tree(root, *, profiles=None, scenes=None, user=None, glob=None):
    for dirname, (name, svc) in (profiles or {}).items():
        d = os.path.join(root, "basic", "profiles", dirname)
        put(os.path.join(d, "basic.ini"), f"[General]\nName={name}\n[Output]\nMode=Advanced\n[AdvOut]\nEncoder=nvenc\n")
        if svc is not None:
            put(os.path.join(d, "service.json"), {"type": "rtmp_common", "settings": {
                "service": "Twitch", "server": SERVER, "key": svc}})
    for fname, (name, names) in (scenes or {}).items():
        put(os.path.join(root, "basic", "scenes", fname), {
            "name": name, "scene_order": [{"name": n} for n in names],
            "sources": [{"id": "scene", "name": n} for n in names] + [{"id": "text", "name": "t"}]})
    if user:
        put(os.path.join(root, "user.ini"), user)
    if glob:
        put(os.path.join(root, "global.ini"), glob)


class DetectBase(unittest.TestCase):
    def setUp(self):
        self.home = helpers.tmpdir()
        self._old = os.environ.copy()
        os.environ.update({"HOME": self.home, "XDG_CONFIG_HOME": os.path.join(self.home, ".config"),
                           "XDG_DATA_HOME": os.path.join(self.home, ".local/share")})
        os.environ.pop("OBS_CONFIG_DIR", None)
        os.environ.pop("STREAMDECK_UI_CONFIG", None)
        self.proc = os.path.join(self.home, "fakeproc")
        os.makedirs(self.proc)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old)
        shutil.rmtree(self.home, ignore_errors=True)

    def scan(self):
        return detect.scan(proc_root=self.proc, use_flatpak=False)

    def apps(self, kind):
        return [a for a in self.scan()["apps"] if a["kind"] == kind]


class ObsTests(DetectBase):
    def test_native_with_locations_and_current(self):
        parent = os.path.join(self.home, "elsewhere")
        root = os.path.join(self.home, ".config/obs-studio")
        obs_tree(os.path.join(parent, "obs-studio"),
                 profiles={"Twitch_Stream_Deck": ("Twitch (Stream Deck)", KEY), "Plain": ("Plain", None)},
                 scenes={"Main.json": ("Main Show", ["Intro", "Game"])})
        put(os.path.join(root, "global.ini"), f"[Locations]\nProfiles={parent}\nSceneCollections={parent}\n"
                                              "[Basic]\nProfile=Old\nSceneCollection=Old\n")
        put(os.path.join(root, "user.ini"), "[Basic]\nProfile=Twitch (Stream Deck)\nSceneCollectionFile=Main\n")
        obs = self.scan()["obs"]
        self.assertEqual([r["kind"] for r in obs["roots"]], ["native"])
        by = {p["dir"]: p for p in obs["profiles"]}
        self.assertEqual(by["Twitch_Stream_Deck"]["name"], "Twitch (Stream Deck)")
        self.assertTrue(by["Twitch_Stream_Deck"]["has_stream_key"])
        self.assertEqual(by["Twitch_Stream_Deck"]["service"], "Twitch")
        self.assertEqual(by["Twitch_Stream_Deck"]["encoder"], "nvenc")
        self.assertFalse(by["Plain"]["has_stream_key"])
        self.assertIsNone(by["Plain"]["service"])
        col = obs["collections"][0]
        self.assertEqual((col["name"], col["scenes"], col["sources"]), ("Main Show", ["Intro", "Game"], 3))
        self.assertEqual(obs["current_profile"], "Twitch (Stream Deck)")   # user.ini beats global.ini
        self.assertEqual(obs["current_collection"], "Main Show")           # via SceneCollectionFile
        self.assertFalse(obs["running"])

    def test_flatpak_and_custom_roots(self):
        fp = os.path.join(self.home, ".var/app/com.obsproject.Studio/config/obs-studio")
        obs_tree(fp, profiles={"P": ("P", None)}, glob="[Basic]\nProfile=P\nSceneCollection=C\n")
        custom = os.path.join(self.home, "custom-obs")
        obs_tree(custom, profiles={"Q": ("Q", None)})
        os.environ["OBS_CONFIG_DIR"] = custom
        obs = self.scan()["obs"]
        self.assertEqual([r["kind"] for r in obs["roots"]], ["flatpak", "custom"])
        self.assertEqual(sorted(p["name"] for p in obs["profiles"]), ["P", "Q"])
        self.assertEqual(obs["current_profile"], "P")                       # falls back to global.ini

    def test_stream_key_never_in_output(self):
        root = os.path.join(self.home, ".config/obs-studio")
        obs_tree(root, profiles={"A": ("A", KEY)})
        out = json.dumps(self.scan())
        self.assertNotIn(KEY, out)
        self.assertNotIn(SERVER, out)
        self.assertNotIn("secret.example", out)

    def test_malformed_json_is_a_warning(self):
        root = os.path.join(self.home, ".config/obs-studio")
        obs_tree(root, profiles={"A": ("A", None)})
        put(os.path.join(root, "basic/profiles/A/service.json"), "{not json " + KEY)
        put(os.path.join(root, "basic/scenes/Bad.json"), "][")
        obs = self.scan()["obs"]
        self.assertTrue(obs["profiles"][0]["warnings"])
        self.assertTrue(obs["collections"][0]["warnings"])
        self.assertNotIn(KEY, json.dumps(obs))

    def test_fifo_and_outside_symlink_skipped(self):
        root = os.path.join(self.home, ".config/obs-studio")
        obs_tree(root, profiles={"A": ("A", None)}, scenes={"Ok.json": ("Ok", ["S"])})
        os.mkfifo(os.path.join(root, "basic/scenes/Pipe.json"))
        secret = os.path.join(self.home, "outside.json")
        put(secret, {"name": "Leaked", "sources": []})
        os.symlink(secret, os.path.join(root, "basic/scenes/Link.json"))
        os.mkfifo(os.path.join(root, "basic/profiles/A/service.json"))
        obs = self.scan()["obs"]
        self.assertEqual([c["name"] for c in obs["collections"]], ["Ok"])
        self.assertNotIn("Leaked", json.dumps(obs))
        self.assertFalse(obs["profiles"][0]["has_stream_key"])

    def test_running(self):
        for pid, exe, argv in (("101", "/usr/bin/vim", ["vim", "obs"]), ("102", "/usr/bin/obs", ["obs"])):
            d = os.path.join(self.proc, pid)
            os.makedirs(d)
            os.symlink(exe, os.path.join(d, "exe"))
            put(os.path.join(d, "cmdline"), "\0".join(argv) + "\0")
        res = self.scan()
        self.assertTrue(res["obs"]["running"])
        self.assertFalse(any(a["running"] for a in res["apps"]))

    def test_running_needs_exact_name_and_flatpak_path(self):
        d = os.path.join(self.proc, "7")
        os.makedirs(d)
        put(os.path.join(d, "cmdline"), "/usr/bin/streamdeck-helper\0--obs-x\0")
        self.assertFalse(self.scan()["obs"]["running"])
        put(os.path.join(d, "cmdline"), "/app/bin/obs\0")
        self.assertTrue(self.scan()["obs"]["running"])


class AppTests(DetectBase):
    def test_streamcontroller(self):
        data = os.path.join(self.home, ".var/app/com.core447.StreamController/data")
        put(os.path.join(data, "pages/A.json"), {"keys": {"0x0": {}, "1x0": {}}})
        put(os.path.join(data, "pages/B.json"), {"keys": {"0x0": {}}})
        put(os.path.join(data, "pages/Bad.json"), "nope")
        put(os.path.join(data, "settings/pages.json"), {"default-pages": {"SER1": "/x", "SER2": "/y"}})
        (a,) = self.apps("streamcontroller")
        self.assertEqual((a["decks"], a["pages"], a["keys"]), (2, 2, 3))
        self.assertEqual(len(a["warnings"]), 1)

    def test_streamdeck_ui_v2_and_v1(self):
        path = os.path.join(self.home, ".streamdeck_ui.json")
        put(path, {"streamdeck_ui_version": 2, "state": {"S1": {"buttons": {
            "0": {"0": {"state": 0, "states": {"0": {"text": "a"}}}, "1": {"state": 0, "states": {"0": {}}}},
            "1": {"0": {"state": 0, "states": {"0": {"keys": "ctrl+a"}}}}}}, "S2": {"buttons": {}}}})
        (a,) = self.apps("streamdeck_ui")
        self.assertEqual((a["decks"], a["pages"], a["keys"]), (2, 2, 2))      # S2 has no pages but is still a deck
        other = os.path.join(self.home, "v1.json")
        put(other, {"streamdeck_ui_version": 1, "state": {"S1": {"buttons": {
            "0": {"0": {"text": "x", "command": "ls"}, "1": {"text": ""}}}}}})
        os.environ["STREAMDECK_UI_CONFIG"] = other
        (a,) = self.apps("streamdeck_ui")
        self.assertEqual((a["decks"], a["pages"], a["keys"]), (1, 1, 1))
        self.assertEqual(a["warnings"], [])

    def test_streamdeck_ui_malformed(self):
        put(os.path.join(self.home, ".streamdeck_ui.json"), "{")
        (a,) = self.apps("streamdeck_ui")
        self.assertTrue(a["warnings"])

    def test_boatswain(self):
        flat = os.path.join(self.home, ".var/app/com.feaneron.Boatswain/data")
        put(os.path.join(flat, "SER.json"), {"active-profile": "u", "profiles": [
            {"id": "u", "name": "Default", "page": {"version": 1, "regions": [{"id": "main-button-grid", "items": [
                {}, {"type": "action", "factory": "default", "action": "default-switch-page-action",
                     "settings": {"page": {"version": 1, "regions": [{"items": [
                         {"type": "action", "factory": "x", "action": "y"}]}]}}}]}]}}]})
        put(os.path.join(flat, "other.json"), {"hello": 1})
        native = os.path.join(self.home, ".local/share")
        put(os.path.join(native, "N.json"), {"active-profile": "u", "profiles": [{"id": "u", "page": []}]})
        put(os.path.join(native, "unrelated.json"), {"a": 1})
        apps = self.apps("boatswain")
        self.assertEqual(len(apps), 2)
        by = {a["label"]: a for a in apps}
        self.assertEqual((by["Boatswain (Flatpak)"]["decks"], by["Boatswain (Flatpak)"]["pages"],
                          by["Boatswain (Flatpak)"]["keys"]), (1, 2, 2))
        self.assertEqual(by["Boatswain"]["decks"], 1)

    def test_elgato(self):
        put(os.path.join(self.home, "Downloads/My Deck.streamDeckProfile"), "zip")
        put(os.path.join(self.home, "Documents/a/b/deep.streamDeckProfile"), "zip")      # depth 2: found
        put(os.path.join(self.home, "Documents/a/b/c/too_deep.streamDeckProfile"), "zip")
        put(os.path.join(self.home, "Desktop/readme.txt"), "x")
        real = os.path.join(self.home, "Desktop/link.streamDeckProfile")
        os.symlink(os.path.join(self.home, "Downloads/My Deck.streamDeckProfile"), real)
        apps = self.apps("elgato")
        self.assertEqual(sorted(a["label"] for a in apps), ["My Deck.streamDeckProfile", "deep.streamDeckProfile"])
        a = apps[0]
        self.assertEqual((a["decks"], a["pages"], a["keys"]), (1, 0, 0))
        self.assertEqual(a["warnings"], ["contents read when you preview"])

    def test_app_running_from_fake_proc(self):
        data = os.path.join(self.home, ".var/app/com.core447.StreamController/data")
        put(os.path.join(data, "pages/A.json"), {"keys": {}})
        d = os.path.join(self.proc, "55")
        os.makedirs(d)
        put(os.path.join(d, "cmdline"), "python3\0/opt/streamcontroller\0")
        (a,) = self.apps("streamcontroller")
        self.assertTrue(a["running"])


class ResolveTests(DetectBase):
    def test_resolve(self):
        data = os.path.join(self.home, ".var/app/com.core447.StreamController/data")
        put(os.path.join(data, "pages/A.json"), {"keys": {}})
        root = os.path.join(self.home, ".config/obs-studio")
        obs_tree(root, profiles={"A": ("A", None)})
        res = self.scan()
        sid = res["apps"][0]["source_id"]
        self.assertEqual(len(sid), 24)
        self.assertEqual(detect.resolve(sid), ("streamcontroller", os.path.realpath(data)))
        self.assertEqual(detect.resolve(res["obs"]["roots"][0]["source_id"]), ("obs", os.path.realpath(root)))
        with self.assertRaises(KeyError):
            detect.resolve("0" * 24)
        shutil.rmtree(data)
        self.scan()
        with self.assertRaises(KeyError):       # the map is rebuilt by each scan
            detect.resolve(sid)

    def test_output_is_json_and_paths_free(self):
        root = os.path.join(self.home, ".config/obs-studio")
        obs_tree(root, profiles={"A": ("A", KEY)}, scenes={"S.json": ("S", ["x"])})
        out = json.dumps(self.scan())
        self.assertNotIn(self.home, out)


class ReadOnlyTests(DetectBase):
    def test_nothing_modified(self):
        root = os.path.join(self.home, ".config/obs-studio")
        obs_tree(root, profiles={"A": ("A", KEY)}, scenes={"S.json": ("S", ["x"])},
                 user="[Basic]\nProfile=A\n")
        put(os.path.join(self.home, ".streamdeck_ui.json"), {"streamdeck_ui_version": 2, "state": {}})
        put(os.path.join(self.home, "Downloads/x.streamDeckProfile"), "zip")

        def snap():
            out = {}
            for dp, dn, fn in os.walk(self.home):
                for n in dn + fn:
                    p = os.path.join(dp, n)
                    st = os.lstat(p)
                    out[p] = (st.st_mtime_ns, st.st_size)
            return out
        before = snap()
        self.scan()
        self.assertEqual(before, snap())


if __name__ == "__main__":
    unittest.main()


class ActiveRootTests(DetectBase):
    """Which OBS folder to back up / restore when a leftover install is lying around."""

    def roots(self):
        native = os.path.join(self.home, ".config/obs-studio")
        flat = os.path.join(self.home, ".var/app/com.obsproject.Studio/config/obs-studio")
        obs_tree(native, profiles={"Old": ("Old", None)}, user="[Basic]\nProfile=Old\n")
        obs_tree(flat, profiles={"Live": ("Live", "k")}, user="[Basic]\nProfile=Live\n")
        return native, flat

    def test_running_flatpak_wins_over_leftover_native(self):
        native, flat = self.roots()
        os.utime(os.path.join(native, "user.ini"), (2e9, 2e9))      # leftover is even "newer"
        d = os.path.join(self.proc, "9")
        os.makedirs(d)
        put(os.path.join(d, "cmdline"), "/app/bin/obs\0")
        res = self.scan()
        self.assertEqual(detect.active_obs_root(res), os.path.realpath(flat))
        self.assertEqual([p for _, p in detect.obs_roots(res)], [os.path.realpath(flat), os.path.realpath(native)])
        self.assertEqual(sum(bool(r.get("active")) for r in res["obs"]["roots"]), 1)

    def test_not_running_newest_settings_win(self):
        native, flat = self.roots()
        os.utime(os.path.join(native, "user.ini"), (1e9, 1e9))
        for rel in ("user.ini", "basic/scenes", "basic/profiles"):
            p = os.path.join(native, rel)
            if os.path.exists(p):
                os.utime(p, (1e9, 1e9))
        os.utime(os.path.join(flat, "user.ini"), (2e9, 2e9))
        self.assertEqual(detect.active_obs_root(self.scan()), os.path.realpath(flat))

    def test_default_root_exists_or_not(self):
        self.assertEqual(detect.default_root("obs"), os.path.realpath(os.path.join(self.home, ".config/obs-studio")))
        os.makedirs(os.path.join(self.home, ".var/app/com.obsproject.Studio"))
        self.assertTrue(detect.default_root("obs").endswith(".var/app/com.obsproject.Studio/config/obs-studio"))
        self.assertEqual(detect.default_root("streamdeck_ui"), os.path.realpath(self.home))
        self.assertTrue(detect.default_root("streamcontroller").endswith("com.core447.StreamController/data"))
        with self.assertRaises(KeyError):
            detect.default_root("nope")

    def test_big_downloads_still_finds_top_level_exports(self):
        dl = os.path.join(self.home, "Downloads")
        os.makedirs(dl)
        for i in range(detect.MAX_ENTRIES + 10):
            open(os.path.join(dl, f"f{i:05d}.txt"), "w").close()
        put(os.path.join(dl, "zz.streamDeckProfile"), "zip")
        put(os.path.join(self.home, "Desktop/desk.streamDeckProfile"), "zip")
        self.assertEqual(sorted(a["label"] for a in self.apps("elgato")),
                         ["desk.streamDeckProfile", "zz.streamDeckProfile"])
