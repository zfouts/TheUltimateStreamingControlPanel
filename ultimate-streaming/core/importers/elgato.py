"""Elgato Stream Deck `.streamDeckProfile` exports (a zip; format notes: no official spec).

Read entirely in memory through _common's SafeZip. Supported layouts:

  v2 (Stream Deck 6.x+)   <UUID>.sdProfile/manifest.json          Device, Name, Pages{Current, Pages[]}
                          <UUID>.sdProfile/Profiles/<PAGE>/manifest.json   Controllers[{Type, Actions}]
                          <UUID>.sdProfile/Profiles/<PAGE>/Images/*.png
  v1 (older)              <UUID>.sdProfile/manifest.json with Actions directly in it; folders in
                          Profiles/<UUID>.sdProfile/
  a zip with a manifest.json at its root, or a zip of several .streamDeckProfile files (one level).

Much of this is reverse-engineered; every guess that affected the result is reported in the
setup's warnings.
"""
import posixpath
import re

from ..models import MODELS
from ._common import (MAX_FOLDER_DEPTH, MAX_JSON_BYTES, MAX_PAGES, ImageBudget, ImportError_,
                      clean_label, color, combo, import_image, needs_action, open_zip, parse_lock,
                      safe_action, safe_json, url_command, vk_key, VK_MODIFIERS, MAX_IMAGE_BYTES)

SOURCE = "elgato"

# Device.Model strings seen in exports (third-party notes; 20GBA9901 is reported as both + and MK.2,
# so a model whose grid doesn't fit the keys is replaced by one inferred from the key positions).
DEVICE_MODELS = {
    "20GAA9901": None,                    # Stream Deck Mobile: grid varies
    "20GAT9901": "Stream Deck Mini",
    "20GAI9901": "Stream Deck Mini",
    "20GBD9901": "Stream Deck Original",
    "20GAA9902": "Stream Deck Original",
    "20GAV9901": "Stream Deck XL",
    "20GAT9902": "Stream Deck XL",
    "20GBA9901": "Stream Deck +",
    "20GBJ9901": "Stream Deck Neo",
}
# Models tried, in order, when the grid is inferred from key positions alone.
INFER_ORDER = ("Stream Deck Mini", "Stream Deck Original", "Stream Deck XL", "Stream Deck + XL",
               "Stream Deck Studio")

BACK_KEY = {"label": "Back", "icon": "emoji:↩️", "bg": "#444444", "action": {"page": "back"}}
OBS_TOGGLES = {"stream": "ToggleStream", "record": "ToggleRecord", "replaybuffer": "ToggleReplayBuffer",
               "savereplay": "SaveReplayBuffer", "virtualcam": "ToggleVirtualCam"}
MULTI = {"com.elgato.streamdeck.multiactions", "com.elgato.streamdeck.multiactions.routine",
         "com.elgato.streamdeck.multiactions.routine2"}
DELAY = {"com.elgato.streamdeck.multiactions.delay", "com.elgato.streamdeck.system.delay"}
MAX_MULTI = 50
MAX_GRID = 16
_COORD = re.compile(r"\s*(\d{1,2})\s*,\s*(\d{1,2})\s*")


def _get(d, *names):
    """The first of names present in dict d (exact, then case-insensitive)."""
    if not isinstance(d, dict):
        return None
    for n in names:
        if n in d:
            return d[n]
    low = {k.lower(): v for k, v in d.items() if isinstance(k, str)}
    for n in names:
        if n.lower() in low:
            return low[n.lower()]
    return None


def _text(v, limit=256):
    return v.strip() if isinstance(v, str) and 0 < len(v.strip()) <= limit else None


