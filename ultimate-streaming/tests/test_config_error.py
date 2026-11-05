"""A failed reload of config.yaml on a running daemon is surfaced as bot.config_error."""
import os
import unittest
from unittest import mock

from tests.helpers import ROOT, Deck, tmpdir

from core.bot import DeckBot  # noqa: E402
from modules.editor import Editor  # noqa: E402

GOOD = "pages:\n  main: [a]\nkeys:\n  a: {label: A}\n"
GOOD2 = "pages:\n  main: [a]\n  other: [a]\nkeys:\n  a: {label: A}\n"
BROKEN = "pages:\n  main: [a\nkeys: {\n"


def write(path, text):
    with open(path, "w") as f:
        f.write(text)
    st = os.stat(path)   # distinct (mtime, size) even for same-size rewrites
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000 * write.n))
    write.n += 1


write.n = 1


class ConfigErrorTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tmpdir(), "config.yaml")
        write(self.path, GOOD)
        self.bot = DeckBot(root=ROOT, config_path=self.path)
        self.bot.deck = Deck("Stream Deck XL")
        self.bot.load_config(initial=True)
        self.events = self.bot.hub.subscribe()

    def drain(self):
        out = []
        while not self.events.empty():
            out.append(self.events.get_nowait())
        return [e["data"] for e in out if e["type"] == "config"]

    def test_broken_reload_sets_error_and_keeps_config(self):
        self.assertIsNone(self.bot.config_error)
        old = self.bot.cfg
        write(self.path, BROKEN)
        with mock.patch("time.time", return_value=1000.0):
            self.assertFalse(self.bot.load_config())
        self.assertIs(self.bot.cfg, old)
        self.assertIsNone(self.bot.config_fallback)
        err = self.bot.config_error
        self.assertEqual(err["since"], 1000)
        self.assertRegex(err["error"], r"^line \d+, column \d+: ")

    def test_fixed_file_clears_error(self):
        write(self.path, BROKEN)
        self.bot.load_config()
        self.assertIsNotNone(self.bot.config_error)
        write(self.path, GOOD2)
        self.assertTrue(self.bot.load_config())
        self.assertIsNone(self.bot.config_error)
        self.assertIn("other", self.bot.cfg["pages"])

    def test_reverting_to_running_bytes_clears_error(self):
        write(self.path, BROKEN)
        self.bot.load_config()
        write(self.path, GOOD)
        self.assertFalse(self.bot.load_config())
        self.assertIsNone(self.bot.config_error)

    def test_since_is_stable_across_failed_reloads(self):
        write(self.path, BROKEN)
        with mock.patch("time.time", return_value=1000.0):
            self.bot.load_config()
        first = dict(self.bot.config_error)
        write(self.path, BROKEN + "# touched\n")   # same error, file changed again
        with mock.patch("time.time", return_value=2000.0):
            self.bot.load_config()
        self.assertEqual(self.bot.config_error, first)

    def test_event_and_status_carry_it(self):
        self.drain()
        write(self.path, BROKEN)
        self.bot.load_config()
        events = self.drain()
        self.assertTrue(events)
        self.assertEqual(events[-1]["config_error"], self.bot.config_error)
        stub = mock.Mock(bot=self.bot)
        stub.device.return_value, stub.decks.return_value = None, []
        with mock.patch("modules.editor.plugin_api.plugins_status", return_value=[]):
            st = Editor.status(stub)
        self.assertEqual(st["config_error"], self.bot.config_error)
        write(self.path, GOOD2)
        self.bot.load_config()
        self.assertIsNone(self.drain()[-1]["config_error"])

    def test_panel_save_clears_error(self):
        write(self.path, BROKEN)
        self.bot.load_config()
        self.drain()
        self.bot.clear_config_fallback()
        self.assertIsNone(self.bot.config_error)
        self.assertIsNone(self.drain()[-1]["config_error"])


if __name__ == "__main__":
    unittest.main()
