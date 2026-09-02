"""
sensor.py -- owns the serial port, turns bytes into magnetic field samples.

Knows nothing about GUIs. Import it, call start(), read from .queue.

Standalone use (test it before any GUI exists):
    python sensor.py --test        # parser self-test, no hardware needed
    python sensor.py --fake        # fake rotating field, no hardware needed
    python sensor.py --list        # show available COM ports
    python sensor.py               # read the board

Expected line format from the TMAG5170 firmware (OUTPUT_CSV = 1):
    # LIMITS sensor=408 spi=4882 uart=274 loop=5 bottleneck=loop rate=5
    Bx,By,Bz,TempC         <- header, sent once
    1.23, -4.56, 0.07, 4.72, 25.43
    # anything diagnostic   <- ignored

The '# LIMITS' line is the firmware's own throughput budget: the ceiling
each stage imposes, and which one is currently slowest.

NOTE: five fields are required. Firmware built before the temperature
channel was added emits four, and every line will be rejected -- you get
silence, not an error. Reflash before blaming the port.

Requires: pip install pyserial
"""

import argparse
import math
import queue
import sys
import threading
import time
from collections import namedtuple

BAUD = 115200          # must match the AXI Uartlite setting in Vivado

# Plausibility window. Must match RANGE_CODE in the firmware, or valid
# readings get silently discarded:
#   RANGE_CODE 0x1 -> +/-25 mT      0x0 -> +/-50 mT      0x2 -> +/-100 mT
FULL_SCALE_MT = 100.0

MAX_COMPONENT = FULL_SCALE_MT * 1.2          # headroom for rounding
MAX_MAGNITUDE = MAX_COMPONENT * 1.8          # a little over sqrt(3)

# Datasheet temperature sensing range is -40 to +170 degC. Widened slightly
# so a reading right at the edge is not thrown away as garbage.
MIN_TEMP_C = -50.0
MAX_TEMP_C = 180.0

Sample = namedtuple("Sample", "bx by bz mag temp")

# Firmware throughput budget, all rates in Hz.
Limits = namedtuple("Limits", "sensor spi uart loop bottleneck rate")


# ------------------------------------------------------------------ parsing

def parse_line(raw):
    """b'1.23, -4.56, 0.07, 4.72, 25.43\\r\\n'
           -> Sample(1.23, -4.56, 0.07, 4.72, 25.43)

    Returns None for anything that isn't a reading: blank lines, the CSV
    header, '#' diagnostics, half-received lines, out-of-range garbage.
    Pure function -- no serial, no state. This is the part worth testing.
    """
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("ascii")
        except UnicodeDecodeError:
            return None

    text = raw.strip()
    if not text or text.startswith("#"):
        return None

    parts = text.split(",")
    if len(parts) != 5:
        return None

    try:
        bx, by, bz, mag, temp = (float(p) for p in parts)
    except ValueError:
        return None                    # header line lands here

    if any(abs(v) > MAX_COMPONENT for v in (bx, by, bz)):
        return None
    if not 0.0 <= mag <= MAX_MAGNITUDE:
        return None
    if not MIN_TEMP_C <= temp <= MAX_TEMP_C:
        return None

    return Sample(bx, by, bz, mag, temp)


def parse_limits(raw):
    """b'# LIMITS sensor=408 spi=4882 ... bottleneck=loop rate=5' -> Limits

    Returns None for any other line. Kept separate from parse_line so the
    sample parser stays a pure four-numbers-or-nothing function; both are
    tried on every incoming line.
    """
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("ascii")
        except UnicodeDecodeError:
            return None

    text = raw.strip()
    if not text.startswith("# LIMITS"):
        return None

    fields = {}
    for token in text[len("# LIMITS"):].split():
        key, _, value = token.partition("=")
        if key and value:
            fields[key] = value

    try:
        return Limits(
            sensor=int(fields["sensor"]),
            spi=int(fields["spi"]),
            uart=int(fields["uart"]),
            loop=int(fields["loop"]),
            bottleneck=fields["bottleneck"],
            rate=int(fields["rate"]),
        )
    except (KeyError, ValueError):
        return None


