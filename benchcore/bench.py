#!/usr/bin/env python3
"""The bench: which instruments are on it, where they are, and the poll loop.

THE SITE CONFIG  (site.json -- never in the repository)
------------------------------------------------------
Everything specific to one bench lives here and nowhere else: which plugins are in
use, what each instance is called, and how each one is reached. Plugins describe a
MODEL; they never carry an address (plan §9.2 -- git history is forever).

    {
      "schema": 1,
      "defaults": {"gateway": "10.0.0.60"},
      "instruments": [
        {"id": "e3641a-1", "plugin": "e3641a", "name": "E3641A",
         "connection": {"type": "gpib-gateway", "gateway": "10.0.0.60", "address": 7}}
      ]
    }

An empty or missing site.json is a bench with no instruments. That is a normal
state, not an error: the dashboard opens empty and offers to add one.

The dashboard's Instruments manager edits this file. Every write is atomic (temp
file + rename) and keeps the previous version as site.json.bak.
"""
import json
import os
import re
import threading
import time

from . import conformance, transport

POLL_S = 0.5
LOG_MAX = 300
SITE_SCHEMA = 1
NAME_MAX = 40

_IID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")


class TrafficLog:
    """Ring buffer of bus traffic, with a monotonic sequence number.

    The sequence matters: the snapshot returns only the LAST 120 entries, so once
    the buffer is full its LENGTH never changes again. A client that re-renders on
    a length change stops updating at entry 120 and quietly shows stale traffic.
    """

    def __init__(self):
        self._log, self.seq = [], 0
        self._lock = threading.Lock()

    def __call__(self, addr, sent, got, kind):
        with self._lock:
            self.seq += 1
            self._log.append({"t": time.strftime("%H:%M:%S"), "addr": addr,
                              "sent": sent, "got": got, "kind": kind})
            del self._log[:-LOG_MAX]

    def tail(self, n=120):
        with self._lock:
            return list(self._log[-n:]), self.seq


class Site:
    def __init__(self, path):
        self.path = path
        self.data = {"schema": SITE_SCHEMA, "defaults": {}, "instruments": []}
        self.problems = []

    def load(self):
        if not os.path.exists(self.path):
            return
        with open(self.path, encoding="utf-8") as fh:
            d = json.load(fh)
        if d.get("schema") != SITE_SCHEMA:
            raise ValueError(f"{self.path}: schema must be {SITE_SCHEMA}")
        d.setdefault("defaults", {})
        d.setdefault("instruments", [])
        self.data = d

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2)
            fh.write("\n")
        if os.path.exists(self.path):
            os.replace(self.path, self.path + ".bak")
        os.replace(tmp, self.path)


