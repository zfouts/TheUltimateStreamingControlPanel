"""modules/_twitch_bot_config.py holds the Twitch Bot's pure config half: the loader must not
treat it as a module, and modules.twitch_bot must re-export the very same objects."""
import types
import unittest

from tests import helpers

from core.bot import DeckBot
from modules import _twitch_bot_config as cfg
from modules import twitch_bot as tb


class Split(unittest.TestCase):
    def test_config_half_is_not_a_module(self):
        self.assertFalse(hasattr(cfg, "setup"))

    def test_loader_skips_it(self):
        loaded = []
        fake = types.SimpleNamespace(root=helpers.ROOT, load_extension=loaded.append)
        DeckBot.load_extensions(fake, "modules")
        self.assertIn("modules.twitch_bot", loaded)
        self.assertNotIn("modules._twitch_bot_config", loaded)

    def test_reexported_as_the_same_objects(self):
        self.assertIs(tb.validate, cfg.validate)
        for name in ("clean_value", "render", "_clean", "Skipped", "DEFAULT_COMMANDS", "CHAT_MAX"):
            self.assertIs(getattr(tb, name), getattr(cfg, name), name)


if __name__ == "__main__":
    unittest.main()
