"""core/webhooks.py: URL checks, posting and its results, and keeping webhook URLs out of logs.
Nothing here reaches the network: the HTTP post is a fake."""
import http.client
import io
import logging
import ssl
import types
import unittest

from tests.helpers import ROOT  # noqa: F401  (sets up the sandbox first)

import requests  # noqa: E402
import urllib3  # noqa: E402

from core import webhooks  # noqa: E402

TOKEN = "FAKEtoken_" + "a1B2-c3_" * 8
URL = f"https://discord.com/api/webhooks/123456789012345678/{TOKEN}"


def response(status, body=None, headers=None):
    return types.SimpleNamespace(status_code=status, headers=headers or {}, closed=False,
                                 json=(lambda: body) if body is not None else _no_json, close=lambda: None)


def _no_json():
    raise ValueError("no JSON")


class Capture:
    """A handler on one logger that keeps the formatted output (message and traceback)."""

    def __init__(self, name):
        self.logger = logging.getLogger(name)
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setLevel(logging.DEBUG)
        self.handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        self.records = []

    def __enter__(self):
        self.level, self.propagate = self.logger.level, self.logger.propagate
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self.logger.addHandler(self.handler)
        keep = self.records

        class Keep(logging.Handler):
            def emit(self, record):
                keep.append(record)
        self.keeper = Keep()
        self.logger.addHandler(self.keeper)
        return self

    def __exit__(self, *exc):
        self.logger.removeHandler(self.handler)
        self.logger.removeHandler(self.keeper)
        self.logger.setLevel(self.level)
        self.logger.propagate = self.propagate

    @property
    def text(self):
        return self.stream.getvalue()


class TestValidate(unittest.TestCase):
    def test_accepts_discord_hosts_and_canonicalises(self):
        for host in ("discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"):
            self.assertEqual(webhooks.validate("discord", f"  https://{host}/api/webhooks/123456789012345678/{TOKEN} "),
                             f"https://{host}/api/webhooks/123456789012345678/{TOKEN}")
        self.assertEqual(webhooks.validate("discord", URL.replace("discord.com", "Discord.COM")), URL)

    def test_rejects_everything_else_without_echoing_the_url(self):
        bad = [
            URL.replace("https", "http"),
            URL.replace("discord.com", "discord.com.evil.example"),
            URL.replace("discord.com", "evil.example"),
            URL.replace("https://", "https://user:pw@"),
            URL.replace("https://", "https://discord.com@"),
            URL.replace("discord.com", "discord.com:443"),
            URL.replace("discord.com", "discord.com:8443"),
            URL + "#frag", URL + "?thread_id=1", URL + "?", URL + "/", URL + "/slack",
            "https://discord.com/api/webhooks/123456789012345678",
            "https://discord.com/api/webhooks/notdigits/" + TOKEN,
            "https://discord.com/api/webhooks/123456789012345678/short",
            "https://discord.com/api/v10/webhooks/123456789012345678/" + TOKEN,
            URL.replace(TOKEN, TOKEN[:10] + " " + TOKEN[10:]),
            URL.replace(TOKEN, TOKEN[:10] + "\n" + TOKEN[10:]),
            URL + "x" * 400, "", None, 42, ["x"],
        ]
        for url in bad:
            with self.subTest(url=url):
                with self.assertRaises(ValueError) as e:
                    webhooks.validate("discord", url)
                self.assertNotIn(TOKEN[:12], str(e.exception))
                self.assertNotIn("123456789012345678", str(e.exception))

    def test_only_discord_type(self):
        with self.assertRaises(ValueError):
            webhooks.validate("slack", URL)

    def test_host_of(self):
        self.assertEqual(webhooks.host_of(URL), "discord.com")
        self.assertIsNone(webhooks.host_of("https://evil.example/x"))
        self.assertIsNone(webhooks.host_of(None))


