# 8648A plugin — HP 8648A signal generator

100 kHz – 1000 MHz, −136 to +13 dBm.

- **The big number is the ABSOLUTE level at the connector.** That isn't always what the
  instrument holds: in **reference mode** (`POW:REF:STAT` on) the entered amplitude is
  a *delta*, so `actual = entered + POW:REF?`. The reference is non-volatile. On one
  bench it made a sweep look about 4.25 dB "high" and the generator was wrongly recorded
  as out of spec; it was a stored +5.00 dBm reference. The driver converts in one place
  (`set_ampl`). The card shows the held value beside the absolute and puts up an amber
  banner whenever reference mode is on.
- **Reference mode is read-only here.** Pressing REF on the front panel captures the
  *current* setting as the new reference, so a bare `POW:REF:STAT ON` could overwrite a
  stored reference. Toggle it at the front panel.
- RF is an output: two-click arm, refused by the server without the flag, and included
  in ALL OUTPUTS OFF and ⏻ OFF. Level is refused outside −136…+13 dBm and frequency
  outside 0.1–1000 MHz, both checked against the **absolute** level. A held attenuator
  (`POW:ATT:AUTO` off) and any active modulation are shown, since both change what a
  power meter downstream reads.
- **Power-on:** it keeps its state in battery-backed RAM (battery A3BT1, 3.0 V 1.2 Ah,
  HP 1420-0338). It powers up where it was left, **except RF is always off**. It has 300
  save/recall registers in 10 sequences (`*SAV`/`*RCL <reg>,<seq>`), **not yet on the
  card**. Error 627 "Battery RAM failure" means the battery has gone.
- Tested: GPIB through an ICS 8065, 2026-09-22.
