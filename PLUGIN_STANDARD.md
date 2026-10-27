# The plugin standard — schema 1

The rules every instrument plugin must meet for the bench panel to accept it. The
same rules are code, in `benchcore/conformance.py`, and one checker enforces them
everywhere:

| Where | What happens to a plugin that breaks a MUST |
|---|---|
| `python3 tools/check_plugin.py <folder>` | reported, exit status 1 |
| `deploy/deploy.sh` (runs the checker on all of `catalog/`) | the deploy stops |
| Instruments manager, **inspect** screen | shown before you decide (file checks only, runs no plugin code) |
| Instruments manager, **install** | refused; nothing is replaced |
| program start | not loaded; listed under Installed plugins as *Not loaded* with the rule |

**MUST** rules are refused. **SHOULD** rules are reported as warnings and never
refused. Rule numbers (S0–S16) are the ones the checker prints.

`tests/test_conformance.py` proves the checker catches things. It breaks a copy of
a real plugin one way at a time and checks that the right rule fails. Run it after
changing the checker.

`PLUGINS.md` is the how-to. This file is the contract.

---

## Manifest

| Rule | Level | Requirement |
|---|---|---|
| S0 | MUST | `manifest.json` parses. `schema` is `1`. `id` is 2–40 of `a-z 0-9 _ -` and equals the folder name. `name`, `model`, `version` present. `idn_match` is a non-empty list. `has_output` is `true`/`false`. `connections` lists one or more of `gpib-gateway`, `vxi11`, `visa`. `driver.file`, `driver.class`, `card.html`, `card.js` name files that exist, flat in the folder. Optional: `identify` (a printable query, default `*IDN?`) and `keepalive` (`"serial-poll"` or a query ending in `?`, default `*OPC?`) |
| S1 | MUST | `capabilities` is a non-empty list from the vocabulary below |
| S2 | MUST | `actions` declares **every** control the card can call, with lower_snake_case names |
| S3 | MUST | no action uses a name the core owns: `power_on`, `power_off`, `all_outputs_off`, `set_polling` |
| S4 | MUST | each action is `{"example": {...}, "confirm": ...}`, both optional. `confirm` is `"always"` or `{"when": {"arg": value, ...}}` |
| S5 | MUST | if `has_output` is true, at least one action is declared with `confirm`: the one that switches the output on |
| S6 | SHOULD | `tested_on` (per connection type: when, how), `quirks` as `[{id, note}]`, `description`, `author`, `license`, and a `README.md` |
| S17 | MUST | **the scripting dictionary.** Every action has a `doc` and an `args` object (`{}` for none) giving each argument's `type` (`float int str bool choice`, with `choices`, and optionally `unit`, `min`, `max`, `doc`). Every `example` argument is documented and fits its type. `state` documents the fields `status()` reports, each with `type` (the argument types plus `list`, `object` with `fields`) and `doc` |
| S19 | SHOULD | `manual` names the programming manual (`title`, plus `part` and `edition` when it has them): a reference to it, never a copy. Each action says what it sends (`scpi`) and cites the manual's page (`ref`) |

**`example`** holds the arguments the checker calls the action with. Give values
that are valid for the instrument.

**The scripting dictionary** (S17-S19) exists because a script never speaks SCPI.
It speaks the plugin's own language, its actions and state fields, through the
panel. The manifest is where that language is written down, and the script editor
reads it for its reference pane, its completion and its lint. The manual's PDF is not
shipped: it's large and it's the manufacturer's copyright. `manual` names it, and
`scpi` + `ref` point into it.

```json
"manual": {"title": "Agilent E364xA ... User's and Service Guide", "part": "E3640-90001",
           "edition": "Tenth Edition, August 6, 2013"},
"actions": {
  "set_v": {"doc": "Set the output VOLTAGE setpoint.",
            "args": {"value": {"type": "float", "unit": "V", "min": 0, "max": 60, "doc": "..."}},
            "scpi": "VOLT <value>", "ref": "p. <page you looked at>", "example": {"value": 1.0}}},
"state": {"v_set": {"type": "float", "unit": "V", "doc": "voltage setpoint, read back", "nullable": true}}
```

**`identify` and `keepalive`** are for instruments older than IEEE 488.2, which
have no `*IDN?` and no `*OPC?`. The core asks `identify` at power-on and matches the
answer against `idn_match`; it runs `keepalive` while polling is paused. The HP 438A
answers `?ID`, and anything it does not know shows an entry error on its display and
aborts a ZERO in progress, so its keepalive is `"serial-poll"`: a bus operation, not a
program code. Such a driver also gets `self.link.read()` (take what the instrument
sends when addressed to talk, sending nothing first) and `self.link.read_stb()`.

