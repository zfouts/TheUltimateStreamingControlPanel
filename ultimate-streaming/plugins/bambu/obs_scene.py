"""The OBS side of the printer camera: a scene with a media source reading the local camera relay
and a status text source, plus in-memory snapshots of that one source. Pure functions over the
OBS module (`obs.request(name, data)`); no state.

Security: the media source only ever reads the URL the caller passes (re-applied on every ensure,
so a hand-edited URL is corrected), and snapshots grab only that input - never the program output
or a scene, and never to a file path."""
import base64

CAMERA_KIND = "ffmpeg_source"
TEXT_KIND = "text_ft2_source_v2"
MARGIN = 24
CROP_FILTER = "Privacy crop"     # a Crop/Pad filter on the camera source itself, so snapshots are cropped too
CROP_KIND = "crop_filter"
LEFT, BOTTOM = 1, 8  # obs alignment bits


def _camera_settings(url):
    # The socket relay takes one client and listens again after OBS disconnects: OBS reconnects
    # (reconnect_delay_sec) as a new client, restart_on_activate makes a fresh connection, and
    # close_when_inactive stays off so a hidden scene doesn't drop the connection.
    return {"is_local_file": False, "input": url, "input_format": "mpegts", "restart_on_activate": True,
            "close_when_inactive": False, "buffering_mb": 1, "reconnect_delay_sec": 2, "hw_decode": False}


def _kinds(obs):
    return {i["inputName"]: i.get("inputKind") for i in obs.request("GetInputList", {})["inputs"]}


def scene_exists(obs, scene):
    return any(s.get("sceneName") == scene for s in obs.request("GetSceneList", {})["scenes"])


def _canvas(obs):
    v = obs.request("GetVideoSettings", {})
    return int(v["baseWidth"]), int(v["baseHeight"])


def _ensure_input(obs, scene, name, kind, kinds, in_scene, create_settings, apply_settings, differs, transform):
    """Create / repair one input in the scene. Returns "created", "updated" or None."""
    result = None
    if name in kinds:
        if kinds[name] != kind:
            what = "a media source" if kind == CAMERA_KIND else "a text source"
            raise ValueError(f"OBS already has a source called {name!r} that isn't {what}; "
                             "rename it or set camera.source")
        current = obs.request("GetInputSettings", {"inputName": name}).get("inputSettings") or {}
        obs.request("SetInputSettings", {"inputName": name, "inputSettings": apply_settings, "overlay": True})
        if differs(current):
            result = "updated"
        if name not in in_scene:
            obs.request("CreateSceneItem", {"sceneName": scene, "sourceName": name, "sceneItemEnabled": True})
    else:
        item = obs.request("CreateInput", {"sceneName": scene, "inputName": name, "inputKind": kind,
                                           "inputSettings": create_settings, "sceneItemEnabled": True})
        obs.request("SetSceneItemTransform", {"sceneName": scene, "sceneItemId": item["sceneItemId"],
                                              "sceneItemTransform": transform})
        kinds[name] = kind
        result = "created"
    return result


def _ensure_crop(obs, source, left, right):
    """Keep the camera source's crop filter matching left/right (pixels of the camera picture).
    It sits on the source, not the scene item, so a Discord snapshot of the source is cropped
    too. Both 0 removes it. Returns True if anything changed."""
    filters = {f.get("filterName"): f for f in
               obs.request("GetSourceFilterList", {"sourceName": source}).get("filters") or []}
    current = filters.get(CROP_FILTER)
    if current is not None and current.get("filterKind") != CROP_KIND:
        raise ValueError(f"the camera source already has a filter called {CROP_FILTER!r} that isn't a "
                         "Crop/Pad filter; rename it in OBS")
    if not left and not right:
        if current is None:
            return False
        obs.request("RemoveSourceFilter", {"sourceName": source, "filterName": CROP_FILTER})
        return True
    want = {"relative": True, "left": int(left), "right": int(right), "top": 0, "bottom": 0}
    if current is None:
        obs.request("CreateSourceFilter", {"sourceName": source, "filterName": CROP_FILTER,
                                           "filterKind": CROP_KIND, "filterSettings": want})
        return True
    have = current.get("filterSettings") or {}
    changed = any(have.get(k, 0 if k != "relative" else True) != v for k, v in want.items())
    if changed:
        obs.request("SetSourceFilterSettings", {"sourceName": source, "filterName": CROP_FILTER,
                                                "filterSettings": want, "overlay": True})
    if current.get("filterEnabled") is False:
        obs.request("SetSourceFilterEnabled", {"sourceName": source, "filterName": CROP_FILTER,
                                               "filterEnabled": True})
        changed = True
    return changed


def apply_crop(obs, source, left, right):
    """Update just the crop on an existing camera source (after a settings change). Returns
    True if it changed, False if nothing to do or the source isn't in OBS yet."""
    if _kinds(obs).get(source) != CAMERA_KIND:
        return False
    return _ensure_crop(obs, source, left, right)


