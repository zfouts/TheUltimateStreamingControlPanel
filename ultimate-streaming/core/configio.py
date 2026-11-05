"""Reading and writing config.yaml for the web editor.

Writes keep the file pleasant to hand-edit: each row of keys and each key definition
stays on one line (YAML "flow" style), sections keep their order, and every save keeps
the previous file as a revision in ~/.config/ultimate-streaming/backups/ (see backups.py).
Comments are not kept, so the file gets a short header pointing to the README instead.
"""
import datetime
import hashlib
import logging
import os
import re

import yaml

from .paths import state_dir
from .safefs import write_atomic  # noqa: F401  (re-exported: backups, discord and announce call it here)

log = logging.getLogger("deck")

HEADER = """\
# Stream Deck config. Edit it in the web editor (`python3 main.py --open-editor`),
# or by hand: changes are picked up live. Every field is described in README.md.
# Saved by the editor on {when}; earlier versions are in {backups}/.
"""
KEEP_BACKUPS = 30    # legacy backups/config-*.yaml cap; saves now keep revisions (backups.py)
CONFIG_VERSION = 1   # bump (and add an upgrade step in upgrade()) when the format changes
MAX_NODES = 200_000              # values in a config once every alias is expanded
MAX_EXPANDED_CHARS = 8 * 1024 * 1024   # text in a config once every alias is expanded


class _Flow(dict):
    pass


class _FlowList(list):
    pass


class _Dumper(yaml.SafeDumper):
    pass


_Dumper.add_representer(str, lambda d, v: d.represent_scalar("tag:yaml.org,2002:str", v, style='"' if "\n" in v else None))
_Dumper.add_representer(_Flow, lambda d, v: d.represent_mapping("tag:yaml.org,2002:map", v.items(), flow_style=True))
_Dumper.add_representer(_FlowList, lambda d, v: d.represent_sequence("tag:yaml.org,2002:seq", v, flow_style=True))


def _flow(v):
    if isinstance(v, dict):
        return _Flow(v)
    if isinstance(v, list):
        return _FlowList(v)
    return v


def _style(cfg):
    """Mark the parts of the config that read best on one line."""
    out = dict(cfg)
    if isinstance(out.get("keys"), dict):
        out["keys"] = {name: _flow(k) for name, k in out["keys"].items()}

    def page(p):
        if isinstance(p, list):
            if any(isinstance(r, list) for r in p):
                return [_FlowList(_flow(k) for k in r) if isinstance(r, list) else _flow(r) for r in p]
            return [_flow(k) for k in p]
        if isinstance(p, dict):
            q = dict(p)
            if "keys" in q:
                q["keys"] = page(q["keys"])
            for f in ("dials", "touch_keys"):
                if isinstance(q.get(f), list):
                    q[f] = _FlowList(_flow(k) for k in q[f])
            return q
        return p

    def pages(ps):
        return {name: page(p) for name, p in ps.items()} if isinstance(ps, dict) else ps

    if isinstance(out.get("pages"), dict):
        out["pages"] = pages(out["pages"])
    if isinstance(out.get("layouts"), dict):
        layouts = {}
        for name, lay in out["layouts"].items():
            if isinstance(lay, dict) and isinstance(lay.get("pages"), dict):
                layouts[name] = {**lay, "pages": pages(lay["pages"])}
            else:
                layouts[name] = pages(lay)
        out["layouts"] = layouts
    brb = out.get("brb")
    if isinstance(brb, dict) and isinstance(brb.get("polls"), list):
        out["brb"] = {**brb, "polls": [_flow(p) for p in brb["polls"]]}
    setup = out.get("obs_setup")
    if isinstance(setup, dict):
        out["obs_setup"] = {k: _flow(v) for k, v in setup.items()}
    return out


def dumps(cfg, when=None):
    text = yaml.dump(_style(cfg), Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=110,
                     default_flow_style=False)
    # A blank key is `-`; YAML quotes it inside [ ... ], which reads worse. Unquote it there only.
    text = re.sub(r"(?<=\[)'-'|(?<=, )'-'(?=[,\]])", "-", text)
    backups = os.path.join(state_dir(), "backups").replace(os.path.expanduser("~"), "~")
    header = HEADER.format(when=when or datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), backups=backups)
    return header + "\n" + text


