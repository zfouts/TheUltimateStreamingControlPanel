"""modules/radio.py: station listing, next/previous/stop actions and their OBS calls, state/display."""
import json
import os
import unittest
from unittest import mock

from tests.helpers import tmpdir

from modules import radio as radio_mod  # noqa: E402
from modules.radio import Radio, track_name  # noqa: E402


class OBSRequestError(Exception):
    """Stands in for the SDK error; radio matches on the class name and .code."""
    code = 600


OBSRequestError.__name__ = "OBSSDKRequestError"


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tmpdir()
        self.state = os.path.join(tmpdir(), "radio.json")
        p = mock.patch.object(radio_mod, "state_file", lambda name: self.state)
        p.start()
        self.addCleanup(p.stop)
        self.obs = mock.Mock()
        self.obs.send.return_value = {}
        self.bot = mock.Mock()
        self.bot.get_module.side_effect = lambda n: self.obs if n == "OBS" else None

    def make(self, **config):
        r = Radio(self.bot)
        r.on_config({"dir": self.root, **config})
        r.on_load()
        return r

    def station(self, name, *files):
        d = os.path.join(self.root, name)
        os.makedirs(d, exist_ok=True)
        paths = []
        for f in files:
            p = os.path.join(d, f)
            open(p, "w").close()
            paths.append(p)
        return paths

    def loaded(self):
        """local_file of the most recent SetInputSettings call."""
        for c in reversed(self.obs.send.call_args_list):
            if c.args[0] == "SetInputSettings":
                return c.args[1]["inputSettings"]["local_file"]
        return None


class Stations(Base):
    def test_lists_folders_sorted(self):
        self.station("Lofi")
        self.station("Elevator")
        open(os.path.join(self.root, "stray.mp3"), "w").close()
        self.assertEqual(self.make().stations(), ["Elevator", "Lofi"])

    def test_hidden_folders_skipped(self):
        self.station(".git")
        self.station("Real")
        self.assertEqual(self.make().stations(), ["Real"])

    def test_missing_dir_is_empty_and_not_created_when_default(self):
        r = Radio(self.bot)
        r.on_config({})
        r.on_load()
        with mock.patch.object(Radio, "root", os.path.join(self.root, "nope")), \
                mock.patch("os.makedirs") as mk:
            self.assertEqual(r.stations(), [])
        mk.assert_not_called()

    def test_configured_missing_dir_created_once(self):
        root = os.path.join(self.root, "fresh")
        r = self.make(dir=root)
        self.assertEqual(r.stations(), [])
        self.assertTrue(os.path.isdir(root))
        with mock.patch("os.makedirs") as mk:
            r.stations()
        mk.assert_not_called()

    def test_tracks_filtered_by_extension_and_sorted(self):
        b, a, _, _ = self.station("S", "b.MP3", "a.ogg", "cover.jpg", "credits.yaml")
        r = self.make()
        self.assertEqual(r.tracks("S"), [a, b])
        self.assertEqual(r.tracks("Missing"), [])

    def test_choices_source(self):
        self.station("A")
        self.assertEqual(self.make().station_choices(), ["A"])

    def test_track_name(self):
        self.assertEqual(track_name("/x/y/Some_Cool_Song.mp3"), "Some Cool Song")

    def test_station_slot_expander(self):
        self.station("Lofi", "a.mp3", "b.mp3")
        r = self.make()
        btn = r.station_button(1, {"label": "custom"})
        self.assertEqual(btn["action"], {"radio": {"station": "Lofi"}})
        self.assertEqual(btn["label"], "custom")        # btn overrides
        self.assertEqual(r.station_button(1, {})["label"], "Lofi\n2 tracks")
        self.assertIsNone(r.station_button(2, {}))

    def test_snapshot_lists_and_caches(self):
        self.station("A")
        r = self.make()
        s = r.snapshot()
        self.assertEqual(s["stations"], ["A"])
        self.assertIsNone(s["now_playing"])
        self.station("B")
        self.assertEqual(r.snapshot()["stations"], ["A"])   # cached for 30 s


class Persistence(Base):
    def test_saved_station_restored(self):
        with open(self.state, "w") as f:
            json.dump({"station": "Lofi", "on": False}, f)
        r = self.make(default_station="Elevator")
        self.assertEqual((r.station, r.on), ("Lofi", False))

    def test_default_station_and_corrupt_state(self):
        with open(self.state, "w") as f:
            f.write("{not json")
        r = self.make(default_station="Elevator")
        self.assertEqual((r.station, r.on), ("Elevator", True))

    def test_control_saves(self):
        self.station("A", "1.mp3")
        r = self.make()
        r.control({"station": "A"})
        self.assertEqual(json.load(open(self.state)), {"station": "A", "on": True})


