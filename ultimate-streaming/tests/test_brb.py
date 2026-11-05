"""modules/brb.py: chat feed, poll and guess games, hype meter, video listing and credits."""
import os
import unittest
from unittest import mock

from tests.helpers import tmpdir

from modules import brb as brb_mod  # noqa: E402
from modules.brb import BRB, bar  # noqa: E402


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class Overlay:
    def __init__(self):
        self.files = {}
        self.history = []

    def write(self, name, text):
        self.files[name] = text
        self.history.append((name, text))


class Base(unittest.TestCase):
    def setUp(self):
        self.overlay = Overlay()
        self.modules = {"Overlay": self.overlay}
        self.bot = mock.Mock()
        self.bot.get_module.side_effect = lambda n: self.modules.get(n)
        self.clock = Clock()
        p = mock.patch.object(brb_mod.time, "time", self.clock)
        p.start()
        self.addCleanup(p.stop)
        self.notes = []
        p = mock.patch.object(brb_mod, "notify", self.notes.append)
        p.start()
        self.addCleanup(p.stop)

    def make(self, **config):
        b = BRB(self.bot)
        b.on_config(config)
        b.on_load()
        return b

    def say(self, b, user, text):
        b.on_chat({"user": user, "text": text})


class ChatFeed(Base):
    def test_feed_written_to_overlay(self):
        b = self.make()
        self.say(b, "ann", "hello")
        self.say(b, "bob", "  hi there  ")
        self.assertEqual(self.overlay.files["chat"], "ann: hello\nbob: hi there")

    def test_feed_capped_to_chat_lines(self):
        b = self.make(chat_lines=3)
        for i in range(5):
            self.say(b, "u", f"m{i}")
        self.assertEqual(self.overlay.files["chat"], "u: m2\nu: m3\nu: m4")

    def test_default_cap_is_seven(self):
        b = self.make()
        for i in range(10):
            self.say(b, "u", f"m{i}")
        self.assertEqual(len(self.overlay.files["chat"].splitlines()), 7)

    def test_long_message_truncated(self):
        b = self.make()
        self.say(b, "u", "x" * 200)
        line = self.overlay.files["chat"]
        self.assertEqual(line, "u: " + "x" * 87 + "…")

    def test_90_chars_not_truncated(self):
        b = self.make()
        self.say(b, "u", "y" * 90)
        self.assertEqual(self.overlay.files["chat"], "u: " + "y" * 90)

    def test_no_overlay_module_is_fine(self):
        self.modules.clear()
        b = self.make()
        self.say(b, "u", "hi")   # no exception
        self.assertEqual(len(b.chat), 1)

    def test_initial_text_written_on_first_refresh(self):
        b = self.make()
        b.refresh()
        self.assertEqual(self.overlay.files["chat"], "Chat messages show up here")
        self.assertEqual(self.overlay.files["vote"], brb_mod.GAME_IDLE)
        self.assertTrue(b.initialised)

    def test_writes_happen_outside_lock(self):
        b = self.make()
        held = []
        b.write = lambda name, text: held.append((name, b.lock.locked()))
        self.say(b, "a", "hi")
        b.start_poll({"question": "q", "options": ["a", "b"]})
        b.hype_control("fill")
        b.refresh()
        self.assertTrue(held)
        self.assertEqual([h for h in held if h[1]], [])
        self.assertIn(("chat", False), held)
        self.assertIn(("vote", False), held)
        self.assertIn(("hype", False), held)

    # BUG: game_control("clear") calls self.write("vote", GAME_IDLE) while holding self.lock.
    def test_clear_writes_outside_lock(self):
        b = self.make()
        held = []
        b.write = lambda name, text: held.append(b.lock.locked())
        b.start_poll({"question": "q", "options": ["a", "b"]})
        b.game_control("clear")
        self.assertEqual(held, [False])


