#!/usr/bin/env python3
"""The plugin standard, as code. PLUGIN_STANDARD.md is the same rules in prose.

Every rule has an id (the S-numbers in PLUGIN_STANDARD.md) and a level:
  MUST    a plugin that fails it is refused -- not installed, not loaded
  SHOULD  reported, never refused

ONE CHECKER, THREE PLACES
  tools/check_plugin.py     the author runs it; deploy.sh runs it on catalog/
  PluginStore install       after the operator confirms, before it goes live
  PluginStore load_all      at every start, so a hand-copied plugin is judged too

TWO DEPTHS
  static    reads files only. Runs no plugin code, so it is safe to show BEFORE the
            operator confirms an install (the inspect screen).
  full      static + imports the driver and exercises it against FakeLink, a
            pretend connection that records every write. This is where the safety
            rules are tested by trying to break them: every action that can turn an
            output on is called WITHOUT the confirmation flag, and must refuse and
            send nothing.

What this cannot prove: that the SCPI is right for the real instrument. Only the
instrument can. That is what the manifest's tested_on is for.
"""
import ast
import json
import os
import re

from . import transport
from .instrument import Instrument

SCHEMA = 1

# The capability vocabulary. A plugin declares what the instrument IS, from this
# list; the list grows with the core. (Plan §1: drivers and panels meet at a
# capability. The full driver/panel split can come later without a new format.)
CAPABILITIES = {
    "dc_source":          "DC power supply output",
    "electronic_load":    "DC electronic load",
    "multimeter":         "DC/AC volts, current, resistance measurement",
    "rf_source":          "RF signal generator",
    "function_generator": "function / arbitrary waveform generator",
    "frequency_counter":  "frequency, period, ratio measurement",
    "power_meter":        "RF power meter",
    "oscilloscope":       "oscilloscope",
    "spectrum_analyzer":  "spectrum analyzer",
    "network_analyzer":   "network analyzer",
}

# Names the core owns. A plugin may not declare them as its own actions.
RESERVED_ACTIONS = {"power_on", "power_off", "all_outputs_off", "set_polling"}

# Methods the core owns. A driver may not override them.
CORE_METHODS = {"power_on", "power_off", "mark_lost", "shutdown", "base_state", "matches"}

# A driver never moves bytes itself (plan §3.6).
FORBIDDEN_IMPORTS = {"socket", "pyvisa", "pyvisa_py", "serial", "usb", "http",
                     "urllib", "requests", "subprocess", "ctypes", "multiprocessing"}

# SCPI that switches energy ON. Used to catch an action that turns an output on
# without being declared as needing confirmation.
OUTPUT_ON_RE = [
    re.compile(r"^\s*:?OUTP(UT)?\d*(:STAT(E)?)?\s+(ON|1)\b", re.I),
    re.compile(r"^\s*:?INP(UT)?\d*(:STAT(E)?)?\s+(ON|1)\b", re.I),
    re.compile(r"^\s*\*RCL\b", re.I),
]

IP_RE = re.compile(r"(?<![\d.])(\d{1,3}\.){3}\d{1,3}(?![\d.])")
GPIB_RE = re.compile(r"gpib\d+,\d+", re.I)
ID_ATTR_RE = re.compile(r"""\bid\s*=\s*["']""", re.I)
ACT_CALL_RE = re.compile(r"""\.act\(\s*['"]([A-Za-z0-9_]+)['"]""")
BASE_KEYS = {"plugin", "model", "where", "idn", "connected", "enabled", "power_msg",
             "error", "has_output"}
CORE_STATE_KEYS = BASE_KEYS | {"data_as_of"}     # base_state(): documented once, in SCRIPTS.md

# The scripting dictionary (S17-S19). A script never speaks SCPI: it speaks the
# plugin's actions and state fields, so THOSE are what the manifest documents. The
# script editor's reference, completion and checks are built from it.
ARG_TYPES = {"float", "int", "str", "bool", "choice"}
STATE_TYPES = ARG_TYPES | {"list", "object"}


