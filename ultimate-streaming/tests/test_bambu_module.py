"""The Bambu plugin's Module (plugins/bambu/module.py), loaded the way the daemon loads it: installed
as a catalog symlink and imported as deck_plugins.bambu. The parts (MQTT watcher, camera relay,
Discord snapshots) are replaced by fakes; nothing touches the network, ffmpeg or OBS."""
from tests import helpers  # noqa: F401

import json
import os
import shutil
import stat
import sys
import threading
import time
import unittest
from unittest import mock

from tests.helpers import ROOT, tmpdir

import yaml  # noqa: E402

from core import chatcmd, module as core_module, paths, plugin_page, plugins  # noqa: E402
from core.bot import DeckBot  # noqa: E402
from core.webapi import ApiError  # noqa: E402
from core.webhooks import Result  # noqa: E402

CODE = "12345678"
WEBHOOK = "https://discord.com/api/webhooks/123456/s3cr3t-token"
WEBHOOK2 = "https://discord.com/api/webhooks/654321/other-token"
SERIAL = "01P00A123456789"


def summary(**kw):
    base = {"state": "IDLE", "active": False, "percent": None, "remaining_min": None, "job": None,
            "layer": None, "total_layers": None, "nozzle_temp": 25.0, "bed_temp": 24.0, "light": "off"}
    base.update(kw)
    return base


PRINTING = dict(state="RUNNING", active=True, percent=58, remaining_min=62, job="Benchy",
                layer=701, total_layers=1058)


class FakeWatcher:
    instances = []

    def __init__(self, host, serial, access_code, on_event=None, *, cert_file):
        self.host, self.serial, self.access_code, self.on_event = host, serial, access_code, on_event
        self.cert_file = cert_file
        self.connected = True
        self.error = None
        self.summary = summary()
        self.calls = []
        FakeWatcher.instances.append(self)

    def start(self):
        self.calls.append("start")

    def stop(self):
        self.calls.append("stop")

    def snapshot(self):
        return {"connected": self.connected, **self.summary,
                "error": None if self.connected else self.redact(self.error or "Can't reach the printer")}

    def redact(self, text):
        return str(text).replace(self.access_code, "<access code>")

    def pause(self):
        self.calls.append("pause")

    def resume(self):
        self.calls.append("resume")

    def stop_print(self):
        self.calls.append("stop_print")

    def set_light(self, on):
        self.calls.append(("light", on))

    def fire(self, event, **fields):
        """Like paho: the callback runs on another thread."""
        self.summary.update(fields)
        t = threading.Thread(target=self.on_event, args=(event, self.snapshot()))
        t.start()
        t.join()


class FakeRelay:
    instances = []

    def __init__(self, host, access_code, port, ffmpeg="ffmpeg", hide_access_code=True, *, serial, cert_file,
                 socket_path=None, link_error=None, on_listening=None, on_stall=None):
        self.host, self.access_code, self.port, self.ffmpeg = host, access_code, port, ffmpeg
        self.socket_path, self.link_error = socket_path, link_error
        self.on_listening, self.on_stall = on_listening, on_stall
        self.is_listening = False
        self.hide_access_code, self.serial, self.cert_file = hide_access_code, serial, cert_file
        self.state, self.error, self.detail = "off", None, None
        self.calls = []
        FakeRelay.instances.append(self)

    def start(self):
        self.calls.append("start")
        self.state, self.error = ("unavailable", self.link_error) if self.link_error else ("live", None)

    def stop(self):
        self.calls.append("stop")
        self.state = "off"

    def forget_certificate(self):
        self.calls.append("forget")

    def listening(self):
        return self.is_listening

    def status(self):
        return {"state": self.state, "error": self.error, "detail": self.detail, "live_seconds": None,
                "port": self.port}

    def redact(self, text):
        return text.replace(self.access_code, "<access code>")


class FakeSnaps:
    instances = []

    def __init__(self, url, **kw):
        self.url, self.kw = url, kw
        self.calls, self.events = [], []
        self.ok = True
        FakeSnaps.instances.append(self)

    def start(self):
        self.calls.append("start")

    def stop(self):
        self.calls.append("stop")

    def on_event(self, event):
        self.events.append(event)

    def post(self, ended=False):
        self.calls.append("post")
        return self.ok

    def status(self):
        return {"printing": False, "every_layers": self.kw["every_layers"], "last_post_ago": None,
                "next_layer": None, "error": None if self.ok else "Couldn't reach Discord."}


class FakeDiscord:
    """Exactly the registry methods plugins may use (contract section 2)."""
    def __init__(self):
        self.urls, self.names, self.reports = {"golive": WEBHOOK2}, {"golive": "Go-live"}, []
        self.report_urls = []

    def webhooks(self):
        return [{"id": i, "name": n, "set": i in self.urls, "host": None, "state": "never", "detail": None}
                for i, n in self.names.items()]

    def webhook_url(self, webhook_id):
        return self.urls.get(webhook_id)

    def name_of(self, webhook_id):
        return self.names.get(webhook_id)

    def report(self, webhook_id, result, url=None):   # modules/discord.py Discord.report
        self.reports.append((webhook_id, result))
        self.report_urls.append(url)

    def is_broken(self, webhook_id):
        return any(i == webhook_id and r.kind == "broken" for i, r in self.reports)


class FakeOBS:
    """Enough of modules/obs.py's OBS for the plugin: request() plus is_request_error()."""
    config_key = "fake_obs"

    def __init__(self, scene="Printer Cam", scenes=("Printer Cam", "Desk")):
        self.scene, self.scenes, self.sent = scene, list(scenes), []

    def on_config(self, config):
        pass

    @staticmethod
    def is_request_error(e):
        return False

    def request(self, name, data=None):
        self.sent.append((name, data))
        if name == "GetCurrentProgramScene":
            return {"sceneName": self.scene}
        if name == "GetSceneList":
            return {"scenes": [{"sceneName": s} for s in self.scenes]}
        if name == "SetCurrentProgramScene":
            self.scene = data["sceneName"]
        return {}