**`confirm`** is enforced **by the core**, before the driver is reached. A call that
needs confirmation and lacks `"confirmed": true` in its arguments is refused, and so
is any action the manifest doesn't declare. `{"when": {"value": true}}` means
"confirmation is needed when `value` is true". That's how `output` is declared:
switching on needs it, switching off doesn't.

### Capability vocabulary

`dc_source`, `electronic_load`, `multimeter`, `rf_source`, `function_generator`,
`frequency_counter`, `power_meter`, `oscilloscope`, `spectrum_analyzer`,
`network_analyzer`. The list grows with the core. Adding one is a core change, made
in `conformance.py` and here.

## Driver

| Rule | Level | Requirement |
|---|---|---|
| S7 | MUST | parses; defines the manifest's class at top level; imports nothing that moves bytes (`socket`, `pyvisa`, `pyvisa_py`, `serial`, `usb`, `http`, `urllib`, `requests`, `subprocess`, `ctypes`, `multiprocessing`). All I/O goes through `self.link` |
| S8 | MUST | overrides none of the core's methods: `power_on`, `power_off`, `mark_lost`, `shutdown`, `base_state`, `matches` |
| S8 | SHOULD | doesn't set `model`, `idn_match` or `has_output` in the class; they come from the manifest |
| S9 | MUST | no IP address or `gpibN,M` address anywhere in the driver or card. Addresses belong in `site.json` |
| S12 | MUST | imports cleanly and subclasses `benchcore.instrument.Instrument`. If the class sets `has_output`, it agrees with the manifest. If `has_output` is true, it implements `all_off()` |
| S13 | MUST | `on_connect()`, `poll()` and `status()` run without raising. `status()` is JSON-serialisable and keeps the core's fields (`plugin model where idn connected enabled power_msg error has_output`) |
| S14 | MUST | every declared action, called with its `example`, returns a dict with a boolean `ok` and doesn't raise. An undeclared name returns `ok: false` and sends nothing |
| S15 | MUST | **an action declared with `confirm`, called without the flag, returns `ok: false` and sends nothing.** Tested twice, with every query answering `0` and then `1`, so a refusal for an unrelated reason can't pass. **Any other action on an instrument with outputs sends no output-enabling command, with or without the flag** (`OUTP… ON/1`, `INP… ON/1`, `*RCL`). If it can, it must be declared with `confirm` |
| S16 | MUST | if `has_output`: `all_off()` sends at least one command and returns `True` |
| S18 | MUST | the `state` documented in the manifest is **exactly** what `status()` reports beyond the core's fields (`plugin model where idn connected enabled power_msg error has_output data_as_of`), one level into `object` fields: nothing undocumented, nothing documented that isn't there |

## Simulator model (optional file `sim.py`)

| Rule | Level | Requirement |
|---|---|---|
| S20 | SHOULD | the plugin ships a model of the instrument for the `/sim/` bench, named in the manifest as `"sim": {"file": "sim.py", "class": "..."}`. Without one, the simulator uses a generic stand-in that answers 0 to everything |
| S20 | MUST | if `sim` is present: the file imports and the class subclasses `benchcore.sim.SimModel` |
| S21 | MUST | **the driver runs against its own model**: power on, poll, every action with its example (and `confirmed`), power off. Neither side may meet a command it doesn't know. The model queues `-113 Undefined header` for an unknown command and sends no reply to an unknown query, as a real instrument does. Errors are recorded as they're queued, so a driver that drains its own error queue can't hide them |

S21 is a consistency check between two files the same author wrote. It catches a
typo on either side. It doesn't prove the SCPI is right for the instrument: the
manual, and then the instrument, still do that.

S12–S16 run the driver against a pretend connection that records every write and
answers queries with plausible values. That is how the safety rules are tested: by
trying to break them. **It cannot prove the SCPI is right for the real instrument.**
Only the instrument can prove that, which is what `tested_on` records.

## Card

| Rule | Level | Requirement |
|---|---|---|
| S10 | MUST | `card.html` has no `id=` attributes and `card.js` doesn't use `getElementById` (the same card can be on the page twice; use `data-k`). `card.js` calls `BenchPanel.register('<manifest id>', …)`. `card.js` makes no network calls of its own (`fetch`, `XMLHttpRequest`, `WebSocket`, `EventSource`); it talks to the core only through `card.act()` |
| S11 | MUST | every `card.act('name')` names a declared action |
| S11 | SHOULD | every declared action is used by the card |

## What the standard deliberately leaves to people

- Whether the SCPI strings are right: the manual, then the instrument.
- Whether a refusal message is helpful.
- What goes on the card.

Those are what review and `tested_on` are for.

## Changing the standard

A new rule is a change to `conformance.py`, this file, and a new case in
`tests/test_conformance.py` that proves the rule catches something. A change that
existing plugins can't meet also bumps `schema`.
