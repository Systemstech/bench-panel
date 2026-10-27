#!/usr/bin/env python3
"""benchpanel -- drive a running BenchPanel from a Python script.

One file, standard library only: copy it next to your script, or put this
folder on PYTHONPATH.

    from benchpanel import Bench

    bench = Bench("http://127.0.0.1:8765/")            # same machine, or an SSH tunnel
    with bench.session("34401a-1") as s:                # ON now, OFF + released on exit
        print(s.read("34401a-1")["reading"])            # a reading taken after this call

WHAT IT GIVES YOU
-----------------
  state() / get(id)       everything the dashboard shows, as dicts
  act(id, action, **args) one control; raises BenchError if the panel refuses
  read(id)                a reading from a poll that BEGAN after this call (or after
                          your last act() on it) -- never the stale one
  wait_for(id, test)      poll until test(state) is true, or time out
  session(*ids)           power instruments on for a block of code, and guarantee
                          they are powered off (outputs off, panel handed back) when
                          it ends -- including on an exception or Ctrl-C
  all_outputs_off()       the header's big red button

THE SAME RULES AS THE PAGE
--------------------------
The panel owns the instrument links and the GPIB bus lock; a script only asks.
Undeclared actions are refused, and anything that can switch an output on needs
confirmed=True -- pass it deliberately, never by default.

RUN FROM THE DASHBOARD'S SCRIPTS WINDOW
---------------------------------------
Write Bench() with no URL. The panel starts the script with BENCHPANEL_URL and
BENCHPANEL_TOKEN set, and Bench() uses them: the local address, and this run's key
in place of any login. See SCRIPTS.md.

REACHING A SERVER FROM ANOTHER MACHINE
--------------------------------------
  1. SSH tunnel (no password; uses your SSH key):
         ssh -N -L 8765:127.0.0.1:8765 you@server
     then Bench("http://127.0.0.1:8765/").  Keep local port 8765: the panel checks
     the Host header and only accepts 127.0.0.1:<its own port>.
  2. Through the portal's HTTPS proxy:
         Bench("https://server/bench/", login_file="~/.config/benchpanel/login")
     The login file holds one line  user:password  and must be chmod 600.
"""
import base64
import json
import os
import signal
import ssl
import stat
import time
import urllib.error
import urllib.request

__all__ = ["Bench", "BenchError"]
__version__ = "1.2.0"


class BenchError(RuntimeError):
    """The panel refused a request, or could not be reached. The message says which."""


