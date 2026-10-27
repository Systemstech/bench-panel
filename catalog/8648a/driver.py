#!/usr/bin/env python3
"""HP 8648A signal generator -- 100 kHz to 1000 MHz, -136 to +13 dBm.

Commands here are the ones already proven against this instrument by
`bench_remote/attribute_4db.py` and `bench_remote/state_8648a.py`, not guesses:
`FREQ:CW`, `POW:AMPL`, `OUTP:STAT`, `POW:REF`, `POW:REF:STAT`, `POW:ATT:AUTO`.

THE ONE THING THAT MAKES THIS INSTRUMENT DANGEROUS TO PUT ON A PANEL
--------------------------------------------------------------------
**It has a REFERENCE MODE, and it is currently ON with +5.00 dBm stored.**

In reference mode the amplitude you enter is a DELTA from a stored reference:

    actual output  =  entered value  +  POW:REF?

The reference lives in NON-VOLATILE memory, so it survives power cycles. On
2026-09-15 this cost a full day: a sweep was logged, every point came out ~4.25 dB
"high", and the error was attributed to the generator being out of specification.
It was not. The generator is inside its +/-1.0 dB spec; the 4.25 dB was 5.00 dB of
reference mode minus 0.74 dB of real, in-spec error. Proven 2026-09-22 with the RF
output off: POW:AMPL? read -60.00 dB with reference mode on and -55.00 dBm with it
off, exactly the predicted -60 + 5.

So this panel does two things about it:

1. **The number you type is the ABSOLUTE level in dBm.** If reference mode is on,
   the panel subtracts the reference before sending, so what is on screen is what
   comes out of the connector. `set_ampl` is the only place that conversion
   happens, and the entered/delta value is still shown beside it so nothing is
   hidden.
2. **Reference mode is READ-ONLY here.** The panel will not write it. On the front
   panel, pressing REF captures the *current* setting as the new reference -- so a
   bare `POW:REF:STAT ON` could silently overwrite Ernesto's +5.00 with whatever
   happened to be set. Until that SCPI behaviour is tested on the real instrument,
   a panel has no business writing a non-volatile setting it cannot guarantee to
   put back. Toggle it at the front panel, or with
   `bench_remote/confirm_8648a_ref.py`, which records a baseline and verifies the
   restore.

RF OUTPUT = has_output. Output ON needs confirmation and the safe shutdown turns it
off, same as the supply.
"""
from benchcore.instrument import Instrument

FREQ_MIN_HZ, FREQ_MAX_HZ = 100e3, 1000e6
AMPL_MIN_DBM, AMPL_MAX_DBM = -136.0, 13.0


