#!/usr/bin/env python3
"""Power supply check: step an E3641A through voltages, measure each on a 34401A -> CSV.

    # plan only -- prints what it would do, switches nothing on:
    python3 examples/psu_dmm_check.py --volts 1,2,5,10
    # really do it:
    python3 examples/psu_dmm_check.py --volts 1,2,5,10 --output-on

WIRING: E3641A output + -> 34401A HI (V/ohm), E3641A output - -> 34401A LO.
Nothing else on the output: the meter's 10 Mohm input is the only load.

WHAT IT DOES, IN ORDER
  1. Powers both instruments ON in the panel (only the ones that were OFF).
  2. Refuses to go on if the supply's output is already ON -- someone is using it.
  3. Records the supply's range, voltage, current limit and over-voltage protection,
     and the meter's function, range and NPLC.
  4. Output off. Supply to the LOW range, 0 V, your current limit, and OVP to 2 V
     above the highest step (switched on). Meter to DC volts, auto range, 10 NPLC.
     Reads both back, and stops with the output still off if anything didn't take.
  5. Output ON at 0 V. Then at each voltage: set it, wait, average the meter, and
     record the supply's own readback too. One CSV row per step.
  6. ALWAYS, even on an error, a Stop or Ctrl-C: output OFF, then everything put back
     as it was found, and a 'restored:' line read back from both instruments.

The CSV has the error of the supply's setting against the meter, and of the supply's
own readback against the meter. It makes no pass/fail call: judge the numbers
against the E3641A's and 34401A's specifications in their manuals.

SCRIPT LIMITS (its own, conservative): 0-20 V, current limit 1-500 mA.
"""
BENCHPANEL_SCRIPT = {
    "schema": 1,
    "name": "E3641A -> 34401A supply check",
    "version": "1.1.0",
    "description": "Steps the E3641A through a list of voltages and measures each on the "
                   "34401A: setting vs meter vs the supply's own readback, to a CSV. Wiring: "
                   "supply + to meter HI, - to LO. Plan-only unless 'Switch the output on' is ticked.",
    "uses": ["e3641a", "34401a"],
    "outputs": True,
    "wiring": [["e3641a:out", "34401a:in"]],
    "args": [
        {"name": "volts", "type": "str", "default": "1,2,5,10", "label": "Voltages, V",
         "help": "comma-separated, 0 to 20 V (this script's limit)"},
        {"name": "current_limit", "type": "float", "default": 0.02, "min": 0.001, "max": 0.5,
         "label": "Current limit, A", "help": "the meter draws ~1 uA; keep this small"},
        {"name": "settle", "type": "float", "default": 1.0, "min": 0.2, "max": 60,
         "label": "Settle time, s"},
        {"name": "readings", "type": "int", "default": 5, "min": 1, "max": 100,
         "label": "Meter readings per step"},
        {"name": "psu", "type": "str", "default": "e3641a-1", "label": "Supply's id"},
        {"name": "dmm", "type": "str", "default": "34401a-1", "label": "Meter's id"},
        {"name": "output_on", "type": "bool", "default": False, "label": "Switch the output on",
         "help": "unticked = plan only, nothing switched on"},
    ],
}

import argparse
import csv
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "client"))
from benchpanel import Bench, BenchError  # noqa: E402

VMAX, IMIN, IMAX = 20.0, 0.001, 0.5


