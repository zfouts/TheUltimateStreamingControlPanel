"""Page switching and deck brightness (on the deck whose key ran the action)."""
from core import Module, action, state
from core.schema import UI, Choice, Source


class Navigation(Module):
    LEVELS = [10, 30, 60, 100]

    def deck(self):
        """The deck to act on: the one whose key ran us (a DeckSession), or the bot itself on a
        core without sessions; both have goto(), brightness and set_brightness()."""
        session = self.bot.session() if hasattr(self.bot, "session") else None
        return session or self.bot

    @action("page", ui=UI("Go to page", "Deck", arg=Source("pages", "Page")))
    def page(self, name):
        """page: name | back"""
        self.deck().goto(name)

    @action("brightness", ui=UI("Deck brightness", "Deck", arg=Choice(
        ("cycle", "Cycle 10/30/60/100%"), ("up", "Brighter"), ("down", "Dimmer"), ("sleep", "Sleep / wake"),
        label="Brightness", custom=True)))
    def brightness(self, arg):
        """brightness: cycle | up | down | sleep (toggle off/on) | 0-100"""
        deck = self.deck()
        awake = self.__dict__.setdefault("awake_levels", {})   # per deck, for sleep / wake
        serial = getattr(deck, "serial", None)
        cur = deck.brightness
        if arg == "sleep":
            if cur > 0:
                awake[serial], cur = cur, 0
            else:
                cur = awake.get(serial, 60)
        elif arg == "cycle":
            cur = self.LEVELS[(self.LEVELS.index(cur) + 1) % len(self.LEVELS)] if cur in self.LEVELS else self.LEVELS[0]
        elif arg == "up":
            cur += 20
        elif arg == "down":
            cur -= 20
        else:
            cur = int(arg)
        deck.set_brightness(cur)

    @state("deck_asleep", ui=UI("Deck is asleep", "Deck", arg=Choice((True, "yes"))))
    def asleep(self, arg, spec):
        """state: {deck_asleep: true}"""
        return self.deck().brightness == 0


def setup(bot):
    bot.add_module(Navigation(bot))
