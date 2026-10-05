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
| `controller.py` | ties the store to the widgets, the file watcher and the board; runs the noise meter |
| `processing.py` | temperature compensation, outlier rejection, filters, noise report |
| `panels.py` | the Processing and Noise measurement panels in Controls |
| `fieldmap.py` | the Field map tab: manual positioning, capture, map, homogeneity |
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

## Processing (host side)

Processing changes what the views, the statistics, the noise meter and the
field map see. It never touches the board or the recorded/exported raw data.

**Temperature compensation.** Neither sensor does this as configured:

- **TMAG5170:** `main.c` writes DEVICE_CONFIG with MAG_TEMPCO = 00b
  (0 %/°C), so the sensor applies no magnet compensation. Its own Hall
  sensitivity drift is specified at up to ±2.8 % (25→125 °C), and offset
  drift at up to ±5 µT/°C on X/Y. The chip's MAG_TEMPCO option would use the
  *die* temperature and assumes the magnet is at the same temperature, which
  is rarely true for a probe in a bore.
- **MAX31865:** it has nothing to switch on. Its only related setting is
  3-wire lead compensation (`RTD_WIRE_MODE` in `main.c`, currently 2/4-wire),
  which is a wiring and firmware choice.

The GUI option normalises the field to `temp_ref_C`:

```
B_comp = B / (1 + a·(T − T_ref))     a: NdFeB −0.12, SmCo −0.03, ferrite −0.20 %/°C
```

`T` comes from the RTD (mount it on the magnet) or the TMAG die.

**Outliers:** `hampel` replaces points more than k·MAD from the local median;
`sigma_clip` replaces points far from the window median.

**Filters:** `moving_average`, `median`, `ema`, `lowpass` (zero-phase
Butterworth) and `notch`. Anything a filter cannot do at the current sample
rate is refused with the reason, including when you lower the sample rate
later. For example, a 60 Hz notch needs more than 133 Hz sampling; below that,
mains is aliased, not removed.

`median`, `hampel`, `lowpass` and `notch` need **scipy** (`pip install scipy`).
Without it they are greyed out.

## Noise measurement

The Noise measurement panel records N seconds of fresh samples (keep the
sensor still). It reports, per channel:

- σ after removing the linear drift, raw and after processing
- peak-to-peak and drift per minute
- noise density
- the expected σ: the datasheet value at the current averaging, combined
  with the 10 µT print step

## Field map

1. Optionally generate a grid plan: plane XY/XZ/YZ, ranges and steps in mm,
   serpentine order.
2. Move the sensor to the position shown, or type any position.
3. Press **Capture** (or Enter). The GUI waits the settle time (hands off),
   then averages raw samples for the averaging time.

Each point stores the mean and σ of every channel, the temperatures and the
settings used. Points are flagged if σ is more than 5× the expected value (the
sensor was probably moving) or if an axis touched full scale. Capturing again
at the same position replaces the point.

The map shows the chosen component as a colour map, with mean, min, max,
peak-to-peak and **homogeneity in ppm**. It warns if the RTD drifted more than
0.5 °C during the map without temperature compensation. Maps save as JSON
(and export as CSV) in `host/maps/`.

The capture time must give at least 5 samples at the current rate.

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
