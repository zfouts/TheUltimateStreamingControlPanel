"""core/chatcmd.py (the chat built-in registry) and how the Twitch Bot uses it."""
import types
import unittest

from tests.helpers import ROOT  # noqa: F401
from tests.test_twitch_bot import BotCase, chat, command

from core import chatcmd  # noqa: E402
from core.webapi import ApiError, Router, bot_api  # noqa: E402
import modules.twitch_bot as tb  # noqa: E402


def builtin(kind="print", **kw):
    args = dict(kind=kind, label="3D print status", help="Says what the printer is doing.",
                placeholders=frozenset({"printer", "status"}), default_response="{printer}: {status}",
                values=lambda arg: {"printer": "Bob", "status": "Printing"},
                sample={"printer": "Sample", "status": "Idle"})
    args.update(kw)
    return chatcmd.Builtin(**args)


def clear_registry():
    for kind in list(chatcmd.all_builtins()):
        chatcmd.unregister(None, kind)


class Registry(unittest.TestCase):
    def setUp(self):
        clear_registry()
        self.addCleanup(clear_registry)

    def test_register_validation(self):
        for bad in (builtin(kind="X"), builtin(kind="a"), builtin(kind="stats"), builtin(kind="text"),
                    builtin(placeholders=frozenset({"Bad"})), builtin(label=""), builtin(values=None),
                    builtin(default_response="x" * 501), "nope"):
            with self.assertRaises(ValueError):
                chatcmd.register(None, bad)
        self.assertEqual(chatcmd.all_builtins(), {})

    def test_replace_same_kind(self):
        chatcmd.register(None, builtin(label="One"))
        chatcmd.register(None, builtin(label="Two"))
        self.assertEqual(len(chatcmd.all_builtins()), 1)
        self.assertEqual(chatcmd.get("print").label, "Two")

    def test_dispatch_on_register_and_unregister(self):
        events = []
        bot = types.SimpleNamespace(dispatch=lambda *a: events.append(a))
        chatcmd.register(bot, builtin())
        chatcmd.unregister(bot, "print")
        chatcmd.unregister(bot, "print")        # already gone: nothing to announce
        self.assertEqual(events, [("chat_builtins_changed",)] * 2)

    def test_describe_shape(self):
        chatcmd.register(None, builtin())
        self.assertEqual(chatcmd.describe(), {"print": {
            "label": "3D print status", "help": "Says what the printer is doing.",
            "placeholders": ["printer", "status"], "default_response": "{printer}: {status}"}})


