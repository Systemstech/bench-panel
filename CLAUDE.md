# BenchPanel — notes for Claude Code

A stand-alone, plug-in test-instrument dashboard, installable as the `benchpanel`
command (`pyproject.toml`) or run portably as `python3 bench_panel.py`. The core
(`benchcore/`, `static/`) knows **no** instrument; every instrument is a plugin folder
(`catalog/<id>/`: manifest.json, driver.py, card.html, card.js, README.md).

Read before changing anything: `README.md`, `PLUGIN_STANDARD.md` (the contract),
`PLUGINS.md` (the how-to), `PLUGINS_WITH_CLAUDE.md` (the workflow for writing a
plugin with you).

## Writing or changing a plugin

1. **Every SCPI command comes from the instrument's own programming manual, with the
   page number in a comment.** Confirm from the title page that the manual covers
   the exact model and serial prefix; sister models (53131A vs 53181A) differ. If
   the manual doesn't say, say so. Don't infer from a similar instrument.
2. **Verify each page citation against that manual** before relying on it.
3. **Never block the bus.** No `READ?`/`MEAS?`/`FETC?` unless the instrument has
   already reported that a result exists, via a register or event that never blocks
   (see `catalog/53181a/driver.py`). Handle "no signal" / "no trigger".
4. **Read-only probe first.** Before designing a driver, write a probe that only
   queries settings, shows the user every command before running it, and hands the
   front panel back (`device_local`).
5. **Outputs:** never switch an instrument output on without the user's explicit
   go-ahead in this conversation. Any action that can turn an output on is declared
   `"confirm"` in the manifest and refuses without `"confirmed": true`.
6. **Record, then restore:** whatever `on_connect()` changes, `on_release()` puts back.
7. **Document the plugin's language in the manifest** (S17-S19): `doc` + typed `args` +
   `scpi` + `ref` (manual page -- only a page you have looked at) for every action,
   `state` for every field `status()` reports, `manual` by title/part/edition. Never
   copy the manual's PDF into the plugin.
7a. **Ship a `sim.py` model** that answers exactly the driver's SCPI (S21 runs one against
   the other). Mark every number that is not from the manual as the model's own.
8. **The plugin must pass** `python3 tools/check_plugin.py catalog/<id>` with no MUST
   failures. After changing `benchcore/conformance.py`, run
   `python3 tests/test_conformance.py`: every mutation must be caught.
9. **Report evidence:** readings against a known source, the error stated, the
   instrument's error queue, and settings read back from the instrument after
   restoring. Not "it works".
10. **No addresses in plugins or docs.** They live in `site.json`, which is gitignored.

## Writing an automation script

Scripts the dashboard imports carry a `BENCHPANEL_SCRIPT` header. See `SCRIPTS.md`.
Go through `Bench()` and never `pyvisa`. Declare `"outputs"` honestly. Restore in
`finally:`. Check with `python3 tools/check_script.py <file>`. After changing
`benchcore/scripts.py`, run `python3 tests/test_scripts.py`.

## Testing without hardware

The simulator: `<panel>/sim/` (benchcore/sim.py + each plugin's sim.py). Wire it with
`POST sim/api/sim`, run scripts through `sim/api/scripts`. After changing the simulator,
a model, or the editor ops, run `python3 tests/test_sim.py`. A simulated result proves
a script's logic and the driver/model agreement, never an instrument's accuracy.
`tools/sim_scpi.py` is the older bare socket; it proves wiring only.

Use a scratch `--site` and `--plugins` when running the panel for tests, so the
user's real `site.json` and installed plugins are untouched.

## Gotchas

- The panel's `/api/state` reflects an action only after the next poll (~0.5 s). Wait
  for it, or read the instrument directly, before asserting.
- `pkill -f <pattern>` can match your own shell's command line. Stop processes by
  PID.
