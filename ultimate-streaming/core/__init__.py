__version__ = "0.2.0"

from .module import Module, action, display, expander, listener, source, state, task
from .util import notify, open_url, sh, spawn

__all__ = ["DeckBot", "Module", "action", "display", "expander", "listener", "source", "state", "task",
           "notify", "open_url", "sh", "spawn"]


def __getattr__(name):
    # Imported lazily so tools (doctor, OBS builder) can use core.paths / core.util
    # without the StreamDeck library installed.
    if name == "DeckBot":
        from .bot import DeckBot
        return DeckBot
    raise AttributeError(name)
