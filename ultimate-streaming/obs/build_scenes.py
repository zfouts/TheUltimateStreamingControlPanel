#!/usr/bin/env python3
"""Build the deck's OBS profile and scene collection in a running OBS, over obs-websocket.

Nothing here is specific to one machine: every setting comes from the `obs_setup:`
section of config.yaml (see config.example.yaml), with portable defaults.

Config `obs_setup.mode`: `adopt` never creates or switches a profile or collection (default whenever
OBS already has a profile besides "Untitled"); `starter` builds the deck's own. OBS's files are backed up
(core/setup_backup.py) before anything here changes OBS.

What it does:
  * Profile: creates it if missing (canvas size, fps, encoder, bitrate, recording folder,
    replay buffer). An existing profile with that name is never modified.
  * Scene collection: creates a new one, leaving your existing collections untouched.
    Refuses to run if the collection already exists, so it never overwrites your edits.

Scenes (in Scenes-dock order, which the deck's scene_slot keys follow):
    Starting Soon · Screen · Game · Camera · BRB · Ending

Text sources read the overlay text files written by modules/overlay.py and
modules/brb.py (title, category, countdown, chat feed, polls, hype meter, video credit).

    python3 obs/build_scenes.py                  build profile + collection (collection must not exist)
    python3 obs/build_scenes.py --scene BRB      rebuild one scene in the current collection
    python3 obs/build_scenes.py --print-config   show the settings that would be used
    python3 obs/build_scenes.py --adopt          use your current profile + collection unchanged (no changes to OBS)
    python3 obs/build_scenes.py --audio          add mic, desktop audio + radio music to an existing collection

OBS must be running with Tools → WebSocket Server Settings → "Enable WebSocket server".
"""
import argparse
import json
import logging
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "vendor"))
sys.path.insert(0, ROOT)

import yaml  # noqa: E402

from core.obs_setup import backup_obs  # noqa: E402
from core.paths import config_path, obs_websocket_config, overlay_dir  # noqa: E402
from core.util import read_secret  # noqa: E402

DEFAULTS = {
    "profile": "Stream Deck",
    "collection": "Stream Deck",
    "canvas": [1920, 1080],         # base and output resolution
    "fps": 60,
    "encoder": "x264",              # x264 (CPU, works everywhere) | nvenc | qsv | amd
    "bitrate": 6000,                # kbps; Twitch's recommended max for 1080p60
    "audio_bitrate": 160,
    "recordings_dir": "~/Videos/Stream",
    "replay_buffer_seconds": 60,    # 0 disables the replay buffer
    "screen": 0,                    # monitor index for X11 screen capture
    "screen_crop": {"left": 0, "right": 0, "top": 0, "bottom": 0},   # e.g. show part of an ultrawide
    "webcam": "/dev/video0",
    "font": "Noto Sans",
    "mono_font": "DejaVu Sans Mono",
    "mode": None,                   # adopt | starter; unset = starter only if OBS has no profiles but "Untitled"
}

DESIGN_W, DESIGN_H = 1920, 1080     # the layout below is drawn at 1080p and scaled to the canvas
TWITCH_PURPLE, LIGHT_PURPLE, DARK, GREY, WHITE = "#9147ff", "#bf94ff", "#18181b", "#adadb8", "#ffffff"
PANEL = "#26262c"
HYPE_TRACK_WIDTH = 720              # must match modules/brb.py

# Scene-item alignment flags (obs_alignment): centre is 0.
CENTER, LEFT, TOP = 0, 1, 4


# ---------------------------------------------------------------------------- settings

def load_settings(path):
    try:
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        print(f"note: {path} not found; using defaults")
        cfg = {}
    s = {**DEFAULTS, **(cfg.get("obs_setup") or {})}
    s["screen_crop"] = {**DEFAULTS["screen_crop"], **(s.get("screen_crop") or {})}
    s["brb_videos"] = os.path.join(os.path.expanduser((cfg.get("brb") or {}).get("media_dir", "~/Stream/brb")),
                                   "videos")
    if s["mode"] not in (None, "adopt", "starter"):
        sys.exit(f'obs_setup.mode must be "adopt" or "starter", not {s["mode"]!r}')
    s["obs"] = cfg.get("obs") or {}
    s["radio_source"] = (cfg.get("radio") or {}).get("source", "BRB Music")
    s["radio_volume_db"] = float((cfg.get("radio") or {}).get("volume_db", -18))
    s["recordings_dir"] = os.path.expanduser(s["recordings_dir"])
    return s


