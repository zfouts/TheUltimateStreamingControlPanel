"""modules/media.py: MPRIS player discovery over dbus-send, commands, timeouts, missing binary."""
import unittest
from unittest import mock

from tests.helpers import ROOT  # noqa: F401  (sandbox + sys.path)

from modules import media as media_mod  # noqa: E402
from modules.media import Media  # noqa: E402

LIST_NAMES = '''method return time=1.5 sender=org.freedesktop.DBus -> destination=:1.99 serial=3 reply_serial=2
   array [
      string "org.freedesktop.DBus"
      string ":1.5"
      string "org.mpris.MediaPlayer2.spotify"
      string "org.mpris.MediaPlayer2.firefox.instance_1_42"
      string "org.gnome.Shell"
   ]'''


def status_reply(s):
    return f'method return time=1 sender=:1.7 -> destination=:1.99 serial=4 reply_serial=2\n   variant       string "{s}"'


def metadata(title=None, artist=None):
    lines = ["method return time=1 sender=:1.7 -> destination=:1.99 serial=4 reply_serial=2",
             "   variant       array ["]
    if artist is not None:
        lines += ['      dict entry(', '         string "xesam:artist"',
                  f'         variant             array [\n               string "{artist}"\n            ]', '      )']
    if title is not None:
        lines += ['      dict entry(', '         string "xesam:title"', f'         variant             string "{title}"', '      )']
    lines.append("   ]")
    return "\n".join(lines)


class FakeDBus:
    """Answers sh() by command: ListNames, PlaybackStatus per player, Metadata, and records the rest."""

    def __init__(self, players=(), statuses=None, meta=None):
        self.players = list(players)
        self.statuses = statuses or {}
        self.meta = meta or {}
        self.calls = []

    def __call__(self, cmd, timeout=5):
        self.calls.append((cmd, timeout))
        if "ListNames" in cmd:
            return 0, "\n".join(f'      string "{p}"' for p in self.players)
        for p in self.players:
            if f"--dest={p} " in cmd:
                if "PlaybackStatus" in cmd:
                    return 0, status_reply(self.statuses.get(p, "Stopped"))
                if "string:Metadata" in cmd:
                    return 0, self.meta.get(p, "")
        return 0, ""

    def commands(self):
        return [c for c, _ in self.calls if "MediaPlayer2.Player." in c]


def make():
    m = Media(mock.Mock())
    m.on_config({})
    return m


class Discovery(unittest.TestCase):
    def test_parses_player_names(self):
        with mock.patch.object(media_mod, "sh", return_value=(0, LIST_NAMES)):
            self.assertEqual(make().players(), ["org.mpris.MediaPlayer2.spotify",
                                                "org.mpris.MediaPlayer2.firefox.instance_1_42"])

    def test_no_players(self):
        with mock.patch.object(media_mod, "sh", return_value=(0, 'array [\n string "org.gnome.Shell"\n]')):
            self.assertEqual(make().players(), [])

    def test_ignores_lines_that_only_mention_mpris_unquoted(self):
        with mock.patch.object(media_mod, "sh", return_value=(0, "error: org.mpris.MediaPlayer2.x not found")):
            self.assertEqual(make().players(), [])

    def test_status_parsing(self):
        m = make()
        with mock.patch.object(media_mod, "sh", return_value=(0, status_reply("Playing"))):
            self.assertEqual(m.status("p"), "Playing")
        for out in ("", "garbage", 'one " quote'):
            with mock.patch.object(media_mod, "sh", return_value=(0, out)):
                self.assertEqual(m.status("p"), "")

    def test_active_player_prefers_playing(self):
        d = FakeDBus(["org.mpris.MediaPlayer2.a", "org.mpris.MediaPlayer2.b"],
                     {"org.mpris.MediaPlayer2.a": "Paused", "org.mpris.MediaPlayer2.b": "Playing"})
        with mock.patch.object(media_mod, "sh", d):
            self.assertEqual(make().active_player(), "org.mpris.MediaPlayer2.b")

    def test_active_player_falls_back_to_first(self):
        d = FakeDBus(["org.mpris.MediaPlayer2.a", "org.mpris.MediaPlayer2.b"])
        with mock.patch.object(media_mod, "sh", d):
            self.assertEqual(make().active_player(), "org.mpris.MediaPlayer2.a")

    def test_active_player_none(self):
        with mock.patch.object(media_mod, "sh", FakeDBus()):
            self.assertIsNone(make().active_player())