class Actions(Base):
    def setUp(self):
        super().setUp()
        self.a_files = self.station("A", "1.mp3", "2.mp3")
        self.b_files = self.station("B", "3.mp3")
        self.c_files = self.station("C")

    def test_next_station_wraps(self):
        r = self.make()
        r.station = "B"
        r.control("next_station")
        self.assertEqual(r.station, "C")
        r.control("next_station")
        self.assertEqual(r.station, "A")

    def test_prev_station_wraps(self):
        r = self.make()
        r.station = "A"
        r.control("prev_station")
        self.assertEqual(r.station, "C")
        r.control("prev_station")
        self.assertEqual(r.station, "B")

    def test_station_switch_when_current_unknown(self):
        r = self.make()
        r.station = "Gone"
        r.control("next_station")
        self.assertEqual(r.station, "A")
        r.station = None
        r.control("prev_station")
        self.assertEqual(r.station, "B")    # unknown counts as index -1, so "previous" is the 2nd-last

    def test_station_switch_plays_from_new_station(self):
        r = self.make()
        r.station = "A"
        r.control("next_station")
        self.assertEqual(self.loaded(), self.b_files[0])
        data = self.obs.send.call_args.args[1]
        self.assertEqual(data["inputName"], "BRB Music")
        self.assertTrue(data["inputSettings"]["is_local_file"])
        self.assertFalse(data["inputSettings"]["looping"])

    def test_next_track_skips_without_repeating(self):
        r = self.make()
        r.station = "A"
        r.control("next")
        first = r.current
        r.control("next")
        self.assertNotEqual(r.current, first)
        self.assertEqual({first, r.current}, set(self.a_files))

    def test_custom_source(self):
        r = self.make(source="Jukebox")
        r.station = "A"
        r.control("next")
        self.assertEqual(self.obs.send.call_args.args[1]["inputName"], "Jukebox")

    def test_off_stops_playback(self):
        r = self.make()
        r.station = "A"
        r.control("next")
        r.control("off")
        self.assertFalse(r.on)
        self.assertIsNone(r.current)
        self.assertEqual(self.loaded(), "")

    def test_on_resumes_and_toggle_flips(self):
        r = self.make()
        r.station = "A"
        r.control("off")
        r.control("on")
        self.assertTrue(r.on)
        self.assertIn(self.loaded(), self.a_files)
        r.control("toggle")
        self.assertFalse(r.on)
        r.control("toggle")
        self.assertTrue(r.on)

    def test_named_station_turns_on(self):
        r = self.make()
        r.on = False
        r.control({"station": "B"})
        self.assertEqual((r.station, r.on), ("B", True))
        self.assertEqual(self.loaded(), self.b_files[0])

    def test_unknown_station_rejected_without_change(self):
        r = self.make()
        r.station = "A"
        with self.assertRaises(ValueError):
            r.control({"station": "Nope"})
        self.assertEqual(r.station, "A")
        self.obs.send.assert_not_called()

    def test_unknown_command_rejected(self):
        r = self.make()
        with self.assertRaises(ValueError):
            r.control("explode")
        self.obs.send.assert_not_called()

    def test_empty_station_loads_nothing(self):
        r = self.make()
        r.station = "C"
        r.control("next")
        self.assertIsNone(r.current)
        self.assertEqual(self.loaded(), "")

    def test_no_station_dirs_station_switch_rejected(self):
        # With no stations, next_station falls through to the "unknown radio action" error
        # (a misleading message, but nothing changes and OBS isn't touched).
        r = Radio(self.bot)
        r.on_config({"dir": os.path.join(self.root, "empty")})
        r.on_load()
        r.station = None
        with self.assertRaises(ValueError):
            r.control("next_station")
        self.assertIsNone(r.station)
        self.obs.send.assert_not_called()

    def test_obs_missing_raises(self):
        self.bot.get_module.side_effect = lambda n: None
        r = self.make()
        r.station = "A"
        with self.assertRaisesRegex(RuntimeError, "OBS module not loaded"):
            r.control("next")

    def test_missing_source_gives_helpful_error(self):
        self.obs.send.side_effect = OBSRequestError("nope")
        r = self.make()
        r.station = "A"
        with self.assertRaisesRegex(RuntimeError, "no 'BRB Music' source"):
            r.control("next")
        self.assertIsNone(r.current)

    def test_other_obs_errors_propagate(self):
        self.obs.send.side_effect = ConnectionError("down")
        r = self.make()
        r.station = "A"
        with self.assertRaises(ConnectionError):
            r.control("next")

    def test_new_file_joins_shuffle(self):
        r = self.make()
        r.station = "A"
        r.control("next")
        new = self.station("A", "9.mp3")[0]
        seen = set()
        for _ in range(3):
            r.control("next")
            seen.add(r.current)
        self.assertIn(new, seen)


