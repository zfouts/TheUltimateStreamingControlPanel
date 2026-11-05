"""core/text.py: the shared cleaner for untrusted text."""
import re
import sys
import time
import unicodedata
import unittest

from tests import helpers  # noqa: F401  (sandboxes HOME, puts vendor/ on sys.path)

from core import text  # noqa: E402
from core.text import clean, collapse_marks, strip_ansi  # noqa: E402

FAMILY = "\U0001F468\u200d\U0001F469\u200d\U0001F467"     # \U0001f468\u200d\U0001f469\u200d\U0001f467
RAW = dict(newlines="drop", marks=None, collapse=False)    # only the steps that always run


class TestSteps(unittest.TestCase):
    def test_none_and_non_str(self):
        self.assertEqual(clean(None), "")
        self.assertEqual(clean(12345), "12345")
        self.assertEqual(clean(ValueError("a\nb")), "a b")
        self.assertEqual(clean(""), "")

    def test_pre_slice_bounds_the_input(self):
        # Only cap * 4 characters are looked at: what follows can never come back after cleaning.
        self.assertEqual(clean("\u200b" * 40 + "abc", 10), "")
        self.assertEqual(clean("\u200b" * 39 + "abc", 10), "a")
        self.assertEqual(clean("\u200b" * 40 + "abc"), "abc")

    def test_ansi_always_stripped(self):
        for kw in ({}, RAW, dict(newlines="space", collapse=False), dict(keep_zwj=False)):
            self.assertEqual(clean("a\x1b[31mred\x1b[0m\x1b[1;2;3Hb\x1bMc\x1b[?25ld", **kw), "aredbcd", kw)
        self.assertEqual(clean("a\x1b"), "a")                 # a lone ESC is a control character
        self.assertEqual(clean("a\x1b[", **RAW), "a")          # cut-off CSI: ESC [ as a two-character form

    def test_strip_ansi(self):
        self.assertEqual(strip_ansi("\x1b[38;5;196mhi\x1b[0m"), "hi")
        # ESC ( and ESC 7 are outside the two-character form (ESC @ to ESC _): clean() then drops the ESC
        self.assertEqual(strip_ansi("\x1b(B\x1b7x"), "\x1b(B\x1b7x")
        self.assertEqual(clean("\x1b(B\x1b7x"), "(B7x")
        self.assertEqual(strip_ansi("\x1bPx"), "x")
        self.assertEqual(strip_ansi("plain\u200b"), "plain\u200b")   # only ANSI
        self.assertEqual(strip_ansi(None), "")

    def test_line_breaks(self):
        nasty = "a\r\nb\rc\nd\x85e\u2028f\u2029g"
        self.assertEqual(clean(nasty, newlines="space", collapse=False), "a b c d e f g")
        self.assertEqual(clean(nasty, newlines="drop", collapse=False), "abcdefg")
        self.assertEqual(clean("a\r\n\r\nb", newlines="space", collapse=False), "a  b")   # CRLF is one break
        self.assertEqual(clean("a\n\nb"), "a b")

    def test_tab(self):
        self.assertEqual(clean("a\tb", collapse=False), "a b")
        self.assertEqual(clean("a\tb", newlines="drop", collapse=False), "ab")
        self.assertEqual(clean("a\vb\fc", collapse=False), "abc")        # other C0 controls: dropped

    def test_drop_categories(self):
        for c in ("\x00", "\x07", "\x7f", "\x9b", "\u00ad", "\u061c", "\u200b", "\u200e", "\u202a",
                  "\u202e", "\u2066", "\u2069", "\ufeff", "\U000e0041", "\U0001d173", "\ud800", "\udfff",
                  "\u2028", "\u2029"):
            self.assertEqual(clean("a" + c + "b", **RAW), "ab", ascii(c))
        # kept: other categories, including private use and noncharacters
        for c in ("\u00a0", "\ue000", "\ufdd0", "\u0301", "\u3000", "\U0001F600"):
            self.assertEqual(clean("a" + c + "b", **RAW), "a" + c + "b", ascii(c))

    def test_bidi_and_zero_width_removed(self):
        self.assertEqual(clean("evil\u202etxt.exe"), "eviltxt.exe")
        self.assertEqual(clean("zero\u200bwidth\u200c\u2060\ufeff"), "zerowidth")

    def test_zwj(self):
        self.assertEqual(clean(FAMILY), FAMILY)
        self.assertEqual(clean("x" + FAMILY + "y", 10), "x" + FAMILY + "y")
        self.assertEqual(clean(FAMILY, keep_zwj=False), "\U0001F468\U0001F469\U0001F467")

    def test_marks(self):
        zalgo = "z" + "\u0301\u0302\u0303\u0304\u0305" + "a" + "\u0489" * 4 + "l"
        self.assertEqual(clean(zalgo), "z\u0301\u0302a\u0489\u0489l")
        self.assertEqual(clean(zalgo, marks=0), "zal")
        self.assertEqual(clean(zalgo, marks=4), "z\u0301\u0302\u0303\u0304a\u0489\u0489\u0489\u0489l")
        self.assertEqual(clean(zalgo, marks=None), zalgo)
        self.assertEqual(clean("e\u0301"), "e\u0301")
        # a dropped character between marks doesn't reset the count: it is gone first
        self.assertEqual(clean("e\u0301\u200b\u0302\u0303"), "e\u0301\u0302")
        # astral marks count too, and mix with BMP ones
        self.assertEqual(clean("a\U0001d167\u0301\U0001d168\U000e0100b", marks=2), "a\U0001d167\u0301b")
        self.assertEqual(clean("\ufdd0\u0301\u0302\u0303\ufdd0"), "\ufdd0\u0301\u0302\ufdd0")

    def test_zalgo_capped(self):
        zalgo = "".join(c + "\u0336\u0337\u0338\u0335\u0334" * 20 for c in "zalgo")
        out = clean(zalgo)
        self.assertEqual(unicodedata.normalize("NFD", out).replace("\u0336\u0337", ""), "zalgo")
        self.assertEqual(len(out), 15)

    def test_collapse(self):
        self.assertEqual(clean("  a \u00a0\u3000 b  "), "a b")
        self.assertEqual(clean("  a   b  ", collapse=False), "  a   b  ")
        self.assertEqual(clean("a\u200b \u200b b"), "a b")

    def test_cap(self):
        self.assertEqual(clean("abcdef", 3), "abc")
        self.assertEqual(clean("abc", 3), "abc")
        self.assertEqual(clean("abc", 0), "")
        self.assertEqual(clean("  abcdef", 3), "abc")        # collapse runs before the cut

    def test_cap_leaves_no_dangling_zwj_or_mark(self):
        self.assertEqual(clean(FAMILY, 2), "\U0001F468")                 # the cut ended on the ZWJ
        self.assertEqual(clean(FAMILY, 4), "\U0001F468\u200d\U0001F469")
        self.assertEqual(clean("ab\u0301\u0302", 3), "ab")                # cut between two marks
        self.assertEqual(clean("ab\u0301\u200d\U0001F469", 4), "ab")     # marks and ZWJ both trimmed
        self.assertEqual(clean("\u0301\u0302\u0303x", 2), "")
        # nothing to trim when nothing was cut
        self.assertEqual(clean("ab\u0301", 3), "ab\u0301")
        self.assertEqual(clean("ab\u200d", 3), "ab\u200d")

    def test_options_validated(self):
        for kw in ({"newlines": "keep"}, {"marks": -1}, {"marks": 1.5}, {"marks": True}):
            with self.assertRaises(ValueError, msg=kw):
                clean("x", **kw)
        for cap in (-1, "10", 2.0, False):
            with self.assertRaises(ValueError, msg=cap):
                clean("x", cap)
        with self.assertRaises(ValueError):
            collapse_marks("x", -1)


