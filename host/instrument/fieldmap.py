"""
Field map: move the Hall sensor by hand to known positions, capture an
averaged reading at each, and see the map build up.

    MapModel      pure data: plan, captured points, statistics, save/load
    FieldMapView  the "Field map" tab

A capture waits `settle_s` after you click (hands off the sensor), then
averages `average_s` of RAW samples -- untared, unfiltered, but temperature
compensated if that is switched on -- and stores mean and σ per channel.
A point whose σ is far above the expected noise is flagged: the sensor was
probably still moving.
"""

import csv
import json
import math
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from . import spec

PLANES = {"XY": ("x", "y", "z"), "XZ": ("x", "z", "y"), "YZ": ("y", "z", "x")}
COMPONENTS = {"mag": "|B|", "bx": "Bx", "by": "By", "bz": "Bz"}
MAX_POINTS = 2500
MAPS_DIR = Path(__file__).resolve().parents[1] / "maps"


class MapError(ValueError):
    pass


# ================================================================== model

class MapModel:

    def __init__(self):
        self.points = []          # dicts, see capture()
        self.plan = []            # [(x, y, z), ...] in mm
        self.plan_index = 0
        self.meta = {"created": datetime.now().isoformat(timespec="seconds"),
                     "units": {"position": "mm", "field": "mT",
                               "temperature": "degC"}}

    # -- plan --------------------------------------------------------------

    @staticmethod
    def grid(plane, a0, a1, da, b0, b1, db, fixed, serpentine=True):
        """Positions for a rectangular grid in one plane. Validated: a step
        that does not fit, an empty range or too many points is refused."""
        if plane not in PLANES:
            raise MapError(f"plane must be one of {', '.join(PLANES)}")
        for lo, hi, step, name in ((a0, a1, da, plane[0]),
                                   (b0, b1, db, plane[1])):
            if step <= 0:
                raise MapError(f"{name} step must be > 0")
            if hi < lo:
                raise MapError(f"{name}: 'to' must be >= 'from'")
        na = int(math.floor((a1 - a0) / da + 1e-9)) + 1
        nb = int(math.floor((b1 - b0) / db + 1e-9)) + 1
        if na * nb > MAX_POINTS:
            raise MapError(f"{na}×{nb} = {na * nb} points; the limit is "
                           f"{MAX_POINTS}. Use larger steps.")
        a_ax, b_ax, c_ax = PLANES[plane]
        out = []
        for j in range(nb):
            cols = range(na) if (not serpentine or j % 2 == 0) \
                else range(na - 1, -1, -1)
            for i in cols:
                p = {a_ax: round(a0 + i * da, 3), b_ax: round(b0 + j * db, 3),
                     c_ax: round(fixed, 3)}
                out.append((p["x"], p["y"], p["z"]))
        return out

    def set_plan(self, positions):
        self.plan = list(positions)
        self.plan_index = 0
        self._skip_captured()

    def _skip_captured(self):
        done = {self._key(p["x"], p["y"], p["z"]) for p in self.points}
        while self.plan_index < len(self.plan) and \
                self._key(*self.plan[self.plan_index]) in done:
            self.plan_index += 1

    @staticmethod
    def _key(x, y, z):
        return (round(x, 3), round(y, 3), round(z, 3))

    def next_position(self):
        if self.plan_index < len(self.plan):
            return self.plan[self.plan_index]
        return None

    def skip(self):
        if self.plan_index < len(self.plan):
            self.plan_index += 1

    # -- capture -----------------------------------------------------------

    @staticmethod
    def check_capture(average_s, settle_s, rate_hz):
        if not 0 <= settle_s <= 60:
            raise MapError("settle time must be 0..60 s")
        if not 0.1 <= average_s <= 600:
            raise MapError("averaging time must be 0.1..600 s")
        if rate_hz and average_s * rate_hz < 5:
            raise MapError(
                f"{average_s:g} s at {rate_hz:.3g} Hz is only "
                f"{average_s * rate_hz:.1f} samples; need >= 5, i.e. at "
                f"least {5 / rate_hz:.2f} s (or raise the sample rate)")

    def add(self, pos, t, cols, settings, temp_comp_applied):
        """Store one averaged point from raw samples (t, cols)."""
        n = len(t)
        if n < 5:
            raise MapError(f"only {n} samples arrived during the capture; "
                           "need >= 5 (is the board streaming?)")
        x, y, z = pos
        p = {"id": len(self.points) + 1, "x": x, "y": y, "z": z,
             "time": datetime.now().isoformat(timespec="seconds"),
             "n": int(n), "seconds": round(float(t[-1] - t[0]), 2),
             "temp_comp": bool(temp_comp_applied), "flags": []}
        for k in ("bx", "by", "bz", "mag", "temp", "rtd"):
            v = np.asarray(cols[k], dtype=float)
            p[k] = round(float(v.mean()), 5)
            p[k + "_std"] = round(float(v.std(ddof=1)), 5)
        avg = settings.get("averaging", 32)
        for k in ("bx", "by", "bz"):
            expect = math.hypot(spec.noise_ut(avg, "z" if k == "bz" else "xy"),
                                spec.PRINT_RESOLUTION_MT * 1000 / math.sqrt(12))
            if p[k + "_std"] * 1000 > 5 * expect:
                p["flags"].append(
                    f"{k} σ {p[k + '_std'] * 1000:.0f} µT > 5× expected "
                    f"({expect:.0f} µT): sensor moving?")
        rng = settings.get("range_mT", 100)
        for k in ("bx", "by", "bz"):
            if abs(cols[k]).max() >= 0.98 * rng:
                p["flags"].append(f"{k} reached ±{rng} mT full scale: "
                                  "clipped, use a larger range")
        p["settings"] = {k: settings.get(k) for k in
                         ("averaging", "range_mT", "sample_rate_hz",
                          "temp_comp", "temp_coeff_pct", "temp_ref_C",
                          "temp_comp_source")}
        # replace a previous capture at the same position
        key = self._key(x, y, z)
        self.points = [q for q in self.points
                       if self._key(q["x"], q["y"], q["z"]) != key]
        self.points.append(p)
        self._renumber()
        if self.next_position() is not None and \
                self._key(*self.next_position()) == key:
            self.plan_index += 1
        self._skip_captured()
        return p

    def remove(self, ids):
        ids = set(ids)
        self.points = [p for p in self.points if p["id"] not in ids]
        self._renumber()

    def _renumber(self):
        for i, p in enumerate(self.points, 1):
            p["id"] = i

    # -- statistics ----------------------------------------------------------

    def stats(self, comp="mag"):
        vals = np.array([p[comp] for p in self.points], dtype=float)
        out = {"points": len(vals), "planned": len(self.plan),
               "remaining": max(0, len(self.plan) - self.plan_index),
               "component": COMPONENTS.get(comp, comp)}
        if not len(vals):
            return out
        mean = float(vals.mean())
        p2p = float(np.ptp(vals))
        out.update(mean_mT=round(mean, 5), min_mT=round(float(vals.min()), 5),
                   max_mT=round(float(vals.max()), 5),
                   p2p_mT=round(p2p, 5),
                   std_mT=round(float(vals.std()), 5) if len(vals) > 1 else 0)
        if abs(mean) > 1e-9:
            out["homogeneity_ppm"] = round(p2p / abs(mean) * 1e6)
        temps = [p["rtd"] for p in self.points]
        out["rtd_range_C"] = round(max(temps) - min(temps), 3)
        out["flagged_points"] = [p["id"] for p in self.points if p["flags"]]
        return out

    # -- files ---------------------------------------------------------------

    def to_json(self):
        return {"meta": self.meta, "plan": self.plan,
                "plan_index": self.plan_index, "points": self.points}

    def save(self, path):
        Path(path).write_text(json.dumps(self.to_json(), indent=1),
                              encoding="utf-8")

    @classmethod
    def load(cls, path):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        m = cls()
        m.meta = data.get("meta", m.meta)
        m.plan = [tuple(p) for p in data.get("plan", [])]
        m.plan_index = int(data.get("plan_index", 0))
        m.points = list(data.get("points", []))
        return m

    def export_csv(self, path):
        keys = ["id", "x", "y", "z", "bx", "by", "bz", "mag", "bx_std",
                "by_std", "bz_std", "mag_std", "temp", "rtd", "n", "seconds",
                "temp_comp", "time", "flags"]
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["id", "x_mm", "y_mm", "z_mm", "Bx_mT", "By_mT",
                        "Bz_mT", "B_mT", "Bx_std_mT", "By_std_mT",
                        "Bz_std_mT", "B_std_mT", "T_die_C", "T_rtd_C",
                        "samples", "seconds", "temp_comp", "time", "flags"])
            for p in self.points:
                w.writerow([("; ".join(p[k]) if k == "flags" else p[k])
                            for k in keys])


