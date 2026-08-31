"""
sensor.py -- owns the serial port, turns bytes into temperatures.

Knows nothing about GUIs. Import it, call start(), read from .queue.

Standalone use (test it before any GUI exists):
    python sensor.py --test        # parser self-test, no hardware needed
    python sensor.py --fake        # fake sine-wave data, no hardware needed
    python sensor.py --list        # show available COM ports
    python sensor.py               # read the board
    python sensor.py --program     # program the board first, then read

Requires: pip install pyserial      (Windows Python, not WSL)
"""

import argparse
import math
import pathlib
import queue
import subprocess
import sys
import threading
import time

BAUD = 115200          # must match the BSP setting in Vitis
HANDSHAKE = b"S"       # what the C code blocks on (WAIT_FOR_HOST)

# Plausibility window for a PT100. Anything outside this is corrupted data,
# not a real reading -- usually a sign of SPI byte misalignment.
MIN_C, MAX_C = -200.0, 850.0


# ------------------------------------------------------------------ parsing

def parse_line(raw):
    """b'256\\r\\n' -> 25.6

    Returns None for anything that isn't a reading: blank lines, the
    DEBUG_MODE diagnostics, half-received lines, out-of-range garbage.
    Pure function -- no serial, no state. This is the part worth testing.
    """
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("ascii")
        except UnicodeDecodeError:
            return None

    text = raw.strip()
    if not text:
        return None

    try:
        celsius = int(text) / 10.0        # C sends tenths of a degree
    except ValueError:
        return None                       # debug output, banner, noise

    if not (MIN_C <= celsius <= MAX_C):
        return None

    return celsius


def self_test():
    """Everything the parser must survive. No board required."""
    cases = [
        (b"256\r\n",      25.6),   # normal reading
        (b"-15\r\n",      -1.5),   # below zero
        (b"0\r\n",         0.0),   # exactly zero is a real reading
        (b"1234\r\n",    123.4),
        (b"",             None),   # read timeout
        (b"\r\n",         None),   # blank line
        (b"adc=9011 rtd=110 ohm\r\n", None),   # DEBUG_MODE line
        (b"M13 starting (MAX31865)\r\n", None),
        (b"25.6\r\n",     None),   # decimal point -> not our format
        (b"99999\r\n",    None),   # 9999.9 C, impossible
        (b"\xff\xfe\r\n", None),   # line noise
    ]
    for raw, expected in cases:
        got = parse_line(raw)
        assert got == expected, f"parse_line({raw!r}) -> {got}, expected {expected}"
    print(f"parser OK ({len(cases)} cases)")


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
        if any(k in blurb for k in ("USB Serial", "CP210", "FT232", "Silicon Labs")):
            return p.device
    return None


def program_board(fast=False):
    """Push bitstream + ELF over JTAG by calling run_board.py."""
    script = pathlib.Path(__file__).with_name("run_board.py")
    cmd = [sys.executable, str(script)] + (["--fast"] if fast else [])
    print(f"Programming board: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


# ------------------------------------------------------------------ reader

class SensorReader:
    """Background thread: serial in, temperatures out through .queue.

    Blocks freely -- it is not the GUI thread, so nothing freezes.
    Never touches a widget. The queue is the only hand-off point.
    """

    def __init__(self, port=None, baud=BAUD, fake=False):
        self.port = port
        self.baud = baud
        self.fake = fake
        self.queue = queue.Queue()
        self.error = None            # set if the port could not be opened
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
            self.error = f"Could not open {port}: {e}\nIs the Vitis terminal still open?"
            return

        with ser:
            time.sleep(0.2)
            ser.reset_input_buffer()      # discard boot banner and noise
            ser.write(HANDSHAKE)          # the board is blocked until this
            ser.flush()

            while not self._stop.is_set():
                value = parse_line(ser.readline())
                if value is not None:
                    self.queue.put(value)

    def _run_fake(self):
        """A slow sine wave, so the GUI can be built with no board attached."""
        t = 0.0
        while not self._stop.is_set():
            self.queue.put(round(22.0 + 3.0 * math.sin(t), 1))
            t += 0.15
            time.sleep(1.0)


# -------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--test",    action="store_true", help="run the parser self-test and exit")
    ap.add_argument("--list",    action="store_true", help="list COM ports and exit")
    ap.add_argument("--fake",    action="store_true", help="generate fake data, no hardware")
    ap.add_argument("--program", action="store_true", help="run run_board.py first")
    ap.add_argument("--fast",    action="store_true", help="with --program, skip the bitstream")
    ap.add_argument("--port",    help="COM port (default: autodetect)")
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

    if args.program:
        program_board(fast=args.fast)

    reader = SensorReader(port=args.port, fake=args.fake).start()
    print("Reading. Ctrl-C to stop.")

    try:
        while True:
            time.sleep(0.25)
            if reader.error:
                sys.exit(reader.error)
            for value in reader.drain():
                print(f"{value:7.1f} C")
    except KeyboardInterrupt:
        pass
    finally:
        reader.stop()


if __name__ == "__main__":
    main()
