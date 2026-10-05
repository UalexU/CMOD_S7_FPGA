"""
Host-side processing of the sample stream: temperature compensation,
outlier rejection, filtering, and noise measurement.

Nothing here changes the board or the recorded/exported raw data -- it
changes what the views, the statistics, the noise meter and the field map
see. Pure numpy; scipy is used when installed and unlocks the filters that
need it (median, Hampel, Butterworth low-pass, notch). Without scipy those
options are reported as unavailable rather than half-working.

Temperature compensation -- why it is done here
-----------------------------------------------
The firmware writes DEVICE_CONFIG with MAG_TEMPCO = 00b (0 %/°C), so the
TMAG5170 applies NO magnet temperature compensation [TMAG §6.6, §7.6]. Its
on-chip option would use the *die* temperature and assumes magnet and
sensor are at the same temperature (TI E2E 1395040); for a field sensor
sitting in a magnet bore that is rarely true. Here the field is normalised
to a reference temperature using a measured temperature -- the RTD (on the
magnet) or the die -- and the magnet's coefficient:

    B(T) = B(T_ref) * (1 + a * (T - T_ref))    a in %/°C, NdFeB ≈ -0.12
    =>  B_comp = B / (1 + a * (T - T_ref))

The MAX31865 has no temperature compensation of its own to enable; the
nearest thing is 3-wire lead compensation, which is a wiring/firmware
setting (RTD_WIRE_MODE in main.c), not a host option.
"""

import math

import numpy as np

try:
    from scipy import ndimage, signal
    HAVE_SCIPY = True
except ImportError:                     # pragma: no cover
    HAVE_SCIPY = False

from . import spec

FIELD = ("bx", "by", "bz")
TEMPS = ("temp", "rtd")

# Remanence temperature coefficients (%/°C) -- typical supplier values, and
# the same three the TMAG5170's MAG_TEMPCO field offers (sign: Br falls).
MAGNET_TEMPCO = {"NdFeB": -0.12, "SmCo": -0.03, "ferrite": -0.20}

FILTERS = ("none", "moving_average", "median", "ema", "lowpass", "notch")
NEEDS_SCIPY = {"median", "lowpass", "notch", "hampel"}
OUTLIERS = ("none", "hampel", "sigma_clip")


def available(option):
    return HAVE_SCIPY or option not in NEEDS_SCIPY


# ================================================================ pipeline

def _mag(cols):
    cols["mag"] = np.sqrt(cols["bx"] ** 2 + cols["by"] ** 2
                          + cols["bz"] ** 2)


def temp_compensate(cols, coeff_pct, t_ref, source):
    """Normalise the field to t_ref. Returns the scale used (array)."""
    temp = cols.get("rtd" if source == "rtd" else "temp")
    if temp is None or not len(temp):
        return None
    scale = 1.0 + (coeff_pct / 100.0) * (temp - t_ref)
    scale = np.where(np.abs(scale) < 1e-6, 1.0, scale)
    for k in FIELD:
        cols[k] = cols[k] / scale
    _mag(cols)
    return scale


def reject_outliers(x, method, window, k):
    """Replace outliers with the local median (Hampel) or the global
    median (sigma clip). Returns (cleaned, n_rejected)."""
    if method == "none" or len(x) < 5:
        return x, 0
    if method == "hampel":
        w = min(window, len(x) - (1 - len(x) % 2))
        if w < 3:
            return x, 0
        med = ndimage.median_filter(x, size=w, mode="nearest")
        mad = ndimage.median_filter(np.abs(x - med), size=w, mode="nearest")
        bad = np.abs(x - med) > k * 1.4826 * np.maximum(mad, 1e-12)
        out = np.where(bad, med, x)
        return out, int(bad.sum())
    if method == "sigma_clip":
        med = np.median(x)
        sd = 1.4826 * np.median(np.abs(x - med))
        if sd <= 0:
            return x, 0
        bad = np.abs(x - med) > k * sd
        return np.where(bad, med, x), int(bad.sum())
    return x, 0


