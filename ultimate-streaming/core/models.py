"""Stream Deck models: grid, key size and extras (matches python-elgato-streamdeck)."""

# name: (rows, cols, key size (w, h), touch keys, dials, has display)
MODELS = {
    "Stream Deck Mini": (2, 3, (80, 80), 0, 0, True),
    "Stream Deck Original": (3, 5, (72, 72), 0, 0, True),
    "Stream Deck Neo": (2, 4, (96, 96), 2, 0, True),
    "Stream Deck XL": (4, 8, (96, 96), 0, 0, True),
    "Stream Deck +": (2, 4, (120, 120), 0, 4, True),
    "Stream Deck + XL": (4, 9, (112, 112), 0, 6, True),
    "Stream Deck Studio": (2, 16, (80, 120), 0, 2, True),
    "Stream Deck Pedal": (1, 3, (96, 96), 0, 0, False),
}


def as_dicts():
    return {name: {"rows": r, "cols": c, "key_size": list(size), "touch_keys": t, "dials": d, "visual": v}
            for name, (r, c, size, t, d, v) in MODELS.items()}


def fake_serial(model):
    """A stable serial for a fake deck: "Stream Deck + XL" -> "FAKE-STREAM-DECK-PLUS-XL"."""
    return "FAKE-" + "-".join(model.upper().replace("+", "PLUS").split())


class FakeDeck:
    """Enough of the StreamDeck API for rendering, layout selection and attaching (as a
    DeckSession) without hardware. Drawing and device calls are accepted and ignored."""

    def __init__(self, model, serial=None):
        self.model = model
        self.serial = serial or fake_serial(model)
        self.rows, self.cols, self.size, self.touch, self.dials, self.visual = MODELS[model]

    def key_layout(self): return self.rows, self.cols
    def deck_type(self): return self.model
    def touch_key_count(self): return self.touch
    def dial_count(self): return self.dials
    def is_visual(self): return self.visual
    def is_touch(self): return False
    def key_count(self): return self.rows * self.cols
    def key_image_format(self): return {"size": self.size, "format": "JPEG", "flip": (False, False), "rotation": 0}
    def get_serial_number(self): return self.serial

    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def open(self): pass
    def close(self): pass
    def reset(self): pass
    def set_key_callback(self, cb): pass
    def set_dial_callback(self, cb): pass
    def set_touchscreen_callback(self, cb): pass
    def set_brightness(self, level): pass
    def set_key_image(self, key, image): pass
