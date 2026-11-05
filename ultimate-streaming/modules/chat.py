"""Reads Twitch chat (anonymously, read-only) and broadcasts each message.

Other modules receive messages with:
    @listener("chat_message")
    def on_chat(self, msg): ...       # msg: {"id", "user", "login", "user_id", "text", "action", "color", "mod", "sub", "badges"}

The channel comes from `twitch: channel:` in config.yaml (or `chat: channel:`).
"""
import random
import re
import socket
import ssl
import threading
import time

from core import Module, state
from core.schema import UI, Choice
from core.twitch_ids import LOGIN_RE, USER_ID_RE
from modules.eventsub import clean, collapse_marks

HOST, PORT = "irc.chat.twitch.tv", 6697
COLOR_RE = re.compile(r"#[0-9A-Fa-f]{6}")
TEXT_MAX = 500
NAME_MAX = 40
ACTION_PREFIX = "\x01ACTION "
TAG_ESCAPES = {":": ";", "s": " ", "\\": "\\", "r": "\r", "n": "\n"}
TAG_ESCAPE_RE = re.compile(r"\\(.?)", re.S)
BADGES = ("broadcaster", "moderator", "vip", "subscriber", "founder", "partner", "staff", "turbo", "premium", "bits")


def parse_tags(raw):
    tags = {}
    for part in raw.lstrip("@").split(";"):
        k, _, v = part.partition("=")
        # One pass, so an escaped backslash can't be re-read as the start of another escape.
        # An unknown escape drops the backslash; a trailing lone backslash is dropped.
        tags[k] = TAG_ESCAPE_RE.sub(lambda m: TAG_ESCAPES.get(m.group(1), m.group(1)), v)
    return tags


def chat_text(text):
    """(text, is_action) for the text of a PRIVMSG: a /me message (CTCP ACTION) is unwrapped, other
    CTCP is left alone, then the viewer's text is made safe for the overlay and capped."""
    action = text.startswith(ACTION_PREFIX)
    if action:
        text = text[len(ACTION_PREFIX):]
        if text.endswith("\x01"):
            text = text[:-1]
    return collapse_marks(clean(text, TEXT_MAX * 4))[:TEXT_MAX], action


class Chat(Module):
    def on_config(self, config):
        super().on_config(config)
        channel = config.get("channel") or (self.bot.cfg.get("twitch") or {}).get("channel") or ""
        channel = channel.strip().lower().lstrip("#")
        if channel != getattr(self, "channel", None):
            self.channel = channel
            self._reconnect()

    def on_load(self):
        self.stop = threading.Event()
        self.connected = False
        self.thread = threading.Thread(target=self._run, daemon=True, name="twitch-chat")
        self.thread.start()

    def on_unload(self):
        self.stop.set()
        self._reconnect()

    def _reconnect(self):
        sock = getattr(self, "sock", None)
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)   # wakes a thread blocked in recv()
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _run(self):
        backoff = 1
        while not self.stop.is_set():
            if not self.channel:
                time.sleep(2)
                continue
            started = time.monotonic()
            channel = self.channel
            try:
                self._session(channel)
                backoff = 1
            except Exception as e:
                if time.monotonic() - started > 60:
                    backoff = 1      # a session that ran a while was healthy: retry quickly
                if self._superseded(channel):
                    backoff = 1      # we closed the socket ourselves: not a failure
                else:
                    self.log.info("chat disconnected (%s); retrying in %ds", e, backoff)
            self.connected = False
            if self._superseded(channel):
                continue             # shutting down, or on to the new channel straight away
            self.stop.wait(backoff)
            backoff = min(backoff * 2, 60)

    def _superseded(self, channel):
        """True when the session for `channel` should end because we are stopping or the channel changed."""
        return self.stop.is_set() or channel != self.channel

    def _session(self, channel):
        raw = socket.create_connection((HOST, PORT), timeout=10)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=HOST)
        self.sock.settimeout(360)  # Twitch PINGs about every 5 minutes
        send = lambda line: self.sock.sendall((line + "\r\n").encode())  # noqa: E731
        send("CAP REQ :twitch.tv/tags")
        send(f"NICK justinfan{random.randint(10000, 99999)}")  # anonymous read-only login
        send(f"JOIN #{channel}")
        buf = b""
        while not self._superseded(channel):
            data = self.sock.recv(4096)
            if not data:
                if self._superseded(channel):
                    break            # _reconnect() shut the socket under us
                raise ConnectionError("closed by server")
            buf += data
            *lines, buf = buf.split(b"\r\n")
            for line in lines:
                if self._superseded(channel):
                    break
                self._handle(line.decode("utf-8", "replace"), send)
        self.sock.close()

    def _handle(self, line, send):
        if line.startswith("PING"):
            send("PONG" + line[4:])
            return
        tags = {}
        if line.startswith("@"):
            raw_tags, _, line = line.partition(" ")
            tags = parse_tags(raw_tags)
        parts = line.split(" ", 3)
        if len(parts) >= 2 and parts[1] == "JOIN" and not self.connected:
            self.connected = True
            self.log.info("reading chat for #%s", self.channel)
        if len(parts) == 4 and parts[1] == "PRIVMSG":
            login = parts[0].lstrip(":").split("!")[0].lower()
            login = login if LOGIN_RE.fullmatch(login) else ""
            user = collapse_marks(clean(tags.get("display-name") or login, NAME_MAX)).strip() or login
            text, is_action = chat_text(parts[3][1:] if parts[3].startswith(":") else parts[3])
            user_id = tags.get("user-id") or ""
            user_id = user_id if USER_ID_RE.fullmatch(user_id) else ""
            # Tag values come from the network: badges are allow-listed, the color must be #rrggbb.
            badges = []
            for b in tags.get("badges", "").split(","):
                name = b.partition("/")[0]
                if name in BADGES and name not in badges:
                    badges.append(name)
            color = tags.get("color") or ""
            self.bot.dispatch("chat_message", {
                "id": tags.get("id") or "", "user": user, "login": login, "user_id": user_id,
                "text": text, "action": is_action,
                "color": color if COLOR_RE.fullmatch(color) else "",
                "mod": "moderator" in badges or "broadcaster" in badges, "sub": "subscriber" in badges,
                "badges": badges})

    def snapshot(self):
        return {"connected": bool(self.connected), "channel": self.channel or ""}

    @state("chat", ui=UI("Reading chat", "Twitch", arg=Choice(("connected", "connected"))))
    def chat_state(self, what, spec):
        """state: {chat: connected}"""
        return self.connected


def setup(bot):
    bot.add_module(Chat(bot))
