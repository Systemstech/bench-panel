#!/usr/bin/env python3
"""Simulator model of the 8648A -- for the /sim/ bench. Not a driver; see benchcore/sim.py.

Answers exactly the SCPI driver.py sends. One output, "rf", a 50-ohm source whose
level is specified INTO 50 ohms.

REFERENCE MODE is modelled, because it is the trap: with it on, POW:AMPL is a DELTA
from the stored reference and the connector puts out (entered + reference). The
default preset is the factory state (reference mode OFF). The preset "ref-on-5dbm"
is the state this bench's 8648A was found in (ON, +5.00 dBm stored) -- run a script
against both to see that it handles either.

Limits enforced (error -222): 0.1..1000 MHz, -136..+13 dBm at the connector.
"""
import math

from benchcore.sim import OutOfRange, SimModel, Undefined, num, onoff, sci

ZSRC = 50.0


class Hp8648ASim(SimModel):
    OUTPUTS = {"rf": "RF OUTPUT (N connector)"}
    PRESETS = {
        "ref-on-5dbm": "REFERENCE MODE ON with +5.00 dBm stored (how this bench's 8648A was found)",
        "rf-on": "RF output left ON at 100 MHz, -30 dBm -- a script must refuse or handle it",
        "am-on": "AM modulation left on",
    }

    def power_on_reset(self):
        # The MODEL's start-up values. *RST is 100 MHz and -136 dBm (Operation and Service
        # Guide pp. 2-24, 2-38); what the instrument recalls at power-on is not checked, so
        # the level here is a readable -55 dBm rather than *RST's -136.
        self.freq = 100e6
        self.entered = -55.0
        self.ref_on, self.ref = False, 0.0
        self.att_auto = True
        self.out = False
        self.mods = {"AM": False, "FM": False, "PM": False, "PULM": False}

    def apply_preset(self, name):
        super().apply_preset(name)
        if name == "ref-on-5dbm":
            self.ref_on, self.ref, self.entered = True, 5.0, -60.0
        elif name == "rf-on":
            self.out, self.entered = True, -30.0
        elif name == "am-on":
            self.mods["AM"] = True

    def absolute(self):
        return self.entered + (self.ref if self.ref_on else 0.0)

    def source(self, port, z_load):
        if port != "rf" or not self.out:
            return None
        # Level is specified into 50 ohms -> EMF, then divide into the actual load.
        v50 = math.sqrt(10 ** (self.absolute() / 10) * 1e-3 * 50.0)
        emf = 2 * v50
        k = 1.0 if math.isinf(z_load) else z_load / (z_load + ZSRC)
        return {"dc": 0.0, "vrms": emf * k, "freq": self.freq, "wave": "SIN"}

    def query(self, h, arg):
        if h == "FREQ:CW":
            return sci(self.freq, 10)
        if h == "POW:AMPL":
            return f"{self.entered:+.2f}"
        if h == "POW:REF:STAT":
            return "1" if self.ref_on else "0"
        if h == "POW:REF":
            return f"{self.ref:+.2f}"
        if h == "POW:ATT:AUTO":
            return "1" if self.att_auto else "0"
        if h == "OUTP:STAT":
            return "1" if self.out else "0"
        for m in self.mods:
            if h == f"{m}:STAT":
                return "1" if self.mods[m] else "0"
        raise Undefined()

    def command(self, h, arg):
        if h == "FREQ:CW":
            f = num(arg, ("HZ",))
            if not 100e3 <= f <= 1000e6:
                raise OutOfRange()
            self.freq = f
        elif h == "POW:AMPL":
            v = num(arg, ("DBM", "DB"))
            if not -136.0 <= v + (self.ref if self.ref_on else 0.0) <= 13.0:
                raise OutOfRange()
            self.entered = v
        elif h == "OUTP:STAT":
            self.out = onoff(arg)
        else:
            raise Undefined()
