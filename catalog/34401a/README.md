# 34401A plugin — HP/Agilent 34401A 6½-digit multimeter

No outputs, so there's nothing it can damage.

- On ⏻ it sets DC volts, autorange and `TRIG:SOUR IMM`: a known state, rather than
  whatever the last script left behind.
- **NPLC is an accuracy control, not a speed switch:**

  | NPLC | digits | rate |
  |---|---|---|
  | 100 | 6½ | slowest, most rejection |
  | 10 | 6½ | about 0.6 rdg/s, full line-noise rejection |
  | 1 | 6½ | about 6 rdg/s |
  | 0.2 | 5½ | |
  | 0.02 | 4½ | hundreds/s, no line rejection |

  Past that, one query per reading over VXI-11 tops out near 10–30 rdg/s. Going faster
  needs the meter's own buffer (`TRIG:COUN`/`INIT`/`FETCH?`), which isn't implemented.
- **`9.9E37` is the overload sentinel**, shown as `OVLD`.
- **Power-on:** no battery and **no saved setups** (no `*SAV`/`*RCL`). It powers up at
  DC volts, autorange, 10 PLC. The GPIB address, interface, *language*, beeper, baud
  and calibration live in a non-volatile EERAM (U505 in the service guide).
- **In LOCAL it answers `*IDN?` and `CONF?` but refuses `READ?`** with
  `+550 "Command not allowed in local"`, which looks like a dead measurement. The core
  asserts REN on every gateway connect for exactly this reason. The front-panel Local
  key is **Shift**.
- Tested: GPIB through an ICS 8065, 2026-09-22.
