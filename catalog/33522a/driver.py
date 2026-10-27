#!/usr/bin/env python3
"""Agilent 33522A two-channel 30 MHz function / arbitrary waveform generator.

LAN-native. The 33522A speaks VXI-11 itself (device inst0), so the usual
connection is `vxi11` straight to its IP -- no GPIB gateway, no shared bus lock.
It also has a GPIB port and USB, hence `gpib-gateway` and `visa` in the manifest.
Front-panel release is device_local on VXI-11 paths -- there is no SYST:LOC in the
33500 command set.

Every command below was taken from the Agilent 33500 Series Operating and Service
Guide, not from memory.

SAVED STATES (*SAV / *RCL), added 2026-09-22
--------------------------------------------
* Locations 1-4 are the user's. Location 0 is the instrument's own power-down
  state; the panel never writes it.
* `MEM:STAT:VAL? n` says whether a location holds a state (the manual: "use this
  before sending *SAV to avoid accidentally overwriting a state"), so Save asks
  for confirmation only when it would overwrite something.
* The manual's list of what a state stores is function, frequency, amplitude,
  offset, duty cycle, symmetry, modulation and display -- OUTPUT ON/OFF IS NOT IN
  IT. Recall is still two-click: it can jump the amplitude on a live output.
* Recalling an empty location is an instrument error, so it is refused up front.
* `MEM:STAT:REC:AUTO` decides what power-on does: ON restores the power-down state,
  OFF means factory reset. Found OFF on this unit 2026-09-22. Outputs are always
  off at power-on either way ("to protect other equipment").

OUTPUT TERMINATION
------------------
`OUTP:LOAD` does not change the hardware; it tells the generator what load to
assume when it DISPLAYS amplitude. 50 ohm vs high-Z doubles or halves the shown
figure. The panel shows the setting next to every amplitude for that reason.
"""
import re

from benchcore.instrument import Instrument

CHANNELS = (1, 2)
FUNCS = ["SIN", "SQU", "RAMP", "PULS", "NOIS", "DC"]
SLOTS = (1, 2, 3, 4)
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,11}$")   # manual: <=12, letter first


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _b(v):
    return None if v is None else v.strip() in ("1", "ON")


