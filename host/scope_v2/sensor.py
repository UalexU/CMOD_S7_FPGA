"""
sensor.py -- serial link to the TMAG5170 + MAX31865 board on the Cmod S7-25.

Owns the port, turns bytes into samples, and carries commands the other way.
Knows nothing about GUIs. Import it, call start(), read from .queue.

Standalone use (test it before any GUI exists):
    python sensor.py --test        # parser self-test, no hardware needed
    python sensor.py --fake        # synthetic field, no hardware needed
    python sensor.py --list        # show available COM ports
    python sensor.py               # read the board

Expected line format from the firmware (OUTPUT_CSV = 1):
    # CONFIG conv_avg=32 range_mt=100 axes=3 temp=1 rtd_hz=60 period_us=200000 sck_khz=625
    Bx,By,Bz,Bmag,DieC,RtdC     <- header, sent once
    1.23, -4.56, 0.07, 4.72, 25.43, 21.30
    # anything diagnostic        <- kept in .notes, not plotted

Six fields: three magnetic axes, magnitude, the TMAG5170's own die
temperature, and the MAX31865 RTD temperature.

The '# CONFIG' line reports how the firmware has the sensors set up. It is
re-sent after every accepted command, so the host never has to guess.

NOTE: all six fields are required. Firmware built before the RTD was added
emits five, and every line will be rejected -- you get silence, not an
error. Reflash before blaming the port.

Requires: pip install pyserial
"""

import argparse
import math
import queue
import random
import sys
import threading
import time
from collections import deque, namedtuple

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

# Bytes moved over SPI for one complete sample set: four 32-bit TMAG5170
# frames (X, Y, Z, T) plus the MAX31865's 9-byte block read.
SPI_BYTES_PER_SET = 4 * 4 + 9

# 8N1: every byte on the wire costs ten bit times.
UART_BITS_PER_BYTE = 10

Sample = namedtuple("Sample", "bx by bz mag temp rtd")

# How the firmware has the sensors set up. Re-sent after every command.
#   conv_avg   averaging multiplier, 1..32
#   range_mt   magnetic full-scale in mT
#   axes       magnetic channels enabled
#   temp       1 if the die temperature channel is on
#   rtd_hz     MAX31865 auto-conversion rate
#   period_us  firmware main-loop period -- what actually sets the send rate
#   sck_khz    SPI clock, needed for the SPI ceiling
# Only conv_avg is required; the rest default to None so a line from older
# firmware still parses.
Config = namedtuple("Config", "conv_avg range_mt axes temp rtd_hz period_us sck_khz",
                    defaults=(None, None, None, None, None, None))

# Every ceiling between the sensor die and this process, in Hz, plus which
# one actually binds. This is the panel that tells you whether asking for a
# faster rate can possibly help, and what to change if it cannot.
Limits = namedtuple("Limits", "conv spi uart loop rate bottleneck")


# ------------------------------------------------------------------ parsing

def parse_line(raw):
    """b'1.23, -4.56, 0.07, 4.72, 25.43, 21.30\\r\\n'
           -> Sample(1.23, -4.56, 0.07, 4.72, 25.43, 21.30)

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
    """b'# CONFIG conv_avg=32 range_mt=100 ...' -> Config

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

    def opt(name):
        return int(fields[name]) if name in fields else None

    try:
        return Config(
            conv_avg=int(fields["conv_avg"]),
            range_mt=opt("range_mt"),
            axes=opt("axes"),
            temp=opt("temp"),
            rtd_hz=opt("rtd_hz"),
            period_us=opt("period_us"),
            sck_khz=opt("sck_khz"),
        )
    except (KeyError, ValueError):
        return None


# -------------------------------------------------------------------- rates

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


