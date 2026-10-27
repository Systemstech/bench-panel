#!/usr/bin/env python3
"""Keysight E3641A DC power supply -- 35 V/0.8 A or 60 V/0.5 A, 30 W.

Every command here was taken from the E3640-90001 User's Guide, not from memory.

THE THREE THINGS THIS INSTRUMENT MADE ME LOOK UP
------------------------------------------------
1. `SYSTem:LOCal`, `SYSTem:REMote` and `SYSTem:RWLock` are **RS-232 only**. Over
   GPIB they queue +514 "Command allowed only with RS-232" -- verified on the real
   instrument 2026-09-22. Release is device_local, handled by Link.go_local().
2. Range keywords are `P35V | P60V | LOW | HIGH`; for this model LOW = P35V and
   HIGH = P60V. The ranges are NOT independent limits -- selecting HIGH caps
   current at 0.5 A, not 0.8 A.
3. The eBay listing said "60V, 2.5A". It is 0.5 A. 60 x 2.5 = 150 W on a 30 W
   supply. The bezel and the manual agree with each other and not with the seller.

SAVED SETUPS 1-5 (*SAV / *RCL), added 2026-09-22
------------------------------------------------
The supply powers up in its *RST state every time (0 V, 0.8 A, OVP 66 V, output
OFF -- manual Table 3-4); there is no battery and no power-on recall. What it does
keep is five setups in non-volatile memory. The manual's list of what a setup
stores INCLUDES `OUTP` -- so recalling a setup saved with the output on turns the
output ON. Hence:
  * the panel refuses to SAVE while the output is on, so a setup saved from here
    can never switch the output on when recalled;
  * RECALL is treated like OUTPUT ON: it needs the confirmation flag, because a
    setup saved from the FRONT PANEL may still hold output-on, and the panel cannot
    see inside a slot without recalling it.
Slot names (MEM:STAT:NAME, up to 9 characters) are what the front panel's
Recall menu shows; the panel reads and writes the same names.
"""
import re

from benchcore.instrument import Instrument

SLOTS = (1, 2, 3, 4, 5)
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_]{0,8}$")

RANGES = {
    "LOW":  {"scpi": "P35V", "vmax": 35.0, "imax": 0.8},
    "HIGH": {"scpi": "P60V", "vmax": 60.0, "imax": 0.5},
}


