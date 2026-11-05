"""Deck keys for Twitch raids and shoutouts, against the same fake Twitch as test_twitch_api.py."""
import json
import os
import sys
import threading
import time
import types
import unittest
import urllib.error
import urllib.request

from tests.helpers import EXAMPLE, ROOT, Deck, free_port, tmpdir

import requests  # noqa: E402
import yaml  # noqa: E402

from core import editor_auth  # noqa: E402
from core.bot import DeckBot  # noqa: E402
from core.module import REWARD_DENIED  # noqa: E402
from core.webapi import ApiError  # noqa: E402

PORT = free_port()
STATE = os.environ["DECK_STATE_DIR"]
CLIENT_ID = "testclientid0123456789"  # privacy-check: allow (fake client id)
DEVICE_CODE = "DEVICECODEsecretVALUE0123456789"
USER = {"id": "1001", "login": "streamer", "display_name": "Streamer",
        "profile_image_url": "https://static-cdn.jtvnw.net/jtv_user_pictures/x.png"}
FORBIDDEN = ("access_token", "refresh_token", "device_code", DEVICE_CODE)


class Resp:
    def __init__(self, status, body=None):
        self.status_code, self.ok = status, 200 <= status < 300
        self.content = b"" if body is None else json.dumps(body).encode()
        self.text = self.content.decode()

    def json(self):
        return json.loads(self.content)


class FakeTwitch:
    """Just enough of id.twitch.tv and api.twitch.tv/helix."""

    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self, scopes=None):
        self.scopes = list(scopes if scopes is not None else TW_MOD.SCOPE_LIST)
        self.pending_polls = 2
        self.n = 0
        self.valid = None
        self.refresh_token = None
        self.refreshes = 0
        self.refresh_delay = 0
        self.confidential = False     # an app registered as "Confidential": refresh needs a secret
        self.revoked = []
        self.calls = []
        self.validates = 0
        self.unauthorized_barrier = None
        self.categories = [
            {"id": "33214", "name": "Fortnite", "box_art_url": "https://static-cdn.jtvnw.net/ttv-boxart/33214.jpg"},
            {"id": "666", "name": "Fortnight Sim", "box_art_url": "https://evil.example/track.jpg"}]

    def _issue(self):
        with self.lock:
            self.n += 1
            self.valid, self.refresh_token = f"access{self.n}", f"refresh{self.n}"
        return {"access_token": self.valid, "refresh_token": self.refresh_token, "expires_in": 14000,
                "scope": list(self.scopes), "token_type": "bearer"}

    # requests.post
    def post(self, url, data=None, timeout=None, **kw):
        data = data or {}
        if url.endswith("/oauth2/device"):
            return Resp(200, {"device_code": DEVICE_CODE, "user_code": "ABCD-EFGH", "expires_in": 60,
                              "interval": 0.01, "verification_uri": "https://www.twitch.tv/activate"})
        if url.endswith("/oauth2/token"):
            if data.get("grant_type", "").endswith("device_code"):
                assert data["device_code"] == DEVICE_CODE
                if self.pending_polls > 0:
                    self.pending_polls -= 1
                    return Resp(400, {"status": 400, "message": "authorization_pending"})
                return Resp(200, self._issue())
            if data.get("grant_type") == "refresh_token":
                time.sleep(self.refresh_delay)
                with self.lock:
                    self.refreshes += 1
                if self.confidential:
                    return Resp(400, {"status": 400, "message": "missing client secret"})
                if data.get("refresh_token") != self.refresh_token:
                    return Resp(400, {"status": 400, "message": "Invalid refresh token"})
                return Resp(200, self._issue())
        if url.endswith("/oauth2/revoke"):
            self.revoked.append(data.get("token"))
            return Resp(200)
        return Resp(404, {"message": "not found"})

    # requests.get
    def get(self, url, headers=None, timeout=None, **kw):
        if url.endswith("/oauth2/validate"):
            self.validates += 1
            if (headers or {}).get("Authorization") != f"OAuth {self.valid}":
                return Resp(401, {"status": 401, "message": "invalid access token"})
            return Resp(200, {"client_id": CLIENT_ID, "login": "streamer", "user_id": "1001",
                              "scopes": [s for s in self.scopes if s != "user:write:chat"], "expires_in": 100})
        return Resp(404, {"message": "not found"})

    # requests.request
    def request(self, method, url, params=None, json=None, headers=None, timeout=None, **kw):
        path = url.split("/helix", 1)[1]
        self.calls.append((method, path, dict(params or {}), json))
        if (headers or {}).get("Authorization") != f"Bearer {self.valid}":
            if self.unauthorized_barrier:
                try:
                    self.unauthorized_barrier.wait(5)
                except threading.BrokenBarrierError:
                    pass
            return Resp(401, {"error": "Unauthorized", "status": 401, "message": "Invalid OAuth token"})
        route = (method, path)
        if route == ("GET", "/users"):
            if params and "login" in params:
                if params["login"] == "nobody":
                    return Resp(200, {"data": []})
                return Resp(200, {"data": [{"id": "2002", "login": params["login"], "display_name": params["login"]}]})
            return Resp(200, {"data": [USER]})
        if route == ("GET", "/channels"):
            return Resp(200, {"data": [{"title": "Old title", "game_id": "33214", "game_name": "Fortnite",
                                        "tags": ["English"]}]})
        if route == ("PATCH", "/channels"):
            return Resp(204)
        if route == ("GET", "/search/categories"):
            return Resp(200, {"data": self.categories})
        if route == ("POST", "/chat/messages"):
            return Resp(200, {"data": [{"message_id": "m1", "is_sent": True}]})
        if route == ("POST", "/moderation/bans"):
            return Resp(200, {"data": [{"user_id": json["data"]["user_id"]}]})
        if route == ("POST", "/clips"):
            return Resp(202, {"data": [{"id": "Clip", "edit_url": "https://clips.twitch.tv/Clip/edit"}]})
        if route == ("POST", "/channels/commercial"):
            return Resp(200, {"data": [{"length": json["length"], "message": "", "retry_after": 480}]})
        if route == ("GET", "/streams"):
            return Resp(200, {"data": []})
        if route in (("GET", "/chat/settings"), ("PATCH", "/chat/settings")):
            data = {"emote_mode": False, "follower_mode": False, "slow_mode": False, "slow_mode_wait_time": None,
                    "subscriber_mode": False, "unique_chat_mode": False, **(json or {})}
            return Resp(200, {"data": [data]})
        return Resp(204)


