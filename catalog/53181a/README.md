# 53181A plugin — HP/Agilent 53181A 225 MHz frequency counter

Manual: **HP 53181A Programming Guide, 53181-90002** (serial prefix 3711 and above).
Every command in `driver.py` came from it. **Don't use the 53131A/132A Programming
Guide**: it's a different counter, even though the two are often filed together.

## Why this driver never hangs

A counter only finishes a measurement when its input has a signal. With no signal:
- `:READ?` / `:MEASure?` wait for a gate that never closes;
- `:FETCh?` sent during a measurement "holds off further commands … until the
  measurement completes", and only a device clear or power-on cancels that (guide
  p. 4-55).

So the driver never asks for a result it hasn't been told exists. It starts one
measurement with `:INIT` + `*OPC`, then each poll reads `*ESR?`, which never
blocks. Bit 0 (Operation Complete) sets when the measurement ends (p. 3-26). Only
then does it send `:FETCh?`. If the measurement is still pending after the gate time
+ 1 s, it sends `:ABORt` (p. 4-4) and shows **NO SIGNAL** until a reading arrives.
Other instruments on the same GPIB bus keep working throughout.

While ON in the panel the counter is in single-measurement mode (`:INIT:CONT OFF`).
At power-off the driver puts back RUN mode if that's how it found the counter.

## The card

- Frequency on channel 1 or 2, period on channel 1 or 2, ratio 1/2 or 2/1. The reading
  shows exactly the digits the counter sent.
- Gate time: 0.01 / 0.1 / 1 / 10 s, or any value from 1 ms to 1000 s. Each ×10 of gate
  is one more digit.
- Channel 1 input: 50 Ω / 1 MΩ, AC / DC, ×1 / ×10, 100 kHz low-pass filter.
  Channel 2 (Option 030) is fixed 50 Ω AC and query-only, so it's shown, not set.
- The reference in use (INT / EXT, and whether it's auto-selected).
- Running min / max / avg; saved setups 1–20 (`*SAV` / `*RCL`, stopping the
  measurement first as the guide's key map shows).

**Not offered:** Voltage Peaks (MAX / MIN / PTP). The 53131A guide documents
`:FETCh?` hanging in that function. The 53181A guide doesn't mention it, so it's
left out as a precaution.

## Quirks

- There is **no channel 3**: `FREQ 3` is error −224.
- `:FUNC?` answers `"FREQ"` for channel 1 (no suffix) and `"FREQ 2"` for channel 2.
- With `:INIT:CONT ON` (RUN) the Operation Status *Measuring* bit read clear on this
  unit. The guide doesn't say whether it's set while waiting for a missing signal,
  which is why the driver uses the pending-operation flag instead.

## Tested

2026-09-23, GPIB through an ICS 8065, with an HP 8648A as the source:

| Input | Set | Gate | Read | Error |
|---|---|---|---|---|
| Ch2 | 100 MHz, −20 dBm | 1 s | 99.99999806 MHz | −0.0194 ppm |
| Ch2 | 500 MHz | 1 s | 499.99999029 MHz | −0.0194 ppm |
| Ch2 | 1000 MHz | 1 s | 999.9999806 MHz | −0.0194 ppm |
| Ch1 (50 Ω) | 10 MHz, −10 dBm | 0.1 / 1 / 10 s | 9.99999981 / 9.999999807 / 9.9999998064 MHz | −0.0190 / −0.0193 / −0.0194 ppm |

The constant ppm across 100× in frequency, on both inputs, is one timebase offset
between the two instruments (the counter was on its internal reference). RF off
gave NO SIGNAL with no bus hang; RF on recovered without intervention.
