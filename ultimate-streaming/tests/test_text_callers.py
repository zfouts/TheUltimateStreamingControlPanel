"""Parity: every cleaner converted to core.text (C1) against main's implementation of it.

The REFERENCE functions below are main's cleaners, copied as they were before the refactor (the
`tight` flag adds the documented tightenings and nothing else). For each wrapper and each string
in CORPUS:

- the wrapper's output equals main's output with the tightenings applied, and
- the strings whose output differs from main's (untightened) output are exactly the frozen set in
  DIFFS, each with the tightening that explains it.

The tightenings (C1), all of them uniform with core.text.clean:
- ANSI: ANSI escapes are now removed by announce, webhooks and chatstats too (their "[31m"
  fragments used to survive; the ESC itself was always dropped).
- CS: lone surrogates (category Cs) are now dropped by twitch_bot and bambu too. (Zl/Zp: both
  already dropped them through their CTRL_RE.)
- BREAKS (announce only): a lone CR becomes a space like any other line break (it used to be
  deleted), and VT/FF are deleted like the other control characters (they used to become spaces).
"""
import re
import unicodedata
import unittest

from tests import helpers  # noqa: F401  (sandboxes HOME, puts vendor/ on sys.path)

from core import chatstats, webhooks  # noqa: E402
from modules import _twitch_bot_config as tbc, announce, chat, eventsub  # noqa: E402
from plugins.bambu import wording  # noqa: E402

# ----- main's implementations (reference), frozen

DROP = ("Cc", "Cf", "Cs", "Zl", "Zp")
ZWJ = "\u200d"
ES_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]")
TW_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]?")
TW_CTRL_RE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")
TW_LEAD_RE = re.compile(r"^[\s/.!\\]+")
TW_LONG_RE = re.compile(r"[A-Za-z0-9]{20,}")
AN_LINE_BREAK_RE = re.compile(r"\r\n|[\n\t\v\f\x85\u2028\u2029]")
BAMBU_CTRL_RE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")
BAMBU_SPACE_RE = re.compile(r"\s+")
BAMBU_EXT_RE = re.compile(r"\.(?:gcode\.3mf|3mf|gcode)$", re.IGNORECASE)

# tightenings
T_ANSI_RE = ES_ANSI_RE                                       # what core.text strips
T_AN_LINE_BREAK_RE = re.compile(r"\r\n|[\r\n\t\x85\u2028\u2029]")


def _drop_cs(text):
    return "".join(c for c in text if unicodedata.category(c) != "Cs")


def ref_eventsub_clean(text, n=100):
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    text = ES_ANSI_RE.sub("", text)
    text = "".join(c for c in text if c in ZWJ or unicodedata.category(c) not in DROP)
    return text[:n]


def ref_collapse_marks(text, n=2):
    out, run = [], 0
    for c in text:
        if unicodedata.category(c) in ("Mn", "Me"):
            run += 1
            if run > n:
                continue
        else:
            run = 0
        out.append(c)
    return "".join(out)


def ref_eventsub_name(value):
    if not isinstance(value, str):
        return ""
    return ref_collapse_marks(ref_eventsub_clean(unicodedata.normalize("NFC", value), 25).strip())


def ref_eventsub_text(value):
    return ref_collapse_marks(ref_eventsub_clean(value, 500)) if isinstance(value, str) else ""


def ref_chat_text(text):
    action = text.startswith("\x01ACTION ")
    if action:
        text = text[len("\x01ACTION "):]
        if text.endswith("\x01"):
            text = text[:-1]
    return ref_collapse_marks(ref_eventsub_clean(text, 500 * 4))[:500], action


def _tw_strip_format(text):
    return "".join(c for c in text if c == "\u200d" or unicodedata.category(c) != "Cf")


def ref_tw_clean(text, n=200, tight=False):
    text = _tw_strip_format(TW_CTRL_RE.sub("", TW_ANSI_RE.sub("", str(text if text is not None else "")[:n * 4])))
    text = _drop_cs(text) if tight else text
    return TW_LONG_RE.sub("[\u2026]", text)[:n]


def ref_tw_clean_value(value, cap=100, lead=True, tight=False):
    text = "" if value is None else str(value)
    text = _tw_strip_format(TW_CTRL_RE.sub("", TW_ANSI_RE.sub("", text[:cap * 10])))
    text = _drop_cs(text) if tight else text
    return (TW_LEAD_RE.sub("", text) if lead else text)[:cap]


