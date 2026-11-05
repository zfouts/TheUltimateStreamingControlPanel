"""Home Assistant service calls and entity states.

config.yaml:
    home_assistant:
      url: http://homeassistant.local:8123         # or env HA_URL
      token_file: ~/.config/ultimate-streaming/ha_token   # long-lived access token, chmod 600 (or env HA_TOKEN)

Leave the section out (or empty) if you don't use Home Assistant; the `ha` keys then
report "not configured" when pressed.
"""
import os
import re

import requests

from core import Module, action, source, state
from core.colors import parse_color
from core.schema import UI, Bool, Data, Number, Source, Text
from core.util import read_secret

LIGHT_ID = re.compile(r"light\.[a-z0-9_]+\Z")
# (connect, read) seconds for the polled `state: {ha: ...}`: it runs on the deck's main loop.
STATE_TIMEOUT = (1, 2)


class HomeAssistant(Module):
    def on_config(self, config):
        super().on_config(config)
        self.url = (os.environ.get("HA_URL") or config.get("url") or "").rstrip("/")
        if config.get("token"):
            self.log.warning("home_assistant.token in config.yaml is ignored; use token_file or HA_TOKEN")
        token = read_secret("HA_TOKEN", config.get("token_file"))
        self.headers = {"Authorization": f"Bearer {token}"} if token else None

    def _check(self):
        if not (self.url and self.headers):
            raise RuntimeError("home_assistant url/token not configured")

    @action("ha", ui=UI("Home Assistant", "Home Assistant", args={
        "service": Text("Service", placeholder="light.toggle", help="e.g. light.toggle, switch.turn_on, scene.turn_on"),
        "entity_id": Source("ha_entities", "Entity"), "data": Data("Extra data")}))
    def call(self, arg):
        """ha: {service: light.toggle, entity_id: light.office, data: {...}}"""
        self._check()
        domain, svc = arg["service"].split(".", 1)
        body = dict(arg.get("data") or {})
        if arg.get("entity_id"):
            body["entity_id"] = arg["entity_id"]
        r = requests.post(f"{self.url}/api/services/{domain}/{svc}", json=body,
                          headers=self.headers, timeout=5)
        r.raise_for_status()

    @action("ha_light", ui=UI("Set a light's color", "Home Assistant", args={
        "entity_id": Source("ha_entities", "Light"), "color": Text("Color"),
        "brightness": Number("Brightness", min=0, max=255), "off": Bool("Turn off instead")}))
    def light(self, arg):
        """ha_light: {entity_id: light.desk, color: purple, brightness: 200}  or  {entity_id: light.desk, off: true}"""
        entity_id = arg.get("entity_id")
        if not isinstance(entity_id, str) or not LIGHT_ID.match(entity_id):
            raise ValueError(f"not a light entity: {entity_id!r}")
        if arg.get("off"):
            service, body = "turn_off", {"entity_id": entity_id}
        else:
            body = {"entity_id": entity_id, "rgb_color": list(parse_color(arg.get("color")))}
            brightness = arg.get("brightness")
            if brightness not in (None, ""):
                if isinstance(brightness, bool) or not isinstance(brightness, (int, float, str)):
                    raise ValueError("brightness must be a number from 0 to 255")
                try:
                    b = int(float(brightness))
                except ValueError:
                    raise ValueError("brightness must be a number from 0 to 255") from None
                if not 0 <= b <= 255 or b != float(brightness):
                    raise ValueError("brightness must be a whole number from 0 to 255")
                body["brightness"] = b
            service = "turn_on"
        self._check()
        r = requests.post(f"{self.url}/api/services/light/{service}", json=body,
                          headers=self.headers, timeout=5)
        r.raise_for_status()

    @state("ha", ui=UI("Home Assistant entity is…", "Home Assistant", arg=Source("ha_entities", "Entity"),
                       extra={"equals": Text("State", placeholder="on")}))
    def entity_state(self, entity_id, spec):
        """state: {ha: light.office, equals: "on"}"""
        self._check()
        r = requests.get(f"{self.url}/api/states/{entity_id}", headers=self.headers, timeout=STATE_TIMEOUT)
        r.raise_for_status()
        return r.json()["state"] == spec.get("equals", "on")

    @source("ha_entities")
    def entity_choices(self):
        self._check()
        r = requests.get(f"{self.url}/api/states", headers=self.headers, timeout=5)
        r.raise_for_status()
        return sorted(e["entity_id"] for e in r.json())


def setup(bot):
    bot.add_module(HomeAssistant(bot))