def meter(bench, dmm, n, per=6.0):
    """Average n fresh meter readings, throwing away the one in flight when called."""
    got = lambda k: (lambda s: (s.get("samples") or 0) >= k)  # noqa: E731
    bench.act(dmm, "reset_stats")
    bench.wait_for(dmm, got(1), timeout=per)
    bench.act(dmm, "reset_stats")
    return bench.wait_for(dmm, got(n), timeout=per * n)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", help="the panel (default: the one that started this script, else local)")
    ap.add_argument("--login", help="login file for the HTTPS portal (user:password, chmod 600)")
    ap.add_argument("--volts", default="1,2,5,10")
    ap.add_argument("--current-limit", type=float, default=0.02)
    ap.add_argument("--settle", type=float, default=1.0)
    ap.add_argument("--readings", type=int, default=5)
    ap.add_argument("--psu", default="e3641a-1")
    ap.add_argument("--dmm", default="34401a-1")
    ap.add_argument("--out", help="CSV path (default ./psu_dmm_check_<time>.csv)")
    ap.add_argument("--output-on", action="store_true", help="actually switch the supply output on")
    a = ap.parse_args()

    try:
        steps = [float(x) for x in a.volts.split(",") if x.strip()]
    except ValueError:
        sys.exit(f"--volts: {a.volts!r} is not a comma-separated list of numbers")
    if not steps:
        sys.exit("--volts: no voltages given")
    bad = [v for v in steps if not 0 <= v <= VMAX]
    if bad:
        sys.exit(f"{bad} V outside this script's 0-{VMAX:g} V")
    if not IMIN <= a.current_limit <= IMAX:
        sys.exit(f"current limit {a.current_limit:g} A outside this script's {IMIN:g}-{IMAX:g} A")
    ovp = max(steps) + 2.0
    out = a.out or f"psu_dmm_check_{time.strftime('%Y%m%d_%H%M%S')}.csv"

    print(f"plan: {a.psu} -> {a.dmm} (DC volts), steps {', '.join(f'{v:g}' for v in steps)} V, "
          f"current limit {a.current_limit * 1000:g} mA, OVP {ovp:g} V, settle {a.settle:g} s, "
          f"{a.readings} readings/step")
    print(f"      CSV -> {out}")
    if not a.output_on:
        print("dry run: nothing switched on. Tick 'Switch the output on' (or add --output-on) to run it.")
        return

    bench = Bench(a.url, login_file=a.login)
    with bench.session(a.psu, a.dmm):
        p0, m0 = bench.read(a.psu), bench.read(a.dmm)
        if p0.get("output"):
            raise BenchError(f"{a.psu}'s output is already ON -- someone is using it. Nothing changed.")
        base = {"range": p0["range"], "v": p0["v_set"], "i": p0["i_set"], "ovp": p0["ovp"],
                "ovp_on": p0["ovp_state"], "func": m0["function"], "mrange": m0["range"], "nplc": m0["nplc"]}
        print(f"baseline: supply {base['range']} range, {base['v']:g} V, {base['i']:g} A, OVP {base['ovp']:g} V "
              f"{'on' if base['ovp_on'] else 'off'}; meter {base['func']} {base['mrange']} NPLC {base['nplc']:g}")
        rows = 0
        try:
            bench.act(a.psu, "output", value=False)
            bench.act(a.psu, "set_range", value="LOW")
            bench.act(a.psu, "set_v", value=0)
            bench.act(a.psu, "set_i", value=a.current_limit)
            bench.act(a.psu, "set_ovp", value=ovp)
            bench.act(a.psu, "ovp_state", value=True)
            bench.act(a.dmm, "function", value="DCV")
            bench.act(a.dmm, "range", value="AUTO")
            bench.act(a.dmm, "nplc", value=10)
            p, m = bench.read(a.psu), bench.read(a.dmm)
            problems = []
            if p["output"]:
                problems.append("supply output still ON")
            if p["range"] != "LOW" or abs(p["v_set"]) > 1e-3 or abs(p["i_set"] - a.current_limit) > 1e-4:
                problems.append(f"supply took {p['range']}, {p['v_set']} V, {p['i_set']} A")
            if abs(p["ovp"] - ovp) > 0.05 or not p["ovp_state"]:
                problems.append(f"OVP is {p['ovp']} V {'on' if p['ovp_state'] else 'OFF'}")
            if m["function"] != "DCV":
                problems.append(f"meter is on {m['function']}")
            if problems:
                raise BenchError("setup did not take, output left off: " + "; ".join(problems))
            print(f"pre-output check: supply LOW, 0 V, {p['i_set'] * 1000:g} mA, OVP {p['ovp']:g} V on, "
                  f"output off; meter DCV")

            bench.act(a.psu, "output", value=True, confirmed=True)
            with open(out, "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["timestamp", "set_v", "meter_v", "meter_min_v", "meter_max_v", "n",
                            "psu_readback_v", "psu_readback_a", "set_minus_meter_mv", "readback_minus_meter_mv",
                            "flag"])
                for v in steps:
                    bench.act(a.psu, "set_v", value=v)
                    time.sleep(a.settle)
                    s = meter(bench, a.dmm, a.readings)
                    ps = bench.read(a.psu)
                    flag = []
                    if ps.get("cc"):
                        flag.append("SUPPLY IN CURRENT LIMIT -- something is loading the output")
                    if ps.get("ovp_tripped"):
                        flag.append("OVP TRIPPED")
                    d_set = (v - s["avg"]) * 1000
                    d_rb = (ps["v_meas"] - s["avg"]) * 1000
                    w.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), v, f"{s['avg']:.7f}", f"{s['min']:.7f}",
                                f"{s['max']:.7f}", s["samples"], f"{ps['v_meas']:.5f}", f"{ps['i_meas']:.5f}",
                                f"{d_set:+.3f}", f"{d_rb:+.3f}", "; ".join(flag)])
                    fh.flush()
                    rows += 1
                    print(f"  set {v:7.3f} V  meter {s['avg']:10.6f} V  supply says {ps['v_meas']:8.4f} V  "
                          f"set-meter {d_set:+8.3f} mV  readback-meter {d_rb:+8.3f} mV"
                          + ("   <-- " + "; ".join(flag) if flag else ""))
                    if ps.get("ovp_tripped"):
                        raise BenchError("over-voltage protection tripped -- stopping")
        finally:
            # Whatever happened: output OFF first, then put everything back.
            try:
                bench.act(a.psu, "output", check=False, value=False)
                bench.act(a.psu, "set_range", check=False, value=base["range"])
                bench.act(a.psu, "set_v", check=False, value=base["v"])
                bench.act(a.psu, "set_i", check=False, value=base["i"])
                bench.act(a.psu, "set_ovp", check=False, value=base["ovp"])
                bench.act(a.psu, "ovp_state", check=False, value=base["ovp_on"])
                bench.act(a.dmm, "function", check=False, value=base["func"])
                bench.act(a.dmm, "range", check=False, value=base["mrange"])
                bench.act(a.dmm, "nplc", check=False, value=base["nplc"])
                p, m = bench.read(a.psu), bench.read(a.dmm)
                print(f"restored: supply output {'ON!' if p['output'] else 'off'}, {p['range']} range, "
                      f"{p['v_set']:g} V, {p['i_set']:g} A, OVP {p['ovp']:g} V {'on' if p['ovp_state'] else 'off'}; "
                      f"meter {m['function']} {m['range']} NPLC {m['nplc']:g}")
            except BenchError as e:
                print(f"!! restoring failed: {e} -- CHECK THE E3641A OUTPUT", file=sys.stderr)
    print(f"{rows} steps written to {out}")


if __name__ == "__main__":
    try:
        main()
    except BenchError as e:
        sys.exit(f"stopped: {e}")
    except KeyboardInterrupt:
        sys.exit("interrupted -- look for the 'restored:' line above; if it is missing, "
                 "check the E3641A output by hand")