def self_test():
    """Everything the parser must survive. No board required."""
    cases = [
        (b"1.23, -4.56, 0.07, 4.72, 25.43\r\n",
         Sample(1.23, -4.56, 0.07, 4.72, 25.43)),
        (b"0.00, 0.00, 0.00, 0.00, 25.00\r\n",
         Sample(0.0, 0.0, 0.0, 0.0, 25.0)),
        (b"-0.50, 12.00, -3.25, 12.43, -12.75\r\n",
         Sample(-0.5, 12.0, -3.25, 12.43, -12.75)),
        (b"1.23,-4.56,0.07,4.72,25.43\r\n",
         Sample(1.23, -4.56, 0.07, 4.72, 25.43)),
        (b"0.00, 0.00, 0.00, 0.00, 165.00\r\n",     # hot but legal
         Sample(0.0, 0.0, 0.0, 0.0, 165.0)),
        (b"Bx,By,Bz,Bmag,TempC\r\n",      None),   # header
        (b"# SPI transfer failed (2)\r\n", None),  # diagnostic
        (b"",                             None),   # read timeout
        (b"\r\n",                         None),   # blank
        (b"1.23, -4.56\r\n",              None),   # truncated line
        (b"1.23, -4.56, 0.07\r\n",        None),   # wrong field count
        (b"1.23, -4.56, 0.07, 4.72\r\n",  None),   # old 4-field firmware
        (b"999.0, 0.0, 0.0, 999.0, 25.0\r\n", None),  # impossible field
        (b"0.0, 0.0, 0.0, -5.0, 25.0\r\n", None),  # negative magnitude
        (b"0.0, 0.0, 0.0, 0.0, 900.0\r\n", None),  # impossible temperature
        (b"\xff\xfe\r\n",                 None),   # line noise
        (b"# LIMITS sensor=408 spi=4882 uart=274 loop=5 bottleneck=loop rate=5\r\n",
         None),                                   # budget line is not a sample
    ]
    for raw, expected in cases:
        got = parse_line(raw)
        assert got == expected, f"parse_line({raw!r}) -> {got}, expected {expected}"

    limit_cases = [
        (b"# LIMITS sensor=408 spi=4882 uart=274 loop=5 bottleneck=loop rate=5\r\n",
         Limits(408, 4882, 274, 5, "loop", 5)),
        (b"# LIMITS sensor=408 spi=4882 uart=274 loop=100 bottleneck=uart rate=274\r\n",
         Limits(408, 4882, 274, 100, "uart", 274)),
        (b"# SPI transfer failed (2)\r\n",       None),   # other diagnostic
        (b"# LIMITS sensor=408\r\n",             None),   # incomplete
        (b"1.23, -4.56, 0.07, 4.72, 25.43\r\n",  None),   # a sample, not limits
    ]
    for raw, expected in limit_cases:
        got = parse_limits(raw)
        assert got == expected, f"parse_limits({raw!r}) -> {got}, expected {expected}"

    print(f"parser OK ({len(cases)} sample cases, {len(limit_cases)} limits cases)")


# ------------------------------------------------------------------- ports

def list_ports():
    from serial.tools import list_ports as lp
    return list(lp.comports())


def find_port():
    """Best guess at the board's COM port."""
    ports = list_ports()
    if len(ports) == 1:
        return ports[0].device
    for p in ports:
        blurb = f"{p.description} {p.manufacturer}"
        if any(k in blurb for k in ("USB Serial", "FT2232", "FT232", "Future Technology")):
            return p.device
    return None


# ------------------------------------------------------------------ reader

