#!/usr/bin/env python3
"""Bench panel -- a plug-in test-instrument dashboard. Stand-alone: no web server needed.

    benchpanel                             # installed: opens http://127.0.0.1:8765
    python3 bench_panel.py                 # from a checkout, no install ("portable")
    benchpanel --no-browser                # as a service
    benchpanel --install-launcher          # add it to the desktop's app menu

It needs Python 3.10+, pyvisa and pyvisa-py and nothing else. It is its own web server: one process owns the instrument links and
serves the page. Apache, nginx or any other server is OPTIONAL -- only useful if
you want TLS and a login in front of it (see deploy/).

THE CORE SHIPS WITH NO INSTRUMENTS
----------------------------------
Every instrument is a plugin (benchcore/plugins.py), including the first. On a new
install the dashboard opens EMPTY; the Instruments manager installs plugins from
the catalog/ folder or from a .zip you upload, and adds instruments to the bench
without a restart. Which instruments are on YOUR bench, and at what address, lives
in site.json in the DATA FOLDER -- never in the code, never in the repository:

    --data-dir DIR           if given
    $BENCHPANEL_DATA         if set
    next to bench_panel.py   when run as `python3 bench_panel.py` (portable mode)
    per-user folder          when installed: ~/.local/share/benchpanel (Linux),
                             %APPDATA%\\BenchPanel (Windows),
                             ~/Library/Application Support/BenchPanel (macOS)

THE SHAPE
---------
    browser  --GET  api/state   --> every card's state + the traffic log
             --POST api/action  --> {instrument, action, args}   one card's control
             --POST api/manage  --> {op, ...}   install/remove plugins, add/remove instruments
             --POST api/scripts --> {op, ...}   import, list, edit, run and stop automation scripts
             --POST sim/api/sim --> {op, ...}   the simulated bench's wiring and presets
    <panel>/sim/   the SAME page and API, driving a simulated copy of the bench
                   (benchcore/sim.py) -- its own runner, results and run keys
    poller thread  -> poll() on each instrument that is ON in the panel, nothing else

SAFETY, WHICH IS NOT OPTIONAL WITH A SUPPLY ON THE BUS
------------------------------------------------------
  * everything starts OFF in the panel; nothing is contacted until its power button
  * enabling an output needs an explicit confirmation flag -- a stray request can't
  * ALL OUTPUTS OFF is always one call away and needs no confirmation
  * SIGINT/SIGTERM/normal exit: outputs off, then every front panel handed back
  * it listens on 127.0.0.1 only unless you give it a password file (--auth-file)
  * every POST must carry the X-Bench-Panel header, which a web page on another
    site cannot add without the browser asking this server first (it never
    agrees) -- so a random page you visit cannot switch your supply on
"""
import argparse
import base64
import hmac
import ipaddress
import json
import os
import shutil
import signal
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from benchcore.bench import Bench, Site
from benchcore.plugins import PluginError, PluginStore
from benchcore.scripts import (Runner, ScriptError, ScriptStore, check_source, lint, list_results,
                                result_path, template)
from benchcore.sim import SimBench

import urllib.parse
import urllib.request

from benchcore import launcher

PKG = os.path.dirname(os.path.abspath(__file__))


def _resource(name):
    """static/ or catalog/: inside the package when installed, beside it in a checkout."""
    for base in (PKG, os.path.dirname(PKG)):
        p = os.path.join(base, name)
        if os.path.isdir(p):
            return p
    return os.path.join(PKG, name)


def user_data_dir():
    if sys.platform.startswith("win"):
        return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "BenchPanel")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/BenchPanel")
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "benchpanel")


STATIC = _resource("static")
CATALOG = _resource("catalog")
SCRIPT_CATALOG = _resource("script_catalog") if os.path.isdir(os.path.join(PKG, "script_catalog")) \
    else os.path.join(os.path.dirname(PKG), "examples")
MAX_BODY = 12 * 1024 * 1024         # a base64 plugin zip, with room to spare
CTYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css",
          ".svg": "image/svg+xml", ".png": "image/png", ".json": "application/json"}

BENCH = None
STORE = None
SCRIPTS = None              # ScriptStore -- shared by the real and the simulated bench
RUNNER = None               # Runner: the one script that may be running on the REAL bench
SIMB = None                 # SimBench: the simulated copy, served under /sim/
SIM_RUNNER = None           # its own Runner, results folder and run keys
SIM_DIRTY = False           # the real bench or its plugins changed: resync when idle


