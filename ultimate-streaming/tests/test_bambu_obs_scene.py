import base64
import unittest

from tests import helpers  # noqa: F401
from plugins.bambu import obs_scene

URL = "udp://127.0.0.1:9000"


class OBSSDKRequestError(Exception):
    pass


class FakeOBS:
    def __init__(self, scenes=(), inputs=None, items=None):
        self.calls = []
        self.scenes = list(scenes)
        self.inputs = dict(inputs or {})   # name -> (kind, settings)
        self.items = {s: list((items or {}).get(s, [])) for s in self.scenes}
        self.transforms = {}
        self.current = None
        self.shot = "data:image/jpg;base64," + base64.b64encode(b"JPEG").decode()
        self.filters = {}                  # source -> [filter dicts]
        self.media_state = "OBS_MEDIA_STATE_ENDED"

    def _filter(self, data):
        return next(f for f in self.filters[data["sourceName"]] if f["filterName"] == data["filterName"])

    def names(self):
        return [c[0] for c in self.calls]

    def request(self, name, data=None):
        data = data or {}
        self.calls.append((name, data))
        if name == "GetVideoSettings":
            return {"baseWidth": 1920, "baseHeight": 1080}
        if name == "GetSceneList":
            return {"scenes": [{"sceneName": s} for s in self.scenes]}
        if name == "GetInputList":
            return {"inputs": [{"inputName": n, "inputKind": k} for n, (k, _) in self.inputs.items()]}
        if name == "GetSceneItemList":
            return {"sceneItems": [{"sourceName": n, "sceneItemId": i + 1}
                                   for i, n in enumerate(self.items[data["sceneName"]])]}
        if name == "GetInputSettings":
            k, s = self.inputs[data["inputName"]]
            return {"inputKind": k, "inputSettings": dict(s)}
        if name == "SetInputSettings":
            self.inputs[data["inputName"]][1].update(data["inputSettings"])
            return {}
        if name == "CreateScene":
            self.scenes.append(data["sceneName"])
            self.items[data["sceneName"]] = []
            return {}
        if name == "CreateInput":
            self.inputs[data["inputName"]] = (data["inputKind"], dict(data["inputSettings"]))
            self.items[data["sceneName"]].append(data["inputName"])
            return {"sceneItemId": len(self.items[data["sceneName"]])}
        if name == "CreateSceneItem":
            self.items[data["sceneName"]].append(data["sourceName"])
            return {"sceneItemId": len(self.items[data["sceneName"]])}
        if name == "SetSceneItemTransform":
            self.transforms[data["sceneItemId"]] = data["sceneItemTransform"]
            return {}
        if name == "SetCurrentProgramScene":
            self.current = data["sceneName"]
            return {}
        if name == "GetSourceScreenshot":
            return {"imageData": self.shot}
        if name == "GetSourceFilterList":
            return {"filters": [dict(f) for f in self.filters.get(data["sourceName"], [])]}
        if name == "CreateSourceFilter":
            self.filters.setdefault(data["sourceName"], []).append(
                {"filterName": data["filterName"], "filterKind": data["filterKind"],
                 "filterSettings": dict(data["filterSettings"]), "filterEnabled": True})
            return {}
        if name == "SetSourceFilterSettings":
            f = self._filter(data)
            f["filterSettings"].update(data["filterSettings"])
            return {}
        if name == "SetSourceFilterEnabled":
            self._filter(data)["filterEnabled"] = data["filterEnabled"]
            return {}
        if name == "TriggerMediaInputAction":
            self.media_state = "OBS_MEDIA_STATE_OPENING"
            return {}
        if name == "GetMediaInputStatus":
            return {"mediaState": self.media_state}
        if name == "RemoveSourceFilter":
            self.filters[data["sourceName"]].remove(self._filter(data))
            return {}
        raise OBSSDKRequestError(name)


def ensure(obs, **kw):
    return obs_scene.ensure_camera(obs, scene="Printer Cam", source="Printer Camera", url=URL,
                                   text_source="Printer Status", text_file="/tmp/status.txt", **kw)


