#!/usr/bin/env python3
"""The simulated bench -- run a script, or poke the cards, with no instrument in reach.

    <panel>/sim/        the same dashboard, driving a SIMULATED copy of this bench

WHAT IS SIMULATED, AND WHAT IS REAL
-----------------------------------
Everything above the wire is REAL: the same plugin drivers, the same core, the same
safety gates, the same Scripts window and runner. Only the Link is swapped: instead
of a socket to a gateway, each instrument's driver talks to an in-memory MODEL of
that instrument (the plugin's own sim.py, see below). So a simulated run exercises
the script AND the driver's SCPI -- a command the model does not know is queued as
-113 "Undefined header" and a query it does not know gets no reply, exactly as a
real instrument would treat a typo.

                   script ──HTTP──> panel core ──> driver ──> SimLink ──> model
                                                                         │
                                                  World: virtual wiring ─┘

THIS FILE HAS NO NETWORK CODE. It imports nothing that moves bytes, and SimBench
never constructs a transport.Link -- tests/test_sim.py proves that by making Link
raise and running every catalog script against the simulator.

MODELS  (a plugin's optional sim.py; manifest "sim": {"file": "sim.py", "class": ...})
------
A model is a SimModel subclass: its power-on state, the SCPI it understands, and --
through PORTS -- what it puts out and what it measures:

    OUTPUTS = {"out": "DC output (+/-)"}           source(port, z_load) -> Signal | None
    INPUTS  = {"in": "HI/LO input"}                z_in(port) -> ohms; world.signal_into(...)

A plugin with no sim.py gets GenericModel: it answers *IDN? with the plugin's model,
remembers what it is told, answers 0 to anything else and measures nothing. Enough
to click through a card; it proves nothing about the driver.

WHAT A MODEL IS NOT
-------------------
An ideal instrument plus a little noise. Its numbers show that a script's LOGIC
works -- the order of its steps, its refusals, its restore -- never how accurate the
real instrument is. Accuracy is only ever measured on the bench.
"""
import json
import math
import os
import random
import re
import threading
import time
from collections import deque

from .bench import Bench, TrafficLog

PORT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}:[a-z0-9_]{1,20}$")
MAX_ERRORS = 20


# -- errors a model raises; handle() turns them into the SCPI error queue -----------

class ScpiError(Exception):
    code, text = -100, "Command error"

    def __init__(self, text=None):
        super().__init__(text or self.text)
        if text:
            self.text = text


class Undefined(ScpiError):
    code, text = -113, "Undefined header"


class OutOfRange(ScpiError):
    code, text = -222, "Data out of range"


class BadData(ScpiError):
    code, text = -104, "Data type error"


class SettingsConflict(ScpiError):
    code, text = -221, "Settings conflict"


# -- parsing helpers models use ----------------------------------------------------------

_MULT = {"G": 1e9, "M": 1e-3, "MA": 1e6, "K": 1e3, "U": 1e-6, "N": 1e-9, "P": 1e-12}
_NUM_RE = re.compile(r"^\s*([-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?)\s*([A-Za-z]*)\s*$")


def num(arg, units=()):
    """'1.5' / '1E6' / '100.000000 MHZ' / '-55.00 DBM' -> float. INF -> inf.

    `units` are the plain unit names this command accepts (e.g. ("HZ",)); a
    prefix on one of them scales it: MHZ -> 1e6, MV -> 1e-3 (SCPI: M is milli,
    MA is mega -- except MHZ, which every instrument reads as megahertz)."""
    if arg is None:
        raise BadData("missing parameter")
    a = arg.strip().strip('"').upper()
    if a in ("INF", "INFINITY", "+INF"):
        return math.inf
    m = _NUM_RE.match(a)
    if not m:
        raise BadData(f"not a number: {arg!r}")
    v, suf = float(m.group(1)), m.group(4)
    if not suf:
        return v
    for u in units:
        if suf == u:
            return v
        if suf.endswith(u):
            p = suf[:-len(u)]
            if u == "HZ" and p == "M":
                return v * 1e6
            if p in _MULT:
                return v * _MULT[p]
    raise BadData(f"unit {suf!r} not accepted")


def onoff(arg):
    a = (arg or "").strip().upper()
    if a in ("ON", "1"):
        return True
    if a in ("OFF", "0"):
        return False
    raise BadData(f"expected ON|OFF, got {arg!r}")


