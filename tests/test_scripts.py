#!/usr/bin/env python3
"""Self-test for benchcore/scripts.py: the checks catch what they claim to, the
import flow writes only what was shown, and the runner stops and cleans up safely.

    python3 tests/test_scripts.py          # exit 0 = all good (deploy.sh runs this)

Runs no instruments and no panel: the runner's scripts here are tiny Python
programs, and "ALL OUTPUTS OFF" is a stand-in that records being pressed.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from benchcore.scripts import (Runner, ScriptError, ScriptStore, check_source, coerce,  # noqa: E402
                               list_results, result_path)

GOOD = '''
BENCHPANEL_SCRIPT = {"schema": 1, "name": "T", "version": "1.0.0", "description": "test",
    "uses": [], "outputs": %(outputs)s,
    "args": [{"name": "n", "type": "int", "default": 2, "min": 1, "max": 9},
             {"name": "mode", "type": "choice", "choices": ["a", "b"], "default": "a"},
             {"name": "go_fast", "type": "bool", "default": False}]}
import argparse, os, sys, time
ap = argparse.ArgumentParser(); ap.add_argument("--n", type=int); ap.add_argument("--mode")
ap.add_argument("--go-fast", action="store_true"); a = ap.parse_args()
print("args", a.n, a.mode, a.go_fast, "token", bool(os.environ.get("BENCHPANEL_TOKEN")),
      "cwd-is-results", os.getcwd() == os.environ.get("BENCHPANEL_RESULTS"))
%(body)s
'''

fails = 0


def check(what, cond):
    global fails
    print(f"{'ok  ' if cond else 'FAIL'}  {what}")
    fails += not cond


def src(outputs="False", body=""):
    return GOOD % {"outputs": outputs, "body": body}


# -- 1. the header checks ------------------------------------------------------
check("a good script passes", check_source(src())["ok"])
MUTANTS = {
    "no header": "print('hi')\n",
    "not valid Python": src() + "\ndef (:\n",
    "header is not a literal": "X = 1\nBENCHPANEL_SCRIPT = {'schema': X}\n",
    "header assigned twice": src() + "\nBENCHPANEL_SCRIPT = {}\n",
    "wrong schema": src().replace('"schema": 1', '"schema": 2'),
    "outputs missing": src().replace('"outputs": False,', ''),
    "outputs not a bool": src(outputs='"no"'),
    "empty name": src().replace('"name": "T"', '"name": ""'),
    "bad arg type": src().replace('"type": "int"', '"type": "number"'),
    "bad arg name": src().replace('"name": "n"', '"name": "N-1"'),
    "duplicate arg": src().replace('"name": "mode"', '"name": "n"'),
    "default out of range": src().replace('"default": 2', '"default": 20'),
    "min not a number (markup)": src().replace('"min": 1', '"min": "\\"><img src=x onerror=alert(1)>"'),
    "max is a bool": src().replace('"default": 2, "min": 1, "max": 9', '"default": 1, "min": 1, "max": True'),
    "choice with no choices": src().replace('"choices": ["a", "b"], ', ''),
    "choice default not a choice": src().replace('"default": "a"', '"default": "c"'),
    "uses not plugin ids": src().replace('"uses": []', '"uses": ["Not An Id!"]'),
    "outputs false but confirmed=True": src(body="bench.act('x', 'output', value=True, confirmed=True)"),
}
for name, text in MUTANTS.items():
    check(f"caught: {name}", not check_source(text)["ok"])
check("confirmed=True is fine when outputs are declared",
      check_source(src(outputs="True", body="x = dict(confirmed=True)"))["ok"])
check("direct pyvisa is flagged for review", any("DIRECTLY" in w for w in
      check_source(src(body="import pyvisa"))["watch"]))

# -- 2. form values --------------------------------------------------------------
spec = {"name": "n", "type": "int", "min": 1, "max": 9}
check("int '3' -> 3", coerce(spec, "3") == 3)
for bad in ("3.5", "x", "0", "10", True):
    try:
        coerce(spec, bad)
        check(f"int refuses {bad!r}", False)
    except ScriptError:
        check(f"int refuses {bad!r}", True)
try:
    coerce({"name": "f", "type": "float"}, "nan")
    check("float refuses nan", False)
except ScriptError:
    check("float refuses nan", True)
try:
    coerce({"name": "s", "type": "str"}, "two\nlines")
    check("str refuses a newline", False)
except ScriptError:
    check("str refuses a newline", True)

# -- 3. import flow ----------------------------------------------------------------
tmp = tempfile.mkdtemp(prefix="bp-scripts-test-")
cat = os.path.join(tmp, "catalog")
os.makedirs(cat)
with open(os.path.join(cat, "demo.py"), "w") as fh:
    fh.write(src())
store = ScriptStore(os.path.join(tmp, "installed"), cat)
check("catalog lists demo", [c["id"] for c in store.listing()["catalog"]] == ["demo"])
i = store.inspect_catalog("demo")
check("inspect wrote nothing", not os.path.exists(store.dir))
check("inspect returns the full source", i["text"] == src())
store.install_catalog("demo")
check("catalog import installs it", store.get("demo") and store.listing()["catalog"][0]["installed_same"])
try:
    store.inspect_upload("evil.sh", b"rm -rf /")
    check("non-.py upload refused", False)
except ScriptError:
    check("non-.py upload refused", True)
try:
    store.inspect_upload("../../x.py", src().encode())
    check("path in the filename cannot escape", store.inspect_upload("../../x.py", src().encode())["id"] == "x")
except ScriptError:
    check("path in the filename cannot escape", True)
bad = store.inspect_upload("broken.py", b"print(1)\n")
try:
    store.install_upload(bad["sha256"])
    check("a failing upload cannot be installed", False)
except ScriptError:
    check("a failing upload cannot be installed", True)
up = store.inspect_upload("up.py", src(body="print('uploaded')").encode())
store.install_upload(up["sha256"])
try:
    store.install_upload(up["sha256"])
    check("a staged upload installs only once", False)
except ScriptError:
    check("a staged upload installs only once", True)
with open(store.get("up")["path"], "rb") as fh:
    check("installed bytes == inspected bytes", fh.read() == src(body="print('uploaded')").encode())
store.remove("up", None)
check("remove keeps a dated copy", not store.get("up") and any(f.startswith("up.py.removed-") for f in os.listdir(store.dir)))

# -- 4. running ------------------------------------------------------------------------
pressed = []
runner = Runner(store, os.path.join(tmp, "results"), "http://127.0.0.1:1/",
                lambda: pressed.append(1) or [])


def wait_done(t=15):
    end = time.time() + t
    while time.time() < end:
        st = runner.status()["run"]["state"]
        if st not in ("running", "stopping"):
            return st
        time.sleep(0.1)
    return "timeout"


def log():
    return "\n".join(runner.status()["log"])


runner.start("demo", {"n": "3", "mode": "b", "go_fast": True}, False, [])
check("clean run finishes", wait_done() == "finished")
check("args arrive as flags", "args 3 b True" in log())
check("token + results folder set", "token True cwd-is-results True" in log())
check("clean run leaves no note", runner.status()["run"]["note"] is None)
check("log file written", os.path.getsize(runner.status()["run"]["log_file"]) > 0)
try:
    runner.start("demo", {"n": "0"}, False, [])
    check("out-of-range setting refused", False)
except ScriptError:
    check("out-of-range setting refused", True)
try:
    runner.start("demo", {"bogus": 1}, False, [])
    check("unknown setting refused", False)
except ScriptError:
    check("unknown setting refused", True)

# Stop: Ctrl-C reaches the script, whose finally: runs ("restored").
body = ("try:\n    print('working', flush=True)\n    time.sleep(60)\n"
        "finally:\n    print('restored', flush=True)")
up = store.inspect_upload("slow.py", src(outputs="True", body=body).encode())
store.install_upload(up["sha256"])
runner.start("slow", {}, True, [])
tok = runner.run["token"]
check("the run's key is accepted while running", runner.token_state(tok) == {"allow_outputs": True, "id": "slow"})
try:
    runner.start("demo", {}, False, [])
    check("only one run at a time", False)
except ScriptError:
    check("only one run at a time", True)
time.sleep(0.8)
runner.stop()
check("Stop ends it", wait_done() == "stopped")
check("the script's finally: ran", "restored" in log())
check("ALL OUTPUTS OFF pressed after a stopped output script", pressed == [1])
check("the key dies with the run", runner.token_state(tok) is None)

# A crash in a script that does NOT declare outputs: nothing pressed.
up = store.inspect_upload("crash.py", src(body="raise SystemExit(3)").encode())
store.install_upload(up["sha256"])
pressed.clear()
runner.start("crash", {}, False, [])
check("crash -> failed", wait_done() == "failed" and runner.status()["run"]["exit"] == 3)
check("no ALL OUTPUTS OFF for a no-outputs script", pressed == [])

# allow_outputs is ignored for a script that doesn't declare outputs
runner.start("demo", {}, True, [])
check("allow_outputs cannot be granted to a no-outputs script", runner.run["allow_outputs"] is False)
wait_done()
try:
    store.remove("slow", None)
    runner.start("slow", {}, False, [])
    check("a removed script cannot run", False)
except ScriptError:
    check("a removed script cannot run", True)
try:
    up = store.inspect_upload("needs.py", src().replace('"uses": []', '"uses": ["53181a"]').encode())
    store.install_upload(up["sha256"])
    runner.start("needs", {}, False, ["8648a"])
    check("missing instrument refused", False)
except ScriptError as e:
    check("missing instrument refused", "53181a" in str(e))

# -- 5. result downloads: only regular files inside the results folder ---------------
res = os.path.join(tmp, "results")
os.makedirs(os.path.join(res, "sub"), exist_ok=True)
with open(os.path.join(res, "sub", "data.csv"), "w") as fh:
    fh.write("a,b\n")
with open(os.path.join(tmp, "secret.txt"), "w") as fh:
    fh.write("no")
os.symlink(os.path.join(tmp, "secret.txt"), os.path.join(res, "link.txt"))
names = [f["name"] for f in list_results(res)]
check("results list includes subfolders", "sub/data.csv" in names)
check("results list skips symlinks", "link.txt" not in names)
check("a result inside the folder resolves", result_path(res, "sub/data.csv") is not None)
for bad in ("../secret.txt", "sub/../../secret.txt", "/etc/passwd", "link.txt", "sub", "", ".hidden",
            "..\\secret.txt", "sub/\x00x"):
    check(f"download refused: {bad!r}", result_path(res, bad) is None)
check("a run lists only its own files", all(f["mtime"] >= runner.run["started"]
      for f in runner.status()["files"]))

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
