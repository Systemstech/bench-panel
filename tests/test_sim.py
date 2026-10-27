#!/usr/bin/env python3
"""Self-test for the simulated bench (benchcore/sim.py), the catalog's models, and the
script editor's server side.

    python3 tests/test_sim.py          # exit 0 = all good (deploy.sh runs this)

Starts a panel on a free port with a scratch data folder. Its REAL bench holds two
instruments at made-up addresses, and the real transport is booby-trapped: any
attempt to connect, query or write through transport.Link writes a marker file.
Every catalog script then runs on the SIMULATED bench, and the test fails if the
marker exists -- the proof that a simulated run never touches the real path.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from benchcore.scripts import lint, parse_header  # noqa: E402

fails = 0


def check(what, cond, detail=""):
    global fails
    print(f"{'ok  ' if cond else 'FAIL'}  {what}" + (f"  -- {detail}" if detail and not cond else ""))
    fails += not cond


tmp = tempfile.mkdtemp(prefix="bp-simtest-")
data = os.path.join(tmp, "data")
os.makedirs(os.path.join(data, "scripts"))
shutil.copytree(os.path.join(HERE, "catalog"), os.path.join(data, "plugins"),
                ignore=shutil.ignore_patterns("__pycache__", "*.bak-*"))
for f in ("psu_dmm_check.py", "fgen_dmm_acv.py", "sweep_8648a_53181a.py", "bench_check.py"):
    shutil.copy(os.path.join(HERE, "examples", f), os.path.join(data, "scripts", f))
with open(os.path.join(data, "site.json"), "w") as fh:
    json.dump({"schema": 1, "defaults": {}, "instruments": [
        {"id": "e3641a-1", "plugin": "e3641a", "name": "Supply",
         "connection": {"type": "gpib-gateway", "gateway": "192.0.2.1", "address": 5}},
        {"id": "34401a-1", "plugin": "34401a", "name": "Meter",
         "connection": {"type": "gpib-gateway", "gateway": "192.0.2.1", "address": 22}}]}, fh)
MARK = os.path.join(tmp, "REAL_TRANSPORT_TOUCHED")
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    PORT = s.getsockname()[1]
BOOT = f"""
import sys; sys.path.insert(0, {HERE!r})
from benchcore import transport
def trap(self, *a, **k):
    open({MARK!r}, "a").write(repr(a) + "\\n")
    raise RuntimeError("real transport touched")