class Report:
    def __init__(self, folder):
        self.folder = folder
        self.items = []             # (rule, level, ok, detail)

    def add(self, rule, level, ok, detail=""):
        self.items.append((rule, level, bool(ok), detail))

    @property
    def must_failures(self):
        return [i for i in self.items if i[1] == "MUST" and not i[2]]

    @property
    def ok(self):
        return not self.must_failures

    def summary(self):
        f = self.must_failures
        return "; ".join(f"{r}: {d}" for r, _, _, d in f) if f else "conforms"

    def as_dict(self):
        return {"ok": self.ok, "items": [
            {"rule": r, "level": lv, "ok": ok, "detail": d} for r, lv, ok, d in self.items]}

    def text(self):
        out = []
        for r, lv, ok, d in self.items:
            mark = "PASS" if ok else ("FAIL" if lv == "MUST" else "warn")
            out.append(f"  {mark:4s} {r:4s} {lv:6s} {d}")
        return "\n".join(out)


# -- confirmation: the rule the core enforces at run time too -------------------
def confirm_required(manifest, name, args):
    """True if this call needs args.confirmed under the manifest's declaration.

    "confirm": "always"                 -- every call
    "confirm": {"when": {"value": true}} -- when every listed arg has that value
    (absent)                            -- never required by the core
    """
    spec = (manifest.get("actions") or {}).get(name) or {}
    c = spec.get("confirm")
    if c == "always":
        return True
    if isinstance(c, dict):
        when = c.get("when") or {}
        return all(_same(args.get(k), v) for k, v in when.items())
    return False


def _same(a, b):
    if isinstance(b, bool):
        return bool(a) == b if a is not None else False
    return a == b


def gate_action(manifest, name, args):
    """The core's check before any plugin action runs. Returns a refusal or None."""
    actions = manifest.get("actions") or {}
    if name not in actions:
        return f"'{name}' is not an action this plugin declares"
    if confirm_required(manifest, name, args) and not args.get("confirmed"):
        return f"'{name}' needs confirmation"
    return None


# -- the checks ----------------------------------------------------------------
def check_manifest_rules(m, rep):
    """S1-S6: the manifest itself (beyond what plugins.check_manifest requires)."""
    caps = m.get("capabilities")
    rep.add("S1", "MUST", isinstance(caps, list) and caps and all(c in CAPABILITIES for c in caps),
            f"capabilities {caps!r} must be a non-empty list from: {', '.join(CAPABILITIES)}")
    acts = m.get("actions")
    good = isinstance(acts, dict) and bool(acts)
    rep.add("S2", "MUST", good, "actions must be a non-empty object: every control the card can call")
    if not good:
        return
    bad_names = [a for a in acts if not re.match(r"^[a-z][a-z0-9_]{0,39}$", a)]
    rep.add("S2", "MUST", not bad_names, f"action names must be lower_snake_case: {bad_names or 'ok'}")
    reserved = sorted(RESERVED_ACTIONS & set(acts))
    rep.add("S3", "MUST", not reserved, f"reserved names the core owns: {reserved or 'none used'}")
    shape = []
    for a, spec in acts.items():
        if not isinstance(spec, dict) or not isinstance(spec.get("example", {}), dict):
            shape.append(a)
            continue
        c = spec.get("confirm")
        if c is not None and c != "always" and not (isinstance(c, dict) and isinstance(c.get("when"), dict)):
            shape.append(a)
    rep.add("S4", "MUST", not shape, f"each action is {{example?, confirm?}} with confirm \"always\" or "
            f"{{\"when\": {{...}}}}: {shape or 'ok'}")
    if m.get("has_output"):
        gated = [a for a, s in acts.items() if isinstance(s, dict) and s.get("confirm")]
        rep.add("S5", "MUST", bool(gated), "has_output is true, so at least one action must be "
                "declared as needing confirmation (the one that switches the output on)")
    tested = m.get("tested_on")
    rep.add("S6", "SHOULD", isinstance(tested, dict) and tested and all(k in transport.TYPES for k in tested),
            "tested_on names each connection type actually tested, with when and how")
    q = m.get("quirks", [])
    rep.add("S6", "SHOULD", isinstance(q, list) and all(isinstance(x, dict) and x.get("note") for x in q),
            "quirks is a list of {id, note}")
    for k in ("description", "author", "license"):
        rep.add("S6", "SHOULD", isinstance(m.get(k), str) and m.get(k), f"{k} is filled in")
    check_language(m, rep)


