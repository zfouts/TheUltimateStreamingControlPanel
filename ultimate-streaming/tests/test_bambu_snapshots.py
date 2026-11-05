import json
import logging
import threading
import time
import unittest

import requests

from tests import helpers  # noqa: F401

from plugins.bambu import snapshots
from plugins.bambu.snapshots import DiscordSnapshots, validate_webhook

WEBHOOK = "https://discord.com/api/webhooks/123/SeCrEtWeBhOoKtOkEn"
JPEG = b"\xff\xd8\xff\xe0fake-jpeg"


class Resp:
    def __init__(self, status_code=204):
        self.status_code = status_code


class Harness:
    def __init__(self, state="RUNNING", layer=701, status=204, **kw):
        self.summary = {"connected": True, "state": state, "active": state in ("RUNNING", "PREPARE"),
                        "percent": 58, "remaining_min": 10, "job": "job", "layer": layer,
                        "total_layers": 1058}
        self.posts = []
        self.status_code = status
        self.frame_error = None
        self.post_error = None
        self.frames = 0
        self.got_post = threading.Event()
        kw.setdefault("min_gap", 0)
        kw.setdefault("final_delay", 0.05)
        kw.setdefault("poll", 0.01)
        self.snaps = DiscordSnapshots(
            WEBHOOK, grab_frame=self._frame, printer_snapshot=lambda: dict(self.summary),
            caption=lambda s, ended: f"{'END' if ended else 'MID'} layer {s['layer']}",
            post=self._post, **kw)

    def _frame(self):
        self.frames += 1
        if self.frame_error:
            raise self.frame_error
        return JPEG

    def _post(self, url, data=None, files=None, timeout=None):
        assert url == WEBHOOK
        if self.post_error:
            raise self.post_error
        self.posts.append({"payload": json.loads(data["payload_json"]), "file": files["files[0]"]})
        self.got_post.set()
        return Resp(self.status_code)

    def set(self, **kw):
        self.summary.update(kw)

    def wait_posts(self, n, timeout=2.0):
        end = time.monotonic() + timeout
        while len(self.posts) < n and time.monotonic() < end:
            time.sleep(0.005)
        return len(self.posts) >= n

    def contents(self):
        return [p["payload"]["content"] for p in self.posts]


class ValidateWebhookTest(unittest.TestCase):
    def test_table(self):
        good = [
            "https://discord.com/api/webhooks/1/tok",
            "https://discordapp.com/api/webhooks/1/tok",
            "https://ptb.discord.com/api/webhooks/1/tok",
            "https://canary.discord.com/api/webhooks/1/tok?thread_id=5",
        ]
        bad = [
            "http://discord.com/api/webhooks/1/tok",
            "https://example.com/api/webhooks/1/tok",
            "https://discord.com.evil.com/api/webhooks/1/tok",
            "https://evil.com/discord.com/api/webhooks/1/tok",
            "https://user:pw@discord.com/api/webhooks/1/tok",  # privacy-check: allow
            "https://discord.com@evil.com/api/webhooks/1/tok",  # privacy-check: allow
            "https://discord.com/api/other/1/tok",
            "https://discord.com/api/webhooks/1/tok#frag",
            "https://discord.com:bad/api/webhooks/1/tok",
            "", None,
        ]
        for url in good:
            self.assertEqual(validate_webhook(url), url)
        for url in bad:
            with self.assertRaises(ValueError, msg=url) as cm:
                validate_webhook(url)
            self.assertEqual(str(cm.exception), "That isn't a Discord webhook URL.")

    def test_constructor_validates_without_leaking(self):
        with self.assertRaises(ValueError) as cm:
            DiscordSnapshots("http://evil.com/api/webhooks/1/SeCrEt", grab_frame=None,
                             printer_snapshot=None, caption=None)
        self.assertNotIn("SeCrEt", str(cm.exception))


