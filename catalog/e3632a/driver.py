#!/usr/bin/env python3
"""Agilent/HP E3632A DC power supply -- 15 V/7 A or 30 V/4 A, 120 W.

Every command here was taken from the E3632-90001 User's Guide (October 2007),
read on its page, not from memory and not copied from the E3641A plugin.

WHAT IS DIFFERENT FROM THE E3641A
---------------------------------
1. Ranges: LOW = P15V (15 V / 7 A), HIGH = P30V (30 V / 4 A)  -- Table 4-1, p. 80.
2. It has OVER-CURRENT protection as well as over-voltage (p. 84). Programmable
   OVP 1-32 V, OCP 0-7.5 A (p. 159). *RST: OVP 32 V on, OCP 7.5 A on (p. 94).
3. Three saved setups, not five, and no names (*SAV / *RCL {1|2|3}, p. 95).
   A setup INCLUDES OUTP, so Save is refused with the output on and Recall needs
   confirmation -- the same rule as the E3641A, for the same reason.
4. STAT:QUES:COND? 1 = CC, 2 = CV, 0 = output off / unregulated, 3 = FAILURE
   (p. 108). 3 is reported as mode "FAIL", never folded into CC or CV.
5. SYST:LOC / SYST:REM / SYST:RWL are RS-232 only (p. 99); over GPIB the core
   releases with device_local, as for the E3641A.

READBACK PLAUSIBILITY  (readback_ok)
------------------------------------
MEAS:VOLT? / MEAS:CURR? come from the supply's own ADC. A faulty ADC does not
raise an error -- one unit on this bench returned the same -3.45 mV / 11.96 mA at
every operating point while delivering several amps, with *TST? passing. So the
driver checks readback against what the regulation mode GUARANTEES:
  * in CV the voltage at the sense terminals IS the setpoint;
  * in CC the output current IS the current limit.
If the relevant reading is further off than READBACK_TOL allows, readback_ok is
False and the card says so. With the output off, or the mode unknown, there is
nothing to check against and readback_ok is None -- unknown, not "fine".
The tolerance is deliberately loose (several times the readback spec, 0.05% +
5 mV / 0.15% + 5 mA): it is there to catch a broken ADC, not to calibrate one.
"""
from benchcore.instrument import Instrument

SLOTS = (1, 2, 3)

RANGES = {
    "LOW":  {"scpi": "P15V", "vmax": 15.0, "imax": 7.0},
    "HIGH": {"scpi": "P30V", "vmax": 30.0, "imax": 4.0},
}
OVP_MIN, OVP_MAX = 1.0, 32.0        # p. 159
OCP_MIN, OCP_MAX = 0.0, 7.5         # p. 159

# (fraction of setting, floor) -- see READBACK PLAUSIBILITY above.
READBACK_TOL = {"v": (0.01, 0.05), "i": (0.02, 0.05)}