def delivery_limits(config, baud=BAUD, line_bytes=38.0):
    """Every ceiling between the sensor die and this process.

    conv  the chip finishing a measurement set -- averaging sets this
    spi   moving those registers out at the SPI clock
    uart  pushing one formatted CSV line through the serial port
    loop  the firmware's own main-loop period

    Whichever is smallest is the rate you actually get, and it names the
    thing worth changing. Raising a rate above the binding ceiling does
    nothing; the point of showing all four is that you can see which.

    line_bytes should be the measured mean line length when one is
    available -- the default is a typical six-field line.
    """
    if config is None:
        return None

    conv = conversion_rate_hz(config.conv_avg, config.axes or 3,
                              bool(config.temp))

    sck_hz = (config.sck_khz or 625) * 1000
    spi = sck_hz / (SPI_BYTES_PER_SET * 8)

    uart = (baud / UART_BITS_PER_BYTE) / max(line_bytes, 1.0)

    loop = 1_000_000 / config.period_us if config.period_us else None

    named = [("sensor", conv), ("spi", spi), ("uart", uart), ("loop", loop)]
    live = [(name, hz) for name, hz in named if hz]
    if not live:
        return None
    bottleneck, rate = min(live, key=lambda pair: pair[1])

    return Limits(conv=conv, spi=spi, uart=uart, loop=loop,
                  rate=rate, bottleneck=bottleneck)


def max_line_rate(baud=BAUD, line_bytes=38.0):
    """Lines per second the port can carry. Handy on its own: this is the
    number that says whether a requested rate needs a faster baud, and baud
    on the AXI Uartlite is fixed at synthesis -- not a runtime setting."""
    return (baud / UART_BITS_PER_BYTE) / max(line_bytes, 1.0)


# ------------------------------------------------------------------- errors

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
                "Arduino IDE. Close it, then press Connect again.")

    if isinstance(exc, FileNotFoundError) or "could not open port" in low \
            or "no such file" in low or "cannot find the file" in low:
        return (f"{port} does not exist.\n\n"
                "The board may be unplugged, or Windows may have moved it to "
                "a different COM number. Press Refresh and pick again.")

    if "device reports readiness" in low:
        return (f"{port} opened but is not responding properly.\n\n"
                "This usually means the port vanished mid-open. Unplug the "
                "board, plug it back in, then Refresh.")

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
        if any(k in blurb for k in ("USB Serial", "FT2232", "FT232",
                                    "Future Technology")):
            return p.device
    return None


# ------------------------------------------------------------------ reader

