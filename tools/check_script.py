#!/usr/bin/env python3
"""Check automation scripts the way the Scripts window does before importing one.

    python3 tools/check_script.py examples/sweep_8648a_53181a.py
    python3 tools/check_script.py -q examples/*.py          # what deploy.sh runs

Reads each file's BENCHPANEL_SCRIPT header WITHOUT running the script (see
SCRIPTS.md). Exit status 0 if every file passes, 1 otherwise. The "look at this"
notes (direct pyvisa, subprocess, ...) are printed but never fail the check.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from benchcore.scripts import ScriptError, _decode, check_source  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("files", nargs="+")
    ap.add_argument("-q", "--quiet", action="store_true", help="one line per script")
    a = ap.parse_args()
    failed = 0
    for f in a.files:
        try:
            with open(f, "rb") as fh:
                rep = check_source(_decode(fh.read()))
        except (OSError, ScriptError) as e:
            rep = {"ok": False, "header": None, "problems": [str(e)], "watch": []}
        h = rep["header"] or {}
        print(f"{'PASS' if rep['ok'] else 'FAIL'}  {f}  "
              f"({h.get('name', '?')} {h.get('version', '')}, {len(h.get('args', []))} settings, "
              f"outputs {'yes' if h.get('outputs') else 'no'})")
        if not a.quiet or not rep["ok"]:
            for p in rep["problems"]:
                print(f"   FAIL  {p}")
            for w in rep["watch"]:
                print(f"   look  {w}")
        failed += not rep["ok"]
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