class Integration(BotCase):
    section = {"enabled": True, "commands": [command("print", trigger="!print", builtin="print", response=None)]}

    def setUp(self):
        clear_registry()
        self.addCleanup(clear_registry)
        super().setUp()

    def say(self, text="!print", **kw):
        before = len(self.tw.sent)
        self.bot.dispatch("chat_message", chat(text, **kw))
        self.settle()
        return self.tw.texts()[before:]

    def register(self, **kw):
        chatcmd.register(self.bot, builtin(**kw))

    def test_skipped_until_registered_then_active_then_inactive(self):
        self.assertNotIn("!print", self.m.triggers)
        self.assertEqual(self.say(), [])
        self.register()
        self.assertIn("!print", self.m.triggers)
        self.assertEqual(self.say(mid="a"), ["Bob: Printing"])
        chatcmd.unregister(self.bot, "print")
        self.assertNotIn("!print", self.m.triggers)
        self.assertEqual(self.say(mid="b"), [])

    def test_values_are_cleaned(self):
        self.register(values=lambda arg: {"printer": "/evil\nline", "status": "ok"})
        self.assertEqual(self.say(), ["evilline: ok"])

    def test_undeclared_placeholder_stays_literal(self):
        self.register(values=lambda arg: {"printer": "Bob", "status": "ok", "secret": "x"},
                      default_response="{printer} {secret}")
        self.assertEqual(self.say(), ["Bob {secret}"])

    def test_explicit_response_validated_against_declared_placeholders(self):
        self.register()
        cfg, errors = tb.validate({"commands": [command("print", trigger="!print", builtin="print",
                                                       response="{status} {nope}")]})
        self.assertEqual([c["id"] for c in cfg["commands"]], ["print"])   # unknown names are just literal
        cfg, errors = tb.validate({"commands": [command("print", trigger="!print", builtin="print",
                                                       response="{messages}")]})
        self.assertEqual(cfg["commands"], [])    # a core placeholder this kind doesn't declare
        cfg, errors = tb.validate({"commands": [command("print", trigger="!print", builtin="print",
                                                       response="/{status}")]})
        self.assertEqual(cfg["commands"], [])
        self.assertTrue(errors)

    def test_unloaded_kind_is_kept(self):
        cfg, errors = tb.validate({"commands": [command("x", trigger="!x", builtin="nothing", response="{any} hi")]})
        self.assertEqual(errors, [])
        self.assertEqual(cfg["commands"][0]["builtin"], "nothing")
        cfg, errors = tb.validate({"commands": [command("x", trigger="!x", builtin="Bad Kind!")]})
        self.assertEqual(cfg["commands"], [])
        self.assertTrue(errors)

    def test_unloaded_command_survives_panel_save_and_works_after_register(self):
        import yaml
        self.assertEqual(self.say(), [])
        with self.assertRaises(ApiError) as e:
            self.m.command_test("print")
        self.assertIn("isn't loaded", str(e.exception))
        view = self.m.config_view()
        self.assertEqual([c["builtin"] for c in view["commands"]], ["print"])
        self.m.save_config(view)
        with open(self.path) as fh:
            saved = yaml.safe_load(fh)["twitch_bot"]["commands"]
        self.assertEqual([c["builtin"] for c in saved], ["print"])
        self.assertEqual(self.say(mid="a"), [])
        self.register()
        self.assertEqual(self.say(mid="b"), ["Bob: Printing"])

    def test_values_raising_posts_nothing_and_test_uses_sample(self):
        def boom(arg):
            raise RuntimeError("printer offline")
        self.register(values=boom)
        self.assertEqual(self.say(), [])
        self.assertEqual(self.m.command_test("print"), {"text": "Sample: Idle", "sample": True})

    def test_test_without_error_is_not_sample(self):
        self.register()
        self.assertEqual(self.m.command_test("print"), {"text": "Bob: Printing"})

    def test_kind_vanished_at_runtime_posts_nothing(self):
        self.register()
        chatcmd.unregister(None, "print")       # no dispatch: the bot still has the command
        self.assertIn("!print", self.m.triggers)
        self.assertEqual(self.say(), [])

    def test_not_trusted(self):
        self.assertNotIn("printer", tb.TRUSTED_VALUES)

    def test_api_state_includes_builtins(self):
        self.register()
        router = Router()
        bot_api.register(router, types.SimpleNamespace(bot=self.bot))
        state = router.gets["/api/bot"]({})
        self.assertEqual(state["builtins"], chatcmd.describe())
        self.assertIn("print", state["builtins"])


if __name__ == "__main__":
    unittest.main()


class Ownership(unittest.TestCase):
    """Added in review: one module can't take another's built-in, and unloading only removes your own."""

    def setUp(self):
        for k in list(chatcmd.all_builtins()):
            chatcmd.unregister(None, k)

    def make(self, owner, kind="owned"):
        return chatcmd.Builtin(kind=kind, label="L", help="H", placeholders=frozenset({"x"}),
                               default_response="{x}", values=owner.values)

    def test_other_owner_refused_and_unregister_checks_owner(self):
        class A:
            def values(self, arg):
                return {}

        class B:
            def values(self, arg):
                return {}
        a, b = A(), B()
        chatcmd.register(None, self.make(a))
        with self.assertRaises(ValueError):
            chatcmd.register(None, self.make(b))
        chatcmd.unregister(None, "owned", owner=b)
        self.assertIsNotNone(chatcmd.get("owned"))
        a2 = A()                                 # a reloaded module's new instance may take over
        chatcmd.register(None, self.make(a2))
        chatcmd.unregister(None, "owned", owner=a)   # the old instance unloading doesn't remove it
        self.assertIs(chatcmd.get("owned").values.__self__, a2)
        chatcmd.unregister_owned(None, [a2])
        self.assertIsNone(chatcmd.get("owned"))
