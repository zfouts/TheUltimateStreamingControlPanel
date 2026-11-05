#!/usr/bin/env python3
"""Install requirements.txt into ./vendor without pip (standard library only).

Each wheel is downloaded from PyPI over HTTPS and must match the sha256 pinned in
requirements.txt, or nothing is installed. Wheels are unpacked into a fresh
directory that replaces vendor/ only once everything succeeded.

    python3 tools/fetch_deps.py [--requirements requirements.txt] [--target vendor]

Also used as a library (core/plugin_install.py, for the plugins' shared dependency folder):
parse(), fetch_wheels() and install() raise DepsError instead of exiting.
"""
import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LINE = re.compile(r"^(?P<name>[A-Za-z0-9_.-]+)==(?P<version>[^\s;]+)\s*(?:;\s*(?P<marker>[^-]+?))?\s*(?P<hashes>(--hash=sha256:[0-9a-f]{64}\s*)+)$")
MODULE_SUFFIXES = (".py", ".pyc", ".so", ".pyd")


class DepsError(Exception):
    """Dependencies couldn't be installed; the message says why. The target was left as it was."""


def normalize(name):
    """PEP 503 project name: paho_mqtt, Paho.MQTT and paho-mqtt are the same project."""
    return re.sub(r"[-_.]+", "-", name).lower()


def marker_ok(marker):
    """Supports the only marker form we use: python_version <op> "X.Y"."""
    if not marker:
        return True
    m = re.fullmatch(r'python_version\s*(<|<=|>|>=|==|!=)\s*["\'](\d+)\.(\d+)["\']', marker.strip())
    if not m:
        raise DepsError(f"unsupported marker: {marker}")
    op, want = m.group(1), (int(m.group(2)), int(m.group(3)))
    have = sys.version_info[:2]
    return {"<": have < want, "<=": have <= want, ">": have > want, ">=": have >= want,
            "==": have == want, "!=": have != want}[op]


