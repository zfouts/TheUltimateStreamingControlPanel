"""The control panel's Twitch routes, against a fake Twitch (OAuth + Helix) so nothing
leaves the machine. Runs the real daemon (minus the hardware) like test_editor.py."""
import json
import os
import stat
import sys
import threading
import time
import types
import unittest
import unittest.mock
import urllib.error
import urllib.request

from tests.helpers import EXAMPLE, ROOT, Deck, free_port, tmpdir

import requests  # noqa: E402
import yaml  # noqa: E402

from core import editor_auth  # noqa: E402
from core.bot import DeckBot  # noqa: E402
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


class Auth(TwitchCase):
    def test_login_pending_has_no_device_code(self):
        FAKE.pending_polls = 10 ** 6   # never approved during this test
        status, raw = call("/api/twitch/login", {})
        self.assertEqual(status, 200)
        assert_clean(self, raw)
        state = json.loads(raw)
        self.assertEqual(state["pending"]["user_code"], "ABCD-EFGH")
        self.assertEqual(set(state["pending"]), {"user_code", "verification_uri", "expires_at"})
        self.assertFalse(state["linked"])
        again = ok_json("/api/twitch/login", {})   # non-re-entrant: same flow comes back
        self.assertEqual(again["pending"], state["pending"])
        assert_clean(self, call("/api/twitch/auth")[1])
        assert_clean(self, TW.snapshot())
        self.assertNotIn(DEVICE_CODE, json.dumps(vars(TW), default=str))

    def test_link_writes_private_token_and_reports_user(self):
        q = BOT.hub.subscribe()
        try:
            state = self.link()
        finally:
            BOT.hub.unsubscribe(q)
        path = os.path.join(STATE, "twitch.json")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path) as f:
            self.assertEqual(json.load(f)["scope"], TW_MOD.SCOPE_LIST)
        self.assertEqual(set(state), {"configured", "linked", "user", "scopes", "missing_scopes", "pending", "error"})
        self.assertEqual(state["user"], {"login": "streamer", "display_name": "Streamer",
                                         "avatar": USER["profile_image_url"]})
        self.assertEqual(state["scopes"], TW_MOD.SCOPE_LIST)
        self.assertEqual(state["missing_scopes"], [])
        self.assertIsNone(state["pending"])
        self.assertIsNone(state["error"])
        self.assertEqual(state["configured"], {"channel": False, "client_id": True})
        assert_clean(self, json.dumps(state))
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        auth_events = [e for e in events if e["type"] == "twitch_auth"]
        self.assertGreaterEqual(len(auth_events), 2)   # started, then linked
        assert_clean(self, json.dumps([e["data"] for e in auth_events]))

    def test_linked_account_mismatch_is_reported(self):
        TW.channel = "someoneelse"
        state = self.link()
        self.assertTrue(state["linked"])
        self.assertEqual(state["error"], "Linked account streamer does not match twitch.channel someoneelse")

    def test_missing_scope_reported_and_enforced(self):
        state = self.link([s for s in TW_MOD.SCOPE_LIST if s != "user:write:chat"])
        self.assertEqual(state["missing_scopes"], ["user:write:chat"])
        status, raw = call("/api/twitch/chat/send", {"text": "hello"})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(raw)["error"], "Re-link Twitch to allow user:write:chat")
        self.assertFalse(any(c[1] == "/chat/messages" for c in FAKE.calls))

    def test_legacy_token_without_scopes_is_validated_hourly(self):
        self.link()
        TW.tok.pop("scope")
        self.assertEqual(ok_json("/api/twitch/auth")["missing_scopes"], ["user:write:chat"])
        ok_json("/api/twitch/auth")
        self.assertEqual(FAKE.validates, 1)
        self.assertEqual(TW.snapshot()["missing_scopes"], ["user:write:chat"])

    def test_logout_revokes_and_deletes(self):
        self.link()
        token = FAKE.valid
        r = ok_json("/api/twitch/logout", {})
        self.assertEqual(r, {"ok": True, "revoked": True})
        self.assertEqual(FAKE.revoked, [token])
        self.assertFalse(os.path.exists(os.path.join(STATE, "twitch.json")))
        state = ok_json("/api/twitch/auth")
        self.assertFalse(state["linked"])
        self.assertIsNone(state["user"])
        status, raw = call("/api/twitch/chat/send", {"text": "hi"})
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(raw)["error"], "Twitch isn’t linked")

    def test_corrupt_token_file_means_not_linked(self):
        with open(os.path.join(STATE, "twitch.json"), "w") as f:
            f.write("{not json")
        TW.on_load()
        self.assertFalse(TW.linked())
        self.assertFalse(ok_json("/api/twitch/auth")["linked"])

    def test_concurrent_refresh_happens_once(self):
        self.link()
        FAKE.valid = "rotated-by-twitch"   # the stored access token is now expired
        FAKE.unauthorized_barrier = threading.Barrier(2)
        FAKE.refresh_delay = 0.2
        results, errors = [], []

        def hit():
            try:
                results.append(TW.api("GET", "/users")["data"][0]["id"])
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=hit) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(results, ["1001", "1001"])
        self.assertEqual(FAKE.refreshes, 1)
        with open(os.path.join(STATE, "twitch.json")) as f:
            self.assertEqual(json.load(f)["access_token"], FAKE.valid)

    def expire(self):
        self.link()
        FAKE.valid = "rotated-by-twitch"       # the access token is expired...
        FAKE.refresh_token = "revoked"         # ...and the refresh token is dead

    def test_dead_refresh_token_is_not_retried(self):
        self.expire()
        with self.assertRaises(TW_MOD.SessionExpired):
            TW.api("GET", "/users")
        calls, refreshes = len(FAKE.calls), FAKE.refreshes
        self.assertEqual(refreshes, 1)
        with unittest.mock.patch.object(TW_MOD.requests, "request", side_effect=AssertionError("network")), \
                unittest.mock.patch.object(TW_MOD.requests, "post", side_effect=AssertionError("network")):
            with self.assertRaises(TW_MOD.SessionExpired):
                TW.api("GET", "/users")
        self.assertEqual((len(FAKE.calls), FAKE.refreshes), (calls, refreshes))
        self.assertTrue(TW.auth_error)

    def test_relinking_clears_dead_refresh_and_auth_error(self):
        self.expire()
        with self.assertRaises(TW_MOD.SessionExpired):
            TW.api("GET", "/users")
        self.assertTrue(TW.dead_refresh)
        FAKE.reset()
        ok_json("/api/twitch/login", {})
        deadline = time.time() + 5
        while TW.dead_refresh and time.time() < deadline:
            time.sleep(0.02)
        self.assertIsNone(TW.dead_refresh)
        self.assertIsNone(TW.auth_error)
        self.assertEqual(TW.api("GET", "/users")["data"][0]["id"], "1001")

    def test_transient_token_endpoint_failure_is_not_a_dead_login(self):
        self.link()
        FAKE.valid = "rotated-by-twitch"
        real = FAKE.post
        down = [Resp(503, {"message": "down"})]

        def post(url, *a, **kw):
            if url.endswith("/oauth2/token") and kw.get("data", {}).get("grant_type") == "refresh_token" and down:
                return down.pop()
            return real(url, *a, **kw)
        with unittest.mock.patch.object(TW_MOD.requests, "post", post):
            with self.assertRaises(ApiError) as cm:
                TW.api("GET", "/users")
            self.assertEqual(cm.exception.status, 502)
            self.assertIsNone(TW.auth_error)
            self.assertIsNone(TW.dead_refresh)
            self.assertTrue(TW.linked())
            self.assertEqual(TW.api("GET", "/users")["data"][0]["id"], "1001")   # refreshes again
        self.assertEqual(FAKE.refreshes, 1)

    def test_network_error_on_refresh_is_transient(self):
        self.link()
        FAKE.valid = "rotated-by-twitch"
        with unittest.mock.patch.object(TW_MOD.requests, "post", side_effect=requests.ConnectionError("boom")):
            with self.assertRaises(ApiError) as cm:
                TW.api("GET", "/users")
        self.assertEqual(cm.exception.status, 502)
        self.assertIsNone(TW.auth_error)

    def test_set_token_clears_auth_error_except_mismatch(self):
        TW.auth_error = "Twitch session expired; link Twitch again"
        with TW.tok_lock:
            TW._set_token({"access_token": "a", "refresh_token": "r"})
        self.assertIsNone(TW.auth_error)
        TW.auth_error = TW_MOD.MISMATCH + "x is not the configured channel"
        with TW.tok_lock:
            TW._set_token({"access_token": "a", "refresh_token": "r"})
        self.assertTrue(TW.auth_error.startswith(TW_MOD.MISMATCH))

    def test_providers_never_make_requests_on_the_calling_thread(self):
        self.link()
        TW.chat_cache, TW.live, TW.live_at = (0, None), None, 0
        main, seen = threading.get_ident(), []
        real = FAKE.request

        def request(*a, **kw):
            seen.append(threading.get_ident())
            return real(*a, **kw)
        with unittest.mock.patch.object(TW_MOD.requests, "request", request):
            self.assertFalse(TW.chat_state("emote", None))
            self.assertEqual(TW.viewers(None), {})
            self.assertNotIn(main, seen)                     # nothing ran synchronously
            for t in list(TW.bg_threads.values()):
                t.join(5)
            self.assertTrue(seen)
            self.assertNotIn(main, seen)
            self.assertIsNotNone(TW.chat_cache[1])
            self.assertIsNotNone(TW.live)

    def test_confidential_app_explained(self):
        self.link()
        FAKE.valid = "rotated-by-twitch"   # the stored access token is now expired
        FAKE.confidential = True
        with self.assertRaises(TW_MOD.SessionExpired) as cm:
            TW.api("GET", "/users")
        self.assertIn("Public", str(cm.exception))
        self.assertEqual(TW.auth_error, TW_MOD.CONFIDENTIAL_APP)

    def test_config_route_validates_and_writes(self):
        self.assertEqual(call("/api/twitch/config", {"channel": "not a name!"})[0], 400)
        self.assertEqual(call("/api/twitch/config", {"client_id": "short"})[0], 400)
        ok_json("/api/twitch/config", {"channel": "NewStreamer", "client_id": CLIENT_ID})
        with open(CONFIG) as f:
            self.assertEqual(yaml.safe_load(f)["twitch"]["channel"], "newstreamer")
        ok_json("/api/twitch/config", {"channel": ""})
        with open(CONFIG) as f:
            tw = yaml.safe_load(f)["twitch"]
        self.assertNotIn("channel", tw)
        self.assertEqual(tw["client_id"], CLIENT_ID)

    def test_changing_client_id_unlinks(self):
        self.link()
        token = FAKE.valid
        self.assertEqual(ok_json("/api/twitch/config", {"client_id": CLIENT_ID}), {"ok": True, "relink": False})
        self.assertTrue(TW.linked())
        try:
            r = ok_json("/api/twitch/config", {"client_id": "otherclientid98765"})  # privacy-check: allow (fake client id)
            self.assertEqual(r, {"ok": True, "relink": True})
            self.assertEqual(FAKE.revoked, [token])
            self.assertFalse(TW.linked())
            self.assertFalse(os.path.exists(os.path.join(STATE, "twitch.json")))
        finally:
            ok_json("/api/twitch/config", {"client_id": CLIENT_ID})