def abgr(color, alpha=255):
    """OBS colour ints are 0xAABBGGRR."""
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return (alpha << 24) | (b << 16) | (g << 8) | r


def wayland():
    return os.environ.get("XDG_SESSION_TYPE") == "wayland" or bool(os.environ.get("WAYLAND_DISPLAY"))


def inputs(s):
    """Every source the scenes use: name -> (input kind, settings)."""
    overlay = overlay_dir()

    def text(content=None, file=None, size=48, color=WHITE, wrap_width=None, face=None):
        t = {"font": {"face": face or s["font"], "style": "Bold", "size": size, "flags": 1},
             "color1": abgr(color), "color2": abgr(color), "drop_shadow": True}
        if file:
            t.update(from_file=True, text_file=os.path.join(overlay, f"{file}.txt"))
        else:
            t["text"] = content
        if wrap_width:
            t.update(word_wrap=True, custom_width=wrap_width)
        return "text_ft2_source_v2", t

    def colour(c, w, h, alpha=255):
        return "color_source_v3", {"color": abgr(c, alpha), "width": w, "height": h}

    if wayland():
        # The portal asks which screen / window to share when OBS creates these.
        screen = ("pipewire-screen-capture-source", {"ShowCursor": True})
        game = ("pipewire-window-capture-source", {"ShowCursor": False})
    else:
        screen = ("xshm_input_v2", {"screen": int(s["screen"]), "show_cursor": True})
        game = ("xcomposite_input", {"show_cursor": False})

    return {
        "Background": colour(DARK, DESIGN_W, DESIGN_H),
        "Accent Bar": colour(TWITCH_PURPLE, DESIGN_W, 10),
        "Lower Third": colour(DARK, DESIGN_W, 150, 0xD9),
        "Headline: Starting": text("STARTING SOON", size=120),
        "Headline: BRB": text("BE RIGHT BACK", size=120),
        "Headline: Ending": text("THANKS FOR WATCHING", size=110),
        "Countdown": text(file="countdown", size=64, color=LIGHT_PURPLE),
        "Ending Subtext": text("Follow so you don't miss the next one", size=48, color=GREY),
        "Stream Title": text(file="title", size=40),
        "Stream Category": text(file="category", size=32, color=LIGHT_PURPLE),
        "Alert": text(file="alert", size=56, color=WHITE, wrap_width=1400),   # modules/alerts.py
        "Screen Capture": screen,
        "Game Capture": game,
        "Webcam": ("v4l2_input", {"device_id": s["webcam"], "auto_reset": True}),
        # BRB screen (driven by modules/brb.py)
        "Panel": colour(PANEL, 100, 100),
        "BRB Timer": text(file="brb_timer", size=56, color=LIGHT_PURPLE),
        "BRB Video": ("vlc_source", {"playlist": [], "loop": True, "playback_behavior": "pause_unpause"}),
        "BRB Video Placeholder": text(f"Drop videos in {s['brb_videos'].replace(os.path.expanduser('~'), '~')}",
                                      size=32, color=GREY),
        "BRB Video Credit": text(file="videocredit", size=20, color=GREY),
        "Chat Header": text("CHAT", size=28, color=LIGHT_PURPLE),
        "Chat Feed": text(file="chat", size=24, wrap_width=670),
        "Vote": text(file="vote", size=22, wrap_width=670, face=s["mono_font"]),
        "Hype Label": text(file="hype", size=26),
        "Hype Track": colour("#3a3a3d", HYPE_TRACK_WIDTH, 24),
        "Hype Fill": colour(TWITCH_PURPLE, 1, 24),
        "Hype Banner": text("HYPE MAXED!", size=150, color=LIGHT_PURPLE),
    }


# ---------------------------------------------------------------------------- layout (at 1080p)

FULL = {"boundsType": "OBS_BOUNDS_SCALE_INNER", "boundsWidth": DESIGN_W, "boundsHeight": DESIGN_H,
        "boundsAlignment": CENTER, "alignment": LEFT | TOP, "positionX": 0, "positionY": 0}
CAM_CORNER = {"boundsType": "OBS_BOUNDS_SCALE_INNER", "boundsWidth": 400, "boundsHeight": 225,
              "boundsAlignment": CENTER, "alignment": LEFT | TOP,
              "positionX": DESIGN_W - 400 - 24, "positionY": DESIGN_H - 225 - 24}