class Ag33522A(Instrument):
    def __init__(self, link, log):
        super().__init__(link, log)
        ch = {"func": None, "freq_hz": None, "ampl": None, "unit": None,
              "offset": None, "output": None, "load": None}
        self.state = {
            "ch1": dict(ch), "ch2": dict(ch),
            "slots": None,          # [{"name":..., "valid":bool}] for 1-4
            "auto_recall": None,    # MEM:STAT:REC:AUTO
            "last_error": None,
        }

    def on_connect(self):
        self._read_slots()

    # -- polling ------------------------------------------------------------
    def poll(self):
        with self.lock:
            for n in CHANNELS:
                c = self.state[f"ch{n}"]
                fn = self.link.query(f"SOUR{n}:FUNC?")
                c["func"] = fn.strip() if fn else None
                c["freq_hz"] = _f(self.link.query(f"SOUR{n}:FREQ?"))
                c["ampl"] = _f(self.link.query(f"SOUR{n}:VOLT?"))
                u = self.link.query(f"SOUR{n}:VOLT:UNIT?")
                c["unit"] = u.strip() if u else None
                c["offset"] = _f(self.link.query(f"SOUR{n}:VOLT:OFFS?"))
                c["output"] = _b(self.link.query(f"OUTP{n}?"))
                ld = _f(self.link.query(f"OUTP{n}:LOAD?"))
                # 9.9E37 is how the 33500 reports INFinity (high-Z).
                c["load"] = None if ld is None else ("HighZ" if ld > 1e30 else f"{ld:g} Ω")
            return self.status()

    def _read_slots(self):
        slots = []
        for n in SLOTS:
            nm = self.link.query(f"MEM:STAT:NAME? {n}")
            ok = self.link.query(f"MEM:STAT:VAL? {n}")
            slots.append({"name": nm.strip().strip('"') if nm else f"STATE_{n}",
                          "valid": _b(ok)})
        self.state["slots"] = slots
        self.state["auto_recall"] = _b(self.link.query("MEM:STAT:REC:AUTO?"))

    def status(self):
        st = self.base_state()
        st.update(self.state)
        st["funcs"] = FUNCS
        return st

    # -- actions ------------------------------------------------------------
    def action(self, name, args):
        with self.lock:
            if name == "clear_errors":
                return {"ok": True, "errors": self.drain_errors()}
            if name == "save_state":
                return self._save(args)
            if name == "recall_state":
                return self._recall(args)
            if name == "auto_recall":
                self.link.write("MEM:STAT:REC:AUTO " + ("ON" if args.get("value") else "OFF"))
                return self._after()
            n = args.get("ch")
            if n not in CHANNELS:
                return {"ok": False, "msg": "channel must be 1 or 2"}
            if name == "set_func":
                f = str(args.get("value", "")).upper()
                if f not in FUNCS:
                    return {"ok": False, "msg": f"unknown function {f}"}
                self.link.write(f"SOUR{n}:FUNC {f}")
                return self._after()
            if name == "set_freq":
                self.link.write(f"SOUR{n}:FREQ {float(args['value']):.9g}")
                return self._after()
            if name == "set_ampl":
                self.link.write(f"SOUR{n}:VOLT {float(args['value']):.6g}")
                return self._after()
            if name == "set_offset":
                self.link.write(f"SOUR{n}:VOLT:OFFS {float(args['value']):.6g}")
                return self._after()
            if name == "set_load":
                v = str(args.get("value", "")).upper()
                if v not in ("50", "INF"):
                    return {"ok": False, "msg": "load must be 50 or INF"}
                self.link.write(f"OUTP{n}:LOAD {v}")
                return self._after()
            if name == "output":
                on = bool(args.get("value"))
                if on and not args.get("confirmed"):
                    return {"ok": False, "msg": "output enable needs confirmation"}
                self.link.write(f"OUTP{n} {'ON' if on else 'OFF'}")
                return self._after()
            return {"ok": False, "msg": f"unknown action {name}"}

    def _after(self):
        """The generator validates ranges itself (e.g. amplitude + offset against
        the load); its error queue is the most precise refusal there is."""
        errs = self.drain_errors()
        self.poll()
        return {"ok": not errs, "msg": " | ".join(errs) if errs else None}

    # -- saved states -------------------------------------------------------
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
            return {"ok": False, "msg": "location must be 1-4 (0 is the power-down state)"}
        name = str(args.get("name") or "").strip()
        if name and not _NAME_RE.match(name):
            return {"ok": False, "msg": "name: up to 12 letters, digits or _, "
                    "starting with a letter"}
        used = _b(self.link.query(f"MEM:STAT:VAL? {n}"))
        if used and not args.get("confirmed"):
            return {"ok": False, "confirm": True,
                    "msg": f"Location {n} already holds a state -- confirm to overwrite."}
        self.link.write(f"*SAV {n}")
        if name:
            self.link.write(f"MEM:STAT:NAME {n},{name}")
        errs = self.drain_errors()
        self._read_slots()
        if errs:
            return {"ok": False, "msg": " | ".join(errs)}
        return {"ok": True, "msg": f"Saved to {n}" + (f" as {name}" if name else "") + "."}

    def _recall(self, args):
        n = self._slot(args)
        if n is None:
            return {"ok": False, "msg": "location must be 1-4"}
        if not _b(self.link.query(f"MEM:STAT:VAL? {n}")):
            return {"ok": False, "msg": f"Location {n} is empty -- nothing to recall."}
        if not args.get("confirmed"):
            return {"ok": False, "msg": "recall needs confirmation"}
        self.link.write(f"*RCL {n}")
        errs = self.drain_errors()
        self.poll()
        if errs:
            return {"ok": False, "msg": " | ".join(errs)}
        live = [f"CH{c}" for c in CHANNELS if self.state[f"ch{c}"]["output"]]
        return {"ok": True, "msg": f"Recalled {n}."
                + (f" Output ON: {', '.join(live)}." if live else "")}

    # -- safety -------------------------------------------------------------
    def all_off(self):
        a = self.link.write("OUTP1 OFF")
        b = self.link.write("OUTP2 OFF")
        return a and b