class SensorReader:
    """Background thread: serial in, Samples out through .queue.

    Blocks freely -- it is not the GUI thread, so nothing freezes.
    Never touches a widget. The queue is the only hand-off point.

    Commands travel the other way through send(). Writes happen on the
    caller's thread under a lock; pyserial's read and write paths are
    independent, so a write never disturbs the reader's blocking readline.
    """

    def __init__(self, port=None, baud=BAUD, fake=False, fake_hz=50.0):
        self.port = port
        self.baud = baud
        self.fake = fake
        self.fake_hz = fake_hz
        self.queue = queue.Queue()
        self.error = None            # fatal: the reader thread has stopped
        self.config = None           # Config, once the firmware reports it

        # Diagnostics for the GUI. Written only by the reader thread, read
        # only by the GUI thread, never read-modify-written across the two,
        # so no lock is needed -- same reasoning as self.error.
        self.lines_seen = 0          # any non-empty line off the wire
        self.samples_seen = 0        # lines that parsed into a Sample
        self.bytes_seen = 0          # for the measured mean line length
        self.last_unparsed = None    # most recent line that was not a Sample
        self.notes = deque(maxlen=400)   # '#' lines, for the log pane

        self._serial = None
        self._write_lock = threading.Lock()
        self._recording = None
        self._record_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    # -- lifecycle -------------------------------------------------------

    def start(self):
        target = self._run_fake if self.fake else self._run_serial
        self._thread = threading.Thread(target=target, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self.stop_recording()

    # -- outbound --------------------------------------------------------

    def send(self, command):
        """One command line to the firmware. Returns True if it went out.

        Silently succeeds in fake mode so the UI can be exercised with no
        board attached -- the fake source honours the rate command.
        """
        text = command.strip()
        if not text:
            return False

        if self.fake:
            self._apply_fake_command(text)
            self.notes.append(f"# (simulated) ACK {text}")
            return True

        if self._serial is None:
            return False
        try:
            with self._write_lock:
                self._serial.write((text + "\n").encode("ascii"))
                self._serial.flush()
            return True
        except Exception as e:                       # noqa: BLE001
            self.error = f"Lost the port while sending {text!r}.\n\n{e}"
            return False

    def set_rate_hz(self, hz):
        return self.send(f"R {int(round(hz))}")

    def set_averaging(self, mult):
        return self.send(f"A {int(mult)}")

    def set_range_mt(self, mt):
        return self.send(f"G {int(mt)}")

    def set_streaming(self, on):
        return self.send(f"P {1 if on else 0}")

    def request_config(self):
        return self.send("Z")

    # -- recording -------------------------------------------------------

    def start_recording(self, path):
        """Append every accepted sample to disk as it arrives, so a long run
        is not held hostage by the display buffer or by RAM."""
        self.stop_recording()
        handle = open(path, "w", encoding="ascii", newline="")
        handle.write("t_host_s,Bx_mT,By_mT,Bz_mT,Bmag_mT,Tdie_C,Trtd_C\n")
        with self._record_lock:
            self._recording = (handle, time.perf_counter())
        return path

    def stop_recording(self):
        with self._record_lock:
            entry, self._recording = self._recording, None
        if entry:
            entry[0].close()

    @property
    def recording(self):
        return self._recording is not None

    def _write_record(self, sample):
        with self._record_lock:
            entry = self._recording
            if entry is None:
                return
            handle, t0 = entry
            handle.write(f"{time.perf_counter() - t0:.6f},{sample.bx},"
                         f"{sample.by},{sample.bz},{sample.mag},"
                         f"{sample.temp},{sample.rtd}\n")

    # -- inbound ---------------------------------------------------------

    def drain(self):
        """Everything received since the last call. Never blocks."""
        out = []
        while True:
            try:
                out.append(self.queue.get_nowait())
            except queue.Empty:
                return out

    @property
    def mean_line_bytes(self):
        """Measured mean line length, for the UART ceiling. Falls back to a
        typical six-field line until enough has arrived to mean anything."""
        if self.lines_seen < 10:
            return 38.0
        return self.bytes_seen / self.lines_seen

    def limits(self):
        return delivery_limits(self.config, self.baud, self.mean_line_bytes)

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
                "sensor configuration failed. Check the '#' output in the "
                "log pane.")

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

        self.port = port
        try:
            with ser:
                self._serial = ser
                time.sleep(0.2)
                ser.reset_input_buffer()   # drop partial line and boot noise
                self.send("Z")             # ask for CONFIG without a reset

                while not self._stop.is_set():
                    raw = ser.readline()
                    if not raw:
                        continue           # read timeout, nothing arrived

                    self.lines_seen += 1
                    self.bytes_seen += len(raw)

                    config = parse_config(raw)
                    if config is not None:
                        self.config = config
                        continue

                    sample = parse_line(raw)
                    if sample is not None:
                        self.samples_seen += 1
                        self.queue.put(sample)
                        self._write_record(sample)
                    elif raw.strip().startswith(b"#"):
                        # Firmware diagnostics: expected, and worth keeping
                        # where a person can read them.
                        self.notes.append(raw.decode("ascii", "replace").strip())
                    else:
                        self.last_unparsed = raw
        except (serial.SerialException, OSError) as e:
            # Board unplugged, or the driver dropped the handle. Without
            # this the thread dies silently and the plot just stops with no
            # explanation anywhere.
            self.error = (f"Lost connection to {port}.\n\n"
                          f"The board was probably unplugged or reset.\n\n{e}")
        finally:
            self._serial = None

    # -- fake source -----------------------------------------------------

    def _apply_fake_command(self, text):
        head, _, arg = text.partition(" ")
        cfg = self.config
        if cfg is None:            # command beat the thread to its first config
            return
        try:
            value = int(arg)
        except ValueError:
            return
        if head == "R" and value > 0:
            self.fake_hz = float(value)
            self.config = cfg._replace(period_us=int(1_000_000 / value))
        elif head == "A" and value in (1, 2, 4, 8, 16, 32):
            self.config = cfg._replace(conv_avg=value)
        elif head == "G" and value in (25, 50, 100):
            self.config = cfg._replace(range_mt=value)

    def _run_fake(self):
        """A magnet rotating in the XY plane with a small vibration on top,
        plus sensor noise and a slow thermal drift, so every view in the GUI
        -- spectrum and histogram included -- has something real to show."""
        self.config = Config(conv_avg=32, range_mt=100, axes=3, temp=1,
                             rtd_hz=60, period_us=int(1_000_000 / self.fake_hz),
                             sck_khz=625)
        self.notes.append("# CONFIG (simulated source, no board attached)")
        self.lines_seen = 10
        self.bytes_seen = 380

        rng = random.Random(0xC0FFEE)
        start = time.perf_counter()
        while not self._stop.is_set():
            period = 1.0 / max(self.fake_hz, 0.1)
            # Wall clock, not an accumulated nominal period: the host stamps
            # samples on arrival, so a generator running on its own idea of
            # time puts every synthesized frequency in the wrong bin.
            t = time.perf_counter() - start

            # 0.7 Hz rotation, a 7 Hz mechanical vibration, and noise floor.
            spin = 2 * math.pi * 0.7 * t
            buzz = 0.8 * math.sin(2 * math.pi * 7.0 * t)
            bx = 20.0 * math.cos(spin) + buzz + rng.gauss(0, 0.12)
            by = 20.0 * math.sin(spin) + rng.gauss(0, 0.12)
            bz = 5.0 + 2.0 * math.sin(2 * math.pi * 0.05 * t) + rng.gauss(0, 0.12)
            mag = math.sqrt(bx * bx + by * by + bz * bz)

            # Two probes drifting on their own schedules -- they should not
            # move in lockstep, and seeing that they do not is the point.
            temp = 25.0 + 1.5 * math.sin(2 * math.pi * 0.008 * t) + rng.gauss(0, 0.04)
            rtd = 21.0 + 3.0 * math.sin(2 * math.pi * 0.003 * t + 1.0) + rng.gauss(0, 0.03)

            sample = Sample(round(bx, 2), round(by, 2), round(bz, 2),
                            round(mag, 2), round(temp, 2), round(rtd, 2))
            self.queue.put(sample)
            self._write_record(sample)
            self.samples_seen += 1

            time.sleep(period)


