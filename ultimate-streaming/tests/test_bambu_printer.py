import json
import logging
import os
import ssl
import sys
import threading
import types
import unittest
from unittest import mock

from tests import helpers  # noqa: F401
from tests.helpers import tmpdir

from plugins.bambu import printer
from plugins.bambu.printer import (MAX_PAYLOAD, PrinterWatcher, PrintStatus, command_payload,
                                   light_payload)

# Not a real certificate: only the PEM armour and the DER SEQUENCE tag are checked.
DER = b"\x30\x82\x01\x0a" + bytes(range(256)) * 2
PEM = ssl.DER_cert_to_PEM_cert(DER)
PEM2 = ssl.DER_cert_to_PEM_cert(DER[:-1] + b"\x01")
CERT_DIR = tmpdir()
CERT = os.path.join(CERT_DIR, "no-such-pin.pem")

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "bambu_push_status_running.json")


def fixture():
    with open(FIXTURE) as f:
        return json.load(f)


def report(state="RUNNING", **fields):
    r = fixture()
    r["print"]["gcode_state"] = state
    r["print"].update(fields)
    return r


class FakeReason:
    def __init__(self, text, failure=False):
        self.text, self.is_failure = text, failure

    def __str__(self):
        return self.text


class RecordingClient:
    def __init__(self):
        self.subscribed, self.published = [], []

    def subscribe(self, topic):
        self.subscribed.append(topic)

    def publish(self, topic, payload):
        self.published.append((topic, json.loads(payload)))
        return types.SimpleNamespace(rc=0)


def watcher(**kw):
    kw.setdefault("cert_file", CERT)
    return PrinterWatcher("printer.local", "01P00A000000000", "s3cretcode", **kw)


def connected(w):
    w._client = RecordingClient()
    w._set_connection(True)
    return w._client


