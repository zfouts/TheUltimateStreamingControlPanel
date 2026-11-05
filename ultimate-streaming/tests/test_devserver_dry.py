"""The dev server's dry run: state providers that run commands or reach the network only log,
and the setup restore route refuses while DECK_DRY_RUN=1 (set by tools/devserver.py unless --live)."""
import importlib.util
import os
import unittest
from unittest import mock

from tests.helpers import EXAMPLE, ROOT

from core.bot import DeckBot  # noqa: E402
from core.webapi import ApiError, setup_api  # noqa: E402

spec = importlib.util.spec_from_file_location("devserver_under_test", os.path.join(ROOT, "tools", "devserver.py"))
devserver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(devserver)


class DryStates(unittest.TestCase):
    def setUp(self):
        self.bot = DeckBot(root=ROOT, config_path=EXAMPLE)
        self.bot.load_extensions("modules")

    def test_cmd_state_does_not_run_a_shell(self):
        self.assertIn("cmd", self.bot.state_providers)
        devserver.dry_run(self.bot)
        with mock.patch("modules.system.sh") as sh, mock.patch("core.util.subprocess.run") as run:
            self.assertFalse(self.bot.check_state({"cmd": "touch /tmp/should-not-exist"}))
        sh.assert_not_called()
        run.assert_not_called()

    def test_ha_state_does_no_http(self):
        devserver.dry_run(self.bot)
        with mock.patch("requests.get") as get:
            self.assertFalse(self.bot.check_state({"ha": "light.office"}))
        get.assert_not_called()

    def test_dry_run_twice_keeps_one_wrapper(self):
        devserver.dry_run(self.bot)
        first = self.bot.state_providers["cmd"]
        devserver.dry_run(self.bot)
        self.assertIs(self.bot.state_providers["cmd"], first)

    def test_every_named_provider_exists(self):
        self.assertLessEqual(devserver.DRY_STATES, set(self.bot.state_providers))
        self.assertLessEqual(devserver.DRY_DISPLAYS, set(self.bot.display_providers))


class RestoreGuard(unittest.TestCase):
    def test_restore_refused_in_dry_run(self):
        routes = {}

        class Router:
            def get(self, path): return lambda fn: routes.setdefault(("GET", path), fn)
            def post(self, path): return lambda fn: routes.setdefault(("POST", path), fn)
            def upload(self, path, **kw): return lambda fn: fn

        ed = mock.Mock()
        with mock.patch.object(setup_api, "_clear_uploads", create=True):
            setup_api.register(Router(), ed)
        restore = routes[("POST", "/api/setup/restore")]
        with mock.patch.dict(os.environ, {"DECK_DRY_RUN": "1"}), \
                mock.patch.object(setup_api.setup_backup, "restore") as real:
            with self.assertRaises(ApiError) as cm:
                restore({"id": "x", "item": "obs", "confirm": True})
        self.assertEqual(cm.exception.status, 403)
        real.assert_not_called()


if __name__ == "__main__":
    unittest.main()
