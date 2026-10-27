#!/usr/bin/env python3
"""Frequency sweep: an 8648A signal generator measured by a 53181A counter -> CSV.

    # plan only -- prints what it would do, switches nothing on:
    python3 examples/sweep_8648a_53181a.py --channel 2 --start 100 --stop 1000 --step 100
    # really do it:
    python3 examples/sweep_8648a_53181a.py --channel 2 --start 100 --stop 1000 --step 100 --rf-on

Wire the 8648A RF OUT to the counter input you name with --channel.

WHAT IT DOES, IN ORDER
  1. Powers both instruments ON in the panel (only the ones that were OFF).
  2. Records their settings: generator frequency and level, counter function, gate,
     channel 1 impedance.
  3. RF off. Counter to FREQ <channel> with your gate (and 50 ohm on channel 1).
  4. Generator to the first frequency at your level, then RF ON.
  5. At each point: set the frequency, throw away the measurement that was in flight
     when it changed, then average --readings measurements. One CSV row per point.
  6. ALWAYS, even on an error or Ctrl-C: RF OFF, both instruments' settings restored,
     and the ones it powered on powered off again.

SAFETY LIMITS (this script's own, conservative -- from what has been proven on this
bench, not the instruments' absolute maxima): channel 1 at most 225 MHz and 0 dBm;
channel 2 100-2700 MHz and -27..+10 dBm. The generator itself stops at 1000 MHz.

The CSV opens directly in the bench_analysis results explorer.
"""
BENCHPANEL_SCRIPT = {
    "schema": 1,
    "name": "8648A -> 53181A frequency sweep",
    "version": "1.2.0",
    "description": "Steps an 8648A generator through a list of frequencies, averages the 53181A "
                   "counter at each, and writes a CSV. Without 'Switch RF on' it only prints the "
                   "plan. Always ends RF off with both instruments' settings put back.",
    "uses": ["8648a", "53181a"],
    "wiring": [["8648a:rf", "53181a:ch{channel}"]],
    "outputs": True,
    "args": [
        {"name": "channel", "type": "choice", "choices": ["1", "2"], "default": "2",
         "label": "Counter channel", "help": "which counter input the 8648A RF OUT is cabled to"},
        {"name": "start", "type": "float", "default": 100, "label": "Start, MHz"},
        {"name": "stop", "type": "float", "default": 1000, "label": "Stop, MHz"},
        {"name": "step", "type": "float", "default": 100, "label": "Step, MHz"},
        {"name": "level", "type": "float", "label": "Level, dBm",
         "help": "blank = -10 dBm on channel 1, -20 dBm on channel 2"},
        {"name": "gate", "type": "float", "default": 1, "min": 0.001, "max": 1000, "label": "Gate, s"},
        {"name": "readings", "type": "int", "default": 3, "min": 1, "max": 100,
         "label": "Readings per point"},
        {"name": "ch1_imp", "type": "choice", "choices": ["50", "1E6"], "default": "50",
         "label": "Channel 1 termination"},
        {"name": "gen", "type": "str", "default": "8648a-1", "label": "Generator's id"},
        {"name": "counter", "type": "str", "default": "53181a-1", "label": "Counter's id"},
        {"name": "rf_on", "type": "bool", "default": False, "label": "Switch RF on",
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

CH_LIMITS = {1: {"f": (0.1, 225.0), "dbm": (-40.0, 0.0), "level": -10.0},
             2: {"f": (100.0, 1000.0), "dbm": (-27.0, 10.0), "level": -20.0}}


def points(a):
    if a.points:
        return [float(x) for x in a.points.split(",")]
    out, f = [], a.start
    while f <= a.stop + 1e-9:
        out.append(round(f, 6))
        f += a.step
    return out


def measure(bench, ctr, n, gate_s):
    """Average n fresh counter readings. Raises BenchError (naming the status) on timeout."""
    per = gate_s * 2 + 3.0                          # generous: gate + fetch + poll cadence

    # Deliberately NOT failing the moment status says "no signal": that label is
    # sticky until a reading arrives, so it can be left over from before RF came on.
    # A real loss of signal shows up as a timeout whose message names the last status.
    def got(k):
        return lambda s: (s.get("samples") or 0) >= k

    # The measurement in flight when the frequency changed may straddle it: take one,
    # throw it away, then start counting.
    bench.act(ctr, "reset_stats")
    bench.wait_for(ctr, got(1), timeout=per)
    bench.act(ctr, "reset_stats")
    return bench.wait_for(ctr, got(n), timeout=per * n)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", help="the panel (default: the one that started this script, else "
                    "http://127.0.0.1:8765/ -- local or an SSH tunnel)")
    ap.add_argument("--login", help="login file (user:password, chmod 600) for the HTTPS portal")
    ap.add_argument("--gen", default="8648a-1", help="generator's id on the bench")
    ap.add_argument("--counter", default="53181a-1", help="counter's id on the bench")
    ap.add_argument("--channel", type=int, choices=(1, 2), default=2)
    ap.add_argument("--start", type=float, default=100.0, help="MHz")
    ap.add_argument("--stop", type=float, default=1000.0, help="MHz")
    ap.add_argument("--step", type=float, default=100.0, help="MHz")
    ap.add_argument("--points", help="comma-separated MHz list instead of start/stop/step")
    ap.add_argument("--level", type=float, help="dBm at the connector (default -10 Ch1, -20 Ch2)")
    ap.add_argument("--gate", type=float, default=1.0, help="counter gate, s")
    ap.add_argument("--readings", type=int, default=3, help="readings averaged per point")
    ap.add_argument("--out", help="CSV path (default ./sweep_8648a_53181a_<time>.csv)")
    ap.add_argument("--ch1-imp", choices=("50", "1E6"), default="50",
                    help="channel 1 termination (default 50). 1E6 leaves a 50-ohm source "
                         "unterminated: about twice the voltage at the input")
    ap.add_argument("--max-spread-ppm", type=float, default=0.01,
                    help="flag a point whose readings scatter more than this (default 0.01 ppm)")
    ap.add_argument("--rf-on", action="store_true", help="actually switch the RF output on")
    a = ap.parse_args()

    lim = CH_LIMITS[a.channel]
    level = lim["level"] if a.level is None else a.level
    pts = points(a)
    bad = [f for f in pts if not lim["f"][0] <= f <= lim["f"][1]]
    if bad:
        sys.exit(f"channel {a.channel}: {bad} MHz outside this script's {lim['f'][0]:g}-{lim['f'][1]:g} MHz")
    if not lim["dbm"][0] <= level <= lim["dbm"][1]:
        sys.exit(f"channel {a.channel}: {level:g} dBm outside this script's {lim['dbm'][0]:g}..{lim['dbm'][1]:g} dBm")
    # From the dashboard, the working directory IS the results folder.
    out = a.out or f"sweep_8648a_53181a_{time.strftime('%Y%m%d_%H%M%S')}.csv"

    print(f"plan: {a.gen} -> {a.counter} channel {a.channel}"
          + (f" ({'50 ohm' if a.ch1_imp == '50' else '1 Mohm'})" if a.channel == 1 else "")
          + f", {level:+g} dBm, gate {a.gate:g} s, "
          f"{a.readings} readings/point, {len(pts)} points: {', '.join(f'{p:g}' for p in pts)} MHz")
    print(f"      CSV -> {out}")
    if not a.rf_on:
        print("dry run: nothing switched on. Add --rf-on to run it.")
        return

    bench = Bench(a.url, login_file=a.login)
    with bench.session(a.gen, a.counter):
        g0, c0 = bench.read(a.gen), bench.read(a.counter)
        base = {"freq_mhz": g0["freq_hz"] / 1e6, "level": g0["ampl_abs"],
                "func": c0["function"], "gate": c0["gate_s"], "imp": c0["ch1"]["imp"]}
        print(f"baseline: generator {base['freq_mhz']:g} MHz {base['level']:+.2f} dBm; "
              f"counter {base['func']}, gate {base['gate']:g} s, Ch1 {base['imp']:g} ohm")
        rows = suspect = 0
        try:
            bench.act(a.gen, "output", value=False)
            bench.act(a.counter, "set_func", value=f"FREQ {a.channel}")
            bench.act(a.counter, "set_gate", value=a.gate)
            if a.channel == 1:
                bench.act(a.counter, "set_imp", value=a.ch1_imp)
            bench.act(a.gen, "set_freq", value=pts[0])
            bench.act(a.gen, "set_ampl", value=level)
            # Read BOTH instruments back before any RF: the counter must really be
            # terminated where we think, and the generator really at the level and
            # frequency we asked for. Anything else aborts with RF still off.
            g, c = bench.read(a.gen), bench.read(a.counter)
            if abs(g["ampl_abs"] - level) > 0.01 or g["output"] or abs(g["freq_hz"] - pts[0] * 1e6) > 1:
                raise BenchError(f"generator did not take the setup: {g['freq_hz']} Hz, {g['ampl_abs']} dBm, "
                                 f"RF {g['output']}")
            if c["function"] != f"FREQ {a.channel}" or (a.channel == 1 and c["ch1"]["imp"] != float(a.ch1_imp)):
                raise BenchError(f"counter did not take the setup: {c['function']}, Ch1 {c['ch1']['imp']} ohm")
            print(f"pre-RF check: generator {g['freq_hz'] / 1e6:g} MHz {g['ampl_abs']:+.2f} dBm RF off; "
                  f"counter {c['function']}" + (f", Ch1 {c['ch1']['imp']:g} ohm" if a.channel == 1 else ""))
            bench.act(a.gen, "output", value=True, confirmed=True)
            with open(out, "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["timestamp", "set_mhz", "level_dbm", "channel", "gate_s", "n",
                            "counter_hz", "min_hz", "max_hz", "error_hz", "error_ppm", "spread_ppm", "flag"])
                for f in pts:
                    bench.act(a.gen, "set_freq", value=f)
                    s = measure(bench, a.counter, a.readings, a.gate)
                    set_hz = f * 1e6
                    err = s["avg"] - set_hz
                    # A counter on a marginal signal still returns numbers -- wrong ones,
                    # with no error (seen 2026-09-23: 550 Hz "measured" for 130 MHz). The
                    # tell is scatter between readings: healthy counts agree to ~1e-4 ppm.
                    spread = (s["max"] - s["min"]) / set_hz * 1e6
                    flag = "SUSPECT: readings scatter" if spread > a.max_spread_ppm else ""
                    suspect += bool(flag)
                    w.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), f, level, a.channel, a.gate, s["samples"],
                                f"{s['avg']:.4f}", f"{s['min']:.4f}", f"{s['max']:.4f}",
                                f"{err:+.4f}", f"{err / set_hz * 1e6:+.5f}", f"{spread:.5f}", flag])
                    fh.flush()
                    rows += 1
                    print(f"  {f:10g} MHz  read {s['avg'] / 1e6:.9f} MHz  err {err:+10.3f} Hz "
                          f"({err / set_hz * 1e6:+.4f} ppm)  n={s['samples']}  spread {spread:.4g} ppm"
                          + ("   <-- SUSPECT" if flag else ""))
        finally:
            # Whatever happened above: RF off first, then put everything back.
            try:
                bench.act(a.gen, "output", check=False, value=False)
                bench.act(a.gen, "set_freq", check=False, value=base["freq_mhz"])
                bench.act(a.gen, "set_ampl", check=False, value=base["level"])
                bench.act(a.counter, "set_func", check=False, value=base["func"])
                bench.act(a.counter, "set_gate", check=False, value=base["gate"])
                bench.act(a.counter, "set_imp", check=False, value="50" if base["imp"] == 50 else "1E6")
                g, c = bench.read(a.gen), bench.read(a.counter)
                print(f"restored: generator {g['freq_hz'] / 1e6:g} MHz {g['ampl_abs']:+.2f} dBm RF "
                      f"{'ON!' if g['output'] else 'off'}; counter {c['function']}, gate {c['gate_s']:g} s, "
                      f"Ch1 {c['ch1']['imp']:g} ohm")
            except BenchError as e:
                print(f"!! restoring failed: {e} -- CHECK THE 8648A RF OUTPUT", file=sys.stderr)
    if suspect:
        # The results explorer flags *_SUSPECT.csv with a NOT A MEASUREMENT banner.
        root, ext = os.path.splitext(out)
        os.replace(out, root + "_SUSPECT" + ext)
        out = root + "_SUSPECT" + ext
        print(f"!! {suspect} point(s) had scattered readings -- the counter was not counting cleanly. "
              f"File renamed so the results explorer flags it.")
    print(f"{rows} points written to {out}")


if __name__ == "__main__":
    try:
        main()
    except BenchError as e:
        sys.exit(f"stopped: {e}")
    except KeyboardInterrupt:
        sys.exit("interrupted -- look for the 'restored:' line above; if it is missing, "
                 "check the 8648A RF output by hand")
