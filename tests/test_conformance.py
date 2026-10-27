#!/usr/bin/env python3
"""Prove the plugin checker CATCHES things -- a checker that passes everything
proves nothing.

Each case copies catalog/e3641a (a plugin with an output, the strictest kind),
breaks it in exactly one way, and asserts that the named rule FAILS. It also
asserts the unbroken copy passes, so a failure is the mutation's and not noise.

    python3 tests/test_conformance.py
"""
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from benchcore.conformance import check_plugin  # noqa: E402

SRC = os.path.join(HERE, "catalog", "e3641a")


def edit(folder, name, fn):
    p = os.path.join(folder, name)
    with open(p, encoding="utf-8") as fh:
        t = fh.read()
    new = fn(t)
    assert new != t, f"mutation did not change {name}"
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(new)


def manifest(folder, fn):
    p = os.path.join(folder, "manifest.json")
    m = json.load(open(p))
    fn(m)
    json.dump(m, open(p, "w"), indent=2)


CASES = [
    ("S0", "keepalive that is neither serial-poll nor a query",
     lambda d: manifest(d, lambda m: m.update(keepalive="*OPC"))),
    ("S0", "empty identify query",
     lambda d: manifest(d, lambda m: m.update(identify=""))),
    ("S15", "output enables without checking confirmed",
     lambda d: edit(d, "driver.py", lambda t: t.replace(
         'if on and not args.get("confirmed"):', 'if False:', 1))),
    ("S15", "output's confirm declaration removed (sends OUTP ON undeclared)",
     lambda d: manifest(d, lambda m: m["actions"]["output"].pop("confirm"))),
    ("S5", "has_output but no action declared as needing confirmation",
     lambda d: manifest(d, lambda m: [s.pop("confirm", None) for s in m["actions"].values()])),
    ("S7", "driver imports socket",
     lambda d: edit(d, "driver.py", lambda t: t.replace("import re\n", "import re\nimport socket\n", 1))),
    ("S8", "driver overrides power_on",
     lambda d: edit(d, "driver.py", lambda t: t.replace(
         "    def on_connect(self):", "    def power_on(self):\n        return {'ok': True}\n\n    def on_connect(self):", 1))),
    ("S9", "an IP address in the driver",
     lambda d: edit(d, "driver.py", lambda t: t.replace("import re\n", "import re\nHOST = '10.1.2.3'\n", 1))),
    ("S10", "card uses an element id",
     lambda d: edit(d, "card.html", lambda t: t.replace('<div class="lcd">', '<div class="lcd" id="psu">', 1))),
    ("S10", "card calls fetch() itself",
     lambda d: edit(d, "card.js", lambda t: t.replace("mount(card) {", "mount(card) {\n    fetch('api/state');", 1))),
    ("S10", "card registers under the wrong id",
     lambda d: edit(d, "card.js", lambda t: t.replace("register('e3641a'", "register('other'", 1))),
    ("S11", "card calls an undeclared action",
     lambda d: edit(d, "card.js", lambda t: t.replace("card.act('ovp_clear')", "card.act('ovp_nuke')", 1))),
    ("S16", "all_off does not report success",
     lambda d: edit(d, "driver.py", lambda t: t.replace(
         'def all_off(self):\n        return self.link.write("OUTP OFF")',
         'def all_off(self):\n        self.link.write("OUTP OFF")', 1))),
    ("S1", "unknown capability",
     lambda d: manifest(d, lambda m: m.__setitem__("capabilities", ["teleporter"]))),
    ("S3", "declares a reserved action name",
     lambda d: manifest(d, lambda m: m["actions"].__setitem__("power_on", {}))),
    ("S14", "an action raises",
     lambda d: edit(d, "driver.py", lambda t: t.replace(
         'if name == "ovp_clear":', 'if name == "ovp_clear":\n                raise RuntimeError("boom")', 1))),
    # -- the scripting dictionary and the simulator model (2026-09-24) --
    ("S17", "an action has no doc",
     lambda d: manifest(d, lambda m: m["actions"]["set_v"].pop("doc"))),
    ("S17", "an example does not fit its documented type",
     lambda d: manifest(d, lambda m: m["actions"]["set_v"]["args"]["value"].__setitem__("type", "bool"))),
    ("S17", "an example uses an undocumented argument",
     lambda d: manifest(d, lambda m: m["actions"]["set_v"]["args"].pop("value"))),
    ("S18", "a reported state field is undocumented",
     lambda d: manifest(d, lambda m: m["state"].pop("v_set"))),
    ("S18", "a documented state field is never reported",
     lambda d: manifest(d, lambda m: m["state"].__setitem__("v_sett", {"type": "float", "doc": "typo"}))),
    ("S20", "sim names a class that is not there",
     lambda d: manifest(d, lambda m: m["sim"].__setitem__("class", "NoSuchModel"))),
    ("S21", "the driver sends a command its model does not know",
     lambda d: edit(d, "driver.py", lambda t: t.replace('"VOLT:PROT:CLE"', '"VOLT:PROT:CLR"', 1))),
    ("S21", "the model does not answer a query the driver polls",
     lambda d: edit(d, "sim.py", lambda t: t.replace('if h == "VOLT:PROT:TRIP":', 'if h == "VOLT:PROT:TRIPPED":', 1))),
]


def run():
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        base = os.path.join(tmp, "e3641a")
        shutil.copytree(SRC, base, ignore=shutil.ignore_patterns("__pycache__"))
        rep = check_plugin(base)
        print(f"{'ok  ' if rep.ok else 'BAD '} unbroken copy passes")
        ok &= rep.ok
        for n, (rule, what, mutate) in enumerate(CASES):
            d = os.path.join(tmp, f"case{n}", "e3641a")
            shutil.copytree(SRC, d, ignore=shutil.ignore_patterns("__pycache__"))
            mutate(d)
            rep = check_plugin(d)
            caught = any(r == rule and lv == "MUST" and not good for r, lv, good, _ in rep.items)
            print(f"{'ok  ' if caught else 'MISS'} {rule:4s} {what}")
            ok &= caught
    print("\nALL CAUGHT" if ok else "\nTHE CHECKER MISSED SOMETHING")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