HIDDEN = {"hidden": True}  # merged into a transform: create the item disabled


def at(x, y, align=CENTER):
    return {"positionX": x, "positionY": y, "alignment": align}


def fit(x, y, width, height, align=CENTER):
    """Like at(), but shrinks the source to fit width x height (long titles stay on screen)."""
    return {**at(x, y, align), "boundsType": "OBS_BOUNDS_MAX_ONLY", "boundsWidth": width,
            "boundsHeight": height, "boundsAlignment": align}


def box(x, y, width, height):
    """Stretch a source (e.g. the 100x100 Panel colour) to exactly fill a rectangle."""
    return {**at(x, y, LEFT | TOP), "boundsType": "OBS_BOUNDS_STRETCH", "boundsWidth": width,
            "boundsHeight": height, "boundsAlignment": CENTER}


def framed(*items):
    """Dark background with purple bars top and bottom, plus the given items."""
    return [("Background", at(0, 0, LEFT | TOP)), ("Accent Bar", at(0, 0, LEFT | TOP)),
            ("Accent Bar", at(0, DESIGN_H - 10, LEFT | TOP)), *items]


def scenes(s):
    crop = {f"crop{k.title()}": int(v) for k, v in s["screen_crop"].items() if v}
    H = DESIGN_H
    return {
        "Starting Soon": framed(("Headline: Starting", at(960, 410)), ("Countdown", at(960, 560)),
                                ("Stream Title", fit(960, 860, 1760, 60)), ("Stream Category", at(960, 925))),
        "Screen": [("Screen Capture", {**FULL, **crop}), ("Webcam", CAM_CORNER),
                   ("Alert", fit(960, 40, 1400, 160, CENTER | TOP))],
        "Game": [("Game Capture", FULL), ("Webcam", CAM_CORNER),
                 ("Alert", fit(960, 40, 1400, 160, CENTER | TOP))],
        "Camera": [("Background", at(0, 0, LEFT | TOP)), ("Webcam", FULL), ("Lower Third", at(0, H - 150, LEFT | TOP)),
                   ("Stream Title", fit(48, H - 128, 1820, 60, LEFT | TOP)),
                   ("Stream Category", at(48, H - 64, LEFT | TOP)),
                   ("Alert", fit(960, 40, 1400, 160, CENTER | TOP))],
        # Left: headline, back-in timer, video panel (with its own audio), title.
        # Right: chat panel, game panel (polls / guess game), hype meter. Banner on top when hype maxes.
        "BRB": framed(
            ("Headline: BRB", fit(60, 50, 1000, 130, LEFT | TOP)), ("BRB Timer", at(64, 190, LEFT | TOP)),
            ("Panel", box(60, 280, 1000, 563)),
            ("BRB Video", {"boundsType": "OBS_BOUNDS_SCALE_INNER", "boundsWidth": 1000, "boundsHeight": 563,
                           "boundsAlignment": CENTER, **at(60, 280, LEFT | TOP), **HIDDEN}),
            ("BRB Video Placeholder", at(560, 561)),
            ("BRB Video Credit", fit(72, 812, 976, 26, LEFT | TOP)),
            ("Stream Title", fit(60, 885, 1000, 50, LEFT | TOP)), ("Stream Category", at(60, 945, LEFT | TOP)),
            ("Panel", box(1140, 50, 720, 500)), ("Chat Header", at(1164, 64, LEFT | TOP)),
            ("Chat Feed", at(1164, 108, LEFT | TOP)),
            ("Panel", box(1140, 570, 720, 340)), ("Vote", at(1164, 588, LEFT | TOP)),
            ("Hype Label", at(1140, 928, LEFT | TOP)),
            ("Hype Track", at(1140, 974, LEFT | TOP)), ("Hype Fill", at(1140, 974, LEFT | TOP)),
            ("Hype Banner", {**at(960, 540), **HIDDEN}),
            # The strip under the stream category (left column), clear of the hype meter on the right.
            ("Alert", fit(560, 995, 1000, 60, CENTER | TOP))),
        "Ending": framed(("Headline: Ending", at(960, 430)), ("Ending Subtext", at(960, 560))),
    }


