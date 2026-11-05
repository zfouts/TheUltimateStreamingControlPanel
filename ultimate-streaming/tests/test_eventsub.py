"""modules/eventsub.py against a fake Twitch module and fake websockets: nothing leaves the machine."""
import collections
import json
import logging
import threading
import time
import unittest
from datetime import datetime, timezone

from tests import helpers  # noqa: F401  (sandboxes HOME, puts vendor/ on sys.path)

import websocket  # noqa: E402

from core.webapi import ApiError  # noqa: E402
from modules import eventsub  # noqa: E402
from modules.eventsub import REDEMPTION, EventSub, valid_reconnect_url  # noqa: E402

ME = "1001"
ALL_SCOPES = ["channel:manage:redemptions", "user:write:chat", "moderator:read:followers",
              "channel:read:subscriptions", "bits:read"]


def stamp(age=0.0):
    """An RFC 3339 timestamp `age` seconds ago, with nanoseconds like Twitch sends."""
    dt = datetime.fromtimestamp(time.time() - age, timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:06d}123Z"


class FakeTwitch:
    def __init__(self, scopes=None):
        self.is_linked = True
        self.scopes = list(ALL_SCOPES if scopes is None else scopes)
        self.calls = []
        self.fail = {}           # subscription type -> exception to raise

    def linked(self):
        return self.is_linked

    def me(self):
        return {"id": ME, "login": "streamer"}

    def granted_scopes(self, network=True):
        assert network is False, "EventSub must not hit the network for scopes"
        return list(self.scopes)

    def api(self, method, path, json=None, params=None):
        self.calls.append((method, path, json))
        kind = (json or {}).get("type")
        if kind in self.fail:
            raise self.fail[kind]
        return {"data": [{"id": f"sub-{len(self.calls)}", "type": kind, "status": "enabled"}]}


class FakeHub:
    def __init__(self):
        self.events = []

    def publish(self, kind, data=None):
        self.events.append((kind, data))


class FakeAlerts:
    def __init__(self, wants=True):
        self.wants = wants

    def wants_events(self):
        if isinstance(self.wants, Exception):
            raise self.wants
        return self.wants


class FakeBot:
    def __init__(self, twitch, alerts=None):
        self.twitch = twitch
        self.alerts = alerts
        self.hub = FakeHub()
        self.dispatched = []

    def get_module(self, name):
        return {"Twitch": self.twitch, "Alerts": self.alerts}.get(name)

    def dispatch(self, event, *args):
        self.dispatched.append((event,) + args)


class FakeWS:
    """A websocket that hands out queued messages, then times out (or blocks briefly) forever."""

    def __init__(self, messages=()):
        self.queue = collections.deque(messages)
        self.connected = True
        self.closed = False
        self.aborted = threading.Event()

    def settimeout(self, t):
        self.timeout = t

    def recv(self):
        if self.aborted.is_set() or self.closed:
            raise websocket.WebSocketConnectionClosedException("closed")
        if self.queue:
            return self.queue.popleft()
        if self.aborted.wait(0.02):
            raise websocket.WebSocketConnectionClosedException("aborted")
        raise websocket.WebSocketTimeoutException("timed out")

    def abort(self):
        self.aborted.set()

    def close(self):
        self.closed = True
        self.connected = False


def msg(kind, payload, mid=None, ts=None, sub_type=None):
    meta = {"message_id": mid or f"m-{time.monotonic_ns()}", "message_type": kind,
            "message_timestamp": ts or stamp()}
    if sub_type:
        meta["subscription_type"] = sub_type
        meta["subscription_version"] = eventsub.SUBS[sub_type][0] if sub_type in eventsub.SUBS else "1"
    return json.dumps({"metadata": meta, "payload": payload})


def welcome(sid="S1", mid=None):
    return msg("session_welcome", {"session": {"id": sid, "status": "connected", "keepalive_timeout_seconds": 10,
                                               "reconnect_url": None, "connected_at": stamp()}}, mid=mid)


# What real notifications carry beside the condition we sent: the optional fields, echoed back empty.
ECHOED = {REDEMPTION: {"reward_id": ""}, "channel.raid": {"from_broadcaster_user_id": ""}}


def sub_obj(kind, sid="S1", broadcaster=ME):
    version, _, condition = eventsub.SUBS.get(kind, ("1", None, lambda me: {"broadcaster_user_id": me}))
    return {"id": "sub-x", "status": "enabled", "type": kind, "version": version, "cost": 0,
            "condition": {**condition(broadcaster), **ECHOED.get(kind, {})},
            "transport": {"method": "websocket", "session_id": sid}, "created_at": stamp()}


def redemption(eid="R1", mid=None, sid="S1", broadcaster=ME, user_input="purple", ts=None, kind=REDEMPTION):
    event = {"id": eid, "broadcaster_user_id": broadcaster, "broadcaster_user_login": "streamer",
             "broadcaster_user_name": "Streamer", "user_id": "42", "user_login": "viewer", "user_name": "Viewer",
             "user_input": user_input, "status": "unfulfilled",
             "reward": {"id": "RW1", "title": "Change the background", "cost": 500, "prompt": "color"},
             "redeemed_at": "2026-10-31T12:00:00.123456789Z"}
    return msg("notification", {"subscription": sub_obj(kind, sid, broadcaster), "event": event},
               mid=mid, ts=ts, sub_type=kind)


def reconnect(url, sid="S1"):
    return msg("session_reconnect", {"session": {"id": sid, "status": "reconnecting", "keepalive_timeout_seconds": None,
                                                 "reconnect_url": url, "connected_at": stamp()}})


class Base(unittest.TestCase):
    def setUp(self):
        self.twitch = FakeTwitch()
        self.bot = FakeBot(self.twitch)
        self.es = EventSub(self.bot)
        self.es.on_config({"enabled": True})
        self.conn = eventsub._Conn(FakeWS())
        self.es._conn = self.conn

    def tearDown(self):
        self.es.on_unload()

    def events(self):
        return [d for d in self.bot.dispatched if d[0] != "eventsub_session"]

    def redemptions(self):
        return [d for d in self.bot.dispatched if d[0] == "reward_redemption"]

    def subscribe_calls(self):
        return [c for c in self.twitch.calls if c[1] == "/eventsub/subscriptions"]