class Poll(Base):
    def poll(self, b, options=("A", "B", "C")):
        b.start_poll({"question": "Next?", "options": list(options)})

    def test_poll_from_config_index(self):
        b = self.make(polls=[{"question": "Q1", "options": ["x", "y"]}])
        b.start_poll(0)
        self.assertEqual(b.game["question"], "Q1")

    def test_option_count_limits(self):
        b = self.make()
        with self.assertRaises(ValueError):
            b.start_poll({"question": "q", "options": ["only"]})
        with self.assertRaises(ValueError):
            b.start_poll({"question": "q", "options": list("abcdefghij")})
        b.start_poll({"question": "q", "options": list("abcdefghi")})   # 9 is allowed
        self.assertTrue(b.game["open"])

    def test_votes_with_and_without_bang(self):
        b = self.make()
        self.poll(b)
        self.say(b, "a", "!1")
        self.say(b, "b", "2")
        self.assertEqual(b.game["votes"], {"a": 0, "b": 1})

    def test_revote_replaces(self):
        b = self.make()
        self.poll(b)
        self.say(b, "a", "!1")
        self.say(b, "a", "!3")
        self.assertEqual(b.game["votes"], {"a": 2})

    def test_out_of_range_and_junk_ignored(self):
        b = self.make()
        self.poll(b)
        for text in ("!4", "!0", "!10", "!1 please", "I vote 1", "!a"):
            self.say(b, "a", text)
        self.assertEqual(b.game["votes"], {})

    def test_vote_case_insensitive_and_stripped(self):
        b = self.make()
        self.poll(b)
        self.say(b, "a", "  !2  ")
        self.assertEqual(b.game["votes"], {"a": 1})

    def test_vote_text_written_on_refresh(self):
        b = self.make()
        self.poll(b)
        self.say(b, "a", "!2")
        b.refresh()
        text = self.overlay.files["vote"]
        self.assertTrue(text.startswith("POLL: Next?"))
        self.assertIn("Type !1–!3 in chat · 1 vote", text)
        self.assertNotIn("1 votes", text)
        self.assertFalse(b.game.get("dirty"))

    def test_winner(self):
        b = self.make()
        self.poll(b)
        for u, t in (("a", "!1"), ("b", "!2"), ("c", "!2")):
            self.say(b, u, t)
        b.game_control("end")
        self.assertEqual(self.notes, ["Poll winner: B (2 votes)"])
        self.assertFalse(b.game["open"])

    def test_single_vote_singular(self):
        b = self.make()
        self.poll(b)
        self.say(b, "a", "!3")
        self.assertEqual(b.winner_text(), "Poll winner: C (1 vote)")

    def test_tie_lists_all_leaders(self):
        b = self.make()
        self.poll(b)
        self.say(b, "a", "!1")
        self.say(b, "b", "!3")
        self.say(b, "c", "!2")
        self.say(b, "d", "!2")
        self.say(b, "e", "!1")
        self.say(b, "f", "!3")
        text = b.winner_text()
        self.assertTrue(text.startswith("Poll winner: "))
        self.assertEqual(set(text.split(": ")[1].split(" (")[0].split(" / ")), {"A", "B", "C"})
        self.assertTrue(text.endswith("(2 votes)"))

    def test_two_way_tie(self):
        b = self.make()
        self.poll(b)
        self.say(b, "a", "!1")
        self.say(b, "b", "!3")
        self.assertEqual(b.winner_text(), "Poll winner: A / C (1 vote)")

    def test_no_votes(self):
        b = self.make()
        self.poll(b)
        b.game_control("end")
        self.assertEqual(self.notes, ["Poll ended with no votes"])
        self.assertIn("Poll ended with no votes", b.game_text())
        self.assertTrue(b.game_text().startswith("RESULTS: Next?"))

    def test_closed_poll_ignores_votes(self):
        b = self.make()
        self.poll(b)
        b.game_control("end")
        self.say(b, "a", "!1")
        self.assertEqual(b.game["votes"], {})

    def test_snapshot_counts(self):
        b = self.make()
        self.poll(b, ("A", "B"))
        self.say(b, "a", "!1")
        self.say(b, "b", "!1")
        self.say(b, "c", "!2")
        g = b.snapshot()["game"]
        self.assertEqual(g["kind"], "poll")
        self.assertEqual(g["total_votes"], 3)
        self.assertEqual([o["votes"] for o in g["options"]], [2, 1])
        self.assertIsNone(g["winner"])
        b.game_control("end")
        self.assertEqual(b.snapshot()["game"]["winner"], "Poll winner: A (2 votes)")


