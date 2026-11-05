"""live_api: the web panel's live mirror of the Stream Deck, plus module snapshots and logs.

    GET  /api/live/decks       every attached deck (info)
    POST /api/devices/identify {serial}: flash that deck white 3x
    (all /api/live deck routes take an optional `deck` serial; omitted = primary)
    GET  /api/live/deck        the current page, every key rendered as the hardware shows it
    GET  /api/live/key?page=&key=  one key of the current page, same shape as a deck entry
    POST /api/live/press       {page, key}: press a key through the real key path
    POST /api/live/page        {page}: switch page ("back" allowed)
    POST /api/live/brightness  {level}: 0..100
    GET  /api/live/snapshot    every module's snapshot()
    GET  /api/live/log?n=      recent log lines
    GET  /api/live/chat?n=     recent chat messages
"""
import base64
import io
import threading
import time

from core.render import Renderer
from core.webapi import ApiError

_renderers = {}  # (w, h, font, emoji_font) -> Renderer
_render_lock = threading.Lock()  # Renderer.image keeps per-call scale state on the instance


class _SizedDeck:
    """Just enough of a deck for the renderer: a key size."""

    def __init__(self, size):
        self.size = tuple(size)

    def key_image_format(self):
        return {"size": self.size}


def _renderer(bot, size):
    key = (*size, str(bot.cfg.get("font")), str(bot.cfg.get("emoji_font")))
    renderer = _renderers.get(key)
    if not renderer:
        if len(_renderers) > 8:
            _renderers.clear()
        renderer = _renderers[key] = Renderer(_SizedDeck(size), bot.root, bot.cfg.get("font"),
                                              bot.cfg.get("emoji_font"))
    return renderer


def _png(renderer, btn):
    buf = io.BytesIO()
    with _render_lock:
        img = renderer.image(btn)
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _look(sess, buttons, page, key):
    """sess.appearance() for one key, from a precomputed sess.buttons(page) (call under bot.lock)."""
    btn = buttons.get(key)
    if not btn:
        return {}
    active = btn.get("active") or btn.get(True) or btn.get("on")
    if sess.states.get((page, key)) and active:
        btn = {**btn, **active}
    btn = {**btn, **sess.displays.get((page, key), {})}
    if sess.armed and sess.armed[:2] == (page, key):
        btn = {**btn, "label": btn["confirm"] if isinstance(btn["confirm"], str) else "Press\nagain",
               "bg": "#d07000"}
    return btn


def _key_out(sess, ed, renderer, page, i, btn, armed):
    image = None
    if btn:
        try:
            image = _png(renderer, btn)
        except Exception as e:
            ed.log.debug("live render key %d: %s", i, e)
    return {"index": i, "label": str(btn.get("label") or "") if btn else "", "image": image,
            "active": bool(btn) and bool(sess.states.get((page, i))),
            "has_action": bool(btn) and btn.get("action") is not None,
            "confirm": bool(btn) and bool(btn.get("confirm")), "armed": armed}


def _count(query, default=200):
    try:
        n = int(query.get("n", default))
    except (TypeError, ValueError):
        raise ApiError("n must be a number")
    return max(1, min(300, n))


def _resolve(bot, serial):
    """The session for `serial`, or the primary deck when omitted (None if nothing is attached)."""
    if serial is None or serial == "":
        return bot.primary
    if not isinstance(serial, str):
        raise ApiError("deck must be a string")
    sess = bot.session(serial)
    if sess is None:
        raise ApiError(f"No deck with serial {serial}", 404)
    return sess


def _white(sess):
    """A plain white key image, drawn through the deck's renderer."""
    return sess.renderer.render({"label": "", "bg": "#ffffff"})


def _identify(bot, sess, log):
    try:
        for _ in range(3):
            with bot.lock:
                if sess.detached or not sess.deck.is_visual():
                    return
                img = _white(sess)
                for key in range(sess.deck.key_count()):
                    sess._write(sess.deck.set_key_image, key, img)
            time.sleep(0.25)
            sess.draw_page()
            time.sleep(0.25)
    except Exception as e:
        log.debug("identify %s: %s", sess.serial, e)