class TestMessages(Base):
    def test_welcome_subscribes_three_types_with_session_id(self):
        self.assertIsNone(self.es._on_message(welcome("S1")))
        calls = self.subscribe_calls()
        self.assertEqual([c[2]["type"] for c in calls], list(eventsub.BOT_TYPES))
        for method, _, body in calls:
            self.assertEqual(method, "POST")
            self.assertEqual(body["version"], "1")
            self.assertEqual(body["condition"], {"broadcaster_user_id": ME})
            self.assertEqual(body["transport"], {"method": "websocket", "session_id": "S1"})
        snap = self.es.snapshot()
        self.assertEqual(snap, {"connected": True, "subscriptions": sorted(eventsub.BOT_TYPES), "skipped": [],
                                "error": None})
        self.assertIn(("eventsub", snap), self.bot.hub.events)

    def test_missing_redemption_scope_subscribes_stream_only_and_reports_relink(self):
        self.twitch.scopes = ["user:write:chat"]
        self.es._on_message(welcome())
        self.assertEqual([c[2]["type"] for c in self.subscribe_calls()], ["stream.online", "stream.offline"])
        snap = self.es.snapshot()
        self.assertEqual(snap["error"], "relink")
        self.assertEqual(snap["subscriptions"], ["stream.offline", "stream.online"])
        self.assertTrue(snap["connected"])

    def test_scope_refused_by_twitch_reports_relink(self):
        self.twitch.fail[REDEMPTION] = ApiError("Re-link Twitch to allow this", 403)
        self.es._on_message(welcome())
        self.assertEqual(self.es.snapshot()["error"], "relink")

    def test_401_stops_subscribing_and_reports_relink(self):
        self.twitch.fail[eventsub.BOT_TYPES[0]] = ApiError("Twitch session expired", 401)
        self.assertEqual(self.es._on_message(welcome()), "reconnect")
        self.assertEqual(len(self.subscribe_calls()), 1)
        self.assertEqual(self.es.snapshot()["error"], "relink")

    def test_all_subscriptions_failing_ends_the_session(self):
        for kind in eventsub.BOT_TYPES:
            self.twitch.fail[kind] = ApiError("Twitch /eventsub/subscriptions: 500", 502)
        self.assertEqual(self.es._on_message(welcome()), "reconnect")
        self.assertFalse(self.es.snapshot()["connected"])

    def test_subscribe_window_is_enforced(self):
        real = time.monotonic
        start = real()
        clock = iter([start, start + 1, start + 11, start + 12, start + 13])
        eventsub.time.monotonic = lambda: next(clock, start + 20)
        try:
            self.assertEqual(self.es._on_message(welcome()), "reconnect")
        finally:
            eventsub.time.monotonic = real
        self.assertEqual(len(self.subscribe_calls()), 1)

    def test_redemption_dispatches_exact_payload(self):
        self.es._on_message(welcome())
        self.es._on_message(redemption())
        self.assertEqual(self.redemptions(), [("reward_redemption", {
            "id": "R1", "reward_id": "RW1", "reward_title": "Change the background", "user": "Viewer",
            "user_login": "viewer", "user_id": "42", "input": "purple", "status": "unfulfilled",
            "ts": eventsub.parse_ts("2026-10-31T12:00:00.123456789Z")})])
        payload = self.redemptions()[0][1]
        self.assertAlmostEqual(payload["ts"], datetime(2026, 10, 31, 12, 0, 0, 123457,
                                                       timezone.utc).timestamp(), places=3)

    def test_stream_online_offline_dispatch_payloads(self):
        self.es._on_message(welcome())
        started = stamp()
        for kind in ("stream.online", "stream.offline"):
            event = {"broadcaster_user_id": ME, "broadcaster_user_login": "streamer"}
            if kind == "stream.online":
                event.update(id="stream1", type="live\x1b[31m\u202e", started_at=started)
            self.es._on_message(msg("notification", {"subscription": sub_obj(kind), "event": event}, sub_type=kind))
        self.assertEqual(self.events(), [("stream_online", {"stream_id": "stream1", "type": "live",
                                                            "started_at": started}), ("stream_offline", {})])

    def test_duplicate_message_id_dropped(self):
        self.es._on_message(welcome())
        self.es._on_message(redemption(eid="R1", mid="dup"))
        self.es._on_message(redemption(eid="R2", mid="dup"))
        self.assertEqual([r[1]["id"] for r in self.redemptions()], ["R1"])

    def test_duplicate_event_id_with_new_message_id_dropped(self):
        self.es._on_message(welcome())
        self.es._on_message(redemption(eid="R1", mid="a"))
        self.es._on_message(redemption(eid="R1", mid="b"))
        self.assertEqual(len(self.redemptions()), 1)

    def test_dedup_survives_reconnect(self):
        self.es._on_message(welcome("S1"))
        self.es._on_message(redemption(eid="R1", mid="a", sid="S1"))
        self.es._conn = eventsub._Conn(FakeWS())          # a fresh session after a normal reconnect
        self.es._on_message(welcome("S2"))
        self.es._on_message(redemption(eid="R1", mid="a", sid="S2"))
        self.es._on_message(redemption(eid="R1", mid="b", sid="S2"))
        self.assertEqual(len(self.redemptions()), 1)

    def test_dedup_keeps_last_500(self):
        recent = eventsub._Recent()
        for i in range(501):
            self.assertTrue(recent.add(i))
        self.assertTrue(recent.add(0))       # evicted, so new again
        self.assertFalse(recent.add(500))
        self.assertEqual(len(recent.keys), 500)

    def test_wrong_broadcaster_dropped(self):
        self.es._on_message(welcome())
        self.es._on_message(redemption(broadcaster="666"))
        self.assertEqual(self.redemptions(), [])

    def test_wrong_broadcaster_in_event_only_dropped(self):
        self.es._on_message(welcome())
        raw = json.loads(redemption())
        raw["payload"]["event"]["broadcaster_user_id"] = "666"
        self.es._on_message(json.dumps(raw))
        self.assertEqual(self.redemptions(), [])

    def test_unknown_subscription_type_dropped(self):
        self.es._on_message(welcome())
        self.es._on_message(redemption(kind="channel.follow"))
        self.assertEqual(self.events(), [])

    def test_unsubscribed_type_dropped(self):
        self.twitch.scopes = []                # redemption type was never subscribed
        self.es._on_message(welcome())
        self.es._on_message(redemption())
        self.assertEqual(self.redemptions(), [])

    def test_metadata_type_mismatch_dropped(self):
        self.es._on_message(welcome())
        raw = json.loads(redemption())
        raw["metadata"]["subscription_type"] = "stream.online"
        self.es._on_message(json.dumps(raw))
        self.assertEqual(self.events(), [])

    def test_wrong_session_dropped(self):
        self.es._on_message(welcome("S1"))
        self.es._on_message(redemption(sid="S-other"))
        self.assertEqual(self.redemptions(), [])

    def test_notification_before_welcome_dropped(self):
        self.es._on_message(redemption())
        self.assertEqual(self.redemptions(), [])

    def test_stale_timestamp_dropped_and_skew_logged_once(self):
        self.es._on_message(welcome())
        with self.assertLogs("deck.EventSub", level="INFO") as logs:
            self.es._on_message(redemption(eid="R1", ts=stamp(age=601)))
            self.es._on_message(redemption(eid="R2", ts=stamp(age=3600)))
            self.es._on_message(redemption(eid="R3", ts=stamp(age=5)))
        self.assertEqual([r[1]["id"] for r in self.redemptions()], ["R3"])
        skew = [line for line in logs.output if "clock" in line]
        self.assertEqual(len(skew), 1)
        self.assertIn("WARNING", skew[0])

    def test_bad_timestamp_dropped(self):
        self.es._on_message(welcome())
        self.es._on_message(redemption(ts="yesterday"))
        self.assertEqual(self.redemptions(), [])

    def test_control_chars_stripped_and_input_capped(self):
        self.es._on_message(welcome())
        nasty = "pur\x00ple\r\n\x1b[31mred\x07‮ " + "x" * 600
        raw = json.loads(redemption(user_input=nasty))
        raw["payload"]["event"]["user_name"] = "Vie\nwer\x1b]0;title\x07"
        self.es._on_message(json.dumps(raw))
        r = self.redemptions()[0][1]
        self.assertEqual(len(r["input"]), 500)
        self.assertTrue(r["input"].startswith("purplered" + "x"))
        for value in r.values():
            if isinstance(value, str):
                self.assertFalse(any(ord(c) < 32 or 0x7f <= ord(c) < 0xa0 for c in value), value)
                self.assertNotIn("‮", value)
        self.assertNotIn("\n", r["user"])

    def test_info_logs_cap_viewer_text(self):
        self.es._on_message(welcome())
        with self.assertLogs("deck.EventSub", level="INFO") as logs:
            self.es._on_message(redemption(user_input="y" * 500 + "\x1b[2J"))
        line = next(line for line in logs.output if "redemption" in line)
        self.assertNotIn("y" * 101, line)
        self.assertNotIn("\x1b", line)

    def test_keepalive_is_ignored_quietly(self):
        self.es._on_message(welcome())
        self.assertIsNone(self.es._on_message(msg("session_keepalive", {})))
        self.assertEqual(self.events(), [])

    def test_garbage_is_dropped(self):
        for raw in ("not json", "[]", json.dumps({"metadata": {}}), json.dumps({"metadata": 1, "payload": {}})):
            self.assertIsNone(self.es._on_message(raw))
        self.assertEqual(self.events(), [])

    def test_revocation_logs_publishes_and_forgets_type(self):
        self.es._on_message(welcome())
        sub = dict(sub_obj(REDEMPTION), status="authorization_revoked")
        self.es._on_message(msg("revocation", {"subscription": sub}, sub_type=REDEMPTION))
        snap = self.es.snapshot()
        self.assertNotIn(REDEMPTION, snap["subscriptions"])
        self.assertEqual(snap["error"], "relink")
        self.assertEqual(self.bot.hub.events[-1], ("eventsub", snap))
        self.es._on_message(redemption())
        self.assertEqual(self.redemptions(), [])

    def test_revoking_last_subscription_disconnects(self):
        self.es._on_message(welcome())
        for kind in eventsub.BOT_TYPES:
            sub = dict(sub_obj(kind), status="authorization_revoked")
            self.es._on_message(msg("revocation", {"subscription": sub}, sub_type=kind))
            self.assertEqual(self.es.snapshot()["connected"], kind != eventsub.BOT_TYPES[-1], kind)
        self.assertEqual(self.es.snapshot()["subscriptions"], [])

    def test_fresh_session_dispatches_eventsub_session(self):
        self.es._on_message(welcome("S1"))
        self.assertEqual(self.bot.dispatched, [("eventsub_session", {})])
        moved = eventsub._Conn(FakeWS(), moved=True)
        self.es._on_message(welcome("S2"), moved)          # a reconnect_url move: no new session event
        self.assertEqual(self.bot.dispatched, [("eventsub_session", {})])

    def test_no_eventsub_session_when_nothing_subscribed(self):
        for kind in eventsub.BOT_TYPES:
            self.twitch.fail[kind] = ApiError("Twitch /eventsub/subscriptions: 500", 502)
        self.es._on_message(welcome())
        self.assertEqual(self.bot.dispatched, [])

    def test_clean_strips_format_chars_but_keeps_zwj(self):
        family = "\U0001F468\u200d\U0001F469\u200d\U0001F467"
        nasty = "a\u200eb\u200fc\u061cd\ufeffe\u202ef\u2066g\u200bh\u00adi" + family
        self.assertEqual(eventsub.clean(nasty), "abcdefghi" + family)

    def test_revocation_for_other_session_ignored(self):
        self.es._on_message(welcome())
        sub = dict(sub_obj(REDEMPTION, sid="other"), status="authorization_revoked")
        self.es._on_message(msg("revocation", {"subscription": sub}, sub_type=REDEMPTION))
        self.assertIn(REDEMPTION, self.es.snapshot()["subscriptions"])