def sci(v, digits=9):
    """A number the way these instruments send it: +1.00000000E+03."""
    if v is None or math.isnan(v):
        return "+9.91000000E+37"
    if math.isinf(v):
        return "+9.90000000E+37"
    return f"{v:+.{digits - 1}E}"


class Signal(dict):
    """What arrives at an input: dc volts, AC rms volts, frequency (Hz or None)."""

    def __init__(self, dc=0.0, vrms=0.0, freq=None, wave=None):
        super().__init__(dc=dc, vrms=vrms, freq=freq, wave=wave)


# -- the model base class ------------------------------------------------------------

class SimModel:
    """One simulated instrument. Subclass it in a plugin's sim.py."""

    MODEL = "SIM"                 # what *IDN? names (the loader fills it from the manifest)
    INPUTS = {}                   # port -> label
    OUTPUTS = {}                  # port -> label
    PRESETS = {}                  # name -> description; apply_preset(name) sets it up
    DEFAULT_PRESET = "power-on"

    def __init__(self, world, iid):
        self.world, self.iid = world, iid
        self.errors = deque()
        self.esr = 0              # standard event status register
        self._opc_pending = False
        self.preset = self.DEFAULT_PRESET
        self.last_cmd = None      # the command being handled (named in error reports)
        self.power_on_reset()

    # -- what subclasses implement ------------------------------------------------
    def power_on_reset(self):
        """The state the instrument comes up in (manual's power-on / *RST state)."""

    def query(self, head, arg):
        """Answer one query (head without the '?', upper case, no leading ':')."""
        raise Undefined()

    def command(self, head, arg):
        """Carry out one command."""
        raise Undefined()

    def source(self, port, z_load):
        """What this instrument drives out of `port` into z_load ohms, or None."""
        return None

    def z_in(self, port):
        """Input impedance of `port`, ohms."""
        return 1e6

    def operation_complete(self):
        """For *OPC: True when the pending operation has finished (default: at once)."""
        return True

    def talk(self):
        """What the instrument sends when addressed to talk with nothing asked (link.read()).
        SCPI instruments send nothing -- the caller times out."""
        raise NoReply("addressed to talk with no query pending")

    def stb(self):
        """The status byte a serial poll returns (link.read_stb())."""
        return 0

    def apply_preset(self, name):
        """Put the instrument in a named starting state (a front-panel situation)."""
        if name not in self.PRESETS and name != self.DEFAULT_PRESET:
            raise ValueError(f"no preset {name!r}")
        self.power_on_reset()
        self.preset = name

    # -- helpers ------------------------------------------------------------------------
    def idn(self):
        return f"SIMULATED,{self.MODEL},SIM{abs(hash(self.iid)) % 100000:05d},sim"

    def error(self, code, text):
        if len(self.errors) < MAX_ERRORS:
            self.errors.append(f'{code:+d},"{text}"')
        else:
            self.errors[-1] = '-350,"Queue overflow"'
        self.esr |= 0x20 if -199 <= code <= -100 else 0x10 if -299 <= code <= -200 else 0x04

    def signal_into(self, port):
        return self.world.signal_into(self.iid, port, self.z_in(port))

    def load_on(self, port):
        return self.world.load_on(self.iid, port)

    # -- the SCPI front end (core -- SCPI models do not override it) ---------------------
    # A model of an instrument older than SCPI (the HP 438A's two-letter program codes)
    # replaces handle() with its own parser. The contract is the same: return the reply
    # text or None, call self.error() for what it does not understand, and raise NoReply
    # for a query it cannot answer.
    def handle(self, line):
        """One line from the driver. -> reply text, or None for no reply."""
        replies = []
        for cmd in _split(line):
            head, _, arg = cmd.partition(" ")
            head = head.strip().lstrip(":").upper()
            arg = arg.strip() or None
            is_q = head.endswith("?")
            h = head.rstrip("?")
            self.last_cmd = cmd
            try:
                r = self._common(h, arg, is_q)
                if r is NotImplemented:
                    r = self.query(h, arg) if is_q else self.command(h, arg)
                if is_q:
                    replies.append(str(r))
            except ScpiError as e:
                self.error(e.code, e.text)
                if is_q:
                    # A real instrument that did not understand a query sends
                    # NOTHING -- the caller times out. Say so to the link.
                    raise NoReply(f"{cmd.strip()} -> {e.code},\"{e.text}\"")
        return ";".join(replies) if replies else None

    def _common(self, h, arg, is_q):
        if h == "*IDN" and is_q:
            return self.idn()
        if h == "*OPC":
            if is_q:
                return "1"
            self._opc_pending = True
            return None
        if h == "*ESR" and is_q:
            if self._opc_pending and self.operation_complete():
                self._opc_pending = False
                self.esr |= 1
            v, self.esr = self.esr, 0
            return str(v)
        if h == "*CLS" and not is_q:
            self.errors.clear()
            self.esr = 0
            return None
        if h in ("SYST:ERR", "SYSTEM:ERROR", "SYST:ERR:NEXT") and is_q:
            return self.errors.popleft() if self.errors else '+0,"No error"'
        if h == "*RST" and not is_q:
            self.power_on_reset()
            return None
        return NotImplemented


