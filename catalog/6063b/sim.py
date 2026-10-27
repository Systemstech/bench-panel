#!/usr/bin/env python3
"""Simulator model of the 6063B electronic load -- for the /sim/ bench. Not a driver.

Answers exactly the HPSL driver.py sends (06060-90005); anything else is -113.

The load's INPUT is wired to a supply's output ("e3641a-1:out" -> "6063b-1:in").
It presents an impedance to that supply and reads back the voltage the supply
then produces:
  CC  z = V_last / I      (V_last = the voltage it read last time; converges in a poll)
  CR  z = R
  CV  z = V_set / 10 A    CRUDE: a real CV load draws whatever holds V_set. Good
                          enough to see a supply go into its current limit; not a
                          model of CV regulation.
  off or shut down: open circuit.
Protection: OCP trips at once (the programmed delay is not modelled), 250 W
overpower trips PS+OP, both latch until INP:PROT:CLE. INP? stays 1 through a trip,
as PROG p. 45 says. Readback is ideal plus a little noise, not the real +/-150 mV.

Presets reproduce things seen on this bench: "sense-rmt-no-leads" (the rear sense
switch on RMT with no sense leads: voltage reads 0 in every mode, CV/CR input
goes off -- OPER p. 43), "input-on-1a" (someone left it sinking), "prot-shutdown".
"""
import math

from benchcore.sim import OutOfRange, SimModel, Undefined, noisy, num, onoff, sci

B = {"VF": 1, "OC": 2, "OP": 8, "OT": 16, "UNR": 1024, "RV": 2048, "OV": 4096, "PS": 8192}
CURR_MAX = {1.0: 1.0, 10.0: 10.0}
RES_SPAN = {24.0: (0.0, 24.0), 24000.0: (24.0, 24000.0), 240000.0: (240.0, 240000.0)}