TS = "2026-10-31T12:00:00.5Z"


def viewer(**extra):
    return {"user_id": "42", "user_login": "Viewer_1", "user_name": "Viewer", **extra}


def bcast(**extra):
    return {"broadcaster_user_id": ME, "broadcaster_user_login": "streamer", "broadcaster_user_name": "Streamer",
            **extra}


EVENTS = {
    "channel.follow": lambda: bcast(**viewer(followed_at=TS)),
    "channel.subscribe": lambda: bcast(**viewer(tier="1000", is_gift=False)),
    "channel.subscription.message": lambda: bcast(**viewer(
        tier="2000", message={"text": "Love the stream", "emotes": []}, cumulative_months=14, streak_months=3,
        duration_months=6)),
    "channel.subscription.gift": lambda: bcast(**viewer(total=5, tier="3000", cumulative_total=40,
                                                        is_anonymous=False)),
    "channel.cheer": lambda: bcast(**viewer(is_anonymous=False, message="Cheer100 gg", bits=100)),
    "channel.raid": lambda: {"from_broadcaster_user_id": "77", "from_broadcaster_user_login": "Raider",
                             "from_broadcaster_user_name": "RaiderName", "to_broadcaster_user_id": ME,
                             "to_broadcaster_user_login": "streamer", "to_broadcaster_user_name": "Streamer",
                             "viewers": 123},
}


def alert(kind, event=None, mid=None, sid="S1", ts=None):
    event = EVENTS[kind]() if event is None else event
    return msg("notification", {"subscription": sub_obj(kind, sid), "event": event}, mid=mid, ts=ts,
               sub_type=kind)


def expected(**over):
    base = {"kind": "", "user": "Viewer", "user_login": "viewer_1", "user_id": "42", "anonymous": False, "tier": 0,
            "months": 0, "cumulative_months": 0, "total": 0, "bits": 0, "viewers": 0, "message": "", "ts": 1.0}
    base.update(over)
    return base


class AlertBase(Base):
    def setUp(self):
        super().setUp()
        self.alerts = FakeAlerts(True)
        self.bot.alerts = self.alerts

    def alerts_out(self):
        return [d[1] for d in self.bot.dispatched if d[0] == "twitch_event"]