class Bench:
    def __init__(self, url=None, login_file=None, cafile=None, timeout=60):
        # Started from the dashboard: the panel tells us where it is and hands us
        # this run's key. An explicit url wins (and then the key is not sent).
        env_url = os.environ.get("BENCHPANEL_URL")
        url = url or env_url or "http://127.0.0.1:8765/"
        self.url = url if url.endswith("/") else url + "/"
        # A SIMULATED run (the panel's /sim/ bench) may only talk to the simulator:
        # a hard-coded address would otherwise reach the real bench.
        if os.environ.get("BENCHPANEL_SIMULATED") and env_url and self.url.rstrip("/") != env_url.rstrip("/"):
            raise BenchError(f"this is a SIMULATED run: it may only use the simulated bench "
                             f"({env_url}), not {self.url}. Leave --url empty.")
        self.timeout = timeout
        self._token = os.environ.get("BENCHPANEL_TOKEN") if env_url and self.url.rstrip("/") == env_url.rstrip("/") else None
        self._auth = _read_login(login_file) if login_file else None
        self._ctx = ssl.create_default_context(cafile=os.path.expanduser(cafile)) if cafile else None
        self._acted = {}            # instrument id -> server time of our last act() on it

    # -- plumbing ----------------------------------------------------------
    def _call(self, path, body=None):
        headers = {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
            headers["X-Bench-Panel"] = "1"          # the panel refuses POSTs without it
        if self._token:
            headers["X-Bench-Script-Token"] = self._token
        elif self._auth:
            headers["Authorization"] = "Basic " + base64.b64encode(self._auth.encode()).decode()
        req = urllib.request.Request(self.url + path, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise BenchError(f"{self.url}: login required or rejected -- check the login file") from None
            if e.code == 403:
                # The panel says why (a script may not use the managers; a finished
                # run's key; a missing header) -- pass its own words on.
                try:
                    why = json.loads(e.read()).get("msg")
                except Exception:
                    why = None
                raise BenchError(f"{self.url}{path}: refused -- {why or 'HTTP 403'}") from None
            if e.code == 421:
                raise BenchError(f"{self.url}: the panel refused the Host header. Through an SSH "
                                 "tunnel, the local port must equal the panel's port (8765).") from None
            raise BenchError(f"{self.url}{path}: HTTP {e.code}") from None
        except urllib.error.URLError as e:
            raise BenchError(f"cannot reach {self.url}: {e.reason}") from None
        try:
            return json.loads(raw)
        except ValueError:
            raise BenchError(f"{self.url}{path} did not return JSON -- wrong URL?") from None

    # -- reading ------------------------------------------------------------
    def state(self):
        """The whole snapshot: {'instruments': [...], 'log': [...], 'now': server time, ...}."""
        return self._call("api/state")

    def instruments(self):
        """{id: state} for every instrument on the bench, in card order."""
        return {c["id"]: c for c in self.state()["instruments"]}

    def get(self, iid):
        """The latest state of one instrument -- possibly from before your last change.
        Use read() when that matters."""
        s = self.instruments()
        if iid not in s:
            raise BenchError(f"no instrument {iid!r} on this bench (have: {', '.join(s) or 'none'})")
        return s[iid]

    def read(self, iid, timeout=10.0):
        """State from a poll that began AFTER this call, and after your last act() on it.

        This is the fix for the classic automation bug: set something, read straight
        back, get the old value. The panel stamps each instrument's state with when
        its poll began (data_as_of); this waits until that is later than now.
        """
        snap = self.state()
        since = max(snap["now"], self._acted.get(iid, 0))
        return self.wait_for(iid, lambda s: True, timeout=timeout, since=since)

    def wait_for(self, iid, test, timeout=30.0, since=None, interval=0.2):
        """Poll until test(state) is true for a reading newer than `since`.

        `since` defaults to your last act() on this instrument. Raises BenchError on
        timeout, naming the last state seen, or if the instrument goes OFF.
        """
        since = self._acted.get(iid, 0) if since is None else since
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = self.get(iid)
            if not last.get("enabled"):
                raise BenchError(f"{iid} is OFF in the panel" +
                                 (f": {last['power_msg']}" if last.get("power_msg") else ""))
            if (last.get("data_as_of") or 0) > since and test(last):
                return last
            time.sleep(interval)
        raise BenchError(f"{iid}: timed out after {timeout:g}s waiting "
                         f"(last status: {last.get('status') if last else '?'})")

    # -- doing --------------------------------------------------------------
    def act(self, iid, action, check=True, **args):
        """Run one control on one instrument, e.g. act("8648a-1", "set_freq", value=100).

        Raises BenchError if the panel or the instrument refuses (check=False returns
        the reply instead). Output-enabling actions need confirmed=True.
        """
        r = self._call("api/action", {"instrument": iid, "action": action, "args": args})
        if r.get("at"):
            self._acted[iid] = r["at"]
        if check and not r.get("ok"):
            raise BenchError(f"{iid} {action}: {r.get('msg') or '; '.join(r.get('errors') or []) or 'refused'}")
        return r

    def power_on(self, iid):
        return self.act(iid, "power_on")

    def power_off(self, iid):
        """Outputs off, front panel handed back, link closed. Returns the reply;
        reply['output_off'] is False if the output could NOT be confirmed off."""
        return self.act(iid, "power_off", check=False)

    def all_outputs_off(self):
        r = self._call("api/action", {"action": "all_outputs_off"})
        return r.get("result", [])

    def session(self, *iids):
        """Power instruments on for a with-block; power off the ones it turned on.

        Instruments that were already ON are left ON -- someone else is using them.
        """
        return _Session(self, iids)

    # -- managing the bench ---------------------------------------------------
    def manage(self, op, **kw):
        """Low-level access to the Instruments manager (add/remove/install ...)."""
        r = self._call("api/manage", dict(kw, op=op))
        if isinstance(r, dict) and r.get("ok") is False:
            raise BenchError(f"{op}: {r.get('msg')}")
        return r


class _Session:
    def __init__(self, bench, iids):
        self.bench, self.iids, self.mine = bench, iids, []

    def __enter__(self):
        try:
            current = self.bench.instruments()
            for iid in self.iids:
                if iid not in current:
                    raise BenchError(f"no instrument {iid!r} on this bench")
                if not current[iid].get("enabled"):
                    self.bench.power_on(iid)
                    self.mine.append(iid)
        except BaseException:
            self._release()
            raise
        return self.bench

    def _release(self):
        problems = []
        for iid in reversed(self.mine):
            try:
                r = self.bench.power_off(iid)
                if r.get("output_off") is False:
                    problems.append(f"{iid}: OUTPUT OFF NOT CONFIRMED -- check its front panel")
            except BenchError as e:
                problems.append(f"{iid}: {e}")
        self.mine = []
        return problems

    def __exit__(self, exc_type, exc, tb):
        problems = self._release()
        if problems:
            if exc_type is None:
                raise BenchError("; ".join(problems))
            # Don't bury the error that ended the block -- report both.
            import sys
            print("benchpanel: while releasing after an error: " + "; ".join(problems), file=sys.stderr)
        return False


def _read_login(path):
    path = os.path.expanduser(path)
    st = os.stat(path)
    if os.name == "posix" and st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise BenchError(f"{path} is readable by other users -- chmod 600 it")
    with open(path, encoding="utf-8") as fh:
        line = fh.readline().strip()
    if ":" not in line:
        raise BenchError(f"{path}: expected one line user:password")
    return line


# Windows: the dashboard's Stop sends CTRL_BREAK, which by default kills Python on
# the spot -- no finally:, no restore. Turn it into Ctrl-C so the script unwinds.
if os.name == "nt" and os.environ.get("BENCHPANEL_TOKEN") and hasattr(signal, "SIGBREAK"):
    def _break(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGBREAK, _break)
