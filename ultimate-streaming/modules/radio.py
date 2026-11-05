"""Radio stations: every folder in ~/Stream/radio/ is a station, played shuffled into OBS.

    ~/Stream/radio/Elevator/*.mp3      (+ optional credits.yaml: "File.mp3": "by Artist · License")
    ~/Stream/radio/Lofi/*.mp3

Plays through one OBS media source (default "BRB Music"), one track at a time. Each
station keeps its own shuffle, new files join it automatically, and the chosen
station is remembered across restarts.

config.yaml (all optional):
    radio:
      dir: ~/Stream/radio
      source: BRB Music
      default_station: Elevator
"""
import json
import os
import random
import time

import yaml

from core import Module, action, display, expander, state, task
from core import source as option_source  # `source` is also a property here
from core.schema import UI, Choice, Number, Source
from core.paths import state_file

AUDIO_EXTS = (".mp3", ".ogg", ".opus", ".flac", ".wav", ".m4a", ".aac")
DONE = ("OBS_MEDIA_STATE_ENDED", "OBS_MEDIA_STATE_NONE", "OBS_MEDIA_STATE_ERROR")


def track_name(path):
    return os.path.splitext(os.path.basename(path))[0].replace("_", " ")


class Radio(Module):
    def on_load(self):
        self.queues, self.seen = {}, {}  # station -> shuffled tracks still to play / tracks known
        self.current, self.credits = None, {}
        self._root_made = False
        self._stations, self._stations_read = [], 0  # snapshot cache of stations()
        try:
            saved = json.load(open(state_file("radio.json")))
        except (OSError, ValueError):
            saved = {}
        self.station = saved.get("station") or self.config.get("default_station")
        self.on = saved.get("on", True)

    # ----- stations

    @property
    def root(self):
        return os.path.expanduser(self.config.get("dir", "~/Stream/radio"))

    @property
    def source(self):
        return self.config.get("source", "BRB Music")

    def stations(self):
        if "dir" in self.config and not self._root_made:
            os.makedirs(self.root, exist_ok=True)   # once, and only when configured
            self._root_made = True
        if not os.path.isdir(self.root):
            return []
        return sorted(d for d in os.listdir(self.root)
                      if os.path.isdir(os.path.join(self.root, d)) and not d.startswith("."))

    def snapshot(self):
        now = time.time()
        if now - self._stations_read > 30:
            try:
                self._stations, self._stations_read = self.stations(), now
            except OSError:
                self._stations_read = now
        playing = None
        if self.on and self.current:  # cached state only; now_playing() asks OBS
            playing = f"{self.station} · " + " ".join(filter(None, [track_name(self.current), self.credit(self.current)]))
        return {"on": self.on, "station": self.station, "stations": list(self._stations), "now_playing": playing}

    def tracks(self, station):
        folder = os.path.join(self.root, station)
        try:
            return sorted(os.path.join(folder, f) for f in os.listdir(folder) if f.lower().endswith(AUDIO_EXTS))
        except OSError:
            return []

    def save(self):
        with open(state_file("radio.json"), "w") as f:
            json.dump({"station": self.station, "on": self.on}, f)

    def obs(self, request, data=None):
        obs = self.bot.get_module("OBS")
        if not obs:
            raise RuntimeError("OBS module not loaded")
        return obs.send(request, data)

    # ----- playback

    def load(self, path):
        self.current = path
        try:
            self.obs("SetInputSettings", {"inputName": self.source, "inputSettings": {
                "is_local_file": True, "local_file": path or "", "looping": False,
                "restart_on_activate": False, "close_when_inactive": False}})
        except Exception as e:
            if type(e).__name__ == "OBSSDKRequestError" and getattr(e, "code", None) == 600:  # ResourceNotFound
                self.current = None
                raise RuntimeError(f"OBS has no {self.source!r} source for the radio to play through. "
                                   "Settings → setup checklist → “Add microphone & desktop audio” adds it "
                                   "(or run obs/build_scenes.py --audio)") from None
            raise

    def next_track(self):
        station = self.station
        files = self.tracks(station) if station else []
        queue = [f for f in self.queues.get(station, []) if f in files]
        # Tracks added since the shuffle was dealt join it at random spots.
        for f in set(files) - self.seen.get(station, set()) - set(queue) - {self.current}:
            queue.insert(random.randint(0, len(queue)), f)
        self.seen[station] = set(files)
        if not queue:
            queue = random.sample(files, len(files))
            if len(queue) > 1 and queue[0] == self.current:
                queue.append(queue.pop(0))  # no immediate repeat
        self.queues[station] = queue
        self.load(queue.pop(0) if queue and self.on else None)

    @task(every=2)
    def tick(self):
        stations = self.stations()
        if self.station not in stations:
            self.station = stations[0] if stations else None
        playing_here = self.current and os.path.dirname(self.current) == os.path.join(self.root, self.station or "")
        if not self.on or not self.station:
            if self.current:
                self.load(None)
            return
        try:
            media = self.obs("GetMediaInputStatus", {"inputName": self.source})["mediaState"]
        except Exception:
            return
        if not playing_here or not os.path.exists(self.current) or media in DONE:
            self.next_track()

    # ----- credits / now playing

    def credit(self, path):
        """Attribution from credits.yaml in the track's station folder, or ""."""
        path_yaml = os.path.join(os.path.dirname(path), "credits.yaml")
        try:
            mtime = os.path.getmtime(path_yaml)
            if self.credits.get(path_yaml, (None,))[0] != mtime:
                with open(path_yaml) as f:
                    data = yaml.safe_load(f)
                self.credits[path_yaml] = (mtime, data if isinstance(data, dict) else {})
            credit = self.credits[path_yaml][1].get(os.path.basename(path), "")
            return credit if isinstance(credit, str) else ""
        except (OSError, yaml.YAMLError):
            return ""

    def now_playing(self):
        """"Station · Track by Artist · License" while a track is playing, else None."""
        if not (self.on and self.current):
            return None
        try:
            if self.obs("GetMediaInputStatus", {"inputName": self.source})["mediaState"] != "OBS_MEDIA_STATE_PLAYING":
                return None
        except Exception:
            return None
        track = " ".join(filter(None, [track_name(self.current), self.credit(self.current)]))
        return f"{self.station} · {track}"

    # ----- deck

    @action("radio", ui=UI("Radio", "BRB screen", scalar="command", args={
        "command": Choice(("toggle", "On / off"), ("next", "Skip track"), ("next_station", "Next station"),
                          ("prev_station", "Previous station"), ("on", "On"), ("off", "Off"), label="Do"),
        "station": Source("stations", "Or play station")}))
    def control(self, arg):
        """radio: next (track) | next_station | prev_station | toggle | on | off | {station: Name}"""
        stations = self.stations()
        if isinstance(arg, dict):
            if arg["station"] not in stations:
                raise ValueError(f"no station folder {arg['station']!r} in {self.root}")
            self.station, self.on = arg["station"], True
        elif arg in ("next_station", "prev_station") and stations:
            i = stations.index(self.station) if self.station in stations else -1
            self.station = stations[(i + (1 if arg == "next_station" else -1)) % len(stations)]
        elif arg in ("toggle", "on", "off"):
            self.on = not self.on if arg == "toggle" else arg == "on"
        elif arg != "next":
            raise ValueError(f"unknown radio action {arg!r}")
        self.save()
        self.next_track()

    @option_source("stations")
    def station_choices(self):
        return self.stations()

    @state("radio", ui=UI("Radio is playing", "BRB screen", arg=Source("stations", "Station (or 'on')")))
    def radio_state(self, what, spec):
        """state: {radio: on} | {radio: <station name>} (that station is playing)"""
        return self.on if what == "on" else (self.on and self.station == what)

    @display("radio", ui=UI("Radio station / track", "BRB screen", extra={
        "track": Choice((False, "the station"), (True, "the track"), label="Show")}))
    def radio_display(self, spec):
        """Current station, and track when spec has `track: true`."""
        if not self.on:
            return {"label": "Radio off"}
        if spec.get("track") and self.current:
            return {"label": track_name(self.current)}
        return {"label": self.station or "No stations"}

    @expander("station_slot", ui=UI("Radio station slot", "BRB screen", arg=Number("Station number", min=1)))
    def station_button(self, slot, btn):
        """{station_slot: 2} becomes a key for the 2nd station folder (blank if there isn't one)."""
        stations = self.stations()
        n = int(slot) - 1
        if n >= len(stations):
            return None
        name = stations[n]
        return {"label": f"{name}\n{len(self.tracks(name))} tracks", "icon": "emoji:📻", "bg": "#3a2d5c",
                "action": {"radio": {"station": name}}, "state": {"radio": name, "every": 1},
                "active": {"bg": "#9147ff", "label": f"{name}\non air"}, **btn}


def setup(bot):
    bot.add_module(Radio(bot))