def _arg_ok(spec, value):
    """Does an example value fit its documented argument?"""
    t = spec.get("type")
    if "choices" in spec:
        return value in spec["choices"] or str(value) in [str(c) for c in spec["choices"]]
    if t == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "bool":
        return isinstance(value, bool)
    return isinstance(value, str)


def _state_spec_bad(name, spec, where="state"):
    if not isinstance(spec, dict) or spec.get("type") not in STATE_TYPES or not isinstance(spec.get("doc"), str):
        return [f"{where}.{name}"]
    if spec["type"] == "object":
        f = spec.get("fields")
        if not isinstance(f, dict) or not f:
            return [f"{where}.{name} (an object needs fields)"]
        return [b for k, v in f.items() for b in _state_spec_bad(k, v, f"{where}.{name}")]
    return []


def check_language(m, rep):
    """S17 + S19: the scripting dictionary in the manifest (S18, state vs the real
    status(), needs the driver and runs in check_dynamic)."""
    acts = m.get("actions") or {}
    bad = []
    for a, spec in acts.items():
        if not isinstance(spec.get("doc"), str) or not spec["doc"].strip():
            bad.append(f"{a}: no doc")
            continue
        args = spec.get("args")
        if not isinstance(args, dict):
            bad.append(f"{a}: no args object (use {{}} for none)")
            continue
        for n, sp in args.items():
            if not isinstance(sp, dict) or sp.get("type") not in ARG_TYPES:
                bad.append(f"{a}.{n}: type must be one of {', '.join(sorted(ARG_TYPES))}")
            elif sp["type"] == "choice" and not (isinstance(sp.get("choices"), list) and sp["choices"]):
                bad.append(f"{a}.{n}: a choice needs choices")
        for n, v in (spec.get("example") or {}).items():
            if n == "confirmed":
                continue
            if n not in args:
                bad.append(f"{a}: example uses '{n}', which args does not document")
            elif isinstance(args[n], dict) and args[n].get("type") in ARG_TYPES and not _arg_ok(args[n], v):
                bad.append(f"{a}: example {n}={v!r} does not fit its documented type")
    rep.add("S17", "MUST", not bad, "every action is documented for scripts (doc, args with types; "
            f"examples fit them): {'; '.join(bad) if bad else 'ok'}")
    st = m.get("state")
    sbad = ["state is missing"] if not isinstance(st, dict) or not st else \
        [b for k, v in st.items() for b in _state_spec_bad(k, v)]
    rep.add("S17", "MUST", not sbad, f"state fields are documented with type and doc: {sbad or 'ok'}")
    man = m.get("manual")
    rep.add("S19", "SHOULD", isinstance(man, dict) and isinstance(man.get("title"), str) and man["title"],
            "manual names the programming manual (title, and part number when it has one) -- "
            "a reference, not a copy of the PDF")
    noscpi = sorted(a for a, s in acts.items() if isinstance(s, dict) and not s.get("scpi")
                    and a not in ("reset_stats",))
    rep.add("S19", "SHOULD", not noscpi, f"each action says what it sends (scpi): {noscpi or 'ok'}")
    noref = sorted(a for a, s in acts.items() if isinstance(s, dict) and s.get("scpi") and not s.get("ref"))
    rep.add("S19", "SHOULD", not noref, f"each action cites its manual page (ref): "
            f"{', '.join(noref) if noref else 'ok'}")