def scale_transform(t, f):
    """Map a 1080p transform onto the real canvas (f = canvas width / 1920)."""
    if f == 1:
        return t
    t = dict(t)
    for k in ("positionX", "positionY", "boundsWidth", "boundsHeight"):
        if k in t:
            t[k] = t[k] * f
    if "boundsType" not in t:
        t["scaleX"] = t["scaleY"] = f
    return t


AUDIO = {"BRB Video": {"volume_db": -10}}   # starting volumes for media sources

# Your microphone and desktop sound. OBS normally makes these itself ("Global Audio Devices"
# in Settings → Audio), but they can be switched off; then a stream would be silent and the
# deck's mute keys (which use these names) would fail. ensure_audio() adds them as ordinary
# sources in every scene, following the system's default input / output device.
MIC, DESKTOP = "Mic/Aux", "Desktop Audio"
AUDIO_KINDS = {  # first kind this OBS has wins
    MIC: [("pulse_input_capture", {"device_id": "default"}), ("alsa_input_capture", {"device_id": "default"})],
    DESKTOP: [("pulse_output_capture", {"device_id": "default"})],
}
AUDIO_KIND_PREFIXES = ("pulse_", "alsa_", "jack_", "pipewire-audio", "coreaudio", "wasapi")


# ---------------------------------------------------------------------------- OBS

class OBSClient:
    def __init__(self, s):
        import obsws_python
        logging.disable(logging.CRITICAL)  # obsws_python logs the password on connect
        host = s["obs"].get("host", "localhost")
        port = s["obs"].get("port")
        password = read_secret("OBS_WEBSOCKET_PASSWORD", s["obs"].get("password_file"))
        path = obs_websocket_config()
        if os.path.exists(path) and host in ("localhost", "127.0.0.1", "::1"):
            c = json.load(open(path))
            port = port or c.get("server_port")
            if password is None and c.get("auth_required"):
                password = c.get("server_password", "")
        for _ in range(40):
            try:
                self.ws = obsws_python.ReqClient(host=host, port=port or 4455, password=password or "", timeout=5)
                return
            except Exception:
                time.sleep(0.5)
        sys.exit(f"Couldn't reach OBS at {host}:{port or 4455}. Is OBS running, with "
                 "Tools → WebSocket Server Settings → Enable WebSocket server ticked?")

    def __call__(self, request, data=None):
        return self.ws.send(request, data, raw=True)


def wait_for(fn, what):
    for _ in range(40):
        if fn():
            return
        time.sleep(0.25)
    sys.exit(f"Timed out waiting for {what}")


def ensure_profile(send, s):
    """Create the profile (if missing) and make it current. Existing profiles are left alone."""
    name = s["profile"]
    profiles = send("GetProfileList")
    if name in profiles["profiles"]:
        print(f'profile "{name}" exists; using it unchanged')
        if profiles["currentProfileName"] != name:
            send("SetCurrentProfile", {"profileName": name})
            wait_for(lambda: send("GetProfileList")["currentProfileName"] == name, "the profile switch")
        return
    previous = profiles["currentProfileName"]
    # A new profile starts with no stream service (no server, no stream key), so "Start Streaming"
    # would fail. Carry over the service of the profile that was in use.
    service = send("GetStreamServiceSettings")
    send("CreateProfile", {"profileName": name})  # also switches to it
    wait_for(lambda: send("GetProfileList")["currentProfileName"] == name, "the new profile")
    if (service.get("streamServiceSettings") or {}).get("key"):
        send("SetStreamServiceSettings", {"streamServiceType": service["streamServiceType"],
                                          "streamServiceSettings": service["streamServiceSettings"]})
        print(f'profile "{name}": stream service copied from "{previous}"')
    else:
        print(f'profile "{name}": no stream key to copy; set it in OBS → Settings → Stream')

    w, h = (int(v) for v in s["canvas"])
    send("SetVideoSettings", {"baseWidth": w, "baseHeight": h, "outputWidth": w, "outputHeight": h,
                              "fpsNumerator": int(s["fps"]), "fpsDenominator": 1})
    os.makedirs(s["recordings_dir"], exist_ok=True)
    version = tuple(int(x) for x in send("GetVersion")["obsVersion"].split(".")[:2] if x.isdigit())
    rec_format = "hybrid_mp4" if version >= (30, 2) else "mkv"   # both survive a crash mid-recording
    rb = int(s["replay_buffer_seconds"])
    params = {
        ("Output", "Mode"): "Simple",
        ("SimpleOutput", "StreamEncoder"): s["encoder"],
        ("SimpleOutput", "RecEncoder"): s["encoder"],
        ("SimpleOutput", "VBitrate"): s["bitrate"],
        ("SimpleOutput", "ABitrate"): s["audio_bitrate"],
        ("SimpleOutput", "RecQuality"): "Stream",          # record at stream quality (no second encoder)
        ("SimpleOutput", "RecFormat2"): rec_format,
        ("SimpleOutput", "FilePath"): s["recordings_dir"],
        ("SimpleOutput", "RecRB"): "true" if rb else "false",
        ("SimpleOutput", "RecRBTime"): rb or 20,
        ("SimpleOutput", "RecRBPrefix"): "Replay",        # modules/brb.py looks for Replay*
    }
    for (category, key), value in params.items():
        send("SetProfileParameter", {"parameterCategory": category, "parameterName": key,
                                     "parameterValue": str(value)})
    # OBS only re-reads output settings when a profile is loaded: bounce off the old one.
    if previous != name:
        send("SetCurrentProfile", {"profileName": previous})
        wait_for(lambda: send("GetProfileList")["currentProfileName"] == previous, "the profile switch")
        send("SetCurrentProfile", {"profileName": name})
        wait_for(lambda: send("GetProfileList")["currentProfileName"] == name, "the profile switch")
    print(f'profile "{name}": {w}x{h}@{s["fps"]}, {s["encoder"]} {s["bitrate"]} kbps, '
          f'recordings → {s["recordings_dir"]} ({rec_format}), replay buffer {rb}s')
    if s["encoder"] != "x264":
        print(f'  (if OBS reports the {s["encoder"]} encoder is unavailable, set obs_setup.encoder: x264)')