class StatusTests(unittest.TestCase):
    def test_fixture_parses_into_the_contract(self):
        status = PrintStatus()
        self.assertIsNone(status.apply(fixture()))
        self.assertEqual(status.summary(), {
            "state": "RUNNING", "active": True, "percent": 58, "remaining_min": 129,
            "job": "base_shell_2in_plate_2", "layer": 701, "total_layers": 1058,
            "nozzle_temp": 220.0, "bed_temp": 55.0, "light": "on"})

    def test_empty_summary(self):
        self.assertEqual(PrintStatus().summary(), {
            "state": None, "active": False, "percent": None, "remaining_min": None, "job": None,
            "layer": None, "total_layers": None, "nozzle_temp": None, "bed_temp": None, "light": None})

    def test_transitions(self):
        for end in ("FINISH", "FAILED", "IDLE"):
            s = PrintStatus()
            s.apply(report("RUNNING"))
            self.assertEqual(s.apply(report(end)), "ended")
            self.assertFalse(s.summary()["active"])
            self.assertIsNone(s.apply(report(end)))
        s = PrintStatus()
        s.apply(report("PAUSE"))
        self.assertEqual(s.apply(report("FINISH")), "ended")
        s = PrintStatus()
        s.apply(report("RUNNING"))
        self.assertIsNone(s.apply(report("PAUSE")))
        self.assertTrue(s.summary()["active"])
        s = PrintStatus()
        s.apply(report("FINISH"))
        self.assertEqual(s.apply(report("PREPARE")), "started")
        s = PrintStatus()
        s.apply(report("IDLE"))
        self.assertEqual(s.apply(report("RUNNING")), "started")

    def test_partial_report_keeps_earlier_fields(self):
        s = PrintStatus()
        s.apply(report("RUNNING"))
        s.apply({"print": {"command": "push_status", "mc_percent": 61}})
        self.assertEqual((s.summary()["percent"], s.summary()["state"], s.summary()["layer"]),
                         (61, "RUNNING", 701))

    def test_non_status_messages_ignored(self):
        s = PrintStatus()
        s.apply(report("RUNNING"))
        self.assertIsNone(s.apply({"print": {"command": "gcode_line", "gcode_state": "IDLE"}}))
        self.assertIsNone(s.apply({"info": {"command": "get_version"}}))
        self.assertIsNone(s.apply({"print": "not a dict"}))
        self.assertIsNone(s.apply([1, 2]))
        self.assertEqual(s.summary()["state"], "RUNNING")

    def test_odd_types(self):
        s = PrintStatus()
        s.apply(report("RUNNING", layer_num="abc", mc_percent=True, nozzle_temper=1e9, bed_temper=float("nan"),
                       total_layer_num=[1], mc_remaining_time={"a": 1}))
        out = s.summary()
        self.assertIsNone(out["layer"])
        self.assertIsNone(out["percent"])
        self.assertIsNone(out["nozzle_temp"])
        self.assertIsNone(out["bed_temp"])
        self.assertIsNone(out["total_layers"])
        self.assertIsNone(out["remaining_min"])

    def test_percent_clamped_and_temps_rounded(self):
        s = PrintStatus()
        s.apply(report("RUNNING", mc_percent=250, nozzle_temper=219.66, bed_temper=-60))
        out = s.summary()
        self.assertEqual(out["percent"], 100)
        self.assertEqual(out["nozzle_temp"], 219.7)
        self.assertIsNone(out["bed_temp"])
        s.apply(report("RUNNING", mc_percent=-4.9))
        self.assertEqual(s.summary()["percent"], 0)

    def test_hostile_state(self):
        s = PrintStatus()
        s.apply(report("RUN\nNING"))
        self.assertIsNone(s.summary()["state"])
        self.assertFalse(s.summary()["active"])
        s.apply(report("running"))
        self.assertEqual(s.summary()["state"], "RUNNING")
        s.apply(report(["RUNNING"]))
        self.assertIsNone(s.summary()["state"])

    def test_job_is_cleaned_with_fallback(self):
        s = PrintStatus()
        s.apply(report(subtask_name="/a/b\\c\n\u202ed.gcode.3mf"))
        self.assertEqual(s.summary()["job"], "cd")
        s.apply(report(subtask_name="x" * 200))
        self.assertLessEqual(len(s.summary()["job"]), 64)
        s.apply(report(subtask_name="", gcode_file="/data/Metadata/plate_2.gcode"))
        self.assertEqual(s.summary()["job"], "plate_2")
        s.apply(report(subtask_name=7, gcode_file=None))
        self.assertIsNone(s.summary()["job"])

    def test_light(self):
        s = PrintStatus()
        for mode, want in (("on", "on"), ("off", "off"), ("flashing", "on"), ("weird", None)):
            s.apply(report(lights_report=[{"node": "work_light", "mode": "on"},
                                          {"node": "chamber_light", "mode": mode}]))
            self.assertEqual(s.summary()["light"], want)
        s.apply(report(lights_report="nope"))
        self.assertIsNone(s.summary()["light"])
        s.apply(report(lights_report=[None, 3, {"node": "chamber_light"}]))
        self.assertIsNone(s.summary()["light"])


class PayloadTests(unittest.TestCase):
    def test_exact(self):
        self.assertEqual(command_payload("pause"), {"print": {"sequence_id": "0", "command": "pause", "param": ""}})
        self.assertEqual(command_payload("stop")["print"]["command"], "stop")
        self.assertEqual(command_payload("resume")["print"]["command"], "resume")
        with self.assertRaises(ValueError):
            command_payload("gcode_line")
        self.assertEqual(light_payload(True), {"system": {
            "sequence_id": "0", "command": "ledctrl", "led_node": "chamber_light", "led_mode": "on",
            "led_on_time": 500, "led_off_time": 500, "loop_times": 0, "interval_time": 0}})
        self.assertEqual(light_payload(False)["system"]["led_mode"], "off")


