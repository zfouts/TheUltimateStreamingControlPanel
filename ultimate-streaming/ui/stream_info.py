#!/usr/bin/env python3
"""GTK dialog for editing the Twitch stream title, category and tags.

Launched by modules/twitch.py. Reads JSON on stdin:
    {client_id, access_token, title, game_id, game_name, tags: [...], history: [{title, game_id, game_name, tags}]}
and prints the chosen {title, game_id, game_name, tags} as JSON on stdout (nothing if cancelled).
It only searches categories itself; the bot performs the update.
"""
import json
import sys
import threading

import requests

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

TITLE_MAX, TAG_MAX, TAGS_MAX = 140, 25, 10


class StreamInfoDialog(Gtk.Window):
    def __init__(self, data):
        super().__init__(title="Stream info")
        self.data, self.result = data, None
        self.headers = {"Authorization": f"Bearer {data['access_token']}", "Client-Id": data["client_id"]}
        self.category = {"id": data.get("game_id") or "", "name": data.get("game_name") or ""}
        self.search_gen = 0

        self.set_border_width(14)
        self.set_default_size(520, -1)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_keep_above(True)
        self.connect("destroy", Gtk.main_quit)
        self.connect("key-press-event", lambda w, e: e.keyval == 0xff1b and self.destroy())  # Esc

        grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        self.add(grid)
        row = 0

        history = data.get("history") or []
        if history:
            self.recent = Gtk.ComboBoxText()
            self.recent.append_text("Load a recent setup…")
            for h in history:
                self.recent.append_text(f"{h['title'][:50]}  ·  {h.get('game_name') or 'no category'}")
            self.recent.set_active(0)
            self.recent.connect("changed", self.on_recent)
            grid.attach(Gtk.Label(label="Recent", xalign=1), 0, row, 1, 1)
            grid.attach(self.recent, 1, row, 1, 1)
            row += 1

        self.title = Gtk.Entry(text=data.get("title") or "", max_length=TITLE_MAX, hexpand=True,
                               activates_default=True)
        self.title_count = Gtk.Label(xalign=1)
        self.title.connect("changed", self.on_title)
        grid.attach(Gtk.Label(label="Title", xalign=1), 0, row, 1, 1)
        grid.attach(self.title, 1, row, 1, 1)
        row += 1
        grid.attach(self.title_count, 1, row, 1, 1)
        row += 1

        self.game_store = Gtk.ListStore(str, str)  # name, id
        completion = Gtk.EntryCompletion(model=self.game_store, minimum_key_length=1)
        completion.set_text_column(0)
        completion.set_match_func(lambda *a: True)  # Twitch already filtered the results
        completion.connect("match-selected", self.on_game_selected)
        self.game = Gtk.Entry(text=self.category["name"], completion=completion, activates_default=True,
                              placeholder_text="Start typing, then pick from the list")
        self.game.connect("changed", self.on_game_typed)
        grid.attach(Gtk.Label(label="Category", xalign=1), 0, row, 1, 1)
        grid.attach(self.game, 1, row, 1, 1)
        row += 1

        self.tags = Gtk.Entry(text=", ".join(data.get("tags") or []), activates_default=True,
                              placeholder_text="English, Chill, FirstPlaythrough  (up to 10)")
        grid.attach(Gtk.Label(label="Tags", xalign=1), 0, row, 1, 1)
        grid.attach(self.tags, 1, row, 1, 1)
        row += 1

        self.error = Gtk.Label(xalign=0, wrap=True)
        self.error.set_no_show_all(True)
        grid.attach(self.error, 1, row, 1, 1)
        row += 1

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel, save = Gtk.Button(label="Cancel"), Gtk.Button(label="Update stream")
        save.get_style_context().add_class("suggested-action")
        save.set_can_default(True)
        cancel.connect("clicked", lambda b: self.destroy())
        save.connect("clicked", self.on_save)
        buttons.pack_start(cancel, False, False, 0)
        buttons.pack_start(save, False, False, 0)
        grid.attach(buttons, 0, row, 2, 1)

        self.on_title(self.title)
        self.show_all()
        self.set_default(save)
        self.title.grab_focus()
        self.present()

    # ----- category search

    def search(self, query):
        r = requests.get("https://api.twitch.tv/helix/search/categories", headers=self.headers,
                         params={"query": query, "first": 15}, timeout=8)
        r.raise_for_status()
        return [(c["name"], c["id"]) for c in r.json()["data"]]

    def on_game_typed(self, entry):
        text = entry.get_text().strip()
        if text.lower() != self.category["name"].lower():
            self.category = {"id": "", "name": ""}
        if len(text) < 2:
            return
        self.search_gen += 1
        gen = self.search_gen
        # Debounce: only search once typing pauses for 300 ms.
        GLib.timeout_add(300, lambda: gen == self.search_gen and self._start_search(text, gen) and False)

    def _start_search(self, text, gen):
        def work():
            try:
                results = self.search(text)
            except Exception as e:
                GLib.idle_add(self.set_error, f"Category search failed: {e}")
                return
            GLib.idle_add(self._show_results, results, gen)
        threading.Thread(target=work, daemon=True).start()
        return True

    def _show_results(self, results, gen):
        if gen != self.search_gen:
            return
        self.game_store.clear()
        for name, cid in results:
            self.game_store.append([name, cid])
        typed = self.game.get_text().strip().lower()
        exact = [r for r in results if r[0].lower() == typed]
        if exact:
            self.category = {"name": exact[0][0], "id": exact[0][1]}
        self.game.get_completion().complete()

    def on_game_selected(self, completion, model, it):
        self.category = {"name": model[it][0], "id": model[it][1]}
        self.game.set_text(model[it][0])
        self.set_error("")
        return True

    # ----- other fields

    def set_error(self, text):
        self.error.set_markup(f'<span foreground="#c01c28">{GLib.markup_escape_text(text)}</span>')
        self.error.set_visible(bool(text))

    def on_title(self, entry):
        self.title_count.set_markup(f"<small>{len(entry.get_text())}/{TITLE_MAX}</small>")

    def on_recent(self, combo):
        i = combo.get_active() - 1
        if i < 0:
            return
        h = self.data["history"][i]
        self.title.set_text(h["title"])
        self.game.set_text(h.get("game_name") or "")
        self.category = {"id": h.get("game_id") or "", "name": h.get("game_name") or ""}
        self.tags.set_text(", ".join(h.get("tags") or []))

    def on_save(self, button):
        title = self.title.get_text().strip()
        # Twitch tags can't contain spaces, so "First Playthrough" becomes "FirstPlaythrough".
        tags = [t.replace(" ", "").lstrip("#") for t in self.tags.get_text().split(",") if t.strip()]
        game_text = self.game.get_text().strip()
        problems = []
        if not title:
            problems.append("The title can't be empty.")
        if game_text and not self.category["id"]:
            problems.append(f"Pick “{game_text}” from the category suggestions.")
        if len(tags) > TAGS_MAX:
            problems.append(f"Twitch allows at most {TAGS_MAX} tags.")
        bad = [t for t in tags if not t.isalnum() or len(t) > TAG_MAX]
        if bad:
            problems.append(f"Tags must be letters/numbers only, ≤{TAG_MAX} chars: {', '.join(bad)}")
        if problems:
            self.set_error("\n".join(problems))
            return
        self.result = {"title": title, "game_id": self.category["id"] if game_text else "",
                       "game_name": self.category["name"] if game_text else "", "tags": tags}
        self.destroy()


def main():
    dialog = StreamInfoDialog(json.load(sys.stdin))
    Gtk.main()
    if dialog.result:
        print(json.dumps(dialog.result))


if __name__ == "__main__":
    main()
