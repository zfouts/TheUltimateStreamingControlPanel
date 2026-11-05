"""The Twitch Bot's configuration, without the runtime (modules/twitch_bot.py): constants, the
viewer-text cleaning and one-pass template render, reward action allow-listing and validate(),
which checks and normalises the twitch_bot section. Pure: no threads, network or module state.

The leading underscore keeps core/bot.py's loader from treating this as a module (it has no setup).
Hot-reload only watches modules, so an edit to this file takes effect after a daemon restart (or
an edit to twitch_bot.py). Don't importlib.reload() it from twitch_bot: alerts.py holds its own
references, and the two halves would then use different versions of it.
"""
import copy
import datetime
import re
import unicodedata

from core import chatcmd
from core import text as textclean
from core.module import REWARD_DENIED
from core.webapi import ApiError
from core.twitch_ids import LOGIN_RE

CHAT_MAX = 500
VALUE_MAX = 100
ERROR_MAX = 200
ID_RE = re.compile(r"[a-z0-9_-]{1,32}")
TRIGGER_RE = re.compile(r"![a-z0-9_]{1,25}")
TWITCH_ID_RE = re.compile(r"[A-Za-z0-9-]{1,64}")
PLACEHOLDER_RE = re.compile(r"\{(user|channel|uptime|game|title|count|color|messages|rank|stream_messages|"
                            r"stream_rank|first_seen|redemptions|commands|list|period)\}")
RENDER_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")   # any name: only those with a value are filled
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]?")
CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f  ‪-‮⁦-⁩]")
LEAD_RE = re.compile(r"^[\s/.!\\]+")
OUT_LEAD_RE = re.compile(r"^[\s!/]+")
LONG_RE = re.compile(r"[A-Za-z0-9]{20,}")   # token-shaped: never in logs, events or errors

TIMER_PH = frozenset(("channel", "uptime", "game", "title", "count"))
COMMAND_PH = TIMER_PH | {"user"}
REPLY_PH = COMMAND_PH | {"color"}
BUILTINS = ("stats", "top10", "uptime", "commands")
# placeholders per command kind: (response, the extra response field and its placeholders)
KIND_PH = {
    None: (COMMAND_PH, None, None),
    "stats": (COMMAND_PH | {"messages", "rank", "stream_messages", "stream_rank", "first_seen", "redemptions",
                            "commands"}, "missing_response", frozenset(("user", "channel"))),
    "top10": (frozenset(("channel", "list", "period")), None, None),
    "uptime": (frozenset(("channel", "uptime", "game", "title")), "offline_response",
               frozenset(("channel", "game", "title"))),
    "commands": (frozenset(("channel", "list", "user")), None, None),
}
EXTRA_DEFAULTS = {"missing_response": "{user}, no stats yet. Say hi!",
                  "offline_response": "{channel} is offline right now."}
ALL_PH = REPLY_PH.union(*(v[0] for v in KIND_PH.values()))
TRUSTED_VALUES = frozenset(("list",))    # built by the bot from stored names / triggers: may start with !
DEFAULT_EXCLUDE = ("nightbot", "streamelements", "streamlabs", "moobot", "fossabot", "soundalerts", "sery_bot",
                   "wizebot", "kofistreambot", "botrixoficial", "commanderroot")
MAX_EXCLUDE = 200
CONFIG_ONLY = ("database_url", "database_password_file")   # never shown in or taken from the panel
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
ANNOUNCE_COLORS = ("primary", "blue", "green", "orange", "purple")
LEVELS = ("everyone", "subscriber", "vip", "moderator", "broadcaster")
SAFE_ACTIONS = frozenset(("obs_scene", "obs_color", "obs_mute", "ha_light", "notify", "countdown", "delay",
                          "radio", "media", "page"))
COLOR_ACTIONS = ("obs_color", "ha_light")
INPUT = "{input}"

MAX_TIMERS, MAX_COMMANDS, MAX_REWARDS = 50, 200, 50
MAX_MESSAGES, MAX_ALIASES, MAX_ACTIONS, MAX_NESTING = 20, 5, 20, 4
DELAY_MAX = 30
DELAY_TOTAL_MAX = 25          # a list's delays together must stay well inside the deadline
ACTION_DEADLINE = 30          # seconds a redemption's actions may take before it is refunded

