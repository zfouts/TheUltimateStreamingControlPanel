"""modules/obs.py keeps one obs-websocket connection: a refused request must not leak sockets."""
import unittest
from unittest import mock

from tests.helpers import ROOT  # noqa: F401  (sandbox + sys.path)

from modules.obs import OBS  # noqa: E402
from obsws_python.error import OBSSDKRequestError  # noqa: E402


class FakeClient:
    made = []

    def __init__(self, **kw):
        self.closed = False
        self.fail = None
        FakeClient.made.append(self)

    def send(self, request, data=None, raw=True):
        if self.fail:
            raise self.fail
        return {"ok": request}

    def disconnect(self):
        self.closed = True


class Connection(unittest.TestCase):
    def setUp(self):
        FakeClient.made = []
        self.obs = OBS(mock.Mock())
        self.obs.on_config({})
        self.obs.on_load()
        self.patch = mock.patch("obsws_python.ReqClient", FakeClient)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_refused_requests_reuse_the_connection(self):
        self.obs.send("GetVersion")
        FakeClient.made[0].fail = OBSSDKRequestError("ToggleInputMute", 600, "not found")
        for _ in range(5):
            with self.assertRaises(OBSSDKRequestError):
                self.obs.send("ToggleInputMute", {"inputName": "nope"})
        FakeClient.made[0].fail = None
        self.assertEqual(self.obs.send("GetVersion"), {"ok": "GetVersion"})
        self.assertEqual(len(FakeClient.made), 1)
        self.assertFalse(FakeClient.made[0].closed)

    def test_broken_connection_is_closed_before_reconnecting(self):
        self.obs.send("GetVersion")
        FakeClient.made[0].fail = ConnectionResetError("gone")
        with self.assertRaises(ConnectionResetError):
            self.obs.send("GetVersion")
        self.assertTrue(FakeClient.made[0].closed)
        self.obs.send("GetVersion")
        self.assertEqual(len(FakeClient.made), 2)


if __name__ == "__main__":
    unittest.main()
