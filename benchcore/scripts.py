#!/usr/bin/env python3
"""Automation scripts -- import them, list them, run them from the dashboard.

A SCRIPT IS ONE .py FILE
------------------------
An ordinary Python program that drives the bench through the panel's own API
(client/benchpanel.py), plus one literal dict near the top that tells the panel
what it is -- read WITHOUT running the script:

    BENCHPANEL_SCRIPT = {
        "schema": 1,
        "name": "8648A -> 53181A frequency sweep",
        "version": "1.0.0",
        "description": "Steps the generator, records the counter, writes a CSV.",
        "uses": ["8648a", "53181a"],        # plugin ids it needs on the bench
        "outputs": True,                    # can it switch an output ON?
        "args": [                           # becomes the Run form, and --flags
            {"name": "channel", "type": "choice", "choices": ["1", "2"], "default": "2"},
            {"name": "start", "type": "float", "default": 100, "label": "Start, MHz"},
            {"name": "rf_on", "type": "bool", "default": False},
        ],
    }

Each arg reaches the script as a command-line flag, `name` with _ turned into -:
"--start 100", "--channel 2", and a bool only as "--rf-on" when ticked. So a
script parses them with argparse exactly as it would from a terminal.

WHERE SCRIPTS MAY COME FROM  (the same rule as plugins)
-------------------------------------------------------
A script is Python that runs on this computer with the bench in reach, so there
are exactly two sources and no "install from URL":
  catalog   the scripts that ship with this program (examples/)
  upload    a .py the operator chose on their own machine
Both go INSPECT (header, checks, the FULL source shown, nothing written) then
INSTALL (only after they confirm). An upload is held in memory between the two,
keyed by its hash, so what gets installed is byte-for-byte what was shown.

RUNNING ONE
-----------
One script at a time, as a child process of the panel, with:
  BENCHPANEL_URL      how to reach this panel (always the local address)
  BENCHPANEL_TOKEN    this run's key; the client sends it on every request
  BENCHPANEL_RESULTS  the results folder, which is also its working directory
  PYTHONPATH          so `from benchpanel import Bench` works as-is

The run's key is what makes the declarations mean something:
  * a script that declares "outputs": false -- or one run WITHOUT the operator
    ticking "allow outputs" -- is refused any confirmed (output-on) action
  * a script can never use the Instruments or Scripts managers
This guards against a script doing by MISTAKE what it didn't declare. It is not a
sandbox: the script is Python running as you, which is why it is shown in full
before it installs.

Stop sends Ctrl-C (so the script's own finally: blocks restore the bench), then
terminates it if it hasn't finished in STOP_GRACE_S. If a script that declares
outputs ends any way other than cleanly, the panel presses ALL OUTPUTS OFF itself.
"""
import ast
import collections
import difflib
import hashlib
import importlib.util
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time

SCHEMA = 1
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,59}$")
ARG_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
ARG_TYPES = {"float", "int", "str", "bool", "choice"}
WIRE_END_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,59}:[a-z0-9_]*(\{[a-z][a-z0-9_]*\})?[a-z0-9_]*$")
WIRE_ARG_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")
MAX_SCRIPT = 256 * 1024
STAGE_TTL_S = 900
LOG_LINES = 3000
STOP_GRACE_S = 20          # Ctrl-C -> terminate. Restoring a bench over GPIB takes a while.

# Things a reviewer should look at twice. None of them is refused -- a script may
# have a good reason -- but each is shown on the inspect screen.
WATCH = [
    (re.compile(r"^\s*(import|from)\s+(pyvisa|visa|gpib|serial|usbtmc)\b", re.M),
     "talks to instruments DIRECTLY, not through the panel -- it bypasses the bus lock "
     "and every safety check. Two programs on one GPIB gateway at once has wedged it before."),
    (re.compile(r"^\s*(import|from)\s+socket\b", re.M), "opens its own network sockets"),
    (re.compile(r"^\s*(import|from)\s+subprocess\b|os\.system\(|os\.popen\(", re.M),
     "runs other programs"),
    (re.compile(r"\b(eval|exec)\s*\("), "uses eval/exec: code it runs is not all visible here"),
    (re.compile(r"shutil\.rmtree|os\.remove\(|os\.unlink\(", re.M), "deletes files"),
    (re.compile(r"urllib\.request|requests\.|http\.client", re.M),
     "makes its own web requests (fine if it's only the panel -- check where to)"),
]


class ScriptError(Exception):
    pass


# -- reading and checking a script (never executes it) -------------------------

