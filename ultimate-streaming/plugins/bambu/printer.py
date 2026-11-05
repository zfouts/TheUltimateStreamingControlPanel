"""The printer's state, read from its local MQTT broker.

The printer publishes `push_status` reports on device/<serial>/report (TLS on 8883, user bblp,
password = LAN access code). The P2S sends the full report about once a second; X1-family printers
send only changed fields, so reports are merged rather than replaced.

The broker's certificate is pinned on first use, like the camera's (trust on first use): the
leaf the printer presents is saved per serial, and from then on it is the only certificate the
connection trusts (a sole trust anchor, so a different certificate fails the TLS handshake
before the access code is sent). Verification is never turned off. The leaf names the serial
rather than the host, and may carry no subjectAltName, so the hostname check is off and the pin
is what identifies the printer. forget_certificate() drops the pin so the next start re-pins.

Reports are untrusted: every field is type-checked and range-checked on the way into summary(), and
nothing from a report is ever put into a command. Commands are fixed dicts (command_payload,
light_payload).
"""
import ipaddress
import json
import logging
import math
import os
import re
import ssl
import threading
import time

from core.safefs import write_atomic

from . import wording
from .camera import CERT_TIMEOUT, checked_pem, pem_fingerprint

log = logging.getLogger("bambu.printer")

MQTT_PORT = 8883
PIN_RETRY = (5, 10, 30)     # seconds between attempts to fetch the certificate while unreachable
PROBE_GAP = 30              # seconds between looks at whether a failed connection is a changed certificate
ERR_CERT = ("The printer's certificate changed since it was first trusted. If the printer was "
            "reset or replaced, use Trust printer cert again.")

ACTIVE = {"PREPARE", "RUNNING", "PAUSE"}
ENDED = {"FINISH", "FAILED", "IDLE"}
MAX_PAYLOAD = 256 * 1024

PUSHALL = json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}})

STATE_RE = re.compile(r"^[A-Z_]{1,16}$")
SERIAL_RE = re.compile(r"^[A-Z0-9]{8,20}$")
HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
USERINFO_RE = re.compile(r"bblp:[^@\s/]*@", re.IGNORECASE)
REDACTED = "<access code>"

# The report fields PrintStatus keeps; everything else (AMS, HMS, ...) is dropped on arrival.
KEEP = ("gcode_state", "mc_percent", "mc_remaining_time", "subtask_name", "gcode_file", "layer_num",
        "total_layer_num", "nozzle_temper", "bed_temper", "lights_report")

COMMANDS = ("pause", "resume", "stop")


def fetch_pem(host, port=MQTT_PORT):
    """The certificate the printer presents, as one clean PEM block (not verified: this is how
    the pin is taken, and how a changed certificate is told from an unreachable printer)."""
    return checked_pem(ssl.get_server_certificate((host, port), timeout=CERT_TIMEOUT))


def pinned_context(cert_file):
    """A TLS client context that trusts only the pinned certificate. The pin is a leaf, so
    OpenSSL needs partial-chain mode to accept it as an anchor."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = False     # see the module docstring: the pin names the printer
    ctx.verify_flags = (ctx.verify_flags | ssl.VERIFY_X509_PARTIAL_CHAIN) & ~ssl.VERIFY_X509_STRICT
    ctx.load_verify_locations(cafile=cert_file)
    return ctx


def command_payload(kind: str) -> dict:
    if kind not in COMMANDS:
        raise ValueError(f"unknown printer command: {kind!r}")
    return {"print": {"sequence_id": "0", "command": kind, "param": ""}}


def light_payload(on: bool) -> dict:
    return {"system": {"sequence_id": "0", "command": "ledctrl", "led_node": "chamber_light",
                       "led_mode": "on" if on else "off", "led_on_time": 500, "led_off_time": 500,
                       "loop_times": 0, "interval_time": 0}}


def _number(value):
    """An int or finite float as given; bools, strings and everything else are None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _count(value, high=10_000_000):
    n = _number(value)
    if n is None:
        return None
    return max(0, min(int(n), high))


def _temp(value):
    n = _number(value)
    if n is None or not -50 <= n <= 600:
        return None
    return round(float(n), 1)


