#!/usr/bin/env python3
"""HP 438A dual-channel RF power meter -- HP-IB program codes, NOT SCPI.

Source, every code read on its page: HP 438A Power Meter Operating and Service
Manual, 00438-90028 (1986-04-01). Page references below are the PRINTED page
(section 3 printed 3-N is PDF page N + 30). The code table is Table 3-7, p. 3-47;
the zero-vs-letter-O question (RL0, OC0, TR0, LM0 end in the digit zero) was
settled from the page image, not the OCR.

WHAT IS DIFFERENT ABOUT THIS INSTRUMENT
---------------------------------------
* It predates IEEE 488.2: no *IDN?, no *OPC?, no SYST:ERR?. It identifies with
  ?ID -> "HP438A,VERX.XX" (p. 3-40). The manifest's "identify" and "keepalive"
  tell the core so. Codes run together with no separators; every numeric entry
  ends in EN (p. 3-31), e.g. "AEKB97.9EN".
* A reading is not a query reply. In free run the meter sends its latest
  measurement whenever it is addressed to talk (pp. 3-34, 3-35), so poll() takes
  it with link.read() and sends nothing first. In trigger HOLD it sends nothing
  at all except the serial-poll status byte (p. 3-33), which is why this driver
  never leaves free run.
* 9.0000E+40 is not a power. "As long as the front panel display indicates an
  error condition, the Power Meter sends 9.0000E+40 as the measured data" (p.
  3-35). It becomes reading = null; the error code comes from the Status Message.
* ZERO and CAL ADJ take seconds (10.1 s and 4.0 s on this bench, 2026-09-18) and
  "any command received during the calibration process aborts the calibration"
  (p. 3-49). While one runs the driver sends NOTHING but serial polls, and waits
  for status-byte bit 1, Cal/Zero Complete (weight 2, p. 3-45).

THINGS THIS PLUGIN DELIBERATELY NEVER SENDS
-------------------------------------------
* PR (PRESET), and so never a Device Clear either -- the same thing (p. 3-70).
  Preset sets both cal factors to 100% and both offsets to 0 dB (p. 3-69): the
  next reading is silently wrong by the sensor's cal factor. It does not undo the
  calibration itself (p. 3-70).
* RC (RECALL): a stored setup includes the reference oscillator's state (p. 3-84),
  so a recall can switch the POWER REF on.
* ST, limits (LL/LH/LM), display (DA/DD/DE), TR0-TR2, GT, @1: not needed yet; the
  core refuses anything undeclared.

THE OUTPUT
----------
POWER REF (1.00 mW, 50 MHz, front panel) is an RF output. OC1 needs confirmation;
all_off() sends OC0. CAL ADJ turns the reference on by itself for the calibration
and puts it back as it was (p. 3-50) -- that is the meter, not this driver.
"""
import re
import time

from benchcore.instrument import Instrument

ERROR_SENTINEL = 9.0e40                  # p. 3-35
MODES = {"A": "AP", "B": "BP", "A/B": "AR", "B/A": "BR", "A-B": "AD", "B-A": "BD"}
# Status Message operating mode BB (p. 3-41).
SM_MODES = {0: "A", 1: "B", 2: "A/B", 3: "B/A", 4: "A-B", 5: "B-A",
            6: "zeroing A", 7: "zeroing B", 8: "cal A", 9: "cal B",
            10: "ext cal A", 11: "ext cal B"}
