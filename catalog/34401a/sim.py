#!/usr/bin/env python3
"""Simulator model of the 34401A -- for the /sim/ bench. Not a driver; see benchcore/sim.py.

Answers exactly the SCPI driver.py sends. Measures whatever is wired to its HI/LO
input ("in"): DC volts, true-RMS AC volts, frequency and period. Current and
resistance read as nothing is connected (0 A; an open circuit is overload).

Modelled: the function, per-function range (auto or fixed) and NPLC; overload
(+9.9E37) when a fixed range is too small; a READ? that TAKES TIME -- NPLC / 60 Hz,
the integration time, so a script that averages 100 readings at NPLC 10 waits as
long as it would on the bench. Input impedance 10 Mohm on DC, 1 Mohm on AC and
frequency (so a 50-ohm generator setting shows the doubling it does on the bench).
Noise is a few ppm: ideal, NOT the meter's accuracy.
"""
import math
import time

from benchcore.sim import OutOfRange, SimModel, Undefined, noisy, num, onoff, sci

CONF = {"CONF:VOLT:DC": "VOLT:DC", "CONF:VOLT:AC": "VOLT:AC", "CONF:CURR:DC": "CURR:DC",
        "CONF:CURR:AC": "CURR:AC", "CONF:RES": "RES", "CONF:FRES": "FRES", "CONF:FREQ": "FREQ",
        "CONF:PER": "PER", "CONF:DIOD": "DIOD", "CONF:CONT": "CONT"}
RANGES = {"VOLT:DC": [0.1, 1, 10, 100, 1000], "VOLT:AC": [0.1, 1, 10, 100, 750],
          "CURR:DC": [0.01, 0.1, 1, 3], "CURR:AC": [1, 3],
          "RES": [100, 1e3, 1e4, 1e5, 1e6, 1e7, 1e8], "FRES": [100, 1e3, 1e4, 1e5, 1e6, 1e7, 1e8]}
NPLCS = [0.02, 0.2, 1, 10, 100]
LINE_HZ = 60.0
OVERLOAD = 9.9e37


class Hp34401ASim(SimModel):
    INPUTS = {"in": "HI / LO input (V, ohm, freq)"}
    PRESETS = {
        "pinned-100mv": "left on DC volts, FIXED 100 mV range (a script that forgets range reads overload)",
        "acv": "left on AC volts",
    }

    def power_on_reset(self):
        self.fn = "VOLT:DC"
        self.cfg = {f: {"auto": True, "range": r[-2], "nplc": 10.0} for f, r in RANGES.items()}
        self.trig = "IMM"

    def apply_preset(self, name):
        super().apply_preset(name)
        if name == "pinned-100mv":
            self.cfg["VOLT:DC"].update(auto=False, range=0.1)
        elif name == "acv":
            self.fn = "VOLT:AC"

    def z_in(self, port):
        return 10e6 if self.fn in ("VOLT:DC", "RES", "FRES", "DIOD", "CONT") else 1e6

    # -- measuring --------------------------------------------------------------------
    def _true(self):
        """The ideal value for the present function, from what is on the input."""
        s = self.signal_into("in") or {"dc": 0.0, "vrms": 0.0, "freq": None}
        if self.fn == "VOLT:DC":
            return s["dc"]
        if self.fn == "VOLT:AC":
            return s["vrms"]
        if self.fn in ("FREQ", "PER"):
            f = s["freq"] if s["vrms"] > 0.01 else None
            if not f:
                return 0.0                      # the MODEL reads 0 with no signal (not checked against the manual)
            return f if self.fn == "FREQ" else 1.0 / f
        if self.fn in ("CURR:DC", "CURR:AC"):
            return 0.0
        return OVERLOAD                         # RES/FRES/DIOD/CONT: open circuit

    def _read(self):
        c = self.cfg.get(self.fn)
        nplc = c["nplc"] if c else 1.0
        time.sleep(min(nplc / LINE_HZ, 2.0) if self.fn != "VOLT:AC" else 0.4)
        v = self._true()
        if v >= OVERLOAD:
            return OVERLOAD
        if c:
            rngs = RANGES[self.fn]
            rng = next((r for r in rngs if abs(v) <= r * 1.2), rngs[-1]) if c["auto"] else c["range"]
            if c["auto"]:
                c["range"] = rng
            if abs(v) > rng * 1.2:
                return OVERLOAD
            return noisy(v, abs(v) * 2e-6 + rng * 3e-7 * (10.0 / max(nplc, 0.02)) ** 0.5)
        return noisy(v, abs(v) * 1e-6)

    # -- SCPI --------------------------------------------------------------------------
    def query(self, h, arg):
        if h == "READ":
            return sci(self._read(), 9)
        for f in RANGES:
            if h == f"{f}:NPLC":
                return sci(self.cfg[f]["nplc"], 5)
            if h == f"{f}:RANG:AUTO":
                return "1" if self.cfg[f]["auto"] else "0"
            if h == f"{f}:RANG":
                return sci(self.cfg[f]["range"], 5)
        raise Undefined()

    def command(self, h, arg):
        if h in CONF:
            self.fn = CONF[h]
            if self.fn in self.cfg:
                self.cfg[self.fn]["auto"] = True
            return
        if h == "TRIG:SOUR":
            if (arg or "").upper() not in ("IMM", "BUS", "EXT"):
                raise OutOfRange("Illegal parameter value")
            self.trig = arg.upper()
            return
        for f in RANGES:
            if h == f"{f}:NPLC":
                v = num(arg)
                if v not in NPLCS:
                    # The MODEL rounds to the nearest supported value. What the real
                    # meter does is not checked here -- a script should read back.
                    v = min(NPLCS, key=lambda x: abs(math.log10(x) - math.log10(max(v, 1e-3))))
                self.cfg[f]["nplc"] = v
                return
            if h == f"{f}:RANG:AUTO":
                self.cfg[f]["auto"] = onoff(arg)
                return
            if h == f"{f}:RANG":
                v = num(arg, ("V", "A", "OHM"))
                pick = next((r for r in RANGES[f] if v <= r * 1.2), None)
                if pick is None:
                    raise OutOfRange()
                self.cfg[f].update(auto=False, range=pick)
                return
        raise Undefined()
