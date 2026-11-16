"""Auto-mod keyword matching: the pure part of modules/automod.py (no network, no threads).

    automod:
      enabled: true
      exempt: vip                 # subscriber | vip | moderator: that level and up is never actioned
      terms:
        - {term: streamboo, action: ban}                       # match: word (the default)
        - {term: "buy followers", action: timeout, seconds: 600}
        - {term: "some phrase", match: anywhere, action: delete, enabled: false}

Spammers dodge plain keyword lists, so both the message and each term are reduced to a canonical
form first (canon()): Unicode compatibility forms (fullwidth, 𝐛𝐨𝐥𝐝, ⓒⓘⓡⓒⓛⓔⓓ), case, accents, common
lookalike letters from other scripts (Cyrillic "а", Greek "ο") and digit/symbol swaps ("b00", "$") all
fold to plain Latin letters, the same way on both sides. Then only letters and digits count:
spaces and punctuation between them are ignored.

  word      (default) the term's letters must start and end on word boundaries: "streamboo" catches
            "s t r e a m b o o", "streamboo .com", "STREAMB00", "str.eam-boo", but not "stream boost"
            or "upstream boo". A multi-word term matches those words next to each other.
  anywhere  also inside other words: "streamboo" catches "xstreamboocom" (and "stream boost").
            Needs at least MIN_ANYWHERE letters/digits.

No regular expressions: a viewer-facing filter must not be able to hang the chat reader.
"""
import unicodedata

ACTIONS = ("ban", "timeout", "delete")
MATCHES = ("word", "anywhere")
EXEMPT = ("subscriber", "vip", "moderator")    # mods and the broadcaster are always exempt
TERM_MAX = 100
MAX_TERMS = 200
MIN_ANYWHERE = 3
MIN_WORD = 2
SECONDS_MAX = 1209600                          # 14 days, Twitch's longest timeout
DEFAULT_SECONDS = 600
SEEN_MAX = 300                                 # characters of the canonical form shown by the panel's test

# Lookalikes that NFKC leaves alone, folded to the Latin letter they imitate (after casefold, so
# only lower case is needed). Digits and symbols are folded the same way on both sides, so a term
# with real digits still matches itself.
_FOLD = {
    # Cyrillic
    "а": "a", "в": "b", "г": "r", "д": "d", "е": "e", "ё": "e", "и": "u", "к": "k", "м": "m",
    "н": "h", "о": "o", "п": "n", "р": "p", "с": "c", "т": "t", "у": "y", "х": "x", "ш": "w", "ь": "b",
    "і": "i", "ї": "i", "ј": "j", "ѕ": "s", "ԁ": "d", "ӏ": "l", "ԛ": "q", "ԝ": "w", "ѡ": "w", "ү": "y",
    "һ": "h", "ɡ": "g",
    # Greek
    "α": "a", "β": "b", "γ": "y", "ε": "e", "η": "n", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p",
    "σ": "o", "ς": "c", "τ": "t", "υ": "u", "χ": "x", "ω": "w", "ϲ": "c", "μ": "u",
    # Latin oddities
    "ı": "i", "ȷ": "j", "ɑ": "a", "ɩ": "i", "ʀ": "r", "ʏ": "y", "ɴ": "n", "ᴏ": "o", "ꮪ": "s", "ſ": "s",
    "ø": "o", "đ": "d", "ħ": "h", "ł": "l", "ŧ": "t", "ß": "ss", "æ": "ae", "œ": "oe", "þ": "p",
    # Armenian / Cherokee lookalikes
    "օ": "o", "ս": "u", "ո": "n", "հ": "h",
    # digits standing in for letters
    "0": "o", "1": "l", "3": "e", "4": "a", "5": "s", "7": "t", "8": "b", "9": "g",
}
# Symbols standing in for letters ("$cam", "f!ll"). The same symbols are also ordinary punctuation
# ("streamboo!", "@name"), so a message is read both ways: with them as separators and as letters.
_SYMBOLS = {"!": "l", "|": "l", "@": "a", "$": "s"}
# "i" and "l" (and 1, !, |) are interchangeable in spam ("fo1low", "foIIow"), so they share one letter.
_SAME = {"i": "l"}


def _table(fold):
    return str.maketrans({**fold, **_SAME, **{k: _SAME.get(v, v) for k, v in fold.items()}})


_TABLE = _table(_FOLD)
_TABLE_SYMBOLS = _table({**_FOLD, **_SYMBOLS})


def canon(text, symbols=False):
    """The canonical form: NFKC, case-folded, accents removed, lookalikes folded (see _FOLD).
    Characters that aren't letters or digits are kept (as separators) for word matching;
    symbols=True folds the symbols in _SYMBOLS to letters too."""
    if not isinstance(text, str):
        return ""
    s = unicodedata.normalize("NFKC", text).casefold()
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if unicodedata.category(c) not in ("Mn", "Me"))
    return s.translate(_TABLE_SYMBOLS if symbols else _TABLE)


def squash(text, canonical=False):
    """canon(text) with everything but letters and digits removed: what "anywhere" compares.
    canonical=True: `text` is already canon()'s output."""
    return "".join(c for c in (text if canonical else canon(text)) if c.isalnum())


def words(text, canonical=False):
    """canon(text) split into runs of letters/digits: what "word" compares."""
    out, cur = [], []
    for c in (text if canonical else canon(text)):
        if c.isalnum():
            cur.append(c)
        elif cur:
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return out


