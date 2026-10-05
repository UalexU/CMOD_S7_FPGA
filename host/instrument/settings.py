"""
The instrument's settings as data: schema, validation, presets, and the
settings.json file that is their single source of truth.

    host/settings.json        the current settings + saved presets

The GUI's controls, a person editing settings.json by hand and the assistant
all change settings through SettingsStore.validate()/commit(), so the same
rules apply to all three. Board settings are *requested* here; what the
board is actually doing comes back in its '# CONFIG' line and is tracked
separately (see controller.py).

Pure Python, no Qt.
"""

import copy
import json
import math
import os
import tempfile
import time
from pathlib import Path

from . import processing, spec

SETTINGS_PATH = Path(__file__).resolve().parents[1] / "settings.json"

VIEWS = ("strip", "vector", "spectrum", "distribution", "map")
CHANNELS = ("bx", "by", "bz", "mag", "temp", "rtd")
SEGMENTS = (1, 2, 4, 8, 16)
THEMES = ("dark", "light")

# key -> description. "board" settings go to the firmware and need the user
# to approve them when the assistant proposes them; "display" settings only
# change the picture.
SCHEMA = {
    # -- board -----------------------------------------------------------
    "averaging": dict(
        group="board", type="choice", choices=spec.AVERAGING, unit="x",
        help="TMAG5170 CONV_AVG. Sets how often the sensor produces a new "
             "reading (more averaging = slower, quieter)."),
    "sample_rate_hz": dict(
        group="board", type="number", min=1.0, unit="Hz",
        help="Delivered sample rate. Upper limit depends on averaging, the "
             "115200-baud UART and the firmware loop; see describe_options."),
    "range_mT": dict(
        group="board", type="choice", choices=spec.RANGES_MT, unit="mT",
        help="Full scale per axis (TMAG5170A1). Smaller = finer steps, but "
             "fields beyond it clip."),
    "streaming": dict(
        group="board", type="bool",
        help="Whether the board sends samples (P 1) or holds (P 0)."),
    # -- display ---------------------------------------------------------
    "window_s": dict(group="display", type="number", min=0.5, max=600.0,
                     unit="s", help="Time span shown in the views."),
    "fps": dict(group="display", type="int", min=1, max=60, unit="fps",
                help="Screen refresh rate. Does not affect acquisition."),
    "autoscale": dict(group="display", type="bool",
                      help="Autoscale the y axes."),
    "visible_channels": dict(group="display", type="subset",
                             choices=CHANNELS,
                             help="Which traces are drawn."),
    "view": dict(group="display", type="choice", choices=VIEWS,
                 help="Which tab is shown."),
    "spectrum_segments": dict(group="display", type="choice",
                              choices=SEGMENTS,
                              help="Welch averaging in the Spectrum view."),
    "theme": dict(group="display", type="choice", choices=THEMES,
                  help="Colour theme."),
    # -- processing (host side; views, stats, noise and map -- never the
    #    recorded/exported raw data, never the board) ----------------------
    "temp_comp": dict(
        group="processing", type="bool",
        help="Normalise the field to temp_ref_C using a measured "
             "temperature and the magnet's coefficient. The TMAG5170 does "
             "not do this as configured (MAG_TEMPCO = 0 %/°C in main.c)."),
    "temp_comp_source": dict(
        group="processing", type="choice", choices=("rtd", "die"),
        help="Temperature used: rtd = MAX31865 probe (put it on the "
             "magnet), die = TMAG5170 internal sensor."),
    "temp_coeff_pct": dict(
        group="processing", type="number", min=-1.0, max=1.0, unit="%/°C",
        help="Magnet remanence coefficient: NdFeB -0.12, SmCo -0.03, "
             "ferrite -0.20 (supplier values; the TMAG's MAG_TEMPCO "
             "options are the same three)."),
    "temp_ref_C": dict(
        group="processing", type="number", min=-40.0, max=125.0, unit="°C",
        help="Temperature the field is normalised to."),
    "filter": dict(
        group="processing", type="choice", choices=processing.FILTERS,
        help="moving_average / median (window in samples), ema (time "
             "constant), lowpass (Butterworth, cutoff), notch (mains)."),
    "filter_window": dict(group="processing", type="int", min=2, max=501,
                          unit="samples",
                          help="Window for moving_average and median "
                               "(median: odd)."),
    "filter_tau_s": dict(group="processing", type="number", min=0.001,
                         max=600.0, unit="s",
                         help="Time constant of the ema filter."),
    "filter_cutoff_hz": dict(group="processing", type="number", min=0.001,
                             max=2500.0, unit="Hz",
                             help="Low-pass cutoff; must be below Nyquist "
                                  "(half the sample rate)."),
    "filter_order": dict(group="processing", type="choice",
                         choices=(1, 2, 4),
                         help="Butterworth order (zero-phase, so the "
                              "effective order is doubled)."),
    "notch_hz": dict(group="processing", type="number", min=1.0,
                     max=1000.0, unit="Hz",
                     help="Notch frequency, e.g. 60 Hz mains (US). Must be "
                          "below Nyquist."),
    "notch_q": dict(group="processing", type="number", min=1.0, max=100.0,
                    help="Notch quality factor (higher = narrower)."),
    "outlier": dict(group="processing", type="choice",
                    choices=processing.OUTLIERS,
                    help="hampel: replace points > k·MAD from the local "
                         "median; sigma_clip: > k·σ from the window "
                         "median."),
    "outlier_window": dict(group="processing", type="int", min=3, max=501,
                           unit="samples",
                           help="Hampel window (odd)."),
    "outlier_k": dict(group="processing", type="number", min=2.0,
                      max=10.0, unit="σ",
                      help="Rejection threshold in robust standard "
                           "deviations."),
}

