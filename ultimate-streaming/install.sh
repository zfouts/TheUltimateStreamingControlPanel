#!/bin/sh
# Install the Stream Deck daemon for the current user. Never uses sudo; if something
# needs root (system packages, the udev rule), it prints the exact command instead.
#
#   ./install.sh               fetch deps, create config, install + start the user service
#   ./install.sh --no-service  everything except the systemd service
#   ./install.sh --uninstall   stop and remove the service, desktop entry and old-name link; keeps
#                              your data and lists where it is (config + tokens, backups, plugin deps)
#   ./install.sh --purge       --uninstall, then also delete that data (asks you to type "yes";
#                              add --yes to skip the question). Refuses paths outside your home.
set -eu

ROOT=$(cd "$(dirname "$0")" && pwd)
PYTHON=${PYTHON:-$(command -v python3 || true)}
CONF_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/ultimate-streaming
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
SERVICE=yes
UNINSTALL=no
PURGE=no
ASSUME_YES=no
CONFIG_HOME=${XDG_CONFIG_HOME:-$HOME/.config}
# Backups of external apps (they hold the OBS stream key and websocket password) and uploads.
STATE_DIR=${XDG_STATE_HOME:-$HOME/.local/state}/ultimate-streaming
# Installed plugins' packages (plugin-vendor) and the plugin lock.
DATA_DIR=${XDG_DATA_HOME:-$HOME/.local/share}/ultimate-streaming

# Escape & | \ for sed in case a path contains them.
esc() { printf '%s' "$1" | sed 's/[&|\\]/\\&/g'; }
say()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m!!  %s\033[0m\n' "$*"; }

# Before the rename this was the "streamdeck" service with settings in ~/.config/streamdeck.
retire_old_names() {
  OLD_UNIT="$UNIT_DIR/streamdeck.service"
  if [ -e "$OLD_UNIT" ] && grep -q 'main.py' "$OLD_UNIT"; then
    if command -v systemctl >/dev/null; then
      systemctl --user disable --now streamdeck.service 2>/dev/null || true
    fi
    rm -f "$OLD_UNIT"
    echo "    removed the old \"streamdeck\" service (now \"ultimate-streaming\")"
  fi
  rm -f "${XDG_DATA_HOME:-$HOME/.local/share}/applications/streamdeck-editor.desktop"
}

for arg in "$@"; do
  case $arg in
    --no-service) SERVICE=no ;;
    --uninstall) UNINSTALL=yes ;;
    --purge) UNINSTALL=yes; PURGE=yes ;;
    --yes) ASSUME_YES=yes ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done
[ "$ASSUME_YES" = no ] || [ "$PURGE" = yes ] || { echo "--yes only goes with --purge." >&2; exit 2; }

# Print the physical path of an existing directory (symlinks resolved), or nothing.
real_dir() { (cd -P "$1" 2>/dev/null && pwd -P) || true; }