class Features(TwitchCase):
    def setUp(self):
        super().setUp()
        self.link()

    def test_chat_send_rate_limited(self):
        self.assertEqual(ok_json("/api/twitch/chat/send", {"text": "  hello  "}), {"ok": True})
        sent = [c for c in FAKE.calls if c[1] == "/chat/messages"][0][3]
        self.assertEqual(sent, {"broadcaster_id": "1001", "sender_id": "1001", "message": "hello"})
        self.assertEqual(call("/api/twitch/chat/send", {"text": "again"})[0], 429)
        self.assertEqual(call("/api/twitch/chat/announce", {"text": "again"})[0], 429)

    def assert_throttled(self, path, body, fresh=None):
        self.assertEqual(call(path, body)[0], 200, path)
        status, raw = call(path, fresh or body)
        self.assertEqual(status, 429, path)
        assert_clean(self, raw)

    def test_throttles_per_operation(self):
        self.assert_throttled("/api/twitch/chat/ban", {"user_id": "42", "seconds": 60})
        self.assert_throttled("/api/twitch/chat/delete", {"id": "abc-123"})
        self.assert_throttled("/api/twitch/shoutout", {"login": "friend"})
        self.assert_throttled("/api/twitch/raid", {"login": "friend"})
        self.assert_throttled("/api/twitch/ad", {"seconds": 30})
        self.assert_throttled("/api/twitch/marker", {})
        self.assert_throttled("/api/twitch/clip", {})
        self.assert_throttled("/api/twitch/channel", {"title": "T", "game_id": "", "game_name": "", "tags": []})
        # buckets are independent
        self.assertEqual(call("/api/twitch/chat/send", {"text": "hi"})[0], 200)

    def test_invalid_call_does_not_consume_slot(self):
        for path, bad, good in (
                ("/api/twitch/chat/ban", {"user_id": "x"}, {"user_id": "42", "seconds": 60}),
                ("/api/twitch/shoutout", {"login": "!"}, {"login": "friend"}),
                ("/api/twitch/raid", {"login": "!"}, {"login": "friend"}),
                ("/api/twitch/ad", {"seconds": 31}, {"seconds": 30}),
                ("/api/twitch/channel", {"title": "", "game_id": "", "game_name": "", "tags": []},
                 {"title": "T", "game_id": "", "game_name": "", "tags": []})):
            self.assertEqual(call(path, bad)[0], 400, path)
            self.assertEqual(call(path, good)[0], 200, path)

    def test_trailing_newline_rejected(self):
        self.assertEqual(call("/api/twitch/chat/ban", {"user_id": "123\n", "seconds": 60})[0], 400)
        self.assertEqual(call("/api/twitch/chat/delete", {"id": "abc\n"})[0], 400)
        body = {"title": "T", "game_id": "", "game_name": "", "tags": ["abc\n"]}
        self.assertEqual(call("/api/twitch/channel", body)[0], 400)

    def test_chat_send_validation(self):
        self.assertEqual(call("/api/twitch/chat/send", {"text": "   "})[0], 400)
        self.assertEqual(call("/api/twitch/chat/send", {"text": "x" * 501})[0], 400)
        self.assertEqual(call("/api/twitch/chat/send", {"text": 5})[0], 400)

    def test_slow_mode_seconds(self):
        st = ok_json("/api/twitch/chat/settings", {"mode": "slow", "on": True, "seconds": 10})
        patch = [c for c in FAKE.calls if c[:2] == ("PATCH", "/chat/settings")][-1][3]
        self.assertEqual(patch, {"slow_mode": True, "slow_mode_wait_time": 10})
        self.assertTrue(st["slow"])
        ok_json("/api/twitch/chat/settings", {"mode": "slow", "on": True})   # default from config
        self.assertEqual([c for c in FAKE.calls if c[:2] == ("PATCH", "/chat/settings")][-1][3]["slow_mode_wait_time"], 30)
        for bad in (2, 121, "10", True, 10.5):
            self.assertEqual(call("/api/twitch/chat/settings", {"mode": "slow", "on": True, "seconds": bad})[0], 400, bad)
        self.assertEqual(call("/api/twitch/chat/settings", {"mode": "emote", "on": True, "seconds": 10})[0], 400)

    def test_ban_validation(self):
        for body in ({"user_id": "42"}, {"user_id": "42", "seconds": 0}, {"user_id": "42", "seconds": 1209601},
                     {"user_id": "42", "seconds": "60"}, {"user_id": "42", "seconds": True},
                     {"user_id": "4x2", "seconds": 60}, {"user_id": "42", "seconds": 60, "reason": "r" * 501},
                     {"user_id": "42", "permanent": "yes"}, {"user_id": "1001", "permanent": True}):
            self.assertEqual(call("/api/twitch/chat/ban", body)[0], 400, body)
        self.assertFalse(any(c[1] == "/moderation/bans" for c in FAKE.calls))
        ok_json("/api/twitch/chat/ban", {"user_id": "42", "seconds": 600, "reason": "spam"})
        TW.last = {}   # past the ban throttle
        ok_json("/api/twitch/chat/ban", {"user_id": "43", "permanent": True})
        bans = [c for c in FAKE.calls if c[1] == "/moderation/bans"]
        self.assertEqual(bans[0][3], {"data": {"user_id": "42", "reason": "spam", "duration": 600}})
        self.assertEqual(bans[1][3], {"data": {"user_id": "43", "reason": ""}})
        self.assertEqual(bans[0][2], {"broadcaster_id": "1001", "moderator_id": "1001"})

    def test_channel_update_validation(self):
        good = {"title": "New title", "game_id": "33214", "game_name": "Fortnite", "tags": ["English", "Chill"]}
        for bad in ({"tags": ["bad tag"]}, {"tags": ["x" * 26]}, {"tags": [f"t{i}" for i in range(11)]},
                    {"title": "   "}, {"title": "x" * 141}, {"game_id": "12; DROP"}, {"tags": "English"}):
            self.assertEqual(call("/api/twitch/channel", {**good, **bad})[0], 400, bad)
        self.assertFalse(any(c[:2] == ("PATCH", "/channels") for c in FAKE.calls))
        r = ok_json("/api/twitch/channel", good)
        self.assertTrue(r["ok"])
        self.assertEqual(r["channel"]["history"][0], good)
        patch = [c for c in FAKE.calls if c[:2] == ("PATCH", "/channels")][0]
        self.assertEqual(patch[2], {"broadcaster_id": "1001"})
        self.assertEqual(patch[3], {"title": "New title", "game_id": "33214", "tags": ["English", "Chill"]})

    def test_categories_filter_box_art(self):
        cats = ok_json("/api/twitch/categories?q=fort")["categories"]
        self.assertEqual(cats, [
            {"id": "33214", "name": "Fortnite", "box_art": "https://static-cdn.jtvnw.net/ttv-boxart/33214.jpg"},
            {"id": "666", "name": "Fortnight Sim", "box_art": None}])
        ok_json("/api/twitch/categories?q=FORT")   # cached
        self.assertEqual(len([c for c in FAKE.calls if c[1] == "/search/categories"]), 1)
        self.assertEqual(ok_json("/api/twitch/categories?q=f")["categories"], [])

    def test_clip_ad_and_stream(self):
        self.assertEqual(ok_json("/api/twitch/clip", {}), {"ok": True, "url": "https://clips.twitch.tv/Clip/edit"})
        self.assertEqual(ok_json("/api/twitch/ad", {"seconds": 90}), {"ok": True, "seconds": 90})
        for bad in (45, 200, "60", None):
            self.assertEqual(call("/api/twitch/ad", {"seconds": bad})[0], 400, bad)
        stream = ok_json("/api/twitch/stream")
        self.assertEqual(stream["live"], False)
        self.assertEqual(TW.snapshot()["live"], stream)

    def test_raid_and_shoutout_logins(self):
        self.assertEqual(call("/api/twitch/raid", {"login": "../users"})[0], 400)
        self.assertEqual(call("/api/twitch/raid", {"login": "nobody"})[0], 404)
        ok_json("/api/twitch/raid", {"login": "friend_1"})
        raid = [c for c in FAKE.calls if c[1] == "/raids"][0]
        self.assertEqual(raid[2], {"from_broadcaster_id": "1001", "to_broadcaster_id": "2002"})
        ok_json("/api/twitch/shoutout", {"login": "friend_1"})


