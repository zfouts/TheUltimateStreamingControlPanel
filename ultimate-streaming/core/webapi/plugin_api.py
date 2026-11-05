"""plugin_api: the panel's pages for installed plugins (core/plugin_page.py).

    GET  /api/plugins/<name>             the page: title, actions, fields, values, secret status, hash
    POST /api/plugins/<name>/settings    {"values": {...}, "secrets": {...}, "hash": "..."}
    POST /api/plugins/<name>/action      {"id": "<action id>"}

plus plugins_status(bot), the `plugins` list of /api/status (modules/editor.py). A plugin's page
comes from the first of its modules whose panel_page() is valid; its settings live in that
module's config section. Secret values never come back: only whether one is set, and where from.
"""
import copy
import hashlib
import logging
import threading

from .. import configio, plugin_page, plugins
from . import ApiError

log = logging.getLogger("deck.plugins")

ERROR_CAP = 300
UNAVAILABLE = "That button isn't available right now"
STALE = "config.yaml changed since the panel loaded it; reload and try again"
_SAVE_LOCK = threading.Lock()   # one plugin settings save at a time (secret files + config.yaml)


# ---------------------------------------------------------------------- reading

def _instances(bot, name):
    ext = bot.extensions.get(f"plugin:{name}")
    return list(ext["instances"]) if ext else []


def page_of(bot, name):
    """(module, normalised page) for plugin `name`: the first of its modules with a valid
    panel_page(); (its first module, None) if none has one; (None, None) if it isn't loaded.
    A panel_page() that raises or returns something invalid is logged once."""
    mods = _instances(bot, name)
    for inst in mods:
        who = f"plugin {name} ({type(inst).__name__})"
        try:
            spec = inst.panel_page()
        except Exception as e:
            plugin_page.log_once(who, f"panel_page() failed: {type(e).__name__}: {str(e)[:200]}")
            continue
        page = plugin_page.normalise_page(spec, who=who)
        if page:
            return inst, page
    return (mods[0] if mods else None), None


def _item(name):
    for item in plugins.installed():
        if item["name"] == name:
            return item
    return None


def _load_error(bot, item):
    if item["error"]:
        text = item["error"]
    else:
        ext = bot.extensions.get(f"plugin:{item['name']}")
        if ext is None:
            text = "not loaded yet: the app loads new plugins within a few seconds"
        else:
            text = ext.get("error") or "it failed to load; the app's log says why"
    return text[:ERROR_CAP]


def _overview(bot, item):
    """name, title, icon, key, loaded, error, has_page, plus the module and page (internal)."""
    name = item["name"]
    inst, page = page_of(bot, name) if not item["error"] else (None, None)
    return {"name": name, "title": page["title"] if page else name,
            "icon": page["icon"] if page else plugin_page.DEFAULT_ICON,
            "key": inst.config_key if inst else None, "loaded": inst is not None,
            "error": None if inst else _load_error(bot, item), "has_page": page is not None,
            "_inst": inst, "_page": page}


def plugins_status(bot):
    """The `plugins` list of /api/status: one entry per installed plugin, sorted by title."""
    out = [_overview(bot, item) for item in plugins.installed()]
    for entry in out:
        del entry["_inst"], entry["_page"]
    return sorted(out, key=lambda e: (e["title"].lower(), e["name"]))


def _read_config(path):
    """(config, hash) from one read of config.yaml; ({}, "") when there is none."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        return None, ""
    return configio.safe_load_bounded(data) or {}, hashlib.sha256(data).hexdigest()


def _registry(bot):
    """[{id, name, set}] from the core Discord module; [] when there isn't one."""
    discord = bot.get_module("Discord")
    if discord is None:
        return []
    try:
        return [{"id": w["id"], "name": w["name"], "set": bool(w.get("set"))} for w in discord.webhooks()]
    except Exception:
        log.exception("reading the Discord webhooks failed")
        return []


def _wants_webhooks(fields):
    return any(f["type"] == "discord_webhook" for f in fields)


def detail(bot, name):
    item = _item(name)
    if item is None:
        raise ApiError(f"no plugin named {name!r} is installed", 404)
    view = _overview(bot, item)
    inst, page = view.pop("_inst"), view.pop("_page")
    view.pop("has_page")
    manifest = item["manifest"] or {}
    try:
        cfg, digest = _read_config(bot.config_path)
    except Exception as e:   # config.yaml doesn't parse: show the defaults; a save will refuse
        log.debug("plugin page %s: config.yaml: %s", name, type(e).__name__)
        cfg, digest = {}, configio.file_hash(bot.config_path)
    view.update(version=manifest.get("version"), description=manifest.get("description"),
                intro=page["intro"] if page else None, actions=[], fields=[], values={}, secrets={},
                hash=digest)
    if page:
        section = cfg.get(inst.config_key) if isinstance(cfg, dict) else None
        hooks = _registry(bot) if _wants_webhooks(page["fields"]) else None
        view.update(actions=page["actions"], fields=page["fields"],
                    values=plugin_page.field_values(page["fields"], section,
                                                    [h["id"] for h in hooks] if hooks is not None else None),
                    secrets=plugin_page.secret_status(page["fields"], section))
        if hooks is not None:
            view["discord_webhooks"] = hooks
    return view