class Skipped(Exception):
    """A chat post that was deliberately not sent (rate limit, duplicate text)."""


def _strip_format(text):
    """Unicode format characters (zero-width, bidi, BOM...) removed; ZWJ kept for emoji. (No longer
    used by the cleaners below, which go through core.text; modules/twitch_bot.py still imports it.)"""
    return "".join(c for c in text if c == "\u200d" or unicodedata.category(c) != "Cf")


def _one_line(text):
    """core.text.clean with this module's options: ANSI, control, format, surrogate and line-separator
    characters removed (CR/LF/tab deleted, not turned into spaces), ZWJ kept, nothing collapsed."""
    return textclean.clean(text, newlines="drop", marks=None, collapse=False)


_TAIL_RUN_RE = re.compile(r"[A-Za-z0-9]+$")


def _clean(text, n=ERROR_MAX):
    """Text that is safe to log or show: control and format characters and token-shaped runs
    removed, truncated (after the redaction, so a token is never cut down to an unredacted part)."""
    raw = str(text if text is not None else "")
    chunk = raw[:n * 4]
    if len(raw) > len(chunk) and raw[len(chunk)].isalnum():
        chunk = _TAIL_RUN_RE.sub("", chunk)   # the pre-cut split a run: never show part of it
    # Redact before cleaning as well as after: removing characters (an escape, zero-width
    # spaces) can shorten a token or pull the cut part of one into view below LONG_RE's length.
    text = _one_line(LONG_RE.sub("[…]", chunk))
    return LONG_RE.sub("[…]", text)[:n]


def clean_value(value, cap=VALUE_MAX, lead=True):
    """A placeholder value: control characters, CR/LF and ANSI removed, leading / . ! \\ (and
    spaces) stripped so it can't make the message a chat command, capped at 100 chars."""
    text = _one_line(("" if value is None else str(value))[:cap * 10])
    return (LEAD_RE.sub("", text) if lead else text)[:cap]


def render(template, values, limit=CHAT_MAX):
    """Fill the placeholders ({name}, for the names in `values`) in one pass: substituted values are never scanned
    again, unknown {x} (and placeholders without a value) stay literal. The result never starts
    with ! or / and is at most `limit` chars (None: untruncated, to measure what fits)."""
    def fill(m):
        name = m.group(1)
        if name not in values:
            return m.group(0)
        if name in TRUSTED_VALUES:   # measuring (limit None): never cut, so a list can't end mid-entry
            cap = CHAT_MAX if limit is not None else max(CHAT_MAX, len(str(values[name])))
            return clean_value(values[name], cap=cap, lead=False)
        return clean_value(values[name])
    out = OUT_LEAD_RE.sub("", RENDER_RE.sub(fill, template or ""))
    return out if limit is None else out[:limit]


def render_list(template, values, entries, sep, empty=""):
    """render() with {list} = as many whole leading entries as fit the final 500-char message
    (trailing entries are dropped, never cut mid-entry); `empty` when there are none."""
    if not entries:
        return render(template, {**values, "list": empty})
    for k in range(len(entries), 0, -1):
        out = render(template, {**values, "list": sep.join(entries[:k])}, limit=None)
        if len(out) <= CHAT_MAX:
            return out
    return render(template, {**values, "list": ""})


# ----- validation (shared with core/webapi/bot_api.py)

