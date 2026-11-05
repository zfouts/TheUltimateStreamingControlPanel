#!/usr/bin/env python3
"""Install, remove and list plugins (see core/plugins.py for what a plugin is).

    python3 tools/plugin.py list                      installed plugins, and the catalog's others
    python3 tools/plugin.py install bambu             from the app's plugins/ catalog (linked)
    python3 tools/plugin.py install ~/my-plugin       from a folder (copied, after asking)
    python3 tools/plugin.py remove bambu
    python3 tools/plugin.py deps                      rebuild the plugins' shared dependencies

The running app notices installs and removals on its own within a few seconds.
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "vendor"))
sys.path.insert(0, ROOT)

from core import plugin_install  # noqa: E402
from core.paths import plugins_dir  # noqa: E402
from core.plugins import ManifestError  # noqa: E402


def cmd_list(_args):
    rows = plugin_install.listing()
    if not rows:
        print("No plugins installed, and none in the catalog.")
        return
    width = max(len(row["name"]) for row in rows)
    for row in rows:
        line = f"{row['name']:<{width}}  {row['version'] or '?':<10}  {row['state']}"
        print(line + (f"  ({row['error']})" if row["error"] else ""))


def cmd_install(args):
    manifest = plugin_install.install(args.source, yes=args.yes)
    folder = os.path.join(plugins_dir(), manifest["name"])
    print(f"Installed {manifest['name']} {manifest['version']}. The running app picks it up within a "
          f"few seconds. How to configure it: {os.path.join(folder, 'README.md')}")


def cmd_remove(args):
    plugin_install.remove(args.name)


def cmd_deps(_args):
    wheels = plugin_install.rebuild_deps()
    print("Plugin dependencies rebuilt." if wheels else "No plugin needs any dependencies.")


def main():
    ap = argparse.ArgumentParser(description="Install, remove and list plugins.")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="installed plugins, and the catalog's others").set_defaults(func=cmd_list)
    p = sub.add_parser("install", help="install from the catalog (by name) or from a folder")
    p.add_argument("source", metavar="name-or-path")
    p.add_argument("--yes", action="store_true", help="don't ask before copying a plugin from a folder")
    p.set_defaults(func=cmd_install)
    p = sub.add_parser("remove", help="remove an installed plugin")
    p.add_argument("name")
    p.set_defaults(func=cmd_remove)
    sub.add_parser("deps", help="rebuild the plugins' shared dependencies").set_defaults(func=cmd_deps)
    args = ap.parse_args()
    try:
        args.func(args)
    except (ManifestError, plugin_install.DepsError, plugin_install.InstallError) as e:
        sys.exit(f"error: {e}")
    except OSError as e:
        sys.exit(f"error: {e.strerror or e}" + (f": {e.filename}" if e.filename else ""))


if __name__ == "__main__":
    main()
