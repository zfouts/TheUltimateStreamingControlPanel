"""The OBS panel API against a fake obs-websocket: state, scenes, outputs, mixer, studio."""
import json
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from tests.helpers import EXAMPLE, ROOT, Deck, free_port, tmpdir

import yaml  # noqa: E402

from core import editor_auth  # noqa: E402
from core.bot import DeckBot  # noqa: E402

PORT = free_port()


class FakeObs:
    """In-memory obs-websocket: replaces OBS.send."""

    def __init__(self):
        self.offline = False
        self.refuse = set()                             # request names answered with a request error
        self.delay = 0
        self.calls = []
        self.scenes = ["Intro", "Game", "BRB"]          # dock order
        self.program, self.preview, self.studio = "Game", "Intro", False
        self.out = {"stream": False, "record": False, "replay": False, "virtualcam": False}
        self.paused = False
        self.bytes = 0
        self.inputs = {"Mic": {"kind": "pulse_input_capture", "muted": False, "db": -6.0},
                       "Desktop": {"kind": "pulse_output_capture", "muted": True, "db": 0.0},
                       "Logo": {"kind": "image_source", "muted": None, "db": None}}

    def send(self, request, data=None):
        self.calls.append((request, data))
        if self.offline:
            raise ConnectionError("down")
        if self.delay:
            time.sleep(self.delay)
        if request in self.refuse:
            raise _RequestError(request)
        d, ids = data or {}, {"Stream": "stream", "Record": "record", "ReplayBuffer": "replay", "VirtualCam": "virtualcam"}
        if request == "GetCurrentProgramScene":
            return {"sceneName": self.program}
        if request == "GetCurrentPreviewScene":
            return {"sceneName": self.preview}
        if request == "GetSceneList":
            return {"scenes": [{"sceneName": n, "sceneIndex": i} for i, n in reversed(list(enumerate(self.scenes)))]}
        if request == "SetCurrentProgramScene":
            self.program = d["sceneName"]
            return {}
        if request == "SetCurrentPreviewScene":
            self.preview = d["sceneName"]
            return {}
        if request == "GetStudioModeEnabled":
            return {"studioModeEnabled": self.studio}
        if request == "SetStudioModeEnabled":
            self.studio = d["studioModeEnabled"]
            return {}
        if request == "TriggerStudioModeTransition":
            self.program, self.preview = self.preview, self.program
            return {}
        if request == "GetStreamStatus":
            return {"outputActive": self.out["stream"], "outputDuration": 61500, "outputBytes": self.bytes,
                    "outputSkippedFrames": 2, "outputTotalFrames": 100}
        if request == "GetRecordStatus":
            return {"outputActive": self.out["record"], "outputPaused": self.paused, "outputDuration": 5000}
        if request == "GetReplayBufferStatus":
            return {"outputActive": self.out["replay"]}
        if request == "GetVirtualCamStatus":
            return {"outputActive": self.out["virtualcam"]}
        for prefix, val in (("Toggle", None), ("Start", True), ("Stop", False)):
            if request.startswith(prefix) and request[len(prefix):] in ids:
                k = ids[request[len(prefix):]]
                self.out[k] = (not self.out[k]) if val is None else val
                return {"outputActive": self.out[k]} if val is None else {}
        if request == "PauseRecord":
            self.paused = True
            return {}
        if request == "ResumeRecord":
            self.paused = False
            return {}
        if request == "GetInputList":
            return {"inputs": [{"inputName": n, "inputKind": i["kind"]} for n, i in self.inputs.items()]}
        if request in ("GetInputMute", "GetInputVolume", "SetInputMute", "SetInputVolume"):
            i = self.inputs[d["inputName"]]
            if i["muted"] is None:
                raise _RequestError("no audio")
            if request == "GetInputMute":
                return {"inputMuted": i["muted"]}
            if request == "GetInputVolume":
                return {"inputVolumeDb": i["db"], "inputVolumeMul": 10 ** (i["db"] / 20)}
            if request == "SetInputMute":
                i["muted"] = d["inputMuted"]
            else:
                i["db"] = d["inputVolumeDb"]
            return {}
        if request == "GetStats":
            return {"cpuUsage": 3.14159, "memoryUsage": 512.0, "activeFps": 60.0, "renderSkippedFrames": 1,
                    "renderTotalFrames": 1000, "outputSkippedFrames": 2, "outputTotalFrames": 100,
                    "availableDiskSpace": 123456.7}
        if request == "SaveSourceScreenshot":
            return {}
        if request == "GetVersion":
            return {"obsVersion": "30.0"}
        raise _RequestError(request)


class _RequestError(Exception):
    pass


_RequestError.__name__ = "OBSSDKRequestError"


