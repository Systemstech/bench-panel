#!/usr/bin/env python3
"""Transports -- the ONLY code in the bench panel that moves bytes.

A plugin never opens a socket and never knows which path it is on (plan §3.6).
It gets a `Link` and calls query / write / go_local on it. Which kind of Link it
gets is decided by the SITE CONFIG, never by the plugin: move an instrument from
a GPIB gateway to LAN and nothing in its plugin changes.

THREE KINDS, ONE INTERFACE
--------------------------
  gpib-gateway  a GPIB instrument behind a VXI-11 LAN/GPIB gateway (ICS 8065 and
                the like). Device "gpib0,<addr>". Every link through the same
                gateway shares ONE bus lock, and REN is asserted on connect.
                  {"type": "gpib-gateway", "gateway": "10.0.0.60", "address": 7}
  vxi11         a LAN instrument that speaks VXI-11 itself (LXI).
                  {"type": "vxi11", "host": "10.0.0.5"}           device inst0
  visa          any other VISA resource string pyvisa-py can open: USBTMC,
                raw socket (::5025::SOCKET), serial (ASRL/dev/ttyUSB0::INSTR).
                  {"type": "visa", "resource": "USB0::0x0957::0x0407::MY123::INSTR"}

Each reports its FACETS, so a driver can ask rather than assume (plan §3.7):
  bus_control   REN / device_local exist (gpib-gateway, vxi11)
  local         the front panel can be handed back

Everything below is a consequence of something the bench has already taught us:

THE 120-SECOND IDLE DROP  -- the 8065 closes an idle link at ~120 s (measured:
  105 s OK, 125 s ConnectionReset). Every Link runs a keepalive and rebuilds itself
  once on a drop.

ONE TRANSACTION AT A TIME  -- GPIB is a shared bus and the 8065 leaks slots when
  links pile up. Every exchange holds the link's lock -- shared per gateway -- and
  there is exactly one link per instrument while it is ON in the panel.

RELEASE IS `device_local`, NOT `SYST:LOC`  -- proven on the bench 2026-09-22:
  `SYST:LOC` over GPIB queues +514 "Command allowed only with RS-232" on BOTH the
  E3641A and the 34401A, and pyvisa's `control_ren(address_gtl)` raises VisaIOError
  against this gateway. The route that works is VXI-11 device_local through a
  CoreClient.

CREATE_LINK PROVES NOTHING  -- it is gateway-local and succeeds with the bus empty.
  `*IDN?` is the first call that actually addresses the device. (Or the plugin's own
  identify query: an instrument older than IEEE 488.2 has no *IDN? -- the HP 438A
  answers ?ID. Its keepalive is chosen by the plugin for the same reason.)

NOTHING IS CONTACTED UNTIL THE POWER BUTTON  -- a Link only reconnects on its own
  while `allowed` is set. If a powered-on instrument stops answering *IDN? it is
  marked `lost` and switched OFF in the panel rather than retried forever.
"""
import socket
import threading
import time

import pyvisa
import pyvisa_py.protocols.vxi11 as vxi11

KEEPALIVE_S = 30.0          # comfortably inside the ~120 s idle drop
IDN_TIMEOUT_MS = 2000       # at connect: a real instrument answers in < 0.2 s
IO_TIMEOUT_MS = 5000        # every exchange after that
WATCHDOG_GRACE_S = 8.0      # how far past its own timeout a call may run

TYPES = ("gpib-gateway", "vxi11", "visa")

# ONE BUS, ONE LOCK  (2026-09-22)
# Every Link through the same gateway shares a lock, so only one transaction is
# ever on that bus. Per-link locks allowed the poll thread and a power-button
# thread to overlap: an 8648A power_on (interface REN docmd + *IDN? to an empty
# address) ran while the poller's 34401A READ? was in flight, and that READ? never
# returned -- pyvisa-py sat in its receive loop indefinitely and the whole panel
# stopped updating. Keyed by gateway host, so two gateways are two buses.
_BUS_LOCKS = {}
_BUS_LOCKS_GUARD = threading.Lock()


def bus_lock(gateway):
    with _BUS_LOCKS_GUARD:
        return _BUS_LOCKS.setdefault(gateway, threading.RLock())


