"""core/importers/elgato.py: .streamDeckProfile zips → our setup / config, with every action
either live (harmless), wrapped for review, or marked needs_action."""
import io
import json
import os
import shlex
import unittest
import zipfile

from tests.helpers import EXAMPLE

import yaml  # noqa: E402
from PIL import Image  # noqa: E402

from core import importer  # noqa: E402
from core.importers import KINDS, elgato, to_config  # noqa: E402
from core.importers._common import PARSE_LOCK, ImportError_  # noqa: E402
from core.importers._common import commit_preview_icons as elgato_commit  # noqa: E402
from core.paths import state_dir  # noqa: E402

P = "ABCD-1234.sdProfile/"


def png(color):
    buf = io.BytesIO()
    Image.new("RGBA", (144, 144), color).save(buf, format="PNG")
    return buf.getvalue()


def act(uuid, title=None, settings=None, image="", **extra):
    state = {"Title": title or "", "Image": image, "ShowTitle": True, "TitleColor": "#ffffff"}
    return {"UUID": uuid, "ActionID": "id-" + uuid, "Name": uuid.rsplit(".", 1)[-1].title(),
            "Settings": settings or {}, "State": 0, "States": [state], **extra}


def hotkey(vk, mods=0, **flags):
    return {"Hotkeys": [{"KeyCmd": False, "KeyCtrl": False, "KeyOption": False, "KeyShift": False,
                         "KeyModifiers": mods, "NativeCode": vk, "QTKeyCode": vk, "VKeyCode": vk, **flags},
                        {"KeyModifiers": 0, "VKeyCode": -1, "NativeCode": -1}]}


def page(actions, name=None, encoders=None):
    man = {"Controllers": [{"Type": "Keypad", "Actions": actions}]}
    if encoders:
        man["Controllers"].append({"Type": "Encoder", "Actions": encoders})
    if name:
        man["Name"] = name
    return json.dumps(man).encode()


