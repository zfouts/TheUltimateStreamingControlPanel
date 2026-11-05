"""Controls MPRIS media players (Spotify, Firefox, VLC, ...) over D-Bus."""
import re
import shlex

from core import Module, action, sh, state
from core.schema import UI, Choice


class Media(Module):
    TIMEOUT = 1   # seconds per dbus-send: `state: {media: playing}` runs one per player on the main loop
    BASE = "dbus-send --session --print-reply --dest="
    METHODS = {"play_pause": "PlayPause", "next": "Next", "previous": "Previous",
               "prev": "Previous", "stop": "Stop", "play": "Play", "pause": "Pause"}

    def _dbus(self, cmd):
        """dbus-send's output; TimeoutError if it hung (so a polled state is backed off, not read as "off")."""
        code, out = sh(cmd, timeout=self.TIMEOUT)
        if code == 124:
            raise TimeoutError(f"dbus-send didn't answer within {self.TIMEOUT}s")
        return out

    def players(self):
        out = self._dbus("dbus-send --session --print-reply --dest=org.freedesktop.DBus "
                         "/org/freedesktop/DBus org.freedesktop.DBus.ListNames")
        return [l.split('"')[1] for l in out.splitlines() if '"org.mpris.MediaPlayer2.' in l]

    def status(self, player):
        out = self._dbus(f"{self.BASE}{shlex.quote(player)} /org/mpris/MediaPlayer2 org.freedesktop.DBus.Properties.Get "
                         "string:org.mpris.MediaPlayer2.Player string:PlaybackStatus")
        return out.rsplit('"', 2)[-2] if out.count('"') >= 2 else ""

    def active_player(self):
        players = self.players()
        playing = [p for p in players if self.status(p) == "Playing"]
        return (playing or players or [None])[0]

    def now_playing(self):
        """"Artist – Title" of the player that's playing, or None."""
        for player in self.players():
            if self.status(player) != "Playing":
                continue
            out = self._dbus(f"{self.BASE}{shlex.quote(player)} /org/mpris/MediaPlayer2 org.freedesktop.DBus.Properties.Get "
                             "string:org.mpris.MediaPlayer2.Player string:Metadata")
            title = re.search(r'"xesam:title"\s*variant\s+string "(.*)"', out)
            artist = re.search(r'"xesam:artist"\s*variant\s+array \[\s*string "(.*)"', out)
            if title:
                return f"{artist.group(1)} – {title.group(1)}" if artist and artist.group(1) else title.group(1)
        return None

    @action("media", ui=UI("Media player", "Desktop", arg=Choice(
        ("play_pause", "Play / pause"), ("next", "Next track"), ("previous", "Previous track"), ("stop", "Stop"))))
    def media(self, cmd):
        """media: play_pause | next | previous | stop | play | pause"""
        player = self.active_player()
        if player:
            self._dbus(f"{self.BASE}{shlex.quote(player)} /org/mpris/MediaPlayer2 "
                       f"org.mpris.MediaPlayer2.Player.{self.METHODS[cmd]}")

    @state("media", ui=UI("Music is playing", "Desktop", arg=Choice(("playing", "playing"))))
    def media_state(self, what, spec):
        """state: {media: playing}"""
        if what != "playing":
            raise ValueError(f"unknown media state {what!r}")
        return any(self.status(p) == "Playing" for p in self.players())


def setup(bot):
    bot.add_module(Media(bot))
