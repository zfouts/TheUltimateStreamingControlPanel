"""The onboarding routes (/api/setup/*) against the real daemon's HTTP server, like test_editor.py:
raw uploads with their own limit (refusals readable over a real socket, a total deadline),
preview / apply on the stored object only, per-key review bound to what was shown and placed at
the key's real destination, backups of every OBS folder, restores into fixed folders, OBS refusals
as status codes, and the stop-another-app route. No real program is touched:
appstop.stop / running are replaced where a route would act on the machine."""
import http.client
import json
import os
import shutil
import socket
import stat
import threading
import time
import unittest
from unittest import mock

from tests import test_importer_elgato as E
from tests.helpers import EXAMPLE, ROOT, Deck, free_port, tmpdir

import yaml  # noqa: E402

from core import appstop, configio, detect, editor_auth, importer, setup_backup  # noqa: E402
from core.bot import DeckBot  # noqa: E402
from core.importers import KINDS, _common  # noqa: E402
from core.webapi import setup_api  # noqa: E402

PORT = free_port()
SECRET = "SECRETMARK7f3a"
SDUI = os.path.join(os.environ["HOME"], ".streamdeck_ui.json")


def sdui_doc(label="Hello"):
    return {"streamdeck_ui_version": 2, "state": {"SER1": {"buttons": {"0": {
        "0": {"states": {"0": {"text": label, "command": f"echo {SECRET}-one"}}},
        "1": {"states": {"0": {"text": "Two", "command": f"echo {SECRET}-two"}}},
        "2": {"states": {"0": {"text": "Bright", "brightness_change": 10}}}}}}}}


def write_sdui(doc):
    with open(SDUI, "w") as f:
        f.write(doc if isinstance(doc, str) else json.dumps(doc))


def setUpModule():
    global BOT, CONFIG, TOKEN
    CONFIG = os.path.join(tmpdir(), "config.yaml")
    with open(EXAMPLE) as f:
        cfg = yaml.safe_load(f)
    cfg["editor"] = {"port": PORT}
    cfg["brightness"] = 41          # not the untouched example: imports merge, not replace
    with open(CONFIG, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)
    BOT = DeckBot(root=ROOT, config_path=CONFIG)
    BOT.deck = Deck("Stream Deck XL")
    BOT.load_config(initial=True)
    BOT.load_extensions("modules")
    TOKEN = editor_auth.token()


def tearDownModule():
    for inst in list(BOT.modules.values()):
        try:
            inst.on_unload()
        except Exception:
            pass


def raw(method, path, body=b"", ctype="application/json", length=None, token=True, send_body=True):
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=30)
    try:
        conn.putrequest(method, path)
        if token:
            conn.putheader("X-Deck-Token", TOKEN)
        if ctype:
            conn.putheader("Content-Type", ctype)
        if method == "POST":
            conn.putheader("Content-Length", str(len(body) if length is None else length))
        conn.endheaders()
        if send_body and body:
            conn.send(body)
        r = conn.getresponse()
        data = r.read()
        return r.status, json.loads(data) if data else None
    finally:
        conn.close()


def call(path, body=None):
    if body is None:
        return raw("GET", path, ctype=None)
    return raw("POST", path, json.dumps(body).encode())


def ok(path, body=None):
    status, data = call(path, body)
    assert status == 200, (path, status, data)
    return data


def upload(data):
    return raw("POST", "/api/setup/upload", data, ctype="application/octet-stream")


def elgato_zip(title="Copy", extra=b""):
    pages = {"P1": E.page({"0,0": E.act(E.SD + "system.hotkey", title, E.hotkey(67, 2)),
                           "1,0": E.act(E.SD + "system.text", "Say", {"text": f"hi {SECRET}"})})}
    return E.profile(pages, files=[("note.txt", extra)] if extra else ())


def uploads_on_disk():
    d = setup_api.upload_dir()
    return sorted(os.path.join(d, n) for n in os.listdir(d))


def config():
    return configio.load(CONFIG)


def page_rows(layout, page):
    lay = config()["layouts"][layout]
    p = lay.get("pages", lay)[page]
    return p.get("keys", p) if isinstance(p, dict) else p


