#!/usr/bin/env python3
"""Check plugin folders against the plugin standard (PLUGIN_STANDARD.md).

    python3 tools/check_plugin.py catalog/34401a
    python3 tools/check_plugin.py catalog/*          # what deploy.sh runs
    python3 tools/check_plugin.py --static myplugin  # files only, runs no plugin code

Exit status 0 if every plugin meets every MUST rule, 1 otherwise. SHOULD rules are
reported as "warn" and never fail the check.

This is the same code the bench panel runs when it installs or loads a plugin, so
a plugin that passes here is one the program will accept.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from benchcore.conformance import check_plugin  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("folders", nargs="+")
    ap.add_argument("--static", action="store_true", help="read files only; run no plugin code")
    ap.add_argument("-q", "--quiet", action="store_true", help="one line per plugin")
    a = ap.parse_args()
    failed = 0
    for f in a.folders:
        if not os.path.isdir(f):
            continue
        rep = check_plugin(f, dynamic=not a.static)
        warns = sum(1 for i in rep.items if i[1] == "SHOULD" and not i[2])
        print(f"{'PASS' if rep.ok else 'FAIL'}  {f}  ({len(rep.items)} checks, "
              f"{len(rep.must_failures)} MUST failed, {warns} warnings)")
        if not a.quiet:
            print(rep.text())
        failed += not rep.ok
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
