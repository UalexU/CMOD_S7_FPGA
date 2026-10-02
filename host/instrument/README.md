# instrument/ — settings, limits and settings.json

`host/settings.json` is the single source of truth for every setting. The GUI
controls, someone editing the file by hand, and the assistant all go through
`SettingsController.request()`, so the same rules apply to all three:

```
validate against the hardware  ->  commit  ->  save settings.json
                                            ├─ display: update the widgets
                                            └─ board: A / G / R / P, one at a time,
                                               each waiting for # ACK or # ERR
```

| File | What it is |
|---|---|
| `spec.py` | hardware and firmware limits, with their datasheet sources |
| `settings.py` | schema, validation, presets, reading and writing `settings.json` (no Qt) |
| `controller.py` | ties the store to the widgets, the file watcher and the board |
| `selftest.py` | `python -m instrument.selftest` — checks the limits, no board needed |

## What can be set

| Setting | Possible values | Why |
|---|---|---|
| `averaging` | 1, 2, 4, 8, 16, 32 | TMAG5170 CONV_AVG codes 0h–5h. The sensor makes a new X+Y+Z+T reading at about 8000 / 5000 / 2857 / 1538 / 800 / 408 Hz |
| `sample_rate_hz` | 1 Hz … ~303 Hz, or `"max"` | The slowest of the sensor (above), the UART (115200 baud ≈ 303 lines/s for a 38-byte line) and the firmware loop |
| `range_mT` | 25, 50, 100 | TMAG5170A1 ranges. A range the field present now would clip is refused |
| `streaming` | true / false | `P 1` / `P 0` |
| `window_s`, `fps`, `smoothing`, `autoscale`, `visible_channels`, `view`, `spectrum_segments`, `theme` | see `settings.SCHEMA` | display only |

**Sample rate is in steps.** The firmware takes `R` as a whole number of Hz
and sleeps that period *after* reading and printing (~2.4 ms of work and
blocked printing per sample). The delivered rate is therefore
`1 / (1/R + overhead)`, and only some rates exist: 10 Hz, for example, falls
between 9.8 Hz (R 10) and 10.7 Hz (R 11).

- A control in the GUI, or a hand edit of the file, snaps to the nearest rate
  that exists.
- The assistant is told both neighbours and has to pick one.

The overhead starts as an estimate and is re-fitted from the measured rate
while running.

**Fixed, not settable:**

- UART baud (set in the Vivado Uartlite)
- RTD 60 Hz notch: a new RTD value every 16.7 ms, so above 60 Hz the RTD
  repeats values
- SPI clock (625 kHz)
- Two-decimal printing: the visible field step is 10 µT even though the ADC
  step at ±25 mT is 0.76 µT

## Board settings and connecting

When the board connects, the GUI adopts what the board reports in its
`# CONFIG` line. The exception is board settings changed while it was
offline: those are sent instead.

## Presets

Built-in presets: `noise floor`, `balanced`, `fast`, `slow logging`. Saved
presets live in `settings.json` under `"presets"`.

## Sources

- TI TMAG5170 datasheet SBASAF4: Table 7-2, Sec 6.5–6.6, Table 7-1
- Analog Devices MAX31865 datasheet Rev 3
- `fw/SPI_BOTH/src/main.c`
