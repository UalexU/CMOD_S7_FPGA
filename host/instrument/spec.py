"""
What this instrument can and cannot do -- from the datasheets and from what
the firmware (fw/SPI_BOTH/src/main.c) actually configures.

Every limit the settings layer enforces is derived here, in one place, with
its source. Nothing in this file talks to Qt, the board or the model.

Sources
  [TMAG] TI TMAG5170 datasheet SBASAF4 (Sep 2021)
         Table 7-2 update rates, Sec 6.5 t_measure, Sec 6.6 noise/sensitivity,
         Table 7-1 ranges, Sec 6.8 SPI timing.
  [MAX]  Analog Devices MAX31865 datasheet Rev 3: t_CONV, Table 2 config.
  [FW]   fw/SPI_BOTH/src/main.c -- DEVICE_CONFIG / SENSOR_CONFIG words,
         handle_command(), the main loop, print_x100().
"""

import math
from dataclasses import dataclass

# =========================================================== TMAG5170 [TMAG]

# CONV_AVG codes 0h..5h. 6h/7h are "not used, defaults to 000b" -- so there
# is no other averaging value, and asking for 3x or 64x is meaningless.
AVERAGING = (1, 2, 4, 8, 16, 32)

# Table 7-2, continuous conversion, X+Y+Z, without temperature (ksps).
DATASHEET_XYZ_KSPS = {1: 10.0, 2: 5.7, 4: 3.1, 8: 1.6, 16: 0.8, 32: 0.4}

T_SLOT_US = 25.0          # Sec 6.5: 25 us per channel per average, +25 us

# The firmware runs the sensor in active-measure (continuous) mode with
# MAG_CH_EN = XYZ, T_CH_EN = 1 and T_RATE = 1 (temperature once per set)
# [FW device_config_word / sensor_config_word]. So the chip completes one
# set every (3 * avg + 1 temperature + 1) slots of 25 us. The datasheet does
# not tabulate the with-temperature case; this counts temperature as one
# extra 25 us channel, which reproduces Table 7-2 within ~2 % without it.
AXES = 3


def conversion_time_us(avg, axes=AXES, temperature=True):
    if avg not in AVERAGING:
        raise ValueError(f"averaging must be one of {AVERAGING}")
    return T_SLOT_US * (axes * avg + (1 if temperature else 0) + 1)


def conversion_rate_hz(avg, axes=AXES, temperature=True):
    """How often the TMAG5170 produces a *new* field reading."""
    return 1e6 / conversion_time_us(avg, axes, temperature)


# Table 7-1 / Sec 6.6, TMAG5170A1 (the firmware's 25/50/100 mT codes are
# the A1 ranges). Sensitivity in LSB/mT of the 16-bit result register.
RANGES_MT = (25, 50, 100)
SENSITIVITY_LSB_PER_MT = {25: 1308, 50: 654, 100: 326}

# Sec 6.6, RMS noise at 25 degC, +/-50 mT range, in uT:
#   (CONV_AVG=0, CONV_AVG=5)
NOISE_UT = {"xy": (140.0, 24.0), "z": (61.0, 11.0)}
TEMP_NOISE_C = (0.35, 0.06)


def noise_ut(avg, axis="xy"):
    """Approximate RMS noise. The datasheet gives only the 1x and 32x end
    points (and plots in between); interpolate on log2(avg), which is what
    the plots show and matches ~1/sqrt(avg) to within a few percent."""
    lo, hi = NOISE_UT[axis]
    f = math.log2(avg) / 5.0
    return lo * (hi / lo) ** f


SPI_MAX_HZ = 10_000_000   # Sec 6.8


# =========================================================== MAX31865 [MAX]

RTD_CONV_MS = {60: 16.7, 50: 20.0}   # continuous mode, typ
# The notch is fixed at compile time (RTD_CONFIG has no 50 Hz bit) and the
# datasheet forbids changing it in auto mode. The board reports rtd_hz.


def rtd_rate_hz(notch_hz=60):
    return 1000.0 / RTD_CONV_MS.get(notch_hz, 16.7)


# ============================================================= firmware [FW]

R_MIN, R_MAX = 1, 5000          # handle_command 'R' accepts 1..5000 Hz
# sample_period_us = 1000000 / R  (integer division), slept *after* the
# reads and the print, so the loop period is work + print + sleep.

PRINT_RESOLUTION_MT = 0.01      # print_x100(): two decimals
PRINT_RESOLUTION_C = 0.01

BAUD = 115200                   # AXI Uartlite, fixed at synthesis in Vivado
UART_BITS_PER_BYTE = 10         # 8N1
UART_FIFO_BYTES = 16            # AXI Uartlite TX FIFO depth
DEFAULT_LINE_BYTES = 38.0       # typical "Bx, By, Bz, |B|, T, Trtd\r\n"

SPI_SCK_HZ = 625_000            # SPI_SCK_KHZ in main.c
SPI_BYTES_PER_SAMPLE = 4 * 4 + 9  # 4 TMAG frames + one 9-byte RTD burst
# MicroBlaze driver overhead per XSpi_Transfer call (5 calls per sample);
# an estimate, refined at run time from the measured rate (see LoopModel).
SPI_CALL_OVERHEAD_US = 40.0


