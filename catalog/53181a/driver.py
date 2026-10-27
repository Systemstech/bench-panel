#!/usr/bin/env python3
"""HP/Agilent 53181A 225 MHz frequency counter (Channel 2 with Option 030).

Every command here is from the HP 53181A Programming Guide (53181-90002; serial
prefix 3711 and above) -- NOT the 53131A/132A guide, which is a different counter
and was, until 2026-09-23, the file filed under "53181" on the lab page.

THE ONE THING THAT MAKES A COUNTER DANGEROUS TO POLL
----------------------------------------------------
A counter measurement only completes when the input gives it edges. With no signal:

  * `:READ?` and `:MEASure?` wait for a gate that never closes. On 2026-09-10 that
    left the VISA session dead until an interface-level device_clear.
  * `:FETCh?` "issued while a measurement is in progress ... holds off further
    commands from being processed until the measurement completes. This hold-off
    action can only be canceled by the measurement completing, Device Clear, or
    power-on." (guide p. 4-55)

So this driver NEVER asks for a result it has not been told exists:

  on_connect   :INIT:CONT OFF (single-measurement mode; the operator's RUN/HOLD
               setting is recorded and put back by on_release)
  each poll    :INIT, *OPC   start one measurement, ask to be told when it ends
               *ESR?         bit 0 (Operation Complete) is set when the pending
                             operation -- the measurement -- has terminated
                             (guide p. 3-26). *ESR? never blocks.
               bit 0 set  -> :FETC? is now safe: the result exists
               still pending after gate + NO_SIGNAL_GRACE_S
                          -> :ABORt (ends the measurement, clears pending,
                             guide p. 4-4), read *ESR? to clear the OPC that causes,
                             report NO SIGNAL, start again next pass

Why not the Operation Status "Measuring" bit (bit 4)? Read on this unit 2026-09-23
in RUN mode it was CLEAR (:STAT:OPER:COND? = +512). Whether it is set while the
counter waits for a missing signal is not something the guide states, and guessing
wrong there is exactly the hang above. The pending-operation flag is defined
precisely, so that is what is used.

Voltage Peaks (MAX / MIN / PTP) is deliberately NOT offered. The sister 53131A/132A
guide (p. 4-78) documents :FETCh? hanging the bus in that function; the 53181A guide
says nothing either way, and nothing here needs it. Conservative, and marked as such.

CHANNELS
--------
Channel 1: 225 MHz, 50 ohm / 1 Mohm, AC / DC, x1 / x10, 100 kHz low-pass filter.
Channel 2 (Option 030): the guide gives only :INP2:COUP? and :INP2:IMP? -- query
only, it is fixed 50 ohm AC -- so the card shows it and offers nothing to set.
There is NO channel 3 on a 53181A: "FREQ 3" is error -224.

`:FUNC?` answers "FREQ" for channel 1 with no suffix, and "FREQ 2" for channel 2
(read on this unit 2026-09-10). The driver normalises every reply to one form.
"""
import re
import time

from benchcore.instrument import Instrument

# card value -> (FUNC string sent, unit)
FUNCS = {
    "FREQ 1":     ('"FREQ 1"', "Hz"),
    "FREQ 2":     ('"FREQ 2"', "Hz"),
    "PER 1":      ('"PER 1"', "s"),
    "PER 2":      ('"PER 2"', "s"),
    "RAT 1,2":    ('"FREQ:RAT 1,2"', "ratio"),
    "RAT 2,1":    ('"FREQ:RAT 2,1"', "ratio"),
}
GATE_MIN_S, GATE_MAX_S = 1e-3, 1000.0     # guide p. 4-75
NO_SIGNAL_GRACE_S = 1.0                   # beyond the gate time, before calling it no signal
SLOTS = range(1, 21)                      # *SAV 1-20 (guide command summary: "1 through 20"); *RCL also takes 0
NAN = 9.9e37                              # "Not a Number" is 9.91E37


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _b(v):
    return None if v is None else v.strip().strip('"') in ("1", "ON")


def norm_func(raw):
    """':FUNC?' reply -> a FUNCS key, or the raw text if it is something else."""
    if raw is None:
        return None
    s = raw.strip().strip('"').upper().replace(":VOLT", "").strip(":")
    s = re.sub(r"\s+", " ", s)
    m = re.match(r"^(FREQ:RAT|FREQ|PER)(?:\s+(\d)(?:\s*,\s*(\d))?)?$", s)
    if not m:
        return s
    kind, a, b = m.group(1), m.group(2) or "1", m.group(3)
    if kind == "FREQ:RAT":
        return f"RAT {a},{b or ('2' if a == '1' else '1')}"
    return f"{kind} {a}"


