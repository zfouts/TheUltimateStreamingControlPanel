"""Template for a new module — copy to modules/<name>.py (no leading underscore) to enable it.

Files starting with "_" are skipped by the loader. Saving a module file reloads it live.

config.yaml section (handed to on_config):
    example:
      greeting: hi

Buttons that use it:
    - {label: Hello, action: {say: "world"}, state: {coin: heads}, display: {source: clock}}
"""
import random
import time

from core import Module, action, display, notify, state, task


class Example(Module):
    def on_config(self, config):
        super().on_config(config)          # stores it as self.config
        self.greeting = config.get("greeting", "hello")

    def on_load(self):
        self.ticks = 0                     # set up resources (connections, devices) here

    def on_unload(self):
        pass                               # and release them here

    @action("say")
    def say(self, arg):
        """action: {say: "world"} — runs in a worker thread; raise to flash the key red."""
        notify(f"{self.greeting} {arg}")

    @state("coin")
    def coin(self, arg, spec):
        """state: {coin: heads, every: 5} — True switches the key to its `active:` look."""
        return random.choice(["heads", "tails"]) == arg

    @display("clock")
    def clock(self, spec):
        """display: {source: clock, every: 1} — return appearance overrides, or {} for the normal look."""
        return {"label": time.strftime("%H:%M:%S")}

    @task(every=60)
    def tick(self):
        """Runs on the bot's loop every 60 s. Keep it quick; use a thread for slow work."""
        self.ticks += 1


def setup(bot):
    bot.add_module(Example(bot))
