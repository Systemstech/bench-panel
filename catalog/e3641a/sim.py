#!/usr/bin/env python3
"""Simulator model of the E3641A -- for the /sim/ bench. Not a driver; see benchcore/sim.py.

Answers exactly the SCPI driver.py sends (E3640-90001). Anything else is -113, so a
typo in the driver or a new command nobody modelled shows up as an error in the
simulated traffic log instead of passing silently.

Modelled: power-on state (0 V, 0.8 A, LOW, OVP 66 V on, output off -- the driver's
own notes, manual Table 3-4), range limits, CV/CC against whatever is wired to the
output (a meter's 10 Mohm is next to nothing), the OVP trip, five saved setups
including the output state. Readback is the setting plus ~0.5 mV of noise:
ideal, NOT the real supply's accuracy.
"""
import math

from benchcore.sim import OutOfRange, SimModel, SettingsConflict, Undefined, noisy, num, onoff, sci

RANGES = {"P35V": (35.0, 0.8), "P60V": (60.0, 0.5)}
RANGE_WORDS = {"P35V": "P35V", "LOW": "P35V", "P60V": "P60V", "HIGH": "P60V"}


class E3641ASim(SimModel):
    OUTPUTS = {"out": "output + / -"}
    PRESETS = {
        "output-on-5v": "someone left the output ON at 5 V / 0.1 A (a script must refuse to start)",
        "high-range": "HIGH (60 V) range selected, 12 V set, output off",
        "ovp-tripped": "OVP tripped at 3 V: output off, trip flag set",
    }

    def power_on_reset(self):
        self.rng = "P35V"
        self.v, self.i = 0.0, 0.8
        self.out = False
        self.ovp, self.ovp_on, self.tripped = 66.0, True, False
        self.slots = getattr(self, "slots", None) or {n: None for n in range(1, 6)}
        self.names = getattr(self, "names", None) or {n: "" for n in range(1, 6)}

    def apply_preset(self, name):
        super().apply_preset(name)
        if name == "output-on-5v":
            self.v, self.i, self.out = 5.0, 0.1, True
        elif name == "high-range":
            self.rng, self.i, self.v = "P60V", 0.5, 12.0
        elif name == "ovp-tripped":
            self.ovp, self.tripped = 3.0, True

    # -- physics -----------------------------------------------------------------------
    def _terminal(self):
        """(volts, amps, mode) at the output terminals right now."""
        if not self.out:
            return 0.0, 0.0, None
        z = self.load_on("out")
        i_cv = 0.0 if math.isinf(z) else self.v / z
        if i_cv > self.i:                       # can't hold the voltage: current limit
            return self.i * z, self.i, "CC"
        return self.v, i_cv, "CV"

    def _check_ovp(self):
        if self.out and self.ovp_on and self._terminal()[0] > self.ovp:
            self.out, self.tripped = False, True

    def source(self, port, z_load):
        if port != "out" or not self.out:
            return None
        v, _, _ = self._terminal()
        return {"dc": v, "vrms": 0.0, "freq": None, "wave": "DC"}

    # -- SCPI --------------------------------------------------------------------------
    def query(self, h, arg):
        if h == "MEAS:VOLT":
            v = self._terminal()[0]
            return f"{noisy(v, 0.0003) if self.out else noisy(-0.007, 0.0005):+.5E}"
        if h == "MEAS:CURR":
            i = self._terminal()[1]
            return f"{noisy(i, 0.00003) if self.out else noisy(-0.0001, 0.00002):+.5E}"
        if h == "VOLT":
            return sci(self.v, 5)
        if h == "CURR":
            return sci(self.i, 5)
        if h == "OUTP":
            return "1" if self.out else "0"
        if h == "VOLT:RANG":
            return self.rng
        if h == "VOLT:PROT":
            return sci(self.ovp, 5)
        if h == "VOLT:PROT:STAT":
            return "1" if self.ovp_on else "0"
        if h == "VOLT:PROT:TRIP":
            return "1" if self.tripped else "0"
        if h == "STAT:QUES:COND":
            # What is UNREGULATED: bit 0 = voltage (so CC), bit 1 = current (so CV).
            mode = self._terminal()[2]
            return str(1 if mode == "CC" else 2 if mode == "CV" else 0)
        if h == "MEM:STAT:NAME":
            n = self._slot(arg)
            return f'"{self.names[n]}"'
        raise Undefined()

    def command(self, h, arg):
        vmax, imax = RANGES[self.rng]
        if h == "VOLT":
            v = num(arg, ("V",))
            if not 0 <= v <= vmax * 1.03:
                raise OutOfRange()
            self.v = v
            self._check_ovp()
        elif h == "CURR":
            i = num(arg, ("A",))
            if not 0 <= i <= imax * 1.03:
                raise OutOfRange()
            self.i = i
        elif h == "VOLT:RANG":
            r = RANGE_WORDS.get((arg or "").upper())
            if r is None:
                raise OutOfRange("Illegal parameter value")
            self.rng = r
            vmax, imax = RANGES[r]
            self.v, self.i = min(self.v, vmax), min(self.i, imax)
        elif h == "OUTP":
            on = onoff(arg)
            if on and self.tripped:
                raise SettingsConflict("Settings conflict: OVP tripped -- clear it first")
            self.out = on
            self._check_ovp()
        elif h == "VOLT:PROT":
            self.ovp = num(arg, ("V",))
            self._check_ovp()
        elif h == "VOLT:PROT:STAT":
            self.ovp_on = onoff(arg)
            self._check_ovp()
        elif h == "VOLT:PROT:CLE":
            self.tripped = False
        elif h == "*SAV":
            self.slots[self._slot(arg)] = (self.rng, self.v, self.i, self.out, self.ovp, self.ovp_on)
        elif h == "*RCL":
            s = self.slots[self._slot(arg)]
            if s is None:                       # an empty slot recalls the *RST state
                self.power_on_reset()
            else:
                self.rng, self.v, self.i, self.out, self.ovp, self.ovp_on = s
            self._check_ovp()
        elif h == "MEM:STAT:NAME":
            n, _, name = (arg or "").partition(",")
            self.names[self._slot(n)] = name.strip().strip("'\"")[:9]
        else:
            raise Undefined()

    @staticmethod
    def _slot(arg):
        try:
            n = int(float(arg))
        except (TypeError, ValueError):
            raise OutOfRange() from None
        if n not in range(1, 6):
            raise OutOfRange()
        return n