def parse_header(source):
    """-> (header dict or None, [problems]). Reads the literal with ast; runs nothing."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return None, [f"not valid Python: line {e.lineno}: {e.msg}"]
    found = [n for n in tree.body if isinstance(n, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == "BENCHPANEL_SCRIPT" for t in n.targets)]
    if not found:
        return None, ["no BENCHPANEL_SCRIPT = {...} header at the top level (see SCRIPTS.md)"]
    if len(found) > 1:
        return None, ["BENCHPANEL_SCRIPT is assigned more than once"]
    try:
        h = ast.literal_eval(found[0].value)
    except (ValueError, TypeError, SyntaxError, RecursionError):
        return None, ["BENCHPANEL_SCRIPT must be a plain literal dict (no names, calls or f-strings)"]
    return h, check_header(h)


def check_header(h):
    bad = []
    if not isinstance(h, dict):
        return ["BENCHPANEL_SCRIPT is not a dict"]
    if h.get("schema") != SCHEMA:
        bad.append(f"schema must be {SCHEMA}")
    for k in ("name", "version", "description"):
        if not isinstance(h.get(k), str) or not h[k].strip():
            bad.append(f"'{k}' must be a non-empty string")
    if not isinstance(h.get("outputs"), bool):
        bad.append("'outputs' must be true or false: can this script switch an output ON?")
    uses = h.get("uses", [])
    if not isinstance(uses, list) or not all(isinstance(u, str) and ID_RE.match(u) for u in uses):
        bad.append("'uses' must be a list of plugin ids, e.g. [\"8648a\", \"53181a\"]")
    wiring = h.get("wiring", [])
    if not (isinstance(wiring, list) and all(
            isinstance(w, list) and len(w) == 2 and all(isinstance(e, str) and WIRE_END_RE.match(e) for e in w)
            for w in wiring)):
        bad.append("'wiring' must be a list of [\"plugin:port\", \"plugin:port\"] pairs, "
                   "e.g. [[\"e3641a:out\", \"34401a:in\"]]")
    else:
        if any(e.split(":")[0] not in (uses if isinstance(uses, list) else []) for w in wiring for e in w):
            bad.append("'wiring' names an instrument that is not in 'uses'")
        names = {a.get("name") for a in h.get("args", []) if isinstance(a, dict)}
        for ref in {m for w in wiring for e in w for m in WIRE_ARG_RE.findall(e)}:
            if ref not in names:
                bad.append(f"'wiring' refers to {{{ref}}}, which is not one of the args")
    args = h.get("args", [])
    if not isinstance(args, list):
        return bad + ["'args' must be a list"]
    seen = set()
    for i, a in enumerate(args):
        where = f"args[{i}]"
        if not isinstance(a, dict):
            bad.append(f"{where} is not an object")
            continue
        n = a.get("name")
        if not isinstance(n, str) or not ARG_RE.match(n):
            bad.append(f"{where}: name must be lower_case letters, digits and _")
            continue
        where = f"arg '{n}'"
        if n in seen:
            bad.append(f"{where} appears twice")
        seen.add(n)
        t = a.get("type")
        if t not in ARG_TYPES:
            bad.append(f"{where}: type must be one of {', '.join(sorted(ARG_TYPES))}")
            continue
        if t == "choice":
            ch = a.get("choices")
            if not isinstance(ch, list) or not ch or not all(isinstance(c, str) for c in ch):
                bad.append(f"{where}: a choice needs 'choices', a list of strings")
                continue
        if "default" in a and a["default"] is not None:
            try:
                coerce(a, a["default"])
            except ScriptError as e:
                bad.append(f"{where}: default {a['default']!r}: {e}")
        for k in ("label", "help"):
            if k in a and not isinstance(a[k], str):
                bad.append(f"{where}: '{k}' must be a string")
        # min / max are shown in the Run form, so anything but a number is refused here.
        for k in ("min", "max"):
            if a.get(k) is not None and (not isinstance(a[k], (int, float)) or isinstance(a[k], bool)):
                bad.append(f"{where}: '{k}' must be a number")
    return bad


def coerce(spec, value):
    """One form value -> the typed value, or ScriptError saying why not."""
    t = spec["type"]
    try:
        if t == "bool":
            if isinstance(value, bool):
                return value
            if str(value).lower() in ("true", "1", "yes", "on"):
                return True
            if str(value).lower() in ("false", "0", "no", "off", ""):
                return False
            raise ValueError
        if t == "int":
            if isinstance(value, bool):
                raise ValueError
            f = float(value)
            if f != int(f):
                raise ValueError
            v = int(f)
        elif t == "float":
            if isinstance(value, bool):
                raise ValueError
            v = float(value)
            if v != v or v in (float("inf"), float("-inf")):
                raise ValueError
        elif t == "choice":
            v = str(value)
            if v not in spec["choices"]:
                raise ScriptError(f"must be one of {', '.join(spec['choices'])}")
            return v
        else:
            v = str(value)
            if "\n" in v or len(v) > 500:
                raise ScriptError("one line, 500 characters at most")
            return v
    except (TypeError, ValueError, OverflowError):
        raise ScriptError(f"not a valid {t}") from None
    for lim, op in (("min", lambda x, m: x >= m), ("max", lambda x, m: x <= m)):
        if isinstance(spec.get(lim), (int, float)) and not op(v, spec[lim]):
            raise ScriptError(f"{lim}imum is {spec[lim]:g}")
    return v


def check_source(source):
    """The whole check the inspect screen shows. -> dict (ok, header, problems, watch)."""
    h, problems = parse_header(source)
    if h is not None and not problems:
        # The one declaration a program can check from the text: a script that says
        # it switches nothing on shouldn't contain the flag that switches things on.
        if h["outputs"] is False and re.search(r"confirmed\s*[=:]\s*True", source):
            problems.append("declares \"outputs\": false but passes confirmed=True -- "
                            "set outputs to true, or take the output-on call out")
    watch = [msg for rx, msg in WATCH if rx.search(source)]
    return {"ok": h is not None and not problems, "header": h, "problems": problems, "watch": watch}


def _decode(data):
    if len(data) > MAX_SCRIPT:
        raise ScriptError(f"larger than {MAX_SCRIPT // 1024} KB -- that is not one script")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ScriptError("not UTF-8 text -- is this really a .py file?") from None


def _script_id(filename):
    base = os.path.basename(filename or "")
    stem, ext = os.path.splitext(base)
    if ext != ".py":
        raise ScriptError("choose a .py file")
    sid = stem.lower().replace(" ", "_")
    if not ID_RE.match(sid):
        raise ScriptError(f"file name {base!r}: use letters, digits, _ and - (2-60 characters)")
    return sid


def _sha(data):
    return hashlib.sha256(data).hexdigest()


# -- lint: the script against the instruments' own dictionaries ------------------------

CORE_ACTIONS = {"power_on", "power_off"}
CLIENT_ARGS = {"check", "confirmed"}
CORE_STATE = {"plugin", "model", "where", "idn", "connected", "enabled", "power_msg", "error",
              "has_output", "data_as_of", "id", "name", "plugin_rev", "missing"}
READERS = {"read", "get", "wait_for"}


def lint(source, header, dictionaries):
    """Advisory findings: [{line, msg}]. `dictionaries` = {plugin id: manifest}.

    Checks what a script can get wrong in the PLUGIN's language -- the only language
    it speaks: an action no plugin it uses has, an argument that action doesn't
    take, and a state field that isn't reported. Instrument ids are usually
    variables, so a name is checked against every plugin the header 'uses'.
    """
    out = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return out
    uses = [u for u in (header or {}).get("uses", []) if isinstance(u, str)]
    for u in uses:
        if u not in dictionaries:
            out.append({"line": 1, "msg": f"'uses' names {u!r}, which is not an installed plugin"})
    known = {u: dictionaries[u] for u in uses if u in dictionaries}
    if not known:
        return out
    actions = {}
    for pid, m in known.items():
        for a, spec in (m.get("actions") or {}).items():
            actions.setdefault(a, []).append((pid, spec))
    fields = set(CORE_STATE)
    for m in known.values():
        fields |= set((m.get("state") or {}).keys())
    names = " / ".join(known)
    state_vars = {}                             # variable -> line it was assigned a state

    def is_reader(call):
        return (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and call.func.attr in READERS)

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            pairs = []
            for t in node.targets:
                if isinstance(t, ast.Name):
                    pairs.append((t, node.value))
                elif isinstance(t, ast.Tuple) and isinstance(node.value, ast.Tuple):
                    pairs += list(zip(t.elts, node.value.elts))
            for t, v in pairs:
                if isinstance(t, ast.Name) and is_reader(v):
                    state_vars[t.id] = node.lineno
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "act":
            if len(node.args) < 2 or not isinstance(node.args[1], ast.Constant) or not isinstance(node.args[1].value, str):
                continue
            a = node.args[1].value
            if a in CORE_ACTIONS:
                continue
            if a not in actions:
                close = difflib.get_close_matches(a, list(actions), n=1)
                out.append({"line": node.lineno, "msg": f'act(..., "{a}"): no {names} action by that name'
                            + (f' -- did you mean "{close[0]}"?' if close else "")})
                continue
            allowed = set(CLIENT_ARGS)
            for _, spec in actions[a]:
                allowed |= set((spec.get("args") or {}).keys())
            for kw in node.keywords:
                if kw.arg and kw.arg not in allowed:
                    out.append({"line": node.lineno, "msg": f'act(..., "{a}", {kw.arg}=...): "{a}" takes '
                                f'{", ".join(sorted(allowed - CLIENT_ARGS)) or "no arguments"}'})
    for node in ast.walk(tree):
        key = None
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
            base, key = node.value, node.slice.value
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
              and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            base, key = node.func.value, node.args[0].value
        if key is None:
            continue
        if not ((isinstance(base, ast.Name) and base.id in state_vars) or is_reader(base)):
            continue
        # a nested field, e.g. s["ch1"]["ampl"], is checked one level down by the reference only
        if key not in fields:
            close = difflib.get_close_matches(key, sorted(fields), n=1)
            out.append({"line": node.lineno, "msg": f'"{key}" is not a state field of {names}'
                        + (f' -- did you mean "{close[0]}"?' if close else "")})
    out.sort(key=lambda f: f["line"])
    return out


TEMPLATE = """#!/usr/bin/env python3
\"\"\"{name}.

