"""Text files that OBS text sources show live: stream title, category and countdown timers.

The files live in ~/.config/ultimate-streaming/overlay/ (see core/paths.py). OBS text sources set to
"Read from file" pick up changes on their own, so the deck drives what's on screen.

Each timer writes <name>.txt. Built in: `countdown` (Starting Soon) and `brb_timer`.
Wording can be overridden in config.yaml:
    overlay:
      timers:
        countdown: {format: "Starting in {}", done: "Starting now!", idle: "Get comfy, we're about to start"}
"""
import collections
import logging
import os
import re
import tempfile
import threading
import time

from core import Module, action, display, task
from core.schema import UI, Bool, Choice, Seconds, Source
from core.paths import overlay_dir
TIMER_TEXT = {
    "countdown": {"format": "Starting in {}", "done": "Starting now!", "idle": "Get comfy, we're about to start"},
    "brb_timer": {"format": "Back in {}", "done": "Back any second now…", "idle": "Back soon"},
}


NAME_RE = re.compile(r"[a-z0-9_]{1,32}")


def _check_name(name):
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ValueError("a timer name is 1-32 characters: a-z, 0-9 and _")
    return name


_WRITE_LOCKS = collections.defaultdict(threading.Lock)   # path -> lock: one writer per file at a time
_LOCKS_LOCK = threading.Lock()


def write_overlay(path, text):
    """Atomically replace the text file `path` (UTF-8) if its content differs. A unique temp file in
    the same folder plus os.replace, so OBS never reads half a file and two writers never collide."""
    with _LOCKS_LOCK:
        lock = _WRITE_LOCKS[path]
    with lock:
        try:
            with open(path, encoding="utf-8") as f:
                if f.read() == text:
                    return
        except (FileNotFoundError, UnicodeDecodeError):
            pass
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".overlay-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def mmss(seconds):
    return f"{seconds // 60}:{seconds % 60:02}"


