"""The interactive BRB screen: chat feed, poll & guess games, hype meter, videos (with their own audio).

Writes overlay files (chat.txt, vote.txt, hype.txt, videocredit.txt) that OBS text
sources in the BRB scene read, and drives the hype bar, banner and the BRB video
in OBS directly. Chat comes from the chat module's "chat_message" event.

Chat commands:  !1 … !9 (vote)   !guess <minutes>   !hype

config.yaml (all optional):
    brb:
      scene: BRB
      chat_lines: 7
      hype_per_message: 2
      hype_per_command: 8
      hype_decay_per_min: 12
      media_dir: ~/Stream/brb        # videos/ play on screen, one at a time, with their own audio
      highlights_dir: ~/Stream/recordings  # where OBS saves replays (Replay*.mp4)
      highlights_count: 0            # also add this many of the newest saved replays
      polls:
        - {question: "What should I make next?", options: [Option A, Option B, Option C]}
"""
import collections
import glob
import os
import random
import re
import threading
import time

import yaml

from core import Module, action, display, listener, notify, state, task
from core.schema import UI, Choice, Source

HYPE_TRACK_WIDTH = 720
VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv")


HYPE_WINDOW = 10     # s: the longest a chatter waits between hype gains (!hype)
HYPE_SEEN_MAX = 5000


def media_files(folder, exts):
    folder = os.path.expanduser(folder)
    return sorted(f for f in glob.glob(os.path.join(folder, "*")) if f.lower().endswith(exts) and os.path.isfile(f))


GAME_IDLE = "No game running right now.\nType !hype to fill the hype meter!"


def bar(fraction, width=12):
    full = round(fraction * width)
    return "█" * full + "░" * (width - full)


