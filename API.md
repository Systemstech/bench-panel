# Scripting BenchPanel

BenchPanel is driven by a small HTTP + JSON interface: the page uses it, and so can
your scripts. Anything the dashboard can do, a script can do, under the **same
safety rules**:

- The panel owns the instrument connections and the GPIB bus lock. A script only
  *asks*, so it can't collide with the page, with another script, or with the bus.
- Only actions a plugin declares are accepted.
- Anything that can switch an output on is refused without `"confirmed": true`.
- Instruments start OFF. A script powers on what it needs and should power it off
  again. `bench.session()` below does that for you, even on an error.

The per-instrument list of actions is **[API_ACTIONS.md](API_ACTIONS.md)**. It's
generated from the plugin manifests, so it always matches what the core accepts.

---

## The Python client (recommended)

`client/benchpanel.py`: one file, standard library only. Copy it next to your script,
or add the `client/` folder to `PYTHONPATH`.

```python
from benchpanel import Bench

bench = Bench("http://127.0.0.1:8765/")

with bench.session("8648a-1", "53181a-1"):          # ON now; OFF + released at the end
    bench.act("8648a-1", "set_freq", value=100)       # MHz
    bench.act("8648a-1", "set_ampl", value=-20)       # dBm, absolute
    bench.act("8648a-1", "output", value=True, confirmed=True)   # deliberate
    ...
    bench.act("8648a-1", "output", value=False)
```

| Call | What it does |
|---|---|
| `Bench(url, login_file=None, cafile=None)` | connect to a running panel (see *Reaching it* below) |
| `bench.instruments()` | `{id: state}` for the whole bench, in card order |
| `bench.get(id)` | the latest state of one instrument (may predate your last change) |
| `bench.read(id)` | state from a poll that **began after** this call and after your last `act()` on it |
| `bench.wait_for(id, test, timeout=30)` | poll until `test(state)` is true on a fresh reading |
| `bench.act(id, action, **args)` | one control; raises `BenchError` if refused (`check=False` returns the reply instead) |
| `bench.power_on(id)` / `bench.power_off(id)` | the card's ⏻ |
| `bench.session(*ids)` | power on for a `with` block; powers off only the ones it turned on |
| `bench.all_outputs_off()` | the header's ALL OUTPUTS OFF |
| `bench.manage(op, **kw)` | the Instruments manager (add, remove, install …) |

### Fresh readings: the one thing to get right

The panel polls each instrument about every half second, and `api/state` shows the
**last completed poll**. Set something and read straight back, and you get the value
from *before* your change. `bench.read()` and `bench.wait_for()` fix that exactly:

- every instrument's state carries `data_as_of`, the server time its last poll
  **began**;
- every action reply carries `at`, the server time the action finished;
- a poll that began after the action ended must have seen it, because polls and
  actions take turns on the instrument.

So `read()` waits until `data_as_of` is later than both *now* and your last `act()`.
Both times come from the server's clock, so your machine's clock doesn't matter.

**A measurement is not the same as a poll.** A counter only produces a reading when
its gate closes. To be sure a reading started after your change, reset the statistics
and wait for new samples, and throw the first one away:

```python
bench.act("8648a-1", "set_freq", value=500)
bench.act("53181a-1", "reset_stats")
bench.wait_for("53181a-1", lambda s: s["samples"] >= 1)      # may straddle the change
bench.act("53181a-1", "reset_stats")
s = bench.wait_for("53181a-1", lambda s: s["samples"] >= 3)  # three clean readings
print(s["avg"])
```

---

## Reaching it

**On the machine running the panel:** `Bench("http://127.0.0.1:8765/")`.

**From another machine, over an SSH tunnel** (no password; uses your SSH key):

```
ssh -N -L 8765:127.0.0.1:8765 you@server
```

Then use `Bench("http://127.0.0.1:8765/")`. **Keep the local port 8765:** the panel
checks the `Host` header and accepts only `127.0.0.1:<its own port>`. A different
local port is refused with *HTTP 421*, and the client says so.

**Through an HTTPS reverse proxy with a login** (Apache with Basic auth, as in `deploy/`):

```python
Bench("https://server/bench/", login_file="~/.config/benchpanel/login")
```

The login file holds one line, `user:password`, and must be `chmod 600`; the client
refuses a file other users can read. Create it yourself. Keep passwords out of scripts
and out of git. If the proxy's certificate comes from a private CA your system doesn't
trust, pass `cafile=` with that CA's certificate.

---

## The raw interface (any language)