WIRING: {wiring_text}

WHAT IT DOES
  1. Powers the instruments ON in the panel (only the ones that were OFF).
  2. Records every setting it will change.
  3. ...
  4. ALWAYS, even on an error, a Stop or Ctrl-C: outputs OFF, then everything put
     back as it was found.
\"\"\"
BENCHPANEL_SCRIPT = {{
    "schema": 1,
    "name": {name!r},
    "version": "0.1.0",
    "description": "What it measures, and the wiring it needs.",
    "uses": {uses!r},
    "outputs": {outputs!r},
    "wiring": {wiring!r},
    "args": [
{args}    ],
}}

import argparse
import sys

from benchpanel import Bench, BenchError


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", help="the panel (default: the one that started this script)")
{argparse}    a = ap.parse_args()

    print("plan: ...")          # say what it WOULD do, before anything is touched
{dry}
    bench = Bench(a.url)
    with bench.session({ids}):
{body}

if __name__ == "__main__":
    try:
        main()
    except BenchError as e:
        sys.exit(f"stopped: {{e}}")
    except KeyboardInterrupt:
        sys.exit("interrupted")
"""


def template(name, uses, outputs, dictionaries):
    """A new script's text: header, argparse, a session, record/restore scaffolding."""
    name = str(name or "New script").strip()[:80] or "New script"
    uses = [u for u in (uses or []) if isinstance(u, str) and ID_RE.match(u)]
    outputs = bool(outputs) and any((dictionaries.get(u) or {}).get("has_output") for u in uses)
    ids = [f"{u}-1" for u in uses]
    args = "".join(f'        {{"name": "id_{u.replace("-", "_")}", "type": "str", "default": "{u}-1", '
                   f'"label": "{(dictionaries.get(u) or {}).get("model", u)} id"}},\n' for u in uses)
    if outputs:
        args += ('        {"name": "output_on", "type": "bool", "default": False, "label": "Switch the output on",\n'
                 '         "help": "unticked = plan only, nothing switched on"},\n')
    argp = "".join(f'    ap.add_argument("--id-{u.replace("_", "-")}", default="{u}-1")\n' for u in uses)
    if outputs:
        argp += '    ap.add_argument("--output-on", action="store_true")\n'
    var = {u: "a.id_" + u.replace("-", "_") for u in uses}
    lines = []
    for u in uses:
        st = (dictionaries.get(u) or {}).get("state") or {}
        first = [k for k in st if st[k].get("type") in ("float", "bool", "str")][:3]
        lines.append(f'        s_{u.replace("-", "_")} = bench.read({var[u]})      # e.g. '
                     + (", ".join(f's["{k}"]' for k in first) or "its state"))
    lines.append("        try:")
    lines.append("            pass    # set things up, measure, write results -- see the reference pane")
    lines.append("        finally:")
    lines.append("            pass    # outputs OFF first, then put back what you recorded")
    wiring = []
    return TEMPLATE.format(name=name, uses=uses, outputs=outputs, wiring=wiring,
                           wiring_text="(fill in: which output goes to which input)",
                           args=args, argparse=argp, ids=", ".join(var[u] for u in uses) or "",
                           dry=('    if not a.output_on:\n        print("dry run: nothing switched on. Tick '
                                "'Switch the output on' to run it.\")\n        return\n") if outputs else "",
                           body="\n".join(lines) if uses else "        pass")


