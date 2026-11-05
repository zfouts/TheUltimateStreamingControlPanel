"""Words for the printer's state: the one-line status (Discord captions), the multi-line block drawn
over the video, and the values the chat `print` built-in fills in.

Everything here takes the summary dict from printer.PrinterWatcher.snapshot(). Job names come from
the printer (a sliced file's name, which anyone who can send a file to it chooses), so they are
cleaned before they reach a screen or the chat.
"""
import datetime
import re

from core import text as textclean

# The ids a status element can choose from, in the order they appear.
STATUS_LINES = ("job", "layer", "percent", "time_left", "finish", "temps")

EXT_RE = re.compile(r"\.(?:gcode\.3mf|3mf|gcode)$", re.IGNORECASE)

JOB_MAX = 64
OVERLAY_JOB_MAX = 24


def clean_text(value, cap: int) -> str:
    """Text that is safe to draw or post: ANSI, control characters (CR/LF/tab too) and format
    characters removed, whitespace collapsed, at most `cap` characters (the last one "…" if cut)."""
    text = "" if value is None else str(value)
    text = textclean.clean(text[:cap * 10 + 64], newlines="drop", marks=None, collapse=True)
    return text if len(text) <= cap else text[:max(cap - 1, 0)].rstrip() + "…"


def clean_job(name) -> str | None:
    """A job name for display: directory and slicer extensions dropped, cleaned, at most 64 characters."""
    if name is None:
        return None
    text = clean_text(name, 1024)
    text = re.split(r"[/\\]", text)[-1].strip()
    text = EXT_RE.sub("", text)
    return clean_text(text, JOB_MAX) or None


def time_left(minutes) -> str | None:
    """Remaining print time in words, for captions and the stream overlay."""
    if minutes is None:
        return None
    if minutes < 1:
        return "almost done"
    h, m = divmod(minutes, 60)
    return f"{h} h {m} min left" if h else f"{m} min left"


def finish_clock(minutes, now: datetime.datetime) -> str | None:
    """`11:45 PM`, with the weekday when it falls on another day."""
    if minutes is None or minutes < 1:
        return None
    at = now + datetime.timedelta(minutes=minutes)
    clock = at.strftime("%I:%M %p").lstrip("0")
    return f"{at.strftime('%a')} {clock}" if at.date() != now.date() else clock


def _finish_time(minutes, now):
    clock = finish_clock(minutes, now)
    return f"done ~{clock}" if clock else None


def _name(name) -> str:
    return clean_text(name, 32)


def status_line(summary: dict, name: str = "Bob", ended: bool = False) -> str:
    """One line from PrinterWatcher.snapshot(). `ended` marks the final shot of a print, where
    IDLE means it was stopped."""
    name = _name(name)
    if summary.get("connected") is False:
        return f"{name} · Printer offline"
    state = summary.get("state")
    pct = f"{summary['percent']}%" if summary.get("percent") is not None else None
    layers = None
    if summary.get("layer") is not None and summary.get("total_layers"):
        layers = f"layer {summary['layer']}/{summary['total_layers']}"

    if state == "RUNNING":
        parts = ["Printing", pct, time_left(summary.get("remaining_min")), layers]
    elif state == "PAUSE":
        parts = ["Paused", pct, layers]
    elif state == "PREPARE":
        parts = ["Getting ready to print"]
    elif state == "FINISH":
        parts = ["Print finished"]
    elif state == "FAILED":
        parts = ["Print failed or was canceled"]
    elif state == "IDLE" and ended:
        parts = ["Print stopped"]
    elif state == "IDLE":
        return f"{name} · Idle"
    else:
        parts = [state.title() if state else "Status unknown"]

    return " · ".join(p for p in [name, *parts, summary.get("job")] if p)


def overlay_text(summary: dict, now: datetime.datetime, name: str = "Bob",
                 lines=STATUS_LINES) -> str:
    """The status block, one item per line; `now` is local time. Only the chosen `lines` show; the
    state words (Paused, Getting ready, ...) always do."""
    name = _name(name)
    if summary.get("connected") is False:
        return "Printer offline"
    state = summary.get("state")
    job = summary.get("job")
    if job and len(job) > OVERLAY_JOB_MAX:
        job = job[:OVERLAY_JOB_MAX - 1] + "…"
    pct = f"{summary['percent']}%" if summary.get("percent") is not None else None
    layer = None
    if summary.get("layer") is not None and summary.get("total_layers"):
        layer = f"Layer {summary['layer']}/{summary['total_layers']}"

    if state == "RUNNING":
        minutes = summary.get("remaining_min")
        items = [("job", job), ("layer", layer), ("percent", pct),
                 ("time_left", time_left(minutes)), ("finish", _finish_time(minutes, now))]
    elif state == "PAUSE":
        items = [(None, "Paused"), ("job", job), ("layer", layer), ("percent", pct)]
    elif state == "PREPARE":
        items = [(None, "Getting ready"), ("job", job)]
    elif state == "FINISH":
        items = [(None, "Print finished"), ("job", job)]
    elif state == "FAILED":
        items = [(None, "Print failed"), (None, "or canceled"), ("job", job)]
    else:
        items = [(None, f"{name} is idle")]
    out = [text for key, text in items if text and (key is None or key in lines)]

    if "temps" in lines:
        temps = [f"{label} {round(t)}°C"
                 for label, t in (("Nozzle", summary.get("nozzle_temp")), ("Bed", summary.get("bed_temp")))
                 if t is not None]
        if temps:
            out += ["", *temps] if out else temps
    return "\n".join(out)


def print_values(summary: dict, name: str = "Bob") -> dict[str, str]:
    """The placeholders of the chat `print` built-in, all strings ("" when unknown)."""
    name = _name(name)
    pct = f"{summary['percent']}%" if summary.get("percent") is not None else ""
    layer = ""
    if summary.get("layer") is not None and summary.get("total_layers"):
        layer = f"{summary['layer']}/{summary['total_layers']}"
    minutes = summary.get("remaining_min")
    left = ""
    finish = ""
    state = summary.get("state")

    if summary.get("connected") is False:
        status = "Offline"
    elif state == "RUNNING":
        left = time_left(minutes) or ""
        clock = finish_clock(minutes, datetime.datetime.now())
        finish = f"done ~{clock}" if clock else ""
        status = " · ".join(p for p in ["Printing" + (f" {pct}" if pct else ""), left] if p)
    elif state == "PAUSE":
        status = f"Paused at {pct}" if pct else "Paused"
    elif state == "PREPARE":
        status = "Getting ready"
    elif state == "FINISH":
        status = "Finished"
    elif state == "FAILED":
        status = "Failed or canceled"
    elif state == "IDLE":
        status = "Idle"
    else:
        status = state.title() if state else "Status unknown"
    return {"printer": name, "status": status, "percent": pct, "layer": layer,
            "time_left": left, "finish": finish, "job": summary.get("job") or ""}