class GuessGame(Base):
    def test_guesses_recorded(self):
        b = self.make()
        b.start_guess()
        self.say(b, "a", "!guess 5")
        self.say(b, "b", "!GUESS 7.5")
        self.say(b, "a", "!guess 10")    # replaces
        self.assertEqual(b.game["guesses"], {"a": 10.0, "b": 7.5})

    def test_bad_guesses_ignored(self):
        b = self.make()
        b.start_guess()
        for t in ("!guess", "!guess abc", "!guess -3", "!guess 5 min", "guess 5"):
            self.say(b, "a", t)
        self.assertEqual(b.game["guesses"], {})

    def test_poll_votes_not_counted_in_guess(self):
        b = self.make()
        b.start_guess()
        self.say(b, "a", "!1")
        self.assertEqual(b.game["guesses"], {})

    def test_closest_guess_wins(self):
        b = self.make()
        b.start_guess()
        self.say(b, "a", "!guess 5")
        self.say(b, "b", "!guess 12")
        self.clock.t += 600    # 10 min away
        b.game_control("end")
        self.assertEqual(self.notes, ["b wins! Guessed 12 min, actual 10.0 min"])

    def test_actual_time_frozen_at_end(self):
        b = self.make()
        b.start_guess()
        self.say(b, "a", "!guess 5")
        self.clock.t += 300
        b.game_control("end")
        self.clock.t += 6000
        self.assertEqual(b.winner_text(), "a wins! Guessed 5 min, actual 5.0 min")

    def test_tie_picks_first_guess(self):
        b = self.make()
        b.start_guess()
        self.say(b, "a", "!guess 4")
        self.say(b, "b", "!guess 6")
        self.clock.t += 300
        b.game_control("end")
        self.assertTrue(self.notes[0].startswith("a wins!"))

    def test_nobody_guessed(self):
        b = self.make()
        b.start_guess()
        self.clock.t += 90
        b.game_control("end")
        self.assertEqual(self.notes, ["Gone 1.5 min. Nobody guessed!"])
        self.assertIn("Nobody guessed!", b.game_text())

    def test_open_text_shows_recent_guesses(self):
        b = self.make()
        b.start_guess()
        for i in range(6):
            self.say(b, f"u{i}", f"!guess {i}")
        text = b.game_text()
        self.assertIn("6 guesses so far", text)
        self.assertIn("u5: 5 min", text)
        self.assertNotIn("u0:", text)

    def test_one_guess_singular(self):
        b = self.make()
        b.start_guess()
        self.say(b, "a", "!guess 3")
        self.assertIn("1 guess so far", b.game_text())

    def test_snapshot(self):
        b = self.make()
        b.start_guess()
        self.say(b, "a", "!guess 3")
        g = b.snapshot()["game"]
        self.assertEqual((g["kind"], g["guesses"], g["total_votes"], g["options"]), ("guess", 1, 0, []))


