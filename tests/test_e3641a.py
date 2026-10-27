#!/usr/bin/env python3
"""Behaviour test for the E3641A plugin, driven against its simulator model.

    python3 tests/test_e3641a.py           # exit 0 = all good (deploy.sh runs this)

Pins the 1.2.2 fix: a setpoint is checked against the range the supply is in NOW,
not the range the last poll saw. psu_dmm_check's restore does exactly this --
set_range(as found) then set_v(as found) back to back -- so with the old code a
supply found on HIGH above 35 V was silently NOT restored (the restore ignores
errors by design). No instruments.
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


def load(name, cls):
    path = os.path.join(ROOT, "catalog", "e3641a", name)
    spec = importlib.util.spec_from_file_location(f"t_e3641a_{name[:-3]}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, cls)


Driver, Model = load("driver.py", "E3641A"), load("sim.py", "E3641ASim")


def bench(preset=None):
    w = World()
    psu = Model(w, "psu")
    w.models["psu"] = psu
    if preset:
        psu.apply_preset(preset)
    d = Driver(SimLink(w, "psu", TrafficLog()), TrafficLog())
    d.link.connect()
    return d, psu


def act(d, name, **args):
    return d.action(name, args)


# The restore in psu_dmm_check, found on HIGH at 40 V, test run on LOW.
d, psu = bench()
psu.rng, psu.v = "P60V", 40.0                     # found like this at the front panel
psu_found = (psu.rng, psu.v)
act(d, "set_range", value="LOW"); act(d, "set_v", value=0.0)
d.poll()                                           # the last poll sees LOW
act(d, "set_range", value="HIGH")                  # restore, back to back
r = act(d, "set_v", value=40.0)
check(f"restore to HIGH then 40 V with no poll between: accepted ({r})", r["ok"] is True)
check("and the supply is really back as found", (psu.rng, psu.v) == psu_found)

# The other direction: the last poll saw HIGH, the supply is now on LOW.
d, psu = bench(preset="high-range")
d.poll()
act(d, "set_range", value="LOW")
before = psu.v
r = act(d, "set_v", value=50.0)
check("LOW then 50 V with no poll between: refused, nothing sent", r["ok"] is False and psu.v == before)
check("current too: LOW then 0.7 A accepted (0.8 A on LOW)", act(d, "set_i", value=0.7)["ok"] is True)
act(d, "set_range", value="HIGH")
check("HIGH then 0.7 A refused (0.5 A on HIGH)", act(d, "set_i", value=0.7)["ok"] is False)

# Ordinary limits still hold.
d, psu = bench()
d.poll()
check("36 V refused on LOW", act(d, "set_v", value=36.0)["ok"] is False)
check("35 V accepted on LOW", act(d, "set_v", value=35.0)["ok"] is True)
check("-1 V refused", act(d, "set_v", value=-1.0)["ok"] is False)
check("no errors on the model's queue", not psu.errors)

print("ALL OK" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
