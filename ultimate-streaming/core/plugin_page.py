"""Plugin pages: the control-panel page a plugin's module describes with panel_page() (see
core/module.py), checked and normalised here, and the read and write side of its settings form.
core/webapi/plugin_api.py serves it.

Everything here is pure (dicts in, dicts out) except the secret files: stage_secrets(),
commit_secrets(), discard_staged() and remove_secret_files() are the only functions that touch
the disk, and only state_file(field["file"]) of a declared secret field. Secret values are never
logged, returned or put in an error message.
"""
import logging
import math
import os
import re
import threading

from . import paths

log = logging.getLogger("deck.plugins")

ICONS = frozenset("""live deck twitch obs settings sun moon x check warn info play stop pause mic volume
send refresh camera link clip flag dots undo redo plus search trash copy edit grid layers clock home
monitor external lock bolt eye text code dial pointer bot sparkle key""".split())
DEFAULT_ICON = "bolt"
TYPES = ("text", "number", "bool", "select", "secret", "discord_webhook")
TONES = ("default", "primary", "danger")
ID_RE = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
WEBHOOK_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}\Z")   # a Discord registry id
FILE_RE = re.compile(r"[a-z0-9_.-]{1,64}\Z")
ENV_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
MAX_ACTIONS, MAX_FIELDS, MAX_OPTIONS, MAX_KEY_PARTS = 12, 40, 30, 3
TEXT_MAX, TEXT_CAP, SECRET_MAX = 200, 500, 500
MISSING = object()

PAGE_KEYS = {"title", "icon", "intro", "actions", "fields"}
ACTION_KEYS = {"id", "label", "icon", "tone", "confirm", "enabled", "hint"}
FIELD_KEYS = {"key", "label", "type", "group", "help"}
TYPE_KEYS = {"text": {"placeholder", "default", "required", "max_length"},
             "number": {"placeholder", "default", "min", "max", "whole"},
             "bool": {"default"},
             "select": {"default", "options"},
             "secret": {"placeholder", "required", "file", "config_key", "env"},
             "discord_webhook": {"required"}}
FIELD_OUT = ("key", "label", "type", "group", "help", "placeholder", "default", "required", "max_length",
             "min", "max", "whole", "options", "file", "config_key", "env")


class PageError(ValueError):
    """A panel_page() spec, or a dotted path in config.yaml, that can't be used; says why."""


# ---------------------------------------------------------------------- normalising a page

def _text(d, key, cap, where, required=False):
    """d[key] as stripped one-line text of at most cap characters; None if optional and unset."""
    value = d.get(key)
    if value is None or (not required and value == ""):
        if required:
            raise PageError(f"{where}: {key} is required")
        return None
    if not isinstance(value, str) or CONTROL_RE.search(value) or not value.strip() or len(value.strip()) > cap:
        raise PageError(f"{where}: {key} must be one line of text, 1 to {cap} characters")
    return value.strip()


def _bool(d, key, default, where):
    value = d.get(key, default)
    if not isinstance(value, bool):
        raise PageError(f"{where}: {key} must be true or false")
    return value


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _dotted(key, where, what="key"):
    if not isinstance(key, str):
        raise PageError(f"{where}: {what} must be text")
    parts = key.split(".")
    if len(parts) > MAX_KEY_PARTS or not all(ID_RE.match(p) for p in parts):
        raise PageError(f"{where}: {what} {key!r} must be up to {MAX_KEY_PARTS} dot-separated names "
                        "of lowercase letters, digits and _")
    return key


def _action(spec, i):
    where = f"actions[{i}]"
    if not isinstance(spec, dict):
        raise PageError(f"{where} must be a mapping")
    unknown = sorted(str(k) for k in spec if k not in ACTION_KEYS)
    if unknown:
        raise PageError(f"{where}: unknown keys {', '.join(unknown)}")
    action_id = spec.get("id")
    if not isinstance(action_id, str) or not ID_RE.match(action_id):
        raise PageError(f"{where}: id must be 1-32 lowercase letters, digits and _, starting with a letter")
    where = f"action {action_id!r}"
    icon = spec.get("icon")
    tone = spec.get("tone", "default")
    if tone not in TONES:
        raise PageError(f"{where}: tone must be one of {', '.join(TONES)}")
    return {"id": action_id, "label": _text(spec, "label", 24, where, required=True),
            "icon": icon if icon in ICONS else None, "tone": tone,
            "confirm": _text(spec, "confirm", 300, where), "enabled": _bool(spec, "enabled", True, where),
            "hint": _text(spec, "hint", 120, where)}


