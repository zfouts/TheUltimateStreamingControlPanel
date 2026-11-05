"""Go-live announcements: when Twitch says the stream went online, post to Discord webhooks.

    announce:
      enabled: true          # master switch (false when the section is absent)
      cooldown: 30           # minutes: a new stream.online this soon after the last announcement is ignored
      destinations:          # at most 10
        - id: main           # [a-z0-9][a-z0-9_-]{0,31}, unique
          name: My Discord
          webhook: golive    # the id of a webhook in Settings → Discord (modules/discord.py)
          enabled: true
          message: "{channel} is live! {title}\\n{url}"
          embed: true        # a card with the title, game, link and Twitch's thumbnail
          ping: none         # none | everyone | here | role:<role id>

Webhook URLs are secrets kept by the Discord module (modules/discord.py); a destination only
names one, and its URL is asked for at the moment of posting. Results go back to the Discord
module (report()), which marks a webhook Discord says is gone as broken: its destinations are
skipped until its URL is replaced. A `url` key in a destination is ignored with a warning.

Only EventSub's stream.online (type "live") triggers a post; polling never does. The listener just
queues; one worker thread (announcements only: test posts run on their own short-lived thread,
with the panel's sample values, so they never take a queue slot) fetches the title/game (at most 15 s), checks the dedupe state in
announce_state.json (same stream id, or within the cooldown: skipped), records it BEFORE posting,
then posts to each enabled destination with retries. Placeholder values come from Twitch and are
untrusted: they are cleaned, Discord-escaped and capped, and filled in one pass; mentions are
only allowed for the destination's own `ping` setting.
"""
import copy
import json
import queue
import random
import re
import threading
import time
import unicodedata
from urllib.parse import urlsplit

from core import Module, action, configio, listener, webhooks
from core import text as textclean
from core.paths import state_file
from core.schema import UI, Choice
from core.webapi import ApiError
from core.twitch_ids import LOGIN_RE

STATE_FILE = "announce_state.json"
ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
PING_RE = re.compile(r"none|everyone|here|role:([0-9]{17,20})")
STREAM_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
RENDER_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")       # names outside PLACEHOLDERS stay literal
ESCAPE_RE = re.compile(r"([\\*_~|>#\-\[\]()`@<])")
PLACEHOLDERS = ("channel", "title", "game", "url")
CAPS = {"channel": 25, "title": 140, "game": 100}
DEFAULT_MESSAGE = "{channel} is live! {title}\n{url}"
DEFAULT_COOLDOWN = 30
COOLDOWN_MAX = 1440
MAX_DESTINATIONS = 10
NAME_MAX = 40
MESSAGE_MAX = 1500
CONTENT_MAX = 2000                   # Discord's limit for a message
EMBED_TITLE_MAX = 256                # Discord's limit for an embed title (an escaped title can be longer)
QUEUE_MAX = 4
ATTEMPTS = 3
BACKOFF = (5, 30, 120)               # seconds before attempt 2, 3 (, 4): plus jitter, or Discord's retry_after
GIVE_UP = 600                        # an announcement older than this is not retried
DEST_GAP = 2                         # seconds between two posts to one destination
INFO_TIMEOUT = 15                    # never wait longer than this for the stream title
TEST_COOLDOWN = 10
THUMB_HOST = "static-cdn.jtvnw.net"
TWITCH_PURPLE = 0x9146FF
PICK_WEBHOOK = "Pick a Discord webhook (Settings → Discord)"
SAMPLE = {"channel": "YourChannel", "title": "Building a stream deck from scratch",
          "game": "Software and Game Development", "url": "https://twitch.tv/yourchannel"}


# ----- text

def clean_value(value, cap):
    """A placeholder value from Twitch on one line: ANSI escapes removed, line breaks and tabs become
    spaces, other control and format characters are removed (ZWJ kept for emoji), whitespace
    collapsed, at most `cap` characters."""
    text = "" if value is None else str(value)[:cap * 4]
    return textclean.clean(text, newlines="space", marks=None, collapse=True)[:cap]


def cut(text, n):
    """text at most n characters, never ending in half of an escape (a lone trailing backslash)."""
    text = text[:n]
    trailing = len(text) - len(text.rstrip("\\"))
    return text[:-1] if trailing % 2 else text


