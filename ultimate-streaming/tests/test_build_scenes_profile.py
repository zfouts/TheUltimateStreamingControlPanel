"""obs/build_scenes.py ensure_profile: a newly created profile keeps the stream service (key)."""
import importlib.util
import os
import unittest

from tests.helpers import ROOT, tmpdir

spec = importlib.util.spec_from_file_location("build_scenes", os.path.join(ROOT, "obs", "build_scenes.py"))
build_scenes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_scenes)


class FakeOBS:
    def __init__(self, key="live_123"):
        self.profiles, self.current, self.calls = ["Twitch"], "Twitch", []
        self.service = {"Twitch": {"streamServiceType": "rtmp_common",
                                   "streamServiceSettings": {"service": "Twitch", "server": "auto", "key": key}}}

    def __call__(self, req, data=None):
        self.calls.append((req, data))
        if req == "GetProfileList":
            return {"profiles": list(self.profiles), "currentProfileName": self.current}
        if req == "CreateProfile":
            self.profiles.append(data["profileName"])
            self.current = data["profileName"]
            self.service[self.current] = {"streamServiceType": "rtmp_custom", "streamServiceSettings": {}}
        elif req == "SetCurrentProfile":
            self.current = data["profileName"]
        elif req == "GetStreamServiceSettings":
            return dict(self.service[self.current])
        elif req == "SetStreamServiceSettings":
            self.service[self.current] = dict(data)
        elif req == "GetVersion":
            return {"obsVersion": "31.0.0"}
        return {}


def settings(folder):
    return {"profile": "Stream Deck", "canvas": [1920, 1080], "fps": 60, "recordings_dir": folder,
            "replay_buffer_seconds": 30, "encoder": "nvenc", "bitrate": 6000, "audio_bitrate": 160}


class EnsureProfile(unittest.TestCase):
    def test_new_profile_inherits_stream_service(self):
        obs = FakeOBS()
        build_scenes.ensure_profile(obs, settings(tmpdir()))
        self.assertEqual(obs.current, "Stream Deck")
        self.assertEqual(obs.service["Stream Deck"]["streamServiceSettings"]["key"], "live_123")
        self.assertEqual(obs.service["Stream Deck"]["streamServiceType"], "rtmp_common")

    def test_no_key_to_copy_leaves_service_alone(self):
        obs = FakeOBS(key="")
        build_scenes.ensure_profile(obs, settings(tmpdir()))
        self.assertNotIn("SetStreamServiceSettings", [c[0] for c in obs.calls])

    def test_existing_profile_untouched(self):
        obs = FakeOBS()
        obs.profiles.append("Stream Deck")
        build_scenes.ensure_profile(obs, settings(tmpdir()))
        self.assertEqual(obs.current, "Stream Deck")
        self.assertNotIn("CreateProfile", [c[0] for c in obs.calls])
        self.assertNotIn("SetStreamServiceSettings", [c[0] for c in obs.calls])


if __name__ == "__main__":
    unittest.main()