class TestNormalise(unittest.TestCase):
    def norm(self, kind, **change):
        event = EVENTS[kind]()
        for key, value in change.items():
            if value is KeyError:
                event.pop(key, None)
            else:
                event[key] = value
        return eventsub.normalise(kind, event, 1.0, ME)

    def test_happy_path_each_kind(self):
        self.assertEqual(self.norm("channel.follow"), expected(kind="follow"))
        self.assertEqual(self.norm("channel.subscribe"), expected(kind="sub", tier=1))
        self.assertEqual(self.norm("channel.subscription.message"), expected(
            kind="resub", tier=2, months=6, cumulative_months=14, message="Love the stream"))
        self.assertEqual(self.norm("channel.subscription.gift"), expected(kind="gift", tier=3, total=5))
        self.assertEqual(self.norm("channel.cheer"), expected(kind="cheer", bits=100, message="Cheer100 gg"))
        self.assertEqual(self.norm("channel.raid"), expected(kind="raid", user="RaiderName", user_login="raider",
                                                             user_id="77", viewers=123))
        self.assertEqual(eventsub.normalise("follow", EVENTS["channel.follow"](), 1.0, ME), expected(kind="follow"))
        self.assertIsInstance(self.norm("channel.follow")["ts"], float)

    def test_anonymous_gift_and_cheer(self):
        anon = {"user_id": None, "user_login": None, "user_name": None, "is_anonymous": True}
        self.assertEqual(self.norm("channel.subscription.gift", **anon), expected(
            kind="gift", user="Anonymous", user_login="", user_id="", anonymous=True, tier=3, total=5))
        self.assertEqual(self.norm("channel.cheer", **anon), expected(
            kind="cheer", user="Anonymous", user_login="", user_id="", anonymous=True, bits=100,
            message="Cheer100 gg"))
        for truthy in ("true", 1, "yes"):                       # strict: only the bool True
            ev = self.norm("channel.cheer", is_anonymous=truthy)
            self.assertFalse(ev["anonymous"])
            self.assertEqual(ev["user"], "Viewer")
        ev = self.norm("channel.follow", is_anonymous=True)     # not a field follows have
        self.assertFalse(ev["anonymous"])

    def test_unusable_name_falls_back_to_login(self):
        self.assertEqual(self.norm("channel.follow", user_name="​‮")["user"], "viewer_1")
        raid = eventsub.normalise("channel.raid", {**EVENTS["channel.raid"](), "from_broadcaster_user_name": None},
                                  1.0, ME)
        self.assertEqual(raid["user"], "raider")

    def test_bad_tier_dropped(self):
        for kind in ("channel.subscribe", "channel.subscription.message", "channel.subscription.gift"):
            for tier in ("4000", "1", 1000, None, KeyError, "prime"):
                self.assertIsNone(self.norm(kind, tier=tier), (kind, tier))
        self.assertEqual(self.norm("channel.cheer", tier="1000")["tier"], 0)

    def test_gifted_subscribe_dropped(self):
        self.assertIsNone(self.norm("channel.subscribe", is_gift=True))
        self.assertIsNone(self.norm("channel.subscribe", is_gift="false"))
        self.assertIsNone(self.norm("channel.subscribe", is_gift=KeyError))

    def test_raid_checks(self):
        self.assertIsNone(self.norm("channel.raid", to_broadcaster_user_id="666"))
        self.assertIsNone(self.norm("channel.raid", to_broadcaster_user_id=KeyError))
        self.assertIsNone(self.norm("channel.raid", from_broadcaster_user_id=ME))              # self-raid
        for bad in ("abc", "", None, 77, "1" * 21, "7 7"):
            self.assertIsNone(self.norm("channel.raid", from_broadcaster_user_id=bad), bad)
        for bad in ("", None, "a" * 26, "rai der", "raider<script>", "r\u00e1ider", "raider\n"):
            self.assertIsNone(self.norm("channel.raid", from_broadcaster_user_login=bad), bad)
        self.assertEqual(self.norm("channel.raid", from_broadcaster_user_login="X")["user_login"], "x")

    def test_event_broadcaster_must_be_present_and_ours(self):
        for kind in ("channel.follow", "channel.subscribe", "channel.subscription.message",
                     "channel.subscription.gift", "channel.cheer"):
            self.assertIsNone(self.norm(kind, broadcaster_user_id="666"), kind)
            self.assertIsNone(self.norm(kind, broadcaster_user_id=KeyError), kind)
            self.assertIsNone(self.norm(kind, broadcaster_user_id=int(ME)), kind)
        self.assertIsNone(eventsub.normalise("channel.follow", EVENTS["channel.follow"](), 1.0, None))
        self.assertIsNone(eventsub.normalise("channel.ban", EVENTS["channel.follow"](), 1.0, ME))
        self.assertIsNone(eventsub.normalise("channel.follow", "nope", 1.0, ME))

    def test_combining_marks_collapsed_and_lengths_capped(self):
        zalgo = "x" + "\u0301" * 5 + "q\u0300\u0301\u0302\u0303"       # no precomposed forms
        ev = self.norm("channel.cheer", user_name=zalgo, message="h" + "\u0336" * 50 + "i")
        self.assertEqual(ev["user"], "x\u0301\u0301q\u0300\u0301")
        self.assertEqual(ev["message"], "h\u0336\u0336i")
        ev = self.norm("channel.cheer", user_name="x" + "\u0301" * 40 + "yz")  # capped at 25, then collapsed
        self.assertEqual(ev["user"], "x\u0301\u0301")
        ev = self.norm("channel.cheer", user_name="N" * 40 + "\u202e\x1b[31m", message="m" * 900)
        self.assertEqual(ev["user"], "N" * 25)
        self.assertEqual(ev["message"], "m" * 500)
        self.assertEqual(self.norm("channel.follow", user_name="e\u0301")["user"], "\u00e9")    # NFC
        ev = self.norm("channel.subscription.message", message={"text": "a\x00b\u2028c" + "\u0300" * 9})
        self.assertEqual(ev["message"], "abc\u0300\u0300")
        for value in ev.values():
            if isinstance(value, str):
                self.assertFalse(any(ord(c) < 32 for c in value))

    def test_enclosing_and_class_zero_marks_capped(self):
        ev = self.norm("channel.cheer", user_name="a" + "\u0489" * 50, message="b" + "\u20dd" * 50 + "c")
        self.assertEqual(ev["user"], "a\u0489\u0489")
        self.assertEqual(ev["message"], "b\u20dd\u20ddc")
        self.assertEqual(eventsub.collapse_marks("x" + "\u0489\u0301\u20dd\u0300"), "x\u0489\u0301")

    def test_unusable_user_fields_blank(self):
        ev = self.norm("channel.follow", user_name=None, user_login="Bad Login!", user_id="4x2")
        self.assertEqual((ev["user"], ev["user_login"], ev["user_id"]), ("", "", ""))
        ev = self.norm("channel.follow", user_name={"x": 1}, user_login=42, user_id=42)
        self.assertEqual((ev["user"], ev["user_login"], ev["user_id"]), ("", "", ""))
        self.assertEqual(self.norm("channel.follow", user_login="a")["user_login"], "a")   # old 1-char login

    def test_ints_strict(self):
        for bad in (True, False, "100", 1.5, -1, 10_000_001, None):
            self.assertEqual(self.norm("channel.cheer", bits=bad)["bits"], 0, bad)
            self.assertEqual(self.norm("channel.raid", viewers=bad)["viewers"], 0, bad)
            self.assertEqual(self.norm("channel.subscription.gift", total=bad)["total"], 0, bad)
            ev = self.norm("channel.subscription.message", duration_months=bad, cumulative_months=bad)
            self.assertEqual((ev["months"], ev["cumulative_months"]), (0, 0), bad)
        self.assertEqual(self.norm("channel.cheer", bits=10_000_000)["bits"], 10_000_000)
        self.assertEqual(self.norm("channel.cheer", bits=0)["bits"], 0)

    def test_message_wrong_shape_blank(self):
        self.assertEqual(self.norm("channel.subscription.message", message="text")["message"], "")
        self.assertEqual(self.norm("channel.subscription.message", message={"text": 5})["message"], "")
        self.assertEqual(self.norm("channel.cheer", message={"text": "x"})["message"], "")
        self.assertEqual(self.norm("channel.subscription.message", message=None)["message"], "")