class Uploads(unittest.TestCase):
    def test_limit_type_and_id(self):
        status, data = raw("POST", "/api/setup/upload", ctype="application/octet-stream",
                           length=51 * 1024 * 1024, send_body=False)
        self.assertEqual(status, 413, data)
        status, _ = raw("POST", "/api/setup/upload", b"x" * 10, ctype="application/json")
        self.assertEqual(status, 415)
        status, _ = raw("POST", "/api/setup/upload", b"x" * 10, ctype=None)
        self.assertEqual(status, 415)
        status, _ = raw("POST", "/api/setup/upload", b"x" * 10, ctype="application/octet-stream", token=False)
        self.assertEqual(status, 401)
        # Bigger than the JSON limit is fine here ...
        status, data = upload(b"\0" * (5 * 1024 * 1024))
        self.assertEqual(status, 200, data)
        self.assertRegex(data["upload_id"], r"^[0-9a-f]{32}$")
        # ... while every JSON route keeps 4 MB.
        status, _ = raw("POST", "/api/setup/backup", length=5 * 1024 * 1024, send_body=False)
        self.assertEqual(status, 413)

    def test_files_private_expire_and_evict(self):
        for p in uploads_on_disk():
            os.unlink(p)
        ids = [upload(bytes([i]) * 100)[1]["upload_id"] for i in range(3)]
        files = uploads_on_disk()
        self.assertEqual(len(files), 3)
        for p in files:
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(setup_api.upload_dir()).st_mode), 0o700)
        fourth = upload(b"4" * 100)[1]["upload_id"]
        self.assertEqual(len(uploads_on_disk()), 3)            # the oldest was evicted, file and all
        status, data = call("/api/setup/import/preview", {"kind": "elgato", "upload_id": ids[0]})
        self.assertEqual(status, 404, data)
        now = setup_api.clock()
        with mock.patch.object(setup_api, "clock", return_value=now + setup_api.TTL + 1):
            ok("/api/setup/backups")                           # any setup call expires old uploads
            self.assertEqual(uploads_on_disk(), [])
            status, _ = call("/api/setup/import/preview", {"kind": "elgato", "upload_id": fourth})
            self.assertEqual(status, 404)


