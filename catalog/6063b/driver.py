#!/usr/bin/env python3
"""HP/Agilent 6063B DC electronic load -- 240 V, 10 A, 250 W. CC, CR and CV.

Sources, each command read on its page:
  PROG = Agilent Electronic Load Family Programming Manual 06060-90005 (April 2000
         update) -- the HPSL language dictionary. It names no 6063B; it says its
         examples use the 6060A and are "valid for any electronic load".
  OPER = 6060B/6063B Operating Manual 5951-2826 (April 2000) -- the 6063B's ranges
         (Table 5-1, pp. 72-73) and defaults (Table 4-6, p. 63).
The same commands (INP, MODE?, CURR, CURR:PROT, MEAS) drove this bench's unit in
the 2026-09-15 commissioning sweep.

A LOAD IS AN OUTPUT
-------------------
"has_output" here means the INPUT: when it is on the load sinks current from
whatever is wired to it. INP ON needs confirmation; all_off() sends INP OFF.

THINGS THIS PLUGIN DELIBERATELY NEVER SENDS
-------------------------------------------
* *RST. OPER Table 4-6 lists the factory default input state as ON; PROG p. 51 says
  INP's *RST default is 0. The two manuals disagree, and a reset that might switch
  the input on is not a button.
* *SAV 0. Register 0 is the POWER-ON state (*RCL 0 runs at every power-up), and a
  power loss during Save 0 corrupts non-volatile memory and forces a recalibration
  (OPER p. 63). Only the volatile slots 1-6 are offered.
* INP:SHOR. A short is full-scale current in CC or minimum resistance in CR (PROG
  p. 50) -- the one control on this load that can hurt the thing under test.
* Transients / triggers. Not needed yet; the core would refuse them undeclared.

SAFETY RULES IN THE DRIVER
--------------------------
* Mode and range changes are refused while the input is ON. A mode change blinks
  the input off and back (PROG p. 52); a range change can move the level to the new
  range's maximum (PROG p. 46). Both can step the current into the thing under test.
* protection_clear needs confirmation: INP:PROT:CLE re-enables the input if it was
  on when protection tripped (PROG p. 50).
* Save is refused with the input on; a saved state includes the input state
  (OPER p. 62), and Recall needs confirmation.
* A protection shutdown does NOT change INP? -- it still reads 1 (PROG p. 45). The
  card therefore reads the status register (PROG pp. 85-86) and shows PS and why.

READINGS
--------
Readback is coarse: +/-0.1% +/-150 mV, 67 mV steps over GPIB; +/-0.12% +/-10 mA,
27 mA steps (OPER Tables 1-1/1-2). 9.9E+37 means over range (PROG p. 52) and is
reported as null with an overload flag, never as a number.
v_sense_suspect: sinking current (> 0.1 A) while reading < 0.1 V volts is what the
rear LCL/RMT sense switch on RMT with no sense leads looks like -- "voltage
readback will not work in any mode" (OPER p. 43). It happened on this bench.
"""
from benchcore.instrument import Instrument

SLOTS = (1, 2, 3, 4, 5, 6)
CURR_RANGES = {"LOW": {"scpi": "1", "max": 1.0}, "HIGH": {"scpi": "10", "max": 10.0}}
RES_RANGES = {"LOW": {"scpi": "24", "max": 24.0},
              "MID": {"scpi": "24000", "max": 24000.0},
              "HIGH": {"scpi": "240000", "max": 240000.0}}
VMAX = 240.0
OCP_MAX, OCP_DEL_MAX = 10.2, 60.0            # OPER Table 5-1, p. 73
MODES = {"CC": "CURR", "CR": "RES", "CV": "VOLT"}
OVERRANGE = 9.0e37

# Channel / questionable status bits, PROG pp. 85-86 (Table 5-1 / 5-2).
BITS = {"VF": 1, "OC": 2, "OP": 8, "OT": 16, "UNR": 1024, "RV": 2048, "OV": 4096, "PS": 8192}