class _Fields:
    """Reads one config item, collecting readable errors ("timers[0] (discord): ...")."""

    def __init__(self, item, where, errors):
        self.item, self.where, self.errors, self.ok = item, where, errors, True

    def bad(self, msg):
        self.errors.append(f"{self.where}: {msg}")
        self.ok = False

    def get(self, key, default):
        v = self.item.get(key, default)
        return default if v is None else v

    def bool(self, key, default):
        v = self.get(key, default)
        if not isinstance(v, bool):
            self.bad(f"{key} must be true or false")
            return default
        return v

    def int(self, key, default, lo, hi):
        v = self.get(key, default)
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            self.bad(f"{key} must be a whole number from {lo} to {hi}")
            return default
        return v

    def text(self, key, default, limit, required=False):
        v = self.get(key, default)
        if not isinstance(v, str):
            self.bad(f"{key} must be text")
            return default
        v = v.strip()
        if required and not v:
            self.bad(f"{key} can't be empty")
        elif len(v) > limit:
            self.bad(f"{key} is longer than {limit} characters")
        elif CTRL_RE.search(v):
            self.bad(f"{key} must be one line without control characters")
        return v

    def choice(self, key, default, options):
        v = self.get(key, default)
        if v not in options:
            self.bad(f"{key} must be one of " + ", ".join(options))
            return default
        return v

    def chat(self, key, value, allowed, required=True):
        """A chat template: one line, <=500 chars, not a chat command, only `allowed` placeholders."""
        if value is None:
            value = ""
        if not isinstance(value, str):
            self.bad(f"{key} must be text")
            return ""
        value = value.strip()
        if not value:
            if required:
                self.bad(f"{key} can't be empty")
            return ""
        if len(value) > CHAT_MAX:
            self.bad(f"{key} is longer than {CHAT_MAX} characters")
        if CTRL_RE.search(value):
            self.bad(f"{key} must be one line without control characters")
        if value[0] in "!/":
            self.bad(f"{key} can't start with ! or / (chat would treat it as a command)")
        if INPUT in value:
            self.bad(f"{key} can't use {INPUT}: a viewer must never choose what your account says "
                     "(reward replies can use {color})")
        for name in sorted(set(PLACEHOLDER_RE.findall(value)) - allowed):
            self.bad(f"{key} can't use {{{name}}} here")
        return value


