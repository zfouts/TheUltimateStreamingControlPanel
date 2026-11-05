"""bot_api: the control panel's Twitch Bot routes. Thin wrappers over the TwitchBot module
(modules/twitch_bot.py), which validates with the same rules it applies to config.yaml."""
import copy
import re

from .. import chatcmd, configio
from . import ApiError

SECRET_QUERY = re.compile(r"(?i)pass|secret|token|key|auth|cred")


def mask_database_url(url):
    """A SQLAlchemy URL with its password and secret-looking query values shown as ***
    ("***" alone if it can't be parsed). Used wherever config.yaml goes to a browser."""
    if not isinstance(url, str) or not url.strip():
        return url
    try:
        import sqlalchemy as sa
        u = sa.engine.make_url(url.strip())
        query = {k: ("***" if SECRET_QUERY.search(k) else v) for k, v in u.query.items()}
        return u.set(query=query).render_as_string(hide_password=True)
    except Exception:
        return "***"


def mask_config(cfg):
    """A copy of a whole config with twitch_bot.database_url masked (the general editor's view)."""
    section = cfg.get("twitch_bot") if isinstance(cfg, dict) else None
    if not isinstance(section, dict) or not section.get("database_url"):
        return cfg
    return {**cfg, "twitch_bot": {**section, "database_url": mask_database_url(section["database_url"])}}


def restore_secrets(incoming, on_disk):
    """Before the general editor saves: a twitch_bot.database_url that comes back masked, or not
    at all, keeps the real one from config.yaml."""
    new = incoming.get("twitch_bot") if isinstance(incoming, dict) else None
    old = on_disk.get("twitch_bot") if isinstance(on_disk, dict) else None
    real = old.get("database_url") if isinstance(old, dict) else None
    if isinstance(new, dict) and isinstance(real, str) and real:
        sent = new.get("database_url")
        if sent is None or sent == mask_database_url(real):
            new["database_url"] = real
    return incoming


def register(router, ed):
    def bot():
        mod = ed.bot.get_module("TwitchBot")
        if not mod:
            raise ApiError("the Twitch Bot module isn't loaded", 503)
        return mod

    def ident(body, name="id"):
        value = body.get(name)
        if not isinstance(value, str) or not value:
            raise ApiError(f"{name} must be text")
        return value

    def state():
        mod = bot()
        es = ed.bot.get_module("EventSub")
        eventsub = None
        if es:
            try:
                eventsub = es.snapshot()
            except Exception as e:
                eventsub = {"connected": False, "subscriptions": [], "error": str(e)[:200] or type(e).__name__}
        if not isinstance(eventsub, dict):
            eventsub = {"connected": False, "subscriptions": [],
                        "error": None if es else "the EventSub module isn't loaded"}
        tw = ed.bot.get_module("Twitch")
        missing = [s for s in tw.missing_scopes(network=False) if s in mod.required_scopes] \
            if tw and tw.linked() else []
        return {"config": mod.config_view(), "status": mod.snapshot(), "eventsub": eventsub, "scopes_missing": missing,
                "hash": configio.file_hash(ed.bot.config_path), "stats": mod.stats_view(),
                "builtins": chatcmd.describe()}

    @router.get("/api/bot")
    def get_bot(query):
        return state()

    @router.post("/api/bot/config")
    def config(body):
        bot().save_config(body.get("config"), body.get("hash"))   # a stale hash is a 409
        return state()

    @router.post("/api/bot/enable")
    def enable(body):
        return {"ok": True, "enabled": bot().set_enabled(body.get("on"))}

    @router.post("/api/bot/timer/send")
    def timer_send(body):
        bot().send_timer(ident(body))
        return {"ok": True}

    @router.post("/api/bot/command/test")
    def command_test(body):
        return bot().command_test(ident(body), body.get("user"))     # {text[, sample: true]}

    @router.get("/api/bot/rewards")
    def rewards(query):
        return {"rewards": bot().twitch_rewards()}

    @router.post("/api/bot/rewards/sync")
    def rewards_sync(body):
        return bot().sync_rewards()

    @router.post("/api/bot/reward/test")
    def reward_test(body):
        return bot().test_reward(ident(body), body.get("input"))

    @router.post("/api/bot/reward/delete")
    def reward_delete(body):
        return bot().delete_reward(ident(body))

    # ----- default commands and chat stats

    @router.get("/api/bot/defaults")
    def defaults(query):
        return {"commands": copy.deepcopy(type(bot()).default_commands)}

    @router.get("/api/bot/stats")
    def stats(query):
        n = query.get("n", "25")
        if not isinstance(n, str) or not re.fullmatch(r"[0-9]{1,3}", n):
            raise ApiError("n must be a whole number from 1 to 100")
        return bot().stats_top(query.get("period", "all"), int(n))

    @router.get("/api/bot/stats/user")
    def stats_user(query):
        return bot().stats_user(query.get("login"))

    @router.post("/api/bot/stats/reset")
    def stats_reset(body):
        return bot().reset_stats(body.get("scope"), body.get("confirm"))