class HP8648A(Instrument):
    def __init__(self, link, log):
        super().__init__(link, log)
        self.state = {
            "freq_hz": None,
            "ampl_entered": None,   # what the instrument holds -- a DELTA if ref_on
            "ampl_abs": None,       # what actually comes out of the connector
            "ref_on": None,
            "ref_dbm": None,
            "att_auto": None,
            "output": None,
            "mod": None,            # list of modulations that are ON
            "warn": None,
            "last_error": None,
        }

    # -- polling ------------------------------------------------------------
    def poll(self):
        with self.lock:
            s = self.state
            s["freq_hz"] = _f(self.link.query("FREQ:CW?"))
            s["ampl_entered"] = _f(self.link.query("POW:AMPL?"))
            s["ref_on"] = _b(self.link.query("POW:REF:STAT?"))
            s["ref_dbm"] = _f(self.link.query("POW:REF?"))
            s["att_auto"] = _b(self.link.query("POW:ATT:AUTO?"))
            s["output"] = _b(self.link.query("OUTP:STAT?"))

            # Modulation changes what a power meter reads. Worth showing even
            # though this panel does not set it.
            on = []
            for scpi, name in (("AM:STAT?", "AM"), ("FM:STAT?", "FM"),
                               ("PM:STAT?", "PM"), ("PULM:STAT?", "PULSE")):
                if _b(self.link.query(scpi)):
                    on.append(name)
            s["mod"] = on

            s["ampl_abs"] = self._absolute(s["ampl_entered"])
            s["warn"] = self._warning()
            return self.status()

    def _absolute(self, entered):
        """Entered value -> what leaves the connector."""
        if entered is None:
            return None
        if self.state.get("ref_on") and self.state.get("ref_dbm") is not None:
            return entered + self.state["ref_dbm"]
        return entered

    def _entered_for(self, absolute):
        """What leaves the connector -> the value to send."""
        if self.state.get("ref_on") and self.state.get("ref_dbm") is not None:
            return absolute - self.state["ref_dbm"]
        return absolute

    def _warning(self):
        bits = []
        if self.state.get("ref_on"):
            r = self.state.get("ref_dbm")
            bits.append(
                "REFERENCE MODE ON (%s dBm stored) — the level this instrument "
                "holds is a DELTA; the panel shows and sets the absolute."
                % ("%+.2f" % r if r is not None else "?"))
        if self.state.get("att_auto") is False:
            bits.append("ATTENUATOR HELD (POW:ATT:AUTO is OFF) — level steps "
                        "may not track the setting.")
        if self.state.get("mod"):
            bits.append("MODULATION ON: " + ", ".join(self.state["mod"]) +
                        " — a power meter will not read the carrier alone.")
        return " ".join(bits) or None

    def status(self):
        st = self.base_state()
        st.update(self.state)
        st["ampl_min"], st["ampl_max"] = AMPL_MIN_DBM, AMPL_MAX_DBM
        st["freq_min_hz"], st["freq_max_hz"] = FREQ_MIN_HZ, FREQ_MAX_HZ
        return st

    # -- actions ------------------------------------------------------------
    def action(self, name, args):
        with self.lock:
            if name == "set_freq":
                mhz = float(args["value"])
                hz = mhz * 1e6
                if not (FREQ_MIN_HZ <= hz <= FREQ_MAX_HZ):
                    return {"ok": False,
                            "msg": "%g MHz is outside 0.1–1000 MHz" % mhz}
                return {"ok": self.link.write("FREQ:CW %.6f MHZ" % mhz)}

            if name == "set_ampl":
                # The value is the ABSOLUTE level wanted at the connector.
                absolute = float(args["value"])
                if not (AMPL_MIN_DBM <= absolute <= AMPL_MAX_DBM):
                    return {"ok": False,
                            "msg": "%+g dBm is outside %+g..%+g dBm"
                                   % (absolute, AMPL_MIN_DBM, AMPL_MAX_DBM)}
                send = self._entered_for(absolute)
                ok = self.link.write("POW:AMPL %.2f DBM" % send)
                msg = None
                if self.state.get("ref_on"):
                    msg = ("reference mode on: sent %+.2f so the output is %+.2f dBm"
                           % (send, absolute))
                return {"ok": ok, "msg": msg}

            if name == "output":
                on = bool(args["value"])
                if on and not args.get("confirmed"):
                    return {"ok": False, "msg": "RF output enable needs confirmation"}
                return {"ok": self.link.write(
                    "OUTP:STAT ON" if on else "OUTP:STAT OFF")}

            if name == "clear_errors":
                return {"ok": True, "errors": self.drain_errors()}

            if name in ("ref_state", "set_ref"):
                # Deliberate refusal -- see the module docstring.
                return {"ok": False,
                        "msg": "reference mode is read-only here: writing it can "
                               "overwrite the stored reference. Use the front "
                               "panel or confirm_8648a_ref.py."}

            return {"ok": False, "msg": "unknown action %s" % name}

    def all_off(self):
        return self.link.write("OUTP:STAT OFF")


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _b(v):
    if v is None:
        return None
    return v.strip().upper() in ("1", "ON", "+1")