def _options(spec, where):
    options = spec.get("options")
    if not isinstance(options, (list, tuple)) or not 1 <= len(options) <= MAX_OPTIONS:
        raise PageError(f"{where}: options must be a list of 1 to {MAX_OPTIONS} [value, label] pairs")
    out, seen = [], set()
    for pair in options:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise PageError(f"{where}: each option must be a [value, label] pair")
        value, label = pair
        ok = (isinstance(value, str) and len(value) <= 64 and not CONTROL_RE.search(value)) or \
            (isinstance(value, int) and not isinstance(value, bool))
        if not ok:
            raise PageError(f"{where}: option values must be text (at most 64 characters) or whole numbers")
        if (type(value), value) in seen:
            raise PageError(f"{where}: option {value!r} is listed twice")
        seen.add((type(value), value))
        out.append([value, _text({"label": label}, "label", 40, f"{where} option {value!r}", required=True)])
    return out


def _field(spec, i):
    where = f"fields[{i}]"
    if not isinstance(spec, dict):
        raise PageError(f"{where} must be a mapping")
    kind = spec.get("type")
    if kind not in TYPES:
        raise PageError(f"{where}: type must be one of {', '.join(TYPES)}")
    unknown = sorted(str(k) for k in spec if k not in FIELD_KEYS | TYPE_KEYS[kind])
    if unknown:
        raise PageError(f"{where}: unknown keys for a {kind} field: {', '.join(unknown)}")
    key = _dotted(spec.get("key"), where)
    where = f"field {key!r}"
    out = dict.fromkeys(FIELD_OUT)
    out.update(key=key, type=kind, label=_text(spec, "label", 40, where, required=True),
               group=_text(spec, "group", 24, where) or "Settings", help=_text(spec, "help", 200, where),
               placeholder=_text(spec, "placeholder", 60, where))
    if kind in ("text", "secret", "discord_webhook"):
        out["required"] = _bool(spec, "required", False, where)
    else:
        out["required"] = False
    if kind == "text":
        max_length = spec.get("max_length", TEXT_MAX)
        if isinstance(max_length, bool) or not isinstance(max_length, int) or not 1 <= max_length <= TEXT_CAP:
            raise PageError(f"{where}: max_length must be a whole number from 1 to {TEXT_CAP}")
        out["max_length"] = max_length
        out["default"] = ""
    elif kind == "number":
        for bound in ("min", "max"):
            if spec.get(bound) is not None and not _number(spec[bound]):
                raise PageError(f"{where}: {bound} must be a number")
            out[bound] = spec.get(bound)
        if out["min"] is not None and out["max"] is not None and out["min"] > out["max"]:
            raise PageError(f"{where}: min is larger than max")
        out["whole"] = _bool(spec, "whole", True, where)
    elif kind == "bool":
        out["default"] = False
    elif kind == "discord_webhook":
        out["default"] = ""
    elif kind == "select":
        out["options"] = _options(spec, where)
        out["default"] = out["options"][0][0]
    else:   # secret
        file = spec.get("file")
        if not isinstance(file, str) or not FILE_RE.match(file) or file.startswith("."):
            raise PageError(f"{where}: file must be 1-64 of a-z 0-9 _ . - (not starting with .)")
        out["file"] = file
        out["config_key"] = _dotted(spec.get("config_key"), where, "config_key")
        env = spec.get("env")
        if env is not None and (not isinstance(env, str) or not ENV_RE.match(env)):
            raise PageError(f"{where}: env must be an environment variable name (A-Z, 0-9, _)")
        out["env"] = env
    if "default" in spec:
        value, problem = check_value(out, spec["default"])
        if problem:
            raise PageError(f"{where}: default {problem}")
        out["default"] = value
    return out