class Tick(Base):
    def setUp(self):
        super().setUp()
        self.files = self.station("A", "1.mp3")

    def status(self, state):
        self.obs.send.side_effect = lambda req, data=None: (
            {"mediaState": state} if req == "GetMediaInputStatus" else {})

    def test_picks_first_station_when_unset_or_gone(self):
        self.status("OBS_MEDIA_STATE_PLAYING")
        r = self.make()
        r.station = "Gone"
        r.tick()
        self.assertEqual(r.station, "A")
        self.assertEqual(r.current, self.files[0])

    def test_starts_when_nothing_playing(self):
        self.status("OBS_MEDIA_STATE_NONE")
        r = self.make()
        r.tick()
        self.assertEqual(r.current, self.files[0])

    def test_leaves_playing_track_alone(self):
        self.status("OBS_MEDIA_STATE_PLAYING")
        r = self.make()
        r.tick()
        n = self.obs.send.call_count
        r.tick()
        self.assertFalse([c for c in self.obs.send.call_args_list[n:] if c.args[0] == "SetInputSettings"])

    def test_advances_when_ended(self):
        self.status("OBS_MEDIA_STATE_PLAYING")
        r = self.make()
        r.tick()
        self.status("OBS_MEDIA_STATE_ENDED")
        r.tick()
        calls = [c for c in self.obs.send.call_args_list if c.args[0] == "SetInputSettings"]
        self.assertEqual(len(calls), 2)

    def test_off_clears_source_once(self):
        self.status("OBS_MEDIA_STATE_PLAYING")
        r = self.make()
        r.tick()
        r.on = False
        r.tick()
        self.assertIsNone(r.current)
        self.assertEqual(self.loaded(), "")
        n = self.obs.send.call_count
        r.tick()
        self.assertEqual(self.obs.send.call_count, n)

    def test_obs_status_failure_is_ignored(self):
        self.obs.send.side_effect = RuntimeError("down")
        r = self.make()
        r.tick()
        self.assertIsNone(r.current)

    def test_no_stations(self):
        r = Radio(self.bot)
        r.on_config({"dir": os.path.join(self.root, "none")})
        r.on_load()
        r.tick()
        self.assertIsNone(r.station)
        self.obs.send.assert_not_called()


class Credits(Base):
    def test_credit_from_yaml(self):
        p = self.station("A", "song.mp3")[0]
        with open(os.path.join(self.root, "A", "credits.yaml"), "w") as f:
            f.write('"song.mp3": "by Artist · CC0"\n"other.mp3": "x"\n')
        self.assertEqual(self.make().credit(p), "by Artist · CC0")

    def test_missing_or_bad_yaml(self):
        p = self.station("A", "song.mp3")[0]
        r = self.make()
        self.assertEqual(r.credit(p), "")
        with open(os.path.join(self.root, "A", "credits.yaml"), "w") as f:
            f.write("a: [unclosed\n")
        self.assertEqual(r.credit(p), "")

    # BUG: a credits.yaml that parses to a list/string makes credit() raise AttributeError
    # (only OSError/YAMLError are caught), which breaks snapshot() and now_playing().
    def test_non_mapping_yaml(self):
        p = self.station("A", "song.mp3")[0]
        with open(os.path.join(self.root, "A", "credits.yaml"), "w") as f:
            f.write("- a\n- b\n")
        self.assertEqual(self.make().credit(p), "")


class StateAndDisplay(Base):
    def setUp(self):
        super().setUp()
        self.files = self.station("A", "Nice_Song.mp3")
        with open(os.path.join(self.root, "A", "credits.yaml"), "w") as f:
            f.write('"Nice_Song.mp3": "by Me"\n')

    def test_state(self):
        r = self.make()
        r.station = "A"
        self.assertTrue(r.radio_state("on", {}))
        self.assertTrue(r.radio_state("A", {}))
        self.assertFalse(r.radio_state("B", {}))
        r.on = False
        self.assertFalse(r.radio_state("on", {}))
        self.assertFalse(r.radio_state("A", {}))

    def test_display_station(self):
        r = self.make()
        r.station = "A"
        self.assertEqual(r.radio_display({}), {"label": "A"})
        r.station = None
        self.assertEqual(r.radio_display({}), {"label": "No stations"})

    def test_display_track(self):
        r = self.make()
        r.station = "A"
        self.assertEqual(r.radio_display({"track": True}), {"label": "A"})   # nothing loaded yet
        r.control("next")
        self.assertEqual(r.radio_display({"track": True}), {"label": "Nice Song"})
        self.assertEqual(r.radio_display({}), {"label": "A"})

    def test_display_off(self):
        r = self.make()
        r.on = False
        self.assertEqual(r.radio_display({"track": True}), {"label": "Radio off"})

    def test_snapshot_now_playing(self):
        r = self.make()
        r.station = "A"
        r.control("next")
        self.assertEqual(r.snapshot()["now_playing"], "A · Nice Song by Me")
        r.on = False
        self.assertIsNone(r.snapshot()["now_playing"])

    def test_now_playing_asks_obs(self):
        r = self.make()
        r.station = "A"
        r.control("next")
        self.obs.send.side_effect = lambda req, data=None: {"mediaState": "OBS_MEDIA_STATE_PLAYING"}
        self.assertEqual(r.now_playing(), "A · Nice Song by Me")
        self.obs.send.side_effect = lambda req, data=None: {"mediaState": "OBS_MEDIA_STATE_PAUSED"}
        self.assertIsNone(r.now_playing())
        self.obs.send.side_effect = RuntimeError("down")
        self.assertIsNone(r.now_playing())

    def test_now_playing_idle(self):
        self.assertIsNone(self.make().now_playing())


if __name__ == "__main__":
    unittest.main()