def escape(text):
    """Discord markdown and mention syntax made literal: \\ * _ ~ | > # - [ ] ( ) ` @ < get a backslash."""
    return ESCAPE_RE.sub(r"\\\1", text)


def render(template, values):
    """Fill {channel} {title} {game} {url} in one pass: a filled-in value is never scanned again
    and anything else in braces (e.g. {title.__class__}) stays as written. `values` must already
    be cleaned and escaped (see placeholder_values)."""
    def fill(m):
        name = m.group(1)
        return values.get(name, "") if name in PLACEHOLDERS else m.group(0)
    return RENDER_RE.sub(fill, template or "")


def placeholder_values(info):
    """{channel, title, game, url} ready for render(): cleaned, escaped, then capped (an escape is
    never cut in half). `info` is raw:
    {"channel" (display name), "login", "title", "game"}."""
    out = {name: cut(escape(clean_value(info.get(name), cap)), cap) for name, cap in CAPS.items()}
    login = info.get("login")
    login = login.lower() if isinstance(login, str) else ""
    out["url"] = f"https://twitch.tv/{login}" if LOGIN_RE.fullmatch(login) else ""
    return out


def thumbnail(url, buster):
    """Twitch's stream thumbnail at 1280x720 with a cache-buster, or None unless it is https on
    static-cdn.jtvnw.net."""
    if not isinstance(url, str) or len(url) > 500:
        return None
    url = url.replace("{width}", "1280").replace("{height}", "720")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if (parts.scheme != "https" or parts.hostname != THUMB_HOST or parts.netloc != THUMB_HOST or port is not None
            or parts.query or parts.fragment or "{" in url or any(c.isspace() for c in url)):
        return None
    return f"{url}?t={int(buster)}"


def build_payload(dest, info, test=False, buster=0):
    """The Discord JSON for one destination. Mentions: none unless dest's ping says so (never on
    a test post, which shows the ping text but notifies nobody)."""
    values = placeholder_values(info)
    text = render(dest["message"], values)
    ping, allowed = dest["ping"], {"parse": []}
    prefix = ""
    if ping in ("everyone", "here"):
        prefix = f"@{ping} "
        allowed = {"parse": ["everyone"]}
    elif ping.startswith("role:"):
        role = ping.split(":", 1)[1]
        prefix = f"<@&{role}> "
        allowed = {"parse": [], "roles": [role]}
    if test:
        prefix, allowed = "[Test] " + prefix, {"parse": []}
    if not text.strip():      # e.g. "{title}" with no title: Discord refuses an empty message
        text = f"{values['channel'] or 'The stream'} is live! {values['url']}".strip()
    payload = {"content": cut(prefix + text, CONTENT_MAX), "allowed_mentions": allowed}
    if dest["embed"]:
        title = values["title"] or f"{values['channel'] or 'The stream'} is live"
        embed = {"title": cut(title, EMBED_TITLE_MAX), "color": TWITCH_PURPLE}
        if values["url"]:
            embed["url"] = values["url"]
        if values["game"]:
            embed["description"] = values["game"]
        image = thumbnail(info.get("thumbnail"), buster)
        if image:
            embed["image"] = {"url": image}
        payload["embeds"] = [embed]
    return payload


# ----- config

def _text_ok(value, limit, multiline=False):
    if not isinstance(value, str):
        return False
    bad = [c for c in value if unicodedata.category(c)[0] == "C" and not (multiline and c in "\n\t")]
    return 0 < len(value) <= limit and not bad