def _paths_overlap(a, b):
    return a == b or a.startswith(b + ".") or b.startswith(a + ".")


def check_page(spec):
    """(normalised page, None) for a valid panel_page() spec, (None, why) for an invalid one,
    (None, None) for None (no page)."""
    if spec is None:
        return None, None
    try:
        if not isinstance(spec, dict):
            raise PageError("panel_page() must return a dict or None")
        unknown = sorted(str(k) for k in spec if k not in PAGE_KEYS)
        if unknown:
            raise PageError(f"unknown keys {', '.join(unknown)}")
        actions, fields = spec.get("actions") or [], spec.get("fields") or []
        if not isinstance(actions, (list, tuple)) or len(actions) > MAX_ACTIONS:
            raise PageError(f"actions must be a list of at most {MAX_ACTIONS}")
        if not isinstance(fields, (list, tuple)) or len(fields) > MAX_FIELDS:
            raise PageError(f"fields must be a list of at most {MAX_FIELDS}")
        actions = [_action(a, i) for i, a in enumerate(actions)]
        fields = [_field(f, i) for i, f in enumerate(fields)]
        ids = [a["id"] for a in actions]
        if len(set(ids)) != len(ids):
            raise PageError("two actions have the same id")
        keys = [f["key"] for f in fields]
        if len(set(keys)) != len(keys):
            raise PageError("two fields have the same key")
        files = [f["file"] for f in fields if f["type"] == "secret"]
        if len(set(files)) != len(files):
            raise PageError("two secret fields use the same file")
        # Every path written into the section: a field's key, or a secret's config_key.
        written = [f["config_key"] if f["type"] == "secret" else f["key"] for f in fields]
        for i, a in enumerate(written):
            for b in written[i + 1:]:
                if _paths_overlap(a, b):
                    raise PageError(f"config keys {a!r} and {b!r} overlap")
        icon = spec.get("icon")
        page = {"title": _text(spec, "title", 24, "page", required=True),
                "icon": icon if icon in ICONS else DEFAULT_ICON,
                "intro": _text(spec, "intro", 200, "page"), "actions": actions, "fields": fields}
    except PageError as e:
        return None, str(e)
    return page, None


_logged = {}
_logged_lock = threading.Lock()


def log_once(who, problem):
    """Log `problem` for `who` (a plugin, a module) unless it is the one last logged for it;
    problem=None forgets it, so the same problem coming back is logged again."""
    with _logged_lock:
        if problem is None:
            _logged.pop(who, None)
            return
        if _logged.get(who) == problem:
            return
        _logged[who] = problem
    log.warning("%s: no panel page: %s", who, problem)


def normalise_page(spec, who=None):
    """The normalised page for a panel_page() result, or None (no page, or an invalid one; why is
    logged once per `who`)."""
    page, problem = check_page(spec)
    if who is not None:
        log_once(who, problem)
    return page


# ---------------------------------------------------------------------- dotted paths

def get_path(section, key, default=MISSING):
    """section["a"]["b"] for key "a.b", or default when any part is missing or not a mapping."""
    node = section
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def set_path(section, key, value):
    """Set section["a"]["b"] = value for key "a.b", creating mappings on the way. PageError if a
    part on the way is set to something that isn't a mapping (it is never overwritten)."""
    parts = key.split(".")
    node = section
    for i, part in enumerate(parts[:-1]):
        nxt = node.get(part)
        if nxt is None:
            nxt = node[part] = {}
        elif not isinstance(nxt, dict):
            raise PageError(f"config.yaml has {'.'.join(parts[:i + 1])} set to something other than "
                            "a mapping; fix it in the Config view first")
        node = nxt
    node[parts[-1]] = value


def del_path(section, key):
    """Remove section["a"]["b"] for key "a.b" if it is there."""
    parts = key.split(".")
    parent = get_path(section, ".".join(parts[:-1]), None) if len(parts) > 1 else section
    if isinstance(parent, dict):
        parent.pop(parts[-1], None)


