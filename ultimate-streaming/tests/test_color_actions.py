"""obs_color and ha_light, against a fake OBS send() and a fake `requests`."""
import types
import unittest
from unittest import mock

from tests.helpers import ROOT  # noqa: F401  (sandbox + sys.path)

from modules import home_assistant  # noqa: E402
from modules.obs import OBS  # noqa: E402


class ReqError(Exception):
    def __init__(self, code):
        self.code = code


ReqError.__name__ = "OBSSDKRequestError"


class ObsColor(unittest.TestCase):
    def setUp(self):
        self.obs = OBS(mock.Mock())
        self.sent = []

    def run_action(self, arg, fail=None):
        def send(request, data=None):
            self.sent.append((request, data))
            if fail:
                raise fail
            return {}
        with mock.patch.object(self.obs, "send", send):
            self.obs.set_color(arg)

    def test_sends_exact_request(self):
        self.run_action({"source": "Background", "color": "#112233"})
        self.assertEqual(self.sent, [("SetInputSettings", {"inputName": "Background",
                                                           "inputSettings": {"color": 0xFF332211}})])

    def test_name_works(self):
        self.run_action({"source": "BG", "color": "Red"})
        self.assertEqual(self.sent[0][1]["inputSettings"]["color"], 0xFF0000FF)

    def test_bad_color_sends_nothing(self):
        with self.assertRaisesRegex(ValueError, "not a color"):
            self.run_action({"source": "BG", "color": "red; rm -rf"})
        self.assertEqual(self.sent, [])

    def test_missing_source_is_friendly(self):
        with self.assertRaisesRegex(RuntimeError, "Nope"):
            self.run_action({"source": "Nope", "color": "red"}, fail=ReqError(600))

    def test_other_errors_propagate(self):
        with self.assertRaises(ReqError):
            self.run_action({"source": "BG", "color": "red"}, fail=ReqError(300))

    def test_source_choices_filter_on_kind(self):
        inputs = [{"inputName": "BG", "inputKind": "color_source_v3"},
                  {"inputName": "Mic", "inputKind": "pulse_input_capture"},
                  {"inputName": "Old", "inputKind": "color_source"}]
        with mock.patch.object(self.obs, "send", lambda r, d=None: {"inputs": inputs}):
            self.assertEqual(self.obs.color_source_choices(), ["BG", "Old"])


class FakeRequests:
    def __init__(self):
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json, headers, timeout))
        return types.SimpleNamespace(raise_for_status=lambda: None)


class HaLight(unittest.TestCase):
    def setUp(self):
        self.ha = home_assistant.HomeAssistant(mock.Mock())
        self.ha.url, self.ha.headers = "http://ha:8123", {"Authorization": "Bearer t"}
        self.fake = FakeRequests()
        p = mock.patch.object(home_assistant, "requests", self.fake)
        p.start()
        self.addCleanup(p.stop)

    def test_on_with_brightness(self):
        self.ha.light({"entity_id": "light.desk", "color": "purple", "brightness": 200})
        self.assertEqual(self.fake.calls, [("http://ha:8123/api/services/light/turn_on",
                                            {"entity_id": "light.desk", "rgb_color": [128, 0, 128], "brightness": 200},
                                            {"Authorization": "Bearer t"}, 5)])

    def test_on_without_brightness(self):
        self.ha.light({"entity_id": "light.desk", "color": "#ff0000"})
        self.assertEqual(self.fake.calls[0][1], {"entity_id": "light.desk", "rgb_color": [255, 0, 0]})

    def test_off(self):
        self.ha.light({"entity_id": "light.desk", "off": True, "color": "nonsense"})
        self.assertEqual(self.fake.calls[0][0], "http://ha:8123/api/services/light/turn_off")
        self.assertEqual(self.fake.calls[0][1], {"entity_id": "light.desk"})

    def test_bad_entity(self):
        for bad in ("switch.fan", "light.", "light.Desk", "light.a/../b", "light.x\n", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.ha.light({"entity_id": bad, "color": "red"})
        self.assertEqual(self.fake.calls, [])

    def test_bad_brightness_and_color(self):
        for arg in ({"brightness": 256}, {"brightness": -1}, {"brightness": "x"}, {"brightness": 1.5},
                    {"brightness": True}, {"color": "blorp"}):
            with self.assertRaises(ValueError, msg=repr(arg)):
                self.ha.light({"entity_id": "light.desk", **{"color": "red", **arg}})
        self.assertEqual(self.fake.calls, [])

    def test_not_configured(self):
        self.ha.url, self.ha.headers = "", None
        with self.assertRaisesRegex(RuntimeError, "not configured"):
            self.ha.light({"entity_id": "light.desk", "color": "red"})
        self.assertEqual(self.fake.calls, [])


if __name__ == "__main__":
    unittest.main()
