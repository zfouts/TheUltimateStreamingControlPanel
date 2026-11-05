"""discord_api: the control panel's Settings → Discord routes. Thin wrappers over the Discord
module (modules/discord.py), which validates with the same rules it applies to config.yaml.
Webhook URLs only ever come in (POST /api/discord/config); what goes out is {"set", "host"}."""
from .. import configio
from . import ApiError


def register(router, ed):
    def mod():
        m = ed.bot.get_module("Discord")
        if not m:
            raise ApiError("the Discord module isn't loaded", 503)
        return m

    def state():
        m = mod()
        return {"config": m.config_view(), "urls": m.urls_view(), "status": m.snapshot()["webhooks"],
                "used_by": m.used_by(), "hash": configio.file_hash(ed.bot.config_path)}

    @router.get("/api/discord")
    def get_discord(query):
        return state()

    @router.post("/api/discord/config")
    def config(body):
        # 400 problems (or a webhook still in use), 409 stale hash
        mod().save(body.get("config"), body.get("urls"), body.get("hash"))
        return state()

    @router.post("/api/discord/test")
    def test(body):
        return mod().test(body.get("id"))       # {"queued": true}; the result arrives in the status event