# --------------------------------------------------------------- self-test

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
        (b"# CONFIG conv_avg=32 range_mt=100 axes=3 temp=1 rtd_hz=60 "
         b"period_us=200000 sck_khz=625\r\n",
         Config(32, 100, 3, 1, 60, 200000, 625)),
        (b"# CONFIG conv_avg=4 range_mt=25 axes=3 temp=1 rtd_hz=50\r\n",
         Config(4, 25, 3, 1, 50)),                # no rate fields yet
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

    # The stock build: the firmware loop is the binding ceiling by a wide
    # margin, which is the whole reason the rate control is worth having.
    stock = Config(32, 100, 3, 1, 60, 200000, 625)
    lim = delivery_limits(stock, 115200, 38.0)
    assert lim.bottleneck == "loop", lim
    assert round(lim.loop) == 5, lim
    assert round(lim.conv) == 408, lim
    assert round(lim.uart) == 303, lim

    # Ask for 1 kHz and the port becomes the wall instead -- at 115200 the
    # UART cannot carry more than ~300 lines/s no matter what the loop does.
    fast = stock._replace(period_us=1000)
    lim = delivery_limits(fast, 115200, 38.0)
    assert lim.bottleneck == "uart", lim

    # Same request at 32x averaging on a faster port: the chip is the wall.
    lim = delivery_limits(fast, 921600, 38.0)
    assert lim.bottleneck == "sensor", lim

    assert delivery_limits(None) is None

    print(f"parser OK ({len(cases)} sample cases, "
          f"{len(config_cases)} config cases, 4 ceiling cases)")


# -------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="TMAG5170 serial reader")
    ap.add_argument("--test", action="store_true", help="run the parser self-test and exit")
    ap.add_argument("--list", action="store_true", help="list COM ports and exit")
    ap.add_argument("--fake", action="store_true", help="generate fake data, no hardware")
    ap.add_argument("--port", help="COM port (default: autodetect)")
    ap.add_argument("--baud", type=int, default=BAUD, help=f"baud rate (default {BAUD})")
    ap.add_argument("--rate", type=float, help="ask the firmware for this sample rate, Hz")
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
                lim = reader.limits()
                print(f"Sensor config: {cfg.conv_avg}x averaging, "
                      f"+/-{cfg.range_mt} mT, rtd {format_rate(cfg.rtd_hz)}")
                print(f"Ceilings: sensor {format_rate(lim.conv)}, "
                      f"spi {format_rate(lim.spi)}, uart {format_rate(lim.uart)}, "
                      f"loop {format_rate(lim.loop)} "
                      f"-> {lim.bottleneck} @ {format_rate(lim.rate)}")
                if args.rate:
                    reader.set_rate_hz(args.rate)
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