class E3641A(Instrument):
    def __init__(self, link, log):
        super().__init__(link, log)
        self.state = {
            "v_set": None, "i_set": None, "v_meas": None, "i_meas": None,
            "output": None, "range": None, "ovp": None, "ovp_state": None,
            "ovp_tripped": None, "cc": None, "mode": None, "last_error": None,
            "slot_names": None,     # the five MEM:STAT:NAME? strings
        }

    def on_connect(self):
        self._read_slot_names()

    def _read_slot_names(self):
        names = []
        for n in SLOTS:
            raw = self.link.query(f"MEM:STAT:NAME? {n}")
            names.append(raw.strip().strip('"').strip("'").strip() if raw else "")
        self.state["slot_names"] = names

    # -- polling ------------------------------------------------------------
    def poll(self):
        """One pass. Measured values every time; settings are cheap enough here
        that reading them back keeps the panel honest if someone turns a knob."""
        with self.lock:
            s = self.state
            v = self.link.query("MEAS:VOLT?")
            i = self.link.query("MEAS:CURR?")
            s["v_meas"] = _f(v)
            s["i_meas"] = _f(i)
            s["v_set"] = _f(self.link.query("VOLT?"))
            s["i_set"] = _f(self.link.query("CURR?"))
            out = self.link.query("OUTP?")
            s["output"] = (out.strip() in ("1", "ON")) if out else None
            rng = self.link.query("VOLT:RANG?")
            s["range"] = _range_name(rng)
            s["ovp"] = _f(self.link.query("VOLT:PROT?"))
            st = self.link.query("VOLT:PROT:STAT?")
            s["ovp_state"] = (st.strip() in ("1", "ON")) if st else None
            tr = self.link.query("VOLT:PROT:TRIP?")
            s["ovp_tripped"] = (tr.strip() in ("1", "ON")) if tr else None
            # CC vs CV, and the bits are the opposite way round from the obvious
            # reading. Manual Table 3-5: the register reports what is UNREGULATED.
            #   bit 0 (dec 1) "Voltage"  -> voltage unregulated -> CONSTANT CURRENT
            #   bit 1 (dec 2) "Current"  -> current unregulated -> CONSTANT VOLTAGE
            # Testing bit 1 for "CC" reads exactly backwards and makes an unloaded
            # supply claim CC. Verified against the manual, not inferred.
            qs = self.link.query("STAT:QUES:COND?")
            try:
                bits = int(qs) if qs is not None else None
            except (TypeError, ValueError):
                bits = None
            if bits is None:
                s["cc"], s["mode"] = None, None
            else:
                s["cc"] = bool(bits & 0x01)
                # With the output off neither bit is set; calling that "CV" would
                # be asserting a regulation mode the supply is not in.
                s["mode"] = ("CC" if bits & 0x01 else
                             "CV" if bits & 0x02 else None)
            return self.status()

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
                # Changing range can clip the existing setpoints; the supply does
                # that itself, and the next poll will show what it actually took.
                ok = self.link.write(f"VOLT:RANG {RANGES[r]['scpi']}")
                return {"ok": ok}
            if name == "output":
                on = bool(args["value"])
                if on and not args.get("confirmed"):
                    return {"ok": False, "msg": "output enable needs confirmation"}
                return {"ok": self.link.write("OUTP ON" if on else "OUTP OFF")}
            if name == "set_ovp":
                return {"ok": self.link.write(f"VOLT:PROT {float(args['value']):.3f}")}
            if name == "ovp_state":
                return {"ok": self.link.write(
                    "VOLT:PROT:STAT " + ("ON" if args["value"] else "OFF"))}
            if name == "ovp_clear":
                return {"ok": self.link.write("VOLT:PROT:CLE")}
            if name == "clear_errors":
                return {"ok": True, "errors": self.drain_errors()}
            if name == "save_state":
                return self._save(args)
            if name == "recall_state":
                return self._recall(args)
            return {"ok": False, "msg": f"unknown action {name}"}

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
            return {"ok": False, "msg": "slot must be 1-5"}
        # Read the output state fresh rather than trusting the last poll.
        out = self.link.query("OUTP?")
        if out is None:
            return {"ok": False, "msg": "could not read the output state"}
        if out.strip() in ("1", "ON"):
            return {"ok": False, "msg": "Switch the output OFF before saving -- a setup "
                    "saved with the output on switches it ON when recalled."}
        name = str(args.get("name") or "").strip()
        if name and not _NAME_RE.match(name):
            return {"ok": False, "msg": "name: up to 9 letters, digits or _, "
                    "starting with a letter or digit"}
        if not self.link.write(f"*SAV {n}"):
            return {"ok": False, "msg": "*SAV failed"}
        if name:
            self.link.write(f"MEM:STAT:NAME {n},'{name}'")
        errs = self.drain_errors()
        self._read_slot_names()
        if errs:
            return {"ok": False, "msg": " | ".join(errs)}
        return {"ok": True, "msg": f"saved to {n}" + (f" as {name}" if name else "")}

    def _recall(self, args):
        n = self._slot(args)
        if n is None:
            return {"ok": False, "msg": "slot must be 1-5"}
        if not args.get("confirmed"):
            return {"ok": False, "msg": "recall needs confirmation -- a saved setup "
                    "can switch the output on"}
        if not self.link.write(f"*RCL {n}"):
            return {"ok": False, "msg": "*RCL failed"}
        errs = self.drain_errors()
        self.poll()                         # show what it actually recalled
        if errs:
            return {"ok": False, "msg": " | ".join(errs)}
        if self.state.get("output"):
            return {"ok": True, "msg": f"Recalled {n} -- the OUTPUT IS ON "
                    "(that setup was saved with it on)."}
        return {"ok": True, "msg": f"Recalled {n}."}

    def _clamped(self, scpi, value, limit_key):
        """Refuse a setpoint the selected range cannot deliver.

        The supply would clamp it silently and report a value you did not ask for.
        Saying no is more useful than quietly doing something else.
        """
        # The range the supply is in NOW, read fresh. The last poll's range is stale
        # right after a set_range: psu_dmm_check restores range then voltage back
        # to back, and a supply found on HIGH above 35 V was refused -- silently,
        # because a restore ignores errors (fixed 1.2.2).
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


def _range_name(raw):
    if not raw:
        return None
    r = raw.strip().upper()
    if "60" in r or r == "HIGH":
        return "HIGH"
    if "35" in r or r == "LOW":
        return "LOW"
    return r
