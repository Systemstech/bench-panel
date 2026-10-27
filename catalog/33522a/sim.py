#!/usr/bin/env python3
"""Simulator model of the 33522A -- for the /sim/ bench. Not a driver; see benchcore/sim.py.

Answers exactly the SCPI driver.py sends. Two outputs, "ch1" and "ch2", each a
50-ohm source.

THE ONE BEHAVIOUR WORTH SIMULATING CAREFULLY: OUTPUT TERMINATION
OUTP:LOAD does not change the hardware; it only tells the generator what load to
assume when it turns the amplitude you type into a voltage. The model keeps the
real quantity -- the open-circuit EMF -- and derives the displayed amplitude from
the LOAD setting, so:
  * load setting 50, 1 Vpp typed  ->  2 Vpp EMF -> ~2 Vpp into a 1 Mohm meter
  * load setting INF, 1 Vpp typed ->  1 Vpp EMF -> ~1 Vpp into a 1 Mohm meter
  * changing the LOAD setting keeps the output and changes the shown figure.
That is the mistake a script can really make, so the simulator makes it visible.

Power-on: factory state (SIN, 1 kHz, 0.1 Vpp, 0 V offset, load 50, outputs off) --
what this unit showed on 2026-09-23 with MEM:STAT:REC:AUTO OFF.

Limits enforced (error -222, setting unchanged): frequency 1 uHz..30 MHz for SIN
(the model applies the same to every waveform), |offset| + Vpk <= 5 V into the
assumed 50 ohms (10 V assumed High-Z), and 1 mVpp minimum; dBm is refused with a
High-Z load setting. These are the MODEL's choices -- the manual's full limit table
depends on waveform and is not copied here, and the real generator may clamp or
switch units where the model refuses.
"""
import math

from benchcore.sim import OutOfRange, SimModel, SettingsConflict, Undefined, num, onoff, sci

FUNCS = ("SIN", "SQU", "RAMP", "PULS", "NOIS", "DC", "PRBS", "ARB", "TRI")
CREST = {"SIN": 2 * math.sqrt(2), "SQU": 2.0, "RAMP": 2 * math.sqrt(3), "TRI": 2 * math.sqrt(3),
         "PULS": 2.0, "NOIS": 2 * 3.0, "PRBS": 2.0, "ARB": 2 * math.sqrt(2)}   # Vpp / Vrms
ZSRC = 50.0


def _load_word(v):
    return math.inf if v > 1e30 else v


class Channel:
    def __init__(self):
        self.func, self.freq, self.offset_emf = "SIN", 1000.0, 0.0
        self.load = 50.0                    # the ASSUMED load, ohms (inf = High-Z)
        self.unit = "VPP"
        self.out = False
        self.emf_pp = 0.2                   # 0.1 Vpp shown into an assumed 50 ohm

    # the assumed load turns EMF into the number on the screen
    def _k(self):
        return 1.0 if math.isinf(self.load) else self.load / (self.load + ZSRC)

    def shown_pp(self):
        return self.emf_pp * self._k()

    def shown_ampl(self):
        vpp = self.shown_pp()
        if self.unit == "VPP":
            return vpp
        vrms = vpp / CREST.get(self.func, 2 * math.sqrt(2))
        if self.unit == "VRMS":
            return vrms
        return 10 * math.log10(max(vrms, 1e-12) ** 2 / 50.0 / 1e-3)      # DBM, into 50 ohm

    def set_ampl(self, value):
        if self.unit == "VPP":
            vpp = value
        elif self.unit == "VRMS":
            vpp = value * CREST.get(self.func, 2 * math.sqrt(2))
        else:
            if not math.isfinite(self.load):
                raise SettingsConflict("Settings conflict: dBm needs a finite load setting")
            vpp = math.sqrt(10 ** (value / 10) * 1e-3 * 50.0) * CREST.get(self.func, 2 * math.sqrt(2))
        self._limits(vpp, self.offset_emf * self._k())
        self.emf_pp = vpp / self._k()

    def set_offset(self, shown):
        self._limits(self.shown_pp(), shown)
        self.offset_emf = shown / self._k()

    def _limits(self, vpp, offs):
        vmax = 10.0 if math.isinf(self.load) else 5.0 * self.load / (self.load + ZSRC) * 2
        if vpp < 0.001 or abs(offs) + vpp / 2 > vmax + 1e-9:
            raise OutOfRange()