def apply_url(obs, source, url):
    """Point an existing camera source at `url` (at start, and after the relay's transport or
    port changed). Returns True if it changed, False if nothing to do or the source isn't in OBS
    yet."""
    if _kinds(obs).get(source) != CAMERA_KIND:
        return False
    current = obs.request("GetInputSettings", {"inputName": source}).get("inputSettings") or {}
    if current.get("input") == url and current.get("is_local_file") is False:
        return False
    obs.request("SetInputSettings", {"inputName": source, "inputSettings": _camera_settings(url),
                                     "overlay": True})
    return True


RESTART = "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART"
# The media states in which the camera source has given up: OBS doesn't reconnect a unix: input
# by itself, so these need a restart_media().
DEAD_STATES = frozenset({"OBS_MEDIA_STATE_ENDED", "OBS_MEDIA_STATE_ERROR", "OBS_MEDIA_STATE_NONE"})


def restart_media(obs, source):
    """Make the camera source open its input again (a new connection to the relay). Returns
    True if asked, False if the source isn't in OBS (or isn't a media source)."""
    if _kinds(obs).get(source) != CAMERA_KIND:
        return False
    obs.request("TriggerMediaInputAction", {"inputName": source, "mediaAction": RESTART})
    return True


def media_state(obs, source):
    """The camera source's OBS_MEDIA_STATE_..., or None if it isn't in OBS."""
    if _kinds(obs).get(source) != CAMERA_KIND:
        return None
    return obs.request("GetMediaInputStatus", {"inputName": source}).get("mediaState")


def ensure_camera(obs, *, scene, source, url, text_source=None, text_file=None, crop_left=0, crop_right=0):
    """Make sure `scene` holds the camera media source (reading `url`), cropped by crop_left /
    crop_right pixels (to keep a window or the room out of the picture), and, optionally, the
    status text source. Returns {"created": [names], "updated": [names]}."""
    out = {"created": [], "updated": []}
    kinds = _kinds(obs)
    # Validate before creating anything, so a clash leaves OBS untouched.
    if kinds.get(source, CAMERA_KIND) != CAMERA_KIND:
        raise ValueError(f"OBS already has a source called {source!r} that isn't a media source; "
                         "rename it or set camera.source")
    with_text = bool(text_source and text_file)
    if with_text and kinds.get(text_source, TEXT_KIND) != TEXT_KIND:
        raise ValueError(f"OBS already has a source called {text_source!r} that isn't a text source; "
                         "rename it or set camera.text_source")
    if not scene_exists(obs, scene):
        obs.request("CreateScene", {"sceneName": scene})
        out["created"].append(scene)
        in_scene = set()
    else:
        in_scene = {i["sourceName"] for i in obs.request("GetSceneItemList", {"sceneName": scene})["sceneItems"]}
    width, height = _canvas(obs)

    settings = _camera_settings(url)
    fit = {"positionX": 0, "positionY": 0, "alignment": LEFT | 4, "boundsType": "OBS_BOUNDS_SCALE_INNER",
           "boundsWidth": width, "boundsHeight": height, "boundsAlignment": 0}
    r = _ensure_input(obs, scene, source, CAMERA_KIND, kinds, in_scene, settings, settings,
                      lambda cur: cur.get("input") != url or cur.get("is_local_file") is not False, fit)
    if _ensure_crop(obs, source, crop_left, crop_right) and r is None:
        r = "updated"
    if r:
        out[r].append(source)

    if with_text:
        font = {"face": "Sans Serif", "size": 36, "style": "Bold"}
        create = {"from_file": True, "text_file": text_file, "font": font, "outline": True, "drop_shadow": True}
        apply = {"from_file": True, "text_file": text_file}
        pos = {"positionX": MARGIN, "positionY": height - MARGIN, "alignment": LEFT | BOTTOM}
        r = _ensure_input(obs, scene, text_source, TEXT_KIND, kinds, in_scene, create, apply,
                          lambda cur: cur.get("text_file") != text_file or cur.get("from_file") is not True, pos)
        if r:
            out[r].append(text_source)
    return out


def grab_frame(obs, source, width=1280):
    """JPEG bytes of the camera input only, taken in memory."""
    if _kinds(obs).get(source) != CAMERA_KIND:
        raise ValueError(f"{source!r} is not a media source in OBS")
    r = obs.request("GetSourceScreenshot", {"sourceName": source, "imageFormat": "jpg",
                                            "imageWidth": int(width), "imageCompressionQuality": 85})
    data = r.get("imageData") or ""
    head, sep, body = data.partition(",")
    if not sep or not head.startswith("data:image/") or ";base64" not in head:
        raise RuntimeError("OBS returned no camera image")
    try:
        raw = base64.b64decode(body, validate=True)
    except ValueError as e:
        raise RuntimeError("OBS returned an unreadable camera image") from e
    if not raw:
        raise RuntimeError("OBS returned an empty camera image")
    return raw


def switch_scene(obs, scene):
    obs.request("SetCurrentProgramScene", {"sceneName": scene})
