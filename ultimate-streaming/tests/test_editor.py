"""The web editor's HTTP API: access control, saving, secrets. Runs the real daemon
(minus the hardware) against a copy of the example config in a sandbox."""
import json
import os
import stat
import unittest
import urllib.error
import urllib.request

from tests.helpers import EXAMPLE, ROOT, Deck, free_port, tmpdir

import yaml  # noqa: E402

from core import editor_auth  # noqa: E402
from core.bot import DeckBot  # noqa: E402

PORT = free_port()
STATE = os.environ["DECK_STATE_DIR"]


def setUpModule():
    global BOT, CONFIG, BASE, TOKEN
    CONFIG = os.path.join(tmpdir(), "config.yaml")
    with open(EXAMPLE) as f:
        cfg = yaml.safe_load(f)
    cfg["editor"] = {"port": PORT}
    with open(CONFIG, "w") as f:
        f.write("# my own note\n" + yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
    BOT = DeckBot(root=ROOT, config_path=CONFIG)
    BOT.deck = Deck("Stream Deck XL")
    BOT.load_config(initial=True)
    BOT.load_extensions("modules")
    BASE, TOKEN = f"http://127.0.0.1:{PORT}", editor_auth.token()


def tearDownModule():
    for inst in list(BOT.modules.values()):
        try:
            inst.on_unload()
        except Exception:
            pass


def call(path, body=None, headers=None, token=True):
    h = {"X-Deck-Token": TOKEN} if token is True else ({"X-Deck-Token": token} if token else {})
    data = None
    if body is not None:
        data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
    h.update(headers or {})
    req = urllib.request.Request(BASE + path, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        with e:
            return e.code, e.headers, e.read()


def ok_json(path, body=None):
    status, _, raw = call(path, body)
    assert status == 200, (path, status, raw[:200])
    return json.loads(raw)


class AccessControl(unittest.TestCase):
    def test_token_required(self):
        self.assertEqual(call("/api/state", token=None)[0], 401)
        self.assertEqual(call("/api/state", token="x" * 43)[0], 401)

    def test_token_file_private(self):
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(STATE, "editor_token")).st_mode), 0o600)

    def test_dns_rebinding_refused(self):
        self.assertEqual(call("/api/state", headers={"Host": f"evil.example:{PORT}"})[0], 421)

    def test_other_websites_refused(self):
        self.assertEqual(call("/api/state", headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(call("/api/run", {"action": {"delay": 0}}, headers={"Origin": "http://evil.example"})[0], 403)

    def test_json_only(self):
        self.assertEqual(call("/api/run", {"action": {"delay": 0}}, headers={"Content-Type": "text/plain"})[0], 400)

    def test_page_headers(self):
        status, headers, _ = call("/", token=None)
        self.assertEqual(status, 200)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_no_file_serving_outside_editor(self):
        for path in ("/../core/paths.py", "/config.yaml", "/%2e%2e/main.py", "/editor_token"):
            self.assertEqual(call(path)[0], 404, path)

    def test_listens_on_localhost_only(self):
        self.assertEqual(BOT.modules and next(m for m in BOT.modules.values()
                                               if type(m).__name__ == "Editor").server.server_address[0],
                         "127.0.0.1")


class Api(unittest.TestCase):
    def test_state(self):
        s = ok_json("/api/state")
        self.assertEqual(s["device"]["model"], "Stream Deck XL")
        other = [n for n, a in s["catalog"]["actions"].items() if a["group"] == "Other"]
        self.assertEqual(other, [], "every action should be described for the editor")

    def test_example_validates_clean(self):
        cfg = ok_json("/api/state")["config"]
        self.assertEqual(ok_json("/api/validate", {"config": cfg})["problems"], [])

    def test_validate_catches_mistakes(self):
        cfg = ok_json("/api/state")["config"]
        cfg["layouts"]["3x2"]["main"][0][0] = "missing_key"
        cfg["layouts"]["3x2"]["main"][0][1] = {"label": "x", "action": {"page": "nowhere"}}
        problems = " ".join(ok_json("/api/validate", {"config": cfg})["problems"])
        self.assertIn("missing_key", problems)
        self.assertIn("nowhere", problems)

    def test_render(self):
        imgs = ok_json("/api/render", {"keys": [{"label": "Hi", "icon": "emoji:🎮"}, None, "nope"],
                                       "size": [96, 96]})["images"]
        self.assertTrue(imgs[0].startswith("data:image/png"))
        self.assertIsNone(imgs[1])

    def test_try_it(self):
        self.assertTrue(ok_json("/api/run", {"action": {"delay": 0}})["ok"])
        bad = ok_json("/api/run", {"action": {"nonexistent": 1}})
        self.assertFalse(bad["ok"])

    def test_save_round_trip_and_conflict(self):
        s = ok_json("/api/state")
        self.assertEqual(s["comments"], 1, "the hand-written comment should be reported before saving")
        self.assertEqual(call("/api/save", {"config": s["config"], "hash": "stale"})[0], 409)
        ok_json("/api/save", {"config": s["config"], "hash": s["hash"]})
        s2 = ok_json("/api/state")
        self.assertEqual(s2["config"], s["config"])
        self.assertEqual(s2["comments"], 0)
        self.assertEqual(stat.S_IMODE(os.stat(CONFIG).st_mode), 0o600)

    def test_secrets(self):
        r = ok_json("/api/secret", {"name": "ha_token", "value": "s3cret-value"})
        self.assertEqual(r["field"], "token_file")
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(STATE, "ha_token")).st_mode), 0o600)
        self.assertEqual(call("/api/secret", {"name": "../../x", "value": "y"})[0], 400)
        state = call("/api/state")[2].decode()
        self.assertNotIn("s3cret-value", state)
        with open(CONFIG) as f:
            self.assertNotIn("s3cret-value", f.read())


if __name__ == "__main__":
    unittest.main()