class Ag33522ASim(SimModel):
    OUTPUTS = {"ch1": "channel 1 output BNC", "ch2": "channel 2 output BNC"}
    PRESETS = {
        "ch1-on": "CH1 output left ON (1 kHz, 1 Vpp) -- a script must refuse to start",
        "unit-dbm": "both channels' amplitude unit left on dBm",
        "high-z": "both channels' load setting left on High-Z (INF)",
    }

    def power_on_reset(self):
        self.ch = {1: Channel(), 2: Channel()}
        self.auto = False
        if not hasattr(self, "slots"):
            self.slots = {n: None for n in range(1, 5)}
            self.names = {n: f"STATE_{n}" for n in range(1, 5)}

    def apply_preset(self, name):
        super().apply_preset(name)
        if name == "ch1-on":
            self.ch[1].emf_pp, self.ch[1].out = 2.0, True
        elif name == "unit-dbm":
            for c in self.ch.values():
                c.unit = "DBM"
        elif name == "high-z":
            for c in self.ch.values():
                c.load = math.inf

    def source(self, port, z_load):
        c = self.ch[1 if port == "ch1" else 2]
        if not c.out:
            return None
        k = 1.0 if math.isinf(z_load) else z_load / (z_load + ZSRC)
        if c.func == "DC":
            return {"dc": c.offset_emf * k, "vrms": 0.0, "freq": None, "wave": "DC"}
        return {"dc": c.offset_emf * k, "vrms": c.emf_pp * k / CREST.get(c.func, 2 * math.sqrt(2)),
                "freq": c.freq, "wave": c.func}

    # -- SCPI --------------------------------------------------------------------------
    def _chan(self, h, prefix):
        """'SOUR1:FUNC' -> (channel, 'FUNC') for prefix 'SOUR'."""
        rest = h[len(prefix):]
        n = 1
        if rest[:1].isdigit():
            n, rest = int(rest[0]), rest[1:]
        if n not in self.ch:
            raise Undefined()
        return self.ch[n], rest.lstrip(":")

    def query(self, h, arg):
        if h.startswith("SOUR"):
            c, sub = self._chan(h, "SOUR")
            if sub == "FUNC":
                return c.func
            if sub == "FREQ":
                return sci(c.freq, 16)
            if sub == "VOLT":
                return sci(c.shown_ampl(), 16)
            if sub == "VOLT:UNIT":
                return c.unit
            if sub == "VOLT:OFFS":
                return sci(c.offset_emf * c._k(), 16)
        elif h.startswith("OUTP"):
            c, sub = self._chan(h, "OUTP")
            if sub == "":
                return "1" if c.out else "0"
            if sub == "LOAD":
                return sci(9.9e37 if math.isinf(c.load) else c.load, 16)
        elif h == "MEM:STAT:NAME":
            return f'"{self.names[self._slot(arg)]}"'
        elif h == "MEM:STAT:VAL":
            return "1" if self.slots[self._slot(arg)] else "0"
        elif h == "MEM:STAT:REC:AUTO":
            return "1" if self.auto else "0"
        raise Undefined()

    def command(self, h, arg):
        if h.startswith("SOUR"):
            c, sub = self._chan(h, "SOUR")
            if sub == "FUNC":
                f = (arg or "").upper()
                if f not in FUNCS:
                    raise OutOfRange("Illegal parameter value")
                c.func = f
            elif sub == "FREQ":
                f = num(arg, ("HZ",))
                if not 1e-6 <= f <= 30e6:
                    raise OutOfRange()
                c.freq = f
            elif sub == "VOLT":
                c.set_ampl(num(arg, ("VPP", "VRMS", "DBM", "V")))
            elif sub == "VOLT:OFFS":
                c.set_offset(num(arg, ("V",)))
            elif sub == "VOLT:UNIT":
                u = (arg or "").upper()
                if u not in ("VPP", "VRMS", "DBM"):
                    raise OutOfRange("Illegal parameter value")
                if u == "DBM" and math.isinf(c.load):
                    raise SettingsConflict("Settings conflict: dBm with a High-Z load setting")
                c.unit = u
            else:
                raise Undefined()
            return
        if h.startswith("OUTP"):
            c, sub = self._chan(h, "OUTP")
            if sub == "":
                c.out = onoff(arg)
            elif sub == "LOAD":
                a = (arg or "").upper()
                v = math.inf if a in ("INF", "INFINITY") else num(arg, ("OHM",))
                if not (math.isinf(v) or 1 <= v <= 10e3):
                    raise OutOfRange()
                shown_pp, shown_off = c.shown_pp(), c.offset_emf * c._k()
                c.load = v
                # The DISPLAY is what the generator keeps; the EMF follows the new load.
                c.emf_pp, c.offset_emf = shown_pp / c._k(), shown_off / c._k()
            else:
                raise Undefined()
            return
        if h == "*SAV":
            n = self._slot(arg)
            self.slots[n] = {k: vars(c).copy() for k, c in self.ch.items()}
            return
        if h == "*RCL":
            n = self._slot(arg)
            if not self.slots[n]:
                raise SettingsConflict("Settings conflict: state location empty")
            for k, d in self.slots[n].items():
                out = self.ch[k].out            # output ON/OFF is not stored in a state
                vars(self.ch[k]).update(d)
                self.ch[k].out = out
            return
        if h == "MEM:STAT:NAME":
            n, _, name = (arg or "").partition(",")
            self.names[self._slot(n)] = name.strip().strip("'\"")[:12]
            return
        if h == "MEM:STAT:REC:AUTO":
            self.auto = onoff(arg)
            return
        raise Undefined()

    @staticmethod
    def _slot(arg):
        try:
            n = int(float(arg))
        except (TypeError, ValueError):
            raise OutOfRange() from None
        if n not in range(1, 5):
            raise OutOfRange()
        return n