class Preview(unittest.TestCase):
    def setUp(self):
        write_sdui(sdui_doc())
        self.addCleanup(lambda: os.path.exists(SDUI) and os.unlink(SDUI))
        with open(CONFIG, "rb") as f:
            original = f.read()

        def restore():
            with open(CONFIG, "wb") as f:
                f.write(original)
        self.addCleanup(restore)

    def source_id(self, kind):
        scan = ok("/api/setup/detect")
        return next(a["source_id"] for a in scan["apps"] if a["kind"] == kind)

    def preview_sdui(self):
        return ok("/api/setup/import/preview", {"kind": "streamdeck_ui", "source_id": self.source_id("streamdeck_ui")})

    def test_source_id_preview_apply_review(self):
        p = self.preview_sdui()
        self.assertRegex(p["token"], r"^[0-9a-f]{32}$")
        self.assertEqual(p["info"]["source"], "streamdeck-ui")
        self.assertTrue(p["plan"]["added"])
        layout = p["plan"]["target_layout"]
        self.assertEqual(layout, BOT.primary.layout_name)
        review = sorted(p["review"], key=lambda r: r["key"])
        self.assertEqual([(r["key"], r["label"], r["kind"], r["argument"]) for r in review],
                         [(0, "Hello", "run", f"echo {SECRET}-one"), (1, "Two", "run", f"echo {SECRET}-two")])
        self.assertTrue(all(r["layout"] == layout and r["row"] == 0 for r in review))
        # Changing the file after the preview has no effect: apply uses the stored result.
        write_sdui(sdui_doc(label="CHANGED"))
        r = ok("/api/setup/import/apply", {"token": p["token"], "choices": {}})
        self.assertTrue(r["ok"])
        h = r["hash"]
        page = review[0]["page"]
        rows = page_rows(layout, page)
        self.assertEqual(rows[0][0]["label"], "Hello")
        self.assertIn("needs_review", rows[0][0]["action"])
        self.assertIn("needs_review", rows[0][1]["action"])
        # The token is used up.
        self.assertEqual(call("/api/setup/import/apply", {"token": p["token"], "choices": {}})[0], 404)
        # Enable exactly one key.
        two = {"layout": layout, "page": page, "key": 1, "hash": h, "expect": review[1]["expect"]}
        status, _ = call("/api/setup/review", two)
        self.assertEqual(status, 400)                       # enable: true is required
        r = ok("/api/setup/review", {**two, "enable": True})
        self.assertEqual(r["action"], {"run": f"echo {SECRET}-two"})
        rows = page_rows(layout, page)
        self.assertEqual(rows[0][1]["action"], {"run": f"echo {SECRET}-two"})
        self.assertIn("needs_review", rows[0][0]["action"])
        h = r["hash"]
        self.assertEqual(call("/api/setup/review", {**two, "hash": h, "enable": True})[0], 409)
        self.assertEqual(call("/api/setup/review", {**two, "hash": h, "key": 2, "enable": True})[0], 409)
        self.assertEqual(call("/api/setup/review", {**two, "hash": h, "page": "nope", "enable": True})[0], 404)

    def test_upload_preview_elgato(self):
        status, up = upload(elgato_zip())
        self.assertEqual(status, 200, up)
        before = len(uploads_on_disk())
        p = ok("/api/setup/import/preview", {"kind": "elgato", "upload_id": up["upload_id"]})
        self.assertEqual(p["info"]["source"], "elgato")
        self.assertEqual(len(uploads_on_disk()), before - 1)       # parsed once, then deleted
        self.assertEqual(sorted(r["kind"] for r in p["review"]), ["keys", "type"])
        self.assertTrue(all(isinstance(r["argument"], str) for r in p["review"]))
        r = ok("/api/setup/import/apply", {"token": p["token"]})
        self.assertTrue(r["ok"])
        status, data = call("/api/setup/import/preview", {"kind": "streamdeck_ui", "upload_id": up["upload_id"]})
        self.assertEqual(status, 400, data)                       # uploads are Elgato profiles only

    def test_bad_requests(self):
        self.assertEqual(call("/api/setup/import/preview", {"kind": "streamdeck_ui", "source_id": "0" * 24})[0], 404)
        status, data = call("/api/setup/import/preview", {"kind": "streamdeck_ui", "path": SDUI})
        self.assertEqual(status, 400)
        self.assertIn("path", data["error"])
        status, _ = call("/api/setup/import/preview", {"kind": "streamdeck_ui", "path": SDUI,
                                                         "source_id": self.source_id("streamdeck_ui")})
        self.assertEqual(status, 400)                             # rejected, never ignored and read
        self.assertEqual(call("/api/setup/import/preview", {"kind": "elgato",
                                                              "source_id": self.source_id("streamdeck_ui")})[0], 400)
        self.assertEqual(call("/api/setup/import/preview", {"kind": "nope", "source_id": "x"})[0], 400)
        self.assertEqual(call("/api/setup/import/preview", {"kind": "streamdeck_ui"})[0], 400)
        self.assertEqual(call("/api/setup/import/apply", {"token": "../x"})[0], 404)
        self.assertEqual(call("/api/setup/import/apply", {"token": "a" * 32})[0], 404)

    def test_token_expiry_and_stale_config(self):
        p = self.preview_sdui()
        now = setup_api.clock()
        with mock.patch.object(setup_api, "clock", return_value=now + setup_api.TTL + 1):
            self.assertEqual(call("/api/setup/import/apply", {"token": p["token"]})[0], 404)
        p = self.preview_sdui()
        cfg = config()
        cfg["brightness"] = 42
        configio.save(CONFIG, cfg)
        self.assertEqual(call("/api/setup/import/apply", {"token": p["token"]})[0], 409)

    def test_preview_limit(self):
        tokens = [self.preview_sdui()["token"] for _ in range(setup_api.MAX_PREVIEWS + 1)]
        self.assertEqual(call("/api/setup/import/apply", {"token": tokens[0]})[0], 404)
        ok("/api/setup/import/apply", {"token": tokens[-1]})

    def test_errors_never_quote_content(self):
        write_sdui('{"streamdeck_ui_version": 2, "state": "' + SECRET + '"')     # broken JSON
        status, data = call("/api/setup/import/preview", {"kind": "streamdeck_ui",
                                                            "source_id": self.source_id("streamdeck_ui")})
        self.assertEqual(status, 400)
        self.assertNotIn(SECRET, json.dumps(data))
        write_sdui({"streamdeck_ui_version": SECRET, "state": {SECRET: {"buttons": SECRET}}})
        status, data = call("/api/setup/import/preview", {"kind": "streamdeck_ui",
                                                            "source_id": self.source_id("streamdeck_ui")})
        self.assertEqual(status, 400)
        self.assertNotIn(SECRET, json.dumps(data))
        for blob in (SECRET.encode() * 100, b"PK\x03\x04" + SECRET.encode(),
                     E.profile({"P1": ('{"Controllers": "' + SECRET + '"').encode()})):
            _, up = upload(blob)
            status, data = call("/api/setup/import/preview", {"kind": "elgato", "upload_id": up["upload_id"]})
            self.assertEqual(status, 400, data)
            self.assertNotIn(SECRET, json.dumps(data))
        with mock.patch.dict(KINDS, {"elgato": mock.Mock(side_effect=RuntimeError(SECRET))}):
            _, up = upload(b"zip")
            status, data = call("/api/setup/import/preview", {"kind": "elgato", "upload_id": up["upload_id"]})
        self.assertEqual((status, data["error"]), (400, setup_api.UNREADABLE))


