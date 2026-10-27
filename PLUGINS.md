# Writing an instrument plugin

The rules are in **[PLUGIN_STANDARD.md](PLUGIN_STANDARD.md)**, and
`python3 tools/check_plugin.py <folder>` checks them, using the same code the program
runs when it installs or loads a plugin. This file is the how-to.

A plugin teaches the bench panel one instrument **model**. It never knows the
instrument's address: that's in the user's `site.json`. The four plugins in `catalog/`
are the working examples. `catalog/34401a/` is the simplest (it has no outputs), and
`catalog/e3641a/` shows everything an instrument with an output needs.

## The folder

```
myinst/
  manifest.json   what it is
  driver.py       how to talk to it (Python)
  card.html       what its card looks like (markup only)
  card.js         how the card behaves (JavaScript)
  README.md       notes for people: quirks, manual references
```

One flat folder. Allowed file types: `.py .json .html .js .css .svg .md .txt .png`. The
folder name must equal the manifest `id`.

## manifest.json

```json
{
  "schema": 1,
  "id": "34401a",
  "name": "HP/Agilent 34401A 6.5-digit multimeter",
  "vendor": "HP / Agilent / Keysight",
  "model": "34401A",
  "version": "1.0.0",
  "idn_match": ["34401A"],
  "has_output": false,
  "connections": ["gpib-gateway", "visa"],
  "capabilities": ["multimeter"],
  "tested_on": {"gpib-gateway": "2026-09-22, ICS 8065"},
  "driver": {"file": "driver.py", "class": "Dmm34401A"},
  "card": {"html": "card.html", "js": "card.js"},
  "description": "Function, range, NPLC and running min/max/avg. No outputs.",
  "quirks": [{"id": "overload-sentinel", "note": "9.9E37 is the overload value, not a reading."}],
  "author": "you",
  "license": "MIT",
  "actions": {
    "function":     {"example": {"value": "DCV"}},
    "clear_errors": {}
  }
}
```

An instrument with an output declares which action switches it on:

```json
  "actions": {
    "output":       {"example": {"value": true}, "confirm": {"when": {"value": true}}},
    "recall_state": {"example": {"slot": 1}, "confirm": "always"}
  }
```

| Field | Rule |
|---|---|
| `schema` | `1` |
| `id` | 2–40 characters of `a-z 0-9 _ -`, starting with a letter or digit |
| `idn_match` | power-on is refused unless one of these appears in the `*IDN?` reply (case-insensitive) |
| `has_output` | **true if the instrument can source power or signal.** The core's safety rules depend on it: outputs off on every exit path, ALL OUTPUTS OFF, the warning at install. Be honest |
| `connections` | any of `gpib-gateway`, `vxi11`, `visa`. Only these are offered when adding one |
| `tested_on` | what you actually tested it on. It's shown before install, and it's how users judge a plugin |
| `quirks` | the hard-won facts. Shown before install. Fill it in |
| `capabilities` | what the instrument is, from the vocabulary in PLUGIN_STANDARD.md |
| `actions` | **every** control the card calls. The core refuses anything undeclared, and refuses a `confirm` action without `"confirmed": true`, before your driver sees it |
| `version` | bump it on every change; the catalog offers **Update x → y** |

## driver.py

Subclass `benchcore.instrument.Instrument`:

```python
from benchcore.instrument import Instrument

class Dmm34401A(Instrument):
    def __init__(self, link, log):
        super().__init__(link, log)
        self.state = {"reading": None, "last_error": None}   # what the card shows

    def on_connect(self):                 # optional: runs once, after *IDN? matched
        self.link.write("CONF:VOLT:DC")

    def poll(self):                       # every ~0.5 s while ON in the panel
        with self.lock:
            v = self.link.query("READ?")  # str, or None if the exchange failed
            self.state["reading"] = float(v) if v else None
            return self.status()

    def action(self, name, args):         # one control on the card
        with self.lock:
            if name == "clear_errors":
                return {"ok": True, "errors": self.drain_errors()}
            return {"ok": False, "msg": f"unknown action {name}"}
```

**What you get:** `self.link.query(scpi)` → `str | None`, `self.link.write(scpi)` →
`bool`, `self.link.facets` (`bus_control`, `local`), `self.state`, `self.lock`,
`self.drain_errors()`, and `self.log(label, sent, got, kind)`.

**What you must not do:** open a socket, serial port or VISA session yourself; hard-code
an address; know which transport you're on. If you need something the link doesn't
offer, that's a gap in the core and should be fixed there.

**Rules the core enforces or relies on:**

- If `has_output` is true you **must** implement `all_off()`. It switches every output
  off and returns `True` only if that was confirmed. The loader refuses the plugin
  without it.
- Don't set `model`, `idn_match` or `has_output` in the class. The loader copies them
  from the manifest, and refuses a driver whose own `has_output` disagrees.
- **Anything that can switch an output on must refuse without `args["confirmed"]`**:
  output enable, and recalling a stored state that may include output-on. The card
  asks twice; the driver is what makes that a rule.
- **Refuse, don't clamp.** A setpoint outside what the instrument can do should return
  `{"ok": False, "msg": "..."}` rather than send something else.