class SensorReader:
    """Background thread: serial in, Samples out through .queue.

    Blocks freely -- it is not the GUI thread, so nothing freezes.
    Never touches a widget. The queue is the only hand-off point.
    """

    def __init__(self, port=None, baud=BAUD, fake=False):
        self.port = port
        self.baud = baud
        self.fake = fake
        self.queue = queue.Queue()
        self.error = None            # set if the port could not be opened
        self.limits = None           # Limits, once the firmware reports them
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        target = self._run_fake if self.fake else self._run_serial
        self._thread = threading.Thread(target=target, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def drain(self):
        """Everything received since the last call. Never blocks."""
        out = []
        while not self.queue.empty():
            out.append(self.queue.get())
        return out

    # -- workers ---------------------------------------------------------

    def _run_serial(self):
        import serial

        port = self.port or find_port()
        if port is None:
            self.error = "No COM port found. Use --list to see what's available."
            return

        try:
            ser = serial.Serial(port, self.baud, timeout=1)
        except serial.SerialException as e:
            self.error = f"Could not open {port}: {e}\nIs another terminal still holding it?"
            return

        with ser:
            time.sleep(0.2)
            ser.reset_input_buffer()      # discard partial line and boot noise

            while not self._stop.is_set():
                raw = ser.readline()

                limits = parse_limits(raw)
                if limits is not None:
                    self.limits = limits
                    continue

                sample = parse_line(raw)
                if sample is not None:
                    self.queue.put(sample)

    def _run_fake(self):
        """A magnet rotating in the XY plane, plus a slow thermal drift, so
        the GUI can be built and demoed with no board attached."""
        # Mirrors what the firmware reports at CONV_AVG=32x, SCK 625 kHz,
        # 115200 baud, 5 Hz loop.
        self.limits = Limits(sensor=408, spi=4901, uart=274,
                             loop=5, bottleneck="loop", rate=5)
        t = 0.0
        while not self._stop.is_set():
            bx = round(20.0 * math.cos(t), 2)
            by = round(20.0 * math.sin(t), 2)
            bz = round(5.0 + 2.0 * math.sin(t * 0.3), 2)
            mag = round(math.sqrt(bx * bx + by * by + bz * bz), 2)
            # drifts slowly around room temperature, unlike the field
            temp = round(25.0 + 1.5 * math.sin(t * 0.05), 2)
            self.queue.put(Sample(bx, by, bz, mag, temp))
            t += 0.2
            time.sleep(0.2)


# -------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="TMAG5170 serial reader")
    ap.add_argument("--test", action="store_true", help="run the parser self-test and exit")
    ap.add_argument("--list", action="store_true", help="list COM ports and exit")
    ap.add_argument("--fake", action="store_true", help="generate fake data, no hardware")
    ap.add_argument("--port", help="COM port (default: autodetect)")
    ap.add_argument("--baud", type=int, default=BAUD, help=f"baud rate (default {BAUD})")
    args = ap.parse_args()

    if args.test:
        self_test()
        return

    if args.list:
        ports = list_ports()
        if not ports:
            print("No serial ports found.")
        for p in ports:
            print(f"  {p.device:8}  {p.description}")
        return

    reader = SensorReader(port=args.port, baud=args.baud, fake=args.fake).start()
    print("Reading. Ctrl-C to stop.")
    reported = False

    try:
        while True:
            time.sleep(0.25)
            if reader.error:
                sys.exit(reader.error)
            if reader.limits and not reported:
                lim = reader.limits
                print(f"Firmware budget: sensor {lim.sensor} Hz, spi {lim.spi} Hz, "
                      f"uart {lim.uart} Hz, loop {lim.loop} Hz "
                      f"-> bottleneck {lim.bottleneck} at {lim.rate} Hz")
                reported = True
            for s in reader.drain():
                print(f"Bx {s.bx:8.2f}  By {s.by:8.2f}  Bz {s.bz:8.2f}  "
                      f"|B| {s.mag:8.2f} mT   T {s.temp:7.2f} C")
    except KeyboardInterrupt:
        pass
    finally:
        reader.stop()


if __name__ == "__main__":
    main()