def _comments(path):
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    if lines and lines[0].startswith(HEADER.splitlines()[0]):
        lines = lines[len(HEADER.splitlines()):]
    out = []
    for line in lines:
        body = re.sub(r'"(?:[^"\\]|\\.)*"|\'[^\']*\'', "", line)  # ignore '#' inside quotes
        m = re.search(r"(?:^|\s)(#.*)", body)
        if m and m.group(1).strip("#─ ").strip():
            out.append(m.group(1).strip())
    return out


def comment_count(path, example=None):
    """How many of your own comments the config has. Saving from the editor drops comments,
    so it warns first. The editor's header and comments copied from `example` don't count."""
    stock = set(_comments(example)) if example else set()
    return sum(1 for c in _comments(path) if c not in stock)


def file_hash(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except FileNotFoundError:
        return ""


def upgrade(cfg):
    """Bring an older config up to CONFIG_VERSION. No upgrades yet: version 1 is the first."""
    return cfg


def _expanded_size(root, max_nodes, max_chars):
    """Check the composed YAML node graph before anything is constructed. Aliases make it a
    DAG whose fully expanded size can be exponential in the text (an "alias bomb"), and
    json.dumps, comparisons and `<<:` merge flattening all pay the expanded size. Counted in
    linear time: each node's expanded (nodes, chars) is memoised by id, so a shared subtree
    costs O(1) after its first visit. Raises ValueError over the limits or on a cycle."""
    memo, on_path = {}, set()
    stack = [(root, False)]
    while stack:
        node, done = stack.pop()
        nid = id(node)
        if done:
            on_path.discard(nid)
            if isinstance(node, yaml.MappingNode):
                kids = [c for pair in node.value for c in pair]
            else:
                kids = node.value
            nodes, chars = 1, 0
            for c in kids:
                n, ch = memo[id(c)]
                nodes, chars = nodes + n, chars + ch
            if nodes > max_nodes or chars > max_chars:
                raise ValueError("config too large once expanded")
            memo[nid] = (nodes, chars)
            continue
        if nid in memo:
            continue
        if nid in on_path:
            raise ValueError("config refers to itself (recursive alias)")
        if isinstance(node, yaml.ScalarNode):
            if len(node.value) > max_chars:
                raise ValueError("config too large once expanded")
            memo[nid] = (1, len(node.value))
            continue
        on_path.add(nid)
        stack.append((node, True))
        kids = [c for pair in node.value for c in pair] if isinstance(node, yaml.MappingNode) else node.value
        stack.extend((c, False) for c in kids if id(c) not in memo)
    return memo[id(root)]


def safe_load_bounded(text_or_bytes, max_nodes=MAX_NODES):
    """yaml.safe_load, but refuses (ValueError) a document that is too large once its aliases
    are expanded, or that refers to itself. Normal anchors and `<<:` merges still work.
    YAML syntax errors raise yaml.YAMLError as usual."""
    loader = yaml.SafeLoader(text_or_bytes)
    try:
        try:
            node = loader.get_single_node()
        except RecursionError:
            raise ValueError("config is nested too deeply") from None
        if node is None:
            return None
        _expanded_size(node, max_nodes, MAX_EXPANDED_CHARS)
        return loader.construct_document(node)
    finally:
        loader.dispose()


def load(path):
    with open(path, "rb") as f:
        return safe_load_bounded(f.read()) or {}


class Conflict(Exception):
    """The file changed on disk since the editor loaded it."""


def save(path, cfg, base_hash=None, source="panel"):
    """Record the current file as a revision, then atomically write cfg and record that too
    (`source` says who saved: see backups.SOURCES). Returns the new file's hash."""
    from . import backups  # backups uses write_atomic; import here to avoid a cycle
    text = dumps(cfg)
    safe_load_bounded(text)  # never write something we can't read back
    with backups.LOCK:
        if base_hash is not None and file_hash(path) != base_hash:
            raise Conflict("config.yaml was changed by someone else since the editor loaded it")
        if os.path.exists(path):
            backups.record(path, "before-save")  # if this fails, don't overwrite the file
        write_atomic(path, text.encode("utf-8"), follow_symlink=True)
        try:
            backups.record(path, source)
        except Exception as e:  # the save itself worked; the daemon's reload records it again
            log.warning("saved config.yaml but couldn't record the revision: %s", e)
    return file_hash(path)