class Backups(unittest.TestCase):
    def setUp(self):
        write_sdui(sdui_doc())
        self.addCleanup(lambda: os.path.exists(SDUI) and os.unlink(SDUI))
        for a in setup_backup.list_archives():
            setup_backup.pin(a["id"], False)
            setup_backup.delete(a["id"])

    def test_backup_with_detected_roots_restore_into_default(self):
        ok("/api/setup/detect")
        with mock.patch.object(setup_backup, "create", wraps=setup_backup.create) as create:
            first = ok("/api/setup/backup", {"include": ["streamdeck_ui"]})
        (items,), kw = create.call_args
        self.assertEqual(items, [{"kind": "streamdeck_ui", "root": os.path.dirname(os.path.realpath(SDUI)),
                                  "include": [".streamdeck_ui.json"], "label": "streamdeck-ui"}])
        self.assertTrue(first["pinned"])                           # the first (onboarding) backup
        self.assertEqual(first["archive"], first["id"])
        self.assertEqual(first["items"][0]["files"], 1)
        second = ok("/api/setup/backup", {"include": ["streamdeck_ui"], "label": "again"})
        self.assertFalse(second["pinned"])
        self.assertEqual([a["id"] for a in ok("/api/setup/backups")["backups"]], [second["id"], first["id"]])
        listing = json.dumps(ok("/api/setup/backups"))
        self.assertNotIn(SECRET, listing)
        self.assertNotIn(os.environ["HOME"], listing)

        write_sdui({"changed": True})
        self.assertEqual(call("/api/setup/restore", {"id": first["id"], "item": "streamdeck_ui"})[0], 400)
        running = mock.Mock(return_value=False)
        with mock.patch.object(appstop, "running", running), \
                mock.patch.object(setup_backup, "restore", wraps=setup_backup.restore) as restore:
            r = ok("/api/setup/restore", {"id": first["id"], "item": "streamdeck_ui", "confirm": True})
        self.assertTrue(r["ok"])
        self.assertEqual(restore.call_args.kwargs["roots"], {"streamdeck_ui": detect.default_root("streamdeck_ui")})
        running.assert_called_with("streamdeck_ui")
        with open(SDUI) as f:
            self.assertEqual(json.load(f), sdui_doc())
        with mock.patch.object(appstop, "running", return_value=True):
            status, data = call("/api/setup/restore", {"id": first["id"], "item": "streamdeck_ui", "confirm": True})
        self.assertEqual(status, 409, data)

    def test_validation_pin_delete(self):
        self.assertEqual(call("/api/setup/backup", {"include": ["elgato"]})[0], 400)
        self.assertEqual(call("/api/setup/backup", {"include": "obs"})[0], 400)
        self.assertEqual(call("/api/setup/backup", {"include": ["boatswain"]})[0], 404)   # not detected
        a = ok("/api/setup/backup", {"include": ["streamdeck_ui"]})
        self.assertEqual(call("/api/setup/backups/delete", {"id": a["id"]})[0], 409)        # pinned
        self.assertFalse(ok("/api/setup/backups/pin", {"id": a["id"], "pinned": False})["pinned"])
        ok("/api/setup/backups/delete", {"id": a["id"]})
        self.assertEqual(call("/api/setup/backups/delete", {"id": a["id"]})[0], 404)
        self.assertEqual(call("/api/setup/backups/delete", {"id": "../../etc"})[0], 400)
        self.assertEqual(call("/api/setup/restore", {"id": "x", "item": "obs", "confirm": True})[0], 400)


class Apps(unittest.TestCase):
    def test_running(self):
        with mock.patch.object(appstop, "running", side_effect=lambda k: k == "boatswain"):
            self.assertEqual(ok("/api/setup/apps/running"),
                             {"streamcontroller": False, "streamdeck_ui": False, "boatswain": True})

    def test_stop(self):
        result = {"stopped": 1, "autostart_disabled": [], "backup": None, "still_running": 0}
        with mock.patch.object(appstop, "stop", return_value=result) as stop:
            for body in ({"kind": "obs", "confirm": True}, {"kind": "elgato", "confirm": True},
                         {"kind": "nope", "confirm": True}, {"kind": "boatswain"},
                         {"kind": "boatswain", "confirm": "yes"}, {"kind": "boatswain", "confirm": True, "force": 1}):
                self.assertEqual(call("/api/setup/app/stop", body)[0], 400, body)
            stop.assert_not_called()
            self.assertEqual(ok("/api/setup/app/stop", {"kind": "boatswain", "confirm": True}), result)
            stop.assert_called_once_with("boatswain", force=False)
            ok("/api/setup/app/stop", {"kind": "streamcontroller", "confirm": True, "force": True})
            stop.assert_called_with("streamcontroller", force=True)


class Obs(unittest.TestCase):
    def test_current_and_addons(self):
        from core import obs_setup
        cur = {"profile": "P", "collection": "C", "scenes": ["Live"], "has_stream_key": True}
        with mock.patch.object(obs_setup, "current", return_value=cur):
            r = ok("/api/setup/obs")
        self.assertEqual(r["addons"], obs_setup.ADDONS)
        self.assertEqual(r["scenes"], ["Live"])
        with mock.patch.object(obs_setup, "add_addon", return_value={"ok": True}) as add:
            self.assertEqual(call("/api/setup/obs/addon", {"scene": "Live", "addon": "nope"})[0], 400)
            ok("/api/setup/obs/addon", {"scene": "Live", "addon": "countdown"})
        self.assertEqual(add.call_args.args[1:3], ("Live", "countdown"))
        self.assertIs(add.call_args.args[3], BOT.cfg)          # the whole config, not a merged section
        with mock.patch.object(obs_setup, "adopt", return_value={"ok": True}) as adopt:
            ok("/api/setup/obs/adopt", {"profile": "P"})
        self.assertEqual(adopt.call_args.kwargs, {"profile": "P", "collection": None})
        real = BOT.get_module
        with mock.patch.object(BOT, "get_module", side_effect=lambda n: None if n == "OBS" else real(n)):
            self.assertEqual(call("/api/setup/obs")[0], 503)


