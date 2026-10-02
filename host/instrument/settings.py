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
from pathlib import Path

from . import spec

SETTINGS_PATH = Path(__file__).resolve().parents[1] / "settings.json"

VIEWS = ("strip", "vector", "spectrum", "distribution")
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
    "smoothing": dict(group="display", type="int", min=1, max=200,
                      unit="samples",
                      help="Moving average for display only; recording and "
                           "export stay raw."),
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
}

BOARD_KEYS = [k for k, s in SCHEMA.items() if s["group"] == "board"]
DISPLAY_KEYS = [k for k, s in SCHEMA.items() if s["group"] == "display"]

DEFAULTS = {
    "averaging": 32, "sample_rate_hz": 3.0, "range_mT": 100,
    "streaming": True,
    "window_s": 20.0, "fps": 30, "smoothing": 1, "autoscale": True,
    "visible_channels": list(CHANNELS), "view": "strip",
    "spectrum_segments": 4, "theme": "dark",
}

BUILTIN_PRESETS = {
    "noise floor": dict(
        averaging=32, sample_rate_hz=20, window_s=60, smoothing=1,
        view="distribution",
        _about="Quietest readings (32x averaging) for measuring sigma."),
    "balanced": dict(
        averaging=8, sample_rate_hz=100, window_s=20, smoothing=1,
        view="strip", _about="General use."),
    "fast": dict(
        averaging=1, sample_rate_hz="max", window_s=5, smoothing=1,
        view="spectrum",
        _about="Highest delivered rate the serial link allows; noisiest."),
    "slow logging": dict(
        averaging=32, sample_rate_hz=2, window_s=600, smoothing=1,
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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".settings.")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, self.path)       # atomic: never a half-written file
        self._last_written = text
        return True

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

        # display settings that depend on the data rate
        rate = after["sample_rate_hz"]
        n = after["window_s"] * rate
        if any(k in res.changes for k in ("smoothing", "window_s",
                                          "sample_rate_hz")):
            if after["smoothing"] > 1 and after["smoothing"] >= n:
                res.errors["smoothing"] = (
                    f"moving average of {after['smoothing']} samples needs "
                    f"more than {after['smoothing']} samples on screen; "
                    f"window {after['window_s']:g} s x {rate:g} Hz = "
                    f"{n:.0f}. Use smoothing <= {max(1, int(n) - 1)} or a "
                    "longer window.")
                res.changes.pop("smoothing", None)

        self._notes(res, after, ctx)
        return res

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
    out["_fixed_in_hardware"] = {
        "uart_baud": f"{spec.BAUD} (AXI Uartlite, changed only in Vivado)",
        "rtd_notch_hz": f"{ctx.rtd_hz} (compile-time in main.c)",
        "spi_sck_khz": spec.SPI_SCK_HZ // 1000,
        "tmag_mode": "continuous, XYZ + temperature once per set",
        "print_resolution": "0.01 mT, 0.01 °C (firmware prints 2 decimals)",
    }
    out["_presets"] = store.preset_names()
    return out
