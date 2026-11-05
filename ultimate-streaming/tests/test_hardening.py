"""Hardening: YAML alias bombs and self-referencing configs are refused everywhere config
text is loaded, special files can't hang the importer, and a few per-request caps."""
import json
import os
import socket
import time
import unittest
import urllib.error
import urllib.request

from tests.helpers import EXAMPLE, ROOT, Deck, free_port, tmpdir

import yaml  # noqa: E402

from core import backups, configio, editor_auth, importer  # noqa: E402
from core.bot import DeckBot  # noqa: E402

PORT = free_port()
PAGES = "pages:\n  main:\n    - [a, b]\n"


def bomb(levels=10):
    """~330 bytes of nested anchors that expand to 10**levels values."""
    names = "abcdefghijklmnop"
    lines = ["a: &a [x]"]
    for i in range(1, levels):
        lines.append(f"{names[i]}: &{names[i]} [{','.join(['*' + names[i - 1]] * 10)}]")
    return "\n".join(lines) + "\n"


def merge_bomb(levels=10):
    """The same with `<<:` merges, which PyYAML expands while building the mapping."""
    lines = ["m0: &m0 {k: v}"]
    for i in range(1, levels):
        lines.append(f"m{i}: &m{i} {{<<: [{', '.join([f'*m{i - 1}'] * 10)}]}}")
    return "\n".join(lines) + "\n"


ANCHORED = """\
style: &style {bg: "#101010", fg: "#ffffff", size: 14}
scene: &scene
  <<: *style
  bg: "#203040"
keys:
  mute: {<<: *style, label: Mute, action: {obs: mute}}
  live: {<<: *scene, label: Live}
pages:
  main:
    - [mute, live, {<<: *scene, label: Cam}]
    - &row [-, -, -]
    - *row
"""


