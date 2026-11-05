"""Let python-elgato-streamdeck also use the hidraw build of hidapi.

The library looks for libhidapi-libusb; some distros (e.g. Debian/Ubuntu's
libhidapi-hidraw0) only ship the hidraw build, which has the same API and works
with the per-user hidraw permissions udev/logind grant (see packaging/70-streamdeck.rules).
Import this before anything from StreamDeck.DeviceManager.
"""
from StreamDeck.Transport.LibUSBHIDAPI import LibUSBHIDAPI

_load_hidapi = LibUSBHIDAPI.Library._load_hidapi_library
LibUSBHIDAPI.Library._load_hidapi_library = lambda self, names: _load_hidapi(
    self, list(names) + ["libhidapi-hidraw.so.0", "libhidapi-hidraw.so"]
)
