#!/usr/bin/env python3
"""Behaviour test for the 6063B plugin, driven against its simulator model with the
E3641A's model as the source on its input.

    python3 tests/test_6063b.py            # exit 0 = all good (deploy.sh runs this)

Proves what the plugin exists for: input on needs confirmation; mode and range
changes are refused while sinking; a protection shutdown is SEEN even though INP?
still reads 1; clearing it needs confirmation; UNR when the source cannot supply;
the sense-switch fault is flagged; slot 0 is never written. No instruments.
"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from benchcore.bench import TrafficLog  # noqa: E402
from benchcore.sim import SimLink, World  # noqa: E402

fails = 0


def check(what, cond):
    global fails
    print(f"{'ok  ' if cond else 'FAIL'}  {what}")
    fails += not cond


def load(plugin, name, cls):
    path = os.path.join(ROOT, "catalog", plugin, name)
    spec = importlib.util.spec_from_file_location(f"t_{plugin}_{name[:-3]}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, cls)


Load, LoadSim = load("6063b", "driver.py", "HP6063B"), load("6063b", "sim.py", "HP6063BSim")
PsuSim = load("e3641a", "sim.py", "E3641ASim")


def bench(preset=None, psu_v=10.0, psu_i=0.8):
    w = World()
    psu, ld = PsuSim(w, "psu"), LoadSim(w, "ld")
    w.models.update(psu=psu, ld=ld)
    w.wire("psu:out", "ld:in")
    psu.v, psu.i, psu.out = psu_v, psu_i, True
    if preset:
        ld.apply_preset(preset)
    d = Load(SimLink(w, "ld", TrafficLog()), TrafficLog())
    d.link.connect()
    return d, ld, psu


def act(d, name, **args):
    return d.action(name, args)


def settle(d, n=3):
    st = None
    for _ in range(n):              # CC converges over a poll or two (see sim.py)
        st = d.poll()
    return st


# -- CC, the normal case ---------------------------------------------------------
d, ld, _ = bench()
st = d.poll()
check("powers up OFF in CC, nothing sunk", st["input"] is False and st["mode"] == "CC" and st["i_meas"] < 0.01)
check("input ON without confirmation refused, nothing sent", act(d, "input", value=True)["ok"] is False and not ld.inp)
check("2 A refused on... the 10 A range is the default, so 0.5 A goes in", act(d, "set_i", value=0.5)["ok"])
act(d, "input", value=True, confirmed=True)
st = settle(d)
check(f"CC 0.5 A from a 10 V supply: {st['i_meas']:.3f} A at {st['v_meas']:.2f} V",
      abs(st["i_meas"] - 0.5) < 0.02 and abs(st["v_meas"] - 10) < 0.1)
check("no faults, not shut down, regulated", st["faults"] == [] and st["shutdown"] is False and st["unregulated"] is False)
check(f"power computed: {st['p_meas']:.2f} W", abs(st["p_meas"] - 5.0) < 0.3)

# -- refused while sinking ---------------------------------------------------------
for name, val in (("set_mode", "CV"), ("set_i_range", "LOW"), ("set_r_range", "LOW")):
    r = act(d, name, value=val)
    check(f"{name} {val} refused while the input is ON", r["ok"] is False and "OFF" in r["msg"])
check("mode unchanged", ld.mode == "CURR" and ld.i_rng == 10.0)
check("save refused while the input is ON", act(d, "save_state", slot=1)["ok"] is False)

# -- UNR: the source cannot supply the CC level --------------------------------------
act(d, "set_i", value=1.5)                    # E3641A LOW range limit is 0.8 A
st = settle(d)
check(f"CC 1.5 A from a 0.8 A-limited supply: UNREGULATED (sinking {st['i_meas']:.2f} A)",
      st["unregulated"] is True and not st["shutdown"])

# -- OCP shutdown: INP? stays 1, the status register tells the truth -----------------
d, ld, _ = bench()
act(d, "set_i", value=0.5); act(d, "set_ocp", value=0.3); act(d, "ocp_state", value=True)
act(d, "input", value=True, confirmed=True)
st = settle(d)
check("0.5 A through OCP 0.3 A: shutdown", st["shutdown"] is True and "OC" in st["faults"])
check("INP? still reads ON through the shutdown (Prog p. 45)", st["input"] is True)
check(f"and it sinks nothing ({st['i_meas']:.3f} A)", st["i_meas"] < 0.01)
check("protection_clear without confirmation refused", act(d, "protection_clear")["ok"] is False and ld.latched)
act(d, "set_ocp", value=2.0)
check("with confirmation it clears", act(d, "protection_clear", confirmed=True)["ok"])
st = settle(d)
check("and the input comes back ON by itself -- why it needs confirming",
      st["shutdown"] is False and abs(st["i_meas"] - 0.5) < 0.02)

# -- CR -----------------------------------------------------------------------------
d, ld, _ = bench()
act(d, "set_r_range", value="LOW"); act(d, "set_r", value=20.0)
check("30 ohm refused on the 24 ohm range", act(d, "set_r", value=30.0)["ok"] is False)
act(d, "set_mode", value="CR"); act(d, "input", value=True, confirmed=True)
st = settle(d)
check(f"CR 20 ohm across 10 V: {st['i_meas']:.3f} A", st["mode"] == "CR" and abs(st["i_meas"] - 0.5) < 0.02)

# -- the sense switch fault -------------------------------------------------------------
d, ld, _ = bench(preset="sense-rmt-no-leads")
act(d, "set_i", value=0.5); act(d, "input", value=True, confirmed=True)
st = settle(d)
check(f"RMT sense, no leads, CC: sinks {st['i_meas']:.2f} A but reads {st['v_meas']:.2f} V -> flagged",
      st["v_sense_suspect"] is True)
d, ld, _ = bench()
act(d, "set_i", value=0.5); act(d, "input", value=True, confirmed=True)
check("a healthy load is not flagged", settle(d)["v_sense_suspect"] is False)

# -- limits and slots --------------------------------------------------------------------
d, ld, _ = bench()
d.poll()
act(d, "set_i_range", value="LOW"); d.poll()
check("2 A refused on the 1 A range", act(d, "set_i", value=2.0)["ok"] is False and ld.i == 0.0)
check("241 V refused", act(d, "set_v", value=241.0)["ok"] is False)
check("OCP 10.5 A refused", act(d, "set_ocp", value=10.5)["ok"] is False)
check("OCP delay 61 s refused", act(d, "set_ocp_delay", value=61.0)["ok"] is False)
check("slot 0 (power-on state) refused", act(d, "save_state", slot=0)["ok"] is False)
check("slot 7 refused", act(d, "save_state", slot=7)["ok"] is False)
check("slot 6 saved with the input off", act(d, "save_state", slot=6)["ok"] is True)
check("recall without confirmation refused", act(d, "recall_state", slot=6)["ok"] is False)
check("all_off sends INP OFF", d.all_off() is True and ld.inp is False)
check("no errors on the model's queue", not ld.errors)

print("ALL OK" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