def apply_audio(send):
    present = {i["inputName"] for i in send("GetInputList")["inputs"]}
    for name, conf in AUDIO.items():
        if name in present:
            send("SetInputVolume", {"inputName": name, "inputVolumeDb": conf["volume_db"]})


def audio_inputs(send):
    """Names of inputs that capture a microphone or the desktop (global devices included)."""
    names = {i["inputName"] for i in send("GetInputList")["inputs"]
             if i["inputKind"].startswith(AUDIO_KIND_PREFIXES)}
    names |= {v for v in send("GetSpecialInputs").values() if v}
    return names


def ensure_audio(send, scene_names):
    """Make sure the stream has a microphone and desktop sound, in every given scene.

    Uses OBS's global audio devices when they're on; otherwise creates Mic/Aux and Desktop
    Audio sources (default devices) and adds them to each scene. Safe to run again."""
    special = send("GetSpecialInputs")
    kinds = set(send("GetInputKindList", {"unversioned": True})["inputKinds"])
    present = {i["inputName"]: i["inputKind"] for i in send("GetInputList")["inputs"]}
    for name, slot in ((MIC, "mic1"), (DESKTOP, "desktop1")):
        if special.get(slot):
            print(f"audio: {special[slot]!r} (OBS global audio device)")
            continue
        if name not in present:
            choice = next(((k, st) for k, st in AUDIO_KINDS[name] if k in kinds), None)
            if not choice:
                print(f"audio: can't add {name!r}: this OBS has none of {[k for k, _ in AUDIO_KINDS[name]]}")
                continue
            if not scene_names:
                print(f"audio: no scenes to add {name!r} to")
                continue
            send("CreateInput", {"sceneName": scene_names[0], "inputName": name, "inputKind": choice[0],
                                 "inputSettings": choice[1], "sceneItemEnabled": True})
            print(f"audio: added {name!r} (system default {'microphone' if name == MIC else 'output'})")
        for scene in scene_names:
            items = {i["sourceName"] for i in send("GetSceneItemList", {"sceneName": scene})["sceneItems"]}
            if name not in items:
                send("CreateSceneItem", {"sceneName": scene, "sourceName": name, "sceneItemEnabled": True})


MUSIC_SETTINGS = {"is_local_file": True, "local_file": "", "looping": False,
                  "restart_on_activate": False, "close_when_inactive": False}


