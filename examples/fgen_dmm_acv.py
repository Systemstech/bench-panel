#!/usr/bin/env python3
"""AC amplitude check: a 33522A sine at a list of amplitudes, measured by a 34401A -> CSV.

    # plan only -- prints what it would do, switches nothing on:
    python3 examples/fgen_dmm_acv.py --amplitudes 0.5,1,2,5 --freq 1000
    # really do it:
    python3 examples/fgen_dmm_acv.py --amplitudes 0.5,1,2,5 --freq 1000 --output-on

WIRING: 33522A CH1 (or the channel you choose) BNC -> 34401A HI/LO, through a BNC to
banana adapter. Nothing else on that output.

WHAT IT DOES, IN ORDER
  1. Powers both instruments ON in the panel (only the ones that were OFF).
  2. Refuses to go on if that channel's output is already ON, if its amplitude unit is
     dBm, or if its load setting is one this script couldn't put back (it can set only
     50 ohm or High Z).
  3. Records the channel's function, frequency, amplitude, offset and load setting, and
     the meter's function, range and NPLC.
  4. Output off. Channel to SINE, your frequency, 0 V offset, load setting HIGH Z (the
     meter's 1 Mohm input is effectively open circuit, so the displayed amplitude is
     what arrives), the lowest amplitude. Meter to AC volts, auto range. Reads both
     back; stops with the output still off if anything didn't take.
  5. Output ON. At each amplitude: set it, wait, average the meter. One CSV row each.
  6. ALWAYS, even on an error, a Stop or Ctrl-C: output OFF, then everything put back
     as it was found, and a 'restored:' line read back from both instruments.

The amplitude is in the generator's own unit, which the script reads rather than
changes: Vpp (a sine's RMS is Vpp / 2*sqrt(2)) or Vrms. The CSV gives the expected RMS
and the meter's error in percent. It makes no pass/fail call: the 34401A's AC
accuracy depends on range and frequency -- see its manual.

SCRIPT LIMITS (its own, conservative): 10 Hz - 100 kHz; up to 10 Vpp (3.5 Vrms).
"""
BENCHPANEL_SCRIPT = {
    "schema": 1,
    "name": "33522A -> 34401A AC amplitude check",
    "version": "1.1.0",
    "description": "Sets a 33522A sine to a list of amplitudes and measures each on the 34401A "
                   "in AC volts: expected RMS vs measured, to a CSV. Wiring: the channel's BNC to "
                   "the meter's HI/LO. Plan-only unless 'Switch the output on' is ticked.",
    "uses": ["33522a", "34401a"],
    "wiring": [["33522a:ch{channel}", "34401a:in"]],
    "outputs": True,
    "args": [
        {"name": "amplitudes", "type": "str", "default": "0.5,1,2,5", "label": "Amplitudes",
         "help": "comma-separated, in the generator's unit (normally Vpp); at most 10 Vpp / 3.5 Vrms"},
        {"name": "freq", "type": "float", "default": 1000, "min": 10, "max": 100000, "label": "Frequency, Hz"},
        {"name": "channel", "type": "choice", "choices": ["1", "2"], "default": "1", "label": "Generator channel"},
        {"name": "settle", "type": "float", "default": 1.5, "min": 0.5, "max": 60, "label": "Settle time, s"},
        {"name": "readings", "type": "int", "default": 3, "min": 1, "max": 50, "label": "Meter readings per step"},
        {"name": "fgen", "type": "str", "default": "33522a-1", "label": "Generator's id"},
        {"name": "dmm", "type": "str", "default": "34401a-1", "label": "Meter's id"},
        {"name": "output_on", "type": "bool", "default": False, "label": "Switch the output on",
         "help": "unticked = plan only, nothing switched on"},
    ],
}

import argparse
import csv
import math
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "client"))
from benchpanel import Bench, BenchError  # noqa: E402

LIMIT = {"VPP": 10.0, "VRMS": 10.0 / (2 * math.sqrt(2))}
FMIN, FMAX = 10.0, 100000.0


def meter(bench, dmm, n, per=8.0):
    """Average n fresh meter readings, throwing away the one in flight when called."""
    got = lambda k: (lambda s: (s.get("samples") or 0) >= k)  # noqa: E731
    bench.act(dmm, "reset_stats")
    bench.wait_for(dmm, got(1), timeout=per)
    bench.act(dmm, "reset_stats")
    return bench.wait_for(dmm, got(n), timeout=per * n)


