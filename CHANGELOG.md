# Changelog

What changed, newest first. "Verified" means on the real bench unless it says
simulator. The public repository starts at 1.6.2 with a fresh history, so commit
hashes quoted for earlier versions are not in it.

## 1.6.2 — 2026-10-27

- **First public release**, published from a fresh history.
- Site details moved out of the repo into the git-ignored `local/` folder: the deploy
  host (`local/deploy.env`) and the site's own docs page (`local/bench_docs.html`).
  `deploy.sh` reads the host from there and refuses to run without one.
- `deploy/bench-panel.service` uses `@USER@` / `@HOME@`, filled in by
  `deploy.sh --unit` (checked against the installed unit: identical).
- New `deploy/README.md`: setting up the service, the Apache proxy and the login.
- Example addresses in code, tests and docs are the documentation range 192.0.2.x.

## 1.6.1 — 2026-09-25

- **Licensed MIT**, copyright Ernesto Castoza: `LICENSE`, and `license` / `authors`
  in `pyproject.toml` (the build now needs setuptools 77 or newer for the SPDX form).
  The plugin manifests already said MIT.
- README: why we built it, credits, and screenshots in `docs/screenshots/`, all taken
  from the simulator or a placeholder bench on 192.0.2.x addresses.
- bench_docs: the 6063B and 438A plugins are no longer listed as not done.
- Disclaimer in the README and bench_docs (v3.7): real outputs can be switched on,
  readings are only as good as the instrument, and no affiliation with Keysight,
  Agilent or Hewlett-Packard.

## 1.6.0 — 2026-09-25

- **New plugin: HP 438A power meter** (1.0.0). Readings in W or dBm, all six
  measurement modes, per-channel cal factor, offset, range and filter, REL, ZERO,
  CAL ADJ, and the POWER REF output (on needs confirmation; ALL OUTPUTS OFF sends
  `OC0`). Every code is from the Operating and Service Manual 00438-90028 and cites
  its printed page.
  - 9.0000E+40, which the meter sends while it shows an error, is never a power:
    `reading` is null and the Status Message's error code says why.
  - While ZERO or CAL ADJ runs (any command aborts them) the driver only
    serial-polls, refuses every action, and waits for Cal/Zero Complete. After 60 s
    it gives up waiting and reads the error code.
  - Never sends PRESET or a Device Clear (both reset the cal factors to 100 %), RECALL
    (can switch POWER REF on), or trigger hold (the meter then answers nothing).
  - Simulator-tested only. **Not yet run on the real meter.**
- **Core: instruments older than IEEE 488.2.** A manifest may name its `identify`
  query (default `*IDN?`) and `keepalive` (`"serial-poll"` or a query, default
  `*OPC?`). Links gain `read()` (take what an instrument sends when addressed to talk)
  and `read_stb()` (serial poll), on the real transport, the simulator and the
  checker's fake link alike. Existing plugins are unchanged and still pass.
- `tests/test_438a.py` (in deploy.sh), including the Status Message the real meter
  returned on 2026-09-14, and a real `Link` checked to keep alive by serial poll.
  Mutation-checked: a command sent mid-ZERO, 9E+40 passed as a power, and a missing
  busy refusal are each caught. `test_conformance.py` gains two S0 cases.

## 1.5.1 — 2026-09-24

- **E3641A plugin 1.2.2:** setpoints are checked against the range read fresh from the
  supply, not the last poll's. `psu_dmm_check`'s restore sets the range, then the
  voltage, back to back. On a supply found on HIGH above 35 V, the old code refused the
  voltage against LOW's 35 V. The restore ignores errors by design, so the supply was
  left un-restored with no message. The reverse (LOW, then 50 V) was accepted and sent.
  The 09-23 real runs started on LOW and were unaffected.
  `tests/test_e3641a.py` (in deploy.sh) fails 4 checks on 1.2.1 and passes on 1.2.2.
  Simulator-tested. **Not yet re-checked on the real supply.**

## 1.5.0 — 2026-09-24

- **New plugin: HP/Agilent 6063B electronic load** (1.0.0), 240 V / 10 A / 250 W, CC/CR/CV.
  Commands from the Electronic Load Family Programming Manual 06060-90005, ranges from
  the 6060B/6063B Operating Manual's Table 5-1.
  - It never sends `*RST` (the manuals disagree on whether the factory input state is on),
    never writes slot 0 (the power-on state; a power loss mid-save forces a recal), and has
    no Short.
  - Mode and range changes are refused while the input is on. `protection_clear` needs
    confirmation because it re-enables the input.
  - A protection shutdown is read from the status register, because `INP?` stays 1.
    UNR is shown as UNREG.
  - A sense-switch hint appears when the load sinks current but reads ~0 V. That was
    this bench's commissioning fault.
- **E3632A 1.0.1:** setpoint limits are now checked against the range read fresh, not
  the last poll's. A script that changed range and set a level at once was judged on
  the old range. The 6063B does the same from the start.
- `tests/test_6063b.py` (32 checks, in deploy.sh). The E3632A test gains the
  change-range-then-set case, which fails on 1.0.0.
- Neither plugin has run on the real instrument yet.

## 1.4.0 — 2026-09-24

- **New plugin: Agilent/HP E3632A** (1.0.0), 15 V / 7 A or 30 V / 4 A. Setpoints
  refused above the range's rating, OVP (1–32 V) **and OCP** (0–7.5 A) with on/off/clear,
  three saved setups (Save refused with the output on, Recall two-click), and mode 3 =
  `FAIL` reported as such. Every action cites its page in E3632-90001 (Oct 2007).