# ---------------------------------------------------------------------- values

def check_value(field, value, webhook_ids=None):
    """(normalised value, None) if value suits the (non-secret) field, else (None, problem).
    webhook_ids: the ids in the Discord registry, for a discord_webhook field; None = unknown, so
    any well-formed id passes."""
    kind = field["type"]
    if kind == "text":
        if not isinstance(value, str):
            return None, "must be text"
        value = value.strip()
        if CONTROL_RE.search(value):
            return None, "must be one line of text without control characters"
        if len(value) > field["max_length"]:
            return None, f"must be at most {field['max_length']} characters"
        return value, None
    if kind == "number":
        if not _number(value):
            return None, "must be a number"
        low, high = field["min"], field["max"]
        if (low is not None and value < low) or (high is not None and value > high):
            span = (f"from {low} to {high}" if low is not None and high is not None
                    else f"at least {low}" if low is not None else f"at most {high}")
            return None, f"must be {span}"
        if field["whole"]:
            if not float(value).is_integer():
                return None, "must be a whole number"
            value = int(value)
        return value, None
    if kind == "bool":
        return (value, None) if isinstance(value, bool) else (None, "must be true or false")
    if kind == "discord_webhook":
        if not isinstance(value, str):
            return None, "must be text"
        value = value.strip()
        if value == "":
            return value, None
        known = WEBHOOK_ID_RE.match(value) if webhook_ids is None else value in webhook_ids
        return (value, None) if known else (None, "must be one of the Discord webhooks (Settings → Discord)")
    if kind == "select":
        for option, _label in field["options"]:
            if type(option) is type(value) and option == value:
                return value, None
        return None, "must be one of the listed choices"
    return None, "can't be set here"


def field_values(fields, section, webhook_ids=None):
    """{key: value} for every non-secret field: config.yaml's value when it is present and valid,
    else the field's default. webhook_ids: see check_value."""
    section = section if isinstance(section, dict) else {}
    out = {}
    for f in fields:
        if f["type"] == "secret":
            continue
        raw = get_path(section, f["key"])
        value, problem = (None, "missing") if raw is MISSING else check_value(f, raw, webhook_ids)
        out[f["key"]] = f["default"] if problem else value
    return out


def secret_path(field):
    """Where the panel keeps a secret field's value: state_file(field["file"])."""
    return os.path.join(paths.state_dir(), field["file"])


def _read_first_line(path):
    try:
        with open(os.path.expanduser(path), encoding="utf-8", errors="replace") as f:
            return bool(f.readline().strip())
    except (OSError, ValueError):
        return False


def secret_status(fields, section, environ=None):
    """{key: {"set": bool, "source": "env" | "file" | None}} for every secret field. Never holds
    a value: the file is only checked for a non-empty first line, as read_secret() reads it."""
    environ = os.environ if environ is None else environ
    section = section if isinstance(section, dict) else {}
    out = {}
    for f in fields:
        if f["type"] != "secret":
            continue
        if f["env"] and environ.get(f["env"], "").strip():
            out[f["key"]] = {"set": True, "source": "env"}
            continue
        path = get_path(section, f["config_key"], None)
        if isinstance(path, str) and path.strip() and _read_first_line(path.strip()):
            out[f["key"]] = {"set": True, "source": "file"}
        else:
            out[f["key"]] = {"set": False, "source": None}
    return out


def check_secret(value):
    """(stripped value, None) or (None, problem); the problem never quotes the value."""
    if not isinstance(value, str):
        return None, "must be text"
    value = value.strip()
    if not value:
        return None, "can't be empty (use Remove to clear it)"
    if len(value) > SECRET_MAX:
        return None, f"must be at most {SECRET_MAX} characters"
    if CONTROL_RE.search(value):
        return None, "must be one line without control characters"
    return value, None


def _show(key):
    text = key if isinstance(key, str) else repr(key)
    return CONTROL_RE.sub("?", text)[:40]


