# HP 438A power meter — BenchPanel plugin

A two-channel RF power meter for the HP 8480-series sensors (8481A, 8484A, …).
The plugin reads it, sets it up per channel, zeroes and calibrates it, and
switches its 1 mW / 50 MHz **POWER REF** output.

**Status: 1.0.0, tested only against the simulated meter, not the real one yet.**
`tested_on` in the manifest stays empty until it has run on a real 438A.

## Manual

*HP 438A Power Meter Operating and Service Manual*, part **00438-90028**,
1986-04-01. Every program code in `driver.py` cites the page it came from, as
printed on the page. In section 3 the printed page 3-N is PDF page N + 30. The code
summary is Table 3-7 on p. 3-47. The manual is not copied into the plugin.

## Not SCPI

The 438A predates IEEE 488.2. It has no `*IDN?`, no `*OPC?` and no error queue.
It takes two-letter program codes run together, and every number ends in `EN`
(`AEKB97.9EN` = set sensor A's cal factor to 97.9 %). So the manifest carries two
fields most plugins leave out:

| Field | Value | Why |
|---|---|---|
| `identify` | `?ID` | returns `HP438A,VERX.XX` (p. 3-40) |
| `keepalive` | `serial-poll` | a bus operation, not a program code. An unknown code shows an entry error on the display, and any code aborts a ZERO or CAL ADJ in progress (p. 3-49) |

## How it reads

| What | How | Page |
|---|---|---|
| The reading | addressed to talk, with nothing sent first; in free run it is the latest measurement | 3-34 |
| Everything else | the 23-character Status Message (`SM`): errors, mode, ranges, filters, units, entry channel, POWER REF, REL, trigger | 3-40, 3-41 |
| Cal factors, offsets | the Learn Mode #1 string (`LP1`) | 3-37 |

`9.0000E+40` is what the meter sends while its display shows an error (p. 3-35).
It is never shown as a power: `reading` becomes null and `meas_error` gives the
code (Table 3-8, pp. 3-57 to 3-59).

## ZERO and CAL ADJ

Both take seconds (10.1 s and 4.0 s on this bench, 2026-09-18), and **any command
received meanwhile aborts them** (p. 3-49). So while one runs the plugin sends
nothing but serial polls, refuses every action, and waits for status-byte bit 1,
Cal/Zero Complete (p. 3-45). It clears the status byte first (`CS`), so the bit can
only mean this operation. If the bit hasn't come after 60 s, the plugin stops
waiting and reads the meter's error code instead. The manual doesn't say whether
a *failed* zero sets the bit.

- **ZERO** (two clicks): no RF may be on the sensor. Any power present becomes an
  offset in every later reading (p. 3-86).
- **CAL ADJ** (two clicks): the sensor must be on POWER REF. Enter the sensor's
  *reference* cal factor from its label; the 8481A's is 100.0 %. The meter switches
  the reference on by itself and puts it back afterwards (p. 3-50). Zero first
  (p. 3-49). Each channel is calibrated with its own sensor (p. 3-48).

## Deliberately never sent

- **`PR` (PRESET), and so never a Device Clear either:** they do the same thing
  (p. 3-70). Preset sets both cal factors to 100 % and both offsets to 0 dB
  (p. 3-69), so later readings are silently off by the sensor's cal factor. It
  does not undo the calibration itself (p. 3-70).
- **`RC` (RECALL):** a stored setup includes the POWER REF state (p. 3-84), so a
  recall can switch the output on.
- **Trigger hold and the triggered modes (`TR0`–`TR2`):** in hold the meter
  outputs nothing but its serial-poll byte (p. 3-33), so it can't even answer
  `?ID` and looks switched off. The plugin keeps it in free run. If another program
  left it in hold, cycle its LINE switch; free run is the turn-on default (p. 3-30).
- Store, limits, display test, group trigger, service-request mask: not needed yet.

## Actions

`measure` (A, B, A/B, B/A, A-B, B-A) · `units` (W, dBm) · `cal_factor` · `offset` ·
`range` (auto, hold, 1–5) · `filter` (auto, hold, 0–9) · `rel` · `osc` (POWER REF;
on needs confirmation) · `zero` (confirm) · `cal_adj` (confirm). The full list, with
arguments and units, is in the manifest and the script editor's reference pane.

## Not verified yet

- The whole plugin on the real meter.
- The exact spacing of the real learn string. The parser is built from Table 3-3
  and the fragment `BEKB100.0EN OS00.00EN` this meter sent on 2026-09-15. It ignores
  whitespace, and it gives null rather than a guess if it can't find a field.
- How a linear ratio or REL % is scaled on the bus (100 or 1.00 for equal power).
  The manual doesn't say.
- Whether a failed ZERO or CAL ADJ sets Cal/Zero Complete.

## First real test (with the meter on, one panel only)

1. Add it: GPIB gateway, address **13** (the factory default, p. 3-46).
2. Press ⏻. The card should show the meter's mode and units, and the Traffic log
   should show `?ID` → `HP438A,VER5.00`.
3. With the sensor on POWER REF: **REF ON** (two clicks). Expect about 1.000 mW,
   matching the meter's own display. **REF OFF**.
4. Only then try ZERO (sensor off the reference, no RF) and CAL ADJ.

## Simulator

`sim.py` models the meter with an 8481A on channel A. Wire a source to `a` or `b`,
or POWER REF to a sensor: `"438a-1:ref" -> "438a-1:a"`. Presets: `uncalibrated-a`
(+0.79 dB until CAL ADJ, as found on 09-18), `needs-zero` (error 15), `no-sensors`
(error 31, the documented power-up state), `sensor-on-b`, `osc-left-on`,
`trigger-hold`. The model's own choices, which aren't from the manual, are listed
at the top of `sim.py`. A simulated reading proves the plugin's logic, never the
meter's accuracy.