def ensure_music(send, scene_names, name="BRB Music", volume_db=-18):
    """The radio's media source (modules/radio.py loads tracks into it), in every given scene,
    so music carries on across scene changes. Starts quiet; the Radio key turns it on/off."""
    present = {i["inputName"] for i in send("GetInputList")["inputs"]}
    if name not in present:
        if "ffmpeg_source" not in set(send("GetInputKindList", {"unversioned": True})["inputKinds"]):
            print(f"music: can't add {name!r}: this OBS has no media source (ffmpeg_source)")
            return
        if not scene_names:
            return
        send("CreateInput", {"sceneName": scene_names[0], "inputName": name, "inputKind": "ffmpeg_source",
                             "inputSettings": dict(MUSIC_SETTINGS),
                             "sceneItemEnabled": True})
        send("SetInputVolume", {"inputName": name, "inputVolumeDb": volume_db})
        print(f"music: added {name!r} for the radio ({volume_db:g} dB)")
    for scene in scene_names:
        items = {i["sourceName"] for i in send("GetSceneItemList", {"sceneName": scene})["sceneItems"]}
        if name not in items:
            send("CreateSceneItem", {"sceneName": scene, "sourceName": name, "sceneItemEnabled": True})


def add_items(send, scene, items, existing, sources, kinds, f):
    """Add a scene's items in order (first = bottom), creating inputs that don't exist yet."""
    for source, transform in items:
        transform = dict(transform)
        enabled = not transform.pop("hidden", False)
        if source in existing:
            item_id = send("CreateSceneItem", {"sceneName": scene, "sourceName": source,
                                               "sceneItemEnabled": enabled})["sceneItemId"]
        else:
            kind, settings = sources[source]
            if kind not in kinds:
                print(f"  skipped {source!r}: this OBS has no {kind!r} source "
                      f"({'install the VLC plugin / libvlc' if kind == 'vlc_source' else 'missing plugin'})")
                continue
            if settings.get("text_file") and not os.path.exists(settings["text_file"]):
                # OBS's text source won't retry a file that was missing when it was created.
                os.makedirs(os.path.dirname(settings["text_file"]), exist_ok=True)
                open(settings["text_file"], "a").close()
            item_id = send("CreateInput", {"sceneName": scene, "inputName": source, "inputKind": kind,
                                           "inputSettings": settings, "sceneItemEnabled": enabled})["sceneItemId"]
            existing.add(source)
        send("SetSceneItemTransform", {"sceneName": scene, "sceneItemId": item_id,
                                       "sceneItemTransform": scale_transform(transform, f)})


def context(send, s):
    kinds = set(send("GetInputKindList", {"unversioned": False})["inputKinds"])
    width = send("GetVideoSettings")["baseWidth"]
    return inputs(s), kinds, width / DESIGN_W


def rebuild_scene(send, s, scene):
    """Replace one scene's items with the layout above, keeping shared inputs and their settings."""
    layout = scenes(s)
    if scene not in layout:
        sys.exit(f"unknown scene {scene!r}; choose from: {', '.join(layout)}")
    in_collection = send("GetSceneCollectionList")["currentSceneCollectionName"]
    if in_collection != s["collection"]:
        if s.get("mode") == "adopt":
            sys.exit(f'--scene would replace the contents of a scene in your own collection "{in_collection}". '
                     "Use the add-ons (Settings → Existing setup) to add single sources instead.")
        sys.exit(f'Switch OBS to the "{s["collection"]}" scene collection first.')
    if scene not in [x["sceneName"] for x in send("GetSceneList")["scenes"]]:
        sys.exit(f'OBS has no scene named {scene!r} in "{in_collection}".')
    old = send("GetSceneItemList", {"sceneName": scene})["sceneItems"]
    existing = {i["inputName"] for i in send("GetInputList")["inputs"]} | \
               {x["sceneName"] for x in send("GetSceneList")["scenes"]}
    # Add the new items before removing the old ones: OBS destroys an input as soon
    # as its last scene item goes, which would lose inputs we're about to reuse.
    add_items(send, scene, layout[scene], existing, *context(send, s))
    for item in old:
        send("RemoveSceneItem", {"sceneName": scene, "sceneItemId": item["sceneItemId"]})
    used = {src for items in layout.values() for src, _ in items}
    for item in old:
        if item["sourceName"] not in used:
            print("dropped unused source:", item["sourceName"])
    print("rebuilt scene:", scene)


def resolve_mode(send, s):
    """adopt | starter. Unset: starter only when OBS has no profile besides the untouched "Untitled"."""
    if s.get("mode"):
        return s["mode"]
    profiles = set(send("GetProfileList")["profiles"])
    return "starter" if profiles <= {"Untitled"} else "adopt"


