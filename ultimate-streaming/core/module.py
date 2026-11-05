"""Base class and decorators for deck modules — the equivalent of discord.py cogs.

A module file in modules/ defines one or more Module subclasses and a
`setup(bot)` function that registers them:

    from core import Module, action, state

    class Lights(Module):
        @action("lamp")                 # config: action: {lamp: toggle}
        def lamp(self, arg): ...

        @state("lamp")                  # config: state: {lamp: on}
        def lamp_on(self, arg, spec): return True

    def setup(bot):
        bot.add_module(Lights(bot))
"""
import logging

# Actions that a Twitch channel-point reward may never run, not even with allow_unsafe_actions
# (e.g. stopping a 3D print). Filled by @action(..., rewards=False); names stay listed after
# their module unloads, which only ever refuses more.
REWARD_DENIED = set()


def action(name, *, on_release=False, ui=None, rewards=True):
    """Handle `action: {name: arg}`. Called as fn(arg) on press, in a worker thread.

    With on_release=True it is called synchronously as fn(arg, down) on both
    press and release instead (for things held down, like push-to-talk).
    `ui` describes the action for the web editor (see core/schema.py).
    rewards=False: never allowed in a Twitch Bot reward (destructive or physical actions).
    """
    if not rewards:
        REWARD_DENIED.add(name)

    def deco(fn):
        fn.__deck_action__ = (name, on_release)
        fn.__deck_ui__ = ui
        return fn
    return deco


def state(name, *, ui=None):
    """Handle `state: {name: arg, ...}`. Called as fn(arg, spec); return True for "active"."""
    def deco(fn):
        fn.__deck_state__ = name
        fn.__deck_ui__ = ui
        return fn
    return deco


def display(name, *, ui=None):
    """Handle `display: {source: name, ...}`. Called as fn(spec); return a dict of
    appearance overrides (label, bg, icon, ...), or {} to show the key's normal look."""
    def deco(fn):
        fn.__deck_display__ = name
        fn.__deck_ui__ = ui
        return fn
    return deco


def expander(name, *, ui=None):
    """Turn placeholder buttons like `{name: arg, ...}` into real buttons.
    Called as fn(arg, rest_of_button); return a button dict, or None for a blank key."""
    def deco(fn):
        fn.__deck_expander__ = name
        fn.__deck_ui__ = ui
        return fn
    return deco


def source(name):
    """Live choices for the web editor's dropdowns (e.g. OBS scene names).
    Called as fn() from a web request thread; return a list of strings or
    {"value": ..., "label": ...} dicts. Raise if unavailable (e.g. OBS not running)."""
    def deco(fn):
        fn.__deck_source__ = name
        return fn
    return deco


def listener(event):
    """Receive events another module broadcasts with bot.dispatch(event, ...), like
    discord.py's Cog.listener. Called as fn(*args) on the dispatching thread."""
    def deco(fn):
        fn.__deck_listener__ = event
        return fn
    return deco


def task(every, on_page_change=True):
    """Run fn() on the bot's main loop every `every` seconds, and right after a deck page
    change unless on_page_change=False (for slow, rarely-needed work)."""
    def deco(fn):
        fn.__deck_task__ = float(every)
        fn.__deck_task_on_page__ = on_page_change
        return fn
    return deco


class Module:
    """Base class for deck modules.

    `config` is this module's section of config.yaml, keyed by `config_key`
    (default: the module file's name, e.g. modules/obs.py → `obs:`).

    A plugin's module can also have its own page in the control panel: see panel_page()
    and panel_action().
    """

    config_key = None

    def __init__(self, bot):
        self.bot = bot
        self.config = {}
        self.log = logging.getLogger(f"deck.{type(self).__name__}")

    def on_config(self, config):
        """Called with this module's config section on load and whenever config.yaml changes."""
        self.config = config

    def on_load(self):
        """Called once after the module is registered (after the first on_config)."""

    def on_unload(self):
        """Called before the module is removed (reload or shutdown): release resources here."""

    def snapshot(self):
        """Live state for the web control panel, or None if there is nothing to show.

        Called every 2 s from the editor's status task and sent to every open browser
        tab (as the "status" event, keyed by this module's config_key) whenever it
        changes. Must be fast and must not touch the network: return cached fields.
        Only JSON-serialisable values; never tokens or secrets."""
        return None

    def panel_page(self):
        """This module's own page in the control panel (plugins only), or None for no page.

        Return a dict describing it; the panel draws it, so a plugin ships no HTML or JS.
        core/plugin_page.py checks it (an invalid one is logged once and shows no page):

            {"title": "My Printer",            # sidebar label and heading, 1-24 characters
             "icon": "layers",                 # a panel icon name (plugin_page.ICONS), else "bolt"
             "intro": "One sentence.",         # optional, at most 200 characters
             "actions": [                      # at most 12 buttons
                 {"id": "pause", "label": "Pause", "icon": "pause", "tone": "default",  # or primary, danger
                  "confirm": None,             # text: the panel asks first
                  "enabled": True, "hint": None}],   # hint: tooltip, e.g. why it's disabled
             "fields": [                       # at most 40 settings, grouped by "group"
                 {"key": "host", "label": "IP address", "type": "text", "group": "Printer",
                  "required": True, "max_length": 200, "help": None, "placeholder": None},
                 {"key": "camera.port", "label": "Port", "type": "number", "group": "Camera",
                  "min": 1024, "max": 65535, "whole": True, "default": 5004},
                 {"key": "camera.enabled", "label": "Camera", "type": "bool", "default": True},
                 {"key": "mode", "label": "Mode", "type": "select", "options": [["a", "A"], ["b", "B"]]},
                 {"key": "code", "label": "Access code", "type": "secret", "required": True,
                  "file": "my_code",           # saved to core.paths.state_file("my_code"), 0600
                  "config_key": "code_file",   # set to that file's path in this module's section
                  "env": "MY_CODE"}]}          # optional: overrides the file (read_secret)

        Field keys are dotted paths inside this module's config section. The panel saves the
        form into config.yaml itself and the daemon reloads it (on_config). Called on a web
        request thread whenever the page or /api/status is fetched: return cached state only,
        quickly, and never a secret."""
        return None

    def panel_action(self, action_id):
        """Run the page button `action_id` (one of panel_page()'s actions, enabled; the panel
        has already asked any `confirm` question). Called on a web request thread: be quick and
        queue slow work. Raise core.webapi.ApiError(message) for a failure the user should see;
        return None or {"message": "..."} (shown as a toast)."""
        from .webapi import ApiError   # here: core.webapi isn't needed by modules without a page
        raise ApiError("no such action", 404)
