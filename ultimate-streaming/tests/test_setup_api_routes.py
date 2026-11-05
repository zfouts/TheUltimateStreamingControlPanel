"""The route table setup_api.register builds: a refactor must not add, drop or move a route."""
import types
import unittest
from unittest import mock

from tests import helpers  # noqa: F401  (sandboxes HOME/XDG_* before anything touches the state dir)
from core.webapi import setup_api


class _Router:
    def __init__(self):
        self.routes = []

    def get(self, path):
        return lambda fn: (self.routes.append(("GET", path)), fn)[1]

    def post(self, path):
        return lambda fn: (self.routes.append(("POST", path)), fn)[1]

    def upload(self, path, **kw):
        return lambda fn: (self.routes.append(("UPLOAD", path)), fn)[1]


class RouteTable(unittest.TestCase):
    def test_routes(self):
        r = _Router()
        with mock.patch.object(setup_api, "_clear_uploads"):   # never touch the uploads folder
            setup_api.register(r, types.SimpleNamespace(bot=None))
        self.assertEqual(r.routes, [
            ("GET", "/api/setup/detect"), ("POST", "/api/setup/backup"),
            ("GET", "/api/setup/backups"), ("POST", "/api/setup/restore"),
            ("POST", "/api/setup/backups/pin"), ("POST", "/api/setup/backups/delete"),
            ("UPLOAD", "/api/setup/upload"), ("POST", "/api/setup/import/preview"),
            ("POST", "/api/setup/import/apply"), ("POST", "/api/setup/review"),
            ("GET", "/api/setup/obs"), ("POST", "/api/setup/obs/adopt"),
            ("POST", "/api/setup/obs/addon"), ("GET", "/api/setup/apps/running"),
            ("POST", "/api/setup/app/stop"),
        ])


if __name__ == "__main__":
    unittest.main()
