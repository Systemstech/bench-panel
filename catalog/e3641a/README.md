# E3641A plugin — Keysight/Agilent E3641A DC power supply

Manual: Agilent E3640-90001 *User's Guide* (the January 2000 edition includes the
schematics). Every command in `driver.py` was taken from it.

- **Ranges:** LOW 35 V / 0.8 A, HIGH 60 V / 0.5 A. They aren't independent limits:
  HIGH caps current at 0.5 A. (Listings claiming "60 V, 2.5 A" are wrong: that's
  150 W on a 30 W supply.)
- **Setpoints outside the selected range are refused, not clamped.** The supply would
  clamp silently and then report a value you never asked for.
- **The CC/CV bits read backwards.** `STAT:QUES:COND?` reports what is *unregulated*:
  bit 0 set means constant **current**, bit 1 set means constant **voltage** (Table
  3-5). With the output off neither bit is set, and the card shows a dash rather than
  invent a mode.
- **Power-on:** no battery, no power-on recall. It always starts at 0 V, 0.8 A, **OVP
  66 V**, LOW range, output OFF (Table 3-4). OVP 66 V can never trip on the 35 V range,
  so set it.
- **Saved setups 1–5** (`*SAV`/`*RCL`, names via `MEM:STAT:NAME`, up to 9 characters,
  the same names the front panel's Recall menu shows). A setup **includes the output
  state**, so:
  - **Save is refused while the output is on.** A setup saved from the panel can never
    switch the output on when recalled.
  - **Recall takes two clicks**, because a setup saved from the front panel may hold
    output-on.
- `SYST:LOC`/`SYST:REM`/`SYST:RWL` are **RS-232 only**; over GPIB they queue `+514`.
  The core releases the panel with `device_local`. The front-panel Local key is
  **Recall**.
- Tested: GPIB through an ICS 8065, 2026-09-22. RS-232 via a VISA serial resource is
  **untested**, and would need `SYST:REM`, which no driver sends yet.