@dataclass
class LoopModel:
    """Predicts the delivered sample rate for a given R command.

    One loop = SPI work + the part of the CSV line that does not fit the
    UART FIFO (xil_printf blocks on it) + the R sleep. The UART can never
    carry more than baud / (10 * line_bytes) lines per second, however
    short the sleep.

    `overhead_us` starts as an estimate and is re-fitted from what the host
    actually receives whenever the loop (not the UART) is the bottleneck.
    """
    line_bytes: float = DEFAULT_LINE_BYTES
    baud: int = BAUD
    overhead_us: float = None

    def __post_init__(self):
        if self.overhead_us is None:
            self.overhead_us = self.estimated_overhead_us()
        self.calibrated = None       # (overhead_us, R, lines) once measured

    # -- pieces -----------------------------------------------------------

    def line_time_us(self):
        return self.line_bytes * UART_BITS_PER_BYTE / self.baud * 1e6

    def estimated_overhead_us(self):
        spi = SPI_BYTES_PER_SAMPLE * 8 / SPI_SCK_HZ * 1e6 \
            + 5 * SPI_CALL_OVERHEAD_US
        blocked = max(0.0, self.line_bytes - UART_FIFO_BYTES) \
            * UART_BITS_PER_BYTE / self.baud * 1e6
        return spi + blocked

    # -- forward and inverse ----------------------------------------------

    @staticmethod
    def period_us(r):
        return 1_000_000 // int(r)

    def rate_for_r(self, r):
        loop = self.overhead_us + self.period_us(r)
        return 1e6 / max(loop, self.line_time_us())

    def loop_ceiling_hz(self):
        return self.rate_for_r(R_MAX)

    def r_for_rate(self, hz):
        """The R command that delivers closest to `hz`, or None if `hz` is
        above what the loop and link can deliver at all."""
        if hz <= 0 or hz > self.loop_ceiling_hz() + 1e-9:
            return None
        sleep = 1e6 / hz - self.overhead_us
        if sleep <= 1e6 / R_MAX:
            return R_MAX
        r0 = max(R_MIN, min(R_MAX, int(1e6 / sleep)))
        best = min({max(R_MIN, r0 - 1), r0, min(R_MAX, r0 + 1)},
                   key=lambda r: abs(self.rate_for_r(r) - hz))
        return best

    # Calibration is only trusted when the measurement can resolve the
    # overhead: the loop period must be short next to it, and the rate must
    # come from a long count, not the GUI's 2-second estimate. At 3 Hz a 1 %
    # rate error is 3 ms of "overhead" -- bigger than the thing measured.
    CAL_MIN_SECONDS = 10.0
    CAL_MIN_LINES = 300
    CAL_MAX_PERIOD_FACTOR = 10.0     # period <= 10 x overhead

    def calibrate(self, lines, seconds, r):
        """Fit the per-loop overhead from `lines` received in `seconds` at
        command R. Returns True if the fit was used. Rejects fits that are
        imprecise (slow loop, short count), UART-bound (they say nothing
        about the loop) or physically implausible (outside 0.5x..4x of the
        analytic estimate)."""
        if not r or seconds < self.CAL_MIN_SECONDS \
                or lines < self.CAL_MIN_LINES:
            return False
        period = self.period_us(r)
        estimate = self.estimated_overhead_us()
        if period > self.CAL_MAX_PERIOD_FACTOR * estimate:
            return False
        loop_us = seconds * 1e6 / lines
        if loop_us <= self.line_time_us() * 1.05:
            return False
        fitted = loop_us - period
        if not 0.5 * estimate <= fitted <= 4.0 * estimate:
            return False
        self.overhead_us = fitted
        self.calibrated = (round(fitted), int(r), int(lines))
        return True

    def reset(self):
        self.overhead_us = self.estimated_overhead_us()
        self.calibrated = None


# ======================================================== combined ceilings

def ceilings(avg, loop=None, rtd_hz=60):
    """Every limit on the delivered sample rate at a given averaging.
    The smallest is what you can actually have."""
    loop = loop or LoopModel()
    out = {
        "sensor": conversion_rate_hz(avg),
        "uart": 1e6 / loop.line_time_us(),
        "loop": loop.loop_ceiling_hz(),
    }
    name = min(out, key=out.get)
    return {"limits_hz": {k: round(v, 1) for k, v in out.items()},
            "max_rate_hz": math.floor(out[name] * 10) / 10,
            "bottleneck": name,
            "rtd_new_value_hz": round(rtd_rate_hz(rtd_hz), 1)}


def max_rate_hz(avg, loop=None):
    """Highest settable delivered rate, floored to 0.1 Hz so that the
    number shown as the maximum is itself always accepted."""
    loop = loop or LoopModel()
    top = min(conversion_rate_hz(avg), 1e6 / loop.line_time_us(),
              loop.loop_ceiling_hz())
    return math.floor(top * 10) / 10


def resolution_mt(range_mt):
    """Smallest step you will see: the larger of the ADC LSB and the
    firmware's two-decimal print."""
    lsb = 1.0 / SENSITIVITY_LSB_PER_MT[range_mt]
    return max(lsb, PRINT_RESOLUTION_MT), lsb