def term_key(term):
    """What a term is compared as: its letters and digits, symbols read as letters ("$cam" = "scam")."""
    return squash(canon(term, symbols=True), canonical=True)


def strength(rule):
    """Which action wins when several terms match one message: ban, then the longest timeout, then delete."""
    return (ACTIONS[::-1].index(rule["action"]), rule["seconds"] if rule["action"] == "timeout" else 0)


# ----- config

def _bool(v, default):
    return v if isinstance(v, bool) else default


def validate(section):
    """(normalised automod section, [readable errors]). A term with an error is left out of the
    normalised copy, so the daemon keeps working with the rest; the panel refuses to save until
    the list is empty."""
    errors = []
    if section is not None and not isinstance(section, dict):
        errors.append("automod must be a mapping")
    section = section if isinstance(section, dict) else {}
    enabled = section.get("enabled", False)
    if not isinstance(enabled, bool):
        errors.append("enabled must be true or false")
        enabled = False
    exempt = section.get("exempt", "vip")
    if exempt not in EXEMPT:
        errors.append(f"exempt must be one of {', '.join(EXEMPT)}")
        exempt = "vip"
    raw = section.get("terms", [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        errors.append("terms must be a list")
        raw = []
    if len(raw) > MAX_TERMS:
        errors.append(f"at most {MAX_TERMS} terms (there are {len(raw)}); the rest are ignored")
        raw = raw[:MAX_TERMS]
    terms, seen = [], {}
    for i, item in enumerate(raw):
        where = f"terms[{i}]"
        if isinstance(item, str):
            item = {"term": item}
        if not isinstance(item, dict):
            errors.append(f"{where}: must be a mapping like {{term: streamboo, action: ban}}")
            continue
        term = item.get("term")
        if not isinstance(term, str) or not term.strip():
            errors.append(f"{where}: term is missing")
            continue
        term = " ".join(term.split())
        where = f'{where} ("{term[:40]}")'
        bad = False
        if len(term) > TERM_MAX:
            errors.append(f"{where}: at most {TERM_MAX} characters")
            bad = True
        match = item.get("match", "word")
        if match not in MATCHES:
            errors.append(f"{where}: match must be word or anywhere")
            bad = True
        action = item.get("action", "ban")
        if action not in ACTIONS:
            errors.append(f"{where}: action must be ban, timeout or delete")
            bad = True
        seconds = item.get("seconds", DEFAULT_SECONDS)
        if isinstance(seconds, bool) or not isinstance(seconds, int) or not 1 <= seconds <= SECONDS_MAX:
            errors.append(f"{where}: seconds must be a whole number from 1 to {SECONDS_MAX} (14 days)")
            bad = True
        enabled_term = item.get("enabled", True)
        if not isinstance(enabled_term, bool):
            errors.append(f"{where}: enabled must be true or false")
            bad = True
        if bad:
            continue
        key = (match, term_key(term))
        need, have = (MIN_ANYWHERE if match == "anywhere" else MIN_WORD), len(squash(term))
        if have < need:
            errors.append(f"{where}: needs at least {need} letters or digits" +
                          ("; use whole-word matching for short words" if match == "anywhere" and
                           have >= MIN_WORD else ""))
            continue
        if key in seen:
            errors.append(f'{where}: same as terms[{seen[key]}] once spaces, case and lookalike letters are '
                          "ignored")
            continue
        seen[key] = i
        terms.append({"term": term, "match": match, "action": action, "seconds": seconds,
                      "enabled": enabled_term})
    return {"enabled": enabled, "exempt": exempt, "terms": terms}, errors


# ----- matching

class Matcher:
    """The enabled terms of a normalised section, ready to check messages against."""

    def __init__(self, terms):
        self.anywhere, self.word = [], []
        for t in terms:
            if t.get("enabled", True):
                (self.anywhere if t["match"] == "anywhere" else self.word).append((term_key(t["term"]), t))

    def __bool__(self):
        return bool(self.anywhere or self.word)

    def find(self, text):
        """The strongest term that matches `text` (see strength()), or None. The text is read with
        _SYMBOLS as punctuation and, if it has any, again with them as letters."""
        if not isinstance(text, str) or not self:
            return None
        readings = [canon(text)]
        if any(c in text for c in _SYMBOLS):
            readings.append(canon(text, symbols=True))
        hits = []
        for c in readings:
            parts = words(c, canonical=True)
            flat = "".join(parts)
            hits += [t for key, t in self.anywhere if key in flat]
            if self.word:
                bounds, pos = {0}, 0
                for p in parts:
                    pos += len(p)
                    bounds.add(pos)
                hits += [t for key, t in self.word if _aligned(flat, key, bounds)]
        return max(hits, key=strength) if hits else None


def _aligned(flat, key, bounds):
    """True if `key` occurs in `flat` starting and ending on a word boundary (an index in `bounds`)."""
    i = flat.find(key)
    while i != -1:
        if i in bounds and i + len(key) in bounds:
            return True
        i = flat.find(key, i + 1)
    return False


def level(msg):
    """0 everyone, 1 subscriber, 2 vip, 3 moderator, 4 broadcaster (from the chat badges)."""
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


def exempt_level(name):
    """The lowest level() that is never actioned for an `exempt` setting."""
    return EXEMPT.index(name) + 1 if name in EXEMPT else 2