def ref_announce_clean_value(value, cap, tight=False):
    text = "" if value is None else str(value)[:cap * 4]
    if tight:
        text = T_ANSI_RE.sub("", text)
    text = (T_AN_LINE_BREAK_RE if tight else AN_LINE_BREAK_RE).sub(" ", text)
    text = "".join(c for c in text if c == "\u200d" or unicodedata.category(c) not in DROP)
    return " ".join(text.split())[:cap]


def ref_announce_value(value, cap, tight=False):
    """placeholder_values' pipeline for one value (cut and escape are unchanged by C1)."""
    return announce.cut(announce.escape(ref_announce_clean_value(value, cap, tight)), cap)


def ref_clean_display(display, login, tight=False):
    if not isinstance(display, str):
        return login
    display = display[:4 * 64]
    if tight:
        display = T_ANSI_RE.sub("", display)
    out = "".join(c for c in display if c == ZWJ or unicodedata.category(c) not in DROP)
    return out.strip()[:64].strip() or login


def ref_webhooks_clean(text, n=120, tight=False):
    text = "" if text is None else str(text)[:n * 4]
    if tight:
        text = T_ANSI_RE.sub("", text)
    text = "".join(c for c in text if unicodedata.category(c) not in DROP)
    return webhooks.redact(text)[:n]     # redact() is unchanged by C1


def ref_bambu_clean_text(value, cap, tight=False):
    text = "" if value is None else str(value)
    text = _tw_strip_format(BAMBU_CTRL_RE.sub("", TW_ANSI_RE.sub("", text[:cap * 10 + 64])))
    text = _drop_cs(text) if tight else text
    text = BAMBU_SPACE_RE.sub(" ", text).strip()
    return text if len(text) <= cap else text[:max(cap - 1, 0)].rstrip() + "\u2026"


def ref_bambu_clean_job(name, tight=False):
    if name is None:
        return None
    text = ref_bambu_clean_text(name, 1024, tight)
    text = re.split(r"[/\\]", text)[-1].strip()
    text = BAMBU_EXT_RE.sub("", text)
    return ref_bambu_clean_text(text, 64, tight) or None


# ----- the corpus

FAMILY = "\U0001F468\u200d\U0001F469\u200d\U0001F467"
WEBHOOK = "https://discord.com/api/webhooks/123456789012345678/" + "Tk" * 34
CORPUS = {
    "plain": "Hello, world!",
    "none": None,
    "int": 12345,
    "empty": "",
    "ansi_color": "\x1b[31mred\x1b[0m text",
    "ansi_cursor": "a\x1b[2J\x1b[1;1Hb\x1b[?25lc",
    "ansi_two_char": "x\x1bMy\x1b7z\x1b(B",
    "lone_esc": "end\x1b",
    "bidi": "evil\u202etxt.exe\u202c",
    "isolates": "\u2066a\u2069\u200e\u200fb\u061c",
    "zero_width": "zero\u200bwidth\u200c\u2060\ufeff",
    "soft_hyphen": "soft\u00adhyphen",
    "zwj_family": "fam " + FAMILY,
    "flag_tags": "\U0001F3F4\U000E0067\U000E0062\U000E0073\U000E0063\U000E0074\U000E007F flag",
    "zalgo": "z\u0301\u0302\u0303\u0304\u0305a\u0336\u0337\u0338l\u0489\u0489\u0489go",
    "astral_marks": "a\U0001d167\U0001d168\U0001d169\U000e0100b",
    "crlf": "line1\r\nline2\r\n",
    "lone_cr": "a\rb",
    "lf_tab": "a\nb\tc",
    "vt_ff": "a\vb\fc",
    "nel_ls_ps": "a\x85b\u2028c\u2029d",
    "c0_c1": "".join(map(chr, range(0x20))) + "\x7f\x80\x9b\x9f" + "ok",
    "surrogates": "a\ud800b\udfffc",
    "spaces": "  lots \u00a0\u3000  of   space  ",
    "command_lead": " /me !cmd .x \\y",
    "token": "token=" + "A1b2C3d4E5f6G7h8I9j0K1" * 2 + " end",
    "webhook": "see " + WEBHOOK + " now",
    "markdown": "**bold** _it_ ~x~ |s| > # - [l](u) `c` @everyone <@1> \\",
    "private_use": "\ue000\uf8ff\U000f0000",
    "noncharacter": "\ufdd0\ufdd0\u0301\u0302\u0303",
    "long": "abc \u200b" * 2000,
    "path_job": "/sd/jobs/\u202eevil\x1b[0m name.gcode.3mf",
    "mixed": "\x1b[1m\u202e" + FAMILY + "\r\n\tz\u0301\u0302\u0303 ok\u2028\ud83d",
}