class PrintStatus:
    def __init__(self):
        self._fields = {}

    def _state(self):
        state = self._fields.get("gcode_state")
        if isinstance(state, str) and STATE_RE.match(state.upper()):
            return state.upper()
        return None

    def apply(self, report) -> str | None:
        """Merge one MQTT report. Returns "ended" when an active print reaches an end state,
        "started" when a new print begins, otherwise None. Never raises on odd input."""
        try:
            body = report.get("print") if isinstance(report, dict) else None
            if not isinstance(body, dict) or body.get("command") != "push_status":
                return None
            before = self._state()
            self._fields.update({k: body[k] for k in KEEP if k in body})
            after = self._state()
        except Exception:
            log.warning("ignoring a malformed printer report")
            return None

        if before in ACTIVE and after in ENDED:
            return "ended"
        if before in ENDED and after in ACTIVE:
            return "started"
        return None

    def _job(self):
        for key in ("subtask_name", "gcode_file"):
            job = wording.clean_job(self._fields.get(key)) if isinstance(self._fields.get(key), str) else None
            if job:
                return job
        return None

    def _light(self):
        report = self._fields.get("lights_report")
        if not isinstance(report, list):
            return None
        for entry in report:
            if isinstance(entry, dict) and entry.get("node") == "chamber_light":
                mode = entry.get("mode")
                if mode in ("on", "flashing"):
                    return "on"
                if mode == "off":
                    return "off"
        return None

    def summary(self) -> dict:
        f = self._fields
        state = self._state()
        percent = _count(f.get("mc_percent"), 100)
        return {
            "state": state,
            "active": state in ACTIVE,
            "percent": percent,
            "remaining_min": _count(f.get("mc_remaining_time")),
            "job": self._job(),
            "layer": _count(f.get("layer_num")),
            "total_layers": _count(f.get("total_layer_num")),
            "nozzle_temp": _temp(f.get("nozzle_temper")),
            "bed_temp": _temp(f.get("bed_temper")),
            "light": self._light(),
        }


