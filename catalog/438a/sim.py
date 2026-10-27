#!/usr/bin/env python3
"""Simulator model of the HP 438A power meter -- for the /sim/ bench. Not a driver.

The 438A speaks HP-IB program codes run together ("CSAEZE", "AEKB97.9EN"), not
SCPI, so this model replaces SimModel.handle() with its own parser. It knows the
codes driver.py sends; anything else is entry error 91 "invalid HP-IB code"
(Table 3-8, p. 3-59), recorded so the S21 check sees it.

PORTS
  a, b   sensor inputs, 50 ohm. An 8481A is on A at power-on (this bench's setup
         since 2026-09-18); B is empty. Wire an RF source to them:
         "8648a-1:rf" -> "438a-1:a".
  ref    POWER REF, 1.00 mW at 50 MHz while the oscillator is on. For CAL ADJ wire
         "438a-1:ref" -> "438a-1:a".

WHAT IS FROM THE MANUAL: the codes, the Status Message layout (pp. 3-40, 3-41),
the learn string's order (Table 3-3), the error codes (Table 3-8), 9.0000E+40
while in error (p. 3-35), auto-filter per range (p. 3-60), the 8481A's ranges
(1-10 uW ... 10-100 mW, p. 3-72), cal factor / offset / REL arithmetic, "any
command aborts ZERO or CAL" (p. 3-49), Cal/Zero Complete = status bit 1 (p. 3-45),
silence in trigger hold (p. 3-33), ?ID -> HP438A,VER5.00 (this bench's unit, 09-14).

THE MODEL'S OWN CHOICES, not from the manual: ZERO and CAL finish after 1.5 s
(the real ones took 10.1 s and 4.0 s); a 0.05 % reading noise; "cannot zero"
above 1 uW; PLEASE ZERO below -50 nW; a linear ratio sent as a percentage
(100 = equal -- the manual does not say, see the ratio-units-unverified quirk).
"""
import math
import re
import time

from benchcore.sim import NoReply, SimModel, noisy

REF_W = 1.0e-3
ZERO_TIME_S = CAL_TIME_S = 1.5          # the model's own (real: 10.1 s / 4.0 s)
MODE_CODES = {"AP": 0, "BP": 1, "AR": 2, "BR": 3, "AD": 4, "BD": 5}
MODE_OF = {v: k for k, v in MODE_CODES.items()}
AUTO_FILTER = {1: 7, 2: 3, 3: 1, 4: 0, 5: 0}           # p. 3-60
NUMERIC = ("KB", "OS", "RM", "FM", "CL")                 # codes followed by <number>EN
DIGIT = ("TR", "RL", "OC", "LP")                         # codes followed by one digit
PLAIN = ("AP", "BP", "AR", "BR", "AD", "BD", "AE", "BE", "LN", "LG",
         "RA", "RH", "FA", "FH", "CS", "ZE", "SM")
_NUM = re.compile(r"\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*(EN|%)", re.I)


def _top(k):
    """Top of range k for an 8481A: range 1 = 1-10 uW ... range 5 = 10-100 mW."""
    return 10.0 ** (k - 6)