# (name, the wrapper on this branch, main's version taking tight=, the tightenings that apply)
WRAPPERS = [
    ("eventsub.clean", eventsub.clean, lambda x, tight: ref_eventsub_clean(x), ()),
    ("eventsub.clean n=10", lambda x: eventsub.clean(x, 10), lambda x, tight: ref_eventsub_clean(x, 10), ()),
    ("eventsub.collapse_marks", lambda x: eventsub.collapse_marks(x) if isinstance(x, str) else None,
     lambda x, tight: ref_collapse_marks(x) if isinstance(x, str) else None, ()),
    ("eventsub._name", eventsub._name, lambda x, tight: ref_eventsub_name(x), ()),
    ("eventsub._text", eventsub._text, lambda x, tight: ref_eventsub_text(x), ()),
    ("chat.chat_text", lambda x: chat.chat_text("\x01ACTION " + x + "\x01") if isinstance(x, str) else None,
     lambda x, tight: ref_chat_text("\x01ACTION " + x + "\x01") if isinstance(x, str) else None, ()),
    ("twitch_bot._clean", tbc._clean, lambda x, tight: ref_tw_clean(x, tight=tight), ("CS",)),
    ("twitch_bot._clean n=30", lambda x: tbc._clean(x, 30), lambda x, tight: ref_tw_clean(x, 30, tight), ("CS",)),
    ("twitch_bot.clean_value", tbc.clean_value, lambda x, tight: ref_tw_clean_value(x, tight=tight), ("CS",)),
    ("twitch_bot.clean_value cap=12 lead=False", lambda x: tbc.clean_value(x, cap=12, lead=False),
     lambda x, tight: ref_tw_clean_value(x, 12, False, tight), ("CS",)),
    ("announce.clean_value 25", lambda x: announce.clean_value(x, 25),
     lambda x, tight: ref_announce_clean_value(x, 25, tight), ("ANSI", "BREAKS")),
    ("announce.clean_value 140", lambda x: announce.clean_value(x, 140),
     lambda x, tight: ref_announce_clean_value(x, 140, tight), ("ANSI", "BREAKS")),
    ("announce value 25", lambda x: announce.cut(announce.escape(announce.clean_value(x, 25)), 25),
     lambda x, tight: ref_announce_value(x, 25, tight), ("ANSI", "BREAKS")),
    ("chatstats.clean_display", lambda x: chatstats.clean_display(x, "login"),
     lambda x, tight: ref_clean_display(x, "login", tight), ("ANSI",)),
    ("webhooks.clean", webhooks.clean, lambda x, tight: ref_webhooks_clean(x, tight=tight), ("ANSI",)),
    ("webhooks.clean n=40", lambda x: webhooks.clean(x, 40), lambda x, tight: ref_webhooks_clean(x, 40, tight),
     ("ANSI",)),
    ("bambu.clean_text 50", lambda x: wording.clean_text(x, 50),
     lambda x, tight: ref_bambu_clean_text(x, 50, tight), ("CS",)),
    ("bambu.clean_text 8", lambda x: wording.clean_text(x, 8),
     lambda x, tight: ref_bambu_clean_text(x, 8, tight), ("CS",)),
    ("bambu.clean_job", wording.clean_job, lambda x, tight: ref_bambu_clean_job(x, tight), ("CS",)),
]

# Frozen: the corpus strings whose output differs from main's, per wrapper, and why.
_CS = {"surrogates": "CS", "mixed": "CS"}
_ANSI = {"ansi_color": "ANSI", "ansi_cursor": "ANSI", "ansi_two_char": "ANSI", "path_job": "ANSI", "mixed": "ANSI"}
_ANNOUNCE = _ANSI | {"lone_cr": "BREAKS", "vt_ff": "BREAKS"}
DIFFS = {
    "twitch_bot._clean": _CS,
    "twitch_bot._clean n=30": _CS,
    "twitch_bot.clean_value": _CS,
    "twitch_bot.clean_value cap=12 lead=False": {"surrogates": "CS"},     # mixed's surrogate is past the cap
    "announce.clean_value 25": _ANNOUNCE,
    "announce.clean_value 140": _ANNOUNCE,
    "announce value 25": _ANNOUNCE,
    "chatstats.clean_display": _ANSI,
    "webhooks.clean": _ANSI,
    "webhooks.clean n=40": _ANSI,
    "bambu.clean_text 50": _CS,
    "bambu.clean_text 8": {"surrogates": "CS"},                           # mixed's surrogate is past the cap
    "bambu.clean_job": _CS,
}


