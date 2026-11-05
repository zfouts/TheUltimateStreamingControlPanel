"""The example config, layout selection for every model, and config file writing."""
import glob
import os
import stat
import unittest

from tests.helpers import EXAMPLE, ROOT, Deck, tmpdir

import yaml  # noqa: E402

from core import configio  # noqa: E402
from core.bot import DeckBot, resolve_key  # noqa: E402
from core.models import MODELS  # noqa: E402


def bot_for(model, path=EXAMPLE):
    bot = DeckBot(root=ROOT, config_path=path)
    bot.deck = Deck(model)
    bot.load_config(initial=True)
    return bot


class ExampleConfig(unittest.TestCase):
    def setUp(self):
        self.cfg = configio.load(EXAMPLE)

    def test_current_version(self):
        self.assertEqual(self.cfg.get("version"), configio.CONFIG_VERSION)

    def test_no_credentials_in_config(self):
        for section in ("twitch", "home_assistant", "obs"):
            for field in ("token", "password", "client_secret", "access_token"):
                self.assertFalse((self.cfg.get(section) or {}).get(field), f"{section}.{field} in example")

    def test_every_saved_key_reference_exists(self):
        lib = self.cfg["keys"]
        for lname, lay in self.cfg["layouts"].items():
            pages = lay.get("pages", lay)
            for pname, page in pages.items():
                keys = page.get("keys", []) if isinstance(page, dict) else page
                flat = [k for row in keys for k in (row if isinstance(row, list) else [row])]
                for k in flat:
                    name = k if isinstance(k, str) else (k or {}).get("use") if isinstance(k, dict) else None
                    if name and name != "-":
                        self.assertIn(name, lib, f"layout {lname} page {pname} uses missing key {name!r}")


class Layouts(unittest.TestCase):
    def test_every_model_gets_pages_with_valid_links(self):
        for model in MODELS:
            with self.subTest(model=model):
                bot = bot_for(model)
                self.assertTrue(bot.pages, f"{model}: no pages")
                rows, cols = bot.grid()
                slots = rows * cols + bot.deck.touch_key_count()
                for name, keys in bot.pages.items():
                    self.assertLessEqual(len(keys), slots, f"{model} page {name} overflows")
                    for key in keys:
                        if not isinstance(key, dict):
                            continue
                        self.assertFalse(str(key.get("label", "")).startswith("? "),
                                         f"{model} page {name}: missing saved key {key.get('label')}")
                        steps = key.get("action") or []
                        for step in steps if isinstance(steps, list) else [steps]:
                            target = step.get("page") if isinstance(step, dict) else None
                            if target and target != "back":
                                self.assertIn(target, bot.pages, f"{model} page {name} links to {target}")

    def test_resolve_key(self):
        lib = {"live": {"label": "Live", "bg": "#f00"}}
        self.assertIsNone(resolve_key("-", lib))
        self.assertEqual(resolve_key("live", lib), lib["live"])
        self.assertEqual(resolve_key({"use": "live", "label": "Go"}, lib), {"label": "Go", "bg": "#f00"})
        self.assertTrue(resolve_key("nope", lib)["label"].startswith("? "))


class ConfigFiles(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir()
        self.path = os.path.join(self.dir, "config.yaml")
        with open(EXAMPLE) as src, open(self.path, "w") as dst:
            dst.write(src.read())

    def test_dump_round_trip(self):
        cfg = configio.load(EXAMPLE)
        self.assertEqual(yaml.safe_load(configio.dumps(cfg)), cfg)

    def test_blank_keys_stay_readable(self):
        text = configio.dumps({"pages": {"main": [["-", "a", "-"]]}})
        self.assertIn("[-, a, -]", text)

    def test_save_backs_up_and_is_private(self):
        h = configio.file_hash(self.path)
        new = configio.save(self.path, {"pages": {"main": ["-"]}}, h)
        self.assertEqual(new, configio.file_hash(self.path))
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        revs = os.path.join(os.environ["DECK_STATE_DIR"], "backups", "revisions")
        before = sorted(glob.glob(os.path.join(revs, "*-before-save.yaml")))
        after = sorted(glob.glob(os.path.join(revs, "*-panel.yaml")))
        self.assertTrue(before, "the file as it was before saving is kept")
        self.assertTrue(after, "the saved file is kept as a revision")
        self.assertEqual(stat.S_IMODE(os.stat(before[-1]).st_mode), 0o600)

    def test_save_refuses_when_file_changed(self):
        h = configio.file_hash(self.path)
        with open(self.path, "a") as f:
            f.write("\n# hand edit\n")
        with self.assertRaises(configio.Conflict):
            configio.save(self.path, {"pages": {"main": ["-"]}}, h)

    def test_no_legacy_backup_files(self):
        """Saves now go to backups/revisions (pruning is tested in test_backups)."""
        h = configio.file_hash(self.path)
        configio.save(self.path, {"pages": {"main": ["-"]}}, h)
        self.assertEqual(glob.glob(os.path.join(os.environ["DECK_STATE_DIR"], "backups", "config-*.yaml")), [])

    def test_comment_count(self):
        self.assertEqual(configio.comment_count(self.path, EXAMPLE), 0, "stock comments shouldn't count")
        with open(self.path, "a") as f:
            f.write("# my note\nextra: 'a # not a comment'\nother: 1  # trailing note\n")
        self.assertEqual(configio.comment_count(self.path, EXAMPLE), 2)
        configio.save(self.path, configio.load(self.path))
        self.assertEqual(configio.comment_count(self.path, EXAMPLE), 0, "editor header shouldn't count")


if __name__ == "__main__":
    unittest.main()