- **Readback plausibility (`readback_ok`).** In CV the sense voltage must equal the
  setpoint, and in CC the current must equal the limit. If readback disagrees, the card
  dims the numbers and says to use a meter. `null` with the output off. Built because
  this bench's unit has a stuck ADC that raises no error and passes `*TST?`.
  The simulator's `readback-stuck` preset reproduces that fault.
- `tests/test_e3632a.py` (24 checks, in deploy.sh). `test_sim.py` now counts the catalog
  instead of assuming five plugins.
- **Not yet run on the real instrument** (`tested_on` is empty, so S6 warns).

## 1.3.2 — 2026-09-24

- **A plugin refused at start now shows as installed, with an Update button.** Before,
  the Instruments window said "None installed" and offered **Install** for a plugin that
  was on disk but failed the standard (a desktop install's 1.1.0 plugins after S17 arrived). The
  catalog now reports its on-disk version and offers **Update 1.1.0 → 1.2.1**, or
  **Reinstall** at the same version. Pressing it was always the fix; the page hid that.
- **Double-clicking Install no longer drops the request.** Two installs of one plugin in
  the same second collided on the `.old-<id>-<time>` backup name; the `OSError` escaped
  and the page said "Failed to fetch". Backups now get `-2`, `-3`…, and any disk error
  in the manager comes back as a message.
- **Addresses with a leading zero are read as decimal.** The C resolver reads
  `192.0.2.010` as octal (.8). A dotted IPv4 host or gateway is now made canonical
  (`.05` → `.5`), and a part over 255 is refused.
- The bench card's "did not load" reason is cut at a whole item, not mid-word.
- `tests/test_manager.py` (in deploy.sh) covers all three; it fails on 1.3.1.

## 1.3.1 — 2026-09-24

- **Manual page numbers for every action** in the five catalog plugins (1.2.1). Each was
  found in the manual on the lab page and read on its page before it was cited; `ref`
  is the page number printed on the page. S19 now passes with no warnings on all five.
  The E3641A guide (E3640-90001) numbers plainly (84); the 34401A (34401-90004) from
  chapter 4 (119); the 33500 guide plainly (205, one behind the PDF page); the 8648A
  (08648-90048) and 53181A (53181-90002) by chapter (2-24, 4-77).

## 1.3.0 — 2026-09-24

- **Script editor** in the Scripts window: Edit, New script… (from a template), highlighting,
  auto-indent, completion of action names and state fields, a live Checks pane with
  **lint** against the instruments' dictionaries, a Reference pane, and Save with a dated
  backup and a conflict check. Works offline. Save & simulate.
- **Simulator** at `<panel>/sim/`: the same dashboard on a simulated copy of the bench.
  Real drivers talk to per-plugin models (`sim.py`) through an in-memory link. It has
  virtual wiring, starting-state presets, a "not answering" fault, its own runner,
  run keys and `sim_results/` folder. The client refuses other addresses during a
  simulated run.
- **Plugin standard:** the scripting dictionary. `manual`, per-action `doc`/`args`/`scpi`/`ref`,
  and `state` (S17 MUST, S18 MUST: state docs = what `status()` reports, S19 SHOULD).
  Simulator models: S20, and S21 MUST (the driver runs against its own model with no
  unknown command on either side). Eight new mutation cases in `test_conformance.py`.
- **All five catalog plugins → 1.2.0:** dictionaries and models. The 53181A model's
  sensitivity is from its Operating Guide pp. 3-2 and 3-3.
- **Script header `wiring`** (optional; `{setting}` placeholders). Declared in the three
  catalog scripts that cable instruments (supply check 1.1.0, AC check 1.1.0, sweep 1.2.0).
- `tests/test_sim.py` runs in `deploy.sh`: every catalog script on the simulated bench
  with the real transport booby-trapped, the physics, refusals, Stop, key isolation,
  the editor's save and the lint.
- `API_ACTIONS.md` now includes each action's doc, arguments and SCPI.

## 1.2.0 — 2026-09-23 (edf6d06)

- **Result downloads.** The Scripts window lists each run's files as download links,
  and a Results section lists the whole results folder. `GET api/results/<path>`
  serves only regular files inside it: no `..`, no absolute paths, no symlinks.
- **Three catalog scripts:** `bench_check.py` (no outputs), `psu_dmm_check.py`
  (E3641A → 34401A), `fgen_dmm_acv.py` (33522A → 34401A). All three, plus the sweep,
  verified on the real bench 2026-09-23 (see SCRIPTS.md).
- Self-test: download-confinement cases.

## 1.1.0 — 2026-09-23 (a7a433e)

- **Scripts window:** import (catalog or a chosen `.py`, full source shown first),
  list, run one at a time with a live log, and Stop.
- A per-run key: output-on is refused unless the script declares outputs *and* the
  operator allows them; a script can't use the managers.
- If a script that can switch outputs on doesn't end cleanly, the panel presses
  ALL OUTPUTS OFF. Panel shutdown stops a running script first.
- `tools/check_script.py`, `tests/test_scripts.py`; both run in `deploy.sh`.
- Client: `Bench()` with no URL uses the panel that started the script.

## 1.0.0 — 2026-09-23 (a56098e and earlier)

- **Package and launcher:** the `benchpanel` command, a per-user data folder, and a
  desktop menu entry (the Windows shortcut is untested).
- **Scripting API:** `client/benchpanel.py` (`read()` waits for a fresh poll),
  `API.md`, a generated `API_ACTIONS.md`, and the 8648A → 53181A sweep example.
- **Plugin standard** S0–S16, enforced at load, install and deploy; the **53181A**
  plugin; the guide to writing plugins with Claude (plus a PDF).
- **Plug-and-play core:** no built-in instruments, `site.json`, and the Instruments
  manager. A refused gateway connection is retried.
