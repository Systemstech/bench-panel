# 33522A plugin — Agilent 33522A two-channel 30 MHz function/arbitrary generator

Manual: Agilent *33500 Series Operating and Service Guide*. Every command in
`driver.py` was taken from it.

- **LAN-native:** add it as *Network (VXI-11 / LXI)* with its IP. That goes straight
  to the instrument (`inst0`), not through a GPIB gateway, and doesn't share a bus
  lock. Its GPIB and USB ports are listed in the manifest too, but only LAN has been
  tested. The front panel is handed back with `device_local`; the 33500 command set has
  no `SYST:LOC`.
- The card shows both channels at once, with one set of controls and a **CH1/CH2**
  selector: function (SIN SQU RAMP PULS NOIS DC), frequency, amplitude (Vpp), offset,
  load, and a per-channel **OUTPUT ON** (two clicks). Out-of-range settings come
  straight from the generator's own error queue, which gives the most precise refusal.
- **Load** (50 Ω / High-Z) changes no hardware. It tells the generator what load to
  assume when it *displays* amplitude, so it doubles or halves the figure shown.
- **Saved states 1–4.** Location 0 is the generator's own power-down state and is never
  written. Empty locations are marked. **Save asks twice only when it would overwrite**
  (`MEM:STAT:VAL?`). Names are up to 12 characters and start with a letter. A state
  stores function, frequency, amplitude, offset, duty, symmetry, modulation and
  display, but **not** output on/off. Recall still takes two clicks, because it can
  jump the amplitude on a live output. Recalling an empty location is refused.
- **Power-on:** outputs are always off. What else it restores depends on
  `MEM:STAT:REC:AUTO`, the card's "At power-on" row. The factory setting resets at every
  power cycle.
- Tested: direct LAN (VXI-11), 2026-09-22.