class HP6063B(Instrument):
    def __init__(self, link, log):
        super().__init__(link, log)
        self.state = {
            "v_meas": None, "i_meas": None, "p_meas": None, "overload": None,
            "input": None, "mode": None,
            "i_set": None, "r_set": None, "v_set": None,
            "i_range": None, "r_range": None,
            "ocp": None, "ocp_delay": None, "ocp_state": None,
            "status": None, "faults": None, "shutdown": None, "unregulated": None,
            "v_sense_suspect": None, "last_error": None,
        }

    # -- polling ------------------------------------------------------------
    def poll(self):
        with self.lock:
            s = self.state
            v = _f(self.link.query("MEAS:VOLT?"))
            i = _f(self.link.query("MEAS:CURR?"))
            p = _f(self.link.query("MEAS:POW?"))
            over = any(x is not None and x > OVERRANGE for x in (v, i, p))
            s["v_meas"], s["i_meas"], s["p_meas"] = (
                None if x is None or x > OVERRANGE else x for x in (v, i, p))
            s["overload"] = over if None not in (v, i, p) else None
            s["input"] = _b(self.link.query("INP?"))
            m = (self.link.query("MODE?") or "").strip().upper()
            s["mode"] = next((k for k, w in MODES.items() if m.startswith(w)), None)
            s["i_set"] = _f(self.link.query("CURR?"))
            s["r_set"] = _f(self.link.query("RES?"))
            s["v_set"] = _f(self.link.query("VOLT?"))
            s["i_range"] = _curr_range(_f(self.link.query("CURR:RANG?")))
            s["r_range"] = _res_range(_f(self.link.query("RES:RANG?")))
            s["ocp"] = _f(self.link.query("CURR:PROT?"))
            s["ocp_delay"] = _f(self.link.query("CURR:PROT:DEL?"))
            s["ocp_state"] = _b(self.link.query("CURR:PROT:STAT?"))
            try:
                bits = int(float(self.link.query("STAT:QUES:COND?")))
            except (TypeError, ValueError):
                bits = None
            s["status"] = bits
            if bits is None:
                s["faults"] = s["shutdown"] = s["unregulated"] = None
            else:
                s["faults"] = [k for k, b in BITS.items() if bits & b and k not in ("PS", "UNR")]
                s["shutdown"] = bool(bits & BITS["PS"])
                s["unregulated"] = bool(bits & BITS["UNR"])
            s["v_sense_suspect"] = (None if not s["input"] or s["i_meas"] is None or s["v_meas"] is None
                                    else s["i_meas"] > 0.1 and s["v_meas"] < 0.1)
            return self.status()

    def status(self):
        st = self.base_state()
        st.update(self.state)
        return st

    # -- actions ------------------------------------------------------------
    def action(self, name, args):
        with self.lock:
            if name == "input":
                on = bool(args["value"])
                if on and not args.get("confirmed"):
                    return {"ok": False, "msg": "input on needs confirmation"}
                return {"ok": self.link.write("INP ON" if on else "INP OFF")}
            if name == "set_mode":
                m = str(args["value"]).upper()
                if m not in MODES:
                    return {"ok": False, "msg": f"mode must be one of {', '.join(MODES)}"}
                return self._while_off(f"MODE:{MODES[m]}", "changing mode")
            if name == "set_i_range":
                r = str(args["value"]).upper()
                if r not in CURR_RANGES:
                    return {"ok": False, "msg": "current range is LOW (1 A) or HIGH (10 A)"}
                return self._while_off(f"CURR:RANG {CURR_RANGES[r]['scpi']}", "changing the current range")
            if name == "set_r_range":
                r = str(args["value"]).upper()
                if r not in RES_RANGES:
                    return {"ok": False, "msg": "resistance range is LOW, MID or HIGH"}
                return self._while_off(f"RES:RANG {RES_RANGES[r]['scpi']}", "changing the resistance range")
            # Levels are checked against the range the load is in NOW, read fresh: a
            # script that changes range and then sets a level must not be judged
            # against the last poll's range.
            if name == "set_i":
                ir = _curr_range(_f(self.link.query("CURR:RANG?")))
                if ir is None:
                    return {"ok": False, "msg": "could not read the current range"}
                return self._level("CURR", float(args["value"]), 0.0, CURR_RANGES[ir]["max"], "A",
                                   f"the {ir} current range")
            if name == "set_r":
                rr = _res_range(_f(self.link.query("RES:RANG?")))
                if rr is None:
                    return {"ok": False, "msg": "could not read the resistance range"}
                lo = {"LOW": 0.0, "MID": 24.0, "HIGH": 240.0}[rr]
                return self._level("RES", float(args["value"]), lo, RES_RANGES[rr]["max"], "ohm",
                                   f"the {rr} resistance range")
            if name == "set_v":
                return self._level("VOLT", float(args["value"]), 0.0, VMAX, "V", "the 6063B")
            if name == "set_ocp":
                return self._level("CURR:PROT", float(args["value"]), 0.0, OCP_MAX, "A", "OCP")
            if name == "set_ocp_delay":
                return self._level("CURR:PROT:DEL", float(args["value"]), 0.0, OCP_DEL_MAX, "s", "the OCP delay")
            if name == "ocp_state":
                return {"ok": self.link.write("CURR:PROT:STAT " + ("ON" if args["value"] else "OFF"))}
            if name == "protection_clear":
                if not args.get("confirmed"):
                    return {"ok": False, "msg": "clearing protection needs confirmation -- it "
                            "switches the input back ON if it was on when it tripped"}
                return {"ok": self.link.write("INP:PROT:CLE")}
            if name == "clear_errors":
                return {"ok": True, "errors": self.drain_errors()}
            if name == "save_state":
                return self._save(args)
            if name == "recall_state":
                return self._recall(args)
            return {"ok": False, "msg": f"unknown action {name}"}

    def _input_is_on(self):
        """Fresh INP? -- None if it could not be read."""
        raw = self.link.query("INP?")
        return None if raw is None else raw.strip() in ("1", "ON")

    def _while_off(self, scpi, what):
        on = self._input_is_on()
        if on is None:
            return {"ok": False, "msg": "could not read the input state"}
        if on:
            return {"ok": False, "msg": f"Switch the input OFF before {what} -- it can step "
                    "the current into whatever is connected."}
        return {"ok": self.link.write(scpi)}

    def _level(self, scpi, value, lo, hi, unit, where):
        if not lo <= value <= hi:
            return {"ok": False, "msg": f"{value:g} {unit} is outside {lo:g}-{hi:g} {unit} on {where}"}
        return {"ok": self.link.write(f"{scpi} {value:.4g}")}

    # -- saved setups (volatile 1-6 only) -----------------------------------
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
            return {"ok": False, "msg": "slot must be 1-6 (0 is the power-on state and is never written here)"}
        on = self._input_is_on()
        if on is None:
            return {"ok": False, "msg": "could not read the input state"}
        if on:
            return {"ok": False, "msg": "Switch the input OFF before saving -- a state saved "
                    "with it on switches it ON when recalled."}
        if not self.link.write(f"*SAV {n}"):
            return {"ok": False, "msg": "*SAV failed"}
        errs = self.drain_errors()
        if errs:
            return {"ok": False, "msg": " | ".join(errs)}
        return {"ok": True, "msg": f"saved to {n} (slots 1-6 are lost at power-off)"}

    def _recall(self, args):
        n = self._slot(args)
        if n is None:
            return {"ok": False, "msg": "slot must be 1-6"}
        if not args.get("confirmed"):
            return {"ok": False, "msg": "recall needs confirmation -- a saved state can switch the input on"}
        if not self.link.write(f"*RCL {n}"):
            return {"ok": False, "msg": "*RCL failed"}
        errs = self.drain_errors()
        self.poll()
        if errs:
            return {"ok": False, "msg": " | ".join(errs)}
        if self.state.get("input"):
            return {"ok": True, "msg": f"Recalled {n} -- the INPUT IS ON (that state was saved with it on)."}
        return {"ok": True, "msg": f"Recalled {n}."}

    def all_off(self):
        return self.link.write("INP OFF")


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _b(v):
    return (v.strip() in ("1", "ON")) if v else None


def _curr_range(amps):
    if amps is None:
        return None
    return "LOW" if amps <= 1.5 else "HIGH"


def _res_range(ohms):
    if ohms is None:
        return None
    return "LOW" if ohms <= 30 else "MID" if ohms <= 30000 else "HIGH"