class CadenceTest(unittest.TestCase):
    def test_posts_captioned_jpeg_at_each_multiple(self):
        h = Harness()
        h.snaps._round()                       # first look only sets the mark
        self.assertEqual(h.posts, [])
        self.assertEqual(h.snaps.status()["next_layer"], 710)
        h.set(layer=710)
        h.snaps._round()
        h.set(layer=720)
        h.snaps._round()
        self.assertEqual(h.contents(), ["MID layer 710", "MID layer 720"])
        self.assertEqual(h.posts[0]["file"][0].startswith("print-"), True)
        self.assertTrue(h.posts[0]["file"][0].endswith(".jpg"))
        self.assertEqual(h.posts[0]["file"][1], JPEG)
        self.assertEqual(h.posts[0]["file"][2], "image/jpeg")

    def test_allowed_mentions_always_empty(self):
        h = Harness(every_layers=1)
        h.snaps._round()
        h.set(layer=702)
        h.snaps._round()
        h.snaps.on_event("started")
        h.snaps._round()
        h.snaps.post(ended=True)
        self.assertEqual(len(h.posts), 3)
        for p in h.posts:
            self.assertEqual(p["payload"]["allowed_mentions"], {"parse": []})

    def test_between_multiples_nothing(self):
        h = Harness()
        h.snaps._round()
        for layer in range(702, 710):
            h.set(layer=layer)
            h.snaps._round()
        self.assertEqual(h.posts, [])

    def test_jump_past_several_multiples_posts_once(self):
        h = Harness()
        h.snaps._round()
        h.set(layer=735)
        h.snaps._round()
        h.snaps._round()
        self.assertEqual(h.contents(), ["MID layer 735"])
        h.set(layer=740)
        h.snaps._round()
        self.assertEqual(len(h.posts), 2)

    def test_min_gap_holds_back_then_posts_current_layer(self):
        h = Harness(every_layers=1, min_gap=0.2)
        h.snaps._round()
        h.set(layer=702)
        h.snaps._round()
        h.set(layer=704)
        h.snaps._round()
        self.assertEqual(len(h.posts), 1)
        time.sleep(0.25)
        h.snaps._round()
        self.assertEqual(h.contents(), ["MID layer 702", "MID layer 704"])

    def test_mid_print_start_waits_for_next_multiple(self):
        h = Harness()
        h.snaps._round()
        self.assertEqual(h.posts, [])
        self.assertEqual(h.snaps.status()["next_layer"], 710)

    def test_next_layer_none_past_last_multiple(self):
        h = Harness(layer=1055)
        h.snaps._round()
        self.assertIsNone(h.snaps.status()["next_layer"])

    def test_paused_idle_disconnected_post_nothing(self):
        for kw in ({"state": "PAUSE", "active": False}, {"state": "IDLE", "active": False}, {"connected": False}):
            h = Harness(every_layers=1)
            h.set(**kw)
            h.snaps._round()
            h.set(layer=702)
            h.snaps._round()
            h.snaps.on_event("started")
            h.snaps._round()
            self.assertEqual(h.posts, [], kw)
            self.assertFalse(h.snaps.status()["printing"], kw)

    def test_new_print_counts_from_its_own_start(self):
        h = Harness(state="FINISH", layer=1058)
        h.snaps._round()
        h.set(state="PREPARE", active=True, layer=0)
        h.snaps.on_event("started")
        h.snaps._round()
        self.assertEqual(h.contents(), ["MID layer 0"])
        h.set(state="RUNNING", layer=10)
        h.snaps._round()
        self.assertEqual(h.contents(), ["MID layer 0", "MID layer 10"])

    def test_layer_drop_without_event_restarts_counting(self):
        h = Harness()
        h.snaps._round()
        h.set(layer=3)
        h.snaps._round()
        self.assertEqual(h.posts, [])
        h.set(layer=10)
        h.snaps._round()
        self.assertEqual(h.contents(), ["MID layer 10"])