class GameControl(Base):
    def test_empty_state_snapshot(self):
        b = self.make()
        g = b.snapshot()["game"]
        self.assertEqual(g, {"kind": None, "question": "", "options": [], "total_votes": 0, "guesses": 0,
                             "ends_at": None, "winner": None})

    def test_clear_without_game_writes_idle(self):
        b = self.make()
        b.game_control("end")    # nothing running: behaves as clear
        self.assertIsNone(b.game)
        self.assertEqual(self.overlay.files["vote"], brb_mod.GAME_IDLE)
        self.assertEqual(self.notes, [])

    def test_next_ends_then_clears(self):
        b = self.make()
        b.start_poll({"question": "q", "options": ["a", "b"]})
        b.game_control("next")
        self.assertFalse(b.game["open"])
        b.game_control("next")
        self.assertIsNone(b.game)
        self.assertEqual(self.overlay.files["vote"], brb_mod.GAME_IDLE)

    def test_end_twice_notifies_once(self):
        b = self.make()
        b.start_poll({"question": "q", "options": ["a", "b"]})
        b.game_control("end")
        b.game_control("end")
        self.assertEqual(len(self.notes), 1)

    def test_states_and_displays(self):
        b = self.make()
        self.assertFalse(b.brb_state("game_open", {}))
        self.assertEqual(b.game_display({}), {})
        b.start_poll({"question": "q", "options": ["a", "b"]})
        self.say(b, "a", "!1")
        self.assertTrue(b.brb_state("game_open", {}))
        self.assertEqual(b.game_display({})["label"], "End game\n1 in")
        b.game_control("end")
        self.assertFalse(b.brb_state("game_open", {}))
        self.assertEqual(b.game_display({})["label"], "Clear\nresult")

    def test_unknown_state(self):
        with self.assertRaises(ValueError):
            self.make().brb_state("nope", {})

    def test_bar(self):
        self.assertEqual(bar(0), "░" * 12)
        self.assertEqual(bar(1), "█" * 12)
        self.assertEqual(bar(0.5, 4), "██░░")