class NoReply(Exception):
    pass


def _split(line):
    """Split 'A 1;B?' on semicolons outside quotes."""
    out, cur, q = [], [], None
    for ch in line:
        if q:
            cur.append(ch)
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
            cur.append(ch)
        elif ch == ";":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [c for c in (x.strip() for x in out) if c]


class GenericModel(SimModel):
    """For a plugin with no sim.py: remembers settings, answers 0, measures nothing."""

    def power_on_reset(self):
        self.store = {}

    def query(self, head, arg):
        return self.store.get(head, "0")

    def command(self, head, arg):
        self.store[head] = arg if arg is not None else "1"


# -- the world: every model on the simulated bench, and the wires between them ----------

class World:
    def __init__(self):
        self.models = {}            # iid -> SimModel
        self.wires = []             # [("iid:port", "iid:port")]  (outputs first)
        self.offline = set()        # iids that do not answer (a simulated fault)
        self.lock = threading.RLock()

    def _other_end(self, iid, port):
        me = f"{iid}:{port}"
        for a, b in self.wires:
            if a == me:
                return b
            if b == me:
                return a
        return None

    def signal_into(self, iid, port, z_in):
        """What arrives at iid:port, given its input impedance."""
        with self.lock:
            far = self._other_end(iid, port)
            if not far:
                return None
            fid, fport = far.split(":", 1)
            m = self.models.get(fid)
            if m is None or fid in self.offline or fport not in m.OUTPUTS:
                return None
            return m.source(fport, z_in)

    def load_on(self, iid, port):
        """The impedance hanging on an output (inf when nothing is wired)."""
        with self.lock:
            far = self._other_end(iid, port)
            if not far:
                return math.inf
            fid, fport = far.split(":", 1)
            m = self.models.get(fid)
            return m.z_in(fport) if m and fport in m.INPUTS else math.inf

    def wire(self, a, b):
        """Connect an output to an input. Either end's old wire is taken off first."""
        with self.lock:
            ends = []
            for e in (a, b):
                if not isinstance(e, str) or not PORT_RE.match(e):
                    raise ValueError(f"{e!r} is not instrument:port")
                iid, port = e.split(":", 1)
                m = self.models.get(iid)
                if m is None:
                    raise ValueError(f"no instrument {iid!r} on the simulated bench")
                kind = "out" if port in m.OUTPUTS else "in" if port in m.INPUTS else None
                if kind is None:
                    raise ValueError(f"{iid} has no port {port!r} (it has "
                                     f"{', '.join(list(m.OUTPUTS) + list(m.INPUTS)) or 'none'})")
                ends.append((e, kind))
            kinds = {k for _, k in ends}
            if kinds != {"out", "in"}:
                raise ValueError("a wire joins one OUTPUT to one INPUT")
            out = next(e for e, k in ends if k == "out")
            inp = next(e for e, k in ends if k == "in")
            self.wires = [w for w in self.wires if out not in w and inp not in w]
            self.wires.append((out, inp))

    def unwire(self, end):
        with self.lock:
            self.wires = [w for w in self.wires if end not in w]


# -- the link: the same shape as transport.Link, with no socket ----------------------------