# THE WATCHDOG
# Serialising the bus removes the known trigger, not every possible hang. If any
# exchange runs WATCHDOG_GRACE_S past its own timeout, the watchdog shuts that
# link's socket from outside. The blocked call then fails, _retry rebuilds the
# link once, and the panel carries on instead of freezing.
_LINKS = []
_LINKS_GUARD = threading.Lock()


def _watchdog():
    while True:
        time.sleep(1.0)
        now = time.time()
        with _LINKS_GUARD:
            links = list(_LINKS)
        for link in links:
            t0 = link.busy_since
            if t0 and now - t0 > link.timeout_s + WATCHDOG_GRACE_S:
                link.busy_since = None
                link.log(link.label, "watchdog",
                         f"exchange hung {now - t0:.0f}s -- socket closed", "err")
                link.kill_socket()


threading.Thread(target=_watchdog, name="bus-watchdog", daemon=True).start()

# ONE RESOURCE MANAGER, NEVER CLOSED  (2026-09-22)
# pyvisa returns the SAME ResourceManager object for every ResourceManager("@py")
# call, and its close() "will also terminate connections obtained from other
# ResourceManager instances" (pyvisa's own docstring). Each Link used to close
# "its" manager on teardown -- which silently killed every OTHER instrument's
# session too. Proven 2026-09-22: switching the E3641A off in the panel produced
# InvalidSession on the 34401A's very next READ?. A Link closes only its own
# resource.
_RM = None


def _resource_manager():
    global _RM
    if _RM is None:
        _RM = pyvisa.ResourceManager("@py")
    return _RM


def _host(text):
    """A host as typed, with a dotted IPv4 address made canonical.

    The C resolver reads a leading zero as OCTAL: "192.0.2.010" connects to .8,
    not .10, with no error. People mean decimal, so decimal it is. Returns
    (host, error)."""
    host = str(text or "").strip()
    parts = host.split(".")
    if len(parts) == 4 and all(x.isdigit() for x in parts):
        nums = [int(x, 10) for x in parts]
        if any(n > 255 for n in nums):
            return None, f"{host} is not an IPv4 address (each part is 0-255)"
        host = ".".join(map(str, nums))
    return host, None


def validate(spec):
    """Check a connection spec from the site config. Returns (clean_spec, error)."""
    if not isinstance(spec, dict):
        return None, "connection must be an object"
    t = spec.get("type")
    if t == "gpib-gateway":
        gw, err = _host(spec.get("gateway"))
        if err:
            return None, err
        try:
            addr = int(spec.get("address"))
        except (TypeError, ValueError):
            return None, "GPIB address must be a number 0-30"
        if not gw:
            return None, "gateway host is required"
        if not 0 <= addr <= 30:
            return None, "GPIB address must be 0-30"
        return {"type": t, "gateway": gw, "address": addr}, None
    if t == "vxi11":
        host, err = _host(spec.get("host"))
        if err:
            return None, err
        if not host:
            return None, "host is required"
        dev = str(spec.get("device") or "inst0").strip()
        return {"type": t, "host": host, "device": dev}, None
    if t == "visa":
        res = str(spec.get("resource") or "").strip()
        if not res:
            return None, "VISA resource string is required"
        return {"type": t, "resource": res}, None
    return None, f"unknown connection type {t!r} (use one of {', '.join(TYPES)})"


def describe(spec):
    """Short human text for where an instrument is -- shown on its card."""
    t = spec.get("type")
    if t == "gpib-gateway":
        return f"gpib0,{spec['address']} via {spec['gateway']}"
    if t == "vxi11":
        return spec["host"] if spec.get("device", "inst0") == "inst0" \
            else f"{spec['host']} {spec['device']}"
    return spec.get("resource", "?")


def same_endpoint(a, b):
    """True if two specs would talk to the same instrument."""
    if a.get("type") != b.get("type"):
        return False
    if a["type"] == "gpib-gateway":
        return a["gateway"] == b["gateway"] and a["address"] == b["address"]
    if a["type"] == "vxi11":
        return a["host"] == b["host"] and a.get("device") == b.get("device")
    return a["resource"] == b["resource"]