class EnsureTests(unittest.TestCase):
    def test_fresh_obs(self):
        obs = FakeOBS()
        r = ensure(obs)
        self.assertEqual(r, {"created": ["Printer Cam", "Printer Camera", "Printer Status"], "updated": []})
        kind, s = obs.inputs["Printer Camera"]
        self.assertEqual(kind, "ffmpeg_source")
        self.assertEqual(s, {"is_local_file": False, "input": URL, "input_format": "mpegts",
                             "restart_on_activate": True, "close_when_inactive": False, "buffering_mb": 1,
                             "reconnect_delay_sec": 2, "hw_decode": False})
        kind, s = obs.inputs["Printer Status"]
        self.assertEqual(kind, "text_ft2_source_v2")
        self.assertEqual(s["text_file"], "/tmp/status.txt")
        self.assertTrue(s["from_file"])
        fit = obs.transforms[1]
        self.assertEqual(fit["boundsType"], "OBS_BOUNDS_SCALE_INNER")
        self.assertEqual((fit["boundsWidth"], fit["boundsHeight"]), (1920, 1080))
        self.assertEqual((fit["positionX"], fit["positionY"]), (0, 0))
        self.assertEqual((obs.transforms[2]["positionX"], obs.transforms[2]["positionY"]), (24, 1056))

    def test_second_run_is_noop(self):
        obs = FakeOBS()
        ensure(obs)
        self.assertEqual(ensure(obs), {"created": [], "updated": []})
        self.assertEqual(obs.items["Printer Cam"], ["Printer Camera", "Printer Status"])

    def test_edited_url_restored(self):
        obs = FakeOBS()
        ensure(obs)
        obs.inputs["Printer Camera"][1]["input"] = "http://evil.example/x"
        self.assertEqual(ensure(obs), {"created": [], "updated": ["Printer Camera"]})
        self.assertEqual(obs.inputs["Printer Camera"][1]["input"], URL)

    def test_wrong_kind(self):
        obs = FakeOBS(inputs={"Printer Camera": ("browser_source", {})})
        with self.assertRaises(ValueError):
            ensure(obs)
        for n in ("CreateScene", "CreateInput", "CreateSceneItem", "SetInputSettings"):
            self.assertNotIn(n, obs.names())

    def test_input_elsewhere_only_adds_item(self):
        obs = FakeOBS(scenes=["Other", "Printer Cam"],
                      inputs={"Printer Camera": ("ffmpeg_source", dict(obs_scene._camera_settings(URL)))},
                      items={"Other": ["Printer Camera"]})
        r = obs_scene.ensure_camera(obs, scene="Printer Cam", source="Printer Camera", url=URL)
        self.assertEqual(r, {"created": [], "updated": []})
        self.assertNotIn("CreateInput", obs.names())
        self.assertNotIn("CreateScene", obs.names())
        self.assertEqual(obs.items["Printer Cam"], ["Printer Camera"])


class GrabTests(unittest.TestCase):
    def cam(self):
        return FakeOBS(inputs={"Cam": ("ffmpeg_source", {})})

    def test_decodes(self):
        obs = self.cam()
        self.assertEqual(obs_scene.grab_frame(obs, "Cam"), b"JPEG")
        name, data = obs.calls[-1]
        self.assertEqual(name, "GetSourceScreenshot")
        self.assertEqual(data["sourceName"], "Cam")
        self.assertEqual(data["imageWidth"], 1280)

    def test_accepts_jpeg_prefix(self):
        obs = self.cam()
        obs.shot = "data:image/jpeg;base64," + base64.b64encode(b"X").decode()
        self.assertEqual(obs_scene.grab_frame(obs, "Cam"), b"X")

    def test_refuses_other_kinds(self):
        obs = FakeOBS(inputs={"Mic": ("pulse_input_capture", {})}, scenes=["Live"])
        for name in ("Mic", "Live", "Missing"):
            with self.assertRaises(ValueError):
                obs_scene.grab_frame(obs, name)
        self.assertNotIn("GetSourceScreenshot", obs.names())

    def test_empty_raises(self):
        obs = self.cam()
        obs.shot = "data:image/jpg;base64,"
        with self.assertRaises(RuntimeError):
            obs_scene.grab_frame(obs, "Cam")

    def test_never_saves_or_shoots_scene(self):
        obs = self.cam()
        obs_scene.grab_frame(obs, "Cam")
        self.assertNotIn("SaveSourceScreenshot", obs.names())
        shots = [d for n, d in obs.calls if n == "GetSourceScreenshot"]
        self.assertEqual([d["sourceName"] for d in shots], ["Cam"])


class SceneTests(unittest.TestCase):
    def test_switch_and_exists(self):
        obs = FakeOBS(scenes=["A"])
        obs_scene.switch_scene(obs, "A")
        self.assertEqual(obs.current, "A")
        self.assertTrue(obs_scene.scene_exists(obs, "A"))
        self.assertFalse(obs_scene.scene_exists(obs, "B"))


if __name__ == "__main__":
    unittest.main()