# ------------------------------------------------------------------ review fixes

def head(path, length, ctype="application/octet-stream", token=True, extra=""):
    lines = [f"POST {path} HTTP/1.1", f"Host: 127.0.0.1:{PORT}"]
    if token:
        lines.append(f"X-Deck-Token: {TOKEN if token is True else token}")
    if ctype:
        lines.append(f"Content-Type: {ctype}")
    if length is not None:
        lines.append(f"Content-Length: {length}")
    return ("\r\n".join(lines) + "\r\n" + extra + "\r\n").encode()


def socket_request(header, body=b"", pause=None, total=None):
    """Send header, then body in a thread (still sending while the server answers); read the
    response until the server closes. Returns (status, headers text, json body)."""
    s = socket.create_connection(("127.0.0.1", PORT), timeout=10)
    s.sendall(header)

    def send():
        sent = 0
        try:
            for i in range(0, len(body), 16384):
                s.sendall(body[i:i + 16384])
                sent += 16384
                if pause:
                    time.sleep(pause)
        except OSError:
            pass
    t = threading.Thread(target=send, daemon=True)
    t.start()
    data = b""
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    except ConnectionResetError:
        pass
    finally:
        t.join(5)
        s.close()
    head_, _, rest = data.partition(b"\r\n\r\n")
    lines = head_.decode().split("\r\n")
    status = int(lines[0].split()[1])
    length = next(int(ln.split(":")[1]) for ln in lines if ln.lower().startswith("content-length"))
    return status, "\n".join(lines[1:]).lower(), json.loads(rest[:length])


class EarlyRejects(unittest.TestCase):
    """Refused uploads reach the client as JSON even while it is still sending the body."""

    def test_refusals_readable(self):
        body = b"\0" * (48 * 1024)
        cases = [(head("/api/setup/upload", 51 * 1024 * 1024), 413),
                 (head("/api/setup/upload", len(body), ctype="text/plain"), 415),
                 (head("/api/setup/upload", None, extra="Transfer-Encoding: chunked\r\n"), 411),
                 (head("/api/setup/upload", len(body), token="x" * 43), 401)]
        for header, want in cases:
            status, headers, data = socket_request(header, body)
            self.assertEqual(status, want, data)
            self.assertIn("connection: close", headers)
            self.assertIn("error", data)

    def test_busy_upload_429(self):
        slow = socket.create_connection(("127.0.0.1", PORT), timeout=10)
        self.addCleanup(slow.close)
        slow.sendall(head("/api/setup/upload", 100) + b"x" * 10)     # holds the one upload slot
        time.sleep(0.3)
        status, headers, data = socket_request(head("/api/setup/upload", 48 * 1024), b"\0" * (48 * 1024))
        self.assertEqual(status, 429, data)
        self.assertIn("connection: close", headers)
        slow.sendall(b"y" * 90)
        self.assertIn(b" 200 ", slow.recv(4096))

    def test_total_deadline(self):
        path = "/api/setup/upload"
        fn, limit, seconds = BOT.get_module("Editor").router.uploads[path]
        self.assertEqual(seconds, setup_api.UPLOAD_SECONDS)
        BOT.get_module("Editor").router.uploads[path] = (fn, limit, 0.5)
        self.addCleanup(BOT.get_module("Editor").router.uploads.__setitem__, path, (fn, limit, seconds))
        before = uploads_on_disk()
        # Bytes keep trickling in (each read is quick), but the whole upload takes too long.
        status, _, data = socket_request(head(path, 400 * 1024), b"\0" * (200 * 1024), pause=0.1)
        self.assertEqual(status, 408, data)
        self.assertEqual(uploads_on_disk(), before)                 # the partial file is gone


class PreviewLock(unittest.TestCase):
    def test_source_read_only_under_lock(self):
        _, up = upload(elgato_zip())
        setup_api.PREVIEW_LOCK.acquire()
        try:
            with mock.patch.object(setup_api, "PREVIEW_WAIT", 0.2), \
                    mock.patch.object(setup_api, "read_regular", wraps=setup_api.read_regular) as rr:
                status, data = call("/api/setup/import/preview", {"kind": "elgato", "upload_id": up["upload_id"]})
            self.assertEqual(status, 429, data)
            rr.assert_not_called()                                   # nothing read while waiting
        finally:
            setup_api.PREVIEW_LOCK.release()
        ok("/api/setup/import/preview", {"kind": "elgato", "upload_id": up["upload_id"]})


