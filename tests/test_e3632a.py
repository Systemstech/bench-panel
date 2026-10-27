#!/usr/bin/env python3
"""Behaviour test for the E3632A plugin, driven against its own simulator model.

    python3 tests/test_e3632a.py           # exit 0 = all good (deploy.sh runs this)

The plugin standard proves the driver and model agree on SCPI. This proves the
things the plugin exists for: the readback check catches a stuck ADC (the real
fault on this bench's unit) and stays quiet on a healthy one; OCP trips and
clears; range and protection limits are refused, not sent. No instruments.
"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from benchcore.bench import TrafficLog  # noqa: E402
from benchcore.sim import SimLink, SimModel, World  # noqa: E402

PLUG = os.path.join(ROOT, "catalog", "e3632a")
fails = 0


def check(what, cond):
    global fails
    print(f"{'ok  ' if cond else 'FAIL'}  {what}")
    fails += not cond


def load(name, cls):
    spec = importlib.util.spec_from_file_location(f"t_{name}", os.path.join(PLUG, name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, cls)


class Resistor(SimModel):
    """A bare load for the test: an input with a fixed resistance."""
    INPUTS = {"in": "load"}
    R = 10.0

    def z_in(self, port):
        return self.R


Driver, Model = load("driver.py", "E3632A"), load("sim.py", "E3632ASim")


def bench(preset=None, ohms=None):
    w = World()
    psu = Model(w, "psu")
    psu.MODEL = "E3632A"
    w.models["psu"] = psu
    if ohms is not None:
        Resistor.R = ohms
        w.models["r"] = Resistor(w, "r")
        w.wire("psu:out", "r:in")
    if preset:
        psu.apply_preset(preset)
    d = Driver(SimLink(w, "psu", TrafficLog()), TrafficLog())
    d.link.connect()
    return d, psu


def act(d, name, **args):
    return d.action(name, args)


# -- healthy supply: readback agrees, check says True ------------------------
d, _ = bench(ohms=10.0)
st = d.poll()
check("output off: readback_ok is None (nothing to compare), mode None",
      st["readback_ok"] is None and st["mode"] is None)
act(d, "set_v", value=5.0); act(d, "set_i", value=1.0)
act(d, "output", value=True, confirmed=True)
st = d.poll()
check(f"5 V into 10 ohm: CV, v_meas {st['v_meas']:.4f}", st["mode"] == "CV" and abs(st["v_meas"] - 5) < 0.01)
check("healthy readback in CV: readback_ok True", st["readback_ok"] is True)
act(d, "set_i", value=0.2)
st = d.poll()
check(f"limit 0.2 A into 10 ohm: CC, i_meas {st['i_meas']:.4f}", st["mode"] == "CC" and st["cc"] is True)
check("healthy readback in CC: readback_ok True", st["readback_ok"] is True)

# -- the real fault: stuck ADC --------------------------------------------------
d, _ = bench(preset="readback-stuck", ohms=10.0)
act(d, "set_v", value=5.0); act(d, "set_i", value=1.0)
act(d, "output", value=True, confirmed=True)
st = d.poll()
check("stuck ADC, CV at 5 V: readback_ok False", st["readback_ok"] is False)
act(d, "set_i", value=0.2)
st = d.poll()
check("stuck ADC, CC at 0.2 A: readback_ok False", st["readback_ok"] is False)
d2, _ = bench(preset="readback-stuck", ohms=10.0)
st = d2.poll()
check("stuck ADC with the output OFF: None, not a false alarm", st["readback_ok"] is None)

# -- OCP ---------------------------------------------------------------------------
d, psu = bench(ohms=2.0)
act(d, "set_ocp", value=1.0); act(d, "set_v", value=4.0); act(d, "set_i", value=3.0)
act(d, "output", value=True, confirmed=True)       # 4 V / 2 ohm = 2 A > OCP 1 A
st = d.poll()
check("2 A into OCP 1 A trips it", st["ocp_tripped"] is True)
check("a tripped supply is not called CV or CC, and readback is not judged",
      st["mode"] is None and st["readback_ok"] is None)
act(d, "set_ocp", value=3.0); act(d, "ocp_clear")
st = d.poll()
check("raise OCP + clear: running again in CV", st["ocp_tripped"] is False and st["mode"] == "CV")

# -- refusals send nothing -----------------------------------------------------
d, psu = bench()
d.poll()
for name, val, why in [("set_v", 16.0, "16 V on LOW (15 V)"), ("set_i", 7.5, "7.5 A on LOW (7 A)"),
                       ("set_ovp", 0.5, "OVP 0.5 V (min 1)"), ("set_ovp", 33.0, "OVP 33 V (max 32)"),
                       ("set_ocp", 8.0, "OCP 8 A (max 7.5)")]:
    before = (psu.v, psu.i, psu.ovp, psu.ocp)
    r = act(d, name, value=val)
    check(f"refused: {why}", r["ok"] is False and (psu.v, psu.i, psu.ovp, psu.ocp) == before)
act(d, "set_range", value="HIGH"); st = d.poll()
check("HIGH range: vmax 30, imax 4", st["range"] == "HIGH" and st["vmax"] == 30 and st["imax"] == 4)
check("5 A refused on HIGH (4 A)", act(d, "set_i", value=5.0)["ok"] is False)
check("25 V accepted on HIGH", act(d, "set_v", value=25.0)["ok"] is True and abs(psu.v - 25) < 1e-9)

# -- a script changes range and sets a level at once, with no poll in between ---------
d, psu = bench()
d.poll()                                           # last poll saw LOW
act(d, "set_range", value="HIGH")
check("HIGH then 25 V straight away: judged on the range it is in NOW", act(d, "set_v", value=25.0)["ok"] is True)
act(d, "set_v", value=5.0); act(d, "set_range", value="LOW")
check("LOW then 20 V straight away: refused, not sent", act(d, "set_v", value=20.0)["ok"] is False and psu.v == 5.0)

# -- saved setups ----------------------------------------------------------------
d, psu = bench()
act(d, "output", value=True, confirmed=True)
check("save refused with the output ON", act(d, "save_state", slot=1)["ok"] is False)
act(d, "output", value=False)
check("save to 3 with it off", act(d, "save_state", slot=3)["ok"] is True)
check("slot 4 does not exist", act(d, "save_state", slot=4)["ok"] is False)
check("recall without confirmation refused", act(d, "recall_state", slot=3)["ok"] is False)
check("the drain left no model errors", not psu.errors)

print("ALL OK" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
