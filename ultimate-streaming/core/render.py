"""Draws a button dict (label, icon, colors) into a key image."""
import glob
import logging
import os
import subprocess
import warnings

from PIL import Image, ImageColor, ImageDraw, ImageFont

log = logging.getLogger("deck")

# Only these decoders are ever used for icons / backgrounds (Pillow's EPS plugin runs Ghostscript).
SAFE_FORMATS = ["PNG", "JPEG", "GIF", "WEBP"]
MAX_PIXELS = 16_000_000
_warned = set()  # paths already reported, so a bad icon doesn't log on every render

AMBER = (255, 176, 0, 255)
MAX_FONT_SIZE = 96  # `size:` is in 96 px-key units; nothing bigger fits on any key

# Tried in order when fontconfig (fc-match) isn't available.
FALLBACK_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",   # Debian / Ubuntu
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",  # Fedora
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",                # Arch
]
FALLBACK_EMOJI = [
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",       # Debian / Ubuntu
    "/usr/share/fonts/google-noto-color-emoji-fonts/NotoColorEmoji.ttf",  # Fedora
    "/usr/share/fonts/noto/NotoColorEmoji.ttf",                # Arch
]


def find_font(pattern, fallbacks):
    """A font file for a fontconfig pattern (e.g. "DejaVu Sans:bold"), or None."""
    try:
        out = subprocess.run(["fc-match", "-f", "%{file}", pattern], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        # fc-match always answers with *something*; only accept it if the family matches.
        fam = subprocess.run(["fc-match", "-f", "%{family}", pattern], capture_output=True,
                             text=True, timeout=5).stdout.lower()
        if out and os.path.exists(out) and pattern.split(":")[0].lower() in fam:
            return out
    except (OSError, subprocess.SubprocessError):
        pass
    return next((f for f in fallbacks if os.path.exists(f)), None)


def _data_dirs():
    dirs = [os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")]
    dirs += (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    dirs += [os.path.expanduser("~/.local/share/flatpak/exports/share"), "/var/lib/flatpak/exports/share"]
    return [d for d in dict.fromkeys(dirs) if d]


# hicolor first (where apps install their icons), then any theme, then pixmaps.
ICON_DIRS = ([os.path.join(d, "icons", "hicolor") for d in _data_dirs()]
             + [os.path.join(d, "icons") for d in _data_dirs()]
             + [os.path.join(d, "pixmaps") for d in _data_dirs()])


def find_icon(name, root):
    if os.path.isabs(os.path.expanduser(name)):
        return os.path.expanduser(name)
    local = os.path.join(root, "icons", name)
    if os.path.exists(local):
        return local
    for base in ICON_DIRS:
        hits = glob.glob(f"{base}/**/{name}.png", recursive=True)
        if hits:
            # Prefer the largest size directory, e.g. 256x256 over 48x48.
            def size(p):
                for part in p.split("/"):
                    if "x" in part and part.split("x")[0].isdigit():
                        return int(part.split("x")[0])
                return 0
            return max(hits, key=size)
    return None


def open_safe(path):
    """An image file as a loaded RGBA/RGB-convertible PIL image, or None (logged once per path)
    if it is missing, in an unsupported format, or too large. The size is checked before decoding."""
    path = os.path.expanduser(str(path))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path, formats=SAFE_FORMATS) as img:
                if img.width * img.height > MAX_PIXELS:
                    raise ValueError(f"image too large ({img.width}x{img.height})")
                img.load()
                return img.copy()
    except (Exception, Image.DecompressionBombWarning) as e:
        if path not in _warned:
            _warned.add(path)
            log.warning("image %s: %s", path, e)
        return None


def valid_color(value):
    """True if Pillow can draw with `value`: a color string ("#9146ff", "purple", "rgb(1,2,3)")
    or a 3/4-tuple of 0-255 ints."""
    if isinstance(value, str):
        try:
            ImageColor.getrgb(value)
            return True
        except ValueError:
            return False
    if isinstance(value, (tuple, list)) and len(value) in (3, 4):
        return all(isinstance(c, int) and not isinstance(c, bool) and 0 <= c <= 255 for c in value)
    return False


def _where(btn):
    return str(btn.get("label") or btn.get("icon") or "?").replace("\n", " ")


def _warn_once(what, btn, value, fallback):
    key = (what, _where(btn), repr(value))
    if key not in _warned:
        _warned.add(key)
        log.warning("key %r: %s %r isn't a color Pillow understands; using %s", key[1], what, value, fallback)


def color(btn, field, default):
    """btn[field] if it is a usable color, else `default` (and one warning per key and value)."""
    value = btn.get(field)
    if value is None:
        return default
    if valid_color(value):
        return tuple(value) if isinstance(value, list) else value
    _warn_once(field, btn, value, default)
    return default


def font_size(btn):
    """The key's `size:` as a whole number in 10..MAX_FONT_SIZE, or None if unset or unusable."""
    value = btn.get("size")
    if value in (None, ""):
        return None
    try:
        if isinstance(value, bool):
            raise ValueError
        n = int(float(value))
        if not n > 0:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        key = ("size", _where(btn), repr(value))
        if key not in _warned:
            _warned.add(key)
            log.warning("key %r: size %r isn't a positive number; using the default", key[1], value)
        return None
    return max(10, min(MAX_FONT_SIZE, n))


def _has_review(action):
    if isinstance(action, dict):
        return "needs_review" in action
    if isinstance(action, list):
        return any(_has_review(a) for a in action)
    return False


def needs_attention(btn):
    """True for an imported key that isn't active yet (awaiting review, or missing its action)."""
    return bool(btn.get("needs_action")) or _has_review(btn.get("action"))


class Renderer:
    _resolved = {}  # font pattern -> file, shared across renderers (fc-match is slow-ish)

    def __init__(self, deck, root, font=None, emoji_font=None):
        """font / emoji_font: a file path or a fontconfig pattern (config.yaml `font:` / `emoji_font:`)."""
        self.deck = deck
        self.root = root
        self.font_path = self._resolve(font or "DejaVu Sans:bold", FALLBACK_FONTS)
        self.emoji_path = self._resolve(emoji_font or "Noto Color Emoji", FALLBACK_EMOJI)
        if not self.font_path:
            log.warning("no TrueType font found (install DejaVu Sans, or set `font:`); using Pillow's built-in font")
        if not self.emoji_path:
            log.warning("no color emoji font found (install Noto Color Emoji); emoji icons are hidden")
        self.cache = {}
        self.scale = 1.0

    @classmethod
    def _resolve(cls, spec, fallbacks):
        if os.path.exists(os.path.expanduser(spec)):
            return os.path.expanduser(spec)
        if spec not in cls._resolved:
            cls._resolved[spec] = find_font(spec, fallbacks)
        return cls._resolved[spec]

    def _font(self, size):
        if self.font_path:
            return ImageFont.truetype(self.font_path, size)
        return ImageFont.load_default(size)  # Pillow >= 10.1

    def _icon(self, spec, box):
        key = (spec, box)
        if key not in self.cache:
            img = None
            try:
                if spec.startswith("emoji:"):
                    if not self.emoji_path:
                        raise FileNotFoundError("no emoji font")
                    f = ImageFont.truetype(self.emoji_path, 109)  # color bitmap fonts only come in this size
                    img = Image.new("RGBA", (136, 128), (0, 0, 0, 0))
                    ImageDraw.Draw(img).text((68, 64), spec[6:], font=f, anchor="mm", embedded_color=True)
                    img = img.crop(img.getbbox())
                else:
                    path = find_icon(spec, self.root)
                    img = open_safe(path) if path else None
                    img = img.convert("RGBA") if img else None
                if img:
                    img.thumbnail((box, box), Image.LANCZOS)
            except Exception as e:
                log.warning("icon %s: %s", spec, e)
            self.cache[key] = img
        return self.cache[key]

    def _image_path(self, image):
        """A key's `image`: absolute and ~ paths as written, a relative one inside the icons/ folder
        (like find_icon), never relative to the working directory."""
        image = str(image)
        if os.path.isabs(os.path.expanduser(image)):
            return os.path.expanduser(image)
        return os.path.join(self.root, "icons", image)

    def _cover(self, path, w, h):
        """A full-key background image, scaled and center-cropped to cover the key."""
        key = ("cover", path, w, h)
        if key not in self.cache:
            img = None
            try:
                src = open_safe(path)
                if src is None:
                    raise ValueError("unavailable")
                src = src.convert("RGB")
                scale = max(w / src.width, h / src.height)
                src = src.resize((max(w, round(src.width * scale)), max(h, round(src.height * scale))), Image.LANCZOS)
                left, top = (src.width - w) // 2, (src.height - h) // 2
                img = src.crop((left, top, left + w, top + h))
            except ValueError:
                pass  # open_safe already logged why
            except Exception as e:
                log.warning("image %s: %s", path, e)
            self.cache[key] = img
        return self.cache[key]

    def _scaled(self, sizes):
        """Font/icon sizes are designed for 96 px keys (XL); scale them for this deck."""
        return tuple(dict.fromkeys(max(8, round(s * self.scale)) for s in sizes))

    def _wrap(self, draw, text, width, sizes=(18, 16, 14, 12, 11, 10)):
        """Find the largest font size where the wrapped text fits."""
        for size in sizes:
            font = self._font(size)
            lines = []
            for para in str(text).replace("\\n", "\n").split("\n"):
                cur = ""
                for word in para.split():
                    trial = f"{cur} {word}".strip()
                    if draw.textlength(trial, font=font) <= width or not cur:
                        cur = trial
                    else:
                        lines.append(cur)
                        cur = word
                lines.append(cur)
            if all(draw.textlength(l, font=font) <= width for l in lines) and len(lines) <= 3:
                return font, lines
        return font, lines

    def image(self, btn, pressed=False):
        """Render to a PIL image (used for previews as well as the deck)."""
        w, h = self.deck.key_image_format()["size"]
        self.scale = min(w, h) / 96
        img = Image.new("RGB", (w, h), color(btn, "bg", "#000000"))
        cover = self._cover(self._image_path(btn["image"]), w, h) if btn.get("image") else None
        if cover:
            img.paste(cover)
        draw = ImageDraw.Draw(img, "RGBA")
        label, icon = btn.get("label"), btn.get("icon")
        font, lines, text_h = None, [], 0
        if label:
            # Smaller text over artwork, so the picture stays visible.
            sizes = (13, 12, 11, 10) if cover else (18, 16, 14, 12, 11, 10)
            size = font_size(btn)
            if size:  # e.g. a clock key: start bigger, shrink until it fits
                sizes = tuple(range(size, 9, -2))
            font, lines = self._wrap(draw, label, w - 8, self._scaled(sizes))
            text_h = (font.size + 2) * len(lines)
        if icon:
            box = min(round(56 * self.scale), h - text_h - 12) if label else round(80 * self.scale)
            ic = self._icon(str(icon), box)
            if ic:
                top = round(5 * self.scale) if label else (h - ic.height) // 2
                img.paste(ic, ((w - ic.width) // 2, top), ic)
        if label and cover:
            # Dark band behind the label so it stays readable over artwork.
            draw.rectangle((0, h - text_h - 8, w, h), fill=(0, 0, 0, 170))
        if label:
            fg = color(btn, "fg", "#ffffff")
            lh = font.size + 2
            y = (h - text_h + lh) // 2 if not (icon or cover) else h - text_h + lh // 2 - 4
            for i, line in enumerate(lines):
                draw.text((w // 2, y + i * lh), line, font=font, fill=fg, anchor="mm")
        if needs_attention(btn):
            r = max(6, round(9 * self.scale))
            m = max(2, round(3 * self.scale))
            cx, cy = w - m - r, m + r
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=AMBER)
            draw.text((cx, cy), "!", font=self._font(max(8, round(14 * self.scale))), fill="#000000", anchor="mm")
        if pressed:
            img = Image.blend(img, Image.new("RGB", (w, h), "#ffffff"), 0.35)
        return img

    def render(self, btn, pressed=False):
        from StreamDeck.ImageHelpers import PILHelper
        return PILHelper.to_native_key_format(self.deck, self.image(btn, pressed))

    # ----- touch strip (Stream Deck + / + XL) and info screen (Neo)

    def strip_size(self):
        fmt = self.deck.touchscreen_image_format() if self.deck.is_touch() else {}
        return tuple(fmt.get("size") or (0, 0))

    def strip(self, dials, count):
        """One segment per dial with its label, so you can see what each dial does."""
        w, h = self.strip_size()
        img = Image.new("RGB", (w, h), "#000000")
        draw = ImageDraw.Draw(img)
        seg = w // count
        self.scale = h / 100
        for i in range(count):
            spec = dials[i] if i < len(dials) else None
            if not spec:
                continue
            x0 = i * seg
            draw.rectangle((x0 + 3, 3, x0 + seg - 4, h - 4), fill=color(spec, "bg", "#1f1f24"))
            font, lines = self._wrap(draw, spec.get("label", ""), seg - 16, self._scaled((24, 20, 18, 16, 14)))
            lh = font.size + 4
            y = (h - lh * len(lines) + lh) // 2
            fg = color(spec, "fg", "#ffffff")
            for j, line in enumerate(lines):
                draw.text((x0 + seg // 2, y + j * lh), line, font=font, fill=fg, anchor="mm")
        from StreamDeck.ImageHelpers import PILHelper
        return PILHelper.to_native_touchscreen_format(self.deck, img)

    def screen_size(self):
        try:
            fmt = self.deck.screen_image_format()
        except Exception:
            return None
        size = tuple(fmt.get("size") or (0, 0))
        return size if size[0] else None

    def screen(self, text):
        w, h = self.screen_size()
        img = Image.new("RGB", (w, h), "#1f1f24")
        draw = ImageDraw.Draw(img)
        self.scale = h / 60
        font, lines = self._wrap(draw, text, w - 12, self._scaled((22, 18, 16, 14)))
        lh = font.size + 2
        y = (h - lh * len(lines) + lh) // 2
        for j, line in enumerate(lines):
            draw.text((w // 2, y + j * lh), line, font=font, fill="#ffffff", anchor="mm")
        from StreamDeck.ImageHelpers import PILHelper
        return PILHelper.to_native_screen_format(self.deck, img)
