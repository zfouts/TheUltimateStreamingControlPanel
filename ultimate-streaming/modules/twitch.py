"""Twitch: clips, stream markers, ads, chat modes and viewer count, plus everything the
web control panel drives (login, title/category/tags, chat send and moderation, raids).

Uses the Helix API with the device-code login, so no client secret is needed:
    twitch:
      channel: yourname  # or env TWITCH_CHANNEL; enough for the chat / stream manager / channel links
      client_id: "..."   # or env TWITCH_CLIENT_ID; dev.twitch.tv/console → Register Your Application
                         # (Client Type: Public). A public client id is not a secret.
Then press a key with `action: {twitch: login}` (or "Link Twitch" in the control panel) and
approve in the browser. The token is kept in ~/.config/ultimate-streaming/twitch.json (mode 600),
never in config.yaml, and never leaves the daemon: auth_state() and snapshot() are allow-lists.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time

import requests

from core import Module, action, display, notify, open_url, state, task
from core.schema import UI, Bool, Choice, Text
from core.paths import state_file
from core.safefs import write_atomic
from core.webapi import ApiError
from core.twitch_ids import LOGIN_RE, USER_ID_RE
DIALOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui", "stream_info.py")
API = "https://api.twitch.tv/helix"
OAUTH = "https://id.twitch.tv/oauth2"
SCOPES = ("clips:edit channel:edit:commercial channel:manage:broadcast moderator:manage:chat_settings "
          "user:write:chat moderator:manage:chat_messages moderator:manage:banned_users "
          "moderator:manage:announcements moderator:manage:shoutouts channel:manage:raids "
          "channel:manage:redemptions moderator:read:followers channel:read:subscriptions bits:read")
SCOPE_LIST = SCOPES.split()
CHAT_MODES = {"emote": "emote_mode", "followers": "follower_mode", "slow": "slow_mode",
              "subs": "subscriber_mode", "unique": "unique_chat_mode"}
CDN = "https://static-cdn.jtvnw.net/"
NOT_LINKED = "Twitch isn’t linked"
CONFIDENTIAL_APP = ("Your Twitch app is set to Client Type “Confidential”, so its logins expire after about 4 "
                    "hours. Register a new app at dev.twitch.tv with Client Type “Public”, put its Client ID "
                    "in the Twitch view, and link again.")
MISMATCH = "Linked account "

TITLE_MAX, TAGS_MAX = 140, 10            # same limits as ui/stream_info.py
TAG_RE = re.compile(r"^[A-Za-z0-9]{1,25}$")
# LOGIN_RE / USER_ID_RE live in core/twitch_ids.py. INPUT_LOGIN_RE is the looser rule for a typed name.
INPUT_LOGIN_RE = re.compile(r"^[A-Za-z0-9_]{3,25}$")
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9]{10,64}$")
GAME_ID_RE = re.compile(r"^[0-9]{0,20}$")
MSG_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
LONG_RE = re.compile(r"[A-Za-z0-9]{20,}")  # token-shaped: never in logs or error messages
AD_LENGTHS = (30, 60, 90, 120, 150, 180)
ANNOUNCE_COLORS = ("primary", "blue", "green", "orange", "purple")
BAN_MAX = 1209600                         # 14 days, Twitch's longest timeout
CHAT_MAX = 500
CHAT_EVERY = 1.0                          # seconds between chat messages / announcements
CHAT_BURST, CHAT_WINDOW = 20, 30.0        # at most 20 chat messages + announcements per 30 s (Twitch's own cap)
VALIDATE_EVERY = 3600
STATUS_TTL, CATEGORY_TTL, CHAT_TTL = 15, 60, 2
LOGIN_UNAVAILABLE = "Twitch login is unavailable right now"


class SessionExpired(RuntimeError):
    status = 401


def _clean(text, n=200):
    """Error text that is safe to log or show: token-shaped runs removed, then truncated."""
    return LONG_RE.sub("[…]", str(text or ""))[:n]


def _cdn(url):
    return url if isinstance(url, str) and url.startswith(CDN) else None


def _text(value, name, limit, required=True):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ApiError(f"{name} must be text")
    value = value.strip()
    if required and not value:
        raise ApiError(f"{name} can't be empty")
    if len(value) > limit:
        raise ApiError(f"{name} is longer than {limit} characters")
    return value


def _login(value):
    if not isinstance(value, str) or not INPUT_LOGIN_RE.fullmatch(value.strip()):
        raise ApiError("not a Twitch username (3–25 letters, numbers or _)")
    return value.strip().lower()


class Twitch(Module):
    def on_config(self, config):
        super().on_config(config)
        self.client_id = (os.environ.get("TWITCH_CLIENT_ID") or config.get("client_id") or "").strip() or None
        self.channel = (os.environ.get("TWITCH_CHANNEL") or config.get("channel") or "").strip().lower()
        self.token_file, self.history_file = state_file("twitch.json"), state_file("stream_history.json")
        self.slow_seconds = int(config.get("slow_mode_seconds", 30))
        # A "linked account doesn't match" error follows the config: fixing twitch.channel clears it.
        if (getattr(self, "auth_error", None) or "").startswith(MISMATCH):
            self.auth_error = self._mismatch()

    def on_load(self):
        self.tok = {}
        try:
            with open(self.token_file) as f:
                tok = json.load(f)
            if isinstance(tok, dict):
                self.tok = tok
            else:
                self.log.warning("%s is not a Twitch token; link Twitch again", self.token_file)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            self.log.warning("can't read %s (%s); link Twitch again", self.token_file, type(e).__name__)
        self.user = None
        self.chat_cache = (0, None)
        self.live, self.live_at = None, 0
        self.categories = {}
        self.pending = None
        self.auth_error = None
        self.dead_refresh = None         # a refresh token Twitch refused (400/401); never retried while stored
        self.bg_threads = {}             # kind -> running background refresh
        self.validated = None            # (time, scopes) from /oauth2/validate, for tokens without a scope list
        self.login_thread = None
        self.cancel = threading.Event()
        self.login_lock = threading.Lock()   # outer: one login flow at a time; logout
        self.tok_lock = threading.Lock()     # inner: every write of self.tok (refresh, login, logout)
        self.misc_lock = threading.Lock()    # rate limit, caches, history file
        self.last = {}                       # throttle kind -> last time
        self.status_thread = None

    def on_unload(self):
        self.cancel.set()

    # ----- auth

    def linked(self):
        return bool(self.client_id and self.tok.get("refresh_token"))

    def login_name(self):
        if self.channel:
            return self.channel
        if not self.linked():
            raise RuntimeError("set twitch.channel (or link Twitch) in config.yaml")
        return self.me()["login"]

    def _save(self):
        write_atomic(self.token_file, json.dumps(self.tok).encode("utf-8"))

    def _set_token(self, data):
        """Store a token response from Twitch (caller holds tok_lock)."""
        tok = {k: data[k] for k in ("access_token", "refresh_token", "token_type", "expires_in") if k in data}
        scope = data.get("scope")
        if isinstance(scope, list):
            tok["scope"] = [s for s in scope if isinstance(s, str)]
        elif isinstance(self.tok.get("scope"), list):
            tok["scope"] = self.tok["scope"]   # a refresh keeps the grant's scopes
        tok["obtained_at"] = int(time.time())
        self.tok = tok
        self.validated = None
        self.dead_refresh = None
        if not (self.auth_error or "").startswith(MISMATCH):
            self.auth_error = None
        self._save()

    def _token(self, **data):
        return requests.post(f"{OAUTH}/token", data={"client_id": self.client_id, **data}, timeout=10)

    def _publish_auth(self):
        hub = getattr(self.bot, "hub", None)
        if hub:
            try:
                hub.publish("twitch_auth", self.auth_state())
            except Exception as e:
                self.log.debug("twitch_auth publish: %s", _clean(e))

    def _mismatch(self):
        login = (self.user or {}).get("login") or ""
        if self.channel and login and login.lower() != self.channel:
            return f"Linked account {login} does not match twitch.channel {self.channel}"
        return None

    def login(self, open_browser=True):
        """Start Twitch's device-code login; the token is collected in the background.
        Returns the pending flow ({user_code, verification_uri, expires_at, interval})."""
        if not self.client_id:
            raise RuntimeError("set twitch.client_id in config.yaml (or TWITCH_CLIENT_ID) first; see README")
        with self.login_lock:
            if self.pending and self.pending["expires_at"] > time.time() and \
                    self.login_thread and self.login_thread.is_alive():
                return dict(self.pending)
            self.cancel.set()                        # a stale flow, if any, stops polling
            cancel = self.cancel = threading.Event()
            r = requests.post(f"{OAUTH}/device", data={"client_id": self.client_id, "scopes": SCOPES}, timeout=10)
            if r.status_code == 400 and "invalid client" in r.text:
                raise RuntimeError("Twitch doesn't recognize twitch.client_id: copy the Client ID "
                                   "(not the secret) from dev.twitch.tv/console/apps into config.yaml")
            if not r.ok:
                raise RuntimeError(f"Twitch login failed: {r.status_code} {_clean(r.text)}")
            d = r.json()
            device_code = d.pop("device_code")       # lives only in wait()'s closure
            uri = d.get("verification_uri") or ""
            if not uri.startswith("https://"):
                uri = "https://www.twitch.tv/activate"
            interval = max(0.01, float(d.get("interval") or 5))
            expires_at = time.time() + float(d.get("expires_in") or 1800)
            self.pending = {"user_code": str(d.get("user_code") or ""), "verification_uri": uri,
                            "expires_at": expires_at, "interval": interval}
            self.auth_error = None
            pending = dict(self.pending)

            def finish(error):
                with self.login_lock:
                    if cancel.is_set():
                        return
                    self.pending, self.auth_error = None, error
                self._publish_auth()
                if open_browser:
                    notify(error)

            def wait():
                wait_for = interval
                while not cancel.wait(wait_for):
                    if time.time() >= expires_at:
                        return finish("Twitch login timed out; start again")
                    try:
                        r = self._token(scopes=SCOPES, device_code=device_code,
                                        grant_type="urn:ietf:params:oauth:grant-type:device_code")
                    except requests.RequestException as e:
                        self.log.debug("login poll: %s", type(e).__name__)
                        continue
                    if r.ok:
                        with self.login_lock:
                            if cancel.is_set():
                                return
                            with self.tok_lock:
                                self._set_token(r.json())
                                self.user = None
                            self.pending = None
                        try:
                            name = self.me()["display_name"]
                            self.auth_error = self._mismatch()
                        except Exception as e:
                            name, self.auth_error = None, f"Linked, but reading your account failed: {_clean(e)}"
                        self.log.info("Twitch linked as %s", name or "?")
                        self._publish_auth()
                        if open_browser:
                            notify(f"Twitch linked as {name or '?'}")
                        return
                    try:
                        msg = str(r.json().get("message") or "")
                    except ValueError:
                        msg = ""
                    if msg == "authorization_pending":
                        continue
                    if msg == "slow_down":
                        wait_for += 5
                        continue
                    return finish(f"Twitch link failed: {_clean(msg) or r.status_code}")

            self.login_thread = threading.Thread(target=wait, daemon=True, name="twitch-login")
            self.login_thread.start()
        self._publish_auth()
        if open_browser:
            open_url(uri)
            notify(f"Approve the Stream Deck on Twitch (code {pending['user_code']})")
        return pending

    def logout(self):
        """Stop any login flow, revoke the token at Twitch and forget it. Returns {"revoked"}."""
        with self.login_lock:
            self.cancel.set()
            with self.tok_lock:   # waits for an in-flight refresh so it can't re-save the token
                tok, self.tok = self.tok, {}
                self.user, self.chat_cache, self.live, self.live_at = None, (0, None), None, 0
                self.pending, self.auth_error, self.validated = None, None, None
                self.dead_refresh = None
                self.categories = {}
                for path in (self.token_file, self.token_file + ".tmp"):
                    try:
                        os.remove(path)
                    except FileNotFoundError:
                        pass
        revoked = False
        if tok.get("access_token") and self.client_id:
            try:
                r = requests.post(f"{OAUTH}/revoke", data={"client_id": self.client_id,
                                                           "token": tok["access_token"]}, timeout=10)
                revoked = bool(r.ok)
            except requests.RequestException as e:
                self.log.info("Twitch revoke failed (%s); the token is forgotten anyway", type(e).__name__)
        self._publish_auth()
        return {"revoked": revoked}

    def _refresh(self, stale):
        """Refresh the access token once, unless another thread already replaced `stale`."""
        with self.tok_lock:
            if self.tok.get("access_token") != stale:
                return
            if not self.tok.get("refresh_token"):
                raise ApiError(NOT_LINKED, 401)
            refresh_token = self.tok["refresh_token"]
            try:
                r = self._token(grant_type="refresh_token", refresh_token=refresh_token)
            except requests.RequestException as e:
                self.log.warning("Twitch login renewal failed (%s)", type(e).__name__)
                raise ApiError(LOGIN_UNAVAILABLE, 502) from None
            if not r.ok:
                if "invalid client" in r.text:
                    raise RuntimeError("Twitch doesn't recognize twitch.client_id (see config.yaml)")
                try:
                    reason = r.json().get("message") or r.reason
                except ValueError:
                    reason = r.reason
                self.log.warning("Twitch refused to renew the login (HTTP %s: %s)", r.status_code, _clean(reason, 120))
                if r.status_code not in (400, 401):
                    raise ApiError(LOGIN_UNAVAILABLE, 502)   # 5xx, 429: Twitch is struggling, the token may be fine
                self.dead_refresh = refresh_token
                if "client secret" in str(reason).lower():
                    # A "Confidential" app gets device-code logins it then won't renew without a secret.
                    self.auth_error = CONFIDENTIAL_APP
                    raise SessionExpired(CONFIDENTIAL_APP)
                self.auth_error = "Twitch session expired; link Twitch again"
                raise SessionExpired("Twitch session expired; press Link Twitch again")
            self._set_token(r.json())

    def api(self, method, path, **kw):
        if not self.linked():
            raise ApiError(NOT_LINKED, 401)
        if self.dead_refresh and self.dead_refresh == self.tok.get("refresh_token"):
            raise SessionExpired(self.auth_error or "Twitch session expired; press Link Twitch again")
        for attempt in (1, 2):
            token = self.tok.get("access_token")
            if not token:
                raise ApiError(NOT_LINKED, 401)
            r = requests.request(method, API + path, timeout=8, **kw, headers={
                "Authorization": f"Bearer {token}", "Client-Id": self.client_id})
            if r.ok:
                return r.json() if r.content else {}
            try:
                msg = str((r.json() or {}).get("message") or "")
            except ValueError:
                msg = r.text
            msg = _clean(msg)
            if r.status_code == 401 and "scope" in msg.lower():
                raise ApiError(f"Re-link Twitch to allow this ({msg})", 403)
            if r.status_code == 401 and attempt == 1:
                try:
                    self._refresh(token)
                except SessionExpired:
                    self._publish_auth()
                    raise
                if not self.linked():
                    raise ApiError(NOT_LINKED, 401)
                continue
            status = r.status_code if r.status_code in (401, 403, 404, 409, 429) else \
                502 if r.status_code >= 500 else 400
            raise ApiError(f"Twitch {path}: {r.status_code} {msg}", status)

    def me(self):
        if not self.user:
            self.user = self.api("GET", "/users")["data"][0]
        return self.user

    # ----- scopes and the panel's view of the login

    def granted_scopes(self, network=True):
        """Scopes the stored token carries. A token saved before scopes were kept is checked with
        /oauth2/validate at most once an hour; until then it is assumed to have them all."""
        if not self.linked():
            return []
        scope = self.tok.get("scope")
        if isinstance(scope, list):
            return list(scope)
        cached = self.validated
        if network and (not cached or time.time() - cached[0] > VALIDATE_EVERY):
            self.validated = (time.time(), cached[1] if cached else None)   # claim: one call per hour
            try:
                r = requests.get(f"{OAUTH}/validate", timeout=10,
                                 headers={"Authorization": f"OAuth {self.tok.get('access_token', '')}"})
                if r.ok and isinstance(r.json().get("scopes"), list):
                    self.validated = (time.time(), [s for s in r.json()["scopes"] if isinstance(s, str)])
            except (requests.RequestException, ValueError) as e:
                self.log.debug("token validate: %s", type(e).__name__)
            cached = self.validated
        return list(cached[1]) if cached and cached[1] is not None else list(SCOPE_LIST)

    def missing_scopes(self, network=True):
        if not self.linked():
            return []
        have = set(self.granted_scopes(network))
        return [s for s in SCOPE_LIST if s not in have]

    def require(self, scope):
        if not self.linked():
            raise ApiError(NOT_LINKED, 401)
        if scope not in self.granted_scopes():
            raise ApiError(f"Re-link Twitch to allow {scope}", 403)

    def _user_view(self):
        u = self.user if self.linked() else None
        if not u:
            return None
        return {"login": str(u.get("login") or ""), "display_name": str(u.get("display_name") or ""),
                "avatar": _cdn(u.get("profile_image_url"))}

    def _pending_view(self):
        p = self.pending
        if not p or p["expires_at"] <= time.time():
            return None
        return {"user_code": p["user_code"], "verification_uri": p["verification_uri"], "expires_at": p["expires_at"]}

    def auth_state(self):
        linked = self.linked()
        return {"configured": {"channel": bool(self.channel), "client_id": bool(self.client_id)},
                "linked": linked, "user": self._user_view(),
                "scopes": self.granted_scopes() if linked else [],
                "missing_scopes": self.missing_scopes() if linked else [],
                "pending": self._pending_view(), "error": self.auth_error}

    def snapshot(self):
        return {"linked": self.linked(), "user": self._user_view(), "pending": self._pending_view(),
                "missing_scopes": self.missing_scopes(network=False), "live": self.live if self.linked() else None,
                "error": self.auth_error}

    def set_config(self, channel=None, client_id=None):
        """Write twitch.channel / twitch.client_id to config.yaml ("" unsets, None leaves it).
        A token belongs to the client id it was issued for, so changing the client id unlinks.
        Returns {"relink": bool}."""
        from core import configio
        changes = {}
        if channel is not None:
            if not isinstance(channel, str):
                raise ApiError("channel must be text")
            channel = channel.strip().lstrip("#")
            changes["channel"] = _login(channel) if channel else ""
        if client_id is not None:
            if not isinstance(client_id, str):
                raise ApiError("client_id must be text")
            client_id = client_id.strip()
            if client_id and not CLIENT_ID_RE.fullmatch(client_id):
                raise ApiError("that doesn't look like a Twitch Client ID (10–64 letters and numbers)")
            changes["client_id"] = client_id
        path = self.bot.config_path
        base = configio.file_hash(path)
        cfg = configio.load(path)
        section = cfg.get("twitch")
        if not isinstance(section, dict):
            section = cfg["twitch"] = {}
        for k, v in changes.items():
            if v:
                section[k] = v
            else:
                section.pop(k, None)
        configio.save(path, cfg, base)
        # The env var wins over config.yaml, so then the effective client id hasn't changed.
        relink = bool("client_id" in changes and self.client_id and changes["client_id"] != self.client_id
                      and not os.environ.get("TWITCH_CLIENT_ID") and self.linked())
        if relink:
            self.logout()   # revokes with the old client id (best effort) and forgets the token
        return {"relink": relink}

    # ----- channel: title, category, tags

    def channel_info(self):
        return self.api("GET", "/channels", params={"broadcaster_id": self.me()["id"]})["data"][0]

    def _history(self):
        try:
            with open(self.history_file) as f:
                items = json.load(f)
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            self.log.warning("can't read %s (%s)", self.history_file, type(e).__name__)
            return []
        out = []
        for h in items if isinstance(items, list) else []:
            if isinstance(h, dict) and isinstance(h.get("title"), str):
                out.append({"title": h["title"], "game_id": str(h.get("game_id") or ""),
                            "game_name": str(h.get("game_name") or ""),
                            "tags": [t for t in h.get("tags") or [] if isinstance(t, str)]})
        return out

    def channel_state(self):
        info = self.channel_info()
        return {"title": info.get("title") or "", "game_id": info.get("game_id") or "",
                "game_name": info.get("game_name") or "", "tags": list(info.get("tags") or []),
                "history": self._history()}

    def update_channel(self, title, game_id, game_name, tags):
        """Validate and apply a title/category/tags change; remember it and update the overlay."""
        title = _text(title, "title", TITLE_MAX)
        game_id = "" if game_id is None else game_id
        if isinstance(game_id, int) and not isinstance(game_id, bool):
            game_id = str(game_id)
        if not isinstance(game_id, str) or not GAME_ID_RE.fullmatch(game_id.strip()):
            raise ApiError("game_id must be a Twitch category id")
        game_id = game_id.strip()
        game_name = _text(game_name, "game_name", 200, required=False)
        tags = [] if tags is None else tags
        if not isinstance(tags, list):
            raise ApiError("tags must be a list")
        if len(tags) > TAGS_MAX:
            raise ApiError(f"Twitch allows at most {TAGS_MAX} tags")
        bad = [t for t in tags if not isinstance(t, str) or not TAG_RE.fullmatch(t)]
        if bad:
            raise ApiError("tags must be 1–25 letters or numbers each: " + ", ".join(_clean(t, 30) for t in bad[:5]))
        self.require("channel:manage:broadcast")
        self._throttle("channel update", 2)
        self.api("PATCH", "/channels", params={"broadcaster_id": self.me()["id"]},
                 json={"title": title, "game_id": game_id, "tags": tags})
        choice = {"title": title, "game_id": game_id, "game_name": game_name if game_id else "", "tags": tags}
        with self.misc_lock:
            history = [choice] + [h for h in self._history() if h != choice]
            tmp = self.history_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(history[:10], f, indent=1)
            os.replace(tmp, self.history_file)
        overlay = self.bot.get_module("Overlay")
        if overlay:
            overlay.set_stream_info(choice["title"], choice["game_name"])
        return choice

    def stream_info(self):
        """Pop up the title/category/tags dialog and apply the result."""
        self.me()  # also refreshes the access token if needed
        info = self.channel_info()
        # The dialog only gets the short-lived access token; refresh tokens are single-use,
        # so only this process may refresh.
        payload = {"client_id": self.client_id, "access_token": self.tok["access_token"],
                   "title": info["title"], "game_id": info["game_id"], "game_name": info["game_name"],
                   "tags": info.get("tags") or [], "history": self._history()}
        p = subprocess.run([sys.executable, DIALOG], input=json.dumps(payload),
                           capture_output=True, text=True)
        if p.returncode:
            raise RuntimeError(f"dialog failed: {_clean(p.stderr.strip().splitlines()[-1:])}")
        if not p.stdout.strip():
            return  # canceled
        choice = json.loads(p.stdout)
        choice = self.update_channel(choice.get("title"), choice.get("game_id"), choice.get("game_name"),
                                     choice.get("tags"))
        notify(f"Stream updated: {choice['title']} · {choice['game_name'] or 'no category'}")

    def search_categories(self, q):
        q = _text(q, "search", 100, required=False)
        if len(q) < 2:
            return []
        key, now = q.lower(), time.time()
        with self.misc_lock:
            hit = self.categories.get(key)
            if hit and now - hit[0] < CATEGORY_TTL:
                return [dict(c) for c in hit[1]]
        data = self.api("GET", "/search/categories", params={"query": q, "first": 20}).get("data") or []
        out = [{"id": str(c.get("id") or ""), "name": str(c.get("name") or ""), "box_art": _cdn(c.get("box_art_url"))}
               for c in data if isinstance(c, dict)]
        with self.misc_lock:
            if len(self.categories) > 200:
                self.categories = {k: v for k, v in self.categories.items() if now - v[0] < CATEGORY_TTL}
            self.categories[key] = (now, out)
        return [dict(c) for c in out]

    def stream_status(self):
        """{live, viewers, started_at, title, game_name}, cached for 15 s."""
        if self.live is not None and time.time() - self.live_at < STATUS_TTL:
            return dict(self.live)
        data = self.api("GET", "/streams", params={"user_id": self.me()["id"]}).get("data") or []
        s = data[0] if data else {}
        self.live = {"live": bool(data), "viewers": s.get("viewer_count"), "started_at": s.get("started_at"),
                     "title": s.get("title"), "game_name": s.get("game_name")}
        self.live_at = time.time()
        return dict(self.live)

    @task(every=30, on_page_change=False)
    def refresh_status(self):
        """Keeps the panel's live/viewers view fresh, in the background, only while a panel is open."""
        if not (self.linked() and self.bot.hub.subscribers > 0):
            return
        if self.status_thread and self.status_thread.is_alive():
            return

        def work():
            try:
                self.stream_status()
            except Exception as e:
                self.log.debug("stream status: %s", _clean(e))
        self.status_thread = threading.Thread(target=work, daemon=True, name="twitch-status")
        self.status_thread.start()

    # ----- chat

    def _throttle(self, kind, every):
        """Allow one `kind` operation per `every` seconds; call only once validation has passed."""
        with self.misc_lock:
            now = time.monotonic()
            if now - self.last.get(kind, -1e9) < every:
                e = ApiError(f"Slow down: {kind} is limited to one every {every:g}s", 429)
                e.retry_after = every - (now - self.last.get(kind, -1e9))   # a short wait fixes it
                raise e
            self.last[kind] = now

    def _chat_slot(self):
        """Shared budget for everything this app posts in chat (panel, deck keys and the Twitch Bot)."""
        with self.misc_lock:
            now = time.monotonic()
            sent = [t for t in getattr(self, "chat_sent", []) if now - t < CHAT_WINDOW]
            if len(sent) >= CHAT_BURST:
                self.chat_sent = sent
                raise ApiError(f"Slow down: at most {CHAT_BURST} chat messages every {CHAT_WINDOW:g}s", 429)
            self.chat_sent = sent + [now]

    def send_chat(self, text, reply_to=None):
        """Post `text` as the linked account; reply_to: a chat message id to thread the reply under."""
        text = _text(text, "message", CHAT_MAX)
        if reply_to is not None and (not isinstance(reply_to, str) or not MSG_ID_RE.fullmatch(reply_to)):
            raise ApiError("not a chat message id")
        self.require("user:write:chat")
        uid = self.me()["id"]
        self._throttle("chat", CHAT_EVERY)
        self._chat_slot()
        body = {"broadcaster_id": uid, "sender_id": uid, "message": text}
        if reply_to:
            body["reply_parent_message_id"] = reply_to
        r = self.api("POST", "/chat/messages", json=body)
        sent = (r.get("data") or [{}])[0]
        if sent.get("is_sent") is False:
            reason = (sent.get("drop_reason") or {}).get("message") or "dropped"
            raise ApiError(f"Twitch didn't send it: {_clean(reason)}")
        mid = sent.get("message_id")
        return mid if isinstance(mid, str) and MSG_ID_RE.fullmatch(mid) else None

    def delete_message(self, msg_id):
        if not isinstance(msg_id, str) or not MSG_ID_RE.fullmatch(msg_id):
            raise ApiError("not a chat message id")
        self.require("moderator:manage:chat_messages")
        uid = self.me()["id"]
        self._throttle("delete", 0.5)
        self.api("DELETE", "/moderation/chat", params={"broadcaster_id": uid, "moderator_id": uid, "message_id": msg_id})

    def ban(self, user_id, seconds=None, reason="", permanent=False):
        if isinstance(user_id, int) and not isinstance(user_id, bool):
            user_id = str(user_id)
        if not isinstance(user_id, str) or not USER_ID_RE.fullmatch(user_id):
            raise ApiError("not a Twitch user id")
        if not isinstance(permanent, bool):
            raise ApiError("permanent must be true or false")
        reason = _text(reason, "reason", 500, required=False)
        data = {"user_id": user_id, "reason": reason}
        if not permanent:
            if not isinstance(seconds, int) or isinstance(seconds, bool) or not 1 <= seconds <= BAN_MAX:
                raise ApiError(f"a timeout needs seconds between 1 and {BAN_MAX} (or permanent: true)")
            data["duration"] = seconds
        self.require("moderator:manage:banned_users")
        uid = self.me()["id"]
        if user_id == uid:
            raise ApiError("you can't ban yourself")
        self._throttle("ban", 1)
        self.api("POST", "/moderation/bans", params={"broadcaster_id": uid, "moderator_id": uid}, json={"data": data})

    def announce(self, text, color="primary"):
        text = _text(text, "announcement", CHAT_MAX)
        color = "primary" if color is None else color
        if color not in ANNOUNCE_COLORS:
            raise ApiError("color must be one of " + ", ".join(ANNOUNCE_COLORS))
        self.require("moderator:manage:announcements")
        uid = self.me()["id"]
        self._throttle("chat", CHAT_EVERY)
        self._chat_slot()
        self.api("POST", "/chat/announcements", params={"broadcaster_id": uid, "moderator_id": uid},
                 json={"message": text, "color": color})

    def _background(self, kind, work):
        """Run `work` on a thread unless the previous `kind` run is still going (never blocks the caller)."""
        with self.misc_lock:
            t = self.bg_threads.get(kind)
            if t and t.is_alive():
                return

            def run():
                try:
                    work()
                except Exception as e:
                    self.log.debug("%s refresh: %s", kind, _clean(e))
            t = self.bg_threads[kind] = threading.Thread(target=run, daemon=True, name=f"twitch-{kind}")
            t.start()

    def chat_settings_cached(self):
        """The last chat settings (None before the first fetch); a stale cache starts a background refresh."""
        stamp, data = self.chat_cache
        if self.linked() and time.time() - stamp > CHAT_TTL:
            self._background("chat", self.chat_settings)
        return data

    def stream_status_cached(self):
        """The last stream status (None before the first fetch); a stale cache starts a background refresh."""
        live = self.live
        if self.linked() and (live is None or time.time() - self.live_at >= STATUS_TTL):
            self._background("status", self.stream_status)
        return dict(live) if live is not None else None

    def chat_settings(self):
        if time.time() - self.chat_cache[0] > CHAT_TTL:
            uid = self.me()["id"]
            data = self.api("GET", "/chat/settings", params={"broadcaster_id": uid, "moderator_id": uid})["data"][0]
            self.chat_cache = (time.time(), data)
        return self.chat_cache[1]

    def chat_settings_state(self):
        s = self.chat_settings()
        return {"emote": bool(s.get("emote_mode")), "followers": bool(s.get("follower_mode")),
                "slow": bool(s.get("slow_mode")), "slow_seconds": s.get("slow_mode_wait_time"),
                "subs": bool(s.get("subscriber_mode")), "unique": bool(s.get("unique_chat_mode"))}

    def set_chat_mode(self, mode, on, seconds=None):
        """seconds: slow mode's wait (3-120, Twitch's limits); default twitch.slow_mode_seconds."""
        if not isinstance(mode, str) or mode not in CHAT_MODES:
            raise ApiError("mode must be one of " + ", ".join(CHAT_MODES))
        if not isinstance(on, bool):
            raise ApiError("on must be true or false")
        if seconds is not None and (mode != "slow" or isinstance(seconds, bool) or not isinstance(seconds, int)
                                    or not 3 <= seconds <= 120):
            raise ApiError("seconds must be a whole number from 3 to 120 (slow mode only)")
        self.require("moderator:manage:chat_settings")
        field, uid = CHAT_MODES[mode], self.me()["id"]
        body = {field: on}
        if mode == "slow" and on:
            body["slow_mode_wait_time"] = seconds or max(3, min(120, self.slow_seconds))
        r = self.api("PATCH", "/chat/settings", params={"broadcaster_id": uid, "moderator_id": uid}, json=body)
        data = r.get("data") or []
        self.chat_cache = (time.time(), data[0]) if data and isinstance(data[0], dict) else (0, None)

    # ----- other people's channels

    def user_by_login(self, login):
        login = _login(login)
        data = self.api("GET", "/users", params={"login": login}).get("data") or []
        if not data:
            raise ApiError(f"no Twitch user named {login}", 404)
        u = data[0]
        return {"id": str(u.get("id") or ""), "login": str(u.get("login") or ""),
                "display_name": str(u.get("display_name") or "")}

    def raid(self, login):
        login = _login(login)
        self.require("channel:manage:raids")
        target = self.user_by_login(login)
        self._throttle("raid", 5)
        self.api("POST", "/raids", params={"from_broadcaster_id": self.me()["id"], "to_broadcaster_id": target["id"]})

    def cancel_raid(self):
        self.require("channel:manage:raids")
        self.api("DELETE", "/raids", params={"broadcaster_id": self.me()["id"]})

    def shoutout(self, login):
        login = _login(login)
        self.require("moderator:manage:shoutouts")
        target, uid = self.user_by_login(login), self.me()["id"]
        self._throttle("shoutout", 2)
        self.api("POST", "/chat/shoutouts", params={"from_broadcaster_id": uid, "to_broadcaster_id": target["id"],
                                                    "moderator_id": uid})
        self._note_shoutout(target["id"])

    def _note_shoutout(self, user_id):
        """Tell Alerts about a shoutout made by hand, so its raid shoutout keeps Twitch's cooldowns."""
        alerts = self.bot.get_module("Alerts")
        if alerts:
            try:
                alerts.note_shoutout(str(user_id))
            except Exception as e:
                self.log.debug("note shoutout: %s", type(e).__name__)

    def shoutout_id(self, user_id):
        """Shout out a channel by its numeric id (the raid shoutout already has it: no lookup)."""
        if isinstance(user_id, int) and not isinstance(user_id, bool):
            user_id = str(user_id)
        if not isinstance(user_id, str) or not USER_ID_RE.fullmatch(user_id):
            raise ApiError("not a Twitch user id")
        self.require("moderator:manage:shoutouts")
        uid = self.me()["id"]
        if user_id == uid:
            raise ApiError("you can't shout yourself out")
        self._throttle("shoutout", 2)
        self.api("POST", "/chat/shoutouts", params={"from_broadcaster_id": uid, "to_broadcaster_id": user_id,
                                                    "moderator_id": uid})

    # ----- features

    def clip(self):
        self.require("clips:edit")
        self._throttle("clip", 2)
        clip = self.api("POST", "/clips", params={"broadcaster_id": self.me()["id"]})["data"][0]
        self.log.info("clip created: %s", clip["edit_url"])
        notify(f"Clip created: {clip['edit_url']}")
        return clip["edit_url"]

    def marker(self, description="Stream Deck"):
        description = _text(description, "description", 140, required=False)
        self.require("channel:manage:broadcast")
        self._throttle("marker", 2)
        self.api("POST", "/streams/markers", json={"user_id": self.me()["id"], "description": description})
        notify("Stream marker added")

    # ----- actions

    @action("twitch", ui=UI("Twitch", "Twitch", arg=Choice(
        ("clip", "Create a clip"), ("marker", "Add a stream marker"), ("stream_info", "Edit title / category / tags"),
        ("login", "Link Twitch account"))))
    def twitch(self, what):
        """twitch: login | clip | marker | stream_info"""
        {"login": self.login, "clip": self.clip, "marker": self.marker,
         "stream_info": self.stream_info}[what]()

    @action("twitch_ad", ui=UI("Run an ad break", "Twitch", arg=Choice(
        (30, "30 seconds"), (60, "60 seconds"), (90, "90 seconds"), (120, "2 minutes"), (180, "3 minutes"))))
    def ad(self, seconds):
        """twitch_ad: 60  (30–180 seconds)"""
        if isinstance(seconds, str) and seconds.strip().isdigit():
            seconds = int(seconds.strip())   # from YAML
        if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds not in AD_LENGTHS:
            raise ApiError("an ad break is " + ", ".join(map(str, AD_LENGTHS)) + " seconds")
        self.require("channel:edit:commercial")
        self._throttle("ad", 10)
        r = self.api("POST", "/channels/commercial", json={"broadcaster_id": self.me()["id"], "length": seconds})
        length = r["data"][0]["length"]
        notify(f"Running a {length}s ad break")
        return length

    @action("twitch_raid", rewards=False, ui=UI("Raid a channel", "Twitch", scalar="channel", args={
        "channel": Text("Channel", placeholder="their Twitch name"), "cancel": Bool("Cancel the raid instead")}))
    def raid_key(self, arg):
        """twitch_raid: name | {channel: name} | cancel"""
        if arg is True or (isinstance(arg, str) and arg.strip().lower() == "cancel"):
            arg = {"cancel": True}
        elif isinstance(arg, str):
            arg = {"channel": arg}
        if not isinstance(arg, dict):
            raise ApiError("name the channel to raid")
        if arg.get("cancel") is True:
            self.cancel_raid()
            notify("Raid canceled")
            return
        login = _login(arg.get("channel"))
        self.raid(login)
        notify(f"Raiding {login}: Twitch starts it after its countdown; press again with cancel to stop")

    @action("twitch_shoutout", rewards=False, ui=UI("Shout out a channel", "Twitch", arg=Text(
        "Channel", placeholder="their Twitch name, or last_raider")))
    def shoutout_key(self, who):
        """twitch_shoutout: name | last_raider"""
        if isinstance(who, str) and who.strip().lower() == "last_raider":
            alerts = self.bot.get_module("Alerts")
            r = alerts.last_raider() if alerts else None
            if not r:
                raise ApiError("nobody has raided yet")
            self.shoutout_id(r["user_id"])
            self._note_shoutout(r["user_id"])
            notify(f"Shouted out {r['name'] or r['login']}")
            return
        login = _login(who)
        self.shoutout(login)
        notify(f"Shouted out {login}")

    @action("twitch_chat", ui=UI("Chat mode on / off", "Twitch", arg=Choice(
        ("emote", "Emote only"), ("followers", "Followers only"), ("slow", "Slow mode"), ("subs", "Subscribers only"),
        ("unique", "Unique messages"))))
    def toggle_chat(self, mode):
        """twitch_chat: emote | followers | slow | subs | unique"""
        if mode not in CHAT_MODES:
            raise ValueError(f"unknown chat mode {mode!r}")
        self.set_chat_mode(mode, not self.chat_settings()[CHAT_MODES[mode]])

    @action("twitch_open", ui=UI("Open Twitch page", "Twitch", arg=Choice(
        ("chat", "Chat (pop-out)"), ("dashboard", "Stream manager"), ("channel", "Your channel"))))
    def open(self, what):
        """twitch_open: chat | dashboard | channel"""
        login = self.login_name()
        open_url({"chat": f"https://www.twitch.tv/popout/{login}/chat?popout=",
                  "dashboard": f"https://dashboard.twitch.tv/u/{login}/stream-manager",
                  "channel": f"https://www.twitch.tv/{login}"}[what])

    # ----- states & displays

    @state("twitch_chat", ui=UI("Chat mode is on", "Twitch", arg=Choice(
        ("emote", "Emote only"), ("followers", "Followers only"), ("slow", "Slow mode"), ("subs", "Subscribers only"),
        ("unique", "Unique messages"))))
    def chat_state(self, mode, spec):
        return bool((self.chat_settings_cached() or {}).get(CHAT_MODES[mode]))

    @state("twitch", ui=UI("Twitch is linked", "Twitch", arg=Choice(("linked", "linked"))))
    def twitch_state(self, what, spec):
        """state: {twitch: linked}"""
        if what != "linked":
            raise ValueError(f"unknown twitch state {what!r}")
        return self.linked()

    @display("twitch_viewers", ui=UI("Viewer count", "Twitch"))
    def viewers(self, spec):
        live = self.stream_status_cached()
        if not live:
            return {}
        return {"label": f"{live['viewers']}\nviewers", "bg": "#6441a5"} if live["live"] else {}


def setup(bot):
    bot.add_module(Twitch(bot))