def setUpModule():
    global BOT, BASE, TOKEN, OBS, FAKE
    config = tmpdir() + "/config.yaml"
    with open(EXAMPLE) as f:
        cfg = yaml.safe_load(f)
    cfg["editor"] = {"port": PORT}
    with open(config, "w") as f:
        yaml.safe_dump(cfg, f)
    BOT = DeckBot(root=ROOT, config_path=config)
    BOT.deck = Deck("Stream Deck XL")
    BOT.load_config(initial=True)
    BOT.load_extensions("modules")
    BASE, TOKEN = f"http://127.0.0.1:{PORT}", editor_auth.token()
    OBS = BOT.get_module("OBS")


def tearDownModule():
    for inst in list(BOT.modules.values()):
        try:
            inst.on_unload()
        except Exception:
            pass


def call(path, body=None):
    h = {"X-Deck-Token": TOKEN}
    data = None
    if body is not None:
        data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
    try:
        with urllib.request.urlopen(urllib.request.Request(BASE + path, data=data, headers=h), timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        with e:
            return e.code, json.loads(e.read())


class ObsApi(unittest.TestCase):
    def setUp(self):
        self.fake = FakeObs()
        patcher = mock.patch.object(OBS, "send", self.fake.send)
        patcher.start()
        self.addCleanup(patcher.stop)
        OBS._bitrate_last = None
        OBS.state = {"ok": False, "error": "x"}

    def subscribe(self):
        q = BOT.hub.subscribe()
        self.addCleanup(BOT.hub.unsubscribe, q)

    def test_state_shape(self):
        status, s = call("/api/obs/state")
        self.assertEqual(status, 200)
        self.assertTrue(s["ok"])
        self.assertEqual(s["scene"], "Game")
        self.assertIsNone(s["preview"])
        self.assertEqual(s["scenes"], [{"name": "Intro", "index": 0}, {"name": "Game", "index": 1},
                                       {"name": "BRB", "index": 2}])
        self.assertEqual(s["stream"]["total"], 100)
        self.assertEqual(s["stream"]["seconds"], 61)
        self.assertEqual(s["record"], {"active": False, "paused": False, "seconds": 5})
        self.assertFalse(s["replay"])
        self.assertEqual([i["name"] for i in s["inputs"]], ["Mic", "Desktop"])  # image source has no audio
        self.assertEqual(s["inputs"][1]["muted"], True)
        self.assertEqual(s["stats"]["cpu"], 3.1)
        self.assertEqual(s["stats"]["disk_mb"], 123457)

    def test_refused_optional_outputs(self):
        self.fake.refuse = {"GetReplayBufferStatus", "GetVirtualCamStatus"}
        status, s = call("/api/obs/state")
        self.assertEqual(status, 200)
        self.assertIsNone(s["replay"])
        self.assertIsNone(s["virtualcam"])
        self.assertEqual(s["scene"], "Game")
        self.assertEqual(s["stream"]["total"], 100)
        self.assertEqual([i["name"] for i in s["inputs"]], ["Mic", "Desktop"])
        self.subscribe()
        OBS.refresh_state()
        OBS.refresh_thread.join(5)
        self.assertTrue(OBS.snapshot()["ok"])

    def test_refused_preview_in_studio(self):
        self.fake.studio = True
        self.fake.refuse = {"GetCurrentPreviewScene"}
        status, s = call("/api/obs/state")
        self.assertEqual(status, 200)
        self.assertIsNone(s["preview"])
        self.assertTrue(s["studio"])

    def test_refresh_runs_off_the_main_loop(self):
        self.subscribe()
        orig, self.fake.delay = OBS.refresh_thread, 1
        t0 = time.time()
        OBS.refresh_state()
        self.assertLess(time.time() - t0, 0.05)
        first = OBS.refresh_thread
        self.assertIsNot(first, orig)
        self.assertTrue(first.is_alive())
        OBS.refresh_state()
        self.assertIs(OBS.refresh_thread, first)
        self.fake.delay = 0
        first.join(30)
        self.assertTrue(OBS.snapshot()["ok"])

    def test_scene_switch(self):
        self.assertEqual(call("/api/obs/scene", {"name": "BRB"}), (200, {"ok": True}))
        self.assertEqual(self.fake.program, "BRB")
        call("/api/obs/scene", {"name": "Intro", "preview": True})
        self.assertEqual((self.fake.program, self.fake.preview), ("BRB", "Intro"))
        self.assertEqual(call("/api/obs/scene", {"name": ""})[0], 400)
        self.assertEqual(call("/api/obs/scene", {"name": "x" * 201})[0], 400)
        self.assertEqual(call("/api/obs/scene", {"name": 5})[0], 400)

    def test_output_toggle(self):
        self.assertEqual(call("/api/obs/output", {"what": "stream", "action": "toggle"}), (200, {"ok": True, "active": True}))
        self.assertTrue(self.fake.out["stream"])
        self.assertEqual(call("/api/obs/output", {"what": "stream", "action": "toggle"})[1]["active"], False)
        self.assertEqual(call("/api/obs/output", {"what": "record", "action": "start"})[1]["active"], True)
        self.assertEqual(call("/api/obs/output", {"what": "record", "action": "pause"})[1]["active"], True)
        self.assertTrue(self.fake.paused)
        self.assertEqual(call("/api/obs/output", {"what": "replay", "action": "pause"})[0], 400)
        self.assertEqual(call("/api/obs/output", {"what": "nope", "action": "start"})[0], 400)
        self.assertEqual(call("/api/obs/output", {"what": "stream", "action": "explode"})[0], 400)

    def test_input_mute_and_volume(self):
        self.assertEqual(call("/api/obs/input", {"name": "Mic", "muted": True, "volume_db": -20})[0], 200)
        self.assertEqual(self.fake.inputs["Mic"]["muted"], True)
        self.assertEqual(self.fake.inputs["Mic"]["db"], -20)
        call("/api/obs/input", {"name": "Mic", "volume_db": 12})
        self.assertEqual(self.fake.inputs["Mic"]["db"], 0)
        call("/api/obs/input", {"name": "Mic", "volume_db": -500})
        self.assertEqual(self.fake.inputs["Mic"]["db"], -100)
        self.assertEqual(call("/api/obs/input", {"name": "Mic", "volume_db": "loud"})[0], 400)
        self.assertEqual(call("/api/obs/input", {"name": "Mic", "muted": "yes"})[0], 400)
        self.assertEqual(call("/api/obs/input", {"name": "Mic"})[0], 400)

    def test_studio_transition(self):
        self.assertEqual(call("/api/obs/studio", {"enabled": True}), (200, {"ok": True, "studio": True}))
        call("/api/obs/scene", {"name": "BRB", "preview": True})
        self.assertEqual(call("/api/obs/state")[1]["preview"], "BRB")
        self.assertEqual(call("/api/obs/studio", {"transition": True})[0], 200)
        self.assertEqual(self.fake.program, "BRB")
        self.assertEqual(call("/api/obs/studio", {"enabled": False})[1]["studio"], False)

    def test_screenshot(self):
        status, r = call("/api/obs/screenshot", {})
        self.assertEqual(status, 200)
        self.assertTrue(r["path"].endswith(".png"))
        self.assertIn("SaveSourceScreenshot", [c[0] for c in self.fake.calls])

    def test_request_validation(self):
        self.assertEqual(call("/api/obs/request", {"request": "Get Stuff"})[0], 400)
        self.assertEqual(call("/api/obs/request", {"request": "GetVersion2"})[0], 400)
        self.assertEqual(call("/api/obs/request", {"request": "GetVersion", "data": [1]})[0], 400)
        self.assertEqual(call("/api/obs/request", {"request": "GetVersion"}),
                         (200, {"ok": True, "response": {"obsVersion": "30.0"}}))
        self.assertEqual(call("/api/obs/request", {"request": "NoSuchThing"})[0], 400)  # OBS refused
        with self.assertRaises(ValueError):
            OBS.request("Get Stuff")

    def test_offline(self):
        self.fake.offline = True
        for path, body in (("/api/obs/state", None), ("/api/obs/scene", {"name": "Game"}),
                           ("/api/obs/output", {"what": "stream", "action": "start"})):
            self.assertEqual(call(path, body)[0], 503, path)
        self.subscribe()
        OBS.refresh_state()
        self.assertFalse(OBS.snapshot()["ok"])
        self.assertIn("error", OBS.snapshot())
        self.assertEqual(len(self.fake.calls), 4, "the task stops at the first failing call")

    def test_task_runs_only_with_subscribers(self):
        OBS.refresh_state()
        self.assertEqual(self.fake.calls, [])
        self.assertFalse(OBS.snapshot()["ok"])
        self.subscribe()
        OBS.refresh_state()
        snap = OBS.snapshot()
        self.assertTrue(snap["ok"])
        self.assertEqual((snap["scene"], snap["scenes"]), ("Game", ["Intro", "Game", "BRB"]))
        self.assertEqual(len(snap["inputs"]), 2)
        self.assertIn("stats", snap)

    def test_bitrate_from_byte_delta(self):
        self.fake.out["stream"], self.fake.bytes = True, 1_000_000
        with mock.patch("modules.obs.time.time", side_effect=[100.0, 102.0, 104.0]):
            self.assertEqual(OBS.stream_status()["bitrate_kbps"], 0)  # first call
            self.fake.bytes = 1_000_000 + 250_000                     # 250 kB in 2 s = 1000 kbps
            self.assertEqual(OBS.stream_status()["bitrate_kbps"], 1000)
            self.fake.out["stream"] = False
            self.assertEqual(OBS.stream_status()["bitrate_kbps"], 0)


if __name__ == "__main__":
    unittest.main()
