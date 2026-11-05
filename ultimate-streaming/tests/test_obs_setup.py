"""core/obs_setup.py and build_scenes adopt mode, against a fake obs-websocket `send`."""
import importlib.util
import os
import unittest
from unittest import mock

from tests.helpers import ROOT

from core import obs_setup  # noqa: E402

spec = importlib.util.spec_from_file_location("build_scenes", os.path.join(ROOT, "obs", "build_scenes.py"))
build_scenes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_scenes)

MUTATING = {"CreateProfile", "CreateSceneCollection", "SetCurrentProfile", "SetCurrentSceneCollection",
            "CreateInput", "CreateSceneItem", "SetSceneItemIndex", "SetSceneItemTransform", "SetInputVolume",
            "CreateScene", "RemoveScene", "RemoveSceneItem", "SetStreamServiceSettings"}


class FakeOBS:
    def __init__(self, key="live_SECRET"):
        self.profiles, self.profile = ["Mine", "Other"], "Mine"
        self.collections, self.collection = ["Main", "Alt"], "Main"
        self.scenes = ["Scene B", "Scene A"]       # OBS lists bottom-first
        self.inputs = {"Mic": "pulse_input_capture"}
        self.items = {"Scene A": ["Mic"], "Scene B": []}
        self.key, self.calls, self.log = key, [], None

    def __call__(self, req, data=None):
        self.calls.append((req, data))
        if self.log is not None and req in MUTATING:
            self.log.append(req)
        if req == "GetProfileList":
            return {"profiles": list(self.profiles), "currentProfileName": self.profile}
        if req == "GetSceneCollectionList":
            return {"sceneCollections": list(self.collections), "currentSceneCollectionName": self.collection}
        if req == "GetSceneList":
            return {"scenes": [{"sceneName": n} for n in self.scenes]}
        if req == "GetStreamServiceSettings":
            return {"streamServiceType": "rtmp_common", "streamServiceSettings": {"key": self.key}}
        if req == "GetInputList":
            return {"inputs": [{"inputName": n, "inputKind": k} for n, k in self.inputs.items()]}
        if req == "GetInputKindList":
            return {"inputKinds": ["text_ft2_source_v2", "color_source_v3", "ffmpeg_source"]}
        if req == "GetVideoSettings":
            return {"baseWidth": 1280, "baseHeight": 720}
        if req == "GetSceneItemList":
            return {"sceneItems": [{"sourceName": n, "sceneItemId": i} for i, n in enumerate(self.items[data["sceneName"]])]}
        if req == "SetCurrentProfile":
            self.profile = data["profileName"]
        elif req == "SetCurrentSceneCollection":
            self.collection = data["sceneCollectionName"]
        elif req == "CreateInput":
            self.inputs[data["inputName"]] = data["inputKind"]
            self.items[data["sceneName"]].append(data["inputName"])
            return {"sceneItemId": 99}
        elif req == "CreateSceneItem":
            self.items[data["sceneName"]].append(data["sourceName"])
            return {"sceneItemId": 98}
        return {}

    def names(self):
        return [c[0] for c in self.calls]