def _loaded_page(bot, name):
    if _item(name) is None:
        raise ApiError(f"no plugin named {name!r} is installed", 404)
    inst, page = page_of(bot, name)
    if inst is None:
        raise ApiError(f"the {name} plugin isn't loaded", 409)
    return inst, page


# ---------------------------------------------------------------------- writing

def save_settings(bot, name, body):
    base = body.get("hash")
    if not isinstance(base, str):
        raise ApiError("hash must be text (from GET /api/plugins/<name>)")
    values = body.get("values", {})
    secrets = body.get("secrets", {})
    values = {} if values is None else values
    secrets = {} if secrets is None else secrets
    with _SAVE_LOCK:
        inst, page = _loaded_page(bot, name)
        if page is None or not page["fields"]:
            raise ApiError(f"the {name} plugin has no settings here", 404)
        fields, key, path = page["fields"], inst.config_key, bot.config_path
        cfg, digest = _read_config(path)
        if cfg is None:
            raise ApiError("there's no config.yaml yet; set up the deck first", 409)
        if not isinstance(cfg, dict):
            raise ApiError("config.yaml isn't a mapping; fix it in the Config view first")
        if digest != base:
            raise ApiError(STALE, 409)
        old = cfg.get(key)
        if old is not None and not isinstance(old, dict):
            raise ApiError(f"config.yaml's {key}: section isn't a mapping; fix it in the Config view first")
        old = old or {}
        section = copy.deepcopy(old)
        errors = plugin_page.apply_settings(fields, section, values, secrets,
                                            [h["id"] for h in _registry(bot)])
        if errors:
            shown = errors[:20] + ([f"…and {len(errors) - 20} more"] if len(errors) > 20 else [])
            e = ApiError("Fix these first: " + "; ".join(shown), 400)
            e.errors = errors
            raise e
        # Secrets go to temp files first and into place only once config.yaml is saved, so a
        # refused save (409) changes nothing.
        staged = plugin_page.stage_secrets(fields, secrets)
        try:
            if section != old:
                cfg[key] = section
                try:
                    configio.save(path, cfg, base_hash=base, source="panel")
                except configio.Conflict:
                    raise ApiError(STALE, 409) from None
            plugin_page.commit_secrets(staged)
            staged = []
            plugin_page.remove_secret_files(fields, old, secrets)
        finally:
            plugin_page.discard_staged(staged)
    _apply(bot, name, secrets_changed=bool(secrets))
    return detail(bot, name)


def _apply(bot, name, secrets_changed):
    """Make the daemon use the saved settings now, as its main loop would on a config change.
    A secret file changes without config.yaml changing (or after the main loop already reloaded
    it), so the plugin's modules then get their section again to re-read it."""
    try:
        changed = bot.load_config()
        if changed:
            bot.apply_config()
    except Exception:
        log.exception("applying the %s plugin's settings failed", name)
        changed = False
    if secrets_changed and not changed:
        with bot.lock:
            for inst in _instances(bot, name):
                try:
                    inst.on_config(bot.cfg.get(inst.config_key) or {})
                except Exception:
                    log.exception("%s rejected the config", type(inst).__name__)


def run_action(bot, name, body):
    action_id = body.get("id")
    if not isinstance(action_id, str):
        raise ApiError("id must be text")
    inst, page = _loaded_page(bot, name)
    act = next((a for a in (page or {}).get("actions", []) if a["id"] == action_id), None)
    if act is None or not act["enabled"]:
        raise ApiError(UNAVAILABLE)
    try:
        result = inst.panel_action(action_id)
    except ApiError:
        raise
    except Exception as e:
        log.warning("plugin %s: button %r failed: %s", name, action_id, type(e).__name__, exc_info=True)
        raise ApiError(str(e)[:ERROR_CAP] or type(e).__name__) from None
    message = result.get("message") if isinstance(result, dict) else None
    return {"ok": True, "message": message[:ERROR_CAP] if isinstance(message, str) and message else None}


def register(router, ed):
    name = r"/api/plugins/([a-z][a-z0-9_]{1,31})"

    @router.get_match(name)
    def get_plugin(m, query):
        return detail(ed.bot, m.group(1))

    @router.post_match(name + "/settings")
    def post_settings(m, body):
        return save_settings(ed.bot, m.group(1), body)

    @router.post_match(name + "/action")
    def post_action(m, body):
        return run_action(ed.bot, m.group(1), body)