class WatcherTests(unittest.TestCase):
    def test_constructor_validation(self):
        for host in ("", "a b", "a/b", "http://x", "x;y", None):
            with self.assertRaises(ValueError, msg=repr(host)):
                PrinterWatcher(host, "01P00A000000000", "c", cert_file=CERT)
        for serial in ("abc", "01p00a000000000", "01P00A/00000000", "A" * 21, None):
            with self.assertRaises(ValueError, msg=repr(serial)):
                PrinterWatcher("1.2.3.4", serial, "c", cert_file=CERT)
        PrinterWatcher("192.168.1.20", "01P00A000000000", "c", cert_file=CERT)
        PrinterWatcher("fe80::1", "01P00A000000000", "c", cert_file=CERT)

    def test_bad_payloads_dropped(self):
        w = watcher()
        w.handle_payload(json.dumps(report("RUNNING")).encode())
        w.handle_payload(b"x" * (MAX_PAYLOAD + 1))
        w.handle_payload(json.dumps(report("FINISH")).ljust(MAX_PAYLOAD + 1).encode())
        w.handle_payload(b"\x00not json")
        w.handle_payload(b"[1, 2]")
        w.handle_payload(b"[" * 100000)
        w.handle_payload(b"\xff\xfe")
        self.assertEqual(w.snapshot()["state"], "RUNNING")

    def test_snapshot_contract(self):
        w = watcher()
        w.handle_report(fixture())
        snap = w.snapshot()
        self.assertEqual(set(snap), {"connected", "state", "active", "percent", "remaining_min", "job", "layer",
                                     "total_layers", "nozzle_temp", "bed_temp", "light", "error"})
        self.assertFalse(snap["connected"])
        json.dumps(snap)

    def test_on_event_gets_event_and_summary(self):
        events = []
        w = watcher(on_event=lambda e, s: events.append((e, s)))
        w.handle_payload(json.dumps(report("RUNNING")).encode())
        w.handle_payload(json.dumps(report("FINISH")).encode())
        self.assertEqual([e for e, _ in events], ["ended"])
        self.assertEqual(events[0][1]["state"], "FINISH")
        self.assertIn("connected", events[0][1])

    def test_failing_handler_does_not_raise(self):
        def boom(event, summary):
            raise ValueError("x")
        w = watcher(on_event=boom)
        w.handle_report(report("RUNNING"))
        with self.assertLogs("bambu.printer", logging.ERROR):
            w.handle_report(report("FINISH"))

    def test_publish_when_disconnected_raises(self):
        w = watcher()
        for call in (w.pause, w.resume, w.stop_print, lambda: w.set_light(True),
                     lambda: w.publish(command_payload("pause"))):
            with self.assertRaisesRegex(RuntimeError, "printer not connected"):
                call()

    def test_commands_publish_fixed_payloads(self):
        w = watcher()
        client = connected(w)
        w.pause()
        w.resume()
        w.stop_print()
        w.set_light(False)
        topic = "device/01P00A000000000/request"
        self.assertEqual(client.published, [
            (topic, command_payload("pause")), (topic, command_payload("resume")),
            (topic, command_payload("stop")), (topic, light_payload(False))])

    def test_on_connect(self):
        w = watcher()
        client = RecordingClient()
        w._on_connect(client, None, None, FakeReason("Success"), None)
        self.assertEqual(client.subscribed, ["device/01P00A000000000/report"])
        self.assertEqual(client.published, [("device/01P00A000000000/request", json.loads(printer.PUSHALL))])
        self.assertTrue(w.snapshot()["connected"])
        self.assertIsNone(w.snapshot()["error"])

    def test_refused_connection(self):
        w = watcher()
        client = RecordingClient()
        with self.assertLogs("bambu.printer", logging.WARNING):
            w._on_connect(client, None, None, FakeReason("Not authorized", True), None)
        self.assertEqual(client.subscribed, [])
        snap = w.snapshot()
        self.assertFalse(snap["connected"])
        self.assertEqual(snap["error"], "Printer refused the connection (check the access code)")

    def test_unreachable_and_disconnect(self):
        w = watcher()
        w._on_connect_fail(None, None)
        self.assertEqual(w.snapshot()["error"], "Can't reach the printer")
        w._on_connect(RecordingClient(), None, None, FakeReason("Success"), None)
        w._on_disconnect(None, None, None, FakeReason("Unspecified error"), None)
        snap = w.snapshot()
        self.assertFalse(snap["connected"])
        self.assertEqual(snap["error"], "Can't reach the printer")

    def test_message_from_another_thread(self):
        events = []
        w = watcher(on_event=lambda e, s: events.append(e))
        for state in ("RUNNING", "FINISH"):
            msg = types.SimpleNamespace(payload=json.dumps(report(state)).encode())
            t = threading.Thread(target=w._on_message, args=(None, None, msg))
            t.start()
            t.join()
        self.assertEqual(events, ["ended"])

    def test_redact(self):
        w = watcher()
        out = w.redact("rtsps://bblp:s3cretcode@192.168.1.5:322/streaming/live/1 and s3cretcode")
        self.assertNotIn("s3cretcode", out)
        self.assertIn("<access code>", out)
        out = w.redact("rtsps://bblp:other-pass@host/x")
        self.assertNotIn("other-pass", out)

    # ------------------------------------------------------------ certificate pinning

    def fake_paho(self):
        client = mock.MagicMock()
        mqtt = types.ModuleType("paho.mqtt.client")
        mqtt.Client = mock.MagicMock(return_value=client)
        mqtt.CallbackAPIVersion = types.SimpleNamespace(VERSION2="v2")
        mods = {"paho": types.ModuleType("paho"), "paho.mqtt": types.ModuleType("paho.mqtt"),
                "paho.mqtt.client": mqtt}
        mods["paho"].mqtt = mods["paho.mqtt"]
        mods["paho.mqtt"].client = mqtt
        patch = mock.patch.dict(sys.modules, mods)
        patch.start()
        self.addCleanup(patch.stop)
        return mqtt, client

    def pin_path(self):
        return os.path.join(tmpdir(), "bambu-mqtt-01P00A000000000.pem")

    def test_start_pins_on_first_use_and_verifies(self):
        mqtt, client = self.fake_paho()
        pin = self.pin_path()
        with mock.patch.object(printer.ssl, "get_server_certificate", return_value=PEM) as fetch, \
                mock.patch.object(printer, "pinned_context") as context:
            w = watcher(cert_file=pin)
            w.start()
            w.stop()
        fetch.assert_called_once_with(("printer.local", 8883), timeout=printer.CERT_TIMEOUT)
        with open(pin) as f:
            self.assertEqual(f.read(), PEM)
        self.assertEqual(oct(os.stat(pin).st_mode & 0o777), "0o600")
        context.assert_called_once_with(pin)
        mqtt.Client.assert_called_once_with("v2")
        client.username_pw_set.assert_called_once_with("bblp", "s3cretcode")
        client.tls_set_context.assert_called_once_with(context.return_value)
        client.tls_set.assert_not_called()
        client.tls_insecure_set.assert_not_called()
        client.reconnect_delay_set.assert_called_once_with(min_delay=1, max_delay=30)
        client.connect_async.assert_called_once_with("printer.local", 8883, keepalive=30)
        client.loop_start.assert_called_once()
        client.loop_stop.assert_called_once()

    def test_existing_pin_is_not_fetched_again(self):
        self.fake_paho()
        pin = self.pin_path()
        with open(pin, "w") as f:
            f.write(PEM2)
        with mock.patch.object(printer.ssl, "get_server_certificate") as fetch, \
                mock.patch.object(printer, "pinned_context"):
            w = watcher(cert_file=pin)
            w.start()
            w.stop()
        fetch.assert_not_called()
        with open(pin) as f:
            self.assertEqual(f.read(), PEM2)

    def test_forget_then_repin(self):
        self.fake_paho()
        pin = self.pin_path()
        with open(pin, "w") as f:
            f.write(PEM)
        w = watcher(cert_file=pin)
        w.forget_certificate()
        w.forget_certificate()      # nothing there is fine
        self.assertFalse(os.path.exists(pin))
        with mock.patch.object(printer.ssl, "get_server_certificate", return_value=PEM2), \
                mock.patch.object(printer, "pinned_context"):
            w.start()
            w.stop()
        with open(pin) as f:
            self.assertEqual(f.read(), PEM2)

    def test_unreachable_printer_keeps_trying_to_pin(self):
        mqtt, client = self.fake_paho()
        pin = self.pin_path()
        results = [OSError("timed out"), PEM]
        def fetch(*a, **kw):
            r = results.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        with mock.patch.object(printer.ssl, "get_server_certificate", side_effect=fetch), \
                mock.patch.object(printer, "pinned_context"), \
                mock.patch.object(printer, "PIN_RETRY", (0.05,)):
            w = watcher(cert_file=pin)
            w.start()
            self.assertEqual(w.snapshot()["error"], "Can't reach the printer")
            client.connect_async.assert_not_called()
            for _ in range(100):
                if client.connect_async.called:
                    break
                threading.Event().wait(0.05)
            w.stop()
        client.connect_async.assert_called_once()
        self.assertTrue(os.path.exists(pin))

    def test_stop_before_the_pin_arrives_writes_nothing(self):
        self.fake_paho()
        pin = self.pin_path()
        w = watcher(cert_file=pin)
        w.stop()
        stopping = w._stopping
        self.assertTrue(stopping.is_set())
        with mock.patch.object(printer.ssl, "get_server_certificate", return_value=PEM):
            self.assertFalse(w._pin(stopping))
        self.assertFalse(os.path.exists(pin))

    def make_cert(self):
        """(cert, key) paths of a fresh self-signed leaf named like a printer serial."""
        import subprocess
        d = tmpdir()
        key, pem = os.path.join(d, "k"), os.path.join(d, "c.pem")
        try:
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key,
                            "-out", pem, "-days", "2", "-subj", "/CN=01P00A000000000"],
                           check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("openssl isn't available")
        return pem, key

    def handshake(self, pin, served):
        """Real TLS over loopback: a server presenting `served` against a client pinned to `pin`."""
        import socket
        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.load_cert_chain(*served)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)

        def serve():
            try:
                conn, _ = listener.accept()
                with conn:
                    server.wrap_socket(conn, server_side=True).recv(1)
            except (OSError, ssl.SSLError):
                pass
        threading.Thread(target=serve, daemon=True).start()
        sock = socket.create_connection(listener.getsockname(), timeout=5)
        self.addCleanup(sock.close)
        printer.pinned_context(pin).wrap_socket(sock, server_hostname="127.0.0.1").close()

    def test_context_accepts_the_pinned_certificate_only(self):
        mine, other = self.make_cert(), self.make_cert()
        self.handshake(mine[0], mine)
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.handshake(mine[0], other)    # same name, different key

    def test_changed_certificate_is_reported_and_cleared_on_connect(self):
        pin = self.pin_path()
        with open(pin, "w") as f:
            f.write(PEM)
        w = watcher(cert_file=pin)
        with mock.patch.object(printer.ssl, "get_server_certificate", return_value=PEM2):
            w._on_connect_fail(None, None)
            self.wait_probe(w)
        snap = w.snapshot()
        self.assertEqual(snap["error"], printer.ERR_CERT)
        self.assertIn("Trust printer cert again", snap["error"])
        w._on_connect(RecordingClient(), None, None, FakeReason("Success"), None)
        self.assertIsNone(w.snapshot()["error"])
        self.assertFalse(w._cert_changed)

    def test_same_certificate_is_just_unreachable(self):
        pin = self.pin_path()
        with open(pin, "w") as f:
            f.write(PEM)
        w = watcher(cert_file=pin)
        with mock.patch.object(printer.ssl, "get_server_certificate", return_value=PEM):
            w._on_connect_fail(None, None)
            self.wait_probe(w)
        self.assertEqual(w.snapshot()["error"], "Can't reach the printer")

    def test_probe_failure_is_not_a_certificate_error(self):
        pin = self.pin_path()
        with open(pin, "w") as f:
            f.write(PEM)
        w = watcher(cert_file=pin)
        with mock.patch.object(printer.ssl, "get_server_certificate", side_effect=OSError("down")):
            w._on_connect_fail(None, None)
            self.wait_probe(w)
        self.assertEqual(w.snapshot()["error"], "Can't reach the printer")

    @staticmethod
    def wait_probe(w):
        for t in threading.enumerate():
            if t.name == "bambu-mqtt-probe":
                t.join(5)


if __name__ == "__main__":
    unittest.main()