class BRB(Module):
    def on_load(self):
        self.lock = threading.Lock()
        self.chat = collections.deque(maxlen=int(self.config.get("chat_lines", 7)))
        self.game = None
        self.hype, self.hype_full_until, self.hype_drawn = 0.0, 0, None
        self.hype_seen = {}  # user -> last time they added hype
        self.video_queue, self.video_seen, self.video_current = [], set(), None
        self.videos_shown, self.video_credits = None, (None, {})
        self._media_dir_made = False
        self.initialised = False  # starting text is written on the first refresh, once Overlay is loaded

    def write_initial(self):
        self.write("chat", "Chat messages show up here")
        self.write("vote", GAME_IDLE)
        self.initialised = True

    # ----- helpers

    def write(self, name, text):
        overlay = self.bot.get_module("Overlay")
        if overlay:
            overlay.write(name, text)

    def obs(self, request, data=None):
        obs = self.bot.get_module("OBS")
        if not obs:
            raise RuntimeError("OBS module not loaded")
        return obs.send(request, data)

    def set_enabled(self, source, enabled):
        scene = self.config.get("scene", "BRB")
        item = self.obs("GetSceneItemId", {"sceneName": scene, "sourceName": source})["sceneItemId"]
        self.obs("SetSceneItemEnabled", {"sceneName": scene, "sceneItemId": item, "sceneItemEnabled": enabled})

    # ----- chat

    @listener("chat_message")
    def on_chat(self, msg):
        user, text = msg["user"], msg["text"].strip()
        now = time.time()
        with self.lock:
            shown = text if len(text) <= 90 else text[:87] + "…"
            self.chat.append(f"{user}: {shown}")
            chat_text = "\n".join(self.chat)

            low = text.lower()
            if self.game and self.game["open"]:
                if self.game["type"] == "poll":
                    m = re.fullmatch(r"!?([1-9])", low)
                    if m and int(m.group(1)) <= len(self.game["options"]):
                        self.game["votes"][user] = int(m.group(1)) - 1
                        self.game["dirty"] = True
                else:
                    m = re.fullmatch(r"!guess\s+(\d+(?:\.\d+)?)", low)
                    if m:
                        self.game["guesses"][user] = float(m.group(1))
                        self.game["dirty"] = True

            # Hype: each chatter adds a little at most every 3 s; !hype adds more, every 10 s.
            is_cmd = low.startswith("!hype")
            last = self.hype_seen.get((user, is_cmd), 0)
            if now - last >= (10 if is_cmd else 3) and now >= self.hype_full_until:
                self.hype_seen[(user, is_cmd)] = now
                gain = self.config.get("hype_per_command", 8) if is_cmd else self.config.get("hype_per_message", 2)
                self.hype = min(100.0, self.hype + gain)
        self.write("chat", chat_text)   # file I/O outside the lock

    def prune_hype_seen(self, now):
        """Forget chatters whose cooldown is over (their entry no longer changes anything); lock held."""
        for key in [k for k, t in self.hype_seen.items() if now - t >= HYPE_WINDOW]:
            del self.hype_seen[key]
        if len(self.hype_seen) > HYPE_SEEN_MAX:
            newest = sorted(self.hype_seen.items(), key=lambda kv: kv[1])[-HYPE_SEEN_MAX:]
            self.hype_seen = dict(newest)

    # ----- games

    @action("brb_poll", ui=UI("Start a chat poll", "BRB screen", arg=Source("polls", "Poll")))
    def start_poll(self, arg):
        """brb_poll: 0 (index into brb.polls) | {question: ..., options: [...]}"""
        poll = self.config.get("polls", [])[int(arg)] if not isinstance(arg, dict) else arg
        if not 2 <= len(poll["options"]) <= 9:
            raise ValueError("a poll needs 2–9 options")
        with self.lock:
            self.game = {"type": "poll", "question": poll["question"], "options": list(poll["options"]),
                         "votes": {}, "open": True, "dirty": True}

    @action("brb_guess", ui=UI("Start the guessing game", "BRB screen", arg=Choice(("start", "start"))))
    def start_guess(self, arg=None):
        """brb_guess: start — viewers guess how many minutes until you're back."""
        with self.lock:
            self.game = {"type": "guess", "start": time.time(), "guesses": {}, "open": True, "dirty": True}

    @action("brb_game", ui=UI("Poll / game control", "BRB screen", arg=Choice(
        ("next", "End it, then clear"), ("end", "End and show the result"), ("clear", "Clear"))))
    def game_control(self, arg):
        """brb_game: end (close it and show the result) | clear | next (end if open, else clear)"""
        with self.lock:
            if arg == "next" and self.game and not self.game["open"]:
                arg = "clear"
            cleared = arg == "clear" or not self.game
            if cleared:
                self.game = None
        if cleared:
            self.write("vote", GAME_IDLE)   # file I/O outside the lock
            return
        with self.lock:
            if not self.game:
                return
            if self.game["open"]:
                self.game["open"] = False
                self.game["ended"] = time.time()
                self.game["dirty"] = True
                winner = self.winner_text()
                if winner:
                    notify(winner)

    def snapshot(self):
        with self.lock:
            g, game = self.game, None
            if g:
                poll = g["type"] == "poll"
                counts = collections.Counter(g["votes"].values()) if poll else {}
                game = {"kind": g["type"], "question": g["question"] if poll else "How many minutes until I'm back?",
                        "options": [{"label": o, "votes": counts[i]} for i, o in enumerate(g["options"])] if poll else [],
                        "total_votes": len(g["votes"]) if poll else 0,
                        "guesses": len(g["guesses"]) if not poll else 0,
                        "ends_at": None,  # games run until you end them
                        "winner": None if g["open"] else self.winner_text()}
            else:
                game = {"kind": None, "question": "", "options": [], "total_votes": 0, "guesses": 0,
                        "ends_at": None, "winner": None}
            current = self.video_current
            video = {"name": os.path.splitext(os.path.basename(current))[0], "credit": self.video_credit(current)} \
                if current else None
            return {"game": game, "hype": {"value": round(self.hype, 1), "max": 100, "full_until": self.hype_full_until or None},
                    "video": video, "chat_lines": len(self.chat)}

    def winner_text(self):
        g = self.game
        if g["type"] == "poll":
            counts = collections.Counter(g["votes"].values())
            if not counts:
                return "Poll ended with no votes"
            best = max(counts.values())
            top = [g["options"][i] for i, c in counts.items() if c == best]
            return f"Poll winner: {' / '.join(top)} ({best} vote{'s' * (best != 1)})"
        actual = (g.get("ended", time.time()) - g["start"]) / 60
        if not g["guesses"]:
            return f"Gone {actual:.1f} min. Nobody guessed!"
        user, guess = min(g["guesses"].items(), key=lambda kv: abs(kv[1] - actual))
        return f"{user} wins! Guessed {guess:g} min, actual {actual:.1f} min"

    def game_text(self):
        g = self.game
        if g["type"] == "poll":
            total = len(g["votes"])
            counts = collections.Counter(g["votes"].values())
            head = ("POLL: " if g["open"] else "RESULTS: ") + g["question"]
            width = max(len(o) for o in g["options"])
            rows = [f"!{i + 1} {o:<{width}} {bar(counts[i] / total if total else 0)} {counts[i]}"
                    for i, o in enumerate(g["options"])]
            foot = f"Type !1–!{len(g['options'])} in chat · {total} vote{'s' * (total != 1)}" if g["open"] \
                else self.winner_text()
            return "\n".join([head, "", *rows, "", foot])
        if g["open"]:
            recent = list(g["guesses"].items())[-4:]
            lines = [f"  {u}: {v:g} min" for u, v in recent]
            return "\n".join(["GUESS GAME", "How many minutes until I'm back?", "Type !guess <minutes>", "",
                              f"{len(g['guesses'])} guess{'es' * (len(g['guesses']) != 1)} so far", *lines])
        return "\n".join(["GUESS GAME RESULT", "", self.winner_text()])

    # ----- hype

    def hype_text(self):
        if time.time() < self.hype_full_until:
            return "HYPE MAXED!"
        return f"HYPE {self.hype:.0f}%  ·  type !hype"

    def add_hype(self, amount):
        """Add `amount` points (0-100) to the hype meter, e.g. for a follow or a raid (modules/alerts.py).
        Ignored while the meter is maxed. Safe from any thread; must not be called with self.lock held."""
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            return
        if not amount > 0:
            return
        with self.lock:
            if time.time() >= self.hype_full_until:
                self.hype = min(100.0, self.hype + min(amount, 100.0))

    @action("brb_hype", ui=UI("Hype meter", "BRB screen", arg=Choice(("reset", "Reset"), ("fill", "Fill (test)"))))
    def hype_control(self, arg):
        """brb_hype: reset | fill (for testing)"""
        with self.lock:
            self.hype = 100.0 if arg == "fill" else 0.0
            if arg == "reset":
                self.hype_full_until = 0

    # ----- periodic work

    @task(every=0.5)
    def refresh(self):
        now = time.time()
        if not self.initialised and self.bot.get_module("Overlay"):
            self.write_initial()
        vote_text = None
        with self.lock:
            self.prune_hype_seen(now)
            if self.game and self.game.pop("dirty", False):
                vote_text = self.game_text()
            # Hype: decay, and latch "maxed" for 30 s once it reaches 100.
            if now >= self.hype_full_until:
                if self.hype_full_until:
                    self.hype_full_until, self.hype = 0, 0.0
                elif self.hype >= 100:
                    self.hype_full_until = now + 30
                    notify("Hype meter maxed!")
                else:
                    self.hype = max(0.0, self.hype - self.config.get("hype_decay_per_min", 12) / 120)
            level, full = round(self.hype), now < self.hype_full_until
            hype_text = self.hype_text()
        if vote_text is not None:       # file I/O outside the lock
            self.write("vote", vote_text)
        self.write("hype", hype_text)
        if (level, full) != self.hype_drawn:
            try:
                self.obs("SetInputSettings", {"inputName": "Hype Fill", "inputSettings": {
                    "width": max(1, HYPE_TRACK_WIDTH * level // 100), "height": 24}})
                self.set_enabled("Hype Banner", full)
                self.hype_drawn = (level, full)
            except Exception as e:
                self.log.debug("hype bar: %s", e)

    def media_dir(self, kind):
        return os.path.join(os.path.expanduser(self.config.get("media_dir", "~/Stream/brb")), kind)

    # ----- videos

    def playlist(self):
        """Videos from media_dir/videos (by name), then optionally the newest saved replays."""
        if "media_dir" in self.config and not self._media_dir_made:
            os.makedirs(self.media_dir("videos"), exist_ok=True)   # once, and only when configured
            self._media_dir_made = True
        files = media_files(self.media_dir("videos"), VIDEO_EXTS)
        count = int(self.config.get("highlights_count", 0))
        if count:
            folder = os.path.expanduser(self.config.get("highlights_dir", "~/Stream/recordings"))
            replays = [f for f in glob.glob(os.path.join(folder, "Replay*")) if f.lower().endswith(VIDEO_EXTS)]
            files += sorted(replays, key=os.path.getmtime)[-count:]
        return files

    @task(every=2)
    def videos_tick(self):
        """Play the video folder one file at a time, shuffled, so the on-screen credit matches."""
        files = self.playlist()
        try:
            if bool(files) != self.videos_shown:
                self.set_enabled("BRB Video", bool(files))
                self.set_enabled("BRB Video Placeholder", not files)
                self.videos_shown = bool(files)
            if not files:
                if self.video_current:
                    self.load_video(None)
                return
            state = self.obs("GetMediaInputStatus", {"inputName": "BRB Video"})["mediaState"]
        except Exception as e:
            self.log.debug("BRB video: %s", e)
            return
        # Only advance when a video actually finished: an off-air (paused) source can report NONE.
        if self.video_current not in files or state in ("OBS_MEDIA_STATE_ENDED", "OBS_MEDIA_STATE_ERROR"):
            self.next_video(files)

    def next_video(self, files=None):
        files = files if files is not None else self.playlist()
        self.video_queue = [f for f in self.video_queue if f in files]
        for f in set(files) - self.video_seen - set(self.video_queue) - {self.video_current}:
            self.video_queue.insert(random.randint(0, len(self.video_queue)), f)  # new files join the shuffle
        self.video_seen = set(files)
        if not self.video_queue:
            self.video_queue = random.sample(files, len(files))
            if len(self.video_queue) > 1 and self.video_queue[0] == self.video_current:
                self.video_queue.append(self.video_queue.pop(0))  # no immediate repeat
        self.load_video(self.video_queue.pop(0) if self.video_queue else None)

    def load_video(self, path):
        self.video_current = path
        self.obs("SetInputSettings", {"inputName": "BRB Video", "inputSettings": {
            "playlist": [{"value": path, "hidden": False, "selected": False}] if path else [],
            "loop": False, "shuffle": False, "playback_behavior": "pause_unpause"}})
        self.write("videocredit", self.video_credit(path) if path else "")

    def video_credit(self, path):
        """Line from credits.yaml next to the video (CC BY needs it on screen), else its title."""
        credits_file = os.path.join(os.path.dirname(path), "credits.yaml")
        try:
            mtime = os.path.getmtime(credits_file)
            if self.video_credits[0] != mtime:
                with open(credits_file) as f:
                    data = yaml.safe_load(f)
                self.video_credits = (mtime, data if isinstance(data, dict) else {})
            credit = self.video_credits[1].get(os.path.basename(path))
            credit = credit if isinstance(credit, str) else None
        except (OSError, yaml.YAMLError):
            credit = None
        return credit or os.path.splitext(os.path.basename(path))[0]

    @action("brb_video", ui=UI("BRB video", "BRB screen", arg=Choice(("next", "Next video"))))
    def video_control(self, arg):
        """brb_video: next"""
        self.next_video()

    # ----- deck states & displays

    @state("brb", ui=UI("BRB screen", "BRB screen", arg=Choice(("hype_full", "hype meter is full"),
                                                               ("game_open", "a poll / game is running"))))
    def brb_state(self, what, spec):
        """state: {brb: hype_full | game_open}"""
        if what == "hype_full":
            return time.time() < self.hype_full_until
        if what == "game_open":
            return bool(self.game and self.game["open"])
        raise ValueError(f"unknown brb state {what!r}")

    @display("brb_hype", ui=UI("Hype meter level", "BRB screen"))
    def hype_display(self, spec):
        if time.time() < self.hype_full_until:
            return {"label": "HYPE\nMAXED!", "bg": "#c01010"}
        return {"label": f"Hype\n{self.hype:.0f}%"}

    @display("brb_game", ui=UI("Poll / game status", "BRB screen"))
    def game_display(self, spec):
        g = self.game
        if not g:
            return {}
        n = len(g["votes"] if g["type"] == "poll" else g["guesses"])
        if g["open"]:
            return {"label": f"End game\n{n} in", "bg": "#2d7d46"}
        return {"label": "Clear\nresult", "bg": "#4e5058"}


def setup(bot):
    bot.add_module(BRB(bot))