transport.Link.connect = transport.Link.query = transport.Link.write = trap
from benchcore import server
sys.argv = ["benchpanel", "--data-dir", {data!r}, "--port", "{PORT}", "--no-browser"]
server.main()
"""
proc = subprocess.Popen([sys.executable, "-c", BOOT], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
U = f"http://127.0.0.1:{PORT}/"


def post(path, body, token=None):
    h = {"Content-Type": "application/json", "X-Bench-Panel": "1"}
    if token:
        h["X-Bench-Script-Token"] = token
    r = urllib.request.Request(U + path, json.dumps(body).encode(), h)
    try:
        return json.loads(urllib.request.urlopen(r, timeout=60).read())
    except urllib.error.HTTPError as e:
        return {"http": e.code}


def get(path):
    return json.loads(urllib.request.urlopen(U + path, timeout=30).read())


def run(sid, args, allow=True, wire=True, stop_after=None):
    r = post("sim/api/scripts", {"op": "run", "script": sid, "args": args, "allow_outputs": allow, "wire": wire})
    if not r.get("ok"):
        print(f"      (run {sid} refused: {r.get('msg')})")
        return r, None
    t0 = time.time()
    while True:
        st = post("sim/api/scripts", {"op": "status", "since": 0})
        if st["run"]["state"] not in ("running", "stopping"):
            return r, st
        if stop_after and time.time() - t0 > stop_after and st["run"]["state"] == "running":
            post("sim/api/scripts", {"op": "stop"})
        if time.time() - t0 > 120:
            return r, st
        time.sleep(0.3)


def csv_rows(st):
    if not st:
        return []
    f = [x["name"] for x in st["files"] if x["name"].endswith(".csv")]
    if not f:
        return []
    with open(os.path.join(data, "sim_results", f[0])) as fh:
        return list(__import__("csv").DictReader(fh))


try:
    for _ in range(50):
        try:
            get("api/state")
            break
        except OSError:
            time.sleep(0.2)

    # -- 1. the simulated bench copies the real one ---------------------------------------
    b = post("sim/api/sim", {"op": "bench"})
    ids = [i["id"] for i in b["instruments"]]
    check("sim bench = the real bench's instruments + one of every other plugin",
          ids[:2] == ["e3641a-1", "34401a-1"]
          and len(ids) == len([d for d in os.listdir(os.path.join(HERE, "catalog"))
                               if os.path.isfile(os.path.join(HERE, "catalog", d, "manifest.json"))]), ids)
    check("every catalog plugin has its own model (none generic)", not any(i["generic"] for i in b["instruments"]))
    st = get("sim/api/state")
    check("sim/api/state says simulated; api/state does not", st["simulated"] and not get("api/state")["simulated"])
    check("instrument manager refused on the sim bench",
          post("sim/api/manage", {"op": "remove", "instrument": "e3641a-1"}).get("ok") is False)

    # -- 2. every catalog script, as it runs on the real bench ------------------------------
    r, st = run("psu_dmm_check", {"output_on": True, "readings": 2, "settle": 0.2})
    rows = csv_rows(st)
    check("supply check: finished, 4 rows", st and st["run"]["state"] == "finished" and len(rows) == 4,
          st and st["log"][-4:])
    check("supply check: wired from its header", r.get("wired") == ["e3641a-1:out -> 34401a-1:in"], r.get("wired"))
    check("supply check: meter reads the set voltage (|error| < 1 mV)",
          rows and all(abs(float(x["set_minus_meter_mv"])) < 1 for x in rows))
    check("supply check: restored as found", st and any("restored: supply output off, LOW range, 0 V, 0.8 A, OVP 66 V on"
                                                        in l for l in st["log"]))

    r, st = run("fgen_dmm_acv", {"output_on": True, "readings": 2, "settle": 0.5})
    rows = csv_rows(st)
    check("AC check: finished, High-Z setting -> ~0 % error", st and st["run"]["state"] == "finished" and rows
          and all(abs(float(x["error_pct"])) < 0.05 for x in rows),
          rows[:1])

    r, st = run("sweep_8648a_53181a", {"rf_on": True, "start": 100, "stop": 200, "step": 100, "channel": "2"})
    check("sweep: counts on channel 2, wiring follows the channel setting",
          st and st["run"]["state"] == "finished" and r.get("wired") == ["8648a-1:rf -> 53181a-1:ch2"],
          (r.get("wired"), st and st["log"][-3:]))
    r, st = run("bench_check", {})
    check("bench check: finished", st and st["run"]["state"] == "finished", st and st["log"][-3:])

    # -- 3. the physics that a script can get wrong -----------------------------------------
    post("sim/api/sim", {"op": "wiring", "wires": [["33522a-1:ch1", "34401a-1:in"]]})
    act = lambda i, a, **k: post("sim/api/action", {"instrument": i, "action": a, "args": k})  # noqa: E731
    act("33522a-1", "power_on")
    act("34401a-1", "power_on")
    act("34401a-1", "function", value="ACV")
    act("33522a-1", "set_load", ch=1, value="50")
    act("33522a-1", "set_ampl", ch=1, value=1.0)
    act("33522a-1", "output", ch=1, value=True, confirmed=True)
    time.sleep(1.5)
    act("34401a-1", "reset_stats")
    time.sleep(2.0)
    v = next(c for c in get("sim/api/state")["instruments"] if c["id"] == "34401a-1")["avg"]
    check("50-ohm SETTING into the 1 Mohm meter: 1 Vpp shown -> ~0.707 Vrms (the doubling)",
          v is not None and abs(v - 0.7071) < 0.002, v)
    act("33522a-1", "output", ch=1, value=False)
    act("33522a-1", "power_off")
    act("34401a-1", "power_off")

    # -- 4. refusals, through the presets ---------------------------------------------------
    post("sim/api/sim", {"op": "preset", "instrument": "e3641a-1", "preset": "output-on-5v"})
    r, st = run("psu_dmm_check", {"output_on": True})
    check("supply left ON (preset): the script refuses and changes nothing",
          st and st["run"]["state"] == "failed" and any("already ON" in l for l in st["log"]), st and st["log"][-3:])
    post("sim/api/sim", {"op": "power_cycle", "instrument": "e3641a-1"})
    post("sim/api/sim", {"op": "offline", "instrument": "34401a-1", "value": True})
    r, st = run("psu_dmm_check", {"output_on": True})
    check("meter offline (fault): the run fails cleanly", st and st["run"]["state"] == "failed", st and st["log"][-3:])
    post("sim/api/sim", {"op": "offline", "instrument": "34401a-1", "value": False})

    # -- 5. Stop mid-run restores through the script's own finally: -------------------------
    r, st = run("psu_dmm_check", {"output_on": True, "volts": "1,2,3,4,5,6,7,8", "settle": 2}, stop_after=6)
    check("Stop mid-run: stopped, and the script's restore line says output off",
          st and st["run"]["state"] == "stopped" and any("restored: supply output off" in l for l in st["log"]),
          st and st["log"][-4:])

    # -- 6. keys: each bench accepts only its own runner's -----------------------------------
    # the key never leaves the server; reach it the way a script does -- through a child
    probe = os.path.join(data, "scripts", "zz_probe.py")
    with open(probe, "w") as fh:
        fh.write('BENCHPANEL_SCRIPT = {"schema": 1, "name": "probe", "version": "1", "description": "d", '
                 '"uses": [], "outputs": False}\n'
                 'import os, json, urllib.request, urllib.error\n'
                 'def call(url):\n'
                 '    r = urllib.request.Request(url, headers={"X-Bench-Script-Token": os.environ["BENCHPANEL_TOKEN"]})\n'
                 '    try: return urllib.request.urlopen(r).status\n'
                 '    except urllib.error.HTTPError as e: return e.code\n'
                 'base = os.environ["BENCHPANEL_URL"]\n'
                 'print("own", call(base + "api/state"))\n'
                 'print("other", call(base.replace("/sim/", "/") + "api/state"))\n'
                 'import benchpanel\n'
                 'try:\n'
                 '    benchpanel.Bench(base.replace("/sim/", "/"))\n'
                 '    print("client: allowed")\n'
                 'except benchpanel.BenchError as e:\n'
                 '    print("client: refused")\n')
    r, st = run("zz_probe", {})
    log = st["log"] if st else []
    check("a simulated run's key works on /sim/", "own 200" in log, log)
    check("...and is refused on the real bench", "other 403" in log, log)
    check("the client refuses a hard-coded real-bench URL in a simulated run", "client: refused" in log, log)

    # -- 7. the editor: save, conflict, backup, check, lint ----------------------------------
    src = post("api/scripts", {"op": "source", "script": "zz_probe"})
    text2 = src["text"].replace('"version": "1"', '"version": "2"')
    r1 = post("api/scripts", {"op": "save", "script": "zz_probe", "text": text2, "sha256": src["sha256"]})
    check("save: ok, backup kept", r1.get("ok") and r1.get("backup")
          and os.path.exists(os.path.join(data, "scripts", r1["backup"])), r1)
    r2 = post("api/scripts", {"op": "save", "script": "zz_probe", "text": text2 + "#", "sha256": src["sha256"]})
    check("save with a stale hash is refused (someone else changed it)", r2.get("ok") is False, r2)
    r3 = post("api/scripts", {"op": "save", "script": "zz_probe", "text": "x", "sha256": None})
    check("save as NEW over an existing name is refused", r3.get("ok") is False, r3)
    r4 = post("api/scripts", {"op": "save", "script": "draft_one", "text": "print(1)\n", "sha256": None})
    check("a draft that fails the checks can be saved, and says why", r4.get("ok") and not r4["check"]["ok"], r4)
    tpl = post("api/scripts", {"op": "template", "name": "My check", "uses": ["e3641a", "34401a"], "outputs": True})
    chk = post("api/scripts", {"op": "check", "text": tpl["text"]})
    check("a new script from the template passes the checks, lint clean", chk["ok"] and not chk["lint"],
          (chk.get("problems"), chk.get("lint")))
    ref = post("api/scripts", {"op": "reference", "uses": ["e3641a"]})
    check("reference: actions, state and ports from the plugin",
          "set_v" in ref["plugins"]["e3641a"]["actions"] and ref["plugins"]["e3641a"]["ports"]["outputs"] == {"out": "output + / -"})

    check("NOTHING touched the real transport", not os.path.exists(MARK),
          open(MARK).read()[:300] if os.path.exists(MARK) else "")
finally:
    proc.terminate()
    try:
        out = proc.communicate(timeout=20)[0].decode(errors="replace")
    except subprocess.TimeoutExpired:
        proc.kill()
        out = ""
    if fails:
        print(out[-2000:])
    shutil.rmtree(tmp, ignore_errors=True)

# -- 8. lint, offline --------------------------------------------------------------------------
D = {json.load(open(os.path.join(HERE, "catalog", p, "manifest.json")))["id"]:
     json.load(open(os.path.join(HERE, "catalog", p, "manifest.json"))) for p in os.listdir(os.path.join(HERE, "catalog"))
     if os.path.isfile(os.path.join(HERE, "catalog", p, "manifest.json"))}
for f in sorted(os.listdir(os.path.join(HERE, "examples"))):
    if f.endswith(".py"):
        s = open(os.path.join(HERE, "examples", f)).read()
        check(f"lint: no findings on the proven {f}", lint(s, parse_header(s)[0], D) == [])
BAD = ('BENCHPANEL_SCRIPT={"schema":1,"name":"x","version":"1","description":"d","uses":["e3641a","34401a"],"outputs":True}\n'
       'b = None\nb.act("e3641a-1", "set_volt", value=5)\nb.act("e3641a-1", "set_v", volts=5)\n'
       'p = b.read("e3641a-1")\nprint(p["vset"], b.read("x").get("readng"), p["v_set"])\n')
found = lint(BAD, parse_header(BAD)[0], D)
check("lint catches a wrong action, a wrong argument and two wrong fields (4)", len(found) == 4, found)

print("\nALL PASS" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