def apply_settings(fields, section, values, secrets, webhook_ids=()):
    """Apply a settings form to `section` (a copy of the plugin's config section, changed in
    place) and return the problems as ["<label>: <problem>", ...] ([] means it is all valid).

    values: {field key: value} for non-secret fields; a value equal to the field's default is
    only written when config.yaml already has that key. secrets: {secret field key: text | None};
    text points the field's config_key at secret_path(field) (the file itself is written later by
    stage_secrets/commit_secrets), None removes the config_key. Keys absent from either are left
    alone. Unknown keys are problems. webhook_ids: the Discord registry's ids, the only non-empty
    values a discord_webhook field takes. No disk access."""
    errors = []
    if not isinstance(values, dict):
        return ["values must be an object"]
    if not isinstance(secrets, dict):
        return ["secrets must be an object"]
    by_key = {f["key"]: f for f in fields}
    for key, raw in values.items():
        f = by_key.get(key)
        if f is None or f["type"] == "secret":
            errors.append(f"{_show(key)}: not a setting of this plugin"
                          if f is None else f"{f['label']}: is a secret; send it in secrets")
            continue
        value, problem = check_value(f, raw, webhook_ids)
        if not problem and f["required"] and value == "":
            problem = "is required"
        if problem:
            errors.append(f"{f['label']}: {problem}")
            continue
        present = get_path(section, key) is not MISSING
        if not present and type(value) is type(f["default"]) and value == f["default"]:
            continue
        try:
            set_path(section, key, value)
        except PageError as e:
            errors.append(f"{f['label']}: {e}")
    for key, raw in secrets.items():
        f = by_key.get(key)
        if f is None or f["type"] != "secret":
            errors.append(f"{_show(key)}: not a secret of this plugin")
            continue
        try:
            if raw is None:
                if f["required"]:
                    errors.append(f"{f['label']}: is required")
                else:
                    del_path(section, f["config_key"])
                continue
            _value, problem = check_secret(raw)
            if problem:
                errors.append(f"{f['label']}: {problem}")
                continue
            set_path(section, f["config_key"], secret_path(f))
        except PageError as e:
            errors.append(f"{f['label']}: {e}")
    return errors


# ---------------------------------------------------------------------- secret files

def stage_secrets(fields, secrets):
    """Write each new secret (a str in `secrets`, already checked by apply_settings) to a private
    temp file next to its final place, created 0600. Returns [(temp, final)] for commit_secrets()
    or discard_staged(); nothing is in place yet. Removes what it wrote if one write fails."""
    by_key = {f["key"]: f for f in fields if f["type"] == "secret"}
    staged = []
    try:
        for key, raw in secrets.items():
            if raw is None:
                continue
            value, problem = check_secret(raw)
            if problem or key not in by_key:   # apply_settings refused it already
                raise PageError(f"{key}: not a valid secret")
            final = paths.state_file(by_key[key]["file"])   # creates the state dir, 0700
            tmp = f"{final}.tmp-{os.getpid()}-{threading.get_ident()}"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            staged.append((tmp, final))
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(value + "\n")
                f.flush()
                os.fsync(f.fileno())
    except BaseException:
        discard_staged(staged)
        raise
    return staged


def commit_secrets(staged):
    """Move staged secret files into place (atomic rename; they stay 0600)."""
    for tmp, final in staged:
        os.replace(tmp, final)


def discard_staged(staged):
    """Remove staged temp files that weren't committed."""
    for tmp, _final in staged:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass


def remove_secret_files(fields, old_section, secrets):
    """For each secret removed (None in `secrets`): delete its file, but only when the section had
    its config_key pointing at the panel's own file (secret_path); a file you named yourself stays."""
    old_section = old_section if isinstance(old_section, dict) else {}
    for f in fields:
        if f["type"] != "secret" or f["key"] not in secrets or secrets[f["key"]] is not None:
            continue
        path = get_path(old_section, f["config_key"], None)
        if not isinstance(path, str):
            continue
        own = secret_path(f)
        if os.path.abspath(os.path.expanduser(path.strip())) == os.path.abspath(own):
            try:
                os.remove(own)
            except FileNotFoundError:
                pass