class TestAlerts(AlertBase):
    def test_welcome_subscribes_alert_types_with_versions_and_conditions(self):
        self.es._on_message(welcome("S1"))
        bodies = {c[2]["type"]: c[2] for c in self.subscribe_calls()}
        self.assertEqual(list(bodies), list(eventsub.BOT_TYPES + eventsub.ALERT_TYPES))
        self.assertEqual(bodies["channel.follow"]["version"], "2")
        self.assertEqual(bodies["channel.follow"]["condition"], {"broadcaster_user_id": ME, "moderator_user_id": ME})
        self.assertEqual(bodies["channel.raid"]["condition"], {"to_broadcaster_user_id": ME})
        for kind in ("channel.subscribe", "channel.subscription.message", "channel.subscription.gift",
                     "channel.cheer"):
            self.assertEqual((bodies[kind]["version"], bodies[kind]["condition"]), ("1", {"broadcaster_user_id": ME}))
        for body in bodies.values():
            self.assertEqual(body["transport"], {"method": "websocket", "session_id": "S1"})
        snap = self.es.snapshot()
        self.assertEqual(snap["skipped"], [])
        self.assertIsNone(snap["error"])
        json.dumps(snap)

    def test_each_kind_dispatches_twitch_event(self):
        self.es._on_message(welcome())
        ts = stamp()
        with self.assertLogs("deck.EventSub", level="DEBUG") as logs:
            for kind in eventsub.ALERT_TYPES:
                self.es._on_message(alert(kind, ts=ts))
        out = self.alerts_out()
        self.assertEqual([e["kind"] for e in out], ["follow", "sub", "resub", "gift", "cheer", "raid"])
        self.assertEqual(out[2], expected(kind="resub", tier=2, months=6, cumulative_months=14,
                                          message="Love the stream", ts=eventsub.parse_ts(ts)))
        # per-event lines (they carry the viewer's name) are DEBUG only, so a flood stays out of the journal
        per_event = [r for r in logs.records if " from " in r.getMessage()]
        self.assertEqual(len(per_event), 6)
        self.assertEqual({r.levelname for r in per_event}, {"DEBUG"})
        for r in logs.records:
            if r.levelname != "DEBUG" or not r.getMessage().startswith("EventSub raw"):
                self.assertNotIn("Love the stream", r.getMessage())
                self.assertNotIn("Cheer100", r.getMessage())

    def test_alert_events_without_alerts_module_subscription_dropped(self):
        self.bot.alerts = None
        self.es._on_message(welcome())
        self.assertEqual([c[2]["type"] for c in self.subscribe_calls()], list(eventsub.BOT_TYPES))
        for kind in eventsub.ALERT_TYPES:
            self.es._on_message(alert(kind))
        self.assertEqual(self.alerts_out(), [])

    def test_alerts_not_wanted_means_no_alert_subscriptions(self):
        for wants in (False, None, 0, RuntimeError("boom")):
            self.twitch.calls.clear()
            self.alerts.wants = wants
            self.es._on_message(welcome(f"S-{wants!r}"))
            self.assertEqual([c[2]["type"] for c in self.subscribe_calls()], list(eventsub.BOT_TYPES), wants)
            self.es._conn = eventsub._Conn(FakeWS())

    def test_alerts_only_when_bot_disabled(self):
        self.es.on_config({"enabled": False})
        self.es._on_message(welcome())
        self.assertEqual([c[2]["type"] for c in self.subscribe_calls()], list(eventsub.ALERT_TYPES))
        self.es._on_message(redemption())
        self.assertEqual(self.redemptions(), [])

    def test_wanted_with_alerts_or_bot(self):
        self.es._loaded = True
        self.assertTrue(self.es._wanted())
        self.es.config["enabled"] = False
        self.assertTrue(self.es._wanted())
        self.alerts.wants = False
        self.assertFalse(self.es._wanted())
        self.alerts.wants = True
        self.twitch.is_linked = False
        self.assertFalse(self.es._wanted())
        self.es._loaded = False

    def test_missing_scope_skips_only_that_type_without_relink(self):
        self.twitch.scopes = ["channel:manage:redemptions", "channel:read:subscriptions"]
        with self.assertLogs("deck.EventSub", level="INFO") as logs:
            self.es._on_message(welcome())
        types = [c[2]["type"] for c in self.subscribe_calls()]
        self.assertNotIn("channel.follow", types)
        self.assertNotIn("channel.cheer", types)
        self.assertIn("channel.raid", types)
        self.assertIn("channel.subscribe", types)
        snap = self.es.snapshot()
        self.assertEqual(snap["skipped"], ["channel.cheer", "channel.follow"])
        self.assertIsNone(snap["error"])
        self.assertTrue(any("channel.follow" in line and "moderator:read:followers" in line and "INFO" in line
                            for line in logs.output))
        self.es._conn = eventsub._Conn(FakeWS())          # a reconnect does not log the same skip again
        with self.assertLogs("deck.EventSub", level="INFO") as logs:
            self.es._on_message(welcome("S2"))
        self.assertFalse(any("skipping" in line for line in logs.output))
        self.es._on_message(alert("channel.follow", sid="S2"))
        self.assertEqual(self.alerts_out(), [])

    def test_missing_redemption_scope_still_relinks_with_alerts(self):
        self.twitch.scopes = ["channel:read:subscriptions"]
        self.es._on_message(welcome())
        self.assertEqual(self.es.snapshot()["error"], "relink")

    def test_403_on_follow_skips_only_it(self):
        self.twitch.fail["channel.follow"] = ApiError("Forbidden", 403)
        self.es._on_message(welcome())
        snap = self.es.snapshot()
        self.assertEqual(snap["skipped"], ["channel.follow"])
        self.assertIsNone(snap["error"])
        self.assertEqual(snap["subscriptions"], sorted(t for t in eventsub.SUBS if t != "channel.follow"))
        self.assertTrue(snap["connected"])

    def test_other_failure_on_alert_type_reports_subscribe(self):
        self.twitch.fail["channel.cheer"] = ApiError("Twitch /eventsub/subscriptions: 500", 502)
        self.es._on_message(welcome())
        self.assertEqual(self.es.snapshot()["error"], "subscribe")
        self.assertEqual(self.es.snapshot()["skipped"], [])

    def test_extra_or_wrong_condition_key_dropped(self):
        self.es._on_message(welcome())
        for kind, cond in (("channel.follow", {"broadcaster_user_id": ME, "moderator_user_id": ME, "x": "1"}),
                           ("channel.follow", {"broadcaster_user_id": ME}),
                           ("channel.follow", {"broadcaster_user_id": ME, "moderator_user_id": "666"}),
                           ("channel.cheer", {"broadcaster_user_id": ME, "user_id": "42"}),
                           ("channel.raid", {"to_broadcaster_user_id": ME, "from_broadcaster_user_id": "77"}),
                           ("channel.raid", {"from_broadcaster_user_id": ME}),
                           ("channel.raid", {"broadcaster_user_id": ME})):
            raw = json.loads(alert(kind))
            raw["payload"]["subscription"]["condition"] = cond
            self.es._on_message(json.dumps(raw))
        self.assertEqual(self.alerts_out(), [])

    def test_condition_matching(self):
        match = eventsub.condition_matches
        self.assertTrue(match({"to_broadcaster_user_id": ME, "from_broadcaster_user_id": ""},
                              {"to_broadcaster_user_id": ME}))
        self.assertTrue(match({"broadcaster_user_id": ME, "reward_id": None}, {"broadcaster_user_id": ME}))
        self.assertFalse(match({"broadcaster_user_id": ME, "reward_id": "abc"}, {"broadcaster_user_id": ME}))
        self.assertFalse(match({"broadcaster_user_id": ME, "x": 0}, {"broadcaster_user_id": ME}))
        self.assertFalse(match({"reward_id": ""}, {"broadcaster_user_id": ME}))
        self.assertFalse(match({"broadcaster_user_id": None}, {"broadcaster_user_id": ME}))
        self.assertFalse(match(None, {"broadcaster_user_id": ME}))

    def test_realistic_raid_and_redemption_conditions_accepted(self):
        self.es._on_message(welcome())
        raw = json.loads(alert("channel.raid"))
        self.assertEqual(raw["payload"]["subscription"]["condition"],
                         {"to_broadcaster_user_id": ME, "from_broadcaster_user_id": ""})
        self.es._on_message(json.dumps(raw))
        raw = json.loads(redemption())
        self.assertEqual(raw["payload"]["subscription"]["condition"], {"broadcaster_user_id": ME, "reward_id": ""})
        self.es._on_message(json.dumps(raw))
        self.assertEqual([e["kind"] for e in self.alerts_out()], ["raid"])
        self.assertEqual(len(self.redemptions()), 1)

    def test_condition_extra_value_or_missing_key_dropped(self):
        self.es._on_message(welcome())
        for cond in ({"to_broadcaster_user_id": ME, "from_broadcaster_user_id": "999"},
                     {"from_broadcaster_user_id": ""}):
            raw = json.loads(alert("channel.raid"))
            raw["payload"]["subscription"]["condition"] = cond
            self.es._on_message(json.dumps(raw))
        for cond in ({"broadcaster_user_id": ME, "reward_id": "abc"}, {"reward_id": ""}):
            raw = json.loads(redemption(eid=f"R-{cond['reward_id']}"))
            raw["payload"]["subscription"]["condition"] = cond
            self.es._on_message(json.dumps(raw))
        self.assertEqual(self.alerts_out(), [])
        self.assertEqual(self.redemptions(), [])

    def test_revocation_with_realistic_condition(self):
        self.es._on_message(welcome())
        for kind in ("channel.raid", REDEMPTION):
            sub = dict(sub_obj(kind), status="authorization_revoked")
            self.assertIn("", sub["condition"].values())
            self.es._on_message(msg("revocation", {"subscription": sub}))
            self.assertNotIn(kind, self.es.snapshot()["subscriptions"])
        sub = dict(sub_obj("channel.cheer"), status="authorization_revoked",
                   condition={"broadcaster_user_id": ME, "user_id": "42"})
        self.es._on_message(msg("revocation", {"subscription": sub}))
        self.assertIn("channel.cheer", self.es.snapshot()["subscriptions"])

    def test_wrong_version_dropped(self):
        self.es._on_message(welcome())
        raw = json.loads(alert("channel.follow"))
        raw["metadata"]["subscription_version"] = "1"
        self.es._on_message(json.dumps(raw))
        raw = json.loads(alert("channel.follow"))
        raw["payload"]["subscription"]["version"] = "1"
        self.es._on_message(json.dumps(raw))
        raw = json.loads(alert("channel.cheer"))
        del raw["metadata"]["subscription_version"]
        self.es._on_message(json.dumps(raw))
        self.assertEqual(self.alerts_out(), [])
        self.es._on_message(alert("channel.follow"))
        self.assertEqual(len(self.alerts_out()), 1)

    def test_revocation_checks_version_and_condition(self):
        self.es._on_message(welcome())
        sub = dict(sub_obj("channel.follow"), status="authorization_revoked", version="1")
        self.es._on_message(msg("revocation", {"subscription": sub}))
        sub = dict(sub_obj("channel.follow"), status="authorization_revoked",
                   condition={"broadcaster_user_id": ME})
        self.es._on_message(msg("revocation", {"subscription": sub}))
        self.assertIn("channel.follow", self.es.snapshot()["subscriptions"])
        sub = dict(sub_obj("channel.follow"), status="authorization_revoked")
        self.es._on_message(msg("revocation", {"subscription": sub}))
        self.assertNotIn("channel.follow", self.es.snapshot()["subscriptions"])

    def test_invalid_events_dropped_end_to_end(self):
        self.es._on_message(welcome())
        bad = [("channel.subscribe", bcast(**viewer(tier="1000", is_gift=True))),
               ("channel.subscribe", bcast(**viewer(tier="9000", is_gift=False))),
               ("channel.raid", dict(EVENTS["channel.raid"](), to_broadcaster_user_id="666")),
               ("channel.raid", dict(EVENTS["channel.raid"](), from_broadcaster_user_id=ME)),
               ("channel.raid", dict(EVENTS["channel.raid"](), from_broadcaster_user_login="bad login")),
               ("channel.cheer", dict(EVENTS["channel.cheer"](), broadcaster_user_id="666")),
               ("channel.follow", viewer()),                                   # no broadcaster id
               ("channel.follow", bcast(**viewer(user_id="nope")))]           # can't be deduplicated
        with self.assertLogs("deck.EventSub", level="INFO") as logs:
            for kind, event in bad:
                self.es._on_message(alert(kind, event=dict(event, user_name="Evil" + "x" * 200)))
        self.assertEqual(self.alerts_out(), [])
        for line in logs.output:
            self.assertNotIn("x" * 101, line)

    def test_follow_refollow_deduped_within_a_day(self):
        clock = [1000.0]
        self.es._seen_follows = eventsub._Expiring(clock=lambda: clock[0])
        self.es._on_message(welcome())
        self.es._on_message(alert("channel.follow"))
        self.es._on_message(alert("channel.follow"))                            # unfollow + follow again
        self.es._on_message(alert("channel.follow", event=bcast(**viewer(user_id="43"))))
        clock[0] += 23 * 3600
        self.es._on_message(alert("channel.follow"))
        self.assertEqual([e["user_id"] for e in self.alerts_out()], ["42", "43"])
        clock[0] += 3601
        self.es._on_message(alert("channel.follow"))
        self.assertEqual([e["user_id"] for e in self.alerts_out()], ["42", "43", "42"])

    def test_other_kinds_rely_on_message_id_dedup(self):
        self.es._on_message(welcome())
        self.es._on_message(alert("channel.cheer", mid="c1"))
        self.es._on_message(alert("channel.cheer", mid="c1"))
        self.es._on_message(alert("channel.cheer", mid="c2"))
        self.assertEqual(len(self.alerts_out()), 2)

    def test_expiring_is_bounded_and_expires(self):
        clock = [0.0]
        seen = eventsub._Expiring(ttl=10, size=3, clock=lambda: clock[0])
        for key in "abc":
            self.assertTrue(seen.add(key))
        self.assertFalse(seen.add("a"))
        self.assertTrue(seen.add("d"))              # full: "a" forgotten
        self.assertEqual(len(seen.keys), 3)
        self.assertTrue(seen.add("a"))
        clock[0] = 10
        self.assertTrue(seen.add("c"))              # expired
        self.assertEqual(list(seen.keys), ["c"])
        self.assertEqual(eventsub._Expiring().size, 5000)
        self.assertEqual(eventsub._Expiring().ttl, 24 * 3600)