# Succeed only for a directory that is strictly inside $HOME once symlinks are resolved.
safe_to_delete() {
  case $1 in /*) ;; *) return 1 ;; esac
  home=$(real_dir "$HOME")
  dir=$(real_dir "$1")
  [ -n "$home" ] && [ "$home" != / ] && [ -n "$dir" ] || return 1
  case $dir in "$home"/?*) return 0 ;; *) return 1 ;; esac
}

uninstall() {
  if command -v systemctl >/dev/null; then
    systemctl --user disable --now ultimate-streaming.service 2>/dev/null || true
  fi
  rm -f "$UNIT_DIR/ultimate-streaming.service" "${XDG_DATA_HOME:-$HOME/.local/share}/applications/ultimate-streaming.desktop"
  retire_old_names
  # The link the rename left at the old settings path; only ours (pointing at CONF_DIR) is removed.
  if [ -L "$CONFIG_HOME/streamdeck" ] && [ "$(readlink "$CONFIG_HOME/streamdeck")" = "$CONF_DIR" ]; then
    rm -f "$CONFIG_HOME/streamdeck"
    echo "    removed the old-name link $CONFIG_HOME/streamdeck"
  fi
  if command -v systemctl >/dev/null; then systemctl --user daemon-reload || true; fi
  say "Service removed. Your data is still on disk:"
  echo "    $CONF_DIR"
  echo "        config.yaml, Twitch and editor tokens, Discord/webhook secrets, overlay text"
  echo "    $STATE_DIR"
  echo "        backups of OBS and other apps (they include your stream key and obs-websocket password), uploads"
  echo "    $DATA_DIR"
  echo "        installed plugins and their packages (plugin-vendor), .plugins.lock"
  echo "    Delete all of it with:  ./install.sh --purge    (or remove those folders yourself)"
}

purge() {
  for d in "$CONF_DIR" "$STATE_DIR" "$DATA_DIR"; do
    [ -e "$d" ] || continue
    safe_to_delete "$d" || { echo "Refusing to delete $d: it is empty, relative, or not inside $HOME." >&2; exit 1; }
  done
  say "--purge deletes the folders listed above, for good."
  if [ "$ASSUME_YES" != yes ]; then
    printf 'Type yes to delete them: '
    answer=
    read -r answer || true
    [ "$answer" = yes ] || { echo "Nothing deleted."; exit 1; }
  fi
  for d in "$CONF_DIR" "$STATE_DIR" "$DATA_DIR"; do
    [ -e "$d" ] || continue
    if [ -L "$d" ]; then
      # A link (e.g. into a dotfiles repo): remove the link, never what it points at.
      echo "    removed the link $d (what it points at, $(real_dir "$d"), is left alone)"
      rm -f "$d"
    else
      rm -rf "$d"
      echo "    deleted $d"
    fi
  done
  say "Purged."
}

if [ "$UNINSTALL" = yes ]; then
  uninstall
  [ "$PURGE" = no ] || purge
  exit 0
fi

[ "$(id -u)" -ne 0 ] || { echo "Run this as your normal user, not root." >&2; exit 1; }
[ -n "$PYTHON" ] || { echo "python3 not found; see README → Prerequisites." >&2; exit 1; }
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 14))' \
  || { echo "Python 3.14+ required (pyenv install 3.14) (found $("$PYTHON" --version 2>&1))." >&2; exit 1; }

say "Python packages (pinned + hash-checked) → $ROOT/vendor"
"$PYTHON" "$ROOT/tools/fetch_deps.py"

# A git pull can change a plugin's pins, so rebuild their shared packages too.
PLUGINS=${XDG_DATA_HOME:-$HOME/.local/share}/ultimate-streaming/plugins
if [ -d "$PLUGINS" ] && [ -n "$(find "$PLUGINS" -mindepth 1 -maxdepth 1 ! -name '.*' 2>/dev/null)" ]; then
  say "Plugin packages (pinned + hash-checked)"
  "$PYTHON" "$ROOT/tools/plugin.py" deps \
    || warn "plugin packages were not rebuilt (see above); the app itself still updates. Retry: $PYTHON $ROOT/tools/plugin.py deps"
fi

say "Config"
retire_old_names
if command -v systemctl >/dev/null; then systemctl --user daemon-reload 2>/dev/null || true; fi
APP_ROOT="$ROOT" "$PYTHON" -c "import os, sys; sys.path.insert(0, os.environ['APP_ROOT']); from core.paths import migrate_legacy; m = migrate_legacy(); m and print('    moved your settings to', m, '(the old folder links there)')"
mkdir -p "$CONF_DIR"
chmod 700 "$CONF_DIR"
if [ -e "$CONF_DIR/config.yaml" ]; then
  echo "    keeping existing $CONF_DIR/config.yaml"
else
  cp "$ROOT/config.example.yaml" "$CONF_DIR/config.yaml"
  chmod 600 "$CONF_DIR/config.yaml"
  echo "    created $CONF_DIR/config.yaml (set twitch.channel and twitch.client_id; see README)"
fi

if [ "$SERVICE" = yes ]; then
  if command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
    say "systemd user service"
    mkdir -p "$UNIT_DIR"
    sed -e "s|@ROOT@|$(esc "$ROOT")|g" -e "s|@PYTHON@|$(esc "$PYTHON")|g" \
      "$ROOT/packaging/ultimate-streaming.service.in" > "$UNIT_DIR/ultimate-streaming.service"
    systemctl --user import-environment DISPLAY XAUTHORITY WAYLAND_DISPLAY 2>/dev/null || true
    systemctl --user daemon-reload
    systemctl --user enable ultimate-streaming.service >/dev/null
    systemctl --user restart ultimate-streaming.service
    echo "    running; logs: journalctl --user -u ultimate-streaming -f"
  else
    warn "systemd user session not available; start it yourself with: $PYTHON $ROOT/main.py"
  fi
fi

say "App-menu entry for the editor"
APPS=${XDG_DATA_HOME:-$HOME/.local/share}/applications
mkdir -p "$APPS"
sed -e "s|@ROOT@|$(esc "$ROOT")|g" -e "s|@PYTHON@|$(esc "$PYTHON")|g" \
  "$ROOT/packaging/ultimate-streaming.desktop.in" > "$APPS/ultimate-streaming.desktop"
echo "    \"The Ultimate Streaming Control Panel for Linux\" added to your applications menu"

say "Checking prerequisites"
"$PYTHON" "$ROOT/tools/doctor.py" || true

# Only suggest the udev rule if a plugged-in deck isn't accessible.
for f in /sys/class/hidraw/hidraw*/device/uevent; do
  [ -e "$f" ] || continue
  if grep -qi ':00000FD9:' "$f"; then
    node=/dev/$(basename "$(dirname "$(dirname "$f")")")
    if [ ! -r "$node" ] || [ ! -w "$node" ]; then
      warn "No permission for the Stream Deck ($node). Run once (needs root), then re-plug the deck:"
      echo "    sudo cp '$ROOT/packaging/70-streamdeck.rules' /etc/udev/rules.d/ && sudo udevadm control --reload && sudo udevadm trigger"
      break
    fi
  fi
done

say "Done."
echo "    Open the editor from your app menu (\"The Ultimate Streaming Control Panel for Linux\") or run:  $PYTHON $ROOT/main.py --open-editor"
echo "    Its Settings page walks you through OBS, and its Twitch page links your account."