# -- the store: catalog + installed + staged uploads ---------------------------

class ScriptStore:
    def __init__(self, installed_dir, catalog_dir):
        self.dir = installed_dir
        self.catalog_dir = catalog_dir
        self._staged = {}               # sha256 -> (time, id, bytes)
        self._lock = threading.Lock()

    def _entries(self, folder):
        out = {}
        if not folder or not os.path.isdir(folder):
            return out
        for fn in sorted(os.listdir(folder)):
            if not fn.endswith(".py") or fn.startswith("_"):
                continue
            try:
                sid = _script_id(fn)
            except ScriptError:
                continue
            path = os.path.join(folder, fn)
            try:
                with open(path, "rb") as fh:
                    data = fh.read(MAX_SCRIPT + 1)
                rep = check_source(_decode(data))
            except (OSError, ScriptError) as e:
                rep = {"ok": False, "header": None, "problems": [str(e)], "watch": []}
            out[sid] = {"id": sid, "path": path, "sha256": _sha(data), **rep}
        return out

    def get(self, sid):
        return self._entries(self.dir).get(sid)

    def listing(self):
        inst = self._entries(self.dir)
        cat = self._entries(self.catalog_dir)

        def brief(e):
            h = e["header"] or {}
            return {"id": e["id"], "ok": e["ok"], "problems": e["problems"], "watch": e["watch"],
                    "name": h.get("name") or e["id"], "version": h.get("version"),
                    "description": h.get("description", ""), "uses": h.get("uses", []),
                    "outputs": h.get("outputs"), "args": h.get("args", []), "wiring": h.get("wiring", []),
                    "sha256": e["sha256"]}
        installed = [brief(e) for e in inst.values()]
        catalog = []
        for e in cat.values():
            b = brief(e)
            have = inst.get(e["id"])
            b["installed_version"] = (have["header"] or {}).get("version") if have else None
            b["installed_same"] = bool(have and have["sha256"] == e["sha256"])
            catalog.append(b)
        return {"installed": installed, "catalog": catalog, "folder": self.dir}

    def source(self, sid, catalog=False):
        e = self._entries(self.catalog_dir if catalog else self.dir).get(sid)
        if not e:
            raise ScriptError(f"no script {sid!r}")
        with open(e["path"], encoding="utf-8") as fh:
            return fh.read()

    def sha(self, sid):
        e = self.get(sid)
        return e["sha256"] if e else None

    def save(self, sid, text, base_sha, running_ids):
        """The editor's Save. -> {id, sha256, backup, created}.

        `base_sha` is the hash of the copy the editor opened (None for a new
        script). If the file on disk has changed since -- another tab, an import --
        the save is refused rather than overwriting someone's work. The old copy is
        kept as <id>.py.bak-<time>. A script that fails its checks CAN be saved (it
        is a draft); it just cannot be run until it passes.
        """
        sid = _script_id(f"{sid}.py")
        if sid in running_ids:
            raise ScriptError("it is running -- stop it before saving over it")
        data = str(text).replace("\r\n", "\n").encode("utf-8")
        _decode(data)
        os.makedirs(self.dir, exist_ok=True)
        dest = os.path.join(self.dir, sid + ".py")
        with self._lock:
            cur = self.get(sid)
            if cur is None and base_sha:
                raise ScriptError(f"{sid}.py is no longer there (removed?) -- save it under a new name")
            if cur is not None and not base_sha:
                raise ScriptError(f"{sid}.py already exists -- open it to edit, or pick another name")
            if cur is not None and cur["sha256"] != base_sha:
                raise ScriptError(f"{sid}.py changed since you opened it (another window, or an import). "
                                  "Copy your text somewhere, reopen it, and merge.")
            backup = None
            if cur is not None:
                if cur["sha256"] == _sha(data):
                    return {"id": sid, "sha256": cur["sha256"], "backup": None, "created": False, "unchanged": True}
                backup = f"{sid}.py.bak-{time.strftime('%Y%m%d-%H%M%S')}"
                shutil.copy2(dest, os.path.join(self.dir, backup))
            tmp = dest + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, dest)
        return {"id": sid, "sha256": _sha(data), "backup": backup, "created": cur is None}

    def _inspection(self, sid, data, source_kind):
        text = _decode(data)
        rep = check_source(text)
        have = self.get(sid)
        return {"id": sid, "source": source_kind, "sha256": _sha(data), "bytes": len(data),
                "lines": text.count("\n") + 1, "text": text,
                "replaces": ((have["header"] or {}).get("version") or "an unreadable copy") if have else None,
                **rep}

    def inspect_catalog(self, sid):
        e = self._entries(self.catalog_dir).get(sid)
        if not e:
            raise ScriptError(f"no script {sid!r} in the catalog")
        with open(e["path"], "rb") as fh:
            return self._inspection(sid, fh.read(), "catalog")

    def install_catalog(self, sid):
        i = self.inspect_catalog(sid)
        return self._install(sid, i["text"].encode("utf-8"), i)

    def inspect_upload(self, filename, data):
        sid = _script_id(filename)
        i = self._inspection(sid, data, "upload")
        now = time.time()
        with self._lock:
            for k in [k for k, v in self._staged.items() if now - v[0] > STAGE_TTL_S]:
                del self._staged[k]
            self._staged[i["sha256"]] = (now, sid, data)
        return i

    def install_upload(self, digest):
        with self._lock:
            staged = self._staged.pop(digest or "", None)
        if staged is None:
            raise ScriptError("that upload is no longer staged -- check it again")
        _, sid, data = staged
        return self._install(sid, data, self._inspection(sid, data, "upload"))

    def _install(self, sid, data, rep):
        if not rep["ok"]:
            raise ScriptError("it does not pass the script checks: " + "; ".join(rep["problems"]))
        os.makedirs(self.dir, exist_ok=True)
        dest = os.path.join(self.dir, sid + ".py")
        backup = None
        if os.path.exists(dest):
            backup = f"{sid}.py.bak-{time.strftime('%Y%m%d-%H%M%S')}"
            os.replace(dest, os.path.join(self.dir, backup))
        tmp = dest + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dest)
        return {"id": sid, "version": rep["header"]["version"], "backup": backup}

    def remove(self, sid, running_id):
        if sid == running_id:
            raise ScriptError("it is running -- stop it first")
        e = self.get(sid)
        if not e:
            raise ScriptError(f"no script {sid!r} installed")
        # Kept, not deleted: a script may be someone's only copy.
        backup = f"{sid}.py.removed-{time.strftime('%Y%m%d-%H%M%S')}"
        os.replace(e["path"], os.path.join(self.dir, backup))
        return {"id": sid, "kept_as": backup}


