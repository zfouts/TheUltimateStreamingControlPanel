# Bambu Lab printers

A plugin for the Ultimate Streaming Control Panel. It shows your printer's progress on a deck
key and in the control panel, puts the printer's camera into OBS, posts progress pictures to
Discord, answers `!print` in chat, and gives you keys to pause, resume and stop the print and
switch the light.

It was built and tested against a Bambu Lab P2S. Other Bambu printers that report over the same
local MQTT connection should work, but that isn't promised.

## What you need

- The printer and your computer on the same network.
- ffmpeg 5.1 or newer, for the camera only. Check with `ffmpeg -version`.
- OBS, for the camera scene and for snapshots.
- A Discord webhook, if you want Discord posts. Optional.

## Install

```sh
python3 tools/plugin.py install bambu
```

The running app picks it up within about a second. To check: `python3 tools/plugin.py list`.
The general rules (where plugins live, updates, removing) are in the main README under
[Plugins](../../README.md#plugins).

## Set up

The easy way: open the control panel, pick **Bambu Printer** under Plugins in the sidebar, and
fill in the IP address, serial number and access code. The panel saves the access code to a
private file (`~/.config/ultimate-streaming/bambu_access_code`, mode 600) and the rest to
`config.yaml`. The same page has the camera, Discord and print-end settings, the live status,
and buttons for pause, resume, stop (it asks first, then stops at once), the light, the camera
and OBS. To do it by hand instead:

**1. Find three things on the printer's screen** (LAN / network settings):

- the IP address (`host`)
- the serial number (`serial`)
- the access code (the LAN access code)

If the printer refuses commands or the connection later, see
[Refused](#the-printer-refuses-the-connection-or-commands) below: some firmware needs LAN Only
mode and Developer mode turned on.

**2. Save the access code to a file** that only you can read:

```sh
umask 077; printf '%s' 'CODE' > ~/.config/ultimate-streaming/bambu_access_code
```

Replace `CODE` with your access code. Status comes from the printer's local MQTT (TLS, port
8883, user `bblp`, the access code as the password).

**3. Add a `bambu:` section to `config.yaml`:**

```yaml
bambu:
  host: 192.168.1.50
  serial: 01P00A123456789
  access_code_file: ~/.config/ultimate-streaming/bambu_access_code
  name: Printer
```

**4. Add keys** (see [Keys](#keys)). Until host, serial and access code are all set, the plugin
loads but says "not configured".

## Settings

Everything goes under `bambu:` in `config.yaml`. Invalid values fall back to the default and
log a warning.

| Key | Default | What |
|---|---|---|
| `host` | none | printer IP address |
| `serial` | none | printer serial number (or `BAMBU_SERIAL`) |
| `access_code_file` | none | file holding the access code (or `BAMBU_ACCESS_CODE`) |
| `name` | `Printer` | what the printer is called on screen, in chat and on Discord |
| `camera.enabled` | `true` | run the camera relay (needs ffmpeg 5.1 or newer) |
| `camera.transport` | `socket` | how the relay hands the video to OBS: `socket`, a Unix socket in a folder only you can open, or `udp`, localhost UDP, where other local users could send video into OBS or watch the camera. `socket` falls back to UDP by itself (with a warning) only for a Flatpak OBS, which can't see that folder, or when the socket path is too long. If the folder isn't safe (`$XDG_RUNTIME_DIR` unset, someone else's or writable by others, or `ultimate-streaming` in it a symlink or someone else's), the camera stays off and the plugin page says why |
| `camera.port` | `5004` | names the socket (`bambu-camera-<port>.sock`, so two printers don't clash), or the localhost UDP port between the relay and OBS (1024 to 65535) |
| `camera.scene` | `Printer Cam` | OBS scene the plugin creates and switches to |
| `camera.source` | `Printer Camera` | OBS media source showing the camera |
| `camera.text_source` | `Printer Status` | OBS text source showing the status block; `""` for none |
| `camera.crop_right` | `0` | pixels cut off the right edge of the camera picture (the P2S picture is 1920 wide), e.g. to keep a window or your room off stream |
| `camera.crop_left` | `0` | the same for the left edge |
| `camera.ffmpeg` | `ffmpeg` | path to ffmpeg |
| `camera.hide_access_code` | `true` | hand ffmpeg the camera address in a private file (a 0600 concat list) so the access code never appears in the process list. `false` puts it on ffmpeg's command line, where other local users can see it; use it only as a fallback, and the app logs a warning while it's off |
| `discord.webhook` | unset (off) | id of a webhook from Settings → Discord |
| `discord.webhook_file` | unset | fallback when no webhook is picked: file holding a webhook URL (or `BAMBU_DISCORD_WEBHOOK`) |
| `discord.every_layers` | `10` | post every N layers (1 to 1000) |
| `discord.min_gap` | `15` | seconds between posts (at least 5) |
| `discord.final_delay` | `30` | seconds after a print ends before the last post |
| `on_print_end.scene` | `""` | OBS scene to switch to after a print ends; `""` stays put |
| `on_print_end.after` | `60` | seconds to wait first; a new print starting cancels it |

An `access_code:` or a webhook URL written straight into `config.yaml` is ignored, with a warning.
Secrets only come from files or the environment.

## Keys

These are the actions, under the editor group "Bambu printer".

| Action | Argument | Does |
|---|---|---|
| `bambu_pause` | none | pauses the print |
| `bambu_resume` | none | resumes it |
| `bambu_stop` | none | first press shows a desktop notification; a second press within 3 seconds stops the print |
| `bambu_light` | `on`, `off`, `toggle` | chamber light |
| `bambu_camera` | `on`, `off`, `toggle`, `trust` | starts or stops the camera relay; `trust` forgets the saved camera and status certificates and trusts the printer's current ones (see [OBS](#obs)) |
| `bambu_scene` | none | sets up the camera scene in OBS if needed, then switches to it |
| `bambu_setup_obs` | none | creates or repairs the Printer Cam scene, media source and text source |
| `bambu_snapshot` | none | posts a camera still to Discord now |

States for `state: {bambu: <arg>}`:

| Arg | True when |
|---|---|
| `printing` | the print is running |
| `paused` | it is paused |
| `active` | preparing, running or paused |
| `finished` | the last print finished |
| `failed` | the last print failed |
| `connected` | the printer connection is up |
| `light` | the chamber light is on |
| `camera` | the camera relay is live |

The `bambu_status` display sets the key's label from the printer's state:

| Printer | Label |
|---|---|
| printing | `58%`, `L 701/1058`, `1 h 2 min` (three lines) |
| paused | `Paused`, `58%` |
| preparing | `Getting ready` |
| finished | `Done` |
| failed | `Failed` |
| idle | your printer name, then `idle` |
| offline | your printer name, then `offline` |
| not configured | `Bambu setup` |

Ready-made keys:

```yaml
# Status
- label: Printer
  display: {source: bambu_status, every: 2}
  state: {bambu: printing}
  active: {bg: "#1a7f37"}

# Pause and resume
- {label: Pause, action: {bambu_pause: ""}, state: {bambu: paused}, active: {bg: "#b08000"}}
- {label: Resume, action: {bambu_resume: ""}}

# Stop: press twice within 3 seconds
- {label: Stop, action: {bambu_stop: ""}}

# Light
- {label: Light, action: {bambu_light: toggle}, state: {bambu: light}, active: {bg: "#b08000"}}

# Camera scene
- {label: Printer cam, action: {bambu_scene: ""}}
```

Pause and stop can never be used in channel-point rewards.

## OBS

Press a `bambu_setup_obs` key (or `bambu_scene`) once. It creates the Printer Cam scene, a media
source for the camera, and a text source for the status block. The names come from `camera.scene`,
`camera.source` and `camera.text_source`.

The camera relay uses ffmpeg 5.1 or newer. It copies the printer's video untouched (no
re-encode) to OBS and keeps the access code off the command line. The video goes over a Unix
socket, `$XDG_RUNTIME_DIR/ultimate-streaming/bambu-camera-<port>.sock`, inside a folder only you
can open (0700), so no other user on the machine can feed their own video into your stream or
watch the camera; no password is involved. ffmpeg waits there for OBS. OBS doesn't reconnect to
a socket by itself, so the plugin restarts the OBS camera source for it: each time the relay
starts listening (at start, after OBS disconnects, after a restart), and when a check every 5
seconds finds the source ended or in error (for example OBS started after the app), at most once
every 10 seconds. If OBS isn't connected, that waits until it is.

The relay uses localhost UDP instead only if you set `camera.transport: udp`, or by itself when
OBS is a Flatpak (it can't see that folder): the log warns and the plugin page says "The camera
uses UDP because OBS is a Flatpak; other local users could inject video." Over UDP any local user
could send video into the OBS source or listen on the port first.

The printer's certificates are trusted the first time each connection is made (trust on first
use) and saved per serial number: the camera's in `~/.config/ultimate-streaming/bambu-camera-<serial>.pem`
and the status connection's (MQTT, which carries the access code) in `bambu-mqtt-<serial>.pem`.
Every connection after that trusts only the saved certificate, so a different one is refused
before the access code is sent; certificate checking is never turned off. If a certificate
changes, the panel says so. Use **Trust printer cert again** (or `bambu_camera: trust`) only if
you know the printer was reset or replaced; it forgets both and the next connections save the
printer's current ones.

If the printer powers off or loses Wi-Fi mid-stream, the camera is marked as stopped after about
15 seconds without video and reconnects by itself when the printer is back. On the socket, no
video can also mean OBS stopped reading, so the plugin first restarts the OBS camera source and
waits another 15 seconds before restarting ffmpeg.

When the plugin starts, and when you change `camera.transport` or `camera.port`, the OBS media
source is pointed at where the relay now sends (after an upgrade, from the old UDP address to the
socket) straight away when OBS is connected, and otherwise the next time you press Set up OBS
scene.

**Hiding part of the picture.** If the camera sees out of the printer (through a glass door or
window), set `camera.crop_right` or `camera.crop_left`, or "Hide from the right/left" on the
plugin page. The crop is a "Privacy crop" filter on the camera source itself, so Discord snapshots
are cropped too, not just the scene. A change is applied to OBS straight away when it's
connected, and otherwise the next time the scene is set up.

The plugin also writes two text files that OBS can display:

| File (in `~/.config/ultimate-streaming/overlay/`) | What |
|---|---|
| `bambu.txt` | the status block, several lines |
| `bambu_line.txt` | the same in one line |

They update about once a second, and only when the text changes.

## Discord

Discord webhooks are managed in one place, Settings → Discord. Add a webhook there (in Discord:
Server Settings → Integrations → Webhooks → New Webhook → Copy Webhook URL), then open this
plugin's page, and under Discord pick it in "Discord webhook". The URL stays in the app's private
state, never in `config.yaml`. Replacing the URL on the Discord page takes effect within a few
seconds, and a webhook Discord reports as deleted is marked broken there.

```yaml
bambu:
  discord:
    webhook: golive        # the webhook's id from Settings → Discord
    every_layers: 10
```

Older setups keep working: with no webhook picked, `discord.webhook_file` (a file holding the URL,
made with `umask 077`) or `BAMBU_DISCORD_WEBHOOK` is used instead. The page no longer shows that
field. Once a webhook is picked, it is the only one used: if it breaks or has no URL, snapshots stop
and the printer card says so, rather than quietly posting to the old file's channel.

It posts when a print starts, every `every_layers` layers, and once after it ends
(`final_delay` seconds later). Pictures come from the camera source only, never your stream
output, and @-mentions are disabled in every post. A `bambu_snapshot` key posts one on demand.

## Twitch chat

`!print` answers with the printer's state. It is read-only. Add it to the Twitch Bot's commands:

```yaml
twitch_bot:
  commands:
    - {trigger: "!print", builtin: print}
```

The default response is `{printer}: {status}`. The placeholders are `{printer}` `{status}`
`{percent}` `{layer}` `{time_left}` `{finish}` `{job}`.

## For other modules

The plugin dispatches `bambu_print_started` and `bambu_print_ended`, each with a summary, so a
module can react with `@listener`.

## Control panel

The panel shows a status card titled "<name> (Bambu printer)" with State, Progress, Layer, Time
left, Nozzle / Bed, Camera and Discord. The note under it names the most important problem. It
never contains a secret.

## Troubleshooting

Run `python3 tools/doctor.py` first. It lists the plugin, its dependencies and the programs it
needs (here, ffmpeg) without running plugin code.

### "not configured"

Host, serial or the access code is missing. A key press says "The Bambu plugin isn't set up
yet: set bambu.host, bambu.serial and the access code". Check the three, and that the access
code file exists and isn't empty.

### Can't reach the printer

Check that `host` is the printer's current IP, that you are on the same network, and that
nothing blocks port 8883. The panel's card shows the error.

### The printer refuses the connection or commands

Check the access code first: it is shown on the printer and can change. If the code is right
and the printer still refuses, then Bambu's 2025 firmware added an authorization mode. If that
applies to your model, enable LAN Only mode and Developer mode on the printer.

### Camera

- **ffmpeg missing or too old:** install ffmpeg 5.1 or newer, or set `camera.ffmpeg` to its path.
  Or set `camera.enabled: false` if you don't want the camera.
- **The camera keeps retrying with the default:** try `hide_access_code: false` under `camera:` and report it.
- **"The printer's camera certificate doesn't match":** check `serial` first. If it's right and
  the printer was reset or replaced, press **Trust printer cert again**.
- **"The printer's certificate changed":** the status connection no longer matches the saved
  certificate. If the printer was reset or replaced, press **Trust printer cert again**;
  otherwise check that nothing on your network is impersonating it.
- **Port in use** (UDP only): another program has `camera.port`. Set a different one (1024 to 65535).
- **"The camera's private folder (…) isn't safe":** the folder named belongs to someone else, is a
  symlink, or others can write to it, so the socket can't be private. Fix its owner or permissions
  (it should be yours, mode 0700), then press **Start camera**. The camera never falls back to UDP
  for this.
- **OBS shows nothing and the camera stays Starting:** OBS isn't reading the relay's socket. Press
  **Set up OBS scene** so its media source points at it.

## Security

- The access code and webhook live in files only you can read (`umask 077`), or in the
  environment. They are never put in `config.yaml`, on a command line or in the panel.
- The status connection (MQTT) and the camera both pin the printer's certificate on first use, so
  the access code is only ever sent to the printer you first connected to.
- The camera reaches OBS through a socket in a private folder, so other local users can neither
  inject video nor watch. Over UDP (`camera.transport: udp`, or a Flatpak OBS) they could.
- The webhook URL lets anyone who has it post to your Discord channel. Don't share it.
- Like every plugin, this one runs with the same rights as the app. Read
  [Plugins](../../README.md#plugins) in the main README.
