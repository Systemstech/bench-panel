# Automation scripts in the dashboard

The **Scripts** button in the dashboard header opens a window to:

- **import** a script, from this program's own catalog or from a `.py` file you
  choose;
- **list** the scripts on this bench: what each does, which instruments it needs,
  and whether it can switch an output on;
- **run** one, filling in its settings on a form, and watch its output live;
- **stop** it, with its own clean-up still run;
- **download** what it wrote (CSVs and each run's log);
- see its **source**, or **remove** it.

A script is an ordinary Python program that drives the bench through the panel's
own interface ([API.md](API.md)), with a short header that tells the panel what it
is. The same script also runs from a terminal. Only one script runs at a time.

---

## Importing

Imports follow the same two steps as plugins, for the same reason: a script is
Python that runs on this computer with your instruments in reach.

1. **Check.** Choose the file (or a catalog entry) and press **Check this .py**.
   Nothing is written yet. You see:
   - the header: name, version, what it does, what it needs, whether it can switch
     outputs on, and its settings;
   - the check result (**Pass** / **FAIL**, with the reasons);
   - **"Read these parts carefully"**: things worth a second look, for example a
     script that imports `pyvisa` and talks to instruments directly, bypassing the
     panel's bus lock and safety checks;
   - its size, SHA-256 and **the whole source**.
2. **Import it.** Only a script that passes can be imported. What's written is
   byte-for-byte what you were shown.

There is no import from a web link. Importing a script with the same file name
replaces the old one and keeps the old copy as `<name>.py.bak-<date>`. **Remove**
keeps a dated copy too (`<name>.py.removed-<date>`), because a script may be
someone's only copy.

Imported scripts live in the data folder's `scripts/` (next to `site.json`). Files a
script writes, and a `.log` of every run, go in `results/`.

---

## Running

**Run…** opens a form built from the script's declared settings. The values you
used last time are remembered in this browser.

- **Run…** is greyed out while another script is running, or when an instrument the
  script needs isn't on the bench. Add it in **Instruments** first.
- A script that can switch outputs on gets an extra box: **"Allow this run to
  switch outputs on."**
  - **Unticked:** the panel refuses every output-on command from this run. That's
    how to do a dry run.
  - **Ticked:** **Run it** needs a second click (**CONFIRM: OUTPUTS MAY GO ON**),
    like an output button on a card.
- While it runs, the header shows **▶ script name**. Click it to open the window.

**Stop** sends the script Ctrl-C, so its `finally:` blocks and `bench.session()`
switch outputs off and put settings back. If it hasn't ended after 20 s it is
terminated, then killed.

**If a script that can switch outputs on ends any way other than cleanly** (Stop,
a crash, a non-zero exit), the panel presses **ALL OUTPUTS OFF** itself afterwards
and notes it in the log. Closing the panel stops a running script first, while the
panel can still answer it, so the script's own clean-up still works.

---

## Downloading results

A script's working folder is the results folder, so whatever it writes lands there,
along with a `.log` of every run.

- **While a run is showing,** the window lists **Files from this run** under it. Each
  name is a download link.
- **The Results section** lists everything in the folder, newest first (the first 15,
  then **Show all**). Run logs are hidden unless you untick **hide run logs**. Each
  file has a **Download** button.

Downloads work through the portal's login like the rest of the page. Only regular
files inside the results folder can be downloaded: no `..`, no absolute paths, and
no symlinks pointing out of it. Nothing can be deleted from the window: clear old
results in the folder itself.

---

## The editor

**Edit** beside a script, or **New script…** above the list, opens the editor. It works
offline, with nothing fetched from the internet.

- **New script…** asks for a file name, a name, the instruments and whether it switches
  an output on. It starts you from a template that already has the header, the settings,
  a session, a dry-run path and a record-then-restore skeleton.
- **Checks** (right pane, live as you type): the same checks as importing, plus a **lint**
  against the instruments' own dictionaries. It flags an action the instrument doesn't
  have (`"set_volt"`: *did you mean "set_v"?*), an argument that action doesn't take,
  and a state field it doesn't report. Click a finding to jump to its line; the line
  number goes red.
- **Reference** (right pane): for each instrument in `"uses"`, its manual (title, part,
  edition), its wiring ports, every action with its arguments, units, what it sends and
  the manual page, and every state field. **Insert** writes the call for you.
- **Completion:** inside `act(..., "` it offers the action names, and inside `["` or
  `.get("` the state fields. Arrow keys, then Enter or Tab. Under the code, the
  signature of the action the cursor is in.
- **Save** (Ctrl+S) keeps the previous copy as `<name>.py.bak-<time>`. If the file changed
  since you opened it (another window, an import), the save is refused rather than
  overwriting it. A script that fails its checks can still be saved as a draft; it just
  can't be run.
- **Save & simulate** saves, then opens its Run form on the simulated bench.

## The simulator

**Simulator** in the header (`<panel>/sim/`) is the same dashboard driving a simulated
copy of this bench. The copy holds every instrument on the real bench (same ids and
names), plus one of every other installed plugin. It has striped purple colours and a
banner so it can't be mistaken for the real thing.

- **What's real:** the plugin drivers, the core, the safety gates, the Scripts window
  and your script. **What's simulated:** each instrument, which is the plugin's `sim.py`
  model instead of a network link. So a simulated run tests your script **and** the
  driver's SCPI. A command the model doesn't know shows up in red in the traffic log,
  as it would on the bench.
- **Wiring** (the Instruments button, renamed): connect an output to an input
  (`e3641a-1 out → 34401a-1 in`). A meter reads what's wired to it: a supply's
  voltage, a generator's true RMS (with the 50 Ω / High-Z doubling), an RF level. A
  counter only completes a measurement when a signal above its sensitivity is present.
- **Starting states:** put an instrument in a front-panel situation (supply output left
  ON, generator unit left on dBm, 8648A reference mode on) and check that your script
  notices and refuses. **Not answering** makes it behave like an instrument that's
  switched off. **Power-cycle** returns it to power-on.
- **Scripts** there run against the simulated bench. Outputs are allowed by default,
  since there's nothing to protect, and the run's files go to a separate folder,
  `sim_results/`.
- **Separation:** a simulated run's key works only on `/sim/`, and the real bench's key
  only on the real bench. During a simulated run the client refuses any other panel
  address, so a hard-coded URL can't reach the real bench. `tests/test_sim.py` proves
  that a run of every catalog script never touches the real transport.
- **What it isn't:** an accuracy model. The instruments are ideal plus a little noise.
  A simulated run shows that the steps, refusals and restore are right. What the
  instruments read is only ever measured on the bench.

### Wiring in the header

```python
"wiring": [["e3641a:out", "34401a:in"]],            # plugin:port -> plugin:port
"wiring": [["8648a:rf", "53181a:ch{channel}"]],     # follows the script's own `channel` setting
```

Optional. It documents the cabling, and the simulator's Run form offers to wire the
simulated bench that way (**Wire the simulated bench the way the script says**, ticked by
default). The ports each instrument has are listed in the editor's Reference pane.

## Writing one

### The header

One literal dict at the top level of the file. The panel reads it **without running
the script**, so it has to be plain values: no variables, no function calls, no
f-strings.

```python
BENCHPANEL_SCRIPT = {
    "schema": 1,
    "name": "PSU soak",
    "version": "1.0.0",
    "description": "Holds the E3641A at a voltage for a while, logging the current.",
    "uses": ["e3641a"],             # plugin ids that must be on the bench
    "outputs": True,                # can this script switch an output ON?
    "args": [
        {"name": "volts", "type": "float", "default": 5, "min": 0, "max": 35,
         "label": "Voltage, V"},
        {"name": "minutes", "type": "int", "default": 10, "min": 1, "max": 600},
        {"name": "range", "type": "choice", "choices": ["LOW", "HIGH"], "default": "LOW"},
        {"name": "psu", "type": "str", "default": "e3641a-1", "label": "Supply's id"},
        {"name": "really", "type": "bool", "default": False, "label": "Switch the output on",
         "help": "unticked = plan only"},
    ],
}
```

| Field | Required | Meaning |
|---|---|---|
| `schema` | yes | `1` |
| `name`, `version`, `description` | yes | shown in the list |
| `outputs` | yes | `true` if the script can switch any output on. **If `false`, the file must not contain `confirmed=True`**, and the panel refuses output-on commands from it. |
| `uses` | no | plugin ids (`"e3641a"`, not an instrument id like `"e3641a-1"`) |
| `args` | no | the settings on the Run form |

Each arg has a `name` (lower_case letters, digits and `_`) and a `type`:

| `type` | Form field | Reaches the script as |
|---|---|---|
| `float`, `int` | text box, checked against `min` / `max` if given | `--name 5.0` |
| `str` | text box (one line) | `--name text` |
| `choice` | drop-down of `choices` (strings) | `--name LOW` |
| `bool` | tick box | `--name` when ticked, nothing when not |

Optional per arg: `default`, `label`, `help`, `min`, `max`, `required`. Underscores
become dashes in the flag: `ch1_imp` arrives as `--ch1-imp`. A blank field is left
out, so the script's own default applies.

### The rest of the script

Parse the flags with `argparse`, exactly as you would for a terminal, and drive the
bench with the client:

```python
import argparse
from benchpanel import Bench, BenchError

ap = argparse.ArgumentParser()
ap.add_argument("--volts", type=float, default=5)
ap.add_argument("--psu", default="e3641a-1")
ap.add_argument("--really", action="store_true")
a = ap.parse_args()

bench = Bench()                     # no URL: the panel that started us, with this run's key
with bench.session(a.psu):          # ON now; OFF and released at the end, whatever happens
    try:
        bench.act(a.psu, "set_v", value=a.volts)
        if a.really:
            bench.act(a.psu, "output", value=True, confirmed=True)
        ...
    finally:
        bench.act(a.psu, "output", check=False, value=False)
```

When the panel starts a script it sets:

| Variable | What it is |
|---|---|
| `BENCHPANEL_URL` | the panel's local address. `Bench()` uses it. |
| `BENCHPANEL_TOKEN` | this run's key. `Bench()` sends it in place of any login. It stops working when the run ends. |
| `BENCHPANEL_RESULTS` | the results folder. It's also the working directory, so a plain `open("out.csv", "w")` lands there. |
| `PYTHONPATH` | includes the client, so `from benchpanel import Bench` works as-is |

Everything printed appears in the window and in the run's `.log`. Print as you go;
output isn't buffered.

### Rules

1. **Go through the panel.** Use `Bench()`. Don't import `pyvisa` or open sockets to
   instruments yourself: that bypasses the bus lock and every safety check, and two
   programs on one GPIB gateway has wedged it before. The import screen flags it.
2. **Declare `outputs` honestly.** It decides whether the run can switch anything on
   at all.
3. **Record, then restore.** Read the settings you'll change first. Put them back,
   and switch outputs off, in a `finally:`.
4. **Put `confirmed=True` only on the line that means to switch something on.**
5. **Don't trust a reading you didn't check.** A counter on a marginal signal returns
   wrong numbers with no error (see the sweep example's `_SUSPECT` files).
6. **Check it before you import it:** `python3 tools/check_script.py myscript.py`
   runs the same checks as the import screen.

### What the run key does, and doesn't do

The key lets a dashboard-started script talk to the panel without the portal login.
It also enforces two things:

- A run that isn't allowed outputs is refused any output-on command.
- A script can never use the Instruments or Scripts managers: it can't add or
  remove instruments, install plugins, or start other scripts.

**It is not a sandbox.** A script is Python running as you, and could ignore the
panel entirely. That's why the import screen shows the whole source. The checks
catch honest mistakes, not bad intent.

---

## The catalog

Scripts that ship with the program live in `examples/` (installed as
`benchcore/script_catalog/`) and show under **Import a script**. When the catalog
has a newer version than the one imported, the button reads **Update x → y**.

| Script | What it does |
|---|---|
| `bench_check.py` | **No wiring, no outputs.** Powers each instrument on in the panel, records what it identifies as, reads and clears its error queue, then powers it off. A quick "is everything answering?" after setting up. An instrument already ON is only read. |
| `psu_dmm_check.py` | **E3641A → 34401A.** Steps the supply through voltages (0–20 V, current limit ≤ 500 mA, OVP set 2 V above the top step), measuring each on the meter's DC volts. The CSV has setting vs meter and the supply's readback vs meter, with no pass/fail. Refuses to start if the supply's output is already on. Wiring: supply + to meter HI, − to LO. |
| `fgen_dmm_acv.py` | **33522A → 34401A.** A sine at a list of amplitudes (10 Hz–100 kHz, ≤ 10 Vpp), measured in AC volts: expected RMS vs measured, as a CSV. Uses the generator's own amplitude unit (Vpp or Vrms, refuses dBm). Refuses a load setting it couldn't put back. Wiring: the channel's BNC to the meter's HI/LO. |
| `sweep_8648a_53181a.py` | **8648A → 53181A.** Steps the generator through frequencies, averages the counter at each, and writes a CSV, flagging scattered readings as SUSPECT. |

Every script that can switch an output on is **plan-only unless its "Switch … on" box
is ticked** *and* the run is allowed outputs. It records the instruments' settings
first and always restores them, output off first, printing a `restored:` line read
back from the instruments.

**All four have run on the real bench** from the Scripts window on the author's bench:

| Script | Run | Result |
|---|---|---|
| `bench_check.py` | 2026-09-23 | 5 of 5 answered, error queues empty, all released |
| `psu_dmm_check.py` | 2026-09-23, 1/2/5/10 V | E3641A inside its programming and readback spec; restored as found |
| `fgen_dmm_acv.py` | 2026-09-23, 0.5–5 Vpp at 1 kHz | 33522A CH1 −0.25%, inside spec; restored as found |
| `sweep_8648a_53181a.py` | 2026-09-23, from a terminal | found the 53181A channel 1 fault above ~120 MHz |

The numbers and their limits are in the author's lab report (not in this repo). All four were also run
against the simulator (`tools/sim_scpi.py`). That tests the flow, the refusals and
the restores, not the measurements.

`deploy.sh` refuses to ship if any catalog script fails `tools/check_script.py`, or
if `tests/test_scripts.py` finds a check that no longer catches what it should.
