#!/usr/bin/env python3
"""Bench check: is every instrument on the bench answering? Switches no output on.

    python3 examples/bench_check.py                     # every instrument on the bench
    python3 examples/bench_check.py --only 34401a-1,e3641a-1

For each instrument, in card order:
  1. If it is OFF in the panel: power it on (this connects and identifies it; the
     panel refuses the wrong model) and time how long that took.
  2. Wait for one fresh poll, and record what it identified as.
  3. Read and CLEAR its error queue, so old errors don't confuse the next job.
  4. Power it off again: outputs confirmed off, front panel handed back.
An instrument that was already ON is someone else's: it is only read, its error
queue is left alone, and it stays ON.

Writes bench_check_<time>.csv. Nothing is wired and nothing is switched on, so it is
safe to run any time the instruments are powered at the bench.
"""
BENCHPANEL_SCRIPT = {
    "schema": 1,
    "name": "Bench check",
    "version": "1.0.0",
    "description": "Powers each instrument on in the panel, records what it identifies as and "
                   "its error queue, and powers it off again. No wiring, no outputs. A quick "
                   "'is everything answering?' after setting up or moving the bench.",
    "uses": [],
    "outputs": False,
    "args": [
        {"name": "only", "type": "str", "default": "", "label": "Only these ids",
         "help": "comma-separated instrument ids; blank = every instrument on the bench"},
        {"name": "timeout", "type": "float", "default": 15, "min": 2, "max": 120,
         "label": "Wait per instrument, s"},
    ],
}

import argparse
import csv
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "client"))
from benchpanel import Bench, BenchError  # noqa: E402


def check_one(bench, c, timeout):
    """-> dict for one CSV row. Never raises for an instrument problem: that's a result."""
    iid = c["id"]
    row = {"id": iid, "name": c.get("name"), "plugin": c.get("plugin"), "where": c.get("where"),
           "was_on": bool(c.get("enabled")), "on_s": "", "idn": "", "errors": "", "released": "", "verdict": ""}
    if c.get("missing"):
        row["verdict"] = f"NOT LOADED: {c['missing']}"
        return row
    if row["was_on"]:
        try:
            s = bench.read(iid, timeout=timeout)
            row.update(idn=s.get("idn") or "", errors="(left alone: it was already ON)",
                       verdict="OK (already ON, only read)")
        except BenchError as e:
            row["verdict"] = f"ON but not answering: {e}"
        return row

    t0 = time.time()
    r = bench.act(iid, "power_on", check=False)
    row["on_s"] = f"{time.time() - t0:.2f}"
    if not r.get("ok"):
        row["verdict"] = f"DID NOT COME ON: {r.get('msg') or 'refused'}"
        return row
    try:
        s = bench.read(iid, timeout=timeout)
        row["idn"] = s.get("idn") or ""
        e = bench.act(iid, "clear_errors", check=False)
        errs = e.get("errors") if e.get("ok") else None
        if errs is None:
            row["errors"] = f"could not read: {e.get('msg')}"
            row["verdict"] = "OK, but its error queue could not be read"
        elif errs:
            row["errors"] = " | ".join(str(x) for x in errs)
            row["verdict"] = "OK, but it had errors queued (now cleared)"
        else:
            row["errors"] = "none"
            row["verdict"] = "OK"
    except BenchError as e:
        row["verdict"] = f"came ON, then no fresh reading: {e}"
    finally:
        off = bench.power_off(iid)
        of = off.get("output_off")
        row["released"] = ("yes" if off.get("ok", True) else "?") + \
            ("" if of is None else ", output confirmed off" if of else ", OUTPUT OFF NOT CONFIRMED")
        if of is False:
            row["verdict"] += " -- CHECK ITS OUTPUT BY HAND"
    return row


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", help="the panel (default: the one that started this script, else local)")
    ap.add_argument("--login", help="login file for the HTTPS portal (user:password, chmod 600)")
    ap.add_argument("--only", default="", help="comma-separated instrument ids")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--out", help="CSV path (default ./bench_check_<time>.csv)")
    a = ap.parse_args()

    bench = Bench(a.url, login_file=a.login)
    cards = bench.state()["instruments"]
    want = [x.strip() for x in a.only.split(",") if x.strip()]
    if want:
        unknown = [w for w in want if w not in {c["id"] for c in cards}]
        if unknown:
            sys.exit(f"not on this bench: {', '.join(unknown)} (have: {', '.join(c['id'] for c in cards) or 'none'})")
        cards = [c for c in cards if c["id"] in want]
    if not cards:
        sys.exit("no instruments on this bench -- add them in Instruments first")

    out = a.out or f"bench_check_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    print(f"checking {len(cards)} instrument(s): {', '.join(c['id'] for c in cards)}\n")
    rows = []
    for c in cards:
        print(f"  {c['id']:14s} ...", end="", flush=True)
        row = check_one(bench, c, a.timeout)
        rows.append(row)
        print(f" {row['verdict']}" + (f"  [{row['on_s']} s to come on]" if row["on_s"] else ""))
        if row["idn"]:
            print(f"  {'':14s}     {row['idn']}")
        if row["errors"] and row["errors"] not in ("none",):
            print(f"  {'':14s}     errors: {row['errors']}")

    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    bad = [r for r in rows if not r["verdict"].startswith("OK")]
    print(f"\n{len(rows) - len(bad)} of {len(rows)} OK. Written to {out}")
    if bad:
        sys.exit(f"{len(bad)} instrument(s) need a look: {', '.join(r['id'] for r in bad)}")


if __name__ == "__main__":
    try:
        main()
    except BenchError as e:
        sys.exit(f"stopped: {e}")
    except KeyboardInterrupt:
        sys.exit("interrupted -- any instrument it had switched on in the panel was being switched off; "
                 "check the cards")
