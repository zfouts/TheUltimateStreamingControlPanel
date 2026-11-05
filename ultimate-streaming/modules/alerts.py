"""On-stream alerts for Twitch follows, subs, resubs, gifted subs, cheers and raids.

Events come from EventSub (modules/eventsub.py) as "twitch_event". Each one becomes an alert in
the overlay text file alert.txt (one at a time, `show_seconds` each, then blank), updates the
latest_* files, adds to the BRB hype meter (modules/brb.py) and can post a chat thank-you or, for
a raid, shout the raider out. Everything except the overlay is off by default.

    alerts:
      enabled: true              # overlay alerts; EventSub only subscribes to these events when true
      show_seconds: 6            # 2-30
      show_message: false        # a resub's / cheer's viewer message as a second line on the overlay
      text:                      # placeholders {user} {months} {total} {bits} {viewers} {tier}
        follow: "{user} just followed!"
        sub: "{user} just subscribed!"
        resub: "{user} resubscribed for {months} months!"
        gift: "{user} gifted {total} subs!"
        cheer: "{user} cheered {bits} bits!"
        raid: "{user} is raiding with {viewers} viewers!"
      storm:                     # many events at once: one alert with a count, no names, no chat
        window_seconds: 30
        follows: 5               # more follows than this in the window: a follow storm
        others: 3                # more subs/resubs/gifts/cheers/raids than this: a storm for those
        text: {follow: "{count} new followers!", sub: "{count} new subs!", raid: "{count} raids!"}
      chat: {follow: "", sub: "", resub: "", gift: "", cheer: "", raid: ""}   # "" = no thank-you
      hype: {follow: 3, sub: 10, resub: 10, gift_per_sub: 4, cheer_per_100_bits: 5,
             raid_per_10_viewers: 1, max_per_event: 20}
      shoutout: {on_raid: false, min_viewers: 10, allow: [], deny: []}   # logins
      blocklist: []              # case-insensitive substrings of names/messages; "re:<pattern>" is a regex
                                 # (at most 100 entries of 200 chars; a regex with nested repeats like
                                 # (a+)+ or a backreference is refused, as it could freeze the worker)

Viewers choose names, messages and how many events arrive (follow-bot and hate-raid storms), so:
templates are filled in one pass by modules.twitch_bot.render (a name is never re-expanded and
can't start a chat command); the viewer message is never sent to chat and only shown on the
overlay when show_message is on; a blocklisted name shows as "Someone" and gets no thank-you,
shoutout or latest_* file; a storm shows one updating count instead of names. Chat thank-yous
are limited to one per kind per 10 s and 5 per 30 s, and are dropped (never retried) on any
error. Automatic raid shoutouts need the stream to be live and respect Twitch's cooldowns
locally (125 s between shoutouts, an hour per channel) before any API call.

Files: overlay/alert.txt (blank when idle), latest_follower.txt, latest_sub.txt, latest_cheer.txt,
latest_raid.txt; state in alerts.json (latest per kind and the last raider, never messages; 0600).

Threading: the listener runs on EventSub's websocket thread and only validates and queues (at
most 50 waiting; the oldest is dropped). One worker thread, "alerts", does every file write, chat
post, shoutout and hype call. Pausing (the `alerts` deck key) is kept in memory only: alerts run
again after a restart.
"""
import collections
import functools
import json
import logging
import math
import os
import re
import threading
import time
import unicodedata
from re import _constants as sre_c, _parser as sre_parse

from core import Module, action, display, listener, state
from core.paths import overlay_dir, state_file
from core.safefs import read_regular, write_atomic
from core.schema import UI, Choice
from core.twitch_ids import LOGIN_RE, USER_ID_RE
from core.webapi import ApiError
from modules.overlay import write_overlay
from modules._twitch_bot_config import clean_value, render

KINDS = ("follow", "sub", "resub", "gift", "cheer", "raid")
INT_FIELDS = ("tier", "months", "cumulative_months", "total", "bits", "viewers")
INT_MAX = 10_000_000
NAME_MAX = 25
MESSAGE_MAX = 500
PLACEHOLDER_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")
LEAD_RE = re.compile(r"^[\s!/]+")
TOKENISH_RE = re.compile(r"[A-Za-z0-9]{20,}")
REPEATS_MAX = 2      # blocklist regex: overlapping repeats in a run (each costs up to n tries)
OPTIONALS_MAX = 8    # blocklist regex: overlapping optionals / ambiguous alternatives (each up to 2)
CONFUSABLES = str.maketrans({"а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i",
                             "ј": "j", "ѕ": "s", "ԁ": "d", "ɡ": "g", "ο": "o", "α": "a", "ν": "v", "κ": "k",
                             "τ": "t", "ι": "i"})

STATE_FILE = "alerts.json"
STATE_MAX = 65536
QUEUE_MAX = 50
GAP = 1.0                     # seconds of blank alert.txt between two alerts
SOMEONE = "Someone"
TEST_USER = "TestFollower"
MESSAGE_CAP, WRAP, MESSAGE_LINES = 200, 50, 2
CHAT_KIND_COOLDOWN, CHAT_CAP, CHAT_WINDOW = 10, 5, 30
SHOUTOUT_GAP, SHOUTOUT_USER_GAP, SHOUTOUT_PENDING_MAX = 125, 3600, 600
BLOCKLIST_MAX, BLOCK_ENTRY_MAX = 100, 200
LOGIN_LIST_MAX = 500
TEMPLATE_MAX = 300

GROUP = {"follow": "follow", "sub": "others", "resub": "others", "gift": "others", "cheer": "others",
         "raid": "others"}
STORM_KEY = {"follow": "follow", "sub": "sub", "resub": "sub", "gift": "sub", "cheer": "cheer", "raid": "raid"}
CHEER_STORM = "{count} cheers!"
LATEST_KIND = {"follow": "follow", "sub": "sub", "resub": "sub", "gift": "sub", "cheer": "cheer", "raid": "raid"}
LATEST_FILE = {"follow": "latest_follower.txt", "sub": "latest_sub.txt", "cheer": "latest_cheer.txt",
               "raid": "latest_raid.txt"}