class TestRedact(unittest.TestCase):
    def test_paths_encoded_paths_and_known_tokens(self):
        text = (f"POST /api/webhooks/123456789012345678/{TOKEN}?wait=true; full {URL}; "
                f"quoted api%2Fwebhooks%2F123456789012345678%2F{TOKEN}; bare {TOKEN}.")
        out = webhooks.redact(text, (URL,))
        self.assertNotIn(TOKEN[:12], out)
        self.assertNotIn("123456789012345678", out)
        self.assertIn("<webhook>", out)
        self.assertEqual(webhooks.redact("nothing secret here"), "nothing secret here")

    def test_clean_strips_controls_and_caps(self):
        out = webhooks.clean("bad\x1b[31m\u202etext\n" + "x" * 500 + URL)
        self.assertNotIn("\n", out)
        self.assertNotIn("\u202e", out)
        self.assertLessEqual(len(out), webhooks.REMOTE_MAX)


class TestLogFilter(unittest.TestCase):
    def test_urllib3_debug_line_is_redacted(self):
        webhooks.install_log_filter()
        with Capture("urllib3.connectionpool") as cap:
            # exactly how urllib3 logs each request at DEBUG
            logging.getLogger("urllib3.connectionpool").debug(
                '%s://%s:%s "%s %s %s" %s %s', "https", "discord.com", 443, "POST",
                f"/api/webhooks/123456789012345678/{TOKEN}?wait=true", "HTTP/1.1", 404, 45)
        self.assertIn('"POST /<webhook>', cap.text)
        self.assertNotIn(TOKEN[:12], cap.text)

    def test_exception_text_is_redacted(self):
        webhooks.install_log_filter()
        with Capture("deck") as cap:
            try:
                raise requests.ConnectionError(f"HTTPSConnectionPool(host='discord.com', port=443): Max retries "
                                               f"exceeded with url: /api/webhooks/123456789012345678/{TOKEN}")
            except requests.ConnectionError:
                logging.getLogger("deck").error("post to %s failed", URL, exc_info=True)
        self.assertIn("ConnectionError", cap.text)
        self.assertNotIn(TOKEN[:12], cap.text)

    def test_stack_info_is_redacted(self):
        webhooks.install_log_filter()
        with Capture("core.webhooks") as cap:
            logging.getLogger("core.webhooks").warning("posting", stack_info=True)
            rec = cap.records[-1]
        rec.stack_info += f"\n  url = '{URL}'"
        webhooks.RedactFilter().filter(rec)
        self.assertNotIn(TOKEN[:12], rec.stack_info)

    def test_real_requests_failure_is_redacted(self):
        """requests' own exception text for an unreachable webhook (no network: port 9 on localhost)."""
        webhooks.install_log_filter()
        url = f"https://127.0.0.1:9/api/webhooks/123456789012345678/{TOKEN}"
        with Capture("core.webhooks") as cap, Capture("urllib3.connectionpool") as pool:
            result = webhooks.post(url, {"content": "x"})
        self.assertEqual(result.kind, "retry")
        self.assertIn("ConnectionError", cap.text)
        for text in (cap.text, pool.text, result.reason):
            self.assertNotIn(TOKEN[:12], text)
            self.assertNotIn("123456789012345678", text)

    def test_install_is_idempotent(self):
        webhooks.install_log_filter()
        webhooks.install_log_filter()
        for name in webhooks.LOGGERS:
            n = sum(1 for f in logging.getLogger(name).filters if getattr(f, "redacts_webhook", False))
            self.assertEqual(n, 1, name)