def check_static(folder, m, rep):
    """S7-S10: read the files. Runs no plugin code."""
    rep.add("S6", "SHOULD", os.path.isfile(os.path.join(folder, "README.md")),
            "README.md with the quirks, manual references and what was tested")
    drv_path = os.path.join(folder, m["driver"]["file"])
    src = open(drv_path, encoding="utf-8").read()
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        rep.add("S7", "MUST", False, f"driver does not parse: {e}")
        return
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    bad = sorted(imported & FORBIDDEN_IMPORTS)
    rep.add("S7", "MUST", not bad, f"driver imports nothing that moves bytes: {bad or 'ok'}")
    cls = next((n for n in tree.body if isinstance(n, ast.ClassDef)
                and n.name == m["driver"]["class"]), None)
    rep.add("S7", "MUST", cls is not None, f"driver defines class {m['driver']['class']} at top level")
    if cls is not None:
        overrides = sorted({f.name for f in cls.body if isinstance(f, ast.FunctionDef)} & CORE_METHODS)
        rep.add("S8", "MUST", not overrides, f"driver overrides no core method: {overrides or 'ok'}")
        fixed = sorted({t.id for s in cls.body if isinstance(s, ast.Assign) for t in s.targets
                        if isinstance(t, ast.Name)} & {"model", "idn_match", "has_output", "plugin_id"})
        rep.add("S8", "SHOULD", not fixed, f"model/idn_match/has_output come from the manifest, "
                f"not the class: {fixed or 'ok'}")
    files = {n: open(os.path.join(folder, n), encoding="utf-8").read()
             for n in (m["driver"]["file"], m["card"]["html"], m["card"]["js"])}
    addr = [f"{n}: {x.group(0)}" for n, t in files.items()
            for x in list(IP_RE.finditer(t)) + list(GPIB_RE.finditer(t))]
    rep.add("S9", "MUST", not addr, f"no addresses in the plugin -- they belong in site.json: {addr or 'ok'}")
    html, js = files[m["card"]["html"]], files[m["card"]["js"]]
    rep.add("S10", "MUST", not ID_ATTR_RE.search(html) and "getElementById" not in js,
            "card uses data-k, never element ids (the same card can be on the page twice)")
    rep.add("S10", "MUST", re.search(r"""BenchPanel\.register\(\s*['"]%s['"]""" % re.escape(m["id"]), js),
            f"card.js calls BenchPanel.register('{m['id']}', ...)")
    net = [w for w in ("fetch(", "XMLHttpRequest", "WebSocket", "EventSource") if w in js]
    rep.add("S10", "MUST", not net, f"card talks to the core only through card.act(): {net or 'ok'}")
    called = set(ACT_CALL_RE.findall(js))
    undeclared = sorted(called - set(m.get("actions") or {}))
    rep.add("S11", "MUST", not undeclared, f"every card.act() name is a declared action: "
            f"{undeclared or 'ok'} (the core refuses undeclared ones)")
    unused = sorted(set(m.get("actions") or {}) - called)
    rep.add("S11", "SHOULD", not unused, f"declared actions the card never calls: {unused or 'none'}")


class FakeLink:
    """A pretend connection. Records writes; answers queries plausibly."""

    def __init__(self, model):
        self.model = model
        self.writes = []
        self.facets = {"bus_control": True, "local": True}
        self.where = self.label = "fake"
        self.host = None
        self.idn = f"FAKE,{model},0,0"
        self.idn_query = "*IDN?"        # Instrument() sets the manifest's
        self.keepalive = "*OPC?"
        self.connected = True
        self.allowed = True
        self.lost = self.refused = False
        self.error = None
        # What every other query answers. The confirmation test runs twice, with
        # "0" and with "1", so a driver cannot pass S15 by refusing for some other
        # reason (a slot that merely LOOKED empty) and never reaching its check.
        self.default = "0"

    def query(self, scpi):
        s = scpi.strip().upper()
        if s in ("*IDN?", self.idn_query.strip().upper()):
            return self.idn
        if s.startswith("SYST:ERR") or s.startswith(":SYST:ERR"):
            return '+0,"No error"'
        if "NAME?" in s:
            return '"FAKE"'
        return self.default

    def write(self, scpi):
        self.writes.append(scpi)
        return True

    def read(self):
        return self.default

    def read_stb(self):
        return int(self.default)

    def poke(self):
        pass

    def connect(self):
        return True

    def go_local(self):
        return True

    def _teardown(self):
        pass

    def forget(self):
        pass