def apply_filter(x, fs, kind, window=5, tau_s=1.0, cutoff_hz=1.0,
                 order=2, notch_hz=60.0, notch_q=30.0):
    n = len(x)
    if kind == "none" or n < 3:
        return x
    if kind == "moving_average":
        w = min(window, n)
        if w < 2:
            return x
        kernel = np.ones(w) / w
        # 'same' with edge padding: no shift, no shrinking window
        pad = np.pad(x, (w // 2, w - 1 - w // 2), mode="edge")
        return np.convolve(pad, kernel, mode="valid")
    if kind == "median":
        return ndimage.median_filter(x, size=min(window, n), mode="nearest")
    if kind == "ema":
        # First-order low-pass with time constant tau, as an FIR kernel
        # truncated at 6 tau -- numpy only, no state between frames.
        a = 1.0 - math.exp(-1.0 / max(tau_s * fs, 1e-9))
        m = int(min(n, max(2, 6 * tau_s * fs)))
        kernel = a * (1 - a) ** np.arange(m)
        kernel /= kernel.sum()
        pad = np.concatenate([np.full(m - 1, x[0]), x])
        return np.convolve(pad, kernel, mode="valid")
    if kind == "lowpass":
        sos = signal.butter(order, cutoff_hz, btype="low", fs=fs,
                            output="sos")
        if n <= 3 * (2 * len(sos) + 1):
            return x
        return signal.sosfiltfilt(sos, x)
    if kind == "notch":
        b, a = signal.iirnotch(notch_hz, notch_q, fs=fs)
        if n <= 3 * max(len(a), len(b)):
            return x
        return signal.filtfilt(b, a, x)
    return x


class Pipeline:
    """Built from the settings; called with the raw window on every frame.

        cols = pipeline(t, cols)      # dict of arrays, untared
    """

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.last = {"outliers": 0, "fs": None}

    def configure(self, values):
        self.values = dict(values)

    @property
    def active(self):
        v = self.values
        return bool(v.get("temp_comp")) or v.get("filter", "none") != "none" \
            or v.get("outlier", "none") != "none"

    def __call__(self, t, cols, filtering=True):
        v = self.values
        if not len(t):
            return cols
        cols = {k: np.asarray(a, dtype=float).copy() for k, a in cols.items()}
        if v.get("temp_comp"):
            temp_compensate(cols, v.get("temp_coeff_pct", -0.12),
                            v.get("temp_ref_C", 25.0),
                            v.get("temp_comp_source", "rtd"))
        if not filtering:
            return cols
        fs = (len(t) - 1) / (t[-1] - t[0]) if len(t) > 1 and t[-1] > t[0] \
            else None
        self.last["fs"] = fs
        rejected = 0
        method = v.get("outlier", "none")
        if method != "none" and available(method):
            for k in FIELD + TEMPS:
                if k in cols:
                    cols[k], nrej = reject_outliers(
                        cols[k], method, v.get("outlier_window", 11),
                        v.get("outlier_k", 3.5))
                    rejected += nrej
        self.last["outliers"] = rejected
        kind = v.get("filter", "none")
        self.last["error"] = None
        if kind != "none" and fs and available(kind):
            for k in FIELD + TEMPS:
                if k not in cols:
                    continue
                try:
                    cols[k] = apply_filter(
                        cols[k], fs, kind,
                        window=v.get("filter_window", 5),
                        tau_s=v.get("filter_tau_s", 1.0),
                        cutoff_hz=v.get("filter_cutoff_hz", 1.0),
                        order=v.get("filter_order", 2),
                        notch_hz=v.get("notch_hz", 60.0),
                        notch_q=v.get("notch_q", 30.0))
                except ValueError as e:
                    # e.g. a notch above Nyquist after the rate dropped: show
                    # the unfiltered trace and say why, never crash a frame.
                    self.last["error"] = f"{kind} skipped: {e}"
                    break
        if method != "none" or kind != "none":
            _mag(cols)
        return cols


# ============================================================ noise metrics

def noise_report(t, cols, averaging, range_mt):
    """Per-channel noise over one block of samples, next to what the
    datasheet and the firmware's 2-decimal printing predict."""
    n = len(t)
    if n < 10:
        return {"error": f"only {n} samples; need at least 10"}
    dur = float(t[-1] - t[0])
    fs = (n - 1) / dur if dur > 0 else None
    q_ut = spec.PRINT_RESOLUTION_MT * 1000 / math.sqrt(12)   # quantisation
    out = {"samples": n, "seconds": round(dur, 2),
           "rate_hz": round(fs, 2) if fs else None,
           "print_quantisation_uT_rms": round(q_ut, 2), "channels": {}}
    for key, x in cols.items():
        x = np.asarray(x, dtype=float)
        if not len(x):
            continue
        is_field = key in FIELD or key == "mag"
        unit_scale = 1000.0 if is_field else 1.0      # mT -> µT
        # detrend first: slow drift is not noise
        tt = np.asarray(t) - t[0]
        slope, icpt = np.polyfit(tt, x, 1) if dur > 0 else (0.0, x.mean())
        resid = x - (slope * tt + icpt)
        sd = float(resid.std(ddof=1)) * unit_scale
        row = {
            "unit": "µT" if is_field else "°C",
            "mean": round(float(x.mean()), 4),
            "std": round(sd, 3),
            "p2p": round(float(np.ptp(resid)) * unit_scale, 3),
            "drift_per_min": round(float(slope) * 60 * unit_scale, 3),
        }
        if fs:
            # white-noise density over the band 0..fs/2
            row["density_per_rtHz"] = round(sd / math.sqrt(fs / 2), 3)
        if key in FIELD:
            axis = "z" if key == "bz" else "xy"
            ds = spec.noise_ut(averaging, axis)
            expected = math.sqrt(ds ** 2 + q_ut ** 2)
            row["expected_std"] = round(expected, 1)
            row["vs_expected"] = round(sd / expected, 2) if expected else None
        out["channels"][key] = row
    out["note"] = (f"Expected σ: datasheet typ at {averaging}x averaging "
                   "(given for the ±50 mT range) combined with the 10 µT "
                   "print step. Drift is removed before σ is computed.")
    return out
