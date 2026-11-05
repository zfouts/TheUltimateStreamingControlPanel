"""Chat command built-ins that other modules and plugins add to the Twitch Bot.

The Twitch Bot's own built-ins (stats, top10, uptime, commands) live in modules/twitch_bot.py.
A module adds another kind, which the user then picks for a command in the panel or config:

    commands:
      - {trigger: "!print", builtin: print, response: "{printer}: {status}"}

    from core import chatcmd

    def on_load(self):
        chatcmd.register(self.bot, chatcmd.Builtin(
            kind="print", label="3D print status", help="Says what the printer is doing.",
            placeholders=frozenset({"printer", "status", "percent"}),
            default_response="{printer}: {status}",
            values=self.print_values,                    # fn(arg) -> {placeholder: text}
            sample={"printer": "Bob", "status": "Printing 58%", "percent": "58%"}))

    def on_unload(self):
        chatcmd.unregister(self.bot, "print", owner=self)

`values` is called from the Twitch Bot's worker thread with the viewer's argument (text after
the trigger; it may only select what is looked up, never be echoed). It must be quick and must
not touch the network: return cached state. Raise to make the bot stay silent for that use;
the panel's Test then shows `sample` instead. Returned values are untrusted text: the bot
cleans every one (clean_value) before filling the response.

Registering or unregistering dispatches "chat_builtins_changed" so the Twitch Bot re-reads its
config: a command whose built-in was missing (plugin not loaded yet) is skipped until it appears.
The registry lives here, not in the Twitch Bot, so it survives that module being reloaded.
"""
import re
import threading
from dataclasses import dataclass, field
from typing import Callable

KIND_RE = re.compile(r"[a-z][a-z0-9_]{1,23}\Z")
PLACEHOLDER_RE = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
CORE_KINDS = frozenset({"stats", "top10", "uptime", "commands", "text"})
MAX_BUILTINS = 32

_lock = threading.Lock()
_registry = {}


@dataclass(frozen=True)
class Builtin:
    kind: str
    label: str                                  # shown in the panel's command editor
    help: str                                   # one sentence for the panel
    placeholders: frozenset                     # {name} values the response may use (besides channel/user)
    default_response: str
    values: Callable[[str], dict] = field(compare=False)
    sample: dict = field(default_factory=dict, compare=False)


def register(bot, builtin):
    """Add (or replace, for the same kind) a built-in. ValueError for an invalid one."""
    if not isinstance(builtin, Builtin) or not KIND_RE.match(builtin.kind or ""):
        raise ValueError("built-in kind must be 2-24 lowercase letters, digits or _")
    if builtin.kind in CORE_KINDS:
        raise ValueError(f"{builtin.kind!r} is one of the Twitch Bot's own built-ins")
    if not all(isinstance(p, str) and PLACEHOLDER_RE.match(p) for p in builtin.placeholders):
        raise ValueError("placeholders must be lowercase names")
    for text, cap in ((builtin.label, 60), (builtin.help, 200), (builtin.default_response, 500)):
        if not isinstance(text, str) or not text or len(text) > cap:
            raise ValueError("label, help and default_response must be short text")
    if not callable(builtin.values):
        raise ValueError("values must be callable")
    with _lock:
        current = _registry.get(builtin.kind)
        if current is not None and not _same_owner(current, builtin):
            raise ValueError(f"the chat built-in {builtin.kind!r} is already provided by "
                             f"{_owner_name(current)}")
        if current is None and len(_registry) >= MAX_BUILTINS:
            raise ValueError("too many chat built-ins")
        _registry[builtin.kind] = builtin
    if bot is not None:
        bot.dispatch("chat_builtins_changed")


def _owner(builtin):
    """The object whose bound method `values` is (a module instance), or None for a plain function."""
    return getattr(builtin.values, "__self__", None)


def _same_owner(a, b):
    """Re-registering is allowed for the same owner, or the same class (a reloaded module's new
    instance replacing the old one's registration)."""
    oa, ob = _owner(a), _owner(b)
    if oa is None or ob is None:
        return oa is ob
    return oa is ob or (type(oa).__qualname__ == type(ob).__qualname__
                        and type(oa).__module__ == type(ob).__module__)


def _owner_name(builtin):
    owner = _owner(builtin)
    return type(owner).__name__ if owner is not None else "another module"


def unregister(bot, kind, owner=None):
    """Remove `kind`. With `owner` (the registering module instance), only if it still owns it,
    so an old instance unloading can't remove its replacement's registration."""
    with _lock:
        current = _registry.get(kind)
        if current is None or (owner is not None and _owner(current) is not owner):
            return
        del _registry[kind]
    if bot is not None:
        bot.dispatch("chat_builtins_changed")


def unregister_owned(bot, instances):
    """Remove every built-in registered by one of `instances` (a plugin being rolled back or unloaded)."""
    ids = {id(i) for i in instances}
    with _lock:
        kinds = [k for k, b in _registry.items() if id(_owner(b)) in ids and _owner(b) is not None]
        for k in kinds:
            del _registry[k]
    if kinds and bot is not None:
        bot.dispatch("chat_builtins_changed")


def get(kind):
    with _lock:
        return _registry.get(kind)


def all_builtins():
    """{kind: Builtin}, a copy."""
    with _lock:
        return dict(_registry)


def describe():
    """JSON for the panel: {kind: {"label", "help", "placeholders": [...], "default_response"}}."""
    return {k: {"label": b.label, "help": b.help, "placeholders": sorted(b.placeholders),
                "default_response": b.default_response}
            for k, b in sorted(all_builtins().items())}