def validate(section):
    """(normalised announce section, [errors]). A bad field gets its default (and an error); a
    destination without a usable id is left out. `webhook` is a webhook id (whether it exists is
    checked by Announce.save against the Discord module); a url key is always an error (and
    ignored): webhook URLs are never kept in config.yaml. Messages never echo a value."""
    errors = []
    out = {"enabled": False, "cooldown": DEFAULT_COOLDOWN, "destinations": []}
    if section is None:
        return out, errors
    if not isinstance(section, dict):
        return out, ["announce must be a mapping"]
    enabled = section.get("enabled", False)
    if isinstance(enabled, bool):
        out["enabled"] = enabled
    else:
        errors.append("announce.enabled must be true or false")
    cooldown = section.get("cooldown", DEFAULT_COOLDOWN)
    if isinstance(cooldown, float) and cooldown.is_integer():
        cooldown = int(cooldown)
    if isinstance(cooldown, int) and not isinstance(cooldown, bool) and 0 <= cooldown <= COOLDOWN_MAX:
        out["cooldown"] = cooldown
    else:
        errors.append(f"announce.cooldown must be a whole number of minutes from 0 to {COOLDOWN_MAX}")
    items = section.get("destinations")
    if items is None:
        items = []
    if not isinstance(items, list):
        errors.append("announce.destinations must be a list")
        items = []
    if len(items) > MAX_DESTINATIONS:
        errors.append(f"announce.destinations: at most {MAX_DESTINATIONS} (the rest are ignored)")
        items = items[:MAX_DESTINATIONS]
    ids = set()
    for i, item in enumerate(items):
        where = f"destinations[{i}]"
        if not isinstance(item, dict):
            errors.append(f"{where} must be a mapping")
            continue
        did = item.get("id")
        if not isinstance(did, str) or not ID_RE.fullmatch(did):
            errors.append(f"{where}: id must be 1-32 lowercase letters, digits, _ or -, starting with a "
                          "letter or digit")
            continue
        where = f"destinations[{i}] ({did})"
        if did in ids:
            errors.append(f"{where}: another destination already has this id")
            continue
        ids.add(did)
        for key in ("url", "webhook_url"):
            if key in item:
                errors.append(f"{where}: {key} is ignored here; webhook URLs are saved in Settings → Discord, "
                              "never in config.yaml")
        dest = {"id": did, "name": did, "webhook": "", "enabled": True, "message": DEFAULT_MESSAGE,
                "embed": True, "ping": "none"}
        hook = item.get("webhook")
        if hook is None or hook == "" or (isinstance(hook, str) and ID_RE.fullmatch(hook)):
            dest["webhook"] = hook or ""
        else:
            errors.append(f"{where}: webhook must be the id of a webhook from Settings → Discord "
                          "(never its URL); ignored")
        name = item.get("name", did)
        if isinstance(name, str) and _text_ok(name.strip(), NAME_MAX):
            dest["name"] = name.strip()
        else:
            errors.append(f"{where}: name must be 1-{NAME_MAX} characters on one line")
        for key in ("enabled", "embed"):
            v = item.get(key, dest[key])
            if isinstance(v, bool):
                dest[key] = v
            else:
                errors.append(f"{where}: {key} must be true or false")
        message = item.get("message", DEFAULT_MESSAGE)
        if isinstance(message, str) and _text_ok(message.strip(), MESSAGE_MAX, multiline=True):
            dest["message"] = message.strip()
        else:
            errors.append(f"{where}: message is required, at most {MESSAGE_MAX} characters, "
                          "without control characters")
        ping = item.get("ping", "none")
        if isinstance(ping, str) and PING_RE.fullmatch(ping):
            dest["ping"] = ping
        else:
            errors.append(f"{where}: ping must be none, everyone, here or role:<role id>")
        out["destinations"].append(dest)
    return out, errors


class _Job:
    def __init__(self, kind, created, stream_id=None):
        self.kind = kind              # live | manual
        self.created = created        # wall clock
        self.stream_id = stream_id