| Request | Body | Reply |
|---|---|---|
| `GET api/state` | — | `{"instruments": [...], "log": [...], "log_seq", "polling", "time", "now"}` |
| `GET api/plugins` | — | installed plugins, the catalog, and any that failed to load |
| `POST api/action` | `{"instrument": id, "action": name, "args": {...}}` | `{"ok": bool, "msg"?, ..., "at": server_time}` |
| `POST api/action` | `{"action": "all_outputs_off"}` | `{"ok": true, "result": [...]}` |
| `POST api/action` | `{"action": "set_polling", "value": bool}` | `{"ok": true, "polling": bool}` |
| `POST api/manage` | `{"op": "add" \| "remove" \| "rename" \| "move" \| "install_catalog" \| ..., ...}` | `{"ok": bool, ...}` |

**Every POST must send `Content-Type: application/json` and `X-Bench-Panel: 1`.** A web
page on another site can't add that header, which is what stops a random page from
pressing your buttons. Without it you get *HTTP 403*.

Each instrument in `api/state` has the core's fields (`id`, `name`, `plugin`, `model`,
`where`, `enabled`, `connected`, `idn`, `power_msg`, `error`, `has_output`,
`data_as_of`) plus whatever its plugin reports (readings, settings). An instrument
whose plugin didn't load has `missing` with the reason instead.

```
curl -s http://127.0.0.1:8765/api/state
curl -s -X POST http://127.0.0.1:8765/api/action \
     -H 'Content-Type: application/json' -H 'X-Bench-Panel: 1' \
     -d '{"instrument": "34401a-1", "action": "power_on"}'
```

---

## The simulated bench: `<panel>/sim/`

Everything in this file works the same under `sim/`: `sim/api/state`, `sim/api/action`,
`sim/api/scripts`. Those drive the simulated bench, and `api/state` says
`"simulated": true`. Only there, `POST sim/api/sim` with `{op}` sets up the bench:

| op | body | does |
|---|---|---|
| `bench` | | instruments (ports, presets, models), and the wires |
| `wire` | `a`, `b` (`"iid:port"`) | connect an output to an input (either end's old wire comes off) |
| `unwire` | `end` | disconnect whatever is on that port |
| `wiring` | `wires: [[a, b], ...]` | replace all wiring |
| `preset` | `instrument`, `preset` | put an instrument in a starting state (switches it OFF in the panel first) |
| `power_cycle` | `instrument` | back to its power-on state |
| `offline` | `instrument`, `value` | not answering (true) or back (false) |

`sim/api/manage` is refused: the simulated bench copies the real one.

## Script editor ops (`POST api/scripts`, either bench)

| op | body | returns |
|---|---|---|
| `source` | `script` | `text`, `sha256` |
| `check` | `text` | the import checks plus `lint: [{line, msg}]` |
| `save` | `script`, `text`, `sha256` (of the copy opened; `null` = new) | `sha256`, `backup`, `check`. Refused if it changed meanwhile, or it's running |
| `template` | `name`, `uses`, `outputs` | `text` of a new script |
| `reference` | `uses` (empty = all) | each plugin's dictionary: `manual`, `actions`, `state`, `ports`, `instances` |
| `run` | also `wire: true` (sim only) | wires the simulated bench from the header first |

## Running scripts from the dashboard

A script with a `BENCHPANEL_SCRIPT` header can be imported into the dashboard's
**Scripts** window and run from there, with a form for its settings, a live log and
a Stop button. Write `Bench()` with no URL: the panel passes its address and a
per-run key through the environment. See **[SCRIPTS.md](SCRIPTS.md)**.

---

## Worked example: frequency sweep → CSV

`examples/sweep_8648a_53181a.py` steps an 8648A through a list of frequencies and
records what a 53181A counter reads at each, in a CSV the bench_analysis results
explorer opens directly.

```
# plan only: prints what it would do, switches nothing on
python3 examples/sweep_8648a_53181a.py --channel 2 --start 100 --stop 1000 --step 100
# run it
python3 examples/sweep_8648a_53181a.py --channel 2 --start 100 --stop 1000 --step 100 --rf-on
```

It records both instruments' settings first. It **always** switches RF off and
restores them at the end, on an error or Ctrl-C too, and it prints a `restored:` line
read back from the instruments so you can see that it did. RF goes on only with
`--rf-on`. Frequency and level are bounded per channel by the script's own
conservative limits. Read it as a template: most automation is this shape.

---

## Good habits

- Wrap work in `bench.session(...)` and put *output off* in a `finally:`.
- Pass `confirmed=True` only on the line that means to switch something on.
- Record what you change and put it back: read the state first, and restore it at the
  end.
- Don't assume a slow instrument has finished. Wait for its state to say so
  (`wait_for`), with a timeout.
- While a script runs, the dashboard still shows everything live, and **ALL OUTPUTS
  OFF** still works from the page.