class TestParity(unittest.TestCase):
    def test_wrappers_match_main_plus_tightenings(self):
        for name, new, ref, tightenings in WRAPPERS:
            for key, value in CORPUS.items():
                with self.subTest(wrapper=name, case=key):
                    self.assertEqual(new(value), ref(value, True))

    def test_differences_from_main_are_the_documented_ones(self):
        for name, new, ref, tightenings in WRAPPERS:
            with self.subTest(wrapper=name):
                changed = {key for key, value in CORPUS.items() if ref(value, False) != ref(value, True)}
                self.assertEqual(changed, set(DIFFS.get(name, {})))
                self.assertLessEqual(set(DIFFS.get(name, {}).values()), set(tightenings))

    def test_each_tightening_is_explained(self):
        # each documented difference, shown on the smallest input that has it
        self.assertEqual((ref_announce_clean_value("a\x1b[31mb", 25), announce.clean_value("a\x1b[31mb", 25)),
                         ("a[31mb", "ab"))
        self.assertEqual((ref_announce_clean_value("a\rb\vc", 25), announce.clean_value("a\rb\vc", 25)),
                         ("ab c", "a bc"))
        self.assertEqual((ref_webhooks_clean("a\x1b[31mb"), webhooks.clean("a\x1b[31mb")), ("a[31mb", "ab"))
        self.assertEqual((ref_clean_display("a\x1b[31mb", "l"), chatstats.clean_display("a\x1b[31mb", "l")),
                         ("a[31mb", "ab"))
        self.assertEqual((ref_tw_clean("a\ud800b"), tbc._clean("a\ud800b")), ("a\ud800b", "ab"))
        self.assertEqual((ref_bambu_clean_text("a\ud800b", 9), wording.clean_text("a\ud800b", 9)),
                         ("a\ud800b", "ab"))


class TestRedactionBeforeCut(unittest.TestCase):
    """A token or webhook URL straddling the cap is redacted whole: the cut comes after redaction."""

    def test_token_straddling_the_cap(self):
        token = "Q7" * 20                                # 40 alphanumerics: token-shaped
        text = "x " * 95 + token + " tail"               # the token spans characters 190..229
        for n in range(150, 260):
            out = tbc._clean(text, n)
            self.assertNotIn("Q", out, n)
            self.assertNotIn("7", out, n)
            self.assertLessEqual(len(out), n)
        self.assertIn("[\u2026]", tbc._clean(text, 200))

    def test_webhook_straddling_the_cap(self):
        text = "x " * 50 + WEBHOOK + " tail"
        for n in range(60, 220):
            out = webhooks.clean(text, n)
            self.assertNotIn("T", out, n)                # "Tk" * 34: the token
            self.assertIsNone(re.search(r"[0-9]", out), (n, out))
            self.assertLessEqual(len(out), n)
        self.assertIn("<webhook>", webhooks.clean(text, 200))


if __name__ == "__main__":
    unittest.main()


class ReviewFindings(unittest.TestCase):
    """Cases from the code review of the shared cleaner: no secret may survive in any form."""

    def test_escape_before_webhooks_anchor_is_still_redacted(self):
        from core import webhooks
        for text in ("https://discord.com/api/\x1b[webhooks/123456/SECRETTOKEN",
                     "https://discord.com/api/\x1bWebhooks/1/SECRETTOKEN"):
            out = webhooks.clean(text)
            self.assertNotIn("SECRETTOKEN", out)
            self.assertNotIn("123456", out)

    def test_twitch_token_fragment_never_shown(self):
        from modules import _twitch_bot_config as cfg
        out = cfg._clean("\u200b" * 790 + "A" * 40, 200)
        self.assertNotRegex(out, r"A{3,}")
        out = cfg._clean("\x1b[" + "b" * 20 + " ok", 200)
        self.assertNotIn("b" * 10, out)
        self.assertIn("ok", out)
