# E3632A plugin — Agilent/HP E3632A DC power supply

Manual: Agilent E3632-90001 *User's Guide* (October 2007). Every command in
`driver.py` was read on its page; each action's `ref` in `manifest.json` is the
printed page number.

- **Ranges:** LOW 15 V / 7 A, HIGH 30 V / 4 A (Table 4-1, p. 80). Setpoints above
  the range's rating are **refused, not clamped**. The supply itself would accept up
  to 15.45 V / 7.21 A and 30.9 V / 4.12 A.
- **Protection:** OVP 1–32 V and **OCP** 0–7.5 A (p. 159), each with on/off and a
  clear. Both are ON at power-up at 32 V / 7.5 A (p. 94). Set them for the load.
- **Mode:** `STAT:QUES:COND?` returns 1 = CC, 2 = CV, 0 = output off, and 3 =
  **failure** (p. 108). 3 is shown as `FAIL`, not guessed into CC or CV.
- **Saved setups 1–3** (`*SAV`/`*RCL`, p. 95), no names. A setup **includes the
  output state**, so Save is refused while the output is on and Recall takes two
  clicks.
- **Readback check (`readback_ok`).** A broken readback ADC raises no error, and
  `*TST?` still passes. One unit on this bench read −3.45 mV / 11.96 mA at every
  operating point while delivering amps. In CV the sense voltage must equal the
  setpoint; in CC the current must equal the limit. If readback disagrees by more
  than a deliberately loose margin, `readback_ok` is false and the card dims the
  numbers and says to use a meter. With the output off there is nothing to compare,
  so it's `null` (unknown), not true.
- `SYST:LOC`/`SYST:REM`/`SYST:RWL` are **RS-232 only** (p. 99). Over GPIB the core
  releases with `device_local`.
- Simulator: `sim.py`, with presets including **`readback-stuck`**, which reproduces
  the ADC fault so the check can be seen working.
- Tested: the standard's checks and the simulator only. **Not yet on the real
  instrument.**
