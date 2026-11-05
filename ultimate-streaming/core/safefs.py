"""Careful file access for reading other programs' settings (detect, external backups, importers).

Files outside our own folders can be anything: a named pipe that blocks forever, a symlink to
~/.ssh, a file swapped between check and open, or something huge. These helpers only ever hand
back regular files, read once, with a size cap.
"""
import os
import secrets
import stat

from .paths import _xdg

APP = "ultimate-streaming"


class NotRegular(OSError):
    """The path is not a regular file (directory, pipe, device, socket or a symlink we refuse)."""


class TooBig(OSError):
    """The file is larger than the caller's limit."""


def read_regular(path, max_bytes, *, follow=False):
    """(bytes, stat) of a regular file of at most max_bytes.

    follow=False refuses a symlink at the last path component (O_NOFOLLOW). Pipes and devices
    are refused before opening, and the open file is checked again in case it was swapped."""
    st = os.stat(path, follow_symlinks=follow)
    if not stat.S_ISREG(st.st_mode):
        raise NotRegular(path)
    if st.st_size > max_bytes:
        raise TooBig(path)
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | (0 if follow else os.O_NOFOLLOW)
    try:
        fd = os.open(path, flags)
    except OSError as e:
        if e.errno == 40:   # ELOOP: a symlink appeared at the last component
            raise NotRegular(path) from None
        raise
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise NotRegular(path)
        data = f.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise TooBig(path)
    return data, st


def write_atomic(path, data, mode=0o600, *, follow_symlink=False):
    """Replace `path` with `data` (bytes) so a reader or a crash never sees half a file.

    A unique temp file in the same folder is created with O_EXCL|O_NOFOLLOW (never through a
    symlink or over an existing file), written, fsynced and renamed over `path`; a symlink at
    `path` is replaced, not followed. With follow_symlink=True a symlink at `path` that the
    user owns (a dotfiles setup) is resolved and its target is written instead, so the link
    stays; a symlink owned by someone else is still replaced. The temp file is removed if
    anything fails."""
    try:
        st = os.lstat(path)
        if follow_symlink and stat.S_ISLNK(st.st_mode) and st.st_uid == os.getuid():
            path = os.path.realpath(path)
    except OSError:
        pass
    folder, name = os.path.split(path)
    tmp = os.path.join(folder, f".{name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def inside(path, root):
    """True if realpath(path) is root or below it (root must already be a realpath)."""
    real = os.path.realpath(path)
    return real == root or real.startswith(root.rstrip(os.sep) + os.sep)


def walk_regular(root, *, max_file, max_files=20000, notes=None):
    """Yield (rel_path, bytes, stat) for every regular file under root.

    root is resolved once. Directories are walked without following symlinks; a symlinked file
    is read only when its target resolves inside root, otherwise skipped (with a note). Files
    over max_file, pipes, devices and sockets are skipped with a note. notes: a list to append
    "<rel>: <reason>" strings to."""
    root = os.path.realpath(root)
    count = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            is_link = os.path.islink(full)
            if is_link and not inside(full, root):
                if notes is not None:
                    notes.append(f"{rel}: symlink outside the folder, skipped")
                continue
            try:
                data, st = read_regular(full, max_file, follow=is_link)
            except TooBig:
                if notes is not None:
                    notes.append(f"{rel}: larger than {max_file // (1024 * 1024)} MB, skipped")
                continue
            except (NotRegular, OSError):
                if notes is not None:
                    notes.append(f"{rel}: not a regular file, skipped")
                continue
            count += 1
            if count > max_files:
                if notes is not None:
                    notes.append(f"more than {max_files} files, the rest skipped")
                return
            yield rel, data, st


def state_home():
    """$XDG_STATE_HOME/ultimate-streaming (default ~/.local/state/ultimate-streaming), created 0700.

    For data that must stay out of ~/.config (often synced as dotfiles), e.g. archives that
    contain stream keys."""
    path = os.path.join(_xdg("XDG_STATE_HOME", os.path.join("~", ".local", "state")), APP)
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path