class ThreadedTest(unittest.TestCase):
    def test_started_event_posts_immediately_via_worker(self):
        h = Harness(state="PREPARE", layer=0)
        h.snaps.start()
        try:
            h.snaps.on_event("started")
            self.assertTrue(h.wait_posts(1))
        finally:
            h.snaps.stop()
        self.assertEqual(h.contents(), ["MID layer 0"])

    def test_layer_post_via_worker(self):
        h = Harness()
        h.snaps.start()
        try:
            end = time.monotonic() + 2
            while h.snaps.status()["next_layer"] is None and time.monotonic() < end:
                time.sleep(0.005)
            h.set(layer=710)
            self.assertTrue(h.wait_posts(1))
        finally:
            h.snaps.stop()

    def test_ended_posts_final_shot_after_delay(self):
        h = Harness(final_delay=0.15)
        h.snaps.start()
        try:
            h.set(state="FINISH", active=False)
            h.snaps.on_event("ended")
            time.sleep(0.05)
            self.assertEqual(h.posts, [])
            self.assertTrue(h.wait_posts(1))
        finally:
            h.snaps.stop()
        self.assertEqual(h.contents(), ["END layer 701"])

    def test_started_cancels_pending_final_shot(self):
        h = Harness(final_delay=0.2)
        h.snaps.start()
        try:
            h.snaps.on_event("ended")
            h.set(state="PREPARE", active=True, layer=0)
            h.snaps.on_event("started")
            self.assertTrue(h.wait_posts(1))
            time.sleep(0.35)
        finally:
            h.snaps.stop()
        self.assertEqual(h.contents(), ["MID layer 0"])

    def test_stop_cancels_pending_final_shot(self):
        h = Harness(final_delay=0.15)
        h.snaps.start()
        h.snaps.on_event("ended")
        h.snaps.stop()
        time.sleep(0.3)
        self.assertEqual(h.posts, [])

    def test_stop_is_prompt(self):
        h = Harness(poll=30)
        h.snaps.start()
        t = time.monotonic()
        h.snaps.stop()
        self.assertLess(time.monotonic() - t, 1.0)
        self.assertFalse(any(t.name == "bambu-snapshots" for t in threading.enumerate()))

    def test_worker_survives_exceptions(self):
        h = Harness(every_layers=1)
        calls = []

        def boom():
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("boom")
            return dict(h.summary)

        h.snaps.printer_snapshot = boom
        h.snaps.start()
        try:
            end = time.monotonic() + 2
            while len(calls) < 5 and time.monotonic() < end:
                time.sleep(0.005)
            self.assertTrue(h.snaps._thread.is_alive())
        finally:
            h.snaps.stop()


class FailureTest(unittest.TestCase):
    def test_camera_failure_skips_and_recovers(self):
        h = Harness()
        h.frame_error = OSError("camera down")
        h.snaps._round()
        h.set(layer=710)
        with self.assertLogs(snapshots.log, "WARNING"):
            h.snaps._round()
        self.assertEqual(h.snaps.status()["error"], "Couldn't grab a frame from the camera.")
        h.frame_error = None
        h.set(layer=720)
        h.snaps._round()
        self.assertEqual(len(h.posts), 1)
        self.assertIsNone(h.snaps.status()["error"])

    def test_http_rejection_and_network_error_hide_url(self):
        h = Harness(status=429)
        with self.assertLogs(snapshots.log, "WARNING") as cm:
            self.assertFalse(h.snaps.post())
        self.assertEqual(h.snaps.status()["error"],
                         "Discord rejected the post (HTTP 429). Check the webhook URL.")
        h.post_error = ConnectionError(f"failed to reach {WEBHOOK}")
        with self.assertLogs(snapshots.log, "WARNING") as cm2:
            self.assertFalse(h.snaps.post())
        self.assertEqual(h.snaps.status()["error"], "Couldn't reach Discord.")
        h.frame_error = OSError(f"bad {WEBHOOK}")
        with self.assertLogs(snapshots.log, "WARNING") as cm3:
            self.assertFalse(h.snaps.post())
        for out in cm.output + cm2.output + cm3.output:
            self.assertNotIn("SeCrEtWeBhOoKtOkEn", out)
        self.assertNotIn("SeCrEtWeBhOoKtOkEn", json.dumps(h.snaps.status()))
        self.assertTrue(any("<webhook>" in o for o in cm2.output))

    def test_requests_connection_error_path_is_redacted(self):
        h = Harness()
        h.post_error = requests.ConnectionError(
            "HTTPSConnectionPool(host='discord.com', port=443): Max retries exceeded with url: "
            "/api/webhooks/123/abcTOKEN?wait=true (Caused by NewConnectionError('x'))")
        with self.assertLogs(snapshots.log, "WARNING") as cm:
            self.assertFalse(h.snaps.post())
        self.assertNotIn("abcTOKEN", "\n".join(cm.output))
        self.assertNotIn("abcTOKEN", json.dumps(h.snaps.status()))
        self.assertNotIn("abcTOKEN", snapshots.DiscordSnapshots._redact(
            h.snaps, "url: /api/webhooks/9/abcTOKEN (Caused"))

    def test_urllib3_debug_log_is_redacted_and_filter_installed_once(self):
        h = Harness()
        Harness()
        lg = logging.getLogger("urllib3.connectionpool")
        self.assertEqual(sum(getattr(f, "redacts_webhook", False) for f in lg.filters), 1)
        with self.assertLogs(lg, "DEBUG") as cm:
            lg.debug('%s "POST %s HTTP/1.1" %s', "https://discord.com:443",
                     "/api/webhooks/123/abcTOKEN", 204)
        out = "\n".join(cm.output)
        self.assertIn("<webhook>", out)
        self.assertNotIn("abcTOKEN", out)


if __name__ == "__main__":
    unittest.main()