def display(value, unit):
    """A reading as the counter's own digits, in a sensible unit.

    The counter sends the resolution it measured to; this keeps exactly those
    significant digits rather than inventing or dropping any.
    """
    if value is None:
        return None, unit
    if unit == "Hz":
        for div, u in ((1e9, "GHz"), (1e6, "MHz"), (1e3, "kHz"), (1, "Hz")):
            if abs(value) >= div:
                return value / div, u
        return value, "Hz"
    if unit == "s":
        for mul, u in ((1, "s"), (1e3, "ms"), (1e6, "µs"), (1e9, "ns")):
            if abs(value) * mul >= 1:
                return value * mul, u
        return value * 1e12, "ps"
    return value, ""


class Hp53181A(Instrument):
    def __init__(self, link, log):
        super().__init__(link, log)
        self.state = {
            "function": None, "unit": None,
            "reading": None, "raw": None, "shown": None, "shown_unit": None,
            "status": None,            # "measuring" | "ok" | "no signal" | "no result"
            "reading_age_s": None,
            "gate_s": None,
            "ch1": {"imp": None, "coup": None, "att": None, "filter": None},
            "ch2": {"imp": None, "coup": None},
            "ref": None, "ref_auto": None,
            "min": None, "max": None, "avg": None, "samples": 0,
            "last_error": None,
        }
        self._baseline = {}
        self._started_at = None     # when the measurement in flight was initiated
        self._last_ok = None
        self._sum = 0.0

    # -- connect / release ---------------------------------------------------
    def on_connect(self):
        # Record what we change, so on_release can put it back.
        self._baseline = {"init_cont": self.link.query(":INIT:CONT?")}
        self.link.write(":INIT:CONT OFF")
        self.link.write(":ABOR")
        self.link.write("*CLS")
        self._read_config()
        self._started_at = None

    def on_release(self):
        self.link.write(":ABOR")
        if _b(self._baseline.get("init_cont")):
            # It was in RUN mode at the front panel: leave it measuring as found.
            self.link.write(":INIT:CONT ON")

    def reset_state(self):
        super().reset_state()
        self._sum, self._started_at, self._last_ok = 0.0, None, None

    def _read_config(self):
        s = self.state
        s["function"] = norm_func(self.link.query(":FUNC?"))
        s["unit"] = FUNCS.get(s["function"], (None, None))[1]
        s["gate_s"] = _f(self.link.query(":FREQ:ARM:STOP:TIM?"))
        c1 = s["ch1"]
        c1["imp"] = _f(self.link.query(":INP:IMP?"))
        c = self.link.query(":INP:COUP?")
        c1["coup"] = c.strip() if c else None
        c1["att"] = _f(self.link.query(":INP:ATT?"))
        c1["filter"] = _b(self.link.query(":INP:FILT?"))
        s["ch2"]["imp"] = _f(self.link.query(":INP2:IMP?"))
        c = self.link.query(":INP2:COUP?")
        s["ch2"]["coup"] = c.strip() if c else None
        self._read_ref()

    def _read_ref(self):
        r = self.link.query(":ROSC:SOUR?")
        self.state["ref"] = r.strip() if r else None
        self.state["ref_auto"] = _b(self.link.query(":ROSC:SOUR:AUTO?"))

    # -- measuring without ever blocking --------------------------------------
    def _start(self):
        self.link.write(":INIT")
        self.link.write("*OPC")
        self._started_at = time.time()

    def _abort(self):
        self.link.write(":ABOR")
        self.link.query("*ESR?")        # clear the Operation Complete the abort causes
        self._started_at = None

    def poll(self):
        with self.lock:
            s = self.state
            if self._started_at is None:
                self._start()
                # "no signal" stays until a reading actually arrives: re-arming after
                # an abort is not news, and flipping the label every second is noise.
                if s["status"] != "no signal":
                    s["status"] = "measuring"
            else:
                esr = _f(self.link.query("*ESR?"))
                if esr is not None and int(esr) & 1:
                    self._take(self.link.query(":FETC?"))
                    self._start()
                elif time.time() - self._started_at > (s["gate_s"] or 0.1) + NO_SIGNAL_GRACE_S:
                    self._abort()
                    s["status"] = "no signal"
                    s["reading"] = s["shown"] = s["raw"] = None
            self._read_ref()
            s["reading_age_s"] = None if self._last_ok is None else round(time.time() - self._last_ok, 1)
            return self.status()

    def _take(self, raw):
        s = self.state
        v = _f(raw)
        if v is None or abs(v) >= NAN:
            # 9.91E37 = "no valid result", with error -230 queued. Drain it.
            self.drain_errors()
            s["status"] = "no result"
            s["reading"] = s["shown"] = s["raw"] = None
            return
        s["status"], s["reading"], s["raw"] = "ok", v, raw.strip()
        s["shown"], s["shown_unit"] = display(v, s["unit"])
        self._last_ok = time.time()
        s["samples"] += 1
        self._sum += v
        s["min"] = v if s["min"] is None else min(s["min"], v)
        s["max"] = v if s["max"] is None else max(s["max"], v)
        s["avg"] = self._sum / s["samples"]

    def status(self):
        st = self.base_state()
        st.update(self.state)
        st["functions"] = list(FUNCS)
        st["slots"] = list(SLOTS)
        # How many significant digits the counter sent -- the card shows exactly those.
        raw = self.state.get("raw")
        st["digits"] = len(re.sub(r"[^0-9]", "", raw.split("E")[0].split("e")[0]).lstrip("0")) if raw else None
        return st

    # -- actions --------------------------------------------------------------
    def _reconfigure(self, *scpi):
        """Stop the measurement in flight, send the settings, start clean."""
        self._abort()
        for c in scpi:
            self.link.write(c)
        errs = self.drain_errors()
        self._read_config()
        self.reset_stats()
        self.state["reading"] = self.state["shown"] = self.state["raw"] = None
        self.state["status"] = "measuring"
        return {"ok": not errs, "msg": " | ".join(errs) if errs else None}

    def action(self, name, args):
        with self.lock:
            if name == "set_func":
                f = str(args.get("value", ""))
                if f not in FUNCS:
                    return {"ok": False, "msg": f"unknown function {f}"}
                return self._reconfigure(f":FUNC {FUNCS[f][0]}")
            if name == "set_gate":
                g = _f(args.get("value"))
                if g is None or not GATE_MIN_S <= g <= GATE_MAX_S:
                    return {"ok": False, "msg": f"gate must be {GATE_MIN_S:g}-{GATE_MAX_S:g} s"}
                return self._reconfigure(":FREQ:ARM:STAR:SOUR IMM", ":FREQ:ARM:STOP:SOUR TIM",
                                         f":FREQ:ARM:STOP:TIM {g:.5g}")
            if name == "set_imp":
                v = str(args.get("value"))
                if v not in ("50", "1E6"):
                    return {"ok": False, "msg": "impedance must be 50 or 1E6"}
                return self._reconfigure(f":INP:IMP {v}")
            if name == "set_coup":
                v = str(args.get("value", "")).upper()
                if v not in ("AC", "DC"):
                    return {"ok": False, "msg": "coupling must be AC or DC"}
                return self._reconfigure(f":INP:COUP {v}")
            if name == "set_att":
                v = str(args.get("value"))
                if v not in ("1", "10"):
                    return {"ok": False, "msg": "attenuation must be 1 or 10"}
                return self._reconfigure(f":INP:ATT {v}")
            if name == "set_filter":
                return self._reconfigure(":INP:FILT " + ("ON" if args.get("value") else "OFF"))
            if name == "reset_stats":
                self.reset_stats()
                return {"ok": True}
            if name == "clear_errors":
                return {"ok": True, "errors": self.drain_errors()}
            if name in ("save_state", "recall_state"):
                try:
                    n = int(args.get("slot"))
                except (TypeError, ValueError):
                    n = None
                if n not in SLOTS:
                    return {"ok": False, "msg": "slot must be 1-20"}
                if name == "save_state":
                    # Guide p. 2-7 (Save/Recall key map): stop the measurement before *SAV / *RCL.
                    r = self._reconfigure(f"*SAV {n}")
                    return {"ok": r["ok"], "msg": r["msg"] or f"Saved to {n}."}
                r = self._reconfigure(f"*RCL {n}", ":INIT:CONT OFF")
                return {"ok": r["ok"], "msg": r["msg"] or f"Recalled {n}."}
            return {"ok": False, "msg": f"unknown action {name}"}

    def reset_stats(self):
        self._sum = 0.0
        self.state.update(min=None, max=None, avg=None, samples=0)
