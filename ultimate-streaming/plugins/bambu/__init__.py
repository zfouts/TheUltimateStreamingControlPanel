"""Bambu Lab printer plugin: print status on the deck, in OBS and in the panel; the printer's
camera in OBS; Discord snapshots while printing; pause / resume / stop / light controls; and a
`print` built-in for the Twitch Bot. See README.md in this folder.

Installed, it is imported as deck_plugins.bambu (core/bot.py). The Module lives in module.py and
is imported only from setup(), so tests can import printer.py, camera.py etc. on their own.
"""


def setup(bot):
    from .module import setup as _setup
    _setup(bot)