class E3632A(Instrument):
    def __init__(self, link, log):
        super().__init__(link, log)
        self.state = {
            "v_set": None, "i_set": None, "v_meas": None, "i_meas": None,
            "readback_ok": None,
            "output": None, "range": None,
            "ovp": None, "ovp_state": None, "ovp_tripped": None,
            "ocp": None, "ocp_state": None, "ocp_tripped": None,
            "cc": None, "mode": None, "last_error": None,
        }

    # -- polling ------------------------------------------------------------
    def poll(self):
        with self.lock:
            s = self.state
            s["v_meas"] = _f(self.link.query("MEAS:VOLT?"))
            s["i_meas"] = _f(self.link.query("MEAS:CURR?"))
            s["v_set"] = _f(self.link.query("VOLT?"))
            s["i_set"] = _f(self.link.query("CURR?"))
            s["output"] = _b(self.link.query("OUTP?"))
            s["range"] = _range_name(self.link.query("VOLT:RANG?"))
            s["ovp"] = _f(self.link.query("VOLT:PROT?"))
            s["ovp_state"] = _b(self.link.query("VOLT:PROT:STAT?"))
            s["ovp_tripped"] = _b(self.link.query("VOLT:PROT:TRIP?"))
            s["ocp"] = _f(self.link.query("CURR:PROT?"))
            s["ocp_state"] = _b(self.link.query("CURR:PROT:STAT?"))
            s["ocp_tripped"] = _b(self.link.query("CURR:PROT:TRIP?"))
            # p. 108: 0 = off/unregulated, 1 = CC, 2 = CV, 3 = failure.
            qs = self.link.query("STAT:QUES:COND?")
            try:
                bits = int(qs) if qs is not None else None
            except (TypeError, ValueError):
                bits = None
            if bits is None:
                s["cc"], s["mode"] = None, None
            else:
                s["mode"] = {1: "CC", 2: "CV", 3: "FAIL"}.get(bits & 0x03)
                s["cc"] = s["mode"] == "CC"
            s["readback_ok"] = self._readback_ok()
            return self.status()

    def _readback_ok(self):
        s = self.state
        if not s["output"] or s["mode"] not in ("CV", "CC"):
            return None
        if s["mode"] == "CV":
            meas, want, (frac, floor) = s["v_meas"], s["v_set"], READBACK_TOL["v"]
        else:
            meas, want, (frac, floor) = s["i_meas"], s["i_set"], READBACK_TOL["i"]
        if meas is None or want is None:
            return None
        return abs(meas - want) <= frac * abs(want) + floor

    def status(self):
        st = self.base_state()
        st.update(self.state)
        limits = RANGES.get(self.state.get("range") or "LOW", RANGES["LOW"])
        st["vmax"], st["imax"] = limits["vmax"], limits["imax"]
        return st

    # -- actions ------------------------------------------------------------
    def action(self, name, args):
        with self.lock:
            if name == "set_v":
                return self._clamped("VOLT", float(args["value"]), "vmax")
            if name == "set_i":
                return self._clamped("CURR", float(args["value"]), "imax")
            if name == "set_range":
                r = str(args["value"]).upper()
                if r not in RANGES:
                    return {"ok": False, "msg": f"unknown range {r}"}
                return {"ok": self.link.write(f"VOLT:RANG {RANGES[r]['scpi']}")}
            if name == "output":
                on = bool(args["value"])
                if on and not args.get("confirmed"):
                    return {"ok": False, "msg": "output enable needs confirmation"}
                return {"ok": self.link.write("OUTP ON" if on else "OUTP OFF")}
            if name == "set_ovp":
                return self._level("VOLT:PROT", float(args["value"]), OVP_MIN, OVP_MAX, "V", "OVP")
            if name == "ovp_state":
                return {"ok": self.link.write("VOLT:PROT:STAT " + ("ON" if args["value"] else "OFF"))}
            if name == "ovp_clear":
                return {"ok": self.link.write("VOLT:PROT:CLE")}
            if name == "set_ocp":
                return self._level("CURR:PROT", float(args["value"]), OCP_MIN, OCP_MAX, "A", "OCP")
            if name == "ocp_state":
                return {"ok": self.link.write("CURR:PROT:STAT " + ("ON" if args["value"] else "OFF"))}
            if name == "ocp_clear":
                return {"ok": self.link.write("CURR:PROT:CLE")}
            if name == "clear_errors":
                return {"ok": True, "errors": self.drain_errors()}
            if name == "save_state":
                return self._save(args)
            if name == "recall_state":
                return self._recall(args)
            return {"ok": False, "msg": f"unknown action {name}"}

    def _level(self, scpi, value, lo, hi, unit, what):
        if not lo <= value <= hi:
            return {"ok": False, "msg": f"{what} {value:g} {unit} is outside {lo:g}-{hi:g} {unit}"}
        return {"ok": self.link.write(f"{scpi} {value:.3f}")}

    # -- saved setups -------------------------------------------------------
    @staticmethod
    def _slot(args):
        try:
            n = int(args.get("slot"))
        except (TypeError, ValueError):
            return None
        return n if n in SLOTS else None

    def _save(self, args):
        n = self._slot(args)
        if n is None:
            return {"ok": False, "msg": "slot must be 1-3"}
        out = self.link.query("OUTP?")           # fresh, not the last poll
        if out is None:
            return {"ok": False, "msg": "could not read the output state"}
        if out.strip() in ("1", "ON"):
            return {"ok": False, "msg": "Switch the output OFF before saving -- a setup "
                    "saved with the output on switches it ON when recalled."}
        if not self.link.write(f"*SAV {n}"):
            return {"ok": False, "msg": "*SAV failed"}
        errs = self.drain_errors()
        if errs:
            return {"ok": False, "msg": " | ".join(errs)}
        return {"ok": True, "msg": f"saved to {n}"}

    def _recall(self, args):
        n = self._slot(args)
        if n is None:
            return {"ok": False, "msg": "slot must be 1-3"}
        if not args.get("confirmed"):
            return {"ok": False, "msg": "recall needs confirmation -- a saved setup "
                    "can switch the output on"}
        if not self.link.write(f"*RCL {n}"):
            return {"ok": False, "msg": "*RCL failed"}
        errs = self.drain_errors()
        self.poll()
        if errs:
            return {"ok": False, "msg": " | ".join(errs)}
        if self.state.get("output"):
            return {"ok": True, "msg": f"Recalled {n} -- the OUTPUT IS ON "
                    "(that setup was saved with it on)."}
        return {"ok": True, "msg": f"Recalled {n}."}

    def _clamped(self, scpi, value, limit_key):
        """Refuse a setpoint beyond the selected range's RATING rather than let the
        supply clamp it silently (it would accept up to 15.45 V / 7.21 A etc.)."""
        # The range the supply is in NOW, read fresh -- not the last poll's.
        rng = _range_name(self.link.query("VOLT:RANG?"))
        if rng not in RANGES:
            return {"ok": False, "msg": "could not read the output range"}
        cap = RANGES[rng][limit_key]
        if value < 0 or value > cap:
            return {"ok": False, "msg": f"{value:g} exceeds {cap:g} on the {rng} range"}
        return {"ok": self.link.write(f"{scpi} {value:.4f}")}

    def all_off(self):
        return self.link.write("OUTP OFF")


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _b(v):
    return (v.strip() in ("1", "ON")) if v else None


def _range_name(raw):
    if not raw:
        return None
    r = raw.strip().upper()
    if "30" in r or r == "HIGH":
        return "HIGH"
    if "15" in r or r == "LOW":
        return "LOW"
    return r