class HP438ASim(SimModel):
    INPUTS = {"a": "sensor A (8481A fitted)", "b": "sensor B (nothing fitted)"}
    OUTPUTS = {"ref": "POWER REF 1.00 mW, 50 MHz"}
    PRESETS = {
        "uncalibrated-a": "channel A reads +0.79 dB high until CAL ADJ (as found 2026-09-18)",
        "needs-zero": "channel A's zero has drifted negative: PLEASE ZERO, error 15",
        "no-sensors": "nothing on either channel: error 31 (the documented power-up pass state)",
        "sensor-on-b": "the 8481A is on channel B instead of A (as on 2026-09-15)",
        "osc-left-on": "someone left POWER REF on",
        "trigger-hold": "left in trigger hold by another program: answers nothing, not even ?ID",
    }

    def idn(self):
        return "HP438A,VER5.00"

    def power_on_reset(self):
        self.sensor = {"A": "8481A", "B": None}
        self.gain = {"A": 1.0, "B": 1.0}        # after CAL ADJ, the meter's gain
        self.sens = {"A": 1.0, "B": 1.0}        # the uncalibrated error CAL ADJ removes
        self.zero = {"A": 0.0, "B": 0.0}        # W subtracted by the last ZERO
        self.cf = {"A": 100.0, "B": 100.0}
        self.ofs = {"A": 0.0, "B": 0.0}
        self.rng = {"A": 0, "B": 0}             # 0 = auto, else manual 1-5
        self.auto_rng = {"A": 1, "B": 1}
        self.flt = {"A": None, "B": None}       # None = auto, else manual 0-9
        self.mode, self.entry, self.log_units = 0, "A", False
        self.osc = self.rel = False
        self.rel_ref = None
        self.hold = False
        self.meas_err = self.entry_err = 0
        self.status_byte = 0
        self.busy = None                        # ("zero"|"cal", channel, t_done, ref_cf)
        self.op_err = 0                         # a failed ZERO/CAL, latched (p. 3-56)

    def apply_preset(self, name):
        super().apply_preset(name)
        if name == "uncalibrated-a":
            self.sens["A"] = 10 ** (0.79 / 10)
        elif name == "needs-zero":
            self.zero["A"] = 3e-7
        elif name == "no-sensors":
            self.sensor = {"A": None, "B": None}
        elif name == "sensor-on-b":
            self.sensor = {"A": None, "B": "8481A"}
            self.mode, self.entry = 1, "B"
        elif name == "osc-left-on":
            self.osc = True
        elif name == "trigger-hold":
            self.hold = True

    # -- ports ----------------------------------------------------------------------
    def z_in(self, port):
        return 50.0

    def source(self, port, z_load):
        on = self.osc or (self.busy is not None and self.busy[0] == "cal")
        if port != "ref" or not on:
            return None
        return {"dc": 0.0, "vrms": math.sqrt(REF_W * 50.0), "freq": 50e6, "wave": "SIN"}

    def _applied(self, ch):
        sig = self.signal_into(ch.lower())
        return 0.0 if not sig or not sig.get("freq") else sig["vrms"] ** 2 / 50.0

    # -- the bus ------------------------------------------------------------------------
    def stb(self):
        self._tick()
        v, self.status_byte = self.status_byte, 0      # read clears it (p. 3-45)
        return v

    def talk(self):
        self._tick()
        if self.hold or self.busy:
            raise NoReply("trigger hold / zero or cal running: no data output")
        if self.op_err:
            # A failed ZERO / CAL is on the display: 9.0000E+40 goes out, which
            # latches the error until the Status Message is read (pp. 3-55, 3-56).
            self.meas_err, self.op_err = self.op_err, 0
            self._latched = True
            return "+9.0000E+40"
        v = self._reading()
        return "+9.0000E+40" if v is None else f"{v:+.4E}"

    def handle(self, line):
        self._tick()
        reply = self._parse(line)
        if reply is not None and self.hold:
            # In hold the meter still LISTENS; it just outputs nothing (p. 3-33).
            raise NoReply("trigger hold: no data output")
        return reply

    def _parse(self, line):
        s = re.sub(r"\s+", "", line)
        reply, i = None, 0
        while i < len(s):
            self.last_cmd = s[i:]
            if self.busy:                       # any command aborts ZERO / CAL (p. 3-49)
                self.busy = None
            if s.startswith("?ID", i):
                reply, i = self.idn(), i + 3
                continue
            c = s[i:i + 2].upper()
            if c in DIGIT and i + 2 < len(s) and s[i + 2].isdigit():
                d = int(s[i + 2])
                i += 3
                if c == "TR" and d == 3:
                    self.hold = False
                elif c == "RL" and d in (0, 1):
                    self.rel, self.rel_ref = bool(d), None
                elif c == "OC" and d in (0, 1):
                    self.osc = bool(d)
                elif c == "LP" and d == 1:
                    reply = self._learn()
                else:
                    return self._bad(f"{c}{d}")
            elif c in NUMERIC:
                m = _NUM.match(s, i + 2)
                if not m:
                    return self._bad(c + " without <number>EN")
                i = m.end()
                if not self._numeric(c, float(m.group(1))):
                    return reply
            elif c in PLAIN:
                i += 2
                reply = self._plain(c) or reply
            else:
                return self._bad(s[i:i + 3])
        return reply

    def _bad(self, what):
        self.entry_err = 91                      # Table 3-8, p. 3-59
        self.error(91, f"Invalid HP-IB code {what}")
        return None

    def _plain(self, c):
        e = self.entry
        if c in MODE_CODES:
            self.mode, self.rel, self.rel_ref = MODE_CODES[c], False, None
            self.entry = "B" if c[0] == "B" else "A"
        elif c in ("AE", "BE"):
            self.entry = c[0]
        elif c in ("LN", "LG"):
            self.log_units = c == "LG"
        elif c == "RA":
            self.rng[e] = 0
        elif c == "RH":
            self.rng[e] = self.rng[e] or self.auto_rng[e]
        elif c == "FA":
            self.flt[e] = None
        elif c == "FH":
            self.flt[e] = self.flt[e] if self.flt[e] is not None else AUTO_FILTER[self.auto_rng[e]]
        elif c == "CS":
            self.status_byte = 0
        elif c == "ZE":
            self.busy = ("zero", e, time.time() + ZERO_TIME_S, None)
        elif c == "SM":
            out = self._status_message()
            self.entry_err = 0                   # latched until SM is read (p. 3-55)
            self._latched = False
            return out
        return None

    def _numeric(self, c, v):
        e = self.entry
        limits = {"KB": (1.0, 150.0, 50), "OS": (-99.99, 99.99, 51), "RM": (1, 5, 52),
                  "FM": (0, 9, 53), "CL": (50.0, 120.0, 56)}
        lo, hi, code = limits[c]
        if not lo <= v <= hi:
            self.entry_err = code
            self.error(code, f"{c} {v:g} out of range")
            return False
        if c == "KB":
            self.cf[e] = round(v, 1)
        elif c == "OS":
            self.ofs[e] = round(v, 2)
        elif c == "RM":
            self.rng[e] = int(v)
        elif c == "FM":
            self.flt[e] = int(v)
        elif c == "CL":
            self.busy = ("cal", e, time.time() + CAL_TIME_S, v)
        return True

    # -- zero and cal finishing on their own --------------------------------------------
    def _tick(self):
        if not self.busy or time.time() < self.busy[2]:
            return
        what, ch, _, ref_cf = self.busy
        self.busy = None
        p = self._applied(ch)
        fail = 0
        if self.sensor[ch] is None:
            fail = 31 if ch == "A" else 32
        elif what == "zero":
            if p > 1e-6:                          # the model's own threshold
                fail = 1 if ch == "A" else 2
            else:
                self.zero[ch] = p
        else:
            raw = self.sens[ch] * (p - self.zero[ch])
            if p < 0.5 * REF_W:                   # not on the reference (the meter checks)
                fail = 3 if ch == "A" else 4
            else:
                self.gain[ch] = REF_W * (ref_cf / 100.0) / raw
        self.op_err = fail
        # Cal/Zero Complete (p. 3-45). Whether a FAILED cycle also sets it, the manual
        # does not say; the model sets it plus Measurement Error (8). The driver copes
        # either way: without bit 1 it times out and reads the error code.
        self.status_byte |= 2 | (8 if fail else 0)

    # -- what the meter measures ----------------------------------------------------------
    def _power(self, ch):
        """(watts after cal factor and offset, error code) for one channel."""
        if self.sensor[ch] is None:
            return None, 31 if ch == "A" else 32
        p = self._applied(ch)
        raw = self.sens[ch] * (p - self.zero[ch]) * self.gain[ch]
        raw = noisy(raw, abs(raw) * 5e-4 + 5e-9)
        if raw < -5e-8:
            return None, 15 if ch == "A" else 16
        if p > 1.2 * _top(5):
            return None, 11 if ch == "A" else 12
        k = next((k for k in range(1, 6) if p <= 1.2 * _top(k)), 5)
        self.auto_rng[ch] = k
        if self.rng[ch] and p > 1.2 * _top(self.rng[ch]):
            return None, 17 if ch == "A" else 18
        return raw * 100.0 / self.cf[ch] * 10 ** (self.ofs[ch] / 10), 0

    def _reading(self):
        m = MODE_OF[self.mode]
        a, ea = self._power("A") if m != "BP" else (None, 0)
        b, eb = self._power("B") if m != "AP" else (None, 0)
        err = ea or eb
        if err:
            self.meas_err = err
            return None
        if m in ("AP", "BP"):
            v = a if m == "AP" else b
        elif m in ("AR", "BR"):
            num, den = (a, b) if m == "AR" else (b, a)
            v = num / den if den else None           # a zero denominator is error 27 (p. 3-81)
        else:
            v = a - b if m == "AD" else b - a
        ratio = m in ("AR", "BR")
        if v is None:
            self.meas_err = 27
            return None
        if self.rel:
            if self.rel_ref is None:
                self.rel_ref = v
            v = v / self.rel_ref if self.rel_ref else None
            ratio = True
            if v is None:
                self.meas_err = 28
                return None
        self.meas_err = 0
        if self.log_units:
            if v <= 0:
                self.meas_err = 27
                return None
            return 10 * math.log10(v) if ratio else 10 * math.log10(v / 1e-3)
        return v * 100.0 if ratio else v

    def _status_message(self):
        """Figure 3-6, p. 3-40, with the codes on p. 3-41. It reports the LAST
        measurement ("updated only after a measurement", p. 3-38), never a new one."""
        mode = self.mode
        if self.busy:
            mode = {"zero": 6, "cal": 8}[self.busy[0]] + (self.busy[1] == "B")
        rng = lambda ch: self.rng[ch] or 10 + self.auto_rng[ch]              # noqa: E731
        flt = lambda ch: (self.flt[ch] if self.flt[ch] is not None           # noqa: E731
                          else 10 + AUTO_FILTER[self.rng[ch] or self.auto_rng[ch]])
        return (f"{self.meas_err:02d}{self.entry_err:02d}{mode:02d}"
                f"{rng('A'):02d}{rng('B'):02d}{flt('A'):02d}{flt('B'):02d}"
                f"{int(self.log_units)}{self.entry}{int(self.osc)}{int(self.rel)}"
                f"{int(self.hold)}2000")

    def _learn(self):
        """Learn Mode #1 in Table 3-3's order (p. 3-37)."""
        def block(ch):
            r = "RA" if not self.rng[ch] else f"RM{self.rng[ch]}EN"
            f = "FA" if self.flt[ch] is None else f"FM{self.flt[ch]}EN"
            return (f"KB{self.cf[ch]:05.1f}EN OS{self.ofs[ch]:05.2f}EN {r} {f} "
                    f"LL+000.000EN HL+000.000EN")
        mode = MODE_OF[self.mode]
        return (f"TR3{mode}AE{block('A')}BE{block('B')}{self.entry}E"
                f"{'LG' if self.log_units else 'LN'}OC{int(self.osc)}GT2LM0")
