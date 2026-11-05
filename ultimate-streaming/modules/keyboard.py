"""Keystrokes: combos, typed text, and keys held while a deck key is held.

Keys go through a virtual kernel keyboard (/dev/uinput), so apps see them
exactly like a physical keyboard's; X's XTest is the fallback. X is still
used to translate key names into keycodes for the current layout.
"""
import ctypes
import fcntl
import os
import struct
import threading
import time

from core import Module, action
from core.schema import UI, Text
from core.schema import Keys as KeyCombo  # this module has its own Keys class


class UInputKeyboard:
    """A virtual kernel keyboard named "Stream Deck keyboard"."""

    UI_SET_EVBIT, UI_SET_KEYBIT, UI_DEV_CREATE, UI_DEV_DESTROY = 0x40045564, 0x40045565, 0x5501, 0x5502
    EV_SYN, EV_KEY = 0, 1

    def __init__(self):
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        fcntl.ioctl(self.fd, self.UI_SET_EVBIT, self.EV_KEY)
        for code in range(1, 256):
            fcntl.ioctl(self.fd, self.UI_SET_KEYBIT, code)
        # struct uinput_user_dev: name[80], input_id{bustype, vendor, product, version},
        # ff_effects_max, then absmax/absmin/absfuzz/absflat[64] each.
        os.write(self.fd, b"Stream Deck keyboard".ljust(80, b"\0")
                 + struct.pack("<HHHHi", 0x03, 0x0fd9, 0x008f, 1, 0) + bytes(4 * 64 * 4))
        fcntl.ioctl(self.fd, self.UI_DEV_CREATE)
        time.sleep(0.5)  # let X/libinput pick up the new device before the first key

    def key(self, x_keycode, down):
        # X keycodes are evdev codes offset by 8.
        os.write(self.fd, struct.pack("llHHi", 0, 0, self.EV_KEY, x_keycode - 8, int(down))
                 + struct.pack("llHHi", 0, 0, self.EV_SYN, 0, 0))

    def close(self):
        fd, self.fd = self.fd, None
        if fd is None:
            return
        try:
            fcntl.ioctl(fd, self.UI_DEV_DESTROY)
        finally:
            os.close(fd)


class Keys:
    """Sends key combos and text to the focused window (uinput, else XTest)."""

    ALIASES = {
        "ctrl": "Control_L", "control": "Control_L", "shift": "Shift_L",
        "alt": "Alt_L", "super": "Super_L", "win": "Super_L", "meta": "Super_L",
        "enter": "Return", "esc": "Escape", "del": "Delete", "space": "space",
        "tab": "Tab", "backspace": "BackSpace", "up": "Up", "down": "Down",
        "left": "Left", "right": "Right", "pgup": "Prior", "pgdn": "Next",
        "home": "Home", "end": "End", "print": "Print", "ins": "Insert",
        "play": "XF86AudioPlay", "next": "XF86AudioNext", "prev": "XF86AudioPrev",
        "-": "minus", "=": "equal", "+": "plus", ",": "comma", ".": "period",
        "/": "slash", ";": "semicolon", "'": "apostrophe", "`": "grave",
        "[": "bracketleft", "]": "bracketright", "\\": "backslash",
    }

    def __init__(self, log):
        self.x = ctypes.cdll.LoadLibrary("libX11.so.6")
        self.xt = ctypes.cdll.LoadLibrary("libXtst.so.6")
        self.x.XOpenDisplay.restype = ctypes.c_void_p
        self.x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.x.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self.x.XStringToKeysym.restype = ctypes.c_ulong
        self.x.XStringToKeysym.argtypes = [ctypes.c_char_p]
        self.x.XKeysymToKeycode.restype = ctypes.c_ubyte
        self.x.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.x.XKeycodeToKeysym.restype = ctypes.c_ulong
        self.x.XKeycodeToKeysym.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_int]
        self.x.XFlush.argtypes = [ctypes.c_void_p]
        self.xt.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
        self.dpy = self.x.XOpenDisplay(None)
        if not self.dpy:
            raise RuntimeError("cannot open X display (is DISPLAY set?)")
        self.lock = threading.Lock()
        try:
            self.uinput = UInputKeyboard()
        except OSError as e:
            log.warning("uinput unavailable (%s); falling back to XTest", e)
            self.uinput = None

    def close(self):
        if self.uinput:
            self.uinput.close()
        if self.dpy:
            self.x.XCloseDisplay(self.dpy)
            self.dpy = None

    def _key(self, code, down):
        if self.uinput:
            self.uinput.key(code, down)
        else:
            self.xt.XTestFakeKeyEvent(self.dpy, code, int(down), 0)
            self.x.XFlush(self.dpy)

    def _keycode(self, name):
        name = self.ALIASES.get(name.lower(), name)
        sym = self.x.XStringToKeysym(name.encode())
        if not sym and len(name) == 1:
            sym = ord(name)
        code = self.x.XKeysymToKeycode(self.dpy, sym) if sym else 0
        if not code:
            raise ValueError(f"unknown key {name!r}")
        return code, sym

    def _tap(self, codes, hold):
        for c in codes:
            self._key(c, True)
        time.sleep(hold)
        for c in reversed(codes):
            self._key(c, False)

    def _codes(self, combo):
        return [self._keycode(p)[0] for p in combo.split("+") if p] or [self._keycode("+")[0]]

    def press(self, combo):
        with self.lock:
            for c in self._codes(combo):
                self._key(c, True)

    def release(self, combo):
        with self.lock:
            for c in reversed(self._codes(combo)):
                self._key(c, False)

    def combo(self, combo, hold=0.04):
        """Press e.g. 'ctrl+shift+m' or 'F12'."""
        with self.lock:
            self._tap(self._codes(combo), hold)

    def type(self, text, delay=0.01):
        shift = self._keycode("Shift_L")[0]
        with self.lock:
            for ch in text:
                name = {"\n": "Return", "\t": "Tab", " ": "space"}.get(ch, ch)
                code, sym = self._keycode(name)
                unshifted = self.x.XKeycodeToKeysym(self.dpy, code, 0)
                self._tap([shift, code] if unshifted != sym else [code], 0.005)
                time.sleep(delay)


class Keyboard(Module):
    def on_load(self):
        self._keys = None
        try:
            self.keys_backend()  # create the virtual keyboard now so the first press isn't lost
        except Exception as e:
            self.log.warning("keyboard not ready yet (%s); will retry on first key", e)

    def on_unload(self):
        if self._keys:
            self._keys.close()

    def keys_backend(self):
        if not self._keys:
            self._keys = Keys(self.log)
        return self._keys

    @action("keys", ui=UI("Press keys", "Desktop", arg=KeyCombo()))
    def keys(self, arg):
        """keys: "ctrl+shift+m"  (or a list of combos, pressed in turn)"""
        for combo in [arg] if isinstance(arg, str) else arg:
            self.keys_backend().combo(combo)

    @action("type", ui=UI("Type text", "Desktop", arg=Text("Text", multiline=True)))
    def type_text(self, text):
        """type: "some text" """
        self.keys_backend().type(text)

    @action("hold", on_release=True, ui=UI("Hold keys while pressed (push-to-talk)", "Desktop", arg=KeyCombo(),
                                           help="Only works as the key's only action"))
    def hold(self, combo, down):
        """hold: "ctrl+shift+alt+t" — held down for as long as the deck key is (push-to-talk)."""
        (self.keys_backend().press if down else self.keys_backend().release)(combo)


def setup(bot):
    bot.add_module(Keyboard(bot))