RATIO = ("A/B", "B/A")
CF_MIN, CF_MAX = 1.0, 150.0              # p. 3-51, error 50 p. 3-59
REF_CF_MIN, REF_CF_MAX = 50.0, 120.0     # p. 3-48, error 56 p. 3-59
OFS_MIN, OFS_MAX = -99.99, 99.99         # p. 3-66, error 51 p. 3-59
RANGES = ("auto", "hold", "1", "2", "3", "4", "5")                               # p. 3-71
FILTERS = ("auto", "hold", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9")     # p. 3-61
STB_CAL_ZERO_DONE = 2                    # p. 3-45, Table 3-6
BUSY_LIMIT_S = 60.0                      # the model's own margin: 10.1 s was measured

# Table 3-8, pp. 3-57 to 3-59. Measurement errors 01-49, entry errors 50-59 and 90-99.
ERRORS = {
    1: "cannot zero sensor A: is RF applied to it?",
    2: "cannot zero sensor B: is RF applied to it?",
    3: "sensor A is not on the reference oscillator during CAL",
    4: "sensor B is not on the reference oscillator during CAL",
    5: "cannot calibrate sensor A: the reference must be 1 mW",
    6: "cannot calibrate sensor B: the reference must be 1 mW",
    11: "input overload on sensor A",
    12: "input overload on sensor B",
    15: "sensor A's zero has drifted negative: ZERO A",
    16: "sensor B's zero has drifted negative: ZERO B",
    17: "sensor A: power too high for the manual range",
    18: "sensor B: power too high for the manual range",
    25: "overflow: input power, offset, cal factor or mode",
    26: "underflow: input power, offset, cal factor or mode",
    27: "illegal log operation: zero or negative power in dBm/dB",
    28: "REL has no valid reference value",
    31: "no sensor on channel A",
    32: "no sensor on channel B",
    33: "sensors on both front and rear A inputs",
    34: "sensors on both front and rear B inputs",
    50: "cal factor out of range (1.0-150.0)",
    51: "offset out of range (-99.99 to +99.99)",
    52: "range number out of range (1-5)",
    53: "filter number out of range (0-9)",
    54: "recall register out of range (0-19)",
    55: "store register out of range (1-19)",
    56: "reference cal factor out of range (50.0-120.0)",
    57: "continuous memory lost: the meter is in PRESET",
    58: "HP-IB address out of range",
    90: "HP-IB data without a valid prefix",
    91: "invalid HP-IB code",
}


class HP438A(Instrument):
    def __init__(self, link, log):
        super().__init__(link, log)
        self._busy = None                # {"what", "t0"} while ZERO / CAL ADJ runs
        self.state = {
            "reading": None, "unit": None,
            "meas_error": None, "meas_error_text": None,
            "entry_error": None, "entry_error_text": None,
            "mode": None, "units": None, "entry_channel": None,
            "osc": None, "rel": None, "trigger": None,
            "sensor_a": _blank_sensor(), "sensor_b": _blank_sensor(),
            "busy": None, "last_op": None, "status_raw": None,
        }

    # -- connect ------------------------------------------------------------
    def on_connect(self):
        # Nothing is changed here, so on_release has nothing to put back. A meter in
        # trigger HOLD would not have answered ?ID at all (p. 3-33), so free run is
        # what we find; TR3 is sent only if the Status Message says otherwise.
        sm = _decode_sm(self.link.query("SM"))
        if sm and sm["trigger"] != "free run":
            self.link.write("TR3")                       # p. 3-34

    # -- polling ------------------------------------------------------------
    def poll(self):
        with self.lock:
            if self._busy:
                return self._poll_busy()
            return self._poll_normal()

    def _poll_normal(self):
        s = self.state
        # Reading FIRST: "the Status Message is updated only after a measurement"
        # (p. 3-38), and HP's own example reads one before asking for SM (p. 3-39).
        raw = _f(self.link.read())
        sm = _decode_sm(self.link.query("SM"))
        learn = _decode_learn(self.link.query("LP1"))
        s["reading"] = None if raw is None or abs(raw) >= ERROR_SENTINEL else raw
        sentinel = raw is not None and abs(raw) >= ERROR_SENTINEL
        s["status_raw"] = sm["raw"] if sm else None
        if sm:
            s["meas_error"] = sm["meas_error"] or None
            s["entry_error"] = sm["entry_error"] or None
            s["meas_error_text"] = _err_text(sm["meas_error"]) or (
                "the meter sent 9.0000E+40 (its error display) but the Status Message "
                "shows no code" if sentinel else None)
            s["entry_error_text"] = _err_text(sm["entry_error"])
            for k in ("mode", "units", "entry_channel", "osc", "rel", "trigger"):
                s[k] = sm[k]
            for ch in ("a", "b"):
                s["sensor_" + ch].update(sm["sensor_" + ch])
            s["unit"] = _unit(sm["mode"], sm["units"], sm["rel"])
        else:
            for k in ("meas_error", "entry_error", "meas_error_text", "entry_error_text",
                      "mode", "units", "entry_channel", "osc", "rel", "trigger", "unit"):
                s[k] = None
        for ch in ("a", "b"):
            got = (learn or {}).get(ch) or {}
            s["sensor_" + ch]["cal_factor"] = got.get("cal_factor")
            s["sensor_" + ch]["offset"] = got.get("offset")
        # Should the Status Message be unreadable, the learn string still knows the
        # oscillator; an ON there must never be hidden behind a null.
        if s["osc"] is None and learn and learn.get("osc") is not None:
            s["osc"] = learn["osc"]
        s["busy"] = None
        return self.status()

    def _poll_busy(self):
        """ZERO / CAL ADJ running: serial polls only -- a command would abort it."""
        s, b = self.state, self._busy
        stb = self.link.read_stb()
        elapsed = time.time() - b["t0"]
        b["stb"] = b.get("stb", 0) | (stb or 0)
        done = stb is not None and stb & STB_CAL_ZERO_DONE
        if done or elapsed > BUSY_LIMIT_S:
            # Over, or given up on: either way it is safe to talk again, and the
            # meter's own error code says more than the status byte can. Whether a
            # FAILED zero sets Cal/Zero Complete at all, the manual does not say.
            self._busy = None
            self._poll_normal()
            code = s["meas_error"]
            if code in (1, 2, 3, 4, 5, 6, 31, 32):
                s["last_op"] = (f"{b['what']} FAILED after {elapsed:.1f} s: error {code:02d}, "
                                f"{ERRORS.get(code)}")
            elif done:
                s["last_op"] = f"{b['what']} done in {elapsed:.1f} s"
            else:
                s["last_op"] = (f"{b['what']}: Cal/Zero Complete not seen in {BUSY_LIMIT_S:.0f} s"
                                + (" (a measurement error was flagged)" if b["stb"] & 8 else "")
                                + " -- check the meter's display before trusting its readings")
            return self.status()
        s["busy"] = f"{b['what']} ({elapsed:.0f} s)"
        return self.status()

    def status(self):
        st = self.base_state()
        st.update(self.state)
        return st

    # -- actions ------------------------------------------------------------
    def action(self, name, args):
        with self.lock:
            if name in ACTIONS and self._busy:
                return {"ok": False, "msg": f"{self._busy['what']} is running and any command "
                        f"aborts it (manual p. 3-49) -- wait for it to finish"}
            if name == "measure":
                m = str(args.get("value", "")).upper()
                if m not in MODES:
                    return {"ok": False, "msg": f"mode must be one of {', '.join(MODES)}"}
                return {"ok": self.link.write(MODES[m])}                 # pp. 3-76, 3-78, 3-80
            if name == "units":
                u = str(args.get("value", ""))
                if u not in ("W", "dBm"):
                    return {"ok": False, "msg": "units are W (linear) or dBm (log)"}
                return {"ok": self.link.write("LN" if u == "W" else "LG")}   # p. 3-54
            if name == "cal_factor":
                ch, err = _channel(args)
                v, err = (None, err) if err else _num(args, CF_MIN, CF_MAX, "cal factor", "%")
                if err:
                    return {"ok": False, "msg": err}
                return {"ok": self.link.write(f"{ch}EKB{v:.1f}EN")}      # p. 3-51
            if name == "offset":
                ch, err = _channel(args)
                v, err = (None, err) if err else _num(args, OFS_MIN, OFS_MAX, "offset", "dB")
                if err:
                    return {"ok": False, "msg": err}
                return {"ok": self.link.write(f"{ch}EOS{v:.2f}EN")}      # p. 3-66
            if name == "range":
                ch, err = _channel(args)
                r = str(args.get("value", "")).lower()
                if err or r not in RANGES:
                    return {"ok": False, "msg": err or f"range is one of {', '.join(RANGES)}"}
                code = {"auto": "RA", "hold": "RH"}.get(r, f"RM{r}EN")   # p. 3-72
                return {"ok": self.link.write(f"{ch}E{code}")}
            if name == "filter":
                ch, err = _channel(args)
                f = str(args.get("value", "")).lower()
                if err or f not in FILTERS:
                    return {"ok": False, "msg": err or f"filter is one of {', '.join(FILTERS)}"}
                code = {"auto": "FA", "hold": "FH"}.get(f, f"FM{f}EN")   # p. 3-62
                return {"ok": self.link.write(f"{ch}E{code}")}
            if name == "rel":
                return {"ok": self.link.write("RL1" if args.get("value") else "RL0")}   # p. 3-74
            if name == "osc":
                on = bool(args.get("value"))
                if on and not args.get("confirmed"):
                    return {"ok": False, "msg": "POWER REF on needs confirmation"}
                return {"ok": self.link.write("OC1" if on else "OC0")}  # p. 3-47
            if name == "zero":
                if not args.get("confirmed"):
                    return {"ok": False, "msg": "ZERO needs confirmation: no RF may be on the sensor"}
                ch, err = _channel(args)
                if err:
                    return {"ok": False, "msg": err}
                # CS first, so bit 1 of the status byte can only mean THIS zero (p. 3-45).
                return self._start(f"CS{ch}EZE", f"zero {ch}")            # p. 3-86
            if name == "cal_adj":
                if not args.get("confirmed"):
                    return {"ok": False, "msg": "CAL ADJ needs confirmation: the sensor must be "
                            "on POWER REF, and it replaces the channel's calibration"}
                ch, err = _channel(args)
                v, err = (None, err) if err else _num(args, REF_CF_MIN, REF_CF_MAX,
                                                      "reference cal factor", "%", key="ref_cf")
                if err:
                    return {"ok": False, "msg": err}
                return self._start(f"CS{ch}ECL{v:.1f}EN", f"cal {ch}")   # pp. 3-48, 3-49
            return {"ok": False, "msg": f"unknown action {name!r}"}

    def _start(self, codes, what):
        if not self.link.write(codes):
            return {"ok": False, "msg": f"{what} was not accepted by the bus"}
        self._busy = {"what": what, "t0": time.time()}
        self.state["busy"] = what
        # The meter drives POWER REF itself now: off while zeroing (p. 3-87), on for
        # a CAL (p. 3-50). Nothing is read until it ends, so the state is unknown.
        self.state["osc"] = None
        self.state["last_op"] = None
        return {"ok": True, "msg": f"{what} started -- the card shows when it finishes"}

    def all_off(self):
        # Aborts a ZERO or CAL ADJ in progress (p. 3-49): safety comes first.
        self._busy = None
        return self.link.write("OC0")                                    # p. 3-47

    def reset_state(self):
        super().reset_state()
        self._busy = None


ACTIONS = ("measure", "units", "cal_factor", "offset", "range", "filter",
           "rel", "osc", "zero", "cal_adj")


def _blank_sensor():
    return {"range": None, "range_auto": None, "filter": None, "filter_auto": None,
            "cal_factor": None, "offset": None}


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _channel(args):
    ch = str(args.get("channel", "")).upper()
    return (ch, None) if ch in ("A", "B") else (None, "channel is A or B")


def _num(args, lo, hi, what, unit, key="value"):
    v = _f(args.get(key))
    if v is None or not lo <= v <= hi:
        return None, f"{what} must be {lo:g} to {hi:g} {unit}"
    return v, None


def _err_text(code):
    return None if not code else ERRORS.get(code, f"error {code:02d} (not in Table 3-8)")


def _unit(mode, units, rel):
    """What the number on the bus means (the table on p. 3-53)."""
    if mode is None or units is None or mode.startswith(("zero", "cal", "ext")):
        return None
    if rel or mode in RATIO:
        return "dB" if units == "dBm" else "%"
    return units


def _decode_sm(text):
    """The 23-character Status Message, Figure 3-6 p. 3-40 and its codes p. 3-41:
    AA meas error, aa entry error, BB mode, CC/cc range A/B (01-05 manual, 11-15
    auto), DD/dd filter A/B (00-09 manual, 10-19 auto), E units (0 W, 1 dBm),
    F entry channel, G osc, H rel, I trigger (0 free run, 1 hold), J group
    trigger, K limits on, L/M limits status A/B."""
    t = (text or "").strip()
    if len(t) != 23 or not (t[:15].isdigit() and t[15] in "AB" and t[16:].isdigit()):
        return None
    n = lambda i: int(t[i:i + 2])                                        # noqa: E731
    mode = SM_MODES.get(n(4))
    if mode is None:
        return None

    def sensor(r, f):
        return {"range": r % 10 or None, "range_auto": r >= 10,
                "filter": f % 10, "filter_auto": f >= 10}
    return {"raw": t, "meas_error": n(0), "entry_error": n(2), "mode": mode,
            "sensor_a": sensor(n(6), n(10)), "sensor_b": sensor(n(8), n(12)),
            "units": "dBm" if t[14] == "1" else "W", "entry_channel": t[15],
            "osc": t[16] == "1", "rel": t[17] == "1",
            "trigger": {"0": "free run", "1": "hold"}.get(t[18], t[18])}


_KB = re.compile(r"KB([-+]?\d+(?:\.\d*)?)EN")
_OS = re.compile(r"OS([-+]?\d+(?:\.\d*)?)EN")


def _decode_learn(text):
    """Learn Mode #1 (Table 3-3, p. 3-37): TRx, the measurement mode, then AE +
    sensor A's CAL FACTOR / OFFSET / range / filter / limits, BE + sensor B's, then
    the entry channel, LG|LN, OC0|OC1, GTx, LM0|LM1. Only the cal factors, offsets
    and oscillator are taken from it -- the Status Message has the rest."""
    t = re.sub(r"\s+", "", text or "")
    a = t.find("AE")
    b = t.find("BE", a + 2) if a >= 0 else -1
    if a < 0 or b < 0:
        return None
    end = min([i for i in (t.find("AE", b + 2), t.find("BE", b + 2)) if i >= 0] or [len(t)])
    out = {}
    for ch, block in (("a", t[a + 2:b]), ("b", t[b + 2:end])):
        kb, os_ = _KB.search(block), _OS.search(block)
        out[ch] = {"cal_factor": float(kb.group(1)) if kb else None,
                   "offset": float(os_.group(1)) if os_ else None}
    osc = re.search(r"OC([01])", t[end:])
    out["osc"] = None if not osc else osc.group(1) == "1"
    return out
