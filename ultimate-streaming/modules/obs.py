"""OBS Studio over obs-websocket v5.

Port and password are read from OBS's own websocket config (native or Flatpak OBS),
so `obs: {}` in config.yaml is enough when the deck and OBS run on the same machine.
To reach OBS elsewhere:
    obs:
      host: 192.168.1.20
      port: 4455
      password_file: ~/.config/ultimate-streaming/obs_password   # or env OBS_WEBSOCKET_PASSWORD
"""
import json
import logging
import os
import re
import threading
import time

from core import Module, action, display, expander, source, state, task
from core.colors import obs_int, parse_color
from core.schema import UI, Choice, Data, Number, Path, Source, Text
from core.paths import obs_websocket_config
from core.util import read_secret


WS_REREAD_EVERY = 30    # s: how often a failed connect re-reads OBS's own websocket config


class OBS(Module):
    def __init__(self, bot):
        super().__init__(bot)
        self._lock = threading.RLock()  # the panel's HTTP threads share the one client with the bot loop
        self._ws_read_at = 0.0

    def on_config(self, config):
        super().on_config(config)
        with self._lock:      # a connect in flight finishes first, so it can't bring back a client with old settings
            self.explicit = {k: v for k, v in config.items() if k in ("host", "port")}
            password = read_secret("OBS_WEBSOCKET_PASSWORD", config.get("password_file"))
            if password is not None:
                self.explicit["password"] = password
            self._ws_read_at = time.time()
            self.settings = {**self._ws_defaults(), **self.explicit}
            self._disconnect()

    def _ws_defaults(self):
        """Port and password from OBS's own websocket config, for what config.yaml doesn't set."""
        path = obs_websocket_config()
        if not os.path.exists(path) or self.explicit.get("host", "localhost") not in ("localhost", "127.0.0.1", "::1"):
            return {}
        try:
            with open(path) as f:
                c = json.load(f)
        except (OSError, ValueError) as e:
            self.log.warning("can't read %s: %s", path, e)
            return {}
        if not isinstance(c, dict):
            return {}
        out = {"port": c.get("server_port", 4455)}
        if c.get("auth_required"):
            out["password"] = c.get("server_password", "")
        return out

    def _rediscover(self):
        """After a failed connect: if port/password come from OBS's config (not config.yaml), re-read it
        (at most every WS_REREAD_EVERY s; OBS's websocket may have been enabled after we started).
        True when the settings changed, so a retry is worthwhile."""
        now = time.time()
        if now - self._ws_read_at < WS_REREAD_EVERY or {"port", "password"} <= set(self.explicit):
            return False
        self._ws_read_at = now
        settings = {**self._ws_defaults(), **self.explicit}
        if settings == self.settings:
            return False
        self.settings = settings
        return True

    def on_load(self):
        # obsws_python logs every refused request with a traceback (and logs the
        # password on connect); failures are reported by the bot instead.
        logging.getLogger("obsws_python").setLevel(logging.CRITICAL)
        self.scenes = []
        self._last_bytes = None
        self._bitrate_last = None
        self.state = {"ok": False, "error": "OBS isn’t reachable"}
        self.refresh_thread = None
        self._refresh_lock = threading.Lock()

    def on_unload(self):
        self._disconnect()

    # ----- connection

    def _disconnect(self):
        client, self.client, self.retry_at = getattr(self, "client", None), None, 0
        if client:
            try:
                client.disconnect()
            except Exception:
                pass

    def _connect(self, obsws_python):
        return obsws_python.ReqClient(
            host=self.settings.get("host", "localhost"), port=self.settings.get("port", 4455),
            password=self.settings.get("password", ""), timeout=2)

    def send(self, request, data=None):
        with self._lock:
            return self._send(request, data)

    def _send(self, request, data):
        if not self.client:
            if time.time() < self.retry_at:
                raise RuntimeError("OBS not reachable")
            import obsws_python
            try:
                self.client = self._connect(obsws_python)
            except Exception:
                try:
                    if not self._rediscover():
                        raise
                    self.client = self._connect(obsws_python)   # OBS's config changed: try what it says now
                except Exception:
                    self.retry_at = time.time() + 5
                    raise
        try:
            return self.client.send(request, data, raw=True)
        except Exception as e:
            if not self.is_request_error(e):   # OBS refusing a request leaves the connection healthy
                client, self.client = self.client, None
                try:
                    client.disconnect()       # close the dead socket instead of leaking it
                except Exception:
                    pass
            raise

    # ----- queries

    def current_scene(self):
        return self.send("GetCurrentProgramScene").get("sceneName")

    def scene_list(self):
        """Scene names in the order OBS's Scenes dock shows them."""
        return [s["sceneName"] for s in reversed(self.send("GetSceneList")["scenes"])]

    def output_active(self, what):
        if what == "studio":
            return bool(self.send("GetStudioModeEnabled").get("studioModeEnabled"))
        req = {"record": "GetRecordStatus", "stream": "GetStreamStatus",
               "replay": "GetReplayBufferStatus", "virtualcam": "GetVirtualCamStatus"}[what]
        return bool(self.send(req).get("outputActive"))

    # ----- actions

    @action("obs", ui=UI("OBS command", "OBS", scalar="request", args={
        "request": Choice(("ToggleStream", "Start / stop streaming"), ("ToggleRecord", "Start / stop recording"),
                          ("ToggleRecordPause", "Pause / resume recording"), ("SaveReplayBuffer", "Save replay"),
                          ("ToggleReplayBuffer", "Replay buffer on / off"), ("ToggleVirtualCam", "Virtual camera on / off"),
                          ("SetInputMute", "Set mute (needs data)"), label="Command", custom=True,
                          help="Any obs-websocket request name"),
        "data": Data("Request data", help="e.g. inputName: Mic/Aux, inputMuted: true")}))
    def obs_request(self, arg):
        """obs: ToggleRecord   (any obs-websocket request), or {request: X, data: {...}}"""
        if isinstance(arg, str):
            self.send(arg)
        else:
            self.send(arg["request"], arg.get("data"))

    @action("obs_scene", ui=UI("Switch OBS scene", "OBS", arg=Source("obs_scenes", "Scene")))
    def switch_scene(self, name):
        self.send("SetCurrentProgramScene", {"sceneName": name})

    @action("obs_mute", ui=UI("Mute / unmute OBS audio", "OBS", arg=Source("obs_inputs", "Audio source")))
    def toggle_mute(self, input_name):
        """obs_mute: "Mic/Aux"  (name as shown in OBS's Audio Mixer)"""
        try:
            self.send("ToggleInputMute", {"inputName": input_name})
        except Exception as e:
            if self.is_request_error(e) and getattr(e, "code", None) == 600:   # ResourceNotFound
                raise RuntimeError(f"OBS has no audio source named {input_name!r}. Settings → setup checklist → "
                                   "“Add microphone & desktop audio”, or point this key at a source OBS has") from None
            raise

    @action("obs_color", ui=UI("Set a color source's color", "OBS", args={
        "source": Source("obs_color_sources", "Color source"),
        "color": Text("Color", placeholder="#9146FF or purple")}))
    def set_color(self, arg):
        """obs_color: {source: "Background", color: purple}  (hex like #9146FF, or a CSS color name)"""
        rgb = parse_color(arg.get("color"))
        name = arg.get("source")
        try:
            self.send("SetInputSettings", {"inputName": name, "inputSettings": {"color": obs_int(rgb)}})
        except Exception as e:
            if self.is_request_error(e) and getattr(e, "code", None) == 600:   # ResourceNotFound
                raise RuntimeError(f"OBS has no color source named {name!r}. Add one in OBS (Sources → + → "
                                   "Color Source), or point this key at a source OBS has") from None
            raise

    @action("obs_studio", ui=UI("Studio mode on / off", "OBS", arg=Choice(("toggle", "toggle"))))
    def toggle_studio(self, arg):
        self.send("SetStudioModeEnabled", {"studioModeEnabled": not self.output_active("studio")})

    @action("obs_screenshot", ui=UI("Screenshot of the scene", "OBS", arg=Path("Save to folder")))
    def screenshot(self, folder):
        """obs_screenshot: ~/Pictures — saves a PNG of the current scene."""
        folder = os.path.expanduser(folder or "~/Pictures")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, time.strftime("obs-%Y%m%d-%H%M%S.png"))
        self.send("SaveSourceScreenshot", {"sourceName": self.current_scene(),
                                           "imageFormat": "png", "imageFilePath": path})
        return path

    # ----- states

    @state("obs", ui=UI("OBS is…", "OBS", arg=Choice(("stream", "streaming"), ("record", "recording"),
                                                     ("replay", "replay buffer on"), ("virtualcam", "virtual camera on"),
                                                     ("studio", "in studio mode"))))
    def output_state(self, what, spec):
        """state: {obs: record | stream | replay | virtualcam | studio}"""
        return self.output_active(what)

    @state("obs_scene", ui=UI("OBS scene is showing", "OBS", arg=Source("obs_scenes", "Scene")))
    def scene_state(self, name, spec):
        return self.current_scene() == name

    @state("obs_muted", ui=UI("OBS audio is muted", "OBS", arg=Source("obs_inputs", "Audio source")))
    def muted_state(self, input_name, spec):
        return bool(self.send("GetInputMute", {"inputName": input_name}).get("inputMuted"))

    # ----- displays

    @display("obs_uptime", ui=UI("Stream uptime (LIVE 1:23:45)", "OBS"))
    def uptime(self, spec):
        """Shows LIVE and the stream duration while streaming."""
        s = self.send("GetStreamStatus")
        if not s.get("outputActive"):
            return {}
        h, m, sec = s["outputTimecode"].split(".")[0].split(":")
        return {"label": f"LIVE\n{int(h)}:{m}:{sec}" if int(h) else f"LIVE\n{int(m)}:{sec}"}

    @display("obs_health", ui=UI("Stream health (bitrate, dropped frames)", "OBS"))
    def health(self, spec):
        """Bitrate and dropped frames while streaming; turns red when frames drop."""
        s = self.send("GetStreamStatus")
        if not s.get("outputActive"):
            self._last_bytes = None
            return {}
        now, total, skipped = time.time(), s.get("outputTotalFrames") or 0, s.get("outputSkippedFrames") or 0
        last, self._last_bytes = self._last_bytes, (s["outputBytes"], now)
        if not last:
            return {"label": "…", "icon": None}
        kbps = (s["outputBytes"] - last[0]) * 8 / 1000 / (now - last[1])
        drop = 100 * skipped / total if total else 0.0
        bad = drop > 1 or (s.get("outputCongestion") or 0) > 0.5
        return {"label": f"{kbps:.0f} kbps\n{drop:.1f}% drop", "icon": None,
                "bg": "#a01818" if bad else "#106040"}

    # ----- scene slots

    @expander("scene_slot", ui=UI("OBS scene slot", "OBS", arg=Number("Scene number", min=1),
                                  help="Becomes a key for that scene, in OBS's Scenes-dock order"))
    def scene_button(self, slot, btn):
        """{scene_slot: 3} becomes a button for OBS's 3rd scene (blank if there isn't one)."""
        n = int(slot) - 1
        if n >= len(self.scenes):
            return None
        name = self.scenes[n]
        return {"label": name, "bg": "#203050", "active": {"bg": "#3060c0"},
                "action": {"obs_scene": name}, "state": {"obs_scene": name, "every": 1}, **btn}

    @source("obs_scenes")
    def scene_choices(self):
        return self.scene_list()

    @source("obs_inputs")
    def input_choices(self):
        """Inputs that have audio (mic, desktop, media...)."""
        names = []
        for i in self.send("GetInputList")["inputs"]:
            try:
                self.send("GetInputMute", {"inputName": i["inputName"]})
                names.append(i["inputName"])
            except Exception:
                pass  # no audio
        return names

    @source("obs_color_sources")
    def color_source_choices(self):
        return [i["inputName"] for i in self.send("GetInputList")["inputs"]
                if str(i.get("inputKind", "")).startswith("color_source")]

    @task(every=3)
    def refresh_scenes(self):
        """Keep the scene list current while a page with scene slots is shown."""
        sessions = getattr(self.bot, "sessions", None)
        if sessions:
            pages = [s.raw_buttons() for s in list(sessions.values())]
        else:
            pages = [self.bot.raw_buttons()]
        if not any(b and "scene_slot" in b for buttons in pages for b in buttons):
            return
        try:
            scenes = self.scene_list()
        except Exception:
            scenes = []
        if scenes != self.scenes:
            self.scenes = scenes
            self.bot.invalidate()

    # ----- control panel API (plain methods; raise on failure)

    OUTPUTS = {"stream": "Stream", "record": "Record", "replay": "ReplayBuffer", "virtualcam": "VirtualCam"}
    AUDIO_HINTS = ("audio", "wasapi", "pulse", "alsa", "coreaudio", "ffmpeg_source", "vlc_source", "browser_source")

    @staticmethod
    def is_request_error(e):
        """True when OBS answered and refused (bad name, wrong state), false when it can't be reached."""
        return type(e).__name__ == "OBSSDKRequestError"

    def stream_status(self):
        s = self.send("GetStreamStatus")
        active, nbytes, now = bool(s.get("outputActive")), s.get("outputBytes") or 0, time.time()
        last = self._bitrate_last
        self._bitrate_last = (nbytes, now) if active else None
        kbps = 0
        if active and last and now > last[1] and nbytes >= last[0]:
            kbps = round((nbytes - last[0]) * 8 / 1000 / (now - last[1]))
        return {"active": active, "seconds": int((s.get("outputDuration") or 0) / 1000), "bytes": nbytes,
                "dropped": s.get("outputSkippedFrames") or 0, "total": s.get("outputTotalFrames") or 0,
                "bitrate_kbps": kbps}

    def record_status(self):
        s = self.send("GetRecordStatus")
        return {"active": bool(s.get("outputActive")), "paused": bool(s.get("outputPaused")),
                "seconds": int((s.get("outputDuration") or 0) / 1000)}

    def _optional(self, fn, *args):
        """fn(*args), or None when OBS answers with a refusal (feature off or unsupported);
        connection errors still propagate."""
        try:
            return fn(*args)
        except Exception as e:
            if not self.is_request_error(e):
                raise
            return None

    def outputs(self):
        return {"stream": self.stream_status(), "record": self.record_status(),
                "replay": self._optional(self.output_active, "replay"),
                "virtualcam": self._optional(self.output_active, "virtualcam"),
                "studio": self.output_active("studio")}

    def set_scene(self, name, preview=False):
        self.send("SetCurrentPreviewScene" if preview else "SetCurrentProgramScene", {"sceneName": name})

    def preview_scene(self):
        """The studio-mode preview scene (only valid while studio mode is on)."""
        return self.send("GetCurrentPreviewScene").get("sceneName")

    def set_studio(self, enabled):
        self.send("SetStudioModeEnabled", {"studioModeEnabled": bool(enabled)})

    def transition(self):
        self.send("TriggerStudioModeTransition")

    def set_output(self, what, action):
        """what: stream|record|replay|virtualcam; action: start|stop|toggle|pause|resume
        (pause/resume only for record). Returns whether the output is active afterwards."""
        if what not in self.OUTPUTS:
            raise ValueError(f"unknown output {what!r}")
        if action not in ("start", "stop", "toggle", "pause", "resume"):
            raise ValueError(f"unknown action {action!r}")
        if action in ("pause", "resume") and what != "record":
            raise ValueError("only the recording can be paused")
        name = self.OUTPUTS[what]
        if action == "pause":
            self.send("PauseRecord")
            return self.output_active("record")
        if action == "resume":
            self.send("ResumeRecord")
            return self.output_active("record")
        if action == "toggle":
            return bool(self.send("Toggle" + name).get("outputActive"))
        self.send(("Start" if action == "start" else "Stop") + name)
        return action == "start"  # OBS reports the change a moment later

    def inputs(self):
        """Audio-capable inputs with mute state and volume."""
        out = []
        for i in self.send("GetInputList")["inputs"]:
            name, kind = i["inputName"], i.get("inputKind") or ""
            hinted = any(h in kind for h in self.AUDIO_HINTS)
            try:
                v = self.send("GetInputVolume", {"inputName": name})
            except Exception as e:
                if not self.is_request_error(e):
                    raise
                if not hinted:
                    continue
                v = {}
            try:
                muted = bool(self.send("GetInputMute", {"inputName": name}).get("inputMuted"))
            except Exception as e:
                if not self.is_request_error(e):
                    raise
                if not v and not hinted:
                    continue
                muted = False
            out.append({"name": name, "kind": kind, "muted": muted,
                        "volume_db": v.get("inputVolumeDb"), "volume_mul": v.get("inputVolumeMul")})
        return out

    def set_mute(self, name, muted):
        self.send("SetInputMute", {"inputName": name, "inputMuted": bool(muted)})

    def set_volume(self, name, db):
        db = max(-100.0, min(0.0, float(db)))
        self.send("SetInputVolume", {"inputName": name, "inputVolumeDb": db})
        return db

    def stats(self):
        s = self.send("GetStats")
        return {"cpu": round(s.get("cpuUsage") or 0, 1), "memory_mb": round(s.get("memoryUsage") or 0, 1),
                "fps": round(s.get("activeFps") or 0, 1), "render_skipped": s.get("renderSkippedFrames") or 0,
                "render_total": s.get("renderTotalFrames") or 0, "output_skipped": s.get("outputSkippedFrames") or 0,
                "output_total": s.get("outputTotalFrames") or 0, "disk_mb": round(s.get("availableDiskSpace") or 0)}

    def take_screenshot(self, folder=None):
        return self.screenshot(folder)

    def request(self, name, data=None):
        """Raw obs-websocket request; the response dict is returned as OBS sent it."""
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z]+", name):
            raise ValueError("request name must be letters only")
        return self.send(name, data) or {}

    def collect(self):
        """Everything the dashboard shows, fetched now. Raises if OBS is unreachable."""
        scene = self.current_scene()          # first: an unreachable OBS fails here, once
        scenes = self.scene_list()
        outputs = self.outputs()
        return {"ok": True, "error": None, "scene": scene, "scenes": scenes, "studio": outputs["studio"],
                "preview": self._optional(self.preview_scene) if outputs["studio"] else None,
                "outputs": outputs, "inputs": self.inputs(), "stats": self.stats()}

    @task(every=2, on_page_change=False)
    def refresh_state(self):
        """Keep the panel's snapshot current, but only while a panel is open."""
        if not self.bot.hub.subscribers:
            return
        with self._refresh_lock:
            if self.refresh_thread and self.refresh_thread.is_alive():
                return

            def work():
                try:
                    self.state = self.collect()
                except Exception as e:
                    if not self.is_request_error(e):
                        self.log.debug("OBS state: %s", e)
                    self.state = {"ok": False, "error": "OBS isn’t reachable"}
            self.refresh_thread = threading.Thread(target=work, daemon=True, name="obs-state")
            self.refresh_thread.start()

    def snapshot(self):
        return self.state


def setup(bot):
    bot.add_module(OBS(bot))
