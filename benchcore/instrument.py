#!/usr/bin/env python3
"""The base class every instrument plugin's driver subclasses.

WHAT A DRIVER GETS                   WHAT A DRIVER MUST NOT DO
  self.link.query(scpi) -> str|None    open a socket, a serial port, a VISA session
  self.link.write(scpi) -> bool        know which path it is on (gateway? LAN? USB?)
  self.link.read() -> str|None         (address it to talk, send nothing first: for
                                        instruments that output a reading unasked)
  self.link.read_stb() -> int|None     (a serial poll: a bus operation, not a command)
  self.link.facets                     hard-code any address
  self.state  (its own dict)           special-case another plugin
  self.log(label, sent, got, kind)

WHAT A DRIVER IMPLEMENTS
  poll()                    read the instrument; update self.state. Called every pass
                            while the instrument is ON in the panel.
  action(name, args)        one control from its card. Return {"ok": bool, "msg": ...}.
  status()                  optional; default = base_state() + self.state.
  on_connect()              optional; runs once after *IDN? has matched the manifest.
  on_release()              optional; runs at power-off, after all_off() and before the
                            front panel is handed back. Put back anything on_connect()
                            changed that the operator will expect to find as they left it.
  all_off()                 REQUIRED if the manifest says has_output. Switch every
                            output off and return True only if that was confirmed.
  reset_state()             optional; the default restores self.state as constructed.

The manifest, not the driver, is the authority for the model name, the *IDN? match
(and, for an instrument older than IEEE 488.2, the identify query and keepalive)
and has_output: the loader copies them onto the class, and refuses a driver whose
own has_output disagrees. The core's safety rules (outputs off on every exit path,
ALL OUTPUTS OFF, confirmation flags) key off has_output without knowing the model.

EVERY INSTRUMENT STARTS OFF IN THE PANEL  (2026-09-22, Ernesto's design)
Nothing is contacted until its power button is pressed. power_on checks *IDN? names
the right model BEFORE anything else is sent, and never touches an output. power_off
switches outputs off, hands the front panel back and closes this one link. An
instrument that stops answering is switched OFF in the panel, not retried forever.
"""
import copy
import threading
import time

REFUSED_RETRIES = 3         # extra attempts after a refused TCP connect
REFUSED_WAIT_S = 2.0        # between them