LATEST_EXTRA = {"follow": None, "sub": "total", "cheer": "bits", "raid": "viewers"}

DEFAULT_TEXT = {
    "follow": "{user} just followed!",
    "sub": "{user} just subscribed!",
    "resub": "{user} resubscribed for {months} months!",
    "gift": "{user} gifted {total} subs!",
    "cheer": "{user} cheered {bits} bits!",
    "raid": "{user} is raiding with {viewers} viewers!",
}
DEFAULT_STORM_TEXT = {"follow": "{count} new followers!", "sub": "{count} new subs!", "raid": "{count} raids!"}
DEFAULT_HYPE = {"follow": 3, "sub": 10, "resub": 10, "gift_per_sub": 4, "cheer_per_100_bits": 5,
                "raid_per_10_viewers": 1, "max_per_event": 20}
TEXT_PH = frozenset(("user", "months", "total", "bits", "viewers", "tier"))


# ----- text

def one_line(text):
    """Rendered template text on one line: CR/LF become spaces, whitespace collapsed, never
    starting with ! or /."""
    text = (text or "").replace("\r", " ").replace("\n", " ")
    return LEAD_RE.sub("", " ".join(text.split()))


def _norm(text):
    """For blocklist matching: NFKD (fullwidth and styled letters become plain, accents split
    off), casefolded, combining marks (strike-through, accents) and format characters (zero-width,
    bidi) removed, and common Cyrillic/Greek lookalikes folded to Latin letters."""
    text = unicodedata.normalize("NFKD", unicodedata.normalize("NFKD", text or "").casefold())
    text = "".join(c for c in text if unicodedata.category(c) not in ("Mn", "Me", "Cf"))
    return text.translate(CONFUSABLES)


_CATEGORY_RE = {k: re.compile(v) for k, v in {
    sre_c.CATEGORY_DIGIT: r"\d", sre_c.CATEGORY_NOT_DIGIT: r"\D", sre_c.CATEGORY_SPACE: r"\s",
    sre_c.CATEGORY_NOT_SPACE: r"\S", sre_c.CATEGORY_WORD: r"\w", sre_c.CATEGORY_NOT_WORD: r"\W"}.items()}
_SINGLE = (sre_c.LITERAL, sre_c.NOT_LITERAL, sre_c.IN, sre_c.ANY)     # items that match one character
_SET_MAX = 0x2000      # a character class bigger than this isn't listed out


def _may_match(body, cp):
    r"""Can a one-character item ([(op, av)]: x, [...], \w, .) match code point `cp`, ignoring case?
    Exact for every code point; True when the item isn't one it understands."""
    if len(body) != 1:
        return True
    op, av = body[0]
    c = chr(cp)
    forms = {x for x in (c, c.lower(), c.upper()) if len(x) == 1}
    if op in (sre_c.LITERAL, sre_c.NOT_LITERAL):
        return any(x.lower() == chr(av).lower() for x in forms) == (op == sre_c.LITERAL)
    if op == sre_c.ANY:
        return c != "\n"
    if op != sre_c.IN:
        return True
    negate, hit = False, False
    for iop, iav in av:
        if iop == sre_c.NEGATE:
            negate = True
        elif iop == sre_c.LITERAL:
            hit = hit or any(x.lower() == chr(iav).lower() for x in forms)
        elif iop == sre_c.RANGE:
            hit = hit or any(iav[0] <= ord(x) <= iav[1] for x in forms)
        elif iop == sre_c.CATEGORY and iav in _CATEGORY_RE:
            hit = hit or _CATEGORY_RE[iav].fullmatch(c) is not None
        else:
            return True
    return hit != negate


@functools.cache
def _category_chars(cat):
    r"""Every code point \s or \d matches (both are small sets); None for the large categories."""
    test = {sre_c.CATEGORY_SPACE: str.isspace, sre_c.CATEGORY_DIGIT: str.isdecimal}.get(cat)
    return frozenset(cp for cp in range(0x110000) if test(chr(cp))) if test else None


def _chars(body):
    """The code points a one-character item can match, if it's a short list; else None."""
    if body is None or len(body) != 1 or body[0][0] not in (sre_c.LITERAL, sre_c.IN):
        return None
    op, av = body[0]
    if op == sre_c.LITERAL:
        return {av}
    out = set()
    for iop, iav in av:
        if iop == sre_c.LITERAL:
            out.add(iav)
        elif iop == sre_c.RANGE and iav[1] - iav[0] < _SET_MAX:
            out.update(range(iav[0], iav[1] + 1))
        elif iop == sre_c.CATEGORY and _category_chars(iav) is not None:
            out |= _category_chars(iav)
        else:
            return None                     # NEGATE, \w, \S, a huge range...
    return out


def _disjoint(a, b):
    """True only when no character can match both one-character items; exact, never a guess."""
    if a is None or b is None or len(a) != 1 or len(b) != 1:
        return False
    for listed, other in ((_chars(a), b), (_chars(b), a)):
        if listed is not None:
            return not any(_may_match(other, cp) for cp in listed)
    return False


def _overlap(p, q):
    """Can parts p and q (tuples of one-character items, or None for "anything") share a character?"""
    if p is None or q is None:
        return True
    return any(not _disjoint(x, y) for x in p for y in q)


def _singles(items):
    """A group's one-character items as a part, or None if it holds anything else."""
    out = []
    for op, av in items:
        if op in _SINGLE:
            out.append([(op, av)])
        elif op == sre_c.SUBPATTERN and _singles(av[3]) is not None:
            out.extend(_singles(av[3]))
        else:
            return None
    return tuple(out)