# =================================================================== view

try:
    import pyqtgraph as pg
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QKeySequence, QShortcut
    from PySide6.QtWidgets import (
        QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
        QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
        QMessageBox, QPushButton, QScrollArea, QSplitter, QTableWidget,
        QTableWidgetItem, QVBoxLayout, QWidget,
    )
    HAVE_QT = True
except ImportError:                     # pragma: no cover
    HAVE_QT = False


def _spin(lo, hi, value, suffix=" mm", decimals=1, step=1.0):
    box = QDoubleSpinBox()
    box.setRange(lo, hi)
    box.setDecimals(decimals)
    box.setSingleStep(step)
    box.setValue(value)
    box.setSuffix(suffix)
    return box


if HAVE_QT:

    class FieldMapView(QWidget):

        def __init__(self):
            super().__init__()
            self.model = MapModel()
            self.controller = None
            self._capture = None        # (phase, deadline, acq_t0, pos)
            self._dirty = False
            self._build()
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._tick)
            self._timer.start(100)
            self._redraw()

        def attach(self, controller):
            self.controller = controller

        # -- layout --------------------------------------------------------

        def _build(self):
            outer = QHBoxLayout(self)
            outer.setContentsMargins(6, 6, 6, 6)
            split = QSplitter(Qt.Horizontal)
            outer.addWidget(split)

            side = QWidget()
            sl = QVBoxLayout(side)
            sl.setContentsMargins(4, 4, 4, 4)

            # live + target
            self.live = QLabel("live: --")
            self.live.setProperty("role", "readout")
            sl.addWidget(self.live)
            self.target = QLabel("Free mode: enter a position and capture.")
            self.target.setWordWrap(True)
            self.target.setStyleSheet("font-size: 14px; font-weight: 600;")
            sl.addWidget(self.target)

            # position
            pos = QGroupBox("Sensor position")
            pf = QFormLayout(pos)
            self.px = _spin(-1000, 1000, 0)
            self.py = _spin(-1000, 1000, 0)
            self.pz = _spin(-1000, 1000, 0)
            pf.addRow("X", self.px)
            pf.addRow("Y", self.py)
            pf.addRow("Z", self.pz)
            sl.addWidget(pos)

            # capture
            cap = QGroupBox("Capture")
            cf = QFormLayout(cap)
            self.settle = _spin(0, 60, 1.0, " s", 1, 0.5)
            self.average = _spin(0.1, 600, 2.0, " s", 1, 0.5)
            cf.addRow("Settle", self.settle)
            cf.addRow("Average", self.average)
            self.capture_btn = QPushButton("Capture  (Enter)")
            self.capture_btn.setProperty("role", "primary")
            self.capture_btn.clicked.connect(self.capture)
            cf.addRow(self.capture_btn)
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            self.skip_btn = QPushButton("Skip point")
            self.skip_btn.clicked.connect(self._skip)
            self.cancel_btn = QPushButton("Cancel")
            self.cancel_btn.clicked.connect(self._cancel)
            rl.addWidget(self.skip_btn)
            rl.addWidget(self.cancel_btn)
            cf.addRow(row)
            self.cap_state = QLabel("")
            self.cap_state.setProperty("role", "hint")
            self.cap_state.setWordWrap(True)
            cf.addRow(self.cap_state)
            sl.addWidget(cap)

            # grid plan
            grid = QGroupBox("Grid plan (optional)")
            gf = QFormLayout(grid)
            self.plane = QComboBox()
            self.plane.addItems(list(PLANES))
            self.plane.currentTextChanged.connect(self._plane_labels)
            gf.addRow("Plane", self.plane)
            self.a0, self.a1, self.da = (_spin(-1000, 1000, -20),
                                         _spin(-1000, 1000, 20),
                                         _spin(0.1, 1000, 10))
            self.b0, self.b1, self.db = (_spin(-1000, 1000, -20),
                                         _spin(-1000, 1000, 20),
                                         _spin(0.1, 1000, 10))
            self.fixed = _spin(-1000, 1000, 0)
            self._a_row, self._b_row = QWidget(), QWidget()
            for w, boxes in ((self._a_row, (self.a0, self.a1, self.da)),
                             (self._b_row, (self.b0, self.b1, self.db))):
                hl = QHBoxLayout(w)
                hl.setContentsMargins(0, 0, 0, 0)
                for b in boxes:
                    hl.addWidget(b)
            self._grid_form = gf
            gf.addRow("X from/to/step", self._a_row)
            gf.addRow("Y from/to/step", self._b_row)
            gf.addRow("Z", self.fixed)
            self.serp = QCheckBox("Serpentine order")
            self.serp.setChecked(True)
            gf.addRow(self.serp)
            plan_btn = QPushButton("Generate plan")
            plan_btn.clicked.connect(self._make_plan)
            gf.addRow(plan_btn)
            sl.addWidget(grid)

            # file
            files = QGroupBox("Map")
            ff = QFormLayout(files)
            self.comp = QComboBox()
            for k, label in COMPONENTS.items():
                self.comp.addItem(label, k)
            self.comp.currentIndexChanged.connect(lambda *_: self._redraw())
            ff.addRow("Show", self.comp)
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            for text, slot in (("New", self._new), ("Open…", self._open),
                               ("Save…", self._save),
                               ("CSV…", self._export)):
                b = QPushButton(text)
                b.clicked.connect(slot)
                rl.addWidget(b)
            ff.addRow(row)
            sl.addWidget(files)
            sl.addStretch(1)

            scroll = QScrollArea()
            scroll.setWidget(side)
            scroll.setWidgetResizable(True)
            scroll.setMinimumWidth(330)
            split.addWidget(scroll)

            right = QWidget()
            rv = QVBoxLayout(right)
            rv.setContentsMargins(0, 0, 0, 0)
            self.stats_label = QLabel("")
            self.stats_label.setProperty("role", "readout")
            self.stats_label.setWordWrap(True)
            rv.addWidget(self.stats_label)
            self.plot = pg.PlotWidget()
            self.plot.setAspectLocked(True)
            self.plot.showGrid(x=True, y=True, alpha=0.3)
            self.scatter = pg.ScatterPlotItem(size=16, pxMode=True)
            self.plan_marks = pg.ScatterPlotItem(
                size=7, pen=pg.mkPen("#888888"), brush=None, symbol="o")
            self.next_mark = pg.ScatterPlotItem(
                size=22, pen=pg.mkPen("#fab219", width=2), brush=None,
                symbol="s")
            for item in (self.plan_marks, self.scatter, self.next_mark):
                self.plot.addItem(item)
            self.cmap = pg.colormap.get("viridis")
            self.bar = pg.ColorBarItem(values=(0, 1), colorMap=self.cmap,
                                       interactive=False, width=12)
            self.plot.getPlotItem().layout.addItem(self.bar, 2, 5)
            rv.addWidget(self.plot, 3)
            self.table = QTableWidget(0, 9)
            self.table.setHorizontalHeaderLabels(
                ["#", "x", "y", "z", "|B| mT", "Bx", "By", "Bz", "flags"])
            self.table.horizontalHeader().setSectionResizeMode(
                QHeaderView.ResizeToContents)
            self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
            self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            rv.addWidget(self.table, 2)
            del_btn = QPushButton("Delete selected points")
            del_btn.clicked.connect(self._delete)
            rv.addWidget(del_btn)
            split.addWidget(right)
            split.setStretchFactor(1, 1)

            for seq in ("Return", "Enter"):
                sc = QShortcut(QKeySequence(seq), self)
                sc.setContext(Qt.WidgetWithChildrenShortcut)
                sc.activated.connect(self.capture)
            self._plane_labels(self.plane.currentText())

        def _plane_labels(self, plane):
            a, b, c = PLANES[plane]
            self._grid_form.labelForField(self._a_row).setText(
                f"{a.upper()} from/to/step")
            self._grid_form.labelForField(self._b_row).setText(
                f"{b.upper()} from/to/step")
            self._grid_form.labelForField(self.fixed).setText(c.upper())
            self.plot.setLabel("bottom", f"{a} [mm]")
            self.plot.setLabel("left", f"{b} [mm]")
            self._redraw()

        # -- the views' common interface -------------------------------------

        def set_visible_channels(self, visible):
            pass

        def restyle(self):
            self._redraw()

        def refresh(self, t, cols):
            if not len(t):
                return
            k = max(1, min(len(t), int(0.5 * len(t) / max(t[-1] - t[0], 1e-6))))
            self.live.setText(
                "live  |B| {:.3f}  Bx {:+.3f}  By {:+.3f}  Bz {:+.3f} mT   "
                "T_rtd {:.2f} °C".format(
                    *(float(np.mean(cols[c][-k:]))
                      for c in ("mag", "bx", "by", "bz", "rtd"))))

        # -- plan / position ---------------------------------------------------

        def _make_plan(self):
            try:
                plan = MapModel.grid(
                    self.plane.currentText(), self.a0.value(), self.a1.value(),
                    self.da.value(), self.b0.value(), self.b1.value(),
                    self.db.value(), self.fixed.value(), self.serp.isChecked())
            except MapError as e:
                QMessageBox.warning(self, "Grid", str(e))
                return
            self.model.set_plan(plan)
            self._goto_next()
            self._redraw()

        def make_plan(self, **kw):
            """For the assistant: same checks as the button."""
            plan = MapModel.grid(**kw)
            self.model.set_plan(plan)
            self._goto_next()
            self._redraw()
            return len(plan)

        def _goto_next(self):
            nxt = self.model.next_position()
            if nxt is None:
                if self.model.plan:
                    self.target.setText("Plan complete. Save the map, or "
                                        "enter a position to add points.")
                return
            self.px.setValue(nxt[0])
            self.py.setValue(nxt[1])
            self.pz.setValue(nxt[2])
            k, n = self.model.plan_index + 1, len(self.model.plan)
            self.target.setText(f"Move sensor to  X {nxt[0]:g}  Y {nxt[1]:g}"
                                f"  Z {nxt[2]:g} mm   ({k}/{n})")

        def _skip(self):
            self.model.skip()
            self._goto_next()
            self._redraw()

        # -- capture -----------------------------------------------------------

        def capture(self):
            if self._capture or self.controller is None:
                return
            c = self.controller
            if c.win.acq.reader is None:
                self.cap_state.setText("Connect the board (or simulator) "
                                       "first.")
                return
            rate = c.win.acq.measured_rate() or \
                c.store.values["sample_rate_hz"]
            try:
                MapModel.check_capture(self.average.value(),
                                       self.settle.value(), rate)
            except MapError as e:
                self.cap_state.setText(str(e))
                return
            pos = (self.px.value(), self.py.value(), self.pz.value())
            self._capture = ["settle", time.monotonic() + self.settle.value(),
                             None, pos]
            self.capture_btn.setEnabled(False)

        def _cancel(self):
            self._capture = None
            self.capture_btn.setEnabled(True)
            self.cap_state.setText("cancelled")

        def _tick(self):
            if not self._capture:
                return
            phase, deadline, t0, pos = self._capture
            left = deadline - time.monotonic()
            if phase == "settle":
                if left > 0:
                    self.cap_state.setText(f"settling… {left:.1f} s (hands "
                                           "off)")
                    return
                self._capture = ["average",
                                 time.monotonic() + self.average.value(),
                                 self.controller.acq_now(), pos]
                return
            if left > 0:
                self.cap_state.setText(f"averaging… {left:.1f} s")
                return
            self._capture = None
            self.capture_btn.setEnabled(True)
            self._finish(t0, pos)

        def _finish(self, t0, pos):
            c = self.controller
            t, cols = c.raw_since(t0)
            v = c.store.values
            if len(t) and v["temp_comp"]:
                cols = c.pipeline(t, cols, filtering=False)
            elif len(t):
                cols["mag"] = np.sqrt(cols["bx"] ** 2 + cols["by"] ** 2
                                      + cols["bz"] ** 2)
            try:
                p = self.model.add(pos, t, cols, v, v["temp_comp"])
            except MapError as e:
                self.cap_state.setText(str(e))
                return
            self._dirty = True
            msg = (f"#{p['id']} at ({pos[0]:g}, {pos[1]:g}, {pos[2]:g}): "
                   f"|B| {p['mag']:.4f} mT, σ {p['mag_std'] * 1000:.0f} µT, "
                   f"{p['n']} samples")
            if p["flags"]:
                msg += (f"  ⚠ {len(p['flags'])} warning(s) -- see the "
                        "table; Capture again at the same position to redo")
            self.cap_state.setText(msg)
            self.cap_state.setToolTip("\n".join(p["flags"]))
            self._goto_next()
            self._redraw()

        # -- drawing -------------------------------------------------------------

        def _redraw(self):
            comp = self.comp.currentData() if hasattr(self, "comp") else "mag"
            plane = self.plane.currentText() if hasattr(self, "plane") \
                else "XY"
            a, b, _c = PLANES[plane]
            pts = self.model.points
            if pts:
                vals = np.array([p[comp] for p in pts])
                lo, hi = float(vals.min()), float(vals.max())
                span = hi - lo if hi > lo else 1.0
                brushes = [pg.mkBrush(self.cmap.map((v - lo) / span,
                                                    mode="qcolor"))
                           for v in vals]
                self.scatter.setData(
                    [p[a] for p in pts], [p[b] for p in pts], brush=brushes,
                    pen=[pg.mkPen("#d03b3b", width=2) if p["flags"]
                         else pg.mkPen(None) for p in pts],
                    symbol="s")
                self.bar.setLevels((lo, hi if hi > lo else lo + 1e-6))
            else:
                self.scatter.setData([], [])
            plan = self.model.plan
            self.plan_marks.setData([p[{"x": 0, "y": 1, "z": 2}[a]]
                                     for p in plan],
                                    [p[{"x": 0, "y": 1, "z": 2}[b]]
                                     for p in plan])
            nxt = self.model.next_position()
            if nxt is not None:
                d = {"x": nxt[0], "y": nxt[1], "z": nxt[2]}
                self.next_mark.setData([d[a]], [d[b]])
            else:
                self.next_mark.setData([], [])
            s = self.model.stats(comp)
            if s["points"]:
                hom = (f"   homogeneity {s['homogeneity_ppm']:,} ppm"
                       if "homogeneity_ppm" in s else "")
                warn = ""
                if s["rtd_range_C"] > 0.5 and not (
                        self.controller
                        and self.controller.store.values["temp_comp"]):
                    warn = (f"   ⚠ RTD varied {s['rtd_range_C']:.2f} °C "
                            "during the map; consider temperature "
                            "compensation")
                self.stats_label.setText(
                    f"{s['component']}: {s['points']} points"
                    + (f" ({s['remaining']} planned left)" if s["planned"]
                       else "")
                    + f"   mean {s['mean_mT']:.4f}   min {s['min_mT']:.4f}"
                    f"   max {s['max_mT']:.4f}   p-p {s['p2p_mT'] * 1000:.1f} µT"
                    + hom + warn)
            else:
                self.stats_label.setText(
                    "No points yet. Put the sensor at a known position, enter "
                    "it on the left and press Capture -- or generate a grid "
                    "plan first.")
            self.table.setRowCount(len(pts))
            for r, p in enumerate(pts):
                cells = [p["id"], p["x"], p["y"], p["z"], f"{p['mag']:.4f}",
                         f"{p['bx']:+.4f}", f"{p['by']:+.4f}",
                         f"{p['bz']:+.4f}", "; ".join(p["flags"])]
                for col, val in enumerate(cells):
                    self.table.setItem(r, col, QTableWidgetItem(str(val)))

        # -- files ---------------------------------------------------------------

        def _confirm_discard(self):
            if not self._dirty or not self.model.points:
                return True
            return QMessageBox.question(
                self, "Field map", "Discard the unsaved map?") == \
                QMessageBox.Yes

        def _new(self):
            if self._confirm_discard():
                self.model = MapModel()
                self._dirty = False
                self.target.setText("Free mode: enter a position and "
                                    "capture.")
                self._redraw()

        def _open(self):
            if not self._confirm_discard():
                return
            MAPS_DIR.mkdir(exist_ok=True)
            path, _ = QFileDialog.getOpenFileName(
                self, "Open field map", str(MAPS_DIR), "Field map (*.json)")
            if path:
                try:
                    self.model = MapModel.load(path)
                except (OSError, ValueError, KeyError) as e:
                    QMessageBox.warning(self, "Field map", str(e))
                    return
                self._dirty = False
                self._goto_next()
                self._redraw()

        def _save(self):
            MAPS_DIR.mkdir(exist_ok=True)
            name = datetime.now().strftime("fieldmap_%Y%m%d_%H%M.json")
            path, _ = QFileDialog.getSaveFileName(
                self, "Save field map", str(MAPS_DIR / name),
                "Field map (*.json)")
            if path:
                self.model.save(path)
                self._dirty = False

        def _export(self):
            MAPS_DIR.mkdir(exist_ok=True)
            name = datetime.now().strftime("fieldmap_%Y%m%d_%H%M.csv")
            path, _ = QFileDialog.getSaveFileName(
                self, "Export CSV", str(MAPS_DIR / name), "CSV (*.csv)")
            if path:
                self.model.export_csv(path)

        def _delete(self):
            rows = {i.row() for i in self.table.selectedIndexes()}
            ids = [self.model.points[r]["id"] for r in rows]
            if ids:
                self.model.remove(ids)
                self._dirty = True
                self._redraw()

        # -- for the assistant -----------------------------------------------

        def status(self, comp="mag"):
            s = self.model.stats(comp)
            nxt = self.model.next_position()
            s["next_position_mm"] = nxt
            s["capturing"] = bool(self._capture)
            s["settle_s"] = self.settle.value()
            s["average_s"] = self.average.value()
            s["last_points"] = [
                {k: p[k] for k in ("id", "x", "y", "z", "mag", "bx", "by",
                                   "bz", "mag_std", "rtd", "flags")}
                for p in self.model.points[-5:]]
            return s
