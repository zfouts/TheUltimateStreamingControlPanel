"""Where things live on disk. Nothing user-specific is hardcoded: everything derives
from $HOME / the XDG base-directory variables, and can be overridden.

    DECK_CONFIG          path to config.yaml   (default: $XDG_CONFIG_HOME/ultimate-streaming/config.yaml)
    DECK_STATE_DIR       tokens, overlay text files, radio state (default: ~/.config/ultimate-streaming)
    OBS_CONFIG_DIR       OBS's config dir      (default: native OBS, then Flatpak OBS)
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _xdg(var, fallback):
    return os.environ.get(var) or os.path.expanduser(fallback)


CONFIG_HOME = _xdg("XDG_CONFIG_HOME", "~/.config")
APP = "ultimate-streaming"
LEGACY_APP = "streamdeck"   # the folder name before the rename; migrated by migrate_legacy()
DEFAULT_CONFIG = os.path.join(CONFIG_HOME, APP, "config.yaml")


def migrate_legacy():
    """Move ~/.config/streamdeck (older versions) to ~/.config/ultimate-streaming, leaving a
    link at the old path: OBS text sources and hand-written paths in config.yaml keep working.
    Does nothing when $DECK_STATE_DIR is set, the new folder exists, or there's nothing to move.
    Returns the new path if it moved something."""
    if os.environ.get("DECK_STATE_DIR"):
        return None
    old, new = os.path.join(CONFIG_HOME, LEGACY_APP), os.path.join(CONFIG_HOME, APP)
    if os.path.exists(new) or os.path.islink(old) or not os.path.isdir(old):
        return None
    os.rename(old, new)           # same filesystem: atomic, keeps permissions
    os.symlink(new, old)
    return new


def config_path():
    """$DECK_CONFIG, else ~/.config/ultimate-streaming/config.yaml, else ./config.yaml (development)."""
    if os.environ.get("DECK_CONFIG"):
        return os.path.expanduser(os.environ["DECK_CONFIG"])
    if os.path.exists(DEFAULT_CONFIG) or not os.path.exists(os.path.join(ROOT, "config.yaml")):
        return DEFAULT_CONFIG
    return os.path.join(ROOT, "config.yaml")


def state_dir():
    return os.path.expanduser(os.environ.get("DECK_STATE_DIR") or os.path.join(CONFIG_HOME, APP))


def state_file(name):
    """A file in the state dir (created on demand, private to the user)."""
    d = state_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    return os.path.join(d, name)


def data_dir():
    """$XDG_DATA_HOME/ultimate-streaming (default ~/.local/share/ultimate-streaming), created 0700.

    For installed code (plugins) and their dependencies: kept out of ~/.config, which is often
    synced as dotfiles, so plugin code never follows a config sync onto another machine."""
    path = os.path.join(_xdg("XDG_DATA_HOME", "~/.local/share"), APP)
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def plugins_dir():
    """Installed plugins: one entry per plugin, a symlink into the repo's plugins/ catalog or a
    copied folder (see core/plugins.py). Created on demand."""
    path = os.path.join(data_dir(), "plugins")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def plugin_vendor_dir():
    """The one shared folder holding every installed plugin's pinned dependencies (not created here)."""
    return os.path.join(data_dir(), "plugin-vendor")


def overlay_dir():
    """Text files OBS text sources read (title, countdown, chat, ...)."""
    return os.path.join(state_dir(), "overlay")


OBS_CONFIG_CANDIDATES = [
    os.path.join(CONFIG_HOME, "obs-studio"),
    os.path.expanduser("~/.var/app/com.obsproject.Studio/config/obs-studio"),  # Flatpak
]


def obs_config_dir():
    if os.environ.get("OBS_CONFIG_DIR"):
        return os.path.expanduser(os.environ["OBS_CONFIG_DIR"])
    for d in OBS_CONFIG_CANDIDATES:
        if os.path.isdir(d):
            return d
    return OBS_CONFIG_CANDIDATES[0]


def obs_websocket_config():
    return os.path.join(obs_config_dir(), "plugin_config", "obs-websocket", "config.json")
