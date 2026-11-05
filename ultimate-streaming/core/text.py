"""Cleaning untrusted text (viewer names, chat, EventSub payloads, remote error text, printer job names):
the one shared core. Callers add their own extras (redaction, markdown escaping, prefix stripping) on top.

    clean("\\x1b[31mhi\\u202e\\nthere")            # "hi there"
    clean(name, 25, newlines="drop", marks=None)    # options: see clean()

clean() always removes ANSI escapes and the characters in DROP (control, format, surrogate, line and
paragraph separators), except ZWJ (U+200D, which joins emoji sequences) unless keep_zwj=False.

The tables are built once, on first use, from this Python's Unicode database (one pass over every
code point, about 0.2 s); after that a call is a few str.translate / regex passes, so a megabyte
stays fast.
"""
import functools
import re
import sys
import unicodedata

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]")   # CSI sequences and two-character escapes
ASTRAL_RE = re.compile(r"[\U00010000-\U0010ffff]")
BREAKS = "\r\n\x85\u2028\u2029"     # line breaks (after CRLF -> LF): all of them also in DROP
DROP = frozenset(("Cc", "Cf", "Cs", "Zl", "Zp"))   # Cf: bidi overrides, LRM/RLM, BOM, zero-width ...
MARKS = frozenset(("Mn", "Me"))                    # combining marks (also those of class 0, like U+0489)
ZWJ = "\u200d"
NEWLINES = ("space", "drop")
# Stands in for an astral mark when looking for runs of marks: the re module tests a BMP-only class
# with a bitmap, but astral ranges one by one, and there are hundreds of astral marks. A noncharacter
# (category Cn: never dropped, never a mark); one that is really in the text reads as "x" there.
STAND_IN = "\ufdd0"


@functools.cache
def _tables():
    """{"drop"/"keep": {code point: None} for str.translate (keep: all but ZWJ), "probe": astral
    marks -> STAND_IN (and STAND_IN -> "x"), "runs": a regex class of BMP marks and STAND_IN}."""
    cats = list(map(unicodedata.category, map(chr, range(sys.maxunicode + 1))))
    drop = {c: None for c, cat in enumerate(cats) if cat in DROP}
    keep = {c: v for c, v in drop.items() if c != ord(ZWJ)}
    marks = [c for c, cat in enumerate(cats) if cat in MARKS]
    probe = {c: STAND_IN for c in marks if c >= 0x10000} | {ord(STAND_IN): "x"}
    runs = "[" + "".join(re.escape(chr(c)) for c in marks if c < 0x10000) + STAND_IN + "]"
    return {"drop": drop, "keep": keep, "probe": probe, "runs": runs}


@functools.cache
def _translation(keep_zwj, newlines, probe):
    """The str.translate table for one clean() call: steps 4 and 5 in one pass (and, for a probe,
    astral marks -> STAND_IN as well)."""
    t = _tables()
    table = t["keep" if keep_zwj else "drop"] | {ord(c): " " if newlines == "space" else None for c in BREAKS}
    if newlines == "space":
        table[ord("\t")] = " "
    return (table | t["probe"]) if probe else table


@functools.lru_cache(maxsize=16)
def _runs_re(n):
    """More than n marks in a row (in a probe string)."""
    return re.compile(f"{_tables()['runs']}{{{n + 1},}}")


def _collapse(text, probe, n):
    """text with each run of marks cut to n, the runs found in `probe` (text with its astral marks
    replaced by STAND_IN: same length, same positions)."""
    runs = _runs_re(n)
    if probe is text:
        return runs.sub(lambda m: m.group()[:n], text)
    out, last = [], 0
    for m in runs.finditer(probe):
        out.append(text[last:m.start() + n])
        last = m.end()
    out.append(text[last:])
    return "".join(out)


def _str(text):
    return "" if text is None else text if isinstance(text, str) else str(text)


def _count(value, name):
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError(f"{name} must be None or an int >= 0")


def strip_ansi(text):
    """text without ANSI escape sequences (CSI `ESC [ ... final` and two-character `ESC x`)."""
    text = _str(text)
    return ANSI_RE.sub("", text) if "\x1b" in text else text


def collapse_marks(text, n=2):
    """At most n combining marks (Mn/Me) after each base character: no "zalgo" text."""
    _count(n, "n")
    text = _str(text)
    astral = STAND_IN in text or ASTRAL_RE.search(text)
    return _collapse(text, text.translate(_tables()["probe"]) if astral else text, n)


def clean(text, cap=None, *, newlines="space", marks=2, collapse=True, keep_zwj=True) -> str:
    """Untrusted text made safe to show, log or pass on, in this order:
    None -> "" (other non-str values -> str()); pre-sliced to cap * 4 when capped; ANSI escapes removed;
    line breaks (CRLF, CR, LF, U+0085, U+2028, U+2029) turned into one space each (newlines="space",
    which also turns a tab into a space) or deleted ("drop"); every Cc/Cf/Cs/Zl/Zp character deleted
    (ZWJ kept when keep_zwj); at most `marks` combining marks per base character (None: unlimited);
    whitespace runs collapsed to one space and the ends stripped (collapse); cut to `cap` characters,
    without a trailing ZWJ or combining mark left dangling by the cut."""
    _count(cap, "cap")
    _count(marks, "marks")
    if newlines not in NEWLINES:
        raise ValueError(f"newlines must be one of {NEWLINES}")
    text = _str(text)
    if cap is not None:
        text = text[:cap * 4]
    text = strip_ansi(text).replace("\r\n", "\n")
    if marks is None:
        text = text.translate(_translation(keep_zwj, newlines, False))
    else:
        # One pass handles the line breaks, drops the characters and builds the probe; only text
        # holding an astral mark (or STAND_IN itself) needs a second pass for the real text.
        probe = text.translate(_translation(keep_zwj, newlines, True))
        if STAND_IN in probe or STAND_IN in text:
            text = _collapse(text.translate(_translation(keep_zwj, newlines, False)), probe, marks)
        else:
            text = _collapse(probe, probe, marks)
    if collapse:
        text = " ".join(text.split())
    if cap is not None and len(text) > cap:
        text = text[:cap]
        while text and (text[-1] == ZWJ or unicodedata.category(text[-1]) in MARKS):
            text = text[:-1]
        if collapse:
            text = text.rstrip()
    return text
