"""Actions declared with rewards=False can never run from a channel-point reward."""
import unittest

from tests import helpers  # noqa: F401

from core import Module, action
from core.module import REWARD_DENIED
from modules import twitch_bot as tb


class Printer(Module):
    @action("test_printer_stop", rewards=False)
    def stop(self, arg):
        pass

    @action("test_printer_light")
    def light(self, arg):
        pass


class RewardDenied(unittest.TestCase):
    def test_registered(self):
        self.assertIn("test_printer_stop", REWARD_DENIED)
        self.assertNotIn("test_printer_light", REWARD_DENIED)

    def test_refused_even_when_unsafe_allowed(self):
        errors = tb.check_reward_actions([{"test_printer_stop": None}], allow_unsafe=True)
        self.assertTrue(any("can never run from a reward" in e for e in errors), errors)
        nested = tb.check_reward_actions([[{"delay": 1}, {"test_printer_stop": None}]], allow_unsafe=True)
        self.assertTrue(nested)

    def test_other_actions_unchanged(self):
        self.assertEqual(tb.check_reward_actions([{"test_printer_light": "on"}], allow_unsafe=True), [])
        self.assertTrue(tb.check_reward_actions([{"test_printer_light": "on"}], allow_unsafe=False))


if __name__ == "__main__":
    unittest.main()
