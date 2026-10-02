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

    print("\n" + ("ALL PASSED" if not failures
                  else f"{len(failures)} FAILED"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