- The power button, power-on `*IDN?` check, power-off, lost-instrument handling and
  safe shutdown are the core's. Don't override `power_on`, `power_off`, `mark_lost` or
  `shutdown`.
- `status()` defaults to the core's fields plus `self.state`. Override it to add
  derived values (limits, choice lists) for the card.

Every action returns a dict: `ok` (bool), optionally `msg` (shown on the card),
`errors` (list, shown instead), or `confirm: true` to ask the card for a second click.

## card.html

Markup for the inside of the card. The core draws the header (name, address, ⏻,
messages) around it. **Never use `id=`**, because the same card can be on the page
twice. Give every element you need a `data-k="name"`.

Classes from the core stylesheet you can use: `lcd`, `big` (+ `u` for units), `stats`,
`sec` (+ `h4`), `row`, `grid`, `pill` (`on off cc cv trip`), `banner`, and on buttons
`go`, `danger`, `sel`. A `<style>` block in the card is allowed. Keep its selectors
specific to your card.

## card.js

```js
BenchPanel.register('34401a', {         // the manifest id
  mount(card) {                          // once, after card.html is inserted
    card.head.innerHTML = '<span class="pill" data-p="rng">--</span>';
    card.k('reset').onclick = () => card.act('reset_stats');
  },
  update(card, st) {                     // every refresh, with the driver's status()
    card.k('val').textContent = card.fmt(st.reading, 6);
  },
});
```

`card` gives you:

| | |
|---|---|
| `card.k('name')` | the element with `data-k="name"` inside this card |
| `card.act(action, args)` | POST to your driver's `action()`; shows its message; returns the result |
| `card.head` | a slot in the header for status pills |
| `card.local` | an object for this card's own UI state (selected channel, slot…) |
| `card.state` | the latest state |
| `card.repaint()` | call `update` again now, e.g. after changing `card.local` |
| `card.msg(text)` | the card's message line |
| `card.arm(btn, armedText, fn)` | two-click arm: first click arms for 4 s, second runs `fn` |
| `card.isArmed(btn)` / `card.disarm(btn)` | don't overwrite an armed button's label |
| `card.buttons(el, items, current, fn)` | a row of choice buttons; items are strings or `{label, value}` |
| `card.fmt(v, digits)`, `card.fmtHz(hz)`, `card.esc(s)` | formatting; `fmt` shows `9.9E37` as `OVLD` |

Anything that switches an output on goes through `card.arm(...)` and sends
`confirmed: true`. Use `textContent`, not `innerHTML`, for anything that came from the
instrument.

## The scripting dictionary (in manifest.json)

Scripts drive your instrument by its **action names and state fields**, never by
SCPI. So the manifest documents them (standard S17-S19): a `doc` and typed `args`
for every action, what it sends (`scpi`) and the manual page (`ref`), a `state`
entry for every field `status()` reports, and `manual` naming the programming guide
by title, part number and edition. The script editor builds its reference pane,
completion and lint from it. S18 compares `state` with what `status()` really
returns, so the two can't drift apart. See `catalog/e3641a/manifest.json`.

## sim.py: a model for the simulated bench

Optional, but without one the `/sim/` bench can only offer a stand-in that answers
0. A model is a `benchcore.sim.SimModel` subclass:

```python
from benchcore.sim import SimModel, Undefined, OutOfRange, num, onoff, sci

class MySim(SimModel):
    OUTPUTS = {"out": "output + / -"}            # ports other instruments can be wired to
    INPUTS = {}
    PRESETS = {"output-on": "output left ON"}    # front-panel situations to test scripts with

    def power_on_reset(self): self.v, self.out = 0.0, False
    def query(self, h, arg):                      # h: upper case, no '?', no leading ':'
        if h == "VOLT": return sci(self.v)
        raise Undefined()                          # -> -113 and NO reply, like the real thing
    def command(self, h, arg):
        if h == "VOLT": self.v = num(arg, ("V",)); return
        raise Undefined()
    def source(self, port, z_load):              # what it drives into a wired input
        return {"dc": self.v, "vrms": 0, "freq": None, "wave": "DC"} if self.out else None
```

Answer **exactly** the SCPI your driver sends, and nothing else. S21 runs the driver
against the model and fails on anything either side doesn't know. Model the
behaviour a script can get wrong (a unit, a load setting, a measurement that never
completes with no signal), and mark every number that isn't from the manual as the
model's own. See the five catalog models.

## Trying it

0. `python3 tools/check_plugin.py path/to/myinst` must say PASS. Fix every FAIL.
   Warnings are advice.
1. Put the folder in `catalog/` (or zip it: the files at the top level or inside one
   folder) and install it from the Instruments manager. You see your manifest as a user
   will.
2. With no hardware: open **Simulator** (`<panel>/sim/`). Your card appears there, driven
   by your `sim.py`, so you can click every control and watch the SCPI in the traffic
   log. `tools/sim_scpi.py` still exists for a bare socket, but it checks none of your
   SCPI.
3. On the real instrument: power on, exercise every control, power off, and check the
   output is off and the front panel is back. Then record it in `tested_on`.

A plugin that failed to load is listed under Installed plugins as *Not loaded*, with
the reason. The startup console lists it too.
