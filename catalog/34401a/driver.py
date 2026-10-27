#!/usr/bin/env python3
"""HP/Agilent 34401A 6.5-digit multimeter.

WHAT "FAST" ACTUALLY MEANS, AND WHY THE PANEL SAYS SO
-----------------------------------------------------
The reference UI has a "Set Fast" button. On this meter that is NPLC -- an
INTEGRATION TIME, so it is an accuracy control wearing a speed label:

    NPLC 10   ~ 6.5 digits   ~0.6 rdg/s    best noise rejection
    NPLC 1    ~ 6.5 digits   ~6  rdg/s
    NPLC 0.2  ~ 5.5 digits
    NPLC 0.02 ~ 4.5 digits   hundreds/s    line noise no longer rejected

Two ceilings sit above that and neither is the meter:
  * one query per reading over VXI-11 across a VLAN tops out around 10-30 rdg/s.
    Past that you stop asking per reading and use the meter's own buffer
    (CONF -> TRIG:COUN n -> INIT -> FETCH?), which this panel does not do yet.
  * the browser polls a few times a second. Nothing faster is visible anyway.

So the panel offers NPLC directly and labels the trade-off, rather than a
"fast/slow" switch that hides which one you are actually giving up.

Note SYST:LOC is RS-232-only here too (+514 over GPIB); release is device_local.
"""
from benchcore.instrument import Instrument

# label -> (CONF command, unit, whether a fixed range list applies)
FUNCTIONS = {
    "DCV":    ("CONF:VOLT:DC",  "VDC",  "VOLT:DC"),
    "ACV":    ("CONF:VOLT:AC",  "VAC",  "VOLT:AC"),
    "DCI":    ("CONF:CURR:DC",  "ADC",  "CURR:DC"),
    "ACI":    ("CONF:CURR:AC",  "AAC",  "CURR:AC"),
    "2W OHM": ("CONF:RES",      "OHM",  "RES"),
    "4W OHM": ("CONF:FRES",     "OHM",  "FRES"),
    "FREQ":   ("CONF:FREQ",     "Hz",   None),
    "PERIOD": ("CONF:PER",      "s",    None),
    "DIODE":  ("CONF:DIOD",     "V",    None),
    "CONT":   ("CONF:CONT",     "OHM",  None),
}
NPLC_CHOICES = [0.02, 0.2, 1, 10, 100]


class Dmm34401A(Instrument):
    def __init__(self, link, log):
        super().__init__(link, log)
        self.state = {
            "reading": None, "unit": "VDC", "function": "DCV",
            "range": "AUTO", "nplc": None,
            "min": None, "max": None, "avg": None, "samples": 0,
            "last_error": None,
        }
        self._sum = 0.0

    def on_connect(self):
        # Start from a known state rather than inheriting whatever the last
        # script left behind -- verify_1787b.py is known to leave it pinned
        # to a manual range. Runs only after *IDN? has named a 34401A.
        self.link.write("CONF:VOLT:DC")
        self.link.write("VOLT:DC:RANG:AUTO ON")
        self.link.write("TRIG:SOUR IMM")
        self._read_config()

    def reset_state(self):
        super().reset_state()
        self._sum = 0.0

    # -- polling ------------------------------------------------------------
    def poll(self):
        with self.lock:
            v = self.link.query("READ?")
            val = _f(v)
            s = self.state
            if val is not None:
                s["reading"] = val
                s["samples"] += 1
                self._sum += val
                s["min"] = val if s["min"] is None else min(s["min"], val)
                s["max"] = val if s["max"] is None else max(s["max"], val)
                s["avg"] = self._sum / s["samples"]
            return self.status()

    def _read_config(self):
        fn = FUNCTIONS[self.state["function"]][2]
        if fn:
            self.state["nplc"] = _f(self.link.query(f"{fn}:NPLC?"))
            rng = self.link.query(f"{fn}:RANG:AUTO?")
            if rng is not None and rng.strip() in ("1", "ON"):
                self.state["range"] = "AUTO"
            else:
                r = self.link.query(f"{fn}:RANG?")
                self.state["range"] = r.strip() if r else "?"
        else:
            self.state["nplc"] = None
            self.state["range"] = "n/a"

    def status(self):
        st = self.base_state()
        st.update(self.state)
        st["functions"] = list(FUNCTIONS)
        st["nplc_choices"] = NPLC_CHOICES
        return st

    # -- actions ------------------------------------------------------------
    def action(self, name, args):
        with self.lock:
            if name == "function":
                f = args["value"]
                if f not in FUNCTIONS:
                    return {"ok": False, "msg": f"unknown function {f}"}
                conf, unit, _ = FUNCTIONS[f]
                if not self.link.write(conf):
                    return {"ok": False}
                self.link.write("TRIG:SOUR IMM")
                self.state.update(function=f, unit=unit, range="AUTO")
                self.reset_stats()
                self._read_config()
                return {"ok": True}
            if name == "range":
                fn = FUNCTIONS[self.state["function"]][2]
                if not fn:
                    return {"ok": False, "msg": "range not settable for this function"}
                v = str(args["value"]).upper()
                ok = (self.link.write(f"{fn}:RANG:AUTO ON") if v == "AUTO"
                      else self.link.write(f"{fn}:RANG {v}"))
                self._read_config()
                self.reset_stats()
                return {"ok": ok}
            if name == "nplc":
                fn = FUNCTIONS[self.state["function"]][2]
                if not fn:
                    return {"ok": False, "msg": "NPLC not settable for this function"}
                ok = self.link.write(f"{fn}:NPLC {float(args['value'])}")
                self._read_config()
                self.reset_stats()
                return {"ok": ok}
            if name == "reset_stats":
                self.reset_stats()
                return {"ok": True}
            if name == "clear_errors":
                return {"ok": True, "errors": self.drain_errors()}
            return {"ok": False, "msg": f"unknown action {name}"}

    def reset_stats(self):
        self._sum = 0.0
        self.state.update(min=None, max=None, avg=None, samples=0)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