class HP6063BSim(SimModel):
    INPUTS = {"in": "load input + / -"}
    PRESETS = {
        "input-on-1a": "someone left the input ON, CC 1 A (a script must refuse to start)",
        "prot-shutdown": "a protection shutdown (OC) has latched: input reads on, sinks nothing",
        "sense-rmt-no-leads": "rear sense switch on RMT, no sense leads: V reads 0, CV/CR input off",
    }

    def power_on_reset(self):
        # OPER Table 4-6 wake-up values, EXCEPT the input: modelled OFF. The manuals
        # disagree about it, and a simulated bench that powers up sinking would hide
        # a script's missing "input off" at the start.
        self.inp = False
        self.mode = "CURR"
        self.i, self.i_rng = 0.0, 10.0
        self.r, self.r_rng = 24000.0, 24000.0
        self.v = 240.0
        self.ocp, self.ocp_del, self.ocp_on = 10.2, 15.0, False
        self.latched = 0
        self.rmt_sense = False
        self.v_last = None
        self.slots = getattr(self, "slots", None) or {n: None for n in range(1, 7)}

    def apply_preset(self, name):
        super().apply_preset(name)
        if name == "input-on-1a":
            self.inp, self.i = True, 1.0
        elif name == "prot-shutdown":
            self.inp, self.i, self.latched = True, 1.0, B["OC"] | B["PS"]
        elif name == "sense-rmt-no-leads":
            self.rmt_sense = True

    # -- physics -----------------------------------------------------------------------
    def _sinking(self):
        if not self.inp or self.latched & B["PS"]:
            return False
        if self.rmt_sense and self.mode in ("RES", "VOLT"):
            return False
        return True

    def z_in(self, port):
        if not self._sinking():
            return math.inf
        if self.mode == "RES":
            return max(self.r, 0.01)
        if self.mode == "VOLT":
            return max(self.v, 0.01) / 10.0
        if self.i <= 0:
            return math.inf
        return max(self.v_last if self.v_last else 1e6, 0.001) / self.i

    def _read(self):
        """(volts at the input, amps sunk) right now; applies protection."""
        sig = self.signal_into("in")
        v = float(sig["dc"]) if sig else 0.0
        z = self.z_in("in")
        i = 0.0 if math.isinf(z) or v <= 0 else v / z
        self.v_last = v if v > 0 else None
        if self._sinking():
            if self.ocp_on and i > self.ocp:
                self.latched |= B["OC"] | B["PS"]
            elif v * i > 250.0:
                self.latched |= B["OP"] | B["PS"]
            if self.latched & B["PS"]:
                i = 0.0
        return v, i

    def _cond(self, v, i):
        bits = self.latched
        if self._sinking() and self.mode == "CURR" and self.i > 0 and i < 0.98 * self.i:
            bits |= B["UNR"]
        return bits

    # -- SCPI --------------------------------------------------------------------------
    def query(self, h, arg):
        if h in ("MEAS:VOLT", "MEAS:CURR", "MEAS:POW"):
            v, i = self._read()
            shown_v = 0.0 if self.rmt_sense else v
            val = {"MEAS:VOLT": noisy(shown_v, 0.01), "MEAS:CURR": noisy(i, 0.002),
                   "MEAS:POW": shown_v * i}[h]
            return f"{val:+.5E}"
        if h == "INP":
            return "1" if self.inp else "0"
        if h == "MODE":
            return self.mode
        if h == "CURR":
            return sci(self.i, 5)
        if h == "RES":
            return sci(self.r, 5)
        if h == "VOLT":
            return sci(self.v, 5)
        if h == "CURR:RANG":
            return sci(self.i_rng, 5)
        if h == "RES:RANG":
            return sci(self.r_rng, 5)
        if h == "CURR:PROT":
            return sci(self.ocp, 5)
        if h == "CURR:PROT:DEL":
            return sci(self.ocp_del, 5)
        if h == "CURR:PROT:STAT":
            return "1" if self.ocp_on else "0"
        if h == "STAT:QUES:COND":
            v, i = self._read()
            return str(self._cond(v, i))
        raise Undefined()

    def command(self, h, arg):
        if h == "INP":
            self.inp = onoff(arg)
        elif h in ("MODE:CURR", "MODE:RES", "MODE:VOLT"):
            self.mode = h.split(":")[1]
        elif h == "CURR:RANG":
            r = num(arg, ("A",))
            self.i_rng = 1.0 if r <= 1.0 else 10.0
            if self.i > self.i_rng:                 # PROG p. 46: level -> new range's max
                self.i = self.i_rng
        elif h == "RES:RANG":
            r = num(arg, ("OHM",))
            self.r_rng = 24.0 if r <= 24 else 24000.0 if r <= 24000 else 240000.0
            lo, hi = RES_SPAN[self.r_rng]
            if not lo <= self.r <= hi:
                self.r = hi
        elif h == "CURR":
            i = num(arg, ("A",))
            if not 0 <= i <= CURR_MAX[self.i_rng] * 1.02:
                raise OutOfRange()
            self.i = i
        elif h == "RES":
            r = num(arg, ("OHM",))
            lo, hi = RES_SPAN[self.r_rng]
            if not lo <= r <= hi:
                raise OutOfRange()
            self.r = r
        elif h == "VOLT":
            v = num(arg, ("V",))
            if not 0 <= v <= 240.0:
                raise OutOfRange()
            self.v = v
        elif h == "CURR:PROT":
            i = num(arg, ("A",))
            if not 0 <= i <= 10.2:
                raise OutOfRange()
            self.ocp = i
        elif h == "CURR:PROT:DEL":
            d = num(arg, ("S",))
            if not 0 <= d <= 60:
                raise OutOfRange()
            self.ocp_del = d
        elif h == "CURR:PROT:STAT":
            self.ocp_on = onoff(arg)
        elif h == "INP:PROT:CLE":
            self.latched = 0
        elif h == "*SAV":
            self.slots[self._slot(arg)] = (self.inp, self.mode, self.i, self.i_rng, self.r, self.r_rng,
                                           self.v, self.ocp, self.ocp_del, self.ocp_on)
        elif h == "*RCL":
            s = self.slots[self._slot(arg)]
            if s is not None:
                (self.inp, self.mode, self.i, self.i_rng, self.r, self.r_rng,
                 self.v, self.ocp, self.ocp_del, self.ocp_on) = s
        else:
            raise Undefined()

    @staticmethod
    def _slot(arg):
        try:
            n = int(float(arg))
        except (TypeError, ValueError):
            raise OutOfRange() from None
        if n not in range(0, 7):
            raise OutOfRange()
        if n == 0:                                  # never sent by the driver; refuse loudly
            raise OutOfRange("slot 0 is the power-on state")
        return n
