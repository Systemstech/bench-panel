# 6063B plugin — HP/Agilent 6063B DC electronic load

240 V, 10 A, 250 W, single input. CC, CR and CV.

Manuals: the **Electronic Load Family Programming Manual 06060-90005** (April 2000
update) for commands (each action's `ref`), and the **6060B/6063B Operating Manual
5951-2826** for the 6063B's ranges (Table 5-1) and defaults (Table 4-6). The
programming manual is a family guide. It uses the 6060A in its examples and says they
apply to any electronic load, but it never names the 6063B.

## What the panel does, and deliberately doesn't do

- **The input is the "output".** With the input on, the load sinks current. INPUT ON
  takes two clicks, and ALL OUTPUTS OFF sends `INP OFF`.
- **It never sends `*RST`.** The two manuals disagree about whether the factory state
  has the input on.
- **It never writes slot 0.** Register 0 holds the power-on settings, and losing power
  during Save 0 corrupts the memory and forces a recalibration (Operating Manual p.
  63). Slots 1–6 work, but they're lost at power-off.
- **No Short button.** A short means full-scale current or minimum resistance.
- **Mode and range changes are refused while the input is on.** Either one can step
  the current into whatever is connected.
- **Clearing protection takes two clicks.** `INP:PROT:CLE` switches the input back on
  if it was on when protection tripped.
- **Protection is read from the status register** (Programming Manual pp. 85–86).
  After a shutdown `INP?` still says 1, so the card shows **INPUT SHUT DOWN** and the
  cause (OC, OP, OT, OV, RV, VF), plus **UNREG** when the load can't hold its
  setting.
- **Sense switch.** If the load is sinking current but reads about 0 V, the card asks
  about the rear **LCL/RMT sense switch**. On RMT with no sense leads, voltage
  readback fails in every mode and the input goes off in CV/CR (Operating Manual p.
  43). This happened on this bench.
- **Readback is coarse:** ±0.1% ±150 mV in 67 mV steps, and ±0.12% ±10 mA in 27 mA
  steps. Use a meter for anything finer. `9.9E+37` (over range) shows as OVLD.

Not offered yet: transients, triggers, slew rates, the Port0 output.

Simulator: `sim.py`. It wires to a supply's output and has these presets:
`sense-rmt-no-leads`, `prot-shutdown` and `input-on-1a`. Its CV behaviour is crude.

**Tested:** the plugin standard and the simulator only, **not yet on the real load**.
The commands match those this bench's unit answered in the 2026-09-15 commissioning
sweep (`INP`, `MODE?`, `CURR`, `CURR:PROT`, `MEAS`).