class Overlay(Module):
    def on_load(self):
        self.dir = overlay_dir()
        os.makedirs(self.dir, exist_ok=True)
        self.timers = {}  # name -> end time
        self._lock = threading.Lock()   # timer state + the remaining/format/write sequence (the main loop and action threads both tick)
        self._bad_formats = set()       # (timer, format) pairs already warned about
        self._info, self._info_read = {"title": "", "category": ""}, 0  # snapshot cache of the text files
        for name in TIMER_TEXT:
            self.write(name, self.text(name, "idle"))

    def path(self, name):
        _check_name(name)
        return os.path.join(self.dir, f"{name}.txt")

    def write(self, name, text):
        """Atomically replace an overlay file (only when the text changed). Safe from any thread."""
        write_overlay(self.path(name), text)

    def snapshot(self):
        now = time.time()
        if now - self._info_read > 5:
            self._info_read = now
            for name in ("title", "category"):
                try:
                    with open(self.path(name)) as f:
                        self._info[name] = f.read()
                except OSError:
                    self._info[name] = ""
        return {"timers": {name: self.remaining(name) for name in {*TIMER_TEXT, *self.timers}},
                "title": self._info["title"], "category": self._info["category"]}

    # ----- timers

    def text(self, timer, kind):
        custom = ((self.config.get("timers") or {}).get(timer) or {}).get(kind)
        return custom or TIMER_TEXT.get(timer, TIMER_TEXT["countdown"])[kind]

    @staticmethod
    def _parse(arg):
        """300 | "5:00" | stop | {seconds: 300, name: brb_timer, scene: ...} -> (name, seconds, scene)"""
        if isinstance(arg, dict):
            return _check_name(arg.get("name", "countdown")), arg.get("seconds"), arg.get("scene")
        if str(arg) == "stop":
            return "countdown", None, None
        m, _, s = str(arg).rpartition(":")
        return "countdown", int(m or 0) * 60 + int(s), None

    def start_timer(self, name, seconds):
        _check_name(name)
        with self._lock:
            self.timers[name] = time.time() + int(seconds)
            self._tick_locked()

    def stop_timer(self, name):
        _check_name(name)
        with self._lock:
            self.timers.pop(name, None)
            self.write(name, self.text(name, "idle"))

    def remaining(self, name="countdown"):
        end = self.timers.get(name)
        return None if end is None else max(0, int(end - time.time() + 0.999))

    @action("countdown", ui=UI("On-screen timer", "Stream", scalar="seconds", args={
        "seconds": Seconds("Length (seconds)", help="300 = 5 minutes"),
        "name": Choice(("countdown", "Starting-soon countdown"), ("brb_timer", "BRB 'back in' timer"), label="Timer"),
        "stop": Bool("Stop it instead")}))
    def countdown(self, arg):
        """countdown: 300 | "5:00" | stop | {name: brb_timer, seconds: 180} | {name: brb_timer, stop: true}"""
        name, seconds, _ = self._parse(arg)
        if seconds is None or (isinstance(arg, dict) and arg.get("stop")):
            self.stop_timer(name)
        else:
            self.start_timer(name, seconds)

    @action("countdown_toggle", ui=UI("Start / stop a timer (and switch scene)", "Stream", args={
        "seconds": Seconds("Length (seconds)"),
        "name": Choice(("countdown", "Starting-soon countdown"), ("brb_timer", "BRB 'back in' timer"), label="Timer"),
        "scene": Source("obs_scenes", "Switch to scene")}))
    def countdown_toggle(self, arg):
        """countdown_toggle: {seconds: 300, scene: Starting Soon, name: countdown} — starts the
        timer (and switches scene), or stops it if it's already running."""
        name, seconds, scene = self._parse(arg)
        if self.remaining(name):
            self.stop_timer(name)
            return
        self.start_timer(name, seconds or 300)
        if scene:
            self.bot.run_action({"obs_scene": scene})

    @task(every=0.25)
    def tick(self):
        with self._lock:
            self._tick_locked()

    def _tick_locked(self):
        for name in list(self.timers):
            left = self.remaining(name)
            if left == 0:
                self.timers.pop(name, None)
                self.write(name, self.text(name, "done"))
            else:
                self.write(name, self._format(name, left))

    def _format(self, name, left):
        fmt = self.text(name, "format")
        try:
            return fmt.format(mmss(left))
        except Exception as e:   # a custom format with a stray {placeholder} or brace
            if (name, fmt) not in self._bad_formats:
                self._bad_formats.add((name, fmt))
                self.log.warning("overlay timer %s: format %r doesn't work (%s: %s); using the default text",
                                 name, fmt, type(e).__name__, e)
            return TIMER_TEXT.get(name, TIMER_TEXT["countdown"])["format"].format(mmss(left))

    @display("countdown", ui=UI("Time left on a timer", "Stream", extra={
        "timer": Choice(("countdown", "Starting-soon countdown"), ("brb_timer", "BRB 'back in' timer"), label="Timer")}))
    def countdown_display(self, spec):
        """display: {source: countdown, timer: brb_timer} — time left on the key while it runs."""
        left = self.remaining(spec.get("timer", "countdown"))
        return {"label": f"Stop\n{mmss(left)}", "bg": "#c01010"} if left else {}

    # ----- stream title / category

    def set_stream_info(self, title, category):
        self.write("title", title or "")
        self.write("category", category or "")

    @task(every=120)
    def sync_stream_info(self):
        """Pick up title/category changes made outside the deck (e.g. on twitch.tv)."""
        twitch = self.bot.get_module("Twitch")
        if not (twitch and twitch.linked()):
            return

        def work():
            try:
                info = twitch.channel_info()
                self.set_stream_info(info["title"], info["game_name"])
            except Exception as e:
                self.log.debug("stream info sync: %s", e)

        threading.Thread(target=work, daemon=True).start()


def setup(bot):
    bot.add_module(Overlay(bot))