class Instrument:
    # Filled in from manifest.json by the loader -- do not set them in a driver.
    plugin_id = None
    model = "instrument"
    idn_match = ()
    has_output = False
    idn_query = "*IDN?"         # manifest "identify": what power_on asks first
    keepalive = "*OPC?"         # manifest "keepalive": a query, or "serial-poll"

    def __init__(self, link, log):
        self.link = link
        # The link asks the identity question and runs the keepalive, so it has to
        # speak this instrument's dialect for both (a pre-488.2 meter has no *IDN?).
        link.idn_query = self.idn_query
        link.keepalive = self.keepalive
        self.log = log
        self.state = {}
        self.lock = threading.RLock()
        self.enabled = False        # ON in the panel
        self.power_msg = None       # why it is off, when that is worth saying
        self._blank = None          # the state dict as constructed, for resets
        self.data_as_of = None      # server time the last completed poll BEGAN

    # -- hooks --------------------------------------------------------------
    def on_connect(self):
        """Put the instrument in a known state after the model check."""

    def on_release(self):
        """Undo what on_connect changed, before the front panel is handed back."""

    def poll(self):
        raise NotImplementedError

    def action(self, name, args):
        raise NotImplementedError

    def all_off(self):
        """Drivers with has_output MUST override this."""
        return None

    def reset_state(self):
        if self._blank is not None:
            self.state = copy.deepcopy(self._blank)

    def status(self):
        st = self.base_state()
        st.update(self.state)
        return st

    # -- helpers drivers may use -------------------------------------------
    def drain_errors(self, limit=10):
        """Empty the SCPI error queue. Stores the first error in state.last_error."""
        out = []
        for _ in range(limit):
            e = self.link.query("SYST:ERR?")
            if e is None or e.startswith("+0") or e.startswith("0,"):
                break
            out.append(e)
        self.state["last_error"] = out[0] if out else None
        return out

    def matches(self, idn):
        idn = (idn or "").upper()
        return any(m.upper() in idn for m in self.idn_match)

    # -- the panel's power button (core -- drivers do not override) ----------
    def power_on(self):
        with self.lock:
            if self.enabled:
                return {"ok": True}
            if self._blank is None:
                self._blank = copy.deepcopy(self.state)
            self.link.lost = False
            # A REFUSED connection is the far end busy (the 8065 freeing links it
            # has just released), not a silent instrument: wait and try again, up
            # to REFUSED_RETRIES times. The sleep holds only this instrument's lock,
            # never the bus lock, so everything else keeps polling meanwhile.
            ok = self.link.connect()
            for _ in range(REFUSED_RETRIES):
                if ok or not self.link.refused:
                    break
                time.sleep(REFUSED_WAIT_S)
                ok = self.link.connect()
            if not ok:
                if self.link.refused:
                    self.power_msg = (f"{self.link.host or self.link.where} refused the "
                                      f"connection {REFUSED_RETRIES + 1} times -- the gateway "
                                      f"or instrument is busy or wedged, not necessarily off. "
                                      f"Try again in a few seconds.")
                else:
                    self.power_msg = (f"Not answering at {self.link.where} -- is the "
                                      f"{self.model} switched on at the bench?")
                return {"ok": False, "msg": self.power_msg}
            idn = self.link.idn or ""
            if not self.matches(idn):
                self.link._teardown()
                self.power_msg = (f"{self.link.where} answered as \"{idn}\", not a "
                                  f"{self.model} -- left off, nothing was sent to it.")
                self.log(self.link.label, "power", self.power_msg, "err")
                return {"ok": False, "msg": self.power_msg}
            self.link.allowed = True
            try:
                self.on_connect()
            except Exception as e:
                self.log(self.link.label, "on_connect", f"{type(e).__name__}: {e}", "err")
            self.enabled = True
            self.power_msg = None
            self.log(self.link.label, "power", f"{self.model} ON in the panel", "ok")
        try:
            started = time.time()
            self.poll()
            self.data_as_of = started
        except Exception:
            pass
        return {"ok": True}

    def power_off(self):
        """Outputs off, front panel handed back, link closed -- in that order.

        REN is a bus-wide line and is deliberately left asserted: dropping it
        would put every OTHER instrument on the bus into local too (the 34401A
        failure of 2026-09-22).
        """
        self.enabled = False                    # the poll loop skips it from now
        with self.lock:
            out_ok = None
            # `allowed` covers a socket the gateway idle-dropped: _retry rebuilds
            # it, so the output still gets switched off.
            if self.link.connected or self.link.allowed:
                if self.has_output:
                    out_ok = bool(self.all_off())
                try:
                    self.on_release()
                except Exception as e:
                    self.log(self.link.label, "on_release", f"{type(e).__name__}: {e}", "err")
                self.link.go_local()
            self.link.allowed = False
            self.link._teardown()
            self.reset_state()
            self.power_msg = None
            self.log(self.link.label, "power", f"{self.model} OFF in the panel"
                     + ("" if out_ok is None else
                        " (output switched off)" if out_ok else
                        " -- OUTPUT OFF WAS NOT CONFIRMED, check the front panel"),
                     "ok" if out_ok is not False else "err")
        return {"ok": True, "output_off": out_ok}

    def mark_lost(self):
        """It stopped answering mid-session. Switch it off in the panel only."""
        self.enabled = False
        with self.lock:
            self.link.allowed = False
            self.link._teardown()
            self.reset_state()
            self.power_msg = (f"Stopped answering at {time.strftime('%H:%M:%S')} -- "
                              f"switched OFF in the panel."
                              + (" Its output could NOT be switched off from here;"
                                 " check the front panel." if self.has_output else ""))
            self.log(self.link.label, "power", self.power_msg, "err")

    def shutdown(self):
        """Leave it safe. Outputs off is not enough: the panel goes back too.

        Returns power_off's result (or None if it was already off) so the caller
        can report what was CONFIRMED, not what was attempted.
        """
        if self.enabled:
            return self.power_off()
        return None

    def base_state(self):
        return {
            "plugin": self.plugin_id,
            "model": self.model,
            "where": self.link.where,
            "idn": self.link.idn if self.enabled else None,
            "connected": self.link.connected,
            "enabled": self.enabled,
            "power_msg": self.power_msg,
            "error": self.link.error if self.enabled else None,
            "has_output": self.has_output,
            "data_as_of": self.data_as_of if self.enabled else None,
        }