PROCESSING_KEYS = [k for k, s in SCHEMA.items() if s["group"] == "processing"]

BOARD_KEYS = [k for k, s in SCHEMA.items() if s["group"] == "board"]
# Everything that is not a board setting applies at once (display and
# processing alike) -- these names are what the approval logic uses.
DISPLAY_KEYS = [k for k, s in SCHEMA.items() if s["group"] != "board"]

DEFAULTS = {
    "averaging": 32, "sample_rate_hz": 3.0, "range_mT": 100,
    "streaming": True,
    "window_s": 20.0, "fps": 30, "autoscale": True,
    "visible_channels": list(CHANNELS), "view": "strip",
    "spectrum_segments": 4, "theme": "dark",
    "temp_comp": False, "temp_comp_source": "rtd", "temp_coeff_pct": -0.12,
    "temp_ref_C": 25.0,
    "filter": "none", "filter_window": 5, "filter_tau_s": 1.0,
    "filter_cutoff_hz": 1.0, "filter_order": 2, "notch_hz": 60.0,
    "notch_q": 30.0,
    "outlier": "none", "outlier_window": 21, "outlier_k": 3.5,
}

BUILTIN_PRESETS = {
    "noise floor": dict(
        averaging=32, sample_rate_hz=20, window_s=60,
        view="distribution",
        _about="Quietest readings (32x averaging) for measuring sigma."),
    "balanced": dict(
        averaging=8, sample_rate_hz=100, window_s=20,
        view="strip", _about="General use."),
    "fast": dict(
        averaging=1, sample_rate_hz="max", window_s=5,
        view="spectrum",
        _about="Highest delivered rate the serial link allows; noisiest."),
    "slow logging": dict(
        averaging=32, sample_rate_hz=2, window_s=600,
        view="strip", _about="Long, quiet records."),
}


class Context:
    """What validation needs to know about the world right now."""

    def __init__(self, loop=None, rtd_hz=60, field_abs_max_mT=None,
                 connected=False):
        self.loop = loop or spec.LoopModel()
        self.rtd_hz = rtd_hz
        # per-axis max |B| over the last few seconds, *raw* (untared)
        self.field_abs_max_mT = field_abs_max_mT or {}
        self.connected = connected


class Result:
    def __init__(self):
        self.changes = {}         # normalized key -> value actually stored
        self.errors = {}          # key -> why it is impossible
        self.notes = []           # consequences worth telling the user

    @property
    def ok(self):
        return not self.errors

    def as_dict(self):
        return {"ok": self.ok, "changes": self.changes,
                "errors": self.errors, "notes": self.notes}


def _fmt_choices(choices):
    return ", ".join(str(c) for c in choices)


# ================================================================= store

