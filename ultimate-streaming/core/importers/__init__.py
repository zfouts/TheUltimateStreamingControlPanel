"""Importers for other Stream Deck programs' setups.

KINDS maps a kind to `read(data: bytes, *, preview=None) -> setup dict` (see _common's
docstring for the shape). With a preview token, icons are written to a preview folder: call
_common.commit_preview_icons(config, token) on apply and cleanup_preview_icons() periodically.
Each reader treats its input as hostile: bounded parsing through _common (SafeZip, safe_json,
import_image), dict actions only, anything that isn't harmless wrapped in `needs_review` by
safe_action, unconvertible keys marked `needs_action`, and ImportError_ messages that never
quote file content. `_common.to_config(setup) -> (config, info)` turns the result into an
incoming config for core.importer.plan / apply (preview first, non-destructive merge) plus the
detected model/grid (info; not part of the config).
"""
from . import boatswain, elgato, streamcontroller, streamdeck_ui
from ._common import ImportError_, to_config

KINDS = {"elgato": elgato.read, "streamcontroller": streamcontroller.read,
         "streamdeck_ui": streamdeck_ui.read, "boatswain": boatswain.read}

__all__ = ["KINDS", "ImportError_", "to_config"]
