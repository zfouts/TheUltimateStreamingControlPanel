"""obs_api: the control panel's OBS routes (scenes, outputs, studio mode, mixer, stats)."""
import math

from core.webapi import ApiError

MAX_NAME = 200
UNREACHABLE = "OBS isn’t reachable"


def _name(body, key, required=True):
    v = body.get(key)
    if v is None and not required:
        return None
    if not isinstance(v, str) or not v or len(v) > MAX_NAME:
        raise ApiError(f"{key} must be a string of 1-{MAX_NAME} characters")
    return v


def _bool(body, key):
    v = body.get(key)
    if v is not None and not isinstance(v, bool):
        raise ApiError(f"{key} must be true or false")
    return v


def register(router, ed):
    def obs():
        m = ed.bot.get_module("OBS")
        if m is None:
            raise ApiError(UNREACHABLE, 503)
        return m

    def run(fn, *args):
        """Call into the OBS module: OBS refusing is a 400, OBS being away a 503."""
        m = obs()
        try:
            return fn(m, *args)
        except ApiError:
            raise
        except ValueError as e:
            raise ApiError(str(e), 400)
        except Exception as e:
            if m.is_request_error(e):
                raise ApiError("OBS refused the request", 400)
            ed.log.debug("OBS call failed: %s", type(e).__name__)
            raise ApiError(UNREACHABLE, 503)

    def object_body(body):
        if not isinstance(body, dict):
            raise ApiError("body must be a JSON object")
        return body

    @router.get("/api/obs/state")
    def state(query):
        s = run(lambda m: m.collect())
        o = s["outputs"]
        return {"ok": True, "scene": s["scene"], "preview": s["preview"],
                "scenes": [{"name": n, "index": i} for i, n in enumerate(s["scenes"])],
                "studio": s["studio"], "stream": o["stream"], "record": o["record"],
                "replay": o["replay"], "virtualcam": o["virtualcam"],
                "inputs": s["inputs"], "stats": s["stats"]}

    @router.post("/api/obs/scene")
    def scene(body):
        body = object_body(body)
        name, preview = _name(body, "name"), _bool(body, "preview")
        run(lambda m: m.set_scene(name, bool(preview)))
        return {"ok": True}

    @router.post("/api/obs/output")
    def output(body):
        body = object_body(body)
        what, action = body.get("what"), body.get("action")
        if what not in ("stream", "record", "replay", "virtualcam"):
            raise ApiError("what must be stream, record, replay or virtualcam")
        if action not in ("start", "stop", "toggle", "pause", "resume"):
            raise ApiError("action must be start, stop, toggle, pause or resume")
        if action in ("pause", "resume") and what != "record":
            raise ApiError("only the recording can be paused")
        return {"ok": True, "active": run(lambda m: m.set_output(what, action))}

    @router.post("/api/obs/input")
    def input_(body):
        body = object_body(body)
        name, muted = _name(body, "name"), _bool(body, "muted")
        db = body.get("volume_db")
        if db is not None and (isinstance(db, bool) or not isinstance(db, (int, float)) or not math.isfinite(db)):
            raise ApiError("volume_db must be a number")
        if muted is None and db is None:
            raise ApiError("nothing to change: give muted and/or volume_db")

        def apply(m):
            if muted is not None:
                m.set_mute(name, muted)
            if db is not None:
                m.set_volume(name, db)
        run(apply)
        return {"ok": True}

    @router.post("/api/obs/studio")
    def studio(body):
        body = object_body(body)
        enabled, trans = _bool(body, "enabled"), _bool(body, "transition")

        def apply(m):
            if enabled is not None:
                m.set_studio(enabled)
            if trans:
                m.transition()
            return m.output_active("studio")
        return {"ok": True, "studio": run(apply)}

    @router.post("/api/obs/screenshot")
    def screenshot(body):
        return {"ok": True, "path": run(lambda m: m.take_screenshot())}

    @router.post("/api/obs/request")
    def request(body):
        # Bodies and responses are deliberately never logged: they can carry anything.
        body = object_body(body)
        name, data = body.get("request"), body.get("data")
        if not isinstance(name, str) or not name.isascii() or not name.isalpha() or len(name) > MAX_NAME:
            raise ApiError("request must be an obs-websocket request name (letters only)")
        if data is not None and not isinstance(data, dict):
            raise ApiError("data must be an object")
        return {"ok": True, "response": run(lambda m: m.request(name, data))}