def check_dynamic(folder, m, rep):
    """S12-S16: import the driver and try to break the rules against FakeLink."""
    from .plugins import PluginError, load_driver
    try:
        cls = load_driver(folder, m)
    except PluginError as e:
        rep.add("S12", "MUST", False, str(e))
        return
    rep.add("S12", "MUST", True, "driver imports; manifest and class agree on has_output")
    link = FakeLink(m["model"])
    inst = cls(link, lambda *a: None)
    try:
        inst.link.allowed = True
        inst.enabled = True
        inst.on_connect()
        inst.poll()
        st = inst.status()
        json.dumps(st)
        missing = sorted(BASE_KEYS - set(st))
        rep.add("S13", "MUST", not missing, f"on_connect/poll/status run; status is JSON with the "
                f"core's fields: {missing or 'ok'}")
        doc = m.get("state") if isinstance(m.get("state"), dict) else {}
        real = set(st) - CORE_STATE_KEYS
        undoc, stale = sorted(real - set(doc)), sorted(set(doc) - real)
        for k in sorted(real & set(doc)):
            if doc[k].get("type") == "object" and isinstance(st[k], dict) and isinstance(doc[k].get("fields"), dict):
                undoc += [f"{k}.{f}" for f in sorted(set(st[k]) - set(doc[k]["fields"]))]
                stale += [f"{k}.{f}" for f in sorted(set(doc[k]["fields"]) - set(st[k]))]
        rep.add("S18", "MUST", not undoc and not stale, "the state documented in the manifest is exactly "
                f"what status() reports: undocumented {undoc or 'none'}, documented but absent {stale or 'none'}")
    except Exception as e:
        rep.add("S13", "MUST", False, f"on_connect/poll/status raised {type(e).__name__}: {e}")
        return
    acts = m.get("actions") or {}
    for name, spec in sorted(acts.items()):
        args = dict(spec.get("example") or {})
        args.pop("confirmed", None)
        needs = confirm_required(m, name, args)
        link.default = "0"
        before = len(link.writes)
        try:
            r = inst.action(name, dict(args))
        except Exception as e:
            rep.add("S14", "MUST", False, f"{name}{args} raised {type(e).__name__}: {e}")
            continue
        sent = link.writes[before:]
        okshape = isinstance(r, dict) and isinstance(r.get("ok"), bool)
        rep.add("S14", "MUST", okshape, f"{name} returns {{ok: bool, ...}}: got {r!r:.80}")
        if needs:
            bad = []
            for default in ("0", "1"):
                link.default = default
                before = len(link.writes)
                try:
                    r2 = inst.action(name, dict(args))
                except Exception as e:
                    bad.append(f"answers={default}: raised {type(e).__name__}")
                    continue
                sent2 = link.writes[before:]
                if not (isinstance(r2, dict) and r2.get("ok") is False and not sent2):
                    bad.append(f"answers={default}: ok={r2.get('ok') if isinstance(r2, dict) else r2!r}, sent {sent2}")
            link.default = "0"
            rep.add("S15", "MUST", not bad, f"{name} WITHOUT confirmation refuses and sends nothing"
                    + (f": {'; '.join(bad)}" if bad else " (tried with queries answering 0 and 1)"))
        elif m.get("has_output"):
            # Try it WITH the flag too: a driver that guards itself but whose
            # manifest does not declare the guard has silently lost the core's
            # lock (bench.py gate_action keys off the declaration).
            before = len(link.writes)
            try:
                inst.action(name, dict(args, confirmed=True))
            except Exception:
                pass
            sent_all = sent + link.writes[before:]
            on = [s for s in sent_all if any(p.search(s) for p in OUTPUT_ON_RE)]
            rep.add("S15", "MUST", not on, f"{name} can switch an output on, so it must be declared "
                    f"with confirm in the manifest: {on or 'ok'}" if on else
                    f"{name} sends nothing that switches an output on, with or without the flag")
    before = len(link.writes)
    r = inst.action("__not_an_action__", {})
    rep.add("S14", "MUST", isinstance(r, dict) and r.get("ok") is False and len(link.writes) == before,
            "an unknown action is refused and sends nothing")
    if m.get("has_output"):
        before = len(link.writes)
        try:
            ok = inst.all_off()
        except Exception as e:
            ok = f"raised {type(e).__name__}: {e}"
        sent = link.writes[before:]
        rep.add("S16", "MUST", ok is True and sent, f"all_off() sends the off command(s) and returns "
                f"True: returned {ok!r}, sent {sent}")