class SimLink:
    """A driver's connection to its model. The driver cannot tell the difference."""

    def __init__(self, world, iid, log):
        self.world, self.iid, self.log = world, iid, log
        self.spec = {"type": "sim"}
        self.type = "sim"
        self.label = f"sim:{iid}"
        self.where = "simulated"
        self.host = None
        self.facets = {"bus_control": False, "local": True}
        self.lock = threading.RLock()
        self.idn = None
        self.idn_query = "*IDN?"    # set from the manifest by Instrument(), as on a real Link
        self.keepalive = "*OPC?"
        self.error = None
        self.last_ok = 0.0
        self.allowed = self.lost = self.refused = False
        self.busy_since = None
        self._open = False

    @property
    def model(self):
        return self.world.models.get(self.iid)

    @property
    def connected(self):
        return self._open

    def connect(self):
        with self.lock:
            self._open = False
            self.refused = False
            m = self.model
            if m is None or self.iid in self.world.offline:
                self.error = "not answering (simulated fault: instrument offline)"
                self.log(self.label, "open", self.error, "err")
                return False
            try:
                self.idn = m.handle(self.idn_query)
            except NoReply as e:
                # It did not answer the identity question: exactly a silent instrument.
                self.error = f"no reply to {self.idn_query}: {e}"
                self.log(self.label, "open", self.error, "err")
                return False
            self._open = True
            self.error = None
            self.last_ok = time.time()
            self.log(self.label, "open", self.idn, "ok")
            return True

    def _exchange(self, scpi, is_query):
        with self.lock:
            if not self._open:
                if not self.allowed or not self.connect():
                    if self.allowed:
                        self.allowed, self.lost = False, True
                    return None
            if self.iid in self.world.offline:
                # Went offline mid-session: exactly like an instrument switched off.
                self.error = "timeout (simulated fault: instrument went offline)"
                self.log(self.label, scpi, self.error, "err")
                self._open = False
                self.allowed, self.lost = False, True
                return None
            try:
                out = self.model.handle(scpi)
            except NoReply as e:
                self.error = f"no reply: {e}"
                self.log(self.label, scpi, f"no reply -- the simulated instrument queued {str(e).split('-> ')[-1]}", "err")
                return None
            self.last_ok = time.time()
            self.error = None
            return out if is_query else True

    def query(self, scpi):
        out = self._exchange(scpi, True)
        if out is not None:
            out = str(out).strip()
            self.log(self.label, scpi, out, "rx")
        return out

    def _bus_op(self, fn, what):
        """read() and read_stb(): no command goes to the model's parser."""
        with self.lock:
            if not self._open and (not self.allowed or not self.connect()):
                return None
            if self.iid in self.world.offline:
                self.error = "timeout (simulated fault: instrument went offline)"
                self.log(self.label, what, self.error, "err")
                self._open = False
                self.allowed, self.lost = False, True
                return None
            try:
                out = fn(self.model)
            except NoReply as e:
                self.error = f"no reply: {e}"
                self.log(self.label, what, f"no reply -- {e}", "err")
                return None
            self.last_ok = time.time()
            self.error = None
            return out

    def read(self):
        out = self._bus_op(lambda m: str(m.talk()).strip(), "(read)")
        if out is not None:
            self.log(self.label, "(read)", out, "rx")
        return out

    def read_stb(self):
        out = self._bus_op(lambda m: int(m.stb()), "(serial poll)")
        if out is not None:
            self.log(self.label, "(serial poll)", f"{out} (0x{out:02X})", "rx")
        return out

    def write(self, scpi):
        ok = self._exchange(scpi, False)
        if ok:
            self.log(self.label, scpi, "", "tx")
        return bool(ok)

    def poke(self):
        pass

    def go_local(self):
        self._open = False
        self.log(self.label, "go to local", "ok (simulated)", "ok")
        return True

    def _teardown(self):
        self._open = False

    def kill_socket(self):
        pass

    def forget(self):
        self._open = False


# -- loading a plugin's model ----------------------------------------------------------------

def load_model_class(plugin):
    """The plugin's SimModel subclass, or GenericModel. -> (cls, note or None)."""
    import importlib.util
    spec = plugin.manifest.get("sim")
    if not spec:
        return GenericModel, "this plugin ships no simulator model -- the generic one answers 0 to everything"
    path = os.path.join(plugin.folder, spec["file"])
    try:
        mspec = importlib.util.spec_from_file_location(f"benchsim_{plugin.id}_{plugin.rev}", path)
        mod = importlib.util.module_from_spec(mspec)
        mspec.loader.exec_module(mod)
        cls = getattr(mod, spec["class"])
        if not (isinstance(cls, type) and issubclass(cls, SimModel)):
            raise TypeError(f"{spec['class']} is not a SimModel")
    except Exception as e:
        return GenericModel, f"its simulator model failed to load ({type(e).__name__}: {e}) -- using the generic one"
    return cls, None