class Base(unittest.TestCase):
    def setUp(self):
        self.order = []
        self.obs = FakeOBS()
        self.obs.log = self.order

        def fake_backup(items, *, label, auto=False, **kw):
            self.order.append(("backup", label, auto))
            return {"id": "arch-1"}
        patches = [mock.patch.object(obs_setup.setup_backup, "create", side_effect=fake_backup),
                   mock.patch.object(obs_setup.detect, "scan", return_value={"obs": {"roots": [
                       {"source_id": "x", "kind": "native", "label": "Native OBS"}]}}),
                   mock.patch.object(obs_setup.detect, "resolve", return_value=("obs", "/tmp/obs-root"))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def mutations(self):
        return [o for o in self.order if isinstance(o, str)]


class BackupObs(Base):
    def test_item_shape_and_label(self):
        self.assertEqual(obs_setup.backup_obs("x"), "arch-1")
        items = obs_setup.setup_backup.create.call_args.args[0]
        self.assertEqual(items, [{"kind": "obs", "root": "/tmp/obs-root",
                                  "include": obs_setup.setup_backup.OBS_INCLUDE, "label": "Native OBS"}])
        self.assertEqual(obs_setup.setup_backup.create.call_args.kwargs, {"label": "before-x", "auto": True})

    def test_backs_up_every_root_active_first(self):
        obs_setup.detect.scan.return_value = {"obs": {"roots": [
            {"source_id": "n", "kind": "native", "label": "Native OBS"},
            {"source_id": "f", "kind": "flatpak", "label": "Flatpak OBS", "active": True}]}}
        paths = {"n": "/tmp/native", "f": "/tmp/flatpak"}
        with mock.patch.object(obs_setup.detect, "resolve", side_effect=lambda sid: ("obs", paths[sid])):
            obs_setup.backup_obs("x")
        roots = [c.args[0][0]["root"] for c in obs_setup.setup_backup.create.call_args_list]
        self.assertEqual(roots, ["/tmp/flatpak", "/tmp/native"])

    def test_no_root(self):
        obs_setup.detect.scan.return_value = {"obs": {"roots": []}}
        self.assertIsNone(obs_setup.backup_obs("x"))
        obs_setup.setup_backup.create.assert_not_called()


class Current(Base):
    def test_never_returns_key(self):
        info = obs_setup.current(self.obs)
        self.assertEqual(info, {"profile": "Mine", "collection": "Main", "scenes": ["Scene A", "Scene B"],
                                "has_stream_key": True})
        self.assertNotIn("live_SECRET", repr(info))
        self.assertFalse(obs_setup.current(FakeOBS(key=""))["has_stream_key"])


class Adopt(Base):
    def test_never_creates(self):
        obs_setup.adopt(self.obs, profile="Other", collection="Alt")
        self.assertFalse({"CreateProfile", "CreateSceneCollection"} & set(self.obs.names()))

    def test_switches_only_when_needed_and_backs_up_first(self):
        r = obs_setup.adopt(self.obs, profile="Other", collection="Alt")
        self.assertEqual(r["switched"], ["profile", "collection"])
        self.assertEqual(self.order[0][0], "backup")
        self.assertEqual(self.order[0][1:], ("before-adopt", True))
        self.assertEqual(self.obs.profile, "Other")
        self.assertEqual(self.obs.collection, "Alt")

    def test_no_switch_when_already_current(self):
        r = obs_setup.adopt(self.obs, profile="Mine", collection="Main")
        self.assertEqual(r["switched"], [])
        self.assertEqual(self.mutations(), [])
        self.assertEqual(obs_setup.adopt(self.obs)["profile"], "Mine")

    def test_unknown_target_changes_nothing(self):
        r = obs_setup.adopt(self.obs, profile="Nope")
        self.assertFalse(r["ok"])
        self.assertEqual(self.order, [])
        self.assertFalse(obs_setup.adopt(self.obs, collection="Nope")["ok"])
        self.assertEqual(self.order, [])


class AddAddon(Base):
    def test_adds_to_named_scene_only(self):
        r = obs_setup.add_addon(self.obs, "Scene B", "title_text", {})
        self.assertEqual(r, {"ok": True, "source": "Stream Title", "created": True})
        self.assertEqual(self.obs.items["Scene B"], ["Stream Title"])
        self.assertEqual(self.obs.items["Scene A"], ["Mic"])
        created = [d for n, d in self.obs.calls if n == "CreateInput"]
        self.assertEqual([d["sceneName"] for d in created], ["Scene B"])
        self.assertEqual(created[0]["inputKind"], "text_ft2_source_v2")

    def test_idempotent(self):
        obs_setup.add_addon(self.obs, "Scene B", "category_text", {})
        n = len(self.obs.calls)
        again = obs_setup.add_addon(self.obs, "Scene B", "category_text", {})
        self.assertEqual(again, {"ok": True, "source": "Stream Category", "created": False})
        self.assertNotIn("CreateInput", self.obs.names()[n:])
        self.assertEqual(self.obs.items["Scene B"], ["Stream Category"])

    def test_reuses_existing_input_in_another_scene(self):
        obs_setup.add_addon(self.obs, "Scene B", "countdown", {})
        r = obs_setup.add_addon(self.obs, "Scene A", "countdown", {})
        self.assertEqual(r, {"ok": True, "source": "Countdown", "created": False})
        self.assertEqual(self.obs.names().count("CreateInput"), 1)
        self.assertEqual(self.obs.items["Scene A"], ["Mic", "Countdown"])

    def test_name_collision_of_other_kind_refused(self):
        self.obs.inputs["Stream Title"] = "image_source"
        r = obs_setup.add_addon(self.obs, "Scene B", "title_text", {})
        self.assertFalse(r["ok"])
        self.assertEqual(self.order, [])
        self.assertEqual(self.obs.inputs["Stream Title"], "image_source")

    def test_collision_with_scene_name_refused(self):
        self.obs.scenes.append("Countdown")
        self.obs.items["Countdown"] = []
        self.assertFalse(obs_setup.add_addon(self.obs, "Scene B", "countdown", {})["ok"])

    def test_bad_scene_and_addon(self):
        self.assertFalse(obs_setup.add_addon(self.obs, "Missing", "title_text", {})["ok"])
        self.assertFalse(obs_setup.add_addon(self.obs, "Scene B", "bogus", {})["ok"])
        self.assertEqual(self.order, [])

    def test_reward_background_is_a_color_source_at_the_bottom(self):
        r = obs_setup.add_addon(self.obs, "Scene A", "reward_background", {})
        self.assertEqual(r["source"], "Reward Background")
        (create,) = [d for n, d in self.obs.calls if n == "CreateInput"]
        self.assertEqual(create["inputKind"], "color_source_v3")
        self.assertEqual(create["inputName"], "Reward Background")
        self.assertEqual((create["inputSettings"]["width"], create["inputSettings"]["height"]), (1280, 720))
        (idx,) = [d for n, d in self.obs.calls if n == "SetSceneItemIndex"]
        self.assertEqual((idx["sceneName"], idx["sceneItemIndex"]), ("Scene A", 0))

    def test_reads_full_bot_config(self):
        cfg = {"obs_setup": {"font": "Comic Neue"}, "radio": {"source": "Tunes", "volume_db": -7}}
        obs_setup.add_addon(self.obs, "Scene B", "title_text", cfg)
        (create,) = [d for n, d in self.obs.calls if n == "CreateInput"]
        self.assertEqual(create["inputSettings"]["font"]["face"], "Comic Neue")
        obs_setup.add_addon(self.obs, "Scene B", "radio_music", cfg)
        self.assertIn("Tunes", self.obs.inputs)
        (vol,) = [d for n, d in self.obs.calls if n == "SetInputVolume"]
        self.assertEqual(vol["inputVolumeDb"], -7.0)

    def test_scene_vanishing_gives_clean_error(self):
        class Boom(RuntimeError):
            pass
        real = self.obs.__call__

        def flaky(req, data=None):
            if req == "CreateInput":
                raise Boom("secret detail")
            return real(req, data)
        r = obs_setup.add_addon(flaky, "Scene B", "title_text", {})
        self.assertFalse(r["ok"])
        self.assertNotIn("secret detail", r["error"])
        flaky_item = lambda req, data=None: (_ for _ in ()).throw(Boom()) if req == "CreateSceneItem" else real(req, data)
        self.obs.inputs["Stream Title"] = "text_ft2_source_v2"
        self.assertFalse(obs_setup.add_addon(flaky_item, "Scene B", "title_text", {})["ok"])

    def test_radio_music(self):
        r = obs_setup.add_addon(self.obs, "Scene B", "radio_music", {})
        self.assertEqual(r["source"], "BRB Music")
        self.assertEqual(self.obs.inputs["BRB Music"], "ffmpeg_source")

    def test_backup_before_every_mutating_call(self):
        for addon in ("title_text", "radio_music", "reward_background"):
            self.order.clear()
            obs_setup.add_addon(self.obs, "Scene B" if addon != "title_text" else "Scene A", addon, {})
            self.assertEqual(self.order[0][0], "backup", addon)
            self.assertTrue(self.mutations(), addon)
        self.order.clear()
        obs_setup.adopt(self.obs, profile="Other")
        self.assertEqual(self.order[0][0], "backup")


class BuildScenesAdopt(Base):
    def test_adopt_makes_no_profile_or_collection_calls(self):
        s = build_scenes.load_settings("/nonexistent/config.yaml")
        s["mode"] = "adopt"
        build_scenes.build(self.obs, s)
        self.assertEqual(self.mutations(), [])
        self.assertFalse({"CreateProfile", "CreateSceneCollection", "SetCurrentProfile",
                          "SetCurrentSceneCollection", "SetStreamServiceSettings"} & set(self.obs.names()))

    def test_scene_refused_in_adopt_mode_on_users_collection(self):
        s = {**build_scenes.DEFAULTS, "mode": "adopt", "brb_videos": ""}
        with self.assertRaises(SystemExit) as cm:
            build_scenes.rebuild_scene(self.obs, s, "Screen")
        self.assertIn("add-ons", str(cm.exception))
        self.assertFalse(MUTATING & set(self.obs.names()))

    def test_scene_missing_is_a_clean_exit(self):
        s = {**build_scenes.DEFAULTS, "mode": "adopt", "collection": "Main", "brb_videos": ""}
        with self.assertRaises(SystemExit) as cm:
            build_scenes.rebuild_scene(self.obs, s, "Screen")
        self.assertIn("no scene named", str(cm.exception))
        self.assertFalse(MUTATING & set(self.obs.names()))

    def test_mode_default(self):
        self.assertEqual(build_scenes.resolve_mode(self.obs, {"mode": None}), "adopt")
        self.obs.profiles = ["Untitled"]
        self.assertEqual(build_scenes.resolve_mode(self.obs, {"mode": None}), "starter")
        self.assertEqual(build_scenes.resolve_mode(self.obs, {"mode": "adopt"}), "adopt")


if __name__ == "__main__":
    unittest.main()
