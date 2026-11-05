"""The OBS builder's audio step and the friendly mute error: a stream must never be silent
because OBS's global audio devices happen to be switched off."""
import os
import sys
import unittest

from tests.helpers import ROOT

sys.path.insert(0, os.path.join(ROOT, "obs"))
import build_scenes  # noqa: E402

SCENES = ["Starting Soon", "Screen", "BRB"]


class FakeOBS:
    def __init__(self, special=None, inputs=None, kinds=("pulse_input_capture", "pulse_output_capture")):
        self.special = special or {"desktop1": None, "desktop2": None, "mic1": None, "mic2": None}
        self.inputs = dict(inputs or {})          # name -> kind
        self.items = {s: [] for s in SCENES}      # scene -> [source names]
        self.kinds = list(kinds)
        self.created = []

    def __call__(self, request, data=None):
        d = data or {}
        if request == "GetSpecialInputs":
            return dict(self.special)
        if request == "GetInputKindList":
            return {"inputKinds": self.kinds}
        if request == "GetInputList":
            return {"inputs": [{"inputName": n, "inputKind": k} for n, k in self.inputs.items()]}
        if request == "CreateInput":
            self.inputs[d["inputName"]] = d["inputKind"]
            self.items[d["sceneName"]].append(d["inputName"])
            self.created.append((d["inputName"], d["inputKind"], d["inputSettings"]))
            return {"sceneItemId": 1}
        if request == "GetSceneItemList":
            return {"sceneItems": [{"sourceName": n} for n in self.items[d["sceneName"]]]}
        if request == "SetInputVolume":
            self.volumes = {**getattr(self, "volumes", {}), d["inputName"]: d["inputVolumeDb"]}
            return {}
        if request == "CreateSceneItem":
            self.items[d["sceneName"]].append(d["sourceName"])
            return {"sceneItemId": 2}
        raise AssertionError(f"unexpected request {request}")


class EnsureAudio(unittest.TestCase):
    def test_adds_mic_and_desktop_to_every_scene_when_obs_has_none(self):
        obs = FakeOBS()
        build_scenes.ensure_audio(obs, SCENES)
        self.assertEqual({c[0]: c[1] for c in obs.created},
                         {"Mic/Aux": "pulse_input_capture", "Desktop Audio": "pulse_output_capture"})
        self.assertTrue(all(c[2] == {"device_id": "default"} for c in obs.created))
        for scene in SCENES:
            self.assertEqual(obs.items[scene].count("Mic/Aux"), 1, scene)
            self.assertEqual(obs.items[scene].count("Desktop Audio"), 1, scene)
        self.assertEqual(build_scenes.audio_inputs(obs), {"Mic/Aux", "Desktop Audio"})

    def test_running_again_changes_nothing(self):
        obs = FakeOBS()
        build_scenes.ensure_audio(obs, SCENES)
        before = {s: list(v) for s, v in obs.items.items()}
        build_scenes.ensure_audio(obs, SCENES)
        self.assertEqual(obs.items, before)
        self.assertEqual(len(obs.created), 2)

    def test_uses_obs_global_devices_when_they_are_on(self):
        obs = FakeOBS(special={"desktop1": "Desktop Audio", "mic1": "Mic/Aux"},
                      inputs={"Desktop Audio": "pulse_output_capture", "Mic/Aux": "pulse_input_capture"})
        build_scenes.ensure_audio(obs, SCENES)
        self.assertEqual(obs.created, [])
        self.assertEqual(obs.items, {s: [] for s in SCENES})

    def test_falls_back_to_alsa_mic_and_says_when_it_cannot(self):
        obs = FakeOBS(kinds=("alsa_input_capture",))
        build_scenes.ensure_audio(obs, SCENES)
        self.assertEqual([c[:2] for c in obs.created], [("Mic/Aux", "alsa_input_capture")])
        self.assertNotIn("Desktop Audio", obs.inputs)


class EnsureMusic(unittest.TestCase):
    def test_radio_source_in_every_scene_quiet_and_continuous(self):
        obs = FakeOBS(kinds=("ffmpeg_source",))
        build_scenes.ensure_music(obs, SCENES, "BRB Music", -18)
        name, kind, settings = obs.created[0]
        self.assertEqual((name, kind), ("BRB Music", "ffmpeg_source"))
        self.assertFalse(settings["restart_on_activate"], "music must carry on across scene changes")
        self.assertFalse(settings["close_when_inactive"])
        self.assertEqual(obs.volumes["BRB Music"], -18)
        for scene in SCENES:
            self.assertEqual(obs.items[scene].count("BRB Music"), 1, scene)
        build_scenes.ensure_music(obs, SCENES, "BRB Music", -18)
        self.assertEqual(len(obs.created), 1, "running again adds nothing")


class FriendlyMuteError(unittest.TestCase):
    def test_missing_source_explains_the_fix(self):
        from tests.helpers import Deck, tmpdir  # noqa: F401  (sandbox env)
        sys.path.insert(0, ROOT)
        from modules.obs import OBS

        class _RequestError(Exception):
            code = 600
        _RequestError.__name__ = "OBSSDKRequestError"

        def send(request, data=None):
            raise _RequestError("No source was found by the name of `Mic/Aux` within the canvas `Main`.")

        obs = OBS.__new__(OBS)
        obs.send = send
        with self.assertRaises(RuntimeError) as cm:
            obs.toggle_mute("Mic/Aux")
        self.assertIn("no audio source named 'Mic/Aux'", str(cm.exception))
        self.assertIn("Add microphone & desktop audio", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