class Commands(unittest.TestCase):
    P = "org.mpris.MediaPlayer2.spotify"

    def run_cmd(self, cmd, d=None):
        d = d or FakeDBus([self.P], {self.P: "Playing"})
        with mock.patch.object(media_mod, "sh", d):
            make().media(cmd)
        return d

    def test_each_command_maps_to_method(self):
        for cmd, method in Media.METHODS.items():
            d = self.run_cmd(cmd)
            self.assertEqual(len(d.commands()), 1, cmd)
            self.assertTrue(d.commands()[0].endswith(f"org.mpris.MediaPlayer2.Player.{method}"), cmd)

    def test_command_targets_active_player(self):
        d = self.run_cmd("play_pause")
        cmd = d.commands()[0]
        self.assertTrue(cmd.startswith(f"dbus-send --session --print-reply --dest={self.P} /org/mpris/MediaPlayer2 "))

    def test_no_player_is_noop(self):
        d = self.run_cmd("next", FakeDBus())
        self.assertEqual(d.commands(), [])

    def test_unknown_command(self):
        with mock.patch.object(media_mod, "sh", FakeDBus([self.P])):
            with self.assertRaises(KeyError):
                make().media("explode")

    def test_player_names_with_unusual_characters_pass_through(self):
        # Legal bus-name characters: letters, digits, '_', '-', '.'
        name = "org.mpris.MediaPlayer2.my-player_2.instance_1_2345"
        d = FakeDBus([name], {name: "Playing"})
        with mock.patch.object(media_mod, "sh", d):
            make().media("pause")
        self.assertIn(f"--dest={name} ", d.commands()[0])

    def test_names_listed_with_spaces_are_not_split(self):
        # a player name with a space isn't a valid bus name, but must not be cut at the space
        name = "org.mpris.MediaPlayer2.odd name"
        with mock.patch.object(media_mod, "sh", FakeDBus([name])):
            self.assertEqual(make().players(), [name])

    # BUG (hardening): the player name read from dbus-send output is interpolated into a
    # `shell=True` command unquoted, so shell metacharacters in a name would be executed.
    # The dbus daemon only permits [A-Za-z0-9_.-] in bus names, so this isn't reachable today.
    def test_shell_metacharacters_in_name_are_quoted(self):
        name = "org.mpris.MediaPlayer2.x;touch${IFS}pwned"
        d = FakeDBus([name], {name: "Playing"})
        with mock.patch.object(media_mod, "sh", d):
            make().media("next")
        self.assertNotIn(f"--dest={name} ", d.commands()[0])
        self.assertIn("'", d.commands()[0])


class NowPlayingAndState(unittest.TestCase):
    A, B = "org.mpris.MediaPlayer2.a", "org.mpris.MediaPlayer2.b"

    def test_state_playing(self):
        with mock.patch.object(media_mod, "sh", FakeDBus([self.A, self.B], {self.B: "Playing"})):
            self.assertTrue(make().media_state("playing", {}))
        with mock.patch.object(media_mod, "sh", FakeDBus([self.A, self.B], {self.A: "Paused"})):
            self.assertFalse(make().media_state("playing", {}))
        with mock.patch.object(media_mod, "sh", FakeDBus()):
            self.assertFalse(make().media_state("playing", {}))

    def test_state_unknown(self):
        with self.assertRaises(ValueError):
            make().media_state("sleeping", {})

    def test_now_playing_artist_and_title(self):
        d = FakeDBus([self.A], {self.A: "Playing"}, {self.A: metadata("Song", "Band")})
        with mock.patch.object(media_mod, "sh", d):
            self.assertEqual(make().now_playing(), "Band – Song")

    def test_now_playing_title_only_or_empty_artist(self):
        for meta in (metadata("Song"), metadata("Song", "")):
            d = FakeDBus([self.A], {self.A: "Playing"}, {self.A: meta})
            with mock.patch.object(media_mod, "sh", d):
                self.assertEqual(make().now_playing(), "Song")

    def test_now_playing_skips_paused_and_untitled(self):
        d = FakeDBus([self.A, self.B], {self.A: "Playing", self.B: "Playing"},
                     {self.A: metadata(None, "Band"), self.B: metadata("Real", "Artist")})
        with mock.patch.object(media_mod, "sh", d):
            self.assertEqual(make().now_playing(), "Artist – Real")
        d = FakeDBus([self.A], {self.A: "Paused"}, {self.A: metadata("Song")})
        with mock.patch.object(media_mod, "sh", d):
            self.assertIsNone(make().now_playing())


class Failures(unittest.TestCase):
    def test_timeout_raises_and_uses_1s(self):
        m = make()
        with mock.patch.object(media_mod, "sh", return_value=(124, "")) as sh:
            with self.assertRaises(TimeoutError):
                m.players()
            self.assertEqual(sh.call_args.kwargs["timeout"], 1)
            with self.assertRaises(TimeoutError):
                m.media_state("playing", {})
            with self.assertRaises(TimeoutError):
                m.media("next")

    def test_timeout_mid_status_raises(self):
        def sh(cmd, timeout=5):
            return (0, 'string "org.mpris.MediaPlayer2.a"') if "ListNames" in cmd else (124, "")
        with mock.patch.object(media_mod, "sh", sh):
            with self.assertRaises(TimeoutError):
                make().media_state("playing", {})

    def test_every_call_passes_timeout(self):
        d = FakeDBus(["org.mpris.MediaPlayer2.a"], {"org.mpris.MediaPlayer2.a": "Playing"},
                     {"org.mpris.MediaPlayer2.a": metadata("S")})
        m = make()
        with mock.patch.object(media_mod, "sh", d):
            m.media("next")
            m.now_playing()
            m.media_state("playing", {})
        self.assertTrue(d.calls)
        self.assertEqual({t for _, t in d.calls}, {Media.TIMEOUT})

    def test_missing_dbus_send_binary(self):
        # the shell reports "command not found" as exit 127 with no stdout
        m = make()
        with mock.patch.object(media_mod, "sh", return_value=(127, "")):
            self.assertEqual(m.players(), [])
            self.assertIsNone(m.active_player())
            self.assertIsNone(m.now_playing())
            self.assertFalse(m.media_state("playing", {}))
            m.media("play_pause")     # no exception

    def test_missing_dbus_send_through_real_sh(self):
        # real core.util.sh with an empty PATH: no dbus-send to find, nothing is spawned
        import os
        from core import util
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}), \
                mock.patch.object(media_mod, "sh", util.sh):
            self.assertEqual(make().players(), [])


if __name__ == "__main__":
    unittest.main()