class Bench:
    def __init__(self, store, site):
        self.store = store
        self.site = site
        self.log = TrafficLog()
        self.instruments = {}       # instance id -> Instrument
        self.meta = {}              # instance id -> its site.json entry
        self.missing = {}           # instance id -> why it could not be built
        self.running = True
        self.poll_enabled = True
        self.lock = threading.RLock()
        self._thread = None

    # -- building instances from the site config ----------------------------
    def build_all(self):
        with self.lock:
            self.instruments, self.meta, self.missing = {}, {}, {}
            for entry in self.site.data["instruments"]:
                self._build(entry)

    def _build(self, entry):
        iid = entry.get("id")
        self.meta[iid] = entry
        plugin = self.store.get(entry.get("plugin"))
        if plugin is None:
            # Keep the entry: the plugin may just need reinstalling or updating.
            # Say which -- "not installed" is wrong for one that failed to load.
            why = self.store.broken.get(entry.get("plugin"))
            if why and len(why) > 160:
                why = why[:160].rsplit(";", 1)[0] + "; …"   # cut at a whole item
            self.missing[iid] = (f"plugin {entry.get('plugin')!r} did not load ({why}) -- "
                                 "update it from the catalog" if why else
                                 f"plugin {entry.get('plugin')!r} is not installed")
            return None
        spec, err = transport.validate(entry.get("connection"))
        if err:
            self.missing[iid] = err
            return None
        if spec["type"] not in plugin.manifest["connections"]:
            self.missing[iid] = (f"the {plugin.manifest['model']} plugin does not support "
                                 f"{spec['type']} connections")
            return None
        inst = plugin.cls(transport.Link(spec, self.log), self.log)
        self.instruments[iid] = inst
        self.missing.pop(iid, None)
        return inst

    def _drop(self, iid):
        inst = self.instruments.pop(iid, None)
        if inst is not None:
            inst.shutdown()
            inst.link.forget()

    def busy_plugins(self):
        """Plugins with an instrument that is ON in the panel right now."""
        return {i.plugin_id for i in self.instruments.values() if i.enabled}

    def bench_plugins(self):
        """Plugin ids of every instrument on the bench, ON or not (for a script's 'uses')."""
        with self.lock:
            return sorted({m.get("plugin") for m in self.meta.values() if m.get("plugin")})

    def users_of(self, pid):
        return [m.get("name") or iid for iid, m in self.meta.items() if m.get("plugin") == pid]

    def rebuild_plugin(self, pid):
        """After an install or upgrade: rebuild every instance that uses it, OFF."""
        with self.lock:
            for iid, entry in list(self.meta.items()):
                if entry.get("plugin") == pid:
                    self._drop(iid)
                    self._build(entry)

    # -- the Instruments manager ---------------------------------------------
    def add(self, plugin_id, name, connection):
        with self.lock:
            plugin = self.store.get(plugin_id)
            if plugin is None:
                return {"ok": False, "msg": f"plugin {plugin_id!r} is not installed"}
            spec, err = transport.validate(connection)
            if err:
                return {"ok": False, "msg": err}
            if spec["type"] not in plugin.manifest["connections"]:
                return {"ok": False, "msg": f"the {plugin.manifest['model']} plugin supports "
                        f"{', '.join(plugin.manifest['connections'])} only"}
            for iid, m in self.meta.items():
                other, _ = transport.validate(m.get("connection"))
                if other and transport.same_endpoint(spec, other):
                    return {"ok": False, "msg": f"{m.get('name') or iid} is already at "
                            f"{transport.describe(spec)}"}
            name = (str(name or "").strip() or plugin.manifest["model"])[:NAME_MAX]
            iid = self._new_id(plugin_id)
            entry = {"id": iid, "plugin": plugin_id, "name": name, "connection": spec}
            self.site.data["instruments"].append(entry)
            if spec["type"] == "gpib-gateway":
                self.site.data["defaults"]["gateway"] = spec["gateway"]
            self.site.save()
            self._build(entry)
            self.log("panel", "add", f"{name} ({plugin_id}) at {transport.describe(spec)}", "ok")
            return {"ok": True, "id": iid}

    def remove(self, iid):
        """Outputs off, released, unloaded, gone from site.json."""
        with self.lock:
            if iid not in self.meta:
                return {"ok": False, "msg": "no such instrument"}
            inst = self.instruments.get(iid)
            out_ok = None
            if inst is not None and inst.enabled:
                out_ok = inst.power_off().get("output_off")
            self._drop(iid)
            name = self.meta.pop(iid).get("name") or iid
            self.missing.pop(iid, None)
            self.site.data["instruments"] = [e for e in self.site.data["instruments"]
                                             if e.get("id") != iid]
            self.site.save()
            self.log("panel", "remove", name, "ok")
            msg = None if out_ok is not False else \
                "Its output could NOT be confirmed off -- check the front panel."
            return {"ok": True, "msg": msg}

    def rename(self, iid, name):
        with self.lock:
            if iid not in self.meta:
                return {"ok": False, "msg": "no such instrument"}
            name = str(name or "").strip()[:NAME_MAX]
            if not name:
                return {"ok": False, "msg": "a name is required"}
            self.meta[iid]["name"] = name
            self.site.save()
            return {"ok": True}

    def move(self, iid, delta):
        """Reorder the cards. The order in site.json is the order on screen."""
        with self.lock:
            lst = self.site.data["instruments"]
            idx = next((n for n, e in enumerate(lst) if e.get("id") == iid), None)
            if idx is None:
                return {"ok": False, "msg": "no such instrument"}
            j = max(0, min(len(lst) - 1, idx + int(delta)))
            lst.insert(j, lst.pop(idx))
            self.site.save()
            return {"ok": True}

    def _new_id(self, pid):
        n = 1
        while f"{pid}-{n}" in self.meta:
            n += 1
        iid = f"{pid}-{n}"
        assert _IID_RE.match(iid)
        return iid

    # -- running --------------------------------------------------------------
    def start(self, poll=True):
        # Nothing is contacted here. Every instrument starts OFF in the panel.
        self.poll_enabled = poll
        self._thread = threading.Thread(target=self._loop, name="poller", daemon=True)
        self._thread.start()

    def _loop(self):
        while self.running:
            with self.lock:
                insts = list(self.instruments.values())
            for inst in insts:
                if not self.running:
                    break
                if not inst.enabled:            # OFF in the panel: never contacted
                    continue
                try:
                    if self.poll_enabled:
                        # Stamp when the poll BEGAN. Polls and actions both hold the
                        # instrument's lock, so a poll that began after an action
                        # ended must have seen its effect -- that is what lets a
                        # script ask for a reading that is truly fresh.
                        started = time.time()
                        inst.poll()
                        inst.data_as_of = started
                    else:
                        # Not polling is exactly when the gateway's idle timer bites.
                        inst.link.poke()
                except Exception as e:                      # never kill the thread
                    self.log(inst.link.label, "poll", f"{type(e).__name__}: {e}", "err")
                if inst.enabled and inst.link.lost:
                    inst.mark_lost()
            time.sleep(POLL_S)

    def snapshot(self):
        cards = []
        with self.lock:
            order = [e.get("id") for e in self.site.data["instruments"]]
            for iid in order:
                entry = self.meta.get(iid, {})
                inst = self.instruments.get(iid)
                plugin = self.store.get(entry.get("plugin"))
                card = {"id": iid, "name": entry.get("name") or iid,
                        "plugin": entry.get("plugin"),
                        "plugin_rev": plugin.rev if plugin else None}
                if inst is None:
                    spec, _ = transport.validate(entry.get("connection"))
                    card.update(missing=self.missing.get(iid, "not loaded"),
                                where=transport.describe(spec) if spec else "?",
                                enabled=False)
                else:
                    try:
                        card.update(inst.status())
                    except Exception as e:
                        card.update(inst.base_state(), error=str(e))
                cards.append(card)
        tail, seq = self.log.tail()
        return {"instruments": cards, "log": tail, "log_seq": seq,
                "polling": self.poll_enabled, "time": time.strftime("%H:%M:%S"),
                "now": time.time()}

    def action(self, iid, action, args):
        """Run one action. Every reply carries `at`, the server time it finished:
        a reading whose data_as_of is later than `at` reflects the action."""
        r = self._action(iid, action, args)
        if isinstance(r, dict):
            r["at"] = time.time()
        return r

    def _action(self, iid, action, args):
        with self.lock:
            inst = self.instruments.get(iid)
        if inst is None:
            return {"ok": False, "msg": self.missing.get(iid, "unknown instrument")}
        if action == "power_on":
            return inst.power_on()
        if action == "power_off":
            return inst.power_off()
        if not inst.enabled:
            return {"ok": False, "msg": f"The {inst.model} is OFF in the panel -- "
                    "press its power button first."}
        args = args or {}
        # The core enforces the manifest's declarations itself: an undeclared action
        # never reaches the driver, and one declared as needing confirmation is
        # refused here without it -- a second lock, independent of the driver's own.
        plugin = self.store.get(inst.plugin_id)
        refusal = conformance.gate_action(plugin.manifest, action, args) if plugin else "plugin not loaded"
        if refusal:
            return {"ok": False, "msg": refusal}
        try:
            return inst.action(action, args)
        except Exception as e:
            return {"ok": False, "msg": f"{type(e).__name__}: {e}"}

    def all_outputs_off(self):
        done = []
        with self.lock:
            items = list(self.instruments.items())
        for iid, inst in items:
            if inst.has_output:
                name = self.meta.get(iid, {}).get("name") or iid
                if inst.enabled:
                    done.append({"instrument": name, "ok": bool(inst.all_off())})
                else:
                    done.append({"instrument": name, "ok": None,
                                 "msg": "off in the panel -- check its front panel"})
        return done

    def shutdown(self):
        if not self.running:
            return
        self.running = False
        if self._thread:
            self._thread.join(timeout=4)
        print("\n  leaving the bench safe ...")
        with self.lock:
            items = list(self.instruments.items())
        for iid, inst in items:
            if not inst.enabled:
                continue                        # never connected -- nothing to release
            try:
                r = inst.shutdown() or {}
                out = r.get("output_off")
                print(f"    {iid:16s} " + ("released (no outputs)" if out is None else
                                           "output off + released" if out else
                                           "!! OUTPUT OFF NOT CONFIRMED -- check its front panel"))
            except Exception as e:
                print(f"    {iid:16s} !! {type(e).__name__}: {e}")
        print("  done.\n")