class BambuModule(unittest.TestCase):
    def setUp(self):
        for cls in (FakeWatcher, FakeRelay, FakeSnaps):
            cls.instances = []
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for var in ("BAMBU_ACCESS_CODE", "BAMBU_SERIAL", "BAMBU_DISCORD_WEBHOOK"):
            os.environ.pop(var, None)
        self.runtime = tmpdir()   # the camera socket's folder: never the real $XDG_RUNTIME_DIR
        os.chmod(self.runtime, 0o700)
        os.environ["XDG_RUNTIME_DIR"] = self.runtime
        self.plugins = paths.plugins_dir()
        for entry in os.listdir(self.plugins):
            self._remove(os.path.join(self.plugins, entry))
        os.symlink(os.path.join(ROOT, "plugins", "bambu"), os.path.join(self.plugins, "bambu"))
        shutil.rmtree(paths.overlay_dir(), ignore_errors=True)
        self.dir = tmpdir()
        self.config_path = os.path.join(self.dir, "config.yaml")
        self.mtime = time.time() - 1000
        self._write({})
        self.bot = DeckBot(root=ROOT, config_path=self.config_path)
        self.bot.load_config(initial=True)
        self.bot.package = "modules"
        self.addCleanup(self.cleanup)
        self.bot.load_plugins()
        self.inst = self.bot.modules["Bambu"]
        self.mod = sys.modules["deck_plugins.bambu.module"]
        for name, fake in (("PrinterWatcher", FakeWatcher), ("CameraRelay", FakeRelay),
                           ("DiscordSnapshots", FakeSnaps)):
            p = mock.patch.object(self.mod, name, fake)
            p.start()
            self.addCleanup(p.stop)
        self.notify = mock.patch.object(self.mod, "notify").start()
        self.addCleanup(mock.patch.stopall)

    @staticmethod
    def _remove(path):
        if os.path.islink(path):
            os.unlink(path)
        else:
            shutil.rmtree(path, ignore_errors=True)

    def cleanup(self):
        for name in list(self.bot.extensions):
            self.bot.unload_extension(name)
        for entry in os.listdir(self.plugins):
            self._remove(os.path.join(self.plugins, entry))
        for mod in [m for m in sys.modules if m.startswith(plugins.PACKAGE + ".")]:
            del sys.modules[mod]

    # ---------------------------------------------------------------- helpers

    def _write(self, bambu):
        cfg = {"pages": {"main": []}}
        if bambu is not None:
            cfg["bambu"] = bambu
        with open(self.config_path, "w") as f:
            yaml.safe_dump(cfg, f)
        self.mtime += 10
        os.utime(self.config_path, (self.mtime, self.mtime))

    def secret(self, name, text):
        path = os.path.join(self.dir, name)
        with open(path, "w") as f:
            f.write(text + "\n")
        os.chmod(path, 0o600)
        return path

    def configure(self, **extra):
        """Write a working bambu section and reload; returns the fake watcher."""
        bambu = {"host": "192.0.2.1", "serial": SERIAL,
                 "access_code_file": self.secret("code", CODE), **extra}
        self._write(bambu)
        self.assertTrue(self.bot.load_config())
        self.inst._flush()
        return FakeWatcher.instances[-1] if FakeWatcher.instances else None

    def with_discord(self, **extra):
        return self.configure(discord={"webhook_file": self.secret("hook", WEBHOOK)}, **extra)

    def run_action(self, name, arg=None):
        return self.bot.actions[name](arg)

    def dispatched(self, dispatch):
        return [c.args for c in dispatch.call_args_list if c.args[0].startswith("bambu_")]

    # ---------------------------------------------------------------- not configured

    def test_empty_config_loads_as_not_configured(self):
        self.inst._flush()
        snap = self.inst.snapshot()
        self.assertFalse(snap["configured"])
        self.assertEqual(snap["card"]["tone"], "warn")
        self.assertIn("isn't set up yet", snap["card"]["note"])
        self.assertEqual(self.bot.read_display({"source": "bambu_status"}), {"label": "Bambu\nsetup"})
        self.assertFalse(self.bot.check_state({"bambu": "connected"}))
        for name, arg in (("bambu_pause", None), ("bambu_resume", None), ("bambu_stop", None),
                          ("bambu_light", "on"), ("bambu_camera", "on"), ("bambu_scene", None),
                          ("bambu_setup_obs", None), ("bambu_snapshot", None)):
            with self.assertRaisesRegex(RuntimeError, r"isn't set up yet: set bambu.host"):
                self.run_action(name, arg)
        self.assertEqual(FakeWatcher.instances, [])
        self.assertEqual(FakeRelay.instances, [])

    def test_access_code_in_config_yaml_is_ignored_with_a_warning(self):
        self._write({"host": "192.0.2.1", "serial": SERIAL, "access_code": CODE})
        with self.assertLogs("deck.Bambu", "WARNING") as logs:
            self.bot.load_config()
        self.inst._flush()
        self.assertTrue(any("access_code in config.yaml is ignored" in line for line in logs.output))
        self.assertFalse(any(CODE in line for line in logs.output))
        self.assertFalse(self.inst.snapshot()["configured"])
        self.assertEqual(FakeWatcher.instances, [])
        self.assertNotIn("access_code", self.inst.config)

    def test_invalid_values_fall_back_to_defaults(self):
        with self.assertLogs("deck.Bambu", "WARNING") as logs:
            self.configure(camera={"port": 80, "enabled": "yes", "hide_access_code": "no"},
                           discord={"every_layers": 0, "min_gap": 1})
        s = self.inst.settings
        self.assertEqual(s.camera["port"], 5004)
        self.assertIs(s.camera["enabled"], True)
        self.assertIs(s.camera["hide_access_code"], True)
        self.assertTrue(any("bambu.camera.hide_access_code" in line for line in logs.output))
        self.assertEqual((s.discord["every_layers"], s.discord["min_gap"]), (10, 15))
        self.assertTrue(any("bambu.camera.port" in line for line in logs.output))

    # ---------------------------------------------------------------- parts

    def test_config_and_secret_file_start_the_watcher_and_camera(self):
        w = self.configure()
        self.assertEqual((w.host, w.serial, w.access_code), ("192.0.2.1", SERIAL, CODE))
        self.assertEqual(w.calls, ["start"])
        relay = FakeRelay.instances[-1]
        self.assertEqual((relay.port, relay.calls, relay.hide_access_code), (5004, ["start"], True))
        self.assertEqual(FakeSnaps.instances, [])   # no webhook: Discord off
        self.assertTrue(self.inst.snapshot()["configured"])
        self.assertTrue(self.bot.check_state({"bambu": "connected"}))
        self.assertTrue(self.bot.check_state({"bambu": "camera"}))

    def test_env_serial_and_code_work_without_files(self):
        os.environ.update({"BAMBU_SERIAL": SERIAL, "BAMBU_ACCESS_CODE": CODE})
        self._write({"host": "192.0.2.1"})
        self.bot.load_config()
        self.inst._flush()
        self.assertEqual(FakeWatcher.instances[-1].access_code, CODE)

    def test_only_changed_parts_restart(self):
        w = self.with_discord()
        relay, snaps = FakeRelay.instances[-1], FakeSnaps.instances[-1]
        self.assertEqual(snaps.url, WEBHOOK)
        self._write({**self.inst.config, "camera": {"port": 5010}})
        self.bot.load_config()
        self.inst._flush()
        self.assertEqual(w.calls, ["start"])
        self.assertEqual(snaps.calls, ["start"])
        self.assertEqual(relay.calls, ["start", "stop"])
        self.assertEqual(FakeRelay.instances[-1].port, 5010)
        self._write({**self.inst.config, "discord": {**self.inst.config["discord"], "min_gap": 30},
                     "name": "Alice"})
        self.bot.load_config()
        self.inst._flush()
        self.assertEqual(w.calls, ["start"])
        self.assertEqual(snaps.calls, ["start", "stop"])
        self.assertEqual(FakeSnaps.instances[-1].kw["min_gap"], 30)
        self.assertEqual(FakeRelay.instances[-1].calls, ["start"])
        relay = FakeRelay.instances[-1]
        self._write({**self.inst.config, "camera": {"port": 5010, "hide_access_code": False}})
        self.bot.load_config()
        self.inst._flush()
        self.assertEqual(relay.calls, ["start", "stop"])
        self.assertIs(FakeRelay.instances[-1].hide_access_code, False)
        self.assertEqual(w.calls, ["start"])

    def test_serial_change_restarts_the_relay_and_watcher(self):
        w = self.configure()
        relay = FakeRelay.instances[-1]
        self.assertEqual(w.cert_file, os.path.join(os.environ["DECK_STATE_DIR"], f"bambu-mqtt-{SERIAL}.pem"))
        self._write({**self.inst.config, "serial": "01P00A999999999"})
        self.bot.load_config()
        self.inst._flush()
        self.assertEqual(relay.calls, ["start", "stop"])
        new = FakeRelay.instances[-1]
        self.assertIsNot(new, relay)
        self.assertEqual(new.serial, "01P00A999999999")
        self.assertEqual(new.calls, ["start"])
        self.assertEqual(w.calls, ["start", "stop"])
        self.assertTrue(FakeWatcher.instances[-1].cert_file.endswith("bambu-mqtt-01P00A999999999.pem"))

    def sock(self, port):
        return os.path.join(self.runtime, "ultimate-streaming", f"bambu-camera-{port}.sock")

    def with_obs(self, obs):
        return mock.patch.object(self.bot, "get_module", lambda name: obs if name == "OBS" else None)

    def test_port_change_updates_the_obs_source_url(self):
        self.configure()
        obs = FakeOBS(scene="Gameplay")
        with self.with_obs(obs), \
                mock.patch.object(self.mod.obs_scene, "apply_url", return_value=True) as apply:
            self._write({**self.inst.config, "camera": {"port": 5010}})
            self.bot.load_config()
            self.inst._flush()
            apply.assert_called_once_with(obs, "Printer Camera", f"unix:{self.sock(5010)}")
            self.assertEqual(FakeRelay.instances[-1].socket_path, self.sock(5010))
        # OBS not connected: nothing raised, and the next Set up OBS scene applies it.
        with mock.patch.object(self.bot, "get_module", lambda name: None), \
                mock.patch.object(self.mod.obs_scene, "apply_url") as apply:
            self._write({**self.inst.config, "camera": {"port": 5011}})
            self.bot.load_config()
            self.inst._flush()
            apply.assert_not_called()

    def test_socket_is_the_default_transport(self):
        self.configure()
        self.assertEqual(self.inst.settings.camera["transport"], "socket")
        relay = FakeRelay.instances[-1]
        self.assertEqual(relay.socket_path, self.sock(5004))
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(relay.socket_path)).st_mode), 0o700)
        self.assertIsNone(self.inst.snapshot()["card"]["note"])
        with self.assertLogs("deck.Bambu", "WARNING") as logs:
            self.configure(camera={"transport": "tcp"})
        self.assertTrue(any("bambu.camera.transport must be one of socket, udp" in line for line in logs.output))
        self.assertEqual(self.inst.settings.camera["transport"], "socket")

    def test_start_points_obs_at_the_socket(self):
        """An upgrade from UDP: the OBS source still reads UDP when the plugin starts; it is
        repointed at the socket without anyone pressing anything (OBS connected)."""
        self.configure()
        obs = FakeOBS(scene="Gameplay")
        with self.with_obs(obs), \
                mock.patch.object(self.mod.obs_scene, "apply_url", return_value=True) as apply:
            self.inst._apply(None, self.inst.settings)    # what on_load does
            self.inst._flush()
            apply.assert_called_once_with(obs, "Printer Camera", f"unix:{self.sock(5004)}")
        # Not configured yet: nothing to point at, and no OBS call.
        with self.with_obs(obs), mock.patch.object(self.mod.obs_scene, "apply_url") as apply:
            self.inst._apply(None, self.mod.Settings())
            self.inst._flush()
            apply.assert_not_called()

    def test_transport_change_updates_the_obs_source_url(self):
        self.configure()
        obs = FakeOBS(scene="Gameplay")
        with self.with_obs(obs), \
                mock.patch.object(self.mod.obs_scene, "apply_url", return_value=True) as apply:
            self._write({**self.inst.config, "camera": {"transport": "udp"}})
            self.bot.load_config()
            self.inst._flush()
            apply.assert_called_once()
            self.assertEqual(apply.call_args.args[:2], (obs, "Printer Camera"))
            self.assertTrue(apply.call_args.args[2].startswith("udp://127.0.0.1:5004?localaddr=127.0.0.1"))
            relay = FakeRelay.instances[-1]
            self.assertIsNone(relay.socket_path)
            self.assertEqual(relay.calls, ["start"])
            apply.reset_mock()
            self._write({**self.inst.config, "camera": {"transport": "socket"}})
            self.bot.load_config()
            self.inst._flush()
            apply.assert_called_once_with(obs, "Printer Camera", f"unix:{self.sock(5004)}")
            self.assertEqual(relay.calls, ["start", "stop"])
            self.assertEqual(FakeRelay.instances[-1].socket_path, self.sock(5004))
        # udp chosen on purpose: no warning and no note.
        with self.assertNoLogs("deck", "WARNING"):
            self.configure(camera={"transport": "udp"})
        self.assertIsNone(self.inst.snapshot()["card"]["note"])

    def test_flatpak_obs_uses_udp_and_says_so(self):
        with mock.patch.object(self.mod, "_obs_is_flatpak", return_value=True), \
                self.assertLogs("deck", "WARNING") as logs:
            self.configure()
        warned = [line for line in logs.output if "Flatpak" in line]
        self.assertEqual(len(warned), 1, logs.output)
        self.assertIsNone(FakeRelay.instances[-1].socket_path)
        self.assertEqual(self.inst.snapshot()["card"]["note"],
                         "The camera uses UDP because OBS is a Flatpak; other local users could inject video.")
        obs = FakeOBS(scene="Gameplay")
        with self.with_obs(obs), mock.patch.object(self.mod.obs_scene, "ensure_camera",
                                                   return_value={"created": [], "updated": []}) as ensure:
            self.run_action("bambu_setup_obs")
        self.assertTrue(ensure.call_args.kwargs["url"].startswith("udp://127.0.0.1:5004?"))
        # The line joins a more important note rather than hiding it.
        relay = FakeRelay.instances[-1]
        relay.state, relay.error = "retrying", "ffmpeg stopped unexpectedly."
        self.assertEqual(self.inst.snapshot()["card"]["note"],
                         "ffmpeg stopped unexpectedly. The camera uses UDP because OBS is a Flatpak; "
                         "other local users could inject video.")
        # Camera off: no line.
        with mock.patch.object(self.mod, "_obs_is_flatpak", return_value=True):
            self.configure(camera={"enabled": False})
        self.assertIsNone(self.inst.snapshot()["card"]["note"])

    def test_unsafe_socket_folder_keeps_the_camera_off(self):
        ours = os.path.join(self.runtime, "ultimate-streaming")
        os.symlink(tmpdir(), ours)
        obs = FakeOBS(scene="Gameplay")
        with self.with_obs(obs), mock.patch.object(self.mod.obs_scene, "apply_url") as apply, \
                self.assertLogs("deck", "WARNING") as logs:
            self.configure()
            apply.assert_not_called()                     # OBS keeps reading what it had
        error = (f"The camera's private folder ({ours}) isn't safe: it is a symlink. Fix its owner "
                 "or permissions; the camera stays off until then.")
        self.assertTrue(any(error in line for line in logs.output), logs.output)
        self.assertFalse(any("UDP" in line for line in logs.output), logs.output)
        relay = FakeRelay.instances[-1]
        self.assertEqual((relay.socket_path, relay.state, relay.error), (None, "unavailable", error))
        card = self.inst.snapshot()["card"]
        self.assertEqual((card["note"], dict(card["rows"])["Camera"]), (error, "Unavailable"))
        with self.with_obs(obs), mock.patch.object(self.mod.obs_scene, "ensure_camera") as ensure:
            with self.assertRaisesRegex(RuntimeError, "isn't safe: it is a symlink"):
                self.run_action("bambu_setup_obs")
            ensure.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, "isn't safe"):
            self.run_action("bambu_camera", "on")
        # Fixed: starting the camera looks at the folder again.
        os.unlink(ours)
        self.run_action("bambu_camera", "on")
        relay = FakeRelay.instances[-1]
        self.assertEqual((relay.socket_path, relay.state), (self.sock(5004), "live"))
        self.assertIsNone(self.inst.snapshot()["card"]["note"])

    def test_listening_relay_restarts_the_obs_source(self):
        self.configure()
        relay = FakeRelay.instances[-1]
        self.assertEqual((relay.on_listening, relay.on_stall),
                         (self.inst._relay_listening, self.inst._relay_stalled))
        obs = FakeOBS(scene="Gameplay")
        with self.with_obs(obs), \
                mock.patch.object(self.mod.obs_scene, "restart_media", return_value=True) as restart:
            relay.on_listening(relay)            # from the relay's thread: only queued
            self.inst._flush()
            restart.assert_called_once_with(obs, "Printer Camera")
            relay.on_stall(relay)
            self.inst._flush()
            self.assertEqual(restart.call_count, 2)
            old = relay
            self.configure(camera={"port": 5010})
            old.on_listening(old)                # a stopped relay's news is dropped
            self.inst._flush()
            self.assertEqual(restart.call_count, 2)
        # OBS not connected: swallowed, like _apply_url.
        with mock.patch.object(self.mod.obs_scene, "restart_media") as restart, \
                self.assertLogs("deck.Bambu", "INFO") as logs:
            relay = FakeRelay.instances[-1]
            relay.on_listening(relay)
            self.inst._flush()
            restart.assert_not_called()
        self.assertTrue(any("not restarted" in line and "OBS isn't reachable" in line
                            for line in logs.output), logs.output)

    def test_check_restarts_a_given_up_obs_source_at_most_every_10_s(self):
        self.configure()
        relay = FakeRelay.instances[-1]
        obs = FakeOBS(scene="Gameplay")
        now = [1000.0]
        clock = mock.patch.object(self.mod.time, "monotonic", lambda: now[0])
        with self.with_obs(obs), clock, \
                mock.patch.object(self.mod.obs_scene, "media_state") as state, \
                mock.patch.object(self.mod.obs_scene, "restart_media", return_value=True) as restart:
            def check():
                self.inst.check_obs_camera()
                self.inst._flush()

            check()                              # not listening (UDP, or ffmpeg not up): no look
            state.assert_not_called()
            relay.is_listening = True
            state.return_value = "OBS_MEDIA_STATE_PLAYING"
            check()
            state.assert_called_once_with(obs, "Printer Camera")
            restart.assert_not_called()
            for dead in ("OBS_MEDIA_STATE_ENDED", "OBS_MEDIA_STATE_ERROR", "OBS_MEDIA_STATE_NONE"):
                with self.subTest(dead=dead):
                    state.reset_mock()
                    restart.reset_mock()
                    state.return_value = dead
                    check()
                    restart.assert_called_once_with(obs, "Printer Camera")
                    now[0] += 5
                    check()                      # 5 s later: still inside the 10 s gap
                    state.assert_called_once()
                    restart.assert_called_once()
                    now[0] += 5
            state.reset_mock()
            state.return_value = None            # the source isn't in OBS: nothing to restart
            check()
            state.assert_called_once()
            self.assertEqual(restart.call_count, 1)
        # OBS not connected: nothing raised, nothing logged, and the next look still happens.
        with clock, self.assertNoLogs("deck.Bambu", "INFO"):
            now[0] += 20
            self.inst.check_obs_camera()
            self.inst._flush()
        self.assertFalse(self.inst._media_check_pending)

    def test_obs_is_flatpak(self):
        home = tmpdir()   # a fresh home of its own, inside the test sandbox
        os.environ.update({"HOME": home, "XDG_CONFIG_HOME": os.path.join(home, ".config")})
        flat = os.path.join(home, ".var", "app", "com.obsproject.Studio")
        native = os.path.join(home, ".config", "obs-studio")
        is_flatpak = self.mod._obs_is_flatpak
        with mock.patch.object(self.mod.detect, "_running") as procs:
            self.assertFalse(is_flatpak())                        # no OBS at all: native
            os.makedirs(flat)
            self.assertTrue(is_flatpak())                         # only the Flatpak
            procs.assert_not_called()
            os.makedirs(native)
            os.environ["OBS_CONFIG_DIR"] = os.path.join(flat, "config", "obs-studio")
            self.assertTrue(is_flatpak())                         # OBS_CONFIG_DIR decides
            os.environ["OBS_CONFIG_DIR"] = native
            self.assertFalse(is_flatpak())
            del os.environ["OBS_CONFIG_DIR"]
            procs.return_value = {"obs", "obs:flatpak"}
            self.assertTrue(is_flatpak())                         # both installed: the running one
            procs.assert_called_with("/proc", True)
            procs.return_value = {"obs", "obs:native"}
            self.assertFalse(is_flatpak())
            procs.return_value = set()                            # neither running: newest settings
            os.makedirs(os.path.join(flat, "config", "obs-studio"))
            for root, when in ((native, 1000), (os.path.join(flat, "config", "obs-studio"), 2000)):
                with open(os.path.join(root, "user.ini"), "w"):
                    pass
                os.utime(os.path.join(root, "user.ini"), (when, when))
            self.assertTrue(is_flatpak())
            os.utime(os.path.join(native, "user.ini"), (3000, 3000))
            self.assertFalse(is_flatpak())
            procs.side_effect = OSError("no /proc")
            self.assertFalse(is_flatpak())                        # only an error: keep the socket

    def test_trust_forgets_the_mqtt_pin_too(self):
        w = self.configure()
        pin = w.cert_file
        with open(pin, "w") as f:
            f.write("old pin")
        self.run_action("bambu_camera", "trust")
        self.assertFalse(os.path.exists(pin))
        self.assertEqual(w.calls, ["start", "stop"])
        new = FakeWatcher.instances[-1]
        self.assertIsNot(new, w)
        self.assertEqual(new.calls, ["start"])
        self.assertIs(self.inst._watcher, new)

    def test_part_start_runs_on_the_control_worker(self):
        threads = []
        orig = FakeWatcher.start

        def start(watcher):
            threads.append(threading.current_thread().name)
            orig(watcher)
        with mock.patch.object(FakeWatcher, "start", start):
            self.configure()
        self.assertEqual(threads, ["bambu-control"])

    def test_unload_stops_every_part_and_the_chat_builtin(self):
        w = self.with_discord()
        relay, snaps = FakeRelay.instances[-1], FakeSnaps.instances[-1]
        worker = self.inst._worker
        self.assertIsNotNone(chatcmd.get("print"))
        self.bot.unload_extension("plugin:bambu")
        self.assertIn("stop", w.calls)
        self.assertIn("stop", relay.calls)
        self.assertIn("stop", snaps.calls)
        self.assertIsNone(chatcmd.get("print"))
        self.assertFalse(worker.is_alive())
        self.assertNotIn("bambu_pause", self.bot.actions)

    # ---------------------------------------------------------------- events

    def test_events_are_dispatched_on_the_main_loop_task(self):
        w = self.with_discord()
        snaps = FakeSnaps.instances[-1]
        with mock.patch.object(self.bot, "dispatch", wraps=self.bot.dispatch) as dispatch:
            w.fire("started", **PRINTING)
            self.assertEqual(self.dispatched(dispatch), [])   # paho's thread only queued it
            self.inst.drain_events()
            calls = self.dispatched(dispatch)
            self.assertEqual([c[0] for c in calls], ["bambu_print_started"])
            self.assertEqual(calls[0][1]["percent"], 58)
            w.fire("ended", state="FINISH", active=False)
            self.inst.drain_events()
            self.assertEqual([c[0] for c in self.dispatched(dispatch)],
                             ["bambu_print_started", "bambu_print_ended"])
        self.assertEqual(snaps.events, ["started", "ended"])

    def test_tasks_are_registered_without_page_change_runs(self):
        tasks = {t[0].__name__: t for t in self.bot.tasks if getattr(t[0], "__self__", None) is self.inst}
        self.assertEqual(tasks["drain_events"][1], 0.5)
        self.assertEqual(tasks["write_overlay"][1], 1.0)
        self.assertFalse(tasks["drain_events"][0].__deck_task_on_page__)
        self.assertFalse(tasks["write_overlay"][0].__deck_task_on_page__)

    def _end(self, w, obs, after=0, active_again=False):
        self.bot.modules["FakeOBS"] = obs
        self.addCleanup(self.bot.modules.pop, "FakeOBS", None)
        w.summary.update(PRINTING)
        w.fire("ended", state="FINISH", active=False)
        if active_again:
            w.summary.update(PRINTING)
        self.inst.drain_events()
        self.inst._flush()

    def test_print_end_switches_scene_when_obs_shows_the_camera(self):
        w = self.configure(on_print_end={"scene": "Desk", "after": 0})
        obs = FakeOBS(scene="Printer Cam")
        with mock.patch.object(self.bot, "get_module", lambda name: obs if name == "OBS" else None):
            self._end(w, obs)
        self.assertEqual(obs.scene, "Desk")

    def test_print_end_stays_when_obs_shows_another_scene(self):
        w = self.configure(on_print_end={"scene": "Desk", "after": 0})
        obs = FakeOBS(scene="Gameplay")
        with mock.patch.object(self.bot, "get_module", lambda name: obs if name == "OBS" else None):
            self._end(w, obs)
        self.assertEqual(obs.scene, "Gameplay")
        self.assertNotIn("SetCurrentProgramScene", [n for n, _ in obs.sent])

    def test_print_end_stays_when_a_print_is_active_again(self):
        w = self.configure(on_print_end={"scene": "Desk", "after": 0})
        obs = FakeOBS(scene="Printer Cam")
        with mock.patch.object(self.bot, "get_module", lambda name: obs if name == "OBS" else None):
            self._end(w, obs, active_again=True)
        self.assertEqual(obs.scene, "Printer Cam")

    def test_new_print_cancels_the_pending_switch(self):
        w = self.configure(on_print_end={"scene": "Desk", "after": 60})
        w.fire("ended", state="FINISH", active=False)
        self.inst.drain_events()
        self.assertIsNotNone(self.inst._end_at)
        w.fire("started", **PRINTING)
        self.inst.drain_events()
        self.assertIsNone(self.inst._end_at)

    def test_no_switch_without_an_end_scene(self):
        w = self.configure()
        w.fire("ended", state="FINISH", active=False)
        self.inst.drain_events()
        self.assertIsNone(self.inst._end_at)

    # ---------------------------------------------------------------- actions

    def test_stop_needs_two_presses_within_three_seconds(self):
        w = self.configure()
        w.summary.update(PRINTING)
        self.inst.drain_events()
        self.run_action("bambu_stop")
        self.notify.assert_called_once_with("Press again within 3 s to stop the print on Printer")
        self.assertNotIn("stop_print", w.calls)
        self.run_action("bambu_stop")
        self.assertEqual(w.calls.count("stop_print"), 1)
        # Too slow: the second press only arms again.
        self.run_action("bambu_stop")
        deadline, armed = self.inst._stop_pending
        self.inst._stop_pending = (time.monotonic() - 0.1, armed)
        self.run_action("bambu_stop")
        self.assertEqual(w.calls.count("stop_print"), 1)
        self.assertEqual(self.notify.call_count, 3)

    def test_a_state_change_clears_the_stop_confirmation(self):
        w = self.configure()
        w.summary.update(PRINTING)
        self.inst.drain_events()
        self.run_action("bambu_stop")
        w.summary.update(state="PAUSE")
        self.inst.drain_events()
        self.assertIsNone(self.inst._stop_pending)
        self.run_action("bambu_stop")
        self.assertNotIn("stop_print", w.calls)
        # Changed between presses, before the task saw it: still not confirmed.
        w.summary.update(state="RUNNING")
        self.run_action("bambu_stop")
        self.assertNotIn("stop_print", w.calls)
        # A printer event clears it too.
        w.fire("ended", state="FINISH", active=False)
        self.inst.drain_events()
        self.assertIsNone(self.inst._stop_pending)

    def test_real_catalog_plugin_loads_and_denies_rewards(self):
        """The repo's plugins/bambu, installed as a catalog symlink and imported by load_plugins()
        itself: the module registers and its decorators mark pause and stop as never-in-a-reward."""
        self.bot.unload_extension("plugin:bambu")
        self.assertNotIn("Bambu", self.bot.modules)
        self.assertNotIn("deck_plugins.bambu.module", sys.modules)
        with mock.patch.object(core_module, "REWARD_DENIED", set()) as denied:
            self.bot.load_plugins()
            self.assertTrue({"bambu_pause", "bambu_stop"} <= denied)
            self.assertNotIn("bambu_resume", denied)
        ext = self.bot.extensions["plugin:bambu"]
        self.assertEqual(os.path.realpath(ext["path"]), os.path.realpath(os.path.join(ROOT, "plugins", "bambu")))
        inst = self.bot.modules["Bambu"]
        self.assertEqual(type(inst).__module__, "deck_plugins.bambu.module")
        self.assertEqual(inst.config_key, "bambu")
        self.assertIn("bambu_pause", self.bot.actions)
        self.assertIn("bambu_stop", self.bot.actions)
        self.assertTrue({"bambu_pause", "bambu_stop"} <= core_module.REWARD_DENIED)

    def test_commands_need_a_connected_printer(self):
        w = self.configure()
        self.run_action("bambu_pause")
        self.run_action("bambu_resume")
        self.run_action("bambu_light", "toggle")
        self.assertEqual(w.calls[1:], ["pause", "resume", ("light", True)])
        w.connected = False
        with self.assertRaisesRegex(RuntimeError, "isn't connected"):
            self.run_action("bambu_pause")

    def test_camera_action_starts_and_stops_the_relay(self):
        self.configure()
        relay = FakeRelay.instances[-1]
        self.run_action("bambu_camera", "off")
        self.assertEqual(relay.calls, ["start", "stop"])
        self.assertIsNone(self.inst.snapshot()["camera"])
        self.run_action("bambu_camera", "toggle")
        self.assertEqual(FakeRelay.instances[-1].state, "live")
        with mock.patch.object(FakeRelay, "start", lambda r: setattr(r, "state", "unavailable")
                               or setattr(r, "error", "ffmpeg isn't installed")):
            self.run_action("bambu_camera", "off")
            with self.assertRaisesRegex(RuntimeError, "ffmpeg isn't installed"):
                self.run_action("bambu_camera", "on")

    def test_relay_pins_per_serial(self):
        self.configure()
        relay = FakeRelay.instances[-1]
        self.assertEqual(relay.serial, self.inst.settings.serial)
        self.assertEqual(relay.cert_file, os.path.join(os.environ["DECK_STATE_DIR"],
                                                       f"bambu-camera-{relay.serial}.pem"))

    def test_camera_trust_forgets_and_restarts(self):
        self.configure()
        old = FakeRelay.instances[-1]
        self.run_action("bambu_camera", "trust")
        new = FakeRelay.instances[-1]
        self.assertIsNot(new, old)
        self.assertEqual(old.calls, ["start", "stop"])   # stopped before the forget
        self.assertEqual(new.calls, ["forget", "start"])
        self.assertIs(self.inst._relay, new)
        # With the camera switched off, trust only forgets.
        self.run_action("bambu_camera", "off")
        self.run_action("bambu_camera", "trust")
        self.assertEqual(FakeRelay.instances[-1].calls, ["forget"])
        self.assertIsNone(self.inst._relay)
        self.assertIsNone(self.inst.snapshot()["camera"])
        with self.assertRaisesRegex(ValueError, "on, off, toggle or trust"):
            self.run_action("bambu_camera", "maybe")
        # The editor's dropdown offers exactly what the action takes.
        self.assertEqual([o["value"] for o in self.mod.Bambu.camera.__deck_ui__["arg"]["options"]],
                         ["on", "off", "toggle", "trust"])

    def test_panel_trust_camera(self):
        self.configure()
        self.assertEqual(self.inst.panel_action("trust_camera"),
                         {"message": "The printer's certificates will be trusted on the next connection"})
        self.assertEqual(FakeRelay.instances[-1].calls, ["forget", "start"])

    def test_obs_actions(self):
        self.configure()
        with self.assertRaisesRegex(RuntimeError, "OBS isn't reachable"):
            self.run_action("bambu_scene")
        self.assertEqual(self.inst.settings.camera["source"], "Printer Camera")   # the default
        obs = FakeOBS(scene="Gameplay")
        with mock.patch.object(self.bot, "get_module", lambda name: obs if name == "OBS" else None), \
                mock.patch.object(self.mod.obs_scene, "ensure_camera",
                                  return_value={"created": [], "updated": []}) as ensure:
            self.run_action("bambu_scene")
            self.assertEqual(obs.scene, "Printer Cam")
            ensure.assert_not_called()           # the scene already exists
            self.run_action("bambu_setup_obs")
            kw = ensure.call_args.kwargs
            self.assertEqual((kw["scene"], kw["source"], kw["text_source"]),
                             ("Printer Cam", "Printer Camera", "Printer Status"))
            self.assertEqual(kw["url"], f"unix:{self.sock(5004)}")
            self.assertTrue(kw["text_file"].endswith(os.path.join("overlay", "bambu.txt")))
            ensure.side_effect = ConnectionRefusedError("refused")
            with self.assertRaisesRegex(RuntimeError, "OBS isn't reachable"):
                self.run_action("bambu_setup_obs")

    def test_crop_setting_passed_to_obs_and_applied_on_change(self):
        self.configure(camera={"crop_right": 384})
        self.assertEqual((self.inst.settings.camera["crop_left"], self.inst.settings.camera["crop_right"]), (0, 384))
        obs = FakeOBS(scene="Gameplay")
        with mock.patch.object(self.bot, "get_module", lambda name: obs if name == "OBS" else None), \
                mock.patch.object(self.mod.obs_scene, "ensure_camera",
                                  return_value={"created": [], "updated": []}) as ensure, \
                mock.patch.object(self.mod.obs_scene, "apply_crop", return_value=True) as apply:
            self.run_action("bambu_setup_obs")
            self.assertEqual((ensure.call_args.kwargs["crop_left"], ensure.call_args.kwargs["crop_right"]), (0, 384))
            relay = FakeRelay.instances[-1]
            self._write({**self.inst.config, "camera": {"crop_right": 400}})
            self.bot.load_config()
            self.inst._flush()
            apply.assert_called_once_with(obs, "Printer Camera", 0, 400)
            self.assertEqual(relay.calls, ["start"])          # a crop change doesn't restart the relay
        with self.assertLogs("deck.Bambu", "WARNING"):
            self.configure(camera={"crop_right": -5})
        self.assertEqual(self.inst.settings.camera["crop_right"], 0)

    def test_same_scene_and_source_name_is_refused(self):
        # OBS scenes and sources share one namespace: the same name for both can't work.
        with self.assertLogs("deck.Bambu", "WARNING") as logs:
            self.configure(camera={"source": "Printer Cam"})
        self.assertTrue(any("different names" in line for line in logs.output))
        obs = FakeOBS(scene="Gameplay")
        with mock.patch.object(self.bot, "get_module", lambda name: obs if name == "OBS" else None):
            with self.assertRaisesRegex(RuntimeError, "need different names"):
                self.run_action("bambu_setup_obs")
        self.assertEqual(obs.sent, [])

    def test_snapshot_action(self):
        self.configure()
        with self.assertRaisesRegex(RuntimeError, "Discord snapshots are off"):
            self.run_action("bambu_snapshot")
        self.with_discord()
        snaps = FakeSnaps.instances[-1]
        self.run_action("bambu_snapshot")
        self.assertEqual(snaps.calls, ["start", "post"])
        snaps.ok = False
        with self.assertRaisesRegex(RuntimeError, "Couldn't reach Discord"):
            self.run_action("bambu_snapshot")

    # ---------------------------------------------------------------- Discord registry

    def registry(self):
        d = FakeDiscord()
        self.bot.modules["Discord"] = d
        self.addCleanup(self.bot.modules.pop, "Discord", None)
        return d

    def test_registry_webhook_is_used(self):
        d = self.registry()
        self.configure(discord={"webhook": "golive"})
        snaps = FakeSnaps.instances[-1]
        self.assertEqual(snaps.url, WEBHOOK2)
        self.assertEqual(self.inst.discord_usage(), [("golive", "Bambu Printer: snapshots")])
        self.assertEqual(d.reports, [])

    def test_registry_wins_over_the_old_file(self):
        self.registry()
        self.configure(discord={"webhook": "golive", "webhook_file": self.secret("hook", WEBHOOK)})
        self.assertEqual(FakeSnaps.instances[-1].url, WEBHOOK2)

    def test_falls_back_to_the_old_file_when_the_registry_has_no_url(self):
        """Only while no webhook is picked: discord.webhook is ""."""
        d = self.registry()
        d.urls.clear()
        for pick in ({}, {"webhook": ""}, {"webhook": None}):
            self.configure(discord={**pick, "webhook_file": self.secret("hook", WEBHOOK)})
            snaps = FakeSnaps.instances[-1]
            self.assertEqual(snaps.url, WEBHOOK)
            self.assertNotIn("post", snaps.kw)   # fallback path is untouched: no reporting
            self.assertEqual(self.inst.settings.webhook_id, None)
            self.assertIsNone(self.inst.settings.webhook_problem)
            self.assertEqual(self.inst.discord_usage(), [])

    def test_falls_back_to_the_old_file_without_a_pick(self):
        self.configure(discord={"webhook_file": self.secret("hook", WEBHOOK)})
        self.assertEqual(FakeSnaps.instances[-1].url, WEBHOOK)
        self.registry()
        # another file with the same URL: an identical config.yaml isn't applied again
        self.configure(discord={"webhook_file": self.secret("hook2", WEBHOOK)})
        self.assertEqual(FakeSnaps.instances[-1].url, WEBHOOK)
        self.assertEqual(self.inst.discord_usage(), [])

    def test_a_picked_but_broken_webhook_never_switches_to_the_old_file(self):
        d = self.registry()
        d.report("golive", Result("broken", "gone"))
        d.urls.clear()                           # the registry hands out no URL for a broken webhook
        w = self.configure(discord={"webhook": "golive", "webhook_file": self.secret("hook", WEBHOOK)})
        self.inst._flush()
        self.assertEqual(FakeSnaps.instances, [])
        problem = "The Go-live webhook is broken or has no URL; fix it in Settings → Discord"
        self.assertEqual(self.inst.settings.webhook_problem, problem)
        self.assertEqual(self.inst.discord_usage(), [("golive", "Bambu Printer: snapshots")])
        w.summary.update(PRINTING)
        card = self.inst.snapshot()["card"]
        self.assertEqual((card["note"], card["tone"]), (problem, "warn"))
        self.assertEqual(dict(card["rows"])["Discord"], "Off")
        with self.assertRaisesRegex(RuntimeError, "Go-live webhook is broken or has no URL"):
            self.run_action("bambu_snapshot")

    def test_a_picked_webhook_the_registry_doesnt_know_is_off_and_named_by_id(self):
        self.configure(discord={"webhook": "golive", "webhook_file": self.secret("hook", WEBHOOK)})
        self.assertEqual(FakeSnaps.instances, [])   # no Discord module at all
        self.assertEqual(self.inst.settings.webhook_problem,
                         "The golive webhook is broken or has no URL; fix it in Settings → Discord")
        d = self.registry()
        self.configure(discord={"webhook": "printer", "webhook_file": self.secret("hook", WEBHOOK)})
        self.assertEqual(FakeSnaps.instances, [])
        self.assertEqual(self.inst.settings.webhook_problem,
                         "The printer webhook is broken or has no URL; fix it in Settings → Discord")
        d.name_of = mock.Mock(side_effect=RuntimeError("down"))
        self.configure(discord={"webhook": "golive"})
        self.assertEqual(FakeSnaps.instances[-1].url, WEBHOOK2)   # a name lookup failure is harmless

    def test_picked_webhook_without_a_url_and_no_fallback_is_off(self):
        d = self.registry()
        d.urls.clear()
        self.configure(discord={"webhook": "golive"})
        self.assertEqual(FakeSnaps.instances, [])
        self.assertIn("broken or has no URL", self.inst.settings.webhook_problem)
        with self.assertRaisesRegex(RuntimeError, "broken or has no URL"):
            self.run_action("bambu_snapshot")

    def test_a_pasted_url_in_discord_webhook_is_ignored_and_not_kept(self):
        self.registry()
        with self.assertLogs("deck.Bambu", "WARNING") as logs:
            self.configure(discord={"webhook": WEBHOOK})
        self.assertEqual(FakeSnaps.instances, [])
        self.assertNotIn(WEBHOOK, "\n".join(logs.output))
        self.assertNotIn("webhook", self.inst.config.get("discord", {}))
        self.assertEqual(self.inst.discord_usage(), [])

    def test_replacing_the_url_in_the_registry_restarts_snapshots(self):
        d = self.registry()
        self.configure(discord={"webhook": "golive"})
        first = FakeSnaps.instances[-1]
        self.inst._check_registry(force=True)
        self.inst._flush()
        self.assertEqual(len(FakeSnaps.instances), 1)   # nothing changed: no restart
        d.urls["golive"] = WEBHOOK
        self.inst._check_registry(force=True)
        self.inst._flush()
        self.assertEqual(first.calls, ["start", "stop"])
        self.assertEqual(FakeSnaps.instances[-1].url, WEBHOOK)
        d.urls.clear()   # broken or removed: snapshots stop
        self.inst._check_registry(force=True)
        self.inst._flush()
        self.assertEqual(FakeSnaps.instances[-1].calls, ["start", "stop"])
        self.assertIsNone(self.inst._snaps)

    def test_registry_check_is_throttled(self):
        d = self.registry()
        self.configure(discord={"webhook": "golive"})
        self.inst._check_registry(force=True)
        d.urls["golive"] = WEBHOOK
        self.inst._check_registry()   # too soon
        self.inst._flush()
        self.assertEqual(len(FakeSnaps.instances), 1)

    def test_results_are_reported_to_the_registry(self):
        d = self.registry()
        self.configure(discord={"webhook": "golive"})
        post = FakeSnaps.instances[-1].kw["post"]
        seen = {}

        def fake(code=None, exc=None):
            def http(url, **kw):
                seen.update(url=url, **kw)
                if exc:
                    raise exc
                return mock.Mock(status_code=code)
            return http

        for code, kind in ((204, "ok"), (404, "broken"), (429, "retry"), (503, "retry"), (400, "failed")):
            with mock.patch.object(self.mod.requests, "post", fake(code)):
                self.assertEqual(post(WEBHOOK2, data={}, timeout=30).status_code, code)
            self.assertEqual(d.reports[-1][0], "golive")
            self.assertEqual(d.reports[-1][1].kind, kind)
            self.assertEqual(d.report_urls[-1], WEBHOOK2)   # so a URL replaced meanwhile isn't blamed
        self.assertIs(seen["allow_redirects"], False)
        with mock.patch.object(self.mod.requests, "post", fake(exc=OSError("boom"))):
            with self.assertRaises(OSError):
                post(WEBHOOK2)
        self.assertEqual(d.reports[-1][1].kind, "retry")
        self.assertTrue(d.is_broken("golive"))
        self.assertNotIn(WEBHOOK2, " ".join(repr(r) for _i, r in d.reports))

    def test_report_survives_a_failing_registry(self):
        d = self.registry()
        self.configure(discord={"webhook": "golive"})
        post = FakeSnaps.instances[-1].kw["post"]
        d.report = mock.Mock(side_effect=RuntimeError("down"))
        with mock.patch.object(self.mod.requests, "post", return_value=mock.Mock(status_code=204)):
            self.assertEqual(post(WEBHOOK2).status_code, 204)

    def test_discord_caption_and_frame(self):
        self.with_discord()
        kw = FakeSnaps.instances[-1].kw
        self.assertEqual(kw["caption"](summary(connected=True, **PRINTING), False),
                         "Printer · Printing · 58% · 1 h 2 min left · layer 701/1058 · Benchy")
        with self.assertRaisesRegex(RuntimeError, "OBS"):
            kw["grab_frame"]()
        obs = FakeOBS()
        with mock.patch.object(self.bot, "get_module", lambda name: obs if name == "OBS" else None), \
                mock.patch.object(self.mod.obs_scene, "grab_frame", return_value=b"jpg") as grab:
            self.assertEqual(kw["grab_frame"](), b"jpg")
        grab.assert_called_once_with(obs, "Printer Camera")

    # ---------------------------------------------------------------- states, display, chat

    def test_states_and_display(self):
        w = self.configure()
        display = lambda: self.bot.read_display({"source": "bambu_status"})["label"]  # noqa: E731
        self.assertEqual(display(), "Printer\nidle")
        w.summary.update(PRINTING)
        self.assertEqual(display(), "58%\nL 701/1058\n1 h 2 min")
        self.assertTrue(self.bot.check_state({"bambu": "printing"}))
        self.assertTrue(self.bot.check_state({"bambu": "active"}))
        self.assertFalse(self.bot.check_state({"bambu": "paused"}))
        w.summary.update(state="PAUSE")
        self.assertEqual(display(), "Paused\n58%")
        self.assertTrue(self.bot.check_state({"bambu": "paused"}))
        w.summary.update(state="PREPARE")
        self.assertEqual(display(), "Getting\nready")
        w.summary.update(state="FINISH", active=False)
        self.assertEqual(display(), "Done")
        self.assertTrue(self.bot.check_state({"bambu": "finished"}))
        w.summary.update(state="FAILED")
        self.assertEqual(display(), "Failed")
        self.assertTrue(self.bot.check_state({"bambu": "failed"}))
        w.summary.update(light="on")
        self.assertTrue(self.bot.check_state({"bambu": "light"}))
        w.connected = False
        self.assertEqual(display(), "Printer\noffline")
        self.assertFalse(self.bot.check_state({"bambu": "failed"}))

    def test_print_builtin(self):
        self.assertIsNotNone(chatcmd.get("print"))
        w = self.configure(name="Alice")
        builtin = chatcmd.get("print")
        self.assertEqual(builtin.default_response, "{printer}: {status}")
        self.assertEqual(builtin.label, "3D print status")
        self.assertEqual(builtin.placeholders,
                         {"printer", "status", "percent", "layer", "time_left", "finish", "job"})
        w.summary.update(PRINTING)
        values = builtin.values("anything")
        self.assertEqual((values["printer"], values["percent"], values["layer"]), ("Alice", "58%", "701/1058"))

    # ---------------------------------------------------------------- overlay files

    def test_overlay_files_written_only_when_changed(self):
        w = self.configure()
        w.summary.update(PRINTING)
        self.inst.write_overlay()
        folder = paths.overlay_dir()
        block, line = (os.path.join(folder, f"{n}.txt") for n in ("bambu", "bambu_line"))
        with open(line) as f:
            self.assertEqual(f.read(), "Printer · Printing · 58% · 1 h 2 min left · layer 701/1058 · Benchy")
        with open(block) as f:
            self.assertIn("Layer 701/1058", f.read())
        old = time.time() - 100
        for path in (block, line):
            os.utime(path, (old, old))
        self.inst.write_overlay()
        self.inst._written.clear()          # even without the cache, unchanged text isn't rewritten
        self.inst.write_overlay()
        self.assertEqual(os.path.getmtime(line), old)
        self.assertEqual(os.path.getmtime(block), old)
        self.assertFalse(os.path.exists(line + ".tmp"))
        w.summary.update(percent=59)
        self.inst.write_overlay()
        self.assertNotEqual(os.path.getmtime(line), old)

    def test_overlay_files_cleared_when_unconfigured_and_on_unload(self):
        w = self.configure()
        w.summary.update(PRINTING)
        self.inst.write_overlay()
        folder = paths.overlay_dir()
        block, line = (os.path.join(folder, f"{n}.txt") for n in ("bambu", "bambu_line"))
        self._write({})
        self.bot.load_config()
        self.inst._flush()
        self.inst.write_overlay()
        for path in (block, line):
            with open(path) as f:
                self.assertEqual(f.read(), "")
        old = time.time() - 100
        os.utime(line, (old, old))
        self.inst.write_overlay()               # cleared once, not every second
        self.assertEqual(os.path.getmtime(line), old)
        w = self.configure()
        w.summary.update(PRINTING)
        self.inst.write_overlay()
        with open(line) as f:
            self.assertIn("Printing", f.read())
        self.bot.unload_extension("plugin:bambu")
        for path in (block, line):
            with open(path) as f:
                self.assertEqual(f.read(), "")

    def test_no_overlay_files_while_not_configured(self):
        self.inst.write_overlay()
        self.assertFalse(os.path.exists(os.path.join(paths.overlay_dir(), "bambu.txt")))

    # ---------------------------------------------------------------- panel

    def test_snapshot_is_json_and_never_holds_a_secret(self):
        w = self.with_discord()
        relay = FakeRelay.instances[-1]
        relay.state, relay.error = "retrying", "The printer refused the camera connection."
        relay.detail = f"rtsps://bblp:{CODE}@192.0.2.1:322/streaming/live/1: 401 Unauthorized"
        w.connected, w.error = False, f"bad password {CODE}"
        text = json.dumps(self.inst.snapshot())
        self.assertNotIn(CODE, text)
        self.assertNotIn(WEBHOOK, text)
        self.assertNotIn("s3cr3t", text)
        snap = json.loads(text)
        card = snap["card"]
        self.assertEqual(card["title"], "Printer (Bambu printer)")
        self.assertEqual(card["tone"], "error")
        self.assertLessEqual(len(card["rows"]), 8)
        self.assertEqual([r[0] for r in card["rows"]],
                         ["State", "Progress", "Layer", "Time left", "Nozzle / Bed", "Camera", "Discord"])
        self.assertTrue(all(isinstance(v, str) for row in card["rows"] for v in row))
        self.assertIn(card["tone"], ("ok", "warn", "error", "idle"))

    def test_card_while_printing(self):
        w = self.configure(name="A very long printer name that goes on")
        w.summary.update(PRINTING, nozzle_temp=219.6, bed_temp=60.0)
        card = self.inst.snapshot()["card"]
        self.assertLessEqual(len(card["title"]), 40)
        self.assertTrue(card["title"].endswith(" (Bambu printer)"))
        self.assertEqual(card["tone"], "ok")
        self.assertIsNone(card["note"])
        self.assertEqual(dict(card["rows"]), {"State": "Printing", "Progress": "58%", "Layer": "701 / 1058",
                                              "Time left": "1 h 2 min", "Nozzle / Bed": "220°C / 60°C",
                                              "Camera": "Live", "Discord": "Off"})


    def test_card_progress(self):
        w = self.configure()
        self.assertIsNone(self.inst.snapshot()["card"]["progress"])
        w.summary.update(PRINTING)
        self.assertEqual(self.inst.snapshot()["card"]["progress"], 58)
        w.summary.update(state="PAUSE")
        self.assertEqual(self.inst.snapshot()["card"]["progress"], 58)
        w.summary.update(state="FINISH", active=False, percent=100)
        self.assertIsNone(self.inst.snapshot()["card"]["progress"])
        w.summary.update(PRINTING)
        w.connected = False
        self.assertIsNone(self.inst.snapshot()["card"]["progress"])

    # ---------------------------------------------------------------- panel page

    def page(self):
        page, problem = plugin_page.check_page(self.inst.panel_page())
        self.assertIsNone(problem)
        return page

    def buttons(self):
        return {a["id"]: a for a in self.page()["actions"]}

    def test_page_is_valid_and_matches_the_parser(self):
        self.inst._flush()
        page = self.page()
        self.assertEqual((page["title"], page["icon"]), ("Bambu Printer", "layers"))
        self.assertEqual(list(self.buttons()), ["pause", "resume", "stop", "light_on", "light_off", "camera",
                                                "trust_camera", "setup_obs", "scene", "snapshot"])
        fields = {f["key"]: f for f in page["fields"]}
        self.assertEqual(list(dict.fromkeys(f["group"] for f in page["fields"])),
                         ["Printer", "Camera", "Discord", "After a print"])
        self.assertEqual((fields["access_code"]["file"], fields["access_code"]["config_key"],
                          fields["access_code"]["env"]), ("bambu_access_code", "access_code_file", "BAMBU_ACCESS_CODE"))
        self.assertNotIn("webhook", fields)   # the old secret field is gone
        self.assertEqual((fields["discord.webhook"]["type"], fields["discord.webhook"]["group"],
                          fields["discord.webhook"]["label"]),
                         ("discord_webhook", "Discord", "Discord webhook"))
        # The form's defaults are what the module uses when config.yaml says nothing.
        s = self.inst.settings
        values = plugin_page.field_values(page["fields"], {})
        self.assertEqual(values["name"], "Printer")
        for key, value in values.items():
            group, _, leaf = key.partition(".")
            if key == "discord.webhook":
                continue
            want = {"camera": s.camera, "discord": s.discord, "on_print_end": s.end}.get(group)
            if want is not None:
                self.assertEqual(value, want[leaf], key)
        # Values the parser takes are accepted by the form, and the other way round.
        self.assertEqual(plugin_page.apply_settings(page["fields"], {}, {"camera.port": 1024, "discord.min_gap": 5.5,
                                                                         "on_print_end.after": 86400}, {}), [])
        self.assertEqual(len(plugin_page.apply_settings(page["fields"], {}, {"camera.port": 80}, {})), 1)

    def test_page_when_not_configured(self):
        self.inst._flush()
        for a in self.buttons().values():
            self.assertEqual((a["id"], a["enabled"], a["hint"]), (a["id"], False, "Set up the printer first"))

    def test_page_buttons_follow_the_printer(self):
        w = self.configure()
        b = self.buttons()
        self.assertEqual({k for k, a in b.items() if a["enabled"]},
                         {"light_on", "light_off", "camera", "trust_camera", "setup_obs", "scene"})
        self.assertEqual((b["trust_camera"]["label"], b["trust_camera"]["icon"]),
                         ("Trust printer cert again", "lock"))
        self.assertIn("reset or replaced", b["trust_camera"]["confirm"])
        self.assertEqual((b["pause"]["hint"], b["stop"]["hint"]), ("Only while printing", "No print is running"))
        self.assertEqual(b["camera"]["label"], "Stop camera")
        self.assertEqual(b["stop"]["tone"], "danger")
        self.assertEqual(b["stop"]["confirm"], "Stop the print on Printer? This cancels it and can't be undone.")
        self.assertIn("Discord webhook", b["snapshot"]["hint"])
        w.summary.update(PRINTING)
        b = self.buttons()
        self.assertEqual((b["pause"]["enabled"], b["resume"]["enabled"], b["stop"]["enabled"]), (True, False, True))
        w.summary.update(state="PAUSE")
        b = self.buttons()
        self.assertEqual((b["pause"]["enabled"], b["resume"]["enabled"], b["stop"]["enabled"]), (False, True, True))
        w.connected = False
        b = self.buttons()
        self.assertFalse(any(b[k]["enabled"] for k in ("pause", "resume", "stop", "light_on", "light_off")))
        self.assertEqual(b["light_on"]["hint"], "The printer isn't connected")
        self.run_action("bambu_camera", "off")
        self.assertEqual(self.buttons()["camera"]["label"], "Start camera")
        self.configure(camera={"enabled": False})
        self.assertFalse(self.buttons()["camera"]["enabled"])
        self.assertFalse(self.buttons()["trust_camera"]["enabled"])
        self.with_discord()
        self.assertTrue(self.buttons()["snapshot"]["enabled"])

    def test_panel_stop_stops_at_once(self):
        w = self.configure()
        w.summary.update(PRINTING)
        self.assertEqual(self.inst.panel_action("stop"), {"message": "Stopping the print"})
        self.assertEqual(w.calls.count("stop_print"), 1)
        self.notify.assert_not_called()
        self.assertIsNone(self.inst._stop_pending)
        # The deck key still needs two presses.
        self.run_action("bambu_stop")
        self.assertEqual(w.calls.count("stop_print"), 1)

    def test_panel_actions(self):
        w = self.configure()
        for action_id, call in (("pause", "pause"), ("resume", "resume"), ("light_on", ("light", True)),
                                ("light_off", ("light", False))):
            self.inst.panel_action(action_id)
            self.assertEqual(w.calls[-1], call)
        self.assertEqual(self.inst.panel_action("camera"), {"message": "Camera relay stopped"})
        self.assertEqual(self.inst.panel_action("camera"), {"message": "Camera relay started"})
        with self.assertRaisesRegex(ApiError, "OBS isn't reachable"):
            self.inst.panel_action("scene")
        with self.assertRaisesRegex(ApiError, "Discord snapshots are off"):
            self.inst.panel_action("snapshot")
        obs = FakeOBS(scene="Gameplay")
        with mock.patch.object(self.bot, "get_module", lambda name: obs if name == "OBS" else None):
            self.assertEqual(self.inst.panel_action("scene"), {"message": "OBS shows the printer camera"})
        self.assertEqual(obs.scene, "Printer Cam")
        w.connected = False
        with self.assertRaisesRegex(ApiError, "isn't connected"):
            self.inst.panel_action("pause")
        with self.assertRaises(ApiError) as cm:
            self.inst.panel_action("explode")
        self.assertEqual(cm.exception.status, 404)

    def test_slow_panel_actions_return_while_still_working(self):
        self.configure()
        release = threading.Event()
        with mock.patch.object(self.mod, "PANEL_WAIT", 0.05), \
                mock.patch.object(self.inst, "setup_obs", lambda arg: release.wait(5)):
            self.assertIn("Working on it", self.inst.panel_action("setup_obs")["message"])
        release.set()

if __name__ == "__main__":
    unittest.main()