class TestCollapseMarks(unittest.TestCase):
    def test_default_two(self):
        self.assertEqual(collapse_marks("a\u0301\u0302\u0303b\u0301"), "a\u0301\u0302b\u0301")
        self.assertEqual(collapse_marks("a\u0301\u0302\u0303", 0), "a")
        self.assertEqual(collapse_marks("a\u0301\u0302\u0303", 1), "a\u0301")

    def test_only_marks(self):
        # collapse_marks removes nothing else, ZWJ resets the run like any non-mark
        self.assertEqual(collapse_marks("\x1b[1m\u200b\u0301\u200d\u0302"), "\x1b[1m\u200b\u0301\u200d\u0302")
        self.assertEqual(collapse_marks("\U0001F600\U000e0100\U000e0101\U000e0102"),
                         "\U0001F600\U000e0100\U000e0101")
        self.assertEqual(collapse_marks(None), "")


class TestTables(unittest.TestCase):
    def test_tables_match_unicodedata(self):
        """The precomputed tables agree with unicodedata.category for every code point."""
        t = text._tables()
        runs = re.compile(t["runs"])
        bad = []
        for cp in range(sys.maxunicode + 1):
            cat = unicodedata.category(chr(cp))
            mark = (cp in t["probe"]) if cp >= 0x10000 else bool(runs.fullmatch(chr(cp)))
            if ((cp in t["drop"]) != (cat in text.DROP)
                    or (cp in t["keep"]) != (cat in text.DROP and cp != 0x200d)
                    or (cp != ord(text.STAND_IN) and mark != (cat in text.MARKS))):
                bad.append(hex(cp))
        self.assertEqual(bad, [])
        self.assertEqual(unicodedata.category(text.STAND_IN), "Cn")


class TestSpeed(unittest.TestCase):
    def test_megabyte_is_fast(self):
        """1 MB of chat-like text with every kind of junk mixed in: ANSI, bidi overrides, zero-width
        spaces, CR/LF/tab, U+2028, zalgo, ZWJ emoji and CJK. (Text that is nothing but junk is the
        slowest case, about as long again: still linear, a few regex / translate passes.)"""
        junk = ("\x1b[31mred\x1b[0m", "\u202eevil\u202c", "zero\u200bwidth", "two\r\nlines\ttab",
                "sep\u2028arated", "z\u0301\u0302\u0303\u0304\u0305algo", FAMILY, "\u4e2d\u6587")
        unit = "".join(f"viewer{i} says hello there, how is the stream going {j} " for i, j in zip(range(4), junk))
        unit += " ".join(junk) + "\n"
        sample = (unit * (1_000_000 // len(unit) + 1))[:1_000_000]
        clean("warm up")    # the tables are built once per process, on first use
        for sample in (sample, "x" * 1_000_000):
            best = min(self._time(sample) for _ in range(5))
            # A guard against pathological (quadratic or backtracking) slowdowns, not a benchmark:
            # ~45 ms here; CI runners can be several times slower, so the bound is generous.
            self.assertLess(best, 1.0, f"{best * 1000:.1f} ms")

    @staticmethod
    def _time(sample):
        t = time.perf_counter()
        clean(sample)
        return time.perf_counter() - t


if __name__ == "__main__":
    unittest.main()


class CutTrim(unittest.TestCase):
    def test_cut_leaves_no_trailing_space_when_collapsing(self):
        from core.text import clean
        self.assertEqual(clean("ab cd", 3), "ab")
        self.assertEqual(clean("ab cd", 3, collapse=False), "ab ")