class PrinterWatcher:
    """Keeps a PrintStatus current from the printer's MQTT reports.

    paho runs its own network thread and calls on_event(event, summary) from it, so on_event must
    be quick. snapshot() and the command methods are safe to call from any thread.
    """

    def __init__(self, host: str, serial: str, access_code: str, on_event=None, *, cert_file):
        if not isinstance(host, str) or not self._valid_host(host):
            raise ValueError("printer host must be a hostname or IP address")
        if not isinstance(serial, str) or not SERIAL_RE.match(serial):
            raise ValueError("printer serial must be 8-20 capital letters and digits")
        self.host = host
        self.serial = serial
        self.access_code = str(access_code or "")
        self.on_event = on_event
        self.cert_file = cert_file
        self.status = PrintStatus()
        self.connected = False
        self._error = None
        self._client = None
        self._stopping = threading.Event()
        self._cert_changed = False
        self._probed = None      # monotonic time of the last certificate look
        self._lock = threading.Lock()

    @staticmethod
    def _valid_host(host: str) -> bool:
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            return bool(HOST_RE.match(host))

    def start(self):
        import paho.mqtt.client as mqtt
        stopping = threading.Event()
        with self._lock:
            self._stopping = stopping
            self._cert_changed = False
        if self._pin(stopping):
            self._connect(mqtt, stopping)
        else:
            threading.Thread(target=self._pin_then_connect, args=(mqtt, stopping),
                             name="bambu-mqtt-pin", daemon=True).start()

    def _pin_then_connect(self, mqtt, stopping):
        """The printer was off (or unreachable) when it was time to take its certificate: keep
        trying until it answers or the watcher stops."""
        failures = 0
        while not stopping.wait(PIN_RETRY[min(failures, len(PIN_RETRY) - 1)]):
            failures += 1
            if self._pin(stopping):
                self._connect(mqtt, stopping)
                return

    def _pin(self, stopping):
        """Make sure cert_file holds the printer's certificate, fetching it if missing (trust on
        first use). False if it couldn't be fetched or saved."""
        if os.path.exists(self.cert_file):
            return True
        try:
            pem = fetch_pem(self.host)
        except (OSError, ValueError) as e:
            log.warning("couldn't get the printer's certificate: %s", self.redact(e))
            self._set_connection(False, "Can't reach the printer")
            return False
        with self._lock:
            # Under the lock, so stop() + forget_certificate() can't be undone by this fetch.
            if stopping.is_set():
                return False
            try:
                write_atomic(self.cert_file, pem.encode("ascii"))
            except OSError as e:
                log.warning("couldn't save the printer's certificate: %s", self.redact(e))
                self._error = "Couldn't save the printer's certificate"
                return False
        log.info("trusted the printer's certificate (sha256 %s)", pem_fingerprint(pem))
        return True

    def _connect(self, mqtt, stopping):
        try:
            context = pinned_context(self.cert_file)
        except (OSError, ssl.SSLError) as e:
            log.warning("couldn't load the printer's saved certificate: %s", self.redact(e))
            self._set_connection(False, "Couldn't read the printer's saved certificate")
            return
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        client.username_pw_set("bblp", self.access_code)
        client.tls_set_context(context)
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_connect_fail = self._on_connect_fail
        client.on_message = self._on_message
        with self._lock:
            if stopping.is_set():
                return
            self._client = client
        client.connect_async(self.host, MQTT_PORT, keepalive=30)
        client.loop_start()

    def forget_certificate(self):
        """Drop the pinned certificate; the next start trusts whatever the printer presents
        then. Stop the watcher first: a stopped one never writes its pin."""
        with self._lock:
            try:
                os.unlink(self.cert_file)
            except FileNotFoundError:
                pass
        log.info("forgot the printer's certificate; it is trusted again on the next connection")

    def stop(self):
        with self._lock:
            self._stopping.set()
            client, self._client = self._client, None
            self.connected = False
        if client:
            client.disconnect()
            client.loop_stop()

    def redact(self, text) -> str:
        """`text` without the access code, or any bblp:<code>@ userinfo."""
        out = USERINFO_RE.sub(f"bblp:{REDACTED}@", str(text))
        if self.access_code:
            out = out.replace(self.access_code, REDACTED)
        return out

    def snapshot(self) -> dict:
        with self._lock:
            summary = self.status.summary()
            return {"connected": self.connected, **summary,
                    "error": None if self.connected else (
                        ERR_CERT if self._cert_changed else
                        self.redact(self._error) if self._error else None)}

    def handle_payload(self, payload: bytes):
        """One raw MQTT message. Oversized, non-JSON and non-object payloads are dropped."""
        try:
            if len(payload) > MAX_PAYLOAD:
                log.warning("ignoring oversized MQTT payload (%d bytes)", len(payload))
                return
            report = json.loads(payload)
        except Exception:
            log.warning("ignoring non-JSON MQTT payload")
            return
        if isinstance(report, dict):
            self.handle_report(report)

    def handle_report(self, report: dict):
        with self._lock:
            event = self.status.apply(report)
        if event and self.on_event:
            try:
                self.on_event(event, self.snapshot())
            except Exception:
                log.exception("printer event handler failed")

    # Commands

    def publish(self, payload: dict):
        with self._lock:
            client, connected = self._client, self.connected
        if client is None or not connected:
            raise RuntimeError("printer not connected")
        info = client.publish(f"device/{self.serial}/request", json.dumps(payload))
        if getattr(info, "rc", 0):
            raise RuntimeError("printer did not accept the command")

    def pause(self):
        self.publish(command_payload("pause"))

    def resume(self):
        self.publish(command_payload("resume"))

    def stop_print(self):
        self.publish(command_payload("stop"))

    def set_light(self, on: bool):
        self.publish(light_payload(bool(on)))

    # paho callbacks (network thread)

    def _set_connection(self, connected: bool, error: str | None = None):
        with self._lock:
            changed = connected != self.connected
            self.connected = connected
            if connected:
                self._cert_changed = False
            self._error = None if connected else (error or self._error)
        if changed:
            log.info("printer MQTT %s", "connected" if connected else "disconnected")

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            text = str(reason_code).lower()
            refused = "not authorized" in text or "bad user" in text or "password" in text
            log.warning("printer refused MQTT connection: %s", self.redact(reason_code))
            self._set_connection(False, "Printer refused the connection (check the access code)"
                                 if refused else "Printer refused the connection")
            return
        client.subscribe(f"device/{self.serial}/report")
        client.publish(f"device/{self.serial}/request", PUSHALL)
        self._set_connection(True)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self._set_connection(False, "Can't reach the printer")
        self._probe_certificate()

    def _on_connect_fail(self, client, userdata):
        log.warning("could not reach the printer at %s", self.redact(self.host))
        self._set_connection(False, "Can't reach the printer")
        self._probe_certificate()

    def _probe_certificate(self):
        """A TLS failure reaches paho as a plain connection failure. Look at what the printer
        presents (off paho's thread, at most every PROBE_GAP seconds) to tell a changed
        certificate from an unreachable printer."""
        with self._lock:
            now = time.monotonic()
            if self._probed is not None and now - self._probed < PROBE_GAP:
                return
            self._probed = now
        threading.Thread(target=self._probe, name="bambu-mqtt-probe", daemon=True).start()

    def _probe(self):
        try:
            with open(self.cert_file, encoding="ascii") as f:
                pinned = ssl.PEM_cert_to_DER_cert(f.read().strip())
            presented = ssl.PEM_cert_to_DER_cert(fetch_pem(self.host))
        except (OSError, ValueError):
            return    # no pin to compare with, or the printer isn't answering: not a certificate problem
        changed = presented != pinned
        with self._lock:
            if self.connected or self._stopping.is_set():
                return
            self._cert_changed = changed
        if changed:
            log.warning("the printer's certificate no longer matches the saved one")

    def _on_message(self, client, userdata, msg):
        self.handle_payload(msg.payload)
