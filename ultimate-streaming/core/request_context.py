"""Who the request being handled comes from, for code far from the HTTP handler.

The remote listener (core/webapi/server.py) handles each request from another device inside
remote(device), with the approved device's dict (core.remote_access.Devices.check). Anything
that request calls can ask remote_device(): the device, or None for the panel on this computer,
the deck itself, and every other caller. Writers of config.yaml use it to refuse changes to the
`editor` section from another device (configio.save, backups.restore, setup_backup.restore).

It is a context variable, so each handler thread sees only its own request, and it is always
reset when the request ends. A thread the request starts does not inherit it.
"""
import contextlib
import contextvars

REMOTE_SETTINGS = "Remote access settings can only be changed on the streaming computer"

_device = contextvars.ContextVar("remote_device", default=None)


class RemoteRefused(Exception):
    """Something only the streaming computer may do was asked for from another device.
    The handler answers it with `status` (403) and the message."""

    status = 403

    def __init__(self, message=REMOTE_SETTINGS):
        super().__init__(message)


def remote_device():
    """The approved device making the current request on the remote listener, or None."""
    return _device.get()


@contextlib.contextmanager
def remote(device):
    """Run the body as a request from `device` (a device dict); always reset afterwards."""
    if not isinstance(device, dict):
        raise TypeError("remote() needs the device's dict")
    token = _device.set(device)
    try:
        yield device
    finally:
        _device.reset(token)


def refuse_remote(message=REMOTE_SETTINGS):
    """Raise RemoteRefused(message) if the current request comes from another device."""
    if _device.get() is not None:
        raise RemoteRefused(message)
