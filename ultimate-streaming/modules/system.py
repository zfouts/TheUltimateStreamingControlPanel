"""Shell commands, notifications, HTTP requests and PulseAudio volume/mute."""
import re
import time

import requests

from core import Module, action, display, notify, sh, spawn, state
from core.schema import UI, Choice, Command, Data, Number, Text


VOLUME_RE = re.compile(r"[+-]?[0-9]{1,3}%|[+-]?[0-9]{1,3}dB")


class System(Module):
    @action("run", ui=UI("Run a program", "Desktop", arg=Command()))
    def run(self, cmd):
        """run: "shell command" — launched detached."""
        spawn(cmd)

    @action("delay", ui=UI("Wait", "Deck", arg=Number("Seconds", min=0, step=0.1)))
    def delay(self, seconds):
        time.sleep(float(seconds))

    @action("notify", ui=UI("Show a notification", "Desktop", arg=Text("Message")))
    def notify(self, msg):
        notify(msg)

    @action("http", ui=UI("Web request (webhook)", "Web", args={
        "url": Text("URL", placeholder="https://..."),
        "method": Choice("POST", "GET", "PUT", "PATCH", "DELETE", label="Method"),
        "json": Data("JSON body"), "headers": Data("Headers")}))
    def http(self, arg):
        """http: {url, method: POST, json: {...}, headers: {...}, timeout: 5}"""
        r = requests.request(arg.get("method", "POST"), arg["url"], json=arg.get("json"),
                             headers=arg.get("headers"), timeout=arg.get("timeout", 5))
        r.raise_for_status()

    @action("volume", ui=UI("Speaker volume", "Desktop", arg=Choice(
        ("up", "Up 5%"), ("down", "Down 5%"), ("mute", "Mute / unmute"), label="Volume", custom=True,
        help="Or a level such as 50%")))
    def volume(self, arg):
        """volume: up | down | mute | 50%  (default output)"""
        cmd = {"up": "set-sink-volume @DEFAULT_SINK@ +5%",
               "down": "set-sink-volume @DEFAULT_SINK@ -5%",
               "mute": "set-sink-mute @DEFAULT_SINK@ toggle"}.get(arg)
        if cmd is None:
            level = str(arg).strip()
            if not VOLUME_RE.fullmatch(level):    # never pass free text to the shell
                raise ValueError("volume must be up, down, mute or a level like 50%, +10% or -10%")
            cmd = f"set-sink-volume @DEFAULT_SINK@ {level}"
        sh(f"pactl {cmd}")

    @action("mic", ui=UI("Microphone mute (system)", "Desktop", arg=Choice(("mute", "Mute / unmute"))))
    def mic(self, arg):
        """mic: mute  (toggles the default input)"""
        sh("pactl set-source-mute @DEFAULT_SOURCE@ toggle")

    @display("clock", ui=UI("Clock", "Desktop", extra={"show": Choice(("time", "Time"), ("date", "Date"),
                                                                     label="Show")}))
    def clock(self, spec):
        """display: {source: clock, show: time | date}"""
        if spec.get("show") == "date":
            return {"label": time.strftime("%a\n%b %-d")}
        return {"label": time.strftime("%-I:%M\n%p")}

    @state("cmd", ui=UI("A command succeeds", "Desktop", arg=Command("Test command"),
                        help="On while the command exits with code 0"))
    def cmd_state(self, cmd, spec):
        """state: {cmd: "shell test"} — exit code 0 means active."""
        return sh(cmd)[0] == 0

    @state("audio", ui=UI("Sound is muted", "Desktop", arg=Choice(("muted", "Speakers muted"),
                                                                ("mic_muted", "Microphone muted"))))
    def audio_state(self, what, spec):
        """state: {audio: muted | mic_muted}"""
        target = {"muted": "sink @DEFAULT_SINK@", "mic_muted": "source @DEFAULT_SOURCE@"}[what]
        kind, name = target.split()
        return "yes" in sh(f"pactl get-{kind}-mute {name}")[1]


def setup(bot):
    bot.add_module(System(bot))
