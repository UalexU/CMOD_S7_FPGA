# TMAG5170 + MAX31865 instrument GUI

PySide6 + pyqtgraph front end for the Cmod S7-25 Hall/RTD board, with a
two-way UART link to the MicroBlaze firmware.

```
main.c          MicroBlaze firmware — sampling loop + UART command parser
sensor.py       serial link, parsing, command channel, throughput ceilings
theme.py        palette and stylesheet
tmag_scope.py   the application
```

```bash
pip install PySide6 pyqtgraph numpy pandas openpyxl pyserial
python tmag_scope.py            # pick a port in the UI
python tmag_scope.py --fake     # synthetic source, no board needed
python sensor.py --test         # parser + ceiling self-test
```

## What the four views are for

| View | Reads |
|---|---|
| **Strip chart** | field and temperature against time, in two frames sharing one x-axis. Crosshair reads every channel at the cursor. |
| **XY vector** | Bx against By. A rotating magnet draws a circle; the least-squares fit reports radius, centre and residual — centre is your per-axis offset, a large residual means unequal axis gain. |
| **Spectrum** | Welch-averaged amplitude spectrum. Mechanical vibration shows here long before it is visible in the strip chart. |
| **Distribution** | per-channel histogram with σ. Point the sensor at nothing, let it sit, and σ is the noise floor at the current averaging. |

Also: tare (zeroes the field, recomputes |B| from the tared components rather
than shifting |B| itself), moving-average smoothing, pause that freezes the
display without interrupting acquisition, CSV recording that streams to disk as
samples arrive, and Excel export with a `run` sheet carrying the board config
and tare offsets. **Smoothing and tare are display-only — recording and export
always write raw samples.**

## Firmware command channel

The GUI sends one line per command; the firmware polls the Uartlite RX FIFO
between samples, so nothing ever blocks waiting for input. Every accepted
command re-sends `# CONFIG`, so the panel shows what the board is doing rather
than what it was asked to do.

```
R <hz>    main-loop rate, 1..5000
A <mult>  TMAG averaging: 1 2 4 8 16 32
G <mt>    range: 25 50 100  (code and scale factor change together)
P <0|1>   pause / resume streaming
Z         re-send CONFIG
```

`# CONFIG` gained `period_us` and `sck_khz`, both needed for the throughput
panel. Older firmware still parses — the extra fields default to `None`.

If your Uartlite block is not `axi_uartlite_0`, change `UART_BASE` in `main.c`.

## The rate question, honestly

The sensor converts at ~408 Hz at 32× averaging. The stock firmware slept
200 ms per loop, so you were seeing **5 Hz — about 1.2% of finished
conversions**. Everything else was being made and overwritten.

Raising it hits the next wall quickly:

| Ceiling | At stock settings | Set by |
|---|---|---|
| sensor | 408 Hz | `CONV_AVG` — 4× averaging gives 2.9 ksps |
| SPI | 3.1 ksps | SCK, 625 kHz in Vivado |
| **UART** | **303 Hz** | 115200 baud ÷ ~38 bytes per CSV line |
| loop | 5 Hz → now yours | the Sample rate control |

So `R 250` is free, and anything above ~300 Hz needs one of:

- **higher baud** — but Uartlite baud is a *synthesis* parameter, so 921600
  means editing the IP and rebuilding the bitstream. That buys ~2.4 ksps.
- **shorter lines** — binary framing (6 × int16 + a sync byte ≈ 14 bytes vs 38)
  roughly triples the ceiling with no hardware change.

The Throughput dock shows all four ceilings live and highlights the binding
one, and the Sample rate control warns before you ask for a rate the link
cannot carry. That is what the unfinished `lim` block in the old `GUI.py` was
reaching for.

**Set `DEBUG_MODE 0` before raising the rate.** Each sample's debug output is
several hundred characters; at 115200 that takes longer than the sample period
and the loop runs at the speed of the debug text.

## Firmware fixes carried in

- `RTD_WIRE_MODE` was defined, documented, and then ignored — `RTD_CONFIG`
  hard-coded `RTD_3WIRE`, so changing it did nothing. It is now used. **This
  changes behaviour: if your probe is 3-wire, set `RTD_WIRE_MODE` to
  `RTD_3WIRE`.**
- `RANGE_CODE` / `RANGE_MT_X100` are now a runtime pair changed only in one
  place, so they cannot drift apart.
- The empty `uart_config_info()` stub is gone.

## Host-side bug that was live

`update_throughput()` in the old `GUI.py` referenced `lim`, which was never
defined. Once a `# CONFIG` line arrived it raised `NameError` on every poll —
the `finally` re-arm kept the GUI alive, so it showed as a red status line and
a throughput panel that never updated. That path is now implemented rather
than removed: `sensor.delivery_limits()`.