# -- result files: what scripts wrote, for download -------------------------------

MAX_LISTED = 2000


def list_results(folder, since=None):
    """Files under the results folder, newest first. `since`: only those modified at
    or after that time (the files one run wrote). Symlinks are never listed."""
    out = []
    if not os.path.isdir(folder):
        return out
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "__pycache__"
                         and not os.path.islink(os.path.join(root, d)))
        for fn in files:
            if fn.startswith("."):
                continue
            full = os.path.join(root, fn)
            try:
                if os.path.islink(full):
                    continue
                st = os.stat(full)
            except OSError:
                continue
            if since is not None and st.st_mtime < since:
                continue
            rel = os.path.relpath(full, folder).replace(os.sep, "/")
            out.append({"name": rel, "bytes": st.st_size, "mtime": st.st_mtime})
    out.sort(key=lambda f: (-f["mtime"], f["name"]))
    return out[:MAX_LISTED]


def result_path(folder, rel):
    """A requested result -> its real path, or None if it is not a regular file
    INSIDE the results folder (no .., no absolute paths, no symlinks out)."""
    if not rel or "\x00" in rel or rel.startswith(("/", "\\")):
        return None
    root = os.path.realpath(folder)
    full = os.path.realpath(os.path.join(root, *rel.split("/")))
    if not full.startswith(root + os.sep) or not os.path.isfile(full):
        return None
    if any(part.startswith(".") for part in rel.split("/")):
        return None
    return full


