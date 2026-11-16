"""automod_api: the control panel's Twitch → Moderation routes. Thin wrappers over the AutoMod
module (modules/automod.py), which validates with the same rules it applies to config.yaml."""
from .. import configio
from . import ApiError


def register(router, ed):
    def mod():
        m = ed.bot.get_module("AutoMod")
        if not m:
            raise ApiError("the auto-mod module isn't loaded", 503)
        return m

    def state():
        return {**mod().state(), "hash": configio.file_hash(ed.bot.config_path)}

    @router.get("/api/automod")
    def get_automod(query):
        return state()

    @router.post("/api/automod/config")
    def config(body):
        mod().save(body.get("config"), body.get("hash"))     # 400 problems, 409 stale hash
        return state()

    @router.post("/api/automod/test")
    def test(body):
        return mod().test(body.get("text"), body.get("config"))

    @router.post("/api/automod/unban")
    def unban(body):
        mod().unban(body.get("id"))
        return state()