class Ctx:
    """Which bench a request is for. The page and API are the same; the bench,
    the runner (and so the run keys) and the results folder are not."""

    def __init__(self, bench, runner, simulated):
        self.bench, self.runner, self.simulated = bench, runner, simulated


REAL = SIM = None


def _dictionaries():
    return {pid: p.manifest for pid, p in STORE.plugins.items()}


def _mark_sim_dirty():
    global SIM_DIRTY
    SIM_DIRTY = True


def _sim_sync_if_idle():
    global SIM_DIRTY
    if SIM_DIRTY and not SIM_RUNNER.running_id():
        SIM_DIRTY = False
        SIMB.sync()
AUTH = None                 # (user, password) when --auth-file is given
ALLOWED_HOSTS = set()       # Host headers accepted when bound to loopback


def manage(req):
    """The Instruments manager. Every op either completes or changes nothing."""
    r = _manage(req)
    if req.get("op") in ("install_catalog", "install_zip", "uninstall", "add", "remove", "rename", "move") \
            and isinstance(r, dict) and r.get("ok"):
        _mark_sim_dirty()
    return r


def _manage(req):
    op = req.get("op")
    try:
        if op == "plugins":
            return STORE.listing()
        if op == "inspect_catalog":
            return {"ok": True, "inspect": STORE.inspect_catalog(req.get("plugin"))}
        if op == "install_catalog":
            r = STORE.install_catalog(req.get("plugin"), BENCH.busy_plugins())
            BENCH.rebuild_plugin(r["id"])
            return {"ok": True, **r}
        if op == "inspect_zip":
            data = base64.b64decode(req.get("data") or "", validate=True)
            return {"ok": True, "inspect": STORE.inspect_zip(data)}
        if op == "install_zip":
            r = STORE.install_zip(req.get("sha256"), BENCH.busy_plugins())
            BENCH.rebuild_plugin(r["id"])
            return {"ok": True, **r}
        if op == "uninstall":
            pid = req.get("plugin")
            r = STORE.uninstall(pid, BENCH.users_of(pid))
            return {"ok": True, **r}
        if op == "add":
            return BENCH.add(req.get("plugin"), req.get("name"), req.get("connection"))
        if op == "remove":
            return BENCH.remove(req.get("instrument"))
        if op == "rename":
            return BENCH.rename(req.get("instrument"), req.get("name"))
        if op == "move":
            return BENCH.move(req.get("instrument"), req.get("delta", 0))
        if op == "defaults":
            return {"ok": True, "defaults": BENCH.site.data.get("defaults", {})}
    except PluginError as e:
        return {"ok": False, "msg": str(e)}
    except (ValueError, TypeError) as e:
        return {"ok": False, "msg": f"bad request: {e}"}
    except OSError as e:
        # A disk error must reach the page as a reason, not as a dropped connection.
        return {"ok": False, "msg": f"file error: {e}"}
    return {"ok": False, "msg": f"unknown op {op!r}"}


def _running_ids():
    return {i for i in (RUNNER.running_id(), SIM_RUNNER.running_id()) if i}


def _check(text):
    rep = check_source(text)
    rep["lint"] = lint(text, rep["header"], _dictionaries())
    return rep


def _reference(uses):
    """The scripting dictionary of each plugin, for the editor's reference pane."""
    out = {}
    ports = {}
    for e in SIMB.site.data["instruments"]:
        m = SIMB.world.models.get(e["id"])
        if m is not None and e["plugin"] not in ports:
            ports[e["plugin"]] = {"inputs": dict(m.INPUTS), "outputs": dict(m.OUTPUTS)}
    for pid, p in sorted(STORE.plugins.items()):
        if uses and pid not in uses:
            continue
        m = p.manifest
        out[pid] = {"model": m["model"], "name": m["name"], "version": m["version"],
                    "has_output": m["has_output"], "manual": m.get("manual"),
                    "actions": m.get("actions") or {}, "state": m.get("state") or {},
                    "ports": ports.get(pid, {"inputs": {}, "outputs": {}}),
                    "instances": [e["id"] for e in BENCH.site.data["instruments"] if e.get("plugin") == pid]}
    return out


