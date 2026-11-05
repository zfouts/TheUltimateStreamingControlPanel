"""The on-stream Alert text source: scene layout and the add-on for users who keep their own scenes."""
import os
import tempfile
import unittest
from unittest import mock

from tests.test_obs_setup import FakeOBS, build_scenes  # noqa: E402

from core import obs_setup  # noqa: E402


def settings():
    return {**build_scenes.DEFAULTS, "brb_videos": ""}


class AlertSource(unittest.TestCase):
    def test_reads_alert_file_in_overlay_dir(self):
        with mock.patch.object(build_scenes, "overlay_dir", return_value="/ov"):
            kind, st = build_scenes.inputs(settings())["Alert"]
        self.assertEqual(kind, "text_ft2_source_v2")
        self.assertEqual(st["text_file"], os.path.join("/ov", "alert.txt"))
        self.assertTrue(st["from_file"])

    def test_scenes_and_stacking(self):
        sc = build_scenes.scenes(settings())
        for name in ("Screen", "Game", "Camera", "BRB"):
            self.assertEqual(sc[name][-1][0], "Alert", name)
            self.assertEqual([s for s, _ in sc[name]].count("Alert"), 1, name)
        for name in ("Starting Soon", "Ending"):
            self.assertNotIn("Alert", [s for s, _ in sc[name]])

    def test_every_scene_item_has_an_input(self):
        s = settings()
        known = build_scenes.inputs(s)
        for items in build_scenes.scenes(s).values():
            for src, _ in items:
                self.assertIn(src, known)

    def test_overlay_file_created(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(build_scenes, "overlay_dir", return_value=d):
            made = build_scenes.ensure_overlay_files(settings())
            self.assertIn(os.path.join(d, "alert.txt"), made)
            self.assertTrue(os.path.exists(os.path.join(d, "alert.txt")))


class AlertAddon(unittest.TestCase):
    def test_listed(self):
        self.assertIn("alert_text", obs_setup.ADDONS)

    def test_definition(self):
        name, kind, st, transform = obs_setup._definition("alert_text", FakeOBS(), {})
        self.assertEqual((name, kind), ("Alert", "text_ft2_source_v2"))
        self.assertTrue(st["from_file"])
        self.assertEqual(transform["positionX"], 960)


if __name__ == "__main__":
    unittest.main()