class TestAlertLoop(AlertBase):
    def start(self, sockets):
        self.opened = []
        sockets = collections.deque(sockets)

        def connect(url):
            self.opened.append(url)
            return sockets.popleft() if sockets else FakeWS()
        self.es._connect = connect
        self.es.on_load()

    def wait(self, cond, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return True
            time.sleep(0.01)
        self.fail("condition not reached")

    def test_desired_set_change_restarts_the_session(self):
        self.alerts.wants = False
        first = FakeWS([welcome("S1")])
        second = FakeWS([welcome("S2"), alert("channel.raid", sid="S2")])
        self.start([first, second, FakeWS([welcome("S3")])])
        self.wait(lambda: self.es.snapshot()["connected"])
        self.assertEqual(self.es.snapshot()["subscriptions"], sorted(eventsub.BOT_TYPES))
        worker = self.es._worker
        self.es.supervise()                               # nothing changed: no restart
        self.assertIs(self.es._worker, worker)
        self.alerts.wants = True
        self.es.supervise()
        self.assertFalse(worker.is_alive())
        self.assertTrue(first.closed)
        self.wait(lambda: self.alerts_out())
        self.assertEqual(self.opened, [eventsub.URL, eventsub.URL])
        self.assertEqual(self.es.snapshot()["subscriptions"], sorted(eventsub.SUBS))
        self.assertIsNot(self.es._worker, worker)
        self.es.on_config({"enabled": False})              # bot off, alerts on: restart with alerts only
        self.wait(lambda: len(self.opened) == 3 and self.es.snapshot()["connected"])
        self.assertEqual(self.es.snapshot()["subscriptions"], sorted(eventsub.ALERT_TYPES))

    def test_relink_with_new_scopes_restarts_the_session(self):
        self.twitch.scopes = [s for s in self.twitch.scopes if s != "moderator:read:followers"]
        self.start([FakeWS([welcome("S1")]), FakeWS([welcome("S2")])])
        self.wait(lambda: self.es.snapshot()["connected"])
        self.assertIn("channel.follow", self.es.snapshot()["skipped"])
        worker = self.es._worker
        self.twitch.scopes = self.twitch.scopes + ["some:unrelated:scope"]
        self.es.supervise()                               # a scope no wanted type needs: no restart
        self.assertIs(self.es._worker, worker)
        self.twitch.scopes = self.twitch.scopes + ["moderator:read:followers"]   # re-linked
        self.es.supervise()
        self.assertFalse(worker.is_alive())
        self.wait(lambda: len(self.opened) == 2 and self.es.snapshot()["connected"])
        self.assertIn("channel.follow", self.es.snapshot()["subscriptions"])
        self.assertEqual(self.es.snapshot()["skipped"], [])

    def test_started_for_alerts_with_bot_disabled(self):
        self.es.on_config({"enabled": False})
        self.start([FakeWS([welcome("S1")])])
        self.wait(lambda: self.es.snapshot()["connected"])
        self.alerts.wants = False
        worker = self.es._worker
        self.es.supervise()
        worker.join(2)
        self.assertFalse(worker.is_alive())


class TestReconnect(Base):
    def test_reconnect_url_rules(self):
        good = ["wss://eventsub.wss.twitch.tv/ws?challenge=abc", "wss://eventsub.wss.twitch.tv:443/ws",
                "wss://edge-1.eventsub.twitch.tv/ws", "WSS://EventSub.WSS.Twitch.TV/ws"]
        bad = ["http://eventsub.wss.twitch.tv/ws", "ws://eventsub.wss.twitch.tv/ws", "https://eventsub.wss.twitch.tv",
               "wss://evil.com/ws", "wss://x.twitch.tv.evil.com/ws", "wss://user@eventsub.wss.twitch.tv/ws",  # privacy-check: allow
               "wss://user:pw@eventsub.wss.twitch.tv/ws", "wss://@eventsub.wss.twitch.tv/ws",  # privacy-check: allow
               "wss://eventsub.wss.twitch.tv:8443/ws", "wss://eventsub.wss.twitch.tv:abc/ws",
               "wss://eviltwitch.tv/ws", "wss://.twitch.tv/ws", "wss://twitch.tv/ws",
               "wss://evil.com\\@eventsub.wss.twitch.tv/ws", "wss://eventsub.wss.twitch.tv./ws",
               "wss://eventsub.wss.twitch.tv/ws\r\nHost: evil", "", None, 42]
        for url in good:
            self.assertTrue(valid_reconnect_url(url), url)
        for url in bad:
            self.assertFalse(valid_reconnect_url(url), url)

    def test_valid_reconnect_url_moves(self):
        self.es._on_message(welcome("S1"))
        url = "wss://eventsub.wss.twitch.tv/ws?challenge=abc"
        self.assertEqual(self.es._on_message(reconnect(url)), ("move", url))

    def test_invalid_reconnect_url_does_normal_reconnect(self):
        for url in ("http://eventsub.wss.twitch.tv/ws", "wss://evil.com/ws", "wss://x.twitch.tv.evil.com/ws",
                    "wss://user@eventsub.wss.twitch.tv/ws", "wss://eventsub.wss.twitch.tv:8443/ws"):  # privacy-check: allow
            self.es._conn = eventsub._Conn(FakeWS())
            self.es._on_message(welcome("S1"))
            self.assertEqual(self.es._on_message(reconnect(url)), "reconnect", url)

    def test_reconnect_for_another_session_ignored(self):
        self.es._on_message(welcome("S1"))
        self.assertIsNone(self.es._on_message(reconnect("wss://eventsub.wss.twitch.tv/ws", sid="S9")))


class TestLoop(Base):
    """The real worker thread, with the websocket factory replaced."""

    def start(self, sockets):
        self.opened = []
        sockets = collections.deque(sockets)

        def connect(url):
            self.opened.append(url)
            return sockets.popleft() if sockets else FakeWS()
        self.es._connect = connect
        self.es.on_load()

    def wait(self, cond, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return True
            time.sleep(0.01)
        self.fail("condition not reached")

    def test_end_to_end_redemption(self):
        self.start([FakeWS([welcome("S1"), redemption()])])
        self.wait(lambda: self.redemptions())
        self.assertEqual(self.opened, [eventsub.URL])
        self.assertTrue(self.es.snapshot()["connected"])

    def test_reconnect_url_move_keeps_subscriptions_and_switches_socket(self):
        url = "wss://eventsub.wss.twitch.tv/ws?challenge=abc"
        old = FakeWS([welcome("S1"), reconnect(url)])
        new = FakeWS([welcome("S2"), redemption(eid="R2", sid="S2")])
        self.start([old, new])
        self.wait(lambda: self.redemptions())
        self.assertEqual(self.opened, [eventsub.URL, url])
        self.assertEqual(len(self.subscribe_calls()), 3)      # no resubscribe after a move
        self.wait(lambda: old.closed)
        self.assertFalse(new.closed)
        self.assertEqual([d for d in self.bot.dispatched if d[0] == "eventsub_session"],
                         [("eventsub_session", {})])          # only the first, fresh session

    def test_move_accepts_previous_session_ids_only(self):
        self.es._on_message(welcome("S1"))
        old = self.conn
        new = eventsub._Conn(FakeWS(), moved=True)
        self.es._on_message(welcome("S2"), new)
        new.accept |= old.accept                          # what _move does after the new welcome
        self.es._on_message(redemption(eid="R1", sid="S1"), new)
        self.es._on_message(redemption(eid="R2", sid="S2"), new)
        self.es._on_message(redemption(eid="R3", sid="S-never-ours"), new)
        self.assertEqual([r[1]["id"] for r in self.redemptions()], ["R1", "R2"])

    def test_move_loop_keeps_old_session_ids_on_new_socket(self):
        url = "wss://eventsub.wss.twitch.tv/ws?challenge=abc"
        old = FakeWS([welcome("S1"), reconnect(url)])
        new = FakeWS([welcome("S2"), redemption(eid="R1", sid="S1"), redemption(eid="R2", sid="S2"),
                      redemption(eid="R3", sid="S-never-ours")])
        self.start([old, new])
        self.wait(lambda: len(self.redemptions()) >= 2)
        time.sleep(0.1)
        self.assertEqual([r[1]["id"] for r in self.redemptions()], ["R1", "R2"])
        self.assertEqual(self.es._conn.accept, {"S1", "S2"})

    def test_concurrent_sync_starts_one_worker(self):
        self.start([])
        self.es.on_unload()
        self.es._loaded = True
        started = []
        real = threading.Thread

        class Counting(real):
            def start(inner):
                started.append(inner)
                time.sleep(0.01)          # widen the check-then-start window
                super().start()
        eventsub.threading.Thread = Counting
        try:
            barrier = threading.Barrier(8)

            def go():
                barrier.wait()
                self.es._sync()
            threads = [real(target=go) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5)
        finally:
            eventsub.threading.Thread = real
        self.assertEqual(len(started), 1)

    def test_evil_reconnect_url_is_never_opened(self):
        first = FakeWS([welcome("S1"), reconnect("wss://evil.com/ws")])
        second = FakeWS([welcome("S2")])
        self.es._next_delay = lambda seconds: 0.01
        self.start([first, second])
        self.wait(lambda: len(self.subscribe_calls()) == 6)
        self.assertEqual(self.opened, [eventsub.URL, eventsub.URL])
        self.assertTrue(first.closed)

    def test_keepalive_timeout_reconnects(self):
        self.es._next_delay = lambda seconds: 0.01
        first = FakeWS([welcome("S1")])
        self.start([first])
        self.wait(lambda: self.es._conn is not None and self.es._conn.session_id == "S1")
        self.es._conn.keepalive = -5          # deadline already passed: next poll tick gives up
        self.wait(lambda: len(self.opened) >= 2)
        self.assertTrue(first.closed)

    def test_backoff_doubles_with_jitter_and_resets_after_long_session(self):
        delays = [self.es._next_delay(1) for _ in range(8)]
        for got, base in zip(delays, (1, 2, 4, 8, 16, 32, 60, 60)):
            self.assertGreaterEqual(got, base)
            self.assertLessEqual(got, min(60, base * 1.25))
        self.assertLessEqual(self.es._next_delay(61), 1.25)
        self.assertGreaterEqual(self.es._next_delay(5), 2)
        self.assertLessEqual(self.es._next_delay(60.5), 1.25)

    def test_worker_stops_on_unload(self):
        self.start([FakeWS([welcome("S1")])])
        self.wait(lambda: self.es.snapshot()["connected"])
        worker = self.es._worker
        self.es.on_unload()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(self.es.snapshot()["connected"])

    def test_worker_stops_on_disable_and_restarts(self):
        self.start([FakeWS([welcome("S1")])])
        self.wait(lambda: self.es.snapshot()["connected"])
        worker = self.es._worker
        self.es.on_config({"enabled": False})
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.es.snapshot(), {"connected": False, "subscriptions": [], "skipped": [],
                                              "error": None})
        self.es.on_config({"enabled": True})
        self.wait(lambda: len(self.opened) == 2)

    def test_worker_stops_on_unlink_via_task(self):
        self.start([FakeWS([welcome("S1")])])
        self.wait(lambda: self.es.snapshot()["connected"])
        worker = self.es._worker
        self.twitch.is_linked = False
        self.es.supervise()
        worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_not_started_unless_linked_and_enabled(self):
        self.twitch.is_linked = False
        self.start([])
        self.es.supervise()
        self.assertIsNone(self.es._worker)
        self.twitch.is_linked = True
        self.es.on_config({"enabled": "yes"})       # only a real true enables it
        self.assertIsNone(self.es._worker)
        self.assertEqual(self.opened, [])

    def test_uses_verified_tls(self):
        import ssl
        seen = {}
        real = websocket.create_connection
        websocket.create_connection = lambda url, **kw: seen.update(kw, url=url) or FakeWS()
        try:
            EventSub(self.bot)._connect(eventsub.URL)
        finally:
            websocket.create_connection = real
        ctx = seen["sslopt"]["context"]
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()
