"""
Self-test for the instrument limits. No board, no Qt, no Ollama needed:

    cd host
    python -m instrument.selftest
"""

import tempfile
from pathlib import Path

from . import spec
from .settings import Context, SettingsStore


def main():
    failures = []

    def check(name, cond):
        print(("PASS " if cond else "FAIL ") + name)
        if not cond:
            failures.append(name)

    # Datasheet Table 7-2 (XYZ, no temperature) is reproduced within 3 %.
    for avg, ksps in spec.DATASHEET_XYZ_KSPS.items():
        got = spec.conversion_rate_hz(avg, temperature=False) / 1000
        check(f"Table 7-2 {avg}x: {got:.2f} vs {ksps} ksps",
              abs(got - ksps) <= 0.05 + 0.02 * ksps)   # table rounds

    store = SettingsStore(Path(tempfile.mkdtemp()) / "settings.json")
    v = store.validate

    check("399 Hz impossible (UART ~303 lines/s)",
          not v({"sample_rate_hz": 399}).ok)
    check("'max' resolves to the ceiling",
          v({"sample_rate_hz": "max"}).changes["sample_rate_hz"]
          == spec.max_rate_hz(32))
    check("the advertised maximum is itself accepted",
          v({"sample_rate_hz": spec.max_rate_hz(32)}).ok)
    check("averaging only 1 2 4 8 16 32",
          [a for a in range(1, 65) if v({"averaging": a}).ok]
          == list(spec.AVERAGING))
    check("range only 25 50 100",
          [r for r in range(1, 301) if v({"range_mT": r}).ok]
          == list(spec.RANGES_MT))
    check("range that would clip is refused",
          not v({"range_mT": 25},
                Context(field_abs_max_mT={"bx": 30.0})).ok)
    check("range that fits is accepted",
          v({"range_mT": 50}, Context(field_abs_max_mT={"bx": 30.0})).ok)
    check("rate below 1 Hz refused", not v({"sample_rate_hz": 0.5}).ok)
    check("unknown key refused", not v({"baud": 921600}).ok)
    check("one bad key rejects the whole request",
          not v({"window_s": 10, "averaging": 3}).ok)

    loop = spec.LoopModel()
    for hz in (1, 100, 250, 300):
        r = loop.r_for_rate(hz)
        check(f"{hz} Hz -> R {r} predicts {loop.rate_for_r(r):.1f} Hz",
              abs(loop.rate_for_r(r) - hz) / hz < 0.01)
    res = v({"sample_rate_hz": 10})
    check("10 Hz not exactly achievable -> neighbours offered: "
          + str(res.errors.get("sample_rate_hz", ""))[-40:],
          not res.ok and "9.8 Hz" in res.errors["sample_rate_hz"])
    check("a listed neighbour is accepted", v({"sample_rate_hz": 9.8}).ok)
    snapped = v({"sample_rate_hz": 10}, snap=True)
    check("GUI path snaps 10 -> 9.8 Hz with a note",
          snapped.ok and snapped.changes["sample_rate_hz"] == 9.8)
    check("no R reaches 399 Hz", loop.r_for_rate(399) is None)

    # Calibration must not drift on noisy slow-rate measurements (this is
    # what once pulled the ceiling down to 76.5 Hz).
    import random
    rnd = random.Random(1)
    cal = spec.LoopModel()
    true_loop = cal.overhead_us + cal.period_us(3)
    for _ in range(2000):
        secs = rnd.uniform(1, 30)
        lines = int(secs * 1e6 / true_loop * rnd.gauss(1, 0.03))
        cal.calibrate(lines, secs, 3)
    check("slow-rate noise never moves the overhead",
          cal.calibrated is None and spec.max_rate_hz(8, cal) > 300)
    cal = spec.LoopModel()
    real = 3000.0                                   # a slower real loop
    r = 200
    lines = int(12 * 1e6 / (real + cal.period_us(r)))
    check("a long count at 200 Hz is used",
          cal.calibrate(lines, 12, r) and abs(cal.overhead_us - real) < 50)
    check("a fit far outside the physics is refused",
          not spec.LoopModel().calibrate(int(12 * 1e6 / 20000), 12, 200))

    # -- processing ----------------------------------------------------
    import numpy as np
    from . import processing as P
    st = SettingsStore(Path(tempfile.mkdtemp()) / "s.json")
    st.values["sample_rate_hz"] = 100.0
    v = st.validate
    check("60 Hz notch impossible at 100 Hz sampling (aliases)",
          not v({"filter": "notch", "notch_hz": 60}).ok)
    check("lowpass above Nyquist refused",
          not v({"filter": "lowpass", "filter_cutoff_hz": 50}).ok)
    check("even median window refused",
          not v({"filter": "median", "filter_window": 10}).ok)
    st.values.update(filter="lowpass", filter_cutoff_hz=20.0)
    check("rate drop that would break the low-pass is refused",
          not v({"sample_rate_hz": 20}).ok)
    t = np.arange(0, 10, 0.01)
    cols = {"bx": np.full(t.size, 10.0), "by": np.zeros(t.size),
            "bz": np.zeros(t.size), "temp": np.full(t.size, 25.0),
            "rtd": np.full(t.size, 35.0), "mag": np.zeros(t.size)}
    out = P.Pipeline({"temp_comp": True, "temp_coeff_pct": -0.12,
                      "temp_ref_C": 25.0, "temp_comp_source": "rtd"})(t, cols)
    check("temp comp: 10 mT at +10 °C, NdFeB -> 10/(1-0.012)",
          abs(out["bx"][0] - 10 / 0.988) < 1e-9)
    spiky = dict(cols, bx=10 + 0.01 * np.random.default_rng(0)
                 .standard_normal(t.size))
    spiky["bx"][500] = 50.0
    if P.HAVE_SCIPY:
        out = P.Pipeline({"outlier": "hampel", "outlier_window": 21,
                          "outlier_k": 3.5})(t, spiky)
        check("hampel removes a spike", out["bx"].max() < 10.1)

    # -- field map -----------------------------------------------------
    from .fieldmap import MapError, MapModel
    check("3x3 grid", len(MapModel.grid("XY", 0, 20, 10, 0, 20, 10, 0)) == 9)
    for bad in (("XY", 0, 10, 0, 0, 10, 1, 0), ("XY", 10, 0, 1, 0, 10, 1, 0),
                ("XY", 0, 1000, 0.1, 0, 1000, 0.1, 0)):
        try:
            MapModel.grid(*bad)
            check(f"grid {bad} refused", False)
        except MapError:
            check(f"grid {bad[:7]} refused", True)
    try:
        MapModel.check_capture(1.0, 1.0, 3.0)
        check("1 s capture at 3 Hz refused (3 samples)", False)
    except MapError:
        check("1 s capture at 3 Hz refused (3 samples)", True)

    print("\n" + ("ALL PASSED" if not failures
                  else f"{len(failures)} FAILED"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