class Link:
    """One session to one instrument, kept alive and self-healing."""

    def __init__(self, spec, log):
        self.spec = spec
        self.type = spec["type"]
        self.log = log
        self.label = describe(spec)             # what the traffic log shows
        self.where = self.label
        if self.type == "gpib-gateway":
            self.host, self.device = spec["gateway"], f"gpib0,{spec['address']}"
            self.lock = bus_lock(self.host)
            self.resource = f"TCPIP0::{self.host}::{self.device}::INSTR"
            self.label = f"gpib{spec['address']}"
        elif self.type == "vxi11":
            self.host, self.device = spec["host"], spec.get("device", "inst0")
            self.lock = threading.RLock()
            self.resource = f"TCPIP0::{self.host}::{self.device}::INSTR"
        else:
            self.host = self.device = None
            self.lock = threading.RLock()
            self.resource = spec["resource"]
        self.facets = {
            "bus_control": self.type == "gpib-gateway",
            "local": self.type in ("gpib-gateway", "vxi11"),
        }
        self._inst = None
        self.idn = None
        self.idn_query = "*IDN?"    # set from the plugin's manifest by Instrument()
        self.keepalive = "*OPC?"    # likewise: a harmless query, or "serial-poll"
        self.last_ok = 0.0
        self.error = None
        self.allowed = False        # may reconnect by itself -- set by power_on
        self.lost = False           # a reconnect found nobody answering
        self.refused = False        # the last connect was REFUSED by the far end
        self.busy_since = None      # set while an exchange is on the wire
        self.timeout_s = IO_TIMEOUT_MS / 1000.0
        with _LINKS_GUARD:
            _LINKS.append(self)

    def forget(self):
        """Drop this link from the watchdog -- the instrument was removed."""
        self._teardown()
        with _LINKS_GUARD:
            if self in _LINKS:
                _LINKS.remove(self)

    # -- connection ---------------------------------------------------------
    def assert_ren(self):
        """Assert REN at the gateway INTERFACE before opening a device link.

        Added 2026-09-22 after the 34401A sat in a restart loop for ten minutes:
        `open` succeeded and returned its *IDN? every time, then `READ?` timed out
        at 5 s, forever. Its error queue had the answer:

            SYST:ERR?  ->  +550,"Command not allowed in local"

        REN had been dropped at the interface by another script's clean exit, which
        releases the WHOLE BUS to local. A 34401A in local answers *IDN? happily and
        refuses READ?, so it looks like a dead measurement. The panel asserts REN
        itself on every connect instead of assuming it.
        """
        core = link = None
        try:
            core = vxi11.CoreClient(self.host, open_timeout=8000)
            err, link, _, _ = core.create_link(0, 0, 8000, "gpib0")
            if err:
                return False
            core.device_docmd(link, 0, 8000, 8000, 0x020003, 1, 1, b"\x01")
            return True
        except Exception:
            return False
        finally:
            try:
                if link is not None:
                    core.destroy_link(link)
            except Exception:
                pass
            try:
                if core is not None:
                    core.close()
            except Exception:
                pass

    def connect(self):
        with self.lock:
            self._teardown()
            if self.facets["bus_control"]:
                self.assert_ren()
            try:
                self._inst = _resource_manager().open_resource(self.resource,
                                                               open_timeout=10000)
                self._inst.timeout = IDN_TIMEOUT_MS
                self._inst.read_termination = "\n"
                self._inst.write_termination = "\n"
                self.timeout_s = IDN_TIMEOUT_MS / 1000.0
                self.busy_since = time.time()
                try:
                    self.idn = self._inst.query(self.idn_query).strip()
                finally:
                    self.busy_since = None
                    self.timeout_s = IO_TIMEOUT_MS / 1000.0
                self._inst.timeout = IO_TIMEOUT_MS
                self.last_ok = time.time()
                self.error = None
                self.log(self.label, "open", self.idn, "ok")
                return True
            except Exception as e:
                # Refused is the gateway (or LAN instrument) turning the TCP
                # connection away -- NOT a silent instrument. Seen 2026-09-23: the
                # 8065 refused a connect ~15 s after four device_local releases,
                # then accepted again. The caller retries that case; see power_on.
                self.refused = isinstance(e, ConnectionRefusedError)
                self.error = f"{type(e).__name__}: {e}"
                self.log(self.label, "open", self.error, "err")
                self._teardown()
                return False
            finally:
                if self._inst is not None:
                    self.refused = False

    def _teardown(self):
        """Close THIS instrument's session only -- never the shared manager."""
        try:
            if self._inst is not None:
                self._inst.close()
        except Exception:
            pass
        self._inst = None

    def kill_socket(self):
        """Called by the watchdog, from OUTSIDE the lock, to unstick a call."""
        try:
            sess = _resource_manager().visalib.sessions[self._inst.session]
            sess.interface.sock.shutdown(socket.SHUT_RDWR)
        except Exception as e:
            self.log(self.label, "watchdog", f"could not close socket: {e}", "err")

    @property
    def connected(self):
        return self._inst is not None

    # -- exchanges ----------------------------------------------------------
    def _retry(self, fn, what):
        """Run fn; on failure rebuild the link ONCE and retry.

        The retry exists for exactly one fault -- the idle drop -- where the
        instrument is fine and only the socket died. A command that fails twice
        is a real failure, and hiding it would be worse than reporting it.
        """
        with self.lock:
            for attempt in (1, 2):
                if not self.connected:
                    # Only a powered-on instrument may reconnect by itself. When the
                    # reconnect fails nothing answered *IDN? -- a dead instrument, not
                    # a dropped socket -- so stop and let the panel switch it off.
                    if not self.allowed:
                        return None
                    if not self.connect():
                        self.allowed = False
                        self.lost = True
                        return None
                try:
                    self.busy_since = time.time()
                    try:
                        out = fn(self._inst)
                    finally:
                        self.busy_since = None
                    self.last_ok = time.time()
                    self.error = None
                    return out
                except Exception as e:
                    self.error = f"{type(e).__name__}: {e}"
                    self.log(self.label, what, self.error,
                             "warn" if attempt == 1 else "err")
                    self._teardown()
            return None

    def query(self, scpi):
        out = self._retry(lambda i: i.query(scpi).strip(), scpi)
        if out is not None:
            self.log(self.label, scpi, out, "rx")
        return out

    def write(self, scpi):
        ok = self._retry(lambda i: (i.write(scpi), True)[1], scpi)
        if ok:
            self.log(self.label, scpi, "", "tx")
        return bool(ok)

    def read(self):
        """Address the instrument to talk and take what it sends, sending nothing first.

        For instruments that put a reading on the bus unasked (the HP 438A in free run
        outputs its latest measurement whenever it is addressed to talk)."""
        out = self._retry(lambda i: i.read().strip(), "read")
        if out is not None:
            self.log(self.label, "(read)", out, "rx")
        return out

    def read_stb(self):
        """Serial poll: the status byte, or None. A bus operation, not a program code,
        so it does not disturb an instrument mid-ZERO the way a command would."""
        out = self._retry(lambda i: int(i.read_stb()), "serial poll")
        if out is not None:
            self.log(self.label, "(serial poll)", f"{out} (0x{out:02X})", "rx")
        return out

    def poke(self):
        """Keep the socket warm without changing any instrument state."""
        if self.connected and (time.time() - self.last_ok) > KEEPALIVE_S:
            if self.keepalive == "serial-poll":
                self._retry(lambda i: i.read_stb(), "keepalive")
            else:
                self._retry(lambda i: i.query(self.keepalive), "keepalive")

    # -- release ------------------------------------------------------------
    def go_local(self):
        """Hand the front panel back, by whatever this path offers.

        VXI-11 paths use device_local (see the module docstring). A generic VISA
        path tries pyvisa's GTL and says honestly when the path has none.
        """
        with self.lock:
            if self.type == "visa":
                try:
                    if self._inst is not None:
                        self._inst.control_ren(pyvisa.constants.RENLineOperation.address_gtl)
                        self.log(self.label, "go to local", "ok", "ok")
                        return True
                except Exception as e:
                    self.log(self.label, "go to local",
                             f"not offered on this path ({type(e).__name__})", "warn")
                finally:
                    self._teardown()
                return False
            self._teardown()
            core = link = None
            try:
                core = vxi11.CoreClient(self.host, open_timeout=8000)
                err, link, _, _ = core.create_link(0, 0, 8000, self.device)
                if err:
                    self.log(self.label, "device_local", f"create_link err={err}", "err")
                    return False
                e = core.device_local(link, 0, 8000, 0)
                self.log(self.label, "device_local", f"-> {e}", "ok" if e == 0 else "err")
                return e == 0
            except Exception as e:
                self.log(self.label, "device_local", f"{type(e).__name__}: {e}", "err")
                return False
            finally:
                try:
                    if link is not None:
                        core.destroy_link(link)
                except Exception:
                    pass
                try:
                    if core is not None:
                        core.close()
                except Exception:
                    pass