def setUpModule():
    global BOT, CONFIG, BASE, TOKEN, ED
    CONFIG = os.path.join(tmpdir(), "config.yaml")
    with open(EXAMPLE) as f:
        cfg = yaml.safe_load(f)
    cfg["editor"] = {"port": PORT}
    with open(CONFIG, "w") as f:
        f.write(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
    BOT = DeckBot(root=ROOT, config_path=CONFIG)
    BOT.deck = Deck("Stream Deck XL")
    BOT.load_config(initial=True)
    BOT.load_extensions("modules")
    BASE, TOKEN = f"http://127.0.0.1:{PORT}", editor_auth.token()
    ED = next(m for m in BOT.modules.values() if type(m).__name__ == "Editor")


def tearDownModule():
    for inst in list(BOT.modules.values()):
        try:
            inst.on_unload()
        except Exception:
            pass


def call(path, body=None, base=None):
    h = {"X-Deck-Token": TOKEN}
    data = None
    if body is not None:
        data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
    req = urllib.request.Request((base or BASE) + path, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        with e:
            return e.code, json.loads(e.read() or b"{}")


class Bounded(unittest.TestCase):
    def test_bomb_rejected_fast(self):
        text = bomb()
        self.assertLess(len(text), 400)
        for t in (text, text.encode(), merge_bomb(), PAGES + text):
            start = time.monotonic()
            with self.assertRaisesRegex(ValueError, "too large once expanded"):
                configio.safe_load_bounded(t)
            self.assertLess(time.monotonic() - start, 1)

    def test_long_aliased_string_rejected(self):
        text = 's: &s "' + "x" * 100_000 + '"\nl: [' + ", ".join(["*s"] * 100) + "]\n"
        with self.assertRaisesRegex(ValueError, "too large once expanded"):
            configio.safe_load_bounded(text)

    def test_small_bomb_within_limit_loads(self):
        cfg = configio.safe_load_bounded(bomb(4))   # 10**3 values: fine
        self.assertEqual(len(cfg["d"]), 10)
        with self.assertRaises(ValueError):
            configio.safe_load_bounded(bomb(4), max_nodes=500)

    def test_recursive_alias_rejected(self):
        for text in ("a: &a [*a]\n", "a: &a {x: 1, y: *a}\n", "&r [1, [2, *r]]\n"):
            with self.assertRaisesRegex(ValueError, "refers to itself"):
                configio.safe_load_bounded(text)

    def test_normal_anchors_and_merges_work(self):
        cfg = configio.safe_load_bounded(ANCHORED)
        self.assertEqual(cfg, yaml.safe_load(ANCHORED))
        self.assertEqual(cfg["keys"]["live"], {"bg": "#203040", "fg": "#ffffff", "size": 14, "label": "Live"})
        self.assertEqual(cfg["pages"]["main"][2], ["-", "-", "-"])
        path = os.path.join(tmpdir(), "config.yaml")
        configio.save(path, cfg)
        self.assertEqual(configio.load(path), cfg)

    def test_plain_documents(self):
        self.assertIsNone(configio.safe_load_bounded(""))
        self.assertEqual(configio.safe_load_bounded("a: 1\n"), {"a": 1})
        with self.assertRaises(yaml.YAMLError):
            configio.safe_load_bounded("a: [1\n")
        with open(EXAMPLE) as f:
            self.assertEqual(configio.load(EXAMPLE), yaml.safe_load(f))

    def test_load_and_backups_refuse_bomb(self):
        path = os.path.join(tmpdir(), "config.yaml")
        with open(path, "w") as f:
            f.write(PAGES + bomb())
        with self.assertRaises(ValueError):
            configio.load(path)
        start = time.monotonic()
        self.assertEqual(backups.summarize(b"a: 1\n", (PAGES + bomb()).encode()), "changed")
        self.assertLess(time.monotonic() - start, 1)
        target = os.path.join(tmpdir(), "config.yaml")
        with self.assertRaisesRegex(ValueError, "too large once expanded"):
            backups._restore_bytes((PAGES + bomb()).encode(), target, "test")
        self.assertFalse(os.path.exists(target))

    def test_bot_load_config_keeps_old_config(self):
        path = os.path.join(tmpdir(), "config.yaml")
        with open(path, "w") as f:
            f.write(PAGES)
        bot = DeckBot(root=ROOT, config_path=path)
        bot.deck = Deck("Stream Deck XL")
        bot.load_config(initial=True)
        old = bot.cfg
        with open(path, "w") as f:
            f.write(PAGES + bomb())
        os.utime(path, (time.time() + 5, time.time() + 5))
        with self.assertLogs("deck", "ERROR") as logs:
            self.assertFalse(bot.load_config())
        self.assertIs(bot.cfg, old)
        self.assertIn("config not loaded", "\n".join(logs.output))


class Importer(unittest.TestCase):
    def test_preview_refuses_bomb(self):
        start = time.monotonic()
        status, res = call("/api/import/preview", {"text": PAGES + bomb()})
        self.assertEqual(status, 400)
        self.assertIn("too large once expanded", res.get("error", json.dumps(res)))
        self.assertLess(time.monotonic() - start, 1)
        with self.assertRaises(ValueError):
            importer.load_source(text=PAGES + merge_bomb())

    def test_fifo_refused_without_hanging(self):
        fifo = os.path.join(tmpdir(), "config.yaml")
        os.mkfifo(fifo)
        start = time.monotonic()
        status, _ = call("/api/import/preview", {"path": fifo})
        self.assertEqual(status, 400)
        self.assertIsNone(importer._inspect(fifo))
        with self.assertRaisesRegex(ValueError, "isn’t a deck config"):
            importer.load_source(path=fifo)
        self.assertLess(time.monotonic() - start, 1)

    def test_device_refused(self):
        self.assertIsNone(importer._inspect("/dev/zero"))
        with self.assertRaises(ValueError):
            importer.load_source(path="/dev/zero")


class Editor(unittest.TestCase):
    def test_yaml_parse_refuses_bomb_and_big_text(self):
        status, res = call("/api/yaml/parse", {"text": "a: &a [1, 2]\nb: *a\n"})
        self.assertEqual((status, res), (200, {"value": {"a": [1, 2], "b": [1, 2]}}))
        start = time.monotonic()
        status, res = call("/api/yaml/parse", {"text": bomb()})
        self.assertEqual(status, 400)
        self.assertLess(time.monotonic() - start, 1)
        self.assertEqual(call("/api/yaml/parse", {"text": "a: &a [*a]\n"})[0], 400)
        self.assertEqual(call("/api/yaml/parse", {"text": "#" * (1024 * 1024 + 1)})[0], 413)

    def test_render_caps_keys(self):
        from modules.editor import MAX_RENDER_KEYS
        status, res = call("/api/render", {"keys": [{"label": str(i)} for i in range(MAX_RENDER_KEYS)], "size": [16, 16]})
        self.assertEqual(status, 200)
        self.assertEqual(len(res["images"]), MAX_RENDER_KEYS)
        self.assertEqual(call("/api/render", {"keys": [{"label": "x"}] * (MAX_RENDER_KEYS + 1)})[0], 400)

    def test_example_library_previews_fit_one_request(self):
        """The Deck view's library preview sends every saved key at once (editor/js/app.js paintLibrary)."""
        import yaml
        from tests.helpers import EXAMPLE
        lib = yaml.safe_load(open(EXAMPLE)).get("keys") or {}
        res = call("/api/render", {"keys": [{"use": n} for n in lib], "library": lib, "size": [48, 48]})
        self.assertEqual(res[0], 200)
        self.assertTrue(all(json.loads(res[1])["images"]) if isinstance(res[1], (bytes, str)) else all(res[1]["images"]))
        self.assertEqual(call("/api/render", {"keys": "abc"})[0], 400)

    def test_new_port_after_failed_start_starts_server(self):
        ED._stop()                                  # as if the start on PORT had failed
        ED.running_as = (True, PORT)
        self.assertIsNone(ED.server)
        other = free_port()
        try:
            ED.on_config({"port": other})
            self.assertIsNotNone(ED.server)
            with socket.create_connection(("127.0.0.1", other), timeout=5):
                pass
            self.assertEqual(call("/api/status", base=f"http://127.0.0.1:{other}")[0], 200)
        finally:
            ED.on_config({"port": PORT})
        self.assertEqual(ED.running_as, (True, PORT))
        self.assertEqual(call("/api/status")[0], 200)


class Misc(unittest.TestCase):
    def test_big_diff_not_compared(self):
        path = os.path.join(tmpdir(), "config.yaml")
        with open(path, "w") as f:
            f.write(PAGES)
        backups.record(path, "manual")
        rid = backups.list_revisions()[0]["id"]
        with open(path, "w") as f:
            f.write(PAGES + "# " + "x" * (600 * 1024) + "\n")
        self.assertIn("not compared", backups.diff(rid, path))

    def test_action_argument_not_logged_at_info(self):
        BOT.actions["_t"] = lambda arg: None
        try:
            with self.assertLogs("deck", "DEBUG") as logs:
                BOT.run_action({"_t": "https://hooks.example/secret"})
        finally:
            del BOT.actions["_t"]
        info = [r.getMessage() for r in logs.records if r.levelname == "INFO"]
        debug = [r.getMessage() for r in logs.records if r.levelname == "DEBUG"]
        self.assertEqual(info, ["action _t"])
        self.assertIn("action _t: https://hooks.example/secret", debug)


if __name__ == "__main__":
    unittest.main()