class Destinations(unittest.TestCase):
    """Review entries name where each key really lands, for a layouts config and a flat config."""

    def setUp(self):
        with open(CONFIG, "rb") as f:
            original = f.read()

        def restore():
            with open(CONFIG, "wb") as f:
                f.write(original)
        self.addCleanup(restore)
        self.addCleanup(lambda: os.path.exists(SDUI) and os.unlink(SDUI))

    def two_decks(self):
        mini = {str(i): {"states": {"0": {"text": f"M{i}"}}} for i in range(6)}
        mini["0"] = {"states": {"0": {"text": "Mini run", "command": "echo mini"}}}
        xl = {"0": {"states": {"0": {"text": "XL run", "command": "echo xl"}}},
              "31": {"states": {"0": {"text": "Last", "command": "echo last"}}}}
        return {"streamdeck_ui_version": 2, "state": {"MINI": {"buttons": {"0": mini}},
                                                       "XL": {"buttons": {"0": xl}}}}

    def preview(self, doc):
        write_sdui(doc)
        scan = ok("/api/setup/detect")
        sid = next(a["source_id"] for a in scan["apps"] if a["kind"] == "streamdeck_ui")
        return ok("/api/setup/import/preview", {"kind": "streamdeck_ui", "source_id": sid})

    def located(self, entry):
        cfg = config()
        if entry["layout"] is None:
            page = cfg["pages"][entry["page"]]
        else:
            lay = cfg["layouts"][entry["layout"]]
            page = lay.get("pages", lay)[entry["page"]]
        rows = page.get("keys", page) if isinstance(page, dict) else page
        flat = [k for r in rows for k in (r if isinstance(r, list) else [r])]
        return flat[entry["key"]]

    def apply_and_check(self, p):
        r = ok("/api/setup/import/apply", {"token": p["token"]})
        for e in p["review"]:
            key = self.located(e)
            self.assertEqual(key["label"], e["label"])
            self.assertEqual(setup_api.expect_hash(key["action"]["needs_review"]["imported_action"]), e["expect"])
        e = p["review"][0]
        r = ok("/api/setup/review", {"layout": e["layout"], "page": e["page"], "key": e["key"],
                                     "hash": r["hash"], "expect": e["expect"], "enable": True})
        self.assertNotIn("needs_review", self.located(e)["action"])
        return r

    def test_layouts_config(self):
        p = self.preview(self.two_decks())
        where = {e["label"]: e["layout"] for e in p["review"]}
        # Each deck's pages join your layout for its grid (the example has both 8x4 and 3x2).
        self.assertTrue({"8x4", "3x2"} <= set(config()["layouts"]))
        self.assertEqual(where, {"Mini run": "3x2", "XL run": "8x4", "Last": "8x4"})
        self.assertTrue(all(e["path"] and e["conflict"] is False for e in p["review"]))
        self.apply_and_check(p)

    def test_flat_pages_config(self):
        flat = {"brightness": 41, "editor": {"port": PORT},
                "pages": {"main": [[{"label": "Mine"}] + [None] * 7] + [[None] * 8 for _ in range(3)]}}
        configio.save(CONFIG, flat)
        p = self.preview(self.two_decks())
        where = {e["label"]: e["layout"] for e in p["review"]}
        # The XL layout merges into your flat 8x4 pages (layout null); the Mini one is added.
        self.assertEqual(where, {"Mini run": "3x2", "XL run": None, "Last": None})
        self.apply_and_check(p)
        self.assertEqual(config()["pages"]["main"][0][0], {"label": "Mine"})
        # A single flat incoming deck into flat pages: top-level too.
        configio.save(CONFIG, flat)
        p = self.preview(sdui_doc())
        self.assertTrue(p["review"])
        self.assertTrue(all(e["layout"] is None for e in p["review"]))
        self.apply_and_check(p)


class ReviewBinding(unittest.TestCase):
    def setUp(self):
        with open(CONFIG, "rb") as f:
            original = f.read()

        def restore():
            with open(CONFIG, "wb") as f:
                f.write(original)
        self.addCleanup(restore)
        cfg = config()
        lay = next(iter(cfg["layouts"]))
        pages = cfg["layouts"][lay].get("pages", cfg["layouts"][lay])
        self.page = next(iter(pages))
        self.action = {"run": "echo shown"}
        rows = pages[self.page]
        rows = rows.get("keys", rows) if isinstance(rows, dict) else rows
        rows[0][0] = {"label": "R", "action": {"needs_review": {"imported_action": self.action, "source": "x",
                                                               "original_id": "y"}}}
        self.hash = configio.save(CONFIG, cfg)
        self.base = {"layout": lay, "page": self.page, "key": 0, "enable": True}

    def test_hash_and_expect_required_and_checked(self):
        good = setup_api.expect_hash(self.action)
        self.assertEqual(call("/api/setup/review", {**self.base, "expect": good})[0], 400)          # no hash
        self.assertEqual(call("/api/setup/review", {**self.base, "hash": self.hash})[0], 400)      # no expect
        before = configio.file_hash(CONFIG)
        status, _ = call("/api/setup/review", {**self.base, "hash": self.hash,
                                               "expect": setup_api.expect_hash({"run": "echo other"})})
        self.assertEqual(status, 409)
        self.assertEqual(call("/api/setup/review", {**self.base, "hash": "0" * 64, "expect": good})[0], 409)
        self.assertEqual(configio.file_hash(CONFIG), before)        # nothing changed
        r = ok("/api/setup/review", {**self.base, "hash": self.hash, "expect": good})
        self.assertEqual(r["action"], self.action)
        self.assertEqual(call("/api/setup/review", {**self.base, "hash": r["hash"], "expect": good})[0], 409)