def regex_risk(pattern):
    r"""Why a blocklist regex could backtrack for too long (Python regexes can't time out), or None.
    Judged on the parsed pattern, not its text. Refused:
    - backreferences;
    - anything of varying length (a+, a?, a{1,3}, a|bb) inside a repeat that runs more than once;
    - parts of varying length that can trade characters with each other ("the run") stacked up:
      a repeat (\w*) costs up to n tries and an optional or ambiguous alternative ((er)?, (a|aa))
      up to 2. Two overlapping repeats before anything that can fail (.*a+=, \w*\w*$, \w*\s*\w*!),
      three anywhere, or too many overlapping optionals are refused.
    A part that overlaps no other part of the run (\d+\s*) adds no cost of its own. A required
    character no part can match ends the run (the "f" in \s+free). Alternatives that start with
    different characters ((free|cheap), (com|net)) are only ever one choice. Overlap is tested
    exactly. Possessive repeats and atomic groups don't backtrack and don't join a run."""
    try:
        tree = sre_parse.parse(pattern, re.IGNORECASE)
    except Exception:
        return None             # re.compile reports it
    refs = (sre_c.GROUPREF, sre_c.GROUPREF_EXISTS, sre_c.GROUPREF_IGNORE, sre_c.GROUPREF_LOC_IGNORE,
            sre_c.GROUPREF_UNI_IGNORE)

    class Risk(Exception):
        pass

    def cost(run):
        """(repeats, optionals) in the run that overlap another part; one lone repeat still counts."""
        tangled = [kind for i, (kind, part) in enumerate(run)
                   if any(_overlap(part, other) for j, (_, other) in enumerate(run) if j != i)]
        k = tangled.count("n") or (1 if any(kind == "n" for kind, _ in run) else 0)
        return k, tangled.count("2")

    def can_fail(run):
        """Something that can fail comes next: every way the run can split the text gets tried."""
        k, m = cost(run)
        if k > 1 or m > OPTIONALS_MAX or (k == 1 and m > OPTIONALS_MAX // 2):
            raise Risk("parts that can take the same characters, before more pattern (like .*a+= or "
                       r"\w*\w*$)")

    def required(run, item):
        """A required one-character item ends the run if no part of it can match that item."""
        can_fail(run)
        if item is not None and run and not any(_overlap(part, (item,)) for _, part in run):
            run.clear()

    def add(run, kind, part):
        run.append((kind, part))
        k, m = cost(run)
        if k > REPEATS_MAX or m > OPTIONALS_MAX:
            raise Risk(r"too many parts that can take the same characters, like \w*\w*\w*")

    def first(alt):
        while alt and alt[0][0] == sre_c.SUBPATTERN:
            alt = alt[0][1][3]
        return [alt[0]] if alt and alt[0][0] in _SINGLE else None

    def walk(items, run, in_repeat):
        for op, av in items:
            if op in refs:
                raise Risk("backreferences")
            if op in (sre_c.MAX_REPEAT, sre_c.MIN_REPEAT):
                lo, hi, sub = av
                if in_repeat and lo != hi:
                    raise Risk("a repeat inside a repeat, like (a+)+ or (a?)+")
                body = list(sub) if len(sub) == 1 and sub[0][0] in _SINGLE else None
                if hi == 1:                                 # (...)? runs once at most
                    mine = list(run)
                    walk(sub, mine, in_repeat)
                    if lo == 1 or (cost(mine), len(mine)) > (cost(run), len(run)):
                        run[:] = mine
                    if lo != hi:
                        add(run, "2", _singles(sub))
                    continue
                walk(sub, [], True)                         # nesting checks only
                if lo >= 1:
                    required(run, body)
                if lo != hi:
                    add(run, "n", (body,) if body else _singles(sub))
            elif op == sre_c.POSSESSIVE_REPEAT:
                lo, _, sub = av
                walk(sub, [], False)
                if lo >= 1:
                    required(run, list(sub) if len(sub) == 1 and sub[0][0] in _SINGLE else None)
            elif op == sre_c.ATOMIC_GROUP:
                can_fail(run)
                walk(av, [], False)
            elif op in _SINGLE:
                required(run, [(op, av)])
            elif op == sre_c.AT:
                if av not in (sre_c.AT_BEGINNING, sre_c.AT_BEGINNING_STRING):
                    can_fail(run)
            elif op == sre_c.BRANCH:
                if in_repeat:
                    raise Risk("alternatives inside a repeat, like (a|aa)+")
                can_fail(run)
                alts = av[1]
                heads = [first(alt) for alt in alts]
                one_choice = all(heads) and all(_disjoint(heads[i], heads[j]) for i in range(len(heads))
                                                for j in range(i + 1, len(heads)))
                worst = None                                # one alternative always runs: the costliest wins
                for alt in alts:
                    mine = list(run)
                    walk(alt, mine, in_repeat)
                    if worst is None or (cost(mine), len(mine)) > (cost(worst), len(worst)):
                        worst = mine
                run[:] = worst
                if not one_choice:
                    for _ in range(max(1, (len(alts) - 1).bit_length())):
                        add(run, "2", None)
            elif op == sre_c.SUBPATTERN:
                walk(av[3], run, in_repeat)
            elif op in (sre_c.ASSERT, sre_c.ASSERT_NOT):
                can_fail(run)
                walk(av[1], list(run), in_repeat)           # zero-width: checked, but doesn't extend the run

    try:
        walk(tree, [], False)
    except Risk as e:
        return str(e)
    except RecursionError:
        return "too deeply nested"
    except Exception:
        return "a pattern that can't be checked"
    return None


_PROBES = tuple(c * 500 for c in "a x1 一.!") + ("a " * 250, "ab" * 250, "a1" * 250, "a" * 499 + "\n")
PROBE_SECONDS = 0.05     # an accepted blocklist regex must handle every probe text within this


def regex_too_slow(compiled, clock=time.perf_counter):
    """The static check's backstop: time the regex on long, repetitive texts like a flood's."""
    start = clock()
    for text in _PROBES:
        compiled.search(text)
        if clock() - start > PROBE_SECONDS:
            return True
    return False


def blocked(blocklist, *texts):
    """True if any of texts matches a blocklist entry: ("sub", normalised substring) | ("re", compiled)."""
    normed = [_norm(t) for t in texts if t]
    for kind, entry in blocklist:
        for t in normed:
            if (entry in t) if kind == "sub" else entry.search(t):
                return True
    return False


def message_lines(message):
    """The viewer message for the overlay: one line of text, capped, hard-wrapped, at most 2 lines."""
    text = " ".join(clean_value(str(message).replace("\r", " ").replace("\n", " ").replace("\t", " "),
                                cap=MESSAGE_MAX, lead=False).split())[:MESSAGE_CAP]
    return [text[i:i + WRAP] for i in range(0, len(text), WRAP)][:MESSAGE_LINES]


def _short_error(e):
    status = getattr(e, "status", None)
    text = clean_value(f"{type(e).__name__}: {e}", cap=120, lead=False)
    text = TOKENISH_RE.sub("[…]", text)
    return f"HTTP {status}: {text}" if isinstance(status, int) else text


def _int(v, lo=0, hi=INT_MAX):
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def _ts(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0


# ----- the event

def validate_event(ev):
    """The event as a clean dict, or None if anything is malformed. Missing optional fields get
    their zero value; a wrong type or range drops the event."""
    if not isinstance(ev, dict):
        return None
    kind, user = ev.get("kind"), ev.get("user")
    if kind not in KINDS or not isinstance(user, str) or not 0 < len(user) <= NAME_MAX:
        return None
    login, uid = ev.get("user_login", ""), ev.get("user_id", "")
    anonymous, message, ts = ev.get("anonymous", False), ev.get("message", ""), ev.get("ts", 0.0)
    if not isinstance(login, str) or (login and not LOGIN_RE.fullmatch(login.lower())):
        return None
    if not isinstance(uid, str) or (uid and not USER_ID_RE.fullmatch(uid)):
        return None
    if not isinstance(anonymous, bool) or not isinstance(message, str) or len(message) > MESSAGE_MAX:
        return None
    if not _ts(ts):
        return None
    out = {"kind": kind, "login": login.lower(), "user_id": uid, "anonymous": anonymous, "message": message}
    for f in INT_FIELDS:
        v = ev.get(f, 0)
        if not _int(v, 0, 3 if f == "tier" else INT_MAX):
            return None
        out[f] = v
    name = clean_value(user, cap=NAME_MAX)
    out["user"] = name or SOMEONE
    if anonymous:
        out["login"], out["user_id"] = "", ""
    return out


# ----- config

def validate(section):
    """(config, [errors]). Every bad value gets its default and one error naming its key."""
    errors = []
    cfg = {"enabled": True, "show_seconds": 6, "show_message": False, "text": dict(DEFAULT_TEXT),
           "storm": {"window_seconds": 30, "follows": 5, "others": 3, "text": dict(DEFAULT_STORM_TEXT)},
           "chat": {k: "" for k in KINDS}, "hype": dict(DEFAULT_HYPE),
           "shoutout": {"on_raid": False, "min_viewers": 10, "allow": frozenset(), "deny": frozenset()},
           "blocklist": ()}
    if section is None:
        return cfg, errors
    if not isinstance(section, dict):
        return cfg, ["alerts must be a mapping"]

    def sub(d, key, where):
        v = d.get(key)
        if v is None:
            return {}
        if not isinstance(v, dict):
            errors.append(f"{where} must be a mapping")
            return {}
        return v

    def boolean(d, key, out, where):
        if key in d and d[key] is not None:
            if isinstance(d[key], bool):
                out[key] = d[key]
            else:
                errors.append(f"{where} must be true or false")

    def number(d, key, out, where, lo, hi, whole=True):
        if key not in d or d[key] is None:
            return
        v = d[key]
        if isinstance(v, float) and whole and v.is_integer():
            v = int(v)
        ok = (_int(v, lo, hi) if whole else
              isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and lo <= v <= hi)
        if ok:
            out[key] = v
        else:
            errors.append(f"{where} must be a {'whole ' if whole else ''}number from {lo} to {hi}")

    def template(d, key, out, where, allowed, empty_ok):
        if key not in d or d[key] is None:
            return
        v = d[key]
        if (isinstance(v, str) and len(v) <= TEMPLATE_MAX and (v.strip() or empty_ok)
                and not any(unicodedata.category(c)[0] == "C" and c not in "\n\r\t" for c in v)):
            out[key] = v.strip()
            unknown = sorted({m.group(1) for m in PLACEHOLDER_RE.finditer(v)} - allowed)
            if unknown:
                errors.append(f"{where}: unknown placeholder {{{unknown[0]}}} stays as written")
        else:
            errors.append(f"{where} must be {'' if empty_ok else 'non-empty '}text of at most "
                          f"{TEMPLATE_MAX} characters")

    def logins(d, key, where):
        v = d.get(key)
        if v is None:
            return frozenset()
        if not isinstance(v, list):
            errors.append(f"{where} must be a list of Twitch logins")
            return frozenset()
        out = set()
        for i, item in enumerate(v[:LOGIN_LIST_MAX]):
            name = item.strip().lstrip("@").lower() if isinstance(item, str) else None
            if name and LOGIN_RE.fullmatch(name):
                out.add(name)
            else:
                errors.append(f"{where}[{i}] is not a Twitch login; skipped")
        if len(v) > LOGIN_LIST_MAX:
            errors.append(f"{where}: at most {LOGIN_LIST_MAX} logins (the rest are ignored)")
        return frozenset(out)

    boolean(section, "enabled", cfg, "alerts.enabled")
    number(section, "show_seconds", cfg, "alerts.show_seconds", 2, 30, whole=False)
    boolean(section, "show_message", cfg, "alerts.show_message")
    text = sub(section, "text", "alerts.text")
    for k in KINDS:
        template(text, k, cfg["text"], f"alerts.text.{k}", TEXT_PH, False)
    storm = sub(section, "storm", "alerts.storm")
    number(storm, "window_seconds", cfg["storm"], "alerts.storm.window_seconds", 5, 600)
    number(storm, "follows", cfg["storm"], "alerts.storm.follows", 1, 1000)
    number(storm, "others", cfg["storm"], "alerts.storm.others", 1, 1000)
    stext = sub(storm, "text", "alerts.storm.text")
    for k in DEFAULT_STORM_TEXT:
        template(stext, k, cfg["storm"]["text"], f"alerts.storm.text.{k}", frozenset(("count",)), False)
    chat = sub(section, "chat", "alerts.chat")
    for k in KINDS:
        template(chat, k, cfg["chat"], f"alerts.chat.{k}", TEXT_PH, True)
    hype = sub(section, "hype", "alerts.hype")
    for k in DEFAULT_HYPE:
        number(hype, k, cfg["hype"], f"alerts.hype.{k}", 0, 100, whole=False)
    so = sub(section, "shoutout", "alerts.shoutout")
    boolean(so, "on_raid", cfg["shoutout"], "alerts.shoutout.on_raid")
    number(so, "min_viewers", cfg["shoutout"], "alerts.shoutout.min_viewers", 0, INT_MAX)
    cfg["shoutout"]["allow"] = logins(so, "allow", "alerts.shoutout.allow")
    cfg["shoutout"]["deny"] = logins(so, "deny", "alerts.shoutout.deny")

    items = section.get("blocklist")
    entries = []
    if items is not None and not isinstance(items, list):
        errors.append("alerts.blocklist must be a list")
    elif items:
        if len(items) > BLOCKLIST_MAX:
            errors.append(f"alerts.blocklist: at most {BLOCKLIST_MAX} entries (the rest are ignored)")
        for i, item in enumerate(items[:BLOCKLIST_MAX]):
            where = f"alerts.blocklist[{i}]"
            if not isinstance(item, str) or not item.strip() or len(item) > BLOCK_ENTRY_MAX:
                errors.append(f"{where} must be text of 1-{BLOCK_ENTRY_MAX} characters; skipped")
                continue
            if item.startswith("re:"):
                risk = regex_risk(item[3:])
                if risk:
                    errors.append(f"{where}: {risk} can freeze alerts; skipped")
                    continue
                try:
                    compiled = re.compile(item[3:], re.IGNORECASE)
                except re.error:
                    errors.append(f"{where} is not a valid regular expression; skipped")
                    continue
                if regex_too_slow(compiled):
                    errors.append(f"{where} is too slow on long messages; skipped")
                    continue
                entries.append(("re", compiled))
            else:
                entries.append(("sub", _norm(item.strip())))
    cfg["blocklist"] = tuple(entries)
    return cfg, errors


# ----- state (alerts.json)

def _state_name(v):
    return isinstance(v, str) and 0 < len(v) <= NAME_MAX and clean_value(v, cap=NAME_MAX) == v


def load_state(data, now):
    """{"latest": {kind: entry}, "last_raider": dict | None} from parsed JSON; every field checked,
    anything invalid dropped. A timestamp more than a day ahead of `now` is invalid."""
    out = {"latest": {}, "last_raider": None}
    if not isinstance(data, dict):
        return out

    def ts_ok(v):
        return _ts(v) and v <= now + 86400
    latest = data.get("latest")
    if isinstance(latest, dict):
        for kind, extra in LATEST_EXTRA.items():
            e = latest.get(kind)
            if not isinstance(e, dict) or not _state_name(e.get("user")) or not ts_ok(e.get("ts")):
                continue
            entry = {"user": e["user"], "ts": float(e["ts"])}
            if extra:
                if not _int(e.get(extra)):
                    continue
                entry[extra] = e[extra]
            out["latest"][kind] = entry
    r = data.get("last_raider")
    if (isinstance(r, dict) and isinstance(r.get("login"), str) and LOGIN_RE.fullmatch(r["login"])
            and isinstance(r.get("user_id"), str) and USER_ID_RE.fullmatch(r["user_id"])
            and _state_name(r.get("name")) and _int(r.get("viewers")) and ts_ok(r.get("ts"))):
        out["last_raider"] = {"login": r["login"], "user_id": r["user_id"], "name": r["name"],
                              "viewers": r["viewers"], "ts": float(r["ts"])}
    return out


def latest_text(kind, e):
    if kind == "sub":
        return f"{e['user']} ×{e['total']}" if e.get("total") else e["user"]
    if kind == "cheer":
        return f"{e['user']} · {e['bits']} bits"
    if kind == "raid":
        return f"{e['user']} · {e['viewers']} viewers"
    return e["user"]


class Alerts(Module):
    """Twitch alerts on the overlay. Pause state is in memory only: a restart resumes alerts."""

    config_key = "alerts"

    def __init__(self, bot):
        super().__init__(bot)
        # Injectable (tests): clocks, files, and whether on_load starts the worker thread.
        self.wall, self.mono = time.time, time.monotonic
        self.start_worker = True
        self.state_path = state_file(STATE_FILE)
        self.dir = overlay_dir()
        # Lock order: never call Twitch, BRB, the hub or write a file while holding self.lock.
        self.lock = threading.Lock()
        self.cfg, _ = validate(None)
        self.paused = False
        self.inbound = collections.deque()        # validated events from the listener
        self.display = collections.deque()        # alert items waiting to show (worker)
        self.showing = None                       # the item on alert.txt now
        self.until = 0.0                          # mono time the item on screen goes away
        self.blank_until = 0.0
        self.dropped = 0
        self.windows = {"follow": collections.deque(), "others": collections.deque()}   # (mono, storm key)
        self.storm = {"follow": False, "others": False}
        self.latest = {}
        self.raider = None
        self.chat_last, self.chat_times = {}, collections.deque()
        self.pending = None                       # the one raid shoutout waiting for its slot
        self.so_last = None                       # mono time of the last shoutout (this module, a key or the panel)
        self.so_user = {}                         # user id -> mono time of its last shoutout
        self.so_result = None
        self.written = None                       # alert.txt's content as last written
        self.write_failing = set()                # overlay files whose last write failed (warned once)
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.worker = None

    # ----- lifecycle

    def on_config(self, config):
        super().on_config(config)
        cfg, errors = validate(config)
        for e in errors:
            self.log.warning("alerts: %s", e)
        with self.lock:
            self.cfg = cfg
        self.wake.set()

    def on_load(self):
        os.makedirs(self.dir, exist_ok=True)
        st = self._load_state()
        with self.lock:
            self.latest, self.raider = st["latest"], st["last_raider"]
            latest = dict(self.latest)
        self._write("alert", "")
        for kind, e in latest.items():
            self._write_latest(kind, e)
        self.stopping.clear()
        if self.start_worker:
            self.worker = threading.Thread(target=self._run, daemon=True, name="alerts")
            self.worker.start()

    def on_unload(self):
        self.stopping.set()
        self.wake.set()
        worker = self.worker
        if worker and worker is not threading.current_thread():
            worker.join(timeout=2)
        if not (worker and worker.is_alive()):
            self._write("alert", "")

    # ----- contracts for other modules (fast, no I/O)

    def wants_events(self):
        """EventSub subscribes to follow/sub/cheer/raid events only while this is True."""
        return bool(self.cfg["enabled"])

    def last_raider(self):
        """{"login", "user_id", "name", "viewers", "ts"} of the last raider, or None."""
        with self.lock:
            return dict(self.raider) if self.raider else None

    # ----- the listener (EventSub's websocket thread: validate and queue, nothing else)

    @listener("twitch_event")
    def on_twitch_event(self, event=None, *_):
        ev = validate_event(event)
        if ev is None:
            return
        self._enqueue(ev)

    def _enqueue(self, ev):
        with self.lock:
            if self.paused or not self.cfg["enabled"]:
                return False
            if len(self.inbound) >= QUEUE_MAX:
                self.inbound.popleft()
                self.dropped += 1
            self.inbound.append(ev)
        self.wake.set()
        return True

    # ----- the worker

    def _run(self):
        while not self.stopping.is_set():
            self.wake.clear()
            try:
                delay = self._tick()
            except Exception as e:      # the worker must survive anything
                self.log.error("alerts: %s", _short_error(e))
                delay = 1.0
            self.wake.wait(max(0.05, min(delay, 1.0)))

    def _tick(self):
        """One round of the worker: take queued events, update storms, the overlay and the
        pending shoutout. Returns the seconds until something is due."""
        now = self.mono()
        with self.lock:
            events = list(self.inbound)
            self.inbound.clear()
            cfg = self.cfg
            if self.paused or not cfg["enabled"]:
                events = []
                self.display.clear()
                self.showing = None
                self.pending = None
        for ev in events:
            self._intake(ev, cfg, now)
        self._prune_storms(cfg, now)
        delay = self._show(cfg, now)
        wait = self._shoutout_step(cfg, now)
        return min(delay, wait if wait is not None else 1.0)

    def _intake(self, ev, cfg, now):
        kind, test = ev["kind"], ev.get("test", False)
        group, skey = GROUP[kind], STORM_KEY[kind]
        hidden = blocked(cfg["blocklist"], ev["user"], ev["login"])
        if test:
            storm = False
        else:
            with self.lock:
                win = self.windows[group]
                win.append((now, skey))
                self._prune(win, cfg, now)
                limit = cfg["storm"]["follows" if group == "follow" else "others"]
                if len(win) > limit and not self.storm[group]:
                    self._enter_storm(group)
                storm = self.storm[group]
            self._hype(ev, cfg)
        if storm:
            with self.lock:
                self._storm_item(group, skey, kind, cfg, now)
            return
        name = SOMEONE if hidden else ev["user"]
        months = ev["cumulative_months"] or ev["months"]
        values = {"user": name, "months": months, "total": ev["total"], "bits": ev["bits"],
                  "viewers": ev["viewers"], "tier": ev["tier"]}
        line = one_line(render(cfg["text"][kind], values))
        if not line:
            line = one_line(render(DEFAULT_TEXT[kind], {**values, "user": SOMEONE}))
        body = line
        msg = ev["message"]
        if (cfg["show_message"] and kind in ("resub", "cheer") and msg.strip() and not hidden
                and not blocked(cfg["blocklist"], msg)):
            lines = message_lines(msg)
            if lines:
                body = "\n".join([line, *lines])
        item = {"kind": kind, "user": name, "text": line, "body": body, "group": group, "storm": None,
                "test": test}
        with self.lock:
            if len(self.display) >= QUEUE_MAX:
                self.display.popleft()
                self.dropped += 1
            self.display.append(item)
        if test or hidden:
            return
        self._record_latest(ev, now)
        self._chat(kind, ev, cfg, values, now)
        if kind == "raid":
            self._raid(ev, cfg, now)

    # ----- storms

    @staticmethod
    def _prune(win, cfg, now):
        cutoff = now - cfg["storm"]["window_seconds"]
        while win and win[0][0] <= cutoff:
            win.popleft()

    def _prune_storms(self, cfg, now):
        with self.lock:
            for group, win in self.windows.items():
                self._prune(win, cfg, now)
                if self.storm[group] and not win:
                    self.storm[group] = False
                    self.log.info("alerts: %s storm over", group)

    def _enter_storm(self, group):
        """With self.lock held: names of this group waiting to show are dropped (the storm alert
        replaces them), and so is the alert on screen and a pending shoutout."""
        self.storm[group] = True
        self.log.info("alerts: %s storm: showing a count instead of names", group)
        self.display = collections.deque(i for i in self.display if i["group"] != group or i["test"])
        if self.showing and self.showing["group"] == group and not self.showing["test"]:
            self.showing = None
        if group == "others":
            self.pending = None

    def _storm_item(self, group, skey, kind, cfg, now):
        """With self.lock held: create or refresh the one storm alert for skey."""
        count = sum(1 for _t, k in self.windows[group] if k == skey)
        template = CHEER_STORM if skey == "cheer" else cfg["storm"]["text"][skey]
        line = one_line(render(template, {"count": count}))
        for item in ([self.showing] if self.showing else []) + list(self.display):
            if item.get("storm") == skey:
                item.update(text=line, body=line, kind=kind, dirty=True)
                return
        self.display.appendleft({"kind": kind, "user": "", "text": line, "body": line, "group": group,
                                 "storm": skey, "test": False, "dirty": False})

    # ----- the overlay

    def _show(self, cfg, now):
        publish = None
        with self.lock:
            cur = self.showing
            if cur and cur.get("dirty"):
                cur["dirty"] = False
                self.until = now + cfg["show_seconds"]
                publish = cur
            if cur and now >= self.until:
                self.showing = cur = None
                self.blank_until = now + GAP
            if cur is None and self.display and now >= self.blank_until:
                cur = self.showing = self.display.popleft()
                cur["dirty"] = False
                self.until = now + cfg["show_seconds"]
                publish = cur
            text = cur["body"] if cur else ""
            if cur:
                delay = self.until - now
            elif self.display:
                delay = self.blank_until - now
            else:
                delay = 1.0
            event = {"kind": publish["kind"], "user": publish["user"], "text": publish["text"],
                     "ts": self.wall()} if publish else None
        if text != self.written:
            self._write("alert", text)
        if event:
            hub = getattr(self.bot, "hub", None)
            if hub is not None:
                try:
                    hub.publish("alert", event)
                except Exception as e:
                    self.log.debug("alerts: hub: %s", _short_error(e))
        return max(0.0, delay)

    def _write(self, name, text):
        try:
            write_overlay(os.path.join(self.dir, f"{name}.txt"), text)
            if name == "alert":
                self.written = text
            if name in self.write_failing:
                self.write_failing.discard(name)
                self.log.info("alerts: writing %s.txt works again", name)
        except OSError as e:
            # warn once per failure streak; the tick retries, so repeats go to DEBUG
            level = logging.DEBUG if name in self.write_failing else logging.WARNING
            self.write_failing.add(name)
            self.log.log(level, "alerts: can't write %s.txt (%s)", name, type(e).__name__)

    def _write_latest(self, kind, entry):
        self._write(LATEST_FILE[kind][:-4], latest_text(kind, entry))

    # ----- latest & state

    def _record_latest(self, ev, now):
        kind = LATEST_KIND[ev["kind"]]
        entry = {"user": ev["user"], "ts": self.wall()}
        extra = LATEST_EXTRA[kind]
        if extra:
            entry[extra] = ev[extra] if ev["kind"] == "gift" or extra != "total" else 0
        with self.lock:
            self.latest[kind] = entry
            if ev["kind"] == "raid" and ev["login"] and ev["user_id"]:
                self.raider = {"login": ev["login"], "user_id": ev["user_id"], "name": ev["user"],
                               "viewers": ev["viewers"], "ts": entry["ts"]}
            data = {"latest": {k: dict(v) for k, v in self.latest.items()},
                    "last_raider": dict(self.raider) if self.raider else None}
        self._write_latest(kind, entry)
        self._save_state(data)

    def _load_state(self):
        try:
            raw, _st = read_regular(self.state_path, STATE_MAX)
            data = json.loads(raw.decode("utf-8"))
        except FileNotFoundError:
            return load_state(None, self.wall())
        except (OSError, ValueError) as e:
            self.log.warning("alerts: can't read %s (%s); starting fresh", STATE_FILE, type(e).__name__)
            return load_state(None, self.wall())
        return load_state(data, self.wall())

    def _save_state(self, data):
        """Atomic, 0600, never through a symlink (core.safefs.write_atomic). A failed write is
        logged; the in-memory state still applies."""
        try:
            write_atomic(self.state_path, json.dumps(data, indent=1, sort_keys=True).encode("utf-8"))
        except OSError as e:
            self.log.warning("alerts: can't save %s (%s)", STATE_FILE, type(e).__name__)

    # ----- hype, chat, shoutouts (worker thread, never with self.lock held)

    def _hype(self, ev, cfg):
        h, kind = cfg["hype"], ev["kind"]
        amount = {"follow": lambda: h["follow"], "sub": lambda: h["sub"], "resub": lambda: h["resub"],
                  "gift": lambda: h["gift_per_sub"] * ev["total"],
                  "cheer": lambda: h["cheer_per_100_bits"] * ev["bits"] / 100,
                  "raid": lambda: h["raid_per_10_viewers"] * ev["viewers"] / 10}[kind]()
        amount = min(amount, h["max_per_event"])
        if amount <= 0:
            return
        brb = self.bot.get_module("BRB")
        if brb is None:
            return
        try:
            brb.add_hype(amount)
        except Exception as e:
            self.log.debug("alerts: hype: %s", _short_error(e))

    def _twitch(self):
        tw = self.bot.get_module("Twitch")
        try:
            return tw if tw is not None and tw.linked() else None
        except Exception:
            return None

    def _chat(self, kind, ev, cfg, values, now):
        """A thank-you in chat: never with the viewer's message, at most one per kind per 10 s and
        5 per 30 s, dropped on any error."""
        template = cfg["chat"][kind]
        if not template or (kind == "raid" and (ev["anonymous"] or not ev["login"])):
            return
        tw = self._twitch()
        if tw is None:
            return
        with self.lock:
            last = self.chat_last.get(kind)
            if last is not None and now - last < CHAT_KIND_COOLDOWN:
                return
            while self.chat_times and self.chat_times[0] <= now - CHAT_WINDOW:
                self.chat_times.popleft()
            if len(self.chat_times) >= CHAT_CAP:
                return
            self.chat_last[kind] = now
            self.chat_times.append(now)
        text = one_line(render(template, values))
        if not text:
            return
        try:
            tw.send_chat(text)
        except ApiError as e:
            self.log.info("alerts: %s thank-you not sent (HTTP %s)", kind, e.status)
        except Exception as e:
            self.log.info("alerts: %s thank-you not sent (%s)", kind, type(e).__name__)

    def _raid(self, ev, cfg, now):
        """Queue a raid for the automatic shoutout if it passes every check that needs no API."""
        so = cfg["shoutout"]
        login, uid = ev["login"], ev["user_id"]
        if not so["on_raid"] or ev["viewers"] < so["min_viewers"] or not login or not uid:
            return
        if login in so["deny"] or (so["allow"] and login not in so["allow"]):
            return
        with self.lock:
            if self.storm["others"]:
                return
            last = self.so_user.get(uid)
            if last is not None and now - last < SHOUTOUT_USER_GAP:
                self.log.info("alerts: %s was shouted out less than an hour ago", login)
                return
            self.pending = {"login": login, "user_id": uid, "name": ev["user"], "viewers": ev["viewers"],
                            "created": now, "retried": False}
        self.wake.set()

    def _shoutout_step(self, cfg, now):
        """Try the pending shoutout when its slot comes. Returns seconds until the slot, or None."""
        with self.lock:
            p = self.pending
            if p is None:
                return None
            if self.paused or not cfg["enabled"] or not cfg["shoutout"]["on_raid"] or self.storm["others"]:
                self.pending = None
                return None
            if now - p["created"] > SHOUTOUT_PENDING_MAX:
                self.pending = None
                self.log.info("alerts: shoutout for %s dropped: waited too long", p["login"])
                return None
            if self.so_last is not None and now - self.so_last < SHOUTOUT_GAP:
                return SHOUTOUT_GAP - (now - self.so_last)
            last = self.so_user.get(p["user_id"])
            if last is not None and now - last < SHOUTOUT_USER_GAP:
                self.pending = None
                return None
            self.pending = None
        so = cfg["shoutout"]      # the config may have changed since the raid: check it again
        if (p["viewers"] < so["min_viewers"] or p["login"] in so["deny"]
                or (so["allow"] and p["login"] not in so["allow"])
                or blocked(cfg["blocklist"], p["name"], p["login"])):
            self.log.info("alerts: shoutout for %s dropped: the shoutout settings changed", p["login"])
            return None
        tw = self._twitch()
        if tw is None:
            self.log.info("alerts: shoutout for %s skipped: Twitch isn't linked", p["login"])
            return None
        try:
            live = bool((tw.stream_status() or {}).get("live"))
        except Exception as e:
            self.log.info("alerts: shoutout for %s skipped: can't tell if live (%s)", p["login"], _short_error(e))
            return None
        if not live:
            self.log.info("alerts: shoutout for %s skipped: not live", p["login"])
            return None
        with self.lock:
            self.so_last = max(self.so_last or 0, self.mono())
        try:
            tw.shoutout_id(p["user_id"])
        except Exception as e:
            err = _short_error(e)
            retry = isinstance(e, ApiError) and e.status == 429 and not p["retried"]
            with self.lock:
                self.so_result = {"login": p["login"], "ts": self.wall(), "ok": False, "error": err}
                if retry:
                    if self.pending is None:       # a newer raid waiting wins
                        self.pending = {**p, "retried": True}
                elif not (isinstance(e, ApiError) and e.status == 429):
                    self.so_user[p["user_id"]] = now
            self.log.info("alerts: shoutout for %s failed (%s)%s", p["login"], err,
                          "; retrying at the next slot" if retry else "")
            return SHOUTOUT_GAP if retry else None
        with self.lock:
            self.so_user[p["user_id"]] = now
            for uid in [u for u, t in self.so_user.items() if now - t >= SHOUTOUT_USER_GAP]:
                del self.so_user[uid]
            self.so_result = {"login": p["login"], "ts": self.wall(), "ok": True, "error": None}
        self.log.info("alerts: shouted out %s", p["login"])
        return None

    def note_shoutout(self, user_id):
        """A shoutout made outside this module (a deck key or the panel) uses up Twitch's global and
        per-channel cooldowns too: record it so the raid shoutout waits for them."""
        if not isinstance(user_id, str) or not USER_ID_RE.fullmatch(user_id):
            return
        now = self.mono()
        with self.lock:
            self.so_last = now
            self.so_user[user_id] = now
            for uid in [u for u, t in self.so_user.items() if now - t >= SHOUTOUT_USER_GAP]:
                del self.so_user[uid]
            if self.pending and self.pending["user_id"] == user_id:
                self.pending = None
        self.wake.set()

    # ----- deck

    @action("alerts", rewards=False, ui=UI("Alerts", "Twitch", arg=Choice(
        ("toggle", "Pause / resume alerts"), ("pause", "Pause alerts"), ("resume", "Resume alerts"),
        ("skip", "Clear the alert on screen"), ("test", "Show a test alert"))))
    def alerts_key(self, arg):
        """alerts: toggle | pause | resume | skip | test. Pausing stops the overlay, chat thank-yous,
        shoutouts and hype until resumed (in memory only: a restart resumes)."""
        if arg not in ("toggle", "pause", "resume", "skip", "test"):
            raise ValueError(f"unknown alerts action {arg!r}")
        if arg == "test":
            ev = {"kind": "follow", "user": TEST_USER, "login": "", "user_id": "", "anonymous": False,
                  "message": "", "test": True, **{f: 0 for f in INT_FIELDS}}
            if not self._enqueue(ev):
                raise RuntimeError("Alerts are paused or turned off")
            return
        with self.lock:
            if arg == "toggle":
                arg = "resume" if self.paused else "pause"
            if arg == "resume":
                self.paused = False
            else:
                if arg == "pause":
                    self.paused = True
                    self.pending = None
                    self.inbound.clear()
                # skip only clears what's queued for the screen: events not yet processed still
                # update the latest-* files, hype, thank-yous and the raid shoutout
                self.display.clear()
                self.showing = None
        self.wake.set()

    @state("alerts", ui=UI("Alerts are paused", "Twitch", arg=Choice(("paused", "paused"))))
    def alerts_state(self, arg, spec):
        """state: {alerts: paused}"""
        if arg != "paused":
            raise ValueError(f"unknown alerts state {arg!r}")
        return self.paused

    @display("last_raider", ui=UI("Last raider", "Twitch"))
    def last_raider_display(self, spec):
        """display: {source: last_raider} — who raided last and with how many viewers."""
        r = self.last_raider()
        if not r:
            return {}
        return {"label": f"Raid from\n{r['name'][:12]}\n{r['viewers']}"}

    # ----- panel

    def snapshot(self):
        with self.lock:
            return {
                "enabled": bool(self.cfg["enabled"]),
                "paused": self.paused,
                "showing": self.showing["text"] if self.showing else None,
                "queued": len(self.inbound) + len(self.display),
                "dropped": self.dropped,
                "storm": dict(self.storm),
                "latest": {k: {"user": v["user"], "ts": v["ts"]} for k, v in self.latest.items()},
                "last_raider": dict(self.raider) if self.raider else None,
                "shoutout": {"pending": self.pending["login"] if self.pending else None,
                             "last": dict(self.so_result) if self.so_result else None},
            }


def setup(bot):
    bot.add_module(Alerts(bot))