def ensure_overlay_files(s):
    """Create the (empty) overlay text files the deck writes, so OBS text sources can read them."""
    made = []
    for kind, settings in inputs(s).values():
        path = settings.get("text_file")
        if path and not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "a").close()
            made.append(path)
    return made


def adopt_current(send, s):
    """Use OBS's current profile and scene collection as they are. Adds nothing to OBS."""
    profile = send("GetProfileList")["currentProfileName"]
    collection = send("GetSceneCollectionList")["currentSceneCollectionName"]
    names = [x["sceneName"] for x in reversed(send("GetSceneList")["scenes"])]
    print(f'adopt: using profile "{profile}", scene collection "{collection}"')
    print("adopt: scenes:", ", ".join(names) or "none")
    made = ensure_overlay_files(s)
    print(f"adopt: overlay files ready ({len(made)} created); OBS itself is unchanged.")
    print("Add sources to a scene of your own from the panel (Settings → Existing setup).")


def build(send, s):
    if s.get("mode") == "adopt":
        return adopt_current(send, s)
    collection = s["collection"]
    if collection in send("GetSceneCollectionList")["sceneCollections"]:
        sys.exit(f'Scene collection "{collection}" already exists; delete or rename it in OBS to rebuild, '
                 'or use --scene NAME to rebuild one scene.')
    if send("GetStreamStatus")["outputActive"] or send("GetRecordStatus")["outputActive"]:
        sys.exit("Stop streaming/recording first.")

    ensure_profile(send, s)
    send("CreateSceneCollection", {"sceneCollectionName": collection})
    wait_for(lambda: send("GetSceneCollectionList")["currentSceneCollectionName"] == collection,
             "the new scene collection")
    time.sleep(1)  # let OBS finish loading the empty collection
    default_scenes = [x["sceneName"] for x in send("GetSceneList")["scenes"]]

    sources, kinds, f = context(send, s)
    created = set()
    for scene, items in scenes(s).items():
        send("CreateScene", {"sceneName": scene})
        add_items(send, scene, items, created, sources, kinds, f)
        print("scene:", scene)
    for name in default_scenes:  # the empty "Scene" a new collection starts with
        send("RemoveScene", {"sceneName": name})

    ensure_audio(send, list(scenes(s)))
    ensure_music(send, list(scenes(s)), s["radio_source"], s["radio_volume_db"])
    apply_audio(send)
    send("SetCurrentSceneTransition", {"transitionName": "Fade"})
    send("SetCurrentSceneTransitionDuration", {"transitionDuration": 300})
    send("SetCurrentProgramScene", {"sceneName": "Starting Soon"})
    print("audio inputs:", ", ".join(sorted(audio_inputs(send))) or "none (see the audio lines above)")
    print("done. Next: Settings → Stream → connect your Twitch account (or paste a stream key) in OBS.")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", default=config_path(), help="config.yaml to read obs_setup from")
    ap.add_argument("--scene", help="rebuild just this scene in the current collection")
    ap.add_argument("--audio", action="store_true",
                    help="only add the microphone, desktop audio and the radio's music source to every scene of the current collection")
    ap.add_argument("--adopt", action="store_true",
                    help="use OBS's current profile and collection as they are; add nothing")
    ap.add_argument("--print-config", action="store_true", help="print the settings and exit")
    args = ap.parse_args()
    s = load_settings(args.config)
    if args.print_config:
        print(yaml.safe_dump({k: v for k, v in s.items() if k != "obs"}, sort_keys=False))
        return
    send = OBSClient(s)
    s["mode"] = "adopt" if args.adopt else resolve_mode(send, s)
    if args.adopt or (s["mode"] == "adopt" and not (args.audio or args.scene)):
        return adopt_current(send, s)
    archive = backup_obs("build-scenes")   # before anything below changes OBS
    print("backup:", archive or "no OBS config folder found; nothing backed up")
    if args.audio:
        all_scenes = [x["sceneName"] for x in reversed(send("GetSceneList")["scenes"])]
        ensure_audio(send, all_scenes)
        ensure_music(send, all_scenes, s["radio_source"], s["radio_volume_db"])
        print("audio inputs:", ", ".join(sorted(audio_inputs(send))) or "none")
    elif args.scene:
        rebuild_scene(send, s, args.scene)
        ensure_audio(send, [args.scene])
        ensure_music(send, [args.scene], s["radio_source"], s["radio_volume_db"])
        apply_audio(send)
    else:
        build(send, s)


if __name__ == "__main__":
    main()