def register(router, ed):
    bot = ed.bot

    @router.get("/api/live/decks")
    def decks(query):
        return {"decks": [s.info() for s in list(bot.sessions.values())]}

    @router.post("/api/devices/identify")
    def identify(body):
        serial = body.get("serial")
        if not isinstance(serial, str) or not serial:
            raise ApiError("serial must be a string")
        sess = _resolve(bot, serial)
        threading.Thread(target=_identify, args=(bot, sess, ed.log), name="identify", daemon=True).start()
        return {"ok": True}

    @router.get("/api/live/deck")
    def deck(query):
        with bot.lock:
            sess = _resolve(bot, query.get("deck"))
            if sess is None:
                return {"attached": False, "page": None, "pages": list(bot.pages), "can_back": False,
                        "brightness": bot.brightness, "layout": bot.layout_name, "rows": 0, "cols": 0,
                        "key_size": [0, 0], "keys": [], "dials": [], "model": None, "serial": None,
                        "name": None}
            dev, page = sess.deck, sess.page
            rows, cols = dev.key_layout()
            size = list(dev.key_image_format()["size"])
            count = dev.key_count()
            looks = []
            buttons = sess.buttons(page)
            for i in range(count):
                btn = _look(sess, buttons, page, i)
                looks.append((btn, bool(sess.armed and sess.armed[:2] == (page, i))))
            dials = [{"index": i, "label": str((d or {}).get("label", ""))}
                     for i, d in enumerate(sess.dials(page))]
            out = {"attached": True, "page": page, "pages": list(sess.pages), "can_back": bool(sess.history),
                   "brightness": sess.brightness, "layout": sess.layout_name, "rows": rows, "cols": cols,
                   "key_size": size, "dials": dials, "model": dev.deck_type(), "serial": sess.serial,
                   "name": sess.name}
        renderer = _renderer(bot, size)
        out["keys"] = [_key_out(sess, ed, renderer, page, i, btn, armed) for i, (btn, armed) in enumerate(looks)]
        return out

    @router.get("/api/live/key")
    def one_key(query):
        page = query.get("page")
        try:
            key = int(query.get("key"))
        except (TypeError, ValueError):
            raise ApiError("key must be an integer")
        with bot.lock:
            sess = _resolve(bot, query.get("deck"))
            if sess is None:
                raise ApiError("No deck attached", 503)
            if page != sess.page:
                raise ApiError("The deck changed page", 409)
            if not 0 <= key < sess.deck.key_count():
                raise ApiError("key out of range")
            size = list(sess.deck.key_image_format()["size"])
            btn = _look(sess, sess.buttons(page), page, key)
            armed = bool(sess.armed and sess.armed[:2] == (page, key))
        return {"page": page, "key": _key_out(sess, ed, _renderer(bot, size), page, key, btn, armed)}

    @router.post("/api/live/press")
    def press(body):
        page, key = body.get("page"), body.get("key")
        if not isinstance(page, str):
            raise ApiError("page must be a string")
        if not isinstance(key, int) or isinstance(key, bool):
            raise ApiError("key must be an integer")
        with bot.lock:
            sess = _resolve(bot, body.get("deck"))
            if sess is None:
                raise ApiError("No deck attached", 503)
            if page != sess.page:
                raise ApiError("The deck changed page", 409)
            if not 0 <= key < sess.deck.key_count():
                raise ApiError("key out of range")
            deck = sess.deck
        # Not under the lock: a press/release action runs right here, and while it ran the deck
        # (polling, its own key presses) would wait on it.
        try:
            bot.on_key(deck, key, True)
        finally:
            bot.on_key(deck, key, False)
        return {"ok": True}

    @router.post("/api/live/page")
    def page(body):
        name = body.get("page")
        if not isinstance(name, str):
            raise ApiError("page must be a string")
        with bot.lock:
            sess = _resolve(bot, body.get("deck"))
            if sess is None:
                raise ApiError("No deck attached", 503)
            if name != "back" and name not in sess.pages:
                raise ApiError("No such page")
            sess.goto(name)
            return {"ok": True, "page": sess.page}

    @router.post("/api/live/brightness")
    def brightness(body):
        level = body.get("level")
        if isinstance(level, bool) or not isinstance(level, (int, float)):
            raise ApiError("level must be a number")
        sess = _resolve(bot, body.get("deck"))
        if sess is None:
            bot.set_brightness(level)
            return {"ok": True, "brightness": bot.brightness}
        sess.set_brightness(level)
        return {"ok": True, "brightness": sess.brightness}

    @router.get("/api/live/snapshot")
    def snapshot(query):
        return ed.snapshot_all()

    @router.get("/api/live/log")
    def log(query):
        return {"lines": [{"ts": e.get("ts"), "level": e.get("level"), "logger": e.get("logger"),
                           "msg": e.get("msg")} for e in map(_event_data, ed.hub.history("log", _count(query)))]}

    @router.get("/api/live/chat")
    def chat(query):
        return {"messages": [_event_data(e) for e in ed.hub.history("chat", _count(query))]}


def _event_data(event):
    """A history envelope {type, ts, data} -> its payload, with a ts."""
    return {"ts": event.get("ts"), **(event.get("data") or {})}