def parse(path):
    """[(name, version, {sha256, ...})] for the requirements in path that apply to this Python."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except (OSError, UnicodeDecodeError) as e:
        raise DepsError(f"can't read {path}: {getattr(e, 'strerror', None) or e}") from None
    reqs = []
    for raw in lines:
        line = raw.split("#", 1)[0].strip() if not raw.lstrip().startswith("--") else raw.strip()
        if not line:
            continue
        m = LINE.match(line)
        if not m:
            raise DepsError(f"{path}: can't parse requirement (must be name==version --hash=sha256:...): {line}")
        hashes = set(re.findall(r"sha256:([0-9a-f]{64})", m.group("hashes")))
        if marker_ok(m.group("marker")):
            reqs.append((m.group("name"), m.group("version"), hashes))
    return reqs


def fetch(url):
    if not url.startswith("https://"):
        raise DepsError(f"refusing non-HTTPS URL {url}")
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            if not r.geturl().startswith("https://"):
                raise DepsError(f"refusing a redirect from {url} to a non-HTTPS URL")
            return r.read()
    except OSError as e:   # URLError, HTTPError, timeouts
        raise DepsError(f"can't download {url}: {getattr(e, 'reason', None) or e}") from None


def safe_extract(data, target):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for member in z.namelist():
                dest = os.path.realpath(os.path.join(target, member))
                if not dest.startswith(os.path.realpath(target) + os.sep):
                    raise DepsError(f"wheel contains an unsafe path: {member}")
            z.extractall(target)
    except zipfile.BadZipFile as e:
        raise DepsError(f"not a valid wheel: {e}") from None


def top_level_names(data):
    """Top-level importable names a wheel provides: its *.dist-info/top_level.txt plus the roots of
    everything it actually unpacks (package folders and single modules; not *.dist-info / *.data).
    Both, since top_level.txt is only what the wheel says about itself."""
    names = set()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            members = z.namelist()
            for member in members:
                if re.fullmatch(r"[^/]+\.dist-info/top_level\.txt", member):
                    text = z.read(member).decode("utf-8", "replace")
                    names.update(line.strip().split("/")[0] for line in text.splitlines())
    except zipfile.BadZipFile as e:
        raise DepsError(f"not a valid wheel: {e}") from None
    for member in members:
        root, sep, _ = member.partition("/")
        if sep:
            if not root.endswith((".dist-info", ".data")) and root != "__pycache__":
                names.add(root)
        elif root.endswith(MODULE_SUFFIXES):
            names.add(root.split(".", 1)[0])
    names.discard("")
    return names


def fetch_wheels(reqs, staging, log=None):
    """Download each (name, version, hashes) from PyPI, verify its sha256 and unpack it into staging.
    Returns [{"name", "version", "top_level": set of top-level importable names}]."""
    wheels = []
    for name, version, hashes in reqs:
        try:
            meta = json.loads(fetch(f"https://pypi.org/pypi/{name}/{version}/json"))
            files = [f for f in meta["urls"] if f["packagetype"] == "bdist_wheel" and f["digests"]["sha256"] in hashes]
            url = files[0]["url"] if files else None
        except (ValueError, LookupError, TypeError):
            raise DepsError(f"{name}=={version}: PyPI returned metadata this tool can't read") from None
        if not files:
            raise DepsError(f"{name}=={version}: no wheel on PyPI matches the pinned hash")
        data = fetch(url)
        digest = hashlib.sha256(data).hexdigest()
        if digest not in hashes:
            raise DepsError(f"{name}=={version}: downloaded file hash {digest} doesn't match requirements.txt")
        top_level = top_level_names(data)
        safe_extract(data, staging)
        wheels.append({"name": name, "version": version, "top_level": top_level})
        if log:
            log(f"  {name} {version}  ok (sha256 verified)")
    return wheels


def merge(requirement_files):
    """The requirements of several files as one list. The same name==version in several files is
    fetched once (accepting only hashes every file pins); one name at two versions is a DepsError."""
    merged = {}   # normalized name -> [name, version, hashes, path]
    for path in requirement_files:
        for name, version, hashes in parse(path):
            seen = merged.get(normalize(name))
            if seen is None:
                merged[normalize(name)] = [name, version, set(hashes), path]
                continue
            if seen[1] != version:
                raise DepsError(f"{name} is pinned to two versions: {seen[1]} ({seen[3]}) and {version} ({path})")
            seen[2] &= hashes
            if not seen[2]:
                raise DepsError(f"{name}=={version} is pinned with different hashes in {seen[3]} and {path}")
    return [(name, version, hashes) for name, version, hashes, _ in merged.values()]


def install(requirement_files, target, check=None, log=None):
    """Install the requirements of every file into target. Wheels are unpacked into target + ".new",
    which replaces target only once all of them were fetched, verified and accepted by
    check(wheels) (optional: raise DepsError to refuse). On any failure target is left as it was.
    Returns the fetch_wheels() list."""
    reqs = merge(requirement_files)
    staging, old = target + ".new", target + ".old"
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)
    try:
        wheels = fetch_wheels(reqs, staging, log=log)
        if check:
            check(wheels)
        shutil.rmtree(old, ignore_errors=True)
        if os.path.lexists(target):
            os.rename(target, old)     # two renames: target is missing only in between
        os.replace(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        if os.path.lexists(old) and not os.path.lexists(target):
            os.rename(old, target)
        raise
    shutil.rmtree(old, ignore_errors=True)
    return wheels


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--requirements", default=os.path.join(ROOT, "requirements.txt"))
    ap.add_argument("--target", default=os.path.join(ROOT, "vendor"))
    args = ap.parse_args()
    try:
        install([args.requirements], args.target, log=print)
    except DepsError as e:
        sys.exit(str(e))
    print(f"installed into {args.target}")


if __name__ == "__main__":
    main()
