#!/usr/bin/env python3
"""Simulator model of the 53181A -- for the /sim/ bench. Not a driver; see benchcore/sim.py.

Answers exactly the SCPI driver.py sends (53181-90002). Two inputs: "ch1" (225 MHz,
50 ohm / 1 Mohm) and "ch2" (the Option 030 channel, fixed 50 ohm).

THE BEHAVIOUR THAT MATTERS: A MEASUREMENT ONLY ENDS IF THERE IS A SIGNAL
:INIT starts one measurement. It completes a gate time later only if the selected
channel has a signal it can count; *OPC then sets bit 0 of *ESR?. With no signal it
never completes -- so the driver's "no signal" path (abort after gate + grace) runs
in the simulator exactly as it does on the bench. :FETC? before completion is
refused here with -230 and no reply, as a warning to a driver that asks too early
(the real counter HANGS the bus instead -- guide p. 4-55).

Sensitivity, from the Operating Guide's specifications (hp_53181a_manual.pdf,
pp. 3-2 and 3-3), sinusoid, x1 attenuator (x10 multiplies them by 10):
  channel 1   DC-100 MHz 20 mVrms, 100-200 MHz 30 mVrms, 200-225 MHz 40 mVrms
  channel 2   Option 030: 100 MHz-2.7 GHz -27 dBm, 2.7-3 GHz -21 dBm
A signal below that is "no signal" here; the real counter degrades rather than
stopping at a sharp edge. Channel 1's low-frequency limit is the model's 0.1 Hz,
and the 100 kHz filter is modelled as a hard cut. Ideal timebase: the reading is
the true frequency rounded to the gate's resolution.
"""
import math
import time

from benchcore.sim import OutOfRange, ScpiError, SimModel, Undefined, num, onoff

FUNCS = {"FREQ 1": ("FREQ", 1), "FREQ 2": ("FREQ", 2), "PER 1": ("PER", 1), "PER 2": ("PER", 2),
         "FREQ:RAT 1,2": ("RAT", (1, 2)), "FREQ:RAT 2,1": ("RAT", (2, 1))}
CH1_SENS = ((100e6, 0.020), (200e6, 0.030), (225e6, 0.040))       # (up to Hz, Vrms) -- p. 3-2
CH2_SENS = ((100e6, None), (2.7e9, -27.0), (3e9, -21.0))           # (up to Hz, dBm)  -- p. 3-3


def _dbm50(vrms):
    return 10 * math.log10(max(vrms, 1e-15) ** 2 / 50.0 / 1e-3)


class DataCorrupt(ScpiError):
    code, text = -230, "Data corrupt or stale"


def _norm(arg):
    s = (arg or "").strip().strip('"').upper()
    s = " ".join(s.replace(",", " , ").split()).replace(" , ", ",")
    return s.replace("FREQ:RAT", "FREQ:RAT").strip()