class CropTests(unittest.TestCase):
    def crop(self, obs):
        return [f for f in obs.filters.get("Printer Camera", []) if f["filterName"] == obs_scene.CROP_FILTER]

    def test_no_crop_by_default(self):
        obs = FakeOBS()
        ensure(obs)
        self.assertEqual(self.crop(obs), [])
        self.assertNotIn("CreateSourceFilter", obs.names())

    def test_crop_created_on_the_source_then_updated_and_removed(self):
        obs = FakeOBS()
        ensure(obs, crop_right=384)
        [f] = self.crop(obs)
        self.assertEqual(f["filterKind"], "crop_filter")
        self.assertEqual(f["filterSettings"], {"relative": True, "left": 0, "right": 384, "top": 0, "bottom": 0})
        self.assertEqual(ensure(obs, crop_right=384)["updated"], [])           # nothing to change
        f["filterEnabled"] = False                                               # turned off by hand
        self.assertEqual(ensure(obs, crop_right=400)["updated"], ["Printer Camera"])
        [f] = self.crop(obs)
        self.assertEqual((f["filterSettings"]["right"], f["filterEnabled"]), (400, True))
        self.assertTrue(obs_scene.apply_crop(obs, "Printer Camera", 10, 0))
        self.assertEqual(self.crop(obs)[0]["filterSettings"]["left"], 10)
        self.assertTrue(obs_scene.apply_crop(obs, "Printer Camera", 0, 0))
        self.assertEqual(self.crop(obs), [])
        self.assertFalse(obs_scene.apply_crop(obs, "Printer Camera", 0, 0))

    def test_apply_url_repoints_an_existing_source(self):
        obs = FakeOBS()
        ensure(obs)
        new = "udp://127.0.0.1:9001"
        self.assertTrue(obs_scene.apply_url(obs, "Printer Camera", new))
        self.assertEqual(obs.inputs["Printer Camera"][1]["input"], new)
        self.assertFalse(obs_scene.apply_url(obs, "Printer Camera", new))   # already there

    def test_apply_url_skips_a_missing_or_other_source(self):
        obs = FakeOBS()
        self.assertFalse(obs_scene.apply_url(obs, "Printer Camera", URL))
        self.assertNotIn("SetInputSettings", obs.names())
        obs.inputs["Printer Camera"] = (obs_scene.TEXT_KIND, {})
        self.assertFalse(obs_scene.apply_url(obs, "Printer Camera", URL))
        self.assertNotIn("SetInputSettings", obs.names())

    def test_restart_media(self):
        obs = FakeOBS()
        ensure(obs)
        self.assertTrue(obs_scene.restart_media(obs, "Printer Camera"))
        self.assertEqual(obs.calls[-1], ("TriggerMediaInputAction", {
            "inputName": "Printer Camera", "mediaAction": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART"}))

    def test_restart_media_skips_a_missing_or_other_source(self):
        obs = FakeOBS()
        self.assertFalse(obs_scene.restart_media(obs, "Printer Camera"))
        obs.inputs["Printer Camera"] = (obs_scene.TEXT_KIND, {})
        self.assertFalse(obs_scene.restart_media(obs, "Printer Camera"))
        self.assertNotIn("TriggerMediaInputAction", obs.names())

    def test_media_state(self):
        obs = FakeOBS()
        self.assertIsNone(obs_scene.media_state(obs, "Printer Camera"))
        self.assertNotIn("GetMediaInputStatus", obs.names())
        ensure(obs)
        self.assertEqual(obs_scene.media_state(obs, "Printer Camera"), "OBS_MEDIA_STATE_ENDED")
        self.assertEqual(obs.calls[-1], ("GetMediaInputStatus", {"inputName": "Printer Camera"}))
        self.assertEqual(obs_scene.DEAD_STATES, {"OBS_MEDIA_STATE_ENDED", "OBS_MEDIA_STATE_ERROR",
                                                 "OBS_MEDIA_STATE_NONE"})

    def test_apply_crop_skips_a_missing_source(self):
        obs = FakeOBS()
        self.assertFalse(obs_scene.apply_crop(obs, "Printer Camera", 0, 384))
        self.assertNotIn("CreateSourceFilter", obs.names())

    def test_name_clash_with_another_filter_kind(self):
        obs = FakeOBS()
        ensure(obs)
        obs.filters["Printer Camera"] = [{"filterName": obs_scene.CROP_FILTER, "filterKind": "color_filter",
                                          "filterSettings": {}, "filterEnabled": True}]
        with self.assertRaises(ValueError):
            ensure(obs, crop_right=384)