class ObsRoots(unittest.TestCase):
    """Every OBS folder is backed up; a restore goes to the active one (else the default)."""

    def setUp(self):
        home = os.environ["HOME"]
        self.native = os.path.join(os.environ["XDG_CONFIG_HOME"], "obs-studio")
        self.flatpak_app = os.path.join(home, ".var/app/com.obsproject.Studio")
        self.flat = os.path.join(self.flatpak_app, "config/obs-studio")
        for root, name in ((self.native, "Native"), (self.flat, "Flat")):
            prof = os.path.join(root, "basic/profiles", name)
            os.makedirs(prof)
            with open(os.path.join(prof, "basic.ini"), "w") as f:
                f.write(f"[General]\nName={name}\n")
            self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.flatpak_app, ignore_errors=True)
        for a in setup_backup.list_archives():
            setup_backup.pin(a["id"], False)
            setup_backup.delete(a["id"])

    def test_backup_every_root_restore_active(self):
        scan = ok("/api/setup/detect")
        self.assertEqual(len(scan["obs"]["roots"]), 2)
        r = ok("/api/setup/backup", {"include": ["obs"]})
        self.assertEqual(len(r["archives"]), 2)
        self.assertEqual(sorted(a["label"] for a in r["archives"]),
                         ["Before onboarding (flatpak)", "Before onboarding (native)"])
        self.assertTrue(all(a["pinned"] and a["items"][0]["files"] == 1 for a in r["archives"]))
        active = detect.active_obs_root(detect.scan())
        with mock.patch.object(appstop, "running", return_value=False), \
                mock.patch.object(setup_backup, "restore", return_value={"ok": True}) as restore:
            ok("/api/setup/restore", {"id": r["id"], "item": "obs", "confirm": True})
            self.assertEqual(restore.call_args.kwargs["roots"], {"obs": active})
            with mock.patch.object(detect, "active_obs_root", return_value=None):
                ok("/api/setup/restore", {"id": r["id"], "item": "obs", "confirm": True})
            self.assertEqual(restore.call_args.kwargs["roots"], {"obs": detect.default_root("obs")})

    def test_restore_into_other_install_needs_confirmation(self):
        r = ok("/api/setup/backup", {"include": ["obs"]})
        by_label = {a["label"]: a for a in r["archives"]}
        arc = by_label["Before onboarding (flatpak)"]
        active = detect.active_obs_root(detect.scan())
        other = self.flat if os.path.realpath(active) == os.path.realpath(self.native) else self.native
        other_arc = arc if os.path.realpath(other) == os.path.realpath(self.flat) else by_label["Before onboarding (native)"]
        with mock.patch.object(appstop, "running", return_value=False):
            status, data = call("/api/setup/restore", {"id": other_arc["id"], "item": "obs", "confirm": True})
            self.assertEqual(status, 409)
            self.assertEqual(data["mismatch"], {"archive_root": os.path.realpath(other),
                                                "target_root": os.path.realpath(active)})
            self.assertIn("error", data)
            status, _ = call("/api/setup/restore", {"id": other_arc["id"], "item": "obs", "confirm": True,
                                                    "allow_other_target": "yes"})
            self.assertEqual(status, 400)
            done = ok("/api/setup/restore", {"id": other_arc["id"], "item": "obs", "confirm": True,
                                             "allow_other_target": True})
            self.assertTrue(done["restored"])

    def test_restore_after_files_deleted(self):
        sc = os.path.join(os.environ["HOME"], ".var/app/com.core447.StreamController/data")
        if os.path.exists(sc):                                   # another module's fixture: set aside
            aside = os.path.join(tmpdir(), "sc")
            os.rename(sc, aside)
            self.addCleanup(os.rename, aside, sc)
        self.addCleanup(shutil.rmtree, sc, ignore_errors=True)
        os.makedirs(os.path.join(sc, "pages"))
        with open(os.path.join(sc, "pages", "Main.json"), "w") as f:
            f.write('{"keys": {}}')
        ok("/api/setup/detect")
        r = ok("/api/setup/backup", {"include": ["streamcontroller"]})
        shutil.rmtree(sc)                                        # the whole folder is gone
        ok("/api/setup/detect")                                  # and no longer detected
        with mock.patch.object(appstop, "running", return_value=False):
            ok("/api/setup/restore", {"id": r["id"], "item": "streamcontroller", "confirm": True})
        with open(os.path.join(sc, "pages", "Main.json")) as f:
            self.assertEqual(f.read(), '{"keys": {}}')