# -- the simulated bench ------------------------------------------------------------------------

class SimSite:
    """A site config that lives in memory: the simulated bench never writes site.json."""

    def __init__(self):
        self.path = None
        self.data = {"schema": 1, "defaults": {}, "instruments": []}

    def load(self):
        pass

    def save(self):
        pass


class SimBench(Bench):
    """The real bench's instruments, with every Link replaced by a SimLink.

    Contents: every instrument on the real bench (same id, same name), plus one of
    every other installed plugin, so any script can be tried. The wiring and each
    instrument's starting preset are kept in sim.json in the data folder.
    """

    simulated = True

    def __init__(self, store, real_site, cfg_path):
        super().__init__(store, SimSite())
        self.real_site = real_site
        self.cfg_path = cfg_path
        self.world = World()
        self.notes = {}             # iid -> why its model is generic
        self._revs = {}             # iid -> plugin rev its instrument was built from
        self.log = TrafficLog()
        self._load_cfg()

    # -- config ------------------------------------------------------------------------------
    def _load_cfg(self):
        self.cfg = {"wires": [], "presets": {}, "offline": []}
        try:
            with open(self.cfg_path, encoding="utf-8") as fh:
                d = json.load(fh)
            if isinstance(d, dict):
                self.cfg.update({k: d[k] for k in self.cfg if k in d})
        except (OSError, ValueError):
            pass

    def save_cfg(self):
        self.cfg["wires"] = [list(w) for w in self.world.wires]
        self.cfg["offline"] = sorted(self.world.offline)
        self.cfg["presets"] = {i: m.preset for i, m in self.world.models.items()}
        if not self.cfg_path:
            return
        tmp = self.cfg_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.cfg, fh, indent=2)
        os.replace(tmp, self.cfg_path)

    # -- contents --------------------------------------------------------------------------------
    def wanted(self):
        """[(iid, name, plugin)] the simulated bench should hold right now."""
        out, seen_plugins, ids = [], set(), set()
        for e in self.real_site.data.get("instruments", []):
            iid, pid = e.get("id"), e.get("plugin")
            if iid and pid:
                out.append((iid, e.get("name") or iid, pid))
                seen_plugins.add(pid)
                ids.add(iid)
        for pid, p in sorted(self.store.plugins.items()):
            if pid in seen_plugins:
                continue
            n = 1
            while f"{pid}-{n}" in ids:
                n += 1
            out.append((f"{pid}-{n}", p.manifest["model"], pid))
            ids.add(f"{pid}-{n}")
        return out

    def sync(self, force=False):
        """Match the real bench and the installed plugins. An instrument whose plugin
        changed or was reloaded (or every one, with force) is rebuilt OFF and its model
        power-cycled; everything else is left exactly as it is."""
        with self.lock:
            want = self.wanted()
            wmap = {i: p for i, _, p in want}
            changed = force or not self._synced
            for iid in set(self.instruments) | set(self.missing):
                pid = self.meta.get(iid, {}).get("plugin")
                plugin = self.store.get(pid)
                if (force or wmap.get(iid) != pid or plugin is None
                        or self._revs.get(iid) != plugin.rev):
                    self._drop(iid)
                    self.missing.pop(iid, None)
                    self.world.models.pop(iid, None)
                    changed = True
            self.site.data["instruments"] = [{"id": i, "plugin": p, "name": n, "connection": {"type": "sim"}}
                                             for i, n, p in want]
            self.meta = {e["id"]: e for e in self.site.data["instruments"]}
            for e in self.site.data["instruments"]:
                if e["id"] not in self.instruments:
                    self._build(e)
                    changed = True
            wires = self.cfg.get("wires", []) if not self._synced else self.world.wires
            self.world.wires = [tuple(w) for w in wires if self._wire_ok(w)]
            off = self.cfg.get("offline", []) if not self._synced else self.world.offline
            self.world.offline = {i for i in off if i in self.world.models}
            self._synced = True
            return changed

    _synced = False

    def _wire_ok(self, w):
        try:
            a, b = w
            return a.split(":")[0] in self.world.models and b.split(":")[0] in self.world.models
        except (ValueError, AttributeError):
            return False

    def _build(self, entry):
        iid = entry["id"]
        self.meta[iid] = entry
        plugin = self.store.get(entry.get("plugin"))
        if plugin is None:
            self.missing[iid] = f"plugin {entry.get('plugin')!r} is not installed"
            return None
        cls, note = load_model_class(plugin)
        if note:
            self.notes[iid] = note
        else:
            self.notes.pop(iid, None)
        model = cls.__new__(cls)
        model.MODEL = plugin.manifest["model"]
        cls.__init__(model, self.world, iid)
        pre = self.cfg.get("presets", {}).get(iid)
        if pre and pre != model.DEFAULT_PRESET:
            try:
                model.apply_preset(pre)
            except ValueError:
                pass
        self.world.models[iid] = model
        self._revs[iid] = plugin.rev
        inst = plugin.cls(SimLink(self.world, iid, self.log), self.log)
        self.instruments[iid] = inst
        self.missing.pop(iid, None)
        return inst

    # -- the simulated bench's own controls (api/sim) ---------------------------------------------
    def describe(self):
        with self.lock:
            out = []
            for e in self.site.data["instruments"]:
                m = self.world.models.get(e["id"])
                inst = self.instruments.get(e["id"])
                out.append({"id": e["id"], "name": e.get("name"), "plugin": e["plugin"],
                            "model": m.MODEL if m else None,
                            "generic": isinstance(m, GenericModel), "note": self.notes.get(e["id"]),
                            "inputs": dict(m.INPUTS) if m else {}, "outputs": dict(m.OUTPUTS) if m else {},
                            "presets": {m.DEFAULT_PRESET: "as the instrument powers up",
                                        **m.PRESETS} if m else {},
                            "preset": m.preset if m else None,
                            "offline": e["id"] in self.world.offline,
                            "on": bool(inst and inst.enabled)})
            return {"instruments": out, "wires": [list(w) for w in self.world.wires]}

    def op(self, req):
        op = req.get("op")
        with self.lock:
            if op == "bench":
                return {"ok": True, **self.describe()}
            if op == "wire":
                self.world.wire(req.get("a"), req.get("b"))
            elif op == "unwire":
                self.world.unwire(str(req.get("end") or ""))
            elif op == "wiring":
                # Replace all wiring at once: a script's declared wiring.
                wires = req.get("wires") or []
                old = list(self.world.wires)
                self.world.wires = []
                try:
                    for a, b in wires:
                        self.world.wire(a, b)
                except (ValueError, TypeError) as e:
                    self.world.wires = old
                    raise ValueError(str(e))
            elif op == "preset":
                iid = req.get("instrument")
                m = self.world.models.get(iid)
                if m is None:
                    raise ValueError(f"no instrument {iid!r}")
                self._off(iid)
                m.apply_preset(str(req.get("preset")))
            elif op == "power_cycle":
                iid = req.get("instrument")
                m = self.world.models.get(iid)
                if m is None:
                    raise ValueError(f"no instrument {iid!r}")
                self._off(iid)
                m.apply_preset(m.DEFAULT_PRESET)
            elif op == "offline":
                iid = req.get("instrument")
                if iid not in self.world.models:
                    raise ValueError(f"no instrument {iid!r}")
                (self.world.offline.add if req.get("value") else self.world.offline.discard)(iid)
            else:
                return {"ok": False, "msg": f"unknown op {op!r}"}
            self.save_cfg()
            return {"ok": True, **self.describe()}

    def shutdown(self):
        """Nothing real to release -- stop the poller quietly."""
        self.running = False
        if self._thread:
            self._thread.join(timeout=4)

    def _off(self, iid):
        inst = self.instruments.get(iid)
        if inst is not None and inst.enabled:
            inst.power_off()

    def resolve_wiring(self, pairs):
        """A script header's wiring ("plugin:port") -> this bench's "iid:port"."""
        first = {}
        for e in self.site.data["instruments"]:
            first.setdefault(e["plugin"], e["id"])
        out = []
        for pair in pairs or []:
            ends = []
            for end in pair:
                pid, port = end.split(":", 1)
                iid = first.get(pid)
                if iid is None:
                    raise ValueError(f"no {pid} on the simulated bench")
                ends.append(f"{iid}:{port}")
            out.append(ends)
        return out


# -- noise helper for models ------------------------------------------------------------------------

def noisy(value, sd):
    return value + random.gauss(0.0, sd) if sd else value