def scripts(req, ctx=None):
    """The Scripts manager: import, list, edit, run, stop. Mirrors manage()."""
    ctx = ctx or REAL
    op = req.get("op")
    try:
        if op == "list":
            return {"ok": True, **SCRIPTS.listing(), "running": ctx.runner.running_id(),
                    "running_any": sorted(_running_ids()),
                    "bench_plugins": ctx.bench.bench_plugins(), "simulated": ctx.simulated}
        if op == "results":
            return {"ok": True, "folder": ctx.runner.results, "files": list_results(ctx.runner.results)}
        if op == "status":
            return {"ok": True, **ctx.runner.status(req.get("since", 0))}
        if op == "source":
            cat = bool(req.get("catalog"))
            text = SCRIPTS.source(req.get("script"), cat)
            return {"ok": True, "text": text,
                    "sha256": None if cat else SCRIPTS.sha(req.get("script"))}
        if op == "check":
            return {"ok": True, **_check(str(req.get("text") or ""))}
        if op == "save":
            r = SCRIPTS.save(req.get("script"), str(req.get("text") or ""), req.get("sha256"), _running_ids())
            return {"ok": True, **r, "check": _check(SCRIPTS.source(r["id"]))}
        if op == "template":
            return {"ok": True, "text": template(req.get("name"), req.get("uses"), req.get("outputs"),
                                                 _dictionaries())}
        if op == "reference":
            return {"ok": True, "plugins": _reference(req.get("uses") or [])}
        if op == "inspect_catalog":
            return {"ok": True, "inspect": SCRIPTS.inspect_catalog(req.get("script"))}
        if op == "install_catalog":
            return {"ok": True, **SCRIPTS.install_catalog(req.get("script"))}
        if op == "inspect_upload":
            data = base64.b64decode(req.get("data") or "", validate=True)
            return {"ok": True, "inspect": SCRIPTS.inspect_upload(req.get("filename"), data)}
        if op == "install_upload":
            return {"ok": True, **SCRIPTS.install_upload(req.get("sha256"))}
        if op == "remove":
            if req.get("script") in _running_ids():
                raise ScriptError("it is running -- stop it first")
            return {"ok": True, **SCRIPTS.remove(req.get("script"), None)}
        if op == "run":
            wired = None
            if ctx.simulated and req.get("wire"):
                e = SCRIPTS.get(req.get("script"))
                h = (e or {}).get("header") or {}
                # "53181a:ch{channel}" takes the run's own value of that setting.
                vals = {a["name"]: a.get("default") for a in h.get("args", [])}
                vals.update(req.get("args") or {})
                pairs = [[str(end).format(**{k: "" if v is None else v for k, v in vals.items()}) for end in w]
                         for w in h.get("wiring") or []]
                if pairs:
                    SIMB.op({"op": "wiring", "wires": SIMB.resolve_wiring(pairs)})
                    wired = [" -> ".join(w) for w in SIMB.world.wires]
            r = ctx.runner.start(req.get("script"), req.get("args") or {},
                                 req.get("allow_outputs"), ctx.bench.bench_plugins())
            return {"ok": True, **r, "wired": wired}
        if op == "stop":
            return ctx.runner.stop()
    except ScriptError as e:
        return {"ok": False, "msg": str(e)}
    except (ValueError, TypeError) as e:
        return {"ok": False, "msg": f"bad request: {e}"}
    return {"ok": False, "msg": f"unknown op {op!r}"}


