"""Keep the user's own OBS setup: adopt the current profile and collection, add single sources
to a scene they choose. Nothing here ever creates a profile or scene collection, and OBS's files
are backed up (core/setup_backup.py) before every operation that changes OBS.

`send(request, data=None)` is the obs-websocket callable obs/build_scenes.py uses."""
import importlib.util
import logging
import os
import time

from core import detect, setup_backup

log = logging.getLogger("deck")

ADDONS = {"title_text": "Stream title text", "category_text": "Stream category text",
          "countdown": "Starting-soon countdown text", "radio_music": "Radio music (BRB Music)",
          "reward_background": "Color background for channel-point rewards",
          "alert_text": "On-stream alerts text (follows, subs, cheers, raids)"}

REWARD_BACKGROUND = "Reward Background"
_TEXT_ADDONS = {"title_text": "Stream Title", "category_text": "Stream Category", "countdown": "Countdown",
                "alert_text": "Alert"}

_bs = None


def _build_scenes():
    """obs/build_scenes.py (a script, not a package) loaded once, for its source definitions."""
    global _bs
    if _bs is None:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "obs", "build_scenes.py")
        spec = importlib.util.spec_from_file_location("obs_build_scenes", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _bs = mod
    return _bs


def backup_obs(label):
    """Archive OBS's profiles, collections and websocket config, for every OBS settings folder found
    (the active one first). Returns the active folder's archive id, or None when there is no OBS
    config folder. Raises (BackupError, ...) if a backup fails: callers must then not change OBS."""
    scan = detect.scan()
    names = {r["source_id"]: r.get("label") for r in scan["obs"]["roots"]}
    first = None
    for sid, real in detect.obs_roots(scan):
        item = {"kind": "obs", "root": real, "include": list(setup_backup.OBS_INCLUDE),
                "label": names.get(sid) or "OBS"}
        archive = setup_backup.create([item], label=f"before-{label}", auto=True)["id"]
        first = first or archive
    return first


def current(send):
    """What OBS is using right now. Never includes the stream key."""
    service = send("GetStreamServiceSettings")
    return {"profile": send("GetProfileList")["currentProfileName"],
            "collection": send("GetSceneCollectionList")["currentSceneCollectionName"],
            "scenes": [x["sceneName"] for x in reversed(send("GetSceneList")["scenes"])],
            "has_stream_key": bool((service.get("streamServiceSettings") or {}).get("key"))}


def _error(msg):
    return {"ok": False, "error": msg}


def _wait(fn):
    for _ in range(40):
        if fn():
            return True
        time.sleep(0.25)
    return False


def adopt(send, *, profile=None, collection=None):
    """Use an existing profile / scene collection (the current ones unless named). Switches only
    when it differs and the target exists; never creates anything."""
    profiles = send("GetProfileList")
    collections = send("GetSceneCollectionList")
    want_p = profile or profiles["currentProfileName"]
    want_c = collection or collections["currentSceneCollectionName"]
    if want_p not in profiles["profiles"]:
        return _error(f"No OBS profile named {want_p!r}")
    if want_c not in collections["sceneCollections"]:
        return _error(f"No OBS scene collection named {want_c!r}")
    backup = backup_obs("adopt")
    switched = []
    if want_p != profiles["currentProfileName"]:
        send("SetCurrentProfile", {"profileName": want_p})
        if not _wait(lambda: send("GetProfileList")["currentProfileName"] == want_p):
            return _error("OBS did not switch profile in time")
        switched.append("profile")
    if want_c != collections["currentSceneCollectionName"]:
        send("SetCurrentSceneCollection", {"sceneCollectionName": want_c})
        if not _wait(lambda: send("GetSceneCollectionList")["currentSceneCollectionName"] == want_c):
            return _error("OBS did not switch scene collection in time")
        switched.append("collection")
    return {"ok": True, "profile": want_p, "collection": want_c, "switched": switched, "backup": backup}


def _radio(cfg):
    return (cfg or {}).get("radio") or {}


def _definition(addon, send, cfg):
    """(source name, input kind, input settings, transform) for an add-on."""
    bs = _build_scenes()
    s = {**bs.DEFAULTS, "brb_videos": "", **((cfg or {}).get("obs_setup") or {})}
    s["screen_crop"] = {**bs.DEFAULTS["screen_crop"], **(s.get("screen_crop") or {})}
    if addon in _TEXT_ADDONS:
        name = _TEXT_ADDONS[addon]
        kind, settings = bs.inputs(s)[name]
        scene = bs.scenes(s)
        transform = next(t for items in scene.values() for src, t in items if src == name)
        return name, kind, settings, transform
    if addon == "radio_music":
        return (str(_radio(cfg).get("source") or "BRB Music"), "ffmpeg_source", dict(bs.MUSIC_SETTINGS), {})
    video = send("GetVideoSettings")
    return (REWARD_BACKGROUND, "color_source_v3",
            {"color": bs.abgr(bs.DARK), "width": int(video["baseWidth"]), "height": int(video["baseHeight"])}, {})


def add_addon(send, scene, addon, cfg):
    """Add one source to `scene` (and only that scene). Reuses an existing input of our name;
    refuses if that name belongs to an input of another kind or to a scene."""
    if addon not in ADDONS:
        return _error(f"Unknown add-on {addon!r}")
    scenes = [x["sceneName"] for x in send("GetSceneList")["scenes"]]
    if scene not in scenes:
        return _error(f"No scene named {scene!r} in the current collection")
    bs = _build_scenes()
    name, kind, settings, transform = _definition(addon, send, cfg)
    present = {i["inputName"]: i["inputKind"] for i in send("GetInputList")["inputs"]}
    if name in scenes or (name in present and present[name] != kind):
        return _error(f"{name!r} already exists in OBS as something else; rename or remove it first")
    exists = name in present
    if not exists:
        kinds = set(send("GetInputKindList", {"unversioned": False})["inputKinds"])
        if kind not in kinds:
            return _error(f"This OBS has no {kind!r} source")
    if name in {i["sourceName"] for i in send("GetSceneItemList", {"sceneName": scene})["sceneItems"]}:
        return {"ok": True, "source": name, "created": False}

    backup_obs(f"addon-{addon}".replace("_", "-"))
    try:
        if exists:
            item_id = send("CreateSceneItem", {"sceneName": scene, "sourceName": name,
                                               "sceneItemEnabled": True})["sceneItemId"]
        else:
            if settings.get("text_file") and not os.path.exists(settings["text_file"]):
                os.makedirs(os.path.dirname(settings["text_file"]), exist_ok=True)
                open(settings["text_file"], "a").close()
            item_id = send("CreateInput", {"sceneName": scene, "inputName": name, "inputKind": kind,
                                           "inputSettings": settings, "sceneItemEnabled": True})["sceneItemId"]
            if addon == "radio_music":
                send("SetInputVolume", {"inputName": name,
                                        "inputVolumeDb": float(_radio(cfg).get("volume_db", -18))})
        if addon == "reward_background":
            send("SetSceneItemIndex", {"sceneName": scene, "sceneItemId": item_id, "sceneItemIndex": 0})
        elif transform:
            f = send("GetVideoSettings")["baseWidth"] / bs.DESIGN_W
            send("SetSceneItemTransform", {"sceneName": scene, "sceneItemId": item_id,
                                           "sceneItemTransform": bs.scale_transform(dict(transform), f)})
    except Exception as e:   # the scene vanished or the collection switched since we looked
        log.warning("add-on %s failed: %s", addon, type(e).__name__)
        return _error(f"OBS refused the change ({type(e).__name__}); the scene may have changed. Try again.")
    return {"ok": True, "source": name, "created": not exists}
