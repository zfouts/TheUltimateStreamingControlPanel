"""twitch_api: the control panel's Twitch routes. Thin wrappers over the Twitch module
(modules/twitch.py), which does the validation, scope checks and token handling; nothing
here ever sees or returns a token."""
from . import ApiError


def register(router, ed):
    def tw():
        mod = ed.bot.get_module("Twitch")
        if not mod:
            raise ApiError("the Twitch module isn't loaded", 503)
        return mod

    def ok(**extra):
        return {"ok": True, **extra}

    # ----- login

    @router.get("/api/twitch/auth")
    def auth(query):
        return tw().auth_state()

    @router.post("/api/twitch/login")
    def login(body):
        mod = tw()
        mod.login(open_browser=False)
        return mod.auth_state()

    @router.post("/api/twitch/logout")
    def logout(body):
        return ok(revoked=tw().logout()["revoked"])

    @router.post("/api/twitch/config")
    def config(body):
        return ok(relink=tw().set_config(body.get("channel"), body.get("client_id"))["relink"])

    # ----- channel

    @router.get("/api/twitch/channel")
    def channel(query):
        return tw().channel_state()

    @router.post("/api/twitch/channel")
    def update_channel(body):
        mod = tw()
        mod.update_channel(body.get("title"), body.get("game_id"), body.get("game_name"), body.get("tags"))
        return ok(channel=mod.channel_state())

    @router.get("/api/twitch/categories")
    def categories(query):
        return {"categories": tw().search_categories(query.get("q", ""))}

    @router.get("/api/twitch/stream")
    def stream(query):
        return tw().stream_status()

    # ----- chat

    @router.get("/api/twitch/chat/settings")
    def chat_settings(query):
        return tw().chat_settings_state()

    @router.post("/api/twitch/chat/settings")
    def set_chat_settings(body):
        mod = tw()
        mod.set_chat_mode(body.get("mode"), body.get("on"), body.get("seconds"))
        return mod.chat_settings_state()

    @router.post("/api/twitch/chat/send")
    def chat_send(body):
        tw().send_chat(body.get("text"))
        return ok()

    @router.post("/api/twitch/chat/delete")
    def chat_delete(body):
        tw().delete_message(body.get("id"))
        return ok()

    @router.post("/api/twitch/chat/ban")
    def chat_ban(body):
        tw().ban(body.get("user_id"), seconds=body.get("seconds"), reason=body.get("reason") or "",
                 permanent=body.get("permanent", False))
        return ok()

    @router.post("/api/twitch/chat/announce")
    def chat_announce(body):
        tw().announce(body.get("text"), body.get("color") or "primary")
        return ok()

    # ----- stream actions

    @router.post("/api/twitch/clip")
    def clip(body):
        return ok(url=tw().clip())

    @router.post("/api/twitch/marker")
    def marker(body):
        desc = body.get("description")
        tw().marker("Stream Deck" if desc is None else desc)
        return ok()

    @router.post("/api/twitch/ad")
    def ad(body):
        seconds = body.get("seconds")
        if not isinstance(seconds, int) or isinstance(seconds, bool):
            raise ApiError("seconds must be a number")
        return ok(seconds=tw().ad(seconds))

    @router.post("/api/twitch/raid")
    def raid(body):
        tw().raid(body.get("login"))
        return ok()

    @router.post("/api/twitch/raid/cancel")
    def raid_cancel(body):
        tw().cancel_raid()
        return ok()

    @router.post("/api/twitch/shoutout")
    def shoutout(body):
        tw().shoutout(body.get("login"))
        return ok()