class ChatReader(unittest.TestCase):
    def capture(self, line):
        chat, got = BOT.get_module("Chat"), []
        BOT.dispatch = lambda event, msg: got.append(msg)
        try:
            chat._handle(line, lambda s: None)
        finally:
            del BOT.dispatch
        return got[0]

    def test_crafted_color_and_badges(self):
        msg = self.capture(r"@badges=moderator/1,subscriber/12,evil<b>/1;color=red\:background:url(x);"
                           r"display-name=Bob;id=abc-123;user-id=42 :bob!bob@bob.tmi.twitch.tv PRIVMSG #chan :hi there")  # privacy-check: allow (IRC hostmask format, not an email)
        self.assertEqual(msg["color"], "")
        self.assertEqual(msg["badges"], ["moderator", "subscriber"])
        self.assertEqual((msg["id"], msg["user_id"], msg["user"], msg["text"]), ("abc-123", "42", "Bob", "hi there"))
        self.assertTrue(msg["mod"] and msg["sub"])
        ok = self.capture("@color=#1E90ff;display-name=Ann :ann!ann@x PRIVMSG #chan :yo")
        self.assertEqual(ok["color"], "#1E90ff")
        self.assertEqual(ok["badges"], [])

    def test_snapshot(self):
        self.assertEqual(BOT.get_module("Chat").snapshot(), {"connected": False, "channel": ""})


if __name__ == "__main__":
    unittest.main()
