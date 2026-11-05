"""Shared test setup: an isolated home directory and a fake deck, so tests never touch
your real config, tokens or Stream Deck. Import this before anything from core/."""
import atexit
import logging
import os
import shutil
import socket
import sys
import tempfile

if __name__ != "tests.helpers":
    # Imported twice under two names, this module would build a second sandbox halfway through
    # test collection and earlier tests would look in the wrong place.
    raise ImportError("import this as `from tests import helpers` / `from tests.helpers import ...`")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(ROOT, "config.example.yaml")

SANDBOX = tempfile.mkdtemp(prefix="streamdeck-test-")
atexit.register(shutil.rmtree, SANDBOX, ignore_errors=True)
os.environ.update({
    "HOME": SANDBOX,
    "XDG_CONFIG_HOME": os.path.join(SANDBOX, ".config"),
    "XDG_DATA_HOME": os.path.join(SANDBOX, ".local", "share"),
    "XDG_STATE_HOME": os.path.join(SANDBOX, ".local", "state"),
    "DECK_STATE_DIR": os.path.join(SANDBOX, "state"),
})
for var in ("DECK_CONFIG", "TWITCH_CLIENT_ID", "TWITCH_CHANNEL", "HA_URL", "HA_TOKEN",
            "OBS_WEBSOCKET_PASSWORD", "OBS_CONFIG_DIR"):
    os.environ.pop(var, None)
sys.path[:0] = [os.path.join(ROOT, "vendor"), ROOT]

from core.models import FakeDeck  # noqa: E402

logging.basicConfig(level=logging.CRITICAL if not os.environ.get("TEST_LOG") else logging.INFO)


class Deck(FakeDeck):
    """A FakeDeck that accepts drawing calls and throws them away.
    Deck(model, serial=...) gives it a serial (default: derived from the model)."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def set_key_image(self, *a):
        pass

    def set_touchscreen_image(self, *a, **kw):
        pass

    def set_brightness(self, *a):
        pass

    def set_screen_image(self, *a):
        pass


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def tmpdir():
    return tempfile.mkdtemp(dir=SANDBOX)
