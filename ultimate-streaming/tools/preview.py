#!/usr/bin/env python3
"""Render a config's pages to PNG contact sheets, no Stream Deck needed.

    python3 tools/preview.py                          every layout in your config → ./preview/
    python3 tools/preview.py --model "Stream Deck Mini" --out /tmp/deck
    python3 tools/preview.py --config config.example.yaml

Placeholder keys (scene_slot, steam_game_slot, station_slot) are drawn as labelled
placeholders, since the real names come from OBS / Steam / your radio folders at runtime.
"""
import argparse
import logging
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "vendor"))
sys.path.insert(0, ROOT)

from PIL import Image, ImageDraw  # noqa: E402

from core.bot import DeckBot  # noqa: E402
from core.models import MODELS, FakeDeck  # noqa: E402
from core.paths import config_path  # noqa: E402
from core.render import Renderer  # noqa: E402

PLACEHOLDERS = {"scene_slot": ("Scene {}", "#203050"), "steam_game_slot": ("Game {}", "#1b2838"),
                "station_slot": ("Station {}", "#3a2d5c")}


def placeholder(btn):
    for key, (label, bg) in PLACEHOLDERS.items():
        if key in btn:
            return {"label": label.format(btn[key]), "bg": bg, **{k: v for k, v in btn.items() if k != key}}
    return btn


def sheet(bot, deck, renderer, page):
    w, h = deck.size
    gap, pad, title_h = 8, 16, 34
    keys = bot.pages[page]
    n_rows = deck.rows + (1 if deck.touch else 0)
    dials = bot.page_dials.get(page) or []
    dial_h = 70 if deck.dials else 0
    width = pad * 2 + deck.cols * w + (deck.cols - 1) * gap
    height = title_h + pad * 2 + n_rows * h + (n_rows - 1) * gap + (dial_h + gap if dial_h else 0)
    img = Image.new("RGB", (width, height), "#0b0b0d")
    draw = ImageDraw.Draw(img)
    draw.text((pad, 10), f"{deck.model} · {bot.layout_name} · page '{page}'", fill="#cccccc")
    for i in range(deck.rows * deck.cols + deck.touch):
        r, c = divmod(i, deck.cols)
        x, y = pad + c * (w + gap), title_h + pad + r * (h + gap)
        btn = keys[i] if i < len(keys) else None
        if deck.model.endswith("Pedal"):  # no display: just say what each pedal does
            label = (btn or {}).get("label", "—")
            draw.rectangle((x, y, x + w, y + h), outline="#555555")
            draw.text((x + w // 2, y + h // 2), label, fill="#ffffff", anchor="mm")
            continue
        tile = renderer.image(placeholder(btn)) if btn else Image.new("RGB", (w, h), "#000000")
        img.paste(tile, (x, y))
        if i >= deck.rows * deck.cols:
            draw.text((x, y - 2), "touch", fill="#888888", anchor="lb")
    if dial_h:
        y = height - pad - dial_h
        seg = (width - 2 * pad) // deck.dials
        for d in range(deck.dials):
            spec = dials[d] if d < len(dials) else {}
            draw.rounded_rectangle((pad + d * seg + 2, y, pad + (d + 1) * seg - 2, y + dial_h), 8,
                                   fill="#1f1f24" if spec else "#111111")
            draw.text((pad + d * seg + seg // 2, y + dial_h // 2), spec.get("label", "") if spec else "",
                      fill="#ffffff", anchor="mm", align="center")
    return img


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", default=config_path())
    ap.add_argument("--model", choices=list(MODELS), action="append", help="default: all models")
    ap.add_argument("--out", default="preview")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    os.makedirs(args.out, exist_ok=True)
    for model in args.model or MODELS:
        deck = FakeDeck(model)
        bot = DeckBot(root=ROOT, config_path=args.config)
        bot.deck = deck
        bot.load_config(initial=True)
        renderer = Renderer(deck, ROOT, bot.cfg.get("font"), bot.cfg.get("emoji_font"))
        for page in bot.pages:
            path = os.path.join(args.out, f"{model.replace(' ', '_').replace('+', 'Plus')}-{page}.png")
            sheet(bot, deck, renderer, page).save(path)
        print(f"{model:22} layout {bot.layout_name:5} → {len(bot.pages)} pages")
    print(f"sheets written to {os.path.abspath(args.out)}")


if __name__ == "__main__":
    main()