def check_sim(folder, m, rep):
    """S20-S21: the simulator model, and the driver run against it."""
    if not m.get("sim"):
        rep.add("S20", "SHOULD", False, "ships a simulator model (sim.py) -- without one the /sim/ "
                "bench uses a generic stand-in that answers 0 to everything")
        return
    from .plugins import load_driver
    from .sim import SimLink, World, load_model_class
    cls = load_driver(folder, m)

    class _P:                                   # just enough of a Plugin for the loader
        manifest, id, rev = m, m["id"], 0
    _P.folder = folder
    mcls, note = load_model_class(_P)
    rep.add("S20", "MUST", note is None, f"sim.py loads and {m['sim']['class']} is a SimModel: {note or 'ok'}")
    if note:
        return
    errs = []
    log = lambda addr, sent, got, kind: errs.append(f"{sent} -> {got}") if kind == "err" else None  # noqa: E731
    world = World()
    model = mcls.__new__(mcls)
    model.MODEL = m["model"]
    mcls.__init__(model, world, "dut")
    world.models["dut"] = model
    # Record every error AS IT IS QUEUED: the driver's own drains (clear_errors, the
    # 33522A's check after each action) empty the queue before we could read it.
    queued = []
    raw_error = model.error
    model.error = lambda code, text: (queued.append(f'{model.last_cmd} -> {code:+d},"{text}"'), raw_error(code, text))
    inst = cls(SimLink(world, "dut", log), log)
    try:
        r = inst.power_on()
        if not r.get("ok"):
            errs.append(f"power_on: {r.get('msg')}")
        inst.poll()
        for name, spec in sorted((m.get("actions") or {}).items()):
            args = dict(spec.get("example") or {}, confirmed=True)
            r = inst.action(name, args)
            if isinstance(r, dict) and r.get("ok") is False and r.get("msg"):
                # A refusal is fine (an empty slot, say) if the model queued no error.
                pass
            inst.poll()
        inst.power_off()
    except Exception as e:
        errs.append(f"raised {type(e).__name__}: {e}")
    errs += queued
    rep.add("S21", "MUST", not errs, "the driver runs against its own model -- power on, poll, every "
            f"action, power off -- with no command either side does not know: {'; '.join(errs[:6]) or 'ok'}")


def check_plugin(folder, dynamic=True):
    """Judge one plugin folder. Returns a Report; report.ok is False on any MUST failure."""
    from .plugins import PluginError, read_manifest
    rep = Report(folder)
    try:
        m = read_manifest(folder)
    except PluginError as e:
        rep.add("S0", "MUST", False, str(e))
        return rep
    rep.add("S0", "MUST", True, "manifest parses; required fields and files present")
    name = os.path.basename(os.path.normpath(folder))
    for prefix in (".new-", ".inspect-"):           # the installer's staging folders
        if name.startswith(prefix):
            name = name[len(prefix):]
    rep.add("S0", "MUST", name == m["id"], f"folder name equals the manifest id ({m['id']})")
    check_manifest_rules(m, rep)
    try:
        check_static(folder, m, rep)
    except (OSError, UnicodeDecodeError) as e:
        rep.add("S7", "MUST", False, f"could not read the plugin files: {e}")
    if dynamic and rep.ok:
        check_dynamic(folder, m, rep)
    if dynamic and rep.ok:
        check_sim(folder, m, rep)
    return rep