def profile(pages, model="20GAV9901", current=None, files=(), prefix=P, order=None):
    """A .streamDeckProfile zip: pages = {page id: manifest bytes}."""
    root = {"Device": {"Model": model, "UUID": ""}, "Name": "Streaming", "Version": "2.0",
            "Pages": {"Current": current or next(iter(pages), ""), "Pages": order or list(pages)}}
    if model is None:
        del root["Device"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(prefix + "manifest.json", json.dumps(root))
        for pid, man in pages.items():
            z.writestr(f"{prefix}Profiles/{pid}/manifest.json", man)
        for name, data in files:
            z.writestr(name, data)
    return buf.getvalue()


SD = "com.elgato.streamdeck."
OBS = "com.elgato.obsstudio."


def realistic():
    """A 2-page XL profile with a folder."""
    main = page({
        "0,0": act(SD + "system.hotkey", "Copy", hotkey(67, 2), image="Images/K00.png"),
        "1,0": act(SD + "system.website", "Twitch", {"url": "https://twitch.tv/x", "openInBrowser": True}),
        "2,0": act(OBS + "scene", "Game", {"sceneId": "Gameplay"}),
        "7,0": act(SD + "profile.openchild", "Tools", {"ProfileUUID": "FOLDER1"}),
        "0,3": act(SD + "system.hotkey", "\u202eMute\x07", hotkey(77, 3)),   # ctrl+shift+m
        "7,3": act(SD + "page.next", "Next"),
    }, name="Main")
    stream = page({
        "0,0": act(OBS + "stream", "Go live"),
        "1,0": act(OBS + "record", "Rec"),
        "2,0": act(OBS + "studiomode", "Studio"),
        "3,0": act(OBS + "mixeraudio", "Mic", {"sourceId": "Mic/Aux"}),
        "4,0": act(SD + "system.text", "Hello", {"text": "hello chat", "isSendingEnter": True}),
        "5,0": act(SD + "system.open", "Notepad", {"path": "C:\\Windows\\notepad.exe"}),
        "6,0": act("com.elgato.discord.mute", "Discord"),
        "0,1": act(SD + "multiactions", "Combo", {}, Actions=[
            act(SD + "system.hotkey", settings=hotkey(112)), act(OBS + "scene", settings={"sceneId": "BRB"})]),
        "1,1": act(SD + "multiactions", "Safe", {}, Actions=[
            act(OBS + "scene", settings={"sceneId": "BRB"}),
            act(SD + "multiactions.delay", settings={"delay": 500}),
            act(OBS + "studiomode")]),
        "2,1": act(SD + "system.website", "Bad link", {"url": "javascript:alert(1)"}),
        "6,3": act(SD + "page.goto", "First", {"PageIndex": 0}),
        "7,3": act(SD + "page.previous", "Prev"),
    }, name="Stream")
    folder = page({
        "0,0": act(SD + "profile.backtoparent", "Back"),
        "1,0": act(OBS + "scene", "Chat", {"sceneId": "Just Chatting"}),
        "2,0": act(SD + "profile.openchild", "Deeper", {"ProfileUUID": "FOLDER2"}),
    })
    folder2 = page({"3,1": act(OBS + "studiomode", "Studio")})
    return profile({"PAGE1": main, "PAGE2": stream, "FOLDER1": folder, "FOLDER2": folder2},
                   current="PAGE2", order=["PAGE1", "PAGE2"],
                   files=[(P + "Profiles/PAGE1/Images/K00.png", png((10, 20, 30, 255)))])


def review(key):
    return key["action"]["needs_review"]["imported_action"]


class RealisticProfileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.setup = elgato.read(realistic())
        cls.cfg, cls.info = to_config(cls.setup)
        cls.pages = cls.cfg["pages"]

    def key(self, page, col, row):
        return self.pages[page][row][col]

    def test_structure(self):
        self.assertIs(KINDS["elgato"], elgato.read)
        self.assertEqual(self.setup["source"], "elgato")
        self.assertEqual(len(self.setup["decks"]), 1)
        self.assertEqual(self.setup["decks"][0]["model"], "Stream Deck XL")
        self.assertEqual(set(self.cfg), {"pages", "start_page"})            # one deck: flat pages
        self.assertEqual(self.info, {"source": "elgato", "model": "Stream Deck XL", "grid": [8, 4]})
        self.assertEqual(list(self.pages), ["main", "stream", "tools", "deeper"])
        self.assertEqual(self.cfg["start_page"], "stream")
        for rows in self.pages.values():
            self.assertEqual(len(rows), 4)
            self.assertTrue(all(len(r) == 8 for r in rows))

    def test_hotkey_with_icon(self):
        k = self.key("main", 0, 0)
        self.assertEqual(k["label"], "Copy")
        self.assertEqual(review(k), {"keys": "ctrl+c"})
        self.assertEqual(k["action"]["needs_review"]["source"], "elgato")
        self.assertEqual(k["action"]["needs_review"]["original_id"], SD + "system.hotkey")
        self.assertTrue(k["icon"].startswith(os.path.join(state_dir(), "icons", "imported", "elgato") + os.sep))
        self.assertTrue(os.path.isfile(k["icon"]))

    def test_label_cleaned_and_modifiers(self):
        k = self.key("main", 0, 3)
        self.assertEqual(k["label"], "Mute")
        self.assertEqual(review(k), {"keys": "ctrl+shift+m"})

    def test_website(self):
        run = review(self.key("main", 1, 0))["run"]
        self.assertEqual(shlex.split(run), ["xdg-open", "https://twitch.tv/x"])
        bad = self.key("stream", 2, 1)
        self.assertNotIn("action", bad)
        self.assertIn("needs_action", bad)

    def test_obs(self):
        self.assertEqual(self.key("main", 2, 0)["action"], {"obs_scene": "Gameplay"})      # live
        self.assertEqual(review(self.key("stream", 0, 0)), {"obs": "ToggleStream"})        # goes live: review
        self.assertEqual(review(self.key("stream", 1, 0)), {"obs": "ToggleRecord"})
        self.assertEqual(self.key("stream", 2, 0)["action"], {"obs_studio": "toggle"})
        self.assertEqual(self.key("stream", 3, 0)["action"], {"obs_mute": "Mic/Aux"})

    def test_text_open_unknown(self):
        self.assertEqual(review(self.key("stream", 4, 0)), {"type": "hello chat\n"})
        for col in (5, 6):
            k = self.key("stream", col, 0)
            self.assertNotIn("action", k)
            self.assertIn("needs_action", k)
        self.assertEqual(self.key("stream", 6, 0)["needs_action"]["original_id"], "com.elgato.discord.mute")
        self.assertNotIn("notepad.exe", json.dumps(self.key("stream", 5, 0)).lower())   # the path isn't copied

    def test_multi_actions(self):
        self.assertEqual(review(self.key("stream", 0, 1)), [{"keys": "F1"}, {"obs_scene": "BRB"}])
        self.assertEqual(self.key("stream", 1, 1)["action"],
                         [{"obs_scene": "BRB"}, {"delay": 0.5}, {"obs_studio": "toggle"}])

    def test_pages_and_folders(self):
        self.assertEqual(self.key("main", 7, 3)["action"], {"page": "stream"})
        self.assertEqual(self.key("stream", 7, 3)["action"], {"page": "main"})
        self.assertEqual(self.key("stream", 6, 3)["action"], {"page": "main"})
        tools = self.key("main", 7, 0)
        self.assertEqual((tools["label"], tools["action"]), ("Tools", {"page": "tools"}))
        self.assertEqual(self.key("tools", 0, 0)["action"], {"page": "back"})
        self.assertEqual(self.key("tools", 1, 0)["action"], {"obs_scene": "Just Chatting"})
        self.assertEqual(self.key("tools", 2, 0)["action"], {"page": "deeper"})
        # a folder without its own back key gets one in the first free slot
        self.assertEqual(self.key("deeper", 0, 0)["action"], {"page": "back"})
        self.assertEqual(self.key("deeper", 3, 1)["action"], {"obs_studio": "toggle"})

    def test_assumptions_reported(self):
        text = " ".join(self.setup["warnings"])
        self.assertIn("Assumed", text)
        self.assertIn("column,row", text)
        self.assertIn("20GAV9901", text)

    def test_plan_accepts_it(self):
        with open(EXAMPLE) as f:
            current = yaml.safe_load(f)
        before = set(current["layouts"]["8x4"])
        p = importer.plan(current, self.cfg, target_layout="8x4")     # the attached XL's layout
        self.assertEqual(p["target_layout"], "8x4")
        added = [a["path"] for a in p["added"]]
        for name in ("main", "stream", "tools", "deeper"):
            if name not in before:
                self.assertIn(f"layouts.8x4.pages.{name}", added)
        self.assertFalse([a for a in added if "Stream Deck XL" in a])
        cfg, _ = importer.apply(current, self.cfg, importer.defaults(p), target_layout="8x4")
        self.assertNotIn("Stream Deck XL", cfg["layouts"])
        self.assertTrue(before <= set(cfg["layouts"]["8x4"]))            # nothing of yours hidden
        self.assertIn("stream", cfg["layouts"]["8x4"])
        yaml.safe_dump(self.cfg, allow_unicode=True)


class LayoutVariantsTest(unittest.TestCase):
    def test_row_col_inferred_from_grid(self):
        # A Mini is 3 columns × 2 rows: "0,2" only fits as row 0, column 2.
        data = profile({"P": page({"0,2": act(OBS + "studiomode", "S"), "1,0": act(OBS + "studiomode", "T")})},
                       model="20GAT9901")
        setup = elgato.read(data)
        d = setup["decks"][0]
        self.assertEqual((d["model"], d["grid"]), ("Stream Deck Mini", (3, 2)))
        rows = d["pages"]["Page 1"]
        self.assertEqual(rows[0][2]["label"], "S")
        self.assertEqual(rows[1][0]["label"], "T")
        self.assertIn("row,column", " ".join(setup["warnings"]))

    def test_model_inferred_from_positions(self):
        data = profile({"P": page({"4,2": act(OBS + "studiomode", "S")})}, model=None)
        d = elgato.read(data)["decks"][0]
        self.assertEqual((d["model"], d["grid"]), ("Stream Deck Original", (5, 3)))
        data = profile({"P": page({"5,0": act(OBS + "studiomode", "S")})}, model="WHATEVER")
        self.assertEqual(elgato.read(data)["decks"][0]["model"], "Stream Deck XL")

    def test_declared_model_that_doesnt_fit(self):
        setup = elgato.read(profile({"P": page({"4,2": act(OBS + "studiomode")})}, model="20GBA9901"))
        self.assertEqual(setup["decks"][0]["model"], "Stream Deck Original")
        self.assertTrue(any("don't fit" in w for w in setup["warnings"]))

    def test_no_known_grid_gives_flat_pages(self):
        data = profile({"P": page({"15,7": act(OBS + "studiomode", "S")})}, model=None)
        setup = elgato.read(data)
        self.assertIsNone(setup["decks"][0]["model"])
        cfg, info = to_config(setup)
        self.assertEqual(set(cfg), {"pages", "start_page"})
        self.assertEqual(info["model"], None)
        self.assertEqual(info["grid"], [16, 8])
        self.assertEqual(cfg["pages"]["page_1"][7][15]["label"], "S")

    def test_v1_single_manifest(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("OLD.sdProfile/manifest.json", json.dumps({
                "Version": "1.0", "Name": "Old", "Device": {"Model": "20GBD9901"},
                "Actions": {"1,1": act(OBS + "scene", "Live", {"sceneId": "Live"}),
                            "0,0": act(SD + "profile.openchild", "Dir", {"ProfileUUID": "SUB"})}}))
            z.writestr("OLD.sdProfile/Profiles/SUB.sdProfile/manifest.json", json.dumps({
                "Actions": {"2,2": act(OBS + "studiomode", "S")}}))
        cfg, info = to_config(elgato.read(buf.getvalue()))
        self.assertEqual(info["model"], "Stream Deck Original")
        pages = cfg["pages"]
        self.assertEqual(list(pages), ["old", "dir"])
        self.assertEqual(pages["old"][1][1]["action"], {"obs_scene": "Live"})
        self.assertEqual(pages["old"][0][0]["action"], {"page": "dir"})
        self.assertEqual(pages["dir"][2][2]["action"], {"obs_studio": "toggle"})
        self.assertEqual(pages["dir"][0][0]["action"], {"page": "back"})

    def test_nested_profiles(self):
        inner = realistic()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("one.streamDeckProfile", inner)
            z.writestr("two.streamDeckProfile", profile({"P": page({"0,0": act(OBS + "studiomode")})},
                                                       model="20GAT9901"))
        setup = elgato.read(buf.getvalue())
        self.assertEqual([d["model"] for d in setup["decks"]], ["Stream Deck XL", "Stream Deck Mini"])
        self.assertEqual(set(to_config(setup)[0]["layouts"]), {"8x4", "3x2"})

    def test_encoders_warned(self):
        setup = elgato.read(profile({"P": page({"0,0": act(OBS + "studiomode")},
                                               encoders={"0,0": act(SD + "system.multimedia")})},
                                    model="20GBA9901"))
        self.assertTrue(any("Dial" in w for w in setup["warnings"]))

    def test_folder_cycle_and_depth(self):
        pages = {"P": page({"0,0": act(SD + "profile.openchild", "F0", {"ProfileUUID": "F0"})})}
        for i in range(15):
            pages[f"F{i}"] = page({"1,0": act(SD + "profile.openchild", f"F{i + 1}", {"ProfileUUID": f"F{i + 1}"}),
                                   "2,0": act(SD + "profile.openchild", "up", {"ProfileUUID": "F0"})})
        setup = elgato.read(profile(pages, order=["P"]))
        names = list(setup["decks"][0]["pages"])
        self.assertEqual(len(names), 11)                     # the page + 10 folder levels
        self.assertTrue(any("nested" in w for w in setup["warnings"]))
        to_config(setup)


class HostileProfileTest(unittest.TestCase):
    def test_image_paths_stay_in_the_bundle(self):
        data = profile({"P": page({"0,0": act(OBS + "studiomode", "S", image="../../../etc/passwd"),
                                   "1,0": act(OBS + "studiomode", "T", image="/etc/passwd")})},
                       files=[("etc/passwd", png((1, 1, 1, 255)))])
        setup = elgato.read(data)
        row = setup["decks"][0]["pages"]["Page 1"][0]
        self.assertNotIn("icon", row[0])
        self.assertNotIn("icon", row[1])

    def test_bad_images_skipped(self):
        data = profile({"P": page({"0,0": act(OBS + "studiomode", "S", image="Images/a.svg")})},
                       files=[(P + "Profiles/P/Images/a.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>")])
        setup = elgato.read(data)
        self.assertNotIn("icon", setup["decks"][0]["pages"]["Page 1"][0][0])
        self.assertTrue(any("SVG" in w for w in setup["warnings"]))

    def test_not_a_profile(self):
        for data in (b"not a zip", profile({}, files=[]),):
            with self.assertRaises(ImportError_):
                elgato.read(data)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("readme.txt", "hi")
        with self.assertRaises(ImportError_):
            elgato.read(buf.getvalue())

    def test_json_bomb_in_manifest(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(P + "manifest.json", "[" * 100000 + "]" * 100000)
        with self.assertRaises(ImportError_):
            elgato.read(buf.getvalue())

    def test_preview_token(self):
        token = "elgato-preview-1234"  # privacy-check: allow
        setup = elgato.read(realistic(), preview=token)
        icon = setup["decks"][0]["pages"]["Main"][0][0]["icon"]
        self.assertTrue(icon.startswith(os.path.join(state_dir(), "icons", "imported", ".preview", token) + os.sep))
        cfg = elgato_commit(to_config(setup)[0], token)
        self.assertFalse(cfg["pages"]["main"][0][0]["icon"].count(".preview"))

    def test_lock_released_after_failure(self):
        with self.assertRaises(ImportError_):
            elgato.read(b"junk")
        self.assertTrue(PARSE_LOCK.acquire(blocking=False))
        PARSE_LOCK.release()

    def test_garbage_fields_dont_crash(self):
        weird = [None, 5, "x", [], {"UUID": 5}, {"UUID": SD + "system.hotkey", "Settings": {"Hotkeys": "x"}},
                 {"UUID": SD + "system.hotkey", "Settings": {"Hotkeys": [{"VKeyCode": 999}]}},
                 {"UUID": SD + "multiactions", "Actions": [[1], [2]]},
                 {"UUID": SD + "multiactions", "Actions": [act(SD + "multiactions", Actions=[])]},
                 {"UUID": SD + "page.goto", "Settings": {"PageIndex": 99}},
                 {"UUID": SD + "profile.openchild", "Settings": {"ProfileUUID": "../../x"}},
                 {"UUID": OBS + "scene", "Settings": {"sceneId": ["x"]}},
                 {"UUID": SD + "system.text", "Settings": {"text": "a" * 5000}},
                 {"UUID": SD + "system.website", "Settings": {"url": "https://x.y/\u202e"}},
                 {"UUID": OBS + "studiomode", "State": 99, "States": [None, 1]}]
        actions = {f"{i % 8},{i // 8}": a for i, a in enumerate(weird)}
        actions["bad"] = act(OBS + "studiomode")
        actions["99,99"] = act(OBS + "studiomode")
        setup = elgato.read(profile({"P": page(actions)}))
        self.assertEqual(setup["decks"][0]["model"], "Stream Deck XL")      # the stray 99,99 is skipped
        cfg, _ = to_config(setup)
        rows = cfg["pages"]["page_1"]
        for row in rows:
            for k in row:
                if k and "action" in k:
                    a = k["action"]
                    self.assertTrue(a == {"obs_studio": "toggle"} or "needs_review" in a, a)


if __name__ == "__main__":
    unittest.main()