class Announce(Module):
    config_key = "announce"

    def __init__(self, bot):
        super().__init__(bot)
        # Injectable (tests): clocks, the HTTP post, randomness, and the files.
        self.wall, self.mono = time.time, time.monotonic
        self.http_post = None                 # None: requests.post
        self.sleep = None                     # (seconds) -> True if stopping; None: self.stopping.wait
        self.random = random.random
        self.info_timeout = INFO_TIMEOUT
        self.state_path = state_file(STATE_FILE)
        # Lock order: never call the Discord module while holding self.lock (it calls discord_usage()).
        self.lock = threading.RLock()         # cfg, results, testing, state
        self.save_lock = threading.Lock()     # one panel save at a time
        self.cfg, _ = validate(None)
        self.jobs = queue.Queue(maxsize=QUEUE_MAX)
        self.stopping = threading.Event()
        self.worker = None
        self.results = {}                     # dest id -> {"state", "at", "detail"}
        self.sending = set()
        self.testing = set()                  # dest ids with a test post queued or running
        self.test_last = {}                   # dest id -> monotonic time of the last test request
        self.last_post = {}                   # webhook id -> monotonic time of the last post
        self.info_cache = None                # the last Twitch info fetched (for the panel's sample)
        self.state = self._load_state()

    # ----- lifecycle

    def on_config(self, config):
        super().on_config(config)
        cfg, errors = validate(config)
        for e in errors:
            self.log.warning("announce: %s", e)
        with self.lock:
            self.cfg = cfg
            ids = {d["id"] for d in cfg["destinations"]}
            for did in [d for d in self.results if d not in ids]:
                self.results.pop(did, None)

    def on_load(self):
        webhooks.install_log_filter()
        self.stopping.clear()
        self.worker = threading.Thread(target=self._run, daemon=True, name="announce")
        self.worker.start()

    def on_unload(self):
        self.stopping.set()
        try:
            self.jobs.put_nowait(None)
        except queue.Full:
            pass
        worker = self.worker
        if worker and worker is not threading.current_thread():
            worker.join(timeout=2)

    # ----- state (announce_state.json)

    def _load_state(self):
        state = {"last_stream_id": None, "last_at": None, "last_kind": None}
        try:
            with open(self.state_path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return state
        except (OSError, ValueError) as e:
            self.log.warning("announce: can't read %s (%s); starting fresh", STATE_FILE, type(e).__name__)
            return state
        if not isinstance(data, dict):
            return state
        sid = data.get("last_stream_id")
        state["last_stream_id"] = sid if isinstance(sid, str) and STREAM_ID_RE.fullmatch(sid) else None
        at = data.get("last_at")
        state["last_at"] = float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else None
        kind = data.get("last_kind")
        state["last_kind"] = kind if kind in ("live", "manual") else None
        return state

    def _save_state(self):
        """Call with self.lock held. A failed write is logged; the in-memory state still applies."""
        try:
            configio.write_atomic(self.state_path, json.dumps(self.state, indent=1, sort_keys=True).encode("utf-8"))
        except OSError as e:
            self.log.warning("announce: can't save %s (%s)", STATE_FILE, type(e).__name__)

    # ----- webhooks (through the Discord module; never with self.lock held)

    def _discord(self):
        return self.bot.get_module("Discord")

    def _route(self, dest, discord=None):
        """(url, None, None) when dest can be posted to now, else (None, state, detail) saying why:
        "error" for no/unknown webhook or no URL, "broken" for a webhook Discord said is gone."""
        discord = discord or self._discord()
        if discord is None:
            return None, "error", "The Discord module isn't loaded."
        wid = dest["webhook"]
        name = discord.name_of(wid) if wid else None
        if name is None:
            return None, "error", PICK_WEBHOOK
        if discord.is_broken(wid):
            return None, "broken", f"The {name} webhook is broken; replace it in Settings → Discord"
        url = discord.webhook_url(wid)
        if not url:
            return None, "error", f"The {name} webhook has no URL yet; add it in Settings → Discord"
        return url, None, None

    # ----- triggers

    @listener("stream_online")
    def on_stream_online(self, event=None, *_):
        """On EventSub's thread: only queue (never blocks)."""
        event = event if isinstance(event, dict) else {}
        sid, kind = event.get("stream_id"), event.get("type")
        sid = sid if isinstance(sid, str) and STREAM_ID_RE.fullmatch(sid) else None
        if kind != "live":
            self.log.info("announce: stream online (type %s), not announced", webhooks.clean(kind, 20) or "unknown")
            return
        self._enqueue(_Job("live", self.wall(), stream_id=sid))

    def _enqueue(self, job):
        try:
            self.jobs.put_nowait(job)
            return True
        except queue.Full:
            self.log.warning("announce: too many announcements waiting; dropped one (%s)", job.kind)
            return False

    def _ready_destinations(self, cfg, log=False):
        """The enabled destinations whose webhook can be posted to now; with log, says why the
        others are skipped."""
        discord = self._discord()
        ready = []
        for d in cfg["destinations"]:
            if not d["enabled"]:
                continue
            url, _state, detail = self._route(d, discord)
            if url:
                ready.append(d)
            elif log:
                self.log.info("announce: %s skipped: %s", d["name"], detail)
        return ready

    def announce_now(self):
        """Queue an announcement now (panel or deck key): ignores the dedupe and cooldown."""
        with self.lock:
            cfg = self.cfg
        if not cfg["enabled"]:
            raise ApiError("Announcements are off; turn them on first.", 409)
        if not self._ready_destinations(cfg):
            raise ApiError("No enabled destination has a working Discord webhook yet.", 409)
        if not self._enqueue(_Job("manual", self.wall())):
            raise ApiError("Busy sending announcements; try again in a minute.", 503)
        return {"queued": True}

    def test(self, dest_id):
        """Post a test to one destination (enabled or not) on a short-lived thread of its own, never
        through the announcement queue, so a test can't fill or delay it. One at a time per
        destination, and at most one every TEST_COOLDOWN seconds."""
        if not isinstance(dest_id, str) or not ID_RE.fullmatch(dest_id):
            raise ApiError("id must be a destination id")
        with self.lock:
            dest = next((d for d in self.cfg["destinations"] if d["id"] == dest_id), None)
        if dest is None:
            raise ApiError("no destination with that id (save it first)", 404)
        url, _state, detail = self._route(dest)
        if not url:
            raise ApiError(detail, 409)
        with self.lock:
            now = self.mono()
            last = self.test_last.get(dest_id)
            if dest_id in self.testing or (last is not None and now - last < TEST_COOLDOWN):
                raise ApiError("Wait a few seconds before testing again.", 429)
            self.testing.add(dest_id)
            self.test_last[dest_id] = now
        try:
            threading.Thread(target=self._run_test, args=(dest_id,), daemon=True, name="announce-test").start()
        except RuntimeError:
            with self.lock:
                self.testing.discard(dest_id)
            raise ApiError("Busy; try again in a minute.", 503) from None
        return {"queued": True}

    def _run_test(self, dest_id):
        """One test post, no retries; with the sample values (no Twitch call). The destination and
        its webhook are looked up again: either may have changed since test() accepted it."""
        try:
            with self.lock:
                dest = next((copy.deepcopy(d) for d in self.cfg["destinations"] if d["id"] == dest_id), None)
            if dest is None or self.stopping.is_set():
                return
            discord = self._discord()
            url, state, detail = self._route(dest, discord)
            if not url:
                self._set_result(dest_id, state, detail)
                return
            payload = build_payload(dest, self._test_info(), test=True, buster=int(self.wall()))
            self.last_post[dest["webhook"]] = self.mono()
            result = webhooks.post(url, payload, http=self.http_post)
            self._finish(discord, dest, result, url, "test post sent", "Test post sent.")
        except Exception as e:      # never log.exception: a traceback could carry a webhook URL
            self.log.error("announce: test failed (%s)", webhooks.clean(f"{type(e).__name__}: {e}", 200))
        finally:
            with self.lock:
                self.testing.discard(dest_id)

    def _test_info(self):
        """Raw info for a test post: the panel's sample() values (cached Twitch values or examples;
        never a network call)."""
        values = self.sample()
        login = values["url"].rsplit("/", 1)[-1]
        return {"channel": values["channel"], "login": login, "title": values["title"],
                "game": values["game"], "thumbnail": None}

    @action("announce_now", ui=UI("Announce going live", "Twitch", arg=Choice(("now", "now"))), rewards=False)
    def announce_key(self, arg=None):
        """announce_now: now"""
        try:
            self.announce_now()
        except ApiError as e:
            raise RuntimeError(str(e)) from None

    # ----- the worker

    def _run(self):
        while not self.stopping.is_set():
            try:
                job = self.jobs.get(timeout=1)
            except queue.Empty:
                continue
            try:
                if job is not None and not self.stopping.is_set():
                    self._handle(job)
            except Exception as e:      # never log.exception: a traceback could carry a webhook URL
                self.log.error("announce: %s failed (%s)", job.kind, webhooks.clean(f"{type(e).__name__}: {e}", 200))
            finally:
                self.jobs.task_done()

    def _handle(self, job):
        with self.lock:
            cfg = copy.deepcopy(self.cfg)
        if job.kind == "live":
            if not cfg["enabled"]:
                self.log.info("announce: stream online, but announcements are off")
                return
            with self.lock:
                last_id, last_at = self.state["last_stream_id"], self.state["last_at"]
                now = self.wall()
                if job.stream_id and job.stream_id == last_id:
                    self.log.info("announce: stream %s was already announced; skipped", job.stream_id)
                    return
                # abs(): a clock that went back (last_at in the future) is within the cooldown too,
                # but a last_at saved by a clock far ahead can't block announcements for longer
                if last_at is not None and abs(now - last_at) < cfg["cooldown"] * 60:
                    self.log.info("announce: last announcement was %d min ago (cooldown %d min); skipped",
                                  int(abs(now - last_at) // 60), cfg["cooldown"])
                    return
        dests = self._ready_destinations(cfg, log=True)
        if not dests:
            self.log.info("announce: no enabled destination with a working webhook; nothing posted")
            return
        with self.lock:   # recorded before posting: a crash mid-post must never post twice
            self.state.update(last_at=self.wall(), last_kind=job.kind)
            if job.kind == "live":
                self.state["last_stream_id"] = job.stream_id
            self._save_state()
        info = self._info()
        self._deliver(dests, info, job)

    def _info(self):
        """Raw {channel, login, title, game, thumbnail} from Twitch, in at most info_timeout
        seconds; title/game "" when it can't be had."""
        tw = self.bot.get_module("Twitch")
        box = []          # the fetch thread's one result; it may finish (and be ignored) after the timeout

        def work():
            box.append(self._fetch(tw))
        t = threading.Thread(target=work, daemon=True, name="announce-info")
        t.start()
        t.join(self.info_timeout)
        result = box[0] if box else {}
        if not box:
            self.log.info("announce: Twitch didn't answer in %d s; posting without the title", self.info_timeout)
        login = getattr(tw, "channel", "") or "" if tw else ""    # configured; no network
        info = {"channel": login, "login": login, "title": "", "game": "", "thumbnail": None}
        info.update(result)
        if info["channel"] or info["login"]:
            self.info_cache = {k: info[k] for k in ("channel", "login", "title", "game")}
        return info

    def _fetch(self, tw):
        out = {}
        if tw is None:
            return out
        try:
            if not tw.linked():
                return out
            me = tw.me()
            if me.get("login"):
                out.update(login=str(me["login"]), channel=str(me.get("display_name") or me["login"]))
            data = (tw.api("GET", "/streams", params={"user_id": me["id"]}) or {}).get("data") or []
            if data and isinstance(data[0], dict):
                s = data[0]
                out.update(title=s.get("title") or "", game=s.get("game_name") or "", thumbnail=s.get("thumbnail_url"))
                return out
            info = tw.channel_info() or {}
            out.update(title=info.get("title") or "", game=info.get("game_name") or "")
        except Exception as e:
            self.log.info("announce: couldn't read the stream title (%s)", webhooks.clean(f"{type(e).__name__}: {e}"))
        return out

    def _set_result(self, did, state, detail=None):
        with self.lock:
            self.results[did] = {"state": state, "at": self.wall(), "detail": detail}

    def _deliver(self, dests, info, job):
        """Post to each destination; retries on the schedule, never past GIVE_UP. The URL is asked
        of the Discord module before every attempt (it may have been replaced, removed or marked
        broken meanwhile), and each final result is reported back to it. Posts to one webhook are
        DEST_GAP apart even when several destinations share it."""
        discord = self._discord()
        buster = int(self.wall())
        pending = {}                          # dest id -> [due (monotonic), attempt, dest, payload]
        start = self.mono()
        for d in dests:
            pending[d["id"]] = [start, 1, d, build_payload(d, info, buster=buster)]
            with self.lock:
                self.sending.add(d["id"])
        try:
            while pending and not self.stopping.is_set():
                did, (due, attempt, dest, payload) = min(pending.items(), key=lambda kv: kv[1][0])
                wid = dest["webhook"]
                last = self.last_post.get(wid)
                if last is not None:
                    due = max(due, last + DEST_GAP)
                wait = due - self.mono()
                if wait > 0 and (self.sleep or self.stopping.wait)(wait):
                    break
                url, state, detail = self._route(dest, discord)
                if not url:
                    del pending[did]
                    self._set_result(did, state, detail)
                    self.log.warning("announce: %s skipped: %s", dest["name"], detail)
                    continue
                self.last_post[wid] = self.mono()
                result = webhooks.post(url, payload, http=self.http_post)
                if result.kind == "retry" and attempt < ATTEMPTS \
                        and self.wall() - job.created < GIVE_UP:
                    delay = result.after if result.after is not None else \
                        BACKOFF[attempt - 1] * (1 + 0.2 * self.random())
                    pending[did] = [self.mono() + max(delay, DEST_GAP), attempt + 1, dest, payload]
                    self.log.info("announce: %s: %s; retrying in %d s", dest["name"], result.reason, int(delay))
                    continue
                del pending[did]
                self._finish(discord, dest, result, url,
                             "announced going live" if job.kind == "live" else "announcement sent")
        finally:
            with self.lock:
                for d in dests:
                    self.sending.discard(d["id"])
                for did in pending:
                    self.results[did] = {"state": "error", "at": self.wall(),
                                         "detail": "Stopped before the post went out."}

    def _finish(self, discord, dest, result, url, done, ok_detail=None):
        """A post's final result: reported to the Discord module, shown on the destination, logged."""
        self._report(discord, dest["webhook"], result, url)
        if result.kind == "ok":
            self._set_result(dest["id"], "ok", ok_detail)
            self.log.info("announce: %s to %s", done, dest["name"])
        elif result.kind == "broken":
            self._set_result(dest["id"], "broken", result.reason)
            self.log.warning("announce: %s: %s", dest["name"], result.reason)
        else:
            self._set_result(dest["id"], "error", result.reason)
            self.log.warning("announce: posting to %s failed: %s", dest["name"], result.reason)

    def _report(self, discord, wid, result, url):
        """Tell the Discord module how a post to its webhook went (it keeps "broken")."""
        if discord is None:
            return
        try:
            discord.report(wid, result, url=url)
        except Exception as e:      # never log.exception: a traceback could carry a webhook URL
            self.log.warning("announce: couldn't report to the Discord module (%s)",
                             webhooks.clean(f"{type(e).__name__}: {e}", 200))

    # ----- panel

    def readiness(self):
        tw = self.bot.get_module("Twitch")
        try:
            twitch = bool(tw and tw.linked())
        except Exception:
            twitch = False
        es = self.bot.get_module("EventSub")
        snap = {}
        if es:
            try:
                snap = es.snapshot() or {}
            except Exception:
                snap = {}
        eventsub = bool(snap.get("connected")) and "stream.online" in (snap.get("subscriptions") or [])
        detail = None
        if not twitch:
            detail = "Twitch isn't linked, so going live won't be detected. 'Announce now' still works."
        elif not es:
            detail = "The EventSub module isn't loaded, so going live won't be detected. 'Announce now' still works."
        elif not eventsub:
            running = (getattr(es, "config", None) or {}).get("enabled") is True
            detail = ("Going live is only detected while the Twitch Bot is on (it runs the EventSub connection). "
                      "'Announce now' still works.") if not running else \
                ("Not connected to Twitch EventSub yet, so going live won't be detected. 'Announce now' still works.")
        return {"eventsub": eventsub, "twitch": twitch, "detail": detail}

    def snapshot(self):
        with self.lock:
            cfg = copy.deepcopy(self.cfg)
            sending, results = self.sending | self.testing, copy.deepcopy(self.results)
            last = {"at": self.state["last_at"], "stream_id": self.state["last_stream_id"],
                    "kind": self.state["last_kind"]}
        discord = self._discord()
        dests = {}
        for d in cfg["destinations"]:
            did = d["id"]
            url, state, detail = self._route(d, discord)
            if did in sending:
                dests[did] = {"state": "sending", "at": None, "detail": None}
            elif not d["enabled"] and not d["webhook"]:     # off and nothing picked: nothing to say
                dests[did] = results.get(did) or {"state": "never", "at": None, "detail": None}
            elif not url:
                at = results[did]["at"] if results.get(did, {}).get("state") == state else None
                dests[did] = {"state": state, "at": at, "detail": detail}
            elif did in results and results[did]["state"] != "broken":     # broken: its URL was replaced since
                dests[did] = results[did]
            else:
                dests[did] = {"state": "never", "at": None, "detail": None}
        return {"enabled": cfg["enabled"], "ready": self.readiness(), "last": last, "destinations": dests}

    def config_view(self):
        with self.lock:
            return copy.deepcopy(self.cfg)

    def webhooks_view(self):
        """[{"id", "name", "set"}]: the Discord webhooks a destination can pick; never a URL."""
        discord = self._discord()
        if discord is None:
            return []
        return [{"id": w["id"], "name": w["name"], "set": bool(w["set"])} for w in discord.webhooks()]

    def discord_usage(self):
        """[(webhook id, label)] for the Discord module's "Used by": every destination's webhook."""
        with self.lock:
            return [(d["webhook"], f"Announcements: {d['name']}") for d in self.cfg["destinations"] if d["webhook"]]

    def sample(self):
        """Placeholder values for the panel's preview: the cached Twitch values (no network), else examples."""
        tw = self.bot.get_module("Twitch")
        raw = dict(self.info_cache or {})
        try:
            user = getattr(tw, "user", None) if tw and tw.linked() else None
            if isinstance(user, dict):
                raw.setdefault("channel", user.get("display_name") or "")
                raw.setdefault("login", user.get("login") or "")
            live = getattr(tw, "live", None) if tw else None
            if isinstance(live, dict) and live.get("live"):
                raw.setdefault("title", live.get("title") or "")
                raw.setdefault("game", live.get("game_name") or "")
        except Exception:
            pass
        out = dict(SAMPLE)
        for name, cap in CAPS.items():
            value = clean_value(raw.get(name), cap)
            if value:
                out[name] = value
        login = raw.get("login")
        if isinstance(login, str) and LOGIN_RE.fullmatch(login.lower()):
            out["url"] = f"https://twitch.tv/{login.lower()}"
        return out

    def save(self, section, base_hash):
        """The panel's save: validate everything (400 listing the problems; every enabled
        destination must pick a webhook that exists in Settings → Discord, a disabled one may
        pick none), then save config.yaml against
        base_hash (409 if it changed). Webhook URLs are never part of this: see modules/discord.py."""
        if not isinstance(section, dict):
            raise ApiError("config must be an object")
        if not isinstance(base_hash, str):
            raise ApiError("hash must be text")
        normalised, errors = validate(section)
        known = {w["id"] for w in self.webhooks_view()}
        for d in normalised["destinations"]:
            if not d["webhook"]:
                if d["enabled"]:
                    errors.append(f"destinations ({d['id']}): pick a Discord webhook, or turn the destination off")
            elif d["webhook"] not in known:
                errors.append(f"destinations ({d['id']}): webhook {d['webhook']} isn't one of the Discord webhooks "
                              "(Settings → Discord)")
        if errors:
            shown = errors[:20] + ([f"…and {len(errors) - 20} more"] if len(errors) > 20 else [])
            e = ApiError("Fix these first: " + "; ".join(shown), 400)
            e.errors = errors
            raise e
        with self.save_lock:
            self._save_section(normalised, base_hash)
            with self.lock:
                before = {d["id"]: d["webhook"] for d in self.cfg["destinations"]}
                after = {d["id"]: d["webhook"] for d in normalised["destinations"]}
                for did in [d for d in self.results if before.get(d) != after.get(d)]:
                    self.results.pop(did, None)
        self.on_config(normalised)
        return normalised

    def _save_section(self, normalised, base_hash):
        path = self.bot.config_path
        try:
            cfg = configio.load(path)
        except FileNotFoundError:
            raise ApiError("there's no config.yaml yet; set up the deck first", 409) from None
        if not isinstance(cfg, dict):
            raise ApiError("config.yaml isn't a mapping; fix it in the Config view first")
        cfg["announce"] = copy.deepcopy(normalised)
        try:
            configio.save(path, cfg, base_hash)
        except configio.Conflict:
            raise ApiError("config.yaml changed since the panel loaded it; reload and try again", 409) from None


def setup(bot):
    bot.add_module(Announce(bot))