class Hp53181ASim(SimModel):
    INPUTS = {"ch1": "channel 1 (225 MHz)", "ch2": "channel 2 (Option 030)"}
    PRESETS = {
        "gate-10s": "gate time 10 s (a script that waits 2 s per reading will time out)",
        "filter-in": "channel 1 100 kHz low-pass filter left IN",
        "hold": "front panel in HOLD (single measurement mode)",
    }

    def power_on_reset(self):
        self.func = "FREQ 1"
        self.gate = 0.1
        self.imp, self.coup, self.att, self.filt = 1e6, "AC", 1.0, False
        self.cont = True                     # RUN at the front panel
        self.started = None
        self.result = None
        self.slots = getattr(self, "slots", {})

    def apply_preset(self, name):
        super().apply_preset(name)
        if name == "gate-10s":
            self.gate = 10.0
        elif name == "filter-in":
            self.filt = True
        elif name == "hold":
            self.cont = False

    def z_in(self, port):
        return self.imp if port == "ch1" else 50.0

    # -- counting ------------------------------------------------------------------------
    def _freq_on(self, ch):
        s = self.signal_into(f"ch{ch}")
        if not s or not s.get("freq"):
            return None
        f, v = s["freq"], s["vrms"]
        if ch == 1:
            need = next((vr for top, vr in CH1_SENS if f <= top), None)
            if need is None or f < 0.1 or v < need * (10 if self.att == 10 else 1) or (self.filt and f > 100e3):
                return None
        else:
            if f < 100e6:
                return None
            need = next((d for top, d in CH2_SENS[1:] if f <= top), None)
            if need is None or _dbm50(v) < need:
                return None
        return f

    def _value(self):
        kind, ch = FUNCS.get(self.func, ("FREQ", 1))
        if kind == "RAT":
            a, b = self._freq_on(ch[0]), self._freq_on(ch[1])
            return None if not a or not b else a / b
        f = self._freq_on(ch)
        if f is None:
            return None
        res = 1.0 / self.gate                 # ideal: one count of resolution per gate
        fr = round(f / res) * res
        return fr if kind == "FREQ" else 1.0 / fr

    def operation_complete(self):
        if self.started is None:
            return True
        if time.time() - self.started < self.gate:
            return False
        v = self._value()
        if v is None:
            return False                       # no edges: the gate never closes
        self.result = v
        return True

    # -- SCPI -------------------------------------------------------------------------------
    def query(self, h, arg):
        if h == "INIT:CONT":
            return "1" if self.cont else "0"
        if h == "FUNC":
            return f'"{self.func}"'.replace('"FREQ 1"', '"FREQ"') if self.func == "FREQ 1" else f'"{self.func}"'
        if h == "FREQ:ARM:STOP:TIM":
            return f"{self.gate:+.6E}"
        if h == "INP:IMP":
            return f"{self.imp:+.6E}"
        if h == "INP:COUP":
            return self.coup
        if h == "INP:ATT":
            return f"{self.att:+.6E}"
        if h == "INP:FILT":
            return "1" if self.filt else "0"
        if h == "INP2:IMP":
            return "+5.000000E+01"
        if h == "INP2:COUP":
            return "AC"
        if h == "ROSC:SOUR":
            return "INT"
        if h == "ROSC:SOUR:AUTO":
            return "1"
        if h == "FETC":
            if self.result is None:
                raise DataCorrupt()
            digits = max(3, min(12, int(math.log10(max(abs(self.result) * self.gate, 1))) + 2))
            return f"{self.result:+.{digits - 1}E}"
        raise Undefined()

    def command(self, h, arg):
        if h == "INIT:CONT":
            self.cont = onoff(arg)
        elif h == "INIT":
            self.started, self.result = time.time(), None
        elif h == "ABOR":
            self.started = None
            self.esr |= 1 if self._opc_pending else 0
            self._opc_pending = False
        elif h == "FUNC":
            f = _norm(arg)
            if f in ("FREQ", "PER"):
                f += " 1"
            if f not in FUNCS:
                if f.endswith(" 3"):
                    raise ScpiError("Illegal parameter value")   # there is no channel 3
                raise OutOfRange("Illegal parameter value")
            self.func = f
        elif h in ("FREQ:ARM:STAR:SOUR", "FREQ:ARM:STOP:SOUR"):
            if (arg or "").upper() not in ("IMM", "TIM", "EXT", "DIG"):
                raise OutOfRange("Illegal parameter value")
        elif h == "FREQ:ARM:STOP:TIM":
            g = num(arg, ("S",))
            if not 1e-3 <= g <= 1000:
                raise OutOfRange()
            self.gate = g
        elif h == "INP:IMP":
            v = num(arg, ("OHM",))
            if v not in (50.0, 1e6):
                raise OutOfRange()
            self.imp = v
        elif h == "INP:COUP":
            if (arg or "").upper() not in ("AC", "DC"):
                raise OutOfRange("Illegal parameter value")
            self.coup = arg.upper()
        elif h == "INP:ATT":
            v = num(arg)
            if v not in (1.0, 10.0):
                raise OutOfRange()
            self.att = v
        elif h == "INP:FILT":
            self.filt = onoff(arg)
        elif h == "*SAV":
            self.slots[self._slot(arg)] = (self.func, self.gate, self.imp, self.coup, self.att, self.filt)
        elif h == "*RCL":
            n = self._slot(arg, allow0=True)
            if n in self.slots:
                self.func, self.gate, self.imp, self.coup, self.att, self.filt = self.slots[n]
        else:
            raise Undefined()

    @staticmethod
    def _slot(arg, allow0=False):
        try:
            n = int(float(arg))
        except (TypeError, ValueError):
            raise OutOfRange() from None
        if not (1 if not allow0 else 0) <= n <= 20:
            raise OutOfRange()
        return n
