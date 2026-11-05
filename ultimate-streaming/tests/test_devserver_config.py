"""tools/devserver.py's config preparation: sandbox edits for a good file, and a broken file left alone."""
import importlib.util
import os
import tempfile
import unittest

import yaml

from tests.helpers import EXAMPLE, ROOT

spec = importlib.util.spec_from_file_location("devserver_cfg_under_test", os.path.join(ROOT, "tools", "devserver.py"))
devserver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(devserver)


class PrepareConfig(unittest.TestCase):
    def write(self, text):
        d = tempfile.mkdtemp(prefix="devserver-cfg-")
        self.addCleanup(__import__("shutil").rmtree, d, ignore_errors=True)
        path = os.path.join(d, "config.yaml")
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_valid_config_gets_port_and_closed_obs(self):
        path = self.write("obs: {host: studio.lan, port: 4455}\n")
        self.assertTrue(devserver.prepare_config(path, 8811, live=False))
        cfg = yaml.safe_load(open(path))
        self.assertEqual(cfg["editor"]["port"], 8811)
        self.assertEqual((cfg["obs"]["host"], cfg["obs"]["port"]), ("127.0.0.1", 9))

    def test_live_leaves_obs_alone(self):
        path = self.write("obs: {host: studio.lan}\n")
        self.assertTrue(devserver.prepare_config(path, 8811, live=True))
        self.assertEqual(yaml.safe_load(open(path))["obs"], {"host": "studio.lan"})

    def test_invalid_yaml_is_left_as_is(self):
        text = "pages: [unclosed\n  key: : :\n"
        path = self.write(text)
        self.assertFalse(devserver.prepare_config(path, 8811, live=False))
        self.assertEqual(open(path).read(), text)

    def test_non_mapping_is_left_as_is(self):
        path = self.write("- just\n- a list\n")
        self.assertFalse(devserver.prepare_config(path, 8811, live=False))
        self.assertEqual(open(path).read(), "- just\n- a list\n")

    def test_overrides_apply_to_a_fallback_config(self):
        cfg = devserver.sandbox_overrides({}, 8811, live=False)
        self.assertEqual(cfg["editor"]["port"], 8811)
        self.assertEqual(cfg["obs"]["port"], 9)

    def test_example_config_still_prepares(self):
        path = self.write(open(EXAMPLE).read())
        self.assertTrue(devserver.prepare_config(path, 8811, live=False))


if __name__ == "__main__":
    unittest.main()