class _Reader:
    def __init__(self, z, warnings, budget):
        self.z, self.warnings, self.budget = z, warnings, budget
        self.assumptions = set()
        self.pages_total = 0

    def note(self, msg):
        if msg not in self.warnings:
            self.warnings.append(msg)

    def json(self, name):
        found = self.z.lookup(name)
        if not found:
            return None
        doc = safe_json(self.z.read(found, MAX_JSON_BYTES))
        return doc if isinstance(doc, dict) else None

    # -------------------------------------------------------- structure

    def profile_roots(self):
        """Prefixes ("X.sdProfile/" or "") of the profiles in this zip."""
        roots = []
        for n in self.z.names():
            m = re.fullmatch(r"([^/]+\.sdProfile/)manifest\.json", n, re.I)
            if m:
                roots.append(m.group(1))
        if not roots and self.z.lookup("manifest.json"):
            roots.append("")
        return roots

    def read_profile(self, prefix):
        root = self.json(prefix + "manifest.json")
        if root is None:
            raise ImportError_("The profile's manifest is missing or isn't a JSON object.")
        version = str(root.get("Version", "")).split(".")[0]
        if version not in ("", "1", "2"):
            self.note("This profile is from a newer Stream Deck version than we know; it was read as version 2.")
        device = root.get("Device") if isinstance(root.get("Device"), dict) else {}
        model_str = device.get("Model") if isinstance(device.get("Model"), str) else ""

        # Top-level pages: (id, manifest dir, manifest)
        tops, current = [], None
        if isinstance(root.get("Actions"), dict) or isinstance(root.get("Controllers"), list):
            tops.append((None, prefix, root))                       # v1: one page in the root manifest
        else:
            pages = root.get("Pages") if isinstance(root.get("Pages"), dict) else {}
            ids = [p for p in pages.get("Pages") or [] if isinstance(p, str)] \
                if isinstance(pages.get("Pages"), list) else []
            if not ids:
                ids = sorted({n.split("/")[2] for n in self.z.names()
                              if re.fullmatch(re.escape(prefix) + r"Profiles/[^/]+/manifest\.json", n, re.I)})
                if ids:
                    self.note("The profile doesn't list its pages; every page in it was imported in name order.")
            for pid in ids:
                found = self.page_manifest(prefix, prefix, pid)
                if found:
                    tops.append((pid, *found))
                else:
                    self.note("A page listed in the profile is missing from it and was skipped.")
            current = _get(pages, "Current", "Default")
        if not tops:
            raise ImportError_("The profile has no pages.")
        if len(tops) > MAX_PAGES:
            raise ImportError_(f"The profile has more than {MAX_PAGES} pages.")

        # Folders reachable from the pages (breadth first, depth ≤ 10, pages ≤ 200 in all)
        nodes = []          # [id, dir, manifest, title, top index, depth]
        by_id = {}
        for i, (pid, d, man) in enumerate(tops):
            name = clean_label(_get(man, "Name")) or f"Page {i + 1}"
            node = {"id": pid, "dir": d, "man": man, "title": name, "top": i, "depth": 0}
            nodes.append(node)
            if pid:
                by_id[pid.casefold()] = node
        self.pages_total += len(nodes)
        if self.pages_total > MAX_PAGES:
            raise ImportError_(f"The profile has more than {MAX_PAGES} pages.")
        queue = list(nodes)
        while queue:
            node = queue.pop(0)
            for _, act in self.keypad(node["man"]):
                fid = self.folder_id(act)
                if fid is None or fid.casefold() in by_id:
                    continue
                if node["depth"] + 1 > MAX_FOLDER_DEPTH:
                    self.note(f"Folders nested more than {MAX_FOLDER_DEPTH} deep weren't imported.")
                    continue
                found = self.page_manifest(prefix, node["dir"], fid)
                if not found:
                    continue
                if self.pages_total >= MAX_PAGES:
                    raise ImportError_(f"The profile has more than {MAX_PAGES} pages (with folders).")
                self.pages_total += 1
                title = clean_label(self.state(act)[1].get("Title")) or clean_label(_get(act, "Name")) or "Folder"
                child = {"id": fid, "dir": found[0], "man": found[1], "title": title, "top": node["top"],
                         "depth": node["depth"] + 1, "folder": True}
                by_id[fid.casefold()] = child
                nodes.append(child)
                queue.append(child)

        model, (cols, rows), swap = self.grid(model_str, nodes)
        return self.convert(nodes, by_id, model, cols, rows, swap, current)

    def page_manifest(self, prefix, here, pid):
        """(dir, manifest) of page/folder `pid`: Profiles/<pid>/ (v2) or Profiles/<pid>.sdProfile/ (v1)."""
        if not isinstance(pid, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", pid) or ".." in pid:
            return None
        for base in dict.fromkeys((prefix, here)):
            for d in (f"{base}Profiles/{pid}/", f"{base}Profiles/{pid}.sdProfile/"):
                found = self.z.lookup(d + "manifest.json")
                if found:
                    man = self.json(found)
                    if man is not None:
                        return found[:-len("manifest.json")], man
        return None

    def keypad(self, man):
        """[((a, b), action)] of a page manifest's key actions (raw coordinate pairs)."""
        actions = None
        controllers = man.get("Controllers")
        if isinstance(controllers, list):
            for c in controllers:
                if not isinstance(c, dict):
                    continue
                kind = c.get("Type")
                if kind in (None, "Keypad") and isinstance(c.get("Actions"), dict):
                    actions = c["Actions"] if actions is None else {**actions, **c["Actions"]}
                elif isinstance(c.get("Actions"), dict) and c["Actions"]:
                    self.note("Dial (encoder) actions aren't imported yet.")
        elif isinstance(man.get("Actions"), dict):
            actions = man["Actions"]
        out = []
        for pos, act in (actions or {}).items():
            m = _COORD.fullmatch(pos) if isinstance(pos, str) else None
            if not m or not isinstance(act, dict):
                if act:
                    self.note("Some keys had a position we don't understand and were skipped.")
                continue
            out.append(((int(m.group(1)), int(m.group(2))), act))
        return out

    def folder_id(self, act):
        if _get(act, "UUID") != "com.elgato.streamdeck.profile.openchild":
            return None
        fid = _get(act.get("Settings"), "ProfileUUID", "ProfileID", "PageUUID", "profile")
        return fid if isinstance(fid, str) and fid else None

    def grid(self, model_str, nodes):
        """(model | None, (cols, rows), swap): swap=True means positions are "row,col"."""
        pts = [p for n in nodes for p, _ in self.keypad(n["man"])]
        amax = max((a for a, _ in pts), default=-1)
        bmax = max((b for _, b in pts), default=-1)

        def fits(model):
            r, c = MODELS[model][:2]
            if amax < c and bmax < r:
                return False
            if amax < r and bmax < c:
                return True
            return None

        declared = DEVICE_MODELS.get(model_str.upper())
        if declared:
            swap = fits(declared)
            if swap is not None:
                self.assumptions.add(f"Device model {model_str} read as {declared}.")
                if swap:
                    self.assumptions.add("Key positions read as \"row,column\" (they only fit the deck that way).")
                else:
                    self.assumptions.add("Key positions read as \"column,row\".")
                r, c = MODELS[declared][:2]
                return declared, (c, r), swap
        elif model_str:
            self.assumptions.add("Unknown device model; the deck was guessed from the key positions.")
        if not pts:
            self.assumptions.add("No keys found; assumed a Stream Deck Original.")
            return "Stream Deck Original", (5, 3), False
        for swap in (False, True):
            for model in INFER_ORDER:
                if fits(model) is swap:
                    if declared:
                        self.note(f"The keys don't fit a {declared}; the deck was guessed from the key positions instead.")
                    r, c = MODELS[model][:2]
                    self.assumptions.add(f"Deck guessed from the key positions: {model}.")
                    self.assumptions.add("Key positions read as \"row,column\"." if swap
                                         else "Key positions read as \"column,row\".")
                    return model, (c, r), swap
        self.assumptions.add("Key positions read as \"column,row\".")
        if declared:
            r, c = MODELS[declared][:2]
            self.note(f"Some keys don't fit a {declared} in either reading; they were skipped.")
            return declared, (c, r), False
        self.assumptions.add("No known deck fits the key positions; the grid is the keys' extent (at most 16×16).")
        return None, (min(amax + 1, MAX_GRID), min(bmax + 1, MAX_GRID)), False

    # -------------------------------------------------------- keys

    def state(self, act):
        states = act.get("States") if isinstance(act.get("States"), list) else []
        idx = act.get("State")
        idx = idx if isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx < len(states) else 0
        st = states[idx] if states and isinstance(states[idx], dict) else {}
        return idx, st

    def icon(self, page_dir, st):
        img = st.get("Image")
        if not isinstance(img, str) or not img:
            return None
        found = None
        rel = posixpath.normpath(img.replace("\\", "/"))
        if not rel.startswith(("../", "/")) and rel != "..":
            found = self.z.lookup(page_dir + rel) or self.z.lookup(rel)
        if not found:
            self.note("Some key images are missing from the profile.")
            return None
        return import_image(self.z.read(found, MAX_IMAGE_BYTES), source=SOURCE, budget=self.budget)

    def convert_action(self, act, ctx, depth=0):
        """Our action (not yet safe_action'ed) or a str note for needs_action."""
        uuid = _get(act, "UUID")
        uuid = uuid if isinstance(uuid, str) else ""
        s = act.get("Settings") if isinstance(act.get("Settings"), dict) else {}
        match uuid:
            case "com.elgato.streamdeck.system.hotkey":
                return self.hotkeys(s)
            case "com.elgato.streamdeck.system.website":
                run = url_command(_get(s, "url", "URL"))
                return run or "Opens a link that isn't http/https"
            case "com.elgato.streamdeck.system.open":
                return "Opens a program or file by its Windows path; choose the Linux program to run"
            case "com.elgato.streamdeck.system.text":
                text = _get(s, "text", "Text")
                if not isinstance(text, str) or not text or len(text) > 2000:
                    return "Types text that couldn't be read"
                text = "".join(c for c in text.replace("\r\n", "\n") if c in "\n\t" or c.isprintable())
                if _get(s, "isSendingEnter", "sendEnter", "SendEnter", "enter") is True:
                    self.assumptions.add("Text keys with the \"send Enter\" option type a line break at the end.")
                    text += "\n"
                return {"type": text}
            case "com.elgato.streamdeck.page.next" | "com.elgato.streamdeck.page.previous":
                tops = ctx["tops"]
                step = 1 if uuid.endswith("next") else -1
                self.assumptions.add("Next / previous page keys wrap around at the last / first page.")
                return {"page": tops[(ctx["top"] + step) % len(tops)]}
            case "com.elgato.streamdeck.page.goto":
                return self.goto(s, ctx)
            case "com.elgato.streamdeck.profile.openchild":
                fid = self.folder_id(act)
                node = ctx["by_id"].get(fid.casefold()) if fid else None
                if depth or not node:
                    return "Opens a folder that isn't in the profile"
                return {"page": ctx["names"][id(node)]}
            case "com.elgato.streamdeck.profile.backtoparent":
                return {"page": "back"}
            case "com.elgato.obsstudio.scene":
                scene = _text(_get(s, "sceneId", "scene", "sceneName", "SceneName"))
                self.assumptions.add("OBS scene keys: the scene name is read from the sceneId / scene setting.")
                return {"obs_scene": scene} if scene else "Switches to an OBS scene whose name couldn't be read"
            case "com.elgato.obsstudio.mixeraudio":
                src = _text(_get(s, "sourceId", "source", "sourceName", "inputName"))
                self.assumptions.add("OBS mute keys: the source name is read from the sourceId / source setting.")
                return {"obs_mute": src} if src else "Mutes an OBS source whose name couldn't be read"
            case "com.elgato.obsstudio.studiomode":
                return {"obs_studio": "toggle"}
            case _ if uuid.startswith("com.elgato.obsstudio.") and uuid.rsplit(".", 1)[1] in OBS_TOGGLES:
                return {"obs": OBS_TOGGLES[uuid.rsplit(".", 1)[1]]}
            case _ if uuid in DELAY and depth:
                ms = _get(s, "delay", "Delay", "time", "duration")
                if isinstance(ms, (int, float)) and not isinstance(ms, bool) and 0 <= ms <= 60000:
                    self.assumptions.add("Multi-action delays are in milliseconds.")
                    return {"delay": round(ms / 1000, 3)}
                return "A delay whose length couldn't be read"
            case _ if uuid in MULTI:
                return self.multi(act, s, ctx, depth)
        return "This action isn't supported yet"

    def hotkeys(self, s):
        combos = []
        for hk in s.get("Hotkeys") if isinstance(s.get("Hotkeys"), list) else []:
            if not isinstance(hk, dict):
                continue
            code = hk.get("VKeyCode")
            bits = hk.get("KeyModifiers") if isinstance(hk.get("KeyModifiers"), int) else 0
            mods = {m for m, on in (("shift", bits & 1 or hk.get("KeyShift") is True),
                                    ("ctrl", bits & 2 or hk.get("KeyCtrl") is True),
                                    ("alt", bits & 4 or hk.get("KeyOption") is True),
                                    ("super", bits & 8 or hk.get("KeyCmd") is True)) if on}
            if code in (-1, None, 0) and not mods:
                continue                                         # unused slot
            key = vk_key(code)
            if key is None and code in VK_MODIFIERS:
                return "A hotkey of modifier keys only"
            if key is None:
                return "A hotkey with a key we don't know"
            combos.append(combo(mods, key))
        if not combos:
            return "A hotkey with no keys set"
        self.assumptions.add("Hotkeys use Windows key codes (VKeyCode) and KeyModifiers 1=Shift 2=Ctrl 4=Alt 8=Win.")
        return {"keys": combos[0] if len(combos) == 1 else combos}

    def goto(self, s, ctx):
        target = _get(s, "PageUUID", "pageUUID", "pageId", "page", "Page", "PageIndex", "pageIndex", "index")
        tops = ctx["tops"]
        if isinstance(target, str):
            node = ctx["by_id"].get(target.casefold())
            if node is not None:
                return {"page": ctx["names"][id(node)]}
        if isinstance(target, int) and not isinstance(target, bool) and 0 <= target < len(tops):
            self.assumptions.add("\"Go to page\" numbers count from 0.")
            return {"page": tops[target]}
        return "Goes to a page that couldn't be found"

    def multi(self, act, s, ctx, depth):
        if depth:
            return "A multi action inside a multi action"
        steps = act.get("Actions") if isinstance(act.get("Actions"), list) else _get(s, "Actions", "Routine")
        if isinstance(steps, list) and steps and all(isinstance(x, list) for x in steps):
            if len(steps) != 1:
                return "A multi action that toggles between two lists of actions"
            steps = steps[0]
        if not isinstance(steps, list) or not steps or len(steps) > MAX_MULTI:
            return "A multi action whose steps couldn't be read"
        self.assumptions.add("Multi actions: the steps are read from the action's Actions list.")
        out = []
        for step in steps:
            if not isinstance(step, dict):
                return "A multi action whose steps couldn't be read"
            got = self.convert_action(step, ctx, depth + 1)
            if isinstance(got, str):
                return "A multi action with a step that isn't supported yet"
            out.extend(got if isinstance(got, list) else [got])
        return out

    def convert_key(self, act, page_dir, ctx):
        uuid = _get(act, "UUID")
        uuid = uuid if isinstance(uuid, str) else ""
        _, st = self.state(act)
        key = {}
        if st.get("ShowTitle") is not False:
            label = clean_label(st.get("Title"))
            if label:
                key["label"] = label
        fg = color(st.get("TitleColor"))
        if fg and fg != "#ffffff":
            key["fg"] = fg
        bg = color(_get(st, "BackgroundColor", "Background"))
        if bg:
            key["bg"] = bg
        icon = self.icon(page_dir, st)
        if icon:
            key["icon"] = icon
        if not key.get("label") and not icon:
            name = clean_label(_get(act, "Name"))
            if name:
                key["label"] = name
        got = self.convert_action(act, ctx)
        if isinstance(got, str):
            key.update(needs_action(uuid, got))
        else:
            key["action"] = safe_action(got, source=SOURCE, original_id=uuid)
        return key

    def convert(self, nodes, by_id, model, cols, rows, swap, current):
        names, used = {}, set()
        for n in nodes:
            base, i = n["title"], 1
            while (base if i == 1 else f"{base} {i}") in used:
                i += 1
            names[id(n)] = base if i == 1 else f"{base} {i}"
            used.add(names[id(n)])
        tops = [names[id(n)] for n in nodes if not n.get("folder")]
        pages, dropped = {}, False
        for n in nodes:
            grid = [[None] * cols for _ in range(rows)]
            ctx = {"tops": tops, "top": n["top"], "by_id": by_id, "names": names}
            has_back = False
            for (a, b), act in self.keypad(n["man"]):
                c, r = (b, a) if swap else (a, b)
                if c >= cols or r >= rows:
                    dropped = True
                    continue
                key = self.convert_key(act, n["dir"], ctx)
                has_back |= key.get("action") == {"page": "back"}
                grid[r][c] = key
            if n.get("folder") and not has_back:
                free = next(((r, c) for r in range(rows) for c in range(cols) if grid[r][c] is None), None)
                if free:
                    grid[free[0]][free[1]] = dict(BACK_KEY)
                    self.assumptions.add("Folders without a back key got one in their first free slot.")
                else:
                    self.note("A folder is full, so it has no back key; use another page key to leave it.")
            pages[names[id(n)]] = grid
        if dropped:
            self.note("Some keys were outside the deck's grid and were skipped.")
        start = tops[0]
        if isinstance(current, str) and current.casefold() in by_id:
            node = by_id[current.casefold()]
            if not node.get("folder"):
                start = names[id(node)]
        return {"model": model, "grid": (cols, rows), "pages": pages, "start_page": start}


def read(data, *, preview=None):
    """A setup dict (see _common) from the bytes of a .streamDeckProfile. ImportError_ on failure.

    preview: a preview token; icons then go to the preview folder (see ImageBudget)."""
    with parse_lock():
        warnings = []
        budget = ImageBudget(preview=preview)
        z = open_zip(data)
        reader = _Reader(z, warnings, budget)
        decks = []
        roots = reader.profile_roots()
        if roots:
            for prefix in roots:
                decks.append(reader.read_profile(prefix))
        else:
            nested = [n for n in z.names() if n.lower().endswith(".streamdeckprofile")]
            for n in nested:
                inner = _Reader(z.open_nested(n), warnings, budget)
                inner.pages_total = reader.pages_total
                inner_roots = inner.profile_roots()
                if not inner_roots:
                    warnings.append("A profile inside the file has no manifest and was skipped.")
                for prefix in inner_roots:
                    decks.append(inner.read_profile(prefix))
                reader.pages_total = inner.pages_total
                reader.assumptions |= inner.assumptions
        if not decks:
            raise ImportError_("That file doesn't contain a Stream Deck profile.")
        warnings.extend(budget.warnings)
        if reader.assumptions:
            warnings.append("Assumed (Elgato doesn't document its format): " + " ".join(sorted(reader.assumptions)))
        return {"source": SOURCE, "decks": decks, "warnings": warnings}