def _confirming(args):
    return isinstance(args, dict) and bool(args.get("confirmed"))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):                              # keep the console clean
        pass

    def _send(self, code, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if code == 401:
            self.send_header("WWW-Authenticate", 'Basic realm="Bench Panel"')
        self.end_headers()
        self.wfile.write(raw)

    def _gate(self):
        """Host check (DNS rebinding), which bench, a running script's key, and the optional login."""
        self.script = None
        path = self.path.split("?", 1)[0]
        if path == "/sim" or path.startswith("/sim/"):
            self.ctx, self.rel = SIM, path[4:] or "/"
        else:
            self.ctx, self.rel = REAL, path
        if ALLOWED_HOSTS and self.headers.get("Host", "") not in ALLOWED_HOSTS:
            self._send(421, {"error": "unexpected Host header"})
            return False
        token = self.headers.get("X-Bench-Script-Token")
        if token is not None:
            # A script run from the dashboard. Its key stands in for the login, and
            # dies with the run -- a key from a finished run is refused outright.
            # Each bench checks only ITS runner's keys: a simulated run's key does
            # nothing on the real bench, and the other way round.
            self.script = self.ctx.runner.token_state(token) if self.ctx and self.ctx.runner else None
            if self.script is None:
                self._send(403, {"ok": False, "msg": "that script run is over -- its key no longer works"})
                return False
            return True
        if AUTH:
            got = self.headers.get("Authorization", "")
            want = "Basic " + base64.b64encode(f"{AUTH[0]}:{AUTH[1]}".encode()).decode()
            if not hmac.compare_digest(got.encode(), want.encode()):
                self._send(401, {"error": "login required"})
                return False
        return True

    def _download(self, rel):
        """One result file, as an attachment, streamed (a long log can be big)."""
        full = result_path(self.ctx.runner.results, rel)
        if not full:
            return self._send(404, {"error": "no such result file"})
        ext = os.path.splitext(full)[1].lower()
        ctype = {".csv": "text/csv; charset=utf-8", ".log": "text/plain; charset=utf-8",
                 ".txt": "text/plain; charset=utf-8", ".json": "application/json"}.get(ext, "application/octet-stream")
        name = os.path.basename(full)
        ascii_name = "".join(c if 32 < ord(c) < 127 and c not in '"\\;' else "_" for c in name)
        try:
            fh = open(full, "rb")
        except OSError:
            return self._send(404, {"error": "no such result file"})
        with fh:
            size = os.fstat(fh.fileno()).st_size
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f'attachment; filename="{ascii_name}"; '
                             f"filename*=UTF-8''{urllib.parse.quote(name)}")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            shutil.copyfileobj(fh, self.wfile, 256 * 1024)

    def _file(self, full):
        if not os.path.isfile(full):
            return self._send(404, {"error": "not found"})
        ctype = CTYPES.get(os.path.splitext(full)[1], "application/octet-stream")
        if ctype.startswith("text/"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as fh:
            self._send(200, fh.read(), ctype)

    def do_GET(self):
        if not self._gate():
            return
        path, ctx = self.rel, self.ctx
        if path in ("/", "/index.html"):
            return self._file(os.path.join(STATIC, "index.html"))
        if path == "/api/state":
            if ctx.simulated:
                _sim_sync_if_idle()
            snap = ctx.bench.snapshot()
            snap["script"] = ctx.runner.summary() if ctx.runner else None
            snap["simulated"] = ctx.simulated
            snap["other_script"] = (RUNNER if ctx.simulated else SIM_RUNNER).summary()
            return self._send(200, snap)
        if path == "/api/plugins":
            return self._send(200, STORE.listing())
        if path.startswith("/api/results/") and not self.script:
            return self._download(urllib.parse.unquote(path[len("/api/results/"):]))
        if path == "/api/scripts" and not self.script:
            return self._send(200, scripts({"op": "list"}, ctx))
        if path.startswith("/static/"):
            name = path[len("/static/"):]
            full = os.path.normpath(os.path.join(STATIC, name))
            if not full.startswith(STATIC + os.sep):
                return self._send(404, {"error": "not found"})
            return self._file(full)
        if path.startswith("/plugin/"):
            # Only the two card files a manifest names -- never driver.py.
            parts = path.split("/")
            if len(parts) == 4:
                p = STORE.get(parts[2])
                full = p.asset(parts[3]) if p else None
                if full:
                    return self._file(full)
            return self._send(404, {"error": "not found"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._gate():
            return
        # A cross-site page cannot add this header without a CORS preflight, and
        # this server never answers one -- see the module docstring.
        if self.headers.get("X-Bench-Panel") != "1":
            return self._send(403, {"ok": False, "msg": "missing X-Bench-Panel header"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > MAX_BODY:
                return self._send(413, {"ok": False, "msg": "request too large"})
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return self._send(400, {"ok": False, "msg": f"bad request: {e}"})

        path, ctx = self.rel, self.ctx
        managing = path.startswith(("/api/manage", "/api/scripts", "/api/sim"))
        if self.script and managing:
            return self._send(403, {"ok": False, "msg": "a script cannot change the bench, "
                                    "its plugins, its scripts or the simulator's wiring"})
        if path.startswith("/api/manage"):
            if ctx.simulated:
                return self._send(200, {"ok": False, "msg": "the simulated bench copies the real one -- "
                                        "add or remove instruments on the real bench; wire and preset "
                                        "them here with Wiring"})
            return self._send(200, manage(req))
        if path.startswith("/api/scripts"):
            return self._send(200, scripts(req, ctx))
        if path.startswith("/api/sim"):
            if not ctx.simulated:
                return self._send(404, {"error": "not found"})
            try:
                return self._send(200, SIMB.op(req))
            except (ValueError, TypeError) as e:
                return self._send(200, {"ok": False, "msg": str(e)})
        if not path.startswith("/api/action"):
            return self._send(404, {"error": "not found"})
        action = req.get("action")
        if self.script and not self.script["allow_outputs"] and _confirming(req.get("args")):
            return self._send(200, {"ok": False, "msg":
                "this script run may not switch outputs on -- the script doesn't declare "
                "\"outputs\": true, or it was started without \"Allow outputs\" ticked"})
        bench = ctx.bench
        if action == "all_outputs_off":
            return self._send(200, {"ok": True, "result": bench.all_outputs_off()})
        if action == "set_polling":
            bench.poll_enabled = bool(req.get("value", True))
            return self._send(200, {"ok": True, "polling": bench.poll_enabled})
        return self._send(200, bench.action(req.get("instrument"), action,
                                            req.get("args") or {}))


def _is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _read_auth(path):
    with open(path, encoding="utf-8") as fh:
        line = fh.readline().strip()
    user, sep, pw = line.partition(":")
    if not sep or not user or len(pw) < 8:
        sys.exit(f"{path}: expected one line 'user:password', password 8+ characters")
    if os.name == "posix" and os.stat(path).st_mode & 0o077:
        sys.exit(f"{path} is readable by other users -- chmod 600 it first")
    return user, pw


def _already_running(port):
    """True if a BenchPanel is already answering on this port."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=1.5) as r:
            return "instruments" in json.loads(r.read())
    except Exception:
        return False


def main(portable_dir=None):
    global BENCH, STORE, AUTH, SCRIPTS, RUNNER, SIMB, SIM_RUNNER, REAL, SIM
    # Line-buffer the console even when it is a file, pipe or journal: the startup
    # summary (which plugins failed to load, which instruments are missing) is the
    # first thing anyone needs, and block buffering held it back until exit.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="benchpanel", description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1",
                    help="address to listen on; anything but loopback needs --auth-file")
    ap.add_argument("--data-dir", help="folder for site.json and plugins/ (see the top of this file)")
    ap.add_argument("--site", help="this bench's instruments and addresses (default: DATA/site.json)")
    ap.add_argument("--plugins", help="where installed plugins live (default: DATA/plugins)")
    ap.add_argument("--scripts", help="where imported scripts live (default: DATA/scripts)")
    ap.add_argument("--results", help="where scripts write their files and logs (default: DATA/results)")
    ap.add_argument("--sim-results", help="where SIMULATED runs write theirs (default: DATA/sim_results)")
    ap.add_argument("--install-launcher", action="store_true",
                    help="add BenchPanel to the desktop app menu, then exit")
    ap.add_argument("--uninstall-launcher", action="store_true", help="remove it again, then exit")
    ap.add_argument("--auth-file", help="file holding one line user:password (chmod 600); "
                    "turns on the built-in login")
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser")
    ap.add_argument("--no-poll", action="store_true",
                    help="start with polling paused (instruments still start OFF)")
    a = ap.parse_args()

    if a.install_launcher or a.uninstall_launcher:
        try:
            print(launcher.uninstall() if a.uninstall_launcher else launcher.install())
        except launcher.LauncherError as e:
            sys.exit(f"launcher: {e}")
        return

    data = (a.data_dir or os.environ.get("BENCHPANEL_DATA") or portable_dir or user_data_dir())
    data = os.path.abspath(os.path.expanduser(data))
    os.makedirs(data, exist_ok=True)
    a.site = a.site or os.path.join(data, "site.json")
    a.plugins = a.plugins or os.path.join(data, "plugins")
    a.scripts = a.scripts or os.path.join(data, "scripts")
    a.results = a.results or os.path.join(data, "results")
    a.sim_results = a.sim_results or os.path.join(data, "sim_results")

    if a.auth_file:
        AUTH = _read_auth(a.auth_file)
    elif not _is_loopback(a.host):
        sys.exit(f"refusing to listen on {a.host} without --auth-file: anyone who can "
                 "reach it could switch on an output. Use 127.0.0.1, or give it a login.")
    if _is_loopback(a.host):
        ALLOWED_HOSTS.update({f"127.0.0.1:{a.port}", f"localhost:{a.port}",
                              f"[::1]:{a.port}"})

    # Launched twice (a second click on the menu icon): don't fail -- show the one
    # that is already running.
    if _is_loopback(a.host) and _already_running(a.port):
        url = f"http://127.0.0.1:{a.port}/"
        print(f"BenchPanel is already running on {url} -- opening it.")
        if not a.no_browser:
            webbrowser.open(url)
        return

    STORE = PluginStore(a.plugins, CATALOG)
    STORE.load_all()
    site = Site(a.site)
    site.load()
    BENCH = Bench(STORE, site)
    BENCH.build_all()
    BENCH.start(poll=not a.no_poll)
    SCRIPTS = ScriptStore(a.scripts, SCRIPT_CATALOG)
    # A script always talks to this panel over the local address, never through a
    # proxy, so it needs neither the portal login nor the certificate.
    local = "127.0.0.1" if a.host in ("0.0.0.0", "::") else a.host
    base = f"http://{'[' + local + ']' if ':' in local else local}:{a.port}/"
    RUNNER = Runner(SCRIPTS, a.results, base, BENCH.all_outputs_off)
    SIMB = SimBench(STORE, site, os.path.join(data, "sim.json"))
    SIMB.sync()
    SIMB.start(poll=True)
    SIM_RUNNER = Runner(SCRIPTS, a.sim_results, base + "sim/", SIMB.all_outputs_off, simulated=True)
    REAL, SIM = Ctx(BENCH, RUNNER, False), Ctx(SIMB, SIM_RUNNER, True)
    try:
        httpd = ThreadingHTTPServer((a.host, a.port), Handler)
    except OSError as e:
        BENCH.shutdown()
        sys.exit(f"cannot listen on {a.host}:{a.port}: {e.strerror} -- something else is using "
                 f"that port. Try --port 8766.")

    def bye(*_):
        # httpd.shutdown() BLOCKS until serve_forever() returns, so calling it on
        # the thread that is inside serve_forever() deadlocks and the process
        # ignores Ctrl-C and SIGTERM entirely -- which also means the bench never
        # gets released. It must be called from another thread.
        # A running script is stopped FIRST, while this server still answers, so
        # its own finally: blocks can put the bench back through it.
        def down():
            RUNNER.shutdown()
            SIM_RUNNER.shutdown()
            httpd.shutdown()
        threading.Thread(target=down, daemon=True).start()
    # SIGHUP: the terminal window the launcher opened was closed. Same safe exit.
    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), bye)

    url = f"http://{'127.0.0.1' if a.host in ('0.0.0.0', '::') else a.host}:{a.port}/"
    print(f"\n  bench panel on {url}")
    print(f"  data folder  {data}" + ("  (portable: next to bench_panel.py)" if data == portable_dir else ""))
    print(f"  site config  {a.site}" + ("" if os.path.exists(a.site) else "  (none yet)"))
    print(f"  plugins      {len(STORE.plugins)} installed in {a.plugins}")
    print(f"  scripts      {len(SCRIPTS.listing()['installed'])} installed in {a.scripts}; results in {a.results}")
    print(f"  simulator    {url}sim/  ({len(SIMB.instruments)} simulated instruments; results in {a.sim_results})")
    for pid, err in STORE.broken.items():
        print(f"    !! {pid}: {err}")
    snap = BENCH.snapshot()["instruments"]
    if not snap:
        print("  instruments  none -- add them from the dashboard")
    for c in snap:
        print(f"    {c['name']:16s} {c.get('where', '?'):28s} "
              + (c.get("missing") or "OFF in the panel"))
    print("\n  Ctrl-C, or closing this window, leaves the bench safe (outputs off, panels released).\n")
    if not a.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    finally:
        # Normally bye() has already stopped the script. If we got here some other
        # way, stop it now; the panel's own shutdown then makes the bench safe.
        RUNNER.shutdown(wait=5)
        SIM_RUNNER.shutdown(wait=5)
        SIMB.shutdown()
        BENCH.shutdown()


if __name__ == "__main__":
    main()
