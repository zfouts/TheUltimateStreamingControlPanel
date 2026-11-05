"""Field descriptions for the web editor's forms.

Modules pass these to their decorators so the editor can build a form for each
action / state / display without knowing about the module:

    from core.schema import UI, Choice, Source, Text

    @action("obs_scene", ui=UI("Switch OBS scene", "OBS", arg=Source("obs_scenes", "Scene")))

An action's argument is either one field (`arg=`) or a mapping (`args={...}`). With
`args` plus `scalar="field"`, a plain value in the config means that field, and the
editor writes a plain value back when only that field is set (e.g. `countdown: 300`).
"""


def UI(label, group, *, arg=None, args=None, scalar=None, extra=None, help=None, default=None):
    """`extra`: additional spec fields for states/displays (e.g. {"equals": Text(...)})."""
    d = {"label": label, "group": group}
    for k, v in (("arg", arg), ("args", args), ("scalar", scalar), ("extra", extra),
                 ("help", help), ("default", default)):
        if v is not None:
            d[k] = v
    return d


def _field(kind, label=None, **kw):
    return {"type": kind, "label": label, **{k: v for k, v in kw.items() if v is not None}}


def Text(label=None, placeholder=None, multiline=False, help=None):
    return _field("text", label, placeholder=placeholder, multiline=multiline or None, help=help)


def Command(label="Command", placeholder=None):
    return _field("command", label, placeholder=placeholder or "e.g. firefox",
                  help="Runs as you, through the shell.")


def Keys(label="Keys", placeholder="ctrl+shift+m"):
    return _field("keys", label, placeholder=placeholder,
                  help="Key names joined with +, e.g. ctrl+alt+t, F12, shift+Print")


def Number(label=None, min=None, max=None, step=None, help=None):
    return _field("number", label, min=min, max=max, step=step, help=help)


def Seconds(label="Seconds", help=None):
    return _field("number", label, min=0, step=1, unit="s", help=help)


def Bool(label=None, help=None):
    return _field("bool", label, help=help)


def Choice(*options, label=None, custom=False, help=None):
    """options: values, or (value, label) pairs. custom=True also allows typing a value."""
    opts = [{"value": o[0], "label": o[1]} if isinstance(o, tuple) else {"value": o, "label": str(o)}
            for o in options]
    return _field("choice", label, options=opts, custom=custom or None, help=help)


def Source(name, label=None, help=None):
    """A dropdown filled from a @source (or the editor's own: "pages", "polls", "saved_keys")."""
    return _field("source", label, source=name, help=help)


def Path(label="Folder", placeholder=None):
    return _field("text", label, placeholder=placeholder or "~/Pictures")


def Data(label="Data", help=None):
    """Free-form structured value, edited as YAML."""
    return _field("yaml", label, help=help)