class SettingsStore:

    def __init__(self, path=SETTINGS_PATH):
        self.path = Path(path)
        self.values = copy.deepcopy(DEFAULTS)
        self.presets = {}
        self._last_written = None
        self.load_error = None
        self.save_error = None

    # -- file ---------------------------------------------------------------

    def load(self, context=None, base=None):
        """Read settings.json. Invalid entries are dropped (and reported in
        load_error) rather than trusted -- a hand-edited file can say
        anything. A dropped entry keeps its value from `base` (the current
        settings when re-reading), or the default on first load."""
        self.load_error = None
        if not self.path.exists():
            return False
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            self.load_error = f"settings.json unreadable: {e}"
            return False
        current = data.get("current", {}) if isinstance(data, dict) else {}
        current = dict(current)
        old_smooth = current.pop("smoothing", None)   # pre-filter settings
        if isinstance(old_smooth, int) and old_smooth > 1 \
                and "filter" not in current:
            current["filter"] = "moving_average"
            current["filter_window"] = old_smooth
        start = copy.deepcopy(base if base is not None else DEFAULTS)
        res = self.validate(current, context, base=start)
        self.values = start
        self.values.update(res.changes)
        if res.errors:
            self.load_error = "; ".join(f"{k}: {v}" for k, v in
                                        res.errors.items())
        presets = data.get("presets", {}) if isinstance(data, dict) else {}
        self.presets = {str(k): v for k, v in presets.items()
                        if isinstance(v, dict)}
        return True

    def save(self):
        doc = {
            "_about": "TMAG5170 Scope settings. Edit freely: the GUI "
                      "re-reads this file, and anything outside the "
                      "instrument's limits is rejected and reported.",
            "current": self.values,
            "presets": self.presets,
        }
        text = json.dumps(doc, indent=2) + "\n"
        if text == self._last_written:
            return False
        self.save_error = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent,
                                       prefix=".settings.", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        except OSError as e:
            self.save_error = f"could not write settings: {e}"
            return False
        # Atomic replace, so the file is never half-written. On Windows the
        # replace fails while another process (an editor, antivirus, a sync
        # client) briefly holds settings.json open -- retry, and never leave
        # the temp file behind.
        for attempt in range(6):
            try:
                os.replace(tmp, self.path)
                self._last_written = text
                return True
            except PermissionError as e:
                last = e
                time.sleep(0.05 * (attempt + 1))
            except OSError as e:
                last = e
                break
        try:
            os.remove(tmp)
        except OSError:
            pass
        self.save_error = (f"settings.json is locked by another program "
                           f"({last}); will retry on the next change")
        return False

    def file_changed_externally(self):
        try:
            return self.path.read_text(encoding="utf-8") != self._last_written
        except OSError:
            return False

    # -- validation -----------------------------------------------------------

    def validate(self, changes, context=None, base=None, snap=False):
        """Check `changes` against the schema *and* the hardware. Returns a
        Result whose .changes are normalized values; impossible requests are
        in .errors with the allowed values, never silently clamped.

        snap: a sample rate between two achievable rates (the firmware's R
        is a whole number of Hz, so low rates come in steps) is moved to the
        nearest achievable one, with a note. Without snap -- the assistant's
        case -- it is an error listing both neighbours, so the model has to
        choose and say so."""
        ctx = context or Context()
        base = base if base is not None else self.values
        res = Result()
        if not isinstance(changes, dict):
            res.errors["_"] = "changes must be an object of setting: value"
            return res

        for key, raw in changes.items():
            if key not in SCHEMA:
                res.errors[key] = ("unknown setting. Known: "
                                   + ", ".join(SCHEMA))
                continue
            try:
                res.changes[key] = self._coerce(key, raw)
            except ValueError as e:
                res.errors[key] = str(e)

        # The rest depends on the settings *after* the change.
        after = dict(base)
        after.update(res.changes)
        avg = after["averaging"]

        # sample rate: "max" is allowed and resolves to the ceiling
        if "sample_rate_hz" in changes and "sample_rate_hz" not in res.errors:
            want = res.changes["sample_rate_hz"]
            top = spec.max_rate_hz(avg, ctx.loop)
            if want == "max":
                want = top
                res.changes["sample_rate_hz"] = want
                after["sample_rate_hz"] = want
            if want > top + 1e-6:
                c = spec.ceilings(avg, ctx.loop, ctx.rtd_hz)
                res.errors["sample_rate_hz"] = (
                    f"{want:g} Hz is not achievable at {avg}x averaging: the "
                    f"maximum is {top:.1f} Hz, set by the {c['bottleneck']} "
                    f"(sensor {c['limits_hz']['sensor']:.0f} Hz, UART "
                    f"{c['limits_hz']['uart']:.0f} Hz, loop "
                    f"{c['limits_hz']['loop']:.0f} Hz). Choose 1..{top:.1f} "
                    "Hz, or 'max'.")
                res.changes.pop("sample_rate_hz", None)
                after["sample_rate_hz"] = base["sample_rate_hz"]
            else:
                near = self._achievable_near(want, ctx.loop)
                if near and abs(near[0] - want) > max(0.05, 0.01 * want):
                    if snap:
                        res.changes["sample_rate_hz"] = near[0]
                        after["sample_rate_hz"] = near[0]
                        res.notes.append(
                            f"{want:g} Hz is between two achievable rates; "
                            f"set to the nearest, {near[0]:g} Hz")
                    else:
                        res.errors["sample_rate_hz"] = (
                            f"{want:g} Hz is not exactly achievable: the "
                            "firmware sets the loop in whole-Hz steps, so "
                            "the nearest delivered rates are "
                            + " and ".join(f"{x:g} Hz" for x in near)
                            + ". Choose one of those.")
                        res.changes.pop("sample_rate_hz", None)
                        after["sample_rate_hz"] = base["sample_rate_hz"]
        elif "averaging" in res.changes:
            # Averaging went up and the old rate no longer fits: say so
            # rather than leave an impossible combination in place.
            top = spec.max_rate_hz(avg, ctx.loop)
            if after["sample_rate_hz"] > top + 1e-6:
                res.errors["averaging"] = (
                    f"at {avg}x the sensor delivers at most {top:.1f} Hz, "
                    f"below the current {after['sample_rate_hz']:g} Hz. Also "
                    f"set sample_rate_hz <= {top:.1f} (or 'max').")

        # range: refuse a range the field present right now would clip
        if "range_mT" in res.changes:
            rng = res.changes["range_mT"]
            over = {ax: v for ax, v in ctx.field_abs_max_mT.items()
                    if v is not None and v >= 0.95 * rng}
            if over:
                worst = max(over.values())
                ok = [r for r in spec.RANGES_MT if worst < 0.95 * r]
                res.errors["range_mT"] = (
                    f"±{rng} mT would clip: {', '.join(over)} reached "
                    f"{worst:.1f} mT in the last seconds. Ranges that fit: "
                    + (_fmt_choices(ok) if ok else "none -- field exceeds "
                       "±100 mT, the TMAG5170A1 maximum"))
                res.changes.pop("range_mT")

        # processing settings that depend on the data rate
        dependent = set(PROCESSING_KEYS) | {"window_s", "sample_rate_hz"}
        if dependent & set(res.changes):
            # Only what this request breaks: a problem that already existed
            # (e.g. after the board's rate was adopted on connect) must not
            # block an unrelated change such as switching temp_comp on.
            before = self._processing_errors(base)
            for key, why in self._processing_errors(after).items():
                if key in res.changes or key not in before:
                    res.errors[key] = why
                    res.changes.pop(key, None)

        self._notes(res, after, ctx)
        return res

    @staticmethod
    def _processing_errors(after):
        """What the chosen filters cannot do at this sample rate and window.
        Keyed by the setting to change; the rate change that caused it is
        named in the message so it can be fixed in the same request."""
        err = {}
        rate = after["sample_rate_hz"]
        nyq = rate / 2
        n = int(after["window_s"] * rate)
        f, o = after["filter"], after["outlier"]
        for opt, key in ((f, "filter"), (o, "outlier")):
            if not processing.available(opt):
                err[key] = (f"'{opt}' needs scipy, which is not installed "
                            "here (pip install scipy).")
        if f in ("moving_average", "median"):
            w = after["filter_window"]
            if f == "median" and w % 2 == 0:
                err["filter_window"] = (f"a median window must be odd; use "
                                        f"{w - 1} or {w + 1}")
            elif w >= n:
                err["filter_window"] = (
                    f"{w} samples is longer than the {n} samples on screen "
                    f"({after['window_s']:g} s at {rate:g} Hz); use <= "
                    f"{max(2, n - 1)} or a longer window_s")
        elif f == "ema":
            tau = after["filter_tau_s"]
            if tau < 1 / rate:
                err["filter_tau_s"] = (
                    f"{tau:g} s is shorter than one sample at {rate:g} Hz "
                    f"({1 / rate:.3g} s), so it would do nothing; use >= "
                    f"{1 / rate:.3g} s")
            elif tau > after["window_s"]:
                err["filter_tau_s"] = (
                    f"{tau:g} s is longer than the {after['window_s']:g} s "
                    "window; the trace would never settle")
        elif f == "lowpass":
            fc = after["filter_cutoff_hz"]
            if fc >= 0.45 * rate:
                err["filter_cutoff_hz"] = (
                    f"{fc:g} Hz is not below Nyquist with margin: at "
                    f"{rate:g} Hz sampling, cutoff must be < "
                    f"{0.45 * rate:.3g} Hz (Nyquist {nyq:g} Hz)")
            elif fc < 2 / after["window_s"]:
                err["filter_cutoff_hz"] = (
                    f"{fc:g} Hz is too low to resolve in a "
                    f"{after['window_s']:g} s window; use >= "
                    f"{2 / after['window_s']:.3g} Hz or a longer window")
        elif f == "notch":
            fn = after["notch_hz"]
            if fn >= 0.45 * rate:
                alias = abs(fn - round(fn / rate) * rate)
                err["notch_hz"] = (
                    f"{fn:g} Hz cannot be filtered at {rate:g} Hz sampling "
                    f"(Nyquist {nyq:g} Hz): it is not in the data as "
                    f"{fn:g} Hz -- mains would alias to ≈{alias:.3g} Hz. "
                    f"Raise the rate above {fn / 0.45:.0f} Hz or use "
                    "lowpass/ema.")
        if o == "hampel":
            w = after["outlier_window"]
            if w % 2 == 0:
                err["outlier_window"] = (f"must be odd; use {w - 1} or "
                                         f"{w + 1}")
            elif w >= n:
                err["outlier_window"] = (f"{w} samples is more than the {n} "
                                         f"on screen; use <= {max(3, n - 1)}")
        return err

    @staticmethod
    def _achievable_near(hz, loop):
        """The achievable delivered rates closest to hz (best first), each
        rounded to 0.1 Hz."""
        r = loop.r_for_rate(hz)
        if r is None:
            return None
        cands = {max(spec.R_MIN, r + d) for d in (-1, 0, 1)}
        cands = {c for c in cands if c <= spec.R_MAX}
        rates = sorted({round(loop.rate_for_r(c), 1) for c in cands},
                       key=lambda x: abs(x - hz))
        below = [x for x in rates if x <= hz]
        above = [x for x in rates if x > hz]
        out = [rates[0]]
        other = (above if rates[0] <= hz else below)
        if other:
            out.append(min(other, key=lambda x: abs(x - hz)))
        return out

    @staticmethod
    def _coerce(key, raw):
        s = SCHEMA[key]
        t = s["type"]
        if t == "bool":
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, str) and raw.lower() in ("on", "true", "1",
                                                         "off", "false", "0"):
                return raw.lower() in ("on", "true", "1")
            raise ValueError("must be true or false")
        if t == "choice":
            for c in s["choices"]:
                if raw == c or str(raw).strip().lower().rstrip("x").replace(
                        "mt", "").strip() == str(c).lower():
                    return c
            raise ValueError(f"{raw!r} is not an option. Allowed: "
                             + _fmt_choices(s["choices"]))
        if t == "subset":
            items = raw if isinstance(raw, list) else [raw]
            bad = [i for i in items if i not in s["choices"]]
            if bad or not items:
                raise ValueError(f"must be a non-empty list from: "
                                 + _fmt_choices(s["choices"]))
            return [c for c in s["choices"] if c in items]
        if key == "sample_rate_hz" and isinstance(raw, str) \
                and raw.strip().lower() == "max":
            return "max"
        try:
            v = float(raw)
        except (TypeError, ValueError):
            raise ValueError("must be a number") from None
        if not math.isfinite(v):
            raise ValueError("must be a finite number")
        if t == "int":
            if v != int(v):
                raise ValueError("must be a whole number")
            v = int(v)
        lo, hi = s.get("min"), s.get("max")
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            raise ValueError(f"must be between {lo} and {hi}"
                             if hi is not None else f"must be >= {lo}")
        if key == "sample_rate_hz":
            v = round(v, 1)
        return v

    @staticmethod
    def _notes(res, after, ctx):
        avg, rate, rng = (after["averaging"], after["sample_rate_hz"],
                          after["range_mT"])
        if any(k in res.changes for k in ("averaging", "sample_rate_hz")):
            r = ctx.loop.r_for_rate(rate)
            if r is not None:
                got = ctx.loop.rate_for_r(r)
                res.notes.append(
                    f"firmware command R {r}; predicted delivered rate "
                    f"≈{got:.1f} Hz (loop timing varies by a few %, the "
                    "Throughput panel shows the measured rate)")
            conv = spec.conversion_rate_hz(avg)
            res.notes.append(
                f"{avg}x averaging: sensor makes a new reading every "
                f"{1e3 / conv:.2f} ms ({conv:.0f} Hz); "
                f"noise ≈{spec.noise_ut(avg, 'xy'):.0f} µT (X/Y), "
                f"≈{spec.noise_ut(avg, 'z'):.0f} µT (Z) rms (datasheet typ, "
                "±50 mT range)")
            rtd = spec.rtd_rate_hz(ctx.rtd_hz)
            if rate > rtd:
                res.notes.append(
                    f"above {rtd:.0f} Hz the RTD repeats values: the "
                    f"MAX31865 converts every {1e3 / rtd:.1f} ms")
        if "range_mT" in res.changes:
            step, lsb = spec.resolution_mt(rng)
            res.notes.append(
                f"±{rng} mT: ADC step {lsb * 1000:.2f} µT, but the firmware "
                f"prints 2 decimals, so the visible step is "
                f"{step * 1000:.0f} µT")

    # -- commit -------------------------------------------------------------

    def commit(self, changes):
        """Store already-validated changes. Returns {key: (old, new)}."""
        diff = {}
        for k, v in changes.items():
            if self.values.get(k) != v:
                diff[k] = (self.values.get(k), v)
                self.values[k] = v
        return diff

    # -- presets ------------------------------------------------------------

    def preset_names(self):
        return sorted(set(BUILTIN_PRESETS) | set(self.presets))

    def preset(self, name):
        p = self.presets.get(name) or BUILTIN_PRESETS.get(name)
        if p is None:
            raise KeyError(name)
        return {k: v for k, v in p.items() if not k.startswith("_")}

    def save_preset(self, name, keys=None):
        name = name.strip()
        if not name:
            raise ValueError("preset name is empty")
        keys = keys or list(SCHEMA)
        self.presets[name] = {k: copy.deepcopy(self.values[k]) for k in keys
                              if k in SCHEMA}
        return self.presets[name]


