"""announce_api: the control panel's go-live announcement routes. Thin wrappers over the Announce
module (modules/announce.py), which validates with the same rules it applies to config.yaml.
No webhook URL passes through here: a destination names a webhook from Settings → Discord
(discord_api), and "webhooks" lists those as {id, name, set}."""
from .. import configio
from . import ApiError


def register(router, ed):
    def mod():
        m = ed.bot.get_module("Announce")
        if not m:
            raise ApiError("the announcements module isn't loaded", 503)
        return m

    def state():
        m = mod()
        return {"config": m.config_view(), "webhooks": m.webhooks_view(), "status": m.snapshot(),
                "placeholders": ["channel", "title", "game", "url"], "sample": m.sample(),
                "hash": configio.file_hash(ed.bot.config_path)}

    @router.get("/api/announce")
    def get_announce(query):
        return state()

    @router.post("/api/announce/config")
    def config(body):
        if body.get("urls"):        # never stored: a URL sent here would be silently lost otherwise
            raise ApiError("Webhook URLs are saved in Settings → Discord; pick a webhook here instead.")
        mod().save(body.get("config"), body.get("hash"))     # 400 problems, 409 stale hash
        return state()

    @router.post("/api/announce/test")
    def test(body):
        return mod().test(body.get("id"))       # {"queued": true}; the result arrives in the status event

    @router.post("/api/announce/now")
    def now(body):
        return mod().announce_now()
