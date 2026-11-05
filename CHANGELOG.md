# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-11-05

### Security

- The Bambu printer camera reaches OBS through a private Unix socket (in a 0700 folder under
  `$XDG_RUNTIME_DIR`) instead of plain localhost UDP, so other users on the computer can no longer
  inject video into your live OBS source or watch the camera. Flatpak OBS, which can't see that
  folder, still uses UDP and the plugin page says so; an unsafe socket folder keeps the camera off
  instead of falling back. The plugin restarts OBS's camera source whenever the relay starts
  listening, so a reload or restart doesn't leave it blank.
- Importing someone else's config.yaml now also drops key `icon` and `image` values that could name
  any file on your computer, and the settings that name a program to run (`bambu.camera.ffmpeg`) or
  would switch the camera back to UDP (`bambu.camera.transport`). A relative `image` is looked up in
  the app's icon folder, like `icon`, not in your home folder.
- All viewer and network text goes through one shared cleaner (`core/text.py`); ANSI color codes
  and invisible surrogates are now removed everywhere they weren't.

### Added

- The panel warns (amber banner) when a `config.yaml` you saved while the daemon was running has an
  error; the deck keeps running on the last good config.
- Restoring an OBS backup into a different OBS install (system vs Flatpak) asks first.

### Fixed

- Twitch bot: a reward action that never finishes no longer blocks every later redemption; quick
  on/off toggles can't leave rewards paused while the bot is on; a failing revert no longer stops
  later reverts.
- Reloading a plugin (automatic when its files change) no longer pauses the deck for up to 10 s.
- BRB and radio: a `credits.yaml` that isn't a mapping no longer breaks the BRB screen or the radio;
  clearing a poll no longer writes the overlay file while holding the module lock.
- The dev server can open a broken config.yaml.

### Changed

- CI runs on every change and tests a full install, uninstall and purge.
- The Twitch bot's configuration code lives in `modules/_twitch_bot_config.py` and the setup routes
  are split by area; no behavior change.
- Panel: plain-language explanation of OBS's WebSocket server, fuller keyboard focus trap in
  dialogs, visible focus rings on menu items.

### Known issues

- Secrets (tokens, keys) still live under `~/.config`, which many people sync as dotfiles.

## [0.1.0] - 2026-11-03

First public release of the Ultimate Streaming Control Panel for Linux.

### Added

- Stream Deck daemon for every Elgato model (Mini, Original / MK.2, Neo, XL, +, + XL, Studio,
  Pedal), several decks at once, with dials and touch strips on the + models.
- Web control panel on localhost: go live, switch scenes, mix audio, read and moderate chat,
  link Twitch, and edit keys and pages with dropdowns of your OBS scenes, audio sources and
  Steam games. The same config is a single YAML file.
- OBS control (streaming, recording, replay buffer, scene keys, mute toggles, stream health and
  uptime on the keys) and a builder for a matching OBS profile and scene set: Starting Soon,
  Screen, Game, Camera, Ending and an interactive BRB screen.
- BRB screen: chat feed, viewer polls and a guessing game, a hype meter, your own videos with
  credits, a "back in" timer and a radio music source.
- Twitch: clips, markers, ads, chat modes, viewer count, title / category / tags; a chat bot
  with timers, commands, channel-point rewards and chat stats; alerts for follows, subs, cheers
  and raids, with thank-yous and an auto-shoutout after a raid.
- Go-live announcements and Discord webhooks.
- Desktop actions: media keys, volume and mic, apps, key combos and push-to-talk, Steam games
  with cover art, Home Assistant.
- Onboarding for existing setups: backups of OBS and other deck apps before anything is
  changed, import of other deck apps' configs with per-key review, and stopping a competing
  program.
- Plugins, installed from a catalog with hash-pinned dependencies, and a Bambu Lab printer
  plugin (status keys, camera snapshots with the printer's certificate pinned on first use, an
  OBS scene, and a crop that keeps the room off stream).
- `install.sh --uninstall` removes the service and lists the folders that still hold data
  (config and tokens, backups with the OBS stream key, plugin packages); `--purge` deletes
  them after confirmation (`--yes` skips the question).
- `tools/devserver.py` runs the panel against a fake deck for development.

### Security

- The panel binds to 127.0.0.1 and requires an access token. The token never goes in a URL or
  on a command line: links carry a single-use launch code (60 s) that the page trades for the
  token, only over this user's own sockets. The token is replaced every time the daemon starts,
  and "Sign out all panels" replaces it on demand. The panel's address is only handed out when
  the listener on its port is this user's.
- Importing someone else's config.yaml can't run anything: every action, state and display
  outside a short deck/OBS/overlay allow-list arrives switched off until you review it, and
  settings that point at files, servers or commands are dropped and listed.
- Secret files can't be read through another setting (e.g. an `ha.token_file` pointing at the
  Twitch token), and commands with a timeout are killed with their whole process group.
- Viewer chat text is cleaned (control, bidirectional and zero-width characters, zalgo) before
  it reaches the OBS overlay.
- The Bambu printer's status connection (MQTT) pins the printer's certificate on first use,
  like the camera, instead of skipping certificate checks.
- Python dependencies are pinned and hash-checked; atomic 0600 writes for secrets.
- The dev server's dry run is confined to a throw-away folder: XDG state, data and config
  folders point into the sandbox, setup restores are refused, and state providers that run
  commands or call the network (`cmd`, `audio`, `media`, `ha`, `twitch_chat`) are only logged,
  unless `--live`.

### Reliability

- A bad key setting (e.g. a color without its `#`) falls back to the default instead of killing
  the deck's key input; a failing or slow state, display or integration (Home Assistant, media
  players, Twitch) is backed off and can't freeze the deck or the on-screen timers.
- A broken or missing `config.yaml` at startup no longer crash-loops the service: the daemon runs
  on the newest backup that loads, the panel says so, and saving a fixed config clears it.
- A revoked Twitch login is no longer retried every minute, and a brief Twitch outage no longer
  asks you to re-link.
- Interrupted OBS restores and backups clean up after themselves and are reported in the panel;
  labeled config revisions are kept; a symlinked `config.yaml` stays a symlink.
- The Bambu camera recovers from a stalled stream, follows serial and port changes, and the OBS
  source follows a port change.

### Known issues

- Secrets (tokens, keys) live under `~/.config`, which many people sync as dotfiles.
- The Bambu camera relay uses unauthenticated localhost UDP (fixed in the next release).
- The Twitch bot module is large and due for a split.
- A typo saved to `config.yaml` while the daemon is running is logged and the old config kept,
  but the panel doesn't show a warning until the next start.