class Hype(Base):
    def test_message_adds_default(self):
        b = self.make()
        self.say(b, "a", "hi")
        self.assertEqual(b.hype, 2)

    def test_command_adds_more(self):
        b = self.make()
        self.say(b, "a", "!hype")
        self.assertEqual(b.hype, 8)

    def test_configured_amounts(self):
        b = self.make(hype_per_message=5, hype_per_command=20)
        self.say(b, "a", "hi")
        self.say(b, "b", "!hype")
        self.assertEqual(b.hype, 25)

    def test_message_cooldown_3s(self):
        b = self.make()
        self.say(b, "a", "one")
        self.clock.t += 2.9
        self.say(b, "a", "two")
        self.assertEqual(b.hype, 2)
        self.clock.t += 0.2
        self.say(b, "a", "three")
        self.assertEqual(b.hype, 4)

    def test_command_cooldown_10s(self):
        b = self.make()
        self.say(b, "a", "!hype")
        self.clock.t += 9.9
        self.say(b, "a", "!hype")
        self.assertEqual(b.hype, 8)
        self.clock.t += 0.2
        self.say(b, "a", "!hype")
        self.assertEqual(b.hype, 16)

    def test_cooldowns_are_per_chatter(self):
        b = self.make()
        self.say(b, "a", "hi")
        self.say(b, "b", "hi")
        self.assertEqual(b.hype, 4)

    def test_message_and_command_cooldowns_independent(self):
        b = self.make()
        self.say(b, "a", "hi")
        self.say(b, "a", "!hype")
        self.assertEqual(b.hype, 10)

    def test_capped_at_100(self):
        b = self.make(hype_per_command=60)
        self.say(b, "a", "!hype")
        self.say(b, "b", "!hype")
        self.say(b, "c", "!hype")
        self.assertEqual(b.hype, 100.0)

    def test_chat_ignored_while_maxed(self):
        b = self.make()
        b.hype_full_until = self.clock.t + 10
        self.say(b, "a", "hi")
        self.assertEqual(b.hype, 0)
        self.assertEqual(b.hype_seen, {})

    def test_add_hype(self):
        b = self.make()
        b.add_hype(30)
        b.add_hype("15")
        self.assertEqual(b.hype, 45)
        b.add_hype(1000)
        self.assertEqual(b.hype, 100)

    def test_add_hype_rejects_bad_values(self):
        b = self.make()
        for v in (0, -5, "x", None, float("nan")):
            b.add_hype(v)
        self.assertEqual(b.hype, 0)

    def test_add_hype_ignored_while_maxed(self):
        b = self.make()
        b.hype_full_until = self.clock.t + 5
        b.add_hype(10)
        self.assertEqual(b.hype, 0)

    def test_maxed_latches_30s_then_resets(self):
        b = self.make()
        b.hype_control("fill")
        b.refresh()
        self.assertEqual(b.hype_full_until, self.clock.t + 30)
        self.assertEqual(self.notes, ["Hype meter maxed!"])
        self.assertTrue(b.brb_state("hype_full", {}))
        self.assertEqual(self.overlay.files["hype"], "HYPE MAXED!")
        self.assertEqual(b.hype_display({}), {"label": "HYPE\nMAXED!", "bg": "#c01010"})
        self.clock.t += 31
        b.refresh()
        self.assertEqual((b.hype, b.hype_full_until), (0.0, 0))
        self.assertFalse(b.brb_state("hype_full", {}))

    def test_decay(self):
        b = self.make(hype_decay_per_min=12)
        b.hype = 50
        b.refresh()
        self.assertAlmostEqual(b.hype, 50 - 12 / 120)

    def test_decay_floors_at_zero(self):
        b = self.make()
        b.hype = 0.01
        b.refresh()
        self.assertEqual(b.hype, 0.0)

    def test_reset(self):
        b = self.make()
        b.hype_control("fill")
        b.hype_full_until = self.clock.t + 10
        b.hype_control("reset")
        self.assertEqual((b.hype, b.hype_full_until), (0.0, 0))

    def test_hype_display_and_text(self):
        b = self.make()
        b.hype = 42.4
        self.assertEqual(b.hype_display({}), {"label": "Hype\n42%"})
        self.assertEqual(b.hype_text(), "HYPE 42%  ·  type !hype")

    def test_snapshot_hype(self):
        b = self.make()
        b.hype = 12.34
        self.assertEqual(b.snapshot()["hype"], {"value": 12.3, "max": 100, "full_until": None})

    def test_prune_boundary(self):
        b = self.make()
        now = self.clock.t
        b.hype_seen = {("a", False): now - 10, ("b", False): now - 9.9}
        with b.lock:
            b.prune_hype_seen(now)
        self.assertEqual(set(b.hype_seen), {("b", False)})

    def test_refresh_prunes(self):
        b = self.make()
        self.say(b, "a", "hi")
        self.clock.t += 11
        b.refresh()
        self.assertEqual(b.hype_seen, {})

    def test_hype_bar_drawn_once_per_change(self):
        obs = mock.Mock()
        obs.send.return_value = {"sceneItemId": 1}
        self.modules["OBS"] = obs
        b = self.make()
        b.hype = 50
        b.refresh()
        calls = obs.send.call_count
        self.assertGreater(calls, 0)
        first = obs.send.call_args_list[0]
        self.assertEqual(first.args[0], "SetInputSettings")
        self.assertEqual(first.args[1]["inputSettings"]["width"], 720 * 50 // 100)
        b.hype = 50    # same rounded level after decay: no redraw
        b.refresh()
        self.assertEqual(obs.send.call_count, calls)

    def test_obs_failure_does_not_raise(self):
        obs = mock.Mock()
        obs.send.side_effect = RuntimeError("down")
        self.modules["OBS"] = obs
        b = self.make()
        b.hype = 10
        b.refresh()
        self.assertIsNone(b.hype_drawn)    # retried next time


class Videos(Base):
    def setUp(self):
        super().setUp()
        self.root = tmpdir()
        self.vids = os.path.join(self.root, "videos")
        os.makedirs(self.vids)

    def touch(self, name, folder=None):
        path = os.path.join(folder or self.vids, name)
        with open(path, "w") as f:
            f.write("x")
        return path

    def make_cfg(self, **kw):
        return self.make(media_dir=self.root, **kw)

    def test_lists_only_videos_sorted(self):
        b = self.make_cfg()
        c = self.touch("b.mp4")
        a = self.touch("a.MKV")
        self.touch("notes.txt")
        self.touch("credits.yaml")
        os.makedirs(os.path.join(self.vids, "dir.mp4"))
        self.assertEqual(b.playlist(), [a, c])

    def test_missing_dir_created_when_configured(self):
        root = os.path.join(self.root, "fresh")
        b = self.make(media_dir=root)
        self.assertEqual(b.playlist(), [])
        self.assertTrue(os.path.isdir(os.path.join(root, "videos")))

    def test_makedirs_once(self):
        b = self.make_cfg()
        with mock.patch("os.makedirs") as mk:
            b.playlist()
            b.playlist()
        self.assertEqual(mk.call_count, 1)

    def test_no_makedirs_unless_configured(self):
        b = self.make()
        b.media_dir = lambda kind: os.path.join(self.root, "nope", kind)
        with mock.patch("os.makedirs") as mk:
            self.assertEqual(b.playlist(), [])
        mk.assert_not_called()
        self.assertFalse(os.path.exists(os.path.join(self.root, "nope")))

    def test_highlights_newest_replays(self):
        rec = os.path.join(self.root, "rec")
        os.makedirs(rec)
        old = self.touch("Replay old.mp4", rec)
        mid = self.touch("Replay mid.mkv", rec)
        new = self.touch("Replay new.mp4", rec)
        self.touch("Other.mp4", rec)
        self.touch("Replay doc.txt", rec)
        for i, p in enumerate((old, mid, new)):
            os.utime(p, (1000 + i, 1000 + i))
        b = self.make_cfg(highlights_dir=rec, highlights_count=2)
        v = self.touch("v.mp4")
        self.assertEqual(b.playlist(), [v, mid, new])

    def test_highlights_off_by_default(self):
        rec = os.path.join(self.root, "rec")
        os.makedirs(rec)
        self.touch("Replay a.mp4", rec)
        b = self.make_cfg(highlights_dir=rec)
        self.assertEqual(b.playlist(), [])

    def test_credit_defaults_to_title(self):
        b = self.make_cfg()
        p = self.touch("Cool Clip.mp4")
        self.assertEqual(b.video_credit(p), "Cool Clip")

    def test_credit_from_yaml(self):
        b = self.make_cfg()
        p = self.touch("clip.mp4")
        with open(os.path.join(self.vids, "credits.yaml"), "w") as f:
            f.write('"clip.mp4": "Clip by Someone · CC BY 4.0"\n')
        self.assertEqual(b.video_credit(p), "Clip by Someone · CC BY 4.0")

    def test_credit_extra_entries_ignored(self):
        b = self.make_cfg()
        p = self.touch("clip.mp4")
        with open(os.path.join(self.vids, "credits.yaml"), "w") as f:
            f.write('"other.mp4": "Not this one"\n"clip.mp4": "Mine"\n"ghost.mp4": "Missing file"\n')
        self.assertEqual(b.video_credit(p), "Mine")

    def test_credit_unlisted_video_falls_back(self):
        b = self.make_cfg()
        p = self.touch("clip.mp4")
        with open(os.path.join(self.vids, "credits.yaml"), "w") as f:
            f.write('"other.mp4": "Not this one"\n')
        self.assertEqual(b.video_credit(p), "clip")

    def test_credit_bad_yaml(self):
        b = self.make_cfg()
        p = self.touch("clip.mp4")
        with open(os.path.join(self.vids, "credits.yaml"), "w") as f:
            f.write("clip.mp4: [unclosed\n  : :\n")
        self.assertEqual(b.video_credit(p), "clip")

    def test_credit_empty_yaml(self):
        b = self.make_cfg()
        p = self.touch("clip.mp4")
        with open(os.path.join(self.vids, "credits.yaml"), "w"):
            pass
        self.assertEqual(b.video_credit(p), "clip")

    def test_credit_file_reloaded_on_mtime_change(self):
        b = self.make_cfg()
        p = self.touch("clip.mp4")
        cf = os.path.join(self.vids, "credits.yaml")
        with open(cf, "w") as f:
            f.write('"clip.mp4": "First"\n')
        os.utime(cf, (1000, 1000))
        self.assertEqual(b.video_credit(p), "First")
        with open(cf, "w") as f:
            f.write('"clip.mp4": "Second"\n')
        os.utime(cf, (2000, 2000))
        self.assertEqual(b.video_credit(p), "Second")

    # BUG: a credits.yaml that parses to a list/string makes video_credit raise AttributeError
    # (only OSError/YAMLError are caught), which breaks load_video / next_video.
    def test_non_mapping_credits_yaml(self):
        # credits.yaml that is a list or string, not a mapping
        b = self.make_cfg()
        p = self.touch("clip.mp4")
        with open(os.path.join(self.vids, "credits.yaml"), "w") as f:
            f.write("- a\n- b\n")
        self.assertEqual(b.video_credit(p), "clip")

    def test_next_video_loads_and_writes_credit(self):
        obs = mock.Mock()
        self.modules["OBS"] = obs
        b = self.make_cfg()
        p = self.touch("only.mp4")
        b.video_control("next")
        self.assertEqual(b.video_current, p)
        req, data = obs.send.call_args.args
        self.assertEqual(req, "SetInputSettings")
        self.assertEqual(data["inputSettings"]["playlist"][0]["value"], p)
        self.assertEqual(self.overlay.files["videocredit"], "only")
        self.assertEqual(b.snapshot()["video"], {"name": "only", "credit": "only"})

    def test_shuffle_plays_everything_before_repeating(self):
        self.modules["OBS"] = mock.Mock()
        b = self.make_cfg()
        paths = {self.touch(f"{n}.mp4") for n in "abcd"}
        seen = []
        for _ in range(4):
            b.next_video()
            seen.append(b.video_current)
        self.assertEqual(set(seen), paths)

    def test_no_immediate_repeat_across_reshuffle(self):
        self.modules["OBS"] = mock.Mock()
        b = self.make_cfg()
        self.touch("a.mp4")
        self.touch("b.mp4")
        for _ in range(20):
            prev = b.video_current
            b.next_video()
            self.assertNotEqual(b.video_current, prev)

    def test_next_video_empty(self):
        self.modules["OBS"] = mock.Mock()
        b = self.make_cfg()
        b.next_video()
        self.assertIsNone(b.video_current)
        self.assertEqual(self.overlay.files["videocredit"], "")

    def test_new_file_joins_queue(self):
        self.modules["OBS"] = mock.Mock()
        b = self.make_cfg()
        self.touch("a.mp4")
        b.next_video()
        c = self.touch("c.mp4")
        b.next_video()
        self.assertEqual(b.video_current, c)

    def test_videos_tick_toggles_placeholder_and_plays(self):
        obs = mock.Mock()
        obs.send.side_effect = lambda req, data=None: (
            {"sceneItemId": 1} if req == "GetSceneItemId" else
            {"mediaState": "OBS_MEDIA_STATE_NONE"} if req == "GetMediaInputStatus" else {})
        self.modules["OBS"] = obs
        b = self.make_cfg()
        b.videos_tick()
        self.assertFalse(b.videos_shown)
        p = self.touch("a.mp4")
        b.videos_tick()
        self.assertTrue(b.videos_shown)
        self.assertEqual(b.video_current, p)

    def test_videos_tick_does_not_advance_while_playing(self):
        obs = mock.Mock()
        obs.send.side_effect = lambda req, data=None: (
            {"sceneItemId": 1} if req == "GetSceneItemId" else
            {"mediaState": "OBS_MEDIA_STATE_PLAYING"} if req == "GetMediaInputStatus" else {})
        self.modules["OBS"] = obs
        b = self.make_cfg()
        p = self.touch("a.mp4")
        self.touch("b.mp4")
        b.videos_shown = True
        b.video_current = p
        b.videos_tick()
        self.assertEqual(b.video_current, p)

    def test_videos_tick_advances_when_ended(self):
        obs = mock.Mock()
        obs.send.side_effect = lambda req, data=None: (
            {"sceneItemId": 1} if req == "GetSceneItemId" else
            {"mediaState": "OBS_MEDIA_STATE_ENDED"} if req == "GetMediaInputStatus" else {})
        self.modules["OBS"] = obs
        b = self.make_cfg()
        p = self.touch("a.mp4")
        q = self.touch("b.mp4")
        b.videos_shown = True
        b.video_current = p
        b.videos_tick()
        self.assertEqual(b.video_current, q)

    def test_videos_tick_swallows_obs_errors(self):
        obs = mock.Mock()
        obs.send.side_effect = RuntimeError("down")
        self.modules["OBS"] = obs
        b = self.make_cfg()
        self.touch("a.mp4")
        b.videos_tick()     # no exception
        self.assertIsNone(b.video_current)


if __name__ == "__main__":
    unittest.main()