class TestPost(unittest.TestCase):
    def post(self, *responses, payload=None):
        calls = []
        queue = list(responses)

        def http(url, **kw):
            calls.append((url, kw))
            r = queue.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        with Capture("core.webhooks") as cap:
            result = webhooks.post(URL, payload or {"content": "hi", "allowed_mentions": {"parse": []}}, http=http)
        self.assertNotIn(TOKEN[:12], cap.text)
        for rec in cap.records:
            self.assertIsNone(rec.exc_info)          # never log.exception on the send path
        return result, calls, cap

    def test_ok_and_request_shape(self):
        result, calls, _ = self.post(response(200, {"id": "1"}))
        self.assertEqual(result.kind, "ok")
        url, kw = calls[0]
        self.assertEqual(url, URL + "?wait=true")
        self.assertEqual(kw["timeout"], (5, 10))
        self.assertIs(kw["allow_redirects"], False)
        self.assertEqual(kw["json"]["allowed_mentions"], {"parse": []})
        self.assertEqual(self.post(response(204))[0].kind, "ok")

    def test_429_uses_retry_after_capped(self):
        r = self.post(response(429, {"retry_after": 2.5}))[0]
        self.assertEqual((r.kind, r.after), ("retry", 2.5))
        r = self.post(response(429, headers={"Retry-After": "7"}))[0]
        self.assertEqual((r.kind, r.after), ("retry", 7.0))
        self.assertEqual(self.post(response(429, {"retry_after": 3600}))[0].after, 60.0)
        self.assertEqual(self.post(response(429, {"retry_after": "nan"}))[0].after, webhooks.RETRY_AFTER_DEFAULT)
        self.assertEqual(self.post(response(429))[0].after, webhooks.RETRY_AFTER_DEFAULT)

    def test_5xx_and_network_retry(self):
        self.assertEqual(self.post(response(502))[0].kind, "retry")
        r, _, cap = self.post(requests.ConnectionError(f"Max retries exceeded with url: "
                                                       f"/api/webhooks/123456789012345678/{TOKEN}?wait=true"))
        self.assertEqual((r.kind, r.after), ("retry", None))
        self.assertNotIn(TOKEN[:12], r.reason)
        self.assertIn("ConnectionError", cap.text)

    def test_read_timeout_may_have_posted_so_it_is_not_retried(self):
        # as requests raises it: the request went out, the answer didn't come back in time
        err = requests.ReadTimeout(urllib3.exceptions.ReadTimeoutError(
            None, f"/api/webhooks/123456789012345678/{TOKEN}?wait=true", "Read timed out. (read timeout=10)"))
        r, calls, cap = self.post(err)
        self.assertEqual((r.kind, r.after), ("failed", None))
        self.assertEqual(r.reason, "Discord didn't answer in time; the post may have gone through. Check the channel.")
        self.assertEqual(len(calls), 1)
        self.assertIn("ReadTimeout", cap.text)
        self.assertNotIn("123456789012345678", cap.text)

    def test_a_connection_dropped_mid_request_is_not_retried(self):
        for err in (requests.ConnectionError(urllib3.exceptions.ProtocolError(
                        "Connection aborted.", http.client.RemoteDisconnected("closed without response"))),
                    requests.ConnectionError(ConnectionResetError(104, "Connection reset by peer")),
                    requests.exceptions.ChunkedEncodingError("Connection broken: IncompleteRead")):
            self.assertEqual(self.post(err)[0].kind, "failed", type(err.args[0]))

    def test_connect_timeout_and_not_connecting_are_retried(self):
        pool_url = f"/api/webhooks/123456789012345678/{TOKEN}?wait=true"
        timeout = requests.ConnectTimeout(urllib3.exceptions.MaxRetryError(
            None, pool_url, urllib3.exceptions.ConnectTimeoutError(None, "Connection to discord.com timed out.")))
        refused = requests.ConnectionError(urllib3.exceptions.MaxRetryError(
            None, pool_url, urllib3.exceptions.NewConnectionError(None, "Failed to establish a new connection")))
        tls = requests.exceptions.SSLError(ssl.SSLError(1, "handshake failure"))
        for err in (timeout, refused, tls):
            r, _, cap = self.post(err)
            self.assertEqual((r.kind, r.reason), ("retry", "Couldn't reach Discord."), type(err).__name__)
            self.assertNotIn("123456789012345678", cap.text)

    def test_auth_and_missing_are_broken(self):
        for code in (401, 403, 404):
            r = self.post(response(code, {"message": "Unknown Webhook", "code": 10015}))[0]
            self.assertEqual(r.kind, "broken")
            self.assertIn(f"HTTP {code}", r.reason)

    def test_other_4xx_and_redirect_fail(self):
        r = self.post(response(400, {"message": f"bad\x1b[2J\nthing {URL} " + "y" * 300}))[0]
        self.assertEqual(r.kind, "failed")
        self.assertTrue(r.reason.startswith("Discord rejected the post (HTTP 400)"))
        self.assertNotIn(TOKEN[:12], r.reason)
        self.assertNotIn("\x1b", r.reason)
        self.assertLessEqual(len(r.reason), 40 + webhooks.REMOTE_MAX)
        self.assertEqual(self.post(response(302, headers={"Location": "https://evil.example/"}))[0].kind, "failed")


if __name__ == "__main__":
    unittest.main()
