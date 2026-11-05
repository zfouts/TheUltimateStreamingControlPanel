"""Settings move from the old folder name (~/.config/streamdeck) to ~/.config/ultimate-streaming
without breaking anything that still points at the old path (OBS text sources, config.yaml)."""
import importlib
import os
import unittest
from unittest import mock

from tests.helpers import tmpdir

from core import paths


class MigrateLegacy(unittest.TestCase):
    def setUp(self):
        self.home = tmpdir()
        env = {"XDG_CONFIG_HOME": self.home}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("DECK_STATE_DIR", None)
        importlib.reload(paths)
        self.addCleanup(importlib.reload, paths)
        self.old, self.new = os.path.join(self.home, "streamdeck"), os.path.join(self.home, "ultimate-streaming")

    def test_moves_old_folder_and_leaves_a_link(self):
        os.makedirs(os.path.join(self.old, "overlay"))
        with open(os.path.join(self.old, "overlay", "title.txt"), "w") as f:
            f.write("My stream")
        self.assertEqual(paths.migrate_legacy(), self.new)
        self.assertTrue(os.path.isdir(self.new) and not os.path.islink(self.new))
        self.assertTrue(os.path.islink(self.old))
        with open(os.path.join(self.old, "overlay", "title.txt")) as f:   # what OBS reads
            self.assertEqual(f.read(), "My stream")
        self.assertEqual(paths.state_dir(), self.new)
        self.assertIsNone(paths.migrate_legacy(), "second run does nothing")

    def test_does_nothing_without_old_folder_or_when_new_exists(self):
        self.assertIsNone(paths.migrate_legacy())
        os.makedirs(self.old)
        os.makedirs(self.new)
        self.assertIsNone(paths.migrate_legacy())
        self.assertFalse(os.path.islink(self.old))

    def test_respects_explicit_state_dir(self):
        os.makedirs(self.old)
        with mock.patch.dict(os.environ, {"DECK_STATE_DIR": tmpdir()}):
            self.assertIsNone(paths.migrate_legacy())
        self.assertFalse(os.path.islink(self.old))


if __name__ == "__main__":
    unittest.main()