def setUpModule():
    global BOT, CONFIG, BASE, TOKEN, TW_MOD, TW, FAKE, SAVED
    CONFIG = os.path.join(tmpdir(), "config.yaml")
    with open(EXAMPLE) as f:
        cfg = yaml.safe_load(f)
    cfg["editor"] = {"port": PORT}
    cfg["twitch"] = {"client_id": CLIENT_ID}   # no channel: the chat reader stays offline
    with open(CONFIG, "w") as f:
        f.write(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
    for leftover in ("twitch.json", "stream_history.json"):
        if os.path.exists(os.path.join(STATE, leftover)):
            os.remove(os.path.join(STATE, leftover))
    BOT = DeckBot(root=ROOT, config_path=CONFIG)
    BOT.deck = Deck("Stream Deck XL")
    BOT.load_config(initial=True)
    BOT.load_extensions("modules")
    BASE, TOKEN = f"http://127.0.0.1:{PORT}", editor_auth.token()
    TW_MOD = sys.modules["modules.twitch"]
    TW = BOT.get_module("Twitch")
    FAKE = FakeTwitch()
    SAVED = (TW_MOD.requests, TW_MOD.notify, TW_MOD.open_url)
    TW_MOD.requests = types.SimpleNamespace(post=FAKE.post, get=FAKE.get, request=FAKE.request,
                                            RequestException=requests.RequestException)
    TW_MOD.notify = TW_MOD.open_url = lambda *a: None


def tearDownModule():
    TW_MOD.requests, TW_MOD.notify, TW_MOD.open_url = SAVED
    for inst in list(BOT.modules.values()):
        try:
            inst.on_unload()
        except Exception:
            pass
    for leftover in ("twitch.json", "stream_history.json"):
        if os.path.exists(os.path.join(STATE, leftover)):
            os.remove(os.path.join(STATE, leftover))


def call(path, body=None):
    h = {"X-Deck-Token": TOKEN}
    data = None
    if body is not None:
        data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        with e:
            return e.code, e.read()


def ok_json(path, body=None):
    status, raw = call(path, body)
    assert status == 200, (path, status, raw[:300])
    return json.loads(raw)


def assert_clean(test, raw):
    text = raw if isinstance(raw, str) else raw.decode() if isinstance(raw, bytes) else json.dumps(raw)
    for word in FORBIDDEN + (FAKE.valid or "access?", FAKE.refresh_token or "refresh?"):
        test.assertNotIn(word, text)


class TwitchCase(unittest.TestCase):
    def setUp(self):
        TW.logout()
        FAKE.reset()
        TW.channel = ""
        TW.last = {}

    def link(self, scopes=None):
        FAKE.reset(scopes)
        ok_json("/api/twitch/login", {})
        deadline = time.time() + 5
        while time.time() < deadline:
            state = ok_json("/api/twitch/auth")
            if state["linked"] and state["user"]:
                return state
            time.sleep(0.02)
        self.fail(f"never linked: {state}")



class Keys(TwitchCase):
    def setUp(self):
        super().setUp()
        self.link()
        self.toasts = []
        TW_MOD.notify = self.toasts.append
        self.addCleanup(setattr, TW_MOD, "notify", lambda *a: None)
        self.alerts = None
        self._get = BOT.get_module
        BOT.get_module = lambda n: self.alerts if n == "Alerts" else self._get(n)
        self.addCleanup(setattr, BOT, "get_module", self._get)

    def paths(self, method=None):
        return [c for c in FAKE.calls if c[1] != "/users" and (method is None or c[0] == method)]

    def test_raid_plain_string_and_dict(self):
        TW.raid_key("Friend_1")
        raid = [c for c in FAKE.calls if c[:2] == ("POST", "/raids")][0]
        self.assertEqual(raid[2], {"from_broadcaster_id": "1001", "to_broadcaster_id": "2002"})
        self.assertIn("Raiding friend_1", self.toasts[0])
        TW.last = {}
        TW.raid_key({"channel": "friend"})
        self.assertEqual(len([c for c in FAKE.calls if c[:2] == ("POST", "/raids")]), 2)

    def test_cancel(self):
        for arg in ("cancel", {"cancel": True}):
            FAKE.calls.clear()
            TW.raid_key(arg)
            self.assertEqual(self.paths(), [("DELETE", "/raids", {"broadcaster_id": "1001"}, None)])
        self.assertEqual(self.toasts, ["Raid canceled"] * 2)

    def test_channel_named_cancel_is_raided_by_dict_form(self):
        TW.raid_key({"channel": "cancel"})
        self.assertEqual([c[:2] for c in self.paths()], [("POST", "/raids")])
        self.assertEqual([c for c in FAKE.calls if c[:2] == ("DELETE", "/raids")], [])
        self.assertIn("Raiding cancel", self.toasts[0])
        FAKE.calls.clear()
        TW.last = {}
        TW.raid_key({"channel": "cancel", "cancel": False})
        self.assertEqual([c for c in FAKE.calls if c[:2] == ("DELETE", "/raids")], [])

    def test_bad_raid_name_rejected_before_network(self):
        FAKE.calls.clear()
        for bad in ("../users", "!", "", {"channel": None}, {}, 5):
            with self.assertRaises(ApiError):
                TW.raid_key(bad)
        self.assertEqual(FAKE.calls, [])
        with self.assertRaises(ApiError):
            TW.raid_key("nobody")

    def test_shoutout_by_name(self):
        TW.shoutout_key("friend")
        call_ = [c for c in FAKE.calls if c[:2] == ("POST", "/chat/shoutouts")][0]
        self.assertEqual(call_[2], {"from_broadcaster_id": "1001", "to_broadcaster_id": "2002",
                                    "moderator_id": "1001"})
        self.assertEqual(self.toasts, ["Shouted out friend"])

    def test_shoutout_last_raider_uses_id(self):
        self.alerts = types.SimpleNamespace(last_raider=lambda: {
            "login": "raider", "user_id": "777", "name": "Raider", "viewers": 5, "ts": 1.0})
        FAKE.calls.clear()
        TW.shoutout_key("last_raider")
        self.assertEqual([c[:2] for c in FAKE.calls if c[1] != "/users" or c[2].get("login")],
                         [("POST", "/chat/shoutouts")])
        self.assertFalse([c for c in FAKE.calls if c[1] == "/users" and "login" in c[2]])
        self.assertEqual(FAKE.calls[-1][2]["to_broadcaster_id"], "777")
        self.assertEqual(self.toasts, ["Shouted out Raider"])

    def test_manual_shoutouts_reach_alerts_cooldowns(self):
        noted = []
        self.alerts = types.SimpleNamespace(note_shoutout=noted.append, last_raider=lambda: {
            "login": "raider", "user_id": "777", "name": "Raider", "viewers": 5, "ts": 1.0})
        TW.shoutout_key("friend")                       # by name (also the panel's path)
        self.assertEqual(noted, ["2002"])
        TW.last = {}
        TW.shoutout_key("last_raider")
        self.assertEqual(noted, ["2002", "777"])
        with self.assertRaises(ApiError):
            TW.shoutout_key("nobody")                   # failed before Twitch shouted anyone out
        with self.assertRaises(ApiError):
            TW.shoutout_key("last_raider")              # throttled: not sent
        self.assertEqual(noted, ["2002", "777"])

    def test_last_raider_nobody(self):
        for alerts in (None, types.SimpleNamespace(last_raider=lambda: None)):
            self.alerts = alerts
            with self.assertRaises(ApiError) as cm:
                TW.shoutout_key("last_raider")
            self.assertEqual(str(cm.exception), "nobody has raided yet")

    def test_shoutout_id_validation_and_throttle(self):
        FAKE.calls.clear()
        for bad in ("12a", "", "../x", True, None, 1.5, "1001"):
            with self.assertRaises(ApiError):
                TW.shoutout_id(bad)
        self.assertFalse([c for c in FAKE.calls if c[1] == "/chat/shoutouts"])
        TW.shoutout_id(42)
        with self.assertRaises(ApiError) as cm:
            TW.shoutout_id("43")
        self.assertEqual(cm.exception.status, 429)

    def test_shoutout_id_needs_scope(self):
        TW.logout()
        self.link([s for s in TW_MOD.SCOPE_LIST if s != "moderator:manage:shoutouts"])
        with self.assertRaises(ApiError):
            TW.shoutout_id("42")


class Static(unittest.TestCase):
    def test_reward_denied(self):
        self.assertLessEqual({"twitch_raid", "twitch_shoutout"}, REWARD_DENIED)

    def test_scopes(self):
        for s in ("moderator:read:followers", "channel:read:subscriptions", "bits:read"):
            self.assertIn(s, TW_MOD.SCOPE_LIST)


if __name__ == "__main__":
    unittest.main()
