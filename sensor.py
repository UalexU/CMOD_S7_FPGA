"""
sensor.py -- owns the serial port, turns bytes into magnetic field samples.

Knows nothing about GUIs. Import it, call start(), read from .queue.

Standalone use (test it before any GUI exists):
    python sensor.py --test        # parser self-test, no hardware needed
    python sensor.py --fake        # fake rotating field, no hardware needed
    python sensor.py --list        # show available COM ports
    python sensor.py               # read the board

Expected line format from the firmware (OUTPUT_CSV = 1):
    # CONFIG conv_avg=32 range_mt=100 axes=3 temp=1 rtd_hz=60
    Bx,By,Bz,Bmag,DieC,RtdC     <- header, sent once
    1.23, -4.56, 0.07, 4.72, 25.43, 21.30
    # anything diagnostic        <- ignored

Six fields: three magnetic axes, magnitude, the TMAG5170's own die
temperature, and the MAX31865 RTD temperature.

The '# CONFIG' line reports how the firmware has the sensors set up --
averaging, range, which channels are on. Sent once at startup, so it costs
nothing on the wire; only per-sample output affects throughput.

NOTE: all six fields are required. Firmware built before the RTD was added
emits five, and every line will be rejected -- you get silence, not an
error. Reflash before blaming the port.

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

# PT100 RTD span is far wider than the Hall die's. Kept as its own window so
# a legitimate RTD reading is not rejected by the die sensor's narrow one.
MIN_RTD_C = -250.0
MAX_RTD_C = 900.0

Sample = namedtuple("Sample", "bx by bz mag temp rtd")

# How the firmware has the sensors set up. Sent once at startup.
#   conv_avg  averaging multiplier, 1..32
#   range_mt  magnetic full-scale in mT
#   axes      magnetic channels enabled
#   temp      1 if the die temperature channel is on
#   rtd_hz    MAX31865 auto-conversion rate
# Only conv_avg is required; the rest default to None so a line from
# older firmware still parses.
Config = namedtuple("Config", "conv_avg range_mt axes temp rtd_hz",
                    defaults=(None, None, None, None))


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
    if len(parts) != 6:
        return None

    try:
        bx, by, bz, mag, temp, rtd = (float(p) for p in parts)
    except ValueError:
        return None                    # header line lands here

    if any(abs(v) > MAX_COMPONENT for v in (bx, by, bz)):
        return None
    if not 0.0 <= mag <= MAX_MAGNITUDE:
        return None
    if not MIN_TEMP_C <= temp <= MAX_TEMP_C:
        return None
    if not MIN_RTD_C <= rtd <= MAX_RTD_C:
        return None

    return Sample(bx, by, bz, mag, temp, rtd)


def parse_config(raw):
    """b'# CONFIG conv_avg=32 range_mt=100 axes=3 temp=1 rtd_hz=60' -> Config

    Returns None for any other line. Kept separate from parse_line so the
    sample parser stays a pure six-numbers-or-nothing function; both are
    tried on every incoming line.
    """
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("ascii")
        except UnicodeDecodeError:
            return None

    text = raw.strip()
    if not text.startswith("# CONFIG"):
        return None

    fields = {}
    for token in text[len("# CONFIG"):].split():
        key, _, value = token.partition("=")
        if key and value:
            fields[key] = value

    try:
        return Config(
            conv_avg=int(fields["conv_avg"]),
            range_mt=int(fields["range_mt"]) if "range_mt" in fields else None,
            axes=int(fields["axes"]) if "axes" in fields else None,
            temp=int(fields["temp"]) if "temp" in fields else None,
            rtd_hz=int(fields["rtd_hz"]) if "rtd_hz" in fields else None,
        )
    except (KeyError, ValueError):
        return None


def self_test():
    """Everything the parser must survive. No board required."""
    cases = [
        (b"1.23, -4.56, 0.07, 4.72, 25.43, 21.30\r\n",
         Sample(1.23, -4.56, 0.07, 4.72, 25.43, 21.30)),
        (b"0.00, 0.00, 0.00, 0.00, 25.00, 25.00\r\n",
         Sample(0.0, 0.0, 0.0, 0.0, 25.0, 25.0)),
        (b"-0.50, 12.00, -3.25, 12.43, -12.75, -196.00\r\n",
         Sample(-0.5, 12.0, -3.25, 12.43, -12.75, -196.0)),
        (b"1.23,-4.56,0.07,4.72,25.43,21.30\r\n",
         Sample(1.23, -4.56, 0.07, 4.72, 25.43, 21.30)),
        (b"0.00, 0.00, 0.00, 0.00, 165.00, 640.00\r\n",   # both hot, legal
         Sample(0.0, 0.0, 0.0, 0.0, 165.0, 640.0)),
        (b"Bx,By,Bz,Bmag,DieC,RtdC\r\n",  None),   # header
        (b"# SPI transfer failed (2)\r\n", None),  # diagnostic
        (b"",                             None),   # read timeout
        (b"\r\n",                         None),   # blank
        (b"1.23, -4.56\r\n",              None),   # truncated line
        (b"1.23, -4.56, 0.07\r\n",        None),   # wrong field count
        (b"1.23, -4.56, 0.07, 4.72, 25.43\r\n", None),  # old 5-field firmware
        (b"999.0, 0.0, 0.0, 999.0, 25.0, 21.3\r\n", None),  # impossible field
        (b"0.0, 0.0, 0.0, -5.0, 25.0, 21.3\r\n", None),  # negative magnitude
        (b"0.0, 0.0, 0.0, 0.0, 900.0, 21.3\r\n", None),  # impossible die temp
        (b"0.0, 0.0, 0.0, 0.0, 25.0, 5000.0\r\n", None),  # impossible RTD
        (b"\xff\xfe\r\n",                 None),   # line noise
        (b"# CONFIG conv_avg=32 range_mt=100 axes=3 temp=1 rtd_hz=60\r\n",
         None),                                   # config line is not a sample
    ]
    for raw, expected in cases:
        got = parse_line(raw)
        assert got == expected, f"parse_line({raw!r}) -> {got}, expected {expected}"

    config_cases = [
        (b"# CONFIG conv_avg=32 range_mt=100 axes=3 temp=1 rtd_hz=60\r\n",
         Config(32, 100, 3, 1, 60)),
        (b"# CONFIG conv_avg=4 range_mt=25 axes=3 temp=1 rtd_hz=50\r\n",
         Config(4, 25, 3, 1, 50)),
        (b"# CONFIG conv_avg=8\r\n",
         Config(8)),                              # older firmware, no extras
        (b"# rtd FAULT -- read register 07h\r\n", None),  # other diagnostic
        (b"# CONFIG range_mt=100\r\n",           None),  # no conv_avg
        (b"1.23, -4.56, 0.07, 4.72, 25.43, 21.30\r\n", None),  # a sample
    ]
    for raw, expected in config_cases:
        got = parse_config(raw)
        assert got == expected, f"parse_config({raw!r}) -> {got}, expected {expected}"

    # conversion_rate_hz against the datasheet-derived figures
    assert round(conversion_rate_hz(32, 3, True)) == 408, conversion_rate_hz(32)
    assert round(conversion_rate_hz(4, 3, True)) == 2857, conversion_rate_hz(4)

    print(f"parser OK ({len(cases)} sample cases, "
          f"{len(config_cases)} config cases)")


# ------------------------------------------------------------------- ports

def format_rate(hz):
    """Sampling rates span four decades here, so no single unit reads well.

    Samples per second and hertz are the same quantity; ksps is simply the
    convention the datasheet uses above 1000, so match it.
    """
    if hz is None:
        return "--"
    if hz >= 10000:
        return f"{hz / 1000:.0f} ksps"
    if hz >= 1000:
        return f"{hz / 1000:.2f} ksps"
    if hz >= 10:
        return f"{hz:.0f} Hz"
    return f"{hz:.1f} Hz"


def conversion_rate_hz(conv_avg, axes=3, temp=True):
    """How often the TMAG5170 finishes a set of measurements.

    One ADC pipeline slot is 25 us. Each axis is sampled conv_avg times;
    temperature converts once per set (T_RATE=1); one extra slot fills the
    pipeline. Derived here rather than sent over the wire -- it is
    arithmetic the host can do just as well as the firmware.
    """
    if not conv_avg:
        return None
    slots = axes * conv_avg + (1 if temp else 0) + 1
    return 1_000_000 / (slots * 25)


def describe_serial_error(exc, port):
    """Turn a pyserial exception into something a person can act on.

    Matches on the message text as well as the exception class, because
    pyserial raises a plain SerialException for most OS-level failures --
    the useful detail lives only in the string.
    """
    text = str(exc)
    low = text.lower()

    if isinstance(exc, PermissionError) or "access is denied" in low \
            or "permission denied" in low or "resource busy" in low:
        return (f"{port} is already open in another program.\n\n"
                "Usually PuTTY, a Vitis or Vivado serial terminal, or the "
                "Arduino IDE. Close it, then press Start again.")

    if isinstance(exc, FileNotFoundError) or "could not open port" in low \
            or "no such file" in low or "cannot find the file" in low:
        return (f"{port} does not exist.\n\n"
                "The board may be unplugged, or Windows may have moved it to "
                "a different COM number. Press Refresh Ports and pick again.")

    if "device reports readiness" in low:
        return (f"{port} opened but is not responding properly.\n\n"
                "This usually means the port vanished mid-open. Unplug the "
                "board, plug it back in, then Refresh Ports.")

    return f"Could not open {port}.\n\n{text}"


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
        self.error = None            # fatal: the reader thread has stopped
        self.config = None           # Config, once the firmware reports it

        # Diagnostics for the GUI. Written only by the reader thread, read
        # only by the GUI thread, never read-modify-written across the two,
        # so no lock is needed -- same reasoning as self.error.
        self.lines_seen = 0          # any non-empty line off the wire
        self.samples_seen = 0        # lines that parsed into a Sample
        self.last_unparsed = None    # most recent line that was not a Sample
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

    def diagnose_no_data(self):
        """Why no samples have arrived. Called by the GUI after a grace
        period; returns None when data is flowing normally."""
        if self.samples_seen:
            return None

        if self.lines_seen == 0:
            return ("Connected, but nothing is arriving.\n\n"
                    "Check that the baud rate matches the firmware, that the "
                    "board is programmed and running, and that it is not "
                    "sitting at a breakpoint in Vitis.")

        if self.last_unparsed is not None:
            preview = self.last_unparsed.strip()[:60]
            return ("Data is arriving but no line is a valid reading.\n\n"
                    "Usually a firmware/parser mismatch -- six comma-separated "
                    "fields are expected (Bx,By,Bz,Bmag,DieC,RtdC).\n\n"
                    f"Last line received:\n{preview!r}")

        return ("Only diagnostic ('#') lines are arriving, no readings.\n\n"
                "The firmware may be stuck before its main loop, or its "
                "sensor configuration failed. Check the '#' output on a "
                "serial terminal.")

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
        except (serial.SerialException, OSError) as e:
            self.error = describe_serial_error(e, port)
            return

        try:
            with ser:
                time.sleep(0.2)
                ser.reset_input_buffer()   # drop partial line and boot noise

                while not self._stop.is_set():
                    raw = ser.readline()
                    if not raw:
                        continue           # read timeout, nothing arrived

                    self.lines_seen += 1

                    config = parse_config(raw)
                    if config is not None:
                        self.config = config
                        continue

                    sample = parse_line(raw)
                    if sample is not None:
                        self.samples_seen += 1
                        self.queue.put(sample)
                    elif not raw.strip().startswith(b"#"):
                        # '#' lines are firmware diagnostics and expected.
                        # Anything else failing to parse is worth surfacing.
                        self.last_unparsed = raw
        except (serial.SerialException, OSError) as e:
            # Board unplugged, or the driver dropped the handle. Without
            # this the thread dies silently and the plot just stops with no
            # explanation anywhere.
            self.error = (f"Lost connection to {port}.\n\n"
                          f"The board was probably unplugged or reset.\n\n{e}")

    def _run_fake(self):
        """A magnet rotating in the XY plane, plus a slow thermal drift, so
        the GUI can be built and demoed with no board attached."""
        # Mirrors what the firmware reports at CONV_AVG=32x, SCK 625 kHz,
        # 115200 baud, 5 Hz loop.
        self.lines_seen = 1
        self.samples_seen = 1
        self.config = Config(conv_avg=32, range_mt=100, axes=3, temp=1,
                             rtd_hz=60)
        t = 0.0
        period = 0.2
        while not self._stop.is_set():
            bx = round(20.0 * math.cos(t), 2)
            by = round(20.0 * math.sin(t), 2)
            bz = round(5.0 + 2.0 * math.sin(t * 0.3), 2)
            mag = round(math.sqrt(bx * bx + by * by + bz * bz), 2)
            # drifts slowly around room temperature, unlike the field
            temp = round(25.0 + 1.5 * math.sin(t * 0.05), 2)
            # RTD drifts on its own slower schedule, offset from the die
            # sensor -- two independent probes should not move in lockstep.
            rtd = round(21.0 + 3.0 * math.sin(t * 0.02 + 1.0), 2)
            self.queue.put(Sample(bx, by, bz, mag, temp, rtd))
            t += 0.2
            time.sleep(period)


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
            if reader.config and not reported:
                cfg = reader.config
                conv = conversion_rate_hz(cfg.conv_avg, cfg.axes or 3,
                                          bool(cfg.temp))
                print(f"Sensor config: {cfg.conv_avg}x averaging, "
                      f"+/-{cfg.range_mt} mT, conversion "
                      f"{format_rate(conv)}, rtd {format_rate(cfg.rtd_hz)}")
                reported = True
            for s in reader.drain():
                print(f"Bx {s.bx:8.2f}  By {s.by:8.2f}  Bz {s.bz:8.2f}  "
                      f"|B| {s.mag:8.2f} mT   die {s.temp:7.2f} C   "
                      f"rtd {s.rtd:7.2f} C")
    except KeyboardInterrupt:
        pass
    finally:
        reader.stop()


if __name__ == "__main__":
    main()