class ObsStatus(unittest.TestCase):
    def test_refusals_map_to_status(self):
        from core import obs_setup
        cases = [("adopt", {"profile": "x"}, {"ok": False, "error": "No OBS profile named 'x'"}, 400),
                 ("adopt", {}, {"ok": False, "error": "OBS did not switch profile in time"}, 503),
                 ("add_addon", {"scene": "S", "addon": "countdown"},
                  {"ok": False, "error": "'Countdown' already exists in OBS as something else; rename it"}, 409),
                 ("add_addon", {"scene": "S", "addon": "countdown"},
                  {"ok": False, "error": "No scene named 'S' in the current collection"}, 400),
                 ("adopt", {}, ConnectionError("socket closed"), 503),
                 ("add_addon", {"scene": "S", "addon": "countdown"}, setup_backup.BackupError("no space"), 409)]
        for fn, body, result, want in cases:
            kw = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
            route = "/api/setup/obs/adopt" if fn == "adopt" else "/api/setup/obs/addon"
            with mock.patch.object(obs_setup, fn, **kw):
                status, data = call(route, body)
            self.assertEqual(status, want, (fn, result, data))
        with mock.patch.object(obs_setup, "current", side_effect=OSError("down")):
            self.assertEqual(call("/api/setup/obs")[0], 503)

    def test_unknown_source_id_not_echoed(self):
        sid = "deadbeef" + SECRET
        status, data = call("/api/setup/import/preview", {"kind": "elgato", "source_id": sid})
        self.assertEqual(status, 404)
        self.assertNotIn(SECRET, json.dumps(data))
        with mock.patch.object(detect, "obs_roots", return_value=[]):
            self.assertEqual(call("/api/setup/backup", {"include": ["obs"]})[0], 404)


class PreviewIcons(unittest.TestCase):
    """Readers write icons under the preview token; apply commits them; expiry cleans up."""

    def setUp(self):
        with open(CONFIG, "rb") as f:
            original = f.read()

        def restore():
            with open(CONFIG, "wb") as f:
                f.write(original)
        self.addCleanup(restore)

    def icon_zip(self):
        pages = {"P1": E.page({"0,0": E.act(E.SD + "system.hotkey", "Pic", E.hotkey(67, 2), image="Images/K.png")})}
        return E.profile(pages, files=[(E.P + "Profiles/P1/Images/K.png", E.png((200, 10, 10, 255)))])

    def test_icons_follow_the_token(self):
        _, up = upload(self.icon_zip())
        with mock.patch.object(importer, "plan", wraps=importer.plan) as plan:
            p = ok("/api/setup/import/preview", {"kind": "elgato", "upload_id": up["upload_id"]})
        self.assertEqual(plan.call_args.args[4], (8, 4))               # the primary deck's grid
        (entry,) = [e for e in p["review"] if e["label"] == "Pic"]
        preview_dir = os.path.join(_common._imported_root(), _common.PREVIEW_DIR, p["token"])
        self.assertTrue(os.listdir(preview_dir))                       # icons written under the token
        with mock.patch.object(_common, "commit_preview_icons", wraps=_common.commit_preview_icons) as commit:
            r = ok("/api/setup/import/apply", {"token": p["token"]})
        self.assertEqual(commit.call_args.args[1], p["token"])
        self.assertFalse(os.path.exists(preview_dir))
        lay = config()["layouts"][entry["layout"]]
        rows = lay.get("pages", lay)[entry["page"]]
        icon = rows[0][0]["icon"]
        self.assertTrue(os.path.isfile(icon))
        self.assertNotIn(_common.PREVIEW_DIR, icon)
        self.assertTrue(r["ok"])

    def test_cleanup_on_expiry_and_eviction(self):
        _, up = upload(self.icon_zip())
        p = ok("/api/setup/import/preview", {"kind": "elgato", "upload_id": up["upload_id"]})
        with mock.patch.object(_common, "cleanup_preview_icons") as cleanup:
            now = setup_api.clock()
            with mock.patch.object(setup_api, "clock", return_value=now + setup_api.TTL + 1):
                ok("/api/setup/backups")
            cleanup.assert_called_with(older_than=setup_api.TTL)
            self.assertEqual(call("/api/setup/import/apply", {"token": p["token"]})[0], 404)
            cleanup.reset_mock()
            write_sdui(sdui_doc())
            self.addCleanup(lambda: os.path.exists(SDUI) and os.unlink(SDUI))
            scan = ok("/api/setup/detect")
            sid = next(a["source_id"] for a in scan["apps"] if a["kind"] == "streamdeck_ui")
            for _ in range(setup_api.MAX_PREVIEWS + 1):              # the last one evicts the oldest
                ok("/api/setup/import/preview", {"kind": "streamdeck_ui", "source_id": sid})
            self.assertGreaterEqual(cleanup.call_count, setup_api.MAX_PREVIEWS + 2)

if __name__ == "__main__":

    unittest.main()