def load_setting(shown):
    """The card's load text -> the value the plugin can set back, or None."""
    if shown == "HighZ":
        return "INF"
    if shown and shown.replace("Ω", "").strip() == "50":
        return "50"
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", help="the panel (default: the one that started this script, else local)")
    ap.add_argument("--login", help="login file for the HTTPS portal (user:password, chmod 600)")
    ap.add_argument("--amplitudes", default="0.5,1,2,5")
    ap.add_argument("--freq", type=float, default=1000.0)
    ap.add_argument("--channel", type=int, choices=(1, 2), default=1)
    ap.add_argument("--settle", type=float, default=1.5)
    ap.add_argument("--readings", type=int, default=3)
    ap.add_argument("--fgen", default="33522a-1")
    ap.add_argument("--dmm", default="34401a-1")
    ap.add_argument("--out", help="CSV path (default ./fgen_dmm_acv_<time>.csv)")
    ap.add_argument("--output-on", action="store_true", help="actually switch the generator output on")
    a = ap.parse_args()

    try:
        amps = [float(x) for x in a.amplitudes.split(",") if x.strip()]
    except ValueError:
        sys.exit(f"--amplitudes: {a.amplitudes!r} is not a comma-separated list of numbers")
    if not amps or min(amps) <= 0:
        sys.exit("--amplitudes: give one or more amplitudes above 0")
    if not FMIN <= a.freq <= FMAX:
        sys.exit(f"{a.freq:g} Hz outside this script's {FMIN:g} Hz - {FMAX:g} Hz")
    ch = a.channel
    key = f"ch{ch}"
    out = a.out or f"fgen_dmm_acv_{time.strftime('%Y%m%d_%H%M%S')}.csv"

    print(f"plan: {a.fgen} CH{ch} sine {a.freq:g} Hz -> {a.dmm} (AC volts), amplitudes "
          f"{', '.join(f'{x:g}' for x in amps)} (the generator's unit, checked when it runs), "
          f"settle {a.settle:g} s, {a.readings} readings/step")
    print(f"      CSV -> {out}")
    if not a.output_on:
        # The amplitude limit depends on the unit, which is only known by asking the
        # generator; a dry run doesn't talk to it. The real run checks before any output.
        if max(amps) > LIMIT["VPP"]:
            print(f"note: {max(amps):g} is over {LIMIT['VPP']:g} Vpp -- a real run will refuse it if the unit is Vpp.")
        print("dry run: nothing switched on. Tick 'Switch the output on' (or add --output-on) to run it.")
        return

    bench = Bench(a.url, login_file=a.login)
    with bench.session(a.fgen, a.dmm):
        g0, m0 = bench.read(a.fgen), bench.read(a.dmm)
        c0 = g0[key]
        if c0.get("output"):
            raise BenchError(f"{a.fgen} CH{ch} output is already ON -- someone is using it. Nothing changed.")
        unit = (c0.get("unit") or "").upper()
        if unit not in LIMIT:
            raise BenchError(f"{a.fgen} CH{ch} amplitude unit is {unit or '?'}; this script needs Vpp or Vrms. "
                             "Change it on the generator. Nothing changed.")
        if max(amps) > LIMIT[unit]:
            raise BenchError(f"{max(amps):g} {unit} is over this script's {LIMIT[unit]:.3g} {unit}. Nothing changed.")
        load0 = load_setting(c0.get("load"))
        if load0 is None:
            raise BenchError(f"{a.fgen} CH{ch} load setting is {c0.get('load')!r}; this script can only put back "
                             "50 ohm or High Z. Set one of those on the generator first. Nothing changed.")
        base = {"func": c0["func"], "freq": c0["freq_hz"], "ampl": c0["ampl"], "offset": c0["offset"],
                "load": load0, "mfunc": m0["function"], "mrange": m0["range"], "nplc": m0["nplc"]}
        print(f"baseline: CH{ch} {base['func']} {base['freq']:g} Hz {base['ampl']:g} {unit} offset {base['offset']:g} V "
              f"load {c0.get('load')}; meter {base['mfunc']} {base['mrange']} NPLC {base['nplc']:g}")
        rows = 0
        try:
            bench.act(a.fgen, "output", ch=ch, value=False)
            bench.act(a.fgen, "set_load", ch=ch, value="INF")
            bench.act(a.fgen, "set_func", ch=ch, value="SIN")
            bench.act(a.fgen, "set_freq", ch=ch, value=a.freq)
            bench.act(a.fgen, "set_offset", ch=ch, value=0)
            bench.act(a.fgen, "set_ampl", ch=ch, value=min(amps))
            bench.act(a.dmm, "function", value="ACV")
            bench.act(a.dmm, "range", value="AUTO")
            g, m = bench.read(a.fgen), bench.read(a.dmm)
            c = g[key]
            problems = []
            if c["output"]:
                problems.append("output still ON")
            if c["func"] != "SIN" or abs(c["freq_hz"] - a.freq) > 1e-6 * a.freq + 1e-3:
                problems.append(f"generator took {c['func']} {c['freq_hz']} Hz")
            if abs(c["offset"]) > 1e-3 or c["load"] != "HighZ" or abs(c["ampl"] - min(amps)) > 1e-3 * min(amps) + 1e-4:
                problems.append(f"generator took {c['ampl']} {c['unit']}, offset {c['offset']}, load {c['load']}")
            if (c.get("unit") or "").upper() != unit:
                problems.append(f"amplitude unit changed to {c.get('unit')}")
            if m["function"] != "ACV":
                problems.append(f"meter is on {m['function']}")
            if problems:
                raise BenchError("setup did not take, output left off: " + "; ".join(problems))
            print(f"pre-output check: CH{ch} SIN {c['freq_hz']:g} Hz {c['ampl']:g} {unit}, offset 0, HighZ, "
                  f"output off; meter ACV")

            bench.act(a.fgen, "output", ch=ch, value=True, confirmed=True)
            with open(out, "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["timestamp", "freq_hz", "set", "unit", "expected_vrms", "meter_vrms",
                            "meter_min", "meter_max", "n", "error_pct"])
                for amp in amps:
                    bench.act(a.fgen, "set_ampl", ch=ch, value=amp)
                    time.sleep(a.settle)
                    s = meter(bench, a.dmm, a.readings)
                    exp = amp / (2 * math.sqrt(2)) if unit == "VPP" else amp
                    err = (s["avg"] - exp) / exp * 100
                    w.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), a.freq, amp, unit, f"{exp:.6f}",
                                f"{s['avg']:.6f}", f"{s['min']:.6f}", f"{s['max']:.6f}", s["samples"], f"{err:+.4f}"])
                    fh.flush()
                    rows += 1
                    print(f"  {amp:7.3f} {unit}  expect {exp:9.6f} Vrms  meter {s['avg']:9.6f} Vrms  "
                          f"error {err:+7.3f} %")
        finally:
            # Whatever happened: output OFF first, then put everything back. The load
            # setting goes back BEFORE the amplitude, because the amplitude is shown
            # (and set) relative to it.
            try:
                bench.act(a.fgen, "output", ch=ch, check=False, value=False)
                bench.act(a.fgen, "set_load", ch=ch, check=False, value=base["load"])
                bench.act(a.fgen, "set_func", ch=ch, check=False, value=base["func"])
                bench.act(a.fgen, "set_freq", ch=ch, check=False, value=base["freq"])
                bench.act(a.fgen, "set_ampl", ch=ch, check=False, value=base["ampl"])
                bench.act(a.fgen, "set_offset", ch=ch, check=False, value=base["offset"])
                bench.act(a.dmm, "function", check=False, value=base["mfunc"])
                bench.act(a.dmm, "range", check=False, value=base["mrange"])
                bench.act(a.dmm, "nplc", check=False, value=base["nplc"])
                g, m = bench.read(a.fgen), bench.read(a.dmm)
                c = g[key]
                print(f"restored: CH{ch} output {'ON!' if c['output'] else 'off'}, {c['func']} {c['freq_hz']:g} Hz "
                      f"{c['ampl']:g} {c['unit']} offset {c['offset']:g} V load {c['load']}; "
                      f"meter {m['function']} {m['range']} NPLC {m['nplc']:g}")
            except BenchError as e:
                print(f"!! restoring failed: {e} -- CHECK THE 33522A CH{ch} OUTPUT", file=sys.stderr)
    print(f"{rows} steps written to {out}")


if __name__ == "__main__":
    try:
        main()
    except BenchError as e:
        sys.exit(f"stopped: {e}")
    except KeyboardInterrupt:
        sys.exit("interrupted -- look for the 'restored:' line above; if it is missing, "
                 "check the 33522A output by hand")