def _number(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def _scan_input(value, where, errors, depth=0):
    """Any {input} in `value` is an error (the color field of obs_color/ha_light is handled by the caller)."""
    if depth > 20:
        errors.append(f"{where}: nested too deeply")
    elif isinstance(value, str):
        if INPUT in value:
            errors.append(f"{where}: {INPUT} may only be the whole color of obs_color or ha_light")
    elif isinstance(value, dict):
        for k, v in value.items():
            _scan_input(k, where, errors, depth + 1)
            _scan_input(v, f"{where}.{k}", errors, depth + 1)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _scan_input(v, f"{where}[{i}]", errors, depth + 1)


def _check_list(actions, allow_unsafe, where, errors, depth):
    if isinstance(actions, dict):
        actions = [actions]
    if not isinstance(actions, list):
        errors.append(f"{where} must be a list of actions")
        return
    if depth > MAX_NESTING:
        errors.append(f"{where}: actions are nested too deeply")
        return
    if len(actions) > MAX_ACTIONS:
        errors.append(f"{where}: at most {MAX_ACTIONS} actions per list")
        return
    for i, a in enumerate(actions):
        w = f"{where}[{i}]"
        if isinstance(a, list):
            _check_list(a, allow_unsafe, w, errors, depth + 1)
        elif isinstance(a, str):
            errors.append(f"{w}: plain text is a shell command, which a reward can't run; "
                          "use an action like {obs_scene: Live}")
        elif not isinstance(a, dict) or not a:
            errors.append(f"{w}: must be an action like {{obs_scene: Live}}")
        else:
            for kind, arg in a.items():
                if not isinstance(kind, str):
                    errors.append(f"{w}: action names must be text")
                    continue
                if kind in REWARD_DENIED:
                    errors.append(f"{w}: {_clean(kind, 40)} can never run from a reward")
                elif kind not in SAFE_ACTIONS and not allow_unsafe:
                    errors.append(f"{w}: {_clean(kind, 40)} isn't allowed in a reward unless "
                                  "allow_unsafe_actions is on")
                if kind == "delay":
                    n = _number(arg)
                    if n is None or not 0 <= n <= DELAY_MAX:
                        errors.append(f"{w}: delay must be 0 to {DELAY_MAX} seconds")
                if kind in COLOR_ACTIONS and isinstance(arg, dict):
                    for k, v in arg.items():
                        if not (k == "color" and v == INPUT):
                            _scan_input(k, f"{w}.{kind}", errors)
                            _scan_input(v, f"{w}.{kind}.{k}", errors)
                else:
                    _scan_input(arg, f"{w}.{kind}", errors)


def check_reward_actions(actions, allow_unsafe, where="actions"):
    """Errors (list of str) for a reward's action list: bare-string actions are always refused,
    anything outside the allowlist needs allow_unsafe, delay <= 30 s, <= 20 actions per (nested)
    list, and {input} only as the whole `color` of obs_color / ha_light."""
    errors = []
    if actions is not None:
        _check_list(actions, bool(allow_unsafe), where, errors, 0)
        total = sum(_number(a["delay"]) or 0 for a in flatten(actions) if "delay" in a)
        if total >= DELAY_TOTAL_MAX:
            errors.append(f"{where}: delays add up to {total:g}s; keep them under {DELAY_TOTAL_MAX}s "
                          f"(a redemption is refunded after {ACTION_DEADLINE}s)")
    return errors


def flatten(actions, depth=0):
    """The action dicts of a (nested) action list, in run order: lets a run stop between any two steps."""
    if isinstance(actions, dict):
        return [actions]
    if not isinstance(actions, list) or depth > MAX_NESTING + 1:
        return []
    out = []
    for a in actions:
        if isinstance(a, list):
            out.extend(flatten(a, depth + 1))
        elif isinstance(a, dict):
            out.append(a)
    return out


def uses_input(actions):
    """True if any obs_color / ha_light in `actions` (nested lists too) has color: "{input}"."""
    if isinstance(actions, list):
        return any(uses_input(a) for a in actions)
    if isinstance(actions, dict):
        return any(k in COLOR_ACTIONS and isinstance(v, dict) and v.get("color") == INPUT
                   for k, v in actions.items())
    return False


def _with_color(actions, hex_color):
    """A copy of `actions` with every color: "{input}" of obs_color / ha_light set to hex_color."""
    if isinstance(actions, list):
        return [_with_color(a, hex_color) for a in actions]
    if isinstance(actions, dict):
        out = {}
        for k, v in actions.items():
            v = copy.deepcopy(v)
            if k in COLOR_ACTIONS and isinstance(v, dict) and v.get("color") == INPUT:
                v["color"] = hex_color
            out[k] = v
        return out
    return copy.deepcopy(actions)


def _list(section, key, errors, cap):
    items = section.get(key)
    if items is None:
        return []
    if not isinstance(items, list):
        errors.append(f"{key} must be a list")
        return []
    if len(items) > cap:
        errors.append(f"{key}: at most {cap}; the rest are ignored")
        items = items[:cap]
    return items


def _item(kind, i, item, errors, ids):
    """(_Fields, id) for item i of `kind`, or (None, None) if it can't even be identified."""
    if not isinstance(item, dict):
        errors.append(f"{kind}[{i}]: must be a mapping")
        return None, None
    raw = item.get("id")
    label = f" ({_clean(raw, 32)})" if isinstance(raw, str) and raw else ""
    f = _Fields(item, f"{kind}[{i}]{label}", errors)
    if not isinstance(raw, str) or not ID_RE.fullmatch(raw):
        f.bad("id must be 1-32 of a-z, 0-9, _ or -")
    elif raw in ids:
        f.bad(f"id {raw} is used twice")
    else:
        ids.add(raw)
    return f, raw


def _timer(f):
    messages = f.item.get("messages")
    if isinstance(messages, str):
        messages = [messages]
    if not isinstance(messages, list) or not 1 <= len(messages) <= MAX_MESSAGES:
        f.bad(f"messages must be a list of 1 to {MAX_MESSAGES} messages")
        messages = []
    return {"enabled": f.bool("enabled", True),
            "messages": [f.chat(f"messages[{j}]", m, TIMER_PH) for j, m in enumerate(messages)],
            "interval_minutes": f.int("interval_minutes", 15, 2, 240),
            "min_chat_lines": f.int("min_chat_lines", 5, 0, 1000),
            "announce": f.bool("announce", False),
            "color": f.choice("color", "primary", ANNOUNCE_COLORS)}


def _trigger(f, key, value, triggers):
    if not isinstance(value, str) or not TRIGGER_RE.fullmatch(value.strip().lower()):
        f.bad(f"{key} must be ! then 1-25 of a-z, 0-9 or _ (like !discord)")
        return None
    value = value.strip().lower()
    if value in triggers:
        f.bad(f"{key} {value} is already used by another command")
        return None
    triggers.add(value)
    return value


def _unloaded_kind(kind):
    """True for a well-formed plugin built-in name that isn't registered right now."""
    return isinstance(kind, str) and bool(chatcmd.KIND_RE.match(kind)) and kind not in chatcmd.CORE_KINDS \
        and kind not in BUILTINS and chatcmd.get(kind) is None


def _command(f, triggers):
    builtin = f.item.get("builtin")
    plugin = chatcmd.all_builtins().get(builtin) if isinstance(builtin, str) else None
    unloaded = not plugin and _unloaded_kind(builtin)
    if builtin not in (None,) + BUILTINS and not plugin and not unloaded:
        f.bad("builtin must be one of " + ", ".join(BUILTINS) + " (or empty for a text command, "
              "or a plugin's built-in name)")
        builtin = None
    if unloaded:    # kept (so a panel save doesn't delete it); its placeholders can't be checked until it loads
        response_ph, extra_key, extra_ph = ALL_PH, None, None
    else:
        response_ph, extra_key, extra_ph = (COMMAND_PH | plugin.placeholders, None, None) if plugin \
            else KIND_PH[builtin]
    extras = {}
    for key in ("offline_response", "missing_response"):
        value = f.item.get(key)
        if key == extra_key:
            extras[key] = f.chat(key, value if value not in (None, "") else EXTRA_DEFAULTS[key], extra_ph)
        else:
            if value not in (None, ""):
                f.bad(f"{key} is only for builtin: {'uptime' if key == 'offline_response' else 'stats'}")
            extras[key] = ""
    response = f.item.get("response")
    if builtin in ("commands", "top10") and isinstance(response, str) and response.strip().startswith("{list}"):
        f.bad("response can't start with {list}: put a word first (like \"Commands: {list}\") so the "
              "list keeps its first entry whole")
    trigger = _trigger(f, "trigger", f.item.get("trigger"), triggers)
    aliases = f.get("aliases", [])
    if isinstance(aliases, str):
        aliases = [aliases]
    if not isinstance(aliases, list) or len(aliases) > MAX_ALIASES:
        f.bad(f"aliases must be a list of at most {MAX_ALIASES}")
        aliases = []
    aliases = [_trigger(f, f"aliases[{j}]", a, triggers) for j, a in enumerate(aliases)]
    return {"enabled": f.bool("enabled", True), "trigger": trigger, "aliases": aliases,
            "response": f.chat("response", f.item.get("response") or (plugin.default_response if plugin else None),
                               response_ph, required=not unloaded),
            "builtin": builtin, **extras, "count": f.int("count", 10, 1, 10),
            "permission": f.choice("permission", "everyone", LEVELS),
            "cooldown_seconds": f.int("cooldown_seconds", 30, 0, 3600),
            "user_cooldown_seconds": f.int("user_cooldown_seconds", 0, 0, 86400),
            "reply": f.bool("reply", True)}


def _color(f, key, default):
    v = f.get(key, default)
    try:
        from core.colors import parse_color, to_hex
        return to_hex(parse_color(v)).upper()
    except (ValueError, TypeError):
        f.bad(f"{key} must be a color like #9146FF or purple")
        return default


def _actions(f, key, allow_unsafe):
    actions = f.item.get(key)
    if actions is None:
        return []
    errors = check_reward_actions(actions, allow_unsafe, key)
    for e in errors:
        f.bad(e)
    return copy.deepcopy([actions] if isinstance(actions, dict) else actions) if not errors else []


def _reward(f, titles, twitch_ids):
    twitch_id = f.get("twitch_id", "")
    if isinstance(twitch_id, int) and not isinstance(twitch_id, bool):
        twitch_id = str(twitch_id)
    if not isinstance(twitch_id, str) or (twitch_id.strip() and not TWITCH_ID_RE.fullmatch(twitch_id.strip())):
        f.bad("twitch_id must be a Twitch reward id (or empty)")
        twitch_id = ""
    twitch_id = twitch_id.strip()
    if twitch_id and twitch_id in twitch_ids:
        f.bad(f"twitch_id {twitch_id} is used by another reward")
    twitch_ids.add(twitch_id)
    title = f.text("title", "", 45, required=True)
    if title and title.casefold() in titles:
        f.bad(f"title {title!r} is used by another reward (Twitch needs unique titles)")
    titles.add(title.casefold())
    allow_unsafe = f.bool("allow_unsafe_actions", False)
    out = {"twitch_id": twitch_id, "managed": f.bool("managed", True), "enabled": f.bool("enabled", True),
           "title": title, "cost": f.int("cost", 0, 1, 2 ** 31 - 1),
           "prompt": f.text("prompt", "", 200),
           "input_required": f.bool("input_required", False),
           "background_color": _color(f, "background_color", "#9146FF"),
           "global_cooldown_seconds": f.int("global_cooldown_seconds", 0, 0, 604800),
           "max_per_stream": f.int("max_per_stream", 0, 0, 1_000_000),
           "max_per_user_per_stream": f.int("max_per_user_per_stream", 0, 0, 1_000_000),
           "skip_queue": f.bool("skip_queue", False),
           "actions": _actions(f, "actions", allow_unsafe),
           "revert_after_seconds": f.int("revert_after_seconds", 0, 0, 3600),
           "revert": _actions(f, "revert", allow_unsafe),
           "reply": f.chat("reply", f.item.get("reply"), REPLY_PH, required=False),
           "allow_unsafe_actions": allow_unsafe}
    if (uses_input(out["actions"]) or uses_input(out["revert"])) and not out["input_required"]:
        f.bad(f"actions use {INPUT}, so input_required must be true")
    return out


def _stats_section(stats, errors):
    if stats is None:
        stats = {}
    if not isinstance(stats, dict):
        errors.append("stats must be a mapping")
        stats = {}
    f = _Fields(stats, "stats", errors)
    exclude = f.get("exclude", list(DEFAULT_EXCLUDE))
    if not isinstance(exclude, list) or len(exclude) > MAX_EXCLUDE:
        f.bad(f"exclude must be a list of at most {MAX_EXCLUDE} logins")
        exclude = list(DEFAULT_EXCLUDE)
    logins = []
    for j, login in enumerate(exclude):
        login = login.strip().lower() if isinstance(login, str) else None
        if not login or not LOGIN_RE.fullmatch(login):
            f.bad(f"exclude[{j}] must be a Twitch login (a-z, 0-9, _)")
        elif login not in logins:
            logins.append(login)
    return {"enabled": f.bool("enabled", True), "include_broadcaster": f.bool("include_broadcaster", False),
            "allow_lookup": f.bool("allow_lookup", True), "exclude": logins}


def _default(cid, trigger, response, builtin=None, enabled=True, cooldown=5, user_cooldown=0, **extra):
    return {"id": cid, "enabled": enabled, "trigger": trigger, "aliases": extra.pop("aliases", []),
            "response": response, "permission": "everyone", "cooldown_seconds": cooldown,
            "user_cooldown_seconds": user_cooldown, "reply": True, "builtin": builtin,
            "offline_response": extra.pop("offline_response", ""), "missing_response": extra.pop("missing_response", ""),
            "count": 10}


DEFAULT_COMMANDS = [
    _default("commands", "!commands", "Commands: {list}", "commands"),
    _default("uptime", "!uptime", "{channel} has been live for {uptime}.", "uptime",
             offline_response="{channel} is offline right now."),
    _default("game", "!game", "{channel} is playing {game}."),
    _default("title", "!title", "Stream title: {title}"),
    _default("stats", "!stats", "{user}: {messages} messages (#{rank} all time), {stream_messages} this stream. "
             "Here since {first_seen}.", "stats", user_cooldown=30, missing_response="{user}, no stats yet. Say hi!"),
    _default("top10", "!top10", "Top chatters ({period}): {list}", "top10", user_cooldown=30, aliases=["!top"]),
    _default("lurk", "!lurk", "{user} is lurking. Enjoy the stream!", user_cooldown=300),
    _default("unlurk", "!unlurk", "Welcome back, {user}!", user_cooldown=300),
    _default("discord", "!discord", "Join the Discord: add your invite link here", enabled=False),
    _default("socials", "!socials", "Follow {channel} everywhere: add your links here", enabled=False),
    _default("schedule", "!schedule", "Stream schedule: add your days and times here", enabled=False),
    _default("specs", "!specs", "PC specs: add your CPU, GPU and RAM here", enabled=False),
]


def validate(section):
    """(normalised twitch_bot section, [readable errors]). Items with an error are left out of
    the normalised section; everything kept has every field, defaults filled in.

    `commands` absent means DEFAULT_COMMANDS; an explicit [] or a bare `commands:` (null) means none.
    database_url and database_password_file are config.yaml-only (CONFIG_ONLY): the panel never
    sees or sends them. With a password file (or DECK_DATABASE_PASSWORD), database_url should be
    credential-free; the password is passed to ChatStats separately."""
    errors = []
    if section is None:
        section = {}
    if not isinstance(section, dict):
        return validate({})[0], ["twitch_bot must be a mapping"]
    top = _Fields(section, "twitch_bot", errors)
    url = section.get("database_url")
    if url is None:
        url = ""
    if not isinstance(url, str) or len(url) > 2000 or CTRL_RE.search(url):
        top.bad("database_url must be one line of text (a SQLAlchemy URL), or empty")   # never echo it
        url = ""
    password_file = section.get("database_password_file")
    if password_file is None:
        password_file = ""
    if not isinstance(password_file, str) or len(password_file) > 4096 or CTRL_RE.search(password_file):
        top.bad("database_password_file must be a file path, or empty")
        password_file = ""
    out = {"enabled": top.bool("enabled", False), "only_when_live": top.bool("only_when_live", True),
           "database_url": url.strip(), "database_password_file": password_file.strip(),
           "stats": _stats_section(section.get("stats"), errors),
           "timers": [], "commands": [], "rewards": []}
    ids = set()
    for i, item in enumerate(_list(section, "timers", errors, MAX_TIMERS)):
        f, tid = _item("timers", i, item, errors, ids)
        if f:
            t = _timer(f)
            if f.ok:
                out["timers"].append({"id": tid, **t})
    ids, triggers = set(), set()
    commands = section if "commands" in section else {"commands": DEFAULT_COMMANDS}   # absent: the defaults
    for i, item in enumerate(_list(commands, "commands", errors, MAX_COMMANDS)):
        f, cid = _item("commands", i, item, errors, ids)
        if f:
            c = _command(f, triggers)
            if f.ok:
                out["commands"].append({"id": cid, **c})
    ids, titles, twitch_ids = set(), set(), {""}
    for i, item in enumerate(_list(section, "rewards", errors, MAX_REWARDS)):
        f, rid = _item("rewards", i, item, errors, ids)
        if f:
            r = _reward(f, titles, twitch_ids)
            if f.ok:
                out["rewards"].append({"id": rid, **r})
    return out, errors


def _level(msg):
    badges = {b for b in msg.get("badges") or [] if isinstance(b, str)}
    if "broadcaster" in badges:
        return 4
    if "moderator" in badges or msg.get("mod") is True:
        return 3
    if "vip" in badges:
        return 2
    if "subscriber" in badges or "founder" in badges or msg.get("sub") is True:
        return 1
    return 0


def _uptime(started_at):
    try:
        start = datetime.datetime.strptime(str(started_at), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return ""
    minutes = max(0, int((datetime.datetime.now(datetime.timezone.utc) - start).total_seconds() // 60))
    return f"{minutes // 60}h {minutes % 60}m" if minutes >= 60 else f"{minutes}m"


def _num(n):
    return f"{int(n or 0):,}"


def _date(ts):
    try:
        d = datetime.datetime.fromtimestamp(float(ts))
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"
    return f"{MONTHS[d.month - 1]} {d.day}, {d.year}"


def default_database_url():
    from core.paths import state_file
    return f"sqlite:///{state_file('chatstats.db')}"


def _dropped(error):
    """Twitch definitely refused the message (is_sent: false), as opposed to us not trying."""
    return isinstance(error, ApiError) and str(error).startswith("Twitch didn't send it")
