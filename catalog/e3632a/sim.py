#!/usr/bin/env python3
"""Simulator model of the E3632A -- for the /sim/ bench. Not a driver; see benchcore/sim.py.

Answers exactly the SCPI driver.py sends (E3632-90001). Anything else is -113.

Modelled: the *RST state (p. 94: 0 V, 7 A, P15V, OVP 32 V on, OCP 7.5 A on, output
off), range limits at the programming maxima (Table 4-1: 15.45 V / 7.21 A, 30.9 V /
4.12 A), CV/CC against whatever is wired to the output, OVP and OCP trips, three
saved setups including the output state. Readback is ideal plus a little noise,
NOT the real supply's accuracy.

ASSUMED, not from the manual: what OUTP? reports after a trip. The manual says an
OVP trip shorts the output with an SCR and an OCP trip programs the current to
zero; here a trip delivers 0 V / 0 A until cleared and OUTP? is left unchanged.

Preset "readback-stuck" reproduces a real fault seen on this bench: MEAS:VOLT?
and MEAS:CURR? return -3.45 mV / 11.96 mA whatever the output does. It exists to
prove the driver's readback_ok check catches it.
"""
import math

from benchcore.sim import OutOfRange, SimModel, Undefined, noisy, num, onoff, sci

RANGES = {"P15V": (15.45, 7.21), "P30V": (30.9, 4.12)}
RANGE_WORDS = {"P15V": "P15V", "LOW": "P15V", "P30V": "P30V", "HIGH": "P30V"}
STUCK = (-3.45286e-03, 1.19644e-02)


class E3632ASim(SimModel):
    OUTPUTS = {"out": "output + / -"}
    PRESETS = {
        "output-on-5v": "someone left the output ON at 5 V / 1 A (a script must refuse to start)",
        "high-range": "HIGH (30 V / 4 A) range selected, 12 V set, output off",
        "ovp-tripped": "OVP tripped at 3 V: output delivers nothing, trip flag set",
        "readback-stuck": "the ADC fault: MEAS always -3.45 mV / 11.96 mA, output works",
    }

    def power_on_reset(self):
        self.rng = "P15V"
        self.v, self.i = 0.0, 7.0
        self.out = False
        self.ovp, self.ovp_on, self.ovp_trip = 32.0, True, False
        self.ocp, self.ocp_on, self.ocp_trip = 7.5, True, False
        self.stuck = False
        self.slots = getattr(self, "slots", None) or {n: None for n in (1, 2, 3)}

    def apply_preset(self, name):
        super().apply_preset(name)
        if name == "output-on-5v":
            self.v, self.i, self.out = 5.0, 1.0, True
        elif name == "high-range":
            self.rng, self.i, self.v = "P30V", 4.0, 12.0
        elif name == "ovp-tripped":
            self.ovp, self.ovp_trip = 3.0, True
        elif name == "readback-stuck":
            self.stuck = True

    # -- physics -----------------------------------------------------------------------
    def _terminal(self):
        """(volts, amps, mode) at the output terminals right now."""
        if not self.out or self.ovp_trip or self.ocp_trip:
            return 0.0, 0.0, None
        z = self.load_on("out")
        i_cv = 0.0 if math.isinf(z) else self.v / z
        if i_cv > self.i:
            return self.i * z, self.i, "CC"
        return self.v, i_cv, "CV"

    def _check_trips(self):
        v, i, _ = self._terminal()
        if self.out and self.ovp_on and v > self.ovp:
            self.ovp_trip = True
        if self.out and self.ocp_on and i > self.ocp:
            self.ocp_trip = True

    def source(self, port, z_load):
        if port != "out":
            return None
        v, _, mode = self._terminal()
        if mode is None:
            return None
        return {"dc": v, "vrms": 0.0, "freq": None, "wave": "DC"}

    # -- SCPI --------------------------------------------------------------------------
    def query(self, h, arg):
        if h in ("MEAS:VOLT", "MEAS"):
            if self.stuck:
                return f"{STUCK[0]:+.8E}"
            return f"{noisy(self._terminal()[0], 0.0003):+.8E}"
        if h == "MEAS:CURR":
            if self.stuck:
                return f"{STUCK[1]:+.8E}"
            return f"{noisy(self._terminal()[1], 0.0002):+.8E}"
        if h == "VOLT":
            return sci(self.v, 8)
        if h == "CURR":
            return sci(self.i, 8)
        if h == "OUTP":
            return "1" if self.out else "0"
        if h == "VOLT:RANG":
            return self.rng
        if h == "VOLT:PROT":
            return sci(self.ovp, 8)
        if h == "VOLT:PROT:STAT":
            return "1" if self.ovp_on else "0"
        if h == "VOLT:PROT:TRIP":
            return "1" if self.ovp_trip else "0"
        if h == "CURR:PROT":
            return sci(self.ocp, 8)
        if h == "CURR:PROT:STAT":
            return "1" if self.ocp_on else "0"
        if h == "CURR:PROT:TRIP":
            return "1" if self.ocp_trip else "0"
        if h == "STAT:QUES:COND":
            mode = self._terminal()[2]      # p. 108: 1 = CC, 2 = CV, 0 = off
            return str(1 if mode == "CC" else 2 if mode == "CV" else 0)
        raise Undefined()

    def command(self, h, arg):
        vmax, imax = RANGES[self.rng]
        if h == "VOLT":
            v = num(arg, ("V",))
            if not 0 <= v <= vmax:
                raise OutOfRange()
            self.v = v
        elif h == "CURR":
            i = num(arg, ("A",))
            if not 0 <= i <= imax:
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
            self.out = onoff(arg)
        elif h == "VOLT:PROT":
            v = num(arg, ("V",))
            if not 1.0 <= v <= 32.0:
                raise OutOfRange()
            self.ovp = v
        elif h == "VOLT:PROT:STAT":
            self.ovp_on = onoff(arg)
        elif h == "VOLT:PROT:CLE":
            self.ovp_trip = False
        elif h == "CURR:PROT":
            i = num(arg, ("A",))
            if not 0.0 <= i <= 7.5:
                raise OutOfRange()
            self.ocp = i
        elif h == "CURR:PROT:STAT":
            self.ocp_on = onoff(arg)
        elif h == "CURR:PROT:CLE":
            self.ocp_trip = False
        elif h == "*SAV":
            self.slots[self._slot(arg)] = (self.rng, self.v, self.i, self.out, self.ovp,
                                           self.ovp_on, self.ocp, self.ocp_on)
        elif h == "*RCL":
            s = self.slots[self._slot(arg)]
            if s is None:
                stuck = self.stuck
                self.power_on_reset()
                self.stuck = stuck
            else:
                (self.rng, self.v, self.i, self.out, self.ovp,
                 self.ovp_on, self.ocp, self.ocp_on) = s
        else:
            raise Undefined()
        self._check_trips()

    @staticmethod
    def _slot(arg):
        try:
            n = int(float(arg))
        except (TypeError, ValueError):
            raise OutOfRange() from None
        if n not in (1, 2, 3):
            raise OutOfRange()
        return n
