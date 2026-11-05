"""Steam: launch installed games (with their cover art) and open parts of the Steam client.

Games are read from the local Steam libraries (appmanifest_*.acf) at startup and then
hourly, most recently played first; Proton, runtimes and other tools are skipped. Artwork comes from
Steam's own library cache, so nothing is downloaded.

config.yaml (optional):
    steam:
      root: ~/.local/share/Steam   # default: auto-detected (native, ~/.steam/steam, or Flatpak Steam)
      hide: [123456]               # app ids to leave off the deck
      command: steam               # default: `steam` if on PATH, else xdg-open (Flatpak)
"""
import glob
import os
import re
import shlex
import shutil

from core import Module, action, expander, source, spawn, task
from core.schema import UI, Choice, Number, Source

TOOL_NAMES = re.compile(r"^(Proton\b|Steam Linux Runtime|Steamworks Common|SteamVR|Steam Audio)", re.I)
OPEN_PAGES = {"main": "steam://open/main", "library": "steam://open/games", "bigpicture": "steam://open/bigpicture",
              "friends": "steam://open/friends", "downloads": "steam://open/downloads",
              "screenshots": "steam://open/screenshots", "store": "steam://store", "settings": "steam://open/settings"}
ART = ("library_capsule.jpg", "library_600x900.jpg", "library_header.jpg", "header.jpg")
STEAM_ROOTS = ("~/.local/share/Steam", "~/.steam/steam", "~/.var/app/com.valvesoftware.Steam/.local/share/Steam")


def vdf_values(text, key):
    return re.findall(r'"%s"\s+"([^"]*)"' % re.escape(key), text)


class Steam(Module):
    def on_load(self):
        self.games = []

    @property
    def root(self):
        if self.config.get("root"):
            return os.path.expanduser(self.config["root"])
        roots = [os.path.expanduser(r) for r in STEAM_ROOTS]
        return next((r for r in roots if os.path.isdir(os.path.join(r, "steamapps"))), roots[0])

    def libraries(self):
        try:
            paths = vdf_values(open(os.path.join(self.root, "steamapps", "libraryfolders.vdf")).read(), "path")
        except OSError:
            paths = []
        return paths or [self.root]

    def scan(self):
        """Installed games as dicts (appid, name, last_played, art), most recently played first."""
        hidden = {str(a) for a in self.config.get("hide", [])}
        games = []
        for lib in self.libraries():
            for manifest in glob.glob(os.path.join(lib, "steamapps", "appmanifest_*.acf")):
                try:
                    text = open(manifest, errors="replace").read()
                except OSError:
                    continue
                appid, name = (vdf_values(text, "appid") or [""])[0], (vdf_values(text, "name") or [""])[0]
                if not appid or appid in hidden or TOOL_NAMES.match(name):
                    continue
                last = int((vdf_values(text, "LastPlayed") or ["0"])[0] or 0)
                games.append({"appid": appid, "name": name, "last_played": last, "art": self.artwork(appid)})
        return sorted(games, key=lambda g: (-g["last_played"], g["name"].lower()))

    def artwork(self, appid):
        cache = os.path.join(self.root, "appcache", "librarycache", appid)
        for name in ART:
            hits = glob.glob(os.path.join(cache, "**", name), recursive=True)
            if hits:
                return hits[0]
        return None

    @task(every=3600, on_page_change=False)
    def refresh(self):
        games = self.scan()
        # Redraw on any change: new/removed games, play order, or artwork Steam has just cached.
        if games != self.games:
            self.games = games
            self.bot.invalidate()

    # ----- deck

    @expander("steam_game_slot", ui=UI("Steam game slot", "Steam", arg=Number("Game number", min=1),
                                       help="1 = most recently played"))
    def game_button(self, slot, btn):
        """{steam_game_slot: 1} becomes a launch key for the most recently played game, and so on."""
        n = int(slot) - 1
        if n >= len(self.games):
            return None
        g = self.games[n]
        return {"label": g["name"], "image": g["art"], "icon": None if g["art"] else "steam",
                "bg": "#1b2838", "action": {"steam": {"run": g["appid"]}}, **btn}

    @source("steam_games")
    def game_choices(self):
        return [{"value": int(g["appid"]), "label": g["name"]} for g in (self.games or self.scan())]

    @action("steam", ui=UI("Steam", "Steam", scalar="page", args={
        "page": Choice(("library", "Library"), ("bigpicture", "Big Picture"), ("friends", "Friends"),
                       ("downloads", "Downloads"), ("screenshots", "Screenshots"), ("store", "Store"),
                       ("settings", "Settings"), label="Open"),
        "run": Source("steam_games", "Or launch game")}))
    def steam(self, arg):
        """steam: library | bigpicture | friends | downloads | screenshots | store | main | settings
        | {run: <appid>}"""
        url = f"steam://rungameid/{int(arg['run'])}" if isinstance(arg, dict) else OPEN_PAGES[arg]
        # `steam <url>` for a native install; otherwise let the desktop's steam:// handler
        # (Flatpak Steam registers one) take it. Override with steam: {command: "..."}.
        cmd = self.config.get("command") or ("steam" if shutil.which("steam") else "xdg-open")
        spawn(f"{cmd} {shlex.quote(url)}")


def setup(bot):
    bot.add_module(Steam(bot))