# ====================================================== describe for model

def describe_options(store, context=None, averaging=None):
    """Everything the assistant may choose from, with the live limits."""
    ctx = context or Context()
    avg = averaging or store.values["averaging"]
    out = {}
    for key, s in SCHEMA.items():
        d = {"group": s["group"], "current": store.values[key],
             "help": s["help"]}
        if "choices" in s:
            d["allowed"] = list(s["choices"])
        if "min" in s:
            d["min"] = s["min"]
        if "max" in s:
            d["max"] = s["max"]
        if "unit" in s:
            d["unit"] = s["unit"]
        out[key] = d

    out["sample_rate_hz"]["max"] = spec.max_rate_hz(avg, ctx.loop)
    out["sample_rate_hz"]["also_allowed"] = "'max'"
    out["sample_rate_hz"]["max_by_averaging"] = {
        a: spec.max_rate_hz(a, ctx.loop) for a in spec.AVERAGING}
    out["averaging"]["sensor_rate_hz"] = {
        a: round(spec.conversion_rate_hz(a)) for a in spec.AVERAGING}
    out["averaging"]["noise_uT_xy_z"] = {
        a: (round(spec.noise_ut(a, "xy")), round(spec.noise_ut(a, "z")))
        for a in spec.AVERAGING}
    out["range_mT"]["visible_step_uT"] = {
        r: round(spec.resolution_mt(r)[0] * 1000) for r in spec.RANGES_MT}
    out["_limits_now"] = spec.ceilings(avg, ctx.loop, ctx.rtd_hz)
    out["_limits_now"]["loop_overhead_us"] = round(ctx.loop.overhead_us)
    out["_limits_now"]["loop_overhead_measured"] = bool(ctx.loop.calibrated)
    out["_fixed_in_hardware"] = {
        "uart_baud": f"{spec.BAUD} (AXI Uartlite, changed only in Vivado)",
        "rtd_notch_hz": f"{ctx.rtd_hz} (compile-time in main.c)",
        "spi_sck_khz": spec.SPI_SCK_HZ // 1000,
        "tmag_mode": "continuous, XYZ + temperature once per set",
        "print_resolution": "0.01 mT, 0.01 °C (firmware prints 2 decimals)",
    }
    out["_presets"] = store.preset_names()
    return out
