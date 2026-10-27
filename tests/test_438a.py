#!/usr/bin/env python3
"""Behaviour test for the HP 438A plugin and the core's pre-488.2 support.

    python3 tests/test_438a.py             # exit 0 = all good (deploy.sh runs this)

The plugin standard proves the driver and model agree on program codes. This proves
what the plugin exists for, against its own model: ?ID instead of *IDN?, a serial-poll
keepalive, readings taken by addressing the meter to talk, 9.0000E+40 never becoming a
power, nothing but serial polls while ZERO or CAL ADJ runs, and CAL ADJ removing a
gain error. It also decodes the Status Message the real meter sent on 2026-09-14.
Then the transport: a real Link asks the plugin's identify query and keeps alive by
serial poll, with no socket. No instruments.
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from benchcore import transport  # noqa: E402
from benchcore.bench import TrafficLog  # noqa: E402
from benchcore.plugins import load_driver, read_manifest  # noqa: E402
from benchcore.sim import SimLink, World, load_model_class  # noqa: E402

PLUG = os.path.join(ROOT, "catalog", "438a")
fails = 0


def check(what, cond):
    global fails
    print(f"{'ok  ' if cond else 'FAIL'}  {what}")
    fails += not cond


M = read_manifest(PLUG)
Driver = load_driver(PLUG, M)


class _P:
    manifest, id, rev, folder = M, M["id"], 0, PLUG


Model, note = load_model_class(_P)
assert note is None, note
drv = Driver.__init__.__globals__     # the loader imports plugins under private names


class Recorder:
    """Wraps a model: records every line the driver sends to its parser."""

    def __init__(self, model):
        self.model, self.lines = model, []
        real = model.handle
        model.handle = lambda line: (self.lines.append(line), real(line))[1]


def bench(preset=None, wire_ref=False):
    w = World()
    m = Model(w, "pm")
    m.MODEL = "438A"
    w.models["pm"] = m
    if preset:
        m.apply_preset(preset)
    if wire_ref:
        w.wire("pm:ref", "pm:a")
    rec = Recorder(m)
    d = Driver(SimLink(w, "pm", TrafficLog()), TrafficLog())
    return d, m, rec


def act(d, name, **args):
    return d.action(name, args)


def wait_idle(d, limit=5.0):
    t0 = time.time()
    while time.time() - t0 < limit:
        st = d.poll()
        if not st["busy"]:
            return st
        time.sleep(0.2)
    return d.poll()


# -- the manifest reaches the link ---------------------------------------------
d, m, rec = bench()
check("loader: identify ?ID and keepalive serial-poll on the class",
      Driver.idn_query == "?ID" and Driver.keepalive == "serial-poll")
check("Instrument() hands both to its link", d.link.idn_query == "?ID" and d.link.keepalive == "serial-poll")
r = d.power_on()
check(f"power_on identifies with ?ID: {d.link.idn!r}", r["ok"] and d.link.idn.startswith("HP438A"))
check("?ID was the first thing sent, never *IDN?", rec.lines[:1] == ["?ID"] and
      not any("*" in x for x in rec.lines))

# -- a reading: taken by talk, in W, sentinel filtered ---------------------------
st = d.poll()
check(f"no RF: reading ~0 W ({st['reading']!r} {st['unit']})", st["unit"] == "W" and abs(st["reading"]) < 1e-7)
check("status decoded: mode A, W, entry A, osc off, free run, A auto range",
      (st["mode"], st["units"], st["entry_channel"], st["osc"], st["trigger"], st["sensor_a"]["range_auto"])
      == ("A", "W", "A", False, "free run", True))
check("learn string: both cal factors 100.0, offsets 0",
      st["sensor_a"]["cal_factor"] == 100.0 and st["sensor_b"]["cal_factor"] == 100.0
      and st["sensor_a"]["offset"] == 0.0)

# -- POWER REF is an output: confirm, and all_off ------------------------------
n = len(rec.lines)
r = act(d, "osc", value=True)
check("REF ON without confirmation refused, nothing sent", r["ok"] is False and len(rec.lines) == n)
d2, m2, _ = bench(wire_ref=True)
d2.power_on()
act(d2, "osc", value=True, confirmed=True)
st = d2.poll()
check(f"REF on, wired to A: {st['reading'] * 1e3:.4f} mW", st["osc"] and abs(st["reading"] - 1e-3) < 5e-6)
act(d2, "cal_factor", channel="A", value=97.9)
st = d2.poll()
check(f"cal factor 97.9: {st['reading'] * 1e3:.4f} mW (1/0.979 = 1.0215)",
      abs(st["reading"] - 1e-3 / 0.979) < 6e-6 and st["sensor_a"]["cal_factor"] == 97.9)
act(d2, "cal_factor", channel="A", value=100.0)
act(d2, "offset", channel="A", value=-3.0)
act(d2, "units", value="dBm")
st = d2.poll()
check(f"offset -3 dB, dBm: {st['reading']:.3f} {st['unit']}", st["unit"] == "dBm" and abs(st["reading"] + 3) < 0.01
      and st["sensor_a"]["offset"] == -3.0)
act(d2, "offset", channel="A", value=0.0)
act(d2, "rel", value=True)
d2.poll()
st = d2.poll()
check(f"REL in dBm reads dB about 0: {st['reading']:.3f} {st['unit']}", st["rel"] and st["unit"] == "dB"
      and abs(st["reading"]) < 0.01)
act(d2, "rel", value=False)
check("all_off sends OC0 and says so", d2.all_off() is True and d2.poll()["osc"] is False)
r = d2.power_off()
check("power_off: output switched off confirmed", r["output_off"] is True and m2.osc is False)

# -- limits refused before anything is sent -------------------------------------
n = len(rec.lines)
bad = [act(d, "cal_factor", channel="A", value=150.1), act(d, "cal_factor", channel="C", value=90),
       act(d, "offset", channel="B", value=-100), act(d, "range", channel="A", value="6"),
       act(d, "filter", channel="A", value="10"), act(d, "measure", value="A*B"),
       act(d, "units", value="mW"),
       act(d, "cal_adj", channel="A", ref_cf=49.9, confirmed=True)]
check("8 out-of-range settings refused, nothing sent", all(x["ok"] is False for x in bad) and len(rec.lines) == n)

# -- the 9.0000E+40 sentinel -----------------------------------------------------
d3, _, _ = bench("no-sensors")
d3.power_on()
st = d3.poll()
check(f"no sensors: reading null, error 31 ({st['meas_error_text']})",
      st["reading"] is None and st["meas_error"] == 31)
d3, _, _ = bench("needs-zero")
d3.power_on()
st = d3.poll()
check(f"zero drifted negative: reading null, error 15", st["reading"] is None and st["meas_error"] == 15)

# -- ZERO: nothing but serial polls until it finishes ----------------------------
r = act(d3, "zero", channel="A")
check("ZERO without confirmation refused", r["ok"] is False)
r = act(d3, "zero", channel="A", confirmed=True)
check("ZERO started", r["ok"] and d3.status()["busy"])
rec3 = Recorder(d3.link.model)
refused = act(d3, "units", value="dBm")
st = d3.poll()
check("while zeroing: an action is refused (it would abort the zero)", refused["ok"] is False)
check("while zeroing: the poll sent no command, only a serial poll", rec3.lines == [] and st["busy"])
check("while zeroing: POWER REF shown unknown, not a stale value (the meter drives it)", st["osc"] is None)
st = wait_idle(d3)
check(f"ZERO finished: {st['last_op']!r}; error cleared, reading back",
      st["last_op"].startswith("zero A done") and st["meas_error"] is None and st["reading"] is not None)

# -- ZERO with RF on the sensor fails, and says so -------------------------------
d4, m4, _ = bench(wire_ref=True)
d4.power_on()
act(d4, "osc", value=True, confirmed=True)
act(d4, "zero", channel="A", confirmed=True)
st = wait_idle(d4)
check(f"ZERO with 1 mW applied: {st['last_op']!r}", "FAILED" in (st["last_op"] or "") and st["meas_error"] == 1)

# -- CAL ADJ removes the as-found gain error (+0.79 dB, 2026-09-18) --------------
d5, m5, _ = bench("uncalibrated-a", wire_ref=True)
d5.power_on()
act(d5, "units", value="dBm")
act(d5, "osc", value=True, confirmed=True)
before = d5.poll()["reading"]
act(d5, "cal_adj", channel="A", ref_cf=100.0, confirmed=True)
st = wait_idle(d5)
after = st["reading"]
check(f"CAL ADJ: {before:+.3f} dBm before, {after:+.3f} dBm after ({st['last_op']})",
      abs(before - 0.79) < 0.02 and abs(after) < 0.01 and st["last_op"].startswith("cal A done"))

# -- CAL ADJ with the sensor NOT on the reference --------------------------------
d6, _, _ = bench()
d6.power_on()
act(d6, "cal_adj", channel="A", ref_cf=100.0, confirmed=True)
st = wait_idle(d6)
check(f"CAL ADJ off the reference: {st['last_op']!r}", "FAILED" in (st["last_op"] or "") and st["meas_error"] == 3)

# -- a zero whose completion bit never comes: give up at the limit, read the code --
d8, m8, _ = bench()
d8.power_on()
drv["BUSY_LIMIT_S"] = 1.0
try:
    act(d8, "zero", channel="A", confirmed=True)
    m8.busy = ("zero", "A", time.time() + 3600, None)     # a meter that never finishes
    d8.poll()
    time.sleep(1.2)
    st = d8.poll()
finally:
    drv["BUSY_LIMIT_S"] = 60.0
check(f"no Cal/Zero Complete by the limit: not called done ({st['last_op']!r:.60})",
      st["busy"] is None and "not seen" in (st["last_op"] or ""))

# -- zero power in dBm: error 27 comes and goes (p. 3-54); a blank never goes unexplained --
d9, _, _ = bench()
d9.power_on()
act(d9, "units", value="dBm")
sts = [d9.poll() for _ in range(40)]
blank = [x for x in sts if x["reading"] is None]
check(f"no RF in dBm: {len(blank)}/40 blank readings, every one with a reason",
      all(x["meas_error_text"] for x in blank))

# the model itself: SM reports the measurement just sent, never a fresh one (p. 3-38)
_, m10, _ = bench()
m10.log_units = True
pairs = [(m10.talk(), m10.handle("SM")) for _ in range(40)]
check("model: every 9.0000E+40 is followed by an SM with its error code",
      all(sm[:2] != "00" for v, sm in pairs if v == "+9.0000E+40") and
      any(v == "+9.0000E+40" for v, _ in pairs))

# -- a ratio with channel B empty -------------------------------------------------
act(d6, "measure", value="A/B")
st = d6.poll()
check("A/B with no sensor on B: reading null, error 32", st["reading"] is None and st["meas_error"] == 32)

# -- trigger hold: answers nothing, so it is never switched on -------------------
d7, _, rec7 = bench("trigger-hold")
r = d7.power_on()
check(f"left in trigger hold: power_on fails as not answering ({r.get('msg', '')[:40]}...)",
      r["ok"] is False and not d7.enabled)

# -- the real meter's Status Message, 2026-09-14 ----------------------------------
sm = drv["_decode_sm"]("150000111117170A0002000")
check("real SM 150000111117170A0002000: error 15, mode A, auto range 1, auto filter 7, W, free run",
      sm["meas_error"] == 15 and sm["mode"] == "A" and sm["sensor_a"] == {
          "range": 1, "range_auto": True, "filter": 7, "filter_auto": True}
      and sm["units"] == "W" and sm["trigger"] == "free run" and sm["osc"] is False)
check("garbage SM is None, never a guess", drv["_decode_sm"]("0") is None
      and drv["_decode_sm"]("15000011111717XA0002000") is None)
ln = drv["_decode_learn"]("TR3BPAEKB 97.9EN OS-1.50EN RA FA LL+000.000EN HL+000.000EN"
                           "BEKB100.0EN OS00.00EN RM3EN FM5EN LL+000.000EN HL+000.000ENBELGOC1GT2LM0")
check("learn string: A 97.9 % / -1.50 dB, B 100.0 % / 0, oscillator ON",
      ln["a"] == {"cal_factor": 97.9, "offset": -1.5} and ln["b"] == {"cal_factor": 100.0, "offset": 0.0}
      and ln["osc"] is True)

# -- the real transport: identify and keepalive, no socket ------------------------


class FakeInst:
    def __init__(self):
        self.calls = []

    def query(self, s):
        self.calls.append(("query", s))
        return "HP438A,VER5.00"

    def read_stb(self):
        self.calls.append(("stb",))
        return 0

    def read(self):
        self.calls.append(("read",))
        return "+1.0000E-03\r\n"


link = transport.Link({"type": "gpib-gateway", "gateway": "192.0.2.1", "address": 13}, TrafficLog())
try:
    inst = Driver(link, TrafficLog())
    fi = FakeInst()
    link._inst, link.last_ok = fi, 0.0
    link.poke()
    check("Link keepalive for this plugin is a serial poll, not *OPC?", fi.calls == [("stb",)])
    check("Link.read() returns the reading as sent", link.read() == "+1.0000E-03")
    check("Link.read_stb() returns an int", link.read_stb() == 0)
    link2 = transport.Link({"type": "gpib-gateway", "gateway": "192.0.2.1", "address": 22}, TrafficLog())
    fi2 = FakeInst()
    link2._inst, link2.last_ok = fi2, 0.0
    link2.poke()
    check("a Link nobody configured still keeps alive with *OPC?", fi2.calls == [("query", "*OPC?")])
    link2._inst = None
    link2.forget()
finally:
    link._inst = None
    link.forget()

print(f"\n{'ALL OK' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
