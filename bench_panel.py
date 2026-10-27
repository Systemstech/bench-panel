#!/usr/bin/env python3
"""Run BenchPanel from this folder -- no install needed ("portable mode").

    python3 bench_panel.py            # opens http://127.0.0.1:8765

Run this way, the bench (site.json) and installed plugins (plugins/) live NEXT TO
THIS FILE, as they always have -- which is how the example service in deploy/ runs it. The
installed `benchpanel` command keeps them in your per-user data folder instead.
Everything else is identical: see benchcore/server.py.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from benchcore.server import main  # noqa: E402

if __name__ == "__main__":
    main(portable_dir=HERE)