# -- running one -----------------------------------------------------------------

def client_path():
    """The folder holding benchpanel.py (the client), for the child's PYTHONPATH."""
    here = os.path.dirname(os.path.abspath(__file__))
    cand = [os.path.join(os.path.dirname(here), "client")]
    spec = importlib.util.find_spec("benchpanel")
    if spec and spec.submodule_search_locations:
        cand += list(spec.submodule_search_locations)
    for c in cand:
        if os.path.isfile(os.path.join(c, "benchpanel.py")):
            return c
    return None


class Runner:
    """At most one script running. All of its state is behind self.lock."""

    def __init__(self, store, results_dir, panel_url, all_outputs_off, simulated=False):
        self.simulated = simulated      # runs against the /sim/ bench
        self.store = store
        self.results = results_dir
        self.url = panel_url
        self.all_outputs_off = all_outputs_off
        self.lock = threading.Lock()
        self.run = None                 # the current or last run (dict)
        self.proc = None
        self.lines = collections.deque(maxlen=LOG_LINES)
        self.seq = 0                    # lines ever written in this run
        self._fh = None                 # the run's .log file in the results folder

    # -- what the token allows -------------------------------------------------
    def token_state(self, token):
        """None: no such run (refuse). Else {'allow_outputs': bool}."""
        with self.lock:
            r = self.run
            if r and r["state"] in ("running", "stopping") and secrets.compare_digest(token, r["token"]):
                return {"allow_outputs": r["allow_outputs"], "id": r["id"]}
        return None

    def running_id(self):
        with self.lock:
            return self.run["id"] if self.run and self.run["state"] in ("running", "stopping") else None

    # -- start ---------------------------------------------------------------
    def start(self, sid, values, allow_outputs, bench_plugins):
        e = self.store.get(sid)
        if not e:
            raise ScriptError(f"no script {sid!r} installed")
        if not e["ok"]:
            raise ScriptError("it no longer passes the script checks: " + "; ".join(e["problems"]))
        h = e["header"]
        missing = [u for u in h.get("uses", []) if u not in bench_plugins]
        if missing:
            raise ScriptError(f"it needs {', '.join(missing)} on the bench -- add "
                              f"{'it' if len(missing) == 1 else 'them'} in Instruments first")
        allow_outputs = bool(allow_outputs) and h["outputs"]
        argv, shown = [], []
        values = values or {}
        for spec in h.get("args", []):
            n = spec["name"]
            raw = values.get(n, spec.get("default"))
            if raw is None or raw == "":
                if spec["type"] == "bool":
                    raw = False
                elif spec.get("required"):
                    raise ScriptError(f"'{spec.get('label') or n}' is required")
                else:
                    continue
            try:
                v = coerce(spec, raw)
            except ScriptError as ex:
                raise ScriptError(f"'{spec.get('label') or n}': {ex}") from None
            flag = "--" + n.replace("_", "-")
            if spec["type"] == "bool":
                if v:
                    argv.append(flag)
                    shown.append(flag)
            else:
                argv += [flag, repr(v) if isinstance(v, float) else str(v)]
                shown.append(f"{flag} {argv[-1]}")
        extra = [x for x in values if x not in {a["name"] for a in h.get("args", [])}]
        if extra:
            raise ScriptError(f"unknown setting(s): {', '.join(extra)}")

        os.makedirs(self.results, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        log_path = os.path.join(self.results, f"{stamp}_{sid}.log")
        n = 1
        while os.path.exists(log_path):                 # two runs in one second
            n += 1
            log_path = os.path.join(self.results, f"{stamp}_{sid}_{n}.log")
        token = secrets.token_urlsafe(24)
        env = dict(os.environ, BENCHPANEL_URL=self.url, BENCHPANEL_TOKEN=token,
                   BENCHPANEL_RESULTS=self.results, PYTHONUNBUFFERED="1")
        env.pop("BENCHPANEL_SIMULATED", None)
        if self.simulated:
            # The client refuses any other panel address while this is set, so a
            # script with a hard-coded URL cannot reach the real bench by mistake.
            env["BENCHPANEL_SIMULATED"] = "1"
        cp = client_path()
        if cp:
            env["PYTHONPATH"] = cp + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        kw = {}
        if os.name == "posix":
            kw["start_new_session"] = True          # Stop signals the script and its children
        else:
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

        with self.lock:
            if self.run and self.run["state"] in ("running", "stopping"):
                raise ScriptError(f"{self.run['name']} is already running -- one at a time")
            self.lines.clear()
            self.seq = 0
            try:
                self._fh = open(log_path, "w", encoding="utf-8")
            except OSError:
                self._fh = None
            self.run = {"id": sid, "name": h["name"], "version": h["version"], "state": "running",
                        "allow_outputs": allow_outputs, "declares_outputs": h["outputs"],
                        "args": shown, "started": time.time(), "ended": None, "exit": None,
                        "log_file": log_path, "token": token, "note": None}
            try:
                self.proc = subprocess.Popen(
                    [sys.executable, "-u", e["path"], *argv], cwd=self.results, env=env,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)
            except OSError as ex:
                self.run.update(state="failed", ended=time.time(), note=f"could not start: {ex}")
                self._close_log()
                raise ScriptError(f"could not start it: {ex}") from None
            proc = self.proc
            if self.simulated:
                self._say("# SIMULATED BENCH -- every instrument here is a model; nothing reaches real hardware")
            self._say(f"$ {os.path.basename(e['path'])} {' '.join(shown)}".rstrip())
            self._say(f"# outputs: {'ALLOWED for this run' if allow_outputs else 'refused for this run'}"
                      f" · results folder: {self.results}")
        threading.Thread(target=self._pump, args=(proc,), daemon=True).start()
        return {"id": sid, "args": shown, "allow_outputs": allow_outputs, "log_file": log_path}

    def _say(self, line):                           # caller holds self.lock
        self.lines.append(line)
        self.seq += 1
        if self._fh:
            try:
                self._fh.write(line + "\n")
                self._fh.flush()
            except (OSError, ValueError):
                self._fh = None

    def _close_log(self):                           # caller holds self.lock
        if self._fh:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    def _pump(self, proc):
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            with self.lock:
                self._say(line)
        rc = proc.wait()
        with self.lock:
            r = self.run
            stopped = r["state"] == "stopping"
            r.update(exit=rc, ended=time.time(),
                     state="stopped" if stopped else ("finished" if rc == 0 else "failed"))
            clean = rc == 0 and not stopped
            declares = r["declares_outputs"]
        if declares and not clean:
            # It could switch outputs on and it didn't finish cleanly: don't trust that
            # it put everything back. The same big red button as the header.
            try:
                res = self.all_outputs_off()
                txt = ", ".join(f"{x['instrument']}: {'off' if x['ok'] else ('not ON in the panel' if x['ok'] is None else 'NOT CONFIRMED')}"
                                for x in res) or "no instruments with outputs"
                note = f"ALL OUTPUTS OFF pressed by the panel after an unclean end ({txt})"
            except Exception as ex:
                note = f"!! ALL OUTPUTS OFF after an unclean end FAILED: {ex} -- check the outputs by hand"
            with self.lock:
                self.run["note"] = note
                self._say("# " + note)
        with self.lock:
            self._say(f"# {self.run['state']} (exit {rc}) after {self.run['ended'] - self.run['started']:.1f} s")
            self._close_log()

    # -- stop ------------------------------------------------------------------
    def stop(self):
        with self.lock:
            r, proc = self.run, self.proc
            if not r or r["state"] != "running":
                raise ScriptError("nothing is running")
            r["state"] = "stopping"
            self._say("# Stop pressed: sending Ctrl-C so the script can put the bench back")
        self._signal(proc, "int")
        threading.Thread(target=self._escalate, args=(proc,), daemon=True).start()
        return {"ok": True, "msg": "Stopping: the script has been asked to finish and restore."}

    def _signal(self, proc, how):
        try:
            if os.name == "posix":
                sig = {"int": signal.SIGINT, "term": signal.SIGTERM, "kill": signal.SIGKILL}[how]
                os.killpg(proc.pid, sig)
            elif how == "int":
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                proc.kill()
        except (ProcessLookupError, OSError):
            pass

    def _escalate(self, proc):
        for how, wait in (("term", STOP_GRACE_S), ("kill", 5)):
            try:
                proc.wait(timeout=wait)
                return
            except subprocess.TimeoutExpired:
                with self.lock:
                    self._say(f"# still running after {wait} s: {'terminating' if how == 'term' else 'killing'} it")
                self._signal(proc, how)

    def shutdown(self, wait=STOP_GRACE_S):
        """Panel exiting: stop a running script and give it time to restore."""
        with self.lock:
            proc = self.proc if self.run and self.run["state"] in ("running", "stopping") else None
        if not proc:
            return
        print("  stopping the running script ...")
        try:
            self.stop()
        except ScriptError:
            pass
        try:
            proc.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            self._signal(proc, "kill")

    # -- status --------------------------------------------------------------
    def status(self, since=0):
        with self.lock:
            r = dict(self.run) if self.run else None
            if r:
                r.pop("token", None)
            first = self.seq - len(self.lines)
            since = int(since or 0)
            start = max(since, first)
            lines = list(self.lines)[start - first:]
            files = []
            if r:
                # What this run wrote: modified after it started (and, once it has
                # ended, before the next one could have). Includes its own .log.
                files = [f for f in list_results(self.results, since=r["started"])
                         if r["ended"] is None or f["mtime"] <= r["ended"] + 2]
            return {"run": r, "log": lines, "seq": self.seq, "from": start, "truncated": since < first,
                    "files": files}

    def summary(self):
        """For /api/state: enough for the header pill, nothing more."""
        with self.lock:
            r = self.run
            if not r:
                return None
            return {"id": r["id"], "name": r["name"], "state": r["state"]}
